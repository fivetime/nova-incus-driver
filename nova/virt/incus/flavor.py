# Copyright 2016 Canonical Ltd
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
from nova import exception
from nova import i18n
from nova.virt import driver
from oslo_config import cfg
from oslo_utils import units

from nova.virt.incus import common
from nova.virt.incus import vif

_ = i18n._
CONF = cfg.CONF


def _base_config(instance, _):
    return {'environment.product_name': 'OpenStack Nova'}


def _nesting(instance, _):
    if instance.flavor.extra_specs.get('incus:nested_allowed'):
        raise exception.InvalidConfiguration(
            'Nested containers are not supported by the Incus Nova driver')


def _security(instance, _):
    if instance.flavor.extra_specs.get('incus:privileged_allowed'):
        raise exception.InvalidConfiguration(
            'Privileged containers are not supported by the Incus Nova '
            'driver')
    return {'security.privileged': 'False'}


def _memory(instance, _):
    mem = instance.memory_mb
    if mem >= 0:
        swap_mb = int(instance.flavor.swap or 0)
        if swap_mb and not CONF.incus.allow_instance_swap:
            raise exception.InvalidConfiguration(
                'Flavor requests swap but [incus] allow_instance_swap is '
                'disabled')
        return {
            'limits.memory': '{}MiB'.format(mem),
            'limits.memory.swap': (
                '{}MiB'.format(swap_mb) if swap_mb else 'false'),
        }


def _cpu(instance, _):
    vcpus = instance.flavor.vcpus
    if vcpus >= 0:
        return {'limits.cpu': str(vcpus)}


def _isolated(instance, client):
    extensions = client.host_info.get('api_extensions', [])
    if 'id_map' not in extensions:
        msg = _("Host does not support isolated instance idmaps")
        raise exception.NovaException(msg)
    return {'security.idmap.isolated': 'True'}


def _processes(instance, _):
    default_limit = CONF.incus.default_process_limit
    maximum_limit = CONF.incus.maximum_process_limit
    if default_limit > maximum_limit:
        raise exception.InvalidConfiguration(
            '[incus] default_process_limit must not exceed '
            'maximum_process_limit')

    value = instance.flavor.extra_specs.get(
        'incus:process_limit', default_limit)
    try:
        process_limit = int(value)
    except (TypeError, ValueError):
        raise exception.InvalidConfiguration(
            'Flavor extra spec incus:process_limit must be a positive '
            'integer')
    if process_limit < 1 or process_limit > maximum_limit:
        raise exception.InvalidConfiguration(
            'Flavor extra spec incus:process_limit must be between 1 and '
            '{}'.format(maximum_limit))
    return {'limits.processes': str(process_limit)}


def _stateful_migration(instance, _):
    if CONF.incus.allow_live_migration:
        return {'migration.stateful': 'true'}


_CONFIG_FILTER_MAP = [
    _base_config,
    _nesting,
    _security,
    _memory,
    _cpu,
    _isolated,
    _processes,
    _stateful_migration,
]


def disk_qos_limits(specs, prefix='quota:disk_'):
    """Translate an OpenStack disk QoS mapping without changing semantics."""
    total_keys = (prefix + 'total_iops_sec', prefix + 'total_bytes_sec')
    unsupported_keys = total_keys + tuple(
        prefix + key for key in (
            'read_bytes_sec_max', 'read_iops_sec_max',
            'write_bytes_sec_max', 'write_iops_sec_max',
            'total_bytes_sec_max', 'total_iops_sec_max', 'size_iops_sec'))
    if any(key in specs for key in unsupported_keys):
        raise exception.InvalidConfiguration(
            'Incus cannot preserve total or burst disk QoS semantics')

    limits = {}
    for direction in ('read', 'write'):
        iops_key = '{}{}_iops_sec'.format(prefix, direction)
        bytes_key = '{}{}_bytes_sec'.format(prefix, direction)
        if iops_key in specs and bytes_key in specs:
            raise exception.InvalidConfiguration(
                'Incus accepts either IOPS or bytes/s for disk {}, not '
                'both'.format(direction))

        key = iops_key if iops_key in specs else bytes_key
        if key not in specs:
            continue

        try:
            value = int(specs[key])
        except (TypeError, ValueError):
            raise exception.InvalidConfiguration(
                '{} must be a positive integer'.format(key))
        if value <= 0:
            raise exception.InvalidConfiguration(
                '{} must be a positive integer'.format(key))

        suffix = 'iops' if key == iops_key else 'B'
        limits['limits.{}'.format(direction)] = '{}{}'.format(value, suffix)

    return limits


