# Copyright 2026 OpenStack Incus contributors
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import unittest
from unittest import mock

from nova_incus_tempest_plugin.tests.scenario import guest


class GuestPrivilegeCommandTest(unittest.TestCase):

    def test_accepts_sudo(self):
        ssh = mock.Mock()
        ssh.exec_command.return_value = '/usr/bin/sudo\n'

        self.assertEqual('/usr/bin/sudo', guest.privilege_command(ssh))

    def test_accepts_doas(self):
        ssh = mock.Mock()
        ssh.exec_command.return_value = '/usr/bin/doas\n'

        self.assertEqual('/usr/bin/doas', guest.privilege_command(ssh))

    def test_rejects_missing_tool(self):
        ssh = mock.Mock()
        ssh.exec_command.return_value = ''

        self.assertRaises(RuntimeError, guest.privilege_command, ssh)
