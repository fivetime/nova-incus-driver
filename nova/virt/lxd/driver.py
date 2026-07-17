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
from nova.image import glance
from nova.network import neutron
from nova.network import model as network_model
from nova import objects
from nova.privsep import path as privsep_path
from nova.virt import driver
from os_brick.initiator import connector
from oslo_concurrency import processutils
from oslo_log import log as logging
from oslo_utils import fileutils
from pylxd import exceptions as lxd_exceptions

from nova.virt.lxd import vif as lxd_vif
from nova.virt.lxd import client as incus_client
from nova.virt.lxd import common
from nova.virt.lxd import flavor
from nova.virt.lxd import storage

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
MIGRATION_RECOVERY_KEY = 'user.openstack.recovery_required'
BASE_DIR = os.path.join(
    CONF.instances_path, CONF.image_cache.subdirectory_name)

_DEVICE_PATH_RE = re.compile(r'^/dev/[A-Za-z0-9][A-Za-z0-9._-]*$')
_VOLUME_MOUNTPOINT_RE = re.compile(
    r'^/dev/(?:vd|sd|xvd)[a-z]{1,2}[0-9]*$')
_CINDER_RBD_IMAGE_RE = re.compile(
    r'^volume-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
    r'[0-9a-f]{4}-[0-9a-f]{12})$')


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


def _get_power_state(lxd_state):
    """Take a lxd state code and translate it to nova power state."""
    state_map = [
        (power_state.RUNNING, {100, 101, 103, 200}),
        (power_state.SHUTDOWN, {102, 104, 107}),
        (power_state.NOSTATE, {105, 106, 401}),
        (power_state.CRASHED, {108, 400}),
        (power_state.SUSPENDED, {109, 110, 111}),
    ]
    for nova_state, lxd_states in state_map:
        if lxd_state in lxd_states:
            return nova_state
    raise ValueError('Unknown LXD power state: {}'.format(lxd_state))


def _sync_glance_image_to_lxd(client, context, image_ref):
    """Sync an image from glance to LXD image store.

    The image from glance can't go directly into the LXD image store,
    as LXD needs some extra metadata connected to it.

    The image is stored in the LXD image store with an alias to
    the image_ref. This way, it will only copy over once.
    """
    lock_path = os.path.join(CONF.instances_path, 'locks')
    with lockutils.lock(
            lock_path, external=True,
            lock_file_prefix='lxd-image-{}'.format(image_ref)):

        # NOTE(jamespage): Re-query by image_ref to ensure
        #                  that another process did not
        #                  sneak infront of this one and create
        #                  the same image already.
        try:
            client.images.get_by_alias(image_ref)
            return
        except lxd_exceptions.LXDAPIException as e:
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

            # It is possible that LXD already have the same image
            # but NOT aliased as result of previous publish/export operation
            # (snapshot from openstack).
            # In that case attempt to add it again
            # (implicitly via instance launch from affected image) will produce
            # LXD error - "Image with same fingerprint already exists".
            # Error does not have unique identifier to handle it we calculate
            # fingerprint of image as LXD do it and check if LXD already have
            # image with such fingerprint.
            # If any we will add alias to this image and will not re-import it
            def add_alias():

                def lxdimage_fingerprint():
                    def sha256_file():
                        sha256 = hashlib.sha256()
                        with closing(open(image_file, 'rb')) as f:
                            for block in iter(lambda: f.read(65536), b''):
                                sha256.update(block)
                        return sha256.hexdigest()

                    return sha256_file()

                fingerprint = lxdimage_fingerprint()
                if client.images.exists(fingerprint):
                    LOG.info("Image with fingerprint {fingerprint} already "
                             "exists but not accessible by alias {alias}, "
                             "add alias"
                             .format(fingerprint=fingerprint, alias=image_ref))
                    lxdimage = client.images.get(fingerprint)
                    lxdimage.add_alias(image_ref, '')
                    return True

                return False

            if add_alias():
                return

            # up2date LXD publish/export operations produce images which
            # already contains /rootfs and metdata.yaml in exported file.
            # We should not pass metdata explicitly in that case as imported
            # image will be unusable bacause LXD will think that it containts
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


