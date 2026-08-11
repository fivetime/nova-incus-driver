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
import hashlib
import os
import random
import time

import eventlet
from nova.compute import manager
from nova.compute import power_state
from nova.compute import task_states
from nova.compute import vm_states
import nova.context
from nova import exception
from nova import objects
from nova.objects import fields as obj_fields
from nova import utils
from oslo_concurrency import lockutils
from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_service import periodic_task
from oslo_utils import uuidutils

from nova.virt.incus import driver as incus_driver  # noqa: F401
from nova.virt.incus import migrate_data as incus_migrate_data
from nova.virt import node as virt_node


CONF = incus_driver.CONF
LOG = logging.getLogger(__name__)

# Incus caches /1.0/metrics for eight seconds. Final settlement must cross
# that window or an immediate detach can reuse counters from the last poll.
_METRICS_SETTLEMENT_DELAY = 9
_IDMAP_RELEASE_REPLAY_INTERVAL = 60
_IDMAP_RELEASE_REPLAY_BATCH = 100
_FAILED_BUILD_CLEANUP_BARRIER_KEY = 'incus_failed_build_cleanup_barrier'
_FAILED_BUILD_CLEANUP_BARRIER_VERSION = 1
_FAILED_BUILD_RELEASE_NETWORK = 1 << 0
_FAILED_BUILD_RELEASE_CINDER = 1 << 1
_FAILED_BUILD_RELEASE_HOST = 1 << 2
_FAILED_BUILD_RELEASE_PLACEMENT = 1 << 3
_FAILED_BUILD_RELEASE_ALL = (
    _FAILED_BUILD_RELEASE_NETWORK |
    _FAILED_BUILD_RELEASE_CINDER |
    _FAILED_BUILD_RELEASE_HOST |
    _FAILED_BUILD_RELEASE_PLACEMENT)
_SYSTEM_METADATA_VALUE_MAX_LENGTH = 255
_TERMINAL_MIGRATION_STATUSES = frozenset({
    'cancelled', 'completed', 'confirmed', 'done', 'error', 'failed',
    'reverted',
})
_VOLUME_RECOVERY_PHASES = frozenset({
    'attach-pending', 'connecting', 'connected', 'rolled-back',
    'attach-disconnecting', 'attach-disconnected', 'disconnecting',
    'disconnected', 'detach-pending', 'intent-conflict',
})


def _valid_volume_recovery_phase(phase):
    if phase in _VOLUME_RECOVERY_PHASES:
        return True
    prefix = 'rotation-'
    return (
        isinstance(phase, str) and phase.startswith(prefix) and
        phase[len(prefix):] in
        incus_driver._COLD_ATTACHMENT_ROTATION_PHASES)


def _idmap_release_lock_name(instance_uuid):
    return incus_driver._idmap_host_claim_lock_name(instance_uuid)


def _idmap_release_lock_path():
    # Nova creates state_path before the compute service starts. Keeping the
    # lock file there avoids introducing another host-local durable store;
    # etcd CAS remains the safety authority.
    return incus_driver._idmap_host_claim_lock_path()


def _share_recovery_lock_name(instance_uuid):
    return 'incus-share-recovery-{}'.format(instance_uuid)


def _volume_manager_transaction_lock_name(instance_uuid, volume_id):
    """Serialize Nova's managed attach/detach transaction with recovery."""
    return incus_driver._volume_manager_transaction_lock_name(
        instance_uuid, volume_id)


def _volume_manager_transaction_lock_path():
    return incus_driver._volume_operation_lock_path()


def _attachment_connection_info(attachment):
    value = attachment.get('connection_info') if isinstance(
        attachment, dict) else None
    return value if isinstance(value, dict) else {}


def _attachment_status(attachment):
    if not isinstance(attachment, dict):
        return None
    return attachment.get('status') or _attachment_connection_info(
        attachment).get('status')


def _attachment_instance_uuid(attachment):
    if not isinstance(attachment, dict):
        return None
    return attachment.get('instance') or _attachment_connection_info(
        attachment).get('instance')


def _attachment_volume_id(attachment):
    if not isinstance(attachment, dict):
        return None
    connection_info = _attachment_connection_info(attachment)
    data = connection_info.get('data') or {}
    return (
        attachment.get('volume_id') or
        connection_info.get('volume_id') or data.get('volume_id') or
        connection_info.get('serial'))


def _validated_attachment_identity(attachment):
    """Return a complete Cinder attachment identity or fail closed."""
    if not isinstance(attachment, dict):
        raise exception.InvalidVolume(
            reason='Cinder returned a malformed attachment record')
    attachment_id = attachment.get('id')
    volume_id = _attachment_volume_id(attachment)
    instance_uuid = _attachment_instance_uuid(attachment)
    status = _attachment_status(attachment)
    if not uuidutils.is_uuid_like(attachment_id):
        raise exception.InvalidVolume(
            reason='Cinder attachment has no valid attachment ID')
    if not uuidutils.is_uuid_like(volume_id):
        raise exception.InvalidVolume(
            reason='Cinder attachment has no valid volume ID')
    if not uuidutils.is_uuid_like(instance_uuid):
        raise exception.InvalidVolume(
            reason='Cinder attachment has no valid instance ID')
    if not isinstance(status, str) or not status:
        raise exception.InvalidVolume(
            reason='Cinder attachment has no valid lifecycle status')
    return attachment_id, volume_id, instance_uuid, status


def _validated_volume_ownership(volume, expected_volume_id):
    """Decode Cinder's all-project volume detail ownership view."""
    if not isinstance(volume, dict):
        raise exception.InvalidVolume(
            reason='Cinder returned a malformed volume record')
    volume_id = volume.get('id')
    status = volume.get('status')
    if (volume_id != expected_volume_id or
            not uuidutils.is_uuid_like(volume_id)):
        raise exception.InvalidVolume(
            reason='Cinder returned another volume during recovery')
    if not isinstance(status, str) or not status:
        raise exception.InvalidVolume(
            reason='Cinder volume has no valid lifecycle status')
    attachments = volume.get('attachments', {})
    if not isinstance(attachments, dict):
        raise exception.InvalidVolume(
            reason='Cinder volume has a malformed attachment inventory')
    refs = []
    seen = set()
    for instance_uuid, attachment in attachments.items():
        if (not uuidutils.is_uuid_like(instance_uuid) or
                not isinstance(attachment, dict)):
            raise exception.InvalidVolume(
                reason='Cinder volume has a malformed attachment owner')
        attachment_id = attachment.get('attachment_id')
        if (not uuidutils.is_uuid_like(attachment_id) or
                attachment_id in seen):
            raise exception.InvalidVolume(
                reason='Cinder volume has an invalid attachment ID')
        seen.add(attachment_id)
        refs.append({
            'id': attachment_id,
            'instance_uuid': instance_uuid,
            'mountpoint': attachment.get('mountpoint'),
        })
    attach_status = volume.get('attach_status')
    expected_attach_status = 'attached' if refs else 'detached'
    if attach_status != expected_attach_status:
        raise exception.InvalidVolume(
            reason='Cinder volume status and attachment inventory disagree')
    return status, refs


def _optional_bdm_connection_info(bdm):
    """Decode optional durable Nova BDM connection information."""
    value = getattr(bdm, 'connection_info', None)
    if value is None or value == '':
        return None
    if isinstance(value, str):
        try:
            value = jsonutils.loads(value)
        except (TypeError, ValueError) as exc:
            raise exception.InvalidVolume(
                reason='Nova BDM connection information is invalid: %s' %
                       exc)
    if not isinstance(value, dict):
        raise exception.InvalidVolume(
            reason='Nova BDM connection information is invalid')
    return value or None


def _bdm_connection_info(bdm):
    """Decode the durable Nova BDM connection information or fail closed."""
    value = _optional_bdm_connection_info(bdm)
    if value is None:
        raise exception.InvalidVolume(
            reason='Nova BDM has no durable Cinder connection information')
    return value


def _canonical_attachment_connection_info(
        connection_info, volume_id, instance_uuid):
    """Return stable attachment identity, excluding lifecycle-only fields."""
    if not isinstance(connection_info, dict):
        raise exception.InvalidVolume(
            reason='Cinder attachment connection information is invalid')
    protocol = connection_info.get('driver_volume_type')
    data = connection_info.get('data')
    if not isinstance(protocol, str) or not protocol or not isinstance(
            data, dict):
        raise exception.InvalidVolume(
            reason='Cinder attachment connection information is incomplete')
    identity = (
        connection_info.get('serial') or
        connection_info.get('volume_id') or data.get('volume_id'))
    if identity != volume_id:
        raise exception.InvalidVolume(
            reason='Cinder connection information identifies another volume')
    if connection_info.get('instance') != instance_uuid:
        raise exception.InvalidVolume(
            reason='Cinder connection information identifies another '
                   'instance')

    canonical = dict(connection_info)
    canonical['data'] = dict(data)
    # Cinder adds the exact attachment UUID after connector initialization;
    # the journal is intentionally written before that lifecycle transition.
    # Ownership is validated separately against the exact attachment record.
    canonical['data'].pop('attachment_id', None)
    # os-brick's durable journal sanitizer omits null values. A null QoS
    # payload is therefore equivalent to the field being absent, while a
    # non-empty QoS contract remains part of the transport identity.
    if canonical['data'].get('qos_specs') is None:
        canonical['data'].pop('qos_specs', None)
    for key in ('status', 'attached_at', 'detached_at'):
        canonical.pop(key, None)
    canonical.pop('volume_id', None)
    canonical['serial'] = volume_id
    return canonical


