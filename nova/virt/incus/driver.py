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
import errno
import glob
import io
import os
import platform
import re
import shutil
import socket
import tarfile
import tempfile
import hashlib
from urllib import parse

import eventlet
import nova.conf
import nova.context
import os_resource_classes as orc
from contextlib import closing

from nova import exception
from nova import i18n
from nova.console import type as console_type
from nova.image import glance
from nova.network import neutron
from nova.network import model as network_model
from nova import objects
from nova.privsep import fs as privsep_fs
from nova.privsep import path as privsep_path
from nova.virt import driver
from os_brick.initiator import connector
from oslo_concurrency import processutils
from oslo_log import log as logging
from oslo_utils import fileutils
from oslo_utils import timeutils
from prometheus_client import parser as prometheus_parser
from pylxd import exceptions as incus_exceptions

from nova.virt.incus import vif as incus_vif
from nova.virt.incus import client as incus_client
from nova.virt.incus import common
from nova.virt.incus import console as incus_console
from nova.virt.incus import flavor
from nova.virt.incus import storage

from nova.api.metadata import base as instance_metadata
from nova.objects import fields as obj_fields
from nova.virt import configdrive
from nova.compute import power_state
from nova.compute import vm_states
from nova.virt import hardware
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
NOVA_CONF = nova.conf.CONF

ACCEPTABLE_IMAGE_FORMATS = {'raw', 'root-tar', 'squashfs'}
INCUS_SYSTEM_CONTAINER_TRAIT = 'CUSTOM_INCUS_SYSTEM_CONTAINER'
INCUS_SWAP_TRAIT = 'CUSTOM_INCUS_SWAP'
INCUS_MANILA_SHARE_TRAIT = 'CUSTOM_INCUS_MANILA_SHARE'
MIGRATION_RECOVERY_KEY = 'user.openstack.recovery_required'
BASE_DIR = os.path.join(
    CONF.instances_path, CONF.image_cache.subdirectory_name)

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
        'pool': CONF.incus.boot_from_volume_storage_pool,
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
        'storage_driver_cephext',
    }
    missing = sorted(required.difference(extensions))
    if missing:
        raise exception.MigrationError(
            reason='Incus BFV migration requires API extensions: %s' %
            ', '.join(missing))

    pool_name = CONF.incus.boot_from_volume_storage_pool
    if not pool_name:
        raise exception.InvalidConfiguration(
            '[incus] boot_from_volume_storage_pool is required for '
            'Cinder boot-from-volume migration')
    pool = client.storage_pools.get(pool_name)
    if pool.driver != 'cephext' or pool.config.get('source') != root_volume[0]:
        raise exception.MigrationError(
            reason='Incus BFV migration requires a cephext pool backed by '
            'the Cinder RBD pool')

    return root_volume


def _preflight_bfv_migration_destination(destination, cinder_pool):
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
            'storage_driver_cephext',
        }
        missing = sorted(required.difference(extensions))
        if missing:
            raise ValueError('missing API extensions: %s' % ', '.join(missing))

        project = remote.projects.get(CONF.incus.migration_preflight_project)
        readiness = project.config
        protocol = readiness.get('user.openstack.preflight_protocol')
        bfv_pool = readiness.get('user.openstack.bfv_pool')
        advertised_cinder_pool = readiness.get(
            'user.openstack.cinder_rbd_pool')
        if protocol != '1':
            raise ValueError('unsupported or missing readiness protocol')
        if bfv_pool != CONF.incus.boot_from_volume_storage_pool:
            raise ValueError('destination BFV pool does not match source')
        if advertised_cinder_pool != cinder_pool:
            raise ValueError(
                'destination Cinder RBD pool does not match root volume')

        pool = remote.storage_pools.get(bfv_pool)
        if pool.driver != 'cephext':
            raise ValueError('destination BFV pool is not cephext')
    except Exception as exc:
        raise exception.MigrationError(
            reason='Incus BFV destination readiness check failed: %s' % exc)


def _volume_device_info_key(volume_id):
    return 'user.openstack.volume.%s' % volume_id


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


def _disk_metric_device(profile, instance, disk_id):
    """Map a Nova disk ID to the host block name used by Incus metrics."""
    normalized = str(disk_id).removeprefix('/dev/')
    for device in profile.devices.values():
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

    root = profile.devices.get('root', {})
    image_name = root.get('initial.ceph.rbd.image_name')
    if not image_name or not _CINDER_RBD_IMAGE_RE.fullmatch(image_name):
        return None
    candidates = glob.glob('/dev/rbd/*/%s' % image_name)
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


def _incus_disk_metrics(client, instance_name):
    """Return cumulative cgroup block counters keyed by host device name."""
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
            if field is None or sample.labels.get('name') != instance_name:
                continue
            device = sample.labels.get('device')
            if not device:
                continue
            metrics.setdefault(device, {})[field] = int(sample.value)
    return metrics


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


def _retry_migration_finish_action(action, description, instance):
    """Retry a target-side migration action without weakening ownership."""
    attempts = CONF.incus.migration_finish_retries
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except Exception:
            if attempt == attempts:
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
    if not _SHARE_ID_RE.fullmatch(share_mapping.share_id):
        raise exception.ShareMountError(
            share_id=share_mapping.share_id,
            server_id=instance.uuid,
            reason='share ID is not a canonical UUID')
    return os.path.join(
        CONF.instances_path, 'incus-shares', instance.uuid,
        share_mapping.share_id)


def _share_device_name(share_mapping):
    return 'manila-' + share_mapping.share_id


def _share_guest_path(share_mapping):
    if not _SHARE_TAG_RE.fullmatch(share_mapping.tag):
        raise exception.ShareMountError(
            share_id=share_mapping.share_id,
            server_id=share_mapping.instance_uuid,
            reason='share tag contains unsupported characters')
    return '/mnt/manila/' + share_mapping.tag


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
    processutils.execute(
        'chown', '-R', '%s:%s' % (storage_id, storage_id), staging,
        run_as_root=True, root_helper=utils.get_root_helper())
    os.replace(staging, destination)
    return destination


def _profile_device_info(profile, volume_id, device):
    encoded = profile.config.get(_volume_device_info_key(volume_id))
    if encoded:
        try:
            device_info = jsonutils.loads(encoded)
        except (TypeError, ValueError) as exc:
            raise exception.InvalidVolume(
                reason='Stored os-brick device information is invalid: %s' %
                exc)
        if not isinstance(device_info, dict):
            raise exception.InvalidVolume(
                reason='Stored os-brick device information is not a mapping')
        return device_info
    return {'path': device['source']} if device else None