class LXDDriver(driver.ComputeDriver):
    """A LXD driver for nova.

    LXD is a system container hypervisor. LXDDriver provides LXD
    functionality to nova. For more information about LXD, see
    http://www.ubuntu.com/cloud/lxd
    """

    capabilities = dict(
        driver.ComputeDriver.capabilities,
        supports_attach_interface=True,
        supports_extend_volume=True,
        supports_image_type_raw=True,
        supports_migrate_to_same_host=False,
    )

    def __init__(self, virtapi):
        super(LXDDriver, self).__init__(virtapi)

        # Capabilities are consumed per compute service. Never mutate the
        # class dictionary when applying a host-local operator policy.
        self.capabilities = dict(type(self).capabilities)
        self.capabilities['supports_evacuate'] = (
            CONF.incus.allow_bfv_evacuate)
        self.client = None  # Initialized by init_host
        self.host = NOVA_CONF.host
        self.network_api = neutron.API()
        self.vif_driver = lxd_vif.LXDGenericVifDriver()
        self.firewall_driver = _NeutronFirewallDriver()

    def init_host(self, host):
        """Initialize the driver on the host.

        The pylxd Client is initialized. This initialization may raise
        an exception if the LXD instance cannot be found.

        The `host` argument is ignored here, as the LXD instance is
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
        except lxd_exceptions.ClientConnectionFailed as e:
            msg = _("Unable to connect to LXD daemon: {}").format(e)
            raise exception.HostNotFound(msg)
        self._after_reboot()

    def cleanup_host(self, host):
        """Clean up the host.

        `nova.virt.ComputeDriver` defines this method. It is overridden
        here to be explicit that there is nothing to be done, as
        `init_host` does not create any resources that would need to be
        cleaned up.

        See `nova.virt.driver.ComputeDriver.cleanup_host` for more
        information.
        """

    def get_info(self, instance, use_cache=True):
        """Return an InstanceInfo object for the instance."""
        try:
            container = self.client.instances.get(instance.name)
        except lxd_exceptions.NotFound:
            raise exception.InstanceNotFound(instance_id=instance.uuid)

        if container.status == 'Stopped':
            return hardware.InstanceInfo(state=power_state.SHUTDOWN)

        state = container.state()
        return hardware.InstanceInfo(
            state=_get_power_state(state.status_code))

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
        """Create a new lxd container as a nova instance.

        Creating a new container requires a number of steps. First, the
        image is fetched from glance, if needed. Next, the network is
        connected. A profile is created in LXD, and then the container
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
        except lxd_exceptions.LXDAPIException as e:
            if e.response.status_code != 404:
                raise  # Re-raise the exception if it wasn't NotFound

        instance_dir = common.InstanceAttributes(instance).instance_dir
        if not os.path.exists(instance_dir):
            fileutils.ensure_tree(instance_dir)

        if not root_volume:
            # A Cinder root volume already contains the prepared rootfs.
            try:
                self.client.images.get_by_alias(instance.image_ref)
            except lxd_exceptions.LXDAPIException as e:
                if e.response.status_code != 404:
                    raise
                _sync_glance_image_to_lxd(
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
        except lxd_exceptions.LXDAPIException:
            with excutils.save_and_reraise_exception():
                self.cleanup(
                    context, instance, network_info, block_device_info)

        # Create the container
        container_config = {
            'name': instance.name,
            'type': 'container',
            'profiles': [profile.name],
            'config': _incus_cloud_init_config(instance),
            'source': ({'type': 'none'} if root_volume else {
                'type': 'image', 'alias': instance.image_ref}),
        }
        try:
            container = self.client.instances.create(
                container_config, wait=True)
        except lxd_exceptions.LXDAPIException:
            with excutils.save_and_reraise_exception():
                self.cleanup(
                    context, instance, network_info, block_device_info)

        try:
            lxd_config = self.client.host_info
            storage.attach_ephemeral(
                self.client, block_device_info, lxd_config, instance)
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

        with lockutils.lock(
                lock_path, external=True,
                lock_file_prefix='lxd-container-{}'.format(instance.name)):
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
            except lxd_exceptions.LXDAPIException as e:
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

        lxd_config = self.client.host_info
        storage.detach_ephemeral(self.client,
                                 block_device_info,
                                 lxd_config,
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
            except lxd_exceptions.LXDAPIException as exc:
                if exc.response.status_code == 404:
                    break
                raise
            if _volume_id(connection_info) in profile.devices:
                self.detach_volume(
                    context, connection_info, instance, mountpoint)

        container_dir = common.InstanceAttributes(instance).instance_dir
        if os.path.exists(container_dir):
            privsep_path.chown(
                container_dir, uid=os.getuid(), gid=os.getgid(),
                recursive=True)
            shutil.rmtree(container_dir)

        try:
            self.client.profiles.get(instance.name).delete()
        except lxd_exceptions.LXDAPIException as e:
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
        except lxd_exceptions.LXDAPIException as exc:
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
            except lxd_exceptions.LXDAPIException as exc:
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
        self._reconcile_reboot_data_volumes(
            context, instance, block_device_info)
        container = self.client.instances.get(instance.name)
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
        except lxd_exceptions.LXDAPIException as exc:
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
        self.plug_vifs(instance, network_info)
        should_run = desired_state in ('true', 'running')
        if should_run and container.status != 'Running':
            container.start(wait=True)
        elif not should_run and container.status == 'Running':
            container.stop(wait=True)
        self._clear_migration_recovery_marker(instance)
        return should_run

    def _clear_migration_recovery_marker(self, instance):
        try:
            profile = self.client.profiles.get(instance.name)
        except lxd_exceptions.LXDAPIException as exc:
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

    def _validate_reboot_vifs(self, instance, network_info):
        """Refuse to start a retained target with stale NIC ownership."""
        if not network_info:
            return
        profile = self.client.profiles.get(instance.name)
        for vif in network_info:
            device_name = lxd_vif.get_vif_devname(vif)
            device = profile.devices.get(device_name)
            expected = {
                'type': 'nic',
                'nictype': 'physical',
                'parent': lxd_vif.get_vif_internal_devname(vif),
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
        except lxd_exceptions.NotFound:
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
        """Replace an attached Cinder block device in one profile update."""
        _validate_volume_mountpoint(mountpoint)
        _validate_volume_access_mode(new_connection_info)

        old_id = _volume_id(old_connection_info)
        new_id = _volume_id(new_connection_info)
        profile = self.client.profiles.get(instance.name)
        old_device = profile.devices.get(old_id)
        if not old_device or old_device.get('path') != mountpoint:
            raise exception.InvalidVolume(
                reason='The old volume is not attached at %s' % mountpoint)
        _validate_profile_volume_slot(
            profile, new_id, mountpoint, replacing_volume_id=old_id)
        old_metadata_key = _volume_device_info_key(old_id)
        new_metadata_key = _volume_device_info_key(new_id)
        old_encoded_device_info = profile.config.get(old_metadata_key)
        old_device_info = _profile_device_info(profile, old_id, old_device)

        new_connector = brick_get_connector(
            new_connection_info['driver_volume_type'])
        new_device_info = new_connector.connect_volume(
            new_connection_info['data'])
        try:
            new_path = os.path.realpath(new_device_info['path'])
            _validate_block_device_path(new_path, 'os-brick connector path')
        except (KeyError, TypeError, exception.InvalidVolume):
            try:
                new_connector.disconnect_volume(
                    new_connection_info['data'], new_device_info)
            except Exception:
                LOG.exception(
                    'Failed to roll back an invalid replacement volume',
                    instance=instance)
            raise exception.InvalidVolume(
                reason='os-brick did not return a valid block device path')

        try:
            del profile.devices[old_id]
            profile.devices[new_id] = {
                'path': mountpoint,
                'required': 'true',
                'source': new_path,
                'type': 'unix-block',
            }
            profile.config.pop(old_metadata_key, None)
            profile.config[new_metadata_key] = _serialize_device_info(
                new_device_info)
            profile.save(wait=True)
        except Exception:
            with excutils.save_and_reraise_exception():
                profile.devices.pop(new_id, None)
                profile.devices[old_id] = old_device
                profile.config.pop(new_metadata_key, None)
                if old_encoded_device_info is not None:
                    profile.config[old_metadata_key] = \
                        old_encoded_device_info
                new_connector.disconnect_volume(
                    new_connection_info['data'], new_device_info)

        old_connector = brick_get_connector(
            old_connection_info['driver_volume_type'])
        old_connector.disconnect_volume(
            old_connection_info['data'], old_device_info)

    def extend_volume(self, context, connection_info, instance,
                      requested_size):
        """Refresh an attached host block device after Cinder extends it."""
        volume_id = _volume_id(connection_info)
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

        net_device = lxd_vif.get_vif_devname(vif)
        config_update = {
            net_device: {
                'nictype': 'physical',
                'hwaddr': vif['address'],
                'parent': lxd_vif.get_vif_internal_devname(vif),
                'type': 'nic',
            }
        }

        profile.devices.update(config_update)
        profile.save(wait=True)

    def detach_interface(self, context, instance, vif):
        try:
            profile = self.client.profiles.get(instance.name)
            devname = lxd_vif.get_vif_devname(vif)

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
        except lxd_exceptions.NotFound:
            # This method is called when an instance get destroyed. It
            # could happen that Nova to receive an event
            # "vif-delete-event" after the instance is destroyed which
            # result the lxd profile not exist.
            LOG.debug("lxd profile for instance {instance} does not exist. "
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
        if instance.config_drive:
            raise exception.InstanceFaultRollback(
                exception.ResizeError(
                    reason='Incus cross-host resize cannot yet preserve '
                    'config-drive contents'))
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
        })

    def snapshot(self, context, instance, image_id, update_task_state):
        lock_path = str(os.path.join(CONF.instances_path, 'locks'))

        with lockutils.lock(
                lock_path, external=True,
                lock_file_prefix='lxd-container-{}'.format(instance.name)):

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
        container = self.client.instances.get(instance.name)
        if container.status != 'Running':
            container.start(wait=True)

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

        lxd_config = self.client.host_info

        # NOTE(jamespage): ZFS storage report is very LXD 2.0.x
        #                  centric and will need to be updated
        #                  to support LXD storage pools
        storage_driver = lxd_config['environment']['storage']
        if CONF.incus.storage_pool:
            local_disk_info = _get_storage_pool_info(
                self.client, CONF.incus.storage_pool)
        elif storage_driver == 'zfs':
            # NOTE(ajkavanagh) - BUG/1782329 - this is temporary until storage
            # pools is implemented.  LXD 3 removed the storage.zfs_pool_name
            # key from the config.  So, if it fails, we need to grab the
            # configured storage pool and use that as the name instead.
            try:
                pool_name = lxd_config['config']['storage.zfs_pool_name']
            except KeyError:
                pool_name = CONF.incus.storage_pool
            local_disk_info = _get_zpool_info(pool_name)
        else:
            local_disk_info = _get_fs_info(CONF.incus.root_dir)

        data = {
            'vcpus': vcpus,
            'memory_mb': local_memory_info['total'] // units.Mi,
            'memory_mb_used': local_memory_info['used'] // units.Mi,
            'local_gb': local_disk_info['total'] // units.Gi,
            'local_gb_used': local_disk_info['used'] // units.Gi,
            'vcpus_used': 0,
            'hypervisor_type': 'lxd',
            'hypervisor_version': '011',
            'cpu_info': jsonutils.dumps(cpu_info),
            'hypervisor_hostname': CONF.host,
            'supported_instances': [
                (obj_fields.Architecture.I686, obj_fields.HVType.LXD,
                 obj_fields.VMMode.EXE),
                (obj_fields.Architecture.X86_64, obj_fields.HVType.LXD,
                 obj_fields.VMMode.EXE),
                (obj_fields.Architecture.I686, obj_fields.HVType.LXC,
                 obj_fields.VMMode.EXE),
                (obj_fields.Architecture.X86_64, obj_fields.HVType.LXC,
                 obj_fields.VMMode.EXE),
            ],
            'numa_topology': None,
        }

        return data

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
                bfv_ownership_uncertain = False
                if root_bdm is not None and container is None:
                    try:
                        container = self.client.instances.get(instance.name)
                    except lxd_exceptions.LXDAPIException as exc:
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

    def confirm_migration(self, context, migration, instance, network_info):
        container = self.client.instances.get(instance.name)
        if container.status != 'Stopped':
            container.stop(wait=True)
        container.delete(wait=True)
        self.client.profiles.get(instance.name).delete()
        self.unplug_vifs(instance, network_info)

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
            _retry_migration_finish_action(
                lambda: self.plug_vifs(instance, network_info),
                'revert VIF wiring', instance)
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
    # LXDDriver "private" implementation methods
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
        storage_id = 0
        """
        Determine UID shift used for container uid mapping
        Sample JSON config from LXD
        {
            "volatile.apply_template": "create",
            ...
            "volatile.last_state.idmap": "[
                {
                \"Isuid\":true,
                \"Isgid\":false,
                \"Hostid\":100000,
                \"Nsid\":0,
                \"Maprange\":65536
                },
                {
                \"Isuid\":false,
                \"Isgid\":true,
                \"Hostid\":100000,
                \"Nsid\":0,
                \"Maprange\":65536
                }] ",
            "volatile.tap5fd6808a-7b.name": "eth0"
        }
        """
        container_id_map = jsonutils.loads(
            container.config['volatile.last_state.idmap'])
        uid_map = list(filter(lambda id_map: id_map.get("Isuid"),
                              container_id_map))
        if uid_map:
            storage_id = uid_map[0].get("Hostid", 0)
        else:
            # privileged containers does not have uid/gid mapping
            # LXD API return nothing
            pass

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
                                       'loop,uid=%d,gid=%d' % (os.getuid(),
                                                               os.getgid()),
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

                processutils.execute('chmod', '-R', '775', configdrive_dir,
                                     run_as_root=True,
                                     root_helper=root_helper)
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