def _root(instance, client, *_args):
    """Configure the root disk."""
    device = {'type': 'disk', 'path': '/'}

    selector = instance.flavor.extra_specs.get('incus:root_storage_pool')
    if selector:
        storage_pool = CONF.incus.root_storage_pools.get(selector)
        if not storage_pool:
            raise exception.InvalidConfiguration(
                'Flavor requests unknown Incus root storage pool selector '
                '{}'.format(selector))
        trait = common.root_storage_pool_trait(selector)
        trait_value = instance.flavor.extra_specs.get(
            'trait:{}'.format(trait))
        if trait_value != 'required':
            raise exception.InvalidConfiguration(
                'Flavor selecting Incus root storage pool {} must require '
                'Placement trait {}'.format(selector, trait))
        resource_class = (
            CONF.incus.root_storage_pool_resource_classes.get(selector))
        if resource_class:
            resource_key = 'resources:{}'.format(resource_class)
            requested = instance.flavor.extra_specs.get(resource_key)
            root_gb = max(instance.root_gb, CONF.incus.minimum_root_disk_gb)
            try:
                requested_gb = int(requested)
            except (TypeError, ValueError):
                raise exception.InvalidConfiguration(
                    'Flavor selecting Incus root storage pool {} must set '
                    '{} to root_gb ({})'.format(
                        selector, resource_key, root_gb))
            if requested_gb != root_gb:
                raise exception.InvalidConfiguration(
                    '{} must equal root_gb ({})'.format(
                        resource_key, root_gb))
    else:
        storage_pool = CONF.incus.storage_pool

    # A managed block or copy-on-write pool must enforce the Nova root disk
    # allocation. The dir backend cannot provide this isolation.
    if storage_pool:
        extensions = client.host_info.get('api_extensions', [])
        if 'storage' in extensions:
            device['pool'] = storage_pool
            storage_type = client.storage_pools.get(
                storage_pool).driver
        else:
            msg = _("Host does not have storage pool support")
            raise exception.NovaException(msg)
    else:
        storage_type = client.host_info['environment']['storage']

    if storage_type in ['btrfs', 'ceph', 'lvm', 'zfs']:
        root_gb = max(instance.root_gb, CONF.incus.minimum_root_disk_gb)
        device['size'] = '{}GB'.format(root_gb)

        specs = instance.flavor.extra_specs

        device.update(disk_qos_limits(specs))

    return {'root': device}


def _ephemeral_storage(instance, client, __, block_info):
    ephemeral_storage = driver.block_device_info_get_ephemerals(block_info)
    if ephemeral_storage:
        raise exception.InvalidConfiguration(
            'Nova ephemeral disks are disabled until they can be backed by '
            'quota-controlled Incus storage volumes')


def _network(instance, _, network_info, __):
    if not network_info:
        return

    devices = {}
    for vifaddr in network_info:
        cfg = vif.get_config(vifaddr)
        devname = vif.get_vif_devname(vifaddr)
        key = devname
        devices[key] = {
            'nictype': 'physical',
            'hwaddr': str(cfg['mac_address']),
            'parent': vif.get_vif_internal_devname(vifaddr),
            'type': 'nic'
        }

        specs = instance.flavor.extra_specs
        # Since Incus does not implement average NIC IO and number of burst
        # bytes, we take the max(vif_*_average, vif_*_peak) to set the peak
        # network IO and simply ignore the burst bytes.
        # Align values to MBit/s (8 * powers of 1000 in this case), having
        # in mind that the values are recieved in Kilobytes/s.
        vif_inbound_limit = max(
            int(specs.get('quota:vif_inbound_average', 0)),
            int(specs.get('quota:vif_inbound_peak', 0)),
        )
        if vif_inbound_limit:
            devices[key]['limits.ingress'] = '{}Mbit'.format(
                vif_inbound_limit * units.k * 8 // units.M)

        vif_outbound_limit = max(
            int(specs.get('quota:vif_outbound_average', 0)),
            int(specs.get('quota:vif_outbound_peak', 0)),
        )
        if vif_outbound_limit:
            devices[key]['limits.egress'] = '{}Mbit'.format(
                vif_outbound_limit * units.k * 8 // units.M)
    return devices


_DEVICE_FILTER_MAP = [
    _root,
    _ephemeral_storage,
    _network,
]


def to_profile(client, instance, network_info, block_info, update=False):
    """Convert a nova flavor to a incus profile.

    Every instance container created via nova-incus has a profile by the
    same name. The profile is sync'd with the configuration of the container.
    When the instance container is deleted, so is the profile.
    """

    name = instance.name

    config = {}
    for f in _CONFIG_FILTER_MAP:
        new = f(instance, client)
        if new:
            config.update(new)

    devices = {}
    for f in _DEVICE_FILTER_MAP:
        new = f(instance, client, network_info, block_info)
        if new:
            devices.update(new)

    if update is True:
        profile = client.profiles.get(name)
        profile.devices = devices
        profile.config = config
        profile.save()
        return profile
    else:
        return client.profiles.create(name, config, devices)
