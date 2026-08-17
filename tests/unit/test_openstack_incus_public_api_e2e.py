# Copyright 2026
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
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DATA_SCRIPT = (
    REPO_ROOT / 'tools' / 'openstack-incus-initial-data-volume-e2e.sh')
BFV_SCRIPT = (
    REPO_ROOT / 'tools' / 'openstack-incus-bfv-snapshot-public-api-e2e.sh')
BFV_COW_SCRIPT = REPO_ROOT / 'tools' / 'openstack-incus-bfv-cow-e2e.sh'
BFV_PUBLISH_SCRIPT = (
    REPO_ROOT / 'tools' / 'publish-incus-bfv-image-to-glance.sh')
RELEASE_GATE = (
    REPO_ROOT / 'tools' / 'openstack-incus-release-gate.sh')
IDMAP_SCRIPT = (
    REPO_ROOT / 'tools' / 'openstack-incus-idmap-conflict-e2e.sh')
CLEANUP_AUDIT = (
    REPO_ROOT / 'tools' / 'openstack-incus-data-volume-cleanup-audit.sh')
RESIZE_SCRIPT = REPO_ROOT / 'tools' / 'openstack-incus-resize-e2e.sh'
VOLUME_MIGRATION_SCRIPT = (
    REPO_ROOT / 'tools' / 'openstack-incus-volume-migration-e2e.sh')


