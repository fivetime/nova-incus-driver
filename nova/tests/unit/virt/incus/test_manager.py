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

import dataclasses
import os
import threading
from unittest import mock

import fixtures

from nova.compute import power_state
from nova.compute import task_states
from nova import context
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
        list_intents.return_value = [self.idmap_intent]
        self.compute.driver.idmap_allocator.list_host_claims.return_value = []
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
        possible = self._host_claim(state='possible', proof=None)
        allocator = self.compute.driver.idmap_allocator
        allocator.get_host_claim.return_value = possible
        promote = (
            self.compute.driver._promote_idmap_claim_if_server_committed)
        promote.return_value = self.idmap_assignment, possible

        self.assertRaises(
            manager.incus_driver.incus_idmap.IDMapConflict,
            self.compute._delete_instance,
            mock.sentinel.context, self._idmap_instance(),
            mock.sentinel.bdms)

        base_delete.assert_not_called()
        self.compute.driver._settle_idmap_host_claim.assert_not_called()
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

        result = self.compute._delete_instance(
            mock.sentinel.context, instance, mock.sentinel.bdms)

        self.assertIs(mock.sentinel.result, result)
        allocator.claim.assert_not_called()
        allocator.request_release.assert_called_once_with(
            instance.uuid, instance.name,
            assignment=empty)
        allocator.retire_claim.assert_not_called()
        allocator.release.assert_called_once_with(self.idmap_intent)
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

    def test_idmap_full_audit_is_skipped_without_allocator(self):
        self.compute.driver.idmap_allocator = None

        self.compute._audit_incus_idmap_allocator(mock.sentinel.context)

    def test_idmap_full_audit_runs_against_allocator(self):
        allocator = self.compute.driver.idmap_allocator
        allocator.audit.return_value = [self.idmap_assignment]

        self.compute._audit_incus_idmap_allocator(mock.sentinel.context)

        allocator.audit.assert_called_once_with()

    @mock.patch.object(manager.LOG, 'critical')
    def test_idmap_full_audit_integrity_failure_is_critical(self, critical):
        allocator = self.compute.driver.idmap_allocator
        allocator.audit.side_effect = (
            manager.incus_driver.incus_idmap.IDMapIntegrityError(
                reason='corrupt reverse index'))

        self.compute._audit_incus_idmap_allocator(mock.sentinel.context)

        critical.assert_called_once()

    @mock.patch.object(manager.LOG, 'warning')
    def test_idmap_full_audit_backend_failure_is_transient(self, warning):
        allocator = self.compute.driver.idmap_allocator
        allocator.audit.side_effect = (
            manager.incus_driver.incus_idmap.IDMapBackendError(
                reason='etcd unavailable'))

        self.compute._audit_incus_idmap_allocator(mock.sentinel.context)

        warning.assert_called_once()

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

        self.compute._replay_incus_idmap_releases(
            context.get_admin_context())

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
        allocator = self.compute.driver.idmap_allocator
        allocator.list_release_intent_candidates.return_value = intents
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

        list_intents = (
            self.compute.driver.idmap_allocator.
            list_release_intent_candidates)
        list_intents.assert_not_called()
        self.compute.driver.idmap_allocator.retire_claim.assert_not_called()
        self.compute.driver.idmap_allocator.release.assert_not_called()

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

    def test_complete_live_migration_rollback_reasserts_network(self):
        ctxt = context.get_admin_context()
        instance = mock.sentinel.instance
        data = migrate_data.IncusLiveMigrateData()

        result = self.compute._complete_live_migration_rollback(
            ctxt, instance, data)

        self.assertIsNone(result)
        finalize = self.compute.driver.finalize_live_migration_rollback
        finalize.assert_called_once_with(ctxt, instance, data)

    def test_pre_live_migration_rollback_does_not_reassert_network(self):
        data = migrate_data.IncusLiveMigrateData()

        self.compute._complete_live_migration_rollback(
            mock.sentinel.context, mock.sentinel.instance, data,
            pre_live_migration=True)

        finalize = self.compute.driver.finalize_live_migration_rollback
        finalize.assert_not_called()

    def test_live_migration_check_data_uses_exact_nova_migration_uuid(self):
        token = '10000000-0000-0000-0000-000000000001'
        data = migrate_data.IncusLiveMigrateData(
            cleanup_token='20000000-0000-0000-0000-000000000002')
        migration = mock.Mock(uuid=token)

        result = self.compute._prepare_live_migration_check_data(
            mock.sentinel.context, mock.sentinel.instance, data, migration)

        self.assertIs(data, result)
        self.assertEqual(token, data.cleanup_token)

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

    def test_pre_deny_share_unmounts_without_credential_hydration(self):
        ctxt = context.get_admin_context()
        instance = mock.Mock(uuid='instance')
        share = mock.Mock(share_id='share')

        result = self.compute._pre_deny_share(ctxt, instance, share)

        self.assertIsNone(result)
        share.enhance_with_ceph_credentials.assert_not_called()
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

        result = self.compute._finish_revert_resize(
            ctxt, instance, mock.sentinel.migration,
            request_spec=mock.sentinel.request_spec)

        self.assertIs(mock.sentinel.result, result)
        self.compute._mount_all_shares.assert_called_once_with(
            ctxt, instance, [active])
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
        get_by_uuid.return_value = instance
        migration = mock.Mock(
            uuid=token, source_compute=self.compute.host,
            dest_compute='compute-2', status='error')
        get_migrations.return_value = [migration]
        candidate = {
            'name': instance.name,
            'uuid': instance.uuid,
            'operation_token': token,
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

        self.compute._recover_incus_bfv_migration_targets(
            context.get_admin_context())

        self.assertEqual(power_state.SHUTDOWN, candidate.power_state)
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
