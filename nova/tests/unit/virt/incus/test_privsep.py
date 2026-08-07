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

import os
from unittest import mock

import fixtures as external_fixtures

from nova import test
from nova.tests import fixtures
import nova.virt.incus.privsep


class IncusPrivsepTest(test.NoDBTestCase):

    INSTANCE_UUID = '00000000-0000-0000-0000-000000000001'
    SHARE_UUID = '10000000-0000-0000-0000-000000000001'

    def setUp(self):
        super().setUp()
        self.useFixture(fixtures.PrivsepFixture())
        self.instances_path = self.useFixture(
            external_fixtures.TempDir()).path
        self.flags(instances_path=self.instances_path)
        self.mountpoint = os.path.join(
            self.instances_path, 'incus-shares', self.INSTANCE_UUID,
            self.SHARE_UUID)
        os.makedirs(self.mountpoint)

    @mock.patch.object(nova.virt.incus.privsep.processutils, 'execute')
    def test_validate_gnu_timeout_accepts_coreutils(self, execute):
        execute.return_value = (
            'timeout (GNU coreutils) 9.5\nCopyright...', '')

        nova.virt.incus.privsep.validate_gnu_timeout()

        execute.assert_called_once_with(
            '/usr/bin/timeout', '--version', timeout=5)

    @mock.patch.object(nova.virt.incus.privsep.processutils, 'execute')
    def test_validate_gnu_timeout_rejects_busybox(self, execute):
        execute.return_value = ('BusyBox v1.37.0 multi-call binary.\n', '')

        self.assertRaises(
            RuntimeError, nova.virt.incus.privsep.validate_gnu_timeout)

    @mock.patch.object(nova.virt.incus.privsep.processutils, 'execute')
    def test_mount_uses_bounded_gnu_timeout_without_lazy_or_force(
            self, execute):
        nova.virt.incus.privsep.mount(
            'nfs', 'server:/export', self.mountpoint,
            ['rw', 'nosuid', 'nodev'], 30)

        command = execute.call_args.args
        self.assertEqual((
            '/usr/bin/timeout', '--signal=TERM', '--kill-after=5s', '30s',
            '/bin/mount', '-t', 'nfs', '-o', 'rw,nosuid,nodev', '--',
            'server:/export', self.mountpoint), command)
        self.assertNotIn('-l', command)
        self.assertNotIn('-f', command)
        self.assertEqual(40, execute.call_args.kwargs['timeout'])

    @mock.patch.object(nova.virt.incus.privsep.processutils, 'execute')
    def test_umount_uses_bounded_gnu_timeout_without_lazy_or_force(
            self, execute):
        nova.virt.incus.privsep.umount(
            self.mountpoint, 20)

        command = execute.call_args.args
        self.assertEqual((
            '/usr/bin/timeout', '--signal=TERM', '--kill-after=5s', '20s',
            '/bin/umount', '--', self.mountpoint), command)
        self.assertNotIn('-l', command)
        self.assertNotIn('-f', command)
        self.assertEqual(30, execute.call_args.kwargs['timeout'])

    @mock.patch.object(nova.virt.incus.privsep.processutils, 'execute')
    def test_mount_accepts_current_cephfs_contract(self, execute):
        secret_path = os.path.join(
            os.path.dirname(self.mountpoint), '.ceph-secret-test')
        with open(secret_path, 'w', encoding='utf-8') as secret:
            secret.write('secret')
        os.chmod(secret_path, 0o600)

        nova.virt.incus.privsep.mount(
            'ceph', 'mon1:/project', self.mountpoint,
            ['rw', 'nosuid', 'nodev', 'name=client.manila',
             'secretfile=%s' % secret_path], 30)

        self.assertIn(
            'rw,nosuid,nodev,name=client.manila,secretfile=%s' % secret_path,
            execute.call_args.args)

    @mock.patch.object(nova.virt.incus.privsep.processutils, 'execute')
    def test_mount_rejects_arbitrary_option_argv(self, execute):
        self.assertRaises(
            ValueError, nova.virt.incus.privsep.mount,
            'nfs', 'server:/export', self.mountpoint,
            ['rw', 'nosuid', 'nodev', '--bind'], 30)

        execute.assert_not_called()

    @mock.patch.object(nova.virt.incus.privsep.processutils, 'execute')
    def test_mount_rejects_missing_safety_option(self, execute):
        self.assertRaises(
            ValueError, nova.virt.incus.privsep.mount,
            'nfs', 'server:/export', self.mountpoint,
            ['rw', 'nosuid'], 30)

        execute.assert_not_called()

    @mock.patch.object(nova.virt.incus.privsep.processutils, 'execute')
    def test_ceph_mount_rejects_secret_outside_share_hierarchy(
            self, execute):
        secret_path = os.path.join(self.instances_path, '.ceph-secret-test')
        with open(secret_path, 'w', encoding='utf-8') as secret:
            secret.write('secret')
        os.chmod(secret_path, 0o600)

        self.assertRaises(
            ValueError, nova.virt.incus.privsep.mount,
            'ceph', 'mon1:/project', self.mountpoint,
            ['rw', 'nosuid', 'nodev', 'name=client.manila',
             'secretfile=%s' % secret_path], 30)

        execute.assert_not_called()

    @mock.patch.object(nova.virt.incus.privsep.processutils, 'execute')
    def test_mount_rejects_unsupported_filesystem(self, execute):
        self.assertRaises(
            ValueError, nova.virt.incus.privsep.mount,
            'ext4', '/dev/sda', self.mountpoint,
            ['rw', 'nosuid', 'nodev'], 30)

        execute.assert_not_called()

    @mock.patch.object(nova.virt.incus.privsep.processutils, 'execute')
    def test_mount_rejects_unbounded_timeout(self, execute):
        self.assertRaises(
            ValueError, nova.virt.incus.privsep.mount,
            'nfs', 'server:/export', self.mountpoint,
            ['rw', 'nosuid', 'nodev'], 301)

        execute.assert_not_called()

    @mock.patch.object(nova.virt.incus.privsep.processutils, 'execute')
    def test_umount_rejects_noncanonical_path(self, execute):
        self.assertRaises(
            ValueError, nova.virt.incus.privsep.umount,
            os.path.dirname(self.mountpoint), 30)

        execute.assert_not_called()

    @mock.patch.object(nova.virt.incus.privsep.processutils, 'execute')
    def test_mount_rejects_symlink_escape(self, execute):
        realpath = nova.virt.incus.privsep.os.path.realpath

        def fake_realpath(path):
            if os.path.abspath(path) == os.path.abspath(self.mountpoint):
                return os.path.join(self.instances_path, 'escaped')
            return realpath(path)

        with mock.patch.object(
                nova.virt.incus.privsep.os.path, 'realpath',
                side_effect=fake_realpath):
            self.assertRaises(
                ValueError, nova.virt.incus.privsep.mount,
                'nfs', 'server:/export', self.mountpoint,
                ['rw', 'nosuid', 'nodev'], 30)

        execute.assert_not_called()

    def _config_drive_paths(self):
        drive_dir = os.path.join(self.instances_path, 'instance-00000001')
        os.makedirs(drive_dir, exist_ok=True)
        iso_path = os.path.join(drive_dir, 'disk.config')
        with open(iso_path, 'wb') as handle:
            handle.write(b'')
        mountpoint = os.path.join(drive_dir, 'mnt')
        os.makedirs(mountpoint, exist_ok=True)
        return iso_path, mountpoint

    @mock.patch.object(nova.virt.incus.privsep.processutils, 'execute')
    def test_config_drive_mount_pins_type_and_hardening_options(
            self, execute):
        iso_path, mountpoint = self._config_drive_paths()

        nova.virt.incus.privsep.configdrive_mount_iso(
            iso_path, mountpoint, 1000, 1000, 60)

        command = execute.call_args[0]
        self.assertIn('-t', command)
        self.assertEqual('iso9660', command[command.index('-t') + 1])
        options = command[command.index('-o') + 1]
        for option in ('ro', 'nosuid', 'nodev', 'noexec'):
            self.assertIn(option, options.split(','))

    @mock.patch.object(nova.virt.incus.privsep.processutils, 'execute')
    def test_config_drive_mount_rejects_mountpoint_outside_instances_path(
            self, execute):
        """The mountpoint is as privileged as the source.

        Mounting a self-made image over a system directory hides the real
        one, so a compromised nova user must not be able to name it.
        """
        iso_path, unused_mountpoint = self._config_drive_paths()

        self.assertRaises(
            ValueError, nova.virt.incus.privsep.configdrive_mount_iso,
            iso_path, '/etc', 1000, 1000, 60)

        execute.assert_not_called()

    @mock.patch.object(nova.virt.incus.privsep.processutils, 'execute')
    def test_config_drive_mount_rejects_symlinked_mountpoint(self, execute):
        iso_path, mountpoint = self._config_drive_paths()
        realpath = nova.virt.incus.privsep.os.path.realpath

        def fake_realpath(path):
            if os.path.abspath(path) == os.path.abspath(mountpoint):
                return '/etc'
            return realpath(path)

        with mock.patch.object(
                nova.virt.incus.privsep.os.path, 'realpath',
                side_effect=fake_realpath):
            self.assertRaises(
                ValueError, nova.virt.incus.privsep.configdrive_mount_iso,
                iso_path, mountpoint, 1000, 1000, 60)

        execute.assert_not_called()

    @mock.patch.object(nova.virt.incus.privsep.processutils, 'execute')
    def test_config_drive_umount_rejects_path_outside_instances_path(
            self, execute):
        """Releasing an arbitrary mountpoint is privileged too.

        Unmounting a Manila share would make later writes land on the
        directory underneath it.
        """
        self.assertRaises(
            ValueError, nova.virt.incus.privsep.configdrive_umount,
            self.mountpoint.replace(self.instances_path, '/srv'), 60)

        execute.assert_not_called()

    @mock.patch.object(nova.virt.incus.privsep.processutils, 'execute')
    def test_config_drive_umount_accepts_its_own_mountpoint(self, execute):
        unused_iso_path, mountpoint = self._config_drive_paths()

        nova.virt.incus.privsep.configdrive_umount(mountpoint, 60)

        self.assertIn(os.path.realpath(mountpoint), execute.call_args[0])
