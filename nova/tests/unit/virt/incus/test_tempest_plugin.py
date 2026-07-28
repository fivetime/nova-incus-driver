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

import configparser
from importlib import metadata
import os

from nova import test


class TempestPluginPackagingTest(test.NoDBTestCase):

    def test_entry_point_loads_packaged_plugin(self):
        repo_root = os.path.abspath(os.path.join(
            os.path.dirname(__file__), '..', '..', '..', '..', '..'))
        setup_cfg = configparser.ConfigParser()
        setup_cfg.read(os.path.join(repo_root, 'setup.cfg'))
        configured = setup_cfg.get(
            'entry_points', 'tempest.test_plugins').strip()
        entry_point_name, entry_point_value = (
            value.strip() for value in configured.split('=', 1))
        entry_point = metadata.EntryPoint(
            name=entry_point_name,
            value=entry_point_value,
            group='tempest.test_plugins')

        self.assertEqual('nova-incus-tempest-plugin', entry_point.name)
        self.assertEqual(
            'nova_incus_tempest_plugin.plugin:NovaIncusTempestPlugin',
            entry_point.value)

        plugin = entry_point.load()()
        test_dir, base_path = plugin.load_tests()
        self.assertTrue(os.path.isdir(test_dir))
        self.assertTrue(test_dir.startswith(base_path))
