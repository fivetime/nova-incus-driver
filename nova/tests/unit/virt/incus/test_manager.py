# Copyright 2026 OpenStack Incus contributors
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import contextlib
import copy
import dataclasses
import os
import threading
import time
from unittest import mock

import fixtures
from oslo_serialization import jsonutils

from nova.compute import power_state
from nova.compute import task_states
from nova.compute import vm_states
from nova import context
from nova import exception
from nova import test
from nova.virt.incus import manager
from nova.virt.incus import migrate_data


class IncusComputeManagerTest(test.NoDBTestCase):

    def setUp(self):
        super().setUp()
        self.flags(migration_auto_recovery=True, group='incus')
        self.flags(volume_usage_poll_interval=60)
        self.state_path = self.useFixture(fixtures.TempDir()).path
        self.flags(state_path=self.state_path)
        self.flags(instances_path=self.state_path)
        self.compute = manager.IncusComputeManager.__new__(
            manager.IncusComputeManager)
        self.compute.host = 'compute-1'
        self.compute.driver = mock.Mock()
        self.compute.driver.get_cold_attachment_rotation.return_value = None
        self.mount_table = {}
        self.compute.driver.get_share_mount_table.return_value = (
            self.mount_table)
        self.host_id = '20000000-0000-0000-0000-000000000002'
        self.materialization_id = '30000000-0000-0000-0000-000000000003'
        self.idmap_assignment = (
            manager.incus_driver.incus_idmap.IDMapAssignment(
                instance_uuid='00000000-0000-0000-0000-000000000001',
                base=500000000,
                size=65536,
                slot=0,
                allocation_id='10000000-0000-0000-0000-000000000001',
                fingerprint='a' * 64,
                host_ids=(self.host_id,)))
        self.idmap_claim = self._host_claim()
        self.idmap_released_assignment = self._assignment(host_ids=())
        self.idmap_intent = (
            manager.incus_driver.incus_idmap.IDMapReleaseIntent(
                instance_uuid=self.idmap_assignment.instance_uuid,
                instance_name='instance-00000001',
                base=self.idmap_assignment.base,
                size=self.idmap_assignment.size,
                slot=self.idmap_assignment.slot,
                allocation_id=self.idmap_assignment.allocation_id,
                fingerprint=self.idmap_assignment.fingerprint))
        self.compute.driver.idmap_allocator.get.return_value = (
            self.idmap_assignment)
        self.compute.driver.idmap_allocator.request_release.return_value = (
            self.idmap_intent)
        self.compute.driver.idmap_allocator.retire_claim.return_value = (
            self.idmap_released_assignment)
        self.compute.driver.idmap_allocator.get_host_claim.return_value = (
            self.idmap_claim)
        self.compute.driver._settle_idmap_host_claim.return_value = (
            self.idmap_claim)
        self.compute.driver.idmap_allocator.release.return_value = True
        list_intents = (
            self.compute.driver.idmap_allocator.
            list_release_intent_candidates)
        list_intents.side_effect = AssertionError(
            'per-compute replay must not scan the fleet release prefix')
        self.compute.driver.idmap_allocator.list_host_claims.return_value = [
            self.idmap_claim]
        self.compute.driver.idmap_allocator.\
            list_release_intents_for_instances.return_value = [
                self.idmap_intent]
        audit = self.compute.driver.idmap_allocator.run_coordinated_audit
        audit.return_value = (True, None)
        self.get_instances_by_host = mock.Mock(return_value=[])
        self.compute._local_incus_idmap_claim_instances = (
            self.get_instances_by_host)
        self.compute.driver.client.instances.get.side_effect = RuntimeError(
            'not found')
        self.compute.driver.client.profiles.get.side_effect = RuntimeError(
            'not found')
        self.compute.driver.inventory_client = self.compute.driver.client
        instances_response = (
            self.compute.driver.inventory_client.api.instances.get.
            return_value)
        instances_response.json.return_value = {'metadata': []}
        profiles_response = (
            self.compute.driver.inventory_client.api.profiles.get.return_value)
        profiles_response.json.return_value = {'metadata': []}
        self.useFixture(fixtures.MockPatchObject(
            manager.incus_driver, '_is_incus_not_found', return_value=True))
        self.useFixture(fixtures.MockPatchObject(
            manager.virt_node, 'read_local_node_uuid',
            return_value=self.host_id))
        self.compute.network_api = mock.Mock()
        self.compute._get_instance_block_device_info = mock.Mock(
            return_value={'block_device_mapping': []})

    def _idmap_instance(self):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            system_metadata={
                manager.incus_driver.IDMAP_BASE_METADATA_KEY: '500000000',
                manager.incus_driver.IDMAP_SIZE_METADATA_KEY: '65536',
                manager.incus_driver.IDMAP_ALLOCATION_METADATA_KEY:
                    '10000000-0000-0000-0000-000000000001',
                manager.incus_driver.IDMAP_FINGERPRINT_METADATA_KEY: 'a' * 64,
            })
        instance.name = 'instance-00000001'
        instance.obj_attr_is_set.return_value = True
        return instance

    def _adoption_deleted_row(self, name='instance-00000001'):
        instance = mock.Mock()
        instance.name = name
        instance.deleted = True
        instance.obj_attr_is_set.return_value = True
        return instance

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_orphan_is_given_the_intent_it_lacks(self, get_by_uuid):
        allocator = self.compute.driver.idmap_allocator
        allocator.get_release_intent.return_value = None
        get_by_uuid.return_value = self._adoption_deleted_row()
        orphan = self._assignment(host_ids=())

        self.compute._adopt_unclaimed_incus_idmap_allocations(
            allocator, [orphan])

        allocator.request_release.assert_called_once_with(
            orphan.instance_uuid, 'instance-00000001', assignment=orphan)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_claimed_allocation_is_left_alone(self, get_by_uuid):
        allocator = self.compute.driver.idmap_allocator
        allocator.get_release_intent.return_value = None

        self.compute._adopt_unclaimed_incus_idmap_allocations(
            allocator, [self._assignment()])

        allocator.request_release.assert_not_called()
        get_by_uuid.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_allocation_that_already_has_an_intent_is_left_alone(
            self, get_by_uuid):
        allocator = self.compute.driver.idmap_allocator
        allocator.get_release_intent.return_value = self.idmap_intent

        self.compute._adopt_unclaimed_incus_idmap_allocations(
            allocator, [self._assignment(host_ids=())])

        allocator.request_release.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_in_flight_build_is_not_mistaken_for_an_orphan(self, get_by_uuid):
        """Between allocate() and the first claim, host_ids is empty.

        A live Nova row is what tells the two apart; adopting one would
        aim a release barrier at a build that is still running.
        """
        allocator = self.compute.driver.idmap_allocator
        allocator.get_release_intent.return_value = None
        live = mock.Mock()
        live.deleted = False
        live.obj_attr_is_set.return_value = True
        get_by_uuid.return_value = live

        self.compute._adopt_unclaimed_incus_idmap_allocations(
            allocator, [self._assignment(host_ids=())])

        allocator.request_release.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_missing_nova_row_is_reported_not_guessed(self, get_by_uuid):
        # The intent binds an exact instance name and the replay path
        # refuses any mismatch, so a name that cannot be established from
        # Nova must not be invented.
        allocator = self.compute.driver.idmap_allocator
        allocator.get_release_intent.return_value = None
        get_by_uuid.side_effect = manager.exception.InstanceNotFound(
            instance_id='00000000-0000-0000-0000-000000000001')

        self.compute._adopt_unclaimed_incus_idmap_allocations(
            allocator, [self._assignment(host_ids=())])

        allocator.request_release.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_one_failure_does_not_stop_the_remaining_orphans(
            self, get_by_uuid):
        allocator = self.compute.driver.idmap_allocator
        allocator.get_release_intent.side_effect = [
            RuntimeError('etcd read failed'), None]
        get_by_uuid.return_value = self._adoption_deleted_row(
            'instance-00000002')
        second = self._assignment(
            host_ids=(),
            instance_uuid='00000000-0000-0000-0000-000000000002', slot=1)

        self.compute._adopt_unclaimed_incus_idmap_allocations(
            allocator, [self._assignment(host_ids=()), second])

        allocator.request_release.assert_called_once_with(
            second.instance_uuid, 'instance-00000002', assignment=second)

    def test_unclaimed_adoption_rotates_before_external_reads(self):
        allocator = self.compute.driver.idmap_allocator
        allocator.get_release_intent.return_value = self.idmap_intent
        assignments = [
            self._assignment(
                host_ids=(), slot=index,
                base=500000000 + index * 65536,
                instance_uuid=(
                    '00000000-0000-4000-8000-{:012x}'.format(index + 1)),
                allocation_id=(
                    '10000000-0000-4000-8000-{:012x}'.format(index + 1)))
            for index in range(250)
        ]

        expected = [
            assignment.instance_uuid for assignment in assignments]
        observed = []
        for expected_start in (0, 100, 200):
            allocator.get_release_intent.reset_mock()
            self.compute._adopt_unclaimed_incus_idmap_allocations(
                allocator, assignments)
            calls = allocator.get_release_intent.call_args_list
            self.assertEqual(100, len(calls))
            observed_batch = [call.args[0] for call in calls]
            self.assertEqual(
                (expected[expected_start:] + expected[:expected_start])[:100],
                observed_batch)
            observed.extend(observed_batch)

        self.assertEqual(set(expected), set(observed))

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_existing_intents_do_not_starve_missing_tail(self, get_by_uuid):
        allocator = self.compute.driver.idmap_allocator
        assignments = [
            self._assignment(
                host_ids=(), slot=index,
                base=500000000 + index * 65536,
                instance_uuid=(
                    '00000000-0000-4000-8000-{:012x}'.format(index + 1)),
                allocation_id=(
                    '10000000-0000-4000-8000-{:012x}'.format(index + 1)))
            for index in range(1001)
        ]
        intents = [
            manager.incus_driver.incus_idmap.IDMapReleaseIntent(
                instance_uuid=assignment.instance_uuid,
                instance_name='instance-{:08x}'.format(index + 1),
                base=assignment.base, size=assignment.size,
                slot=assignment.slot,
                allocation_id=assignment.allocation_id,
                fingerprint=assignment.fingerprint)
            for index, assignment in enumerate(assignments[:-1])
        ]
        missing = assignments[-1]
        allocator.get_release_intent.return_value = None
        get_by_uuid.return_value = self._adoption_deleted_row(
            'instance-00001001')

        self.compute._adopt_unclaimed_incus_idmap_allocations(
            allocator, assignments, intents)

        allocator.get_release_intent.assert_called_once_with(
            missing.instance_uuid)
        allocator.request_release.assert_called_once_with(
            missing.instance_uuid, 'instance-00001001', assignment=missing)

    def test_unclaimed_release_replay_rotates_before_external_io(self):
        assignments = [
            self._assignment(
                host_ids=(), slot=index,
                base=500000000 + index * 65536,
                instance_uuid=(
                    '00000000-0000-4000-8000-{:012x}'.format(index + 1)),
                allocation_id=(
                    '10000000-0000-4000-8000-{:012x}'.format(index + 1)))
            for index in range(250)
        ]
        intents = [
            manager.incus_driver.incus_idmap.IDMapReleaseIntent(
                instance_uuid=assignment.instance_uuid,
                instance_name='instance-{:08x}'.format(index + 1),
                base=assignment.base, size=assignment.size,
                slot=assignment.slot,
                allocation_id=assignment.allocation_id,
                fingerprint=assignment.fingerprint)
            for index, assignment in enumerate(assignments)
        ]
        replay = mock.Mock()
        self.compute._replay_incus_idmap_release = replay
        self.compute._idmap_screening_inventory = mock.Mock(
            return_value=mock.sentinel.inventory)

        expected = [intent.instance_uuid for intent in intents]
        observed = []
        for expected_start in (0, 100, 200):
            replay.reset_mock()
            self.compute._replay_unclaimed_incus_idmap_releases(
                context.get_admin_context(),
                self.compute.driver.idmap_allocator,
                assignments, intents)
            calls = replay.call_args_list
            self.assertEqual(100, len(calls))
            observed_batch = [call.args[2].instance_uuid for call in calls]
            self.assertEqual(
                (expected[expected_start:] + expected[:expected_start])[:100],
                observed_batch)
            observed.extend(observed_batch)

        self.assertEqual(set(expected), set(observed))

    def _assignment(self, host_ids=None, **overrides):
        values = {
            'instance_uuid': '00000000-0000-0000-0000-000000000001',
            'base': 500000000,
            'size': 65536,
            'slot': 0,
            'allocation_id': '10000000-0000-0000-0000-000000000001',
            'fingerprint': 'a' * 64,
            'host_ids': ((getattr(self, 'host_id', None),)
                         if host_ids is None else host_ids),
        }
        values.update(overrides)
        return manager.incus_driver.incus_idmap.IDMapAssignment(**values)

    def _host_claim(self, **overrides):
        values = {
            'host_id': self.host_id,
            'materialization_id': self.materialization_id,
            'instance_uuid': self.idmap_assignment.instance_uuid,
            'base': self.idmap_assignment.base,
            'size': self.idmap_assignment.size,
            'slot': self.idmap_assignment.slot,
            'allocation_id': self.idmap_assignment.allocation_id,
            'fingerprint': self.idmap_assignment.fingerprint,
            'state': 'cleaned',
            'proof': mock.sentinel.idmap_cleanup_proof,
        }
        values.update(overrides)
        return manager.incus_driver.incus_idmap.IDMapHostClaim(**values)

    @mock.patch.object(manager.manager.ComputeManager, '_delete_instance',
                       return_value=mock.sentinel.result)
    def test_final_delete_releases_idmap_after_nova_deletion(
            self, base_delete):
        instance = self._idmap_instance()

        def delete_after_intent(*args):
            request_release = (
                self.compute.driver.idmap_allocator.request_release)
            request_release.assert_called_once_with(
                instance.uuid, instance.name,
                assignment=self.idmap_assignment)
            return mock.sentinel.result

        base_delete.side_effect = delete_after_intent
        result = self.compute._delete_instance(
            mock.sentinel.context, instance, mock.sentinel.bdms)

        self.assertIs(mock.sentinel.result, result)
        base_delete.assert_called_once_with(
            mock.sentinel.context, instance, mock.sentinel.bdms)
        retire_claim = self.compute.driver.idmap_allocator.retire_claim
        retire_claim.assert_called_once_with(
            instance.uuid, self.host_id, self.materialization_id,
            assignment=self.idmap_assignment)
        self.compute.driver.idmap_allocator.release.assert_called_once_with(
            self.idmap_intent)
        self.compute.driver._settle_idmap_host_claim.assert_called_once_with(
            instance, self.idmap_claim, final_delete=True)

    @mock.patch.object(manager.manager.ComputeManager, '_delete_instance',
                       return_value=mock.sentinel.result)
    def test_final_delete_settles_a_possible_claim_that_never_committed(
            self, base_delete):
        """A crash mid-create leaves 'possible' with no container to promote.

        The gate used to accept only a committed container as evidence,
        making such an instance permanently undeletable. The materialization
        attempt settlement is the remaining authority and must unblock the
        delete once it proves non-materialization.
        """
        instance = self._idmap_instance()
        possible = self._host_claim(state='possible', proof=None)
        cleaned = self._host_claim(
            proof=mock.Mock(instance_name=instance.name))
        allocator = self.compute.driver.idmap_allocator
        allocator.get_host_claim.return_value = possible
        allocator.request_release.return_value = self.idmap_intent
        promote = self.compute.driver._promote_idmap_claim_if_server_committed
        # Incus has no committed container for this claim.
        promote.return_value = (self.idmap_assignment, possible)
        self.compute.driver._settle_idmap_host_claim.return_value = cleaned

        result = self.compute._delete_instance(
            mock.sentinel.context, instance, mock.sentinel.bdms)

        self.assertIs(mock.sentinel.result, result)
        self.compute.driver._settle_idmap_host_claim.assert_any_call(
            instance, possible, final_delete=False)
        base_delete.assert_called_once_with(
            mock.sentinel.context, instance, mock.sentinel.bdms)

    @mock.patch.object(manager.manager.ComputeManager, '_delete_instance',
                       return_value=mock.sentinel.result)
    def test_final_delete_still_refuses_an_unsettleable_possible_claim(
            self, base_delete):
        instance = self._idmap_instance()
        possible = self._host_claim(state='possible', proof=None)
        allocator = self.compute.driver.idmap_allocator
        allocator.get_host_claim.return_value = possible
        allocator.request_release.return_value = self.idmap_intent
        promote = self.compute.driver._promote_idmap_claim_if_server_committed
        promote.return_value = (self.idmap_assignment, possible)
        # Settlement cannot produce a cleaned claim either: the manager
        # wrapper surfaces that as an IDMapError, which the gate converts
        # back into its fail-closed conflict.
        self.compute.driver._settle_idmap_host_claim.return_value = possible

        self.assertRaises(
            manager.incus_driver.incus_idmap.IDMapConflict,
            self.compute._delete_instance,
            mock.sentinel.context, instance, mock.sentinel.bdms)
        base_delete.assert_not_called()

    @mock.patch.object(manager.manager.ComputeManager, '_delete_instance',
                       return_value=mock.sentinel.result)
    def test_final_delete_tolerates_claim_retired_elsewhere(
            self, base_delete):
        """A migration that moved ownership already retired this claim.

        _exact_idmap_host_claim returns None once this host leaves the
        allocation's host index, and the delete path used to dereference
        that None and abandon the release with an AttributeError.
        """
        instance = self._idmap_instance()
        allocator = self.compute.driver.idmap_allocator
        # The host left the allocation's index and its claim key is gone:
        # that is exactly the state a completed ownership transfer leaves.
        allocator.get.return_value = self._assignment(host_ids=())
        allocator.get_host_claim.return_value = None
        allocator.request_release.return_value = self.idmap_intent

        result = self.compute._delete_instance(
            mock.sentinel.context, instance, mock.sentinel.bdms)

        self.assertIs(mock.sentinel.result, result)
        self.compute.driver._settle_idmap_host_claim.assert_not_called()
        allocator.retire_claim.assert_not_called()
        allocator.release.assert_called_once_with(self.idmap_intent)

    @mock.patch.object(manager.manager.ComputeManager, '_delete_instance',
                       return_value=mock.sentinel.result)
    def test_final_delete_releases_lock_during_nova_deletion(
            self, base_delete):
        # driver.destroy (inside Nova's delete) takes the same per-instance
        # claim lock for its rootfs release receipt; holding the release
        # lock across super()._delete_instance self-deadlocks final delete.
        instance = self._idmap_instance()
        lock_name = manager._idmap_release_lock_name(instance.uuid)
        observed = {}

        def probe(*args):
            semaphore = manager.lockutils.internal_lock(lock_name)
            acquired = semaphore.acquire(blocking=False)
            observed['lock_free'] = acquired
            if acquired:
                semaphore.release()
            return mock.sentinel.result

        base_delete.side_effect = probe
        result = self.compute._delete_instance(
            mock.sentinel.context, instance, mock.sentinel.bdms)

        self.assertIs(mock.sentinel.result, result)
        self.assertTrue(observed['lock_free'])

    @mock.patch.object(manager.manager.ComputeManager, '_delete_instance',
                       return_value=mock.sentinel.result)
    def test_final_delete_settles_exact_claim_before_retire(
            self, base_delete):
        assignment = self.idmap_assignment
        retired = self._assignment(host_ids=())
        allocator = self.compute.driver.idmap_allocator
        allocator.get.return_value = assignment
        allocator.request_release.return_value = self.idmap_intent
        allocator.retire_claim.return_value = retired
        calls = mock.Mock()
        calls.attach_mock(
            self.compute.driver._settle_idmap_host_claim, 'settle')
        calls.attach_mock(allocator.retire_claim, 'retire')
        calls.attach_mock(allocator.release, 'release')

        result = self.compute._delete_instance(
            mock.sentinel.context, self._idmap_instance(),
            mock.sentinel.bdms)

        self.assertIs(mock.sentinel.result, result)
        self.assertEqual([
            mock.call.settle(
                mock.ANY, self.idmap_claim, final_delete=True),
            mock.call.retire(
                self.idmap_intent.instance_uuid, self.host_id,
                self.materialization_id,
                assignment=assignment),
            mock.call.release(self.idmap_intent),
        ], calls.mock_calls)

    @mock.patch.object(manager.manager.ComputeManager, '_delete_instance',
                       return_value=mock.sentinel.result)
    def test_final_delete_promotes_exact_possible_claim_before_delete(
            self, base_delete):
        possible = self._host_claim(state='possible', proof=None)
        committed = self._host_claim(state='committed', proof=None)
        cleaned = self._host_claim(
            state='cleaned', proof=mock.sentinel.idmap_cleanup_proof)
        allocator = self.compute.driver.idmap_allocator
        allocator.get_host_claim.side_effect = [
            possible, committed, cleaned]
        promote = (
            self.compute.driver._promote_idmap_claim_if_server_committed)
        promote.return_value = self.idmap_assignment, committed
        self.compute.driver._settle_idmap_host_claim.return_value = cleaned
        ordered = mock.Mock()
        ordered.attach_mock(
            self.compute.driver._promote_idmap_claim_if_server_committed,
            'promote')
        ordered.attach_mock(base_delete, 'delete')

        result = self.compute._delete_instance(
            mock.sentinel.context, self._idmap_instance(),
            mock.sentinel.bdms)

        self.assertIs(mock.sentinel.result, result)
        self.assertEqual('promote', ordered.mock_calls[0][0])
        self.assertEqual('delete', ordered.mock_calls[1][0])
        self.compute.driver._settle_idmap_host_claim.assert_called_once_with(
            mock.ANY, committed, final_delete=True)
        allocator.retire_claim.assert_called_once()
        allocator.release.assert_called_once_with(self.idmap_intent)

    @mock.patch.object(manager.manager.ComputeManager, '_delete_instance')
    def test_final_delete_retains_unresolved_possible_claim(
            self, base_delete):
        """The unknown-materialization invariant survives the settle path.

        A 'possible' claim the server cannot promote is now handed to the
        attempt settlement; only when THAT cannot prove non-materialization
        either does the delete stay refused. The claim is retained.
        """
        possible = self._host_claim(state='possible', proof=None)
        allocator = self.compute.driver.idmap_allocator
        allocator.get_host_claim.return_value = possible
        promote = (
            self.compute.driver._promote_idmap_claim_if_server_committed)
        promote.return_value = self.idmap_assignment, possible
        settle = self.compute.driver._settle_idmap_host_claim
        settle.side_effect = manager.incus_driver.incus_idmap.IDMapConflict(
            reason='attempt is still active on the server')

        self.assertRaises(
            manager.incus_driver.incus_idmap.IDMapConflict,
            self.compute._delete_instance,
            mock.sentinel.context, self._idmap_instance(),
            mock.sentinel.bdms)

        base_delete.assert_not_called()
        settle.assert_called_once_with(
            mock.ANY, possible, final_delete=False)
        allocator.retire_claim.assert_not_called()
        allocator.release.assert_not_called()

    @mock.patch.object(manager.manager.ComputeManager, '_delete_instance',
                       return_value=mock.sentinel.result)
    def test_settlement_failure_retains_exact_claim(self, base_delete):
        assignment = self.idmap_assignment
        allocator = self.compute.driver.idmap_allocator
        allocator.get.return_value = assignment
        allocator.request_release.return_value = self.idmap_intent
        settle = self.compute.driver._settle_idmap_host_claim
        settle.side_effect = RuntimeError('proof unavailable')

        result = self.compute._delete_instance(
            mock.sentinel.context, self._idmap_instance(),
            mock.sentinel.bdms)

        self.assertIs(mock.sentinel.result, result)
        allocator.retire_claim.assert_not_called()
        allocator.release.assert_not_called()

    @mock.patch.object(manager.manager.ComputeManager, '_delete_instance',
                       return_value=mock.sentinel.result)
    def test_final_delete_idmap_release_failure_leaves_shared_intent(
            self, base_delete):
        instance = self._idmap_instance()
        self.compute.driver.idmap_allocator.release.side_effect = RuntimeError(
            'etcd unavailable')

        result = self.compute._delete_instance(
            mock.sentinel.context, instance,
            mock.sentinel.bdms)

        self.assertIs(mock.sentinel.result, result)
        request_release = self.compute.driver.idmap_allocator.request_release
        request_release.assert_called_once()
        self.compute.driver.idmap_allocator.retire_claim.assert_called_once()

    @mock.patch.object(manager.manager.ComputeManager, '_delete_instance',
                       side_effect=RuntimeError('Nova delete failed'))
    def test_nova_delete_failure_keeps_shared_intent(
            self, base_delete):
        instance = self._idmap_instance()
        self.assertRaises(
            RuntimeError, self.compute._delete_instance,
            mock.sentinel.context, instance,
            mock.sentinel.bdms)

        request_release = self.compute.driver.idmap_allocator.request_release
        request_release.assert_called_once()
        self.compute.driver.idmap_allocator.retire_claim.assert_not_called()
        self.compute.driver.idmap_allocator.release.assert_not_called()

    @mock.patch.object(manager.manager.ComputeManager, '_delete_instance',
                       return_value=mock.sentinel.result)
    def test_final_delete_requests_intent_without_nova_metadata(
            self, base_delete):
        instance = self._idmap_instance()
        instance.system_metadata = {}

        result = self.compute._delete_instance(
            mock.sentinel.context, instance, mock.sentinel.bdms)

        self.assertIs(mock.sentinel.result, result)
        request_release = self.compute.driver.idmap_allocator.request_release
        request_release.assert_called_once_with(
            instance.uuid, instance.name,
            assignment=self.idmap_assignment)

    @mock.patch.object(manager.manager.ComputeManager, '_delete_instance')
    def test_final_delete_request_failure_precedes_destruction(
            self, base_delete):
        self.compute.driver.idmap_allocator.request_release.side_effect = (
            RuntimeError('etcd unavailable'))

        self.assertRaises(
            RuntimeError, self.compute._delete_instance,
            mock.sentinel.context, self._idmap_instance(),
            mock.sentinel.bdms)

        base_delete.assert_not_called()

    @mock.patch.object(manager.manager.ComputeManager, '_delete_instance')
    def test_final_delete_changed_materialization_never_retires(
            self, base_delete):
        wrong = self._host_claim(
            materialization_id='50000000-0000-0000-0000-000000000005')
        allocator = self.compute.driver.idmap_allocator
        allocator.get_host_claim.side_effect = [self.idmap_claim, wrong]
        base_delete.return_value = mock.sentinel.result

        result = self.compute._delete_instance(
            mock.sentinel.context, self._idmap_instance(),
            mock.sentinel.bdms)

        self.assertIs(mock.sentinel.result, result)
        allocator.request_release.assert_called_once()
        allocator.retire_claim.assert_not_called()
        allocator.release.assert_not_called()
        base_delete.assert_called_once()

    @mock.patch.object(manager.manager.ComputeManager, '_delete_instance')
    def test_final_delete_fails_closed_when_metadata_has_no_registry_record(
            self, base_delete):
        instance = self._idmap_instance()
        self.compute.driver.idmap_allocator.get.return_value = None

        self.assertRaises(
            manager.incus_driver.incus_idmap.IDMapIntegrityError,
            self.compute._delete_instance,
            mock.sentinel.context, instance, mock.sentinel.bdms)

        base_delete.assert_not_called()

    @mock.patch.object(manager.manager.ComputeManager, '_delete_instance',
                       return_value=mock.sentinel.result)
    def test_final_delete_without_local_claim_waits_for_remote_claim(
            self, base_delete):
        instance = self._idmap_instance()
        remote = '40000000-0000-0000-0000-000000000004'
        assignment = self._assignment(host_ids=(remote,))
        allocator = self.compute.driver.idmap_allocator
        allocator.get.return_value = assignment
        allocator.get_host_claim.return_value = None

        result = self.compute._delete_instance(
            mock.sentinel.context, instance, mock.sentinel.bdms)

        self.assertIs(mock.sentinel.result, result)
        allocator.request_release.assert_called_once_with(
            instance.uuid, instance.name, assignment=assignment)
        allocator.retire_claim.assert_not_called()
        allocator.release.assert_not_called()
        base_delete.assert_called_once_with(
            mock.sentinel.context, instance, mock.sentinel.bdms)

    @mock.patch.object(manager.manager.ComputeManager, '_delete_instance',
                       return_value=mock.sentinel.result)
    def test_final_delete_releases_empty_never_materialized_generation(
            self, base_delete):
        instance = self._idmap_instance()
        empty = self._assignment(host_ids=())
        allocator = self.compute.driver.idmap_allocator
        allocator.get.return_value = empty
        allocator.get_host_claim.return_value = None

        with mock.patch.object(
                self.compute, '_local_idmap_resources_absent',
                return_value=True) as exact_absence:
            result = self.compute._delete_instance(
                mock.sentinel.context, instance, mock.sentinel.bdms)

        self.assertIs(mock.sentinel.result, result)
        allocator.claim.assert_not_called()
        allocator.request_release.assert_called_once_with(
            instance.uuid, instance.name,
            assignment=empty)
        allocator.retire_claim.assert_not_called()
        allocator.release.assert_called_once_with(self.idmap_intent)
        exact_absence.assert_called_once_with(self.idmap_intent)
        base_delete.assert_called_once_with(
            mock.sentinel.context, instance, mock.sentinel.bdms)

    @mock.patch.object(manager.manager.ComputeManager, '_delete_instance')
    def test_final_delete_fails_closed_without_node_uuid(self, base_delete):
        instance = self._idmap_instance()
        manager.virt_node.read_local_node_uuid.return_value = None
        self.assertRaises(
            manager.incus_driver.incus_idmap.IDMapIntegrityError,
            self.compute._delete_instance, mock.sentinel.context,
            instance, mock.sentinel.bdms)

        base_delete.assert_not_called()

    @mock.patch.object(manager.manager.ComputeManager, '_delete_instance',
                       return_value=mock.sentinel.result)
    def test_final_delete_retains_claim_while_local_resource_exists(
            self, base_delete):
        self.compute.driver.client.instances.get.side_effect = None
        self.compute.driver.client.instances.get.return_value = mock.Mock()

        result = self.compute._delete_instance(
            mock.sentinel.context, self._idmap_instance(),
            mock.sentinel.bdms)

        self.assertIs(mock.sentinel.result, result)
        self.compute.driver.idmap_allocator.retire_claim.assert_not_called()
        self.compute.driver.idmap_allocator.release.assert_not_called()

    def test_local_resource_paths_each_prevent_claim_retirement(self):
        paths = (
            os.path.join(self.state_path, self.idmap_intent.instance_name),
            *self.compute._idmap_host_journal_paths(
                self.idmap_intent.instance_uuid),
        )
        for path in paths:
            os.makedirs(path)
            self.assertFalse(
                self.compute._local_idmap_resources_absent(
                    self.idmap_intent), path)
            os.rmdir(path)

    def test_local_profile_prevents_claim_retirement(self):
        self.compute.driver.client.profiles.get.side_effect = None
        self.compute.driver.client.profiles.get.return_value = mock.Mock()

        self.assertFalse(self.compute._local_idmap_resources_absent(
            self.idmap_intent))

    def test_foreign_same_generation_profile_prevents_claim_retirement(self):
        profiles_response = (
            self.compute.driver.client.api.profiles.get.return_value)
        profiles_response.json.return_value = {'metadata': [{
            'name': 'foreign-profile',
            'project': 'foreign-project',
            'config': {
                'security.idmap.base': str(self.idmap_intent.base),
                'security.idmap.size': str(self.idmap_intent.size),
            },
        }]}

        self.assertFalse(self.compute._local_idmap_resources_absent(
            self.idmap_intent))
        self.compute.driver.idmap_allocator.retire_claim.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_replay_batch_screens_against_one_shared_listing(
            self, get_by_uuid):
        """A batch pays for one all-project listing, not one per candidate."""
        get_by_uuid.side_effect = manager.exception.InstanceNotFound(
            instance_id=self.idmap_intent.instance_uuid)
        intents = [self.idmap_intent] * 4
        allocator = self.compute.driver.idmap_allocator
        allocator.list_host_claims.return_value = [
            mock.Mock(instance_uuid=str(index)) for index in range(4)]
        allocator.list_release_intents_for_instances.return_value = intents
        instances_get = (
            self.compute.driver.inventory_client.api.instances.get)
        instances_get.reset_mock()

        self.compute._replay_incus_idmap_releases(
            context.get_admin_context())

        # One screening listing for the whole batch, plus the exact proof
        # each candidate repeats immediately before it releases.
        self.assertEqual(1 + len(intents), instances_get.call_count)

    def test_indexed_release_replay_skips_all_project_listing(self):
        self.compute.driver.inventory_client.has_api_extension.return_value = (
            True)
        replay = mock.Mock()
        with mock.patch.object(
                self.compute, '_all_project_idmap_inventory') as inventory, \
                mock.patch.object(
                    self.compute, '_replay_incus_idmap_release', replay):
            self.compute._replay_incus_idmap_releases(
                context.get_admin_context())

        inventory.assert_not_called()
        replay.assert_called_once_with(
            mock.ANY, self.compute.driver.idmap_allocator,
            self.idmap_intent, self.host_id, inventory=None)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_screening_listing_failure_never_releases(self, get_by_uuid):
        """A failed screen must fall back to exact proofs, not to yes."""
        get_by_uuid.side_effect = manager.exception.InstanceNotFound(
            instance_id=self.idmap_intent.instance_uuid)
        instances_get = (
            self.compute.driver.inventory_client.api.instances.get)
        real_response = instances_get.return_value
        instances_get.side_effect = [Exception('etcd of incus is down')] + (
            [real_response] * 8)

        self.compute._replay_incus_idmap_releases(
            context.get_admin_context())

        # The candidate still released, but only because it proved absence
        # itself after the screen was unavailable.
        allocator = self.compute.driver.idmap_allocator
        allocator.release.assert_called_once_with(self.idmap_intent)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_screened_presence_blocks_without_an_exact_listing(
            self, get_by_uuid):
        """A snapshot match is authoritative; it cannot have appeared."""
        get_by_uuid.side_effect = manager.exception.InstanceNotFound(
            instance_id=self.idmap_intent.instance_uuid)
        candidates = (
            self.compute.driver.idmap_allocator.
            list_release_intents_for_instances)
        self.compute.driver.idmap_allocator.list_host_claims.return_value = [
            mock.Mock(instance_uuid=str(index)) for index in range(4)]
        candidates.return_value = [self.idmap_intent] * 4
        instances_response = (
            self.compute.driver.inventory_client.api.instances.get.
            return_value)
        instances_response.json.return_value = {'metadata': [{
            'name': 'foreign-instance',
            'project': 'foreign-project',
            'config': {
                'security.idmap.base': str(self.idmap_intent.base),
                'security.idmap.size': str(self.idmap_intent.size),
            },
        }]}
        instances_get = (
            self.compute.driver.inventory_client.api.instances.get)
        instances_get.reset_mock()

        self.compute._replay_incus_idmap_releases(
            context.get_admin_context())

        allocator = self.compute.driver.idmap_allocator
        allocator.release.assert_not_called()
        allocator.retire_claim.assert_not_called()
        self.assertEqual(1, instances_get.call_count)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_unclaimed_intent_still_proves_absence_exactly(
            self, get_by_uuid):
        """The full-audit coordinator owns intents with no host index."""
        get_by_uuid.side_effect = manager.exception.InstanceNotFound(
            instance_id=self.idmap_intent.instance_uuid)
        allocator = self.compute.driver.idmap_allocator
        unclaimed = self._assignment(host_ids=())
        allocator.get.return_value = unclaimed
        absent = mock.Mock(return_value=True)
        with mock.patch.object(
                self.compute, '_local_idmap_resources_absent', absent):
            self.compute._replay_unclaimed_incus_idmap_releases(
                context.get_admin_context(), allocator, [unclaimed],
                [self.idmap_intent])

        allocator.retire_claim.assert_not_called()
        allocator.release.assert_called_once_with(self.idmap_intent)
        # One exact proof remains after the batch inventory screen.
        self.assertEqual(1, absent.call_count)
        self.assertEqual({}, absent.call_args.kwargs)

    def _force_full_idmap_audit(self):
        """Make the next audit cycle owe its complete scan."""
        self.compute._incus_full_audit_deadline = time.monotonic() - 1

    def test_release_and_claim_lock_names_are_the_same_lock(self):
        """The aliasing is deliberate; the recursion hazard follows from it."""
        self.assertEqual(
            manager._idmap_release_lock_name('a-uuid'),
            manager.incus_driver._idmap_host_claim_lock_name('a-uuid'))

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_replay_does_not_retake_the_lock_it_already_holds(
            self, get_by_uuid):
        """Re-entering oslo's non-reentrant lock wedges every periodic.

        _replay_incus_idmap_release runs inside the release lock, which is
        the host claim lock under another name. If the promotion helper is
        allowed to acquire it again the green thread blocks on itself, and
        because oslo runs all of a service's periodics in one green thread
        that stops every periodic on the compute until it restarts.
        """
        # A deleted Nova row is what lets the replay reach the promotion
        # helper; InstanceNotFound short-circuits before it.
        deleted = self._idmap_instance()
        deleted.deleted = True
        deleted.name = self.idmap_intent.instance_name
        get_by_uuid.return_value = deleted
        allocator = self.compute.driver.idmap_allocator
        possible = self._host_claim(state='possible', proof=None)
        allocator.get_host_claim.return_value = possible
        promote = self.compute.driver._promote_idmap_claim_if_server_committed
        promote.return_value = (self.idmap_assignment, self.idmap_claim)

        held = []

        real_lock = manager.lockutils.lock

        @contextlib.contextmanager
        def tracking_lock(name, *args, **kwargs):
            if name in held:
                raise AssertionError(
                    'lock {!r} was acquired while already held; oslo locks '
                    'are not reentrant and this deadlocks'.format(name))
            held.append(name)
            try:
                with real_lock(name, *args, **kwargs) as result:
                    yield result
            finally:
                held.remove(name)

        with mock.patch.object(manager.lockutils, 'lock', tracking_lock):
            self.compute._replay_incus_idmap_releases(
                context.get_admin_context())

        # It must have been told the lock is already held, not simply have
        # skipped the promotion.
        self.assertTrue(promote.called)
        for call in promote.call_args_list:
            self.assertTrue(
                call.kwargs.get('_claim_lock_held'),
                'promotion helper was called without _claim_lock_held while '
                'the caller holds that lock')

    def test_idmap_full_audit_is_skipped_without_allocator(self):
        self.compute.driver.idmap_allocator = None

        self.compute._audit_incus_idmap_allocator(mock.sentinel.context)

    def test_idmap_full_audit_runs_against_allocator(self):
        allocator = self.compute.driver.idmap_allocator
        allocator.run_coordinated_audit.return_value = (
            True, ([self.idmap_assignment], [], []))
        self._force_full_idmap_audit()

        self.compute._audit_incus_idmap_allocator(mock.sentinel.context)

        allocator.run_coordinated_audit.assert_called_once_with(full=True)

    @mock.patch.object(manager.LOG, 'critical')
    def test_idmap_full_audit_integrity_failure_is_critical(self, critical):
        allocator = self.compute.driver.idmap_allocator
        allocator.run_coordinated_audit.side_effect = (
            manager.incus_driver.incus_idmap.IDMapIntegrityError(
                reason='corrupt reverse index'))
        self._force_full_idmap_audit()

        self.compute._audit_incus_idmap_allocator(mock.sentinel.context)

        critical.assert_called_once()

    @mock.patch.object(manager.LOG, 'warning')
    def test_idmap_full_audit_backend_failure_is_transient(self, warning):
        allocator = self.compute.driver.idmap_allocator
        allocator.run_coordinated_audit.side_effect = (
            manager.incus_driver.incus_idmap.IDMapBackendError(
                reason='etcd unavailable'))
        self._force_full_idmap_audit()

        self.compute._audit_incus_idmap_allocator(mock.sentinel.context)

        warning.assert_called_once()

    def test_idle_audit_cycles_only_probe(self):
        """The steady-state cost must not grow with the registry."""
        allocator = self.compute.driver.idmap_allocator

        for _ in range(5):
            self.compute._audit_incus_idmap_allocator(mock.sentinel.context)

        self.assertEqual(5, allocator.run_coordinated_audit.call_count)
        self.assertEqual(
            [mock.call(full=False)] * 5,
            allocator.run_coordinated_audit.call_args_list)

    def test_first_full_audit_deadline_is_jittered(self):
        """A fleet restarted together must not synchronize its scans."""
        interval = (
            manager.CONF.incus.idmap_allocator_full_audit_interval)
        deadlines = set()
        for _ in range(20):
            self.compute._incus_full_audit_deadline = None
            self.compute._incus_full_idmap_audit_due()
            deadlines.add(self.compute._incus_full_audit_deadline)
            self.assertLessEqual(
                self.compute._incus_full_audit_deadline,
                time.monotonic() + interval)
        self.assertGreater(len(deadlines), 1)

    def test_full_audit_runs_once_its_deadline_passes(self):
        allocator = self.compute.driver.idmap_allocator
        allocator.run_coordinated_audit.side_effect = [
            (True, None), (True, ([], [], [])), (True, None)]

        self.compute._audit_incus_idmap_allocator(mock.sentinel.context)

        self._force_full_idmap_audit()
        self.compute._audit_incus_idmap_allocator(mock.sentinel.context)

        # The scan reschedules itself rather than repeating every cycle.
        self.compute._audit_incus_idmap_allocator(mock.sentinel.context)
        self.assertEqual([
            mock.call(full=False),
            mock.call(full=True),
            mock.call(full=False),
        ], allocator.run_coordinated_audit.call_args_list)

    def test_full_audit_follower_does_not_scan_registry(self):
        allocator = self.compute.driver.idmap_allocator
        allocator.run_coordinated_audit.return_value = (False, None)
        self._force_full_idmap_audit()

        self.compute._audit_incus_idmap_allocator(mock.sentinel.context)

        allocator.run_coordinated_audit.assert_called_once_with(full=True)

    def test_audit_coordination_does_not_query_nova_service_inventory(self):
        allocator = self.compute.driver.idmap_allocator
        with mock.patch.object(
                manager.objects.ServiceList,
                'get_all_computes_by_hv_type') as get_services:
            self.compute._audit_incus_idmap_allocator(
                mock.sentinel.context)

        get_services.assert_not_called()
        allocator.run_coordinated_audit.assert_called_once_with(full=False)

    @mock.patch.object(manager.LOG, 'warning')
    def test_probe_backend_failure_does_not_escalate(self, warning):
        """A transport outage is not evidence of corruption."""
        allocator = self.compute.driver.idmap_allocator
        allocator.run_coordinated_audit.side_effect = (
            manager.incus_driver.incus_idmap.IDMapBackendError(
                reason='etcd unavailable'))

        self.compute._audit_incus_idmap_allocator(mock.sentinel.context)

        warning.assert_called_once()

    def test_unavailable_full_audit_retries_on_the_next_cycle(self):
        """A missed scan must not wait out the whole interval."""
        allocator = self.compute.driver.idmap_allocator
        allocator.run_coordinated_audit.side_effect = (
            manager.incus_driver.incus_idmap.IDMapBackendError(
                reason='etcd unavailable'))
        self._force_full_idmap_audit()

        self.compute._audit_incus_idmap_allocator(mock.sentinel.context)
        self.compute._audit_incus_idmap_allocator(mock.sentinel.context)

        self.assertEqual(2, allocator.run_coordinated_audit.call_count)
        self.assertEqual([
            mock.call(full=True), mock.call(full=True)],
            allocator.run_coordinated_audit.call_args_list)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_idmap_release_replay_retires_local_claim_after_absence(
            self, get_by_uuid):
        get_by_uuid.side_effect = manager.exception.InstanceNotFound(
            instance_id=self.idmap_intent.instance_uuid)

        self.compute._replay_incus_idmap_releases(
            context.get_admin_context())

        allocator = self.compute.driver.idmap_allocator
        allocator.retire_claim.assert_called_once_with(
            self.idmap_intent.instance_uuid, self.host_id,
            self.materialization_id,
            assignment=self.idmap_assignment)
        allocator.release.assert_called_once_with(self.idmap_intent)
        self.assertEqual('yes', get_by_uuid.call_args.args[0].read_deleted)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_replay_settles_exact_claim_before_retire(
            self, get_by_uuid):
        get_by_uuid.side_effect = manager.exception.InstanceNotFound(
            instance_id=self.idmap_intent.instance_uuid)
        assignment = self.idmap_assignment
        retired = self._assignment(host_ids=())
        allocator = self.compute.driver.idmap_allocator
        allocator.get.return_value = assignment
        allocator.retire_claim.return_value = retired
        calls = mock.Mock()
        calls.attach_mock(
            self.compute.driver._settle_idmap_host_claim, 'settle')
        calls.attach_mock(allocator.retire_claim, 'retire')
        calls.attach_mock(allocator.release, 'release')

        self.compute._replay_incus_idmap_releases(
            context.get_admin_context())

        self.assertEqual([
            mock.call.settle(None, self.idmap_claim, final_delete=True),
            mock.call.retire(
                self.idmap_intent.instance_uuid, self.host_id,
                self.materialization_id,
                assignment=assignment),
            mock.call.release(self.idmap_intent),
        ], calls.mock_calls)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_replay_ack_failure_retains_claim(
            self, get_by_uuid):
        get_by_uuid.side_effect = manager.exception.InstanceNotFound(
            instance_id=self.idmap_intent.instance_uuid)
        assignment = self.idmap_assignment
        allocator = self.compute.driver.idmap_allocator
        allocator.get.return_value = assignment
        settle = self.compute.driver._settle_idmap_host_claim
        settle.side_effect = RuntimeError('ACK failed')

        self.compute._replay_incus_idmap_releases(
            context.get_admin_context())

        allocator.retire_claim.assert_not_called()
        allocator.release.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_replay_unproven_claim_never_retires(
            self, get_by_uuid):
        get_by_uuid.side_effect = manager.exception.InstanceNotFound(
            instance_id=self.idmap_intent.instance_uuid)
        possible = self._host_claim(state='possible', proof=None)
        allocator = self.compute.driver.idmap_allocator
        allocator.get_host_claim.return_value = possible
        self.compute.driver._settle_idmap_host_claim.return_value = possible

        self.compute._replay_incus_idmap_releases(
            context.get_admin_context())

        allocator.retire_claim.assert_not_called()
        allocator.release.assert_not_called()
        self.compute.driver._promote_idmap_claim_if_server_committed.\
            assert_not_called()
        self.compute.driver._settle_idmap_host_claim.assert_called_once_with(
            None, possible, final_delete=False)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_idmap_release_replay_keeps_intent_for_live_nova_instance(
            self, get_by_uuid):
        live = mock.Mock(deleted=False)
        live.obj_attr_is_set.return_value = True
        get_by_uuid.return_value = live

        self.compute._replay_incus_idmap_releases(
            context.get_admin_context())

        allocator = self.compute.driver.idmap_allocator
        allocator.retire_claim.assert_not_called()
        allocator.release.assert_not_called()
        self.compute.driver.client.instances.get.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_idmap_release_replay_accepts_deleted_exact_nova_row(
            self, get_by_uuid):
        deleted = self._idmap_instance()
        deleted.deleted = True
        deleted.obj_attr_is_set.return_value = True
        get_by_uuid.return_value = deleted

        self.compute._replay_incus_idmap_releases(
            context.get_admin_context())

        allocator = self.compute.driver.idmap_allocator
        allocator.retire_claim.assert_called_once()
        allocator.release.assert_called_once_with(self.idmap_intent)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_idmap_release_replay_keeps_intent_when_deleted_is_unset(
            self, get_by_uuid):
        uncertain = mock.Mock()
        uncertain.obj_attr_is_set.return_value = False
        get_by_uuid.return_value = uncertain

        self.compute._replay_incus_idmap_releases(
            context.get_admin_context())

        allocator = self.compute.driver.idmap_allocator
        allocator.retire_claim.assert_not_called()
        allocator.release.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_idmap_release_replay_keeps_intent_on_unknown_nova_state(
            self, get_by_uuid):
        get_by_uuid.side_effect = RuntimeError('database unavailable')

        self.compute._replay_incus_idmap_releases(
            context.get_admin_context())

        allocator = self.compute.driver.idmap_allocator
        allocator.retire_claim.assert_not_called()
        allocator.release.assert_not_called()

    def test_idmap_release_replay_skips_claim_owned_by_another_host(self):
        assignment = self._assignment(host_ids=(
            '40000000-0000-0000-0000-000000000004',))
        self.compute.driver.idmap_allocator.get.return_value = assignment
        self.compute.driver.idmap_allocator.get_host_claim.return_value = None
        self.compute.driver.idmap_allocator.list_host_claims.return_value = []

        with mock.patch.object(
                manager.objects.Instance, 'get_by_uuid') as get_by_uuid:
            self.compute._replay_incus_idmap_releases(
                context.get_admin_context())

        get_by_uuid.assert_not_called()
        self.compute.driver.client.instances.get.assert_not_called()
        self.compute.driver.idmap_allocator.retire_claim.assert_not_called()
        self.compute.driver.idmap_allocator.release.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_idmap_release_replay_finishes_already_unclaimed_intent(
            self, get_by_uuid):
        self.compute.driver.idmap_allocator.get.return_value = (
            self._assignment(host_ids=()))
        self.compute.driver.idmap_allocator.get_host_claim.return_value = None
        get_by_uuid.side_effect = manager.exception.InstanceNotFound(
            instance_id=self.idmap_intent.instance_uuid)

        unclaimed = self._assignment(host_ids=())
        self.compute._replay_unclaimed_incus_idmap_releases(
            context.get_admin_context(), self.compute.driver.idmap_allocator,
            [unclaimed], [self.idmap_intent])

        self.compute.driver.idmap_allocator.retire_claim.assert_not_called()
        self.compute.driver.idmap_allocator.release.assert_called_once_with(
            self.idmap_intent)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_idmap_release_replay_waits_for_other_claims_after_retire(
            self, get_by_uuid):
        get_by_uuid.side_effect = manager.exception.InstanceNotFound(
            instance_id=self.idmap_intent.instance_uuid)
        self.compute.driver.idmap_allocator.retire_claim.return_value = (
            self._assignment(host_ids=(
                '40000000-0000-0000-0000-000000000004',)))

        self.compute._replay_incus_idmap_releases(
            context.get_admin_context())

        self.compute.driver.idmap_allocator.retire_claim.assert_called_once()
        self.compute.driver.idmap_allocator.release.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_idmap_release_replay_retains_mismatched_generation(
            self, get_by_uuid):
        self.compute.driver.idmap_allocator.get.return_value = (
            self._assignment(base=500065536))

        self.compute._replay_incus_idmap_releases(
            context.get_admin_context())

        get_by_uuid.assert_not_called()
        self.compute.driver.idmap_allocator.retire_claim.assert_not_called()
        self.compute.driver.idmap_allocator.release.assert_not_called()

    def test_idmap_release_replay_rotates_batch_cursor(self):
        intents = [mock.Mock(instance_uuid=str(index)) for index in range(150)]
        claims = [mock.Mock(instance_uuid=str(index)) for index in range(150)]
        allocator = self.compute.driver.idmap_allocator
        allocator.list_host_claims.return_value = claims
        allocator.list_release_intents_for_instances.side_effect = [
            intents[:100], intents[100:] + intents[:50]]
        self.compute._replay_incus_idmap_release = mock.Mock()

        self.compute._replay_incus_idmap_releases(
            context.get_admin_context())
        first = [call.args[2] for call in
                 self.compute._replay_incus_idmap_release.call_args_list]
        self.compute._replay_incus_idmap_release.reset_mock()
        self.compute._replay_incus_idmap_releases(
            context.get_admin_context())
        second = [call.args[2] for call in
                  self.compute._replay_incus_idmap_release.call_args_list]

        self.assertEqual(intents[:100], first)
        self.assertEqual(intents[100:] + intents[:50], second)

    def test_idmap_release_replay_fails_closed_without_node_uuid(self):
        manager.virt_node.read_local_node_uuid.return_value = None

        self.compute._replay_incus_idmap_releases(
            context.get_admin_context())

        list_claims = (
            self.compute.driver.idmap_allocator.
            list_host_claims)
        list_claims.assert_not_called()
        self.compute.driver.idmap_allocator.retire_claim.assert_not_called()
        self.compute.driver.idmap_allocator.release.assert_not_called()

    def test_active_host_claim_batch_avoids_incus_inventory(self):
        claims = [
            mock.Mock(instance_uuid='00000000-0000-0000-0000-%012d' % index)
            for index in range(1000)
        ]
        instances = []
        for claim in claims:
            instance = mock.Mock(uuid=claim.instance_uuid, deleted=False)
            instance.obj_attr_is_set.return_value = True
            instances.append(instance)
        allocator = self.compute.driver.idmap_allocator
        allocator.list_host_claims.return_value = claims
        self.get_instances_by_host.return_value = instances
        self.compute._all_project_idmap_inventory = mock.Mock()
        self.compute._reconcile_incus_idmap_host_claim = mock.Mock()

        self.compute._reconcile_incus_idmap_host_claims(
            context.get_admin_context())

        self.compute._all_project_idmap_inventory.assert_not_called()
        self.compute._reconcile_incus_idmap_host_claim.assert_not_called()
        allocator.get.assert_not_called()
        self.get_instances_by_host.assert_called_once()

    def test_host_claim_batch_filters_active_before_truncating(self):
        active_claims = [
            mock.Mock(instance_uuid='active-{}'.format(index))
            for index in range(1000)
        ]
        stale_claim = mock.Mock(instance_uuid='stale')
        instances = []
        for claim in active_claims:
            instance = mock.Mock(uuid=claim.instance_uuid, deleted=False)
            instance.obj_attr_is_set.return_value = True
            instances.append(instance)
        allocator = self.compute.driver.idmap_allocator
        allocator.list_host_claims.return_value = active_claims + [stale_claim]
        self.get_instances_by_host.return_value = instances
        self.compute.driver.inventory_client.has_api_extension.return_value = (
            True)
        self.compute._all_project_idmap_inventory = mock.Mock()
        reconcile = mock.Mock()
        self.compute._reconcile_incus_idmap_host_claim = reconcile

        self.compute._reconcile_incus_idmap_host_claims(
            context.get_admin_context())

        allocator.list_host_claims.assert_called_once_with(self.host_id)
        self.get_instances_by_host.assert_called_once()
        self.compute._all_project_idmap_inventory.assert_not_called()
        reconcile.assert_called_once_with(
            mock.ANY, allocator, stale_claim, self.host_id, inventory=None)

    def test_host_claim_batch_processes_one_hundred_stale_candidates(self):
        active_claims = [
            mock.Mock(instance_uuid='active-{}'.format(index))
            for index in range(100)
        ]
        stale_claims = [
            mock.Mock(instance_uuid='stale-{}'.format(index))
            for index in range(100)
        ]
        instances = []
        for claim in active_claims:
            instance = mock.Mock(uuid=claim.instance_uuid, deleted=False)
            instance.obj_attr_is_set.return_value = True
            instances.append(instance)
        allocator = self.compute.driver.idmap_allocator
        allocator.list_host_claims.return_value = active_claims + stale_claims
        self.get_instances_by_host.return_value = instances
        self.compute.driver.inventory_client.has_api_extension.return_value = (
            True)
        reconcile = mock.Mock()
        self.compute._reconcile_incus_idmap_host_claim = reconcile

        self.compute._reconcile_incus_idmap_host_claims(
            context.get_admin_context())

        processed = [call.args[2] for call in reconcile.call_args_list]
        self.assertEqual(stale_claims, processed)
        self.assertEqual(100, len(processed))

    def test_host_claim_bulk_nova_failure_uses_exact_fallback(self):
        claim = self._host_claim()
        allocator = self.compute.driver.idmap_allocator
        allocator.list_host_claims.return_value = [claim]
        self.get_instances_by_host.side_effect = RuntimeError('database down')
        inventory = mock.sentinel.inventory
        self.compute._all_project_idmap_inventory = mock.Mock(
            return_value=inventory)
        self.compute._reconcile_incus_idmap_host_claim = mock.Mock()

        self.compute._reconcile_incus_idmap_host_claims(
            context.get_admin_context())

        self.compute._reconcile_incus_idmap_host_claim.assert_called_once_with(
            mock.ANY, allocator, claim, self.host_id, inventory=inventory)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_host_claim_reconcile_retires_clean_historical_host(
            self, get_by_uuid):
        claim = self._host_claim()
        allocator = self.compute.driver.idmap_allocator
        allocator.list_host_claims.return_value = [claim]
        instance = self._idmap_instance()
        instance.host = 'compute-2'
        instance.task_state = None
        instance.deleted = False
        get_by_uuid.return_value = instance

        self.compute._reconcile_incus_idmap_host_claims(
            context.get_admin_context())

        allocator.list_host_claims.assert_called_once_with(self.host_id)
        allocator.retire_claim.assert_called_once_with(
            instance.uuid, self.host_id, self.materialization_id,
            assignment=self.idmap_assignment)
        self.compute.driver._settle_idmap_host_claim.assert_called_once_with(
            instance, claim, final_delete=False)
        self.assertEqual(3, get_by_uuid.call_count)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_host_claim_reconcile_keeps_incus_io_outside_claim_lock(
            self, get_by_uuid):
        claim = self._host_claim()
        allocator = self.compute.driver.idmap_allocator
        instance = self._idmap_instance()
        instance.host = 'compute-2'
        instance.task_state = None
        instance.deleted = False
        get_by_uuid.return_value = instance
        lock_held = {'value': False}

        class TrackingLock:
            def __enter__(inner_self):
                self.assertFalse(lock_held['value'])
                lock_held['value'] = True

            def __exit__(inner_self, exc_type, exc_value, traceback):
                lock_held['value'] = False

        def local_resources_absent(*args, **kwargs):
            self.assertFalse(lock_held['value'])
            return True

        with mock.patch.object(
                self.compute, '_local_idmap_resources_absent_by_name',
                side_effect=local_resources_absent) as absent, \
                mock.patch.object(
                    manager.lockutils, 'lock', return_value=TrackingLock()):
            self.compute._reconcile_incus_idmap_host_claim(
                context.get_admin_context(), allocator, claim, self.host_id)

        self.assertEqual(2, absent.call_count)
        allocator.retire_claim.assert_called_once()
        self.assertFalse(lock_held['value'])

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_host_claim_reconcile_promotes_server_committed_possible(
            self, get_by_uuid):
        possible = self._host_claim(state='possible', proof=None)
        committed = self._host_claim(state='committed', proof=None)
        cleaned = self._host_claim(
            state='cleaned', proof=mock.sentinel.idmap_cleanup_proof)
        allocator = self.compute.driver.idmap_allocator
        allocator.list_host_claims.return_value = [possible]
        allocator.get_host_claim.side_effect = [possible, cleaned, cleaned]
        instance = self._idmap_instance()
        instance.host = 'compute-2'
        instance.task_state = None
        instance.deleted = False
        get_by_uuid.return_value = instance
        promote = (
            self.compute.driver._promote_idmap_claim_if_server_committed)
        promote.return_value = self.idmap_assignment, committed
        self.compute.driver._settle_idmap_host_claim.return_value = cleaned

        self.compute._reconcile_incus_idmap_host_claims(
            context.get_admin_context())

        promote.assert_called_once_with(instance, possible)
        self.compute.driver._settle_idmap_host_claim.assert_called_once_with(
            instance, committed, final_delete=True)
        allocator.retire_claim.assert_called_once_with(
            instance.uuid, self.host_id, self.materialization_id,
            assignment=self.idmap_assignment)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_host_claim_reconcile_aborts_uncommitted_possible_nonfinal(
            self, get_by_uuid):
        possible = self._host_claim(state='possible', proof=None)
        cleaned = self._host_claim(
            state='cleaned', proof=mock.sentinel.idmap_cleanup_proof)
        allocator = self.compute.driver.idmap_allocator
        allocator.list_host_claims.return_value = [possible]
        allocator.get_host_claim.side_effect = [possible, cleaned, cleaned]
        instance = self._idmap_instance()
        instance.host = 'compute-2'
        instance.task_state = None
        instance.deleted = False
        get_by_uuid.return_value = instance
        promote = (
            self.compute.driver._promote_idmap_claim_if_server_committed)
        promote.return_value = self.idmap_assignment, possible
        self.compute.driver._settle_idmap_host_claim.return_value = cleaned

        self.compute._reconcile_incus_idmap_host_claims(
            context.get_admin_context())

        self.compute.driver._settle_idmap_host_claim.assert_called_once_with(
            instance, possible, final_delete=False)
        allocator.retire_claim.assert_called_once()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_host_claim_reconcile_keeps_inflight_destination(
            self, get_by_uuid):
        claim = self._host_claim()
        allocator = self.compute.driver.idmap_allocator
        allocator.list_host_claims.return_value = [claim]
        instance = self._idmap_instance()
        instance.host = 'compute-2'
        instance.task_state = task_states.MIGRATING
        instance.deleted = False
        get_by_uuid.return_value = instance

        self.compute._reconcile_incus_idmap_host_claims(
            context.get_admin_context())

        allocator.retire_claim.assert_not_called()
        self.compute.driver.client.instances.get.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_reconcile_race_then_destination_claim_preserves_host(
            self, get_by_uuid):
        assignment = manager.incus_driver.incus_idmap.IDMapAssignment(
            instance_uuid=self.idmap_assignment.instance_uuid,
            base=self.idmap_assignment.base,
            size=self.idmap_assignment.size,
            slot=self.idmap_assignment.slot,
            allocation_id=self.idmap_assignment.allocation_id,
            fingerprint=self.idmap_assignment.fingerprint,
            host_ids=(self.host_id,))
        claim = manager.incus_driver.incus_idmap.IDMapHostClaim(
            host_id=self.host_id,
            materialization_id=self.materialization_id,
            instance_uuid=assignment.instance_uuid,
            base=assignment.base,
            size=assignment.size,
            slot=assignment.slot,
            allocation_id=assignment.allocation_id,
            fingerprint=assignment.fingerprint,
            state='cleaned',
            proof=mock.sentinel.idmap_cleanup_proof)
        instance = self._idmap_instance()
        instance.host = 'compute-2'
        instance.task_state = None
        instance.deleted = False
        get_by_uuid.return_value = instance

        state = {'assignment': assignment}
        retired = threading.Event()
        allocator = mock.Mock()

        def get_assignment(instance_uuid):
            return state['assignment']

        def get_host_claim(instance_uuid, host_id):
            if host_id in state['assignment'].host_ids:
                return claim
            return None

        def retire_claim(
                instance_uuid, host_id, materialization_id, assignment):
            self.assertEqual(self.materialization_id, materialization_id)
            state['assignment'] = dataclasses.replace(
                state['assignment'], host_ids=())
            retired.set()
            return state['assignment']

        def claim_host(instance_uuid, host_id, *args, **kwargs):
            state['assignment'] = dataclasses.replace(
                state['assignment'], host_ids=(host_id,))
            return state['assignment']

        allocator.get.side_effect = get_assignment
        allocator.get_host_claim.side_effect = get_host_claim
        allocator.retire_claim.side_effect = retire_claim
        allocator.claim.side_effect = claim_host
        runtime_driver = manager.incus_driver.IncusDriver(None)
        runtime_driver.idmap_allocator = allocator
        runtime_driver.client = self.compute.driver.client
        runtime_driver.inventory_client = (
            self.compute.driver.inventory_client)
        runtime_driver._settle_idmap_host_claim = mock.Mock(
            return_value=claim)
        self.compute.driver = runtime_driver
        failures = []

        def reconcile():
            try:
                self.compute._reconcile_incus_idmap_host_claim(
                    context.get_admin_context(), allocator, claim,
                    self.host_id)
            except Exception as exc:
                failures.append(exc)

        def ensure_claim():
            try:
                self.assertTrue(retired.wait(5))
                with manager.lockutils.lock(
                        manager._idmap_release_lock_name(instance.uuid),
                        external=True,
                        lock_path=manager._idmap_release_lock_path()):
                    current = allocator.get(instance.uuid)
                    allocator.claim(
                        instance.uuid, self.host_id,
                        '40000000-0000-0000-0000-000000000004',
                        assignment=current)
            except Exception as exc:
                failures.append(exc)

        destination_thread = threading.Thread(
            target=ensure_claim, name='destination-claim')
        destination_thread.start()
        reconcile_thread = threading.Thread(target=reconcile)
        reconcile_thread.start()
        self.assertTrue(retired.wait(5))
        reconcile_thread.join(5)
        destination_thread.join(5)

        self.assertFalse(reconcile_thread.is_alive())
        self.assertFalse(destination_thread.is_alive())
        self.assertEqual([], failures)
        self.assertEqual((self.host_id,), state['assignment'].host_ids)
        allocator.retire_claim.assert_called_once()
        allocator.claim.assert_called_once()

    def test_host_claim_reconcile_fails_closed_without_node_uuid(self):
        manager.virt_node.read_local_node_uuid.return_value = None

        self.compute._reconcile_incus_idmap_host_claims(
            context.get_admin_context())

        list_claims = self.compute.driver.idmap_allocator.list_host_claims
        list_claims.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_terminal_failed_build_queues_release_before_claim_retirement(
            self, get_by_uuid):
        proof = mock.Mock(instance_name='instance-00000001')
        cleaned = self._host_claim(proof=proof)
        instance = self._idmap_instance()
        instance.host = None
        instance.task_state = None
        instance.vm_state = manager.vm_states.ERROR
        instance.deleted = False
        get_by_uuid.return_value = instance
        allocator = self.compute.driver.idmap_allocator
        allocator.get_host_claim.return_value = cleaned
        self.compute.driver._settle_idmap_host_claim.return_value = cleaned

        self.compute._reconcile_incus_idmap_host_claim(
            context.get_admin_context(), allocator, cleaned, self.host_id)

        allocator.request_release.assert_called_once_with(
            instance.uuid, instance.name, assignment=self.idmap_assignment)
        allocator.retire_claim.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_terminal_reschedule_failure_fences_multiple_cleaned_hosts(
            self, get_by_uuid):
        remote_host = '40000000-0000-0000-0000-000000000004'
        assignment = self._assignment(
            host_ids=tuple(sorted((self.host_id, remote_host))))
        proof = mock.Mock(instance_name='instance-00000001')
        cleaned = self._host_claim(proof=proof)
        instance = self._idmap_instance()
        instance.host = None
        instance.task_state = None
        instance.vm_state = manager.vm_states.ERROR
        instance.deleted = False
        get_by_uuid.return_value = instance
        allocator = self.compute.driver.idmap_allocator
        allocator.get.return_value = assignment
        allocator.get_host_claim.return_value = cleaned
        allocator.request_release.return_value = self.idmap_intent
        self.compute.driver._settle_idmap_host_claim.return_value = cleaned

        self.compute._reconcile_incus_idmap_host_claim(
            context.get_admin_context(), allocator, cleaned, self.host_id)

        allocator.request_release.assert_called_once_with(
            instance.uuid, instance.name, assignment=assignment)
        allocator.retire_claim.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_failed_build_releases_a_claim_that_never_materialized(
            self, get_by_uuid):
        """A build failing before commit leaves 'possible' and no proof."""
        possible = self._host_claim(state='possible', proof=None)
        cleaned = self._host_claim(proof=mock.Mock(
            instance_name='instance-00000001'))
        instance = self._idmap_instance()
        instance.host = None
        instance.task_state = None
        instance.vm_state = manager.vm_states.ERROR
        instance.deleted = False
        get_by_uuid.return_value = instance
        allocator = self.compute.driver.idmap_allocator
        allocator.request_release.return_value = self.idmap_intent
        promote = self.compute.driver._promote_idmap_claim_if_server_committed
        # Incus reports the materialization never committed.
        promote.return_value = (self.idmap_assignment, possible)
        # The registry only shows the cleaned claim once settling wrote it.
        settled_claims = []
        allocator.get_host_claim.side_effect = (
            lambda *a, **kw: cleaned if settled_claims else possible)

        def settle(*args, **kwargs):
            settled_claims.append(cleaned)
            return cleaned

        self.compute.driver._settle_idmap_host_claim.side_effect = settle

        self.compute._reconcile_incus_idmap_host_claim(
            context.get_admin_context(), allocator, possible, self.host_id)

        # No rootfs was materialized, so it settles through the
        # materialization abort rather than a release receipt.
        self.compute.driver._settle_idmap_host_claim.assert_called_once_with(
            instance, possible, final_delete=False)
        allocator.request_release.assert_called_once_with(
            instance.uuid, instance.name, assignment=self.idmap_assignment)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_failed_build_abandons_an_unregistered_unmaterialized_claim(
            self, get_by_uuid):
        """A build that died before registering takes the non-final path."""
        unmaterialized = self._host_claim(state='unmaterialized', proof=None)
        empty_assignment = self._assignment(host_ids=())
        instance = self._idmap_instance()
        instance.host = None
        instance.task_state = None
        instance.vm_state = manager.vm_states.ERROR
        instance.deleted = False
        get_by_uuid.return_value = instance
        allocator = self.compute.driver.idmap_allocator
        allocator.request_release.return_value = self.idmap_intent
        abandoned = []
        allocator.get.side_effect = (
            lambda *a, **kw: (
                empty_assignment if abandoned else self.idmap_assignment))
        allocator.get_host_claim.side_effect = (
            lambda *a, **kw: None if abandoned else unmaterialized)

        def settle(*args, **kwargs):
            abandoned.append(True)
            return None

        self.compute.driver._settle_idmap_host_claim.side_effect = settle

        self.compute._reconcile_incus_idmap_host_claim(
            context.get_admin_context(), allocator, unmaterialized,
            self.host_id)

        # The local-delete leftover journal is consumed the way destroy
        # would have, before the absence proof runs.
        (self.compute.driver._remove_spawn_attempt_for_claim
            .assert_called_once_with(instance, unmaterialized))
        # Never registered: non-final settle, whose 404 branch abandons.
        self.compute.driver._settle_idmap_host_claim.assert_called_once_with(
            instance, unmaterialized, final_delete=False)
        allocator.request_release.assert_called_once_with(
            instance.uuid, instance.name, assignment=empty_assignment)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_failed_build_promoted_by_server_uses_the_release_receipt(
            self, get_by_uuid):
        """A create that did commit still owns storage to release."""
        possible = self._host_claim(state='possible', proof=None)
        committed = self._host_claim(state='committed', proof=None)
        cleaned = self._host_claim(proof=mock.Mock(
            instance_name='instance-00000001'))
        instance = self._idmap_instance()
        instance.host = None
        instance.task_state = None
        instance.vm_state = manager.vm_states.ERROR
        instance.deleted = False
        get_by_uuid.return_value = instance
        allocator = self.compute.driver.idmap_allocator
        allocator.request_release.return_value = self.idmap_intent
        promote = self.compute.driver._promote_idmap_claim_if_server_committed
        promote.return_value = (self.idmap_assignment, committed)
        settled_claims = []
        allocator.get_host_claim.side_effect = (
            lambda *a, **kw: cleaned if settled_claims else possible)

        def settle(*args, **kwargs):
            settled_claims.append(cleaned)
            return cleaned

        self.compute.driver._settle_idmap_host_claim.side_effect = settle

        self.compute._reconcile_incus_idmap_host_claim(
            context.get_admin_context(), allocator, possible, self.host_id)

        self.compute.driver._settle_idmap_host_claim.assert_called_once_with(
            instance, committed, final_delete=True)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_purged_failed_build_fences_from_cleaned_claim_proof(
            self, get_by_uuid):
        proof = mock.Mock(instance_name='instance-00000001')
        cleaned = self._host_claim(proof=proof)
        get_by_uuid.side_effect = manager.exception.InstanceNotFound(
            instance_id=self.idmap_assignment.instance_uuid)
        allocator = self.compute.driver.idmap_allocator
        allocator.get_host_claim.return_value = cleaned
        self.compute.driver._settle_idmap_host_claim.return_value = cleaned

        self.compute._reconcile_incus_idmap_host_claim(
            context.get_admin_context(), allocator, cleaned, self.host_id)

        allocator.request_release.assert_called_once_with(
            self.idmap_assignment.instance_uuid, 'instance-00000001',
            assignment=self.idmap_assignment)
        allocator.retire_claim.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_terminal_failed_build_without_generation_metadata_retains_claim(
            self, get_by_uuid):
        proof = mock.Mock(instance_name='instance-00000001')
        cleaned = self._host_claim(proof=proof)
        instance = self._idmap_instance()
        instance.system_metadata = {}
        instance.host = None
        instance.task_state = None
        instance.vm_state = manager.vm_states.ERROR
        instance.deleted = False
        get_by_uuid.return_value = instance
        allocator = self.compute.driver.idmap_allocator
        allocator.get_host_claim.return_value = cleaned

        self.compute._reconcile_incus_idmap_host_claim(
            context.get_admin_context(), allocator, cleaned, self.host_id)

        allocator.request_release.assert_not_called()
        allocator.retire_claim.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_deleted_failed_build_queues_release_before_claim_retirement(
            self, get_by_uuid):
        proof = mock.Mock(instance_name='instance-00000001')
        cleaned = self._host_claim(proof=proof)
        instance = self._idmap_instance()
        instance.host = None
        instance.task_state = None
        instance.vm_state = manager.vm_states.ERROR
        instance.deleted = True
        get_by_uuid.return_value = instance
        allocator = self.compute.driver.idmap_allocator
        allocator.get_host_claim.return_value = cleaned
        self.compute.driver._settle_idmap_host_claim.return_value = cleaned

        self.compute._reconcile_incus_idmap_host_claim(
            context.get_admin_context(), allocator, cleaned, self.host_id)

        allocator.request_release.assert_called_once_with(
            instance.uuid, instance.name, assignment=self.idmap_assignment)
        allocator.retire_claim.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_hostless_nonterminal_instance_retains_claim(self, get_by_uuid):
        instance = self._idmap_instance()
        instance.host = None
        instance.task_state = None
        instance.vm_state = manager.vm_states.ACTIVE
        instance.deleted = False
        get_by_uuid.return_value = instance
        allocator = self.compute.driver.idmap_allocator

        self.compute._reconcile_incus_idmap_host_claim(
            context.get_admin_context(), allocator, self.idmap_claim,
            self.host_id)

        allocator.request_release.assert_not_called()
        allocator.retire_claim.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_two_host_reschedule_then_final_delete_preserves_generation(
            self, get_by_uuid):
        destination_host = '40000000-0000-0000-0000-000000000004'
        destination_token = '50000000-0000-0000-0000-000000000005'
        source_claim = self._host_claim(
            proof=mock.Mock(instance_name='instance-00000001'))
        destination_claim = self._host_claim(
            host_id=destination_host,
            materialization_id=destination_token,
            state='committed', proof=None)
        state = {
            'assignment': self._assignment(
                host_ids=tuple(sorted((self.host_id, destination_host)))),
            'claims': {
                self.host_id: source_claim,
                destination_host: destination_claim,
            },
        }
        instance = self._idmap_instance()
        instance.host = 'compute-2'
        instance.task_state = task_states.SPAWNING
        instance.vm_state = manager.vm_states.BUILDING
        instance.deleted = False
        get_by_uuid.return_value = instance
        allocator = self.compute.driver.idmap_allocator

        def get_assignment(instance_uuid):
            self.assertEqual(instance.uuid, instance_uuid)
            return state['assignment']

        def get_claim(instance_uuid, host_id):
            self.assertEqual(instance.uuid, instance_uuid)
            return state['claims'].get(host_id)

        def retire_claim(
                instance_uuid, host_id, materialization_id,
                assignment=None):
            self.assertEqual(state['assignment'], assignment)
            claim = state['claims'].pop(host_id)
            self.assertEqual(claim.materialization_id, materialization_id)
            state['assignment'] = dataclasses.replace(
                state['assignment'], host_ids=tuple(
                    value for value in state['assignment'].host_ids
                    if value != host_id))
            return state['assignment']

        allocator.get.side_effect = get_assignment
        allocator.get_host_claim.side_effect = get_claim
        allocator.retire_claim.side_effect = retire_claim

        def settle(unused_instance, claim, **unused_kwargs):
            if claim.host_id != destination_host:
                return claim
            cleaned = dataclasses.replace(
                claim, state='cleaned',
                proof=mock.Mock(instance_name=instance.name))
            state['claims'][destination_host] = cleaned
            return cleaned

        self.compute.driver._settle_idmap_host_claim.side_effect = settle

        # The source claim remains a reschedule credential while destination
        # spawn is in flight.
        self.compute._reconcile_incus_idmap_host_claim(
            context.get_admin_context(), allocator, source_claim,
            self.host_id)
        allocator.retire_claim.assert_not_called()

        # Once the destination is authoritative, only the historical source
        # claim retires; the allocation generation remains unchanged.
        instance.task_state = None
        instance.vm_state = manager.vm_states.ACTIVE
        self.compute._reconcile_incus_idmap_host_claim(
            context.get_admin_context(), allocator, source_claim,
            self.host_id)
        self.assertEqual((destination_host,), state['assignment'].host_ids)
        self.assertEqual(
            self.idmap_assignment.allocation_id,
            state['assignment'].allocation_id)

        # Final delete on the destination creates the release fence before
        # retiring its last claim and releasing the shared slot.
        manager.virt_node.read_local_node_uuid.return_value = destination_host
        self.compute.host = 'compute-2'
        allocator.request_release.return_value = self.idmap_intent
        with mock.patch.object(
                manager.manager.ComputeManager, '_delete_instance',
                return_value=mock.sentinel.deleted) as base_delete:
            result = self.compute._delete_instance(
                context.get_admin_context(), instance, mock.sentinel.bdms)

        self.assertIs(mock.sentinel.deleted, result)
        base_delete.assert_called_once()
        allocator.request_release.assert_called_once_with(
            instance.uuid, instance.name,
            assignment=mock.ANY)
        self.assertEqual((), state['assignment'].host_ids)
        allocator.release.assert_called_once_with(self.idmap_intent)

    @mock.patch.object(manager.manager.ComputeManager, '_delete_instance')
    def test_stored_idmap_fails_closed_without_allocator(self, base_delete):
        self.compute.driver.idmap_allocator = None

        self.assertRaises(
            manager.incus_driver.incus_idmap.IDMapIntegrityError,
            self.compute._delete_instance, mock.sentinel.context,
            self._idmap_instance(), mock.sentinel.bdms)

        base_delete.assert_not_called()

    def test_live_migration_cleanup_flags_clean_target_keep_shared_disks(self):
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2')

        self.assertEqual(
            (True, False),
            self.compute._live_migration_cleanup_flags(data))

    @mock.patch.object(
        manager.manager.ComputeManager, '_live_migration_cleanup_flags',
        return_value=(mock.sentinel.cleanup, mock.sentinel.destroy_disks))
    def test_live_migration_cleanup_flags_delegates_other_drivers(
            self, base_flags):
        data = mock.sentinel.other_migrate_data
        migr_ctxt = mock.sentinel.migration_context

        result = self.compute._live_migration_cleanup_flags(
            data, migr_ctxt=migr_ctxt)

        self.assertEqual(
            (mock.sentinel.cleanup, mock.sentinel.destroy_disks), result)
        base_flags.assert_called_once_with(data, migr_ctxt=migr_ctxt)

    @mock.patch.object(
        manager.manager.ComputeManager, '_rollback_live_migration')
    def test_rollback_live_migration_finalizes_after_base_rollback(
            self, base_rollback):
        ctxt = context.get_admin_context()
        instance = mock.sentinel.instance
        data = migrate_data.IncusLiveMigrateData()
        calls = []
        base_rollback.side_effect = (
            lambda *a, **kw: calls.append('base'))
        finalize = self.compute.driver.finalize_live_migration_rollback
        finalize.side_effect = (
            lambda *a, **kw: calls.append('finalize'))

        self.compute._rollback_live_migration(
            ctxt, instance, 'dest-host', migrate_data=data)

        self.assertEqual(['base', 'finalize'], calls)
        finalize.assert_called_once_with(ctxt, instance, data)

    @mock.patch.object(
        manager.manager.ComputeManager, '_rollback_live_migration')
    def test_rollback_live_migration_pre_live_retires_source_generation(
            self, base_rollback):
        data = migrate_data.IncusLiveMigrateData()
        instance = mock.Mock(host=self.compute.host)

        self.compute._rollback_live_migration(
            mock.sentinel.context, instance, 'dest-host',
            migrate_data=data, pre_live_migration=True)

        base_rollback.assert_called_once()
        finalize = self.compute.driver.finalize_live_migration_rollback
        finalize.assert_not_called()
        retire = self.compute.driver.finalize_pre_live_migration_rollback
        retire.assert_called_once_with(instance, data)

    @mock.patch.object(manager.objects.BlockDeviceMappingList,
                       'get_by_instance_uuid')
    @mock.patch.object(manager.objects.InstanceList, 'get_by_host')
    def test_startup_rolls_back_an_abandoned_detach(
            self, get_by_host, get_bdms):
        """A detach that never reached the driver must not stick."""
        instance = self._idmap_instance()
        instance.task_state = None
        get_by_host.return_value = [instance]
        bdm = mock.Mock(volume_id='vol-1', deleted=False)
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        self.compute.volume_api.get.return_value = {'status': 'detaching'}
        self.compute.driver.holds_volume_attachment.return_value = True

        self.compute._roll_back_interrupted_detaches(
            context.get_admin_context())

        self.compute.volume_api.roll_detaching.assert_called_once_with(
            mock.ANY, 'vol-1')

    @mock.patch.object(manager.objects.BlockDeviceMappingList,
                       'get_by_instance_uuid')
    @mock.patch.object(manager.objects.InstanceList, 'get_by_host')
    def test_startup_leaves_an_in_flight_detach_alone(
            self, get_by_host, get_bdms):
        """A driver that reached the disconnect owns its own recovery."""
        instance = self._idmap_instance()
        instance.task_state = None
        get_by_host.return_value = [instance]
        bdm = mock.Mock(volume_id='vol-1', deleted=False)
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        self.compute.volume_api.get.return_value = {'status': 'detaching'}
        self.compute.driver.holds_volume_attachment.return_value = False

        self.compute._roll_back_interrupted_detaches(
            context.get_admin_context())

        self.compute.volume_api.roll_detaching.assert_not_called()

    @mock.patch.object(manager.objects.BlockDeviceMappingList,
                       'get_by_instance_uuid')
    @mock.patch.object(manager.objects.InstanceList, 'get_by_host')
    def test_startup_ignores_volumes_that_are_not_detaching(
            self, get_by_host, get_bdms):
        instance = self._idmap_instance()
        instance.task_state = None
        get_by_host.return_value = [instance]
        get_bdms.return_value = [mock.Mock(volume_id='vol-1', deleted=False)]
        self.compute.volume_api = mock.Mock()
        self.compute.volume_api.get.return_value = {'status': 'in-use'}

        self.compute._roll_back_interrupted_detaches(
            context.get_admin_context())

        self.compute.volume_api.roll_detaching.assert_not_called()
        self.compute.driver.holds_volume_attachment.assert_not_called()

    @mock.patch.object(manager.manager.ComputeManager, '_attach_volume')
    def test_hot_attach_commits_journal_after_upstream_completion(
            self, base_attach):
        instance = self._idmap_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        connection_info = {
            'serial': volume_id,
            'driver_volume_type': 'rbd',
            'data': {'name': 'volumes/volume-{}'.format(volume_id)},
        }
        bdm = mock.Mock(
            volume_id=volume_id,
            attachment_id='40000000-0000-0000-0000-000000000004',
            mount_device='/dev/vdb')
        bdm.get.side_effect = lambda key, default=None: (
            connection_info if key == 'connection_info' else default)
        base_attach.return_value = mock.sentinel.result

        result = self.compute._attach_volume(
            context.get_admin_context(), instance, bdm)

        self.assertIs(mock.sentinel.result, result)
        base_attach.assert_called_once_with(mock.ANY, instance, bdm)
        confirm = self.compute.driver.confirm_connected_volume_journal
        confirm.assert_called_once_with(
            instance, volume_id, connection_info,
            expected_mountpoint='/dev/vdb')
        self.compute.driver.prepare_managed_volume_attach.\
            assert_called_once_with(
                instance, volume_id,
                '40000000-0000-0000-0000-000000000004', '/dev/vdb')
        self.compute.driver.cancel_managed_volume_attach.assert_called_once()

    @mock.patch.object(manager.manager.ComputeManager, '_attach_volume')
    def test_hot_attach_retains_bdm_when_journal_confirmation_fails(
            self, base_attach):
        instance = self._idmap_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        connection_info = {
            'serial': volume_id,
            'driver_volume_type': 'rbd',
            'data': {'name': 'volumes/volume-{}'.format(volume_id)},
        }
        bdm = mock.Mock(
            volume_id=volume_id,
            attachment_id='40000000-0000-0000-0000-000000000004',
            mount_device='/dev/vdb')
        bdm.get.side_effect = lambda key, default=None: (
            connection_info if key == 'connection_info' else default)
        base_attach.return_value = mock.sentinel.result
        confirm = self.compute.driver.confirm_connected_volume_journal
        confirm.side_effect = RuntimeError('journal fsync failed')

        result = self.compute._attach_volume(
            context.get_admin_context(), instance, bdm)

        self.assertIs(mock.sentinel.result, result)
        confirm.assert_called_once()
        bdm.destroy.assert_not_called()

    @mock.patch.object(manager.manager.ComputeManager, '_attach_volume')
    def test_hot_attach_failure_retains_pre_driver_intent(self, base_attach):
        instance = self._idmap_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        bdm = mock.Mock(
            volume_id=volume_id,
            attachment_id='40000000-0000-0000-0000-000000000004',
            mount_device='/dev/vdb')
        base_attach.side_effect = RuntimeError('attach failed')

        self.assertRaises(
            RuntimeError, self.compute._attach_volume,
            context.get_admin_context(), instance, bdm)

        self.compute.driver.prepare_managed_volume_attach.\
            assert_called_once()
        self.compute.driver.cancel_managed_volume_attach.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    @mock.patch.object(manager.manager.ComputeManager, '_attach_volume')
    def test_hot_attach_transaction_blocks_periodic_recovery(
            self, base_attach, get_instance):
        instance = self._idmap_instance()
        instance.host = self.compute.host
        instance.task_state = None
        volume_id = '50000000-0000-0000-0000-000000000005'
        connection_info = {
            'serial': volume_id,
            'driver_volume_type': 'rbd',
            'data': {'name': 'volumes/volume-{}'.format(volume_id)},
        }
        bdm = mock.Mock(
            volume_id=volume_id,
            attachment_id='40000000-0000-0000-0000-000000000004',
            device_name='/dev/vdb')
        bdm.get.side_effect = lambda key, default=None: (
            connection_info if key == 'connection_info' else default)
        base_entered = threading.Event()
        release_base = threading.Event()
        recovery_waiting = threading.Event()
        failures = []
        locks = {}
        transaction_name = manager._volume_manager_transaction_lock_name(
            instance.uuid, volume_id)

        @contextlib.contextmanager
        def serialized_lock(name, **kwargs):
            lock = locks.setdefault(name, threading.Lock())
            if (name == transaction_name and
                    threading.current_thread().name == 'periodic'):
                recovery_waiting.set()
            lock.acquire()
            try:
                yield
            finally:
                lock.release()

        def hold_base(*args, **kwargs):
            base_entered.set()
            self.assertTrue(release_base.wait(5))
            return mock.sentinel.result

        base_attach.side_effect = hold_base
        get_instance.return_value = instance
        self.compute.driver.list_volume_journal_recovery_candidates.\
            return_value = [{
                'uuid': instance.uuid,
                'volume_ids': [volume_id],
                'phases': {volume_id: 'connecting'},
            }]
        self.compute.driver.get_volume_journal_recovery_phase.return_value = (
            'connecting')

        def run_attach():
            try:
                self.compute._attach_volume(
                    context.get_admin_context(), instance, bdm)
            except Exception as exc:
                failures.append(exc)

        def run_periodic():
            try:
                self.compute._recover_incus_volume_journals(
                    context.get_admin_context())
            except Exception as exc:
                failures.append(exc)

        with mock.patch.object(
                manager.lockutils, 'lock', side_effect=serialized_lock), \
                mock.patch.object(
                    self.compute,
                    '_recover_incus_connecting_volume_journal') as recover:
            attach_thread = threading.Thread(
                target=run_attach, name='attach')
            periodic_thread = threading.Thread(
                target=run_periodic, name='periodic')
            attach_thread.start()
            self.assertTrue(base_entered.wait(5))
            periodic_thread.start()
            self.assertTrue(recovery_waiting.wait(5))
            recover.assert_not_called()
            release_base.set()
            attach_thread.join(5)
            periodic_thread.join(5)

        self.assertFalse(attach_thread.is_alive())
        self.assertFalse(periodic_thread.is_alive())
        self.assertEqual([], failures)
        recover.assert_called_once_with(
            mock.ANY, instance, volume_id, journal_phase='connecting')

    @mock.patch.object(manager.manager.ComputeManager, '_detach_volume')
    def test_managed_detach_transaction_covers_upstream_and_finalize(
            self, base_detach):
        instance = self._idmap_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        bdm = mock.Mock(
            volume_id=volume_id,
            attachment_id='40000000-0000-0000-0000-000000000004',
            device_name='/dev/vdb')
        events = []
        transaction_name = manager._volume_manager_transaction_lock_name(
            instance.uuid, volume_id)

        @contextlib.contextmanager
        def tracked_lock(name, **kwargs):
            events.append(('enter', name))
            try:
                yield
            finally:
                events.append(('exit', name))

        base_detach.side_effect = lambda *args, **kwargs: events.append(
            ('base', volume_id))
        finalize = self.compute.driver.finalize_disconnected_volume_journal
        finalize.side_effect = (
            lambda *args, **kwargs: events.append(('finalize', volume_id)))

        with mock.patch.object(
                manager.lockutils, 'lock', side_effect=tracked_lock):
            self.compute._detach_volume(
                context.get_admin_context(), bdm, instance)

        self.assertEqual(('enter', transaction_name), events[0])
        self.assertLess(
            events.index(('base', volume_id)),
            events.index(('finalize', volume_id)))
        self.assertEqual(('exit', transaction_name), events[-1])

    @staticmethod
    def _cinder_attachment(instance, volume_id, status):
        return {
            'id': '40000000-0000-0000-0000-000000000004',
            'volume_id': volume_id,
            'connection_info': {
                'status': status,
                'instance': instance.uuid,
                'volume_id': volume_id,
                'driver_volume_type': 'rbd',
                'data': {
                    'name': 'volumes/volume-{}'.format(volume_id),
                },
            },
        }

    @staticmethod
    def _cinder_volume(instance, volume_id, attachment_status=None,
                       attachment_id=None, volume_status=None):
        attachments = {}
        if attachment_status == 'attached':
            attachments[instance.uuid] = {
                'attachment_id': attachment_id,
                'mountpoint': '/dev/vdb',
            }
        return {
            'id': volume_id,
            'status': volume_status or (
                'in-use' if attachments else 'available'),
            'attach_status': 'attached' if attachments else 'detached',
            'attachments': attachments,
        }

    def _configure_cinder_recovery_attachment(
            self, instance, volume_id, status):
        detail = self._cinder_attachment(instance, volume_id, status)
        self.compute.volume_api.attachment_get.return_value = detail
        volume_status = {
            'attached': 'in-use',
            'attaching': 'attaching',
            'reserved': 'reserved',
            'error_attaching': 'error',
            'detached': 'available',
            'deleted': 'available',
        }.get(status, 'error')
        self.compute.volume_api.get.return_value = self._cinder_volume(
            instance, volume_id, attachment_status=status,
            attachment_id=detail['id'], volume_status=volume_status)
        return detail

    def _volume_recovery_instance(self):
        instance = self._idmap_instance()
        instance.host = self.compute.host
        instance.task_state = None
        self.compute.driver.get_managed_volume_attach_intent.return_value = {
            'attachment_id': '40000000-0000-0000-0000-000000000004',
            'mountpoint': '/dev/vdb',
            'operation_kind': 'hot-attach',
            'operation_token': None,
            'operation_direction': None,
        }
        self.compute.driver.get_volume_journal_phase.return_value = (
            'connecting')
        self.compute.driver.get_volume_journal_recovery_phase.return_value = (
            'connecting')
        return instance

    def _volume_recovery_bdm(self, volume_id, connection_info=None):
        bdm = mock.Mock(
            volume_id=volume_id,
            deleted=False,
            attachment_id='40000000-0000-0000-0000-000000000004',
            device_name='/dev/vdb')
        bdm.connection_info = (
            jsonutils.dumps(connection_info)
            if connection_info is not None else None)
        return bdm

    def _configure_internal_volume_recovery(
            self, instance, volume_id, operation_kind, operation_token,
            operation_direction, operation_migration_uuid=None):
        attachment = self._cinder_attachment(
            instance, volume_id, 'attached')
        connection_info = dict(attachment['connection_info'])
        connection_info['serial'] = volume_id
        bdm = self._volume_recovery_bdm(
            volume_id, connection_info=connection_info)
        self.compute.driver.get_managed_volume_attach_intent.return_value = {
            'attachment_id': bdm.attachment_id,
            'mountpoint': bdm.device_name,
            'operation_kind': operation_kind,
            'operation_token': operation_token,
            'operation_direction': operation_direction,
            'operation_migration_uuid': (
                operation_migration_uuid
                if operation_kind == 'migration' else None),
        }
        self.compute.volume_api = mock.Mock()
        self._configure_cinder_recovery_attachment(
            instance, volume_id, 'attached')
        return bdm

    def _configure_managed_detach(
            self, volume_id, phase='disconnecting', destroy_bdm=True):
        intent = {
            'attachment_id': '40000000-0000-0000-0000-000000000004',
            'mountpoint': '/dev/vdb',
            'destroy_bdm': destroy_bdm,
        }
        self.compute.driver.get_managed_volume_detach_intent.return_value = (
            intent)
        self.compute.driver.get_volume_journal_phase.return_value = phase
        return intent

    @staticmethod
    def _rotation_attachment(
            instance, volume_id, attachment_id, status='attached'):
        connection_info = {
            'status': status,
            'instance': instance.uuid,
            'volume_id': volume_id,
        }
        if status in ('attaching', 'attached'):
            connection_info.update({
                'driver_volume_type': 'rbd',
                'data': {'name': 'volumes/volume-{}'.format(volume_id)},
            })
        return {
            'id': attachment_id,
            'volume_id': volume_id,
            'status': status,
            'instance': instance.uuid,
            'connection_info': connection_info,
        }

    def _configure_cold_rotation(self, volume_count=2):
        instance = self._volume_recovery_instance()
        instance.task_state = task_states.RESIZE_MIGRATING
        instance.migration_context = mock.Mock(migration_id=7)
        token = '60000000-0000-0000-0000-000000000006'
        migration = mock.MagicMock(
            uuid=token, source_compute=self.compute.host,
            dest_compute='compute-2', status='migrating')
        self.compute.driver.get_cold_source_migration_token.return_value = (
            token)
        volumes = []
        attachments = {}
        intents = {}
        rotations = {}
        events = []
        for index in range(volume_count):
            volume_id = '50000000-0000-0000-0000-{:012d}'.format(index + 1)
            old_id = '40000000-0000-0000-0000-{:012d}'.format(index + 1)
            mountpoint = '/dev/vd{}'.format(chr(ord('b') + index))
            connection_info = self._rotation_attachment(
                instance, volume_id, old_id)['connection_info']
            bdm = mock.Mock(
                is_volume=True, boot_index=-1, volume_id=volume_id,
                attachment_id=old_id, device_name=mountpoint, deleted=False)
            bdm.connection_info = jsonutils.dumps(connection_info)
            bdm.save.side_effect = lambda volume_id=volume_id: events.append(
                ('bdm-save', volume_id))
            volumes.append(bdm)
            attachments[old_id] = self._rotation_attachment(
                instance, volume_id, old_id)
            intents[volume_id] = {
                'attachment_id': old_id,
                'mountpoint': mountpoint,
                'operation_kind': 'migration',
                'operation_token': token,
                'operation_direction': 'cold-source-restore',
                'operation_migration_uuid': token,
                'boot_volume': False,
            }

        self.compute.volume_api = mock.Mock()

        def attachment_get(unused_context, attachment_id):
            try:
                return attachments[attachment_id]
            except KeyError:
                raise exception.VolumeAttachmentNotFound(
                    attachment_id=attachment_id)

        def attachment_get_all(unused_context, instance_id=None,
                               volume_id=None):
            return [
                value for value in attachments.values()
                if value['volume_id'] == volume_id]

        def attachment_create(unused_context, volume_id, instance_uuid):
            new_id = '70000000-0000-0000-0000-{}'.format(volume_id[-12:])
            attachments[new_id] = self._rotation_attachment(
                instance, volume_id, new_id, status='reserved')
            events.append(('create', volume_id))
            return {'id': new_id}

        def attachment_delete(unused_context, attachment_id):
            volume_id = attachments[attachment_id]['volume_id']
            events.append(('delete', volume_id))
            attachments.pop(attachment_id)

        self.compute.volume_api.attachment_get.side_effect = attachment_get
        self.compute.volume_api.attachment_get_all.side_effect = (
            attachment_get_all)
        self.compute.volume_api.attachment_create.side_effect = (
            attachment_create)
        self.compute.volume_api.attachment_delete.side_effect = (
            attachment_delete)
        self.compute.driver.get_managed_volume_attach_intent.side_effect = (
            lambda unused_instance, volume_id: intents.get(volume_id))
        self.compute.driver.get_cold_attachment_rotation.side_effect = (
            lambda unused_instance, volume_id: rotations.get(volume_id))

        def prepare_rotation(
                unused_instance, volume_id, old_attachment_id, mountpoint,
                operation_token, migration_uuid, baseline_attachment_ids,
                boot_volume=False):
            payload = {
                'old_attachment_id': old_attachment_id,
                'new_attachment_id': None,
                'mountpoint': mountpoint,
                'operation_token': operation_token,
                'migration_uuid': migration_uuid,
                'baseline_attachment_ids': baseline_attachment_ids,
                'phase': 'prepared',
                'boot_volume': boot_volume,
            }
            rotations[volume_id] = payload
            events.append(('prepared', volume_id))
            return payload, True

        def transition(
                unused_instance, volume_id, expected, phase,
                new_attachment_id=None):
            self.assertEqual(expected, rotations[volume_id])
            payload = dict(expected)
            payload['phase'] = phase
            if new_attachment_id is not None:
                payload['new_attachment_id'] = new_attachment_id
            rotations[volume_id] = payload
            events.append((phase, volume_id))
            return payload

        self.compute.driver.prepare_cold_attachment_rotation.side_effect = (
            prepare_rotation)
        self.compute.driver.transition_cold_attachment_rotation.side_effect = (
            transition)
        return (
            instance, migration, volumes, attachments, intents, rotations,
            events)

    @mock.patch.object(
        manager.objects.BlockDeviceMapping, 'get_by_volume_and_instance')
    @mock.patch.object(manager.objects.Migration, 'get_by_id_and_instance')
    def test_cold_attachment_rotation_n2_orders_every_commit_point(
            self, get_migration, get_bdm):
        (instance, migration, volumes, unused_attachments, unused_intents,
         rotations, events) = self._configure_cold_rotation()
        get_migration.return_value = migration
        by_volume = {bdm.volume_id: bdm for bdm in volumes}
        get_bdm.side_effect = (
            lambda unused_context, volume_id, unused_instance:
            by_volume[volume_id])

        self.compute._terminate_volume_connections(
            context.get_admin_context(), instance, volumes)

        self.compute.volume_api.attachment_get_all.assert_not_called()
        self.assertEqual(
            {'bdm-rotated'}, {value['phase'] for value in rotations.values()})
        for bdm in volumes:
            volume_id = bdm.volume_id
            self.assertLess(
                events.index(('prepared', volume_id)),
                events.index(('creating', volume_id)))
            self.assertLess(
                events.index(('creating', volume_id)),
                events.index(('create', volume_id)))
            self.assertLess(
                events.index(('new-created', volume_id)),
                events.index(('delete', volume_id)))
            self.assertLess(
                events.index(('old-deleted', volume_id)),
                events.index(('bdm-save', volume_id)))
            self.assertLess(
                events.index(('bdm-save', volume_id)),
                events.index(('bdm-rotated', volume_id)))

    @mock.patch.object(manager.objects.Migration, 'get_by_id_and_instance')
    def test_cold_attachment_rotation_lost_create_response_fails_closed(
            self, get_migration):
        (instance, migration, volumes, attachments, unused_intents, rotations,
         events) = self._configure_cold_rotation(volume_count=1)
        get_migration.return_value = migration
        bdm = volumes[0]
        new_id = '70000000-0000-0000-0000-000000000001'

        def lost_response(unused_context, volume_id, unused_instance):
            attachments[new_id] = self._rotation_attachment(
                instance, volume_id, new_id, status='reserved')
            events.append(('create', volume_id))
            raise RuntimeError('response lost')

        self.compute.volume_api.attachment_create.side_effect = lost_response
        self.assertRaises(
            RuntimeError, self.compute._terminate_volume_connections,
            context.get_admin_context(), instance, volumes)
        self.assertEqual('creating', rotations[bdm.volume_id]['phase'])
        old_id = rotations[bdm.volume_id]['old_attachment_id']
        self.assertIn(old_id, attachments)
        self.assertEqual(old_id, bdm.attachment_id)

        self.assertRaises(
            exception.InvalidVolume,
            self.compute._terminate_volume_connections,
            context.get_admin_context(), instance, volumes)
        self.assertEqual(1, events.count(('create', bdm.volume_id)))
        self.compute.volume_api.attachment_delete.assert_not_called()
        bdm.save.assert_not_called()

    def test_cold_attachment_rotation_never_adopts_unknown_create_result(self):
        (instance, unused_migration, volumes, attachments, unused_intents,
         rotations, unused_events) = self._configure_cold_rotation(
             volume_count=1)
        bdm = volumes[0]
        volume_id = bdm.volume_id
        old_id = bdm.attachment_id
        rotation = {
            'old_attachment_id': old_id,
            'new_attachment_id': None,
            'mountpoint': bdm.device_name,
            'operation_token':
                '60000000-0000-0000-0000-000000000006',
            'migration_uuid':
                '60000000-0000-0000-0000-000000000006',
            'baseline_attachment_ids': [old_id],
            'phase': 'creating',
            'boot_volume': False,
        }
        rotations[volume_id] = rotation

        for candidate_count in (0, 1, 2):
            with self.subTest(candidate_count=candidate_count):
                candidates = []
                for index in range(candidate_count):
                    attachment_id = (
                        '71000000-0000-0000-0000-{:012d}'.format(index + 1))
                    candidate = self._rotation_attachment(
                        instance, volume_id, attachment_id, status='reserved')
                    attachments[attachment_id] = candidate
                    candidates.append(candidate)
                self.compute.volume_api.attachment_get_all.return_value = (
                    candidates)

                self.assertRaises(
                    exception.InvalidVolume,
                    self.compute._advance_cold_attachment_rotation_locked,
                    context.get_admin_context(), instance, bdm, rotation)

                self.assertEqual(old_id, bdm.attachment_id)
                self.compute.volume_api.attachment_create.assert_not_called()
                self.compute.volume_api.attachment_delete.assert_not_called()
                self.compute.volume_api.attachment_get_all.assert_not_called()
                bdm.save.assert_not_called()
                for candidate in candidates:
                    attachments.pop(candidate['id'])

    @mock.patch.object(
        manager.objects.BlockDeviceMapping, 'get_by_volume_and_instance')
    def test_cold_attachment_rotation_recovers_delete_response_loss(
            self, get_bdm):
        (instance, unused_migration, volumes, attachments, unused_intents,
         rotations, events) = self._configure_cold_rotation(volume_count=1)
        bdm = volumes[0]
        volume_id = bdm.volume_id
        old_id = bdm.attachment_id
        new_id = '70000000-0000-0000-0000-000000000001'
        attachments[new_id] = self._rotation_attachment(
            instance, volume_id, new_id, status='reserved')
        rotations[volume_id] = {
            'old_attachment_id': old_id,
            'new_attachment_id': new_id,
            'mountpoint': bdm.device_name,
            'operation_token':
                '60000000-0000-0000-0000-000000000006',
            'migration_uuid':
                '60000000-0000-0000-0000-000000000006',
            'baseline_attachment_ids': [old_id],
            'phase': 'new-created',
            'boot_volume': False,
        }
        get_bdm.return_value = bdm
        attempts = [0]

        def lost_delete(unused_context, attachment_id):
            attempts[0] += 1
            attachments.pop(attachment_id)
            raise RuntimeError('delete response lost')

        self.compute.volume_api.attachment_delete.side_effect = lost_delete
        result = self.compute._advance_cold_attachment_rotation_locked(
            context.get_admin_context(), instance, bdm, rotations[volume_id])

        self.assertEqual('bdm-rotated', result['phase'])
        self.assertEqual(1, attempts[0])
        self.assertEqual(new_id, bdm.attachment_id)
        self.assertNotIn(old_id, attachments)
        self.assertLess(
            events.index(('old-deleted', volume_id)),
            events.index(('bdm-save', volume_id)))

    @mock.patch.object(
        manager.objects.BlockDeviceMapping, 'get_by_volume_and_instance')
    def test_cold_attachment_rotation_recovers_bdm_save_response_loss(
            self, get_bdm):
        (instance, unused_migration, volumes, attachments, unused_intents,
         rotations, unused_events) = self._configure_cold_rotation(
             volume_count=1)
        bdm = volumes[0]
        volume_id = bdm.volume_id
        old_id = bdm.attachment_id
        new_id = '70000000-0000-0000-0000-000000000001'
        attachments.pop(old_id)
        attachments[new_id] = self._rotation_attachment(
            instance, volume_id, new_id, status='reserved')
        rotations[volume_id] = {
            'old_attachment_id': old_id,
            'new_attachment_id': new_id,
            'mountpoint': bdm.device_name,
            'operation_token':
                '60000000-0000-0000-0000-000000000006',
            'migration_uuid':
                '60000000-0000-0000-0000-000000000006',
            'baseline_attachment_ids': [old_id],
            'phase': 'old-deleted',
            'boot_volume': False,
        }
        durable_id = [old_id]
        save_calls = [0]

        def current_bdm(unused_context, unused_volume, unused_instance):
            current = mock.Mock(
                attachment_id=durable_id[0], volume_id=volume_id,
                device_name=bdm.device_name)

            def save():
                save_calls[0] += 1
                durable_id[0] = current.attachment_id
                raise RuntimeError('BDM save response lost')

            current.save.side_effect = save
            return current

        get_bdm.side_effect = current_bdm
        result = self.compute._advance_cold_attachment_rotation_locked(
            context.get_admin_context(), instance, bdm, rotations[volume_id])

        self.assertEqual('bdm-rotated', result['phase'])
        self.assertEqual(new_id, durable_id[0])
        self.assertEqual(1, save_calls[0])
        self.compute.volume_api.attachment_delete.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMapping, 'get_by_volume_and_instance')
    def test_cold_attachment_rotation_rejects_unknown_bdm_owner(
            self, get_bdm):
        (instance, unused_migration, volumes, attachments, unused_intents,
         rotations, unused_events) = self._configure_cold_rotation(
             volume_count=1)
        bdm = volumes[0]
        volume_id = bdm.volume_id
        old_id = bdm.attachment_id
        new_id = '70000000-0000-0000-0000-000000000001'
        third_id = '72000000-0000-0000-0000-000000000001'
        attachments.pop(old_id)
        attachments[new_id] = self._rotation_attachment(
            instance, volume_id, new_id, status='reserved')
        rotations[volume_id] = {
            'old_attachment_id': old_id,
            'new_attachment_id': new_id,
            'mountpoint': bdm.device_name,
            'operation_token':
                '60000000-0000-0000-0000-000000000006',
            'migration_uuid':
                '60000000-0000-0000-0000-000000000006',
            'baseline_attachment_ids': [old_id],
            'phase': 'old-deleted',
            'boot_volume': False,
        }
        current = mock.Mock(
            attachment_id=third_id, volume_id=volume_id,
            device_name=bdm.device_name)
        get_bdm.return_value = current

        self.assertRaises(
            exception.InvalidVolume,
            self.compute._advance_cold_attachment_rotation_locked,
            context.get_admin_context(), instance, bdm, rotations[volume_id])

        self.assertEqual(third_id, current.attachment_id)
        current.save.assert_not_called()
        self.compute.volume_api.attachment_create.assert_not_called()
        self.compute.volume_api.attachment_delete.assert_not_called()

    def test_failed_cold_rotation_keeps_old_and_deletes_replacement(self):
        (instance, migration, volumes, attachments, intents, rotations,
         unused_events) = self._configure_cold_rotation(volume_count=1)
        migration.status = 'failed'
        bdm = volumes[0]
        volume_id = bdm.volume_id
        old_id = bdm.attachment_id
        new_id = '70000000-0000-0000-0000-000000000001'
        attachments[new_id] = self._rotation_attachment(
            instance, volume_id, new_id, status='reserved')
        rotations[volume_id] = {
            'old_attachment_id': old_id,
            'new_attachment_id': new_id,
            'mountpoint': bdm.device_name,
            'operation_token': migration.uuid,
            'migration_uuid': migration.uuid,
            'baseline_attachment_ids': [old_id],
            'phase': 'new-created',
            'boot_volume': False,
        }
        self.compute.driver.cancel_cold_attachment_rotation.side_effect = (
            lambda unused_instance, unused_volume, unused_rotation:
            rotations.pop(volume_id))

        handled = self.compute._recover_failed_cold_attachment_rotation_locked(
            context.get_admin_context(), instance, volume_id,
            intents[volume_id], bdm, rotations[volume_id], migration)

        self.assertFalse(handled)
        self.assertIn(old_id, attachments)
        self.assertNotIn(new_id, attachments)
        self.assertEqual(old_id, bdm.attachment_id)
        self.compute.driver.fence_failed_cold_source_volume_generation.\
            assert_called_once_with(instance, migration.uuid)
        self.compute.driver.restart_internal_volume_attach.assert_not_called()

    def test_failed_cold_rotation_replays_deleted_replacement(self):
        (instance, migration, volumes, attachments, intents, rotations,
         events) = self._configure_cold_rotation(volume_count=1)
        migration.status = 'failed'
        bdm = volumes[0]
        volume_id = bdm.volume_id
        old_id = bdm.attachment_id
        new_id = '70000000-0000-0000-0000-000000000001'
        rotations[volume_id] = {
            'old_attachment_id': old_id,
            'new_attachment_id': new_id,
            'mountpoint': bdm.device_name,
            'operation_token': migration.uuid,
            'migration_uuid': migration.uuid,
            'baseline_attachment_ids': [old_id],
            'phase': 'new-created',
            'boot_volume': False,
        }

        handled = self.compute._recover_failed_cold_attachment_rotation_locked(
            context.get_admin_context(), instance, volume_id,
            intents[volume_id], bdm, rotations[volume_id], migration)

        self.assertFalse(handled)
        self.assertIn(old_id, attachments)
        self.assertNotIn(new_id, attachments)
        self.assertEqual(old_id, bdm.attachment_id)
        self.assertIn(('source-old-retained', volume_id), events)
        self.compute.volume_api.attachment_delete.assert_not_called()
        self.compute.driver.restart_internal_volume_attach.assert_not_called()

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    def test_failed_cold_bfv_replays_source_old_retained(
            self, get_migrations):
        (instance, migration, volumes, unused_attachments, intents, rotations,
         events) = self._configure_cold_rotation(volume_count=1)
        migration.status = 'failed'
        bdm = volumes[0]
        bdm.boot_index = 0
        volume_id = bdm.volume_id
        intents[volume_id]['boot_volume'] = True
        rotations[volume_id] = {
            'old_attachment_id': bdm.attachment_id,
            'new_attachment_id':
                '70000000-0000-0000-0000-000000000001',
            'mountpoint': bdm.device_name,
            'operation_token': migration.uuid,
            'migration_uuid': migration.uuid,
            'baseline_attachment_ids': [bdm.attachment_id],
            'phase': 'source-old-retained',
            'boot_volume': True,
        }
        get_migrations.return_value = [migration]
        self.compute.driver.get_source_volume_generation_recovery_candidate.\
            return_value = {
                'operation_token': migration.uuid,
                'migration_uuid': migration.uuid,
            }
        self.compute.driver.get_internal_volume_attach_connection_info.\
            return_value = None
        self.compute.driver.get_volume_journal_phase.return_value = None

        handled = self.compute._recover_failed_cold_attachment_rotation_locked(
            context.get_admin_context(), instance, volume_id,
            intents[volume_id], bdm, rotations[volume_id], migration)

        self.assertTrue(handled)
        self.assertIn(('source-rollback-complete', volume_id), events)
        self.compute.driver.mark_source_volume_generation_rollback_complete.\
            assert_called_once_with(instance, migration.uuid, migration.uuid)
        self.compute.driver.cancel_managed_volume_attach.\
            assert_called_once_with(instance, volume_id, intents[volume_id])
        self.compute.driver.restart_internal_volume_attach.assert_not_called()
        self.compute.driver.confirm_connected_volume_journal.\
            assert_not_called()

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    def test_failed_cold_bfv_prepared_retains_terminal_owner_mismatch(
            self, get_migrations):
        (instance, migration, volumes, attachments, intents, rotations,
         unused_events) = self._configure_cold_rotation(volume_count=1)
        migration.status = 'failed'
        bdm = volumes[0]
        bdm.boot_index = 0
        volume_id = bdm.volume_id
        intents[volume_id]['boot_volume'] = True
        rotations[volume_id] = {
            'old_attachment_id': bdm.attachment_id,
            'new_attachment_id': None,
            'mountpoint': bdm.device_name,
            'operation_token': migration.uuid,
            'migration_uuid': migration.uuid,
            'baseline_attachment_ids': [bdm.attachment_id],
            'phase': 'prepared',
            'boot_volume': True,
        }
        attachments.pop(bdm.attachment_id)
        get_migrations.return_value = [migration]
        self.compute.driver.get_source_volume_generation_recovery_candidate.\
            return_value = {
                'operation_token': migration.uuid,
                'migration_uuid': migration.uuid,
            }
        self.compute.driver.get_internal_volume_attach_connection_info.\
            return_value = None
        self.compute.driver.get_volume_journal_phase.return_value = None

        self.assertRaises(
            exception.InvalidVolume,
            self.compute._recover_failed_cold_attachment_rotation_locked,
            context.get_admin_context(), instance, volume_id,
            intents[volume_id], bdm, rotations[volume_id], migration)

        self.assertEqual(
            'source-rollback-complete', rotations[volume_id]['phase'])
        self.compute.driver.cancel_managed_volume_attach.assert_not_called()
        self.compute.driver.cancel_cold_attachment_rotation.assert_not_called()
        self.compute.driver.restart_internal_volume_attach.assert_not_called()

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    def test_failed_cold_bfv_intent_only_is_control_plane_recovery(
            self, get_migrations):
        (instance, migration, volumes, attachments, intents, unused_rotations,
         unused_events) = self._configure_cold_rotation(volume_count=1)
        migration.status = 'failed'
        bdm = volumes[0]
        bdm.boot_index = 0
        volume_id = bdm.volume_id
        intent = dict(intents[volume_id], boot_volume=True)
        get_migrations.return_value = [migration]
        self.compute.driver.get_volume_journal_phase.return_value = None
        self.compute.driver.get_internal_volume_attach_connection_info.\
            return_value = None
        attachment = attachments[bdm.attachment_id]

        self.compute._recover_incus_internal_attach_locked(
            context.get_admin_context(), instance, volume_id,
            'attach-pending', intent, bdm, attachment, 'attached',
            attachment['connection_info'])

        self.compute.driver.mark_source_volume_generation_rollback_complete.\
            assert_called_once_with(instance, migration.uuid, migration.uuid)
        self.compute.driver.cancel_managed_volume_attach.\
            assert_called_once_with(instance, volume_id, intent)
        self.compute.driver.resume_internal_volume_attach.assert_not_called()
        self.compute.driver.restart_internal_volume_attach.assert_not_called()
        self.compute.driver.confirm_connected_volume_journal.\
            assert_not_called()

    def test_internal_attach_recovery_rejects_missing_bdm(self):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'

        self.assertRaises(
            exception.InvalidVolume,
            self.compute._recover_incus_internal_attach_locked,
            context.get_admin_context(), instance, volume_id,
            'attach-pending', {}, None, None, None, None)

        self.compute.driver.validate_internal_volume_attach_owner.\
            assert_not_called()

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    def test_failed_cold_bfv_intent_only_rejects_local_data_mapping(
            self, get_migrations):
        (instance, migration, volumes, attachments, intents, unused_rotations,
         unused_events) = self._configure_cold_rotation(volume_count=1)
        migration.status = 'failed'
        bdm = volumes[0]
        bdm.boot_index = 0
        volume_id = bdm.volume_id
        intent = dict(intents[volume_id], boot_volume=True)
        get_migrations.return_value = [migration]
        self.compute.driver.get_volume_journal_phase.return_value = None
        self.compute.driver.get_internal_volume_attach_connection_info.\
            return_value = {'driver_volume_type': 'rbd', 'data': {}}
        attachment = attachments[bdm.attachment_id]

        self.assertRaises(
            exception.InvalidVolume,
            self.compute._recover_incus_internal_attach_locked,
            context.get_admin_context(), instance, volume_id,
            'attach-pending', intent, bdm, attachment, 'attached',
            attachment['connection_info'])

        self.compute.driver.cancel_managed_volume_attach.assert_not_called()
        self.compute.driver.resume_internal_volume_attach.assert_not_called()
        self.compute.driver.confirm_connected_volume_journal.\
            assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMapping, 'get_by_volume_and_instance')
    def test_failed_cold_rotation_promotes_known_replacement(
            self, get_bdm):
        (instance, migration, volumes, attachments, intents, rotations,
         unused_events) = self._configure_cold_rotation(volume_count=1)
        migration.status = 'error'
        bdm = volumes[0]
        volume_id = bdm.volume_id
        old_id = bdm.attachment_id
        new_id = '70000000-0000-0000-0000-000000000001'
        attachments.pop(old_id)
        attachments[new_id] = self._rotation_attachment(
            instance, volume_id, new_id, status='reserved')
        rotations[volume_id] = {
            'old_attachment_id': old_id,
            'new_attachment_id': new_id,
            'mountpoint': bdm.device_name,
            'operation_token': migration.uuid,
            'migration_uuid': migration.uuid,
            'baseline_attachment_ids': [old_id],
            'phase': 'old-deleted',
            'boot_volume': False,
        }
        get_bdm.return_value = bdm

        def attachment_update(
                unused_context, attachment_id, unused_connector,
                mountpoint=None):
            self.assertEqual(new_id, attachment_id)
            self.assertEqual(bdm.device_name, mountpoint)
            attachments[new_id] = self._rotation_attachment(
                instance, volume_id, new_id, status='attaching')
            return attachments[new_id]

        def attachment_complete(unused_context, attachment_id):
            attachments[attachment_id] = self._rotation_attachment(
                instance, volume_id, attachment_id, status='attached')

        self.compute.volume_api.attachment_update.side_effect = (
            attachment_update)
        self.compute.volume_api.attachment_complete.side_effect = (
            attachment_complete)
        replacement_intent = dict(intents[volume_id])
        replacement_intent['attachment_id'] = new_id
        self.compute.driver.replace_cold_source_volume_attach_intent.\
            return_value = replacement_intent

        handled = self.compute._recover_failed_cold_attachment_rotation_locked(
            context.get_admin_context(), instance, volume_id,
            intents[volume_id], bdm, rotations[volume_id], migration)

        self.assertTrue(handled)
        self.assertEqual(new_id, bdm.attachment_id)
        self.compute.driver.restart_internal_volume_attach.\
            assert_called_once_with(
                mock.ANY, instance, volume_id, mock.ANY,
                expected_mountpoint=bdm.device_name)
        self.compute.driver.cancel_managed_volume_attach.\
            assert_called_once_with(instance, volume_id, replacement_intent)
        self.compute.driver.finalize_failed_cold_source_volume_generation.\
            assert_called_once_with(instance, migration.uuid)

    @mock.patch.object(
        manager.objects.BlockDeviceMapping, 'get_by_volume_and_instance')
    def test_failed_cold_bfv_promotes_without_os_brick_or_data_journal(
            self, get_bdm):
        (instance, migration, volumes, attachments, intents, rotations,
         events) = self._configure_cold_rotation(volume_count=1)
        migration.status = 'error'
        bdm = volumes[0]
        bdm.boot_index = 0
        volume_id = bdm.volume_id
        old_id = bdm.attachment_id
        new_id = '70000000-0000-0000-0000-000000000001'
        attachments.pop(old_id)
        attachments[new_id] = self._rotation_attachment(
            instance, volume_id, new_id, status='reserved')
        intents[volume_id]['boot_volume'] = True
        rotations[volume_id] = {
            'old_attachment_id': old_id,
            'new_attachment_id': new_id,
            'mountpoint': bdm.device_name,
            'operation_token': migration.uuid,
            'migration_uuid': migration.uuid,
            'baseline_attachment_ids': [old_id],
            'phase': 'old-deleted',
            'boot_volume': True,
        }
        get_bdm.return_value = bdm

        def attachment_update(
                unused_context, attachment_id, unused_connector,
                mountpoint=None):
            attachments[attachment_id] = self._rotation_attachment(
                instance, volume_id, attachment_id, status='attaching')
            return attachments[attachment_id]

        def attachment_complete(unused_context, attachment_id):
            attachments[attachment_id] = self._rotation_attachment(
                instance, volume_id, attachment_id, status='attached')

        self.compute.volume_api.attachment_update.side_effect = (
            attachment_update)
        self.compute.volume_api.attachment_complete.side_effect = (
            attachment_complete)
        replacement_intent = dict(
            intents[volume_id], attachment_id=new_id)
        self.compute.driver.replace_cold_source_volume_attach_intent.\
            return_value = replacement_intent
        self.compute.driver.cancel_cold_attachment_rotation.side_effect = (
            lambda unused_instance, unused_volume, unused_rotation:
            rotations.pop(volume_id))

        handled = self.compute._recover_failed_cold_attachment_rotation_locked(
            context.get_admin_context(), instance, volume_id,
            intents[volume_id], bdm, rotations[volume_id], migration)

        self.assertTrue(handled)
        self.assertEqual(new_id, bdm.attachment_id)
        self.assertIn(('source-rollback-complete', volume_id), events)
        self.compute.driver.restart_internal_volume_attach.assert_not_called()
        self.compute.driver.confirm_connected_volume_journal.\
            assert_not_called()
        self.compute.driver.cancel_managed_volume_attach.\
            assert_called_once_with(instance, volume_id, replacement_intent)

    @mock.patch.object(
        manager.objects.BlockDeviceMapping, 'get_by_volume_and_instance')
    @mock.patch.object(manager.objects.Migration, 'get_by_id_and_instance')
    def test_cold_bfv_rotation_has_no_os_brick_side_effect(
            self, get_migration, get_bdm):
        (instance, migration, volumes, unused_attachments, intents, rotations,
         unused_events) = self._configure_cold_rotation(volume_count=1)
        bdm = volumes[0]
        bdm.boot_index = None
        bdm.device_name = '/dev/sda'
        instance.root_device_name = '/dev/sda'
        volume_id = bdm.volume_id
        old_intent = intents.pop(volume_id)
        boot_intent = dict(
            old_intent, mountpoint='/dev/sda', boot_volume=True)

        def prepare_intent(unused_instance, requested_volume, *_args,
                           **kwargs):
            self.assertEqual(volume_id, requested_volume)
            self.assertTrue(kwargs['boot_volume'])
            intents[volume_id] = boot_intent
            return boot_intent

        self.compute.driver.prepare_managed_volume_attach.side_effect = (
            prepare_intent)
        get_migration.return_value = migration
        get_bdm.return_value = bdm

        self.compute._terminate_volume_connections(
            context.get_admin_context(), instance, volumes)

        self.assertEqual('bdm-rotated', rotations[volume_id]['phase'])
        self.assertTrue(rotations[volume_id]['boot_volume'])
        self.compute.driver.restart_internal_volume_attach.assert_not_called()
        self.compute.driver._recover_source_release_volume_journal_locked.\
            assert_not_called()

    def test_terminate_volume_connections_delegates_other_operations(self):
        instance = self._volume_recovery_instance()
        instance.task_state = None
        bdms = [mock.Mock(is_volume=True)]
        ctxt = context.get_admin_context()
        with mock.patch.object(
                manager.manager.ComputeManager,
                '_terminate_volume_connections',
                return_value='delegated') as terminate:
            result = self.compute._terminate_volume_connections(
                ctxt, instance, bdms)

        self.assertEqual('delegated', result)
        terminate.assert_called_once_with(ctxt, instance, bdms)

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(manager.objects.RequestSpec, 'get_by_instance_uuid')
    @mock.patch.object(manager.objects.Migration, 'get_by_id_and_instance')
    def test_startup_recovers_cold_rotation_before_generic_init(
            self, get_migration, get_request_spec, get_bdms):
        instance = self._volume_recovery_instance()
        instance.task_state = task_states.RESIZE_MIGRATING
        instance.migration_context = mock.Mock(migration_id=7)
        token = '60000000-0000-0000-0000-000000000006'
        volume_id = '50000000-0000-0000-0000-000000000005'
        migration = mock.MagicMock(
            uuid=token, source_compute=self.compute.host,
            dest_compute='compute-2', source_node='node-1',
            dest_node='node-2', status='migrating')
        get_migration.return_value = migration
        request_spec = mock.sentinel.request_spec
        get_request_spec.return_value = request_spec
        get_bdms.return_value = [mock.Mock(is_volume=True)]
        generation = {
            'uuid': instance.uuid,
            'operation_token': token,
            'migration_uuid': token,
        }
        self.compute._cold_source_recovery_evidence = mock.Mock(
            side_effect=[
                ({volume_id}, {token}, []),
                (set(), {token}, [generation]),
            ])
        events = []
        migration.save.side_effect = lambda: events.append(
            'migration-{}'.format(migration.status))
        self.compute._recover_incus_volume_journals = mock.Mock(
            side_effect=lambda unused_context: events.append('journal-replay'))
        self.compute._get_instance_block_device_info = mock.Mock(
            return_value={'block_device_mapping': []})
        self.compute.network_api = mock.Mock()
        self.compute.driver.restore_failed_cold_source_storage_ownership.\
            side_effect = lambda *_args: events.append('source-owned')
        source_allocations = {'source-rp': {'resources': {'VCPU': 1}}}
        self.compute._restore_interrupted_cold_source_allocations = mock.Mock(
            side_effect=lambda *_args: events.append('placement') or
            source_allocations)
        provider_mappings = {'port-1': ['source-rp']}
        self.compute._fill_provider_mapping_based_on_allocs = mock.Mock(
            return_value=provider_mappings)
        self.compute.network_api.setup_networks_on_host.side_effect = (
            lambda *_args: events.append('network-setup'))
        self.compute.network_api.migrate_instance_finish.side_effect = (
            lambda *_args, **_kwargs: events.append('network-finish'))
        self.compute.network_api.get_instance_nw_info.return_value = []
        self.compute.driver.power_on.side_effect = (
            lambda *_args, **_kwargs: events.append('power-on'))
        self.compute._get_power_state = mock.Mock(
            return_value=power_state.RUNNING)
        instance.save.side_effect = lambda **_kwargs: events.append(
            'task-cleared')
        self.compute.driver.finalize_failed_cold_source_volume_generation.\
            side_effect = lambda *_args: events.append('finalize') or True

        recovered = self.compute._recover_interrupted_cold_source_rotation(
            context.get_admin_context(), instance)

        self.assertTrue(recovered)
        self.assertEqual('reverted', migration.status)
        self.assertIsNone(instance.task_state)
        self.assertEqual(vm_states.ACTIVE, instance.vm_state)
        self.assertEqual(power_state.RUNNING, instance.power_state)
        self.assertNotIn('old_vm_state', instance.system_metadata)
        self.assertIsNone(instance.old_flavor)
        self.assertIsNone(instance.new_flavor)
        instance.drop_migration_context.assert_called_once_with()
        self.assertEqual([
            'migration-error', 'journal-replay', 'source-owned',
            'placement', 'network-setup', 'network-finish', 'power-on',
            'migration-reverted', 'task-cleared', 'finalize'], events)
        self.compute.driver.restore_failed_cold_source_storage_ownership.\
            assert_called_once_with(instance, token)
        self.compute.network_api.setup_networks_on_host.\
            assert_called_once_with(mock.ANY, instance, self.compute.host)
        self.compute.network_api.migrate_instance_finish.\
            assert_called_once_with(
                mock.ANY, instance, migration,
                provider_mappings=provider_mappings)
        self.compute._fill_provider_mapping_based_on_allocs.\
            assert_called_once_with(
                mock.ANY, source_allocations, request_spec)
        self.compute.driver.power_on.assert_called_once_with(
            mock.ANY, instance, [], {'block_device_mapping': []})
        instance.save.assert_called_once_with(
            expected_task_state=task_states.RESIZE_MIGRATING)

    @mock.patch.object(manager.objects.RequestSpec, 'get_by_instance_uuid')
    def test_startup_provider_mapping_falls_back_for_source_only_allocation(
            self, get_request_spec):
        instance = self._volume_recovery_instance()
        source_uuid = '61000000-0000-0000-0000-000000000006'
        migration = mock.Mock(source_node='node-1')
        allocations = {source_uuid: {'resources': {'VCPU': 1}}}
        get_request_spec.side_effect = manager.messaging.RemoteError(
            'CantStartEngineError',
            'No sql_connection parameter is established')
        self.compute.reportclient = mock.Mock()
        self.compute.reportclient.get_provider_by_name.return_value = {
            'uuid': source_uuid}
        self.compute._fill_provider_mapping_based_on_allocs = mock.Mock()

        result = self.compute._startup_cold_source_provider_mappings(
            context.get_admin_context(), instance, migration, allocations)

        self.assertIsNone(result)
        self.compute._fill_provider_mapping_based_on_allocs.assert_not_called()

    @mock.patch.object(manager.objects.RequestSpec, 'get_by_instance_uuid')
    def test_startup_provider_mapping_fallback_rejects_extra_provider(
            self, get_request_spec):
        instance = self._volume_recovery_instance()
        source_uuid = '61000000-0000-0000-0000-000000000006'
        migration = mock.Mock(source_node='node-1')
        allocations = {
            source_uuid: {'resources': {'VCPU': 1}},
            '62000000-0000-0000-0000-000000000006': {
                'resources': {'NET_BW_EGR_KILOBIT_PER_SEC': 1000}},
        }
        get_request_spec.side_effect = manager.messaging.RemoteError(
            'CantStartEngineError',
            'No sql_connection parameter is established')
        self.compute.reportclient = mock.Mock()
        self.compute.reportclient.get_provider_by_name.return_value = {
            'uuid': source_uuid}

        self.assertRaises(
            exception.MigrationError,
            self.compute._startup_cold_source_provider_mappings,
            context.get_admin_context(), instance, migration, allocations)

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    @mock.patch.object(manager.objects.Migration, 'get_by_id_and_instance')
    def test_startup_periodic_retires_terminal_rotation_before_runtime(
            self, get_migration, get_instance, get_bdms):
        instance = self._volume_recovery_instance()
        instance.task_state = task_states.RESIZE_MIGRATING
        instance.migration_context = mock.Mock(migration_id=7)
        token = '60000000-0000-0000-0000-000000000006'
        volume_id = '50000000-0000-0000-0000-000000000005'
        migration = mock.Mock(
            uuid=token, source_compute=self.compute.host,
            dest_compute='compute-2', status='failed')
        get_migration.return_value = migration
        get_instance.side_effect = [instance, instance]
        bdm = self._volume_recovery_bdm(volume_id)
        get_bdms.return_value = [bdm]
        rotation = {
            'phase': 'source-rollback-complete',
            'operation_token': token,
            'migration_uuid': token,
        }
        rotations = {volume_id: rotation}
        generation = {
            'uuid': instance.uuid,
            'operation_token': token,
            'migration_uuid': token,
        }

        def candidates():
            if volume_id not in rotations:
                return []
            return [{
                'uuid': instance.uuid,
                'volume_ids': [volume_id],
                'phases': {
                    volume_id: 'rotation-source-rollback-complete'},
            }]

        self.compute.driver.list_volume_journal_recovery_candidates.\
            side_effect = candidates
        self.compute.driver.get_managed_volume_attach_intent.return_value = (
            None)
        self.compute.driver.get_cold_attachment_rotation.side_effect = (
            lambda unused_instance, requested_volume:
            rotations.get(requested_volume))
        self.compute.driver.get_volume_journal_recovery_phase.side_effect = (
            lambda unused_instance, requested_volume:
            ('rotation-source-rollback-complete'
             if requested_volume in rotations else None))
        self.compute.driver.get_source_volume_generation_recovery_candidate.\
            return_value = generation

        def retire(
                unused_context, unused_instance, requested_volume,
                unused_rotation, unused_bdm):
            rotations.pop(requested_volume)

        self.compute._retire_terminal_cold_attachment_rotation_locked = (
            mock.Mock(side_effect=retire))
        self.compute.driver.restore_failed_cold_source_storage_ownership.\
            side_effect = RuntimeError('stop after terminal replay')

        self.assertRaises(
            RuntimeError,
            self.compute._recover_interrupted_cold_source_rotation,
            context.get_admin_context(), instance)

        self.assertEqual({}, rotations)
        self.compute._retire_terminal_cold_attachment_rotation_locked.\
            assert_called_once_with(
                mock.ANY, instance, volume_id, rotation, bdm)
        self.compute.driver.restore_failed_cold_source_storage_ownership.\
            assert_called_once_with(instance, token)

    @mock.patch.object(manager.objects.Migration, 'get_by_id_and_instance')
    def test_startup_retains_uncertain_cold_rotation(self, get_migration):
        instance = self._volume_recovery_instance()
        instance.task_state = task_states.RESIZE_MIGRATING
        instance.migration_context = mock.Mock(migration_id=7)
        token = '60000000-0000-0000-0000-000000000006'
        volume_id = '50000000-0000-0000-0000-000000000005'
        migration = mock.Mock(
            uuid=token, source_compute=self.compute.host,
            dest_compute='compute-2', status='migrating')
        get_migration.return_value = migration
        self.compute._cold_source_recovery_evidence = mock.Mock(
            side_effect=[
                ({volume_id}, {token}, []),
                ({volume_id}, {token}, []),
            ])
        self.compute._recover_incus_volume_journals = mock.Mock()

        recovered = self.compute._recover_interrupted_cold_source_rotation(
            context.get_admin_context(), instance)

        self.assertFalse(recovered)
        self.assertEqual('error', migration.status)
        self.assertEqual(task_states.RESIZE_MIGRATING, instance.task_state)
        self.compute.driver.power_on.assert_not_called()
        instance.save.assert_not_called()

    @mock.patch.object(manager.objects.Migration, 'get_by_id_and_instance')
    def test_startup_accepts_post_migrating_source_window(self, get_migration):
        instance = self._volume_recovery_instance()
        instance.task_state = task_states.RESIZE_MIGRATING
        instance.migration_context = mock.Mock(migration_id=7)
        token = '60000000-0000-0000-0000-000000000006'
        migration = mock.Mock(
            uuid=token, source_compute=self.compute.host,
            dest_compute='compute-2', status='post-migrating')
        get_migration.return_value = migration
        self.compute._cold_source_recovery_evidence = mock.Mock(
            return_value=(
                {'50000000-0000-0000-0000-000000000005'}, {token}, []))
        self.compute._recover_incus_volume_journals = mock.Mock(
            side_effect=RuntimeError('stop after source decision'))

        self.assertRaises(
            RuntimeError,
            self.compute._recover_interrupted_cold_source_rotation,
            context.get_admin_context(), instance)

        self.assertEqual('error', migration.status)
        migration.save.assert_called_once_with()

    @mock.patch.object(manager.objects.Migration, 'get_by_id_and_instance')
    def test_startup_storage_restore_failure_keeps_runtime_fenced(
            self, get_migration):
        instance = self._volume_recovery_instance()
        instance.task_state = task_states.RESIZE_MIGRATING
        instance.migration_context = mock.Mock(migration_id=7)
        token = '60000000-0000-0000-0000-000000000006'
        migration = mock.Mock(
            uuid=token, source_compute=self.compute.host,
            dest_compute='compute-2', status='error')
        get_migration.return_value = migration
        generation = {
            'uuid': instance.uuid,
            'operation_token': token,
            'migration_uuid': token,
        }
        self.compute._cold_source_recovery_evidence = mock.Mock(
            side_effect=[
                ({'50000000-0000-0000-0000-000000000005'}, {token}, []),
                (set(), {token}, [generation]),
            ])
        self.compute._recover_incus_volume_journals = mock.Mock()
        self.compute.driver.restore_failed_cold_source_storage_ownership.\
            side_effect = RuntimeError('handover unavailable')

        self.assertRaises(
            RuntimeError,
            self.compute._recover_interrupted_cold_source_rotation,
            context.get_admin_context(), instance)

        self.assertEqual(task_states.RESIZE_MIGRATING, instance.task_state)
        instance.save.assert_not_called()
        self.compute.network_api.migrate_instance_finish.assert_not_called()
        self.compute.driver.power_on.assert_not_called()

    def test_startup_placement_revert_accepts_only_exact_source_owner(self):
        instance = self._volume_recovery_instance()
        migration = mock.Mock(
            uuid='60000000-0000-0000-0000-000000000006',
            source_node='node-1', dest_node='node-2')
        source_uuid = '61000000-0000-0000-0000-000000000006'
        dest_uuid = '62000000-0000-0000-0000-000000000006'
        source_allocations = {
            source_uuid: {'resources': {'VCPU': 1}},
            '63000000-0000-0000-0000-000000000006': {
                'resources': {'NET_BW_EGR_KILOBIT_PER_SEC': 1000}},
        }
        self.compute.reportclient = mock.Mock()
        self.compute.reportclient.get_provider_by_name.side_effect = [
            {'uuid': source_uuid}, {'uuid': dest_uuid}]
        self.compute.reportclient.get_allocs_for_consumer.side_effect = [
            {'allocations': {source_uuid: {'resources': {'VCPU': 1}}}},
            {'allocations': {}}, {'allocations': source_allocations},
        ]
        self.compute._revert_allocation = mock.Mock()

        result = self.compute._restore_interrupted_cold_source_allocations(
            context.get_admin_context(), instance, migration)

        self.assertEqual(source_allocations, result)
        self.compute._revert_allocation.assert_called_once_with(
            mock.ANY, instance, migration)

    def test_startup_placement_lost_ack_requires_exact_reread(self):
        instance = self._volume_recovery_instance()
        migration = mock.Mock(
            uuid='60000000-0000-0000-0000-000000000006',
            source_node='node-1', dest_node='node-2')
        source_uuid = '61000000-0000-0000-0000-000000000006'
        dest_uuid = '62000000-0000-0000-0000-000000000006'
        self.compute.reportclient = mock.Mock()
        self.compute.reportclient.get_provider_by_name.side_effect = [
            {'uuid': source_uuid}, {'uuid': dest_uuid}]
        self.compute.reportclient.get_allocs_for_consumer.side_effect = [
            {'allocations': {source_uuid: {'resources': {'VCPU': 1}}}},
            {'allocations': {}},
            {'allocations': {
                source_uuid: {'resources': {'VCPU': 1}}}},
        ]
        self.compute._revert_allocation = mock.Mock(
            side_effect=RuntimeError('Placement response lost'))

        result = self.compute._restore_interrupted_cold_source_allocations(
            context.get_admin_context(), instance, migration)

        self.assertIn(source_uuid, result)

    def test_startup_placement_rejects_destination_or_missing_source(self):
        instance = self._volume_recovery_instance()
        migration = mock.Mock(
            uuid='60000000-0000-0000-0000-000000000006',
            source_node='node-1', dest_node='node-2')
        source_uuid = '61000000-0000-0000-0000-000000000006'
        dest_uuid = '62000000-0000-0000-0000-000000000006'
        self.compute.reportclient = mock.Mock()
        self.compute.reportclient.get_provider_by_name.side_effect = [
            {'uuid': source_uuid}, {'uuid': dest_uuid}]
        self.compute.reportclient.get_allocs_for_consumer.side_effect = [
            {'allocations': {}}, {'allocations': {}},
            {'allocations': {
                dest_uuid: {'resources': {'VCPU': 1}}}},
        ]

        self.assertRaises(
            exception.MigrationError,
            self.compute._restore_interrupted_cold_source_allocations,
            context.get_admin_context(), instance, migration)

    def test_startup_placement_query_failure_never_means_no_allocation(self):
        instance = self._volume_recovery_instance()
        migration = mock.Mock(
            uuid='60000000-0000-0000-0000-000000000006',
            source_node='node-1', dest_node='node-2')
        self.compute.reportclient = mock.Mock()
        self.compute.reportclient.get_provider_by_name.side_effect = [
            {'uuid': '61000000-0000-0000-0000-000000000006'},
            {'uuid': '62000000-0000-0000-0000-000000000006'},
        ]
        self.compute.reportclient.get_allocs_for_consumer.side_effect = (
            RuntimeError('Placement unavailable'))

        self.assertRaises(
            RuntimeError,
            self.compute._restore_interrupted_cold_source_allocations,
            context.get_admin_context(), instance, migration)

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(manager.objects.RequestSpec, 'get_by_instance_uuid')
    @mock.patch.object(manager.objects.Migration, 'get_by_id_and_instance')
    def test_startup_stopped_source_restores_storage_and_network_only(
            self, get_migration, get_request_spec, get_bdms):
        instance = self._volume_recovery_instance()
        instance.system_metadata['old_vm_state'] = vm_states.STOPPED
        instance.task_state = task_states.RESIZE_MIGRATING
        instance.migration_context = mock.Mock(migration_id=7)
        token = '60000000-0000-0000-0000-000000000006'
        migration = mock.MagicMock(
            uuid=token, source_compute=self.compute.host,
            dest_compute='compute-2', source_node='node-1',
            dest_node='node-2', status='error')
        get_migration.return_value = migration
        get_request_spec.return_value = mock.sentinel.request_spec
        get_bdms.return_value = []
        generation = {
            'uuid': instance.uuid,
            'operation_token': token,
            'migration_uuid': token,
        }
        self.compute._cold_source_recovery_evidence = mock.Mock(
            side_effect=[
                ({'50000000-0000-0000-0000-000000000005'}, {token}, []),
                (set(), {token}, [generation]),
            ])
        self.compute._recover_incus_volume_journals = mock.Mock()
        self.compute._get_instance_block_device_info = mock.Mock(
            return_value={'block_device_mapping': []})
        self.compute.network_api = mock.Mock()
        self.compute.network_api.get_instance_nw_info.return_value = []
        self.compute._restore_interrupted_cold_source_allocations = mock.Mock(
            return_value={'source-rp': {'resources': {'VCPU': 1}}})
        self.compute._fill_provider_mapping_based_on_allocs = mock.Mock(
            return_value={'port-1': ['source-rp']})
        self.compute._get_power_state = mock.Mock(
            return_value=power_state.SHUTDOWN)
        self.compute.driver.finalize_failed_cold_source_volume_generation.\
            return_value = True

        recovered = self.compute._recover_interrupted_cold_source_rotation(
            context.get_admin_context(), instance)

        self.assertTrue(recovered)
        self.compute.driver.restore_failed_cold_source_storage_ownership.\
            assert_called_once_with(instance, token)
        self.compute.network_api.setup_networks_on_host.assert_called_once()
        self.compute.network_api.migrate_instance_finish.assert_called_once()
        self.compute.driver.power_on.assert_not_called()
        self.assertIsNone(instance.task_state)
        self.assertEqual(vm_states.STOPPED, instance.vm_state)
        self.assertEqual(power_state.SHUTDOWN, instance.power_state)
        self.assertEqual('reverted', migration.status)
        self.assertNotIn('old_vm_state', instance.system_metadata)
        instance.drop_migration_context.assert_called_once_with()

    def test_init_instance_fails_startup_while_rotation_unresolved(self):
        instance = self._volume_recovery_instance()
        self.compute._recover_interrupted_cold_source_rotation = mock.Mock(
            return_value=False)
        with mock.patch.object(
                manager.manager.ComputeManager, '_init_instance') as base:
            self.assertRaises(
                exception.MigrationError, self.compute._init_instance,
                context.get_admin_context(), instance)

        base.assert_not_called()

    def test_init_instance_propagates_rotation_recovery_failure(self):
        instance = self._volume_recovery_instance()
        self.compute._recover_interrupted_cold_source_rotation = mock.Mock(
            side_effect=exception.InvalidVolume(reason='ambiguous owner'))
        with mock.patch.object(
                manager.manager.ComputeManager, '_init_instance') as base:
            self.assertRaises(
                exception.InvalidVolume, self.compute._init_instance,
                context.get_admin_context(), instance)

        base.assert_not_called()

    def test_post_live_source_volumes_use_durable_recovery_per_volume(self):
        instance = self._volume_recovery_instance()
        root_volume_id = '52000000-0000-0000-0000-000000000005'
        data_volume_id = '53000000-0000-0000-0000-000000000005'
        source_bdms = [
            mock.Mock(
                is_volume=True, volume_id=root_volume_id,
                attachment_id='44000000-0000-0000-0000-000000000004'),
            mock.Mock(
                is_volume=True, volume_id=data_volume_id,
                attachment_id='45000000-0000-0000-0000-000000000004'),
        ]
        self.compute.driver.get_volume_journal_recovery_phase.side_effect = [
            None, 'attach-disconnected']
        self.compute.driver.get_managed_volume_attach_intent.side_effect = [
            {'boot_volume': True}, {'boot_volume': False}]
        self.compute._recover_incus_connecting_volume_journal = mock.Mock()
        self.compute.volume_api = mock.Mock()

        self.compute._post_live_migration_remove_source_vol_connections(
            context.get_admin_context(), instance, source_bdms)

        self.assertEqual(
            [
                mock.call(
                    mock.ANY, instance, root_volume_id,
                    journal_phase='attach-pending'),
                mock.call(
                    mock.ANY, instance, data_volume_id,
                    journal_phase='attach-disconnected'),
            ],
            self.compute._recover_incus_connecting_volume_journal.
            call_args_list)
        self.compute.volume_api.attachment_delete.assert_not_called()

    def test_post_live_source_volume_accepts_periodic_convergence(self):
        instance = self._volume_recovery_instance()
        volume_id = '53000000-0000-0000-0000-000000000005'
        attachment_id = '45000000-0000-0000-0000-000000000004'
        source_bdm = mock.Mock(
            is_volume=True, volume_id=volume_id,
            attachment_id=attachment_id)
        self.compute.driver.get_volume_journal_recovery_phase.return_value = (
            None)
        self.compute.driver.get_managed_volume_attach_intent.return_value = (
            None)
        self.compute._get_exact_cinder_attachment = mock.Mock(
            return_value=None)
        self.compute._recover_incus_connecting_volume_journal = mock.Mock()

        self.compute._post_live_migration_remove_source_vol_connections(
            context.get_admin_context(), instance, [source_bdm])

        self.compute._get_exact_cinder_attachment.assert_called_once_with(
            mock.ANY, attachment_id, volume_id, instance.uuid)
        recover = self.compute._recover_incus_connecting_volume_journal
        recover.assert_not_called()

    @mock.patch.object(manager.LOG, 'critical')
    def test_post_live_source_volume_rejects_attachment_without_evidence(
            self, critical):
        instance = self._volume_recovery_instance()
        volume_id = '53000000-0000-0000-0000-000000000005'
        attachment_id = '45000000-0000-0000-0000-000000000004'
        source_bdm = mock.Mock(
            is_volume=True, volume_id=volume_id,
            attachment_id=attachment_id)
        self.compute.driver.get_volume_journal_recovery_phase.return_value = (
            None)
        self.compute.driver.get_managed_volume_attach_intent.return_value = (
            None)
        self.compute._get_exact_cinder_attachment = mock.Mock(
            return_value={'id': attachment_id})
        self.compute._recover_incus_connecting_volume_journal = mock.Mock()

        self.compute._post_live_migration_remove_source_vol_connections(
            context.get_admin_context(), instance, [source_bdm])

        critical.assert_called_once()
        recover = self.compute._recover_incus_connecting_volume_journal
        recover.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_periodic_volume_recovery_completes_attaching_volume(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        bdm = self._volume_recovery_bdm(volume_id)
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        attachment = self._configure_cinder_recovery_attachment(
            instance, volume_id, 'attaching')
        self.compute.driver.resume_connecting_volume_journal.return_value = (
            '/dev/vdb')

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id)

        resume = self.compute.driver.resume_connecting_volume_journal
        resume.assert_called_once()
        expected_connection_info = dict(attachment['connection_info'])
        expected_connection_info['serial'] = volume_id
        self.assertEqual(
            expected_connection_info, jsonutils.loads(bdm.connection_info))
        bdm.save.assert_called_once_with()
        self.compute.volume_api.attachment_complete.assert_called_once_with(
            mock.ANY, attachment['id'])
        self.compute.volume_api.attachment_get.assert_called_once_with(
            mock.ANY, attachment['id'])
        confirm = self.compute.driver.confirm_connected_volume_journal
        confirm.assert_called_once()
        rollback = self.compute.driver.rollback_connecting_volume_journal
        rollback.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_periodic_volume_recovery_after_driver_before_bdm_save(
            self, get_bdms):
        """Cinder's attachment is authoritative before Nova saves the BDM."""
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        bdm = self._volume_recovery_bdm(volume_id)
        bdm.connection_info = None
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        attachment = self._configure_cinder_recovery_attachment(
            instance, volume_id, 'attaching')
        self.compute.driver.resume_connecting_volume_journal.return_value = (
            '/dev/vdb')

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id)

        expected_connection_info = dict(attachment['connection_info'])
        expected_connection_info['serial'] = volume_id
        self.assertEqual(
            expected_connection_info, jsonutils.loads(bdm.connection_info))
        bdm.save.assert_called_once_with()
        self.compute.volume_api.attachment_complete.assert_called_once_with(
            mock.ANY, attachment['id'])
        confirm = self.compute.driver.confirm_connected_volume_journal
        confirm.assert_called_once()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_periodic_volume_recovery_after_bdm_save_before_complete(
            self, get_bdms):
        """An attaching Cinder record is replayable after the BDM commit."""
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        bdm = self._volume_recovery_bdm(volume_id)
        attachment = self._cinder_attachment(
            instance, volume_id, 'attaching')
        bdm_connection_info = dict(attachment['connection_info'])
        bdm_connection_info['serial'] = volume_id
        bdm.connection_info = jsonutils.dumps(bdm_connection_info)
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        attachment = self._configure_cinder_recovery_attachment(
            instance, volume_id, 'attaching')
        self.compute.driver.resume_connecting_volume_journal.return_value = (
            '/dev/vdb')

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id)

        resume = self.compute.driver.resume_connecting_volume_journal
        resume.assert_called_once()
        bdm.save.assert_called_once_with()
        self.compute.volume_api.attachment_complete.assert_called_once_with(
            mock.ANY, attachment['id'])
        confirm = self.compute.driver.confirm_connected_volume_journal
        confirm.assert_called_once()
        rollback = self.compute.driver.rollback_connecting_volume_journal
        rollback.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_periodic_volume_recovery_preserves_formally_attached_volume(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        attachment = self._cinder_attachment(instance, volume_id, 'attached')
        bdm_connection_info = dict(attachment['connection_info'])
        bdm_connection_info['status'] = 'attaching'
        bdm_connection_info['serial'] = volume_id
        bdm = self._volume_recovery_bdm(
            volume_id, connection_info=bdm_connection_info)
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        attachment = self._configure_cinder_recovery_attachment(
            instance, volume_id, 'attached')

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id)

        confirm = self.compute.driver.confirm_connected_volume_journal
        confirm.assert_called_once()
        resume = self.compute.driver.resume_connecting_volume_journal
        resume.assert_not_called()
        rollback = self.compute.driver.rollback_connecting_volume_journal
        rollback.assert_not_called()
        self.compute.volume_api.attachment_delete.assert_not_called()
        bdm.destroy.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_attach_intent_only_committed_volume_retires_intent(
            self, get_bdms):
        """A failed intent unlink after commit never rolls back the volume."""
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        attachment = self._cinder_attachment(instance, volume_id, 'attached')
        bdm_connection_info = dict(attachment['connection_info'])
        bdm_connection_info['serial'] = volume_id
        bdm = self._volume_recovery_bdm(
            volume_id, connection_info=bdm_connection_info)
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        self._configure_cinder_recovery_attachment(
            instance, volume_id, 'attached')
        self.compute.driver.get_volume_journal_phase.return_value = None

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='attach-pending')

        self.compute.driver.confirm_connected_volume_journal.\
            assert_called_once_with(
                instance, volume_id, mock.ANY,
                expected_mountpoint='/dev/vdb')
        self.compute.driver.cancel_managed_volume_attach.assert_called_once()
        self.compute.driver.rollback_connecting_volume_journal.\
            assert_not_called()
        self.compute.volume_api.attachment_delete.assert_not_called()
        bdm.destroy.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_attach_pending_missing_attachment_retires_exact_bdm(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        bdm = self._volume_recovery_bdm(volume_id)
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        self.compute.volume_api.attachment_get.side_effect = (
            exception.VolumeAttachmentNotFound(
                attachment_id=bdm.attachment_id))
        self.compute.volume_api.get.return_value = self._cinder_volume(
            instance, volume_id)
        self.compute.driver.get_volume_journal_phase.return_value = None

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='attach-pending')

        bdm.destroy.assert_called_once_with()
        self.compute.driver.cancel_managed_volume_attach.assert_called_once()
        self.compute.driver.rollback_connecting_volume_journal.\
            assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_spawn_attach_pending_replays_exact_internal_owner(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        token = '60000000-0000-0000-0000-000000000006'
        bdm = self._configure_internal_volume_recovery(
            instance, volume_id, 'spawn', token, 'materialize')
        get_bdms.return_value = [bdm]
        self.compute.driver.get_volume_journal_phase.return_value = None

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='attach-pending')

        self.compute.driver.validate_internal_volume_attach_owner.\
            assert_called_once()
        self.compute.driver.resume_internal_volume_attach.\
            assert_called_once_with(
                mock.ANY, instance, volume_id, mock.ANY,
                expected_mountpoint='/dev/vdb')
        self.compute.driver.confirm_connected_volume_journal.\
            assert_not_called()
        self.compute.driver.cancel_managed_volume_attach.assert_called_once()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_spawn_connected_retires_exact_internal_owner(self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        token = '60000000-0000-0000-0000-000000000006'
        bdm = self._configure_internal_volume_recovery(
            instance, volume_id, 'spawn', token, 'materialize')
        get_bdms.return_value = [bdm]
        self.compute.driver.get_volume_journal_phase.return_value = 'connected'

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='connected')

        self.compute.driver.confirm_connected_volume_journal.\
            assert_called_once_with(
                instance, volume_id, mock.ANY,
                expected_mountpoint='/dev/vdb')
        self.compute.driver.resume_internal_volume_attach.assert_not_called()
        self.compute.driver.cancel_managed_volume_attach.assert_called_once()

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_aborted_migration_target_rolls_back_local_mapping(
            self, get_bdms, get_migrations):
        instance = self._volume_recovery_instance()
        source_host = 'incus-source'
        instance.host = source_host
        volume_id = '50000000-0000-0000-0000-000000000005'
        token = '60000000-0000-0000-0000-000000000006'
        migration_uuid = '70000000-0000-0000-0000-000000000007'
        bdm = self._configure_internal_volume_recovery(
            instance, volume_id, 'migration', token, 'cold-target',
            operation_migration_uuid=migration_uuid)
        target_attachment_id = bdm.attachment_id
        source_attachment_id = '41000000-0000-0000-0000-000000000004'
        target_attachment = self._cinder_attachment(
            instance, volume_id, 'attached')
        source_attachment = self._cinder_attachment(
            instance, volume_id, 'attached')
        source_attachment['id'] = source_attachment_id
        source_connection_info = dict(source_attachment['connection_info'])
        source_connection_info['serial'] = volume_id
        bdm.attachment_id = source_attachment_id
        bdm.connection_info = jsonutils.dumps(source_connection_info)
        get_bdms.return_value = [bdm]
        self.compute.driver.get_volume_journal_phase.return_value = 'connected'
        get_migrations.return_value = [mock.Mock(
            uuid=migration_uuid, source_compute=source_host,
            dest_compute=self.compute.host, status='failed')]
        self.compute.driver.internal_migration_attach_disposition.\
            return_value = 'aborted'
        self.compute.driver.get_internal_volume_attach_connection_info.\
            return_value = dict(target_attachment['connection_info'])
        target_reads = [target_attachment]

        def attachment_get(unused_context, attachment_id):
            if attachment_id == source_attachment_id:
                return source_attachment
            if attachment_id == target_attachment_id and target_reads:
                return target_reads.pop()
            raise exception.VolumeAttachmentNotFound(
                attachment_id=attachment_id)

        self.compute.volume_api.attachment_get.side_effect = attachment_get
        self.compute.volume_api.get.return_value = self._cinder_volume(
            instance, volume_id, attachment_status='attached',
            attachment_id=source_attachment_id)

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='connected')

        self.compute.driver.rollback_internal_volume_attach.\
            assert_called_once_with(
                mock.ANY, instance, volume_id, mock.ANY,
                expected_mountpoint='/dev/vdb')
        self.compute.driver.finalize_rolled_back_volume_journal.\
            assert_called_once_with(instance, volume_id)
        self.compute.driver.cancel_managed_volume_attach.assert_called_once()
        self.compute.driver.confirm_connected_volume_journal.\
            assert_not_called()
        self.compute.volume_api.attachment_delete.assert_called_once_with(
            mock.ANY, target_attachment_id)
        bdm.destroy.assert_not_called()

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_committed_migration_target_retires_connected_owner(
            self, get_bdms, get_migrations):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        token = '60000000-0000-0000-0000-000000000006'
        migration_uuid = '70000000-0000-0000-0000-000000000007'
        bdm = self._configure_internal_volume_recovery(
            instance, volume_id, 'migration', token, 'live-target',
            operation_migration_uuid=migration_uuid)
        get_bdms.return_value = [bdm]
        self.compute.driver.get_volume_journal_phase.return_value = 'connected'
        get_migrations.return_value = [mock.Mock(
            uuid=migration_uuid, source_compute='incus-source',
            dest_compute=self.compute.host, status='completed')]
        self.compute.driver.internal_migration_attach_disposition.\
            return_value = 'committed'

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='connected')

        self.compute.driver.confirm_connected_volume_journal.\
            assert_called_once_with(
                instance, volume_id, mock.ANY,
                expected_mountpoint='/dev/vdb')
        self.compute.driver.rollback_internal_volume_attach.assert_not_called()
        self.compute.driver.cancel_managed_volume_attach.assert_called_once()

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_committed_cold_target_resumes_attached_connecting_owner(
            self, get_bdms, get_migrations):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        token = '60000000-0000-0000-0000-000000000006'
        migration_uuid = '70000000-0000-0000-0000-000000000007'
        bdm = self._configure_internal_volume_recovery(
            instance, volume_id, 'migration', token, 'cold-target',
            operation_migration_uuid=migration_uuid)
        get_bdms.return_value = [bdm]
        self.compute.driver.get_volume_journal_phase.return_value = (
            'connecting')
        self._configure_cinder_recovery_attachment(
            instance, volume_id, 'attached')
        get_migrations.return_value = [mock.Mock(
            uuid=migration_uuid, source_compute='incus-source',
            dest_compute=self.compute.host, status='finished')]
        self.compute.driver.internal_migration_attach_disposition.\
            return_value = 'committed'

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='connecting')

        self.compute.driver.resume_connecting_volume_journal.\
            assert_called_once_with(
                mock.ANY, instance, volume_id, mock.ANY,
                expected_mountpoint='/dev/vdb')
        self.compute.volume_api.attachment_complete.assert_not_called()
        self.compute.driver.confirm_connected_volume_journal.\
            assert_called_once()
        self.compute.driver.cancel_managed_volume_attach.assert_called_once()

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_live_source_release_disconnects_then_deletes_exact_attachment(
            self, get_bdms, get_migrations):
        instance = self._volume_recovery_instance()
        # Nova calls the source-volume cleanup hook before persisting the new
        # instance.host. The exact target BDM/attachment and absent source
        # container below are the post-commit authority in this window.
        instance.host = self.compute.host
        volume_id = '50000000-0000-0000-0000-000000000005'
        source_attachment_id = '40000000-0000-0000-0000-000000000004'
        target_attachment_id = '41000000-0000-0000-0000-000000000004'
        token = '60000000-0000-0000-0000-000000000006'
        migration_uuid = '70000000-0000-0000-0000-000000000007'
        source_attachment = self._cinder_attachment(
            instance, volume_id, 'attached')
        source_attachment['id'] = source_attachment_id
        target_attachment = self._cinder_attachment(
            instance, volume_id, 'attached')
        target_attachment['id'] = target_attachment_id
        target_info = dict(target_attachment['connection_info'])
        target_info['serial'] = volume_id
        bdm = self._volume_recovery_bdm(
            volume_id, connection_info=target_info)
        bdm.attachment_id = target_attachment_id
        get_bdms.return_value = [bdm]
        intent = {
            'attachment_id': source_attachment_id,
            'mountpoint': '/dev/vdb',
            'operation_kind': 'migration',
            'operation_token': token,
            'operation_direction': 'live-source-release',
            'operation_migration_uuid': migration_uuid,
        }
        self.compute.driver.get_managed_volume_attach_intent.return_value = (
            intent)
        get_migrations.return_value = [mock.Mock(
            uuid=migration_uuid, source_compute=self.compute.host,
            dest_compute='compute-2', status='running',
            migration_type='live-migration')]
        not_found = mock.Mock(status_code=404)
        not_found.json.return_value = {'error': 'not found'}
        self.compute.driver.client.instances.get.side_effect = (
            manager.incus_driver.incus_exceptions.LXDAPIException(not_found))
        source_info = dict(source_attachment['connection_info'])
        source_info['serial'] = volume_id
        self.compute.driver.get_internal_volume_attach_connection_info.\
            return_value = source_info
        phase_reads = [0]

        def journal_phase(unused_instance, unused_volume_id):
            phase_reads[0] += 1
            return 'disconnecting' if phase_reads[0] == 1 else 'disconnected'

        self.compute.driver.get_volume_journal_phase.side_effect = (
            journal_phase)
        self.compute.volume_api = mock.Mock()
        source_exists = [True]

        def attachment_get(unused_context, attachment_id):
            if attachment_id == source_attachment_id and source_exists[0]:
                return source_attachment
            if attachment_id == target_attachment_id:
                return target_attachment
            raise exception.VolumeAttachmentNotFound(
                attachment_id=attachment_id)

        delete_attempts = [0]

        def attachment_delete(unused_context, attachment_id):
            self.assertEqual(source_attachment_id, attachment_id)
            delete_attempts[0] += 1
            if delete_attempts[0] == 1:
                raise RuntimeError('transient Cinder failure')
            source_exists[0] = False

        self.compute.volume_api.attachment_get.side_effect = attachment_get
        self.compute.volume_api.attachment_delete.side_effect = (
            attachment_delete)
        target_volume = self._cinder_volume(
            instance, volume_id, attachment_status='attached',
            attachment_id=target_attachment_id)
        self.compute.volume_api.get.return_value = target_volume
        self.assertRaises(
            RuntimeError,
            self.compute._recover_incus_connecting_volume_journal,
            context.get_admin_context(), instance, volume_id,
            journal_phase='attach-disconnecting')
        self.compute.driver.finalize_disconnected_volume_journal.\
            assert_not_called()
        self.compute.driver.cancel_managed_volume_attach.assert_not_called()

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='attach-disconnected')

        self.compute.driver._recover_source_release_volume_journal_locked.\
            assert_called_once_with(
                mock.ANY, instance, volume_id, '/dev/vdb')
        self.assertEqual(2, delete_attempts[0])
        self.compute.driver.finalize_disconnected_volume_journal.\
            assert_called_once_with(instance, volume_id)
        self.compute.driver.cancel_managed_volume_attach.\
            assert_called_once_with(instance, volume_id, intent)
        bdm.destroy.assert_not_called()

    def test_attachment_identity_ignores_post_connect_lifecycle_fields(self):
        instance_uuid = '10000000-0000-0000-0000-000000000001'
        volume_id = '20000000-0000-0000-0000-000000000002'
        journal = {
            'serial': volume_id,
            'instance': instance_uuid,
            'driver_volume_type': 'rbd',
            'data': {
                'volume_id': volume_id,
                'name': 'pool/volume-' + volume_id,
                'hosts': ['192.0.2.10'],
            },
        }
        completed = copy.deepcopy(journal)
        completed['data']['attachment_id'] = (
            '30000000-0000-0000-0000-000000000003')
        completed['data']['qos_specs'] = None

        self.assertEqual(
            manager._canonical_attachment_connection_info(
                journal, volume_id, instance_uuid),
            manager._canonical_attachment_connection_info(
                completed, volume_id, instance_uuid))

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_live_source_release_retains_while_source_container_exists(
            self, get_bdms, get_migrations):
        instance = self._volume_recovery_instance()
        instance.host = 'compute-2'
        volume_id = '50000000-0000-0000-0000-000000000005'
        token = '60000000-0000-0000-0000-000000000006'
        migration_uuid = '70000000-0000-0000-0000-000000000007'
        self.compute.driver.get_managed_volume_attach_intent.return_value = {
            'attachment_id':
                '40000000-0000-0000-0000-000000000004',
            'mountpoint': '/dev/vdb',
            'operation_kind': 'migration',
            'operation_token': token,
            'operation_direction': 'live-source-release',
            'operation_migration_uuid': migration_uuid,
        }
        bdm = self._configure_internal_volume_recovery(
            instance, volume_id, 'migration', token,
            'live-source-release',
            operation_migration_uuid=migration_uuid)
        bdm.attachment_id = '41000000-0000-0000-0000-000000000004'
        get_bdms.return_value = [bdm]
        get_migrations.return_value = [mock.Mock(
            uuid=migration_uuid, source_compute=self.compute.host,
            dest_compute='compute-2', status='completed',
            migration_type='live-migration')]
        self.compute.driver.client.instances.get.return_value = mock.Mock(
            status='Stopped')

        self.assertRaises(
            exception.InvalidVolume,
            self.compute._recover_incus_connecting_volume_journal,
            context.get_admin_context(), instance, volume_id,
            journal_phase='connected')

        self.compute.driver._recover_source_release_volume_journal_locked.\
            assert_not_called()
        self.compute.volume_api.attachment_delete.assert_not_called()

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_bfv_source_release_retries_exact_attachment_without_os_brick(
            self, get_bdms, get_migrations):
        instance = self._volume_recovery_instance()
        instance.host = 'compute-2'
        volume_id = '51000000-0000-0000-0000-000000000005'
        source_attachment_id = '42000000-0000-0000-0000-000000000004'
        target_attachment_id = '43000000-0000-0000-0000-000000000004'
        token = '61000000-0000-0000-0000-000000000006'
        migration_uuid = '71000000-0000-0000-0000-000000000007'
        source_attachment = self._cinder_attachment(
            instance, volume_id, 'attached')
        source_attachment['id'] = source_attachment_id
        target_attachment = self._cinder_attachment(
            instance, volume_id, 'attached')
        target_attachment['id'] = target_attachment_id
        target_info = dict(target_attachment['connection_info'])
        target_info['serial'] = volume_id
        bdm = self._volume_recovery_bdm(
            volume_id, connection_info=target_info)
        bdm_info = jsonutils.loads(bdm.connection_info)
        bdm_info['data']['device_path'] = '/dev/source-host-rbd'
        bdm.connection_info = jsonutils.dumps(bdm_info)
        bdm.attachment_id = target_attachment_id
        bdm.device_name = '/dev/sda'
        get_bdms.return_value = [bdm]
        intent = {
            'attachment_id': source_attachment_id,
            'mountpoint': '/dev/sda',
            'operation_kind': 'migration',
            'operation_token': token,
            'operation_direction': 'live-source-release',
            'operation_migration_uuid': migration_uuid,
            'boot_volume': True,
        }
        self.compute.driver.get_managed_volume_attach_intent.return_value = (
            intent)
        get_migrations.return_value = [mock.Mock(
            uuid=migration_uuid, source_compute=self.compute.host,
            dest_compute='compute-2', status='completed',
            migration_type='live-migration')]
        not_found = mock.Mock(status_code=404)
        not_found.json.return_value = {'error': 'not found'}
        self.compute.driver.client.instances.get.side_effect = (
            manager.incus_driver.incus_exceptions.LXDAPIException(not_found))
        self.compute.driver.get_internal_volume_attach_connection_info.\
            return_value = None
        self.compute.driver.get_volume_journal_phase.return_value = None
        self.compute.volume_api = mock.Mock()
        source_exists = [True]

        def attachment_get(unused_context, attachment_id):
            if attachment_id == source_attachment_id and source_exists[0]:
                return source_attachment
            if attachment_id == target_attachment_id:
                return target_attachment
            raise exception.VolumeAttachmentNotFound(
                attachment_id=attachment_id)

        delete_attempts = [0]

        def attachment_delete(unused_context, attachment_id):
            self.assertEqual(source_attachment_id, attachment_id)
            delete_attempts[0] += 1
            if delete_attempts[0] == 1:
                raise RuntimeError('transient Cinder failure')
            source_exists[0] = False

        self.compute.volume_api.attachment_get.side_effect = attachment_get
        self.compute.volume_api.attachment_delete.side_effect = (
            attachment_delete)
        target_volume = self._cinder_volume(
            instance, volume_id, attachment_status='attached',
            attachment_id=target_attachment_id)
        target_volume['attachments'][instance.uuid]['mountpoint'] = '/dev/sda'
        self.compute.volume_api.get.return_value = target_volume

        self.assertRaises(
            RuntimeError,
            self.compute._recover_incus_connecting_volume_journal,
            context.get_admin_context(), instance, volume_id,
            journal_phase='attach-pending')
        self.compute.driver.cancel_managed_volume_attach.assert_not_called()

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='attach-pending')

        self.assertEqual(2, delete_attempts[0])
        self.compute.driver._recover_source_release_volume_journal_locked.\
            assert_not_called()
        self.compute.driver.finalize_disconnected_volume_journal.\
            assert_not_called()
        self.compute.driver.cancel_managed_volume_attach.\
            assert_called_once_with(instance, volume_id, intent)
        bdm.destroy.assert_not_called()

    def test_finished_cold_bfv_releases_only_rotated_source_attachment(self):
        instance = self._volume_recovery_instance()
        instance.host = 'compute-2'
        volume_id = '51000000-0000-0000-0000-000000000005'
        old_id = '42000000-0000-0000-0000-000000000004'
        new_id = '43000000-0000-0000-0000-000000000004'
        token = '61000000-0000-0000-0000-000000000006'
        old_attachment = self._rotation_attachment(
            instance, volume_id, old_id, status='attached')
        new_attachment = self._rotation_attachment(
            instance, volume_id, new_id, status='attached')
        target_info = dict(new_attachment['connection_info'])
        target_info['serial'] = volume_id
        bdm = self._volume_recovery_bdm(
            volume_id, connection_info=target_info)
        bdm.attachment_id = new_id
        bdm.device_name = '/dev/sda'
        intent = {
            'attachment_id': old_id,
            'mountpoint': '/dev/sda',
            'operation_kind': 'migration',
            'operation_token': token,
            'operation_direction': 'cold-source-restore',
            'operation_migration_uuid': token,
            'boot_volume': True,
        }
        rotation = {
            'old_attachment_id': old_id,
            'new_attachment_id': new_id,
            'mountpoint': '/dev/sda',
            'operation_token': token,
            'migration_uuid': token,
            'baseline_attachment_ids': [old_id],
            'phase': 'bdm-rotated',
            'boot_volume': True,
        }
        migration = mock.Mock(
            uuid=token, source_compute=self.compute.host,
            dest_compute='compute-2', status='finished',
            migration_type='migration')
        not_found = mock.Mock(status_code=404)
        not_found.json.return_value = {'error': 'not found'}
        self.compute.driver.client.instances.get.side_effect = (
            manager.incus_driver.incus_exceptions.LXDAPIException(not_found))
        self.compute.driver.get_cold_attachment_rotation.return_value = (
            rotation)
        self.compute.driver.get_internal_volume_attach_connection_info.\
            return_value = None
        self.compute.driver.get_volume_journal_phase.return_value = None
        self.compute.volume_api = mock.Mock()
        attachments = {old_id: old_attachment, new_id: new_attachment}

        def attachment_get(unused_context, attachment_id):
            if attachment_id not in attachments:
                raise exception.VolumeAttachmentNotFound(
                    attachment_id=attachment_id)
            return attachments[attachment_id]

        self.compute.volume_api.attachment_get.side_effect = attachment_get
        self.compute.volume_api.attachment_delete.side_effect = (
            lambda unused_context, attachment_id:
            attachments.pop(attachment_id))
        target_volume = self._cinder_volume(
            instance, volume_id, attachment_status='attached',
            attachment_id=new_id)
        target_volume['attachments'][instance.uuid]['mountpoint'] = '/dev/sda'
        self.compute.volume_api.get.return_value = target_volume
        terminal_rotation = dict(
            rotation, phase='source-release-complete')
        transition = self.compute.driver.transition_cold_attachment_rotation
        transition.return_value = terminal_rotation

        self.compute._recover_incus_migration_source_release_locked(
            context.get_admin_context(), instance, volume_id, 'attach-pending',
            intent, bdm, old_attachment, migration)

        self.assertNotIn(old_id, attachments)
        self.assertIn(new_id, attachments)
        self.compute.volume_api.attachment_delete.assert_called_once_with(
            mock.ANY, old_id)
        self.compute.driver.cancel_cold_attachment_rotation.\
            assert_called_once_with(instance, volume_id, terminal_rotation)
        self.compute.driver.transition_cold_attachment_rotation.\
            assert_called_once_with(
                instance, volume_id, rotation, 'source-release-complete')
        self.compute.driver.cancel_managed_volume_attach.\
            assert_called_once_with(instance, volume_id, intent)
        self.compute.driver._recover_source_release_volume_journal_locked.\
            assert_not_called()

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    def test_terminal_cold_release_retires_intent_before_rotation(
            self, get_migrations):
        instance = self._volume_recovery_instance()
        instance.host = 'compute-2'
        volume_id = '51000000-0000-0000-0000-000000000005'
        old_id = '42000000-0000-0000-0000-000000000004'
        new_id = '43000000-0000-0000-0000-000000000004'
        token = '61000000-0000-0000-0000-000000000006'
        rotation = {
            'old_attachment_id': old_id,
            'new_attachment_id': new_id,
            'mountpoint': '/dev/sda',
            'operation_token': token,
            'migration_uuid': token,
            'baseline_attachment_ids': [old_id],
            'phase': 'source-release-complete',
            'boot_volume': True,
        }
        intent = {
            'attachment_id': old_id,
            'mountpoint': '/dev/sda',
            'operation_kind': 'migration',
            'operation_token': token,
            'operation_direction': 'cold-source-restore',
            'operation_migration_uuid': token,
            'boot_volume': True,
        }
        target = self._rotation_attachment(
            instance, volume_id, new_id, status='attached')
        target_info = dict(target['connection_info'])
        target_info['serial'] = volume_id
        bdm = self._volume_recovery_bdm(
            volume_id, connection_info=target_info)
        bdm.attachment_id = new_id
        bdm.device_name = '/dev/sda'
        get_migrations.return_value = [mock.Mock(
            uuid=token, source_compute=self.compute.host,
            dest_compute='compute-2', status='finished')]
        not_found = mock.Mock(status_code=404)
        not_found.json.return_value = {'error': 'not found'}
        self.compute.driver.client.instances.get.side_effect = (
            manager.incus_driver.incus_exceptions.LXDAPIException(not_found))
        self.compute.driver.get_volume_journal_phase.return_value = None
        current_intent = [intent]
        self.compute.driver.get_managed_volume_attach_intent.side_effect = (
            lambda unused_instance, unused_volume: current_intent[0])
        self.compute.driver.cancel_managed_volume_attach.side_effect = (
            lambda *_args: current_intent.__setitem__(0, None))
        self.compute.volume_api = mock.Mock()

        def attachment_get(unused_context, attachment_id):
            if attachment_id == new_id:
                return target
            raise exception.VolumeAttachmentNotFound(
                attachment_id=attachment_id)

        self.compute.volume_api.attachment_get.side_effect = attachment_get
        self.compute.driver.cancel_cold_attachment_rotation.side_effect = [
            OSError('rotation fsync failed'), None]

        self.assertRaises(
            OSError,
            self.compute._retire_terminal_cold_attachment_rotation_locked,
            context.get_admin_context(), instance, volume_id, rotation, bdm)
        self.compute._retire_terminal_cold_attachment_rotation_locked(
            context.get_admin_context(), instance, volume_id, rotation, bdm)

        self.compute.driver.cancel_managed_volume_attach.\
            assert_called_once_with(instance, volume_id, intent)
        self.assertEqual(
            2, self.compute.driver.cancel_cold_attachment_rotation.call_count)

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    def test_terminal_cold_bfv_rollback_replays_without_os_brick(
            self, get_migrations):
        instance = self._volume_recovery_instance()
        volume_id = '51000000-0000-0000-0000-000000000005'
        old_id = '42000000-0000-0000-0000-000000000004'
        token = '61000000-0000-0000-0000-000000000006'
        rotation = {
            'old_attachment_id': old_id,
            'new_attachment_id': None,
            'mountpoint': '/dev/sda',
            'operation_token': token,
            'migration_uuid': token,
            'baseline_attachment_ids': [old_id],
            'phase': 'source-rollback-complete',
            'boot_volume': True,
        }
        intent = {
            'attachment_id': old_id,
            'mountpoint': '/dev/sda',
            'operation_kind': 'migration',
            'operation_token': token,
            'operation_direction': 'cold-source-restore',
            'operation_migration_uuid': token,
            'boot_volume': True,
        }
        source_attachment = self._rotation_attachment(
            instance, volume_id, old_id, status='attached')
        bdm = self._volume_recovery_bdm(volume_id)
        bdm.attachment_id = old_id
        bdm.device_name = '/dev/sda'
        get_migrations.return_value = [mock.Mock(
            uuid=token, source_compute=self.compute.host,
            dest_compute='compute-2', status='failed')]
        self.compute.driver.get_source_volume_generation_recovery_candidate.\
            return_value = {
                'operation_token': token, 'migration_uuid': token}
        self.compute.driver.get_internal_volume_attach_connection_info.\
            return_value = None
        self.compute.driver.get_volume_journal_phase.return_value = None
        current_intent = [intent]
        self.compute.driver.get_managed_volume_attach_intent.side_effect = (
            lambda unused_instance, unused_volume: current_intent[0])
        self.compute.driver.cancel_managed_volume_attach.side_effect = (
            lambda *_args: current_intent.__setitem__(0, None))
        self.compute.volume_api = mock.Mock()

        def attachment_get(unused_context, attachment_id):
            if attachment_id == old_id:
                return source_attachment
            raise exception.VolumeAttachmentNotFound(
                attachment_id=attachment_id)

        self.compute.volume_api.attachment_get.side_effect = attachment_get
        self.compute.driver.cancel_cold_attachment_rotation.side_effect = [
            OSError('rotation fsync failed'), None]

        self.assertRaises(
            OSError,
            self.compute._retire_terminal_cold_attachment_rotation_locked,
            context.get_admin_context(), instance, volume_id, rotation, bdm)
        self.compute._retire_terminal_cold_attachment_rotation_locked(
            context.get_admin_context(), instance, volume_id, rotation, bdm)

        self.compute.driver.restart_internal_volume_attach.assert_not_called()
        self.compute.driver.confirm_connected_volume_journal.\
            assert_not_called()
        self.compute.driver.cancel_managed_volume_attach.\
            assert_called_once_with(instance, volume_id, intent)

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_committed_cold_target_restarts_rolled_back_generation(
            self, get_bdms, get_migrations):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        token = '60000000-0000-0000-0000-000000000006'
        migration_uuid = '70000000-0000-0000-0000-000000000007'
        bdm = self._configure_internal_volume_recovery(
            instance, volume_id, 'migration', token, 'cold-target',
            operation_migration_uuid=migration_uuid)
        get_bdms.return_value = [bdm]
        self.compute.driver.get_volume_journal_phase.return_value = (
            'rolled-back')
        get_migrations.return_value = [mock.Mock(
            uuid=migration_uuid, source_compute='incus-source',
            dest_compute=self.compute.host, status='finished')]
        self.compute.driver.internal_migration_attach_disposition.\
            return_value = 'committed'

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='rolled-back')

        self.compute.driver.restart_internal_volume_attach.\
            assert_called_once_with(
                mock.ANY, instance, volume_id, mock.ANY,
                expected_mountpoint='/dev/vdb')
        self.compute.driver.rollback_internal_volume_attach.assert_not_called()
        self.compute.driver.cancel_managed_volume_attach.assert_called_once()

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_cold_revert_source_completes_attaching_generation(
            self, get_bdms, get_migrations):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        token = '60000000-0000-0000-0000-000000000006'
        migration_uuid = '70000000-0000-0000-0000-000000000007'
        bdm = self._configure_internal_volume_recovery(
            instance, volume_id, 'migration', token, 'cold-revert-source',
            operation_migration_uuid=migration_uuid)
        get_bdms.return_value = [bdm]
        attaching = self._cinder_attachment(
            instance, volume_id, 'attaching')
        attached = self._cinder_attachment(instance, volume_id, 'attached')
        reads = [attaching, attached]
        self.compute.volume_api.attachment_get.side_effect = (
            lambda unused_context, unused_attachment: reads.pop(0))
        self.compute.volume_api.get.return_value = self._cinder_volume(
            instance, volume_id, attachment_status='attached',
            attachment_id=bdm.attachment_id)
        get_migrations.return_value = [mock.Mock(
            uuid=migration_uuid, source_compute=self.compute.host,
            dest_compute='incus-target', status='reverting')]

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='connecting')

        self.compute.driver.resume_internal_volume_attach.\
            assert_called_once_with(
                mock.ANY, instance, volume_id, mock.ANY,
                expected_mountpoint='/dev/vdb')
        bdm.save.assert_called_once_with()
        self.compute.volume_api.attachment_complete.assert_called_once_with(
            mock.ANY, bdm.attachment_id)
        self.compute.driver.mark_source_volume_generation_rollback_complete.\
            assert_called_once_with(instance, token, migration_uuid)
        self.compute.driver.cancel_managed_volume_attach.assert_called_once()
        self.compute.driver.finalize_remote_source_volume_generation.\
            assert_called_once_with(instance, token)

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_failed_cold_source_restarts_disconnecting_generation(
            self, get_bdms, get_migrations):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        token = '60000000-0000-0000-0000-000000000006'
        bdm = self._configure_internal_volume_recovery(
            instance, volume_id, 'migration', token,
            'cold-source-restore', operation_migration_uuid=token)
        get_bdms.return_value = [bdm]
        self.compute.driver.get_volume_journal_phase.return_value = (
            'disconnecting')
        get_migrations.return_value = [mock.Mock(
            uuid=token, source_compute=self.compute.host,
            dest_compute='incus-target', status='failed')]

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='attach-disconnecting')

        self.compute.driver.restart_internal_volume_attach.\
            assert_called_once_with(
                mock.ANY, instance, volume_id, mock.ANY,
                expected_mountpoint='/dev/vdb')
        self.compute.driver.mark_source_volume_generation_rollback_complete.\
            assert_called_once_with(instance, token, token)
        self.compute.driver.cancel_managed_volume_attach.assert_called_once()
        self.compute.driver.finalize_failed_cold_source_volume_generation.\
            assert_called_once_with(instance, token)

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_failed_cold_source_restarts_disconnected_generation(
            self, get_bdms, get_migrations):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        token = '60000000-0000-0000-0000-000000000006'
        bdm = self._configure_internal_volume_recovery(
            instance, volume_id, 'migration', token,
            'cold-source-restore', operation_migration_uuid=token)
        get_bdms.return_value = [bdm]
        self.compute.driver.get_volume_journal_phase.return_value = (
            'disconnected')
        get_migrations.return_value = [mock.Mock(
            uuid=token, source_compute=self.compute.host,
            dest_compute='incus-target', status='error')]

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='attach-disconnected')

        self.compute.driver.restart_internal_volume_attach.\
            assert_called_once_with(
                mock.ANY, instance, volume_id, mock.ANY,
                expected_mountpoint='/dev/vdb')
        self.compute.driver.mark_source_volume_generation_rollback_complete.\
            assert_called_once_with(instance, token, token)
        self.compute.driver.cancel_managed_volume_attach.assert_called_once()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_failed_spawn_rolls_back_connecting_generation(self, get_bdms):
        instance = self._volume_recovery_instance()
        instance.vm_state = manager.vm_states.ERROR
        volume_id = '50000000-0000-0000-0000-000000000005'
        token = '60000000-0000-0000-0000-000000000006'
        bdm = self._configure_internal_volume_recovery(
            instance, volume_id, 'spawn', token, 'materialize')
        get_bdms.return_value = [bdm]
        self.compute.driver.get_volume_journal_phase.return_value = (
            'connecting')

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='connecting')

        self.compute.driver.rollback_internal_volume_attach.\
            assert_called_once_with(
                mock.ANY, instance, volume_id, mock.ANY,
                expected_mountpoint='/dev/vdb')
        self.compute.driver.finalize_rolled_back_volume_journal.\
            assert_called_once_with(instance, volume_id)
        self.compute.driver.cancel_managed_volume_attach.assert_called_once()
        self.compute.driver.finalize_spawn_volume_generation.\
            assert_called_once_with(instance, token)
        self.compute.driver.resume_internal_volume_attach.assert_not_called()
        self.compute.volume_api.attachment_complete.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_failed_spawn_rolls_back_connected_generation(self, get_bdms):
        instance = self._volume_recovery_instance()
        instance.vm_state = manager.vm_states.ERROR
        volume_id = '50000000-0000-0000-0000-000000000005'
        token = '60000000-0000-0000-0000-000000000006'
        bdm = self._configure_internal_volume_recovery(
            instance, volume_id, 'spawn', token, 'materialize')
        get_bdms.return_value = [bdm]
        self.compute.driver.get_volume_journal_phase.return_value = 'connected'

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='connected')

        self.compute.driver.rollback_internal_volume_attach.\
            assert_called_once_with(
                mock.ANY, instance, volume_id, mock.ANY,
                expected_mountpoint='/dev/vdb')
        self.compute.driver.finalize_rolled_back_volume_journal.\
            assert_called_once_with(instance, volume_id)
        self.compute.driver.cancel_managed_volume_attach.assert_called_once()
        self.compute.driver.finalize_spawn_volume_generation.\
            assert_called_once_with(instance, token)
        self.compute.driver.confirm_connected_volume_journal.\
            assert_not_called()
        self.compute.volume_api.attachment_complete.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_internal_attach_pending_retires_formal_owner(self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        token = '60000000-0000-0000-0000-000000000006'
        bdm = self._configure_internal_volume_recovery(
            instance, volume_id, 'reconcile', token, 'power-reconcile')
        get_bdms.return_value = [bdm]
        self.compute.driver.get_volume_journal_phase.return_value = None

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='attach-pending')

        self.compute.driver.confirm_connected_volume_journal.\
            assert_not_called()
        self.compute.driver.cancel_managed_volume_attach.assert_called_once()
        self.compute.driver.resume_internal_volume_attach.\
            assert_called_once_with(
                mock.ANY, instance, volume_id, mock.ANY,
                expected_mountpoint='/dev/vdb')
        self.compute.volume_api.attachment_complete.assert_not_called()

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_nonterminal_live_target_retires_formally_attached_intent(
            self, get_bdms, get_migrations):
        instance = self._volume_recovery_instance()
        source_host = 'incus-source'
        instance.host = source_host
        volume_id = '50000000-0000-0000-0000-000000000005'
        token = '60000000-0000-0000-0000-000000000006'
        migration_uuid = '70000000-0000-0000-0000-000000000007'
        bdm = self._configure_internal_volume_recovery(
            instance, volume_id, 'migration', token, 'live-target',
            operation_migration_uuid=migration_uuid)
        get_bdms.return_value = [bdm]
        self.compute.driver.get_volume_journal_phase.return_value = 'connected'
        get_migrations.return_value = [mock.Mock(
            uuid=migration_uuid, source_compute=source_host,
            dest_compute=self.compute.host, status='running')]
        self.compute.driver.internal_migration_attach_disposition.\
            return_value = 'active'

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='connected')

        self.compute.driver.confirm_connected_volume_journal.\
            assert_called_once()
        self.compute.driver.cancel_managed_volume_attach.assert_called_once()
        self.compute.driver.rollback_internal_volume_attach.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_formal_migration_commit_republishes_intent_after_fsync_error(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        token = '60000000-0000-0000-0000-000000000006'
        migration_uuid = '70000000-0000-0000-0000-000000000007'
        bdm = self._configure_internal_volume_recovery(
            instance, volume_id, 'migration', token, 'cold-target',
            operation_migration_uuid=migration_uuid)
        get_bdms.return_value = [bdm]
        intent = self.compute.driver.\
            get_managed_volume_attach_intent.return_value
        self.compute.driver.confirm_connected_volume_journal.side_effect = (
            OSError('fsync failed'))
        self.compute.driver.prepare_managed_volume_attach.return_value = intent

        self.compute._commit_formal_internal_volume_intents(
            context.get_admin_context(), instance, token, migration_uuid,
            'cold-target')

        self.compute.driver.prepare_managed_volume_attach.\
            assert_called_once_with(
                instance, volume_id, bdm.attachment_id, bdm.device_name,
                operation_kind='migration', operation_token=token,
                operation_direction='cold-target',
                operation_migration_uuid=migration_uuid,
                boot_volume=False)
        self.compute.driver.publish_migration_target_volumes_complete.\
            assert_called_once_with(instance, token, migration_uuid)

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_formal_cold_revert_bfv_retires_without_os_brick(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        instance.root_device_name = '/dev/sda'
        volume_id = '50000000-0000-0000-0000-000000000005'
        token = '60000000-0000-0000-0000-000000000006'
        migration_uuid = '70000000-0000-0000-0000-000000000007'
        bdm = self._configure_internal_volume_recovery(
            instance, volume_id, 'migration', token, 'cold-revert-source',
            operation_migration_uuid=migration_uuid)
        bdm.boot_index = None
        bdm.device_name = '/dev/sda'
        get_bdms.return_value = [bdm]
        intent = self.compute.driver.get_managed_volume_attach_intent.\
            return_value
        intent.update({'mountpoint': '/dev/sda', 'boot_volume': True})
        profile = mock.Mock(
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
            },
            devices={
                'root': {
                    'type': 'disk',
                    'path': '/',
                    'initial.ceph.rbd.image_name': 'volume-%s' % volume_id,
                },
            })
        self.compute.driver.client.profiles.get.side_effect = None
        self.compute.driver.client.profiles.get.return_value = profile
        self.compute.driver.get_volume_journal_phase.return_value = None
        self.compute.driver.get_internal_volume_attach_connection_info.\
            return_value = None

        self.compute._commit_formal_internal_volume_intents(
            context.get_admin_context(), instance, token, migration_uuid,
            'cold-revert-source')

        self.compute.driver.confirm_connected_volume_journal.\
            assert_not_called()
        self.compute.driver.cancel_managed_volume_attach.\
            assert_called_once_with(instance, volume_id, intent)

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    def test_periodic_cold_revert_bfv_retires_without_os_brick(
            self, get_migrations):
        instance = self._volume_recovery_instance()
        instance.root_device_name = '/dev/sda'
        volume_id = '50000000-0000-0000-0000-000000000005'
        token = '60000000-0000-0000-0000-000000000006'
        migration_uuid = '70000000-0000-0000-0000-000000000007'
        bdm = self._configure_internal_volume_recovery(
            instance, volume_id, 'migration', token, 'cold-revert-source',
            operation_migration_uuid=migration_uuid)
        bdm.boot_index = None
        bdm.device_name = '/dev/sda'
        intent = self.compute.driver.get_managed_volume_attach_intent.\
            return_value
        intent.update({'mountpoint': '/dev/sda', 'boot_volume': True})
        attachment = self._cinder_attachment(instance, volume_id, 'attached')
        connection_info = dict(attachment['connection_info'])
        connection_info['serial'] = volume_id
        profile = mock.Mock(
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
            },
            devices={
                'root': {
                    'type': 'disk',
                    'path': '/',
                    'initial.ceph.rbd.image_name': 'volume-%s' % volume_id,
                },
            })
        self.compute.driver.client.profiles.get.side_effect = None
        self.compute.driver.client.profiles.get.return_value = profile
        self.compute.driver.get_volume_journal_phase.return_value = None
        self.compute.driver.get_internal_volume_attach_connection_info.\
            return_value = None
        self.compute.driver.get_cold_attachment_rotation.return_value = None
        get_migrations.return_value = [mock.Mock(
            uuid=migration_uuid, source_compute=self.compute.host,
            dest_compute='compute-2', status='reverted')]

        self.compute._recover_incus_internal_attach_locked(
            context.get_admin_context(), instance, volume_id,
            'attach-pending', intent, bdm, attachment, 'attached',
            connection_info)

        self.compute.driver.resume_internal_volume_attach.assert_not_called()
        self.compute.driver.restart_internal_volume_attach.assert_not_called()
        self.compute.driver.confirm_connected_volume_journal.\
            assert_not_called()
        self.compute.driver.cancel_managed_volume_attach.\
            assert_called_once_with(instance, volume_id, intent)
        self.compute.driver.mark_source_volume_generation_rollback_complete.\
            assert_called_once_with(instance, token, migration_uuid)
        self.compute.driver.finalize_remote_source_volume_generation.\
            assert_called_once_with(instance, token)

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_failed_cold_target_keeps_formal_cinder_owner(
            self, get_bdms, get_migrations):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        token = '60000000-0000-0000-0000-000000000006'
        migration_uuid = '70000000-0000-0000-0000-000000000007'
        bdm = self._configure_internal_volume_recovery(
            instance, volume_id, 'migration', token, 'cold-target',
            operation_migration_uuid=migration_uuid)
        get_bdms.return_value = [bdm]
        self.compute.driver.get_volume_journal_phase.return_value = 'connected'
        target = self._cinder_attachment(instance, volume_id, 'attached')
        self.compute.driver.get_internal_volume_attach_connection_info.\
            return_value = dict(target['connection_info'])
        get_migrations.return_value = [mock.Mock(
            uuid=migration_uuid, source_compute='incus-source',
            dest_compute=self.compute.host, status='failed')]
        self.compute.driver.internal_migration_attach_disposition.\
            return_value = 'aborted'

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='connected')

        self.compute.driver.rollback_internal_volume_attach.\
            assert_called_once()
        self.compute.driver.finalize_rolled_back_volume_journal.\
            assert_called_once_with(instance, volume_id)
        self.compute.volume_api.attachment_delete.assert_not_called()
        bdm.destroy.assert_not_called()
        self.compute.driver.cancel_managed_volume_attach.assert_called_once()

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_active_migration_target_has_no_volume_side_effects(
            self, get_bdms, get_migrations):
        instance = self._volume_recovery_instance()
        instance.host = 'incus-source'
        volume_id = '50000000-0000-0000-0000-000000000005'
        token = '60000000-0000-0000-0000-000000000006'
        migration_uuid = '70000000-0000-0000-0000-000000000007'
        bdm = self._configure_internal_volume_recovery(
            instance, volume_id, 'migration', token, 'live-target',
            operation_migration_uuid=migration_uuid)
        get_bdms.return_value = [bdm]
        self._configure_cinder_recovery_attachment(
            instance, volume_id, 'attaching')
        get_migrations.return_value = [mock.Mock(
            uuid=migration_uuid, source_compute='incus-source',
            dest_compute=self.compute.host, status='running')]

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='connecting')

        self.compute.driver.internal_migration_attach_disposition.\
            assert_not_called()
        self.compute.driver.resume_internal_volume_attach.assert_not_called()
        self.compute.driver.rollback_internal_volume_attach.assert_not_called()
        self.compute.driver.cancel_managed_volume_attach.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_attached_recovery_rejects_bdm_connection_info_mismatch(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        attachment = self._cinder_attachment(instance, volume_id, 'attached')
        bdm_connection_info = dict(attachment['connection_info'])
        bdm_connection_info['data'] = dict(bdm_connection_info['data'])
        bdm_connection_info['data']['name'] = 'volumes/another-volume'
        bdm_connection_info['serial'] = volume_id
        bdm = self._volume_recovery_bdm(
            volume_id, connection_info=bdm_connection_info)
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        attachment = self._configure_cinder_recovery_attachment(
            instance, volume_id, 'attached')

        self.assertRaises(
            exception.InvalidVolume,
            self.compute._recover_incus_connecting_volume_journal,
            context.get_admin_context(), instance, volume_id)

        confirm = self.compute.driver.confirm_connected_volume_journal
        confirm.assert_not_called()
        rollback = self.compute.driver.rollback_connecting_volume_journal
        rollback.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_attaching_recovery_rejects_bdm_connection_info_mismatch(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        attachment = self._cinder_attachment(instance, volume_id, 'attaching')
        bdm_connection_info = dict(attachment['connection_info'])
        bdm_connection_info['data'] = dict(bdm_connection_info['data'])
        bdm_connection_info['data']['name'] = 'volumes/another-volume'
        bdm_connection_info['serial'] = volume_id
        bdm = self._volume_recovery_bdm(
            volume_id, connection_info=bdm_connection_info)
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        self._configure_cinder_recovery_attachment(
            instance, volume_id, 'attaching')

        self.assertRaises(
            exception.InvalidVolume,
            self.compute._recover_incus_connecting_volume_journal,
            context.get_admin_context(), instance, volume_id)

        resume = self.compute.driver.resume_connecting_volume_journal
        resume.assert_not_called()
        bdm.save.assert_not_called()
        self.compute.volume_api.attachment_complete.assert_not_called()
        confirm = self.compute.driver.confirm_connected_volume_journal
        confirm.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_attaching_recovery_rejects_bdm_without_target(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        bdm = self._volume_recovery_bdm(volume_id)
        bdm.device_name = None
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        self._configure_cinder_recovery_attachment(
            instance, volume_id, 'attaching')

        self.assertRaises(
            exception.InvalidVolume,
            self.compute._recover_incus_connecting_volume_journal,
            context.get_admin_context(), instance, volume_id)

        resume = self.compute.driver.resume_connecting_volume_journal
        resume.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_periodic_volume_recovery_rolls_back_error_attaching(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        bdm = self._volume_recovery_bdm(volume_id)
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        attachment = self._configure_cinder_recovery_attachment(
            instance, volume_id, 'error_attaching')

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id)

        rollback = self.compute.driver.rollback_connecting_volume_journal
        rollback.assert_called_once()
        self.compute.volume_api.attachment_delete.assert_called_once_with(
            mock.ANY, attachment['id'])
        bdm.destroy.assert_called_once_with()
        finalize = self.compute.driver.finalize_rolled_back_volume_journal
        finalize.assert_called_once()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_failed_attach_disconnect_journal_keeps_attach_generation(
            self, get_bdms):
        """Failed attach cleanup is not misclassified as managed detach."""
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        bdm = self._volume_recovery_bdm(volume_id)
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        attachment = self._configure_cinder_recovery_attachment(
            instance, volume_id, 'error_attaching')
        self.compute.driver.get_volume_journal_phase.return_value = (
            'disconnecting')

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='attach-disconnecting')

        self.compute.driver.rollback_connecting_volume_journal.\
            assert_called_once()
        self.compute.driver._recover_disconnecting_volume_journal_locked.\
            assert_not_called()
        self.compute.volume_api.attachment_delete.assert_called_once_with(
            mock.ANY, attachment['id'])
        bdm.destroy.assert_called_once_with()
        self.compute.driver.finalize_rolled_back_volume_journal.\
            assert_called_once()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid',
        return_value=[])
    def test_failed_attach_disconnected_without_bdm_finishes_rollback(
            self, get_bdms):
        """Nova may destroy the BDM after attachment_complete fails."""
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        attachment_id = (
            self.compute.driver.get_managed_volume_attach_intent.
            return_value['attachment_id'])
        self.compute.volume_api = mock.Mock()
        self.compute.volume_api.attachment_get.side_effect = (
            exception.VolumeAttachmentNotFound(
                attachment_id=attachment_id))
        self.compute.volume_api.get.return_value = self._cinder_volume(
            instance, volume_id)
        self.compute.driver.get_volume_journal_phase.return_value = (
            'disconnected')

        self.compute._recover_incus_connecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='attach-disconnected')

        self.compute.driver.rollback_connecting_volume_journal.\
            assert_called_once_with(
                mock.ANY, instance, volume_id, connection_info=None,
                expected_mountpoint='/dev/vdb')
        self.compute.volume_api.attachment_delete.assert_not_called()
        self.compute.driver.finalize_rolled_back_volume_journal.\
            assert_called_once_with(instance, volume_id)
        self.compute.driver.cancel_managed_volume_attach.assert_called_once()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_rolled_back_intent_rejects_new_same_volume_bdm(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        bdm = self._volume_recovery_bdm(volume_id)
        bdm.attachment_id = '70000000-0000-0000-0000-000000000007'
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        self.compute.driver.get_volume_journal_phase.return_value = (
            'rolled-back')

        self.assertRaises(
            exception.InvalidVolume,
            self.compute._recover_incus_connecting_volume_journal,
            context.get_admin_context(), instance, volume_id,
            journal_phase='rolled-back')

        self.compute.volume_api.attachment_get.assert_not_called()
        self.compute.driver.rollback_connecting_volume_journal.\
            assert_not_called()
        self.compute.volume_api.attachment_delete.assert_not_called()
        bdm.destroy.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_attach_recovery_rejects_stale_candidate_phase(self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        get_bdms.return_value = [self._volume_recovery_bdm(volume_id)]
        self.compute.driver.get_volume_journal_phase.return_value = 'connected'
        self.compute.volume_api = mock.Mock()

        self.assertRaises(
            exception.InvalidVolume,
            self.compute._recover_incus_connecting_volume_journal,
            context.get_admin_context(), instance, volume_id,
            journal_phase='rolled-back')

        get_bdms.assert_not_called()
        self.compute.volume_api.attachment_get.assert_not_called()
        self.compute.driver.rollback_connecting_volume_journal.\
            assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_volume_rollback_rejects_bdm_connection_info_mismatch(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        attachment = self._cinder_attachment(
            instance, volume_id, 'error_attaching')
        bdm_connection_info = dict(attachment['connection_info'])
        bdm_connection_info['data'] = dict(bdm_connection_info['data'])
        bdm_connection_info['data']['name'] = 'volumes/another-volume'
        bdm_connection_info['serial'] = volume_id
        bdm = self._volume_recovery_bdm(
            volume_id, connection_info=bdm_connection_info)
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        self._configure_cinder_recovery_attachment(
            instance, volume_id, 'error_attaching')

        self.assertRaises(
            exception.InvalidVolume,
            self.compute._recover_incus_connecting_volume_journal,
            context.get_admin_context(), instance, volume_id)

        rollback = self.compute.driver.rollback_connecting_volume_journal
        rollback.assert_not_called()
        self.compute.volume_api.attachment_delete.assert_not_called()
        bdm.destroy.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_volume_rollback_rejects_bdm_for_another_volume(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        bdm_connection_info = self._cinder_attachment(
            instance, volume_id, 'attaching')['connection_info']
        bdm_connection_info = dict(bdm_connection_info)
        bdm_connection_info['serial'] = (
            '60000000-0000-0000-0000-000000000006')
        bdm = self._volume_recovery_bdm(
            volume_id, connection_info=bdm_connection_info)
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        self.compute.volume_api.attachment_get.side_effect = (
            exception.VolumeAttachmentNotFound(
                attachment_id=bdm.attachment_id))
        self.compute.volume_api.get.return_value = self._cinder_volume(
            instance, volume_id)

        self.assertRaises(
            exception.InvalidVolume,
            self.compute._recover_incus_connecting_volume_journal,
            context.get_admin_context(), instance, volume_id)

        rollback = self.compute.driver.rollback_connecting_volume_journal
        rollback.assert_not_called()
        bdm.destroy.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid',
        return_value=[])
    def test_periodic_volume_recovery_retains_orphan_without_bdm(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        self.compute.volume_api = mock.Mock()
        self.assertRaises(
            exception.InvalidVolume,
            self.compute._recover_incus_connecting_volume_journal,
            context.get_admin_context(), instance, volume_id)

        rollback = self.compute.driver.rollback_connecting_volume_journal
        rollback.assert_not_called()
        self.compute.volume_api.attachment_delete.assert_not_called()
        finalize = self.compute.driver.finalize_rolled_back_volume_journal
        finalize.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid',
        return_value=[])
    def test_periodic_volume_recovery_fails_closed_without_attached_bdm(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        self.compute.volume_api = mock.Mock()
        self._configure_cinder_recovery_attachment(
            instance, volume_id, 'attached')

        self.assertRaises(
            exception.InvalidVolume,
            self.compute._recover_incus_connecting_volume_journal,
            context.get_admin_context(), instance, volume_id)

        confirm = self.compute.driver.confirm_connected_volume_journal
        confirm.assert_not_called()
        rollback = self.compute.driver.rollback_connecting_volume_journal
        rollback.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_periodic_volume_recovery_fails_closed_on_unknown_status(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        get_bdms.return_value = [self._volume_recovery_bdm(volume_id)]
        self.compute.volume_api = mock.Mock()
        self._configure_cinder_recovery_attachment(
            instance, volume_id, 'error_detaching')

        self.assertRaises(
            exception.InvalidVolume,
            self.compute._recover_incus_connecting_volume_journal,
            context.get_admin_context(), instance, volume_id)

        rollback = self.compute.driver.rollback_connecting_volume_journal
        rollback.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_periodic_volume_recovery_retries_after_attachment_complete_error(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        bdm = self._volume_recovery_bdm(volume_id)
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        self._configure_cinder_recovery_attachment(
            instance, volume_id, 'attaching')
        self.compute.volume_api.attachment_complete.side_effect = RuntimeError(
            'ambiguous Cinder reply')
        self.compute.driver.resume_connecting_volume_journal.return_value = (
            '/dev/vdb')

        self.assertRaises(
            RuntimeError,
            self.compute._recover_incus_connecting_volume_journal,
            context.get_admin_context(), instance, volume_id)

        bdm.save.assert_called_once_with()
        confirm = self.compute.driver.confirm_connected_volume_journal
        confirm.assert_not_called()
        self.compute.volume_api.attachment_delete.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_periodic_volume_rollback_keeps_journal_until_cinder_delete(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        bdm = self._volume_recovery_bdm(volume_id)
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        self._configure_cinder_recovery_attachment(
            instance, volume_id, 'error_attaching')
        self.compute.volume_api.attachment_delete.side_effect = RuntimeError(
            'Cinder unavailable')

        self.assertRaises(
            RuntimeError,
            self.compute._recover_incus_connecting_volume_journal,
            context.get_admin_context(), instance, volume_id)

        rollback = self.compute.driver.rollback_connecting_volume_journal
        rollback.assert_called_once()
        bdm.destroy.assert_not_called()
        finalize = self.compute.driver.finalize_rolled_back_volume_journal
        finalize.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_periodic_volume_recovery_retains_on_cinder_query_failure(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        get_bdms.return_value = [self._volume_recovery_bdm(volume_id)]
        self.compute.volume_api = mock.Mock()
        self.compute.volume_api.attachment_get.side_effect = RuntimeError(
            'Cinder unavailable')

        self.assertRaises(
            RuntimeError,
            self.compute._recover_incus_connecting_volume_journal,
            context.get_admin_context(), instance, volume_id)

        resume = self.compute.driver.resume_connecting_volume_journal
        resume.assert_not_called()
        rollback = self.compute.driver.rollback_connecting_volume_journal
        rollback.assert_not_called()
        confirm = self.compute.driver.confirm_connected_volume_journal
        confirm.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_volume_periodic_loads_instance_before_lock_when_migration_off(
            self, get_instance):
        self.flags(migration_auto_recovery=False, group='incus')
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        candidate = {
            'uuid': instance.uuid,
            'volume_ids': [volume_id],
            'phases': {volume_id: 'connecting'},
        }
        get_instance.side_effect = [instance, instance]
        list_candidates = (
            self.compute.driver.list_volume_journal_recovery_candidates)
        list_candidates.return_value = [candidate]

        with mock.patch.object(
                manager.incus_driver, '_volume_topology_lock_name',
                return_value='volume-topology') as lock_name, \
                mock.patch.object(
                    self.compute,
                    '_recover_incus_connecting_volume_journal') as recover:
            self.compute._recover_incus_volume_journals(
                context.get_admin_context())

        lock_name.assert_called_once_with(instance)
        recover.assert_called_once_with(
            mock.ANY, instance, volume_id, journal_phase='connecting')

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_volume_periodic_retains_deleted_instance_journal(
            self, get_instance):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        list_candidates = (
            self.compute.driver.list_volume_journal_recovery_candidates)
        list_candidates.return_value = [{
                'uuid': instance.uuid,
                'volume_ids': [volume_id],
                'phases': {volume_id: 'connecting'},
            }]
        get_instance.side_effect = exception.InstanceNotFound(
            instance_id=instance.uuid)

        with mock.patch.object(
                manager.incus_driver,
                '_volume_topology_lock_name') as lock_name:
            self.compute._recover_incus_volume_journals(
                context.get_admin_context())

        lock_name.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_volume_periodic_skips_stale_completed_candidate(
            self, get_instance):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        get_instance.side_effect = [instance, instance]
        self.compute.driver.list_volume_journal_recovery_candidates.\
            return_value = [{
                'uuid': instance.uuid,
                'volume_ids': [volume_id],
                'phases': {volume_id: 'connecting'},
            }]
        self.compute.driver.get_volume_journal_recovery_phase.return_value = (
            None)

        with mock.patch.object(
                self.compute,
                '_recover_incus_connecting_volume_journal') as attach, \
                mock.patch.object(
                    self.compute,
                    '_recover_incus_disconnecting_volume_journal') as detach:
            self.compute._recover_incus_volume_journals(
                context.get_admin_context())

        attach.assert_not_called()
        detach.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_volume_periodic_dispatches_locked_current_phase(
            self, get_instance):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        get_instance.side_effect = [instance, instance]
        self.compute.driver.list_volume_journal_recovery_candidates.\
            return_value = [{
                'uuid': instance.uuid,
                'volume_ids': [volume_id],
                'phases': {volume_id: 'connecting'},
            }]
        self.compute.driver.get_volume_journal_recovery_phase.return_value = (
            'attach-disconnected')

        with mock.patch.object(
                self.compute,
                '_recover_incus_connecting_volume_journal') as recover:
            self.compute._recover_incus_volume_journals(
                context.get_admin_context())

        recover.assert_called_once_with(
            mock.ANY, instance, volume_id,
            journal_phase='attach-disconnected')

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_volume_periodic_dispatches_terminal_rotation_without_intent(
            self, get_instance, get_bdms):
        instance = self._volume_recovery_instance()
        instance.task_state = task_states.RESIZE_MIGRATING
        volume_id = '50000000-0000-0000-0000-000000000005'
        rotation = {
            'phase': 'source-rollback-complete',
            'operation_token':
                '60000000-0000-0000-0000-000000000006',
        }
        bdm = self._volume_recovery_bdm(volume_id)
        get_instance.side_effect = [instance, instance]
        get_bdms.return_value = [bdm]
        self.compute.driver.get_managed_volume_attach_intent.return_value = (
            None)
        self.compute.driver.get_cold_attachment_rotation.return_value = (
            rotation)
        self.compute.driver.list_volume_journal_recovery_candidates.\
            return_value = [{
                'uuid': instance.uuid,
                'volume_ids': [volume_id],
                'phases': {
                    volume_id: 'rotation-source-rollback-complete'},
            }]
        self.compute.driver.get_volume_journal_recovery_phase.return_value = (
            'rotation-source-rollback-complete')

        with mock.patch.object(
                self.compute,
                '_retire_terminal_cold_attachment_rotation_locked') as retire:
            self.compute._recover_incus_volume_journals(
                context.get_admin_context())

        retire.assert_called_once_with(
            mock.ANY, instance, volume_id, rotation, bdm)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_volume_periodic_rejects_unknown_rotation_phase(
            self, get_instance):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        self.compute.driver.list_volume_journal_recovery_candidates.\
            return_value = [{
                'uuid': instance.uuid,
                'volume_ids': [volume_id],
                'phases': {volume_id: 'rotation-unknown'},
            }]

        self.compute._recover_incus_volume_journals(
            context.get_admin_context())

        get_instance.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_volume_periodic_dispatches_cross_host_source_release(
            self, get_instance):
        instance = self._volume_recovery_instance()
        instance.host = 'compute-2'
        volume_id = '50000000-0000-0000-0000-000000000005'
        token = '20000000-0000-0000-0000-000000000002'
        migration_uuid = '30000000-0000-0000-0000-000000000003'
        self.compute.driver.get_managed_volume_attach_intent.return_value = {
            'attachment_id':
                '40000000-0000-0000-0000-000000000004',
            'mountpoint': '/dev/vdb',
            'operation_kind': 'migration',
            'operation_token': token,
            'operation_direction': 'live-source-release',
            'operation_migration_uuid': migration_uuid,
        }
        get_instance.side_effect = [instance, instance]
        self.compute.driver.list_volume_journal_recovery_candidates.\
            return_value = [{
                'uuid': instance.uuid,
                'volume_ids': [volume_id],
                'phases': {volume_id: 'attach-disconnected'},
            }]
        self.compute.driver.get_volume_journal_recovery_phase.return_value = (
            'attach-disconnected')

        with mock.patch.object(
                self.compute,
                '_recover_incus_connecting_volume_journal') as recover:
            self.compute._recover_incus_volume_journals(
                context.get_admin_context())

        recover.assert_called_once_with(
            mock.ANY, instance, volume_id,
            journal_phase='attach-disconnected')

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_volume_recovery_rejects_attachment_without_status(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        get_bdms.return_value = [self._volume_recovery_bdm(volume_id)]
        self.compute.volume_api = mock.Mock()
        malformed = self._cinder_attachment(instance, volume_id, 'attaching')
        malformed['connection_info'].pop('status')
        self.compute.volume_api.attachment_get.return_value = malformed

        self.assertRaises(
            exception.InvalidVolume,
            self.compute._recover_incus_connecting_volume_journal,
            context.get_admin_context(), instance, volume_id)

        self.compute.volume_api.attachment_get.assert_called_once()
        rollback = self.compute.driver.rollback_connecting_volume_journal
        rollback.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_volume_recovery_rejects_attachment_without_id(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        get_bdms.return_value = [self._volume_recovery_bdm(volume_id)]
        self.compute.volume_api = mock.Mock()
        malformed = self._cinder_attachment(
            instance, volume_id, 'error_attaching')
        malformed['id'] = None
        self.compute.volume_api.attachment_get.return_value = malformed

        self.assertRaises(
            exception.InvalidVolume,
            self.compute._recover_incus_connecting_volume_journal,
            context.get_admin_context(), instance, volume_id)

        self.compute.volume_api.attachment_delete.assert_not_called()
        rollback = self.compute.driver.rollback_connecting_volume_journal
        rollback.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_volume_rollback_rejects_new_attachment_owner(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        get_bdms.return_value = [self._volume_recovery_bdm(volume_id)]
        self.compute.volume_api = mock.Mock()
        new_instance = mock.Mock(
            uuid='60000000-0000-0000-0000-000000000006')
        new_attachment_id = '70000000-0000-0000-0000-000000000007'
        old_attachment_id = get_bdms.return_value[0].attachment_id
        self.compute.volume_api.attachment_get.side_effect = (
            exception.VolumeAttachmentNotFound(
                attachment_id=old_attachment_id))
        self.compute.volume_api.get.return_value = self._cinder_volume(
            new_instance, volume_id, attachment_status='attached',
            attachment_id=new_attachment_id)

        self.assertRaises(
            exception.InvalidVolume,
            self.compute._recover_incus_connecting_volume_journal,
            context.get_admin_context(), instance, volume_id,
            journal_phase='connecting')

        rollback = self.compute.driver.rollback_connecting_volume_journal
        rollback.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_volume_rollback_rechecks_cinder_inside_volume_lock(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        bdm = self._volume_recovery_bdm(volume_id)
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        events = []

        @contextlib.contextmanager
        def tracked_lock(name, **kwargs):
            events.append(('enter', name))
            try:
                yield
            finally:
                events.append(('exit', name))

        def get_volume(_context, requested_volume_id):
            events.append(('get', requested_volume_id))
            return self._cinder_volume(instance, requested_volume_id)

        self.compute.volume_api.attachment_get.side_effect = (
            exception.VolumeAttachmentNotFound(
                attachment_id=bdm.attachment_id))
        self.compute.volume_api.get.side_effect = get_volume
        self.compute.driver.rollback_connecting_volume_journal.side_effect = (
            lambda *_args, **_kwargs: events.append(('rollback', volume_id)))

        with mock.patch.object(
                manager.lockutils, 'lock', side_effect=tracked_lock):
            self.compute._recover_incus_connecting_volume_journal(
                context.get_admin_context(), instance, volume_id,
                journal_phase='connecting')

        volume_lock = manager.incus_driver._volume_operation_lock_name(
            volume_id)
        self.assertEqual(('enter', volume_lock), events[0])
        self.assertLess(
            events.index(('get', volume_id)),
            events.index(('rollback', volume_id)))
        self.assertEqual(('exit', volume_lock), events[-1])

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_disconnecting_recovery_finishes_exact_managed_detach(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        attachment = self._cinder_attachment(instance, volume_id, 'attached')
        bdm_connection_info = dict(attachment['connection_info'])
        bdm_connection_info['serial'] = volume_id
        bdm = self._volume_recovery_bdm(
            volume_id, connection_info=bdm_connection_info)
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        attachment = self._configure_cinder_recovery_attachment(
            instance, volume_id, 'attached')
        self._configure_managed_detach(volume_id)

        self.compute._recover_incus_disconnecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='disconnecting')

        recover = (
            self.compute.driver._recover_disconnecting_volume_journal_locked)
        recover.assert_called_once_with(
            mock.ANY, instance, volume_id, bdm_connection_info,
            expected_mountpoint='/dev/vdb')
        self.compute.volume_api.attachment_delete.assert_called_once_with(
            mock.ANY, attachment['id'])
        bdm.destroy.assert_called_once_with()
        self.compute.driver.finalize_disconnected_volume_journal.\
            assert_called_once_with(instance, volume_id)
        self.compute.driver.cancel_managed_volume_detach.\
            assert_called_once()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_detach_pending_rolls_back_before_driver_intent(
            self, get_bdms):
        """A restart before driver entry restores the exact live attach."""
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        attachment = self._cinder_attachment(instance, volume_id, 'attached')
        bdm_connection_info = dict(attachment['connection_info'])
        bdm_connection_info['serial'] = volume_id
        bdm = self._volume_recovery_bdm(
            volume_id, connection_info=bdm_connection_info)
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        self.compute.volume_api.attachment_get.return_value = attachment
        self.compute.volume_api.get.return_value = self._cinder_volume(
            instance, volume_id, attachment_status='attached',
            attachment_id=attachment['id'], volume_status='detaching')
        self._configure_managed_detach(volume_id, phase=None)

        self.compute._recover_incus_disconnecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='detach-pending')

        self.compute.driver.confirm_connected_volume_journal.\
            assert_called_once_with(
                instance, volume_id, bdm_connection_info,
                expected_mountpoint='/dev/vdb')
        self.compute.volume_api.roll_detaching.assert_called_once_with(
            mock.ANY, volume_id)
        self.compute.driver.cancel_managed_volume_detach.\
            assert_called_once()
        self.compute.driver._recover_disconnecting_volume_journal_locked.\
            assert_not_called()
        self.compute.volume_api.attachment_delete.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid',
        return_value=[])
    def test_terminal_detach_intent_does_not_touch_new_owner(self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        intent = self._configure_managed_detach(volume_id, phase=None)
        self.compute.volume_api = mock.Mock()
        self.compute.volume_api.attachment_get.side_effect = (
            exception.VolumeAttachmentNotFound(
                attachment_id=intent['attachment_id']))
        new_instance = mock.Mock(
            uuid='60000000-0000-0000-0000-000000000006')
        self.compute.volume_api.get.return_value = self._cinder_volume(
            new_instance, volume_id, attachment_status='attached',
            attachment_id='70000000-0000-0000-0000-000000000007')

        self.compute._recover_incus_disconnecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='detach-pending')

        self.compute.driver.validate_disconnected_volume_state.\
            assert_called_once_with(instance, volume_id)
        self.compute.driver.cancel_managed_volume_detach.\
            assert_called_once()
        self.compute.driver._recover_disconnecting_volume_journal_locked.\
            assert_not_called()
        self.compute.volume_api.attachment_delete.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_detach_pending_retries_after_roll_detaching_committed(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        attachment = self._cinder_attachment(instance, volume_id, 'attached')
        bdm_connection_info = dict(attachment['connection_info'])
        bdm_connection_info['serial'] = volume_id
        get_bdms.return_value = [self._volume_recovery_bdm(
            volume_id, connection_info=bdm_connection_info)]
        self.compute.volume_api = mock.Mock()
        self.compute.volume_api.attachment_get.return_value = attachment
        self.compute.volume_api.get.return_value = self._cinder_volume(
            instance, volume_id, attachment_status='attached',
            attachment_id=attachment['id'], volume_status='in-use')
        self._configure_managed_detach(volume_id, phase=None)

        self.compute._recover_incus_disconnecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='detach-pending')

        self.compute.volume_api.roll_detaching.assert_not_called()
        self.compute.driver.cancel_managed_volume_detach.\
            assert_called_once()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid',
        return_value=[])
    def test_detach_intent_only_after_commit_is_retired(self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        intent = self._configure_managed_detach(volume_id, phase=None)
        self.compute.volume_api = mock.Mock()
        self.compute.volume_api.attachment_get.side_effect = (
            exception.VolumeAttachmentNotFound(
                attachment_id=intent['attachment_id']))
        self.compute.volume_api.get.return_value = self._cinder_volume(
            instance, volume_id)

        self.compute._recover_incus_disconnecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='detach-pending')

        self.compute.driver.cancel_managed_volume_detach.\
            assert_called_once()
        self.compute.driver.validate_disconnected_volume_state.\
            assert_called_once_with(instance, volume_id)
        self.compute.driver._recover_disconnecting_volume_journal_locked.\
            assert_not_called()
        self.compute.volume_api.attachment_delete.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_disconnecting_recovery_rejects_new_attachment_owner(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        attachment = self._cinder_attachment(instance, volume_id, 'attached')
        bdm_connection_info = dict(attachment['connection_info'])
        bdm_connection_info['serial'] = volume_id
        bdm = self._volume_recovery_bdm(
            volume_id, connection_info=bdm_connection_info)
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        self.compute.volume_api.attachment_get.return_value = attachment
        new_instance = mock.Mock(
            uuid='60000000-0000-0000-0000-000000000006')
        self.compute.volume_api.get.return_value = self._cinder_volume(
            new_instance, volume_id, attachment_status='attached',
            attachment_id='70000000-0000-0000-0000-000000000007')
        self._configure_managed_detach(volume_id)

        self.assertRaises(
            exception.InvalidVolume,
            self.compute._recover_incus_disconnecting_volume_journal,
            context.get_admin_context(), instance, volume_id,
            journal_phase='disconnecting')

        recover = (
            self.compute.driver._recover_disconnecting_volume_journal_locked)
        recover.assert_not_called()
        self.compute.volume_api.attachment_delete.assert_not_called()
        bdm.destroy.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_disconnected_recovery_never_repeats_host_disconnect(
            self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        attachment = self._cinder_attachment(instance, volume_id, 'detached')
        bdm_connection_info = dict(attachment['connection_info'])
        bdm_connection_info['serial'] = volume_id
        bdm = self._volume_recovery_bdm(
            volume_id, connection_info=bdm_connection_info)
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        self.compute.volume_api.attachment_get.return_value = attachment
        self._configure_managed_detach(volume_id, phase='disconnected')

        self.compute._recover_incus_disconnecting_volume_journal(
            context.get_admin_context(), instance, volume_id,
            journal_phase='disconnected')

        self.compute.driver._recover_disconnecting_volume_journal_locked.\
            assert_not_called()
        self.compute.volume_api.attachment_delete.assert_called_once_with(
            mock.ANY, attachment['id'])
        bdm.destroy.assert_called_once_with()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_disconnecting_destroy_bdm_false_fails_closed(self, get_bdms):
        instance = self._volume_recovery_instance()
        volume_id = '50000000-0000-0000-0000-000000000005'
        bdm = self._volume_recovery_bdm(volume_id)
        get_bdms.return_value = [bdm]
        self.compute.volume_api = mock.Mock()
        self._configure_managed_detach(
            volume_id, destroy_bdm=False)

        self.assertRaises(
            exception.InvalidVolume,
            self.compute._recover_incus_disconnecting_volume_journal,
            context.get_admin_context(), instance, volume_id,
            journal_phase='disconnecting')

        self.compute.volume_api.attachment_get.assert_not_called()
        self.compute.driver._recover_disconnecting_volume_journal_locked.\
            assert_not_called()
        bdm.destroy.assert_not_called()

    def test_complete_live_migration_rollback_reasserts_network(self):
        ctxt = context.get_admin_context()
        instance = mock.sentinel.instance
        data = migrate_data.IncusLiveMigrateData()

        result = self.compute._complete_live_migration_rollback(
            ctxt, instance, data)

        self.assertIsNone(result)
        finalize = self.compute.driver.finalize_live_migration_rollback
        finalize.assert_called_once_with(ctxt, instance, data)

    def test_complete_live_rollback_retires_source_volume_generation(self):
        ctxt = context.get_admin_context()
        instance = mock.Mock(host=self.compute.host)
        token = '10000000-0000-0000-0000-000000000001'
        migration_uuid = '20000000-0000-0000-0000-000000000002'
        data = migrate_data.IncusLiveMigrateData(
            cleanup_token=token, migration_uuid=migration_uuid)

        self.compute._complete_live_migration_rollback(
            ctxt, instance, data, migration_status='failed')

        self.compute.driver.finalize_live_migration_rollback.\
            assert_called_once_with(ctxt, instance, data)
        self.compute.driver.finalize_remote_source_volume_generation.\
            assert_called_once_with(
                instance, token)

    @mock.patch.object(
        manager.manager.ComputeManager, 'finish_revert_resize',
        return_value=mock.sentinel.result)
    def test_finish_cold_revert_retires_source_volume_generation(
            self, base_finish):
        ctxt = context.get_admin_context()
        token = '10000000-0000-0000-0000-000000000001'
        migration_uuid = '20000000-0000-0000-0000-000000000002'
        instance = mock.Mock(host=self.compute.host)
        instance.name = 'instance-00000001'
        migration = mock.Mock(uuid=migration_uuid, status='reverted')
        self.compute.driver.client.profiles.get.side_effect = None
        self.compute.driver.client.profiles.get.return_value = mock.Mock(
            config={manager.incus_driver.MIGRATION_CLEANUP_TOKEN_KEY: token})
        self.compute._commit_formal_internal_volume_intents = mock.Mock()

        result = self.compute._finish_revert_resize_and_commit_volumes(
            ctxt, instance, migration, mock.sentinel.request_spec)

        self.assertIs(mock.sentinel.result, result)
        base_finish.assert_called_once_with(
            ctxt, instance, migration, mock.sentinel.request_spec)
        self.compute._commit_formal_internal_volume_intents.\
            assert_called_once_with(
                ctxt, instance, token, migration_uuid,
                'cold-revert-source')
        self.compute.driver.finalize_remote_source_volume_generation.\
            assert_called_once_with(
                instance, token)

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_cold_revert_handoffs_retained_source_rotation(self, get_bdms):
        ctxt = context.get_admin_context()
        instance = self._volume_recovery_instance()
        instance.host = 'compute-2'
        instance.task_state = manager.task_states.RESIZE_REVERTING
        volume_id = '50000000-0000-0000-0000-000000000005'
        old_id = '51000000-0000-0000-0000-000000000005'
        target_id = '52000000-0000-0000-0000-000000000005'
        source_id = '53000000-0000-0000-0000-000000000005'
        token = '54000000-0000-0000-0000-000000000005'
        bdm = self._volume_recovery_bdm(volume_id)
        bdm.attachment_id = source_id
        bdm.device_name = '/dev/sdb'
        get_bdms.return_value = [bdm]
        migration = mock.Mock(
            uuid=token, source_compute=self.compute.host,
            dest_compute='compute-2')
        intent = {
            'attachment_id': old_id,
            'mountpoint': '/dev/sdb',
            'operation_kind': 'migration',
            'operation_token': token,
            'operation_direction': 'cold-source-restore',
            'operation_migration_uuid': token,
            'boot_volume': False,
        }
        rotation = {
            'old_attachment_id': old_id,
            'new_attachment_id': target_id,
            'mountpoint': '/dev/sdb',
            'operation_token': token,
            'migration_uuid': token,
            'phase': 'bdm-rotated',
            'boot_volume': False,
        }
        self.compute.driver.get_managed_volume_attach_intent.return_value = (
            intent)
        self.compute.driver.get_cold_attachment_rotation.return_value = (
            rotation)
        source_attachment = self._rotation_attachment(
            instance, volume_id, source_id, status='attaching')
        self.compute._get_exact_cinder_attachment = mock.Mock(
            side_effect=lambda unused_context, attachment_id,
            unused_volume, unused_instance: (
                source_attachment if attachment_id == source_id else None))
        self.compute.driver.get_volume_journal_phase.return_value = (
            'disconnected')
        replacement = dict(
            intent, attachment_id=source_id,
            operation_direction='cold-revert-source')
        self.compute.driver.replace_cold_source_volume_attach_intent.\
            return_value = replacement

        self.compute._handoff_cold_source_rotations_for_revert(
            ctxt, instance, migration)

        self.compute.driver.replace_cold_source_volume_attach_intent.\
            assert_called_once_with(
                instance, volume_id, intent, source_id,
                operation_direction='cold-revert-source')
        self.compute.driver.cancel_cold_attachment_rotation.\
            assert_called_once_with(instance, volume_id, rotation)

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_cold_revert_handoff_rejects_live_target_owner(self, get_bdms):
        ctxt = context.get_admin_context()
        instance = self._volume_recovery_instance()
        instance.host = 'compute-2'
        instance.task_state = manager.task_states.RESIZE_REVERTING
        volume_id = '50000000-0000-0000-0000-000000000005'
        old_id = '51000000-0000-0000-0000-000000000005'
        target_id = '52000000-0000-0000-0000-000000000005'
        source_id = '53000000-0000-0000-0000-000000000005'
        token = '54000000-0000-0000-0000-000000000005'
        bdm = self._volume_recovery_bdm(volume_id)
        bdm.attachment_id = source_id
        bdm.device_name = '/dev/sdb'
        get_bdms.return_value = [bdm]
        migration = mock.Mock(
            uuid=token, source_compute=self.compute.host,
            dest_compute='compute-2')
        intent = {
            'attachment_id': old_id,
            'mountpoint': '/dev/sdb',
            'operation_kind': 'migration',
            'operation_token': token,
            'operation_direction': 'cold-source-restore',
            'operation_migration_uuid': token,
            'boot_volume': False,
        }
        rotation = {
            'old_attachment_id': old_id,
            'new_attachment_id': target_id,
            'mountpoint': '/dev/sdb',
            'operation_token': token,
            'migration_uuid': token,
            'phase': 'bdm-rotated',
            'boot_volume': False,
        }
        self.compute.driver.get_managed_volume_attach_intent.return_value = (
            intent)
        self.compute.driver.get_cold_attachment_rotation.return_value = (
            rotation)
        target = self._rotation_attachment(
            instance, volume_id, target_id, status='attached')
        self.compute._get_exact_cinder_attachment = mock.Mock(
            side_effect=lambda unused_context, attachment_id,
            unused_volume, unused_instance: (
                target if attachment_id == target_id else None))

        self.assertRaises(
            exception.InvalidVolume,
            self.compute._handoff_cold_source_rotations_for_revert,
            ctxt, instance, migration)

        self.compute.driver.replace_cold_source_volume_attach_intent.\
            assert_not_called()
        self.compute.driver.cancel_cold_attachment_rotation.\
            assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_cold_revert_handoff_accepts_completed_retry(self, get_bdms):
        ctxt = context.get_admin_context()
        instance = self._volume_recovery_instance()
        instance.host = 'compute-2'
        instance.task_state = manager.task_states.RESIZE_REVERTING
        volume_id = '50000000-0000-0000-0000-000000000005'
        source_id = '53000000-0000-0000-0000-000000000005'
        token = '54000000-0000-0000-0000-000000000005'
        bdm = self._volume_recovery_bdm(volume_id)
        bdm.attachment_id = source_id
        bdm.device_name = '/dev/sdb'
        bdm.boot_index = None
        get_bdms.return_value = [bdm]
        migration = mock.Mock(
            uuid=token, source_compute=self.compute.host,
            dest_compute='compute-2')
        intent = {
            'attachment_id': source_id,
            'mountpoint': '/dev/sdb',
            'operation_kind': 'migration',
            'operation_token': token,
            'operation_direction': 'cold-revert-source',
            'operation_migration_uuid': token,
            'boot_volume': False,
        }
        self.compute.driver.get_managed_volume_attach_intent.return_value = (
            intent)
        self.compute.driver.get_cold_attachment_rotation.return_value = None
        self.compute._get_exact_cinder_attachment = mock.Mock(
            return_value=self._rotation_attachment(
                instance, volume_id, source_id, status='attaching'))
        self.compute.driver.get_volume_journal_phase.return_value = (
            'disconnected')
        self.compute.driver.get_internal_volume_attach_connection_info.\
            return_value = None

        self.compute._handoff_cold_source_rotations_for_revert(
            ctxt, instance, migration)

        self.compute.driver.replace_cold_source_volume_attach_intent.\
            assert_not_called()
        self.compute.driver.cancel_cold_attachment_rotation.\
            assert_not_called()

    def test_pre_live_migration_rollback_retires_source_generation(self):
        data = migrate_data.IncusLiveMigrateData()
        instance = mock.Mock(host=self.compute.host)

        self.compute._complete_live_migration_rollback(
            mock.sentinel.context, instance, data,
            pre_live_migration=True)

        finalize = self.compute.driver.finalize_live_migration_rollback
        finalize.assert_not_called()
        retire = self.compute.driver.finalize_pre_live_migration_rollback
        retire.assert_called_once_with(instance, data)

    def test_live_migration_check_data_uses_exact_nova_migration_uuid(self):
        token = '10000000-0000-0000-0000-000000000001'
        data = migrate_data.IncusLiveMigrateData(
            cleanup_token='20000000-0000-0000-0000-000000000002')
        migration = mock.Mock(uuid=token)

        result = self.compute._prepare_live_migration_check_data(
            mock.sentinel.context, mock.sentinel.instance, data, migration)

        self.assertIs(data, result)
        self.assertEqual(token, data.migration_uuid)
        self.assertEqual(
            '20000000-0000-0000-0000-000000000002', data.cleanup_token)

    def test_live_migration_check_data_rejects_missing_migration_uuid(self):
        data = migrate_data.IncusLiveMigrateData(
            cleanup_token='20000000-0000-0000-0000-000000000002')

        self.assertRaises(
            manager.exception.MigrationPreCheckError,
            self.compute._prepare_live_migration_check_data,
            mock.sentinel.context, mock.sentinel.instance, data,
            mock.Mock(uuid=None))

    def _assert_unsupported_action_reverts_task(
            self, method, expected_exception, instance, *args):
        self.compute._instance_update = mock.Mock()
        ctxt = context.get_admin_context()

        with mock.patch.object(
                manager.manager.compute_utils,
                'add_instance_fault_from_exc') as add_fault, \
                mock.patch.object(
                    manager.manager.compute_utils.objects.InstanceActionEvent,
                    'event_start') as event_start, \
                mock.patch.object(
                    manager.manager.compute_utils.objects.InstanceActionEvent,
                    'event_finish_with_failure') as event_failure:
            self.assertRaises(
                expected_exception, method, ctxt, instance, *args)

        self.compute._instance_update.assert_called_once_with(
            ctxt, instance, task_state=None)
        add_fault.assert_called_once()
        event_start.assert_called_once()
        event_failure.assert_called_once()
        self.assertNotEqual('error', instance.vm_state)

    def test_suspend_rejected_without_changing_vm_state(self):
        instance = mock.MagicMock(
            task_state=task_states.SUSPENDING, vm_state='active')
        instance.__getitem__.return_value = (
            '8cf7d85e-6c3f-4ebf-9ed7-5c4b64f928f8')

        self._assert_unsupported_action_reverts_task(
            self.compute.suspend_instance,
            manager.exception.InstanceSuspendFailure, instance)

    def test_resume_rejected_without_changing_vm_state(self):
        instance = mock.MagicMock(
            task_state=task_states.RESUMING, vm_state='suspended')
        instance.__getitem__.return_value = (
            '8cf7d85e-6c3f-4ebf-9ed7-5c4b64f928f8')

        self._assert_unsupported_action_reverts_task(
            self.compute.resume_instance,
            manager.exception.InstanceResumeFailure, instance)

    def test_rescue_rejected_without_changing_vm_state(self):
        instance = mock.MagicMock(
            task_state=task_states.RESCUING, vm_state='active',
            uuid='8cf7d85e-6c3f-4ebf-9ed7-5c4b64f928f8')
        instance.__getitem__.return_value = instance.uuid

        self._assert_unsupported_action_reverts_task(
            self.compute.rescue_instance,
            manager.exception.InstanceNotRescuable, instance,
            'password', None, True)

    def test_unrescue_rejected_without_changing_vm_state(self):
        instance = mock.MagicMock(
            task_state=task_states.UNRESCUING, vm_state='rescued')
        instance.__getitem__.return_value = (
            '8cf7d85e-6c3f-4ebf-9ed7-5c4b64f928f8')

        self._assert_unsupported_action_reverts_task(
            self.compute.unrescue_instance,
            manager.exception.InstanceUnRescueFailure, instance)

    def test_live_migration_hydrates_every_share_before_first_mount(self):
        ctxt = context.get_admin_context()
        instance = mock.MagicMock(
            uuid='00000000-0000-0000-0000-000000000001')
        instance.__getitem__.side_effect = (
            lambda key: getattr(instance, key))
        nfs = mock.Mock(share_id='share-1', status='active',
                        share_proto='NFS')
        cephfs = mock.Mock(share_id='share-2', status='active',
                           share_proto='CEPHFS')
        self.compute._get_share_info = mock.Mock(return_value=[nfs, cephfs])
        events = mock.Mock()
        events.attach_mock(nfs.set_access_according_to_protocol, 'nfs_access')
        events.attach_mock(
            cephfs.set_access_according_to_protocol, 'ceph_access')
        events.attach_mock(
            cephfs.enhance_with_ceph_credentials, 'ceph_credentials')
        events.attach_mock(
            self.compute.driver.stage_share_for_live_migration, 'stage')
        data = migrate_data.IncusLiveMigrateData(
            full_checkpoint_verified=True,
            cleanup_token='10000000-0000-0000-0000-000000000001')

        with mock.patch.object(
                manager.manager.safe_utils, 'get_wrapped_function',
                return_value=mock.Mock()):
            self.compute._pre_live_migration_locked(
                ctxt, instance, mock.sentinel.disk, data)

        self.assertEqual([
            mock.call.nfs_access(),
            mock.call.ceph_access(),
            mock.call.ceph_credentials(ctxt),
        ], events.mock_calls[:3])
        self.assertEqual(2, events.stage.call_count)

    def test_live_hydration_failure_has_no_mount_side_effect(self):
        ctxt = context.get_admin_context()
        instance = mock.MagicMock(
            uuid='00000000-0000-0000-0000-000000000001')
        instance.__getitem__.side_effect = (
            lambda key: getattr(instance, key))
        first = mock.Mock(share_id='share-1', status='active',
                          share_proto='NFS')
        second = mock.Mock(share_id='share-2', status='active',
                           share_proto='CEPHFS')
        second.enhance_with_ceph_credentials.side_effect = RuntimeError(
            'Manila unavailable')
        self.compute._get_share_info = mock.Mock(
            return_value=[first, second])
        data = migrate_data.IncusLiveMigrateData(
            full_checkpoint_verified=True,
            cleanup_token='10000000-0000-0000-0000-000000000001')

        self.assertRaises(
            RuntimeError, self.compute._pre_live_migration_locked,
            ctxt, instance, mock.sentinel.disk, data)

        self.compute.driver.get_share_mount_table.assert_not_called()
        self.compute.driver.stage_share_for_live_migration.assert_not_called()

    def test_cold_hydration_failure_has_no_mount_side_effect(self):
        ctxt = context.get_admin_context()
        instance = mock.Mock(uuid='instance')
        share = mock.Mock(
            share_id='share-1', status='active', share_proto='CEPHFS')
        share.enhance_with_ceph_credentials.side_effect = RuntimeError(
            'credential lookup failed')
        self.compute._get_share_info = mock.Mock(return_value=[share])
        rollback = self.compute.driver.rollback_cold_migration_preparation
        rollback.return_value = False

        self.assertRaises(
            RuntimeError, self.compute._finish_resize_helper,
            ctxt, 'disk', mock.sentinel.image, instance,
            mock.sentinel.migration, mock.sentinel.request_spec)

        self.compute.driver.stage_share_for_cold_migration.assert_not_called()

    def test_mount_all_shares_rolls_back_changed_items_in_reverse(self):
        ctxt = context.get_admin_context()
        instance = mock.Mock(uuid='instance')
        shares = [
            mock.Mock(share_id='share-%d' % index, share_proto='NFS')
            for index in range(3)
        ]
        failure = RuntimeError('third mount failed')
        self.compute.driver.mount_share_transaction.side_effect = [
            True, True, failure]
        self.compute._set_share_mapping_and_instance_in_error = mock.Mock()

        raised = self.assertRaises(
            RuntimeError, self.compute._mount_all_shares,
            ctxt, instance, shares)

        self.assertIs(failure, raised)
        self.assertEqual([
            mock.call(
                ctxt, instance, shares[1], mount_table=self.mount_table),
            mock.call(
                ctxt, instance, shares[0], mount_table=self.mount_table),
        ], self.compute.driver.umount_share_transaction.call_args_list)

    def test_umount_all_shares_attempts_all_and_aggregates(self):
        ctxt = context.get_admin_context()
        instance = mock.Mock(uuid='instance')
        shares = [
            mock.Mock(share_id='share-%d' % index, share_proto='NFS')
            for index in range(3)
        ]
        self.compute.driver.umount_share_transaction.side_effect = [
            RuntimeError('first'), None, RuntimeError('third')]
        self.compute._set_share_mapping_status = mock.Mock()
        self.compute._set_instance_obj_error_state = mock.Mock()

        self.assertRaises(
            manager.exception.ShareUmountError,
            self.compute._umount_all_shares, ctxt, instance, shares)

        self.assertEqual(
            3, self.compute.driver.umount_share_transaction.call_count)
        self.assertEqual(
            [shares[0], shares[2]],
            [call.args[0] for call in
             self.compute._set_share_mapping_status.call_args_list])
        for share in shares:
            share.set_access_according_to_protocol.assert_called_once_with()

    def test_umount_all_shares_hydrates_cephfs_before_driver(self):
        ctxt = context.get_admin_context()
        instance = mock.Mock(uuid='instance')
        share = mock.Mock(share_id='share', share_proto='CEPHFS')

        self.compute._umount_all_shares(ctxt, instance, [share])

        share.set_access_according_to_protocol.assert_called_once_with()
        share.enhance_with_ceph_credentials.assert_called_once_with(ctxt)
        self.compute.driver.umount_share_transaction.assert_called_once_with(
            ctxt, instance, share, mount_table=self.mount_table)

    def test_mount_table_is_built_once_for_32_and_64_shares(self):
        ctxt = context.get_admin_context()
        instance = mock.Mock(uuid='instance')
        self.compute.driver.mount_share_transaction.return_value = False
        for cardinality in (32, 64):
            with self.subTest(cardinality=cardinality):
                self.compute.driver.reset_mock()
                self.compute.driver.get_share_mount_table.return_value = (
                    self.mount_table)
                shares = [
                    mock.Mock(
                        share_id='share-%d' % index, share_proto='NFS')
                    for index in range(cardinality)
                ]

                self.compute._mount_all_shares(ctxt, instance, shares)

                self.compute.driver.get_share_mount_table.assert_called_once()
                self.assertEqual(
                    cardinality,
                    self.compute.driver.mount_share_transaction.call_count)

    def test_pre_deny_share_hydrates_before_unmount(self):
        ctxt = context.get_admin_context()
        instance = mock.Mock(uuid='instance')
        share = mock.Mock(share_id='share', share_proto='CEPHFS')

        result = self.compute._pre_deny_share(ctxt, instance, share)

        self.assertIsNone(result)
        share.set_access_according_to_protocol.assert_called_once_with()
        share.enhance_with_ceph_credentials.assert_called_once_with(ctxt)
        self.compute.driver.umount_share_transaction.assert_called_once_with(
            ctxt, instance, share, mount_table=self.mount_table)

    def test_pre_deny_share_marks_mapping_when_unmount_fails(self):
        ctxt = context.get_admin_context()
        instance = mock.Mock(uuid='instance')
        share = mock.Mock(share_id='share')
        self.compute.driver.umount_share_transaction.side_effect = (
            manager.exception.ShareUmountError(
                share_id=share.share_id, server_id=instance.uuid,
                reason='busy'))
        self.compute._set_share_mapping_and_instance_in_error = mock.Mock()

        self.assertRaises(
            manager.exception.ShareUmountError,
            self.compute._pre_deny_share, ctxt, instance, share)

        mark_error = self.compute._set_share_mapping_and_instance_in_error
        mark_error.assert_called_once_with(instance, share)

    @mock.patch.object(manager.manager.safe_utils, 'get_wrapped_function')
    def test_pre_live_rejects_unattested_source_before_side_effects(
            self, get_wrapped):
        ctxt = context.get_admin_context()
        instance = mock.MagicMock(
            uuid='00000000-0000-0000-0000-000000000001')
        self.compute._get_share_info = mock.Mock()

        for attestation in (None, False):
            data = migrate_data.IncusLiveMigrateData()
            if attestation is not None:
                data.full_checkpoint_verified = attestation

            with self.subTest(attestation=attestation):
                self.assertRaisesRegex(
                    manager.exception.MigrationError, 'did not attest',
                    self.compute._pre_live_migration_locked,
                    ctxt, instance, mock.sentinel.disk, data)

        self.compute._get_share_info.assert_not_called()
        self.compute.driver.get_share_mount_table.assert_not_called()
        self.compute.driver.stage_share_for_live_migration.assert_not_called()
        get_wrapped.assert_not_called()
        cleanup = (
            self.compute.driver.cleanup_pre_live_migration_destination)
        cleanup.assert_not_called()

    @mock.patch.object(manager.manager.safe_utils, 'get_wrapped_function')
    def test_pre_live_migration_mounts_destination_shares(
            self, get_wrapped):
        ctxt = context.get_admin_context()
        instance = mock.MagicMock(
            uuid='00000000-0000-0000-0000-000000000001')
        instance.__getitem__.side_effect = (
            lambda key: getattr(instance, key))
        shares = [
            mock.Mock(share_id='share-1', status='active'),
            mock.Mock(share_id='share-2', status='active'),
        ]
        self.compute._get_share_info = mock.Mock(return_value=shares)
        self.compute.driver.stage_share_for_live_migration.side_effect = [
            True, False]
        base_pre = mock.Mock(return_value=mock.sentinel.migrate_data)
        get_wrapped.return_value = base_pre
        data = migrate_data.IncusLiveMigrateData(
            full_checkpoint_verified=True,
            cleanup_token='10000000-0000-0000-0000-000000000001')

        result = self.compute._pre_live_migration_locked(
            ctxt, instance, mock.sentinel.disk,
            data)

        self.assertIs(mock.sentinel.migrate_data, result)
        self.assertEqual(
            [
                mock.call(
                    ctxt, instance, share,
                    '10000000-0000-0000-0000-000000000001',
                    mount_table=self.mount_table)
                for share in shares
            ],
            self.compute.driver.stage_share_for_live_migration.call_args_list)
        unstage = self.compute.driver.unstage_share_for_live_migration
        unstage.assert_not_called()
        base_pre.assert_called_once_with(
            self.compute, ctxt, instance, mock.sentinel.disk,
            data)

    @mock.patch.object(manager.manager.safe_utils, 'get_wrapped_function')
    def test_pre_live_base_rejection_unstages_all_destination_shares(
            self, get_wrapped):
        ctxt = context.get_admin_context()
        instance = mock.MagicMock(
            uuid='00000000-0000-0000-0000-000000000001')
        instance.__getitem__.side_effect = (
            lambda key: getattr(instance, key))
        shares = [
            mock.Mock(share_id='share-1', status='active'),
            mock.Mock(share_id='share-2', status='active'),
        ]
        self.compute._get_share_info = mock.Mock(return_value=shares)
        base_pre = mock.Mock(side_effect=manager.exception.MigrationError(
            reason='incremental source rejected'))
        get_wrapped.return_value = base_pre
        data = migrate_data.IncusLiveMigrateData(
            full_checkpoint_verified=True,
            cleanup_token='10000000-0000-0000-0000-000000000001')

        self.assertRaisesRegex(
            manager.exception.MigrationError, 'incremental source rejected',
            self.compute._pre_live_migration_locked,
            ctxt, instance, mock.sentinel.disk, data)

        unstage = self.compute.driver.unstage_share_for_live_migration
        self.assertEqual([
            mock.call(
                ctxt, instance, shares[1],
                '10000000-0000-0000-0000-000000000001',
                mount_table=self.mount_table),
            mock.call(
                ctxt, instance, shares[0],
                '10000000-0000-0000-0000-000000000001',
                mount_table=self.mount_table),
        ], unstage.call_args_list)
        cleanup = (
            self.compute.driver.cleanup_pre_live_migration_destination)
        cleanup.assert_called_once_with(ctxt, instance, data)

    @mock.patch.object(
        manager.manager.ComputeManager, 'pre_live_migration')
    def test_pre_live_migration_rolls_back_mounted_shares(self, base_pre):
        ctxt = context.get_admin_context()
        instance = mock.MagicMock(
            uuid='00000000-0000-0000-0000-000000000001')
        instance.__getitem__.side_effect = (
            lambda key: getattr(instance, key))
        first = mock.Mock(share_id='share-1', status='active')
        second = mock.Mock(share_id='share-2', status='active')
        self.compute._get_share_info = mock.Mock(
            return_value=[first, second])
        self.compute.driver.needs_migration_recovery.return_value = False
        self.compute.driver.stage_share_for_live_migration.side_effect = [
            True, RuntimeError('mount failed')]
        data = migrate_data.IncusLiveMigrateData(
            full_checkpoint_verified=True,
            cleanup_token='10000000-0000-0000-0000-000000000001')

        self.assertRaises(
            RuntimeError,
            self.compute._pre_live_migration_locked,
            ctxt, instance, mock.sentinel.disk,
            data)

        unstage = self.compute.driver.unstage_share_for_live_migration
        self.assertEqual([
            mock.call(
                ctxt, instance, second,
                '10000000-0000-0000-0000-000000000001',
                mount_table=self.mount_table),
            mock.call(
                ctxt, instance, first,
                '10000000-0000-0000-0000-000000000001',
                mount_table=self.mount_table),
        ], unstage.call_args_list)
        cleanup = (
            self.compute.driver.cleanup_pre_live_migration_destination)
        cleanup.assert_called_once_with(ctxt, instance, data)
        base_pre.assert_not_called()

    @mock.patch.object(
        manager.manager.ComputeManager, 'pre_live_migration')
    def test_pre_live_first_share_failure_rolls_back_its_journal(
            self, base_pre):
        ctxt = context.get_admin_context()
        instance = mock.MagicMock(
            uuid='00000000-0000-0000-0000-000000000001')
        instance.__getitem__.side_effect = (
            lambda key: getattr(instance, key))
        share = mock.Mock(share_id='share-1', status='active')
        self.compute._get_share_info = mock.Mock(return_value=[share])
        self.compute.driver.stage_share_for_live_migration.side_effect = (
            RuntimeError('first mount failed'))
        data = migrate_data.IncusLiveMigrateData(
            full_checkpoint_verified=True,
            cleanup_token='10000000-0000-0000-0000-000000000001')

        self.assertRaises(
            RuntimeError,
            self.compute._pre_live_migration_locked,
            ctxt, instance, mock.sentinel.disk, data)

        unstage = self.compute.driver.unstage_share_for_live_migration
        unstage.assert_called_once_with(
            ctxt, instance, share,
            '10000000-0000-0000-0000-000000000001',
            mount_table=self.mount_table)
        cleanup = (
            self.compute.driver.cleanup_pre_live_migration_destination)
        cleanup.assert_called_once_with(ctxt, instance, data)
        base_pre.assert_not_called()

    @mock.patch.object(
        manager.manager.ComputeManager, '_finish_resize_helper',
        return_value=mock.sentinel.result)
    @mock.patch.object(
        manager.incus_driver, 'prepare_cold_migration_share_info',
        return_value=('prepared-disk', 'migration-token'))
    def test_finish_resize_pre_stages_only_active_shares(
            self, prepare, base_finish):
        ctxt = context.get_admin_context()
        instance = mock.Mock(uuid='instance')
        self.compute.driver.get_info.return_value.state = (
            power_state.RUNNING)
        active = mock.Mock(share_id='active', status='active')
        inactive = mock.Mock(share_id='inactive', status='inactive')
        self.compute._get_share_info = mock.Mock(
            return_value=[active, inactive])

        result = self.compute._finish_resize_helper(
            ctxt, 'disk', mock.sentinel.image, instance,
            mock.sentinel.migration, mock.sentinel.request_spec)

        self.assertIs(mock.sentinel.result, result)
        prepare.assert_called_once_with('disk', [active])
        stage = self.compute.driver.stage_share_for_cold_migration
        stage.assert_called_once_with(
            ctxt, instance, active, 'migration-token',
            mount_table=self.mount_table)
        base_finish.assert_called_once_with(
            ctxt, 'prepared-disk', mock.sentinel.image, instance,
            mock.sentinel.migration, mock.sentinel.request_spec)
        self.assertEqual(power_state.RUNNING, instance.power_state)
        instance.save.assert_called_once_with(expected_task_state=[None])

    @mock.patch.object(
        manager.manager.ComputeManager, '_finish_resize_helper')
    def test_finish_resize_rejects_stale_profile_before_side_effects(
            self, base_finish):
        ctxt = context.get_admin_context()
        instance = mock.Mock(uuid='instance')
        preflight = (
            self.compute.driver.preflight_cold_migration_destination_profile)
        preflight.side_effect = exception.MigrationPreCheckError(
            reason='stale profile')
        self.compute._get_share_info = mock.Mock()

        self.assertRaises(
            exception.MigrationPreCheckError,
            self.compute._finish_resize_helper,
            ctxt, 'disk', mock.sentinel.image, instance,
            mock.sentinel.migration, mock.sentinel.request_spec)

        preflight.assert_called_once_with(instance, 'disk')
        self.compute._get_share_info.assert_not_called()
        self.compute.driver.stage_share_for_cold_migration.assert_not_called()
        base_finish.assert_not_called()

    @mock.patch.object(
        manager.manager.ComputeManager, '_finish_resize_helper',
        side_effect=RuntimeError('finish failed'))
    @mock.patch.object(
        manager.incus_driver, 'prepare_cold_migration_share_info',
        return_value=('prepared-disk', 'migration-token'))
    def test_finish_resize_delegates_failed_finish_transaction(
            self, prepare, base_finish):
        ctxt = context.get_admin_context()
        instance = mock.Mock(uuid='instance')
        first = mock.Mock(share_id='first', status='active')
        second = mock.Mock(share_id='second', status='active')
        self.compute._get_share_info = mock.Mock(
            return_value=[first, second])
        rollback = (
            self.compute.driver.rollback_cold_migration_preparation)
        rollback.return_value = False

        self.assertRaises(
            RuntimeError,
            self.compute._finish_resize_helper,
            ctxt, 'disk', mock.sentinel.image, instance,
            mock.sentinel.migration, mock.sentinel.request_spec)

        rollback.assert_called_once_with(
            ctxt, instance, 'prepared-disk')
        unstage = self.compute.driver.unstage_share_for_cold_migration
        unstage.assert_not_called()

    @mock.patch.object(
        manager.manager.ComputeManager, '_finish_resize_helper')
    @mock.patch.object(
        manager.incus_driver, 'prepare_cold_migration_share_info',
        return_value=('prepared-disk', 'migration-token'))
    def test_finish_resize_first_share_failure_retires_preparation(
            self, prepare, base_finish):
        ctxt = context.get_admin_context()
        instance = mock.Mock(uuid='instance')
        share = mock.Mock(share_id='share', status='active')
        self.compute._get_share_info = mock.Mock(return_value=[share])
        stage = self.compute.driver.stage_share_for_cold_migration
        stage.side_effect = RuntimeError('first mount failed')
        rollback = (
            self.compute.driver.rollback_cold_migration_preparation)
        rollback.return_value = False

        self.assertRaises(
            RuntimeError,
            self.compute._finish_resize_helper,
            ctxt, 'disk', mock.sentinel.image, instance,
            mock.sentinel.migration, mock.sentinel.request_spec)

        unstage = self.compute.driver.unstage_share_for_cold_migration
        unstage.assert_called_once_with(
            ctxt, instance, share, 'migration-token',
            mount_table=self.mount_table)
        rollback.assert_called_once_with(
            ctxt, instance, 'prepared-disk')
        base_finish.assert_not_called()

    @mock.patch.object(
        manager.manager.ComputeManager, '_finish_resize_helper')
    def test_finish_resize_share_query_failure_retires_original_attempt(
            self, base_finish):
        ctxt = context.get_admin_context()
        instance = mock.Mock(uuid='instance')
        self.compute._get_share_info = mock.Mock(
            side_effect=RuntimeError('Manila query failed'))
        rollback = (
            self.compute.driver.rollback_cold_migration_preparation)
        rollback.return_value = False

        self.assertRaises(
            RuntimeError,
            self.compute._finish_resize_helper,
            ctxt, 'source-disk', mock.sentinel.image, instance,
            mock.sentinel.migration, mock.sentinel.request_spec)

        rollback.assert_called_once_with(
            ctxt, instance, 'source-disk')
        unstage = self.compute.driver.unstage_share_for_cold_migration
        unstage.assert_not_called()
        base_finish.assert_not_called()

    @mock.patch.object(
        manager.manager.ComputeManager, '_finish_resize_helper',
        side_effect=RuntimeError('finish failed'))
    @mock.patch.object(
        manager.incus_driver, 'prepare_cold_migration_share_info',
        return_value=('prepared-disk', 'migration-token'))
    def test_finish_resize_retains_staging_for_recovery_target(
            self, prepare, base_finish):
        ctxt = context.get_admin_context()
        instance = mock.Mock(uuid='instance')
        share = mock.Mock(share_id='share', status='active')
        self.compute._get_share_info = mock.Mock(return_value=[share])
        rollback = (
            self.compute.driver.rollback_cold_migration_preparation)
        rollback.return_value = True

        self.assertRaises(
            RuntimeError,
            self.compute._finish_resize_helper,
            ctxt, 'disk', mock.sentinel.image, instance,
            mock.sentinel.migration, mock.sentinel.request_spec)

        unstage = self.compute.driver.unstage_share_for_cold_migration
        unstage.assert_not_called()
        rollback.assert_called_once_with(
            ctxt, instance, 'prepared-disk')

    @mock.patch.object(
        manager.manager.ComputeManager, '_finish_revert_resize',
        return_value=mock.sentinel.result)
    def test_finish_revert_validates_retained_active_shares(
            self, base_revert):
        ctxt = context.get_admin_context()
        instance = mock.Mock(uuid='instance')
        active = mock.Mock(share_id='active', status='active')
        inactive = mock.Mock(share_id='inactive', status='inactive')
        self.compute._get_share_info = mock.Mock(
            return_value=[active, inactive])
        self.compute._mount_all_shares = mock.Mock()
        self.compute._handoff_cold_source_rotations_for_revert = mock.Mock()

        result = self.compute._finish_revert_resize(
            ctxt, instance, mock.sentinel.migration,
            request_spec=mock.sentinel.request_spec)

        self.assertIs(mock.sentinel.result, result)
        self.compute._mount_all_shares.assert_called_once_with(
            ctxt, instance, [active])
        self.compute._handoff_cold_source_rotations_for_revert.\
            assert_called_once_with(
                ctxt, instance, mock.sentinel.migration)
        base_revert.assert_called_once_with(
            ctxt, instance, mock.sentinel.migration,
            request_spec=mock.sentinel.request_spec)

    @mock.patch.object(manager.eventlet, 'sleep')
    @mock.patch.object(
        manager.manager.ComputeManager, '_shutdown_instance')
    @mock.patch.object(
        manager.manager.ComputeManager, '_notify_volume_usage_detach')
    def test_shutdown_settles_volume_usage_before_destroy(
            self, notify_usage, shutdown, sleep):
        volume = mock.Mock(is_volume=True, volume_id='volume-1')
        local_disk = mock.Mock(is_volume=False)
        instance = mock.Mock(system_metadata={})
        instance.obj_attr_is_set.return_value = True
        ctxt = context.get_admin_context()

        self.compute._shutdown_instance(
            ctxt, instance, [volume, local_disk],
            requested_networks=['network-1'], notify=False,
            try_deallocate_networks=False)

        sleep.assert_called_once_with(manager._METRICS_SETTLEMENT_DELAY)
        notify_usage.assert_called_once_with(ctxt, instance, volume)
        shutdown.assert_called_once_with(
            ctxt, instance, [volume, local_disk],
            requested_networks=['network-1'], notify=False,
            try_deallocate_networks=False)

    @mock.patch.object(manager.eventlet, 'sleep')
    @mock.patch.object(
        manager.manager.ComputeManager, '_shutdown_instance')
    @mock.patch.object(
        manager.manager.ComputeManager, '_notify_volume_usage_detach')
    def test_shutdown_continues_when_volume_usage_settlement_fails(
            self, notify_usage, shutdown, sleep):
        volume = mock.Mock(is_volume=True, volume_id='volume-1')
        instance = mock.Mock(system_metadata={})
        instance.obj_attr_is_set.return_value = True
        ctxt = context.get_admin_context()
        notify_usage.side_effect = RuntimeError('statistics unavailable')
        self.compute._try_deallocate_network = mock.Mock()

        self.compute._shutdown_instance(ctxt, instance, [volume])

        sleep.assert_called_once_with(manager._METRICS_SETTLEMENT_DELAY)
        shutdown.assert_called_once_with(
            ctxt, instance, [volume],
            requested_networks=None, notify=True,
            try_deallocate_networks=False)
        self.compute._try_deallocate_network.assert_called_once_with(
            mock.ANY, instance, None)

    def _failed_build_instance(self, system_metadata=None):
        instance = mock.Mock(
            uuid='10000000-0000-0000-0000-000000000001',
            host=self.compute.host,
            compute_id='20000000-0000-0000-0000-000000000002',
            vm_state=manager.vm_states.BUILDING,
            task_state=task_states.SPAWNING,
            system_metadata=dict(system_metadata or {}))
        instance.name = 'instance-00000001'
        instance.obj_attr_is_set.return_value = True
        return instance

    @staticmethod
    def _unsafe_failed_build_assessment(reason='local residue'):
        return manager.incus_driver.FailedBuildCleanupAssessment.unsafe(reason)

    def _install_failed_build_barrier(self, instance, assessment=None):
        assessment = assessment or self._unsafe_failed_build_assessment()
        record = self.compute._failed_build_cleanup_record(
            instance, assessment)
        instance.system_metadata[
            manager._FAILED_BUILD_CLEANUP_BARRIER_KEY
        ] = record
        return record

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_failed_build_barrier_persists_versioned_host_binding(
            self, get_by_uuid):
        instance = self._failed_build_instance()
        current = self._failed_build_instance()
        get_by_uuid.return_value = current
        assessment = self._unsafe_failed_build_assessment('stale container')

        self.compute._persist_failed_build_cleanup_barrier(
            context.get_admin_context(), instance, assessment)

        encoded = instance.system_metadata[
            manager._FAILED_BUILD_CLEANUP_BARRIER_KEY]
        self.assertLessEqual(
            len(encoded), manager._SYSTEM_METADATA_VALUE_MAX_LENGTH)
        self.assertRegex(encoded, r'^v1:0:[0-9a-f]{64}$')
        decoded = self.compute._decode_failed_build_cleanup_barrier(instance)
        self.assertEqual((False, False, False, False), (
            decoded.release_network,
            decoded.release_cinder,
            decoded.release_host,
            decoded.release_placement))
        current.save.assert_called_once_with()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    @mock.patch.object(
        manager.manager.ComputeManager, '_shutdown_instance',
        side_effect=RuntimeError('Incus destroy uncertain'))
    def test_failed_build_destroy_retains_network_before_barrier(
            self, base_shutdown, get_by_uuid):
        instance = self._failed_build_instance()
        current = self._failed_build_instance()
        get_by_uuid.return_value = current
        self.compute.driver.assess_failed_build_cleanup.return_value = (
            self._unsafe_failed_build_assessment('stale container'))
        self.compute._try_deallocate_network = mock.Mock()
        ctxt = context.get_admin_context()

        self.assertRaisesRegex(
            RuntimeError, 'destroy uncertain',
            self.compute._shutdown_instance, ctxt, instance, [],
            requested_networks=mock.sentinel.requested_networks)

        base_shutdown.assert_called_once_with(
            ctxt, instance, [],
            requested_networks=mock.sentinel.requested_networks,
            notify=True, try_deallocate_networks=False)
        self.compute._try_deallocate_network.assert_not_called()
        self.assertIn(
            manager._FAILED_BUILD_CLEANUP_BARRIER_KEY,
            instance.system_metadata)
        self.assertFalse(
            self.compute._should_delete_allocation_for_failed_build(
                ctxt, instance))

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    @mock.patch.object(
        manager.manager.ComputeManager, '_shutdown_instance',
        return_value=mock.sentinel.shutdown_result)
    def test_successful_retry_clears_barrier_then_deallocates_once(
            self, base_shutdown, get_by_uuid):
        instance = self._failed_build_instance()
        self._install_failed_build_barrier(instance)
        current = self._failed_build_instance(instance.system_metadata)
        get_by_uuid.return_value = current
        self.compute._try_deallocate_network = mock.Mock()
        ctxt = mock.Mock()
        ctxt.elevated.return_value = mock.sentinel.admin_context

        result = self.compute._shutdown_instance(
            ctxt, instance, [],
            requested_networks=mock.sentinel.requested_networks)

        self.assertIs(mock.sentinel.shutdown_result, result)
        base_shutdown.assert_called_once_with(
            ctxt, instance, [],
            requested_networks=mock.sentinel.requested_networks,
            notify=True, try_deallocate_networks=False)
        self.compute._try_deallocate_network.assert_called_once_with(
            mock.sentinel.admin_context, instance,
            mock.sentinel.requested_networks)
        self.assertNotIn(
            manager._FAILED_BUILD_CLEANUP_BARRIER_KEY,
            instance.system_metadata)
        self.assertNotIn(
            manager._FAILED_BUILD_CLEANUP_BARRIER_KEY,
            current.system_metadata)
        current.save.assert_called_once_with()

    @mock.patch.object(
        manager.IncusComputeManager,
        '_persist_failed_build_cleanup_barrier')
    def test_assessment_failure_persists_fail_closed_barrier(self, persist):
        instance = self._failed_build_instance()
        self.compute.driver.assess_failed_build_cleanup.side_effect = (
            RuntimeError('Incus API unavailable'))

        self.compute._record_failed_build_cleanup_barrier(
            context.get_admin_context(), instance, [])

        assessment = persist.call_args.args[2]
        self.assertEqual((False, False, False, False), (
            assessment.release_network,
            assessment.release_cinder,
            assessment.release_host,
            assessment.release_placement))

    @mock.patch.object(
        manager.IncusComputeManager,
        '_persist_failed_build_cleanup_barrier')
    def test_clean_assessment_does_not_persist_barrier(self, persist):
        instance = self._failed_build_instance()
        self.compute.driver.assess_failed_build_cleanup.return_value = (
            manager.incus_driver.FailedBuildCleanupAssessment(
                True, True, True, True))

        self.compute._record_failed_build_cleanup_barrier(
            context.get_admin_context(), instance, [])

        persist.assert_not_called()

    @mock.patch.object(
        manager.manager.ComputeManager,
        '_nil_out_instance_obj_host_and_node')
    @mock.patch.object(manager.manager.ComputeManager, '_cleanup_volumes')
    @mock.patch.object(
        manager.manager.ComputeManager, '_cleanup_allocated_networks')
    def test_failed_build_barrier_retains_framework_ownership(
            self, cleanup_networks, cleanup_volumes, nil_host):
        instance = self._failed_build_instance()
        self._install_failed_build_barrier(instance)
        ctxt = context.get_admin_context()

        self.compute._cleanup_allocated_networks(
            ctxt, instance, mock.sentinel.requested_networks)
        self.compute._cleanup_volumes(ctxt, instance, [])
        self.compute._nil_out_instance_obj_host_and_node(instance)

        cleanup_networks.assert_not_called()
        cleanup_volumes.assert_not_called()
        nil_host.assert_not_called()
        self.assertFalse(
            self.compute._should_delete_allocation_for_failed_build(
                ctxt, instance))

    @mock.patch.object(
        manager.manager.ComputeManager,
        '_nil_out_instance_obj_host_and_node')
    @mock.patch.object(manager.manager.ComputeManager, '_cleanup_volumes')
    @mock.patch.object(
        manager.manager.ComputeManager, '_cleanup_allocated_networks')
    def test_clean_instance_delegates_framework_cleanup(
            self, cleanup_networks, cleanup_volumes, nil_host):
        instance = self._failed_build_instance()
        ctxt = context.get_admin_context()

        self.compute._cleanup_allocated_networks(
            ctxt, instance, mock.sentinel.requested_networks)
        self.compute._cleanup_volumes(
            ctxt, instance, mock.sentinel.bdms,
            raise_exc=False, detach=False)
        self.compute._nil_out_instance_obj_host_and_node(instance)

        cleanup_networks.assert_called_once_with(
            ctxt, instance, mock.sentinel.requested_networks)
        cleanup_volumes.assert_called_once_with(
            ctxt, instance, mock.sentinel.bdms,
            raise_exc=False, detach=False)
        nil_host.assert_called_once_with(instance)
        self.assertTrue(
            self.compute._should_delete_allocation_for_failed_build(
                ctxt, instance))

    def test_malformed_or_foreign_barrier_fails_closed(self):
        instance = self._failed_build_instance({
            manager._FAILED_BUILD_CLEANUP_BARRIER_KEY: '{not-json',
        })

        assessment = self.compute._failed_build_cleanup_barrier(instance)

        self.assertEqual((False, False, False, False), (
            assessment.release_network,
            assessment.release_cinder,
            assessment.release_host,
            assessment.release_placement))
        self.assertFalse(
            self.compute._should_delete_allocation_for_failed_build(
                context.get_admin_context(), instance))

        instance = self._failed_build_instance()
        encoded = self._install_failed_build_barrier(instance)
        instance.system_metadata[
            manager._FAILED_BUILD_CLEANUP_BARRIER_KEY
        ] = encoded[:-1] + ('0' if encoded[-1] != '0' else '1')
        self.assertFalse(
            self.compute._should_delete_allocation_for_failed_build(
                context.get_admin_context(), instance))

    def test_barrier_binding_rejects_host_and_compute_id_changes(self):
        instance = self._failed_build_instance()
        self._install_failed_build_barrier(instance)

        instance.host = 'other-compute'
        self.assertRaises(
            ValueError,
            self.compute._decode_failed_build_cleanup_barrier, instance)

        instance.host = self.compute.host
        instance.compute_id = '30000000-0000-0000-0000-000000000003'
        self.assertRaises(
            ValueError,
            self.compute._decode_failed_build_cleanup_barrier, instance)

    def test_barrier_size_is_fixed_for_maximum_owner_names(self):
        original_host = self.compute.host
        self.compute.host = 'h' * 255
        instance = self._failed_build_instance()
        instance.name = 'n' * 255
        try:
            encoded = self._install_failed_build_barrier(instance)
            self.assertLessEqual(
                len(encoded), manager._SYSTEM_METADATA_VALUE_MAX_LENGTH)
            self.assertEqual(
                self._unsafe_failed_build_assessment().release_placement,
                self.compute._decode_failed_build_cleanup_barrier(
                    instance).release_placement)
        finally:
            self.compute.host = original_host

    @mock.patch.object(manager.eventlet, 'sleep')
    @mock.patch.object(
        manager.manager.ComputeManager, '_notify_volume_usage_detach')
    def test_detach_waits_for_fresh_incus_metrics(
            self, notify_usage, sleep):
        volume = mock.Mock()
        instance = mock.Mock()
        ctxt = context.get_admin_context()

        self.compute._notify_volume_usage_detach(ctxt, instance, volume)

        sleep.assert_called_once_with(manager._METRICS_SETTLEMENT_DELAY)
        notify_usage.assert_called_once_with(ctxt, instance, volume)

    @mock.patch.object(manager.eventlet, 'sleep')
    @mock.patch.object(
        manager.manager.ComputeManager, '_notify_volume_usage_detach')
    def test_detach_skips_delay_when_volume_metering_is_disabled(
            self, notify_usage, sleep):
        self.flags(volume_usage_poll_interval=0)
        volume = mock.Mock()
        instance = mock.Mock()
        ctxt = context.get_admin_context()

        self.compute._notify_volume_usage_detach(ctxt, instance, volume)

        sleep.assert_not_called()
        notify_usage.assert_called_once_with(ctxt, instance, volume)

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuids')
    @mock.patch.object(manager.objects.InstanceList, 'get_by_host')
    def test_get_host_volume_bdms_uses_one_bulk_query(
            self, get_instances, get_bdms):
        ctxt = context.get_admin_context()
        instance_1 = mock.Mock(uuid='instance-1')
        instance_2 = mock.Mock(uuid='instance-2')
        instance_3 = mock.Mock(uuid='instance-3')
        get_instances.return_value = [
            instance_1, instance_2, instance_3]
        volume_1 = mock.Mock(
            instance_uuid=instance_1.uuid, is_volume=True)
        volume_2 = mock.Mock(
            instance_uuid=instance_2.uuid, is_volume=True)
        local_disk = mock.Mock(
            instance_uuid=instance_1.uuid, is_volume=False)
        get_bdms.return_value = [volume_2, local_disk, volume_1]

        result = self.compute._get_host_volume_bdms(
            ctxt, use_slave=True)

        self.assertEqual([
            {
                'instance': instance_1,
                'instance_bdms': [volume_1],
            },
            {
                'instance': instance_2,
                'instance_bdms': [volume_2],
            },
            {
                'instance': instance_3,
                'instance_bdms': [],
            },
        ], result)
        get_instances.assert_called_once_with(
            ctxt, self.compute.host, use_slave=True)
        get_bdms.assert_called_once_with(
            ctxt, ['instance-1', 'instance-2', 'instance-3'],
            use_slave=True)

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuids')
    @mock.patch.object(
        manager.objects.InstanceList, 'get_by_host', return_value=[])
    def test_get_host_volume_bdms_skips_bulk_query_without_instances(
            self, get_instances, get_bdms):
        ctxt = context.get_admin_context()

        result = self.compute._get_host_volume_bdms(ctxt)

        self.assertEqual([], result)
        get_instances.assert_called_once_with(
            ctxt, self.compute.host, use_slave=False)
        get_bdms.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_cleanup_recovery_replays_remote_host_profile(
            self, get_by_uuid):
        candidate = mock.Mock(
            task_state=None,
            uuid='10000000-0000-0000-0000-000000000001',
            host='compute-2', info_cache=None)
        candidate.name = 'instance-candidate'
        get_by_uuid.return_value = candidate
        self.compute.driver.list_cleanup_recovery_candidates.return_value = [{
            'name': candidate.name,
            'uuid': candidate.uuid,
        }]
        network_info = (
            self.compute.network_api.get_instance_nw_info.return_value)

        self.compute._recover_incus_cleanup_profiles(
            context.get_admin_context())

        get_by_uuid.assert_called_once_with(
            mock.ANY, candidate.uuid, expected_attrs=['info_cache'])
        self.assertEqual('yes', get_by_uuid.call_args.args[0].read_deleted)
        self.compute.driver.recover_cleanup_profile.assert_called_once_with(
            mock.ANY, candidate, network_info)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_cleanup_recovery_replays_same_host_absent_target_profile(
            self, get_by_uuid):
        candidate = mock.Mock(
            task_state=None,
            uuid='10000000-0000-0000-0000-000000000001',
            host=self.compute.host, info_cache=None)
        candidate.name = 'instance-candidate'
        get_by_uuid.return_value = candidate
        self.compute.driver.list_cleanup_recovery_candidates.return_value = [{
            'name': candidate.name,
            'uuid': candidate.uuid,
        }]

        self.compute._recover_incus_cleanup_profiles(
            context.get_admin_context())

        network_info = (
            self.compute.network_api.get_instance_nw_info.return_value)
        self.compute.driver.recover_cleanup_profile.assert_called_once_with(
            mock.ANY, candidate, network_info)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_cleanup_recovery_ignores_frozen_task_state_of_deleted_row(
            self, get_by_uuid):
        # The periodic reads deleted rows (read_deleted=yes), so a deletion
        # that raced an unfinished build leaves deleted=<id> with the frozen
        # task_state 'deleting'. That history must not retain the profile.
        network_info = mock.sentinel.network_info
        candidate = mock.Mock(
            task_state='deleting', deleted=2304,
            uuid='10000000-0000-0000-0000-000000000001',
            host='compute-2',
            info_cache=mock.Mock(network_info=network_info))
        candidate.name = 'instance-candidate'
        candidate.obj_attr_is_set.return_value = True
        get_by_uuid.return_value = candidate
        self.compute.driver.list_cleanup_recovery_candidates.return_value = [{
            'name': candidate.name,
            'uuid': candidate.uuid,
        }]

        self.compute._recover_incus_cleanup_profiles(
            context.get_admin_context())

        self.compute.driver.recover_cleanup_profile.assert_called_once_with(
            mock.ANY, candidate, network_info)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_cleanup_recovery_still_defers_to_live_task_state(
            self, get_by_uuid):
        candidate = mock.Mock(
            task_state='deleting', deleted=0,
            uuid='10000000-0000-0000-0000-000000000001',
            host='compute-2', info_cache=None)
        candidate.name = 'instance-candidate'
        candidate.obj_attr_is_set.return_value = True
        get_by_uuid.return_value = candidate
        self.compute.driver.list_cleanup_recovery_candidates.return_value = [{
            'name': candidate.name,
            'uuid': candidate.uuid,
        }]

        self.compute._recover_incus_cleanup_profiles(
            context.get_admin_context())

        self.compute.driver.recover_cleanup_profile.assert_not_called()

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_cleanup_recovery_uses_deleted_instance_network_cache(
            self, get_by_uuid):
        network_info = mock.sentinel.network_info
        candidate = mock.Mock(
            task_state=None,
            uuid='10000000-0000-0000-0000-000000000001',
            host='compute-2',
            info_cache=mock.Mock(network_info=network_info))
        candidate.name = 'instance-candidate'
        get_by_uuid.return_value = candidate
        self.compute.driver.list_cleanup_recovery_candidates.return_value = [{
            'name': candidate.name,
            'uuid': candidate.uuid,
        }]

        self.compute._recover_incus_cleanup_profiles(
            context.get_admin_context())

        self.compute.network_api.get_instance_nw_info.assert_not_called()
        self.compute.driver.recover_cleanup_profile.assert_called_once_with(
            mock.ANY, candidate, network_info)

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_share_journal_recovery_requires_terminal_related_migration(
            self, get_by_uuid, get_migrations):
        instance = mock.Mock(
            uuid='10000000-0000-0000-0000-000000000001',
            host='compute-2', task_state=None, vm_state='active',
            deleted=False)
        instance.name = 'instance-candidate'
        get_by_uuid.return_value = instance
        migration = mock.Mock(
            uuid='20000000-0000-0000-0000-000000000002',
            source_compute=self.compute.host, dest_compute='compute-2',
            status='completed')
        get_migrations.return_value = [migration]
        candidate = {
            'name': instance.name,
            'uuid': instance.uuid,
            'operation_token':
                '20000000-0000-0000-0000-000000000002',
            'share_ids': (
                '30000000-0000-0000-0000-000000000003',),
        }
        list_candidates = (
            self.compute.driver.list_share_journal_recovery_candidates)
        list_candidates.return_value = [candidate]

        self.compute._recover_incus_share_journals(
            context.get_admin_context())

        get_migrations.assert_called_once_with(
            mock.ANY, {'instance_uuid': instance.uuid})
        recover = self.compute.driver.recover_share_journal_candidate
        recover.assert_called_once_with(instance, candidate)

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_share_journal_recovery_retains_unconfirmed_resize(
            self, get_by_uuid, get_migrations):
        instance = mock.Mock(
            uuid='10000000-0000-0000-0000-000000000001',
            host='compute-2', task_state=None, vm_state='resized',
            deleted=False)
        instance.name = 'instance-candidate'
        get_by_uuid.return_value = instance
        candidate = {
            'name': instance.name,
            'uuid': instance.uuid,
            'operation_token':
                '20000000-0000-0000-0000-000000000002',
            'share_ids': (),
        }
        list_candidates = (
            self.compute.driver.list_share_journal_recovery_candidates)
        list_candidates.return_value = [candidate]

        self.compute._recover_incus_share_journals(
            context.get_admin_context())

        get_migrations.assert_not_called()
        recover = self.compute.driver.recover_share_journal_candidate
        recover.assert_not_called()

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_share_journal_recovery_retains_soft_deleted_instance(
            self, get_by_uuid, get_migrations):
        instance = mock.Mock(
            uuid='10000000-0000-0000-0000-000000000001',
            host='compute-2', task_state=None, vm_state='active',
            deleted=True)
        instance.name = 'instance-candidate'
        get_by_uuid.return_value = instance
        candidate = {
            'name': instance.name,
            'uuid': instance.uuid,
            'operation_token':
                '20000000-0000-0000-0000-000000000002',
            'share_ids': (),
        }
        list_candidates = (
            self.compute.driver.list_share_journal_recovery_candidates)
        list_candidates.return_value = [candidate]

        self.compute._recover_incus_share_journals(
            context.get_admin_context())

        get_migrations.assert_not_called()
        recover = self.compute.driver.recover_share_journal_candidate
        recover.assert_not_called()

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_share_journal_recovery_retains_non_terminal_migration(
            self, get_by_uuid, get_migrations):
        instance = mock.Mock(
            uuid='10000000-0000-0000-0000-000000000001',
            host='compute-2', task_state=None, vm_state='active',
            deleted=False)
        instance.name = 'instance-candidate'
        get_by_uuid.return_value = instance
        get_migrations.return_value = [mock.Mock(
            source_compute=self.compute.host, dest_compute='compute-2',
            status='finished')]
        candidate = {
            'name': instance.name,
            'uuid': instance.uuid,
            'operation_token':
                '20000000-0000-0000-0000-000000000002',
            'share_ids': (),
        }
        list_candidates = (
            self.compute.driver.list_share_journal_recovery_candidates)
        list_candidates.return_value = [candidate]

        self.compute._recover_incus_share_journals(
            context.get_admin_context())

        recover = self.compute.driver.recover_share_journal_candidate
        recover.assert_not_called()

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_destination_profile_recovery_requires_exact_terminal_migration(
            self, get_by_uuid, get_migrations):
        instance = mock.Mock(
            uuid='10000000-0000-0000-0000-000000000001',
            host='compute-2', task_state=None, vm_state='active',
            deleted=False, info_cache=None)
        instance.name = 'instance-candidate'
        token = '20000000-0000-0000-0000-000000000002'
        migration_uuid = '30000000-0000-0000-0000-000000000003'
        get_by_uuid.return_value = instance
        migration = mock.Mock(
            uuid=migration_uuid, source_compute=self.compute.host,
            dest_compute='compute-2', status='error')
        get_migrations.return_value = [migration]
        candidate = {
            'name': instance.name,
            'uuid': instance.uuid,
            'operation_token': token,
            'migration_uuid': migration_uuid,
            'idmap_base': 1065536,
            'idmap_size': 65536,
        }
        list_candidates = (
            self.compute.driver.
            list_destination_prepared_recovery_candidates)
        list_candidates.return_value = [candidate]
        network_info = (
            self.compute.network_api.get_instance_nw_info.return_value)

        self.compute._recover_incus_destination_profiles(
            context.get_admin_context())

        get_migrations.assert_called_once_with(
            mock.ANY, {'instance_uuid': instance.uuid})
        recover = (
            self.compute.driver.recover_destination_prepared_profile)
        recover.assert_called_once_with(
            mock.ANY, instance, candidate, migration, network_info)

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_source_volume_generation_recovery_retires_exact_source_owner(
            self, get_by_uuid, get_migrations):
        instance = mock.Mock(
            uuid='10000000-0000-0000-0000-000000000001',
            host=self.compute.host, task_state=None, deleted=False)
        instance.name = 'instance-source'
        instance.obj_attr_is_set.return_value = True
        token = '20000000-0000-0000-0000-000000000002'
        migration_uuid = '30000000-0000-0000-0000-000000000003'
        candidate = {
            'name': instance.name,
            'uuid': instance.uuid,
            'operation_token': token,
            'migration_uuid': migration_uuid,
        }
        self.compute.driver.\
            list_source_volume_generation_recovery_candidates.return_value = [
                candidate]
        get_by_uuid.return_value = instance
        get_migrations.return_value = [mock.Mock(
            uuid=migration_uuid, source_compute=self.compute.host,
            dest_compute='compute-2', status='reverted')]
        self.compute.driver.\
            finalize_remote_source_volume_generation.return_value = True

        self.compute._recover_incus_source_volume_generations(
            context.get_admin_context())

        get_by_uuid.assert_called_once_with(
            mock.ANY, instance.uuid,
            expected_attrs=['system_metadata'])
        get_migrations.assert_called_once_with(
            mock.ANY, {'instance_uuid': instance.uuid})
        self.compute.driver.finalize_remote_source_volume_generation.\
            assert_called_once_with(instance, token)

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_source_volume_generation_recovery_resumes_live_rollback(
            self, get_by_uuid, get_migrations):
        instance = mock.Mock(
            uuid='10000000-0000-0000-0000-000000000001',
            host=self.compute.host, task_state=None, deleted=False)
        instance.name = 'instance-source'
        instance.obj_attr_is_set.return_value = True
        token = '20000000-0000-0000-0000-000000000002'
        migration_uuid = '30000000-0000-0000-0000-000000000003'
        candidate = {
            'name': instance.name,
            'uuid': instance.uuid,
            'operation_token': token,
            'migration_uuid': migration_uuid,
            'rollback_complete': False,
        }
        self.compute.driver.\
            list_source_volume_generation_recovery_candidates.return_value = [
                candidate]
        get_by_uuid.return_value = instance
        get_migrations.return_value = [mock.Mock(
            uuid=migration_uuid, source_compute=self.compute.host,
            dest_compute='compute-2', status='error',
            migration_type='live-migration')]
        self.compute.driver.\
            finalize_remote_source_volume_generation.return_value = True
        network_info = (
            self.compute.network_api.get_instance_nw_info.return_value)

        self.compute._recover_incus_source_volume_generations(
            context.get_admin_context())

        self.compute.driver.recover_live_migration_rollback.\
            assert_called_once_with(
                mock.ANY, instance, token, migration_uuid, network_info)
        self.compute.driver.finalize_remote_source_volume_generation.\
            assert_called_once_with(instance, token)

    @mock.patch.object(manager.objects.MigrationList, 'get_by_filters')
    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_source_volume_generation_recovery_rejects_target_owner(
            self, get_by_uuid, get_migrations):
        instance = mock.Mock(
            uuid='10000000-0000-0000-0000-000000000001',
            host=self.compute.host, task_state=None, deleted=False)
        instance.name = 'instance-source'
        instance.obj_attr_is_set.return_value = True
        token = '20000000-0000-0000-0000-000000000002'
        migration_uuid = '30000000-0000-0000-0000-000000000003'
        self.compute.driver.\
            list_source_volume_generation_recovery_candidates.return_value = [{
                'name': instance.name,
                'uuid': instance.uuid,
                'operation_token': token,
                'migration_uuid': migration_uuid,
            }]
        get_by_uuid.return_value = instance
        get_migrations.return_value = [mock.Mock(
            uuid=migration_uuid, source_compute='compute-2',
            dest_compute=self.compute.host, status='completed')]

        self.compute._recover_incus_source_volume_generations(
            context.get_admin_context())

        self.compute.driver.finalize_source_volume_generation.\
            assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_recovers_only_explicit_candidate(self, get_by_uuid, get_bdms):
        candidate = mock.Mock()
        candidate.task_state = None
        candidate.uuid = 'candidate'
        candidate.host = self.compute.host
        candidate.name = 'instance-candidate'
        candidate.vm_state = 'resized'
        get_by_uuid.return_value = candidate
        recovery_candidates = (
            self.compute.driver.list_migration_recovery_candidates)
        recovery_candidates.return_value = [{
            'name': candidate.name,
            'uuid': candidate.uuid,
        }]
        self.compute.driver.needs_migration_recovery.return_value = True
        self.compute.driver.recover_migration_target.return_value = True

        self.compute._recover_incus_bfv_migration_targets(
            context.get_admin_context())

        self.assertIsNone(candidate.task_state)
        self.assertEqual(power_state.RUNNING, candidate.power_state)
        self.assertEqual('resized', candidate.vm_state)
        self.assertEqual([
            mock.call(expected_task_state=[None]),
            mock.call(expected_task_state=task_states.REBOOTING_HARD),
        ], candidate.save.call_args_list)
        get_by_uuid.assert_called_once_with(
            mock.ANY, candidate.uuid,
            expected_attrs=['flavor', 'info_cache'])
        get_bdms.assert_called_once_with(mock.ANY, candidate.uuid)
        self.compute.driver.recover_migration_target.assert_called_once_with(
            mock.ANY, candidate,
            self.compute.network_api.get_instance_nw_info.return_value,
            block_device_info=(
                self.compute._get_instance_block_device_info.return_value))

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_recovery_failure_releases_retry_fence(
            self, get_by_uuid, get_bdms):
        candidate = mock.Mock(
            task_state=None,
            uuid='candidate',
            host=self.compute.host)
        candidate.name = 'instance-candidate'
        get_by_uuid.return_value = candidate
        recovery_candidates = (
            self.compute.driver.list_migration_recovery_candidates)
        recovery_candidates.return_value = [{
            'name': candidate.name,
            'uuid': candidate.uuid,
        }]
        self.compute.driver.needs_migration_recovery.return_value = True
        recover = self.compute.driver.recover_migration_target
        recover.side_effect = RuntimeError('rbd unavailable')

        self.compute._recover_incus_bfv_migration_targets(
            context.get_admin_context())

        self.assertIsNone(candidate.task_state)
        self.assertEqual([
            mock.call(expected_task_state=[None]),
            mock.call(expected_task_state=task_states.REBOOTING_HARD),
        ], candidate.save.call_args_list)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_skips_instance_with_active_task(self, get_by_uuid):
        instance = mock.Mock()
        instance.task_state = task_states.MIGRATING
        instance.uuid = 'candidate'
        instance.host = self.compute.host
        instance.name = 'instance-candidate'
        get_by_uuid.return_value = instance
        recovery_candidates = (
            self.compute.driver.list_migration_recovery_candidates)
        recovery_candidates.return_value = [{
            'name': instance.name,
            'uuid': instance.uuid,
        }]

        self.compute._recover_incus_bfv_migration_targets(
            context.get_admin_context())

        self.compute.driver.needs_migration_recovery.assert_not_called()
        self.compute.driver.recover_migration_target.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_recovery_preserves_stopped_power_state(
            self, get_by_uuid, get_bdms):
        candidate = mock.Mock(
            task_state=None,
            uuid='candidate',
            host=self.compute.host)
        candidate.name = 'instance-candidate'
        get_by_uuid.return_value = candidate
        recovery_candidates = (
            self.compute.driver.list_migration_recovery_candidates)
        recovery_candidates.return_value = [{
            'name': candidate.name,
            'uuid': candidate.uuid,
        }]
        self.compute.driver.needs_migration_recovery.return_value = True
        self.compute.driver.recover_migration_target.return_value = False
        candidate.vm_state = vm_states.ACTIVE

        self.compute._recover_incus_bfv_migration_targets(
            context.get_admin_context())

        self.assertEqual(power_state.SHUTDOWN, candidate.power_state)
        self.assertEqual(vm_states.STOPPED, candidate.vm_state)
        self.assertIsNone(candidate.task_state)

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_recovery_restores_active_vm_state(
            self, get_by_uuid, get_bdms):
        candidate = mock.Mock(
            task_state=None,
            uuid='candidate',
            host=self.compute.host,
            vm_state=vm_states.STOPPED)
        candidate.name = 'instance-candidate'
        get_by_uuid.return_value = candidate
        self.compute.driver.list_migration_recovery_candidates.return_value = [
            {'name': candidate.name, 'uuid': candidate.uuid}]
        self.compute.driver.needs_migration_recovery.return_value = True
        self.compute.driver.recover_migration_target.return_value = True

        self.compute._recover_incus_bfv_migration_targets(
            context.get_admin_context())

        self.assertEqual(power_state.RUNNING, candidate.power_state)
        self.assertEqual(vm_states.ACTIVE, candidate.vm_state)
        self.assertIsNone(candidate.task_state)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_recovery_batches_candidates_with_rotating_cursor(
            self, get_by_uuid):
        self.flags(migration_recovery_batch_size=1, group='incus')
        first = mock.Mock(
            task_state=None, uuid='first', host=self.compute.host)
        first.name = 'instance-first'
        second = mock.Mock(
            task_state=None, uuid='second', host=self.compute.host)
        second.name = 'instance-second'
        get_by_uuid.side_effect = [first, second]
        self.compute.driver.list_migration_recovery_candidates.return_value = [
            {'name': first.name, 'uuid': first.uuid},
            {'name': second.name, 'uuid': second.uuid},
        ]
        self.compute.driver.needs_migration_recovery.return_value = False

        self.compute._recover_incus_bfv_migration_targets(
            context.get_admin_context())
        self.compute._recover_incus_bfv_migration_targets(
            context.get_admin_context())

        self.assertEqual([
            mock.call(
                mock.ANY, first.uuid,
                expected_attrs=['flavor', 'info_cache']),
            mock.call(
                mock.ANY, second.uuid,
                expected_attrs=['flavor', 'info_cache']),
        ], get_by_uuid.call_args_list)

    @mock.patch.object(manager.objects.Instance, 'get_by_uuid')
    def test_disabled_recovery_does_not_query_database(self, get_by_uuid):
        self.flags(migration_auto_recovery=False, group='incus')

        self.compute._recover_incus_bfv_migration_targets(
            context.get_admin_context())

        candidates = self.compute.driver.list_migration_recovery_candidates
        candidates.assert_not_called()
        get_by_uuid.assert_not_called()


class IncusAbandonedMigrationReservationTest(test.NoDBTestCase):
    """Only a reservation Nova can no longer consume may be released."""

    def setUp(self):
        super().setUp()
        self.flags(migration_auto_recovery=True, group='incus')
        self.compute = manager.IncusComputeManager.__new__(
            manager.IncusComputeManager)
        self.compute.host = 'compute-1'
        self.compute.driver = mock.Mock()
        self.compute.driver.get_available_nodes.return_value = ['compute-1']
        self.candidate = {
            'token': '40000000-0000-0000-0000-000000000004',
            'name': 'instance-0000002f',
            'idmap_base': 500000000,
            'idmap_size': 65536,
        }
        self.listing = (
            self.compute.driver.
            list_unstarted_migration_attempt_reservations)
        self.listing.return_value = [self.candidate]
        self.release = (
            self.compute.driver.
            release_unstarted_migration_attempt_reservation)

    def _run(self, instance_names=(), migration_names=()):
        instances = []
        for name in instance_names:
            instance = mock.Mock()
            instance.name = name
            instances.append(instance)

        migrations = []
        migration_instances = {}
        for index, name in enumerate(migration_names):
            uuid = '5000000%d-0000-0000-0000-000000000005' % index
            migration = mock.Mock()
            migration.instance_uuid = uuid
            migrations.append(migration)
            instance = mock.Mock()
            instance.name = name
            migration_instances[uuid] = instance

        with mock.patch.object(
                manager.objects.InstanceList, 'get_by_host',
                return_value=instances), \
            mock.patch.object(
                manager.objects.MigrationList,
                'get_in_progress_by_host_and_node',
                return_value=migrations), \
            mock.patch.object(
                manager.objects.Instance, 'get_by_uuid',
                side_effect=lambda ctx, uuid, **kw: (
                    migration_instances[uuid])):
            self.compute._release_abandoned_incus_migration_reservations(
                context.get_admin_context())

    def test_release_requires_two_unchanged_observations(self):
        self._run()
        self.release.assert_not_called()

        self._run()
        self.release.assert_called_once_with(self.candidate)

    def test_in_progress_migration_protects_the_reservation(self):
        self._run(migration_names=[self.candidate['name']])
        self._run(migration_names=[self.candidate['name']])
        self.release.assert_not_called()

    def test_local_instance_protects_the_reservation(self):
        self._run(instance_names=[self.candidate['name']])
        self._run(instance_names=[self.candidate['name']])
        self.release.assert_not_called()

    def test_a_changed_reservation_restarts_the_observation(self):
        self._run()
        started = dict(self.candidate, idmap_base=600000000)
        self.listing.return_value = [started]
        self._run()
        self.release.assert_not_called()

        self._run()
        self.release.assert_called_once_with(started)

    def test_a_reservation_that_disappears_is_forgotten(self):
        self._run()
        self.listing.return_value = []
        self._run()
        self.listing.return_value = [self.candidate]
        self._run()
        self.release.assert_not_called()

    def test_unknown_nova_state_retains_every_reservation(self):
        self._run()
        with mock.patch.object(
                manager.objects.InstanceList, 'get_by_host',
                side_effect=Exception('boom')):
            self.compute._release_abandoned_incus_migration_reservations(
                context.get_admin_context())
        self.release.assert_not_called()

    def test_disabled_auto_recovery_does_not_list(self):
        self.flags(migration_auto_recovery=False, group='incus')
        self._run()
        self.listing.assert_not_called()

    def test_a_failed_release_is_retried_next_pass(self):
        self.release.side_effect = Exception('boom')
        self._run()
        self._run()
        self.release.assert_called_once_with(self.candidate)

        self.release.side_effect = None
        self._run()
        self.assertEqual(2, self.release.call_count)
