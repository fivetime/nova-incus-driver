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


def _idmap_release_lock_name(instance_uuid):
    return incus_driver._idmap_host_claim_lock_name(instance_uuid)


def _idmap_release_lock_path():
    # Nova creates state_path before the compute service starts. Keeping the
    # lock file there avoids introducing another host-local durable store;
    # etcd CAS remains the safety authority.
    return incus_driver._idmap_host_claim_lock_path()


def _share_recovery_lock_name(instance_uuid):
    return 'incus-share-recovery-{}'.format(instance_uuid)


class IncusComputeManager(manager.ComputeManager):
    """Nova manager extension for fenced BFV post-claim recovery."""

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

    def _local_idmap_resources_absent(self, intent, inventory=None):
        """Prove this host no longer retains resources for an intent."""
        return self._local_idmap_resources_absent_by_name(
            intent.instance_uuid, intent.instance_name,
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
            settled = self._settle_idmap_host_claim(
                current, claim, final_delete=claim.state != 'possible')
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
        start = (
            getattr(self, '_incus_idmap_host_claim_cursor', 0) % len(claims))
        ordered = claims[start:] + claims[:start]
        batch = ordered[:_IDMAP_RELEASE_REPLAY_BATCH]
        self._incus_idmap_host_claim_cursor = (
            start + len(batch)) % len(claims)
        context = (
            context or nova.context.get_admin_context()
        ).elevated(read_deleted='yes')
        inventory = self._all_project_idmap_inventory()
        for claim in batch:
            self._reconcile_incus_idmap_host_claim(
                context, allocator, claim, host_id, inventory=inventory)

    @periodic_task.periodic_task(
        spacing=CONF.incus.idmap_allocator_audit_interval,
        run_immediately=False)
    def _audit_incus_idmap_allocator(self, context):
        """Probe the registry every cycle, scan it completely rarely.

        Reading and parsing the whole namespace on every compute every
        minute is pure steady-state overhead that grows with the fleet and
        with the instance count. The full scan stays the integrity
        authority; what changes is how often an *idle* registry pays for
        it. Every registry mutation already audits inline, and a probe that
        fails escalates to a scan immediately, so the interval below only
        bounds how long a corruption invisible to counts can go unnoticed
        while nothing is happening.
        """
        allocator = getattr(self.driver, 'idmap_allocator', None)
        if allocator is None:
            return

        if self._incus_full_idmap_audit_due():
            self._run_incus_full_idmap_audit(allocator)
            return

        try:
            counts = allocator.probe()
        except incus_driver.incus_idmap.IDMapIntegrityError:
            LOG.error(
                'Incus idmap registry drift probe failed its cardinality '
                'invariants; escalating to a complete audit', exc_info=True)
            self._run_incus_full_idmap_audit(allocator)
            return
        except incus_driver.incus_idmap.IDMapBackendError:
            LOG.warning(
                'Incus idmap registry drift probe is temporarily '
                'unavailable', exc_info=True)
            return
        except Exception:
            LOG.exception(
                'Unexpected Incus idmap registry drift probe failure; '
                'escalating to a complete audit')
            self._run_incus_full_idmap_audit(allocator)
            return
        LOG.debug('Incus idmap registry drift probe verified %s', counts)

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

    def _run_incus_full_idmap_audit(self, allocator):
        """Run the authoritative scan and reschedule the next one."""
        interval = CONF.incus.idmap_allocator_full_audit_interval
        self._incus_full_audit_deadline = time.monotonic() + interval
        try:
            assignments = allocator.audit()
        except incus_driver.incus_idmap.IDMapIntegrityError:
            # The allocator permanently latches allocation, claim and start
            # checks closed for this process. Repairing etcd is not enough:
            # nova-compute must be restarted after an operator audit.
            LOG.critical(
                'Incus idmap registry integrity audit failed; this compute '
                'is permanently fail-closed until the registry is repaired '
                'and nova-compute is restarted', exc_info=True)
            return
        except incus_driver.incus_idmap.IDMapBackendError:
            # A transport outage is not evidence of corruption and must not
            # set the permanent integrity latch. Retry on the next cycle
            # rather than waiting out the full interval.
            self._incus_full_audit_deadline = time.monotonic()
            LOG.warning(
                'Incus idmap registry audit is temporarily unavailable',
                exc_info=True)
            return
        except Exception:
            self._incus_full_audit_deadline = time.monotonic()
            LOG.exception('Unexpected Incus idmap registry audit failure')
            return
        LOG.debug(
            'Incus idmap registry integrity audit verified %d allocation(s)',
            len(assignments))

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
            if (local_claim is None or
                    local_claim.state == 'possible'):
                raise incus_driver.incus_idmap.IDMapConflict(
                    reason='Final Nova deletion cannot prove whether the '
                           'local Incus rootfs materialized')

        # Nova's delete drives driver.destroy, whose rootfs release receipt
        # path takes the same per-instance claim lock. Holding the release
        # lock across it self-deadlocks the final delete; the shared intent
        # created above stays authoritative without it.
        result = super()._delete_instance(context, instance, bdms)
        with lockutils.lock(
                _idmap_release_lock_name(instance.uuid), external=True,
                lock_path=_idmap_release_lock_path()):
            try:
                if not self._local_idmap_resources_absent(intent):
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
                if not self._local_idmap_resources_absent(
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
        """Replay fleet-wide final-delete intents from the shared allocator."""
        allocator = getattr(self.driver, 'idmap_allocator', None)
        if allocator is None:
            return
        try:
            host_id = self._local_node_uuid()
            intents = allocator.list_release_intent_candidates()
        except Exception:
            LOG.exception('Failed to list shared Incus idmap release intents')
            return

        if not intents:
            self._incus_idmap_release_cursor = 0
            return
        start = (
            getattr(self, '_incus_idmap_release_cursor', 0) % len(intents))
        ordered = intents[start:] + intents[:start]
        batch = ordered[:_IDMAP_RELEASE_REPLAY_BATCH]
        self._incus_idmap_release_cursor = (
            start + len(batch)) % len(intents)
        context = (
            context or nova.context.get_admin_context()
        ).elevated(read_deleted='yes')
        inventory = self._all_project_idmap_inventory()
        for intent in batch:
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
            pre_live_migration=pre_live_migration)

    def _complete_live_migration_rollback(
            self, context, instance, migrate_data,
            pre_live_migration=False):
        """Prove target cleanup before Nova reports rollback complete."""
        if (
            not pre_live_migration and
            isinstance(migrate_data, incus_migrate_data.IncusLiveMigrateData)
        ):
            self.driver.finalize_live_migration_rollback(
                context, instance, migrate_data)

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
        dest_check_data.cleanup_token = migration_uuid
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
            return base_pre_live_migration(
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
            return super()._finish_resize_helper(
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

    def _finish_revert_resize(
            self, context, instance, migration, request_spec=None):
        """Validate and reuse retained source mounts before source restart."""
        share_info = [
            mapping
            for mapping in self._get_share_info(context, instance)
            if mapping.status == obj_fields.ShareMappingStatus.ACTIVE
        ]
        self._mount_all_shares(context, instance, share_info)
        return super()._finish_revert_resize(
            context, instance, migration, request_spec=request_spec)

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
                        candidate['operation_token'] and
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
        here, be free of an in-flight task, and no longer carry that volume
        as an attached block device mapping. Anything ambiguous keeps the
        journal and reports it, because acting on a volume whose ownership
        moved would disconnect storage another host is using.
        """
        if not CONF.incus.migration_auto_recovery:
            return

        context = context or nova.context.get_admin_context()
        try:
            candidates = (
                self.driver.list_volume_journal_recovery_candidates())
        except Exception:
            LOG.exception('Failed to list Incus Cinder journal recovery work')
            return

        for candidate in candidates:
            instance_uuid = candidate['uuid']
            with lockutils.lock(
                    _share_recovery_lock_name(instance_uuid),
                    external=True, lock_path=CONF.state_path):
                try:
                    instance = objects.Instance.get_by_uuid(
                        context, instance_uuid)
                except exception.InstanceNotFound:
                    LOG.error(
                        'Cinder journal for %s references a deleted Nova '
                        'instance; retaining it', instance_uuid)
                    continue
                except Exception:
                    LOG.exception(
                        'Failed to verify Nova ownership for a Cinder '
                        'journal; retaining it')
                    continue
                if instance.host != self.host or instance.task_state:
                    LOG.debug(
                        'Retaining Cinder journal while Nova ownership is '
                        'unresolved', instance=instance)
                    continue
                try:
                    self.driver.recover_volume_journal_candidate(
                        context, instance, candidate)
                except Exception:
                    LOG.exception(
                        'Automatic Incus Cinder journal recovery failed',
                        instance=instance)

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
