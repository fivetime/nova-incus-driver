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
from unittest import mock
from nova import context
from nova import exception
from nova import test
from nova.network import model as network_model
from nova.tests.unit import fake_instance

from nova.virt.incus import flavor


class ToProfileTest(test.NoDBTestCase):
    """Tests for nova.virt.incus.flavor.to_profile."""

    def setUp(self):
        super(ToProfileTest, self).setUp()
        self.client = mock.Mock()
        self.client.host_info = {
            'api_extensions': ['id_map'],
            'environment': {
                'storage': 'zfs'
            }
        }

        self.patchers = []
        CONF_patcher = mock.patch('nova.virt.incus.driver.nova.conf.CONF')
        self.patchers.append(CONF_patcher)
        self.CONF = CONF_patcher.start()
        self.CONF.instances_path = '/i'
        self.CONF.incus.root_dir = ''

        CONF_patcher = mock.patch('nova.virt.incus.flavor.CONF')
        self.patchers.append(CONF_patcher)
        self.CONF2 = CONF_patcher.start()
        self.CONF2.incus.storage_pool = None
        self.CONF2.incus.root_storage_pools = {}
        self.CONF2.incus.root_storage_pool_resource_classes = {}
        self.CONF2.incus.root_dir = ''
        self.CONF2.incus.minimum_root_disk_gb = 1
        self.CONF2.incus.default_process_limit = 1024
        self.CONF2.incus.maximum_process_limit = 65536
        self.CONF2.incus.allow_instance_swap = False
        self.CONF2.incus.allow_live_migration = False
        self.CONF2.incus.data_volume_mount_fuse = 'ext4=fuse2fs'

    def tearDown(self):
        super(ToProfileTest, self).tearDown()
        for patcher in self.patchers:
            patcher.stop()

    def assert_profile_created(self, name, expected_config, expected_devices):
        expected_config.update({
            'limits.processes': '1024',
            'security.idmap.isolated': 'True',
            'security.privileged': 'False',
            'security.syscalls.intercept.mount': 'true',
            'security.syscalls.intercept.mount.fuse': 'ext4=fuse2fs',
        })
        if 'limits.memory' in expected_config:
            expected_config['limits.memory.swap'] = 'false'
        root = expected_devices.get('root', {})
        if root.get('size') == '0GB':
            root['size'] = '1GB'
        create = self.client.profiles.create
        create.assert_called_once_with(name, expected_config, expected_devices)

    def test_data_volume_mount_fuse_rejects_unsafe_syntax(self):
        self.CONF2.incus.data_volume_mount_fuse = 'ext4=fuse2fs;touch /tmp/x'

        self.assertRaises(
            exception.InvalidConfiguration,
            flavor.data_volume_fuse_binaries)

    def test_to_profile(self):
        """A profile configuration is requested of the Incus client."""
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = []
        block_info = []

        expected_config = {
            'environment.product_name': 'OpenStack Nova',
            'limits.cpu': '1',
            'limits.memory': '0MiB',
        }
        expected_devices = {
            'root': {
                'path': '/',
                'size': '0GB',
                'type': 'disk'
            },
        }

        flavor.to_profile(self.client, instance, network_info, block_info)

        self.assert_profile_created(
            instance.name, expected_config, expected_devices)

    def test_to_profile_lvm(self):
        """A profile configuration is requested of the Incus client."""
        self.client.host_info['environment']['storage'] = 'lvm'
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = []
        block_info = []

        expected_config = {
            'environment.product_name': 'OpenStack Nova',
            'limits.cpu': '1',
            'limits.memory': '0MiB',
        }
        expected_devices = {
            'root': {
                'path': '/',
                'size': '0GB',
                'type': 'disk'
            },
        }

        flavor.to_profile(self.client, instance, network_info, block_info)

        self.assert_profile_created(
            instance.name, expected_config, expected_devices)

    def test_storage_pools(self):
        self.client.host_info['api_extensions'].append('storage')
        self.CONF2.incus.storage_pool = 'test_pool'
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = []
        block_info = []
        expected_config = {
            'environment.product_name': 'OpenStack Nova',
            'limits.cpu': '1',
            'limits.memory': '0MiB'
        }
        expected_devices = {
            'root': {
                'path': '/',
                'type': 'disk',
                'pool': 'test_pool',
            },
        }
        flavor.to_profile(self.client, instance, network_info, block_info)

        self.assert_profile_created(
            instance.name, expected_config, expected_devices)

    def test_flavor_selects_root_storage_pool(self):
        self.client.host_info['api_extensions'].append('storage')
        self.CONF2.incus.storage_pool = 'ceph-default'
        self.CONF2.incus.root_storage_pools = {
            'local-nvme': 'local-zfs',
            'durable': 'ceph-rootfs',
        }
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=1024)
        instance.flavor.extra_specs = {
            'incus:root_storage_pool': 'local-nvme',
            'trait:CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME': 'required',
        }
        self.client.storage_pools.get.return_value.driver = 'zfs'

        flavor.to_profile(self.client, instance, [], [])

        devices = self.client.profiles.create.call_args.args[2]
        self.assertEqual('local-zfs', devices['root']['pool'])
        self.client.storage_pools.get.assert_called_once_with('local-zfs')

    def test_flavor_rejects_unknown_root_storage_pool(self):
        self.CONF2.incus.storage_pool = 'ceph-default'
        self.CONF2.incus.root_storage_pools = {
            'durable': 'ceph-rootfs',
        }
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=1024)
        instance.flavor.extra_specs = {
            'incus:root_storage_pool': 'unmanaged',
        }

        self.assertRaises(
            exception.InvalidConfiguration,
            flavor.to_profile, self.client, instance, [], [])

    def test_flavor_requires_local_pool_capacity_resource(self):
        self.CONF2.incus.root_storage_pools = {
            'local-nvme': 'local-zfs',
        }
        self.CONF2.incus.root_storage_pool_resource_classes = {
            'local-nvme': 'CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME_DISK_GB',
        }
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=1024, root_gb=20)
        instance.flavor.extra_specs = {
            'incus:root_storage_pool': 'local-nvme',
            'trait:CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME': 'required',
            'resources:CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME_DISK_GB': '10',
        }

        self.assertRaises(
            exception.InvalidConfiguration,
            flavor.to_profile, self.client, instance, [], [])

    def test_to_profile_rejects_privileged(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        instance.flavor.extra_specs = {
            'incus:privileged_allowed': True,
        }

        self.assertRaises(
            exception.InvalidConfiguration,
            flavor.to_profile, self.client, instance, [], [])

    def test_to_profile_rejects_nesting(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        instance.flavor.extra_specs = {'incus:nested_allowed': True}

        self.assertRaises(
            exception.InvalidConfiguration,
            flavor.to_profile, self.client, instance, [], [])

    def test_to_profile_process_limit_extra_spec(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        instance.flavor.extra_specs = {'incus:process_limit': '4096'}

        flavor.to_profile(self.client, instance, [], [])

        config = self.client.profiles.create.call_args.args[1]
        self.assertEqual('4096', config['limits.processes'])

    def test_to_profile_prepares_new_instances_for_live_migration(self):
        self.CONF2.incus.allow_live_migration = True
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=1024)

        flavor.to_profile(self.client, instance, [], [])

        config = self.client.profiles.create.call_args.args[1]
        self.assertEqual('true', config['migration.stateful'])

    def test_to_profile_maps_flavor_swap_to_cgroup_limit(self):
        self.CONF2.incus.allow_instance_swap = True
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=1024)
        instance.flavor.swap = 2048

        flavor.to_profile(self.client, instance, [], [])

        config = self.client.profiles.create.call_args.args[1]
        self.assertEqual('1024MiB', config['limits.memory'])
        self.assertEqual('2048MiB', config['limits.memory.swap'])

    def test_to_profile_rejects_swap_when_operator_disables_it(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=1024)
        instance.flavor.swap = 2048

        self.assertRaises(
            exception.InvalidConfiguration,
            flavor.to_profile, self.client, instance, [], [])

    def test_to_profile_rejects_invalid_process_limit(self):
        ctx = context.get_admin_context()
        for value in ('0', '-1', 'unlimited', '65537'):
            instance = fake_instance.fake_instance_obj(
                ctx, name='test', memory_mb=0)
            instance.flavor.extra_specs = {'incus:process_limit': value}
            self.client.profiles.create.reset_mock()

            self.assertRaises(
                exception.InvalidConfiguration,
                flavor.to_profile, self.client, instance, [], [])

    def test_to_profile_rejects_process_default_above_ceiling(self):
        self.CONF2.incus.default_process_limit = 65537
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)

        self.assertRaises(
            exception.InvalidConfiguration,
            flavor.to_profile, self.client, instance, [], [])

    def test_to_profile_idmap(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        instance.flavor.extra_specs = {
            'incus:isolated': True,
        }
        network_info = []
        block_info = []

        expected_config = {
            'environment.product_name': 'OpenStack Nova',
            'security.idmap.isolated': 'True',
            'limits.cpu': '1',
            'limits.memory': '0MiB',
        }
        expected_devices = {
            'root': {
                'path': '/',
                'size': '0GB',
                'type': 'disk'
            },
        }

        flavor.to_profile(self.client, instance, network_info, block_info)

        self.assert_profile_created(
            instance.name, expected_config, expected_devices)

    def test_to_profile_idmap_unsupported(self):
        self.client.host_info['api_extensions'].remove('id_map')
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        instance.flavor.extra_specs = {
            'incus:isolated': True,
        }
        network_info = []
        block_info = []

        self.assertRaises(
            exception.NovaException,
            flavor.to_profile, self.client, instance, network_info, block_info)

    def test_to_profile_quota_extra_specs_bytes(self):
        """A profile configuration is requested of the Incus client."""
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        instance.flavor.extra_specs = {
            'quota:disk_read_bytes_sec': '3000000',
            'quota:disk_write_bytes_sec': '4000000',
        }
        network_info = []
        block_info = []

        expected_config = {
            'environment.product_name': 'OpenStack Nova',
            'limits.cpu': '1',
            'limits.memory': '0MiB',
        }
        expected_devices = {
            'root': {
                'limits.read': '3000000B',
                'limits.write': '4000000B',
                'path': '/',
                'size': '0GB',
                'type': 'disk'
            },
        }

        flavor.to_profile(self.client, instance, network_info, block_info)

        self.assert_profile_created(
            instance.name, expected_config, expected_devices)

    def test_to_profile_quota_extra_specs_iops(self):
        """A profile configuration is requested of the Incus client."""
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        instance.flavor.extra_specs = {
            'quota:disk_read_iops_sec': '300',
            'quota:disk_write_iops_sec': '400',
        }
        network_info = []
        block_info = []

        expected_config = {
            'environment.product_name': 'OpenStack Nova',
            'limits.cpu': '1',
            'limits.memory': '0MiB',
        }
        expected_devices = {
            'root': {
                'limits.read': '300iops',
                'limits.write': '400iops',
                'path': '/',
                'size': '0GB',
                'type': 'disk'
            },
        }

        flavor.to_profile(self.client, instance, network_info, block_info)

        self.assert_profile_created(
            instance.name, expected_config, expected_devices)

    def test_to_profile_rejects_quota_total_bytes(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        instance.flavor.extra_specs = {
            'quota:disk_total_bytes_sec': '6000000',
        }
        network_info = []
        block_info = []

        self.assertRaises(
            exception.InvalidConfiguration,
            flavor.to_profile, self.client, instance, network_info, block_info)

    def test_to_profile_rejects_quota_total_iops(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        instance.flavor.extra_specs = {
            'quota:disk_total_iops_sec': '500',
        }
        network_info = []
        block_info = []

        self.assertRaises(
            exception.InvalidConfiguration,
            flavor.to_profile, self.client, instance, network_info, block_info)

    def test_to_profile_rejects_bytes_and_iops_for_same_direction(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        instance.flavor.extra_specs = {
            'quota:disk_read_bytes_sec': '3000000',
            'quota:disk_read_iops_sec': '300',
        }

        self.assertRaises(
            exception.InvalidConfiguration,
            flavor.to_profile, self.client, instance, [], [])

    def test_to_profile_rejects_non_positive_disk_quota(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        instance.flavor.extra_specs = {
            'quota:disk_write_iops_sec': '0',
        }

        self.assertRaises(
            exception.InvalidConfiguration,
            flavor.to_profile, self.client, instance, [], [])

    def test_disk_qos_limits_accepts_cinder_qos_namespace(self):
        self.assertEqual({
            'limits.read': '750iops',
            'limits.write': '1048576B',
        }, flavor.disk_qos_limits({
            'read_iops_sec': '750',
            'write_bytes_sec': '1048576',
        }, prefix=''))

    def test_disk_qos_limits_rejects_burst_semantics(self):
        self.assertRaises(
            exception.InvalidConfiguration,
            flavor.disk_qos_limits,
            {'quota:disk_read_iops_sec_max': '1000'})

    def test_to_profile_network_config_average(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        instance.flavor.extra_specs = {
            'quota:vif_inbound_average': '1000000',
            'quota:vif_outbound_average': '2000000',
        }
        network_info = [{
            'id': '0123456789abcdef',
            'type': network_model.VIF_TYPE_OVS,
            'address': '00:11:22:33:44:55',
            'network': {
                'bridge': 'fakebr'},
            'devname': 'tap0123456789a'}]
        block_info = []

        expected_config = {
            'environment.product_name': 'OpenStack Nova',
            'limits.cpu': '1',
            'limits.memory': '0MiB',
        }
        expected_devices = {
            'tap0123456789a': {
                'hwaddr': '00:11:22:33:44:55',
                'nictype': 'physical',
                'name': 'nic0123456789ab',
                'parent': 'tin0123456789a',
                'type': 'nic',
                'limits.egress': '16000Mbit',
                'limits.ingress': '8000Mbit',
            },
            'root': {
                'path': '/',
                'size': '0GB',
                'type': 'disk'
            },
        }

        flavor.to_profile(self.client, instance, network_info, block_info)

        self.assert_profile_created(
            instance.name, expected_config, expected_devices)

    def test_to_profile_network_config_peak(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        instance.flavor.extra_specs = {
            'quota:vif_inbound_peak': '3000000',
            'quota:vif_outbound_peak': '4000000',
        }
        network_info = [{
            'id': '0123456789abcdef',
            'type': network_model.VIF_TYPE_OVS,
            'address': '00:11:22:33:44:55',
            'network': {
                'bridge': 'fakebr'},
            'devname': 'tap0123456789a'}]
        block_info = []

        expected_config = {
            'environment.product_name': 'OpenStack Nova',
            'limits.cpu': '1',
            'limits.memory': '0MiB',
        }
        expected_devices = {
            'tap0123456789a': {
                'hwaddr': '00:11:22:33:44:55',
                'nictype': 'physical',
                'name': 'nic0123456789ab',
                'parent': 'tin0123456789a',
                'type': 'nic',
                'limits.egress': '32000Mbit',
                'limits.ingress': '24000Mbit',
            },
            'root': {
                'path': '/',
                'size': '0GB',
                'type': 'disk'
            },
        }

        flavor.to_profile(self.client, instance, network_info, block_info)

        self.assert_profile_created(
            instance.name, expected_config, expected_devices)

    @mock.patch(
        'nova.virt.incus.flavor.driver.block_device_info_get_ephemerals')
    def test_to_profile_ephemeral_storage(self, get_ephemerals):
        get_ephemerals.return_value = [
            {'virtual_name': 'ephemeral1'},
        ]

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = []
        block_info = []

        self.assertRaises(
            exception.InvalidConfiguration,
            flavor.to_profile,
            self.client, instance, network_info, block_info)