def _incus_cloud_init_config(instance):
    """Translate Nova bootstrap data to the Incus NoCloud template keys."""
    config = {'user.openstack.uuid': instance.uuid}

    user_data = getattr(instance, 'user_data', None)
    if user_data:
        if isinstance(user_data, str):
            user_data = user_data.encode('ascii')
        config['cloud-init.user-data'] = base64.b64decode(user_data).decode(
            'utf-8')

    key_data = getattr(instance, 'key_data', None)
    if key_data:
        key_name = getattr(instance, 'key_name', None) or 'nova-key'
        config['user.meta-data'] = 'public-keys:\n  %s: %s\n' % (
            jsonutils.dumps(key_name), jsonutils.dumps(key_data.strip()))

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


def _get_storage_pool_info(client, pool_name):
    """Return capacity reported by the configured Incus storage pool."""
    resources = client.storage_pools.get(pool_name).resources.get()
    total = resources.space['total']
    used = resources.space['used']
    return {
        'total': total,
        'used': used,
        'available': max(0, total - used),
    }


def _get_power_state(incus_state):
    """Take a incus state code and translate it to nova power state."""
    state_map = [
        (power_state.RUNNING, {100, 101, 103, 200}),
        (power_state.SHUTDOWN, {102, 104, 107}),
        (power_state.NOSTATE, {105, 106, 401}),
        (power_state.CRASHED, {108, 400}),
        (power_state.SUSPENDED, {109, 110, 111}),
    ]
    for nova_state, incus_states in state_map:
        if incus_state in incus_states:
            return nova_state
    raise ValueError('Unknown Incus power state: {}'.format(incus_state))


def _sync_glance_image_to_incus(client, context, image_ref):
    """Sync an image from glance to Incus image store.

    The image from glance can't go directly into the Incus image store,
    as Incus needs some extra metadata connected to it.

    The image is stored in the Incus image store with an alias to
    the image_ref. This way, it will only copy over once.
    """
    lock_path = os.path.join(CONF.instances_path, 'locks')
    with lockutils.lock(
            lock_path, external=True,
            lock_file_prefix='incus-image-{}'.format(image_ref)):

        # NOTE(jamespage): Re-query by image_ref to ensure
        #                  that another process did not
        #                  sneak infront of this one and create
        #                  the same image already.
        try:
            client.images.get_by_alias(image_ref)
            return
        except incus_exceptions.LXDAPIException as e:
            if e.response.status_code != 404:
                raise

        try:
            ifd, image_file = tempfile.mkstemp()
            mfd, manifest_file = tempfile.mkstemp()

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
            os.close(ifd)
            os.close(mfd)
            os.unlink(image_file)
            os.unlink(manifest_file)


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


