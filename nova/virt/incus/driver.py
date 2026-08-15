# Copyright 2015 Canonical Ltd
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from __future__ import absolute_import

import base64
import copy
import contextlib
from contextlib import closing
import dataclasses
from dataclasses import dataclass
import errno
import functools
import glob
import gzip
import hashlib
import io
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import tarfile
import tempfile
import threading
import time
import types
from urllib import parse
import uuid
import zlib

import eventlet
import nova.conf
import os_resource_classes as orc

from nova import exception
from nova import i18n
from nova.console import type as console_type
from nova.image import glance
from nova.network import neutron
from nova.network import model as network_model
from nova import objects
from nova.objects import migrate_data as nova_migrate_data
from nova.privsep import path as privsep_path
from nova.virt import driver
from nova.volume import cinder
from os_brick import exception as brick_exception
from os_brick.initiator import connector
from oslo_concurrency import processutils
from oslo_log import log as logging
from oslo_utils import fileutils
from oslo_utils import timeutils
from oslo_utils import versionutils
from oslo_utils import uuidutils
from prometheus_client import parser as prometheus_parser
from pylxd import exceptions as incus_exceptions
import yaml

from nova.virt.incus import vif as incus_vif
from nova.virt.incus import client as incus_client
from nova.virt.incus import common
from nova.virt.incus import console as incus_console
from nova.virt.incus import flavor
from nova.virt.incus import idmap as incus_idmap
from nova.virt.incus import migrate_data as incus_migrate_data
from nova.virt.incus import privsep as incus_privsep
from nova.virt.incus import storage_protocol as incus_storage_protocol

from nova.api.metadata import base as instance_metadata
from nova.objects import fields as obj_fields
from nova.virt import configdrive
from nova.compute import power_state
from nova.compute import vm_states
from nova.compute import utils as compute_utils
from nova.virt import hardware
from nova.virt import node as virt_node
from oslo_utils import units
from oslo_serialization import jsonutils
from nova import utils
import psutil
from oslo_concurrency import lockutils
from nova.compute import task_states
from oslo_utils import excutils

_ = i18n._

CONF = nova.conf.CONF
LOG = logging.getLogger(__name__)
IMAGE_API = glance.API()

MAX_CONSOLE_BYTES = 100 * units.Ki
# Config-drive ISOs are mounted under instances_path, because that is what
# the privileged mount and umount entrypoints will accept - but in a
# directory of their own, never inside an instance directory. Instance
# removal chowns, walks and rmtree's that directory, and every one of those
# steps fails on a live read-only mount, so a mount left behind there would
# make the instance undeletable rather than merely untidy.
_CONFIGDRIVE_MOUNT_DIR = 'incus-configdrive-mnt'
_CONFIGDRIVE_UMOUNT_ATTEMPTS = 3
_CONFIGDRIVE_UMOUNT_RETRY_SECONDS = 2
# Cached-image deletions are serial and each waits on the server, so a
# pass is bounded to keep the periodic task's greenthread available. What
# it defers is taken by the next pass, not dropped.
_IMAGE_CACHE_DELETE_BATCH = 25
NOVA_CONF = nova.conf.CONF

ACCEPTABLE_IMAGE_FORMATS = {'raw', 'root-tar', 'squashfs'}
INCUS_DATA_VOLUME_IMAGE_PROPERTY = 'hw_incus_data_volume_fuse'
INCUS_SYSTEM_CONTAINER_TRAIT = 'CUSTOM_INCUS_SYSTEM_CONTAINER'
INCUS_SWAP_TRAIT = 'CUSTOM_INCUS_SWAP'
INCUS_MANILA_SHARE_TRAIT = 'CUSTOM_INCUS_MANILA_SHARE'
INCUS_MANILA_LIVE_MIGRATION_TRAIT = (
    'CUSTOM_INCUS_MANILA_LIVE_MIGRATION')
INCUS_MANILA_COLD_MIGRATION_TRAIT = (
    'CUSTOM_INCUS_MANILA_COLD_MIGRATION')
INCUS_STATEFUL_MIGRATION_EXTENSION = 'migration_stateful_shifted_root'
INCUS_LIVE_BFV_MIGRATION_EXTENSION = (
    'migration_live_shared_cephext_storage')
INCUS_LIVE_CEPH_MIGRATION_EXTENSION = (
    'migration_live_shared_ceph_storage')
INCUS_STORAGE_HANDOVER_EXTENSION = 'instance_storage_handover'
INCUS_STORAGE_HANDOVER_DETACHED_EXTENSION = (
    'instance_storage_handover_detached')
INCUS_STORAGE_HANDOVER_PROOF_EXTENSION = (
    'instance_storage_handover_proof')
INCUS_STORAGE_READY_FENCE_EXTENSION = (
    'migration_shared_ceph_storage_ready_fence')
INCUS_MIGRATION_ATTEMPT_EXTENSION = 'migration_attempt_fencing'
INCUS_MIGRATION_ATTEMPT_LIST_EXTENSION = (
    'migration_attempt_reservation_generation')
INCUS_STORAGE_MATERIALIZATION_ATTEMPT_EXTENSION = (
    incus_storage_protocol.STORAGE_MATERIALIZATION_ATTEMPT_EXTENSION)
INCUS_STORAGE_RELEASE_RECEIPT_EXTENSION = (
    incus_storage_protocol.STORAGE_RELEASE_RECEIPT_EXTENSION)
MIGRATION_RECOVERY_KEY = 'user.openstack.recovery_required'
MIGRATION_DESTINATION_KEY = 'user.openstack.migration_destination_address'
MIGRATION_RECEIVE_COMPLETE_KEY = (
    'volatile.migration.storage_receive_complete')
MIGRATION_CLEANUP_TOKEN_KEY = 'user.openstack.migration_cleanup_token'
MIGRATION_CLEANUP_COMPLETE_KEY = (
    'user.openstack.migration_cleanup_complete')
MIGRATION_TARGET_VOLUMES_COMPLETE_KEY = (
    'user.openstack.migration_target_volumes_complete')
MIGRATION_DESTINATION_PREPARED_KEY = (
    'user.openstack.migration_destination_prepared')
MIGRATION_OPERATION_KEY = 'user.openstack.migration_operation_id'
MIGRATION_TARGET_OPERATION_KEY = (
    'user.openstack.migration_target_operation_id')
MIGRATION_ROLLBACK_COMPLETE_KEY = (
    'user.openstack.migration_rollback_complete')
MIGRATION_NOVA_UUID_KEY = 'user.openstack.migration_uuid'
MIGRATION_IDMAP_RETIREMENT_KEY = (
    'user.openstack.migration_idmap_retirement')
CLEANUP_RECOVERY_KEY = 'user.openstack.cleanup_required'
IDMAP_BASE_METADATA_KEY = 'incus_idmap_base'
IDMAP_SIZE_METADATA_KEY = 'incus_idmap_size'
IDMAP_ALLOCATION_METADATA_KEY = 'incus_idmap_allocation_id'
IDMAP_FINGERPRINT_METADATA_KEY = 'incus_idmap_fingerprint'
IDMAP_ALLOCATION_CONFIG_KEY = 'user.openstack.idmap_allocation_id'
IDMAP_COMPUTE_CONFIG_KEY = 'user.openstack.compute_id'
IDMAP_MATERIALIZATION_CONFIG_KEY = (
    'user.openstack.rootfs_materialization_id')
SPAWN_VOLUME_GENERATION_KEY = 'user.openstack.spawn_volume_generation'
_PRE_LIVE_DISCONNECTED_KEY = 'incus_pre_live_disconnected'
_VOLUME_ATTACHMENT_RECORD_VERSION = 2
_VOLUME_JOURNAL_VERSION = 1
_MANAGED_DETACH_INTENT_VERSION = 1
_MANAGED_ATTACH_INTENT_VERSION = 3
_COLD_ATTACHMENT_ROTATION_VERSION = 1
_COLD_ATTACHMENT_ROTATION_PHASES = frozenset({
    'prepared', 'creating', 'new-created', 'source-old-retained',
    'old-deleted', 'bdm-rotated',
    'source-release-complete', 'source-rollback-complete',
})
_COLD_ATTACHMENT_ROTATION_TERMINAL_PHASES = frozenset({
    'source-release-complete', 'source-rollback-complete',
})
_VOLUME_ATTACH_OPERATION_KINDS = frozenset({
    'hot-attach', 'spawn', 'reconcile', 'migration',
})
_VOLUME_ATTACH_MIGRATION_DIRECTIONS = frozenset({
    'cold-target', 'cold-source-restore', 'cold-revert-source',
    'live-target', 'live-source-release',
})
_SHARE_JOURNAL_VERSION = 2
_SPAWN_ATTEMPT_JOURNAL_VERSION = 1
_SPAWN_ATTEMPT_PHASES = frozenset({'preflight', 'opening'})
_HOST_RESOURCE_CACHE_TTL = 60
_INSTANCE_INVENTORY_CACHE_TTL = 10
_VOLUME_RECOVERY_TMP_STALE_SECONDS = 300
_VOLUME_RECOVERY_TMP_RE = re.compile(
    r'\.(?:attach|detach|rotation|volume)-[A-Za-z0-9_-]+\.tmp')


@dataclass(frozen=True)
class _IDMapMaterialization:
    assignment: object
    claim: object
    binding: object
    client: object


@dataclass(frozen=True)
class FailedBuildCleanupAssessment:
    """Resource-release decisions after an uncertain failed build cleanup."""

    release_network: bool
    release_cinder: bool
    release_host: bool
    release_placement: bool
    reasons: tuple = ()

    @classmethod
    def unsafe(cls, reason):
        return cls(False, False, False, False, (str(reason),))


def _canonical_materialization_id(value, label='materialization ID'):
    try:
        canonical = str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise incus_idmap.IDMapConfigurationError(
            '{} must be a canonical UUID'.format(label)) from exc
    if not isinstance(value, str) or canonical != value:
        raise incus_idmap.IDMapConfigurationError(
            '{} must be a canonical lowercase UUID'.format(label))
    return value


def _incus_instance_storage_volume(project_name, instance_name):
    """Return Incus project.Instance's internal storage volume name."""
    if project_name == 'default':
        return instance_name
    return '{}_{}'.format(project_name, instance_name)


def _idmap_host_claim_lock_name(instance_uuid):
    return 'incus-idmap-release-{}'.format(instance_uuid)


def _idmap_host_claim_lock_path():
    return CONF.state_path


_DEVICE_PATH_RE = re.compile(r'^/dev/[A-Za-z0-9][A-Za-z0-9._-]*$')
_VOLUME_MOUNTPOINT_RE = re.compile(
    r'^/dev/(?:vd|sd|xvd)[a-z]{1,2}[0-9]*$')
_CINDER_RBD_IMAGE_RE = re.compile(
    r'^volume-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
    r'[0-9a-f]{4}-[0-9a-f]{12})$')
_SHARE_ID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
    r'[0-9a-f]{4}-[0-9a-f]{12}$')
_SHARE_TAG_RE = re.compile(r'^[A-Za-z0-9-]{1,255}$')
_CEPHFS_NAME_RE = re.compile(r'^[A-Za-z0-9_.-]{1,255}$')
_CEPHFS_EXPORT_RE = re.compile(
    r'^(?P<monitors>[^\x00\r\n]+):(?P<path>/[A-Za-z0-9._/-]+)$')
_CEPHFS_DNS_NAME_RE = re.compile(
    r'^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$')
_STORAGE_RELEASE_RECEIPT_DIGEST_RE = re.compile(
    r'^sha256:[0-9a-f]{64}$')
_NETWORK_ACTIVATION_VENDOR_DATA = """#cloud-config
bootcmd:
  - |
    rc=0
    backend=
    if command -v netplan >/dev/null 2>&1; then
      backend=netplan
      netplan apply || rc=$?
    elif command -v nmcli >/dev/null 2>&1; then
      backend=NetworkManager
      nmcli connection reload || rc=$?
      for iface in /sys/class/net/nic*; do
        [ -e "$iface" ] || continue
        nmcli device connect "${iface##*/}" || rc=$?
      done
    elif command -v networkctl >/dev/null 2>&1; then
      backend=systemd-networkd
      networkctl reload || rc=$?
      for iface in /sys/class/net/nic*; do
        [ -e "$iface" ] || continue
        networkctl reconfigure "${iface##*/}" || rc=$?
      done
    elif command -v ifup >/dev/null 2>&1; then
      backend=ifupdown
      ifup -a || rc=$?
    else
      rc=127
    fi
    if [ "$rc" -ne 0 ]; then
      message="nova-incus: failed to activate guest network"
      [ -n "$backend" ] && message="$message using $backend"
      command -v logger >/dev/null 2>&1 && logger -t nova-incus "$message"
      printf '%s\\n' "$message" >/dev/console 2>/dev/null || true
      exit "$rc"
    fi
"""


def _root_storage_pool_traits():
    """Return collision-free Placement traits for configured pool selectors."""
    traits = {}
    for selector in CONF.incus.root_storage_pools:
        trait = common.root_storage_pool_trait(selector)
        previous = traits.get(trait)
        if previous is not None and previous != selector:
            raise exception.InvalidConfiguration(
                'Incus root storage pool selectors {} and {} map to the '
                'same Placement trait {}'.format(previous, selector, trait))
        traits[trait] = selector
    return set(traits)


def _invalidates_instance_inventory(action):
    """Keep short-lived bulk inventory caches coherent around host writes."""
    @functools.wraps(action)
    def wrapped(self, *args, **kwargs):
        self._invalidate_instance_inventory_cache()
        try:
            return action(self, *args, **kwargs)
        finally:
            # A concurrent periodic read may have populated an intermediate
            # state while the Incus operation was in flight.
            self._invalidate_instance_inventory_cache()

    return wrapped


def _guards_serial_console(action):
    """Refuse new console brokers for the whole of a guest-ending action.

    Applied to operations after which the guest no longer runs here, so a
    console request arriving mid-flight cannot build a broker the action
    is about to strand.
    """
    @functools.wraps(action)
    def wrapped(self, context, instance, *args, **kwargs):
        with self._quiesced_serial_console(instance):
            return action(self, context, instance, *args, **kwargs)

    return wrapped


def _validate_block_device_path(path, description):
    """Reject paths that could expose or overwrite arbitrary files."""
    if not isinstance(path, str) or not _DEVICE_PATH_RE.fullmatch(path):
        raise exception.InvalidVolume(
            reason='%s must be a direct block device path under /dev' %
            description)
    return path


def _validate_volume_mountpoint(path):
    if not isinstance(path, str) or not _VOLUME_MOUNTPOINT_RE.fullmatch(path):
        raise exception.InvalidVolume(
            reason='Volume mountpoint must be a Nova data disk path using '
            '/dev/vd*, /dev/sd*, or /dev/xvd*')
    return path


def _validate_profile_volume_slot(profile, volume_id, mountpoint,
                                  replacing_volume_id=None):
    if volume_id in profile.devices and volume_id != replacing_volume_id:
        raise exception.InvalidVolume(
            reason='Volume %s is already present in the Incus profile' %
            volume_id)
    for device_id, device in profile.devices.items():
        if (device_id != replacing_volume_id and
                device.get('path') == mountpoint):
            raise exception.DevicePathInUse(path=mountpoint)


def _validate_profile_volume_owner(profile, instance):
    config = profile.config if isinstance(profile.config, dict) else {}
    if (config.get('environment.product_name') != 'OpenStack Nova' or
            config.get('user.openstack.uuid') != instance.uuid):
        raise exception.InvalidVolume(
            reason='Incus profile is not owned by Nova instance %s' %
                   instance.uuid)


def _validate_volume_access_mode(connection_info):
    access_mode = connection_info.get('data', {}).get('access_mode') or 'rw'
    if access_mode != 'rw':
        raise exception.InvalidVolume(
            reason='Incus unix-block devices cannot enforce Cinder '
            'access_mode=%s' % access_mode)


def _data_volume_qos(connection_info, api_extensions):
    qos_specs = (connection_info.get('data') or {}).get('qos_specs') or {}
    if not qos_specs:
        return {}
    if 'unix_block_limits' not in api_extensions:
        raise exception.InvalidVolume(
            reason='Incus unix-block devices cannot enforce Cinder QoS yet')
    return flavor.disk_qos_limits(qos_specs, prefix='')


def _volume_id(connection_info):
    volume_id = (connection_info.get('serial') or
                 connection_info.get('data', {}).get('volume_id'))
    if volume_id is None or not str(volume_id).strip():
        raise exception.InvalidVolume(
            reason='Cinder connection information has no volume identifier')
    return str(volume_id)


def _detach_volume_id(profile, connection_info, mountpoint):
    """Resolve a data volume ID when Nova has lost attachment metadata."""
    try:
        return _volume_id(connection_info)
    except exception.InvalidVolume:
        matches = []
        for device_id, device in profile.devices.items():
            if (device.get('type') == 'unix-block' and
                    device.get('path') == mountpoint and
                    any(key in profile.config for key in
                        _volume_device_info_keys(device_id))):
                matches.append(device_id)

        if len(matches) != 1:
            raise exception.InvalidVolume(
                reason='Cinder connection information has no volume '
                       'identifier and Incus profile path {} resolves to {} '
                       'managed volumes'.format(mountpoint, len(matches)))

        LOG.warning(
            'Recovered missing Cinder volume identifier %(volume_id)s from '
            'Incus profile mountpoint %(mountpoint)s',
            {'volume_id': matches[0], 'mountpoint': mountpoint})
        return matches[0]


def _detach_volume_protocol(connection_info, device, device_info):
    """Resolve the os-brick protocol for legacy incomplete attachments."""
    protocol = connection_info.get('driver_volume_type')
    if protocol:
        return protocol

    # Older Nova attachment records can lose their connection_info while the
    # Incus profile still has the authoritative kernel RBD mapping. Only infer
    # a protocol when the device path proves which connector owns it.
    paths = [
        (device or {}).get('source'),
        (device_info or {}).get('path'),
    ]
    if any(str(path).startswith('/dev/rbd') for path in paths if path):
        LOG.warning(
            'Recovered missing Cinder driver_volume_type as rbd from Incus '
            'profile device metadata')
        return 'rbd'

    raise exception.InvalidVolume(
        reason='Cinder connection information has no driver_volume_type and '
               'the Incus profile does not prove an RBD mapping')


def _boot_from_volume(block_device_info):
    """Return and validate the single Nova root-volume BDM, if present."""
    if not isinstance(block_device_info, dict):
        return None
    roots = []
    for bdm in driver.block_device_info_get_mapping(block_device_info):
        try:
            boot_index = int(bdm.get('boot_index'))
        except (TypeError, ValueError):
            continue
        if boot_index == 0:
            roots.append(bdm)

    if len(roots) > 1:
        raise exception.InvalidConfiguration(
            'An Incus instance may have only one boot_index=0 volume')
    return roots[0] if roots else None


def _is_boot_volume(bdm):
    try:
        return int(bdm.get('boot_index')) == 0
    except (TypeError, ValueError):
        return False


def _boot_volume_mountpoints(volume_bdms, root_device_name=None):
    """Validate and reserve every Nova root-volume device path."""
    mountpoints = set()
    authoritative_root = None
    boot_volumes = [bdm for bdm in volume_bdms if _is_boot_volume(bdm)]
    if len(boot_volumes) > 1:
        raise exception.InvalidConfiguration(
            'An Incus instance may have only one boot_index=0 volume')
    if boot_volumes and root_device_name is not None:
        authoritative_root = _validate_volume_mountpoint(root_device_name)
        mountpoints.add(authoritative_root)
    for bdm in boot_volumes:
        mountpoint = bdm.get('mount_device')
        if mountpoint is None:
            continue
        mountpoint = _validate_volume_mountpoint(mountpoint)
        if mountpoint in mountpoints and mountpoint != authoritative_root:
            raise exception.DevicePathInUse(path=mountpoint)
        mountpoints.add(mountpoint)
    return mountpoints


def _spawn_data_volume_bdms(block_device_info, root_device_name=None):
    """Validate and return Cinder data volumes delegated to spawn()."""
    if not isinstance(block_device_info, dict):
        return []
    volume_bdms = list(
        driver.block_device_info_get_mapping(block_device_info))
    data_volumes = []
    volume_ids = set()
    mountpoints = _boot_volume_mountpoints(
        volume_bdms, root_device_name=root_device_name)
    for bdm in volume_bdms:
        connection_info = bdm.get('connection_info')
        if not connection_info:
            raise exception.InvalidVolume(
                reason='Spawn-time Cinder block-device mapping has no '
                       'connection information')

        volume_id = _bdm_volume_id(bdm)
        if volume_id in volume_ids:
            raise exception.InvalidVolume(
                reason='Cinder volume %s appears more than once in the '
                       'instance block-device mapping' % volume_id)
        volume_ids.add(volume_id)

        if _is_boot_volume(bdm):
            continue

        mountpoint = _validate_volume_mountpoint(bdm.get('mount_device'))
        if mountpoint in mountpoints:
            raise exception.DevicePathInUse(path=mountpoint)
        mountpoints.add(mountpoint)
        data_volumes.append(bdm)

    return data_volumes


def _reboot_data_volume_bdms(block_device_info, root_device_name=None):
    """Return validated non-root BDMs from Nova's power/reboot payload."""
    if not isinstance(block_device_info, dict):
        return []
    volume_bdms = list(
        driver.block_device_info_get_mapping(block_device_info))
    data_volumes = []
    volume_ids = set()
    mountpoints = _boot_volume_mountpoints(
        volume_bdms, root_device_name=root_device_name)
    explicit_boot = [bdm for bdm in volume_bdms if _is_boot_volume(bdm)]
    inferred_boot = None
    if not explicit_boot and root_device_name is not None:
        authoritative_root = _validate_volume_mountpoint(root_device_name)
        root_candidates = [
            bdm for bdm in volume_bdms
            if bdm.get('mount_device') == authoritative_root]
        if len(root_candidates) > 1:
            raise exception.DevicePathInUse(path=authoritative_root)
        if root_candidates:
            inferred_boot = root_candidates[0]
            mountpoints.add(authoritative_root)
    for bdm in volume_bdms:
        if _is_boot_volume(bdm) or bdm is inferred_boot:
            continue
        connection_info = bdm.get('connection_info')
        if not connection_info:
            raise exception.InvalidVolume(
                reason='Power-on Cinder block-device mapping has no '
                       'connection information')
        volume_id = _bdm_volume_id(bdm)
        if volume_id in volume_ids:
            raise exception.InvalidVolume(
                reason='Duplicate Cinder volume in power-on block mapping: '
                       '%s' % volume_id)
        volume_ids.add(volume_id)
        mountpoint = _validate_volume_mountpoint(bdm.get('mount_device'))
        if mountpoint in mountpoints:
            raise exception.DevicePathInUse(path=mountpoint)
        mountpoints.add(mountpoint)
        data_volumes.append(bdm)
    return data_volumes


def _bdm_volume_id(bdm):
    """Return the authoritative volume ID from a real or legacy driver BDM."""
    volume_id = getattr(bdm, 'volume_id', None)
    if not volume_id and hasattr(bdm, 'get'):
        volume_id = bdm.get('volume_id')
    if not volume_id:
        volume_id = _volume_id(bdm.get('connection_info') or {})
    if not str(volume_id).strip():
        raise exception.InvalidVolume(
            reason='Cinder block-device mapping has no volume identifier')
    return str(volume_id)


def _bdm_attachment_id(bdm):
    """Return the exact Cinder attachment generation from a driver BDM."""
    attachment_id = getattr(bdm, 'attachment_id', None)
    if not attachment_id and hasattr(bdm, 'get'):
        attachment_id = bdm.get('attachment_id')
    if not uuidutils.is_uuid_like(attachment_id):
        raise exception.InvalidVolume(
            reason='Cinder block-device mapping has no exact attachment ID')
    return str(attachment_id)


def _rbd_namespace(connection_data):
    """Return one unambiguous RBD namespace identity."""
    rbd_namespace = connection_data.get('rbd_namespace')
    legacy_namespace = connection_data.get('namespace')
    if (rbd_namespace is not None and legacy_namespace is not None and
            rbd_namespace != legacy_namespace):
        raise exception.InvalidVolume(
            reason='RBD connection information contains conflicting '
                   'namespace values')
    namespace = (
        rbd_namespace if rbd_namespace is not None else legacy_namespace)
    if namespace is None:
        return ''
    if not isinstance(namespace, str):
        raise exception.InvalidVolume(
            reason='RBD connection namespace must be a string')
    return namespace


def _validate_recoverable_data_volume(connection_info, volume_id):
    """Require the exact Cinder RBD identity used by reconciliation."""
    protocol = connection_info.get('driver_volume_type')
    if protocol != 'rbd':
        raise exception.InvalidVolume(
            reason='Incus data volumes currently require the Cinder RBD '
                   'protocol so their identity can be recovered after a '
                   'compute restart (volume %s uses %s)' %
                   (volume_id, protocol or 'no protocol'))

    connection_data = connection_info.get('data') or {}
    image = connection_data.get('name')
    if not isinstance(image, str) or image.count('/') != 1:
        raise exception.InvalidVolume(
            reason='Cinder RBD data volume %s has no stable pool/image name' %
                   volume_id)
    pool, image_name = image.split('/', 1)
    if not pool or not image_name:
        raise exception.InvalidVolume(
            reason='Cinder RBD data volume %s has an invalid pool/image name' %
                   volume_id)
    match = _CINDER_RBD_IMAGE_RE.fullmatch(image_name)
    if not match:
        raise exception.InvalidVolume(
            reason='Cinder RBD data volume image must use the canonical '
                   'volume-<UUID> name')
    if match.group(1) != str(volume_id).lower():
        raise exception.InvalidVolume(
            reason='Cinder RBD data volume image UUID does not match the '
                   'authoritative BDM volume ID')
    return image, _rbd_namespace(connection_data)


def _is_explicit_true(value):
    if value is True:
        return True
    if isinstance(value, int) and not isinstance(value, bool):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return False


def _has_encryption_marker(value):
    """Treat every non-empty encryption marker as unsupported."""
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in ('', '0', 'false', 'no', 'off')
    return bool(value)


def _cinder_rbd_root(bdm):
    connection_info = bdm.get('connection_info') or {}
    if connection_info.get('driver_volume_type') != 'rbd':
        raise exception.InvalidConfiguration(
            'Incus boot-from-volume requires a Cinder RBD volume')

    data = connection_info.get('data') or {}
    name = data.get('name')
    if not isinstance(name, str) or name.count('/') != 1:
        raise exception.InvalidConfiguration(
            'Cinder RBD connection_info must contain name=pool/image')
    ceph_pool, image_name = name.split('/', 1)
    match = _CINDER_RBD_IMAGE_RE.fullmatch(image_name)
    if not match:
        raise exception.InvalidConfiguration(
            'Cinder RBD root image must use the volume-<UUID> name')

    volume_id = _volume_id(connection_info)
    if volume_id.lower() != match.group(1):
        raise exception.InvalidConfiguration(
            'Cinder RBD image UUID does not match connection_info volume ID')
    _validate_volume_access_mode(connection_info)
    return ceph_pool, image_name


def _bfv_storage_pool_name(cinder_pool):
    """Resolve a Cinder RBD pool to its configured Incus cephext pool."""
    pool_name = CONF.incus.boot_from_volume_storage_pools.get(cinder_pool)
    if not pool_name:
        raise exception.InvalidConfiguration(
            'No Incus cephext storage pool is configured for Cinder RBD '
            'pool %s' % cinder_pool)
    return pool_name


def _advertised_bfv_storage_pools(readiness):
    """Return the non-secret Cinder-to-Incus pool readiness mapping."""
    encoded = readiness.get('user.openstack.bfv_storage_pools')
    if encoded:
        try:
            mapping = json.loads(encoded)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                'invalid destination BFV storage pool metadata: %s' % exc)
        if (not isinstance(mapping, dict) or
                not all(isinstance(key, str) and isinstance(value, str)
                        for key, value in mapping.items())):
            raise ValueError(
                'destination BFV storage pool metadata must be a string '
                'mapping')
        return mapping

    cinder_pool = readiness.get('user.openstack.cinder_rbd_pool')
    bfv_pool = readiness.get('user.openstack.bfv_pool')
    if cinder_pool and bfv_pool:
        return {cinder_pool: bfv_pool}
    return {}


def _bfv_root_device(instance, root_bdm, root_volume):
    """Build a Cinder-owned root device without Flavor size semantics."""
    flavor_limits = flavor.disk_qos_limits(instance.flavor.extra_specs)
    qos_specs = ((root_bdm.get('connection_info') or {}).get(
        'data') or {}).get('qos_specs') or {}
    volume_limits = flavor.disk_qos_limits(qos_specs, prefix='')
    if flavor_limits and volume_limits:
        raise exception.InvalidConfiguration(
            'A BFV root cannot combine Flavor disk quota and '
            'Cinder volume QoS')

    device = {
        'type': 'disk',
        'path': '/',
        'pool': _bfv_storage_pool_name(root_volume[0]),
        'initial.ceph.rbd.image_name': root_volume[1],
    }
    if root_bdm.get('volume_size'):
        device['size'] = '%dB' % (
            int(root_bdm['volume_size']) * units.Gi)
    device.update(volume_limits or flavor_limits)
    return device


def _require_bfv_migration_support(client, root_bdm):
    """Validate that an Incus endpoint can hand over a Cinder root RBD."""
    root_volume = _cinder_rbd_root(root_bdm)
    extensions = client.host_info.get('api_extensions', [])
    required = {
        'migration_shared_ceph_storage',
        INCUS_STORAGE_HANDOVER_EXTENSION,
        INCUS_STORAGE_HANDOVER_PROOF_EXTENSION,
        INCUS_STORAGE_READY_FENCE_EXTENSION,
        'storage_driver_cephext',
    }
    missing = sorted(required.difference(extensions))
    if missing:
        raise exception.MigrationError(
            reason='Incus BFV migration requires API extensions: %s' %
            ', '.join(missing))

    pool_name = _bfv_storage_pool_name(root_volume[0])
    pool = client.storage_pools.get(pool_name)
    pool_config = pool.config or {}
    if (pool.driver != 'cephext' or
            pool_config.get('source') != root_volume[0]):
        raise exception.MigrationError(
            reason='Incus BFV migration requires a cephext pool backed by '
            'the Cinder RBD pool')

    return root_volume


def _require_bfv_live_migration_support(client, root_bdm):
    """Require the ordered CRIU/cephext handover protocol on one endpoint."""
    try:
        root_volume = _require_bfv_migration_support(client, root_bdm)
    except (exception.InvalidConfiguration, exception.MigrationError) as exc:
        raise exception.MigrationPreCheckError(reason=str(exc))
    extensions = set(client.host_info.get('api_extensions', []))
    if INCUS_LIVE_BFV_MIGRATION_EXTENSION not in extensions:
        raise exception.MigrationPreCheckError(
            reason='Incus BFV live migration requires API extension: %s' %
            INCUS_LIVE_BFV_MIGRATION_EXTENSION)
    return root_volume


def _preflight_bfv_migration_destination(
        destination, cinder_pool, live=False):
    """Verify remote BFV readiness before stopping the source instance."""
    try:
        with socket.create_connection(
                (destination, CONF.incus.migration_port),
                timeout=CONF.incus.migration_preflight_timeout):
            pass
    except OSError as exc:
        raise exception.MigrationError(
            reason='Incus BFV destination %s:%s is unreachable: %s' % (
                destination, CONF.incus.migration_port, exc))

    tls_destination = CONF.incus.migration_preflight_server_names.get(
        destination, destination)
    if ':' in tls_destination and not tls_destination.startswith('['):
        tls_destination = '[%s]' % tls_destination
    endpoint = 'https://%s:%s' % (
        tls_destination, CONF.incus.migration_port)
    try:
        verify = CONF.incus.migration_preflight_tls_ca_by_server.get(
            destination, CONF.incus.migration_preflight_tls_ca)
        remote = incus_client.get_migration_preflight_client(
            endpoint, verify=verify)
        extensions = set(remote.host_info.get('api_extensions', []))
        required = {
            'migration_shared_ceph_storage',
            INCUS_STORAGE_HANDOVER_EXTENSION,
            INCUS_STORAGE_HANDOVER_PROOF_EXTENSION,
            INCUS_STORAGE_READY_FENCE_EXTENSION,
            'storage_driver_cephext',
        }
        if live:
            required.add(INCUS_LIVE_BFV_MIGRATION_EXTENSION)
        missing = sorted(required.difference(extensions))
        if missing:
            raise ValueError('missing API extensions: %s' % ', '.join(missing))

        project = remote.projects.get(CONF.incus.migration_preflight_project)
        readiness = project.config
        protocol = readiness.get('user.openstack.preflight_protocol')
        if protocol != '1':
            raise ValueError('unsupported or missing readiness protocol')
        bfv_pool = _bfv_storage_pool_name(cinder_pool)
        advertised_pools = _advertised_bfv_storage_pools(readiness)
        if advertised_pools.get(cinder_pool) != bfv_pool:
            raise ValueError(
                'destination readiness metadata does not advertise '
                'Cinder RBD pool %s through Incus pool %s' % (
                    cinder_pool, bfv_pool))
        pool = remote.storage_pools.get(bfv_pool)
        if pool.driver != 'cephext':
            raise ValueError('destination BFV pool is not cephext')
    except Exception as exc:
        raise exception.MigrationError(
            reason='Incus BFV destination readiness check failed: %s' % exc)


def _validated_migration_address(address):
    parsed = parse.urlsplit(address or '')
    if (parsed.scheme != 'https' or not parsed.netloc or
            parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise exception.InvalidConfiguration(
            '[incus] migration_address must be an HTTPS origin')
    return parsed


def _migration_endpoint(address):
    """Return a TLS-verifiable endpoint for an advertised migration origin."""
    parsed = _validated_migration_address(address)
    server_name = CONF.incus.migration_preflight_server_names.get(
        parsed.hostname, parsed.hostname)
    if ':' in server_name and not server_name.startswith('['):
        server_name = '[%s]' % server_name
    netloc = server_name
    if parsed.port:
        netloc = '%s:%d' % (server_name, parsed.port)
    return parse.urlunsplit((parsed.scheme, netloc, '', '', ''))


def _migration_client(address):
    parsed = _validated_migration_address(address)
    verify = CONF.incus.migration_tls_ca_by_server.get(
        parsed.hostname, CONF.incus.migration_tls_ca)
    return incus_client.get_migration_client(
        _migration_endpoint(address), verify=verify)


def _live_migration_destination_address(migrate_data):
    if not isinstance(
            migrate_data, incus_migrate_data.IncusLiveMigrateData):
        return None
    if not migrate_data.obj_attr_is_set('destination_address'):
        return None
    return migrate_data.destination_address


def _live_migration_cleanup_token(migrate_data):
    if not isinstance(
            migrate_data, incus_migrate_data.IncusLiveMigrateData):
        raise exception.MigrationError(
            reason='Missing Incus live migration data')
    if (not migrate_data.obj_attr_is_set('cleanup_token') or
            not uuidutils.is_uuid_like(migrate_data.cleanup_token)):
        raise exception.MigrationError(
            reason='Missing or invalid Incus live migration cleanup token')
    return migrate_data.cleanup_token


def _require_full_checkpoint_attestation(migrate_data):
    if (not isinstance(
            migrate_data, incus_migrate_data.IncusLiveMigrateData) or
            not migrate_data.obj_attr_is_set('full_checkpoint_verified') or
            migrate_data.full_checkpoint_verified is not True):
        raise exception.MigrationError(
            reason='Incus live migration source did not attest a locked '
                   'full-checkpoint configuration')


def _require_full_checkpoint_profile_config(config):
    if config.get('migration.incremental.memory') != 'false':
        raise exception.MigrationError(
            reason='Incus live migration requires source profile '
                   'migration.incremental.memory=false')


def _live_migration_uuid(migrate_data):
    if not isinstance(
            migrate_data, incus_migrate_data.IncusLiveMigrateData):
        raise exception.MigrationError(
            reason='Missing Incus live migration data')
    if (not migrate_data.obj_attr_is_set('migration_uuid') or
            not uuidutils.is_uuid_like(migrate_data.migration_uuid)):
        raise exception.MigrationError(
            reason='Missing or invalid Nova live Migration UUID')
    return migrate_data.migration_uuid


def _cold_migration_cleanup_token(context, instance):
    """Return the canonical Nova Migration UUID for a cold migration."""
    migration_context = getattr(instance, 'migration_context', None)
    if migration_context is None:
        raise exception.MigrationError(
            reason='Incus cold migration has no Nova migration context')
    migration_id = getattr(migration_context, 'migration_id', None)
    if (not isinstance(migration_id, int) or
            isinstance(migration_id, bool) or migration_id <= 0):
        raise exception.MigrationError(
            reason='Incus cold migration has no valid Nova migration ID')

    migration = objects.Migration.get_by_id_and_instance(
        context, migration_id, instance.uuid)
    token = getattr(migration, 'uuid', None)
    try:
        canonical_token = str(uuid.UUID(token))
    except (AttributeError, TypeError, ValueError):
        canonical_token = None
    if (not isinstance(token, str) or canonical_token != token or
            not _SHARE_ID_RE.fullmatch(token)):
        raise exception.MigrationError(
            reason='Incus cold migration has no canonical Nova Migration '
                   'UUID')
    return token


def _live_migration_idmap(migrate_data):
    if not isinstance(
            migrate_data, incus_migrate_data.IncusLiveMigrateData):
        raise exception.MigrationError(
            reason='Missing Incus live migration data')
    try:
        base = int(migrate_data.idmap_base)
        size = int(migrate_data.idmap_size)
    except (AttributeError, TypeError, ValueError):
        raise exception.MigrationError(
            reason='Missing or invalid Incus live migration idmap')
    if base < 0 or size <= 0:
        raise exception.MigrationError(
            reason='Missing or invalid Incus live migration idmap')
    return base, size


def prepare_cold_migration_share_info(disk_info, share_info):
    """Bind destination Manila staging to one cold-migration attempt."""
    try:
        transfer = jsonutils.loads(disk_info)
        if transfer.get('format') != 'incus-pull-v1':
            raise ValueError('unsupported migration data format')
        cleanup_token = transfer['cleanup_token']
        if not uuidutils.is_uuid_like(cleanup_token):
            raise ValueError('invalid migration cleanup token')
    except (TypeError, ValueError, KeyError) as exc:
        raise exception.MigrationError(
            reason='Invalid Incus migration data: %s' % exc)
    share_ids = []
    for share_mapping in share_info or []:
        if not _SHARE_ID_RE.fullmatch(share_mapping.share_id):
            raise exception.ShareMountError(
                share_id=share_mapping.share_id,
                server_id=share_mapping.instance_uuid,
                reason='share ID is not a canonical UUID')
        share_ids.append(share_mapping.share_id)
    if len(set(share_ids)) != len(share_ids):
        raise exception.MigrationError(
            reason='Cold migration contains duplicate Manila share mappings')
    transfer['manila_share_ids'] = sorted(share_ids)
    return jsonutils.dumps(transfer), cleanup_token


def _cold_migration_share_ids(transfer):
    share_ids = transfer.get('manila_share_ids', [])
    valid = (
        isinstance(share_ids, list) and
        all(
            isinstance(share_id, str) and
            bool(_SHARE_ID_RE.fullmatch(share_id))
            for share_id in share_ids
        ) and
        len(set(share_ids)) == len(share_ids)
    )
    if not valid:
        raise ValueError('invalid Manila share mapping list')
    return share_ids


def _parse_cold_migration_transfer(disk_info):
    """Validate and normalize the source-owned cold-migration envelope."""
    try:
        transfer = jsonutils.loads(disk_info)
        if transfer.get('format') != 'incus-pull-v1':
            raise ValueError('unsupported migration data format')
        migration_data = transfer['migration_data']
        cleanup_token = transfer['cleanup_token']
        if not uuidutils.is_uuid_like(cleanup_token):
            raise ValueError('invalid migration cleanup token')
        idmap_base = int(transfer['idmap_base'])
        idmap_size = int(transfer['idmap_size'])
        if idmap_base < 0 or idmap_size <= 0:
            raise ValueError('invalid migration idmap reservation')
        expected_share_ids = _cold_migration_share_ids(transfer)
        migration_data.setdefault('config', {})[
            'boot.autostart'] = 'false'
    except (TypeError, ValueError, KeyError) as exc:
        raise exception.MigrationError(
            reason='Invalid Incus migration data: %s' % exc) from exc
    return (
        transfer, migration_data, cleanup_token,
        idmap_base, idmap_size, expected_share_ids,
    )


def _bind_migration_instance_local_owner(migration_data, instance):
    """Bind migration payloads to the receipt owner in local config."""
    if not isinstance(migration_data, dict):
        raise exception.MigrationError(
            reason='Incus migration payload is not an object')
    config = migration_data.setdefault('config', {})
    if not isinstance(config, dict):
        raise exception.MigrationError(
            reason='Incus migration payload config is not an object')
    owner = config.get('user.openstack.uuid')
    if owner not in (None, instance.uuid):
        raise exception.MigrationError(
            reason='Incus migration payload belongs to another OpenStack '
                   'instance UUID')
    config['user.openstack.uuid'] = instance.uuid


def _migration_address_for_host(host):
    """Build the Incus migration origin advertised by another compute."""
    if not isinstance(host, str) or not host.strip():
        raise exception.MigrationError(
            reason='Migration destination host is missing')
    normalized = host.strip()
    if ':' in normalized and not normalized.startswith('['):
        normalized = '[%s]' % normalized
    return 'https://%s:%d' % (normalized, CONF.incus.migration_port)


def _instance_migration_idmap(container, profile=None):
    """Return the fixed isolated idmap that must be reserved on a target."""
    config = (
        container.config
        if container is not None and isinstance(container.config, dict)
        else {})
    expanded = (
        getattr(container, 'expanded_config', None)
        if isinstance(getattr(container, 'expanded_config', None), dict)
        else {})
    profile_config = (
        profile.config
        if profile is not None and isinstance(profile.config, dict) else {})
    try:
        base = int(
            config.get('volatile.idmap.base') or
            config.get('security.idmap.base') or
            expanded.get('volatile.idmap.base') or
            expanded.get('security.idmap.base') or
            profile_config.get('security.idmap.base'))
        size_value = (
            config.get('security.idmap.size') or
            expanded.get('security.idmap.size') or
            profile_config.get('security.idmap.size') or 65536)
        if str(size_value).lower() == 'auto':
            size_value = 65536
        size = int(size_value)
    except (TypeError, ValueError):
        raise exception.MigrationPreCheckError(
            reason='Source container has no valid fixed isolated idmap')
    if base < 0 or size <= 0:
        raise exception.MigrationPreCheckError(
            reason='Source container has no valid fixed isolated idmap')
    return base, size


def _instance_nova_uuid(container):
    """Return Nova ownership from Incus expanded or local instance config."""
    owners = set()
    for attribute in ('expanded_config', 'config'):
        config = getattr(container, attribute, None)
        if isinstance(config, dict):
            instance_uuid = config.get('user.openstack.uuid')
            if instance_uuid:
                owners.add(instance_uuid)
    if len(owners) == 1:
        return owners.pop()
    return None


def _require_migration_attempt_fencing(client):
    extensions = set(client.host_info.get('api_extensions', []))
    if INCUS_MIGRATION_ATTEMPT_EXTENSION not in extensions:
        raise exception.MigrationError(
            reason='Incus migration requires API extension: %s' %
            INCUS_MIGRATION_ATTEMPT_EXTENSION)


def _migration_attempt_metadata(response):
    try:
        metadata = response.json()['metadata']
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise exception.MigrationError(
            reason='Incus migration attempt returned invalid metadata') from (
                exc)
    if not isinstance(metadata, dict):
        raise exception.MigrationError(
            reason='Incus migration attempt returned invalid metadata')
    return metadata


def _validate_migration_attempt(
        attempt, instance, token, idmap_base, idmap_size):
    """Reject a token response that does not match its exact registration."""
    if (
            attempt.get('token') != token or
            attempt.get('project') != CONF.incus.project or
            attempt.get('resource_type') != 'instance' or
            attempt.get('resource_name') != instance.name):
        raise exception.MigrationError(
            reason='Incus migration attempt binding does not match the '
                   'requested Nova instance')
    try:
        attempt_base = int(attempt.get('idmap_base'))
        attempt_size = int(attempt.get('idmap_size'))
    except (TypeError, ValueError) as exc:
        raise exception.MigrationError(
            reason='Incus migration attempt has no fixed idmap '
                   'reservation') from exc
    if (attempt_base, attempt_size) != (idmap_base, idmap_size):
        raise exception.MigrationError(
            reason='Incus migration attempt idmap reservation does not '
                   'match the source instance')
    if attempt.get('state') not in (
            'active', 'aborted', 'committed', 'failed'):
        raise exception.MigrationError(
            reason='Incus migration attempt returned unsupported state %r' %
            attempt.get('state'))
    return attempt


def _destination_prepared_profile_binding(config):
    """Return the exact crash-recovery binding for a target profile."""
    if not isinstance(config, dict):
        raise exception.MigrationError(
            reason='Incus migration destination profile config is malformed')
    marker = config.get(MIGRATION_DESTINATION_PREPARED_KEY)
    cleanup_token = config.get(MIGRATION_CLEANUP_TOKEN_KEY)
    migration_uuid = config.get(MIGRATION_NOVA_UUID_KEY)
    instance_uuid = config.get('user.openstack.uuid')
    if (config.get('environment.product_name') != 'OpenStack Nova' or
            not uuidutils.is_uuid_like(instance_uuid) or
            not uuidutils.is_uuid_like(cleanup_token) or
            not uuidutils.is_uuid_like(migration_uuid) or
            marker != cleanup_token or
            config.get(MIGRATION_DESTINATION_KEY)):
        raise exception.MigrationError(
            reason='Incus migration destination profile has an invalid '
                   'prepared-owner binding')
    try:
        idmap_base = int(config.get('security.idmap.base'))
        idmap_size = int(config.get('security.idmap.size'))
    except (TypeError, ValueError) as exc:
        raise exception.MigrationError(
            reason='Incus migration destination profile has no fixed idmap '
                   'binding') from exc
    if idmap_base < 0 or idmap_size <= 0:
        raise exception.MigrationError(
            reason='Incus migration destination profile has an invalid '
                   'fixed idmap binding')
    return {
        'uuid': instance_uuid,
        'operation_token': cleanup_token,
        'migration_uuid': migration_uuid,
        'idmap_base': idmap_base,
        'idmap_size': idmap_size,
    }


def _get_migration_attempt(
        client, instance, token, idmap_base, idmap_size):
    _require_migration_attempt_fencing(client)
    response = client.api['migration-attempts'][token].get(
        params={'project': CONF.incus.project})
    return _validate_migration_attempt(
        _migration_attempt_metadata(response),
        instance, token, idmap_base, idmap_size)


def _migration_attempt_list_metadata(response):
    try:
        metadata = response.json()['metadata']
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise exception.MigrationError(
            reason='Incus migration attempt list returned invalid '
                   'metadata') from exc
    if not isinstance(metadata, list):
        raise exception.MigrationError(
            reason='Incus migration attempt list returned invalid metadata')
    return metadata


def _unstarted_migration_attempt_reservation(attempt):
    """Return the binding of an unstarted reservation, or None.

    Only a registration that Incus still counts against new attempts is
    worth releasing. A record that already started, already finished, or
    holds no idmap reservation costs the target nothing.
    """
    if not isinstance(attempt, dict):
        return None
    if (attempt.get('state') != 'active' or
            attempt.get('started') or
            attempt.get('finished') or
            not attempt.get('idmap_active') or
            attempt.get('project') != CONF.incus.project or
            attempt.get('resource_type') != 'instance'):
        return None
    token = attempt.get('token')
    name = attempt.get('resource_name')
    if not uuidutils.is_uuid_like(token) or not name:
        return None
    try:
        idmap_base = int(attempt['idmap_base'])
        idmap_size = int(attempt['idmap_size'])
    except (KeyError, TypeError, ValueError):
        return None
    if idmap_base < 0 or idmap_size <= 0:
        return None
    return {
        'token': token,
        'name': name,
        'idmap_base': idmap_base,
        'idmap_size': idmap_size,
    }


def _release_unstarted_migration_attempt(client, candidate):
    """Abort and retire one reservation whose migration never started.

    The re-read before the abort is what makes this safe to run from a
    periodic task. Incus finishes an abort of an unstarted attempt in the
    same statement that fences it, so a create request that won the race
    leaves the record unfinished and this refuses to retire it.
    """
    _require_migration_attempt_fencing(client)
    token = candidate['token']
    response = client.api['migration-attempts'][token].get(
        params={'project': CONF.incus.project})
    current = _unstarted_migration_attempt_reservation(
        _migration_attempt_metadata(response))
    if current != candidate:
        raise exception.MigrationError(
            reason='Incus migration attempt %s changed before its '
                   'reservation could be released' % token)

    response = client.api['migration-attempts'][token].put(
        params={'project': CONF.incus.project},
        json={'state': 'aborted'})
    attempt = _migration_attempt_metadata(response)
    if (attempt.get('token') != token or
            attempt.get('resource_name') != candidate['name'] or
            attempt.get('state') not in ('aborted', 'failed') or
            not attempt.get('finished')):
        raise exception.MigrationError(
            reason='Incus migration attempt %s started before its '
                   'reservation could be released' % token)

    client.api['migration-attempts'][token].delete(
        params={'project': CONF.incus.project})


def _register_migration_attempt(
        client, instance, token, idmap_base, idmap_size):
    """Durably reserve one target name and idmap before starting receive."""
    if not uuidutils.is_uuid_like(token):
        raise exception.MigrationError(
            reason='Missing or invalid Incus migration attempt token')
    _require_migration_attempt_fencing(client)
    response = client.api['migration-attempts'][token].put(
        params={'project': CONF.incus.project},
        json={
            'state': 'active',
            'resource_type': 'instance',
            'resource_name': instance.name,
            'idmap_base': idmap_base,
            'idmap_size': idmap_size,
        })
    attempt = _validate_migration_attempt(
        _migration_attempt_metadata(response),
        instance, token, idmap_base, idmap_size)
    if attempt['state'] != 'active':
        raise exception.MigrationError(
            reason='Incus migration attempt is not active')
    return attempt


def _migration_attempt_instance(client, instance):
    container = client.instances.get(instance.name)
    if _instance_nova_uuid(container) != instance.uuid:
        raise exception.MigrationError(
            reason='Incus migration target UUID does not match the Nova '
                   'instance')
    return container


def _wait_migration_attempt_finished(
        client, instance, token, idmap_base, idmap_size, states):
    """Wait for an attempt to reach one of the requested terminal states."""
    expected_states = set(states)

    def attempt_is_finished():
        attempt = _get_migration_attempt(
            client, instance, token, idmap_base, idmap_size)
        if attempt['state'] == 'committed':
            return attempt
        if (attempt['state'] not in expected_states or
                not attempt.get('finished')):
            raise _MigrationStateNotReady(
                'Incus migration attempt is not settled '
                '(state=%s, finished=%s)' % (
                    attempt['state'], attempt.get('finished')))
        return attempt

    return _wait_migration_finish_condition(
        attempt_is_finished, 'migration attempt settlement', instance)


def _abort_migration_attempt(
        client, instance, token, idmap_base, idmap_size,
        target_cleanup=None):
    """Fence a target before cancelling operations or accepting absence."""
    _require_migration_attempt_fencing(client)
    try:
        response = client.api['migration-attempts'][token].put(
            params={'project': CONF.incus.project},
            json={'state': 'aborted'})
        attempt = _validate_migration_attempt(
            _migration_attempt_metadata(response),
            instance, token, idmap_base, idmap_size)
    except incus_exceptions.LXDAPIException as exc:
        # A 409 can only be treated as a commit race after an exact GET proves
        # this token belongs to this instance and fixed idmap.
        if _incus_api_status_code(exc) != 409:
            raise
        attempt = _get_migration_attempt(
            client, instance, token, idmap_base, idmap_size)
        if attempt['state'] != 'committed':
            raise

    if attempt['state'] == 'committed':
        return attempt
    if attempt['state'] not in ('aborted', 'failed'):
        raise exception.MigrationError(
            reason='Incus migration attempt abort returned state %s' %
            attempt['state'])

    # PUT aborted is the irreversible fence. Only after it succeeds may Nova
    # ask Incus to cancel the operation and use target absence as evidence.
    operation_id = attempt.get('operation_uuid')
    _settle_instance_migration_operations(
        client, instance, operation_ids=(operation_id,))
    if target_cleanup is not None:
        # Incus deliberately keeps an aborted attempt unfinished when a
        # partially received target still exists after its operation
        # reverter. Delete that fenced record before waiting for settlement;
        # otherwise Nova and Incus would wait on each other indefinitely.
        target_cleanup()
    try:
        return _wait_migration_attempt_finished(
            client, instance, token, idmap_base, idmap_size,
            ('aborted', 'failed'))
    except _MigrationConditionTimeout as wait_error:
        # The normal receive reverter marks an aborted attempt finished. The
        # only exceptional path is an incusd restart that lost the in-memory
        # operation. Ask the server to settle that record only after Nova has
        # independently proved both the operation and target instance absent.
        try:
            client.instances.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
        else:
            raise wait_error

        try:
            response = client.api['migration-attempts'][token].put(
                params={'project': CONF.incus.project},
                json={'state': 'settled'})
        except incus_exceptions.LXDAPIException as exc:
            raise wait_error from exc
        attempt = _validate_migration_attempt(
            _migration_attempt_metadata(response),
            instance, token, idmap_base, idmap_size)
        if (attempt['state'] not in ('aborted', 'failed') or
                not attempt.get('finished')):
            raise wait_error
        return attempt


def _retire_migration_attempt(
        client, instance, token, idmap_base, idmap_size):
    """Garbage collect a terminal attempt while Incus retains its tombstone."""
    try:
        attempt = _get_migration_attempt(
            client, instance, token, idmap_base, idmap_size)
    except incus_exceptions.LXDAPIException as exc:
        if _is_incus_not_found(exc):
            return
        raise
    if not attempt.get('finished'):
        raise exception.MigrationError(
            reason='Refusing to retire an unfinished Incus migration attempt')
    client.api['migration-attempts'][token].delete(
        params={'project': CONF.incus.project})


def _finalize_committed_migration_attempt(
        client, instance, token, idmap_base, idmap_size):
    """Clear staging metadata and retire a committed target token."""
    attempt = _get_migration_attempt(
        client, instance, token, idmap_base, idmap_size)
    if attempt['state'] != 'committed' or not attempt.get('finished'):
        raise exception.MigrationError(
            reason='Incus migration attempt is not committed')
    profile = client.profiles.get(instance.name)
    config = profile.config if isinstance(profile.config, dict) else {}
    if (config.get('user.openstack.uuid') != instance.uuid or
            config.get(MIGRATION_CLEANUP_TOKEN_KEY) != token):
        raise exception.MigrationError(
            reason='Incus migration target profile changed before attempt '
                   'retirement')
    if config.get(MIGRATION_TARGET_VOLUMES_COMPLETE_KEY) != token:
        raise exception.MigrationError(
            reason='Incus migration target has no durable proof that its '
                   'Cinder volume transactions completed')
    original_config = dict(profile.config)
    updated_config = dict(profile.config)
    updated_config.pop(MIGRATION_CLEANUP_TOKEN_KEY, None)
    updated_config.pop(MIGRATION_CLEANUP_COMPLETE_KEY, None)
    updated_config.pop(MIGRATION_DESTINATION_PREPARED_KEY, None)
    updated_config.pop(MIGRATION_NOVA_UUID_KEY, None)
    updated_config.pop(MIGRATION_TARGET_OPERATION_KEY, None)
    updated_config.pop(MIGRATION_TARGET_VOLUMES_COMPLETE_KEY, None)
    profile.config = updated_config
    try:
        profile.save(wait=True)
    except Exception:
        # Keep this in-memory object consistent with the durable server state.
        # A later inventory pass must continue to see the prepared marker.
        profile.config = original_config
        raise
    _retire_migration_attempt(
        client, instance, token, idmap_base, idmap_size)


def _instance_root_pool(client, instance_name, container=None):
    """Return the root storage pool object for one Incus instance."""
    if container is None:
        container = client.instances.get(instance_name)
    devices = (
        getattr(container, 'expanded_devices', None) or
        getattr(container, 'devices', {}) or {})
    root = devices.get('root') or {}
    pool_name = root.get('pool')
    if not pool_name:
        raise exception.InvalidConfiguration(
            'Incus instance %s has no root storage pool' % instance_name)
    return client.storage_pools.get(pool_name)


def _storage_pool_identity(pool):
    config = pool.config or {}
    source = (
        config.get('source') or
        config.get('ceph.osd.pool_name') or '')
    return {
        'shared': pool.driver in ('ceph', 'cephext'),
        'driver': pool.driver,
        'cluster': config.get('ceph.cluster_name', 'ceph'),
        'source': source,
    }


def _placement_pool_identity(pool, pool_name):
    """Return a hashable physical identity for Placement accounting."""
    identity = _storage_pool_identity(pool)
    source = identity['source']
    if not source:
        raise exception.InvalidConfiguration(
            'Capacity-tracked Incus storage pool {} does not expose a '
            'stable source identity'.format(pool_name))
    return (
        identity['driver'],
        identity['cluster'] if identity['shared'] else '',
        source,
    )


def _validate_boot_from_volume_storage_pools(client):
    """Make this compute prove at startup what its BFV mapping claims.

    Nothing else reads the mapping until a boot-from-volume instance is
    already being built here, so a pool that was named but never created,
    or created against the wrong Cinder pool, produced a compute that
    started, reported up, accepted scheduling, and only failed once an
    instance landed on it. Root storage pools already fail startup for
    the same class of mistake.

    This is a node-local invariant: it asks whether this host can honour
    its own configuration, never what other computes offer.
    """
    for cinder_pool, pool_name in sorted(
            CONF.incus.boot_from_volume_storage_pools.items()):
        try:
            pool = client.storage_pools.get(pool_name)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
            raise exception.InvalidConfiguration(
                'boot_from_volume_storage_pools maps Cinder RBD pool {} to '
                'Incus storage pool {}, which does not exist on this '
                'compute'.format(cinder_pool, pool_name))
        # Only configuration invariants are checked here. A pool's status
        # is runtime state that can resolve itself, and refusing to start
        # over it would be the same mistake as freezing the Placement
        # inventory when a pool momentarily reports no capacity.
        pool_config = pool.config or {}
        if pool.driver != 'cephext':
            raise exception.InvalidConfiguration(
                'Incus boot-from-volume storage pool {} uses driver {}; '
                'boot-from-volume roots require cephext'.format(
                    pool_name, pool.driver))
        if pool_config.get('source') != cinder_pool:
            # A pool pointing at the wrong Cinder RBD pool is worse than a
            # missing one: it would resolve and then operate on another
            # backend's images.
            raise exception.InvalidConfiguration(
                'Incus boot-from-volume storage pool {} is backed by {} but '
                'is mapped to Cinder RBD pool {}'.format(
                    pool_name, pool_config.get('source'), cinder_pool))


def _validate_root_storage_pool_accounting(client):
    """Reject Placement mappings that can duplicate physical capacity."""
    selectors = CONF.incus.root_storage_pools
    resource_classes = CONF.incus.root_storage_pool_resource_classes
    shared_capacities = CONF.incus.shared_root_storage_pool_capacities_gb

    unused_capacities = set(shared_capacities) - set(resource_classes)
    if unused_capacities:
        raise exception.InvalidConfiguration(
            'shared_root_storage_pool_capacities_gb contains selectors '
            'without a capacity resource class: {}'.format(
                ', '.join(sorted(unused_capacities))))

    default_identity = None
    if CONF.incus.storage_pool and selectors:
        default_pool = client.storage_pools.get(CONF.incus.storage_pool)
        default_identity = _placement_pool_identity(
            default_pool, CONF.incus.storage_pool)

    selector_pools = {}
    for selector, pool_name in sorted(selectors.items()):
        pool = client.storage_pools.get(pool_name)
        pool_identity = _placement_pool_identity(pool, pool_name)
        selector_pools[selector] = (pool, pool_identity)
        if (pool_identity != default_identity and
                selector not in resource_classes):
            raise exception.InvalidConfiguration(
                'Non-default Incus root storage selector {} requires a '
                'dedicated Placement capacity resource class'.format(
                    selector))

    seen_classes = {}
    seen_pools = {}
    for selector, resource_class in sorted(resource_classes.items()):
        pool_name = selectors.get(selector)
        if not pool_name:
            raise exception.InvalidConfiguration(
                'Capacity-tracked Incus root storage selector {} is not '
                'present in root_storage_pools'.format(selector))
        if not resource_class.startswith('CUSTOM_'):
            raise exception.InvalidConfiguration(
                'Incus root storage resource class {} must start with '
                'CUSTOM_'.format(resource_class))

        previous = seen_classes.get(resource_class)
        if previous is not None:
            raise exception.InvalidConfiguration(
                'Incus root storage selectors {} and {} reuse Placement '
                'resource class {}'.format(
                    previous, selector, resource_class))
        seen_classes[resource_class] = selector

        pool, pool_identity = selector_pools[selector]
        previous = seen_pools.get(pool_identity)
        if previous is not None:
            raise exception.InvalidConfiguration(
                'Incus root storage selectors {} and {} report the same '
                'physical pool through separate Placement inventories'.format(
                    previous, selector))
        if default_identity == pool_identity:
            raise exception.InvalidConfiguration(
                'Incus root storage selector {} reports the default '
                'storage_pool {} a second time through {}'.format(
                    selector, CONF.incus.storage_pool, resource_class))
        seen_pools[pool_identity] = selector

        shared_capacity = shared_capacities.get(selector)
        shared = pool.driver in ('ceph', 'cephext')
        if shared and shared_capacity is None:
            raise exception.InvalidConfiguration(
                'Shared Incus root storage selector {} requires an explicit '
                'per-compute Placement capacity budget'.format(selector))
        if not shared and shared_capacity is not None:
            raise exception.InvalidConfiguration(
                'Shared capacity is configured for node-local Incus root '
                'storage selector {}'.format(selector))
        if shared:
            try:
                capacity_gb = int(shared_capacity)
            except (TypeError, ValueError):
                capacity_gb = 0
            if capacity_gb < 1:
                raise exception.InvalidConfiguration(
                    'Shared Incus root storage selector {} capacity budget '
                    'must be a positive GiB value'.format(selector))


def _instance_has_negotiated_handover(container):
    config = getattr(container, 'config', {}) or {}
    if not isinstance(config, dict):
        return False
    return bool(
        config.get('volatile.migration.storage_handover') or
        _instance_migration_receive_complete(container) or
        str(config.get(
            'volatile.migration.storage_delete_protection', '')).lower() in (
                '1', 'true', 'yes', 'on'))


def _instance_migration_receive_complete(container):
    config = getattr(container, 'config', {}) or {}
    return str(config.get(
        MIGRATION_RECEIVE_COMPLETE_KEY, '')).lower() in (
            '1', 'true', 'yes', 'on')


def _instance_storage_identity(client, instance_name):
    """Return a stable comparison tuple for an Incus root storage pool."""
    container = client.instances.get(instance_name)
    identity = _storage_pool_identity(
        _instance_root_pool(
            client, instance_name, container=container))
    identity['shared'] = (
        identity['shared'] and
        _instance_has_negotiated_handover(container))
    return identity


def _live_migration_shares_root_storage(client, instance, container=None):
    """Return whether this root moves by handover instead of by copy.

    Both Ceph-backed drivers hand the same volume to the destination rather
    than transferring its contents, so the source loses the right to touch
    that volume as soon as the destination claims it.
    """
    try:
        pool = _instance_root_pool(client, instance.name, container=container)
    except Exception as exc:
        LOG.debug(
            'Treating the Incus root pool as unshared because its identity '
            'could not be read: %s', exc, instance=instance)
        return False
    return pool.driver in ('ceph', 'cephext')


def _preflight_shared_ceph_handover_destination(
        destination, pool_name, source_identity):
    """Prove the target supports deletion-safe shared Ceph ownership."""
    remote = _migration_client(_migration_address_for_host(destination))
    extensions = set(remote.host_info.get('api_extensions', []))
    required = {
        INCUS_STORAGE_HANDOVER_EXTENSION,
        INCUS_STORAGE_HANDOVER_PROOF_EXTENSION,
        INCUS_STORAGE_READY_FENCE_EXTENSION,
    }
    missing = sorted(required - extensions)
    if missing:
        raise exception.MigrationError(
            reason='Incus migration destination does not advertise required '
            'shared Ceph handover extensions: %s' % ', '.join(missing))
    try:
        destination_pool = remote.storage_pools.get(pool_name)
    except Exception as exc:
        raise exception.MigrationError(
            reason='Cannot verify destination shared Ceph pool %s: %s' %
            (pool_name, exc)) from exc
    destination_identity = _storage_pool_identity(destination_pool)
    if (not destination_identity['shared'] or
            destination_identity['driver'] != source_identity['driver']):
        raise exception.MigrationError(
            reason='Source and destination Incus Ceph pool identities differ')
    if not (destination_pool.config or {}):
        # Incus redacts pool config for non-admin clients such as the
        # restricted migration preflight identity. Cluster and OSD pool
        # equality is then enforced authoritatively by the Incus fork's
        # migration negotiation, which refuses an in-place claim unless
        # the FSID, pool and driver all match exactly.
        LOG.debug(
            'Destination pool %s config is redacted for the preflight '
            'identity; deferring exact pool identity equality to the '
            'Incus migration negotiation', pool_name)
        return
    if destination_identity != source_identity:
        raise exception.MigrationError(
            reason='Source and destination Incus Ceph pool identities differ')


def _storage_handover_ownership_proof(
        client, instance_name, migration_attempt=None, operation_uuid=None):
    """Return the durable target receive proof required for ownership."""
    if migration_attempt is None or operation_uuid is None:
        profile = client.profiles.get(instance_name)
        config = profile.config if isinstance(profile.config, dict) else {}
        migration_attempt = (
            migration_attempt or
            config.get(MIGRATION_CLEANUP_TOKEN_KEY))
        operation_uuid = (
            operation_uuid or
            config.get(MIGRATION_TARGET_OPERATION_KEY))

    operation_uuid = _migration_operation_id(operation_uuid)
    if (not uuidutils.is_uuid_like(migration_attempt) or
            operation_uuid is None):
        raise exception.MigrationError(
            reason='Incus shared-storage ownership requires a committed '
                   'migration attempt and its target receive operation UUID')
    return migration_attempt, operation_uuid


def _set_storage_handover_state(
        client, instance_name, state, container=None,
        migration_attempt=None, operation_uuid=None):
    """Set deletion ownership for an Incus-managed shared Ceph root."""
    if container is None:
        container = client.instances.get(instance_name)

    pool = _instance_root_pool(
        client, instance_name, container=container)
    if pool.driver not in ('ceph', 'cephext'):
        return False

    config = (
        container.config if isinstance(container.config, dict) else {})
    handover = config.get('volatile.migration.storage_handover')
    delete_protected = str(config.get(
        'volatile.migration.storage_delete_protection', '')).lower() in (
            '1', 'true', 'yes', 'on')
    receive_complete = _instance_migration_receive_complete(container)

    if state == 'protected':
        # cephext never owns deletion of the external Cinder RBD. Ordinary
        # Ceph protection is valid only when Incus itself negotiated this
        # record as one side of the shared-volume handover.
        if pool.driver == 'cephext':
            return False
        if not (
                handover in ('pending', 'committed') or delete_protected):
            raise exception.MigrationError(
                reason='Refusing to delete an unprotected shared Ceph '
                       'instance record')
    elif state == 'owned':
        # A committed source or a destination whose complete receive was
        # durably recorded may become the sole deletion owner. Pool names
        # alone are never ownership proof.
        if (not handover and not delete_protected and
                not receive_complete and
                not config.get('volatile.migration.storage_handover_role')):
            return True
        if handover != 'committed' and not receive_complete:
            raise exception.MigrationError(
                reason='Incus shared-storage target has no completed receive '
                       'proof')
        migration_attempt, operation_uuid = (
            _storage_handover_ownership_proof(
                client, instance_name,
                migration_attempt=migration_attempt,
                operation_uuid=operation_uuid))
    elif state == 'source-owned':
        # Only the original source can abandon a pending or committed
        # handover, and only after Nova has independently fenced the
        # destination operation and record.
        if (not handover and not delete_protected and
                not receive_complete and
                not config.get('volatile.migration.storage_handover_role')):
            return True
        if (handover not in ('pending', 'committed') or
                config.get(
                    'volatile.migration.storage_handover_role') != 'source' or
                receive_complete):
            raise exception.MigrationError(
                reason='Incus shared-storage source does not have a '
                       'restorable handover state')
    else:
        raise exception.MigrationError(
            reason='Unsupported Incus storage handover state %s' % state)

    extensions = set(client.host_info.get('api_extensions', []))
    required = {INCUS_STORAGE_HANDOVER_EXTENSION}
    if state in ('owned', 'source-owned'):
        required.add(INCUS_STORAGE_HANDOVER_PROOF_EXTENSION)
    missing = sorted(required - extensions)
    if missing:
        raise exception.MigrationError(
            reason='Incus-managed Ceph migration requires API extensions: %s'
            % ', '.join(missing))

    request = {'state': state}
    if state == 'owned':
        request.update({
            'migration_attempt': migration_attempt,
            'operation_uuid': operation_uuid,
        })
    client.api.instances[instance_name]['storage-handover'].put(
        params={'project': CONF.incus.project},
        json=request)
    current = client.instances.get(instance_name)
    current_config = (
        current.config if isinstance(current.config, dict) else {})
    if state == 'protected':
        if str(current_config.get(
                'volatile.migration.storage_delete_protection', '')
               ).lower() not in ('1', 'true', 'yes', 'on'):
            raise exception.MigrationError(
                reason='Incus did not persist shared-storage delete '
                       'protection')
    elif not _storage_handover_is_owned(
            client, instance_name, container=current):
        raise exception.MigrationError(
            reason='Incus did not persist shared-storage ownership')
    return True


def _restore_source_storage_ownership(client, instance):
    """Restore one fenced source after the destination is conclusively gone."""
    container = client.instances.get(instance.name)
    return _set_storage_handover_state(
        client, instance.name, 'source-owned', container=container)


def _migration_operation_id(operation):
    """Return one validated Incus operation UUID from a URL or UUID."""
    if not isinstance(operation, str) or not operation:
        return None
    operation_id = os.path.basename(parse.urlsplit(operation).path)
    return operation_id if uuidutils.is_uuid_like(operation_id) else None


def _operation_targets_instance(operation, instance_name):
    resources = operation.get('resources') or {}
    if not isinstance(resources, dict):
        return False
    for resource in resources.get('instances') or []:
        if (isinstance(resource, str) and
                parse.unquote(os.path.basename(
                    parse.urlsplit(resource).path)) == instance_name):
            return True
    return False


def _operation_is_terminal(operation):
    try:
        return int(operation.get('status_code')) >= 200
    except (TypeError, ValueError):
        return operation.get('status') in (
            'Success', 'Failure', 'Cancelled', 'Canceled')


def _incus_operation(client, operation_id):
    response = client.api.operations[operation_id].get(
        params={'project': CONF.incus.project})
    metadata = response.json().get('metadata')
    if not isinstance(metadata, dict):
        raise exception.MigrationError(
            reason='Incus operation %s returned invalid metadata' %
            operation_id)
    return metadata


def _settle_incus_operation(client, operation_id, instance=None):
    """Cancel one Incus operation and wait until it is terminal.

    Incus refuses to settle a materialization attempt while its target
    operation is still pending, running or cancelling. A create that
    outlives the client's read timeout is exactly that case, so the abort
    path has to end the operation before it can settle the attempt.
    """
    operation_id = _migration_operation_id(operation_id)
    if not operation_id:
        return

    try:
        operation = _incus_operation(client, operation_id)
    except incus_exceptions.LXDAPIException as exc:
        if _is_incus_not_found(exc):
            return
        raise

    if _operation_is_terminal(operation):
        return

    if operation.get('may_cancel'):
        try:
            client.api.operations[operation_id].delete(
                params={'project': CONF.incus.project})
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise

    def operation_is_terminal():
        try:
            current = _incus_operation(client, operation_id)
        except incus_exceptions.LXDAPIException as exc:
            if _is_incus_not_found(exc):
                return
            raise
        if not _operation_is_terminal(current):
            raise _MigrationStateNotReady(
                'Incus operation %s is still active' % operation_id)

    _wait_migration_finish_condition(
        operation_is_terminal, 'operation %s settlement' % operation_id,
        instance)


def _instance_migration_operations(client, instance_name):
    response = client.api.operations.get(params={
        'project': CONF.incus.project,
        'recursion': 1,
    })
    grouped = response.json().get('metadata') or {}
    if not isinstance(grouped, dict) or not all(
            isinstance(operations, list)
            for operations in grouped.values()):
        raise exception.MigrationError(
            reason='Incus operation listing returned invalid metadata')
    operations = [
        operation
        for status_operations in grouped.values()
        for operation in status_operations
    ]
    return {
        operation.get('id'): operation
        for operation in operations
        if (isinstance(operation, dict) and
            isinstance(operation.get('id'), str) and
            _operation_targets_instance(operation, instance_name) and
            not _operation_is_terminal(operation))
    }


def _settle_instance_migration_operations(
        client, instance, operation_ids=()):
    """Cancel and prove terminal all migration operations for one instance."""
    pending = _instance_migration_operations(client, instance.name)
    for operation_id in operation_ids:
        operation_id = _migration_operation_id(operation_id)
        if operation_id and operation_id not in pending:
            try:
                operation = _incus_operation(client, operation_id)
            except incus_exceptions.LXDAPIException as exc:
                if _is_incus_not_found(exc):
                    continue
                raise
            if not _operation_is_terminal(operation):
                pending[operation_id] = operation

    for operation_id, operation in pending.items():
        if operation.get('may_cancel'):
            try:
                client.api.operations[operation_id].delete(
                    params={'project': CONF.incus.project})
            except incus_exceptions.LXDAPIException as exc:
                if not _is_incus_not_found(exc):
                    raise

        def operation_is_terminal():
            try:
                current = _incus_operation(client, operation_id)
            except incus_exceptions.LXDAPIException as exc:
                if _is_incus_not_found(exc):
                    return
                raise
            if not _operation_is_terminal(current):
                raise _MigrationStateNotReady(
                    'Incus migration operation %s is still active' %
                    operation_id)

        _wait_migration_finish_condition(
            operation_is_terminal,
            'operation %s settlement' % operation_id, instance)

    # A destination create can become visible after the request that listed
    # operations. A second empty scan is the barrier before absence is accepted
    # as proof that no late target can materialize.
    remaining = _instance_migration_operations(client, instance.name)
    if remaining:
        raise exception.MigrationError(
            reason='Incus still has active migration operations for %s: %s' %
            (instance.name, ', '.join(sorted(remaining))))


def _create_migration_target(
        client, config, instance, attempt_token, idmap_base, idmap_size,
        operation_started=None):
    """Start a fenced receive and recover a lost response from its token."""
    attempt = _get_migration_attempt(
        client, instance, attempt_token, idmap_base, idmap_size)
    if attempt['state'] == 'committed':
        return (
            _migration_attempt_instance(client, instance),
            attempt.get('operation_uuid') or None)
    if attempt['state'] != 'active':
        raise exception.MigrationError(
            reason='Incus migration attempt cannot start in state %s' %
            attempt['state'])

    request = copy.deepcopy(config)
    request.setdefault('source', {})['migration_attempt'] = attempt_token
    operation_id = None
    original_error = None
    try:
        response = client.api.instances.post(json=request)
        body = response.json()
        operation_id = _migration_operation_id(body.get('operation'))
        if operation_id is None:
            raise exception.MigrationError(
                reason='Incus migration target did not return an operation '
                       'UUID')
        if operation_started is not None:
            operation_started(operation_id)
        client.operations.wait_for_operation(operation_id)
    except Exception as exc:
        original_error = exc

    # The HTTP response and operation wait are not the commit record. A lost
    # response can still mean the target durably received the instance.
    attempt = _get_migration_attempt(
        client, instance, attempt_token, idmap_base, idmap_size)
    attempt_operation_id = _migration_operation_id(
        attempt.get('operation_uuid'))
    operation_id = attempt_operation_id or operation_id
    if attempt['state'] == 'active' and attempt_operation_id:
        try:
            client.operations.wait_for_operation(attempt_operation_id)
        except Exception as exc:
            if original_error is None:
                original_error = exc
        attempt = _get_migration_attempt(
            client, instance, attempt_token, idmap_base, idmap_size)

    if attempt['state'] == 'committed' and attempt.get('finished'):
        if operation_started is not None and operation_id is not None:
            operation_started(operation_id)
        return _migration_attempt_instance(client, instance), operation_id
    if original_error is not None:
        raise original_error
    raise exception.MigrationError(
        reason='Incus migration receive did not commit '
               '(state=%s, finished=%s)' % (
                   attempt['state'], attempt.get('finished')))


def _delete_migration_target_record(remote, instance):
    """Delete a migration target without deleting its shared Ceph root."""
    try:
        container = remote.instances.get(instance.name)
    except incus_exceptions.LXDAPIException as exc:
        if _is_incus_not_found(exc):
            return
        raise

    _set_storage_handover_state(
        remote, instance.name, 'protected', container=container)
    if container.status != 'Stopped':
        try:
            container.stop(timeout=-1, force=True, wait=True)
        except incus_exceptions.LXDAPIException as exc:
            if 'instance is already stopped' not in str(exc).lower():
                raise
    container.delete(wait=True)


def _mark_remote_migration_recovery(
        remote, instance_name, desired_state):
    """Leave a durable target-side repair request after an ambiguous commit."""
    profile = remote.profiles.get(instance_name)
    profile.config[MIGRATION_RECOVERY_KEY] = desired_state
    profile.save(wait=True)


def _storage_handover_is_owned(client, instance_name, container=None):
    """Return whether a shared root has no remaining handover state."""
    if container is None:
        container = client.instances.get(instance_name)
    pool = _instance_root_pool(
        client, instance_name, container=container)
    if pool.driver not in ('ceph', 'cephext'):
        return True

    config = (
        container.config if isinstance(container.config, dict) else {})
    return (
        not config.get('volatile.migration.storage_handover') and
        not config.get('volatile.migration.storage_handover_role') and
        str(config.get(
            'volatile.migration.storage_delete_protection', '')).lower()
        not in ('1', 'true', 'yes', 'on') and
        not _instance_migration_receive_complete(container)
    )


def _converge_migration_target_ownership(
        client, instance, desired_state=None, local_volume_evidence=False):
    """Commit or verify target ownership after a lost Nova callback."""
    container = client.instances.get(instance.name)
    if desired_state is None:
        desired_state = (
            'running' if container.status == 'Running' else 'stopped')
    if _instance_nova_uuid(container) != instance.uuid:
        raise exception.MigrationError(
            reason='Incus migration destination UUID does not match the '
                   'Nova instance')

    profile = client.profiles.get(instance.name)
    profile_config = (
        profile.config if isinstance(profile.config, dict) else {})
    if (profile_config.get('environment.product_name') != 'OpenStack Nova' or
            profile_config.get('user.openstack.uuid') != instance.uuid):
        raise exception.MigrationError(
            reason='Incus migration destination profile is not owned by '
                   'the Nova instance')

    cleanup_token = profile_config.get(MIGRATION_CLEANUP_TOKEN_KEY)

    def publish_local_volume_proof():
        if not local_volume_evidence:
            return
        migration_uuid = profile_config.get(MIGRATION_NOVA_UUID_KEY)
        if not _publish_migration_target_volumes_complete(
                client, instance, cleanup_token, migration_uuid):
            raise exception.MigrationError(
                reason='Incus migration target retains a local Cinder '
                       'volume transaction')

    if _storage_handover_is_owned(
            client, instance.name, container=container):
        # The ownership transition precedes profile/attempt retirement. A
        # process can die in that interval, so finish it when proof remains.
        if uuidutils.is_uuid_like(cleanup_token):
            idmap_base, idmap_size = _instance_migration_idmap(
                container, profile)
            try:
                publish_local_volume_proof()
                _finalize_committed_migration_attempt(
                    client, instance, cleanup_token,
                    idmap_base, idmap_size)
            except Exception:
                LOG.critical(
                    'Migration target owns shared storage but its staging '
                    'metadata could not be retired; queued recovery',
                    instance=instance, exc_info=True)
                _mark_remote_migration_recovery(
                    client, instance.name, desired_state)
        return

    if not uuidutils.is_uuid_like(cleanup_token):
        raise exception.MigrationError(
            reason='Protected Incus migration target has no valid cleanup '
                   'token')
    idmap_base, idmap_size = _instance_migration_idmap(container, profile)
    attempt = _get_migration_attempt(
        client, instance, cleanup_token, idmap_base, idmap_size)
    if attempt['state'] != 'committed' or not attempt.get('finished'):
        raise exception.MigrationError(
            reason='Protected Incus migration target has no committed '
                   'migration attempt')

    try:
        _retry_migration_finish_action(
            lambda: _set_storage_handover_state(
                client, instance.name, 'owned', container=container,
                migration_attempt=cleanup_token,
                operation_uuid=attempt.get('operation_uuid')),
            'recovered destination shared-storage ownership commit',
            instance)
    except Exception as ownership_error:
        try:
            _mark_remote_migration_recovery(
                client, instance.name, desired_state)
        except Exception:
            LOG.critical(
                'Protected migration target could neither commit ownership '
                'nor persist a recovery marker',
                instance=instance, exc_info=True)
            raise exception.MigrationError(
                reason='Protected Incus migration target has no durable '
                       'ownership recovery marker') from ownership_error
        return

    try:
        publish_local_volume_proof()
        _finalize_committed_migration_attempt(
            client, instance, cleanup_token, idmap_base, idmap_size)
    except Exception:
        LOG.critical(
            'Recovered migration target ownership but could not retire its '
            'staging metadata; queued recovery',
            instance=instance, exc_info=True)
        _mark_remote_migration_recovery(
            client, instance.name, desired_state)


def _validate_migration_share_mappings(context, instance, profile):
    """Validate Manila profile devices against Nova's authoritative records."""
    mappings = objects.ShareMappingList.get_by_instance_uuid(
        context, instance.uuid)
    expected = {}
    for mapping in mappings:
        share_id = getattr(mapping, 'share_id', None)
        if (not isinstance(share_id, str) or
                not _SHARE_ID_RE.fullmatch(share_id)):
            raise exception.MigrationPreCheckError(
                reason='Nova Manila mapping has a non-canonical share ID')
        if (getattr(mapping, 'status', None) !=
                obj_fields.ShareMappingStatus.ACTIVE):
            raise exception.MigrationPreCheckError(
                reason='Manila share %s is not active' % share_id)
        if getattr(mapping, 'instance_uuid', None) != instance.uuid:
            raise exception.MigrationPreCheckError(
                reason='Nova Manila mapping %s belongs to another instance' %
                       share_id)
        tag = getattr(mapping, 'tag', None)
        if not isinstance(tag, str) or not _SHARE_TAG_RE.fullmatch(tag):
            raise exception.MigrationPreCheckError(
                reason='Nova Manila mapping %s has an invalid guest tag' %
                       share_id)

        device_name = _share_device_name(mapping)
        if device_name in expected:
            raise exception.MigrationPreCheckError(
                reason='Nova has duplicate Manila mappings for share %s' %
                       share_id)
        expected[device_name] = {
            'type': 'disk',
            'source': _share_mount_path(instance, mapping),
            'path': _share_guest_path(mapping),
            'readonly': 'false',
            'recursive': 'true',
        }

    _mounts, malformed = _profile_share_mount_inventory(profile, instance)
    if malformed:
        device_name, reason = malformed[0]
        raise exception.MigrationPreCheckError(
            reason='Incus Manila profile device %(device)s is malformed: '
                   '%(reason)s' % {
                       'device': device_name,
                       'reason': reason,
                   })

    actual = {
        name: device
        for name, device in profile.devices.items()
        if name.startswith('manila-')
    }
    if actual != expected:
        raise exception.MigrationPreCheckError(
            reason='Incus Manila devices do not match Nova share mappings')
    return mappings


def _live_migration_profile_check(client, context, instance):
    profile = client.profiles.get(instance.name)
    config = profile.config if isinstance(profile.config, dict) else {}
    if config.get('migration.stateful') != 'true':
        raise exception.MigrationPreCheckError(
            reason='Instance was not created with migration.stateful=true')
    if _is_explicit_true(config.get('security.privileged', 'false')):
        raise exception.MigrationPreCheckError(
            reason='Privileged Incus containers cannot use Nova live '
                   'migration')
    profile_uuid = config.get('user.openstack.uuid')
    if profile_uuid != instance.uuid:
        raise exception.MigrationPreCheckError(
            reason='Incus source profile has a missing or different Nova UUID')

    _validate_migration_share_mappings(context, instance, profile)
    unsupported = []
    for name, device in profile.devices.items():
        device_type = device.get('type')
        if name == 'root' and device_type == 'disk':
            continue
        if name.startswith('manila-') and device_type == 'disk':
            continue
        if device_type in ('nic', 'none', 'unix-block'):
            continue
        unsupported.append('%s:%s' % (name, device_type or 'unknown'))
    if unsupported:
        raise exception.MigrationPreCheckError(
            reason='Incus live migration does not support profile devices: '
            '%s' % ', '.join(sorted(unsupported)))
    return profile


def _validate_live_migration_source_instance(
        instance, container, profile, require_incremental=False,
        error_class=exception.MigrationPreCheckError):
    """Validate effective source state that is not profile-only."""
    profiles = (
        list(container.profiles)
        if isinstance(container.profiles, (list, tuple)) else [])
    if profiles != [instance.name]:
        raise error_class(
            reason='Incus live migration requires the dedicated instance '
                   'profile to be the only attached profile')
    config = container.config if isinstance(container.config, dict) else {}
    if config.get('user.openstack.uuid') != instance.uuid:
        raise error_class(
            reason='Incus source instance has a missing or different Nova '
                   'UUID')
    if _is_explicit_true(config.get('security.privileged', 'false')):
        raise error_class(
            reason='Privileged Incus containers cannot use Nova live '
                   'migration')
    expanded_config = (
        container.expanded_config
        if isinstance(container.expanded_config, dict) else {})
    if expanded_config.get('migration.stateful') != 'true':
        raise error_class(
            reason='Incus source expanded config must set '
                   'migration.stateful=true')
    if _is_explicit_true(expanded_config.get(
            'security.privileged', 'false')):
        raise error_class(
            reason='Privileged Incus containers cannot use Nova live '
                   'migration')
    if require_incremental and (
            (profile.config if isinstance(profile.config, dict) else {}).get(
                'migration.incremental.memory') != 'false' or
            config.get('migration.incremental.memory') != 'false' or
            expanded_config.get(
                'migration.incremental.memory') != 'false'):
        raise error_class(
            reason='Incus source profile, local config, and expanded config '
                   'must set migration.incremental.memory=false')
    return config, expanded_config


def _full_checkpoint_live_migration_source(
        client, context, instance, block_device_info,
        normalize_incremental_memory=False):
    """Read and validate one locked source snapshot for live migration."""
    profile = _live_migration_profile_check(client, context, instance)
    container = client.instances.get(instance.name)
    config, expanded_config = _validate_live_migration_source_instance(
        instance, container, profile)
    profile_incremental = profile.config.get(
        'migration.incremental.memory')
    local_incremental = config.get('migration.incremental.memory')
    expanded_incremental = expanded_config.get(
        'migration.incremental.memory')
    if (profile_incremental != 'false' or
            local_incremental != 'false' or
            expanded_incremental != 'false'):
        if not normalize_incremental_memory:
            raise exception.MigrationPreCheckError(
                reason='Incus source profile, local config, and expanded '
                       'config must set migration.incremental.memory=false')
        if profile.config.get('migration.incremental.memory') != 'false':
            profile.config['migration.incremental.memory'] = 'false'
            try:
                profile.save(wait=True)
            except Exception as exc:
                raise exception.MigrationPreCheckError(
                    reason='Failed to disable CRIU incremental memory '
                           'migration on the source profile: %s' % exc
                ) from exc
        if config.get('migration.incremental.memory') != 'false':
            container.config['migration.incremental.memory'] = 'false'
            try:
                container.save(wait=True)
            except Exception as exc:
                raise exception.MigrationPreCheckError(
                    reason='Failed to disable CRIU incremental memory '
                           'migration on the source instance: %s' % exc
                ) from exc
        # Re-read and repeat the complete profile validation after both
        # writes. This closes the interval in which another driver operation
        # could have changed devices, ownership, or migration markers.
        profile = _live_migration_profile_check(client, context, instance)
        container = client.instances.get(instance.name)
        try:
            config, expanded_config = _validate_live_migration_source_instance(
                instance, container, profile, require_incremental=True)
        except exception.MigrationPreCheckError as exc:
            raise exception.MigrationPreCheckError(
                reason='Incus source profile, local config, and expanded '
                       'config did not converge to '
                       'the required live-migration state: %s' % exc)
        LOG.info(
            'Disabled CRIU incremental memory migration on an existing '
            'Incus instance before live migration', instance=instance)

    _validate_live_migration_data_volumes(profile, block_device_info)
    if container.status != 'Running':
        raise exception.MigrationPreCheckError(
            reason='Incus CRIU live migration requires a running instance')
    if profile.config.get(CLEANUP_RECOVERY_KEY):
        raise exception.MigrationPreCheckError(
            reason='Incus source profile has unresolved cleanup work')
    if any(profile.config.get(key) for key in (
            MIGRATION_CLEANUP_TOKEN_KEY,
            MIGRATION_ROLLBACK_COMPLETE_KEY,
            MIGRATION_NOVA_UUID_KEY,
            MIGRATION_DESTINATION_KEY,
            MIGRATION_OPERATION_KEY)):
        raise exception.MigrationPreCheckError(
            reason='Incus source profile has an unresolved migration '
                   'generation')
    return container, profile


def _validate_live_migration_data_volumes(profile, block_device_info):
    """Require each unix-block profile device to match one Nova data BDM."""
    expected = {}
    for bdm in driver.block_device_info_get_mapping(block_device_info):
        if _is_boot_volume(bdm):
            continue
        connection_info = bdm.get('connection_info')
        mountpoint = bdm.get('mount_device')
        if not connection_info or not mountpoint:
            raise exception.MigrationPreCheckError(
                reason='Cinder data volume migration requires complete '
                'connection information and a mount device')
        expected[_volume_id(connection_info)] = mountpoint

    actual = {
        name: device.get('path')
        for name, device in profile.devices.items()
        if device.get('type') == 'unix-block'
    }
    if actual != expected:
        raise exception.MigrationPreCheckError(
            reason='Incus unix-block devices do not match Nova Cinder data '
            'volume mappings')


def _migration_host_facts(client):
    environment = client.host_info.get('environment', {})
    facts = {
        'architecture': environment.get('kernel_architecture'),
        'kernel_version': environment.get('kernel_version'),
        'server_version': environment.get('server_version'),
    }
    missing = sorted(name for name, value in facts.items() if not value)
    if missing:
        raise exception.MigrationPreCheckError(
            reason='Incus did not report migration host facts: %s' %
            ', '.join(missing))
    return facts


def _require_stateful_migration_extension(client):
    if INCUS_STATEFUL_MIGRATION_EXTENSION not in set(
            client.host_info.get('api_extensions', [])):
        raise exception.MigrationPreCheckError(
            reason='Incus server does not advertise %s' %
            INCUS_STATEFUL_MIGRATION_EXTENSION)


def _require_live_ceph_migration_extension(client):
    if INCUS_LIVE_CEPH_MIGRATION_EXTENSION not in set(
            client.host_info.get('api_extensions', [])):
        raise exception.MigrationPreCheckError(
            reason='Incus server does not advertise %s' %
            INCUS_LIVE_CEPH_MIGRATION_EXTENSION)


def _migration_operation_url(operation_url, migration_address):
    """Expose a local Incus migration operation on its remote endpoint."""
    operation = parse.urlsplit(operation_url)
    address = _validated_migration_address(migration_address)
    return parse.urlunsplit((
        address.scheme,
        address.netloc,
        operation.path,
        '',
        '',
    ))


def _delete_migration_target_resource(
        remote, collection, name, project, wait=False):
    """Delete a remote resource without losing the client's project scope."""
    manager = getattr(remote, collection)
    manager.get(name)
    response = getattr(remote.api, collection)[name].delete(
        params={'project': project})
    if wait:
        remote.operations.wait_for_operation(response.json()['operation'])


def _remove_live_migration_target(remote, instance):
    """Remove target artifacts after a failed destination create."""
    try:
        _delete_migration_target_resource(
            remote, 'instances', instance.name, CONF.incus.project, wait=True)
    except incus_exceptions.LXDAPIException as exc:
        if not _is_incus_not_found(exc):
            raise
    try:
        _delete_migration_target_resource(
            remote, 'profiles', instance.name, CONF.incus.project)
    except incus_exceptions.LXDAPIException as exc:
        if not _is_incus_not_found(exc):
            raise


def _remove_stale_live_migration_profile(client, instance):
    """Remove an unused profile left by an earlier failed migration."""
    try:
        client.instances.get(instance.name)
    except incus_exceptions.LXDAPIException as exc:
        if not _is_incus_not_found(exc):
            raise
    else:
        raise exception.DestinationDiskExists(path=instance.name)

    try:
        profile = client.profiles.get(instance.name)
    except incus_exceptions.LXDAPIException as exc:
        if _is_incus_not_found(exc):
            return
        raise

    profile_config = (
        profile.config if isinstance(profile.config, dict) else {})
    profile_devices = (
        profile.devices if isinstance(profile.devices, dict) else {})
    if (profile_config.get('environment.product_name') !=
            'OpenStack Nova' or
            profile_config.get('user.openstack.uuid') != instance.uuid or
            profile.used_by):
        raise exception.DestinationDiskExists(path=instance.name)
    mounted_manila_devices = any(
        name.startswith('manila-') and
        device.get('source') and
        os.path.ismount(device['source'])
        for name, device in profile_devices.items())
    if (profile_config.get(MIGRATION_DESTINATION_PREPARED_KEY) or
            _profile_has_volume_connections(profile) or
            _volume_journal_records(instance) or
            mounted_manila_devices):
        raise exception.DestinationDiskExists(path=instance.name)
    profile.delete()


def _stateful_migration_profile_config(container, profile):
    """Copy a profile while pinning the source user namespace mapping."""
    config = dict(profile.config)
    try:
        idmap_base, idmap_size = _instance_migration_idmap(
            container, profile)
    except exception.MigrationPreCheckError:
        raise

    # CRIU records the source user namespace IDs in its checkpoint. Incus
    # would otherwise allocate a new isolated range on the independent target
    # daemon and shift the checkpoint files to IDs that CRIU cannot access.
    config['security.idmap.base'] = str(idmap_base)
    config['security.idmap.size'] = str(idmap_size)
    return config


def _live_migration_source_profile(container, profile):
    """Serialize the source profile without host-specific block paths."""
    expanded_config = (
        container.expanded_config
        if isinstance(container.expanded_config, dict) else {})
    if expanded_config.get('migration.incremental.memory') != 'false':
        raise exception.MigrationPreCheckError(
            reason='Incus source expanded config must set '
                   'migration.incremental.memory=false')
    if expanded_config.get('migration.stateful') != 'true':
        raise exception.MigrationPreCheckError(
            reason='Incus source expanded config must set '
                   'migration.stateful=true')
    if _is_explicit_true(expanded_config.get(
            'security.privileged', 'false')):
        raise exception.MigrationPreCheckError(
            reason='Privileged Incus containers cannot use Nova live '
                   'migration')
    config = _stateful_migration_profile_config(container, profile)
    # The target must receive the same full-checkpoint policy even when this
    # is an older profile normalized through instance-local configuration.
    config['migration.incremental.memory'] = 'false'
    for key in list(config):
        if key.startswith((
                'user.openstack.volume.',
                'user.openstack.volume_device_info.')):
            config.pop(key)
    config.pop(MIGRATION_RECOVERY_KEY, None)
    config.pop(MIGRATION_DESTINATION_KEY, None)
    config.pop(MIGRATION_OPERATION_KEY, None)
    config.pop(MIGRATION_TARGET_OPERATION_KEY, None)
    config.pop(MIGRATION_ROLLBACK_COMPLETE_KEY, None)
    config.pop(MIGRATION_NOVA_UUID_KEY, None)
    config.pop(CLEANUP_RECOVERY_KEY, None)
    config.pop(MIGRATION_DESTINATION_PREPARED_KEY, None)

    devices = copy.deepcopy(profile.devices)
    for name, device in list(devices.items()):
        if device.get('type') == 'unix-block':
            devices.pop(name)

    return jsonutils.dumps({
        'config': config,
        'devices': devices,
    })


def _live_migration_profile_data(migrate_data):
    try:
        data = jsonutils.loads(migrate_data.source_profile)
        config = data['config']
        devices = data['devices']
    except (AttributeError, KeyError, TypeError, ValueError):
        raise exception.MigrationError(
            reason='Missing or invalid Incus source profile migration data')
    if not isinstance(config, dict) or not isinstance(devices, dict):
        raise exception.MigrationError(
            reason='Invalid Incus source profile migration data')
    return config, devices


def _volume_device_info_key(volume_id):
    return 'user.openstack.volume.%s' % volume_id


def _legacy_volume_device_info_key(volume_id):
    return 'user.openstack.volume_device_info.%s' % volume_id


def _volume_device_info_keys(volume_id):
    return (_volume_device_info_key(volume_id),
            _legacy_volume_device_info_key(volume_id))


def _sanitize_volume_connection_data(value, key=None):
    """Copy connector data without persisting credentials in Incus."""
    sensitive = {
        'auth_password', 'password', 'key', 'keyring', 'secret', 'token',
    }
    if key == _PRE_LIVE_DISCONNECTED_KEY:
        return None
    if key is not None and (
            key.lower() in sensitive or
            key.lower().endswith(('_password', '_token'))):
        return None
    if isinstance(value, dict):
        sanitized = {}
        for child_key, child_value in value.items():
            if not isinstance(child_key, str):
                continue
            cleaned = _sanitize_volume_connection_data(
                child_value, child_key)
            if cleaned is not None:
                sanitized[child_key] = cleaned
        return sanitized
    if isinstance(value, (list, tuple)):
        return [
            cleaned for child in value
            if (cleaned := _sanitize_volume_connection_data(child))
            is not None
        ]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise exception.InvalidVolume(
        reason='Cinder connection data contains an unsupported value type')


def _serialize_volume_attachment(
        connection_info, device_info, mountpoint, phase='connected'):
    """Serialize a durable, credential-free os-brick cleanup record."""
    protocol = connection_info.get('driver_volume_type')
    if not isinstance(protocol, str) or not protocol:
        raise exception.InvalidVolume(
            reason='Cinder connection information has no driver_volume_type')
    record = {
        'version': _VOLUME_ATTACHMENT_RECORD_VERSION,
        'phase': phase,
        'driver_volume_type': protocol,
        'connection_data': _sanitize_volume_connection_data(
            connection_info.get('data') or {}),
        'device_info': device_info,
        'mountpoint': mountpoint,
    }
    return _serialize_device_info(record)


def _volume_journal_directory(instance):
    return os.path.join(
        CONF.instances_path, 'incus-volume-journal', instance.uuid)


def _volume_journal_path(instance, volume_id):
    digest = hashlib.sha256(str(volume_id).encode('utf-8')).hexdigest()
    return os.path.join(_volume_journal_directory(instance), digest + '.json')


def _managed_detach_intent_path(instance, volume_id):
    digest = hashlib.sha256(str(volume_id).encode('utf-8')).hexdigest()
    return os.path.join(
        _volume_journal_directory(instance), digest + '.detach-intent')


def _managed_attach_intent_path(instance, volume_id):
    digest = hashlib.sha256(str(volume_id).encode('utf-8')).hexdigest()
    return os.path.join(
        _volume_journal_directory(instance), digest + '.attach-intent')


def _cold_attachment_rotation_path(instance, volume_id):
    digest = hashlib.sha256(str(volume_id).encode('utf-8')).hexdigest()
    return os.path.join(
        _volume_journal_directory(instance), digest + '.attachment-rotation')


def _fsync_directory(path):
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _prune_orphan_volume_recovery_directory(instance):
    """Remove only an empty or provably stale temporary journal directory.

    The caller serializes this with the instance volume-topology lock.  A
    recent temporary file may still belong to a writer that has not reached
    ``os.replace`` yet, so only strictly named regular files older than the
    conservative stale threshold are eligible.  Any durable evidence or
    unknown entry keeps destroy fail-closed.
    """
    journal_dir = _volume_journal_directory(instance)
    try:
        names = os.listdir(journal_dir)
    except FileNotFoundError:
        return True

    cutoff = time.time() - _VOLUME_RECOVERY_TMP_STALE_SECONDS
    stale_temporary = []
    for name in names:
        if _VOLUME_RECOVERY_TMP_RE.fullmatch(name) is None:
            return False
        path = os.path.join(journal_dir, name)
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            continue
        if (os.path.islink(path) or not os.path.isfile(path) or
                metadata.st_mtime > cutoff):
            return False
        stale_temporary.append(path)

    for path in stale_temporary:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    if stale_temporary:
        _fsync_directory(journal_dir)
    try:
        os.rmdir(journal_dir)
    except FileNotFoundError:
        return True
    except OSError as exc:
        if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
            return False
        raise
    _fsync_directory(os.path.dirname(journal_dir))
    return True


def _spawn_attempt_journal_directory():
    return os.path.join(CONF.instances_path, 'incus-spawn-attempts')


def _spawn_attempt_journal_path(instance):
    return os.path.join(_spawn_attempt_journal_directory(), instance.uuid)


def _spawn_attempt_payload(
        instance, compute_id, attempt_id, phase='preflight', generation=None):
    if phase not in _SPAWN_ATTEMPT_PHASES:
        raise incus_idmap.IDMapIntegrityError(
            'Incus spawn attempt has an invalid phase')
    if generation is None:
        allocation_id = None
        base = None
        size = None
        slot = None
        fingerprint = None
    else:
        getter = (
            generation.get if isinstance(generation, dict)
            else lambda key: getattr(generation, key))
        allocation_id = _canonical_materialization_id(
            getter('allocation_id'), 'allocation ID')
        base = int(getter('base'))
        size = int(getter('size'))
        slot = int(getter('slot'))
        fingerprint = str(getter('fingerprint'))
        if (base < 0 or size <= 0 or slot < 0 or
                not re.fullmatch(r'[0-9a-f]{64}', fingerprint)):
            raise incus_idmap.IDMapIntegrityError(
                'Incus spawn attempt has an invalid allocation generation')
    return {
        'version': _SPAWN_ATTEMPT_JOURNAL_VERSION,
        'phase': phase,
        'instance_uuid': _canonical_materialization_id(
            instance.uuid, 'instance UUID'),
        'instance_name': instance.name,
        'compute_uuid': _canonical_materialization_id(
            compute_id, 'compute UUID'),
        'attempt_uuid': _canonical_materialization_id(
            attempt_id, 'spawn attempt UUID'),
        'allocation_id': allocation_id,
        'base': base,
        'size': size,
        'slot': slot,
        'fingerprint': fingerprint,
    }


def _validate_spawn_attempt_payload(instance, payload):
    required = {
        'version', 'phase', 'instance_uuid', 'instance_name',
        'compute_uuid', 'attempt_uuid', 'allocation_id', 'base', 'size',
        'slot', 'fingerprint',
    }
    try:
        if not isinstance(payload, dict) or set(payload) != required:
            raise ValueError('invalid schema')
        generation_values = (
            payload['allocation_id'], payload['base'], payload['size'],
            payload['slot'], payload['fingerprint'])
        generation = None
        if any(value is not None for value in generation_values):
            if any(value is None for value in generation_values):
                raise ValueError('incomplete allocation generation')
            generation = payload
        expected = _spawn_attempt_payload(
            instance, payload['compute_uuid'], payload['attempt_uuid'],
            phase=payload['phase'], generation=generation)
    except (AttributeError, TypeError, ValueError,
            incus_idmap.IDMapError) as exc:
        raise incus_idmap.IDMapIntegrityError(
            'Host Incus spawn attempt journal is invalid') from exc
    if payload != expected:
        raise incus_idmap.IDMapIntegrityError(
            'Host Incus spawn attempt journal ownership is invalid')
    return payload


def _read_spawn_attempt_journal(instance):
    try:
        with open(
                _spawn_attempt_journal_path(instance),
                encoding='utf-8') as stream:
            payload = json.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise incus_idmap.IDMapIntegrityError(
            'Host Incus spawn attempt journal is unreadable') from exc
    return _validate_spawn_attempt_payload(instance, payload)


def _spawn_attempt_has_generation(attempt):
    return attempt.get('allocation_id') is not None


def _spawn_attempt_generation_matches(attempt, generation):
    return (
        _spawn_attempt_has_generation(attempt) and
        attempt['allocation_id'] == generation.allocation_id and
        attempt['base'] == generation.base and
        attempt['size'] == generation.size and
        attempt['slot'] == generation.slot and
        attempt['fingerprint'] == generation.fingerprint)


def _write_spawn_attempt_journal(
        instance, compute_id, attempt_id, phase, expected_phase=None,
        generation=None):
    """Atomically persist the side-effect boundary for one spawn attempt."""
    journal_dir = _spawn_attempt_journal_directory()
    directory_created = not os.path.isdir(journal_dir)
    os.makedirs(journal_dir, mode=0o700, exist_ok=True)
    os.chmod(journal_dir, 0o700)
    existing = _read_spawn_attempt_journal(instance)
    if expected_phase is None:
        if existing is not None:
            raise incus_idmap.IDMapConflict(
                reason='Another durable Incus spawn attempt exists')
    else:
        expected = _spawn_attempt_payload(
            instance, compute_id, attempt_id, phase=expected_phase,
            generation=generation)
        if existing != expected:
            raise incus_idmap.IDMapIntegrityError(
                'Incus spawn attempt changed before its phase transition')

    payload = _spawn_attempt_payload(
        instance, compute_id, attempt_id, phase=phase,
        generation=generation)
    fd, temporary = tempfile.mkstemp(
        prefix='.spawn-', suffix='.tmp', dir=journal_dir)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            fd = None
            json.dump(payload, stream, sort_keys=True, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, _spawn_attempt_journal_path(instance))
        _fsync_directory(journal_dir)
        if directory_created:
            _fsync_directory(os.path.dirname(journal_dir))
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return payload


def _remove_spawn_attempt_journal(instance, expected):
    current = _read_spawn_attempt_journal(instance)
    if current is None:
        return False
    if current != expected:
        raise incus_idmap.IDMapIntegrityError(
            'Incus spawn attempt changed before durable cleanup')
    os.unlink(_spawn_attempt_journal_path(instance))
    _fsync_directory(_spawn_attempt_journal_directory())
    return True


def _write_volume_journal(
        instance, volume_id, connection_info, device_info, mountpoint, phase):
    """Atomically persist host connector ownership outside the Incus DB."""
    journal_dir = _volume_journal_directory(instance)
    journal_root = os.path.dirname(journal_dir)
    root_created = not os.path.isdir(journal_root)
    instance_created = not os.path.isdir(journal_dir)
    os.makedirs(journal_dir, mode=0o700, exist_ok=True)
    os.chmod(journal_root, 0o700)
    os.chmod(journal_dir, 0o700)
    payload = {
        'version': _VOLUME_JOURNAL_VERSION,
        'instance_uuid': instance.uuid,
        'instance_name': instance.name,
        'volume_id': str(volume_id),
        'attachment': jsonutils.loads(_serialize_volume_attachment(
            connection_info, device_info, mountpoint, phase=phase)),
    }
    fd, temporary = tempfile.mkstemp(
        prefix='.volume-', suffix='.tmp', dir=journal_dir)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            fd = None
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, _volume_journal_path(instance, volume_id))
        _fsync_directory(journal_dir)
        if instance_created:
            _fsync_directory(journal_root)
        if root_created:
            _fsync_directory(os.path.dirname(journal_root))
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _write_managed_detach_intent(
        instance, volume_id, attachment_id, destroy_bdm, mountpoint):
    """Persist Nova manager authority to finish one external detach."""
    if (not uuidutils.is_uuid_like(volume_id) or
            not uuidutils.is_uuid_like(attachment_id) or
            not isinstance(destroy_bdm, bool) or
            not isinstance(mountpoint, str) or not mountpoint):
        raise exception.InvalidVolume(
            reason='Nova managed detach intent is incomplete')
    if _read_managed_attach_intent(instance, volume_id) is not None:
        raise exception.InvalidVolume(
            reason='A Nova managed attach transaction is still active')
    payload = {
        'version': _MANAGED_DETACH_INTENT_VERSION,
        'instance_uuid': instance.uuid,
        'instance_name': instance.name,
        'volume_id': str(volume_id),
        'attachment_id': str(attachment_id),
        'destroy_bdm': destroy_bdm,
        'mountpoint': mountpoint,
    }
    existing = _read_managed_detach_intent(instance, volume_id)
    if existing is not None:
        if existing != payload:
            raise exception.InvalidVolume(
                reason='Another Nova managed detach intent already exists')
        return existing

    journal_dir = _volume_journal_directory(instance)
    journal_root = os.path.dirname(journal_dir)
    root_created = not os.path.isdir(journal_root)
    instance_created = not os.path.isdir(journal_dir)
    os.makedirs(journal_dir, mode=0o700, exist_ok=True)
    os.chmod(journal_root, 0o700)
    os.chmod(journal_dir, 0o700)
    fd, temporary = tempfile.mkstemp(
        prefix='.detach-', suffix='.tmp', dir=journal_dir)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            fd = None
            json.dump(payload, stream, sort_keys=True, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            temporary, _managed_detach_intent_path(instance, volume_id))
        _fsync_directory(journal_dir)
        if instance_created:
            _fsync_directory(journal_root)
        if root_created:
            _fsync_directory(os.path.dirname(journal_root))
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return payload


def _read_managed_detach_intent(instance, volume_id):
    try:
        with open(
                _managed_detach_intent_path(instance, volume_id),
                encoding='utf-8') as stream:
            payload = json.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise exception.InvalidVolume(
            reason='Nova managed detach intent is unreadable: %s' % exc)
    if (not isinstance(payload, dict) or
            payload.get('version') != _MANAGED_DETACH_INTENT_VERSION or
            payload.get('instance_uuid') != instance.uuid or
            payload.get('instance_name') != instance.name or
            payload.get('volume_id') != str(volume_id) or
            not uuidutils.is_uuid_like(payload.get('attachment_id')) or
            not isinstance(payload.get('destroy_bdm'), bool) or
            not isinstance(payload.get('mountpoint'), str) or
            not payload.get('mountpoint')):
        raise exception.InvalidVolume(
            reason='Nova managed detach intent ownership is invalid')
    return payload


def _remove_managed_detach_intent(instance, volume_id, expected=None):
    current = _read_managed_detach_intent(instance, volume_id)
    if current is None:
        return
    if expected is not None and current != expected:
        raise exception.InvalidVolume(
            reason='Nova managed detach intent changed during cleanup')
    journal_dir = _volume_journal_directory(instance)
    os.unlink(_managed_detach_intent_path(instance, volume_id))
    _fsync_directory(journal_dir)
    try:
        os.rmdir(journal_dir)
    except OSError as exc:
        if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
            raise
    else:
        _fsync_directory(os.path.dirname(journal_dir))


def _validate_managed_attach_operation(
        operation_kind, operation_token, operation_direction,
        operation_migration_uuid=None, boot_volume=False):
    if not isinstance(boot_volume, bool):
        raise exception.InvalidVolume(
            reason='Nova managed attach boot-volume marker is invalid')
    if operation_kind not in _VOLUME_ATTACH_OPERATION_KINDS:
        raise exception.InvalidVolume(
            reason='Nova managed attach operation kind is invalid')
    if (boot_volume and
            (operation_kind != 'migration' or
             operation_direction not in (
                 'cold-source-restore', 'cold-revert-source',
                 'live-source-release'))):
        raise exception.InvalidVolume(
            reason='A boot-volume owner is valid only on a migration source')
    if operation_kind == 'hot-attach':
        if (operation_token is not None or operation_direction is not None or
                operation_migration_uuid is not None):
            raise exception.InvalidVolume(
                reason='Nova hot attach must not carry an internal owner')
        return
    if not uuidutils.is_uuid_like(operation_token):
        raise exception.InvalidVolume(
            reason='Nova internal attach has no durable operation token')
    expected_direction = {
        'spawn': 'materialize',
        'reconcile': 'power-reconcile',
    }.get(operation_kind)
    if expected_direction is not None:
        if (operation_direction != expected_direction or
                operation_migration_uuid is not None):
            raise exception.InvalidVolume(
                reason='Nova internal attach direction is invalid')
        return
    if operation_direction not in _VOLUME_ATTACH_MIGRATION_DIRECTIONS:
        raise exception.InvalidVolume(
            reason='Nova migration attach direction is invalid')
    if not uuidutils.is_uuid_like(operation_migration_uuid):
        raise exception.InvalidVolume(
            reason='Nova migration attach has no exact Migration UUID')


def _write_managed_attach_intent(
        instance, volume_id, attachment_id, mountpoint,
        operation_kind='hot-attach', operation_token=None,
        operation_direction=None, operation_migration_uuid=None,
        boot_volume=False):
    """Persist the exact Cinder identity before ComputeManager attach."""
    if (not uuidutils.is_uuid_like(volume_id) or
            not uuidutils.is_uuid_like(attachment_id) or
            not isinstance(mountpoint, str) or not mountpoint):
        raise exception.InvalidVolume(
            reason='Nova managed attach intent is incomplete')
    _validate_managed_attach_operation(
        operation_kind, operation_token, operation_direction,
        operation_migration_uuid, boot_volume)
    if _read_managed_detach_intent(instance, volume_id) is not None:
        raise exception.InvalidVolume(
            reason='A Nova managed detach transaction is still active')
    payload = {
        'version': _MANAGED_ATTACH_INTENT_VERSION,
        'instance_uuid': instance.uuid,
        'instance_name': instance.name,
        'volume_id': str(volume_id),
        'attachment_id': str(attachment_id),
        'mountpoint': mountpoint,
        'operation_kind': operation_kind,
        'operation_token': operation_token,
        'operation_direction': operation_direction,
        'operation_migration_uuid': operation_migration_uuid,
        'boot_volume': boot_volume,
    }
    existing = _read_managed_attach_intent(instance, volume_id)
    if existing is not None:
        if existing != payload:
            raise exception.InvalidVolume(
                reason='Another Nova managed attach intent already exists')
        return existing
    journal_dir = _volume_journal_directory(instance)
    journal_root = os.path.dirname(journal_dir)
    root_created = not os.path.isdir(journal_root)
    instance_created = not os.path.isdir(journal_dir)
    os.makedirs(journal_dir, mode=0o700, exist_ok=True)
    os.chmod(journal_root, 0o700)
    os.chmod(journal_dir, 0o700)
    fd, temporary = tempfile.mkstemp(
        prefix='.attach-', suffix='.tmp', dir=journal_dir)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            fd = None
            json.dump(payload, stream, sort_keys=True, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            temporary, _managed_attach_intent_path(instance, volume_id))
        _fsync_directory(journal_dir)
        if instance_created:
            _fsync_directory(journal_root)
        if root_created:
            _fsync_directory(os.path.dirname(journal_root))
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return payload


def _read_managed_attach_intent(instance, volume_id):
    try:
        with open(
                _managed_attach_intent_path(instance, volume_id),
                encoding='utf-8') as stream:
            payload = json.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise exception.InvalidVolume(
            reason='Nova managed attach intent is unreadable: %s' % exc)
    required = {
        'version', 'instance_uuid', 'instance_name', 'volume_id',
        'attachment_id', 'mountpoint', 'operation_kind', 'operation_token',
        'operation_direction', 'operation_migration_uuid', 'boot_volume',
    }
    if (not isinstance(payload, dict) or set(payload) != required or
            payload.get('version') != _MANAGED_ATTACH_INTENT_VERSION or
            payload.get('instance_uuid') != instance.uuid or
            payload.get('instance_name') != instance.name or
            payload.get('volume_id') != str(volume_id) or
            not uuidutils.is_uuid_like(payload.get('attachment_id')) or
            not isinstance(payload.get('mountpoint'), str) or
            not payload.get('mountpoint')):
        raise exception.InvalidVolume(
            reason='Nova managed attach intent ownership is invalid')
    try:
        _validate_managed_attach_operation(
            payload.get('operation_kind'), payload.get('operation_token'),
            payload.get('operation_direction'),
            payload.get('operation_migration_uuid'),
            payload.get('boot_volume'))
    except exception.InvalidVolume as exc:
        raise exception.InvalidVolume(
            reason='Nova managed attach intent ownership is invalid') from exc
    return payload


def _replace_managed_attach_intent(
        instance, volume_id, expected, attachment_id,
        operation_direction=None):
    current = _read_managed_attach_intent(instance, volume_id)
    if current != expected:
        raise exception.InvalidVolume(
            reason='Nova managed attach intent changed before replacement')
    if (current.get('operation_kind') != 'migration' or
            current.get('operation_direction') != 'cold-source-restore' or
            not uuidutils.is_uuid_like(attachment_id)):
        raise exception.InvalidVolume(
            reason='Nova managed attach intent replacement is invalid')
    payload = copy.deepcopy(current)
    payload['attachment_id'] = str(attachment_id)
    if operation_direction is not None:
        _validate_managed_attach_operation(
            payload['operation_kind'], payload['operation_token'],
            operation_direction, payload['operation_migration_uuid'],
            payload['boot_volume'])
        payload['operation_direction'] = operation_direction
    journal_dir = _volume_journal_directory(instance)
    fd, temporary = tempfile.mkstemp(
        prefix='.attach-', suffix='.tmp', dir=journal_dir)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            fd = None
            json.dump(payload, stream, sort_keys=True, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            temporary, _managed_attach_intent_path(instance, volume_id))
        _fsync_directory(journal_dir)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return payload


def _remove_managed_attach_intent(instance, volume_id, expected=None):
    current = _read_managed_attach_intent(instance, volume_id)
    if current is None:
        return
    if expected is not None and current != expected:
        raise exception.InvalidVolume(
            reason='Nova managed attach intent changed during cleanup')
    journal_dir = _volume_journal_directory(instance)
    os.unlink(_managed_attach_intent_path(instance, volume_id))
    _fsync_directory(journal_dir)
    try:
        os.rmdir(journal_dir)
    except OSError as exc:
        if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
            raise
    else:
        _fsync_directory(os.path.dirname(journal_dir))


def _validate_cold_attachment_rotation(instance, volume_id, payload):
    required = {
        'version', 'instance_uuid', 'instance_name', 'volume_id',
        'mountpoint', 'operation_token', 'migration_uuid',
        'old_attachment_id', 'new_attachment_id',
        'baseline_attachment_ids', 'phase', 'boot_volume',
    }
    baseline = payload.get('baseline_attachment_ids') if isinstance(
        payload, dict) else None
    if (not isinstance(payload, dict) or set(payload) != required or
            payload.get('version') != _COLD_ATTACHMENT_ROTATION_VERSION or
            payload.get('instance_uuid') != instance.uuid or
            payload.get('instance_name') != instance.name or
            payload.get('volume_id') != str(volume_id) or
            not isinstance(payload.get('mountpoint'), str) or
            not payload.get('mountpoint') or
            not uuidutils.is_uuid_like(payload.get('operation_token')) or
            not uuidutils.is_uuid_like(payload.get('migration_uuid')) or
            not uuidutils.is_uuid_like(payload.get('old_attachment_id')) or
            (payload.get('new_attachment_id') is not None and
             not uuidutils.is_uuid_like(payload.get('new_attachment_id'))) or
            not isinstance(baseline, list) or
            baseline != sorted(set(baseline)) or
            any(not uuidutils.is_uuid_like(value) for value in baseline) or
            payload.get('phase') not in _COLD_ATTACHMENT_ROTATION_PHASES or
            not isinstance(payload.get('boot_volume'), bool)):
        raise exception.InvalidVolume(
            reason='Cold migration attachment rotation ownership is invalid')
    if (payload['phase'] in ('prepared', 'creating',
                             'source-rollback-complete') and
            payload['new_attachment_id'] is not None):
        if payload['phase'] != 'source-rollback-complete':
            raise exception.InvalidVolume(
                reason='Uncreated attachment rotation names a replacement')
    if (payload['phase'] not in (
            'prepared', 'creating', 'source-rollback-complete') and
            payload['new_attachment_id'] is None):
        raise exception.InvalidVolume(
            reason='Attachment rotation has no durable replacement identity')
    if payload['old_attachment_id'] not in baseline:
        raise exception.InvalidVolume(
            reason='Attachment rotation baseline omits its old owner')
    return payload


def _read_cold_attachment_rotation(instance, volume_id):
    try:
        with open(
                _cold_attachment_rotation_path(instance, volume_id),
                encoding='utf-8') as stream:
            payload = json.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise exception.InvalidVolume(
            reason='Cold migration attachment rotation is unreadable: %s' %
                   exc)
    return _validate_cold_attachment_rotation(instance, volume_id, payload)


def _write_cold_attachment_rotation(
        instance, volume_id, payload, expected=None):
    payload = _validate_cold_attachment_rotation(
        instance, volume_id, copy.deepcopy(payload))
    current = _read_cold_attachment_rotation(instance, volume_id)
    if expected is None:
        if current is not None:
            if current != payload:
                raise exception.InvalidVolume(
                    reason='Another attachment rotation already exists')
            return current, False
    elif current != expected:
        raise exception.InvalidVolume(
            reason='Attachment rotation changed before durable transition')

    journal_dir = _volume_journal_directory(instance)
    journal_root = os.path.dirname(journal_dir)
    root_created = not os.path.isdir(journal_root)
    instance_created = not os.path.isdir(journal_dir)
    os.makedirs(journal_dir, mode=0o700, exist_ok=True)
    os.chmod(journal_root, 0o700)
    os.chmod(journal_dir, 0o700)
    fd, temporary = tempfile.mkstemp(
        prefix='.rotation-', suffix='.tmp', dir=journal_dir)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            fd = None
            json.dump(payload, stream, sort_keys=True, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            temporary, _cold_attachment_rotation_path(instance, volume_id))
        _fsync_directory(journal_dir)
        if instance_created:
            _fsync_directory(journal_root)
        if root_created:
            _fsync_directory(os.path.dirname(journal_root))
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return payload, True


def _remove_cold_attachment_rotation(instance, volume_id, expected):
    current = _read_cold_attachment_rotation(instance, volume_id)
    if current is None:
        return
    if current != expected:
        raise exception.InvalidVolume(
            reason='Attachment rotation changed during cleanup')
    journal_dir = _volume_journal_directory(instance)
    os.unlink(_cold_attachment_rotation_path(instance, volume_id))
    _fsync_directory(journal_dir)
    try:
        os.rmdir(journal_dir)
    except OSError as exc:
        if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
            raise
    else:
        _fsync_directory(os.path.dirname(journal_dir))


def _read_volume_journal(instance, volume_id):
    path = _volume_journal_path(instance, volume_id)
    try:
        with open(path, encoding='utf-8') as stream:
            payload = json.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise exception.InvalidVolume(
            reason='Host Cinder cleanup journal is unreadable: %s' % exc)
    if (not isinstance(payload, dict) or
            payload.get('version') != _VOLUME_JOURNAL_VERSION or
            payload.get('instance_uuid') != instance.uuid or
            payload.get('instance_name') != instance.name or
            payload.get('volume_id') != str(volume_id) or
            not isinstance(payload.get('attachment'), dict)):
        raise exception.InvalidVolume(
            reason='Host Cinder cleanup journal ownership is invalid')
    return payload['attachment']


def _volume_journal_records(instance):
    journal_dir = _volume_journal_directory(instance)
    try:
        names = os.listdir(journal_dir)
    except FileNotFoundError:
        return {}
    records = {}
    for name in names:
        if not name.endswith('.json'):
            continue
        path = os.path.join(journal_dir, name)
        try:
            with open(path, encoding='utf-8') as stream:
                payload = json.load(stream)
        except (OSError, ValueError) as exc:
            raise exception.InvalidVolume(
                reason='Host Cinder cleanup journal is unreadable: %s' % exc)
        if (not isinstance(payload, dict) or
                payload.get('version') != _VOLUME_JOURNAL_VERSION or
                payload.get('instance_uuid') != instance.uuid or
                payload.get('instance_name') != instance.name or
                not isinstance(payload.get('volume_id'), str) or
                not isinstance(payload.get('attachment'), dict) or
                os.path.basename(_volume_journal_path(
                    instance, payload['volume_id'])) != name):
            raise exception.InvalidVolume(
                reason='Host Cinder cleanup journal ownership is invalid')
        records[payload['volume_id']] = payload['attachment']
    return records


def _volume_journal_records_by_uuid(instance_uuid):
    """Read journals for an instance whose Nova object may not be loadable.

    Recovery enumerates the journal tree before it knows which instances
    still exist, so it cannot build the instance object the ownership check
    in _volume_journal_records() needs. Every record still has to name this
    exact instance UUID and hash to its own filename; anything else is
    reported so the caller keeps the journal instead of acting on it.
    """
    journal_dir = os.path.join(
        CONF.instances_path, 'incus-volume-journal', instance_uuid)
    try:
        names = os.listdir(journal_dir)
    except FileNotFoundError:
        return {}
    records = {}
    for name in names:
        if not name.endswith('.json'):
            continue
        try:
            with open(os.path.join(journal_dir, name), encoding='utf-8') as f:
                payload = json.load(f)
        except (OSError, ValueError) as exc:
            raise exception.InvalidVolume(
                reason='Host Cinder cleanup journal is unreadable: %s' % exc)
        volume_id = payload.get('volume_id') if isinstance(
            payload, dict) else None
        if (not isinstance(payload, dict) or
                payload.get('version') != _VOLUME_JOURNAL_VERSION or
                payload.get('instance_uuid') != instance_uuid or
                not isinstance(volume_id, str) or
                not isinstance(payload.get('attachment'), dict) or
                hashlib.sha256(
                    volume_id.encode('utf-8')).hexdigest() + '.json' != name):
            raise exception.InvalidVolume(
                reason='Host Cinder cleanup journal ownership is invalid')
        records[volume_id] = payload['attachment']
    return records


def _managed_attach_intents_by_uuid(instance_uuid):
    journal_dir = os.path.join(
        CONF.instances_path, 'incus-volume-journal', instance_uuid)
    try:
        names = os.listdir(journal_dir)
    except FileNotFoundError:
        return {}
    intents = {}
    for name in names:
        if not name.endswith('.attach-intent'):
            continue
        try:
            with open(os.path.join(journal_dir, name), encoding='utf-8') as f:
                payload = json.load(f)
        except (OSError, ValueError) as exc:
            raise exception.InvalidVolume(
                reason='Nova managed attach intent is unreadable: %s' % exc)
        volume_id = payload.get('volume_id') if isinstance(
            payload, dict) else None
        expected_name = (
            hashlib.sha256(str(volume_id).encode('utf-8')).hexdigest() +
            '.attach-intent')
        if (not isinstance(payload, dict) or
                payload.get('version') != _MANAGED_ATTACH_INTENT_VERSION or
                payload.get('instance_uuid') != instance_uuid or
                not uuidutils.is_uuid_like(volume_id) or
                not uuidutils.is_uuid_like(payload.get('attachment_id')) or
                not isinstance(payload.get('instance_name'), str) or
                not payload.get('instance_name') or
                not isinstance(payload.get('mountpoint'), str) or
                not payload.get('mountpoint') or
                not isinstance(payload.get('boot_volume'), bool) or
                name != expected_name):
            raise exception.InvalidVolume(
                reason='Nova managed attach intent ownership is invalid')
        intents[volume_id] = payload
    return intents


def _cold_attachment_rotations_by_uuid(instance_uuid):
    journal_dir = os.path.join(
        CONF.instances_path, 'incus-volume-journal', instance_uuid)
    try:
        names = os.listdir(journal_dir)
    except FileNotFoundError:
        return {}
    rotations = {}
    for name in names:
        if not name.endswith('.attachment-rotation'):
            continue
        try:
            with open(os.path.join(journal_dir, name), encoding='utf-8') as f:
                payload = json.load(f)
        except (OSError, ValueError) as exc:
            raise exception.InvalidVolume(
                reason='Cold attachment rotation is unreadable: %s' % exc)
        volume_id = payload.get('volume_id') if isinstance(
            payload, dict) else None
        expected_name = (
            hashlib.sha256(str(volume_id).encode('utf-8')).hexdigest() +
            '.attachment-rotation')
        if (not isinstance(payload, dict) or
                payload.get('version') !=
                _COLD_ATTACHMENT_ROTATION_VERSION or
                payload.get('instance_uuid') != instance_uuid or
                not uuidutils.is_uuid_like(volume_id) or
                not isinstance(payload.get('instance_name'), str) or
                not payload.get('instance_name') or name != expected_name):
            raise exception.InvalidVolume(
                reason='Cold attachment rotation ownership is invalid')
        rotations[volume_id] = payload
    return rotations


def _managed_detach_intents_by_uuid(instance_uuid):
    """Read manager detach generations without requiring an Instance."""
    journal_dir = os.path.join(
        CONF.instances_path, 'incus-volume-journal', instance_uuid)
    try:
        names = os.listdir(journal_dir)
    except FileNotFoundError:
        return {}
    intents = {}
    for name in names:
        if not name.endswith('.detach-intent'):
            continue
        try:
            with open(os.path.join(journal_dir, name), encoding='utf-8') as f:
                payload = json.load(f)
        except (OSError, ValueError) as exc:
            raise exception.InvalidVolume(
                reason='Nova managed detach intent is unreadable: %s' % exc)
        volume_id = payload.get('volume_id') if isinstance(
            payload, dict) else None
        expected_name = (
            hashlib.sha256(str(volume_id).encode('utf-8')).hexdigest() +
            '.detach-intent')
        if (not isinstance(payload, dict) or
                payload.get('version') != _MANAGED_DETACH_INTENT_VERSION or
                payload.get('instance_uuid') != instance_uuid or
                not uuidutils.is_uuid_like(volume_id) or
                not uuidutils.is_uuid_like(payload.get('attachment_id')) or
                not isinstance(payload.get('instance_name'), str) or
                not payload.get('instance_name') or
                not isinstance(payload.get('destroy_bdm'), bool) or
                not isinstance(payload.get('mountpoint'), str) or
                not payload.get('mountpoint') or name != expected_name):
            raise exception.InvalidVolume(
                reason='Nova managed detach intent ownership is invalid')
        intents[volume_id] = payload
    return intents


def _volume_recovery_phase(
        record, attach_intent, detach_intent, rotation=None):
    """Classify one durable volume transaction by its owning generation."""
    if attach_intent is not None and detach_intent is not None:
        return 'intent-conflict'
    if rotation is not None:
        return 'rotation-{}'.format(rotation.get('phase'))
    phase = record.get('phase') if record is not None else None
    if attach_intent is not None and phase in (
            'disconnecting', 'disconnected'):
        return 'attach-{}'.format(phase)
    if attach_intent is not None and phase is None:
        return 'attach-pending'
    if detach_intent is not None and phase is None:
        return 'detach-pending'
    return phase


def _remove_volume_journal(instance, volume_id):
    journal_dir = _volume_journal_directory(instance)
    try:
        os.unlink(_volume_journal_path(instance, volume_id))
    except FileNotFoundError:
        return
    _fsync_directory(journal_dir)
    try:
        os.rmdir(journal_dir)
    except OSError as exc:
        if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
            raise
    else:
        _fsync_directory(os.path.dirname(journal_dir))


def _publish_migration_target_volumes_complete(
        client, instance, operation_token, migration_uuid):
    """Publish target-local proof after every volume record is retired."""
    if (not uuidutils.is_uuid_like(operation_token) or
            not uuidutils.is_uuid_like(migration_uuid)):
        raise exception.MigrationError(
            reason='Incus migration target volume proof is invalid')

    def volume_evidence_absent():
        journal_dir = _volume_journal_directory(instance)
        try:
            entries = os.listdir(journal_dir)
        except FileNotFoundError:
            return True
        if entries:
            return False
        try:
            os.rmdir(journal_dir)
        except FileNotFoundError:
            return True
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                return False
            raise
        _fsync_directory(os.path.dirname(journal_dir))
        return True

    if not volume_evidence_absent():
        return False
    with lockutils.lock(_profile_lock_name(instance)):
        profile = client.profiles.get(instance.name)
        _validate_profile_volume_owner(profile, instance)
        config = profile.config if isinstance(profile.config, dict) else {}
        if (config.get(MIGRATION_CLEANUP_TOKEN_KEY) != operation_token or
                config.get(MIGRATION_NOVA_UUID_KEY) != migration_uuid):
            raise exception.MigrationError(
                reason='Incus migration target volume proof owner changed')
        # Recheck after acquiring the profile serialization fence. Migration
        # volume writers validate this same profile generation before they
        # can publish local evidence.
        if not volume_evidence_absent():
            return False
        existing = config.get(MIGRATION_TARGET_VOLUMES_COMPLETE_KEY)
        if existing not in (None, operation_token):
            raise exception.MigrationError(
                reason='Incus migration target volume proof changed')
        profile.config[MIGRATION_TARGET_VOLUMES_COMPLETE_KEY] = (
            operation_token)
        profile.save(wait=True)
    return True


def _profile_volume_record(profile, volume_id, device=None):
    """Decode current and legacy profile volume metadata."""
    encoded = next((
        profile.config[key]
        for key in _volume_device_info_keys(volume_id)
        if key in profile.config), None)
    if not encoded:
        return {
            'version': 0,
            'phase': 'connected',
            'driver_volume_type': None,
            'connection_data': {},
            'device_info': (
                {'path': device['source']} if device else None),
            'mountpoint': (device or {}).get('path'),
        }
    try:
        decoded = jsonutils.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise exception.InvalidVolume(
            reason='Stored os-brick volume metadata is invalid: %s' % exc)
    if not isinstance(decoded, dict):
        raise exception.InvalidVolume(
            reason='Stored os-brick volume metadata is not a mapping')
    if decoded.get('version') == _VOLUME_ATTACHMENT_RECORD_VERSION:
        device_info = decoded.get('device_info')
        connection_data = decoded.get('connection_data')
        if (decoded.get('phase') not in (
                'connecting', 'connected', 'disconnecting', 'disconnected') or
                not isinstance(device_info, dict) or
                not isinstance(connection_data, dict) or
                not isinstance(decoded.get('driver_volume_type'), str)):
            raise exception.InvalidVolume(
                reason='Stored os-brick attachment record is incomplete')
        return decoded
    if decoded.get('version') == 1:
        decoded['phase'] = 'connected'
        return decoded
    # Before record version 1, this value contained device_info directly.
    return {
        'version': 0,
        'phase': 'connected',
        'driver_volume_type': None,
        'connection_data': {},
        'device_info': decoded,
        'mountpoint': (device or {}).get('path'),
    }


def _profile_has_volume_connections(profile):
    """Return whether a profile retains an os-brick volume connection."""
    if not isinstance(profile.config, dict):
        return False
    if any(
            key.startswith(('user.openstack.volume.',
                            'user.openstack.volume_device_info.'))
            for key in profile.config):
        return True
    if not isinstance(profile.devices, dict):
        return False
    return any(
        device.get('type') == 'unix-block'
        for device in profile.devices.values())


def _serialize_device_info(device_info):
    if not isinstance(device_info, dict):
        raise exception.InvalidVolume(
            reason='os-brick device information must be a dictionary')
    try:
        return jsonutils.dumps(device_info, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise exception.InvalidVolume(
            reason='os-brick device information is not serializable: %s' %
            exc)


def _mapped_cinder_rbd_devices():
    """Return Cinder RBD symlinks indexed by image name.

    A host-wide volume-usage poll can include thousands of BFV roots. Building
    this index once avoids scanning every RBD pool directory for each BDM.
    The selected path is still resolved and validated by
    ``_disk_metric_device`` before it is trusted.
    """
    devices = {}
    for path in glob.glob('/dev/rbd/*/volume-*'):
        image_name = os.path.basename(path)
        if not _CINDER_RBD_IMAGE_RE.fullmatch(image_name):
            continue
        devices.setdefault(image_name, []).append(path)
    return devices


def _disk_metric_device(profile, instance, disk_id, rbd_devices=None):
    """Map a Nova disk ID to the host block name used by Incus metrics."""
    devices = (
        profile.get('devices', {}) if isinstance(profile, dict)
        else profile.devices)
    normalized = str(disk_id).removeprefix('/dev/')
    for device in devices.values():
        if device.get('type') != 'unix-block':
            continue
        if str(device.get('path', '')).removeprefix('/dev/') != normalized:
            continue
        source = os.path.realpath(device.get('source', ''))
        _validate_block_device_path(source, 'Incus volume metric source')
        return os.path.basename(source)

    root_device = str(
        getattr(instance, 'root_device_name', '') or '').removeprefix('/dev/')
    if normalized != root_device:
        return None

    root = devices.get('root', {})
    image_name = root.get('initial.ceph.rbd.image_name')
    if not image_name or not _CINDER_RBD_IMAGE_RE.fullmatch(image_name):
        return None
    candidates = (
        rbd_devices.get(image_name, [])
        if rbd_devices is not None
        else glob.glob('/dev/rbd/*/%s' % image_name)
    )
    if len(candidates) != 1:
        LOG.warning(
            'Expected one mapped RBD device for root volume %(image)s, '
            'found %(count)d',
            {'image': image_name, 'count': len(candidates)},
            instance=instance)
        return None
    source = os.path.realpath(candidates[0])
    _validate_block_device_path(source, 'Incus BFV metric source')
    return os.path.basename(source)


def _incus_all_disk_metrics(client):
    """Return cumulative block counters keyed by instance and host device."""
    response = client.api.metrics.get(is_api=False)
    wanted = {
        'incus_disk_read_bytes_total': 'rd_bytes',
        'incus_disk_reads_completed_total': 'rd_req',
        'incus_disk_written_bytes_total': 'wr_bytes',
        'incus_disk_writes_completed_total': 'wr_req',
    }
    metrics = {}
    for family in prometheus_parser.text_string_to_metric_families(
            response.text):
        for sample in family.samples:
            field = wanted.get(sample.name)
            if field is None:
                continue
            instance_name = sample.labels.get('name')
            device = sample.labels.get('device')
            if not instance_name or not device:
                continue
            metrics.setdefault(instance_name, {}).setdefault(
                device, {})[field] = int(sample.value)
    return metrics


def _incus_disk_metrics(client, instance_name):
    """Return cumulative block counters for one Incus instance."""
    return _incus_all_disk_metrics(client).get(instance_name, {})


def _nova_block_stats(counters):
    if not counters or set(counters) != {
            'rd_req', 'rd_bytes', 'wr_req', 'wr_bytes'}:
        return None
    return [
        counters['rd_req'],
        counters['rd_bytes'],
        counters['wr_req'],
        counters['wr_bytes'],
        0,
    ]


class _MigrationStateNotReady(Exception):
    """Signal that an asynchronous migration condition has not converged."""


class _MigrationConditionTimeout(exception.MigrationError):
    """An asynchronous migration condition did not converge in time."""


def _is_retryable_migration_exception(exc):
    """Return whether retrying a migration side effect is safe and useful."""
    if isinstance(exc, (OSError, TimeoutError, socket.timeout,
                        incus_exceptions.ClientConnectionFailed)):
        return True
    if not isinstance(exc, incus_exceptions.LXDAPIException):
        return False
    return (
        _is_incus_busy_operation(exc) or
        _incus_api_status_code(exc) in (408, 429, 500, 502, 503, 504))


def _retry_migration_finish_action(action, description, instance):
    """Retry a side effect only for explicit transient failures."""
    attempts = CONF.incus.migration_finish_retries
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except Exception as exc:
            if (attempt == attempts or
                    not _is_retryable_migration_exception(exc)):
                raise
            LOG.warning(
                'Incus migration target %(action)s failed on attempt '
                '%(attempt)s/%(attempts)s; retrying',
                {
                    'action': description,
                    'attempt': attempt,
                    'attempts': attempts,
                },
                instance=instance,
                exc_info=True)
            eventlet.sleep(CONF.incus.migration_finish_retry_interval)


def _wait_migration_finish_condition(action, description, instance):
    """Poll a read-only migration condition without retrying bad metadata."""
    attempts = CONF.incus.migration_finish_retries
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except _MigrationStateNotReady as exc:
            if attempt == attempts:
                raise _MigrationConditionTimeout(reason=str(exc)) from exc
        except Exception as exc:
            if (attempt == attempts or
                    not _is_retryable_migration_exception(exc)):
                raise
        LOG.debug(
            'Incus migration %(action)s is not ready on attempt '
            '%(attempt)s/%(attempts)s',
            {
                'action': description,
                'attempt': attempt,
                'attempts': attempts,
            },
            instance=instance)
        eventlet.sleep(CONF.incus.migration_finish_retry_interval)


def _incus_api_error_message(exc):
    response = getattr(exc, 'response', None)
    if response is None:
        return str(exc)

    try:
        body = response.json()
    except Exception:
        return str(exc)

    if isinstance(body, dict):
        metadata = body.get('metadata')
        if isinstance(metadata, dict):
            error = metadata.get('err')
            if error:
                return str(error)

        return str(body.get('error') or exc)

    return str(body or exc)


def _incus_api_status_code(exc):
    response = getattr(exc, 'response', None)
    status_code = getattr(response, 'status_code', None)
    if status_code is None or not 200 <= status_code < 300:
        return status_code

    try:
        body = response.json()
    except Exception:
        return status_code

    if not isinstance(body, dict):
        return status_code

    metadata = body.get('metadata')
    if not isinstance(metadata, dict):
        return status_code

    return metadata.get('status_code') or status_code


def _is_incus_not_found(exc):
    return (
        isinstance(exc, incus_exceptions.NotFound) or
        _incus_api_status_code(exc) == 404
    )


def _is_incus_busy_operation(exc):
    if _incus_api_status_code(exc) not in (400, 409):
        return False

    message = _incus_api_error_message(exc).lower()
    return (
        ('busy running' in message and 'operation' in message) or
        'operation is currently running' in message or
        'already has an operation' in message)


def _retry_incus_instance_action(
        action, description, instance, retry_transient=False):
    attempts = CONF.incus.migration_finish_retries
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except Exception as exc:
            retryable = (
                _is_retryable_migration_exception(exc)
                if retry_transient
                else (
                    isinstance(exc, incus_exceptions.LXDAPIException) and
                    _is_incus_busy_operation(exc)
                )
            )
            if not retryable or attempt == attempts:
                raise

            LOG.warning(
                'Incus instance %(action)s hit a transient failure on attempt '
                '%(attempt)s/%(attempts)s; retrying',
                {
                    'action': description,
                    'attempt': attempt,
                    'attempts': attempts,
                },
                instance=instance,
                exc_info=True)
            eventlet.sleep(CONF.incus.migration_finish_retry_interval)


def _configdrive_path(instance):
    return os.path.join(
        common.InstanceAttributes(instance).instance_dir, 'configdrive')


def _remove_instance_directory(instance):
    instance_dir = common.InstanceAttributes(instance).instance_dir
    if not os.path.exists(instance_dir):
        return
    privsep_path.chown(
        instance_dir, uid=os.getuid(), gid=os.getgid(), recursive=True)
    for root, dirs, files in os.walk(instance_dir, topdown=False):
        for name in files:
            os.chmod(os.path.join(root, name), 0o600)
        for name in dirs:
            os.chmod(os.path.join(root, name), 0o700)
        os.chmod(root, 0o700)
    shutil.rmtree(instance_dir)


def _share_mount_path(instance, share_mapping):
    instance_uuid = str(instance.uuid)
    share_id = str(share_mapping.share_id)
    if not _SHARE_ID_RE.fullmatch(instance_uuid):
        raise exception.ShareMountError(
            share_id=share_id,
            server_id=instance_uuid,
            reason='instance ID is not a canonical UUID')
    if not _SHARE_ID_RE.fullmatch(share_id):
        raise exception.ShareMountError(
            share_id=share_id,
            server_id=instance_uuid,
            reason='share ID is not a canonical UUID')
    return os.path.join(
        CONF.instances_path, 'incus-shares', instance_uuid, share_id)


def _validate_share_mount_path(
        mount_path, instance_uuid, share_id, error_cls):
    """Reject non-canonical or symlinked Manila staging paths."""
    instance_uuid = str(instance_uuid)
    share_id = str(share_id)
    expected = os.path.abspath(os.path.join(
        CONF.instances_path, 'incus-shares', instance_uuid, share_id))
    requested = os.path.abspath(mount_path)
    expected_real = os.path.join(
        os.path.realpath(os.path.abspath(CONF.instances_path)),
        'incus-shares', instance_uuid, share_id)
    reason = None
    if (not _SHARE_ID_RE.fullmatch(instance_uuid) or
            not _SHARE_ID_RE.fullmatch(share_id)):
        reason = 'staging path does not contain canonical UUIDs'
    elif requested != expected:
        reason = 'staging path is not canonical'
    elif os.path.realpath(requested) != expected_real:
        reason = 'staging path contains a symbolic-link escape'
    else:
        current = os.path.join(
            os.path.abspath(CONF.instances_path), 'incus-shares')
        for component in (instance_uuid, share_id):
            if os.path.lexists(current) and os.path.islink(current):
                reason = 'staging path contains a symbolic link'
                break
            current = os.path.join(current, component)
        if reason is None and os.path.lexists(current) and os.path.islink(
                current):
            reason = 'staging path contains a symbolic link'
    if reason is not None:
        raise error_cls(
            share_id=share_id, server_id=instance_uuid, reason=reason)
    return expected_real


def _share_journal_directory(instance):
    return os.path.join(
        CONF.instances_path, 'incus-share-journal', instance.uuid)


def _share_journal_path(instance, share_id):
    if not _SHARE_ID_RE.fullmatch(str(share_id)):
        raise exception.ShareMountError(
            share_id=share_id,
            server_id=instance.uuid,
            reason='share ID is not a canonical UUID')
    return os.path.join(
        _share_journal_directory(instance), '%s.json' % share_id)


def _share_mapping_owner_token(instance, share_mapping):
    """Return a stable owner for ordinary attach and restart retries."""
    identity = getattr(share_mapping, 'id', None)
    material = 'mapping:%s:%s:%s' % (
        instance.uuid, share_mapping.share_id, identity)
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def _share_journal_payload(
        instance, share_mapping, operation_token, phase):
    if not isinstance(operation_token, str) or not operation_token:
        raise exception.ShareMountError(
            share_id=share_mapping.share_id,
            server_id=instance.uuid,
            reason='share staging owner token is missing')
    identity = {
        'instance_uuid': instance.uuid,
        'instance_name': instance.name,
        'share_id': share_mapping.share_id,
        'share_proto': share_mapping.share_proto,
        'export_location': share_mapping.export_location,
        'tag': share_mapping.tag,
    }
    if any(not isinstance(value, str) or not value
           for value in identity.values()):
        raise exception.ShareMountError(
            share_id=getattr(share_mapping, 'share_id', 'unknown'),
            server_id=getattr(instance, 'uuid', 'unknown'),
            reason='share staging identity contains an invalid value')
    # access_key is deliberately absent. In particular, a CephFS secret must
    # remain in memory and in the short-lived 0600 secretfile only. The
    # non-secret CephX client name is required to validate a mount exactly
    # during journal replay after the Nova ShareMapping is no longer present.
    payload = {
        'version': _SHARE_JOURNAL_VERSION,
        'instance_uuid': identity['instance_uuid'],
        'instance_name': identity['instance_name'],
        'share_id': identity['share_id'],
        'operation_token': operation_token,
        'phase': phase,
        'share_proto': identity['share_proto'],
        'export_location': identity['export_location'],
        'tag': identity['tag'],
    }
    if (share_mapping.share_proto ==
            obj_fields.ShareMappingProto.CEPHFS):
        payload['access_to'] = _loaded_share_access_to(share_mapping)
    return payload


def _write_share_journal(
        instance, share_mapping, operation_token, phase):
    """Atomically persist host-side Manila mount ownership.

    The caller holds the per-instance/share operation lock. An existing
    record is a compare-and-set guard: only its phase may change. Ownership
    and the complete share binding are immutable until the record is removed.
    """
    journal_dir = _share_journal_directory(instance)
    journal_root = os.path.dirname(journal_dir)
    root_created = not os.path.isdir(journal_root)
    instance_created = not os.path.isdir(journal_dir)
    os.makedirs(journal_dir, mode=0o700, exist_ok=True)
    os.chmod(journal_root, 0o700)
    os.chmod(journal_dir, 0o700)
    payload = _share_journal_payload(
        instance, share_mapping, operation_token, phase)
    _validate_share_journal_payload(
        instance, payload, share_mapping=share_mapping,
        operation_token=operation_token)
    journal_path = _share_journal_path(instance, share_mapping.share_id)
    try:
        with open(journal_path, encoding='utf-8') as stream:
            current = jsonutils.loads(stream.read())
    except FileNotFoundError:
        current = None
    except (OSError, ValueError) as exc:
        raise exception.ShareMountError(
            share_id=share_mapping.share_id,
            server_id=instance.uuid,
            reason='host Manila staging journal is unreadable: %s' % exc)
    if current is not None:
        _validate_share_journal_payload(
            instance, current, share_mapping=share_mapping,
            operation_token=operation_token)
    fd, temporary = tempfile.mkstemp(
        prefix='.share-', suffix='.tmp', dir=journal_dir)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            fd = None
            jsonutils.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, journal_path)
        _fsync_directory(journal_dir)
        if instance_created:
            _fsync_directory(journal_root)
        if root_created:
            _fsync_directory(os.path.dirname(journal_root))
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _validate_share_journal_payload(
        instance, payload, share_mapping=None, operation_token=None):
    share_id = (
        share_mapping.share_id if share_mapping is not None
        else payload.get('share_id'))
    version = payload.get('version') if isinstance(payload, dict) else None
    valid = (
        isinstance(payload, dict) and
        version in (1, _SHARE_JOURNAL_VERSION) and
        payload.get('instance_uuid') == instance.uuid and
        payload.get('instance_name') == instance.name and
        payload.get('share_id') == share_id and
        isinstance(payload.get('operation_token'), str) and
        bool(payload.get('operation_token')) and
        payload.get('phase') in ('staging', 'mounted', 'unmounting') and
        isinstance(payload.get('share_proto'), str) and
        isinstance(payload.get('export_location'), str) and
        isinstance(payload.get('tag'), str) and
        'access_key' not in payload)
    if (valid and payload.get('share_proto') ==
            obj_fields.ShareMappingProto.CEPHFS):
        valid = (
            version == 1 or
            (isinstance(payload.get('access_to'), str) and
             bool(_CEPHFS_NAME_RE.fullmatch(payload['access_to']))))
    if share_mapping is not None:
        expected_access_to = _loaded_share_access_to(share_mapping)
        valid = valid and (
            payload.get('share_proto') == share_mapping.share_proto and
            payload.get('export_location') ==
            share_mapping.export_location and
            payload.get('tag') == share_mapping.tag and
            (version == 1 or
             payload.get('share_proto') !=
             obj_fields.ShareMappingProto.CEPHFS or
             expected_access_to is None or
             payload.get('access_to') == expected_access_to))
    if operation_token is not None:
        valid = valid and (
            payload.get('operation_token') == operation_token)
    if not valid:
        raise exception.ShareMountError(
            share_id=share_id or 'unknown',
            server_id=instance.uuid,
            reason='host Manila staging journal ownership is invalid')
    return payload


def _loaded_share_access_to(share_mapping):
    """Return a loaded CephX client name without triggering OVO loading."""
    attr_is_set = getattr(share_mapping, 'obj_attr_is_set', None)
    if callable(attr_is_set):
        try:
            loaded = attr_is_set('access_to')
        except Exception:
            loaded = None
        if loaded is False:
            return None
    try:
        access_to = share_mapping.access_to
    except Exception:
        return None
    return access_to if isinstance(access_to, str) else None


def _read_share_journal(
        instance, share_mapping, operation_token=None):
    path = _share_journal_path(instance, share_mapping.share_id)
    try:
        with open(path, encoding='utf-8') as stream:
            payload = json.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise exception.ShareMountError(
            share_id=share_mapping.share_id,
            server_id=instance.uuid,
            reason='host Manila staging journal is unreadable: %s' % exc)
    return _validate_share_journal_payload(
        instance, payload, share_mapping=share_mapping,
        operation_token=operation_token)


def _share_journal_records(instance, operation_token=None):
    journal_dir = _share_journal_directory(instance)
    try:
        names = os.listdir(journal_dir)
    except FileNotFoundError:
        return []
    records = []
    for name in names:
        if not name.endswith('.json'):
            continue
        path = os.path.join(journal_dir, name)
        try:
            with open(path, encoding='utf-8') as stream:
                payload = json.load(stream)
        except (OSError, ValueError) as exc:
            raise exception.ShareMountError(
                share_id=name.removesuffix('.json'),
                server_id=instance.uuid,
                reason='host Manila staging journal is unreadable: %s' % exc)
        payload = _validate_share_journal_payload(
            instance, payload, operation_token=operation_token)
        if os.path.basename(
                _share_journal_path(instance, payload['share_id'])) != name:
            raise exception.ShareMountError(
                share_id=payload['share_id'],
                server_id=instance.uuid,
                reason='host Manila staging journal path is invalid')
        records.append(payload)
    return sorted(records, key=lambda record: record['share_id'])


def _share_journal_recovery_candidates():
    """List migration-owned journals without authorizing their cleanup."""
    journal_root = os.path.join(
        CONF.instances_path, 'incus-share-journal')
    try:
        with os.scandir(journal_root) as entries:
            owner_entries = sorted(entries, key=lambda entry: entry.name)
    except FileNotFoundError:
        return []

    candidates = []
    for owner_entry in owner_entries:
        if (owner_entry.is_symlink() or
                not owner_entry.is_dir(follow_symlinks=False) or
                not _SHARE_ID_RE.fullmatch(owner_entry.name)):
            LOG.error(
                'Ignoring invalid Incus Manila journal owner path %s',
                owner_entry.path)
            continue
        try:
            with os.scandir(owner_entry.path) as entries:
                journal_entries = sorted(
                    (entry for entry in entries
                     if entry.name.endswith('.json')),
                    key=lambda entry: entry.name)
            if not journal_entries:
                continue
            if any(entry.is_symlink() or
                   not entry.is_file(follow_symlinks=False)
                   for entry in journal_entries):
                raise exception.MigrationError(
                    reason='journal directory contains a non-file')
            with open(journal_entries[0].path, encoding='utf-8') as stream:
                first = json.load(stream)
            instance_name = first.get('instance_name')
            if not isinstance(instance_name, str) or not instance_name:
                raise exception.MigrationError(
                    reason='journal has no instance name')
            owner = types.SimpleNamespace(
                uuid=owner_entry.name, name=instance_name)
            records = _share_journal_records(owner)
        except Exception:
            LOG.exception(
                'Ignoring an invalid Incus Manila journal transaction in %s',
                owner_entry.path)
            continue

        by_token = {}
        for record in records:
            token = record['operation_token']
            # Ordinary attach journals use a SHA-256 owner. Only a UUID
            # migration transaction can ever be replayed automatically.
            if not uuidutils.is_uuid_like(token):
                continue
            candidate = by_token.setdefault(token, {
                'uuid': owner.uuid,
                'name': owner.name,
                'operation_token': token,
                'share_ids': [],
            })
            candidate['share_ids'].append(record['share_id'])
        for candidate in by_token.values():
            candidate['share_ids'] = tuple(sorted(candidate['share_ids']))
            candidates.append(candidate)
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate['uuid'], candidate['operation_token']))


def _remove_share_journal(instance, share_id, operation_token=None):
    path = _share_journal_path(instance, share_id)
    if operation_token is not None:
        try:
            with open(path, encoding='utf-8') as stream:
                payload = json.load(stream)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            raise exception.ShareUmountError(
                share_id=share_id,
                server_id=instance.uuid,
                reason='host Manila staging journal is unreadable: %s' % exc)
        try:
            _validate_share_journal_payload(
                instance, payload, operation_token=operation_token)
        except exception.ShareMountError as exc:
            raise exception.ShareUmountError(
                share_id=share_id,
                server_id=instance.uuid,
                reason=exc) from exc
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    journal_dir = _share_journal_directory(instance)
    _fsync_directory(journal_dir)
    try:
        os.rmdir(journal_dir)
    except OSError as exc:
        if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
            raise
    else:
        _fsync_directory(os.path.dirname(journal_dir))


def _share_mapping_from_journal(instance, record):
    return type('JournaledShareMapping', (), {
        'share_id': record['share_id'],
        'instance_uuid': instance.uuid,
        'share_proto': record['share_proto'],
        'export_location': record['export_location'],
        'tag': record['tag'],
        'access_to': record.get('access_to'),
        'journal_version': record['version'],
    })()


def _journaled_share_mappings(
        instance, operation_token, expected_share_ids=None):
    records = _share_journal_records(
        instance, operation_token=operation_token)
    actual_ids = {record['share_id'] for record in records}
    if expected_share_ids is not None and actual_ids != set(
            expected_share_ids):
        raise exception.MigrationError(
            reason='Destination Manila staging journals do not match the '
                   'cold migration request')
    mappings = []
    mount_table = _share_mount_table_index()
    for record in records:
        mapping = _share_mapping_from_journal(instance, record)
        _validate_existing_share_mount(
            _share_mount_path(instance, mapping), mapping,
            mount_table=mount_table)
        mappings.append(mapping)
    return mappings


def _ensure_share_mount_path(instance, share_mapping):
    """Create a CRIU-accessible, non-listable Manila staging hierarchy."""
    share_root = os.path.join(CONF.instances_path, 'incus-shares')
    instance_root = os.path.join(share_root, instance.uuid)
    mount_path = _share_mount_path(instance, share_mapping)
    _validate_share_mount_path(
        mount_path, instance.uuid, share_mapping.share_id,
        exception.ShareMountError)
    mounted = os.path.ismount(mount_path)

    # CRIU opens external mounts after entering the instance user namespace.
    # Mapped root therefore needs search permission on these two directories,
    # but it must not be able to list another instance's staged shares.
    fileutils.ensure_tree(share_root, mode=0o711)
    os.chmod(share_root, 0o711)
    fileutils.ensure_tree(instance_root, mode=0o711)
    os.chmod(instance_root, 0o711)
    fileutils.ensure_tree(mount_path, mode=0o700)
    _validate_share_mount_path(
        mount_path, instance.uuid, share_mapping.share_id,
        exception.ShareMountError)
    # Once mounted, chmod would target the remote filesystem. In particular,
    # an NFS export with root_squash correctly rejects that operation.
    if not mounted:
        os.chmod(mount_path, 0o700)
    return mount_path


def _share_device_name(share_mapping):
    return 'manila-' + share_mapping.share_id


def _share_guest_path(share_mapping):
    if not _SHARE_TAG_RE.fullmatch(share_mapping.tag):
        raise exception.ShareMountError(
            share_id=share_mapping.share_id,
            server_id=share_mapping.instance_uuid,
            reason='share tag contains unsupported characters')
    return '/mnt/manila/' + share_mapping.tag


def _save_profile_marker(profile):
    """Persist a durable profile marker, tolerating backup.yaml resync noise.

    Incus commits the profile change to its database before propagating it
    into each member instance's backup.yaml; a propagation failure surfaces
    as an API error that says "profile change still saved". The propagation
    mounts the instance's root volume, which is impossible for records
    inside a shared-storage handover or a migration receive/teardown
    window, but the marker the caller needs is already durable at that
    point, so such failures must not abort the caller's operation.
    """
    try:
        profile.save(wait=True)
    except incus_exceptions.LXDAPIException as error:
        if 'profile change still saved' not in str(error):
            raise
        LOG.warning(
            'Profile marker for %s persisted, but its backup.yaml resync '
            'failed; continuing: %s', profile.name, error)


def _profile_lock_name(instance):
    """Serialize full Incus profile updates for one Nova instance."""
    return 'incus-profile-{}'.format(instance.uuid)


def _volume_topology_lock_name(instance):
    """Serialize all Cinder device topology changes for one instance."""
    return 'incus-volume-topology-{}'.format(instance.uuid)


def _volume_topology_lock_path():
    return CONF.state_path


def _live_migration_host_generation_lock_name(instance):
    """Serialize source retirement and reverse-target preparation."""
    return 'incus-live-host-generation-{}'.format(instance.uuid)


def _volume_operation_lock_name(volume_id):
    """Serialize one Cinder volume across instances on this compute host."""
    digest = hashlib.sha256(str(volume_id).encode('utf-8')).hexdigest()
    return 'incus-volume-operation-{}'.format(digest)


def _volume_operation_lock_path():
    return CONF.state_path


def _volume_manager_transaction_lock_name(instance_uuid, volume_id):
    """Serialize manager and driver-owned Cinder transactions."""
    digest = hashlib.sha256(
        '{}\0{}'.format(instance_uuid, volume_id).encode('utf-8')).hexdigest()
    return 'incus-nova-volume-transaction-{}'.format(digest)


def _share_operation_lock_name(instance, share_id):
    digest = hashlib.sha256(str(share_id).encode('utf-8')).hexdigest()
    return 'incus-share-{}-{}'.format(instance.uuid, digest)


def _share_mount_fstypes(share_mapping):
    if share_mapping.share_proto == obj_fields.ShareMappingProto.NFS:
        # A mount requested as "nfs" is commonly reported as "nfs4".
        return {'nfs', 'nfs4'}
    if share_mapping.share_proto == obj_fields.ShareMappingProto.CEPHFS:
        return {'ceph'}
    raise exception.ShareProtocolNotSupported(
        share_proto=share_mapping.share_proto)


def _normalize_share_export(export):
    normalized = str(export).strip()
    if normalized != '/':
        normalized = normalized.rstrip('/')
    return normalized


def _validate_cephfs_monitor(endpoint):
    """Return a canonical Ceph monitor endpoint or fail closed."""
    if endpoint.startswith('['):
        closing = endpoint.find(']')
        if closing < 0 or closing + 1 >= len(endpoint):
            raise ValueError('bracketed monitor address has no port')
        host = endpoint[1:closing]
        if endpoint[closing + 1] != ':':
            raise ValueError('bracketed monitor address has no port')
        try:
            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise ValueError('monitor IPv6 address is invalid') from exc
        port_text = endpoint[closing + 2:]
        canonical_host = '[%s]' % host.lower()
    else:
        host, separator, port_text = endpoint.rpartition(':')
        if not separator or not host or ':' in host:
            raise ValueError('monitor address must include a port')
        try:
            canonical_host = str(ipaddress.IPv4Address(host))
        except ValueError:
            if not _CEPHFS_DNS_NAME_RE.fullmatch(host):
                raise ValueError('monitor hostname is invalid')
            canonical_host = host.lower()
    try:
        port = int(port_text)
    except (TypeError, ValueError) as exc:
        raise ValueError('monitor port is invalid') from exc
    if str(port) != port_text or not 1 <= port <= 65535:
        raise ValueError('monitor port is invalid')
    return '%s:%d' % (canonical_host, port)


def _cephfs_mount_spec(share_mapping, access_to=None):
    """Build an unambiguous Ceph v2 device and monitor option."""
    fsid = CONF.incus.manila_cephfs_cluster_fsid
    filesystem = CONF.incus.manila_cephfs_filesystem_name
    try:
        canonical_fsid = str(uuid.UUID(fsid))
    except (AttributeError, TypeError, ValueError) as exc:
        raise exception.ShareMountError(
            share_id=share_mapping.share_id,
            server_id=share_mapping.instance_uuid,
            reason='manila_cephfs_cluster_fsid is not a UUID') from exc
    if canonical_fsid != fsid:
        raise exception.ShareMountError(
            share_id=share_mapping.share_id,
            server_id=share_mapping.instance_uuid,
            reason='manila_cephfs_cluster_fsid is not canonical')
    if (not isinstance(filesystem, str) or
            not _CEPHFS_NAME_RE.fullmatch(filesystem)):
        raise exception.ShareMountError(
            share_id=share_mapping.share_id,
            server_id=share_mapping.instance_uuid,
            reason='manila_cephfs_filesystem_name is invalid')

    export = _normalize_share_export(share_mapping.export_location)
    match = _CEPHFS_EXPORT_RE.fullmatch(export)
    if match is None:
        raise exception.ShareMountError(
            share_id=share_mapping.share_id,
            server_id=share_mapping.instance_uuid,
            reason='CephFS export does not use monitors:/absolute/path')
    try:
        monitors = tuple(
            _validate_cephfs_monitor(endpoint)
            for endpoint in match.group('monitors').split(','))
    except ValueError as exc:
        raise exception.ShareMountError(
            share_id=share_mapping.share_id,
            server_id=share_mapping.instance_uuid,
            reason='CephFS monitor list is invalid: %s' % exc) from exc
    if not monitors or len(set(monitors)) != len(monitors):
        raise exception.ShareMountError(
            share_id=share_mapping.share_id,
            server_id=share_mapping.instance_uuid,
            reason='CephFS monitor list is empty or contains duplicates')
    if access_to is None:
        access_to = share_mapping.access_to
    if (not isinstance(access_to, str) or
            not _CEPHFS_NAME_RE.fullmatch(access_to)):
        raise exception.ShareMountError(
            share_id=share_mapping.share_id,
            server_id=share_mapping.instance_uuid,
            reason='CephFS access client name is invalid')
    device = '{}@{}.{}={}'.format(
        access_to, fsid, filesystem, match.group('path'))
    return device, '/'.join(monitors)


def _share_mount_table_index():
    """Return one normalized mount-table snapshot keyed by mountpoint."""
    return {
        os.path.realpath(partition.mountpoint): {
            'device': _normalize_share_export(partition.device),
            'fstype': partition.fstype,
            'opts': frozenset(filter(None, partition.opts.split(','))),
        }
        for partition in psutil.disk_partitions(all=True)
    }


def _validate_existing_share_mount(
        mount_path, share_mapping, mount_table=None):
    """Fail closed if a staged path is mounted from another export."""
    cephfs = (share_mapping.share_proto ==
              obj_fields.ShareMappingProto.CEPHFS)
    legacy_cephfs_journal = (
        cephfs and getattr(share_mapping, 'journal_version', None) == 1 and
        getattr(share_mapping, 'access_to', None) is None)
    if cephfs and not legacy_cephfs_journal:
        expected_export, _mon_addr = _cephfs_mount_spec(share_mapping)
    elif not cephfs:
        expected_export = _normalize_share_export(
            share_mapping.export_location)
    expected_fstypes = _share_mount_fstypes(share_mapping)
    real_mount_path = _validate_share_mount_path(
        mount_path, share_mapping.instance_uuid, share_mapping.share_id,
        exception.ShareMountError)

    if mount_table is None:
        mount_table = _share_mount_table_index()
    mounted = mount_table.get(real_mount_path)
    if mounted is not None:
        actual_export = mounted['device']
        if legacy_cephfs_journal:
            access_to, separator, _remainder = actual_export.partition('@')
            if not separator:
                raise exception.ShareMountError(
                    share_id=share_mapping.share_id,
                    server_id=share_mapping.instance_uuid,
                    reason='existing CephFS host mount source is invalid')
            expected_export, _mon_addr = _cephfs_mount_spec(
                share_mapping, access_to=access_to)
        actual_options = frozenset(mounted.get('opts') or ())
        required_options = {'rw', 'nosuid', 'nodev'}
        if (mounted['fstype'] not in expected_fstypes or
                actual_export != expected_export or
                not required_options.issubset(actual_options)):
            raise exception.ShareMountError(
                share_id=share_mapping.share_id,
                server_id=share_mapping.instance_uuid,
                reason=(
                    'existing host mount uses source %(actual)s and '
                    'filesystem %(fstype)s and options %(options)s; '
                    'expected source %(expected)s, filesystem '
                    '%(expected_fstypes)s and rw,nosuid,nodev' % {
                        'actual': actual_export,
                        'fstype': mounted['fstype'],
                        'expected': expected_export,
                        'expected_fstypes': ','.join(
                            sorted(expected_fstypes)),
                        'options': ','.join(sorted(actual_options)),
                    }))
        return

    raise exception.ShareMountError(
        share_id=share_mapping.share_id,
        server_id=share_mapping.instance_uuid,
        reason='existing host mount is absent from the mount table')


def _profile_share_mount_inventory(profile, instance):
    """Return safe mount paths and malformed Manila profile device reasons."""
    instance_root = os.path.realpath(os.path.join(
        CONF.instances_path, 'incus-shares', instance.uuid))
    mounts = []
    malformed = []
    devices = profile.devices
    if not isinstance(devices, dict):
        return mounts, [(
            'profile.devices', 'profile device inventory is not a mapping')]
    for name, device in devices.items():
        if not isinstance(name, str) or not name.startswith('manila-'):
            continue
        share_id = name.removeprefix('manila-')
        if not _SHARE_ID_RE.fullmatch(share_id):
            malformed.append((name, 'device name does not contain a UUID'))
            continue
        if not isinstance(device, dict):
            malformed.append((name, 'device configuration is not a mapping'))
            continue
        if device.get('type') != 'disk':
            malformed.append((name, 'device type is not disk'))
            continue
        expected = os.path.realpath(os.path.join(instance_root, share_id))
        source = device.get('source')
        if not isinstance(source, str) or not source:
            malformed.append((name, 'device source is missing'))
            continue
        if os.path.realpath(source) != expected:
            malformed.append((
                name,
                'device source is outside its Nova staging directory'))
            continue
        mounts.append(expected)
    return mounts, malformed


def _profile_share_mounts(profile, instance):
    """Return mounts, failing closed on malformed Manila profile devices."""
    mounts, malformed = _profile_share_mount_inventory(profile, instance)
    if malformed:
        device_name, reason = malformed[0]
        raise exception.ShareUmountError(
            share_id=device_name,
            server_id=instance.uuid,
            reason='malformed Incus Manila profile device: %s' % reason)
    return mounts


def _profile_has_share_devices(profile):
    devices = profile.devices
    return (
        isinstance(devices, dict) and
        any(isinstance(name, str) and name.startswith('manila-')
            for name in devices)
    )


def _cleanup_profile_share_mounts(profile, instance):
    """Unmount host-side Manila staging paths after migration handoff."""
    instance_root = os.path.realpath(os.path.join(
        CONF.instances_path, 'incus-shares', instance.uuid))
    mounts, malformed = _profile_share_mount_inventory(profile, instance)
    failures = [
        (device_name, ValueError(
            'malformed Incus Manila profile device: %s' % reason))
        for device_name, reason in malformed
    ]
    removed_devices = {}
    mount_table = _share_mount_table_index()
    for mount_path in reversed(mounts):
        share_id = os.path.basename(mount_path)
        real_mount_path = os.path.realpath(mount_path)
        try:
            if real_mount_path in mount_table:
                incus_privsep.umount(
                    mount_path, CONF.incus.share_unmount_timeout)
                mount_table.pop(real_mount_path, None)
            if os.path.isdir(mount_path):
                os.rmdir(mount_path)
            _remove_share_journal(instance, share_id)
            device_name = 'manila-' + share_id
            if device_name in profile.devices:
                removed_devices[device_name] = profile.devices.pop(
                    device_name)
        except Exception as exc:
            failures.append((share_id, exc))
            LOG.exception(
                'Failed to clean destination Manila staging path %s',
                mount_path, instance=instance)
    if os.path.isdir(instance_root):
        try:
            os.rmdir(instance_root)
        except OSError as exc:
            # A failed child cleanup legitimately keeps this directory busy.
            if (
                    not failures or
                    exc.errno not in (errno.ENOTEMPTY, errno.EEXIST)):
                failures.append(('instance-root', exc))

    if removed_devices:
        try:
            profile.save(wait=True)
        except Exception as exc:
            profile.devices.update(removed_devices)
            failures.append(('profile-devices', exc))
            LOG.exception(
                'Failed to persist Manila profile device cleanup',
                instance=instance)

    if failures:
        share_id, first_error = failures[0]
        raise exception.ShareUmountError(
            share_id=share_id,
            server_id=instance.uuid,
            reason='{} Manila staging cleanup operation(s) failed; '
                   'first error: {}'.format(len(failures), first_error))


def _cleanup_share_journal_mounts(instance, operation_token=None):
    """Retry mounts which were persisted before an Incus profile existed."""
    failures = []
    mount_table = _share_mount_table_index()
    for record in reversed(
            _share_journal_records(
                instance, operation_token=operation_token)):
        share_id = record['share_id']
        mount_path = os.path.join(
            CONF.instances_path, 'incus-shares', instance.uuid, share_id)
        real_mount_path = os.path.realpath(mount_path)
        try:
            if real_mount_path in mount_table:
                mapping = _share_mapping_from_journal(instance, record)
                _validate_existing_share_mount(
                    mount_path, mapping, mount_table=mount_table)
                incus_privsep.umount(
                    mount_path, CONF.incus.share_unmount_timeout)
                mount_table.pop(real_mount_path, None)
            if os.path.isdir(mount_path):
                os.rmdir(mount_path)
            _remove_share_journal(
                instance, share_id, record['operation_token'])
        except Exception as exc:
            failures.append((share_id, exc))
            LOG.exception(
                'Failed to clean journaled Manila staging path %s',
                mount_path, instance=instance)
    instance_root = os.path.join(
        CONF.instances_path, 'incus-shares', instance.uuid)
    if os.path.isdir(instance_root):
        try:
            os.rmdir(instance_root)
        except OSError as exc:
            if (
                    not failures or
                    exc.errno not in (errno.ENOTEMPTY, errno.EEXIST)):
                failures.append(('instance-root', exc))
    if failures:
        share_id, first_error = failures[0]
        raise exception.ShareUmountError(
            share_id=share_id,
            server_id=instance.uuid,
            reason='{} journaled Manila cleanup operation(s) failed; '
                   'first error: {}'.format(len(failures), first_error))


def _live_migration_share_sources(devices):
    return [
        device.get('source')
        for name, device in devices.items()
        if (name.startswith('manila-') and
            device.get('type') == 'disk' and
            device.get('recursive') == 'true' and
            device.get('source'))
    ]


def _prepare_live_migration_destination_profile(
        client, instance, config, devices, cleanup_token):
    config = dict(config)
    config[MIGRATION_DESTINATION_PREPARED_KEY] = cleanup_token
    share_sources = _live_migration_share_sources(devices)
    expected_share_ids = {
        name.removeprefix('manila-')
        for name in devices
        if name.startswith('manila-')
    }
    share_journals = _share_journal_records(
        instance, operation_token=cleanup_token)
    if {record['share_id'] for record in share_journals} != (
            expected_share_ids):
        raise exception.MigrationError(
            reason='Destination Manila staging journals do not match '
                   'the source profile')
    mount_table = _share_mount_table_index()
    for record in share_journals:
        mapping = _share_mapping_from_journal(instance, record)
        _validate_existing_share_mount(
            _share_mount_path(instance, mapping), mapping,
            mount_table=mount_table)
    for attempt_number in range(CONF.incus.migration_finish_retries):
        try:
            client.profiles.create(instance.name, config, devices)
            break
        except incus_exceptions.LXDAPIException as exc:
            if _incus_api_status_code(exc) == 409:
                raise exception.DestinationDiskExists(path=instance.name)
            mount_visibility_race = (
                share_sources and
                all(os.path.ismount(path) for path in share_sources) and
                'recursive option is only supported for additional '
                'bind-mounted paths' in str(exc))
            if (
                    not mount_visibility_race or
                    attempt_number + 1 >=
                    CONF.incus.migration_finish_retries):
                raise
            LOG.warning(
                'Waiting for destination Manila mounts to propagate into '
                'the Incus daemon namespace before creating profile %s '
                '(attempt %d/%d)',
                instance.name, attempt_number + 1,
                CONF.incus.migration_finish_retries,
                instance=instance)
            eventlet.sleep(CONF.incus.migration_finish_retry_interval)
    # Profile creation is the durable consumer of all destination mounts.
    for record in share_journals:
        _remove_share_journal(
            instance, record['share_id'], cleanup_token)


def _pack_configdrive_for_migration(instance, container):
    """Return a bounded, authenticated config-drive migration payload."""
    source = _configdrive_path(instance)
    if not os.path.isdir(source):
        raise exception.MigrationError(
            reason='Instance config-drive directory is missing')

    storage_id = _container_root_host_id(container)
    privsep_path.chown(
        source, uid=os.getuid(), gid=os.getgid(), recursive=True)
    max_bytes = CONF.incus.configdrive_migration_max_bytes
    max_files = CONF.incus.configdrive_migration_max_files
    total_bytes = 0
    entry_count = 0
    archive = io.BytesIO()
    try:
        with tarfile.open(fileobj=archive, mode='w:gz') as output:
            for root, dirs, files in os.walk(source):
                dirs.sort()
                files.sort()
                relative_root = os.path.relpath(root, source)
                for name in dirs + files:
                    path = os.path.join(root, name)
                    if os.path.islink(path):
                        raise exception.MigrationError(
                            reason='Config-drive migration rejects '
                            'symbolic links')
                    relative = os.path.normpath(os.path.join(
                        relative_root, name))
                    if relative.startswith('..') or os.path.isabs(relative):
                        raise exception.MigrationError(
                            reason='Config-drive contains an unsafe path')
                    entry_count += 1
                    if entry_count > max_files:
                        raise exception.MigrationError(
                            reason='Config-drive exceeds migration file limit')

                    info = output.gettarinfo(path, arcname=relative)
                    info.uid = 0
                    info.gid = 0
                    info.uname = ''
                    info.gname = ''
                    if info.isdir():
                        output.addfile(info)
                        continue
                    if not info.isfile():
                        raise exception.MigrationError(
                            reason='Config-drive contains a non-regular file')
                    total_bytes += info.size
                    if total_bytes > max_bytes:
                        raise exception.MigrationError(
                            reason='Config-drive exceeds migration byte limit')
                    with open(path, 'rb') as stream:
                        output.addfile(info, stream)
    finally:
        privsep_path.chown(
            source, uid=storage_id, gid=storage_id, recursive=True)

    raw = archive.getvalue()
    if len(raw) > max_bytes:
        raise exception.MigrationError(
            reason='Compressed config-drive exceeds migration byte limit')
    return {
        'format': 'tar.gz-v1',
        'size': len(raw),
        'sha256': hashlib.sha256(raw).hexdigest(),
        'data': base64.b64encode(raw).decode('ascii'),
    }


def _stage_configdrive_from_migration(instance, payload):
    """Validate and extract a config-drive before claiming target storage."""
    if not isinstance(payload, dict) or payload.get('format') != 'tar.gz-v1':
        raise exception.MigrationError(
            reason='Unsupported config-drive migration payload')
    try:
        declared_size = int(payload['size'])
        raw = base64.b64decode(payload['data'], validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise exception.MigrationError(
            reason='Invalid config-drive migration encoding: %s' % exc)

    max_bytes = CONF.incus.configdrive_migration_max_bytes
    if declared_size != len(raw) or len(raw) > max_bytes:
        raise exception.MigrationError(
            reason='Config-drive migration payload size is invalid')
    digest = hashlib.sha256(raw).hexdigest()
    if digest != str(payload.get('sha256', '')):
        raise exception.MigrationError(
            reason='Config-drive migration payload checksum mismatch')

    instance_dir = common.InstanceAttributes(instance).instance_dir
    fileutils.ensure_tree(instance_dir)
    staging = tempfile.mkdtemp(
        prefix='.configdrive-migration-', dir=instance_dir)
    total_bytes = 0
    entry_count = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
            for member in archive:
                entry_count += 1
                if entry_count > CONF.incus.configdrive_migration_max_files:
                    raise exception.MigrationError(
                        reason='Config-drive exceeds migration file limit')
                normalized = os.path.normpath(member.name)
                if (normalized in ('', '.') or
                        normalized.startswith('..') or
                        os.path.isabs(normalized)):
                    raise exception.MigrationError(
                        reason='Config-drive archive contains an unsafe path')
                if not (member.isdir() or member.isfile()):
                    raise exception.MigrationError(
                        reason='Config-drive archive contains an unsafe type')

                destination = os.path.join(staging, normalized)
                if member.isdir():
                    os.makedirs(destination, mode=0o700, exist_ok=True)
                    continue
                total_bytes += member.size
                if total_bytes > max_bytes:
                    raise exception.MigrationError(
                        reason='Config-drive exceeds migration byte limit')
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise exception.MigrationError(
                        reason='Config-drive archive file has no contents')
                with source, open(destination, 'xb') as output:
                    shutil.copyfileobj(source, output)
                os.chmod(destination, 0o400)
        for root, dirs, _files in os.walk(staging, topdown=False):
            for name in dirs:
                os.chmod(os.path.join(root, name), 0o500)
            os.chmod(root, 0o500)
        return staging
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _prepare_configdrive_migration(instance, transfer):
    """Validate config-drive mode and stage any transferred content."""
    payload = transfer.get('configdrive')
    if bool(payload) != bool(instance.config_drive):
        raise exception.MigrationError(
            reason='Source and destination disagree on config-drive mode')

    if payload:
        return _stage_configdrive_from_migration(instance, payload)

    return None


def _container_root_host_id(container):
    for key in (
            'volatile.idmap.next',
            'volatile.idmap.current',
            'volatile.last_state.idmap'):
        try:
            mappings = jsonutils.loads(container.config.get(key, '[]'))
        except (TypeError, ValueError) as exc:
            raise exception.MigrationError(
                reason='Cannot read target container idmap: %s' % exc)
        for mapping in mappings:
            if mapping.get('Isuid'):
                return mapping.get('Hostid', 0)
    raise exception.MigrationError(
        reason='Target unprivileged container has no UID idmap')


def _commit_staged_configdrive(instance, container, staging):
    """Apply the target idmap and atomically publish a staged config-drive."""
    destination = _configdrive_path(instance)
    if os.path.exists(destination):
        raise exception.MigrationError(
            reason='Target config-drive directory already exists')
    storage_id = _container_root_host_id(container)
    incus_privsep.chown_tree_to_host_id(staging, storage_id)
    os.replace(staging, destination)
    return destination


def _profile_device_info(profile, volume_id, device):
    return _profile_volume_record(
        profile, volume_id, device=device)['device_info']


def _profile_has_volume_connection(profile, volume_id):
    devices = profile.devices if isinstance(profile.devices, dict) else {}
    config = profile.config if isinstance(profile.config, dict) else {}
    return (
        volume_id in devices or
        any(key in config
            for key in _volume_device_info_keys(volume_id)))


def _profile_volume_ids(profile):
    """Return volume IDs with durable connector metadata."""
    config = profile.config if isinstance(profile.config, dict) else {}
    prefixes = (
        'user.openstack.volume.',
        'user.openstack.volume_device_info.',
    )
    volume_ids = set()
    for key in config:
        for prefix in prefixes:
            if key.startswith(prefix) and key[len(prefix):]:
                volume_ids.add(key[len(prefix):])
                break
    return sorted(volume_ids)


def _data_volume_topology(profile, journal_records):
    """Return proven and opaque unix-block ownership for one Nova profile."""
    devices = profile.devices if isinstance(profile.devices, dict) else {}
    profile_ids = set(_profile_volume_ids(profile))
    journal_ids = set(journal_records)
    unix_block_ids = {
        name for name, device in devices.items()
        if isinstance(device, dict) and device.get('type') == 'unix-block'
    }
    proven_ids = profile_ids | journal_ids
    return {
        'profile_ids': profile_ids,
        'journal_ids': journal_ids,
        'unix_block_ids': unix_block_ids,
        'proven_ids': proven_ids,
        'opaque_ids': unix_block_ids - proven_ids,
    }


def _rbd_mapping_matches(connection_data, mapping_cache=None):
    """Return local RBD mappings matching one stable pool/image identity."""
    image = connection_data.get('name')
    if not isinstance(image, str) or image.count('/') != 1:
        raise exception.InvalidVolume(
            reason='RBD connection information has no pool/image name')
    pool, image_name = image.split('/', 1)
    namespace = _rbd_namespace(connection_data)
    command = ['rbd', 'showmapped', '--format=json']
    user = connection_data.get('auth_username')
    if user:
        command.extend(['--id', str(user)])
    hosts = connection_data.get('hosts') or []
    ports = connection_data.get('ports') or []
    if hosts and ports:
        monitors = []
        for host, port in zip(hosts, ports):
            host = str(host)
            if ':' in host and not host.startswith('['):
                host = '[%s]' % host
            monitors.append('%s:%s' % (host, port))
        command.extend(['--mon_host', ','.join(monitors)])
    cache_key = tuple(command)
    mapping_index = (
        mapping_cache.get(cache_key)
        if mapping_cache is not None else None)
    if mapping_index is None:
        try:
            # `rbd showmapped` reads the kernel's local mapping table and does
            # not require a privileged helper. Keeping it unprivileged avoids
            # granting nova-compute a broad executable rootwrap rule for the
            # rbd binary.
            output, _error = processutils.execute(*command)
            mappings = jsonutils.loads(output)
        except Exception as exc:
            raise exception.InvalidVolume(
                reason='Cannot verify the local RBD mapping for %s: %s' %
                       (image, exc)) from exc
        if isinstance(mappings, dict):
            mappings = list(mappings.values())
        if not isinstance(mappings, list):
            raise exception.InvalidVolume(
                reason='rbd showmapped returned an invalid mapping list')
        mapping_index = {}
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            identity = (
                mapping.get('pool'),
                mapping.get('name'),
                mapping.get('namespace') or '',
            )
            mapping_index.setdefault(identity, []).append(mapping)
        if mapping_cache is not None:
            mapping_cache[cache_key] = mapping_index
    return image, mapping_index.get((pool, image_name, namespace), [])


def _mapped_rbd_device(connection_data, mapping_cache=None):
    """Resolve one RBD mapping by stable pool/image identity."""
    image, matches = _rbd_mapping_matches(
        connection_data, mapping_cache=mapping_cache)
    if len(matches) != 1:
        raise exception.InvalidVolume(
            reason='Expected exactly one local RBD mapping for %s, found %d' %
                   (image, len(matches)))
    device = os.path.realpath(matches[0].get('device', ''))
    return _validate_block_device_path(
        device, 'rbd showmapped device')


def _failed_build_rbd_mapping_ownership(
        volume_bdms, container, profile, root_bdm):
    """Return Cinder/host release safety from Nova BDM and KRBD state."""
    release_cinder = True
    release_host = True
    reasons = set()
    if root_bdm is not None and (container is not None or profile is not None):
        release_cinder = False
        reasons.add('BFV root is still claimed by Incus')
    mapping_cache = {}
    for bdm in volume_bdms:
        is_root = _is_boot_volume(bdm)
        connection_info = bdm.get('connection_info')
        if not isinstance(connection_info, dict):
            release_cinder = False
            release_host = False
            reasons.add('Cinder connection information is unavailable')
            continue
        if container is not None and not is_root:
            release_cinder = False
            reasons.add('running Incus ownership may retain a data volume')
        if connection_info.get('driver_volume_type') != 'rbd':
            continue
        try:
            image, matches = _rbd_mapping_matches(
                connection_info.get('data') or {},
                mapping_cache=mapping_cache)
        except Exception as exc:
            release_cinder = False
            release_host = False
            reasons.add('local RBD mapping is uncertain: {}'.format(exc))
            continue
        if matches:
            release_cinder = False
            release_host = False
            reasons.add(
                'local {} RBD mapping still exists for {}'.format(
                    'BFV root' if is_root else 'data-volume', image))
    return release_cinder, release_host, reasons


def _validate_volume_recovery_record(
        record, volume_id, mountpoint, connection_info):
    """Prove that an unfinished journal belongs to this attach request."""
    if not isinstance(record, dict):
        raise exception.InvalidVolume(
            reason='Cinder volume %s has an invalid recovery record' %
                   volume_id)
    phase = record.get('phase')
    if phase not in (
            'connecting', 'connected', 'rolled-back', 'disconnecting',
            'disconnected'):
        raise exception.InvalidVolume(
            reason='Cinder volume %s has an invalid recovery phase' %
                   volume_id)
    recorded_mountpoint = record.get('mountpoint')
    if recorded_mountpoint and recorded_mountpoint != mountpoint:
        raise exception.InvalidVolume(
            reason='Cinder volume %s recovery record uses %s instead of %s' %
                   (volume_id, recorded_mountpoint, mountpoint))
    requested_protocol = connection_info.get('driver_volume_type')
    recorded_protocol = record.get('driver_volume_type')
    if recorded_protocol and recorded_protocol != requested_protocol:
        raise exception.InvalidVolume(
            reason='Cinder volume %s recovery record uses a different '
                   'connector protocol' % volume_id)
    if requested_protocol == 'rbd':
        requested_data = connection_info.get('data') or {}
        recorded_data = record.get('connection_data') or {}
        requested_name = requested_data.get('name')
        recorded_name = recorded_data.get('name')
        if (recorded_name and
                (not requested_name or recorded_name != requested_name)):
            raise exception.InvalidVolume(
                reason='Cinder volume %s recovery record uses a different '
                       'RBD image' % volume_id)
        if _rbd_namespace(recorded_data) != _rbd_namespace(requested_data):
            raise exception.InvalidVolume(
                reason='Cinder volume %s recovery record uses a different '
                       'RBD namespace' % volume_id)
    return phase


def _profile_volume_attachment_matches(
        profile, volume_id, mountpoint, qos_limits, connection_info,
        rbd_mapping_cache=None, allow_missing_rbd_mapping=False):
    """Validate an already-connected volume for idempotent retries."""
    device = profile.devices.get(volume_id)
    metadata = [
        key for key in _volume_device_info_keys(volume_id)
        if key in profile.config
    ]
    if device is None and not metadata:
        return False
    if device is None or not metadata:
        raise exception.InvalidVolume(
            reason='Incus profile contains an incomplete connection for '
                   'Cinder volume %s' % volume_id)
    if (device.get('type') != 'unix-block' or
            device.get('path') != mountpoint or
            device.get('required') != 'true'):
        raise exception.InvalidVolume(
            reason='Existing Incus device for Cinder volume %s does not '
                   'match the requested attachment' % volume_id)

    record = _profile_volume_record(profile, volume_id, device=device)
    source = os.path.realpath(device.get('source', ''))
    requested_protocol = connection_info.get('driver_volume_type')
    if not (allow_missing_rbd_mapping and requested_protocol == 'rbd'):
        _validate_block_device_path(source, 'Existing os-brick connector path')
    if record.get('phase') != 'connected':
        raise exception.InvalidVolume(
            reason='Cinder volume %s has an unfinished %s journal record' %
                   (volume_id, record.get('phase')))
    stored_protocol = record.get('driver_volume_type')
    if stored_protocol and stored_protocol != requested_protocol:
        raise exception.InvalidVolume(
            reason='Existing Incus connector protocol for Cinder volume %s '
                   'does not match the requested attachment' % volume_id)
    if requested_protocol == 'rbd':
        requested_data = connection_info.get('data') or {}
        stored_data = record.get('connection_data') or {}
        requested_name = requested_data.get('name')
        stored_name = stored_data.get('name')
        if not isinstance(requested_name, str) or '/' not in requested_name:
            raise exception.InvalidVolume(
                reason='RBD connection information has no pool/image name')
        if stored_name and stored_name != requested_name:
            raise exception.InvalidVolume(
                reason='Stored RBD identity for Cinder volume %s does not '
                       'match the requested pool/image' % volume_id)
        if _rbd_namespace(stored_data) != _rbd_namespace(requested_data):
            raise exception.InvalidVolume(
                reason='Stored RBD namespace for Cinder volume %s does not '
                       'match the requested namespace' % volume_id)
        if allow_missing_rbd_mapping:
            unused_image, mappings = _rbd_mapping_matches(
                connection_info.get('data') or {},
                mapping_cache=rbd_mapping_cache)
            if len(mappings) > 1:
                raise exception.InvalidVolume(
                    reason='Expected at most one local RBD mapping for Cinder '
                           'volume %s, found %d' % (volume_id, len(mappings)))
            if not mappings:
                recorded_source = os.path.realpath(
                    (record.get('device_info') or {}).get('path', ''))
                if not recorded_source or source != recorded_source:
                    raise exception.InvalidVolume(
                        reason='Disconnected Incus source for Cinder volume '
                               '%s does not match its durable os-brick record'
                               % volume_id)
            else:
                expected_source = _validate_block_device_path(
                    os.path.realpath(mappings[0].get('device', '')),
                    'rbd showmapped device')
                if source != expected_source:
                    raise exception.InvalidVolume(
                        reason='Existing Incus source for Cinder volume %s '
                               'resolves to a different RBD mapping' %
                               volume_id)
        else:
            expected_source = _mapped_rbd_device(
                connection_info.get('data') or {},
                mapping_cache=rbd_mapping_cache)
            if source != expected_source:
                raise exception.InvalidVolume(
                    reason='Existing Incus source for Cinder volume %s '
                           'resolves to a different RBD mapping' % volume_id)
    for key in ('limits.read', 'limits.write'):
        if device.get(key) != qos_limits.get(key):
            raise exception.InvalidVolume(
                reason='Existing Incus QoS for Cinder volume %s does not '
                       'match the requested attachment' % volume_id)
    return True


def _network_prefix(address, netmask):
    address_version = ipaddress.ip_address(address).version
    mask = ipaddress.ip_address(netmask)
    if mask.version != address_version:
        raise ValueError('Network address and netmask versions differ')
    bits = format(int(mask), '0%db' % mask.max_prefixlen)
    if '01' in bits:
        raise ValueError('Network netmask is not contiguous')
    return bits.count('1')


def _incus_network_config(network_info):
    """Convert Nova network metadata to cloud-init network config v2."""
    if not network_info:
        return None

    ethernets = {}
    for vif in network_info:
        mac_address = vif.get('address')
        network = vif.get('network') or {}
        if not mac_address:
            continue

        interface_name = incus_vif.get_vif_guest_devname(vif)
        # Incus applies this stable name to the NIC device. Avoid a MAC match
        # here: netplan renders it as PermanentMACAddress=, which does not
        # match a veth and leaves the interface unmanaged.
        interface = {}
        mtu = (network.get('meta') or {}).get('mtu')
        if mtu:
            interface['mtu'] = mtu

        addresses = []
        routes = []
        nameservers = []
        for subnet in network.get('subnets', []):
            cidr = ipaddress.ip_network(str(subnet['cidr']), strict=False)
            for ip in subnet.get('ips', []):
                addresses.append('%s/%s' % (
                    ip['address'], cidr.prefixlen))

            gateway = subnet.get('gateway')
            if gateway:
                gateway_address = (
                    gateway.get('address')
                    if hasattr(gateway, 'get') else str(gateway))
                routes.append({
                    'to': '0.0.0.0/0' if cidr.version == 4 else '::/0',
                    'via': gateway_address,
                })

            for route in subnet.get('routes', []):
                route_cidr = route.get('cidr')
                if route_cidr is None:
                    prefix = _network_prefix(
                        route['network'], route['netmask'])
                    route_cidr = '%s/%s' % (route['network'], prefix)
                route_gateway = route.get('gateway')
                if hasattr(route_gateway, 'get'):
                    route_gateway = route_gateway.get('address')
                routes.append({
                    'to': str(route_cidr),
                    'via': str(route_gateway),
                })
            nameservers.extend(
                dns.get('address') if hasattr(dns, 'get') else str(dns)
                for dns in subnet.get('dns', []))

        if addresses:
            interface['addresses'] = addresses
        if routes:
            interface['routes'] = routes
        if nameservers:
            interface['nameservers'] = {
                'addresses': list(dict.fromkeys(nameservers)),
            }
        if not interface:
            continue
        ethernets[interface_name] = interface

    if not ethernets:
        return None
    return yaml.safe_dump(
        {'version': 2, 'ethernets': ethernets},
        default_flow_style=False, sort_keys=False)


def _decompress_bounded_user_data(raw):
    """Expand gzipped user-data without letting it expand without bound.

    Nova caps user-data at 64 KiB, but gzip expands by up to about a
    thousand times, so decompressing whole turns that cap into tens of
    megabytes. The result is not transient either: it is stored in the
    Incus instance configuration and returned by every read of that
    instance, so each inventory scan pays for it again.

    Reading one byte past the ceiling is what makes the limit real. A
    length check applied after ``gzip.decompress`` would already have
    built the oversized value it was meant to prevent.
    """
    limit = CONF.incus.maximum_user_data_kb * units.Ki
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as expanded:
            decompressed = expanded.read(limit + 1)
    except (OSError, EOFError, zlib.error) as exc:
        raise exception.Invalid(
            'Instance user_data is not valid gzip data') from exc
    if len(decompressed) > limit:
        raise exception.Invalid(
            'Instance user_data expands beyond the %d KiB limit set by '
            '[incus] maximum_user_data_kb'
            % CONF.incus.maximum_user_data_kb)
    return decompressed


def _incus_cloud_init_config(instance, network_info=None):
    """Translate Nova bootstrap data to the Incus NoCloud template keys."""
    config = {'user.openstack.uuid': instance.uuid}
    metadata = {
        'instance-id': instance.uuid,
        'local-hostname': instance.hostname,
    }

    user_data = getattr(instance, 'user_data', None)
    if user_data:
        if isinstance(user_data, str):
            user_data = user_data.encode('ascii')
        raw = base64.b64decode(user_data)
        if raw[:2] == b'\x1f\x8b':
            # Users gzip user-data to fit Nova's 64K API limit. Incus
            # config values must be text, so carry the decompressed form,
            # which cloud-init treats identically.
            raw = _decompress_bounded_user_data(raw)
        try:
            config['cloud-init.user-data'] = raw.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise exception.Invalid(
                'Instance user_data is neither UTF-8 text nor '
                'gzip-compressed UTF-8 text') from exc

    key_data = getattr(instance, 'key_data', None)
    if key_data:
        key_name = getattr(instance, 'key_name', None) or 'nova-key'
        metadata['public-keys'] = {key_name: key_data.strip()}

    config['user.meta-data'] = yaml.safe_dump(
        metadata, default_flow_style=False, sort_keys=True)

    network_config = _incus_network_config(network_info)
    if network_config:
        config['cloud-init.network-config'] = network_config
        config['cloud-init.vendor-data'] = _NETWORK_ACTIVATION_VENDOR_DATA

    return config


class _NeutronFirewallDriver:
    """No-op adapter; Neutron ML2/OVN owns filtering for every VIF."""

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _last_bytes(file_like_object, num):
    """Return num bytes from the end of the file, and remaning byte count.

    :param file_like_object: The file to read
    :param num: The number of bytes to return

    :returns: (data, remaining)
    """

    try:
        file_like_object.seek(-num, os.SEEK_END)
    except IOError as e:
        # seek() fails with EINVAL when trying to go before the start of
        # the file. It means that num is larger than the file size, so
        # just go to the start.
        if e.errno == errno.EINVAL:
            file_like_object.seek(0, os.SEEK_SET)
        else:
            raise

    remaining = file_like_object.tell()
    return (file_like_object.read(), remaining)


def _neutron_failed_callback(event_name, instance):
    LOG.error("Neutron Reported failure on event "
              "{event} for instance {uuid}"
              .format(event=event_name, uuid=instance.name),
              instance=instance)
    if CONF.vif_plugging_is_fatal:
        raise exception.VirtualInterfaceCreateException()


def _get_cpu_info():
    """Get cpu information.

    This method executes lscpu and then parses the output,
    returning a dictionary of information.
    """
    cpuinfo = {}
    out, err = processutils.execute('lscpu')
    if err:
        msg = _("Unable to parse lscpu output.")
        raise exception.NovaException(msg)

    cpu = [line.strip('\n') for line in out.splitlines()]
    for line in cpu:
        if line.strip():
            name, value = line.split(':', 1)
            name = name.strip().lower()
            cpuinfo[name] = value.strip()

    f = open('/proc/cpuinfo', 'r')
    features = [line.strip('\n') for line in f.readlines()]
    for line in features:
        if line.strip():
            if line.startswith('flags'):
                name, value = line.split(':', 1)
                name = name.strip().lower()
                cpuinfo[name] = value.strip()

    return cpuinfo


def _get_ram_usage():
    """Get memory info."""
    with open('/proc/meminfo') as fp:
        m = fp.read().split()
        idx1 = m.index('MemTotal:')
        idx2 = m.index('MemFree:')
        idx3 = m.index('Buffers:')
        idx4 = m.index('Cached:')

        total = int(m[idx1 + 1])
        avail = int(m[idx2 + 1]) + int(m[idx3 + 1]) + int(m[idx4 + 1])

    return {
        'total': total * 1024,
        'used': (total - avail) * 1024
    }


def _host_has_swap():
    """Return whether the compute host has usable swap configured."""
    with open('/proc/meminfo') as fp:
        fields = dict(
            line.split(':', 1) for line in fp if ':' in line)

    return int(fields.get('SwapTotal', '0 kB').split()[0]) > 0


def _get_fs_info(path):
    """Get free/used/total disk space."""
    hddinfo = os.statvfs(path)
    total = hddinfo.f_blocks * hddinfo.f_bsize
    available = hddinfo.f_bavail * hddinfo.f_bsize
    used = total - available
    return {'total': total,
            'available': available,
            'used': used}


def _get_zpool_info(pool_or_dataset):
    """Get the free/used/total diskspace in a zfs pool or dataset.
    A dataset is distinguished by having a '/' in the string.

    :param pool_or_dataset: The string name of the pool or dataset
    :type pool_or_dataset: str
    :returns: dictionary with keys 'total', 'available', 'used'
    :rtype: Dict[str, int]
    :raises: :class:`exception.NovaException`
    :raises: :class:`oslo.concurrency.PorcessExecutionError`
    :raises: :class:`OSError`
    """
    def _get_zfs_attribute(cmd, attribute):
        value, err = processutils.execute(cmd, 'list',
                                   '-o', attribute,
                                   '-H',
                                   '-p',
                                   pool_or_dataset,
                                   run_as_root=True)
        if err:
            msg = _("Unable to parse zfs output.")
            raise exception.NovaException(msg)
        value = int(value.strip())
        return value

    if '/' in pool_or_dataset:
        # it's a dataset:
        # for zfs datasets we only have 'available' and 'used' and so need to
        # construct the total from available and used.
        used = _get_zfs_attribute('zfs', 'used')
        available = _get_zfs_attribute('zfs', 'available')
        total = available + used
    else:
        # otherwise it's a zpool
        total = _get_zfs_attribute('zpool', 'size')
        used = _get_zfs_attribute('zpool', 'alloc')
        available = _get_zfs_attribute('zpool', 'free')
    return {'total': total,
            'available': available,
            'used': used}


def _get_storage_pool_info(client, pool_name, pool=None):
    """Return capacity reported by the configured Incus storage pool."""
    if pool is None:
        pool = client.storage_pools.get(pool_name)
    resources = pool.resources.get()
    total = resources.space['total']
    used = resources.space['used']
    return {
        'total': total,
        'used': used,
        'available': max(0, total - used),
    }


def _placement_storage_pool_info(
        client, pool_name, shared_capacity_gb=None, pool=None):
    """Return node-owned capacity without duplicating a shared Ceph pool."""
    if pool is None:
        pool = client.storage_pools.get(pool_name)
    if pool.driver not in ('ceph', 'cephext'):
        if shared_capacity_gb is not None:
            raise exception.InvalidConfiguration(
                'Shared capacity is configured for node-local Incus pool {}'
                .format(pool_name))
        return _get_storage_pool_info(client, pool_name, pool=pool)

    if shared_capacity_gb is None:
        raise exception.InvalidConfiguration(
            'Shared Incus pool {} requires an explicit per-compute '
            'Placement capacity budget'.format(pool_name))
    try:
        capacity_gb = int(shared_capacity_gb)
    except (TypeError, ValueError):
        capacity_gb = 0
    if capacity_gb < 1:
        raise exception.InvalidConfiguration(
            'Shared Incus pool {} capacity budget must be a positive GiB '
            'value'.format(pool_name))

    total = capacity_gb * units.Gi
    return {'total': total, 'used': 0, 'available': total}


def _quiesced_inventory(current_inventory, resource_class):
    """Preserve an existing Placement inventory while blocking new claims."""
    previous = current_inventory.get(resource_class)
    if previous is None:
        return None
    quiesced = copy.deepcopy(previous)
    quiesced['reserved'] = quiesced['total']
    return quiesced


def _incus_hypervisor_version(host_info):
    """Return Incus' server version in Nova's integer version format."""
    server_version = host_info.get('environment', {}).get('server_version')
    try:
        return versionutils.convert_version_to_int(server_version)
    except (TypeError, ValueError):
        LOG.warning(
            'Incus returned an invalid server version %r; reporting 0',
            server_version)
        return 0


def _get_power_state(incus_state):
    """Take a incus state code and translate it to nova power state.

    The codes are Incus' own, from shared/api/status_code.go. Nova has
    no value for "in transition", so codes that genuinely describe one
    become NOSTATE - but a code describing a settled state has to map to
    that state.
    """
    state_map = [
        # 111 is Thawed: the guest resumed after a freeze and is running
        # again. Reporting NOSTATE for it made every unpause look to
        # Nova's power-state sync like an instance whose state was lost.
        (power_state.RUNNING, {100, 101, 103, 111, 200}),
        (power_state.SHUTDOWN, {102, 104, 107}),
        # Pending, Starting, Freezing, Ready, Cancelled: mid-transition,
        # or an operation outcome rather than a guest state.
        (power_state.NOSTATE, {105, 106, 109, 113, 401}),
        (power_state.CRASHED, {108, 112, 400}),
        (power_state.PAUSED, {110}),
    ]
    for nova_state, incus_states in state_map:
        if incus_state in incus_states:
            return nova_state
    # A code this driver has not seen is no reason to fail the caller:
    # this feeds get_info, and so Nova's periodic power-state sync, which
    # would then keep breaking for that instance until the driver caught
    # up with a newer Incus. NOSTATE is Nova's own value for "unknown".
    LOG.warning(
        'Unknown Incus power state %r; reporting it as NOSTATE',
        incus_state)
    return power_state.NOSTATE


def _sync_glance_image_to_incus(client, context, image_ref):
    """Sync an image from glance to Incus image store.

    The image from glance can't go directly into the Incus image store,
    as Incus needs some extra metadata connected to it.

    The image is stored in the Incus image store with an alias to
    the image_ref. This way, it will only copy over once.
    """
    # lockutils.lock's first parameter is the lock NAME, and the name alone
    # keys the in-process semaphore; lock_file_prefix reaches only the
    # on-disk lock. Passing a constant path here shared one mutex across
    # every image sync, container destroy and snapshot in this process.
    with lockutils.lock(
            'incus-image-{}'.format(image_ref), external=True):

        # NOTE(jamespage): Re-query by image_ref to ensure
        #                  that another process did not
        #                  sneak infront of this one and create
        #                  the same image already.
        try:
            client.images.get_by_alias(image_ref)
            return
        except incus_exceptions.LXDAPIException as e:
            if not _is_incus_not_found(e):
                raise

        # GB-scale images must not land on /tmp (commonly tmpfs backed by
        # RAM); stage them next to the instances the download serves.
        staging_dir = os.path.join(CONF.instances_path, 'image-staging')
        os.makedirs(staging_dir, exist_ok=True)
        ifd = mfd = None
        image_file = manifest_file = None
        try:
            ifd, image_file = tempfile.mkstemp(dir=staging_dir)
            mfd, manifest_file = tempfile.mkstemp(dir=staging_dir)

            image = IMAGE_API.get(context, image_ref)
            if image.get('disk_format') not in ACCEPTABLE_IMAGE_FORMATS:
                raise exception.ImageUnacceptable(
                    image_id=image_ref, reason=_("Bad image format"))
            IMAGE_API.download(context, image_ref, dest_path=image_file)

            # It is possible that Incus already have the same image
            # but NOT aliased as result of previous publish/export operation
            # (snapshot from openstack).
            # In that case attempt to add it again
            # (implicitly via instance launch from affected image) will produce
            # Incus error - "Image with same fingerprint already exists".
            # Error does not have unique identifier to handle it we calculate
            # Calculate the fingerprint as Incus does and check whether the
            # image is already available.
            # image with such fingerprint.
            # If any we will add alias to this image and will not re-import it
            def add_alias():

                def incusimage_fingerprint():
                    def sha256_file():
                        sha256 = hashlib.sha256()
                        with closing(open(image_file, 'rb')) as f:
                            for block in iter(lambda: f.read(65536), b''):
                                sha256.update(block)
                        return sha256.hexdigest()

                    return sha256_file()

                fingerprint = incusimage_fingerprint()
                if client.images.exists(fingerprint):
                    LOG.info("Image with fingerprint {fingerprint} already "
                             "exists but not accessible by alias {alias}, "
                             "add alias"
                             .format(fingerprint=fingerprint, alias=image_ref))
                    incusimage = client.images.get(fingerprint)
                    incusimage.add_alias(image_ref, '')
                    return True

                return False

            if add_alias():
                return

            # up2date Incus publish/export operations produce images which
            # already contains /rootfs and metdata.yaml in exported file.
            # We should not pass metdata explicitly in that case as imported
            # image will be unusable bacause Incus will think that it containts
            # rootfs and will not extract embedded /rootfs properly.
            # Try to detect if image content already has metadata and not pass
            # explicit metadata in that case
            def imagefile_has_metadata(image_file):
                try:
                    with closing(tarfile.TarFile.open(
                        name=image_file, mode='r:*')) as tf:
                        try:
                            tf.getmember('metadata.yaml')
                            return True
                        except KeyError:
                            pass
                except tarfile.ReadError:
                    pass
                return False

            if imagefile_has_metadata(image_file):
                LOG.info("Image {alias} already has metadata, "
                         "skipping metadata injection..."
                         .format(alias=image_ref))
                with open(image_file, 'rb') as image:
                    image = client.images.create(image, wait=True)
            else:
                metadata = {
                    'architecture': image.get(
                        'hw_architecture',
                        obj_fields.Architecture.from_host()),
                    'creation_date': int(os.stat(image_file).st_ctime)}
                metadata_yaml = jsonutils.dumps(
                    metadata, sort_keys=True, indent=4,
                    separators=(',', ': '),
                    ensure_ascii=False).encode('utf-8') + b"\n"

                tarball = tarfile.open(manifest_file, "w:gz")
                tarinfo = tarfile.TarInfo(name='metadata.yaml')
                tarinfo.size = len(metadata_yaml)
                tarball.addfile(tarinfo, io.BytesIO(metadata_yaml))
                tarball.close()

                with open(manifest_file, 'rb') as manifest:
                    with open(image_file, 'rb') as image:
                        image = client.images.create(
                            image, metadata=manifest,
                            wait=True)

            image.add_alias(image_ref, '')

        finally:
            # Guard each teardown step: referencing a descriptor the failed
            # mkstemp never returned raised NameError here and masked the
            # original download or import error.
            for fd in (ifd, mfd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            for path in (image_file, manifest_file):
                if path is not None:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass


def brick_get_connector_properties(multipath=None, enforce_multipath=None):
    """Wrapper to automatically set root_helper in brick calls.
    :param multipath: A boolean indicating whether the connector can
                      support multipath.
    :param enforce_multipath: If True, it raises exception when multipath=True
                              is specified but multipathd is not running.
                              If False, it falls back to multipath=False
                              when multipathd is not running.
    """

    if multipath is None:
        multipath = CONF.incus.volume_use_multipath
    if enforce_multipath is None:
        enforce_multipath = CONF.incus.volume_enforce_multipath
    root_helper = utils.get_root_helper()
    return connector.get_connector_properties(root_helper,
                                              CONF.my_ip,
                                              multipath,
                                              enforce_multipath,
                                              host=CONF.host)


def brick_get_connector(protocol, driver=None,
                        use_multipath=None,
                        device_scan_attempts=None,
                        enforce_multipath=None,
                        *args, **kwargs):
    """Wrapper to get a brick connector object.
    This automatically populates the required protocol as well
    as the root_helper needed to execute commands.
    """

    if use_multipath is None:
        use_multipath = CONF.incus.volume_use_multipath
    if device_scan_attempts is None:
        device_scan_attempts = CONF.incus.num_volume_scan_tries
    if enforce_multipath is None:
        enforce_multipath = CONF.incus.volume_enforce_multipath
    root_helper = utils.get_root_helper()
    if protocol.upper() == "RBD":
        kwargs['do_local_attach'] = True
    return connector.InitiatorConnector.factory(
        protocol, root_helper,
        driver=driver,
        use_multipath=use_multipath,
        device_scan_attempts=device_scan_attempts,
        enforce_multipath=enforce_multipath,
        *args, **kwargs)


def _loaded_instance_system_metadata(instance):
    attr_is_set = getattr(instance, 'obj_attr_is_set', None)
    if callable(attr_is_set) and not attr_is_set('system_metadata'):
        return {}
    return getattr(instance, 'system_metadata', None) or {}


def _profile_instance_users(profile):
    """Return the instance names an Incus profile reports as users."""
    names = set()
    for reference in getattr(profile, 'used_by', None) or ():
        if not isinstance(reference, str):
            # An unreadable reference is not proof that this instance owns
            # it, so surface it as a distinct user and let the caller treat
            # the profile as in use by something else.
            names.add(reference)
            continue
        path = parse.urlparse(reference).path.rstrip('/')
        marker = '/instances/'
        index = path.rfind(marker)
        if index < 0:
            marker = '/containers/'
            index = path.rfind(marker)
        names.add(
            path[index + len(marker):] if index >= 0 else path)
    return names


def _profile_users_other_than(profile, instance):
    """Return profile users that are not this instance's own containers.

    A cleanup that could not finish leaves this instance's own container
    behind, which is exactly why the profile is retained. Reading that as
    "the profile is in use" would refuse to mark the profile for recovery
    at the one moment the marker is needed, so only another instance's
    usage makes the profile foreign here.
    """
    own = {instance.name, '{}-rescue'.format(instance.name)}
    return sorted(_profile_instance_users(profile) - own)


@dataclass(frozen=True)
class _AllProjectIDMapInventory:
    """One immutable, pre-indexed all-project Incus inventory.

    The inventory is used only as a fail-closed screening snapshot.  Its
    indexes make a batch of release candidates O(records + candidates)
    instead of scanning every record again for every candidate.  A release
    is still authorized by a fresh inventory fetched immediately before the
    exact claim/range CAS.
    """

    by_uuid: dict
    by_range: dict
    invalid_ranges: frozenset
    by_name: dict
    by_materialization: dict


def _idmap_inventory_add(index, key, resource):
    if key is None:
        return
    index.setdefault(key, set()).add(resource)


def _idmap_inventory_uuid(value):
    """Return one UUID identity key independent of textual spelling."""
    try:
        return str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        return None


def _idmap_inventory_range(base, size):
    """Return a canonical numeric ID-map range, else no usable key."""
    if size is None or str(size).strip().lower() in ('', 'auto'):
        size = 65536
    try:
        base = int(base)
        size = int(size)
    except (TypeError, ValueError):
        return None
    if base < 0 or base > ((1 << 32) - 1) or size <= 0:
        return None
    if size > (1 << 32) or base + size > (1 << 32):
        return None
    return str(base), str(size)


def _idmap_inventory_ranges_overlap(left, right):
    """Return whether two canonical half-open ID-map ranges overlap."""
    if left is None or right is None:
        return False
    left_base, left_size = (int(value) for value in left)
    right_base, right_size = (int(value) for value in right)
    return (left_base < right_base + right_size and
            right_base < left_base + left_size)


def _all_project_idmap_inventory(client):
    """Return one all-project instance and profile listing.

    A periodic that evaluates a batch of candidates fetches this once and
    screens every candidate against it, instead of issuing two unscoped
    recursive listings per candidate. The exact proof that authorizes an
    actual release still refetches; see the callers.
    """
    params = {'recursion': 1, 'all-projects': True}
    by_uuid = {}
    by_range = {}
    invalid_ranges = set()
    by_name = {}
    by_materialization = {}
    for endpoint, resource_type in (
            ('instances', 'instance'), ('profiles', 'profile')):
        response = getattr(client.api, endpoint).get(params=params)
        try:
            body = response.json()
        except Exception as exc:
            raise incus_idmap.IDMapIntegrityError(
                'Incus all-project {} inventory is not JSON'.format(
                    resource_type)) from exc
        records = body.get('metadata') if isinstance(body, dict) else None
        if not isinstance(records, list):
            raise incus_idmap.IDMapIntegrityError(
                'Incus all-project {} inventory is malformed'.format(
                    resource_type))
        for record in records:
            if not isinstance(record, dict):
                raise incus_idmap.IDMapIntegrityError(
                    'Incus all-project {} inventory contains a malformed '
                    'record'.format(resource_type))
            project = record.get('project')
            name = record.get('name')
            if (not isinstance(project, str) or not project or
                    not isinstance(name, str) or not name):
                raise incus_idmap.IDMapIntegrityError(
                    'Incus all-project {} inventory contains an invalid '
                    'resource identity'.format(resource_type))
            resource = (resource_type, project, name)
            _idmap_inventory_add(
                by_name, (project, name), resource)
            local_config = record.get('config')
            if (not isinstance(local_config, dict) or
                    any(not isinstance(key, str) or
                        not isinstance(value, str)
                        for key, value in local_config.items())):
                raise incus_idmap.IDMapIntegrityError(
                    'Incus all-project {} inventory contains malformed '
                    'config'.format(resource_type))
            effective_config = {}
            if resource_type == 'instance':
                # Instance expanded_config is omitted when empty. Profiles do
                # not expose this field at all, so only instances consume it.
                expanded_config = record.get('expanded_config', {})
                if (not isinstance(expanded_config, dict) or
                        any(not isinstance(key, str) or
                            not isinstance(value, str)
                            for key, value in expanded_config.items())):
                    raise incus_idmap.IDMapIntegrityError(
                        'Incus all-project instance inventory contains '
                        'malformed expanded_config')
                effective_config.update(expanded_config)
            effective_config.update(local_config)
            _idmap_inventory_add(
                by_uuid, _idmap_inventory_uuid(
                    effective_config.get('user.openstack.uuid')), resource)
            volatile_base = effective_config.get('volatile.idmap.base')
            security_base = effective_config.get('security.idmap.base')
            base = volatile_base or security_base
            size = effective_config.get('security.idmap.size')
            idmap_range = _idmap_inventory_range(base, size)
            if ((volatile_base is not None or security_base is not None) and
                    idmap_range is None):
                # A malformed configured range cannot prove absence. Incus
                # normally validates these values, but legacy/corrupt records
                # must retain every candidate rather than disappear here.
                invalid_ranges.add(resource)
            else:
                _idmap_inventory_add(by_range, idmap_range, resource)
            _idmap_inventory_add(
                by_materialization,
                effective_config.get(IDMAP_MATERIALIZATION_CONFIG_KEY),
                resource)

    def freeze(index):
        return {
            key: frozenset(values) for key, values in index.items()
        }

    return _AllProjectIDMapInventory(
        by_uuid=freeze(by_uuid),
        by_range=freeze(by_range),
        invalid_ranges=frozenset(invalid_ranges),
        by_name=freeze(by_name),
        by_materialization=freeze(by_materialization))


def _all_project_idmap_resources_absent(
        client, instance_uuid, idmap_base, idmap_size,
        allowed_profile_name=None, inventory=None):
    """Prove no Incus project retains this UUID or overlapping idmap range.

    ``inventory`` screens against a snapshot the caller already fetched for
    a whole batch. A snapshot can only be stale in the direction of listing
    a resource that has since gone, so a match remains authoritative
    evidence to retain. Absence from a snapshot is only a screen: the caller
    must repeat this proof without one immediately before it releases.
    """
    expected_range = _idmap_inventory_range(idmap_base, idmap_size)
    expected_uuid = _idmap_inventory_uuid(instance_uuid)
    if expected_range is None or expected_uuid is None:
        raise incus_idmap.IDMapIntegrityError(
            'Cannot prove absence for an invalid ID map owner or range')
    if (inventory is None and
            client.has_api_extension('idmap_usage') is True):
        response = client.api['idmap-usage'].get(params={
            'owner': expected_uuid,
            'base': expected_range[0],
            'size': expected_range[1],
        })
        metadata = response.json().get('metadata')
        if not isinstance(metadata, list):
            raise incus_idmap.IDMapIntegrityError(
                'Incus ID map usage response is not a list')
        matches = set()
        for record in metadata:
            if not isinstance(record, dict):
                raise incus_idmap.IDMapIntegrityError(
                    'Incus ID map usage response contains a malformed '
                    'record')
            resource_type = record.get('type')
            project = record.get('project')
            name = record.get('name')
            if (resource_type not in ('instance', 'profile') or
                    not isinstance(project, str) or not project or
                    not isinstance(name, str) or not name):
                raise incus_idmap.IDMapIntegrityError(
                    'Incus ID map usage response contains an invalid '
                    'resource identity')
            matches.add((resource_type, project, name))
    else:
        if inventory is None:
            inventory = _all_project_idmap_inventory(client)
        matches = set(inventory.invalid_ranges)
        matches.update(inventory.by_uuid.get(expected_uuid, ()))
        for candidate_range, resources in inventory.by_range.items():
            if _idmap_inventory_ranges_overlap(
                    expected_range, candidate_range):
                matches.update(resources)
    for resource_type, project, name in sorted(
            matches, key=lambda value: tuple(str(item) for item in value)):
        if (resource_type == 'profile' and allowed_profile_name and
                name == allowed_profile_name and
                project == CONF.incus.project):
            continue
        LOG.critical(
            'Incus %(resource)s %(project)s/%(name)s still matches '
            'Nova instance %(uuid)s or idmap %(base)s:%(size)s; '
            'retaining its idmap host claim',
            {
                'resource': resource_type,
                'project': project or '<unknown>',
                'name': name or '<unknown>',
                'uuid': instance_uuid,
                'base': expected_range[0],
                'size': expected_range[1],
            })
        return False
    return True


def _all_project_spawn_attempt_resources_absent(
        client, attempt, inventory=None):
    """Prove a preflight attempt has no Incus-local resource or token.

    ``inventory`` has the same screening semantics as in
    :func:`_all_project_idmap_resources_absent`.
    """
    if inventory is None:
        inventory = _all_project_idmap_inventory(client)
    matches = set(inventory.by_name.get(
        (CONF.incus.project, attempt['instance_name']), ()))
    matches.update(inventory.by_uuid.get(
        _idmap_inventory_uuid(attempt['instance_uuid']), ()))
    matches.update(inventory.by_materialization.get(
        attempt['attempt_uuid'], ()))
    for resource_type, project, name in sorted(
            matches, key=lambda value: tuple(str(item) for item in value)):
        LOG.critical(
            'Incus %(resource)s %(project)s/%(name)s still matches '
            'preflight spawn attempt %(attempt)s for Nova instance '
            '%(uuid)s',
            {
                'resource': resource_type,
                'project': project or '<unknown>',
                'name': name or '<unknown>',
                'attempt': attempt['attempt_uuid'],
                'uuid': attempt['instance_uuid'],
            })
        return False
    return True


def _initial_data_volume_image_capability(instance, image_meta):
    """Read a custom Glance capability without changing Nova objects."""
    metadata_key = 'image_{}'.format(INCUS_DATA_VOLUME_IMAGE_PROPERTY)
    value = _loaded_instance_system_metadata(instance).get(metadata_key)
    if value is None:
        properties = getattr(image_meta, 'properties', None)
        getter = getattr(properties, 'get', None)
        if callable(getter):
            # ImageMetaProps.get raises AttributeError for custom
            # properties that are not registered Nova object fields;
            # an absent capability must read as False, not blow up the
            # scheduler with three pointless build retries.
            try:
                value = getter(INCUS_DATA_VOLUME_IMAGE_PROPERTY)
            except AttributeError:
                value = None
        elif isinstance(image_meta, dict):
            properties = image_meta.get('properties') or {}
            value = properties.get(INCUS_DATA_VOLUME_IMAGE_PROPERTY)
    return _is_explicit_true(value)


def _require_initial_data_volume_image_capability(
        instance, image_meta, data_volume_bdms):
    if not data_volume_bdms:
        return
    if _initial_data_volume_image_capability(instance, image_meta):
        return
    raise exception.ImageUnacceptable(
        image_id=instance.image_ref or 'unknown',
        reason=(
            'Initial Cinder data volumes require Glance image property '
            '{}=true and the configured guest FUSE mount helpers'.format(
                INCUS_DATA_VOLUME_IMAGE_PROPERTY)))


def _instance_idmap_metadata(instance):
    """Return the Nova-owned idmap assignment stored on an instance."""
    metadata = _loaded_instance_system_metadata(instance)
    values = (
        metadata.get(IDMAP_BASE_METADATA_KEY),
        metadata.get(IDMAP_SIZE_METADATA_KEY),
        metadata.get(IDMAP_ALLOCATION_METADATA_KEY),
        metadata.get(IDMAP_FINGERPRINT_METADATA_KEY),
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise incus_idmap.IDMapIntegrityError(
            'Nova instance has an incomplete Incus idmap assignment')
    try:
        base = int(values[0])
        size = int(values[1])
    except (TypeError, ValueError) as exc:
        raise incus_idmap.IDMapIntegrityError(
            'Nova instance has invalid Incus idmap metadata') from exc
    return {
        'base': base,
        'size': size,
        'allocation_id': str(values[2]),
        'fingerprint': str(values[3]),
    }


def _idmap_generation_matches_metadata(generation, metadata):
    return (
        metadata is not None and
        generation.base == metadata['base'] and
        generation.size == metadata['size'] and
        generation.allocation_id == metadata['allocation_id'] and
        generation.fingerprint == metadata['fingerprint']
    )


def _same_idmap_generation(left, right):
    fields = (
        'instance_uuid', 'base', 'size', 'slot', 'allocation_id',
        'fingerprint')
    return all(getattr(left, field) == getattr(right, field)
               for field in fields)


def _idmap_retirement_proof(generation, host_id, materialization_id):
    """Return canonical proof that one host retired an exact generation."""
    materialization_id = _canonical_materialization_id(materialization_id)
    return json.dumps({
        'allocation_id': generation.allocation_id,
        'base': generation.base,
        'fingerprint': generation.fingerprint,
        'host_id': host_id,
        'instance_uuid': generation.instance_uuid,
        'materialization_id': materialization_id,
        'schema': 2,
        'size': generation.size,
        'slot': generation.slot,
    }, sort_keys=True, separators=(',', ':'))


def _parse_idmap_retirement_proof(raw):
    required = {
        'allocation_id', 'base', 'fingerprint', 'host_id', 'instance_uuid',
        'materialization_id', 'schema', 'size', 'slot',
    }
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != required:
            raise ValueError('invalid schema')
        host_id = str(uuid.UUID(value['host_id']))
        instance_uuid = str(uuid.UUID(value['instance_uuid']))
        allocation_id = str(uuid.UUID(value['allocation_id']))
        materialization_id = str(uuid.UUID(value['materialization_id']))
        proof = incus_idmap.IDMapHostClaim(
            host_id=host_id,
            materialization_id=materialization_id,
            instance_uuid=instance_uuid,
            base=int(value['base']),
            size=int(value['size']),
            slot=int(value['slot']),
            allocation_id=allocation_id,
            fingerprint=str(value['fingerprint']),
            state='cleaned')
    except (AttributeError, TypeError, ValueError) as exc:
        raise incus_idmap.IDMapIntegrityError(
            'Incus cleanup acknowledgement has invalid idmap retirement '
            'proof') from exc
    if value['schema'] != 2 or raw != _idmap_retirement_proof(
            proof, host_id, materialization_id):
        raise incus_idmap.IDMapIntegrityError(
            'Incus cleanup acknowledgement has non-canonical idmap '
            'retirement proof')
    return proof


class IncusDriver(driver.ComputeDriver):
    """A Incus driver for nova.

    Incus is a system container hypervisor. IncusDriver provides Incus
    functionality to nova. For more information about Incus, see
    http://www.ubuntu.com/cloud/incus
    """

    capabilities = dict(
        driver.ComputeDriver.capabilities,
        has_imagecache=True,
        supports_attach_interface=True,
        supports_device_tagging=False,
        supports_tagged_attach_interface=False,
        supports_tagged_attach_volume=False,
        supports_extend_volume=True,
        supports_multiattach=False,
        supports_bfv_rescue=False,
        supports_vtpm=False,
        supports_secure_boot=False,
        supports_accelerators=False,
        supports_virtio_fs=False,
        supports_mem_backing_file=False,
        supports_image_type_raw=True,
        supports_migrate_to_same_host=False,
    )

    def __init__(self, virtapi):
        super(IncusDriver, self).__init__(virtapi)

        # Capabilities are consumed per compute service. Never mutate the
        # class dictionary when applying a host-local operator policy.
        self.capabilities = dict(type(self).capabilities)
        self.capabilities['supports_evacuate'] = (
            CONF.incus.allow_bfv_evacuate)
        self.client = None  # Initialized by init_host
        self.inventory_client = None  # Unscoped all-project safety queries.
        self.idmap_allocator = None
        self.storage_ownership = None
        self.volume_api = cinder.API()
        self.host = NOVA_CONF.host
        self.network_api = neutron.API()
        self.vif_driver = incus_vif.IncusGenericVifDriver()
        self.firewall_driver = _NeutronFirewallDriver()
        self._serial_consoles = {}
        self._serial_consoles_lock = threading.Lock()
        self._serial_console_destroying = set()
        self._host_resource_cache = {}
        self._host_resource_cache_lock = threading.Lock()
        self._disk_metrics_cache = None
        self._metric_devices_cache = None
        self._metric_instance_devices_cache = {}
        self._instance_inventory_cache = None
        self._profile_inventory_cache = None
        # All mutable-inventory caches share one generation and condition.
        # Fetchers claim a generation while holding this condition, perform
        # Incus I/O after releasing it, and publish only if the generation is
        # still current.  This avoids both lock-order cycles and network I/O
        # under a process mutex while retaining one in-flight fetch per cache.
        self._inventory_cache_condition = threading.Condition()
        self._inventory_cache_generation = 0
        self._instance_inventory_fetch_generation = None
        self._profile_inventory_fetch_generation = None
        self._disk_metrics_fetch_generation = None
        self._metric_devices_fetch_generation = None
        self._metric_instance_devices_fetch_generations = {}

    def _validate_initial_volume_encryption(
            self, context, root_bdm, data_volume_bdms):
        """Reject every encrypted initial Cinder BDM before side effects."""
        bdms = ([root_bdm] if root_bdm is not None else [])
        bdms.extend(data_volume_bdms)
        encrypted = []
        for bdm in bdms:
            connection_info = bdm.get('connection_info') or {}
            volume_id = _bdm_volume_id(bdm)
            encryption = self.volume_api.get_volume_encryption_metadata(
                context, volume_id)
            # Cinder returns a truthy resource object even for unencrypted
            # volumes; only a populated encryption provider or key ID marks
            # the volume as encrypted.
            if not isinstance(encryption, dict):
                encryption = {
                    key: getattr(encryption, key, None)
                    for key in ('provider', 'encryption_key_id')}
            volume_encrypted = bool(
                encryption.get('provider') or
                encryption.get('encryption_key_id'))
            if (volume_encrypted or _has_encryption_marker(
                    (connection_info.get('data') or {}).get('encrypted'))):
                encrypted.append((
                    connection_info.get('driver_volume_type', 'unknown'),
                    volume_id))
        if encrypted:
            volume_type, volume_id = encrypted[0]
            raise exception.VolumeEncryptionNotSupported(
                volume_type=volume_type, volume_id=volume_id)

    def _preflight_data_volume_bdms(
            self, data_volume_bdms, validate_connectors=True):
        """Validate every initial data BDM before any connector is invoked."""
        extensions = self.client.host_info.get('api_extensions', [])
        protocols = set()
        for bdm in data_volume_bdms:
            connection_info = bdm.get('connection_info') or {}
            volume_id = _bdm_volume_id(bdm)
            connection_volume_id = _volume_id(connection_info)
            if connection_volume_id != volume_id:
                raise exception.InvalidVolume(
                    reason='Cinder block-device mapping volume ID %s does not '
                           'match connection_info volume ID %s' %
                           (volume_id, connection_volume_id))
            mountpoint = _validate_volume_mountpoint(bdm.get('mount_device'))
            _validate_volume_access_mode(connection_info)
            _data_volume_qos(connection_info, extensions)

            protocol = connection_info.get('driver_volume_type')
            if not isinstance(protocol, str) or not protocol.strip():
                raise exception.InvalidVolume(
                    reason='Cinder volume %s has no driver_volume_type' %
                           volume_id)
            _validate_recoverable_data_volume(connection_info, volume_id)
            if (connection_info.get('multiattach') or
                    (connection_info.get('data') or {}).get('multiattach')):
                raise exception.MultiattachNotSupportedByVirtDriver(
                    volume_id=volume_id)

            # This also proves the recovery record can be serialized before
            # the first os-brick connection creates host state.
            _serialize_volume_attachment(
                connection_info, {}, mountpoint, phase='connecting')
            protocols.add(protocol)

        if not validate_connectors:
            return
        for protocol in sorted(protocols):
            try:
                brick_get_connector(protocol)
            except brick_exception.InvalidConnectorProtocol as exc:
                raise exception.InvalidVolume(
                    reason='Cinder connector protocol %s is unsupported' %
                           protocol) from exc

    @staticmethod
    def _spawn_build_abort(instance, exc):
        reason = (
            exc.format_message()
            if hasattr(exc, 'format_message') else str(exc))
        raise exception.BuildAbortException(
            instance_uuid=instance.uuid, reason=reason) from exc

    @staticmethod
    @contextlib.contextmanager
    def _timed_phase(instance, operation, phase):
        """Log one structured duration line for an operation phase.

        The line is emitted on success and on failure alike, so an aborted
        operation still records where its time went. Fields are structured
        (operation=, phase=, outcome=, duration_ms=) for direct aggregation
        from journal output.
        """
        watch = timeutils.StopWatch()
        watch.start()
        outcome = 'ok'
        try:
            yield
        except BaseException:
            outcome = 'error'
            raise
        finally:
            LOG.info(
                'timing operation=%(operation)s phase=%(phase)s '
                'outcome=%(outcome)s duration_ms=%(ms)d',
                {'operation': operation, 'phase': phase,
                 'outcome': outcome,
                 'ms': int(watch.elapsed() * 1000)},
                instance=instance)

    def init_host(self, host):
        """Initialize the driver on the host.

        The pylxd Client is initialized. This initialization may raise
        an exception if the Incus instance cannot be found.

        The `host` argument is ignored here, as the Incus instance is
        assumed to be on the same system as the compute worker
        running this code. This is by (current) design.

        See `nova.virt.driver.ComputeDriver.init_host` for more
        information.
        """
        if (CONF.incus.volume_enforce_multipath and
                not CONF.incus.volume_use_multipath):
            raise exception.InvalidConfiguration(
                '[incus] volume_enforce_multipath requires '
                'volume_use_multipath')
        # Fail fast on static root-storage capacity misconfiguration; the
        # resource-tracker path only degrades at runtime.
        for selector, resource_class in (
                CONF.incus.root_storage_pool_resource_classes.items()):
            if not CONF.incus.root_storage_pools.get(selector):
                raise exception.InvalidConfiguration(
                    'Capacity-tracked Incus root storage selector {} is not '
                    'present in root_storage_pools'.format(selector))
            if not resource_class.startswith('CUSTOM_'):
                raise exception.InvalidConfiguration(
                    'Incus root storage resource class {} must start with '
                    'CUSTOM_'.format(resource_class))
        if CONF.incus.enable_manila_shares:
            try:
                incus_privsep.validate_gnu_timeout()
            except Exception as exc:
                raise exception.InvalidConfiguration(
                    'Incus Manila shares require GNU coreutils timeout at '
                    '{}: {}'.format(
                        incus_privsep.GNU_TIMEOUT_PATH, exc)) from exc
        try:
            self.client = incus_client.get_client(CONF)
            self.inventory_client = incus_client.get_client(
                CONF, project=None)
        except incus_exceptions.ClientConnectionFailed as e:
            msg = _("Unable to connect to Incus daemon: {}").format(e)
            raise exception.HostNotFound(msg)
        self.storage_ownership = (
            incus_storage_protocol.StorageOwnershipClient(self.client))
        _validate_root_storage_pool_accounting(self.client)
        _validate_boot_from_volume_storage_pools(self.client)

        migration_enabled = any((
            CONF.incus.allow_cold_migration,
            CONF.incus.allow_live_migration,
            CONF.incus.allow_bfv_evacuate,
        ))
        endpoint = CONF.incus.idmap_allocator_endpoint
        if migration_enabled and not endpoint:
            raise exception.InvalidConfiguration(
                '[incus] idmap_allocator_endpoint is required when cold/live '
                'migration or BFV evacuation is enabled')
        if endpoint:
            extensions = set(self.client.host_info.get('api_extensions', []))
            missing = sorted({
                'id_map', 'id_map_base',
                INCUS_STORAGE_MATERIALIZATION_ATTEMPT_EXTENSION,
                INCUS_STORAGE_RELEASE_RECEIPT_EXTENSION,
            } - extensions)
            if missing:
                raise exception.InvalidConfiguration(
                    'Incus global idmap allocation requires API extensions: '
                    '{}'.format(', '.join(missing)))
            try:
                self.idmap_allocator = incus_idmap.IDMapAllocator(
                    endpoint=endpoint,
                    namespace=CONF.incus.idmap_allocator_namespace,
                    base=CONF.incus.idmap_allocator_base,
                    size=CONF.incus.idmap_allocator_size,
                    count=CONF.incus.idmap_allocator_count,
                    timeout=CONF.incus.idmap_allocator_timeout,
                    ca_cert=CONF.incus.idmap_allocator_ca_cert,
                    cert_cert=CONF.incus.idmap_allocator_client_cert,
                    cert_key=CONF.incus.idmap_allocator_client_key,
                    username=CONF.incus.idmap_allocator_username,
                    password_file=(
                        CONF.incus.idmap_allocator_password_file),
                    allow_insecure=(
                        CONF.incus.idmap_allocator_allow_insecure),
                    audit_lease_ttl=max(
                        30,
                        CONF.incus.idmap_allocator_audit_interval * 3),
                )
            except incus_idmap.IDMapError as exc:
                raise exception.InvalidConfiguration(
                    'Invalid Incus idmap allocator configuration: {}'.format(
                        exc)) from exc
            try:
                self.idmap_allocator.initialize()
                # Elect one lease-backed fleet auditor. Concurrent compute
                # starts do not each scan the entire registry; followers are
                # admitted only after their sensitive paths observe and CAS
                # against the elected auditor's exact healthy generation.
                self.idmap_allocator.run_coordinated_audit(full=True)
            except incus_idmap.IDMapBackendError:
                # Existing running workloads and stop remain available while
                # etcd is temporarily unavailable. Start, reboot, allocation,
                # migration and evacuation revalidate ownership and fail
                # closed.
                LOG.critical(
                    'Incus idmap allocator is unavailable; new instances, '
                    'start, reboot, migration and evacuation will fail '
                    'closed until it recovers', exc_info=True)

    def _storage_ownership_client(self, client=None):
        client = client or self.client
        if client is self.client and self.storage_ownership is not None:
            return self.storage_ownership
        return incus_storage_protocol.StorageOwnershipClient(client)

    def _create_spawn_preflight_attempt(self, instance, attempt_id):
        """Persist exact proof that spawn has not crossed its side effects."""
        if self.idmap_allocator is None:
            return None
        host_id = virt_node.read_local_node_uuid()
        if not host_id:
            raise incus_idmap.IDMapConfigurationError(
                'Nova has no persistent compute-node UUID')
        with lockutils.lock(
                _idmap_host_claim_lock_name(instance.uuid), external=True,
                lock_path=_idmap_host_claim_lock_path()):
            if self.idmap_allocator.get_release_intent(
                    instance.uuid) is not None:
                raise incus_idmap.IDMapConflict(
                    reason='Incus spawn is blocked by an idmap release intent')
            assignment = self.idmap_allocator.get(instance.uuid)
            stored = _instance_idmap_metadata(instance)
            if assignment is None:
                if stored is not None:
                    raise incus_idmap.IDMapIntegrityError(
                        'Nova idmap metadata has no allocator generation')
            else:
                if (assignment.instance_uuid != instance.uuid or
                        not _idmap_generation_matches_metadata(
                            assignment, stored)):
                    raise incus_idmap.IDMapIntegrityError(
                        'Rescheduled Incus spawn does not match its exact '
                        'idmap generation')
                for claimed_host in assignment.host_ids:
                    claim = self.idmap_allocator.get_host_claim(
                        instance.uuid, claimed_host)
                    if (claim is None or claim.host_id != claimed_host or
                            not _same_idmap_generation(claim, assignment) or
                            claim.state != 'cleaned' or claim.proof is None):
                        raise incus_idmap.IDMapConflict(
                            reason='Rescheduled Incus spawn has an uncleared '
                                   'idmap host claim')
            return _write_spawn_attempt_journal(
                instance, host_id, attempt_id, phase='preflight',
                generation=assignment)

    def _open_spawn_attempt(self, instance, attempt):
        """Durably close the no-side-effect path before allocator mutation."""
        if attempt is None:
            return None
        with lockutils.lock(
                _idmap_host_claim_lock_name(instance.uuid), external=True,
                lock_path=_idmap_host_claim_lock_path()):
            return _write_spawn_attempt_journal(
                instance, attempt['compute_uuid'], attempt['attempt_uuid'],
                phase='opening', expected_phase='preflight',
                generation=(attempt if _spawn_attempt_has_generation(attempt)
                            else None))

    def _finish_spawn_attempt_open(self, instance, attempt):
        """Remove a phase credential after its exact claim is durable."""
        if attempt is None:
            return
        with lockutils.lock(
                _idmap_host_claim_lock_name(instance.uuid), external=True,
                lock_path=_idmap_host_claim_lock_path()):
            current = self.idmap_allocator.get(instance.uuid)
            claim = self.idmap_allocator.get_host_claim(
                instance.uuid, attempt['compute_uuid'])
            if (current is None or claim is None or
                    claim.materialization_id != attempt['attempt_uuid'] or
                    not _same_idmap_generation(current, claim)):
                raise incus_idmap.IDMapIntegrityError(
                    'Incus spawn opening has no exact durable host claim')
            if (_spawn_attempt_has_generation(attempt) and
                    not _spawn_attempt_generation_matches(
                        attempt, current)):
                raise incus_idmap.IDMapIntegrityError(
                    'Incus spawn opening changed allocator generation')
            _remove_spawn_attempt_journal(instance, attempt)

    def _remove_spawn_attempt_for_claim(self, instance, claim):
        """Remove a leftover opening journal only for an exact live claim."""
        with lockutils.lock(
                _idmap_host_claim_lock_name(instance.uuid), external=True,
                lock_path=_idmap_host_claim_lock_path()):
            attempt = _read_spawn_attempt_journal(instance)
            if attempt is None:
                return
            expected = _spawn_attempt_payload(
                instance, claim.host_id, claim.materialization_id,
                phase='opening',
                generation=(attempt if _spawn_attempt_has_generation(attempt)
                            else None))
            if attempt != expected:
                raise incus_idmap.IDMapIntegrityError(
                    'Host spawn attempt does not match the exact idmap claim')
            if (_spawn_attempt_has_generation(attempt) and
                    not _spawn_attempt_generation_matches(attempt, claim)):
                raise incus_idmap.IDMapIntegrityError(
                    'Host spawn attempt has another idmap generation')
            current = self.idmap_allocator.get_host_claim(
                instance.uuid, claim.host_id)
            if (current is None or
                    current.materialization_id != claim.materialization_id or
                    not _same_idmap_generation(current, claim)):
                raise incus_idmap.IDMapIntegrityError(
                    'Host spawn attempt has no exact idmap claim')
            _remove_spawn_attempt_journal(instance, expected)

    def _consume_spawn_preflight_noop(self, instance):
        """Consume one exact preflight-only attempt as an empty destroy."""
        if self.idmap_allocator is None:
            return False
        attempt = _read_spawn_attempt_journal(instance)
        if attempt is None:
            return False
        host_id = virt_node.read_local_node_uuid()
        if not host_id or attempt['compute_uuid'] != host_id:
            raise incus_idmap.IDMapIntegrityError(
                'Incus spawn preflight belongs to another compute')

        with lockutils.lock(
                _idmap_host_claim_lock_name(instance.uuid), external=True,
                lock_path=_idmap_host_claim_lock_path()):
            current = _read_spawn_attempt_journal(instance)
            if current != attempt:
                raise incus_idmap.IDMapIntegrityError(
                    'Incus spawn preflight changed during destroy')
            assignment = self.idmap_allocator.get(instance.uuid)
            if attempt['phase'] == 'opening':
                claim = self.idmap_allocator.get_host_claim(
                    instance.uuid, host_id)
                if (assignment is None and claim is None and
                        not _spawn_attempt_has_generation(attempt)):
                    # The opening marker is durable before the allocator is
                    # touched. An exact empty registry state proves that the
                    # first allocation attempt made no shared-state change.
                    _remove_spawn_attempt_journal(instance, attempt)
                    return True
                if (assignment is not None and claim is not None and
                        claim.materialization_id == attempt['attempt_uuid'] and
                        _same_idmap_generation(assignment, claim) and
                        (not _spawn_attempt_has_generation(attempt) or
                         _spawn_attempt_generation_matches(
                             attempt, assignment))):
                    # register_materialization() may have committed server
                    # state even when its HTTP response was lost. The exact
                    # claim lets the ordinary destroy path discover and
                    # settle that attempt by token.
                    return False
                raise incus_idmap.IDMapConflict(
                    reason='Incus spawn crossed the durable opening boundary '
                           'without an exact host claim; refusing destroy')
            release_intent = self.idmap_allocator.get_release_intent(
                instance.uuid)
            if (release_intent is not None and
                    (assignment is None or
                     release_intent.instance_name != instance.name or
                     not _same_idmap_generation(
                         release_intent, assignment))):
                raise incus_idmap.IDMapIntegrityError(
                    'Incus spawn preflight has another release generation')
            stored = _instance_idmap_metadata(instance)
            if _spawn_attempt_has_generation(attempt):
                if (assignment is None or
                        not _spawn_attempt_generation_matches(
                            attempt, assignment) or
                        not _idmap_generation_matches_metadata(
                            assignment, stored)):
                    raise incus_idmap.IDMapIntegrityError(
                        'Incus reschedule preflight does not match its exact '
                        'idmap generation')
                local_claim = self.idmap_allocator.get_host_claim(
                    instance.uuid, host_id)
                if local_claim is not None or host_id in assignment.host_ids:
                    raise incus_idmap.IDMapConflict(
                        reason='Incus reschedule preflight already has a '
                               'local idmap host claim')
                for claimed_host in assignment.host_ids:
                    claim = self.idmap_allocator.get_host_claim(
                        instance.uuid, claimed_host)
                    if (claim is None or claim.host_id != claimed_host or
                            not _same_idmap_generation(claim, assignment) or
                            claim.state != 'cleaned' or
                            claim.proof is None):
                        raise incus_idmap.IDMapConflict(
                            reason='Incus reschedule preflight has an '
                                   'uncleared historical idmap host claim')
            else:
                if assignment is not None:
                    raise incus_idmap.IDMapIntegrityError(
                        'Incus spawn preflight unexpectedly has an idmap '
                        'allocation')
                if stored is not None:
                    raise incus_idmap.IDMapIntegrityError(
                        'Incus spawn preflight unexpectedly has Nova idmap '
                        'metadata')
            if (_volume_journal_records(instance) or
                    _share_journal_records(instance)):
                raise incus_idmap.IDMapIntegrityError(
                    'Incus spawn preflight unexpectedly has host attachment '
                    'journals')
            retained_paths = (
                common.InstanceAttributes(instance).instance_dir,
                _volume_journal_directory(instance),
                _share_journal_directory(instance),
            )
            if any(os.path.lexists(path) for path in retained_paths):
                raise incus_idmap.IDMapIntegrityError(
                    'Incus spawn preflight unexpectedly has a local host '
                    'resource path')
            if not _all_project_spawn_attempt_resources_absent(
                    self.inventory_client, attempt):
                raise incus_idmap.IDMapIntegrityError(
                    'Incus spawn preflight still has an Incus resource or '
                    'configuration token')
            if _read_spawn_attempt_journal(instance) != attempt:
                raise incus_idmap.IDMapIntegrityError(
                    'Incus spawn preflight changed before final cleanup')
            _remove_spawn_attempt_journal(instance, attempt)
            return True

    @staticmethod
    def _idmap_materialization_config(binding):
        return {
            IDMAP_ALLOCATION_CONFIG_KEY: binding.allocation_id,
            IDMAP_COMPUTE_CONFIG_KEY: binding.compute_id,
            IDMAP_MATERIALIZATION_CONFIG_KEY: binding.token,
            'user.openstack.uuid': binding.owner,
            'security.idmap.base': str(binding.idmap_base),
            'security.idmap.size': str(binding.idmap_size),
        }

    @staticmethod
    def _materialization_identity(instance_name, claim):
        return incus_storage_protocol.StorageMaterializationIdentity(
            token=claim.materialization_id,
            allocation_id=claim.allocation_id,
            compute_id=claim.host_id,
            owner=claim.instance_uuid,
            project=CONF.incus.project,
            instance_name=instance_name,
            idmap_base=claim.base,
            idmap_size=claim.size,
        )

    @staticmethod
    def _materialization_binding_from_proof(proof):
        return incus_storage_protocol.StorageMaterializationBinding(
            token=proof.token,
            allocation_id=proof.allocation_id,
            compute_id=proof.compute_id,
            owner=proof.owner,
            project=proof.project,
            instance_name=proof.instance_name,
            idmap_base=proof.idmap_base,
            idmap_size=proof.idmap_size,
            storage_driver=proof.storage_driver,
            storage_pool=proof.storage_pool,
            storage_volume=proof.storage_volume,
            cleanup_disposition=proof.cleanup_disposition,
            rbd_image=proof.rbd_image,
        )

    def _root_storage_materialization_binding(
            self, client, instance, assignment, compute_id,
            materialization_id, root_device, shared_migration=False):
        if not isinstance(root_device, dict):
            raise incus_idmap.IDMapConfigurationError(
                'Incus root materialization has no root disk device')
        pool_name = root_device.get('pool')
        if not isinstance(pool_name, str) or not pool_name:
            raise incus_idmap.IDMapConfigurationError(
                'Global idmap allocation requires an explicit Incus root '
                'storage pool')
        pool = client.storage_pools.get(pool_name)
        storage_driver = getattr(pool, 'driver', None)
        if not isinstance(storage_driver, str) or not storage_driver:
            raise incus_idmap.IDMapIntegrityError(
                'Incus root storage pool returned no driver identity')
        rbd_image = root_device.get('initial.ceph.rbd.image_name', '')
        if not isinstance(rbd_image, str):
            raise incus_idmap.IDMapConfigurationError(
                'Incus root RBD image name is invalid')
        if storage_driver == 'cephext' and not rbd_image:
            raise incus_idmap.IDMapConfigurationError(
                'Incus cephext root materialization requires an RBD image')
        if storage_driver != 'cephext' and rbd_image:
            raise incus_idmap.IDMapConfigurationError(
                'Only an Incus cephext root may claim an external RBD image')
        cleanup_disposition = 'delete'
        if storage_driver == 'cephext':
            cleanup_disposition = 'detach'
        elif shared_migration and storage_driver == 'ceph':
            cleanup_disposition = 'handover'
        return incus_storage_protocol.StorageMaterializationBinding(
            token=_canonical_materialization_id(materialization_id),
            allocation_id=assignment.allocation_id,
            compute_id=_canonical_materialization_id(
                compute_id, 'compute ID'),
            owner=instance.uuid,
            project=CONF.incus.project,
            instance_name=instance.name,
            idmap_base=assignment.base,
            idmap_size=assignment.size,
            storage_driver=storage_driver,
            storage_pool=pool_name,
            storage_volume=_incus_instance_storage_volume(
                CONF.incus.project, instance.name),
            cleanup_disposition=cleanup_disposition,
            rbd_image=rbd_image,
        )

    def _exact_idmap_host_claim(self, instance, claim):
        if not isinstance(claim, incus_idmap.IDMapHostClaim):
            raise incus_idmap.IDMapIntegrityError(
                'Incus idmap reconciliation requires an exact host claim')
        if instance is not None and claim.instance_uuid != instance.uuid:
            raise incus_idmap.IDMapIntegrityError(
                'Incus idmap host claim belongs to another instance')
        assignment = self.idmap_allocator.get(claim.instance_uuid)
        if assignment is None or not _same_idmap_generation(
                assignment, claim):
            raise incus_idmap.IDMapIntegrityError(
                'Incus idmap host claim has no exact allocation generation')
        current = self.idmap_allocator.get_host_claim(
            claim.instance_uuid, claim.host_id)
        if current is None:
            return assignment, None
        if (current.materialization_id != claim.materialization_id or
                not _same_idmap_generation(current, claim)):
            raise incus_idmap.IDMapConflict(
                reason='Another materialization owns the Incus host claim')
        if instance is not None:
            # An absent stamp is a build that died before Nova cached the
            # assignment; the registry pair above is the authority. Only a
            # present-but-different stamp is evidence of another generation.
            stored = _instance_idmap_metadata(instance)
            if (stored is not None and
                    not _idmap_generation_matches_metadata(
                        assignment, stored)):
                raise incus_idmap.IDMapIntegrityError(
                    'Nova metadata does not match the Incus host claim')
        return assignment, current

    def _begin_idmap_materialization(
            self, instance, materialization_id, root_device,
            observed_base=None, observed_size=None,
            shared_migration=False):
        """Claim and register a pristine root before any host side effect."""
        if self.idmap_allocator is None:
            self._ensure_instance_idmap(
                instance, observed_base=observed_base,
                observed_size=observed_size)
            return None
        materialization_id = _canonical_materialization_id(
            materialization_id)
        host_id = virt_node.read_local_node_uuid()
        if not host_id:
            raise incus_idmap.IDMapConfigurationError(
                'Nova has no persistent compute-node UUID')
        with lockutils.lock(
                _idmap_host_claim_lock_name(instance.uuid), external=True,
                lock_path=_idmap_host_claim_lock_path()):
            assignment = self._ensure_instance_idmap(
                instance, observed_base=observed_base,
                observed_size=observed_size)
            current = self.idmap_allocator.get(instance.uuid)
            if current is None or not _same_idmap_generation(
                    current, assignment):
                raise incus_idmap.IDMapIntegrityError(
                    'Allocator generation changed before materialization')
            self.idmap_allocator.claim(
                instance.uuid, host_id, materialization_id,
                assignment=current)
            claim = self.idmap_allocator.get_host_claim(
                instance.uuid, host_id)
            if (claim is None or
                    claim.materialization_id != materialization_id or
                    claim.state != 'unmaterialized'):
                raise incus_idmap.IDMapConflict(
                    reason='Incus materialization has no pristine exact claim')
            binding = self._root_storage_materialization_binding(
                self.client, instance, current, host_id,
                materialization_id, root_device,
                shared_migration=shared_migration)
            self._storage_ownership_client().register_materialization(binding)
            return _IDMapMaterialization(
                assignment=current, claim=claim, binding=binding,
                client=self.client)

    def _resume_idmap_materialization(
            self, client, instance, materialization_id, compute_id,
            root_device, observed_base, observed_size):
        """Rebuild an already-registered destination transaction."""
        if self.idmap_allocator is None:
            return None
        assignment = self._ensure_instance_idmap(
            instance, observed_base=observed_base,
            observed_size=observed_size)
        claim = self.idmap_allocator.get_host_claim(
            instance.uuid, compute_id)
        if (claim is None or
                claim.materialization_id != materialization_id or
                claim.state != 'unmaterialized'):
            raise incus_idmap.IDMapConflict(
                reason='Migration target has no exact unmaterialized claim')
        binding = self._root_storage_materialization_binding(
            client, instance, assignment, compute_id,
            materialization_id, root_device, shared_migration=True)
        attempt = self._storage_ownership_client(
            client).get_materialization(binding)
        if (attempt.state != 'active' or attempt.started or
                attempt.finished or attempt.storage_phase != 'none'):
            raise incus_idmap.IDMapConflict(
                reason='Migration target materialization is not pristine')
        return _IDMapMaterialization(
            assignment=assignment, claim=claim, binding=binding,
            client=client)

    def _record_and_ack_materialization_proof(
            self, materialization, proof):
        binding = materialization.binding
        claim = self.idmap_allocator.record_materialization_proof(
            binding.owner, binding.compute_id, binding.token, proof,
            assignment=materialization.assignment)
        try:
            self._storage_ownership_client(
                materialization.client).acknowledge_materialization_proof(
                    binding, proof)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
        return claim

    def _abort_idmap_materialization(self, materialization):
        if materialization is None:
            return None
        protocol = self._storage_ownership_client(materialization.client)
        attempt = protocol.get_materialization(materialization.binding)
        if attempt.state == 'committed':
            raise incus_idmap.IDMapConflict(
                reason='Committed root materialization cannot be aborted')
        if attempt.state == 'active':
            attempt = protocol.abort_materialization(
                materialization.binding)
        if attempt.proof is None:
            # Settling is refused while the target operation still runs. A
            # create that outlived the client read timeout is still running
            # at this point, so end it first; otherwise the 409 escapes and
            # turns a slow but recoverable build into a hard failure.
            _settle_incus_operation(
                materialization.client, attempt.operation_uuid)
            attempt = protocol.settle_materialization(
                materialization.binding)
        if attempt.proof is None:
            raise incus_idmap.IDMapIntegrityError(
                'Incus returned no terminal materialization proof')
        return self._record_and_ack_materialization_proof(
            materialization, attempt.proof)

    @staticmethod
    def _attempt_matches_idmap_claim(attempt, claim):
        binding = getattr(attempt, 'binding', None)
        return (
            binding is not None and
            binding.token == claim.materialization_id and
            binding.allocation_id == claim.allocation_id and
            binding.compute_id == claim.host_id and
            binding.owner == claim.instance_uuid and
            binding.idmap_base == claim.base and
            binding.idmap_size == claim.size)

    def _promote_idmap_claim_if_server_committed(
            self, instance, claim, client=None, attempt=None,
            _claim_lock_held=False):
        """Promote possible only from one exact committed Incus attempt."""
        if self.idmap_allocator is None:
            return None, None
        if not _claim_lock_held:
            client = client or self.client
            if attempt is None:
                # Discovering an Incus/storage attempt may block. Do that
                # before taking the cross-process claim lock, then bind the
                # result to the exact A/H/T/U again inside the lock.
                identity = self._materialization_identity(
                    instance.name, claim)
                attempt = self._storage_ownership_client(
                    client).discover_materialization(identity)
            with lockutils.lock(
                    _idmap_host_claim_lock_name(claim.instance_uuid),
                    external=True, lock_path=_idmap_host_claim_lock_path()):
                return self._promote_idmap_claim_if_server_committed(
                    instance, claim, client=client, attempt=attempt,
                    _claim_lock_held=True)

        assignment, current = self._exact_idmap_host_claim(instance, claim)
        if current is None or current.state != 'possible':
            return assignment, current

        client = client or self.client
        if attempt is None:
            # Some start/delete callers deliberately hold the claim lock
            # across their full Incus transaction. Preserve that contract;
            # ordinary reconciliation discovers before entering the lock.
            identity = self._materialization_identity(
                instance.name, current)
            attempt = self._storage_ownership_client(
                client).discover_materialization(identity)
        if not self._attempt_matches_idmap_claim(attempt, current):
            raise incus_idmap.IDMapIntegrityError(
                'Incus materialization attempt does not match its exact '
                'allocator claim')
        if attempt.state != 'committed' or not attempt.finished:
            return assignment, current

        current = self.idmap_allocator.mark_materialization_committed(
            current.instance_uuid, current.host_id,
            current.materialization_id, assignment=assignment)
        return assignment, current

    def _with_rootfs_materialization_barrier(
            self, materialization, request_config, create_action,
            recover_action=None):
        """Cross possible immediately before POST and observe its result."""
        if materialization is None:
            return create_action()
        binding = materialization.binding
        request_config.update(self._idmap_materialization_config(binding))
        original_error = None
        result = None
        with lockutils.lock(
                _idmap_host_claim_lock_name(binding.owner), external=True,
                lock_path=_idmap_host_claim_lock_path()):
            assignment, claim = self._exact_idmap_host_claim(
                None, materialization.claim)
            if claim is None or claim.state != 'unmaterialized':
                raise incus_idmap.IDMapConflict(
                    reason='Incus materialization claim is not pristine')
            self.idmap_allocator.mark_materialization_possible(
                binding.owner, binding.compute_id, binding.token,
                assignment=assignment)
            try:
                result = create_action()
            except Exception as exc:
                original_error = exc

        protocol = self._storage_ownership_client(materialization.client)
        try:
            attempt = protocol.observe_materialization_start(binding)
        except Exception:
            if original_error is not None:
                raise original_error
            raise
        if attempt.state == 'committed' and attempt.finished:
            unused_assignment, committed_claim = (
                self._promote_idmap_claim_if_server_committed(
                    None, materialization.claim,
                    client=materialization.client, attempt=attempt))
            if (committed_claim is None or
                    committed_claim.state != 'committed'):
                raise incus_idmap.IDMapIntegrityError(
                    'Incus committed the rootfs but its exact allocator '
                    'claim did not commit')
            if original_error is not None:
                if recover_action is None:
                    raise original_error
                return recover_action()
            return result
        if original_error is None:
            original_error = incus_idmap.IDMapIntegrityError(
                'Incus create returned before materialization committed')
        try:
            self._abort_idmap_materialization(materialization)
        except Exception:
            # The caller must see why the build failed, not why the cleanup
            # of that failure failed. Losing the original error hides
            # recoverable causes (a create slower than the client read
            # timeout) behind whatever the abort path happened to hit, and
            # Nova's retry decision is made on the exception it receives.
            LOG.exception(
                'Failed to abort the Incus rootfs materialization after a '
                'failed create; reporting the original failure')
            raise original_error
        raise original_error

    def _idmap_rootfs_release_context(self, instance):
        """Return final-delete intent, allocation and this host's exact T."""
        if self.idmap_allocator is None:
            return None, None, None
        assignment = self.idmap_allocator.get(instance.uuid)
        # Raises on a partial or invalid stamp; absence returns None.
        stored = _instance_idmap_metadata(instance)
        host_id = virt_node.read_local_node_uuid()
        if not host_id:
            raise incus_idmap.IDMapConfigurationError(
                'Nova has no persistent compute-node UUID')
        if assignment is None:
            claim = self.idmap_allocator.get_host_claim(
                instance.uuid, host_id)
            if claim is not None:
                raise incus_idmap.IDMapIntegrityError(
                    'A local Incus host claim exists without its allocation '
                    'generation')
            # No allocation and no local claim: the build never reached the
            # allocator (a delete racing a queued build), or a delete retry
            # arrived after the release already completed. Stale Nova
            # metadata alone is not a resource; deletion must not demand
            # evidence that only a successful build produces.
            return None, None, None
        if stored is not None and not _idmap_generation_matches_metadata(
                assignment, stored):
            raise incus_idmap.IDMapIntegrityError(
                'Incus allocator generation does not match the Nova '
                'instance metadata')
        # stored is None with a live assignment: the build died between the
        # durable allocation and the Nova metadata stamp. The registry pair
        # (assignment plus this host's exact claim) is the authority the
        # release path verifies against; the stamp is Nova's cache of it.
        claim = self.idmap_allocator.get_host_claim(instance.uuid, host_id)
        if claim is None:
            # The build never claimed this host. Local release work needs a
            # claim; disposal of the bare allocation belongs to the terminal
            # failed-build reconciler, which fences it by proving absence.
            return None, None, None
        if not _same_idmap_generation(claim, assignment):
            raise incus_idmap.IDMapIntegrityError(
                'Local Incus root has no exact materialization claim')
        if claim.state == 'possible':
            assignment, claim = (
                self._promote_idmap_claim_if_server_committed(
                    instance, claim))
        intent = self.idmap_allocator.get_release_intent(instance.uuid)
        if intent is not None and (
                intent.instance_uuid != instance.uuid or
                intent.instance_name != instance.name or
                not _same_idmap_generation(intent, assignment)):
            raise incus_idmap.IDMapIntegrityError(
                'Incus rootfs release intent does not match the Nova '
                'instance and allocator generation')
        return intent, assignment, claim

    def _settle_idmap_host_claim(
            self, instance, claim, final_delete=False, client=None):
        """Persist then ACK exact Incus proof; never retire the claim.

        The one exception is a claim still ``unmaterialized`` whose
        materialization attempt was never registered with Incus: it has no
        proof to persist or acknowledge, so it is abandoned whole and
        ``None`` is returned.
        """
        if self.idmap_allocator is None:
            raise incus_idmap.IDMapIntegrityError(
                'Cannot settle an idmap claim without the shared allocator')
        assignment, current = self._exact_idmap_host_claim(instance, claim)
        if current is None:
            return None
        proof = current.proof
        if instance is None and (
                current.state != 'cleaned' or proof is None):
            raise incus_idmap.IDMapIntegrityError(
                'A purged Nova row requires an already-durable exact proof')

        if final_delete and current.state not in ('committed', 'cleaned'):
            raise incus_idmap.IDMapConflict(
                reason='Final rootfs release requires an exact committed '
                       'materialization claim')

        client = client or self.client
        protocol = self._storage_ownership_client(client)
        if current.state == 'cleaned':
            if isinstance(proof, incus_idmap.IDMapMaterializationProof):
                incus_idmap.validate_materialization_proof(proof)
                binding = self._materialization_binding_from_proof(proof)
                acknowledge = protocol.acknowledge_materialization_proof
                acknowledge_payload = proof
            elif isinstance(proof, incus_idmap.IDMapRootfsReleaseProof):
                if incus_idmap.rootfs_release_proof_digest(proof) != (
                        proof.digest):
                    raise incus_idmap.IDMapIntegrityError(
                        'Stored Incus release proof has an invalid digest')
                binding = self._materialization_binding_from_proof(proof)
                acknowledge = protocol.acknowledge_release_receipt
                # The ACK endpoint validates a canonical v2 receipt; the
                # stored proof carries the identical field set.
                acknowledge_payload = incus_idmap.IDMapRootfsReleaseReceipt(
                    **dataclasses.asdict(proof))
            else:
                raise incus_idmap.IDMapIntegrityError(
                    'Cleaned Incus host claim has an invalid proof')
            if (binding.owner != current.instance_uuid or
                    binding.compute_id != current.host_id or
                    binding.token != current.materialization_id or
                    binding.allocation_id != current.allocation_id or
                    binding.idmap_base != current.base or
                    binding.idmap_size != current.size):
                raise incus_idmap.IDMapIntegrityError(
                    'Stored Incus proof does not match its exact host claim')
            try:
                acknowledge(binding, acknowledge_payload)
            except incus_exceptions.LXDAPIException as exc:
                if not _is_incus_not_found(exc):
                    raise
            return current

        identity = self._materialization_identity(instance.name, current)
        if final_delete:
            binding, receipt = protocol.discover_release_receipt(identity)
            current = self.idmap_allocator.record_rootfs_release_proof(
                current.instance_uuid, current.host_id,
                current.materialization_id, receipt,
                assignment=assignment)
            try:
                protocol.acknowledge_release_receipt(binding, receipt)
            except incus_exceptions.LXDAPIException as exc:
                if not _is_incus_not_found(exc):
                    raise
            return current

        try:
            attempt = protocol.discover_materialization(identity)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
            if current.state != 'unmaterialized':
                # The 'possible' transition happens only after the attempt
                # is registered, and a registered attempt is only deleted
                # after its proof made the claim 'cleaned'. A claim beyond
                # 'unmaterialized' without a server attempt is therefore
                # registry corruption, not a recoverable absence.
                raise incus_idmap.IDMapIntegrityError(
                    'Incus has no materialization attempt for a local claim '
                    'that already issued its create request') from exc
            # A claim still 'unmaterialized' proves the create request was
            # never issued; with no server attempt registered there is
            # nothing to abort, settle or prove. Abandon the never-
            # registered claim in one CAS instead of demanding evidence
            # that only a registered materialization produces.
            self.idmap_allocator.abandon_unregistered_claim(
                current.instance_uuid, current.host_id,
                current.materialization_id, assignment=assignment)
            return None
        if attempt.state == 'committed':
            raise incus_idmap.IDMapConflict(
                reason='Committed materialization requires a release receipt')
        if attempt.state == 'active':
            attempt = protocol.abort_materialization(attempt.binding)
        if attempt.proof is None:
            attempt = protocol.settle_materialization(attempt.binding)
        if attempt.proof is None:
            raise incus_idmap.IDMapIntegrityError(
                'Incus materialization has no terminal cleanup proof')
        current = self.idmap_allocator.record_materialization_proof(
            current.instance_uuid, current.host_id,
            current.materialization_id, attempt.proof,
            assignment=assignment)
        try:
            protocol.acknowledge_materialization_proof(
                attempt.binding, attempt.proof)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
        return current

    def _delete_instance_with_rootfs_release_receipt(
            self, container, instance, claim, client=None):
        """Delete one exact T-bound root and persist its v2 receipt."""
        client = client or self.client
        unused_assignment, claim = (
            self._promote_idmap_claim_if_server_committed(
                instance, claim, client=client))
        if claim is None or claim.state != 'committed':
            raise incus_idmap.IDMapIntegrityError(
                'Refusing final Incus rootfs deletion without an exact '
                'committed materialization claim')
        config = container.config if isinstance(container.config, dict) else {}
        expected = {
            'user.openstack.uuid': claim.instance_uuid,
            IDMAP_ALLOCATION_CONFIG_KEY: claim.allocation_id,
            IDMAP_COMPUTE_CONFIG_KEY: claim.host_id,
            IDMAP_MATERIALIZATION_CONFIG_KEY: claim.materialization_id,
        }
        if any(config.get(key) != value for key, value in expected.items()):
            raise incus_idmap.IDMapIntegrityError(
                'Incus instance local A/H/T/U does not match its host claim')

        params = {
            'project': CONF.incus.project,
            'rootfs-idmap-release-token': claim.materialization_id,
            'rootfs-idmap-release-owner': claim.instance_uuid,
            'rootfs-idmap-allocation-id': claim.allocation_id,
            'rootfs-idmap-compute-id': claim.host_id,
        }
        delete_error = None
        try:
            response = client.api.instances[instance.name].delete(
                params=params)
            operation_id = _migration_operation_id(
                response.json().get('operation'))
            if operation_id is None:
                raise incus_idmap.IDMapIntegrityError(
                    'Incus rootfs release delete returned no operation UUID')
            client.operations.wait_for_operation(operation_id)
        except Exception as exc:
            delete_error = exc

        try:
            return self._settle_idmap_host_claim(
                instance, claim, final_delete=True, client=client)
        except Exception as receipt_error:
            if delete_error is not None:
                raise receipt_error from delete_error
            raise

    @staticmethod
    def _instance_has_materialization_binding(container):
        """True when the local record carries a complete A/H/T/U binding."""
        config = container.config if isinstance(container.config, dict) \
            else {}
        return all(config.get(key) for key in (
            'user.openstack.uuid', IDMAP_ALLOCATION_CONFIG_KEY,
            IDMAP_COMPUTE_CONFIG_KEY, IDMAP_MATERIALIZATION_CONFIG_KEY))

    def _delete_fence_retired_instance(self, container, instance,
                                       client=None):
        """Dispose of a local record whose host claim was fence-retired.

        The registry no longer holds this host's claim, so there is
        nothing to settle - but the local Incus record still carries its
        materialization binding and Incus correctly refuses an unproven
        delete. Only recorded fence evidence may authorize the disposal:
        it is the operator's confirmation of how this host's storage
        access ended. The receipt the delete produces is acknowledged
        without a registry write because the fence ledger already is the
        durable audit record of this disposal.
        """
        client = client or self.client
        config = container.config if isinstance(container.config, dict) \
            else {}
        token = config.get(IDMAP_MATERIALIZATION_CONFIG_KEY)
        allocation_id = config.get(IDMAP_ALLOCATION_CONFIG_KEY)
        host_id = config.get(IDMAP_COMPUTE_CONFIG_KEY)
        owner = config.get('user.openstack.uuid')
        if not all((token, allocation_id, host_id, owner)):
            raise incus_idmap.IDMapIntegrityError(
                'Incus instance carries a partial materialization binding')
        if owner != instance.uuid:
            raise incus_idmap.IDMapIntegrityError(
                'Incus instance materialization binding names another '
                'owner')
        local_host = virt_node.read_local_node_uuid()
        if host_id != local_host:
            raise incus_idmap.IDMapIntegrityError(
                'Incus instance materialization binding names another '
                'compute')
        if self.idmap_allocator is None:
            raise incus_idmap.IDMapIntegrityError(
                'Cannot dispose of a bound Incus root without the shared '
                'allocator')
        proof = self.idmap_allocator.get_fence_proof(instance.uuid, host_id)
        if proof is None:
            raise incus_idmap.IDMapIntegrityError(
                'Incus root keeps its materialization binding but the '
                'registry has neither this host claim nor a fence disposal '
                'for it; refusing to guess')
        if proof.allocation_id != allocation_id:
            raise incus_idmap.IDMapIntegrityError(
                'Fence disposal names another allocation generation than '
                'the local Incus root binding')
        try:
            idmap_base = int(config.get('security.idmap.base'))
            idmap_size = int(config.get('security.idmap.size'))
        except (TypeError, ValueError):
            raise incus_idmap.IDMapIntegrityError(
                'Bound Incus root has no explicit idmap base and size')
        LOG.warning(
            'Disposing of the local record of %(name)s under fence '
            'evidence recorded by %(operator)s at %(fenced_at)s',
            {'name': instance.name, 'operator': proof.operator,
             'fenced_at': proof.fenced_at}, instance=instance)
        # The volume's authoritative owner is wherever the instance was
        # evacuated to; this host only disposes of its record. The detached
        # state makes Incus skip rootfs normalization and shared volume
        # deletion - both would need to mount a volume this host no longer
        # owns - and release only local state, recording the outcome in
        # the release receipt.
        extensions = set(client.host_info.get('api_extensions', []))
        if INCUS_STORAGE_HANDOVER_DETACHED_EXTENSION not in extensions:
            raise incus_idmap.IDMapIntegrityError(
                'Fence-based local disposal requires the Incus %s API '
                'extension' % INCUS_STORAGE_HANDOVER_DETACHED_EXTENSION)
        client.api.instances[instance.name]['storage-handover'].put(
            params={'project': CONF.incus.project},
            json={'state': 'detached'})
        current = client.instances.get(instance.name)
        current_config = (
            current.config if isinstance(current.config, dict) else {})
        if str(current_config.get(
                'volatile.migration.storage_delete_protection', '')
               ).lower() not in ('1', 'true', 'yes', 'on'):
            raise incus_idmap.IDMapIntegrityError(
                'Incus did not persist the detached shared-storage '
                'protection')
        params = {
            'project': CONF.incus.project,
            'rootfs-idmap-release-token': token,
            'rootfs-idmap-release-owner': owner,
            'rootfs-idmap-allocation-id': allocation_id,
            'rootfs-idmap-compute-id': host_id,
        }
        response = client.api.instances[instance.name].delete(params=params)
        operation_id = _migration_operation_id(
            response.json().get('operation'))
        if operation_id is None:
            raise incus_idmap.IDMapIntegrityError(
                'Incus rootfs release delete returned no operation UUID')
        client.operations.wait_for_operation(operation_id)
        identity = incus_storage_protocol.StorageMaterializationIdentity(
            token=token, allocation_id=allocation_id, compute_id=host_id,
            owner=owner, project=CONF.incus.project,
            instance_name=instance.name, idmap_base=idmap_base,
            idmap_size=idmap_size)
        protocol = self._storage_ownership_client(client)
        try:
            binding, receipt = protocol.discover_release_receipt(identity)
            protocol.acknowledge_release_receipt(binding, receipt)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise

    def _ensure_instance_idmap(
            self, instance, observed_base=None, observed_size=None):
        """Allocate or verify one deployment-wide fixed idmap."""
        stored = _instance_idmap_metadata(instance)
        if self.idmap_allocator is None:
            if stored:
                # A globally allocated mapping must never be consumed by a
                # compute that cannot verify its generation or establish a
                # durable host claim. This also makes mixed rolling upgrades
                # fail closed instead of silently weakening isolation.
                raise incus_idmap.IDMapIntegrityError(
                    'Nova idmap metadata is present but this compute has no '
                    'global allocator configured')
            return None

        if (observed_base is None) != (observed_size is None):
            raise incus_idmap.IDMapIntegrityError(
                'Incus instance returned an incomplete idmap')

        if stored:
            assignment = self.idmap_allocator.get(instance.uuid)
            if assignment is None:
                # Never mint a replacement generation implicitly. A stale
                # etcd restore or partial operator repair could otherwise
                # make a live instance's slot available for reuse.
                raise incus_idmap.IDMapIntegrityError(
                    'Nova idmap metadata has no allocator record; freeze '
                    'allocations and run the explicit registry recovery '
                    'procedure')
            if (
                stored['base'] != assignment.base or
                stored['size'] != assignment.size or
                stored['allocation_id'] != assignment.allocation_id or
                stored['fingerprint'] != assignment.fingerprint
            ):
                raise incus_idmap.IDMapIntegrityError(
                    'Nova idmap metadata does not match the allocator record')

        if observed_base is not None:
            observed_base = int(observed_base)
            observed_size = int(observed_size)
            if stored and (
                    stored['base'], stored['size']) != (
                        observed_base, observed_size):
                raise incus_idmap.IDMapIntegrityError(
                    'Nova idmap metadata does not match the running Incus '
                    'instance')
            if not stored:
                assignment = self.idmap_allocator.adopt(
                    instance.uuid, observed_base, observed_size)
            elif (
                assignment.base != observed_base or
                assignment.size != observed_size
            ):
                raise incus_idmap.IDMapIntegrityError(
                    'Allocator record does not match the running Incus '
                    'instance')
        elif stored:
            # The verified assignment above is authoritative. Re-allocation
            # here would turn an absent registry record into an unsafe new
            # generation.
            pass
        else:
            assignment = self.idmap_allocator.allocate(instance.uuid)

        expected = {
            IDMAP_BASE_METADATA_KEY: str(assignment.base),
            IDMAP_SIZE_METADATA_KEY: str(assignment.size),
            IDMAP_ALLOCATION_METADATA_KEY: assignment.allocation_id,
            IDMAP_FINGERPRINT_METADATA_KEY: assignment.fingerprint,
        }
        metadata = dict(_loaded_instance_system_metadata(instance))
        if not any(
                metadata.get(key) != value
                for key, value in expected.items()):
            # Already stamped with this generation. Every caller reaches
            # here on the common path, several of them per spawn, so
            # taking a database round trip to confirm what the object
            # already says would be paid on every instance of a run.
            return assignment

        if isinstance(instance, objects.Instance):
            # Instance.save() replaces the complete system_metadata
            # mapping. Refresh under Nova's per-instance operation lock
            # immediately before merging, so a stale object received
            # earlier in a long spawn or migration workflow cannot delete
            # keys someone else added meanwhile. Re-read afterwards: the
            # refresh may itself show the stamp already present.
            instance.refresh()
            metadata = dict(_loaded_instance_system_metadata(instance))
            if not any(
                    metadata.get(key) != value
                    for key, value in expected.items()):
                return assignment

        metadata.update(expected)
        instance.system_metadata = metadata
        # If this save fails the durable allocator record is retained.
        # A Nova retry obtains the same assignment and repairs metadata.
        instance.save()
        return assignment

    def _idmap_claim_from_local_config(self, instance, config):
        """Validate and return the exact claim named by local Incus config."""
        if self.idmap_allocator is None:
            return None, None
        config = config if isinstance(config, dict) else {}
        try:
            owner = _canonical_materialization_id(
                config["user.openstack.uuid"], "instance owner")
            allocation_id = _canonical_materialization_id(
                config[IDMAP_ALLOCATION_CONFIG_KEY], "allocation ID")
            compute_id = _canonical_materialization_id(
                config[IDMAP_COMPUTE_CONFIG_KEY], "compute ID")
            materialization_id = _canonical_materialization_id(
                config[IDMAP_MATERIALIZATION_CONFIG_KEY])
            idmap_base = int(config["security.idmap.base"])
            idmap_size = int(config["security.idmap.size"])
        except (KeyError, TypeError, ValueError) as exc:
            raise incus_idmap.IDMapIntegrityError(
                'Incus instance local config lacks an exact A/H/T/U and '
                'fixed idmap binding') from exc
        if owner != instance.uuid or idmap_base < 0 or idmap_size <= 0:
            raise incus_idmap.IDMapIntegrityError(
                'Incus instance local ownership or fixed idmap is invalid')

        assignment = self._ensure_instance_idmap(
            instance, observed_base=idmap_base, observed_size=idmap_size)
        if allocation_id != assignment.allocation_id:
            raise incus_idmap.IDMapIntegrityError(
                'Incus instance local allocation does not match the '
                'allocator generation')
        claim = self.idmap_allocator.get_host_claim(
            instance.uuid, compute_id)
        if (claim is None or
                claim.materialization_id != materialization_id or
                not _same_idmap_generation(claim, assignment)):
            raise incus_idmap.IDMapIntegrityError(
                'Incus instance local A/H/T/U has no exact allocator claim')
        return assignment, claim

    def _instance_local_idmap_claim(self, instance, container):
        """Return the claim named by an instance's non-expanded config."""
        config = (
            container.config if isinstance(container.config, dict) else {})
        return self._idmap_claim_from_local_config(instance, config)

    def _migration_target_idmap_claim(self, client, instance):
        """Recover a destination claim from its local instance or profile."""
        try:
            container = client.instances.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
            profile = client.profiles.get(instance.name)
            config = (
                profile.config if isinstance(profile.config, dict) else {})
        else:
            config = (
                container.config if isinstance(container.config, dict)
                else {})
        return self._idmap_claim_from_local_config(instance, config)

    def _delete_migration_target_with_idmap(self, client, instance):
        """Delete a protected target through its exact release receipt."""
        unused_assignment, claim = self._migration_target_idmap_claim(
            client, instance)
        if claim is not None and claim.state == 'possible':
            unused_assignment, claim = (
                self._promote_idmap_claim_if_server_committed(
                    instance, claim, client=client))
        try:
            container = client.instances.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
            if claim is not None:
                self._settle_idmap_host_claim(
                    instance, claim,
                    final_delete=claim.state == 'committed',
                    client=client)
            return
        _set_storage_handover_state(
            client, instance.name, 'protected', container=container)
        if container.status != 'Stopped':
            try:
                container.stop(timeout=-1, force=True, wait=True)
            except incus_exceptions.LXDAPIException as exc:
                if 'instance is already stopped' not in str(exc).lower():
                    raise
        if claim is None:
            container.delete(wait=True)
        else:
            self._delete_instance_with_rootfs_release_receipt(
                container, instance, claim, client=client)

    def _retire_instance_idmap_claim_if_clean(self, instance):
        """Retire this compute's claim after local ownership is gone."""
        if self.idmap_allocator is None:
            return False

        for collection in (
                self.client.instances, self.client.profiles):
            try:
                collection.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if not _is_incus_not_found(exc):
                    LOG.critical(
                        'Cannot prove local Incus resources are absent; '
                        'retaining the idmap host claim',
                        instance=instance, exc_info=True)
                    return False
            except Exception:
                LOG.critical(
                    'Cannot prove local Incus resources are absent; '
                    'retaining the idmap host claim',
                    instance=instance, exc_info=True)
                return False
            else:
                return False

        local_paths = (
            common.InstanceAttributes(instance).instance_dir,
            _volume_journal_directory(instance),
            _share_journal_directory(instance),
            _spawn_attempt_journal_path(instance),
        )
        if any(os.path.lexists(path) for path in local_paths):
            return False

        stored = _instance_idmap_metadata(instance)
        if stored is None:
            LOG.critical(
                'Cannot retire the local Incus idmap claim because Nova '
                'system metadata is unavailable', instance=instance)
            return False
        host_id = virt_node.read_local_node_uuid()
        if not host_id:
            LOG.critical(
                'Cannot retire the local Incus idmap claim because Nova has '
                'no persistent compute-node UUID', instance=instance)
            return False

        try:
            release_intent = self.idmap_allocator.get_release_intent(
                instance.uuid)
        except Exception:
            LOG.critical(
                'Cannot establish whether a shared Incus idmap release is '
                'coordinating this cleanup; retaining the host claim',
                instance=instance, exc_info=True)
            return False
        if release_intent is not None:
            if (release_intent.instance_name != instance.name or
                    not _idmap_generation_matches_metadata(
                        release_intent, stored)):
                LOG.critical(
                    'Shared Incus idmap release intent does not match the '
                    'local Nova generation; retaining the host claim',
                    instance=instance)
                return False
            # Final Nova deletion is coordinated by IncusComputeManager. It
            # repeats the exact absence proof after the driver's cleanup and
            # retires this claim together with the immutable release intent.
            # Scanning every project here would duplicate that authoritative
            # proof once per deleted instance.
            return False

        try:
            with lockutils.lock(
                    _idmap_host_claim_lock_name(instance.uuid), external=True,
                    lock_path=_idmap_host_claim_lock_path()):
                assignment = self.idmap_allocator.get(instance.uuid)
                if assignment is None:
                    # Another reconciler may already have retired the final
                    # claim and released this exact generation.
                    return True
                if not _idmap_generation_matches_metadata(assignment, stored):
                    raise incus_idmap.IDMapIntegrityError(
                        'Nova Incus idmap metadata does not match the '
                        'allocator generation during local claim retirement')
                if not _all_project_idmap_resources_absent(
                        self.inventory_client, instance.uuid,
                        assignment.base, assignment.size):
                    return False
                if host_id not in assignment.host_ids:
                    return True
                claim = self.idmap_allocator.get_host_claim(
                    instance.uuid, host_id)
                if claim is None:
                    raise incus_idmap.IDMapIntegrityError(
                        'Allocator assignment lists a host without its exact '
                        'materialization claim')
                if claim.state == 'possible':
                    assignment, claim = (
                        self._promote_idmap_claim_if_server_committed(
                            instance, claim, _claim_lock_held=True))
                claim = self._settle_idmap_host_claim(
                    instance, claim,
                    final_delete=claim.state == 'committed')
                if claim is None or claim.state != 'cleaned':
                    return False
                assignment = self.idmap_allocator.retire_claim(
                    instance.uuid, host_id, claim.materialization_id,
                    assignment=assignment)
                return (
                    assignment is None or host_id not in assignment.host_ids)
        except Exception:
            # Local cleanup is already complete. A retained claim is a safe
            # leak and can be reconciled later; reversing cleanup would be
            # destructive and cannot make the etcd result less ambiguous.
            LOG.critical(
                'Failed to retire the local Incus idmap host claim after '
                'cleanup; retaining it for reconciliation',
                instance=instance, exc_info=True)
            return False

    def _retire_cleanup_ack_idmap_claim(self, instance, profile):
        """Retire this rollback target's claim and return durable proof."""
        if self.idmap_allocator is None:
            return None

        stored = _instance_idmap_metadata(instance)
        if stored is None:
            raise incus_idmap.IDMapIntegrityError(
                'Cannot acknowledge Incus cleanup without Nova idmap '
                'metadata')
        host_id = virt_node.read_local_node_uuid()
        if not host_id:
            raise incus_idmap.IDMapIntegrityError(
                'Cannot acknowledge Incus cleanup without a persistent '
                'compute-node UUID')

        configured = profile.config.get(MIGRATION_IDMAP_RETIREMENT_KEY)
        assignment = self.idmap_allocator.get(instance.uuid)
        if assignment is None:
            raise incus_idmap.IDMapIntegrityError(
                'Cannot acknowledge Incus cleanup after its idmap '
                'generation disappeared')
        if not _idmap_generation_matches_metadata(assignment, stored):
            raise incus_idmap.IDMapIntegrityError(
                'Nova idmap metadata does not match the cleanup target '
                'generation')
        if not _all_project_idmap_resources_absent(
                self.inventory_client, instance.uuid,
                assignment.base, assignment.size,
                allowed_profile_name=instance.name):
            raise incus_idmap.IDMapIntegrityError(
                'Cannot acknowledge Incus cleanup while another project '
                'retains the instance UUID or idmap generation')

        if configured:
            proof = _parse_idmap_retirement_proof(configured)
            if (not _same_idmap_generation(proof, assignment) or
                    proof.host_id != host_id or
                    proof.materialization_id !=
                    profile.config.get(MIGRATION_CLEANUP_TOKEN_KEY)):
                raise incus_idmap.IDMapIntegrityError(
                    'Incus cleanup profile contains another idmap '
                    'retirement proof')
            if host_id in assignment.host_ids:
                raise incus_idmap.IDMapIntegrityError(
                    'Incus cleanup profile claims idmap retirement while '
                    'the local host claim still exists')
            return configured

        generation = assignment
        if host_id in assignment.host_ids:
            claim = self.idmap_allocator.get_host_claim(
                instance.uuid, host_id)
            if claim is None:
                raise incus_idmap.IDMapIntegrityError(
                    'Cleanup target has no exact materialization claim')
            if claim.state == 'possible':
                assignment, claim = (
                    self._promote_idmap_claim_if_server_committed(
                        instance, claim))
            claim = self._settle_idmap_host_claim(
                instance, claim,
                final_delete=claim.state == 'committed')
            if claim is None or claim.state != 'cleaned':
                raise incus_idmap.IDMapIntegrityError(
                    'Cleanup target has no durable materialization proof')
            assignment = self.idmap_allocator.retire_claim(
                instance.uuid, host_id, claim.materialization_id,
                assignment=assignment)
        if (assignment is None or
                not _same_idmap_generation(generation, assignment) or
                host_id in assignment.host_ids):
            raise incus_idmap.IDMapIntegrityError(
                'Cannot prove the cleanup target retired its exact idmap '
                'host claim')
        return _idmap_retirement_proof(
            generation, host_id,
            profile.config.get(MIGRATION_CLEANUP_TOKEN_KEY))

    def _validate_cleanup_ack_idmap_retirement(
            self, profile, instance, idmap_base, idmap_size):
        """Validate target-local retirement before deleting a remote ACK."""
        if self.idmap_allocator is None:
            return

        raw = profile.config.get(MIGRATION_IDMAP_RETIREMENT_KEY)
        if not raw:
            raise exception.MigrationError(
                reason='Incus cleanup acknowledgement has no target-local '
                       'idmap retirement proof')
        proof = _parse_idmap_retirement_proof(raw)
        stored = _instance_idmap_metadata(instance)
        assignment = self.idmap_allocator.get(instance.uuid)
        local_host_id = virt_node.read_local_node_uuid()
        if (stored is None or assignment is None or not local_host_id or
                proof.instance_uuid != instance.uuid or
                (proof.base, proof.size) != (idmap_base, idmap_size) or
                not _idmap_generation_matches_metadata(proof, stored) or
                not _same_idmap_generation(proof, assignment) or
                proof.materialization_id !=
                profile.config.get(MIGRATION_CLEANUP_TOKEN_KEY) or
                proof.host_id == local_host_id or
                tuple(assignment.host_ids) != (local_host_id,)):
            raise exception.MigrationError(
                reason='Incus cleanup acknowledgement idmap retirement '
                       'proof does not match allocator ownership')

    def _validate_remote_cleanup_acknowledgement(
            self, profile, instance, cleanup_token,
            idmap_base, idmap_size):
        """Validate a token-bound remote cleanup and local-claim proof."""
        config = (
            profile.config if isinstance(profile.config, dict) else {})
        if (config.get('environment.product_name') != 'OpenStack Nova' or
                config.get('user.openstack.uuid') != instance.uuid or
                config.get(MIGRATION_CLEANUP_TOKEN_KEY) != cleanup_token or
                config.get(MIGRATION_CLEANUP_COMPLETE_KEY) != cleanup_token or
                profile.used_by or
                _profile_has_volume_connections(profile) or
                _profile_has_share_devices(profile)):
            raise exception.MigrationError(
                reason='Incus migration destination cleanup '
                       'acknowledgement is incomplete or mismatched')
        self._validate_cleanup_ack_idmap_retirement(
            profile, instance, idmap_base, idmap_size)

    def cleanup_host(self, host):
        """Clean up the host.

        `nova.virt.ComputeDriver` defines this method.

        See `nova.virt.driver.ComputeDriver.cleanup_host` for more
        information.
        """
        with self._serial_consoles_lock:
            brokers = list(self._serial_consoles.values())
            self._serial_consoles.clear()
            self._serial_console_destroying.clear()
        for broker in brokers:
            try:
                broker.close()
            except Exception:
                LOG.exception(
                    'Failed to close an Incus serial console broker during '
                    'compute host cleanup')

    def get_info(self, instance, use_cache=True):
        """Return an InstanceInfo object for the instance."""
        if use_cache:
            container = self._get_instance_inventory_snapshot().get(
                instance.name)
            if container is None:
                raise exception.InstanceNotFound(instance_id=instance.uuid)
        else:
            try:
                container = self.client.instances.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if _is_incus_not_found(exc):
                    raise exception.InstanceNotFound(
                        instance_id=instance.uuid) from exc
                raise

        status_code = getattr(container, 'status_code', None)
        if status_code is not None:
            return hardware.InstanceInfo(
                state=_get_power_state(status_code))

        # Older SDK/server combinations may omit status_code from the
        # recursive instance representation.
        if container.status == 'Stopped':
            return hardware.InstanceInfo(state=power_state.SHUTDOWN)

        try:
            state = container.state()
        except incus_exceptions.LXDAPIException as exc:
            if _is_incus_not_found(exc):
                raise exception.InstanceNotFound(
                    instance_id=instance.uuid) from exc
            raise
        return hardware.InstanceInfo(
            state=_get_power_state(state.status_code))

    def _get_generation_cached_snapshot(
            self, cache_attribute, fetch_generation_attribute, ttl,
            fetch):
        """Single-flight one cache without holding a lock across ``fetch``."""
        while True:
            with self._inventory_cache_condition:
                generation = self._inventory_cache_generation
                cached = getattr(self, cache_attribute)
                if (cached is not None and
                        time.monotonic() - cached[0] <= ttl):
                    return cached[1]

                if (getattr(self, fetch_generation_attribute) ==
                        generation):
                    self._inventory_cache_condition.wait()
                    continue

                setattr(
                    self, fetch_generation_attribute, generation)

            try:
                value = fetch()
            except Exception:
                with self._inventory_cache_condition:
                    if (getattr(self, fetch_generation_attribute) ==
                            generation):
                        setattr(
                            self, fetch_generation_attribute, None)
                    current = self._inventory_cache_generation
                    self._inventory_cache_condition.notify_all()
                if current != generation:
                    # The failed read belongs to an invalidated generation.
                    # Retry against the current one instead of surfacing an
                    # obsolete result to its caller.
                    continue
                raise

            with self._inventory_cache_condition:
                if (getattr(self, fetch_generation_attribute) ==
                        generation):
                    setattr(self, fetch_generation_attribute, None)
                current = self._inventory_cache_generation
                if current == generation:
                    setattr(
                        self, cache_attribute,
                        (time.monotonic(), value))
                self._inventory_cache_condition.notify_all()
                if current == generation:
                    return value
            # Invalidation raced the I/O.  Never publish or return that stale
            # result; join or start the single flight for the new generation.

    def _get_instance_inventory_snapshot(self):
        """Return a short-lived expanded inventory for periodic work."""
        def fetch():
            instances = self.client.instances.all(recursion=1)
            return {
                item.name: item
                for item in instances
                if getattr(item, 'type', 'container') == 'container'
            }

        return self._get_generation_cached_snapshot(
            '_instance_inventory_cache',
            '_instance_inventory_fetch_generation',
            _INSTANCE_INVENTORY_CACHE_TTL, fetch)

    def _get_profile_inventory_snapshot(self):
        """Return a short-lived profile inventory for candidate discovery.

        Both profile recovery periodics run back to back in one process and
        used to issue the same recursive listing each. Only *discovery*
        shares this snapshot: every action path still reads its exact
        profile, so a snapshot that has gone stale can at worst offer a
        candidate whose action path then finds nothing to do, or withhold
        one until the next cycle.
        """
        def fetch():
            response = self.client.api.profiles.get(params={'recursion': 1})
            body = response.json()
            profiles = body.get('metadata') if isinstance(body, dict) else None
            if not isinstance(profiles, list):
                raise exception.InvalidConfiguration(
                    'Incus recursive profile inventory is malformed')
            return tuple(
                profile for profile in profiles if isinstance(profile, dict))

        return self._get_generation_cached_snapshot(
            '_profile_inventory_cache',
            '_profile_inventory_fetch_generation',
            max(1, CONF.incus.migration_recovery_interval // 2), fetch)

    def _invalidate_instance_inventory_cache(self):
        """Invalidate every cache derived from mutable instance inventory."""
        with self._inventory_cache_condition:
            self._inventory_cache_generation += 1
            self._instance_inventory_cache = None
            self._profile_inventory_cache = None
            self._metric_devices_cache = None
            self._metric_instance_devices_cache.clear()
            self._disk_metrics_cache = None
            # Old-generation I/O is allowed to finish, but it no longer owns
            # a publication slot.  Invalidation never waits for it.
            self._instance_inventory_fetch_generation = None
            self._profile_inventory_fetch_generation = None
            self._disk_metrics_fetch_generation = None
            self._metric_devices_fetch_generation = None
            self._metric_instance_devices_fetch_generations.clear()
            self._inventory_cache_condition.notify_all()

    def _get_diagnostics_data(self, instance):
        """Return accurately attributable counters from the Incus state API."""
        try:
            container = self.client.instances.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if _is_incus_not_found(exc):
                raise exception.InstanceNotFound(
                    instance_id=instance.uuid) from exc
            raise

        state = container.state()
        cpu = state.cpu or {}
        memory = state.memory or {}
        networks = state.network or {}
        disks = state.disk or {}
        uptime = None
        extensions = self.client.host_info.get('api_extensions', [])
        started_at = getattr(state, 'started_at', None)
        if 'instance_state_started_at' in extensions and started_at:
            try:
                started_at = timeutils.normalize_time(
                    timeutils.parse_isotime(started_at))
                now = timeutils.normalize_time(timeutils.utcnow())
                uptime = max(
                    0, int(timeutils.delta_seconds(started_at, now)))
            except (TypeError, ValueError):
                LOG.warning(
                    'Incus returned an invalid started_at value for '
                    'instance %s: %r', instance.uuid, started_at)

        nics = []
        for name, network in sorted(networks.items()):
            if network.get('type') == 'loopback':
                continue
            counters = network.get('counters') or {}
            nics.append({
                'name': name,
                'mac_address': network.get('hwaddr') or None,
                'rx_octets': counters.get('bytes_received'),
                'rx_errors': counters.get('errors_received'),
                'rx_drop': counters.get('packets_dropped_inbound'),
                'rx_packets': counters.get('packets_received'),
                'tx_octets': counters.get('bytes_sent'),
                'tx_errors': counters.get('errors_sent'),
                'tx_drop': counters.get('packets_dropped_outbound'),
                'tx_packets': counters.get('packets_sent'),
            })

        return {
            'state': _get_power_state(state.status_code),
            'cpu_time': cpu.get('usage'),
            'memory_maximum': memory.get('total'),
            'memory_used': memory.get('usage'),
            'nics': nics,
            'disk_count': len(disks),
            'uptime': uptime,
        }

    def get_diagnostics(self, instance):
        """Return legacy pre-2.48 diagnostics without synthetic disk I/O."""
        data = self._get_diagnostics_data(instance)
        output = {}
        if data['cpu_time'] is not None:
            output['cpu0_time'] = data['cpu_time']
        if data['memory_maximum'] is not None:
            output['memory'] = data['memory_maximum'] // units.Ki
        if data['memory_used'] is not None:
            output['memory-used'] = data['memory_used'] // units.Ki
        for nic in data['nics']:
            prefix = nic['name']
            for source, suffix in (
                    ('rx_octets', 'rx'),
                    ('rx_errors', 'rx_errors'),
                    ('rx_drop', 'rx_drop'),
                    ('rx_packets', 'rx_packets'),
                    ('tx_octets', 'tx'),
                    ('tx_errors', 'tx_errors'),
                    ('tx_drop', 'tx_drop'),
                    ('tx_packets', 'tx_packets')):
                if nic[source] is not None:
                    output['%s_%s' % (prefix, suffix)] = nic[source]
        return output

    def get_instance_diagnostics(self, instance):
        """Return the standardized diagnostics object for Incus containers."""
        data = self._get_diagnostics_data(instance)
        diags = objects.Diagnostics(
            state=power_state.STATE_MAP[data['state']],
            # Nova's versioned Diagnostics object has no external-driver
            # value. Use its standard system-container-compatible driver
            # category and retain the actual backend in ``hypervisor``.
            driver=obj_fields.HypervisorDriver.LIBVIRT,
            config_drive=configdrive.required_by(instance),
            hypervisor='incus',
            hypervisor_os='linux',
            uptime=data['uptime'])
        diags.memory_details = objects.MemoryDiagnostics(
            maximum=(data['memory_maximum'] // units.Mi
                     if data['memory_maximum'] is not None else None),
            used=(data['memory_used'] // units.Mi
                  if data['memory_used'] is not None else None))
        if data['cpu_time'] is not None:
            # Incus reports one cgroup aggregate rather than per-vCPU time.
            diags.add_cpu(id=None, time=data['cpu_time'])
        diags.num_cpus = instance.vcpus
        for _index in range(data['disk_count']):
            diags.add_disk()
        for nic in data['nics']:
            diags.add_nic(
                mac_address=nic['mac_address'],
                rx_octets=nic['rx_octets'],
                rx_errors=nic['rx_errors'],
                rx_drop=nic['rx_drop'],
                rx_packets=nic['rx_packets'],
                tx_octets=nic['tx_octets'],
                tx_errors=nic['tx_errors'],
                tx_drop=nic['tx_drop'],
                tx_packets=nic['tx_packets'])
        return diags

    def block_stats(self, instance, disk_id):
        """Return cumulative cgroup v2 counters for a Cinder block device."""
        try:
            profile = self._get_metric_instance_devices_snapshot(
                instance.name)
            if profile is None:
                return None
            metric_device = _disk_metric_device(
                profile, instance, disk_id)
            if metric_device is None:
                return None
            counters = self._get_disk_metrics_snapshot().get(
                instance.name, {}).get(metric_device)
        except incus_exceptions.LXDAPIException as exc:
            if _is_incus_not_found(exc):
                LOG.info(
                    'Cannot get block stats for missing instance %s',
                    instance.name, instance=instance)
                return None
            raise

        stats = _nova_block_stats(counters)
        if stats is None:
            LOG.warning(
                'Incus metrics are incomplete for instance %(instance)s '
                'device %(device)s',
                {'instance': instance.name, 'device': metric_device},
                instance=instance)
        return stats

    def get_all_volume_usage(self, context, compute_host_bdms):
        """Return Cinder volume counters in Nova's polling format."""
        compute_host_bdms = [
            item for item in compute_host_bdms
            if item.get('instance_bdms')]
        if not compute_host_bdms:
            return []

        usage = []
        metrics_by_instance = self._get_disk_metrics_snapshot()
        profiles = self._get_metric_devices_snapshot()
        rbd_devices = _mapped_cinder_rbd_devices()
        for instance_bdms in compute_host_bdms:
            instance = instance_bdms['instance']
            profile = profiles.get(instance.name)
            disk_metrics = metrics_by_instance.get(instance.name, {})
            for bdm in instance_bdms['instance_bdms']:
                try:
                    volume_id = bdm['volume_id']
                except (KeyError, TypeError):
                    volume_id = (
                        getattr(bdm, 'volume_id', None) or '<unknown>')
                try:
                    if not isinstance(profile, dict):
                        raise exception.InvalidVolume(
                            reason='Incus metric profile is missing or is not '
                                   'a dictionary')
                    devices = profile.get('devices')
                    if not isinstance(devices, dict):
                        raise exception.InvalidVolume(
                            reason='Incus metric profile has no valid devices '
                                   'dictionary')
                    metric_device = _disk_metric_device(
                        profile, instance, bdm['device_name'],
                        rbd_devices=rbd_devices)
                except (
                        exception.InvalidVolume,
                        incus_exceptions.NotFound,
                        AttributeError,
                        KeyError,
                        TypeError,
                        ValueError) as exc:
                    # A malformed or concurrently removed attachment must not
                    # suppress usage for every other volume on this host.
                    LOG.error(
                        'Failed to collect volume usage for instance '
                        '%(instance)s volume %(volume)s: %(error)s',
                        {
                            'instance': instance.name,
                            'volume': volume_id,
                            'error': exc,
                        },
                        instance=instance)
                    continue
                if metric_device is None:
                    continue
                stats = _nova_block_stats(
                    disk_metrics.get(metric_device))
                if stats is None:
                    LOG.warning(
                        'Incus metrics are incomplete for instance '
                        '%(instance)s device %(device)s',
                        {
                            'instance': instance.name,
                            'device': metric_device,
                        },
                        instance=instance)
                    continue
                usage.append({
                    'volume': volume_id,
                    'instance': instance,
                    'rd_req': stats[0],
                    'rd_bytes': stats[1],
                    'wr_req': stats[2],
                    'wr_bytes': stats[3],
                })
        return usage

    def _get_disk_metrics_snapshot(self):
        """Reuse one Incus metrics sample across a burst of volume events."""
        return self._get_generation_cached_snapshot(
            '_disk_metrics_cache', '_disk_metrics_fetch_generation', 1,
            lambda: _incus_all_disk_metrics(self.client))

    def _get_metric_devices_snapshot(self):
        """Bulk-read effective devices for a burst of volume metrics calls."""
        def fetch():
            instances = self._get_instance_inventory_snapshot().values()
            return {
                item.name: {
                    'devices': (
                        getattr(item, 'expanded_devices', None) or
                        getattr(item, 'devices', {}) or {}),
                }
                for item in instances
            }

        return self._get_generation_cached_snapshot(
            '_metric_devices_cache', '_metric_devices_fetch_generation', 1,
            fetch)

    def _get_metric_instance_devices_snapshot(self, instance_name):
        """Read one effective device map without expanding the whole host.

        ``block_stats`` is used for a single detach as well as for a shutdown
        burst. Expanding every instance on a thousand-instance compute for a
        single volume is disproportionate, so cache one exact instance read.
        The host-wide volume-usage poll continues to use the bulk snapshot.
        """
        while True:
            with self._inventory_cache_condition:
                generation = self._inventory_cache_generation
                now = time.monotonic()
                bulk = self._metric_devices_cache
                if bulk is not None and now - bulk[0] <= 1:
                    return bulk[1].get(instance_name)

                cached = self._metric_instance_devices_cache.get(
                    instance_name)
                if cached is not None and now - cached[0] <= 1:
                    return cached[1]

                if (self._metric_instance_devices_fetch_generations.get(
                        instance_name) == generation):
                    self._inventory_cache_condition.wait()
                    continue

                self._metric_instance_devices_fetch_generations[
                    instance_name] = generation

            try:
                item = self.client.instances.get(instance_name)
                devices = {
                    'devices': (
                        getattr(item, 'expanded_devices', None) or
                        getattr(item, 'devices', {}) or {}),
                }
            except Exception:
                with self._inventory_cache_condition:
                    if (self._metric_instance_devices_fetch_generations.get(
                            instance_name) == generation):
                        self._metric_instance_devices_fetch_generations.pop(
                            instance_name, None)
                    current = self._inventory_cache_generation
                    self._inventory_cache_condition.notify_all()
                if current != generation:
                    continue
                raise

            with self._inventory_cache_condition:
                if (self._metric_instance_devices_fetch_generations.get(
                        instance_name) == generation):
                    self._metric_instance_devices_fetch_generations.pop(
                        instance_name, None)
                current = self._inventory_cache_generation
                if current == generation:
                    self._metric_instance_devices_cache[instance_name] = (
                        time.monotonic(), devices)
                self._inventory_cache_condition.notify_all()
                if current == generation:
                    return devices

    def list_instances(self):
        """Return a list of all instance names."""
        # The Nova Incus project is dedicated to this system-container
        # driver. Avoid transferring every instance config and device map for
        # Nova's frequent name-only inventory reconciliation.
        return [
            instance.name for instance in self.client.instances.all()]

    def get_num_instances(self):
        """Count the same expanded snapshot used by power-state syncing.

        Nova calls this immediately before ``get_info`` for every database
        instance. Reusing the expanded inventory avoids a separate name-only
        Incus request without adding any per-instance reads.
        """
        return len(self._get_instance_inventory_snapshot())

    def list_instance_uuids(self):
        """Return UUIDs for Incus instances managed by this Nova driver."""
        owners = {}
        for instance in self._get_instance_inventory_snapshot().values():
            instance_uuid = getattr(instance, 'config', {}).get(
                'user.openstack.uuid')
            if instance_uuid:
                owners.setdefault(instance_uuid, []).append(instance.name)

        for instance_uuid, names in owners.items():
            if len(names) > 1:
                LOG.error(
                    'Multiple Incus containers claim Nova instance UUID '
                    '%(uuid)s: %(names)s. Refusing to report duplicate UUIDs; '
                    'an operator must identify and remove the stale record.',
                    {'uuid': instance_uuid, 'names': sorted(names)})

        # Nova uses this result as a database UUID filter. Report each UUID
        # once while retaining the integrity error above for duplicate owners.
        return list(owners)

    def list_migration_recovery_candidates(self):
        """Return unambiguous migration owners needing runtime repair."""
        candidates_by_uuid = {}
        for container in self._get_instance_inventory_snapshot().values():

            config = getattr(container, 'config', {}) or {}
            expanded_config = (
                getattr(container, 'expanded_config', None) or config)
            instance_uuid = config.get('user.openstack.uuid')
            marker = expanded_config.get(MIGRATION_RECOVERY_KEY)
            if not (
                instance_uuid and
                marker in ('true', 'running', 'stopped')
            ):
                continue

            candidates_by_uuid.setdefault(instance_uuid, []).append({
                'name': container.name,
                'uuid': instance_uuid,
            })

        candidates = []
        for instance_uuid, owners in candidates_by_uuid.items():
            if len(owners) > 1:
                LOG.error(
                    'Multiple Incus targets marked for recovery '
                    'claim Nova instance UUID %(uuid)s: %(names)s. Refusing '
                    'automatic recovery until an operator resolves the '
                    'conflict.',
                    {
                        'uuid': instance_uuid,
                        'names': sorted(owner['name'] for owner in owners),
                    })
                continue
            candidates.append(owners[0])

        return sorted(candidates, key=lambda candidate: candidate['name'])

    def list_unstarted_migration_attempt_reservations(self):
        """Return this host's target reservations that never started.

        A live migration pre-check registers a target name and idmap on the
        destination before anything else exists there. Incus deliberately
        keeps such a reservation across its own restarts, because the create
        request it fences can still arrive; only the orchestrator that made
        the reservation can know that the migration was abandoned instead.

        Nothing here decides that. The caller must prove that no migration
        can still consume each reservation before releasing it.
        """
        extensions = set(self.client.host_info.get('api_extensions', []))
        if INCUS_MIGRATION_ATTEMPT_LIST_EXTENSION not in extensions:
            return []

        response = self.client.api['migration-attempts'].get(
            params={'project': CONF.incus.project, 'recursion': '1'})
        candidates = []
        for attempt in _migration_attempt_list_metadata(response):
            candidate = _unstarted_migration_attempt_reservation(attempt)
            if candidate is not None:
                candidates.append(candidate)

        return sorted(candidates, key=lambda candidate: candidate['token'])

    def release_unstarted_migration_attempt_reservation(self, candidate):
        """Release one reservation the caller proved is abandoned."""
        _release_unstarted_migration_attempt(self.client, candidate)

    def _plug_vifs_for_spawn(
            self, context, instance, network_info, block_device_info):
        timeout = CONF.vif_plugging_timeout
        events = []
        if timeout:
            events = [
                ('network-vif-plugged', vif['id'])
                for vif in network_info if not vif.get('active', True)
            ]

        try:
            with self.virtapi.wait_for_instance_event(
                    instance, events, timeout=timeout,
                    error_callback=_neutron_failed_callback):
                self.plug_vifs(instance, network_info)
        except exception.InstanceEventTimeout:
            if not CONF.vif_plugging_is_fatal:
                return
            self.cleanup(
                context, instance, network_info, block_device_info)
            raise exception.VirtualInterfaceCreateException()

    def rebuild(self, context, instance, image_meta, injected_files,
                admin_password, allocations, bdms, detach_block_devices,
                attach_block_devices, network_info=None,
                evacuate=False, block_device_info=None,
                preserve_ephemeral=False, accel_uuids=None,
                reimage_boot_volume=False):
        """Gate evacuation, then delegate orchestration to Nova's fallback."""
        if not evacuate:
            raise NotImplementedError()
        if not CONF.incus.allow_bfv_evacuate:
            raise exception.InstanceEvacuateNotSupported()

        root_bdm = _boot_from_volume(block_device_info)
        if root_bdm is None:
            raise exception.InstanceEvacuateNotSupported()

        _require_bfv_migration_support(self.client, root_bdm)

        # ComputeManager catches this and executes _rebuild_default_impl,
        # which owns the Placement, Cinder attachment, Neutron and spawn
        # transaction. The driver must not duplicate that framework logic.
        raise NotImplementedError()

    @_invalidates_instance_inventory
    def _spawn_root_device_preflight(self, context, instance, image_meta,
                                     network_info, block_device_info):
        """Resolve and validate the spawn root device before any side effect.

        Returns ``(root_volume, root_device, data_volume_bdms)`` where
        ``root_volume`` is ``None`` for an Incus-managed (non-BFV) root.
        """
        if driver.block_device_info_get_ephemerals(block_device_info):
            raise exception.InvalidConfiguration(
                'Nova ephemeral disks are disabled until they can be '
                'backed by quota-controlled Incus storage volumes')

        root_bdm = _boot_from_volume(block_device_info)
        data_volume_bdms = _spawn_data_volume_bdms(
            block_device_info,
            root_device_name=getattr(instance, 'root_device_name', None))
        self._validate_initial_volume_encryption(
            context, root_bdm, data_volume_bdms)
        self._preflight_data_volume_bdms(data_volume_bdms)
        _require_initial_data_volume_image_capability(
            instance, image_meta, data_volume_bdms)
        root_volume = None
        if root_bdm:
            root_volume = _cinder_rbd_root(root_bdm)
            bfv_pool_name = _bfv_storage_pool_name(root_volume[0])
            extensions = self.client.host_info.get('api_extensions', [])
            if 'storage_driver_cephext' not in extensions:
                raise exception.InvalidConfiguration(
                    'The Incus server does not support '
                    'storage_driver_cephext')
            bfv_pool = self.client.storage_pools.get(bfv_pool_name)
            if bfv_pool.driver != 'cephext':
                raise exception.InvalidConfiguration(
                    'Incus boot-from-volume storage pool must use '
                    'cephext')
            if bfv_pool.config.get('source') != root_volume[0]:
                raise exception.InvalidConfiguration(
                    'The Incus cephext pool source does not match the '
                    'Cinder RBD pool')
            root_device = _bfv_root_device(
                instance, root_bdm, root_volume)
        else:
            root_device = flavor._root(
                instance, self.client, network_info,
                block_device_info)['root']
        return root_volume, root_device, data_volume_bdms

    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, allocations, network_info=None,
              block_device_info=None, power_on=True, accel_info=None):
        """Create a new incus container as a nova instance.

        Creating a new container requires a number of steps. First, the
        image is fetched from glance, if needed. Next, the network is
        connected. A profile is created in Incus, and then the container
        is created and started.

        See `nova.virt.driver.ComputeDriver.spawn` for more
        information.
        """
        spawn_watch = timeutils.StopWatch()
        spawn_watch.start()
        root_volume = None
        root_device = None
        materialization = None
        materialization_id = uuidutils.generate_uuid()
        with self._timed_phase(instance, 'spawn', 'preflight'):
            spawn_attempt = self._create_spawn_preflight_attempt(
                instance, materialization_id)
            try:
                root_volume, root_device, data_volume_bdms = (
                    self._spawn_root_device_preflight(
                        context, instance, image_meta, network_info,
                        block_device_info))
            except (exception.Invalid,
                    exception.MultiattachNotSupportedByVirtDriver) as exc:
                self._spawn_build_abort(instance, exc)

            try:
                self.client.instances.get(instance.name)
                raise exception.InstanceExists(name=instance.name)
            except incus_exceptions.LXDAPIException as e:
                if not _is_incus_not_found(e):
                    raise  # Re-raise the exception if it wasn't NotFound

        with self._timed_phase(instance, 'spawn', 'idmap_materialization'):
            spawn_attempt = self._open_spawn_attempt(instance, spawn_attempt)
            try:
                materialization = self._begin_idmap_materialization(
                    instance, materialization_id, root_device)
            except incus_idmap.IDMapError as exc:
                raise exception.InvalidConfiguration(
                    'Cannot register a deployment-wide Incus root '
                    'materialization: {}'.format(
                        exc)) from exc
            self._finish_spawn_attempt_open(instance, spawn_attempt)

        instance_dir = common.InstanceAttributes(instance).instance_dir
        if not os.path.exists(instance_dir):
            fileutils.ensure_tree(instance_dir)

        if not root_volume:
            # A Cinder root volume already contains the prepared rootfs.
            with self._timed_phase(instance, 'spawn', 'image_sync'):
                try:
                    self.client.images.get_by_alias(instance.image_ref)
                except incus_exceptions.LXDAPIException as e:
                    if not _is_incus_not_found(e):
                        raise
                    try:
                        _sync_glance_image_to_incus(
                            self.client, context, instance.image_ref)
                    except Exception:
                        with excutils.save_and_reraise_exception():
                            self._abort_idmap_materialization(
                                materialization)
                            self.cleanup(
                                context, instance, network_info,
                                block_device_info)

        # Plug in the network
        if network_info:
            with self._timed_phase(instance, 'spawn', 'vif_plug'):
                try:
                    self._plug_vifs_for_spawn(
                        context, instance, network_info, block_device_info)
                except Exception:
                    with excutils.save_and_reraise_exception():
                        self._abort_idmap_materialization(materialization)
                        self.cleanup(
                            context, instance, network_info,
                            block_device_info)

        # Create the profile
        with self._timed_phase(instance, 'spawn', 'profile'):
            try:
                profile = flavor.to_profile(
                    self.client, instance, network_info, block_device_info)
                if root_volume:
                    profile.devices['root'] = root_device
                    profile.save()
            except incus_exceptions.LXDAPIException:
                with excutils.save_and_reraise_exception():
                    self._abort_idmap_materialization(materialization)
                    self.cleanup(
                        context, instance, network_info, block_device_info)

        # Create the container
        container_config = {
            'name': instance.name,
            'type': 'container',
            'profiles': [profile.name],
            'config': {
                **_incus_cloud_init_config(instance, network_info),
                # The storage release receipt endpoint binds the external
                # owner to instance-local configuration, not a profile that
                # can be shared or removed independently.
                'user.openstack.uuid': instance.uuid,
                # Nova must reconcile ownership before a workload resumes
                # after a fenced compute returns.
                'boot.autostart': 'false',
                # Instance-local configuration wins over every attached
                # profile in Incus's ExpandedConfig(). Keep CRIU on one full
                # final checkpoint even if an extra profile requests pre-copy.
                'migration.incremental.memory': 'false',
            },
            'source': ({'type': 'none'} if root_volume else {
                'type': 'image', 'alias': instance.image_ref}),
        }

        def create_container():
            try:
                return self.client.instances.create(
                    container_config, wait=True)
            except incus_exceptions.LXDAPIException as exc:
                if root_volume or not _is_incus_not_found(exc):
                    raise
                # The image was present when this spawn checked for it,
                # but cache aging holds no lock over the interval before
                # the create and only sees the instances that existed when
                # its pass began - so a build that started after that
                # snapshot can have its image removed underneath it. Sync
                # it back and create once more rather than failing a spawn
                # over a cache decision. The retry stays inside this
                # callable so the materialization barrier sees one attempt.
                LOG.info(
                    'Cached Incus image %s disappeared before the container '
                    'was created; syncing it again', instance.image_ref,
                    instance=instance)
                _sync_glance_image_to_incus(
                    self.client, context, instance.image_ref)
                return self.client.instances.create(
                    container_config, wait=True)

        with self._timed_phase(instance, 'spawn', 'incus_create'):
            try:
                container = self._with_rootfs_materialization_barrier(
                    materialization, container_config['config'],
                    create_container,
                    recover_action=lambda: self.client.instances.get(
                        instance.name))
            except Exception:
                with excutils.save_and_reraise_exception():
                    self.cleanup(
                        context, instance, network_info, block_device_info)

        attached_data_volumes = []
        try:
            with self._timed_phase(instance, 'spawn', 'volumes'):
                # Nova's initial BDM preparation creates and completes the
                # Cinder attachments with do_driver_attach=False. The virt
                # driver's spawn transaction must therefore connect every
                # non-root volume before the guest is first started.
                # (Ephemeral disks are rejected in the preflight; the dead
                # LVM/ZFS ephemeral module has been removed.)
                if data_volume_bdms:
                    with lockutils.lock(_profile_lock_name(instance)):
                        profile = self.client.profiles.get(instance.name)
                        _validate_profile_volume_owner(profile, instance)
                        profile.config[SPAWN_VOLUME_GENERATION_KEY] = (
                            materialization_id)
                        profile.save(wait=True)
                for bdm in data_volume_bdms:
                    self._attach_and_commit_internal_volume_operation(
                        context, bdm['connection_info'], instance,
                        bdm['mount_device'], _bdm_attachment_id(bdm),
                        'spawn', materialization_id, 'materialize',
                        encryption=bdm.get('encrypted'))
                    attached_data_volumes.append(bdm)
            if configdrive.required_by(instance):
                with self._timed_phase(instance, 'spawn', 'configdrive'):
                    configdrive_path = self._add_configdrive(
                        context, instance,
                        injected_files, admin_password,
                        network_info)

                    profile = self.client.profiles.get(instance.name)
                    config_drive = {
                        'configdrive': {
                            'path': '/config-drive',
                            'source': configdrive_path,
                            'type': 'disk',
                            'readonly': 'true',
                        }
                    }
                    profile.devices.update(config_drive)
                    profile.save()

            self.firewall_driver.setup_basic_filtering(
                instance, network_info)
            self.firewall_driver.instance_filter(
                instance, network_info)

            if power_on:
                with self._timed_phase(instance, 'spawn', 'start'):
                    self._start_instance_with_idmap(instance, container)

            self.firewall_driver.apply_instance_filter(
                instance, network_info)
            if data_volume_bdms:
                self.finalize_spawn_volume_generation(
                    instance, materialization_id)
        except Exception:
            with excutils.save_and_reraise_exception():
                self._fence_failed_spawn(
                    context, instance, network_info, block_device_info,
                    materialization, materialization_id, data_volume_bdms,
                    attached_data_volumes)
        LOG.info(
            'timing operation=spawn phase=total outcome=ok '
            'duration_ms=%(ms)d',
            {'ms': int(spawn_watch.elapsed() * 1000)}, instance=instance)

    def _rollback_failed_spawn_volume_intents(
            self, context, instance, materialization_id, data_volume_bdms,
            attached_data_volumes):
        """Roll back every exact spawn-generation mapping before deletion."""
        bdms = {}
        for bdm in data_volume_bdms:
            volume_id = _bdm_volume_id(bdm)
            if volume_id in bdms:
                raise exception.InvalidVolume(
                    reason='Failed spawn has duplicate Cinder volume mappings')
            bdms[volume_id] = bdm

        intents = _managed_attach_intents_by_uuid(instance.uuid)
        for volume_id, intent in intents.items():
            if (intent.get('operation_kind') != 'spawn' or
                    intent.get('operation_token') != materialization_id):
                raise exception.InvalidVolume(
                    reason='Failed spawn has a foreign unfinished Cinder '
                           'transaction')
            if volume_id not in bdms:
                raise exception.InvalidVolume(
                    reason='Failed spawn Cinder intent has no exact Nova BDM')

        attempted_ids = {_bdm_volume_id(bdm) for bdm in attached_data_volumes}
        volume_ids = set(intents) | attempted_ids
        for volume_id in sorted(volume_ids):
            bdm = bdms[volume_id]
            connection_info = bdm.get('connection_info')
            mountpoint = bdm.get('mount_device')
            attachment_id = _bdm_attachment_id(bdm)
            if (not connection_info or _volume_id(connection_info) != volume_id
                    or not mountpoint):
                raise exception.InvalidVolume(
                    reason='Failed spawn Cinder BDM identity is incomplete')
            with lockutils.lock(
                    _volume_manager_transaction_lock_name(
                        instance.uuid, volume_id),
                    external=True, lock_path=_volume_operation_lock_path()):
                intent = self.prepare_managed_volume_attach(
                    instance, volume_id, attachment_id, mountpoint,
                    operation_kind='spawn',
                    operation_token=materialization_id,
                    operation_direction='materialize')
                with lockutils.lock(
                        _volume_topology_lock_name(instance), external=True,
                        lock_path=_volume_topology_lock_path()):
                    with lockutils.lock(
                            _volume_operation_lock_name(volume_id),
                            external=True,
                            lock_path=_volume_operation_lock_path()):
                        current = self.get_managed_volume_attach_intent(
                            instance, volume_id)
                        if current != intent:
                            raise exception.InvalidVolume(
                                reason='Failed spawn Cinder intent changed '
                                       'during rollback')
                        self.validate_internal_volume_attach_owner(
                            instance, intent)
                        self.rollback_internal_volume_attach(
                            context, instance, volume_id, connection_info,
                            expected_mountpoint=mountpoint)
                        record = _read_volume_journal(instance, volume_id)
                        if record is not None:
                            if record.get('phase') != 'rolled-back':
                                raise exception.InvalidVolume(
                                    reason='Failed spawn Cinder rollback did '
                                           'not reach a terminal phase')
                            self.finalize_rolled_back_volume_journal(
                                instance, volume_id)
                        self.cancel_managed_volume_attach(
                            instance, volume_id, intent)
        self.finalize_spawn_volume_generation(instance, materialization_id)

    def _fence_failed_spawn(self, context, instance, network_info,
                            block_device_info, materialization,
                            materialization_id, data_volume_bdms,
                            attached_data_volumes):
        """Fence and roll back a spawn that failed after container creation.

        Called from inside ``save_and_reraise_exception``; the original
        spawn failure is always re-raised after this returns.
        """
        def stop_spawn_container():
            try:
                current = self.client.instances.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if _is_incus_not_found(exc):
                    return
                raise
            if current.status != 'Stopped':
                current.stop(timeout=-1, force=True, wait=True)

        def delete_spawn_container():
            try:
                current = self.client.instances.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if _is_incus_not_found(exc):
                    return
                raise
            if materialization is None:
                current.delete(wait=True)
            else:
                self._delete_instance_with_rootfs_release_receipt(
                    current, instance, materialization.claim)

        try:
            _retry_incus_instance_action(
                stop_spawn_container,
                'failed spawn container stop',
                instance, retry_transient=True)
        except Exception:
            # A still-running or ambiguously deleted workload must
            # retain every storage and network attachment. Nova can
            # retry destroy after Incus reaches a known state.
            LOG.critical(
                'Failed to fence the Incus container after spawn '
                'failed; retaining all attached resources',
                instance=instance, exc_info=True)
            return

        try:
            self._rollback_failed_spawn_volume_intents(
                context, instance, materialization_id, data_volume_bdms,
                attached_data_volumes)
        except Exception:
            LOG.critical(
                'Failed to roll back the exact spawn-generation Cinder '
                'transactions; retaining the stopped Incus container',
                instance=instance, exc_info=True)
            return

        try:
            _retry_incus_instance_action(
                delete_spawn_container,
                'failed spawn container deletion',
                instance, retry_transient=True)
        except Exception:
            LOG.critical(
                'Failed to delete the stopped Incus container after its '
                'spawn-generation Cinder mappings were rolled back',
                instance=instance, exc_info=True)
            return
        try:
            self.cleanup(
                context, instance, network_info, block_device_info)
        except Exception as e:
            LOG.warning('The cleanup process failed with: %s. This '
                        'error may or not may be relevant', e)

    def _failed_build_idmap_release_allowed(self, instance, reasons):
        """Return whether idmap ownership permits releasing the failed host.

        Only observes and settles already-proven claims; any uncertainty
        appends a reason and blocks the release.
        """
        allowed = True
        try:
            idmap_metadata = _instance_idmap_metadata(instance)
            allocator = getattr(self, 'idmap_allocator', None)
            if allocator is None:
                if idmap_metadata is not None:
                    raise incus_idmap.IDMapIntegrityError(
                        'Nova idmap metadata has no configured allocator')
            else:
                assignment = allocator.get(instance.uuid)
                if assignment is None:
                    if idmap_metadata is not None:
                        raise incus_idmap.IDMapIntegrityError(
                            'Nova idmap metadata has no allocator record')
                else:
                    host_id = virt_node.read_local_node_uuid()
                    if not host_id:
                        raise incus_idmap.IDMapIntegrityError(
                            'Nova compute node UUID is unavailable')
                    claim = allocator.get_host_claim(instance.uuid, host_id)
                    indexed = host_id in assignment.host_ids
                    if indexed != (claim is not None):
                        raise incus_idmap.IDMapIntegrityError(
                            'Incus idmap host index and claim disagree')
                    if claim is not None:
                        unused_assignment, exact_claim = (
                            self._exact_idmap_host_claim(instance, claim))
                        if (exact_claim.state != 'cleaned' or
                                exact_claim.proof is None):
                            allowed = False
                            reasons.add(
                                'local idmap materialization still exists')
                        else:
                            # Replaying the exact ACK proves this is a durable
                            # cleaned claim. Keep it in the registry while Nova
                            # decides whether to reschedule or terminally fail;
                            # it no longer owns local host resources.
                            self._settle_idmap_host_claim(
                                instance, exact_claim, final_delete=False)
            if (idmap_metadata is not None and
                    not _all_project_idmap_resources_absent(
                        self.inventory_client, instance.uuid,
                        idmap_metadata['base'],
                        idmap_metadata['size'])):
                allowed = False
                reasons.add('Incus idmap resource still exists')
        except Exception as exc:
            allowed = False
            reasons.add('idmap ownership is uncertain: {}'.format(exc))
        return allowed

    def assess_failed_build_cleanup(self, instance, block_device_info):
        """Decide which Nova resources can be released after failed spawn.

        This method only observes local state. Any uncertainty retains the
        affected external ownership instead of allowing Nova to free a port,
        Cinder attachment, host assignment, or Placement allocation while a
        local Incus resource may still consume it.
        """
        if not isinstance(block_device_info, dict):
            return FailedBuildCleanupAssessment.unsafe(
                'block-device inventory is unavailable')

        # This assessment is reached only after driver.destroy() failed. A
        # missing Incus instance/profile does not prove that spawn-time host
        # VIF wiring was removed: profile creation can fail after plug(), and
        # cleanup can then fail in vif_driver.unplug(). Nova's generic failed
        # build cleanup would otherwise release Neutron despite that error.
        # Retain networking until a later destroy retry completes end to end.
        reasons = {'host VIF absence is not proven after failed destroy'}
        release_network = False
        release_cinder = True
        release_host = True

        try:
            container = self.client.instances.get(instance.name)
        except Exception as exc:
            if _is_incus_not_found(exc):
                container = None
            else:
                return FailedBuildCleanupAssessment.unsafe(
                    'Incus instance inventory is uncertain: {}'.format(exc))

        try:
            profile = self.client.profiles.get(instance.name)
        except Exception as exc:
            if _is_incus_not_found(exc):
                profile = None
            else:
                return FailedBuildCleanupAssessment.unsafe(
                    'Incus profile inventory is uncertain: {}'.format(exc))

        if container is not None:
            if _instance_nova_uuid(container) != instance.uuid:
                return FailedBuildCleanupAssessment.unsafe(
                    'named Incus instance ownership is ambiguous')
            release_network = False
            release_host = False
            reasons.add('Incus instance still exists')

        try:
            volume_journals = _volume_journal_records(instance)
        except Exception as exc:
            release_cinder = False
            release_host = False
            reasons.add(
                'Cinder journal ownership is uncertain: {}'.format(exc))
            volume_journals = None

        if profile is not None:
            try:
                _validate_profile_volume_owner(profile, instance)
            except Exception as exc:
                return FailedBuildCleanupAssessment.unsafe(
                    'named Incus profile ownership is ambiguous: {}'.format(
                        exc))
            release_host = False
            reasons.add('Incus profile still exists')
            devices = (
                profile.devices if isinstance(profile.devices, dict) else {})
            if any(
                    isinstance(device, dict) and
                    device.get('type') == 'nic'
                    for device in devices.values()):
                release_network = False
                reasons.add('Incus profile still owns a network device')
            try:
                if _profile_has_volume_connections(profile):
                    release_cinder = False
                    reasons.add('Incus profile still owns a Cinder device')
                topology = _data_volume_topology(
                    profile, volume_journals or {})
                if topology['opaque_ids']:
                    release_cinder = False
                    reasons.add(
                        'Incus profile has an opaque unix-block device')
            except Exception as exc:
                release_cinder = False
                reasons.add(
                    'Cinder profile ownership is uncertain: {}'.format(exc))

        if volume_journals:
            release_cinder = False
            release_host = False
            reasons.add('host Cinder cleanup journal still exists')

        try:
            volume_bdms = list(
                driver.block_device_info_get_mapping(block_device_info))
        except Exception as exc:
            release_cinder = False
            release_host = False
            reasons.add(
                'Cinder block-device inventory is invalid: {}'.format(exc))
            volume_bdms = []

        root_bdm = None
        try:
            root_bdm = _boot_from_volume(block_device_info)
        except Exception as exc:
            release_cinder = False
            release_host = False
            reasons.add('BFV root identity is uncertain: {}'.format(exc))
        mapping_cinder, mapping_host, mapping_reasons = (
            _failed_build_rbd_mapping_ownership(
                volume_bdms, container, profile, root_bdm))
        release_cinder = release_cinder and mapping_cinder
        release_host = release_host and mapping_host
        reasons.update(mapping_reasons)

        try:
            share_journals = _share_journal_records(instance)
        except Exception as exc:
            release_host = False
            reasons.add(
                'Manila journal ownership is uncertain: {}'.format(exc))
            share_journals = None
        if share_journals:
            release_host = False
            reasons.add('host Manila cleanup journal still exists')

        local_paths = (
            common.InstanceAttributes(instance).instance_dir,
            _volume_journal_directory(instance),
            _share_journal_directory(instance),
            _spawn_attempt_journal_path(instance),
        )
        try:
            retained_paths = [
                path for path in local_paths if os.path.lexists(path)]
        except Exception as exc:
            release_host = False
            reasons.add(
                'host resource paths are uncertain: {}'.format(exc))
        else:
            if retained_paths:
                release_host = False
                reasons.add('host resource path still exists')

        if not self._failed_build_idmap_release_allowed(instance, reasons):
            release_host = False

        release_host = release_host and release_network and release_cinder
        release_placement = release_host
        return FailedBuildCleanupAssessment(
            release_network=release_network,
            release_cinder=release_cinder,
            release_host=release_host,
            release_placement=release_placement,
            reasons=tuple(sorted(reasons)))

    def _assert_destroy_volume_transactions_settled(self, instance):
        if not _prune_orphan_volume_recovery_directory(instance):
            raise exception.InvalidVolume(
                reason='Refusing to destroy an Incus instance with '
                       'unfinished Cinder recovery evidence')
        try:
            profile = self.client.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if _is_incus_not_found(exc):
                return
            raise
        config = profile.config if isinstance(profile.config, dict) else {}
        cleanup_token = config.get(MIGRATION_CLEANUP_TOKEN_KEY)
        rollback_token = config.get(MIGRATION_ROLLBACK_COMPLETE_KEY)
        migration_uuid = config.get(MIGRATION_NOVA_UUID_KEY)
        if rollback_token is not None:
            if (not uuidutils.is_uuid_like(cleanup_token) or
                    rollback_token != cleanup_token or
                    not uuidutils.is_uuid_like(migration_uuid)):
                raise exception.InvalidVolume(
                    reason='Incus source rollback volume generation is '
                           'malformed')
            raise exception.InvalidVolume(
                reason='Refusing to destroy an Incus source while its '
                       'rollback volume generation is not retired')

    @_invalidates_instance_inventory
    @_guards_serial_console
    def destroy(self, context, instance, network_info, block_device_info=None,
                destroy_disks=True, destroy_secrets=True):
        """Destroy a running instance.

        Since the profile and the instance are created on `spawn`, it is
        safe to delete them together.

        See `nova.virt.driver.ComputeDriver.destroy` for more
        information.
        """
        if self._consume_spawn_preflight_noop(instance):
            LOG.debug(
                'Consumed exact preflight-only spawn attempt for %s; no '
                'Incus or host resource cleanup is required',
                instance.name)
            return

        # Removing the container first would destroy the only profile proof
        # needed to reconcile a committed Cinder attach/detach generation.
        # Keep the complete guest intact until periodic recovery (or a
        # migration-specific manager hook) retires every local transaction.
        with lockutils.lock(
                _volume_topology_lock_name(instance), external=True,
                lock_path=_volume_topology_lock_path()):
            self._assert_destroy_volume_transactions_settled(instance)

        failed_build_cleanup = (
            getattr(instance, 'vm_state', None) == vm_states.BUILDING and
            getattr(instance, 'task_state', None) == task_states.SPAWNING)
        unused_release_intent, unused_release_assignment, release_claim = (
            self._idmap_rootfs_release_context(instance))

        def destroy_container(name):
            if name != instance.name:
                return _retry_incus_instance_action(
                    lambda: stop_and_delete(name),
                    'stop and delete {}'.format(name),
                    instance, retry_transient=True)
            # Hold the topology lock through stop/delete, not just the first
            # preflight.  This closes the interval in which a periodic replay
            # could otherwise publish new durable volume evidence after the
            # check but before the container is irreversibly removed.
            with lockutils.lock(
                    _volume_topology_lock_name(instance), external=True,
                    lock_path=_volume_topology_lock_path()):
                self._assert_destroy_volume_transactions_settled(instance)
                return _retry_incus_instance_action(
                    lambda: stop_and_delete(name),
                    'stop and delete {}'.format(name),
                    instance, retry_transient=True)

        def stop_and_delete(name):
            try:
                container = self.client.instances.get(name)
            except incus_exceptions.LXDAPIException as exc:
                if _is_incus_not_found(exc):
                    if name == instance.name and release_claim is not None:
                        self._settle_idmap_host_claim(
                            instance, release_claim,
                            final_delete=(
                                release_claim.state == 'committed'))
                    LOG.debug(
                        "Incus container is already absent for "
                        "%(instance)s; continuing idempotent cleanup.",
                        {'instance': name})
                    return
                raise

            if (name == instance.name and release_claim is not None and
                    release_claim.state != 'committed'):
                raise incus_idmap.IDMapIntegrityError(
                    'An Incus instance exists without an exact committed '
                    'materialization claim')

            if (name == instance.name and
                    _instance_has_negotiated_handover(container)):
                root_pool = _instance_root_pool(
                    self.client, name, container=container)
                if root_pool.driver == 'ceph':
                    if cleanup_token:
                        # A token-bound resize/live-migration loser must
                        # delete only its local record even when Nova asks to
                        # destroy local copied disks. The cleanup ACK proves
                        # target teardown independently of destroy_disks.
                        _set_storage_handover_state(
                            self.client, name, 'protected',
                            container=container)
                    elif destroy_disks:
                        # destroy_disks is a Nova cleanup policy, not proof
                        # that this record owns an Incus-managed shared root.
                        # Only migration commit/revert may grant ownership.
                        raise exception.MigrationError(
                            reason='Incus instance %s is protected by an '
                                   'incomplete shared-storage handover' %
                                   name)
                    else:
                        # Reassert the existing negotiated protection, then
                        # delete only the record.
                        _set_storage_handover_state(
                            self.client, name, 'protected',
                            container=container)
            if container.status != 'Stopped':
                container.stop(wait=True)

            if name == instance.name and release_claim is not None:
                self._delete_instance_with_rootfs_release_receipt(
                    container, instance, release_claim)
            elif (name == instance.name and
                    self._instance_has_materialization_binding(container)):
                # No registry claim, yet the local record is still bound:
                # the claim was disposed of externally. Fence evidence is
                # the only authority this path accepts (an evacuated-stale
                # record on a returning host is the normal case); anything
                # else stays fail-closed rather than guessing at incusd's
                # receipt requirement.
                self._delete_fence_retired_instance(container, instance)
            else:
                container.delete(wait=True)

        cleanup_token = None
        try:
            profile = self.client.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
        else:
            if isinstance(profile.config, dict):
                cleanup_token = profile.config.get(
                    MIGRATION_CLEANUP_TOKEN_KEY)

        # The name is what excludes; see the note in _sync_glance_image.
        # This lock is held across stop, delete, receipt settlement, cleanup
        # and idmap claim retirement, so keying it on a constant serialized
        # roughly three quarters of every destroy on the host against every
        # other one.
        with lockutils.lock(
                'incus-container-{}'.format(instance.name), external=True):
            if (cleanup_token and destroy_disks and
                    getattr(instance, 'host', None) == self.host):
                # A successful migration updates Nova's authoritative host
                # before every target-side staging record is necessarily
                # retired. Do not mistake that current owner for a rollback
                # loser merely because its profile still has the cleanup
                # token. Converge the committed attempt, then require both
                # Incus ownership and token retirement before final delete.
                _converge_migration_target_ownership(
                    self.client, instance, local_volume_evidence=True)
                current = self.client.instances.get(instance.name)
                if not _storage_handover_is_owned(
                        self.client, instance.name, container=current):
                    raise exception.MigrationError(
                        reason='Refusing final delete before the current '
                               'Incus migration target owns shared storage')
                current_profile = self.client.profiles.get(instance.name)
                current_config = (
                    current_profile.config
                    if isinstance(current_profile.config, dict) else {})
                if current_config.get(MIGRATION_CLEANUP_TOKEN_KEY):
                    raise exception.MigrationError(
                        reason='Refusing final delete before committed Incus '
                               'migration staging metadata is retired')
                cleanup_token = None

            # TODO(sahid): Each time we get a container we should
            # protect it by using a mutex.
            with self._timed_phase(instance, 'destroy', 'container'):
                destroy_container(instance.name)
                if instance.vm_state == vm_states.RESCUED:
                    destroy_container('{}-rescue'.format(instance.name))

            with self._timed_phase(instance, 'destroy', 'cleanup'):
                if cleanup_token:
                    self._cleanup(
                        context, instance, network_info,
                        block_device_info=block_device_info,
                        destroy_disks=destroy_disks,
                        destroy_secrets=destroy_secrets,
                        delete_profile=False)
                    self._acknowledge_cleanup_profile(
                        instance, cleanup_token)
                else:
                    self.cleanup(
                        context, instance, network_info, block_device_info,
                        destroy_disks=destroy_disks,
                        destroy_secrets=destroy_secrets)
            with self._timed_phase(instance, 'destroy', 'claim_settlement'):
                if release_claim is not None:
                    self._remove_spawn_attempt_for_claim(
                        instance, release_claim)
                if not failed_build_cleanup:
                    self._retire_instance_idmap_claim_if_clean(instance)

    def cleanup(self, context, instance, network_info, block_device_info=None,
                destroy_disks=True, migrate_data=None, destroy_vifs=True,
                destroy_secrets=True):
        return self._cleanup(
            context, instance, network_info,
            block_device_info=block_device_info,
            destroy_disks=destroy_disks,
            migrate_data=migrate_data,
            destroy_vifs=destroy_vifs,
            destroy_secrets=destroy_secrets,
            delete_profile=True)

    def _cleanup(
            self, context, instance, network_info, block_device_info=None,
            destroy_disks=True, migrate_data=None, destroy_vifs=True,
            destroy_secrets=True, delete_profile=True):
        """Clean up the filesystem around the container.

        See `nova.virt.driver.ComputeDriver.cleanup` for more
        information.
        """
        failures = []
        retain_profile = not delete_profile

        def attempt(description, action):
            try:
                action()
            except Exception as exc:
                failures.append((description, exc))
                LOG.exception(
                    'Failed Incus cleanup step: %s',
                    description, instance=instance)

        if destroy_vifs:
            for vif in network_info or []:
                vif_id = vif.get('id', 'unknown')
                attempt(
                    'unplug destination VIF %s' % vif_id,
                    lambda vif=vif: self.vif_driver.unplug(instance, vif))
                attempt(
                    'remove firewall filter for VIF %s' % vif_id,
                    lambda vif=vif: self.firewall_driver.unfilter_instance(
                        instance, [vif]))

        profile = None
        try:
            profile = self.client.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                failures.append(('read Manila cleanup profile', exc))
        else:
            attempt(
                'unmount Manila staging paths',
                lambda: _cleanup_profile_share_mounts(profile, instance))
        attempt(
            'unmount journaled Manila staging paths',
            lambda: _cleanup_share_journal_mounts(instance))

        # ComputeManager releases Cinder attachments after destroy() returns;
        # the virt driver must disconnect host mappings before that happens.
        # Keep the profile until every data-volume disconnect succeeds because
        # it carries the exact os-brick device_info needed for an idempotent
        # retry. BFV roots are owned by Incus/cephext and never use os-brick.
        for bdm in driver.block_device_info_get_mapping(block_device_info):
            if _is_boot_volume(bdm):
                continue
            connection_info = bdm.get('connection_info')
            mountpoint = bdm.get('mount_device')
            if not connection_info or not mountpoint:
                continue
            # The detach transaction performs its own authoritative profile
            # reads and persistence checks. Reuse the initial snapshot only
            # to decide which Nova BDMs need that transaction; otherwise an
            # instance with N data volumes incurs N redundant Incus reads.
            if profile is None:
                break
            try:
                volume_id = _volume_id(connection_info)
            except Exception as exc:
                failures.append(('validate Cinder cleanup record', exc))
                LOG.exception(
                    'Failed to validate Cinder cleanup record',
                    instance=instance)
                continue
            if _profile_has_volume_connection(profile, volume_id):
                attach_intent = _read_managed_attach_intent(
                    instance, volume_id)
                retain_source_release = (
                    attach_intent is not None and
                    attach_intent.get('operation_kind') == 'migration' and
                    attach_intent.get('operation_direction') ==
                    'live-source-release')
                attempt(
                    'disconnect Cinder volume %s' % volume_id,
                    lambda connection_info=connection_info,
                    mountpoint=mountpoint,
                    retain_source_release=retain_source_release:
                    self._detach_volume(
                        context, connection_info, instance, mountpoint,
                        retain_journal=retain_source_release))

        # Nova can deliberately omit destination BDMs after a source-side
        # detach attempt. The versioned profile record is therefore the
        # authoritative retry journal for any mapping still present.
        failures.extend(
            ('disconnect profile-recorded Cinder volume %s' % volume_id, exc)
            for volume_id, exc in
            self._disconnect_profile_volume_connections(context, instance))

        attempt(
            'remove instance directory',
            lambda: _remove_instance_directory(instance))

        try:
            profile = self.client.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as e:
            if _is_incus_not_found(e):
                LOG.debug(
                    "Incus profile is already absent for %(instance)s; "
                    "cleanup is complete.",
                    {'instance': instance.name})
            else:
                failures.append(('read final Incus profile', e))
                LOG.exception(
                    'Failed to read final Incus cleanup profile',
                    instance=instance)
        else:
            if _profile_has_volume_connections(profile):
                retain_profile = True
                LOG.error(
                    'Retaining Incus profile because it contains host volume '
                    'connection metadata that requires cleanup',
                    instance=instance)
                if not any(
                        description.startswith('disconnect ')
                        for description, _exc in failures):
                    failures.append((
                        'disconnect retained Cinder volume metadata',
                        exception.InvalidVolume(
                            reason='Incus profile still contains Cinder '
                                   'connection metadata')))
            elif os.path.lexists(_volume_journal_directory(instance)):
                retain_profile = True
                failures.append((
                    'finish retained Cinder volume transaction',
                    exception.InvalidVolume(
                        reason='A durable Cinder attach/detach intent still '
                               'owns destination cleanup')))
                LOG.error(
                    'Retaining Incus profile because a durable Cinder '
                    'transaction is unfinished', instance=instance)
            elif delete_profile and not failures:
                attempt('delete Incus profile', profile.delete)
            else:
                LOG.error(
                    'Retaining Incus profile as a migration cleanup barrier',
                    instance=instance)

        retain_profile = retain_profile or bool(failures)
        if retain_profile:
            try:
                profile = self.client.profiles.get(instance.name)
                profile_config = (
                    profile.config if isinstance(profile.config, dict)
                    else {})
                profile_uuid = profile_config.get('user.openstack.uuid')
                foreign_users = _profile_users_other_than(profile, instance)
                if (profile_config.get('environment.product_name') !=
                        'OpenStack Nova' or
                        profile_uuid not in (None, instance.uuid) or
                        foreign_users):
                    raise exception.InvalidConfiguration(
                        'Refusing to mark a foreign or in-use Incus profile '
                        'for automatic cleanup')
                profile.config['user.openstack.uuid'] = instance.uuid
                profile.config[CLEANUP_RECOVERY_KEY] = 'true'
                profile.save(wait=True)
            except incus_exceptions.LXDAPIException as exc:
                if not _is_incus_not_found(exc):
                    raise
                if not failures:
                    failures.append((
                        'persist Incus cleanup retry journal',
                        exception.InvalidConfiguration(
                            'Incus cleanup profile disappeared')))
            except Exception as exc:
                failures.append((
                    'persist Incus cleanup retry journal', exc))
                LOG.critical(
                    'Failed to persist the Incus cleanup retry journal',
                    instance=instance, exc_info=True)

        if failures:
            raise exception.MigrationError(
                reason='%d Incus cleanup operation(s) failed; the profile '
                       'was retained as a retry and migration safety barrier'
                       % len(failures))

    def _acknowledge_cleanup_profile(self, instance, cleanup_token):
        """Persist token-bound proof that one destination is fully clean."""
        if not uuidutils.is_uuid_like(cleanup_token):
            raise exception.MigrationError(
                reason='Missing or invalid Incus cleanup token')

        def assert_instance_absent():
            try:
                self.client.instances.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if not _is_incus_not_found(exc):
                    raise
            else:
                raise exception.MigrationError(
                    reason='Incus cleanup cannot be acknowledged while the '
                           'destination instance record exists')

        assert_instance_absent()

        def validate_profile(profile):
            config = (
                profile.config if isinstance(profile.config, dict) else {})
            if (config.get('environment.product_name') != 'OpenStack Nova' or
                    config.get('user.openstack.uuid') != instance.uuid or
                    config.get(MIGRATION_CLEANUP_TOKEN_KEY) != cleanup_token or
                    profile.used_by or
                    _profile_has_volume_connections(profile) or
                    _volume_journal_records(instance) or
                    _profile_has_share_devices(profile) or
                    _share_journal_records(instance) or
                    any(os.path.ismount(path) for path in
                        _profile_share_mounts(profile, instance)) or
                    any(os.path.lexists(path) for path in (
                        common.InstanceAttributes(instance).instance_dir,
                        _volume_journal_directory(instance),
                        _share_journal_directory(instance)))):
                raise exception.MigrationError(
                    reason='Incus destination cleanup profile is not safe to '
                           'acknowledge')

        with lockutils.lock(_profile_lock_name(instance)):
            with lockutils.lock(
                    _idmap_host_claim_lock_name(instance.uuid), external=True,
                    lock_path=_idmap_host_claim_lock_path()):
                profile = self.client.profiles.get(instance.name)
                validate_profile(profile)
                retirement = self._retire_cleanup_ack_idmap_claim(
                    instance, profile)

                # Retirement is irreversible. Re-read all local barriers and
                # the all-project inventory before publishing the token-bound
                # ACK. Holding the claim lock prevents a new local claim from
                # crossing this proof window.
                profile = self.client.profiles.get(instance.name)
                assert_instance_absent()
                validate_profile(profile)
                if retirement is not None:
                    proof = _parse_idmap_retirement_proof(retirement)
                    if not _all_project_idmap_resources_absent(
                            self.inventory_client, instance.uuid,
                            proof.base, proof.size,
                            allowed_profile_name=instance.name):
                        raise incus_idmap.IDMapIntegrityError(
                            'Incus resources appeared after cleanup idmap '
                            'retirement; refusing to publish the cleanup ACK')
                original_config = dict(profile.config)
                if retirement is None:
                    profile.config.pop(MIGRATION_IDMAP_RETIREMENT_KEY, None)
                else:
                    profile.config[MIGRATION_IDMAP_RETIREMENT_KEY] = retirement
                profile.config[MIGRATION_CLEANUP_COMPLETE_KEY] = cleanup_token
                profile.config.pop(CLEANUP_RECOVERY_KEY, None)
                profile.config.pop(
                    MIGRATION_DESTINATION_PREPARED_KEY, None)
                profile.config.pop(MIGRATION_TARGET_OPERATION_KEY, None)
                try:
                    profile.save(wait=True)
                except Exception:
                    # A failed save leaves the server-side prepared marker in
                    # place. Restore it locally so retries using this resource
                    # object do not mistake an unsaved ACK for durable state.
                    profile.config = original_config
                    raise
        return True

    @_invalidates_instance_inventory
    def cleanup_lingering_instance_resources(self, instance):
        """Remove a stopped record left on a failed migration source."""
        try:
            container = self.client.instances.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if _is_incus_not_found(exc):
                container = None
            else:
                LOG.exception(
                    'Failed to query lingering Incus migration record',
                    instance=instance)
                return False
        except Exception:
            LOG.exception(
                'Failed to query lingering Incus migration record',
                instance=instance)
            return False

        if container is not None and container.status != 'Stopped':
            LOG.error(
                'Refusing to clean a running lingering Incus record',
                instance=instance)
            return False
        if (container is not None and
                _instance_nova_uuid(container) != instance.uuid):
            LOG.error(
                'Refusing to clean an Incus record owned by another UUID',
                instance=instance)
            return False

        root = container.devices.get('root', {}) if container else {}
        external_root = root.get('initial.ceph.rbd.image_name')
        if (external_root and
                not _instance_has_negotiated_handover(container)):
            LOG.error(
                'Refusing to clean an external BFV root without negotiated '
                'migration delete protection',
                instance=instance)
            return False

        try:
            if container is not None:
                _retry_incus_instance_action(
                    lambda: self._delete_migration_target_with_idmap(
                        self.client, instance),
                    'lingering migration record deletion',
                    instance, retry_transient=True)

            # The profile and host journals contain the only exact os-brick
            # device_info and Manila mount ownership proof. Replay them before
            # deleting the profile; _cleanup retains and marks it on failure.
            self._cleanup(
                None, instance, [], block_device_info=None,
                destroy_vifs=False, delete_profile=True)
            self._retire_instance_idmap_claim_if_clean(instance)
            return True
        except Exception:
            LOG.exception(
                'Failed to clean lingering Incus migration resources',
                instance=instance)
            return False

    def list_cleanup_recovery_candidates(self):
        """Return cleanup-marked profiles from the shared profile snapshot."""
        profiles = self._get_profile_inventory_snapshot()

        candidates = []
        for profile in profiles:
            config = profile.get('config')
            name = profile.get('name')
            if (not isinstance(config, dict) or
                    config.get(CLEANUP_RECOVERY_KEY) != 'true' or
                    config.get('environment.product_name') !=
                    'OpenStack Nova' or
                    not isinstance(name, str) or not name):
                continue
            instance_uuid = config.get('user.openstack.uuid')
            if not uuidutils.is_uuid_like(instance_uuid):
                LOG.error(
                    'Ignoring cleanup-marked Incus profile %s with an '
                    'invalid Nova UUID', name)
                continue
            candidates.append({
                'name': name,
                'uuid': instance_uuid,
            })
        return sorted(candidates, key=lambda item: item['name'])

    def list_source_volume_generation_recovery_candidates(self):
        """Return exact source rollback generations awaiting convergence."""
        candidates = []
        for profile in self._get_profile_inventory_snapshot():
            config = profile.get('config')
            name = profile.get('name')
            if (not isinstance(config, dict) or
                    config.get('environment.product_name') !=
                    'OpenStack Nova' or
                    not isinstance(name, str) or not name):
                continue
            instance_uuid = config.get('user.openstack.uuid')
            operation_token = config.get(MIGRATION_CLEANUP_TOKEN_KEY)
            migration_uuid = config.get(MIGRATION_NOVA_UUID_KEY)
            if (not uuidutils.is_uuid_like(instance_uuid) or
                    not uuidutils.is_uuid_like(operation_token) or
                    not uuidutils.is_uuid_like(migration_uuid)):
                continue
            rollback_token = config.get(MIGRATION_ROLLBACK_COMPLETE_KEY)
            if rollback_token not in (None, operation_token):
                continue
            candidates.append({
                'name': name,
                'uuid': instance_uuid,
                'operation_token': operation_token,
                'migration_uuid': migration_uuid,
                'rollback_complete': rollback_token == operation_token,
            })
        return sorted(candidates, key=lambda item: item['name'])

    def get_source_volume_generation_recovery_candidate(self, instance):
        """Read one exact source generation without the inventory cache."""
        with lockutils.lock(_profile_lock_name(instance)):
            profile = self.client.profiles.get(instance.name)
            _validate_profile_volume_owner(profile, instance)
            config = profile.config if isinstance(profile.config, dict) else {}
            operation_token = config.get(MIGRATION_CLEANUP_TOKEN_KEY)
            rollback_token = config.get(MIGRATION_ROLLBACK_COMPLETE_KEY)
            migration_uuid = config.get(MIGRATION_NOVA_UUID_KEY)
            if rollback_token is None and migration_uuid is None:
                return None
            if (not uuidutils.is_uuid_like(operation_token) or
                    not uuidutils.is_uuid_like(migration_uuid) or
                    rollback_token != operation_token):
                raise exception.MigrationError(
                    reason='Incus source rollback generation is incomplete')
            return {
                'name': instance.name,
                'uuid': instance.uuid,
                'operation_token': operation_token,
                'migration_uuid': migration_uuid,
            }

    def list_destination_prepared_recovery_candidates(self):
        """Return target profiles that survive without their mount journal."""
        profiles = self._get_profile_inventory_snapshot()

        candidates = []
        for profile in profiles:
            config = profile.get('config')
            name = profile.get('name')
            if (not isinstance(config, dict) or
                    MIGRATION_DESTINATION_PREPARED_KEY not in config or
                    not isinstance(name, str) or not name):
                continue
            # Once cleanup() has published its own retry marker, the existing
            # cleanup recovery loop owns this profile and avoids duplicate
            # host unmount/disconnect attempts.
            if config.get(CLEANUP_RECOVERY_KEY) == 'true':
                continue
            try:
                binding = _destination_prepared_profile_binding(config)
            except exception.MigrationError:
                LOG.error(
                    'Ignoring destination-prepared Incus profile %s with an '
                    'invalid transaction binding', name, exc_info=True)
                continue
            candidates.append({'name': name, **binding})
        return sorted(candidates, key=lambda item: item['name'])

    def _abort_and_cleanup_destination_profile(
            self, context, instance, cleanup_token, idmap_base, idmap_size,
            network_info):
        """Fence a non-committed target, then publish a durable cleanup ACK."""
        attempt = _abort_migration_attempt(
            self.client, instance, cleanup_token, idmap_base, idmap_size,
            target_cleanup=lambda: _retry_migration_finish_action(
                lambda: self._delete_migration_target_with_idmap(
                    self.client, instance),
                'aborted prepared migration target deletion', instance))
        if attempt['state'] == 'committed':
            return attempt
        if (attempt['state'] not in ('aborted', 'failed') or
                not attempt.get('finished')):
            raise exception.MigrationError(
                reason='Prepared Incus migration destination did not reach '
                       'a terminal non-committed state')

        try:
            self.client.instances.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
        else:
            raise exception.MigrationError(
                reason='Refusing prepared destination cleanup while its '
                       'Incus instance record still exists')

        self._cleanup(
            context, instance, network_info, block_device_info=None,
            destroy_vifs=True, delete_profile=False)
        self._acknowledge_cleanup_profile(instance, cleanup_token)
        return attempt

    def recover_destination_prepared_profile(
            self, context, instance, candidate, migration, network_info):
        """Reconcile a profile that outlived its destination callback."""
        expected = {
            'name': instance.name,
            'uuid': instance.uuid,
        }
        if any(candidate.get(key) != value for key, value in expected.items()):
            raise exception.MigrationError(
                reason='Prepared destination profile owner does not match '
                       'the Nova instance')
        cleanup_token = candidate.get('operation_token')
        migration_uuid = candidate.get('migration_uuid')
        if (getattr(migration, 'uuid', None) != migration_uuid or
                not uuidutils.is_uuid_like(cleanup_token) or
                not uuidutils.is_uuid_like(migration_uuid)):
            raise exception.MigrationError(
                reason='Prepared destination profile does not match the '
                       'exact Nova migration')

        with lockutils.lock(_profile_lock_name(instance)):
            profile = self.client.profiles.get(instance.name)
            config = (
                profile.config if isinstance(profile.config, dict) else {})
            binding = _destination_prepared_profile_binding(config)
            if any(binding.get(key) != candidate.get(key) for key in (
                    'uuid', 'operation_token', 'migration_uuid', 'idmap_base',
                    'idmap_size')):
                raise exception.MigrationError(
                    reason='Prepared destination profile changed during '
                           'recovery')

        try:
            container = self.client.instances.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
            container = None
        if (container is not None and
                _instance_nova_uuid(container) != instance.uuid):
            raise exception.MigrationError(
                reason='Prepared migration target UUID does not match Nova')

        attempt = _get_migration_attempt(
            self.client, instance, cleanup_token,
            candidate['idmap_base'], candidate['idmap_size'])
        if (attempt['state'] != 'committed' and
                instance.host == getattr(migration, 'dest_compute', None)):
            raise exception.MigrationError(
                reason='Nova names this compute as instance owner but the '
                       'Incus migration attempt was not committed')
        if attempt['state'] != 'committed':
            attempt = self._abort_and_cleanup_destination_profile(
                context, instance, cleanup_token,
                candidate['idmap_base'], candidate['idmap_size'],
                network_info)
            if attempt['state'] != 'committed':
                return True

        # Abort is a no-op after the server proves commit. Never unmount or
        # delete committed storage unless Nova also proves this destination is
        # the authoritative host and its target record still exists.
        if container is None:
            raise exception.MigrationError(
                reason='Committed Incus migration attempt has no target '
                       'instance record; refusing automatic cleanup')
        if instance.host != getattr(migration, 'dest_compute', None):
            raise exception.MigrationError(
                reason='Committed Incus migration target is not the Nova '
                       'instance owner; refusing automatic cleanup')
        _converge_migration_target_ownership(
            self.client, instance, local_volume_evidence=True)
        return True

    def list_share_journal_recovery_candidates(self):
        """Return migration journal owners for manager-side validation."""
        return _share_journal_recovery_candidates()

    def holds_volume_attachment(self, instance, volume_id):
        """Return whether this host still owns the guest side of a volume.

        Used to tell an abandoned detach from one that is merely in flight.
        A journal means the driver reached the disconnect and its own
        recovery owns the outcome; a profile device with no journal means
        nothing was released and the guest never lost the volume. Any
        uncertainty answers False so the caller leaves the volume alone.
        """
        try:
            if _volume_journal_records(instance).get(volume_id) is not None:
                return False
            profile = self.client.profiles.get(instance.name)
        except Exception:
            return False
        devices = profile.devices if isinstance(profile.devices, dict) else {}
        device = devices.get(volume_id)
        return (isinstance(device, dict) and
                device.get('type') == 'unix-block')

    def prepare_managed_volume_detach(
            self, instance, volume_id, attachment_id, destroy_bdm,
            mountpoint):
        """Record that ComputeManager may finish Cinder/BDM cleanup."""
        return _write_managed_detach_intent(
            instance, volume_id, attachment_id, destroy_bdm, mountpoint)

    def prepare_managed_volume_attach(
            self, instance, volume_id, attachment_id, mountpoint,
            operation_kind='hot-attach', operation_token=None,
            operation_direction=None, operation_migration_uuid=None,
            boot_volume=False):
        return _write_managed_attach_intent(
            instance, volume_id, attachment_id, mountpoint,
            operation_kind=operation_kind,
            operation_token=operation_token,
            operation_direction=operation_direction,
            operation_migration_uuid=operation_migration_uuid,
            boot_volume=boot_volume)

    def get_managed_volume_attach_intent(self, instance, volume_id):
        return _read_managed_attach_intent(instance, volume_id)

    def publish_migration_target_volumes_complete(
            self, instance, operation_token, migration_uuid):
        return _publish_migration_target_volumes_complete(
            self.client, instance, operation_token, migration_uuid)

    def get_cold_source_migration_token(self, instance):
        """Return the exact source-profile generation for a cold migration."""
        with lockutils.lock(_profile_lock_name(instance)):
            profile = self.client.profiles.get(instance.name)
            _validate_profile_volume_owner(profile, instance)
            config = profile.config if isinstance(profile.config, dict) else {}
            token = config.get(MIGRATION_CLEANUP_TOKEN_KEY)
            if (not uuidutils.is_uuid_like(token) or
                    not config.get(MIGRATION_DESTINATION_KEY) or
                    not uuidutils.is_uuid_like(
                        config.get(MIGRATION_OPERATION_KEY))):
                raise exception.MigrationError(
                    reason='Incus cold source has no durable migration owner')
            return token

    def prepare_cold_attachment_rotation(
            self, instance, volume_id, old_attachment_id, mountpoint,
            operation_token, migration_uuid, baseline_attachment_ids,
            boot_volume=False):
        """Fence a Cinder replacement attachment before its POST request."""
        payload = {
            'version': _COLD_ATTACHMENT_ROTATION_VERSION,
            'instance_uuid': instance.uuid,
            'instance_name': instance.name,
            'volume_id': str(volume_id),
            'mountpoint': mountpoint,
            'operation_token': operation_token,
            'migration_uuid': migration_uuid,
            'old_attachment_id': old_attachment_id,
            'new_attachment_id': None,
            'baseline_attachment_ids': sorted(set(
                str(value) for value in baseline_attachment_ids)),
            'phase': 'prepared',
            'boot_volume': bool(boot_volume),
        }
        return _write_cold_attachment_rotation(
            instance, volume_id, payload)

    def get_cold_attachment_rotation(self, instance, volume_id):
        return _read_cold_attachment_rotation(instance, volume_id)

    def transition_cold_attachment_rotation(
            self, instance, volume_id, expected, phase,
            new_attachment_id=None):
        transitions = {
            'prepared': 'creating',
            'creating': 'new-created',
            'new-created': 'old-deleted',
            'old-deleted': 'bdm-rotated',
        }
        terminal_transitions = {
            'source-release-complete': {'bdm-rotated'},
            'source-rollback-complete': {
                'prepared', 'source-old-retained', 'old-deleted',
                'bdm-rotated'},
        }
        expected_phase = expected.get('phase')
        if (expected_phase == 'new-created' and
                phase == 'source-old-retained'):
            pass
        elif (transitions.get(expected_phase) != phase and
                expected_phase not in terminal_transitions.get(phase, set())):
            raise exception.InvalidVolume(
                reason='Cold attachment rotation transition is invalid')
        payload = copy.deepcopy(expected)
        payload['phase'] = phase
        if phase == 'new-created':
            if not uuidutils.is_uuid_like(new_attachment_id):
                raise exception.InvalidVolume(
                    reason='Cold attachment rotation has no replacement ID')
            payload['new_attachment_id'] = str(new_attachment_id)
        elif (new_attachment_id is not None and
              new_attachment_id != payload.get('new_attachment_id')):
            raise exception.InvalidVolume(
                reason='Cold attachment rotation replacement ID changed')
        return _write_cold_attachment_rotation(
            instance, volume_id, payload, expected=expected)[0]

    def cancel_cold_attachment_rotation(
            self, instance, volume_id, expected):
        _remove_cold_attachment_rotation(instance, volume_id, expected)

    def validate_internal_volume_attach_owner(self, instance, intent):
        """Validate durable Incus-side authority for an internal attach."""
        operation_kind = intent.get('operation_kind')
        if operation_kind == 'hot-attach':
            return
        profile = self.client.profiles.get(instance.name)
        _validate_profile_volume_owner(profile, instance)
        config = profile.config if isinstance(profile.config, dict) else {}
        if operation_kind == 'migration':
            if config.get(MIGRATION_CLEANUP_TOKEN_KEY) != intent.get(
                    'operation_token'):
                raise exception.InvalidVolume(
                    reason='Incus migration profile no longer owns the volume '
                           'attach generation')
            return

        if operation_kind == 'spawn' and config.get(
                SPAWN_VOLUME_GENERATION_KEY) != intent.get('operation_token'):
            raise exception.InvalidVolume(
                reason='Incus profile no longer owns the spawn volume '
                       'generation')

        try:
            container = self.client.instances.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if operation_kind == 'spawn' and _is_incus_not_found(exc):
                # Older failed-spawn cleanup could delete the container before
                # retiring its volume transaction.  The exact profile UUID,
                # spawn generation, BDM and Cinder attachment remain enough
                # authority to roll back only this host mapping.
                return
            raise
        if _instance_nova_uuid(container) != instance.uuid:
            raise exception.InvalidVolume(
                reason='Incus instance no longer owns the volume attach')
        if operation_kind == 'reconcile':
            if intent.get('operation_token') != intent.get('attachment_id'):
                raise exception.InvalidVolume(
                    reason='Power reconciliation attachment generation '
                           'changed')
            return

        container_config = (
            container.config if isinstance(container.config, dict) else {})
        materialization_id = container_config.get(
            IDMAP_MATERIALIZATION_CONFIG_KEY)
        if (materialization_id is not None and
                materialization_id != intent.get('operation_token')):
            raise exception.InvalidVolume(
                reason='Incus spawn materialization generation changed')
        if self.idmap_allocator is not None:
            host_id = virt_node.read_local_node_uuid()
            claim = self.idmap_allocator.get_host_claim(
                instance.uuid, host_id)
            if (claim is None or claim.materialization_id !=
                    intent.get('operation_token') or
                    claim.state != 'committed'):
                raise exception.InvalidVolume(
                    reason='Incus spawn volume owner has no exact committed '
                           'materialization claim')

    def internal_migration_attach_disposition(self, instance, intent):
        """Return the token-bound target migration attempt disposition."""
        if (intent.get('operation_kind') != 'migration' or
                intent.get('operation_direction') not in (
                    'cold-target', 'live-target')):
            raise exception.InvalidVolume(
                reason='Internal volume attach is not a migration target')
        profile = self.client.profiles.get(instance.name)
        _validate_profile_volume_owner(profile, instance)
        config = profile.config if isinstance(profile.config, dict) else {}
        binding = _destination_prepared_profile_binding(config)
        if (binding['uuid'] != instance.uuid or
                binding['operation_token'] != intent.get('operation_token') or
                binding['migration_uuid'] !=
                intent.get('operation_migration_uuid')):
            raise exception.InvalidVolume(
                reason='Incus migration target volume owner changed')
        attempt = _get_migration_attempt(
            self.client, instance, binding['operation_token'],
            binding['idmap_base'], binding['idmap_size'])
        if attempt['state'] == 'committed' and attempt.get('finished'):
            return 'committed'
        if (attempt['state'] in ('aborted', 'failed') and
                attempt.get('finished')):
            return 'aborted'
        return 'active'

    def cancel_managed_volume_attach(self, instance, volume_id, intent):
        _remove_managed_attach_intent(instance, volume_id, expected=intent)

    def replace_cold_source_volume_attach_intent(
            self, instance, volume_id, expected, attachment_id,
            operation_direction=None):
        return _replace_managed_attach_intent(
            instance, volume_id, expected, attachment_id,
            operation_direction=operation_direction)

    def resume_internal_volume_attach(
            self, context, instance, volume_id, connection_info,
            expected_mountpoint):
        """Commit an exact internal attach, including intent-only crashes."""
        record = _read_volume_journal(instance, volume_id)
        if record is None:
            self._attach_volume_locked(
                context, connection_info, instance, expected_mountpoint,
                retain_journal=True)
        else:
            self.resume_connecting_volume_journal(
                context, instance, volume_id, connection_info,
                expected_mountpoint=expected_mountpoint)
        self.confirm_connected_volume_journal(
            instance, volume_id, connection_info,
            expected_mountpoint=expected_mountpoint)

    def rollback_internal_volume_attach(
            self, context, instance, volume_id, connection_info,
            expected_mountpoint):
        """Remove only the local mapping owned by an internal attach intent."""
        record = _read_volume_journal(instance, volume_id)
        if record is None:
            profile = self.client.profiles.get(instance.name)
            _validate_profile_volume_owner(profile, instance)
            if not _profile_has_volume_connection(profile, volume_id):
                _validate_profile_volume_slot(
                    profile, volume_id, expected_mountpoint,
                    replacing_volume_id=volume_id)
                return
            qos_limits = _data_volume_qos(
                connection_info,
                self.client.host_info.get('api_extensions', []))
            if not _profile_volume_attachment_matches(
                    profile, volume_id, expected_mountpoint, qos_limits,
                    connection_info):
                raise exception.InvalidVolume(
                    reason='Incus migration target volume identity changed '
                           'before rollback')
            self._detach_volume_locked(
                context, connection_info, instance, expected_mountpoint,
                retain_journal=True)
        self.rollback_connecting_volume_journal(
            context, instance, volume_id,
            connection_info=connection_info,
            expected_mountpoint=expected_mountpoint)

    def restart_internal_volume_attach(
            self, context, instance, volume_id, connection_info,
            expected_mountpoint):
        """Replace a completed local rollback with the same formal owner."""
        record = _read_volume_journal(instance, volume_id)
        phase = record.get('phase') if isinstance(record, dict) else None
        if phase not in ('disconnecting', 'disconnected', 'rolled-back'):
            raise exception.InvalidVolume(
                reason='Internal Cinder attach has no restartable rollback '
                       'journal')
        self.rollback_internal_volume_attach(
            context, instance, volume_id, connection_info,
            expected_mountpoint=expected_mountpoint)
        self.finalize_rolled_back_volume_journal(instance, volume_id)
        # The managed intent remains durable across the interval between
        # retiring the old rollback record and writing the new connecting
        # record, so a process death is replayable from exact Cinder/BDM data.
        self.resume_internal_volume_attach(
            context, instance, volume_id, connection_info,
            expected_mountpoint=expected_mountpoint)

    def get_internal_volume_attach_connection_info(
            self, instance, volume_id, expected_mountpoint):
        """Return the exact local connection owned by an internal intent.

        Cinder may already have deleted a failed migration target attachment
        and Nova may have restored the BDM to the source attachment.  The
        destination journal/profile is therefore the only valid source for
        the destination host connection that must be rolled back.
        """
        record = _read_volume_journal(instance, volume_id)
        profile = self.client.profiles.get(instance.name)
        _validate_profile_volume_owner(profile, instance)
        devices = profile.devices if isinstance(profile.devices, dict) else {}
        device = devices.get(volume_id)
        if record is None:
            if not _profile_has_volume_connection(profile, volume_id):
                _validate_profile_volume_slot(
                    profile, volume_id, expected_mountpoint,
                    replacing_volume_id=volume_id)
                return None
            record = _profile_volume_record(
                profile, volume_id, device=device)

        mountpoint = record.get('mountpoint') or (device or {}).get('path')
        protocol = record.get('driver_volume_type')
        connection_data = record.get('connection_data')
        if (mountpoint != expected_mountpoint or
                not isinstance(protocol, str) or not protocol or
                not isinstance(connection_data, dict)):
            raise exception.InvalidVolume(
                reason='Internal Cinder attach has incomplete or changed '
                       'local connection metadata')
        return {
            'serial': volume_id,
            # The journal path and payload are already bound to this exact
            # Nova instance. Rehydrate the outer Cinder identity that is not
            # duplicated inside the credential-free connection_data record.
            'instance': instance.uuid,
            'driver_volume_type': protocol,
            'data': copy.deepcopy(connection_data),
        }

    def get_managed_volume_detach_intent(self, instance, volume_id):
        return _read_managed_detach_intent(instance, volume_id)

    def get_volume_journal_phase(self, instance, volume_id):
        record = _read_volume_journal(instance, volume_id)
        return record.get('phase') if record is not None else None

    def get_volume_journal_recovery_phase(self, instance, volume_id):
        """Re-read the intent owner and journal beneath manager locks."""
        return _volume_recovery_phase(
            _read_volume_journal(instance, volume_id),
            _read_managed_attach_intent(instance, volume_id),
            _read_managed_detach_intent(instance, volume_id),
            _read_cold_attachment_rotation(instance, volume_id))

    def cancel_managed_volume_detach(self, instance, volume_id, intent):
        _remove_managed_detach_intent(instance, volume_id, expected=intent)

    def _recover_disconnecting_volume_journal_locked(
            self, context, instance, volume_id, connection_info,
            expected_mountpoint):
        """Replay host cleanup after manager validates its detach intent."""
        attachment = _read_volume_journal(instance, volume_id)
        if attachment is None:
            return
        phase = _validate_volume_recovery_record(
            attachment, volume_id, expected_mountpoint, connection_info)
        if phase == 'disconnected':
            return
        if phase != 'disconnecting':
            raise exception.InvalidVolume(
                reason='Cinder volume %s is not in detach recovery' %
                       volume_id)
        connection_data = attachment.get('connection_data') or {}
        if not connection_data:
            raise exception.InvalidVolume(
                reason='Cinder journal has no connector data')
        protocol = attachment.get('driver_volume_type')
        if not protocol:
            raise exception.InvalidVolume(
                reason='Cinder journal has no connector protocol')
        device_info = attachment.get('device_info') or {}
        device_path = device_info.get('path')
        if not isinstance(device_path, str) or not device_path:
            raise exception.InvalidVolume(
                reason='Cinder disconnect journal has no device path')
        device_path = os.path.realpath(device_path)
        _validate_block_device_path(
            device_path, 'Recovered os-brick disconnect path')

        # The process can die after journaling intent but before removing guest
        # access. Persist the exact profile transition before touching os-brick
        # so manager-side Cinder cleanup can never strand a live guest device.
        with lockutils.lock(_profile_lock_name(instance)):
            profile = self.client.profiles.get(instance.name)
            _validate_profile_volume_owner(profile, instance)
            device = profile.devices.get(volume_id)
            if device is not None and (
                    device.get('type') != 'unix-block' or
                    device.get('path') != expected_mountpoint or
                    os.path.realpath(device.get('source', '')) != device_path):
                raise exception.InvalidVolume(
                    reason='Incus guest device no longer matches the '
                           'disconnect journal')
            profile.devices.pop(volume_id, None)
            profile.config[_volume_device_info_key(volume_id)] = (
                _serialize_volume_attachment(
                    connection_info, device_info, expected_mountpoint,
                    phase='disconnecting'))
            profile.config.pop(
                _legacy_volume_device_info_key(volume_id), None)
            try:
                profile.save(wait=True)
            except Exception:
                persisted = self.client.profiles.get(instance.name)
                persisted_device = persisted.devices.get(volume_id)
                persisted_record = _profile_volume_record(
                    persisted, volume_id, device=persisted_device)
                if (persisted_device is not None or
                        persisted_record.get('phase') != 'disconnecting'):
                    raise
                LOG.warning(
                    'Incus reported a failed recovery profile update for '
                    'Cinder volume %(volume)s, but guest access is removed '
                    'and its disconnect journal is persisted',
                    {'volume': volume_id}, instance=instance)
        LOG.warning(
            'Replaying the unfinished Cinder disconnect for volume %s',
            volume_id, instance=instance)
        brick_get_connector(protocol).disconnect_volume(
            connection_data, device_info)
        _write_volume_journal(
            instance, volume_id, connection_info,
            attachment.get('device_info') or {}, expected_mountpoint,
            phase='disconnected')

    def recover_source_release_volume_journal(
            self, context, instance, volume_id, expected_mountpoint):
        """Serialize cleanup replay against manager and periodic recovery."""
        with lockutils.lock(
                _volume_manager_transaction_lock_name(
                    instance.uuid, volume_id),
                external=True, lock_path=_volume_operation_lock_path()):
            with lockutils.lock(
                    _volume_topology_lock_name(instance), external=True,
                    lock_path=_volume_topology_lock_path()):
                with lockutils.lock(
                        _volume_operation_lock_name(volume_id), external=True,
                        lock_path=_volume_operation_lock_path()):
                    return self._recover_source_release_volume_journal_locked(
                        context, instance, volume_id, expected_mountpoint)

    def _recover_source_release_volume_journal_locked(
            self, context, instance, volume_id, expected_mountpoint):
        """Finish a migration source disconnect from its local evidence."""
        attachment = _read_volume_journal(instance, volume_id)
        if attachment is None:
            profile = self.client.profiles.get(instance.name)
            _validate_profile_volume_owner(profile, instance)
            device = profile.devices.get(volume_id)
            record = _profile_volume_record(
                profile, volume_id, device=device)
            if not record:
                return
            connection_info = {
                'serial': volume_id,
                'driver_volume_type': record.get('driver_volume_type'),
                'data': copy.deepcopy(record.get('connection_data') or {}),
            }
            if (record.get('mountpoint') != expected_mountpoint or
                    not connection_info['driver_volume_type']):
                raise exception.InvalidVolume(
                    reason='Migration source volume metadata is incomplete')
            self._detach_volume_locked(
                context, connection_info, instance, expected_mountpoint,
                retain_journal=True)
            return
        connection_info = {
            'serial': volume_id,
            'driver_volume_type': attachment.get('driver_volume_type'),
            'data': copy.deepcopy(attachment.get('connection_data') or {}),
        }
        self._recover_disconnecting_volume_journal_locked(
            context, instance, volume_id, connection_info,
            expected_mountpoint)

    def finalize_disconnected_volume_journal(self, instance, volume_id):
        """Clear host evidence after manager finishes Cinder/BDM cleanup."""
        record = _read_volume_journal(instance, volume_id)
        if record is None:
            return
        if record.get('phase') != 'disconnected':
            raise exception.InvalidVolume(
                reason='Cinder volume %s has a non-disconnected journal' %
                       volume_id)
        try:
            profile = self.client.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
        else:
            _validate_profile_volume_owner(profile, instance)
            if profile.devices.get(volume_id) is not None:
                raise exception.InvalidVolume(
                    reason='Incus still exposes disconnected Cinder volume '
                           '%s' % volume_id)
            changed = False
            for metadata_key in _volume_device_info_keys(volume_id):
                if metadata_key in profile.config:
                    profile.config.pop(metadata_key, None)
                    changed = True
            if changed:
                profile.save(wait=True)
        _remove_volume_journal(instance, volume_id)

    def _retire_disconnected_source_profile_device_locked(
            self, instance, volume_id, expected_mountpoint,
            connection_info):
        """Remove an exact source device after its host disconnect."""
        journal = _read_volume_journal(instance, volume_id)
        if journal is None:
            raise exception.InvalidVolume(
                reason='Migration source volume release lost its '
                       'disconnected host evidence')
        phase = _validate_volume_recovery_record(
            journal, volume_id, expected_mountpoint, connection_info)
        if phase != 'disconnected':
            raise exception.InvalidVolume(
                reason='Migration source volume has non-terminal host '
                       'evidence')
        device_info = journal.get('device_info') or {}
        device_path = os.path.realpath(device_info.get('path', ''))
        if not device_path:
            raise exception.InvalidVolume(
                reason='Migration source disconnect journal has no device '
                       'path')
        with lockutils.lock(_profile_lock_name(instance)):
            try:
                profile = self.client.profiles.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if _is_incus_not_found(exc):
                    return
                raise
            _validate_profile_volume_owner(profile, instance)
            device = profile.devices.get(volume_id)
            if device is not None and (
                    device.get('type') != 'unix-block' or
                    device.get('path') != expected_mountpoint or
                    os.path.realpath(device.get('source', '')) !=
                    device_path):
                raise exception.InvalidVolume(
                    reason='Incus source device no longer matches the '
                           'disconnected migration journal')
            if device is None:
                return
            profile.devices.pop(volume_id)
            profile.config[_volume_device_info_key(volume_id)] = (
                _serialize_volume_attachment(
                    connection_info, device_info, expected_mountpoint,
                    phase='disconnected'))
            profile.config.pop(
                _legacy_volume_device_info_key(volume_id), None)
            profile.save(wait=True)

    def validate_disconnected_volume_state(self, instance, volume_id):
        """Prove an intent-only terminal detach has no local guest state."""
        if _read_volume_journal(instance, volume_id) is not None:
            raise exception.InvalidVolume(
                reason='Cinder volume still has a host cleanup journal')
        try:
            profile = self.client.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
            return
        _validate_profile_volume_owner(profile, instance)
        if _profile_has_volume_connection(profile, volume_id):
            raise exception.InvalidVolume(
                reason='Incus still references terminally detached Cinder '
                       'volume %s' % volume_id)

    def resume_connecting_volume_journal(
            self, context, instance, volume_id, connection_info,
            expected_mountpoint=None):
        """Finish guest-side attach while retaining the commit journal.

        Cinder still reports the attachment as ``attaching`` when this is
        called.  The journal must therefore survive the Incus profile commit:
        the manager still has to persist the Nova BDM and call Cinder's
        attachment-complete API.  A compute crash at either point re-enters
        this method idempotently instead of leaking an os-brick mapping.
        """
        record = _read_volume_journal(instance, volume_id)
        if record is None:
            raise exception.InvalidVolume(
                reason='Cinder connecting journal disappeared during recovery')
        phase = _validate_volume_recovery_record(
            record, volume_id, record.get('mountpoint'), connection_info)
        if phase not in ('connecting', 'connected'):
            raise exception.InvalidVolume(
                reason='Cinder volume %s is not in attach recovery' %
                       volume_id)
        if (expected_mountpoint is not None and
                record.get('mountpoint') != expected_mountpoint):
            raise exception.InvalidVolume(
                reason='Nova BDM target for volume %s does not match the '
                       'connecting journal' % volume_id)

        # The compute manager holds instance-topology then per-volume locks
        # across Cinder/BDM commit. Do not reacquire the non-reentrant volume
        # lock here.
        self._attach_volume_locked(
            context, connection_info, instance, record['mountpoint'],
            retain_journal=True)
        committed = _read_volume_journal(instance, volume_id)
        if committed is None or committed.get('phase') != 'connected':
            raise exception.InvalidVolume(
                reason='Cinder volume %s did not reach the local connected '
                       'recovery phase' % volume_id)
        return record['mountpoint']

    def confirm_connected_volume_journal(
            self, instance, volume_id, connection_info,
            expected_mountpoint=None):
        """Remove a journal only after Cinder and Incus agree on attach."""
        record = _read_volume_journal(instance, volume_id)
        if record is None:
            # A manager can commit the host journal and then fail to fsync
            # removal of its exact attach intent. The formally attached BDM
            # still needs a full Incus profile proof before that last intent
            # is retired, even though no journal remains to validate.
            if expected_mountpoint is None:
                return
            mountpoint = expected_mountpoint
        else:
            mountpoint = record.get('mountpoint')
            phase = _validate_volume_recovery_record(
                record, volume_id, mountpoint, connection_info)
            if phase != 'connected':
                raise exception.InvalidVolume(
                    reason='Cinder reports volume %s attached while its '
                           'local journal is still %s' % (volume_id, phase))
            if (expected_mountpoint is not None and
                    mountpoint != expected_mountpoint):
                raise exception.InvalidVolume(
                    reason='Nova BDM target for volume %s does not match the '
                           'connected journal' % volume_id)

        profile = self.client.profiles.get(instance.name)
        _validate_profile_volume_owner(profile, instance)
        qos_limits = _data_volume_qos(
            connection_info,
            self.client.host_info.get('api_extensions', []))
        if not _profile_volume_attachment_matches(
                profile, volume_id, mountpoint, qos_limits,
                connection_info):
            raise exception.InvalidVolume(
                reason='Cinder reports volume %s attached but Incus has no '
                       'matching guest device' % volume_id)
        if record is not None:
            _remove_volume_journal(instance, volume_id)

    def rollback_connecting_volume_journal(
            self, context, instance, volume_id, connection_info=None,
            expected_mountpoint=None):
        """Remove host ownership and persist a monotonic rollback commit.

        ``connecting`` may repeat the exact connector request to recover its
        cleanup handle. A failed attach can also have entered the ordinary
        detach path; its ``disconnecting``/``disconnected`` journal remains
        owned by the same managed attach intent and is normalized here. After
        disconnect succeeds the journal atomically advances to
        ``rolled-back``. Replaying that phase never calls os-brick again; the
        manager only finishes Cinder/BDM bookkeeping and removes the journal.
        """
        record = _read_volume_journal(instance, volume_id)
        if record is None:
            return
        mountpoint = record.get('mountpoint')
        if (expected_mountpoint is not None and
                mountpoint != expected_mountpoint):
            raise exception.InvalidVolume(
                reason='Nova BDM target for volume %s does not match the '
                       'rollback journal' % volume_id)
        phase = record.get('phase')
        if phase not in (
                'connecting', 'connected', 'disconnecting', 'disconnected',
                'rolled-back'):
            raise exception.InvalidVolume(
                reason='Cinder volume %s is not in attach rollback' %
                       volume_id)
        stored_data = dict(record.get('connection_data') or {})
        effective = copy.deepcopy(connection_info or {})
        effective_data = stored_data
        effective_data.update(effective.get('data') or {})
        effective.update({
            'serial': volume_id,
            'driver_volume_type': (
                effective.get('driver_volume_type') or
                record.get('driver_volume_type')),
            'data': effective_data,
        })
        _validate_recoverable_data_volume(effective, volume_id)
        _validate_volume_recovery_record(
            record, volume_id, mountpoint, effective)

        if phase == 'rolled-back':
            # Older code could persist the journal commit before removing the
            # matching terminal profile metadata. Retire only that exact
            # metadata on replay; host ownership is already gone, so never
            # reconnect or disconnect the volume in this phase.
            with lockutils.lock(_profile_lock_name(instance)):
                try:
                    profile = self.client.profiles.get(instance.name)
                except incus_exceptions.LXDAPIException as exc:
                    if not _is_incus_not_found(exc):
                        raise
                    profile = None
                if profile is not None:
                    _validate_profile_volume_owner(profile, instance)
                    if profile.devices.get(volume_id) is not None:
                        raise exception.InvalidVolume(
                            reason='Rolled-back Cinder volume still has an '
                                   'Incus guest device')
                    metadata_keys = [
                        key for key in _volume_device_info_keys(volume_id)
                        if key in profile.config]
                    if metadata_keys:
                        profile_record = _profile_volume_record(
                            profile, volume_id)
                        profile_phase = _validate_volume_recovery_record(
                            profile_record, volume_id, mountpoint, effective)
                        if profile_phase not in (
                                'disconnecting', 'disconnected'):
                            raise exception.InvalidVolume(
                                reason='Rolled-back Cinder volume has '
                                       'non-terminal Incus metadata')
                        for metadata_key in metadata_keys:
                            profile.config.pop(metadata_key, None)
                        profile.save(wait=True)
            return

        # attach_volume() uses the normal detach implementation to undo a
        # failed host connect. A crash in that cleanup therefore leaves a
        # disconnecting/disconnected journal owned by the original managed
        # attach intent, not by a Nova detach request. Finish only that local
        # cleanup here, then convert its commit marker into the attach
        # rollback phase used for exact Cinder/BDM finalization.
        if phase == 'disconnecting':
            self._recover_disconnecting_volume_journal_locked(
                context, instance, volume_id, effective,
                expected_mountpoint=mountpoint)
            record = _read_volume_journal(instance, volume_id)
            if record is None or record.get('phase') != 'disconnected':
                raise exception.InvalidVolume(
                    reason='Failed attach cleanup did not reach the local '
                           'disconnected phase')
            phase = 'disconnected'
        if phase == 'disconnected':
            with lockutils.lock(_profile_lock_name(instance)):
                try:
                    profile = self.client.profiles.get(instance.name)
                except incus_exceptions.LXDAPIException as exc:
                    if not _is_incus_not_found(exc):
                        raise
                    profile = None
                if profile is not None:
                    _validate_profile_volume_owner(profile, instance)
                    if profile.devices.get(volume_id) is not None:
                        raise exception.InvalidVolume(
                            reason='Disconnected Cinder volume still has an '
                                   'Incus guest device')
                    metadata_keys = [
                        key for key in _volume_device_info_keys(volume_id)
                        if key in profile.config]
                    if metadata_keys:
                        profile_record = _profile_volume_record(
                            profile, volume_id)
                        profile_phase = _validate_volume_recovery_record(
                            profile_record, volume_id, mountpoint, effective)
                        if profile_phase not in (
                                'disconnecting', 'disconnected'):
                            raise exception.InvalidVolume(
                                reason='Disconnected Cinder volume has '
                                       'non-terminal Incus metadata')
                        for metadata_key in metadata_keys:
                            profile.config.pop(metadata_key, None)
                        profile.save(wait=True)
            _write_volume_journal(
                instance, volume_id, effective,
                record.get('device_info') or {}, mountpoint,
                phase='rolled-back')
            return

        protocol = effective['driver_volume_type']
        connector = brick_get_connector(protocol)
        device_info = record.get('device_info') or {}
        if not device_info.get('path'):
            device_info = connector.connect_volume(effective_data)
        if (not isinstance(device_info, dict) or
                not device_info.get('path')):
            raise exception.InvalidVolume(
                reason='os-brick could not recover device information for '
                       'unfinished Cinder volume %s' % volume_id)
        device_path = os.path.realpath(device_info['path'])
        _validate_block_device_path(
            device_path, 'Recovered os-brick connector path')

        try:
            profile = self.client.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
            profile = None
        if profile is not None:
            _validate_profile_volume_owner(profile, instance)
            device = profile.devices.get(volume_id)
            if device is not None and (
                    device.get('type') != 'unix-block' or
                    device.get('path') != mountpoint or
                    os.path.realpath(device.get('source', '')) != device_path):
                raise exception.InvalidVolume(
                    reason='Refusing to roll back a Cinder attach whose Incus '
                           'device identity changed')
            profile.devices.pop(volume_id, None)
            for metadata_key in _volume_device_info_keys(volume_id):
                profile.config.pop(metadata_key, None)
            profile.save(wait=True)

        connector.disconnect_volume(effective_data, device_info)
        # Persist the host-side commit point before Cinder can release the
        # attachment. A replay of this phase performs external bookkeeping
        # only and therefore cannot disconnect a later owner on this host.
        _write_volume_journal(
            instance, volume_id, effective, device_info, mountpoint,
            phase='rolled-back')

    def finalize_rolled_back_volume_journal(self, instance, volume_id):
        """Clear rollback evidence after Cinder and Nova records are gone."""
        record = _read_volume_journal(instance, volume_id)
        if record is None:
            return
        if record.get('phase') != 'rolled-back':
            raise exception.InvalidVolume(
                reason='Cinder volume %s has a non-rollback journal' %
                       volume_id)
        try:
            profile = self.client.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
        else:
            if _profile_has_volume_connection(profile, volume_id):
                raise exception.InvalidVolume(
                    reason='Incus still references rolled-back Cinder volume '
                           '%s' % volume_id)
        _remove_volume_journal(instance, volume_id)

    def list_volume_journal_recovery_candidates(self):
        """Return unfinished volume journals for manager-side validation.

        The journal is durable before both connect and disconnect, so a
        process that dies inside either leaves one behind. Nova's compute
        manager can also die before this driver is entered at all, in which
        case there is no journal and only the mapping reconciler can see the
        divergence; these two mechanisms answer different questions and are
        both needed.
        """
        candidates = []
        journal_root = os.path.join(
            CONF.instances_path, 'incus-volume-journal')
        try:
            instance_uuids = sorted(os.listdir(journal_root))
        except FileNotFoundError:
            return candidates
        for instance_uuid in instance_uuids:
            if not uuidutils.is_uuid_like(instance_uuid):
                continue
            records = _volume_journal_records_by_uuid(instance_uuid)
            attach_intents = _managed_attach_intents_by_uuid(instance_uuid)
            detach_intents = _managed_detach_intents_by_uuid(instance_uuid)
            rotations = _cold_attachment_rotations_by_uuid(instance_uuid)
            volume_ids = (
                set(records) | set(attach_intents) | set(detach_intents) |
                set(rotations))
            if not volume_ids:
                continue

            candidates.append({
                'uuid': instance_uuid,
                'volume_ids': sorted(volume_ids),
                'phases': {
                    volume_id: _volume_recovery_phase(
                        records.get(volume_id),
                        attach_intents.get(volume_id),
                        detach_intents.get(volume_id),
                        rotations.get(volume_id))
                    for volume_id in volume_ids
                },
            })
        return candidates

    def recover_share_journal_candidate(self, instance, candidate):
        """Clean one terminal migration journal after rechecking runtime."""
        expected = {
            'uuid': instance.uuid,
            'name': instance.name,
        }
        if any(candidate.get(key) != value for key, value in expected.items()):
            raise exception.MigrationError(
                reason='Manila journal recovery owner does not match Nova')
        operation_token = candidate.get('operation_token')
        if not uuidutils.is_uuid_like(operation_token):
            raise exception.MigrationError(
                reason='Manila journal recovery token is not a migration UUID')
        records = _share_journal_records(
            instance, operation_token=operation_token)
        if tuple(record['share_id'] for record in records) != tuple(
                candidate.get('share_ids', ())):
            raise exception.MigrationError(
                reason='Manila journal set changed during recovery')

        for collection, resource_type in (
                (self.client.instances, 'instance'),
                (self.client.profiles, 'profile')):
            try:
                collection.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if not _is_incus_not_found(exc):
                    raise
            else:
                raise exception.MigrationError(
                    reason='Refusing Manila journal cleanup while a local '
                           'Incus {} exists'.format(resource_type))

        _cleanup_share_journal_mounts(
            instance, operation_token=operation_token)
        return True

    def recover_cleanup_profile(
            self, context, instance, network_info):
        """Replay one durable source/destination cleanup transaction."""
        with lockutils.lock(_profile_lock_name(instance)):
            profile = self.client.profiles.get(instance.name)
            config = (
                profile.config if isinstance(profile.config, dict) else {})
            if (config.get(CLEANUP_RECOVERY_KEY) != 'true' or
                    config.get('environment.product_name') !=
                    'OpenStack Nova' or
                    config.get('user.openstack.uuid') != instance.uuid):
                raise exception.InvalidConfiguration(
                    'Incus cleanup recovery profile is not owned by the '
                    'requested Nova instance')
            cleanup_token = config.get(MIGRATION_CLEANUP_TOKEN_KEY)
            destination_cleanup = (
                not config.get(MIGRATION_DESTINATION_KEY) and
                uuidutils.is_uuid_like(cleanup_token))
            if destination_cleanup:
                try:
                    idmap_base = int(config.get('security.idmap.base'))
                    idmap_size = int(config.get('security.idmap.size'))
                except (TypeError, ValueError) as exc:
                    raise exception.MigrationError(
                        reason='Cleanup-marked migration destination has no '
                               'fixed idmap proof') from exc
                attempt = _get_migration_attempt(
                    self.client, instance, cleanup_token,
                    idmap_base, idmap_size)
                if (
                    attempt['state'] not in ('aborted', 'failed') or
                    not attempt.get('finished')
                ):
                    raise exception.MigrationError(
                        reason='Refusing destination cleanup before its '
                               'migration attempt is terminal and '
                               'non-committed')

        try:
            self.client.instances.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
        else:
            # A cleanup profile is not authority to delete an instance. Nova's
            # deleted-instance reconciliation handles stopped records using
            # the migration handover checks in
            # cleanup_lingering_instance_resources(). This periodic path only
            # replays host resources after positive target absence.
            raise exception.MigrationError(
                reason='Refusing cleanup recovery while an Incus instance '
                       'record still exists')

        self._cleanup(
            context, instance, network_info, block_device_info=None,
            destroy_vifs=True, delete_profile=not destination_cleanup)
        if destination_cleanup:
            self._acknowledge_cleanup_profile(
                instance, cleanup_token)
        return True

    @_invalidates_instance_inventory
    def reboot(self, context, instance, network_info, reboot_type,
               block_device_info=None, bad_volumes_callback=None,
               accel_info=None, share_info=None):
        """Reboot the container.

        Nova *should* not execute this on a stopped container, but
        the documentation specifically says that if it is called, the
        container should always return to a 'Running' state.

        See `nova.virt.driver.ComputeDriver.cleanup` for more
        information.
        """
        container = self._reconcile_reboot_data_volumes(
            context, instance, block_device_info)
        if container.status == 'Stopped':
            self._validate_reboot_vifs(instance, network_info)
            self.plug_vifs(instance, network_info)

            def start_stopped():
                current = self.client.instances.get(instance.name)
                if current.status != 'Running':
                    self._start_instance_with_idmap(instance, current)

            _retry_incus_instance_action(
                start_stopped,
                'start stopped instance during reboot',
                instance, retry_transient=True)
        else:
            def restart(force):
                # Re-read on every busy retry. A concurrent stop or a lost
                # start response can otherwise leave Nova operating on a stale
                # SDK model and fail even though the requested running state
                # has already converged.
                current = self.client.instances.get(instance.name)
                with lockutils.lock(
                        _idmap_host_claim_lock_name(instance.uuid),
                        external=True,
                        lock_path=_idmap_host_claim_lock_path()):
                    self._ensure_instance_idmap_before_start(
                        instance, current, _claim_lock_held=True)
                    if current.status == 'Stopped':
                        current.start(wait=True)
                    else:
                        current.restart(force=force, wait=True)

            if reboot_type == 'SOFT':
                try:
                    _retry_incus_instance_action(
                        lambda: restart(force=False),
                        'soft reboot',
                        instance)
                except incus_exceptions.LXDAPIException as exc:
                    if (
                        _is_incus_busy_operation(exc) or
                        _incus_api_status_code(exc) != 400
                    ):
                        raise
                    LOG.warning(
                        'Incus soft reboot failed; falling back to hard '
                        'reboot',
                        instance=instance,
                        exc_info=True)
                    _retry_incus_instance_action(
                        lambda: restart(force=True),
                        'hard reboot fallback',
                        instance)
            else:
                _retry_incus_instance_action(
                    lambda: restart(force=True),
                    'hard reboot',
                    instance)
        self._reassert_vifs(instance, network_info)
        self._clear_migration_recovery_marker(instance)

    def needs_migration_recovery(self, instance):
        """Return whether this host owns a marked, stopped migration target."""
        try:
            container = self.client.instances.get(instance.name)
            profile = self.client.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if _is_incus_not_found(exc):
                return False
            raise

        checks = {
            'known_state': container.status in ('Running', 'Stopped'),
            'uuid_match': (
                _instance_nova_uuid(container) == instance.uuid),
            'marked': profile.config.get(MIGRATION_RECOVERY_KEY) in (
                'true', 'running', 'stopped'),
        }
        LOG.debug(
            'Evaluated Incus migration recovery candidate: %(checks)s',
            {'checks': checks}, instance=instance)
        return all(checks.values())

    def _mark_migration_recovery_required(
            self, instance, power_on=True):
        # Attach/detach paths fetch and save their own profile objects. Always
        # re-read here so a stale flavor.to_profile object cannot overwrite
        # the only durable os-brick cleanup record.
        profile = self.client.profiles.get(instance.name)
        profile.config[MIGRATION_RECOVERY_KEY] = (
            'running' if power_on else 'stopped')
        _save_profile_marker(profile)
        LOG.warning(
            'Marked claimed migration target for automatic recovery',
            instance=instance)

    def _mark_cleanup_recovery_required(self, instance):
        """Journal source host cleanup before deleting its instance record."""
        with lockutils.lock(_profile_lock_name(instance)):
            profile = self.client.profiles.get(instance.name)
            config = (
                profile.config if isinstance(profile.config, dict) else {})
            if (config.get('environment.product_name') != 'OpenStack Nova' or
                    config.get('user.openstack.uuid') != instance.uuid):
                raise exception.InvalidConfiguration(
                    'Refusing to journal cleanup on an Incus profile not '
                    'owned by the Nova instance')
            profile.config[CLEANUP_RECOVERY_KEY] = 'true'
            _save_profile_marker(profile)

    @_invalidates_instance_inventory
    def recover_migration_target(
            self, context, instance, network_info, block_device_info=None):
        """Repair a marked migration owner without changing intended state."""
        profile = self.client.profiles.get(instance.name)
        desired_state = profile.config.get(MIGRATION_RECOVERY_KEY)
        if desired_state not in ('true', 'running', 'stopped'):
            raise exception.InvalidConfiguration(
                'Incus BFV recovery marker is missing or invalid')

        _retry_migration_finish_action(
            lambda: _set_storage_handover_state(
                self.client, instance.name, 'owned'),
            'migration recovery shared-storage ownership', instance)
        self._reconcile_reboot_data_volumes(
            context, instance, block_device_info)
        cleanup_token = profile.config.get(MIGRATION_CLEANUP_TOKEN_KEY)
        if uuidutils.is_uuid_like(cleanup_token):
            journals = _share_journal_records(
                instance, operation_token=cleanup_token)
            if journals:
                with lockutils.lock(_profile_lock_name(instance)):
                    profile = self.client.profiles.get(instance.name)
                    self._attach_journaled_share_devices(
                        profile, instance, cleanup_token,
                        [record['share_id'] for record in journals])
        container = self.client.instances.get(instance.name)
        self._validate_reboot_vifs(instance, network_info)
        should_run = desired_state in ('true', 'running')
        was_running = container.status == 'Running'
        if network_info and was_running:
            container.stop(wait=True)
        self._refresh_vifs(instance, network_info)
        if should_run and (network_info or not was_running):
            self._start_instance_with_idmap(instance, container)
        elif not should_run and was_running and not network_info:
            container.stop(wait=True)
        profile = self.client.profiles.get(instance.name)
        cleanup_token = (
            profile.config.get(MIGRATION_CLEANUP_TOKEN_KEY)
            if isinstance(profile.config, dict) else None)
        if uuidutils.is_uuid_like(cleanup_token):
            idmap_base, idmap_size = _instance_migration_idmap(
                container, profile)
            try:
                attempt = _get_migration_attempt(
                    self.client, instance, cleanup_token,
                    idmap_base, idmap_size)
            except incus_exceptions.LXDAPIException as exc:
                if not _is_incus_not_found(exc):
                    raise
            else:
                if attempt['state'] != 'committed':
                    raise exception.MigrationError(
                        reason='Marked migration owner has a non-committed '
                               'local attempt')
                migration_uuid = profile.config.get(MIGRATION_NOVA_UUID_KEY)
                if not _publish_migration_target_volumes_complete(
                        self.client, instance, cleanup_token,
                        migration_uuid):
                    raise exception.MigrationError(
                        reason='Marked migration owner retains a local '
                               'Cinder volume transaction')
                # A cold migration in VERIFY_RESIZE still needs this exact
                # attempt for the source-side confirm/revert decision.  The
                # recovery loop repairs only the destination runtime; it
                # must not accept the resize on Nova's behalf.
                if instance.vm_state != vm_states.RESIZED:
                    _finalize_committed_migration_attempt(
                        self.client, instance, cleanup_token,
                        idmap_base, idmap_size)
        self._clear_migration_recovery_marker(instance)
        return should_run

    def _ensure_instance_idmap_before_start(
            self, instance, container, _claim_lock_held=False):
        """Fail closed unless this instance still owns its observed idmap."""
        if not _claim_lock_held:
            with lockutils.lock(
                    _idmap_host_claim_lock_name(instance.uuid), external=True,
                    lock_path=_idmap_host_claim_lock_path()):
                return self._ensure_instance_idmap_before_start(
                    instance, container, _claim_lock_held=True)

        assignment, claim = self._instance_local_idmap_claim(
            instance, container)
        if self.idmap_allocator is None:
            return assignment

        host_id = virt_node.read_local_node_uuid()
        if not host_id:
            raise incus_idmap.IDMapConfigurationError(
                'Nova has no persistent compute-node UUID; refusing to '
                'start an Incus instance')
        if claim.host_id != host_id:
            raise incus_idmap.IDMapIntegrityError(
                'Incus instance local host binding does not match this '
                'compute')
        if claim.state == 'possible':
            assignment, claim = (
                self._promote_idmap_claim_if_server_committed(
                    instance, claim, _claim_lock_held=True))
        return self.idmap_allocator.assert_startable(
            instance.uuid, host_id, claim.materialization_id,
            assignment=assignment)

    def _start_instance_with_idmap(self, instance, container):
        """Start only after validating the observed fixed idmap generation."""
        with lockutils.lock(
                _idmap_host_claim_lock_name(instance.uuid), external=True,
                lock_path=_idmap_host_claim_lock_path()):
            self._ensure_instance_idmap_before_start(
                instance, container, _claim_lock_held=True)
            container.start(wait=True)

    def _refresh_vifs(self, instance, network_info):
        """Recreate retained host VIFs so OVN reasserts their binding state."""
        if not network_info:
            return
        self.unplug_vifs(instance, network_info)
        _retry_migration_finish_action(
            lambda: self.plug_vifs(instance, network_info),
            'migration recovery VIF wiring', instance)

    def _clear_migration_recovery_marker(self, instance):
        try:
            profile = self.client.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if _is_incus_not_found(exc):
                return
            raise
        if (isinstance(profile.config, dict) and
                MIGRATION_RECOVERY_KEY in profile.config):
            profile.config.pop(MIGRATION_RECOVERY_KEY)
            profile.save(wait=True)

    def _reconcile_reboot_data_volumes(
            self, context, instance, block_device_info):
        """Make the durable Cinder topology exact before an instance starts."""
        data_volume_bdms = _reboot_data_volume_bdms(
            block_device_info,
            root_device_name=getattr(instance, 'root_device_name', None))
        self._preflight_data_volume_bdms(
            data_volume_bdms, validate_connectors=False)
        desired = {
            _bdm_volume_id(bdm): bdm for bdm in data_volume_bdms
        }

        def attach_desired_volume(bdm):
            connection_info = bdm['connection_info']
            mountpoint = bdm['mount_device']
            attachment_id = _bdm_attachment_id(bdm)
            self._attach_and_commit_internal_volume_operation(
                context, connection_info, instance, mountpoint,
                attachment_id, 'reconcile', attachment_id,
                'power-reconcile')

        container = self.client.instances.get(instance.name)
        profile = self.client.profiles.get(instance.name)
        journals = _volume_journal_records(instance)
        topology = _data_volume_topology(profile, journals)

        if block_device_info is None and topology['proven_ids']:
            raise exception.InvalidVolume(
                reason='Nova did not provide block-device mappings for an '
                       'instance that retains Cinder volume state')
        if desired or topology['proven_ids'] or topology['opaque_ids']:
            _validate_profile_volume_owner(profile, instance)
        if topology['opaque_ids']:
            raise exception.InvalidVolume(
                reason='Incus profile contains opaque unix-block devices: %s'
                       % ', '.join(sorted(topology['opaque_ids'])))

        extra = topology['proven_ids'] - set(desired)
        if container.status != 'Stopped':
            if (extra or topology['journal_ids'] or
                    topology['profile_ids'] != set(desired)):
                raise exception.InvalidVolume(
                    reason='Running Incus instance Cinder topology differs '
                           'from Nova block-device mappings')
        else:
            for volume_id in sorted(extra):
                self._disconnect_profile_volume_connection(
                    context, instance, volume_id)

            for volume_id, bdm in sorted(desired.items()):
                connection_info = bdm['connection_info']
                mountpoint = bdm['mount_device']
                profile = self.client.profiles.get(instance.name)
                journals = _volume_journal_records(instance)
                qos_limits = _data_volume_qos(
                    connection_info,
                    self.client.host_info.get('api_extensions', []))

                if volume_id in journals:
                    attach_desired_volume(bdm)
                    continue
                if not _profile_has_volume_connection(profile, volume_id):
                    attach_desired_volume(bdm)
                    continue
                try:
                    _profile_volume_attachment_matches(
                        profile, volume_id, mountpoint, qos_limits,
                        connection_info, rbd_mapping_cache={})
                except exception.InvalidVolume:
                    LOG.warning(
                        'Repairing an incomplete or stale Cinder attachment '
                        'before starting the retained Incus instance',
                        instance=instance, exc_info=True)
                    self._disconnect_profile_volume_connection(
                        context, instance, volume_id,
                        connection_info=connection_info,
                        mountpoint=mountpoint)
                    attach_desired_volume(bdm)

        profile = self.client.profiles.get(instance.name)
        journals = _volume_journal_records(instance)
        topology = _data_volume_topology(profile, journals)
        if (topology['opaque_ids'] or topology['journal_ids'] or
                topology['profile_ids'] != set(desired) or
                topology['unix_block_ids'] != set(desired)):
            raise exception.InvalidVolume(
                reason='Incus Cinder topology did not converge to the exact '
                       'Nova block-device mapping')

        rbd_mapping_cache = {}
        for volume_id, bdm in sorted(desired.items()):
            connection_info = bdm['connection_info']
            qos_limits = _data_volume_qos(
                connection_info,
                self.client.host_info.get('api_extensions', []))
            _profile_volume_attachment_matches(
                profile, volume_id, bdm['mount_device'], qos_limits,
                connection_info, rbd_mapping_cache=rbd_mapping_cache)

        root_bdm = _boot_from_volume(block_device_info)
        if root_bdm is not None and root_bdm.get('volume_size'):
            root_volume = _cinder_rbd_root(root_bdm)
            self._resize_bfv_root(
                container, root_volume[1],
                int(root_bdm['volume_size']) * units.Gi)
        return container

    @staticmethod
    def _resize_bfv_root(container, image_name, requested_size):
        """Grow a matching Cinder-owned root filesystem through Incus."""
        root = container.devices.get('root')
        if not root or root.get(
                'initial.ceph.rbd.image_name') != image_name:
            return False

        requested = '%dB' % requested_size
        if root.get('size') == requested:
            return True

        updated_root = dict(root)
        updated_root['size'] = requested
        container.devices['root'] = updated_root
        container.save(wait=True)
        return True

    def _validate_reboot_vifs(self, instance, network_info):
        """Refuse to start a retained target with stale NIC ownership."""
        if not network_info:
            return
        container = self.client.instances.get(instance.name)
        profile = self.client.profiles.get(instance.name)
        for vif in network_info:
            device_name = incus_vif.get_vif_devname(vif)
            device = container.devices.get(
                device_name, profile.devices.get(device_name))
            expected = {
                'type': 'nic',
                'nictype': 'physical',
                'parent': incus_vif.get_vif_internal_devname(vif),
                'hwaddr': str(vif['address']),
                'name': incus_vif.get_vif_guest_devname(vif),
            }
            if device is None or any(
                    device.get(key) != value
                    for key, value in expected.items()):
                raise exception.InterfaceAttachFailed(
                    instance_uuid=instance.uuid)

    def manage_image_cache(self, context, all_instances):
        """Age out unused Glance images synced into the local Incus store.

        _sync_glance_image_to_incus imports every booted Glance image into
        this node's Incus image store under an alias equal to the Glance
        UUID, and nothing ever removed them: a long-lived node accumulated
        every historical image until the store filled, the reported DISK_GB
        shrank and scheduling starved.

        Removal is deliberately narrow, per node and fail-safe:

        - only images whose aliases are exclusively canonical UUIDs are
          candidates (operator-published images carry named aliases);
        - an image referenced by any instance on this host is kept;
        - a candidate must have been unused for at least
          [image_cache]remove_unused_original_minimum_age_seconds;
        - the per-node ceph.rbd.image_prefix keeps cached image volumes
          node-scoped, so deletion cannot affect another compute, and the
          server-side image-use lock keeps a concurrent clone safe.
        """
        referenced = {
            getattr(inst, 'image_ref', None) for inst in all_instances}
        referenced.discard(None)
        referenced.discard('')
        min_age = CONF.image_cache.remove_unused_original_minimum_age_seconds
        now = timeutils.utcnow(with_timezone=True)
        try:
            # One recursive listing rather than pylxd's plain index, whose
            # objects carry only a fingerprint and fetch every attribute
            # below on first access - a request per image, on a periodic
            # task, for a store that is mostly not candidates.
            response = self.client.api.images.get(params={'recursion': 1})
            body = response.json()
            images = body.get('metadata') if isinstance(body, dict) else None
            if not isinstance(images, list):
                raise exception.InvalidConfiguration(
                    'Incus recursive image inventory is malformed')
        except Exception:
            LOG.warning(
                'Cannot list the Incus image store for cache aging',
                exc_info=True)
            return

        candidates = []
        for image in images:
            if not isinstance(image, dict):
                continue
            aliases = [
                alias.get('name', '')
                for alias in (image.get('aliases') or [])
                if isinstance(alias, dict)]
            if not aliases or not all(
                    uuidutils.is_uuid_like(alias) for alias in aliases):
                continue
            if any(alias in referenced for alias in aliases):
                continue
            last_used = None
            for stamp in (image.get('last_used_at'),
                          image.get('uploaded_at')):
                if not stamp:
                    continue
                try:
                    parsed = timeutils.parse_isotime(stamp)
                except ValueError:
                    continue
                # Incus reports never-used as the zero time.
                if parsed.year <= 1970:
                    continue
                if last_used is None or parsed > last_used:
                    last_used = parsed
            if last_used is None:
                continue
            if (now - last_used).total_seconds() < min_age:
                continue
            fingerprint = image.get('fingerprint')
            if fingerprint:
                candidates.append((fingerprint, aliases, last_used))

        # Deletions are serial and each waits on the server, so the first
        # pass on a node that has been running for months would otherwise
        # hold this periodic task for minutes. The remainder is not
        # dropped, it is taken by the following passes - and it is logged,
        # because a bounded pass that looked complete would misreport the
        # store as fully aged.
        batch = candidates[:_IMAGE_CACHE_DELETE_BATCH]
        if len(candidates) > len(batch):
            LOG.info(
                'Aging %(batch)d of %(total)d unused cached Incus images '
                'this pass; the rest follow on later passes',
                {'batch': len(batch), 'total': len(candidates)})
        for fingerprint, aliases, last_used in batch:
            try:
                LOG.info(
                    'Removing unused cached Incus image %(fingerprint).12s '
                    'aliased %(aliases)s (unused since %(last_used)s)',
                    {'fingerprint': fingerprint,
                     'aliases': aliases, 'last_used': last_used})
                self.client.images.get(fingerprint).delete(wait=True)
            except Exception:
                LOG.warning(
                    'Failed to age cached Incus image; leaving it in place',
                    exc_info=True)

    def get_console_output(self, context, instance):
        """Get the output of the container console.

        See `nova.virt.driver.ComputeDriver.get_console_output` for more
        information.
        """
        # Read the tail of the host-side log file so a tenant hammering a
        # huge console log cannot balloon nova-compute memory; the API
        # fetch below has no ranged read and loads the whole log.
        console_path = common.InstanceAttributes(instance).console_path
        try:
            with open(console_path, 'rb') as console_file:
                data, unused_remaining = _last_bytes(
                    console_file, MAX_CONSOLE_BYTES)
                return data
        except FileNotFoundError:
            # A guest that has not written to its console yet. The API
            # below returns the same emptiness, so this is not worth a
            # log line on every request.
            pass
        except OSError as exc:
            # Anything else - most often the log directory being
            # unreadable by the compute service user - is a standing
            # condition that silently defeats the bounded read on every
            # request. Swallowing it left no trace of why memory use
            # tracked console size.
            LOG.warning(
                'Cannot read the host-side Incus console log %(path)s '
                '(%(error)s); falling back to the unbounded API read, '
                'which loads the whole log into memory',
                {'path': console_path, 'error': exc}, instance=instance)
        container = self.client.instances.get(instance.name)
        return container.console_log()[-MAX_CONSOLE_BYTES:]

    def get_serial_console(self, context, instance):
        """Return a token-protected Nova serialproxy backend."""
        if not CONF.serial_console.enabled:
            raise exception.ConsoleTypeUnavailable(console_type='serial')
        with self._serial_consoles_lock:
            if instance.uuid in self._serial_console_destroying:
                raise exception.InstanceNotRunning(instance_id=instance.uuid)
            container = self.client.instances.get(instance.name)
            if container.status != 'Running':
                raise exception.InstanceNotRunning(instance_id=instance.uuid)
            broker = self._serial_consoles.get(instance.uuid)
            if broker is None:
                broker = incus_console.SerialConsoleBroker(
                    CONF.serial_console.proxyclient_address, container)
                self._serial_consoles[instance.uuid] = broker
        return console_type.ConsoleSerial(
            host=CONF.serial_console.proxyclient_address,
            port=broker.port)

    def get_host_ip_addr(self):
        return CONF.my_ip

    def _retain_volume_cleanup_metadata(
            self, instance, volume_id, connection_info, device_info,
            mountpoint):
        """Durably retain os-brick data after an uncertain disconnect."""
        _write_volume_journal(
            instance, volume_id, connection_info, device_info or {},
            mountpoint, phase='disconnecting')
        try:
            with lockutils.lock(_profile_lock_name(instance)):
                profile = self.client.profiles.get(instance.name)
                _validate_profile_volume_owner(profile, instance)
                profile.devices.pop(volume_id, None)
                profile.config[_volume_device_info_key(volume_id)] = (
                    _serialize_volume_attachment(
                        connection_info, device_info or {}, mountpoint,
                        phase='disconnecting'))
                profile.save(wait=True)
        except Exception:
            LOG.critical(
                'Failed to retain cleanup metadata for Cinder volume %s '
                'in Incus after host disconnect failed; the host-local '
                'journal remains authoritative',
                volume_id, instance=instance, exc_info=True)

    @_invalidates_instance_inventory
    def attach_volume(self, context, connection_info, instance, mountpoint,
                      disk_bus=None, device_type=None, encryption=None):
        # The Nova hot-attach path completes its BDM and Cinder attachment
        # only after this method returns.  Keep the connected journal across
        # that gap; IncusComputeManager removes it after attachment_complete.
        return self._attach_volume(
            context, connection_info, instance, mountpoint,
            disk_bus=disk_bus, device_type=device_type,
            encryption=encryption, retain_journal=True)

    def _attach_volume(
            self, context, connection_info, instance, mountpoint,
            disk_bus=None, device_type=None, encryption=None,
            retain_journal=False):
        """Attach a volume without extending Nova's public driver contract."""
        volume_id = _volume_id(connection_info)
        with lockutils.lock(
                _volume_topology_lock_name(instance), external=True,
                lock_path=_volume_topology_lock_path()):
            with lockutils.lock(
                    _volume_operation_lock_name(volume_id), external=True,
                    lock_path=_volume_operation_lock_path()):
                return self._attach_volume_locked(
                    context, connection_info, instance, mountpoint,
                    disk_bus=disk_bus, device_type=device_type,
                    encryption=encryption, retain_journal=retain_journal)

    def _attach_volume_for_operation(
            self, context, connection_info, instance, mountpoint,
            attachment_id, operation_kind, operation_token,
            operation_direction, operation_migration_uuid=None,
            encryption=None, allow_missing_instance=False,
            expected_migration_token=None, require_missing_instance=False,
            commit_immediately=False):
        """Attach under a durable non-hot-attach operation generation."""
        volume_id = _volume_id(connection_info)
        if expected_migration_token is not None:
            with lockutils.lock(_profile_lock_name(instance)):
                profile = self.client.profiles.get(instance.name)
                _validate_profile_volume_owner(profile, instance)
                config = (
                    profile.config if isinstance(profile.config, dict) else {})
                if (config.get(MIGRATION_CLEANUP_TOKEN_KEY) !=
                        expected_migration_token or profile.used_by):
                    raise exception.MigrationError(
                        reason='Incus migration volume staging profile '
                               'changed '
                               'before durable attachment intent creation')
        with lockutils.lock(
                _volume_manager_transaction_lock_name(
                    instance.uuid, volume_id),
                external=True, lock_path=_volume_operation_lock_path()):
            intent = self.prepare_managed_volume_attach(
                instance, volume_id, attachment_id, mountpoint,
                operation_kind=operation_kind,
                operation_token=operation_token,
                operation_direction=operation_direction,
                operation_migration_uuid=operation_migration_uuid)
            with lockutils.lock(
                    _volume_topology_lock_name(instance), external=True,
                    lock_path=_volume_topology_lock_path()):
                with lockutils.lock(
                        _volume_operation_lock_name(volume_id), external=True,
                        lock_path=_volume_operation_lock_path()):
                    self._attach_volume_locked(
                        context, connection_info, instance, mountpoint,
                        encryption=encryption,
                        allow_missing_instance=allow_missing_instance,
                        expected_migration_token=expected_migration_token,
                        require_missing_instance=require_missing_instance,
                        retain_journal=True)
                    if commit_immediately:
                        try:
                            self.confirm_connected_volume_journal(
                                instance, volume_id, connection_info,
                                expected_mountpoint=mountpoint)
                            self.cancel_managed_volume_attach(
                                instance, volume_id, intent)
                        except OSError:
                            # unlink() can succeed before its directory fsync
                            # fails. Re-publish the exact generation so the
                            # caller cannot mistake an absent file for durable
                            # retirement and clear the profile owner token.
                            # Failure to restore that fence is not harmless:
                            # fail while the container/profile still exist.
                            recovered = self.prepare_managed_volume_attach(
                                instance, volume_id, attachment_id, mountpoint,
                                operation_kind=operation_kind,
                                operation_token=operation_token,
                                operation_direction=operation_direction,
                                operation_migration_uuid=(
                                    operation_migration_uuid))
                            if recovered != intent:
                                raise exception.InvalidVolume(
                                    reason='Internal Cinder recovery intent '
                                           'changed after an fsync failure')
                            LOG.critical(
                                'Internal Cinder volume %s committed but its '
                                'local recovery evidence could not be retired',
                                volume_id, instance=instance, exc_info=True)
            # The caller's Nova operation commits after this driver call.
            # Keep the connected journal and exact intent through that gap;
            # either the caller or periodic recovery retires them once the
            # owning Nova/Cinder transaction is durably authoritative.
            return intent

    def _commit_internal_volume_attach_operation(
            self, instance, volume_id, connection_info, mountpoint, intent):
        with lockutils.lock(
                _volume_manager_transaction_lock_name(
                    instance.uuid, volume_id),
                external=True, lock_path=_volume_operation_lock_path()):
            current = self.get_managed_volume_attach_intent(
                instance, volume_id)
            if current != intent:
                raise exception.InvalidVolume(
                    reason='Internal Cinder recovery intent changed before '
                           'durable retirement')
            with lockutils.lock(
                    _volume_topology_lock_name(instance), external=True,
                    lock_path=_volume_topology_lock_path()):
                with lockutils.lock(
                        _volume_operation_lock_name(volume_id), external=True,
                        lock_path=_volume_operation_lock_path()):
                    try:
                        self.confirm_connected_volume_journal(
                            instance, volume_id, connection_info,
                            expected_mountpoint=mountpoint)
                        self.cancel_managed_volume_attach(
                            instance, volume_id, intent)
                    except OSError:
                        recovered = self.prepare_managed_volume_attach(
                            instance, volume_id, intent['attachment_id'],
                            mountpoint,
                            operation_kind=intent['operation_kind'],
                            operation_token=intent['operation_token'],
                            operation_direction=(
                                intent['operation_direction']),
                            operation_migration_uuid=(
                                intent.get('operation_migration_uuid')))
                        if recovered != intent:
                            raise exception.InvalidVolume(
                                reason='Internal Cinder recovery intent '
                                       'changed after an fsync failure')
                        LOG.critical(
                            'Internal Cinder volume %s is locally restored '
                            'but its recovery evidence could not be retired',
                            volume_id, instance=instance, exc_info=True)
                        return False
        return True

    def _attach_and_commit_internal_volume_operation(
            self, context, connection_info, instance, mountpoint,
            attachment_id, operation_kind, operation_token,
            operation_direction, operation_migration_uuid=None,
            encryption=None):
        intent = self._attach_volume_for_operation(
            context, connection_info, instance, mountpoint, attachment_id,
            operation_kind, operation_token, operation_direction,
            operation_migration_uuid=operation_migration_uuid,
            encryption=encryption, commit_immediately=True)
        return intent

    def finalize_spawn_volume_generation(self, instance, generation):
        """Clear the spawn owner only after all of its intents are retired."""
        intents = _managed_attach_intents_by_uuid(instance.uuid)
        if any(
                intent.get('operation_kind') == 'spawn' and
                intent.get('operation_token') == generation
                for intent in intents.values()):
            return False
        with lockutils.lock(_profile_lock_name(instance)):
            profile = self.client.profiles.get(instance.name)
            _validate_profile_volume_owner(profile, instance)
            current = profile.config.get(SPAWN_VOLUME_GENERATION_KEY)
            if current is None:
                return True
            if current != generation:
                raise exception.InvalidVolume(
                    reason='Incus spawn volume generation changed before '
                           'retirement')
            profile.config.pop(SPAWN_VOLUME_GENERATION_KEY)
            profile.save(wait=True)
        return True

    def finalize_source_volume_generation(
            self, instance, operation_token, require_rollback_complete=False):
        """Retire a source rollback token after all volume intents are gone."""
        intents = _managed_attach_intents_by_uuid(instance.uuid)
        rotations = _cold_attachment_rotations_by_uuid(instance.uuid)
        if any(
                intent.get('operation_kind') == 'migration' and
                intent.get('operation_token') == operation_token
                for intent in intents.values()):
            return False
        if any(
                rotation.get('operation_token') == operation_token
                for rotation in rotations.values()):
            return False
        with lockutils.lock(_profile_lock_name(instance)):
            profile = self.client.profiles.get(instance.name)
            _validate_profile_volume_owner(profile, instance)
            config = profile.config if isinstance(profile.config, dict) else {}
            current = config.get(MIGRATION_CLEANUP_TOKEN_KEY)
            if current is None:
                if (config.get(MIGRATION_ROLLBACK_COMPLETE_KEY) is None and
                        config.get(MIGRATION_NOVA_UUID_KEY) is None):
                    return True
                raise exception.MigrationError(
                    reason='Incus source rollback marker has no owner token')
            if current != operation_token:
                raise exception.MigrationError(
                    reason='Incus source volume generation owner changed')
            if (require_rollback_complete and
                    config.get(MIGRATION_ROLLBACK_COMPLETE_KEY) !=
                    operation_token):
                raise exception.MigrationError(
                    reason='Incus source rollback is not durably complete')
            # Re-read while holding the profile lock. Intent writers also
            # require this profile generation to remain unchanged, so a new
            # same-token transaction cannot be valid after token retirement.
            intents = _managed_attach_intents_by_uuid(instance.uuid)
            rotations = _cold_attachment_rotations_by_uuid(instance.uuid)
            if any(
                    intent.get('operation_kind') == 'migration' and
                    intent.get('operation_token') == operation_token
                    for intent in intents.values()):
                return False
            if any(
                    rotation.get('operation_token') == operation_token
                    for rotation in rotations.values()):
                return False
            config.pop(MIGRATION_CLEANUP_TOKEN_KEY, None)
            config.pop(MIGRATION_ROLLBACK_COMPLETE_KEY, None)
            config.pop(MIGRATION_NOVA_UUID_KEY, None)
            config.pop(MIGRATION_DESTINATION_KEY, None)
            config.pop(MIGRATION_OPERATION_KEY, None)
            profile.config = config
            profile.save(wait=True)
        return True

    def mark_source_volume_generation_rollback_complete(
            self, instance, operation_token, migration_uuid):
        """Persist the exact source owner before retiring volume evidence."""
        if (not uuidutils.is_uuid_like(operation_token) or
                not uuidutils.is_uuid_like(migration_uuid)):
            raise exception.MigrationError(
                reason='Incus source rollback generation is invalid')
        with lockutils.lock(_profile_lock_name(instance)):
            profile = self.client.profiles.get(instance.name)
            _validate_profile_volume_owner(profile, instance)
            config = profile.config if isinstance(profile.config, dict) else {}
            if config.get(MIGRATION_CLEANUP_TOKEN_KEY) != operation_token:
                raise exception.MigrationError(
                    reason='Incus source rollback generation owner changed')
            existing_token = config.get(MIGRATION_ROLLBACK_COMPLETE_KEY)
            existing_migration = config.get(MIGRATION_NOVA_UUID_KEY)
            if existing_token not in (None, operation_token):
                raise exception.MigrationError(
                    reason='Incus source rollback marker changed')
            if existing_migration not in (None, migration_uuid):
                raise exception.MigrationError(
                    reason='Incus source rollback Nova migration changed')
            config[MIGRATION_ROLLBACK_COMPLETE_KEY] = operation_token
            config[MIGRATION_NOVA_UUID_KEY] = migration_uuid
            profile.config = config
            profile.save(wait=True)
        return True

    def fence_failed_cold_source_volume_generation(
            self, instance, operation_token):
        """Prove the target non-committed before restoring source I/O."""
        with lockutils.lock(_profile_lock_name(instance)):
            profile = self.client.profiles.get(instance.name)
            _validate_profile_volume_owner(profile, instance)
            config = profile.config if isinstance(profile.config, dict) else {}
            if config.get(MIGRATION_CLEANUP_TOKEN_KEY) != operation_token:
                raise exception.MigrationError(
                    reason='Failed cold source generation owner changed')
            destination_address = config.get(MIGRATION_DESTINATION_KEY)
            operation_id = config.get(MIGRATION_OPERATION_KEY)
        if not destination_address:
            raise exception.MigrationError(
                reason='Failed cold source generation has no destination')
        container = self.client.instances.get(instance.name)
        idmap_base, idmap_size = _instance_migration_idmap(
            container, profile)
        remote = _migration_client(destination_address)
        attempt_absent = False
        try:
            attempt = _get_migration_attempt(
                remote, instance, operation_token, idmap_base, idmap_size)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
            attempt_absent = True
            attempt = None
            for collection in (remote.instances, remote.profiles):
                try:
                    collection.get(instance.name)
                except incus_exceptions.LXDAPIException as resource_exc:
                    if not _is_incus_not_found(resource_exc):
                        raise
                else:
                    raise exception.MigrationError(
                        reason='Failed cold target resources exist without '
                               'their exact migration attempt')
        if not attempt_absent:
            if attempt['state'] == 'active':
                attempt = _abort_migration_attempt(
                    remote, instance, operation_token, idmap_base, idmap_size,
                    target_cleanup=lambda: _retry_migration_finish_action(
                        lambda: self._delete_migration_target_with_idmap(
                            remote, instance),
                        'failed cold migration target deletion', instance))
            elif attempt['state'] in ('aborted', 'failed'):
                attempt = _wait_migration_attempt_finished(
                    remote, instance, operation_token, idmap_base, idmap_size,
                    ('aborted', 'failed'))
            if attempt['state'] == 'committed':
                raise exception.MigrationError(
                    reason='Failed cold source restore lost ownership to a '
                           'committed target')
            if attempt['state'] not in ('aborted', 'failed'):
                raise exception.MigrationError(
                    reason='Failed cold source target is not durably fenced')
        _settle_instance_migration_operations(
            self.client, instance, operation_ids=(operation_id,))
        if not attempt_absent:
            _retire_migration_attempt(
                remote, instance, operation_token, idmap_base, idmap_size)
        return True

    def restore_failed_cold_source_storage_ownership(
            self, instance, operation_token):
        """Fence the target and restore the exact source root ownership."""
        self.fence_failed_cold_source_volume_generation(
            instance, operation_token)
        with lockutils.lock(_profile_lock_name(instance)):
            profile = self.client.profiles.get(instance.name)
            _validate_profile_volume_owner(profile, instance)
            config = profile.config if isinstance(profile.config, dict) else {}
            if (config.get(MIGRATION_CLEANUP_TOKEN_KEY) != operation_token or
                    config.get(MIGRATION_ROLLBACK_COMPLETE_KEY) !=
                    operation_token):
                raise exception.MigrationError(
                    reason='Failed cold source storage owner changed')
        return _retry_migration_finish_action(
            lambda: _restore_source_storage_ownership(self.client, instance),
            'failed cold source storage ownership restore', instance)

    def finalize_failed_cold_source_volume_generation(
            self, instance, operation_token):
        """Fence a failed cold target before retiring source volume state."""
        intents = _managed_attach_intents_by_uuid(instance.uuid)
        rotations = _cold_attachment_rotations_by_uuid(instance.uuid)
        if any(
                intent.get('operation_kind') == 'migration' and
                intent.get('operation_token') == operation_token
                for intent in intents.values()):
            return False
        if any(
                rotation.get('operation_token') == operation_token
                for rotation in rotations.values()):
            return False
        self.fence_failed_cold_source_volume_generation(
            instance, operation_token)
        return self.finalize_source_volume_generation(
            instance, operation_token, require_rollback_complete=True)

    def finalize_remote_source_volume_generation(
            self, instance, operation_token):
        """Retire a reverted source only after remote cleanup is settled."""
        intents = _managed_attach_intents_by_uuid(instance.uuid)
        rotations = _cold_attachment_rotations_by_uuid(instance.uuid)
        if any(
                intent.get('operation_kind') == 'migration' and
                intent.get('operation_token') == operation_token
                for intent in intents.values()):
            return False
        if any(
                rotation.get('operation_token') == operation_token
                for rotation in rotations.values()):
            return False
        with lockutils.lock(_profile_lock_name(instance)):
            profile = self.client.profiles.get(instance.name)
            _validate_profile_volume_owner(profile, instance)
            config = profile.config if isinstance(profile.config, dict) else {}
            if (config.get(MIGRATION_CLEANUP_TOKEN_KEY) != operation_token or
                    config.get(MIGRATION_ROLLBACK_COMPLETE_KEY) !=
                    operation_token or
                    not uuidutils.is_uuid_like(
                        config.get(MIGRATION_NOVA_UUID_KEY))):
                raise exception.MigrationError(
                    reason='Reverted source generation owner changed')
            destination_address = config.get(MIGRATION_DESTINATION_KEY)
            if not destination_address:
                raise exception.MigrationError(
                    reason='Reverted source generation has no destination')
            idmap_base, idmap_size = _instance_migration_idmap(None, profile)

        remote = _migration_client(destination_address)
        try:
            remote.instances.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
        else:
            raise exception.MigrationError(
                reason='Reverted source generation still has an Incus '
                       'instance on the migration destination')
        try:
            acknowledgement = remote.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
        else:
            self._validate_remote_cleanup_acknowledgement(
                acknowledgement, instance, operation_token,
                idmap_base, idmap_size)
            acknowledgement.delete()
        _retire_migration_attempt(
            remote, instance, operation_token, idmap_base, idmap_size)
        return self.finalize_source_volume_generation(
            instance, operation_token, require_rollback_complete=True)

    def finalize_pre_live_migration_rollback(
            self, instance, migrate_data):
        """Retire source preparation after destination pre-live aborts."""
        cleanup_token = _live_migration_cleanup_token(migrate_data)
        migration_uuid = _live_migration_uuid(migrate_data)
        source_operation_id = (
            migrate_data.source_operation_id
            if migrate_data.obj_attr_is_set('source_operation_id') else None)
        destination_operation_id = (
            migrate_data.destination_operation_id
            if migrate_data.obj_attr_is_set(
                'destination_operation_id') else None)
        if (source_operation_id is not None or
                destination_operation_id is not None):
            raise exception.MigrationError(
                reason='Pre-live rollback contains a started Incus '
                       'migration operation')
        self.mark_source_volume_generation_rollback_complete(
            instance, cleanup_token, migration_uuid)
        return self.finalize_remote_source_volume_generation(
            instance, cleanup_token)

    def _stage_volume_for_live_migration(
            self, context, connection_info, instance, mountpoint,
            attachment_id, cleanup_token, migration_uuid):
        """Connect a target volume before the migrated instance exists."""
        return self._attach_volume_for_operation(
            context, connection_info, instance, mountpoint,
            attachment_id, 'migration', cleanup_token, 'live-target',
            operation_migration_uuid=migration_uuid,
            allow_missing_instance=True,
            expected_migration_token=cleanup_token,
            require_missing_instance=True)

    def _attach_volume_locked(
            self, context, connection_info, instance, mountpoint,
            disk_bus=None, device_type=None, encryption=None,
            allow_missing_instance=False,
            expected_migration_token=None, retain_journal=False,
            require_missing_instance=False):
        """Attach block device to a nova instance.

        Attaching a block device to a container requires a couple of steps.
        First os_brick connects the cinder volume to the host. Next,
        the block device is added to the containers profile. Next, the
        apparmor profile for the container is updated to allow mounting
        'ext4' block devices. Finally, the profile is saved.

        The block device must be formatted as ext4 in order to mount
        the block device inside the container.

        See `nova.virt.driver.ComputeDriver.attach_volume' for
        more information/
        """
        _validate_volume_mountpoint(mountpoint)
        _validate_volume_access_mode(connection_info)
        qos_limits = _data_volume_qos(
            connection_info,
            self.client.host_info.get('api_extensions', []))

        if (_has_encryption_marker(encryption) or
                _has_encryption_marker(
                    (connection_info.get('data') or {}).get('encrypted'))):
            raise exception.VolumeEncryptionNotSupported(
                volume_type=connection_info['driver_volume_type'],
                volume_id=_volume_id(connection_info))

        volume_id = _volume_id(connection_info)
        _validate_recoverable_data_volume(connection_info, volume_id)

        try:
            container = self.client.instances.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if not allow_missing_instance or not _is_incus_not_found(exc):
                raise
            container = None
        if require_missing_instance and container is not None:
            raise exception.DestinationDiskExists(path=instance.name)
        if container is not None and container.status == 'Running':
            for binary in flavor.data_volume_fuse_binaries():
                result = container.execute(['which', binary])
                if result.exit_code != 0:
                    raise exception.InvalidVolume(
                        reason='Guest image must provide {} before attaching '
                               'Cinder data volumes'.format(binary))

        protocol = connection_info['driver_volume_type']
        metadata_key = _volume_device_info_key(volume_id)

        # Recover any prior attach/detach transaction before writing new
        # intent. In particular, never overwrite a connected journal carrying
        # the device_info required for safe os-brick cleanup.
        journal = _read_volume_journal(instance, volume_id)
        with lockutils.lock(_profile_lock_name(instance)):
            profile = self.client.profiles.get(instance.name)
            _validate_profile_volume_owner(profile, instance)
            if expected_migration_token is not None:
                config = (
                    profile.config
                    if isinstance(profile.config, dict) else {})
                if (config.get('environment.product_name') !=
                        'OpenStack Nova' or
                        config.get('user.openstack.uuid') != instance.uuid or
                        config.get(MIGRATION_CLEANUP_TOKEN_KEY) !=
                        expected_migration_token or profile.used_by):
                    raise exception.MigrationError(
                        reason='Incus live-migration volume staging profile '
                               'changed during attachment')
            if _profile_has_volume_connection(profile, volume_id):
                profile_record = _profile_volume_record(
                    profile, volume_id,
                    device=profile.devices.get(volume_id))
                profile_phase = _validate_volume_recovery_record(
                    profile_record, volume_id, mountpoint, connection_info)
                journal_phase = (
                    _validate_volume_recovery_record(
                        journal, volume_id, mountpoint, connection_info)
                    if journal is not None else None)
                if (profile_phase == 'connected' and
                        journal_phase != 'disconnecting' and
                        profile.devices.get(volume_id) is not None and
                        _profile_volume_attachment_matches(
                            profile, volume_id, mountpoint, qos_limits,
                            connection_info)):
                    if not retain_journal:
                        _remove_volume_journal(instance, volume_id)
                    LOG.debug(
                        'Cinder volume %(volume)s is already connected at '
                        '%(mountpoint)s; treating attach as idempotent',
                        {'volume': volume_id, 'mountpoint': mountpoint},
                        instance=instance)
                    return
                if (profile_phase == 'disconnecting' and
                        journal_phase not in (None, 'disconnecting')):
                    raise exception.InvalidVolume(
                        reason='Cinder volume %s has conflicting profile and '
                               'host recovery phases' % volume_id)
                recovery_record = journal or profile_record
                phase = journal_phase or profile_phase
            elif journal is not None:
                recovery_record = journal
                phase = _validate_volume_recovery_record(
                    recovery_record, volume_id, mountpoint, connection_info)
            else:
                recovery_record = None
                phase = None
            _validate_profile_volume_slot(
                profile, volume_id, mountpoint,
                replacing_volume_id=volume_id)

        if phase == 'disconnecting':
            # A prior detach removed guest access but did not reach its commit
            # point. Complete it before starting a fresh attachment.
            self._detach_volume_locked(
                context, connection_info, instance, mountpoint)
            journal = None
            recovery_record = None

        if journal is None:
            # Persist intent before os-brick mutates host state. A process
            # crash after connect is repaired by an idempotent connect retry.
            _write_volume_journal(
                instance, volume_id, connection_info, {}, mountpoint,
                phase='connecting')

        with lockutils.lock(_profile_lock_name(instance)):
            profile = self.client.profiles.get(instance.name)
            existing_device = profile.devices.get(volume_id)
            if existing_device is not None:
                if (existing_device.get('type') != 'unix-block' or
                        existing_device.get('path') != mountpoint):
                    raise exception.InvalidVolume(
                        reason='Existing Incus device for Cinder volume %s '
                               'does not match the recovery request' %
                               volume_id)
            else:
                _validate_profile_volume_slot(
                    profile, volume_id, mountpoint)
            profile.config[metadata_key] = _serialize_volume_attachment(
                connection_info, {}, mountpoint, phase='connecting')
            profile.config.pop(
                _legacy_volume_device_info_key(volume_id), None)
            profile.save(wait=True)
        storage_driver = brick_get_connector(protocol)
        try:
            device_info = storage_driver.connect_volume(
                connection_info['data'])
        except Exception:
            with excutils.save_and_reraise_exception():
                try:
                    self._detach_volume_locked(
                        context, connection_info, instance, mountpoint)
                except Exception:
                    LOG.critical(
                        'Retaining connecting journal for Cinder volume %s '
                        'after os-brick connect failed ambiguously',
                        volume_id, instance=instance, exc_info=True)
        try:
            device_path = os.path.realpath(device_info['path'])
        except (KeyError, TypeError):
            self._retain_volume_cleanup_metadata(
                instance, volume_id, connection_info, device_info,
                mountpoint)
            try:
                self._detach_volume_locked(
                    context, connection_info, instance, mountpoint)
            except Exception:
                LOG.exception(
                    'Failed to roll back a volume connection without a path',
                    instance=instance)
            raise exception.InvalidVolume(
                reason='os-brick did not return a block device path')
        try:
            _validate_block_device_path(
                device_path, 'os-brick connector path')
        except exception.InvalidVolume:
            self._retain_volume_cleanup_metadata(
                instance, volume_id, connection_info, device_info,
                mountpoint)
            try:
                self._detach_volume_locked(
                    context, connection_info, instance, mountpoint)
            except Exception:
                LOG.exception(
                    'Failed to roll back an invalid volume connection',
                    instance=instance)
            raise
        _write_volume_journal(
            instance, volume_id, connection_info, device_info, mountpoint,
            phase='connected')
        try:
            with lockutils.lock(_profile_lock_name(instance)):
                profile = self.client.profiles.get(instance.name)
                _validate_profile_volume_owner(profile, instance)
                if expected_migration_token is not None:
                    config = (
                        profile.config
                        if isinstance(profile.config, dict) else {})
                    if (config.get('environment.product_name') !=
                            'OpenStack Nova' or
                            config.get('user.openstack.uuid') !=
                            instance.uuid or
                            config.get(MIGRATION_CLEANUP_TOKEN_KEY) !=
                            expected_migration_token or profile.used_by):
                        raise exception.MigrationError(
                            reason='Incus live-migration volume staging '
                                   'profile changed after host connect')
                existing_device = profile.devices.get(volume_id)
                if (existing_device is not None and
                        (existing_device.get('type') != 'unix-block' or
                         existing_device.get('path') != mountpoint)):
                    raise exception.InvalidVolume(
                        reason='Existing Incus device for Cinder volume %s '
                               'does not match the final attach request' %
                               volume_id)
                _validate_profile_volume_slot(
                    profile, volume_id, mountpoint,
                    replacing_volume_id=volume_id)
                profile.devices[volume_id] = {
                    'path': mountpoint,
                    'required': 'true',
                    'source': device_path,
                    'type': 'unix-block',
                }
                profile.devices[volume_id].update(qos_limits)
                profile.config[metadata_key] = _serialize_volume_attachment(
                    connection_info, device_info, mountpoint,
                    phase='connected')
                profile.save(wait=True)
            if not retain_journal:
                _remove_volume_journal(instance, volume_id)
        except Exception:
            with excutils.save_and_reraise_exception():
                self._retain_volume_cleanup_metadata(
                    instance, volume_id, connection_info, device_info,
                    mountpoint)
                try:
                    # attach_volume() already owns the instance topology lock.
                    self._detach_volume_locked(
                        context, connection_info, instance, mountpoint)
                except Exception:
                    LOG.critical(
                        'Retaining disconnecting journal for Cinder volume '
                        '%s after the final Incus attach update failed',
                        volume_id, instance=instance, exc_info=True)

    @_invalidates_instance_inventory
    def detach_volume(self, context, connection_info, instance, mountpoint,
                      encryption=None):
        return self._detach_volume(
            context, connection_info, instance, mountpoint,
            encryption=encryption, retain_journal=True)

    def _detach_volume(
            self, context, connection_info, instance, mountpoint,
            encryption=None, retain_journal=False):
        """Detach a volume without extending Nova's public driver contract."""
        volume_id = _volume_id(connection_info)
        with lockutils.lock(
                _volume_topology_lock_name(instance), external=True,
                lock_path=_volume_topology_lock_path()):
            with lockutils.lock(
                    _volume_operation_lock_name(volume_id), external=True,
                    lock_path=_volume_operation_lock_path()):
                return self._detach_volume_locked(
                    context, connection_info, instance, mountpoint,
                    encryption=encryption, retain_journal=retain_journal)

    def _detach_volume_locked(
            self, context, connection_info, instance, mountpoint,
            encryption=None, retain_journal=False):
        """Detach block device from a nova instance.

        First the volume id is deleted from the profile, and the
        profile is saved. The os-brick disconnects the volume
        from the host.

        See `nova.virt.driver.Computedriver.detach_volume` for
        more information.
        """
        pre_live_disconnected = (connection_info.get('data') or {}).get(
            _PRE_LIVE_DISCONNECTED_KEY, False)
        bfv_root = (
            mountpoint == getattr(instance, 'root_device_name', None))
        if bfv_root:
            # A cephext root is not an os-brick mapping. Cinder may release its
            # attachment only after both Incus objects that can claim the RBD
            # are gone. The profile check below covers a partially deleted
            # instance; this check covers the inverse partial state.
            _cinder_rbd_root({'connection_info': connection_info})
            try:
                self.client.instances.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if not _is_incus_not_found(exc):
                    raise
            else:
                raise exception.InvalidVolume(
                    reason='Refusing to detach an Incus BFV root while the '
                           'instance still exists')

        try:
            with lockutils.lock(_profile_lock_name(instance)):
                profile = self.client.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
            try:
                volume_id = _volume_id(connection_info)
            except exception.InvalidVolume:
                volume_id = None
            journal = (
                _read_volume_journal(instance, volume_id)
                if volume_id is not None else None)
            if journal is not None:
                protocol = journal.get('driver_volume_type')
                device_info = journal.get('device_info')
                if (not isinstance(protocol, str) or
                        not isinstance(device_info, dict)):
                    raise exception.InvalidVolume(
                        reason='Host Cinder cleanup journal is incomplete')
                effective_connection_info = copy.deepcopy(connection_info)
                effective_connection_info['driver_volume_type'] = protocol
                connection_data = dict(
                    journal.get('connection_data') or {})
                connection_data.update(
                    effective_connection_info.get('data') or {})
                effective_connection_info['data'] = connection_data
                phase = _validate_volume_recovery_record(
                    journal, volume_id, mountpoint,
                    effective_connection_info)
                if phase == 'disconnected':
                    if not retain_journal:
                        _remove_volume_journal(instance, volume_id)
                    return
                storage_driver = brick_get_connector(protocol)
                if phase == 'connecting':
                    # The profile can be removed by an interrupted destroy
                    # after os-brick connected but before device_info was
                    # persisted. Recover the exact same connector handle from
                    # the host journal before disconnecting it.
                    device_info = storage_driver.connect_volume(
                        connection_data)
                    if (not isinstance(device_info, dict) or
                            not device_info.get('path')):
                        raise exception.InvalidVolume(
                            reason='os-brick could not recover device '
                                   'information for unfinished Cinder volume '
                                   '%s' % volume_id)
                    _validate_block_device_path(
                        os.path.realpath(device_info['path']),
                        'Recovered os-brick connector path')
                storage_driver.disconnect_volume(
                    connection_data, device_info)
                _write_volume_journal(
                    instance, volume_id, effective_connection_info,
                    device_info, mountpoint, phase='disconnected')
                if not retain_journal:
                    _remove_volume_journal(instance, volume_id)
                return
            if pre_live_disconnected:
                # ComputeManager intentionally issues a second detach after
                # destination pre-live cleanup. Absence of both profile and
                # journal is the durable idempotency proof.
                return
            if not bfv_root:
                raise
            # Nova destroys the Incus guest and profile before detaching a
            # BFV root for Cinder reimage. The cephext root was already
            # unmounted and never used the os-brick data-volume path.
            return
        if bfv_root:
            raise exception.InvalidVolume(
                reason='Refusing to detach an Incus BFV root while its '
                       'profile still exists')
        volume_id = _detach_volume_id(profile, connection_info, mountpoint)
        device = profile.devices.get(volume_id)
        metadata_keys = _volume_device_info_keys(volume_id)
        has_metadata = any(
            key in profile.config for key in metadata_keys)
        journal = _read_volume_journal(instance, volume_id)
        if device is None and not has_metadata and journal is None:
            LOG.debug(
                'Cinder volume %s is already disconnected from the Incus '
                'profile; treating detach as idempotent',
                volume_id, instance=instance)
            return
        record = (
            journal if journal is not None else
            _profile_volume_record(profile, volume_id, device=device))
        device_info = record['device_info']
        effective_connection_info = copy.deepcopy(connection_info)
        if (not effective_connection_info.get('driver_volume_type') and
                record.get('driver_volume_type')):
            effective_connection_info['driver_volume_type'] = record[
                'driver_volume_type']
        connection_data = dict(record.get('connection_data') or {})
        connection_data.update(
            effective_connection_info.get('data') or {})
        effective_connection_info['data'] = connection_data
        protocol = _detach_volume_protocol(
            effective_connection_info, device, device_info)
        effective_connection_info['driver_volume_type'] = protocol
        storage_driver = brick_get_connector(protocol)
        phase = _validate_volume_recovery_record(
            record, volume_id, mountpoint, effective_connection_info)

        if phase == 'connecting':
            # The intent journal is durable before connect_volume(), but its
            # returned device_info cannot be persisted atomically with the
            # host mapping. Re-run the same connector request after a process
            # crash to recover that cleanup handle. The record validation above
            # prevents a different volume, RBD image or connector from being
            # used to claim the unfinished mapping.
            device_info = storage_driver.connect_volume(connection_data)
            if (not isinstance(device_info, dict) or
                    not device_info.get('path')):
                raise exception.InvalidVolume(
                    reason='os-brick could not recover device information for '
                           'unfinished Cinder volume %s' % volume_id)
            _validate_block_device_path(
                os.path.realpath(device_info['path']),
                'Recovered os-brick connector path')

        # Remove guest access but keep a durable disconnecting journal until
        # os-brick confirms that host state is gone.
        _write_volume_journal(
            instance, volume_id, effective_connection_info,
            device_info or {}, mountpoint, phase='disconnecting')
        with lockutils.lock(_profile_lock_name(instance)):
            profile = self.client.profiles.get(instance.name)
            profile.devices.pop(volume_id, None)
            profile.config[_volume_device_info_key(volume_id)] = (
                _serialize_volume_attachment(
                    effective_connection_info, device_info or {}, mountpoint,
                    phase='disconnecting'))
            profile.config.pop(
                _legacy_volume_device_info_key(volume_id), None)
            try:
                profile.save(wait=True)
            except Exception:
                try:
                    persisted = self.client.profiles.get(instance.name)
                except Exception:
                    raise
                persisted_device = (
                    persisted.devices.get(volume_id)
                    if isinstance(persisted.devices, dict) else None)
                persisted_record = _profile_volume_record(
                    persisted, volume_id, device=persisted_device)
                if (persisted_device is not None or
                        persisted_record.get('phase') != 'disconnecting'):
                    raise
                LOG.warning(
                    'Incus reported a failed detach profile update for volume '
                    '%(volume)s, but the disconnecting journal was persisted; '
                    'continuing host cleanup',
                    {'volume': volume_id},
                    instance=instance)

        storage_driver.disconnect_volume(
            connection_data, device_info or {})

        # Host disconnect is the local commit point. Keep a monotonic marker
        # until ComputeManager has deleted the Cinder attachment and applied
        # its explicit BDM policy; internal callers opt out because they own
        # their surrounding transaction.
        _write_volume_journal(
            instance, volume_id, effective_connection_info,
            device_info or {}, mountpoint, phase='disconnected')
        with lockutils.lock(_profile_lock_name(instance)):
            profile = self.client.profiles.get(instance.name)
            profile.devices.pop(volume_id, None)
            for metadata_key in metadata_keys:
                profile.config.pop(metadata_key, None)
            try:
                profile.save(wait=True)
            except Exception:
                try:
                    persisted = self.client.profiles.get(instance.name)
                except Exception:
                    raise
                if _profile_has_volume_connection(persisted, volume_id):
                    raise
                LOG.warning(
                    'Incus reported a failed final detach update for volume '
                    '%(volume)s, but its cleanup journal is absent',
                    {'volume': volume_id}, instance=instance)
        if not retain_journal:
            _remove_volume_journal(instance, volume_id)

    def _remove_profile_volume_reference(
            self, instance, volume_id, metadata_keys):
        """Remove a failed attach after persisted profile confirmation."""
        try:
            profile = self.client.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if _is_incus_not_found(exc):
                return True
            raise
        except Exception:
            LOG.exception(
                'Cannot read Incus profile while rolling back volume %s',
                volume_id, instance=instance)
            return False

        if not _profile_has_volume_connection(profile, volume_id):
            return True

        profile.devices.pop(volume_id, None)
        for metadata_key in metadata_keys:
            profile.config.pop(metadata_key, None)
        try:
            profile.save(wait=True)
        except Exception:
            try:
                profile = self.client.profiles.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if _is_incus_not_found(exc):
                    return True
                LOG.exception(
                    'Cannot verify Incus profile rollback for volume %s',
                    volume_id, instance=instance)
                return False
            except Exception:
                LOG.exception(
                    'Cannot verify Incus profile rollback for volume %s',
                    volume_id, instance=instance)
                return False
        return not _profile_has_volume_connection(profile, volume_id)

    def _disconnect_profile_volume_connection(
            self, context, instance, volume_id, connection_info=None,
            mountpoint=None):
        """Replay one connector cleanup from its durable profile record."""
        journal = _read_volume_journal(instance, volume_id)
        attach_intent = _read_managed_attach_intent(instance, volume_id)
        source_release = (
            attach_intent is not None and
            attach_intent.get('operation_kind') == 'migration' and
            attach_intent.get('operation_direction') == 'live-source-release')
        if source_release:
            if attach_intent.get('boot_volume'):
                raise exception.InvalidVolume(
                    reason='BFV source release must never enter os-brick '
                           'cleanup')
            expected_mountpoint = attach_intent['mountpoint']
            if mountpoint is not None and mountpoint != expected_mountpoint:
                raise exception.InvalidVolume(
                    reason='Live source release mountpoint changed during '
                           'cleanup')
            # ComputeManager still owns exact old-attachment deletion. Advance
            # host cleanup only to its durable terminal phase and retain both
            # records so a transient Cinder error remains replayable.
            self.recover_source_release_volume_journal(
                context, instance, volume_id, expected_mountpoint)
            return
        try:
            current = self.client.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if _is_incus_not_found(exc):
                current = None
            else:
                raise
        device = (
            current.devices.get(volume_id)
            if current is not None and isinstance(current.devices, dict)
            else None)
        if journal is not None:
            record = journal
        elif current is not None:
            record = _profile_volume_record(
                current, volume_id, device=device)
        else:
            raise exception.InvalidVolume(
                reason='No durable cleanup record exists for Cinder volume %s'
                       % volume_id)
        mountpoint = (
            mountpoint or record.get('mountpoint') or
            (device or {}).get('path'))
        effective = copy.deepcopy(connection_info or {})
        protocol = (
            effective.get('driver_volume_type') or
            record.get('driver_volume_type'))
        if not mountpoint or not protocol:
            raise exception.InvalidVolume(
                reason='Incus profile cleanup record for Cinder volume %s '
                       'is incomplete' % volume_id)
        stored_data = dict(record.get('connection_data') or {})
        stored_data.update(effective.get('data') or {})
        effective.update({
            'serial': volume_id,
            'driver_volume_type': protocol,
            'data': stored_data,
        })
        if current is None:
            device_info = record.get('device_info')
            if not isinstance(device_info, dict):
                raise exception.InvalidVolume(
                    reason='Host Cinder cleanup journal for volume %s has no '
                           'device_info' % volume_id)
            brick_get_connector(protocol).disconnect_volume(
                stored_data, device_info)
            _remove_volume_journal(instance, volume_id)
            return
        self._detach_volume(
            context, effective, instance, mountpoint)

    def _disconnect_profile_volume_connections(self, context, instance):
        """Retry every connector cleanup recorded in an Incus profile."""
        journal_records = _volume_journal_records(instance)
        try:
            profile = self.client.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
            profile = None

        failures = []
        volume_ids = set(journal_records)
        if profile is not None:
            volume_ids.update(_profile_volume_ids(profile))
        for volume_id in sorted(volume_ids):
            try:
                self._disconnect_profile_volume_connection(
                    context, instance, volume_id)
            except Exception as exc:
                failures.append((volume_id, exc))
                LOG.exception(
                    'Failed to disconnect profile-recorded Cinder volume %s',
                    volume_id, instance=instance)
        return failures

    def swap_volume(self, context, old_connection_info, new_connection_info,
                    instance, mountpoint, resize_to):
        """Reject Cinder online swap until block-copy support exists."""
        raise NotImplementedError(
            'Incus volume swap requires copying the old block device into '
            'the replacement before changing the attachment')

    def extend_volume(self, context, connection_info, instance,
                      requested_size):
        """Grow a BFV root filesystem or refresh an attached data device."""
        volume_id = _volume_id(connection_info)
        container = self.client.instances.get(instance.name)
        image_name = 'volume-%s' % volume_id
        if self._resize_bfv_root(container, image_name, requested_size):
            return

        storage_driver = brick_get_connector(
            connection_info['driver_volume_type'])
        try:
            new_size = storage_driver.extend_volume(connection_info['data'])
        except NotImplementedError:
            raise exception.ExtendVolumeNotSupported()
        if new_size is None or new_size < requested_size:
            raise exception.VolumeExtendFailed(
                volume_id=volume_id,
                reason='os-brick reported %s bytes; expected at least %s' %
                (new_size, requested_size))

    @_invalidates_instance_inventory
    def attach_interface(self, context, instance, image_meta, vif):
        net_device = incus_vif.get_vif_devname(vif)
        device = {
                'nictype': 'physical',
                'hwaddr': vif['address'],
                'name': incus_vif.get_vif_guest_devname(vif),
                'parent': incus_vif.get_vif_internal_devname(vif),
                'type': 'nic',
        }

        try:
            self.vif_driver.plug(instance, vif)
            self.firewall_driver.setup_basic_filtering(instance, vif)

            def add_device():
                container = self.client.instances.get(instance.name)
                devices = dict(container.devices)
                devices[net_device] = device
                container.devices = devices
                container.save(wait=True)

            with lockutils.lock(_profile_lock_name(instance)):
                _retry_incus_instance_action(
                    add_device,
                    'attach interface {}'.format(vif['id']),
                    instance, retry_transient=True)
        except Exception:
            with excutils.save_and_reraise_exception():
                if self._remove_instance_device_reference(
                        instance, net_device):
                    try:
                        self.firewall_driver.unfilter_instance(
                            instance, [vif])
                    except Exception:
                        LOG.exception(
                            'Failed to roll back filtering for VIF %s',
                            vif['id'], instance=instance)
                    try:
                        self.vif_driver.unplug(instance, vif)
                    except Exception:
                        LOG.exception(
                            'Failed to roll back host wiring for VIF %s',
                            vif['id'], instance=instance)
                else:
                    LOG.error(
                        'Retaining VIF wiring for %(vif)s because Incus '
                        'instance %(instance)s still references it after '
                        'attach failed',
                        {
                            'vif': vif['id'],
                            'instance': instance.name,
                        },
                        instance=instance)

    def _remove_instance_device_reference(self, instance, device_name):
        with lockutils.lock(_profile_lock_name(instance)):
            try:
                container = self.client.instances.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if _is_incus_not_found(exc):
                    return True
                LOG.exception(
                    'Cannot read Incus instance while rolling back device %s',
                    device_name, instance=instance)
                return False
            except Exception:
                LOG.exception(
                    'Cannot read Incus instance while rolling back device %s',
                    device_name, instance=instance)
                return False

            if device_name not in container.devices:
                return True
            devices = dict(container.devices)
            devices.pop(device_name, None)
            container.devices = devices
            try:
                container.save(wait=True)
            except Exception:
                try:
                    container = self.client.instances.get(instance.name)
                except incus_exceptions.LXDAPIException as exc:
                    if _is_incus_not_found(exc):
                        return True
                    LOG.exception(
                        'Cannot verify Incus device rollback for %s',
                        device_name, instance=instance)
                    return False
                except Exception:
                    LOG.exception(
                        'Cannot verify Incus device rollback for %s',
                        device_name, instance=instance)
                    return False
            return device_name not in container.devices

    @_invalidates_instance_inventory
    def detach_interface(self, context, instance, vif):
        # A Neutron vif-deleted event races with normal server deletion. The
        # destroy path owns profile and instance cleanup, so changing the
        # profile here would only contend with stop/delete and produce a
        # misleading external_instance_event failure.
        if instance.task_state == task_states.DELETING:
            self.vif_driver.unplug(instance, vif)
            return

        requested_devname = incus_vif.get_vif_devname(vif)

        def find_device(devices):
            if requested_devname in devices:
                return requested_devname

            for name, device in devices.items():
                if device.get('hwaddr') == vif['address']:
                    return name

            return None

        def update_local_device(device_name, device):
            def update():
                container = self.client.instances.get(instance.name)
                devices = dict(container.devices)
                if device is None:
                    devices.pop(device_name, None)
                else:
                    devices[device_name] = device
                container.devices = devices
                container.save(wait=True)

            _retry_incus_instance_action(
                update,
                'update interface {} during detach'.format(vif['id']),
                instance, retry_transient=True)

        try:
            with lockutils.lock(_profile_lock_name(instance)):
                container = self.client.instances.get(instance.name)
                profile = self.client.profiles.get(instance.name)
                profile_devname = find_device(profile.devices)
                local_devname = find_device(container.devices)
                if (local_devname is None and profile_devname is not None and
                        profile_devname in container.devices):
                    local_devname = profile_devname

                local_device = (
                    copy.deepcopy(container.devices.get(local_devname))
                    if local_devname is not None else None)
                mask_applied = (
                    local_device is not None and
                    local_device.get('type') == 'none')

                if profile_devname is None:
                    if local_devname is not None:
                        update_local_device(local_devname, None)
                    self.vif_driver.unplug(instance, vif)
                    return

                # Mask the inherited profile NIC using an instance-local
                # "none" device. This atomically replaces the usual same-name
                # local override and hides the inherited profile NIC.
                if not mask_applied:
                    update_local_device(profile_devname, {'type': 'none'})

                if (local_devname is not None and
                        local_devname != profile_devname):
                    update_local_device(local_devname, None)

                devices = dict(profile.devices)
                devices.pop(profile_devname)
                profile.devices = devices
                try:
                    profile.save(wait=True)
                except Exception:
                    # Incus can return either an explicit partial-success
                    # error or lose the response after persisting the update.
                    # Read the authoritative profile before deciding whether
                    # to commit the detach or restore the local overrides.
                    saved_profile = self.client.profiles.get(instance.name)
                    if profile_devname in saved_profile.devices:
                        try:
                            if (local_devname is not None and
                                    local_devname != profile_devname and
                                    local_device is not None):
                                update_local_device(
                                    local_devname, local_device)
                            if (local_devname == profile_devname and
                                    local_device is not None):
                                update_local_device(
                                    profile_devname, local_device)
                            else:
                                update_local_device(profile_devname, None)
                        except Exception:
                            LOG.critical(
                                'Failed to restore the Incus device state '
                                'for VIF %s after profile detach failed',
                                vif['id'], instance=instance,
                                exc_info=True)
                        raise

                # The profile no longer supplies the NIC, so remove the
                # temporary mask. The effective configuration stays detached.
                update_local_device(profile_devname, None)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
            # This method is called when an instance get destroyed. It
            # could happen that Nova to receive an event
            # "vif-delete-event" after the instance is destroyed which
            # result the incus profile not exist.
            LOG.debug("incus profile for instance {instance} does not exist. "
                      "The instance probably got destroyed before this method "
                      "got called.".format(instance=instance.name))

        self.vif_driver.unplug(instance, vif)

    @_invalidates_instance_inventory
    @_guards_serial_console
    def migrate_disk_and_power_off(
            self, context, instance, dest, flavor, network_info,
            block_device_info=None, timeout=0, retry_interval=0):
        if dest == self.host:
            raise exception.InstanceFaultRollback(
                inner_exception=exception.UnableToMigrateToSelf(
                    instance_id=instance.uuid, host=self.host))
        if not CONF.incus.allow_cold_migration:
            raise exception.MigrationError(
                reason='Incus cold migration is disabled by configuration')

        root_bdm = _boot_from_volume(block_device_info)
        if (root_bdm is None and
                flavor.root_gb < instance.flavor.root_gb):
            raise exception.InstanceFaultRollback(
                exception.ResizeError(
                    reason='Incus root filesystems cannot be resized down'))

        migration_address = CONF.incus.migration_address
        parsed_address = parse.urlsplit(migration_address or '')
        if (parsed_address.scheme != 'https' or
                not parsed_address.netloc or
                parsed_address.path not in ('', '/')):
            raise exception.InvalidConfiguration(
                '[incus] migration_address must be an HTTPS origin')

        if root_bdm:
            root_volume = _require_bfv_migration_support(
                self.client, root_bdm)
            try:
                _preflight_bfv_migration_destination(dest, root_volume[0])
            except exception.MigrationError as exc:
                # No instance state has changed yet. Tell the compute manager
                # to restore the pre-migration vm_state instead of setting the
                # still-running source instance to ERROR.
                raise exception.InstanceFaultRollback(exc) from exc

        cleanup_token = _cold_migration_cleanup_token(context, instance)
        container = self.client.instances.get(instance.name)
        root_pool = _instance_root_pool(self.client, instance.name)
        if root_pool.driver == 'ceph':
            source_identity = _storage_pool_identity(root_pool)
            required = {
                INCUS_STORAGE_HANDOVER_EXTENSION,
                INCUS_STORAGE_HANDOVER_PROOF_EXTENSION,
                INCUS_STORAGE_READY_FENCE_EXTENSION,
            }
            missing = sorted(
                required - set(
                    self.client.host_info.get('api_extensions', [])))
            if missing:
                raise exception.MigrationError(
                    reason='Incus source does not advertise required shared '
                    'Ceph handover extensions: %s' % ', '.join(missing))
            _preflight_shared_ceph_handover_destination(
                dest, root_pool.name, source_identity)
        with lockutils.lock(_profile_lock_name(instance)):
            migration_profile = self.client.profiles.get(instance.name)
            try:
                _validate_migration_share_mappings(
                    context, instance, migration_profile)
            except exception.MigrationPreCheckError as exc:
                # No source or destination state has changed yet. Preserve
                # the pre-resize VM state while reporting the authoritative
                # Nova/profile mismatch to the operator.
                raise exception.InstanceFaultRollback(
                    inner_exception=exc) from exc
            idmap_base, idmap_size = _instance_migration_idmap(
                container, migration_profile)
            try:
                self._ensure_instance_idmap(
                    instance, observed_base=idmap_base,
                    observed_size=idmap_size)
            except incus_idmap.IDMapError as exc:
                # The source is still running and no migration marker has
                # been written. Preserve Nova's pre-migration state.
                raise exception.InstanceFaultRollback(
                    exception.MigrationError(
                        reason='Incus source idmap is not globally reserved: '
                               '{}'.format(exc))) from exc
            if migration_profile.config.get(CLEANUP_RECOVERY_KEY):
                raise exception.MigrationError(
                    reason='Incus source profile has unresolved cleanup work')
            unresolved_generation_keys = (
                MIGRATION_CLEANUP_TOKEN_KEY,
                MIGRATION_ROLLBACK_COMPLETE_KEY,
                MIGRATION_NOVA_UUID_KEY,
                MIGRATION_DESTINATION_KEY,
                MIGRATION_OPERATION_KEY,
            )
            if any(
                    migration_profile.config.get(key)
                    for key in unresolved_generation_keys):
                raise exception.MigrationError(
                    reason='Incus source profile has an unresolved migration '
                           'generation')
            migration_profile.config[MIGRATION_DESTINATION_KEY] = (
                _migration_address_for_host(dest))
            migration_profile.config[MIGRATION_CLEANUP_TOKEN_KEY] = (
                cleanup_token)
            migration_profile.config.pop(
                MIGRATION_CLEANUP_COMPLETE_KEY, None)
            migration_profile.config.pop(
                MIGRATION_ROLLBACK_COMPLETE_KEY, None)
            migration_profile.save(wait=True)
        configdrive_payload = None
        if instance.config_drive:
            configdrive_payload = _pack_configdrive_for_migration(
                instance, container)
        destination_address = _migration_address_for_host(dest)
        migration_target = _migration_client(destination_address)
        try:
            _register_migration_attempt(
                migration_target, instance, cleanup_token,
                idmap_base, idmap_size)
        except Exception:
            with lockutils.lock(_profile_lock_name(instance)):
                migration_profile = self.client.profiles.get(instance.name)
                migration_profile.config.pop(
                    MIGRATION_DESTINATION_KEY, None)
                migration_profile.config.pop(
                    MIGRATION_CLEANUP_TOKEN_KEY, None)
                migration_profile.save(wait=True)
            raise
        was_running = container.status != 'Stopped'

        detach_attempted = []
        source_operation_id = None
        try:
            if was_running:
                container.stop(wait=True)
            migration_data = container.generate_migration_data(live=False)
            # pylxd historically emitted the source profile list under the
            # non-API key ``default``. The destination profile is recreated
            # by Nova and must be selected explicitly for its root quota and
            # Neutron physical NIC devices to apply.
            migration_data.pop('default', None)
            migration_data['profiles'] = [instance.name]
            source = migration_data['source']
            source_operation_id = _migration_operation_id(
                source['operation'])
            if source_operation_id is None:
                raise exception.MigrationError(
                    reason='Incus source migration returned no operation UUID')
            source['operation'] = _migration_operation_url(
                source['operation'], migration_address)
            with lockutils.lock(_profile_lock_name(instance)):
                migration_profile = self.client.profiles.get(instance.name)
                migration_profile.config[MIGRATION_OPERATION_KEY] = (
                    source_operation_id)
                migration_profile.save(wait=True)

            for bdm in driver.block_device_info_get_mapping(
                    block_device_info):
                if _is_boot_volume(bdm):
                    # The root RBD is transferred by the Incus cephext
                    # handover. It must never enter the os-brick data-volume
                    # detach/attach path.
                    continue
                connection_info = bdm.get('connection_info')
                mountpoint = bdm.get('mount_device')
                if connection_info and mountpoint:
                    # Record rollback ownership before detach. A failed detach
                    # can already have removed guest access and persisted a
                    # disconnecting journal, so restoring only calls that
                    # returned successfully would leave the source with
                    # partial I/O.
                    volume_id = _volume_id(connection_info)
                    with lockutils.lock(
                            _volume_manager_transaction_lock_name(
                                instance.uuid, volume_id),
                            external=True,
                            lock_path=_volume_operation_lock_path()):
                        intent = self.prepare_managed_volume_attach(
                            instance, volume_id, _bdm_attachment_id(bdm),
                            mountpoint, operation_kind='migration',
                            operation_token=cleanup_token,
                            operation_direction='cold-source-restore',
                            operation_migration_uuid=cleanup_token)
                        detach_attempted.append(bdm)
                        self._detach_volume(
                            context, connection_info, instance, mountpoint,
                            retain_journal=True)
        except Exception as original_error:
            rollback_failures = []
            try:
                attempt = _abort_migration_attempt(
                    migration_target, instance, cleanup_token,
                    idmap_base, idmap_size,
                    target_cleanup=lambda: _retry_migration_finish_action(
                        lambda: _delete_migration_target_record(
                            migration_target, instance),
                        'aborted cold migration target deletion', instance))
            except Exception as attempt_error:
                raise exception.MigrationError(
                    reason='Cold migration preparation failed and its target '
                           'attempt could not be fenced; the source remains '
                           'stopped') from attempt_error
            if attempt['state'] == 'committed':
                raise exception.MigrationError(
                    reason='Cold migration preparation failed after the '
                           'target attempt committed; the source remains '
                           'stopped for reconciliation') from original_error
            try:
                _settle_instance_migration_operations(
                    self.client, instance,
                    operation_ids=(source_operation_id,))
            except Exception as operation_error:
                raise exception.MigrationError(
                    reason='Cold migration preparation failed and its Incus '
                           'source operation could not be fenced; the source '
                           'remains stopped') from operation_error
            restored_volumes = []
            for bdm in reversed(detach_attempted):
                connection_info = bdm['connection_info']
                mountpoint = bdm['mount_device']
                try:
                    intent = _retry_migration_finish_action(
                        lambda connection_info=connection_info,
                        mountpoint=mountpoint:
                        self._attach_volume_for_operation(
                            context, connection_info, instance, mountpoint,
                            _bdm_attachment_id(bdm), 'migration',
                            cleanup_token, 'cold-source-restore',
                            operation_migration_uuid=cleanup_token),
                        'source data-volume rollback', instance)
                    restored_volumes.append((
                        _volume_id(connection_info), connection_info,
                        mountpoint, intent))
                except Exception as rollback_error:
                    rollback_failures.append(
                        (_volume_id(connection_info), rollback_error))
                    LOG.exception(
                        'Failed to restore a source volume after migration '
                        'preparation failed', instance=instance)
            if rollback_failures:
                raise exception.MigrationError(
                    reason=(
                        'Cold migration preparation failed and {} source '
                        'Cinder volume connection(s) could not be restored; '
                        'the source remains stopped to prevent partial I/O'
                    ).format(len(rollback_failures))) from original_error
            # Persist an enumerable source generation before retiring the
            # first per-volume intent. This also covers the zero-volume case.
            self.mark_source_volume_generation_rollback_complete(
                instance, cleanup_token, cleanup_token)
            for volume_id, connection_info, mountpoint, intent in (
                    restored_volumes):
                self._commit_internal_volume_attach_operation(
                    instance, volume_id, connection_info, mountpoint, intent)
            container.sync()
            if was_running and container.status != 'Running':
                _retry_migration_finish_action(
                    lambda: self._start_instance_with_idmap(
                        instance, container),
                    'source restart after migration preparation failure',
                    instance)
            if not self.finalize_failed_cold_source_volume_generation(
                    instance, cleanup_token):
                LOG.critical(
                    'Cold source runtime was restored but exact Cinder '
                    'recovery evidence remains; retaining the migration '
                    'profile token for periodic retirement',
                    instance=instance)
            raise

        return jsonutils.dumps({
            'format': 'incus-pull-v1',
            'boot_from_volume': bool(root_bdm),
            'migration_data': migration_data,
            'source_operation_id': source_operation_id,
            'cleanup_token': cleanup_token,
            'idmap_base': idmap_base,
            'idmap_size': idmap_size,
            'was_running': was_running,
            'configdrive': configdrive_payload,
        })

    def snapshot(self, context, instance, image_id, update_task_state):
        if compute_utils.is_volume_backed_instance(context, instance):
            raise exception.InvalidRequest(
                'The Incus image snapshot path cannot publish a Cinder '
                'boot volume; use Nova volume-backed snapshot orchestration')

        # The name is what excludes; see the note in _sync_glance_image.
        with lockutils.lock(
                'incus-container-{}'.format(instance.name), external=True):

            update_task_state(task_state=task_states.IMAGE_PENDING_UPLOAD)

            container = self.client.instances.get(instance.name)
            instance_snapshot = None
            image = None
            data = None
            try:
                snapshot_name = 'nova-{}'.format(image_id)
                instance_snapshot = container.snapshots.create(
                    snapshot_name, wait=True)
                image = instance_snapshot.publish(wait=True)

                update_task_state(
                    task_state=task_states.IMAGE_UPLOADING,
                    expected_state=task_states.IMAGE_PENDING_UPLOAD)

                snapshot = IMAGE_API.get(context, image_id)
                data = image.export()
                image_meta = {'name': snapshot['name'],
                              'disk_format': 'raw',
                              'container_format': 'bare'}
                IMAGE_API.update(context, image_id, image_meta, data)
            finally:
                if data is not None:
                    data.close()
                if image is not None:
                    image.delete(wait=True)
                if instance_snapshot is not None:
                    instance_snapshot.delete(wait=True)

    @_invalidates_instance_inventory
    def pause(self, instance):
        """Pause container.

        See `nova.virt.driver.ComputeDriver.pause` for more
        information.
        """
        container = self.client.instances.get(instance.name)
        container.freeze(wait=True)

    @_invalidates_instance_inventory
    def unpause(self, instance):
        """Unpause container.

        See `nova.virt.driver.ComputeDriver.unpause` for more
        information.
        """
        container = self.client.instances.get(instance.name)
        container.unfreeze(wait=True)

    def suspend(self, context, instance):
        raise NotImplementedError(
            'Incus freeze does not satisfy Nova suspend semantics because '
            'it does not release instance memory')

    @_invalidates_instance_inventory
    def resume(self, context, instance, network_info, block_device_info=None,
               share_info=None):
        raise NotImplementedError(
            'Incus suspend and resume require a reliable container '
            'checkpoint implementation')

    @_invalidates_instance_inventory
    def resume_state_on_host_boot(self, context, instance, network_info,
                                  share_info, block_device_info=None):
        """resume guest state when a host is booted."""
        try:
            state = self.get_info(instance).state
            ignored_states = (power_state.RUNNING,
                              power_state.SUSPENDED,
                              power_state.NOSTATE,
                              power_state.PAUSED)

            if state in ignored_states:
                return

            self.power_on(
                context, instance, network_info, block_device_info,
                share_info=share_info)
        except (exception.InternalError, exception.InstanceNotFound):
            pass

    def rescue(self, context, instance, network_info, image_meta,
               rescue_password, block_device_info, share_info):
        raise NotImplementedError(
            'Incus container rescue requires storage-pool-native rootfs '
            'attachment and is not implemented')

    def unrescue(self, context, instance):
        raise NotImplementedError(
            'Incus container rescue is not implemented')

    def _release_serial_console_broker(self, instance):
        """Close and drop any serial console broker for a departed guest.

        Brokers used to be reclaimed only by destroy; power-off, resize and
        migration left them holding proxy ports for a dead console until
        the pool drained.

        Use this only where the guest is already gone from this host. While
        it is still running here, releasing without holding the console
        closed lets a concurrent request build a replacement that the
        imminent stop will strand - use ``_quiesced_serial_console``.
        """
        with self._serial_consoles_lock:
            broker = self._serial_consoles.pop(instance.uuid, None)
        if broker is None:
            return
        try:
            broker.close()
        except Exception:
            LOG.exception(
                'Failed to close the Incus serial console broker for a '
                'stopping instance', instance=instance)

    @contextlib.contextmanager
    def _quiesced_serial_console(self, instance):
        """Hold the console closed across an operation that stops a guest.

        Closing the broker first and stopping the container afterwards
        leaves a window in which the container is still Running and no
        broker is registered, so a concurrent get_serial_console builds a
        new one - which the stop then strands, holding a proxy port for a
        console that can never produce output. Refusing new brokers for the
        whole operation is what actually closes the leak.

        The refusal is always lifted, including when the operation fails,
        because a later console request re-reads the authoritative Incus
        state and must not be blocked by a stop that did not happen.
        """
        with self._serial_consoles_lock:
            self._serial_console_destroying.add(instance.uuid)
            broker = self._serial_consoles.pop(instance.uuid, None)
        if broker is not None:
            try:
                broker.close()
            except Exception:
                LOG.exception(
                    'Failed to close the Incus serial console broker before '
                    'stopping the instance', instance=instance)
        try:
            yield
        finally:
            with self._serial_consoles_lock:
                self._serial_console_destroying.discard(instance.uuid)

    @_invalidates_instance_inventory
    def power_off(self, instance, timeout=0, retry_interval=0):
        """Power off an instance

        See 'nova.virt.drvier.ComputeDriver.power_off` for more
        information.
        """
        def force_stop():
            container = self.client.instances.get(instance.name)
            if container.status != 'Stopped':
                container.stop(timeout=0, force=True, wait=True)

        with self._quiesced_serial_console(instance):
            self._power_off_locked(instance, timeout, retry_interval,
                                   force_stop)

    def _power_off_locked(self, instance, timeout, retry_interval,
                          force_stop):
        """Stop the guest while new console brokers are refused."""
        if timeout:
            def clean_stop():
                container = self.client.instances.get(instance.name)
                if container.status != 'Stopped':
                    # Incus owns the timeout and sends the guest's clean
                    # shutdown signal once. Nova's retry_interval therefore
                    # has no separate polling role on this backend.
                    container.stop(
                        timeout=timeout, force=False, wait=True)

            try:
                _retry_incus_instance_action(
                    clean_stop, 'graceful power off', instance,
                    retry_transient=True)
                return
            except incus_exceptions.LXDAPIException as exc:
                if (
                    _is_retryable_migration_exception(exc) or
                    _incus_api_status_code(exc) != 400
                ):
                    # An exhausted busy/transport-class server error leaves
                    # the result uncertain. Do not issue a destructive second
                    # operation until Nova retries the action. Authorization
                    # and server failures must also preserve their real error.
                    raise
                LOG.info(
                    'Incus graceful shutdown failed; forcing power off',
                    instance=instance, exc_info=True)

        _retry_incus_instance_action(
            force_stop, 'forced power off', instance, retry_transient=True)

    @_invalidates_instance_inventory
    def power_on(self, context, instance, network_info,
                 block_device_info=None, accel_info=None, share_info=None):
        """Power on an instance

        See 'nova.virt.drvier.ComputeDriver.power_on` for more
        information.
        """
        # A host restart destroys os-brick's process-local assumptions. Rebuild
        # and prove the complete Nova Cinder topology before any guest start.
        reconciled_container = self._reconcile_reboot_data_volumes(
            context, instance, block_device_info)
        profile = self.client.profiles.get(instance.name)
        self._attach_share_devices(profile, instance, share_info)

        def start():
            nonlocal reconciled_container
            container = reconciled_container
            reconciled_container = None
            if container is None:
                container = self.client.instances.get(instance.name)
            if container.status != 'Running':
                self._start_instance_with_idmap(instance, container)

        _retry_incus_instance_action(
            start, 'power on', instance, retry_transient=True)
        self._reassert_vifs(instance, network_info)

    def _reassert_vifs(self, instance, network_info):
        """Restore host-side VIF wiring after a container start."""
        for vif in network_info or []:
            self.vif_driver.reassert(instance, vif)

    def _attach_share_devices(
            self, profile, instance, share_info, operation_token=None):
        changed = False
        attached = []
        added_device_names = []
        for share_mapping in share_info or []:
            mount_path = _share_mount_path(instance, share_mapping)
            if not os.path.ismount(mount_path):
                raise exception.ShareMountError(
                    share_id=share_mapping.share_id,
                    server_id=instance.uuid,
                    reason='host share mount is absent')
            device_name = _share_device_name(share_mapping)
            expected = {
                'type': 'disk',
                'source': mount_path,
                'path': _share_guest_path(share_mapping),
                'readonly': 'false',
                'recursive': 'true',
            }
            current = profile.devices.get(device_name)
            if current is not None and current != expected:
                raise exception.ShareMountError(
                    share_id=share_mapping.share_id,
                    server_id=instance.uuid,
                    reason='Incus share device conflicts with existing state')
            if current is None:
                profile.devices[device_name] = expected
                changed = True
                added_device_names.append(device_name)
            attached.append(share_mapping)
        if changed:
            try:
                profile.save(wait=True)
            except Exception:
                try:
                    persisted = self.client.profiles.get(instance.name)
                except Exception:
                    for device_name in added_device_names:
                        profile.devices.pop(device_name, None)
                    raise
                for share_mapping in attached:
                    device_name = _share_device_name(share_mapping)
                    expected = profile.devices[device_name]
                    if persisted.devices.get(device_name) != expected:
                        for added_name in added_device_names:
                            profile.devices.pop(added_name, None)
                        raise
                LOG.warning(
                    'Incus reported a failed Manila profile update, but all '
                    'share devices were persisted; continuing attachment',
                    instance=instance)
        # A pre-profile journal is no longer needed only after the matching
        # profile device is known to be durable.
        for share_mapping in attached:
            owner_token = (
                operation_token or
                _share_mapping_owner_token(instance, share_mapping))
            _remove_share_journal(
                instance, share_mapping.share_id, owner_token)
        return tuple(added_device_names)

    def _attach_journaled_share_devices(
            self, profile, instance, operation_token, expected_share_ids):
        mappings = _journaled_share_mappings(
            instance, operation_token,
            expected_share_ids=expected_share_ids)
        self._attach_share_devices(
            profile, instance, mappings,
            operation_token=operation_token)
        return mappings

    def _attach_cold_migration_share_devices(
            self, profile, instance, share_mappings, operation_token):
        if not share_mappings:
            return
        with lockutils.lock(_profile_lock_name(instance)):
            profile = self.client.profiles.get(instance.name)
            self._attach_share_devices(
                profile, instance, share_mappings,
                operation_token=operation_token)

    def get_share_mount_table(self):
        """Build one mount table snapshot for a manager share transaction."""
        return _share_mount_table_index()

    @_invalidates_instance_inventory
    def mount_share(self, context, instance, share_mapping):
        """Mount an approved Manila export and stage its Incus disk device."""
        with lockutils.lock(
                _share_operation_lock_name(
                    instance, share_mapping.share_id)):
            return self._mount_share_locked(
                context, instance, share_mapping)

    @_invalidates_instance_inventory
    def mount_share_transaction(
            self, context, instance, share_mapping, mount_table):
        """Mount one member using a transaction-wide mount snapshot."""
        with lockutils.lock(
                _share_operation_lock_name(
                    instance, share_mapping.share_id)):
            return self._mount_share_locked(
                context, instance, share_mapping,
                mount_table=mount_table)

    def _stage_share_mount_locked(
            self, instance, share_mapping, operation_token=None,
            mount_table=None):
        """Mount a Manila export without requiring an Incus profile."""
        if not CONF.incus.enable_manila_shares:
            raise exception.ShareProtocolNotSupported(
                share_proto=share_mapping.share_proto)
        operation_token = (
            operation_token or
            _share_mapping_owner_token(instance, share_mapping))
        existing = _read_share_journal(
            instance, share_mapping, operation_token=operation_token)
        if existing is None:
            _write_share_journal(
                instance, share_mapping, operation_token, 'staging')
        mount_path = _ensure_share_mount_path(instance, share_mapping)
        real_mount_path = os.path.realpath(mount_path)
        if mount_table is None:
            mounted = os.path.ismount(mount_path)
            mount_table = (
                _share_mount_table_index() if mounted else {})
        else:
            mounted = real_mount_path in mount_table
        if mounted:
            _validate_existing_share_mount(
                mount_path, share_mapping, mount_table=mount_table)
            _write_share_journal(
                instance, share_mapping, operation_token, 'mounted')
            return mount_path, False

        secret_path = None
        try:
            options = ['rw', 'nosuid', 'nodev']
            device = share_mapping.export_location
            if (share_mapping.share_proto ==
                    obj_fields.ShareMappingProto.NFS):
                fstype = 'nfs'
            elif (share_mapping.share_proto ==
                  obj_fields.ShareMappingProto.CEPHFS):
                if (not share_mapping.access_to or
                        not share_mapping.access_key):
                    raise exception.ShareMountError(
                        share_id=share_mapping.share_id,
                        server_id=instance.uuid,
                        reason='CephFS credentials are missing')
                fstype = 'ceph'
                device, mon_addr = _cephfs_mount_spec(share_mapping)
                fd, secret_path = tempfile.mkstemp(
                    prefix='.ceph-secret-',
                    dir=os.path.dirname(mount_path))
                try:
                    os.fchmod(fd, 0o600)
                    os.write(fd, share_mapping.access_key.encode('utf-8'))
                finally:
                    os.close(fd)
                options = [
                    'rw', 'nosuid', 'nodev',
                    'mon_addr=%s' % mon_addr,
                    'name=%s' % share_mapping.access_to,
                    'secretfile=%s' % secret_path,
                ]
            else:
                raise exception.ShareProtocolNotSupported(
                    share_proto=share_mapping.share_proto)
            incus_privsep.mount(
                fstype,
                (device if fstype == 'ceph'
                 else share_mapping.export_location),
                mount_path, options,
                CONF.incus.share_mount_timeout)
            mount_table[real_mount_path] = {
                'device': _normalize_share_export(device),
                'fstype': fstype,
                'opts': frozenset(options),
            }
            _write_share_journal(
                instance, share_mapping, operation_token, 'mounted')
            return mount_path, True
        except (exception.ShareMountError,
                exception.ShareProtocolNotSupported):
            raise
        except Exception as exc:
            raise exception.ShareMountError(
                share_id=share_mapping.share_id,
                server_id=instance.uuid,
                reason=exc)
        finally:
            if secret_path:
                try:
                    os.unlink(secret_path)
                except FileNotFoundError:
                    pass

    def stage_share_for_live_migration(
            self, context, instance, share_mapping, operation_token,
            mount_table=None):
        """Stage a destination export before its Incus profile exists."""
        with lockutils.lock(
                _share_operation_lock_name(
                    instance, share_mapping.share_id)):
            _mount_path, mounted_now = self._stage_share_mount_locked(
                instance, share_mapping,
                operation_token=operation_token,
                mount_table=mount_table)
            return mounted_now

    def stage_share_for_cold_migration(
            self, context, instance, share_mapping, operation_token,
            mount_table=None):
        """Stage a cold-migration destination with an exact attempt owner."""
        with lockutils.lock(
                _share_operation_lock_name(
                    instance, share_mapping.share_id)):
            _mount_path, mounted_now = self._stage_share_mount_locked(
                instance, share_mapping,
                operation_token=operation_token,
                mount_table=mount_table)
            return mounted_now

    def preflight_cold_migration_destination_profile(
            self, instance, disk_info):
        """Fence a conflicting target profile before Nova rotates volumes."""
        (
            _transfer, _migration_data, cleanup_token,
            idmap_base, idmap_size, _expected_share_ids,
        ) = _parse_cold_migration_transfer(disk_info)
        try:
            profile = self.client.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if _is_incus_not_found(exc):
                return
            raise

        config = profile.config if isinstance(profile.config, dict) else {}

        def reject_conflicting_profile(reason, owner_error=None):
            # The source registered this exact target attempt before sending
            # disk_info. Abort it while it is still unstarted, but never use
            # that attempt as authority to alter an older same-name profile.
            attempt = _abort_migration_attempt(
                self.client, instance, cleanup_token,
                idmap_base, idmap_size)
            if (attempt['state'] == 'committed' or
                    attempt.get('started')):
                error = exception.MigrationError(
                    reason='A conflicting Incus destination profile exists '
                           'after the cold migration target started')
            else:
                error = exception.MigrationPreCheckError(reason=reason)
            if owner_error is not None:
                raise error from owner_error
            raise error

        try:
            binding = _destination_prepared_profile_binding(config)
        except exception.MigrationError as owner_error:
            reject_conflicting_profile(
                'Incus cold migration destination retains an unresolved '
                'profile from another operation', owner_error=owner_error)

        if (
                binding['uuid'] != instance.uuid or
                binding['operation_token'] != cleanup_token or
                binding['migration_uuid'] != cleanup_token or
                binding['idmap_base'] != idmap_base or
                binding['idmap_size'] != idmap_size
        ):
            reject_conflicting_profile(
                'Incus cold migration destination profile belongs to another '
                'prepared transaction')

    def rollback_cold_migration_preparation(
            self, context, instance, disk_info):
        """Fail closed around a failed manager-side cold preparation."""
        (
            transfer, _migration_data, cleanup_token,
            idmap_base, idmap_size, _expected_share_ids,
        ) = _parse_cold_migration_transfer(disk_info)

        # Entering ComputeManager's base helper does not prove whether
        # finish_migration created a target. The prepared marker is written in
        # the initial profile create, so even a profile-only crash can now be
        # fenced and cleaned without depending on a Manila mount journal.
        try:
            container = self.client.instances.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
            container = None
        try:
            profile = self.client.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
            profile = None

        if container is not None or profile is not None:
            if container is not None:
                if _instance_nova_uuid(container) != instance.uuid:
                    raise exception.MigrationError(
                        reason='Refusing to reconcile a cold migration '
                               'target with a mismatched Nova UUID')
            if profile is None:
                # An instance without its durable profile has no complete
                # transaction binding. Keep it for operator reconciliation.
                return True
            profile_config = (
                profile.config
                if isinstance(profile.config, dict) else {})
            binding = _destination_prepared_profile_binding(profile_config)
            if (
                    binding['uuid'] != instance.uuid or
                    binding['operation_token'] != cleanup_token or
                    binding['idmap_base'] != idmap_base or
                    binding['idmap_size'] != idmap_size
            ):
                raise exception.MigrationError(
                    reason='Refusing to reconcile a cold migration profile '
                           'outside this cleanup transaction')
            if (
                    container is not None and
                    _instance_migration_receive_complete(container)
            ):
                try:
                    self._mark_migration_recovery_required(
                        instance,
                        power_on=bool(transfer.get('was_running', True)))
                except Exception:
                    # A missing recovery marker is not evidence that the
                    # target is disposable. Retaining its mount journals and
                    # idmap is the only data-preserving outcome.
                    LOG.critical(
                        'Failed to mark a cold migration target for '
                        'recovery; retaining every target resource',
                        instance=instance, exc_info=True)
                return True

            network_info = self.network_api.get_instance_nw_info(
                context, instance)
            attempt = self._abort_and_cleanup_destination_profile(
                context, instance, cleanup_token, idmap_base, idmap_size,
                network_info)
            # A committed attempt is never cleanup authority. The target is
            # retained even if its local instance record is missing.
            return True

        attempt = None
        try:
            attempt = _abort_migration_attempt(
                self.client, instance, cleanup_token,
                idmap_base, idmap_size)
        except incus_exceptions.LXDAPIException as exc:
            # A manager retry can arrive after a previous rollback already
            # retired the exact attempt. Journal cleanup remains idempotent.
            if not _is_incus_not_found(exc):
                raise
        if attempt is not None and attempt['state'] == 'committed':
            return True
        if (
                attempt is not None and
                (
                    attempt['state'] not in ('aborted', 'failed') or
                    not attempt.get('finished')
                )
        ):
            return True

        # Do not retire the idmap reservation until every host mount owned by
        # this token is gone. A cleanup failure deliberately leaves the
        # aborted attempt as a retryable safety reservation.
        _cleanup_share_journal_mounts(
            instance, operation_token=cleanup_token)
        if attempt is not None:
            _retire_migration_attempt(
                self.client, instance, cleanup_token,
                idmap_base, idmap_size)
        return False

    def _mount_share_locked(
            self, context, instance, share_mapping, mount_table=None):
        owner_token = _share_mapping_owner_token(instance, share_mapping)
        _mount_path, mounted_now = self._stage_share_mount_locked(
            instance, share_mapping, operation_token=owner_token,
            mount_table=mount_table)
        try:
            with lockutils.lock(_profile_lock_name(instance)):
                try:
                    profile = self.client.profiles.get(instance.name)
                except incus_exceptions.LXDAPIException as exc:
                    if _is_incus_not_found(exc):
                        try:
                            self._unstage_share_mount_locked(
                                instance, share_mapping,
                                operation_token=owner_token,
                                mount_table=mount_table)
                        except Exception:
                            LOG.critical(
                                'Failed to roll back a Manila mount after '
                                'the Incus profile was not found',
                                instance=instance, exc_info=True)
                        raise exception.ShareMountError(
                            share_id=share_mapping.share_id,
                            server_id=instance.uuid,
                            reason='Incus instance profile does not exist')
                    raise
                added_devices = self._attach_share_devices(
                    profile, instance, [share_mapping],
                    operation_token=owner_token)
        except Exception as exc:
            # A failed profile save is not proof that Incus rejected the
            # update. Keep the mount and its owner journal so a retry can
            # inspect durable profile state without exposing an absent source
            # through a possibly committed disk device.
            LOG.error(
                'Retaining the Manila mount journal after profile attach '
                'failed', instance=instance)
            if isinstance(
                    exc, (exception.ShareMountError,
                          exception.ShareProtocolNotSupported)):
                raise
            raise exception.ShareMountError(
                share_id=share_mapping.share_id,
                server_id=instance.uuid,
                reason=exc)
        return bool(mounted_now or added_devices)

    @_invalidates_instance_inventory
    def umount_share(self, context, instance, share_mapping):
        """Remove the Incus share device before unmounting the host export."""
        with lockutils.lock(
                _share_operation_lock_name(
                    instance, share_mapping.share_id)):
            return self._umount_share_locked(
                context, instance, share_mapping)

    @_invalidates_instance_inventory
    def umount_share_transaction(
            self, context, instance, share_mapping, mount_table):
        """Unmount one member and update the shared mount snapshot."""
        with lockutils.lock(
                _share_operation_lock_name(
                    instance, share_mapping.share_id)):
            return self._umount_share_locked(
                context, instance, share_mapping,
                mount_table=mount_table)

    def _unstage_share_mount_locked(
            self, instance, share_mapping, operation_token=None,
            mount_table=None):
        mount_path = _share_mount_path(instance, share_mapping)
        try:
            journal = _read_share_journal(
                instance, share_mapping, operation_token=operation_token)
            real_mount_path = os.path.realpath(mount_path)
            mounted = (
                real_mount_path in mount_table
                if mount_table is not None else os.path.ismount(mount_path))
            if (operation_token is not None and journal is None and mounted):
                raise exception.ShareUmountError(
                    share_id=share_mapping.share_id,
                    server_id=instance.uuid,
                    reason='host Manila mount has no matching owner journal')
            if journal is not None:
                _write_share_journal(
                    instance, share_mapping,
                    journal['operation_token'], 'unmounting')
            if mounted:
                _validate_existing_share_mount(
                    mount_path, share_mapping, mount_table=mount_table)
                incus_privsep.umount(
                    mount_path, CONF.incus.share_unmount_timeout)
                if mount_table is not None:
                    mount_table.pop(real_mount_path, None)
            if os.path.isdir(mount_path):
                os.rmdir(mount_path)
            instance_share_dir = os.path.dirname(mount_path)
            if os.path.isdir(instance_share_dir):
                try:
                    os.rmdir(instance_share_dir)
                except OSError as exc:
                    # Sibling share mappings keep their common parent.
                    if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                        raise
            if journal is not None:
                _remove_share_journal(
                    instance, share_mapping.share_id,
                    journal['operation_token'])
        except exception.ShareUmountError:
            raise
        except Exception as exc:
            raise exception.ShareUmountError(
                share_id=share_mapping.share_id,
                server_id=instance.uuid,
                reason=exc)

    def unstage_share_for_live_migration(
            self, context, instance, share_mapping, operation_token,
            mount_table=None):
        """Undo a destination-only mount without changing Nova share state."""
        with lockutils.lock(
                _share_operation_lock_name(
                    instance, share_mapping.share_id)):
            self._unstage_share_mount_locked(
                instance, share_mapping,
                operation_token=operation_token,
                mount_table=mount_table)

    def unstage_share_for_cold_migration(
            self, context, instance, share_mapping, operation_token,
            mount_table=None):
        """Undo only the cold-migration mount owned by this attempt."""
        with lockutils.lock(
                _share_operation_lock_name(
                    instance, share_mapping.share_id)):
            self._unstage_share_mount_locked(
                instance, share_mapping,
                operation_token=operation_token,
                mount_table=mount_table)

    def _umount_share_locked(
            self, context, instance, share_mapping, mount_table=None):
        if not CONF.incus.enable_manila_shares:
            raise exception.ShareProtocolNotSupported(
                share_proto=share_mapping.share_proto)
        owner_token = _share_mapping_owner_token(instance, share_mapping)
        journal = _read_share_journal(
            instance, share_mapping, operation_token=owner_token)
        owned_mapping = (
            _share_mapping_from_journal(instance, journal)
            if journal is not None else share_mapping)
        _write_share_journal(
            instance, owned_mapping, owner_token, 'unmounting')
        with lockutils.lock(_profile_lock_name(instance)):
            try:
                profile = self.client.profiles.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if _is_incus_not_found(exc):
                    profile = None
                else:
                    raise

            if profile is not None:
                device_name = _share_device_name(share_mapping)
                if device_name in profile.devices:
                    profile.devices.pop(device_name)
                    try:
                        profile.save(wait=True)
                    except Exception:
                        try:
                            persisted = self.client.profiles.get(instance.name)
                        except Exception:
                            raise
                        if device_name in persisted.devices:
                            raise
                        LOG.warning(
                            'Incus reported a failed Manila detach profile '
                            'update, but the share device is absent; '
                            'continuing host unmount',
                            instance=instance)

        self._unstage_share_mount_locked(
            instance, owned_mapping, operation_token=owner_token,
            mount_table=mount_table)
        return False

    def _get_host_resource_snapshot(self, nodename, use_cache=False):
        """Collect host totals, reusing the current ResourceTracker cycle."""
        now = time.monotonic()
        if use_cache:
            with self._host_resource_cache_lock:
                cached = self._host_resource_cache.pop(nodename, None)
            if cached and now - cached[0] <= _HOST_RESOURCE_CACHE_TTL:
                return copy.deepcopy(cached[1])

        cpuinfo = _get_cpu_info()
        cpu_info = {
            'arch': platform.machine(),
            'features': cpuinfo.get('flags', 'unknown'),
            'model': cpuinfo.get('model name', 'unknown'),
            'topology': {
                'sockets': cpuinfo['socket(s)'],
                'cores': cpuinfo['core(s) per socket'],
                'threads': cpuinfo['thread(s) per core'],
            },
            'vendor': cpuinfo.get('vendor id', 'unknown'),
        }

        cpu_topology = cpu_info['topology']
        vcpus = (int(cpu_topology['cores']) *
                 int(cpu_topology['sockets']) *
                 int(cpu_topology['threads']))

        local_memory_info = _get_ram_usage()
        incus_config = self.client.host_info
        storage_driver = incus_config['environment']['storage']
        default_storage_pool_available = True
        if CONF.incus.storage_pool:
            try:
                default_pool = self.client.storage_pools.get(
                    CONF.incus.storage_pool)
                status = str(
                    getattr(default_pool, 'status', '')).casefold()
                if status != 'created':
                    default_storage_pool_available = False
                    local_disk_info = {'total': 0, 'used': 0}
                    LOG.error(
                        'Default Incus root storage pool %(pool)s is in '
                        'state %(status)s; suppressing new instance '
                        'scheduling',
                        {'pool': CONF.incus.storage_pool,
                         'status': getattr(default_pool, 'status', None)})
                else:
                    local_disk_info = _placement_storage_pool_info(
                        self.client,
                        CONF.incus.storage_pool,
                        CONF.incus.shared_storage_pool_capacity_gb,
                        pool=default_pool)
            except (incus_exceptions.LXDAPIException,
                    incus_exceptions.ClientConnectionFailed) as exc:
                default_storage_pool_available = False
                local_disk_info = {'total': 0, 'used': 0}
                LOG.error(
                    'Default Incus root storage pool %(pool)s cannot report '
                    'capacity; suppressing new instance scheduling: '
                    '%(error)s',
                    {'pool': CONF.incus.storage_pool, 'error': exc})
        elif storage_driver == 'zfs':
            # NOTE(ajkavanagh) - BUG/1782329 - this is temporary until storage
            # pools is implemented.  Incus 3 removed the storage.zfs_pool_name
            # key from the config.  So, if it fails, we need to grab the
            # configured storage pool and use that as the name instead.
            try:
                pool_name = incus_config['config']['storage.zfs_pool_name']
            except KeyError:
                pool_name = CONF.incus.storage_pool
            local_disk_info = _get_zpool_info(pool_name)
        else:
            local_disk_info = _get_fs_info(CONF.incus.root_dir)

        snapshot = {
            'cpu_info': cpu_info,
            'hypervisor_version': _incus_hypervisor_version(incus_config),
            'local_gb': local_disk_info['total'] // units.Gi,
            'local_gb_used': local_disk_info['used'] // units.Gi,
            'disk_available_least': local_disk_info['available'] // units.Gi,
            'memory_mb': local_memory_info['total'] // units.Mi,
            'memory_mb_used': local_memory_info['used'] // units.Mi,
            'vcpus': vcpus,
            'default_storage_pool_available':
                default_storage_pool_available,
        }
        with self._host_resource_cache_lock:
            self._host_resource_cache[nodename] = (
                time.monotonic(), copy.deepcopy(snapshot))
        return snapshot

    def get_available_resource(self, nodename):
        """Aggregate all available system resources.

        See `nova.virt.driver.ComputeDriver.get_available_resource`
        for more information.
        """
        resources = self._get_host_resource_snapshot(nodename)
        supported_instances = [
            (obj_fields.Architecture.I686, obj_fields.HVType.LXD,
             obj_fields.VMMode.EXE),
            (obj_fields.Architecture.X86_64, obj_fields.HVType.LXD,
             obj_fields.VMMode.EXE),
        ]

        data = {
            'vcpus': resources['vcpus'],
            'memory_mb': resources['memory_mb'],
            'memory_mb_used': resources['memory_mb_used'],
            'local_gb': resources['local_gb'],
            'local_gb_used': resources['local_gb_used'],
            # Left unset, this reads as zero free space in
            # "hypervisor show", which is the one place an operator looks
            # for it. The pool already measures what is actually free.
            'disk_available_least': resources['disk_available_least'],
            # vcpus_used counts what instances were promised, while the
            # two used figures above are what the host is really
            # consuming. The asymmetry is Nova's own field semantics -
            # libvirt reports the same way - not an accounting mistake.
            'vcpus_used': self._get_vcpus_used(),
            # Nova's HVType enum has no incus value, so this reports the
            # closest one it accepts. Changing it needs an enum addition
            # upstream, not a change here.
            'hypervisor_type': obj_fields.HVType.LXD,
            'hypervisor_version': resources['hypervisor_version'],
            'cpu_info': jsonutils.dumps(resources['cpu_info']),
            'hypervisor_hostname': CONF.host,
            'supported_instances': supported_instances,
            'numa_topology': None,
        }

        return data

    def _get_vcpus_used(self):
        """Return vCPUs assigned to Nova-owned Incus instance records."""
        used = 0
        try:
            containers = self._get_instance_inventory_snapshot().values()
        except Exception:
            # Reporting zero here would make a failing host look empty and
            # attract the scheduler exactly when Incus is unreachable.
            # Raising keeps the resource tracker on its last known view,
            # matching the fail-closed storage-pool availability handling.
            LOG.exception('Failed to audit Incus vCPU usage')
            raise

        for container in containers:
            config = getattr(container, 'expanded_config', None)
            if config is None:
                config = getattr(container, 'config', {})
            if not config.get('user.openstack.uuid'):
                continue
            value = config.get('limits.cpu')
            if value is None:
                LOG.warning(
                    'Nova-owned Incus instance %s has no limits.cpu',
                    container.name)
                continue
            try:
                used += int(value)
            except (TypeError, ValueError):
                LOG.warning(
                    'Cannot account non-numeric limits.cpu=%r for Incus '
                    'instance %s', value, container.name)
        return used

    def set_admin_password(self, instance, new_pass):
        """Set the image-declared admin account password through Incus exec."""
        if not isinstance(new_pass, str) or any(
                character in new_pass for character in ('\x00', '\r', '\n')):
            raise exception.InstancePasswordSetFailed(
                instance=instance.uuid,
                reason='password contains an unsupported control character')

        image_meta = getattr(instance, 'image_meta', None)
        properties = (
            getattr(image_meta, 'properties', {}) if image_meta else {})
        username = properties.get('os_admin_user') or 'root'
        if (not isinstance(username, str) or not username or
                any(character in username
                    for character in ('\x00', '\r', '\n', ':'))):
            raise exception.SetAdminPasswdNotSupported()

        container = self.client.instances.get(instance.name)
        result = container.execute(
            ['chpasswd'],
            stdin_payload='%s:%s\n' % (username, new_pass),
            user=0,
            group=0)
        if result.exit_code == 127:
            raise exception.SetAdminPasswdNotSupported()
        if result.exit_code != 0:
            raise exception.InstancePasswordSetFailed(
                instance=instance.uuid,
                reason='guest password utility failed with exit code %d' %
                       result.exit_code)

    def update_provider_tree(self, provider_tree, nodename, allocations=None):
        """Report Incus compute and managed rootfs capacity to Placement."""
        resources = self._get_host_resource_snapshot(
            nodename, use_cache=True)
        current = provider_tree.data(nodename)
        ratios = self._get_allocation_ratios(current.inventory)
        inventory = {
            orc.VCPU: {
                'total': resources['vcpus'],
                'min_unit': 1,
                'max_unit': resources['vcpus'],
                'step_size': 1,
                'allocation_ratio': ratios[orc.VCPU],
                'reserved': CONF.reserved_host_cpus,
            },
            orc.MEMORY_MB: {
                'total': resources['memory_mb'],
                'min_unit': 1,
                'max_unit': resources['memory_mb'],
                'step_size': 1,
                'allocation_ratio': ratios[orc.MEMORY_MB],
                'reserved': CONF.reserved_host_memory_mb,
            },
        }
        default_storage_pool_available = resources.get(
            'default_storage_pool_available', True)
        if default_storage_pool_available:
            inventory[orc.DISK_GB] = {
                'total': resources['local_gb'],
                'min_unit': 1,
                'max_unit': resources['local_gb'],
                'step_size': 1,
                'allocation_ratio': ratios[orc.DISK_GB],
                'reserved': self._get_reserved_host_disk_gb_from_config(),
            }
        else:
            quiesced = _quiesced_inventory(current.inventory, orc.DISK_GB)
            if quiesced is not None:
                inventory[orc.DISK_GB] = quiesced
        root_pool_traits = _root_storage_pool_traits()
        available_root_pool_traits = set(root_pool_traits)
        root_pools = {}
        for selector, pool_name in CONF.incus.root_storage_pools.items():
            trait = common.root_storage_pool_trait(selector)
            try:
                pool = self.client.storage_pools.get(pool_name)
            except (incus_exceptions.LXDAPIException,
                    incus_exceptions.ClientConnectionFailed) as exc:
                available_root_pool_traits.discard(trait)
                LOG.error(
                    'Configured Incus root storage selector %(selector)s '
                    'cannot access pool %(pool)s; suppressing its Placement '
                    'trait: %(error)s',
                    {'selector': selector, 'pool': pool_name, 'error': exc})
                continue
            root_pools[selector] = pool
            if str(getattr(pool, 'status', '')).casefold() != 'created':
                available_root_pool_traits.discard(trait)
                LOG.error(
                    'Configured Incus root storage selector %(selector)s '
                    'uses pool %(pool)s in state %(status)s; suppressing its '
                    'Placement trait',
                    {'selector': selector, 'pool': pool_name,
                     'status': getattr(pool, 'status', None)})

        for selector, resource_class in (
                CONF.incus.root_storage_pool_resource_classes.items()):
            pool_name = CONF.incus.root_storage_pools.get(selector)
            if not pool_name or not resource_class.startswith('CUSTOM_'):
                # Static misconfiguration is rejected at init_host; raising
                # here on every resource-tracker cycle would freeze the
                # Placement inventory while the service still reported up.
                LOG.error(
                    'Invalid Incus root storage capacity configuration for '
                    'selector %(selector)s; skipping its resource class',
                    {'selector': selector})
                continue
            pool = root_pools.get(selector)
            if pool is None or common.root_storage_pool_trait(
                    selector) not in available_root_pool_traits:
                quiesced = _quiesced_inventory(
                    current.inventory, resource_class)
                if quiesced is not None:
                    inventory[resource_class] = quiesced
                continue
            try:
                pool_info = _placement_storage_pool_info(
                    self.client,
                    pool_name,
                    CONF.incus.shared_root_storage_pool_capacities_gb.get(
                        selector),
                    pool=pool)
            except (incus_exceptions.LXDAPIException,
                    incus_exceptions.ClientConnectionFailed) as exc:
                available_root_pool_traits.discard(
                    common.root_storage_pool_trait(selector))
                quiesced = _quiesced_inventory(
                    current.inventory, resource_class)
                if quiesced is not None:
                    inventory[resource_class] = quiesced
                LOG.error(
                    'Configured Incus root storage selector %(selector)s '
                    'cannot report capacity for pool %(pool)s; preserving '
                    'its existing inventory and suppressing its Placement '
                    'trait: %(error)s',
                    {'selector': selector, 'pool': pool_name, 'error': exc})
                continue
            total_gb = pool_info['total'] // units.Gi
            if total_gb < 1:
                # Runtime data, not static misconfiguration: a pool can
                # report nothing while it is being resized or while its
                # backend is degraded. Raising here would freeze the whole
                # Placement inventory at its previous values while the
                # service still reported up, which is the failure this
                # method exists to avoid. Degrade exactly like the
                # unreachable-pool path above.
                available_root_pool_traits.discard(
                    common.root_storage_pool_trait(selector))
                quiesced = _quiesced_inventory(
                    current.inventory, resource_class)
                if quiesced is not None:
                    inventory[resource_class] = quiesced
                LOG.error(
                    'Incus root storage pool %(pool)s reports no usable '
                    'capacity for selector %(selector)s; preserving its '
                    'existing inventory and suppressing its Placement trait',
                    {'pool': pool_name, 'selector': selector})
                continue
            inventory[resource_class] = {
                'total': total_gb,
                'min_unit': 1,
                'max_unit': total_gb,
                'step_size': 1,
                'allocation_ratio': 1.0,
                'reserved': 0,
            }
        provider_tree.update_inventory(nodename, inventory)
        traits = set(current.traits)
        if default_storage_pool_available:
            traits.add(INCUS_SYSTEM_CONTAINER_TRAIT)
        else:
            traits.discard(INCUS_SYSTEM_CONTAINER_TRAIT)
        managed_pool_traits = {
            trait for trait in traits
            if trait.startswith(common.INCUS_STORAGE_POOL_TRAIT_PREFIX)
        }
        traits.difference_update(managed_pool_traits)
        traits.update(available_root_pool_traits)
        if CONF.incus.allow_instance_swap and _host_has_swap():
            traits.add(INCUS_SWAP_TRAIT)
        else:
            traits.discard(INCUS_SWAP_TRAIT)
        if CONF.incus.enable_manila_shares:
            traits.add(INCUS_MANILA_SHARE_TRAIT)
            if CONF.incus.allow_live_migration:
                traits.add(INCUS_MANILA_LIVE_MIGRATION_TRAIT)
            else:
                traits.discard(INCUS_MANILA_LIVE_MIGRATION_TRAIT)
            if CONF.incus.allow_cold_migration:
                traits.add(INCUS_MANILA_COLD_MIGRATION_TRAIT)
            else:
                traits.discard(INCUS_MANILA_COLD_MIGRATION_TRAIT)
        else:
            traits.discard(INCUS_MANILA_SHARE_TRAIT)
            traits.discard(INCUS_MANILA_LIVE_MIGRATION_TRAIT)
            traits.discard(INCUS_MANILA_COLD_MIGRATION_TRAIT)
        provider_tree.update_traits(nodename, traits)

    def refresh_instance_security_rules(self, instance):
        return self.firewall_driver.refresh_instance_security_rules(
            instance)

    def ensure_filtering_rules_for_instance(self, instance, network_info):
        return self.firewall_driver.ensure_filtering_rules_for_instance(
            instance, network_info)

    def filter_defer_apply_on(self):
        return self.firewall_driver.filter_defer_apply_on()

    def filter_defer_apply_off(self):
        return self.firewall_driver.filter_defer_apply_off()

    def unfilter_instance(self, instance, network_info):
        return self.firewall_driver.unfilter_instance(
            instance, network_info)

    def get_host_uptime(self):
        out, err = processutils.execute('env', 'LANG=C', 'uptime')
        return out

    def plug_vifs(self, instance, network_info):
        plugged = []
        try:
            for vif in network_info or []:
                self.vif_driver.plug(instance, vif)
                plugged.append(vif)
        except Exception:
            with excutils.save_and_reraise_exception():
                self._cleanup_vifs_best_effort(
                    instance, reversed(plugged))

    def _unplug_every_vif(self, instance, network_info, description):
        """Unplug all VIFs and report what failed, stopping for nothing.

        Aborting on the first failure leaked every remaining interface of
        a multi-NIC instance, and each host device left behind stays
        until someone removes it by hand. Both callers need that
        guarantee, so it lives here rather than in each of them: the
        driver API re-raises the first failure, the rollback path
        aggregates them all.

        :returns: list of ``(vif_id, exception)`` in the order attempted.
        """
        failures = []
        for vif in list(network_info or []):
            vif_id = vif.get('id', 'unknown')
            try:
                self.vif_driver.unplug(instance, vif)
            except Exception as exc:
                failures.append((vif_id, exc))
                LOG.exception(
                    'Failed to unplug %(description)s %(vif)s; continuing '
                    'with the rest',
                    {'description': description, 'vif': vif_id},
                    instance=instance)
        return failures

    def _cleanup_vifs_best_effort(
            self, instance, network_info, remove_firewall=False):
        """Attempt every target VIF cleanup and report all failures."""
        failures = [
            ('unplug destination VIF %s' % vif_id, exc)
            for vif_id, exc in self._unplug_every_vif(
                instance, network_info, 'destination VIF')
        ]
        if not remove_firewall:
            return failures
        for vif in list(network_info or []):
            vif_id = vif.get('id', 'unknown')
            try:
                self.firewall_driver.unfilter_instance(instance, [vif])
            except Exception as exc:
                failures.append(
                    ('remove firewall filter for VIF %s' % vif_id, exc))
                LOG.exception(
                    'Failed to remove firewall filter for VIF %s',
                    vif_id, instance=instance)
        return failures

    def unplug_vifs(self, instance, network_info):
        # Every VIF is attempted; the first failure is re-raised afterwards
        # so this still reports failure to Nova as the driver API requires.
        failures = self._unplug_every_vif(instance, network_info, 'VIF')
        if failures:
            raise failures[0][1]

    def get_host_cpu_stats(self):
        return {
            'kernel': int(psutil.cpu_times()[2]),
            'idle': int(psutil.cpu_times()[3]),
            'user': int(psutil.cpu_times()[0]),
            'iowait': int(psutil.cpu_times()[4]),
            'frequency': _get_cpu_info().get('cpu mhz', 0)
        }

    def get_volume_connector(self, instance):
        return brick_get_connector_properties()

    def get_available_nodes(self, refresh=False):
        return [CONF.host]

    def check_instance_shared_storage_local(self, context, instance):
        """Return the destination root identity for Nova resize rollback."""
        try:
            identity = _instance_storage_identity(
                self.client, instance.name)
            identity['instance_name'] = instance.name
            return identity
        except incus_exceptions.LXDAPIException as exc:
            if _is_incus_not_found(exc):
                return {'shared': False}
            raise

    def check_instance_shared_storage_remote(self, context, data):
        """Require the retained source to reference the same shared pool."""
        if not isinstance(data, dict) or not data.get('shared'):
            return False
        try:
            local = _instance_storage_identity(
                self.client, data.get('instance_name') or '')
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
            return False
        except exception.InvalidConfiguration:
            # Older callers do not include instance_name. The Nova contract
            # supplies only opaque data, so include the name explicitly below.
            return False
        return local == {
            key: data.get(key)
            for key in ('shared', 'driver', 'cluster', 'source')
        }

    # XXX: rockstar (5 July 2016) - The methods and code below this line
    # have not been through the cleanup process. We know the cleanup process
    # is complete when there is no more code below this comment, and the
    # comment can be removed.

    #
    # ComputeDriver implementation methods
    #
    def _rollback_failed_finish_migration(
            self, context, instance, network_info, configdrive_staging,
            attempt, cleanup_token, idmap_base, idmap_size, container,
            create_completed, profile, attached, plugged, power_on,
            materialization):
        """Fence or retain a failed cold-migration destination."""
        if configdrive_staging:
            shutil.rmtree(configdrive_staging, ignore_errors=True)

        target_ownership_uncertain = False

        def delete_aborted_target():
            nonlocal container
            self._delete_migration_target_with_idmap(
                self.client, instance)
            container = None

        try:
            attempt = _abort_migration_attempt(
                self.client, instance, cleanup_token,
                idmap_base, idmap_size,
                target_cleanup=delete_aborted_target)
        except Exception:
            target_ownership_uncertain = True
            LOG.critical(
                'Cannot fence the destination migration attempt; '
                'retaining every target resource',
                instance=instance, exc_info=True)
        else:
            target_ownership_uncertain = attempt['state'] == 'committed'

        if container is None:
            try:
                container = self.client.instances.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if not _is_incus_not_found(exc):
                    target_ownership_uncertain = True
                    LOG.exception(
                        'Cannot determine whether the migration target '
                        'exists after destination failure',
                        instance=instance)
            except Exception:
                target_ownership_uncertain = True
                LOG.exception(
                    'Cannot determine whether the migration target exists '
                    'after destination failure',
                    instance=instance)

        target_exists = container is not None
        target_receive_complete = (
            target_exists and (
                create_completed or
                _instance_migration_receive_complete(container)))
        retain_target = (
            target_exists or target_ownership_uncertain)
        recovery_marked = False
        if retain_target:
            if target_receive_complete and profile is not None:
                try:
                    _retry_migration_finish_action(
                        lambda: self._mark_migration_recovery_required(
                            instance, power_on=power_on),
                        'durable recovery marker', instance)
                    recovery_marked = True
                except Exception:
                    LOG.exception(
                        'Failed to mark migration target for automatic '
                        'recovery',
                        instance=instance)
            LOG.error(
                'Retaining the migration target after finish_migration '
                'failed; receive_complete=%s. Repair the reported error '
                'before retrying or removing the protected target',
                target_receive_complete, instance=instance)

        if not retain_target:
            try:
                if materialization is not None:
                    assignment, claim = self._exact_idmap_host_claim(
                        instance, materialization.claim)
                    if claim is not None and claim.state != 'cleaned':
                        if claim.state == 'unmaterialized':
                            self._abort_idmap_materialization(
                                materialization)
                        else:
                            if claim.state == 'possible':
                                promote = (
                                    self.
                                    _promote_idmap_claim_if_server_committed)
                                assignment, claim = promote(instance, claim)
                            self._settle_idmap_host_claim(
                                instance, claim,
                                final_delete=(
                                    claim.state == 'committed'))
                # This is the only rollback transaction which may remove the
                # profile. It replays every profile/host Cinder and Manila
                # journal, unwires VIFs, and marks the profile for periodic
                # recovery if any step fails.
                self._cleanup(
                    context, instance, network_info,
                    block_device_info=None,
                    destroy_vifs=plugged, delete_profile=True)
                self._retire_instance_idmap_claim_if_clean(instance)
            except Exception:
                retain_target = True
                LOG.exception(
                    'Failed to roll back cold-migration destination '
                    'resources; retained the cleanup profile for recovery',
                    instance=instance)
        if (not retain_target and attempt is not None and
                attempt.get('finished')):
            try:
                _retire_migration_attempt(
                    self.client, instance, cleanup_token,
                    idmap_base, idmap_size)
            except Exception:
                LOG.exception(
                    'Failed to retire rolled-back migration attempt',
                    instance=instance)

        # A durably marked, definitely received target can continue through
        # Nova finish_resize; the periodic manager repairs runtime state
        # without auto-confirming the resize.
        return (
            recovery_marked and
            CONF.incus.migration_auto_recovery
        )

    @_invalidates_instance_inventory
    def finish_migration(self, context, migration, instance, disk_info,
                         network_info, image_meta, resize_instance,
                         allocations, block_device_info=None, power_on=True):
        if (getattr(migration, 'source_compute', None) and
                migration.source_compute ==
                getattr(migration, 'dest_compute', None)):
            raise exception.UnableToMigrateToSelf(
                instance_id=instance.uuid, host=migration.dest_compute)
        if not CONF.incus.allow_cold_migration:
            raise exception.MigrationError(
                reason='Incus cold migration is disabled by configuration')

        (
            transfer, migration_data, cleanup_token,
            idmap_base, idmap_size, expected_share_ids,
        ) = _parse_cold_migration_transfer(disk_info)
        _bind_migration_instance_local_owner(migration_data, instance)
        try:
            self._ensure_instance_idmap(
                instance, observed_base=idmap_base,
                observed_size=idmap_size)
        except incus_idmap.IDMapError as exc:
            raise exception.MigrationError(
                reason='Incus destination cannot verify the global idmap: '
                       '{}'.format(exc)) from exc

        root_bdm = _boot_from_volume(block_device_info)
        if bool(root_bdm) != bool(transfer.get('boot_from_volume')):
            raise exception.MigrationError(
                reason='Source and destination disagree on Incus BFV '
                'migration mode')
        root_volume = None
        if root_bdm:
            root_volume = _require_bfv_migration_support(
                self.client, root_bdm)
            root_device = _bfv_root_device(
                instance, root_bdm, root_volume)
        else:
            root_device = flavor._root(
                instance, self.client, network_info,
                block_device_info)['root']
        try:
            materialization = self._begin_idmap_materialization(
                instance, cleanup_token, root_device,
                observed_base=idmap_base, observed_size=idmap_size,
                shared_migration=True)
        except incus_idmap.IDMapError as exc:
            raise exception.MigrationError(
                reason='Cannot register the cold migration root '
                       'materialization: {}'.format(exc)) from exc
        try:
            attempt = _register_migration_attempt(
                self.client, instance, cleanup_token,
                idmap_base, idmap_size)
        except Exception:
            with excutils.save_and_reraise_exception():
                self._abort_idmap_materialization(materialization)
                self._retire_instance_idmap_claim_if_clean(instance)
        try:
            configdrive_staging = _prepare_configdrive_migration(
                instance, transfer)
            journaled_shares = _journaled_share_mappings(
                instance, cleanup_token,
                expected_share_ids=expected_share_ids)
        except Exception:
            with excutils.save_and_reraise_exception():
                attempt = _abort_migration_attempt(
                    self.client, instance, cleanup_token,
                    idmap_base, idmap_size)
                if attempt['state'] != 'committed':
                    _retire_migration_attempt(
                        self.client, instance, cleanup_token,
                        idmap_base, idmap_size)
                    self._abort_idmap_materialization(materialization)
                    self._retire_instance_idmap_claim_if_clean(instance)

        profile = None
        container = None
        create_completed = False
        target_operation_id = None
        attached = []
        plugged = False
        try:
            # The profile is the durable cleanup owner for target-side VIFs.
            # Create and identify it before a multi-VIF plug can partially
            # succeed, otherwise a failed rollback has no periodic candidate.
            profile_config = {
                MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
                MIGRATION_DESTINATION_PREPARED_KEY: cleanup_token,
                MIGRATION_NOVA_UUID_KEY: migration.uuid,
                'security.idmap.base': str(idmap_base),
                'security.idmap.size': str(idmap_size),
            }
            if materialization is not None:
                profile_config.update(self._idmap_materialization_config(
                    materialization.binding))
            device_overrides = (
                {'root': root_device} if root_volume else None)
            with lockutils.lock(_profile_lock_name(instance)):
                profile = flavor.to_profile(
                    self.client, instance, network_info, block_device_info,
                    config_overrides=profile_config,
                    device_overrides=device_overrides)
            # Test doubles and older SDK resource wrappers may not echo the
            # create payload. Keep the local object coherent without a second
            # API write; the durable profile was already created atomically.
            if isinstance(profile.config, dict):
                profile.config.update(profile_config)
                profile.config.pop(MIGRATION_CLEANUP_COMPLETE_KEY, None)
            if root_volume and isinstance(profile.devices, dict):
                profile.devices['root'] = root_device

            # Set this before the first plug attempt. plug_vifs() rolls back
            # every successfully plugged VIF once, and _cleanup() performs a
            # second per-VIF pass while retaining the profile on any failure.
            plugged = bool(network_info)
            _retry_migration_finish_action(
                lambda: self.plug_vifs(instance, network_info),
                'VIF wiring', instance)

            def record_target_operation(operation_id):
                nonlocal target_operation_id
                target_operation_id = operation_id
                with lockutils.lock(_profile_lock_name(instance)):
                    current = self.client.profiles.get(instance.name)
                    current.config[MIGRATION_TARGET_OPERATION_KEY] = (
                        operation_id)
                    current.save(wait=True)

            container, target_operation_id = (
                self._with_rootfs_materialization_barrier(
                    materialization,
                    migration_data.setdefault('config', {}),
                    lambda: _create_migration_target(
                        self.client, migration_data, instance, cleanup_token,
                        idmap_base, idmap_size,
                        operation_started=record_target_operation),
                    recover_action=lambda: _create_migration_target(
                        self.client, migration_data, instance, cleanup_token,
                        idmap_base, idmap_size,
                        operation_started=record_target_operation)))
            create_completed = True
            if configdrive_staging:
                target_container = self.client.instances.get(instance.name)
                configdrive_path = _commit_staged_configdrive(
                    instance, target_container, configdrive_staging)
                configdrive_staging = None
                with lockutils.lock(_profile_lock_name(instance)):
                    profile = self.client.profiles.get(instance.name)
                    profile.devices['configdrive'] = {
                        'path': '/config-drive',
                        'source': configdrive_path,
                        'type': 'disk',
                        'readonly': 'true',
                    }
                    profile.save(wait=True)

            for bdm in _reboot_data_volume_bdms(
                    block_device_info,
                    root_device_name=instance.root_device_name):
                connection_info = bdm.get('connection_info')
                mountpoint = bdm.get('mount_device')
                if connection_info and mountpoint:
                    _retry_migration_finish_action(
                        lambda connection_info=connection_info,
                        mountpoint=mountpoint, bdm=bdm:
                        self._attach_volume_for_operation(
                            context, connection_info, instance, mountpoint,
                            _bdm_attachment_id(bdm), 'migration',
                            cleanup_token, 'cold-target',
                            operation_migration_uuid=migration.uuid),
                        'data-volume attachment', instance)
                    attached.append((connection_info, mountpoint))

            self._attach_cold_migration_share_devices(
                profile, instance, journaled_shares, cleanup_token)

            if power_on:
                _retry_migration_finish_action(
                    lambda: self._start_instance_with_idmap(
                        instance, container),
                    'container start', instance)
        except Exception:
            with excutils.save_and_reraise_exception() as error_context:
                recovered = self._rollback_failed_finish_migration(
                    context, instance, network_info, configdrive_staging,
                    attempt, cleanup_token, idmap_base, idmap_size,
                    container, create_completed, profile, attached, plugged,
                    power_on, materialization)
                if recovered:
                    error_context.reraise = False

    @_invalidates_instance_inventory
    def confirm_migration(self, context, migration, instance, network_info):
        unused_intent, unused_assignment, source_claim = (
            self._idmap_rootfs_release_context(instance))
        try:
            profile = self.client.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if _is_incus_not_found(exc):
                profile = None
            else:
                raise
        try:
            source_container = self.client.instances.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if _is_incus_not_found(exc):
                source_container = None
            else:
                raise
        if source_container is None:
            # Source deletion is the irreversible half of shared-storage
            # handover. A process can die before committing target ownership;
            # converge the target (or durably queue recovery) instead of
            # treating source absence alone as completion proof.
            destination_address = None
            cleanup_complete = profile is None
            if profile is not None:
                profile_config = (
                    profile.config if isinstance(profile.config, dict) else {})
                if (
                    profile_config.get('environment.product_name') !=
                    'OpenStack Nova' or
                    profile_config.get('user.openstack.uuid') != instance.uuid
                ):
                    raise exception.MigrationError(
                        reason='Cold migration source cleanup profile is not '
                               'owned by the Nova instance')
                destination_address = profile_config.get(
                    MIGRATION_DESTINATION_KEY)
            remote = _migration_client(
                destination_address or
                _migration_address_for_host(migration.dest_compute))
            _converge_migration_target_ownership(remote, instance)
            if profile is not None:
                # Ownership is already decided. Finish any source os-brick,
                # Manila, VIF and profile cleanup left by the interrupted
                # confirm transaction.
                try:
                    self._cleanup(
                        context, instance, network_info,
                        destroy_vifs=True, delete_profile=True)
                except Exception:
                    LOG.critical(
                        'Retried cold migration confirm retained source '
                        'cleanup work for periodic repair',
                        instance=instance, exc_info=True)
                else:
                    cleanup_complete = True
            if cleanup_complete:
                self._retire_instance_idmap_claim_if_clean(instance)
            return
        if profile is None:
            raise exception.MigrationError(
                reason='Cold migration source record and profile disagree')

        cleanup_token = profile.config.get(MIGRATION_CLEANUP_TOKEN_KEY)
        if not uuidutils.is_uuid_like(cleanup_token):
            raise exception.MigrationError(
                reason='Cold migration confirm has no valid attempt token')
        idmap_base, idmap_size = _instance_migration_idmap(
            source_container, profile)
        destination_address = None
        configured_address = profile.config.get(MIGRATION_DESTINATION_KEY)
        if isinstance(configured_address, str) and configured_address:
            destination_address = configured_address
        if not destination_address:
            destination_address = _migration_address_for_host(
                migration.dest_compute)
        operation_id = profile.config.get(MIGRATION_OPERATION_KEY)

        _settle_instance_migration_operations(
            self.client, instance, operation_ids=(operation_id,))
        remote = _migration_client(destination_address)
        attempt = _get_migration_attempt(
            remote, instance, cleanup_token, idmap_base, idmap_size)
        if attempt['state'] != 'committed' or not attempt.get('finished'):
            raise exception.MigrationError(
                reason='Refusing to confirm cold migration before the '
                       'destination attempt is committed')

        # The source record deletion below is irreversible. Persist the
        # os-brick/Manila/VIF cleanup transaction first so a compute-process
        # restart cannot strand host mappings without a periodic candidate.
        self._mark_cleanup_recovery_required(instance)

        def delete_source_record():
            try:
                container = self.client.instances.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if _is_incus_not_found(exc):
                    if source_claim is not None:
                        self._settle_idmap_host_claim(
                            instance, source_claim, final_delete=True)
                    return
                raise
            _set_storage_handover_state(
                self.client, instance.name, 'protected',
                container=container)
            if container.status != 'Stopped':
                container.stop(wait=True)
            if source_claim is None:
                container.delete(wait=True)
            else:
                self._delete_instance_with_rootfs_release_receipt(
                    container, instance, source_claim)

        _retry_migration_finish_action(
            delete_source_record, 'confirmed source record deletion',
            instance)

        # The receiving ordinary-Ceph record stays deletion-protected until
        # the old source record is gone. Only then may it become authoritative
        # for a future user-requested delete.
        destination = remote.instances.get(instance.name)
        if _instance_nova_uuid(destination) != instance.uuid:
            raise exception.MigrationError(
                reason='Incus migration destination UUID does not match '
                       'the Nova instance')
        ownership_committed = False
        try:
            _retry_migration_finish_action(
                lambda: _set_storage_handover_state(
                    remote, instance.name, 'owned',
                    container=destination,
                    migration_attempt=cleanup_token,
                    operation_uuid=attempt.get('operation_uuid')),
                'destination shared-storage ownership commit', instance)
            ownership_committed = True
        except Exception as ownership_error:
            try:
                _mark_remote_migration_recovery(
                    remote, instance.name,
                    'running' if destination.status == 'Running'
                    else 'stopped')
            except Exception:
                LOG.critical(
                    'The cold migration source is gone but target ownership '
                    'could not be committed or queued for recovery',
                    instance=instance, exc_info=True)
                raise exception.MigrationError(
                    reason='Cold migration target ownership is protected but '
                           'has no durable automatic-recovery marker') from (
                               ownership_error)
            LOG.critical(
                'Cold migration target remains deletion-protected; queued '
                'automatic ownership recovery', instance=instance,
                exc_info=True)

        if ownership_committed:
            try:
                _retry_migration_finish_action(
                    lambda: _finalize_committed_migration_attempt(
                        remote, instance, cleanup_token,
                        idmap_base, idmap_size),
                    'committed migration attempt retirement', instance)
            except Exception:
                LOG.critical(
                    'Cold migration committed but target staging metadata '
                    'could not be retired; queuing target recovery',
                    instance=instance, exc_info=True)
                _mark_remote_migration_recovery(
                    remote, instance.name,
                    'running' if destination.status == 'Running'
                    else 'stopped')

        # The target is now authoritative (or durably queued to become so).
        # Source-side Cinder, VIF, filesystem and profile cleanup cannot roll
        # that decision back, so retain a journal and let Nova finish.
        try:
            self._cleanup(
                context, instance, network_info,
                destroy_vifs=True, delete_profile=True)
        except Exception:
            LOG.critical(
                'Confirmed cold migration retained source cleanup work for '
                'periodic repair', instance=instance, exc_info=True)
        else:
            self._retire_instance_idmap_claim_if_clean(instance)

    @_invalidates_instance_inventory
    def finish_revert_migration(self, context, instance, network_info,
                                migration, block_device_info=None,
                                power_on=True):
        container = self.client.instances.get(instance.name)
        root_bdm = _boot_from_volume(block_device_info)
        if root_bdm:
            _require_bfv_migration_support(self.client, root_bdm)
        with lockutils.lock(_profile_lock_name(instance)):
            source_profile = self.client.profiles.get(instance.name)
            source_config = (
                source_profile.config
                if isinstance(source_profile.config, dict) else {})
            cleanup_token = source_config.get(MIGRATION_CLEANUP_TOKEN_KEY)
            destination_address = (
                source_config.get(MIGRATION_DESTINATION_KEY) or
                _migration_address_for_host(migration.dest_compute))
            source_operation_id = source_config.get(MIGRATION_OPERATION_KEY)
            rollback_complete = (
                source_config.get(MIGRATION_ROLLBACK_COMPLETE_KEY) ==
                cleanup_token)
        if not uuidutils.is_uuid_like(cleanup_token):
            raise exception.MigrationError(
                reason='Cold migration revert has no valid cleanup token')
        idmap_base, idmap_size = _instance_migration_idmap(
            container, source_profile)

        _settle_instance_migration_operations(
            self.client, instance,
            operation_ids=(source_operation_id,))
        remote = _migration_client(destination_address)

        if rollback_complete:
            container.sync()
            if power_on and container.status != 'Running':
                raise exception.MigrationError(
                    reason='Cold migration rollback completion marker exists '
                           'but the source instance is not running')
            try:
                acknowledgement = remote.profiles.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if not _is_incus_not_found(exc):
                    raise
            else:
                self._validate_remote_cleanup_acknowledgement(
                    acknowledgement, instance, cleanup_token,
                    idmap_base, idmap_size)
                acknowledgement.delete()
            _retire_migration_attempt(
                remote, instance, cleanup_token,
                idmap_base, idmap_size)
            return

        attempt = _get_migration_attempt(
            remote, instance, cleanup_token, idmap_base, idmap_size)
        if attempt['state'] == 'active':
            attempt = _abort_migration_attempt(
                remote, instance, cleanup_token, idmap_base, idmap_size,
                target_cleanup=lambda: _retry_migration_finish_action(
                    lambda: self._delete_migration_target_with_idmap(
                        remote, instance),
                    'aborted cold migration target deletion', instance))
        elif attempt['state'] in ('aborted', 'failed'):
            attempt = _wait_migration_attempt_finished(
                remote, instance, cleanup_token, idmap_base, idmap_size,
                ('aborted', 'failed'))
        elif attempt['state'] != 'committed':
            raise exception.MigrationError(
                reason='Cold migration revert found unsupported target '
                       'attempt state %s' % attempt['state'])

        try:
            remote.instances.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
        else:
            raise exception.MigrationError(
                reason='Cold migration destination instance still exists; '
                       'refusing to restore the source')
        try:
            acknowledgement = remote.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
            acknowledgement = None
        if acknowledgement is None:
            if attempt['state'] == 'committed':
                raise exception.MigrationError(
                    reason='Committed cold migration destination has no '
                           'positive cleanup acknowledgement')
        else:
            self._validate_remote_cleanup_acknowledgement(
                acknowledgement, instance, cleanup_token,
                idmap_base, idmap_size)

        source_ownership_restored = False
        _retry_migration_finish_action(
            lambda: _restore_source_storage_ownership(
                self.client, instance),
            'reverted source shared-storage ownership', instance)
        source_ownership_restored = True
        try:
            for bdm in _reboot_data_volume_bdms(
                    block_device_info,
                    root_device_name=instance.root_device_name):
                connection_info = bdm.get('connection_info')
                mountpoint = bdm.get('mount_device')
                if connection_info and mountpoint:
                    _retry_migration_finish_action(
                        lambda connection_info=connection_info,
                        mountpoint=mountpoint, bdm=bdm:
                        self._attach_volume_for_operation(
                            context, connection_info, instance, mountpoint,
                            _bdm_attachment_id(bdm), 'migration',
                            cleanup_token, 'cold-revert-source',
                            operation_migration_uuid=migration.uuid),
                        'revert data-volume attachment', instance)
            if network_info:
                # The retained source VIF still exists after cold migration.
                # Re-plugging it idempotently does not make ovn-controller
                # re-assert Port_Binding.up when a resize is reverted. Remove
                # the stale host wiring first so OVN observes a fresh
                # interface after the binding returns to this chassis.
                self._refresh_vifs(instance, network_info)
            if power_on and container.status != 'Running':
                _retry_migration_finish_action(
                    lambda: self._start_instance_with_idmap(
                        instance, container),
                    'revert container start', instance)
            with lockutils.lock(_profile_lock_name(instance)):
                profile = self.client.profiles.get(instance.name)
                profile.config[MIGRATION_ROLLBACK_COMPLETE_KEY] = cleanup_token
                profile.config[MIGRATION_NOVA_UUID_KEY] = migration.uuid
                profile.config.pop(MIGRATION_OPERATION_KEY, None)
                profile.save(wait=True)

            # Delete the destination acknowledgement only after the source
            # runtime, volumes and network are restored and durably marked.
            if acknowledgement is not None:
                acknowledgement = remote.profiles.get(instance.name)
                self._validate_remote_cleanup_acknowledgement(
                    acknowledgement, instance, cleanup_token,
                    idmap_base, idmap_size)
                acknowledgement.delete()
            _retire_migration_attempt(
                remote, instance, cleanup_token,
                idmap_base, idmap_size)
        except Exception:
            if not source_ownership_restored:
                raise
            if not CONF.incus.migration_auto_recovery:
                raise
            try:
                _retry_migration_finish_action(
                    lambda: self._mark_migration_recovery_required(
                        instance, power_on=power_on),
                    'revert durable recovery marker', instance)
            except Exception:
                LOG.exception(
                    'Failed to mark reverted owner for automatic recovery',
                    instance=instance)
                raise
            # Nova has already restored Cinder, Neutron, Placement and the
            # instance host to this retained source. Preserve that completed
            # ownership decision and let the manager repair runtime state.
            LOG.error(
                'Retaining the reverted owner for automatic runtime '
                'recovery after finish_revert_migration failed',
                instance=instance)

    def pre_live_migration(self, context, instance, block_device_info,
                           network_info, disk_info, migrate_data):
        with lockutils.lock(
                _live_migration_host_generation_lock_name(instance),
                external=True, lock_path=CONF.state_path):
            return self._pre_live_migration_locked(
                context, instance, block_device_info, network_info,
                disk_info, migrate_data)

    def _pre_live_migration_locked(
            self, context, instance, block_device_info, network_info,
            disk_info, migrate_data):
        if not isinstance(
                migrate_data, incus_migrate_data.IncusLiveMigrateData):
            raise exception.MigrationError(
                reason='Missing Incus live migration destination data')

        # This versioned attestation distinguishes a source that validated
        # profile/local/expanded state from an older source that only happened
        # to serialize a profile value of false. Reject it before target idmap,
        # Incus profile/idmap/VIF and host-side Cinder driver preparation. The
        # custom compute manager repeats this check before its Manila staging
        # and before the upstream manager creates Cinder attachments.
        _require_full_checkpoint_attestation(migrate_data)
        cleanup_token = _live_migration_cleanup_token(migrate_data)
        migration_uuid = _live_migration_uuid(migrate_data)
        idmap_base, idmap_size = _live_migration_idmap(migrate_data)
        config, devices = _live_migration_profile_data(migrate_data)
        _require_full_checkpoint_profile_config(config)
        if (config.get('user.openstack.uuid') != instance.uuid or
                config.get(MIGRATION_CLEANUP_TOKEN_KEY) != cleanup_token):
            raise exception.MigrationError(
                reason='Incus source profile UUID or cleanup token does not '
                       'match the live migration request')
        try:
            profile_idmap = (
                int(config.get('security.idmap.base')),
                int(config.get('security.idmap.size')))
        except (TypeError, ValueError) as exc:
            raise exception.MigrationError(
                reason='Incus live migration profile has no fixed idmap'
            ) from exc
        if profile_idmap != (idmap_base, idmap_size):
            raise exception.MigrationError(
                reason='Incus live migration profile idmap does not match '
                       'the target reservation')
        root_bdm = _boot_from_volume(block_device_info)
        if root_bdm:
            root_volume = _require_bfv_live_migration_support(
                self.client, root_bdm)
            root_device = _bfv_root_device(
                instance, root_bdm, root_volume)
            devices['root'] = root_device
        else:
            root_device = devices.get('root')
        try:
            materialization = self._begin_idmap_materialization(
                instance, cleanup_token, root_device,
                observed_base=idmap_base, observed_size=idmap_size,
                shared_migration=True)
        except incus_idmap.IDMapError as exc:
            raise exception.MigrationError(
                reason='Cannot register the live migration root '
                       'materialization: {}'.format(exc)) from exc
        if materialization is not None:
            config.update(self._idmap_materialization_config(
                materialization.binding))
        try:
            _register_migration_attempt(
                self.client, instance, cleanup_token,
                idmap_base, idmap_size)
        except Exception:
            with excutils.save_and_reraise_exception():
                self._abort_idmap_materialization(materialization)
                self._retire_instance_idmap_claim_if_clean(instance)
        config[MIGRATION_CLEANUP_COMPLETE_KEY] = ''
        config[MIGRATION_NOVA_UUID_KEY] = migration_uuid
        try:
            _remove_stale_live_migration_profile(self.client, instance)
        except Exception:
            with excutils.save_and_reraise_exception():
                attempt = _abort_migration_attempt(
                    self.client, instance, cleanup_token,
                    idmap_base, idmap_size)
                if attempt['state'] != 'committed':
                    _retire_migration_attempt(
                        self.client, instance, cleanup_token,
                        idmap_base, idmap_size)
                    self._abort_idmap_materialization(materialization)
                    self._retire_instance_idmap_claim_if_clean(instance)

        try:
            _prepare_live_migration_destination_profile(
                self.client, instance, config, devices, cleanup_token)
        except Exception:
            with excutils.save_and_reraise_exception():
                attempt = _abort_migration_attempt(
                    self.client, instance, cleanup_token,
                    idmap_base, idmap_size)
                if attempt['state'] != 'committed':
                    _retire_migration_attempt(
                        self.client, instance, cleanup_token,
                        idmap_base, idmap_size)
                    self._abort_idmap_materialization(materialization)
                    self._retire_instance_idmap_claim_if_clean(instance)

        prepared_volumes = []
        volume_bdms = list(driver.block_device_info_get_mapping(
            block_device_info))
        # Nova always invokes driver_detach for every destination BDM after a
        # pre-live failure. Start with "nothing connected" and clear the
        # marker only while this driver owns a live host mapping.
        for bdm in volume_bdms:
            connection_info = bdm.get('connection_info')
            if connection_info:
                connection_info.setdefault(
                    'data', {})[_PRE_LIVE_DISCONNECTED_KEY] = True
        try:
            self.plug_vifs(instance, network_info)
            self.firewall_driver.setup_basic_filtering(
                instance, network_info)
            self.firewall_driver.prepare_instance_filter(
                instance, network_info)
            self.firewall_driver.apply_instance_filter(
                instance, network_info)

            for bdm in volume_bdms:
                if _is_boot_volume(bdm):
                    # Incus cephext claims the root during the ordered
                    # CRIU/shared-storage handover. Connecting it through
                    # os-brick here would violate single-writer ownership.
                    # The root BDM was already validated before its Incus
                    # device was added to the destination profile.
                    continue
                connection_info = bdm.get('connection_info')
                mountpoint = bdm.get('mount_device')
                if connection_info and mountpoint:
                    self._stage_volume_for_live_migration(
                        context, connection_info, instance, mountpoint,
                        _bdm_attachment_id(bdm), cleanup_token,
                        migration_uuid)
                    connection_info.setdefault('data', {}).pop(
                        _PRE_LIVE_DISCONNECTED_KEY, None)
                    prepared_volumes.append(bdm)
        except Exception:
            with excutils.save_and_reraise_exception():
                for bdm in reversed(prepared_volumes):
                    connection_info = bdm['connection_info']
                    mountpoint = bdm['mount_device']
                    try:
                        self._detach_volume(
                            context, connection_info, instance, mountpoint)
                    except Exception:
                        LOG.exception(
                            'Failed to roll back a destination Cinder '
                            'connection during pre-live migration',
                            instance=instance)
                    else:
                        connection_info.setdefault(
                            'data', {})[_PRE_LIVE_DISCONNECTED_KEY] = True

                cleanup_complete = False
                try:
                    # The profile was created before any target-side VIF,
                    # volume or Manila resource. Reuse the durable cleanup
                    # transaction so every VIF is retried independently and
                    # any residual host resource retains a periodic-recovery
                    # owner instead of being orphaned.
                    self._cleanup(
                        context, instance, network_info,
                        block_device_info=None, destroy_vifs=True,
                        delete_profile=True)
                    cleanup_complete = True
                except Exception:
                    LOG.exception(
                        'Failed to roll back pre-live migration destination '
                        'resources; retained the cleanup profile for recovery',
                        instance=instance)

                # attach_volume normally rolls back its own partial work. If
                # it retained device_info after an uncertain disconnect,
                # leave the marker clear so Nova's mandatory second detach
                # retries it instead of silently leaking a host mapping.
                profile = None
                if not cleanup_complete:
                    try:
                        profile = self.client.profiles.get(instance.name)
                    except incus_exceptions.LXDAPIException as exc:
                        if not _is_incus_not_found(exc):
                            raise
                if profile is not None:
                    for bdm in volume_bdms:
                        if _is_boot_volume(bdm):
                            continue
                        connection_info = bdm.get('connection_info')
                        if not connection_info:
                            continue
                        volume_id = _volume_id(connection_info)
                        if _profile_has_volume_connection(profile, volume_id):
                            connection_info.setdefault('data', {}).pop(
                                _PRE_LIVE_DISCONNECTED_KEY, None)
                attempt = _abort_migration_attempt(
                    self.client, instance, cleanup_token,
                    idmap_base, idmap_size)
                if (
                    cleanup_complete and
                    attempt['state'] != 'committed'
                ):
                    _retire_migration_attempt(
                        self.client, instance, cleanup_token,
                        idmap_base, idmap_size)
                    self._abort_idmap_materialization(materialization)
                    self._retire_instance_idmap_claim_if_clean(instance)

        return migrate_data

    def _abort_pre_live_migration_preparation(
            self, instance, migrate_data):
        """Clean a pre-receive target without requiring an ACK profile."""
        cleanup_token = _live_migration_cleanup_token(migrate_data)
        idmap_base, idmap_size = _live_migration_idmap(migrate_data)
        attempt = None
        try:
            attempt = _abort_migration_attempt(
                self.client, instance, cleanup_token,
                idmap_base, idmap_size,
                target_cleanup=lambda: _retry_migration_finish_action(
                    lambda: _delete_migration_target_record(
                        self.client, instance),
                    'aborted live migration pre-stage target deletion',
                    instance))
        except incus_exceptions.LXDAPIException as exc:
            # Manila staging precedes driver.pre_live_migration(), so the
            # attempt legitimately does not exist when the first mount fails.
            if not _is_incus_not_found(exc):
                raise
        if attempt is not None and attempt['state'] == 'committed':
            raise exception.MigrationError(
                reason='Refusing pre-live cleanup after the target attempt '
                       'committed')

        _cleanup_share_journal_mounts(
            instance, operation_token=cleanup_token)
        if attempt is not None:
            _retire_migration_attempt(
                self.client, instance, cleanup_token,
                idmap_base, idmap_size)
        self._retire_instance_idmap_claim_if_clean(instance)
        return True

    def cleanup_pre_live_migration_destination(
            self, context, instance, migrate_data=None):
        """Remove an unused profile after Nova's second detach pass."""
        try:
            self.client.instances.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
        else:
            return False

        profile = None
        with lockutils.lock(_profile_lock_name(instance)):
            try:
                profile = self.client.profiles.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if not _is_incus_not_found(exc):
                    raise
            if profile is not None:
                profile_config = (
                    profile.config
                    if isinstance(profile.config, dict) else {})
                if (profile_config.get('environment.product_name') !=
                        'OpenStack Nova' or
                        profile_config.get('user.openstack.uuid') !=
                        instance.uuid or profile.used_by):
                    raise exception.DestinationDiskExists(path=instance.name)
                cleanup_token = profile_config.get(
                    MIGRATION_CLEANUP_TOKEN_KEY)
                try:
                    idmap_base = int(
                        profile_config.get('security.idmap.base'))
                    idmap_size = int(
                        profile_config.get('security.idmap.size'))
                except (TypeError, ValueError) as exc:
                    raise exception.MigrationError(
                        reason='Failed pre-live profile has no fixed idmap'
                    ) from exc
                if migrate_data is not None:
                    expected_token = _live_migration_cleanup_token(
                        migrate_data)
                    expected_idmap = _live_migration_idmap(migrate_data)
                    if (
                            cleanup_token != expected_token or
                            (idmap_base, idmap_size) != expected_idmap):
                        raise exception.MigrationError(
                            reason='Failed pre-live profile does not match '
                                   'the migration cleanup transaction')
        if profile is None:
            if migrate_data is not None:
                return self._abort_pre_live_migration_preparation(
                    instance, migrate_data)
            return False
        attempt = _abort_migration_attempt(
            self.client, instance, cleanup_token,
            idmap_base, idmap_size)
        if attempt['state'] == 'committed':
            raise exception.MigrationError(
                reason='Refusing pre-live cleanup after the target attempt '
                       'committed')
        try:
            network_info = self.network_api.get_instance_nw_info(
                context, instance)
        except Exception:
            LOG.exception(
                'Failed to read destination VIFs for pre-live migration '
                'cleanup; retained the cleanup profile for recovery',
                instance=instance)
            self._mark_cleanup_recovery_required(instance)
            return False

        try:
            self._cleanup(
                context, instance, network_info,
                block_device_info=None, destroy_vifs=True,
                delete_profile=True)
        except Exception:
            LOG.exception(
                'Failed to retry pre-live migration destination cleanup; '
                'retained the cleanup profile for recovery',
                instance=instance)
            return False

        _retire_migration_attempt(
            self.client, instance, cleanup_token,
            idmap_base, idmap_size)
        self._retire_instance_idmap_claim_if_clean(instance)
        return True

    @_invalidates_instance_inventory
    def live_migration(self, context, instance, dest,
                       post_method, recover_method, block_migration=False,
                       migrate_data=None):
        if not isinstance(
                migrate_data, incus_migrate_data.IncusLiveMigrateData):
            raise exception.MigrationError(
                reason='Missing Incus live migration destination data')
        if block_migration:
            raise exception.MigrationError(
                reason='Incus CRIU block migration is not supported')

        cleanup_token = _live_migration_cleanup_token(migrate_data)
        idmap_base, idmap_size = _live_migration_idmap(migrate_data)
        remote = None
        container = self.client.instances.get(instance.name)
        source_operation_id = None
        destination_operation_id = None
        destination_materialization = None
        try:
            remote = _migration_client(migrate_data.destination_address)
            try:
                destination_profile = remote.profiles.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if _is_incus_not_found(exc):
                    raise exception.MigrationError(
                        reason='Incus live migration destination profile is '
                               'missing; pre_live_migration did not complete'
                    ) from exc
                raise
            profile_config = (
                destination_profile.config
                if isinstance(destination_profile.config, dict) else {})
            if (profile_config.get('environment.product_name') !=
                    'OpenStack Nova' or destination_profile.used_by):
                raise exception.MigrationError(
                    reason='Incus live migration destination profile is not '
                           'an unused OpenStack Nova staging profile')
            with lockutils.lock(_profile_lock_name(instance)):
                source_profile = _live_migration_profile_check(
                    self.client, context, instance)
                container = self.client.instances.get(instance.name)
                _validate_live_migration_source_instance(
                    instance, container, source_profile,
                    require_incremental=True,
                    error_class=exception.MigrationError)
            if profile_config.get(
                    'migration.incremental.memory') != 'false':
                raise exception.MigrationError(
                    reason='Incus live migration destination profile must set '
                           'migration.incremental.memory=false')
            unused_assignment, destination_claim = (
                self._idmap_claim_from_local_config(
                    instance, profile_config))
            if (destination_claim is not None and
                    destination_claim.materialization_id != cleanup_token):
                raise exception.MigrationError(
                    reason='Incus live migration destination profile has a '
                           'different materialization token')
            destination_root = (
                destination_profile.devices.get('root')
                if isinstance(destination_profile.devices, dict) else None)
            destination_materialization = self._resume_idmap_materialization(
                remote, instance, cleanup_token,
                destination_claim.host_id, destination_root,
                idmap_base, idmap_size)
            unused_source_assignment, source_claim = (
                self._instance_local_idmap_claim(instance, container))
            if (source_claim is not None and destination_claim is not None and
                    source_claim.materialization_id ==
                    destination_claim.materialization_id):
                raise exception.MigrationError(
                    reason='Incus live migration source and destination must '
                           'use distinct materialization tokens')

            destination_profile = remote.profiles.get(instance.name)
            destination_config = (
                destination_profile.config
                if isinstance(destination_profile.config, dict) else {})
            if destination_config.get(
                    'migration.incremental.memory') != 'false':
                raise exception.MigrationError(
                    reason='Incus live migration destination profile must set '
                           'migration.incremental.memory=false')
            # Re-read the complete source profile and instance at the exact
            # execution boundary. Hold the local profile lock through source
            # operation creation so another driver operation cannot mutate the
            # validated profile between the checks and CRIU startup.
            with lockutils.lock(_profile_lock_name(instance)):
                source_profile = _live_migration_profile_check(
                    self.client, context, instance)
                container = self.client.instances.get(instance.name)
                _validate_live_migration_source_instance(
                    instance, container, source_profile,
                    require_incremental=True,
                    error_class=exception.MigrationError)
                source_config = (
                    source_profile.config
                    if isinstance(source_profile.config, dict) else {})
                migration_uuid = _live_migration_uuid(migrate_data)
                existing_migration_uuid = source_config.get(
                    MIGRATION_NOVA_UUID_KEY)
                if existing_migration_uuid not in (None, migration_uuid):
                    raise exception.MigrationError(
                        reason='Incus live migration source generation '
                               'changed')
                source_config[MIGRATION_NOVA_UUID_KEY] = migration_uuid
                source_profile.config = source_config
                source_profile.save(wait=True)
                migration_data = container.generate_migration_data(live=True)
            migration_data.pop('default', None)
            migration_data['profiles'] = [instance.name]
            _bind_migration_instance_local_owner(
                migration_data, instance)

            source = migration_data['source']
            source_operation_id = _migration_operation_id(
                source['operation'])
            if source_operation_id is None:
                raise exception.MigrationError(
                    reason='Incus live migration returned no source '
                           'operation UUID')
            migrate_data.source_operation_id = source_operation_id
            # Saving a profile makes Incus resync the instance's backup.yaml,
            # which lives inside the root volume and therefore needs that
            # volume mounted. Once a shared-storage handover has started the
            # destination already holds the only permitted claim on it, so the
            # write fails and takes the whole migration down with it. The
            # operation UUID is carried to Nova in migrate_data above, and the
            # recovery paths that consume this key enumerate the instance's
            # migration operations themselves and treat it only as a hint, so
            # skipping the durable copy costs nothing here.
            if _live_migration_shares_root_storage(
                    self.client, instance, container):
                LOG.debug(
                    'Skipping the source migration-operation profile write '
                    'for a shared-storage root; the destination owns the '
                    'volume for the rest of this migration',
                    instance=instance)
            else:
                with lockutils.lock(_profile_lock_name(instance)):
                    source_profile = self.client.profiles.get(instance.name)
                    source_profile.config[MIGRATION_OPERATION_KEY] = (
                        source_operation_id)
                    source_profile.save(wait=True)
            # pylxd starts the source operation in live mode but does not
            # propagate that mode into the target InstanceSource payload.
            # Without this flag the target opens only the cold-migration
            # channels while the source waits for the CRIU state channel.
            source['live'] = True
            source['operation'] = _migration_operation_url(
                source['operation'], CONF.incus.migration_address)

            def record_target_operation(operation_id):
                nonlocal destination_operation_id
                destination_operation_id = operation_id
                migrate_data.destination_operation_id = operation_id
                with lockutils.lock(_profile_lock_name(instance)):
                    target_profile = remote.profiles.get(instance.name)
                    config = (
                        target_profile.config
                        if isinstance(target_profile.config, dict) else {})
                    if (config.get(MIGRATION_CLEANUP_TOKEN_KEY) !=
                            _live_migration_cleanup_token(migrate_data)):
                        raise exception.MigrationError(
                            reason='Incus migration target operation profile '
                                   'token does not match')
                    target_profile.config[MIGRATION_TARGET_OPERATION_KEY] = (
                        operation_id)
                    _save_profile_marker(target_profile)

            self._with_rootfs_materialization_barrier(
                destination_materialization,
                migration_data.setdefault('config', {}),
                lambda: _create_migration_target(
                    remote, migration_data, instance, cleanup_token,
                    idmap_base, idmap_size,
                    operation_started=record_target_operation),
                recover_action=lambda: _create_migration_target(
                    remote, migration_data, instance, cleanup_token,
                    idmap_base, idmap_size,
                    operation_started=record_target_operation))
        except Exception as migration_error:
            settlement_failures = []
            target_attempt = None
            if remote is not None:
                def delete_failed_target():
                    self._delete_migration_target_with_idmap(
                        remote, instance)

                try:
                    target_attempt = _abort_migration_attempt(
                        remote, instance, cleanup_token,
                        idmap_base, idmap_size,
                        target_cleanup=lambda: (
                            _retry_migration_finish_action(
                                delete_failed_target,
                                'aborted live migration target deletion',
                                instance)))
                except Exception as exc:
                    settlement_failures.append(exc)
                    LOG.critical(
                        'Failed to fence the Incus live migration target '
                        'attempt', instance=instance, exc_info=True)
            if (target_attempt is not None and
                    target_attempt['state'] == 'committed'):
                _migration_attempt_instance(remote, instance)
                LOG.warning(
                    'Recovered a committed live migration after its client '
                    'response path failed', instance=instance,
                    exc_info=migration_error)
                post_method(
                    context, instance, dest, block_migration, migrate_data)
                return
            try:
                _settle_instance_migration_operations(
                    self.client, instance,
                    operation_ids=(source_operation_id,))
            except Exception as exc:
                settlement_failures.append(exc)
                LOG.critical(
                    'Failed to fence the Incus live migration source '
                    'operation', instance=instance, exc_info=True)
            if settlement_failures:
                # Calling Nova's recovery callback before both asynchronous
                # operations are terminal can materialize a late target after
                # the source resumes. Fail closed and require periodic/operator
                # recovery instead.
                try:
                    container.sync()
                    if container.status != 'Stopped':
                        container.stop(timeout=-1, force=True, wait=True)
                except Exception:
                    LOG.critical(
                        'Failed to stop the ambiguous live migration source',
                        instance=instance, exc_info=True)
                raise exception.MigrationError(
                    reason='Incus live migration failed and its asynchronous '
                           'operations could not be fenced'
                ) from migration_error
            # Nova owns rollback ordering. The target profile contains the
            # os-brick device_info required to disconnect destination volume
            # mappings before destination VIF and Manila cleanup.
            recover_method(context, instance, dest, migrate_data)
            return

        post_method(
            context, instance, dest, block_migration, migrate_data)

    @_invalidates_instance_inventory
    def post_live_migration(self, context, instance, block_device_info,
                            migrate_data=None):
        container = self.client.instances.get(instance.name)
        unused_assignment, source_claim = self._instance_local_idmap_claim(
            instance, container)
        destination_address = _live_migration_destination_address(
            migrate_data)
        if not destination_address:
            raise exception.MigrationError(
                reason='Missing live migration destination address')
        cleanup_token = _live_migration_cleanup_token(migrate_data)
        idmap_base, idmap_size = _live_migration_idmap(migrate_data)
        remote = _migration_client(destination_address)
        attempt = _get_migration_attempt(
            remote, instance, cleanup_token, idmap_base, idmap_size)
        if attempt['state'] != 'committed' or not attempt.get('finished'):
            raise exception.MigrationError(
                reason='Refusing live migration source deletion before the '
                       'destination attempt is committed')
        # The source record deletion below is irreversible. Persist the
        # source host cleanup transaction before changing shared-root
        # ownership so os-brick and Manila journals remain discoverable after
        # a compute-process restart.
        self._mark_cleanup_recovery_required(instance)

        # Persist every source data-volume release owner before the first
        # irreversible source-record operation.  A process that dies after
        # deleting the source container must never leave a later volume with
        # neither a journal nor an operation generation.
        source_volume_releases = []
        try:
            for bdm in driver.block_device_info_get_mapping(
                    block_device_info):
                boot_volume = _is_boot_volume(bdm)
                connection_info = bdm.get('connection_info')
                mountpoint = bdm.get('mount_device')
                if not connection_info or not mountpoint:
                    raise exception.InvalidVolume(
                        reason='Live migration source Cinder mapping has no '
                               'connection information or mountpoint')
                volume_id = _volume_id(connection_info)
                with lockutils.lock(
                        _volume_manager_transaction_lock_name(
                            instance.uuid, volume_id),
                        external=True,
                        lock_path=_volume_operation_lock_path()):
                    source_mapping_present = True
                    source_record = None
                    if not boot_volume:
                        qos_limits = _data_volume_qos(
                            connection_info,
                            self.client.host_info.get('api_extensions', []))
                        with lockutils.lock(_profile_lock_name(instance)):
                            source_profile = self.client.profiles.get(
                                instance.name)
                            _validate_profile_volume_owner(
                                source_profile, instance)
                        mapping_cache = {}
                        if connection_info.get('driver_volume_type') == 'rbd':
                            unused_image, source_mappings = (
                                _rbd_mapping_matches(
                                    connection_info.get('data') or {},
                                    mapping_cache=mapping_cache))
                            if len(source_mappings) > 1:
                                raise exception.InvalidVolume(
                                    reason='Live migration source has '
                                           'multiple '
                                           'local RBD mappings for %s' %
                                           volume_id)
                            source_mapping_present = bool(source_mappings)
                        if not _profile_volume_attachment_matches(
                                source_profile, volume_id, mountpoint,
                                qos_limits, connection_info,
                                rbd_mapping_cache=mapping_cache,
                                allow_missing_rbd_mapping=True):
                            raise exception.InvalidVolume(
                                reason='Live migration source profile does '
                                       'not contain the exact Cinder '
                                       'data-volume mapping for %s' %
                                       volume_id)
                        source_record = _profile_volume_record(
                            source_profile, volume_id,
                            device=source_profile.devices.get(volume_id))
                    phase = self.get_volume_journal_phase(instance, volume_id)
                    if phase is not None:
                        raise exception.InvalidVolume(
                            reason='Live migration source volume %s has '
                                   'unfinished host work' % volume_id)
                    intent = self.prepare_managed_volume_attach(
                        instance, volume_id, _bdm_attachment_id(bdm),
                        mountpoint, operation_kind='migration',
                        operation_token=cleanup_token,
                        operation_direction='live-source-release',
                        operation_migration_uuid=(
                            _live_migration_uuid(migrate_data)),
                        boot_volume=boot_volume)
                    if not boot_volume and not source_mapping_present:
                        # The source mapping can already be absent when the
                        # CRIU source operation becomes terminal. Preserve that
                        # exact, disconnected state before the source record is
                        # irreversibly deleted.
                        _write_volume_journal(
                            instance, volume_id, connection_info,
                            source_record.get('device_info') or {}, mountpoint,
                            phase='disconnected')
                source_volume_releases.append((
                    bdm, connection_info, mountpoint, volume_id, intent,
                    boot_volume))
        except Exception:
            # No source record has been deleted yet.  Remove only exact
            # prepared intents that still have no host journal; a journal is
            # proof that a concurrent/retried transaction crossed the safe
            # cancellation point and must remain for recovery.
            for (unused_bdm, unused_info, unused_mountpoint, volume_id,
                 intent, unused_boot_volume) in reversed(
                    source_volume_releases):
                try:
                    with lockutils.lock(
                            _volume_manager_transaction_lock_name(
                                instance.uuid, volume_id),
                            external=True,
                            lock_path=_volume_operation_lock_path()):
                        if self.get_volume_journal_phase(
                                instance, volume_id) is None:
                            self.cancel_managed_volume_attach(
                                instance, volume_id, intent)
                except Exception:
                    LOG.critical(
                        'Failed to cancel prepared live-source release for '
                        'Cinder volume %s while the source record is intact',
                        volume_id, instance=instance, exc_info=True)
            raise

        # Protect the source record before deleting it. The destination stays
        # protected by Incus until this source record is gone, so there is
        # never a point at which both records can delete the shared RBD.
        _retry_migration_finish_action(
            lambda: _set_storage_handover_state(
                self.client, instance.name, 'protected',
                container=container),
            'source shared-storage delete protection', instance)
        if container.status != 'Stopped':
            # Incus may keep the source record in Running state briefly after
            # CRIU has restored the target. Match `incus delete --force`:
            # force-stop the source record before removing it.
            try:
                container.stop(timeout=-1, force=True, wait=True)
            except incus_exceptions.LXDAPIException as exc:
                # The migration source can finish stopping between the status
                # read and this request. Treat only that exact Incus response
                # as success; all other cleanup failures remain fatal.
                if 'instance is already stopped' not in str(exc).lower():
                    raise
                LOG.debug(
                    'Source instance stopped before live-migration cleanup',
                    instance=instance)
        if source_claim is None:
            container.delete(wait=True)
        else:
            self._delete_instance_with_rootfs_release_receipt(
                container, instance, source_claim)

        ownership_committed = False
        try:
            _retry_migration_finish_action(
                lambda: _set_storage_handover_state(
                    remote, instance.name, 'owned',
                    migration_attempt=cleanup_token,
                    operation_uuid=attempt.get('operation_uuid')),
                'destination shared-storage ownership commit', instance)
            ownership_committed = True
        except Exception:
            # The target remains runnable but deletion-protected. Leave a
            # durable marker so its compute service can retry ownership
            # without turning an otherwise completed migration into a
            # source/target split-brain rollback.
            LOG.critical(
                'Live migration target remains shared-storage '
                'deletion-protected; queuing automatic recovery',
                instance=instance, exc_info=True)
            try:
                _mark_remote_migration_recovery(
                    remote, instance.name, 'running')
            except Exception:
                LOG.critical(
                    'Failed to queue ownership recovery for a protected '
                    'live migration target; operator repair is required',
                    instance=instance, exc_info=True)

        if ownership_committed:
            try:
                _retry_migration_finish_action(
                    lambda: _finalize_committed_migration_attempt(
                        remote, instance, cleanup_token,
                        idmap_base, idmap_size),
                    'committed live migration attempt retirement', instance)
            except Exception:
                LOG.critical(
                    'Live migration committed but target staging metadata '
                    'could not be retired; queuing target recovery',
                    instance=instance, exc_info=True)
                _mark_remote_migration_recovery(
                    remote, instance.name, 'running')

        # Cinder removes the source attachment record after this hook, but
        # os-brick host mappings are owned by the virt driver. Disconnect
        # every source data-volume mapping while the profile still contains
        # the exact device_info returned by connect_volume(). The BFV root is
        # transferred by the Incus cephext handover and never uses os-brick.
        for (bdm, connection_info, mountpoint, volume_id, intent,
             boot_volume) in source_volume_releases:
            if boot_volume:
                # The cephext handover already transferred the BFV root.  Its
                # intent remains until ComputeManager removes the exact old
                # Cinder attachment; it must never enter os-brick.
                continue
            try:
                self._disconnect_live_source_volume(
                    context, instance, volume_id, connection_info,
                    mountpoint, intent)
            except Exception:
                # Match Nova's libvirt contract: the instance is already
                # running on the destination and cannot be rolled back here.
                # detach_volume restores the profile entry on failure, and
                # cleanup() deliberately retains that profile for repair.
                LOG.exception(
                    'Retaining source volume connection after live migration '
                    'disconnect failed for volume %s',
                    _volume_id(connection_info), instance=instance)

    def _disconnect_live_source_volume(
            self, context, instance, volume_id, connection_info,
            mountpoint, intent):
        """Disconnect one source mapping unless recovery already did so."""
        with lockutils.lock(
                _volume_manager_transaction_lock_name(
                    instance.uuid, volume_id),
                external=True,
                lock_path=_volume_operation_lock_path()):
            current = self.get_managed_volume_attach_intent(
                instance, volume_id)
            if current != intent:
                journal_phase = self.get_volume_journal_phase(
                    instance, volume_id)
                if current is None and journal_phase is None:
                    # Periodic recovery may finish the exact source release
                    # while CRIU handover is returning to this hook. It
                    # removes the intent only after local disconnect and the
                    # exact Cinder source attachment deletion both converge.
                    return
                raise exception.InvalidVolume(
                    reason='Live migration source volume release generation '
                           'changed before disconnect')
            journal_phase = self.get_volume_journal_phase(instance, volume_id)
            if journal_phase == 'disconnected':
                self._retire_disconnected_source_profile_device_locked(
                    instance, volume_id, mountpoint, connection_info)
                return
            if journal_phase is not None:
                raise exception.InvalidVolume(
                    reason='Live migration source volume release has '
                           'unexpected %s host evidence' % journal_phase)
            self._detach_volume(
                context, connection_info, instance, mountpoint,
                retain_journal=True)

    def _live_source_cleanup_was_superseded(self, instance):
        """Detect a newer destination generation on the former source host.

        Nova calls ``post_live_migration_at_source`` after the old Cinder
        attachment has been retired.  An immediate reverse migration can
        create a new destination profile or instance on this host before that
        callback runs.  The resource names are intentionally stable, so the
        old callback must not consume the new generation.
        """
        with lockutils.lock(_profile_lock_name(instance)):
            try:
                profile = self.client.profiles.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if _is_incus_not_found(exc):
                    profile = None
                else:
                    raise
            if profile is not None:
                _validate_profile_volume_owner(profile, instance)
                config = (
                    profile.config if isinstance(profile.config, dict)
                    else {})
                if config.get(MIGRATION_DESTINATION_PREPARED_KEY):
                    return True

            try:
                container = self.client.instances.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if _is_incus_not_found(exc):
                    return False
                raise
            if _instance_nova_uuid(container) != instance.uuid:
                raise exception.MigrationError(
                    reason='Incus instance name was reused by a different '
                           'owner before live source cleanup')
            return True

    @_invalidates_instance_inventory
    def post_live_migration_at_source(self, context, instance, network_info):
        with lockutils.lock(
                _live_migration_host_generation_lock_name(instance),
                external=True, lock_path=CONF.state_path):
            return self._post_live_migration_at_source_locked(
                context, instance, network_info)

    def _post_live_migration_at_source_locked(
            self, context, instance, network_info):
        if self._live_source_cleanup_was_superseded(instance):
            LOG.warning(
                'Skipping obsolete live source cleanup because this host '
                'already owns a newer destination generation',
                instance=instance)
            return
        # The guest now runs on the destination, so any broker left here is
        # bound to a container this host is about to delete. Keeping it
        # would hold a proxy port, and would hand a dead console to the
        # next request if the instance ever migrated back.
        self._release_serial_console_broker(instance)
        failures = []
        try:
            profile = self.client.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if _is_incus_not_found(exc):
                profile = None
            else:
                raise
        try:
            if profile is not None:
                with lockutils.lock(_profile_lock_name(instance)):
                    _cleanup_profile_share_mounts(profile, instance)
        except Exception as exc:
            failures.append(exc)
            LOG.exception(
                'Failed to clean source Manila mounts after live migration',
                instance=instance)
        # cleanup() deletes the profile only after all os-brick connection
        # metadata has been removed. A failed source disconnect must retain
        # that metadata for deterministic operator or periodic repair.
        try:
            self._cleanup(
                context, instance, network_info,
                delete_profile=not failures)
        except Exception as exc:
            failures.append(exc)
        if not failures:
            self._retire_instance_idmap_claim_if_clean(instance)
        if failures:
            # The workload is already authoritative on the destination.
            # Raising here can prevent Nova from completing Cinder and
            # Neutron bookkeeping but cannot roll the migration back. The
            # retained profile is the durable retry journal.
            LOG.critical(
                '%d live migration source cleanup operation(s) failed; '
                'retained the profile for periodic or operator retry',
                len(failures), instance=instance)

    @_invalidates_instance_inventory
    def post_live_migration_at_destination(
            self, context, instance, network_info, block_migration=False,
            block_device_info=None):
        container = self.client.instances.get(instance.name)
        if container.status != 'Running':
            raise exception.MigrationError(
                reason='CRIU-restored Incus instance is not running on the '
                'destination')
        # Source post_live_migration() commits ownership and retires its proof
        # before Nova invokes this destination callback. Repeatedly issuing an
        # owned transition would require proof which has intentionally been
        # retired. Verify the already-owned state, or finish a protected target
        # left by a lost source callback and persist recovery on uncertainty.
        _converge_migration_target_ownership(
            self.client, instance, desired_state='running',
            local_volume_evidence=True)

    def rollback_live_migration_at_source(
            self, context, instance, migrate_data):
        try:
            container = self.client.instances.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if _is_incus_not_found(exc):
                raise exception.MigrationError(
                    reason='Incus live migration source record is missing; '
                           'refusing to report rollback success') from exc
            raise
        destination_address = _live_migration_destination_address(
            migrate_data)
        cleanup_token = _live_migration_cleanup_token(migrate_data)
        idmap_base, idmap_size = _live_migration_idmap(migrate_data)
        if not destination_address:
            raise exception.MigrationError(
                reason='Live migration rollback has no destination address')
        remote = _migration_client(destination_address)
        try:
            target_attempt = _abort_migration_attempt(
                remote, instance, cleanup_token,
                idmap_base, idmap_size,
                target_cleanup=lambda: _retry_migration_finish_action(
                    lambda: self._delete_migration_target_with_idmap(
                        remote, instance),
                    'aborted live migration target deletion', instance))
        except Exception:
            try:
                container.sync()
                if container.status != 'Stopped':
                    container.stop(timeout=-1, force=True, wait=True)
            except Exception:
                LOG.critical(
                    'Failed to fence the live migration source after the '
                    'target attempt could not be fenced',
                    instance=instance, exc_info=True)
            raise
        try:
            _settle_instance_migration_operations(
                self.client, instance,
                operation_ids=(
                    getattr(migrate_data, 'source_operation_id', None),))
        except Exception:
            try:
                container.sync()
                if container.status != 'Stopped':
                    container.stop(timeout=-1, force=True, wait=True)
            except Exception:
                LOG.critical(
                    'Failed to fence the source after its migration operation '
                    'could not be settled', instance=instance, exc_info=True)
            raise
        if target_attempt['state'] == 'committed':
            LOG.warning(
                'Live migration rollback lost the attempt race to a '
                'committed target; leaving target teardown to the '
                'destination rollback RPC', instance=instance)

        # Do not restore source ownership or runtime here. Nova has not yet
        # reverted Cinder attachment IDs, Neutron bindings, destination
        # os-brick mappings, or Manila mounts. The custom manager calls
        # finalize_live_migration_rollback only after those control-plane
        # steps and the target instance/profile absence barrier complete.
        try:
            container.sync()
            if container.status != 'Stopped':
                container.stop(timeout=-1, force=True, wait=True)
        except Exception:
            LOG.critical(
                'Failed to fence the source during live migration rollback; '
                'operator fencing is required',
                instance=instance, exc_info=True)

    def finalize_live_migration_rollback(
            self, context, instance, migrate_data):
        """Reassert source VIFs after target Neutron bindings are removed."""
        destination_address = _live_migration_destination_address(
            migrate_data)
        if not destination_address:
            raise exception.MigrationError(
                reason='Missing live migration destination address')
        cleanup_token = _live_migration_cleanup_token(migrate_data)
        idmap_base, idmap_size = _live_migration_idmap(migrate_data)
        remote = _migration_client(destination_address)

        release_pending = False
        if self.idmap_allocator is not None:
            release_intent = self.idmap_allocator.get_release_intent(
                instance.uuid)
            if release_intent is not None:
                if release_intent.instance_name != instance.name:
                    raise exception.MigrationError(
                        reason='Incus live rollback idmap release owner '
                               'changed')
                release_pending = True

        try:
            attempt = _get_migration_attempt(
                remote, instance, cleanup_token, idmap_base, idmap_size)
        except incus_exceptions.LXDAPIException as exc:
            if not _is_incus_not_found(exc):
                raise
            # Destination rollback retires its terminal attempt after it has
            # fenced and cleaned the target.  Its asynchronous RPC can win
            # that race before the source finalizer starts.  Absence alone
            # does not authorize source recovery; the target/profile barrier
            # below still has to prove destination cleanup.
            attempt = None
        if attempt is not None and attempt['state'] == 'active':
            attempt = _abort_migration_attempt(
                remote, instance, cleanup_token,
                idmap_base, idmap_size,
                target_cleanup=lambda: _retry_migration_finish_action(
                    lambda: self._delete_migration_target_with_idmap(
                        remote, instance),
                    'aborted live migration target deletion', instance))
        elif (attempt is not None and
              attempt['state'] in ('aborted', 'failed')):
            attempt = _wait_migration_attempt_finished(
                remote, instance, cleanup_token,
                idmap_base, idmap_size, ('aborted', 'failed'))
        elif (attempt is not None and
              attempt['state'] != 'committed'):
            raise exception.MigrationError(
                reason='Unsupported live rollback attempt state %s' %
                attempt['state'])
        _settle_instance_migration_operations(
            self.client, instance,
            operation_ids=(
                getattr(migrate_data, 'source_operation_id', None),))

        with lockutils.lock(_profile_lock_name(instance)):
            source_profile = self.client.profiles.get(instance.name)
            source_config = (
                source_profile.config
                if isinstance(source_profile.config, dict) else {})
            rollback_already_complete = (
                source_config.get(MIGRATION_ROLLBACK_COMPLETE_KEY) ==
                cleanup_token)
        if rollback_already_complete:
            container = self.client.instances.get(instance.name)
            if (container.status != 'Running' and
                    not (release_pending and
                         container.status == 'Stopped')):
                raise exception.MigrationError(
                    reason='Incus rollback completion marker exists but the '
                           'source instance is not running')
            try:
                profile = remote.profiles.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if not _is_incus_not_found(exc):
                    raise
            else:
                self._validate_remote_cleanup_acknowledgement(
                    profile, instance, cleanup_token,
                    idmap_base, idmap_size)
                profile.delete()
            _retire_migration_attempt(
                remote, instance, cleanup_token,
                idmap_base, idmap_size)
            return

        def _target_cleanup_complete():
            try:
                remote.instances.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if not _is_incus_not_found(exc):
                    raise
            else:
                raise _MigrationStateNotReady(
                    'Incus live migration destination instance cleanup is '
                    'still in progress')
            try:
                profile = remote.profiles.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if not _is_incus_not_found(exc):
                    raise
                if (attempt is not None and
                        attempt['state'] == 'committed'):
                    raise exception.MigrationError(
                        reason='Committed Incus live migration destination '
                               'cleanup has no positive acknowledgement')
                return None
            # The destination rollback strips devices and stamps the
            # completion token asynchronously; a profile that fails the
            # acknowledgement checks mid-cleanup is not yet terminal. The
            # barrier deadline turns a persistent mismatch into a failure.
            try:
                self._validate_remote_cleanup_acknowledgement(
                    profile, instance, cleanup_token,
                    idmap_base, idmap_size)
            except exception.MigrationError as exc:
                raise _MigrationStateNotReady(str(exc))
            return profile

        # Destination rollback is an asynchronous Nova RPC. Require its
        # token-bound positive acknowledgement before changing ownership.
        _wait_migration_finish_condition(
            _target_cleanup_complete,
            'live migration destination cleanup', instance)
        _retry_migration_finish_action(
            lambda: _restore_source_storage_ownership(
                self.client, instance),
            'rolled-back source shared-storage ownership', instance)

        network_info = network_model.NetworkInfo()
        if ('vifs' in migrate_data and migrate_data.vifs):
            network_info = network_model.NetworkInfo([
                vif.source_vif for vif in migrate_data.vifs
                if ('source_vif' in vif and vif.source_vif)
            ])
        if network_info and not release_pending:
            vif_ids = {vif['id'] for vif in network_info}

            def _vifs_have_active_state(expected):
                current = self.network_api.get_instance_nw_info(
                    context, instance)
                states = {
                    vif['id']: bool(vif.get('active'))
                    for vif in current if vif['id'] in vif_ids
                }
                if (set(states) != vif_ids or
                        any(state != expected for state in states.values())):
                    raise _MigrationStateNotReady(
                        'Neutron VIFs have not reached rollback state %s' %
                        ('ACTIVE' if expected else 'DOWN'))

            def _reassert_vifs():
                for vif in network_info:
                    self.vif_driver.reassert(instance, vif)

        container = self.client.instances.get(instance.name)
        container.sync()
        if container.status != 'Running' and not release_pending:
            # A failed target restore can remove one side of the retained veth
            # pair. Rebuild the complete source wiring while the container is
            # stopped; Incus otherwise fails its start on the missing parent.
            self._refresh_vifs(instance, network_info)
            _retry_migration_finish_action(
                lambda: self._start_instance_with_idmap(
                    instance, container),
                'rolled-back source container start', instance)

        if network_info and not release_pending:
            # CRIU rollback has already resumed the source container with its
            # original veth peer when it never stopped. Rebuild only the OVS
            # Port row in that case; the stopped case recreated the whole pair
            # above before the container start.
            _retry_migration_finish_action(
                _reassert_vifs,
                'live migration rollback VIF wiring', instance)
            _wait_migration_finish_condition(
                lambda: _vifs_have_active_state(True),
                'live migration source VIF activation', instance)

        # Persist the source-side commit before deleting the destination ACK.
        # A retry after that deletion can then prove that ownership, runtime
        # and VIF restoration had already completed.
        with lockutils.lock(_profile_lock_name(instance)):
            source_profile = self.client.profiles.get(instance.name)
            source_profile.config[MIGRATION_CLEANUP_TOKEN_KEY] = (
                cleanup_token)
            source_profile.config[MIGRATION_DESTINATION_KEY] = (
                destination_address)
            source_profile.config[MIGRATION_ROLLBACK_COMPLETE_KEY] = (
                cleanup_token)
            source_profile.config[MIGRATION_NOVA_UUID_KEY] = (
                _live_migration_uuid(migrate_data))
            source_profile.config.pop(MIGRATION_OPERATION_KEY, None)
            source_profile.save(wait=True)

        # Keep the acknowledgement until source ownership, runtime and VIF
        # state are all restored. Deleting it last makes finalize idempotent.
        def _delete_acknowledgement():
            try:
                profile = remote.profiles.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if _is_incus_not_found(exc):
                    return
                raise
            self._validate_remote_cleanup_acknowledgement(
                profile, instance, cleanup_token,
                idmap_base, idmap_size)
            profile.delete()

        _retry_migration_finish_action(
            _delete_acknowledgement,
            'live migration cleanup acknowledgement deletion', instance)
        _retire_migration_attempt(
            remote, instance, cleanup_token,
            idmap_base, idmap_size)

    def recover_live_migration_rollback(
            self, context, instance, operation_token, migration_uuid,
            network_info):
        """Resume an exact failed live rollback from its source profile."""
        with lockutils.lock(_profile_lock_name(instance)):
            profile = self.client.profiles.get(instance.name)
            _validate_profile_volume_owner(profile, instance)
            config = profile.config if isinstance(profile.config, dict) else {}
            if (config.get(MIGRATION_CLEANUP_TOKEN_KEY) != operation_token or
                    config.get(MIGRATION_NOVA_UUID_KEY) != migration_uuid or
                    config.get(MIGRATION_ROLLBACK_COMPLETE_KEY) is not None):
                raise exception.MigrationError(
                    reason='Incus live rollback generation changed')
            destination_address = config.get(MIGRATION_DESTINATION_KEY)
            if not destination_address:
                raise exception.MigrationError(
                    reason='Incus live rollback has no destination address')
            idmap_base, idmap_size = _instance_migration_idmap(None, profile)
            source_operation_id = config.get(MIGRATION_OPERATION_KEY)

        vifs = [
            nova_migrate_data.VIFMigrateData(source_vif=vif)
            for vif in network_info or []
        ]
        data = incus_migrate_data.IncusLiveMigrateData(
            destination_address=destination_address,
            cleanup_token=operation_token,
            migration_uuid=migration_uuid,
            source_operation_id=source_operation_id,
            idmap_base=idmap_base,
            idmap_size=idmap_size,
            vifs=vifs)
        self.finalize_live_migration_rollback(context, instance, data)
        return True

    @_invalidates_instance_inventory
    def rollback_live_migration_at_destination(
            self, context, instance, network_info, block_device_info,
            destroy_disks=True, migrate_data=None):
        cleanup_token = _live_migration_cleanup_token(migrate_data)
        idmap_base, idmap_size = _live_migration_idmap(migrate_data)
        try:
            profile = self.client.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if _is_incus_not_found(exc):
                profile = None
            else:
                raise
        pre_receive = (
            profile is None and
            getattr(migrate_data, 'source_operation_id', None) is None and
            getattr(
                migrate_data, 'destination_operation_id', None) is None)
        if pre_receive:
            # The manager stages Manila before driver.pre_live_migration().
            # Its first mount can therefore fail before an Incus profile,
            # receive operation or even an attempt exists. This rollback is
            # complete once the exact token journal and any matching attempt
            # are gone; an ACK profile would invent a resource solely to
            # acknowledge that no receive ever started.
            self._abort_pre_live_migration_preparation(
                instance, migrate_data)
            return
        failures = []
        _abort_migration_attempt(
            self.client, instance, cleanup_token,
            idmap_base, idmap_size,
            target_cleanup=lambda: _retry_migration_finish_action(
                lambda: self._delete_migration_target_with_idmap(
                    self.client, instance),
                'aborted live migration target deletion', instance))
        try:
            _retry_migration_finish_action(
                lambda: self._delete_migration_target_with_idmap(
                    self.client, instance),
                'live migration destination record deletion', instance)
        except Exception as exc:
            try:
                self.client.instances.get(instance.name)
            except incus_exceptions.LXDAPIException as verify_exc:
                if not _is_incus_not_found(verify_exc):
                    raise
                LOG.warning(
                    'Incus target deletion reported failure but the target '
                    'record is absent; continuing destination cleanup',
                    instance=instance)
            else:
                # Do not tear storage or networking away from a target that
                # may still be running. Its profile deliberately keeps the
                # source-side completion barrier closed.
                raise exception.MigrationError(
                    reason='Failed to fence the live migration target: %s' %
                    exc) from exc
        if profile is not None:
            try:
                with lockutils.lock(_profile_lock_name(instance)):
                    _cleanup_profile_share_mounts(profile, instance)
            except Exception as exc:
                failures.append(exc)
                LOG.exception(
                    'Failed to clean destination Manila mounts during live '
                    'migration rollback',
                    instance=instance)
        # cleanup() unplugs destination VIFs and disconnects block devices
        # before deleting the profile. The source uses profile absence as the
        # completion barrier before it reasserts its retained VIFs.
        try:
            self._cleanup(
                context, instance, network_info,
                destroy_disks=destroy_disks,
                destroy_vifs=True,
                delete_profile=False)
        except Exception as exc:
            failures.append(exc)
            LOG.exception(
                'Failed to clean destination Incus resources during live '
                'migration rollback',
                instance=instance)
        if failures:
            raise exception.MigrationError(
                reason='%d live migration destination cleanup operation(s) '
                       'failed; the profile was retained as a safety barrier'
                       % len(failures))

        try:
            self._acknowledge_cleanup_profile(instance, cleanup_token)
        except incus_exceptions.LXDAPIException as exc:
            if _is_incus_not_found(exc):
                raise exception.MigrationError(
                    reason='Incus destination cleanup profile disappeared '
                           'before the positive acknowledgement') from exc
            raise

    def check_can_live_migrate_destination(
            self, context, instance, src_compute_info, dst_compute_info,
            block_migration=False, disk_over_commit=False):
        if getattr(instance, 'host', None) == self.host:
            raise exception.MigrationPreCheckError(
                reason='Incus live migration to the source compute is not '
                       'supported')
        if not CONF.incus.allow_live_migration:
            raise exception.MigrationPreCheckError(
                reason='Incus live migration is disabled by configuration')
        if block_migration:
            raise exception.MigrationPreCheckError(
                reason='Incus CRIU block migration is not supported')
        address = CONF.incus.migration_address
        _validated_migration_address(address)
        _require_stateful_migration_extension(self.client)
        _require_live_ceph_migration_extension(self.client)
        try:
            _require_migration_attempt_fencing(self.client)
        except exception.MigrationError as exc:
            raise exception.MigrationPreCheckError(reason=str(exc))
        facts = _migration_host_facts(self.client)
        cleanup_token = uuidutils.generate_uuid()
        return incus_migrate_data.IncusLiveMigrateData(
            destination_address=address,
            destination_architecture=facts['architecture'],
            destination_kernel_version=facts['kernel_version'],
            destination_server_version=facts['server_version'],
            cleanup_token=cleanup_token,
            # Nova does not persist dest_compute until after this driver hook.
            # The manager hook replaces this placeholder with the authoritative
            # Migration.uuid before the result leaves the destination compute.
            migration_uuid=cleanup_token,
            source_operation_id=None,
            destination_operation_id=None)

    def cleanup_live_migration_destination_check(
            self, context, dest_check_data):
        return

    def check_can_live_migrate_source(self, context, instance,
                                      dest_check_data, block_device_info=None):
        if not CONF.incus.allow_live_migration:
            raise exception.MigrationPreCheckError(
                reason='Incus live migration is disabled by configuration')
        if not isinstance(
                dest_check_data, incus_migrate_data.IncusLiveMigrateData):
            raise exception.MigrationPreCheckError(
                reason='Destination did not return Incus migration data')
        try:
            cleanup_token = _live_migration_cleanup_token(dest_check_data)
        except exception.MigrationError as exc:
            raise exception.MigrationPreCheckError(reason=str(exc))
        if instance.config_drive:
            raise exception.MigrationPreCheckError(
                reason='Incus live migration does not support config drives')
        root_bdm = _boot_from_volume(block_device_info)
        if root_bdm is not None:
            root_volume = _require_bfv_live_migration_support(
                self.client, root_bdm)
            destination = _validated_migration_address(
                dest_check_data.destination_address).hostname
            try:
                _preflight_bfv_migration_destination(
                    destination, root_volume[0], live=True)
            except exception.MigrationError as exc:
                raise exception.MigrationPreCheckError(reason=str(exc))
        else:
            root_pool = _instance_root_pool(self.client, instance.name)
            if root_pool.driver == 'ceph':
                extensions = set(
                    self.client.host_info.get('api_extensions', []))
                required = {
                    INCUS_STORAGE_HANDOVER_EXTENSION,
                    INCUS_STORAGE_HANDOVER_PROOF_EXTENSION,
                    INCUS_STORAGE_READY_FENCE_EXTENSION,
                }
                missing = sorted(required - extensions)
                if missing:
                    raise exception.MigrationPreCheckError(
                        reason='Incus source does not advertise required '
                        'shared Ceph handover extensions: %s' %
                        ', '.join(missing))
                destination = _validated_migration_address(
                    dest_check_data.destination_address).hostname
                try:
                    _preflight_shared_ceph_handover_destination(
                        destination, root_pool.name,
                        _storage_pool_identity(root_pool))
                except exception.MigrationError as exc:
                    raise exception.MigrationPreCheckError(reason=str(exc))

        _require_stateful_migration_extension(self.client)
        _require_live_ceph_migration_extension(self.client)
        with lockutils.lock(_profile_lock_name(instance)):
            container, profile = _full_checkpoint_live_migration_source(
                self.client, context, instance, block_device_info,
                normalize_incremental_memory=True)
        idmap_base, idmap_size = _instance_migration_idmap(
            container, profile)
        try:
            self._ensure_instance_idmap(
                instance, observed_base=idmap_base,
                observed_size=idmap_size)
        except incus_idmap.IDMapError as exc:
            raise exception.MigrationPreCheckError(
                reason='Incus source idmap is not globally reserved: '
                       '{}'.format(exc)) from exc
        source_facts = _migration_host_facts(self.client)
        comparisons = (
            ('architecture', source_facts['architecture'],
             dest_check_data.destination_architecture),
            ('kernel version', source_facts['kernel_version'],
             dest_check_data.destination_kernel_version),
            ('Incus version', source_facts['server_version'],
             dest_check_data.destination_server_version),
        )
        incompatible = [
            '%s source=%s destination=%s' % values
            for values in comparisons if values[1] != values[2]
        ]
        if incompatible:
            raise exception.MigrationPreCheckError(
                reason='Incus CRIU migration host mismatch: %s' %
                '; '.join(incompatible))
        remote = _migration_client(dest_check_data.destination_address)
        try:
            _register_migration_attempt(
                remote, instance, cleanup_token,
                idmap_base, idmap_size)
        except Exception as exc:
            raise exception.MigrationPreCheckError(
                reason='Cannot reserve the live migration target: %s' %
                exc) from exc
        try:
            with lockutils.lock(_profile_lock_name(instance)):
                container, profile = _full_checkpoint_live_migration_source(
                    self.client, context, instance, block_device_info)
                current_idmap = _instance_migration_idmap(container, profile)
                if current_idmap != (idmap_base, idmap_size):
                    raise exception.MigrationPreCheckError(
                        reason='Incus source idmap changed during live '
                               'migration pre-check')
                profile.config['user.openstack.uuid'] = instance.uuid
                profile.config[MIGRATION_DESTINATION_KEY] = (
                    dest_check_data.destination_address)
                profile.config[MIGRATION_CLEANUP_TOKEN_KEY] = cleanup_token
                profile.config.pop(MIGRATION_CLEANUP_COMPLETE_KEY, None)
                profile.config.pop(MIGRATION_ROLLBACK_COMPLETE_KEY, None)
                profile.save(wait=True)
                source_profile = _live_migration_source_profile(
                    container, profile)
                dest_check_data.source_profile = source_profile
                dest_check_data.idmap_base = idmap_base
                dest_check_data.idmap_size = idmap_size
                # Set only after the locked second validation and successful
                # source-profile serialization. Object compatibility strips
                # this field for a pre-1.6 destination.
                dest_check_data.full_checkpoint_verified = True
        except Exception:
            with excutils.save_and_reraise_exception():
                attempt = _abort_migration_attempt(
                    remote, instance, cleanup_token,
                    idmap_base, idmap_size)
                if attempt['state'] != 'committed':
                    _retire_migration_attempt(
                        remote, instance, cleanup_token,
                        idmap_base, idmap_size)
        return dest_check_data

    #
    # IncusDriver "private" implementation methods
    #
    # XXX: rockstar (21 Nov 2016) - The methods and code below this line
    # have not been through the cleanup process. We know the cleanup process
    # is complete when there is no more code below this comment, and the
    # comment can be removed.
    @staticmethod
    def _umount_configdrive_iso(mountpoint, instance):
        """Release the config-drive ISO, retrying a transient busy unmount.

        A leaked mount holds a loop device and keeps its own directory
        undeletable. It can no longer block instance deletion - the
        mountpoint sits outside the instance directory - so failing the
        spawn over it would be the worse trade: the config drive has
        already been copied and the guest is fine. Retry the cases that
        pass on their own, then say so loudly and continue.
        """
        for attempt in range(1, _CONFIGDRIVE_UMOUNT_ATTEMPTS + 1):
            try:
                incus_privsep.configdrive_umount(mountpoint, 60)
                return
            except Exception as exc:
                if attempt == _CONFIGDRIVE_UMOUNT_ATTEMPTS:
                    LOG.error(
                        'Could not unmount the config drive ISO at '
                        '%(path)s after %(attempts)d attempts (%(error)s); '
                        'a loop mount is left behind and needs an operator '
                        'umount',
                        {'path': mountpoint,
                         'attempts': _CONFIGDRIVE_UMOUNT_ATTEMPTS,
                         'error': exc}, instance=instance)
                    return
                LOG.warning(
                    'Unmounting the config drive ISO at %(path)s failed '
                    '(%(error)s); retrying',
                    {'path': mountpoint, 'error': exc}, instance=instance)
                eventlet.sleep(_CONFIGDRIVE_UMOUNT_RETRY_SECONDS)

    def _add_configdrive(self, context, instance,
                         injected_files, admin_password, network_info):
        """Create configdrive for the instance."""
        if CONF.config_drive_format != 'iso9660':
            raise exception.ConfigDriveUnsupportedFormat(
                format=CONF.config_drive_format)

        container = self.client.instances.get(instance.name)
        storage_id = _container_root_host_id(container)

        extra_md = {}
        if admin_password:
            extra_md['admin_pass'] = admin_password

        inst_md = instance_metadata.InstanceMetadata(
            instance, content=injected_files, extra_md=extra_md,
            network_info=network_info)

        iso_path = os.path.join(
            common.InstanceAttributes(instance).instance_dir,
            'configdrive.iso')

        with configdrive.ConfigDriveBuilder(instance_md=inst_md) as cdb:
            try:
                cdb.make_drive(iso_path)
            except processutils.ProcessExecutionError as e:
                with excutils.save_and_reraise_exception():
                    LOG.error("Creating config drive failed with error: {}"
                              .format(e), instance=instance)

        configdrive_dir = os.path.join(
            nova.conf.CONF.instances_path, instance.name, 'configdrive')
        if not os.path.exists(configdrive_dir):
            fileutils.ensure_tree(configdrive_dir)

        # The mountpoint lives under instances_path, not the system temp
        # directory, because the privileged mount and umount entrypoints
        # constrain both of their paths there - that is what keeps a
        # compromised nova user from mounting a self-made image over a
        # system directory. It deliberately does not live inside the
        # instance directory: a mount that outlives this block would make
        # that instance permanently undeletable, since its removal path
        # chowns, walks and rmtree's the tree and each of those fails on a
        # live read-only mount.
        mount_root = os.path.join(
            nova.conf.CONF.instances_path, _CONFIGDRIVE_MOUNT_DIR)
        os.makedirs(mount_root, exist_ok=True)
        with utils.tempdir(dir=mount_root) as tmpdir:
            mounted = False
            try:
                # Dedicated privsep entrypoints replace the three
                # nova-rootwrap invocations here, which each cold-started a
                # Python interpreter and required unconstrained
                # mount/umount/chown CommandFilters that undid privsep's
                # path validation at the deployment layer.
                incus_privsep.configdrive_mount_iso(
                    iso_path, tmpdir, os.getuid(), os.getgid(), 60)
                mounted = True

                # Copy and adjust the files from the ISO so that we
                # dont have the ISO mounted during the life cycle of the
                # instance and the directory can be removed once the instance
                # is terminated
                for ent in os.listdir(tmpdir):
                    shutil.copytree(os.path.join(tmpdir, ent),
                                    os.path.join(configdrive_dir, ent))

                for root, dirs, files in os.walk(
                        configdrive_dir, topdown=False):
                    for name in files:
                        os.chmod(os.path.join(root, name), 0o400)
                    for name in dirs:
                        os.chmod(os.path.join(root, name), 0o500)
                    os.chmod(root, 0o500)
                incus_privsep.chown_tree_to_host_id(
                    configdrive_dir, storage_id)
            finally:
                if mounted:
                    self._umount_configdrive_iso(tmpdir, instance)

        return configdrive_dir
