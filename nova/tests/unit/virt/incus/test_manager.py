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
from nova.virt.incus import manager
from nova.virt.incus import migrate_data


class IncusComputeManagerTest(test.NoDBTestCase):

    def setUp(self):
        super().setUp()
        self.flags(migration_auto_recovery=True, group='incus')
        self.flags(volume_usage_poll_interval=60)
        self.compute = manager.IncusComputeManager.__new__(
            manager.IncusComputeManager)
        self.compute.host = 'compute-1'
        self.compute.driver = mock.Mock()
        self.compute.network_api = mock.Mock()
        self.compute._get_instance_block_device_info = mock.Mock(
            return_value={'block_device_mapping': []})

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
        manager.manager.ComputeManager, '_rollback_live_migration',
        return_value=mock.sentinel.result)
    def test_rollback_live_migration_reasserts_network_after_base_cleanup(
            self, base_rollback):
        ctxt = context.get_admin_context()
        instance = mock.sentinel.instance
        data = migrate_data.IncusLiveMigrateData()

        result = self.compute._rollback_live_migration(
            ctxt, instance, 'compute-2', migrate_data=data,
            source_bdms=mock.sentinel.source_bdms)

        self.assertIs(mock.sentinel.result, result)
        base_rollback.assert_called_once_with(
            ctxt, instance, 'compute-2', migrate_data=data,
            migration_status='failed',
            source_bdms=mock.sentinel.source_bdms,
            pre_live_migration=False)
        finalize = self.compute.driver.finalize_live_migration_rollback
        finalize.assert_called_once_with(ctxt, instance, data)

    @mock.patch.object(
        manager.manager.ComputeManager, '_rollback_live_migration')
    def test_pre_live_migration_rollback_does_not_reassert_network(
            self, base_rollback):
        data = migrate_data.IncusLiveMigrateData()

        self.compute._rollback_live_migration(
            mock.sentinel.context, mock.sentinel.instance, 'compute-2',
            migrate_data=data, pre_live_migration=True)

        finalize = self.compute.driver.finalize_live_migration_rollback
        finalize.assert_not_called()

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

    @mock.patch.object(
        manager.manager.ComputeManager, 'pre_live_migration')
    def test_pre_live_migration_mounts_destination_shares(self, base_pre):
        ctxt = context.get_admin_context()
        instance = mock.Mock(uuid='00000000-0000-0000-0000-000000000001')
        shares = [mock.Mock(share_id='share-1'),
                  mock.Mock(share_id='share-2')]
        self.compute._get_share_info = mock.Mock(return_value=shares)
        self.compute._mount_share = mock.Mock()
        base_pre.return_value = mock.sentinel.migrate_data

        result = self.compute.pre_live_migration(
            ctxt, instance, mock.sentinel.disk,
            mock.sentinel.migrate_data)

        self.assertIs(mock.sentinel.migrate_data, result)
        self.assertEqual(
            [mock.call(ctxt, instance, share) for share in shares],
            self.compute._mount_share.call_args_list)
        base_pre.assert_called_once_with(
            ctxt, instance, mock.sentinel.disk,
            mock.sentinel.migrate_data)

    @mock.patch.object(
        manager.manager.ComputeManager, 'pre_live_migration')
    def test_pre_live_migration_rolls_back_mounted_shares(self, base_pre):
        ctxt = context.get_admin_context()
        instance = mock.Mock(uuid='00000000-0000-0000-0000-000000000001')
        first = mock.Mock(share_id='share-1')
        second = mock.Mock(share_id='share-2')
        self.compute._get_share_info = mock.Mock(
            return_value=[first, second])
        self.compute._mount_share = mock.Mock(
            side_effect=[None, RuntimeError('mount failed')])
        self.compute._umount_share = mock.Mock()

        self.assertRaises(
            RuntimeError,
            self.compute.pre_live_migration,
            ctxt, instance, mock.sentinel.disk,
            mock.sentinel.migrate_data)

        self.compute._umount_share.assert_called_once_with(
            ctxt, instance, first)
        base_pre.assert_not_called()

    @mock.patch.object(manager.eventlet, 'sleep')
    @mock.patch.object(
        manager.manager.ComputeManager, '_shutdown_instance')
    @mock.patch.object(
        manager.manager.ComputeManager, '_notify_volume_usage_detach')
    def test_shutdown_settles_volume_usage_before_destroy(
            self, notify_usage, shutdown, sleep):
        volume = mock.Mock(is_volume=True, volume_id='volume-1')
        local_disk = mock.Mock(is_volume=False)
        instance = mock.Mock()
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
        instance = mock.Mock()
        ctxt = context.get_admin_context()
        notify_usage.side_effect = RuntimeError('statistics unavailable')

        self.compute._shutdown_instance(ctxt, instance, [volume])

        sleep.assert_called_once_with(manager._METRICS_SETTLEMENT_DELAY)
        shutdown.assert_called_once_with(
            ctxt, instance, [volume],
            requested_networks=None, notify=True,
            try_deallocate_networks=True)

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
