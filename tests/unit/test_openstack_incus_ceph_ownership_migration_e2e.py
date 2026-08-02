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
SHARED_E2E = (
    REPO_ROOT / 'tools' / 'openstack-incus-live-migration-e2e.sh')
MATRIX = (
    REPO_ROOT / 'tools' /
    'openstack-incus-ceph-ownership-migration-matrix.sh')
BFV_DELETE = (
    REPO_ROOT / 'tools' /
    'openstack-incus-bfv-delete-protection-e2e.sh')
ABA_DELETE = (
    REPO_ROOT / 'tools' /
    'openstack-incus-ceph-exact-delete-aba-e2e.sh')
START_FENCE_SKIP = (
    REPO_ROOT / 'tools' /
    'openstack-incus-storage-start-fence-skip.sh')
RELEASE_GATE = REPO_ROOT / 'tools' / 'openstack-incus-release-gate.sh'


class CephOwnershipMigrationE2EContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.e2e = SHARED_E2E.read_text(encoding='utf-8')
        cls.matrix = MATRIX.read_text(encoding='utf-8')
        cls.bfv_delete = BFV_DELETE.read_text(encoding='utf-8')
        cls.aba_delete = ABA_DELETE.read_text(encoding='utf-8')
        cls.start_fence_skip = START_FENCE_SKIP.read_text(encoding='utf-8')
        cls.release_gate = RELEASE_GATE.read_text(encoding='utf-8')

    def test_matrix_defaults_to_non_destructive(self):
        self.assertIn(
            'RUN_DESTRUCTIVE=${RUN_DESTRUCTIVE:-false}', self.matrix)
        gate = self.matrix.index('if [[ "$RUN_DESTRUCTIVE" != true ]]')
        first_e2e = self.matrix.index('bash "$E2E"')
        self.assertLess(gate, first_e2e)
        self.assertIn(
            'RUN_UUID must be a canonical lowercase UUIDv4', self.matrix)
        self.assertIn('SERVER="incus-ceph-cold-$RUN_UUID"', self.matrix)
        self.assertIn('SERVER="incus-ceph-live-$RUN_UUID"', self.matrix)

    def test_matrix_covers_cold_revert_and_second_migration(self):
        self.assertIn('MIGRATION_MODE=cold', self.matrix)
        self.assertIn(
            'MIGRATION_ACTIONS=confirm,revert,confirm', self.matrix)
        self.assertIn(
            '$NODE02_HOST=$NODE02_SSH,$SOURCE_HOST=$SOURCE_SSH,'
            '$NODE03_HOST=$NODE03_SSH', self.matrix)

    def test_matrix_covers_live_rollback_and_second_migration(self):
        self.assertIn('MIGRATION_MODE=live', self.matrix)
        self.assertIn('INJECT_RESTORE_FAILURE=1', self.matrix)
        self.assertIn(
            '$NODE02_HOST=$NODE02_SSH,$NODE03_HOST=$NODE03_SSH',
            self.matrix)

    def test_ordinary_ceph_uses_immutable_pool_and_image_identity(self):
        for token in (
                'managed_root_pool_identity()',
                'managed_root_image_id()',
                'managed_root_exact_mapping_count()',
                'managed_root_exact_watcher_count()',
                '/sys/bus/rbd/devices',
                'rbd_header.$managed_root_rbd_id',
                '"$active_count" == 1',
                '"$inactive_count" == 0',
                'watcher_count <= 1'):
            self.assertIn(token, self.e2e)
        self.assertIn(
            'current_id" == "$managed_root_rbd_id', self.e2e)
        self.assertIn(
            'current_pool_id" == "$managed_root_pool_id', self.e2e)

    def test_every_migration_outcome_rechecks_exact_owner(self):
        self.assertGreaterEqual(
            self.e2e.count('assert_managed_root_owner'), 6)
        self.assertIn(
            'assert_managed_root_owner "$current_ssh" "$target_ssh"',
            self.e2e)
        self.assertIn(
            'assert_managed_root_owner "$target_ssh" "$current_ssh"',
            self.e2e)

    def test_ordinary_ceph_final_delete_checks_exact_header(self):
        server_delete = self.e2e.rindex(
            'openstack server delete --wait "$server_id"')
        exact_absence = self.e2e.rindex(
            'managed_root_exact_object_exists "$SOURCE_SSH"')
        self.assertLess(server_delete, exact_absence)
        self.assertIn(
            'exact managed Ceph root RBD must be absent after server delete',
            self.e2e)

    def test_failed_cleanup_requires_exact_nova_uuid(self):
        self.assertIn(
            'observed_uuid" == "$server_id', self.e2e)
        self.assertIn(
            'profile_uuid" == "$server_id', self.e2e)
        self.assertIn(
            'Refusing cleanup of $host/$instance_name', self.e2e)

    def test_bfv_root_survives_nova_and_only_cinder_deletes_it(self):
        for token in (
                'original_rbd_image_id=$(rbd_image_id)',
                'original_rbd_pool_id=$(rbd_pool_id)',
                'Nova/Incus deletion removed the Cinder-owned BFV root',
                'Nova/Incus deletion replaced the Cinder-owned BFV RBD',
                'exact_rbd_object_exists'):
            self.assertIn(token, self.bfv_delete)
        nova_preserve = self.bfv_delete.index(
            'Nova/Incus deletion removed the Cinder-owned BFV root')
        cinder_delete = self.bfv_delete.rindex(
            'openstack volume delete "$volume_id"')
        exact_absence = self.bfv_delete.rindex('exact_rbd_object_exists')
        self.assertLess(nova_preserve, cinder_delete)
        self.assertLess(cinder_delete, exact_absence)

    def test_new_matrix_is_not_silently_promoted_to_release_gate(self):
        self.assertNotIn(
            'openstack-incus-ceph-ownership-migration-matrix.sh',
            self.release_gate)
        self.assertNotIn(
            'openstack-incus-ceph-exact-delete-aba-e2e.sh',
            self.release_gate)

    def test_aba_probe_is_destructive_opt_in_and_uuid_scoped(self):
        self.assertIn(
            'RUN_DESTRUCTIVE=${RUN_DESTRUCTIVE:-false}', self.aba_delete)
        gate = self.aba_delete.index('if [[ "$RUN_DESTRUCTIVE" != true ]]')
        create = self.aba_delete.index('server_id=$(openstack')
        self.assertLess(gate, create)
        self.assertIn('RUN_UUID must be a canonical UUIDv4', self.aba_delete)
        self.assertIn(
            'clone_name="incus_identity_test_clone_${RUN_UUID//-/}"',
            self.aba_delete)
        self.assertIn(
            'EXPECTED_ROOT_POOL=${EXPECTED_ROOT_POOL:?', self.aba_delete)

    def test_aba_probe_preflights_local_runtime_and_ceph_dependencies(self):
        for token in (
                'jq sha256sum base64 flock awk python3',
                'command -v podman',
                'incus python3 ceph rbd rados dd base64',
                'could not query the configured Ceph pool',
                'ceph_rbd ls --format json',
                'ceph_rados ls'):
            self.assertIn(token, self.aba_delete)
        preflight = self.aba_delete.index(
            'could not query the configured Ceph pool')
        create = self.aba_delete.index('server_id=$(openstack')
        self.assertLess(preflight, create)

    def test_aba_probe_binds_a_to_canonical_pool_and_image_identity(self):
        for token in (
                'storage_identity=$(jq -er .storage_identity',
                '(.pool_id | type) == "number"',
                '(.id | type) == "string"',
                '(.block_name_prefix | type) == "string"',
                '"rbd_data.$a_image_id"',
                'actual_identity=$(rbd_identity "$rbd_name")'):
            self.assertIn(token, self.aba_delete)
        self.assertIn(
            '"$storage_volume" == "${INCUS_PROJECT}_${instance_name}"',
            self.aba_delete)
        self.assertIn(
            '"$actual_identity" == "$identity_document"',
            self.aba_delete)

    def test_aba_probe_creates_a_real_pending_dependent_clone(self):
        failure = self.aba_delete.index(
            'failed_operation=$(delete_instance_with_receipt failure)')
        pending = self.aba_delete.index('pending_output=$(incus_query')
        replacement = self.aba_delete.index(
            'ceph_rbd create --size "$b_size_mib" "$rbd_name"')
        retry = self.aba_delete.index(
            'delete_instance_with_receipt success >/dev/null')
        self.assertLess(failure, pending)
        self.assertLess(pending, replacement)
        self.assertLess(replacement, retry)
        self.assertIn('dependent clones', self.aba_delete)
        self.assertIn('not complete', self.aba_delete)

    def test_aba_probe_uses_the_reserved_identity_tombstone(self):
        self.assertIn(
            'tombstone_name="incus_identity_release_$tombstone_digest"',
            self.aba_delete)
        self.assertIn(
            'printf \'%s\' "$storage_identity" | sha256sum',
            self.aba_delete)
        self.assertIn(
            '"$tombstone_identity" == "$identity_document"',
            self.aba_delete)

    def test_aba_probe_preserves_b_identity_and_content(self):
        self.assertGreaterEqual(
            self.aba_delete.count(
                '[[ "$(rbd_identity "$rbd_name")" == "$b_identity" ]]'),
            2)
        self.assertGreaterEqual(
            self.aba_delete.count(
                '[[ "$(read_b_marker "${#b_marker}")" == '
                '"$b_marker_b64" ]]'),
            3)
        self.assertIn('conv=notrunc,fsync', self.aba_delete)
        self.assertIn(
            'marker failed its immediate fsync/readback', self.aba_delete)

    def test_aba_probe_proves_a_absent_before_receipt_ack(self):
        mapping = self.aba_delete.index(
            'exact_mapping_count "$a_pool_id" "$a_image_id"')
        header = self.aba_delete.index(
            'assert_exact_header_absent "$a_image_id"')
        receipt = self.aba_delete.index(
            'receipt_document=$(incus_query "$receipt_query")')
        self.assertLess(mapping, receipt)
        self.assertLess(header, receipt)
        self.assertIn(
            'objects=$(ceph_rados ls)', self.aba_delete)

    def test_aba_probe_requires_complete_then_idempotently_acked_receipt(self):
        self.assertIn(
            '"$(jq -r .state <<<"$receipt_document")" == complete',
            self.aba_delete)
        self.assertIn(
            'openstack server delete --wait "$server_id"', self.aba_delete)
        self.assertIn(
            'incus_query -X DELETE "$ack_url"', self.aba_delete)
        self.assertGreaterEqual(
            self.aba_delete.count('incus_query -X DELETE "$ack_url"'), 2)
        self.assertIn(
            'replayed exact-delete receipt is not complete', self.aba_delete)
        self.assertIn(
            'ACKed receipt remains visible through GET', self.aba_delete)

    def test_aba_probe_safe_leaks_after_fault_mutation(self):
        self.assertIn(
            'if [[ "$test_complete" != true && '
            '"$mutation_started" == true ]]', self.aba_delete)
        self.assertIn(
            'SAFE-LEAK: preserving UUID-scoped ABA evidence',
            self.aba_delete)

    def test_aba_probe_absence_checks_have_successful_inventory_queries(self):
        self.assertIn(
            'could not list Incus instances before proving record absence',
            self.aba_delete)
        self.assertIn(
            'could not list Nova servers before proving server absence',
            self.aba_delete)
        self.assertIn(
            'could not list RBD images while checking', self.aba_delete)
        self.assertIn(
            'could not list Ceph objects before proving header absence',
            self.aba_delete)

    def test_aba_probe_cleans_b_by_recorded_exact_id(self):
        self.assertIn('remove_b_exact()', self.aba_delete)
        self.assertIn(
            'refusing to clean replacement name with a changed identity',
            self.aba_delete)
        self.assertIn('ceph_rbd trash mv "$rbd_name"', self.aba_delete)
        self.assertIn('ceph_rbd trash rm "$b_image_id"', self.aba_delete)

    def test_unsafe_start_fence_cases_are_explicit_skips(self):
        for state in (
                'active', 'aborted', 'pending-release', 'uncommitted',
                'possible'):
            self.assertIn('%s)' % state, self.start_fence_skip)
        self.assertIn('exit 77', self.start_fence_skip)
        self.assertIn('SKIP: FENCE_STATE=', self.start_fence_skip)
        self.assertNotIn('PASS', self.start_fence_skip)

    def test_start_fence_skip_names_why_public_injection_is_unsafe(self):
        for explanation in (
                'rejects registration over an existing instance',
                'terminal and cannot be rebound',
                'no public fault hook',
                'no supported API',
                'directly corrupting the shared registry'):
            self.assertIn(explanation, self.start_fence_skip)


if __name__ == '__main__':
    unittest.main()
