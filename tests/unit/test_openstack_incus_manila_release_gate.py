# Copyright 2026 OpenStack Incus Authors
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

import pathlib
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNTIME = (
    REPO_ROOT / 'tools' / 'openstack-incus-nova-runtime-preflight.sh')
FLEET = REPO_ROOT / 'tools' / 'openstack-incus-fleet-preflight.sh'
RELEASE = REPO_ROOT / 'tools' / 'openstack-incus-release-gate.sh'
SNAPSHOT = (
    REPO_ROOT / 'tools' / 'openstack-incus-manila-snapshot-e2e.sh')
ARCHITECTURE = REPO_ROOT / 'doc' / 'source' / 'architecture.rst'
READINESS = REPO_ROOT / 'doc' / 'source' / 'production_readiness.rst'
TEST_STATUS = REPO_ROOT / 'TEST_STATUS.md'


class ManilaReleaseGateContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.runtime = RUNTIME.read_text(encoding='utf-8')
        cls.fleet = FLEET.read_text(encoding='utf-8')
        cls.release = RELEASE.read_text(encoding='utf-8')
        cls.snapshot = SNAPSHOT.read_text(encoding='utf-8')
        cls.architecture = ARCHITECTURE.read_text(encoding='utf-8')
        cls.readiness = READINESS.read_text(encoding='utf-8')
        cls.test_status = TEST_STATUS.read_text(encoding='utf-8')

    def test_runtime_gate_enters_every_matching_process_namespace(self):
        self.assertIn('for proc in /proc/[0-9]*', self.runtime)
        self.assertIn('for pid in "${runtime_pids[@]}"', self.runtime)
        self.assertIn('nsenter --target "$pid" --mount', self.runtime)
        self.assertIn('no running Nova $ROLE process matched', self.runtime)
        self.assertIn('nova-api-metadata', self.runtime)
        self.assertNotIn('test -f patches/', self.runtime)

    def test_runtime_gate_checks_all_nova_core_hooks(self):
        for hook in (
                '_pre_deny_share',
                '_prepare_live_migration_check_data',
                '_complete_live_migration_rollback'):
            self.assertIn('"{}"'.format(hook), self.runtime)
        self.assertIn('nova_manager.ComputeManager', self.runtime)
        self.assertIn(
            'incus_manager.IncusComputeManager.__dict__.get(hook)',
            self.runtime)

    def test_runtime_gate_checks_api_and_compute_entry_contracts(self):
        self.assertIn('_require_share_migration_capability', self.runtime)
        self.assertIn('compute_api.API.resize', self.runtime)
        self.assertIn('compute_api.API.live_migrate', self.runtime)
        self.assertIn('nova-incus-compute', self.runtime)
        self.assertIn('INCUS_COMPUTE_MANAGER', self.runtime)

    def test_runtime_and_placement_traits_are_both_required(self):
        for trait in (
                'CUSTOM_INCUS_MANILA_SHARE',
                'CUSTOM_INCUS_MANILA_COLD_MIGRATION',
                'CUSTOM_INCUS_MANILA_LIVE_MIGRATION'):
            self.assertIn(trait, self.runtime)
            self.assertIn(trait, self.fleet)
        self.assertIn('bash -s -- api', self.fleet)
        self.assertIn('bash -s -- compute', self.fleet)
        self.assertIn('resource provider trait list', self.fleet)

    def test_api_runtime_mappings_are_conditional_on_manila_gate(self):
        self.assertIn(
            'RUN_MIGRATION_MANILA=${RUN_MIGRATION_MANILA:-false}',
            self.release)
        self.assertIn(
            'RUN_MIGRATION_MANILA requires RUN_MIGRATION_MATRIX=true',
            self.release)
        self.assertIn(
            'NOVA_API_NODES:?Enumerate every nova-api host', self.release)
        self.assertIn(
            'REQUIRE_MANILA_MIGRATION_RUNTIME="$RUN_MIGRATION_MANILA"',
            self.release)
        self.assertIn(
            '[[ "$REQUIRE_MANILA_MIGRATION_RUNTIME" == true', self.fleet)

    def test_release_gate_requires_real_nfs_and_cephfs_shares(self):
        for name in (
                'MIGRATION_MANILA_NFS_SHARE',
                'MIGRATION_MANILA_CEPHFS_SHARE',
                'MIGRATION_MANILA_NFS_SHARE_TYPE',
                'MIGRATION_MANILA_CEPHFS_SHARE_TYPE'):
            self.assertIn(name, self.release)
        self.assertIn('share show "$share_ref" -f json', self.release)
        self.assertIn('protocol != expected_protocol', self.release)
        self.assertIn('must be available', self.release)
        self.assertIn('does not match snapshot type', self.release)

    def test_release_gate_proves_snapshot_support_on_both_backends(self):
        self.assertIn('"snapshot_support"', self.release)
        self.assertIn('"create_share_from_snapshot_support"', self.release)
        self.assertIn('SHARE_PROTOCOL=NFS', self.release)
        self.assertIn('SHARE_PROTOCOL=CEPHFS', self.release)
        self.assertEqual(
            2,
            self.release.count(
                '"$SCRIPT_DIR/openstack-incus-manila-snapshot-e2e.sh"'))

    def test_three_share_matrix_always_includes_both_protocols(self):
        self.assertIn(
            'migration_three_shares="$MIGRATION_MANILA_NFS_SHARE "',
            self.release)
        self.assertIn(
            'migration_three_shares+="$MIGRATION_MANILA_CEPHFS_SHARE "',
            self.release)
        self.assertIn(
            'MIGRATION_MANILA_SHARES needs a third independent share',
            self.release)

    def test_snapshot_e2e_uses_pinned_ssh_host_identity(self):
        self.assertIn('StrictHostKeyChecking=yes', self.snapshot)
        self.assertIn('UserKnownHostsFile=$SSH_KNOWN_HOSTS_FILE',
                      self.snapshot)
        self.assertNotIn('StrictHostKeyChecking=no', self.snapshot)
        self.assertIn('.lower().replace(" ", "_")', self.snapshot)

    def test_docs_do_not_claim_file_presence_is_runtime_evidence(self):
        self.assertIn('every running API and\ncompute process',
                      self.architecture)
        self.assertIn('Comparing a\nsource-tree hash', self.architecture)
        self.assertIn('real NFS share and a different real CephFS share',
                      self.readiness)
        self.assertIn('These checks have not yet run', self.test_status)


if __name__ == '__main__':
    unittest.main()
