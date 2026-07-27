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

import collections
import base64
from contextlib import closing
import errno
import hashlib
import inspect
import io
import os
import stat
import tarfile
import tempfile

import eventlet
from oslo_config import cfg
from oslo_serialization import jsonutils
from oslo_utils import timeutils
from oslo_utils import units
from unittest import mock
from nova import context
from nova import exception
from nova import test
from nova.compute import manager
from nova.compute import power_state
from nova.compute import task_states
from nova.compute import vm_states
from nova.network import model as network_model
from nova.objects import migrate_data as nova_migrate_data
from nova.tests.unit import fake_instance
from nova.virt import driver as nova_driver
from pylxd import exceptions as incuscore_exceptions
import six

from nova.virt.incus import common
from nova.virt.incus import driver
from nova.virt.incus import migrate_data

ORIGINAL_BRICK_GET_CONNECTOR = driver.brick_get_connector

MockResponse = collections.namedtuple('Response', ['status_code'])

MockContainer = collections.namedtuple('Container', ['name'])
MockContainerState = collections.namedtuple(
    'ContainerState', ['status', 'memory', 'status_code'])

_VIF = {
    'devname': 'lol0', 'type': 'bridge', 'id': '0123456789abcdef',
    'address': 'ca:fe:de:ad:be:ef'}


def incus_api_exception(status_code, message):
    response = mock.Mock(status_code=status_code)
    response.json.return_value = {'error': message}
    return incuscore_exceptions.LXDAPIException(response)


def incus_operation_exception(status_code, message):
    response = mock.Mock(status_code=200)
    response.json.return_value = {
        'metadata': {
            'status': 'Failure',
            'status_code': status_code,
            'err': message,
        },
    }
    return incuscore_exceptions.LXDAPIException(response)


def fake_connection_info(volume, location, iqn, auth=False, transport=None):
    dev_name = 'ip-%s-iscsi-%s-lun-1' % (location, iqn)
    if transport is not None:
        dev_name = 'pci-0000:00:00.0-' + dev_name
    dev_path = '/dev/disk/by-path/%s' % (dev_name)
    ret = {
        'driver_volume_type': 'iscsi',
        'data': {
            'volume_id': volume['id'],
            'target_portal': location,
            'target_iqn': iqn,
            'target_lun': 1,
            'device_path': dev_path,
        }
    }
    if auth:
        ret['data']['auth_method'] = 'CHAP'
        ret['data']['auth_username'] = 'foo'
        ret['data']['auth_password'] = 'bar'
    return ret


class VolumeConnectionInfoTest(test.NoDBTestCase):

    def test_volume_id_prefers_modern_serial(self):
        connection_info = {
            'serial': 'modern-id',
            'data': {'volume_id': 'legacy-id'},
        }

        self.assertEqual('modern-id', driver._volume_id(connection_info))

    def test_volume_id_accepts_legacy_data_field(self):
        self.assertEqual(
            'legacy-id',
            driver._volume_id({'data': {'volume_id': 'legacy-id'}}))

    def test_volume_id_rejects_missing_identifier(self):
        self.assertRaises(
            exception.InvalidVolume, driver._volume_id, {'data': {}})


class GetPowerStateTest(test.NoDBTestCase):
    """Tests for nova.virt.incus.driver.IncusDriver."""

    def test_running(self):
        state = driver._get_power_state(100)
        self.assertEqual(power_state.RUNNING, state)

    def test_shutdown(self):
        state = driver._get_power_state(102)
        self.assertEqual(power_state.SHUTDOWN, state)

    def test_nostate(self):
        state = driver._get_power_state(105)
        self.assertEqual(power_state.NOSTATE, state)

    def test_crashed(self):
        state = driver._get_power_state(108)
        self.assertEqual(power_state.CRASHED, state)

    def test_suspended(self):
        state = driver._get_power_state(109)
        self.assertEqual(power_state.SUSPENDED, state)

    def test_unknown(self):
        self.assertRaises(ValueError, driver._get_power_state, 69)


