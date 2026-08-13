# Copyright 2026
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

import os
import pathlib
import shutil
import subprocess
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATION_E2E = (
    REPO_ROOT / 'tools' / 'openstack-incus-live-migration-e2e.sh')
COLD_MATRIX = (
    REPO_ROOT /
    'tools' /
    'openstack-incus-cold-migration-cardinality-matrix.sh')
RELEASE_GATE = (
    REPO_ROOT / 'tools' / 'openstack-incus-release-gate.sh')


class ColdMigrationE2EContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.e2e = MIGRATION_E2E.read_text(encoding='utf-8')
        cls.matrix = COLD_MATRIX.read_text(encoding='utf-8')
        cls.release_gate = RELEASE_GATE.read_text(encoding='utf-8')

    def test_shared_executor_has_fail_closed_mode_and_action_contract(self):
        self.assertIn(
            'MIGRATION_MODE=${MIGRATION_MODE:-live}', self.e2e)
        self.assertIn(
            'MIGRATION_ACTIONS=${MIGRATION_ACTIONS:-}', self.e2e)
        self.assertIn(
            'MIGRATION_MODE must be live or cold', self.e2e)
        self.assertIn(
            'MIGRATION_ACTIONS must match MIGRATION_TARGETS count',
            self.e2e)
        self.assertIn(
            'Unsupported cold-migration action:', self.e2e)
        self.assertIn(
            'INJECT_RESTORE_FAILURE is supported only for live migration',
            self.e2e)

    def test_cold_hop_reaches_verify_resize_before_marker_and_action(self):
        migrate = self.e2e.index('migrate_and_verify()')
        verify_resize = self.e2e.index(
            'wait_status VERIFY_RESIZE', migrate)
        marker = self.e2e.index(
            'verify_guest_persistent_state "$target_ssh"', verify_resize)
        action = self.e2e.index('case "$action" in', marker)
        confirm = self.e2e.index(
            'openstack server resize confirm "$server_id"', action)
        revert = self.e2e.index(
            'openstack server resize revert "$server_id"', action)
        self.assertLess(verify_resize, marker)
        self.assertLess(marker, action)
        self.assertLess(action, confirm)
        self.assertLess(action, revert)

    def test_all_persistent_resource_markers_are_verified(self):
        self.assertIn('/root/incus-migration-e2e-marker', self.e2e)
        self.assertIn(
            'dd if="$data_device" bs=1 count="${#volume_marker}"',
            self.e2e)
        self.assertIn(
            'incus_exec_read "$host" "$instance_name"',
            self.e2e)
        self.assertIn(
            'cat "$manila_marker_path"',
            self.e2e)
        self.assertIn(
            'verify_guest_persistent_state "$target_ssh"', self.e2e)
        self.assertIn(
            'verify_guest_persistent_state "$current_ssh"', self.e2e)
        self.assertIn('verify_openstack_volume_attachments', self.e2e)
        self.assertIn('volume set differs:', self.e2e)
        self.assertIn('volume {} attachment cardinality is {}', self.e2e)
        self.assertIn('verify_share_api_active', self.e2e)
        self.assertIn('share mapping cardinality differs:', self.e2e)

    def test_cold_pid_is_not_used_as_a_continuity_assertion(self):
        live_check = 'if [[ "$MIGRATION_MODE" == live ]]; then'
        pid_check = 'if [[ "$dest_pid" != "$source_pid" ]]; then'
        self.assertIn(live_check, self.e2e)
        pid_offset = self.e2e.index(pid_check)
        live_offset = self.e2e.rfind(live_check, 0, pid_offset)
        cold_offset = self.e2e.index('else', live_offset)
        self.assertLess(live_offset, pid_offset)
        self.assertLess(pid_offset, cold_offset)
        self.assertIn(
            '((dest_counter >= current_counter))',
            self.e2e[cold_offset:])

    def test_confirm_and_revert_prove_authoritative_cleanup(self):
        for token in (
                'wait_incus_absent "$current_ssh"',
                'wait_incus_absent "$target_ssh"',
                'assert_inactive_storage_absent '
                '"$target_ssh" "$current_ssh"',
                'assert_inactive_storage_absent '
                '"$current_ssh" "$target_ssh"',
                'verify_network_owner '
                '"$target_host" "$target_ssh" "$current_ssh"',
                'verify_network_owner '
                '"$current_host" "$current_ssh" "$target_ssh"'):
            self.assertIn(token, self.e2e)
        self.assertIn(
            'wait_profile_absent "$host"', self.e2e)

    def test_wrapper_covers_exact_two_by_three_by_three_matrix(self):
        self.assertIn(
            'CARDINALITY_COUNTS=${CARDINALITY_COUNTS:-0 1 3}',
            self.matrix)
        self.assertIn('for root_model in local bfv; do', self.matrix)
        self.assertIn(
            'for data_count in "${cardinalities[@]}"; do', self.matrix)
        self.assertIn(
            'for share_count in "${cardinalities[@]}"; do', self.matrix)
        self.assertIn(
            'Full cold matrix requires exactly '
            'CARDINALITY_COUNTS=\'0 1 3\'',
            self.matrix)
        self.assertIn('MIGRATION_MODE=cold', self.matrix)
        self.assertIn(
            'MIGRATION_ACTIONS="$migration_actions"', self.matrix)

    def test_wrapper_targets_three_nodes_and_reverts_maximum_cases(self):
        self.assertIn(
            'COLD_REVERT_CASES=${COLD_REVERT_CASES:-'
            'local_d3_s3,bfv_d3_s3}',
            self.matrix)
        self.assertIn(
            'migration_actions=confirm,revert,confirm', self.matrix)
        self.assertIn(
            'Cold migration matrix requires three distinct compute names',
            self.matrix)
        self.assertIn(
            'Full cold matrix requires at least three Manila shares',
            self.matrix)
        for token in (
                '${NODE02_HOST}=${NODE02_SSH}',
                '${NODE03_HOST}=${NODE03_SSH}',
                '${NODE01_HOST}=${NODE01_SSH}'):
            self.assertIn(token, self.matrix)

    def test_wrapper_and_release_gate_preserve_strict_ssh_inputs(self):
        for script in (self.matrix, self.release_gate):
            self.assertIn('SSH_KNOWN_HOSTS_FILE', script)
        self.assertIn(
            '[[ -f "$SSH_IDENTITY" && -r "$SSH_IDENTITY" ]]',
            self.matrix)
        self.assertIn(
            '[[ -f "$SSH_KNOWN_HOSTS_FILE" && '
            '-r "$SSH_KNOWN_HOSTS_FILE" ]]',
            self.matrix)
        self.assertIn(
            'SSH_KNOWN_HOSTS_FILE="$SSH_KNOWN_HOSTS_FILE"',
            self.matrix)
        self.assertIn(
            '"$SCRIPT_DIR/openstack-incus-cold-migration-'
            'cardinality-matrix.sh"',
            self.release_gate)

    def test_wrapper_residual_audit_includes_control_plane_resources(self):
        for resource in (
                'baseline_servers', 'baseline_volumes', 'baseline_ports',
                'baseline_allocations', 'final_servers', 'final_volumes',
                'final_ports', 'final_allocations'):
            self.assertIn(resource, self.matrix)
        self.assertIn(
            'and residual-state audit', self.matrix)

    @unittest.skipUnless(
        os.name != 'nt' and shutil.which('bash'),
        'POSIX bash is not installed')
    def test_shell_syntax(self):
        for path in (MIGRATION_E2E, COLD_MATRIX, RELEASE_GATE):
            subprocess.run(
                ['bash', '-n', str(path)],
                check=True,
                capture_output=True,
                text=True)
