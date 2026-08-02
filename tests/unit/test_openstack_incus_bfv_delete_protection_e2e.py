# Copyright 2026 OpenStack Incus Authors
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

import pathlib
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = (REPO_ROOT / 'tools' /
          'openstack-incus-bfv-delete-protection-e2e.sh')


class BFVDeleteProtectionE2EContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.script = SCRIPT.read_text(encoding='utf-8')

    def test_defaults_to_non_destructive(self):
        self.assertIn('RUN_DESTRUCTIVE=${RUN_DESTRUCTIVE:-false}', self.script)
        gate = self.script.index('if [[ "$RUN_DESTRUCTIVE" != true ]]')
        first_create = min(
            self.script.index('openstack volume create'),
            self.script.index('openstack --os-compute-api-version'))
        self.assertLess(gate, first_create)

    def test_uses_cinder_root_then_nova_bfv(self):
        self.assertIn('openstack volume create --image "$IMAGE"', self.script)
        self.assertIn('--volume "$volume_id"', self.script)
        self.assertIn(
            'openstack server delete --wait "$server_id"', self.script)
        self.assertIn('wait_value available volume_status', self.script)

    def test_proves_release_without_removing_the_external_root(self):
        self.assertIn('cinder_attachment_count', self.script)
        self.assertIn('volume_attachment_count', self.script)
        self.assertIn('watcher_count', self.script)
        self.assertIn('host_rbd_mapping_count', self.script)
        self.assertIn('instance_or_profile_exists', self.script)
        self.assertIn('Nova/Incus deletion removed the Cinder-owned BFV root',
                      self.script)
        self.assertIn('rbd_image_exists ||', self.script)

    def test_deletes_the_rbd_only_after_explicit_cinder_delete(self):
        main_flow = self.script.index(
            'volume_id=$(openstack volume create')
        delete_server = self.script.index(
            'openstack server delete --wait "$server_id"', main_flow)
        preserve = self.script.index(
            'Nova/Incus deletion removed the Cinder', delete_server)
        assert_runtime = self.script.index(
            'assert_runtime_released', preserve)
        delete_volume = self.script.index(
            'openstack volume delete "$volume_id"', assert_runtime)
        self.assertLess(delete_server, preserve)
        self.assertLess(preserve, assert_runtime)
        self.assertLess(assert_runtime, delete_volume)
        self.assertIn('Cinder deletion did not remove BFV root RBD image',
                      self.script)

    def test_requires_all_compute_hosts_for_runtime_audit(self):
        self.assertIn('COMPUTE_HOSTS=${COMPUTE_HOSTS:?', self.script)
        self.assertIn('COMPUTE_SSH=${COMPUTE_SSH:?', self.script)
        self.assertIn('for index in "${!compute_hosts[@]}"', self.script)
        self.assertIn('StrictHostKeyChecking=yes', self.script)


if __name__ == '__main__':
    unittest.main()
