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
import gzip
import copy
from contextlib import closing
from contextlib import contextmanager
import dataclasses
import errno
import hashlib
import inspect
import io
import os
import re
import shutil
import stat
import tarfile
import tempfile
import threading
import time
import uuid

from oslo_serialization import jsonutils
from oslo_utils import timeutils
from oslo_utils import units
from unittest import mock
from nova import block_device
from nova import context
from nova import exception
from nova import objects
from nova import test
from nova.compute import manager
from nova.compute import power_state
from nova.compute import task_states
from nova.compute import vm_states
from nova.conductor import manager as conductor_manager
from nova.network import model as network_model
from nova.objects import migrate_data as nova_migrate_data
from nova.tests.unit import fake_block_device
from nova.tests.unit import fake_instance
from nova.virt import block_device as driver_block_device
from nova.virt import driver as nova_driver
from pylxd import client as incus_client
from pylxd import exceptions as incuscore_exceptions
import six
import yaml

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

_TEST_VOLUME_ID = '8231d2e8-1111-4222-8333-123456789abc'
_TEST_VOLUME_ID_2 = '9231d2e8-1111-4222-8333-123456789abc'
_TEST_VOLUME_ID_3 = 'a231d2e8-1111-4222-8333-123456789abc'


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


def migration_disk_info(data):
    data.setdefault(
        'cleanup_token', '10000000-0000-0000-0000-000000000001')
    data.setdefault('idmap_base', 1065536)
    data.setdefault('idmap_size', 65536)
    return jsonutils.dumps(data)


def fake_connection_info(volume, location, iqn, auth=False, transport=None):
    """Return a recoverable RBD connection for attach transaction tests."""
    dev_name = 'ip-%s-rbd-%s' % (location, iqn)
    if transport is not None:
        dev_name = 'pci-0000:00:00.0-' + dev_name
    dev_path = '/dev/disk/by-path/%s' % (dev_name)
    volume_id = {
        '1': _TEST_VOLUME_ID,
        '2': _TEST_VOLUME_ID_2,
        '3': _TEST_VOLUME_ID_3,
    }.get(str(volume['id']), str(volume['id']))
    ret = {
        'driver_volume_type': 'rbd',
        'serial': volume_id,
        'data': {
            'volume_id': volume_id,
            'name': 'volumes/volume-%s' % volume_id,
            'device_path': dev_path,
            'access_mode': 'rw',
        }
    }
    if auth:
        ret['data']['auth_method'] = 'CHAP'
        ret['data']['auth_username'] = 'foo'
        ret['data']['auth_password'] = 'bar'
    return ret


def real_volume_driver_bdm(
        ctxt, volume_id, mount_device, boot_index, connection_info):
    """Build the DriverVolumeBlockDevice shape Nova passes to spawn()."""
    mapping = block_device.BlockDeviceDict({
        'instance_uuid': '00000000-0000-4000-8000-000000000001',
        'device_name': mount_device,
        'source_type': 'volume',
        'destination_type': 'volume',
        'volume_id': volume_id,
        'volume_size': 1,
        'connection_info': jsonutils.dumps(connection_info),
        'attachment_id': volume_id,
        'delete_on_termination': False,
        'boot_index': boot_index,
    })
    return driver_block_device.DriverVolumeBlockDevice(
        fake_block_device.fake_bdm_object(ctxt, mapping))


class VolumeConnectionInfoTest(test.NoDBTestCase):

    @mock.patch.object(driver, '_validate_block_device_path',
                       side_effect=lambda path, label: path)
    @mock.patch.object(driver.processutils, 'execute')
    def test_mapped_rbd_device_reuses_index_for_same_connector(
            self, execute, validate_path):
        execute.return_value = (jsonutils.dumps({
            '0': {
                'pool': 'volumes',
                'name': 'volume-one',
                'namespace': '',
                'device': '/dev/rbd0',
            },
            '1': {
                'pool': 'volumes',
                'name': 'volume-two',
                'namespace': '',
                'device': '/dev/rbd1',
            },
        }), '')
        cache = {}

        first = driver._mapped_rbd_device(
            {'name': 'volumes/volume-one'}, mapping_cache=cache)
        second = driver._mapped_rbd_device(
            {'name': 'volumes/volume-two'}, mapping_cache=cache)

        self.assertEqual('/dev/rbd0', first)
        self.assertEqual('/dev/rbd1', second)
        execute.assert_called_once_with(
            'rbd', 'showmapped', '--format=json')

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
        self.assertEqual(
            power_state.CRASHED, driver._get_power_state(108))
        self.assertEqual(
            power_state.CRASHED, driver._get_power_state(112))

    def test_freeze_transitions_have_no_stable_state(self):
        # Freezing and Ready are mid-transition; Nova has no value for
        # that, so NOSTATE is the honest answer.
        self.assertEqual(
            power_state.NOSTATE, driver._get_power_state(109))
        self.assertEqual(
            power_state.NOSTATE, driver._get_power_state(113))

    def test_thawed_is_running_again(self):
        """Thawed is a settled state, not a transition.

        The guest resumed after a freeze. Calling it NOSTATE made every
        unpause look to Nova's power-state sync like an instance whose
        state had been lost.
        """
        self.assertEqual(
            power_state.RUNNING, driver._get_power_state(111))

    def test_frozen_is_paused(self):
        self.assertEqual(
            power_state.PAUSED, driver._get_power_state(110))

    def test_unknown_code_is_reported_not_raised(self):
        # This feeds get_info and so Nova's periodic power-state sync; a
        # code from a newer Incus must not break that instance until the
        # driver catches up.
        with mock.patch.object(driver.LOG, 'warning') as warning:
            self.assertEqual(
                power_state.NOSTATE, driver._get_power_state(69))

        warning.assert_called_once()


class MigrationAttemptProtocolTest(test.NoDBTestCase):

    def setUp(self):
        super().setUp()
        self.instances_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.instances_dir.cleanup)
        self.conf_patcher = mock.patch.object(driver, 'CONF')
        self.conf = self.conf_patcher.start()
        self.addCleanup(self.conf_patcher.stop)
        self.conf.incus.project = 'nova'
        self.conf.incus.migration_finish_retries = 3
        self.conf.incus.migration_finish_retry_interval = 0
        self.conf.instances_path = self.instances_dir.name
        self.instance = mock.Mock(
            name='instance-test',
            uuid='00000000-0000-0000-0000-000000000001')
        self.instance.name = 'instance-test'
        self.token = '10000000-0000-0000-0000-000000000001'
        self.client = mock.Mock()
        self.client.instances.get.return_value.config = {
            'security.idmap.base': '500000000',
            'security.idmap.size': '65536',
        }
        self.client.instances.get.return_value.expanded_config = {}
        self.client.host_info = {
            'api_extensions': ['migration_attempt_fencing']}
        self.client.api = mock.MagicMock()
        collection = self.client.api['migration-attempts']
        self.endpoint = collection[self.token]

    def _attempt(self, state='active', finished=False, **overrides):
        metadata = {
            'token': self.token,
            'project': 'nova',
            'resource_type': 'instance',
            'resource_name': self.instance.name,
            'state': state,
            'finished': finished,
            'operation_uuid': '',
            'idmap_base': 1065536,
            'idmap_size': 65536,
        }
        metadata.update(overrides)
        response = mock.Mock()
        response.json.return_value = {'metadata': metadata}
        return response

    def test_register_attempt_binds_name_project_and_idmap(self):
        self.endpoint.put.return_value = self._attempt()

        attempt = driver._register_migration_attempt(
            self.client, self.instance, self.token, 1065536, 65536)

        self.assertEqual('active', attempt['state'])
        self.endpoint.put.assert_called_once_with(
            params={'project': 'nova'},
            json={
                'state': 'active',
                'resource_type': 'instance',
                'resource_name': self.instance.name,
                'idmap_base': 1065536,
                'idmap_size': 65536,
            })

    def test_register_attempt_rejects_idmap_mismatch(self):
        self.endpoint.put.return_value = self._attempt(idmap_size=131072)

        self.assertRaises(
            exception.MigrationError,
            driver._register_migration_attempt,
            self.client, self.instance, self.token, 1065536, 65536)

    def test_instance_owner_uses_expanded_profile_config(self):
        container = mock.Mock(
            expanded_config={
                'user.openstack.uuid': self.instance.uuid,
            },
            config={})

        self.assertEqual(
            self.instance.uuid, driver._instance_nova_uuid(container))

    def test_instance_owner_rejects_conflicting_config(self):
        container = mock.Mock(
            expanded_config={
                'user.openstack.uuid': self.instance.uuid,
            },
            config={
                'user.openstack.uuid':
                    '00000000-0000-0000-0000-000000000002',
            })

        self.assertIsNone(driver._instance_nova_uuid(container))

    def test_abort_fences_before_operation_settlement(self):
        self.endpoint.put.return_value = self._attempt(
            state='aborted', finished=True)
        events = []
        self.endpoint.put.side_effect = lambda **kwargs: (
            events.append('abort') or self._attempt(
                state='aborted', finished=True))

        with mock.patch.object(
                driver, '_settle_instance_migration_operations',
                side_effect=lambda *args, **kwargs: events.append('settle')):
            with mock.patch.object(
                    driver, '_wait_migration_attempt_finished',
                    side_effect=lambda *args, **kwargs: (
                        events.append('wait') or {
                            'state': 'aborted', 'finished': True})):
                result = driver._abort_migration_attempt(
                    self.client, self.instance, self.token, 1065536, 65536)

        self.assertEqual(['abort', 'settle', 'wait'], events)
        self.assertEqual('aborted', result['state'])

    def test_create_recovers_committed_attempt_after_lost_response(self):
        active = {
            'state': 'active', 'finished': False, 'operation_uuid': ''}
        committed = {
            'state': 'committed', 'finished': True,
            'operation_uuid': '20000000-0000-0000-0000-000000000002'}
        container = mock.sentinel.container
        self.client.api.instances.post.side_effect = (
            incuscore_exceptions.ClientConnectionFailed('lost response'))
        request = {
            'name': self.instance.name,
            'source': {'type': 'migration'},
        }

        with mock.patch.object(
                driver, '_get_migration_attempt',
                side_effect=[active, committed]):
            with mock.patch.object(
                    driver, '_migration_attempt_instance',
                    return_value=container):
                result, operation_id = driver._create_migration_target(
                    self.client, request, self.instance, self.token,
                    1065536, 65536)

        self.assertIs(container, result)
        self.assertEqual(
            '20000000-0000-0000-0000-000000000002', operation_id)
        sent = self.client.api.instances.post.call_args.kwargs['json']
        self.assertEqual(
            self.token, sent['source']['migration_attempt'])
        self.assertNotIn('migration_attempt', request['source'])

    def test_side_effect_retry_rejects_generic_runtime_error(self):
        action = mock.Mock(side_effect=RuntimeError('bad configuration'))

        self.assertRaises(
            RuntimeError, driver._retry_migration_finish_action,
            action, 'configuration write', self.instance)

        action.assert_called_once_with()

    def test_side_effect_retry_accepts_connection_failure(self):
        action = mock.Mock(side_effect=[
            incuscore_exceptions.ClientConnectionFailed('disconnected'),
            mock.sentinel.result,
        ])

        result = driver._retry_migration_finish_action(
            action, 'API write', self.instance)

        self.assertIs(mock.sentinel.result, result)
        self.assertEqual(2, action.call_count)

    def test_publishes_target_volume_completion_after_local_evidence_gone(
            self):
        migration_uuid = '20000000-0000-0000-0000-000000000002'
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': self.instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: self.token,
            driver.MIGRATION_NOVA_UUID_KEY: migration_uuid,
        }

        self.assertTrue(driver._publish_migration_target_volumes_complete(
            self.client, self.instance, self.token, migration_uuid))

        self.assertEqual(
            self.token,
            profile.config[driver.MIGRATION_TARGET_VOLUMES_COMPLETE_KEY])
        profile.save.assert_called_once_with(wait=True)

    def test_target_volume_completion_waits_for_local_evidence(self):
        migration_uuid = '20000000-0000-0000-0000-000000000002'
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': self.instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: self.token,
            driver.MIGRATION_NOVA_UUID_KEY: migration_uuid,
        }
        journal_dir = driver._volume_journal_directory(self.instance)
        os.makedirs(journal_dir)
        with open(
                os.path.join(journal_dir, 'pending.attach-intent'), 'w',
                encoding='utf-8'):
            pass

        self.assertFalse(driver._publish_migration_target_volumes_complete(
            self.client, self.instance, self.token, migration_uuid))

        profile.save.assert_not_called()

    def test_finalize_marker_save_failure_remains_retryable(self):
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': self.instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: self.token,
            driver.MIGRATION_DESTINATION_PREPARED_KEY: self.token,
            driver.MIGRATION_TARGET_VOLUMES_COMPLETE_KEY: self.token,
        }
        profile.save.side_effect = RuntimeError('database unavailable')
        attempt = {'state': 'committed', 'finished': True}

        with mock.patch.object(
                driver, '_get_migration_attempt', return_value=attempt):
            with mock.patch.object(
                    driver, '_retire_migration_attempt') as retire:
                self.assertRaises(
                    RuntimeError,
                    driver._finalize_committed_migration_attempt,
                    self.client, self.instance, self.token,
                    1065536, 65536)

        self.assertEqual(
            self.token,
            profile.config[driver.MIGRATION_DESTINATION_PREPARED_KEY])
        retire.assert_not_called()

    def test_finalize_committed_attempt_requires_target_volume_proof(self):
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': self.instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: self.token,
            driver.MIGRATION_DESTINATION_PREPARED_KEY: self.token,
        }
        journal_dir = driver._volume_journal_directory(self.instance)
        os.makedirs(journal_dir)
        with open(
                os.path.join(journal_dir, 'pending.attach-intent'), 'w',
                encoding='utf-8'):
            pass
        attempt = {'state': 'committed', 'finished': True}

        with mock.patch.object(
                driver, '_get_migration_attempt', return_value=attempt):
            with mock.patch.object(
                    driver, '_retire_migration_attempt') as retire:
                self.assertRaises(
                    exception.MigrationError,
                    driver._finalize_committed_migration_attempt,
                    self.client, self.instance, self.token,
                    1065536, 65536)

        self.assertEqual(
            self.token, profile.config[driver.MIGRATION_CLEANUP_TOKEN_KEY])
        profile.save.assert_not_called()
        retire.assert_not_called()

    def test_finalize_remote_attempt_ignores_source_volume_journal(self):
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': self.instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: self.token,
            driver.MIGRATION_DESTINATION_PREPARED_KEY: self.token,
            driver.MIGRATION_TARGET_VOLUMES_COMPLETE_KEY: self.token,
        }
        journal_dir = driver._volume_journal_directory(self.instance)
        os.makedirs(journal_dir)
        with open(
                os.path.join(journal_dir, 'source-release.attach-intent'),
                'w', encoding='utf-8'):
            pass
        attempt = {'state': 'committed', 'finished': True}

        with mock.patch.object(
                driver, '_get_migration_attempt', return_value=attempt):
            with mock.patch.object(
                    driver, '_retire_migration_attempt') as retire:
                driver._finalize_committed_migration_attempt(
                    self.client, self.instance, self.token, 1065536, 65536)

        profile.save.assert_called_once_with(wait=True)
        retire.assert_called_once_with(
            self.client, self.instance, self.token, 1065536, 65536)


class IncusIDMapDriverTest(test.NoDBTestCase):

    def setUp(self):
        super().setUp()
        self.instances_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.instances_dir.cleanup)
        self.flags(instances_path=self.instances_dir.name)
        self.flags(state_path=self.instances_dir.name)
        lock_path = os.path.join(self.instances_dir.name, 'locks')
        os.makedirs(lock_path)
        self.flags(lock_path=lock_path, group='oslo_concurrency')
        self.flags(project='nova', group='incus')
        self.driver = driver.IncusDriver(None)
        self.driver.idmap_allocator = mock.Mock(
            spec=driver.incus_idmap.IDMapAllocator)
        self.host_id = '20000000-0000-0000-0000-000000000002'
        node_uuid = mock.patch.object(
            driver.virt_node, 'read_local_node_uuid',
            return_value=self.host_id)
        node_uuid.start()
        self.addCleanup(node_uuid.stop)
        self.assignment = driver.incus_idmap.IDMapAssignment(
            instance_uuid='00000000-0000-0000-0000-000000000001',
            base=500000000, size=65536, slot=0,
            allocation_id='10000000-0000-0000-0000-000000000001',
            fingerprint='a' * 64, host_ids=(self.host_id,))
        self.materialization_id = (
            '30000000-0000-0000-0000-000000000003')
        self.claim = self._claim(state='committed')
        self.driver.idmap_allocator.get.return_value = self.assignment
        self.driver.idmap_allocator.claim.return_value = self.assignment
        self.driver.idmap_allocator.get_host_claim.return_value = self.claim
        self.driver.idmap_allocator.assert_startable.return_value = (
            self.assignment)
        self.driver.idmap_allocator.get_release_intent.return_value = None
        self.driver.client = mock.Mock()
        self.driver.inventory_client = self.driver.client
        self.driver.client.api = mock.MagicMock()
        self.driver.storage_ownership = mock.Mock(
            spec=driver.incus_storage_protocol.StorageOwnershipClient)
        self._set_all_project_inventory()
        self.instance = mock.Mock(
            uuid=self.assignment.instance_uuid, name='instance-00000001',
            system_metadata={})
        self.instance.name = 'instance-00000001'

    def _claim(self, state='committed', proof=None):
        return driver.incus_idmap.IDMapHostClaim(
            host_id=self.host_id,
            materialization_id=self.materialization_id,
            instance_uuid=self.assignment.instance_uuid,
            base=self.assignment.base,
            size=self.assignment.size,
            slot=self.assignment.slot,
            allocation_id=self.assignment.allocation_id,
            fingerprint=self.assignment.fingerprint,
            state=state,
            proof=proof)

    def _binding(self, disposition='delete', storage_driver='ceph'):
        return driver.incus_storage_protocol.StorageMaterializationBinding(
            token=self.materialization_id,
            allocation_id=self.assignment.allocation_id,
            compute_id=self.host_id,
            owner=self.instance.uuid,
            project='nova',
            instance_name=self.instance.name,
            idmap_base=self.assignment.base,
            idmap_size=self.assignment.size,
            storage_driver=storage_driver,
            storage_pool='root-pool',
            storage_volume='nova_instance-00000001',
            cleanup_disposition=disposition)

    def _materialization(self, state='unmaterialized'):
        claim = self._claim(state=state)
        return driver._IDMapMaterialization(
            assignment=self.assignment,
            claim=claim,
            binding=self._binding(),
            client=self.driver.client)

    def _attempt(self, state='committed', finished=True):
        return driver.incus_storage_protocol.StorageMaterializationAttempt(
            binding=self._binding(),
            storage_identity='rbd-image-id',
            baseline_clean=True,
            state=state,
            storage_phase=(
                'materialized' if state == 'committed' else 'pending'),
            started=True,
            finished=finished,
            operation_uuid=(
                '40000000-0000-0000-0000-000000000004'),
            daemon_start=1)

    def _release_intent(self):
        return driver.incus_idmap.IDMapReleaseIntent(
            instance_uuid=self.assignment.instance_uuid,
            instance_name=self.instance.name,
            base=self.assignment.base,
            size=self.assignment.size,
            slot=self.assignment.slot,
            allocation_id=self.assignment.allocation_id,
            fingerprint=self.assignment.fingerprint)

    def _release_receipt_metadata(self, **overrides):
        values = {
            'digest': 'sha256:' + ('b' * 64),
            'token': self.assignment.allocation_id,
            'owner': self.assignment.instance_uuid,
            'project': 'nova',
            'instance_name': self.instance.name,
            'idmap_base': self.assignment.base,
            'idmap_size': self.assignment.size,
            'storage_driver': 'cephext',
            'storage_pool': 'cinder-bfv',
            'storage_volume': 'container_nova_instance-00000001',
            'storage_identity': '{"block_name_prefix":"rbd_data.",'
                                '"id":"1234"}',
            'rbd_image': 'volume-00000000-0000-0000-0000-000000000009',
            'outcome': 'normalized',
            'state': 'complete',
            'created_at': 1785542400,
            'completed_at': 1785542401,
        }
        values.update(overrides)
        return values

    def _set_all_project_inventory(self, instances=None, profiles=None):
        self.driver.client.api.instances.get.return_value.json.return_value = {
            'metadata': list(instances or [])}
        self.driver.client.api.profiles.get.return_value.json.return_value = {
            'metadata': list(profiles or [])}

    def _set_instance_idmap_metadata(self):
        self.instance.system_metadata = {
            driver.IDMAP_BASE_METADATA_KEY: str(self.assignment.base),
            driver.IDMAP_SIZE_METADATA_KEY: str(self.assignment.size),
            driver.IDMAP_ALLOCATION_METADATA_KEY:
                self.assignment.allocation_id,
            driver.IDMAP_FINGERPRINT_METADATA_KEY:
                self.assignment.fingerprint,
        }

    def _idmap_container(self):
        return mock.Mock(config={
            'user.openstack.uuid': self.instance.uuid,
            driver.IDMAP_ALLOCATION_CONFIG_KEY:
                self.assignment.allocation_id,
            driver.IDMAP_COMPUTE_CONFIG_KEY: self.host_id,
            driver.IDMAP_MATERIALIZATION_CONFIG_KEY:
                self.materialization_id,
            'security.idmap.base': str(self.assignment.base),
            'security.idmap.size': str(self.assignment.size),
        }, expanded_config={})

    def test_allocate_persists_nova_system_metadata(self):
        self.driver.idmap_allocator.allocate.return_value = self.assignment

        result = self.driver._ensure_instance_idmap(self.instance)

        self.assertEqual(self.assignment, result)
        self.assertEqual(
            {
                driver.IDMAP_BASE_METADATA_KEY: '500000000',
                driver.IDMAP_SIZE_METADATA_KEY: '65536',
                driver.IDMAP_ALLOCATION_METADATA_KEY:
                    self.assignment.allocation_id,
                driver.IDMAP_FINGERPRINT_METADATA_KEY: 'a' * 64,
            }, self.instance.system_metadata)
        self.instance.save.assert_called_once_with()
        self.driver.idmap_allocator.claim.assert_not_called()

    def test_begin_claims_and_registers_before_spawn_can_continue(self):
        token = self.materialization_id
        root = {'type': 'disk', 'path': '/', 'pool': 'root-pool'}
        unmaterialized = self._claim(state='unmaterialized')
        self.driver._ensure_instance_idmap = mock.Mock(
            return_value=self.assignment)
        self.driver.idmap_allocator.get.return_value = self.assignment
        self.driver.idmap_allocator.get_host_claim.return_value = (
            unmaterialized)
        self.driver.client.storage_pools.get.return_value = mock.Mock(
            driver='ceph')
        calls = mock.Mock()
        calls.attach_mock(self.driver.idmap_allocator.claim, 'claim')
        calls.attach_mock(
            self.driver.storage_ownership.register_materialization,
            'register')

        materialization = self.driver._begin_idmap_materialization(
            self.instance, token, root)

        self.assertEqual(self.assignment, materialization.assignment)
        self.assertEqual(unmaterialized, materialization.claim)
        self.assertEqual([
            mock.call.claim(
                self.instance.uuid, self.host_id, token,
                assignment=self.assignment),
            mock.call.register(materialization.binding),
        ], calls.mock_calls)

    def test_preflight_spawn_attempt_allows_exact_no_claim_destroy(self):
        self.driver.idmap_allocator.get.return_value = None
        self.driver.idmap_allocator.get_release_intent.return_value = None
        self.instance.system_metadata = {}
        attempt = self.driver._create_spawn_preflight_attempt(
            self.instance, self.materialization_id)

        self.assertEqual('preflight', attempt['phase'])
        self.assertTrue(self.driver._consume_spawn_preflight_noop(
            self.instance))
        self.assertIsNone(driver._read_spawn_attempt_journal(self.instance))
        self.driver.idmap_allocator.retire_claim.assert_not_called()

    def test_preflight_spawn_attempt_allows_exact_release_destroy(self):
        self._set_instance_idmap_metadata()
        assignment = dataclasses.replace(self.assignment, host_ids=())
        self.driver.idmap_allocator.get.return_value = assignment
        self.driver.idmap_allocator.get_host_claim.return_value = None
        attempt = self.driver._create_spawn_preflight_attempt(
            self.instance, self.materialization_id)
        self.driver.idmap_allocator.get_release_intent.return_value = (
            self._release_intent())

        self.assertEqual(
            attempt, driver._read_spawn_attempt_journal(self.instance))
        self.assertTrue(self.driver._consume_spawn_preflight_noop(
            self.instance))
        self.assertIsNone(driver._read_spawn_attempt_journal(self.instance))

    def test_preflight_spawn_attempt_rejects_other_release_generation(self):
        self._set_instance_idmap_metadata()
        assignment = dataclasses.replace(self.assignment, host_ids=())
        self.driver.idmap_allocator.get.return_value = assignment
        self.driver.idmap_allocator.get_host_claim.return_value = None
        attempt = self.driver._create_spawn_preflight_attempt(
            self.instance, self.materialization_id)
        self.driver.idmap_allocator.get_release_intent.return_value = (
            dataclasses.replace(
                self._release_intent(), allocation_id=str(uuid.uuid4())))

        self.assertRaises(
            driver.incus_idmap.IDMapIntegrityError,
            self.driver._consume_spawn_preflight_noop, self.instance)
        self.assertEqual(
            attempt, driver._read_spawn_attempt_journal(self.instance))

    def test_opening_spawn_attempt_without_allocator_state_is_empty(self):
        self.driver.idmap_allocator.get.return_value = None
        self.driver.idmap_allocator.get_host_claim.return_value = None
        attempt = self.driver._create_spawn_preflight_attempt(
            self.instance, self.materialization_id)
        self.driver._open_spawn_attempt(self.instance, attempt)

        self.assertTrue(self.driver._consume_spawn_preflight_noop(
            self.instance))
        self.assertIsNone(driver._read_spawn_attempt_journal(self.instance))

    def test_opening_spawn_attempt_with_partial_claim_fails_closed(self):
        self.driver.idmap_allocator.get.return_value = None
        self.driver.idmap_allocator.get_host_claim.return_value = (
            self._claim(state='unmaterialized'))
        attempt = self.driver._create_spawn_preflight_attempt(
            self.instance, self.materialization_id)
        opened = self.driver._open_spawn_attempt(self.instance, attempt)

        self.assertRaises(
            driver.incus_idmap.IDMapConflict,
            self.driver._consume_spawn_preflight_noop, self.instance)
        self.assertEqual(
            opened, driver._read_spawn_attempt_journal(self.instance))

    def test_finish_spawn_attempt_open_requires_opening_payload(self):
        self.driver.idmap_allocator.get.return_value = None
        self.driver.idmap_allocator.get_release_intent.return_value = None
        self.instance.system_metadata = {}
        preflight = self.driver._create_spawn_preflight_attempt(
            self.instance, self.materialization_id)
        opened = self.driver._open_spawn_attempt(self.instance, preflight)
        self.driver.idmap_allocator.get.return_value = self.assignment
        self.driver.idmap_allocator.get_host_claim.return_value = (
            self._claim(state='unmaterialized'))

        # spawn() must thread the opening payload forward; finishing with
        # the stale preflight payload is an integrity error and must retain
        # the durable journal.
        self.assertRaises(
            driver.incus_idmap.IDMapIntegrityError,
            self.driver._finish_spawn_attempt_open, self.instance, preflight)
        self.assertEqual(
            opened, driver._read_spawn_attempt_journal(self.instance))

        self.driver._finish_spawn_attempt_open(self.instance, opened)
        self.assertIsNone(driver._read_spawn_attempt_journal(self.instance))

    def test_preflight_spawn_attempt_with_incus_token_fails_closed(self):
        self.driver.idmap_allocator.get.return_value = None
        self.driver.idmap_allocator.get_release_intent.return_value = None
        self.instance.system_metadata = {}
        attempt = self.driver._create_spawn_preflight_attempt(
            self.instance, self.materialization_id)
        self._set_all_project_inventory(instances=[{
            'name': self.instance.name,
            'project': 'nova',
            'config': {
                driver.IDMAP_MATERIALIZATION_CONFIG_KEY:
                    self.materialization_id,
            },
        }])

        self.assertRaises(
            driver.incus_idmap.IDMapIntegrityError,
            self.driver._consume_spawn_preflight_noop, self.instance)
        self.assertEqual(
            attempt, driver._read_spawn_attempt_journal(self.instance))

    def test_opening_spawn_attempt_removed_only_by_exact_claim(self):
        unmaterialized = self._claim(state='unmaterialized')
        self.driver.idmap_allocator.get.return_value = self.assignment
        self.driver.idmap_allocator.get_host_claim.return_value = (
            unmaterialized)
        attempt = driver._write_spawn_attempt_journal(
            self.instance, self.host_id, self.materialization_id,
            phase='preflight')
        opened = self.driver._open_spawn_attempt(self.instance, attempt)

        self.driver._finish_spawn_attempt_open(self.instance, opened)

        self.assertIsNone(driver._read_spawn_attempt_journal(self.instance))

    def test_reschedule_reuses_exact_cleaned_allocation_generation(self):
        source_host = '10000000-0000-0000-0000-000000000001'
        source_token = '30000000-0000-0000-0000-000000000001'
        assignment = dataclasses.replace(
            self.assignment, host_ids=(source_host,))
        source_claim = dataclasses.replace(
            self.claim, host_id=source_host,
            materialization_id=source_token, state='cleaned',
            proof=mock.sentinel.source_cleanup_proof)
        destination_claim = self._claim(state='unmaterialized')
        claimed_assignment = dataclasses.replace(
            assignment, host_ids=tuple(sorted((source_host, self.host_id))))
        state = {
            'assignment': assignment,
            'claims': {source_host: source_claim},
        }
        self._set_instance_idmap_metadata()

        def get_assignment(instance_uuid):
            self.assertEqual(self.instance.uuid, instance_uuid)
            return state['assignment']

        def get_claim(instance_uuid, host_id):
            self.assertEqual(self.instance.uuid, instance_uuid)
            return state['claims'].get(host_id)

        def claim(instance_uuid, host_id, token, assignment=None):
            self.assertEqual(self.instance.uuid, instance_uuid)
            self.assertEqual(self.host_id, host_id)
            self.assertEqual(self.materialization_id, token)
            self.assertEqual(state['assignment'], assignment)
            state['assignment'] = claimed_assignment
            state['claims'][self.host_id] = destination_claim
            return claimed_assignment

        self.driver.idmap_allocator.get.side_effect = get_assignment
        self.driver.idmap_allocator.get_host_claim.side_effect = get_claim
        self.driver.idmap_allocator.claim.side_effect = claim
        self.driver._root_storage_materialization_binding = mock.Mock(
            return_value=mock.sentinel.binding)

        attempt = self.driver._create_spawn_preflight_attempt(
            self.instance, self.materialization_id)
        opened = self.driver._open_spawn_attempt(self.instance, attempt)
        materialization = self.driver._begin_idmap_materialization(
            self.instance, self.materialization_id,
            {'type': 'disk', 'path': '/', 'pool': 'root-pool'})
        self.driver._finish_spawn_attempt_open(self.instance, opened)

        self.assertTrue(driver._spawn_attempt_generation_matches(
            attempt, assignment))
        self.assertEqual(assignment, materialization.assignment)
        self.assertEqual(source_claim, state['claims'][source_host])
        self.assertEqual(destination_claim, state['claims'][self.host_id])
        self.assertEqual(claimed_assignment, state['assignment'])
        self.driver.idmap_allocator.allocate.assert_not_called()
        self.driver.storage_ownership.register_materialization.\
            assert_called_once_with(mock.sentinel.binding)
        self.assertIsNone(driver._read_spawn_attempt_journal(self.instance))

    def test_reschedule_preflight_failure_preserves_generation_for_retry(self):
        source_host = '10000000-0000-0000-0000-000000000001'
        source_token = '30000000-0000-0000-0000-000000000001'
        failed_token = '30000000-0000-0000-0000-000000000008'
        assignment = dataclasses.replace(
            self.assignment, host_ids=(source_host,))
        source_claim = dataclasses.replace(
            self.claim, host_id=source_host,
            materialization_id=source_token, state='cleaned',
            proof=mock.sentinel.source_cleanup_proof)
        destination_claim = self._claim(state='unmaterialized')
        claimed_assignment = dataclasses.replace(
            assignment, host_ids=tuple(sorted((source_host, self.host_id))))
        state = {
            'assignment': assignment,
            'claims': {source_host: source_claim},
        }
        self._set_instance_idmap_metadata()

        self.driver.idmap_allocator.get.side_effect = (
            lambda unused_uuid: state['assignment'])
        self.driver.idmap_allocator.get_host_claim.side_effect = (
            lambda unused_uuid, host_id: state['claims'].get(host_id))

        failed_attempt = self.driver._create_spawn_preflight_attempt(
            self.instance, failed_token)

        self.assertTrue(self.driver._consume_spawn_preflight_noop(
            self.instance))
        self.assertTrue(driver._spawn_attempt_generation_matches(
            failed_attempt, assignment))
        self.assertEqual(assignment, state['assignment'])
        self.assertEqual({source_host: source_claim}, state['claims'])
        self.driver.idmap_allocator.retire_claim.assert_not_called()

        def claim(instance_uuid, host_id, token, assignment=None):
            self.assertEqual(self.instance.uuid, instance_uuid)
            self.assertEqual(self.host_id, host_id)
            self.assertEqual(self.materialization_id, token)
            self.assertEqual(state['assignment'], assignment)
            state['assignment'] = claimed_assignment
            state['claims'][self.host_id] = destination_claim
            return claimed_assignment

        self.driver.idmap_allocator.claim.side_effect = claim
        self.driver._root_storage_materialization_binding = mock.Mock(
            return_value=mock.sentinel.binding)
        retry = self.driver._create_spawn_preflight_attempt(
            self.instance, self.materialization_id)
        opened = self.driver._open_spawn_attempt(self.instance, retry)
        self.driver._begin_idmap_materialization(
            self.instance, self.materialization_id,
            {'type': 'disk', 'path': '/', 'pool': 'root-pool'})
        self.driver._finish_spawn_attempt_open(self.instance, opened)

        self.assertEqual(claimed_assignment, state['assignment'])
        self.assertEqual(source_claim, state['claims'][source_host])
        self.assertEqual(destination_claim, state['claims'][self.host_id])
        self.driver.idmap_allocator.allocate.assert_not_called()
        self.assertIsNone(driver._read_spawn_attempt_journal(self.instance))

    def test_reschedule_rejects_unclean_historical_claim(self):
        source_host = '10000000-0000-0000-0000-000000000001'
        assignment = dataclasses.replace(
            self.assignment, host_ids=(source_host,))
        source_claim = dataclasses.replace(
            self.claim, host_id=source_host, state='possible', proof=None)
        self._set_instance_idmap_metadata()
        self.driver.idmap_allocator.get.return_value = assignment
        self.driver.idmap_allocator.get_host_claim.return_value = source_claim

        self.assertRaises(
            driver.incus_idmap.IDMapConflict,
            self.driver._create_spawn_preflight_attempt,
            self.instance, self.materialization_id)

        self.assertIsNone(driver._read_spawn_attempt_journal(self.instance))

    def test_opening_response_loss_destroys_exact_claim_then_retires(self):
        cleaned = self._claim(
            state='cleaned', proof=mock.sentinel.cleanup_proof)
        self._set_instance_idmap_metadata()
        self.driver.idmap_allocator.get.return_value = self.assignment
        self.driver.idmap_allocator.get_host_claim.return_value = cleaned
        attempt = driver._write_spawn_attempt_journal(
            self.instance, self.host_id, self.materialization_id,
            phase='preflight')
        self.driver._open_spawn_attempt(self.instance, attempt)
        self.driver._settle_idmap_host_claim = mock.Mock(
            return_value=cleaned)
        self.driver.cleanup = mock.Mock()
        self.driver._retire_instance_idmap_claim_if_clean = mock.Mock()
        absent = incuscore_exceptions.NotFound(MockResponse(404))
        self.driver.client.instances.get.side_effect = absent
        self.driver.client.profiles.get.side_effect = absent

        self.driver.destroy(
            context.get_admin_context(), self.instance, [],
            block_device_info={'block_device_mapping': []})

        self.driver._settle_idmap_host_claim.assert_called_once_with(
            self.instance, cleaned, final_delete=False)
        self.driver.cleanup.assert_called_once()
        self.driver._retire_instance_idmap_claim_if_clean.\
            assert_called_once_with(self.instance)
        self.assertIsNone(driver._read_spawn_attempt_journal(self.instance))

    def test_opening_journal_generation_must_match_claim(self):
        other = dataclasses.replace(
            self.assignment,
            allocation_id='10000000-0000-0000-0000-000000000009')
        attempt = driver._write_spawn_attempt_journal(
            self.instance, self.host_id, self.materialization_id,
            phase='preflight', generation=other)
        self.driver._open_spawn_attempt(self.instance, attempt)

        self.assertRaises(
            driver.incus_idmap.IDMapIntegrityError,
            self.driver._remove_spawn_attempt_for_claim,
            self.instance, self.claim)

        self.assertIsNotNone(
            driver._read_spawn_attempt_journal(self.instance))

    def test_release_context_absent_everywhere_is_noop(self):
        # A delete racing a queued build, or a retry after the release
        # completed: no allocation, no claim, no metadata. Deletion must
        # proceed without demanding build-only evidence.
        self.driver.idmap_allocator.get.return_value = None
        self.driver.idmap_allocator.get_host_claim.return_value = None

        self.assertEqual(
            (None, None, None),
            self.driver._idmap_rootfs_release_context(self.instance))

    def test_release_context_stale_metadata_alone_is_noop(self):
        self.driver.idmap_allocator.get.return_value = None
        self.driver.idmap_allocator.get_host_claim.return_value = None
        self._set_instance_idmap_metadata()

        self.assertEqual(
            (None, None, None),
            self.driver._idmap_rootfs_release_context(self.instance))

    def test_release_context_claim_without_allocation_fails_closed(self):
        self.driver.idmap_allocator.get.return_value = None
        self.driver.idmap_allocator.get_host_claim.return_value = (
            self._claim(state='unmaterialized'))

        self.assertRaisesRegex(
            driver.incus_idmap.IDMapIntegrityError,
            'without its allocation',
            self.driver._idmap_rootfs_release_context, self.instance)

    def test_release_context_unstamped_build_uses_registry_authority(self):
        # The build died between the durable allocation and the Nova
        # metadata stamp: the registry pair is the release authority.
        self.instance.system_metadata = {}

        intent, assignment, claim = (
            self.driver._idmap_rootfs_release_context(self.instance))

        self.assertIsNone(intent)
        self.assertEqual(self.assignment, assignment)
        self.assertEqual(self.claim, claim)

    def test_release_context_metadata_mismatch_still_fails_closed(self):
        self._set_instance_idmap_metadata()
        self.instance.system_metadata[
            driver.IDMAP_ALLOCATION_METADATA_KEY] = (
                '10000000-0000-0000-0000-00000000000f')

        self.assertRaisesRegex(
            driver.incus_idmap.IDMapIntegrityError,
            'does not match the Nova',
            self.driver._idmap_rootfs_release_context, self.instance)

    def test_release_context_allocation_without_local_claim_is_noop(self):
        # The build never claimed this host; the bare allocation belongs to
        # the terminal failed-build reconciler, not this delete.
        self._set_instance_idmap_metadata()
        self.driver.idmap_allocator.get_host_claim.return_value = None

        self.assertEqual(
            (None, None, None),
            self.driver._idmap_rootfs_release_context(self.instance))

    def test_settle_abandons_never_registered_unmaterialized_claim(self):
        unmaterialized = self._claim(state='unmaterialized')
        self.driver.idmap_allocator.get_host_claim.return_value = (
            unmaterialized)
        self.driver.storage_ownership.discover_materialization.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))

        settled = self.driver._settle_idmap_host_claim(
            self.instance, unmaterialized)

        self.assertIsNone(settled)
        (self.driver.idmap_allocator.abandon_unregistered_claim
            .assert_called_once_with(
                unmaterialized.instance_uuid, unmaterialized.host_id,
                unmaterialized.materialization_id,
                assignment=self.assignment))
        self.driver.idmap_allocator.record_materialization_proof \
            .assert_not_called()

    def test_settle_missing_attempt_beyond_unmaterialized_fails_closed(self):
        possible = self._claim(state='possible')
        self.driver.idmap_allocator.get_host_claim.return_value = possible
        self.driver.storage_ownership.discover_materialization.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))

        self.assertRaisesRegex(
            driver.incus_idmap.IDMapIntegrityError,
            'already issued its create request',
            self.driver._settle_idmap_host_claim, self.instance, possible)
        self.driver.idmap_allocator.abandon_unregistered_claim \
            .assert_not_called()

    def test_failed_build_destroy_reacks_cleaned_claim_without_retiring(self):
        cleaned = self._claim(
            state='cleaned', proof=mock.sentinel.cleanup_proof)
        self.instance.vm_state = vm_states.BUILDING
        self.instance.task_state = task_states.SPAWNING
        self.driver._consume_spawn_preflight_noop = mock.Mock(
            return_value=False)
        self.driver._idmap_rootfs_release_context = mock.Mock(
            return_value=(None, self.assignment, cleaned))
        self.driver._settle_idmap_host_claim = mock.Mock(
            return_value=cleaned)
        self.driver.cleanup = mock.Mock()
        self.driver._remove_spawn_attempt_for_claim = mock.Mock()
        self.driver._retire_instance_idmap_claim_if_clean = mock.Mock()
        self.driver.client.instances.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        self.driver.client.profiles.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))

        self.driver.destroy(
            context.get_admin_context(), self.instance, [],
            block_device_info={'block_device_mapping': []})

        self.driver._settle_idmap_host_claim.assert_called_once_with(
            self.instance, cleaned, final_delete=False)
        self.driver.cleanup.assert_called_once()
        self.driver._retire_instance_idmap_claim_if_clean.assert_not_called()

    @mock.patch.object(driver.lockutils, 'lock')
    def test_materialization_lock_covers_mark_and_create(self, lock):
        materialization = self._materialization()
        possible = self._claim(state='possible')
        committed = self._claim(state='committed')
        self.driver._exact_idmap_host_claim = mock.Mock(side_effect=[
            (self.assignment, materialization.claim),
            (self.assignment, possible),
        ])
        mark_committed = (
            self.driver.idmap_allocator.mark_materialization_committed)
        mark_committed.return_value = committed
        observe = self.driver.storage_ownership.observe_materialization_start
        observe.return_value = self._attempt()
        create = mock.Mock(return_value=mock.sentinel.container)
        request_config = {}

        result = self.driver._with_rootfs_materialization_barrier(
            materialization, request_config, create)

        self.assertIs(mock.sentinel.container, result)
        create.assert_called_once_with()
        self.driver.idmap_allocator.mark_materialization_possible.\
            assert_called_once_with(
                self.instance.uuid, self.host_id, self.materialization_id,
                assignment=self.assignment)
        mark_committed.assert_called_once_with(
            self.instance.uuid, self.host_id, self.materialization_id,
            assignment=self.assignment)
        self.assertEqual(
            self.materialization_id,
            request_config[driver.IDMAP_MATERIALIZATION_CONFIG_KEY])
        self.assertEqual(2, lock.call_count)

    @mock.patch.object(driver, '_settle_incus_operation')
    @mock.patch.object(driver.lockutils, 'lock')
    def test_abort_settles_the_target_operation_before_settling(
            self, lock, settle_operation):
        """A create slower than the read timeout is still running."""
        materialization = self._materialization()
        protocol = self.driver.storage_ownership
        aborted = self._attempt(state='aborted', finished=False)
        settled = self._attempt(state='aborted', finished=True)
        settled = dataclasses.replace(settled, proof=mock.sentinel.proof)
        protocol.get_materialization.return_value = self._attempt(
            state='active', finished=False)
        protocol.abort_materialization.return_value = aborted
        protocol.settle_materialization.return_value = settled
        self.driver._record_and_ack_materialization_proof = mock.Mock(
            return_value=mock.sentinel.recorded)

        order = []
        settle_operation.side_effect = lambda *a, **kw: order.append(
            'operation')
        protocol.settle_materialization.side_effect = (
            lambda *a, **kw: order.append('attempt') or settled)

        self.driver._abort_idmap_materialization(materialization)

        settle_operation.assert_called_once_with(
            materialization.client, aborted.operation_uuid)
        protocol.settle_materialization.assert_called_once_with(
            materialization.binding)
        self.assertEqual(['operation', 'attempt'], order)

    @mock.patch.object(driver.lockutils, 'lock')
    def test_failed_abort_reports_the_original_build_error(self, lock):
        """The abort's own failure must not mask why the build failed."""
        materialization = self._materialization()
        self.driver._exact_idmap_host_claim = mock.Mock(
            return_value=(self.assignment, materialization.claim))
        observe = self.driver.storage_ownership.observe_materialization_start
        # A create that outlived the read timeout has not committed yet, so
        # the barrier takes its abort path.
        observe.return_value = self._attempt(state='active', finished=False)
        original = TimeoutError('read timed out waiting for the operation')
        create = mock.Mock(side_effect=original)
        self.driver._abort_idmap_materialization = mock.Mock(
            side_effect=incuscore_exceptions.LXDAPIException(
                MockResponse(409)))

        raised = self.assertRaises(
            TimeoutError,
            self.driver._with_rootfs_materialization_barrier,
            materialization, {}, create)

        self.assertIs(original, raised)
        self.driver._abort_idmap_materialization.assert_called_once()

    @mock.patch.object(driver.lockutils, 'lock')
    def test_lost_create_response_recovers_only_after_exact_commit(self, lock):
        materialization = self._materialization()
        possible = self._claim(state='possible')
        committed = self._claim(state='committed')
        self.driver._exact_idmap_host_claim = mock.Mock(side_effect=[
            (self.assignment, materialization.claim),
            (self.assignment, possible),
        ])
        mark_committed = (
            self.driver.idmap_allocator.mark_materialization_committed)
        mark_committed.return_value = committed
        observe = self.driver.storage_ownership.observe_materialization_start
        observe.return_value = self._attempt()
        create = mock.Mock(side_effect=RuntimeError('create response lost'))
        recover = mock.Mock(return_value=mock.sentinel.container)

        result = self.driver._with_rootfs_materialization_barrier(
            materialization, {}, create, recover_action=recover)

        self.assertIs(mock.sentinel.container, result)
        recover.assert_called_once_with()
        mark_committed.assert_called_once()

    def test_delete_success_then_proof_failure_is_not_reported_complete(self):
        claim = self._claim(state='committed')
        container = mock.Mock(config={
            'user.openstack.uuid': self.instance.uuid,
            driver.IDMAP_ALLOCATION_CONFIG_KEY: claim.allocation_id,
            driver.IDMAP_COMPUTE_CONFIG_KEY: claim.host_id,
            driver.IDMAP_MATERIALIZATION_CONFIG_KEY:
                claim.materialization_id})
        endpoint = self.driver.client.api.instances[self.instance.name]
        response = mock.Mock()
        response.json.return_value = {
            'operation': '/1.0/operations/'
                         '30000000-0000-0000-0000-000000000003'}
        endpoint.delete.return_value = response
        proof_error = RuntimeError('process died before proof')
        self.driver._promote_idmap_claim_if_server_committed = mock.Mock(
            return_value=(self.assignment, claim))
        self.driver._settle_idmap_host_claim = mock.Mock(
            side_effect=proof_error)

        raised = self.assertRaises(
            RuntimeError,
            self.driver._delete_instance_with_rootfs_release_receipt,
            container, self.instance, claim)

        self.assertIs(proof_error, raised)
        wait = self.driver.client.operations.wait_for_operation
        wait.assert_called_once_with(
            '30000000-0000-0000-0000-000000000003')

    def test_tokenized_delete_recovers_lost_operation_response(self):
        claim = self._claim(state='committed')
        container = mock.Mock(config={
            'user.openstack.uuid': self.instance.uuid,
            driver.IDMAP_ALLOCATION_CONFIG_KEY: claim.allocation_id,
            driver.IDMAP_COMPUTE_CONFIG_KEY: claim.host_id,
            driver.IDMAP_MATERIALIZATION_CONFIG_KEY:
                claim.materialization_id})
        endpoint = self.driver.client.api.instances[self.instance.name]
        endpoint.delete.side_effect = RuntimeError('connection lost')
        self.driver._promote_idmap_claim_if_server_committed = mock.Mock(
            return_value=(self.assignment, claim))
        self.driver._settle_idmap_host_claim = mock.Mock(
            return_value=mock.sentinel.proof)

        result = self.driver._delete_instance_with_rootfs_release_receipt(
            container, self.instance, claim)

        self.assertIs(mock.sentinel.proof, result)
        endpoint.delete.assert_called_once_with(params={
            'project': 'nova',
            'rootfs-idmap-release-token': claim.materialization_id,
            'rootfs-idmap-release-owner': claim.instance_uuid,
            'rootfs-idmap-allocation-id': claim.allocation_id,
            'rootfs-idmap-compute-id': claim.host_id,
        })
        self.driver._settle_idmap_host_claim.assert_called_once_with(
            self.instance, claim, final_delete=True,
            client=self.driver.client)

    def test_tokenized_delete_rejects_profile_only_owner(self):
        claim = self._claim(state='committed')
        container = mock.Mock(config={})
        self.driver._promote_idmap_claim_if_server_committed = mock.Mock(
            return_value=(self.assignment, claim))

        self.assertRaises(
            driver.incus_idmap.IDMapIntegrityError,
            self.driver._delete_instance_with_rootfs_release_receipt,
            container, self.instance, claim)

        self.driver.client.api.instances[
            self.instance.name].delete.assert_not_called()

    def _fence_bound_container(self):
        return mock.Mock(config={
            'user.openstack.uuid': self.assignment.instance_uuid,
            driver.IDMAP_ALLOCATION_CONFIG_KEY:
                self.assignment.allocation_id,
            driver.IDMAP_COMPUTE_CONFIG_KEY: self.host_id,
            driver.IDMAP_MATERIALIZATION_CONFIG_KEY:
                self.materialization_id,
            'security.idmap.base': str(self.assignment.base),
            'security.idmap.size': str(self.assignment.size)})

    def test_fence_retired_record_disposal_needs_no_registry_write(self):
        """A returning host disposes of its evacuated-stale record.

        The claim was fence-retired, so the local delete supplies the
        four-tuple from the container's own binding and the produced
        receipt is acknowledged without touching the registry.
        """
        self.driver.idmap_allocator.get_fence_proof.return_value = (
            driver.incus_idmap.IDMapFenceProof(
                instance_uuid=self.assignment.instance_uuid,
                host_id=self.host_id,
                allocation_id=self.assignment.allocation_id,
                fence_agent='virsh', fenced_at='2026-08-07T00:00:00Z',
                operator='ops@example.com', evidence='status=off'))
        container = self._fence_bound_container()
        endpoint = self.driver.client.api.instances[self.instance.name]
        response = mock.Mock()
        response.json.return_value = {
            'operation': '/1.0/operations/'
                         '30000000-0000-0000-0000-000000000003'}
        endpoint.delete.return_value = response
        ownership = self.driver.storage_ownership
        ownership.discover_release_receipt.return_value = (
            mock.sentinel.binding, mock.sentinel.receipt)

        self.driver.client.host_info = {
            'api_extensions': ['instance_storage_handover',
                               'instance_storage_handover_detached']}
        protected = mock.Mock(config={
            'volatile.migration.storage_delete_protection': 'true'})
        self.driver.client.instances.get.return_value = protected

        self.driver._delete_fence_retired_instance(container, self.instance)

        handover = self.driver.client.api.instances[
            self.instance.name]['storage-handover']
        handover.put.assert_called_once_with(
            params={'project': 'nova'}, json={'state': 'detached'})
        endpoint.delete.assert_called_once_with(params={
            'project': 'nova',
            'rootfs-idmap-release-token': self.materialization_id,
            'rootfs-idmap-release-owner': self.assignment.instance_uuid,
            'rootfs-idmap-allocation-id': self.assignment.allocation_id,
            'rootfs-idmap-compute-id': self.host_id,
        })
        ownership.acknowledge_release_receipt.assert_called_once_with(
            mock.sentinel.binding, mock.sentinel.receipt)
        record = self.driver.idmap_allocator.record_rootfs_release_proof
        record.assert_not_called()
        self.driver.idmap_allocator.retire_claim.assert_not_called()

    def test_fence_disposal_refused_without_ledger_entry(self):
        # A bound record with neither a claim nor fence evidence stays
        # fail-closed: no guessing at incusd's receipt requirement.
        self.driver.idmap_allocator.get_fence_proof.return_value = None
        container = self._fence_bound_container()

        self.assertRaisesRegex(
            driver.incus_idmap.IDMapIntegrityError, 'fence disposal',
            self.driver._delete_fence_retired_instance,
            container, self.instance)

        self.driver.client.api.instances[
            self.instance.name].delete.assert_not_called()

    def test_fence_disposal_refuses_foreign_generation_evidence(self):
        self.driver.idmap_allocator.get_fence_proof.return_value = (
            driver.incus_idmap.IDMapFenceProof(
                instance_uuid=self.assignment.instance_uuid,
                host_id=self.host_id,
                allocation_id='10000000-0000-0000-0000-00000000009f',
                fence_agent='virsh', fenced_at='2026-08-07T00:00:00Z',
                operator='ops@example.com', evidence='status=off'))
        container = self._fence_bound_container()

        self.assertRaisesRegex(
            driver.incus_idmap.IDMapIntegrityError,
            'another allocation generation',
            self.driver._delete_fence_retired_instance,
            container, self.instance)

        self.driver.client.api.instances[
            self.instance.name].delete.assert_not_called()

    def test_binding_predicate_requires_every_key(self):
        self.assertTrue(
            self.driver._instance_has_materialization_binding(
                self._fence_bound_container()))
        partial = self._fence_bound_container()
        del partial.config[driver.IDMAP_MATERIALIZATION_CONFIG_KEY]
        self.assertFalse(
            self.driver._instance_has_materialization_binding(partial))

    def test_allocate_refreshes_and_preserves_unrelated_system_metadata(self):
        instance = objects.Instance(
            uuid=self.assignment.instance_uuid,
            system_metadata={'stale-key': 'stale-value'})
        self.driver.idmap_allocator.allocate.return_value = self.assignment

        with mock.patch.object(instance, 'refresh') as refresh:
            refresh.side_effect = lambda: setattr(
                instance, 'system_metadata', {'unrelated-key': 'keep-me'})
            with mock.patch.object(instance, 'save') as save:
                self.driver._ensure_instance_idmap(instance)

        refresh.assert_called_once_with()
        save.assert_called_once_with()
        self.assertEqual('keep-me', instance.system_metadata['unrelated-key'])
        self.assertNotIn('stale-key', instance.system_metadata)

    def test_an_already_stamped_instance_costs_no_database_round_trip(self):
        """Every caller reaches here on the common path.

        Several do so per spawn, so confirming from the database what the
        object already says would be paid on every instance of a run.
        """
        instance = objects.Instance(
            uuid=self.assignment.instance_uuid,
            system_metadata={
                driver.IDMAP_BASE_METADATA_KEY: str(self.assignment.base),
                driver.IDMAP_SIZE_METADATA_KEY: str(self.assignment.size),
                driver.IDMAP_ALLOCATION_METADATA_KEY:
                    self.assignment.allocation_id,
                driver.IDMAP_FINGERPRINT_METADATA_KEY:
                    self.assignment.fingerprint,
            })
        self.driver.idmap_allocator.get.return_value = self.assignment

        with mock.patch.object(instance, 'refresh') as refresh:
            with mock.patch.object(instance, 'save') as save:
                result = self.driver._ensure_instance_idmap(instance)

        self.assertEqual(self.assignment, result)
        refresh.assert_not_called()
        save.assert_not_called()

    def test_a_concurrent_stamp_found_by_refresh_is_not_rewritten(self):
        # The refresh exists to avoid clobbering another writer; if it
        # shows the stamp already applied there is nothing left to write.
        instance = objects.Instance(
            uuid=self.assignment.instance_uuid, system_metadata={})
        self.driver.idmap_allocator.allocate.return_value = self.assignment

        def refreshed():
            instance.system_metadata = {
                driver.IDMAP_BASE_METADATA_KEY: str(self.assignment.base),
                driver.IDMAP_SIZE_METADATA_KEY: str(self.assignment.size),
                driver.IDMAP_ALLOCATION_METADATA_KEY:
                    self.assignment.allocation_id,
                driver.IDMAP_FINGERPRINT_METADATA_KEY:
                    self.assignment.fingerprint,
            }

        with mock.patch.object(instance, 'refresh', side_effect=refreshed):
            with mock.patch.object(instance, 'save') as save:
                self.driver._ensure_instance_idmap(instance)

        save.assert_not_called()

    def test_existing_metadata_requires_exact_allocator_generation(self):
        self.instance.system_metadata = {
            driver.IDMAP_BASE_METADATA_KEY: '500000000',
            driver.IDMAP_SIZE_METADATA_KEY: '65536',
            driver.IDMAP_ALLOCATION_METADATA_KEY:
                self.assignment.allocation_id,
            driver.IDMAP_FINGERPRINT_METADATA_KEY: 'a' * 64,
        }
        self.driver.idmap_allocator.get.return_value = self.assignment

        result = self.driver._ensure_instance_idmap(self.instance)

        self.assertEqual(self.assignment, result)
        self.driver.idmap_allocator.get.assert_called_once_with(
            self.instance.uuid)
        self.driver.idmap_allocator.allocate.assert_not_called()
        self.instance.save.assert_not_called()

    def test_claim_rechecks_generation_under_reconciliation_lock(self):
        self.driver._ensure_instance_idmap = mock.Mock(
            return_value=self.assignment)
        replacement = dataclasses.replace(
            self.assignment,
            allocation_id='50000000-0000-0000-0000-000000000005')
        self.driver.idmap_allocator.get.return_value = replacement

        self.assertRaises(
            driver.incus_idmap.IDMapIntegrityError,
            self.driver._begin_idmap_materialization,
            self.instance, self.materialization_id,
            {'type': 'disk', 'path': '/', 'pool': 'root-pool'})

        self.driver.idmap_allocator.claim.assert_not_called()

    def test_global_metadata_without_allocator_fails_closed(self):
        self.instance.system_metadata = {
            driver.IDMAP_BASE_METADATA_KEY: '500000000',
            driver.IDMAP_SIZE_METADATA_KEY: '65536',
            driver.IDMAP_ALLOCATION_METADATA_KEY:
                self.assignment.allocation_id,
            driver.IDMAP_FINGERPRINT_METADATA_KEY: 'a' * 64,
        }
        self.driver.idmap_allocator = None

        self.assertRaises(
            driver.incus_idmap.IDMapIntegrityError,
            self.driver._ensure_instance_idmap, self.instance)

    def test_allocator_requires_persistent_compute_uuid(self):
        self.driver._ensure_instance_idmap = mock.Mock(
            return_value=self.assignment)

        driver.virt_node.read_local_node_uuid.return_value = None
        self.assertRaises(
            driver.incus_idmap.IDMapConfigurationError,
            self.driver._begin_idmap_materialization,
            self.instance, self.materialization_id,
            {'type': 'disk', 'path': '/', 'pool': 'root-pool'})

        self.driver.idmap_allocator.claim.assert_not_called()

    def test_existing_metadata_without_registry_record_fails_closed(self):
        self.instance.system_metadata = {
            driver.IDMAP_BASE_METADATA_KEY: '500000000',
            driver.IDMAP_SIZE_METADATA_KEY: '65536',
            driver.IDMAP_ALLOCATION_METADATA_KEY:
                self.assignment.allocation_id,
            driver.IDMAP_FINGERPRINT_METADATA_KEY: 'a' * 64,
        }
        self.driver.idmap_allocator.get.return_value = None

        self.assertRaises(
            driver.incus_idmap.IDMapIntegrityError,
            self.driver._ensure_instance_idmap, self.instance)

        self.driver.idmap_allocator.allocate.assert_not_called()
        self.driver.idmap_allocator.adopt.assert_not_called()
        self.instance.save.assert_not_called()

    def test_observed_mapping_must_match_nova_metadata(self):
        self.instance.system_metadata = {
            driver.IDMAP_BASE_METADATA_KEY: '500065536',
            driver.IDMAP_SIZE_METADATA_KEY: '65536',
            driver.IDMAP_ALLOCATION_METADATA_KEY:
                self.assignment.allocation_id,
            driver.IDMAP_FINGERPRINT_METADATA_KEY: 'a' * 64,
        }

        self.assertRaises(
            driver.incus_idmap.IDMapIntegrityError,
            self.driver._ensure_instance_idmap, self.instance,
            observed_base=500000000, observed_size=65536)
        self.driver.idmap_allocator.adopt.assert_not_called()

    def test_start_gate_forwards_observed_instance_mapping(self):
        self._set_instance_idmap_metadata()
        container = self._idmap_container()

        result = self.driver._ensure_instance_idmap_before_start(
            self.instance, container)

        self.assertEqual(self.assignment, result)
        self.driver.idmap_allocator.assert_startable.assert_called_once_with(
            self.instance.uuid, self.host_id, self.materialization_id,
            assignment=self.assignment)

    def test_possible_start_promotes_from_exact_committed_attempt(self):
        self._set_instance_idmap_metadata()
        possible = self._claim(state='possible')
        committed = self._claim(state='committed')
        self.driver.idmap_allocator.get_host_claim.return_value = possible
        mark_committed = (
            self.driver.idmap_allocator.mark_materialization_committed)
        mark_committed.return_value = committed
        self.driver.storage_ownership.discover_materialization.return_value = (
            self._attempt())
        container = self._idmap_container()

        self.driver._start_instance_with_idmap(self.instance, container)

        mark_committed.assert_called_once_with(
            self.instance.uuid, self.host_id, self.materialization_id,
            assignment=self.assignment)
        self.driver.idmap_allocator.assert_startable.assert_called_once_with(
            self.instance.uuid, self.host_id, self.materialization_id,
            assignment=self.assignment)
        container.start.assert_called_once_with(wait=True)

    def test_possible_start_rejects_noncommitted_server_attempt(self):
        self._set_instance_idmap_metadata()
        possible = self._claim(state='possible')
        self.driver.idmap_allocator.get_host_claim.return_value = possible
        self.driver.storage_ownership.discover_materialization.return_value = (
            self._attempt(state='active', finished=False))
        self.driver.idmap_allocator.assert_startable.side_effect = (
            driver.incus_idmap.IDMapConflict(
                reason='materialization has not committed'))
        container = self._idmap_container()

        self.assertRaises(
            driver.incus_idmap.IDMapConflict,
            self.driver._start_instance_with_idmap,
            self.instance, container)

        self.driver.idmap_allocator.mark_materialization_committed.\
            assert_not_called()
        container.start.assert_not_called()

    def test_possible_start_rejects_committed_attempt_for_another_token(self):
        self._set_instance_idmap_metadata()
        possible = self._claim(state='possible')
        self.driver.idmap_allocator.get_host_claim.return_value = possible
        wrong_binding = dataclasses.replace(
            self._binding(),
            token='50000000-0000-0000-0000-000000000005')
        attempt = dataclasses.replace(
            self._attempt(), binding=wrong_binding)
        self.driver.storage_ownership.discover_materialization.return_value = (
            attempt)
        container = self._idmap_container()

        self.assertRaises(
            driver.incus_idmap.IDMapIntegrityError,
            self.driver._start_instance_with_idmap,
            self.instance, container)

        self.driver.idmap_allocator.mark_materialization_committed.\
            assert_not_called()
        self.driver.idmap_allocator.assert_startable.assert_not_called()
        container.start.assert_not_called()

    def test_possible_claim_cannot_drive_final_delete(self):
        possible = self._claim(state='possible')
        self.driver.idmap_allocator.get_host_claim.return_value = possible
        self.driver.storage_ownership.discover_materialization.return_value = (
            self._attempt(state='active', finished=False))
        container = self._idmap_container()

        self.assertRaises(
            driver.incus_idmap.IDMapIntegrityError,
            self.driver._delete_instance_with_rootfs_release_receipt,
            container, self.instance, possible)

        self.driver.client.api.instances[
            self.instance.name].delete.assert_not_called()

    def test_final_settlement_requires_committed_claim(self):
        self._set_instance_idmap_metadata()
        possible = self._claim(state='possible')
        self.driver.idmap_allocator.get_host_claim.return_value = possible

        self.assertRaises(
            driver.incus_idmap.IDMapConflict,
            self.driver._settle_idmap_host_claim,
            self.instance, possible, final_delete=True)

        self.driver.storage_ownership.discover_release_receipt.\
            assert_not_called()

    def test_migration_source_settles_delete_disposition_detach_receipt(self):
        """The first shared-Ceph migration cannot rewrite its v1 attempt."""
        self._set_instance_idmap_metadata()
        claim = self._claim(state='committed')
        binding = self._binding(disposition='delete', storage_driver='ceph')
        receipt = driver.incus_idmap.IDMapRootfsReleaseReceipt(
            token=binding.token,
            allocation_id=binding.allocation_id,
            compute_id=binding.compute_id,
            materialization_id=binding.token,
            owner=binding.owner,
            project=binding.project,
            instance_name=binding.instance_name,
            idmap_base=binding.idmap_base,
            idmap_size=binding.idmap_size,
            storage_driver=binding.storage_driver,
            storage_pool=binding.storage_pool,
            storage_volume=binding.storage_volume,
            rbd_image='container_nova_instance-00000001',
            storage_identity='rbd_data.1234567890abcdef',
            baseline_clean=True,
            cleanup_disposition='delete',
            outcome='detached',
            state='complete',
            digest='',
            created_at=10,
            completed_at=11)
        proof = driver.incus_idmap.IDMapRootfsReleaseProof(
            **receipt.__dict__)
        receipt = dataclasses.replace(
            receipt,
            digest=driver.incus_idmap.rootfs_release_proof_digest(proof))
        proof = driver.incus_idmap.validate_rootfs_release_receipt(receipt)
        cleaned = self._claim(state='cleaned', proof=proof)
        self.driver.idmap_allocator.get_host_claim.return_value = claim
        discover = self.driver.storage_ownership.discover_release_receipt
        discover.return_value = (binding, receipt)
        record = self.driver.idmap_allocator.record_rootfs_release_proof
        record.return_value = cleaned

        result = self.driver._settle_idmap_host_claim(
            self.instance, claim, final_delete=True)

        self.assertEqual(cleaned, result)
        discover.assert_called_once()
        record.assert_called_once_with(
            claim.instance_uuid, claim.host_id, claim.materialization_id,
            receipt, assignment=self.assignment)
        self.driver.storage_ownership.acknowledge_release_receipt.\
            assert_called_once_with(binding, receipt)

    def test_cleaned_claim_release_ack_replay_sends_canonical_receipt(self):
        # A cleaned claim stores an IDMapRootfsReleaseProof; replaying the
        # ACK must convert it back to the canonical receipt type the
        # protocol endpoint validates, or fleet-wide release replay wedges.
        self._set_instance_idmap_metadata()
        binding = self._binding(disposition='delete', storage_driver='ceph')
        receipt = driver.incus_idmap.IDMapRootfsReleaseReceipt(
            token=binding.token,
            allocation_id=binding.allocation_id,
            compute_id=binding.compute_id,
            materialization_id=binding.token,
            owner=binding.owner,
            project=binding.project,
            instance_name=binding.instance_name,
            idmap_base=binding.idmap_base,
            idmap_size=binding.idmap_size,
            storage_driver=binding.storage_driver,
            storage_pool=binding.storage_pool,
            storage_volume=binding.storage_volume,
            rbd_image='container_nova_instance-00000001',
            storage_identity='rbd_data.1234567890abcdef',
            baseline_clean=True,
            cleanup_disposition='delete',
            outcome='detached',
            state='complete',
            digest='',
            created_at=10,
            completed_at=11)
        proof = driver.incus_idmap.IDMapRootfsReleaseProof(
            **receipt.__dict__)
        receipt = dataclasses.replace(
            receipt,
            digest=driver.incus_idmap.rootfs_release_proof_digest(proof))
        proof = driver.incus_idmap.validate_rootfs_release_receipt(receipt)
        cleaned = self._claim(state='cleaned', proof=proof)
        self.driver.idmap_allocator.get_host_claim.return_value = cleaned

        result = self.driver._settle_idmap_host_claim(
            self.instance, cleaned, final_delete=True)

        self.assertEqual(cleaned, result)
        acknowledge = self.driver.storage_ownership.acknowledge_release_receipt
        acknowledge.assert_called_once()
        sent = acknowledge.call_args[0][1]
        self.assertIsInstance(
            sent, driver.incus_idmap.IDMapRootfsReleaseReceipt)
        self.assertEqual(
            proof, driver.incus_idmap.validate_rootfs_release_receipt(sent))
        self.driver.storage_ownership.discover_release_receipt.\
            assert_not_called()

    def test_release_intent_blocks_start_after_exact_mapping_check(self):
        self.instance.system_metadata = {
            driver.IDMAP_BASE_METADATA_KEY: str(self.assignment.base),
            driver.IDMAP_SIZE_METADATA_KEY: str(self.assignment.size),
            driver.IDMAP_ALLOCATION_METADATA_KEY:
                self.assignment.allocation_id,
            driver.IDMAP_FINGERPRINT_METADATA_KEY:
                self.assignment.fingerprint,
        }
        container = mock.Mock(
            config={
                'user.openstack.uuid': self.instance.uuid,
                driver.IDMAP_ALLOCATION_CONFIG_KEY:
                    self.assignment.allocation_id,
                driver.IDMAP_COMPUTE_CONFIG_KEY: self.host_id,
                driver.IDMAP_MATERIALIZATION_CONFIG_KEY:
                    self.materialization_id,
                'security.idmap.base': '500000000',
                'security.idmap.size': '65536',
            },
            expanded_config={})
        failure = driver.incus_idmap.IDMapConflict(
            reason='release intent blocks instance start')
        self.driver.idmap_allocator.assert_startable.side_effect = failure

        raised = self.assertRaises(
            driver.incus_idmap.IDMapConflict,
            self.driver._start_instance_with_idmap,
            self.instance, container)

        self.assertIs(failure, raised)
        container.start.assert_not_called()

    def test_clean_local_state_retires_exact_host_claim(self):
        self.instance.system_metadata = {
            driver.IDMAP_BASE_METADATA_KEY: str(self.assignment.base),
            driver.IDMAP_SIZE_METADATA_KEY: str(self.assignment.size),
            driver.IDMAP_ALLOCATION_METADATA_KEY:
                self.assignment.allocation_id,
            driver.IDMAP_FINGERPRINT_METADATA_KEY:
                self.assignment.fingerprint,
        }
        absent = incuscore_exceptions.NotFound(MockResponse(404))
        self.driver.client.instances.get.side_effect = absent
        self.driver.client.profiles.get.side_effect = absent
        self.driver.idmap_allocator.get.return_value = self.assignment
        retired = dataclasses.replace(self.assignment, host_ids=())
        self.driver.idmap_allocator.retire_claim.return_value = retired
        cleaned = self._claim(
            state='cleaned', proof=mock.sentinel.cleanup_proof)
        self.driver._settle_idmap_host_claim = mock.Mock(
            return_value=cleaned)

        with mock.patch.object(os.path, 'lexists', return_value=False):
            self.assertTrue(
                self.driver._retire_instance_idmap_claim_if_clean(
                    self.instance))

        self.driver.idmap_allocator.retire_claim.assert_called_once_with(
            self.instance.uuid, self.host_id, self.materialization_id,
            assignment=self.assignment)

    def test_final_delete_intent_defers_claim_retirement_to_manager(self):
        self._set_instance_idmap_metadata()
        absent = incuscore_exceptions.NotFound(MockResponse(404))
        self.driver.client.instances.get.side_effect = absent
        self.driver.client.profiles.get.side_effect = absent
        self.driver.idmap_allocator.get_release_intent.return_value = (
            self._release_intent())

        with mock.patch.object(os.path, 'lexists', return_value=False):
            self.assertFalse(
                self.driver._retire_instance_idmap_claim_if_clean(
                    self.instance))

        self.driver.idmap_allocator.get.assert_not_called()
        self.driver.idmap_allocator.retire_claim.assert_not_called()
        self.driver.client.api.instances.get.assert_not_called()
        self.driver.client.api.profiles.get.assert_not_called()

    def test_local_resource_prevents_host_claim_retirement(self):
        self.driver.client.instances.get.return_value = mock.sentinel.instance

        self.assertFalse(
            self.driver._retire_instance_idmap_claim_if_clean(self.instance))

        self.driver.idmap_allocator.get.assert_not_called()
        self.driver.idmap_allocator.retire_claim.assert_not_called()

    @mock.patch.object(driver, '_profile_share_mounts', return_value=[])
    @mock.patch.object(driver, '_share_journal_records', return_value=[])
    @mock.patch.object(driver, '_volume_journal_records', return_value=[])
    @mock.patch.object(os.path, 'lexists', return_value=False)
    def test_cleanup_ack_retires_target_claim_before_publishing_proof(
            self, lexists, volume_records, share_records, share_mounts):
        source_host = '10000000-0000-0000-0000-000000000003'
        assignment = dataclasses.replace(
            self.assignment, host_ids=(source_host, self.host_id))
        retired = dataclasses.replace(assignment, host_ids=(source_host,))
        self.instance.system_metadata = {
            driver.IDMAP_BASE_METADATA_KEY: str(assignment.base),
            driver.IDMAP_SIZE_METADATA_KEY: str(assignment.size),
            driver.IDMAP_ALLOCATION_METADATA_KEY: assignment.allocation_id,
            driver.IDMAP_FINGERPRINT_METADATA_KEY: assignment.fingerprint,
        }
        token = '30000000-0000-0000-0000-000000000003'
        profile = mock.Mock(
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': self.instance.uuid,
                driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
            },
            devices={}, used_by=[])
        self.driver.client.instances.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        self.driver.client.profiles.get.return_value = profile
        self.driver.idmap_allocator.get.return_value = assignment
        self.driver.idmap_allocator.retire_claim.return_value = retired
        self.driver.idmap_allocator.get_host_claim.return_value = self.claim
        cleaned = self._claim(
            state='cleaned', proof=mock.sentinel.cleanup_proof)
        self.driver._settle_idmap_host_claim = mock.Mock(
            return_value=cleaned)

        self.assertTrue(self.driver._acknowledge_cleanup_profile(
            self.instance, token))

        self.driver.idmap_allocator.retire_claim.assert_called_once_with(
            self.instance.uuid, self.host_id, self.materialization_id,
            assignment=assignment)
        proof = driver._parse_idmap_retirement_proof(
            profile.config[driver.MIGRATION_IDMAP_RETIREMENT_KEY])
        self.assertEqual(self.host_id, proof.host_id)
        self.assertTrue(driver._same_idmap_generation(proof, assignment))
        self.assertEqual(
            token, profile.config[driver.MIGRATION_CLEANUP_COMPLETE_KEY])
        profile.save.assert_called_once_with(wait=True)

    def test_duplicate_uuid_in_another_project_blocks_claim_retirement(self):
        self.instance.system_metadata = {
            driver.IDMAP_BASE_METADATA_KEY: str(self.assignment.base),
            driver.IDMAP_SIZE_METADATA_KEY: str(self.assignment.size),
            driver.IDMAP_ALLOCATION_METADATA_KEY:
                self.assignment.allocation_id,
            driver.IDMAP_FINGERPRINT_METADATA_KEY:
                self.assignment.fingerprint,
        }
        absent = incuscore_exceptions.NotFound(MockResponse(404))
        self.driver.client.instances.get.side_effect = absent
        self.driver.client.profiles.get.side_effect = absent
        self._set_all_project_inventory(instances=[{
            'name': 'foreign-name',
            'project': 'foreign-project',
            'config': {'user.openstack.uuid': self.instance.uuid},
        }])

        with mock.patch.object(os.path, 'lexists', return_value=False):
            self.assertFalse(
                self.driver._retire_instance_idmap_claim_if_clean(
                    self.instance))

        self.driver.idmap_allocator.retire_claim.assert_not_called()

    def test_fallback_inventory_normalizes_uuid_identity(self):
        owner = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        self.driver.client.has_api_extension.return_value = False
        self._set_all_project_inventory(instances=[{
            'name': 'foreign-name',
            'project': 'foreign-project',
            'config': {
                'user.openstack.uuid':
                    '{AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA}',
            },
        }])

        self.assertFalse(driver._all_project_idmap_resources_absent(
            self.driver.client, owner,
            self.assignment.base, self.assignment.size))

    def test_fallback_inventory_normalizes_numeric_idmap_range(self):
        self.driver.client.has_api_extension.return_value = False
        self._set_all_project_inventory(profiles=[{
            'name': 'foreign-profile',
            'project': 'foreign-project',
            'config': {
                'security.idmap.base': '0500000000',
                'security.idmap.size': '065536',
            },
        }])

        self.assertFalse(driver._all_project_idmap_resources_absent(
            self.driver.client, self.instance.uuid,
            self.assignment.base, self.assignment.size))

    def test_fallback_inventory_volatile_base_overrides_security_base(self):
        self.driver.client.has_api_extension.return_value = False
        self._set_all_project_inventory(instances=[{
            'name': 'volatile-away',
            'project': 'foreign-project',
            'expanded_config': {
                'security.idmap.base': str(self.assignment.base),
                'security.idmap.size': str(self.assignment.size),
            },
            'config': {'volatile.idmap.base': '600000000'},
        }])

        self.assertTrue(driver._all_project_idmap_resources_absent(
            self.driver.client, self.instance.uuid,
            self.assignment.base, self.assignment.size))

    def test_fallback_inventory_defaults_missing_empty_and_auto_size(self):
        self.driver.client.has_api_extension.return_value = False
        for configured_size in (None, '', 'auto'):
            with self.subTest(configured_size=configured_size):
                config = {'security.idmap.base': str(self.assignment.base)}
                if configured_size is not None:
                    config['security.idmap.size'] = configured_size
                self._set_all_project_inventory(instances=[{
                    'name': 'default-size',
                    'project': 'foreign-project',
                    'config': config,
                }])

                self.assertFalse(
                    driver._all_project_idmap_resources_absent(
                        self.driver.client, self.instance.uuid,
                        self.assignment.base, self.assignment.size))

    def test_fallback_inventory_uses_half_open_overlap(self):
        self.driver.client.has_api_extension.return_value = False
        self._set_all_project_inventory(instances=[{
            'name': 'partial-overlap',
            'project': 'foreign-project',
            'config': {
                'security.idmap.base': str(self.assignment.base - 1),
                'security.idmap.size': '2',
            },
        }])
        self.assertFalse(driver._all_project_idmap_resources_absent(
            self.driver.client, self.instance.uuid,
            self.assignment.base, self.assignment.size))

        self._set_all_project_inventory(instances=[{
            'name': 'adjacent-left',
            'project': 'foreign-project',
            'config': {
                'security.idmap.base': str(
                    self.assignment.base - self.assignment.size),
                'security.idmap.size': str(self.assignment.size),
            },
        }])
        self.assertTrue(driver._all_project_idmap_resources_absent(
            self.driver.client, self.instance.uuid,
            self.assignment.base, self.assignment.size))

    def test_fallback_inventory_malformed_range_never_proves_absence(self):
        self.driver.client.has_api_extension.return_value = False
        self._set_all_project_inventory(instances=[{
            'name': 'malformed-range',
            'project': 'foreign-project',
            'config': {
                'security.idmap.base': 'not-an-integer',
                'security.idmap.size': str(self.assignment.size),
            },
        }])

        self.assertFalse(driver._all_project_idmap_resources_absent(
            self.driver.client, self.instance.uuid,
            self.assignment.base, self.assignment.size))

    def test_fallback_inventory_rejects_invalid_resource_identity(self):
        self.driver.client.has_api_extension.return_value = False
        missing = object()
        valid = {
            'name': 'foreign-name',
            'project': 'foreign-project',
            'config': {},
        }
        for resource_type in ('instance', 'profile'):
            for field, value in (
                    ('name', missing), ('name', None), ('name', ''),
                    ('name', 1), ('project', missing), ('project', None),
                    ('project', ''), ('project', 1)):
                with self.subTest(
                        resource_type=resource_type, field=field, value=value):
                    record = dict(valid)
                    if value is missing:
                        record.pop(field)
                    else:
                        record[field] = value
                    self._set_all_project_inventory(**{
                        resource_type + 's': [record],
                    })

                    self.assertRaises(
                        driver.incus_idmap.IDMapIntegrityError,
                        driver._all_project_idmap_resources_absent,
                        self.driver.client, self.instance.uuid,
                        self.assignment.base, self.assignment.size)

    def test_fallback_inventory_rejects_malformed_config(self):
        self.driver.client.has_api_extension.return_value = False
        missing = object()
        valid = {
            'name': 'foreign-name',
            'project': 'foreign-project',
            'config': {},
        }
        for resource_type in ('instance', 'profile'):
            for value in (
                    missing, None, [], 'not-a-map',
                    {'bad-value': None}, {'bad-value': []}):
                with self.subTest(
                        resource_type=resource_type, value=value):
                    record = dict(valid)
                    if value is missing:
                        record.pop('config')
                    else:
                        record['config'] = value
                    self._set_all_project_inventory(**{
                        resource_type + 's': [record],
                    })

                    self.assertRaises(
                        driver.incus_idmap.IDMapIntegrityError,
                        driver._all_project_idmap_resources_absent,
                        self.driver.client, self.instance.uuid,
                        self.assignment.base, self.assignment.size)

    def test_fallback_inventory_accepts_legal_empty_config_shapes(self):
        self.driver.client.has_api_extension.return_value = False
        for resource_type, record in (
                ('instance', {
                    'name': 'foreign-instance',
                    'project': 'foreign-project',
                    'config': {},
                }),
                ('instance', {
                    'name': 'foreign-instance',
                    'project': 'foreign-project',
                    'config': {},
                    'expanded_config': {},
                }),
                ('profile', {
                    'name': 'foreign-profile',
                    'project': 'foreign-project',
                    'config': {},
                })):
            with self.subTest(resource_type=resource_type, record=record):
                self._set_all_project_inventory(**{
                    resource_type + 's': [record],
                })

                self.assertTrue(
                    driver._all_project_idmap_resources_absent(
                        self.driver.client, self.instance.uuid,
                        self.assignment.base, self.assignment.size))

    def test_fallback_inventory_rejects_malformed_expanded_config(self):
        self.driver.client.has_api_extension.return_value = False
        for value in (
                None, [], 'not-a-map',
                {'bad-value': None}, {'bad-value': []}):
            with self.subTest(value=value):
                self._set_all_project_inventory(instances=[{
                    'name': 'foreign-instance',
                    'project': 'foreign-project',
                    'config': {},
                    'expanded_config': value,
                }])

                self.assertRaises(
                    driver.incus_idmap.IDMapIntegrityError,
                    driver._all_project_idmap_resources_absent,
                    self.driver.client, self.instance.uuid,
                    self.assignment.base, self.assignment.size)

    def test_indexed_idmap_usage_blocks_foreign_resource(self):
        self.driver.client.has_api_extension.return_value = True
        response = (
            self.driver.client.api['idmap-usage'].get.return_value)
        response.json.return_value = {'metadata': [{
            'type': 'instance',
            'project': 'foreign-project',
            'name': 'foreign-instance',
        }]}

        self.assertFalse(driver._all_project_idmap_resources_absent(
            self.driver.client, self.instance.uuid,
            self.assignment.base, self.assignment.size))

        self.driver.client.api.instances.get.assert_not_called()
        self.driver.client.api.profiles.get.assert_not_called()
        self.driver.client.api.__getitem__.assert_called_with('idmap-usage')

    def test_indexed_idmap_usage_allows_only_owned_profile(self):
        self.driver.client.has_api_extension.return_value = True
        response = (
            self.driver.client.api['idmap-usage'].get.return_value)
        response.json.return_value = {'metadata': [{
            'type': 'profile',
            'project': 'nova',
            'name': self.instance.name,
        }]}

        self.assertTrue(driver._all_project_idmap_resources_absent(
            self.driver.client, self.instance.uuid,
            self.assignment.base, self.assignment.size,
            allowed_profile_name=self.instance.name))

    def test_indexed_idmap_usage_rejects_malformed_response(self):
        self.driver.client.has_api_extension.return_value = True
        response = (
            self.driver.client.api['idmap-usage'].get.return_value)
        response.json.return_value = {'metadata': [{
            'type': 'instance',
            'project': '',
            'name': 'foreign-instance',
        }]}

        self.assertRaises(
            driver.incus_idmap.IDMapIntegrityError,
            driver._all_project_idmap_resources_absent,
            self.driver.client, self.instance.uuid,
            self.assignment.base, self.assignment.size)

    def test_idmap_usage_sdk_path_uses_hyphen(self):
        api = incus_client._APINode(
            'http://incus.example/1.0', mock.sentinel.session)

        self.assertEqual(
            'http://incus.example/1.0/idmap-usage',
            api['idmap-usage']._api_endpoint)
        self.assertEqual(
            'http://incus.example/1.0/idmap_usage',
            api.idmap_usage._api_endpoint)

    @mock.patch.object(driver, '_profile_share_mounts', return_value=[])
    @mock.patch.object(driver, '_share_journal_records', return_value=[])
    @mock.patch.object(driver, '_volume_journal_records', return_value=[])
    @mock.patch.object(os.path, 'lexists', return_value=False)
    def test_same_generation_foreign_profile_blocks_cleanup_ack(
            self, lexists, volume_records, share_records, share_mounts):
        source_host = '10000000-0000-0000-0000-000000000003'
        assignment = dataclasses.replace(
            self.assignment, host_ids=(source_host, self.host_id))
        self.instance.system_metadata = {
            driver.IDMAP_BASE_METADATA_KEY: str(assignment.base),
            driver.IDMAP_SIZE_METADATA_KEY: str(assignment.size),
            driver.IDMAP_ALLOCATION_METADATA_KEY: assignment.allocation_id,
            driver.IDMAP_FINGERPRINT_METADATA_KEY: assignment.fingerprint,
        }
        token = '30000000-0000-0000-0000-000000000003'
        profile = mock.Mock(
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': self.instance.uuid,
                driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
            },
            devices={}, used_by=[])
        self.driver.client.instances.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        self.driver.client.profiles.get.return_value = profile
        self.driver.idmap_allocator.get.return_value = assignment
        self._set_all_project_inventory(profiles=[
            {
                'name': self.instance.name,
                'project': driver.CONF.incus.project,
                'config': {
                    'user.openstack.uuid': self.instance.uuid,
                    'security.idmap.base': str(assignment.base),
                    'security.idmap.size': str(assignment.size),
                },
            },
            {
                'name': 'foreign-profile',
                'project': 'foreign-project',
                'config': {
                    'security.idmap.base': str(assignment.base),
                    'security.idmap.size': str(assignment.size),
                },
            },
        ])

        self.assertRaises(
            driver.incus_idmap.IDMapIntegrityError,
            self.driver._acknowledge_cleanup_profile,
            self.instance, token)

        self.driver.idmap_allocator.retire_claim.assert_not_called()
        profile.save.assert_not_called()

    def test_source_ack_rejects_residual_third_host_claim(self):
        target_host = '10000000-0000-0000-0000-000000000003'
        third_host = '30000000-0000-0000-0000-000000000003'
        assignment = dataclasses.replace(
            self.assignment, host_ids=(self.host_id, third_host))
        self.instance.system_metadata = {
            driver.IDMAP_BASE_METADATA_KEY: str(assignment.base),
            driver.IDMAP_SIZE_METADATA_KEY: str(assignment.size),
            driver.IDMAP_ALLOCATION_METADATA_KEY: assignment.allocation_id,
            driver.IDMAP_FINGERPRINT_METADATA_KEY: assignment.fingerprint,
        }
        token = '40000000-0000-0000-0000-000000000004'
        profile = mock.Mock(
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': self.instance.uuid,
                driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
                driver.MIGRATION_CLEANUP_COMPLETE_KEY: token,
                driver.MIGRATION_IDMAP_RETIREMENT_KEY:
                    driver._idmap_retirement_proof(
                        assignment, target_host, token),
            },
            devices={}, used_by=[])
        self.driver.idmap_allocator.get.return_value = assignment

        self.assertRaises(
            exception.MigrationError,
            self.driver._validate_remote_cleanup_acknowledgement,
            profile, self.instance, token,
            assignment.base, assignment.size)

    def test_source_ack_accepts_exact_target_retirement_proof(self):
        target_host = '10000000-0000-0000-0000-000000000003'
        assignment = dataclasses.replace(
            self.assignment, host_ids=(self.host_id,))
        self.instance.system_metadata = {
            driver.IDMAP_BASE_METADATA_KEY: str(assignment.base),
            driver.IDMAP_SIZE_METADATA_KEY: str(assignment.size),
            driver.IDMAP_ALLOCATION_METADATA_KEY: assignment.allocation_id,
            driver.IDMAP_FINGERPRINT_METADATA_KEY: assignment.fingerprint,
        }
        token = '40000000-0000-0000-0000-000000000004'
        profile = mock.Mock(
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': self.instance.uuid,
                driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
                driver.MIGRATION_CLEANUP_COMPLETE_KEY: token,
                driver.MIGRATION_IDMAP_RETIREMENT_KEY:
                    driver._idmap_retirement_proof(
                        assignment, target_host, token),
            },
            devices={}, used_by=[])
        self.driver.idmap_allocator.get.return_value = assignment

        self.driver._validate_remote_cleanup_acknowledgement(
            profile, self.instance, token,
            assignment.base, assignment.size)


class ColdMigrationCleanupTokenTest(test.NoDBTestCase):

    def setUp(self):
        super().setUp()
        self.context = context.get_admin_context()
        self.instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            migration_context=mock.Mock(migration_id=42))
        self.token = '20000000-0000-0000-0000-000000000002'

    @mock.patch.object(driver.objects.Migration, 'get_by_id_and_instance')
    def test_uses_current_nova_migration_uuid(self, get_migration):
        get_migration.return_value = mock.Mock(uuid=self.token)

        token = driver._cold_migration_cleanup_token(
            self.context, self.instance)

        self.assertEqual(self.token, token)
        get_migration.assert_called_once_with(
            self.context, 42, self.instance.uuid)

    @mock.patch.object(driver.objects.Migration, 'get_by_id_and_instance')
    def test_missing_migration_context_fails_closed(self, get_migration):
        self.instance.migration_context = None

        self.assertRaises(
            exception.MigrationError,
            driver._cold_migration_cleanup_token,
            self.context, self.instance)

        get_migration.assert_not_called()

    @mock.patch.object(driver.objects.Migration, 'get_by_id_and_instance')
    def test_invalid_migration_id_fails_closed(self, get_migration):
        self.instance.migration_context.migration_id = 0

        self.assertRaises(
            exception.MigrationError,
            driver._cold_migration_cleanup_token,
            self.context, self.instance)

        get_migration.assert_not_called()

    @mock.patch.object(driver.objects.Migration, 'get_by_id_and_instance')
    def test_noncanonical_migration_uuid_fails_closed(self, get_migration):
        get_migration.return_value = mock.Mock(
            uuid='ABCDEF00-0000-0000-0000-000000000002')

        self.assertRaises(
            exception.MigrationError,
            driver._cold_migration_cleanup_token,
            self.context, self.instance)


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
                'id_map_base',
                'storage_materialization_attempt_v1',
                'storage_release_receipt_v2',
                'migration_stateful_shifted_root',
                'migration_live_shared_ceph_storage',
                'migration_shared_ceph_storage_ready_fence',
                'migration_attempt_fencing',
                'instance_storage_handover',
                'instance_storage_handover_proof',
            ],
            'environment': {
                'storage': 'zfs',
                'kernel_architecture': 'x86_64',
                'kernel_version': '6.8.0-test',
                'server_version': '7.2',
            }
        }
        self.client.profiles.get.return_value.devices = {}
        self.Client.return_value = self.client

        # Migration prechecks treat the empty Nova mapping set as
        # authoritative; individual Manila tests replace this return value.
        share_mappings_patcher = mock.patch.object(
            driver.objects.ShareMappingList, 'get_by_instance_uuid',
            return_value=[])

        cold_token_patcher = mock.patch.object(
            driver, '_cold_migration_cleanup_token',
            return_value='20000000-0000-0000-0000-000000000002')
        self.patchers = [share_mappings_patcher, cold_token_patcher]
        self.share_mappings = share_mappings_patcher.start()
        self.cold_migration_cleanup_token = cold_token_patcher.start()

        cinder_api_patcher = mock.patch.object(driver.cinder, 'API')
        self.patchers.append(cinder_api_patcher)
        self.CinderAPI = cinder_api_patcher.start()
        self.volume_api = self.CinderAPI.return_value
        self.volume_api.get_volume_encryption_metadata.return_value = {}

        CONF_patcher = mock.patch('nova.virt.incus.driver.CONF')
        self.patchers.append(CONF_patcher)
        self.CONF = CONF_patcher.start()
        self.instances_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.instances_dir.cleanup)
        self.CONF.instances_path = self.instances_dir.name
        self.CONF.state_path = self.instances_dir.name
        self.CONF.my_ip = '0.0.0.0'
        self.CONF.config_drive_format = 'iso9660'
        self.CONF.force_config_drive = False
        self.CONF.incus.storage_pool = None
        self.CONF.incus.project = 'nova'
        self.CONF.incus.migration_recovery_interval = 60
        self.CONF.incus.shared_storage_pool_capacity_gb = None
        self.CONF.incus.root_storage_pools = {}
        self.CONF.incus.root_storage_pool_resource_classes = {}
        self.CONF.incus.boot_from_volume_storage_pools = {}
        self.CONF.incus.maximum_user_data_kb = 1024
        self.CONF.incus.shared_root_storage_pool_capacities_gb = {}
        self.CONF.incus.allow_cold_migration = False
        self.CONF.incus.allow_live_migration = False
        self.CONF.incus.allow_bfv_evacuate = False
        self.CONF.incus.idmap_allocator_endpoint = 'http://etcd:2379'
        self.CONF.incus.idmap_allocator_namespace = 'unit-test'
        self.CONF.incus.idmap_allocator_base = 500000000
        self.CONF.incus.idmap_allocator_size = 65536
        self.CONF.incus.idmap_allocator_count = 1000
        self.CONF.incus.idmap_allocator_timeout = 5
        self.CONF.incus.idmap_allocator_audit_interval = 60
        self.CONF.incus.idmap_allocator_allow_insecure = False
        self.CONF.incus.idmap_allocator_ca_cert = None
        self.CONF.incus.idmap_allocator_client_cert = None
        self.CONF.incus.idmap_allocator_client_key = None
        self.CONF.incus.idmap_allocator_username = None
        self.CONF.incus.idmap_allocator_password_file = None
        self.CONF.incus.migration_address = None
        self.CONF.incus.migration_tls_ca = None
        self.CONF.incus.migration_tls_ca_by_server = {}
        self.CONF.incus.migration_preflight_server_names = {}
        self.CONF.incus.migration_finish_retries = 3
        self.CONF.incus.migration_finish_retry_interval = 0
        self.CONF.incus.migration_auto_recovery = True
        self.CONF.incus.configdrive_migration_max_bytes = 8 * 1024 * 1024
        self.CONF.incus.configdrive_migration_max_files = 512
        self.CONF.incus.volume_use_multipath = False
        self.CONF.incus.volume_enforce_multipath = False
        self.CONF.incus.num_volume_scan_tries = 3
        self.CONF.incus.data_volume_mount_fuse = 'ext4=fuse2fs'
        self.CONF.incus.enable_manila_shares = False
        self.CONF.serial_console.enabled = False
        self.CONF.serial_console.proxyclient_address = '127.0.0.1'

        allocator_patcher = mock.patch.object(
            driver.incus_idmap, 'IDMapAllocator')
        self.patchers.append(allocator_patcher)
        self.IDMapAllocator = allocator_patcher.start()
        self.idmap_allocator = self.IDMapAllocator.return_value
        self.idmap_allocator.get_release_intent.return_value = None

        node_uuid_patcher = mock.patch.object(
            driver.virt_node, 'read_local_node_uuid',
            return_value='20000000-0000-0000-0000-000000000002')
        self.patchers.append(node_uuid_patcher)
        self.read_local_node_uuid = node_uuid_patcher.start()

        ensure_idmap_patcher = mock.patch.object(
            driver.IncusDriver, '_ensure_instance_idmap')
        self.patchers.append(ensure_idmap_patcher)
        self.ensure_instance_idmap = ensure_idmap_patcher.start()

        # Legacy driver workflow tests exercise Nova/Incus orchestration, not
        # the deployment-wide ID map transaction.  Keep that transaction
        # isolated here; IncusIDMapDriverTest covers its exact v3 state and
        # identity protocol without permissive workflow mocks.
        begin_materialization_patcher = mock.patch.object(
            driver.IncusDriver, '_begin_idmap_materialization',
            return_value=None)
        self.patchers.append(begin_materialization_patcher)
        self.begin_idmap_materialization = (
            begin_materialization_patcher.start())

        spawn_attempt_patcher = mock.patch.object(
            driver.IncusDriver, '_create_spawn_preflight_attempt',
            return_value=None)
        self.patchers.append(spawn_attempt_patcher)
        self.create_spawn_preflight_attempt = spawn_attempt_patcher.start()

        start_idmap_patcher = mock.patch.object(
            driver.IncusDriver, '_ensure_instance_idmap_before_start')
        self.patchers.append(start_idmap_patcher)
        self.ensure_start_idmap = start_idmap_patcher.start()

        # XXX: rockstar (03 Nov 2016) - This should be removed once
        # everything is where it should live.
        CONF2_patcher = mock.patch('nova.virt.incus.driver.nova.conf.CONF')
        self.patchers.append(CONF2_patcher)
        self.CONF2 = CONF2_patcher.start()
        self.CONF2.incus.root_dir = '/incus'
        self.CONF2.incus.storage_pool = None
        # Real path: the config-drive mountpoint is created under
        # instances_path, so it must not be a fictional directory here.
        self.CONF2.instances_path = self.instances_dir.name

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

        # Driver workflow tests isolate Nova orchestration from the Incus
        # migration-attempt REST contract. Dedicated tests below exercise the
        # exact token binding, idmap fencing, abort and lost-response paths.
        attempt_active = {
            'state': 'active', 'finished': False,
            'operation_uuid': '',
        }
        attempt_committed = {
            'state': 'committed', 'finished': True,
            'operation_uuid': '20000000-0000-0000-0000-000000000002',
        }
        attempt_aborted = {
            'state': 'aborted', 'finished': True,
            'operation_uuid': '',
        }
        attempt_patches = {
            '_register_migration_attempt': mock.Mock(
                return_value=attempt_active),
            '_get_migration_attempt': mock.Mock(
                return_value=attempt_committed),
            '_abort_migration_attempt': mock.Mock(
                return_value=attempt_aborted),
            '_wait_migration_attempt_finished': mock.Mock(
                return_value=attempt_aborted),
            '_retire_migration_attempt': mock.Mock(),
            '_finalize_committed_migration_attempt': mock.Mock(),
        }
        for name, replacement in attempt_patches.items():
            patcher = mock.patch.object(driver, name, replacement)
            self.patchers.append(patcher)
            setattr(self, name, patcher.start())

        def create_migration_target(
                client, config, instance, attempt_token,
                idmap_base, idmap_size, operation_started=None):
            return client.instances.create(config, wait=True), None

        create_target_patcher = mock.patch.object(
            driver, '_create_migration_target',
            side_effect=create_migration_target)
        self.patchers.append(create_target_patcher)
        self.create_migration_target = create_target_patcher.start()

        # NOTE: mock out fileutils to ensure that unit tests don't try
        #       to manipulate the filesystem (breaks in package builds).
        # This used to be a bare module attribute assignment that was never
        # restored, leaking the mock into every later test class in the
        # same process.
        fileutils_patcher = mock.patch.object(driver, 'fileutils')
        self.patchers.append(fileutils_patcher)
        fileutils_patcher.start()

    def tearDown(self):
        super(IncusDriverTest, self).tearDown()
        self.Client_patcher.stop()
        for patcher in self.patchers:
            patcher.stop()

    def _start_concurrent_calls(self, action, count=8):
        barrier = threading.Barrier(count + 1)
        results = [None] * count
        errors = [None] * count

        def call(index):
            try:
                barrier.wait(timeout=5)
                results[index] = action()
            except Exception as exc:
                errors[index] = exc

        threads = [
            threading.Thread(target=call, args=(index,))
            for index in range(count)
        ]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        return threads, results, errors

    def _join_concurrent_calls(self, threads, results, errors):
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(
            [thread for thread in threads if thread.is_alive()],
            'concurrent cache calls did not complete')
        self.assertEqual([None] * len(errors), errors)
        return results

    def _configure_exact_idmap_release(
            self, incus_driver, instance, *resources,
            storage_driver='ceph', cleanup_disposition='delete',
            outcome=None, idmap_base=500000000, idmap_size=65536,
            host_id='20000000-0000-0000-0000-000000000002',
            materialization_id='30000000-0000-0000-0000-000000000003'):
        """Install one protocol-realistic A/H/T/U release fixture."""
        allocation_id = '10000000-0000-0000-0000-000000000001'
        assignment = driver.incus_idmap.IDMapAssignment(
            instance_uuid=instance.uuid,
            base=idmap_base,
            size=idmap_size,
            slot=0,
            allocation_id=allocation_id,
            fingerprint='a' * 64,
            host_ids=(host_id,))
        claim = driver.incus_idmap.IDMapHostClaim(
            host_id=host_id,
            materialization_id=materialization_id,
            instance_uuid=instance.uuid,
            base=assignment.base,
            size=assignment.size,
            slot=assignment.slot,
            allocation_id=assignment.allocation_id,
            fingerprint=assignment.fingerprint,
            state='committed')
        metadata = (
            dict(instance.system_metadata or {})
            if instance.obj_attr_is_set('system_metadata') else {})
        metadata.update({
            driver.IDMAP_BASE_METADATA_KEY: str(assignment.base),
            driver.IDMAP_SIZE_METADATA_KEY: str(assignment.size),
            driver.IDMAP_ALLOCATION_METADATA_KEY: assignment.allocation_id,
            driver.IDMAP_FINGERPRINT_METADATA_KEY: assignment.fingerprint,
        })
        instance.system_metadata = metadata
        local_config = {
            'user.openstack.uuid': instance.uuid,
            driver.IDMAP_ALLOCATION_CONFIG_KEY: assignment.allocation_id,
            driver.IDMAP_COMPUTE_CONFIG_KEY: host_id,
            driver.IDMAP_MATERIALIZATION_CONFIG_KEY: materialization_id,
            'security.idmap.base': str(assignment.base),
            'security.idmap.size': str(assignment.size),
        }
        for resource in resources:
            config = dict(
                resource.config if isinstance(resource.config, dict) else {})
            config.update(local_config)
            resource.config = config

        if outcome is None:
            if storage_driver == 'cephext':
                outcome = 'normalized'
            elif cleanup_disposition == 'detach':
                outcome = 'detached'
            else:
                outcome = 'deleted'
        binding = driver.incus_storage_protocol.StorageMaterializationBinding(
            token=materialization_id,
            allocation_id=assignment.allocation_id,
            compute_id=host_id,
            owner=instance.uuid,
            project='nova',
            instance_name=instance.name,
            idmap_base=assignment.base,
            idmap_size=assignment.size,
            storage_driver=storage_driver,
            storage_pool='root-pool',
            storage_volume='nova_{}'.format(instance.name),
            cleanup_disposition=cleanup_disposition,
            rbd_image='container_nova_{}'.format(instance.name))
        receipt = driver.incus_idmap.IDMapRootfsReleaseReceipt(
            token=binding.token,
            allocation_id=binding.allocation_id,
            compute_id=binding.compute_id,
            materialization_id=binding.token,
            owner=binding.owner,
            project=binding.project,
            instance_name=binding.instance_name,
            idmap_base=binding.idmap_base,
            idmap_size=binding.idmap_size,
            storage_driver=binding.storage_driver,
            storage_pool=binding.storage_pool,
            storage_volume=binding.storage_volume,
            rbd_image=binding.rbd_image,
            storage_identity='rbd_data.1234567890abcdef',
            baseline_clean=True,
            cleanup_disposition=binding.cleanup_disposition,
            outcome=outcome,
            state='complete',
            digest='',
            created_at=10,
            completed_at=11)
        proof = driver.incus_idmap.IDMapRootfsReleaseProof(
            **receipt.__dict__)
        receipt = dataclasses.replace(
            receipt,
            digest=driver.incus_idmap.rootfs_release_proof_digest(proof))
        proof = driver.incus_idmap.validate_rootfs_release_receipt(receipt)
        cleaned = dataclasses.replace(claim, state='cleaned', proof=proof)

        self.ensure_instance_idmap.return_value = assignment
        self.idmap_allocator.get.return_value = assignment
        self.idmap_allocator.get_host_claim.return_value = claim
        self.idmap_allocator.get_release_intent.return_value = None
        self.idmap_allocator.record_rootfs_release_proof.return_value = cleaned
        self.idmap_allocator.retire_claim.return_value = dataclasses.replace(
            assignment, host_ids=())
        ownership = mock.Mock(
            spec=driver.incus_storage_protocol.StorageOwnershipClient)
        ownership.discover_release_receipt.return_value = (binding, receipt)
        incus_driver.storage_ownership = ownership
        incus_driver._storage_ownership_client = mock.Mock(
            return_value=ownership)

        def delete_with_release_receipt(
                container, target_instance, target_claim, client=None):
            observed_assignment, observed_claim = (
                incus_driver._instance_local_idmap_claim(
                    target_instance, container))
            self.assertEqual(assignment, observed_assignment)
            self.assertEqual(claim, observed_claim)
            self.assertEqual(claim, target_claim)
            return container.delete(wait=True)

        tokenized_delete = mock.Mock(side_effect=delete_with_release_receipt)
        incus_driver._delete_instance_with_rootfs_release_receipt = (
            tokenized_delete)
        return (assignment, claim, binding, receipt, ownership,
                tokenized_delete)

    def test_init_host(self):
        """init_host initializes the pylxd Client."""
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertEqual(
            [mock.call(self.CONF), mock.call(self.CONF, project=None)],
            self.Client.call_args_list)
        self.assertEqual(self.client, incus_driver.client)
        self.assertEqual(self.client, incus_driver.inventory_client)
        self.IDMapAllocator.assert_called_once_with(
            endpoint='http://etcd:2379', namespace='unit-test',
            base=500000000, size=65536, count=1000, timeout=5,
            ca_cert=None, cert_cert=None, cert_key=None,
            username=None, password_file=None,
            allow_insecure=False, audit_lease_ttl=180)
        self.idmap_allocator.initialize.assert_called_once_with()
        self.idmap_allocator.run_coordinated_audit.assert_called_once_with(
            full=True)

    def test_init_host_requires_global_idmap_for_migration(self):
        self.CONF.incus.idmap_allocator_endpoint = None
        self.CONF.incus.allow_cold_migration = True
        incus_driver = driver.IncusDriver(None)

        self.assertRaisesRegex(
            exception.InvalidConfiguration, 'idmap_allocator_endpoint',
            incus_driver.init_host, None)

    def test_init_host_requires_release_receipts_with_global_idmap(self):
        self.client.host_info['api_extensions'].remove(
            'storage_release_receipt_v2')
        incus_driver = driver.IncusDriver(None)

        self.assertRaisesRegex(
            exception.InvalidConfiguration, 'storage_release_receipt',
            incus_driver.init_host, None)

    def test_init_host_keeps_existing_power_operations_when_etcd_is_down(self):
        self.idmap_allocator.initialize.side_effect = (
            driver.incus_idmap.IDMapBackendError(reason='unavailable'))
        incus_driver = driver.IncusDriver(None)

        incus_driver.init_host(None)

        self.assertIs(self.idmap_allocator, incus_driver.idmap_allocator)

    def test_init_host_rejects_corrupt_idmap_registry(self):
        self.idmap_allocator.run_coordinated_audit.side_effect = (
            driver.incus_idmap.IDMapIntegrityError(
                reason='orphan allocation'))
        incus_driver = driver.IncusDriver(None)

        self.assertRaises(
            driver.incus_idmap.IDMapIntegrityError,
            incus_driver.init_host, None)

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

    @mock.patch.object(
        driver.incus_privsep, 'validate_gnu_timeout',
        side_effect=RuntimeError('BusyBox timeout'))
    def test_init_host_rejects_non_gnu_timeout_for_manila(self, validate):
        self.CONF.incus.enable_manila_shares = True
        incus_driver = driver.IncusDriver(None)

        self.assertRaisesRegex(
            exception.InvalidConfiguration, 'GNU coreutils timeout',
            incus_driver.init_host, None)

        validate.assert_called_once_with()
        self.Client.assert_not_called()

    def _configure_bfv_pool(self, cinder_pool='cinder-volumes',
                            pool_name='cinder'):
        """Declare the BFV mapping a test depends on.

        init_host verifies that every mapped pool exists on this compute
        and is a cephext pool backed by the named Cinder RBD pool, so a
        test exercising boot-from-volume has to say which mapping it
        assumes instead of leaning on a permissive mock.
        """
        self.CONF.incus.boot_from_volume_storage_pools = {
            cinder_pool: pool_name}
        pool = self.client.storage_pools.get.return_value
        pool.driver = 'cephext'
        pool.config = {'source': cinder_pool}
        return pool

    def _bfv_pool(self, source='cinder-volumes', pool_driver='cephext'):
        return mock.Mock(driver=pool_driver, config={'source': source})

    def test_init_host_accepts_a_correctly_backed_bfv_pool(self):
        self.CONF.incus.boot_from_volume_storage_pools = {
            'cinder-volumes': 'cinder-bfv'}
        self.client.storage_pools.get.return_value = self._bfv_pool()
        incus_driver = driver.IncusDriver(None)

        incus_driver.init_host(None)

        self.client.storage_pools.get.assert_any_call('cinder-bfv')

    def test_init_host_rejects_a_bfv_pool_that_does_not_exist(self):
        """Configuration that names a pool must prove it at startup.

        Nothing else reads the mapping until an instance is already being
        built here, so without this the compute reports up and accepts
        scheduling it cannot honour.
        """
        self.CONF.incus.boot_from_volume_storage_pools = {
            'cinder-volumes': 'never-created'}
        self.client.storage_pools.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        incus_driver = driver.IncusDriver(None)

        self.assertRaisesRegex(
            exception.InvalidConfiguration, 'does not exist on this compute',
            incus_driver.init_host, None)

    def test_init_host_rejects_a_bfv_pool_backed_by_another_cinder_pool(self):
        # Worse than a missing pool: it resolves, then operates on another
        # backend's images.
        self.CONF.incus.boot_from_volume_storage_pools = {
            'nvme-rep3': 'cinder-nvme-bfv'}
        self.client.storage_pools.get.return_value = self._bfv_pool(
            source='cinder-volumes')
        incus_driver = driver.IncusDriver(None)

        self.assertRaisesRegex(
            exception.InvalidConfiguration, 'is backed by',
            incus_driver.init_host, None)

    def test_init_host_rejects_a_bfv_pool_with_the_wrong_driver(self):
        self.CONF.incus.boot_from_volume_storage_pools = {
            'cinder-volumes': 'cinder-bfv'}
        self.client.storage_pools.get.return_value = self._bfv_pool(
            pool_driver='ceph')
        incus_driver = driver.IncusDriver(None)

        self.assertRaisesRegex(
            exception.InvalidConfiguration, 'require cephext',
            incus_driver.init_host, None)

    def test_init_host_rejects_duplicate_root_pool_resource_class(self):
        self.CONF.incus.root_storage_pools = {
            'fast': 'fast-pool',
            'durable': 'durable-pool',
        }
        self.CONF.incus.root_storage_pool_resource_classes = {
            'fast': 'CUSTOM_INCUS_ROOT_DISK_GB',
            'durable': 'CUSTOM_INCUS_ROOT_DISK_GB',
        }
        self.client.storage_pools.get.return_value = mock.Mock(
            driver='zfs', config={'source': 'tank/incus'})
        incus_driver = driver.IncusDriver(None)

        self.assertRaisesRegex(
            exception.InvalidConfiguration, 'reuse Placement resource class',
            incus_driver.init_host, None)

    def test_init_host_rejects_duplicate_physical_root_pool(self):
        self.CONF.incus.root_storage_pools = {
            'fast': 'ceph-alias-a',
            'durable': 'ceph-alias-b',
        }
        self.CONF.incus.root_storage_pool_resource_classes = {
            'fast': 'CUSTOM_INCUS_FAST_DISK_GB',
            'durable': 'CUSTOM_INCUS_DURABLE_DISK_GB',
        }
        self.CONF.incus.shared_root_storage_pool_capacities_gb = {
            'fast': '100',
            'durable': '100',
        }
        self.client.storage_pools.get.return_value = mock.Mock(
            driver='ceph',
            config={
                'source': 'incus-rootfs',
                'ceph.cluster_name': 'ceph',
            })
        incus_driver = driver.IncusDriver(None)

        self.assertRaisesRegex(
            exception.InvalidConfiguration, 'same physical pool',
            incus_driver.init_host, None)

    def test_init_host_rejects_default_pool_double_accounting(self):
        self.CONF.incus.storage_pool = 'ceph-root'
        self.CONF.incus.shared_storage_pool_capacity_gb = 100
        self.CONF.incus.root_storage_pools = {
            'durable': 'ceph-root-alias',
        }
        self.CONF.incus.root_storage_pool_resource_classes = {
            'durable': 'CUSTOM_INCUS_DURABLE_DISK_GB',
        }
        self.CONF.incus.shared_root_storage_pool_capacities_gb = {
            'durable': '100',
        }
        self.client.storage_pools.get.return_value = mock.Mock(
            driver='ceph',
            config={
                'source': 'incus-rootfs',
                'ceph.cluster_name': 'ceph',
            })
        incus_driver = driver.IncusDriver(None)

        self.assertRaisesRegex(
            exception.InvalidConfiguration, 'reports the default',
            incus_driver.init_host, None)

    def test_init_host_rejects_non_default_pool_without_inventory(self):
        self.CONF.incus.storage_pool = 'ceph-root'
        self.CONF.incus.root_storage_pools = {
            'local-nvme': 'local-zfs',
        }

        def get_pool(name):
            if name == 'ceph-root':
                return mock.Mock(
                    driver='ceph',
                    config={
                        'source': 'incus-rootfs',
                        'ceph.cluster_name': 'ceph',
                    })
            return mock.Mock(driver='zfs', config={'source': 'tank/incus'})

        self.client.storage_pools.get.side_effect = get_pool
        incus_driver = driver.IncusDriver(None)

        self.assertRaisesRegex(
            exception.InvalidConfiguration,
            'requires a dedicated Placement capacity resource class',
            incus_driver.init_host, None)

    def test_init_host_accepts_default_pool_alias_without_inventory(self):
        self.CONF.incus.storage_pool = 'ceph-root'
        self.CONF.incus.root_storage_pools = {
            'durable': 'ceph-root-alias',
        }
        self.client.storage_pools.get.return_value = mock.Mock(
            driver='ceph',
            config={
                'source': 'incus-rootfs',
                'ceph.cluster_name': 'ceph',
            })
        incus_driver = driver.IncusDriver(None)

        incus_driver.init_host(None)

    def test_init_host_rejects_unused_shared_pool_budget(self):
        self.CONF.incus.shared_root_storage_pool_capacities_gb = {
            'durable': '100',
        }
        incus_driver = driver.IncusDriver(None)

        self.assertRaisesRegex(
            exception.InvalidConfiguration, 'without a capacity resource',
            incus_driver.init_host, None)

    def test_power_off_invalidates_mutable_inventory_caches(self):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        container = self.client.instances.get.return_value
        container.status = 'Running'
        incus_driver._instance_inventory_cache = mock.sentinel.instances
        incus_driver._metric_devices_cache = mock.sentinel.devices
        incus_driver._disk_metrics_cache = mock.sentinel.metrics

        def stop(timeout=0, force=True, wait=True):
            self.assertIsNone(incus_driver._instance_inventory_cache)
            incus_driver._instance_inventory_cache = (
                0, {'stale': mock.sentinel.container})
            incus_driver._metric_devices_cache = mock.sentinel.stale_devices
            incus_driver._disk_metrics_cache = mock.sentinel.stale_metrics

        container.stop.side_effect = stop

        incus_driver.power_off(instance)

        self.assertIsNone(incus_driver._instance_inventory_cache)
        self.assertIsNone(incus_driver._metric_devices_cache)
        self.assertIsNone(incus_driver._disk_metrics_cache)

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
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        container = mock.Mock(
            type='container', status='Running',
            status_code=100)
        container.name = instance.name
        self.client.instances.all.return_value = [container]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        info = incus_driver.get_info(instance)

        self.assertEqual(power_state.RUNNING, info.state)
        self.client.instances.all.assert_called_once_with(recursion=1)
        self.client.instances.get.assert_not_called()
        container.state.assert_not_called()

    def test_get_info_stopped_does_not_query_runtime_state(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        container = mock.Mock(
            type='container', status='Stopped',
            status_code=102)
        container.name = instance.name
        self.client.instances.all.return_value = [container]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        info = incus_driver.get_info(instance)

        self.assertEqual(power_state.SHUTDOWN, info.state)
        container.state.assert_not_called()

    def test_get_info_reuses_expanded_inventory(self):
        ctx = context.get_admin_context()
        first_instance = fake_instance.fake_instance_obj(
            ctx, name='first', id=1)
        second_instance = fake_instance.fake_instance_obj(
            ctx, name='second', id=2)
        first = mock.Mock(
            type='container', status='Running',
            status_code=100)
        first.name = first_instance.name
        second = mock.Mock(
            type='container', status='Stopped',
            status_code=102)
        second.name = second_instance.name
        self.client.instances.all.return_value = [first, second]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        first_info = incus_driver.get_info(first_instance)
        second_info = incus_driver.get_info(second_instance)

        self.assertEqual(power_state.RUNNING, first_info.state)
        self.assertEqual(power_state.SHUTDOWN, second_info.state)
        self.client.instances.all.assert_called_once_with(recursion=1)

    def test_instance_inventory_cache_single_flight(self):
        item = mock.Mock(type='container')
        item.name = 'instance-1'
        fetch_started = threading.Event()
        release_fetch = threading.Event()

        def fetch(recursion):
            self.assertEqual(1, recursion)
            fetch_started.set()
            if not release_fetch.wait(timeout=5):
                raise RuntimeError('inventory fetch was not released')
            return [item]

        self.client.instances.all.side_effect = fetch
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        threads, results, errors = self._start_concurrent_calls(
            incus_driver._get_instance_inventory_snapshot)
        self.assertTrue(fetch_started.wait(timeout=5))
        release_fetch.set()

        results = self._join_concurrent_calls(threads, results, errors)

        self.assertEqual(1, self.client.instances.all.call_count)
        self.assertTrue(all(result is results[0] for result in results))
        self.assertIs(item, results[0]['instance-1'])

    def test_metric_inventory_abba_invalidation_does_not_deadlock(self):
        stale = mock.Mock(
            type='container',
            expanded_devices={'stale': {'type': 'disk'}})
        stale.name = 'instance-1'
        fresh = mock.Mock(
            type='container',
            expanded_devices={'fresh': {'type': 'disk'}})
        fresh.name = 'instance-1'
        fetch_started = threading.Event()
        release_fetch = threading.Event()
        invalidated = threading.Event()
        fetch_count = 0
        fetch_count_lock = threading.Lock()

        def fetch(recursion):
            nonlocal fetch_count
            self.assertEqual(1, recursion)
            with fetch_count_lock:
                fetch_count += 1
                current = fetch_count
            if current == 1:
                fetch_started.set()
                if not release_fetch.wait(timeout=5):
                    raise RuntimeError('inventory fetch was not released')
                return [stale]
            return [fresh]

        self.client.instances.all.side_effect = fetch
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        results = []
        errors = []

        def read_metrics():
            try:
                results.append(
                    incus_driver._get_metric_devices_snapshot())
            except Exception as exc:
                errors.append(exc)

        reader = threading.Thread(target=read_metrics)
        reader.start()
        self.assertTrue(fetch_started.wait(timeout=5))

        def invalidate():
            incus_driver._invalidate_instance_inventory_cache()
            invalidated.set()

        invalidator = threading.Thread(target=invalidate)
        invalidator.start()
        # These two paths used to acquire metric->inventory and
        # inventory->metric respectively.  Invalidation must finish while
        # the Incus inventory request is still blocked.
        completed_without_fetch = invalidated.wait(timeout=1)
        release_fetch.set()
        invalidator.join(timeout=5)
        reader.join(timeout=5)

        self.assertTrue(
            completed_without_fetch,
            'cache invalidation waited for an in-flight inventory fetch')
        self.assertFalse(invalidator.is_alive())
        self.assertFalse(reader.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(2, fetch_count)
        self.assertEqual(
            {'fresh': {'type': 'disk'}},
            results[0]['instance-1']['devices'])
        self.assertIs(
            fresh,
            incus_driver._instance_inventory_cache[1]['instance-1'])
        self.assertEqual(
            {'fresh': {'type': 'disk'}},
            incus_driver._metric_devices_cache[1][
                'instance-1']['devices'])

    def test_get_info_without_cache_reads_exact_instance(self):
        container = mock.Mock(status='Running', status_code=100)
        self.client.instances.get.return_value = container
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        info = incus_driver.get_info(instance, use_cache=False)

        self.assertEqual(power_state.RUNNING, info.state)
        self.client.instances.get.assert_called_once_with(instance.name)
        self.client.instances.all.assert_not_called()
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

    @mock.patch.object(driver.configdrive, 'required_by', return_value=False)
    @mock.patch.object(driver.IncusDriver, '_get_diagnostics_data')
    def test_get_instance_diagnostics_uses_standard_rpc_enum(
            self, get_data, required_by):
        get_data.return_value = {
            'state': power_state.RUNNING,
            'uptime': 42,
            'memory_maximum': 2 * units.Mi,
            'memory_used': units.Mi,
            'cpu_time': 123,
            'disk_count': 0,
            'nics': [],
        }
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test',
            image_ref='image-id', system_metadata={})
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        result = incus_driver.get_instance_diagnostics(instance)

        self.assertEqual(
            driver.obj_fields.HypervisorDriver.LIBVIRT, result.driver)
        self.assertEqual('incus', result.hypervisor)

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

    @mock.patch('nova.virt.incus.driver._incus_all_disk_metrics')
    def test_disk_metrics_cache_single_flight(self, fetch_metrics):
        fetch_started = threading.Event()
        release_fetch = threading.Event()
        expected = {'instance-1': {'rbd1': {'rd_req': 1}}}

        def fetch(client):
            self.assertIs(self.client, client)
            fetch_started.set()
            if not release_fetch.wait(timeout=5):
                raise RuntimeError('metrics fetch was not released')
            return expected

        fetch_metrics.side_effect = fetch
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        threads, results, errors = self._start_concurrent_calls(
            incus_driver._get_disk_metrics_snapshot)
        self.assertTrue(fetch_started.wait(timeout=5))
        release_fetch.set()

        results = self._join_concurrent_calls(threads, results, errors)

        fetch_metrics.assert_called_once_with(self.client)
        self.assertTrue(all(result is expected for result in results))

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

    @mock.patch('nova.virt.incus.driver.glob.glob')
    def test_mapped_cinder_rbd_devices_indexes_all_pools_once(self, glob):
        first = (
            '/dev/rbd/fast/'
            'volume-8231d2e8-1111-2222-3333-444444444444')
        second = (
            '/dev/rbd/durable/'
            'volume-9331d2e8-1111-2222-3333-444444444444')
        glob.return_value = [first, second, '/dev/rbd/fast/not-a-volume']

        result = driver._mapped_cinder_rbd_devices()

        self.assertEqual({
            os.path.basename(first): [first],
            os.path.basename(second): [second],
        }, result)
        glob.assert_called_once_with('/dev/rbd/*/volume-*')

    @mock.patch('nova.virt.incus.driver._incus_all_disk_metrics')
    @mock.patch('nova.virt.incus.driver._disk_metric_device',
                return_value='rbd1')
    def test_block_stats(self, metric_device, disk_metrics):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test')
        disk_metrics.return_value = {
            instance.name: {
                'rbd1': {
                    'rd_req': 3,
                    'rd_bytes': 4096,
                    'wr_req': 4,
                    'wr_bytes': 8192,
                },
            },
        }
        profile = {'devices': {}}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._get_metric_instance_devices_snapshot = mock.Mock(
            return_value=profile)

        result = incus_driver.block_stats(instance, 'vda')

        self.assertEqual([3, 4096, 4, 8192, 0], result)
        metric_device.assert_called_once_with(profile, instance, 'vda')
        disk_metrics.assert_called_once_with(self.client)

    def test_metric_instance_devices_uses_exact_cached_read(self):
        container = self.client.instances.get.return_value
        container.expanded_devices = {
            'root': {'type': 'disk', 'path': '/'}}
        container.devices = {}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        first = incus_driver._get_metric_instance_devices_snapshot('test')
        second = incus_driver._get_metric_instance_devices_snapshot('test')

        self.assertEqual(first, second)
        self.assertEqual(container.expanded_devices, first['devices'])
        self.client.instances.get.assert_called_once_with('test')
        self.client.instances.all.assert_not_called()

    def test_metric_devices_cache_single_flight(self):
        item = mock.Mock(
            expanded_devices={'root': {'type': 'disk', 'path': '/'}})
        item.name = 'instance-1'
        fetch_started = threading.Event()
        release_fetch = threading.Event()
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        def fetch():
            fetch_started.set()
            if not release_fetch.wait(timeout=5):
                raise RuntimeError('device inventory fetch was not released')
            return {item.name: item}

        incus_driver._get_instance_inventory_snapshot = mock.Mock(
            side_effect=fetch)
        threads, results, errors = self._start_concurrent_calls(
            incus_driver._get_metric_devices_snapshot)
        self.assertTrue(fetch_started.wait(timeout=5))
        release_fetch.set()

        results = self._join_concurrent_calls(threads, results, errors)

        incus_driver._get_instance_inventory_snapshot.assert_called_once_with()
        self.assertTrue(all(result is results[0] for result in results))
        self.assertEqual(
            item.expanded_devices,
            results[0][item.name]['devices'])

    def test_metric_instance_devices_cache_single_flight(self):
        item = mock.Mock(
            expanded_devices={'root': {'type': 'disk', 'path': '/'}})
        fetch_started = threading.Event()
        release_fetch = threading.Event()

        def fetch(instance_name):
            self.assertEqual('instance-1', instance_name)
            fetch_started.set()
            if not release_fetch.wait(timeout=5):
                raise RuntimeError('exact device fetch was not released')
            return item

        self.client.instances.get.side_effect = fetch
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        threads, results, errors = self._start_concurrent_calls(
            lambda: incus_driver._get_metric_instance_devices_snapshot(
                'instance-1'))
        self.assertTrue(fetch_started.wait(timeout=5))
        release_fetch.set()

        results = self._join_concurrent_calls(threads, results, errors)

        self.client.instances.get.assert_called_once_with('instance-1')
        self.assertTrue(all(result is results[0] for result in results))
        self.assertEqual(item.expanded_devices, results[0]['devices'])

    def test_metric_instance_devices_reuses_fresh_bulk_snapshot(self):
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        expected = {'devices': {'root': {'type': 'disk', 'path': '/'}}}
        incus_driver._metric_devices_cache = (
            time.monotonic(), {'test': expected})

        result = incus_driver._get_metric_instance_devices_snapshot('test')

        self.assertIs(expected, result)
        self.client.instances.get.assert_not_called()

    @mock.patch('nova.virt.incus.driver._mapped_cinder_rbd_devices',
                return_value={})
    @mock.patch('nova.virt.incus.driver._incus_all_disk_metrics')
    @mock.patch('nova.virt.incus.driver._disk_metric_device',
                return_value='rbd1')
    def test_get_all_volume_usage_fetches_metrics_once(
            self, metric_device, all_disk_metrics, mapped_rbd_devices):
        counters = {
            'rd_req': 3,
            'rd_bytes': 4096,
            'wr_req': 4,
            'wr_bytes': 8192,
        }
        profile = {'devices': {}}
        ctxt = context.get_admin_context()
        instance_1 = fake_instance.fake_instance_obj(ctxt, id=1)
        instance_2 = fake_instance.fake_instance_obj(ctxt, id=2)
        all_disk_metrics.return_value = {
            instance_1.name: {
                'rbd1': counters,
            },
            instance_2.name: {
                'rbd1': counters,
            },
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._get_metric_devices_snapshot = mock.Mock(
            return_value={
                instance_1.name: profile,
                instance_2.name: profile,
            })

        result = incus_driver.get_all_volume_usage(None, [
            {
                'instance': instance_1,
                'instance_bdms': [{
                    'device_name': '/dev/vda',
                    'volume_id': 'volume-1',
                }],
            },
            {
                'instance': instance_2,
                'instance_bdms': [{
                    'device_name': '/dev/vdb',
                    'volume_id': 'volume-2',
                }],
            },
        ])

        self.assertEqual([
            {
                'volume': 'volume-1',
                'instance': instance_1,
                **counters,
            },
            {
                'volume': 'volume-2',
                'instance': instance_2,
                **counters,
            },
        ], result)
        self.assertEqual([
            mock.call(
                profile, instance_1, '/dev/vda', rbd_devices={}),
            mock.call(
                profile, instance_2, '/dev/vdb', rbd_devices={}),
        ], metric_device.call_args_list)
        all_disk_metrics.assert_called_once_with(self.client)
        mapped_rbd_devices.assert_called_once_with()

    @mock.patch.object(driver.LOG, 'error')
    @mock.patch('nova.virt.incus.driver._disk_metric_device')
    def test_get_all_volume_usage_isolates_invalid_volume(
            self, metric_device, log_error):
        ctxt = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctxt, id=1)
        bad_bdm = {
            'device_name': '/dev/vdb',
            'volume_id': 'volume-bad',
        }
        good_bdm = {
            'device_name': '/dev/vdc',
            'volume_id': 'volume-good',
        }
        counters = {
            'rd_req': 3,
            'rd_bytes': 4096,
            'wr_req': 4,
            'wr_bytes': 8192,
        }
        metric_device.side_effect = [
            exception.InvalidVolume(reason='unsafe source'),
            'rbd2',
        ]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._get_disk_metrics_snapshot = mock.Mock(return_value={
            instance.name: {'rbd2': counters},
        })
        incus_driver._get_metric_devices_snapshot = mock.Mock(return_value={
            instance.name: {'devices': {}},
        })

        result = incus_driver.get_all_volume_usage(None, [{
            'instance': instance,
            'instance_bdms': [bad_bdm, good_bdm],
        }])

        self.assertEqual([{
            'volume': 'volume-good',
            'instance': instance,
            **counters,
        }], result)
        self.assertEqual('volume-bad', log_error.call_args.args[1]['volume'])
        self.assertEqual(instance.name,
                         log_error.call_args.args[1]['instance'])

    @mock.patch.object(driver.LOG, 'error')
    def test_get_all_volume_usage_isolates_bad_profile_per_bdm(
            self, log_error):
        ctxt = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctxt, id=1)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._get_disk_metrics_snapshot = mock.Mock(return_value={})
        incus_driver._get_metric_devices_snapshot = mock.Mock(return_value={
            instance.name: {'devices': []},
        })

        result = incus_driver.get_all_volume_usage(None, [{
            'instance': instance,
            'instance_bdms': [
                {
                    'device_name': '/dev/vdb',
                    'volume_id': 'volume-1',
                },
                {
                    'device_name': '/dev/vdc',
                    'volume_id': 'volume-2',
                },
            ],
        }])

        self.assertEqual([], result)
        self.assertEqual(2, log_error.call_count)
        self.assertEqual(
            ['volume-1', 'volume-2'],
            [call.args[1]['volume'] for call in log_error.call_args_list])

    @mock.patch.object(driver.LOG, 'error')
    @mock.patch('nova.virt.incus.driver._disk_metric_device')
    def test_get_all_volume_usage_isolates_incus_not_found(
            self, metric_device, log_error):
        ctxt = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctxt, id=1)
        response = mock.Mock(status_code=404)
        response.json.return_value = {'error': 'profile disappeared'}
        metric_device.side_effect = incuscore_exceptions.NotFound(response)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._get_disk_metrics_snapshot = mock.Mock(return_value={})
        incus_driver._get_metric_devices_snapshot = mock.Mock(return_value={
            instance.name: {'devices': {}},
        })

        result = incus_driver.get_all_volume_usage(None, [{
            'instance': instance,
            'instance_bdms': [{
                'device_name': '/dev/vdb',
                'volume_id': 'volume-1',
            }],
        }])

        self.assertEqual([], result)
        self.assertEqual('volume-1', log_error.call_args.args[1]['volume'])
        self.assertEqual(instance.name,
                         log_error.call_args.args[1]['instance'])

    def test_get_all_volume_usage_propagates_metrics_transport_failure(self):
        ctxt = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctxt, id=1)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        transport_error = RuntimeError('Incus metrics endpoint unavailable')
        incus_driver._get_disk_metrics_snapshot = mock.Mock(
            side_effect=transport_error)
        incus_driver._get_metric_devices_snapshot = mock.Mock()

        raised = self.assertRaises(
            RuntimeError,
            incus_driver.get_all_volume_usage,
            None,
            [{
                'instance': instance,
                'instance_bdms': [{
                    'device_name': '/dev/vdb',
                    'volume_id': 'volume-1',
                }],
            }])

        self.assertIs(transport_error, raised)
        incus_driver._get_metric_devices_snapshot.assert_not_called()

    def test_get_all_volume_usage_propagates_profile_transport_failure(self):
        ctxt = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctxt, id=1)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        transport_error = RuntimeError(
            'Incus instance inventory endpoint unavailable')
        incus_driver._get_disk_metrics_snapshot = mock.Mock(return_value={})
        incus_driver._get_metric_devices_snapshot = mock.Mock(
            side_effect=transport_error)

        raised = self.assertRaises(
            RuntimeError,
            incus_driver.get_all_volume_usage,
            None,
            [{
                'instance': instance,
                'instance_bdms': [{
                    'device_name': '/dev/vdb',
                    'volume_id': 'volume-1',
                }],
            }])

        self.assertIs(transport_error, raised)

    def test_list_instances(self):
        self.client.instances.all.return_value = [
            MockContainer('mock-instance-1'),
            MockContainer('mock-instance-2'),
        ]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        instances = incus_driver.list_instances()

        self.assertEqual(['mock-instance-1', 'mock-instance-2'], instances)
        self.client.instances.all.assert_called_once_with()

    def test_get_num_instances_primes_power_state_inventory(self):
        first = mock.Mock(type='container')
        first.name = 'instance-1'
        second = mock.Mock(type='container')
        second.name = 'instance-2'
        self.client.instances.all.return_value = [first, second]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertEqual(2, incus_driver.get_num_instances())
        self.assertIs(first, incus_driver._get_instance_inventory_snapshot()[
            first.name])
        self.client.instances.all.assert_called_once_with(recursion=1)

    def test_list_instance_uuids_ignores_unmanaged_instances_and_vms(self):
        managed = mock.Mock(
            name='managed',
            type='container',
            config={'user.openstack.uuid': 'managed-uuid'})
        unmanaged = mock.Mock(name='unmanaged', type='container', config={})
        virtual_machine = mock.Mock(
            name='virtual-machine',
            type='virtual-machine',
            config={'user.openstack.uuid': 'vm-uuid'})
        self.client.instances.all.return_value = [
            managed, unmanaged, virtual_machine]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertEqual(['managed-uuid'], incus_driver.list_instance_uuids())

    @mock.patch.object(driver.LOG, 'error')
    def test_list_instance_uuids_reports_duplicate_owners_once(self, error):
        first = mock.Mock(
            type='container',
            config={'user.openstack.uuid': 'duplicate-uuid'})
        first.name = 'instance-00000001'
        stale = mock.Mock(
            type='container',
            config={'user.openstack.uuid': 'duplicate-uuid'})
        stale.name = 'criu-test-copy'
        self.client.instances.all.return_value = [first, stale]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertEqual(
            ['duplicate-uuid'], incus_driver.list_instance_uuids())
        error.assert_called_once_with(
            'Multiple Incus containers claim Nova instance UUID '
            '%(uuid)s: %(names)s. Refusing to report duplicate UUIDs; '
            'an operator must identify and remove the stale record.',
            {
                'uuid': 'duplicate-uuid',
                'names': ['criu-test-copy', 'instance-00000001'],
            })

    def test_incus_cloud_init_config(self):
        instance = mock.Mock(
            uuid='instance-uuid',
            hostname='tenant-server',
            user_data=base64.b64encode(b'#cloud-config\nruncmd: []\n'),
            key_name='tenant-key',
            key_data='ssh-ed25519 AAAATEST tenant')

        config = driver._incus_cloud_init_config(instance)
        metadata = yaml.safe_load(config.pop('user.meta-data'))
        self.assertEqual({
            'user.openstack.uuid': 'instance-uuid',
            'cloud-init.user-data': '#cloud-config\nruncmd: []\n',
        }, config)
        self.assertEqual({
            'instance-id': 'instance-uuid',
            'local-hostname': 'tenant-server',
            'public-keys': {
                'tenant-key': 'ssh-ed25519 AAAATEST tenant',
            },
        }, metadata)

    def test_get_vcpus_used_raises_when_inventory_unavailable(self):
        # Reporting zero would make a failing host look empty and attract
        # the scheduler exactly when Incus is unreachable.
        drv = driver.IncusDriver(None)
        with mock.patch.object(
                drv, '_get_instance_inventory_snapshot',
                side_effect=RuntimeError('incus unreachable')):
            self.assertRaises(RuntimeError, drv._get_vcpus_used)

    def test_unplug_vifs_attempts_every_vif_and_reraises_first(self):
        drv = driver.IncusDriver(None)
        drv.vif_driver = mock.Mock()
        boom = RuntimeError('vif 1 unplug failed')
        drv.vif_driver.unplug.side_effect = [boom, None, None]
        instance = mock.Mock()
        instance.name = 'instance-00000001'
        vifs = [{'id': 'vif-1'}, {'id': 'vif-2'}, {'id': 'vif-3'}]

        raised = self.assertRaises(
            RuntimeError, drv.unplug_vifs, instance, vifs)

        self.assertIs(boom, raised)
        self.assertEqual(3, drv.vif_driver.unplug.call_count)

    def test_rollback_cleanup_shares_the_attempt_every_vif_guarantee(self):
        """Both VIF cleanup paths must not stop at the first failure.

        A device left behind stays until someone removes it by hand, so
        the two paths now share one implementation of that guarantee
        rather than each carrying its own copy for a future fix to miss.
        """
        drv = driver.IncusDriver(None)
        drv.vif_driver = mock.Mock()
        drv.vif_driver.unplug.side_effect = [
            RuntimeError('vif 1'), None, RuntimeError('vif 3')]
        instance = mock.Mock()
        instance.name = 'instance-00000001'
        vifs = [{'id': 'vif-1'}, {'id': 'vif-2'}, {'id': 'vif-3'}]

        failures = drv._cleanup_vifs_best_effort(instance, vifs)

        self.assertEqual(3, drv.vif_driver.unplug.call_count)
        # Reported in full rather than only the first, because this
        # caller aggregates them for the operator.
        self.assertEqual(
            ['unplug destination VIF vif-1', 'unplug destination VIF vif-3'],
            [description for description, unused_exc in failures])

    def test_rollback_cleanup_skips_the_firewall_when_not_asked(self):
        drv = driver.IncusDriver(None)
        drv.vif_driver = mock.Mock()
        drv.firewall_driver = mock.Mock()
        instance = mock.Mock()
        instance.name = 'instance-00000001'

        failures = drv._cleanup_vifs_best_effort(
            instance, [{'id': 'vif-1'}], remove_firewall=False)

        self.assertEqual([], failures)
        drv.firewall_driver.unfilter_instance.assert_not_called()

    def test_incus_cloud_init_config_gzip_user_data(self):
        # Users gzip user-data to fit Nova's 64K API limit; it must arrive
        # at cloud-init as the equivalent decompressed text.
        payload = b'#cloud-config\nruncmd: []\n'
        instance = mock.Mock(
            uuid='instance-uuid',
            hostname='tenant-server',
            user_data=base64.b64encode(gzip.compress(payload)),
            key_name=None, key_data=None)

        config = driver._incus_cloud_init_config(instance)

        self.assertEqual(
            payload.decode('utf-8'), config['cloud-init.user-data'])

    def test_incus_cloud_init_config_rejects_a_decompression_bomb(self):
        """A 64 KiB upload must not become tens of megabytes.

        The expanded value is stored in the Incus instance configuration
        and returned by every read of that instance, so the cost would be
        paid again on each inventory scan rather than once at boot.
        """
        self.CONF.incus.maximum_user_data_kb = 1
        # Well inside Nova's own 64 KiB API limit once base64-encoded.
        bomb = gzip.compress(b'\0' * (4 * units.Mi))
        self.assertLess(len(base64.b64encode(bomb)), 64 * units.Ki)
        instance = mock.Mock(
            uuid='instance-uuid',
            user_data=base64.b64encode(bomb),
            key_name=None, key_data=None)

        self.assertRaisesRegex(
            Exception, 'maximum_user_data_kb',
            driver._incus_cloud_init_config, instance)

    def test_incus_cloud_init_config_reads_only_up_to_the_ceiling(self):
        # The limit has to be enforced during expansion; checking the
        # length afterwards would already have built the oversized value.
        self.CONF.incus.maximum_user_data_kb = 1
        reads = []
        real_gzipfile = driver.gzip.GzipFile

        class RecordingGzipFile(real_gzipfile):
            def read(self, size=-1):
                reads.append(size)
                return super().read(size)

        instance = mock.Mock(
            uuid='instance-uuid',
            user_data=base64.b64encode(gzip.compress(b'\0' * (4 * units.Mi))),
            key_name=None, key_data=None)

        with mock.patch.object(driver.gzip, 'GzipFile', RecordingGzipFile):
            self.assertRaises(
                exception.Invalid, driver._incus_cloud_init_config, instance)

        # Bounded at the ceiling rather than reading the whole stream.
        self.assertEqual(units.Ki + 1, reads[0])

    def test_incus_cloud_init_config_rejects_corrupt_gzip_user_data(self):
        instance = mock.Mock(
            uuid='instance-uuid',
            user_data=base64.b64encode(b'\x1f\x8btruncated'),
            key_name=None, key_data=None)

        self.assertRaisesRegex(
            Exception, 'not valid gzip data',
            driver._incus_cloud_init_config, instance)

    def test_incus_cloud_init_config_rejects_binary_user_data(self):
        instance = mock.Mock(
            uuid='instance-uuid',
            user_data=base64.b64encode(b'\xff\xfe\x00binary'),
            key_name=None, key_data=None)

        self.assertRaisesRegex(
            Exception, 'neither UTF-8 text nor',
            driver._incus_cloud_init_config, instance)

    def test_incus_cloud_init_network_config(self):
        network_info = [{
            'id': '01234567-89ab-cdef-0123-456789abcdef',
            'address': 'CA:FE:DE:AD:BE:EF',
            'network': {
                'meta': {'mtu': 1450},
                'subnets': [{
                    'cidr': '10.0.0.0/24',
                    'ips': [{'address': '10.0.0.10'}],
                    'gateway': {'address': '10.0.0.1'},
                    'routes': [],
                    'dns': [{'address': '1.1.1.1'}],
                }, {
                    'cidr': '2001:db8::/64',
                    'ips': [{'address': '2001:db8::10'}],
                    'gateway': {'address': '2001:db8::1'},
                    'routes': [],
                    'dns': [
                        {'address': '2001:4860:4860::8888'},
                        {'address': '2001:4860:4860::8888'},
                    ],
                }],
            },
        }]
        instance = mock.Mock(
            uuid='instance-uuid', hostname='tenant-server',
            user_data=None, key_data=None)

        config = driver._incus_cloud_init_config(
            instance, network_info)

        self.assertEqual(
            'version: 2\n'
            'ethernets:\n'
            '  nic0123456789ab:\n'
            '    mtu: 1450\n'
            '    addresses:\n'
            '    - 10.0.0.10/24\n'
            '    - 2001:db8::10/64\n'
            '    routes:\n'
            '    - to: 0.0.0.0/0\n'
            '      via: 10.0.0.1\n'
            '    - to: ::/0\n'
            '      via: 2001:db8::1\n'
            '    nameservers:\n'
            '      addresses:\n'
            '      - 1.1.1.1\n'
            '      - 2001:4860:4860::8888\n',
            config['cloud-init.network-config'])
        self.assertEqual(
            driver._NETWORK_ACTIVATION_VENDOR_DATA,
            config['cloud-init.vendor-data'])
        self.assertIsInstance(
            driver.yaml.safe_load(config['cloud-init.vendor-data']), dict)

    def test_incus_cloud_init_network_config_names_survive_reordering(self):
        network_info = [
            {
                'id': '11111111-1111-1111-1111-111111111111',
                'address': 'ca:fe:de:ad:be:01',
                'network': {
                    'subnets': [{
                        'cidr': '10.0.1.0/24',
                        'ips': [{'address': '10.0.1.10'}],
                    }],
                },
            },
            {
                'id': '22222222-2222-2222-2222-222222222222',
                'address': 'ca:fe:de:ad:be:02',
                'network': {
                    'subnets': [{
                        'cidr': '10.0.2.0/24',
                        'ips': [{'address': '10.0.2.10'}],
                    }],
                },
            },
        ]

        config = driver.yaml.safe_load(
            driver._incus_network_config(network_info))

        self.assertEqual(
            ['nic111111111111', 'nic222222222222'],
            list(config['ethernets']))
        self.assertEqual(
            ['10.0.1.10/24'],
            config['ethernets']['nic111111111111']['addresses'])
        self.assertEqual(
            ['10.0.2.10/24'],
            config['ethernets']['nic222222222222']['addresses'])

        reordered = driver.yaml.safe_load(
            driver._incus_network_config(list(reversed(network_info))))
        self.assertEqual(
            config['ethernets']['nic111111111111'],
            reordered['ethernets']['nic111111111111'])
        self.assertEqual(
            config['ethernets']['nic222222222222'],
            reordered['ethernets']['nic222222222222'])

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

    @mock.patch('nova.virt.incus.driver._sync_glance_image_to_incus')
    @mock.patch('nova.virt.configdrive.required_by')
    def test_spawn_resyncs_an_image_aged_out_before_the_create(
            self, configdrive, sync):
        """Cache aging can remove the image this spawn just verified.

        The aging pass holds no lock over the interval and only sees the
        instances that existed when its pass began, so a build starting
        after that snapshot can lose its image. Losing a build to a cache
        decision is not acceptable when the image can simply be fetched
        again.
        """
        def container_get(*args, **kwargs):
            raise incuscore_exceptions.LXDAPIException(MockResponse(404))

        self.client.instances.get.side_effect = container_get
        configdrive.return_value = False
        container = mock.Mock()
        self.client.instances.create.side_effect = [
            incuscore_exceptions.LXDAPIException(MockResponse(404)),
            container,
        ]

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0, root_gb=1)
        virtapi = manager.ComputeVirtAPI(mock.MagicMock())
        incus_driver = driver.IncusDriver(virtapi)
        incus_driver.init_host(None)
        incus_driver.firewall_driver = mock.Mock()

        incus_driver.spawn(
            ctx, instance, mock.Mock(), mock.Mock(), mock.Mock(),
            mock.Mock(), [_VIF], {'block_device_mapping': []})

        # Synced again for the create, and the spawn completed.
        sync.assert_called_once_with(
            incus_driver.client, ctx, instance.image_ref)
        self.assertEqual(2, self.client.instances.create.call_count)
        container.start.assert_called_once_with(wait=True)

    @mock.patch('nova.virt.configdrive.required_by')
    def test_spawn_does_not_retry_a_create_that_failed_for_another_reason(
            self, configdrive):
        # Only a missing image earns a second create; anything else must
        # surface as it always did.
        def container_get(*args, **kwargs):
            raise incuscore_exceptions.LXDAPIException(MockResponse(404))

        self.client.instances.get.side_effect = container_get
        configdrive.return_value = False
        self.client.instances.create.side_effect = (
            incuscore_exceptions.LXDAPIException(MockResponse(500)))

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0, root_gb=1)
        virtapi = manager.ComputeVirtAPI(mock.MagicMock())
        incus_driver = driver.IncusDriver(virtapi)
        incus_driver.init_host(None)
        incus_driver.firewall_driver = mock.Mock()

        self.assertRaises(
            incuscore_exceptions.LXDAPIException,
            incus_driver.spawn,
            ctx, instance, mock.Mock(), mock.Mock(), mock.Mock(),
            mock.Mock(), [_VIF], {'block_device_mapping': []})

        self.assertEqual(1, self.client.instances.create.call_count)

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
            ctx, name='test', memory_mb=0, root_gb=1)
        image_meta = mock.Mock()
        injected_files = mock.Mock()
        admin_password = mock.Mock()
        allocations = mock.Mock()
        network_info = [_VIF]
        block_device_info = {'block_device_mapping': []}
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
        self.begin_idmap_materialization.assert_called_once()
        self.client.instances.create.assert_called_once_with({
            'name': instance.name,
            'type': 'container',
            'profiles': [self.client.profiles.create.return_value.name],
            'config': {
                **driver._incus_cloud_init_config(instance),
                'user.openstack.uuid': instance.uuid,
                'boot.autostart': 'false',
                'migration.incremental.memory': 'false',
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
            ctx, name='test', memory_mb=512, root_gb=1)
        virtapi = manager.ComputeVirtAPI(mock.MagicMock())
        incus_driver = driver.IncusDriver(virtapi)
        incus_driver.init_host(None)
        block_device_info = {'block_device_mapping': []}

        self.assertRaises(
            exception.BuildAbortException,
            incus_driver.spawn,
            ctx, instance, mock.Mock(), [], None, mock.Mock(), [],
            block_device_info)

        self.client.images.get_by_alias.assert_not_called()
        self.client.instances.create.assert_not_called()
        self.vif_driver.plug.assert_not_called()
        get_ephemerals.assert_called_once_with(block_device_info)

    @mock.patch.object(
        driver.IncusDriver, '_attach_and_commit_internal_volume_operation')
    @mock.patch('nova.virt.configdrive.required_by', return_value=False)
    def test_spawn_boot_from_cinder_rbd(
            self, configdrive, attach_volume):
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
        data_bdm = {
            'boot_index': None,
            'mount_device': '/dev/vdb',
            'attachment_id':
                'a231d2e8-1111-4222-8333-123456789abc',
            'connection_info': {
                'driver_volume_type': 'rbd',
                'serial': '9231d2e8-1111-4222-8333-123456789abc',
                'data': {
                    'name': ('cinder-volumes/volume-'
                             '9231d2e8-1111-4222-8333-123456789abc'),
                    'access_mode': 'rw',
                },
            },
        }
        profile = self.client.profiles.create.return_value
        profile.devices = {'root': {'type': 'disk', 'path': '/',
                                    'size': '20GB'}}
        container = self.client.instances.create.return_value

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-bfv', memory_mb=512,
            expected_attrs=['system_metadata'],
            system_metadata={
                'image_hw_incus_data_volume_fuse': 'true'})
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        }
        self.client.profiles.get.return_value = profile
        image_meta = mock.Mock(disk_format='raw', container_format='bare')
        incus_driver = driver.IncusDriver(
            manager.ComputeVirtAPI(mock.MagicMock()))
        incus_driver.init_host(None)
        incus_driver.firewall_driver = mock.Mock()

        incus_driver.spawn(
            ctx, instance, image_meta, [], None, mock.Mock(), [],
            {'block_device_mapping': [root_bdm, data_bdm]})

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
                'migration.incremental.memory': 'false',
            },
            'source': {'type': 'none'},
        }, wait=True)
        attach_volume.assert_called_once_with(
            ctx, data_bdm['connection_info'], instance, '/dev/vdb',
            data_bdm['attachment_id'], 'spawn', mock.ANY, 'materialize',
            encryption=None)
        container.start.assert_called_once_with(wait=True)

    @mock.patch.object(
        driver.IncusDriver, '_attach_and_commit_internal_volume_operation')
    @mock.patch('nova.virt.configdrive.required_by', return_value=False)
    def test_spawn_attaches_initial_data_volumes_before_start(
            self, configdrive, attach_volume):
        container = self.client.instances.create.return_value
        self.client.instances.get.side_effect = (
            incuscore_exceptions.LXDAPIException(MockResponse(404)))
        events = []
        attach_volume.side_effect = (
            lambda *args, **kwargs: events.append(
                'attach-' + args[1]['serial']))
        container.start.side_effect = lambda **kwargs: events.append('start')
        data_bdms = [{
            'boot_index': None,
            'mount_device': '/dev/vdb',
            'attachment_id':
                'a231d2e8-1111-4222-8333-123456789abc',
            'connection_info': {
                'driver_volume_type': 'rbd',
                'serial': _TEST_VOLUME_ID,
                'data': {
                    'name': 'volumes/volume-%s' % _TEST_VOLUME_ID,
                },
            },
        }, {
            'boot_index': None,
            'mount_device': '/dev/vdc',
            'attachment_id':
                'b231d2e8-1111-4222-8333-123456789abc',
            'connection_info': {
                'driver_volume_type': 'rbd',
                'serial': _TEST_VOLUME_ID_2,
                'data': {
                    'name': 'volumes/volume-%s' % _TEST_VOLUME_ID_2,
                },
            },
        }]
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-data-volumes', memory_mb=512, root_gb=1,
            expected_attrs=['system_metadata'],
            system_metadata={
                'image_hw_incus_data_volume_fuse': 'true'})
        profile = self.client.profiles.create.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        }
        self.client.profiles.get.return_value = profile
        incus_driver = driver.IncusDriver(
            manager.ComputeVirtAPI(mock.MagicMock()))
        incus_driver.init_host(None)
        incus_driver.firewall_driver = mock.Mock()

        incus_driver.spawn(
            ctx, instance, mock.Mock(), [], None, mock.Mock(), [],
            {'block_device_mapping': data_bdms})

        self.assertEqual(
            ['attach-' + _TEST_VOLUME_ID,
             'attach-' + _TEST_VOLUME_ID_2,
             'start'], events)

    @mock.patch.object(
        driver.IncusDriver, '_attach_and_commit_internal_volume_operation')
    @mock.patch.object(driver.flavor, 'to_profile')
    @mock.patch('nova.virt.configdrive.required_by', return_value=False)
    def test_spawn_root_and_initial_data_volume_cardinality_matrix(
            self, configdrive, to_profile, attach_volume):
        self._configure_bfv_pool()
        self.client.host_info['api_extensions'].append(
            'storage_driver_cephext')
        self.CONF.incus.boot_from_volume_storage_pools = {
            'cinder-volumes': 'cinder'}
        self.client.storage_pools.get.return_value = mock.Mock(
            driver='cephext', config={'source': 'cinder-volumes'})
        not_found = incuscore_exceptions.LXDAPIException(MockResponse(404))
        ctx = context.get_admin_context()

        for root_mode in ('local', 'bfv'):
            for volume_count in (0, 1, 3):
                with self.subTest(
                        root_mode=root_mode, volume_count=volume_count):
                    self.client.instances.get.reset_mock()
                    self.client.instances.create.reset_mock()
                    self.client.instances.get.side_effect = not_found
                    attach_volume.reset_mock()
                    to_profile.reset_mock()

                    events = []
                    container = mock.Mock(status='Stopped')
                    container.start.side_effect = (
                        lambda **kwargs: events.append('start'))
                    self.client.instances.create.return_value = container
                    profile = mock.Mock()
                    profile.name = 'profile-%s-%d' % (
                        root_mode, volume_count)
                    profile.devices = {}
                    to_profile.return_value = profile

                    attach_volume.side_effect = (
                        lambda _ctx, connection, *_args, **_kwargs:
                            events.append('attach-' + connection['serial']))
                    data_bdms = []
                    for index in range(volume_count):
                        data_volume_id = (
                            '9231d2e8-1111-4222-8333-%012d' %
                            (volume_count * 10 + index))
                        data_bdms.append(real_volume_driver_bdm(
                            ctx, data_volume_id,
                            '/dev/vd%s' % chr(ord('b') + index), None, {
                                'driver_volume_type': 'rbd',
                                'serial': data_volume_id,
                                'data': {
                                    'name': 'cinder-volumes/volume-%s' %
                                            data_volume_id,
                                },
                            }))

                    bdms = list(data_bdms)
                    if root_mode == 'bfv':
                        volume_id = (
                            '8231d2e8-1111-4222-8333-%012d' % volume_count)
                        bdms.insert(0, real_volume_driver_bdm(
                            ctx, volume_id, '/dev/vda', 0, {
                                'driver_volume_type': 'rbd',
                                'serial': volume_id,
                                'data': {
                                    'name': 'cinder-volumes/volume-%s' %
                                            volume_id,
                                    'volume_id': volume_id,
                                    'access_mode': 'rw',
                                },
                            }))

                    system_metadata = {}
                    if volume_count:
                        system_metadata[
                            'image_hw_incus_data_volume_fuse'] = 'true'
                    instance = fake_instance.fake_instance_obj(
                        ctx,
                        name='test-%s-%d' % (root_mode, volume_count),
                        memory_mb=512,
                        root_gb=1,
                        expected_attrs=['system_metadata'],
                        system_metadata=system_metadata)
                    profile.config = {
                        'environment.product_name': 'OpenStack Nova',
                        'user.openstack.uuid': instance.uuid,
                    }
                    self.client.profiles.get.return_value = profile
                    incus_driver = driver.IncusDriver(
                        manager.ComputeVirtAPI(mock.MagicMock()))
                    incus_driver.init_host(None)
                    incus_driver.firewall_driver = mock.Mock()

                    incus_driver.spawn(
                        ctx, instance, mock.Mock(properties={}), [], None,
                        mock.Mock(), [], {'block_device_mapping': bdms})

                    self.assertEqual(
                        ['attach-' + bdm['connection_info']['serial']
                         for bdm in data_bdms] + ['start'],
                        events)
                    source = self.client.instances.create.call_args.args[0][
                        'source']
                    self.assertEqual(
                        'none' if root_mode == 'bfv' else 'image',
                        source['type'])

    def test_spawn_rejects_initial_encrypted_data_volume_before_side_effects(
            self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-encrypted-data', memory_mb=512, root_gb=1,
            expected_attrs=['system_metadata'],
            system_metadata={
                'image_hw_incus_data_volume_fuse': 'true'})
        data_bdm = {
            'boot_index': None,
            'mount_device': '/dev/vdb',
            'connection_info': {
                'driver_volume_type': 'rbd',
                'serial': 'volume-encrypted',
                'data': {'encrypted': {'provider': 'luks'}},
            },
        }
        incus_driver = driver.IncusDriver(
            manager.ComputeVirtAPI(mock.MagicMock()))
        incus_driver.init_host(None)

        self.assertRaises(
            exception.BuildAbortException,
            incus_driver.spawn, ctx, instance,
            mock.Mock(properties={}), [], None, mock.Mock(), [],
            {'block_device_mapping': [data_bdm]})

        self.client.instances.get.assert_not_called()
        self.client.instances.create.assert_not_called()
        self.client.profiles.create.assert_not_called()
        self.vif_driver.plug.assert_not_called()
        self.volume_api.get_volume_encryption_metadata.assert_called_once_with(
            ctx, 'volume-encrypted')

    def test_spawn_rejects_explicitly_encrypted_bfv_before_side_effects(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-encrypted-bfv', memory_mb=512)
        root_bdm = {
            'boot_index': 0,
            'connection_info': {
                'driver_volume_type': 'rbd',
                'serial': '8231d2e8-1111-4222-8333-123456789abc',
                'data': {},
            },
        }
        incus_driver = driver.IncusDriver(
            manager.ComputeVirtAPI(mock.MagicMock()))
        incus_driver.init_host(None)
        self.volume_api.get_volume_encryption_metadata.return_value = {
            'provider': 'luks'}

        self.assertRaises(
            exception.BuildAbortException,
            incus_driver.spawn, ctx, instance,
            mock.Mock(properties={}), [], None, mock.Mock(), [],
            {'block_device_mapping': [root_bdm]})

        self.client.instances.get.assert_not_called()
        self.client.instances.create.assert_not_called()
        self.client.profiles.create.assert_not_called()
        self.vif_driver.plug.assert_not_called()

    def test_spawn_queries_authoritative_encryption_for_real_bdms(self):
        ctx = context.get_admin_context()
        root_id = '8231d2e8-1111-4222-8333-123456789abc'
        data_id = '9231d2e8-1111-4222-8333-123456789abc'
        root_bdm = real_volume_driver_bdm(
            ctx, root_id, '/dev/vda', 0, {
                'driver_volume_type': 'rbd',
                'serial': root_id,
                'data': {
                    'name': 'cinder-volumes/volume-%s' % root_id,
                    'access_mode': 'rw',
                },
            })
        data_bdm = real_volume_driver_bdm(
            ctx, data_id, '/dev/vdb', None, {
                'driver_volume_type': 'rbd',
                'serial': data_id,
                'data': {
                    'name': 'cinder-volumes/volume-%s' % data_id,
                    'access_mode': 'rw',
                },
            })
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-real-encrypted-bdm', memory_mb=512, root_gb=1,
            expected_attrs=['system_metadata'],
            system_metadata={
                'image_hw_incus_data_volume_fuse': 'true'})
        incus_driver = driver.IncusDriver(
            manager.ComputeVirtAPI(mock.MagicMock()))
        incus_driver.init_host(None)
        self.volume_api.get_volume_encryption_metadata.side_effect = [
            {}, {'provider': 'luks'},
        ]

        self.assertRaises(
            exception.BuildAbortException,
            incus_driver.spawn, ctx, instance,
            mock.Mock(properties={}), [], None, mock.Mock(), [],
            {'block_device_mapping': [root_bdm, data_bdm]})

        self.assertEqual([
            mock.call(ctx, root_id), mock.call(ctx, data_id),
        ], self.volume_api.get_volume_encryption_metadata.call_args_list)
        self.client.instances.get.assert_not_called()
        self.client.instances.create.assert_not_called()
        self.client.profiles.create.assert_not_called()
        self.vif_driver.plug.assert_not_called()

    def test_initial_volume_encryption_ignores_unencrypted_metadata(self):
        # cinderclient returns a truthy resource object (request-id
        # tracking only) even for unencrypted volumes; it must not be
        # mistaken for an encryption marker.
        ctx = context.get_admin_context()
        root_bdm = {
            'boot_index': 0,
            'connection_info': {
                'driver_volume_type': 'rbd',
                'serial': '8231d2e8-1111-4222-8333-123456789abc',
                'data': {},
            },
        }
        incus_driver = driver.IncusDriver(
            manager.ComputeVirtAPI(mock.MagicMock()))
        incus_driver.init_host(None)
        self.volume_api.get_volume_encryption_metadata.return_value = (
            mock.Mock(spec=[]))

        incus_driver._validate_initial_volume_encryption(ctx, root_bdm, [])

    def test_bdm_volume_id_uses_real_driver_bdm_attribute(self):
        ctx = context.get_admin_context()
        volume_id = '8231d2e8-1111-4222-8333-123456789abc'
        bdm = real_volume_driver_bdm(
            ctx, volume_id, '/dev/vdb', None, {
                'driver_volume_type': 'rbd',
                'data': {
                    'name': 'cinder-volumes/volume-%s' % volume_id,
                },
            })

        self.assertEqual(volume_id, driver._bdm_volume_id(bdm))

    def test_bdm_volume_id_uses_legacy_connection_info(self):
        volume_id = '8231d2e8-1111-4222-8333-123456789abc'
        bdm = {
            'connection_info': {
                'driver_volume_type': 'rbd',
                'serial': volume_id,
                'data': {
                    'name': 'cinder-volumes/volume-%s' % volume_id,
                },
            },
        }

        self.assertEqual(volume_id, driver._bdm_volume_id(bdm))

    def test_spawn_preflights_all_data_volumes_before_first_connector(self):
        ctx = context.get_admin_context()
        first_id = '8231d2e8-1111-4222-8333-123456789abc'
        second_id = '9231d2e8-1111-4222-8333-123456789abc'
        first = real_volume_driver_bdm(
            ctx, first_id, '/dev/vdb', None, {
                'driver_volume_type': 'rbd',
                'serial': first_id,
                'data': {
                    'name': 'cinder-volumes/volume-%s' % first_id,
                    'access_mode': 'rw',
                },
            })
        second = real_volume_driver_bdm(
            ctx, second_id, '/dev/vdc', None, {
                'driver_volume_type': 'rbd',
                'serial': second_id,
                'data': {
                    'name': 'cinder-volumes/volume-%s' % second_id,
                    'access_mode': 'ro',
                },
            })
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-preflight-all-data', memory_mb=512, root_gb=1,
            expected_attrs=['system_metadata'],
            system_metadata={
                'image_hw_incus_data_volume_fuse': 'true'})
        incus_driver = driver.IncusDriver(
            manager.ComputeVirtAPI(mock.MagicMock()))
        incus_driver.init_host(None)

        with mock.patch.object(driver, 'brick_get_connector') as get_connector:
            self.assertRaises(
                exception.BuildAbortException,
                incus_driver.spawn, ctx, instance,
                mock.Mock(properties={}), [], None, mock.Mock(), [],
                {'block_device_mapping': [first, second]})

        get_connector.assert_not_called()
        self.client.instances.get.assert_not_called()
        self.client.instances.create.assert_not_called()
        self.client.profiles.create.assert_not_called()
        self.vif_driver.plug.assert_not_called()

    def test_spawn_multiattach_preflight_aborts_without_reschedule(self):
        ctx = context.get_admin_context()
        volume_id = '8231d2e8-1111-4222-8333-123456789abc'
        bdm = real_volume_driver_bdm(
            ctx, volume_id, '/dev/vdb', None, {
                'driver_volume_type': 'rbd',
                'serial': volume_id,
                'data': {
                    'name': 'cinder-volumes/volume-%s' % volume_id,
                    'multiattach': True,
                },
            })
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-multiattach-preflight', memory_mb=512, root_gb=1,
            expected_attrs=['system_metadata'], system_metadata={
                'image_hw_incus_data_volume_fuse': 'true'})
        incus_driver = driver.IncusDriver(
            manager.ComputeVirtAPI(mock.MagicMock()))
        incus_driver.init_host(None)

        with mock.patch.object(driver, 'brick_get_connector') as connector:
            self.assertRaises(
                exception.BuildAbortException,
                incus_driver.spawn, ctx, instance,
                mock.Mock(properties={}), [], None, mock.Mock(), [],
                {'block_device_mapping': [bdm]})

        connector.assert_not_called()
        self.client.instances.get.assert_not_called()
        self.client.instances.create.assert_not_called()
        self.client.profiles.create.assert_not_called()
        self.vif_driver.plug.assert_not_called()

    def test_spawn_non_rbd_data_volume_aborts_before_side_effects(self):
        ctx = context.get_admin_context()
        volume_id = '8231d2e8-1111-4222-8333-123456789abc'
        bdm = real_volume_driver_bdm(
            ctx, volume_id, '/dev/vdb', None, {
                'driver_volume_type': 'iscsi',
                'serial': volume_id,
                'data': {'volume_id': volume_id},
            })
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-non-rbd-preflight', memory_mb=512, root_gb=1,
            expected_attrs=['system_metadata'], system_metadata={
                'image_hw_incus_data_volume_fuse': 'true'})
        incus_driver = driver.IncusDriver(
            manager.ComputeVirtAPI(mock.MagicMock()))
        incus_driver.init_host(None)

        with mock.patch.object(driver, 'brick_get_connector') as connector:
            self.assertRaises(
                exception.BuildAbortException,
                incus_driver.spawn, ctx, instance,
                mock.Mock(properties={}), [], None, mock.Mock(), [],
                {'block_device_mapping': [bdm]})

        connector.assert_not_called()
        self.client.instances.get.assert_not_called()
        self.client.instances.create.assert_not_called()
        self.client.profiles.create.assert_not_called()
        self.vif_driver.plug.assert_not_called()

    def test_spawn_rejects_data_rbd_image_for_different_volume(self):
        ctx = context.get_admin_context()
        bdm = real_volume_driver_bdm(
            ctx, _TEST_VOLUME_ID, '/dev/vdb', None, {
                'driver_volume_type': 'rbd',
                'serial': _TEST_VOLUME_ID,
                'data': {
                    'name': 'volumes/volume-%s' % _TEST_VOLUME_ID_2,
                },
            })
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-rbd-identity-mismatch', memory_mb=512, root_gb=1,
            expected_attrs=['system_metadata'], system_metadata={
                'image_hw_incus_data_volume_fuse': 'true'})
        incus_driver = driver.IncusDriver(
            manager.ComputeVirtAPI(mock.MagicMock()))
        incus_driver.init_host(None)

        with mock.patch.object(driver, 'brick_get_connector') as connector:
            self.assertRaisesRegex(
                exception.BuildAbortException,
                'image UUID does not match',
                incus_driver.spawn, ctx, instance,
                mock.Mock(properties={}), [], None, mock.Mock(), [],
                {'block_device_mapping': [bdm]})

        connector.assert_not_called()
        self.client.instances.get.assert_not_called()
        self.client.instances.create.assert_not_called()
        self.client.profiles.create.assert_not_called()
        self.vif_driver.plug.assert_not_called()

    def test_recovery_record_rejects_different_rbd_namespace(self):
        connection_info = {
            'driver_volume_type': 'rbd',
            'serial': _TEST_VOLUME_ID,
            'data': {
                'name': 'volumes/volume-%s' % _TEST_VOLUME_ID,
                'rbd_namespace': 'tenant-a',
            },
        }
        recorded = copy.deepcopy(connection_info)
        recorded['data']['rbd_namespace'] = 'tenant-b'
        record = jsonutils.loads(driver._serialize_volume_attachment(
            recorded, {'path': '/dev/rbd0'}, '/dev/vdb'))

        self.assertRaisesRegex(
            exception.InvalidVolume, 'different RBD namespace',
            driver._validate_volume_recovery_record,
            record, _TEST_VOLUME_ID, '/dev/vdb', connection_info)

    def test_spawn_initial_data_volume_requires_image_capability(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-data-image-gate', memory_mb=512, root_gb=1,
            image_ref='00000000-0000-0000-0000-000000000010',
            system_metadata={})
        data_bdm = {
            'boot_index': None,
            'mount_device': '/dev/vdb',
            'connection_info': {
                'driver_volume_type': 'rbd',
                'serial': _TEST_VOLUME_ID,
                'data': {
                    'name': 'volumes/volume-%s' % _TEST_VOLUME_ID,
                },
            },
        }
        incus_driver = driver.IncusDriver(
            manager.ComputeVirtAPI(mock.MagicMock()))
        incus_driver.init_host(None)

        self.assertRaises(
            exception.BuildAbortException,
            incus_driver.spawn, ctx, instance,
            mock.Mock(properties={}), [], None, mock.Mock(), [],
            {'block_device_mapping': [data_bdm]})

        self.client.instances.get.assert_not_called()
        self.client.instances.create.assert_not_called()
        self.client.profiles.create.assert_not_called()
        self.vif_driver.plug.assert_not_called()

    def test_initial_data_volume_image_capability_prefers_system_metadata(
            self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, expected_attrs=['system_metadata'], system_metadata={
                'image_hw_incus_data_volume_fuse': 'true'})

        self.assertTrue(driver._initial_data_volume_image_capability(
            instance, mock.Mock(properties={
                'hw_incus_data_volume_fuse': 'false'})))

    def test_initial_data_volume_capability_absent_reads_false(self):
        """ImageMetaProps raises AttributeError for unregistered keys."""
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, expected_attrs=['system_metadata'], system_metadata={})
        properties = mock.Mock(spec=['get'])
        properties.get.side_effect = AttributeError(
            'ImageMetaProps object has no attribute '
            "'hw_incus_data_volume_fuse'")

        self.assertFalse(driver._initial_data_volume_image_capability(
            instance, mock.Mock(properties=properties)))

    def test_spawn_data_volumes_reject_duplicate_volume(self):
        connection_info = {
            'driver_volume_type': 'rbd',
            'serial': 'volume-1',
            'data': {},
        }
        block_device_info = {'block_device_mapping': [{
            'boot_index': 0,
            'connection_info': connection_info,
        }, {
            'boot_index': None,
            'mount_device': '/dev/vdb',
            'connection_info': connection_info,
        }]}

        self.assertRaises(
            exception.InvalidVolume,
            driver._spawn_data_volume_bdms, block_device_info)

    def test_spawn_data_volumes_reject_missing_connection_info(self):
        block_device_info = {'block_device_mapping': [{
            'boot_index': None,
            'mount_device': '/dev/vdb',
            'connection_info': None,
        }]}

        self.assertRaisesRegex(
            exception.InvalidVolume, 'no connection information',
            driver._spawn_data_volume_bdms, block_device_info)

    def test_spawn_data_volumes_accept_authoritative_root_mountpoint(self):
        root_bdm = {
            'boot_index': 0,
            'mount_device': '/dev/vda',
            'connection_info': {
                'driver_volume_type': 'rbd',
                'serial': 'root-volume',
                'data': {},
            },
        }
        data_bdm = {
            'boot_index': None,
            'mount_device': '/dev/vdb',
            'connection_info': {
                'driver_volume_type': 'rbd',
                'serial': 'data-volume',
                'data': {},
            },
        }

        result = driver._spawn_data_volume_bdms(
            {'block_device_mapping': [root_bdm, data_bdm]},
            root_device_name='/dev/vda')

        self.assertEqual([data_bdm], result)

    def test_spawn_data_volumes_reject_authoritative_root_collision(self):
        block_device_info = {'block_device_mapping': [{
            'boot_index': 0,
            'mount_device': '/dev/vda',
            'connection_info': {
                'driver_volume_type': 'rbd',
                'serial': 'root-volume',
                'data': {},
            },
        }, {
            'boot_index': None,
            'mount_device': '/dev/vda',
            'connection_info': {
                'driver_volume_type': 'rbd',
                'serial': 'data-volume',
                'data': {},
            },
        }]}

        self.assertRaises(
            exception.DevicePathInUse,
            driver._spawn_data_volume_bdms, block_device_info,
            root_device_name='/dev/vda')

    def test_reboot_data_volumes_reject_multiple_boot_volumes(self):
        block_device_info = {'block_device_mapping': [{
            'boot_index': 0,
            'mount_device': '/dev/vda',
            'connection_info': {
                'driver_volume_type': 'rbd',
                'serial': 'root-volume-1',
                'data': {},
            },
        }, {
            'boot_index': 0,
            'mount_device': '/dev/vdc',
            'connection_info': {
                'driver_volume_type': 'rbd',
                'serial': 'root-volume-2',
                'data': {},
            },
        }]}

        self.assertRaisesRegex(
            exception.InvalidConfiguration,
            'only one boot_index=0 volume',
            driver._reboot_data_volume_bdms, block_device_info,
            root_device_name='/dev/vda')

    def test_spawn_rejects_bfv_root_data_mountpoint_collision_first(self):
        root_connection = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'}, '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        data_connection = fake_connection_info(
            {'id': 2, 'name': 'volume-00000002'}, '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000002')
        block_device_info = {'block_device_mapping': [{
            'boot_index': 0,
            'mount_device': '/dev/vda',
            'connection_info': root_connection,
        }, {
            'boot_index': None,
            'mount_device': '/dev/vda',
            'connection_info': data_connection,
        }]}
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-bfv-path-collision', root_device_name='/dev/vda',
            memory_mb=512, root_gb=1, system_metadata={})
        incus_driver = driver.IncusDriver(
            manager.ComputeVirtAPI(mock.MagicMock()))
        incus_driver.init_host(None)

        with mock.patch.object(driver, 'brick_get_connector') as connector:
            self.assertRaises(
                exception.BuildAbortException,
                incus_driver.spawn, ctx, instance, mock.Mock(properties={}),
                [], None, mock.Mock(), [], block_device_info)
            connector.assert_not_called()

        self.client.instances.get.assert_not_called()
        self.client.instances.create.assert_not_called()
        self.client.profiles.create.assert_not_called()

    def test_reboot_rejects_authoritative_root_data_mountpoint_collision(self):
        root_connection = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'}, '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        data_connection = fake_connection_info(
            {'id': 2, 'name': 'volume-00000002'}, '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000002')
        block_device_info = {'block_device_mapping': [{
            'boot_index': 0,
            'mount_device': None,
            'connection_info': root_connection,
        }, {
            'boot_index': None,
            'mount_device': '/dev/vda',
            'connection_info': data_connection,
        }]}
        instance = mock.Mock(
            name='instance-00000001', root_device_name='/dev/vda')
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        with mock.patch.object(driver, 'brick_get_connector') as connector:
            self.assertRaises(
                exception.DevicePathInUse,
                incus_driver._reconcile_reboot_data_volumes,
                mock.sentinel.context, instance, block_device_info)
            connector.assert_not_called()

        self.client.instances.get.assert_not_called()
        self.client.profiles.get.assert_not_called()

    @mock.patch('nova.virt.configdrive.required_by', return_value=False)
    def test_spawn_data_volume_failure_rolls_back_exact_generation(
            self, configdrive):
        self.client.instances.get.side_effect = (
            incuscore_exceptions.LXDAPIException(MockResponse(404)))
        data_bdms = []
        volume_ids = (
            _TEST_VOLUME_ID, _TEST_VOLUME_ID_2, _TEST_VOLUME_ID_3)
        for index, device in enumerate(('vdb', 'vdc', 'vdd')):
            volume_id = volume_ids[index]
            data_bdms.append({
                'boot_index': None,
                'attachment_id': str(uuid.uuid4()),
                'mount_device': '/dev/' + device,
                'connection_info': {
                    'driver_volume_type': 'rbd',
                    'serial': volume_id,
                    'data': {
                        'name': 'volumes/volume-%s' % volume_id,
                    },
                },
            })
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-data-rollback', memory_mb=512, root_gb=1,
            expected_attrs=['system_metadata'],
            system_metadata={
                'image_hw_incus_data_volume_fuse': 'true'})
        incus_driver = driver.IncusDriver(
            manager.ComputeVirtAPI(mock.MagicMock()))
        incus_driver.init_host(None)
        incus_driver.firewall_driver = mock.Mock()
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        }
        incus_driver._attach_and_commit_internal_volume_operation = mock.Mock(
            side_effect=[None, None, RuntimeError('third attach failed')])
        incus_driver._rollback_failed_spawn_volume_intents = mock.Mock()
        incus_driver.cleanup = mock.Mock()

        self.assertRaisesRegex(
            RuntimeError, 'third attach failed', incus_driver.spawn,
            ctx, instance, mock.Mock(), [], None, mock.Mock(), [],
            {'block_device_mapping': data_bdms})

        rollback = incus_driver._rollback_failed_spawn_volume_intents
        rollback.assert_called_once()
        self.assertIs(ctx, rollback.call_args.args[0])
        self.assertIs(instance, rollback.call_args.args[1])
        self.assertEqual(data_bdms, rollback.call_args.args[3])
        self.assertEqual(data_bdms[:2], rollback.call_args.args[4])
        self.client.instances.create.return_value.start.assert_not_called()
        incus_driver.cleanup.assert_called_once()

    @mock.patch('nova.virt.configdrive.required_by', return_value=False)
    def test_spawn_firewall_failure_fences_before_detaching_volumes(
            self, configdrive):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-spawn-fence-order', memory_mb=512, root_gb=1,
            expected_attrs=['system_metadata'],
            system_metadata={
                'image_hw_incus_data_volume_fuse': 'true'})
        container = self.client.instances.create.return_value
        container.status = 'Running'
        not_found = incuscore_exceptions.NotFound(MockResponse(404))
        self.client.instances.get.side_effect = [
            not_found, container, container]
        data_bdms = [{
            'boot_index': None,
            'attachment_id': str(uuid.uuid4()),
            'mount_device': '/dev/vdb',
            'connection_info': {
                'driver_volume_type': 'rbd',
                'serial': _TEST_VOLUME_ID,
                'data': {
                    'name': 'volumes/volume-%s' % _TEST_VOLUME_ID,
                },
            },
        }, {
            'boot_index': None,
            'attachment_id': str(uuid.uuid4()),
            'mount_device': '/dev/vdc',
            'connection_info': {
                'driver_volume_type': 'rbd',
                'serial': _TEST_VOLUME_ID_2,
                'data': {
                    'name': 'volumes/volume-%s' % _TEST_VOLUME_ID_2,
                },
            },
        }]
        events = []
        container.start.side_effect = (
            lambda **kwargs: events.append('start'))
        container.stop.side_effect = (
            lambda **kwargs: events.append('stop'))
        container.delete.side_effect = (
            lambda **kwargs: events.append('delete'))
        incus_driver = driver.IncusDriver(
            manager.ComputeVirtAPI(mock.MagicMock()))
        incus_driver.init_host(None)
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        }
        incus_driver._attach_and_commit_internal_volume_operation = mock.Mock(
            side_effect=lambda _ctx, connection, *_args, **_kwargs:
                events.append('attach-' + connection['serial']))
        incus_driver._rollback_failed_spawn_volume_intents = mock.Mock(
            side_effect=lambda *_args, **_kwargs:
                events.append('rollback-volumes'))
        incus_driver.cleanup = mock.Mock(
            side_effect=lambda *_args, **_kwargs: events.append('cleanup'))
        incus_driver.firewall_driver = mock.Mock()

        def firewall_failure(*_args):
            events.append('firewall')
            raise RuntimeError('firewall failed')

        incus_driver.firewall_driver.apply_instance_filter.side_effect = (
            firewall_failure)

        self.assertRaisesRegex(
            RuntimeError, 'firewall failed', incus_driver.spawn,
            ctx, instance, mock.Mock(), [], None, mock.Mock(), [],
            {'block_device_mapping': data_bdms})

        self.assertEqual([
            'attach-' + _TEST_VOLUME_ID,
            'attach-' + _TEST_VOLUME_ID_2,
            'start', 'firewall', 'stop', 'rollback-volumes', 'delete',
            'cleanup',
        ], events)

    @mock.patch('nova.virt.configdrive.required_by', return_value=False)
    def test_spawn_fencing_failure_retains_all_attached_resources(
            self, configdrive):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-spawn-fence-failure', memory_mb=512, root_gb=1,
            expected_attrs=['system_metadata'],
            system_metadata={
                'image_hw_incus_data_volume_fuse': 'true'})
        container = self.client.instances.create.return_value
        container.status = 'Running'
        calls = {'get': 0}

        def get_instance(_name):
            calls['get'] += 1
            if calls['get'] == 1:
                raise incuscore_exceptions.NotFound(MockResponse(404))
            return container

        self.client.instances.get.side_effect = get_instance
        container.stop.side_effect = (
            incuscore_exceptions.ClientConnectionFailed('response lost'))
        data_bdm = {
            'boot_index': None,
            'attachment_id': str(uuid.uuid4()),
            'mount_device': '/dev/vdb',
            'connection_info': {
                'driver_volume_type': 'rbd',
                'serial': _TEST_VOLUME_ID,
                'data': {
                    'name': 'volumes/volume-%s' % _TEST_VOLUME_ID,
                },
            },
        }
        incus_driver = driver.IncusDriver(
            manager.ComputeVirtAPI(mock.MagicMock()))
        incus_driver.init_host(None)
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        }
        incus_driver._attach_and_commit_internal_volume_operation = mock.Mock()
        incus_driver._rollback_failed_spawn_volume_intents = mock.Mock()
        incus_driver.cleanup = mock.Mock()
        incus_driver.firewall_driver = mock.Mock()
        incus_driver.firewall_driver.apply_instance_filter.side_effect = (
            RuntimeError('firewall failed'))

        self.assertRaisesRegex(
            RuntimeError, 'firewall failed', incus_driver.spawn,
            ctx, instance, mock.Mock(), [], None, mock.Mock(), [],
            {'block_device_mapping': [data_bdm]})

        attach = incus_driver._attach_and_commit_internal_volume_operation
        attach.assert_called_once()
        incus_driver._rollback_failed_spawn_volume_intents.assert_not_called()
        incus_driver.cleanup.assert_not_called()
        container.delete.assert_not_called()

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
            ctx, name='test', memory_mb=0, root_gb=1)
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
            ctx, name='test', memory_mb=0, root_gb=1)
        image_meta = mock.Mock()
        injected_files = mock.Mock()
        admin_password = mock.Mock()
        allocations = mock.Mock()
        network_info = [_VIF]
        block_device_info = {'block_device_mapping': []}
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
        self.assertEqual({
            'path': '/config-drive',
            'source': incus_driver._add_configdrive.return_value,
            'type': 'disk',
            'readonly': 'true',
        }, profile.devices['configdrive'])
        profile.save.assert_called_once_with()

    @mock.patch('nova.virt.incus.driver.os.listdir', return_value=[])
    @mock.patch.object(driver.incus_privsep, 'configdrive_umount')
    @mock.patch.object(driver.incus_privsep, 'chown_tree_to_host_id')
    @mock.patch.object(driver.incus_privsep, 'configdrive_mount_iso')
    @mock.patch('nova.virt.incus.driver.configdrive.ConfigDriveBuilder')
    @mock.patch('nova.virt.incus.driver.instance_metadata.InstanceMetadata')
    def test_add_configdrive_uses_modern_instance_metadata_signature(
            self, instance_metadata_mock, builder_mock, mount_mock,
            chown_mock, umount_mock, listdir_mock):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0, root_gb=1)
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
        # The three former rootwrap invocations now run through dedicated
        # privsep entrypoints.
        mount_mock.assert_called_once()
        chown_mock.assert_called_once_with(mock.ANY, 100000)
        umount_mock.assert_called_once()

    @mock.patch('nova.virt.incus.driver.os.listdir', return_value=[])
    @mock.patch.object(driver.incus_privsep, 'configdrive_umount')
    @mock.patch.object(driver.incus_privsep, 'chown_tree_to_host_id')
    @mock.patch.object(driver.incus_privsep, 'configdrive_mount_iso')
    @mock.patch('nova.virt.incus.driver.configdrive.ConfigDriveBuilder')
    @mock.patch('nova.virt.incus.driver.instance_metadata.InstanceMetadata')
    def test_add_configdrive_mounts_outside_the_instance_directory(
            self, instance_metadata_mock, builder_mock, mount_mock,
            chown_mock, umount_mock, listdir_mock):
        """A leaked mount must not make the instance undeletable.

        Instance removal chowns, walks and rmtree's the instance
        directory, and each of those fails on a live read-only mount. The
        mountpoint therefore stays under instances_path - which is what
        the privileged entrypoints require - but outside any instance
        directory.
        """
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0, root_gb=1)
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
            ctx, instance, [], 'secret', [_VIF])

        mountpoint = mount_mock.call_args[0][1]
        instances_path = self.CONF2.instances_path
        instance_dir = os.path.join(instances_path, instance.name)
        self.assertTrue(
            mountpoint.startswith(instances_path + os.sep),
            '%s must stay under instances_path' % mountpoint)
        self.assertFalse(
            mountpoint.startswith(instance_dir + os.sep),
            '%s must not sit inside the instance directory' % mountpoint)
        self.assertIn(driver._CONFIGDRIVE_MOUNT_DIR, mountpoint)

    @mock.patch.object(driver.eventlet, 'sleep')
    @mock.patch.object(driver.incus_privsep, 'configdrive_umount')
    def test_configdrive_umount_retries_a_busy_unmount(self, umount, sleep):
        instance = mock.Mock(uuid='00000000-0000-0000-0000-000000000001')
        umount.side_effect = [
            driver.processutils.ProcessExecutionError('target is busy'), None]

        driver.IncusDriver._umount_configdrive_iso('/mnt/cd', instance)

        self.assertEqual(2, umount.call_count)

    @mock.patch.object(driver.eventlet, 'sleep')
    @mock.patch.object(driver.incus_privsep, 'configdrive_umount')
    def test_configdrive_umount_reports_a_leak_without_failing_the_build(
            self, umount, sleep):
        # The guest is fine and the config drive is already copied, so a
        # stuck unmount must be loud rather than fatal.
        instance = mock.Mock(uuid='00000000-0000-0000-0000-000000000001')
        umount.side_effect = (
            driver.processutils.ProcessExecutionError('busy'))

        with mock.patch.object(driver.LOG, 'error') as error:
            driver.IncusDriver._umount_configdrive_iso('/mnt/cd', instance)

        self.assertEqual(
            driver._CONFIGDRIVE_UMOUNT_ATTEMPTS, umount.call_count)
        error.assert_called_once()
        self.assertIn('operator umount', error.call_args[0][0])

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
            ctx, name='test', memory_mb=0, root_gb=1)
        image_meta = mock.Mock()
        injected_files = mock.Mock()
        admin_password = mock.Mock()
        allocations = mock.Mock()
        network_info = [_VIF]
        block_device_info = {'block_device_mapping': []}
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
        self.begin_idmap_materialization.assert_called_once()

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
            ctx, name='test', memory_mb=0, root_gb=1)
        image_meta = mock.Mock()
        injected_files = mock.Mock()
        admin_password = mock.Mock()
        allocations = mock.Mock()
        network_info = [_VIF]
        block_device_info = {'block_device_mapping': []}
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
        self.begin_idmap_materialization.assert_called_once()

    @mock.patch('nova.virt.configdrive.required_by', return_value=False)
    def test_spawn_container_cleanup_fail(self, configdrive):
        """Cleanup is called but also fail when container creation fails."""
        container = mock.Mock()
        self.client.instances.get.side_effect = [
            incuscore_exceptions.LXDAPIException(MockResponse(404)),
            container,
            container,
        ]
        self.client.instances.create.return_value = container

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0, root_gb=1)
        self.client.profiles.get.return_value.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        }
        image_meta = mock.Mock()
        injected_files = mock.Mock()
        admin_password = mock.Mock()
        allocations = mock.Mock()
        network_info = [_VIF]
        block_device_info = {'block_device_mapping': []}
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
            ctx, name='test', memory_mb=0, root_gb=1)
        image_meta = mock.Mock()
        injected_files = mock.Mock()
        admin_password = mock.Mock()
        allocations = mock.Mock()
        network_info = [_VIF]
        block_device_info = {'block_device_mapping': []}
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

    def _driver_with_vif_event_timeout(self):
        virtapi = mock.Mock()
        waiter = mock.MagicMock()
        waiter.__exit__.side_effect = exception.InstanceEventTimeout()
        virtapi.wait_for_instance_event.return_value = waiter
        incus_driver = driver.IncusDriver(virtapi)
        incus_driver.plug_vifs = mock.Mock()
        incus_driver.cleanup = mock.Mock()
        return incus_driver, virtapi

    def test_spawn_vif_event_timeout_is_nonfatal_when_configured(self):
        self.CONF.vif_plugging_timeout = 5
        self.CONF.vif_plugging_is_fatal = False
        incus_driver, virtapi = self._driver_with_vif_event_timeout()
        ctxt = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctxt, name='test', memory_mb=0)
        network_info = [dict(_VIF, id='vif1', active=False)]
        block_device_info = mock.sentinel.block_device_info

        incus_driver._plug_vifs_for_spawn(
            ctxt, instance, network_info, block_device_info)

        incus_driver.plug_vifs.assert_called_once_with(
            instance, network_info)
        incus_driver.cleanup.assert_not_called()
        virtapi.wait_for_instance_event.assert_called_once_with(
            instance,
            [('network-vif-plugged', 'vif1')],
            timeout=5,
            error_callback=driver._neutron_failed_callback)

    def test_spawn_vif_event_timeout_is_fatal_when_configured(self):
        self.CONF.vif_plugging_timeout = 5
        self.CONF.vif_plugging_is_fatal = True
        incus_driver, _virtapi = self._driver_with_vif_event_timeout()
        ctxt = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctxt, name='test', memory_mb=0)
        network_info = [dict(_VIF, id='vif1', active=False)]
        block_device_info = mock.sentinel.block_device_info

        self.assertRaises(
            exception.VirtualInterfaceCreateException,
            incus_driver._plug_vifs_for_spawn,
            ctxt, instance, network_info, block_device_info)

        incus_driver.plug_vifs.assert_called_once_with(
            instance, network_info)
        incus_driver.cleanup.assert_called_once_with(
            ctxt, instance, network_info, block_device_info)

    @mock.patch('nova.virt.incus.driver.lockutils.lock')
    def test_destroy(self, lock):
        mock_container = mock.Mock()
        mock_container.status = 'Running'
        mock_container.config = {}
        self.client.instances.get.return_value = mock_container
        self.client.storage_pools.get.return_value.driver = 'ceph'
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = [_VIF]

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        self._configure_exact_idmap_release(
            incus_driver, instance, mock_container)
        incus_driver.cleanup = mock.Mock()  # There is a separate cleanup test
        broker = mock.Mock()
        incus_driver._serial_consoles[instance.uuid] = broker

        incus_driver.destroy(ctx, instance, network_info)

        broker.close.assert_called_once_with()
        self.assertNotIn(instance.uuid, incus_driver._serial_consoles)
        self.assertNotIn(
            instance.uuid, incus_driver._serial_console_destroying)
        incus_driver.cleanup.assert_called_once_with(
            ctx, instance, network_info, None,
            destroy_disks=True, destroy_secrets=True)
        self.assertEqual(2, incus_driver.client.instances.get.call_count)
        self.assertEqual(
            [mock.call(instance.name), mock.call(instance.name)],
            incus_driver.client.instances.get.call_args_list)
        mock_container.stop.assert_called_once_with(wait=True)
        mock_container.delete.assert_called_once_with(wait=True)
        tokenized_delete = (
            incus_driver._delete_instance_with_rootfs_release_receipt)
        tokenized_delete.assert_called_once_with(
            mock_container, instance, mock.ANY)

    def test_destroy_rejects_unfinished_volume_evidence_before_container(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        driver._write_managed_attach_intent(
            instance, _TEST_VOLUME_ID,
            '40000000-0000-0000-0000-000000000004', '/dev/vdb')

        self.assertRaises(
            exception.InvalidVolume,
            incus_driver.destroy, ctx, instance, [])

        incus_driver.client.instances.get.assert_not_called()
        incus_driver.client.profiles.get.assert_not_called()

    def test_destroy_rejects_unretired_source_volume_generation(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        token = '20000000-0000-0000-0000-000000000002'
        migration_uuid = '30000000-0000-0000-0000-000000000003'
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
            driver.MIGRATION_ROLLBACK_COMPLETE_KEY: token,
            driver.MIGRATION_NOVA_UUID_KEY: migration_uuid,
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.InvalidVolume,
            incus_driver.destroy, ctx, instance, [])

        incus_driver.client.instances.get.assert_not_called()

    def test_destroy_accepts_retiring_target_migration_uuid(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_NOVA_UUID_KEY:
                '30000000-0000-0000-0000-000000000003',
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver._assert_destroy_volume_transactions_settled(instance)

    def test_prune_orphan_volume_recovery_directory_removes_empty(self):
        instance = mock.Mock(uuid=str(uuid.uuid4()))
        journal_dir = driver._volume_journal_directory(instance)
        os.makedirs(journal_dir)

        self.assertTrue(
            driver._prune_orphan_volume_recovery_directory(instance))
        self.assertFalse(os.path.lexists(journal_dir))

    def test_prune_orphan_volume_recovery_directory_removes_stale_temp(self):
        instance = mock.Mock(uuid=str(uuid.uuid4()))
        journal_dir = driver._volume_journal_directory(instance)
        os.makedirs(journal_dir)
        temporary = os.path.join(journal_dir, '.attach-stale.tmp')
        with open(temporary, 'w', encoding='utf-8'):
            pass
        stale = time.time() - driver._VOLUME_RECOVERY_TMP_STALE_SECONDS - 1
        os.utime(temporary, (stale, stale))

        self.assertTrue(
            driver._prune_orphan_volume_recovery_directory(instance))
        self.assertFalse(os.path.lexists(journal_dir))

    def test_prune_orphan_volume_recovery_directory_keeps_formal_evidence(
            self):
        instance = mock.Mock(uuid=str(uuid.uuid4()))
        journal_dir = driver._volume_journal_directory(instance)
        os.makedirs(journal_dir)
        formal = os.path.join(journal_dir, 'evidence.attach-intent')
        with open(formal, 'w', encoding='utf-8'):
            pass

        self.assertFalse(
            driver._prune_orphan_volume_recovery_directory(instance))
        self.assertTrue(os.path.isfile(formal))

    @mock.patch('nova.virt.incus.driver.lockutils.lock')
    def test_destroy_does_not_claim_protected_migration_target(self, lock):
        mock_container = mock.Mock(
            status='Stopped',
            config={
                'volatile.migration.storage_delete_protection': 'true',
            })
        self.client.instances.get.return_value = mock_container
        self.client.storage_pools.get.return_value.driver = 'ceph'
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = [_VIF]

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        self._configure_exact_idmap_release(
            incus_driver, instance, mock_container)
        incus_driver.cleanup = mock.Mock()

        self.assertRaises(
            exception.MigrationError,
            incus_driver.destroy, ctx, instance, network_info,
            destroy_disks=True)

        mock_container.stop.assert_not_called()
        mock_container.delete.assert_not_called()
        incus_driver.cleanup.assert_not_called()

    @mock.patch.object(driver, '_set_storage_handover_state')
    @mock.patch('nova.virt.incus.driver.lockutils.lock')
    def test_destroy_without_disks_deletes_only_protected_record(
            self, lock, set_handover):
        mock_container = mock.Mock(
            status='Stopped',
            config={
                'volatile.migration.storage_delete_protection': 'true',
            })
        self.client.instances.get.return_value = mock_container
        self.client.storage_pools.get.return_value.driver = 'ceph'
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        self._configure_exact_idmap_release(
            incus_driver, instance, mock_container)
        incus_driver.cleanup = mock.Mock()

        incus_driver.destroy(
            ctx, instance, [_VIF], destroy_disks=False)

        set_handover.assert_called_once_with(
            self.client, instance.name, 'protected',
            container=mock_container)
        mock_container.delete.assert_called_once_with(wait=True)
        incus_driver.cleanup.assert_called_once()

    @mock.patch.object(
        driver, '_storage_handover_is_owned', return_value=True)
    @mock.patch.object(driver, '_converge_migration_target_ownership')
    @mock.patch('nova.virt.incus.driver.lockutils.lock')
    def test_destroy_converges_current_migration_owner_before_final_delete(
            self, lock, converge, is_owned):
        token = '10000000-0000-0000-0000-000000000001'
        container = mock.Mock(status='Stopped', config={})
        self.client.instances.get.return_value = container
        initial_profile = mock.Mock(config={
            driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
        })
        retired_profile = mock.Mock(config={})
        self.client.profiles.get.side_effect = [
            initial_profile, initial_profile, retired_profile,
            retired_profile]
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        self._configure_exact_idmap_release(
            incus_driver, instance, container)
        instance.host = incus_driver.host
        incus_driver.cleanup = mock.Mock()
        incus_driver._acknowledge_cleanup_profile = mock.Mock()

        incus_driver.destroy(ctx, instance, [_VIF], destroy_disks=True)

        converge.assert_called_once_with(
            self.client, instance, local_volume_evidence=True)
        is_owned.assert_called_once_with(
            self.client, instance.name, container=container)
        container.delete.assert_called_once_with(wait=True)
        incus_driver.cleanup.assert_called_once()
        incus_driver._acknowledge_cleanup_profile.assert_not_called()

    @mock.patch.object(
        driver, '_storage_handover_is_owned', return_value=True)
    @mock.patch.object(driver, '_converge_migration_target_ownership')
    @mock.patch('nova.virt.incus.driver.lockutils.lock')
    def test_destroy_blocks_current_owner_with_unretired_migration_token(
            self, lock, converge, is_owned):
        token = '10000000-0000-0000-0000-000000000001'
        container = mock.Mock(status='Stopped', config={})
        self.client.instances.get.return_value = container
        profile = mock.Mock(config={
            driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
        })
        self.client.profiles.get.return_value = profile
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        self._configure_exact_idmap_release(
            incus_driver, instance, container)
        instance.host = incus_driver.host
        incus_driver.cleanup = mock.Mock()
        incus_driver._acknowledge_cleanup_profile = mock.Mock()

        self.assertRaises(
            exception.MigrationError,
            incus_driver.destroy, ctx, instance, [_VIF],
            destroy_disks=True)

        converge.assert_called_once_with(
            self.client, instance, local_volume_evidence=True)
        container.delete.assert_not_called()
        incus_driver.cleanup.assert_not_called()
        incus_driver._acknowledge_cleanup_profile.assert_not_called()

    @mock.patch.object(driver, '_set_storage_handover_state')
    @mock.patch.object(driver, '_converge_migration_target_ownership')
    @mock.patch('nova.virt.incus.driver.lockutils.lock')
    def test_destroy_without_disks_keeps_current_host_in_loser_cleanup(
            self, lock, converge, set_handover):
        token = '10000000-0000-0000-0000-000000000001'
        container = mock.Mock(status='Stopped', config={
            'volatile.migration.storage_delete_protection': 'true',
        })
        self.client.instances.get.return_value = container
        self.client.storage_pools.get.return_value.driver = 'ceph'
        self.client.profiles.get.return_value.config = {
            driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
        }
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        self._configure_exact_idmap_release(
            incus_driver, instance, container)
        instance.host = incus_driver.host
        incus_driver._cleanup = mock.Mock()
        incus_driver._acknowledge_cleanup_profile = mock.Mock()

        incus_driver.destroy(
            ctx, instance, [_VIF], destroy_disks=False)

        converge.assert_not_called()
        set_handover.assert_called_once_with(
            self.client, instance.name, 'protected', container=container)
        incus_driver._cleanup.assert_called_once_with(
            ctx, instance, [_VIF], block_device_info=None,
            destroy_disks=False, destroy_secrets=True,
            delete_profile=False)
        incus_driver._acknowledge_cleanup_profile.assert_called_once_with(
            instance, token)

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
        self._configure_exact_idmap_release(
            incus_driver, instance, mock_container)
        incus_driver.cleanup = mock.Mock()

        incus_driver.destroy(ctx, instance, network_info)

        self.assertEqual(2, mock_container.stop.call_count)
        mock_container.delete.assert_called_once_with(wait=True)
        incus_driver.cleanup.assert_called_once_with(
            ctx, instance, network_info, None,
            destroy_disks=True, destroy_secrets=True)

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
        self._configure_exact_idmap_release(
            incus_driver, instance, mock_container)
        incus_driver.cleanup = mock.Mock(
            side_effect=RuntimeError('profile still in use'))

        self.assertRaises(
            incuscore_exceptions.LXDAPIException,
            incus_driver.destroy, ctx, instance, network_info)

        self.assertEqual(
            self.CONF.incus.migration_finish_retries,
            mock_container.stop.call_count)
        self.assertNotIn(
            instance.uuid, incus_driver._serial_console_destroying)
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
        self._configure_exact_idmap_release(
            incus_driver, instance, mock_container)
        incus_driver.cleanup = mock.Mock()

        incus_driver.destroy(ctx, instance, network_info)

        mock_container.stop.assert_not_called()
        self.assertEqual(2, mock_container.delete.call_count)
        incus_driver.cleanup.assert_called_once_with(
            ctx, instance, network_info, None,
            destroy_disks=True, destroy_secrets=True)

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
        self._configure_exact_idmap_release(
            incus_driver, instance, stopped_container, running_container)
        incus_driver.cleanup = mock.Mock()

        incus_driver.destroy(ctx, instance, network_info)

        self.assertEqual(3, self.client.instances.get.call_count)
        stopped_container.stop.assert_not_called()
        stopped_container.delete.assert_called_once_with(wait=True)
        running_container.stop.assert_called_once_with(wait=True)
        running_container.delete.assert_called_once_with(wait=True)
        incus_driver.cleanup.assert_called_once_with(
            ctx, instance, network_info, None,
            destroy_disks=True, destroy_secrets=True)

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
        self._configure_exact_idmap_release(
            incus_driver, instance, mock_container)
        incus_driver.cleanup = mock.Mock()

        incus_driver.destroy(ctx, instance, network_info)

        self.assertEqual(2, mock_container.stop.call_count)
        mock_container.delete.assert_called_once_with(wait=True)
        incus_driver.cleanup.assert_called_once_with(
            ctx, instance, network_info, None,
            destroy_disks=True, destroy_secrets=True)

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
        self._configure_exact_idmap_release(
            incus_driver, instance, mock_container)
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
        self._configure_exact_idmap_release(
            incus_driver, instance, mock_stopped_container)
        incus_driver.cleanup = mock.Mock()

        # set the vm_state on the fake instance to RESCUED
        instance.vm_state = vm_states.RESCUED

        # set up the containers.get to return the stopped container and then
        # the rescued container
        self.client.instances.get.side_effect = [
            mock_stopped_container, mock_rescued_container]

        incus_driver.destroy(ctx, instance, network_info)

        incus_driver.cleanup.assert_called_once_with(
            ctx, instance, network_info, None,
            destroy_disks=True, destroy_secrets=True)
        incus_driver.client.instances.get.assert_has_calls([
            mock.call(instance.name),
            mock.call('{}-rescue'.format(instance.name))])
        mock_stopped_container.stop.assert_not_called()
        mock_stopped_container.delete.assert_called_once_with(wait=True)
        mock_rescued_container.stop.assert_called_once_with(wait=True)
        mock_rescued_container.delete.assert_called_once_with(wait=True)

    @mock.patch.object(driver.LOG, 'debug')
    @mock.patch('nova.virt.incus.driver.lockutils.lock')
    def test_destroy_without_instance(self, lock, debug):
        def side_effect(*args, **kwargs):
            raise incuscore_exceptions.LXDAPIException(MockResponse(404))
        self.client.instances.get.side_effect = side_effect

        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = [_VIF]

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        self._configure_exact_idmap_release(incus_driver, instance)
        incus_driver.cleanup = mock.Mock()  # There is a separate cleanup test

        incus_driver.destroy(ctx, instance, network_info)
        incus_driver.cleanup.assert_called_once_with(
            ctx, instance, network_info, None,
            destroy_disks=True, destroy_secrets=True)
        debug.assert_called_once_with(
            "Incus container is already absent for "
            "%(instance)s; continuing idempotent cleanup.",
            {'instance': instance.name})

    @mock.patch.object(driver.LOG, 'debug')
    @mock.patch('nova.virt.incus.driver.lockutils.lock')
    def test_destroy_without_instance_accepts_async_not_found(
            self, lock, debug):
        self.client.instances.get.side_effect = incus_operation_exception(
            404, 'Instance not found')
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = [_VIF]

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        self._configure_exact_idmap_release(incus_driver, instance)
        incus_driver.cleanup = mock.Mock()

        incus_driver.destroy(ctx, instance, network_info)

        incus_driver.cleanup.assert_called_once_with(
            ctx, instance, network_info, None,
            destroy_disks=True, destroy_secrets=True)
        debug.assert_called_once_with(
            "Incus container is already absent for "
            "%(instance)s; continuing idempotent cleanup.",
            {'instance': instance.name})

    @mock.patch('nova.virt.incus.driver.neutron')
    @mock.patch('os.path.exists', mock.Mock(return_value=True))
    @mock.patch.object(driver.os, 'getgid', return_value=1001)
    @mock.patch.object(driver.os, 'getuid', return_value=1001)
    @mock.patch('shutil.rmtree')
    @mock.patch.object(driver.privsep_path, 'chown')
    def test_cleanup(self, chown, rmtree, getuid, getgid, _):
        mock_profile = mock.Mock(devices={}, config={}, used_by=[])
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

    @mock.patch.object(driver.LOG, 'debug')
    @mock.patch.object(driver.IncusDriver, 'unplug_vifs')
    @mock.patch.object(driver, '_remove_instance_directory')
    def test_cleanup_missing_profile_is_idempotent(
            self, remove_instance_directory, unplug_vifs,
            debug):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', root_gb=1)
        self.client.profiles.get.side_effect = (
            incuscore_exceptions.LXDAPIException(MockResponse(404)))
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.firewall_driver = mock.Mock()

        incus_driver.cleanup(ctx, instance, [], {'block_device_mapping': []})

        remove_instance_directory.assert_called_once_with(instance)
        debug.assert_called_once_with(
            "Incus profile is already absent for %(instance)s; "
            "cleanup is complete.",
            {'instance': instance.name})

    @mock.patch.object(driver, '_cleanup_share_journal_mounts')
    @mock.patch.object(
        driver, '_cleanup_profile_share_mounts',
        side_effect=exception.ShareUmountError(
            share_id='10000000-0000-0000-0000-000000000001',
            server_id='20000000-0000-0000-0000-000000000002',
            reason='busy'))
    @mock.patch.object(driver, '_remove_instance_directory')
    def test_cleanup_manila_failure_marks_and_retains_profile(
            self, remove_instance_directory,
            cleanup_shares, cleanup_journals):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        profile = self.client.profiles.get.return_value
        profile.devices = {}
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        }
        profile.used_by = []
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._disconnect_profile_volume_connections = mock.Mock(
            return_value=[])

        self.assertRaises(
            exception.MigrationError, incus_driver._cleanup,
            ctx, instance, [], block_device_info=None,
            destroy_vifs=False, delete_profile=True)

        self.assertEqual(
            'true', profile.config[driver.CLEANUP_RECOVERY_KEY])
        profile.save.assert_called_once_with(wait=True)
        profile.delete.assert_not_called()
        cleanup_shares.assert_called_once_with(profile, instance)
        cleanup_journals.assert_called_once_with(instance)

    @mock.patch.object(driver, '_cleanup_share_journal_mounts')
    @mock.patch.object(
        driver, '_cleanup_profile_share_mounts',
        side_effect=exception.ShareUmountError(
            share_id='10000000-0000-0000-0000-000000000001',
            server_id='20000000-0000-0000-0000-000000000002',
            reason='busy'))
    @mock.patch.object(driver, '_remove_instance_directory')
    def test_cleanup_marks_profile_its_own_container_still_uses(
            self, remove_instance_directory,
            cleanup_shares, cleanup_journals):
        """The retained container is why the profile is retained.

        A cleanup that could not finish leaves this instance's own
        container behind. Reading that as "in use" refused to write the
        recovery marker at exactly the moment it was needed, which
        stranded the profile and pinned its idmap allocation forever.
        """
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        profile = self.client.profiles.get.return_value
        profile.devices = {}
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        }
        profile.used_by = [
            '/1.0/instances/{}?project=nova'.format(instance.name),
            '/1.0/instances/{}-rescue'.format(instance.name),
        ]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._disconnect_profile_volume_connections = mock.Mock(
            return_value=[])

        self.assertRaises(
            exception.MigrationError, incus_driver._cleanup,
            ctx, instance, [], block_device_info=None,
            destroy_vifs=False, delete_profile=True)

        self.assertEqual(
            'true', profile.config[driver.CLEANUP_RECOVERY_KEY])
        profile.save.assert_called_once_with(wait=True)

    @mock.patch.object(driver, '_cleanup_share_journal_mounts')
    @mock.patch.object(
        driver, '_cleanup_profile_share_mounts',
        side_effect=exception.ShareUmountError(
            share_id='10000000-0000-0000-0000-000000000001',
            server_id='20000000-0000-0000-0000-000000000002',
            reason='busy'))
    @mock.patch.object(driver, '_remove_instance_directory')
    def test_cleanup_refuses_to_mark_a_profile_another_instance_uses(
            self, remove_instance_directory,
            cleanup_shares, cleanup_journals):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        profile = self.client.profiles.get.return_value
        profile.devices = {}
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        }
        profile.used_by = ['/1.0/instances/instance-0000dead?project=nova']
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._disconnect_profile_volume_connections = mock.Mock(
            return_value=[])

        self.assertRaises(
            exception.MigrationError, incus_driver._cleanup,
            ctx, instance, [], block_device_info=None,
            destroy_vifs=False, delete_profile=True)

        self.assertNotIn(driver.CLEANUP_RECOVERY_KEY, profile.config)
        profile.save.assert_not_called()

    def test_profile_users_other_than_reads_every_reference_form(self):
        instance = mock.Mock()
        instance.name = 'instance-00000001'
        profile = mock.Mock(used_by=[
            '/1.0/instances/instance-00000001?project=nova',
            '/1.0/containers/instance-00000001',
            '/1.0/instances/instance-00000001-rescue',
            '/1.0/instances/instance-00000002?project=other',
        ])

        self.assertEqual(
            ['instance-00000002'],
            driver._profile_users_other_than(profile, instance))

    def test_profile_users_other_than_treats_junk_as_foreign(self):
        """An unreadable reference is not proof of self-ownership."""
        instance = mock.Mock()
        instance.name = 'instance-00000001'

        self.assertEqual(
            [], driver._profile_users_other_than(
                mock.Mock(used_by=None), instance))
        self.assertEqual(
            [12345], driver._profile_users_other_than(
                mock.Mock(used_by=[12345]), instance))

    @mock.patch.object(driver.IncusDriver, 'unplug_vifs')
    @mock.patch.object(driver.os.path, 'exists', return_value=False)
    def test_cleanup_disconnects_data_volume_before_profile_delete(
            self, _exists, _unplug_vifs):
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

        def detach(*args, **kwargs):
            profile.devices.clear()
            profile.config.clear()

        incus_driver._detach_volume = mock.Mock(side_effect=detach)

        incus_driver.cleanup(
            ctx, instance, [], block_device_info, destroy_vifs=False)

        incus_driver._detach_volume.assert_called_once_with(
            ctx, connection_info, instance, '/dev/sdb',
            retain_journal=False)
        profile.delete.assert_called_once_with()

    @mock.patch.object(driver.os.path, 'exists', return_value=False)
    def test_cleanup_does_not_reread_profile_per_data_volume(
            self, _exists):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        volume_ids = ('data-volume-1', 'data-volume-2')
        block_device_info = {'block_device_mapping': [
            {
                'boot_index': None,
                'connection_info': {
                    'driver_volume_type': 'rbd',
                    'serial': volume_id,
                    'data': {'volume_id': volume_id},
                },
                'mount_device': '/dev/sd%s' % chr(ord('b') + index),
            }
            for index, volume_id in enumerate(volume_ids)
        ]}
        profile = self.client.profiles.get.return_value
        profile.devices = {
            volume_id: {'type': 'unix-block'}
            for volume_id in volume_ids
        }
        profile.config = {
            driver._volume_device_info_key(volume_id): '{}'
            for volume_id in volume_ids
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._disconnect_profile_volume_connections = mock.Mock(
            return_value=[])

        def detach(
                _context, connection_info, _instance, _mountpoint,
                retain_journal=False):
            self.assertFalse(retain_journal)
            volume_id = connection_info['serial']
            profile.devices.pop(volume_id)
            profile.config.pop(driver._volume_device_info_key(volume_id))

        incus_driver._detach_volume = mock.Mock(side_effect=detach)

        incus_driver.cleanup(
            ctx, instance, [], block_device_info, destroy_vifs=False)

        self.assertEqual(2, incus_driver._detach_volume.call_count)
        # One initial snapshot and one final deletion-safety read. The number
        # is independent of the number of Nova BDMs.
        self.assertEqual(2, self.client.profiles.get.call_count)
        profile.delete.assert_called_once_with()

    @mock.patch.object(driver.IncusDriver, 'unplug_vifs')
    @mock.patch.object(driver.os.path, 'exists', return_value=False)
    def test_cleanup_retains_profile_when_data_disconnect_fails(
            self, _exists, _unplug_vifs):
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
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver._volume_device_info_key('data-volume'): '{}'}
        profile.used_by = []
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.firewall_driver = mock.Mock()
        incus_driver._detach_volume = mock.Mock(
            side_effect=RuntimeError('disconnect failed'))

        self.assertRaises(
            exception.MigrationError, incus_driver.cleanup, ctx, instance, [],
            block_device_info, destroy_vifs=False)

        profile.delete.assert_not_called()
        self.assertEqual(
            'true', profile.config[driver.CLEANUP_RECOVERY_KEY])

    def test_reboot(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.reboot(ctx, instance, None, None)

        self.assertEqual(2, self.client.instances.get.call_count)
        self.client.instances.get.return_value.restart.assert_called_once_with(
            force=True, wait=True)
        self.ensure_start_idmap.assert_called_once_with(
            instance, self.client.instances.get.return_value,
            _claim_lock_held=True)

    def test_reboot_idmap_failure_blocks_restart(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        container = self.client.instances.get.return_value
        failure = driver.incus_idmap.IDMapIntegrityError(
            reason='allocator generation is absent')
        self.ensure_start_idmap.side_effect = failure
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        raised = self.assertRaises(
            driver.incus_idmap.IDMapIntegrityError,
            incus_driver.reboot, ctx, instance, None, 'HARD')

        self.assertIs(failure, raised)
        container.restart.assert_not_called()
        container.start.assert_not_called()

    def test_soft_reboot_is_graceful(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.reboot(ctx, instance, None, 'SOFT')

        self.client.instances.get.return_value.restart.assert_called_once_with(
            force=False, wait=True)

    def test_reboot_reasserts_vifs_after_start(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        vif = mock.sentinel.vif
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.reboot(ctx, instance, [vif], 'HARD')

        self.vif_driver.reassert.assert_called_once_with(instance, vif)

    def test_soft_reboot_falls_back_to_hard(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        container = self.client.instances.get.return_value
        container.restart.side_effect = [
            incus_api_exception(400, 'guest did not shut down'),
            None,
        ]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.reboot(ctx, instance, None, 'SOFT')

        self.assertEqual([
            mock.call(force=False, wait=True),
            mock.call(force=True, wait=True),
        ], container.restart.call_args_list)

    def test_soft_reboot_does_not_mask_authorization_failure(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        container = self.client.instances.get.return_value
        failure = incus_api_exception(403, 'not authorized')
        container.restart.side_effect = failure
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        raised = self.assertRaises(
            incuscore_exceptions.LXDAPIException,
            incus_driver.reboot, ctx, instance, None, 'SOFT')

        self.assertIs(failure, raised)
        container.restart.assert_called_once_with(force=False, wait=True)

    def test_reboot_busy_retry_refreshes_instance_model(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        initial = mock.Mock(status='Running')
        busy = mock.Mock(status='Running')
        replacement = mock.Mock(status='Running')
        busy.restart.side_effect = incus_operation_exception(
            409, 'Instance is busy running an "update" operation')
        self.client.instances.get.side_effect = [
            initial, busy, replacement]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.reboot(ctx, instance, None, 'HARD')

        busy.restart.assert_called_once_with(force=True, wait=True)
        replacement.restart.assert_called_once_with(force=True, wait=True)

    def test_reboot_stopped_converges_after_lost_start_response(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        initial = mock.Mock(status='Stopped')
        starting = mock.Mock(status='Stopped')
        running = mock.Mock(status='Running')
        starting.start.side_effect = (
            incuscore_exceptions.ClientConnectionFailed('response lost'))
        self.client.instances.get.side_effect = [
            initial, starting, running]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.reboot(ctx, instance, None, 'HARD')

        starting.start.assert_called_once_with(wait=True)
        running.start.assert_not_called()

    @mock.patch.object(driver.IncusDriver, '_cleanup')
    def test_cleanup_lingering_bfv_source_record(self, cleanup):
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
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        fixture = self._configure_exact_idmap_release(
            incus_driver, instance, container,
            storage_driver='cephext', cleanup_disposition='detach',
            outcome='detached')
        tokenized_delete = fixture[-1]
        incus_driver._instance_inventory_cache = mock.sentinel.instances
        incus_driver._metric_devices_cache = mock.sentinel.devices
        incus_driver._metric_instance_devices_cache = {
            instance.name: mock.sentinel.device}
        incus_driver._disk_metrics_cache = mock.sentinel.metrics

        result = incus_driver.cleanup_lingering_instance_resources(instance)

        self.assertTrue(result)
        self.assertIsNone(incus_driver._instance_inventory_cache)
        self.assertIsNone(incus_driver._metric_devices_cache)
        self.assertEqual({}, incus_driver._metric_instance_devices_cache)
        self.assertIsNone(incus_driver._disk_metrics_cache)
        tokenized_delete.assert_called_once_with(
            container, instance, mock.ANY, client=self.client)
        container.delete.assert_called_once_with(wait=True)
        cleanup.assert_called_once_with(
            None, instance, [], block_device_info=None,
            destroy_vifs=False, delete_profile=True)

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

    @mock.patch.object(driver.IncusDriver, '_cleanup')
    def test_cleanup_lingering_replays_journals_after_record_is_absent(
            self, cleanup):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test')
        self.client.instances.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        result = incus_driver.cleanup_lingering_instance_resources(instance)

        self.assertTrue(result)
        cleanup.assert_called_once_with(
            None, instance, [], block_device_info=None,
            destroy_vifs=False, delete_profile=True)

    def test_list_cleanup_recovery_candidates_uses_recursive_inventory(self):
        response = self.client.api.profiles.get.return_value
        response.json.return_value = {'metadata': [
            {
                'name': 'instance-b',
                'config': {
                    'environment.product_name': 'OpenStack Nova',
                    'user.openstack.uuid':
                        '20000000-0000-0000-0000-000000000002',
                    driver.CLEANUP_RECOVERY_KEY: 'true',
                },
            },
            {
                'name': 'foreign',
                'config': {
                    'environment.product_name': 'other',
                    'user.openstack.uuid':
                        '30000000-0000-0000-0000-000000000003',
                    driver.CLEANUP_RECOVERY_KEY: 'true',
                },
            },
            {
                'name': 'instance-a',
                'config': {
                    'environment.product_name': 'OpenStack Nova',
                    'user.openstack.uuid':
                        '10000000-0000-0000-0000-000000000001',
                    driver.CLEANUP_RECOVERY_KEY: 'true',
                },
            },
        ]}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertEqual([
            {
                'name': 'instance-a',
                'uuid': '10000000-0000-0000-0000-000000000001',
            },
            {
                'name': 'instance-b',
                'uuid': '20000000-0000-0000-0000-000000000002',
            },
        ], incus_driver.list_cleanup_recovery_candidates())
        self.client.api.profiles.get.assert_called_once_with(
            params={'recursion': 1})

    def test_lists_destination_prepared_profiles_after_journal_loss(self):
        token = '20000000-0000-0000-0000-000000000002'
        migration_uuid = '30000000-0000-0000-0000-000000000003'
        instance_uuid = '10000000-0000-0000-0000-000000000001'
        response = self.client.api.profiles.get.return_value
        response.json.return_value = {'metadata': [{
            'name': 'instance-target',
            'config': {
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance_uuid,
                driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
                driver.MIGRATION_DESTINATION_PREPARED_KEY: token,
                driver.MIGRATION_NOVA_UUID_KEY: migration_uuid,
                'security.idmap.base': '1065536',
                'security.idmap.size': '65536',
            },
        }]}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertEqual([{
            'name': 'instance-target',
            'uuid': instance_uuid,
            'operation_token': token,
            'migration_uuid': migration_uuid,
            'idmap_base': 1065536,
            'idmap_size': 65536,
        }], incus_driver.list_destination_prepared_recovery_candidates())

    def test_lists_source_volume_generation_after_evidence_unlink(self):
        token = '20000000-0000-0000-0000-000000000002'
        migration_uuid = '30000000-0000-0000-0000-000000000003'
        instance_uuid = '10000000-0000-0000-0000-000000000001'
        response = self.client.api.profiles.get.return_value
        response.json.return_value = {'metadata': [{
            'name': 'instance-source',
            'config': {
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance_uuid,
                driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
                driver.MIGRATION_ROLLBACK_COMPLETE_KEY: token,
                driver.MIGRATION_NOVA_UUID_KEY: migration_uuid,
            },
        }]}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertEqual([{
            'name': 'instance-source',
            'uuid': instance_uuid,
            'operation_token': token,
            'migration_uuid': migration_uuid,
            'rollback_complete': True,
        }], incus_driver.list_source_volume_generation_recovery_candidates())

    def test_lists_incomplete_live_source_rollback_generation(self):
        token = '20000000-0000-0000-0000-000000000002'
        migration_uuid = '30000000-0000-0000-0000-000000000003'
        instance_uuid = '10000000-0000-0000-0000-000000000001'
        response = self.client.api.profiles.get.return_value
        response.json.return_value = {'metadata': [{
            'name': 'instance-source',
            'config': {
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance_uuid,
                driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
                driver.MIGRATION_NOVA_UUID_KEY: migration_uuid,
            },
        }]}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertEqual([{
            'name': 'instance-source',
            'uuid': instance_uuid,
            'operation_token': token,
            'migration_uuid': migration_uuid,
            'rollback_complete': False,
        }], incus_driver.list_source_volume_generation_recovery_candidates())

    def test_failed_spawn_owner_allows_exact_missing_container_recovery(self):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='instance-source')
        generation = '20000000-0000-0000-0000-000000000002'
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.SPAWN_VOLUME_GENERATION_KEY: generation,
        }
        self.client.instances.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.validate_internal_volume_attach_owner(instance, {
            'operation_kind': 'spawn',
            'operation_token': generation,
        })

        self.client.instances.get.assert_called_once_with(instance.name)

    @mock.patch.object(driver, '_managed_attach_intents_by_uuid')
    def test_source_volume_generation_marks_and_retires_exact_owner(
            self, intents):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='instance-source')
        token = '20000000-0000-0000-0000-000000000002'
        migration_uuid = '30000000-0000-0000-0000-000000000003'
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
        }
        intents.return_value = {}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.mark_source_volume_generation_rollback_complete(
            instance, token, migration_uuid)

        self.assertEqual(
            token, profile.config[driver.MIGRATION_ROLLBACK_COMPLETE_KEY])
        self.assertEqual(
            migration_uuid, profile.config[driver.MIGRATION_NOVA_UUID_KEY])
        self.assertTrue(incus_driver.finalize_source_volume_generation(
            instance, token, require_rollback_complete=True))
        self.assertNotIn(driver.MIGRATION_CLEANUP_TOKEN_KEY, profile.config)
        self.assertNotIn(
            driver.MIGRATION_ROLLBACK_COMPLETE_KEY, profile.config)
        self.assertNotIn(driver.MIGRATION_NOVA_UUID_KEY, profile.config)

    @mock.patch.object(driver, '_retire_migration_attempt')
    @mock.patch.object(driver, '_migration_client')
    @mock.patch.object(driver, '_managed_attach_intents_by_uuid')
    def test_remote_source_generation_retries_before_token_retirement(
            self, intents, migration_client, retire_attempt):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='instance-source')
        token = '20000000-0000-0000-0000-000000000002'
        migration_uuid = '30000000-0000-0000-0000-000000000003'
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
            driver.MIGRATION_ROLLBACK_COMPLETE_KEY: token,
            driver.MIGRATION_NOVA_UUID_KEY: migration_uuid,
            driver.MIGRATION_DESTINATION_KEY:
                'https://192.0.2.20:8443',
            'security.idmap.base': '1065536',
            'security.idmap.size': '65536',
        }
        profile.devices = {}
        intents.return_value = {}
        response = MockResponse(404)
        migration_client.return_value.instances.get.side_effect = (
            incuscore_exceptions.NotFound(response))
        migration_client.return_value.profiles.get.side_effect = (
            incuscore_exceptions.NotFound(response))
        retire_attempt.side_effect = [
            RuntimeError('target unavailable'), None]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            RuntimeError,
            incus_driver.finalize_remote_source_volume_generation,
            instance, token)
        self.assertEqual(
            token, profile.config[driver.MIGRATION_CLEANUP_TOKEN_KEY])

        self.assertTrue(
            incus_driver.finalize_remote_source_volume_generation(
                instance, token))
        self.assertNotIn(driver.MIGRATION_CLEANUP_TOKEN_KEY, profile.config)
        self.assertEqual(2, retire_attempt.call_count)

    @mock.patch.object(driver, '_migration_client')
    @mock.patch.object(driver, '_managed_attach_intents_by_uuid')
    def test_pre_live_rollback_marks_before_remote_retirement(
            self, intents, migration_client):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='instance-pre-live-source')
        token = '20000000-0000-0000-0000-000000000002'
        migration_uuid = '30000000-0000-0000-0000-000000000003'
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
            driver.MIGRATION_DESTINATION_KEY: 'https://192.0.2.20:8443',
            'security.idmap.base': '1065536',
            'security.idmap.size': '65536',
        }
        profile.devices = {}
        intents.return_value = {}
        response = MockResponse(404)
        migration_client.return_value.instances.get.side_effect = (
            incuscore_exceptions.NotFound(response))
        migration_client.return_value.profiles.get.side_effect = (
            incuscore_exceptions.NotFound(response))
        migration_client.return_value.api.__getitem__.return_value.\
            __getitem__.return_value.delete.return_value = None
        data = migrate_data.IncusLiveMigrateData(
            cleanup_token=token, migration_uuid=migration_uuid,
            destination_address='https://192.0.2.20:8443',
            idmap_base=1065536, idmap_size=65536)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertTrue(incus_driver.finalize_pre_live_migration_rollback(
            instance, data))

        self.assertNotIn(driver.MIGRATION_CLEANUP_TOKEN_KEY, profile.config)
        self.assertNotIn(
            driver.MIGRATION_ROLLBACK_COMPLETE_KEY, profile.config)

    @mock.patch.object(driver, '_migration_client')
    @mock.patch.object(driver, '_managed_attach_intents_by_uuid')
    def test_pre_live_rollback_retains_generation_while_target_exists(
            self, intents, migration_client):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='instance-pre-live-source')
        token = '20000000-0000-0000-0000-000000000002'
        migration_uuid = '30000000-0000-0000-0000-000000000003'
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
            driver.MIGRATION_DESTINATION_KEY: 'https://192.0.2.20:8443',
            'security.idmap.base': '1065536',
            'security.idmap.size': '65536',
        }
        profile.devices = {}
        intents.return_value = {}
        migration_client.return_value.instances.get.return_value = mock.Mock()
        data = migrate_data.IncusLiveMigrateData(
            cleanup_token=token, migration_uuid=migration_uuid,
            destination_address='https://192.0.2.20:8443',
            idmap_base=1065536, idmap_size=65536)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.MigrationError,
            incus_driver.finalize_pre_live_migration_rollback,
            instance, data)

        self.assertEqual(
            token, profile.config[driver.MIGRATION_CLEANUP_TOKEN_KEY])
        self.assertEqual(
            token, profile.config[driver.MIGRATION_ROLLBACK_COMPLETE_KEY])
        self.assertEqual(
            migration_uuid, profile.config[driver.MIGRATION_NOVA_UUID_KEY])

    def test_host_wide_locks_are_named_per_instance(self):
        """The lock NAME keys the in-process semaphore, nothing else.

        lockutils.lock(name, lock_file_prefix=None, external=False,
        lock_path=None) keys internal_lock on name alone; lock_file_prefix
        reaches only external_lock. Passing a constant path positionally
        gave every destroy, image sync and snapshot in one nova-compute
        process the same mutex, which serialized about three quarters of
        every delete against every other one on the host.
        """
        source = inspect.getsource(driver)
        for call in re.findall(
                r'lockutils\.lock\(\s*([^,)]+)', source):
            name = call.strip()
            self.assertNotIn(
                'lock_path', name,
                'lockutils.lock() was given a path as its lock name; the '
                'name keys the in-process semaphore, so a shared path '
                'serializes unrelated instances')

    def test_profile_recovery_periodics_share_one_listing(self):
        """Discovery is shared when all profile periodics run together."""
        response = self.client.api.profiles.get.return_value
        response.json.return_value = {'metadata': [{
            'name': 'instance-a',
            'config': {
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid':
                    '10000000-0000-0000-0000-000000000001',
                driver.CLEANUP_RECOVERY_KEY: 'true',
            },
        }]}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        self.client.api.profiles.get.reset_mock()

        incus_driver.list_cleanup_recovery_candidates()
        incus_driver.list_destination_prepared_recovery_candidates()
        incus_driver.list_source_volume_generation_recovery_candidates()

        self.client.api.profiles.get.assert_called_once_with(
            params={'recursion': 1})

        # A mutation must drop the shared snapshot, not serve it past a
        # profile write.
        incus_driver._invalidate_instance_inventory_cache()
        incus_driver.list_cleanup_recovery_candidates()
        self.assertEqual(2, self.client.api.profiles.get.call_count)

    def test_profile_snapshot_rejects_malformed_inventory(self):
        response = self.client.api.profiles.get.return_value
        response.json.return_value = {'metadata': 'not-a-list'}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.InvalidConfiguration,
            incus_driver.list_cleanup_recovery_candidates)

    @mock.patch.object(driver.IncusDriver, '_acknowledge_cleanup_profile')
    @mock.patch.object(driver.IncusDriver, '_cleanup')
    def test_destination_prepared_committed_without_instance_is_retained(
            self, cleanup, acknowledge):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='instance-target')
        token = '20000000-0000-0000-0000-000000000002'
        migration_uuid = '30000000-0000-0000-0000-000000000003'
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
            driver.MIGRATION_DESTINATION_PREPARED_KEY: token,
            driver.MIGRATION_NOVA_UUID_KEY: migration_uuid,
            'security.idmap.base': '1065536',
            'security.idmap.size': '65536',
        }
        self.client.instances.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        self._get_migration_attempt.return_value = {
            'state': 'committed', 'finished': True}
        candidate = {
            'name': instance.name,
            'uuid': instance.uuid,
            'operation_token': token,
            'migration_uuid': migration_uuid,
            'idmap_base': 1065536,
            'idmap_size': 65536,
        }
        migration = mock.Mock(uuid=migration_uuid, status='completed')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.MigrationError,
            incus_driver.recover_destination_prepared_profile,
            mock.sentinel.context, instance, candidate, migration,
            mock.sentinel.network_info)

        cleanup.assert_not_called()
        acknowledge.assert_not_called()

    @mock.patch.object(driver, '_converge_migration_target_ownership')
    @mock.patch.object(driver.IncusDriver, '_acknowledge_cleanup_profile')
    @mock.patch.object(driver.IncusDriver, '_cleanup')
    def test_destination_prepared_committed_target_never_unmounts(
            self, cleanup, acknowledge, converge):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='instance-target',
            host='destination-compute')
        token = '20000000-0000-0000-0000-000000000002'
        migration_uuid = '30000000-0000-0000-0000-000000000003'
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
            driver.MIGRATION_DESTINATION_PREPARED_KEY: token,
            driver.MIGRATION_NOVA_UUID_KEY: migration_uuid,
            'security.idmap.base': '1065536',
            'security.idmap.size': '65536',
        }
        self.client.instances.get.return_value = mock.Mock(config={
            'user.openstack.uuid': instance.uuid,
        })
        self._get_migration_attempt.return_value = {
            'state': 'committed', 'finished': True}
        candidate = {
            'name': instance.name,
            'uuid': instance.uuid,
            'operation_token': token,
            'migration_uuid': migration_uuid,
            'idmap_base': 1065536,
            'idmap_size': 65536,
        }
        migration = mock.Mock(
            uuid=migration_uuid, status='completed',
            dest_compute='destination-compute')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertTrue(incus_driver.recover_destination_prepared_profile(
            mock.sentinel.context, instance, candidate, migration,
            mock.sentinel.network_info))

        converge.assert_called_once_with(
            self.client, instance, local_volume_evidence=True)
        cleanup.assert_not_called()
        acknowledge.assert_not_called()

    @mock.patch.object(driver.IncusDriver, '_acknowledge_cleanup_profile')
    @mock.patch.object(driver.IncusDriver, '_cleanup')
    def test_destination_prepared_uncommitted_owner_fails_closed(
            self, cleanup, acknowledge):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='instance-target',
            host='destination-compute')
        token = '20000000-0000-0000-0000-000000000002'
        migration_uuid = '30000000-0000-0000-0000-000000000003'
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
            driver.MIGRATION_DESTINATION_PREPARED_KEY: token,
            driver.MIGRATION_NOVA_UUID_KEY: migration_uuid,
            'security.idmap.base': '1065536',
            'security.idmap.size': '65536',
        }
        self.client.instances.get.return_value = mock.Mock(config={
            'user.openstack.uuid': instance.uuid,
        })
        self._get_migration_attempt.return_value = {
            'state': 'active', 'finished': False}
        candidate = {
            'name': instance.name,
            'uuid': instance.uuid,
            'operation_token': token,
            'migration_uuid': migration_uuid,
            'idmap_base': 1065536,
            'idmap_size': 65536,
        }
        migration = mock.Mock(
            uuid=migration_uuid, status='error',
            dest_compute='destination-compute')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.MigrationError,
            incus_driver.recover_destination_prepared_profile,
            mock.sentinel.context, instance, candidate, migration,
            mock.sentinel.network_info)

        self._abort_migration_attempt.assert_not_called()
        cleanup.assert_not_called()
        acknowledge.assert_not_called()

    @mock.patch.object(driver.IncusDriver, '_acknowledge_cleanup_profile')
    @mock.patch.object(driver.IncusDriver, '_cleanup')
    def test_destination_prepared_profile_only_cleanup_is_idempotent(
            self, cleanup, acknowledge):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='instance-target',
            host='source-compute')
        token = '20000000-0000-0000-0000-000000000002'
        migration_uuid = '30000000-0000-0000-0000-000000000003'
        volume_id = '40000000-0000-0000-0000-000000000004'
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
            driver.MIGRATION_DESTINATION_PREPARED_KEY: token,
            driver.MIGRATION_NOVA_UUID_KEY: migration_uuid,
            driver._volume_device_info_key(volume_id):
                driver._serialize_volume_attachment(
                    {
                        'driver_volume_type': 'rbd',
                        'serial': volume_id,
                        'data': {'name': 'volumes/volume-%s' % volume_id},
                    },
                    {'path': '/dev/rbd0'}, '/dev/vdb', phase='connected'),
            'security.idmap.base': '1065536',
            'security.idmap.size': '65536',
        }
        self.client.instances.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        self._get_migration_attempt.return_value = {
            'state': 'active', 'finished': False}
        self._abort_migration_attempt.return_value = {
            'state': 'aborted', 'finished': True}
        candidate = {
            'name': instance.name,
            'uuid': instance.uuid,
            'operation_token': token,
            'migration_uuid': migration_uuid,
            'idmap_base': 1065536,
            'idmap_size': 65536,
        }
        migration = mock.Mock(uuid=migration_uuid, status='error')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertTrue(incus_driver.recover_destination_prepared_profile(
            mock.sentinel.context, instance, candidate, migration,
            mock.sentinel.network_info))

        cleanup.assert_called_once_with(
            mock.sentinel.context, instance, mock.sentinel.network_info,
            block_device_info=None, destroy_vifs=True,
            delete_profile=False)
        acknowledge.assert_called_once_with(instance, token)

    @mock.patch.object(driver.IncusDriver, '_acknowledge_cleanup_profile')
    @mock.patch.object(driver.IncusDriver, '_cleanup')
    def test_recover_destination_cleanup_profile_persists_ack(
            self, cleanup, acknowledge):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test')
        token = '10000000-0000-0000-0000-000000000001'
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
            driver.CLEANUP_RECOVERY_KEY: 'true',
            'security.idmap.base': '1065536',
            'security.idmap.size': '65536',
        }
        self.client.instances.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        self._get_migration_attempt.return_value = {
            'state': 'aborted', 'finished': True}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.recover_cleanup_profile(
            mock.sentinel.context, instance, mock.sentinel.network_info)

        cleanup.assert_called_once_with(
            mock.sentinel.context, instance, mock.sentinel.network_info,
            block_device_info=None, destroy_vifs=True,
            delete_profile=False)
        acknowledge.assert_called_once_with(instance, token)
        self._get_migration_attempt.assert_called_once_with(
            self.client, instance, token, 1065536, 65536)

    @mock.patch.object(driver.IncusDriver, '_cleanup')
    def test_recover_destination_cleanup_refuses_committed_attempt(
            self, cleanup):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test')
        token = '10000000-0000-0000-0000-000000000001'
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
            driver.CLEANUP_RECOVERY_KEY: 'true',
            'security.idmap.base': '1065536',
            'security.idmap.size': '65536',
        }
        self._get_migration_attempt.return_value = {
            'state': 'committed', 'finished': True}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.MigrationError,
            incus_driver.recover_cleanup_profile,
            mock.sentinel.context, instance, mock.sentinel.network_info)

        cleanup.assert_not_called()

    @mock.patch.object(driver.IncusDriver, '_cleanup')
    def test_recover_cleanup_refuses_existing_instance_record(self, cleanup):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test')
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_DESTINATION_KEY:
                'https://compute-2.example.test:8443',
            driver.CLEANUP_RECOVERY_KEY: 'true',
        }
        self.client.instances.get.return_value = mock.Mock(status='Stopped')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.MigrationError,
            incus_driver.recover_cleanup_profile,
            mock.sentinel.context, instance, mock.sentinel.network_info)

        cleanup.assert_not_called()

    @mock.patch.object(driver.IncusDriver, '_acknowledge_cleanup_profile')
    @mock.patch.object(driver.IncusDriver, '_cleanup')
    def test_recover_source_cleanup_profile_deletes_after_replay(
            self, cleanup, acknowledge):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test')
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY:
                '10000000-0000-0000-0000-000000000001',
            driver.MIGRATION_DESTINATION_KEY:
                'https://compute-2.example.test:8443',
            driver.CLEANUP_RECOVERY_KEY: 'true',
        }
        self.client.instances.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.recover_cleanup_profile(
            mock.sentinel.context, instance, mock.sentinel.network_info)

        cleanup.assert_called_once_with(
            mock.sentinel.context, instance, mock.sentinel.network_info,
            block_device_info=None, destroy_vifs=True,
            delete_profile=True)
        acknowledge.assert_not_called()

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

    @mock.patch('nova.virt.incus.driver.neutron')
    def test_get_console_output_tail_reads_host_file(self, _):
        # The host-side log file is read with _last_bytes so a huge console
        # log cannot balloon nova-compute memory; the API is not consulted.
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        log_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, log_dir, ignore_errors=True)
        console_path = os.path.join(log_dir, 'console.log')
        with open(console_path, 'wb') as f:
            f.write(b'y' * 100 + b'x' * driver.MAX_CONSOLE_BYTES)
        attributes = mock.Mock(console_path=console_path)

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        with mock.patch.object(
                driver.common, 'InstanceAttributes',
                return_value=attributes):
            contents = incus_driver.get_console_output(context, instance)

        self.assertEqual(b'x' * driver.MAX_CONSOLE_BYTES, contents)
        self.client.instances.get.assert_not_called()

    @mock.patch('nova.virt.incus.driver.neutron')
    def test_get_console_output_reports_an_unreadable_host_log(self, _):
        """An unreadable log defeats the bounded read on every request.

        The log directory is usually root-owned, so a non-root compute
        service falls back to the unbounded API read forever. Swallowing
        the error left no trace of why memory tracked console size.
        """
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        attributes = mock.Mock(console_path='/var/log/incus/console.log')
        self.client.instances.get.return_value.console_log.return_value = (
            b'x' * driver.MAX_CONSOLE_BYTES)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        with mock.patch.object(
                driver.common, 'InstanceAttributes',
                return_value=attributes), \
                mock.patch(
                    'builtins.open',
                    side_effect=PermissionError(13, 'Permission denied')), \
                mock.patch.object(driver.LOG, 'warning') as warning:
            contents = incus_driver.get_console_output(context, instance)

        # Still serves the console, but says why the bound was lost.
        self.assertEqual(b'x' * driver.MAX_CONSOLE_BYTES, contents)
        warning.assert_called_once()
        self.assertIn('unbounded', warning.call_args[0][0])

    @mock.patch('nova.virt.incus.driver.neutron')
    def test_get_console_output_is_quiet_before_the_guest_writes(self, _):
        # A guest that has not written to its console yet is ordinary and
        # must not produce a warning on every request.
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        attributes = mock.Mock(console_path='/var/log/incus/console.log')
        self.client.instances.get.return_value.console_log.return_value = b''
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        with mock.patch.object(
                driver.common, 'InstanceAttributes',
                return_value=attributes), \
                mock.patch(
                    'builtins.open',
                    side_effect=FileNotFoundError(2, 'No such file')), \
                mock.patch.object(driver.LOG, 'warning') as warning:
            contents = incus_driver.get_console_output(context, instance)

        self.assertEqual(b'', contents)
        warning.assert_not_called()

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
            'attachment_id':
                'a231d2e8-1111-4222-8333-123456789abc',
            'connection_info': {
                'serial': _TEST_VOLUME_ID,
                'driver_volume_type': 'rbd',
                'data': {
                    'name': 'volumes/volume-%s' % _TEST_VOLUME_ID,
                },
            },
            'mount_device': '/dev/vdb',
        }
        get_mapping.return_value = [root, data]
        profile = self.client.profiles.get.return_value
        profile.devices = {}
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        }
        container = self.client.instances.get.return_value
        container.status = 'Stopped'
        container.devices = {}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        def attach(_ctx, connection_info, _instance, mountpoint, *_args,
                   **_kwargs):
            profile.devices[_TEST_VOLUME_ID] = {
                'type': 'unix-block',
                'path': mountpoint,
                'required': 'true',
                'source': '/dev/rbd0',
            }
            profile.config[
                driver._volume_device_info_key(_TEST_VOLUME_ID)] = (
                driver._serialize_volume_attachment(
                    connection_info, {'path': '/dev/rbd0'}, mountpoint))

        incus_driver._attach_and_commit_internal_volume_operation = (
            mock.Mock(side_effect=attach))

        with mock.patch.object(
                driver, '_mapped_rbd_device', return_value='/dev/rbd0'):
            incus_driver.reboot(
                ctx, instance, None, 'HARD', block_device_info={})

        incus_driver._attach_and_commit_internal_volume_operation.\
            assert_called_once_with(
                ctx, data['connection_info'], instance, '/dev/vdb',
                data['attachment_id'], 'reconcile', mock.ANY,
                'power-reconcile')
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

    def test_lists_migration_recovery_candidates_with_one_recursive_call(self):
        marked = mock.Mock(
            type='container',
            status='Stopped',
            config={'user.openstack.uuid': 'marked-uuid'},
            expanded_config={driver.MIGRATION_RECOVERY_KEY: 'running'},
            expanded_devices={
                'root': {
                    'initial.ceph.rbd.image_name': 'volume-marked',
                },
            },
            devices={})
        marked.name = 'instance-marked'
        running = mock.Mock(
            type='container',
            status='Running',
            config={'user.openstack.uuid': 'running-uuid'},
            expanded_config={driver.MIGRATION_RECOVERY_KEY: 'running'},
            expanded_devices={
                'root': {
                    'initial.ceph.rbd.image_name': 'volume-running',
                },
            },
            devices={})
        running.name = 'instance-running'
        unmarked = mock.Mock(
            type='container',
            status='Stopped',
            config={'user.openstack.uuid': 'unmarked-uuid'},
            expanded_config={},
            expanded_devices={
                'root': {
                    'initial.ceph.rbd.image_name': 'volume-unmarked',
                },
            },
            devices={})
        unmarked.name = 'instance-unmarked'
        self.client.instances.all.return_value = [running, unmarked, marked]
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client
        instance = mock.Mock()
        instance.name = 'instance-test'

        self.assertEqual(
            [
                {'name': 'instance-marked', 'uuid': 'marked-uuid'},
                {'name': 'instance-running', 'uuid': 'running-uuid'},
            ],
            incus_driver.list_migration_recovery_candidates())
        self.client.instances.all.assert_called_once_with(recursion=1)
        self.client.profiles.get.assert_not_called()

    def test_storage_handover_rejects_unnegotiated_ceph(self):
        self.client.api.instances = mock.MagicMock()
        container = mock.Mock(config={}, expanded_devices={
            'root': {'pool': 'ceph-root'},
        })
        self.client.storage_pools.get.return_value.driver = 'ceph'

        self.assertRaises(
            exception.MigrationError,
            driver._set_storage_handover_state,
            self.client, 'instance-test', 'protected',
            container=container)

        self.client.storage_pools.get.assert_called_once_with('ceph-root')
        self.client.api.instances.__getitem__.assert_not_called()

    def test_storage_handover_requires_persisted_delete_protection(self):
        self.client.api.instances = mock.MagicMock()
        container = mock.Mock(config={
            'volatile.migration.storage_handover': 'pending',
            'volatile.migration.storage_handover_role': 'source',
        }, expanded_devices={
            'root': {'pool': 'ceph-root'},
        })
        self.client.storage_pools.get.return_value.driver = 'ceph'
        self.client.instances.get.return_value = container

        self.assertRaises(
            exception.MigrationError,
            driver._set_storage_handover_state,
            self.client, 'instance-test', 'protected',
            container=container)

        endpoint = self.client.api.instances[
            'instance-test']['storage-handover']
        endpoint.put.assert_called_once_with(
            params={'project': self.CONF.incus.project},
            json={'state': 'protected'})

    def test_storage_handover_commits_only_negotiated_ceph(self):
        self.client.api.instances = mock.MagicMock()
        container = mock.Mock(config={
            'volatile.migration.storage_handover': 'committed',
            'volatile.migration.storage_delete_protection': 'true',
        }, expanded_devices={
            'root': {'pool': 'ceph-root'},
        })
        pool = self.client.storage_pools.get.return_value
        pool.driver = 'ceph'
        self.client.host_info['api_extensions'].append(
            driver.INCUS_STORAGE_HANDOVER_PROOF_EXTENSION)
        self.client.instances.get.return_value = mock.Mock(
            config={}, expanded_devices={
                'root': {'pool': 'ceph-root'},
            })

        self.assertTrue(driver._set_storage_handover_state(
            self.client, 'instance-test', 'owned',
            container=container,
            migration_attempt='10000000-0000-0000-0000-000000000001',
            operation_uuid='20000000-0000-0000-0000-000000000002'))

        endpoint = self.client.api.instances[
            'instance-test']['storage-handover']
        endpoint.put.assert_called_once_with(
            params={'project': self.CONF.incus.project},
            json={
                'state': 'owned',
                'migration_attempt':
                    '10000000-0000-0000-0000-000000000001',
                'operation_uuid':
                    '20000000-0000-0000-0000-000000000002',
            })

    def test_storage_handover_rejects_unpersisted_owned_state(self):
        self.client.api.instances = mock.MagicMock()
        container = mock.Mock(config={
            'volatile.migration.storage_handover': 'committed',
            'volatile.migration.storage_delete_protection': 'true',
        }, expanded_devices={
            'root': {'pool': 'ceph-root'},
        })
        pool = self.client.storage_pools.get.return_value
        pool.driver = 'ceph'
        self.client.host_info['api_extensions'].append(
            driver.INCUS_STORAGE_HANDOVER_PROOF_EXTENSION)
        self.client.instances.get.return_value = container

        self.assertRaises(
            exception.MigrationError,
            driver._set_storage_handover_state,
            self.client, 'instance-test', 'owned',
            container=container,
            migration_attempt='10000000-0000-0000-0000-000000000001',
            operation_uuid='20000000-0000-0000-0000-000000000002')

    def test_storage_handover_restores_committed_source(self):
        self.client.api.instances = mock.MagicMock()
        container = mock.Mock(config={
            'volatile.migration.storage_handover': 'committed',
            'volatile.migration.storage_handover_role': 'source',
            'volatile.migration.storage_delete_protection': 'true',
        }, expanded_devices={
            'root': {'pool': 'ceph-root'},
        })
        self.client.storage_pools.get.return_value.driver = 'ceph'
        self.client.host_info['api_extensions'].append(
            driver.INCUS_STORAGE_HANDOVER_PROOF_EXTENSION)
        self.client.instances.get.return_value = mock.Mock(
            config={}, expanded_devices={
                'root': {'pool': 'ceph-root'},
            })

        self.assertTrue(driver._set_storage_handover_state(
            self.client, 'instance-test', 'source-owned',
            container=container))

        endpoint = self.client.api.instances[
            'instance-test']['storage-handover']
        endpoint.put.assert_called_once_with(
            params={'project': self.CONF.incus.project},
            json={'state': 'source-owned'})

    def test_storage_handover_rejects_target_as_source_owner(self):
        self.client.api.instances = mock.MagicMock()
        container = mock.Mock(config={
            'volatile.migration.storage_handover': 'committed',
            'volatile.migration.storage_handover_role': 'target',
            'volatile.migration.storage_delete_protection': 'true',
        }, expanded_devices={
            'root': {'pool': 'ceph-root'},
        })
        self.client.storage_pools.get.return_value.driver = 'ceph'

        self.assertRaises(
            exception.MigrationError,
            driver._set_storage_handover_state,
            self.client, 'instance-test', 'source-owned',
            container=container)

        self.client.api.instances.__getitem__.assert_not_called()

    def test_storage_handover_owned_rejects_missing_receive_proof(self):
        container = mock.Mock(config={
            'volatile.migration.storage_handover': 'committed',
        }, expanded_devices={
            'root': {'pool': 'ceph-root'},
        })
        self.client.storage_pools.get.return_value.driver = 'ceph'
        profile = self.client.profiles.get.return_value
        profile.config = {
            driver.MIGRATION_CLEANUP_TOKEN_KEY:
                '10000000-0000-0000-0000-000000000001',
        }

        self.assertRaises(
            exception.MigrationError,
            driver._set_storage_handover_state,
            self.client, 'instance-test', 'owned',
            container=container)

    def test_shared_storage_check_requires_negotiated_handover(self):
        instance = mock.Mock()
        instance.name = 'instance-test'
        instance.uuid = '10000000-0000-0000-0000-000000000001'
        container = mock.Mock(config={}, expanded_devices={
            'root': {'pool': 'ceph-root'},
        })
        self.client.instances.get.return_value = container
        pool = self.client.storage_pools.get.return_value
        pool.driver = 'ceph'
        pool.config = {
            'source': 'incus-rootfs',
            'ceph.cluster_name': 'ceph',
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertEqual(
            {
                'shared': False,
                'driver': 'ceph',
                'cluster': 'ceph',
                'source': 'incus-rootfs',
                'instance_name': 'instance-test',
            },
            incus_driver.check_instance_shared_storage_local(
                mock.sentinel.context,
                instance))

        container.config[
            'volatile.migration.storage_delete_protection'] = 'true'
        self.assertTrue(
            incus_driver.check_instance_shared_storage_local(
                mock.sentinel.context,
                instance)['shared'])

    @mock.patch.object(driver.LOG, 'error')
    def test_recovery_candidates_reject_duplicate_uuid(self, error):
        containers = []
        for name in ('instance-current', 'instance-stale'):
            container = mock.Mock(
                type='container',
                status='Stopped',
                config={'user.openstack.uuid': 'duplicate-uuid'},
                expanded_config={driver.MIGRATION_RECOVERY_KEY: 'true'},
                expanded_devices={
                    'root': {
                        'initial.ceph.rbd.image_name': 'volume-duplicate',
                    },
                },
                devices={})
            container.name = name
            containers.append(container)
        self.client.instances.all.return_value = containers
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertEqual(
            [], incus_driver.list_migration_recovery_candidates())
        error.assert_called_once()

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
        incus_driver._instance_inventory_cache = mock.sentinel.instances
        incus_driver._metric_devices_cache = mock.sentinel.devices
        incus_driver._metric_instance_devices_cache = {
            instance.name: mock.sentinel.device}
        incus_driver._disk_metrics_cache = mock.sentinel.metrics

        should_run = incus_driver.recover_migration_target(
            ctx, instance, [], {'block_device_mapping': []})

        self.assertFalse(should_run)
        self.assertIsNone(incus_driver._instance_inventory_cache)
        self.assertIsNone(incus_driver._metric_devices_cache)
        self.assertEqual({}, incus_driver._metric_instance_devices_cache)
        self.assertIsNone(incus_driver._disk_metrics_cache)
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
    def test_reboot_repairs_inconsistent_data_volume_before_start(
            self, get_mapping):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        connection_info = {
            'serial': _TEST_VOLUME_ID,
            'driver_volume_type': 'rbd',
            'data': {
                'name': 'volumes/volume-%s' % _TEST_VOLUME_ID,
            },
        }
        get_mapping.return_value = [{
            'boot_index': 1,
            'attachment_id':
                'a231d2e8-1111-4222-8333-123456789abc',
            'connection_info': connection_info,
            'mount_device': '/dev/vdb',
        }]
        profile = self.client.profiles.get.return_value
        profile.devices = {
            _TEST_VOLUME_ID: {
                'type': 'unix-block', 'path': '/dev/vdc'}}
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver._volume_device_info_key(_TEST_VOLUME_ID):
                driver._serialize_volume_attachment(
                    connection_info, {'path': '/dev/rbd9'}, '/dev/vdc'),
        }
        container = self.client.instances.get.return_value
        container.status = 'Stopped'
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        def disconnect(*args, **kwargs):
            profile.devices.pop(_TEST_VOLUME_ID, None)
            profile.config.pop(
                driver._volume_device_info_key(_TEST_VOLUME_ID), None)

        def attach(_ctx, requested, _instance, mountpoint, *_args,
                   **_kwargs):
            profile.devices[_TEST_VOLUME_ID] = {
                'type': 'unix-block',
                'path': mountpoint,
                'required': 'true',
                'source': '/dev/rbd0',
            }
            profile.config[
                driver._volume_device_info_key(_TEST_VOLUME_ID)] = (
                driver._serialize_volume_attachment(
                    requested, {'path': '/dev/rbd0'}, mountpoint))

        incus_driver._disconnect_profile_volume_connection = mock.Mock(
            side_effect=disconnect)
        incus_driver._attach_and_commit_internal_volume_operation = (
            mock.Mock(side_effect=attach))

        with mock.patch.object(
                driver, '_mapped_rbd_device', return_value='/dev/rbd0'):
            incus_driver.reboot(
                ctx, instance, None, 'HARD', block_device_info={})

        disconnect = incus_driver._disconnect_profile_volume_connection
        disconnect.assert_called_once_with(
            ctx, instance, _TEST_VOLUME_ID,
            connection_info=connection_info, mountpoint='/dev/vdb')
        incus_driver._attach_and_commit_internal_volume_operation.\
            assert_called_once_with(
                ctx, connection_info, instance, '/dev/vdb',
                'a231d2e8-1111-4222-8333-123456789abc', 'reconcile',
                mock.ANY, 'power-reconcile')
        container.start.assert_called_once_with(wait=True)
        container.restart.assert_not_called()

    def test_power_on_accepts_exact_real_bdm_volume_topology(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-exact-data-topology', memory_mb=0)
        volume_id = '8231d2e8-1111-4222-8333-123456789abc'
        connection_info = {
            'driver_volume_type': 'rbd',
            'serial': volume_id,
            'data': {'name': 'volumes/volume-%s' % volume_id},
        }
        bdm = real_volume_driver_bdm(
            ctx, volume_id, '/dev/vdb', None, connection_info)
        profile = self.client.profiles.get.return_value
        profile.devices = {
            volume_id: {
                'type': 'unix-block',
                'path': '/dev/vdb',
                'required': 'true',
                'source': '/dev/rbd0',
            },
        }
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver._volume_device_info_key(volume_id):
                driver._serialize_volume_attachment(
                    connection_info, {'path': '/dev/rbd0'}, '/dev/vdb'),
        }
        container = self.client.instances.get.return_value
        container.status = 'Stopped'
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._attach_volume = mock.Mock()
        incus_driver._disconnect_profile_volume_connection = mock.Mock()

        with mock.patch.object(
                driver, '_mapped_rbd_device', return_value='/dev/rbd0'):
            incus_driver.power_on(
                ctx, instance, None, {'block_device_mapping': [bdm]})

        incus_driver._attach_volume.assert_not_called()
        incus_driver._disconnect_profile_volume_connection.assert_not_called()
        container.start.assert_called_once_with(wait=True)

    def test_power_on_cleans_only_proven_extra_data_volume(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-extra-data-topology', memory_mb=0)
        volume_id = 'stale-volume'
        connection_info = {
            'driver_volume_type': 'rbd',
            'serial': volume_id,
            'data': {'name': 'volumes/volume-stale'},
        }
        profile = self.client.profiles.get.return_value
        profile.devices = {
            volume_id: {
                'type': 'unix-block', 'path': '/dev/vdb',
                'source': '/dev/rbd9'},
        }
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver._volume_device_info_key(volume_id):
                driver._serialize_volume_attachment(
                    connection_info, {'path': '/dev/rbd9'}, '/dev/vdb'),
        }
        container = self.client.instances.get.return_value
        container.status = 'Stopped'
        events = []

        def disconnect(*_args, **_kwargs):
            events.append('disconnect')
            profile.devices.pop(volume_id)
            profile.config.pop(driver._volume_device_info_key(volume_id))

        container.start.side_effect = lambda **_kwargs: events.append('start')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._disconnect_profile_volume_connection = mock.Mock(
            side_effect=disconnect)

        incus_driver.power_on(
            ctx, instance, None, {'block_device_mapping': []})

        self.assertEqual(['disconnect', 'start'], events)
        disconnect_mock = incus_driver._disconnect_profile_volume_connection
        disconnect_mock.assert_called_once_with(ctx, instance, volume_id)

    def test_volume_journal_recovery_replays_only_terminal_disconnects(self):
        """Guest access is durably removed before host disconnect replay."""
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-journal-recovery', memory_mb=0)
        volume_id = '00000000-0000-0000-0000-0000000000aa'
        connection_info = {
            'driver_volume_type': 'rbd',
            'serial': volume_id,
            'data': {'name': 'volumes/volume-recover'},
        }
        driver._write_volume_journal(
            instance, volume_id, connection_info, {'path': '/dev/rbd7'},
            '/dev/vdb', phase='disconnecting')
        profile = mock.Mock(
            devices={
                volume_id: {
                    'type': 'unix-block',
                    'path': '/dev/vdb',
                    'source': '/dev/rbd7',
                },
            },
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver._volume_device_info_key(volume_id):
                    driver._serialize_volume_attachment(
                        connection_info, {'path': '/dev/rbd7'}, '/dev/vdb',
                        phase='disconnecting'),
            })
        self.client.profiles.get.return_value = profile
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        candidates = incus_driver.list_volume_journal_recovery_candidates()
        candidate = next(
            c for c in candidates if c['uuid'] == instance.uuid)
        self.assertEqual([volume_id], candidate['volume_ids'])
        self.assertEqual(
            {volume_id: 'disconnecting'}, candidate['phases'])

        connector = mock.Mock()
        events = []
        profile.save.side_effect = lambda **kwargs: events.append(
            'profile-save')
        connector.disconnect_volume.side_effect = (
            lambda *_args, **_kwargs: events.append('disconnect'))
        with mock.patch.object(
                driver, 'brick_get_connector', return_value=connector):
            incus_driver._recover_disconnecting_volume_journal_locked(
                ctx, instance, volume_id, connection_info,
                expected_mountpoint='/dev/vdb')

        self.assertEqual(['profile-save', 'disconnect'], events)
        connector.disconnect_volume.assert_called_once_with(
            mock.ANY, {'path': '/dev/rbd7'})
        self.assertEqual(
            'volumes/volume-recover',
            connector.disconnect_volume.call_args[0][0]['name'])
        self.assertNotIn(volume_id, profile.devices)
        self.assertEqual(
            'disconnected',
            driver._read_volume_journal(instance, volume_id)['phase'])

    def test_source_cleanup_retains_live_release_terminal_journal(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-source-release-cleanup', memory_mb=0)
        volume_id = '00000000-0000-0000-0000-0000000000ac'
        attachment_id = '40000000-0000-0000-0000-000000000004'
        cleanup_token = '50000000-0000-0000-0000-000000000005'
        migration_uuid = '60000000-0000-0000-0000-000000000006'
        connection_info = {
            'driver_volume_type': 'rbd',
            'serial': volume_id,
            'data': {'name': 'volumes/volume-source-release'},
        }
        driver._write_volume_journal(
            instance, volume_id, connection_info, {'path': '/dev/rbd7'},
            '/dev/vdb', phase='disconnected')
        profile = mock.Mock(
            devices={},
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
            })
        self.client.profiles.get.return_value = profile
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        intent = incus_driver.prepare_managed_volume_attach(
            instance, volume_id, attachment_id, '/dev/vdb',
            operation_kind='migration', operation_token=cleanup_token,
            operation_direction='live-source-release',
            operation_migration_uuid=migration_uuid)
        connector = mock.Mock()

        with mock.patch.object(
                driver, 'brick_get_connector', return_value=connector):
            failures = incus_driver._disconnect_profile_volume_connections(
                ctx, instance)

        self.assertEqual([], failures)
        self.assertEqual(
            'disconnected',
            driver._read_volume_journal(instance, volume_id)['phase'])
        self.assertEqual(
            intent,
            incus_driver.get_managed_volume_attach_intent(
                instance, volume_id))
        connector.disconnect_volume.assert_not_called()

    def test_source_cleanup_serializes_connected_live_release(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-connected-source-release', memory_mb=0)
        volume_id = '00000000-0000-0000-0000-0000000000ad'
        attachment_id = '40000000-0000-0000-0000-000000000014'
        cleanup_token = '50000000-0000-0000-0000-000000000015'
        migration_uuid = '60000000-0000-0000-0000-000000000016'
        connection_info = {
            'driver_volume_type': 'rbd',
            'serial': volume_id,
            'data': {'name': 'volumes/volume-source-connected'},
        }
        profile = mock.Mock(
            devices={
                volume_id: {
                    'type': 'unix-block',
                    'path': '/dev/vdb',
                    'source': '/dev/rbd7',
                },
            },
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
                driver._volume_device_info_key(volume_id):
                    driver._serialize_volume_attachment(
                        connection_info, {'path': '/dev/rbd7'}, '/dev/vdb'),
            })
        self.client.profiles.get.return_value = profile
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.prepare_managed_volume_attach(
            instance, volume_id, attachment_id, '/dev/vdb',
            operation_kind='migration', operation_token=cleanup_token,
            operation_direction='live-source-release',
            operation_migration_uuid=migration_uuid)
        connector = mock.Mock()
        active_locks = []
        expected_locks = [
            driver._volume_manager_transaction_lock_name(
                instance.uuid, volume_id),
            driver._volume_topology_lock_name(instance),
            driver._volume_operation_lock_name(volume_id),
        ]

        @contextmanager
        def fake_lock(name, *args, **kwargs):
            active_locks.append(name)
            try:
                yield
            finally:
                self.assertEqual(name, active_locks.pop())

        def disconnect(*args, **kwargs):
            self.assertEqual(expected_locks, active_locks[:3])

        connector.disconnect_volume.side_effect = disconnect
        with mock.patch.object(
                driver.lockutils, 'lock', side_effect=fake_lock):
            with mock.patch.object(
                    driver, 'brick_get_connector', return_value=connector):
                failures = incus_driver._disconnect_profile_volume_connections(
                    ctx, instance)

        self.assertEqual([], failures)
        self.assertEqual([], active_locks)
        connector.disconnect_volume.assert_called_once_with(
            mock.ANY, {'path': '/dev/rbd7'})
        self.assertEqual(
            'disconnected',
            driver._read_volume_journal(instance, volume_id)['phase'])

    def test_source_cleanup_serializes_disconnecting_live_release(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-disconnecting-source-release', memory_mb=0)
        volume_id = '00000000-0000-0000-0000-0000000000ae'
        attachment_id = '40000000-0000-0000-0000-000000000024'
        cleanup_token = '50000000-0000-0000-0000-000000000025'
        migration_uuid = '60000000-0000-0000-0000-000000000026'
        connection_info = {
            'driver_volume_type': 'rbd',
            'serial': volume_id,
            'data': {'name': 'volumes/volume-source-disconnecting'},
        }
        driver._write_volume_journal(
            instance, volume_id, connection_info, {'path': '/dev/rbd8'},
            '/dev/vdb', phase='disconnecting')
        profile = mock.Mock(
            devices={},
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
                driver._volume_device_info_key(volume_id):
                    driver._serialize_volume_attachment(
                        connection_info, {'path': '/dev/rbd8'}, '/dev/vdb',
                        phase='disconnecting'),
            })
        self.client.profiles.get.return_value = profile
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.prepare_managed_volume_attach(
            instance, volume_id, attachment_id, '/dev/vdb',
            operation_kind='migration', operation_token=cleanup_token,
            operation_direction='live-source-release',
            operation_migration_uuid=migration_uuid)
        connector = mock.Mock()
        active_locks = []
        expected_locks = [
            driver._volume_manager_transaction_lock_name(
                instance.uuid, volume_id),
            driver._volume_topology_lock_name(instance),
            driver._volume_operation_lock_name(volume_id),
        ]

        @contextmanager
        def fake_lock(name, *args, **kwargs):
            active_locks.append(name)
            try:
                yield
            finally:
                self.assertEqual(name, active_locks.pop())

        def disconnect(*args, **kwargs):
            self.assertEqual(expected_locks, active_locks[:3])

        connector.disconnect_volume.side_effect = disconnect
        with mock.patch.object(
                driver.lockutils, 'lock', side_effect=fake_lock):
            with mock.patch.object(
                    driver, 'brick_get_connector', return_value=connector):
                failures = incus_driver._disconnect_profile_volume_connections(
                    ctx, instance)

        self.assertEqual([], failures)
        self.assertEqual([], active_locks)
        connector.disconnect_volume.assert_called_once_with(
            mock.ANY, {'path': '/dev/rbd8'})
        self.assertEqual(
            'disconnected',
            driver._read_volume_journal(instance, volume_id)['phase'])

    def test_volume_journal_recovery_retains_a_still_mapped_volume(self):
        """A failed guest-device update blocks every host-side effect."""
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-journal-retained', memory_mb=0)
        volume_id = '00000000-0000-0000-0000-0000000000bb'
        connection_info = {
            'driver_volume_type': 'rbd',
            'serial': volume_id,
            'data': {'name': 'volumes/volume-retained'},
        }
        driver._write_volume_journal(
            instance, volume_id, connection_info, {'path': '/dev/rbd8'},
            '/dev/vdb', phase='disconnecting')
        profile = mock.Mock(
            devices={
                volume_id: {
                    'type': 'unix-block',
                    'path': '/dev/vdb',
                    'source': '/dev/rbd8',
                },
            },
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver._volume_device_info_key(volume_id):
                    driver._serialize_volume_attachment(
                        connection_info, {'path': '/dev/rbd8'}, '/dev/vdb',
                        phase='disconnecting'),
            })
        persisted = mock.Mock(
            devices=dict(profile.devices), config=dict(profile.config))
        profile.save.side_effect = RuntimeError('Incus update failed')
        self.client.profiles.get.side_effect = [profile, persisted]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        connector = mock.Mock()
        with mock.patch.object(
                driver, 'brick_get_connector', return_value=connector):
            self.assertRaises(
                RuntimeError,
                incus_driver._recover_disconnecting_volume_journal_locked,
                ctx, instance, volume_id, connection_info,
                expected_mountpoint='/dev/vdb')

        connector.disconnect_volume.assert_not_called()
        self.assertEqual(
            'disconnecting',
            driver._read_volume_journal(instance, volume_id)['phase'])

    def test_volume_candidate_uses_managed_attach_generation_for_cleanup(self):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test-attach-cleanup-owner',
            memory_mb=0)
        volume_id = '00000000-0000-0000-0000-0000000000bc'
        attachment_id = '00000000-0000-0000-0000-0000000000bd'
        connection_info = {
            'driver_volume_type': 'rbd',
            'serial': volume_id,
            'data': {'name': 'volumes/volume-attach-cleanup'},
        }
        driver._write_managed_attach_intent(
            instance, volume_id, attachment_id, '/dev/vdb')
        driver._write_volume_journal(
            instance, volume_id, connection_info, {'path': '/dev/rbd8'},
            '/dev/vdb', phase='disconnecting')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        candidate = next(
            item for item in
            incus_driver.list_volume_journal_recovery_candidates()
            if item['uuid'] == instance.uuid)

        self.assertEqual(
            {volume_id: 'attach-disconnecting'}, candidate['phases'])

    def test_volume_candidate_includes_pre_driver_detach_intent(self):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test-detach-pending-owner',
            memory_mb=0)
        volume_id = '00000000-0000-0000-0000-0000000000be'
        attachment_id = '00000000-0000-0000-0000-0000000000bf'
        driver._write_managed_detach_intent(
            instance, volume_id, attachment_id, True, '/dev/vdb')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        candidate = next(
            item for item in
            incus_driver.list_volume_journal_recovery_candidates()
            if item['uuid'] == instance.uuid)

        self.assertEqual(
            {volume_id: 'detach-pending'}, candidate['phases'])

    def test_managed_volume_intents_reject_another_operation_generation(self):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test-volume-intent-fence',
            memory_mb=0)
        first_volume = '00000000-0000-0000-0000-0000000000c0'
        second_volume = '00000000-0000-0000-0000-0000000000c1'
        first_attachment = '00000000-0000-0000-0000-0000000000c2'
        second_attachment = '00000000-0000-0000-0000-0000000000c3'
        driver._write_managed_attach_intent(
            instance, first_volume, first_attachment, '/dev/vdb')
        driver._write_managed_detach_intent(
            instance, second_volume, second_attachment, True, '/dev/vdc')

        self.assertRaises(
            exception.InvalidVolume,
            driver._write_managed_detach_intent,
            instance, first_volume, first_attachment, True, '/dev/vdb')
        self.assertRaises(
            exception.InvalidVolume,
            driver._write_managed_attach_intent,
            instance, second_volume, second_attachment, '/dev/vdc')

    def test_cold_attachment_rotation_journal_is_cas_and_enumerable(self):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test-cold-rotation-journal',
            memory_mb=0)
        volume_id = '00000000-0000-0000-0000-0000000000d0'
        old_attachment = '00000000-0000-0000-0000-0000000000d1'
        new_attachment = '00000000-0000-0000-0000-0000000000d2'
        token = '00000000-0000-0000-0000-0000000000d3'
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        intent = incus_driver.prepare_managed_volume_attach(
            instance, volume_id, old_attachment, '/dev/vdb',
            operation_kind='migration', operation_token=token,
            operation_direction='cold-source-restore',
            operation_migration_uuid=token)

        prepared, created = incus_driver.prepare_cold_attachment_rotation(
            instance, volume_id, old_attachment, '/dev/vdb', token, token,
            [old_attachment])
        self.assertTrue(created)
        self.assertEqual('prepared', prepared['phase'])
        creating = incus_driver.transition_cold_attachment_rotation(
            instance, volume_id, prepared, 'creating')
        known = incus_driver.transition_cold_attachment_rotation(
            instance, volume_id, creating, 'new-created',
            new_attachment_id=new_attachment)
        self.assertRaises(
            exception.InvalidVolume,
            incus_driver.transition_cold_attachment_rotation,
            instance, volume_id, creating, 'new-created',
            new_attachment_id=new_attachment)
        old_absent = incus_driver.transition_cold_attachment_rotation(
            instance, volume_id, known, 'old-deleted')
        switched = incus_driver.transition_cold_attachment_rotation(
            instance, volume_id, old_absent, 'bdm-rotated')

        self.assertEqual(
            switched,
            incus_driver.get_cold_attachment_rotation(instance, volume_id))
        candidate = next(
            item for item in
            incus_driver.list_volume_journal_recovery_candidates()
            if item['uuid'] == instance.uuid)
        self.assertEqual([volume_id], candidate['volume_ids'])
        self.assertEqual(
            {volume_id: 'rotation-bdm-rotated'}, candidate['phases'])

        replacement = (
            incus_driver.replace_cold_source_volume_attach_intent(
                instance, volume_id, intent, new_attachment))
        self.assertEqual(new_attachment, replacement['attachment_id'])
        terminal = incus_driver.transition_cold_attachment_rotation(
            instance, volume_id, switched, 'source-rollback-complete')
        candidate = next(
            item for item in
            incus_driver.list_volume_journal_recovery_candidates()
            if item['uuid'] == instance.uuid)
        self.assertEqual(
            {volume_id: 'rotation-source-rollback-complete'},
            candidate['phases'])
        incus_driver.cancel_managed_volume_attach(
            instance, volume_id, replacement)
        candidate = next(
            item for item in
            incus_driver.list_volume_journal_recovery_candidates()
            if item['uuid'] == instance.uuid)
        self.assertEqual([volume_id], candidate['volume_ids'])
        incus_driver.cancel_cold_attachment_rotation(
            instance, volume_id, terminal)
        self.assertFalse(os.path.exists(
            driver._volume_journal_directory(instance)))

    @mock.patch.object(
        driver, '_mapped_rbd_device', return_value='/dev/sdc')
    @mock.patch('os.path.realpath', return_value='/dev/sdc')
    def test_periodic_attach_recovery_retains_journal_until_cinder_commit(
            self, realpath, mapped_device):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-periodic-attach-recovery', memory_mb=0)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        pending = driver._serialize_volume_attachment(
            connection_info, {}, '/dev/sdd', phase='connecting')
        profile = mock.Mock(
            devices={},
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver._volume_device_info_key(_TEST_VOLUME_ID): pending,
            })
        self.client.profiles.get.return_value = profile
        driver._write_volume_journal(
            instance, _TEST_VOLUME_ID, connection_info, {}, '/dev/sdd',
            phase='connecting')
        connector = mock.Mock()
        connector.connect_volume.return_value = {'path': '/dev/sdc'}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        with mock.patch.object(
                driver, 'brick_get_connector', return_value=connector):
            mountpoint = incus_driver.resume_connecting_volume_journal(
                ctx, instance, _TEST_VOLUME_ID, connection_info)
            second_mountpoint = incus_driver.resume_connecting_volume_journal(
                ctx, instance, _TEST_VOLUME_ID, connection_info)

        self.assertEqual('/dev/sdd', mountpoint)
        self.assertEqual('/dev/sdd', second_mountpoint)
        connector.connect_volume.assert_called_once_with(
            connection_info['data'])
        self.assertEqual(
            'connected',
            driver._read_volume_journal(
                instance, _TEST_VOLUME_ID)['phase'])
        self.assertEqual(
            'connected', jsonutils.loads(profile.config[
                driver._volume_device_info_key(_TEST_VOLUME_ID)])['phase'])

    @mock.patch.object(
        driver, '_mapped_rbd_device', return_value='/dev/sdc')
    def test_periodic_attach_confirmation_removes_only_matching_journal(
            self, mapped_device):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test-confirm-attach',
            memory_mb=0)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        record = driver._serialize_volume_attachment(
            connection_info, {'path': '/dev/sdc'}, '/dev/sdd',
            phase='connected')
        profile = mock.Mock(
            devices={
                _TEST_VOLUME_ID: {
                    'type': 'unix-block',
                    'path': '/dev/sdd',
                    'source': '/dev/sdc',
                    'required': 'true',
                },
            },
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver._volume_device_info_key(_TEST_VOLUME_ID): record,
            })
        self.client.profiles.get.return_value = profile
        driver._write_volume_journal(
            instance, _TEST_VOLUME_ID, connection_info,
            {'path': '/dev/sdc'}, '/dev/sdd', phase='connected')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.confirm_connected_volume_journal(
            instance, _TEST_VOLUME_ID, connection_info)

        self.assertIsNone(
            driver._read_volume_journal(instance, _TEST_VOLUME_ID))

    @mock.patch.object(
        driver, '_mapped_rbd_device', return_value='/dev/sdc')
    def test_periodic_attach_confirmation_rejects_bdm_target_mismatch(
            self, mapped_device):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test-confirm-target-mismatch',
            memory_mb=0)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        record = driver._serialize_volume_attachment(
            connection_info, {'path': '/dev/sdc'}, '/dev/sdd',
            phase='connected')
        profile = mock.Mock(
            devices={
                _TEST_VOLUME_ID: {
                    'type': 'unix-block',
                    'path': '/dev/sdd',
                    'source': '/dev/sdc',
                    'required': 'true',
                },
            },
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver._volume_device_info_key(_TEST_VOLUME_ID): record,
            })
        self.client.profiles.get.return_value = profile
        driver._write_volume_journal(
            instance, _TEST_VOLUME_ID, connection_info,
            {'path': '/dev/sdc'}, '/dev/sdd', phase='connected')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.InvalidVolume,
            incus_driver.confirm_connected_volume_journal,
            instance, _TEST_VOLUME_ID, connection_info,
            expected_mountpoint='/dev/vde')

        self.assertIsNotNone(
            driver._read_volume_journal(instance, _TEST_VOLUME_ID))

    @mock.patch.object(
        driver, '_mapped_rbd_device', return_value='/dev/sdc')
    def test_attach_intent_only_confirmation_still_validates_profile(
            self, mapped_device):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test-intent-only-confirm',
            memory_mb=0)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        profile = mock.Mock(
            devices={
                _TEST_VOLUME_ID: {
                    'type': 'unix-block',
                    'path': '/dev/sdd',
                    'source': '/dev/sdc',
                    'required': 'true',
                },
            },
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver._volume_device_info_key(_TEST_VOLUME_ID):
                    driver._serialize_volume_attachment(
                        connection_info, {'path': '/dev/sdc'}, '/dev/sdd',
                        phase='connected'),
            })
        self.client.profiles.get.return_value = profile
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.confirm_connected_volume_journal(
            instance, _TEST_VOLUME_ID, connection_info,
            expected_mountpoint='/dev/sdd')

        self.client.profiles.get.assert_called_once_with(instance.name)

    def test_internal_attach_pending_starts_exact_local_connection(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-internal-attach-pending', memory_mb=0)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        with mock.patch.object(
                incus_driver, '_attach_volume_locked') as attach:
            with mock.patch.object(
                    incus_driver,
                    'confirm_connected_volume_journal') as confirm:
                incus_driver.resume_internal_volume_attach(
                    ctx, instance, _TEST_VOLUME_ID, connection_info,
                    '/dev/sdd')

        attach.assert_called_once_with(
            ctx, connection_info, instance, '/dev/sdd',
            retain_journal=True)
        confirm.assert_called_once_with(
            instance, _TEST_VOLUME_ID, connection_info,
            expected_mountpoint='/dev/sdd')

    def test_internal_attach_connection_rehydrates_instance_identity(self):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test-journal-identity',
            memory_mb=0)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        driver._write_volume_journal(
            instance, _TEST_VOLUME_ID, connection_info,
            {'path': '/dev/sdc'}, '/dev/sdd', 'disconnected')
        self.client.profiles.get.return_value = mock.Mock(
            devices={}, config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
            })
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        recovered = incus_driver.get_internal_volume_attach_connection_info(
            instance, _TEST_VOLUME_ID, '/dev/sdd')

        self.assertEqual(instance.uuid, recovered['instance'])
        self.assertEqual(_TEST_VOLUME_ID, recovered['serial'])

    def test_immediate_internal_attach_republishes_intent_after_fsync_error(
            self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-internal-attach-fsync', memory_mb=0)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        attachment_id = '40000000-0000-0000-0000-000000000004'
        generation = '50000000-0000-0000-0000-000000000005'
        intent = {
            'attachment_id': attachment_id,
            'mountpoint': '/dev/sdd',
            'operation_kind': 'spawn',
            'operation_token': generation,
            'operation_direction': 'materialize',
            'operation_migration_uuid': None,
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        with mock.patch.object(
                incus_driver, 'prepare_managed_volume_attach',
                side_effect=[intent, intent]) as prepare:
            with mock.patch.object(incus_driver, '_attach_volume_locked'):
                with mock.patch.object(
                        incus_driver,
                        'confirm_connected_volume_journal'):
                    with mock.patch.object(
                            incus_driver, 'cancel_managed_volume_attach',
                            side_effect=OSError('fsync failed')):
                        result = incus_driver._attach_volume_for_operation(
                            ctx, connection_info, instance, '/dev/sdd',
                            attachment_id, 'spawn', generation,
                            'materialize', commit_immediately=True)

        self.assertEqual(intent, result)
        self.assertEqual(2, prepare.call_count)
        self.assertEqual(prepare.call_args_list[0], prepare.call_args_list[1])

    def test_internal_target_rollback_intent_only_has_no_side_effect(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-internal-rollback-pending', memory_mb=0)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        profile = mock.Mock(
            devices={},
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
            })
        self.client.profiles.get.return_value = profile
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        with mock.patch.object(
                incus_driver, '_detach_volume_locked') as detach:
            incus_driver.rollback_internal_volume_attach(
                ctx, instance, _TEST_VOLUME_ID, connection_info,
                '/dev/sdd')

        detach.assert_not_called()
        self.assertIsNone(
            driver._read_volume_journal(instance, _TEST_VOLUME_ID))

    @mock.patch.object(
        driver, '_profile_volume_attachment_matches', return_value=True)
    def test_internal_target_rollback_connected_without_journal(
            self, attachment_matches):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-internal-rollback-connected', memory_mb=0)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        profile = mock.Mock(
            devices={
                _TEST_VOLUME_ID: {
                    'type': 'unix-block', 'path': '/dev/sdd',
                    'source': '/dev/sdc'}},
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver._volume_device_info_key(_TEST_VOLUME_ID):
                    driver._serialize_volume_attachment(
                        connection_info, {'path': '/dev/sdc'}, '/dev/sdd'),
            })
        self.client.profiles.get.return_value = profile
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        def disconnect(*_args, **_kwargs):
            driver._write_volume_journal(
                instance, _TEST_VOLUME_ID, connection_info,
                {'path': '/dev/sdc'}, '/dev/sdd', phase='disconnected')

        with mock.patch.object(
                incus_driver, '_detach_volume_locked',
                side_effect=disconnect) as detach:
            incus_driver.rollback_internal_volume_attach(
                ctx, instance, _TEST_VOLUME_ID, connection_info,
                '/dev/sdd')

        detach.assert_called_once_with(
            ctx, connection_info, instance, '/dev/sdd',
            retain_journal=True)
        self.assertEqual(
            'rolled-back',
            driver._read_volume_journal(
                instance, _TEST_VOLUME_ID)['phase'])

    def test_internal_attach_restart_replaces_rolled_back_generation(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-internal-restart', memory_mb=0)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        driver._write_volume_journal(
            instance, _TEST_VOLUME_ID, connection_info,
            {'path': '/dev/sdc'}, '/dev/sdd', phase='rolled-back')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        with mock.patch.object(
                incus_driver, 'rollback_internal_volume_attach') as rollback:
            with mock.patch.object(
                    incus_driver,
                    'finalize_rolled_back_volume_journal') as finalize:
                with mock.patch.object(
                        incus_driver,
                        'resume_internal_volume_attach') as resume:
                    incus_driver.restart_internal_volume_attach(
                        ctx, instance, _TEST_VOLUME_ID, connection_info,
                        '/dev/sdd')

        rollback.assert_called_once_with(
            ctx, instance, _TEST_VOLUME_ID, connection_info,
            expected_mountpoint='/dev/sdd')
        finalize.assert_called_once_with(instance, _TEST_VOLUME_ID)
        resume.assert_called_once_with(
            ctx, instance, _TEST_VOLUME_ID, connection_info,
            expected_mountpoint='/dev/sdd')

    def test_internal_migration_disposition_uses_exact_attempt(self):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(),
            name='test-internal-migration-disposition', memory_mb=0)
        token = '10000000-0000-0000-0000-000000000001'
        migration_uuid = '20000000-0000-0000-0000-000000000002'
        profile = mock.Mock(
            devices={}, used_by=[],
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
                driver.MIGRATION_DESTINATION_PREPARED_KEY: token,
                driver.MIGRATION_NOVA_UUID_KEY: migration_uuid,
                'security.idmap.base': '1065536',
                'security.idmap.size': '65536',
            })
        self.client.profiles.get.return_value = profile
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        intent = {
            'operation_kind': 'migration',
            'operation_direction': 'live-target',
            'operation_token': token,
            'operation_migration_uuid': migration_uuid,
        }

        with mock.patch.object(
                driver, '_get_migration_attempt', return_value={
                    'state': 'committed', 'finished': True}) as get_attempt:
            result = incus_driver.internal_migration_attach_disposition(
                instance, intent)

        self.assertEqual('committed', result)
        get_attempt.assert_called_once_with(
            self.client, instance, token, 1065536, 65536)
        get_attempt.reset_mock()
        intent['operation_migration_uuid'] = (
            '30000000-0000-0000-0000-000000000003')

        self.assertRaises(
            exception.InvalidVolume,
            incus_driver.internal_migration_attach_disposition,
            instance, intent)
        get_attempt.assert_not_called()

    def test_boot_volume_marker_rejected_outside_source_migration(self):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test-invalid-boot-intent',
            memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        attachment_id = '40000000-0000-0000-0000-000000000004'
        token = '50000000-0000-0000-0000-000000000005'

        operations = (
            ('hot-attach', None, None, None),
            ('spawn', token, 'materialize', None),
            ('reconcile', attachment_id, 'power-reconcile', None),
        )
        for kind, operation_token, direction, migration_uuid in operations:
            self.assertRaises(
                exception.InvalidVolume,
                incus_driver.prepare_managed_volume_attach,
                instance, _TEST_VOLUME_ID, attachment_id, '/dev/sda',
                operation_kind=kind, operation_token=operation_token,
                operation_direction=direction,
                operation_migration_uuid=migration_uuid,
                boot_volume=True)

        self.assertFalse(os.path.exists(
            driver._managed_attach_intent_path(instance, _TEST_VOLUME_ID)))

    def test_periodic_attach_resume_rejects_bdm_target_mismatch(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-resume-target-mismatch', memory_mb=0)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        driver._write_volume_journal(
            instance, _TEST_VOLUME_ID, connection_info, {}, '/dev/sdd',
            phase='connecting')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        with mock.patch.object(
                incus_driver, '_attach_volume_locked') as attach:
            self.assertRaises(
                exception.InvalidVolume,
                incus_driver.resume_connecting_volume_journal,
                ctx, instance, _TEST_VOLUME_ID, connection_info,
                expected_mountpoint='/dev/vde')

        attach.assert_not_called()

    def test_periodic_attach_rollback_rejects_bdm_target_mismatch(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-rollback-target-mismatch', memory_mb=0)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        driver._write_volume_journal(
            instance, _TEST_VOLUME_ID, connection_info, {}, '/dev/sdd',
            phase='connecting')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        with mock.patch.object(driver, 'brick_get_connector') as connector:
            self.assertRaises(
                exception.InvalidVolume,
                incus_driver.rollback_connecting_volume_journal,
                ctx, instance, _TEST_VOLUME_ID, connection_info,
                expected_mountpoint='/dev/vde')

        connector.assert_not_called()

    def test_failed_attach_disconnected_rejects_intent_target_mismatch(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-disconnected-target-mismatch', memory_mb=0)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        driver._write_volume_journal(
            instance, _TEST_VOLUME_ID, connection_info,
            {'path': '/dev/sdc'}, '/dev/sdd', phase='disconnected')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        with mock.patch.object(driver, 'brick_get_connector') as connector:
            self.assertRaises(
                exception.InvalidVolume,
                incus_driver.rollback_connecting_volume_journal,
                ctx, instance, _TEST_VOLUME_ID, connection_info,
                expected_mountpoint='/dev/vde')

        connector.assert_not_called()
        self.assertEqual(
            'disconnected',
            driver._read_volume_journal(instance, _TEST_VOLUME_ID)['phase'])

    @mock.patch('os.path.realpath', return_value='/dev/sdc')
    def test_failed_attach_disconnect_recovery_becomes_rolled_back(
            self, realpath):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-failed-attach-disconnect', memory_mb=0)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        record = driver._serialize_volume_attachment(
            connection_info, {'path': '/dev/sdc'}, '/dev/sdd',
            phase='disconnecting')
        profile = mock.Mock(
            devices={
                _TEST_VOLUME_ID: {
                    'type': 'unix-block', 'path': '/dev/sdd',
                    'source': '/dev/sdc'},
            },
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver._volume_device_info_key(_TEST_VOLUME_ID): record,
            })
        self.client.profiles.get.return_value = profile
        driver._write_volume_journal(
            instance, _TEST_VOLUME_ID, connection_info,
            {'path': '/dev/sdc'}, '/dev/sdd', phase='disconnecting')
        connector = mock.Mock()
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        with mock.patch.object(
                driver, 'brick_get_connector', return_value=connector):
            incus_driver.rollback_connecting_volume_journal(
                ctx, instance, _TEST_VOLUME_ID, connection_info,
                expected_mountpoint='/dev/sdd')

        profile.save.assert_called_once_with(wait=True)
        connector.disconnect_volume.assert_called_once_with(
            connection_info['data'], {'path': '/dev/sdc'})
        self.assertEqual(
            'rolled-back',
            driver._read_volume_journal(instance, _TEST_VOLUME_ID)['phase'])

    def test_public_volume_operations_lock_instance_then_volume(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-volume-lock-order', memory_mb=0)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        incus_driver = driver.IncusDriver(None)
        events = []

        @contextmanager
        def tracked_lock(name, **kwargs):
            events.append(('enter', name))
            try:
                yield
            finally:
                events.append(('exit', name))

        topology_lock = driver._volume_topology_lock_name(instance)
        volume_lock = driver._volume_operation_lock_name(_TEST_VOLUME_ID)
        with mock.patch.object(
                driver.lockutils, 'lock', side_effect=tracked_lock), \
                mock.patch.object(
                    incus_driver, '_attach_volume_locked') as attach, \
                mock.patch.object(
                    incus_driver, '_detach_volume_locked') as detach:
            incus_driver.attach_volume(
                ctx, connection_info, instance, '/dev/sdd')
            self.assertEqual([
                ('enter', topology_lock), ('enter', volume_lock),
                ('exit', volume_lock), ('exit', topology_lock),
            ], events)
            events.clear()
            incus_driver.detach_volume(
                ctx, connection_info, instance, '/dev/sdd')

        self.assertEqual([
            ('enter', topology_lock), ('enter', volume_lock),
            ('exit', volume_lock), ('exit', topology_lock),
        ], events)
        attach.assert_called_once()
        detach.assert_called_once()

    @mock.patch('os.path.realpath', return_value='/dev/sdc')
    def test_periodic_attach_rollback_retains_evidence_until_external_cleanup(
            self, realpath):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-rollback-attach', memory_mb=0)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        pending = driver._serialize_volume_attachment(
            connection_info, {}, '/dev/sdd', phase='connecting')
        profile = mock.Mock(
            devices={},
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver._volume_device_info_key(_TEST_VOLUME_ID): pending,
            })
        self.client.profiles.get.return_value = profile
        driver._write_volume_journal(
            instance, _TEST_VOLUME_ID, connection_info, {}, '/dev/sdd',
            phase='connecting')
        connector = mock.Mock()
        connector.connect_volume.return_value = {'path': '/dev/sdc'}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        with mock.patch.object(
                driver, 'brick_get_connector', return_value=connector):
            incus_driver.rollback_connecting_volume_journal(
                ctx, instance, _TEST_VOLUME_ID, connection_info)
            # Model a crash after Cinder deletes the old attachment and a
            # later same-host owner reuses the volume. The stale journal is
            # replayed, but rolled-back must never touch its host mapping.
            incus_driver.rollback_connecting_volume_journal(
                ctx, instance, _TEST_VOLUME_ID, connection_info)

        connector.connect_volume.assert_called_once_with(
            connection_info['data'])
        connector.disconnect_volume.assert_called_once_with(
            connection_info['data'], {'path': '/dev/sdc'})
        self.assertEqual(
            'rolled-back',
            driver._read_volume_journal(
                instance, _TEST_VOLUME_ID)['phase'])
        self.assertNotIn(
            driver._volume_device_info_key(_TEST_VOLUME_ID), profile.config)

        incus_driver.finalize_rolled_back_volume_journal(
            instance, _TEST_VOLUME_ID)
        self.assertIsNone(
            driver._read_volume_journal(instance, _TEST_VOLUME_ID))

    def test_power_on_cleans_proven_stale_volume_journal(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-stale-volume-journal', memory_mb=0)
        volume_id = 'stale-volume'
        connection_info = {
            'driver_volume_type': 'rbd',
            'serial': volume_id,
            'data': {'name': 'volumes/volume-stale'},
        }
        driver._write_volume_journal(
            instance, volume_id, connection_info, {'path': '/dev/rbd9'},
            '/dev/vdb', phase='connected')
        profile = self.client.profiles.get.return_value
        profile.devices = {}
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        }
        container = self.client.instances.get.return_value
        container.status = 'Stopped'

        def disconnect(*_args, **_kwargs):
            driver._remove_volume_journal(instance, volume_id)

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._disconnect_profile_volume_connection = mock.Mock(
            side_effect=disconnect)

        incus_driver.power_on(
            ctx, instance, None, {'block_device_mapping': []})

        disconnect_mock = incus_driver._disconnect_profile_volume_connection
        disconnect_mock.assert_called_once_with(ctx, instance, volume_id)
        self.assertIsNone(driver._read_volume_journal(instance, volume_id))
        container.start.assert_called_once_with(wait=True)

    def test_power_on_rejects_opaque_unix_block_without_cleanup(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-opaque-data-topology', memory_mb=0)
        profile = self.client.profiles.get.return_value
        profile.devices = {
            'foreign': {
                'type': 'unix-block', 'path': '/dev/vdb',
                'source': '/dev/mapper/foreign'},
        }
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        }
        container = self.client.instances.get.return_value
        container.status = 'Stopped'
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._disconnect_profile_volume_connection = mock.Mock()

        self.assertRaisesRegex(
            exception.InvalidVolume, 'opaque unix-block',
            incus_driver.power_on, ctx, instance, None,
            {'block_device_mapping': []})

        incus_driver._disconnect_profile_volume_connection.assert_not_called()
        container.start.assert_not_called()

    def test_power_on_rejects_running_data_volume_mismatch_no_cleanup(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-running-data-topology', memory_mb=0)
        volume_id = 'stale-volume'
        connection_info = {
            'driver_volume_type': 'rbd',
            'serial': volume_id,
            'data': {'name': 'volumes/volume-stale'},
        }
        profile = self.client.profiles.get.return_value
        profile.devices = {
            volume_id: {
                'type': 'unix-block', 'path': '/dev/vdb',
                'source': '/dev/rbd9'},
        }
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver._volume_device_info_key(volume_id):
                driver._serialize_volume_attachment(
                    connection_info, {'path': '/dev/rbd9'}, '/dev/vdb'),
        }
        container = self.client.instances.get.return_value
        container.status = 'Running'
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._disconnect_profile_volume_connection = mock.Mock()

        self.assertRaisesRegex(
            exception.InvalidVolume, 'topology differs',
            incus_driver.power_on, ctx, instance, None,
            {'block_device_mapping': []})

        incus_driver._disconnect_profile_volume_connection.assert_not_called()
        container.start.assert_not_called()

    def test_power_on_rejects_running_rbd_namespace_mismatch(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-running-namespace-mismatch', memory_mb=0)
        requested = {
            'driver_volume_type': 'rbd',
            'serial': _TEST_VOLUME_ID,
            'data': {
                'name': 'volumes/volume-%s' % _TEST_VOLUME_ID,
                'rbd_namespace': 'tenant-a',
            },
        }
        recorded = copy.deepcopy(requested)
        recorded['data']['rbd_namespace'] = 'tenant-b'
        bdm = real_volume_driver_bdm(
            ctx, _TEST_VOLUME_ID, '/dev/vdb', None, requested)
        profile = self.client.profiles.get.return_value
        profile.devices = {
            _TEST_VOLUME_ID: {
                'type': 'unix-block', 'path': '/dev/vdb',
                'required': 'true', 'source': '/dev/rbd0'},
        }
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver._volume_device_info_key(_TEST_VOLUME_ID):
                driver._serialize_volume_attachment(
                    recorded, {'path': '/dev/rbd0'}, '/dev/vdb'),
        }
        container = self.client.instances.get.return_value
        container.status = 'Running'
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._disconnect_profile_volume_connection = mock.Mock()

        self.assertRaisesRegex(
            exception.InvalidVolume, 'Stored RBD namespace',
            incus_driver.power_on, ctx, instance, None,
            {'block_device_mapping': [bdm]})

        incus_driver._disconnect_profile_volume_connection.assert_not_called()
        container.start.assert_not_called()

    def test_power_on_without_bdm_rejects_retained_volume_state(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-missing-bdm-topology', memory_mb=0)
        volume_id = 'retained-volume'
        connection_info = {
            'driver_volume_type': 'rbd',
            'serial': volume_id,
            'data': {'name': 'volumes/volume-retained'},
        }
        profile = self.client.profiles.get.return_value
        profile.devices = {
            volume_id: {
                'type': 'unix-block', 'path': '/dev/vdb',
                'source': '/dev/rbd9'},
        }
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver._volume_device_info_key(volume_id):
                driver._serialize_volume_attachment(
                    connection_info, {'path': '/dev/rbd9'}, '/dev/vdb'),
        }
        container = self.client.instances.get.return_value
        container.status = 'Stopped'
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._disconnect_profile_volume_connection = mock.Mock()

        self.assertRaisesRegex(
            exception.InvalidVolume, 'did not provide block-device mappings',
            incus_driver.power_on, ctx, instance, None)

        incus_driver._disconnect_profile_volume_connection.assert_not_called()
        container.start.assert_not_called()

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
                'name': driver.incus_vif.get_vif_guest_devname(_VIF),
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
                'name': driver.incus_vif.get_vif_guest_devname(_VIF),
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
        incus_driver._get_host_resource_snapshot = mock.Mock(return_value={
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

        incus_driver._get_host_resource_snapshot.assert_called_once_with(
            'compute-1', use_cache=True)
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
        incus_driver._get_host_resource_snapshot = mock.Mock(return_value={
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
        incus_driver.client = self.client
        incus_driver._get_host_resource_snapshot = mock.Mock(return_value={
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
        self.client.storage_pools.get.return_value.status = 'Created'
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
        incus_driver._get_host_resource_snapshot = mock.Mock(return_value={
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
        self.client.storage_pools.get.return_value.status = 'Created'
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
        get_pool_info.assert_called_once_with(
            self.client, 'local-zfs',
            pool=self.client.storage_pools.get.return_value)

    @mock.patch('nova.virt.incus.driver._get_storage_pool_info')
    @mock.patch('nova.virt.incus.driver._host_has_swap', return_value=False)
    def test_unreportable_pool_capacity_degrades_instead_of_freezing(
            self, _host_has_swap, get_pool_info):
        """A pool reporting nothing must not freeze the whole inventory.

        Raising here would leave every resource class stuck at its last
        value while the service kept reporting up - the failure mode this
        method exists to avoid. The selector's own inventory is preserved
        and its trait suppressed, exactly like an unreachable pool.
        """
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client
        incus_driver._get_host_resource_snapshot = mock.Mock(return_value={
            'vcpus': 8, 'memory_mb': 16384, 'local_gb': 100})
        incus_driver._get_allocation_ratios = mock.Mock(return_value={
            'VCPU': 4.0, 'MEMORY_MB': 1.5, 'DISK_GB': 1.0})
        incus_driver._get_reserved_host_disk_gb_from_config = mock.Mock(
            return_value=2)
        get_pool_info.return_value = {'total': 0, 'used': 0}
        previous = {
            'total': 80, 'min_unit': 1, 'max_unit': 80, 'step_size': 1,
            'allocation_ratio': 1.0, 'reserved': 0,
        }
        provider_tree = mock.Mock()
        provider_tree.data.return_value = mock.Mock(
            inventory={
                'CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME_DISK_GB': previous},
            traits=set())
        self.CONF.incus.root_storage_pools = {'local-nvme': 'local-zfs'}
        self.client.storage_pools.get.return_value.status = 'Created'
        self.CONF.incus.root_storage_pool_resource_classes = {
            'local-nvme': 'CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME_DISK_GB',
        }
        self.CONF.reserved_host_cpus = 0
        self.CONF.reserved_host_memory_mb = 0

        incus_driver.update_provider_tree(provider_tree, 'compute-1')

        inventory = provider_tree.update_inventory.call_args.args[1]
        quiesced = inventory['CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME_DISK_GB']
        # Existing allocations keep their capacity; new ones are blocked
        # by reserving all of it.
        self.assertEqual(previous['total'], quiesced['total'])
        self.assertEqual(previous['total'], quiesced['reserved'])
        # The live resource classes are still refreshed this cycle.
        self.assertIn('VCPU', inventory)
        traits = provider_tree.update_traits.call_args.args[1]
        self.assertNotIn('CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME', traits)

    @mock.patch('nova.virt.incus.driver._host_has_swap', return_value=False)
    def test_update_provider_tree_slices_shared_pool_capacity(
            self, _host_has_swap):
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client
        incus_driver._get_host_resource_snapshot = mock.Mock(return_value={
            'vcpus': 8, 'memory_mb': 16384, 'local_gb': 100})
        incus_driver._get_allocation_ratios = mock.Mock(return_value={
            'VCPU': 4.0, 'MEMORY_MB': 1.5, 'DISK_GB': 1.0})
        incus_driver._get_reserved_host_disk_gb_from_config = mock.Mock(
            return_value=2)
        provider_tree = mock.Mock()
        provider_tree.data.return_value = mock.Mock(
            inventory={}, traits=set())
        pool = self.client.storage_pools.get.return_value
        pool.driver = 'ceph'
        pool.status = 'Created'
        self.CONF.incus.root_storage_pools = {
            'durable': 'ceph-rootfs',
        }
        self.CONF.incus.root_storage_pool_resource_classes = {
            'durable': 'CUSTOM_INCUS_STORAGE_POOL_DURABLE_DISK_GB',
        }
        self.CONF.incus.shared_root_storage_pool_capacities_gb = {
            'durable': '40',
        }
        self.CONF.reserved_host_cpus = 0
        self.CONF.reserved_host_memory_mb = 0

        incus_driver.update_provider_tree(provider_tree, 'compute-1')

        inventory = provider_tree.update_inventory.call_args.args[1]
        self.assertEqual(
            40,
            inventory['CUSTOM_INCUS_STORAGE_POOL_DURABLE_DISK_GB']['total'])
        pool.resources.get.assert_not_called()

    @mock.patch('nova.virt.incus.driver._host_has_swap', return_value=False)
    def test_update_provider_tree_rejects_unsliced_shared_pool(
            self, _host_has_swap):
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client
        incus_driver._get_host_resource_snapshot = mock.Mock(return_value={
            'vcpus': 8, 'memory_mb': 16384, 'local_gb': 100})
        incus_driver._get_allocation_ratios = mock.Mock(return_value={
            'VCPU': 4.0, 'MEMORY_MB': 1.5, 'DISK_GB': 1.0})
        incus_driver._get_reserved_host_disk_gb_from_config = mock.Mock(
            return_value=2)
        provider_tree = mock.Mock()
        provider_tree.data.return_value = mock.Mock(
            inventory={}, traits=set())
        self.client.storage_pools.get.return_value.driver = 'ceph'
        self.client.storage_pools.get.return_value.status = 'Created'
        self.CONF.incus.root_storage_pools = {
            'durable': 'ceph-rootfs',
        }
        self.CONF.incus.root_storage_pool_resource_classes = {
            'durable': 'CUSTOM_INCUS_STORAGE_POOL_DURABLE_DISK_GB',
        }
        self.CONF.reserved_host_cpus = 0
        self.CONF.reserved_host_memory_mb = 0

        self.assertRaises(
            exception.InvalidConfiguration,
            incus_driver.update_provider_tree,
            provider_tree,
            'compute-1')

    @mock.patch('nova.virt.incus.driver._host_has_swap', return_value=False)
    def test_update_provider_tree_suppresses_unavailable_root_pool(
            self, _host_has_swap):
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client
        incus_driver._get_host_resource_snapshot = mock.Mock(return_value={
            'vcpus': 8, 'memory_mb': 16384, 'local_gb': 100})
        incus_driver._get_allocation_ratios = mock.Mock(return_value={
            'VCPU': 4.0, 'MEMORY_MB': 1.5, 'DISK_GB': 1.0})
        incus_driver._get_reserved_host_disk_gb_from_config = mock.Mock(
            return_value=2)
        stale_inventory = {
            'total': 80,
            'min_unit': 1,
            'max_unit': 80,
            'step_size': 1,
            'allocation_ratio': 1.0,
            'reserved': 0,
        }
        current = mock.Mock(
            inventory={
                'CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME_DISK_GB':
                    stale_inventory,
            },
            traits={
                'CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME',
                'CUSTOM_OPERATOR_MANAGED',
            })
        provider_tree = mock.Mock()
        provider_tree.data.return_value = current
        pool = self.client.storage_pools.get.return_value
        pool.status = 'Unavailable'
        self.CONF.incus.root_storage_pools = {
            'local-nvme': 'local-zfs',
        }
        self.CONF.incus.root_storage_pool_resource_classes = {
            'local-nvme': 'CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME_DISK_GB',
        }
        self.CONF.reserved_host_cpus = 0
        self.CONF.reserved_host_memory_mb = 0

        with mock.patch.object(driver.LOG, 'error') as log_error:
            incus_driver.update_provider_tree(provider_tree, 'compute-1')

        inventory = provider_tree.update_inventory.call_args.args[1]
        self.assertEqual(
            dict(stale_inventory, reserved=80),
            inventory['CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME_DISK_GB'])
        self.assertIsNot(
            stale_inventory,
            inventory['CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME_DISK_GB'])
        provider_tree.update_traits.assert_called_once_with(
            'compute-1', {
                'CUSTOM_INCUS_SYSTEM_CONTAINER',
                'CUSTOM_OPERATOR_MANAGED',
            })
        log_error.assert_called_once()
        self.assertIn(
            'state %(status)s', log_error.call_args.args[0])
        pool.resources.get.assert_not_called()

    @mock.patch('nova.virt.incus.driver._host_has_swap', return_value=False)
    def test_update_provider_tree_suppresses_pool_capacity_error(
            self, _host_has_swap):
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client
        incus_driver._get_host_resource_snapshot = mock.Mock(return_value={
            'vcpus': 8, 'memory_mb': 16384, 'local_gb': 100})
        incus_driver._get_allocation_ratios = mock.Mock(return_value={
            'VCPU': 4.0, 'MEMORY_MB': 1.5, 'DISK_GB': 1.0})
        incus_driver._get_reserved_host_disk_gb_from_config = mock.Mock(
            return_value=2)
        stale_inventory = {
            'total': 80,
            'min_unit': 1,
            'max_unit': 80,
            'step_size': 1,
            'allocation_ratio': 1.0,
            'reserved': 0,
        }
        current = mock.Mock(
            inventory={
                'CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME_DISK_GB':
                    stale_inventory,
            },
            traits={'CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME'})
        provider_tree = mock.Mock()
        provider_tree.data.return_value = current
        pool = self.client.storage_pools.get.return_value
        pool.status = 'Created'
        pool.driver = 'zfs'
        pool.resources.get.side_effect = incus_api_exception(
            500, 'cannot open zpool')
        self.CONF.incus.root_storage_pools = {
            'local-nvme': 'local-zfs',
        }
        self.CONF.incus.root_storage_pool_resource_classes = {
            'local-nvme': 'CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME_DISK_GB',
        }
        self.CONF.reserved_host_cpus = 0
        self.CONF.reserved_host_memory_mb = 0

        with mock.patch.object(driver.LOG, 'error') as log_error:
            incus_driver.update_provider_tree(provider_tree, 'compute-1')

        inventory = provider_tree.update_inventory.call_args.args[1]
        self.assertEqual(
            dict(stale_inventory, reserved=80),
            inventory['CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME_DISK_GB'])
        provider_tree.update_traits.assert_called_once_with(
            'compute-1', {'CUSTOM_INCUS_SYSTEM_CONTAINER'})
        log_error.assert_called_once()
        self.assertIn(
            'cannot report capacity', log_error.call_args.args[0])

    @mock.patch('nova.virt.incus.driver._host_has_swap', return_value=False)
    def test_update_provider_tree_quiesces_unavailable_default_pool(
            self, _host_has_swap):
        incus_driver = driver.IncusDriver(None)
        incus_driver._get_host_resource_snapshot = mock.Mock(return_value={
            'vcpus': 8,
            'memory_mb': 16384,
            'local_gb': 0,
            'default_storage_pool_available': False,
        })
        incus_driver._get_allocation_ratios = mock.Mock(return_value={
            'VCPU': 4.0, 'MEMORY_MB': 1.5, 'DISK_GB': 1.0})
        incus_driver._get_reserved_host_disk_gb_from_config = mock.Mock(
            return_value=2)
        current = mock.Mock(
            inventory={
                'DISK_GB': {
                    'total': 100,
                    'min_unit': 1,
                    'max_unit': 100,
                    'step_size': 1,
                    'allocation_ratio': 1.0,
                    'reserved': 2,
                },
            },
            traits={
                'CUSTOM_INCUS_SYSTEM_CONTAINER',
                'CUSTOM_OPERATOR_MANAGED',
            })
        provider_tree = mock.Mock()
        provider_tree.data.return_value = current
        self.CONF.reserved_host_cpus = 0
        self.CONF.reserved_host_memory_mb = 0

        incus_driver.update_provider_tree(provider_tree, 'compute-1')

        inventory = provider_tree.update_inventory.call_args.args[1]
        self.assertEqual(100, inventory['DISK_GB']['reserved'])
        provider_tree.update_traits.assert_called_once_with(
            'compute-1', {'CUSTOM_OPERATOR_MANAGED'})

    @mock.patch('nova.virt.incus.driver._host_has_swap', return_value=False)
    def test_update_provider_tree_reports_manila_migration_traits(
            self, _host_has_swap):
        incus_driver = driver.IncusDriver(None)
        incus_driver._get_host_resource_snapshot = mock.Mock(return_value={
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
        self.CONF.incus.allow_cold_migration = True

        incus_driver.update_provider_tree(provider_tree, 'compute-1')

        provider_tree.update_traits.assert_called_once_with(
            'compute-1', {
                'CUSTOM_INCUS_MANILA_COLD_MIGRATION',
                'CUSTOM_INCUS_MANILA_LIVE_MIGRATION',
                'CUSTOM_INCUS_MANILA_SHARE',
                'CUSTOM_INCUS_SYSTEM_CONTAINER',
            })

    def test_attach_interface(self):
        expected = {
            'hwaddr': '00:11:22:33:44:55',
            'name': 'nic0123456789ab',
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

    def test_attach_interface_rolls_back_vif_after_incus_failure(self):
        container = mock.Mock(devices={})
        container.save.side_effect = [RuntimeError('save failed'), None]
        self.client.instances.get.return_value = container
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        vif = {
            'id': '0123456789abcdef',
            'type': network_model.VIF_TYPE_OVS,
            'address': '00:11:22:33:44:55',
            'network': {'bridge': 'fakebr'},
            'devname': 'tap0123456789a',
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.firewall_driver = mock.Mock()

        self.assertRaises(
            RuntimeError,
            incus_driver.attach_interface,
            context.get_admin_context(),
            instance,
            None,
            vif)

        self.assertNotIn('tap0123456789a', container.devices)
        incus_driver.firewall_driver.unfilter_instance.assert_called_once_with(
            instance, [vif])
        self.vif_driver.unplug.assert_called_once_with(instance, vif)

    def test_attach_interface_rolls_back_vif_after_filter_failure(self):
        container = mock.Mock(devices={})
        self.client.instances.get.return_value = container
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        vif = {
            'id': '0123456789abcdef',
            'type': network_model.VIF_TYPE_OVS,
            'address': '00:11:22:33:44:55',
            'network': {'bridge': 'fakebr'},
            'devname': 'tap0123456789a',
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.firewall_driver = mock.Mock()
        incus_driver.firewall_driver.setup_basic_filtering.side_effect = (
            RuntimeError('filter failed'))

        self.assertRaises(
            RuntimeError,
            incus_driver.attach_interface,
            context.get_admin_context(),
            instance,
            None,
            vif)

        container.save.assert_not_called()
        incus_driver.firewall_driver.unfilter_instance.assert_called_once_with(
            instance, [vif])
        self.vif_driver.unplug.assert_called_once_with(instance, vif)

    def test_attach_interface_preserves_original_error_on_cleanup_failure(
            self):
        container = mock.Mock(devices={})
        self.client.instances.get.return_value = container
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        vif = {
            'id': '0123456789abcdef',
            'type': network_model.VIF_TYPE_OVS,
            'address': '00:11:22:33:44:55',
            'network': {'bridge': 'fakebr'},
            'devname': 'tap0123456789a',
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.firewall_driver = mock.Mock()
        incus_driver.firewall_driver.setup_basic_filtering.side_effect = (
            RuntimeError('filter failed'))
        incus_driver.firewall_driver.unfilter_instance.side_effect = (
            RuntimeError('unfilter failed'))
        self.vif_driver.unplug.side_effect = RuntimeError('unplug failed')

        raised = self.assertRaises(
            RuntimeError,
            incus_driver.attach_interface,
            context.get_admin_context(),
            instance,
            None,
            vif)

        self.assertEqual('filter failed', str(raised))
        self.vif_driver.unplug.assert_called_once_with(instance, vif)

    def test_attach_interface_retains_vif_if_incus_still_references_it(self):
        initial = mock.Mock(devices={})
        initial.save.side_effect = RuntimeError('save failed')
        device = {
            'hwaddr': '00:11:22:33:44:55',
            'parent': 'tin0123456789a',
            'nictype': 'physical',
            'type': 'nic',
        }
        persisted = mock.Mock(devices={'tap0123456789a': device})
        persisted.save.side_effect = RuntimeError('instance busy')
        still_persisted = mock.Mock(
            devices={'tap0123456789a': device})
        self.client.instances.get.side_effect = [
            initial, persisted, still_persisted]
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        vif = {
            'id': '0123456789abcdef',
            'type': network_model.VIF_TYPE_OVS,
            'address': '00:11:22:33:44:55',
            'network': {'bridge': 'fakebr'},
            'devname': 'tap0123456789a',
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.firewall_driver = mock.Mock()

        self.assertRaises(
            RuntimeError,
            incus_driver.attach_interface,
            context.get_admin_context(),
            instance,
            None,
            vif)

        incus_driver.firewall_driver.unfilter_instance.assert_not_called()
        self.vif_driver.unplug.assert_not_called()

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

    def test_detach_interface_restores_local_state_if_profile_save_fails(self):
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
        profile.save.side_effect = RuntimeError('profile save failed')
        saved_profile = mock.Mock(
            devices={'tap0123456789a': dict(device)})
        self.client.profiles.get.side_effect = [profile, saved_profile]
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        vif = {
            'id': '0123456789abcdef',
            'type': network_model.VIF_TYPE_OVS,
            'address': '00:11:22:33:44:55',
            'network': {'bridge': 'fakebr'},
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            RuntimeError,
            incus_driver.detach_interface,
            context.get_admin_context(),
            instance,
            vif)

        self.assertEqual(
            {'tap0123456789a': device}, container.devices)
        self.assertEqual(2, container.save.call_count)
        self.vif_driver.unplug.assert_not_called()

    def test_detach_interface_restores_different_local_device_on_failure(self):
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
        profile.devices = {'eth0': dict(device)}
        profile.save.side_effect = RuntimeError('profile save failed')
        saved_profile = mock.Mock(devices={'eth0': dict(device)})
        self.client.profiles.get.side_effect = [profile, saved_profile]
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        vif = {
            'id': '0123456789abcdef',
            'type': network_model.VIF_TYPE_OVS,
            'address': '00:11:22:33:44:55',
            'network': {'bridge': 'fakebr'},
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            RuntimeError,
            incus_driver.detach_interface,
            context.get_admin_context(),
            instance,
            vif)

        self.assertEqual(
            {'tap0123456789a': device}, container.devices)
        self.assertEqual(4, container.save.call_count)
        self.vif_driver.unplug.assert_not_called()

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

    def test_migrate_disk_and_power_off_rejects_same_host_first(self):
        self.CONF.incus.allow_cold_migration = True
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        raised = self.assertRaises(
            exception.InstanceFaultRollback,
            incus_driver.migrate_disk_and_power_off,
            ctx, instance, incus_driver.host, instance.flavor, [])

        self.assertIsInstance(
            raised.inner_exception, exception.UnableToMigrateToSelf)
        self.client.instances.get.assert_not_called()
        self.client.profiles.get.assert_not_called()

    @mock.patch.object(driver, '_migration_client')
    def test_migrate_disk_and_power_off_different_host(
            self, migration_client):
        container = mock.Mock()
        container.status = 'Running'
        container.config = {'volatile.idmap.base': '1065536'}
        container.generate_migration_data.return_value = {
            'name': 'test',
            'source': {
                'type': 'migration',
                'operation': (
                    'http+unix://incus/1.0/operations/'
                    '20000000-0000-0000-0000-000000000002'),
                'secrets': {'0': 'secret'},
            },
        }
        self.client.instances.get.return_value = container
        migration_client.return_value = self.client
        profile = mock.Mock()
        profile.config = {'security.idmap.size': '65536'}
        profile.devices = {'root': {'type': 'disk', 'path': '/'}}
        self.client.profiles.get.return_value = profile
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

        self.cold_migration_cleanup_token.assert_called_once_with(
            ctx, instance)
        self.assertEqual(
            '20000000-0000-0000-0000-000000000002',
            result['cleanup_token'])
        self.assertEqual('incus-pull-v1', result['format'])
        self.assertFalse(result['boot_from_volume'])
        self.assertTrue(result['was_running'])
        self.assertEqual(
            'https://10.224.0.16:8443/1.0/operations/'
            '20000000-0000-0000-0000-000000000002',
            result['migration_data']['source']['operation'])
        self.assertEqual(
            [instance.name], result['migration_data']['profiles'])
        container.stop.assert_called_once_with(wait=True)
        container.generate_migration_data.assert_called_once_with(live=False)

    @mock.patch.object(driver, '_migration_client')
    def test_cold_migration_rejects_missing_manila_device_before_side_effects(
            self, migration_client):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        share_id = '10000000-0000-0000-0000-000000000001'
        self.share_mappings.return_value = [mock.Mock(
            share_id=share_id,
            instance_uuid=instance.uuid,
            tag='project-data',
            status=driver.obj_fields.ShareMappingStatus.ACTIVE)]
        container = mock.Mock(
            status='Running',
            config={'volatile.idmap.base': '1065536'},
            expanded_devices={
                'root': {'type': 'disk', 'path': '/', 'pool': 'local'}},
        )
        profile = mock.Mock(
            config={'security.idmap.size': '65536'},
            devices={'root': {'type': 'disk', 'path': '/'}},
        )
        self.client.instances.get.return_value = container
        self.client.profiles.get.return_value = profile
        self.client.storage_pools.get.return_value.driver = 'zfs'
        self.CONF.incus.allow_cold_migration = True
        self.CONF.incus.migration_address = 'https://10.224.0.16:8443'
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        raised = self.assertRaises(
            exception.InstanceFaultRollback,
            incus_driver.migrate_disk_and_power_off,
            ctx, instance, '10.224.0.17', instance.flavor, [])

        self.assertIsInstance(
            raised.inner_exception, exception.MigrationPreCheckError)
        self.assertIn(
            'do not match Nova share mappings', str(raised.inner_exception))
        profile.save.assert_not_called()
        container.stop.assert_not_called()
        container.generate_migration_data.assert_not_called()
        self.ensure_instance_idmap.assert_not_called()
        migration_client.assert_not_called()

    def _assert_cold_migration_rejects_invalid_manila_mapping(
            self, share_id='10000000-0000-0000-0000-000000000001',
            tag='project-data'):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        self.share_mappings.return_value = [mock.Mock(
            share_id=share_id,
            instance_uuid=instance.uuid,
            tag=tag,
            status=driver.obj_fields.ShareMappingStatus.ACTIVE)]
        container = mock.Mock(
            status='Running',
            config={'volatile.idmap.base': '1065536'},
            expanded_devices={
                'root': {'type': 'disk', 'path': '/', 'pool': 'local'}},
        )
        profile = mock.Mock(
            config={'security.idmap.size': '65536'},
            devices={'root': {'type': 'disk', 'path': '/'}},
        )
        self.client.instances.get.return_value = container
        self.client.profiles.get.return_value = profile
        self.client.storage_pools.get.return_value.driver = 'zfs'
        self.CONF.incus.allow_cold_migration = True
        self.CONF.incus.migration_address = 'https://10.224.0.16:8443'
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        with mock.patch.object(
                driver, '_migration_client') as migration_client:
            raised = self.assertRaises(
                exception.InstanceFaultRollback,
                incus_driver.migrate_disk_and_power_off,
                ctx, instance, '10.224.0.17', instance.flavor, [])

        self.assertIsInstance(
            raised.inner_exception, exception.MigrationPreCheckError)
        profile.save.assert_not_called()
        container.stop.assert_not_called()
        container.generate_migration_data.assert_not_called()
        self.ensure_instance_idmap.assert_not_called()
        migration_client.assert_not_called()

    def test_cold_migration_rejects_invalid_manila_share_id_without_effects(
            self):
        self._assert_cold_migration_rejects_invalid_manila_mapping(
            share_id='not-a-canonical-share-id')

    def test_cold_migration_rejects_invalid_manila_tag_without_effects(self):
        self._assert_cold_migration_rejects_invalid_manila_mapping(
            tag='../escape')

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

    def test_bfv_migration_requires_ready_fence_extension(self):
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
        extensions = self.client.host_info['api_extensions']
        extensions.remove(driver.INCUS_STORAGE_READY_FENCE_EXTENSION)
        extensions.extend([
            'migration_shared_ceph_storage',
            'storage_driver_cephext',
        ])

        self.assertRaisesRegex(
            exception.MigrationError,
            driver.INCUS_STORAGE_READY_FENCE_EXTENSION,
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
            driver.INCUS_STORAGE_READY_FENCE_EXTENSION,
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

    @mock.patch.object(driver, '_settle_instance_migration_operations')
    @mock.patch.object(driver, '_migration_client')
    def test_migrate_disk_failure_restarts_source(
            self, migration_client, settle_operations):
        events = []
        container = mock.Mock(status='Running')
        container.config = {'volatile.idmap.base': '1065536'}
        container.expanded_devices = {
            'root': {'type': 'disk', 'path': '/', 'pool': 'root'}}
        container.generate_migration_data.side_effect = RuntimeError(
            'migration operation failed')
        container.stop.side_effect = (
            lambda **kwargs: setattr(container, 'status', 'Stopped'))
        self.client.instances.get.return_value = container
        self.client.profiles.get.return_value.config = {
            'environment.product_name': 'OpenStack Nova',
            'security.idmap.size': '65536'}
        self.client.api.operations.get.return_value.json.return_value = {
            'metadata': {}}
        self.CONF.incus.allow_cold_migration = True
        self.CONF.incus.migration_address = 'https://10.224.0.16:8443'
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        self.client.profiles.get.return_value.config[
            'user.openstack.uuid'] = instance.uuid
        container.start.side_effect = lambda **kwargs: events.append('start')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.mark_source_volume_generation_rollback_complete = (
            mock.Mock(side_effect=lambda *_args: events.append('marker')))
        incus_driver.finalize_failed_cold_source_volume_generation = (
            mock.Mock(return_value=True))

        self.assertRaises(
            RuntimeError,
            incus_driver.migrate_disk_and_power_off,
            ctx, instance, '10.224.0.17', instance.flavor, [])

        container.stop.assert_called_once_with(wait=True)
        container.start.assert_called_once_with(wait=True)
        self.assertLess(events.index('marker'), events.index('start'))

    @mock.patch.object(driver, '_migration_client')
    @mock.patch.object(driver, '_preflight_bfv_migration_destination')
    @mock.patch.object(driver, '_require_bfv_migration_support')
    @mock.patch.object(driver, '_boot_from_volume')
    @mock.patch('nova.virt.driver.block_device_info_get_mapping')
    def test_migrate_disk_detaches_only_data_volumes(
            self, get_mapping, boot_from_volume, require_bfv, preflight,
            migration_client):
        container = mock.Mock(status='Running')
        container.config = {'volatile.idmap.base': '1065536'}
        container.expanded_devices = {
            'root': {'type': 'disk', 'path': '/', 'pool': 'root'}}
        container.generate_migration_data.return_value = {
            'source': {
                'operation': (
                    'http+unix://incus/1.0/operations/'
                    '20000000-0000-0000-0000-000000000002'),
            },
        }
        self.client.instances.get.return_value = container
        self.client.profiles.get.return_value.config = {
            'environment.product_name': 'OpenStack Nova',
            'security.idmap.size': '65536'}
        self.client.api.operations.get.return_value.json.return_value = {
            'metadata': {}}
        self.CONF.incus.allow_cold_migration = True
        self.CONF.incus.migration_address = 'https://10.224.0.16:8443'
        connection_info = {'driver_volume_type': 'local', 'data': {
            'volume_id': '50000000-0000-0000-0000-000000000005'}}
        root_bdm = {
            'boot_index': 0,
            'connection_info': mock.sentinel.root_connection,
            'mount_device': '/dev/sda',
        }
        boot_from_volume.return_value = root_bdm
        require_bfv.return_value = ('cinder-volumes', 'volume-root')
        get_mapping.return_value = [root_bdm, {
            'attachment_id':
                '30000000-0000-0000-0000-000000000003',
            'connection_info': connection_info,
            'mount_device': '/dev/vdb',
        }]
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        self.client.profiles.get.return_value.config[
            'user.openstack.uuid'] = instance.uuid
        instance.flavor.root_gb = 20
        smaller_flavor = mock.Mock(root_gb=10)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._detach_volume = mock.Mock()

        incus_driver.migrate_disk_and_power_off(
            ctx, instance, '10.224.0.17',
            smaller_flavor, [], block_device_info={})

        incus_driver._detach_volume.assert_called_once_with(
            ctx, connection_info, instance, '/dev/vdb',
            retain_journal=True)
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
            driver.INCUS_STORAGE_HANDOVER_EXTENSION,
            driver.INCUS_STORAGE_HANDOVER_PROOF_EXTENSION,
            'migration_shared_ceph_storage',
            'migration_live_shared_cephext_storage',
            driver.INCUS_STORAGE_READY_FENCE_EXTENSION,
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
            driver.INCUS_STORAGE_HANDOVER_EXTENSION,
            driver.INCUS_STORAGE_HANDOVER_PROOF_EXTENSION,
            driver.INCUS_STORAGE_READY_FENCE_EXTENSION,
            'storage_driver_cephext']}

        self.assertRaisesRegex(
            exception.MigrationError,
            'missing API extensions: migration_shared_ceph_storage',
            driver._preflight_bfv_migration_destination,
            'compute-2.example.test', 'cinder-volumes')

    @mock.patch.object(driver.incus_client,
                       'get_migration_preflight_client')
    @mock.patch.object(driver.socket, 'create_connection')
    def test_bfv_destination_preflight_rejects_missing_ready_fence(
            self, connect, get_remote):
        get_remote.return_value.host_info = {'api_extensions': [
            'migration_shared_ceph_storage',
            'storage_driver_cephext',
        ]}

        self.assertRaisesRegex(
            exception.MigrationError,
            driver.INCUS_STORAGE_READY_FENCE_EXTENSION,
            driver._preflight_bfv_migration_destination,
            'compute-2.example.test', 'cinder-volumes')

    @mock.patch.object(driver, '_migration_client')
    @mock.patch.object(
        driver, '_migration_address_for_host',
        return_value='https://compute-2.example.test:8443')
    def test_shared_ceph_preflight_rejects_missing_ready_fence(
            self, migration_address, migration_client):
        remote = migration_client.return_value
        remote.host_info = {
            'api_extensions': [driver.INCUS_STORAGE_HANDOVER_EXTENSION],
        }

        self.assertRaisesRegex(
            exception.MigrationError,
            driver.INCUS_STORAGE_READY_FENCE_EXTENSION,
            driver._preflight_shared_ceph_handover_destination,
            'compute-2.example.test', 'ceph-root', {
                'shared': True,
                'driver': 'ceph',
                'cluster': 'ceph',
                'source': 'incus-rootfs',
            })

        remote.storage_pools.get.assert_not_called()

    @mock.patch.object(driver, '_migration_client')
    @mock.patch.object(
        driver, '_migration_address_for_host',
        return_value='https://compute-2.example.test:8443')
    def test_shared_ceph_preflight_accepts_redacted_destination_config(
            self, migration_address, migration_client):
        # Incus redacts pool config for the restricted preflight identity;
        # exact cluster/source equality is then enforced by the Incus
        # migration negotiation, not this preflight.
        remote = migration_client.return_value
        remote.host_info = {'api_extensions': [
            driver.INCUS_STORAGE_HANDOVER_EXTENSION,
            driver.INCUS_STORAGE_HANDOVER_PROOF_EXTENSION,
            driver.INCUS_STORAGE_READY_FENCE_EXTENSION,
        ]}
        remote.storage_pools.get.return_value = mock.Mock(
            driver='ceph', config=None)

        driver._preflight_shared_ceph_handover_destination(
            'compute-2.example.test', 'ceph-root', {
                'shared': True,
                'driver': 'ceph',
                'cluster': 'ceph',
                'source': 'incus-rootfs',
            })

    @mock.patch.object(driver, '_migration_client')
    @mock.patch.object(
        driver, '_migration_address_for_host',
        return_value='https://compute-2.example.test:8443')
    def test_shared_ceph_preflight_rejects_redacted_driver_mismatch(
            self, migration_address, migration_client):
        remote = migration_client.return_value
        remote.host_info = {'api_extensions': [
            driver.INCUS_STORAGE_HANDOVER_EXTENSION,
            driver.INCUS_STORAGE_HANDOVER_PROOF_EXTENSION,
            driver.INCUS_STORAGE_READY_FENCE_EXTENSION,
        ]}
        remote.storage_pools.get.return_value = mock.Mock(
            driver='cephext', config=None)

        self.assertRaisesRegex(
            exception.MigrationError,
            'pool identities differ',
            driver._preflight_shared_ceph_handover_destination,
            'compute-2.example.test', 'ceph-root', {
                'shared': True,
                'driver': 'ceph',
                'cluster': 'ceph',
                'source': 'incus-rootfs',
            })

    @mock.patch.object(driver, '_migration_client')
    @mock.patch.object(
        driver, '_migration_address_for_host',
        return_value='https://compute-2.example.test:8443')
    def test_shared_ceph_preflight_rejects_visible_identity_mismatch(
            self, migration_address, migration_client):
        remote = migration_client.return_value
        remote.host_info = {'api_extensions': [
            driver.INCUS_STORAGE_HANDOVER_EXTENSION,
            driver.INCUS_STORAGE_HANDOVER_PROOF_EXTENSION,
            driver.INCUS_STORAGE_READY_FENCE_EXTENSION,
        ]}
        remote.storage_pools.get.return_value = mock.Mock(
            driver='ceph', config={
                'ceph.cluster_name': 'ceph',
                'source': 'another-osd-pool',
            })

        self.assertRaisesRegex(
            exception.MigrationError,
            'pool identities differ',
            driver._preflight_shared_ceph_handover_destination,
            'compute-2.example.test', 'ceph-root', {
                'shared': True,
                'driver': 'ceph',
                'cluster': 'ceph',
                'source': 'incus-rootfs',
            })

    @mock.patch.object(driver.incus_client,
                       'get_migration_preflight_client')
    @mock.patch.object(driver.socket, 'create_connection')
    def test_bfv_destination_preflight_rejects_cinder_pool_mismatch(
            self, connect, get_remote):
        self.CONF.incus.boot_from_volume_storage_pools = {
            'cinder-volumes': 'cinder-bfv'}
        remote = get_remote.return_value
        remote.host_info = {'api_extensions': [
            driver.INCUS_STORAGE_HANDOVER_EXTENSION,
            driver.INCUS_STORAGE_HANDOVER_PROOF_EXTENSION,
            'migration_shared_ceph_storage',
            'migration_live_shared_cephext_storage',
            driver.INCUS_STORAGE_READY_FENCE_EXTENSION,
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

    @mock.patch.object(driver, '_settle_instance_migration_operations')
    @mock.patch.object(driver, '_migration_client')
    @mock.patch('nova.virt.driver.block_device_info_get_mapping')
    def test_migrate_disk_volume_failure_restores_source(
            self, get_mapping, migration_client, settle_operations):
        container = mock.Mock(status='Running')
        container.config = {'volatile.idmap.base': '1065536'}
        container.expanded_devices = {
            'root': {'type': 'disk', 'path': '/', 'pool': 'root'}}
        container.generate_migration_data.return_value = {
            'source': {
                'operation': (
                    'http+unix://incus/1.0/operations/'
                    '20000000-0000-0000-0000-000000000002'),
            },
        }
        container.stop.side_effect = (
            lambda **kwargs: setattr(container, 'status', 'Stopped'))
        self.client.instances.get.return_value = container
        self.client.profiles.get.return_value.config = {
            'environment.product_name': 'OpenStack Nova',
            'security.idmap.size': '65536'}
        self.client.api.operations.get.return_value.json.return_value = {
            'metadata': {}}
        self.CONF.incus.allow_cold_migration = True
        self.CONF.incus.migration_address = 'https://10.224.0.16:8443'
        first = {'driver_volume_type': 'local', 'data': {
            'volume_id': '50000000-0000-0000-0000-000000000005'}}
        second = {'driver_volume_type': 'local', 'data': {
            'volume_id': '60000000-0000-0000-0000-000000000006'}}
        get_mapping.return_value = [
            {'attachment_id':
             '30000000-0000-0000-0000-000000000003',
             'connection_info': first, 'mount_device': '/dev/vdb'},
            {'attachment_id':
             '40000000-0000-0000-0000-000000000004',
             'connection_info': second, 'mount_device': '/dev/vdc'},
        ]
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        self.client.profiles.get.return_value.config[
            'user.openstack.uuid'] = instance.uuid
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._detach_volume = mock.Mock(
            side_effect=[None, RuntimeError('disconnect failed')])
        intents = [
            {'volume': 'second'},
            {'volume': 'first'},
        ]
        incus_driver._attach_volume_for_operation = mock.Mock(
            side_effect=intents)
        events = []
        incus_driver.mark_source_volume_generation_rollback_complete = (
            mock.Mock(side_effect=lambda *_args: events.append('marker')))
        incus_driver._commit_internal_volume_attach_operation = mock.Mock(
            side_effect=lambda *_args: events.append('commit'))
        incus_driver.finalize_failed_cold_source_volume_generation = (
            mock.Mock(return_value=True))

        self.assertRaises(
            RuntimeError, incus_driver.migrate_disk_and_power_off,
            ctx, instance, '10.224.0.17',
            instance.flavor, [], block_device_info={})

        self.assertEqual(
            [
                mock.call(
                    ctx, second, instance, '/dev/vdc',
                    '40000000-0000-0000-0000-000000000004', 'migration',
                    '20000000-0000-0000-0000-000000000002',
                    'cold-source-restore',
                    operation_migration_uuid=(
                        '20000000-0000-0000-0000-000000000002')),
                mock.call(
                    ctx, first, instance, '/dev/vdb',
                    '30000000-0000-0000-0000-000000000003', 'migration',
                    '20000000-0000-0000-0000-000000000002',
                    'cold-source-restore',
                    operation_migration_uuid=(
                        '20000000-0000-0000-0000-000000000002')),
            ],
            incus_driver._attach_volume_for_operation.
            call_args_list)
        self.assertEqual(['marker', 'commit', 'commit'], events)
        container.start.assert_called_once_with(wait=True)

    @mock.patch.object(driver, '_settle_instance_migration_operations')
    @mock.patch.object(driver, '_migration_client')
    @mock.patch('nova.virt.driver.block_device_info_get_mapping')
    def test_migrate_disk_volume_rollback_failure_keeps_source_stopped(
            self, get_mapping, migration_client, settle_operations):
        container = mock.Mock(status='Running')
        container.config = {'volatile.idmap.base': '1065536'}
        container.expanded_devices = {
            'root': {'type': 'disk', 'path': '/', 'pool': 'root'}}
        container.generate_migration_data.return_value = {
            'source': {
                'operation': (
                    'http+unix://incus/1.0/operations/'
                    '20000000-0000-0000-0000-000000000002'),
            },
        }
        container.stop.side_effect = (
            lambda **kwargs: setattr(container, 'status', 'Stopped'))
        self.client.instances.get.return_value = container
        self.client.profiles.get.return_value.config = {
            'environment.product_name': 'OpenStack Nova',
            'security.idmap.size': '65536'}
        self.CONF.incus.allow_cold_migration = True
        self.CONF.incus.migration_address = 'https://10.224.0.16:8443'
        first = {
            'driver_volume_type': 'local',
            'data': {
                'volume_id': '50000000-0000-0000-0000-000000000005'},
        }
        second = {
            'driver_volume_type': 'local',
            'data': {
                'volume_id': '60000000-0000-0000-0000-000000000006'},
        }
        get_mapping.return_value = [
            {'attachment_id':
             '30000000-0000-0000-0000-000000000003',
             'connection_info': first, 'mount_device': '/dev/vdb'},
            {'attachment_id':
             '40000000-0000-0000-0000-000000000004',
             'connection_info': second, 'mount_device': '/dev/vdc'},
        ]
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        self.client.profiles.get.return_value.config[
            'user.openstack.uuid'] = instance.uuid
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._detach_volume = mock.Mock(
            side_effect=[None, RuntimeError('disconnect failed')])

        def attach_volume(
                context, connection_info, instance, mountpoint, *_args,
                **_kwargs):
            if connection_info is second:
                raise RuntimeError('second restore failed')

        incus_driver._attach_volume_for_operation = mock.Mock(
            side_effect=attach_volume)
        incus_driver.mark_source_volume_generation_rollback_complete = (
            mock.Mock())

        self.assertRaises(
            exception.MigrationError,
            incus_driver.migrate_disk_and_power_off,
            ctx, instance, '10.224.0.17',
            instance.flavor, [], block_device_info={})

        self.assertEqual(
            [
                mock.call(
                    ctx, second, instance, '/dev/vdc',
                    '40000000-0000-0000-0000-000000000004', 'migration',
                    '20000000-0000-0000-0000-000000000002',
                    'cold-source-restore',
                    operation_migration_uuid=(
                        '20000000-0000-0000-0000-000000000002')),
                mock.call(
                    ctx, first, instance, '/dev/vdb',
                    '30000000-0000-0000-0000-000000000003', 'migration',
                    '20000000-0000-0000-0000-000000000002',
                    'cold-source-restore',
                    operation_migration_uuid=(
                        '20000000-0000-0000-0000-000000000002')),
            ],
            incus_driver._attach_volume_for_operation.
            call_args_list)
        incus_driver.mark_source_volume_generation_rollback_complete.\
            assert_not_called()
        container.start.assert_not_called()

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
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        profile = mock.Mock()
        profile.devices = {}
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        }
        self.client.profiles.get.return_value = profile
        realpath.return_value = '/dev/sdc'
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

        self.assertEqual(
            3, incus_driver.client.profiles.get.call_count)
        self.assertEqual([
            mock.call(instance.name),
            mock.call(instance.name),
            mock.call(instance.name),
        ], incus_driver.client.profiles.get.call_args_list)
        volume_connector.connect_volume.assert_called_once_with(
            connection_info['data'])
        self.assertEqual({
            _TEST_VOLUME_ID: {
                'path': '/dev/sdd',
                'required': 'true',
                'source': '/dev/sdc',
                'type': 'unix-block',
                'limits.read': '500iops',
                'limits.write': '1048576B',
            },
        }, profile.devices)
        expected_connection_data = dict(connection_info['data'])
        expected_connection_data.pop('auth_password')
        self.assertEqual(
            {
                'version': 2,
                'phase': 'connected',
                'driver_volume_type': 'rbd',
                'connection_data': expected_connection_data,
                'device_info': {'path': '/dev/disk/x'},
                'mountpoint': '/dev/sdd',
            },
            jsonutils.loads(profile.config[
                driver._volume_device_info_key(_TEST_VOLUME_ID)]))
        self.assertEqual(2, profile.save.call_count)
        profile.save.assert_has_calls([
            mock.call(wait=True),
            mock.call(wait=True),
        ])

    def test_attach_volume_rejects_non_rbd_before_side_effects(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-non-rbd-attach', memory_mb=0)
        connection_info = {
            'driver_volume_type': 'iscsi',
            'serial': 'volume-1',
            'data': {'volume_id': 'volume-1'},
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        with mock.patch.object(driver, 'brick_get_connector') as connector:
            self.assertRaisesRegex(
                exception.InvalidVolume, 'require the Cinder RBD protocol',
                incus_driver.attach_volume, ctx, connection_info, instance,
                '/dev/vdb')

        connector.assert_not_called()
        self.client.instances.get.assert_not_called()
        self.client.profiles.get.assert_not_called()

    @mock.patch.object(
        driver.IncusDriver, 'confirm_connected_volume_journal')
    @mock.patch.object(driver.IncusDriver, '_attach_volume_locked')
    def test_stage_live_volume_allows_missing_verified_target(
            self, attach_locked, confirm):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        cleanup_token = '10000000-0000-0000-0000-000000000001'
        migration_uuid = '10000000-0000-0000-0000-000000000004'
        volume_id = '20000000-0000-0000-0000-000000000002'
        attachment_id = '30000000-0000-0000-0000-000000000003'
        connection_info = {
            'serial': volume_id,
            'driver_volume_type': 'rbd',
            'data': {'name': 'volumes/volume-{}'.format(volume_id)},
        }
        self.client.instances.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
        }
        profile.used_by = []
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver._stage_volume_for_live_migration(
            ctx, connection_info, instance, '/dev/vdb', attachment_id,
            cleanup_token, migration_uuid)

        attach_locked.assert_called_once_with(
            ctx, connection_info, instance, '/dev/vdb',
            encryption=None,
            allow_missing_instance=True,
            expected_migration_token=cleanup_token,
            require_missing_instance=True, retain_journal=True)
        confirm.assert_not_called()
        intent = incus_driver.get_managed_volume_attach_intent(
            instance, volume_id)
        self.assertEqual(attachment_id, intent['attachment_id'])
        self.assertEqual('/dev/vdb', intent['mountpoint'])
        self.assertEqual('live-target', intent['operation_direction'])
        self.assertEqual('migration', intent['operation_kind'])
        self.assertEqual(
            migration_uuid, intent['operation_migration_uuid'])
        self.assertEqual(cleanup_token, intent['operation_token'])
        self.assertEqual(volume_id, intent['volume_id'])

    @mock.patch.object(driver.IncusDriver, '_attach_volume_locked')
    def test_stage_live_volume_rejects_unverified_profile(
            self, attach_locked):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        self.client.instances.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY:
                '10000000-0000-0000-0000-000000000009',
        }
        profile.used_by = []
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        migration_uuid = '10000000-0000-0000-0000-000000000004'

        self.assertRaises(
            exception.MigrationError,
            incus_driver._stage_volume_for_live_migration,
            ctx, {
                'serial': '20000000-0000-0000-0000-000000000002',
                'driver_volume_type': 'rbd',
                'data': {'name': 'volumes/volume-test'},
            }, instance, '/dev/vdb',
            '30000000-0000-0000-0000-000000000003',
            '10000000-0000-0000-0000-000000000001',
            migration_uuid)

        attach_locked.assert_not_called()

    @mock.patch('os.path.realpath', return_value='/dev/sdc')
    def test_attach_volume_resumes_connecting_journal(self, realpath):
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        pending = driver._serialize_volume_attachment(
            connection_info, {}, '/dev/sdd', phase='connecting')
        profile = mock.Mock(
            devices={},
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver._volume_device_info_key(_TEST_VOLUME_ID): pending,
            })
        self.client.profiles.get.return_value = profile
        driver._write_volume_journal(
            instance, _TEST_VOLUME_ID, connection_info, {}, '/dev/sdd',
            'connecting')
        connector = mock.Mock()
        connector.connect_volume.return_value = {'path': '/dev/sdc'}
        driver.brick_get_connector = mock.Mock(return_value=connector)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.attach_volume(
            context.get_admin_context(), connection_info, instance,
            '/dev/sdd')

        connector.connect_volume.assert_called_once_with(
            connection_info['data'])
        self.assertEqual('connected', jsonutils.loads(
            profile.config[
                driver._volume_device_info_key(_TEST_VOLUME_ID)])['phase'])
        self.assertEqual(
            '/dev/sdc', profile.devices[_TEST_VOLUME_ID]['source'])
        self.assertEqual(
            'connected', driver._read_volume_journal(
                instance, _TEST_VOLUME_ID)['phase'])

    @mock.patch('os.path.realpath', return_value='/dev/sdc')
    def test_detach_volume_reconnects_matching_connecting_journal(
            self, realpath):
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        pending = driver._serialize_volume_attachment(
            connection_info, {}, '/dev/sdd', phase='connecting')
        profile = mock.Mock(
            devices={},
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver._volume_device_info_key(_TEST_VOLUME_ID): pending,
            })
        self.client.profiles.get.return_value = profile
        driver._write_volume_journal(
            instance, _TEST_VOLUME_ID, connection_info, {}, '/dev/sdd',
            'connecting')
        connector = mock.Mock()
        connector.connect_volume.return_value = {'path': '/dev/sdc'}
        driver.brick_get_connector = mock.Mock(return_value=connector)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.detach_volume(
            context.get_admin_context(), connection_info, instance,
            '/dev/sdd')

        connector.connect_volume.assert_called_once_with(
            connection_info['data'])
        connector.disconnect_volume.assert_called_once_with(
            connection_info['data'], {'path': '/dev/sdc'})
        self.assertEqual(
            'disconnected',
            driver._read_volume_journal(
                instance, _TEST_VOLUME_ID)['phase'])
        self.assertNotIn(
            driver._volume_device_info_key(_TEST_VOLUME_ID), profile.config)

    @mock.patch('os.path.realpath', return_value='/dev/sdc')
    def test_detach_volume_recovers_connecting_journal_without_profile(
            self, realpath):
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        self.client.profiles.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        driver._write_volume_journal(
            instance, _TEST_VOLUME_ID, connection_info, {}, '/dev/sdd',
            'connecting')
        connector = mock.Mock()
        connector.connect_volume.return_value = {'path': '/dev/sdc'}
        driver.brick_get_connector = mock.Mock(return_value=connector)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.detach_volume(
            context.get_admin_context(), connection_info, instance,
            '/dev/sdd')

        connector.connect_volume.assert_called_once_with(
            connection_info['data'])
        connector.disconnect_volume.assert_called_once_with(
            connection_info['data'], {'path': '/dev/sdc'})
        self.assertEqual(
            'disconnected',
            driver._read_volume_journal(
                instance, _TEST_VOLUME_ID)['phase'])

    def test_detach_volume_rejects_connecting_journal_for_other_rbd(self):
        volume_id = '8231d2e8-1111-4222-8333-123456789abc'
        recorded = {
            'driver_volume_type': 'rbd',
            'serial': volume_id,
            'data': {'name': 'pool/volume-%s' % volume_id},
        }
        requested = copy.deepcopy(recorded)
        requested['data']['name'] = 'other/volume-%s' % volume_id
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        pending = driver._serialize_volume_attachment(
            recorded, {}, '/dev/sdd', phase='connecting')
        profile = mock.Mock(
            devices={},
            config={driver._volume_device_info_key(volume_id): pending})
        self.client.profiles.get.return_value = profile
        driver._write_volume_journal(
            instance, volume_id, recorded, {}, '/dev/sdd', 'connecting')
        connector = mock.Mock()
        driver.brick_get_connector = mock.Mock(return_value=connector)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.InvalidVolume, incus_driver.detach_volume,
            context.get_admin_context(), requested, instance, '/dev/sdd')

        connector.connect_volume.assert_not_called()
        connector.disconnect_volume.assert_not_called()
        self.assertEqual(
            'connecting',
            driver._read_volume_journal(instance, volume_id)['phase'])

    @mock.patch('os.path.realpath', return_value='/dev/sdc')
    def test_attach_volume_recovers_connected_journal_before_profile(
            self, realpath):
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        pending = driver._serialize_volume_attachment(
            connection_info, {}, '/dev/sdd', phase='connecting')
        profile = mock.Mock(
            devices={},
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver._volume_device_info_key(_TEST_VOLUME_ID): pending,
            })
        self.client.profiles.get.return_value = profile
        driver._write_volume_journal(
            instance, _TEST_VOLUME_ID, connection_info,
            {'path': '/dev/sdc'},
            '/dev/sdd', 'connected')
        connector = mock.Mock()
        connector.connect_volume.return_value = {'path': '/dev/sdc'}
        driver.brick_get_connector = mock.Mock(return_value=connector)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.attach_volume(
            context.get_admin_context(), connection_info, instance,
            '/dev/sdd')

        connector.connect_volume.assert_called_once_with(
            connection_info['data'])
        self.assertEqual('connected', jsonutils.loads(
            profile.config[
                driver._volume_device_info_key(_TEST_VOLUME_ID)])['phase'])
        self.assertEqual(
            'connected', driver._read_volume_journal(
                instance, _TEST_VOLUME_ID)['phase'])

    @mock.patch('os.path.realpath', return_value='/dev/sdc')
    def test_attach_volume_finishes_disconnect_before_reconnect(
            self, realpath):
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        disconnecting = driver._serialize_volume_attachment(
            connection_info, {'path': '/dev/sdc'}, '/dev/sdd',
            phase='disconnecting')
        profile = mock.Mock(
            devices={},
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver._volume_device_info_key(
                    _TEST_VOLUME_ID): disconnecting,
            })
        self.client.profiles.get.return_value = profile
        driver._write_volume_journal(
            instance, _TEST_VOLUME_ID, connection_info,
            {'path': '/dev/sdc'},
            '/dev/sdd', 'disconnecting')
        connector = mock.Mock()
        connector.connect_volume.return_value = {'path': '/dev/sdc'}
        driver.brick_get_connector = mock.Mock(return_value=connector)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.attach_volume(
            context.get_admin_context(), connection_info, instance,
            '/dev/sdd')

        connector.disconnect_volume.assert_called_once_with(
            connection_info['data'], {'path': '/dev/sdc'})
        connector.connect_volume.assert_called_once_with(
            connection_info['data'])
        self.assertEqual('connected', jsonutils.loads(
            profile.config[
                driver._volume_device_info_key(_TEST_VOLUME_ID)])['phase'])
        self.assertEqual(
            'connected', driver._read_volume_journal(
                instance, _TEST_VOLUME_ID)['phase'])

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
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        profile = mock.Mock()
        profile.devices = {}
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        }
        profile.save.side_effect = [
            None,
            RuntimeError('Incus API failed'),
            None,
            None,
            None,
        ]
        self.client.profiles.get.return_value = profile
        volume_connector = mock.Mock()
        device_info = {'path': '/dev/sdc'}
        volume_connector.connect_volume.return_value = device_info
        driver.brick_get_connector = mock.Mock(return_value=volume_connector)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            RuntimeError, incus_driver.attach_volume,
            context.get_admin_context(), connection_info, instance,
            '/dev/sdd')

        volume_connector.disconnect_volume.assert_called_once_with(
            connection_info['data'], device_info)

    @mock.patch('os.path.realpath', return_value='/dev/sdc')
    def test_attach_volume_retains_mapping_when_profile_still_references_it(
            self, realpath):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        owner_config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        }
        initial = mock.Mock(devices={}, config=dict(owner_config))
        initial.save.side_effect = RuntimeError('profile update failed')
        persisted = mock.Mock(
            devices={_TEST_VOLUME_ID: {
                'path': '/dev/sdd',
                'required': 'true',
                'source': '/dev/sdc',
                'type': 'unix-block',
            }},
            config={
                **owner_config,
                driver._volume_device_info_key(_TEST_VOLUME_ID):
                    '{"path":"/dev/sdc"}',
            })
        persisted.save.side_effect = RuntimeError('instance is busy')
        still_persisted = mock.Mock(
            devices=dict(persisted.devices),
            config=dict(persisted.config))
        self.client.profiles.get.side_effect = [
            initial, persisted, still_persisted]
        volume_connector = mock.Mock()
        volume_connector.connect_volume.return_value = {'path': '/dev/sdc'}
        driver.brick_get_connector = mock.Mock(return_value=volume_connector)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'}, '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            RuntimeError, incus_driver.attach_volume,
            context.get_admin_context(), connection_info, instance,
            '/dev/sdd')

        volume_connector.disconnect_volume.assert_not_called()

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

    def test_attach_connection_encryption_marker_is_rejected_before_connect(
            self):
        volume_connector = mock.Mock()
        driver.brick_get_connector = mock.Mock(return_value=volume_connector)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        connection_info['data']['encrypted'] = {'provider': 'luks'}
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.VolumeEncryptionNotSupported,
            incus_driver.attach_volume,
            context.get_admin_context(), connection_info, instance,
            '/dev/sdd')

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
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        profile = mock.Mock()
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        }
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
                ctx, connection_info, instance, '/dev/sdd')

        get_connector.assert_not_called()

    def test_attach_volume_serializes_different_ids_for_same_mountpoint(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-topology-lock', memory_mb=0)
        profile = mock.Mock(
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
            },
            devices={})
        self.client.profiles.get.return_value = profile
        self.client.instances.get.return_value = mock.Mock(status='Stopped')

        first_connect_entered = threading.Event()
        release_first_connect = threading.Event()
        second_started = threading.Event()
        connector = mock.Mock()

        def connect_volume(connection_data):
            first_connect_entered.set()
            if not release_first_connect.wait(5):
                raise RuntimeError('test timed out waiting to release connect')
            return {'path': '/dev/sdc'}

        connector.connect_volume.side_effect = connect_volume
        driver.brick_get_connector = mock.Mock(return_value=connector)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        connections = [
            fake_connection_info(
                {'id': index, 'name': 'volume-%d' % index},
                '10.0.2.15:3260',
                'iqn.2010-10.org.openstack:volume-%d' % index)
            for index in (1, 2)
        ]
        results = []

        def attach(connection_info, started=None):
            if started is not None:
                started.set()
            try:
                incus_driver.attach_volume(
                    ctx, connection_info, instance, '/dev/sdd')
            except Exception as exc:
                results.append(exc)
            else:
                results.append(None)

        first = threading.Thread(target=attach, args=(connections[0],))
        second = threading.Thread(
            target=attach, args=(connections[1], second_started))
        first.start()
        self.assertTrue(first_connect_entered.wait(5))
        second.start()
        self.assertTrue(second_started.wait(5))
        time.sleep(0.1)
        self.assertEqual(1, connector.connect_volume.call_count)
        self.assertTrue(second.is_alive())

        release_first_connect.set()
        first.join(5)
        second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(1, connector.connect_volume.call_count)
        self.assertEqual(1, sum(result is None for result in results))
        failures = [result for result in results if result is not None]
        self.assertEqual(1, len(failures))
        self.assertIsInstance(failures[0], exception.DevicePathInUse)
        self.assertEqual(['/dev/sdd'], [
            device['path'] for device in profile.devices.values()])

    def test_attach_volume_rejects_duplicate_volume_before_connect(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        profile = mock.Mock()
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        }
        profile.devices = {
            _TEST_VOLUME_ID: {
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
                ctx, connection_info, instance, '/dev/sdd')

        get_connector.assert_not_called()

    @mock.patch('os.path.realpath', return_value='/var/lib/tenant-volume')
    def test_attach_volume_rejects_non_device_connector_path(self, realpath):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        profile = mock.Mock()
        profile.devices = {}
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        }
        self.client.profiles.get.return_value = profile
        volume_connector = mock.Mock()
        device_info = {'path': '/var/lib/tenant-volume'}
        volume_connector.connect_volume.return_value = device_info
        driver.brick_get_connector = mock.Mock(return_value=volume_connector)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15', 'iqn.2010-10.org.openstack:volume-00000001')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.InvalidVolume, incus_driver.attach_volume,
            context.get_admin_context(), connection_info, instance,
            '/dev/sdd')

        volume_connector.disconnect_volume.assert_called_once_with(
            connection_info['data'], device_info)

    def test_attach_volume_without_device_path_disconnects(self):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        profile = mock.Mock()
        profile.devices = {}
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        }
        self.client.profiles.get.return_value = profile
        volume_connector = mock.Mock()
        device_info = {}
        volume_connector.connect_volume.return_value = device_info
        driver.brick_get_connector = mock.Mock(return_value=volume_connector)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'},
            '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
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
            _TEST_VOLUME_ID: {
                'path': '/dev/sdc',
                'source': '/dev/rbd0',
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
        mountpoint = '/dev/sdc'

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        volume_connector = mock.Mock()
        driver.brick_get_connector = mock.Mock(return_value=volume_connector)
        incus_driver.detach_volume(ctx, connection_info, instance,
                                   mountpoint, None)

        self.assertEqual(
            [mock.call(instance.name)] * 3,
            incus_driver.client.profiles.get.call_args_list)

        self.assertEqual(expected, profile.devices)
        self.assertEqual(
            [mock.call(wait=True), mock.call(wait=True)],
            profile.save.call_args_list)
        volume_connector.disconnect_volume.assert_called_once_with(
            connection_info['data'], {'path': '/dev/rbd0'})

    def test_detach_volume_accepts_persisted_profile_change(self):
        device = {
            'path': '/dev/sdc',
            'source': '/dev/rbd0',
            'type': 'unix-block',
        }
        profile = mock.Mock(devices={_TEST_VOLUME_ID: device}, config={})
        persisted = mock.Mock(devices={}, config={})

        def save_with_persisted_failure(wait=True):
            persisted.devices = copy.deepcopy(profile.devices)
            persisted.config = copy.deepcopy(profile.config)
            raise incus_operation_exception(
                400, 'profile change still saved')

        profile.save.side_effect = save_with_persisted_failure
        self.client.profiles.get.side_effect = [
            profile, profile, persisted, persisted]
        connector = mock.Mock()
        driver.brick_get_connector = mock.Mock(return_value=connector)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'}, '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.detach_volume(
            context.get_admin_context(), connection_info, instance,
            '/dev/sdc')

        connector.disconnect_volume.assert_called_once_with(
            connection_info['data'], {'path': '/dev/rbd0'})

    def test_detach_volume_retains_mapping_when_profile_change_failed(self):
        device = {
            'path': '/dev/sdc',
            'source': '/dev/rbd0',
            'type': 'unix-block',
        }
        profile = mock.Mock(devices={_TEST_VOLUME_ID: device}, config={})
        profile.save.side_effect = RuntimeError('instance is busy')
        persisted = mock.Mock(
            devices={_TEST_VOLUME_ID: device}, config={})
        self.client.profiles.get.side_effect = [profile, profile, persisted]
        connector = mock.Mock()
        driver.brick_get_connector = mock.Mock(return_value=connector)
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'}, '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            RuntimeError, incus_driver.detach_volume,
            context.get_admin_context(), connection_info, instance,
            '/dev/sdc')

        connector.disconnect_volume.assert_not_called()

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
            'driver_volume_type': 'rbd',
            'serial': volume_id,
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
        self.assertEqual(
            [mock.call(wait=True), mock.call(wait=True)],
            profile.save.call_args_list)
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
            'serial': volume_id,
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
        self.assertEqual(
            [mock.call(wait=True), mock.call(wait=True)],
            profile.save.call_args_list)
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
        instance = mock.Mock(root_device_name='/dev/sda')
        instance.name = 'instance-00000001'
        instance.uuid = '10000000-0000-0000-0000-000000000001'
        not_found = incuscore_exceptions.NotFound(MockResponse(404))
        self.client.instances.get.side_effect = not_found
        self.client.profiles.get.side_effect = not_found
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        incus_driver.detach_volume(
            mock.sentinel.context, connection_info, instance, '/dev/sda')

        self.client.instances.get.assert_called_once_with(instance.name)
        self.client.profiles.get.assert_called_once_with(instance.name)

    def test_detach_bfv_root_rejects_existing_instance(self):
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'}, '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        instance = mock.Mock(
            name='instance-00000001', root_device_name='/dev/sda')
        self.client.instances.get.return_value = mock.sentinel.container
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertRaisesRegex(
            exception.InvalidVolume, 'instance still exists',
            incus_driver.detach_volume, mock.sentinel.context,
            connection_info, instance, '/dev/sda')

        self.client.profiles.get.assert_not_called()

    def test_detach_bfv_root_rejects_existing_profile(self):
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'}, '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        instance = mock.Mock(
            name='instance-00000001', root_device_name='/dev/sda')
        self.client.instances.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        self.client.profiles.get.return_value = mock.sentinel.profile
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertRaisesRegex(
            exception.InvalidVolume, 'profile still exists',
            incus_driver.detach_volume, mock.sentinel.context,
            connection_info, instance, '/dev/sda')

    def test_detach_missing_profile_does_not_hide_data_volume(self):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='instance-00000001',
            root_device_name='/dev/sda')
        self.client.profiles.get.side_effect = incuscore_exceptions.NotFound(
            MockResponse(404))
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertRaises(
            incuscore_exceptions.NotFound, incus_driver.detach_volume,
            mock.sentinel.context,
            {
                'driver_volume_type': 'rbd',
                'serial': _TEST_VOLUME_ID,
                'data': {},
            },
            instance, '/dev/sdb')

    def test_detach_volume_restores_profile_on_disconnect_failure(self):
        device = {
            'path': '/dev/sdc',
            'source': '/dev/rbd0',
            'type': 'unix-block',
        }
        profile = mock.Mock()
        profile.devices = {_TEST_VOLUME_ID: device}
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

        self.assertEqual({}, profile.devices)
        self.assertEqual(1, profile.save.call_count)
        record = driver._read_volume_journal(instance, _TEST_VOLUME_ID)
        self.assertEqual('disconnecting', record['phase'])
        self.assertIn(
            driver._volume_device_info_key(_TEST_VOLUME_ID), profile.config)

    def test_detach_volume_uses_persisted_connector_device_info(self):
        profile = mock.Mock()
        profile.devices = {_TEST_VOLUME_ID: {
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
            driver._volume_device_info_key(_TEST_VOLUME_ID):
                jsonutils.dumps(device_info),
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
        self.assertNotIn(
            driver._volume_device_info_key(_TEST_VOLUME_ID), profile.config)

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
        container = self.client.instances.get.return_value
        container.config = {'volatile.idmap.base': '1065536'}
        container.expanded_devices = {
            'root': {'type': 'disk', 'path': '/', 'pool': 'root'}}
        self.client.profiles.get.return_value.config = {
            'security.idmap.size': '65536'}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.MigrationError,
            incus_driver.migrate_disk_and_power_off,
            ctx, instance, '10.224.0.17', instance.flavor, [])

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
                        driver.incus_privsep,
                        'chown_tree_to_host_id') as chown_tree:
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
            chown_tree.assert_called_once_with(
                mock.ANY, 100000)

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
        container.status = 'Stopped'
        container.status_code = None
        state = mock.Mock()
        state.memory = dict({'usage': 0, 'usage_peak': 0})
        state.status_code = 102
        container.state.return_value = state
        self.client.instances.get.return_value = container
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        container.name = instance.name
        container.type = 'container'
        self.client.instances.all.return_value = [container]

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.resume_state_on_host_boot(ctx, instance, None, None, None)
        container.start.assert_called_once_with(wait=True)
        self.ensure_start_idmap.assert_called_once_with(
            instance, container, _claim_lock_held=True)

    def test_resume_state_on_host_boot_forwards_bdm_and_fails_closed(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-host-boot-volume-failure', memory_mb=0)
        block_device_info = {'block_device_mapping': [mock.sentinel.bdm]}
        network_info = mock.sentinel.network_info
        share_info = [mock.sentinel.share]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.get_info = mock.Mock(
            return_value=mock.Mock(state=power_state.SHUTDOWN))
        failure = exception.InvalidVolume(reason='topology mismatch')
        incus_driver.power_on = mock.Mock(side_effect=failure)

        raised = self.assertRaises(
            exception.InvalidVolume,
            incus_driver.resume_state_on_host_boot,
            ctx, instance, network_info, share_info, block_device_info)

        self.assertIs(failure, raised)
        incus_driver.power_on.assert_called_once_with(
            ctx, instance, network_info, block_device_info,
            share_info=share_info)

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
        container.stop.assert_called_once_with(
            timeout=0, force=True, wait=True)

    def test_power_off_retries_busy_operation(self):
        container = mock.Mock(status='Running')
        container.stop.side_effect = [
            incus_operation_exception(
                400, 'Instance is busy running a "update" operation'),
            None,
        ]
        self.client.instances.get.return_value = container
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.power_off(instance)

        self.assertEqual(2, container.stop.call_count)

    def test_power_off_honors_graceful_timeout(self):
        container = mock.Mock(status='Running')
        self.client.instances.get.return_value = container
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.power_off(instance, timeout=15, retry_interval=3)

        container.stop.assert_called_once_with(
            timeout=15, force=False, wait=True)

    def test_power_off_forces_after_guest_shutdown_failure(self):
        graceful = mock.Mock(status='Running')
        forced = mock.Mock(status='Running')
        graceful.stop.side_effect = incus_operation_exception(
            400, 'Guest failed to shut down')
        self.client.instances.get.side_effect = [graceful, forced]
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.power_off(instance, timeout=15)

        graceful.stop.assert_called_once_with(
            timeout=15, force=False, wait=True)
        forced.stop.assert_called_once_with(
            timeout=0, force=True, wait=True)

    def test_power_off_does_not_mask_authorization_failure(self):
        container = mock.Mock(status='Running')
        failure = incus_api_exception(403, 'not authorized')
        container.stop.side_effect = failure
        self.client.instances.get.return_value = container
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        raised = self.assertRaises(
            incuscore_exceptions.LXDAPIException,
            incus_driver.power_off, instance, timeout=15)

        self.assertIs(failure, raised)
        container.stop.assert_called_once_with(
            timeout=15, force=False, wait=True)

    def test_power_off_converges_after_lost_response(self):
        stopping = mock.Mock(status='Running')
        stopped = mock.Mock(status='Stopped')
        stopping.stop.side_effect = (
            incuscore_exceptions.ClientConnectionFailed('response lost'))
        self.client.instances.get.side_effect = [stopping, stopped]
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.power_off(instance)

        stopping.stop.assert_called_once_with(
            timeout=0, force=True, wait=True)
        stopped.stop.assert_not_called()

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
        self.ensure_start_idmap.assert_called_once_with(
            instance, container, _claim_lock_held=True)

    def test_power_on_idmap_failure_blocks_start(self):
        container = self.client.instances.get.return_value
        container.status = 'Stopped'
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        failure = driver.incus_idmap.IDMapIntegrityError(
            reason='allocator generation changed')
        self.ensure_start_idmap.side_effect = failure
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        raised = self.assertRaises(
            driver.incus_idmap.IDMapIntegrityError,
            incus_driver.power_on,
            context.get_admin_context(), instance, None)

        self.assertIs(failure, raised)
        container.start.assert_not_called()

    def test_power_on_reasserts_vifs_after_start(self):
        container = mock.Mock(status='Stopped')
        self.client.instances.get.return_value = container
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        vif = mock.sentinel.vif
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.power_on(
            context.get_admin_context(), instance, [vif])

        self.vif_driver.reassert.assert_called_once_with(instance, vif)

    def test_power_on_retries_busy_operation(self):
        container = mock.Mock(status='Stopped')
        container.start.side_effect = [
            incus_operation_exception(
                409, 'Instance is busy running a "stop" operation'),
            None,
        ]
        self.client.instances.get.return_value = container
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.power_on(
            context.get_admin_context(), instance, None)

        self.assertEqual(2, container.start.call_count)

    def test_power_on_converges_after_lost_response(self):
        starting = mock.Mock(status='Stopped')
        running = mock.Mock(status='Running')
        starting.start.side_effect = (
            incuscore_exceptions.ClientConnectionFailed('response lost'))
        self.client.instances.get.side_effect = [starting, running]
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test', memory_mb=0)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.power_on(
            context.get_admin_context(), instance, None)

        starting.start.assert_called_once_with(wait=True)
        running.start.assert_not_called()

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
            'hypervisor_version': 7002,
            'disk_available_least': 500,
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
                'server_version': '7.2',
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

    @mock.patch.object(
        driver, '_get_fs_info',
        return_value={
            'total': 100 * units.Gi,
            'used': 20 * units.Gi,
            'available': 80 * units.Gi,
        })
    @mock.patch.object(
        driver, '_get_ram_usage',
        return_value={
            'total': 16 * units.Gi,
            'used': 4 * units.Gi,
        })
    @mock.patch.object(
        driver, '_get_cpu_info',
        return_value={
            'flags': 'flag',
            'model name': 'model',
            'socket(s)': '1',
            'core(s) per socket': '4',
            'thread(s) per core': '2',
            'vendor id': 'vendor',
        })
    def test_host_resource_snapshot_cache_is_single_use(
            self, get_cpu_info, get_ram_usage, get_fs_info):
        self.client.host_info = {
            'environment': {
                'storage': 'dir',
                'server_version': '7.2',
            },
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        initial = incus_driver._get_host_resource_snapshot('compute-1')
        get_cpu_info.reset_mock()
        get_ram_usage.reset_mock()
        get_fs_info.reset_mock()

        cached = incus_driver._get_host_resource_snapshot(
            'compute-1', use_cache=True)

        self.assertEqual(initial, cached)
        get_cpu_info.assert_not_called()
        get_ram_usage.assert_not_called()
        get_fs_info.assert_not_called()

        refreshed = incus_driver._get_host_resource_snapshot(
            'compute-1', use_cache=True)

        self.assertEqual(initial, refreshed)
        get_cpu_info.assert_called_once_with()
        get_ram_usage.assert_called_once_with()
        get_fs_info.assert_called_once_with(self.CONF.incus.root_dir)

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

    def test_placement_storage_pool_info_uses_shared_budget(self):
        pool = self.client.storage_pools.get.return_value
        pool.driver = 'ceph'

        result = driver._placement_storage_pool_info(
            self.client, 'shared-rootfs', 25)

        self.assertEqual({
            'total': 25 * units.Gi,
            'used': 0,
            'available': 25 * units.Gi,
        }, result)
        pool.resources.get.assert_not_called()

    def test_placement_storage_pool_info_reads_local_pool_once(self):
        pool = self.client.storage_pools.get.return_value
        pool.driver = 'zfs'
        pool.resources.get.return_value.space = {
            'total': 30 * units.Gi,
            'used': 10 * units.Gi,
        }

        result = driver._placement_storage_pool_info(
            self.client, 'local-rootfs')

        self.assertEqual(30 * units.Gi, result['total'])
        self.client.storage_pools.get.assert_called_once_with('local-rootfs')
        pool.resources.get.assert_called_once_with()

    def test_placement_storage_pool_info_requires_shared_budget(self):
        self.client.storage_pools.get.return_value.driver = 'ceph'

        self.assertRaises(
            exception.InvalidConfiguration,
            driver._placement_storage_pool_info,
            self.client,
            'shared-rootfs')

    def test_placement_storage_pool_info_rejects_budget_for_local_pool(self):
        self.client.storage_pools.get.return_value.driver = 'zfs'

        self.assertRaises(
            exception.InvalidConfiguration,
            driver._placement_storage_pool_info,
            self.client,
            'local-rootfs',
            25)

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
            'hypervisor_version': 7002,
            'disk_available_least': 1843,
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
                'server_version': '7.2',
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
    @mock.patch(
        'nova.virt.incus.driver.compute_utils.is_volume_backed_instance',
        return_value=False)
    def test_snapshot(self, is_bfv, lock, IMAGE_API):
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
    @mock.patch(
        'nova.virt.incus.driver.compute_utils.is_volume_backed_instance',
        return_value=False)
    def test_snapshot_upload_failure_cleans_temporary_resources(
            self, is_bfv, lock, IMAGE_API):
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

    @mock.patch(
        'nova.virt.incus.driver.compute_utils.is_volume_backed_instance',
        return_value=True)
    def test_snapshot_rejects_bfv_before_incus_side_effects(self, is_bfv):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.InvalidRequest,
            incus_driver.snapshot,
            ctx,
            instance,
            'image-id',
            mock.Mock())

        self.client.instances.get.assert_not_called()

    def _prepare_cold_revert_protocol(self, instance, container, remote):
        cleanup_token = '10000000-0000-0000-0000-000000000001'
        container.config = {'volatile.idmap.base': '1065536'}
        container.expanded_devices = {
            'root': {'type': 'disk', 'path': '/', 'pool': 'root'}}
        profile = self.client.profiles.get.return_value
        profile.config = {
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
            driver.MIGRATION_DESTINATION_KEY:
                'https://192.0.2.20:8443',
            'security.idmap.size': '65536',
        }
        self.client.storage_pools.get.return_value.driver = 'zfs'
        self._get_migration_attempt.return_value = {
            'state': 'aborted',
            'finished': True,
            'operation_uuid': '',
        }
        self.client.api.operations.get.return_value.json.return_value = {
            'metadata': {}}
        remote.instances.get.side_effect = incuscore_exceptions.NotFound(
            MockResponse(404))
        remote.profiles.get.side_effect = incuscore_exceptions.NotFound(
            MockResponse(404))
        return profile

    @mock.patch.object(driver, '_migration_client')
    def test_finish_revert_migration(self, migration_client):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = []

        container = mock.Mock()
        container.status = 'Stopped'
        self.client.instances.get.return_value = container
        self._prepare_cold_revert_protocol(
            instance, container, migration_client.return_value)
        migration = mock.Mock(
            source_compute='compute', dest_compute='compute')

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.finish_revert_migration(
            ctx, instance, network_info, migration)

        container.start.assert_called_once_with(wait=True)

    @mock.patch.object(driver, '_migration_client')
    def test_finish_revert_migration_replays_completion_marker(
            self, migration_client):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        container = mock.Mock(status='Running')
        self.client.instances.get.return_value = container
        profile = self._prepare_cold_revert_protocol(
            instance, container, migration_client.return_value)
        cleanup_token = profile.config[
            driver.MIGRATION_CLEANUP_TOKEN_KEY]
        profile.config[driver.MIGRATION_ROLLBACK_COMPLETE_KEY] = cleanup_token
        migration = mock.Mock(
            source_compute='source', dest_compute='destination')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._attach_volume = mock.Mock()
        incus_driver._refresh_vifs = mock.Mock()

        # Simulate a compute-process restart after the source was restored and
        # marked but before Nova observed the first return. Replaying the
        # manager callback must only finish the remote protocol.
        incus_driver.finish_revert_migration(
            ctx, instance, [mock.sentinel.vif], migration)
        incus_driver.finish_revert_migration(
            ctx, instance, [mock.sentinel.vif], migration)

        self.assertEqual(2, container.sync.call_count)
        self.assertEqual(2, self._retire_migration_attempt.call_count)
        self._get_migration_attempt.assert_not_called()
        incus_driver._attach_volume.assert_not_called()
        incus_driver._refresh_vifs.assert_not_called()
        container.start.assert_not_called()

    def test_get_vcpus_used_counts_only_nova_owned_records(self):
        owned = mock.Mock(
            name='owned',
            type='container',
            expanded_config={
                'user.openstack.uuid': '00000000-0000-0000-0000-000000000001',
                'limits.cpu': '4',
            })
        owned.name = 'instance-owned'
        stopped = mock.Mock(
            name='stopped',
            type='container',
            expanded_config={
                'user.openstack.uuid': '00000000-0000-0000-0000-000000000002',
                'limits.cpu': '2',
            })
        stopped.name = 'instance-stopped'
        foreign = mock.Mock(
            name='foreign',
            type='container',
            expanded_config={'limits.cpu': '32'})
        foreign.name = 'operator-container'
        malformed = mock.Mock(
            name='malformed',
            type='container',
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
        self.client.instances.all.assert_called_once_with(recursion=1)

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
    def test_power_off_refuses_a_console_until_the_guest_is_stopped(
            self, broker_factory):
        """The window between releasing and stopping is the whole leak.

        Closing the broker first and stopping afterwards left the guest
        Running with no broker registered, so a console request built a
        replacement that the imminent stop stranded on a proxy port.
        """
        self.CONF.serial_console.enabled = True
        self.CONF.serial_console.proxyclient_address = '192.0.2.10'
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-console')
        container = self.client.instances.get.return_value
        container.status = 'Running'
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client
        existing = mock.Mock()
        incus_driver._serial_consoles[instance.uuid] = existing
        raced = []

        def stop(*args, **kwargs):
            # A console request arriving while the stop is in flight.
            raced.append(self.assertRaises(
                exception.InstanceNotRunning,
                incus_driver.get_serial_console, None, instance))
            container.status = 'Stopped'

        container.stop.side_effect = stop

        incus_driver.power_off(instance)

        self.assertEqual(1, len(raced))
        existing.close.assert_called_once_with()
        broker_factory.assert_not_called()
        self.assertNotIn(instance.uuid, incus_driver._serial_consoles)
        # The refusal is lifted once the guest is actually stopped.
        self.assertNotIn(
            instance.uuid, incus_driver._serial_console_destroying)

    def test_power_off_lifts_the_console_refusal_after_failing(self):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-console')
        container = self.client.instances.get.return_value
        container.status = 'Running'
        container.stop.side_effect = RuntimeError('stop failed')
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertRaises(
            RuntimeError, incus_driver.power_off, instance)

        # A stop that did not happen must not block later consoles.
        self.assertNotIn(
            instance.uuid, incus_driver._serial_console_destroying)

    def test_live_migration_source_releases_its_console_broker(self):
        """The guest now runs elsewhere; this host's broker is dead.

        Keeping it held a proxy port, and handed a stale console to the
        next request if the instance ever migrated back.
        """
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client
        incus_driver.cleanup = mock.Mock()
        broker = mock.Mock()
        incus_driver._serial_consoles[instance.uuid] = broker

        incus_driver.post_live_migration_at_source(ctx, instance, [_VIF])

        broker.close.assert_called_once_with()
        self.assertNotIn(instance.uuid, incus_driver._serial_consoles)

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

    @mock.patch.object(driver.incus_console, 'SerialConsoleBroker')
    def test_get_serial_console_serializes_concurrent_broker_creation(
            self, broker_factory):
        self.CONF.serial_console.enabled = True
        self.CONF.serial_console.proxyclient_address = '192.0.2.10'
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-console')
        container = self.client.instances.get.return_value
        container.status = 'Running'
        broker = mock.Mock(port=10001)

        def create_broker(*args):
            # Yield the GIL so an implementation without registry locking
            # enters the constructor twice.
            time.sleep(0.05)
            return broker

        broker_factory.side_effect = create_broker
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client
        barrier = threading.Barrier(3)
        results = []
        errors = []

        def get_console():
            barrier.wait()
            try:
                results.append(
                    incus_driver.get_serial_console(None, instance))
            except Exception as exc:
                errors.append(exc)

        workers = [
            threading.Thread(target=get_console),
            threading.Thread(target=get_console),
        ]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=5)

        self.assertFalse(errors)
        self.assertEqual(2, len(results))
        broker_factory.assert_called_once_with('192.0.2.10', container)
        self.assertTrue(all(not worker.is_alive() for worker in workers))

    def test_get_serial_console_rejects_destroy_in_progress(self):
        self.CONF.serial_console.enabled = True
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-console')
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client
        incus_driver._serial_console_destroying.add(instance.uuid)

        self.assertRaises(
            exception.InstanceNotRunning,
            incus_driver.get_serial_console, None, instance)

        self.client.instances.get.assert_not_called()

    def test_cleanup_host_closes_every_serial_console_broker(self):
        incus_driver = driver.IncusDriver(None)
        first = mock.Mock()
        first.close.side_effect = RuntimeError('close failed')
        second = mock.Mock()
        incus_driver._serial_consoles = {
            'first': first,
            'second': second,
        }
        incus_driver._serial_console_destroying.add('first')

        incus_driver.cleanup_host(None)

        first.close.assert_called_once_with()
        second.close.assert_called_once_with()
        self.assertEqual({}, incus_driver._serial_consoles)
        self.assertEqual(set(), incus_driver._serial_console_destroying)

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
    @mock.patch.object(driver.incus_privsep, 'mount')
    @mock.patch.object(driver.os, 'chmod')
    def test_mount_nfs_share_stages_incus_device(
            self, chmod, mount, ismount):
        ismount.side_effect = [False, False, True]
        driver.fileutils.ensure_tree.reset_mock()
        self.CONF.incus.enable_manila_shares = True
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
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
        chmod.assert_has_calls([
            mock.call(share_root, 0o711),
            mock.call(instance_root, 0o711),
            mock.call(mount_path, 0o700),
        ])
        mount.assert_called_once_with(
            'nfs', share.export_location, mount_path,
            ['rw', 'nosuid', 'nodev'],
            self.CONF.incus.share_mount_timeout)
        self.assertEqual({
            'type': 'disk',
            'source': mount_path,
            'path': '/mnt/manila/project-data',
            'readonly': 'false',
            'recursive': 'true',
        }, profile.devices[driver._share_device_name(share)])
        profile.save.assert_called_once_with(wait=True)

    @mock.patch.object(driver.os.path, 'ismount', return_value=False)
    @mock.patch.object(driver.incus_privsep, 'mount')
    @mock.patch.object(driver.os, 'chmod')
    def test_mount_cephfs_share_uses_private_secretfile(
            self, chmod, mount, ismount):
        self.CONF.incus.enable_manila_shares = True
        ismount.side_effect = [False, False, True]
        driver.fileutils.ensure_tree.side_effect = (
            lambda path, mode=0o755:
            os.makedirs(path, mode=mode, exist_ok=True))
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='mon1:/volumes/project',
            share_proto='CEPHFS',
            access_to='nova-client',
            access_key='secret')
        profile = self.client.profiles.get.return_value
        profile.devices = {}
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        incus_driver.mount_share(None, instance, share)

        args = mount.call_args.args
        self.assertEqual(
            ('ceph', share.export_location,
             driver._share_mount_path(instance, share)), args[:3])
        self.assertEqual(
            ['rw', 'nosuid', 'nodev', 'name=nova-client'],
            args[3][:4])
        secret_path = args[3][4].removeprefix('secretfile=')
        self.assertEqual(
            os.path.dirname(driver._share_mount_path(instance, share)),
            os.path.dirname(secret_path))
        self.assertFalse(os.path.exists(secret_path))
        self.assertEqual(self.CONF.incus.share_mount_timeout, args[4])

    @mock.patch.object(driver.os.path, 'ismount')
    @mock.patch.object(driver.incus_privsep, 'mount')
    @mock.patch.object(driver.os, 'chmod')
    @mock.patch.object(driver.lockutils, 'lock')
    def test_mount_share_holds_profile_lock_only_for_profile_update(
            self, lock, chmod, mount, ismount):
        self.CONF.incus.enable_manila_shares = True
        ismount.side_effect = [False, False, True]
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        profile = self.client.profiles.get.return_value
        profile.devices = {}
        profile_lock_name = driver._profile_lock_name(instance)
        profile_lock_active = [False]

        def fake_lock(name):
            context_manager = mock.MagicMock()
            if name == profile_lock_name:
                context_manager.__enter__.side_effect = (
                    lambda: profile_lock_active.__setitem__(0, True))
                context_manager.__exit__.side_effect = (
                    lambda *args: profile_lock_active.__setitem__(0, False))
            return context_manager

        lock.side_effect = fake_lock
        mount.side_effect = (
            lambda *args, **kwargs:
            self.assertFalse(profile_lock_active[0]))
        profile.save.side_effect = (
            lambda *args, **kwargs:
            self.assertTrue(profile_lock_active[0]))
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        incus_driver.mount_share(None, instance, share)

        mount.assert_called_once()
        profile.save.assert_called_once_with(wait=True)
        self.assertFalse(profile_lock_active[0])

    @mock.patch.object(driver.os.path, 'ismount', return_value=True)
    @mock.patch.object(driver.incus_privsep, 'mount')
    @mock.patch.object(driver.os, 'chmod')
    @mock.patch.object(driver, '_validate_existing_share_mount')
    def test_mount_nfs_share_does_not_chmod_existing_mount(
            self, validate_mount, chmod, mount, ismount):
        driver.fileutils.ensure_tree.reset_mock()
        self.CONF.incus.enable_manila_shares = True
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        profile = self.client.profiles.get.return_value
        profile.devices = {}
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        incus_driver.mount_share(None, instance, share)

        mount_path = driver._share_mount_path(instance, share)
        self.assertNotIn(mock.call(mount_path, 0o700), chmod.call_args_list)
        mount.assert_not_called()
        validate_mount.assert_called_once_with(
            mount_path, share, mount_table=mock.ANY)
        self.assertEqual(
            mount_path,
            profile.devices[driver._share_device_name(share)]['source'])

    @mock.patch.object(driver, '_share_mount_table_index')
    @mock.patch.object(driver.os.path, 'ismount')
    @mock.patch.object(driver.incus_privsep, 'umount')
    @mock.patch.object(driver.incus_privsep, 'mount')
    @mock.patch.object(driver, '_ensure_share_mount_path')
    def test_mount_share_rolls_back_when_profile_is_not_created(
            self, ensure_path, mount, umount, ismount, mount_table):
        self.CONF.incus.enable_manila_shares = True
        mount_path = '/instances/incus-shares/instance/share'
        ensure_path.return_value = mount_path
        ismount.side_effect = [False, True]
        response = mock.Mock(status_code=404)
        self.client.profiles.get.side_effect = (
            incuscore_exceptions.LXDAPIException(response))
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        journal_mount_path = driver._share_mount_path(instance, share)
        mount_table.return_value = {
            journal_mount_path: {
                'device': share.export_location,
                'fstype': 'nfs',
                'opts': frozenset(('rw', 'nosuid', 'nodev')),
            },
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertRaises(
            exception.ShareMountError,
            incus_driver.mount_share, None, instance, share)

        mount.assert_called_once()
        umount.assert_called_once_with(
            driver._share_mount_path(instance, share),
            self.CONF.incus.share_unmount_timeout)
        self.assertFalse(os.path.exists(
            driver._share_journal_path(instance, share.share_id)))

    @mock.patch.object(driver.os.path, 'ismount')
    @mock.patch.object(driver.incus_privsep, 'umount')
    @mock.patch.object(driver.incus_privsep, 'mount')
    @mock.patch.object(driver, '_ensure_share_mount_path')
    def test_mount_share_profile_failure_retains_owned_mount(
            self, ensure_path, mount, umount, ismount):
        self.CONF.incus.enable_manila_shares = True
        mount_path = '/instances/incus-shares/instance/share'
        ensure_path.return_value = mount_path
        ismount.side_effect = [False, True]
        profile = mock.Mock(devices={})
        profile.save.side_effect = RuntimeError('profile write failed')
        persisted = mock.Mock(devices={})
        self.client.profiles.get.side_effect = [profile, persisted]
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertRaises(
            exception.ShareMountError,
            incus_driver.mount_share, None, instance, share)

        mount.assert_called_once()
        umount.assert_not_called()
        self.assertTrue(os.path.exists(
            driver._share_journal_path(instance, share.share_id)))

    @mock.patch.object(driver.os.path, 'ismount', return_value=False)
    @mock.patch.object(driver.incus_privsep, 'mount')
    @mock.patch.object(driver.os, 'chmod')
    def test_live_migration_share_stage_does_not_require_profile(
            self, chmod, mount, ismount):
        self.CONF.incus.enable_manila_shares = True
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        self.client.profiles.get.side_effect = (
            incuscore_exceptions.LXDAPIException(MockResponse(404)))
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertTrue(incus_driver.stage_share_for_live_migration(
            None, instance, share,
            '20000000-0000-0000-0000-000000000002'))

        mount.assert_called_once()
        self.client.profiles.get.assert_not_called()

    @mock.patch.object(driver, '_retire_migration_attempt')
    @mock.patch.object(
        driver, '_abort_migration_attempt',
        return_value={'state': 'aborted', 'finished': True})
    @mock.patch.object(driver.os.path, 'ismount', return_value=False)
    @mock.patch.object(
        driver.incus_privsep, 'mount',
        side_effect=RuntimeError('first NFS mount failed'))
    def test_cold_first_mount_failure_releases_token_for_retry(
            self, mount, ismount, abort_attempt, retire_attempt):
        self.CONF.incus.enable_manila_shares = True
        driver.fileutils.ensure_tree.side_effect = (
            lambda path, mode=0o755:
            os.makedirs(path, mode=mode, exist_ok=True))
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        first_token = '20000000-0000-0000-0000-000000000002'
        second_token = '30000000-0000-0000-0000-000000000003'
        disk_info = migration_disk_info({
            'format': 'incus-pull-v1',
            'cleanup_token': first_token,
            'migration_data': {},
        })
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client
        not_found = incus_api_exception(404, 'not found')
        self.client.instances.get.side_effect = not_found
        self.client.profiles.get.side_effect = not_found

        first_error = self.assertRaises(
            exception.ShareMountError,
            incus_driver.stage_share_for_cold_migration,
            None, instance, share, first_token)
        self.assertIn('first NFS mount failed', str(first_error))
        self.assertIsNotNone(driver._read_share_journal(
            instance, share, operation_token=first_token))

        self.assertFalse(
            incus_driver.rollback_cold_migration_preparation(
                None, instance, disk_info))

        self.assertEqual([], driver._share_journal_records(instance))
        abort_args, abort_kwargs = abort_attempt.call_args
        self.assertEqual(
            (self.client, instance, first_token, 1065536, 65536),
            abort_args)
        self.assertEqual({}, abort_kwargs)
        retire_attempt.assert_called_once_with(
            self.client, instance, first_token, 1065536, 65536)

        mount.side_effect = None
        self.assertTrue(incus_driver.stage_share_for_cold_migration(
            None, instance, share, second_token))
        self.assertEqual(
            second_token,
            driver._read_share_journal(
                instance, share,
                operation_token=second_token)['operation_token'])
        incus_driver.unstage_share_for_cold_migration(
            None, instance, share, second_token)
        self.assertEqual([], driver._share_journal_records(instance))

    @mock.patch.object(driver, '_retire_migration_attempt')
    @mock.patch.object(
        driver, '_abort_migration_attempt',
        side_effect=incus_api_exception(404, 'attempt not registered'))
    @mock.patch.object(driver.os.path, 'ismount', return_value=False)
    @mock.patch.object(
        driver.incus_privsep, 'mount',
        side_effect=RuntimeError('first NFS mount failed'))
    def test_live_first_mount_failure_cleanup_allows_new_token(
            self, mount, ismount, abort_attempt, retire_attempt):
        self.CONF.incus.enable_manila_shares = True
        driver.fileutils.ensure_tree.side_effect = (
            lambda path, mode=0o755:
            os.makedirs(path, mode=mode, exist_ok=True))
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        first_token = '20000000-0000-0000-0000-000000000002'
        second_token = '30000000-0000-0000-0000-000000000003'
        data = migrate_data.IncusLiveMigrateData(
            cleanup_token=first_token,
            idmap_base=1065536,
            idmap_size=65536)
        not_found = incus_api_exception(404, 'not found')
        self.client.instances.get.side_effect = not_found
        self.client.profiles.get.side_effect = not_found
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        first_error = self.assertRaises(
            exception.ShareMountError,
            incus_driver.stage_share_for_live_migration,
            None, instance, share, first_token)
        self.assertIn('first NFS mount failed', str(first_error))

        self.assertTrue(
            incus_driver.cleanup_pre_live_migration_destination(
                None, instance, data))
        self.assertEqual([], driver._share_journal_records(instance))
        abort_attempt.assert_called_once_with(
            self.client, instance, first_token, 1065536, 65536,
            target_cleanup=mock.ANY)
        retire_attempt.assert_not_called()

        mount.side_effect = None
        self.assertTrue(incus_driver.stage_share_for_live_migration(
            None, instance, share, second_token))
        incus_driver.unstage_share_for_live_migration(
            None, instance, share, second_token)
        self.assertEqual([], driver._share_journal_records(instance))

    @mock.patch.object(driver, '_retire_migration_attempt')
    @mock.patch.object(driver, '_cleanup_share_journal_mounts')
    @mock.patch.object(driver, '_abort_migration_attempt')
    def test_cold_failed_finish_marker_error_retains_share_transaction(
            self, abort_attempt, cleanup_journals, retire_attempt):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        token = '20000000-0000-0000-0000-000000000002'
        disk_info = migration_disk_info({
            'format': 'incus-pull-v1',
            'cleanup_token': token,
            'migration_data': {},
            'was_running': True,
        })
        container = mock.Mock(config={
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_RECEIVE_COMPLETE_KEY: 'true',
        })
        self.client.instances.get.return_value = container
        self.client.profiles.get.return_value = mock.Mock(config={
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
            driver.MIGRATION_DESTINATION_PREPARED_KEY: token,
            driver.MIGRATION_NOVA_UUID_KEY: token,
            'security.idmap.base': '1065536',
            'security.idmap.size': '65536',
        })
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client
        incus_driver._mark_migration_recovery_required = mock.Mock(
            side_effect=RuntimeError('profile save failed'))

        self.assertTrue(
            incus_driver.rollback_cold_migration_preparation(
                None, instance, disk_info))

        incus_driver._mark_migration_recovery_required.assert_called_once_with(
            instance, power_on=True)
        abort_attempt.assert_not_called()
        cleanup_journals.assert_not_called()
        retire_attempt.assert_not_called()

    @mock.patch.object(driver.IncusDriver, '_acknowledge_cleanup_profile')
    @mock.patch.object(driver.IncusDriver, '_cleanup')
    def test_cold_profile_only_rollback_is_cleaned_and_acknowledged(
            self, cleanup, acknowledge):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001')
        instance.name = 'instance-share'
        token = '20000000-0000-0000-0000-000000000002'
        disk_info = migration_disk_info({
            'format': 'incus-pull-v1',
            'cleanup_token': token,
            'migration_data': {},
        })
        profile = mock.Mock(config={
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
            driver.MIGRATION_DESTINATION_PREPARED_KEY: token,
            driver.MIGRATION_NOVA_UUID_KEY: token,
            'security.idmap.base': '1065536',
            'security.idmap.size': '65536',
        })
        self.client.instances.get.side_effect = incus_api_exception(
            404, 'not found')
        self.client.profiles.get.return_value = profile
        network_info = mock.sentinel.network_info
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client
        incus_driver.network_api = mock.Mock()
        incus_driver.network_api.get_instance_nw_info.return_value = (
            network_info)

        self.assertTrue(incus_driver.rollback_cold_migration_preparation(
            mock.sentinel.context, instance, disk_info))

        cleanup.assert_called_once_with(
            mock.sentinel.context, instance, network_info,
            block_device_info=None, destroy_vifs=True,
            delete_profile=False)
        acknowledge.assert_called_once_with(instance, token)

    @mock.patch.object(driver, '_retire_migration_attempt')
    @mock.patch.object(driver, '_cleanup_share_journal_mounts')
    @mock.patch.object(
        driver, '_abort_migration_attempt',
        return_value={'state': 'committed', 'finished': True})
    def test_cold_failed_finish_committed_attempt_retains_shares(
            self, abort_attempt, cleanup_journals, retire_attempt):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001')
        instance.name = 'instance-share'
        token = '20000000-0000-0000-0000-000000000002'
        disk_info = migration_disk_info({
            'format': 'incus-pull-v1',
            'cleanup_token': token,
            'migration_data': {},
        })
        not_found = incus_api_exception(404, 'not found')
        self.client.instances.get.side_effect = not_found
        self.client.profiles.get.side_effect = not_found
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertTrue(
            incus_driver.rollback_cold_migration_preparation(
                None, instance, disk_info))

        abort_attempt.assert_called_once_with(
            self.client, instance, token, 1065536, 65536)
        cleanup_journals.assert_not_called()
        retire_attempt.assert_not_called()

    @mock.patch.object(driver, '_cleanup_share_journal_mounts')
    @mock.patch.object(driver, '_abort_migration_attempt')
    def test_cold_failed_finish_unknown_target_state_fails_closed(
            self, abort_attempt, cleanup_journals):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001')
        instance.name = 'instance-share'
        disk_info = migration_disk_info({
            'format': 'incus-pull-v1',
            'migration_data': {},
        })
        self.client.instances.get.side_effect = incus_api_exception(
            503, 'Incus unavailable')
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertRaises(
            incuscore_exceptions.LXDAPIException,
            incus_driver.rollback_cold_migration_preparation,
            None, instance, disk_info)

        self.client.profiles.get.assert_not_called()
        abort_attempt.assert_not_called()
        cleanup_journals.assert_not_called()

    def test_share_journal_never_persists_cephfs_secret(self):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='mon1:/volumes/project',
            share_proto='CEPHFS',
            access_to='nova-client',
            access_key='do-not-write-this-secret')
        token = '20000000-0000-0000-0000-000000000002'

        driver._write_share_journal(
            instance, share, token, 'staging')

        path = driver._share_journal_path(instance, share.share_id)
        with open(path, encoding='utf-8') as stream:
            serialized = stream.read()
        self.assertNotIn('do-not-write-this-secret', serialized)
        self.assertNotIn('access_key', serialized)
        payload = jsonutils.loads(serialized)
        self.assertEqual(token, payload['operation_token'])
        self.assertEqual(instance.uuid, payload['instance_uuid'])
        self.assertEqual(instance.name, payload['instance_name'])

    def test_share_journal_rejects_different_migration_owner(self):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        driver._write_share_journal(
            instance, share,
            '20000000-0000-0000-0000-000000000002', 'mounted')

        self.assertRaises(
            exception.ShareMountError,
            driver._read_share_journal,
            instance, share,
            operation_token='30000000-0000-0000-0000-000000000003')

    def test_share_journal_write_rejects_different_owner(self):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        first_token = '20000000-0000-0000-0000-000000000002'
        second_token = '30000000-0000-0000-0000-000000000003'
        driver._write_share_journal(
            instance, share, first_token, 'mounted')

        self.assertRaises(
            exception.ShareMountError,
            driver._write_share_journal,
            instance, share, second_token, 'unmounting')

        payload = driver._read_share_journal(
            instance, share, operation_token=first_token)
        self.assertEqual('mounted', payload['phase'])

    def test_share_journal_write_rejects_changed_binding(self):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        changed = mock.Mock(
            share_id=share.share_id,
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/different',
            share_proto='NFS')
        token = '20000000-0000-0000-0000-000000000002'
        driver._write_share_journal(
            instance, share, token, 'mounted')

        self.assertRaises(
            exception.ShareMountError,
            driver._write_share_journal,
            instance, changed, token, 'unmounting')

        payload = driver._read_share_journal(
            instance, share, operation_token=token)
        self.assertEqual(share.export_location, payload['export_location'])
        self.assertEqual('mounted', payload['phase'])

    def test_share_journal_write_allows_same_owner_phase_update(self):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        token = '20000000-0000-0000-0000-000000000002'
        driver._write_share_journal(
            instance, share, token, 'staging')

        driver._write_share_journal(
            instance, share, token, 'mounted')

        payload = driver._read_share_journal(
            instance, share, operation_token=token)
        self.assertEqual('mounted', payload['phase'])

    def test_share_journal_lock_serializes_competing_owners(self):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        tokens = (
            '20000000-0000-0000-0000-000000000002',
            '30000000-0000-0000-0000-000000000003')
        barrier = threading.Barrier(3)
        outcomes = []

        def write(token):
            barrier.wait()
            try:
                with driver.lockutils.lock(
                        driver._share_operation_lock_name(
                            instance, share.share_id)):
                    driver._write_share_journal(
                        instance, share, token, 'staging')
            except Exception as exc:
                outcomes.append(exc)
            else:
                outcomes.append(None)

        workers = [threading.Thread(target=write, args=(token,))
                   for token in tokens]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(5)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(1, sum(outcome is None for outcome in outcomes))
        failures = [outcome for outcome in outcomes
                    if outcome is not None]
        self.assertEqual(1, len(failures))
        self.assertIsInstance(failures[0], exception.ShareMountError)
        payload = driver._read_share_journal(instance, share)
        self.assertIn(payload['operation_token'], tokens)

    @mock.patch.object(driver.psutil, 'disk_partitions')
    def test_journaled_share_mount_table_is_indexed_once_at_scale(
            self, disk_partitions):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        token = '20000000-0000-0000-0000-000000000002'
        for cardinality in (32, 64):
            with self.subTest(cardinality=cardinality):
                shares = [
                    mock.Mock(
                        share_id=(
                            '10000000-0000-0000-0000-%012d' % index),
                        instance_uuid=instance.uuid,
                        tag='share-%d' % index,
                        export_location='server:/share-%d' % index,
                        share_proto='NFS')
                    for index in range(cardinality)
                ]
                for share in shares:
                    driver._write_share_journal(
                        instance, share, token, 'mounted')
                disk_partitions.return_value = [
                    mock.Mock(
                        mountpoint=driver._share_mount_path(instance, share),
                        device=share.export_location,
                        fstype='nfs4', opts='rw,nosuid,nodev')
                    for share in shares
                ]
                disk_partitions.reset_mock()

                mappings = driver._journaled_share_mappings(
                    instance, token,
                    expected_share_ids=[share.share_id for share in shares])

                self.assertEqual(cardinality, len(mappings))
                disk_partitions.assert_called_once_with(all=True)
                for share in shares:
                    driver._remove_share_journal(
                        instance, share.share_id, token)

    def test_share_journal_candidates_exclude_ordinary_attach_owner(self):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        migration_share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid, tag='migration',
            export_location='server:/migration', share_proto='NFS')
        ordinary_share = mock.Mock(
            share_id='30000000-0000-0000-0000-000000000003',
            instance_uuid=instance.uuid, tag='ordinary',
            export_location='server:/ordinary', share_proto='NFS', id=7)
        token = '20000000-0000-0000-0000-000000000002'
        driver._write_share_journal(
            instance, migration_share, token, 'mounted')
        driver._write_share_journal(
            instance, ordinary_share,
            driver._share_mapping_owner_token(instance, ordinary_share),
            'mounted')

        candidates = driver._share_journal_recovery_candidates()

        self.assertEqual([{
            'uuid': instance.uuid,
            'name': instance.name,
            'operation_token': token,
            'share_ids': (migration_share.share_id,),
        }], candidates)

    @mock.patch.object(driver, '_cleanup_share_journal_mounts')
    def test_recover_share_journal_rechecks_local_runtime_absence(
            self, cleanup):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid, tag='migration',
            export_location='server:/migration', share_proto='NFS')
        token = '20000000-0000-0000-0000-000000000002'
        driver._write_share_journal(instance, share, token, 'mounted')
        not_found = incus_api_exception(404, 'not found')
        self.client.instances.get.side_effect = not_found
        self.client.profiles.get.side_effect = not_found
        candidate = {
            'uuid': instance.uuid,
            'name': instance.name,
            'operation_token': token,
            'share_ids': (share.share_id,),
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertTrue(incus_driver.recover_share_journal_candidate(
            instance, candidate))

        cleanup.assert_called_once_with(
            instance, operation_token=token)

    @mock.patch.object(driver, '_cleanup_share_journal_mounts')
    def test_recover_share_journal_refuses_existing_profile(self, cleanup):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid, tag='migration',
            export_location='server:/migration', share_proto='NFS')
        token = '20000000-0000-0000-0000-000000000002'
        driver._write_share_journal(instance, share, token, 'mounted')
        self.client.instances.get.side_effect = incus_api_exception(
            404, 'not found')
        self.client.profiles.get.return_value = mock.Mock()
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertRaises(
            exception.MigrationError,
            incus_driver.recover_share_journal_candidate,
            instance, {
                'uuid': instance.uuid,
                'name': instance.name,
                'operation_token': token,
                'share_ids': (share.share_id,),
            })

        cleanup.assert_not_called()

    @mock.patch.object(driver.os.path, 'ismount', return_value=True)
    def test_share_profile_save_precedes_journal_removal(self, ismount):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        token = '20000000-0000-0000-0000-000000000002'
        driver._write_share_journal(
            instance, share, token, 'mounted')
        profile = mock.Mock(devices={})
        profile.save.side_effect = RuntimeError('profile write failed')
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertRaises(
            RuntimeError,
            incus_driver._attach_share_devices,
            profile, instance, [share], operation_token=token)
        self.assertTrue(os.path.exists(
            driver._share_journal_path(instance, share.share_id)))

        profile.save.side_effect = None
        incus_driver._attach_share_devices(
            profile, instance, [share], operation_token=token)
        self.assertFalse(os.path.exists(
            driver._share_journal_path(instance, share.share_id)))

    @mock.patch.object(driver.os.path, 'ismount', return_value=True)
    def test_share_profile_save_accepts_persisted_device(self, ismount):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        token = '20000000-0000-0000-0000-000000000002'
        driver._write_share_journal(
            instance, share, token, 'mounted')
        expected = {
            'type': 'disk',
            'source': driver._share_mount_path(instance, share),
            'path': '/mnt/manila/project-data',
            'readonly': 'false',
            'recursive': 'true',
        }
        profile = mock.Mock(devices={})
        profile.save.side_effect = incus_operation_exception(
            400, 'profile change still saved')
        persisted = mock.Mock(
            devices={driver._share_device_name(share): expected})
        self.client.profiles.get.return_value = persisted
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        incus_driver._attach_share_devices(
            profile, instance, [share], operation_token=token)

        self.assertFalse(os.path.exists(
            driver._share_journal_path(instance, share.share_id)))

    @mock.patch.object(driver.os.path, 'ismount', return_value=True)
    @mock.patch.object(driver, '_validate_existing_share_mount')
    def test_attach_journaled_shares_requires_exact_set(
            self, validate_mount, ismount):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        token = '20000000-0000-0000-0000-000000000002'
        shares = [
            mock.Mock(
                share_id=share_id,
                instance_uuid=instance.uuid,
                tag='project-%d' % index,
                export_location='server:/project-%d' % index,
                share_proto='NFS')
            for index, share_id in enumerate((
                '10000000-0000-0000-0000-000000000001',
                '30000000-0000-0000-0000-000000000003',
            ))
        ]
        for share in shares:
            driver._write_share_journal(
                instance, share, token, 'mounted')
        profile = mock.Mock(devices={})
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        attached = incus_driver._attach_journaled_share_devices(
            profile, instance, token,
            [share.share_id for share in shares])

        self.assertEqual(
            [share.share_id for share in shares],
            [share.share_id for share in attached])
        profile.save.assert_called_once_with(wait=True)
        self.assertEqual([], driver._share_journal_records(instance))

    @mock.patch.object(
        driver.incus_privsep, 'umount',
        side_effect=RuntimeError('share is busy'))
    @mock.patch.object(driver, '_share_mount_table_index')
    @mock.patch.object(driver.os.path, 'ismount', return_value=True)
    def test_share_umount_failure_retains_journal(
            self, ismount, mount_table, umount):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        token = '20000000-0000-0000-0000-000000000002'
        mount_path = driver._share_mount_path(instance, share)
        mount_table.return_value = {
            mount_path: {
                'device': share.export_location,
                'fstype': 'nfs',
                'opts': frozenset(('rw', 'nosuid', 'nodev')),
            },
        }
        driver._write_share_journal(
            instance, share, token, 'mounted')
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertRaises(
            exception.ShareUmountError,
            incus_driver.unstage_share_for_cold_migration,
            None, instance, share, token)

        payload = driver._read_share_journal(
            instance, share, operation_token=token)
        self.assertEqual('unmounting', payload['phase'])
        umount.assert_called_once_with(
            mount_path, self.CONF.incus.share_unmount_timeout)

    def test_prepare_cold_migration_share_info_binds_ids_only(self):
        token = '20000000-0000-0000-0000-000000000002'
        disk_info = migration_disk_info({
            'format': 'incus-pull-v1',
            'cleanup_token': token,
            'migration_data': {},
        })
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid='00000000-0000-0000-0000-000000000003',
            access_key='must-not-cross-the-rpc')

        prepared, actual_token = (
            driver.prepare_cold_migration_share_info(
                disk_info, [share]))

        self.assertEqual(token, actual_token)
        payload = jsonutils.loads(prepared)
        self.assertEqual(
            [share.share_id], payload['manila_share_ids'])
        self.assertNotIn('must-not-cross-the-rpc', prepared)

    def test_cleanup_ack_rejects_retained_share_device(self):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        token = '20000000-0000-0000-0000-000000000002'
        self.client.instances.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
        }
        profile.devices = {
            'manila-10000000-0000-0000-0000-000000000001': {
                'type': 'disk',
                'source': '/missing',
            },
        }
        profile.used_by = []
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertRaises(
            exception.MigrationError,
            incus_driver._acknowledge_cleanup_profile,
            instance, token)

        profile.save.assert_not_called()

    def test_cleanup_ack_rejects_retained_share_journal(self):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        token = '20000000-0000-0000-0000-000000000002'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        driver._write_share_journal(
            instance, share, token, 'unmounting')
        self.client.instances.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: token,
        }
        profile.devices = {}
        profile.used_by = []
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertRaises(
            exception.MigrationError,
            incus_driver._acknowledge_cleanup_profile,
            instance, token)

        profile.save.assert_not_called()

    @mock.patch.object(driver.psutil, 'disk_partitions')
    @mock.patch.object(
        driver.os.path, 'realpath', side_effect=lambda path: path)
    def test_existing_share_mount_rejects_wrong_export(
            self, realpath, disk_partitions):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001')
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            export_location='server:/expected',
            share_proto='NFS')
        mount_path = driver._share_mount_path(instance, share)
        disk_partitions.return_value = [mock.Mock(
            mountpoint=mount_path,
            device='server:/other',
            fstype='nfs4', opts='rw,nosuid,nodev')]

        self.assertRaises(
            exception.ShareMountError,
            driver._validate_existing_share_mount, mount_path, share)

    @mock.patch.object(driver.psutil, 'disk_partitions')
    @mock.patch.object(
        driver.os.path, 'realpath', side_effect=lambda path: path)
    def test_existing_share_mount_accepts_nfs4_export(
            self, realpath, disk_partitions):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001')
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            export_location='server:/expected/',
            share_proto='NFS')
        mount_path = driver._share_mount_path(instance, share)
        disk_partitions.return_value = [mock.Mock(
            mountpoint=mount_path,
            device='server:/expected',
            fstype='nfs4', opts='rw,nosuid,nodev')]

        driver._validate_existing_share_mount(mount_path, share)

    @mock.patch.object(driver.psutil, 'disk_partitions')
    def test_existing_share_mount_rejects_unsafe_options(
            self, disk_partitions):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001')
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            export_location='server:/expected',
            share_proto='NFS')
        mount_path = driver._share_mount_path(instance, share)
        disk_partitions.return_value = [mock.Mock(
            mountpoint=mount_path,
            device='server:/expected',
            fstype='nfs4', opts='rw,suid,dev')]

        error = self.assertRaises(
            exception.ShareMountError,
            driver._validate_existing_share_mount, mount_path, share)

        self.assertIn('rw,nosuid,nodev', str(error))

    def test_share_mount_path_rejects_noncanonical_instance_uuid(self):
        instance = mock.Mock(uuid='not-a-uuid')
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001')

        self.assertRaises(
            exception.ShareMountError,
            driver._share_mount_path, instance, share)

    def test_existing_share_mount_rejects_symlink_escape(self):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001')
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            export_location='server:/expected',
            share_proto='NFS')
        mount_path = driver._share_mount_path(instance, share)
        realpath = driver.os.path.realpath

        def fake_realpath(path):
            if os.path.abspath(path) == os.path.abspath(mount_path):
                return os.path.join(self.CONF.instances_path, 'escaped')
            return realpath(path)

        with mock.patch.object(
                driver.os.path, 'realpath', side_effect=fake_realpath):
            self.assertRaises(
                exception.ShareMountError,
                driver._validate_existing_share_mount,
                mount_path, share, mount_table={})

    @mock.patch.object(driver, '_share_mount_table_index')
    @mock.patch.object(driver.os.path, 'ismount', return_value=True)
    @mock.patch.object(driver.incus_privsep, 'umount')
    @mock.patch.object(driver.os, 'rmdir')
    def test_umount_share_removes_device_before_host_mount(
            self, rmdir, umount, ismount, mount_table):
        self.CONF.incus.enable_manila_shares = True
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        mount_path = driver._share_mount_path(instance, share)
        mount_table.return_value = {
            mount_path: {
                'device': share.export_location,
                'fstype': 'nfs',
                'opts': frozenset(('rw', 'nosuid', 'nodev')),
            },
        }
        profile = self.client.profiles.get.return_value
        profile.devices = {
            driver._share_device_name(share): {'type': 'disk'}}
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertFalse(incus_driver.umount_share(None, instance, share))

        self.assertNotIn(driver._share_device_name(share), profile.devices)
        profile.save.assert_called_once_with(wait=True)
        umount.assert_called_once_with(
            driver._share_mount_path(instance, share),
            self.CONF.incus.share_unmount_timeout)

    @mock.patch.object(driver, '_share_mount_table_index')
    @mock.patch.object(driver.os.path, 'isdir', return_value=True)
    @mock.patch.object(driver.os.path, 'ismount', return_value=True)
    @mock.patch.object(driver.incus_privsep, 'umount')
    @mock.patch.object(driver.os, 'rmdir')
    def test_umount_share_keeps_parent_with_other_share(
            self, rmdir, umount, ismount, isdir, mount_table):
        self.CONF.incus.enable_manila_shares = True
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        mount_path = driver._share_mount_path(instance, share)
        mount_table.return_value = {
            mount_path: {
                'device': share.export_location,
                'fstype': 'nfs',
                'opts': frozenset(('rw', 'nosuid', 'nodev')),
            },
        }
        profile = self.client.profiles.get.return_value
        profile.devices = {
            driver._share_device_name(share): {'type': 'disk'}}
        rmdir.side_effect = [
            None,
            OSError(errno.ENOTEMPTY, 'Directory not empty'),
            None,
        ]
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client

        self.assertFalse(incus_driver.umount_share(None, instance, share))

        self.assertEqual(3, rmdir.call_count)
        umount.assert_called_once_with(
            driver._share_mount_path(instance, share),
            self.CONF.incus.share_unmount_timeout)

    @mock.patch.object(driver, '_share_mount_table_index')
    @mock.patch.object(driver.os.path, 'isdir', return_value=True)
    @mock.patch.object(driver.os.path, 'ismount', return_value=True)
    @mock.patch.object(driver.incus_privsep, 'umount')
    @mock.patch.object(driver.os, 'rmdir')
    def test_umount_share_reports_parent_removal_error(
            self, rmdir, umount, ismount, isdir, mount_table):
        self.CONF.incus.enable_manila_shares = True
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001',
            name='instance-share')
        instance.name = 'instance-share'
        share = mock.Mock(
            share_id='10000000-0000-0000-0000-000000000001',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        mount_path = driver._share_mount_path(instance, share)
        mount_table.return_value = {
            mount_path: {
                'device': share.export_location,
                'fstype': 'nfs',
                'opts': frozenset(('rw', 'nosuid', 'nodev')),
            },
        }
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

    @mock.patch.object(driver.os.path, 'isdir', return_value=True)
    @mock.patch.object(driver, '_share_mount_table_index')
    @mock.patch.object(driver.incus_privsep, 'umount')
    @mock.patch.object(driver.os, 'rmdir')
    def test_cleanup_profile_share_mounts_attempts_all_after_failure(
            self, rmdir, umount, mount_table, isdir):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001')
        profile = mock.Mock()
        share_ids = [
            '10000000-0000-0000-0000-000000000001',
            '20000000-0000-0000-0000-000000000002',
            '30000000-0000-0000-0000-000000000003',
        ]
        profile.devices = {
            'manila-' + share_id: {
                'type': 'disk',
                'source': os.path.join(
                    self.CONF.instances_path, 'incus-shares',
                    instance.uuid, share_id),
            }
            for share_id in share_ids
        }
        mount_table.return_value = {
            os.path.realpath(device['source']): {
                'device': 'server:/%s' % name,
                'fstype': 'nfs',
                'opts': frozenset(('rw', 'nosuid', 'nodev')),
            }
            for name, device in profile.devices.items()
        }
        umount.side_effect = [RuntimeError('first failed'), None, None]
        rmdir.side_effect = OSError(errno.ENOTEMPTY, 'not empty')

        self.assertRaises(
            exception.ShareUmountError,
            driver._cleanup_profile_share_mounts, profile, instance)

        self.assertEqual(3, umount.call_count)

    @mock.patch.object(driver, '_share_mount_table_index')
    @mock.patch.object(driver.incus_privsep, 'umount')
    def test_cleanup_profile_share_mounts_reports_malformed_after_safe_cleanup(
            self, umount, mount_table):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001')
        share_id = '10000000-0000-0000-0000-000000000001'
        mount_path = os.path.realpath(os.path.join(
            self.CONF.instances_path, 'incus-shares',
            instance.uuid, share_id))
        valid_name = 'manila-' + share_id
        invalid_name = 'manila-not-a-uuid'
        profile = mock.Mock(devices={
            valid_name: {
                'type': 'disk',
                'source': mount_path,
            },
            invalid_name: {
                'type': 'disk',
                'source': '/must/not/unmount',
            },
        })
        mount_table.return_value = {
            mount_path: {
                'device': 'server:/project-data',
                'fstype': 'nfs',
                'opts': frozenset(('rw', 'nosuid', 'nodev')),
            },
        }

        raised = self.assertRaises(
            exception.ShareUmountError,
            driver._cleanup_profile_share_mounts, profile, instance)

        self.assertIn(invalid_name, str(raised))
        self.assertIn('malformed Incus Manila profile device', str(raised))
        umount.assert_called_once_with(
            mount_path, self.CONF.incus.share_unmount_timeout)
        profile.save.assert_called_once_with(wait=True)
        self.assertNotIn(valid_name, profile.devices)
        self.assertIn(invalid_name, profile.devices)

    def test_profile_share_mount_inventory_reports_each_malformed_shape(self):
        instance = mock.Mock(
            uuid='00000000-0000-0000-0000-000000000001')
        share_ids = [
            '%08d-0000-0000-0000-000000000001' % number
            for number in range(1, 6)
        ]
        valid_path = os.path.realpath(os.path.join(
            self.CONF.instances_path, 'incus-shares',
            instance.uuid, share_ids[0]))
        profile = mock.Mock(devices={
            'manila-' + share_ids[0]: {
                'type': 'disk', 'source': valid_path},
            'manila-not-a-uuid': {
                'type': 'disk', 'source': '/untrusted'},
            'manila-' + share_ids[1]: [],
            'manila-' + share_ids[2]: {
                'type': 'unix-block', 'source': '/untrusted'},
            'manila-' + share_ids[3]: {'type': 'disk'},
            'manila-' + share_ids[4]: {
                'type': 'disk', 'source': '/untrusted'},
        })

        mounts, malformed = driver._profile_share_mount_inventory(
            profile, instance)

        self.assertEqual([valid_path], mounts)
        self.assertEqual({
            'manila-not-a-uuid': 'device name does not contain a UUID',
            'manila-' + share_ids[1]:
                'device configuration is not a mapping',
            'manila-' + share_ids[2]: 'device type is not disk',
            'manila-' + share_ids[3]: 'device source is missing',
            'manila-' + share_ids[4]:
                'device source is outside its Nova staging directory',
        }, dict(malformed))

    @mock.patch.object(driver, '_share_mount_table_index')
    @mock.patch.object(driver.incus_privsep, 'umount')
    @mock.patch.object(driver.os, 'rmdir')
    def test_post_live_migration_source_retains_malformed_share_profile(
            self, rmdir, umount, mount_table):
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
        mount_table.return_value = {
            os.path.realpath(mount_path): {
                'device': 'server:/project-data',
                'fstype': 'nfs',
                'opts': frozenset(('rw', 'nosuid', 'nodev')),
            },
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client
        incus_driver._cleanup = mock.Mock()

        incus_driver.post_live_migration_at_source(
            None, instance, mock.sentinel.network_info)

        umount.assert_called_once_with(
            os.path.realpath(mount_path),
            self.CONF.incus.share_unmount_timeout)
        incus_driver._cleanup.assert_called_once_with(
            None, instance, mock.sentinel.network_info,
            delete_profile=False)
        self.assertNotIn('manila-' + share_id, profile.devices)
        self.assertIn('manila-not-a-uuid', profile.devices)
        self.vif_driver.plug.assert_not_called()
        self.vif_driver.unplug.assert_not_called()

    @mock.patch.object(driver, '_migration_client')
    def test_finish_revert_migration_refreshes_retained_vif(
            self, migration_client):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        vif = mock.Mock()
        container = mock.Mock(status='Stopped')
        self.client.instances.get.return_value = container
        self._prepare_cold_revert_protocol(
            instance, container, migration_client.return_value)
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

    @mock.patch.object(driver, '_migration_client')
    @mock.patch.object(driver, '_require_bfv_migration_support')
    @mock.patch.object(driver, '_boot_from_volume')
    @mock.patch('nova.virt.driver.block_device_info_get_mapping')
    def test_finish_revert_migration_attaches_only_data_volumes(
            self, get_mapping, boot_from_volume, require_bfv,
            migration_client):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        container = mock.Mock(status='Stopped')
        self.client.instances.get.return_value = container
        self._prepare_cold_revert_protocol(
            instance, container, migration_client.return_value)
        migration = mock.Mock(
            uuid='40000000-0000-0000-0000-000000000004',
            source_compute='source', dest_compute='destination')
        root_bdm = {
            'boot_index': 0,
            'connection_info': mock.sentinel.root_connection,
            'mount_device': '/dev/sda',
        }
        data_connection = {'driver_volume_type': 'local'}
        data_bdm = {
            'boot_index': 1,
            'attachment_id':
                '30000000-0000-0000-0000-000000000003',
            'connection_info': data_connection,
            'mount_device': '/dev/vdb',
        }
        boot_from_volume.return_value = root_bdm
        get_mapping.return_value = [root_bdm, data_bdm]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._attach_volume_for_operation = mock.Mock()

        incus_driver.finish_revert_migration(
            ctx, instance, [], migration, block_device_info={}, power_on=True)

        incus_driver._attach_volume_for_operation.assert_called_once_with(
            ctx, data_connection, instance, '/dev/vdb',
            data_bdm['attachment_id'], 'migration',
            '10000000-0000-0000-0000-000000000001',
            'cold-revert-source',
            operation_migration_uuid=migration.uuid)
        require_bfv.assert_called_once_with(self.client, root_bdm)
        container.start.assert_called_once_with(wait=True)

    @mock.patch.object(driver, '_migration_client')
    @mock.patch.object(driver, '_require_bfv_migration_support')
    @mock.patch.object(driver, '_boot_from_volume')
    @mock.patch('nova.virt.driver.block_device_info_get_mapping',
                return_value=[])
    def test_finish_revert_migration_marks_failed_bfv_owner(
            self, get_mapping, boot_from_volume, require_bfv,
            migration_client):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        boot_from_volume.return_value = {'boot_index': 0}
        container = mock.Mock(status='Stopped')
        container.start.side_effect = RuntimeError('start failed')
        self.client.instances.get.return_value = container
        profile = self._prepare_cold_revert_protocol(
            instance, container, migration_client.return_value)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.finish_revert_migration(
            ctx, instance, [],
            mock.Mock(source_compute='source', dest_compute='destination'),
            block_device_info={},
            power_on=True)

        container.start.assert_called_once_with(wait=True)
        self.assertEqual(
            'running', profile.config[driver.MIGRATION_RECOVERY_KEY])
        profile.save.assert_called_once_with(wait=True)

    @mock.patch.object(driver.flavor, 'to_profile')
    def test_finish_migration_rejects_same_host_before_side_effects(
            self, to_profile):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        migration = mock.Mock(
            source_compute='compute', dest_compute='compute')
        self.CONF.incus.allow_cold_migration = True
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaises(
            exception.UnableToMigrateToSelf,
            incus_driver.finish_migration,
            ctx, migration, instance, '{}', [], mock.Mock(), True, {},
            block_device_info=None, power_on=True)

        to_profile.assert_not_called()
        self.client.instances.create.assert_not_called()
        self.client.profiles.get.assert_not_called()

    @mock.patch.object(driver.os.path, 'ismount', return_value=True)
    @mock.patch.object(driver, '_validate_existing_share_mount')
    @mock.patch.object(driver.flavor, 'to_profile')
    def test_finish_migration_consumes_staged_manila_journal(
            self, to_profile, validate_mount, ismount):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', root_gb=1)
        migration = mock.Mock(
            source_compute='source', dest_compute='destination')
        cleanup_token = '10000000-0000-0000-0000-000000000001'
        share = mock.Mock(
            share_id='20000000-0000-0000-0000-000000000002',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        driver._write_share_journal(
            instance, share, cleanup_token, 'mounted')
        profile = mock.Mock(config={}, devices={})
        timeline = []
        profile.save.side_effect = lambda wait: timeline.append(
            ('profile', dict(profile.devices)))
        to_profile.return_value = profile
        self.client.profiles.get.return_value = profile
        container = mock.Mock()
        container.start.side_effect = lambda wait: timeline.append(
            ('start', None))
        self.client.instances.create.return_value = container
        self.CONF.incus.allow_cold_migration = True
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        disk_info = migration_disk_info({
            'format': 'incus-pull-v1',
            'cleanup_token': cleanup_token,
            'migration_data': {
                'name': instance.name,
                'source': {'type': 'migration'},
            },
            'manila_share_ids': [share.share_id],
            'was_running': True,
        })

        incus_driver.finish_migration(
            ctx, migration, instance, disk_info, [], mock.Mock(), True, {},
            block_device_info=None, power_on=True)

        initial_config = to_profile.call_args.kwargs['config_overrides']
        self.assertEqual(
            cleanup_token,
            initial_config[driver.MIGRATION_DESTINATION_PREPARED_KEY])
        self.assertEqual(
            cleanup_token,
            initial_config[driver.MIGRATION_CLEANUP_TOKEN_KEY])
        self.assertEqual('1065536', initial_config['security.idmap.base'])
        self.assertEqual('65536', initial_config['security.idmap.size'])

        device = profile.devices[driver._share_device_name(share)]
        self.assertEqual(
            driver._share_mount_path(instance, share),
            device['source'])
        self.assertEqual('/mnt/manila/project-data', device['path'])
        self.assertEqual([], driver._share_journal_records(instance))
        container.start.assert_called_once_with(wait=True)
        share_save = next(
            index for index, event in enumerate(timeline)
            if driver._share_device_name(share) in (event[1] or {}))
        start = next(
            index for index, event in enumerate(timeline)
            if event[0] == 'start')
        self.assertLess(share_save, start)

    @mock.patch.object(driver.flavor, 'to_profile')
    def test_finish_migration_create_failure_rolls_back(self, to_profile):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', root_gb=1)
        migration = mock.Mock(
            source_compute='source', dest_compute='destination')
        vif = network_model.VIF(id='test-vif')
        network_info = [vif]
        profile = to_profile.return_value
        profile.config = {}
        profile.devices = {}
        self.client.profiles.get.return_value = profile
        self.client.instances.create.side_effect = RuntimeError(
            'destination pull failed')
        self.client.instances.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        self.CONF.incus.allow_cold_migration = True
        disk_info = migration_disk_info({
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
            instance, vif)
        self.vif_driver.unplug.assert_called_once_with(
            instance, vif)
        profile.delete.assert_called_once_with()

    @mock.patch.object(driver.flavor, 'to_profile')
    def test_finish_migration_partial_vif_cleanup_retains_profile(
            self, to_profile):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', root_gb=1)
        migration = mock.Mock(
            source_compute='source', dest_compute='destination')
        vifs = [
            network_model.VIF(id='first-vif'),
            network_model.VIF(id='second-vif'),
        ]
        profile = mock.Mock(
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
            },
            devices={},
            used_by=[])
        to_profile.return_value = profile
        self.client.profiles.get.return_value = profile
        self.client.instances.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        self.vif_driver.plug.side_effect = [
            None, RuntimeError('second plug failed')]
        self.vif_driver.unplug.side_effect = [
            RuntimeError('initial rollback failed'),
            RuntimeError('cleanup retry failed'),
            None,
        ]
        self.CONF.incus.allow_cold_migration = True
        disk_info = migration_disk_info({
            'format': 'incus-pull-v1',
            'migration_data': {
                'name': instance.name,
                'source': {'type': 'migration'},
            },
        })
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaisesRegex(
            RuntimeError, 'second plug failed',
            incus_driver.finish_migration,
            ctx, migration, instance, disk_info, vifs, mock.Mock(),
            True, {}, block_device_info=None, power_on=True)

        self.assertEqual(
            [
                mock.call(instance, vifs[0]),
                mock.call(instance, vifs[0]),
                mock.call(instance, vifs[1]),
            ],
            self.vif_driver.unplug.call_args_list)
        self.assertEqual(
            'true', profile.config[driver.CLEANUP_RECOVERY_KEY])
        profile.delete.assert_not_called()
        self._retire_migration_attempt.assert_not_called()

    def test_failed_finish_cleanup_failure_retains_attempt_for_periodic_retry(
            self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        self.client.instances.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._cleanup = mock.Mock(
            side_effect=exception.MigrationError(reason='umount failed'))
        attempt = {'state': 'active', 'finished': False}
        self._abort_migration_attempt.return_value = {
            'state': 'aborted', 'finished': True}

        recovered = incus_driver._rollback_failed_finish_migration(
            ctx, instance, [mock.sentinel.vif], None, attempt,
            '10000000-0000-0000-0000-000000000001',
            1065536, 65536, None, False, mock.Mock(), [], True, True, None)

        self.assertFalse(recovered)
        incus_driver._cleanup.assert_called_once_with(
            ctx, instance, [mock.sentinel.vif],
            block_device_info=None, destroy_vifs=True,
            delete_profile=True)
        self._retire_migration_attempt.assert_not_called()

    @mock.patch.object(driver, '_require_bfv_migration_support')
    @mock.patch.object(driver, '_boot_from_volume')
    @mock.patch('nova.virt.driver.block_device_info_get_mapping',
                return_value=[])
    @mock.patch.object(driver.flavor, 'to_profile')
    def test_finish_migration_retains_claimed_bfv_target_on_start_failure(
            self, to_profile, get_mapping, boot_from_volume, require_bfv):
        self._configure_bfv_pool()
        require_bfv.return_value = ('cinder-volumes', 'volume-root')
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        migration = mock.Mock(
            source_compute='source', dest_compute='destination')
        root_bdm = {'boot_index': 0}
        boot_from_volume.return_value = root_bdm
        profile = to_profile.return_value
        profile.config = {}
        self.client.profiles.get.return_value = profile
        container = self.client.instances.create.return_value
        container.start.side_effect = RuntimeError('target start failed')
        self.CONF.incus.allow_cold_migration = True
        disk_info = migration_disk_info({
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
        self.assertEqual(1, container.start.call_count)
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
        self._configure_bfv_pool()
        require_bfv.return_value = ('cinder-volumes', 'volume-root')
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        boot_from_volume.return_value = {'boot_index': 0}
        profile = to_profile.return_value
        profile.config = {}
        self.client.profiles.get.return_value = profile
        profile.save.side_effect = [
            incuscore_exceptions.ClientConnectionFailed('database busy'),
            None,
        ]
        container = self.client.instances.create.return_value
        container.start.side_effect = RuntimeError('target start failed')
        self.CONF.incus.allow_cold_migration = True
        disk_info = migration_disk_info({
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
        self._configure_bfv_pool()
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        root_bdm = {'boot_index': 0}
        boot_from_volume.return_value = root_bdm
        require_bfv.return_value = ('cinder-volumes', 'volume-root')
        profile = to_profile.return_value
        profile.devices = {}
        profile.config = {}
        self.client.profiles.get.return_value = profile
        profile.save.side_effect = [
            incuscore_exceptions.ClientConnectionFailed(
                'database unavailable')
            for unused_attempt in range(
                self.CONF.incus.migration_finish_retries)
        ]
        container = self.client.instances.create.return_value
        container.start.side_effect = RuntimeError('target start failed')
        self.CONF.incus.allow_cold_migration = True
        disk_info = migration_disk_info({
            'format': 'incus-pull-v1',
            'boot_from_volume': True,
            'migration_data': {
                'name': instance.name,
                'source': {'type': 'migration'},
            },
        })
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        vif = network_model.VIF(id='test-vif')
        self.assertRaises(
            RuntimeError,
            incus_driver.finish_migration,
            ctx, mock.Mock(), instance, disk_info, [vif],
            mock.Mock(), True, {}, block_device_info={}, power_on=True)

        container.delete.assert_not_called()
        profile.delete.assert_not_called()
        self.vif_driver.unplug.assert_not_called()
        self.assertEqual(
            self.CONF.incus.migration_finish_retries,
            profile.save.call_count)

    @mock.patch.object(driver, '_require_bfv_migration_support')
    @mock.patch.object(driver, '_boot_from_volume')
    @mock.patch('nova.virt.driver.block_device_info_get_mapping',
                return_value=[])
    @mock.patch.object(driver.flavor, 'to_profile')
    def test_finish_migration_retries_transient_target_start_failure(
            self, to_profile, get_mapping, boot_from_volume, require_bfv):
        self._configure_bfv_pool()
        require_bfv.return_value = ('cinder-volumes', 'volume-root')
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        migration = mock.Mock(
            source_compute='source', dest_compute='destination')
        root_bdm = {'boot_index': 0}
        boot_from_volume.return_value = root_bdm
        container = self.client.instances.create.return_value
        container.start.side_effect = [
            incuscore_exceptions.ClientConnectionFailed(
                'transient target start failure'),
            None,
        ]
        profile = to_profile.return_value
        profile.config = {}
        self.client.profiles.get.return_value = profile
        self.CONF.incus.allow_cold_migration = True
        disk_info = migration_disk_info({
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
        self._configure_bfv_pool()
        require_bfv.return_value = ('cinder-volumes', 'volume-root')
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        migration = mock.Mock(
            source_compute='source', dest_compute='destination')
        root_bdm = {'boot_index': 0}
        boot_from_volume.return_value = root_bdm
        profile = to_profile.return_value
        profile.config = {}
        self.client.profiles.get.return_value = profile
        claimed_container = mock.Mock(config={
            driver.MIGRATION_RECEIVE_COMPLETE_KEY: 'true',
        })
        self.client.instances.create.side_effect = RuntimeError(
            'response timed out after server accepted create')
        self.client.instances.get.return_value = claimed_container
        self.CONF.incus.allow_cold_migration = True
        disk_info = migration_disk_info({
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

    @mock.patch.object(driver, '_require_bfv_migration_support')
    @mock.patch.object(driver, '_boot_from_volume')
    @mock.patch.object(driver.flavor, 'to_profile')
    def test_finish_migration_rolls_back_incomplete_timeout_target(
            self, to_profile, boot_from_volume, require_bfv):
        self._configure_bfv_pool()
        require_bfv.return_value = ('cinder-volumes', 'volume-root')
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        root_bdm = {'boot_index': 0}
        boot_from_volume.return_value = root_bdm
        profile = to_profile.return_value
        profile.config = {}
        profile.devices = {}
        self.client.profiles.get.return_value = profile
        self.client.instances.create.side_effect = RuntimeError(
            'response timed out before receive completed')
        self.client.instances.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        self.CONF.incus.allow_cold_migration = True
        disk_info = migration_disk_info({
            'format': 'incus-pull-v1',
            'boot_from_volume': True,
            'migration_data': {
                'name': instance.name,
                'source': {'type': 'migration'},
            },
        })
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        vif = network_model.VIF(id='test-vif')
        self.assertRaises(
            RuntimeError,
            incus_driver.finish_migration,
            ctx, mock.Mock(), instance, disk_info, [vif],
            mock.Mock(), True, {}, block_device_info={}, power_on=True)

        self.assertNotIn(driver.MIGRATION_RECOVERY_KEY, profile.config)
        profile.delete.assert_called_once_with()
        self.vif_driver.unplug.assert_called_once_with(
            instance, vif)

    def test_finish_migration_rejects_bfv_mode_mismatch(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        migration = mock.Mock(
            source_compute='source', dest_compute='destination')
        self.CONF.incus.allow_cold_migration = True
        disk_info = migration_disk_info({
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
        self._configure_bfv_pool()
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        migration = mock.Mock(
            uuid='40000000-0000-0000-0000-000000000004',
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
            'attachment_id':
                '30000000-0000-0000-0000-000000000003',
            'connection_info': data_connection,
            'mount_device': '/dev/vdb',
        }
        boot_from_volume.return_value = root_bdm
        require_bfv.return_value = (
            'cinder-volumes', 'volume-%s' % volume_id)
        get_mapping.return_value = [root_bdm, data_bdm]
        profile = to_profile.return_value
        profile.devices = {'root': {'size': '10GB'}}
        profile.config = {}
        self.client.profiles.get.return_value = profile
        self.CONF.incus.allow_cold_migration = True
        self.CONF.incus.boot_from_volume_storage_pools = {
            'cinder-volumes': 'cinder'}
        disk_info = migration_disk_info({
            'format': 'incus-pull-v1',
            'boot_from_volume': True,
            'migration_data': {
                'name': instance.name,
                'source': {'type': 'migration'},
            },
        })
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._attach_volume_for_operation = mock.Mock()

        incus_driver.finish_migration(
            ctx, migration, instance, disk_info, [], mock.Mock(), True, {},
            block_device_info={}, power_on=True)

        incus_driver._attach_volume_for_operation.assert_called_once_with(
            ctx, data_connection, instance, '/dev/vdb',
            data_bdm['attachment_id'], 'migration',
            '10000000-0000-0000-0000-000000000001', 'cold-target',
            operation_migration_uuid=migration.uuid)
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

    def test_check_can_live_migrate_destination_rejects_same_host_first(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        self.CONF.incus.allow_live_migration = True
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        instance.host = incus_driver.host

        raised = self.assertRaises(
            exception.MigrationPreCheckError,
            incus_driver.check_can_live_migrate_destination,
            ctx, instance, mock.Mock(), mock.Mock())

        self.assertIn('source compute', str(raised))
        self.client.instances.get.assert_not_called()
        self.client.profiles.get.assert_not_called()

    def test_check_can_live_migrate_destination_returns_host_facts(
            self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        self.CONF.incus.allow_live_migration = True
        self.CONF.incus.migration_address = 'https://192.0.2.20:8443'
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        with mock.patch.object(
                driver.objects.MigrationList, 'get_by_filters') as get:
            data = incus_driver.check_can_live_migrate_destination(
                ctx, instance, mock.Mock(), mock.Mock())

        self.assertIsInstance(data, migrate_data.IncusLiveMigrateData)
        self.assertEqual(
            'https://192.0.2.20:8443', data.destination_address)
        self.assertEqual('x86_64', data.destination_architecture)
        self.assertEqual('6.8.0-test', data.destination_kernel_version)
        self.assertEqual('7.2', data.destination_server_version)
        self.assertEqual(data.cleanup_token, data.migration_uuid)
        get.assert_not_called()
        self.assertFalse(data.obj_attr_is_set('full_checkpoint_verified'))

    def test_live_migrate_data_full_checkpoint_compatibility(self):
        data = migrate_data.IncusLiveMigrateData(
            full_checkpoint_verified=True)

        current = data.obj_to_primitive(target_version='1.6')
        legacy = data.obj_to_primitive(target_version='1.5')

        self.assertIs(
            True,
            current['nova_object.data']['full_checkpoint_verified'])
        self.assertNotIn(
            'full_checkpoint_verified', legacy['nova_object.data'])

    def test_conductor_backports_full_checkpoint_attestation_for_1_5(self):
        data = migrate_data.IncusLiveMigrateData(
            full_checkpoint_verified=True)
        conductor = conductor_manager.ConductorManager.__new__(
            conductor_manager.ConductorManager)

        primitive = conductor.object_backport_versions(
            None, data, {'IncusLiveMigrateData': '1.5'})

        self.assertEqual('1.5', primitive['nova_object.version'])
        self.assertNotIn(
            'full_checkpoint_verified', primitive['nova_object.data'])

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

    def test_shared_root_storage_detected_for_both_ceph_drivers(self):
        """A shared root moves by handover, so the source must stop writing.

        Saving a profile resyncs backup.yaml inside the root volume. Once
        the destination claims a Ceph-backed root the source can no longer
        mount it, and doing so anyway failed the whole live migration with
        "RBD image is already in use".
        """
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test-shared-root')

        for pool_driver, shared in (
                ('ceph', True), ('cephext', True),
                ('zfs', False), ('dir', False)):
            with mock.patch.object(
                    driver, '_instance_root_pool',
                    return_value=mock.Mock(driver=pool_driver)):
                self.assertIs(
                    shared,
                    driver._live_migration_shares_root_storage(
                        self.client, instance))

    def test_shared_root_storage_unreadable_pool_is_not_shared(self):
        instance = fake_instance.fake_instance_obj(
            context.get_admin_context(), name='test-unreadable-pool')

        with mock.patch.object(
                driver, '_instance_root_pool',
                side_effect=Exception('pool is gone')):
            self.assertFalse(
                driver._live_migration_shares_root_storage(
                    self.client, instance))

    @mock.patch.object(driver, '_migration_client')
    def test_check_can_live_migrate_source_accepts_compatible_container(
            self, migration_client):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', config_drive=False)
        instance.config_drive = ''
        self.CONF.incus.allow_live_migration = True
        profile = mock.Mock()
        profile.config = {
            'migration.incremental.memory': 'false',
            'migration.stateful': 'true',
            'user.openstack.uuid': instance.uuid,
        }
        profile.devices = {
            'root': {'type': 'disk', 'path': '/'},
            'eth0': {'type': 'nic'},
        }
        self.client.profiles.get.return_value = profile
        self.client.instances.get.return_value.status = 'Running'
        self.client.instances.get.return_value.config = {
            'migration.incremental.memory': 'false',
            'user.openstack.uuid': instance.uuid,
            'volatile.idmap.base': '1065536'}
        self.client.instances.get.return_value.expanded_config = {
            'migration.incremental.memory': 'false',
            'migration.stateful': 'true'}
        self.client.instances.get.return_value.profiles = [instance.name]
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2',
            cleanup_token='10000000-0000-0000-0000-000000000001')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        result = incus_driver.check_can_live_migrate_source(
            ctx, instance, data, {'block_device_mapping': []})

        self.assertIs(data, result)
        self.assertIs(True, result.full_checkpoint_verified)
        self.assertEqual([
            mock.call(ctx, instance.uuid),
            mock.call(ctx, instance.uuid),
        ], self.share_mappings.call_args_list)
        source_profile = jsonutils.loads(result.source_profile)
        self.assertEqual('1065536',
                         source_profile['config']['security.idmap.base'])
        self.assertEqual(profile.devices, source_profile['devices'])
        migration_client.assert_called_once_with(
            'https://192.0.2.20:8443')

    def test_live_migration_source_normalizes_expanded_incremental_config(
            self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        profile = mock.Mock(
            config={
                'migration.stateful': 'true',
                'user.openstack.uuid': instance.uuid,
            },
            devices={'root': {'type': 'disk', 'path': '/'}})
        container = mock.Mock(
            config={'user.openstack.uuid': instance.uuid},
            expanded_config={
                'migration.incremental.memory': 'true',
                'migration.stateful': 'true',
            },
            profiles=[instance.name],
            status='Running')

        def persist_config(wait):
            container.expanded_config = dict(profile.config)
            container.expanded_config.update(container.config)

        container.save.side_effect = persist_config
        self.client.profiles.get.return_value = profile
        self.client.instances.get.return_value = container

        observed_container, observed_profile = (
            driver._full_checkpoint_live_migration_source(
                self.client, ctx, instance, {'block_device_mapping': []},
                normalize_incremental_memory=True))

        self.assertIs(container, observed_container)
        self.assertIs(profile, observed_profile)
        self.assertEqual(
            'false', container.config['migration.incremental.memory'])
        self.assertEqual(
            'false', container.expanded_config[
                'migration.incremental.memory'])
        profile.save.assert_called_once_with(wait=True)
        container.save.assert_called_once_with(wait=True)

    def test_live_migration_source_profile_normalization_failure_is_fatal(
            self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        profile = mock.Mock(
            config={
                'migration.stateful': 'true',
                'user.openstack.uuid': instance.uuid,
            },
            devices={'root': {'type': 'disk', 'path': '/'}})
        profile.save.side_effect = RuntimeError('profile write failed')
        container = mock.Mock(
            config={'user.openstack.uuid': instance.uuid},
            expanded_config={'migration.stateful': 'true'},
            profiles=[instance.name],
            status='Running')
        self.client.profiles.get.return_value = profile
        self.client.instances.get.return_value = container

        self.assertRaisesRegex(
            exception.MigrationPreCheckError, 'source profile',
            driver._full_checkpoint_live_migration_source,
            self.client, ctx, instance, {'block_device_mapping': []}, True)

        profile.save.assert_called_once_with(wait=True)
        container.save.assert_not_called()

    @mock.patch.object(driver, '_migration_client')
    def test_source_normalization_failure_does_not_register_target_attempt(
            self, migration_client):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', config_drive=False)
        instance.config_drive = ''
        self.CONF.incus.allow_live_migration = True
        profile = mock.Mock(
            config={
                'migration.stateful': 'true',
                'user.openstack.uuid': instance.uuid,
            },
            devices={'root': {'type': 'disk', 'path': '/'}})
        profile.save.side_effect = RuntimeError('profile write failed')
        container = mock.Mock(
            config={'user.openstack.uuid': instance.uuid},
            expanded_config={
                'migration.incremental.memory': 'true',
                'migration.stateful': 'true',
            },
            profiles=[instance.name],
            status='Running')
        self.client.profiles.get.return_value = profile
        self.client.instances.get.return_value = container
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2',
            cleanup_token='10000000-0000-0000-0000-000000000001')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaisesRegex(
            exception.MigrationPreCheckError, 'source profile',
            incus_driver.check_can_live_migrate_source,
            ctx, instance, data, {'block_device_mapping': []})

        migration_client.assert_not_called()
        profile.save.assert_called_once_with(wait=True)
        container.save.assert_not_called()

    def test_live_migration_source_local_normalization_failure_is_fatal(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        profile = mock.Mock(
            config={
                'migration.stateful': 'true',
                'user.openstack.uuid': instance.uuid,
            },
            devices={'root': {'type': 'disk', 'path': '/'}})
        container = mock.Mock(
            config={'user.openstack.uuid': instance.uuid},
            expanded_config={'migration.stateful': 'true'},
            profiles=[instance.name],
            status='Running')
        container.save.side_effect = RuntimeError('instance write failed')
        self.client.profiles.get.return_value = profile
        self.client.instances.get.return_value = container

        self.assertRaisesRegex(
            exception.MigrationPreCheckError, 'source instance',
            driver._full_checkpoint_live_migration_source,
            self.client, ctx, instance, {'block_device_mapping': []}, True)

        profile.save.assert_called_once_with(wait=True)
        container.save.assert_called_once_with(wait=True)

    def test_live_migration_source_rejects_foreign_local_owner_without_writes(
            self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        profile = mock.Mock(
            config={
                'migration.incremental.memory': 'false',
                'migration.stateful': 'true',
                'user.openstack.uuid': instance.uuid,
            },
            devices={'root': {'type': 'disk', 'path': '/'}})
        container = mock.Mock(
            config={
                'migration.incremental.memory': 'false',
                'user.openstack.uuid':
                    '10000000-0000-0000-0000-000000000001',
            },
            expanded_config={
                'migration.incremental.memory': 'false',
                'migration.stateful': 'true',
            },
            profiles=[instance.name],
            status='Running')
        self.client.profiles.get.return_value = profile
        self.client.instances.get.return_value = container

        self.assertRaisesRegex(
            exception.MigrationPreCheckError, 'different Nova UUID',
            driver._full_checkpoint_live_migration_source,
            self.client, ctx, instance, {'block_device_mapping': []}, True)

        profile.save.assert_not_called()
        container.save.assert_not_called()

    def test_live_migration_source_rejects_missing_profile_owner(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        profile = mock.Mock(
            config={
                'migration.incremental.memory': 'false',
                'migration.stateful': 'true',
            },
            devices={'root': {'type': 'disk', 'path': '/'}})
        self.client.profiles.get.return_value = profile

        self.assertRaisesRegex(
            exception.MigrationPreCheckError, 'missing or different Nova UUID',
            driver._full_checkpoint_live_migration_source,
            self.client, ctx, instance, {'block_device_mapping': []}, True)

        self.client.instances.get.assert_not_called()
        profile.save.assert_not_called()

    def test_live_migration_source_rejects_missing_local_owner(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        profile = mock.Mock(
            config={
                'migration.incremental.memory': 'false',
                'migration.stateful': 'true',
                'user.openstack.uuid': instance.uuid,
            },
            devices={'root': {'type': 'disk', 'path': '/'}})
        container = mock.Mock(
            config={'migration.incremental.memory': 'false'},
            expanded_config={
                'migration.incremental.memory': 'false',
                'migration.stateful': 'true',
            },
            profiles=[instance.name],
            status='Running')
        self.client.profiles.get.return_value = profile
        self.client.instances.get.return_value = container

        self.assertRaisesRegex(
            exception.MigrationPreCheckError, 'missing or different Nova UUID',
            driver._full_checkpoint_live_migration_source,
            self.client, ctx, instance, {'block_device_mapping': []}, True)

        profile.save.assert_not_called()
        container.save.assert_not_called()

    def test_live_migration_source_rejects_effective_stateful_override(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        profile = mock.Mock(
            config={
                'migration.incremental.memory': 'false',
                'migration.stateful': 'true',
                'user.openstack.uuid': instance.uuid,
            },
            devices={'root': {'type': 'disk', 'path': '/'}})
        container = mock.Mock(
            config={
                'migration.incremental.memory': 'false',
                'migration.stateful': 'false',
                'user.openstack.uuid': instance.uuid,
            },
            expanded_config={
                'migration.incremental.memory': 'false',
                'migration.stateful': 'false',
            },
            profiles=[instance.name],
            status='Running')
        self.client.profiles.get.return_value = profile
        self.client.instances.get.return_value = container

        self.assertRaisesRegex(
            exception.MigrationPreCheckError, 'expanded config.*stateful=true',
            driver._full_checkpoint_live_migration_source,
            self.client, ctx, instance, {'block_device_mapping': []}, True)

        profile.save.assert_not_called()
        container.save.assert_not_called()

    def test_live_migration_source_rejects_profile_privileged_tokens(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')

        for value in ('true', '1', 'yes', 'on'):
            profile = mock.Mock(
                config={
                    'migration.incremental.memory': 'false',
                    'migration.stateful': 'true',
                    'security.privileged': value,
                    'user.openstack.uuid': instance.uuid,
                },
                devices={'root': {'type': 'disk', 'path': '/'}})
            self.client.profiles.get.return_value = profile

            with self.subTest(value=value):
                self.assertRaisesRegex(
                    exception.MigrationPreCheckError, 'Privileged',
                    driver._full_checkpoint_live_migration_source,
                    self.client, ctx, instance,
                    {'block_device_mapping': []}, True)

        self.client.instances.get.assert_not_called()

    def test_live_migration_source_rejects_local_privileged_tokens(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        profile = mock.Mock(
            config={
                'migration.incremental.memory': 'false',
                'migration.stateful': 'true',
                'user.openstack.uuid': instance.uuid,
            },
            devices={'root': {'type': 'disk', 'path': '/'}})
        self.client.profiles.get.return_value = profile

        for value in ('true', '1', 'yes', 'on'):
            container = mock.Mock(
                config={
                    'migration.incremental.memory': 'false',
                    'security.privileged': value,
                    'user.openstack.uuid': instance.uuid,
                },
                expanded_config={
                    'migration.incremental.memory': 'false',
                    'migration.stateful': 'true',
                    'security.privileged': 'false',
                },
                profiles=[instance.name],
                status='Running')
            self.client.instances.get.return_value = container

            with self.subTest(value=value):
                self.assertRaisesRegex(
                    exception.MigrationPreCheckError, 'Privileged',
                    driver._full_checkpoint_live_migration_source,
                    self.client, ctx, instance,
                    {'block_device_mapping': []}, True)

            profile.save.assert_not_called()
            container.save.assert_not_called()

    def test_live_migration_source_rejects_expanded_privileged_tokens(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        profile = mock.Mock(
            config={
                'migration.incremental.memory': 'false',
                'migration.stateful': 'true',
                'user.openstack.uuid': instance.uuid,
            },
            devices={'root': {'type': 'disk', 'path': '/'}})
        self.client.profiles.get.return_value = profile

        for value in ('true', '1', 'yes', 'on'):
            container = mock.Mock(
                config={
                    'migration.incremental.memory': 'false',
                    'security.privileged': 'false',
                    'user.openstack.uuid': instance.uuid,
                },
                expanded_config={
                    'migration.incremental.memory': 'false',
                    'migration.stateful': 'true',
                    'security.privileged': value,
                },
                profiles=[instance.name],
                status='Running')
            self.client.instances.get.return_value = container

            with self.subTest(value=value):
                self.assertRaisesRegex(
                    exception.MigrationPreCheckError, 'Privileged',
                    driver._full_checkpoint_live_migration_source,
                    self.client, ctx, instance,
                    {'block_device_mapping': []}, True)

            profile.save.assert_not_called()
            container.save.assert_not_called()

    def test_live_migration_source_rejects_non_dedicated_profile_chain(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        profile = mock.Mock(
            config={
                'migration.incremental.memory': 'false',
                'migration.stateful': 'true',
                'user.openstack.uuid': instance.uuid,
            },
            devices={'root': {'type': 'disk', 'path': '/'}})
        self.client.profiles.get.return_value = profile

        for profiles in (
                [], [instance.name, 'extra'], ['extra', instance.name]):
            container = mock.Mock(
                config={
                    'migration.incremental.memory': 'false',
                    'migration.stateful': 'true',
                    'user.openstack.uuid': instance.uuid,
                },
                expanded_config={
                    'migration.incremental.memory': 'false',
                },
                profiles=profiles,
                status='Running')
            self.client.instances.get.return_value = container

            self.assertRaisesRegex(
                exception.MigrationPreCheckError,
                'only attached profile',
                driver._full_checkpoint_live_migration_source,
                self.client, ctx, instance, {'block_device_mapping': []},
                True)
            container.save.assert_not_called()

    def test_source_expanded_config_must_converge(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        self.client.profiles.get.return_value = mock.Mock(
            config={
                'migration.stateful': 'true',
                'user.openstack.uuid': instance.uuid,
            },
            devices={'root': {'type': 'disk', 'path': '/'}})
        container = mock.Mock(
            config={'user.openstack.uuid': instance.uuid},
            expanded_config={
                'migration.incremental.memory': 'true',
                'migration.stateful': 'true',
            },
            profiles=[instance.name],
            status='Running')
        self.client.instances.get.return_value = container

        self.assertRaisesRegex(
            exception.MigrationPreCheckError, 'did not converge',
            driver._full_checkpoint_live_migration_source,
            self.client, ctx, instance, {'block_device_mapping': []}, True)

        container.save.assert_called_once_with(wait=True)

    @mock.patch.object(driver, '_migration_client')
    def test_check_can_live_migrate_source_accepts_manila_share(
            self, migration_client):
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
        self.share_mappings.return_value = [mapping]
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
            config={
                'migration.incremental.memory': 'false',
                'migration.stateful': 'true',
                'user.openstack.uuid': instance.uuid,
            },
            devices={
                'root': {'type': 'disk', 'path': '/'},
                'manila-' + share_id: share_device,
            })
        self.client.profiles.get.return_value = profile
        self.client.instances.get.return_value.status = 'Running'
        self.client.instances.get.return_value.config = {
            'migration.incremental.memory': 'false',
            'user.openstack.uuid': instance.uuid,
            'volatile.idmap.base': '1065536'}
        self.client.instances.get.return_value.expanded_config = {
            'migration.incremental.memory': 'false',
            'migration.stateful': 'true'}
        self.client.instances.get.return_value.profiles = [instance.name]
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2',
            cleanup_token='10000000-0000-0000-0000-000000000001')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        result = incus_driver.check_can_live_migrate_source(
            ctx, instance, data, {'block_device_mapping': []})

        self.assertEqual(
            share_device,
            jsonutils.loads(result.source_profile)['devices'][
                'manila-' + share_id])
        migration_client.assert_called_once_with(
            'https://192.0.2.20:8443')

    @mock.patch.object(driver, '_migration_client')
    def test_check_can_live_migrate_source_rejects_missing_manila_device(
            self, migration_client):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', config_drive=False)
        instance.config_drive = ''
        self.CONF.incus.allow_live_migration = True
        share_id = '10000000-0000-0000-0000-000000000001'
        self.share_mappings.return_value = [mock.Mock(
            share_id=share_id,
            instance_uuid=instance.uuid,
            tag='project-data',
            status=driver.obj_fields.ShareMappingStatus.ACTIVE)]
        profile = mock.Mock(
            config={
                'migration.incremental.memory': 'false',
                'migration.stateful': 'true',
                'user.openstack.uuid': instance.uuid,
            },
            devices={'root': {'type': 'disk', 'path': '/'}},
        )
        container = self.client.instances.get.return_value
        container.status = 'Running'
        container.expanded_devices = {
            'root': {'type': 'disk', 'path': '/', 'pool': 'local'}}
        self.client.storage_pools.get.return_value.driver = 'zfs'
        self.client.profiles.get.return_value = profile
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2',
            cleanup_token='10000000-0000-0000-0000-000000000001')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaisesRegex(
            exception.MigrationPreCheckError,
            'do not match Nova share mappings',
            incus_driver.check_can_live_migrate_source,
            ctx, instance, data, {'block_device_mapping': []})

        profile.save.assert_not_called()
        migration_client.assert_not_called()

    def test_check_can_live_migrate_source_rejects_forged_manila_device(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', config_drive=False)
        instance.config_drive = ''
        self.CONF.incus.allow_live_migration = True
        self.share_mappings.return_value = []
        profile = mock.Mock(
            config={
                'migration.stateful': 'true',
                'user.openstack.uuid': instance.uuid,
            },
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
            destination_server_version='7.2',
            cleanup_token='10000000-0000-0000-0000-000000000001')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaisesRegex(
            exception.MigrationPreCheckError,
            'device source is outside its Nova staging directory',
            incus_driver.check_can_live_migrate_source,
            ctx, instance, data, {'block_device_mapping': []})

    @mock.patch.object(driver, '_migration_client')
    @mock.patch.object(driver, '_preflight_bfv_migration_destination')
    @mock.patch.object(driver, '_require_bfv_live_migration_support')
    def test_check_can_live_migrate_source_accepts_bfv_root(
            self, require_bfv, preflight_destination, migration_client):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', config_drive=False)
        instance.config_drive = ''
        self.CONF.incus.allow_live_migration = True
        profile = mock.Mock(
            config={
                'migration.incremental.memory': 'false',
                'migration.stateful': 'true',
                'user.openstack.uuid': instance.uuid,
            },
            devices={'root': {'type': 'disk', 'path': '/', 'pool': 'cinder'}})
        container = self.client.instances.get.return_value
        container.status = 'Running'
        container.config = {
            'migration.incremental.memory': 'false',
            'user.openstack.uuid': instance.uuid,
            'volatile.idmap.base': '1065536'}
        container.expanded_config = {
            'migration.incremental.memory': 'false',
            'migration.stateful': 'true'}
        container.profiles = [instance.name]
        self.client.profiles.get.return_value = profile
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2',
            cleanup_token='10000000-0000-0000-0000-000000000001')
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
        migration_client.assert_called_once_with(
            'https://192.0.2.20:8443')

    @mock.patch.object(driver.incus_client,
                       'get_migration_preflight_client')
    @mock.patch.object(driver.socket, 'create_connection')
    def test_bfv_live_destination_preflight_requires_live_extension(
            self, connect, get_remote):
        get_remote.return_value.host_info = {'api_extensions': [
            'migration_shared_ceph_storage',
            driver.INCUS_STORAGE_READY_FENCE_EXTENSION,
            'storage_driver_cephext']}

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

    @mock.patch.object(driver, '_migration_client')
    def test_check_can_live_migrate_source_accepts_cinder_data_volume(
            self, migration_client):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', config_drive=False)
        instance.config_drive = ''
        self.CONF.incus.allow_live_migration = True
        profile = mock.Mock(
            config={
                'migration.incremental.memory': 'false',
                'migration.stateful': 'true',
                'user.openstack.uuid': instance.uuid,
            },
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
        container.config = {
            'migration.incremental.memory': 'false',
            'user.openstack.uuid': instance.uuid,
            'volatile.idmap.base': '1065536'}
        container.expanded_config = {
            'migration.incremental.memory': 'false',
            'migration.stateful': 'true'}
        container.profiles = [instance.name]
        self.client.profiles.get.return_value = profile
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2',
            cleanup_token='10000000-0000-0000-0000-000000000001')
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
        migration_client.assert_called_once_with(
            'https://192.0.2.20:8443')

    def test_check_can_live_migrate_source_rejects_kernel_mismatch(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', config_drive=False)
        instance.config_drive = ''
        self.CONF.incus.allow_live_migration = True
        profile = mock.Mock()
        profile.config = {
            'migration.incremental.memory': 'false',
            'migration.stateful': 'true',
            'user.openstack.uuid': instance.uuid,
        }
        profile.devices = {'root': {'type': 'disk', 'path': '/'}}
        self.client.profiles.get.return_value = profile
        self.client.instances.get.return_value.status = 'Running'
        self.client.instances.get.return_value.config = {
            'migration.incremental.memory': 'false',
            'user.openstack.uuid': instance.uuid,
            'volatile.idmap.base': '1065536'}
        self.client.instances.get.return_value.expanded_config = {
            'migration.incremental.memory': 'false',
            'migration.stateful': 'true'}
        self.client.instances.get.return_value.profiles = [instance.name]
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.9.0-other',
            destination_server_version='7.2',
            cleanup_token='10000000-0000-0000-0000-000000000001')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaisesRegex(
            exception.MigrationPreCheckError, 'kernel version',
            incus_driver.check_can_live_migrate_source,
            ctx, instance, data, {'block_device_mapping': []})

    @mock.patch.object(
        driver.IncusDriver, '_stage_volume_for_live_migration')
    @mock.patch.object(driver, '_remove_stale_live_migration_profile')
    def test_pre_live_migration_creates_profile_and_attaches_data_volume(
            self, remove_stale_profile, stage_volume):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        cleanup_token = '10000000-0000-0000-0000-000000000001'
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2',
            cleanup_token=cleanup_token,
            migration_uuid='40000000-0000-0000-0000-000000000004',
            idmap_base=1065536,
            idmap_size=65536,
            full_checkpoint_verified=True,
            source_profile=jsonutils.dumps({
                'config': {
                    'migration.stateful': 'true',
                    'migration.incremental.memory': 'false',
                    'security.idmap.base': '1065536',
                    'security.idmap.size': '65536',
                    'user.openstack.uuid': instance.uuid,
                    driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
                },
                'devices': {'root': {'type': 'disk', 'path': '/'}},
            }))
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        connection_info = {
            'serial': '20000000-0000-0000-0000-000000000002',
            'driver_volume_type': 'rbd',
            'data': {},
        }

        result = incus_driver.pre_live_migration(
            ctx, instance, {'block_device_mapping': [{
                'boot_index': None,
                'mount_device': '/dev/vdb',
                'connection_info': connection_info,
                'attachment_id':
                    '30000000-0000-0000-0000-000000000003',
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
                'migration.incremental.memory': 'false',
                'security.idmap.base': '1065536',
                'security.idmap.size': '65536',
                'user.openstack.uuid': instance.uuid,
                driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
                driver.MIGRATION_CLEANUP_COMPLETE_KEY: '',
                driver.MIGRATION_NOVA_UUID_KEY:
                    '40000000-0000-0000-0000-000000000004',
                driver.MIGRATION_DESTINATION_PREPARED_KEY: cleanup_token,
            },
            {'root': {'type': 'disk', 'path': '/'}})
        stage_volume.assert_called_once_with(
            ctx, connection_info, instance, '/dev/vdb',
            '30000000-0000-0000-0000-000000000003', cleanup_token,
            '40000000-0000-0000-0000-000000000004')

    def test_pre_live_migration_rejects_incremental_source_before_side_effects(
            self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        cleanup_token = '10000000-0000-0000-0000-000000000001'
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        for value in (None, 'true'):
            config = {
                'migration.stateful': 'true',
                'security.idmap.base': '1065536',
                'security.idmap.size': '65536',
                'user.openstack.uuid': instance.uuid,
                driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
            }
            if value is not None:
                config['migration.incremental.memory'] = value
            data = migrate_data.IncusLiveMigrateData(
                destination_address='https://192.0.2.20:8443',
                destination_architecture='x86_64',
                destination_kernel_version='6.8.0-test',
                destination_server_version='7.2',
                cleanup_token=cleanup_token,
                migration_uuid='40000000-0000-0000-0000-000000000004',
                idmap_base=1065536,
                idmap_size=65536,
                full_checkpoint_verified=True,
                source_profile=jsonutils.dumps({
                    'config': config,
                    'devices': {'root': {'type': 'disk', 'path': '/'}},
                }))

            self.assertRaisesRegex(
                exception.MigrationError,
                'migration.incremental.memory=false',
                incus_driver.pre_live_migration,
                ctx, instance, {'block_device_mapping': []}, [], None, data)

        self.begin_idmap_materialization.assert_not_called()
        self.client.profiles.create.assert_not_called()
        self.vif_driver.plug.assert_not_called()

    def test_pre_live_migration_rejects_old_source_attestation_first(
            self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        for attestation in (None, False):
            data = migrate_data.IncusLiveMigrateData(
                cleanup_token='10000000-0000-0000-0000-000000000001',
                source_profile=jsonutils.dumps({
                    'config': {
                        'migration.incremental.memory': 'false',
                        'user.openstack.uuid': instance.uuid,
                    },
                    'devices': {},
                }))
            if attestation is not None:
                data.full_checkpoint_verified = attestation

            with self.subTest(attestation=attestation):
                self.assertRaisesRegex(
                    exception.MigrationError, 'did not attest',
                    incus_driver.pre_live_migration,
                    ctx, instance, {'block_device_mapping': []}, [], None,
                    data)

        self.begin_idmap_materialization.assert_not_called()
        self.client.profiles.create.assert_not_called()
        self.vif_driver.plug.assert_not_called()

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

    @mock.patch.object(driver, '_validate_existing_share_mount')
    @mock.patch.object(driver.IncusDriver, 'mount_share')
    @mock.patch.object(driver, '_remove_stale_live_migration_profile')
    def test_pre_live_migration_leaves_manila_mount_to_manager(
            self, remove_stale_profile, mount_share, validate_mount):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        cleanup_token = '10000000-0000-0000-0000-000000000001'
        share = mock.Mock(
            share_id='20000000-0000-0000-0000-000000000002',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        source = driver._share_mount_path(instance, share)
        driver._write_share_journal(
            instance, share, cleanup_token, 'mounted')
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2',
            cleanup_token=cleanup_token,
            migration_uuid='40000000-0000-0000-0000-000000000004',
            idmap_base=1065536,
            idmap_size=65536,
            full_checkpoint_verified=True,
            source_profile=jsonutils.dumps({
                'config': {
                    'migration.stateful': 'true',
                    'migration.incremental.memory': 'false',
                    'security.idmap.base': '1065536',
                    'security.idmap.size': '65536',
                    'user.openstack.uuid': instance.uuid,
                    driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
                },
                'devices': {
                    'root': {'type': 'disk', 'path': '/'},
                    driver._share_device_name(share): {
                        'type': 'disk',
                        'source': source,
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
        validate_mount.assert_called_once()
        self.assertFalse(os.path.exists(
            driver._share_journal_path(instance, share.share_id)))

    @mock.patch.object(driver.eventlet, 'sleep')
    @mock.patch.object(driver.os.path, 'ismount', return_value=True)
    @mock.patch.object(driver, '_validate_existing_share_mount')
    @mock.patch.object(driver, '_remove_stale_live_migration_profile')
    def test_pre_live_migration_retries_manila_mount_propagation(
            self, remove_stale_profile, validate_mount, ismount, sleep):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        cleanup_token = '10000000-0000-0000-0000-000000000001'
        share = mock.Mock(
            share_id='20000000-0000-0000-0000-000000000002',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        share_source = driver._share_mount_path(instance, share)
        driver._write_share_journal(
            instance, share, cleanup_token, 'mounted')
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2',
            cleanup_token=cleanup_token,
            migration_uuid='40000000-0000-0000-0000-000000000004',
            idmap_base=1065536,
            idmap_size=65536,
            full_checkpoint_verified=True,
            source_profile=jsonutils.dumps({
                'config': {
                    'migration.stateful': 'true',
                    'migration.incremental.memory': 'false',
                    'security.idmap.base': '1065536',
                    'security.idmap.size': '65536',
                    'user.openstack.uuid': instance.uuid,
                    driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
                },
                'devices': {
                    'root': {'type': 'disk', 'path': '/'},
                    driver._share_device_name(share): {
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
        instance = mock.Mock()
        instance.name = 'instance-00000001'
        instance.uuid = '10000000-0000-0000-0000-000000000001'
        self.client.instances.get.side_effect = incuscore_exceptions.NotFound(
            MockResponse(404))
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        }
        profile.devices = {}
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

        self.client.instances.get.assert_called_once_with(instance.name)
        self.client.profiles.get.assert_not_called()

    @mock.patch.object(driver, '_require_bfv_live_migration_support')
    @mock.patch.object(
        driver.IncusDriver, '_stage_volume_for_live_migration')
    @mock.patch.object(driver, '_remove_stale_live_migration_profile')
    def test_pre_live_migration_leaves_bfv_root_to_cephext(
            self, remove_stale_profile, stage_volume, require_bfv):
        self._configure_bfv_pool()
        require_bfv.return_value = ('cinder-volumes', 'volume-root')
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        cleanup_token = '10000000-0000-0000-0000-000000000001'
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2',
            cleanup_token=cleanup_token,
            migration_uuid='40000000-0000-0000-0000-000000000004',
            idmap_base=1065536,
            idmap_size=65536,
            full_checkpoint_verified=True,
            source_profile=jsonutils.dumps({
                'config': {
                    'migration.stateful': 'true',
                    'migration.incremental.memory': 'false',
                    'security.idmap.base': '1065536',
                    'security.idmap.size': '65536',
                    'user.openstack.uuid': instance.uuid,
                    driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
                },
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
        stage_volume.assert_not_called()

    @mock.patch.object(
        driver.IncusDriver, '_stage_volume_for_live_migration')
    @mock.patch.object(driver, '_remove_stale_live_migration_profile')
    def test_pre_live_migration_failure_removes_profile_and_network(
            self, remove_stale_profile, stage_volume):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        cleanup_token = '10000000-0000-0000-0000-000000000001'
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2',
            cleanup_token=cleanup_token,
            migration_uuid='40000000-0000-0000-0000-000000000004',
            idmap_base=1065536,
            idmap_size=65536,
            full_checkpoint_verified=True,
            source_profile=jsonutils.dumps({
                'config': {
                    'migration.stateful': 'true',
                    'migration.incremental.memory': 'false',
                    'security.idmap.base': '1065536',
                    'security.idmap.size': '65536',
                    'user.openstack.uuid': instance.uuid,
                    driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
                },
                'devices': {'root': {'type': 'disk', 'path': '/'}},
            }))
        connection_info = {
            'serial': 'volume-id',
            'driver_volume_type': 'rbd',
            'data': {},
        }
        vif = network_model.VIF(id='test-vif')
        stage_volume.side_effect = RuntimeError('connect failed')
        profile = self.client.profiles.get.return_value
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaisesRegex(
            RuntimeError, 'connect failed',
            incus_driver.pre_live_migration,
            ctx, instance, {'block_device_mapping': [{
                'boot_index': None,
                'attachment_id':
                    '30000000-0000-0000-0000-000000000003',
                'mount_device': '/dev/vdb',
                'connection_info': connection_info,
            }]}, [vif], None, data)

        profile.delete.assert_called_once_with()
        self.vif_driver.unplug.assert_called_once_with(
            instance, vif)
        self.assertIs(
            True,
            connection_info['data'][driver._PRE_LIVE_DISCONNECTED_KEY])

    @mock.patch.object(driver, '_remove_stale_live_migration_profile')
    def test_pre_live_partial_vif_cleanup_retains_profile(
            self, remove_stale_profile):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        cleanup_token = '10000000-0000-0000-0000-000000000001'
        vifs = [
            network_model.VIF(id='first-vif'),
            network_model.VIF(id='second-vif'),
        ]
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2',
            cleanup_token=cleanup_token,
            migration_uuid='40000000-0000-0000-0000-000000000004',
            idmap_base=1065536,
            idmap_size=65536,
            full_checkpoint_verified=True,
            source_profile=jsonutils.dumps({
                'config': {
                    'migration.stateful': 'true',
                    'migration.incremental.memory': 'false',
                    'security.idmap.base': '1065536',
                    'security.idmap.size': '65536',
                    'user.openstack.uuid': instance.uuid,
                    driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
                },
                'devices': {'root': {'type': 'disk', 'path': '/'}},
            }))
        profile = mock.Mock(
            config={
                'environment.product_name': 'OpenStack Nova',
                'security.idmap.base': '1065536',
                'security.idmap.size': '65536',
                'user.openstack.uuid': instance.uuid,
                driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
            },
            devices={'root': {'type': 'disk', 'path': '/'}},
            used_by=[])
        self.client.profiles.get.return_value = profile
        self.vif_driver.plug.side_effect = [
            None, RuntimeError('second plug failed')]
        self.vif_driver.unplug.side_effect = [
            RuntimeError('initial rollback failed'),
            RuntimeError('cleanup retry failed'),
            None,
        ]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaisesRegex(
            RuntimeError, 'second plug failed',
            incus_driver.pre_live_migration,
            ctx, instance, {'block_device_mapping': []},
            vifs, None, data)

        self.assertEqual(
            [
                mock.call(instance, vifs[0]),
                mock.call(instance, vifs[0]),
                mock.call(instance, vifs[1]),
            ],
            self.vif_driver.unplug.call_args_list)
        self.assertEqual(
            'true', profile.config[driver.CLEANUP_RECOVERY_KEY])
        profile.delete.assert_not_called()
        self._retire_migration_attempt.assert_not_called()

    @mock.patch.object(
        driver.IncusDriver, '_stage_volume_for_live_migration',
        side_effect=RuntimeError('connect failed'))
    @mock.patch.object(driver.incus_privsep, 'umount',
                       side_effect=RuntimeError('NFS umount failed'))
    @mock.patch.object(driver, '_share_mount_table_index')
    @mock.patch.object(driver, '_validate_existing_share_mount')
    @mock.patch.object(driver, '_remove_stale_live_migration_profile')
    def test_pre_live_manila_cleanup_failure_retains_profile(
            self, remove_stale_profile, validate_mount, mount_table, umount,
            stage_volume):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        cleanup_token = '10000000-0000-0000-0000-000000000001'
        share = mock.Mock(
            share_id='20000000-0000-0000-0000-000000000002',
            instance_uuid=instance.uuid,
            tag='project-data',
            export_location='server:/project-data',
            share_proto='NFS')
        driver._write_share_journal(
            instance, share, cleanup_token, 'mounted')
        share_device = driver._share_device_name(share)
        share_path = driver._share_mount_path(instance, share)
        mount_table.return_value = {
            os.path.realpath(share_path): {
                'device': share.export_location,
                'fstype': 'nfs',
                'opts': frozenset(('rw', 'nosuid', 'nodev')),
            },
        }
        devices = {
            'root': {'type': 'disk', 'path': '/'},
            share_device: {
                'type': 'disk',
                'source': share_path,
                'path': '/mnt/manila/project-data',
                'readonly': 'false',
                'recursive': 'true',
            },
        }
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2',
            cleanup_token=cleanup_token,
            migration_uuid='40000000-0000-0000-0000-000000000004',
            idmap_base=1065536,
            idmap_size=65536,
            full_checkpoint_verified=True,
            source_profile=jsonutils.dumps({
                'config': {
                    'migration.stateful': 'true',
                    'migration.incremental.memory': 'false',
                    'security.idmap.base': '1065536',
                    'security.idmap.size': '65536',
                    'user.openstack.uuid': instance.uuid,
                    driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
                },
                'devices': devices,
            }))
        profile = mock.Mock(
            config={
                'environment.product_name': 'OpenStack Nova',
                'security.idmap.base': '1065536',
                'security.idmap.size': '65536',
                'user.openstack.uuid': instance.uuid,
                driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
            },
            devices=copy.deepcopy(devices),
            used_by=[])
        self.client.profiles.get.return_value = profile
        connection_info = {
            'serial': 'volume-id',
            'driver_volume_type': 'rbd',
            'data': {},
        }
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        self.assertRaisesRegex(
            RuntimeError, 'connect failed',
            incus_driver.pre_live_migration,
            ctx, instance, {'block_device_mapping': [{
                'boot_index': None,
                'attachment_id':
                    '30000000-0000-0000-0000-000000000003',
                'mount_device': '/dev/vdb',
                'connection_info': connection_info,
            }]}, [], None, data)

        self.assertEqual([], driver._share_journal_records(instance))
        self.assertIn(share_device, profile.devices)
        self.assertEqual(
            'true', profile.config[driver.CLEANUP_RECOVERY_KEY])
        umount.assert_called_once_with(
            os.path.realpath(share_path),
            self.CONF.incus.share_unmount_timeout)
        profile.delete.assert_not_called()
        self._retire_migration_attempt.assert_not_called()

    def test_cleanup_pre_live_destination_retries_vifs_idempotently(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        cleanup_token = '10000000-0000-0000-0000-000000000001'
        data = migrate_data.IncusLiveMigrateData(
            cleanup_token=cleanup_token,
            idmap_base=1065536,
            idmap_size=65536)
        vifs = [
            network_model.VIF(id='first-vif'),
            network_model.VIF(id='second-vif'),
        ]
        profile = mock.Mock(
            config={
                'environment.product_name': 'OpenStack Nova',
                'security.idmap.base': '1065536',
                'security.idmap.size': '65536',
                'user.openstack.uuid': instance.uuid,
                driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
            },
            devices={'root': {'type': 'disk', 'path': '/'}},
            used_by=[])
        self.client.instances.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        self.client.profiles.get.return_value = profile
        self.vif_driver.unplug.side_effect = [
            RuntimeError('first cleanup failed'),
            None,
            None,
            None,
        ]
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.network_api = mock.Mock()
        incus_driver.network_api.get_instance_nw_info.return_value = vifs

        self.assertFalse(
            incus_driver.cleanup_pre_live_migration_destination(
                ctx, instance, data))
        self.assertEqual(
            'true', profile.config[driver.CLEANUP_RECOVERY_KEY])
        profile.delete.assert_not_called()
        self._retire_migration_attempt.assert_not_called()

        self.assertTrue(
            incus_driver.cleanup_pre_live_migration_destination(
                ctx, instance, data))

        profile.delete.assert_called_once_with()
        self._retire_migration_attempt.assert_called_once_with(
            self.client, instance, cleanup_token, 1065536, 65536)
        self.assertEqual(4, self.vif_driver.unplug.call_count)

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
        self.client.profiles.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))

        incus_driver.detach_volume(
            ctx, connection_info, instance, '/dev/vdb')

        self.client.profiles.get.assert_called_once_with(instance.name)
        self.assertIs(
            True,
            connection_info['data'][driver._PRE_LIVE_DISCONNECTED_KEY])

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
    def test_live_migration_accepts_legacy_destination_round_trip(
            self, get_remote):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        self.CONF.incus.migration_address = 'https://192.0.2.10:8443'
        self.CONF.incus.project = 'nova'
        container = mock.Mock()
        container.status = 'Stopped'
        container.config = {
            'migration.incremental.memory': 'false',
            'user.openstack.uuid': instance.uuid,
            'volatile.idmap.base': '1065536',
        }
        container.expanded_config = {
            'migration.incremental.memory': 'false',
            'migration.stateful': 'true',
        }
        container.profiles = [instance.name]
        container.generate_migration_data.return_value = {
            'default': ['test'],
            'source': {
                'operation': (
                    'http+unix://incus/1.0/operations/'
                    '20000000-0000-0000-0000-000000000002'),
            },
        }
        self.client.instances.get.return_value = container
        profile = mock.Mock()
        profile.config = {
            'migration.incremental.memory': 'false',
            'migration.stateful': 'true',
            'user.openstack.uuid': instance.uuid,
        }
        profile.devices = {'root': {'type': 'disk', 'path': '/'}}
        self.client.profiles.get.return_value = profile
        remote = get_remote.return_value
        remote.profiles.get.return_value = mock.Mock(
            config={
                'environment.product_name': 'OpenStack Nova',
                'migration.incremental.memory': 'false',
                driver.MIGRATION_CLEANUP_TOKEN_KEY:
                    '10000000-0000-0000-0000-000000000001',
            },
            used_by=[])
        post = mock.Mock()
        recover = mock.Mock()
        source_data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2',
            cleanup_token='10000000-0000-0000-0000-000000000001',
            migration_uuid='50000000-0000-0000-0000-000000000005',
            idmap_base=1065536,
            idmap_size=65536,
            full_checkpoint_verified=True)
        legacy_primitive = source_data.obj_to_primitive(target_version='1.5')
        data = migrate_data.IncusLiveMigrateData(
            **legacy_primitive['nova_object.data'])
        self.assertFalse(data.obj_attr_is_set('full_checkpoint_verified'))
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        destination_claim = mock.Mock(
            host_id='30000000-0000-0000-0000-000000000003',
            materialization_id=data.cleanup_token)
        source_claim = mock.Mock(
            materialization_id=(
                '40000000-0000-0000-0000-000000000004'))
        incus_driver._idmap_claim_from_local_config = mock.Mock(
            return_value=(mock.sentinel.assignment, destination_claim))
        incus_driver._instance_local_idmap_claim = mock.Mock(
            return_value=(mock.sentinel.assignment, source_claim))
        incus_driver._resume_idmap_materialization = mock.Mock(
            return_value=None)
        incus_driver.live_migration(
            ctx, instance, 'destination', post, recover,
            migrate_data=data)

        payload = remote.instances.create.call_args.args[0]
        remote.profiles.create.assert_not_called()
        self.assertEqual([instance.name], payload['profiles'])
        self.assertNotIn('default', payload)
        self.assertEqual(
            instance.uuid, payload['config']['user.openstack.uuid'])
        self.assertIs(True, payload['source']['live'])
        self.assertEqual(
            'https://192.0.2.10:8443/1.0/operations/'
            '20000000-0000-0000-0000-000000000002',
            payload['source']['operation'])
        remote.instances.create.assert_called_once_with(payload, wait=True)
        incus_driver._resume_idmap_materialization.assert_called_once_with(
            remote, instance, data.cleanup_token,
            destination_claim.host_id, mock.ANY, 1065536, 65536)
        post.assert_called_once_with(
            ctx, instance, 'destination', False, data)
        recover.assert_not_called()

    @mock.patch.object(driver, '_settle_instance_migration_operations')
    @mock.patch.object(
        driver, '_abort_migration_attempt',
        return_value={'state': 'aborted'})
    @mock.patch('nova.virt.incus.driver._migration_client')
    def test_live_migration_rechecks_full_checkpoint_before_generate(
            self, get_remote, abort_attempt, settle_operations):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        container = mock.Mock(
            config={
                'migration.incremental.memory': 'false',
                'user.openstack.uuid': instance.uuid,
                'volatile.idmap.base': '1065536',
            },
            expanded_config={
                'migration.incremental.memory': 'false',
                'migration.stateful': 'true',
            },
            profiles=[instance.name])
        self.client.instances.get.return_value = container
        valid_profile = mock.Mock(
            config={
                'migration.incremental.memory': 'false',
                'migration.stateful': 'true',
                'user.openstack.uuid': instance.uuid,
            },
            devices={'root': {'type': 'disk', 'path': '/'}})
        mutated_profile = mock.Mock(
            config={
                'migration.incremental.memory': 'true',
                'migration.stateful': 'true',
                'user.openstack.uuid': instance.uuid,
            },
            devices={'root': {'type': 'disk', 'path': '/'}})
        self.client.profiles.get.side_effect = [
            valid_profile, mutated_profile]
        remote = get_remote.return_value
        remote.profiles.get.return_value = mock.Mock(
            config={
                'environment.product_name': 'OpenStack Nova',
                'migration.incremental.memory': 'false',
            },
            used_by=[])
        post = mock.Mock()
        recover = mock.Mock()
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            cleanup_token='10000000-0000-0000-0000-000000000001',
            idmap_base=1065536,
            idmap_size=65536)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        destination_claim = mock.Mock(
            host_id='30000000-0000-0000-0000-000000000003',
            materialization_id=data.cleanup_token)
        source_claim = mock.Mock(
            materialization_id='40000000-0000-0000-0000-000000000004')
        incus_driver._idmap_claim_from_local_config = mock.Mock(
            return_value=(mock.sentinel.assignment, destination_claim))
        incus_driver._instance_local_idmap_claim = mock.Mock(
            return_value=(mock.sentinel.assignment, source_claim))
        incus_driver._resume_idmap_materialization = mock.Mock(
            return_value=None)

        incus_driver.live_migration(
            ctx, instance, 'destination', post, recover, migrate_data=data)

        self.assertEqual([
            mock.call(instance.name),
            mock.call(instance.name),
        ], self.client.profiles.get.call_args_list)
        incus_driver._resume_idmap_materialization.assert_called_once()
        container.generate_migration_data.assert_not_called()
        abort_attempt.assert_called_once()
        settle_operations.assert_called_once_with(
            self.client, instance, operation_ids=(None,))
        recover.assert_called_once_with(
            ctx, instance, 'destination', data)
        post.assert_not_called()

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
        container.config = {
            'migration.incremental.memory': 'false',
            'user.openstack.uuid': instance.uuid,
            'volatile.idmap.base': '1065536',
        }
        container.expanded_config = {
            'migration.incremental.memory': 'false',
            'migration.stateful': 'true',
        }
        container.profiles = [instance.name]
        container.generate_migration_data.return_value = {
            'source': {
                'operation': (
                    'http+unix://incus/1.0/operations/'
                    '20000000-0000-0000-0000-000000000002'),
            },
        }
        self.client.instances.get.return_value = container
        self.client.profiles.get.return_value = mock.Mock(
            config={
                'migration.incremental.memory': 'false',
                'migration.stateful': 'true',
                'user.openstack.uuid': instance.uuid,
            },
            devices={'root': {'type': 'disk', 'path': '/'}})
        profile = mock.Mock()
        profile.config = {'migration.stateful': 'true'}
        profile.devices = {'root': {'type': 'disk', 'path': '/'}}
        self.client.profiles.get.return_value = profile
        remote = get_remote.return_value
        remote.profiles.get.side_effect = incuscore_exceptions.NotFound(
            MockResponse(404))
        remote.instances.create.side_effect = RuntimeError('restore failed')
        self.client.api.operations.get.return_value.json.return_value = {
            'metadata': {}}
        post = mock.Mock()
        recover = mock.Mock()
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2',
            cleanup_token='10000000-0000-0000-0000-000000000001',
            migration_uuid='50000000-0000-0000-0000-000000000005',
            idmap_base=1065536,
            idmap_size=65536)
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
        container.config = {
            'migration.incremental.memory': 'false',
            'user.openstack.uuid': instance.uuid,
            'volatile.idmap.base': '1065536',
        }
        container.expanded_config = {
            'migration.incremental.memory': 'false',
            'migration.stateful': 'true',
        }
        container.profiles = [instance.name]
        container.generate_migration_data.return_value = {
            'source': {
                'operation': (
                    'http+unix://incus/1.0/operations/'
                    '20000000-0000-0000-0000-000000000002'),
            },
        }
        self.client.instances.get.return_value = container
        self.client.profiles.get.return_value = mock.Mock(
            config={
                'migration.incremental.memory': 'false',
                'migration.stateful': 'true',
                'user.openstack.uuid': instance.uuid,
            },
            devices={'root': {'type': 'disk', 'path': '/'}})
        remote = get_remote.return_value
        remote.profiles.get.return_value = mock.Mock(
            config={
                'environment.product_name': 'OpenStack Nova',
                'migration.incremental.memory': 'false',
                driver.MIGRATION_CLEANUP_TOKEN_KEY:
                    '10000000-0000-0000-0000-000000000001',
            },
            used_by=[])
        remote.instances.create.side_effect = RuntimeError('restore failed')
        self.client.api.operations = mock.MagicMock()
        self.client.api.operations.get.return_value.json.return_value = {
            'metadata': {}}
        operation = self.client.api.operations[
            '20000000-0000-0000-0000-000000000002']
        operation.get.return_value.json.return_value = {
            'metadata': {
                'id': '20000000-0000-0000-0000-000000000002',
                'status_code': 400,
            }}
        post = mock.Mock()
        recover = mock.Mock()
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            destination_architecture='x86_64',
            destination_kernel_version='6.8.0-test',
            destination_server_version='7.2',
            cleanup_token='10000000-0000-0000-0000-000000000001',
            migration_uuid='50000000-0000-0000-0000-000000000005',
            idmap_base=1065536,
            idmap_size=65536)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        destination_claim = mock.Mock(
            host_id='30000000-0000-0000-0000-000000000003',
            materialization_id=data.cleanup_token)
        source_claim = mock.Mock(
            materialization_id='40000000-0000-0000-0000-000000000004')
        incus_driver._idmap_claim_from_local_config = mock.Mock(
            return_value=(mock.sentinel.assignment, destination_claim))
        incus_driver._instance_local_idmap_claim = mock.Mock(
            return_value=(mock.sentinel.assignment, source_claim))
        incus_driver._resume_idmap_materialization = mock.Mock(
            return_value=None)

        incus_driver.live_migration(
            ctx, instance, 'destination', post, recover,
            migrate_data=data)

        remote.profiles.create.assert_not_called()
        remote.instances.create.assert_called_once()
        recover.assert_called_once_with(
            ctx, instance, 'destination', data)
        post.assert_not_called()

    @mock.patch.object(driver, '_settle_instance_migration_operations')
    @mock.patch.object(
        driver, '_abort_migration_attempt',
        return_value={'state': 'aborted'})
    @mock.patch.object(driver, '_migration_client')
    def test_rollback_live_migration_source_stays_fenced(
            self, migration_client, abort_attempt, settle_operations):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        container = mock.Mock(status='Stopped')
        self.client.instances.get.return_value = container
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            cleanup_token='10000000-0000-0000-0000-000000000001',
            source_operation_id=None,
            idmap_base=1065536,
            idmap_size=65536)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.rollback_live_migration_at_source(
            ctx, instance, data)

        abort_attempt.assert_called_once()
        self.assertEqual(
            (migration_client.return_value, instance, data.cleanup_token,
             data.idmap_base, data.idmap_size),
            abort_attempt.call_args.args)
        self.assertTrue(callable(
            abort_attempt.call_args.kwargs['target_cleanup']))
        settle_operations.assert_called_once_with(
            self.client, instance, operation_ids=(None,))
        container.sync.assert_called_once_with()
        container.stop.assert_not_called()
        container.start.assert_not_called()

    @mock.patch.object(driver, '_settle_instance_migration_operations')
    @mock.patch.object(
        driver, '_abort_migration_attempt',
        return_value={'state': 'aborted'})
    @mock.patch.object(driver, '_migration_client')
    def test_rollback_live_migration_source_fences_running_container(
            self, migration_client, abort_attempt, settle_operations):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        container = mock.Mock(status='Running')
        self.client.instances.get.return_value = container
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            cleanup_token='10000000-0000-0000-0000-000000000001',
            source_operation_id=None,
            idmap_base=1065536,
            idmap_size=65536)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.rollback_live_migration_at_source(
            ctx, instance, data)

        abort_attempt.assert_called_once()
        self.assertEqual(
            (migration_client.return_value, instance, data.cleanup_token,
             data.idmap_base, data.idmap_size),
            abort_attempt.call_args.args)
        self.assertTrue(callable(
            abort_attempt.call_args.kwargs['target_cleanup']))
        settle_operations.assert_called_once_with(
            self.client, instance, operation_ids=(None,))
        container.sync.assert_called_once_with()
        container.stop.assert_called_once_with(
            timeout=-1, force=True, wait=True)
        container.start.assert_not_called()

    @mock.patch.object(driver, '_restore_source_storage_ownership')
    @mock.patch.object(driver, '_settle_instance_migration_operations')
    @mock.patch('nova.virt.incus.driver._migration_client')
    def test_finalize_live_migration_rollback_reasserts_original_vifs(
            self, get_remote, settle_operations, restore_ownership):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        cleanup_token = '10000000-0000-0000-0000-000000000001'
        source_vif = network_model.VIF(id='test-vif')
        vif_data = nova_migrate_data.VIFMigrateData(source_vif=source_vif)
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            cleanup_token=cleanup_token,
            migration_uuid='40000000-0000-0000-0000-000000000004',
            source_operation_id=None,
            idmap_base=1065536,
            idmap_size=65536,
            vifs=[vif_data])
        remote = get_remote.return_value
        remote.instances.get.side_effect = incuscore_exceptions.NotFound(
            MockResponse(404))
        cleanup_profile = mock.Mock(
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
                driver.MIGRATION_CLEANUP_COMPLETE_KEY: cleanup_token,
            },
            devices={},
            used_by=[])
        remote.profiles.get.return_value = cleanup_profile
        source_profile = mock.Mock(config={}, devices={})
        self.client.profiles.get.return_value = source_profile
        self.client.instances.get.return_value = mock.Mock(status='Running')
        active_vif = network_model.VIF(id='test-vif', active=True)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.network_api.get_instance_nw_info = mock.Mock(
            return_value=network_model.NetworkInfo([active_vif]))
        incus_driver.vif_driver.reassert = mock.Mock()
        incus_driver.unplug_vifs = mock.Mock()
        incus_driver._validate_remote_cleanup_acknowledgement = mock.Mock()

        incus_driver.finalize_live_migration_rollback(
            ctx, instance, data)

        incus_driver.vif_driver.reassert.assert_called_once_with(
            instance, source_vif)
        incus_driver.unplug_vifs.assert_not_called()
        settle_operations.assert_called_once_with(
            self.client, instance, operation_ids=(None,))
        restore_ownership.assert_called_once_with(self.client, instance)
        cleanup_profile.delete.assert_called_once_with()
        self.assertEqual(
            2,
            incus_driver._validate_remote_cleanup_acknowledgement.call_count)
        source_profile.save.assert_called_once_with(wait=True)
        self.assertEqual(
            cleanup_token,
            source_profile.config[driver.MIGRATION_CLEANUP_TOKEN_KEY])
        self.assertEqual(
            data.destination_address,
            source_profile.config[driver.MIGRATION_DESTINATION_KEY])
        self.assertEqual(
            2, remote.profiles.get.call_count)

    @mock.patch.object(driver, '_restore_source_storage_ownership')
    @mock.patch.object(driver, '_settle_instance_migration_operations')
    @mock.patch('nova.virt.incus.driver._migration_client')
    def test_finalize_live_rollback_accepts_retired_target_attempt(
            self, get_remote, settle_operations, restore_ownership):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        cleanup_token = '10000000-0000-0000-0000-000000000001'
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            cleanup_token=cleanup_token,
            migration_uuid='40000000-0000-0000-0000-000000000004',
            source_operation_id=None,
            idmap_base=1065536,
            idmap_size=65536)
        remote = get_remote.return_value
        self._get_migration_attempt.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        remote.instances.get.side_effect = incuscore_exceptions.NotFound(
            MockResponse(404))
        cleanup_profile = mock.Mock(
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
                driver.MIGRATION_CLEANUP_COMPLETE_KEY: cleanup_token,
            },
            devices={}, used_by=[])
        remote.profiles.get.return_value = cleanup_profile
        source_profile = mock.Mock(config={}, devices={})
        self.client.profiles.get.return_value = source_profile
        self.client.instances.get.return_value = mock.Mock(status='Running')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.network_api.get_instance_nw_info = mock.Mock(
            return_value=network_model.NetworkInfo())
        incus_driver._validate_remote_cleanup_acknowledgement = mock.Mock()

        incus_driver.finalize_live_migration_rollback(ctx, instance, data)

        cleanup_profile.delete.assert_called_once_with()
        restore_ownership.assert_called_once_with(self.client, instance)
        source_profile.save.assert_called_once_with(wait=True)
        self._retire_migration_attempt.assert_called_once_with(
            remote, instance, cleanup_token, 1065536, 65536)

    @mock.patch.object(driver, '_restore_source_storage_ownership')
    @mock.patch.object(driver, '_settle_instance_migration_operations')
    @mock.patch('nova.virt.incus.driver._migration_client')
    def test_finalize_live_rollback_rebuilds_veth_before_stopped_start(
            self, get_remote, settle_operations, restore_ownership):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        cleanup_token = '10000000-0000-0000-0000-000000000001'
        source_vif = network_model.VIF(id='test-vif')
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            cleanup_token=cleanup_token,
            migration_uuid='40000000-0000-0000-0000-000000000004',
            source_operation_id=None, idmap_base=1065536, idmap_size=65536,
            vifs=[nova_migrate_data.VIFMigrateData(source_vif=source_vif)])
        remote = get_remote.return_value
        remote.instances.get.side_effect = incuscore_exceptions.NotFound(
            MockResponse(404))
        cleanup_profile = mock.Mock(
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
                driver.MIGRATION_CLEANUP_COMPLETE_KEY: cleanup_token,
            }, devices={}, used_by=[])
        remote.profiles.get.return_value = cleanup_profile
        source_profile = mock.Mock(config={}, devices={})
        self.client.profiles.get.return_value = source_profile
        container = mock.Mock(status='Stopped')
        self.client.instances.get.return_value = container
        active_vif = network_model.VIF(id='test-vif', active=True)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.network_api.get_instance_nw_info = mock.Mock(
            return_value=network_model.NetworkInfo([active_vif]))
        calls = []
        incus_driver._refresh_vifs = mock.Mock(
            side_effect=lambda *args: calls.append('refresh'))
        incus_driver._start_instance_with_idmap = mock.Mock(
            side_effect=lambda *args: calls.append('start'))
        incus_driver.vif_driver.reassert = mock.Mock(
            side_effect=lambda *args: calls.append('reassert'))
        incus_driver._validate_remote_cleanup_acknowledgement = mock.Mock()

        incus_driver.finalize_live_migration_rollback(ctx, instance, data)

        self.assertEqual(['refresh', 'start', 'reassert'], calls)
        incus_driver._refresh_vifs.assert_called_once_with(
            instance, network_model.NetworkInfo([source_vif]))

    @mock.patch.object(driver, '_restore_source_storage_ownership')
    @mock.patch.object(driver, '_settle_instance_migration_operations')
    @mock.patch('nova.virt.incus.driver._migration_client')
    def test_finalize_live_rollback_preserves_stopped_pending_delete(
            self, get_remote, settle_operations, restore_ownership):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        cleanup_token = '10000000-0000-0000-0000-000000000001'
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            cleanup_token=cleanup_token,
            migration_uuid='40000000-0000-0000-0000-000000000004',
            source_operation_id=None, idmap_base=1065536, idmap_size=65536)
        remote = get_remote.return_value
        remote.instances.get.side_effect = incuscore_exceptions.NotFound(
            MockResponse(404))
        cleanup_profile = mock.Mock(
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
                driver.MIGRATION_CLEANUP_COMPLETE_KEY: cleanup_token,
            }, devices={}, used_by=[])
        remote.profiles.get.return_value = cleanup_profile
        source_profile = mock.Mock(config={}, devices={})
        self.client.profiles.get.return_value = source_profile
        self.client.instances.get.return_value = mock.Mock(status='Stopped')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.idmap_allocator = mock.Mock()
        incus_driver.idmap_allocator.get_release_intent.return_value = (
            mock.Mock(instance_name=instance.name))
        incus_driver._refresh_vifs = mock.Mock()
        incus_driver._start_instance_with_idmap = mock.Mock()
        incus_driver._validate_remote_cleanup_acknowledgement = mock.Mock()

        incus_driver.finalize_live_migration_rollback(ctx, instance, data)

        incus_driver._refresh_vifs.assert_not_called()
        incus_driver._start_instance_with_idmap.assert_not_called()
        self.assertEqual(
            cleanup_token,
            source_profile.config[driver.MIGRATION_ROLLBACK_COMPLETE_KEY])

    @mock.patch.object(driver, '_restore_source_storage_ownership')
    @mock.patch.object(driver, '_settle_instance_migration_operations')
    @mock.patch.object(driver, '_migration_client')
    def test_finalize_rollback_waits_out_mid_cleanup_acknowledgement(
            self, get_remote, settle_operations, restore_ownership):
        """A mid-cleanup destination profile is not-ready, not terminal."""
        self.flags(migration_finish_retry_interval=0, group='incus')
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        cleanup_token = '10000000-0000-0000-0000-000000000001'
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            cleanup_token=cleanup_token,
            migration_uuid='40000000-0000-0000-0000-000000000004',
            source_operation_id=None,
            idmap_base=1065536,
            idmap_size=65536)
        remote = get_remote.return_value
        remote.instances.get.side_effect = incuscore_exceptions.NotFound(
            MockResponse(404))
        cleanup_profile = mock.Mock(
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
                driver.MIGRATION_CLEANUP_COMPLETE_KEY: cleanup_token,
            },
            devices={},
            used_by=[])
        remote.profiles.get.return_value = cleanup_profile
        source_profile = mock.Mock(config={}, devices={})
        self.client.profiles.get.return_value = source_profile
        self.client.instances.get.return_value = mock.Mock(status='Running')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.network_api.get_instance_nw_info = mock.Mock(
            return_value=network_model.NetworkInfo())
        incus_driver._validate_remote_cleanup_acknowledgement = mock.Mock(
            side_effect=[
                exception.MigrationError(
                    reason='destination rollback still stripping devices'),
                None,
                None,
            ])

        incus_driver.finalize_live_migration_rollback(ctx, instance, data)

        self.assertEqual(
            3,
            incus_driver._validate_remote_cleanup_acknowledgement.call_count)
        cleanup_profile.delete.assert_called_once_with()

    @mock.patch.object(driver, '_abort_migration_attempt')
    @mock.patch.object(driver, '_cleanup_profile_share_mounts')
    def test_rollback_live_migration_destination_cleans_profile_last(
            self, cleanup_shares, abort_attempt):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        profile = self.client.profiles.get.return_value
        target = self.client.instances.get.return_value
        data = migrate_data.IncusLiveMigrateData(
            cleanup_token='10000000-0000-0000-0000-000000000001',
            idmap_base=1065536,
            idmap_size=65536)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        fixture = self._configure_exact_idmap_release(
            incus_driver, instance, target, profile,
            outcome='detached', idmap_base=1065536)
        tokenized_delete = fixture[-1]
        incus_driver._cleanup = mock.Mock()
        incus_driver._acknowledge_cleanup_profile = mock.Mock()

        incus_driver.rollback_live_migration_at_destination(
            ctx, instance, mock.sentinel.network_info,
            {'block_device_mapping': []}, destroy_disks=False,
            migrate_data=data)

        abort_attempt.assert_called_once()
        self.assertEqual(
            (self.client, instance, data.cleanup_token,
             data.idmap_base, data.idmap_size),
            abort_attempt.call_args.args)
        self.assertTrue(callable(
            abort_attempt.call_args.kwargs['target_cleanup']))
        tokenized_delete.assert_called_once_with(
            target, instance, mock.ANY, client=self.client)
        target.delete.assert_called_once_with(wait=True)
        cleanup_shares.assert_called_once_with(profile, instance)
        incus_driver._cleanup.assert_called_once_with(
            ctx, instance, mock.sentinel.network_info,
            destroy_disks=False, destroy_vifs=True, delete_profile=False)
        incus_driver._acknowledge_cleanup_profile.assert_called_once_with(
            instance, data.cleanup_token)
        incus_driver.idmap_allocator.request_release.assert_not_called()
        incus_driver.idmap_allocator.release.assert_not_called()
        profile.delete.assert_not_called()

    @mock.patch.object(driver, '_retire_migration_attempt')
    @mock.patch.object(driver, '_cleanup_share_journal_mounts')
    @mock.patch.object(
        driver, '_abort_migration_attempt',
        side_effect=incus_api_exception(404, 'attempt not found'))
    def test_rollback_live_pre_receive_needs_no_ack_profile(
            self, abort_attempt, cleanup_journals, retire_attempt):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(ctx, name='test')
        self.client.profiles.get.side_effect = incus_api_exception(
            404, 'profile not found')
        data = migrate_data.IncusLiveMigrateData(
            cleanup_token='10000000-0000-0000-0000-000000000001',
            source_operation_id=None,
            destination_operation_id=None,
            idmap_base=1065536,
            idmap_size=65536)
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver._cleanup = mock.Mock()
        incus_driver._acknowledge_cleanup_profile = mock.Mock()

        incus_driver.rollback_live_migration_at_destination(
            ctx, instance, mock.sentinel.network_info,
            {'block_device_mapping': []}, destroy_disks=False,
            migrate_data=data)

        abort_attempt.assert_called_once()
        cleanup_journals.assert_called_once_with(
            instance, operation_token=data.cleanup_token)
        retire_attempt.assert_not_called()
        incus_driver._cleanup.assert_not_called()
        incus_driver._acknowledge_cleanup_profile.assert_not_called()

    @mock.patch.object(driver, '_migration_client')
    def test_confirm_migration(self, migration_client):
        ctx = context.get_admin_context()
        migration = mock.Mock(
            source_compute='compute', dest_compute='compute')
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = []
        profile = mock.Mock()
        cleanup_token = '10000000-0000-0000-0000-000000000001'
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
            driver.MIGRATION_DESTINATION_KEY:
                'https://192.0.2.20:8443',
            'security.idmap.size': '65536',
        }
        container = mock.Mock(
            status='Stopped',
            config={
                'volatile.idmap.base': '1065536',
                'volatile.migration.storage_handover': 'pending',
            },
            expanded_devices={
                'root': {'type': 'disk', 'path': '/', 'pool': 'ceph-root'},
            })
        protected_source = mock.Mock(
            status='Stopped',
            config={
                'volatile.idmap.base': '1065536',
                'volatile.migration.storage_handover': 'pending',
                'volatile.migration.storage_handover_role': 'source',
                'volatile.migration.storage_delete_protection': 'true',
            },
            expanded_devices=container.expanded_devices)
        self.client.profiles.get.return_value = profile
        self.client.instances.get.side_effect = [
            container, container, protected_source]
        self.client.storage_pools.get.return_value.driver = 'ceph'
        self.client.host_info['api_extensions'].append(
            driver.INCUS_STORAGE_HANDOVER_EXTENSION)
        self.client.api.instances = mock.MagicMock()
        self.client.api.operations.get.return_value.json.return_value = {
            'metadata': {}}
        remote = migration_client.return_value
        remote.api.instances = mock.MagicMock()
        remote.host_info = {
            'api_extensions': [
                driver.INCUS_STORAGE_HANDOVER_EXTENSION,
                driver.INCUS_STORAGE_HANDOVER_PROOF_EXTENSION,
            ]}
        destination = mock.Mock(
            status='Stopped',
            config={
                'user.openstack.uuid': instance.uuid,
                driver.MIGRATION_RECEIVE_COMPLETE_KEY: 'true',
            },
            expanded_devices={
                'root': {'type': 'disk', 'path': '/', 'pool': 'ceph-root'},
            })
        owned_destination = mock.Mock(
            status='Stopped',
            config={'user.openstack.uuid': instance.uuid},
            expanded_devices=destination.expanded_devices)
        remote.instances.get.side_effect = [
            destination, owned_destination]
        remote.storage_pools.get.return_value.driver = 'ceph'

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        self._configure_exact_idmap_release(
            incus_driver, instance, container, profile,
            outcome='detached', idmap_base=1065536)
        incus_driver._cleanup = mock.Mock()
        ordered = mock.Mock()
        ordered.attach_mock(profile.save, 'journal')
        ordered.attach_mock(container.delete, 'delete')

        incus_driver.confirm_migration(
            ctx, migration, instance, network_info)

        self.assertEqual(
            'true', profile.config[driver.CLEANUP_RECOVERY_KEY])
        profile.save.assert_called_once_with(wait=True)
        self.assertEqual(
            [mock.call.journal(wait=True), mock.call.delete(wait=True)],
            ordered.mock_calls)
        container.delete.assert_called_once_with(wait=True)
        self.client.api.instances[instance.name][
            'storage-handover'].put.assert_called_once_with(
                params={'project': self.CONF.incus.project},
                json={'state': 'protected'})
        remote.api.instances[instance.name][
            'storage-handover'].put.assert_called_once_with(
                params={'project': self.CONF.incus.project},
                json={
                    'state': 'owned',
                    'migration_attempt': cleanup_token,
                    'operation_uuid':
                        '20000000-0000-0000-0000-000000000002',
                })
        incus_driver._cleanup.assert_called_once_with(
            ctx, instance, network_info,
            destroy_vifs=True, delete_profile=True)
        self._finalize_committed_migration_attempt.assert_called_once_with(
            remote, instance, cleanup_token, 1065536, 65536)

    @mock.patch.object(driver, '_migration_client')
    def test_confirm_migration_source_absent_converges_protected_target(
            self, migration_client):
        ctx = context.get_admin_context()
        migration = mock.Mock(dest_compute='compute-dest')
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        absent = incuscore_exceptions.NotFound(MockResponse(404))
        self.client.profiles.get.side_effect = absent
        self.client.instances.get.side_effect = absent

        cleanup_token = '10000000-0000-0000-0000-000000000001'
        operation_id = '20000000-0000-0000-0000-000000000002'
        remote = migration_client.return_value
        remote.host_info = {
            'api_extensions': [
                driver.INCUS_STORAGE_HANDOVER_EXTENSION,
                driver.INCUS_STORAGE_HANDOVER_PROOF_EXTENSION,
            ]}
        remote.api.instances = mock.MagicMock()
        remote.storage_pools.get.return_value.driver = 'ceph'
        protected_destination = mock.Mock(
            config={
                'user.openstack.uuid': instance.uuid,
                'volatile.idmap.base': '1065536',
                'volatile.migration.storage_handover_role': 'target',
                'volatile.migration.storage_delete_protection': 'true',
                driver.MIGRATION_RECEIVE_COMPLETE_KEY: 'true',
            },
            expanded_devices={
                'root': {'type': 'disk', 'path': '/', 'pool': 'ceph-root'},
            })
        owned_destination = mock.Mock(
            config={'user.openstack.uuid': instance.uuid},
            expanded_devices=protected_destination.expanded_devices)
        remote.instances.get.side_effect = [
            protected_destination, owned_destination]
        remote.profiles.get.return_value = mock.Mock(
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
                driver.MIGRATION_TARGET_OPERATION_KEY: operation_id,
                'security.idmap.size': '65536',
            })

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        self._configure_exact_idmap_release(
            incus_driver, instance, outcome='detached',
            idmap_base=1065536)
        incus_driver.confirm_migration(ctx, migration, instance, [])

        remote.api.instances[instance.name][
            'storage-handover'].put.assert_called_once_with(
                params={'project': self.CONF.incus.project},
                json={
                    'state': 'owned',
                    'migration_attempt': cleanup_token,
                    'operation_uuid': operation_id,
                })
        self._finalize_committed_migration_attempt.assert_called_once_with(
            remote, instance, cleanup_token, 1065536, 65536)

    @mock.patch.object(
        driver, '_set_storage_handover_state',
        side_effect=RuntimeError('lost ownership response'))
    def test_confirm_migration_source_absent_queues_ownership_recovery(
            self, set_handover):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        cleanup_token = '10000000-0000-0000-0000-000000000001'
        profile = mock.Mock(config={
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_CLEANUP_TOKEN_KEY: cleanup_token,
            driver.MIGRATION_TARGET_OPERATION_KEY:
                '20000000-0000-0000-0000-000000000002',
            'security.idmap.size': '65536',
        })
        self.client.profiles.get.return_value = profile
        self.client.storage_pools.get.return_value.driver = 'ceph'
        self.client.instances.get.return_value = mock.Mock(
            status='Stopped',
            config={
                'volatile.idmap.base': '1065536',
                'volatile.migration.storage_handover_role': 'target',
                'volatile.migration.storage_delete_protection': 'true',
                driver.MIGRATION_RECEIVE_COMPLETE_KEY: 'true',
            },
            expanded_config={'user.openstack.uuid': instance.uuid},
            expanded_devices={
                'root': {'type': 'disk', 'path': '/', 'pool': 'ceph-root'},
            })

        driver._converge_migration_target_ownership(
            self.client, instance)

        self.assertEqual(
            'stopped', profile.config[driver.MIGRATION_RECOVERY_KEY])
        profile.save.assert_called_once_with(wait=True)
        self._finalize_committed_migration_attempt.assert_not_called()

    @mock.patch.object(driver, '_migration_client')
    def test_confirm_migration_source_absent_verifies_owned_target(
            self, migration_client):
        ctx = context.get_admin_context()
        migration = mock.Mock(dest_compute='compute-dest')
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        absent = incuscore_exceptions.NotFound(MockResponse(404))
        self.client.profiles.get.side_effect = absent
        self.client.instances.get.side_effect = absent

        remote = migration_client.return_value
        remote.api.instances = mock.MagicMock()
        remote.storage_pools.get.return_value.driver = 'ceph'
        remote.instances.get.return_value = mock.Mock(
            config={'user.openstack.uuid': instance.uuid},
            expanded_devices={
                'root': {'type': 'disk', 'path': '/', 'pool': 'ceph-root'},
            })
        remote.profiles.get.return_value = mock.Mock(config={
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        })

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        self._configure_exact_idmap_release(
            incus_driver, instance, outcome='detached',
            idmap_base=1065536)
        incus_driver.confirm_migration(ctx, migration, instance, [])

        remote.api.instances[instance.name][
            'storage-handover'].put.assert_not_called()
        self._finalize_committed_migration_attempt.assert_not_called()

    @mock.patch.object(driver, '_migration_client')
    def test_confirm_migration_source_record_absent_cleans_retained_profile(
            self, migration_client):
        ctx = context.get_admin_context()
        migration = mock.Mock(dest_compute='compute-dest')
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        source_profile = mock.Mock(config={
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
            driver.MIGRATION_DESTINATION_KEY: 'https://192.0.2.20:8443',
        })
        self.client.profiles.get.return_value = source_profile
        self.client.instances.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))

        remote = migration_client.return_value
        remote.api.instances = mock.MagicMock()
        remote.storage_pools.get.return_value.driver = 'ceph'
        remote.instances.get.return_value = mock.Mock(
            config={},
            expanded_config={'user.openstack.uuid': instance.uuid},
            expanded_devices={
                'root': {'type': 'disk', 'path': '/', 'pool': 'ceph-root'},
            })
        remote.profiles.get.return_value = mock.Mock(config={
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        })

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        self._configure_exact_idmap_release(
            incus_driver, instance, source_profile, outcome='detached',
            idmap_base=1065536)
        incus_driver._cleanup = mock.Mock()
        incus_driver.confirm_migration(ctx, migration, instance, [])

        migration_client.assert_called_once_with(
            'https://192.0.2.20:8443')
        incus_driver._cleanup.assert_called_once_with(
            ctx, instance, [], destroy_vifs=True, delete_profile=True)

    def _prepare_post_live_migration_protocol(self, instance):
        migration_client_patcher = mock.patch.object(
            driver, '_migration_client')
        self.patchers.append(migration_client_patcher)
        migration_client = migration_client_patcher.start()
        handover_patcher = mock.patch.object(
            driver, '_set_storage_handover_state', return_value=True)
        self.patchers.append(handover_patcher)
        handover = handover_patcher.start()
        data = migrate_data.IncusLiveMigrateData(
            destination_address='https://192.0.2.20:8443',
            cleanup_token='10000000-0000-0000-0000-000000000001',
            migration_uuid='40000000-0000-0000-0000-000000000004',
            idmap_base=1065536,
            idmap_size=65536)
        profile = self.client.profiles.get.return_value
        profile.config = {
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        }
        return data, migration_client.return_value, handover

    def test_post_live_migration(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        container = mock.Mock()
        container.status = 'Stopped'
        self.client.instances.get.return_value = container
        data, remote, handover = (
            self._prepare_post_live_migration_protocol(instance))

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        profile = self.client.profiles.get.return_value
        self._configure_exact_idmap_release(
            incus_driver, instance, container, profile,
            outcome='detached', idmap_base=1065536)
        ordered = mock.Mock()
        ordered.attach_mock(profile.save, 'journal')
        ordered.attach_mock(container.delete, 'delete')

        incus_driver.post_live_migration(
            ctx, instance, None, migrate_data=data)

        self.assertEqual(
            [
                mock.call(
                    self.client, instance.name, 'protected',
                    container=container),
                mock.call(
                    remote, instance.name, 'owned',
                    migration_attempt=data.cleanup_token,
                    operation_uuid=(
                        '20000000-0000-0000-0000-000000000002')),
            ],
            handover.call_args_list)
        self.assertEqual(
            [mock.call.journal(wait=True), mock.call.delete(wait=True)],
            ordered.mock_calls)
        self.assertEqual(
            'true', profile.config[driver.CLEANUP_RECOVERY_KEY])
        container.stop.assert_not_called()
        container.delete.assert_called_once_with(wait=True)

    def test_post_live_migration_requires_durable_cleanup_journal(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        container = mock.Mock(status='Stopped')
        self.client.instances.get.return_value = container
        data, _remote, handover = (
            self._prepare_post_live_migration_protocol(instance))
        profile = self.client.profiles.get.return_value
        profile.save.side_effect = RuntimeError('profile write failed')
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        self._configure_exact_idmap_release(
            incus_driver, instance, container, profile,
            outcome='detached', idmap_base=1065536)

        self.assertRaisesRegex(
            RuntimeError, 'profile write failed',
            incus_driver.post_live_migration,
            ctx, instance, None, migrate_data=data)

        handover.assert_not_called()
        container.stop.assert_not_called()
        container.delete.assert_not_called()

    def test_post_live_migration_force_stops_running_source(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        container = mock.Mock(status='Running')
        self.client.instances.get.return_value = container
        data, _remote, _handover = (
            self._prepare_post_live_migration_protocol(instance))

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        self._configure_exact_idmap_release(
            incus_driver, instance, container,
            self.client.profiles.get.return_value,
            outcome='detached', idmap_base=1065536)

        incus_driver.post_live_migration(
            ctx, instance, None, migrate_data=data)

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
        data, _remote, _handover = (
            self._prepare_post_live_migration_protocol(instance))

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        self._configure_exact_idmap_release(
            incus_driver, instance, container,
            self.client.profiles.get.return_value,
            outcome='detached', idmap_base=1065536)

        incus_driver.post_live_migration(
            ctx, instance, None, migrate_data=data)

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
        data, _remote, _handover = (
            self._prepare_post_live_migration_protocol(instance))

        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        self._configure_exact_idmap_release(
            incus_driver, instance, container,
            self.client.profiles.get.return_value,
            outcome='detached', idmap_base=1065536)

        self.assertRaises(
            incuscore_exceptions.LXDAPIException,
            incus_driver.post_live_migration, ctx, instance, None,
            migrate_data=data)

        container.delete.assert_not_called()

    @mock.patch.object(
        driver, '_mapped_rbd_device', return_value='/dev/rbd0')
    def test_post_live_migration_disconnects_source_data_volumes(
            self, mapped_rbd_device):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        container = mock.Mock(status='Stopped')
        self.client.instances.get.return_value = container
        root_volume_id = '10000000-0000-0000-0000-000000000001'
        data_volume_id = '20000000-0000-0000-0000-000000000002'
        root_connection = {
            'driver_volume_type': 'rbd',
            'serial': root_volume_id,
            'data': {'volume_id': root_volume_id},
        }
        data_connection = {
            'driver_volume_type': 'rbd',
            'serial': data_volume_id,
            'data': {
                'volume_id': data_volume_id,
                'name': 'volumes/volume-%s' % data_volume_id,
            },
        }
        block_device_info = {'block_device_mapping': [
            {
                'boot_index': 0,
                'connection_info': root_connection,
                'mount_device': '/dev/sda',
                'attachment_id':
                    '30000000-0000-0000-0000-000000000003',
            },
            {
                'boot_index': None,
                'connection_info': data_connection,
                'mount_device': '/dev/sdb',
                'attachment_id':
                    '40000000-0000-0000-0000-000000000004',
            },
        ]}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        data, _remote, _handover = (
            self._prepare_post_live_migration_protocol(instance))
        self._configure_exact_idmap_release(
            incus_driver, instance, container,
            self.client.profiles.get.return_value,
            outcome='detached', idmap_base=1065536)
        profile = self.client.profiles.get.return_value
        profile.devices = {
            data_volume_id: {
                'type': 'unix-block', 'path': '/dev/sdb',
                'source': '/dev/rbd0', 'required': 'true',
            },
        }
        profile.config[driver._volume_device_info_key(data_volume_id)] = (
            driver._serialize_volume_attachment(
                data_connection, {'path': '/dev/rbd0'}, '/dev/sdb',
                phase='connected'))
        incus_driver._detach_volume = mock.Mock()
        prepare_real = incus_driver.prepare_managed_volume_attach
        ordered = mock.Mock()
        with mock.patch.object(
                incus_driver, 'prepare_managed_volume_attach',
                wraps=prepare_real) as prepare:
            ordered.attach_mock(prepare, 'prepare')
            ordered.attach_mock(container.delete, 'delete')
            incus_driver.post_live_migration(
                ctx, instance, block_device_info, migrate_data=data)

        container.delete.assert_called_once_with(wait=True)
        self.assertEqual(
            ['prepare', 'prepare', 'delete'],
            [call[0] for call in ordered.mock_calls])
        self.assertTrue(prepare.call_args_list[0].kwargs['boot_volume'])
        self.assertFalse(prepare.call_args_list[1].kwargs['boot_volume'])
        incus_driver._detach_volume.assert_called_once_with(
            ctx, data_connection, instance, '/dev/sdb', retain_journal=True)
        mapped_rbd_device.assert_called_once_with(
            data_connection['data'], mapping_cache=None)

    def test_live_source_disconnect_accepts_periodic_convergence(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-source-periodic-convergence', memory_mb=0)
        volume_id = '20000000-0000-0000-0000-000000000002'
        connection_info = {
            'driver_volume_type': 'rbd',
            'serial': volume_id,
            'data': {'volume_id': volume_id},
        }
        intent = {'volume_id': volume_id, 'operation_kind': 'migration'}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.get_managed_volume_attach_intent = mock.Mock(
            return_value=None)
        incus_driver.get_volume_journal_phase = mock.Mock(return_value=None)
        incus_driver._detach_volume = mock.Mock()

        incus_driver._disconnect_live_source_volume(
            ctx, instance, volume_id, connection_info, '/dev/sdb', intent)

        incus_driver._detach_volume.assert_not_called()

    def test_live_source_disconnect_rejects_partial_periodic_evidence(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-source-partial-convergence', memory_mb=0)
        volume_id = '20000000-0000-0000-0000-000000000002'
        connection_info = {
            'driver_volume_type': 'rbd',
            'serial': volume_id,
            'data': {'volume_id': volume_id},
        }
        intent = {'volume_id': volume_id, 'operation_kind': 'migration'}
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        incus_driver.get_managed_volume_attach_intent = mock.Mock(
            return_value=None)
        incus_driver.get_volume_journal_phase = mock.Mock(
            return_value='disconnected')
        incus_driver._detach_volume = mock.Mock()

        self.assertRaises(
            exception.InvalidVolume,
            incus_driver._disconnect_live_source_volume,
            ctx, instance, volume_id, connection_info, '/dev/sdb', intent)

        incus_driver._detach_volume.assert_not_called()

    def test_post_live_migration_rejects_incomplete_volume_before_delete(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test-incomplete-source-volume', memory_mb=0)
        container = mock.Mock(status='Stopped')
        self.client.instances.get.return_value = container
        data, _remote, handover = (
            self._prepare_post_live_migration_protocol(instance))
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)
        self._configure_exact_idmap_release(
            incus_driver, instance, container,
            self.client.profiles.get.return_value,
            outcome='detached', idmap_base=1065536)
        block_device_info = {'block_device_mapping': [{
            'boot_index': None,
            'mount_device': '/dev/sdb',
            'attachment_id':
                '40000000-0000-0000-0000-000000000004',
        }]}

        self.assertRaises(
            exception.InvalidVolume,
            incus_driver.post_live_migration,
            ctx, instance, block_device_info, migrate_data=data)

        container.stop.assert_not_called()
        container.delete.assert_not_called()
        handover.assert_not_called()

    def test_post_live_migration_at_source(self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        network_info = []
        profile = mock.Mock()
        profile.devices = {}
        self.client.profiles.get.return_value = profile

        incus_driver = driver.IncusDriver(None)
        incus_driver._cleanup = mock.Mock()
        incus_driver.init_host(None)

        incus_driver.post_live_migration_at_source(
            ctx, instance, network_info)

        incus_driver._cleanup.assert_called_once_with(
            ctx, instance, network_info, delete_profile=True)

    def test_post_live_migration_at_destination_is_idempotent_after_retire(
            self):
        ctx = context.get_admin_context()
        instance = fake_instance.fake_instance_obj(
            ctx, name='test', memory_mb=0)
        self.client.storage_pools.get.return_value.driver = 'ceph'
        self.client.instances.get.return_value = mock.Mock(
            status='Running',
            config={'user.openstack.uuid': instance.uuid},
            expanded_devices={
                'root': {'type': 'disk', 'path': '/', 'pool': 'ceph-root'},
            })
        self.client.profiles.get.return_value = mock.Mock(config={
            'environment.product_name': 'OpenStack Nova',
            'user.openstack.uuid': instance.uuid,
        })
        self.client.api.instances = mock.MagicMock()
        incus_driver = driver.IncusDriver(None)
        incus_driver.init_host(None)

        incus_driver.post_live_migration_at_destination(
            ctx, instance, [], block_migration=False,
            block_device_info={})

        self.client.api.instances.__getitem__.assert_not_called()
        self._finalize_committed_migration_attempt.assert_not_called()

    def _failed_cleanup_test_driver(self):
        incus_driver = driver.IncusDriver(None)
        incus_driver.client = self.client
        incus_driver.idmap_allocator = None
        return incus_driver

    def _failed_cleanup_test_instance(self):
        return fake_instance.fake_instance_obj(
            context.get_admin_context(), name='instance-cleanup-assessment',
            root_device_name='/dev/vda', memory_mb=512, root_gb=1,
            expected_attrs=['system_metadata'], system_metadata={})

    def _failed_cleanup_absent_incus_resources(self):
        self.client.instances.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        self.client.profiles.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))

    def test_failed_build_cleanup_assessment_retains_unproven_host_vif(self):
        self._failed_cleanup_absent_incus_resources()
        assessment = (
            self._failed_cleanup_test_driver().assess_failed_build_cleanup(
                self._failed_cleanup_test_instance(),
                {'block_device_mapping': []}))

        self.assertEqual(
            driver.FailedBuildCleanupAssessment(
                release_network=False,
                release_cinder=True,
                release_host=False,
                release_placement=False,
                reasons=(
                    'host VIF absence is not proven after failed destroy',)),
            assessment)

    def test_failed_build_cleanup_assessment_retains_stale_instance(self):
        instance = self._failed_cleanup_test_instance()
        self.client.instances.get.return_value = mock.Mock(
            config={'user.openstack.uuid': instance.uuid},
            expanded_config={'user.openstack.uuid': instance.uuid})
        self.client.profiles.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))

        assessment = (
            self._failed_cleanup_test_driver().assess_failed_build_cleanup(
                instance, {'block_device_mapping': []}))

        self.assertFalse(assessment.release_network)
        self.assertTrue(assessment.release_cinder)
        self.assertFalse(assessment.release_host)
        self.assertFalse(assessment.release_placement)
        self.assertIn('Incus instance still exists', assessment.reasons)

    def test_failed_build_cleanup_assessment_api_uncertainty_fails_closed(
            self):
        self.client.instances.get.side_effect = RuntimeError('API unavailable')

        assessment = (
            self._failed_cleanup_test_driver().assess_failed_build_cleanup(
                self._failed_cleanup_test_instance(),
                {'block_device_mapping': []}))

        self.assertEqual((False, False, False, False), (
            assessment.release_network,
            assessment.release_cinder,
            assessment.release_host,
            assessment.release_placement))
        self.assertIn('inventory is uncertain', assessment.reasons[0])

    def test_failed_build_cleanup_assessment_retains_profile_ownership(self):
        instance = self._failed_cleanup_test_instance()
        self.client.instances.get.side_effect = (
            incuscore_exceptions.NotFound(MockResponse(404)))
        self.client.profiles.get.return_value = mock.Mock(
            config={
                'environment.product_name': 'OpenStack Nova',
                'user.openstack.uuid': instance.uuid,
                driver._volume_device_info_key(_TEST_VOLUME_ID): '{}',
            },
            devices={
                'eth0': {'type': 'nic'},
                _TEST_VOLUME_ID: {
                    'type': 'unix-block',
                    'path': '/dev/vdb',
                    'source': '/dev/rbd0',
                },
            })

        assessment = (
            self._failed_cleanup_test_driver().assess_failed_build_cleanup(
                instance, {'block_device_mapping': []}))

        self.assertFalse(assessment.release_network)
        self.assertFalse(assessment.release_cinder)
        self.assertFalse(assessment.release_host)
        self.assertFalse(assessment.release_placement)

    @mock.patch.object(driver, '_volume_journal_records')
    def test_failed_build_cleanup_assessment_retains_volume_journal(
            self, journal_records):
        self._failed_cleanup_absent_incus_resources()
        journal_records.return_value = {
            _TEST_VOLUME_ID: {'phase': 'connected'}}

        assessment = (
            self._failed_cleanup_test_driver().assess_failed_build_cleanup(
                self._failed_cleanup_test_instance(),
                {'block_device_mapping': []}))

        self.assertFalse(assessment.release_network)
        self.assertFalse(assessment.release_cinder)
        self.assertFalse(assessment.release_host)
        self.assertFalse(assessment.release_placement)

    @mock.patch.object(driver, '_rbd_mapping_matches')
    def test_failed_build_cleanup_assessment_checks_bfv_root_mapping(
            self, mapping_matches):
        self._failed_cleanup_absent_incus_resources()
        connection_info = fake_connection_info(
            {'id': 1, 'name': 'volume-00000001'}, '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000001')
        mapping_matches.return_value = (
            connection_info['data']['name'], [{'device': '/dev/rbd0'}])
        block_device_info = {'block_device_mapping': [{
            'boot_index': 0,
            'mount_device': '/dev/vda',
            'connection_info': connection_info,
        }]}

        assessment = (
            self._failed_cleanup_test_driver().assess_failed_build_cleanup(
                self._failed_cleanup_test_instance(), block_device_info))

        self.assertFalse(assessment.release_network)
        self.assertFalse(assessment.release_cinder)
        self.assertFalse(assessment.release_host)
        self.assertFalse(assessment.release_placement)
        self.assertIn('BFV root', ' '.join(assessment.reasons))
        mapping_matches.assert_called_once_with(
            connection_info['data'], mapping_cache=mock.ANY)

    @mock.patch.object(driver, '_rbd_mapping_matches')
    def test_failed_build_cleanup_assessment_mapping_query_fails_closed(
            self, mapping_matches):
        self._failed_cleanup_absent_incus_resources()
        connection_info = fake_connection_info(
            {'id': 2, 'name': 'volume-00000002'}, '10.0.2.15:3260',
            'iqn.2010-10.org.openstack:volume-00000002')
        mapping_matches.side_effect = RuntimeError('rbd query failed')
        block_device_info = {'block_device_mapping': [{
            'boot_index': None,
            'mount_device': '/dev/vdb',
            'connection_info': connection_info,
        }]}

        assessment = (
            self._failed_cleanup_test_driver().assess_failed_build_cleanup(
                self._failed_cleanup_test_instance(), block_device_info))

        self.assertFalse(assessment.release_network)
        self.assertFalse(assessment.release_cinder)
        self.assertFalse(assessment.release_host)
        self.assertFalse(assessment.release_placement)
        self.assertIn('mapping is uncertain', ' '.join(assessment.reasons))


class ManageImageCacheTest(test.NoDBTestCase):
    def setUp(self):
        super().setUp()
        self.flags(
            remove_unused_original_minimum_age_seconds=3600,
            group='image_cache')
        self.driver = driver.IncusDriver(None)
        self.driver.client = mock.Mock()

    _NEXT_FINGERPRINT = 0

    @classmethod
    def _image(cls, aliases, last_used_at,
               uploaded_at='2020-01-01T00:00:00Z'):
        """One entry as the recursive image listing really returns it."""
        cls._NEXT_FINGERPRINT += 1
        return {
            'fingerprint': '%064x' % cls._NEXT_FINGERPRINT,
            'aliases': [{'name': alias} for alias in aliases],
            'last_used_at': last_used_at,
            'uploaded_at': uploaded_at,
        }

    def _listing(self, *images):
        """Serve those entries from the recursive listing endpoint."""
        response = mock.Mock()
        response.json.return_value = {'metadata': list(images)}
        self.driver.client.api.images.get.return_value = response
        deleters = {}
        for image in images:
            deleters[image['fingerprint']] = mock.Mock()
        self.driver.client.images.get.side_effect = (
            lambda fingerprint: deleters[fingerprint])
        return deleters

    def _deleter(self, deleters, image):
        return deleters[image['fingerprint']]

    def test_removes_old_unreferenced_uuid_alias_image(self):
        stale = self._image(
            ['10000000-0000-0000-0000-000000000001'],
            '2020-01-02T00:00:00Z')
        deleters = self._listing(stale)

        self.driver.manage_image_cache(mock.sentinel.ctx, [])

        self._deleter(deleters, stale).delete.assert_called_once_with(
            wait=True)
        # One recursive listing, not a request per image.
        self.driver.client.api.images.get.assert_called_once_with(
            params={'recursion': 1})

    def test_keeps_image_referenced_by_an_instance(self):
        ref = '10000000-0000-0000-0000-000000000001'
        used = self._image([ref], '2020-01-02T00:00:00Z')
        deleters = self._listing(used)
        instance = mock.Mock(image_ref=ref)

        self.driver.manage_image_cache(mock.sentinel.ctx, [instance])

        self._deleter(deleters, used).delete.assert_not_called()

    def test_keeps_operator_published_named_alias_image(self):
        named = self._image(
            ['10000000-0000-0000-0000-000000000001', 'release-2026'],
            '2020-01-02T00:00:00Z')
        deleters = self._listing(named)

        self.driver.manage_image_cache(mock.sentinel.ctx, [])

        self._deleter(deleters, named).delete.assert_not_called()

    def test_keeps_recently_used_image(self):
        fresh = self._image(
            ['10000000-0000-0000-0000-000000000001'],
            timeutils.utcnow(with_timezone=True).isoformat())
        deleters = self._listing(fresh)

        self.driver.manage_image_cache(mock.sentinel.ctx, [])

        self._deleter(deleters, fresh).delete.assert_not_called()

    def test_never_used_image_falls_back_to_upload_age(self):
        # Incus reports never-used as the zero time; the upload timestamp
        # still ages the image out.
        stale = self._image(
            ['10000000-0000-0000-0000-000000000001'],
            '1970-01-01T00:00:00Z')
        deleters = self._listing(stale)

        self.driver.manage_image_cache(mock.sentinel.ctx, [])

        self._deleter(deleters, stale).delete.assert_called_once_with(
            wait=True)

    def test_survives_listing_and_deletion_failures(self):
        self.driver.client.api.images.get.side_effect = RuntimeError('down')
        self.driver.manage_image_cache(mock.sentinel.ctx, [])

        broken = self._image(
            ['10000000-0000-0000-0000-000000000001'],
            '2020-01-02T00:00:00Z')
        second = self._image(
            ['10000000-0000-0000-0000-000000000002'],
            '2020-01-02T00:00:00Z')
        self.driver.client.api.images.get.side_effect = None
        deleters = self._listing(broken, second)
        self._deleter(deleters, broken).delete.side_effect = RuntimeError(
            'busy')

        self.driver.manage_image_cache(mock.sentinel.ctx, [])

        self._deleter(deleters, second).delete.assert_called_once_with(
            wait=True)

    def test_a_long_backlog_is_bounded_and_reported(self):
        """A first pass on an old node must not hold the greenthread.

        Deletions are serial and each waits on the server, so the pass is
        bounded - and what it defers is logged, because a bounded pass
        that looked complete would misreport the store as fully aged.
        """
        backlog = [
            self._image(
                ['10000000-0000-0000-0000-%012d' % index],
                '2020-01-02T00:00:00Z')
            for index in range(driver._IMAGE_CACHE_DELETE_BATCH + 5)]
        deleters = self._listing(*backlog)

        with mock.patch.object(driver.LOG, 'info') as info:
            self.driver.manage_image_cache(mock.sentinel.ctx, [])

        deleted = [
            fingerprint for fingerprint, deleter in deleters.items()
            if deleter.delete.called]
        self.assertEqual(driver._IMAGE_CACHE_DELETE_BATCH, len(deleted))
        self.assertTrue(any(
            'the rest follow on later passes' in call[0][0]
            for call in info.call_args_list))


class TimedPhaseTest(test.NoDBTestCase):
    def test_timed_phase_logs_success(self):
        instance = mock.Mock(uuid='00000000-0000-0000-0000-0000000000aa')
        with self.assertLogs('nova.virt.incus.driver', level='INFO') as logs:
            with driver.IncusDriver._timed_phase(instance, 'spawn', 'x'):
                pass
        line = logs.output[-1]
        self.assertIn('timing operation=spawn phase=x outcome=ok', line)
        self.assertIn('duration_ms=', line)

    def test_timed_phase_logs_failure_and_reraises(self):
        instance = mock.Mock(uuid='00000000-0000-0000-0000-0000000000ab')

        def run():
            with driver.IncusDriver._timed_phase(instance, 'spawn', 'y'):
                raise ValueError('phase failure')

        with self.assertLogs('nova.virt.incus.driver', level='INFO') as logs:
            self.assertRaises(ValueError, run)
        self.assertIn(
            'timing operation=spawn phase=y outcome=error', logs.output[-1])


class SaveProfileMarkerTest(test.NoDBTestCase):
    """The durable-marker save tolerates backup.yaml resync failures only."""

    def test_plain_save_passes_through(self):
        profile = mock.Mock(name='profile')
        driver._save_profile_marker(profile)
        profile.save.assert_called_once_with(wait=True)

    def test_backup_file_resync_failure_is_tolerated(self):
        profile = mock.Mock(name='profile')
        profile.name = 'instance-00000001'
        profile.save.side_effect = incus_api_exception(
            500,
            'The following instances failed to update (profile change '
            'still saved):\n - Project: nova, Instance: instance-00000001: '
            'Failed to write backup file: Failed getting instance pool: '
            'Instance storage pool not found')
        driver._save_profile_marker(profile)
        profile.save.assert_called_once_with(wait=True)

    def test_other_api_errors_still_raise(self):
        profile = mock.Mock(name='profile')
        profile.name = 'instance-00000001'
        profile.save.side_effect = incus_api_exception(
            404, 'Profile not found')
        self.assertRaises(
            incuscore_exceptions.LXDAPIException,
            driver._save_profile_marker, profile)


class UnstartedMigrationReservationTest(test.NoDBTestCase):
    """An unstarted target reservation can only be released as a whole."""

    def setUp(self):
        super().setUp()
        self.conf_patcher = mock.patch.object(driver, 'CONF')
        self.conf = self.conf_patcher.start()
        self.addCleanup(self.conf_patcher.stop)
        self.conf.incus.project = 'nova'
        self.token = '10000000-0000-0000-0000-000000000001'
        self.client = mock.Mock()
        self.client.host_info = {'api_extensions': [
            'migration_attempt_fencing',
            'migration_attempt_reservation_generation',
        ]}
        self.client.api = mock.MagicMock()
        self.collection = self.client.api['migration-attempts']
        self.endpoint = self.collection[self.token]
        self.driver = mock.Mock()
        self.driver.client = self.client
        self.candidate = {
            'token': self.token,
            'name': 'instance-test',
            'idmap_base': 1065536,
            'idmap_size': 65536,
        }

    def _record(self, **overrides):
        record = {
            'token': self.token,
            'project': 'nova',
            'resource_type': 'instance',
            'resource_name': 'instance-test',
            'state': 'active',
            'started': False,
            'finished': False,
            'idmap_base': 1065536,
            'idmap_size': 65536,
            'idmap_active': True,
        }
        record.update(overrides)
        return record

    def _response(self, metadata):
        response = mock.Mock()
        response.json.return_value = {'metadata': metadata}
        return response

    def _list(self, records):
        self.collection.get.return_value = self._response(records)
        listing = (
            driver.IncusDriver.list_unstarted_migration_attempt_reservations)
        return listing(self.driver)

    def test_lists_only_unstarted_active_reservations(self):
        self.assertEqual([self.candidate], self._list([self._record()]))

    def test_ignores_records_that_cannot_wedge_a_range(self):
        for record in (
            self._record(started=True),
            self._record(finished=True, state='aborted'),
            self._record(state='committed', finished=True),
            self._record(idmap_active=False),
            self._record(idmap_base=None, idmap_size=None),
            self._record(project='other'),
            self._record(resource_type='volume'),
            self._record(token='not-a-uuid'),
        ):
            self.assertEqual([], self._list([record]), record)

    def test_listing_needs_the_reservation_generation_extension(self):
        self.client.host_info = {
            'api_extensions': ['migration_attempt_fencing']}

        self.assertEqual([], self._list([self._record()]))
        self.collection.get.assert_not_called()

    def test_release_aborts_and_retires_the_token(self):
        self.endpoint.get.return_value = self._response(self._record())
        self.endpoint.put.return_value = self._response(
            self._record(state='aborted', finished=True))

        driver._release_unstarted_migration_attempt(
            self.client, self.candidate)

        self.endpoint.put.assert_called_once_with(
            params={'project': 'nova'}, json={'state': 'aborted'})
        self.endpoint.delete.assert_called_once_with(
            params={'project': 'nova'})

    def test_release_refuses_a_reservation_that_changed(self):
        self.endpoint.get.return_value = self._response(
            self._record(started=True))

        self.assertRaises(
            exception.MigrationError,
            driver._release_unstarted_migration_attempt,
            self.client, self.candidate)
        self.endpoint.put.assert_not_called()
        self.endpoint.delete.assert_not_called()

    def test_release_refuses_to_retire_an_unfinished_abort(self):
        # A create request that won the race leaves the aborted record
        # unfinished. Retiring it would discard a live target's fence.
        self.endpoint.get.return_value = self._response(self._record())
        self.endpoint.put.return_value = self._response(
            self._record(state='aborted', started=True))

        self.assertRaises(
            exception.MigrationError,
            driver._release_unstarted_migration_attempt,
            self.client, self.candidate)
        self.endpoint.delete.assert_not_called()
