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

from unittest import mock

from nova.compute import power_state
from nova.compute import task_states
from nova import context
from nova import test
from nova.virt.lxd import manager


class IncusComputeManagerTest(test.NoDBTestCase):

    def setUp(self):
        super().setUp()
        self.flags(migration_auto_recovery=True, group='incus')
        self.compute = manager.IncusComputeManager.__new__(
            manager.IncusComputeManager)
        self.compute.host = 'compute-1'
        self.compute.driver = mock.Mock()
        self.compute.network_api = mock.Mock()
        self.compute._get_instance_block_device_info = mock.Mock(
            return_value={'block_device_mapping': []})

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(manager.objects.InstanceList, 'get_by_host')
    def test_recovers_only_explicit_candidate(self, get_by_host, get_bdms):
        candidate = mock.Mock()
        candidate.task_state = None
        candidate.uuid = 'candidate'
        candidate.vm_state = 'resized'
        ordinary = mock.Mock()
        ordinary.task_state = None
        ordinary.uuid = 'ordinary'
        get_by_host.return_value = [candidate, ordinary]
        self.compute.driver.needs_migration_recovery.side_effect = [
            True, False]
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
        ordinary.save.assert_not_called()
        get_bdms.assert_called_once_with(mock.ANY, candidate.uuid)
        self.compute.driver.recover_migration_target.assert_called_once_with(
            mock.ANY, candidate,
            self.compute.network_api.get_instance_nw_info.return_value,
            block_device_info=(
                self.compute._get_instance_block_device_info.return_value))

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(manager.objects.InstanceList, 'get_by_host')
    def test_recovery_failure_releases_retry_fence(
            self, get_by_host, get_bdms):
        candidate = mock.Mock(task_state=None, uuid='candidate')
        get_by_host.return_value = [candidate]
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

    @mock.patch.object(manager.objects.InstanceList, 'get_by_host')
    def test_skips_instance_with_active_task(self, get_by_host):
        instance = mock.Mock()
        instance.task_state = task_states.MIGRATING
        get_by_host.return_value = [instance]

        self.compute._recover_incus_bfv_migration_targets(
            context.get_admin_context())

        self.compute.driver.needs_migration_recovery.assert_not_called()
        self.compute.driver.recover_migration_target.assert_not_called()

    @mock.patch.object(
        manager.objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(manager.objects.InstanceList, 'get_by_host')
    def test_recovery_preserves_stopped_power_state(
            self, get_by_host, get_bdms):
        candidate = mock.Mock(task_state=None, uuid='candidate')
        get_by_host.return_value = [candidate]
        self.compute.driver.needs_migration_recovery.return_value = True
        self.compute.driver.recover_migration_target.return_value = False

        self.compute._recover_incus_bfv_migration_targets(
            context.get_admin_context())

        self.assertEqual(power_state.SHUTDOWN, candidate.power_state)
        self.assertIsNone(candidate.task_state)

    @mock.patch.object(manager.objects.InstanceList, 'get_by_host')
    def test_disabled_recovery_does_not_query_database(self, get_by_host):
        self.flags(migration_auto_recovery=False, group='incus')

        self.compute._recover_incus_bfv_migration_targets(
            context.get_admin_context())

        get_by_host.assert_not_called()