class IncusDriverTest(test.NoDBTestCase):
    """Tests for nova.virt.incus.driver.IncusDriver."""

    def setUp(self):
        super(IncusDriverTest, self).setUp()
        self.flags(force_config_drive=False)

        self.Client_patcher = mock.patch(
            'nova.virt.incus.driver.incus_client.get_client')
        self.Client = self.Client_patcher.start()

        self.client = mock.Mock()
        self.client.host_info = {
            'api_extensions': [
                'id_map',
                'migration_stateful_shifted_root',
                'migration_live_shared_ceph_storage',
            ],
            'environment': {
                'storage': 'zfs',
                'kernel_architecture': 'x86_64',
                'kernel_version': '6.8.0-test',
                'server_version': '7.2',
            }
        }
        self.Client.return_value = self.client

        self.patchers = []

        CONF_patcher = mock.patch('nova.virt.incus.driver.CONF')
        self.patchers.append(CONF_patcher)
        self.CONF = CONF_patcher.start()
        self.CONF.instances_path = '/path/to/instances'
        self.CONF.my_ip = '0.0.0.0'
        self.CONF.config_drive_format = 'iso9660'
        self.CONF.force_config_drive = False
        self.CONF.incus.storage_pool = None
        self.CONF.incus.root_storage_pools = {}
        self.CONF.incus.root_storage_pool_resource_classes = {}
        self.CONF.incus.allow_cold_migration = False
        self.CONF.incus.allow_live_migration = False
        self.CONF.incus.migration_address = None
        self.CONF.incus.migration_tls_ca = None
        self.CONF.incus.migration_tls_ca_by_server = {}
        self.CONF.incus.migration_preflight_server_names = {}
        self.CONF.incus.migration_finish_retries = 3
        self.CONF.incus.migration_finish_retry_interval = 0
        self.CONF.incus.configdrive_migration_max_bytes = 8 * 1024 * 1024
        self.CONF.incus.configdrive_migration_max_files = 512
        self.CONF.incus.volume_use_multipath = False
        self.CONF.incus.volume_enforce_multipath = False
        self.CONF.incus.num_volume_scan_tries = 3
        self.CONF.incus.data_volume_mount_fuse = 'ext4=fuse2fs'
        self.CONF.incus.enable_manila_shares = False
        self.CONF.serial_console.enabled = False
        self.CONF.serial_console.proxyclient_address = '127.0.0.1'

        # XXX: rockstar (03 Nov 2016) - This should be removed once
        # everything is where it should live.
        CONF2_patcher = mock.patch('nova.virt.incus.driver.nova.conf.CONF')
        self.patchers.append(CONF2_patcher)
        self.CONF2 = CONF2_patcher.start()
        self.CONF2.incus.root_dir = '/incus'
        self.CONF2.incus.storage_pool = None
        self.CONF2.instances_path = '/i'

        # IncusDriver._after_reboot reads from the database and syncs container
        # state. These tests can't read from the database.
        after_reboot_patcher = mock.patch(
            'nova.virt.incus.driver.IncusDriver._after_reboot')
        self.patchers.append(after_reboot_patcher)
        self.after_reboot = after_reboot_patcher.start()

        bdige_patcher = mock.patch(
            'nova.virt.incus.driver.driver.block_device_info_get_ephemerals')
        self.patchers.append(bdige_patcher)
        self.block_device_info_get_ephemerals = bdige_patcher.start()
        self.block_device_info_get_ephemerals.return_value = []

        vif_driver_patcher = mock.patch(
            'nova.virt.incus.driver.incus_vif.IncusGenericVifDriver')
        self.patchers.append(vif_driver_patcher)
        self.IncusGenericVifDriver = vif_driver_patcher.start()
        self.vif_driver = mock.Mock()
        self.IncusGenericVifDriver.return_value = self.vif_driver

        vif_gc_patcher = mock.patch(
            'nova.virt.incus.driver.incus_vif.get_config')
        self.patchers.append(vif_gc_patcher)
        self.get_config = vif_gc_patcher.start()
        self.get_config.return_value = {
            'mac_address': '00:11:22:33:44:55', 'bridge': 'qbr0123456789a',
        }

        # NOTE: mock out fileutils to ensure that unit tests don't try
        #       to manipulate the filesystem (breaks in package builds).
        driver.fileutils = mock.Mock()

    def tearDown(self):
        super(IncusDriverTest, self).tearDown()
        self.Client_patcher.stop()
        for patcher in self.patchers:
            patcher.stop()

    def test_init_host(self):
        """init_host initializes the pylxd Client."""
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.Client.assert_called_once_with(self.CONF)
        self.assertEqual(self.client, incus_driver.client)

    def test_capabilities_extend_modern_nova_defaults(self):
        capabilities = driver.IncusDriver.capabilities

        self.assertTrue(capabilities['supports_attach_interface'])
        self.assertTrue(capabilities['supports_image_type_raw'])
        self.assertFalse(capabilities['supports_evacuate'])
        self.assertTrue(capabilities['supports_extend_volume'])
        self.assertFalse(capabilities['supports_multiattach'])
        self.assertFalse(capabilities['supports_bfv_rescue'])
        self.assertFalse(capabilities['supports_device_tagging'])
        self.assertFalse(capabilities['supports_tagged_attach_interface'])
        self.assertFalse(capabilities['supports_tagged_attach_volume'])
        self.assertFalse(capabilities['supports_vtpm'])
        self.assertFalse(capabilities['supports_secure_boot'])
        self.assertFalse(capabilities['supports_accelerators'])
        self.assertFalse(capabilities['supports_virtio_fs'])

    def test_capabilities_enable_bfv_evacuate_per_driver(self):
        self.CONF.incus.allow_bfv_evacuate = True

        incus_driver = driver.IncusDriver(None)

        self.assertTrue(incus_driver.capabilities['supports_evacuate'])
        self.assertFalse(driver.IncusDriver.capabilities['supports_evacuate'])

    def test_rebuild_non_evacuate_delegates_to_nova(self):
        incus_driver = driver.IncusDriver(None)

        self.assertRaises(
            NotImplementedError, incus_driver.rebuild,
            None, mock.Mock(), mock.Mock(), [], None, {}, [], mock.Mock(),
            mock.Mock())

    def test_rebuild_evacuate_disabled(self):
        incus_driver = driver.IncusDriver(None)

        self.assertRaises(
            exception.InstanceEvacuateNotSupported, incus_driver.rebuild,
            None, mock.Mock(), mock.Mock(), [], None, {}, [], mock.Mock(),
            mock.Mock(), evacuate=True, block_device_info={})

    def test_rebuild_evacuate_rejects_local_root(self):
        self.CONF.incus.allow_bfv_evacuate = True
        incus_driver = driver.IncusDriver(None)

        self.assertRaises(
            exception.InstanceEvacuateNotSupported, incus_driver.rebuild,
            None, mock.Mock(), mock.Mock(), [], None, {}, [], mock.Mock(),
            mock.Mock(), evacuate=True, block_device_info={})

    @mock.patch.object(driver, '_require_bfv_migration_support')
    def test_rebuild_bfv_evacuate_delegates_to_nova(self, require_support):
        self.CONF.incus.allow_bfv_evacuate = True
        root_bdm = {'boot_index': 0, 'connection_info': {}}
        block_device_info = {'block_device_mapping': [root_bdm]}
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertRaises(
            NotImplementedError, incus_driver.rebuild,
            None, mock.Mock(), mock.Mock(), [], None, {}, [], mock.Mock(),
            mock.Mock(), evacuate=True,
            block_device_info=block_device_info)

        require_support.assert_called_once_with(self.client, root_bdm)

    def test_overrides_match_compute_driver_call_signatures(self):
        def call_signature(method):
            return [
                (parameter.name, parameter.kind, parameter.default)
                for parameter in inspect.signature(method).parameters.values()
            ]

        for name, implementation in driver.IncusDriver.__dict__.items():
            base_method = getattr(nova_driver.ComputeDriver, name, None)
            if not callable(implementation) or not callable(base_method):
                continue
            self.assertEqual(
                call_signature(base_method), call_signature(implementation),
                'IncusDriver.%s does not match ComputeDriver.%s' %
                (name, name))

    def test_init_host_fail(self):
        def side_effect(conf):
            raise incuscore_exceptions.ClientConnectionFailed()
        self.Client.side_effect = side_effect
        self.Client.return_value = None

        incus_driver = driver.IncusDriver(None)

        self.assertRaises(exception.HostNotFound, incus_driver.init_host, None)

    def test_init_host_rejects_invalid_multipath_configuration(self):
        self.CONF.incus.volume_use_multipath = False
        self.CONF.incus.volume_enforce_multipath = True
        incus_driver = driver.IncusDriver(None)

        self.assertRaises(
            exception.InvalidConfiguration, incus_driver.init_host, None)

        self.Client.assert_not_called()

    @mock.patch('nova.virt.incus.driver.utils.get_root_helper',
                return_value='sudo nova-rootwrap')
    @mock.patch('nova.virt.incus.driver.connector.InitiatorConnector.factory')
    def test_brick_connector_uses_incus_volume_options(
            self, factory, get_root_helper):
        self.CONF.incus.volume_use_multipath = True
        self.CONF.incus.volume_enforce_multipath = True
        self.CONF.incus.num_volume_scan_tries = 7

        ORIGINAL_BRICK_GET_CONNECTOR('iscsi')

        factory.assert_called_once_with(
            'iscsi', 'sudo nova-rootwrap', driver=None,
            use_multipath=True, device_scan_attempts=7,
            enforce_multipath=True)

    @mock.patch('nova.virt.incus.driver.utils.get_root_helper',
                return_value='sudo nova-rootwrap')
    @mock.patch('nova.virt.incus.driver.connector.get_connector_properties')
    def test_connector_properties_use_incus_multipath_options(
            self, get_properties, get_root_helper):
        self.CONF.incus.volume_use_multipath = True
        self.CONF.incus.volume_enforce_multipath = True
        self.CONF.my_ip = '192.0.2.10'
        self.CONF.host = 'compute-01'

        driver.brick_get_connector_properties()

        get_properties.assert_called_once_with(
            'sudo nova-rootwrap', '192.0.2.10', True, True,
            host='compute-01')

    def test_get_info(self):
        container = mock.Mock()
        container.state.return_value = MockContainerState(
            'Running', {'usage': 4000, 'usage_peak': 4500}, 100)
        self.client.instances.get.return_value = container

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        info = incus_driver.get_info(instance)

        self.assertEqual(power_state.RUNNING, info.state)

    def test_get_info_stopped_does_not_query_runtime_state(self):
        container = mock.Mock(status='Stopped')
        self.client.instances.get.return_value = container
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        info = incus_driver.get_info(instance)

        self.assertEqual(power_state.SHUTDOWN, info.state)
        container.state.assert_not_called()

    @mock.patch('nova.virt.incus.driver.timeutils.utcnow')
    def test_get_diagnostics_reports_only_incus_counters(self, utcnow):
        utcnow.return_value = timeutils.parse_isotime(
            '2026-07-18T01:00:42Z')
        self.client.host_info['api_extensions'].append(
            'instance_state_started_at')
        state = mock.Mock(
            status_code=100,
            started_at='2026-07-18T01:00:00Z',
            cpu={'usage': 123456789},
            memory={'total': 2 * units.Gi, 'usage': 768 * units.Mi},
            disk={'root': {'total': 20 * units.Gi}},
            network={
                'lo': {'type': 'loopback', 'counters': {}},
                'eth0': {
                    'type': 'broadcast',
                    'hwaddr': '00:16:3e:12:34:56',
                    'counters': {
                        'bytes_received': 100,
                        'errors_received': 1,
                        'packets_dropped_inbound': 2,
                        'packets_received': 3,
                        'bytes_sent': 200,
                        'errors_sent': 4,
                        'packets_dropped_outbound': 5,
                        'packets_sent': 6,
                    },
                },
            })
        container = mock.Mock()
        container.state.return_value = state
        self.client.instances.get.return_value = container
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        result = incus_driver.get_diagnostics(instance)

        self.assertEqual({
            'cpu0_time': 123456789,
            'memory': 2 * units.Mi,
            'memory-used': 768 * units.Ki,
            'eth0_rx': 100,
            'eth0_rx_errors': 1,
            'eth0_rx_drop': 2,
            'eth0_rx_packets': 3,
            'eth0_tx': 200,
            'eth0_tx_errors': 4,
            'eth0_tx_drop': 5,
            'eth0_tx_packets': 6,
        }, result)
        self.assertNotIn('lo_rx', result)

        data = incus_driver._get_diagnostics_data(instance)
        self.assertEqual(42, data['uptime'])

    def test_get_diagnostics_omits_uptime_without_server_extension(self):
        state = mock.Mock(
            status_code=100, cpu={}, memory={}, disk={}, network={},
            started_at='2026-07-18T01:00:00Z')
        container = mock.Mock()
        container.state.return_value = state
        self.client.instances.get.return_value = container
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        data = incus_driver._get_diagnostics_data(instance)

        self.assertIsNone(data['uptime'])

    def test_get_diagnostics_missing_instance(self):
        self.client.instances.get.side_effect = incuscore_exceptions.NotFound(
            MockResponse(404))
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.InstanceNotFound,
            incus_driver.get_diagnostics, instance)

    def test_get_instance_diagnostics_requires_nova_compatibility_patch(self):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        original = driver.obj_fields.HypervisorDriver
        driver.obj_fields.HypervisorDriver = mock.Mock(spec=[])
        try:
            self.assertRaisesRegex(
                NotImplementedError, 'incus diagnostics driver identifier',
                incus_driver.get_instance_diagnostics, instance)
        finally:
            driver.obj_fields.HypervisorDriver = original

    def test_incus_disk_metrics_parses_only_requested_instance(self):
        self.client.api.metrics.get.return_value = mock.Mock(text='''
# TYPE incus_disk_read_bytes_total counter
incus_disk_read_bytes_total{device="rbd1",name="test"} 4096
incus_disk_reads_completed_total{device="rbd1",name="test"} 3
incus_disk_written_bytes_total{device="rbd1",name="test"} 8192
incus_disk_writes_completed_total{device="rbd1",name="test"} 4
incus_disk_read_bytes_total{device="rbd2",name="other"} 999
''')

        result = driver._incus_disk_metrics(self.client, 'test')

        self.assertEqual({
            'rbd1': {
                'rd_bytes': 4096,
                'rd_req': 3,
                'wr_bytes': 8192,
                'wr_req': 4,
            },
        }, result)
        self.client.api.metrics.get.assert_called_once_with(is_api=False)

    @mock.patch('nova.virt.incus.driver.os.path.realpath',
                return_value='/dev/rbd3')
    def test_disk_metric_device_maps_data_volume(self, realpath):
        profile = mock.Mock(devices={
            'volume-id': {
                'type': 'unix-block',
                'path': '/dev/vdb',
                'source': '/dev/disk/by-path/volume-id',
            },
        })
        instance = mock.Mock(root_device_name='/dev/vda')

        result = driver._disk_metric_device(profile, instance, 'vdb')

        self.assertEqual('rbd3', result)
        realpath.assert_called_once_with('/dev/disk/by-path/volume-id')

    @mock.patch('nova.virt.incus.driver.os.path.realpath',
                return_value='/dev/rbd4')
    @mock.patch('nova.virt.incus.driver.glob.glob')
    def test_disk_metric_device_maps_bfv_root(self, glob, realpath):
        volume_id = '8231d2e8-1111-2222-3333-444444444444'
        path = '/dev/rbd/cinder/volume-%s' % volume_id
        glob.return_value = [path]
        profile = mock.Mock(devices={
            'root': {
                'type': 'disk',
                'path': '/',
                'initial.ceph.rbd.image_name': 'volume-%s' % volume_id,
            },
        })
        instance = mock.Mock(root_device_name='/dev/vda')

        result = driver._disk_metric_device(profile, instance, '/dev/vda')

        self.assertEqual('rbd4', result)
        realpath.assert_called_once_with(path)

    @mock.patch('nova.virt.incus.driver._incus_disk_metrics')
    @mock.patch('nova.virt.incus.driver._disk_metric_device',
                return_value='rbd1')
    def test_block_stats(self, metric_device, disk_metrics):
        disk_metrics.return_value = {
            'rbd1': {
                'rd_req': 3,
                'rd_bytes': 4096,
                'wr_req': 4,
                'wr_bytes': 8192,
            },
        }
        profile = mock.Mock()
        self.client.profiles.get.return_value = profile
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        result = incus_driver.block_stats(instance, 'vda')

        self.assertEqual([3, 4096, 4, 8192, 0], result)
        metric_device.assert_called_once_with(profile, instance, 'vda')

    @mock.patch('nova.virt.incus.driver._incus_disk_metrics')
    @mock.patch('nova.virt.incus.driver._disk_metric_device',
                return_value='rbd1')
    def test_get_all_volume_usage(self, metric_device, disk_metrics):
        disk_metrics.return_value = {
            'rbd1': {
                'rd_req': 3,
                'rd_bytes': 4096,
                'wr_req': 4,
                'wr_bytes': 8192,
            },
        }
        profile = mock.Mock()
        self.client.profiles.get.return_value = profile
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        result = incus_driver.get_all_volume_usage(None, [{
            'instance': instance,
            'instance_bdms': [{
                'device_name': '/dev/vda',
                'volume_id': 'volume-id',
            }],
        }])

        self.assertEqual([{
            'volume': 'volume-id',
            'instance': instance,
            'rd_req': 3,
            'rd_bytes': 4096,
            'wr_req': 4,
            'wr_bytes': 8192,
        }], result)
        metric_device.assert_called_once_with(profile, instance, '/dev/vda')
        disk_metrics.assert_called_once_with(self.client, instance.name)

    def test_list_instances(self):
        self.client.instances.all.return_value = [
            MockContainer('mock-instance-1'),
            MockContainer('mock-instance-2'),
        ]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        instances = incus_driver.list_instances()

        self.assertEqual(['mock-instance-1', 'mock-instance-2'], instances)

    def test_list_instance_uuids_ignores_unmanaged_instances_and_vms(self):
        managed = mock.Mock(
            type='container',
            config={'user.openstack.uuid': 'managed-uuid'})
        unmanaged = mock.Mock(type='container', config={})
        virtual_machine = mock.Mock(
            type='virtual-machine',
            config={'user.openstack.uuid': 'vm-uuid'})
        self.client.instances.all.return_value = [
            managed, unmanaged, virtual_machine]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertEqual(['managed-uuid'], incus_driver.list_instance_uuids())

    def test_incus_cloud_init_config(self):
        instance = mock.Mock(
            uuid='instance-uuid',
            user_data=base64.b64encode(b'#cloud-config\nruncmd: []\n'),
            key_name='tenant-key',
            key_data='ssh-ed25519 AAAATEST tenant')

        self.assertEqual({
            'user.openstack.uuid': 'instance-uuid',
            'cloud-init.user-data': '#cloud-config\nruncmd: []\n',
            'user.meta-data': (
                'public-keys:\n'
                '  "tenant-key": "ssh-ed25519 AAAATEST tenant"\n'),
        }, driver._incus_cloud_init_config(instance))

    @mock.patch('nova.virt.incus.driver.IMAGE_API')
    @mock.patch('nova.virt.incus.driver.lockutils.lock')
    def test_spawn_unified_image(self, lock, IMAGE_API=None):
        def image_get(*args, **kwargs):
            raise incuscore_exceptions.LXDAPIException(MockResponse(404))
        self.client.images.get_by_alias.side_effect = image_get
        self.client.images.exists.return_value = False
        image = {'name': mock.Mock(), 'disk_format': 'raw'}
        IMAGE_API.get.return_value = image

        def download_unified(*args, **kwargs):
            # unified image with metadata
            # structure is gzipped tarball, content:
            # /
            #  metadata.yaml
            #  rootfs/
            unified_tgz = 'H4sIALpegVkAA+3SQQ7CIBCFYY7CCXRAppwHo66sTVpYeHsh0a'\
                          'Ru1A2Lxv/bDGQmYZLHeM7plHLa3dN4NX1INQyhVRdV1vXFuIML'\
                          '4lVVopF28cZKp33elCWn2VpTjuWWy4e5L/2NmqcpX5Z91zdawD'\
                          'HqT/kHrf/E+Xo0Vrtu9fTn+QMAAAAAAAAAAAAAAADYrgfk/3zn'\
                          'ACgAAA=='
            with closing(open(kwargs['dest_path'], 'wb+')) as img:
                img.write(base64.b64decode(unified_tgz))
        IMAGE_API.download = download_unified
        self.test_spawn()

    @mock.patch('nova.virt.configdrive.required_by')
    def test_spawn(self, configdrive, neutron_failure=None):
        def container_get(*args, **kwargs):
            raise incuscore_exceptions.LXDAPIException(MockResponse(404))
        self.client.instances.get.side_effect = container_get
        configdrive.return_value = False
        container = mock.Mock()
        self.client.instances.create.return_value = container

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        image_meta = mock.Mock()
        injected_files = mock.Mock()
        admin_password = mock.Mock()
        allocations = mock.Mock()
        network_info = [_VIF]
        block_device_info = mock.Mock()
        virtapi = manager.ComputeVirtAPI(mock.MagicMock())

        incus_driver = driver.IncusDriver(virtapi)
        incus_driver.init_host(None)
        # XXX: rockstar (6 Jul 2016) - There are a number of XXX comments
        # related to these calls in spawn. They require some work before we
        # can take out these mocks and follow the real codepaths.
        incus_driver.firewall_driver = mock.Mock()

        incus_driver.spawn(
            ctx, instance, image_meta, injected_files, admin_password,
            allocations, network_info, block_device_info)

        self.vif_driver.plug.assert_called_once_with(
            instance, network_info[0])
        fd = incus_driver.firewall_driver
        fd.setup_basic_filtering.assert_called_once_with(
            instance, network_info)
        fd.apply_instance_filter.assert_called_once_with(
            instance, network_info)
        container.start.assert_called_once_with(wait=True)
        self.client.instances.create.assert_called_once_with({
            'name': instance.name,
            'type': 'container',
            'profiles': [self.client.profiles.create.return_value.name],
            'config': {
                **driver._incus_cloud_init_config(instance),
                'boot.autostart': 'false',
            },
            'source': {
                'type': 'image',
                'alias': instance.image_ref,
            },
        }, wait=True)

    def test_spawn_rejects_ephemeral_before_allocating_resources(self):
        get_ephemerals = driver.driver.block_device_info_get_ephemerals
        get_ephemerals.return_value = [{'virtual_name': 'ephemeral0'}]
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=512)
        virtapi = manager.ComputeVirtAPI(mock.MagicMock())
        incus_driver = driver.IncusDriver(virtapi)
        incus_driver.init_host(None)
        block_device_info = {'block_device_mapping': []}

        self.assertRaises(
            exception.InvalidConfiguration,
            incus_driver.spawn,
            ctx, instance, mock.Mock(), [], None, mock.Mock(), [],
            block_device_info)

        self.client.images.get_by_alias.assert_not_called()
        self.client.instances.create.assert_not_called()
        self.vif_driver.plug.assert_not_called()
        get_ephemerals.assert_called_once_with(block_device_info)

    @mock.patch('nova.virt.configdrive.required_by', return_value=False)
    def test_spawn_boot_from_cinder_rbd(self, configdrive):
        def container_get(*args, **kwargs):
            raise incuscore_exceptions.LXDAPIException(MockResponse(404))

        volume_id = '8231d2e8-1111-4222-8333-123456789abc'
        self.client.instances.get.side_effect = container_get
        self.client.host_info['api_extensions'].append(
            'storage_driver_cephext')
        self.CONF.incus.boot_from_volume_storage_pools = {
            'cinder-volumes': 'cinder'}
        bfv_pool = self.client.storage_pools.get.return_value
        bfv_pool.driver = 'cephext'
        bfv_pool.config = {'source': 'cinder-volumes'}
        root_bdm = {
            'boot_index': 0,
            'connection_info': {
                'driver_volume_type': 'rbd',
                'serial': volume_id,
                'data': {
                    'name': 'cinder-volumes/volume-%s' % volume_id,
                    'volume_id': volume_id,
                    'access_mode': 'rw',
                    'qos_specs': {
                        'read_iops_sec': '700',
                        'write_bytes_sec': '50000000',
                    },
                },
            },
        }
        profile = self.client.profiles.create.return_value
        profile.devices = {'root': {'type': 'disk', 'path': '/',
                                    'size': '20GB'}}
        container = self.client.instances.create.return_value

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-bfv', memory_mb=512)
        image_meta = mock.Mock(disk_format='raw', container_format='bare')
        incus_driver = driver.IncusDriver(
            manager.ComputeVirtAPI(mock.MagicMock()))
        incus_driver.init_host(None)
        incus_driver.firewall_driver = mock.Mock()

        incus_driver.spawn(
            ctx, instance, image_meta, [], None, mock.Mock(), [],
            {'block_device_mapping': [root_bdm]})

        self.client.images.get_by_alias.assert_not_called()
        self.client.storage_pools.get.assert_called_with('cinder')
        self.assertEqual({
            'type': 'disk',
            'path': '/',
            'pool': 'cinder',
            'initial.ceph.rbd.image_name': 'volume-%s' % volume_id,
            'limits.read': '700iops',
            'limits.write': '50000000B',
        }, profile.devices['root'])
        profile.save.assert_called()
        self.client.instances.create.assert_called_once_with({
            'name': instance.name,
            'type': 'container',
            'profiles': [profile.name],
            'config': {
                **driver._incus_cloud_init_config(instance),
                'boot.autostart': 'false',
            },
            'source': {'type': 'none'},
        }, wait=True)
        container.start.assert_called_once_with(wait=True)

    def test_boot_from_volume_rejects_non_rbd(self):
        bdm = {'connection_info': {
            'driver_volume_type': 'iscsi',
            'data': {'volume_id': '8231d2e8-1111-4222-8333-123456789abc'},
        }}
        self.assertRaises(exception.InvalidConfiguration,
                          driver._cinder_rbd_root, bdm)

    def test_bfv_storage_pool_selects_by_cinder_rbd_pool(self):
        self.CONF.incus.boot_from_volume_storage_pools = {
            'ssd-rep3-rbd-pool': 'cinder-ssd-rep3',
            'nvme-rep3-rbd-pool': 'cinder-nvme-rep3',
        }

        self.assertEqual(
            'cinder-nvme-rep3',
            driver._bfv_storage_pool_name('nvme-rep3-rbd-pool'))

    def test_bfv_storage_pool_rejects_unconfigured_cinder_pool(self):
        self.CONF.incus.boot_from_volume_storage_pools = {
            'ssd-rep3-rbd-pool': 'cinder-ssd-rep3'}

        self.assertRaisesRegex(
            exception.InvalidConfiguration,
            'No Incus cephext storage pool is configured',
            driver._bfv_storage_pool_name,
            'nvme-rep3-rbd-pool')

    def test_boot_from_volume_rejects_mismatched_rbd_uuid(self):
        bdm = {'connection_info': {
            'driver_volume_type': 'rbd',
            'serial': '8231d2e8-1111-4222-8333-123456789abc',
            'data': {
                'name': ('cinder-volumes/volume-'
                         '9231d2e8-1111-4222-8333-123456789abc'),
            },
        }}
        self.assertRaises(exception.InvalidConfiguration,
                          driver._cinder_rbd_root, bdm)

    def test_boot_from_volume_rejects_multiple_root_volumes(self):
        mapping = [{'boot_index': 0}, {'boot_index': '0'}]
        with mock.patch(
                'nova.virt.incus.driver.driver.block_device_info_get_mapping',
                return_value=mapping):
            self.assertRaises(
                exception.InvalidConfiguration,
                driver._boot_from_volume,
                {'block_device_mapping': mapping})

    def test_spawn_already_exists(self):
        """InstanceExists is raised if the container already exists."""
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        image_meta = mock.Mock()
        injected_files = mock.Mock()
        admin_password = mock.Mock()
        allocations = mock.Mock()

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.InstanceExists,

            incus_driver.spawn,
            ctx, instance, image_meta, injected_files, admin_password,
            allocations, None, None)

    @mock.patch('nova.virt.configdrive.required_by')
    def test_spawn_with_configdrive(self, configdrive):
        def container_get(*args, **kwargs):
            raise incuscore_exceptions.LXDAPIException(MockResponse(404))

        self.client.instances.get.side_effect = container_get
        configdrive.return_value = True

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        image_meta = mock.Mock()
        injected_files = mock.Mock()
        admin_password = mock.Mock()
        allocations = mock.Mock()
        network_info = [_VIF]
        block_device_info = mock.Mock()
        virtapi = manager.ComputeVirtAPI(mock.MagicMock())

        incus_driver = driver.IncusDriver(virtapi)
        incus_driver.init_host(None)
        # XXX: rockstar (6 Jul 2016) - There are a number of XXX comments
        # related to these calls in spawn. They require some work before we
        # can take out these mocks and follow the real codepaths.
        incus_driver.firewall_driver = mock.Mock()
        incus_driver._add_configdrive = mock.Mock()

        incus_driver.spawn(
            ctx, instance, image_meta, injected_files, admin_password,
            allocations, network_info, block_device_info)

        self.vif_driver.plug.assert_called_once_with(
            instance, network_info[0])
        fd = incus_driver.firewall_driver
        fd.setup_basic_filtering.assert_called_once_with(
            instance, network_info)
        fd.apply_instance_filter.assert_called_once_with(
            instance, network_info)
        configdrive.assert_called_once_with(instance)
        incus_driver.client.profiles.get.assert_called_once_with(instance.name)
        profile = incus_driver.client.profiles.get.return_value
        profile.devices.update.assert_called_once_with({
            'configdrive': {
                'path': '/config-drive',
                'source': incus_driver._add_configdrive.return_value,
                'type': 'disk',
                'readonly': 'true',
            }
        })
        profile.save.assert_called_once_with()

    @mock.patch('nova.virt.incus.driver.fileutils.ensure_tree')
    @mock.patch('nova.virt.incus.driver.os.listdir', return_value=[])
    @mock.patch('nova.virt.incus.driver.processutils.execute',
                return_value=('', ''))
    @mock.patch('nova.virt.incus.driver.utils.get_root_helper',
                return_value='sudo nova-rootwrap')
    @mock.patch('nova.virt.incus.driver.configdrive.ConfigDriveBuilder')
    @mock.patch('nova.virt.incus.driver.instance_metadata.InstanceMetadata')
    def test_add_configdrive_uses_modern_instance_metadata_signature(
            self, instance_metadata_mock, builder_mock, root_helper_mock,
            execute_mock, listdir_mock, ensure_tree_mock):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        injected_files = [('etc/example', b'content')]
        network_info = [_VIF]
        container = self.client.instances.get.return_value
        container.config = {
            'volatile.last_state.idmap': jsonutils.dumps([
                {'Isuid': True, 'Hostid': 100000},
                {'Isgid': True, 'Hostid': 100000},
            ])
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver._add_configdrive(
            ctx, instance, injected_files, 'secret', network_info)

        instance_metadata_mock.assert_called_once_with(
            instance, content=injected_files,
            extra_md={'admin_pass': 'secret'}, network_info=network_info)
        builder_mock.assert_called_once_with(
            instance_md=instance_metadata_mock.return_value)
        self.assertTrue(execute_mock.call_args_list)
        for call in execute_mock.call_args_list:
            self.assertEqual('sudo nova-rootwrap',
                             call.kwargs['root_helper'])

    @mock.patch('nova.virt.configdrive.required_by')
    def test_spawn_profile_fail(self, configdrive, neutron_failure=None):
        """Cleanup is called when profile creation fails."""
        def container_get(*args, **kwargs):
            raise incuscore_exceptions.LXDAPIException(MockResponse(404))

        def profile_create(*args, **kwargs):
            raise incuscore_exceptions.LXDAPIException(MockResponse(500))
        self.client.instances.get.side_effect = container_get
        self.client.profiles.create.side_effect = profile_create
        configdrive.return_value = False
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        image_meta = mock.Mock()
        injected_files = mock.Mock()
        admin_password = mock.Mock()
        allocations = mock.Mock()
        network_info = [_VIF]
        block_device_info = mock.Mock()
        virtapi = manager.ComputeVirtAPI(mock.MagicMock())

        incus_driver = driver.IncusDriver(virtapi)
        incus_driver.init_host(None)
        incus_driver.cleanup = mock.Mock()

        self.assertRaises(
            incuscore_exceptions.LXDAPIException,
            incus_driver.spawn,
            ctx, instance, image_meta, injected_files, admin_password,
            allocations, network_info, block_device_info)
        incus_driver.cleanup.assert_called_once_with(
            ctx, instance, network_info, block_device_info)

    @mock.patch('nova.virt.configdrive.required_by')
    def test_spawn_container_fail(self, configdrive, neutron_failure=None):
        """Cleanup is called when container creation fails."""
        def container_get(*args, **kwargs):
            raise incuscore_exceptions.LXDAPIException(MockResponse(404))

        def container_create(*args, **kwargs):
            raise incuscore_exceptions.LXDAPIException(MockResponse(500))
        self.client.instances.get.side_effect = container_get
        self.client.instances.create.side_effect = container_create
        configdrive.return_value = False
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        image_meta = mock.Mock()
        injected_files = mock.Mock()
        admin_password = mock.Mock()
        allocations = mock.Mock()
        network_info = [_VIF]
        block_device_info = mock.Mock()
        virtapi = manager.ComputeVirtAPI(mock.MagicMock())

        incus_driver = driver.IncusDriver(virtapi)
        incus_driver.init_host(None)
        incus_driver.cleanup = mock.Mock()

        self.assertRaises(
            incuscore_exceptions.LXDAPIException,
            incus_driver.spawn,
            ctx, instance, image_meta, injected_files, admin_password,
            allocations, network_info, block_device_info)
        incus_driver.cleanup.assert_called_once_with(
            ctx, instance, network_info, block_device_info)

    @mock.patch('nova.virt.configdrive.required_by', return_value=False)
    def test_spawn_container_cleanup_fail(self, configdrive):
        """Cleanup is called but also fail when container creation fails."""
        self.client.instances.get.side_effect = (
            incuscore_exceptions.LXDAPIException(MockResponse(404)))
        container = mock.Mock()
        self.client.instances.create.return_value = container

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        image_meta = mock.Mock()
        injected_files = mock.Mock()
        admin_password = mock.Mock()
        allocations = mock.Mock()
        network_info = [_VIF]
        block_device_info = mock.Mock()
        virtapi = manager.ComputeVirtAPI(mock.MagicMock())

        incus_driver = driver.IncusDriver(virtapi)
        incus_driver.init_host(None)

        container.start.side_effect = (
            incuscore_exceptions.LXDAPIException(MockResponse(500)))
        incus_driver.cleanup = mock.Mock()
        incus_driver.cleanup.side_effect = Exception("a bad thing")

        self.assertRaises(
            incuscore_exceptions.LXDAPIException,
            incus_driver.spawn,
            ctx, instance, image_meta, injected_files, admin_password,
            allocations, network_info, block_device_info)
        incus_driver.cleanup.assert_called_once_with(
            ctx, instance, network_info, block_device_info)
        container.delete.assert_called_once_with(wait=True)

    def test_spawn_container_start_fail(self, neutron_failure=None):
        def container_get(*args, **kwargs):
            raise incuscore_exceptions.LXDAPIException(MockResponse(404))

        def side_effect(*args, **kwargs):
            raise incuscore_exceptions.LXDAPIException(MockResponse(200))

        self.client.instances.get.side_effect = container_get
        container = mock.Mock()
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        image_meta = mock.Mock()
        injected_files = mock.Mock()
        admin_password = mock.Mock()
        allocations = mock.Mock()
        network_info = [_VIF]
        block_device_info = mock.Mock()
        virtapi = manager.ComputeVirtAPI(mock.MagicMock())

        incus_driver = driver.IncusDriver(virtapi)
        incus_driver.init_host(None)
        incus_driver.cleanup = mock.Mock()
        incus_driver.client.instances.create = mock.Mock(
            side_effect=side_effect)
        container.start.side_effect = side_effect

        self.assertRaises(
            incuscore_exceptions.LXDAPIException,
            incus_driver.spawn,
            ctx, instance, image_meta, injected_files, admin_password,
            allocations, network_info, block_device_info)
        incus_driver.cleanup.assert_called_once_with(
            ctx, instance, network_info, block_device_info)

    def _test_spawn_instance_with_network_events(self, neutron_failure=None):
        generated_events = []

        def wait_timeout():
            event = mock.MagicMock()
            if neutron_failure == 'timeout':
                raise eventlet.timeout.Timeout()
            elif neutron_failure == 'error':
                event.status = 'failed'
            else:
                event.status = 'completed'
            return event

        def fake_prepare(instance, event_name):
            m = mock.MagicMock()
            m.instance = instance
            m.event_name = event_name
            m.wait.side_effect = wait_timeout
            generated_events.append(m)
            return m

        virtapi = manager.ComputeVirtAPI(mock.MagicMock())
        prepare = virtapi._compute.instance_events.prepare_for_instance_event
        prepare.side_effect = fake_prepare
        drv = driver.IncusDriver(virtapi)

        instance_href = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)

        @mock.patch.object(drv, 'plug_vifs')
        @mock.patch('nova.virt.configdrive.required_by')
        def test_spawn(configdrive, plug_vifs):
            def container_get(*args, **kwargs):
                raise incuscore_exceptions.LXDAPIException(MockResponse(404))
            self.client.instances.get.side_effect = container_get
            configdrive.return_value = False

            ctx = context.get_admin_context()
            instance = fake_instance.fake_instance_obj(
                ctx, name='test', memory_mb=0)
            image_meta = mock.Mock()
            injected_files = mock.Mock()
            admin_password = mock.Mock()
            allocations = mock.Mock()
            network_info = [_VIF]
            block_device_info = mock.Mock()

            drv.init_host(None)
            drv.spawn(
                ctx, instance, image_meta, injected_files, admin_password,
                allocations, network_info, block_device_info)

        test_spawn()

        if cfg.CONF.vif_plugging_timeout:
            prepare.assert_has_calls([
                mock.call(instance_href, 'network-vif-plugged-vif1'),
                mock.call(instance_href, 'network-vif-plugged-vif2')])
            for event in generated_events:
                if neutron_failure and generated_events.index(event) != 0:
                    self.assertEqual(0, event.call_count)
        else:
            self.assertEqual(0, prepare.call_count)

    def test_spawn_instance_with_network_events(self):
        self.flags(vif_plugging_timeout=0)
        self._test_spawn_instance_with_network_events()

    def test_spawn_instance_with_events_neutron_failed_nonfatal_timeout(self):
        self.flags(vif_plugging_timeout=0)
        self.flags(vif_plugging_is_fatal=False)
        self._test_spawn_instance_with_network_events(
            neutron_failure='timeout')

    @mock.patch('nova.virt.incus.driver.lockutils.lock')
    def test_destroy(self, lock):
        mock_container = mock.Mock()
        mock_container.status = 'Running'
        self.client.instances.get.return_value = mock_container
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = [_VIF]

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.cleanup = mock.Mock()  # There is a separate cleanup test

        incus_driver.destroy(ctx, instance, network_info)

        incus_driver.cleanup.assert_called_once_with(
            ctx, instance, network_info, None)
        incus_driver.client.instances.get.assert_called_once_with(
            instance.name)
        mock_container.stop.assert_called_once_with(wait=True)
        mock_container.delete.assert_called_once_with(wait=True)

    @mock.patch('nova.virt.incus.driver.lockutils.lock')
    def test_destroy_retries_busy_stop_before_cleanup(self, lock):
        mock_container = mock.Mock()
        mock_container.status = 'Running'
        mock_container.stop.side_effect = [
            incus_api_exception(
                400,
                'Instance is busy running an "update" operation'),
            None,
        ]
        self.client.instances.get.return_value = mock_container
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = [_VIF]

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.cleanup = mock.Mock()

        incus_driver.destroy(ctx, instance, network_info)

        self.assertEqual(2, mock_container.stop.call_count)
        mock_container.delete.assert_called_once_with(wait=True)
        incus_driver.cleanup.assert_called_once_with(
            ctx, instance, network_info, None)

    @mock.patch('nova.virt.incus.driver.lockutils.lock')
    def test_destroy_busy_stop_failure_does_not_cleanup(self, lock):
        mock_container = mock.Mock()
        mock_container.status = 'Running'
        error = incus_api_exception(
            400, 'Instance is busy running an "update" operation')
        mock_container.stop.side_effect = error
        self.client.instances.get.return_value = mock_container
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = [_VIF]

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.cleanup = mock.Mock(
            side_effect=RuntimeError('profile still in use'))

        self.assertRaises(
            incuscore_exceptions.LXDAPIException,
            incus_driver.destroy, ctx, instance, network_info)

        self.assertEqual(
            self.CONF.incus.migration_finish_retries,
            mock_container.stop.call_count)
        mock_container.delete.assert_not_called()
        incus_driver.cleanup.assert_not_called()

    @mock.patch('nova.virt.incus.driver.lockutils.lock')
    def test_destroy_retries_busy_delete_before_cleanup(self, lock):
        mock_container = mock.Mock()
        mock_container.status = 'Stopped'
        mock_container.delete.side_effect = [
            incus_api_exception(
                409,
                'Instance is busy running an "update" operation'),
            None,
        ]
        self.client.instances.get.return_value = mock_container
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = [_VIF]

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.cleanup = mock.Mock()

        incus_driver.destroy(ctx, instance, network_info)

        mock_container.stop.assert_not_called()
        self.assertEqual(2, mock_container.delete.call_count)
        incus_driver.cleanup.assert_called_once_with(
            ctx, instance, network_info, None)

    @mock.patch('nova.virt.incus.driver.lockutils.lock')
    def test_destroy_refreshes_state_after_busy_delete(self, lock):
        stopped_container = mock.Mock()
        stopped_container.status = 'Stopped'
        stopped_container.delete.side_effect = incus_operation_exception(
            400,
            'Failed to create instance delete operation: Instance is busy '
            'running a "restart" operation')
        running_container = mock.Mock()
        running_container.status = 'Running'
        self.client.instances.get.side_effect = [
            stopped_container,
            running_container,
        ]
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = [_VIF]

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.cleanup = mock.Mock()

        incus_driver.destroy(ctx, instance, network_info)

        self.assertEqual(2, self.client.instances.get.call_count)
        stopped_container.stop.assert_not_called()
        stopped_container.delete.assert_called_once_with(wait=True)
        running_container.stop.assert_called_once_with(wait=True)
        running_container.delete.assert_called_once_with(wait=True)
        incus_driver.cleanup.assert_called_once_with(
            ctx, instance, network_info, None)

    @mock.patch('nova.virt.incus.driver.lockutils.lock')
    def test_destroy_retries_async_busy_stop_before_cleanup(self, lock):
        mock_container = mock.Mock()
        mock_container.status = 'Running'
        mock_container.stop.side_effect = [
            incus_operation_exception(
                400,
                'Instance is busy running an "update" operation'),
            None,
        ]
        self.client.instances.get.return_value = mock_container
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = [_VIF]

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.cleanup = mock.Mock()

        incus_driver.destroy(ctx, instance, network_info)

        self.assertEqual(2, mock_container.stop.call_count)
        mock_container.delete.assert_called_once_with(wait=True)
        incus_driver.cleanup.assert_called_once_with(
            ctx, instance, network_info, None)

    @mock.patch('nova.virt.incus.driver.lockutils.lock')
    def test_destroy_async_busy_stop_failure_does_not_cleanup(self, lock):
        mock_container = mock.Mock()
        mock_container.status = 'Running'
        mock_container.stop.side_effect = incus_operation_exception(
            400, 'Instance is busy running an "update" operation')
        self.client.instances.get.return_value = mock_container
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = [_VIF]

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.cleanup = mock.Mock()

        self.assertRaises(
            incuscore_exceptions.LXDAPIException,
            incus_driver.destroy, ctx, instance, network_info)

        self.assertEqual(
            self.CONF.incus.migration_finish_retries,
            mock_container.stop.call_count)
        mock_container.delete.assert_not_called()
        incus_driver.cleanup.assert_not_called()

    @mock.patch('nova.virt.incus.driver.lockutils.lock')
    def test_destroy_when_in_rescue(self, lock):
        mock_stopped_container = mock.Mock()
        mock_stopped_container.status = 'Stopped'
        mock_rescued_container = mock.Mock()
        mock_rescued_container.status = 'Running'
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = [_VIF]

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.cleanup = mock.Mock()

        # set the vm_state on the fake instance to RESCUED
        instance.vm_state = vm_states.RESCUED

        # set up the containers.get to return the stopped container and then
        # the rescued container
        self.client.instances.get.side_effect = [
            mock_stopped_container, mock_rescued_container]

        incus_driver.destroy(ctx, instance, network_info)

        incus_driver.cleanup.assert_called_once_with(
            ctx, instance, network_info, None)
        incus_driver.client.instances.get.assert_has_calls([
            mock.call(instance.name),
            mock.call('{}-rescue'.format(instance.name))])
        mock_stopped_container.stop.assert_not_called()
        mock_stopped_container.delete.assert_called_once_with(wait=True)
        mock_rescued_container.stop.assert_called_once_with(wait=True)
        mock_rescued_container.delete.assert_called_once_with(wait=True)

    @mock.patch('nova.virt.incus.driver.lockutils.lock')
    def test_destroy_without_instance(self, lock):
        def side_effect(*args, **kwargs):
            raise incuscore_exceptions.LXDAPIException(MockResponse(404))
        self.client.instances.get.side_effect = side_effect

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = [_VIF]

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.cleanup = mock.Mock()  # There is a separate cleanup test

        incus_driver.destroy(ctx, instance, network_info)
        incus_driver.cleanup.assert_called_once_with(
            ctx, instance, network_info, None)

    @mock.patch('nova.virt.incus.driver.neutron')
    @mock.patch('os.path.exists', mock.Mock(return_value=True))
    @mock.patch.object(driver.os, 'getgid', return_value=1001)
    @mock.patch.object(driver.os, 'getuid', return_value=1001)
    @mock.patch('shutil.rmtree')
    @mock.patch.object(driver.privsep_path, 'chown')
    def test_cleanup(self, chown, rmtree, getuid, getgid, _):
        mock_profile = mock.Mock()
        self.client.profiles.get.return_value = mock_profile

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = [_VIF]
        instance_dir = common.InstanceAttributes(instance).instance_dir
        block_device_info = {'block_device_mapping': []}

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.firewall_driver = mock.Mock()

        incus_driver.cleanup(ctx, instance, network_info, block_device_info)

        self.vif_driver.unplug.assert_called_once_with(
            instance, network_info[0])
        incus_driver.firewall_driver.unfilter_instance.assert_called_once_with(
            instance, network_info)
        chown.assert_called_once_with(
            instance_dir, uid=1001, gid=1001, recursive=True)
        rmtree.assert_called_once_with(instance_dir)
        mock_profile.delete.assert_called_once_with()

    @mock.patch.object(driver.storage, 'detach_ephemeral')
    @mock.patch.object(driver.IncusDriver, 'unplug_vifs')
    @mock.patch.object(driver.os.path, 'exists', return_value=False)
    def test_cleanup_disconnects_data_volume_before_profile_delete(
            self, _exists, _unplug_vifs, _detach_ephemeral):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        connection_info = {
            'driver_volume_type': 'rbd',
            'serial': 'data-volume',
            'data': {'volume_id': 'data-volume'},
        }
        block_device_info = {'block_device_mapping': [
            {
                'boot_index': 0,
                'connection_info': {
                    'driver_volume_type': 'rbd',
                    'serial': 'root-volume',
                    'data': {'volume_id': 'root-volume'},
                },
                'mount_device': '/dev/sda',
            },
            {
                'boot_index': None,
                'connection_info': connection_info,
                'mount_device': '/dev/sdb',
            },
        ]}
        profile = self.client.profiles.get.return_value
        profile.devices = {'data-volume': {'type': 'unix-block'}}
        profile.config = {
            driver._volume_device_info_key('data-volume'): '{}'}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.firewall_driver = mock.Mock()

        def detach(*args):
            profile.devices.clear()
            profile.config.clear()

        incus_driver.detach_volume = mock.Mock(side_effect=detach)

        incus_driver.cleanup(
            ctx, instance, [], block_device_info, destroy_vifs=False)

        incus_driver.detach_volume.assert_called_once_with(
            ctx, connection_info, instance, '/dev/sdb')
        profile.delete.assert_called_once_with()

    @mock.patch.object(driver.storage, 'detach_ephemeral')
    @mock.patch.object(driver.IncusDriver, 'unplug_vifs')
    @mock.patch.object(driver.os.path, 'exists', return_value=False)
    def test_cleanup_retains_profile_when_data_disconnect_fails(
            self, _exists, _unplug_vifs, _detach_ephemeral):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        connection_info = {
            'driver_volume_type': 'rbd',
            'serial': 'data-volume',
            'data': {'volume_id': 'data-volume'},
        }
        block_device_info = {'block_device_mapping': [{
            'boot_index': None,
            'connection_info': connection_info,
            'mount_device': '/dev/sdb',
        }]}
        profile = self.client.profiles.get.return_value
        profile.devices = {'data-volume': {'type': 'unix-block'}}
        profile.config = {
            driver._volume_device_info_key('data-volume'): '{}'}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.firewall_driver = mock.Mock()
        incus_driver.detach_volume = mock.Mock(
            side_effect=RuntimeError('disconnect failed'))

        self.assertRaises(
            RuntimeError, incus_driver.cleanup, ctx, instance, [],
            block_device_info, destroy_vifs=False)

        profile.delete.assert_not_called()

    def test_reboot(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.reboot(ctx, instance, None, None)

        self.client.instances.get.assert_called_once_with(instance.name)
        self.client.instances.get.return_value.restart.assert_called_once_with(
            force=True, wait=True)

    def test_cleanup_lingering_bfv_source_record(self):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test')
        container = self.client.instances.get.return_value
        container.status = 'Stopped'
        container.config = {
            'user.openstack.uuid': instance.uuid,
            'volatile.migration.storage_handover': 'committed',
        }
        container.devices = {
            'root': {
                'initial.ceph.rbd.image_name': 'volume-root',
                'type': 'disk',
                'path': '/',
            },
        }
        profile = self.client.profiles.get.return_value
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        result = incus_driver.cleanup_lingering_instance_resources(instance)

        self.assertTrue(result)
        container.delete.assert_called_once_with(wait=True)
        profile.delete.assert_called_once_with()

    def test_cleanup_lingering_bfv_requires_handover_protection(self):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test')
        container = self.client.instances.get.return_value
        container.status = 'Stopped'
        container.config = {'user.openstack.uuid': instance.uuid}
        container.devices = {
            'root': {
                'initial.ceph.rbd.image_name': 'volume-root',
                'type': 'disk',
                'path': '/',
            },
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        result = incus_driver.cleanup_lingering_instance_resources(instance)

        self.assertFalse(result)
        container.delete.assert_not_called()

    def test_cleanup_lingering_record_rejects_running_instance(self):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test')
        container = self.client.instances.get.return_value
        container.status = 'Running'
        container.config = {'user.openstack.uuid': instance.uuid}
        container.devices = {}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        result = incus_driver.cleanup_lingering_instance_resources(instance)

        self.assertFalse(result)
        container.delete.assert_not_called()

    def test_cleanup_lingering_record_rejects_uuid_mismatch(self):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test')
        container = self.client.instances.get.return_value
        container.status = 'Stopped'
        container.config = {'user.openstack.uuid': 'different'}
        container.devices = {}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        result = incus_driver.cleanup_lingering_instance_resources(instance)

        self.assertFalse(result)
        container.delete.assert_not_called()

    @mock.patch('nova.virt.incus.driver.neutron')
    def test_get_console_output(self, _):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        self.client.instances.get.return_value.console_log.return_value = (
            b'x' * (driver.MAX_CONSOLE_BYTES + 1))

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        contents = incus_driver.get_console_output(context, instance)

        self.assertEqual(b'x' * driver.MAX_CONSOLE_BYTES, contents)
        self.client.instances.get.assert_called_once_with(instance.name)

    def test_reboot_starts_stopped_migration_target(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        container = self.client.instances.get.return_value
        container.status = 'Stopped'
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.reboot(ctx, instance, None, 'HARD')

        container.start.assert_called_once_with(wait=True)
        container.restart.assert_not_called()

    @mock.patch('nova.virt.driver.block_device_info_get_mapping')
    def test_reboot_restores_missing_data_volume_before_start(
            self, get_mapping):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        root = {'boot_index': 0}
        data = {
            'boot_index': 1,
            'connection_info': {
                'serial': 'volume-data',
                'driver_volume_type': 'rbd',
                'data': {},
            },
            'mount_device': '/dev/vdb',
        }
        get_mapping.return_value = [root, data]
        profile = self.client.profiles.get.return_value
        profile.devices = {}
        profile.config = {}
        container = self.client.instances.get.return_value
        container.status = 'Stopped'
        container.devices = {}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.attach_volume = mock.Mock()

        incus_driver.reboot(
            ctx, instance, None, 'HARD', block_device_info={})

        incus_driver.attach_volume.assert_called_once_with(
            ctx, data['connection_info'], instance, '/dev/vdb')
        container.start.assert_called_once_with(wait=True)

    def test_needs_migration_recovery_requires_explicit_marker(self):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test')
        container = self.client.instances.get.return_value
        container.status = 'Stopped'
        container.config = {
            'user.openstack.uuid': instance.uuid,
        }
        container.devices = {
            'root': {
                'initial.ceph.rbd.image_name': 'volume-root',
                'type': 'disk',
                'path': '/',
            },
        }
        profile = self.client.profiles.get.return_value
        profile.config = {}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertFalse(incus_driver.needs_migration_recovery(instance))
        profile.config[driver.MIGRATION_RECOVERY_KEY] = 'true'
        self.assertTrue(incus_driver.needs_migration_recovery(instance))

    def test_reboot_clears_migration_recovery_marker(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        container = self.client.instances.get.return_value
        container.status = 'Stopped'
        profile = self.client.profiles.get.return_value
        profile.config = {driver.MIGRATION_RECOVERY_KEY: 'true'}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._reconcile_reboot_data_volumes = mock.Mock()
        incus_driver._validate_reboot_vifs = mock.Mock()
        incus_driver.plug_vifs = mock.Mock()

        incus_driver.reboot(ctx, instance, [], 'HARD', {
            'block_device_mapping': [],
        })

        self.assertNotIn(driver.MIGRATION_RECOVERY_KEY, profile.config)
        profile.save.assert_called_once_with(wait=True)

    def test_recover_migration_target_preserves_stopped_state(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        container = self.client.instances.get.return_value
        container.status = 'Stopped'
        profile = self.client.profiles.get.return_value
        profile.config = {driver.MIGRATION_RECOVERY_KEY: 'stopped'}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._reconcile_reboot_data_volumes = mock.Mock()
        incus_driver._validate_reboot_vifs = mock.Mock()
        incus_driver.plug_vifs = mock.Mock()

        should_run = incus_driver.recover_migration_target(
            ctx, instance, [], {'block_device_mapping': []})

        self.assertFalse(should_run)
        container.start.assert_not_called()
        container.stop.assert_not_called()
        self.assertNotIn(driver.MIGRATION_RECOVERY_KEY, profile.config)

    def test_recover_migration_target_refreshes_vif_before_restart(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        vif = mock.Mock()
        container = self.client.instances.get.return_value
        container.status = 'Running'
        profile = self.client.profiles.get.return_value
        profile.config = {driver.MIGRATION_RECOVERY_KEY: 'running'}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._reconcile_reboot_data_volumes = mock.Mock()
        incus_driver._validate_reboot_vifs = mock.Mock()
        parent = mock.Mock()
        parent.attach_mock(container.stop, 'stop')
        parent.attach_mock(self.vif_driver.unplug, 'unplug')
        parent.attach_mock(self.vif_driver.plug, 'plug')
        parent.attach_mock(container.start, 'start')

        should_run = incus_driver.recover_migration_target(
            ctx, instance, [vif], {'block_device_mapping': []})

        self.assertTrue(should_run)
        self.assertEqual(
            [mock.call.stop(wait=True),
             mock.call.unplug(instance, vif),
             mock.call.plug(instance, vif),
             mock.call.start(wait=True)],
            parent.mock_calls)

    def test_recover_migration_target_stops_networkless_owner(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        container = self.client.instances.get.return_value
        container.status = 'Running'
        profile = self.client.profiles.get.return_value
        profile.config = {driver.MIGRATION_RECOVERY_KEY: 'stopped'}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._reconcile_reboot_data_volumes = mock.Mock()
        incus_driver._validate_reboot_vifs = mock.Mock()

        should_run = incus_driver.recover_migration_target(
            ctx, instance, [], {'block_device_mapping': []})

        self.assertFalse(should_run)
        container.stop.assert_called_once_with(wait=True)
        container.start.assert_not_called()

    @mock.patch('nova.virt.driver.block_device_info_get_mapping')
    def test_reboot_rejects_inconsistent_data_volume_before_start(
            self, get_mapping):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        connection_info = {
            'serial': 'volume-data',
            'driver_volume_type': 'rbd',
            'data': {},
        }
        get_mapping.return_value = [{
            'boot_index': 1,
            'connection_info': connection_info,
            'mount_device': '/dev/vdb',
        }]
        profile = self.client.profiles.get.return_value
        profile.devices = {
            'volume-data': {'type': 'unix-block', 'path': '/dev/vdc'}}
        profile.config = {
            driver._volume_device_info_key('volume-data'): '{}'}
        container = self.client.instances.get.return_value
        container.status = 'Stopped'
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.InvalidVolume,
            incus_driver.reboot,
            ctx, instance, None, 'HARD', block_device_info={})

        container.start.assert_not_called()
        container.restart.assert_not_called()

    def test_reboot_repairs_host_vif_before_starting_retained_target(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        device_name = driver.incus_vif.get_vif_devname(_VIF)
        profile = self.client.profiles.get.return_value
        profile.devices = {
            device_name: {
                'type': 'nic',
                'nictype': 'physical',
                'parent': driver.incus_vif.get_vif_internal_devname(_VIF),
                'hwaddr': _VIF['address'],
            },
        }
        profile.config = {}
        container = self.client.instances.get.return_value
        container.status = 'Stopped'
        container.devices = {}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.reboot(ctx, instance, [_VIF], 'HARD')

        self.vif_driver.plug.assert_called_once_with(instance, _VIF)
        container.start.assert_called_once_with(wait=True)

    def test_reboot_rejects_stale_vif_profile_before_start(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        profile = self.client.profiles.get.return_value
        profile.devices = {}
        profile.config = {}
        container = self.client.instances.get.return_value
        container.status = 'Stopped'
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.InterfaceAttachFailed,
            incus_driver.reboot,
            ctx, instance, [_VIF], 'HARD')

        self.vif_driver.plug.assert_not_called()
        container.start.assert_not_called()

    def test_reboot_accepts_instance_local_vif(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        device_name = driver.incus_vif.get_vif_devname(_VIF)
        profile = self.client.profiles.get.return_value
        profile.devices = {}
        profile.config = {}
        container = self.client.instances.get.return_value
        container.status = 'Stopped'
        container.devices = {
            device_name: {
                'type': 'nic',
                'nictype': 'physical',
                'parent': driver.incus_vif.get_vif_internal_devname(_VIF),
                'hwaddr': _VIF['address'],
            },
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.reboot(ctx, instance, [_VIF], 'HARD')

        self.vif_driver.plug.assert_called_once_with(instance, _VIF)
        container.start.assert_called_once_with(wait=True)

    def test_plug_vifs_rolls_back_partial_wiring(self):
        instance = mock.sentinel.instance
        vifs = [dict(_VIF), dict(_VIF, id='second')]
        self.vif_driver.plug.side_effect = [None, RuntimeError('failed')]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            RuntimeError, incus_driver.plug_vifs, instance, vifs)

        self.vif_driver.unplug.assert_called_once_with(instance, vifs[0])

    def test_get_host_ip_addr(self):
        incus_driver = driver.IncusDriver(None)

        result = incus_driver.get_host_ip_addr()

        self.assertEqual('0.0.0.0', result)

    @mock.patch('nova.virt.incus.driver._host_has_swap', return_value=False)
    def test_update_provider_tree(self, _host_has_swap):
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client
        incus_driver.get_available_resource = mock.Mock(return_value={
            'vcpus': 8,
            'memory_mb': 16384,
            'local_gb': 100,
        })
        incus_driver._get_allocation_ratios = mock.Mock(return_value={
            'VCPU': 4.0,
            'MEMORY_MB': 1.5,
            'DISK_GB': 1.0,
        })
        incus_driver._get_reserved_host_disk_gb_from_config = mock.Mock(
            return_value=2)
        current = mock.Mock(
            inventory={}, traits={'CUSTOM_OPERATOR_MANAGED'})
        provider_tree = mock.Mock()
        provider_tree.data.return_value = current
        self.CONF.reserved_host_cpus = 1
        self.CONF.reserved_host_memory_mb = 512

        incus_driver.update_provider_tree(provider_tree, 'compute-1')

        provider_tree.update_inventory.assert_called_once_with(
            'compute-1', {
                'VCPU': {
                    'total': 8, 'min_unit': 1, 'max_unit': 8,
                    'step_size': 1, 'allocation_ratio': 4.0,
                    'reserved': 1,
                },
                'MEMORY_MB': {
                    'total': 16384, 'min_unit': 1, 'max_unit': 16384,
                    'step_size': 1, 'allocation_ratio': 1.5,
                    'reserved': 512,
                },
                'DISK_GB': {
                    'total': 100, 'min_unit': 1, 'max_unit': 100,
                    'step_size': 1, 'allocation_ratio': 1.0,
                    'reserved': 2,
                },
            })
        provider_tree.update_traits.assert_called_once_with(
            'compute-1', {
                'CUSTOM_INCUS_SYSTEM_CONTAINER',
                'CUSTOM_OPERATOR_MANAGED',
            })

    @mock.patch('nova.virt.incus.driver._host_has_swap', return_value=True)
    def test_update_provider_tree_reports_swap_trait(self, _host_has_swap):
        incus_driver = driver.IncusDriver(None)
        incus_driver.get_available_resource = mock.Mock(return_value={
            'vcpus': 8, 'memory_mb': 16384, 'local_gb': 100})
        incus_driver._get_allocation_ratios = mock.Mock(return_value={
            'VCPU': 4.0, 'MEMORY_MB': 1.5, 'DISK_GB': 1.0})
        incus_driver._get_reserved_host_disk_gb_from_config = mock.Mock(
            return_value=2)
        provider_tree = mock.Mock()
        provider_tree.data.return_value = mock.Mock(
            inventory={}, traits=set())
        self.CONF.reserved_host_cpus = 0
        self.CONF.reserved_host_memory_mb = 0
        self.CONF.incus.allow_instance_swap = True

        incus_driver.update_provider_tree(provider_tree, 'compute-1')

        provider_tree.update_traits.assert_called_once_with(
            'compute-1', {
                'CUSTOM_INCUS_SWAP',
                'CUSTOM_INCUS_SYSTEM_CONTAINER',
            })

    @mock.patch('nova.virt.incus.driver._host_has_swap', return_value=False)
    def test_update_provider_tree_reports_root_pool_traits(
            self, _host_has_swap):
        incus_driver = driver.IncusDriver(None)
        incus_driver.get_available_resource = mock.Mock(return_value={
            'vcpus': 8, 'memory_mb': 16384, 'local_gb': 100})
        incus_driver._get_allocation_ratios = mock.Mock(return_value={
            'VCPU': 4.0, 'MEMORY_MB': 1.5, 'DISK_GB': 1.0})
        incus_driver._get_reserved_host_disk_gb_from_config = mock.Mock(
            return_value=2)
        provider_tree = mock.Mock()
        provider_tree.data.return_value = mock.Mock(
            inventory={}, traits={
                'CUSTOM_INCUS_STORAGE_POOL_REMOVED',
                'CUSTOM_OPERATOR_MANAGED',
            })
        self.CONF.incus.root_storage_pools = {
            'local-nvme': 'local-zfs',
            'durable': 'ceph-rootfs',
        }
        self.CONF.reserved_host_cpus = 0
        self.CONF.reserved_host_memory_mb = 0

        incus_driver.update_provider_tree(provider_tree, 'compute-1')

        provider_tree.update_traits.assert_called_once_with(
            'compute-1', {
                'CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME',
                'CUSTOM_INCUS_STORAGE_POOL_DURABLE',
                'CUSTOM_INCUS_SYSTEM_CONTAINER',
                'CUSTOM_OPERATOR_MANAGED',
            })

    def test_root_storage_pool_traits_reject_collisions(self):
        self.CONF.incus.root_storage_pools = {
            'local-nvme': 'one',
            'local_nvme': 'two',
        }

        self.assertRaises(
            exception.InvalidConfiguration,
            driver._root_storage_pool_traits)

    @mock.patch('nova.virt.incus.driver._get_storage_pool_info')
    @mock.patch('nova.virt.incus.driver._host_has_swap', return_value=False)
    def test_update_provider_tree_reports_local_pool_capacity(
            self, _host_has_swap, get_pool_info):
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client
        incus_driver.get_available_resource = mock.Mock(return_value={
            'vcpus': 8, 'memory_mb': 16384, 'local_gb': 100})
        incus_driver._get_allocation_ratios = mock.Mock(return_value={
            'VCPU': 4.0, 'MEMORY_MB': 1.5, 'DISK_GB': 1.0})
        incus_driver._get_reserved_host_disk_gb_from_config = mock.Mock(
            return_value=2)
        get_pool_info.return_value = {
            'total': 80 * units.Gi,
            'used': 10 * units.Gi,
        }
        provider_tree = mock.Mock()
        provider_tree.data.return_value = mock.Mock(
            inventory={}, traits=set())
        self.CONF.incus.root_storage_pools = {
            'local-nvme': 'local-zfs',
        }
        self.CONF.incus.root_storage_pool_resource_classes = {
            'local-nvme': 'CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME_DISK_GB',
        }
        self.CONF.reserved_host_cpus = 0
        self.CONF.reserved_host_memory_mb = 0

        incus_driver.update_provider_tree(provider_tree, 'compute-1')

        inventory = provider_tree.update_inventory.call_args.args[1]
        self.assertEqual({
            'total': 80,
            'min_unit': 1,
            'max_unit': 80,
            'step_size': 1,
            'allocation_ratio': 1.0,
            'reserved': 0,
        }, inventory['CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME_DISK_GB'])
        get_pool_info.assert_called_once_with(self.client, 'local-zfs')

    @mock.patch('nova.virt.incus.driver._host_has_swap', return_value=False)
    def test_update_provider_tree_reports_manila_live_migration_trait(
            self, _host_has_swap):
        incus_driver = driver.IncusDriver(None)
        incus_driver.get_available_resource = mock.Mock(return_value={
            'vcpus': 8, 'memory_mb': 16384, 'local_gb': 100})
        incus_driver._get_allocation_ratios = mock.Mock(return_value={
            'VCPU': 4.0, 'MEMORY_MB': 1.5, 'DISK_GB': 1.0})
        incus_driver._get_reserved_host_disk_gb_from_config = mock.Mock(
            return_value=2)
        provider_tree = mock.Mock()
        provider_tree.data.return_value = mock.Mock(
            inventory={}, traits=set())
        self.CONF.incus.enable_manila_shares = True
        self.CONF.incus.allow_live_migration = True

        incus_driver.update_provider_tree(provider_tree, 'compute-1')

        provider_tree.update_traits.assert_called_once_with(
            'compute-1', {
                'CUSTOM_INCUS_MANILA_LIVE_MIGRATION',
                'CUSTOM_INCUS_MANILA_SHARE',
                'CUSTOM_INCUS_SYSTEM_CONTAINER',
            })

    def test_attach_interface(self):
        expected = {
            'hwaddr': '00:11:22:33:44:55',
            'parent': 'tin0123456789a',
            'nictype': 'physical',
            'type': 'nic',
        }

        container = mock.Mock()
        container.devices = {}
        self.client.instances.get.return_value = container

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        image_meta = None
        vif = {
            'id': '0123456789abcdef',
            'type': network_model.VIF_TYPE_OVS,
            'address': '00:11:22:33:44:55',
            'network': {
                'bridge': 'fakebr'},
            'devname': 'tap0123456789a'}

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.firewall_driver = mock.Mock()

        incus_driver.attach_interface(ctx, instance, image_meta, vif)

        self.assertEqual(expected, container.devices['tap0123456789a'])
        container.save.assert_called_once_with(wait=True)
        self.client.profiles.get.assert_not_called()

    def test_detach_interface_legacy(self):
        container = mock.Mock()
        container.devices = {}
        self.client.instances.get.return_value = container
        profile = mock.Mock()
        profile.devices = {
            'eth0': {
                'nictype': 'bridged',
                'parent': 'incusbr0',
                'hwaddr': '00:11:22:33:44:55',
                'type': 'nic'
            },
            'root': {
                'path': '/',
                'type': 'disk'
            },
        }
        self.client.profiles.get.return_value = profile

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        vif = {
            'id': '0123456789abcdef',
            'type': network_model.VIF_TYPE_OVS,
            'address': '00:11:22:33:44:55',
            'network': {
                'bridge': 'fakebr'}}

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.detach_interface(ctx, instance, vif)

        self.vif_driver.unplug.assert_called_once_with(
            instance, vif)
        self.assertEqual(['root'], sorted(profile.devices.keys()))
        profile.save.assert_called_once_with(wait=True)
        self.assertEqual({}, container.devices)
        self.assertEqual(2, container.save.call_count)

    def test_detach_interface(self):
        container = mock.Mock()
        container.devices = {}
        self.client.instances.get.return_value = container
        profile = mock.Mock()
        profile.devices = {
            'tap0123456789a': {
                'nictype': 'physical',
                'parent': 'tin0123456789a',
                'hwaddr': '00:11:22:33:44:55',
                'type': 'nic'
            },
            'root': {
                'path': '/',
                'type': 'disk'
            },
        }
        self.client.profiles.get.return_value = profile

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        vif = {
            'id': '0123456789abcdef',
            'type': network_model.VIF_TYPE_OVS,
            'address': '00:11:22:33:44:55',
            'network': {
                'bridge': 'fakebr'}}

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.detach_interface(ctx, instance, vif)

        self.vif_driver.unplug.assert_called_once_with(
            instance, vif)
        self.assertEqual(['root'], sorted(profile.devices.keys()))
        profile.save.assert_called_once_with(wait=True)
        self.assertEqual({}, container.devices)
        self.assertEqual(2, container.save.call_count)

    def test_detach_interface_local_device_retries_busy_update(self):
        container = mock.Mock()
        container.devices = {
            'tap0123456789a': {
                'nictype': 'physical',
                'parent': 'tin0123456789a',
                'hwaddr': '00:11:22:33:44:55',
                'type': 'nic',
            },
        }
        container.save.side_effect = [
            incus_operation_exception(
                400,
                'Instance is busy running a "stop" operation'),
            None,
        ]
        self.client.instances.get.return_value = container
        profile = mock.Mock()
        profile.devices = {}
        self.client.profiles.get.return_value = profile

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        vif = {
            'id': '0123456789abcdef',
            'type': network_model.VIF_TYPE_OVS,
            'address': '00:11:22:33:44:55',
            'network': {'bridge': 'fakebr'},
        }

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.detach_interface(ctx, instance, vif)

        self.assertEqual({}, container.devices)
        self.assertEqual(2, container.save.call_count)
        profile.save.assert_not_called()
        self.vif_driver.unplug.assert_called_once_with(instance, vif)

    def test_detach_interface_removes_local_and_profile_device(self):
        device = {
            'nictype': 'physical',
            'parent': 'tin0123456789a',
            'hwaddr': '00:11:22:33:44:55',
            'type': 'nic',
        }
        container = mock.Mock()
        container.devices = {'tap0123456789a': dict(device)}
        self.client.instances.get.return_value = container
        profile = mock.Mock()
        profile.devices = {'tap0123456789a': dict(device)}
        self.client.profiles.get.return_value = profile

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        vif = {
            'id': '0123456789abcdef',
            'type': network_model.VIF_TYPE_OVS,
            'address': '00:11:22:33:44:55',
            'network': {'bridge': 'fakebr'},
        }

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.detach_interface(ctx, instance, vif)

        self.assertEqual({}, container.devices)
        self.assertEqual({}, profile.devices)
        self.assertEqual(2, container.save.call_count)
        profile.save.assert_called_once_with(wait=True)
        self.vif_driver.unplug.assert_called_once_with(instance, vif)

    def test_detach_interface_masks_profile_before_different_local_name(self):
        container = mock.Mock()
        container.devices = {
            'tap0123456789a': {
                'nictype': 'physical',
                'parent': 'tin0123456789a',
                'hwaddr': '00:11:22:33:44:55',
                'type': 'nic',
            },
        }
        saved_devices = []
        container.save.side_effect = lambda wait: saved_devices.append(
            dict(container.devices))
        self.client.instances.get.return_value = container
        profile = mock.Mock()
        profile.devices = {
            'eth0': {
                'nictype': 'physical',
                'parent': 'tin0123456789a',
                'hwaddr': '00:11:22:33:44:55',
                'type': 'nic',
            },
        }
        self.client.profiles.get.return_value = profile

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        vif = {
            'id': '0123456789abcdef',
            'type': network_model.VIF_TYPE_OVS,
            'address': '00:11:22:33:44:55',
            'network': {'bridge': 'fakebr'},
        }

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.detach_interface(ctx, instance, vif)

        self.assertEqual(
            {
                'tap0123456789a': mock.ANY,
                'eth0': {'type': 'none'},
            },
            saved_devices[0])
        self.assertEqual({'eth0': {'type': 'none'}}, saved_devices[1])
        self.assertEqual({}, saved_devices[2])
        self.assertEqual({}, container.devices)
        self.assertEqual({}, profile.devices)
        self.vif_driver.unplug.assert_called_once_with(instance, vif)

    def test_detach_interface_accepts_persisted_profile_change(self):
        container = mock.Mock()
        container.devices = {}
        self.client.instances.get.return_value = container
        profile = mock.Mock()
        profile.devices = {
            'tap0123456789a': {
                'nictype': 'physical',
                'parent': 'tin0123456789a',
                'hwaddr': '00:11:22:33:44:55',
                'type': 'nic',
            },
        }
        profile.save.side_effect = incus_operation_exception(
            400,
            'The following instances failed to update '
            '(profile change still saved)')
        saved_profile = mock.Mock()
        saved_profile.devices = {}
        self.client.profiles.get.side_effect = [profile, saved_profile]

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        vif = {
            'id': '0123456789abcdef',
            'type': network_model.VIF_TYPE_OVS,
            'address': '00:11:22:33:44:55',
            'network': {'bridge': 'fakebr'},
        }

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.detach_interface(ctx, instance, vif)

        self.assertEqual({}, container.devices)
        self.assertEqual(2, container.save.call_count)
        self.vif_driver.unplug.assert_called_once_with(instance, vif)

    def test_detach_interface_during_delete_only_unplugs_vif(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0,
            task_state=task_states.DELETING)
        vif = {
            'id': '0123456789abcdef',
            'type': network_model.VIF_TYPE_OVS,
            'address': '00:11:22:33:44:55',
            'network': {'bridge': 'fakebr'},
        }

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.detach_interface(ctx, instance, vif)

        self.client.instances.get.assert_not_called()
        self.client.profiles.get.assert_not_called()
        self.vif_driver.unplug.assert_called_once_with(instance, vif)

    def test_detach_interface_not_found(self):
        self.client.instances.get.side_effect = incuscore_exceptions.NotFound(
            "404")

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        vif = {
            'id': '0123456789abcdef',
            'type': network_model.VIF_TYPE_OVS,
            'address': '00:11:22:33:44:55',
            'network': {
                'bridge': 'fakebr'}}

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.detach_interface(ctx, instance, vif)

        self.vif_driver.unplug.assert_called_once_with(
            instance, vif)

    def test_migrate_disk_and_power_off(self):
        container = mock.Mock()
        self.client.instances.get.return_value = container
        profile = mock.Mock()
        self.client.profiles.get.return_value = profile

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        dest = '0.0.0.0'
        target_flavor = instance.flavor
        network_info = []

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.MigrationError,
            incus_driver.migrate_disk_and_power_off,
            ctx, instance, dest, target_flavor, network_info)

        profile.save.assert_not_called()
        container.stop.assert_not_called()

    def test_migrate_disk_and_power_off_different_host(self):
        container = mock.Mock()
        container.status = 'Running'
        container.generate_migration_data.return_value = {
            'name': 'test',
            'source': {
                'type': 'migration',
                'operation': 'http+unix://incus/1.0/operations/op-id',
                'secrets': {'0': 'secret'},
            },
        }
        self.client.instances.get.return_value = container
        self.CONF.incus.allow_cold_migration = True
        self.CONF.incus.migration_address = 'https://10.224.0.16:8443'

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        dest = '0.0.0.1'
        flavor = instance.flavor
        network_info = []

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        result = jsonutils.loads(incus_driver.migrate_disk_and_power_off(
            ctx, instance, dest, flavor, network_info))

        self.assertEqual('incus-pull-v1', result['format'])
        self.assertFalse(result['boot_from_volume'])
        self.assertTrue(result['was_running'])
        self.assertEqual(
            'https://10.224.0.16:8443/1.0/operations/op-id',
            result['migration_data']['source']['operation'])
        self.assertEqual(
            [instance.name], result['migration_data']['profiles'])
        container.stop.assert_called_once_with(wait=True)
        container.generate_migration_data.assert_called_once_with(live=False)

    def test_bfv_migration_requires_shared_ceph_extension(self):
        volume_id = '8231d2e8-1111-4222-8333-123456789abc'
        root_bdm = {
            'connection_info': {
                'driver_volume_type': 'rbd',
                'serial': volume_id,
                'data': {
                    'name': 'cinder-volumes/volume-%s' % volume_id,
                    'access_mode': 'rw',
                },
            },
        }
        self.client.host_info['api_extensions'].append(
            'storage_driver_cephext')

        self.assertRaises(
            exception.MigrationError,
            driver._require_bfv_migration_support,
            self.client, root_bdm)

        self.client.storage_pools.get.assert_not_called()

    def test_bfv_migration_validates_cephext_pool(self):
        volume_id = '8231d2e8-1111-4222-8333-123456789abc'
        root_bdm = {
            'connection_info': {
                'driver_volume_type': 'rbd',
                'serial': volume_id,
                'data': {
                    'name': 'cinder-volumes/volume-%s' % volume_id,
                    'access_mode': 'rw',
                },
            },
        }
        self.client.host_info['api_extensions'].extend([
            'migration_shared_ceph_storage',
            'storage_driver_cephext',
        ])
        self.CONF.incus.boot_from_volume_storage_pools = {
            'cinder-volumes': 'cinder'}
        pool = self.client.storage_pools.get.return_value
        pool.driver = 'cephext'
        pool.config = {'source': 'cinder-volumes'}

        self.assertEqual(
            ('cinder-volumes', 'volume-%s' % volume_id),
            driver._require_bfv_migration_support(self.client, root_bdm))

    def test_migrate_disk_failure_restarts_source(self):
        container = mock.Mock(status='Running')
        container.generate_migration_data.side_effect = RuntimeError(
            'migration operation failed')
        self.client.instances.get.return_value = container
        self.CONF.incus.allow_cold_migration = True
        self.CONF.incus.migration_address = 'https://10.224.0.16:8443'
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            RuntimeError,
            incus_driver.migrate_disk_and_power_off,
            ctx, instance, '10.224.0.17', instance.flavor, [])

        container.stop.assert_called_once_with(wait=True)
        container.start.assert_called_once_with(wait=True)

    @mock.patch.object(driver, '_preflight_bfv_migration_destination')
    @mock.patch.object(driver, '_require_bfv_migration_support')
    @mock.patch.object(driver, '_boot_from_volume')
    @mock.patch('nova.virt.driver.block_device_info_get_mapping')
    def test_migrate_disk_detaches_only_data_volumes(
            self, get_mapping, boot_from_volume, require_bfv, preflight):
        container = mock.Mock(status='Running')
        container.generate_migration_data.return_value = {
            'source': {
                'operation': 'http+unix://incus/1.0/operations/op-id',
            },
        }
        self.client.instances.get.return_value = container
        self.CONF.incus.allow_cold_migration = True
        self.CONF.incus.migration_address = 'https://10.224.0.16:8443'
        connection_info = {'driver_volume_type': 'local', 'data': {
            'volume_id': 'volume-id'}}
        root_bdm = {
            'boot_index': 0,
            'connection_info': mock.sentinel.root_connection,
            'mount_device': '/dev/sda',
        }
        boot_from_volume.return_value = root_bdm
        require_bfv.return_value = ('cinder-volumes', 'volume-root')
        get_mapping.return_value = [root_bdm, {
            'connection_info': connection_info,
            'mount_device': '/dev/vdb',
        }]
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        instance.flavor.root_gb = 20
        smaller_flavor = mock.Mock(root_gb=10)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.detach_volume = mock.Mock()

        incus_driver.migrate_disk_and_power_off(
            ctx, instance, '10.224.0.17',
            smaller_flavor, [], block_device_info={})

        incus_driver.detach_volume.assert_called_once_with(
            ctx, connection_info, instance, '/dev/vdb')
        require_bfv.assert_called_once_with(self.client, root_bdm)
        preflight.assert_called_once_with('10.224.0.17', 'cinder-volumes')

    @mock.patch.object(driver, '_preflight_bfv_migration_destination')
    @mock.patch.object(driver, '_require_bfv_migration_support')
    @mock.patch.object(driver, '_boot_from_volume')
    def test_migrate_bfv_unreachable_destination_does_not_stop_source(
            self, boot_from_volume, require_bfv, preflight):
        root_bdm = {'boot_index': 0}
        boot_from_volume.return_value = root_bdm
        require_bfv.return_value = ('cinder-volumes', 'volume-root')
        preflight.side_effect = exception.MigrationError(
            reason='destination is unreachable')
        self.CONF.incus.allow_cold_migration = True
        self.CONF.incus.migration_address = 'https://10.224.0.16:8443'
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        error = self.assertRaises(
            exception.InstanceFaultRollback,
            incus_driver.migrate_disk_and_power_off,
            ctx, instance, '10.224.0.17', instance.flavor, [],
            block_device_info={})

        self.assertIsInstance(error.inner_exception, exception.MigrationError)
        require_bfv.assert_called_once_with(self.client, root_bdm)
        preflight.assert_called_once_with('10.224.0.17', 'cinder-volumes')
        self.client.instances.get.assert_not_called()

    @mock.patch.object(driver.incus_client,
                       'get_migration_preflight_client')
    @mock.patch.object(driver.socket, 'create_connection')
    def test_bfv_destination_readiness_preflight(self, connect, get_remote):
        self.CONF.incus.boot_from_volume_storage_pools = {
            'cinder-volumes': 'cinder-bfv'}
        self.CONF.incus.migration_port = 8443
        self.CONF.incus.migration_preflight_timeout = 5
        self.CONF.incus.migration_preflight_project = 'nova-preflight'
        self.CONF.incus.migration_preflight_server_names = {
            '10.224.0.17': 'compute-2.example.test'}
        self.CONF.incus.migration_preflight_tls_ca = '/etc/nova/default.crt'
        self.CONF.incus.migration_preflight_tls_ca_by_server = {
            '10.224.0.17': '/etc/nova/compute-2.crt'}
        remote = get_remote.return_value
        remote.host_info = {'api_extensions': [
            'migration_shared_ceph_storage',
            'migration_live_shared_cephext_storage',
            'storage_driver_cephext']}
        remote.projects.get.return_value.config = {
            'user.openstack.preflight_protocol': '1',
            'user.openstack.bfv_storage_pools': (
                '{"cinder-volumes":"cinder-bfv",'
                '"nvme-volumes":"nvme-bfv"}'),
        }
        remote.storage_pools.get.return_value.driver = 'cephext'

        driver._preflight_bfv_migration_destination(
            '10.224.0.17', 'cinder-volumes')

        connect.assert_called_once_with(
            ('10.224.0.17', 8443), timeout=5)
        get_remote.assert_called_once_with(
            'https://compute-2.example.test:8443',
            verify='/etc/nova/compute-2.crt')
        remote.projects.get.assert_called_once_with('nova-preflight')
        remote.storage_pools.get.assert_called_once_with('cinder-bfv')

    @mock.patch.object(driver.incus_client,
                       'get_migration_preflight_client')
    @mock.patch.object(driver.socket, 'create_connection')
    def test_bfv_destination_preflight_rejects_missing_extension(
            self, connect, get_remote):
        get_remote.return_value.host_info = {'api_extensions': [
            'storage_driver_cephext']}

        self.assertRaisesRegex(
            exception.MigrationError,
            'missing API extensions: migration_shared_ceph_storage',
            driver._preflight_bfv_migration_destination,
            'compute-2.example.test', 'cinder-volumes')

    @mock.patch.object(driver.incus_client,
                       'get_migration_preflight_client')
    @mock.patch.object(driver.socket, 'create_connection')
    def test_bfv_destination_preflight_rejects_cinder_pool_mismatch(
            self, connect, get_remote):
        self.CONF.incus.boot_from_volume_storage_pools = {
            'cinder-volumes': 'cinder-bfv'}
        remote = get_remote.return_value
        remote.host_info = {'api_extensions': [
            'migration_shared_ceph_storage',
            'migration_live_shared_cephext_storage',
            'storage_driver_cephext']}
        remote.projects.get.return_value.config = {
            'user.openstack.preflight_protocol': '1',
            'user.openstack.bfv_pool': 'cinder-bfv',
            'user.openstack.cinder_rbd_pool': 'wrong-pool',
        }
        remote.storage_pools.get.return_value.driver = 'cephext'

        self.assertRaisesRegex(
            exception.MigrationError,
            'destination readiness metadata does not advertise',
            driver._preflight_bfv_migration_destination,
            'compute-2.example.test', 'cinder-volumes')

    @mock.patch('nova.virt.driver.block_device_info_get_mapping')
    def test_migrate_disk_volume_failure_restores_source(self, get_mapping):
        container = mock.Mock(status='Running')
        container.generate_migration_data.return_value = {
            'source': {
                'operation': 'http+unix://incus/1.0/operations/op-id',
            },
        }
        self.client.instances.get.return_value = container
        self.CONF.incus.allow_cold_migration = True
        self.CONF.incus.migration_address = 'https://10.224.0.16:8443'
        first = {'driver_volume_type': 'local', 'data': {
            'volume_id': 'first'}}
        second = {'driver_volume_type': 'local', 'data': {
            'volume_id': 'second'}}
        get_mapping.return_value = [
            {'connection_info': first, 'mount_device': '/dev/vdb'},
            {'connection_info': second, 'mount_device': '/dev/vdc'},
        ]
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.detach_volume = mock.Mock(
            side_effect=[None, RuntimeError('disconnect failed')])
        incus_driver.attach_volume = mock.Mock()

        self.assertRaises(
            RuntimeError, incus_driver.migrate_disk_and_power_off,
            ctx, instance, '10.224.0.17',
            instance.flavor, [], block_device_info={})

        incus_driver.attach_volume.assert_called_once_with(
            ctx, first, instance, '/dev/vdb')
        container.start.assert_called_once_with(wait=True)

    def test_migrate_disk_rejects_non_https_address_before_shutdown(self):
        self.CONF.incus.allow_cold_migration = True
        self.CONF.incus.migration_address = 'http://10.224.0.16:8443'
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.InvalidConfiguration,
            incus_driver.migrate_disk_and_power_off,
            ctx, instance, '10.224.0.17', instance.flavor, [])

        self.client.instances.get.assert_not_called()

    def test_migrate_disk_rejects_rootfs_shrink_before_shutdown(self):
        self.CONF.incus.allow_cold_migration = True
        self.CONF.incus.migration_address = 'https://10.224.0.16:8443'
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        instance.flavor.root_gb = 20
        smaller_flavor = mock.Mock(root_gb=10)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.InstanceFaultRollback,
            incus_driver.migrate_disk_and_power_off,
            ctx, instance, '10.224.0.17', smaller_flavor, [])

        self.client.instances.get.assert_not_called()

    @mock.patch('os.path.realpath')
    def test_attach_volume(self, realpath):
        profile = mock.Mock()
        profile.devices = {}
        profile.config = {}
        self.client.profiles.get.return_value = profile
        realpath.return_value = '/dev/sdc'
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260', 'iqn.2010-10.org.openstack:volume-00000001',
            auth=True)
        connection_info['data']['qos_specs'] = {
            'read_iops_sec': '500',
            'write_bytes_sec': '1048576',
        }
        self.client.host_info['api_extensions'].append('unix_block_limits')
        mountpoint = '/dev/sdd'

        volume_connector = mock.Mock()
        volume_connector.connect_volume.return_value = {'path': '/dev/disk/x'}
        driver.brick_get_connector = mock.Mock(return_value=volume_connector)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.attach_volume(
            ctx, connection_info, instance, mountpoint, None, None, None)

        incus_driver.client.profiles.get.assert_called_once_with(instance.name)
        volume_connector.connect_volume.assert_called_once_with(
            connection_info['data'])
        self.assertEqual({
            '1': {
                'path': '/dev/sdd',
                'required': 'true',
                'source': '/dev/sdc',
                'type': 'unix-block',
                'limits.read': '500iops',
                'limits.write': '1048576B',
            },
        }, profile.devices)
        self.assertEqual(
            {'path': '/dev/disk/x'},
            jsonutils.loads(profile.config['user.openstack.volume.1']))
        profile.save.assert_called_once_with(wait=True)

    def test_attach_volume_requires_guest_fuse_helper_before_connect(self):
        profile = mock.Mock(devices={}, config={})
        self.client.profiles.get.return_value = profile
        container = mock.Mock(status='Running')
        container.execute.return_value.exit_code = 1
        self.client.instances.get.return_value = container
        volume_connector = mock.Mock()
        driver.brick_get_connector = mock.Mock(return_value=volume_connector)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaisesRegex(
            exception.InvalidVolume, 'must provide fuse2fs',
            incus_driver.attach_volume,
            context.get_admin_context(), connection_info, instance,
            '/dev/sdd')

        container.execute.assert_called_once_with(['which', 'fuse2fs'])
        volume_connector.connect_volume.assert_not_called()

    @mock.patch('os.path.realpath', return_value='/dev/sdc')
    def test_attach_volume_rolls_back_host_connection(self, realpath):
        profile = mock.Mock()
        profile.devices = {}
        profile.config = {}
        profile.save.side_effect = RuntimeError('Incus API failed')
        self.client.profiles.get.return_value = profile
        volume_connector = mock.Mock()
        device_info = {'path': '/dev/sdc'}
        volume_connector.connect_volume.return_value = device_info
        driver.brick_get_connector = mock.Mock(return_value=volume_connector)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            RuntimeError, incus_driver.attach_volume,
            context.get_admin_context(), connection_info, instance,
            '/dev/sdd')

        volume_connector.disconnect_volume.assert_called_once_with(
            connection_info['data'], device_info)

    def test_attach_encrypted_volume_is_rejected_before_connect(self):
        volume_connector = mock.Mock()
        driver.brick_get_connector = mock.Mock(return_value=volume_connector)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.VolumeEncryptionNotSupported,
            incus_driver.attach_volume,
            context.get_admin_context(), connection_info, instance,
            '/dev/sdd', encryption={'provider': 'luks'})

        volume_connector.connect_volume.assert_not_called()

    def test_attach_read_only_volume_is_rejected_before_connect(self):
        volume_connector = mock.Mock()
        driver.brick_get_connector = mock.Mock(return_value=volume_connector)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'}, '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        connection_info['data']['access_mode'] = 'ro'
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.InvalidVolume, incus_driver.attach_volume,
            context.get_admin_context(), connection_info, instance,
            '/dev/sdd')

        volume_connector.connect_volume.assert_not_called()

    def test_attach_volume_rejects_non_device_mountpoint_before_connect(self):
        volume_connector = mock.Mock()
        driver.brick_get_connector = mock.Mock(return_value=volume_connector)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15', 'iqn.2010-10.org.openstack:volume-00000001')
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.InvalidVolume, incus_driver.attach_volume,
            context.get_admin_context(), connection_info, instance,
            '/etc/tenant-volume')

        driver.brick_get_connector.assert_not_called()

    def test_attach_volume_rejects_special_device_before_connect(self):
        volume_connector = mock.Mock()
        driver.brick_get_connector = mock.Mock(return_value=volume_connector)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15', 'iqn.2010-10.org.openstack:volume-00000001')
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.InvalidVolume, incus_driver.attach_volume,
            context.get_admin_context(), connection_info, instance,
            '/dev/null')

        driver.brick_get_connector.assert_not_called()

    def test_attach_volume_rejects_qos_before_connect(self):
        driver.brick_get_connector = mock.Mock()
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15', 'iqn.2010-10.org.openstack:volume-00000001')
        connection_info['data']['qos_specs'] = {'read_iops_sec': '500'}
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.InvalidVolume, incus_driver.attach_volume,
            context.get_admin_context(), connection_info, instance,
            '/dev/vdb')

        self.client.profiles.get.assert_not_called()
        driver.brick_get_connector.assert_not_called()

    def test_data_volume_qos_maps_with_server_extension(self):
        self.assertEqual({
            'limits.read': '500iops',
            'limits.write': '1048576B',
        }, driver._data_volume_qos({
            'data': {'qos_specs': {
                'read_iops_sec': '500',
                'write_bytes_sec': '1048576',
            }},
        }, ['unix_block_limits']))

    def test_attach_volume_rejects_duplicate_mountpoint_before_connect(self):
        profile = mock.Mock()
        profile.config = {}
        profile.devices = {
            'existing-volume': {
                'path': '/dev/sdd',
                'source': '/dev/dm-0',
                'type': 'unix-block',
            },
        }
        self.client.profiles.get.return_value = profile
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15', 'iqn.2010-10.org.openstack:volume-00000001')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        with mock.patch(
                'nova.virt.incus.driver.brick_get_connector') as get_connector:
            self.assertRaises(
                exception.DevicePathInUse, incus_driver.attach_volume,
                context.get_admin_context(), connection_info,
                fake_instance.fake_instance_obj(
                    context.get_admin_context(), name='test'),
                '/dev/sdd')

        get_connector.assert_not_called()

    def test_attach_volume_rejects_duplicate_volume_before_connect(self):
        profile = mock.Mock()
        profile.config = {}
        profile.devices = {
            '1': {
                'path': '/dev/sdc',
                'source': '/dev/dm-0',
                'type': 'unix-block',
            },
        }
        self.client.profiles.get.return_value = profile
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15', 'iqn.2010-10.org.openstack:volume-00000001')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        with mock.patch(
                'nova.virt.incus.driver.brick_get_connector') as get_connector:
            self.assertRaises(
                exception.InvalidVolume, incus_driver.attach_volume,
                context.get_admin_context(), connection_info,
                fake_instance.fake_instance_obj(
                    context.get_admin_context(), name='test'),
                '/dev/sdd')

        get_connector.assert_not_called()

    @mock.patch('os.path.realpath', return_value='/var/lib/tenant-volume')
    def test_attach_volume_rejects_non_device_connector_path(self, realpath):
        profile = mock.Mock()
        profile.devices = {}
        profile.config = {}
        self.client.profiles.get.return_value = profile
        volume_connector = mock.Mock()
        device_info = {'path': '/var/lib/tenant-volume'}
        volume_connector.connect_volume.return_value = device_info
        driver.brick_get_connector = mock.Mock(return_value=volume_connector)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15', 'iqn.2010-10.org.openstack:volume-00000001')
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.InvalidVolume, incus_driver.attach_volume,
            context.get_admin_context(), connection_info, instance,
            '/dev/sdd')

        volume_connector.disconnect_volume.assert_called_once_with(
            connection_info['data'], device_info)

    def test_attach_volume_without_device_path_disconnects(self):
        profile = mock.Mock()
        profile.devices = {}
        profile.config = {}
        self.client.profiles.get.return_value = profile
        volume_connector = mock.Mock()
        device_info = {}
        volume_connector.connect_volume.return_value = device_info
        driver.brick_get_connector = mock.Mock(return_value=volume_connector)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.InvalidVolume, incus_driver.attach_volume,
            context.get_admin_context(), connection_info, instance,
            '/dev/sdd')

        volume_connector.disconnect_volume.assert_called_once_with(
            connection_info['data'], device_info)

    def test_detach_volume(self):
        profile = mock.Mock()
        profile.config = {}
        profile.devices = {
            'eth0': {
                'name': 'eth0',
                'nictype': 'bridged',
                'parent': 'incusbr0',
                'type': 'nic'
            },
            'root': {
                'path': '/',
                'type': 'disk'
            },
            '1': {
                'path': '/dev/sdc',
                'source': '/dev/drbd1000',
                'type': 'unix-block'
            },
        }

        expected = {
            'eth0': {
                'name': 'eth0',
                'nictype': 'bridged',
                'parent': 'incusbr0',
                'type': 'nic'
            },
            'root': {
                'path': '/',
                'type': 'disk'
            },
        }

        self.client.profiles.get.return_value = profile
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260', 'iqn.2010-10.org.openstack:volume-00000001',
            auth=True)
        mountpoint = mock.Mock()

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        volume_connector = mock.Mock()
        driver.brick_get_connector = mock.Mock(return_value=volume_connector)
        incus_driver.detach_volume(ctx, connection_info, instance,
                                   mountpoint, None)

        incus_driver.client.profiles.get.assert_called_once_with(instance.name)

        self.assertEqual(expected, profile.devices)
        profile.save.assert_called_once_with(wait=True)
        volume_connector.disconnect_volume.assert_called_once_with(
            connection_info['data'], {'path': '/dev/drbd1000'})

    def test_detach_volume_recovers_missing_connection_volume_id(self):
        volume_id = '8231d2e8-1111-4222-8333-123456789abc'
        metadata_key = driver._volume_device_info_key(volume_id)
        profile = mock.Mock()
        profile.config = {metadata_key: '{"path":"/dev/rbd7"}'}
        profile.devices = {
            'root': {'path': '/', 'type': 'disk'},
            volume_id: {
                'path': '/dev/sdc',
                'source': '/dev/rbd7',
                'type': 'unix-block',
            },
        }
        self.client.profiles.get.return_value = profile
        connector = mock.Mock()
        driver.brick_get_connector = mock.Mock(return_value=connector)
        connection_info = {
            'data': {'name': 'pool/volume-%s' % volume_id},
        }
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.detach_volume(
            context.get_admin_context(), connection_info, instance,
            '/dev/sdc')

        self.assertNotIn(volume_id, profile.devices)
        self.assertNotIn(metadata_key, profile.config)
        profile.save.assert_called_once_with(wait=True)
        driver.brick_get_connector.assert_called_once_with('rbd')
        connector.disconnect_volume.assert_called_once_with(
            connection_info['data'], {'path': '/dev/rbd7'})

    def test_detach_volume_recovers_legacy_profile_metadata(self):
        volume_id = '8231d2e8-1111-4222-8333-123456789abc'
        metadata_key = driver._legacy_volume_device_info_key(volume_id)
        profile = mock.Mock()
        profile.config = {metadata_key: '{"path":"/dev/rbd7"}'}
        profile.devices = {
            volume_id: {
                'path': '/dev/sdc',
                'source': '/dev/rbd7',
                'type': 'unix-block',
            },
        }
        self.client.profiles.get.return_value = profile
        connector = mock.Mock()
        driver.brick_get_connector = mock.Mock(return_value=connector)
        connection_info = {
            'driver_volume_type': 'rbd',
            'data': {'name': 'pool/volume-%s' % volume_id},
        }
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.detach_volume(
            context.get_admin_context(), connection_info, instance,
            '/dev/sdc')

        self.assertNotIn(volume_id, profile.devices)
        self.assertNotIn(metadata_key, profile.config)
        profile.save.assert_called_once_with(wait=True)
        connector.disconnect_volume.assert_called_once_with(
            connection_info['data'], {'path': '/dev/rbd7'})

    def test_detach_volume_missing_id_rejects_unmanaged_mountpoint(self):
        profile = mock.Mock()
        profile.config = {}
        profile.devices = {
            'foreign': {
                'path': '/dev/sdc',
                'source': '/dev/rbd7',
                'type': 'unix-block',
            },
        }
        self.client.profiles.get.return_value = profile
        connection_info = {
            'driver_volume_type': 'rbd',
            'data': {'name': 'pool/unknown'},
        }
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.InvalidVolume, incus_driver.detach_volume,
            context.get_admin_context(), connection_info, instance,
            '/dev/sdc')

        profile.save.assert_not_called()

    def test_detach_missing_profile_is_idempotent_for_bfv_root(self):
        volume_id = '8231d2e8-1111-4222-8333-123456789abc'
        connection_info = {
            'driver_volume_type': 'rbd',
            'serial': volume_id,
            'data': {
                'name': 'cinder-volumes/volume-%s' % volume_id,
            },
        }
        instance = mock.Mock(
            name='instance-00000001', root_device_name='/dev/sda')
        self.client.profiles.get.side_effect = incuscore_exceptions.NotFound(
            mock.Mock())
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        incus_driver.detach_volume(
            mock.sentinel.context, connection_info, instance, '/dev/sda')

        self.client.profiles.get.assert_called_once_with(instance.name)

    def test_detach_missing_profile_does_not_hide_data_volume(self):
        instance = mock.Mock(
            name='instance-00000001', root_device_name='/dev/sda')
        self.client.profiles.get.side_effect = incuscore_exceptions.NotFound(
            mock.Mock())
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertRaises(
            incuscore_exceptions.NotFound, incus_driver.detach_volume,
            mock.sentinel.context,
            {'driver_volume_type': 'rbd', 'data': {}},
            instance, '/dev/sdb')

    def test_detach_volume_restores_profile_on_disconnect_failure(self):
        device = {
            'path': '/dev/sdc',
            'source': '/dev/drbd1000',
            'type': 'unix-block',
        }
        profile = mock.Mock()
        profile.devices = {'1': device}
        profile.config = {}
        self.client.profiles.get.return_value = profile
        volume_connector = mock.Mock()
        volume_connector.disconnect_volume.side_effect = RuntimeError(
            'disconnect failed')
        driver.brick_get_connector = mock.Mock(return_value=volume_connector)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'}, '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            RuntimeError, incus_driver.detach_volume,
            ctx, connection_info, instance, '/dev/sdc')

        self.assertEqual({'1': device}, profile.devices)
        self.assertEqual(2, profile.save.call_count)

    def test_detach_volume_uses_persisted_connector_device_info(self):
        profile = mock.Mock()
        profile.devices = {'1': {
            'path': '/dev/sdc',
            'source': '/dev/rbd0',
            'type': 'unix-block',
        }}
        device_info = {
            'path': '/dev/rbd0',
            'type': 'block',
            'conf': '/run/os-brick/volume.conf',
        }
        profile.config = {
            'user.openstack.volume.1': jsonutils.dumps(device_info),
        }
        self.client.profiles.get.return_value = profile
        volume_connector = mock.Mock()
        driver.brick_get_connector = mock.Mock(return_value=volume_connector)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'}, '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.detach_volume(
            ctx, connection_info, instance, '/dev/sdc')

        volume_connector.disconnect_volume.assert_called_once_with(
            connection_info['data'], device_info)
        self.assertNotIn('user.openstack.volume.1', profile.config)

    def test_swap_volume_is_rejected_without_block_copy(self):
        old_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'}, '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        new_info = fake_connection_info(
            {'id': 2, 'name': 'volume-00000002'}, '10.0.2.16:3260',
            'iqn.2010-10.org.openstack:volume-00000002')
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            NotImplementedError, incus_driver.swap_volume,
            context.get_admin_context(), old_info, new_info, instance,
            '/dev/sdd', 2)

        self.client.profiles.get.assert_not_called()

    def test_extend_volume(self):
        volume_connector = mock.Mock()
        volume_connector.extend_volume.return_value = 2 * units.Gi
        driver.brick_get_connector = mock.Mock(return_value=volume_connector)
        connection_info = {'driver_volume_type': 'rbd', 'data': {
            'volume_id': 'volume-id', 'name': 'volumes/volume-id'}}
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.extend_volume(
            ctx, connection_info, instance, 2 * units.Gi)

        volume_connector.extend_volume.assert_called_once_with(
            connection_info['data'])

    def test_extend_bfv_root_updates_incus_size_without_os_brick(self):
        connection_info = {
            'serial': 'root-id',
            'driver_volume_type': 'rbd',
            'data': {'name': 'pool/volume-root-id'},
        }
        container = self.client.instances.get.return_value
        container.devices = {
            'root': {
                'type': 'disk',
                'path': '/',
                'initial.ceph.rbd.image_name': 'volume-root-id',
            },
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        driver.brick_get_connector = mock.Mock()

        incus_driver.extend_volume(
            context.get_admin_context(), connection_info,
            fake_instance.fake_instance_obj(
                context.get_admin_context(), name='test'),
            2 * units.Gi)

        self.assertEqual(
            '%dB' % (2 * units.Gi), container.devices['root']['size'])
        container.save.assert_called_once_with(wait=True)
        driver.brick_get_connector.assert_not_called()

    def test_extend_bfv_root_is_idempotent(self):
        requested_size = 2 * units.Gi
        connection_info = {
            'serial': 'root-id',
            'driver_volume_type': 'rbd',
            'data': {'name': 'pool/volume-root-id'},
        }
        container = self.client.instances.get.return_value
        container.devices = {
            'root': {
                'type': 'disk',
                'path': '/',
                'initial.ceph.rbd.image_name': 'volume-root-id',
                'size': '%dB' % requested_size,
            },
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.extend_volume(
            context.get_admin_context(), connection_info,
            fake_instance.fake_instance_obj(
                context.get_admin_context(), name='test'),
            requested_size)

        container.save.assert_not_called()

    def test_extend_volume_rejects_stale_size(self):
        volume_connector = mock.Mock()
        volume_connector.extend_volume.return_value = units.Gi
        driver.brick_get_connector = mock.Mock(return_value=volume_connector)
        connection_info = {'driver_volume_type': 'rbd', 'data': {
            'volume_id': 'volume-id'}}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.VolumeExtendFailed, incus_driver.extend_volume,
            context.get_admin_context(), connection_info,
            fake_instance.fake_instance_obj(
                context.get_admin_context(), name='test'),
            2 * units.Gi)

    def test_extend_volume_maps_not_implemented(self):
        volume_connector = mock.Mock()
        volume_connector.extend_volume.side_effect = NotImplementedError
        driver.brick_get_connector = mock.Mock(return_value=volume_connector)
        connection_info = {'driver_volume_type': 'unsupported', 'data': {
            'volume_id': 'volume-id'}}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.ExtendVolumeNotSupported, incus_driver.extend_volume,
            context.get_admin_context(), connection_info,
            fake_instance.fake_instance_obj(
                context.get_admin_context(), name='test'),
            2 * units.Gi)

    def test_pause(self):
        container = mock.Mock()
        self.client.instances.get.return_value = container
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.pause(instance)

        self.client.instances.get.assert_called_once_with(instance.name)
        container.freeze.assert_called_once_with(wait=True)

    def test_unpause(self):
        container = mock.Mock()
        self.client.instances.get.return_value = container
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.unpause(instance)

        self.client.instances.get.assert_called_once_with(instance.name)
        container.unfreeze.assert_called_once_with(wait=True)

    def test_suspend_is_rejected_without_memory_checkpoint(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            NotImplementedError, incus_driver.suspend, ctx, instance)
        self.client.instances.get.assert_not_called()

    @mock.patch.object(driver, '_pack_configdrive_for_migration',
                       side_effect=exception.MigrationError(
                           reason='config-drive too large'))
    def test_migrate_disk_rejects_invalid_configdrive_before_shutdown(
            self, pack_configdrive):
        self.CONF.incus.allow_cold_migration = True
        self.CONF.incus.migration_address = 'https://10.224.0.16:8443'
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        instance.config_drive = True
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.MigrationError,
            incus_driver.migrate_disk_and_power_off,
            ctx, instance, '10.224.0.17', instance.flavor, [])

        container = self.client.instances.get.return_value
        pack_configdrive.assert_called_once_with(instance, container)
        container.stop.assert_not_called()

    def test_configdrive_migration_round_trip(self):
        instance = mock.Mock()
        with tempfile.TemporaryDirectory() as temp_dir:
            instance_dir = os.path.join(temp_dir, 'instance')
            configdrive_dir = os.path.join(instance_dir, 'configdrive')
            metadata_dir = os.path.join(
                configdrive_dir, 'openstack', 'latest')
            os.makedirs(metadata_dir)
            metadata_path = os.path.join(metadata_dir, 'meta_data.json')
            with open(metadata_path, 'wb') as stream:
                stream.write(b'{"uuid":"instance-uuid"}')

            container = mock.Mock()
            container.config = {
                'volatile.idmap.current': jsonutils.dumps([
                    {'Isuid': True, 'Hostid': 100000},
                ]),
            }
            attributes = mock.Mock(instance_dir=instance_dir)
            with mock.patch.object(
                    common, 'InstanceAttributes',
                    return_value=attributes), mock.patch.object(
                        driver.privsep_path, 'chown'):
                payload = driver._pack_configdrive_for_migration(
                    instance, container)
                shutil_rmtree = driver.shutil.rmtree
                shutil_rmtree(configdrive_dir)
                staging = driver._stage_configdrive_from_migration(
                    instance, payload)
                with mock.patch.object(
                        driver.processutils, 'execute') as execute:
                    destination = driver._commit_staged_configdrive(
                        instance, container, staging)

            with open(os.path.join(
                    destination, 'openstack', 'latest',
                    'meta_data.json'), 'rb') as stream:
                self.assertEqual(
                    b'{"uuid":"instance-uuid"}', stream.read())
            self.assertEqual(
                0o400,
                stat.S_IMODE(os.stat(os.path.join(
                    destination, 'openstack', 'latest',
                    'meta_data.json')).st_mode))
            execute.assert_called_once()

    def test_container_root_host_id_prefers_next_mapping(self):
        container = mock.Mock()
        container.config = {
            'volatile.idmap.current': jsonutils.dumps([
                {'Isuid': True, 'Hostid': 100000},
            ]),
            'volatile.idmap.next': jsonutils.dumps([
                {'Isuid': True, 'Hostid': 200000},
            ]),
        }

        self.assertEqual(
            200000, driver._container_root_host_id(container))

    def test_configdrive_migration_rejects_symlink(self):
        instance = mock.Mock()
        with tempfile.TemporaryDirectory() as temp_dir:
            instance_dir = os.path.join(temp_dir, 'instance')
            configdrive_dir = os.path.join(instance_dir, 'configdrive')
            os.makedirs(configdrive_dir)
            os.symlink('/etc/passwd', os.path.join(configdrive_dir, 'escape'))
            container = mock.Mock()
            container.config = {
                'volatile.idmap.current': jsonutils.dumps([
                    {'Isuid': True, 'Hostid': 100000},
                ]),
            }
            with mock.patch.object(
                    common, 'InstanceAttributes',
                    return_value=mock.Mock(instance_dir=instance_dir)), \
                    mock.patch.object(driver.privsep_path, 'chown'):
                self.assertRaises(
                    exception.MigrationError,
                    driver._pack_configdrive_for_migration,
                    instance, container)

    def test_configdrive_migration_rejects_path_traversal(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w:gz') as output:
            info = tarfile.TarInfo('../escape')
            info.size = 1
            output.addfile(info, io.BytesIO(b'x'))
        raw = archive.getvalue()
        payload = {
            'format': 'tar.gz-v1',
            'size': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(),
            'data': base64.b64encode(raw).decode('ascii'),
        }
        instance = mock.Mock()
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(
                    common, 'InstanceAttributes',
                    return_value=mock.Mock(instance_dir=temp_dir)):
                self.assertRaises(
                    exception.MigrationError,
                    driver._stage_configdrive_from_migration,
                    instance, payload)
            self.assertFalse(os.path.exists(os.path.join(
                temp_dir, 'escape')))

    def test_resume_is_rejected_without_memory_checkpoint(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            NotImplementedError, incus_driver.resume,
            ctx, instance, None, None)
        self.client.instances.get.assert_not_called()

    def test_resume_state_on_host_boot(self):
        container = mock.Mock()
        state = mock.Mock()
        state.memory = dict({'usage': 0, 'usage_peak': 0})
        state.status_code = 102
        container.state.return_value = state
        self.client.instances.get.return_value = container
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.resume_state_on_host_boot(ctx, instance, None, None, None)
        container.start.assert_called_once_with(wait=True)

    def test_rescue_is_rejected_without_storage_native_implementation(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            NotImplementedError, incus_driver.rescue, ctx, instance, [_VIF],
            mock.Mock(), mock.Mock(), {}, [])
        self.client.instances.get.assert_not_called()
        self.client.profiles.get.assert_not_called()

    def test_unrescue_is_rejected(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            NotImplementedError, incus_driver.unrescue, ctx, instance)
        self.client.instances.get.assert_not_called()
        self.client.profiles.get.assert_not_called()

    def test_power_off(self):
        container = mock.Mock()
        container.status = 'Running'
        self.client.instances.get.return_value = container
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.power_off(instance)

        self.client.instances.get.assert_called_once_with(instance.name)
        container.stop.assert_called_once_with(wait=True)

    def test_power_on(self):
        container = mock.Mock()
        container.status = 'Stopped'
        self.client.instances.get.return_value = container
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.power_on(ctx, instance, None)

        self.client.instances.get.assert_called_once_with(instance.name)
        container.start.assert_called_once_with(wait=True)

    @mock.patch('socket.gethostname', mock.Mock(return_value='fake_hostname'))
    @mock.patch('os.statvfs', return_value=mock.Mock(
        f_blocks=131072000, f_bsize=8192, f_bavail=65536000))
    @mock.patch('builtins.open')
    @mock.patch.object(driver.processutils, 'execute')
    def test_get_available_resource(self, execute, open, statvfs):
        self.CONF.host = 'fake_hostname'
        expected = {
            'cpu_info': {
                "features": "fake flag goes here",
                "model": "Fake CPU",
                "topology": {"sockets": "10", "threads": "4", "cores": "5"},
                "arch": "x86_64", "vendor": "FakeVendor"
            },
            'hypervisor_hostname': 'fake_hostname',
            'hypervisor_type': 'lxd',
            'hypervisor_version': '011',
            'local_gb': 1000,
            'local_gb_used': 500,
            'memory_mb': 10000,
            'memory_mb_used': 8000,
            'numa_topology': None,
            'supported_instances': [
                ('i686', 'lxd', 'exe'),
                ('x86_64', 'lxd', 'exe')],
            'vcpus': 200,
            'vcpus_used': 0}

        execute.return_value = (
            'Model name:          Fake CPU\n'
            'Vendor ID:           FakeVendor\n'
            'Socket(s):           10\n'
            'Core(s) per socket:  5\n'
            'Thread(s) per core:  4\n\n',
            None)
        meminfo = mock.MagicMock()
        meminfo.__enter__.return_value = six.moves.cStringIO(
            'MemTotal: 10240000 kB\n'
            'MemFree:   2000000 kB\n'
            'Buffers:     24000 kB\n'
            'Cached:      24000 kB\n')

        open.side_effect = [
            six.moves.cStringIO('flags: fake flag goes here\n'
                                'processor: 2\n'
                                '\n'),
            meminfo,
        ]
        incus_config = {
            'environment': {
                'storage': 'dir',
            },
            'config': {}
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = mock.MagicMock()
        incus_driver.client.host_info = incus_config
        value = incus_driver.get_available_resource(None)
        # This is funky, but json strings make for fragile tests.
        value['cpu_info'] = jsonutils.loads(value['cpu_info'])

        self.assertEqual(expected, value)

    @mock.patch.object(driver.processutils, 'execute')
    def test__get_zpool_info(self, execute):
        # first test with a zpool; should make 3 calls to execute
        execute.side_effect = [
            ('1\n', None),
            ('2\n', None),
            ('3\n', None)
        ]
        expected = {
            'total': 1,
            'used': 2,
            'available': 3,
        }
        self.assertEqual(expected, driver._get_zpool_info('incus'))

        # then test with a zfs dataset; should just be 2 calls
        execute.reset_mock()
        execute.side_effect = [
            ('10\n', None),
            ('20\n', None),
        ]
        expected = {
            'total': 30,
            'used': 10,
            'available': 20,
        }
        self.assertEqual(expected, driver._get_zpool_info('incus/dataset'))

    def test__get_storage_pool_info(self):
        resources = mock.Mock()
        resources.space = {'total': 30 * units.Gi, 'used': 10 * units.Gi}
        pool = self.client.storage_pools.get.return_value
        pool.resources.get.return_value = resources

        result = driver._get_storage_pool_info(self.client, 'tenant-rootfs')

        self.assertEqual({
            'total': 30 * units.Gi,
            'used': 10 * units.Gi,
            'available': 20 * units.Gi,
        }, result)
        self.client.storage_pools.get.assert_called_once_with('tenant-rootfs')

    @mock.patch('socket.gethostname', mock.Mock(return_value='fake_hostname'))
    @mock.patch('builtins.open')
    @mock.patch.object(driver.processutils, 'execute')
    def test_get_available_resource_zfs(self, execute, open):
        self.CONF.host = 'fake_hostname'
        expected = {
            'cpu_info': {
                "features": "fake flag goes here",
                "model": "Fake CPU",
                "topology": {"sockets": "10", "threads": "4", "cores": "5"},
                "arch": "x86_64", "vendor": "FakeVendor"
            },
            'hypervisor_hostname': 'fake_hostname',
            'hypervisor_type': 'lxd',
            'hypervisor_version': '011',
            'local_gb': 2222,
            'local_gb_used': 200,
            'memory_mb': 10000,
            'memory_mb_used': 8000,
            'numa_topology': None,
            'supported_instances': [
                ('i686', 'lxd', 'exe'),
                ('x86_64', 'lxd', 'exe')],
            'vcpus': 200,
            'vcpus_used': 0}

        execute.side_effect = [
            ('Model name:          Fake CPU\n'
             'Vendor ID:           FakeVendor\n'
             'Socket(s):           10\n'
             'Core(s) per socket:  5\n'
             'Thread(s) per core:  4\n\n',
             None),
            ('2385940232273\n', None),  # 2.17T
            ('215177861529\n', None),   # 200.4G
            ('1979120929996\n', None)   # 1.8T
        ]

        meminfo = mock.MagicMock()
        meminfo.__enter__.return_value = six.moves.cStringIO(
            'MemTotal: 10240000 kB\n'
            'MemFree:   2000000 kB\n'
            'Buffers:     24000 kB\n'
            'Cached:      24000 kB\n')

        open.side_effect = [
            six.moves.cStringIO('flags: fake flag goes here\n'
                                'processor: 2\n'
                                '\n'),
            meminfo,
        ]
        incus_config = {
            'environment': {
                'storage': 'zfs',
            },
            'config': {
                'storage.zfs_pool_name': 'incus',
            }
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = mock.MagicMock()
        incus_driver.client.host_info = incus_config
        value = incus_driver.get_available_resource(None)
        # This is funky, but json strings make for fragile tests.
        value['cpu_info'] = jsonutils.loads(value['cpu_info'])

        self.assertEqual(expected, value)

    def test_refresh_instance_security_rules(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        firewall = mock.Mock()

        incus_driver = driver.IncusDriver(None)
        incus_driver.firewall_driver = firewall
        incus_driver.refresh_instance_security_rules(instance)

        firewall.refresh_instance_security_rules.assert_called_once_with(
            instance)

    def test_ensure_filtering_rules_for_instance(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        firewall = mock.Mock()
        network_info = object()

        incus_driver = driver.IncusDriver(None)
        incus_driver.firewall_driver = firewall
        incus_driver.ensure_filtering_rules_for_instance(
            instance, network_info)

        firewall.ensure_filtering_rules_for_instance.assert_called_once_with(
            instance, network_info)

    def test_filter_defer_apply_on(self):
        firewall = mock.Mock()

        incus_driver = driver.IncusDriver(None)
        incus_driver.firewall_driver = firewall
        incus_driver.filter_defer_apply_on()

        firewall.filter_defer_apply_on.assert_called_once_with()

    def test_filter_defer_apply_off(self):
        firewall = mock.Mock()

        incus_driver = driver.IncusDriver(None)
        incus_driver.firewall_driver = firewall
        incus_driver.filter_defer_apply_off()

        firewall.filter_defer_apply_off.assert_called_once_with()

    def test_unfilter_instance(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        firewall = mock.Mock()
        network_info = object()

        incus_driver = driver.IncusDriver(None)
        incus_driver.firewall_driver = firewall
        incus_driver.unfilter_instance(instance, network_info)

        firewall.unfilter_instance.assert_called_once_with(
            instance, network_info)

    @mock.patch.object(driver.processutils, 'execute')
    def test_get_host_uptime(self, execute):
        expected = '00:00:00 up 0 days, 0:00 , 0 users, load average: 0'
        execute.return_value = (expected, 'stderr')

        incus_driver = driver.IncusDriver(None)
        result = incus_driver.get_host_uptime()

        self.assertEqual(expected, result)

    @mock.patch('nova.virt.incus.driver.psutil.cpu_times')
    @mock.patch('builtins.open')
    @mock.patch.object(driver.processutils, 'execute')
    def test_get_host_cpu_stats(self, execute, open, cpu_times):
        cpu_times.return_value = [
            '1', 'b', '2', '3', '4'
        ]
        execute.return_value = (
            'Model name:          Fake CPU\n'
            'Vendor ID:           FakeVendor\n'
            'Socket(s):           10\n'
            'Core(s) per socket:  5\n'
            'Thread(s) per core:  4\n\n',
            None)
        open.return_value = six.moves.cStringIO(
            'flags: fake flag goes here\n'
            'processor: 2\n\n')

        expected = {
            'user': 1, 'iowait': 4, 'frequency': 0, 'kernel': 2, 'idle': 3}

        incus_driver = driver.IncusDriver(None)
        result = incus_driver.get_host_cpu_stats()

        self.assertEqual(expected, result)

    @mock.patch('nova.virt.incus.driver.brick_get_connector_properties')
    def test_get_volume_connector(self, get_connector_properties):
        expected = {'host': 'compute-01', 'ip': '192.0.2.10'}
        get_connector_properties.return_value = expected
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)

        incus_driver = driver.IncusDriver(None)
        result = incus_driver.get_volume_connector(instance)

        self.assertEqual(expected, result)
        get_connector_properties.assert_called_once_with()

    def test_get_available_nodes(self):
        self.CONF.host = 'nova-incus'
        expected = ['nova-incus']

        incus_driver = driver.IncusDriver(None)
        result = incus_driver.get_available_nodes()

        self.assertEqual(expected, result)

    @mock.patch('nova.virt.incus.driver.IMAGE_API')
    @mock.patch('nova.virt.incus.driver.lockutils.lock')
    def test_snapshot(self, lock, IMAGE_API):
        update_task_state_expected = [
            mock.call(task_state='image_pending_upload'),
            mock.call(
                expected_state='image_pending_upload',
                task_state='image_uploading'),
        ]

        container = mock.Mock()
        self.client.instances.get.return_value = container
        instance_snapshot = mock.Mock()
        container.snapshots.create.return_value = instance_snapshot
        image = mock.Mock()
        instance_snapshot.publish.return_value = image
        data = mock.Mock()
        image.export.return_value = data
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        image_id = mock.Mock()
        update_task_state = mock.Mock()
        snapshot = {'name': mock.Mock()}
        IMAGE_API.get.return_value = snapshot

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.snapshot(ctx, instance, image_id, update_task_state)

        self.assertEqual(
            update_task_state_expected, update_task_state.call_args_list)
        IMAGE_API.get.assert_called_once_with(ctx, image_id)
        IMAGE_API.update.assert_called_once_with(
            ctx, image_id, {
                'name': snapshot['name'],
                'disk_format': 'raw',
                'container_format': 'bare'},
            data)
        data.close.assert_called_once_with()
        image.delete.assert_called_once_with(wait=True)
        instance_snapshot.delete.assert_called_once_with(wait=True)
        container.snapshots.create.assert_called_once_with(
            'nova-{}'.format(image_id), wait=True)

    @mock.patch('nova.virt.incus.driver.IMAGE_API')
    @mock.patch('nova.virt.incus.driver.lockutils.lock')
    def test_snapshot_upload_failure_cleans_temporary_resources(
            self, lock, IMAGE_API):
        container = mock.Mock(status='Running')
        instance_snapshot = mock.Mock()
        image = mock.Mock()
        data = mock.Mock()
        container.snapshots.create.return_value = instance_snapshot
        instance_snapshot.publish.return_value = image
        image.export.return_value = data
        self.client.instances.get.return_value = container
        IMAGE_API.get.return_value = {'name': 'snapshot'}
        IMAGE_API.update.side_effect = RuntimeError('upload failed')
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            RuntimeError, incus_driver.snapshot, ctx, instance, 'image-id',
            mock.Mock())

        data.close.assert_called_once_with()
        image.delete.assert_called_once_with(wait=True)
        instance_snapshot.delete.assert_called_once_with(wait=True)
        container.stop.assert_not_called()
        container.start.assert_not_called()

    def test_finish_revert_migration(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = []

        container = mock.Mock()
        container.status = 'Stopped'
        self.client.instances.get.return_value = container
        migration = mock.Mock(
            source_compute='compute', dest_compute='compute')

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.finish_revert_migration(
            ctx, instance, network_info, migration)

        container.start.assert_called_once_with(wait=True)

    def test_get_vcpus_used_counts_only_nova_owned_records(self):
        owned = mock.Mock(
            name='owned',
            expanded_config={
                'user.openstack.uuid': '00000000-0000-0000-0000-000000000001',
                'limits.cpu': '4',
            })
        owned.name = 'instance-owned'
        stopped = mock.Mock(
            name='stopped',
            expanded_config={
                'user.openstack.uuid': '00000000-0000-0000-0000-000000000002',
                'limits.cpu': '2',
            })
        stopped.name = 'instance-stopped'
        foreign = mock.Mock(
            name='foreign',
            expanded_config={'limits.cpu': '32'})
        foreign.name = 'operator-container'
        malformed = mock.Mock(
            name='malformed',
            expanded_config={
                'user.openstack.uuid': '00000000-0000-0000-0000-000000000003',
                'limits.cpu': '0-3',
            })
        malformed.name = 'instance-malformed'
        self.client.instances.all.return_value = [
            owned, stopped, foreign, malformed]
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertEqual(6, incus_driver._get_vcpus_used())

    def test_set_admin_password_uses_stdin_and_image_admin_user(self):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-password',
            image_meta=mock.Mock(properties={'os_admin_user': 'ubuntu'}))
        container = self.client.instances.get.return_value
        container.execute.return_value = mock.Mock(
            exit_code=0, stdout='', stderr='')
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        incus_driver.set_admin_password(instance, 's3cret:with-colon')

        container.execute.assert_called_once_with(
            ['chpasswd'],
            stdin_payload='ubuntu:s3cret:with-colon\n',
            user=0,
            group=0)

    def test_set_admin_password_rejects_newline(self):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-password',
            image_meta=mock.Mock(properties={}))
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertRaises(
            exception.InstancePasswordSetFailed,
            incus_driver.set_admin_password,
            instance,
            'unsafe\npassword')
        self.client.instances.get.assert_not_called()

    def test_set_admin_password_rejects_guest_without_chpasswd(self):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-password',
            image_meta=mock.Mock(properties={}))
        container = self.client.instances.get.return_value
        container.execute.return_value = mock.Mock(
            exit_code=127, stdout='', stderr='not found')
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertRaises(
            exception.SetAdminPasswdNotSupported,
            incus_driver.set_admin_password,
            instance,
            'secret')

    @mock.patch.object(driver.incus_console, 'SerialConsoleBroker')
    def test_get_serial_console_reuses_instance_broker(self, broker_factory):
        self.CONF.serial_console.enabled = True
        self.CONF.serial_console.proxyclient_address = '192.0.2.10'
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-console')
        container = self.client.instances.get.return_value
        container.status = 'Running'
        broker_factory.return_value.port = 10001
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        first = incus_driver.get_serial_console(None, instance)
        second = incus_driver.get_serial_console(None, instance)

        self.assertEqual('192.0.2.10', first.host)
        self.assertEqual(10001, first.port)
        self.assertIs(first.__class__, second.__class__)
        broker_factory.assert_called_once_with('192.0.2.10', container)

    def test_get_serial_console_rejects_stopped_instance(self):
        self.CONF.serial_console.enabled = True
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-console')
        self.client.instances.get.return_value.status = 'Stopped'
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertRaises(
            exception.InstanceNotRunning,
            incus_driver.get_serial_console, None, instance)

    @mock.patch.object(driver.os.path, 'ismount', return_value=False)
    @mock.patch.object(driver.privsep_fs, 'mount')
    @mock.patch.object(driver.os, 'chmod')
    def test_mount_nfs_share_stages_incus_device(
            self, chmod, mount, ismount):
        ismount.side_effect = [False, False, True]
        driver.fileutils.ensure_tree.reset_mock()
        self.CONF.incus.enable_manila_shares = True
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            share_proto='NFS')
        profile = self.client.profiles.get.return_value
        profile.devices = {}
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        incus_driver.mount_share(None, instance, share)

        mount_path = driver._share_mount_path(instance, share)
        share_root = os.path.join(
            self.CONF.instances_path, 'incus-shares')
        instance_root = os.path.join(share_root, instance.uuid)
        self.assertEqual([
            mock.call(share_root, mode=0o711),
            mock.call(instance_root, mode=0o711),
            mock.call(mount_path, mode=0o700),
        ], driver.fileutils.ensure_tree.call_args_list)
        self.assertEqual([
            mock.call(share_root, 0o711),
            mock.call(instance_root, 0o711),
            mock.call(mount_path, 0o700),
        ], chmod.call_args_list)
        mount.assert_called_once_with(
            'nfs', share.export_location, mount_path,
            ['-o', 'nosuid,nodev'])
        self.assertEqual({
            'type': 'disk',
            'source': mount_path,
            'path': '/mnt/manila/project-data',
            'readonly': 'false',
            'recursive': 'true',
        }, profile.devices[driver._share_device_name(share)])
        profile.save.assert_called_once_with(wait=True)

    @mock.patch.object(driver.os.path, 'ismount', return_value=True)
    @mock.patch.object(driver.privsep_fs, 'mount')
    @mock.patch.object(driver.os, 'chmod')
    def test_mount_nfs_share_does_not_chmod_existing_mount(
            self, chmod, mount, ismount):
        driver.fileutils.ensure_tree.reset_mock()
        self.CONF.incus.enable_manila_shares = True
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            share_proto='NFS')
        profile = self.client.profiles.get.return_value
        profile.devices = {}
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        incus_driver.mount_share(None, instance, share)

        mount_path = driver._share_mount_path(instance, share)
        self.assertNotIn(mock.call(mount_path, 0o700), chmod.call_args_list)
        mount.assert_not_called()
        self.assertEqual(
            mount_path,
            profile.devices[driver._share_device_name(share)]['source'])

    @mock.patch.object(driver.os.path, 'ismount', return_value=True)
    @mock.patch.object(driver.privsep_fs, 'umount')
    @mock.patch.object(driver.os, 'rmdir')
    def test_umount_share_removes_device_before_host_mount(
            self, rmdir, umount, ismount):
        self.CONF.incus.enable_manila_shares = True
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            share_proto='NFS')
        profile = self.client.profiles.get.return_value
        profile.devices = {
            driver._share_device_name(share): {'type': 'disk'}}
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertFalse(incus_driver.umount_share(None, instance, share))

        self.assertNotIn(driver._share_device_name(share), profile.devices)
        profile.save.assert_called_once_with(wait=True)
        umount.assert_called_once_with(
            driver._share_mount_path(instance, share))

    @mock.patch.object(driver.os.path, 'isdir', return_value=True)
    @mock.patch.object(driver.os.path, 'ismount', return_value=True)
    @mock.patch.object(driver.privsep_fs, 'umount')
    @mock.patch.object(driver.os, 'rmdir')
    def test_umount_share_keeps_parent_with_other_share(
            self, rmdir, umount, ismount, isdir):
        self.CONF.incus.enable_manila_shares = True
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            share_proto='NFS')
        profile = self.client.profiles.get.return_value
        profile.devices = {
            driver._share_device_name(share): {'type': 'disk'}}
        rmdir.side_effect = [
            None,
            OSError(errno.ENOTEMPTY, 'Directory not empty'),
        ]
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertFalse(incus_driver.umount_share(None, instance, share))

        self.assertEqual(2, rmdir.call_count)
        umount.assert_called_once_with(
            driver._share_mount_path(instance, share))

    @mock.patch.object(driver.os.path, 'isdir', return_value=True)
    @mock.patch.object(driver.os.path, 'ismount', return_value=True)
    @mock.patch.object(driver.privsep_fs, 'umount')
    @mock.patch.object(driver.os, 'rmdir')
    def test_umount_share_reports_parent_removal_error(
            self, rmdir, umount, ismount, isdir):
        self.CONF.incus.enable_manila_shares = True
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            share_proto='NFS')
        profile = self.client.profiles.get.return_value
        profile.devices = {
            driver._share_device_name(share): {'type': 'disk'}}
        rmdir.side_effect = [
            None,
            OSError(errno.EACCES, 'Permission denied'),
        ]
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertRaises(
            exception.ShareUmountError,
            incus_driver.umount_share, None, instance, share)

    def test_mount_share_disabled_is_explicitly_rejected(self):
        self.CONF.incus.enable_manila_shares = False
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        share = mock.Mock(share_proto='NFS')
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertRaises(
            exception.ShareProtocolNotSupported,
            incus_driver.mount_share, None, instance, share)

    @mock.patch.object(driver.os.path, 'ismount', return_value=True)
    @mock.patch.object(driver.privsep_fs, 'umount')
    @mock.patch.object(driver.os, 'rmdir')
    def test_post_live_migration_source_cleans_validated_share_mount(
            self, rmdir, umount, ismount):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        share_id = '10000000-0000-0000-0000-000000000001'
        mount_path = os.path.join(
            self.CONF.instances_path, 'incus-shares',
            instance.uuid, share_id)
        profile = self.client.profiles.get.return_value
        profile.devices = {
            'manila-' + share_id: {
                'type': 'disk',
                'source': mount_path,
                'path': '/mnt/manila/project-data',
            },
            'manila-not-a-uuid': {
                'type': 'disk',
                'source': '/must/not/unmount',
            },
            'root': {'type': 'disk', 'source': mount_path},
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client
        incus_driver.cleanup = mock.Mock()

        incus_driver.post_live_migration_at_source(
            None, instance, mock.sentinel.network_info)

        umount.assert_called_once_with(os.path.realpath(mount_path))
        incus_driver.cleanup.assert_called_once_with(
            None, instance, mock.sentinel.network_info)
        self.vif_driver.plug.assert_not_called()
        self.vif_driver.unplug.assert_not_called()

    def test_finish_revert_migration_refreshes_retained_vif(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        vif = mock.Mock()
        container = mock.Mock(status='Stopped')
        self.client.instances.get.return_value = container
        migration = mock.Mock(
            source_compute='source', dest_compute='destination')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        parent = mock.Mock()
        parent.attach_mock(self.vif_driver.unplug, 'unplug')
        parent.attach_mock(self.vif_driver.plug, 'plug')
        parent.attach_mock(container.start, 'start')

        incus_driver.finish_revert_migration(
            ctx, instance, [vif], migration)

        self.assertEqual(
            [mock.call.unplug(instance, vif),
             mock.call.plug(instance, vif),
             mock.call.start(wait=True)],
            parent.mock_calls)

    @mock.patch.object(driver, '_require_bfv_migration_support')
    @mock.patch.object(driver, '_boot_from_volume')
    @mock.patch('nova.virt.driver.block_device_info_get_mapping')
    def test_finish_revert_migration_attaches_only_data_volumes(
            self, get_mapping, boot_from_volume, require_bfv):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        container = mock.Mock(status='Stopped')
        self.client.instances.get.return_value = container
        migration = mock.Mock(
            source_compute='source', dest_compute='destination')
        root_bdm = {
            'boot_index': 0,
            'connection_info': mock.sentinel.root_connection,
            'mount_device': '/dev/sda',
        }
        data_connection = {'driver_volume_type': 'local'}
        data_bdm = {
            'boot_index': 1,
            'connection_info': data_connection,
            'mount_device': '/dev/vdb',
        }
        boot_from_volume.return_value = root_bdm
        get_mapping.return_value = [root_bdm, data_bdm]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.attach_volume = mock.Mock()

        incus_driver.finish_revert_migration(
            ctx, instance, [], migration, block_device_info={}, power_on=True)

        incus_driver.attach_volume.assert_called_once_with(
            ctx, data_connection, instance, '/dev/vdb')
        require_bfv.assert_called_once_with(self.client, root_bdm)
        container.start.assert_called_once_with(wait=True)

    @mock.patch.object(driver, '_require_bfv_migration_support')
    @mock.patch.object(driver, '_boot_from_volume')
    @mock.patch('nova.virt.driver.block_device_info_get_mapping',
                return_value=[])
    def test_finish_revert_migration_marks_failed_bfv_owner(
            self, get_mapping, boot_from_volume, require_bfv):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        boot_from_volume.return_value = {'boot_index': 0}
        container = mock.Mock(status='Stopped')
        container.start.side_effect = RuntimeError('start failed')
        self.client.instances.get.return_value = container
        profile = self.client.profiles.get.return_value
        profile.config = {}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.finish_revert_migration(
            ctx, instance, [], mock.Mock(), block_device_info={},
            power_on=True)

        self.assertEqual(
            self.CONF.incus.migration_finish_retries,
            container.start.call_count)
        self.assertEqual(
            'running', profile.config[driver.MIGRATION_RECOVERY_KEY])
        profile.save.assert_called_once_with(wait=True)

    @mock.patch.object(driver.flavor, 'to_profile')
    def test_finish_migration_same_host(self, to_profile):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        migration = mock.Mock(
            source_compute='compute', dest_compute='compute')
        container = mock.Mock()
        self.client.instances.create.return_value = container
        self.CONF.incus.allow_cold_migration = True
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        disk_info = jsonutils.dumps({
            'format': 'incus-pull-v1',
            'migration_data': {
                'name': instance.name,
                'source': {'type': 'migration'},
            },
            'was_running': True,
        })
        incus_driver.finish_migration(
            ctx, migration, instance, disk_info, [], mock.Mock(), True, {},
            block_device_info=None, power_on=True)

        to_profile.assert_called_once_with(
            self.client, instance, [], None)
        self.client.instances.create.assert_called_once_with(
            {
                'name': instance.name,
                'source': {'type': 'migration'},
                'config': {'boot.autostart': 'false'},
            },
            wait=True)
        container.start.assert_called_once_with(wait=True)

    @mock.patch.object(driver.flavor, 'to_profile')
    def test_finish_migration_create_failure_rolls_back(self, to_profile):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        migration = mock.Mock(
            source_compute='source', dest_compute='destination')
        network_info = [mock.sentinel.vif]
        profile = to_profile.return_value
        self.client.instances.create.side_effect = RuntimeError(
            'destination pull failed')
        self.CONF.incus.allow_cold_migration = True
        disk_info = jsonutils.dumps({
            'format': 'incus-pull-v1',
            'migration_data': {
                'name': instance.name,
                'source': {'type': 'migration'},
            },
        })
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            RuntimeError,
            incus_driver.finish_migration,
            ctx, migration, instance, disk_info, network_info, mock.Mock(),
            True, {}, block_device_info=None, power_on=True)

        self.vif_driver.plug.assert_called_once_with(
            instance, mock.sentinel.vif)
        self.vif_driver.unplug.assert_called_once_with(
            instance, mock.sentinel.vif)
        profile.delete.assert_called_once_with()

    @mock.patch.object(driver, '_require_bfv_migration_support')
    @mock.patch.object(driver, '_boot_from_volume')
    @mock.patch('nova.virt.driver.block_device_info_get_mapping',
                return_value=[])
    @mock.patch.object(driver.flavor, 'to_profile')
    def test_finish_migration_retains_claimed_bfv_target_on_start_failure(
            self, to_profile, get_mapping, boot_from_volume, require_bfv):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        migration = mock.Mock(
            source_compute='source', dest_compute='destination')
        root_bdm = {'boot_index': 0}
        boot_from_volume.return_value = root_bdm
        profile = to_profile.return_value
        profile.config = {}
        container = self.client.instances.create.return_value
        container.start.side_effect = RuntimeError('target start failed')
        self.CONF.incus.allow_cold_migration = True
        disk_info = jsonutils.dumps({
            'format': 'incus-pull-v1',
            'boot_from_volume': True,
            'migration_data': {
                'name': instance.name,
                'source': {'type': 'migration'},
            },
        })
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.finish_migration(
            ctx, migration, instance, disk_info, [mock.sentinel.vif],
            mock.Mock(), True, {}, block_device_info={}, power_on=True)

        require_bfv.assert_called_once_with(self.client, root_bdm)
        self.assertEqual(3, container.start.call_count)
        self.assertEqual(
            'running',
            profile.config[driver.MIGRATION_RECOVERY_KEY])
        profile.save.assert_called()
        container.delete.assert_not_called()
        profile.delete.assert_not_called()
        self.vif_driver.unplug.assert_not_called()

    @mock.patch.object(driver, '_require_bfv_migration_support')
    @mock.patch.object(driver, '_boot_from_volume')
    @mock.patch('nova.virt.driver.block_device_info_get_mapping',
                return_value=[])
    @mock.patch.object(driver.flavor, 'to_profile')
    def test_finish_migration_retries_transient_marker_failure(
            self, to_profile, get_mapping, boot_from_volume, require_bfv):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        boot_from_volume.return_value = {'boot_index': 0}
        profile = to_profile.return_value
        profile.config = {}
        profile.save.side_effect = [RuntimeError('database busy'), None]
        container = self.client.instances.create.return_value
        container.start.side_effect = RuntimeError('target start failed')
        self.CONF.incus.allow_cold_migration = True
        disk_info = jsonutils.dumps({
            'format': 'incus-pull-v1',
            'boot_from_volume': True,
            'migration_data': {
                'name': instance.name,
                'source': {'type': 'migration'},
            },
        })
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.finish_migration(
            ctx, mock.Mock(), instance, disk_info, [mock.sentinel.vif],
            mock.Mock(), True, {}, block_device_info={}, power_on=True)

        self.assertEqual(2, profile.save.call_count)
        self.assertEqual(
            'running', profile.config[driver.MIGRATION_RECOVERY_KEY])
        container.delete.assert_not_called()
        profile.delete.assert_not_called()
        self.vif_driver.unplug.assert_not_called()

    @mock.patch.object(driver, '_require_bfv_migration_support')
    @mock.patch.object(driver, '_boot_from_volume')
    @mock.patch('nova.virt.driver.block_device_info_get_mapping',
                return_value=[])
    @mock.patch.object(driver.flavor, 'to_profile')
    def test_finish_migration_marker_failure_reraises_start_failure(
            self, to_profile, get_mapping, boot_from_volume, require_bfv):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        root_bdm = {'boot_index': 0}
        boot_from_volume.return_value = root_bdm
        require_bfv.return_value = ('cinder-volumes', 'volume-root')
        profile = to_profile.return_value
        profile.devices = {}
        profile.save.side_effect = [
            None,
            RuntimeError('database unavailable'),
            RuntimeError('database unavailable'),
            RuntimeError('database unavailable'),
        ]
        container = self.client.instances.create.return_value
        container.start.side_effect = RuntimeError('target start failed')
        self.CONF.incus.allow_cold_migration = True
        disk_info = jsonutils.dumps({
            'format': 'incus-pull-v1',
            'boot_from_volume': True,
            'migration_data': {
                'name': instance.name,
                'source': {'type': 'migration'},
            },
        })
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            RuntimeError,
            incus_driver.finish_migration,
            ctx, mock.Mock(), instance, disk_info, [mock.sentinel.vif],
            mock.Mock(), True, {}, block_device_info={}, power_on=True)

        container.delete.assert_not_called()
        profile.delete.assert_not_called()
        self.vif_driver.unplug.assert_not_called()
        self.assertEqual(
            self.CONF.incus.migration_finish_retries + 1,
            profile.save.call_count)

    @mock.patch.object(driver, '_require_bfv_migration_support')
    @mock.patch.object(driver, '_boot_from_volume')
    @mock.patch('nova.virt.driver.block_device_info_get_mapping',
                return_value=[])
    @mock.patch.object(driver.flavor, 'to_profile')
    def test_finish_migration_retries_transient_target_start_failure(
            self, to_profile, get_mapping, boot_from_volume, require_bfv):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        migration = mock.Mock(
            source_compute='source', dest_compute='destination')
        root_bdm = {'boot_index': 0}
        boot_from_volume.return_value = root_bdm
        container = self.client.instances.create.return_value
        container.start.side_effect = [
            RuntimeError('transient target start failure'), None]
        self.CONF.incus.allow_cold_migration = True
        disk_info = jsonutils.dumps({
            'format': 'incus-pull-v1',
            'boot_from_volume': True,
            'migration_data': {
                'name': instance.name,
                'source': {'type': 'migration'},
            },
        })
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.finish_migration(
            ctx, migration, instance, disk_info, [mock.sentinel.vif],
            mock.Mock(), True, {}, block_device_info={}, power_on=True)

        self.assertEqual(2, container.start.call_count)
        self.client.instances.get.assert_not_called()
        to_profile.return_value.delete.assert_not_called()
        self.vif_driver.unplug.assert_not_called()

    @mock.patch.object(driver, '_require_bfv_migration_support')
    @mock.patch.object(driver, '_boot_from_volume')
    @mock.patch.object(driver.flavor, 'to_profile')
    def test_finish_migration_recovers_bfv_target_after_create_timeout(
            self, to_profile, boot_from_volume, require_bfv):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        migration = mock.Mock(
            source_compute='source', dest_compute='destination')
        root_bdm = {'boot_index': 0}
        boot_from_volume.return_value = root_bdm
        profile = to_profile.return_value
        profile.config = {}
        claimed_container = mock.Mock()
        self.client.instances.create.side_effect = RuntimeError(
            'response timed out after server accepted create')
        self.client.instances.get.return_value = claimed_container
        self.CONF.incus.allow_cold_migration = True
        disk_info = jsonutils.dumps({
            'format': 'incus-pull-v1',
            'boot_from_volume': True,
            'migration_data': {
                'name': instance.name,
                'source': {'type': 'migration'},
            },
        })
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.finish_migration(
            ctx, migration, instance, disk_info, [mock.sentinel.vif],
            mock.Mock(), True, {}, block_device_info={}, power_on=True)

        self.client.instances.get.assert_called_once_with(instance.name)
        self.assertEqual(
            'running',
            profile.config[driver.MIGRATION_RECOVERY_KEY])
        claimed_container.delete.assert_not_called()
        profile.delete.assert_not_called()
        self.vif_driver.unplug.assert_not_called()

    def test_finish_migration_rejects_bfv_mode_mismatch(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        migration = mock.Mock(
            source_compute='source', dest_compute='destination')
        self.CONF.incus.allow_cold_migration = True
        disk_info = jsonutils.dumps({
            'format': 'incus-pull-v1',
            'boot_from_volume': True,
            'migration_data': {
                'name': instance.name,
                'source': {'type': 'migration'},
            },
        })
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.MigrationError,
            incus_driver.finish_migration,
            ctx, migration, instance, disk_info, [], mock.Mock(), True, {},
            block_device_info=None, power_on=True)

        self.client.instances.create.assert_not_called()

    @mock.patch.object(driver, '_require_bfv_migration_support')
    @mock.patch.object(driver, '_boot_from_volume')
    @mock.patch('nova.virt.driver.block_device_info_get_mapping')
    @mock.patch.object(driver.flavor, 'to_profile')
    def test_finish_migration_attaches_only_data_volumes(
            self, to_profile, get_mapping, boot_from_volume, require_bfv):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        migration = mock.Mock(
            source_compute='source', dest_compute='destination')
        container = self.client.instances.create.return_value
        volume_id = '8231d2e8-1111-4222-8333-123456789abc'
        root_bdm = {
            'boot_index': 0,
            'connection_info': {
                'driver_volume_type': 'rbd',
                'serial': volume_id,
                'data': {
                    'name': 'cinder-volumes/volume-%s' % volume_id,
                },
            },
            'mount_device': '/dev/sda',
        }
        data_connection = {'driver_volume_type': 'local'}
        data_bdm = {
            'boot_index': 1,
            'connection_info': data_connection,
            'mount_device': '/dev/vdb',
        }
        boot_from_volume.return_value = root_bdm
        require_bfv.return_value = (
            'cinder-volumes', 'volume-%s' % volume_id)
        get_mapping.return_value = [root_bdm, data_bdm]
        profile = to_profile.return_value
        profile.devices = {'root': {'size': '10GB'}}
        self.CONF.incus.allow_cold_migration = True
        self.CONF.incus.boot_from_volume_storage_pools = {
            'cinder-volumes': 'cinder'}
        disk_info = jsonutils.dumps({
            'format': 'incus-pull-v1',
            'boot_from_volume': True,
            'migration_data': {
                'name': instance.name,
                'source': {'type': 'migration'},
            },
        })
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.attach_volume = mock.Mock()

        incus_driver.finish_migration(
            ctx, migration, instance, disk_info, [], mock.Mock(), True, {},
            block_device_info={}, power_on=True)

        incus_driver.attach_volume.assert_called_once_with(
            ctx, data_connection, instance, '/dev/vdb')
        require_bfv.assert_called_once_with(self.client, root_bdm)
        container.start.assert_called_once_with(wait=True)
        self.assertEqual('cinder', profile.devices['root']['pool'])
        self.assertNotIn('size', profile.devices['root'])

    def test_check_can_live_migrate_destination_disabled(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        src_compute_info = mock.Mock()
        dst_compute_info = mock.Mock()

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.MigrationPreCheckError,
            incus_driver.check_can_live_migrate_destination,
            ctx, instance, src_compute_info, dst_compute_info)

    def test_check_can_live_migrate_destination_returns_host_facts(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        self.CONF.incus.allow_live_migration = True
        self.CONF.incus.migration_address = 'https://192.0.2.20:8443'
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        data = incus_driver.check_can_live_migrate_destination(
            ctx, instance, mock.Mock(), mock.Mock())

        self.assertIsInstance(data, migrate_data.IncusLiveMigrateData)
        self.assertEqual(
            'https://192.0.2.20:8443', data.destination_address)
        self.assertEqual('x86_64', data.destination_architecture)
        self.assertEqual('6.8.0-test', data.destination_kernel_version)
        self.assertEqual('7.2', data.destination_server_version)

    def test_live_migrate_destination_rejects_old_ceph_handover(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        self.CONF.incus.allow_live_migration = True
        self.CONF.incus.migration_address = 'https://192.0.2.20:8443'
        self.client.host_info['api_extensions'].remove(
            'migration_live_shared_ceph_storage')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        exc = self.assertRaises(
            exception.MigrationPreCheckError,
            incus_driver.check_can_live_migrate_destination,
            ctx, instance, mock.Mock(), mock.Mock())

        self.assertIn(
            'migration_live_shared_ceph_storage', str(exc))

    def test_check_can_live_migrate_source_accepts_compatible_container(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', config_drive=False)
        instance.config_drive = ''
        self.CONF.incus.allow_live_migration = True
        profile = mock.Mock()
        profile.config = {'migration.stateful': 'true'}
        profile.devices = {
            'root': {'type': 'disk', 'path': '/'},
            'eth0': {'type': 'nic'},
        }
        self.client.profiles.get.return_value = profile
        self.client.instances.get.return_value.status = 'Running'
        self.client.instances.get.return_value.config = {
            'volatile.idmap.base': '1065536'}
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        result = incus_driver.check_can_live_migrate_source(
            ctx, instance, data, {'block_device_mapping': []})

        self.assertIs(data, result)
        source_profile = jsonutils.loads(result.source_profile)
        self.assertEqual('1065536',
                         source_profile['config']['security.idmap.base'])
        self.assertEqual(profile.devices, source_profile['devices'])

    @mock.patch.object(
        driver.objects.ShareMappingList, 'get_by_instance_uuid')
    def test_check_can_live_migrate_source_accepts_manila_share(
            self, get_shares):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', config_drive=False)
        instance.config_drive = ''
        self.CONF.incus.allow_live_migration = True
        share_id = '10000000-0000-0000-0000-000000000001'
        mapping = mock.Mock(
            share_id=share_id,
            instance_uuid=instance.uuid,
            tag='project-data',
            status=driver.obj_fields.ShareMappingStatus.ACTIVE)
        get_shares.return_value = [mapping]
        share_device = {
            'type': 'disk',
            'source': os.path.join(
                self.CONF.instances_path, 'incus-shares',
                instance.uuid, share_id),
            'path': '/mnt/manila/project-data',
            'readonly': 'false',
            'recursive': 'true',
        }
        profile = mock.Mock(
            config={'migration.stateful': 'true'},
            devices={
                'root': {'type': 'disk', 'path': '/'},
                'manila-' + share_id: share_device,
            })
        self.client.profiles.get.return_value = profile
        self.client.instances.get.return_value.status = 'Running'
        self.client.instances.get.return_value.config = {
            'volatile.idmap.base': '1065536'}
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        result = incus_driver.check_can_live_migrate_source(
            ctx, instance, data, {'block_device_mapping': []})

        self.assertEqual(
            share_device,
            jsonutils.loads(result.source_profile)['devices'][
                'manila-' + share_id])

    @mock.patch.object(
        driver.objects.ShareMappingList, 'get_by_instance_uuid')
    def test_check_can_live_migrate_source_rejects_forged_manila_device(
            self, get_shares):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', config_drive=False)
        instance.config_drive = ''
        self.CONF.incus.allow_live_migration = True
        get_shares.return_value = []
        profile = mock.Mock(
            config={'migration.stateful': 'true'},
            devices={
                'root': {'type': 'disk', 'path': '/'},
                'manila-10000000-0000-0000-0000-000000000001': {
                    'type': 'disk',
                    'source': '/etc',
                    'path': '/mnt/manila/forged',
                },
            })
        self.client.profiles.get.return_value = profile
        self.client.instances.get.return_value.status = 'Running'
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaisesRegex(
            exception.MigrationPreCheckError,
            'do not match Nova share mappings',
            incus_driver.check_can_live_migrate_source,
            ctx, instance, data, {'block_device_mapping': []})

    @mock.patch.object(driver, '_preflight_bfv_migration_destination')
    @mock.patch.object(driver, '_require_bfv_live_migration_support')
    def test_check_can_live_migrate_source_accepts_bfv_root(
            self, require_bfv, preflight_destination):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', config_drive=False)
        instance.config_drive = ''
        self.CONF.incus.allow_live_migration = True
        profile = mock.Mock(
            config={'migration.stateful': 'true'},
            devices={'root': {'type': 'disk', 'path': '/', 'pool': 'cinder'}})
        container = self.client.instances.get.return_value
        container.status = 'Running'
        container.config = {'volatile.idmap.base': '1065536'}
        self.client.profiles.get.return_value = profile
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        root_bdm = {
            'boot_index': 0,
            'mount_device': '/dev/vda',
            'connection_info': {'serial': 'root-volume'},
        }
        require_bfv.return_value = (
            'cinder-volumes', 'volume-root')

        result = incus_driver.check_can_live_migrate_source(
            ctx, instance, data,
            {'block_device_mapping': [root_bdm]})

        self.assertIs(data, result)
        require_bfv.assert_called_once_with(self.client, root_bdm)
        preflight_destination.assert_called_once_with(
            '192.0.2.20', 'cinder-volumes', live=True)

    @mock.patch.object(driver.incus_client,
                       'get_migration_preflight_client')
    @mock.patch.object(driver.socket, 'create_connection')
    def test_bfv_live_destination_preflight_requires_live_extension(
            self, connect, get_remote):
        get_remote.return_value.host_info = {'api_extensions': [
            'migration_shared_ceph_storage', 'storage_driver_cephext']}

        self.assertRaisesRegex(
            exception.MigrationError,
            driver.INCUS_LIVE_BFV_MIGRATION_EXTENSION,
            driver._preflight_bfv_migration_destination,
            'compute-2.example.test', 'cinder-volumes', live=True)

    @mock.patch.object(driver, '_require_bfv_migration_support')
    def test_require_bfv_live_migration_support_requires_extension(
            self, require_bfv):
        require_bfv.return_value = (
            'cinder-volumes', 'volume-root')
        extensions = self.client.host_info['api_extensions']
        if driver.INCUS_LIVE_BFV_MIGRATION_EXTENSION in extensions:
            extensions.remove(driver.INCUS_LIVE_BFV_MIGRATION_EXTENSION)

        self.assertRaisesRegex(
            exception.MigrationPreCheckError,
            driver.INCUS_LIVE_BFV_MIGRATION_EXTENSION,
            driver._require_bfv_live_migration_support,
            self.client, mock.sentinel.root_bdm)

    def test_check_can_live_migrate_source_accepts_cinder_data_volume(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', config_drive=False)
        instance.config_drive = ''
        self.CONF.incus.allow_live_migration = True
        profile = mock.Mock(
            config={'migration.stateful': 'true'},
            devices={
                'root': {'type': 'disk', 'path': '/'},
                'volume-id': {
                    'type': 'unix-block',
                    'path': '/dev/vdb',
                    'source': '/dev/rbd0',
                },
            })
        container = self.client.instances.get.return_value
        container.status = 'Running'
        container.config = {'volatile.idmap.base': '1065536'}
        self.client.profiles.get.return_value = profile
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        result = incus_driver.check_can_live_migrate_source(
            ctx, instance, data, {'block_device_mapping': [{
                'boot_index': None,
                'mount_device': '/dev/vdb',
                'connection_info': {
                    'serial': 'volume-id',
                    'driver_volume_type': 'rbd',
                    'data': {},
                },
            }]})

        source_profile = jsonutils.loads(result.source_profile)
        self.assertNotIn('volume-id', source_profile['devices'])
        self.assertIn('root', source_profile['devices'])

    def test_check_can_live_migrate_source_rejects_kernel_mismatch(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', config_drive=False)
        instance.config_drive = ''
        self.CONF.incus.allow_live_migration = True
        profile = mock.Mock()
        profile.config = {'migration.stateful': 'true'}
        profile.devices = {'root': {'type': 'disk', 'path': '/'}}
        self.client.profiles.get.return_value = profile
        self.client.instances.get.return_value.status = 'Running'
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.9.0-other',
            destination_server_version='7.2')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaisesRegex(
            exception.MigrationPreCheckError, 'kernel version',
            incus_driver.check_can_live_migrate_source,
            ctx, instance, data, {'block_device_mapping': []})

    @mock.patch.object(driver.IncusDriver, 'attach_volume')
    @mock.patch.object(driver, '_remove_stale_live_migration_profile')
    def test_pre_live_migration_creates_profile_and_attaches_data_volume(
            self, remove_stale_profile, attach_volume):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2',
            source_profile=jsonutils.dumps({
                'config': {
                    'migration.stateful': 'true',
                    'security.idmap.base': '1065536',
                },
                'devices': {'root': {'type': 'disk', 'path': '/'}},
            }))
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        connection_info = {
            'serial': 'volume-id',
            'driver_volume_type': 'rbd',
            'data': {},
        }

        result = incus_driver.pre_live_migration(
            ctx, instance, {'block_device_mapping': [{
                'boot_index': None,
                'mount_device': '/dev/vdb',
                'connection_info': connection_info,
            }]}, [mock.sentinel.vif], None, data)

        self.assertIs(data, result)
        remove_stale_profile.assert_called_once_with(
            self.client, instance)
        self.vif_driver.plug.assert_called_once_with(
            instance, mock.sentinel.vif)
        self.client.profiles.create.assert_called_once_with(
            instance.name,
            {
                'migration.stateful': 'true',
                'security.idmap.base': '1065536',
            },
            {'root': {'type': 'disk', 'path': '/'}})
        attach_volume.assert_called_once_with(
            ctx, connection_info, instance, '/dev/vdb')

    @mock.patch.object(driver.os.path, 'ismount', return_value=True)
    @mock.patch.object(driver.os, 'chmod')
    def test_ensure_share_mount_path_does_not_chmod_mounted_export(
            self, chmod, ismount):
        driver.fileutils.ensure_tree.reset_mock()
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001')
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001')

        mount_path = driver._ensure_share_mount_path(instance, share)

        share_root = os.path.join(
            self.CONF.instances_path, 'incus-shares')
        instance_root = os.path.join(share_root, instance.uuid)
        self.assertEqual(
            driver._share_mount_path(instance, share), mount_path)
        self.assertEqual([
            mock.call(share_root, 0o711),
            mock.call(instance_root, 0o711),
        ], chmod.call_args_list)

    @mock.patch.object(driver.IncusDriver, 'mount_share')
    @mock.patch.object(driver, '_remove_stale_live_migration_profile')
    def test_pre_live_migration_leaves_manila_mount_to_manager(
            self, remove_stale_profile, mount_share):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2',
            source_profile=jsonutils.dumps({
                'config': {'migration.stateful': 'true'},
                'devices': {
                    'root': {'type': 'disk', 'path': '/'},
                    'manila-10000000-0000-0000-0000-000000000001': {
                        'type': 'disk',
                        'source': '/var/lib/nova/incus-shares/share',
                        'path': '/mnt/manila/project-data',
                    },
                },
            }))
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.pre_live_migration(
            ctx, instance, {'block_device_mapping': []},
            [], None, data)

        mount_share.assert_not_called()

    @mock.patch.object(driver.eventlet, 'sleep')
    @mock.patch.object(driver.os.path, 'ismount', return_value=True)
    @mock.patch.object(driver, '_remove_stale_live_migration_profile')
    def test_pre_live_migration_retries_manila_mount_propagation(
            self, remove_stale_profile, ismount, sleep):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        share_source = '/var/lib/nova/incus-shares/instance/share'
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2',
            source_profile=jsonutils.dumps({
                'config': {'migration.stateful': 'true'},
                'devices': {
                    'root': {'type': 'disk', 'path': '/'},
                    'manila-10000000-0000-0000-0000-000000000001': {
                        'type': 'disk',
                        'source': share_source,
                        'path': '/mnt/manila/project-data',
                        'recursive': 'true',
                    },
                },
            }))
        response = mock.Mock(status_code=400)
        response.json.return_value = {
            'error': 'The recursive option is only supported for additional '
                     'bind-mounted paths'}
        self.client.profiles.create.side_effect = [
            incuscore_exceptions.LXDAPIException(response), None]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        result = incus_driver.pre_live_migration(
            ctx, instance, {'block_device_mapping': []},
            [], None, data)

        self.assertIs(data, result)
        self.assertEqual(2, self.client.profiles.create.call_count)
        ismount.assert_called_once_with(share_source)
        sleep.assert_called_once_with(
            self.CONF.incus.migration_finish_retry_interval)

    def test_remove_stale_live_migration_profile(self):
        instance = mock.Mock(name='instance')
        instance.name = 'instance-00000001'
        self.client.instances.get.side_effect = incuscore_exceptions.NotFound(
            MockResponse(404))
        profile = self.client.profiles.get.return_value
        profile.config = {'environment.product_name': 'OpenStack Nova'}
        profile.used_by = []

        driver._remove_stale_live_migration_profile(
            self.client, instance)

        profile.delete.assert_called_once_with()

    def test_remove_stale_live_migration_profile_rejects_live_target(self):
        instance = mock.Mock(name='instance')
        instance.name = 'instance-00000001'

        self.assertRaises(
            exception.DestinationDiskExists,
            driver._remove_stale_live_migration_profile,
            self.client, instance)

        self.client.profiles.get.assert_not_called()

    @mock.patch.object(driver, '_require_bfv_live_migration_support')
    @mock.patch.object(driver.IncusDriver, 'attach_volume')
    @mock.patch.object(driver, '_remove_stale_live_migration_profile')
    def test_pre_live_migration_leaves_bfv_root_to_cephext(
            self, remove_stale_profile, attach_volume, require_bfv):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2',
            source_profile=jsonutils.dumps({
                'config': {'migration.stateful': 'true'},
                'devices': {
                    'root': {
                        'type': 'disk',
                        'path': '/',
                        'pool': 'cinder',
                        'initial.ceph.rbd.image_name': 'volume-root',
                    },
                },
            }))
        root_bdm = {
            'boot_index': 0,
            'mount_device': '/dev/vda',
            'connection_info': {'serial': 'root-volume'},
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        result = incus_driver.pre_live_migration(
            ctx, instance, {'block_device_mapping': [root_bdm]},
            [], None, data)

        self.assertIs(data, result)
        require_bfv.assert_called_once_with(self.client, root_bdm)
        attach_volume.assert_not_called()

    @mock.patch.object(driver.IncusDriver, 'unplug_vifs')
    @mock.patch.object(driver.IncusDriver, 'attach_volume')
    @mock.patch.object(driver, '_remove_stale_live_migration_profile')
    def test_pre_live_migration_failure_removes_profile_and_network(
            self, remove_stale_profile, attach_volume, unplug_vifs):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2',
            source_profile=jsonutils.dumps({
                'config': {'migration.stateful': 'true'},
                'devices': {'root': {'type': 'disk', 'path': '/'}},
            }))
        connection_info = {
            'serial': 'volume-id',
            'driver_volume_type': 'rbd',
            'data': {},
        }
        attach_volume.side_effect = RuntimeError('connect failed')
        profile = self.client.profiles.get.return_value
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaisesRegex(
            RuntimeError, 'connect failed',
            incus_driver.pre_live_migration,
            ctx, instance, {'block_device_mapping': [{
                'boot_index': None,
                'mount_device': '/dev/vdb',
                'connection_info': connection_info,
            }]}, [mock.sentinel.vif], None, data)

        profile.delete.assert_called_once_with()
        unplug_vifs.assert_called_once_with(
            instance, [mock.sentinel.vif])
        self.assertIs(
            True,
            connection_info['data'][driver._PRE_LIVE_DISCONNECTED_KEY])

    def test_detach_volume_skips_pre_live_double_disconnect(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        connection_info = {
            'serial': 'volume-id',
            'driver_volume_type': 'rbd',
            'data': {driver._PRE_LIVE_DISCONNECTED_KEY: True},
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.detach_volume(
            ctx, connection_info, instance, '/dev/vdb')

        self.client.profiles.get.assert_not_called()
        self.assertNotIn(
            driver._PRE_LIVE_DISCONNECTED_KEY, connection_info['data'])

    def test_migration_operation_url_uses_public_endpoint(self):
        result = driver._migration_operation_url(
            'http+unix://incus/1.0/operations/op',
            'https://192.0.2.10:8443')

        self.assertEqual(
            'https://192.0.2.10:8443/1.0/operations/op',
            result)

    def test_migration_operation_url_drops_project_query(self):
        result = driver._migration_operation_url(
            'http+unix://incus/1.0/operations/op?target=node-1&'
            'project=default',
            'https://192.0.2.10:8443')

        self.assertEqual(
            'https://192.0.2.10:8443/1.0/operations/op',
            result)

    def test_delete_migration_target_resource_waits_for_operation(self):
        remote = mock.MagicMock()
        response = (
            remote.api.instances['test'].delete.return_value)
        response.json.return_value = {
            'operation': '/1.0/operations/delete-op'}

        driver._delete_migration_target_resource(
            remote, 'instances', 'test', 'nova', wait=True)

        remote.instances.get.assert_called_once_with('test')
        remote.api.instances['test'].delete.assert_called_once_with(
            params={'project': 'nova'})
        remote.operations.wait_for_operation.assert_called_once_with(
            '/1.0/operations/delete-op')

    def test_delete_migration_target_profile_does_not_wait(self):
        remote = mock.MagicMock()

        driver._delete_migration_target_resource(
            remote, 'profiles', 'test', 'nova')

        remote.profiles.get.assert_called_once_with('test')
        remote.api.profiles['test'].delete.assert_called_once_with(
            params={'project': 'nova'})
        remote.operations.wait_for_operation.assert_not_called()

    @mock.patch('nova.virt.incus.driver._migration_client')
    def test_live_migration_restores_target_then_calls_post(self, get_remote):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        self.CONF.incus.migration_address = 'https://192.0.2.10:8443'
        self.CONF.incus.project = 'nova'
        container = mock.Mock()
        container.status = 'Stopped'
        container.config = {'volatile.idmap.base': '1065536'}
        container.generate_migration_data.return_value = {
            'default': ['test'],
            'source': {
                'operation': (
                    'http+unix://incus/1.0/operations/op'),
            },
        }
        self.client.instances.get.return_value = container
        profile = mock.Mock()
        profile.config = {'migration.stateful': 'true'}
        profile.devices = {'root': {'type': 'disk', 'path': '/'}}
        self.client.profiles.get.return_value = profile
        remote = get_remote.return_value
        remote.profiles.get.side_effect = incuscore_exceptions.NotFound(
            MockResponse(404))
        post = mock.Mock()
        recover = mock.Mock()
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.live_migration(
            ctx, instance, 'destination', post, recover,
            migrate_data=data)

        payload = remote.instances.create.call_args.args[0]
        remote.profiles.create.assert_called_once_with(
            instance.name,
            {
                'migration.stateful': 'true',
                'security.idmap.base': '1065536',
            },
            {'root': {'type': 'disk', 'path': '/'}})
        self.assertEqual([instance.name], payload['profiles'])
        self.assertNotIn('default', payload)
        self.assertIs(True, payload['source']['live'])
        self.assertEqual(
            'https://192.0.2.10:8443/1.0/operations/op',
            payload['source']['operation'])
        remote.instances.create.assert_called_once_with(payload, wait=True)
        post.assert_called_once_with(
            ctx, instance, 'destination', False, data)
        recover.assert_not_called()

    def test_stateful_migration_profile_config_requires_idmap_base(self):
        container = mock.Mock(config={})
        profile = mock.Mock(config={'migration.stateful': 'true'})

        self.assertRaises(
            exception.MigrationPreCheckError,
            driver._stateful_migration_profile_config,
            container,
            profile)

    @mock.patch('nova.virt.incus.driver._migration_client')
    def test_live_migration_failure_preserves_target_for_nova_rollback(
            self, get_remote):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        self.CONF.incus.migration_address = 'https://192.0.2.10:8443'
        container = mock.Mock()
        container.config = {'volatile.idmap.base': '1065536'}
        container.generate_migration_data.return_value = {
            'source': {
                'operation': 'http+unix://incus/1.0/operations/op',
            },
        }
        self.client.instances.get.return_value = container
        profile = mock.Mock()
        profile.config = {'migration.stateful': 'true'}
        profile.devices = {'root': {'type': 'disk', 'path': '/'}}
        self.client.profiles.get.return_value = profile
        remote = get_remote.return_value
        remote.profiles.get.side_effect = incuscore_exceptions.NotFound(
            MockResponse(404))
        remote.instances.create.side_effect = RuntimeError('restore failed')
        post = mock.Mock()
        recover = mock.Mock()
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.live_migration(
            ctx, instance, 'destination', post, recover,
            migrate_data=data)

        recover.assert_called_once_with(
            ctx, instance, 'destination', data)
        post.assert_not_called()

    @mock.patch('nova.virt.incus.driver._migration_client')
    def test_live_migration_failure_preserves_precreated_target_profile(
            self, get_remote):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        self.CONF.incus.migration_address = 'https://192.0.2.10:8443'
        container = mock.Mock()
        container.config = {'volatile.idmap.base': '1065536'}
        container.generate_migration_data.return_value = {
            'source': {
                'operation': 'http+unix://incus/1.0/operations/op',
            },
        }
        self.client.instances.get.return_value = container
        remote = get_remote.return_value
        remote.instances.create.side_effect = RuntimeError('restore failed')
        post = mock.Mock()
        recover = mock.Mock()
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.live_migration(
            ctx, instance, 'destination', post, recover,
            migrate_data=data)

        remote.profiles.create.assert_not_called()
        recover.assert_called_once_with(
            ctx, instance, 'destination', data)
        post.assert_not_called()

    def test_rollback_live_migration_source_start_failure_is_nonfatal(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        container = mock.Mock(status='Stopped')
        container.start.side_effect = RuntimeError('restore failed')
        self.client.instances.get.return_value = container
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.rollback_live_migration_at_source(
            ctx, instance, migrate_data.IncusLiveMigrateData())

        container.start.assert_called_once_with(wait=True)

    def test_rollback_live_migration_source_waits_for_criu_restore(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        container = mock.Mock(status='Stopped')

        def sync():
            if container.sync.call_count == 2:
                container.status = 'Running'

        container.sync.side_effect = sync
        self.client.instances.get.return_value = container
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.rollback_live_migration_at_source(
            ctx, instance, migrate_data.IncusLiveMigrateData())

        self.assertEqual(2, container.sync.call_count)
        container.start.assert_not_called()

    @mock.patch('nova.virt.incus.driver._migration_client')
    def test_finalize_live_migration_rollback_reasserts_original_vifs(
            self, get_remote):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        source_vif = network_model.VIF(id='test-vif')
        vif_data = nova_migrate_data.VIFMigrateData(source_vif=source_vif)
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            vifs=[vif_data])
        get_remote.return_value.profiles.get.side_effect = [
            mock.sentinel.profile,
            incuscore_exceptions.NotFound(MockResponse(404)),
        ]
        inactive_vif = network_model.VIF(id='test-vif', active=False)
        active_vif = network_model.VIF(id='test-vif', active=True)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.network_api.get_instance_nw_info = mock.Mock(
            side_effect=[
                network_model.NetworkInfo([inactive_vif]),
                network_model.NetworkInfo([active_vif]),
            ])
        incus_driver.vif_driver.reassert = mock.Mock()
        incus_driver.unplug_vifs = mock.Mock()

        incus_driver.finalize_live_migration_rollback(
            ctx, instance, data)

        incus_driver.vif_driver.reassert.assert_called_once_with(
            instance, source_vif)
        incus_driver.unplug_vifs.assert_not_called()
        self.assertEqual(
            2, get_remote.return_value.profiles.get.call_count)

    @mock.patch.object(driver, '_cleanup_profile_share_mounts')
    def test_rollback_live_migration_destination_cleans_profile_last(
            self, cleanup_shares):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        profile = self.client.profiles.get.return_value
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.cleanup = mock.Mock()

        incus_driver.rollback_live_migration_at_destination(
            ctx, instance, mock.sentinel.network_info,
            {'block_device_mapping': []}, destroy_disks=False)

        cleanup_shares.assert_called_once_with(profile, instance)
        incus_driver.cleanup.assert_called_once_with(
            ctx, instance, mock.sentinel.network_info,
            destroy_disks=False, destroy_vifs=True)
        profile.delete.assert_not_called()

    def test_confirm_migration(self):
        ctx = context.get_admin_context()
        migration = mock.Mock(
            source_compute='compute', dest_compute='compute')
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = []
        profile = mock.Mock()
        container = mock.Mock()
        container.status = 'Stopped'
        self.client.profiles.get.return_value = profile
        self.client.instances.get.return_value = container

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.confirm_migration(
            ctx, migration, instance, network_info)

        profile.delete.assert_called_once_with()
        container.delete.assert_called_once_with(wait=True)

    def test_post_live_migration(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        container = mock.Mock()
        container.status = 'Stopped'
        self.client.instances.get.return_value = container

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.post_live_migration(context, instance, None)

        container.stop.assert_not_called()
        container.delete.assert_called_once_with(wait=True)

    def test_post_live_migration_force_stops_running_source(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        container = mock.Mock(status='Running')
        self.client.instances.get.return_value = container

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.post_live_migration(ctx, instance, None)

        container.stop.assert_called_once_with(
            timeout=-1, force=True, wait=True)
        container.delete.assert_called_once_with(wait=True)

    def test_post_live_migration_accepts_concurrent_source_stop(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        response = mock.Mock(status_code=400)
        response.json.return_value = {
            'error': 'The instance is already stopped'}
        container = mock.Mock(status='Running')
        container.stop.side_effect = incuscore_exceptions.LXDAPIException(
            response)
        self.client.instances.get.return_value = container

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.post_live_migration(ctx, instance, None)

        container.stop.assert_called_once_with(
            timeout=-1, force=True, wait=True)
        container.delete.assert_called_once_with(wait=True)

    def test_post_live_migration_rejects_other_source_stop_error(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        response = mock.Mock(status_code=500)
        response.json.return_value = {'error': 'storage unavailable'}
        container = mock.Mock(status='Running')
        container.stop.side_effect = incuscore_exceptions.LXDAPIException(
            response)
        self.client.instances.get.return_value = container

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            incuscore_exceptions.LXDAPIException,
            incus_driver.post_live_migration, ctx, instance, None)

        container.delete.assert_not_called()

    def test_post_live_migration_disconnects_source_data_volumes(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        container = mock.Mock(status='Stopped')
        self.client.instances.get.return_value = container
        root_connection = {
            'driver_volume_type': 'rbd',
            'serial': 'root-volume',
            'data': {'volume_id': 'root-volume'},
        }
        data_connection = {
            'driver_volume_type': 'rbd',
            'serial': 'data-volume',
            'data': {'volume_id': 'data-volume'},
        }
        block_device_info = {'block_device_mapping': [
            {
                'boot_index': 0,
                'connection_info': root_connection,
                'mount_device': '/dev/sda',
            },
            {
                'boot_index': None,
                'connection_info': data_connection,
                'mount_device': '/dev/sdb',
            },
        ]}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.detach_volume = mock.Mock()

        incus_driver.post_live_migration(
            ctx, instance, block_device_info)

        container.delete.assert_called_once_with(wait=True)
        incus_driver.detach_volume.assert_called_once_with(
            ctx, data_connection, instance, '/dev/sdb')

    def test_post_live_migration_at_source(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = []
        profile = mock.Mock()
        self.client.profiles.get.return_value = profile

        incus_driver = driver.IncusDriver(None)
        incus_driver.cleanup = mock.Mock()
        incus_driver.init_host(None)

        incus_driver.post_live_migration_at_source(
            ctx, instance, network_info)

        incus_driver.cleanup.assert_called_once_with(
            ctx, instance, network_info)
