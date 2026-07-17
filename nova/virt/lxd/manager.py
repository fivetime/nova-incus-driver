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

from nova.compute import manager
from nova.compute import power_state
from nova.compute import task_states
import nova.context
from nova import exception
from nova import objects
from oslo_log import log as logging
from oslo_service import periodic_task

from nova.virt.lxd import driver as incus_driver  # noqa: F401


CONF = incus_driver.CONF
LOG = logging.getLogger(__name__)


class IncusComputeManager(manager.ComputeManager):
    """Nova manager extension for fenced BFV post-claim recovery."""

    @periodic_task.periodic_task(
        spacing=CONF.incus.migration_recovery_interval)
    def _recover_incus_bfv_migration_targets(self, context):
        if not CONF.incus.migration_auto_recovery:
            return

        context = (context or nova.context.get_admin_context()).elevated()
        instances = objects.InstanceList.get_by_host(
            context, self.host, expected_attrs=['flavor', 'info_cache'])
        LOG.debug(
            'Scanning %(count)d local instances for Incus BFV recovery',
            {'count': len(instances)})
        for instance in instances:
            if instance.task_state is not None:
                LOG.debug(
                    'Skipping Incus BFV recovery candidate with task state '
                    '%(task_state)s',
                    {'task_state': instance.task_state}, instance=instance)
                continue
            recovery_claimed = False
            try:
                if not self.driver.needs_migration_recovery(instance):
                    continue
                instance.task_state = task_states.REBOOTING_HARD
                instance.save(expected_task_state=[None])
                recovery_claimed = True
                bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(
                    context, instance.uuid)
                block_device_info = self._get_instance_block_device_info(
                    context, instance, bdms=bdms)
                network_info = self.network_api.get_instance_nw_info(
                    context, instance)
                should_run = self.driver.recover_migration_target(
                    context, instance, network_info,
                    block_device_info=block_device_info)
                # Preserve VERIFY_RESIZE and its confirm/revert contract.
                # This loop repairs runtime state; it does not accept a resize
                # or rewrite Placement allocations.
                instance.power_state = (
                    power_state.RUNNING if should_run
                    else power_state.SHUTDOWN)
                instance.task_state = None
                instance.save(
                    expected_task_state=task_states.REBOOTING_HARD)
            except (exception.InstanceNotFound,
                    exception.UnexpectedTaskStateError):
                LOG.info(
                    'BFV recovery candidate changed state during scan',
                    instance=instance)
            except Exception:
                LOG.exception(
                    'Automatic BFV migration target recovery failed',
                    instance=instance)
                if recovery_claimed:
                    try:
                        instance.task_state = None
                        instance.save(
                            expected_task_state=task_states.REBOOTING_HARD)
                    except (exception.InstanceNotFound,
                            exception.UnexpectedTaskStateError):
                        LOG.info(
                            'BFV recovery candidate changed state while '
                            'releasing its retry fence',
                            instance=instance)
                    except Exception:
                        LOG.exception(
                            'Failed to release BFV recovery retry fence',
                            instance=instance)
