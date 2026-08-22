# Copyright 2016 Canonical Ltd
# Copyright 2026 OpenStack Incus contributors
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

from oslo_log import log as logging
import testtools

from tempest.common import utils
from tempest.common import waiters
from tempest import config
from tempest import exceptions
from tempest.lib.common.utils import test_utils
from tempest.lib import decorators
from tempest.scenario import manager

from nova_incus_tempest_plugin.tests.scenario import guest

CONF = config.CONF
LOG = logging.getLogger(__name__)


class IncusVolumeScenario(manager.ScenarioTest):
    """Validate Cinder data volumes through the public OpenStack APIs."""

    credentials = ['primary', 'admin']
    volume_min_microversion = '3.42'

    def setUp(self):
        super().setUp()
        self.image_ref = CONF.compute.image_ref
        self.flavor_ref = CONF.compute.flavor_ref
        self.ssh_user = CONF.validation.image_ssh_user

    @classmethod
    def setup_clients(cls):
        super().setup_clients()
        cls.admin_servers_client = cls.os_admin.servers_client

    def _wait_for_volume_available_on_the_system(self, ssh):
        device = '/dev/%s' % CONF.compute.volume_device_name

        def _device_exists():
            return ssh.exec_command(
                'test -b %s && echo present || true' % device).strip() == (
                    'present')

        if not test_utils.call_until_true(
                _device_exists, CONF.compute.build_timeout,
                CONF.compute.build_interval):
            raise exceptions.TimeoutException(
                'Timed out waiting for %s in the system container' % device)
        return device

    def _server_host(self, server_id):
        server = self.admin_servers_client.show_server(server_id)['server']
        return server['OS-EXT-SRV-ATTR:host']

    def _mount_fuse_ext4(self, ssh, device, make_filesystem=False):
        privilege = guest.privilege_command(ssh)
        if make_filesystem:
            ssh.exec_command('%s mke2fs -F -t ext4 %s' %
                             (privilege, device))
        ssh.exec_command('%s mkdir -p /mnt/cinder-tempest' % privilege)
        ssh.exec_command(
            '%s fuse2fs -o allow_other %s /mnt/cinder-tempest' %
            (privilege, device))

    def _unmount_fuse_ext4(self, ssh):
        privilege = guest.privilege_command(ssh)
        ssh.exec_command('%s fusermount3 -u /mnt/cinder-tempest' % privilege)

    @decorators.idempotent_id('44356d4b-3a74-44e0-9719-9e36c3acff50')
    @decorators.attr(type='smoke')
    @utils.services('compute', 'network', 'volume')
    @testtools.skipUnless(
        CONF.validation.run_validation,
        'Guest data verification requires Tempest SSH validation.')
    def test_volume_attach(self):
        """Attach, use and detach an ext4 volume without a kernel mount."""
        keypair = self.create_keypair()
        security_group = self.create_security_group()
        server = self.create_server(
            image_id=self.image_ref,
            flavor=self.flavor_ref,
            key_name=keypair['name'],
            security_groups=[{'name': security_group['name']}],
            config_drive=True,
            wait_until='ACTIVE')
        volume = self.create_volume(
            volume_type=CONF.volume.volume_type or None)
        ip_address = self.create_floating_ip(
            server)['floating_ip_address']
        ssh = self.get_remote_client(
            ip_address=ip_address, username=self.ssh_user,
            private_key=keypair['private_key'])

        self.nova_volume_attach(server, volume)
        self.addCleanup(
            test_utils.call_and_ignore_notfound_exc,
            self.nova_volume_detach, server, volume)
        device = self._wait_for_volume_available_on_the_system(ssh)
        self._mount_fuse_ext4(ssh, device, make_filesystem=True)
        privilege = guest.privilege_command(ssh)
        ssh.exec_command(
            'echo tempest-volume | %s tee '
            '/mnt/cinder-tempest/marker >/dev/null && sync' % privilege)
        marker = ssh.exec_command(
            'cat /mnt/cinder-tempest/marker').strip()
        self._unmount_fuse_ext4(ssh)
        self.nova_volume_detach(server, volume)
        self.assertEqual('tempest-volume', marker)

    @decorators.idempotent_id('dbcc8145-d69a-44d9-86a0-b29bbf4c19d4')
    @decorators.attr(type=['multinode', 'slow'])
    @utils.services('compute', 'network', 'volume')
    @testtools.skipUnless(
        CONF.validation.run_validation,
        'Guest data verification requires Tempest SSH validation.')
    @testtools.skipUnless(CONF.compute_feature_enabled.cold_migration,
                          'Cold migration not available.')
    def test_volume_extend_and_cold_migrate(self):
        """Preserve an attached Cinder volume across cold migration."""
        if CONF.compute.min_compute_nodes < 2:
            raise self.skipException('At least two compute nodes are required')

        keypair = self.create_keypair()
        security_group = self.create_security_group()
        server = self.create_server(
            image_id=self.image_ref,
            flavor=self.flavor_ref,
            key_name=keypair['name'],
            security_groups=[{'name': security_group['name']}],
            config_drive=True,
            wait_until='ACTIVE')
        volume = self.create_volume(
            size=1, volume_type=CONF.volume.volume_type or None)
        ip_address = self.create_floating_ip(
            server)['floating_ip_address']
        ssh = self.get_remote_client(
            ip_address=ip_address, username=self.ssh_user,
            private_key=keypair['private_key'])
        source_host = self._server_host(server['id'])

        self.nova_volume_attach(server, volume)
        self.addCleanup(
            test_utils.call_and_ignore_notfound_exc,
            self.nova_volume_detach, server, volume)
        device = self._wait_for_volume_available_on_the_system(ssh)
        self._mount_fuse_ext4(ssh, device, make_filesystem=True)
        privilege = guest.privilege_command(ssh)
        ssh.exec_command(
            'echo tempest-migration | %s tee '
            '/mnt/cinder-tempest/marker >/dev/null && sync' % privilege)

        self.volumes_client.extend_volume(volume['id'], new_size=2)
        waiters.wait_for_volume_resource_status(
            self.volumes_client, volume['id'], 'in-use')
        size_bytes = int(ssh.exec_command(
            '%s blockdev --getsize64 %s' %
            (privilege, device)).strip())
        self.assertGreaterEqual(size_bytes, 2 * 1024 ** 3)
        self._unmount_fuse_ext4(ssh)

        self.admin_servers_client.migrate_server(server['id'])
        waiters.wait_for_server_status(
            self.servers_client, server['id'], 'VERIFY_RESIZE')
        self.servers_client.confirm_resize_server(server['id'])
        waiters.wait_for_server_status(
            self.servers_client, server['id'], 'ACTIVE')
        destination_host = self._server_host(server['id'])
        self.assertNotEqual(source_host, destination_host)

        ssh = self.get_remote_client(
            ip_address=ip_address, username=self.ssh_user,
            private_key=keypair['private_key'])
        device = self._wait_for_volume_available_on_the_system(ssh)
        self._mount_fuse_ext4(ssh, device)
        marker = ssh.exec_command(
            'cat /mnt/cinder-tempest/marker').strip()
        self._unmount_fuse_ext4(ssh)
        self.assertEqual('tempest-migration', marker)
        self.nova_volume_detach(server, volume)