class PublicApiE2EContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.data = DATA_SCRIPT.read_text(encoding='utf-8')
        cls.bfv = BFV_SCRIPT.read_text(encoding='utf-8')
        cls.bfv_cow = BFV_COW_SCRIPT.read_text(encoding='utf-8')
        cls.bfv_publish = BFV_PUBLISH_SCRIPT.read_text(encoding='utf-8')
        cls.release_gate = RELEASE_GATE.read_text(encoding='utf-8')
        cls.idmap = IDMAP_SCRIPT.read_text(encoding='utf-8')
        cls.cleanup_audit = CLEANUP_AUDIT.read_text(encoding='utf-8')
        cls.resize = RESIZE_SCRIPT.read_text(encoding='utf-8')
        cls.volume_migration = VOLUME_MIGRATION_SCRIPT.read_text(
            encoding='utf-8')

    def test_resize_checks_the_configured_incus_project(self):
        self.assertIn('INCUS_PROJECT=${INCUS_PROJECT:-nova}', self.resize)
        self.assertEqual(
            5, self.resize.count("incus --project '$INCUS_PROJECT' exec"))
        self.assertNotIn('"incus exec ', self.resize)

    def test_volume_migration_checks_the_configured_incus_project(self):
        script = self.volume_migration
        self.assertIn('INCUS_PROJECT=${INCUS_PROJECT:-nova}', script)
        self.assertEqual(
            3, script.count("incus --project '$INCUS_PROJECT' exec"))
        self.assertEqual(
            4, script.count("incus --project '$INCUS_PROJECT' profile"))
        self.assertNotIn('"incus exec ', script)
        self.assertNotIn('"incus profile ', script)
        self.assertIn('data[\\"mountpoint\\"].startswith(\\"/dev/\\")', script)
        self.assertNotIn('data[\\"path\\"]', script)
        self.assertIn('--host "$DEST_HOST" "$server_id" || true', script)
        self.assertNotIn('--host "$DEST_HOST" --wait', script)
        self.assertIn(
            'fuse2fs $DEVICE /mnt/cinder >/dev/null 2>&1', script)

    def test_scripts_default_to_non_destructive(self):
        for script in (self.data, self.bfv):
            self.assertIn('RUN_DESTRUCTIVE=${RUN_DESTRUCTIVE:-false}', script)
            gate = script.index('if [[ "$RUN_DESTRUCTIVE" != true ]]')
            first_create = min(
                pos for token in ('openstack volume create',
                                  'openstack server create')
                if (pos := script.find(token)) >= 0)
            self.assertLess(gate, first_create)

    def test_bfv_snapshot_reads_durable_guest_evidence_fail_closed(self):
        self.assertIn(
            'HOST_SSH_MAP=${HOST_SSH_MAP:?', self.bfv)
        self.assertIn('StrictHostKeyChecking=yes', self.bfv)
        self.assertIn(
            'INCUS_PROJECT" =~ ^[A-Za-z0-9_.-]+$', self.bfv)
        self.assertIn(
            '^[A-Za-z0-9_][A-Za-z0-9_.-]*@', self.bfv)
        self.assertIn('INCUS_RUNTIME_MODE=${INCUS_RUNTIME_MODE:-podman}',
                      self.bfv)
        self.assertIn('incus_runtime_remote()', self.bfv)
        self.assertIn(
            'kubectl -n $namespace get pod -l application=incus', self.bfv)
        self.assertIn('INCUS_KUBE_NODE_MAP=${INCUS_KUBE_NODE_MAP:-}',
                      self.bfv)
        self.assertIn('spec.nodeName=$kube_node', self.bfv)
        self.assertNotIn('hostname -s', self.bfv)
        self.assertIn(
            'incus_runtime_remote "$target" incus --project', self.bfv)
        self.assertIn('/root/openstack-incus-bfv-snapshot-marker', self.bfv)
        self.assertIn('/run/openstack-incus-bfv-restore-check.ok', self.bfv)
        self.assertIn('mkdir -p /usr/local/sbin', self.bfv)
        self.assertIn('mkdir -p /etc/local.d', self.bfv)
        self.assertIn('/dev/console 2>/dev/null || true', self.bfv)
        self.assertIn('console log (diagnostic only)', self.bfv)
        self.assertNotIn('/var/lib/incus', self.bfv)

    def test_bfv_snapshot_parses_current_server_volume_inventory(self):
        self.assertIn(
            'openstack server volume list "$restore_server" -f json',
            self.bfv)
        self.assertIn(
            'item.get("Volume ID") or item.get("ID")', self.bfv)
        self.assertNotIn(
            'server volume list "$restore_server" -f value -c ID',
            self.bfv)

    def test_initial_volume_host_fallback_is_explicit_and_read_only(self):
        self.assertIn('HOST_SSH_MAP=${HOST_SSH_MAP:-}', self.data)
        self.assertIn(
            '[[ -n "$host" && -n "$HOST_SSH_MAP" ]] || return 1',
            self.data)
        self.assertIn(
            'INCUS_RUNTIME_MODE=${INCUS_RUNTIME_MODE:-podman}', self.data)
        self.assertIn(
            'kubectl -n $namespace get pod -l application=incus', self.data)
        self.assertIn('INCUS_KUBE_NODE_MAP=${INCUS_KUBE_NODE_MAP:-}',
                      self.data)
        self.assertIn('spec.nodeName=$kube_node', self.data)
        self.assertNotIn('hostname -s', self.data)
        self.assertIn('StrictHostKeyChecking=yes', self.data)
        self.assertIn(
            'incus --project "$INCUS_PROJECT" exec', self.data)
        self.assertIn(
            '"$instance_name" -- cat "$GUEST_MARKER_LOG"', self.data)
        self.assertNotIn('/var/lib/incus', self.data)

    def test_initial_volume_is_in_first_create_bdm(self):
        self.assertIn('--block-device "$bdm"', self.data)
        self.assertIn('source_type=volume', self.data)
        self.assertIn('destination_type=volume', self.data)
        self.assertIn('boot_index=-1', self.data)
        self.assertIn('delete_on_termination=false', self.data)
        self.assertNotIn('server add volume', self.data)

    def test_initial_volume_checks_first_boot_and_reboot_data(self):
        self.assertIn('OPENSTACK_INCUS_DATA_FIRST_OK', self.data)
        self.assertIn('OPENSTACK_INCUS_DATA_REBOOT_OK', self.data)
        self.assertIn('server reboot --hard --wait "$server_id"', self.data)
        self.assertIn('fuse2fs "\\$device" "\\$mountpoint"', self.data)
        self.assertNotIn('fuse2fs -o rw+', self.data)
        self.assertIn(
            'grep -q " \\$mountpoint fuse" /proc/mounts', self.data)
        self.assertNotIn('mount -t ext4', self.data)

    def test_initial_volume_requires_exact_attachment_inventory(self):
        self.assertIn(
            'EXPECTED_ROOT_VOLUME_ID="$root_volume_id"', self.data)
        self.assertIn('if set(row_ids) != expected_ids:', self.data)
        self.assertIn(
            'server volume attachment inventory contains duplicates',
            self.data)
        self.assertIn(
            'BFV root volume is not attached exactly once', self.data)
        self.assertIn(
            'Failed to persist exact cleanup evidence', self.data)

    def test_bfv_snapshot_uses_nova_and_cinder_public_apis(self):
        self.assertIn('server image create', self.bfv)
        self.assertIn('block_device_mapping', self.bfv)
        self.assertIn('source_type == "snapshot"', self.bfv)
        self.assertIn('volume snapshot show', self.bfv)
        self.assertIn('--image "$snapshot_image"', self.bfv)
        self.assertNotIn('--boot-from-volume', self.bfv)

    def test_bfv_restore_requires_persisted_guest_marker(self):
        self.assertIn('OPENSTACK_INCUS_BFV_SOURCE_OK', self.bfv)
        self.assertIn('OPENSTACK_INCUS_BFV_RESTORE_OK', self.bfv)
        self.assertIn(
            'Restore reused the source root volume instead of a snapshot '
            'clone',
            self.bfv)

    def test_bfv_snapshot_and_restore_prove_cinder_lineage(self):
        self.assertIn(
            'BFV image-create must expose exactly one root snapshot UUID',
            self.bfv)
        self.assertIn(
            'Nova image-create snapshot does not belong to the source root',
            self.bfv)
        self.assertIn(
            'restored BFV root volume was not created from the Nova image',
            self.bfv)

    def test_cleanup_uses_captured_uuid_variables(self):
        self.assertIn('server delete --wait "$server_id"', self.data)
        self.assertIn(
            'for candidate in "${volume_ids[@]}" "$root_volume_id";',
            self.data)
        self.assertIn('volume delete "$candidate"', self.data)
        self.assertNotIn('server delete --wait "$NAME', self.data)
        self.assertNotIn('volume delete "$NAME', self.data)

        self.assertIn('server delete --wait "$server"', self.bfv)
        self.assertIn('volume delete "$volume"', self.bfv)
        self.assertIn('image delete "$snapshot_image"', self.bfv)
        self.assertIn('volume snapshot delete "$snapshot"', self.bfv)
        self.assertNotIn('server delete --wait "$NAME', self.bfv)

    def test_cleanup_requires_positive_absence_proof_before_pass(self):
        for script in (self.data, self.bfv):
            self.assertIn('resource_exists_exact()', script)
            self.assertIn('list_command=', script)
            self.assertIn('grep -Fqx -- "$resource_id"', script)
            self.assertIn('cleanup_failed=true', script)
            self.assertIn('pass_message=', script)
            self.assertNotIn(
                'echo "PASS public API',
                script)

    def test_release_gate_requires_all_public_api_storage_e2es(self):
        self.assertIn(
            'RUN_PUBLIC_API_E2E=${RUN_PUBLIC_API_E2E:-false}',
            self.release_gate)
        self.assertIn(
            '"$SCRIPT_DIR/openstack-incus-initial-data-volume-matrix.sh"',
            self.release_gate)
        self.assertIn(
            '"$SCRIPT_DIR/openstack-incus-bfv-snapshot-public-api-e2e.sh"',
            self.release_gate)
        self.assertIn(
            '"$SCRIPT_DIR/openstack-incus-bfv-cow-e2e.sh"',
            self.release_gate)
        self.assertIn(
            'PUBLIC_API_CINDER_POOL:?Set the Cinder RBD pool under test',
            self.release_gate)
        self.assertIn('HOST_SSH_MAP="$COMPUTE_NODES"', self.release_gate)
        self.assertIn('CINDER_POOL="$PUBLIC_API_CINDER_POOL"',
                      self.release_gate)
        self.assertIn(
            '"$RUN_PUBLIC_API_E2E" == true',
            self.release_gate)
        self.assertIn('REQUIRE_HOST_CLEANUP_AUDIT=true', self.release_gate)
        self.assertIn('NOVA_INSTANCES_PATH="$NOVA_INSTANCES_PATH"',
                      self.release_gate)

    def test_bfv_cow_proves_exact_parent_lineage(self):
        self.assertIn("direct_url=$(openstack image show", self.bfv_cow)
        self.assertIn("parent_pool=$(jq -r '.parent.pool", self.bfv_cow)
        self.assertIn('[[ "$parent_pool" == "$glance_pool" ]]',
                      self.bfv_cow)
        self.assertIn('((overlap > 0))', self.bfv_cow)

    def test_bfv_cow_supports_kubernetes_ceph_toolbox(self):
        self.assertIn(
            'RBD_RUNTIME_MODE=${RBD_RUNTIME_MODE:-local}', self.bfv_cow)
        self.assertIn(
            'kubectl -n "$RBD_KUBE_NAMESPACE" exec "$RBD_KUBE_TARGET"',
            self.bfv_cow)
        self.assertEqual(2, self.bfv_cow.count('rbd_cmd --id'))

    def test_bfv_publish_converges_to_one_requested_store(self):
        script = self.bfv_publish
        self.assertIn('IMAGE_STORE=${IMAGE_STORE:-}', script)
        self.assertIn(
            'image import --method copy-image --store "$IMAGE_STORE"',
            script)
        self.assertIn('while ((SECONDS < deadline))', script)
        self.assertIn('image delete --store "$store"', script)
        visibility = script.index(
            'openstack image set "--$VISIBILITY" "$created_image_id"')
        remove_default = script.index(
            'openstack image delete --store "$store"')
        self.assertLess(remove_default, visibility)

    def test_initial_volume_cleanup_audit_is_exact_and_fail_closed(self):
        self.assertIn('StrictHostKeyChecking=yes', self.cleanup_audit)
        self.assertIn(
            'INCUS_RUNTIME_MODE=${INCUS_RUNTIME_MODE:-podman}',
            self.cleanup_audit)
        self.assertIn('incus_runtime_remote()', self.cleanup_audit)
        self.assertIn('application=incus', self.cleanup_audit)
        self.assertIn('incus-volume-journal/$server_uuid',
                      self.cleanup_audit)
        self.assertIn('profile list --format json', self.cleanup_audit)
        self.assertIn('rbd device list --format json', self.cleanup_audit)
        self.assertIn('"volume-" + item', self.cleanup_audit)
        self.assertIn("%$'\\r'", self.cleanup_audit)
        self.assertNotIn('|| true', self.cleanup_audit)

    def test_release_gate_executes_complete_migration_evidence(self):
        self.assertIn(
            'MIGRATION_COMPUTE_NODES must contain exactly three mappings',
            self.release_gate)
        self.assertIn(
            'for ((source_index = 0;',
            self.release_gate)
        self.assertIn(
            'for ((dest_index = 0;',
            self.release_gate)
        self.assertIn(
            '"$SCRIPT_DIR/openstack-incus-bfv-migration-matrix.sh"',
            self.release_gate)
        self.assertIn(
            '"$SCRIPT_DIR/openstack-incus-live-migration-matrix.sh"',
            self.release_gate)
        self.assertIn(
            '"$SCRIPT_DIR/openstack-incus-live-migration-'
            'cardinality-matrix.sh"',
            self.release_gate)
        self.assertIn(
            '"$SCRIPT_DIR/openstack-incus-cold-migration-'
            'cardinality-matrix.sh"',
            self.release_gate)
        self.assertIn('INJECT_RESTORE_FAILURE=1', self.release_gate)
        self.assertIn('DATA_VOLUME_COUNT=3', self.release_gate)
        self.assertIn(
            'MIGRATION_MANILA_SHARES must contain at least three shares',
            self.release_gate)
        self.assertIn(
            'bash "$SCRIPT_DIR/openstack-incus-idmap-conflict-e2e.sh"',
            self.release_gate)
        self.assertIn(
            'node_index < ${#migration_nodes[@]}',
            self.release_gate)

    def test_idmap_probe_rejects_overlap_without_persisting_attempt(self):
        self.assertIn('RUN_DESTRUCTIVE=${RUN_DESTRUCTIVE:-false}', self.idmap)
        self.assertIn('security.idmap.isolated', self.idmap)
        self.assertIn('volatile.idmap.base', self.idmap)
        self.assertIn('/1.0/migration-attempts/', self.idmap)
        self.assertIn("grep -qi 'overlap.*instance'", self.idmap)
        self.assertIn('rejected migration attempt was persisted', self.idmap)
        self.assertIn(
            'openstack server delete --wait "$server_id"', self.idmap)

    def test_release_decision_uses_completed_phases_not_mutable_run_flags(
            self):
        for phase in (
                'PHASE_FLEET_PASSED',
                'PHASE_TEMPEST_PASSED',
                'PHASE_PUBLIC_API_E2E_PASSED',
                'PHASE_SCALE_PASSED',
                'PHASE_MIGRATION_MATRIX_PASSED',
                'PHASE_DESTRUCTIVE_FENCE_PASSED'):
            self.assertIn('{}=false'.format(phase), self.release_gate)
            self.assertIn('{}=true'.format(phase), self.release_gate)
            self.assertIn('"${}" == true'.format(phase), self.release_gate)
        self.assertNotIn(
            'source "$EVACUATION_E2E_ENV"\n    run',
            self.release_gate)

    def test_release_gate_rejects_empty_tempest_statistics(self):
        self.assertIn('values["total"] <= 0', self.release_gate)
        self.assertIn(
            'values["passed"] + values["skipped"] != values["total"]',
            self.release_gate)
        self.assertNotIn(
            "grep -Eq '^Failed:[[:space:]]+0$'",
            self.release_gate)

    def test_release_gate_records_skips_but_requires_supported_tests(self):
        self.assertIn('tempest-required-pass-', self.release_gate)
        self.assertIn('tempest-passed-', self.release_gate)
        self.assertIn('tempest-skipped-', self.release_gate)
        self.assertIn(
            'Required Incus Tempest scenario did not pass',
            self.release_gate)
        self.assertIn(
            'No standard Tempest compute API test passed',
            self.release_gate)
        self.assertIn(
            'Tempest skipped-test evidence is incomplete',
            self.release_gate)

    @unittest.skipUnless(
        os.name != 'nt' and shutil.which('bash'),
        'POSIX bash is not installed')
    def test_initial_volume_cleanup_needs_successful_absence_inventory(self):
        fake_openstack = r'''
#!/usr/bin/env python3
import json
import os
from pathlib import Path
import re
import sys

args = sys.argv[1:]
if args[:1] == ["--os-compute-api-version"]:
    args = args[2:]
state_path = Path(os.environ["FAKE_STATE"])
state = (
    json.loads(state_path.read_text(encoding="utf-8"))
    if state_path.exists() else {}
)
server_id = "11111111-1111-4111-8111-111111111111"
volume_id = "22222222-2222-4222-8222-222222222222"


def save():
    state_path.write_text(json.dumps(state), encoding="utf-8")


def contains(*values):
    iterator = iter(args)
    return all(any(item == value for item in iterator) for value in values)


if args[:2] == ["volume", "create"]:
    state["volume"] = "available"
    save()
    print(volume_id)
elif contains("server", "create"):
    user_data = args[args.index("--user-data") + 1]
    content = Path(user_data).read_text(encoding="utf-8")
    state["token"] = re.search(r"^TOKEN='([^']+)'$", content, re.M).group(1)
    state["server"] = "ACTIVE"
    state["volume"] = "in-use"
    save()
    print(server_id)
elif args[:3] == ["server", "volume", "list"]:
    print(json.dumps([{"ID": volume_id, "Device": "/dev/vdb"}]))
elif args[:3] == ["console", "log", "show"]:
    token = state["token"]
    print("OPENSTACK_INCUS_DATA_FIRST_OK:" + token)
    print("OPENSTACK_INCUS_DATA_REBOOT_OK:" + token)
elif contains("server", "reboot"):
    pass
elif args[:2] == ["server", "delete"]:
    state["server"] = "deleted"
    state["volume"] = "available"
    save()
elif args[:2] == ["server", "show"]:
    if state.get("server") == "deleted":
        raise SystemExit(1)
    if "OS-EXT-SRV-ATTR:instance_name" in args:
        print("instance-00000001")
    else:
        print(state.get("server", "ACTIVE"))
elif args[:2] == ["server", "list"]:
    if state.get("server") != "deleted":
        print(server_id)
elif args[:2] == ["volume", "delete"]:
    state["volume"] = "deleted"
    save()
elif args[:2] == ["volume", "show"]:
    if state.get("volume") == "deleted":
        raise SystemExit(1)
    print(state.get("volume", "available"))
elif args[:2] == ["volume", "list"]:
    if state.get("volume") == "deleted":
        if os.environ.get("FAKE_LIST_FAILURE") == "true":
            raise SystemExit(2)
    else:
        print(volume_id)
else:
    print("unexpected fake OpenStack command: " + repr(args), file=sys.stderr)
    raise SystemExit(2)
'''
        with tempfile.TemporaryDirectory() as directory:
            directory = pathlib.Path(directory)
            executable = directory / 'openstack'
            executable.write_text(
                textwrap.dedent(fake_openstack).lstrip(),
                encoding='utf-8')
            executable.chmod(0o755)

            def run(fake_list_failure):
                environment = os.environ.copy()
                environment.update({
                    'RUN_DESTRUCTIVE': 'true',
                    'IMAGE': 'image-id',
                    'FLAVOR': 'flavor-id',
                    'NETWORK': 'network-id',
                    'VOLUME_TYPE': 'volume-type',
                    'TIMEOUT': '1',
                    'FAKE_STATE': str(
                        directory / 'state-{}.json'.format(
                            fake_list_failure)),
                    'FAKE_LIST_FAILURE': str(fake_list_failure).lower(),
                    'PATH': '{}{}{}'.format(
                        directory, os.pathsep, environment['PATH']),
                })
                return subprocess.run(
                    ['bash', str(DATA_SCRIPT)],
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=20)

            success = run(False)
            failure = run(True)

        self.assertEqual(0, success.returncode, success.stderr)
        self.assertIn('PASS public API', success.stdout)
        self.assertNotEqual(0, failure.returncode, failure.stdout)
        self.assertNotIn('PASS public API', failure.stdout)
        self.assertIn(
            'Could not prove cleanup of volume UUID', failure.stderr)

    @unittest.skipUnless(
        os.name != 'nt' and shutil.which('bash'),
        'POSIX bash is not installed')
    def test_shell_syntax(self):
        for path in (DATA_SCRIPT, BFV_SCRIPT, IDMAP_SCRIPT, CLEANUP_AUDIT):
            subprocess.run(
                ['bash', '-n', str(path)],
                check=True,
                capture_output=True,
                text=True)