class IncusDriver(driver.ComputeDriver):
    """A Incus driver for nova.

    Incus is a system container hypervisor. IncusDriver provides Incus
    functionality to nova. For more information about Incus, see
    http://www.ubuntu.com/cloud/incus
    """

    capabilities = dict(
        driver.ComputeDriver.capabilities,
        has_imagecache=False,
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
        self.host = NOVA_CONF.host
        self.network_api = neutron.API()
        self.vif_driver = incus_vif.IncusGenericVifDriver()
        self.firewall_driver = _NeutronFirewallDriver()
        self._serial_consoles = {}

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
        try:
            self.client = incus_client.get_client(CONF)
        except incus_exceptions.ClientConnectionFailed as e:
            msg = _("Unable to connect to Incus daemon: {}").format(e)
            raise exception.HostNotFound(msg)
        self._after_reboot()

    def cleanup_host(self, host):
        """Clean up the host.

        `nova.virt.ComputeDriver` defines this method.

        See `nova.virt.driver.ComputeDriver.cleanup_host` for more
        information.
        """
        for broker in self._serial_consoles.values():
            broker.close()
        self._serial_consoles.clear()

    def get_info(self, instance, use_cache=True):
        """Return an InstanceInfo object for the instance."""
        try:
            container = self.client.instances.get(instance.name)
        except incus_exceptions.NotFound:
            raise exception.InstanceNotFound(instance_id=instance.uuid)

        if container.status == 'Stopped':
            return hardware.InstanceInfo(state=power_state.SHUTDOWN)

        state = container.state()
        return hardware.InstanceInfo(
            state=_get_power_state(state.status_code))

    def _get_diagnostics_data(self, instance):
        """Return accurately attributable counters from the Incus state API."""
        try:
            container = self.client.instances.get(instance.name)
        except incus_exceptions.NotFound:
            raise exception.InstanceNotFound(instance_id=instance.uuid)

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
        if not hasattr(obj_fields.HypervisorDriver, 'Incus'):
            raise NotImplementedError(
                'Nova must include the incus diagnostics driver identifier')

        data = self._get_diagnostics_data(instance)
        diags = objects.Diagnostics(
            state=power_state.STATE_MAP[data['state']],
            driver=obj_fields.HypervisorDriver.Incus,
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
            profile = self.client.profiles.get(instance.name)
            metric_device = _disk_metric_device(
                profile, instance, disk_id)
            if metric_device is None:
                return None
            counters = _incus_disk_metrics(
                self.client, instance.name).get(metric_device)
        except incus_exceptions.NotFound:
            LOG.info(
                'Cannot get block stats for missing instance %s',
                instance.name, instance=instance)
            return None

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
        usage = []
        for instance_bdms in compute_host_bdms:
            instance = instance_bdms['instance']
            try:
                profile = self.client.profiles.get(instance.name)
                disk_metrics = _incus_disk_metrics(
                    self.client, instance.name)
            except incus_exceptions.NotFound:
                LOG.info(
                    'Skipping volume usage for missing instance %s',
                    instance.name, instance=instance)
                continue
            for bdm in instance_bdms['instance_bdms']:
                metric_device = _disk_metric_device(
                    profile, instance, bdm['device_name'])
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
                    'volume': bdm['volume_id'],
                    'instance': instance,
                    'rd_req': stats[0],
                    'rd_bytes': stats[1],
                    'wr_req': stats[2],
                    'wr_bytes': stats[3],
                })
        return usage

    def list_instances(self):
        """Return a list of all instance names."""
        return [instance.name
                for instance in self.client.instances.all(recursion=1)
                if getattr(instance, 'type', 'container') == 'container']

    def list_instance_uuids(self):
        """Return UUIDs for Incus instances managed by this Nova driver."""
        uuids = []
        for instance in self.client.instances.all(recursion=1):
            if getattr(instance, 'type', 'container') != 'container':
                continue
            instance_uuid = getattr(instance, 'config', {}).get(
                'user.openstack.uuid')
            if instance_uuid:
                uuids.append(instance_uuid)
        return uuids

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
        if driver.block_device_info_get_ephemerals(block_device_info):
            raise exception.InvalidConfiguration(
                'Nova ephemeral disks are disabled until they can be backed '
                'by quota-controlled Incus storage volumes')

        root_bdm = _boot_from_volume(block_device_info)
        root_volume = None
        if root_bdm:
            root_volume = _cinder_rbd_root(root_bdm)
            bfv_pool_name = CONF.incus.boot_from_volume_storage_pool
            if not bfv_pool_name:
                raise exception.InvalidConfiguration(
                    '[incus] boot_from_volume_storage_pool is required for '
                    'Cinder boot-from-volume')
            extensions = self.client.host_info.get('api_extensions', [])
            if 'storage_driver_cephext' not in extensions:
                raise exception.InvalidConfiguration(
                    'The Incus server does not support storage_driver_cephext')
            bfv_pool = self.client.storage_pools.get(bfv_pool_name)
            if bfv_pool.driver != 'cephext':
                raise exception.InvalidConfiguration(
                    'Incus boot_from_volume_storage_pool must use cephext')
            if bfv_pool.config.get('source') != root_volume[0]:
                raise exception.InvalidConfiguration(
                    'The Incus cephext pool source does not match the Cinder '
                    'RBD pool')

        try:
            self.client.instances.get(instance.name)
            raise exception.InstanceExists(name=instance.name)
        except incus_exceptions.LXDAPIException as e:
            if e.response.status_code != 404:
                raise  # Re-raise the exception if it wasn't NotFound

        instance_dir = common.InstanceAttributes(instance).instance_dir
        if not os.path.exists(instance_dir):
            fileutils.ensure_tree(instance_dir)

        if not root_volume:
            # A Cinder root volume already contains the prepared rootfs.
            try:
                self.client.images.get_by_alias(instance.image_ref)
            except incus_exceptions.LXDAPIException as e:
                if e.response.status_code != 404:
                    raise
                _sync_glance_image_to_incus(
                    self.client, context, instance.image_ref)

        # Plug in the network
        if network_info:
            timeout = CONF.vif_plugging_timeout
            if timeout:
                events = [('network-vif-plugged', vif['id'])
                          for vif in network_info if not vif.get(
                    'active', True)]
            else:
                events = []

            try:
                with self.virtapi.wait_for_instance_event(
                        instance, events, timeout=timeout,
                        error_callback=_neutron_failed_callback):
                    self.plug_vifs(instance, network_info)
            except eventlet.timeout.Timeout:
                LOG.warn("Timeout waiting for vif plugging callback for "
                         "instance {uuid}"
                         .format(uuid=instance['name']))
                if CONF.vif_plugging_is_fatal:
                    self.destroy(
                        context, instance, network_info, block_device_info)
                    raise exception.InstanceDeployFailure(
                        'Timeout waiting for vif plugging',
                        instance_id=instance['name'])

        # Create the profile
        try:
            profile = flavor.to_profile(
                self.client, instance, network_info, block_device_info)
            if root_volume:
                profile.devices['root'] = _bfv_root_device(
                    instance, root_bdm, root_volume)
                profile.save()
        except incus_exceptions.LXDAPIException:
            with excutils.save_and_reraise_exception():
                self.cleanup(
                    context, instance, network_info, block_device_info)

        # Create the container
        container_config = {
            'name': instance.name,
            'type': 'container',
            'profiles': [profile.name],
            'config': {
                **_incus_cloud_init_config(instance),
                # Nova must reconcile ownership before a workload resumes
                # after a fenced compute returns.
                'boot.autostart': 'false',
            },
            'source': ({'type': 'none'} if root_volume else {
                'type': 'image', 'alias': instance.image_ref}),
        }
        try:
            container = self.client.instances.create(
                container_config, wait=True)
        except incus_exceptions.LXDAPIException:
            with excutils.save_and_reraise_exception():
                self.cleanup(
                    context, instance, network_info, block_device_info)

        try:
            incus_config = self.client.host_info
            storage.attach_ephemeral(
                self.client, block_device_info, incus_config, instance)
            if configdrive.required_by(instance):
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
                container.start(wait=True)

            self.firewall_driver.apply_instance_filter(
                instance, network_info)
        except Exception:
            with excutils.save_and_reraise_exception():
                try:
                    container.delete(wait=True)
                except Exception:
                    LOG.exception(
                        'Failed to delete container after a spawn failure',
                        instance=instance)
                try:
                    self.cleanup(
                        context, instance, network_info, block_device_info)
                except Exception as e:
                    LOG.warn('The cleanup process failed with: %s. This '
                             'error may or not may be relevant', e)

    def destroy(self, context, instance, network_info, block_device_info=None,
                destroy_disks=True, destroy_secrets=True):
        """Destroy a running instance.

        Since the profile and the instance are created on `spawn`, it is
        safe to delete them together.

        See `nova.virt.driver.ComputeDriver.destroy` for more
        information.
        """
        lock_path = os.path.join(CONF.instances_path, 'locks')
        broker = self._serial_consoles.pop(instance.uuid, None)
        if broker is not None:
            broker.close()

        with lockutils.lock(
                lock_path, external=True,
                lock_file_prefix='incus-container-{}'.format(instance.name)):
            # TODO(sahid): Each time we get a container we should
            # protect it by using a mutex.
            try:
                container = self.client.instances.get(instance.name)
                if container.status != 'Stopped':
                    container.stop(wait=True)
                container.delete(wait=True)
                if (instance.vm_state == vm_states.RESCUED):
                    rescued_container = self.client.instances.get(
                        '{}-rescue'.format(instance.name))
                    if rescued_container.status != 'Stopped':
                        rescued_container.stop(wait=True)
                    rescued_container.delete(wait=True)
            except incus_exceptions.LXDAPIException as e:
                if e.response.status_code == 404:
                    LOG.warning("Failed to delete instance. "
                                "Container does not exist for {instance}."
                                .format(instance=instance.name))
                else:
                    raise
            finally:
                self.cleanup(
                    context, instance, network_info, block_device_info)

    def cleanup(self, context, instance, network_info, block_device_info=None,
                destroy_disks=True, migrate_data=None, destroy_vifs=True,
                destroy_secrets=True):
        """Clean up the filesystem around the container.

        See `nova.virt.driver.ComputeDriver.cleanup` for more
        information.
        """
        if destroy_vifs:
            self.unplug_vifs(instance, network_info)
            self.firewall_driver.unfilter_instance(instance, network_info)

        incus_config = self.client.host_info
        storage.detach_ephemeral(self.client,
                                 block_device_info,
                                 incus_config,
                                 instance)

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
            try:
                profile = self.client.profiles.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if exc.response.status_code == 404:
                    break
                raise
            if _volume_id(connection_info) in profile.devices:
                self.detach_volume(
                    context, connection_info, instance, mountpoint)

        _remove_instance_directory(instance)

        try:
            self.client.profiles.get(instance.name).delete()
        except incus_exceptions.LXDAPIException as e:
            if e.response.status_code == 404:
                LOG.warning("Failed to delete instance. "
                            "Profile does not exist for {instance}."
                            .format(instance=instance.name))
            else:
                raise

    def cleanup_lingering_instance_resources(self, instance):
        """Remove a stopped record left on a failed migration source."""
        try:
            container = self.client.instances.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if exc.response.status_code == 404:
                return True
            LOG.exception(
                'Failed to query lingering Incus migration record',
                instance=instance)
            return False
        except Exception:
            LOG.exception(
                'Failed to query lingering Incus migration record',
                instance=instance)
            return False

        if container.status != 'Stopped':
            LOG.error(
                'Refusing to clean a running lingering Incus record',
                instance=instance)
            return False
        if container.config.get('user.openstack.uuid') != instance.uuid:
            LOG.error(
                'Refusing to clean an Incus record owned by another UUID',
                instance=instance)
            return False

        root = container.devices.get('root', {})
        external_root = root.get('initial.ceph.rbd.image_name')
        handover = container.config.get(
            'volatile.migration.storage_handover')
        if external_root and handover not in ('pending', 'committed'):
            LOG.error(
                'Refusing to clean an external BFV root without a protected '
                'handover state',
                instance=instance)
            return False

        try:
            container.delete(wait=True)
            try:
                self.client.profiles.get(instance.name).delete()
            except incus_exceptions.LXDAPIException as exc:
                if exc.response.status_code != 404:
                    raise
            return True
        except Exception:
            LOG.exception(
                'Failed to clean lingering Incus migration resources',
                instance=instance)
            return False

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
            container.start(wait=True)
        else:
            container.restart(force=True, wait=True)
        self._clear_migration_recovery_marker(instance)

    def needs_migration_recovery(self, instance):
        """Return whether this host owns a marked, stopped BFV target."""
        try:
            container = self.client.instances.get(instance.name)
            profile = self.client.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if exc.response.status_code == 404:
                return False
            raise

        root = container.devices.get('root', {})
        # storage_handover protects deletion of the source record and is not
        # present on the destination. The driver-owned profile marker is only
        # written after this target has definitely claimed the external root.
        checks = {
            'stopped': container.status == 'Stopped',
            'uuid_match': (
                container.config.get('user.openstack.uuid') == instance.uuid),
            'external_root': bool(
                root.get('initial.ceph.rbd.image_name')),
            'marked': profile.config.get(MIGRATION_RECOVERY_KEY) in (
                'true', 'running', 'stopped'),
        }
        LOG.debug(
            'Evaluated Incus BFV migration recovery candidate: %(checks)s',
            {'checks': checks}, instance=instance)
        return all(checks.values())

    def _mark_migration_recovery_required(
            self, instance, profile, power_on=True):
        profile.config[MIGRATION_RECOVERY_KEY] = (
            'running' if power_on else 'stopped')
        profile.save(wait=True)
        LOG.warning(
            'Marked claimed BFV target for automatic recovery',
            instance=instance)

    def recover_migration_target(
            self, context, instance, network_info, block_device_info=None):
        """Repair a marked BFV owner without changing its intended state."""
        profile = self.client.profiles.get(instance.name)
        desired_state = profile.config.get(MIGRATION_RECOVERY_KEY)
        if desired_state not in ('true', 'running', 'stopped'):
            raise exception.InvalidConfiguration(
                'Incus BFV recovery marker is missing or invalid')

        self._reconcile_reboot_data_volumes(
            context, instance, block_device_info)
        container = self.client.instances.get(instance.name)
        self._validate_reboot_vifs(instance, network_info)
        should_run = desired_state in ('true', 'running')
        was_running = container.status == 'Running'
        if network_info and was_running:
            container.stop(wait=True)
        self._refresh_vifs(instance, network_info)
        if should_run and (network_info or not was_running):
            container.start(wait=True)
        elif not should_run and was_running and not network_info:
            container.stop(wait=True)
        self._clear_migration_recovery_marker(instance)
        return should_run

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
            if exc.response.status_code == 404:
                return
            raise
        if (isinstance(profile.config, dict) and
                MIGRATION_RECOVERY_KEY in profile.config):
            profile.config.pop(MIGRATION_RECOVERY_KEY)
            profile.save(wait=True)

    def _reconcile_reboot_data_volumes(
            self, context, instance, block_device_info):
        """Restore data-volume devices before recovering a retained target."""
        profile = self.client.profiles.get(instance.name)
        container = self.client.instances.get(instance.name)
        root_bdm = _boot_from_volume(block_device_info)
        if root_bdm is not None and root_bdm.get('volume_size'):
            root_volume = _cinder_rbd_root(root_bdm)
            self._resize_bfv_root(
                container, root_volume[1],
                int(root_bdm['volume_size']) * units.Gi)

        seen = set()
        for bdm in driver.block_device_info_get_mapping(block_device_info):
            if _is_boot_volume(bdm):
                continue
            connection_info = bdm.get('connection_info')
            mountpoint = bdm.get('mount_device')
            if not connection_info or not mountpoint:
                continue

            volume_id = _volume_id(connection_info)
            if volume_id in seen:
                raise exception.InvalidVolume(
                    reason='Duplicate Cinder volume in reboot block mapping: '
                    '%s' % volume_id)
            seen.add(volume_id)

            device = profile.devices.get(volume_id)
            if device is None:
                self.attach_volume(
                    context, connection_info, instance, mountpoint)
                continue

            metadata_key = _volume_device_info_key(volume_id)
            if (device.get('type') != 'unix-block' or
                    device.get('path') != mountpoint or
                    metadata_key not in profile.config):
                raise exception.InvalidVolume(
                    reason='Incus profile data volume %s does not match the '
                    'Nova block-device mapping' % volume_id)
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
        profile = self.client.profiles.get(instance.name)
        for vif in network_info:
            device_name = incus_vif.get_vif_devname(vif)
            device = profile.devices.get(device_name)
            expected = {
                'type': 'nic',
                'nictype': 'physical',
                'parent': incus_vif.get_vif_internal_devname(vif),
                'hwaddr': str(vif['address']),
            }
            if device is None or any(
                    device.get(key) != value
                    for key, value in expected.items()):
                raise exception.InterfaceAttachFailed(
                    instance_uuid=instance.uuid)

    def get_console_output(self, context, instance):
        """Get the output of the container console.

        See `nova.virt.driver.ComputeDriver.get_console_output` for more
        information.
        """
        container = self.client.instances.get(instance.name)
        return container.console_log()[-MAX_CONSOLE_BYTES:]

    def get_serial_console(self, context, instance):
        """Return a token-protected Nova serialproxy backend."""
        if not CONF.serial_console.enabled:
            raise exception.ConsoleTypeUnavailable(console_type='serial')
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

    def attach_volume(self, context, connection_info, instance, mountpoint,
                      disk_bus=None, device_type=None, encryption=None):
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

        if encryption:
            raise exception.VolumeEncryptionNotSupported(
                volume_type=connection_info['driver_volume_type'],
                volume_id=_volume_id(connection_info))

        volume_id = _volume_id(connection_info)
        profile = self.client.profiles.get(instance.name)
        _validate_profile_volume_slot(profile, volume_id, mountpoint)

        protocol = connection_info['driver_volume_type']
        storage_driver = brick_get_connector(protocol)
        device_info = storage_driver.connect_volume(
            connection_info['data'])
        try:
            device_path = os.path.realpath(device_info['path'])
        except (KeyError, TypeError):
            try:
                storage_driver.disconnect_volume(
                    connection_info['data'], device_info)
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
            try:
                storage_driver.disconnect_volume(
                    connection_info['data'], device_info)
            except Exception:
                LOG.exception(
                    'Failed to roll back an invalid volume connection',
                    instance=instance)
            raise
        metadata_key = _volume_device_info_key(volume_id)
        try:
            profile.devices[volume_id] = {
                'path': mountpoint,
                'required': 'true',
                'source': device_path,
                'type': 'unix-block',
            }
            profile.devices[volume_id].update(qos_limits)
            profile.config[metadata_key] = _serialize_device_info(device_info)
            profile.save(wait=True)
        except Exception:
            with excutils.save_and_reraise_exception():
                profile.devices.pop(volume_id, None)
                profile.config.pop(metadata_key, None)
                storage_driver.disconnect_volume(
                    connection_info['data'], device_info)

    def detach_volume(self, context, connection_info, instance, mountpoint,
                      encryption=None):
        """Detach block device from a nova instance.

        First the volume id is deleted from the profile, and the
        profile is saved. The os-brick disconnects the volume
        from the host.

        See `nova.virt.driver.Computedriver.detach_volume` for
        more information.
        """
        try:
            profile = self.client.profiles.get(instance.name)
        except incus_exceptions.NotFound:
            if mountpoint != getattr(instance, 'root_device_name', None):
                raise
            # Nova destroys the Incus guest and profile before detaching a
            # BFV root for Cinder reimage. The cephext root was already
            # unmounted and never used the os-brick data-volume path.
            _cinder_rbd_root({'connection_info': connection_info})
            return
        volume_id = _volume_id(connection_info)
        device = profile.devices.get(volume_id)
        metadata_key = _volume_device_info_key(volume_id)
        encoded_device_info = profile.config.get(metadata_key)
        device_info = _profile_device_info(profile, volume_id, device)
        if device:
            del profile.devices[volume_id]
            profile.config.pop(metadata_key, None)
            profile.save(wait=True)

        protocol = connection_info['driver_volume_type']
        storage_driver = brick_get_connector(protocol)
        try:
            storage_driver.disconnect_volume(
                connection_info['data'], device_info)
        except Exception:
            if device:
                try:
                    profile.devices[volume_id] = device
                    if encoded_device_info is not None:
                        profile.config[metadata_key] = encoded_device_info
                    profile.save(wait=True)
                except Exception:
                    LOG.exception(
                        'Failed to restore the Incus volume device after a '
                        'host disconnect failure', instance=instance)
            raise

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

    def attach_interface(self, context, instance, image_meta, vif):
        self.vif_driver.plug(instance, vif)
        self.firewall_driver.setup_basic_filtering(instance, vif)

        profile = self.client.profiles.get(instance.name)

        net_device = incus_vif.get_vif_devname(vif)
        config_update = {
            net_device: {
                'nictype': 'physical',
                'hwaddr': vif['address'],
                'parent': incus_vif.get_vif_internal_devname(vif),
                'type': 'nic',
            }
        }

        profile.devices.update(config_update)
        profile.save(wait=True)

    def detach_interface(self, context, instance, vif):
        try:
            profile = self.client.profiles.get(instance.name)
            devname = incus_vif.get_vif_devname(vif)

            # NOTE(jamespage): Attempt to remove device using
            #                  new style tap naming
            if devname in profile.devices:
                del profile.devices[devname]
                profile.save(wait=True)
            else:
                # NOTE(jamespage): For upgrades, scan devices
                #                  and attempt to identify
                #                  using mac address as the
                #                  device will *not* have a
                #                  consistent name
                for key, val in profile.devices.items():
                    if val.get('hwaddr') == vif['address']:
                        del profile.devices[key]
                        profile.save(wait=True)
                        break
        except incus_exceptions.NotFound:
            # This method is called when an instance get destroyed. It
            # could happen that Nova to receive an event
            # "vif-delete-event" after the instance is destroyed which
            # result the incus profile not exist.
            LOG.debug("incus profile for instance {instance} does not exist. "
                      "The instance probably got destroyed before this method "
                      "got called.".format(instance=instance.name))

        self.vif_driver.unplug(instance, vif)

    def migrate_disk_and_power_off(
            self, context, instance, dest, flavor, network_info,
            block_device_info=None, timeout=0, retry_interval=0):
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

        container = self.client.instances.get(instance.name)
        configdrive_payload = None
        if instance.config_drive:
            configdrive_payload = _pack_configdrive_for_migration(
                instance, container)
        was_running = container.status != 'Stopped'
        if was_running:
            container.stop(wait=True)

        detached = []
        try:
            migration_data = container.generate_migration_data(live=False)
            # pylxd historically emitted the source profile list under the
            # non-API key ``default``. The destination profile is recreated
            # by Nova and must be selected explicitly for its root quota and
            # Neutron physical NIC devices to apply.
            migration_data.pop('default', None)
            migration_data['profiles'] = [instance.name]
            source = migration_data['source']
            operation = parse.urlsplit(source['operation'])
            source['operation'] = parse.urlunsplit((
                parsed_address.scheme,
                parsed_address.netloc,
                operation.path,
                operation.query,
                '',
            ))

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
                    self.detach_volume(
                        context, connection_info, instance, mountpoint)
                    detached.append((connection_info, mountpoint))
        except Exception:
            for connection_info, mountpoint in reversed(detached):
                try:
                    self.attach_volume(
                        context, connection_info, instance, mountpoint)
                except Exception:
                    LOG.exception(
                        'Failed to restore a source volume after migration '
                        'preparation failed', instance=instance)
            if was_running:
                container.start(wait=True)
            raise

        return jsonutils.dumps({
            'format': 'incus-pull-v1',
            'boot_from_volume': bool(root_bdm),
            'migration_data': migration_data,
            'was_running': was_running,
            'configdrive': configdrive_payload,
        })

    def snapshot(self, context, instance, image_id, update_task_state):
        lock_path = str(os.path.join(CONF.instances_path, 'locks'))

        with lockutils.lock(
                lock_path, external=True,
                lock_file_prefix='incus-container-{}'.format(instance.name)):

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

    def pause(self, instance):
        """Pause container.

        See `nova.virt.driver.ComputeDriver.pause` for more
        information.
        """
        container = self.client.instances.get(instance.name)
        container.freeze(wait=True)

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

    def resume(self, context, instance, network_info, block_device_info=None,
               share_info=None):
        raise NotImplementedError(
            'Incus suspend and resume require a reliable container '
            'checkpoint implementation')

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

            self.power_on(context, instance, network_info, block_device_info)
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

    def power_off(self, instance, timeout=0, retry_interval=0):
        """Power off an instance

        See 'nova.virt.drvier.ComputeDriver.power_off` for more
        information.
        """
        container = self.client.instances.get(instance.name)
        if container.status != 'Stopped':
            container.stop(wait=True)

    def power_on(self, context, instance, network_info,
                 block_device_info=None, accel_info=None, share_info=None):
        """Power on an instance

        See 'nova.virt.drvier.ComputeDriver.power_on` for more
        information.
        """
        profile = self.client.profiles.get(instance.name)
        self._attach_share_devices(profile, instance, share_info)
        container = self.client.instances.get(instance.name)
        if container.status != 'Running':
            container.start(wait=True)

    def _attach_share_devices(self, profile, instance, share_info):
        changed = False
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
        if changed:
            profile.save(wait=True)

    def mount_share(self, context, instance, share_mapping):
        """Mount an approved Manila export and stage its Incus disk device."""
        if not CONF.incus.enable_manila_shares:
            raise exception.ShareProtocolNotSupported(
                share_proto=share_mapping.share_proto)
        mount_path = _share_mount_path(instance, share_mapping)
        fileutils.ensure_tree(mount_path, mode=0o700)
        mounted = os.path.ismount(mount_path)
        secret_path = None
        try:
            if not mounted:
                options = ['-o', 'nosuid,nodev']
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
                    fd, secret_path = tempfile.mkstemp(
                        prefix='.ceph-secret-',
                        dir=os.path.dirname(mount_path))
                    try:
                        os.fchmod(fd, 0o600)
                        os.write(fd, share_mapping.access_key.encode('utf-8'))
                    finally:
                        os.close(fd)
                    options = [
                        '-o',
                        'nosuid,nodev,name=%s,secretfile=%s' %
                        (share_mapping.access_to, secret_path),
                    ]
                else:
                    raise exception.ShareProtocolNotSupported(
                        share_proto=share_mapping.share_proto)
                privsep_fs.mount(
                    fstype, share_mapping.export_location,
                    mount_path, options)

            try:
                profile = self.client.profiles.get(instance.name)
            except incus_exceptions.LXDAPIException as exc:
                if exc.response.status_code == 404:
                    return
                raise
            self._attach_share_devices(profile, instance, [share_mapping])
        except (exception.ShareMountError,
                exception.ShareProtocolNotSupported):
            raise
        except Exception as exc:
            if not mounted and os.path.ismount(mount_path):
                try:
                    privsep_fs.umount(mount_path)
                except Exception:
                    LOG.exception(
                        'Failed to roll back Manila share mount',
                        instance=instance)
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

    def umount_share(self, context, instance, share_mapping):
        """Remove the Incus share device before unmounting the host export."""
        if not CONF.incus.enable_manila_shares:
            raise exception.ShareProtocolNotSupported(
                share_proto=share_mapping.share_proto)
        try:
            profile = self.client.profiles.get(instance.name)
        except incus_exceptions.LXDAPIException as exc:
            if exc.response.status_code == 404:
                profile = None
            else:
                raise

        if profile is not None:
            device_name = _share_device_name(share_mapping)
            if device_name in profile.devices:
                profile.devices.pop(device_name)
                profile.save(wait=True)

        mount_path = _share_mount_path(instance, share_mapping)
        try:
            if os.path.ismount(mount_path):
                privsep_fs.umount(mount_path)
            if os.path.isdir(mount_path):
                os.rmdir(mount_path)
            instance_share_dir = os.path.dirname(mount_path)
            if os.path.isdir(instance_share_dir):
                os.rmdir(instance_share_dir)
            return False
        except Exception as exc:
            raise exception.ShareUmountError(
                share_id=share_mapping.share_id,
                server_id=instance.uuid,
                reason=exc)

    def get_available_resource(self, nodename):
        """Aggregate all available system resources.

        See 'nova.virt.drvier.ComputeDriver.get_available_resource`
        for more information.
        """
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

        # NOTE(jamespage): ZFS storage report is very Incus 2.0.x
        #                  centric and will need to be updated
        #                  to support Incus storage pools
        storage_driver = incus_config['environment']['storage']
        if CONF.incus.storage_pool:
            local_disk_info = _get_storage_pool_info(
                self.client, CONF.incus.storage_pool)
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

        incus_hv_type = getattr(
            obj_fields.HVType, 'INCUS', obj_fields.HVType.LXC)
        supported_instances = [
            (obj_fields.Architecture.I686, incus_hv_type,
             obj_fields.VMMode.EXE),
            (obj_fields.Architecture.X86_64, incus_hv_type,
             obj_fields.VMMode.EXE),
        ]
        if incus_hv_type != obj_fields.HVType.LXC:
            supported_instances.extend([
                (obj_fields.Architecture.I686, obj_fields.HVType.LXC,
                 obj_fields.VMMode.EXE),
                (obj_fields.Architecture.X86_64, obj_fields.HVType.LXC,
                 obj_fields.VMMode.EXE),
            ])

        data = {
            'vcpus': vcpus,
            'memory_mb': local_memory_info['total'] // units.Mi,
            'memory_mb_used': local_memory_info['used'] // units.Mi,
            'local_gb': local_disk_info['total'] // units.Gi,
            'local_gb_used': local_disk_info['used'] // units.Gi,
            'vcpus_used': self._get_vcpus_used(),
            'hypervisor_type': 'incus',
            'hypervisor_version': '011',
            'cpu_info': jsonutils.dumps(cpu_info),
            'hypervisor_hostname': CONF.host,
            'supported_instances': supported_instances,
            'numa_topology': None,
        }

        return data

    def _get_vcpus_used(self):
        """Return vCPUs assigned to Nova-owned Incus instance records."""
        used = 0
        try:
            containers = self.client.instances.all()
        except Exception:
            LOG.exception('Failed to audit Incus vCPU usage')
            return 0

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
        resources = self.get_available_resource(nodename)
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
            orc.DISK_GB: {
                'total': resources['local_gb'],
                'min_unit': 1,
                'max_unit': resources['local_gb'],
                'step_size': 1,
                'allocation_ratio': ratios[orc.DISK_GB],
                'reserved': self._get_reserved_host_disk_gb_from_config(),
            },
        }
        provider_tree.update_inventory(nodename, inventory)
        traits = set(current.traits) | {INCUS_SYSTEM_CONTAINER_TRAIT}
        if CONF.incus.allow_instance_swap and _host_has_swap():
            traits.add(INCUS_SWAP_TRAIT)
        else:
            traits.discard(INCUS_SWAP_TRAIT)
        if CONF.incus.enable_manila_shares:
            traits.add(INCUS_MANILA_SHARE_TRAIT)
        else:
            traits.discard(INCUS_MANILA_SHARE_TRAIT)
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
                for vif in reversed(plugged):
                    try:
                        self.vif_driver.unplug(instance, vif)
                    except Exception:
                        LOG.exception(
                            'Failed to roll back partially plugged VIF',
                            instance=instance)

    def unplug_vifs(self, instance, network_info):
        for vif in network_info:
            self.vif_driver.unplug(instance, vif)

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

    # XXX: rockstar (5 July 2016) - The methods and code below this line
    # have not been through the cleanup process. We know the cleanup process
    # is complete when there is no more code below this comment, and the
    # comment can be removed.

    #
    # ComputeDriver implementation methods
    #
    def finish_migration(self, context, migration, instance, disk_info,
                         network_info, image_meta, resize_instance,
                         allocations, block_device_info=None, power_on=True):
        if not CONF.incus.allow_cold_migration:
            raise exception.MigrationError(
                reason='Incus cold migration is disabled by configuration')

        try:
            transfer = jsonutils.loads(disk_info)
            if transfer.get('format') != 'incus-pull-v1':
                raise ValueError('unsupported migration data format')
            migration_data = transfer['migration_data']
            migration_data.setdefault('config', {})[
                'boot.autostart'] = 'false'
        except (TypeError, ValueError, KeyError) as exc:
            raise exception.MigrationError(
                reason='Invalid Incus migration data: %s' % exc)

        root_bdm = _boot_from_volume(block_device_info)
        if bool(root_bdm) != bool(transfer.get('boot_from_volume')):
            raise exception.MigrationError(
                reason='Source and destination disagree on Incus BFV '
                'migration mode')
        root_volume = None
        if root_bdm:
            root_volume = _require_bfv_migration_support(
                self.client, root_bdm)
        configdrive_staging = _prepare_configdrive_migration(
            instance, transfer)

        profile = None
        container = None
        attached = []
        plugged = False
        try:
            _retry_migration_finish_action(
                lambda: self.plug_vifs(instance, network_info),
                'VIF wiring', instance)
            plugged = True
            profile = flavor.to_profile(
                self.client, instance, network_info, block_device_info)
            if root_volume:
                profile.devices['root'] = _bfv_root_device(
                    instance, root_bdm, root_volume)
                profile.save(wait=True)
            container = self.client.instances.create(
                migration_data, wait=True)
            if configdrive_staging:
                target_container = self.client.instances.get(instance.name)
                configdrive_path = _commit_staged_configdrive(
                    instance, target_container, configdrive_staging)
                configdrive_staging = None
                profile.devices['configdrive'] = {
                    'path': '/config-drive',
                    'source': configdrive_path,
                    'type': 'disk',
                    'readonly': 'true',
                }
                profile.save(wait=True)

            for bdm in driver.block_device_info_get_mapping(
                    block_device_info):
                if _is_boot_volume(bdm):
                    continue
                connection_info = bdm.get('connection_info')
                mountpoint = bdm.get('mount_device')
                if connection_info and mountpoint:
                    _retry_migration_finish_action(
                        lambda connection_info=connection_info,
                        mountpoint=mountpoint: self.attach_volume(
                            context, connection_info, instance, mountpoint),
                        'data-volume attachment', instance)
                    attached.append((connection_info, mountpoint))

            if power_on:
                _retry_migration_finish_action(
                    lambda: container.start(wait=True),
                    'container start', instance)
        except Exception:
            with excutils.save_and_reraise_exception() as error_context:
                if configdrive_staging:
                    shutil.rmtree(configdrive_staging, ignore_errors=True)
                bfv_ownership_uncertain = False
                if root_bdm is not None and container is None:
                    try:
                        container = self.client.instances.get(instance.name)
                    except incus_exceptions.LXDAPIException as exc:
                        if exc.response.status_code != 404:
                            bfv_ownership_uncertain = True
                            LOG.exception(
                                'Cannot determine whether the BFV migration '
                                'target claimed its root volume',
                                instance=instance)
                    except Exception:
                        bfv_ownership_uncertain = True
                        LOG.exception(
                            'Cannot determine whether the BFV migration '
                            'target claimed its root volume',
                            instance=instance)

                for connection_info, mountpoint in reversed(attached):
                    try:
                        self.detach_volume(
                            context, connection_info, instance, mountpoint)
                    except Exception:
                        LOG.exception(
                            'Failed to roll back migrated volume attachment',
                            instance=instance)
                bfv_claimed = root_bdm is not None and container is not None
                retain_bfv_target = bfv_claimed or bfv_ownership_uncertain
                if retain_bfv_target:
                    # The destination now owns the external root RBD. Retain
                    # its Incus record, profile and VIF so Nova's destination
                    # host remains aligned with storage ownership. Deleting
                    # this record would strand the instance between hosts and
                    # make a hard-reboot recovery impossible.
                    recovery_marked = False
                    if bfv_claimed and profile is not None:
                        try:
                            _retry_migration_finish_action(
                                lambda: self._mark_migration_recovery_required(
                                    instance, profile, power_on=power_on),
                                'durable recovery marker', instance)
                            recovery_marked = True
                        except Exception:
                            LOG.exception(
                                'Failed to mark claimed BFV target for '
                                'automatic recovery',
                                instance=instance)
                    LOG.error(
                        'Retaining the claimed BFV migration target after '
                        'finish_migration failed; repair the reported error '
                        'and hard reboot the instance on this host',
                        instance=instance)
                    # A definite claim plus a durable marker is a valid staged
                    # migration target. Let Nova complete finish_resize and
                    # enter VERIFY_RESIZE so host, Cinder, Neutron and
                    # Placement ownership remain on the target. The optional
                    # manager repairs runtime state without auto-confirming.
                    if recovery_marked:
                        error_context.reraise = False
                elif container is not None:
                    try:
                        container.delete(wait=True)
                    except Exception:
                        LOG.exception(
                            'Failed to remove incomplete migration target',
                            instance=instance)
                if profile is not None and not retain_bfv_target:
                    try:
                        profile.delete()
                    except Exception:
                        LOG.exception(
                            'Failed to remove incomplete migration profile',
                            instance=instance)
                if plugged and not retain_bfv_target:
                    self.unplug_vifs(instance, network_info)
                if not retain_bfv_target:
                    try:
                        _remove_instance_directory(instance)
                    except Exception:
                        LOG.exception(
                            'Failed to remove migration target directory',
                            instance=instance)

    def confirm_migration(self, context, migration, instance, network_info):
        container = self.client.instances.get(instance.name)
        if container.status != 'Stopped':
            container.stop(wait=True)
        container.delete(wait=True)
        self.client.profiles.get(instance.name).delete()
        self.unplug_vifs(instance, network_info)
        _remove_instance_directory(instance)

    def finish_revert_migration(self, context, instance, network_info,
                                migration, block_device_info=None,
                                power_on=True):
        container = self.client.instances.get(instance.name)
        root_bdm = _boot_from_volume(block_device_info)
        if root_bdm:
            _require_bfv_migration_support(self.client, root_bdm)
        try:
            for bdm in driver.block_device_info_get_mapping(
                    block_device_info):
                if _is_boot_volume(bdm):
                    continue
                connection_info = bdm.get('connection_info')
                mountpoint = bdm.get('mount_device')
                if connection_info and mountpoint:
                    _retry_migration_finish_action(
                        lambda connection_info=connection_info,
                        mountpoint=mountpoint: self.attach_volume(
                            context, connection_info, instance, mountpoint),
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
                    lambda: container.start(wait=True),
                    'revert container start', instance)
        except Exception:
            if root_bdm is None:
                raise
            profile = self.client.profiles.get(instance.name)
            try:
                _retry_migration_finish_action(
                    lambda: self._mark_migration_recovery_required(
                        instance, profile, power_on=power_on),
                    'revert durable recovery marker', instance)
            except Exception:
                LOG.exception(
                    'Failed to mark BFV revert owner for automatic recovery',
                    instance=instance)
                raise
            # Nova has already restored Cinder, Neutron, Placement and the
            # instance host to this retained source. Preserve that completed
            # ownership decision and let the manager repair runtime state.
            LOG.error(
                'Retaining the BFV revert owner for automatic runtime '
                'recovery after finish_revert_migration failed',
                instance=instance)

    def pre_live_migration(self, context, instance, block_device_info,
                           network_info, disk_info, migrate_data):
        for vif in network_info:
            self.vif_driver.plug(instance, vif)
        self.firewall_driver.setup_basic_filtering(
            instance, network_info)
        self.firewall_driver.prepare_instance_filter(
            instance, network_info)
        self.firewall_driver.apply_instance_filter(
            instance, network_info)

        flavor.to_profile(self.client,
                          instance, network_info, block_device_info)

    def live_migration(self, context, instance, dest,
                       post_method, recover_method, block_migration=False,
                       migrate_data=None):
        raise exception.MigrationError(
            reason='Incus live migration is not supported')

    def post_live_migration(self, context, instance, block_device_info,
                            migrate_data=None):
        self.client.instances.get(instance.name).delete(wait=True)

    def post_live_migration_at_source(self, context, instance, network_info):
        self.client.profiles.get(instance.name).delete()
        self.cleanup(context, instance, network_info)

    def check_can_live_migrate_destination(
            self, context, instance, src_compute_info, dst_compute_info,
            block_migration=False, disk_over_commit=False):
        raise exception.MigrationPreCheckError(
            reason='Incus live migration is not supported')

    def cleanup_live_migration_destination_check(
            self, context, dest_check_data):
        return

    def check_can_live_migrate_source(self, context, instance,
                                      dest_check_data, block_device_info=None):
        raise exception.MigrationPreCheckError(
            reason='Incus live migration is not supported')

    #
    # IncusDriver "private" implementation methods
    #
    # XXX: rockstar (21 Nov 2016) - The methods and code below this line
    # have not been through the cleanup process. We know the cleanup process
    # is complete when there is no more code below this comment, and the
    # comment can be removed.
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

        with utils.tempdir() as tmpdir:
            mounted = False
            root_helper = utils.get_root_helper()
            try:
                _, err = processutils.execute('mount',
                                       '-o',
                                       ('loop,ro,nosuid,nodev,noexec,'
                                        'uid=%d,gid=%d') % (
                                            os.getuid(), os.getgid()),
                                       iso_path, tmpdir,
                                       run_as_root=True,
                                       root_helper=root_helper)
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
                processutils.execute('chown', '-R',
                              '%s:%s' % (storage_id, storage_id),
                              configdrive_dir, run_as_root=True,
                              root_helper=root_helper)
            finally:
                if mounted:
                    processutils.execute(
                        'umount', tmpdir, run_as_root=True,
                        root_helper=root_helper)

        return configdrive_dir

    def _after_reboot(self):
        """Perform sync operation after host reboot."""
        context = nova.context.get_admin_context()
        instances = objects.InstanceList.get_by_host(
            context, self.host, expected_attrs=['info_cache', 'metadata'])

        for instance in instances:
            if (instance.vm_state != vm_states.STOPPED):
                continue
            try:
                network_info = self.network_api.get_instance_nw_info(
                    context, instance)
            except exception.InstanceNotFound:
                network_info = network_model.NetworkInfo()

            self.plug_vifs(instance, network_info)
            self.firewall_driver.setup_basic_filtering(instance, network_info)
            self.firewall_driver.prepare_instance_filter(
                instance, network_info)
            self.firewall_driver.apply_instance_filter(instance, network_info)