class IncusComputeManager(manager.ComputeManager):
    """Nova manager extension for fenced BFV post-claim recovery."""

    def _cold_source_recovery_evidence(self, instance):
        """Return exact cold-source evidence owned by this instance."""
        volume_ids = set()
        operation_tokens = set()
        for candidate in (
                self.driver.list_volume_journal_recovery_candidates()):
            if candidate.get('uuid') != instance.uuid:
                continue
            for volume_id in candidate.get('volume_ids', ()):
                intent = self.driver.get_managed_volume_attach_intent(
                    instance, volume_id)
                rotation = self.driver.get_cold_attachment_rotation(
                    instance, volume_id)
                if (rotation is None and
                        (intent is None or
                         intent.get('operation_direction') !=
                         'cold-source-restore')):
                    continue
                volume_ids.add(volume_id)
                if intent is not None:
                    operation_tokens.add(intent.get('operation_token'))
                if rotation is not None:
                    operation_tokens.add(rotation.get('operation_token'))

        generation_candidates = []
        generation = (
            self.driver.get_source_volume_generation_recovery_candidate(
                instance))
        if generation is not None:
            generation_candidates.append(generation)
            operation_tokens.add(generation.get('operation_token'))

        operation_tokens.discard(None)
        return volume_ids, operation_tokens, generation_candidates

    def _maybe_finalize_failed_cold_source_generation(
            self, instance, operation_token):
        deferred = getattr(
            self, '_deferred_cold_source_generation_instances', set())
        if instance.uuid in deferred:
            return False
        return self.driver.finalize_failed_cold_source_volume_generation(
            instance, operation_token)

    def _restore_interrupted_cold_source_allocations(
            self, context, instance, migration):
        """Move Placement ownership back to the source with exact replay."""
        source = self.reportclient.get_provider_by_name(
            context, migration.source_node)
        destination = self.reportclient.get_provider_by_name(
            context, migration.dest_node)
        source_uuid = source.get('uuid') if isinstance(source, dict) else None
        destination_uuid = (
            destination.get('uuid') if isinstance(destination, dict) else None)
        if (not uuidutils.is_uuid_like(source_uuid) or
                not uuidutils.is_uuid_like(destination_uuid) or
                source_uuid == destination_uuid):
            raise exception.MigrationError(
                reason='Interrupted cold migration has invalid Placement '
                       'providers')

        migration_payload = self.reportclient.get_allocs_for_consumer(
            context, migration.uuid)
        migration_allocations = migration_payload.get(
            'allocations') if isinstance(migration_payload, dict) else None
        if not isinstance(migration_allocations, dict):
            raise exception.MigrationError(
                reason='Placement returned an invalid migration allocation '
                       'document')
        move_error = None
        if migration_allocations:
            try:
                self._revert_allocation(context, instance, migration)
            except Exception as exc:
                # Placement has no transaction spanning its response and the
                # compute DB. Treat a lost response as success only after an
                # authoritative read proves the exact final owners below.
                move_error = exc

        migration_payload = self.reportclient.get_allocs_for_consumer(
            context, migration.uuid)
        instance_payload = self.reportclient.get_allocs_for_consumer(
            context, instance.uuid)
        migration_allocations = migration_payload.get(
            'allocations') if isinstance(migration_payload, dict) else None
        instance_allocations = instance_payload.get(
            'allocations') if isinstance(instance_payload, dict) else None
        exact = (
            migration_allocations == {} and
            isinstance(instance_allocations, dict) and
            source_uuid in instance_allocations and
            destination_uuid not in instance_allocations)
        if not exact:
            if move_error is not None:
                raise move_error
            raise exception.MigrationError(
                reason='Interrupted cold migration Placement ownership is '
                       'not durably restored to the source')
        return instance_allocations

    def _recover_interrupted_cold_source_rotation(self, context, instance):
        """Rollback a cold-source attachment rotation before Nova startup."""
        if instance.task_state != task_states.RESIZE_MIGRATING:
            return None
        volume_ids, operation_tokens, generations = (
            self._cold_source_recovery_evidence(instance))
        if not volume_ids and not generations:
            return None

        migration_context = getattr(instance, 'migration_context', None)
        migration_id = getattr(migration_context, 'migration_id', None)
        if (not isinstance(migration_id, int) or
                isinstance(migration_id, bool) or migration_id <= 0):
            raise exception.MigrationError(
                reason='Interrupted cold attachment rotation has no '
                       'Migration ID')
        migration = objects.Migration.get_by_id_and_instance(
            context, migration_id, instance.uuid)
        if (getattr(migration, 'uuid', None) not in operation_tokens or
                operation_tokens != {migration.uuid} or
                migration.source_compute != self.host or
                migration.dest_compute == self.host or
                migration.status not in (
                    'migrating', 'post-migrating', 'cancelled', 'error',
                    'failed', 'reverted')):
            raise exception.MigrationError(
                reason='Interrupted cold attachment rotation has no exact '
                       'failed source owner')
        if any(
                candidate.get('operation_token') != migration.uuid or
                candidate.get('migration_uuid') != migration.uuid
                for candidate in generations):
            raise exception.MigrationError(
                reason='Interrupted cold source generation owner changed')

        # Upstream startup rolls RESIZE_MIGRATING back for safety. Publish that
        # decision before replay so the journal cannot mistake the replacement
        # attachment for a still-active target handoff.
        if migration.status in ('migrating', 'post-migrating'):
            migration.status = 'error'
            migration.save()

        deferred = getattr(
            self, '_deferred_cold_source_generation_instances', None)
        if deferred is None:
            deferred = set()
            self._deferred_cold_source_generation_instances = deferred
        deferred.add(instance.uuid)
        try:
            self._recover_incus_volume_journals(context)
        finally:
            deferred.discard(instance.uuid)

        remaining_volumes, remaining_tokens, generations = (
            self._cold_source_recovery_evidence(instance))
        if remaining_volumes:
            LOG.critical(
                'Retaining interrupted cold migration for instance %s until '
                'all attachment rotations and source mappings are exact',
                instance.uuid, instance=instance)
            return False
        if (remaining_tokens != {migration.uuid} or len(generations) != 1 or
                generations[0].get('operation_token') != migration.uuid or
                generations[0].get('migration_uuid') != migration.uuid):
            raise exception.MigrationError(
                reason='Recovered cold source has no durable runtime marker')

        old_vm_state = instance.system_metadata.get(
            'old_vm_state', vm_states.ACTIVE)
        self.driver.restore_failed_cold_source_storage_ownership(
            instance, migration.uuid)
        source_allocations = self._restore_interrupted_cold_source_allocations(
            context, instance, migration)
        request_spec = objects.RequestSpec.get_by_instance_uuid(
            context, instance.uuid)
        provider_mappings = self._fill_provider_mapping_based_on_allocs(
            context, source_allocations, request_spec)
        self.network_api.setup_networks_on_host(
            context, instance, migration.source_compute)
        with utils.temporary_mutation(
                migration, dest_compute=migration.source_compute):
            self.network_api.migrate_instance_finish(
                context, instance, migration,
                provider_mappings=provider_mappings)

        bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(
            context, instance.uuid)
        block_device_info = self._get_instance_block_device_info(
            context, instance, bdms=bdms)
        network_info = self.network_api.get_instance_nw_info(
            context, instance)
        if old_vm_state != vm_states.STOPPED:
            self.driver.power_on(
                context, instance, network_info, block_device_info)

        # The runtime and every cross-service owner are now source-local.
        # Commit the migration outcome first so a crash before the instance
        # save safely re-enters this exact recovery path.
        migration.status = 'reverted'
        migration.save()
        instance.drop_migration_context()
        instance.old_flavor = None
        instance.new_flavor = None
        instance.system_metadata.pop('old_vm_state', None)
        instance.power_state = self._get_power_state(instance)
        instance.vm_state = (
            vm_states.STOPPED
            if old_vm_state == vm_states.STOPPED else vm_states.ACTIVE)
        instance.task_state = None
        instance.save(expected_task_state=task_states.RESIZE_MIGRATING)
        if not self.driver.finalize_failed_cold_source_volume_generation(
                instance, migration.uuid):
            LOG.critical(
                'Cold source runtime was restored but its generation marker '
                'could not yet be retired; periodic recovery will retry',
                instance=instance)
        return True

    def _init_instance(self, context, instance):
        """Recover cold attachment rotation before generic startup rollback."""
        try:
            recovered = self._recover_interrupted_cold_source_rotation(
                context, instance)
        except Exception:
            # Generic startup unconditionally clears RESIZE_MIGRATING even if
            # finish_revert_migration fails. Preserve the task and all durable
            # storage evidence instead of starting a partially owned source.
            LOG.exception(
                'Interrupted Incus cold attachment rotation is unresolved; '
                'refusing compute startup before generic instance recovery',
                instance=instance)
            raise
        if recovered is False:
            raise exception.MigrationError(
                reason='Interrupted Incus cold attachment rotation remains '
                       'unresolved; refusing compute startup')
        if recovered:
            instance = objects.Instance.get_by_uuid(context, instance.uuid)
        return super()._init_instance(context, instance)

    def init_host(self, service_ref):
        """Resolve volumes this compute left mid-detach when it died."""
        result = super().init_host(service_ref)
        try:
            self._roll_back_interrupted_detaches(
                nova.context.get_admin_context())
        except Exception:
            LOG.exception(
                'Failed to reconcile interrupted Cinder detaches at startup')
        return result

    def _roll_back_interrupted_detaches(self, context):
        """Return a volume abandoned in 'detaching' to a definite state.

        Nova marks a volume 'detaching' before the virt driver is entered,
        so a process killed in that window leaves Cinder in an
        intermediate state while the block device mapping and the host
        mapping both still say attached. Nothing converges it: the API
        refuses a retry because the volume is not 'in-use', and the volume
        journal is empty because the driver never ran.

        Host and Nova state are consistent here -- the guest still has its
        volume and never stopped using it -- so the detach is treated as
        not having happened, which is the same rule migration follows when
        it fails. roll_detaching is what Nova's own detach failure path
        calls; a killed process simply never reaches it.
        """
        for instance in objects.InstanceList.get_by_host(
                context, self.host, expected_attrs=[]):
            if instance.task_state is not None:
                continue
            try:
                bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(
                    context, instance.uuid)
            except Exception:
                LOG.exception(
                    'Cannot read block device mappings while reconciling '
                    'interrupted detaches', instance=instance)
                continue
            for bdm in bdms:
                if not bdm.volume_id or bdm.deleted:
                    continue
                try:
                    volume = self.volume_api.get(context, bdm.volume_id)
                except Exception:
                    LOG.exception(
                        'Cannot read Cinder volume %s while reconciling '
                        'interrupted detaches', bdm.volume_id,
                        instance=instance)
                    continue
                if volume.get('status') != 'detaching':
                    continue
                if not self.driver.holds_volume_attachment(
                        instance, bdm.volume_id):
                    # The driver did run and released host state, so the
                    # detach is mid-flight rather than abandoned. Its own
                    # journal recovery owns that case.
                    continue
                LOG.warning(
                    'Returning volume %s to in-use: its detach did not '
                    'survive this compute restart and the guest never lost '
                    'it', bdm.volume_id, instance=instance)
                try:
                    self.volume_api.roll_detaching(context, bdm.volume_id)
                except Exception:
                    LOG.exception(
                        'Failed to roll back the interrupted detach of '
                        'volume %s', bdm.volume_id, instance=instance)
                    continue
                try:
                    intent = self.driver.get_managed_volume_detach_intent(
                        instance, bdm.volume_id)
                    if intent is not None:
                        self.driver.cancel_managed_volume_detach(
                            instance, bdm.volume_id, intent)
                except Exception:
                    LOG.exception(
                        'Failed to retire the rolled-back managed detach '
                        'intent for Cinder volume %s', bdm.volume_id,
                        instance=instance)

    def _attach_volume(self, context, instance, bdm):
        """Serialize Nova's complete managed attach with its reconciler."""
        with lockutils.lock(
                _volume_manager_transaction_lock_name(
                    instance.uuid, bdm.volume_id),
                external=True,
                lock_path=_volume_manager_transaction_lock_path()):
            return self._attach_volume_locked(context, instance, bdm)

    def _attach_volume_locked(self, context, instance, bdm):
        """Commit the Incus attach journal after Nova and Cinder commit.

        The driver returns with a durable ``connected`` journal because the
        upstream block-device flow persists the BDM and completes the Cinder
        attachment only afterwards.  Do not propagate final journal cleanup
        failure: at this point Cinder is formally attached, and the periodic
        reconciler must validate and remove the journal without causing
        ComputeManager.attach_volume() to delete the live BDM.
        """
        attachment_id = getattr(bdm, 'attachment_id', None)
        # ``bdm`` is Nova's DriverVolumeBlockDevice here. Its canonical
        # guest path is exposed as ``mount_device``; ``device_name`` belongs
        # to the underlying BlockDeviceMapping object.
        mountpoint = getattr(bdm, 'mount_device', None)
        intent = self.driver.prepare_managed_volume_attach(
            instance, bdm.volume_id, attachment_id, mountpoint)
        result = super()._attach_volume(context, instance, bdm)
        connection_info = bdm.get('connection_info') or {}
        try:
            if not isinstance(connection_info, dict):
                raise exception.InvalidVolume(
                    reason='Nova BDM has no connection information after '
                           'Cinder attachment completion')
            with lockutils.lock(
                    incus_driver._volume_topology_lock_name(instance),
                    external=True,
                    lock_path=incus_driver._volume_topology_lock_path()):
                self.driver.confirm_connected_volume_journal(
                    instance, bdm.volume_id, connection_info,
                    expected_mountpoint=mountpoint)
                self.driver.cancel_managed_volume_attach(
                    instance, bdm.volume_id, intent)
        except Exception:
            LOG.critical(
                'Cinder volume %(volume)s is formally attached but its Incus '
                'connected journal could not be committed; retaining the BDM '
                'and journal for fail-closed periodic recovery',
                {'volume': bdm.volume_id}, instance=instance, exc_info=True)
        return result

    def _detach_volume(self, context, bdm, instance, destroy_bdm=True,
                       attachment_id=None):
        """Serialize Nova's complete managed detach with its reconciler."""
        with lockutils.lock(
                _volume_manager_transaction_lock_name(
                    instance.uuid, bdm.volume_id),
                external=True,
                lock_path=_volume_manager_transaction_lock_path()):
            return self._detach_volume_locked(
                context, bdm, instance, destroy_bdm=destroy_bdm,
                attachment_id=attachment_id)

    def _detach_volume_locked(
            self, context, bdm, instance, destroy_bdm=True,
            attachment_id=None):
        """Fence Nova-managed Cinder/BDM cleanup around driver detach."""
        volume_id = bdm.volume_id
        bdm_attachment_id = getattr(bdm, 'attachment_id', None)
        effective_attachment_id = attachment_id or bdm_attachment_id
        if (attachment_id and bdm_attachment_id and
                attachment_id != bdm_attachment_id):
            raise exception.InvalidVolume(
                reason='Nova detach request and BDM attachment IDs disagree')
        mountpoint = getattr(bdm, 'device_name', None)
        intent = self.driver.prepare_managed_volume_detach(
            instance, volume_id, effective_attachment_id, bool(destroy_bdm),
            mountpoint)
        try:
            result = super()._detach_volume(
                context, bdm, instance, destroy_bdm=destroy_bdm,
                attachment_id=attachment_id)
        except Exception:
            try:
                phase = self.driver.get_volume_journal_phase(
                    instance, volume_id)
            except Exception:
                LOG.critical(
                    'Cannot determine whether managed detach intent for '
                    'Cinder volume %s is safe to cancel; retaining it',
                    volume_id, instance=instance, exc_info=True)
            else:
                if phase is None:
                    try:
                        self.driver.cancel_managed_volume_detach(
                            instance, volume_id, intent)
                    except Exception:
                        LOG.critical(
                            'Failed to cancel pre-driver managed detach '
                            'intent for Cinder volume %s', volume_id,
                            instance=instance, exc_info=True)
            raise

        try:
            self.driver.finalize_disconnected_volume_journal(
                instance, volume_id)
            self.driver.cancel_managed_volume_detach(
                instance, volume_id, intent)
        except Exception:
            # Cinder/BDM cleanup has committed. Retain both durable records for
            # periodic finalization rather than turning success into a retry
            # that could target a later owner.
            LOG.critical(
                'Nova detached Cinder volume %s but could not retire its '
                'Incus recovery evidence; periodic recovery will retry',
                volume_id, instance=instance, exc_info=True)
        return result

    @contextlib.contextmanager
    def _cold_rotation_volume_locks(self, instance, volume_id):
        with lockutils.lock(
                _volume_manager_transaction_lock_name(
                    instance.uuid, volume_id),
                external=True,
                lock_path=_volume_manager_transaction_lock_path()):
            with lockutils.lock(
                    incus_driver._volume_topology_lock_name(instance),
                    external=True,
                    lock_path=incus_driver._volume_topology_lock_path()):
                with lockutils.lock(
                        incus_driver._volume_operation_lock_name(volume_id),
                        external=True,
                        lock_path=incus_driver._volume_operation_lock_path()):
                    yield

    def _cold_source_rotation_owner(self, context, instance):
        """Return the exact active cold migration or fail closed."""
        if instance.task_state != task_states.RESIZE_MIGRATING:
            return None
        migration_context = getattr(instance, 'migration_context', None)
        migration_id = getattr(migration_context, 'migration_id', None)
        if (not isinstance(migration_id, int) or
                isinstance(migration_id, bool) or migration_id <= 0):
            raise exception.MigrationError(
                reason='Cold attachment rotation has no Migration ID')
        migration = objects.Migration.get_by_id_and_instance(
            context, migration_id, instance.uuid)
        token = self.driver.get_cold_source_migration_token(instance)
        if (getattr(migration, 'uuid', None) != token or
                migration.source_compute != self.host or
                migration.dest_compute == self.host or
                migration.status != 'migrating'):
            raise exception.MigrationError(
                reason='Cold attachment rotation has no exact active source '
                       'owner')
        return migration

    @staticmethod
    def _cold_rotation_is_boot_volume(bdm):
        try:
            return int(getattr(bdm, 'boot_index', -1)) == 0
        except (TypeError, ValueError):
            return False

    def _cold_rotation_attachment_inventory(
            self, context, instance, volume_id):
        """Return visible attachments for conflict detection only."""
        attachments = self.volume_api.attachment_get_all(
            context, volume_id=volume_id)
        if not isinstance(attachments, list):
            raise exception.InvalidVolume(
                reason='Cinder returned an invalid attachment inventory')
        attachment_ids = []
        for attachment in attachments:
            attachment_id, actual_volume, unused_instance, unused_status = (
                _validated_attachment_identity(attachment))
            if actual_volume != volume_id:
                raise exception.InvalidVolume(
                    reason='Cinder attachment inventory contains another '
                           'volume')
            attachment_ids.append(attachment_id)
        if len(set(attachment_ids)) != len(attachment_ids):
            raise exception.InvalidVolume(
                reason='Cinder attachment inventory contains duplicates')
        return sorted(attachment_ids)

    def _prepare_cold_attachment_rotation_locked(
            self, context, instance, bdm, migration):
        volume_id = getattr(bdm, 'volume_id', None)
        attachment_id = getattr(bdm, 'attachment_id', None)
        mountpoint = getattr(bdm, 'device_name', None)
        boot_volume = self._cold_rotation_is_boot_volume(bdm)
        if (not getattr(bdm, 'is_volume', False) or
                not uuidutils.is_uuid_like(volume_id) or
                not uuidutils.is_uuid_like(attachment_id) or
                not isinstance(mountpoint, str) or not mountpoint):
            raise exception.InvalidVolume(
                reason='Cold migration contains an incomplete Cinder BDM')

        intent = self.driver.get_managed_volume_attach_intent(
            instance, volume_id)
        if intent is None:
            if not boot_volume:
                raise exception.InvalidVolume(
                    reason='Cold source data volume has no detach owner')
            intent = self.driver.prepare_managed_volume_attach(
                instance, volume_id, attachment_id, mountpoint,
                operation_kind='migration',
                operation_token=migration.uuid,
                operation_direction='cold-source-restore',
                operation_migration_uuid=migration.uuid,
                boot_volume=True)
        expected_intent = {
            'attachment_id': attachment_id,
            'mountpoint': mountpoint,
            'operation_kind': 'migration',
            'operation_token': migration.uuid,
            'operation_direction': 'cold-source-restore',
            'operation_migration_uuid': migration.uuid,
            'boot_volume': boot_volume,
        }
        if any(
                intent.get(key) != value
                for key, value in expected_intent.items()):
            raise exception.InvalidVolume(
                reason='Cold source Cinder intent does not match its BDM')
        self.driver.validate_internal_volume_attach_owner(instance, intent)

        rotation = self.driver.get_cold_attachment_rotation(
            instance, volume_id)
        if rotation is not None:
            expected_rotation = {
                'old_attachment_id': attachment_id,
                'mountpoint': mountpoint,
                'operation_token': migration.uuid,
                'migration_uuid': migration.uuid,
                'boot_volume': boot_volume,
            }
            if any(
                    rotation.get(key) != value
                    for key, value in expected_rotation.items()):
                raise exception.InvalidVolume(
                    reason='Cold attachment rotation owner changed')
            return intent, rotation

        old_attachment = self._get_exact_cinder_attachment(
            context, attachment_id, volume_id, instance.uuid)
        if (old_attachment is None or
                _attachment_status(old_attachment) != 'attached'):
            raise exception.InvalidVolume(
                reason='Cold attachment rotation has no attached old owner')
        # Nova's attachment_get_all wrapper is project-scoped even for the
        # service context used by nova-compute, so an empty list cannot prove
        # that a tenant attachment is absent. The exact attachment show above
        # is the authority. We never adopt an unknown attachment after a lost
        # create response, so the durable baseline only needs that exact ID.
        baseline = [attachment_id]
        rotation, unused_created = (
            self.driver.prepare_cold_attachment_rotation(
                instance, volume_id, attachment_id, mountpoint,
                migration.uuid, migration.uuid, baseline,
                boot_volume=boot_volume))
        return intent, rotation

    def _validate_cold_rotation_new_attachment(
            self, context, instance, volume_id, rotation):
        attachment = self._get_exact_cinder_attachment(
            context, rotation['new_attachment_id'], volume_id, instance.uuid)
        if attachment is None or _attachment_status(attachment) not in (
                'reserved', 'attaching', 'attached'):
            raise exception.InvalidVolume(
                reason='Cold attachment rotation replacement is not owned by '
                       'the instance')
        return attachment

    def _create_cold_rotation_attachment_locked(
            self, context, instance, volume_id, rotation):
        rotation = self.driver.transition_cold_attachment_rotation(
            instance, volume_id, rotation, 'creating')
        try:
            created = self.volume_api.attachment_create(
                context, volume_id, instance.uuid)
        except Exception:
            LOG.critical(
                'Cinder replacement attachment creation for volume %s has an '
                'uncertain result. Retaining the old owner and durable '
                'creating marker for manual exact reconciliation; the Cinder '
                'API has no idempotency key for this request.',
                volume_id, instance=instance, exc_info=True)
            raise
        new_attachment_id = created.get('id') if isinstance(
            created, dict) else None
        if (not uuidutils.is_uuid_like(new_attachment_id) or
                new_attachment_id in rotation['baseline_attachment_ids']):
            raise exception.InvalidVolume(
                reason='Cinder returned an invalid replacement attachment ID')
        # Persist the server-assigned UUID before the first exact show. A
        # failed show is replayable; a lost POST response is not.
        return self.driver.transition_cold_attachment_rotation(
            instance, volume_id, rotation, 'new-created',
            new_attachment_id=new_attachment_id)

    def _advance_cold_attachment_rotation_locked(
            self, context, instance, bdm, rotation):
        volume_id = bdm.volume_id
        if rotation['phase'] == 'prepared':
            rotation = self._create_cold_rotation_attachment_locked(
                context, instance, volume_id, rotation)
        if rotation['phase'] == 'creating':
            LOG.critical(
                'Cold migration attachment creation for volume %s has no '
                'durable response UUID. Refusing to create, delete or switch '
                'any attachment automatically.',
                volume_id, instance=instance)
            raise exception.InvalidVolume(
                reason='Cinder attachment creation result is uncertain')
        if rotation['phase'] == 'new-created':
            self._validate_cold_rotation_new_attachment(
                context, instance, volume_id, rotation)
            old_attachment = self._get_exact_cinder_attachment(
                context, rotation['old_attachment_id'], volume_id,
                instance.uuid)
            if old_attachment is not None:
                if _attachment_status(old_attachment) != 'attached':
                    raise exception.InvalidVolume(
                        reason='Cold migration old attachment changed state')
                try:
                    self.volume_api.attachment_delete(
                        context, rotation['old_attachment_id'])
                except Exception:
                    if self._get_exact_cinder_attachment(
                            context, rotation['old_attachment_id'], volume_id,
                            instance.uuid) is not None:
                        raise
            if self._get_exact_cinder_attachment(
                    context, rotation['old_attachment_id'], volume_id,
                    instance.uuid) is not None:
                raise exception.InvalidVolume(
                    reason='Cold migration old attachment remains after '
                           'deletion')
            rotation = self.driver.transition_cold_attachment_rotation(
                instance, volume_id, rotation, 'old-deleted')
        if rotation['phase'] == 'old-deleted':
            self._validate_cold_rotation_new_attachment(
                context, instance, volume_id, rotation)
            current = objects.BlockDeviceMapping.get_by_volume_and_instance(
                context, volume_id, instance.uuid)
            if current.attachment_id == rotation['old_attachment_id']:
                current.attachment_id = rotation['new_attachment_id']
                try:
                    current.save()
                except Exception:
                    durable = (
                        objects.BlockDeviceMapping.get_by_volume_and_instance(
                            context, volume_id, instance.uuid))
                    if durable.attachment_id != rotation['new_attachment_id']:
                        raise
                    current = durable
            elif current.attachment_id != rotation['new_attachment_id']:
                raise exception.InvalidVolume(
                    reason='Nova BDM changed to an unknown attachment')
            durable = objects.BlockDeviceMapping.get_by_volume_and_instance(
                context, volume_id, instance.uuid)
            if durable.attachment_id != rotation['new_attachment_id']:
                raise exception.InvalidVolume(
                    reason='Nova did not durably switch the Cinder BDM')
            bdm.attachment_id = rotation['new_attachment_id']
            rotation = self.driver.transition_cold_attachment_rotation(
                instance, volume_id, rotation, 'bdm-rotated')
        return rotation

    def _terminate_volume_connections(self, context, instance, bdms):
        """Rotate cold-source Cinder owners with durable crash evidence."""
        migration = self._cold_source_rotation_owner(context, instance)
        if migration is None:
            return super()._terminate_volume_connections(
                context, instance, bdms)
        volume_bdms = sorted(
            (bdm for bdm in bdms if bdm.is_volume),
            key=lambda item: str(item.volume_id))
        if not volume_bdms:
            return None
        prepared = []
        with lockutils.lock(
                _share_recovery_lock_name(instance.uuid), external=True,
                lock_path=CONF.state_path):
            # Persist every volume owner before the first Cinder side effect.
            for bdm in volume_bdms:
                with self._cold_rotation_volume_locks(
                        instance, bdm.volume_id):
                    intent, rotation = (
                        self._prepare_cold_attachment_rotation_locked(
                            context, instance, bdm, migration))
                    prepared.append((bdm, intent, rotation))
            for bdm, unused_intent, unused_rotation in prepared:
                with self._cold_rotation_volume_locks(
                        instance, bdm.volume_id):
                    rotation = self.driver.get_cold_attachment_rotation(
                        instance, bdm.volume_id)
                    self._advance_cold_attachment_rotation_locked(
                        context, instance, bdm, rotation)
        return None

    def _post_live_migration_remove_source_vol_connections(
            self, context, instance, source_bdms):
        """Release each exact old source attachment with its host journal."""
        with lockutils.lock(
                _share_recovery_lock_name(instance.uuid), external=True,
                lock_path=CONF.state_path):
            for bdm in source_bdms:
                if not bdm.is_volume:
                    continue
                volume_id = bdm.volume_id
                try:
                    with lockutils.lock(
                            _volume_manager_transaction_lock_name(
                                instance.uuid, volume_id),
                            external=True,
                            lock_path=(
                                _volume_manager_transaction_lock_path())):
                        with lockutils.lock(
                                incus_driver._volume_topology_lock_name(
                                    instance),
                                external=True,
                                lock_path=(
                                    incus_driver.
                                    _volume_topology_lock_path())):
                            phase = (
                                self.driver.
                                get_volume_journal_recovery_phase(
                                    instance, volume_id))
                            intent = (
                                self.driver.
                                get_managed_volume_attach_intent(
                                    instance, volume_id))
                            if phase is None and intent is None:
                                attachment = (
                                    self._get_exact_cinder_attachment(
                                        context, bdm.attachment_id,
                                        volume_id, instance.uuid))
                                if attachment is None:
                                    # Periodic recovery already retired the
                                    # exact source attachment and its local
                                    # evidence before this callback arrived.
                                    continue
                                raise exception.InvalidVolume(
                                    reason='Live migration source attachment '
                                           'remains without durable local '
                                           'release evidence')
                            self._recover_incus_connecting_volume_journal(
                                context, instance, volume_id,
                                journal_phase=(phase or 'attach-pending'))
                except Exception:
                    # As in Nova's base implementation, a source-release error
                    # cannot roll back a guest already authoritative on the
                    # destination. Unlike the base implementation, the exact
                    # intent and journal survive for periodic replay.
                    LOG.critical(
                        'Live migration source attachment %(attachment)s or '
                        'its Incus host evidence remains for periodic '
                        'recovery',
                        {'attachment': bdm.attachment_id},
                        instance=instance, exc_info=True)

    def _local_node_uuid(self):
        """Return Nova's durable compute identity or fail closed."""
        host_id = virt_node.read_local_node_uuid()
        if not host_id:
            raise incus_driver.incus_idmap.IDMapIntegrityError(
                'Nova compute node UUID is unavailable')
        return host_id

    @staticmethod
    def _idmap_generation_matches(left, right):
        fields = (
            'instance_uuid', 'base', 'size', 'slot', 'allocation_id',
            'fingerprint')
        return all(getattr(left, field) == getattr(right, field)
                   for field in fields)

    @staticmethod
    def _idmap_claim_identity_matches(left, right):
        fields = (
            'host_id', 'materialization_id', 'instance_uuid', 'base',
            'size', 'slot', 'allocation_id', 'fingerprint')
        return all(getattr(left, field) == getattr(right, field)
                   for field in fields)

    def _exact_idmap_host_claim(
            self, allocator, assignment, host_id, expected=None):
        """Return the exact durable claim represented by an assignment."""
        claim = allocator.get_host_claim(assignment.instance_uuid, host_id)
        indexed = host_id in assignment.host_ids
        if not indexed:
            if claim is not None:
                raise incus_driver.incus_idmap.IDMapIntegrityError(
                    'Incus idmap host index conflicts with its allocation')
            return None
        if claim is None:
            raise incus_driver.incus_idmap.IDMapIntegrityError(
                'Incus idmap allocation has no exact local host claim')
        if (claim.host_id != host_id or
                not self._idmap_generation_matches(claim, assignment)):
            raise incus_driver.incus_idmap.IDMapIntegrityError(
                'Incus idmap host claim belongs to another generation')
        if (expected is not None and
                not self._idmap_claim_identity_matches(claim, expected)):
            raise incus_driver.incus_idmap.IDMapConflict(
                reason='another materialization owns the local host claim')
        return claim

    def _settle_idmap_host_claim(self, instance, claim, final_delete=False):
        """Obtain a durable, acknowledged proof for one exact claim."""
        if final_delete and claim.state not in ('committed', 'cleaned'):
            raise incus_driver.incus_idmap.IDMapConflict(
                reason='Final Incus idmap cleanup requires a committed '
                       'materialization claim')
        settled = self.driver._settle_idmap_host_claim(
            instance, claim, final_delete=final_delete)
        if settled is None:
            if claim.state == 'unmaterialized':
                # The driver abandons a never-registered claim whole; it has
                # no proof to demand. Verify the abandonment instead: the
                # claim must be gone and the host de-indexed.
                allocator = self.driver.idmap_allocator
                remaining = allocator.get_host_claim(
                    claim.instance_uuid, claim.host_id)
                current = allocator.get(claim.instance_uuid)
                if remaining is None and (
                        current is None or
                        claim.host_id not in current.host_ids):
                    return None
                raise incus_driver.incus_idmap.IDMapIntegrityError(
                    'Incus idmap claim abandonment left its exact host '
                    'claim behind')
            raise incus_driver.incus_idmap.IDMapConflict(
                reason='the exact Incus idmap host claim disappeared')
        if not self._idmap_claim_identity_matches(settled, claim):
            raise incus_driver.incus_idmap.IDMapIntegrityError(
                'Incus idmap cleanup settled another materialization')
        if settled.state != 'cleaned' or settled.proof is None:
            raise incus_driver.incus_idmap.IDMapIntegrityError(
                'Incus idmap host claim has no acknowledged cleanup proof')
        return settled

    def _idmap_release_assignment(self, instance):
        """Resolve the allocator's exact generation before final deletion."""
        stored = incus_driver._instance_idmap_metadata(instance)
        allocator = getattr(self.driver, 'idmap_allocator', None)
        if allocator is None:
            if stored is not None:
                raise incus_driver.incus_idmap.IDMapIntegrityError(
                    'Nova has Incus idmap metadata but no allocator is '
                    'configured')
            return None

        assignment = allocator.get(instance.uuid)
        if assignment is None:
            if stored is not None:
                raise incus_driver.incus_idmap.IDMapIntegrityError(
                    'Nova Incus idmap metadata has no allocator record')
            return None

        if assignment.instance_uuid != instance.uuid:
            raise incus_driver.incus_idmap.IDMapIntegrityError(
                'Incus idmap allocator returned another instance owner')
        if stored is not None and (
                stored['base'] != assignment.base or
                stored['size'] != assignment.size or
                stored['allocation_id'] != assignment.allocation_id or
                stored['fingerprint'] != assignment.fingerprint):
            raise incus_driver.incus_idmap.IDMapIntegrityError(
                'Nova Incus idmap metadata does not match the allocator '
                'generation')
        return assignment

    @staticmethod
    def _idmap_host_journal_paths(instance_uuid):
        return (
            os.path.join(
                CONF.instances_path, 'incus-volume-journal', instance_uuid),
            os.path.join(
                CONF.instances_path, 'incus-share-journal', instance_uuid),
            os.path.join(
                CONF.instances_path, 'incus-spawn-attempts', instance_uuid),
        )

    def _all_project_idmap_inventory(self):
        """Fetch the batch screening snapshot, or None if unavailable.

        Returning None makes every candidate in the cycle fall back to its
        own exact proof, which is the behaviour that existed before this
        screen. A screening fetch must never be able to release anything by
        failing.
        """
        try:
            return incus_driver._all_project_idmap_inventory(
                self.driver.inventory_client)
        except Exception:
            LOG.warning(
                'Cannot read the all-project Incus inventory for this idmap '
                'cycle; each candidate falls back to its own exact proof',
                exc_info=True)
            return None

    def _idmap_screening_inventory(self):
        """Return a legacy batch screen only without indexed exact proof.

        A server with ``idmap_usage`` makes each final proof an indexed exact
        query, so transferring the whole fleet merely to screen a bounded
        batch would reintroduce O(G) steady-state work. Older servers retain
        the shared snapshot optimization and fresh-scan fallback.
        """
        try:
            if (self.driver.inventory_client.has_api_extension(
                    'idmap_usage') is True):
                return None
        except Exception:
            LOG.warning(
                'Cannot detect indexed Incus idmap usage support; using the '
                'legacy all-project screening path', exc_info=True)
        return self._all_project_idmap_inventory()

    def _local_idmap_resources_absent(self, intent, inventory=None):
        """Prove this host no longer retains resources for an intent."""
        return self._local_idmap_resources_absent_by_name(
            intent.instance_uuid, intent.instance_name,
            intent.base, intent.size, inventory=inventory)

    def _local_named_idmap_resources_absent(
            self, instance_uuid, instance_name):
        """Prove exact local names and journals are absent without a scan."""
        for collection, resource_type in (
                (self.driver.client.instances, 'instance'),
                (self.driver.client.profiles, 'profile')):
            try:
                collection.get(instance_name)
            except Exception as exc:
                if not incus_driver._is_incus_not_found(exc):
                    raise
            else:
                LOG.warning(
                    'Incus %(resource_type)s %(name)s still exists; retaining '
                    'idmap release intent %(uuid)s',
                    {
                        'resource_type': resource_type,
                        'name': instance_name,
                        'uuid': instance_uuid,
                    })
                return False

        local_paths = (
            os.path.join(CONF.instances_path, instance_name),
            *self._idmap_host_journal_paths(instance_uuid),
        )
        for path in local_paths:
            if os.path.lexists(path):
                LOG.warning(
                    'Local Incus resource path %(path)s still exists; '
                    'retaining idmap release intent %(uuid)s',
                    {'path': path, 'uuid': instance_uuid})
                return False
        return True

    def _screen_idmap_resources_absent(self, intent, inventory=None):
        """Apply cheap local and optional immutable batch screening."""
        if not self._local_named_idmap_resources_absent(
                intent.instance_uuid, intent.instance_name):
            return False
        if inventory is None:
            return True
        return incus_driver._all_project_idmap_resources_absent(
            self.driver.inventory_client, intent.instance_uuid,
            intent.base, intent.size, inventory=inventory)

    def _local_idmap_resources_absent_by_name(
            self, instance_uuid, instance_name, idmap_base, idmap_size,
            inventory=None):
        """Prove one named instance has no resources on this compute.

        ``inventory`` only screens the all-project scan against a snapshot
        this cycle already fetched. The exact per-name Incus reads and the
        local path checks below are always fresh, and a caller that is about
        to release must repeat the whole proof with no snapshot.
        """
        if not self._local_named_idmap_resources_absent(
                instance_uuid, instance_name):
            return False
        try:
            if not incus_driver._all_project_idmap_resources_absent(
                    self.driver.inventory_client, instance_uuid,
                    idmap_base, idmap_size, inventory=inventory):
                return False
        except Exception:
            LOG.critical(
                'Cannot prove the all-project Incus inventory is free of '
                'idmap owner %(uuid)s and range %(base)s:%(size)s; retaining '
                'the host claim',
                {
                    'uuid': instance_uuid,
                    'base': idmap_base,
                    'size': idmap_size,
                }, exc_info=True)
            return False
        return True

    @staticmethod
    def _idmap_claim_proof_instance_name(claim):
        """Return the instance name a cleaned claim proves, else None.

        A build that fails before its materialization commits leaves the
        claim at 'possible' with no proof, which is the state this cleanup
        exists to dispose of. Only an already-cleaned claim can name its
        instance, so absence of a name is normal here rather than an error.
        """
        if claim.state != 'cleaned':
            return None
        instance_name = getattr(getattr(claim, 'proof', None),
                                'instance_name', None)
        if not isinstance(instance_name, str) or not instance_name:
            raise incus_driver.incus_idmap.IDMapIntegrityError(
                'Cleaned Incus idmap claim carries no instance name')
        return instance_name

    @classmethod
    def _idmap_claim_instance_name(cls, claim):
        instance_name = cls._idmap_claim_proof_instance_name(claim)
        if instance_name is None:
            raise incus_driver.incus_idmap.IDMapIntegrityError(
                'Terminal failed-build cleanup requires an exact cleaned '
                'claim with its instance name')
        return instance_name

    def _resolve_possible_failed_build_claim(self, instance, claim):
        """Ask Incus whether a 'possible' claim actually materialized.

        'possible' is the deliberately ambiguous state around the create
        request: the rootfs may or may not exist. Resolving it against the
        server decides the disposal — a committed materialization needs a
        release receipt, an uncommitted one needs the materialization
        abort. Without an instance record there is no name to build the
        materialization identity from, so the claim is left as it is.
        """
        if claim.state != 'possible' or instance is None:
            return claim
        unused_assignment, promoted = (
            self.driver._promote_idmap_claim_if_server_committed(
                instance, claim))
        if promoted is None:
            raise incus_driver.incus_idmap.IDMapConflict(
                reason='the exact Incus idmap host claim disappeared')
        return promoted

    def _terminal_failed_build_release_state(
            self, context, assignment, host_id, expected_name):
        """Return the exact Nova terminal state permitting a release fence."""
        try:
            instance = objects.Instance.get_by_uuid(
                context, assignment.instance_uuid,
                expected_attrs=['system_metadata'])
        except exception.InstanceNotFound:
            return 'purged', None
        if instance.name != expected_name:
            raise incus_driver.incus_idmap.IDMapIntegrityError(
                'Terminal failed-build claim instance name does not match '
                'Nova')
        if instance.obj_attr_is_set('deleted') and instance.deleted:
            return 'deleted', instance
        if (instance.host is None and instance.task_state is None and
                instance.vm_state == vm_states.ERROR):
            return 'failed', instance
        return None, instance

    def _queue_terminal_failed_build_release(
            self, context, allocator, assignment, claim, host_id,
            instance=None, inventory=None):
        """Create the immutable release fence before a failed host is lost."""
        try:
            if instance is not None:
                # Nova's own row is authoritative for the name. Cross-check
                # it against the claim only once the claim can name itself;
                # demanding that of a claim stuck at 'possible' would reject
                # exactly the failed builds this path must dispose of.
                instance_name = instance.name
                claim_name = self._idmap_claim_proof_instance_name(claim)
                if claim_name is not None and claim_name != instance_name:
                    raise incus_driver.incus_idmap.IDMapIntegrityError(
                        'Cleaned failed-build claim belongs to another Nova '
                        'instance name')
            else:
                # A purged Nova row leaves the cleaned claim as the only
                # place the exact instance name survives.
                instance_name = self._idmap_claim_instance_name(claim)
            state, current = self._terminal_failed_build_release_state(
                context, assignment, host_id, instance_name)
            if state is None:
                return False
            if current is not None:
                stored = incus_driver._instance_idmap_metadata(current)
                if (stored is None or
                        not incus_driver._idmap_generation_matches_metadata(
                            assignment, stored)):
                    raise incus_driver.incus_idmap.IDMapIntegrityError(
                        'Terminal failed-build Nova metadata does not match '
                        'the exact idmap generation')
                # A local delete (compute unreachable) skips driver.destroy,
                # leaving the spawn attempt journal destroy would have
                # consumed, and the absence proof below would retain the
                # claim forever on that journal. Consume it exactly the way
                # destroy does: only against the exact live claim it names;
                # any mismatch raises and keeps the claim retained. A purged
                # Nova row (current is None) keeps its journal and stays
                # retained and visible.
                self.driver._remove_spawn_attempt_for_claim(current, claim)
            if not self._local_idmap_resources_absent_by_name(
                    assignment.instance_uuid, instance_name,
                    assignment.base, assignment.size, inventory=inventory):
                return False
            claim = self._resolve_possible_failed_build_claim(
                current, claim)
            # A claim still at 'possible' after the server was asked has no
            # materialized rootfs to release, so it settles through the
            # materialization abort instead of a release receipt. Demanding
            # the receipt path here is what left these claims unreleased.
            # An 'unmaterialized' claim never issued its create request at
            # all: it takes the same non-final path, where a registered
            # attempt aborts and a never-registered one is abandoned whole.
            settled = self._settle_idmap_host_claim(
                current, claim,
                final_delete=claim.state not in (
                    'possible', 'unmaterialized'))
        except Exception:
            LOG.exception(
                'Cannot prove terminal failed-build idmap ownership for %s; '
                'retaining its exact claim', assignment.instance_uuid)
            return False

        with lockutils.lock(
                _idmap_release_lock_name(assignment.instance_uuid),
                external=True, lock_path=_idmap_release_lock_path()):
            try:
                latest_assignment = allocator.get(assignment.instance_uuid)
                if (latest_assignment is None or
                        not self._idmap_generation_matches(
                            latest_assignment, assignment)):
                    raise incus_driver.incus_idmap.IDMapIntegrityError(
                        'Failed-build idmap generation changed before release '
                        'fencing')
                exact_claim = self._exact_idmap_host_claim(
                    allocator, latest_assignment, host_id, expected=settled)
                if exact_claim != settled:
                    raise incus_driver.incus_idmap.IDMapIntegrityError(
                        'Failed-build idmap claim changed before release '
                        'fencing')
                latest_state, unused_instance = (
                    self._terminal_failed_build_release_state(
                        context, latest_assignment, host_id, instance_name))
                if latest_state is None:
                    return False
                if not self._local_idmap_resources_absent_by_name(
                        latest_assignment.instance_uuid, instance_name,
                        latest_assignment.base, latest_assignment.size):
                    return False
                intent = allocator.request_release(
                    latest_assignment.instance_uuid, instance_name,
                    assignment=latest_assignment)
                if (intent.instance_name != instance_name or
                        not self._idmap_generation_matches(
                            intent, latest_assignment)):
                    raise incus_driver.incus_idmap.IDMapIntegrityError(
                        'Terminal failed-build release intent has another '
                        'generation')
                LOG.warning(
                    'Queued immutable Incus idmap release for terminal '
                    'failed build %(uuid)s in Nova state %(state)s',
                    {'uuid': assignment.instance_uuid, 'state': latest_state})
                return True
            except Exception:
                LOG.exception(
                    'Failed to queue terminal failed-build idmap release %s; '
                    'retaining its exact claim', assignment.instance_uuid)
                return False

    def _reconcile_incus_idmap_host_claim(
            self, context, allocator, claim, host_id, inventory=None):
        """Retire one historical claim after an exact Incus cleanup proof."""
        try:
            assignment = allocator.get(claim.instance_uuid)
            if assignment is None:
                return
            exact_claim = self._exact_idmap_host_claim(
                allocator, assignment, host_id, expected=claim)
            if exact_claim is None:
                return
        except Exception:
            LOG.exception(
                'Cannot establish the exact local Incus idmap claim %s; '
                'retaining it', claim.instance_uuid)
            return
        try:
            instance = objects.Instance.get_by_uuid(
                context, claim.instance_uuid,
                expected_attrs=['system_metadata'])
        except exception.InstanceNotFound:
            # A failed build can lose its Nova host before the user deletes
            # the ERROR row. The cleaned claim is the last durable place that
            # still carries the exact instance name, so create the immutable
            # release fence before any retirement can empty host_ids.
            self._queue_terminal_failed_build_release(
                context, allocator, assignment, exact_claim, host_id,
                inventory=inventory)
            return
        except Exception:
            LOG.exception(
                'Cannot establish Nova ownership for local Incus idmap '
                'claim %s; retaining it', claim.instance_uuid)
            return

        if instance.obj_attr_is_set('deleted') and instance.deleted:
            self._queue_terminal_failed_build_release(
                context, allocator, assignment, exact_claim, host_id,
                instance=instance, inventory=inventory)
            return
        if (instance.host is None and instance.task_state is None and
                instance.vm_state == vm_states.ERROR):
            self._queue_terminal_failed_build_release(
                context, allocator, assignment, exact_claim, host_id,
                instance=instance, inventory=inventory)
            return
        if instance.task_state is not None or instance.host == self.host:
            return
        if instance.host is None:
            LOG.warning(
                'Nova instance %s has no host but is not a terminal failed '
                'build; retaining its exact Incus idmap claim',
                instance.uuid)
            return
        try:
            stored = incus_driver._instance_idmap_metadata(instance)
        except Exception:
            LOG.critical(
                'Nova instance has corrupt idmap metadata for local '
                'historical claim %s; retaining it',
                claim.instance_uuid, exc_info=True)
            return
        if (stored is None or
                not incus_driver._idmap_generation_matches_metadata(
                    claim, stored)):
            LOG.critical(
                'Nova metadata does not match local historical Incus idmap '
                'claim %s; retaining it', claim.instance_uuid)
            return

        # Incus inventory and storage-proof operations can block on the
        # daemon or Ceph. Keep them outside the cross-process instance lock.
        # The exact A/H/T/U claim is revalidated under the short final lock,
        # so a destination that replaces this cleaned token wins safely.
        if not self._local_idmap_resources_absent_by_name(
                instance.uuid, instance.name, claim.base, claim.size,
                inventory=inventory):
            return
        try:
            if exact_claim.state == 'possible':
                assignment, exact_claim = (
                    self.driver._promote_idmap_claim_if_server_committed(
                        instance, exact_claim))
            settled = self._settle_idmap_host_claim(
                instance, exact_claim,
                final_delete=(exact_claim.state == 'committed'))
            if settled is None:
                # A verified abandonment removed the claim whole; there is
                # nothing left to retire on this host.
                return
            assignment = allocator.get(claim.instance_uuid)
            if (assignment is None or
                    not self._idmap_generation_matches(
                        assignment, exact_claim)):
                raise incus_driver.incus_idmap.IDMapIntegrityError(
                    'Incus idmap generation changed during cleanup proof')
            exact_claim = self._exact_idmap_host_claim(
                allocator, assignment, host_id, expected=settled)
            if (exact_claim is None or exact_claim != settled or
                    exact_claim.state != 'cleaned' or
                    exact_claim.proof is None):
                raise incus_driver.incus_idmap.IDMapIntegrityError(
                    'Incus idmap cleanup proof cannot be revalidated')
        except Exception:
            LOG.exception(
                'Cannot prove local historical Incus idmap claim %s is '
                'clean; retaining it', claim.instance_uuid)
            return

        # Re-read Nova after proof settlement, then repeat the complete local
        # inventory check. Both can perform I/O and therefore remain outside
        # the final claim lock.
        try:
            current = objects.Instance.get_by_uuid(
                context, claim.instance_uuid,
                expected_attrs=['system_metadata'])
        except Exception:
            LOG.exception(
                'Cannot revalidate Nova ownership for local Incus idmap '
                'claim %s; retaining it', claim.instance_uuid)
            return
        if (current.task_state is not None or
                current.host == self.host or
                current.name != instance.name or
                (current.obj_attr_is_set('deleted') and current.deleted)):
            return
        try:
            current_stored = incus_driver._instance_idmap_metadata(current)
        except Exception:
            LOG.exception(
                'Cannot revalidate Nova idmap metadata for local historical '
                'claim %s; retaining it', claim.instance_uuid)
            return
        if (current_stored is None or
                not incus_driver._idmap_generation_matches_metadata(
                    exact_claim, current_stored)):
            return
        if not self._local_idmap_resources_absent_by_name(
                current.uuid, current.name,
                exact_claim.base, exact_claim.size):
            return

        # Only exact registry and Nova ownership revalidation belongs in the
        # critical section. retire_claim is an exact-token CAS; a concurrent
        # cleaned-claim replacement makes this pass retain the new claim.
        with lockutils.lock(
                _idmap_release_lock_name(claim.instance_uuid), external=True,
                lock_path=_idmap_release_lock_path()):
            try:
                assignment = allocator.get(claim.instance_uuid)
                if assignment is None:
                    return
                exact_claim = self._exact_idmap_host_claim(
                    allocator, assignment, host_id, expected=settled)
                if (exact_claim is None or exact_claim != settled or
                        exact_claim.state != 'cleaned' or
                        exact_claim.proof is None):
                    return
                latest = objects.Instance.get_by_uuid(
                    context, claim.instance_uuid,
                    expected_attrs=['system_metadata'])
                if (latest.task_state is not None or
                        latest.host == self.host or
                        latest.name != instance.name or
                        (latest.obj_attr_is_set('deleted') and
                         latest.deleted)):
                    return
                latest_stored = incus_driver._instance_idmap_metadata(latest)
                if (latest_stored is None or
                        not incus_driver._idmap_generation_matches_metadata(
                            exact_claim, latest_stored)):
                    return
                allocator.retire_claim(
                    exact_claim.instance_uuid, host_id,
                    exact_claim.materialization_id, assignment=assignment)
            except Exception:
                LOG.exception(
                    'Failed to retire local historical Incus idmap claim %s; '
                    'retaining it for another pass', claim.instance_uuid)

    @periodic_task.periodic_task(
        spacing=_IDMAP_RELEASE_REPLAY_INTERVAL, run_immediately=True)
    def _reconcile_incus_idmap_host_claims(self, context):
        """Converge this compute's claims without a fleet-wide audit."""
        allocator = getattr(self.driver, 'idmap_allocator', None)
        if allocator is None:
            return
        try:
            host_id = self._local_node_uuid()
            claims = allocator.list_host_claims(host_id)
        except Exception:
            LOG.exception('Failed to list local Incus idmap host claims')
            return

        if not claims:
            self._incus_idmap_host_claim_cursor = 0
            return
        context = (
            context or nova.context.get_admin_context()
        ).elevated(read_deleted='yes')
        try:
            local_instances = self._local_incus_idmap_claim_instances(context)
            active_local_uuids = {
                instance.uuid for instance in local_instances
                if not (instance.obj_attr_is_set('deleted') and
                        instance.deleted)
            }
        except Exception:
            # This is only a screening optimization. Falling back to the
            # exact per-claim Nova lookup preserves the pre-existing
            # fail-closed behaviour when the bulk query is unavailable.
            active_local_uuids = None
            LOG.warning(
                'Cannot bulk-read Nova instances for local Incus idmap '
                'claim screening; falling back to exact claim lookups',
                exc_info=True)
        candidates = claims
        if active_local_uuids is not None:
            candidates = [
                claim for claim in claims
                if claim.instance_uuid not in active_local_uuids
            ]
        if not candidates:
            self._incus_idmap_host_claim_cursor = 0
            return

        # Rotate and bound the candidates only after the one bulk Nova read
        # removes every live local owner.  Truncating first lets a large live
        # prefix indefinitely consume the whole batch while a stale claim at
        # the tail waits for later cycles.
        start = (
            getattr(self, '_incus_idmap_host_claim_cursor', 0) %
            len(candidates))
        ordered = candidates[start:] + candidates[:start]
        batch = ordered[:_IDMAP_RELEASE_REPLAY_BATCH]
        self._incus_idmap_host_claim_cursor = (
            start + len(batch)) % len(candidates)
        inventory = self._idmap_screening_inventory()
        for claim in batch:
            self._reconcile_incus_idmap_host_claim(
                context, allocator, claim, host_id, inventory=inventory)

    def _local_incus_idmap_claim_instances(self, context):
        """Bulk-read this host's Nova rows for claim reconciliation."""
        return objects.InstanceList.get_by_host(
            context, self.host, expected_attrs=[])

    @periodic_task.periodic_task(
        spacing=CONF.incus.idmap_allocator_audit_interval,
        run_immediately=False)
    def _audit_incus_idmap_allocator(self, context):
        """Maintain one lease-backed auditor for the migration domain."""
        allocator = getattr(self.driver, 'idmap_allocator', None)
        if allocator is None:
            return
        full_due = self._incus_full_idmap_audit_due()
        try:
            coordinator, snapshot = allocator.run_coordinated_audit(
                full=full_due)
        except incus_driver.incus_idmap.IDMapIntegrityError:
            LOG.critical(
                'Incus idmap registry integrity audit failed; the sticky '
                'fleet failure blocks sensitive operations on every current '
                'compute until operators repair and clear it',
                exc_info=True)
            return
        except incus_driver.incus_idmap.IDMapBackendError:
            if full_due:
                self._incus_full_audit_deadline = time.monotonic()
            LOG.warning(
                'Incus idmap fleet audit or coordinator lease is '
                'temporarily unavailable', exc_info=True)
            return
        except Exception:
            if full_due:
                self._incus_full_audit_deadline = time.monotonic()
            LOG.exception('Unexpected Incus idmap fleet audit failure')
            return
        if not coordinator:
            return
        if snapshot is None:
            LOG.debug('Renewed Incus idmap fleet audit lease after probe')
            return

        self._incus_full_audit_deadline = (
            time.monotonic() +
            CONF.incus.idmap_allocator_full_audit_interval)
        assignments, intents, unused_claims = snapshot
        LOG.debug(
            'Incus idmap registry integrity audit verified %d allocation(s)',
            len(assignments))
        adopted = self._adopt_unclaimed_incus_idmap_allocations(
            allocator, assignments, intents)
        self._replay_unclaimed_incus_idmap_releases(
            context, allocator, assignments, tuple(intents) + tuple(adopted))

    def _incus_full_idmap_audit_due(self):
        """Return whether this process owes a complete audit now.

        The first deadline is offset by a random fraction of the interval
        so that a fleet restarted together does not line up its scans.
        """
        deadline = getattr(self, '_incus_full_audit_deadline', None)
        interval = CONF.incus.idmap_allocator_full_audit_interval
        now = time.monotonic()
        if deadline is None:
            self._incus_full_audit_deadline = now + random.uniform(
                0, interval)
            return False
        return now >= deadline

    def _adopt_unclaimed_incus_idmap_allocations(self, allocator,
                                                 assignments, intents=()):
        """Give an allocation nobody claims the release intent it lacks.

        An allocation whose last claim went away without leaving an intent
        is invisible to both periodic reclaimers: the host-claim pass only
        walks this compute's claims, and the replay pass only walks
        existing intents. It happens when Nova deletes an instance whose
        compute is unreachable, because a local delete never reaches the
        driver, and after a fence retirement that removes the last claim
        before the destination establishes its own. Nothing would ever
        free the slot.

        Only the intent is created here, never the deletion. The existing
        replay path then applies the full barrier it always has: Nova's
        row proven deleted, the complete inventory proven absent, and an
        exact-generation compare-and-swap. Writing an intent for an
        allocation that turns out to be live is therefore recoverable,
        while deleting one would not be.

        The audit is the only caller because it is the one place that
        already holds a complete, single-revision view of every
        allocation. Asking for one just to look for a rare orphan would
        reintroduce the fleet-wide per-cycle scan the audit interval
        exists to avoid.
        """
        intent_uuids = {intent.instance_uuid for intent in intents}
        candidates = [
            assignment for assignment in assignments
            if (not assignment.host_ids and
                assignment.instance_uuid not in intent_uuids)
        ]
        if not candidates:
            self._incus_idmap_adoption_cursor = 0
            return []
        start = (
            getattr(self, '_incus_idmap_adoption_cursor', 0) %
            len(candidates))
        ordered = candidates[start:] + candidates[:start]
        batch = ordered[:_IDMAP_RELEASE_REPLAY_BATCH]
        self._incus_idmap_adoption_cursor = (
            start + len(batch)) % len(candidates)

        adopted = []
        for assignment in batch:
            try:
                if allocator.get_release_intent(
                        assignment.instance_uuid) is not None:
                    continue
                instance_name = self._unclaimed_idmap_instance_name(
                    assignment)
                if instance_name is None:
                    continue
                intent = allocator.request_release(
                    assignment.instance_uuid, instance_name,
                    assignment=assignment)
            except Exception:
                # One unreadable allocation must not stop the others, and
                # the next audit retries this one.
                LOG.exception(
                    'Cannot adopt unclaimed Incus idmap allocation %s',
                    assignment.instance_uuid)
                continue
            adopted.append(intent)
            LOG.warning(
                'Adopted unclaimed Incus idmap allocation %(uuid)s (slot '
                '%(slot)s) for release; no host claimed it and no release '
                'intent existed',
                {'uuid': assignment.instance_uuid, 'slot': assignment.slot})
        if adopted:
            LOG.info(
                'Adopted %d unclaimed Incus idmap allocation(s) for release',
                len(adopted))
        return adopted

    def _replay_unclaimed_incus_idmap_releases(
            self, context, allocator, assignments, intents):
        """Give zero-claim intents one coordinator during the full audit.

        Such an intent has no ``hosts/<host_id>/`` reverse index by
        definition, so it cannot appear in the cheap host-local replay pass.
        The full audit already owns a single-revision fleet view and is the
        bounded place to discover it. Exact Nova, Incus and allocator reads
        still gate the actual release.
        """
        by_uuid = {value.instance_uuid: value for value in assignments}
        candidates = []
        seen = set()
        for intent in intents:
            assignment = by_uuid.get(intent.instance_uuid)
            if (assignment is None or assignment.host_ids or
                    intent.instance_uuid in seen):
                continue
            candidates.append(intent)
            seen.add(intent.instance_uuid)
        if not candidates:
            self._incus_idmap_unclaimed_release_cursor = 0
            return

        start = (
            getattr(self, '_incus_idmap_unclaimed_release_cursor', 0) %
            len(candidates))
        ordered = candidates[start:] + candidates[:start]
        batch = ordered[:_IDMAP_RELEASE_REPLAY_BATCH]
        self._incus_idmap_unclaimed_release_cursor = (
            start + len(batch)) % len(candidates)

        try:
            host_id = self._local_node_uuid()
            inventory = self._idmap_screening_inventory()
        except Exception:
            LOG.exception(
                'Cannot prepare unclaimed Incus idmap release replay')
            return
        context = (
            context or nova.context.get_admin_context()
        ).elevated(read_deleted='yes')
        for intent in batch:
            self._replay_incus_idmap_release(
                context, allocator, intent, host_id, inventory=inventory)

    def _unclaimed_idmap_instance_name(self, assignment):
        """Return the Nova name an orphaned allocation must be released as.

        The intent binds to an exact instance name and the replay path
        refuses any mismatch, so Nova's row is the only authority for it.
        An allocation whose row is gone entirely cannot be adopted: it is
        reported for the operator recovery procedure instead, because
        inventing a name would make the release fence check the wrong
        thing.
        """
        context = nova.context.get_admin_context().elevated(
            read_deleted='yes')
        try:
            instance = objects.Instance.get_by_uuid(
                context, assignment.instance_uuid,
                expected_attrs=['system_metadata'])
        except exception.InstanceNotFound:
            LOG.error(
                'Unclaimed Incus idmap allocation %(uuid)s (slot %(slot)s) '
                'has no Nova instance row, so its exact name cannot be '
                'established; it needs the operator registry recovery '
                'procedure',
                {'uuid': assignment.instance_uuid, 'slot': assignment.slot})
            return None
        if not instance.obj_attr_is_set('deleted') or not instance.deleted:
            # A live row means this is an in-flight build between allocate
            # and its first claim, not an orphan.
            return None
        return instance.name

    def _nova_idmap_retirement_state(self, context, intent):
        """Return whether Nova permits retirement and its deleted row."""
        try:
            instance = objects.Instance.get_by_uuid(
                context, intent.instance_uuid,
                expected_attrs=['system_metadata'])
        except exception.InstanceNotFound:
            return True, None
        except Exception:
            LOG.exception(
                'Cannot establish Nova state for Incus idmap release intent '
                '%s; retaining it', intent.instance_uuid)
            return False, None

        if not instance.obj_attr_is_set('deleted') or not instance.deleted:
            LOG.warning(
                'Nova instance %s is live or its state is uncertain; '
                'retaining its Incus idmap release intent',
                intent.instance_uuid)
            return False, instance
        if instance.name != intent.instance_name:
            LOG.critical(
                'Deleted Nova instance name does not match Incus idmap '
                'release intent %s; retaining it', intent.instance_uuid)
            return False, instance
        try:
            stored = incus_driver._instance_idmap_metadata(instance)
        except Exception:
            LOG.critical(
                'Deleted Nova instance has corrupt Incus idmap metadata; '
                'retaining release intent %s', intent.instance_uuid,
                exc_info=True)
            return False, instance
        if stored is not None and (
                stored['base'] != intent.base or
                stored['size'] != intent.size or
                stored['allocation_id'] != intent.allocation_id or
                stored['fingerprint'] != intent.fingerprint):
            LOG.critical(
                'Deleted Nova instance idmap metadata does not match release '
                'intent %s; retaining it', intent.instance_uuid)
            return False, instance
        return True, instance

    def _retire_local_idmap_claim(
            self, allocator, assignment, claim, host_id):
        if (not self._idmap_generation_matches(claim, assignment) or
                claim.host_id != host_id or claim.state != 'cleaned' or
                claim.proof is None):
            raise incus_driver.incus_idmap.IDMapIntegrityError(
                'Incus idmap host claim lacks an exact cleanup proof')
        return allocator.retire_claim(
            claim.instance_uuid, host_id, claim.materialization_id,
            assignment=assignment)

    def _complete_idmap_release(self, allocator, intent, assignment):
        """Release an exact generation once its distributed claims are gone."""
        if assignment is not None:
            if not self._idmap_generation_matches(intent, assignment):
                raise incus_driver.incus_idmap.IDMapIntegrityError(
                    'Allocator generation does not match the Incus idmap '
                    'release intent')
            if assignment.host_ids:
                return False
        released = allocator.release(intent)
        if released:
            return True
        current = allocator.get(intent.instance_uuid)
        if (current is not None and
                not self._idmap_generation_matches(intent, current)):
            raise incus_driver.incus_idmap.IDMapIntegrityError(
                'Allocator generation changed while completing an Incus '
                'idmap release intent')
        return False

    def _delete_instance(self, context, instance, bdms):
        """Fence global idmap release around Nova's destructive delete."""
        allocator = getattr(self.driver, 'idmap_allocator', None)
        if allocator is None:
            # Still validate stored metadata so a configuration regression
            # cannot silently orphan an allocator generation.
            self._idmap_release_assignment(instance)
            return super()._delete_instance(context, instance, bdms)

        host_id = self._local_node_uuid()
        with lockutils.lock(
                _idmap_release_lock_name(instance.uuid), external=True,
                lock_path=_idmap_release_lock_path()):
            assignment = self._idmap_release_assignment(instance)
            if assignment is not None:
                local_claim = self._exact_idmap_host_claim(
                    allocator, assignment, host_id)

                # The shared intent must exist before Nova destroys any local
                # or database state. It remains authoritative after every
                # exception, including failures after instance.destroy().
                intent = allocator.request_release(
                    instance.uuid, instance.name, assignment=assignment)
                if (intent.instance_name != instance.name or
                        not self._idmap_generation_matches(
                            intent, assignment)):
                    raise incus_driver.incus_idmap.IDMapIntegrityError(
                        'Shared Incus idmap release intent has another '
                        'generation')
        if assignment is None:
            return super()._delete_instance(context, instance, bdms)
        if local_claim is not None and local_claim.state == 'possible':
            # Runs outside the release lock: the promotion helper takes the
            # claim lock itself and the etcd CAS remains the safety
            # authority for every transition it performs.
            assignment, local_claim = (
                self.driver._promote_idmap_claim_if_server_committed(
                    instance, local_claim))
            if local_claim is None:
                raise incus_driver.incus_idmap.IDMapConflict(
                    reason='Final Nova deletion cannot prove whether the '
                           'local Incus rootfs materialized')
            if local_claim.state == 'possible':
                # A crash between the create request and its outcome leaves
                # the claim at 'possible' with no committed container to
                # promote from - the only evidence this gate used to accept,
                # which made such an instance permanently undeletable. The
                # materialization attempt is the remaining authority:
                # settling it (abort for a registered attempt, whole-claim
                # abandonment for one that was never registered) proves
                # non-materialization. A genuinely committed rootfs cannot
                # reach this branch, because the promotion above would have
                # lifted the claim.
                try:
                    local_claim = self._settle_idmap_host_claim(
                        instance, local_claim, final_delete=False)
                except incus_driver.incus_idmap.IDMapError as exc:
                    raise incus_driver.incus_idmap.IDMapConflict(
                        reason='Final Nova deletion cannot prove whether '
                               'the local Incus rootfs materialized') from exc

        # Nova's delete drives driver.destroy, whose rootfs release receipt
        # path takes the same per-instance claim lock. Holding the release
        # lock across it self-deadlocks the final delete; the shared intent
        # created above stays authoritative without it.
        result = super()._delete_instance(context, instance, bdms)
        with lockutils.lock(
                _idmap_release_lock_name(instance.uuid), external=True,
                lock_path=_idmap_release_lock_path()):
            try:
                if not self._screen_idmap_resources_absent(intent):
                    LOG.critical(
                        'Final Nova deletion left local Incus resources; '
                        'retaining the shared idmap release intent',
                        instance=instance)
                    return result
                assignment = allocator.get(instance.uuid)
                if (assignment is None or
                        not self._idmap_generation_matches(
                            intent, assignment)):
                    raise incus_driver.incus_idmap.IDMapIntegrityError(
                        'Incus idmap generation changed during final delete')
                # A re-read of None means this host is no longer in the
                # allocation's host index, so its claim was already retired
                # elsewhere; a migration that transferred ownership does
                # exactly that. There is nothing left here to settle.
                exact_claim = None
                if local_claim is not None:
                    exact_claim = self._exact_idmap_host_claim(
                        allocator, assignment, host_id,
                        expected=local_claim)
                if exact_claim is not None:
                    settled = self._settle_idmap_host_claim(
                        instance, exact_claim,
                        final_delete=(exact_claim.state in
                                      ('committed', 'cleaned')))
                    assignment = allocator.get(instance.uuid)
                    if (assignment is None or
                            not self._idmap_generation_matches(
                                intent, assignment)):
                        raise incus_driver.incus_idmap.IDMapIntegrityError(
                            'Incus idmap generation changed after cleanup ACK')
                    exact_claim = self._exact_idmap_host_claim(
                        allocator, assignment, host_id, expected=settled)
                    if exact_claim != settled:
                        raise incus_driver.incus_idmap.IDMapIntegrityError(
                            'Incus idmap cleanup proof changed before retire')
                    # ACK completion is not an inventory proof. Recheck after
                    # it so no local resource can be created in the interval.
                    if not self._local_idmap_resources_absent(intent):
                        LOG.critical(
                            'Final Nova deletion regained local Incus '
                            'resources after cleanup proof; retaining the '
                            'shared idmap release intent', instance=instance)
                        return result
                    assignment = self._retire_local_idmap_claim(
                        allocator, assignment, exact_claim, host_id)
                elif not self._local_idmap_resources_absent(intent):
                    # An assignment with no claim on this host can reach the
                    # release CAS without the claimed branch's post-cleanup
                    # proof.  Keep the same exact all-project barrier before
                    # a zero-claim generation can return its range.
                    return result
                if not self._complete_idmap_release(
                        allocator, intent, assignment):
                    LOG.info(
                        'Incus idmap release is waiting for claims from other '
                        'compute nodes', instance=instance)
            except Exception:
                LOG.critical(
                    'Failed to complete the shared Incus idmap release; its '
                    'intent remains queued for fleet-wide replay',
                    instance=instance, exc_info=True)
            return result

    def _replay_incus_idmap_release(self, context, allocator, intent,
                                    host_id, inventory=None):
        """Retire this host's claim and finish one shared release intent."""
        with lockutils.lock(
                _idmap_release_lock_name(intent.instance_uuid), external=True,
                lock_path=_idmap_release_lock_path()):
            try:
                assignment = allocator.get(intent.instance_uuid)
                if assignment is None:
                    raise incus_driver.incus_idmap.IDMapIntegrityError(
                        'Shared Incus idmap release intent has no allocation')
                if not self._idmap_generation_matches(intent, assignment):
                    LOG.critical(
                        'Allocator generation does not match shared Incus '
                        'idmap release intent %s; retaining it',
                        intent.instance_uuid)
                    return

                host_claimed = (
                    assignment is not None and
                    host_id in assignment.host_ids)
                no_claims = not assignment.host_ids
                if not host_claimed and not no_claims:
                    return

                allowed, instance = self._nova_idmap_retirement_state(
                    context, intent)
                if not allowed:
                    return

                # Even an unclaimed, never-materialized crash generation is
                # releasable only after this coordinator proves the complete
                # all-project/local inventory absent. A materialized
                # generation additionally needs Incus's durable receipt.
                # This first pass may screen against the cycle snapshot; the
                # proof that authorizes the release is repeated exactly
                # below, on both the claimed and the unclaimed path.
                if not self._screen_idmap_resources_absent(
                        intent, inventory=inventory):
                    return

                if host_claimed:
                    exact_claim = self._exact_idmap_host_claim(
                        allocator, assignment, host_id)
                    if (instance is not None and
                            exact_claim.state == 'possible'):
                        # This whole block already holds that same lock:
                        # _idmap_release_lock_name aliases the host claim
                        # lock name so release and claim work mutually
                        # exclude. Letting the promotion helper take it
                        # again deadlocks the caller against itself, and
                        # oslo runs every periodic task for a service in
                        # one green thread, so that stops all of them on
                        # this compute until the process restarts.
                        assignment, exact_claim = (
                            self.driver.
                            _promote_idmap_claim_if_server_committed(
                                instance, exact_claim,
                                _claim_lock_held=True))
                    settled = self._settle_idmap_host_claim(
                        instance, exact_claim,
                        final_delete=(exact_claim.state == 'committed' or
                                      exact_claim.state == 'cleaned'))
                    assignment = allocator.get(intent.instance_uuid)
                    if (assignment is None or
                            not self._idmap_generation_matches(
                                intent, assignment)):
                        raise incus_driver.incus_idmap.IDMapIntegrityError(
                            'Incus idmap generation changed after cleanup ACK')
                    exact_claim = self._exact_idmap_host_claim(
                        allocator, assignment, host_id, expected=settled)
                    if exact_claim != settled:
                        raise incus_driver.incus_idmap.IDMapIntegrityError(
                            'Incus idmap cleanup proof changed before replay')
                    if not self._local_idmap_resources_absent(intent):
                        return
                    assignment = self._retire_local_idmap_claim(
                        allocator, assignment, exact_claim, host_id)
                elif not self._local_idmap_resources_absent(intent):
                    # An intent with no claims anywhere reaches the range
                    # release without passing through the claimed branch's
                    # exact recheck, so it needs its own.
                    return

                if not self._complete_idmap_release(
                        allocator, intent, assignment):
                    LOG.debug(
                        'Shared Incus idmap release intent %s still has '
                        'compute claims', intent.instance_uuid)
            except Exception:
                LOG.exception(
                    'Failed to replay shared Incus idmap release intent %s; '
                    'retaining it', intent.instance_uuid)

    @periodic_task.periodic_task(
        spacing=_IDMAP_RELEASE_REPLAY_INTERVAL, run_immediately=True)
    def _replay_incus_idmap_releases(self, context):
        """Replay final-delete intents indexed by this compute's claims."""
        allocator = getattr(self.driver, 'idmap_allocator', None)
        if allocator is None:
            return
        try:
            host_id = self._local_node_uuid()
            claims = allocator.list_host_claims(host_id)
        except Exception:
            LOG.exception('Failed to list local Incus idmap release claims')
            return

        if not claims:
            self._incus_idmap_release_cursor = 0
            return
        start = (
            getattr(self, '_incus_idmap_release_cursor', 0) % len(claims))
        ordered = claims[start:] + claims[:start]
        claim_batch = ordered[:_IDMAP_RELEASE_REPLAY_BATCH]
        self._incus_idmap_release_cursor = (
            start + len(claim_batch)) % len(claims)
        try:
            intents = allocator.list_release_intents_for_instances(
                claim.instance_uuid for claim in claim_batch)
        except Exception:
            LOG.exception(
                'Failed to read release intents for local Incus idmap '
                'claims')
            return
        if not intents:
            return
        context = (
            context or nova.context.get_admin_context()
        ).elevated(read_deleted='yes')
        inventory = self._idmap_screening_inventory()
        for intent in intents:
            self._replay_incus_idmap_release(
                context, allocator, intent, host_id, inventory=inventory)

    def _live_migration_cleanup_flags(self, migrate_data, migr_ctxt=None):
        """Request destination cleanup without deleting shared root storage."""
        if isinstance(
                migrate_data, incus_migrate_data.IncusLiveMigrateData):
            # Incus pre_live_migration creates a destination profile, VIFs,
            # os-brick mappings and possibly Manila mounts. Nova's base
            # implementation only recognizes libvirt and Hyper-V migration
            # data, so explicitly route an Incus failure through destination
            # rollback. Both rootfs models use shared storage during CRIU
            # migration; the driver cleanup must never delete those disks.
            return True, False
        return super()._live_migration_cleanup_flags(
            migrate_data, migr_ctxt=migr_ctxt)

    @manager.wrap_exception()
    @manager.reverts_task_state
    @manager.wrap_instance_event(prefix='compute')
    @manager.wrap_instance_fault
    def suspend_instance(self, context, instance):
        raise exception.InstanceSuspendFailure(
            reason='Incus system containers do not support memory suspend')

    @manager.wrap_exception()
    @manager.reverts_task_state
    @manager.wrap_instance_event(prefix='compute')
    @manager.wrap_instance_fault
    def resume_instance(self, context, instance):
        raise exception.InstanceResumeFailure(
            reason='Incus system containers do not support memory resume')

    @manager.wrap_exception()
    @manager.reverts_task_state
    @manager.wrap_instance_event(prefix='compute')
    @manager.wrap_instance_fault
    def rescue_instance(self, context, instance, rescue_password,
                        rescue_image_ref, clean_shutdown):
        raise exception.InstanceNotRescuable(
            instance_id=instance.uuid,
            reason='Incus requires a storage-native rescue implementation')

    @manager.wrap_exception()
    @manager.reverts_task_state
    @manager.wrap_instance_event(prefix='compute')
    @manager.wrap_instance_fault
    def unrescue_instance(self, context, instance):
        raise exception.InstanceUnRescueFailure(
            reason='Incus system-container rescue is not implemented')

    def _notify_volume_usage_detach(self, context, instance, bdm):
        if CONF.volume_usage_poll_interval > 0:
            eventlet.sleep(_METRICS_SETTLEMENT_DELAY)
        return super()._notify_volume_usage_detach(context, instance, bdm)

    def _get_host_volume_bdms(self, context, use_slave=False):
        """Return host BDMs without one database query per instance."""
        instances = objects.InstanceList.get_by_host(
            context, self.host, use_slave=use_slave)
        if not instances:
            return []

        instance_bdms = {
            instance.uuid: []
            for instance in instances
        }
        bdms = objects.BlockDeviceMappingList.get_by_instance_uuids(
            context, list(instance_bdms), use_slave=use_slave)
        for bdm in bdms:
            if bdm.is_volume and bdm.instance_uuid in instance_bdms:
                instance_bdms[bdm.instance_uuid].append(bdm)

        # Preserve Nova's per-instance return shape and InstanceList order,
        # including hosts whose instances currently have no volume BDMs.
        return [
            {
                'instance': instance,
                'instance_bdms': instance_bdms[instance.uuid],
            }
            for instance in instances
        ]

    @staticmethod
    def _is_failed_build_cleanup(instance):
        return (
            getattr(instance, 'vm_state', None) == vm_states.BUILDING and
            getattr(instance, 'task_state', None) == task_states.SPAWNING)

    def _failed_build_cleanup_record(self, instance, assessment):
        compute_id = getattr(instance, 'compute_id', None)
        release_mask = 0
        if assessment.release_network:
            release_mask |= _FAILED_BUILD_RELEASE_NETWORK
        if assessment.release_cinder:
            release_mask |= _FAILED_BUILD_RELEASE_CINDER
        if assessment.release_host:
            release_mask |= _FAILED_BUILD_RELEASE_HOST
        if assessment.release_placement:
            release_mask |= _FAILED_BUILD_RELEASE_PLACEMENT
        host = getattr(instance, 'host', None)
        if host != self.host or compute_id is None:
            raise ValueError(
                'failed-build cleanup owner has no local host/compute binding')
        binding = jsonutils.dumps({
            'compute_id': str(compute_id),
            'host': host,
            'instance_name': instance.name,
            'instance_uuid': instance.uuid,
        }, sort_keys=True, separators=(',', ':')).encode('utf-8')
        digest = hashlib.sha256(binding).hexdigest()
        # This fixed-size record is independent of Nova's valid host/name
        # maxima and always fits instance_system_metadata.value String(255).
        return 'v{}:{:x}:{}'.format(
            _FAILED_BUILD_CLEANUP_BARRIER_VERSION, release_mask, digest)

    def _decode_failed_build_cleanup_barrier(self, instance):
        metadata = incus_driver._loaded_instance_system_metadata(instance)
        encoded = metadata.get(_FAILED_BUILD_CLEANUP_BARRIER_KEY)
        if encoded is None:
            return None
        try:
            version, encoded_mask, digest = encoded.split(':')
            release_mask = int(encoded_mask, 16)
            valid = (
                isinstance(encoded, str) and
                len(encoded) <= _SYSTEM_METADATA_VALUE_MAX_LENGTH and
                version == 'v{}'.format(
                    _FAILED_BUILD_CLEANUP_BARRIER_VERSION) and
                encoded_mask == '{:x}'.format(release_mask) and
                0 <= release_mask <= _FAILED_BUILD_RELEASE_ALL and
                len(digest) == 64 and
                all(character in '0123456789abcdef' for character in digest)
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(
                'failed-build cleanup barrier is malformed') from exc
        if not valid:
            raise ValueError(
                'failed-build cleanup barrier is invalid')
        release_network = bool(
            release_mask & _FAILED_BUILD_RELEASE_NETWORK)
        release_cinder = bool(
            release_mask & _FAILED_BUILD_RELEASE_CINDER)
        release_host = bool(release_mask & _FAILED_BUILD_RELEASE_HOST)
        release_placement = bool(
            release_mask & _FAILED_BUILD_RELEASE_PLACEMENT)
        if (release_host and not (release_network and release_cinder)):
            raise ValueError(
                'failed-build cleanup barrier release order is invalid')
        if release_placement and not release_host:
            raise ValueError(
                'failed-build cleanup barrier Placement order is invalid')
        expected = self._failed_build_cleanup_record(
            instance,
            incus_driver.FailedBuildCleanupAssessment(
                release_network=release_network,
                release_cinder=release_cinder,
                release_host=release_host,
                release_placement=release_placement))
        if encoded != expected:
            raise ValueError(
                'failed-build cleanup barrier ownership is invalid')
        return incus_driver.FailedBuildCleanupAssessment(
            release_network=release_network,
            release_cinder=release_cinder,
            release_host=release_host,
            release_placement=release_placement,
            reasons=('durable failed-build cleanup barrier',))

    def _failed_build_cleanup_barrier(self, instance):
        try:
            return self._decode_failed_build_cleanup_barrier(instance)
        except Exception as exc:
            LOG.critical(
                'Cannot validate the Incus failed-build cleanup barrier; '
                'retaining all external ownership: %s',
                exc, instance=instance, exc_info=True)
            return incus_driver.FailedBuildCleanupAssessment.unsafe(
                'failed-build cleanup barrier is invalid')

    def _persist_failed_build_cleanup_barrier(
            self, context, instance, assessment):
        encoded = self._failed_build_cleanup_record(instance, assessment)
        if len(encoded) > _SYSTEM_METADATA_VALUE_MAX_LENGTH:
            raise ValueError(
                'failed-build cleanup barrier exceeds Nova system_metadata '
                'value capacity')

        # Keep the in-flight object protected even if the durable write fails;
        # _set_instance_obj_error_state() gets another chance to persist it.
        metadata = dict(
            incus_driver._loaded_instance_system_metadata(instance))
        metadata[_FAILED_BUILD_CLEANUP_BARRIER_KEY] = encoded
        instance.system_metadata = metadata

        current = objects.Instance.get_by_uuid(
            context, instance.uuid, expected_attrs=['system_metadata'])
        current_record = self._failed_build_cleanup_record(
            current, assessment)
        if current_record != encoded:
            raise ValueError(
                'Nova instance ownership changed while persisting the Incus '
                'failed-build cleanup barrier')
        current_metadata = dict(current.system_metadata or {})
        current_metadata[_FAILED_BUILD_CLEANUP_BARRIER_KEY] = encoded
        current.system_metadata = current_metadata
        current.save()
        instance.system_metadata = dict(current_metadata)

    def _clear_failed_build_cleanup_barrier(self, context, instance):
        metadata = incus_driver._loaded_instance_system_metadata(instance)
        if _FAILED_BUILD_CLEANUP_BARRIER_KEY not in metadata:
            return
        # Do not let another compute clear an ownership record merely because
        # its local cleanup succeeded.
        self._decode_failed_build_cleanup_barrier(instance)
        current = objects.Instance.get_by_uuid(
            context, instance.uuid, expected_attrs=['system_metadata'])
        self._decode_failed_build_cleanup_barrier(current)
        current_metadata = dict(current.system_metadata or {})
        current_metadata.pop(_FAILED_BUILD_CLEANUP_BARRIER_KEY, None)
        current.system_metadata = current_metadata
        current.save()
        instance.system_metadata = dict(current_metadata)

    def _record_failed_build_cleanup_barrier(
            self, context, instance, bdms):
        try:
            block_device_info = self._get_instance_block_device_info(
                context, instance, bdms=bdms)
            assessment = self.driver.assess_failed_build_cleanup(
                instance, block_device_info)
            if not isinstance(
                    assessment,
                    incus_driver.FailedBuildCleanupAssessment):
                raise TypeError(
                    'driver returned an invalid cleanup assessment')
        except Exception as exc:
            LOG.critical(
                'Cannot assess local Incus ownership after failed build; '
                'retaining all external ownership',
                instance=instance, exc_info=True)
            assessment = incus_driver.FailedBuildCleanupAssessment.unsafe(
                'local ownership assessment failed: {}'.format(exc))

        if all((
                assessment.release_network,
                assessment.release_cinder,
                assessment.release_host,
                assessment.release_placement)):
            return
        LOG.critical(
            'Persisting Incus failed-build cleanup barrier; retained '
            'ownership requires a successful retry: %s',
            '; '.join(assessment.reasons), instance=instance)
        try:
            self._persist_failed_build_cleanup_barrier(
                context, instance, assessment)
        except Exception:
            LOG.critical(
                'Failed to persist the Incus failed-build cleanup barrier; '
                'the in-flight Nova instance remains protected',
                instance=instance, exc_info=True)

    def _cleanup_allocated_networks(
            self, context, instance, requested_networks):
        barrier = self._failed_build_cleanup_barrier(instance)
        if barrier is not None and not barrier.release_network:
            LOG.critical(
                'Retaining Neutron ownership after failed Incus build: %s',
                '; '.join(barrier.reasons), instance=instance)
            return
        return super()._cleanup_allocated_networks(
            context, instance, requested_networks)

    def _cleanup_volumes(
            self, context, instance, bdms, raise_exc=True, detach=True):
        barrier = self._failed_build_cleanup_barrier(instance)
        if barrier is not None and not barrier.release_cinder:
            LOG.critical(
                'Retaining Cinder ownership after failed Incus build: %s',
                '; '.join(barrier.reasons), instance=instance)
            return
        return super()._cleanup_volumes(
            context, instance, bdms, raise_exc=raise_exc, detach=detach)

    def _nil_out_instance_obj_host_and_node(self, instance):
        barrier = self._failed_build_cleanup_barrier(instance)
        if barrier is not None and not barrier.release_host:
            LOG.critical(
                'Retaining Nova compute ownership after failed Incus build: '
                '%s', '; '.join(barrier.reasons), instance=instance)
            return
        return super()._nil_out_instance_obj_host_and_node(instance)

    def _should_delete_allocation_for_failed_build(
            self, context, instance):
        barrier = self._failed_build_cleanup_barrier(instance)
        return barrier is None or barrier.release_placement

    def _shutdown_instance(self, context, instance, bdms,
                           requested_networks=None, notify=True,
                           try_deallocate_networks=True):
        """Settle volume counters before Incus removes the block devices."""
        volumes = [bdm for bdm in bdms if bdm.is_volume]
        if volumes and CONF.volume_usage_poll_interval > 0:
            # Wait once for the instance, rather than once per attached volume.
            eventlet.sleep(_METRICS_SETTLEMENT_DELAY)

        for bdm in volumes:
            try:
                super()._notify_volume_usage_detach(context, instance, bdm)
            except Exception:
                # Metering must not prevent an instance from being deleted.
                LOG.exception(
                    'Failed to settle final volume usage before instance '
                    'shutdown for volume %s', bdm.volume_id,
                    instance=instance)

        try:
            result = super()._shutdown_instance(
                context, instance, bdms,
                requested_networks=requested_networks,
                notify=notify,
                # Nova's base implementation deallocates Neutron ports even
                # when driver.destroy() raises a generic exception.  Incus
                # must first prove that no local container/profile can still
                # consume those ports, so defer network release until the
                # entire base shutdown path has completed successfully.
                try_deallocate_networks=False)
        except Exception:
            if self._is_failed_build_cleanup(instance):
                self._record_failed_build_cleanup_barrier(
                    context, instance, bdms)
            raise
        else:
            self._clear_failed_build_cleanup_barrier(context, instance)
            if try_deallocate_networks:
                self._try_deallocate_network(
                    context.elevated(), instance, requested_networks)
            return result

    def _rollback_live_migration(self, context, instance,
                                 dest, migrate_data=None,
                                 migration_status='failed',
                                 source_bdms=None,
                                 pre_live_migration=False):
        """Restore the source runtime after the control-plane rollback.

        rollback_live_migration_at_source deliberately leaves the source
        container fenced (stopped): Cinder attachment IDs, Neutron bindings,
        destination os-brick mappings and Manila mounts are only reverted by
        the base rollback that follows it. Only after those complete may the
        driver restore shared-storage ownership and restart the source from
        its checkpoint, so run the completion step after the base rollback.
        """
        super()._rollback_live_migration(
            context, instance, dest, migrate_data=migrate_data,
            migration_status=migration_status, source_bdms=source_bdms,
            pre_live_migration=pre_live_migration)
        self._complete_live_migration_rollback(
            context, instance, migrate_data,
            pre_live_migration=pre_live_migration,
            migration_status=migration_status)

    def _complete_live_migration_rollback(
            self, context, instance, migrate_data,
            pre_live_migration=False, migration_status='failed'):
        """Prove target cleanup before Nova reports rollback complete."""
        if (
            pre_live_migration and
            isinstance(migrate_data, incus_migrate_data.IncusLiveMigrateData)
        ):
            try:
                if (migration_status not in (
                        'cancelled', 'error', 'failed') or
                        instance.host != self.host):
                    raise exception.MigrationError(
                        reason='Nova has not retained source ownership after '
                               'pre-live migration rollback')
                self.driver.finalize_pre_live_migration_rollback(
                    instance, migrate_data)
            except Exception:
                LOG.critical(
                    'Pre-live migration rollback committed but its source '
                    'generation token could not be retired; periodic cleanup '
                    'will retry', instance=instance, exc_info=True)
            return
        if (
            not pre_live_migration and
            isinstance(migrate_data, incus_migrate_data.IncusLiveMigrateData)
        ):
            self.driver.finalize_live_migration_rollback(
                context, instance, migrate_data)
            try:
                if (migration_status not in (
                        'cancelled', 'error', 'failed') or
                        instance.host != self.host):
                    raise exception.MigrationError(
                        reason='Nova has not committed source ownership after '
                               'live migration rollback')
                self.driver.finalize_remote_source_volume_generation(
                    instance,
                    incus_driver._live_migration_cleanup_token(migrate_data),
                )
            except Exception:
                LOG.critical(
                    'Live migration rollback committed but its source '
                    'generation token could not be retired; periodic cleanup '
                    'will retry', instance=instance, exc_info=True)

    def _prepare_live_migration_check_data(
            self, context, instance, dest_check_data, migration):
        """Bind every Incus live-migration side effect to Nova's UUID."""
        base_prepare = getattr(
            super(), '_prepare_live_migration_check_data', None)
        if base_prepare is not None:
            dest_check_data = base_prepare(
                context, instance, dest_check_data, migration)
        if not isinstance(
                dest_check_data, incus_migrate_data.IncusLiveMigrateData):
            return dest_check_data
        migration_uuid = getattr(migration, 'uuid', None)
        if not uuidutils.is_uuid_like(migration_uuid):
            raise exception.MigrationPreCheckError(
                reason='Incus live migration has no durable Nova migration '
                       'UUID')
        dest_check_data.migration_uuid = migration_uuid
        return dest_check_data

    @staticmethod
    def _hydrate_share_mapping(context, share_mapping):
        """Populate the complete ephemeral Manila access contract."""
        share_mapping.set_access_according_to_protocol()
        if (share_mapping.share_proto ==
                obj_fields.ShareMappingProto.CEPHFS):
            share_mapping.enhance_with_ceph_credentials(context)

    def _hydrate_share_info(self, context, share_info):
        """Hydrate every mapping before the first host mount side effect."""
        share_info = list(share_info or [])
        for share_mapping in share_info:
            self._hydrate_share_mapping(context, share_mapping)
        return share_info

    @manager.wrap_exception()
    def _mount_all_shares(self, context, instance, share_info):
        """Mount shares as one transaction and undo completed changes."""
        share_info = list(share_info or [])
        failed = None
        try:
            for failed in share_info:
                self._hydrate_share_mapping(context, failed)
        except Exception:
            # No driver call has happened yet, so hydration failure cannot
            # leave a partial host mount transaction.
            if failed is not None:
                self._set_share_mapping_and_instance_in_error(
                    instance, failed)
            raise

        mount_table = self.driver.get_share_mount_table()
        changed = []
        for share_mapping in share_info:
            try:
                if self.driver.mount_share_transaction(
                        context, instance, share_mapping,
                        mount_table=mount_table):
                    changed.append(share_mapping)
            except Exception:
                self._set_share_mapping_and_instance_in_error(
                    instance, share_mapping)
                for mounted_mapping in reversed(changed):
                    try:
                        self.driver.umount_share_transaction(
                            context, instance, mounted_mapping,
                            mount_table=mount_table)
                    except Exception:
                        LOG.exception(
                            'Failed to roll back transactional Manila mount '
                            'for share %s', mounted_mapping.share_id,
                            instance=instance)
                raise

    @manager.wrap_exception()
    def _umount_all_shares(self, context, instance, share_info):
        """Attempt every unmount and report one recognizable failure."""
        failures = []
        mount_table = self.driver.get_share_mount_table()
        for share_mapping in list(share_info or []):
            try:
                self.driver.umount_share_transaction(
                    context, instance, share_mapping,
                    mount_table=mount_table)
            except Exception as exc:
                failures.append((share_mapping, exc))

        if not failures:
            return
        for share_mapping, failure in failures:
            LOG.error(
                'Manila unmount transaction failed for share %(share)s: '
                '%(error)s',
                {'share': share_mapping.share_id, 'error': failure},
                instance=instance)
            self._set_share_mapping_status(
                share_mapping, obj_fields.ShareMappingStatus.ERROR)
        self._set_instance_obj_error_state(instance, clean_task_state=True)
        if len(failures) == 1:
            raise failures[0][1]
        share_ids = ','.join(
            mapping.share_id for mapping, _failure in failures)
        raise exception.ShareUmountError(
            share_id=share_ids,
            server_id=instance.uuid,
            reason='{} independent share unmount operations failed'.format(
                len(failures))) from failures[0][1]

    def _pre_deny_share(self, context, instance, share_mapping):
        """Remove physical state before Manila revokes the access rule."""
        try:
            self.driver.umount_share_transaction(
                context, instance, share_mapping,
                mount_table=self.driver.get_share_mount_table())
        except Exception:
            self._set_share_mapping_and_instance_in_error(
                instance, share_mapping)
            raise

    @manager.wrap_exception()
    @manager.wrap_instance_event(prefix='compute')
    @manager.wrap_instance_fault
    def pre_live_migration(self, context, instance, disk, migrate_data):
        with lockutils.lock(
                _share_recovery_lock_name(instance.uuid), external=True,
                lock_path=CONF.state_path):
            return self._pre_live_migration_locked(
                context, instance, disk, migrate_data)

    def _pre_live_migration_locked(
            self, context, instance, disk, migrate_data):
        """Mount active Manila shares on the migration destination."""
        # Reject an older or unverified source before Manila staging and
        # before Nova's base manager creates Cinder attachments. The driver
        # repeats this check at its own host-side preparation boundary.
        incus_driver._require_full_checkpoint_attestation(migrate_data)
        share_info = [
            mapping
            for mapping in self._get_share_info(context, instance)
            if mapping.status == obj_fields.ShareMappingStatus.ACTIVE
        ]
        share_info = self._hydrate_share_info(context, share_info)
        mount_table = self.driver.get_share_mount_table()
        cleanup_token = incus_driver._live_migration_cleanup_token(
            migrate_data)
        staged = []
        try:
            for share_mapping in share_info:
                # _stage_share_mount_locked() persists its owner journal
                # before mounting. Include the in-flight call so its journal
                # is rolled back even when the first mount attempt fails.
                staged.append(share_mapping)
                self.driver.stage_share_for_live_migration(
                    context, instance, share_mapping, cleanup_token,
                    mount_table=mount_table)
            base_pre_live_migration = manager.safe_utils.get_wrapped_function(
                manager.ComputeManager.pre_live_migration)
            result = base_pre_live_migration(
                self, context, instance, disk, migrate_data)
        except Exception:
            for share_mapping in reversed(staged):
                try:
                    self.driver.unstage_share_for_live_migration(
                        context, instance, share_mapping, cleanup_token,
                        mount_table=mount_table)
                except Exception:
                    LOG.exception(
                        'Failed to roll back destination Manila mount for '
                        'share %s', share_mapping.share_id,
                        instance=instance)
            try:
                # Nova's base manager has already issued its mandatory
                # second driver_detach pass at this point. Delete the staging
                # profile only after both Cinder metadata and Manila mounts
                # are confirmed absent.
                self.driver.cleanup_pre_live_migration_destination(
                    context, instance, migrate_data)
            except Exception:
                LOG.exception(
                    'Failed to remove unused Incus pre-live migration '
                    'profile', instance=instance)
            raise

        try:
            self._commit_formal_internal_volume_intents(
                context, instance,
                incus_driver._live_migration_cleanup_token(migrate_data),
                incus_driver._live_migration_uuid(migrate_data),
                'live-target')
        except Exception:
            # The base manager has already completed the target Cinder
            # attachments.  Local evidence retirement cannot safely turn
            # that formal commit into a migration rollback.
            LOG.critical(
                'Live migration target attachments committed but their '
                'Incus recovery evidence could not be retired; periodic '
                'recovery will retry', instance=instance, exc_info=True)
        return result

    def revert_resize(self, context, instance, migration, request_spec):
        """Retire formal target volume evidence before destructive revert."""
        with lockutils.lock(
                _share_recovery_lock_name(instance.uuid), external=True,
                lock_path=CONF.state_path):
            try:
                profile = self.driver.client.profiles.get(instance.name)
                config = (
                    profile.config if isinstance(profile.config, dict) else {})
                cleanup_token = config.get(
                    incus_driver.MIGRATION_CLEANUP_TOKEN_KEY)
            except Exception:
                LOG.exception(
                    'Cannot validate cold-target Cinder evidence before '
                    'resize revert', instance=instance)
                raise
            if uuidutils.is_uuid_like(cleanup_token):
                self._commit_formal_internal_volume_intents(
                    context, instance, cleanup_token, migration.uuid,
                    'cold-target')
            return super().revert_resize(
                context, instance, migration, request_spec)

    def finish_revert_resize(
            self, context, instance, migration, request_spec):
        with lockutils.lock(
                _share_recovery_lock_name(instance.uuid), external=True,
                lock_path=CONF.state_path):
            return self._finish_revert_resize_and_commit_volumes(
                context, instance, migration, request_spec)

    def _finish_revert_resize_and_commit_volumes(
            self, context, instance, migration, request_spec):
        result = super().finish_revert_resize(
            context, instance, migration, request_spec)
        try:
            profile = self.driver.client.profiles.get(instance.name)
            config = profile.config if isinstance(profile.config, dict) else {}
            cleanup_token = config.get(
                incus_driver.MIGRATION_CLEANUP_TOKEN_KEY)
        except Exception:
            LOG.exception(
                'Cannot read cold-revert Cinder intent owner after Nova '
                'completed attachments', instance=instance)
            return result
        if uuidutils.is_uuid_like(cleanup_token):
            try:
                self._commit_formal_internal_volume_intents(
                    context, instance, cleanup_token, migration.uuid,
                    'cold-revert-source')
                if (migration.status != 'reverted' or
                        instance.host != self.host):
                    raise exception.MigrationError(
                        reason='Nova has not committed cold-revert source '
                               'ownership')
                self.driver.finalize_remote_source_volume_generation(
                    instance, cleanup_token)
            except Exception:
                LOG.critical(
                    'Cold-revert source attachments committed but their '
                    'Incus recovery evidence could not be retired; periodic '
                    'recovery will retry', instance=instance, exc_info=True)
        return result

    def _finish_resize_helper(
            self, context, disk_info, image, instance, migration,
            request_spec):
        with lockutils.lock(
                _share_recovery_lock_name(instance.uuid), external=True,
                lock_path=CONF.state_path):
            return self._finish_resize_helper_locked(
                context, disk_info, image, instance, migration,
                request_spec)

    def _finish_resize_helper_locked(
            self, context, disk_info, image, instance, migration,
            request_spec):
        """Pre-stage active shares before the cold target profile exists."""
        # Nova's base helper rotates Cinder attachments before it calls the
        # driver's finish_migration().  Reject an older source-cleanup profile
        # here, while the control-plane attachments still name their source.
        self.driver.preflight_cold_migration_destination_profile(
            instance, disk_info)
        staged = []
        finish_started = False
        cleanup_token = None
        try:
            share_info = [
                mapping
                for mapping in self._get_share_info(context, instance)
                if mapping.status == obj_fields.ShareMappingStatus.ACTIVE
            ]
            share_info = self._hydrate_share_info(context, share_info)
            mount_table = self.driver.get_share_mount_table()
            disk_info, cleanup_token = (
                incus_driver.prepare_cold_migration_share_info(
                    disk_info, share_info))
            for share_mapping in share_info:
                # The stage call owns a durable journal as soon as it starts,
                # not only after mount(2) succeeds.
                staged.append(share_mapping)
                self.driver.stage_share_for_cold_migration(
                    context, instance, share_mapping, cleanup_token,
                    mount_table=mount_table)
            finish_started = True
            result = super()._finish_resize_helper(
                context, disk_info, image, instance, migration,
                request_spec)
        except Exception:
            if not finish_started and cleanup_token is not None:
                for share_mapping in reversed(staged):
                    try:
                        self.driver.unstage_share_for_cold_migration(
                            context, instance, share_mapping, cleanup_token,
                            mount_table=mount_table)
                    except Exception:
                        # The driver-level abort below retries all journals
                        # owned by this token before retiring its idmap.
                        LOG.exception(
                            'Failed the first cold-migration Manila rollback '
                            'pass for share %s', share_mapping.share_id,
                            instance=instance)

            try:
                retain_target = (
                    self.driver.rollback_cold_migration_preparation(
                        context, instance, disk_info))
            except Exception as cleanup_error:
                raise exception.MigrationError(
                    reason='Cold-migration preparation failed and its '
                           'target transaction could not be reconciled: %s'
                           % cleanup_error) from cleanup_error
            if retain_target:
                LOG.error(
                    'Retaining pre-mounted Manila shares for a failed '
                    'cold-migration target queued for recovery',
                    instance=instance)
            raise

        try:
            # Nova's destination finish path preserves the source-side
            # power_state value.  Refresh it after the Incus target is
            # authoritative so confirm_resize does not turn a running
            # migrated container into a SHUTOFF Nova instance.
            instance.power_state = self.driver.get_info(instance).state
            instance.save(expected_task_state=[None])
        except Exception:
            # The migration is already committed at this point. A power
            # state persistence failure must not tear down the target.
            LOG.critical(
                'Cold migration target completed but its authoritative '
                'power state could not be persisted; periodic power sync '
                'will retry', instance=instance, exc_info=True)

        try:
            self._commit_formal_internal_volume_intents(
                context, instance, cleanup_token, migration.uuid,
                'cold-target')
        except Exception:
            # The base helper has already completed Cinder attachments and
            # recorded the resize as finished.  Retain exact evidence for the
            # periodic reconciler instead of rolling back a committed target.
            LOG.critical(
                'Cold migration target attachments committed but their '
                'Incus recovery evidence could not be retired; periodic '
                'recovery will retry', instance=instance, exc_info=True)
        return result

    def _commit_formal_internal_volume_intents(
            self, context, instance, cleanup_token, migration_uuid,
            operation_direction):
        """Retire internal intents only after Nova completed attachments."""
        bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(
            context, instance.uuid)
        for candidate in bdms:
            volume_id = getattr(candidate, 'volume_id', None)
            attachment_id = getattr(candidate, 'attachment_id', None)
            mountpoint = getattr(candidate, 'device_name', None)
            if (not uuidutils.is_uuid_like(volume_id) or
                    not uuidutils.is_uuid_like(attachment_id) or
                    not isinstance(mountpoint, str) or not mountpoint):
                continue
            with lockutils.lock(
                    _volume_manager_transaction_lock_name(
                        instance.uuid, volume_id),
                    external=True,
                    lock_path=_volume_manager_transaction_lock_path()):
                with lockutils.lock(
                        incus_driver._volume_topology_lock_name(instance),
                        external=True,
                        lock_path=incus_driver._volume_topology_lock_path()):
                    with lockutils.lock(
                            incus_driver._volume_operation_lock_name(
                                volume_id),
                            external=True,
                            lock_path=incus_driver.
                            _volume_operation_lock_path()):
                        intent = (
                            self.driver.get_managed_volume_attach_intent(
                                instance, volume_id))
                        if intent is None:
                            continue
                        if (intent.get('operation_kind') != 'migration' or
                                intent.get('operation_direction') !=
                                operation_direction or
                                intent.get('operation_token') !=
                                cleanup_token or
                                intent.get('operation_migration_uuid') !=
                                migration_uuid or
                                intent.get('attachment_id') != attachment_id or
                                intent.get('mountpoint') != mountpoint):
                            raise exception.InvalidVolume(
                                reason='Migration Cinder intent '
                                       'changed before commit')
                        attachment = self._get_exact_cinder_attachment(
                            context, attachment_id, volume_id, instance.uuid)
                        if (attachment is None or
                                _attachment_status(attachment) != 'attached'):
                            raise exception.InvalidVolume(
                                reason='Migration Cinder '
                                       'attachment did not commit')
                        connection_info = _attachment_connection_info(
                            attachment)
                        connection_info = dict(connection_info)
                        connection_info['data'] = dict(
                            connection_info.get('data') or {})
                        connection_info.setdefault('serial', volume_id)
                        bdm_connection_info = _bdm_connection_info(candidate)
                        if (_canonical_attachment_connection_info(
                                bdm_connection_info, volume_id,
                                instance.uuid) !=
                                _canonical_attachment_connection_info(
                                    connection_info, volume_id,
                                    instance.uuid)):
                            raise exception.InvalidVolume(
                                reason='Migration Nova BDM and '
                                       'Cinder attachment disagree')
                        try:
                            self.driver.confirm_connected_volume_journal(
                                instance, volume_id, connection_info,
                                expected_mountpoint=mountpoint)
                            self.driver.cancel_managed_volume_attach(
                                instance, volume_id, intent)
                        except OSError:
                            # The Cinder/BDM transaction is already formal.
                            # unlink may have succeeded before fsync failed, so
                            # re-publish the same exact intent for periodic
                            # retirement. The migration is already committed;
                            # failure to restore this local evidence still must
                            # not reverse the formal Cinder/Nova transaction.
                            try:
                                recovered = (
                                    self.driver.prepare_managed_volume_attach(
                                        instance, volume_id, attachment_id,
                                        mountpoint,
                                        operation_kind='migration',
                                        operation_token=cleanup_token,
                                        operation_direction=(
                                            operation_direction),
                                        operation_migration_uuid=(
                                            migration_uuid)))
                                if recovered != intent:
                                    raise exception.InvalidVolume(
                                        reason='Migration recovery intent '
                                               'changed after fsync failure')
                            except Exception:
                                LOG.critical(
                                    'Migration volume %s committed and its '
                                    'recovery intent could not be '
                                    're-published',
                                    volume_id, instance=instance,
                                    exc_info=True)
                                raise
                            LOG.critical(
                                'Migration volume %s committed but its local '
                                'journal intent could not be retired',
                                volume_id, instance=instance, exc_info=True)
        if not self.driver.publish_migration_target_volumes_complete(
                instance, cleanup_token, migration_uuid):
            raise exception.MigrationError(
                reason='Incus migration target retains a local Cinder '
                       'volume transaction after Nova committed attachments')

    def _finish_revert_resize(
            self, context, instance, migration, request_spec=None):
        """Validate and reuse retained source mounts before source restart."""
        self._handoff_cold_source_rotations_for_revert(
            context, instance, migration)
        share_info = [
            mapping
            for mapping in self._get_share_info(context, instance)
            if mapping.status == obj_fields.ShareMappingStatus.ACTIVE
        ]
        self._mount_all_shares(context, instance, share_info)
        return super()._finish_revert_resize(
            context, instance, migration, request_spec=request_spec)

    def _handoff_cold_source_rotations_for_revert(
            self, context, instance, migration):
        """Atomically move completed source-release owners to revert attach."""
        if (migration.source_compute != self.host or
                instance.host not in (
                    migration.source_compute, migration.dest_compute) or
                instance.task_state != task_states.RESIZE_REVERTING or
                not uuidutils.is_uuid_like(getattr(migration, 'uuid', None))):
            raise exception.MigrationError(
                reason='Cold revert does not have exact source ownership')
        bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(
            context, instance.uuid)
        for bdm in bdms:
            volume_id = getattr(bdm, 'volume_id', None)
            if not uuidutils.is_uuid_like(volume_id):
                continue
            with contextlib.ExitStack() as locks:
                locks.enter_context(lockutils.lock(
                    _volume_manager_transaction_lock_name(
                        instance.uuid, volume_id),
                    external=True,
                    lock_path=_volume_manager_transaction_lock_path()))
                locks.enter_context(lockutils.lock(
                    incus_driver._volume_topology_lock_name(instance),
                    external=True,
                    lock_path=incus_driver._volume_topology_lock_path()))
                locks.enter_context(lockutils.lock(
                    incus_driver._volume_operation_lock_name(volume_id),
                    external=True,
                    lock_path=incus_driver._volume_operation_lock_path()))
                intent = self.driver.get_managed_volume_attach_intent(
                    instance, volume_id)
                rotation = self.driver.get_cold_attachment_rotation(
                    instance, volume_id)
                if intent is None and rotation is None:
                    continue
                if rotation is None:
                    self._validate_handed_off_cold_revert_intent_locked(
                        context, instance, volume_id, intent, bdm, migration)
                    continue
                if intent is None or rotation is None:
                    raise exception.InvalidVolume(
                        reason='Cold revert source generation is incomplete')
                expected = {
                    'operation_kind': 'migration',
                    'operation_token': migration.uuid,
                    'operation_direction': 'cold-source-restore',
                    'operation_migration_uuid': migration.uuid,
                    'mountpoint': rotation.get('mountpoint'),
                    'boot_volume': rotation.get('boot_volume'),
                    'attachment_id': rotation.get('old_attachment_id'),
                }
                if (rotation.get('operation_token') != migration.uuid or
                        rotation.get('migration_uuid') != migration.uuid or
                        rotation.get('phase') != 'bdm-rotated' or
                        any(intent.get(key) != value
                            for key, value in expected.items())):
                    raise exception.InvalidVolume(
                        reason='Cold revert source generation owner changed')
                current_attachment_id = getattr(bdm, 'attachment_id', None)
                if (not uuidutils.is_uuid_like(current_attachment_id) or
                        current_attachment_id in {
                            rotation['old_attachment_id'],
                            rotation['new_attachment_id']} or
                        getattr(bdm, 'device_name', None) !=
                        rotation['mountpoint']):
                    raise exception.InvalidVolume(
                        reason='Cold revert BDM has no replacement source')
                for stale_attachment_id in (
                        rotation['old_attachment_id'],
                        rotation['new_attachment_id']):
                    if self._get_exact_cinder_attachment(
                            context, stale_attachment_id, volume_id,
                            instance.uuid) is not None:
                        raise exception.InvalidVolume(
                            reason='Cold revert retains a prior Cinder owner')
                source_attachment = self._get_exact_cinder_attachment(
                    context, current_attachment_id, volume_id, instance.uuid)
                if (source_attachment is None or
                        _attachment_status(source_attachment) not in (
                            'reserved', 'attaching')):
                    raise exception.InvalidVolume(
                        reason='Cold revert replacement source is invalid')
                if rotation['boot_volume']:
                    if (self.driver.get_volume_journal_phase(
                            instance, volume_id) is not None or
                            self.driver.
                            get_internal_volume_attach_connection_info(
                                instance, volume_id,
                                rotation['mountpoint']) is not None):
                        raise exception.InvalidVolume(
                            reason='Cold revert BFV has local data evidence')
                elif self.driver.get_volume_journal_phase(
                        instance, volume_id) != 'disconnected':
                    raise exception.InvalidVolume(
                        reason='Cold revert data volume is not disconnected')
                replacement = (
                    self.driver.replace_cold_source_volume_attach_intent(
                        instance, volume_id, intent, current_attachment_id,
                        operation_direction='cold-revert-source'))
                if replacement.get('attachment_id') != current_attachment_id:
                    raise exception.InvalidVolume(
                        reason='Cold revert source intent replacement failed')
                self._retire_handed_off_cold_rotation_locked(
                    context, instance, volume_id, replacement, bdm, rotation,
                    migration)

    def _validate_handed_off_cold_revert_intent_locked(
            self, context, instance, volume_id, intent, bdm, migration):
        """Validate a retry after the source rotation was retired."""
        attachment_id = getattr(bdm, 'attachment_id', None)
        boot_volume = self._cold_rotation_is_boot_volume(bdm)
        expected = {
            'operation_kind': 'migration',
            'operation_token': migration.uuid,
            'operation_direction': 'cold-revert-source',
            'operation_migration_uuid': migration.uuid,
            'mountpoint': getattr(bdm, 'device_name', None),
            'boot_volume': boot_volume,
            'attachment_id': attachment_id,
        }
        if (not isinstance(intent, dict) or
                not uuidutils.is_uuid_like(attachment_id) or
                not isinstance(expected['mountpoint'], str) or
                not expected['mountpoint'] or
                any(intent.get(key) != value
                    for key, value in expected.items())):
            raise exception.InvalidVolume(
                reason='Cold revert handed-off generation changed')
        source_attachment = self._get_exact_cinder_attachment(
            context, attachment_id, volume_id, instance.uuid)
        if (source_attachment is None or
                _attachment_status(source_attachment) not in (
                    'reserved', 'attaching')):
            raise exception.InvalidVolume(
                reason='Cold revert handed-off source is invalid')
        journal_phase = self.driver.get_volume_journal_phase(
            instance, volume_id)
        connection_info = (
            self.driver.get_internal_volume_attach_connection_info(
                instance, volume_id, expected['mountpoint']))
        if boot_volume:
            if journal_phase is not None or connection_info is not None:
                raise exception.InvalidVolume(
                    reason='Cold revert BFV has local data evidence')
        elif journal_phase != 'disconnected':
            raise exception.InvalidVolume(
                reason='Cold revert data volume is not disconnected')

    def _retire_handed_off_cold_rotation_locked(
            self, context, instance, volume_id, intent, bdm, rotation,
            migration):
        """Retire the old rotation after its revert intent is durable."""
        if (intent.get('operation_kind') != 'migration' or
                intent.get('operation_direction') != 'cold-revert-source' or
                intent.get('operation_token') != migration.uuid or
                intent.get('operation_migration_uuid') != migration.uuid or
                rotation.get('operation_token') != migration.uuid or
                rotation.get('migration_uuid') != migration.uuid or
                rotation.get('phase') != 'bdm-rotated' or
                intent.get('attachment_id') !=
                getattr(bdm, 'attachment_id', None) or
                intent.get('mountpoint') != rotation.get('mountpoint') or
                intent.get('boot_volume') != rotation.get('boot_volume')):
            raise exception.InvalidVolume(
                reason='Cold revert handoff generation changed')
        if intent['attachment_id'] in {
                rotation['old_attachment_id'],
                rotation['new_attachment_id']}:
            raise exception.InvalidVolume(
                reason='Cold revert handoff retained a prior owner')
        for stale_attachment_id in (
                rotation['old_attachment_id'], rotation['new_attachment_id']):
            if self._get_exact_cinder_attachment(
                    context, stale_attachment_id, volume_id,
                    instance.uuid) is not None:
                raise exception.InvalidVolume(
                    reason='Cold revert handoff retains a Cinder owner')
        self.driver.cancel_cold_attachment_rotation(
            instance, volume_id, rotation)

    @periodic_task.periodic_task(
        spacing=CONF.incus.migration_recovery_interval)
    def _recover_incus_cleanup_profiles(self, context):
        """Replay cleanup journals left on a former instance host."""
        if not CONF.incus.migration_auto_recovery:
            return

        context = (
            context or nova.context.get_admin_context()
        ).elevated(read_deleted='yes')
        candidates = self.driver.list_cleanup_recovery_candidates()
        if not candidates:
            self._incus_cleanup_recovery_cursor = 0
            return

        start = (
            getattr(self, '_incus_cleanup_recovery_cursor', 0) %
            len(candidates))
        ordered = candidates[start:] + candidates[:start]
        candidates = ordered[:CONF.incus.migration_recovery_batch_size]
        self._incus_cleanup_recovery_cursor = (
            start + len(candidates)) % len(ordered)

        for candidate in candidates:
            try:
                instance = objects.Instance.get_by_uuid(
                    context, candidate['uuid'],
                    expected_attrs=['info_cache'])
            except exception.InstanceNotFound:
                LOG.error(
                    'Cleanup-marked Incus profile %(name)s references '
                    'deleted Nova instance %(uuid)s; leaving it for Nova '
                    'deleted-instance reconciliation',
                    candidate)
                continue

            if instance.name != candidate['name']:
                LOG.error(
                    'Cleanup-marked Incus profile %(name)s does not match '
                    'Nova instance %(uuid)s name %(instance_name)s',
                    {**candidate, 'instance_name': instance.name})
                continue
            deleted = (
                instance.obj_attr_is_set('deleted') and instance.deleted)
            if not deleted and instance.task_state is not None:
                # A live in-flight operation owns its own cleanup. A deleted
                # row's task state is frozen history (this context reads
                # deleted rows, so InstanceNotFound never fires for them) and
                # must not retain the profile forever.
                LOG.debug(
                    'Skipping cleanup profile while the Nova instance has '
                    'task state %(task_state)s',
                    {'task_state': instance.task_state}, instance=instance)
                continue

            try:
                info_cache = getattr(instance, 'info_cache', None)
                network_info = (
                    info_cache.network_info
                    if (
                        info_cache is not None and
                        info_cache.network_info is not None
                    )
                    else self.network_api.get_instance_nw_info(
                        context, instance)
                )
                self.driver.recover_cleanup_profile(
                    context, instance, network_info)
            except Exception:
                LOG.exception(
                    'Automatic Incus migration cleanup recovery failed',
                    instance=instance)

    @periodic_task.periodic_task(
        spacing=CONF.incus.migration_recovery_interval)
    def _recover_incus_source_volume_generations(self, context):
        """Retire source rollback tokens after volume evidence is gone."""
        if not CONF.incus.migration_auto_recovery:
            return

        context = (
            context or nova.context.get_admin_context()
        ).elevated(read_deleted='yes')
        candidates = (
            self.driver.list_source_volume_generation_recovery_candidates())
        if not candidates:
            self._incus_source_volume_generation_cursor = 0
            return
        start = (
            getattr(self, '_incus_source_volume_generation_cursor', 0) %
            len(candidates))
        ordered = candidates[start:] + candidates[:start]
        selected = ordered[:CONF.incus.migration_recovery_batch_size]
        self._incus_source_volume_generation_cursor = (
            start + len(selected)) % len(ordered)
        for candidate in selected:
            instance = None
            with lockutils.lock(
                    _share_recovery_lock_name(candidate['uuid']),
                    external=True, lock_path=CONF.state_path):
                try:
                    instance = objects.Instance.get_by_uuid(
                        context, candidate['uuid'])
                    deleted = (
                        instance.obj_attr_is_set('deleted') and
                        instance.deleted)
                    if (deleted or
                            instance.name != candidate['name'] or
                            instance.host != self.host or
                            instance.task_state is not None):
                        LOG.debug(
                            'Retaining Incus source rollback generation '
                            'while Nova ownership is unresolved',
                            instance=instance)
                        continue
                    migrations = objects.MigrationList.get_by_filters(
                        context, {'instance_uuid': instance.uuid})
                    exact = [
                        migration for migration in migrations
                        if (getattr(migration, 'uuid', None) ==
                            candidate['migration_uuid'])]
                    if len(exact) != 1:
                        raise exception.MigrationError(
                            reason='Incus source rollback generation does not '
                                   'match exactly one Nova migration')
                    migration = exact[0]
                    if (migration.source_compute != self.host or
                            migration.status not in (
                                 'cancelled', 'error', 'failed', 'reverted')):
                        raise exception.MigrationError(
                            reason='Nova has not committed source ownership '
                                   'for the Incus rollback generation')
                    is_live = (
                        getattr(migration, 'migration_type', None) ==
                        'live-migration')
                    if not candidate.get('rollback_complete', True):
                        if not is_live:
                            raise exception.MigrationError(
                                reason='Incomplete Incus source rollback is '
                                       'not a live migration')
                        network_info = self.network_api.get_instance_nw_info(
                            context, instance)
                        self.driver.recover_live_migration_rollback(
                            context, instance,
                            candidate['operation_token'],
                            candidate['migration_uuid'], network_info)
                    if is_live or migration.status == 'reverted':
                        finalized = (
                            self.driver.
                            finalize_remote_source_volume_generation(
                                instance, candidate['operation_token']))
                    else:
                        finalized = (
                            self.driver.
                            finalize_failed_cold_source_volume_generation(
                                instance, candidate['operation_token']))
                    if not finalized:
                        LOG.debug(
                            'Retaining Incus source rollback generation '
                            'until its volume intents are retired',
                            instance=instance)
                except exception.InstanceNotFound:
                    LOG.error(
                        'Incus source rollback generation %(operation_token)s '
                        'references deleted Nova instance %(uuid)s; refusing '
                        'automatic retirement', candidate)
                except Exception:
                    LOG.exception(
                        'Automatic Incus source rollback generation recovery '
                        'failed', instance=instance)

    @periodic_task.periodic_task(
        spacing=CONF.incus.migration_recovery_interval)
    def _recover_incus_destination_profiles(self, context):
        """Reconcile target profiles after their pre-mount journals vanish."""
        if not CONF.incus.migration_auto_recovery:
            return

        context = (
            context or nova.context.get_admin_context()
        ).elevated(read_deleted='yes')
        try:
            candidates = (
                self.driver.list_destination_prepared_recovery_candidates())
        except Exception:
            LOG.exception(
                'Failed to list prepared Incus migration destinations')
            return
        if not candidates:
            self._incus_destination_recovery_cursor = 0
            return

        start = (
            getattr(self, '_incus_destination_recovery_cursor', 0) %
            len(candidates))
        ordered = candidates[start:] + candidates[:start]
        candidates = ordered[:CONF.incus.migration_recovery_batch_size]
        self._incus_destination_recovery_cursor = (
            start + len(candidates)) % len(ordered)

        for candidate in candidates:
            with lockutils.lock(
                    _share_recovery_lock_name(candidate['uuid']),
                    external=True, lock_path=CONF.state_path):
                try:
                    instance = objects.Instance.get_by_uuid(
                        context, candidate['uuid'],
                        expected_attrs=['info_cache'])
                except exception.InstanceNotFound:
                    LOG.error(
                        'Prepared Incus destination %(name)s references '
                        'deleted Nova instance %(uuid)s; refusing automatic '
                        'cleanup', candidate)
                    continue
                if instance.deleted:
                    LOG.error(
                        'Prepared Incus destination %(name)s references a '
                        'soft-deleted Nova instance %(uuid)s; refusing '
                        'automatic cleanup', candidate)
                    continue
                if instance.name != candidate['name']:
                    LOG.error(
                        'Prepared Incus destination %(name)s does not match '
                        'Nova instance %(uuid)s name %(instance_name)s',
                        {**candidate, 'instance_name': instance.name})
                    continue
                if (instance.host is None or
                        instance.task_state is not None or
                        instance.vm_state == vm_states.RESIZED):
                    LOG.debug(
                        'Retaining prepared Incus destination while Nova '
                        'migration ownership is unresolved',
                        instance=instance)
                    continue

                try:
                    migrations = objects.MigrationList.get_by_filters(
                        context, {'instance_uuid': instance.uuid})
                except Exception:
                    LOG.exception(
                        'Failed to verify Nova migration ownership for a '
                        'prepared Incus destination', instance=instance)
                    continue
                exact = [
                    migration for migration in migrations
                    if (getattr(migration, 'uuid', None) ==
                        candidate['migration_uuid'] and
                        self.host in (
                            migration.source_compute,
                            migration.dest_compute))
                ]
                if len(exact) != 1:
                    LOG.error(
                        'Prepared Incus destination token %(token)s does not '
                        'match exactly one Nova migration involving compute '
                        '%(host)s; refusing automatic cleanup',
                        {
                            'token': candidate['operation_token'],
                            'host': self.host,
                        }, instance=instance)
                    continue
                migration = exact[0]
                if migration.status not in _TERMINAL_MIGRATION_STATUSES:
                    LOG.debug(
                        'Retaining prepared Incus destination while exact '
                        'Nova migration %(uuid)s remains %(status)s',
                        {
                            'uuid': migration.uuid,
                            'status': migration.status,
                        }, instance=instance)
                    continue

                try:
                    info_cache = getattr(instance, 'info_cache', None)
                    network_info = (
                        info_cache.network_info
                        if (info_cache is not None and
                            info_cache.network_info is not None)
                        else self.network_api.get_instance_nw_info(
                            context, instance))
                    self.driver.recover_destination_prepared_profile(
                        context, instance, candidate, migration,
                        network_info)
                except Exception:
                    LOG.exception(
                        'Automatic prepared Incus destination recovery '
                        'failed; retaining all resources', instance=instance)

    @periodic_task.periodic_task(
        spacing=CONF.incus.migration_recovery_interval)
    def _recover_incus_share_journals(self, context):
        """Clean journal-only mounts after a terminal migration."""
        if not CONF.incus.migration_auto_recovery:
            return

        context = (
            context or nova.context.get_admin_context()
        ).elevated(read_deleted='yes')
        try:
            candidates = (
                self.driver.list_share_journal_recovery_candidates())
        except Exception:
            LOG.exception('Failed to list Incus Manila journal recovery work')
            return
        if not candidates:
            self._incus_share_recovery_cursor = 0
            return

        start = (
            getattr(self, '_incus_share_recovery_cursor', 0) %
            len(candidates))
        ordered = candidates[start:] + candidates[:start]
        candidates = ordered[:CONF.incus.migration_recovery_batch_size]
        self._incus_share_recovery_cursor = (
            start + len(candidates)) % len(ordered)

        for candidate in candidates:
            with lockutils.lock(
                    _share_recovery_lock_name(candidate['uuid']),
                    external=True, lock_path=CONF.state_path):
                try:
                    instance = objects.Instance.get_by_uuid(
                        context, candidate['uuid'])
                except exception.InstanceNotFound:
                    LOG.error(
                        'Manila journal %(operation_token)s references '
                        'deleted Nova instance %(uuid)s; refusing automatic '
                        'unmount', candidate)
                    continue
                if instance.deleted:
                    LOG.error(
                        'Manila journal %(operation_token)s references a '
                        'soft-deleted Nova instance %(uuid)s; refusing '
                        'automatic unmount', candidate)
                    continue
                if instance.name != candidate['name']:
                    LOG.error(
                        'Manila journal owner %(name)s does not match Nova '
                        'instance %(uuid)s name %(instance_name)s',
                        {**candidate, 'instance_name': instance.name})
                    continue
                if (instance.host is None or
                        instance.task_state is not None or
                        instance.vm_state == vm_states.RESIZED):
                    LOG.debug(
                        'Retaining Manila journal while Nova migration '
                        'ownership is unresolved', instance=instance)
                    continue

                try:
                    migrations = objects.MigrationList.get_by_filters(
                        context, {'instance_uuid': instance.uuid})
                except Exception:
                    LOG.exception(
                        'Failed to verify Nova migration ownership for a '
                        'Manila journal; retaining the mount',
                        instance=instance)
                    continue
                exact = [
                    migration for migration in migrations
                    if (getattr(migration, 'uuid', None) ==
                        candidate['operation_token'] and
                        self.host in (
                        migration.source_compute,
                        migration.dest_compute))
                ]
                if len(exact) != 1:
                    LOG.error(
                        'Manila journal owner token %(token)s does not match '
                        'exactly one Nova migration involving compute '
                        '%(host)s; refusing automatic unmount',
                        {
                            'token': candidate['operation_token'],
                            'host': self.host,
                        }, instance=instance)
                    continue
                migration = exact[0]
                if migration.status not in _TERMINAL_MIGRATION_STATUSES:
                    LOG.debug(
                        'Retaining Manila journal while exact Nova migration '
                        '%(uuid)s remains %(status)s',
                        {
                            'uuid': migration.uuid,
                            'status': migration.status,
                        }, instance=instance)
                    continue
                try:
                    self.driver.recover_share_journal_candidate(
                        instance, candidate)
                except Exception:
                    LOG.exception(
                        'Automatic Incus Manila journal recovery failed',
                        instance=instance)

    @periodic_task.periodic_task(
        spacing=CONF.incus.migration_recovery_interval)
    def _recover_incus_volume_journals(self, context):
        """Finish volume work this compute started but did not complete.

        The driver's journal is durable before both connect and disconnect,
        so a process that dies inside either leaves one behind and the
        operation can be replayed. Replay is safe because os-brick connect
        and disconnect are idempotent for an exact volume identity.

        A journal is only evidence that *this* compute began the work. It is
        never sufficient on its own: the exact Nova instance must still exist
        here and be free of an in-flight task.  Connecting journals are then
        reconciled against the exact Nova BDM and Cinder attachment; any
        contradictory or ambiguous ownership evidence keeps the journal and
        reports it, because acting on a volume whose ownership moved could
        disconnect storage another host is using.
        """
        context = context or nova.context.get_admin_context()
        try:
            candidates = (
                self.driver.list_volume_journal_recovery_candidates())
        except Exception:
            LOG.exception('Failed to list Incus Cinder journal recovery work')
            return

        for candidate in candidates:
            instance_uuid = candidate['uuid']
            phases = candidate.get('phases')
            if (not isinstance(phases, dict) or
                    set(phases) != set(candidate.get('volume_ids', ())) or
                    any(not _valid_volume_recovery_phase(phase)
                        for phase in phases.values())):
                LOG.error(
                    'Cinder journal recovery candidate has an incomplete '
                    'phase inventory for instance %s; refusing automatic '
                    'recovery', instance_uuid)
                continue
            for volume_id in candidate.get('volume_ids', ()):
                try:
                    with contextlib.ExitStack() as locks:
                        # Migration callbacks acquire this instance lock before
                        # entering Nova's Cinder flow.  Taking it before the
                        # transaction/topology/volume locks prevents periodic
                        # recovery from observing the short interval between a
                        # driver return and attachment_complete().
                        locks.enter_context(lockutils.lock(
                            _share_recovery_lock_name(instance_uuid),
                            external=True, lock_path=CONF.state_path))
                        locks.enter_context(lockutils.lock(
                            _volume_manager_transaction_lock_name(
                                instance_uuid, volume_id),
                            external=True,
                            lock_path=(
                                _volume_manager_transaction_lock_path())))
                        # The transaction lock fences a still-running Nova
                        # attach/detach from its periodic recovery. Acquire it
                        # before the topology and driver volume locks in every
                        # path, then refresh all mutable authority beneath it.
                        lock_instance = objects.Instance.get_by_uuid(
                            context, instance_uuid)
                        with lockutils.lock(
                                incus_driver._volume_topology_lock_name(
                                    lock_instance),
                                external=True,
                                lock_path=(
                                    incus_driver.
                                    _volume_topology_lock_path())):
                            instance = objects.Instance.get_by_uuid(
                                context, instance_uuid)
                            attach_intent = (
                                self.driver.get_managed_volume_attach_intent(
                                    instance, volume_id))
                            rotation = (
                                self.driver.get_cold_attachment_rotation(
                                    instance, volume_id))
                            migration_target = (
                                attach_intent is not None and
                                attach_intent.get('operation_kind') ==
                                'migration' and
                                attach_intent.get('operation_direction') in (
                                     'cold-target', 'live-target'))
                            migration_source_release = (
                                attach_intent is not None and
                                attach_intent.get('operation_kind') ==
                                'migration' and
                                attach_intent.get('operation_direction') in (
                                    'cold-source-restore',
                                    'live-source-release'))
                            terminal_source_release = (
                                rotation is not None and
                                rotation.get('phase') ==
                                'source-release-complete')
                            internal_attach = (
                                (attach_intent is not None and
                                 attach_intent.get('operation_kind') !=
                                 'hot-attach') or rotation is not None)
                            if ((instance.host != self.host and
                                 not migration_target and
                                 not migration_source_release and
                                 not terminal_source_release) or
                                    (instance.task_state and
                                     not internal_attach)):
                                LOG.debug(
                                    'Retaining Cinder journal while Nova '
                                    'ownership is unresolved',
                                    instance=instance)
                                continue
                            current_phase = (
                                self.driver.
                                get_volume_journal_recovery_phase(
                                    instance, volume_id))
                            if current_phase is None:
                                LOG.debug(
                                    'Cinder volume recovery candidate '
                                    '%(volume)s completed while waiting for '
                                    'its transaction lock',
                                    {'volume': volume_id}, instance=instance)
                                continue
                            if (current_phase in (
                                    'attach-pending', 'connecting',
                                    'connected', 'rolled-back',
                                    'attach-disconnecting',
                                    'attach-disconnected') or
                                    (isinstance(current_phase, str) and
                                     current_phase.startswith('rotation-'))):
                                self._recover_incus_connecting_volume_journal(
                                    context, instance, volume_id,
                                    journal_phase=current_phase)
                            elif current_phase in (
                                    'detach-pending', 'disconnecting',
                                    'disconnected'):
                                recover = getattr(
                                    self,
                                    '_recover_incus_disconnecting_'
                                    'volume_journal')
                                recover(
                                    context, instance, volume_id,
                                    journal_phase=current_phase)
                            else:
                                raise exception.InvalidVolume(
                                    reason='Cinder volume has conflicting '
                                           'managed attach and detach '
                                           'intents')
                except exception.InstanceNotFound:
                    LOG.error(
                        'Cinder journal for %s references a deleted Nova '
                        'instance; retaining it', instance_uuid)
                except Exception:
                    LOG.exception(
                        'Automatic recovery of unfinished Cinder volume work '
                        'failed for volume %s on instance %s',
                        volume_id, instance_uuid)

    def _get_exact_cinder_attachment(
            self, context, attachment_id, volume_id, instance_uuid):
        """Fetch one attachment by globally unique ID or return its absence."""
        try:
            attachment = self.volume_api.attachment_get(
                context, attachment_id)
        except exception.VolumeAttachmentNotFound:
            return None
        detail_id, detail_volume, detail_instance, _ = (
            _validated_attachment_identity(attachment))
        if (detail_id != attachment_id or detail_volume != volume_id or
                detail_instance != instance_uuid):
            raise exception.InvalidVolume(
                reason='Cinder attachment identity changed during recovery')
        return attachment

    def _recover_incus_disconnecting_volume_journal(
            self, context, instance, volume_id, journal_phase=None):
        """Finish only a detach explicitly authorized by ComputeManager."""
        if journal_phase not in (
                None, 'detach-pending', 'disconnecting', 'disconnected'):
            raise exception.InvalidVolume(
                reason='Cinder detach recovery received an invalid phase')
        with lockutils.lock(
                incus_driver._volume_operation_lock_name(volume_id),
                external=True,
                lock_path=incus_driver._volume_operation_lock_path()):
            if journal_phase is not None:
                actual_phase = self.driver.get_volume_journal_phase(
                    instance, volume_id)
                expected_phase = (
                    None if journal_phase == 'detach-pending'
                    else journal_phase)
                if actual_phase != expected_phase:
                    raise exception.InvalidVolume(
                        reason='Cinder detach journal changed before its '
                               'recovery lock was acquired')
            intent = self.driver.get_managed_volume_detach_intent(
                instance, volume_id)
            if intent is None:
                raise exception.InvalidVolume(
                    reason='Disconnect journal has no Nova managed detach '
                           'intent; refusing Cinder or BDM cleanup')
            bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(
                context, instance.uuid)
            matching_bdms = [
                bdm for bdm in bdms
                if (bdm.volume_id == volume_id and
                    not getattr(bdm, 'deleted', False))
            ]
            if len(matching_bdms) > 1:
                raise exception.InvalidVolume(
                    reason='Nova has duplicate BDMs during detach recovery')
            bdm = matching_bdms[0] if matching_bdms else None
            if bdm is None:
                if journal_phase == 'detach-pending':
                    self.driver.validate_disconnected_volume_state(
                        instance, volume_id)
                    attachment = self._get_exact_cinder_attachment(
                        context, intent['attachment_id'], volume_id,
                        instance.uuid)
                    volume = self.volume_api.get(context, volume_id)
                    _, completed_refs = (
                        _validated_volume_ownership(volume, volume_id))
                    if (attachment is not None or any(
                            ref['id'] == intent['attachment_id']
                            for ref in completed_refs)):
                        raise exception.InvalidVolume(
                            reason='Terminal detach intent still has its '
                                   'original Cinder attachment')
                    self.driver.cancel_managed_volume_detach(
                        instance, volume_id, intent)
                    return
                if (journal_phase != 'disconnected' or
                        not intent['destroy_bdm']):
                    raise exception.InvalidVolume(
                        reason='Nova BDM disappeared before host detach '
                               'recovery committed')
                self.driver.finalize_disconnected_volume_journal(
                    instance, volume_id)
                self.driver.cancel_managed_volume_detach(
                    instance, volume_id, intent)
                return

            attachment_id = getattr(bdm, 'attachment_id', None)
            mountpoint = getattr(bdm, 'device_name', None)
            if (attachment_id != intent['attachment_id'] or
                    mountpoint != intent['mountpoint']):
                raise exception.InvalidVolume(
                    reason='Nova BDM no longer matches managed detach intent')
            bdm_connection_info = _bdm_connection_info(bdm)
            canonical_bdm = _canonical_attachment_connection_info(
                bdm_connection_info, volume_id, instance.uuid)
            attachment = self._get_exact_cinder_attachment(
                context, attachment_id, volume_id, instance.uuid)

            if journal_phase == 'detach-pending':
                if (attachment is None or
                        _attachment_status(attachment) != 'attached'):
                    raise exception.InvalidVolume(
                        reason='Pre-driver detach has no exact attached '
                               'Cinder attachment')
                connection_info = _attachment_connection_info(attachment)
                if (_canonical_attachment_connection_info(
                        connection_info, volume_id, instance.uuid) !=
                        canonical_bdm):
                    raise exception.InvalidVolume(
                        reason='Nova BDM and pre-driver detach connection '
                               'information disagree')
                volume = self.volume_api.get(context, volume_id)
                volume_status, completed_refs = _validated_volume_ownership(
                    volume, volume_id)
                expected_ref = {
                    'id': attachment_id,
                    'instance_uuid': instance.uuid,
                    'mountpoint': mountpoint,
                }
                if completed_refs != [expected_ref] or volume_status not in (
                        'detaching', 'in-use'):
                    raise exception.InvalidVolume(
                        reason='Cinder volume does not prove the exact '
                               'pre-driver detach owner')
                # No driver journal means guest access must still be exact.
                # Reuse the same profile proof as a committed attach without
                # deleting or recreating any host mapping.
                self.driver.confirm_connected_volume_journal(
                    instance, volume_id, bdm_connection_info,
                    expected_mountpoint=mountpoint)
                if volume_status == 'detaching':
                    self.volume_api.roll_detaching(context, volume_id)
                self.driver.cancel_managed_volume_detach(
                    instance, volume_id, intent)
                return

            if not intent['destroy_bdm']:
                # Rebuild creates a replacement attachment outside this
                # method and only saves it after _detach_volume returns. A
                # restarted manager cannot reconstruct that attachment ID.
                raise exception.InvalidVolume(
                    reason='Interrupted destroy_bdm=False detach requires '
                           'explicit Nova rebuild recovery')
            if attachment is not None:
                attachment_status = _attachment_status(attachment)
                if attachment_status not in (
                        'attached', 'error_detaching', 'detached', 'deleted'):
                    raise exception.InvalidVolume(
                        reason='Cinder attachment is not in a detachable '
                               'recovery state')
                connection_info = _attachment_connection_info(attachment)
                if (_canonical_attachment_connection_info(
                        connection_info, volume_id, instance.uuid) !=
                        canonical_bdm):
                    raise exception.InvalidVolume(
                        reason='Nova BDM and Cinder detach connection '
                               'information disagree')

            if journal_phase != 'disconnected':
                volume = self.volume_api.get(context, volume_id)
                volume_status, completed_refs = _validated_volume_ownership(
                    volume, volume_id)
                for completed in completed_refs:
                    if (completed['id'] != attachment_id or
                            completed['instance_uuid'] != instance.uuid or
                            completed['mountpoint'] != mountpoint):
                        raise exception.InvalidVolume(
                            reason='Cinder volume has another completed '
                                   'owner; refusing stale host disconnect')
                if attachment is None and (
                        completed_refs or volume_status != 'available'):
                    raise exception.InvalidVolume(
                        reason='Missing detach attachment is not proven '
                               'released by Cinder volume state')
                if (attachment is not None and
                        _attachment_status(attachment) == 'attached' and
                        not completed_refs):
                    raise exception.InvalidVolume(
                        reason='Cinder attachment detail and volume ownership '
                               'view disagree during detach recovery')

            if journal_phase != 'disconnected':
                self.driver._recover_disconnecting_volume_journal_locked(
                    context, instance, volume_id, bdm_connection_info,
                    expected_mountpoint=mountpoint)
            if attachment is not None:
                try:
                    self.volume_api.attachment_delete(context, attachment_id)
                except exception.VolumeAttachmentNotFound:
                    pass
            if intent['destroy_bdm']:
                bdm.destroy()
            self.driver.finalize_disconnected_volume_journal(
                instance, volume_id)
            self.driver.cancel_managed_volume_detach(
                instance, volume_id, intent)

    def _recover_incus_connecting_volume_journal(
            self, context, instance, volume_id, journal_phase=None):
        """Serialize one attach recovery against all same-host volume work."""
        with lockutils.lock(
                incus_driver._volume_operation_lock_name(volume_id),
                external=True,
                lock_path=incus_driver._volume_operation_lock_path()):
            if journal_phase is not None:
                if journal_phase.startswith('rotation-'):
                    actual_phase = (
                        self.driver.get_volume_journal_recovery_phase(
                            instance, volume_id))
                    expected_phase = journal_phase
                else:
                    actual_phase = self.driver.get_volume_journal_phase(
                        instance, volume_id)
                    expected_phase = {
                        'attach-pending': None,
                        'attach-disconnecting': 'disconnecting',
                        'attach-disconnected': 'disconnected',
                    }.get(journal_phase, journal_phase)
                if actual_phase != expected_phase:
                    raise exception.InvalidVolume(
                        reason='Cinder attach journal changed before its '
                               'recovery lock was acquired')
            return self._recover_incus_connecting_volume_journal_locked(
                context, instance, volume_id, journal_phase=journal_phase)

    def _recover_incus_attach_pending_locked(
            self, context, instance, volume_id, intent, bdm, attachment,
            status, connection_info):
        """Converge the pre-driver or post-commit attach intent window."""
        attachment_id = intent['attachment_id']
        if status == 'attached':
            if bdm is None or not connection_info.get('driver_volume_type'):
                raise exception.InvalidVolume(
                    reason='Committed Cinder attachment has no matching Nova '
                           'BDM or connection information')
            bdm_connection_info = _bdm_connection_info(bdm)
            if (_canonical_attachment_connection_info(
                    bdm_connection_info, volume_id, instance.uuid) !=
                    _canonical_attachment_connection_info(
                        connection_info, volume_id, instance.uuid)):
                raise exception.InvalidVolume(
                    reason='Nova BDM and committed Cinder attachment '
                           'connection information disagree')
            self.driver.confirm_connected_volume_journal(
                instance, volume_id, connection_info,
                expected_mountpoint=intent['mountpoint'])
            self.driver.cancel_managed_volume_attach(
                instance, volume_id, intent)
            return

        rollback_statuses = {
            'attaching', 'reserved', 'error_attaching', 'detached',
            'deleted',
        }
        if attachment is not None and status not in rollback_statuses:
            raise exception.InvalidVolume(
                reason='Pre-driver Cinder attachment is in ambiguous state '
                       '%s' % status)
        volume = self.volume_api.get(context, volume_id)
        volume_status, completed_refs = _validated_volume_ownership(
            volume, volume_id)
        if completed_refs:
            raise exception.InvalidVolume(
                reason='Pre-driver Cinder volume has a completed owner')
        if attachment is None and volume_status != 'available':
            raise exception.InvalidVolume(
                reason='Missing pre-driver attachment is not proven released '
                       'by Cinder')
        if attachment is not None:
            try:
                self.volume_api.attachment_delete(context, attachment_id)
            except exception.VolumeAttachmentNotFound:
                pass
        if bdm is not None:
            bdm.destroy()
        self.driver.cancel_managed_volume_attach(
            instance, volume_id, intent)

    def _recover_incus_formal_attach_locked(
            self, context, instance, volume_id, intent, bdm, status,
            connection_info):
        """Preserve or complete one exact authoritative Cinder attach."""
        attachment_id = intent['attachment_id']
        if bdm is None or getattr(bdm, 'attachment_id', None) != attachment_id:
            raise exception.InvalidVolume(
                reason='Cinder volume %s is %s without a matching Nova BDM' %
                       (volume_id, status))
        if not connection_info.get('driver_volume_type'):
            raise exception.InvalidVolume(
                reason='%s Cinder volume %s has no connection information' %
                       (status.capitalize(), volume_id))
        mountpoint = getattr(bdm, 'device_name', None)
        if not isinstance(mountpoint, str) or not mountpoint:
            raise exception.InvalidVolume(
                reason='Nova BDM has no target for %s Cinder volume %s' %
                       (status, volume_id))

        bdm_connection_info = _optional_bdm_connection_info(bdm)
        if status == 'attached' and bdm_connection_info is None:
            raise exception.InvalidVolume(
                reason='Nova BDM has no durable Cinder connection information')
        if (bdm_connection_info is not None and
                _canonical_attachment_connection_info(
                    bdm_connection_info, volume_id, instance.uuid) !=
                _canonical_attachment_connection_info(
                    connection_info, volume_id, instance.uuid)):
            raise exception.InvalidVolume(
                reason='Nova BDM and Cinder attachment connection '
                       'information disagree for %s volume %s' %
                       (status, volume_id))

        if status == 'attaching':
            recovered_mountpoint = (
                self.driver.resume_connecting_volume_journal(
                    context, instance, volume_id, connection_info,
                    expected_mountpoint=mountpoint))
            if recovered_mountpoint != mountpoint:
                raise exception.InvalidVolume(
                    reason='Nova BDM target for volume %s changed during '
                           'attach recovery' % volume_id)
            bdm.connection_info = jsonutils.dumps(connection_info)
            bdm.save()
            self.volume_api.attachment_complete(context, attachment_id)

        self.driver.confirm_connected_volume_journal(
            instance, volume_id, connection_info,
            expected_mountpoint=mountpoint)
        self.driver.cancel_managed_volume_attach(
            instance, volume_id, intent)

    def _rollback_incus_managed_attach_locked(
            self, context, instance, volume_id, journal_phase, intent, bdm,
            attachment, status, connection_info):
        """Roll back one exact failed attach generation and its BDM."""
        rollback_statuses = {
            'reserved', 'error_attaching', 'detached', 'deleted',
        }
        if attachment is not None and status not in rollback_statuses:
            raise exception.InvalidVolume(
                reason='Cinder attachment for volume %s is in ambiguous '
                       'state %s' % (volume_id, status))

        bdm_connection_info = _optional_bdm_connection_info(bdm)
        canonical_bdm = None
        if bdm_connection_info is not None:
            canonical_bdm = _canonical_attachment_connection_info(
                bdm_connection_info, volume_id, instance.uuid)
            if (connection_info and
                    canonical_bdm !=
                    _canonical_attachment_connection_info(
                        connection_info, volume_id, instance.uuid)):
                raise exception.InvalidVolume(
                    reason='Nova BDM and Cinder rollback connection '
                           'information disagree for volume %s' % volume_id)

        if journal_phase != 'rolled-back':
            volume = self.volume_api.get(context, volume_id)
            volume_status, completed_refs = _validated_volume_ownership(
                volume, volume_id)
            if completed_refs:
                raise exception.InvalidVolume(
                    reason='Cinder volume %s has a completed owner; refusing '
                           'attach rollback' % volume_id)
            if attachment is None and volume_status != 'available':
                raise exception.InvalidVolume(
                    reason='Missing Cinder attachment is not proven released '
                           'by volume state for %s' % volume_id)
            if attachment is not None and volume_status not in (
                    'available', 'reserved', 'attaching', 'detaching',
                    'error'):
                raise exception.InvalidVolume(
                    reason='Cinder volume %s is in ambiguous rollback state '
                           '%s' % (volume_id, volume_status))
            if attachment is not None:
                refreshed = self._get_exact_cinder_attachment(
                    context, intent['attachment_id'], volume_id,
                    instance.uuid)
                if refreshed is None:
                    raise exception.InvalidVolume(
                        reason='Cinder attachment disappeared during rollback '
                               'validation for volume %s' % volume_id)
                if _attachment_status(refreshed) not in rollback_statuses:
                    raise exception.InvalidVolume(
                        reason='Cinder attachment changed state during '
                               'rollback recovery for volume %s' % volume_id)
                refreshed_info = _attachment_connection_info(refreshed)
                if refreshed_info:
                    connection_info = dict(refreshed_info)
                    connection_info['data'] = dict(
                        connection_info.get('data') or {})
                    connection_info.setdefault('serial', volume_id)
                    if (canonical_bdm is not None and
                            _canonical_attachment_connection_info(
                                connection_info, volume_id, instance.uuid) !=
                            canonical_bdm):
                        raise exception.InvalidVolume(
                            reason='Nova BDM and refreshed Cinder rollback '
                                   'connection information disagree for '
                                   'volume %s' % volume_id)

        self.driver.rollback_connecting_volume_journal(
            context, instance, volume_id,
            connection_info=connection_info or None,
            expected_mountpoint=intent['mountpoint'])
        if attachment is not None:
            try:
                self.volume_api.attachment_delete(
                    context, intent['attachment_id'])
            except exception.VolumeAttachmentNotFound:
                pass
        if bdm is not None:
            bdm.destroy()
        self.driver.finalize_rolled_back_volume_journal(
            instance, volume_id)
        self.driver.cancel_managed_volume_attach(
            instance, volume_id, intent)

    def _recover_incus_migration_target_attach_locked(
            self, context, instance, volume_id, journal_phase, intent, bdm,
            attachment, status, connection_info, migration):
        """Converge only the destination side of one migration generation."""
        terminal = _TERMINAL_MIGRATION_STATUSES | {'finished'}
        direction = intent['operation_direction']
        formal_live_pre = (
            direction == 'live-target' and
            migration.status not in terminal and status == 'attached')
        if (direction == 'live-target' and migration.status not in terminal and
                not formal_live_pre):
            LOG.debug(
                'Retaining staged target Cinder volume %(volume)s while '
                'migration %(migration)s remains %(status)s',
                {
                    'volume': volume_id,
                    'migration': intent['operation_migration_uuid'],
                    'status': migration.status,
                }, instance=instance)
            return

        attempt_disposition = (
            self.driver.internal_migration_attach_disposition(
                instance, intent))
        if attempt_disposition == 'active' and not formal_live_pre:
            LOG.debug(
                'Retaining staged target Cinder volume %(volume)s while its '
                'Incus migration attempt settles',
                {'volume': volume_id}, instance=instance)
            return
        if (direction == 'cold-target' and migration.status not in terminal and
                attempt_disposition != 'committed'):
            LOG.debug(
                'Retaining non-committed cold target Cinder volume %(volume)s '
                'until Nova records the migration outcome',
                {'volume': volume_id}, instance=instance)
            return

        rollback_statuses = {'cancelled', 'error', 'failed', 'reverted'}
        if formal_live_pre and attempt_disposition != 'aborted':
            disposition = 'formal-live-pre'
        elif (attempt_disposition == 'committed' and
                instance.host == migration.dest_compute == self.host):
            # A late Nova error cannot revoke an already committed Incus
            # target which Nova still names as the instance owner.
            disposition = 'committed'
        elif (migration.status in rollback_statuses and
                instance.host == migration.source_compute and
                migration.source_compute != self.host):
            # Cold revert can legitimately leave a committed target attempt;
            # Nova's restored source ownership authorizes target-local cleanup.
            disposition = 'aborted'
        elif (direction == 'cold-target' and
                attempt_disposition == 'aborted' and
                migration.status in rollback_statuses and
                instance.host == migration.dest_compute == self.host):
            # Nova deliberately leaves a failed finish_resize owned by the
            # destination so a hard reboot can recover it.  Remove only the
            # failed host mapping; keep its formal Cinder BDM/attachment.
            disposition = 'failed-target-owner'
        else:
            raise exception.InvalidVolume(
                reason='Nova and Incus migration target ownership disagree')

        if disposition in ('committed', 'formal-live-pre'):
            return self._recover_incus_formal_migration_target_attach_locked(
                context, instance, volume_id, journal_phase, intent, bdm,
                attachment, status, connection_info, migration, disposition)
        if disposition == 'failed-target-owner':
            return self._recover_incus_failed_migration_target_attach_locked(
                context, instance, volume_id, journal_phase, intent, bdm,
                attachment, status, connection_info)
        return self._recover_incus_aborted_migration_target_attach_locked(
            context, instance, volume_id, intent, bdm, attachment, status,
            connection_info, migration)

    def _recover_incus_formal_migration_target_attach_locked(
            self, context, instance, volume_id, journal_phase, intent, bdm,
            attachment, status, connection_info, migration, disposition):
        """Converge a committed or formally attached migration target."""
        if disposition == 'committed':
            valid_host = (
                migration.dest_compute == self.host and
                instance.host == self.host)
        else:
            valid_host = (
                migration.dest_compute == self.host and
                migration.source_compute != self.host and
                instance.host == migration.source_compute)
        if not valid_host:
            raise exception.InvalidVolume(
                reason='Formal target volume is not owned by the expected '
                       'Nova migration endpoint')
        if (bdm is None or
                getattr(bdm, 'attachment_id', None) !=
                intent['attachment_id'] or
                getattr(bdm, 'device_name', None) != intent['mountpoint']):
            raise exception.InvalidVolume(
                reason='Committed migration target has no exact target '
                       'Nova BDM')
        if attachment is None or status not in ('attaching', 'attached'):
            raise exception.InvalidVolume(
                reason='Committed migration target has no recoverable '
                       'Cinder attachment')
        if not connection_info.get('driver_volume_type'):
            raise exception.InvalidVolume(
                reason='Committed migration target has no Cinder connection '
                       'information')

        canonical_target = _canonical_attachment_connection_info(
            connection_info, volume_id, instance.uuid)
        bdm_connection_info = _optional_bdm_connection_info(bdm)
        if (bdm_connection_info is not None and
                _canonical_attachment_connection_info(
                    bdm_connection_info, volume_id, instance.uuid) !=
                canonical_target):
            raise exception.InvalidVolume(
                reason='Committed migration target BDM and attachment '
                       'connection information disagree')
        restart_phases = {
            'attach-disconnecting', 'attach-disconnected', 'rolled-back'}
        if journal_phase in restart_phases:
            self.driver.restart_internal_volume_attach(
                context, instance, volume_id, connection_info,
                expected_mountpoint=intent['mountpoint'])

        if status == 'attaching':
            if journal_phase not in restart_phases:
                self.driver.resume_internal_volume_attach(
                    context, instance, volume_id, connection_info,
                    expected_mountpoint=intent['mountpoint'])
            bdm.connection_info = jsonutils.dumps(connection_info)
            bdm.save()
            self.volume_api.attachment_complete(
                context, intent['attachment_id'])
            attachment = self._get_exact_cinder_attachment(
                context, intent['attachment_id'], volume_id, instance.uuid)
            if (attachment is None or
                    _attachment_status(attachment) != 'attached'):
                raise exception.InvalidVolume(
                    reason='Cinder did not commit the migration target '
                           'attachment')
            connection_info = _attachment_connection_info(attachment)
            connection_info = dict(connection_info)
            connection_info['data'] = dict(
                connection_info.get('data') or {})
            connection_info.setdefault('serial', volume_id)
        elif bdm_connection_info is None:
            bdm.connection_info = jsonutils.dumps(connection_info)
            bdm.save()

        volume = self.volume_api.get(context, volume_id)
        unused_status, completed_refs = _validated_volume_ownership(
            volume, volume_id)
        # Cinder's volume summary is keyed by server UUID and can fold a
        # same-server source/target attachment pair.  The exact target
        # attachment_get above is the positive identity proof; use this
        # lossy view only to reject a visible conflicting owner.
        if any(
                ref['instance_uuid'] != instance.uuid or
                ref['mountpoint'] != intent['mountpoint']
                for ref in completed_refs):
            raise exception.InvalidVolume(
                reason='Migration target volume has another completed '
                       'Cinder owner')
        self.driver.confirm_connected_volume_journal(
            instance, volume_id, connection_info,
            expected_mountpoint=intent['mountpoint'])
        self.driver.cancel_managed_volume_attach(
            instance, volume_id, intent)
        self.driver.publish_migration_target_volumes_complete(
            instance, intent['operation_token'],
            intent['operation_migration_uuid'])

    def _recover_incus_failed_migration_target_attach_locked(
            self, context, instance, volume_id, journal_phase, intent, bdm,
            attachment, status, connection_info):
        """Remove a failed target mapping while retaining its formal owner."""
        if (bdm is None or
                getattr(bdm, 'attachment_id', None) !=
                intent['attachment_id'] or
                getattr(bdm, 'device_name', None) != intent['mountpoint']):
            raise exception.InvalidVolume(
                reason='Failed cold target has no exact Nova BDM')
        if attachment is None or status not in ('attaching', 'attached'):
            raise exception.InvalidVolume(
                reason='Failed cold target has no recoverable Cinder '
                       'attachment')
        if not connection_info.get('driver_volume_type'):
            raise exception.InvalidVolume(
                reason='Failed cold target has no Cinder connection '
                       'information')
        canonical_target = _canonical_attachment_connection_info(
            connection_info, volume_id, instance.uuid)
        bdm_connection_info = _optional_bdm_connection_info(bdm)
        if (bdm_connection_info is not None and
                _canonical_attachment_connection_info(
                    bdm_connection_info, volume_id, instance.uuid) !=
                canonical_target):
            raise exception.InvalidVolume(
                reason='Failed cold target BDM and attachment disagree')
        if bdm_connection_info is None:
            bdm.connection_info = jsonutils.dumps(connection_info)
            bdm.save()
        if status == 'attaching':
            self.volume_api.attachment_complete(
                context, intent['attachment_id'])
            refreshed = self._get_exact_cinder_attachment(
                context, intent['attachment_id'], volume_id, instance.uuid)
            if (refreshed is None or
                    _attachment_status(refreshed) != 'attached'):
                raise exception.InvalidVolume(
                    reason='Failed cold target Cinder attachment did not '
                           'commit')

        volume = self.volume_api.get(context, volume_id)
        unused_status, completed_refs = _validated_volume_ownership(
            volume, volume_id)
        if any(
                ref['instance_uuid'] != instance.uuid or
                ref['mountpoint'] != intent['mountpoint']
                for ref in completed_refs):
            raise exception.InvalidVolume(
                reason='Failed cold target volume has another completed '
                       'Cinder owner')
        local_connection_info = (
            self.driver.get_internal_volume_attach_connection_info(
                instance, volume_id, intent['mountpoint']))
        if (local_connection_info is not None and
                _canonical_attachment_connection_info(
                    local_connection_info, volume_id, instance.uuid) !=
                canonical_target):
            raise exception.InvalidVolume(
                reason='Failed cold target local and Cinder connection '
                       'information disagree')
        if local_connection_info is not None:
            self.driver.rollback_internal_volume_attach(
                context, instance, volume_id, local_connection_info,
                expected_mountpoint=intent['mountpoint'])
            self.driver.finalize_rolled_back_volume_journal(
                instance, volume_id)
        elif journal_phase != 'attach-pending':
            raise exception.InvalidVolume(
                reason='Failed cold target has no exact local rollback '
                       'evidence')
        self.driver.cancel_managed_volume_attach(
            instance, volume_id, intent)

    def _recover_incus_aborted_migration_target_attach_locked(
            self, context, instance, volume_id, intent, bdm, attachment,
            status, connection_info, migration):
        """Roll back a target mapping after Nova restores the remote source."""
        if (migration.source_compute == self.host or
                instance.host != migration.source_compute):
            raise exception.InvalidVolume(
                reason='Aborted target volume does not have an exact remote '
                       'source owner')

        source_attachment_id = (
            getattr(bdm, 'attachment_id', None) if bdm is not None else None)
        if (not uuidutils.is_uuid_like(source_attachment_id) or
                source_attachment_id == intent['attachment_id'] or
                getattr(bdm, 'device_name', None) != intent['mountpoint']):
            raise exception.InvalidVolume(
                reason='Aborted migration target has no exact restored source '
                       'Nova BDM')
        source_attachment = self._get_exact_cinder_attachment(
            context, source_attachment_id, volume_id, instance.uuid)
        if (source_attachment is None or
                _attachment_status(source_attachment) != 'attached'):
            raise exception.InvalidVolume(
                reason='Aborted migration target has no exact attached source '
                       'Cinder owner')
        source_connection_info = _attachment_connection_info(
            source_attachment)
        source_bdm_connection_info = _optional_bdm_connection_info(bdm)
        if (not source_connection_info.get('driver_volume_type') or
                source_bdm_connection_info is None or
                _canonical_attachment_connection_info(
                    source_connection_info, volume_id, instance.uuid) !=
                _canonical_attachment_connection_info(
                    source_bdm_connection_info, volume_id, instance.uuid)):
            raise exception.InvalidVolume(
                reason='Aborted migration target source BDM and Cinder '
                       'attachment disagree')

        # Nova can restore the BDM to its source attachment and delete the
        # target Cinder attachment before this periodic runs.  Never use that
        # source connection to disconnect the destination host: only the
        # token-bound local journal/profile can name the target mapping.
        local_connection_info = (
            self.driver.get_internal_volume_attach_connection_info(
                instance, volume_id, intent['mountpoint']))
        if (local_connection_info is not None and connection_info and
                _canonical_attachment_connection_info(
                    local_connection_info, volume_id, instance.uuid) !=
                _canonical_attachment_connection_info(
                    connection_info, volume_id, instance.uuid)):
            raise exception.InvalidVolume(
                reason='Aborted migration target local and Cinder connection '
                       'information disagree')

        volume = self.volume_api.get(context, volume_id)
        unused_status, completed_refs = _validated_volume_ownership(
            volume, volume_id)
        if any(
                ref['instance_uuid'] != instance.uuid or
                ref['mountpoint'] != intent['mountpoint']
                for ref in completed_refs):
            raise exception.InvalidVolume(
                reason='Aborted migration target volume has another '
                       'completed Cinder owner')

        if local_connection_info is not None:
            self.driver.rollback_internal_volume_attach(
                context, instance, volume_id, local_connection_info,
                expected_mountpoint=intent['mountpoint'])
            self.driver.finalize_rolled_back_volume_journal(
                instance, volume_id)
        if attachment is not None:
            rollback_safe = {
                'attaching', 'attached', 'reserved', 'error_attaching',
                'error_detaching', 'detached', 'deleted',
            }
            if status not in rollback_safe:
                raise exception.InvalidVolume(
                    reason='Aborted migration target attachment is not in a '
                           'rollback-safe state')
            try:
                self.volume_api.attachment_delete(
                    context, intent['attachment_id'])
            except exception.VolumeAttachmentNotFound:
                pass
            if self._get_exact_cinder_attachment(
                    context, intent['attachment_id'], volume_id,
                    instance.uuid) is not None:
                raise exception.InvalidVolume(
                    reason='Aborted migration target attachment was not '
                           'deleted')
        self.driver.cancel_managed_volume_attach(
            instance, volume_id, intent)

    def _restore_cold_rotation_new_owner_locked(
            self, context, instance, volume_id, intent, bdm, rotation,
            migration):
        """Promote the known replacement attachment back to the source."""
        rotation = self._advance_cold_attachment_rotation_locked(
            context, instance, bdm, rotation)
        if rotation['phase'] != 'bdm-rotated':
            raise exception.InvalidVolume(
                reason='Cold source replacement attachment is not durable')
        attachment = self._validate_cold_rotation_new_attachment(
            context, instance, volume_id, rotation)
        status = _attachment_status(attachment)
        connection_info = _attachment_connection_info(attachment)
        if status == 'reserved':
            connector = self.driver.get_volume_connector(instance)
            try:
                attachment = self.volume_api.attachment_update(
                    context, rotation['new_attachment_id'], connector,
                    mountpoint=rotation['mountpoint'])
            except Exception:
                attachment = self._get_exact_cinder_attachment(
                    context, rotation['new_attachment_id'], volume_id,
                    instance.uuid)
                if (attachment is None or
                        _attachment_status(attachment) == 'reserved'):
                    raise
            status = _attachment_status(attachment)
            connection_info = _attachment_connection_info(attachment)
        if status not in ('attaching', 'attached') or not connection_info.get(
                'driver_volume_type'):
            raise exception.InvalidVolume(
                reason='Replacement attachment cannot restore source I/O')
        connection_info = dict(connection_info)
        connection_info['data'] = dict(connection_info.get('data') or {})
        connection_info.setdefault('serial', volume_id)

        current = objects.BlockDeviceMapping.get_by_volume_and_instance(
            context, volume_id, instance.uuid)
        if current.attachment_id != rotation['new_attachment_id']:
            raise exception.InvalidVolume(
                reason='Restored source BDM lost its replacement attachment')
        current.connection_info = jsonutils.dumps(connection_info)
        current.save()
        bdm.attachment_id = current.attachment_id
        bdm.connection_info = current.connection_info
        if not rotation['boot_volume']:
            self.driver.restart_internal_volume_attach(
                context, instance, volume_id, connection_info,
                expected_mountpoint=rotation['mountpoint'])
        if status == 'attaching':
            self.volume_api.attachment_complete(
                context, rotation['new_attachment_id'])
        attachment = self._get_exact_cinder_attachment(
            context, rotation['new_attachment_id'], volume_id, instance.uuid)
        if (attachment is None or
                _attachment_status(attachment) != 'attached'):
            raise exception.InvalidVolume(
                reason='Cinder did not commit the restored source attachment')

        self.driver.mark_source_volume_generation_rollback_complete(
            instance, intent['operation_token'], migration.uuid)
        replacement_intent = intent
        if intent['attachment_id'] != rotation['new_attachment_id']:
            replacement_intent = (
                self.driver.replace_cold_source_volume_attach_intent(
                    instance, volume_id, intent,
                    rotation['new_attachment_id']))
        if not rotation['boot_volume']:
            self.driver.confirm_connected_volume_journal(
                instance, volume_id, connection_info,
                expected_mountpoint=rotation['mountpoint'])
        rotation = self.driver.transition_cold_attachment_rotation(
            instance, volume_id, rotation, 'source-rollback-complete')
        self.driver.cancel_managed_volume_attach(
            instance, volume_id, replacement_intent)
        self.driver.cancel_cold_attachment_rotation(
            instance, volume_id, rotation)
        self._maybe_finalize_failed_cold_source_generation(
            instance, intent['operation_token'])

    def _retire_terminal_cold_attachment_rotation_locked(
            self, context, instance, volume_id, rotation, bdm):
        """Retire a completed rotation with the rotation file removed last."""
        phase = rotation.get('phase')
        if phase not in incus_driver._COLD_ATTACHMENT_ROTATION_TERMINAL_PHASES:
            raise exception.InvalidVolume(
                reason='Cold attachment rotation is not terminal')
        migrations = objects.MigrationList.get_by_filters(
            context, {'instance_uuid': instance.uuid})
        exact = [
            migration for migration in migrations
            if getattr(migration, 'uuid', None) == rotation['migration_uuid']]
        if len(exact) != 1:
            raise exception.InvalidVolume(
                reason='Terminal cold rotation has no exact Nova migration')
        migration = exact[0]
        if (migration.source_compute != self.host or
                migration.dest_compute == self.host or
                rotation['operation_token'] != migration.uuid):
            raise exception.InvalidVolume(
                reason='Terminal cold rotation names another owner')

        if phase == 'source-release-complete':
            if (migration.status not in ('finished', 'confirmed', 'done') or
                    instance.host != migration.dest_compute or bdm is None or
                    getattr(bdm, 'attachment_id', None) !=
                    rotation['new_attachment_id'] or
                    getattr(bdm, 'device_name', None) !=
                    rotation['mountpoint']):
                raise exception.InvalidVolume(
                    reason='Terminal cold source release lost target owner')
            try:
                source_container = self.driver.client.instances.get(
                    instance.name)
            except incus_driver.incus_exceptions.LXDAPIException as exc:
                if not incus_driver._is_incus_not_found(exc):
                    raise
                source_container = None
            if (source_container is not None and
                    source_container.status != 'Stopped'):
                raise exception.InvalidVolume(
                    reason='Terminal cold source release still has an active '
                           'source runtime')
            if self._get_exact_cinder_attachment(
                    context, rotation['old_attachment_id'], volume_id,
                    instance.uuid) is not None:
                raise exception.InvalidVolume(
                    reason='Terminal cold source attachment still exists')
            target = self._get_exact_cinder_attachment(
                context, rotation['new_attachment_id'], volume_id,
                instance.uuid)
            if target is None or _attachment_status(target) != 'attached':
                raise exception.InvalidVolume(
                    reason='Terminal cold rotation lost target attachment')
            target_info = _attachment_connection_info(target)
            bdm_info = _optional_bdm_connection_info(bdm)
            if (not target_info.get('driver_volume_type') or
                    bdm_info is None or
                    _canonical_attachment_connection_info(
                        target_info, volume_id, instance.uuid) !=
                    _canonical_attachment_connection_info(
                        bdm_info, volume_id, instance.uuid)):
                raise exception.InvalidVolume(
                    reason='Terminal cold rotation target metadata changed')
        else:
            if (migration.status not in (
                    'cancelled', 'error', 'failed', 'reverted') or
                    instance.host != migration.source_compute or bdm is None or
                    getattr(bdm, 'device_name', None) !=
                    rotation['mountpoint']):
                raise exception.InvalidVolume(
                    reason='Terminal cold rollback lost source owner')
            generation = (
                self.driver.get_source_volume_generation_recovery_candidate(
                    instance))
            if (generation is None or
                    generation.get('operation_token') != migration.uuid or
                    generation.get('migration_uuid') != migration.uuid):
                raise exception.InvalidVolume(
                    reason='Terminal cold rollback lost its generation')
            current_attachment_id = getattr(bdm, 'attachment_id', None)
            allowed = {rotation['old_attachment_id']}
            if rotation['new_attachment_id'] is not None:
                allowed.add(rotation['new_attachment_id'])
            if current_attachment_id not in allowed:
                raise exception.InvalidVolume(
                    reason='Terminal cold rollback BDM owner changed')
            current_attachment = self._get_exact_cinder_attachment(
                context, current_attachment_id, volume_id, instance.uuid)
            if (current_attachment is None or
                    _attachment_status(current_attachment) != 'attached'):
                raise exception.InvalidVolume(
                    reason='Terminal cold rollback attachment is not attached')
            other_attachment_id = (
                rotation['new_attachment_id']
                if current_attachment_id == rotation['old_attachment_id']
                else rotation['old_attachment_id'])
            if (other_attachment_id is not None and
                    self._get_exact_cinder_attachment(
                        context, other_attachment_id, volume_id,
                        instance.uuid) is not None):
                raise exception.InvalidVolume(
                    reason='Terminal cold rollback retains two attachments')
            local_info = (
                self.driver.get_internal_volume_attach_connection_info(
                    instance, volume_id, rotation['mountpoint']))
            if rotation['boot_volume']:
                if local_info is not None:
                    raise exception.InvalidVolume(
                        reason='Terminal BFV rollback has os-brick evidence')
            else:
                cinder_info = _attachment_connection_info(current_attachment)
                if (local_info is None or
                        _canonical_attachment_connection_info(
                            local_info, volume_id, instance.uuid) !=
                        _canonical_attachment_connection_info(
                            cinder_info, volume_id, instance.uuid)):
                    raise exception.InvalidVolume(
                        reason='Terminal cold rollback local mapping changed')

        if self.driver.get_volume_journal_phase(
                instance, volume_id) is not None:
            raise exception.InvalidVolume(
                reason='Terminal cold rotation still has a volume journal')
        intent = self.driver.get_managed_volume_attach_intent(
            instance, volume_id)
        if intent is not None:
            expected_attachment_id = (
                rotation['old_attachment_id']
                if phase == 'source-release-complete'
                else getattr(bdm, 'attachment_id', None))
            expected = {
                'attachment_id': expected_attachment_id,
                'mountpoint': rotation['mountpoint'],
                'operation_kind': 'migration',
                'operation_token': migration.uuid,
                'operation_direction': 'cold-source-restore',
                'operation_migration_uuid': migration.uuid,
                'boot_volume': rotation['boot_volume'],
            }
            if any(
                    intent.get(key) != value
                    for key, value in expected.items()):
                raise exception.InvalidVolume(
                    reason='Terminal cold rotation intent owner changed')
            self.driver.cancel_managed_volume_attach(
                instance, volume_id, intent)
        self.driver.cancel_cold_attachment_rotation(
            instance, volume_id, rotation)
        if phase == 'source-rollback-complete':
            self._maybe_finalize_failed_cold_source_generation(
                instance, migration.uuid)

    def _recover_failed_cold_bfv_intent_locked(
            self, context, instance, volume_id, journal_phase, intent, bdm,
            migration):
        """Retire a BFV intent written before its rotation record."""
        if (journal_phase != 'attach-pending' or
                self.driver.get_volume_journal_phase(
                    instance, volume_id) is not None or
                not intent.get('boot_volume') or
                bdm is None or not self._cold_rotation_is_boot_volume(bdm) or
                getattr(bdm, 'attachment_id', None) !=
                intent['attachment_id'] or
                getattr(bdm, 'device_name', None) != intent['mountpoint']):
            raise exception.InvalidVolume(
                reason='Cold source BFV intent has no exact Nova owner')
        attachment = self._get_exact_cinder_attachment(
            context, intent['attachment_id'], volume_id, instance.uuid)
        if (attachment is None or
                _attachment_status(attachment) != 'attached'):
            raise exception.InvalidVolume(
                reason='Cold source BFV intent lost its Cinder owner')
        inventory = self._cold_rotation_attachment_inventory(
            context, instance, volume_id)
        if inventory != [intent['attachment_id']]:
            raise exception.InvalidVolume(
                reason='Cold source BFV intent has ambiguous Cinder owners')
        if self.driver.get_internal_volume_attach_connection_info(
                instance, volume_id, intent['mountpoint']) is not None:
            raise exception.InvalidVolume(
                reason='Cold source BFV intent has os-brick evidence')

        self.driver.mark_source_volume_generation_rollback_complete(
            instance, intent['operation_token'], migration.uuid)
        self.driver.cancel_managed_volume_attach(
            instance, volume_id, intent)
        self._maybe_finalize_failed_cold_source_generation(
            instance, intent['operation_token'])

    def _complete_failed_cold_bfv_rotation_locked(
            self, context, instance, volume_id, intent, bdm, rotation,
            migration):
        """Retire a control-plane-only BFV rollback generation."""
        self.driver.mark_source_volume_generation_rollback_complete(
            instance, intent['operation_token'], migration.uuid)
        rotation = self.driver.transition_cold_attachment_rotation(
            instance, volume_id, rotation, 'source-rollback-complete')
        self._retire_terminal_cold_attachment_rotation_locked(
            context, instance, volume_id, rotation, bdm)

    def _recover_failed_cold_attachment_rotation_locked(
            self, context, instance, volume_id, intent, bdm, rotation,
            migration):
        """Roll a proven non-committed cold target back to its source."""
        if (instance.host != self.host or
                migration.status not in ('cancelled', 'error', 'failed',
                                         'reverted')):
            raise exception.InvalidVolume(
                reason='Cold source rollback has no authoritative Nova owner')
        self.driver.fence_failed_cold_source_volume_generation(
            instance, intent['operation_token'])
        if rotation['phase'] == 'prepared':
            if rotation['boot_volume']:
                self._complete_failed_cold_bfv_rotation_locked(
                    context, instance, volume_id, intent, bdm, rotation,
                    migration)
                return True
            return False
        if rotation['phase'] == 'creating':
            LOG.critical(
                'Cannot restore cold source volume %s automatically because '
                'its replacement attachment POST result is unknown',
                volume_id, instance=instance)
            raise exception.InvalidVolume(
                reason='Cinder attachment creation result is uncertain')
        if rotation['phase'] == 'new-created':
            old_attachment = self._get_exact_cinder_attachment(
                context, rotation['old_attachment_id'], volume_id,
                instance.uuid)
            if old_attachment is not None:
                if (_attachment_status(old_attachment) != 'attached' or
                        getattr(bdm, 'attachment_id', None) !=
                        rotation['old_attachment_id']):
                    raise exception.InvalidVolume(
                        reason='Cold source rollback attachment state changed')
                new_attachment = self._get_exact_cinder_attachment(
                    context, rotation['new_attachment_id'], volume_id,
                    instance.uuid)
                if new_attachment is not None:
                    if _attachment_status(new_attachment) != 'reserved':
                        raise exception.InvalidVolume(
                            reason='Cold source replacement attachment state '
                                   'changed')
                    try:
                        self.volume_api.attachment_delete(
                            context, rotation['new_attachment_id'])
                    except Exception:
                        if self._get_exact_cinder_attachment(
                                context, rotation['new_attachment_id'],
                                volume_id, instance.uuid) is not None:
                            raise
                if self._get_exact_cinder_attachment(
                        context, rotation['new_attachment_id'], volume_id,
                        instance.uuid) is not None:
                    raise exception.InvalidVolume(
                        reason='Cold source replacement attachment remains')
                rotation = self.driver.transition_cold_attachment_rotation(
                    instance, volume_id, rotation, 'source-old-retained')
                if rotation['boot_volume']:
                    self._complete_failed_cold_bfv_rotation_locked(
                        context, instance, volume_id, intent, bdm, rotation,
                        migration)
                    return True
                return False
            self._validate_cold_rotation_new_attachment(
                context, instance, volume_id, rotation)
            rotation = self.driver.transition_cold_attachment_rotation(
                instance, volume_id, rotation, 'old-deleted')
        if rotation['phase'] == 'source-old-retained':
            if rotation['boot_volume']:
                self._complete_failed_cold_bfv_rotation_locked(
                    context, instance, volume_id, intent, bdm, rotation,
                    migration)
                return True
            return False
        self._restore_cold_rotation_new_owner_locked(
            context, instance, volume_id, intent, bdm, rotation, migration)
        return True

    def _recover_incus_internal_attach_locked(
            self, context, instance, volume_id, journal_phase, intent, bdm,
            attachment, status, connection_info):
        """Converge an exact spawn/reconcile/source attach generation."""
        self.driver.validate_internal_volume_attach_owner(instance, intent)
        operation_kind = intent['operation_kind']
        direction = intent['operation_direction']
        if operation_kind == 'migration':
            migrations = objects.MigrationList.get_by_filters(
                context, {'instance_uuid': instance.uuid})
            exact = [
                migration for migration in migrations
                if getattr(migration, 'uuid', None) ==
                intent['operation_migration_uuid']]
            if len(exact) != 1:
                raise exception.InvalidVolume(
                    reason='Internal volume owner does not match exactly one '
                           'Nova migration')
            migration = exact[0]
            expected_host = (
                migration.dest_compute if direction in (
                    'cold-target', 'live-target')
                else migration.source_compute)
            if expected_host != self.host:
                raise exception.InvalidVolume(
                    reason='Internal migration volume owner names another '
                           'compute host')
            if (direction == 'cold-revert-source' and
                    self.driver.get_cold_attachment_rotation(
                        instance, volume_id) is not None):
                rotation = self.driver.get_cold_attachment_rotation(
                    instance, volume_id)
                self._retire_handed_off_cold_rotation_locked(
                    context, instance, volume_id, intent, bdm, rotation,
                    migration)
            if direction == 'cold-source-restore':
                rotation = self.driver.get_cold_attachment_rotation(
                    instance, volume_id)
                if rotation is not None:
                    if migration.status in (
                            'cancelled', 'error', 'failed', 'reverted'):
                        handled = (
                            self.
                            _recover_failed_cold_attachment_rotation_locked(
                                context, instance, volume_id, intent, bdm,
                                rotation, migration))
                        if handled:
                            return
                        # The replacement was safely removed while the exact
                        # old owner remained. Continue through ordinary source
                        # journal replay below.
                        attachment = self._get_exact_cinder_attachment(
                            context, intent['attachment_id'], volume_id,
                            instance.uuid)
                        status = _attachment_status(attachment)
                        connection_info = _attachment_connection_info(
                            attachment)
                        if connection_info:
                            connection_info = dict(connection_info)
                            connection_info['data'] = dict(
                                connection_info.get('data') or {})
                            connection_info.setdefault('serial', volume_id)
                    else:
                        rotation = (
                            self._advance_cold_attachment_rotation_locked(
                                context, instance, bdm, rotation))
                        if migration.status not in (
                                'finished', 'confirmed', 'done'):
                            return
                elif (intent.get('boot_volume') and
                      migration.status in (
                          'cancelled', 'error', 'failed', 'reverted')):
                    return self._recover_failed_cold_bfv_intent_locked(
                        context, instance, volume_id, journal_phase, intent,
                        bdm, migration)
                elif (intent.get('boot_volume') and
                      migration.status in ('finished', 'confirmed', 'done')):
                    raise exception.InvalidVolume(
                        reason='Completed cold migration BFV has no durable '
                               'attachment rotation')
            if direction in ('cold-target', 'live-target'):
                return self._recover_incus_migration_target_attach_locked(
                    context, instance, volume_id, journal_phase, intent, bdm,
                    attachment, status, connection_info, migration)
            if (direction == 'live-source-release' or
                    (direction == 'cold-source-restore' and
                     migration.status in ('finished', 'confirmed', 'done'))):
                return self._recover_incus_migration_source_release_locked(
                    context, instance, volume_id, journal_phase, intent, bdm,
                    attachment, migration)
            elif instance.host != self.host:
                raise exception.InvalidVolume(
                    reason='Source volume restore is not owned by this host')
            if (direction == 'cold-source-restore' and
                    migration.status not in (
                        'cancelled', 'error', 'failed', 'reverted')):
                # A short-lived source-restore intent surrounds the initial
                # os-brick detach. While Nova still owns an active migration,
                # leave it untouched; after the failed RPC is authoritative,
                # the exact intent makes disconnecting/disconnected replayable.
                return
            if (direction == 'cold-revert-source' and
                    migration.status not in ('reverting', 'reverted')):
                return

        if (bdm is None or
                getattr(bdm, 'attachment_id', None) !=
                intent['attachment_id'] or
                getattr(bdm, 'device_name', None) != intent['mountpoint']):
            raise exception.InvalidVolume(
                reason='Internal Cinder attach has no exact Nova BDM')

        if status not in ('attaching', 'attached') or attachment is None:
            raise exception.InvalidVolume(
                reason='Internal volume attach is not formally owned by '
                       'Cinder')
        bdm_connection_info = _optional_bdm_connection_info(bdm)
        if not connection_info.get('driver_volume_type'):
            raise exception.InvalidVolume(
                reason='Internal Cinder attachment has no connection '
                       'information')
        if (bdm_connection_info is not None and
                _canonical_attachment_connection_info(
                    bdm_connection_info, volume_id, instance.uuid) !=
                _canonical_attachment_connection_info(
                    connection_info, volume_id, instance.uuid)):
            raise exception.InvalidVolume(
                reason='Internal Cinder attachment and Nova BDM connection '
                       'information disagree')

        if (operation_kind == 'spawn' and
                (self._is_failed_build_cleanup(instance) or
                 getattr(instance, 'vm_state', None) == vm_states.ERROR)):
            # A failed build owns rollback, not materialization.  This branch
            # intentionally precedes phase-specific attach recovery: a crash
            # can leave either the connecting or connected generation after
            # the container has already disappeared.  The exact profile,
            # generation, BDM and Cinder attachment authorize removal of only
            # this host mapping; Nova's failed-build transaction retains
            # responsibility for releasing the formal attachment and BDM.
            self.driver.rollback_internal_volume_attach(
                context, instance, volume_id, connection_info,
                expected_mountpoint=intent['mountpoint'])
            self.driver.finalize_rolled_back_volume_journal(
                instance, volume_id)
            self.driver.cancel_managed_volume_attach(
                instance, volume_id, intent)
            self.driver.finalize_spawn_volume_generation(
                instance, intent['operation_token'])
            return

        restarted = False
        if journal_phase in ('attach-disconnecting', 'attach-disconnected',
                             'rolled-back'):
            self.driver.restart_internal_volume_attach(
                context, instance, volume_id, connection_info,
                expected_mountpoint=intent['mountpoint'])
            restarted = True
        elif journal_phase in ('connecting', 'attach-pending'):
            self.driver.resume_internal_volume_attach(
                context, instance, volume_id, connection_info,
                expected_mountpoint=intent['mountpoint'])
        elif journal_phase != 'connected':
            raise exception.InvalidVolume(
                reason='Internal Cinder attach journal phase is invalid')
        elif not restarted:
            self.driver.confirm_connected_volume_journal(
                instance, volume_id, connection_info,
                expected_mountpoint=intent['mountpoint'])

        if bdm_connection_info is None or status == 'attaching':
            bdm.connection_info = jsonutils.dumps(connection_info)
            bdm.save()
        if status == 'attaching':
            self.volume_api.attachment_complete(
                context, intent['attachment_id'])
            attachment = self._get_exact_cinder_attachment(
                context, intent['attachment_id'], volume_id, instance.uuid)
            if (attachment is None or
                    _attachment_status(attachment) != 'attached'):
                raise exception.InvalidVolume(
                    reason='Cinder did not commit the internal attachment')

        volume = self.volume_api.get(context, volume_id)
        unused_status, completed_refs = _validated_volume_ownership(
            volume, volume_id)
        foreign = [
            ref for ref in completed_refs
            if (ref['instance_uuid'] != instance.uuid or
                ref['mountpoint'] != intent['mountpoint'])]
        if foreign:
            raise exception.InvalidVolume(
                reason='Internal Cinder attach has another completed owner')
        if operation_kind != 'migration':
            exact_refs = [
                ref for ref in completed_refs
                if ref['id'] == intent['attachment_id']]
            if len(exact_refs) != 1 or len(completed_refs) != 1:
                raise exception.InvalidVolume(
                    reason='Internal Cinder attach has ambiguous completed '
                           'ownership')

        if direction == 'cold-source-restore':
            # Publish the exact source/Migration owner before unlinking the
            # last filesystem intent.  A crash or fsync failure after this
            # point is discoverable from the Incus profile even when the
            # volume recovery directory has already become empty.
            self.driver.confirm_connected_volume_journal(
                instance, volume_id, connection_info,
                expected_mountpoint=intent['mountpoint'])
            self.driver.mark_source_volume_generation_rollback_complete(
                instance, intent['operation_token'], migration.uuid)
            rotation = self.driver.get_cold_attachment_rotation(
                instance, volume_id)
            if rotation is not None:
                rotation = self.driver.transition_cold_attachment_rotation(
                    instance, volume_id, rotation,
                    'source-rollback-complete')

        self.driver.cancel_managed_volume_attach(
            instance, volume_id, intent)
        if operation_kind == 'spawn':
            self.driver.finalize_spawn_volume_generation(
                instance, intent['operation_token'])
        elif direction == 'cold-source-restore':
            if rotation is not None:
                self.driver.cancel_cold_attachment_rotation(
                    instance, volume_id, rotation)
            self._maybe_finalize_failed_cold_source_generation(
                instance, intent['operation_token'])
        elif direction == 'cold-revert-source':
            self.driver.finalize_remote_source_volume_generation(
                instance, intent['operation_token'])

    def _recover_incus_migration_source_release_locked(
            self, context, instance, volume_id, journal_phase, intent, bdm,
            source_attachment, migration):
        """Retire only a proven obsolete source-host volume mapping."""
        direction = intent['operation_direction']
        live_source_post_commit = (
            direction == 'live-source-release' and
            getattr(migration, 'migration_type', None) ==
            'live-migration' and migration.status == 'running' and
            instance.host == migration.source_compute)
        if (migration.source_compute != self.host or
                migration.dest_compute == self.host or
                (instance.host != migration.dest_compute and
                 not live_source_post_commit)):
            raise exception.InvalidVolume(
                reason='Migration source release has no authoritative target')
        if (direction == 'live-source-release' and
                getattr(migration, 'migration_type', None) !=
                'live-migration'):
            raise exception.InvalidVolume(
                reason='Live source release names a non-live migration')
        if (direction == 'cold-source-restore' and
                migration.status not in ('finished', 'confirmed', 'done')):
            raise exception.InvalidVolume(
                reason='Cold source release is not durably target-owned')
        rotation = self.driver.get_cold_attachment_rotation(
            instance, volume_id)
        if direction == 'cold-source-restore':
            if (rotation is None or rotation.get('phase') != 'bdm-rotated' or
                    rotation.get('old_attachment_id') !=
                    intent['attachment_id'] or
                    rotation.get('new_attachment_id') !=
                    getattr(bdm, 'attachment_id', None) or
                    rotation.get('operation_token') !=
                    intent['operation_token']):
                raise exception.InvalidVolume(
                    reason='Cold source release has no exact attachment '
                           'rotation proof')

        try:
            source_container = self.driver.client.instances.get(instance.name)
        except incus_driver.incus_exceptions.LXDAPIException as exc:
            if not incus_driver._is_incus_not_found(exc):
                raise
            source_container = None
        if direction == 'live-source-release' and source_container is not None:
            raise exception.InvalidVolume(
                reason='Refusing live source volume release while its Incus '
                       'instance record still exists')
        if (direction == 'cold-source-restore' and
                source_container is not None and
                source_container.status != 'Stopped'):
            raise exception.InvalidVolume(
                reason='Refusing cold source volume release while its source '
                       'instance is running')

        if (bdm is None or
                getattr(bdm, 'attachment_id', None) in (
                    None, intent['attachment_id']) or
                getattr(bdm, 'device_name', None) != intent['mountpoint']):
            raise exception.InvalidVolume(
                reason='Migration source release has no exact target BDM')
        target_attachment = self._get_exact_cinder_attachment(
            context, bdm.attachment_id, volume_id, instance.uuid)
        if (target_attachment is None or
                _attachment_status(target_attachment) != 'attached'):
            raise exception.InvalidVolume(
                reason='Migration source release has no attached target')
        target_info = _attachment_connection_info(target_attachment)
        bdm_info = _optional_bdm_connection_info(bdm)
        if not target_info.get('driver_volume_type') or bdm_info is None:
            raise exception.InvalidVolume(
                reason='Migration target BDM and Cinder attachment disagree')
        target_canonical = _canonical_attachment_connection_info(
            target_info, volume_id, instance.uuid)
        bdm_canonical = _canonical_attachment_connection_info(
            bdm_info, volume_id, instance.uuid)
        # BFV root I/O is transferred by Incus' fenced Ceph handover, not
        # os-brick. Nova may retain the source host connector in the BDM even
        # after switching attachment_id, so require exact identity but do not
        # compare host-specific transport fields. Data volumes still require
        # byte-for-byte canonical connector agreement before source release.
        if (not intent.get('boot_volume') and
                target_canonical != bdm_canonical):
            raise exception.InvalidVolume(
                reason='Migration target BDM and Cinder attachment disagree')
        volume = self.volume_api.get(context, volume_id)
        unused_status, completed_refs = _validated_volume_ownership(
            volume, volume_id)
        if any(
                ref['instance_uuid'] != instance.uuid or
                ref['mountpoint'] != intent['mountpoint']
                for ref in completed_refs):
            raise exception.InvalidVolume(
                reason='Migration source release has ambiguous target owner')

        local_info = self.driver.get_internal_volume_attach_connection_info(
            instance, volume_id, intent['mountpoint'])
        source_info = _attachment_connection_info(source_attachment)
        if (source_attachment is not None and
                _attachment_status(source_attachment) not in (
                    'attached', 'attaching', 'error_detaching', 'detached',
                    'deleted')):
            raise exception.InvalidVolume(
                reason='Migration source attachment is not releaseable')
        if (local_info is not None and source_info and
                _canonical_attachment_connection_info(
                    local_info, volume_id, instance.uuid) !=
                _canonical_attachment_connection_info(
                    source_info, volume_id, instance.uuid)):
            raise exception.InvalidVolume(
                reason='Migration source Cinder attachment and local mapping '
                       'disagree')

        local_evidence = (
            local_info is not None or
            self.driver.get_volume_journal_phase(instance, volume_id) is not
            None)
        boot_volume = intent.get('boot_volume', False)
        if boot_volume and local_evidence:
            raise exception.InvalidVolume(
                reason='BFV source release unexpectedly has local os-brick '
                       'evidence')
        if (not boot_volume and source_attachment is not None and
                not local_evidence):
            raise exception.InvalidVolume(
                reason='Data-volume source attachment remains without a '
                       'terminal local release proof')
        if local_evidence:
            if journal_phase != 'attach-disconnected':
                self.driver._recover_source_release_volume_journal_locked(
                    context, instance, volume_id, intent['mountpoint'])
            if self.driver.get_volume_journal_phase(
                    instance, volume_id) != 'disconnected':
                raise exception.InvalidVolume(
                    reason='Migration source host disconnect did not reach '
                           'its durable terminal phase')

        if source_attachment is not None:
            try:
                self.volume_api.attachment_delete(
                    context, intent['attachment_id'])
            except exception.VolumeAttachmentNotFound:
                pass
        if self._get_exact_cinder_attachment(
                context, intent['attachment_id'], volume_id,
                instance.uuid) is not None:
            raise exception.InvalidVolume(
                reason='Migration source attachment remains after release')

        if local_evidence:
            self.driver.finalize_disconnected_volume_journal(
                instance, volume_id)
        if rotation is not None:
            rotation = self.driver.transition_cold_attachment_rotation(
                instance, volume_id, rotation, 'source-release-complete')
        self.driver.cancel_managed_volume_attach(
            instance, volume_id, intent)
        if rotation is not None:
            self.driver.cancel_cold_attachment_rotation(
                instance, volume_id, rotation)

    def _recover_incus_connecting_volume_journal_locked(
            self, context, instance, volume_id, journal_phase=None):
        """Converge one journaled attach using Nova and Cinder authority.

        ``attached`` is the only Cinder state that protects guest access from
        rollback.  ``attaching`` resumes the original request and completes
        it in Nova's normal order.  Explicit non-attached states roll back.
        Missing, duplicate or contradictory ownership evidence fails closed.
        """
        rotation = self.driver.get_cold_attachment_rotation(
            instance, volume_id)
        rotation_phase = (
            rotation.get('phase') if rotation is not None else None)
        if (rotation_phase in
                incus_driver._COLD_ATTACHMENT_ROTATION_TERMINAL_PHASES):
            bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(
                context, instance.uuid)
            matching_bdms = [
                bdm for bdm in bdms
                if (bdm.volume_id == volume_id and
                    not getattr(bdm, 'deleted', False))]
            if len(matching_bdms) != 1:
                raise exception.InvalidVolume(
                    reason='Terminal cold rotation has no unique Nova BDM')
            return self._retire_terminal_cold_attachment_rotation_locked(
                context, instance, volume_id, rotation, matching_bdms[0])
        if journal_phase and journal_phase.startswith('rotation-'):
            raw_phase = self.driver.get_volume_journal_phase(
                instance, volume_id)
            if raw_phase in ('disconnecting', 'disconnected'):
                journal_phase = 'attach-{}'.format(raw_phase)
            elif raw_phase is None:
                journal_phase = 'attach-pending'
            else:
                journal_phase = raw_phase
        if journal_phase not in (
                None, 'attach-pending', 'connecting', 'connected',
                'rolled-back', 'attach-disconnecting',
                'attach-disconnected'):
            raise exception.InvalidVolume(
                reason='Cinder attach recovery received an invalid journal '
                       'phase')
        intent = self.driver.get_managed_volume_attach_intent(
            instance, volume_id)
        if intent is None:
            raise exception.InvalidVolume(
                reason='Cinder attach journal has no exact managed attach '
                       'intent; refusing legacy volume-ID recovery')
        bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(
            context, instance.uuid)
        matching_bdms = [
            bdm for bdm in bdms
            if (bdm.volume_id == volume_id and
                not getattr(bdm, 'deleted', False))
        ]
        if len(matching_bdms) > 1:
            raise exception.InvalidVolume(
                reason='Nova has duplicate block device mappings for Cinder '
                       'volume %s' % volume_id)
        bdm = matching_bdms[0] if matching_bdms else None
        attachment_id = intent['attachment_id']
        bdm_attachment_id = (
            getattr(bdm, 'attachment_id', None) if bdm is not None else None)
        bdm_device_name = (
            getattr(bdm, 'device_name', None) if bdm is not None else None)
        internal_attach = intent.get('operation_kind') != 'hot-attach'
        if not internal_attach and bdm is not None and (
                bdm_attachment_id != attachment_id or
                bdm_device_name != intent['mountpoint']):
            raise exception.InvalidVolume(
                reason='Nova BDM no longer matches managed attach intent')
        attachment = self._get_exact_cinder_attachment(
            context, attachment_id, volume_id, instance.uuid)

        status = _attachment_status(attachment)
        connection_info = _attachment_connection_info(attachment)
        if connection_info:
            connection_info = dict(connection_info)
            connection_info['data'] = dict(connection_info.get('data') or {})
            connection_info.setdefault('serial', volume_id)

        if internal_attach:
            return self._recover_incus_internal_attach_locked(
                context, instance, volume_id, journal_phase, intent, bdm,
                attachment, status, connection_info)

        if journal_phase == 'attach-pending':
            return self._recover_incus_attach_pending_locked(
                context, instance, volume_id, intent, bdm, attachment,
                status, connection_info)
        if status in ('attached', 'attaching'):
            return self._recover_incus_formal_attach_locked(
                context, instance, volume_id, intent, bdm, status,
                connection_info)
        return self._rollback_incus_managed_attach_locked(
            context, instance, volume_id, journal_phase, intent, bdm,
            attachment, status, connection_info)

    @periodic_task.periodic_task(
        spacing=CONF.incus.migration_recovery_interval)
    def _release_abandoned_incus_migration_reservations(self, context):
        """Release target reservations whose migration never started.

        A live migration pre-check reserves a target name and idmap on the
        destination before anything else exists there. Incus cannot expire
        that reservation on its own: the create request it fences carries no
        deadline and can legitimately arrive after a target restart, so an
        abandoned one would otherwise wedge its idmap range forever. Only
        Nova knows whether a migration can still consume it.

        Two independent facts must hold before a reservation is released.
        Nova must have no in-progress migration and no local instance that
        could still create that target name, and the reservation must have
        been in exactly the same unstarted state in the previous pass. The
        second rule keeps a pre-check that is merely slower than one
        recovery interval from ever being mistaken for an abandoned one.
        """
        if not CONF.incus.migration_auto_recovery:
            return

        context = (context or nova.context.get_admin_context()).elevated()
        try:
            candidates = (
                self.driver.list_unstarted_migration_attempt_reservations())
        except Exception:
            LOG.exception(
                'Failed to list unstarted Incus migration reservations')
            return

        previous = getattr(self, '_incus_unstarted_reservations', {})
        current = {
            candidate['token']: candidate for candidate in candidates}
        self._incus_unstarted_reservations = current
        if not candidates:
            return

        try:
            claimed = self._incus_claimable_target_names(context)
        except Exception:
            LOG.exception(
                'Failed to determine which Incus target names Nova can '
                'still create; retaining every unstarted reservation')
            return

        for token, candidate in sorted(current.items()):
            if candidate['name'] in claimed:
                continue
            if previous.get(token) != candidate:
                LOG.debug(
                    'Deferring release of unstarted Incus migration '
                    'reservation %(token)s for %(name)s until it is seen '
                    'unchanged twice', candidate)
                continue
            try:
                self.driver.release_unstarted_migration_attempt_reservation(
                    candidate)
            except Exception:
                LOG.exception(
                    'Failed to release the abandoned Incus migration '
                    'reservation %(token)s for %(name)s', candidate)
                continue

            self._incus_unstarted_reservations.pop(token, None)
            LOG.info(
                'Released the abandoned Incus migration reservation '
                '%(token)s for %(name)s (idmap %(idmap_base)s+'
                '%(idmap_size)s)', candidate)

    def _incus_claimable_target_names(self, context):
        """Return the target names a Nova migration could still create here.

        An in-progress migration is authoritative regardless of which
        instance list it appears in, because a live migration target is not
        yet owned by this host. Local instances are included as well so that
        a completed migration whose token was never retired is not released
        while its target exists.
        """
        names = set()
        for instance in objects.InstanceList.get_by_host(
                context, self.host, expected_attrs=[]):
            names.add(instance.name)

        # Incus computes are never clustered, so this host has exactly one
        # node and a per-node migration query covers all of them.
        nodename = self.driver.get_available_nodes()[0]
        migrations = objects.MigrationList.get_in_progress_by_host_and_node(
            context, self.host, nodename)
        instance_uuids = {
            migration.instance_uuid for migration in migrations
            if migration.instance_uuid}
        for instance_uuid in instance_uuids:
            try:
                instance = objects.Instance.get_by_uuid(
                    context, instance_uuid, expected_attrs=[])
            except exception.InstanceNotFound:
                continue
            names.add(instance.name)

        return names

    @periodic_task.periodic_task(
        spacing=CONF.incus.migration_recovery_interval)
    def _recover_incus_bfv_migration_targets(self, context):
        if not CONF.incus.migration_auto_recovery:
            return

        context = (context or nova.context.get_admin_context()).elevated()
        candidates = self.driver.list_migration_recovery_candidates()
        if not candidates:
            self._incus_recovery_cursor = 0
            return

        start = (
            getattr(self, '_incus_recovery_cursor', 0) % len(candidates))
        ordered = candidates[start:] + candidates[:start]
        candidates = ordered[:CONF.incus.migration_recovery_batch_size]
        self._incus_recovery_cursor = (
            start + len(candidates)) % len(ordered)
        LOG.debug(
            'Processing %(count)d marked Incus BFV recovery candidates',
            {'count': len(candidates)})
        for candidate in candidates:
            try:
                instance = objects.Instance.get_by_uuid(
                    context, candidate['uuid'],
                    expected_attrs=['flavor', 'info_cache'])
            except exception.InstanceNotFound:
                LOG.warning(
                    'Ignoring marked Incus BFV recovery target %(name)s '
                    'because Nova instance %(uuid)s no longer exists',
                    candidate)
                continue

            if (
                instance.host != self.host or
                instance.name != candidate['name']
            ):
                LOG.error(
                    'Ignoring marked Incus BFV recovery target %(name)s: '
                    'Nova instance %(uuid)s belongs to host %(host)s as '
                    '%(instance_name)s',
                    {
                        **candidate,
                        'host': instance.host,
                        'instance_name': instance.name,
                    })
                continue
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
