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
HOST_PREFLIGHT = (
    REPO_ROOT / 'tools' / 'openstack-incus-production-preflight.sh')
FLEET_PREFLIGHT = (
    REPO_ROOT / 'tools' / 'openstack-incus-fleet-preflight.sh')
RELEASE_GATE = REPO_ROOT / 'tools' / 'openstack-incus-release-gate.sh'
BFV_MATRIX = REPO_ROOT / 'tools' / 'openstack-incus-bfv-migration-matrix.sh'
BFV_E2E = REPO_ROOT / 'tools' / 'openstack-incus-bfv-migration-e2e.sh'
LIVE_E2E = REPO_ROOT / 'tools' / 'openstack-incus-live-migration-e2e.sh'
LIVE_MATRIX = REPO_ROOT / 'tools' / 'openstack-incus-live-migration-matrix.sh'
LIVE_CARDINALITY = (
    REPO_ROOT / 'tools' /
    'openstack-incus-live-migration-cardinality-matrix.sh')
COLD_CARDINALITY = (
    REPO_ROOT / 'tools' /
    'openstack-incus-cold-migration-cardinality-matrix.sh')


class MigrationAddressPreflightContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.host = HOST_PREFLIGHT.read_text(encoding='utf-8')
        cls.fleet = FLEET_PREFLIGHT.read_text(encoding='utf-8')

    def test_host_allows_wildcard_or_explicit_ipv4_bind(self):
        self.assertIn(
            'must use :PORT, 0.0.0.0:PORT or an explicit IPv4:PORT',
            self.host)
        self.assertIn('is_ipv4 "$https_bind_host"', self.host)
        self.assertIn('is_tcp_port "$https_bind_port"', self.host)

    def test_host_uses_nova_migration_address_as_advertised_endpoint(self):
        self.assertIn(
            'crudini --get "$NOVA_CONFIG" incus \\\n'
            '    migration_address',
            self.host)
        self.assertIn(
            'must use https://<non-wildcard IPv4>:PORT',
            self.host)
        self.assertIn('[[ "$migration_host" != 0.0.0.0 ]]', self.host)
        self.assertNotIn(
            'check_equal "Nova migration address" "https://$https_address"',
            self.host)

    def test_host_matches_bind_port_and_probes_advertised_endpoint(self):
        self.assertIn(
            '[[ "$migration_port" == "$https_bind_port" ]]',
            self.host)
        self.assertIn(
            'socket.create_connection(\n'
            '            (sys.argv[1], int(sys.argv[2])), timeout=5)',
            self.host)
        self.assertIn(
            'pass "migration TCP reachability" '
            '"$migration_host:$migration_port"',
            self.host)

    def test_fleet_reads_migration_address_from_nova_config(self):
        self.assertIn(
            'REMOTE_NOVA_CONFIG=${REMOTE_NOVA_CONFIG:-'
            '/etc/nova/nova-cpu.conf}',
            self.fleet)
        self.assertIn(
            "crudini --get '$REMOTE_NOVA_CONFIG' incus migration_address",
            self.fleet)
        self.assertNotIn(
            'incus config get core.https_address',
            self.fleet)

    def test_fleet_uniqueness_uses_advertised_migration_address(self):
        self.assertIn(
            'declare -A seen_migration_addresses=()',
            self.fleet)
        self.assertIn(
            'seen_migration_addresses[$migration_address]',
            self.fleet)
        self.assertIn(
            'invalid advertised endpoint:',
            self.fleet)
        self.assertNotIn('seen_addresses[$https_address]', self.fleet)


class SplitRuntimePreflightContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.host = HOST_PREFLIGHT.read_text(encoding='utf-8')

    def test_checks_independent_lxcfs_and_incus_services(self):
        self.assertIn(
            'INCUS_LXCFS_SERVICE=${INCUS_LXCFS_SERVICE:-'
            'incus-lxcfs.service}',
            self.host)
        self.assertIn(
            'check_systemd_service "$INCUS_LXCFS_SERVICE"', self.host)
        self.assertIn('check_systemd_service "$INCUS_SERVICE"', self.host)
        self.assertIn(
            'fail "competing host lxcfs.service"', self.host)

    def test_checks_runtime_roles_and_persistent_run_directory(self):
        self.assertIn(
            '"$INCUS_LXCFS_CONTAINER:lxcfs"', self.host)
        self.assertIn('"$INCUS_CONTAINER:incusd"', self.host)
        self.assertIn(
            'check_equal "Incus runtime preservation" restart',
            self.host)

    def test_checks_host_and_container_lxcfs_health(self):
        self.assertIn('pass "host LXCFS response"', self.host)
        self.assertIn('pass "LXCFS container health"', self.host)
        self.assertIn('pass "Incus container health"', self.host)
        self.assertIn(
            'expected shared or rshared', self.host)

    def test_rejects_incus_managed_networks(self):
        self.assertIn(
            'incus network list \\\n'
            '    --all-projects --format csv -c emn',
            self.host)
        self.assertIn(
            'fail "managed Incus networks"', self.host)
        self.assertIn(
            'unsupported on Neutron nodes', self.host)

    def test_checks_every_running_nova_guest(self):
        running_block = self.host[
            self.host.index('if running_inventory=$('):
            self.host.index('for instance_name in "${running_nova_instances')]
        self.assertIn(
            'select(.status == "Running")', running_block)
        self.assertNotIn('user.openstack.uuid', running_block)
        self.assertIn(
            'runtime_config="$INCUS_RUNTIME_ROOT/'
            '${INCUS_PROJECT}_${instance_name}/lxc.conf"',
            self.host)
        self.assertIn(
            'pass "guest LXCFS response:$instance_name"',
            self.host)

    def test_both_runtime_images_are_immutable_and_revision_checked(self):
        self.assertIn(
            'check_runtime_image "Incus LXCFS"', self.host)
        self.assertIn(
            'check_runtime_image "Incus control"', self.host)
        self.assertIn(
            'Image= must use an immutable @sha256 reference',
            self.host)


class IDMapAllocatorPreflightContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.host = HOST_PREFLIGHT.read_text(encoding='utf-8')
        cls.fleet = FLEET_PREFLIGHT.read_text(encoding='utf-8')

    def test_host_requires_tls_rbac_and_private_secret_modes(self):
        self.assertIn(
            'idmap_allocator_allow_insecure must be false in production',
            self.host)
        self.assertIn('idmap_allocator_ca_cert', self.host)
        self.assertIn('idmap_allocator_client_cert', self.host)
        self.assertIn('idmap_allocator_client_key', self.host)
        self.assertIn('idmap allocator client key mode', self.host)
        self.assertIn('idmap_allocator_username', self.host)
        self.assertIn('idmap_allocator_password_file', self.host)
        self.assertIn('idmap allocator password file', self.host)

    def test_fleet_compares_transport_policy(self):
        self.assertIn(
            'idmap_allocator_allow_insecure', self.fleet)
        self.assertIn('idmap_allocator_username', self.fleet)
        self.assertIn("printf '%s|%s|%s|%s|%s|%s|%s'", self.fleet)

    def test_host_requires_a_persistent_compute_identity(self):
        self.assertIn('Nova persistent compute identity', self.host)
        self.assertIn('compute_id_path=', self.host)
        self.assertIn('"$state_fstype" != tmpfs', self.host)
        self.assertIn('"$state_fstype" != overlay', self.host)

    def test_fleet_rejects_duplicate_compute_identities(self):
        self.assertIn('seen_compute_ids', self.fleet)
        self.assertIn('duplicate of ${seen_compute_ids[$compute_id]}',
                      self.fleet)


class SharedCephPoolPrefixContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.host = HOST_PREFLIGHT.read_text(encoding='utf-8')
        cls.fleet = FLEET_PREFLIGHT.read_text(encoding='utf-8')

    def test_fleet_collects_ceph_pool_identity_per_host(self):
        self.assertIn('ceph_pool_records=()', self.fleet)
        self.assertIn("select(.driver==\\\"ceph\\\")", self.fleet)
        self.assertIn('ceph.rbd.image_prefix', self.fleet)

    def test_fleet_requires_distinct_prefixes_on_shared_osd_pools(self):
        self.assertIn(
            'shares OSD pool ${group#*|} with other hosts but has no '
            'ceph.rbd.image_prefix',
            self.fleet)
        self.assertIn('duplicates ${group_prefix_owner[$member_prefix]}',
                      self.fleet)

    def test_fleet_groups_by_cluster_and_source_not_pool_name(self):
        self.assertIn('group="$record_cluster|$record_source"', self.fleet)

    def test_host_preflight_requires_versioned_storage_protocol(self):
        self.assertIn('storage_materialization_attempt_v1', self.host)
        self.assertIn('storage_release_receipt_v2', self.host)
        self.assertNotIn('storage_release_receipt \\', self.host)

    def test_fleet_checks_journal_directory_ownership(self):
        """A root-owned journal directory breaks every data-volume attach.

        nova-compute writes these journals as the service user, so an
        ownership drift only surfaces much later as "Permission denied"
        during attach. It cost a full matrix run to find once.
        """
        self.assertIn('incus-volume-journal', self.fleet)
        self.assertIn('incus-share-journal', self.fleet)
        self.assertIn('incus-spawn-attempts', self.fleet)
        self.assertIn('journal directories', self.fleet)
        self.assertIn('MainPID', self.fleet)


class ReleaseSshIdentityContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.gate = RELEASE_GATE.read_text(encoding='utf-8')
        cls.executors = [
            path.read_text(encoding='utf-8')
            for path in (FLEET_PREFLIGHT, BFV_MATRIX, BFV_E2E, LIVE_E2E)
        ]
        cls.wrappers = [
            path.read_text(encoding='utf-8')
            for path in (LIVE_MATRIX, LIVE_CARDINALITY, COLD_CARDINALITY)
        ]

    def test_remote_executors_force_batch_and_known_host_checks(self):
        for script in self.executors:
            self.assertIn('-o BatchMode=yes', script)
            self.assertIn('-o StrictHostKeyChecking=yes', script)
            self.assertIn(
                '-o "UserKnownHostsFile=$SSH_KNOWN_HOSTS_FILE"',
                script)
            self.assertIn(
                'SSH_KNOWN_HOSTS_FILE=${SSH_KNOWN_HOSTS_FILE:-'
                '$HOME/.ssh/known_hosts}',
                script)
            self.assertNotIn('StrictHostKeyChecking=no', script)
            self.assertNotIn('StrictHostKeyChecking=accept-new', script)

    def test_matrix_wrappers_forward_exact_known_hosts_file(self):
        for script in self.wrappers:
            self.assertIn(
                'SSH_KNOWN_HOSTS_FILE=${SSH_KNOWN_HOSTS_FILE:-'
                '$HOME/.ssh/known_hosts}',
                script)
            self.assertIn(
                'SSH_KNOWN_HOSTS_FILE="$SSH_KNOWN_HOSTS_FILE"',
                script)

    def test_release_gate_prevalidates_all_remote_host_mappings(self):
        self.assertIn(
            'ssh-keygen -F "$host" -f "$SSH_KNOWN_HOSTS_FILE"',
            self.gate)
        self.assertIn(
            'require_node_mapping_hosts COMPUTE_NODES "$COMPUTE_NODES"',
            self.gate)
        self.assertIn(
            'MIGRATION_COMPUTE_NODES "$MIGRATION_COMPUTE_NODES"',
            self.gate)
        self.assertIn(
            'require_known_host CONTROLLER_SSH "$CONTROLLER_SSH"',
            self.gate)
        self.assertNotIn('ssh-keyscan', self.gate)


class MigrationResidualAuditContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.e2e = LIVE_E2E.read_text(encoding='utf-8')
        cls.wrappers = [
            path.read_text(encoding='utf-8')
            for path in (LIVE_MATRIX, LIVE_CARDINALITY, COLD_CARDINALITY)
        ]

    def test_e2e_requires_exact_placement_consumer_cleanup(self):
        self.assertIn(
            'openstack resource provider allocation show \\\n'
            '    "$server_id" -f json', self.e2e)
        self.assertIn(
            "jq -e 'length == 0' <<<\"$placement_allocations\"",
            self.e2e)

    def test_global_baselines_require_explicit_quiescent_cloud_mode(self):
        for script in self.wrappers:
            self.assertIn(
                'QUIESCENT_CLOUD_AUDIT=${QUIESCENT_CLOUD_AUDIT:-0}',
                script)
            baseline = script.index('baseline_servers=$(')
            gate = script.rindex(
                'if [[ "$QUIESCENT_CLOUD_AUDIT" == 1 ]]', 0, baseline)
            self.assertLess(gate, baseline)


class BfvCommandTimeoutContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.e2e = BFV_E2E.read_text(encoding='utf-8')
        cls.matrix = BFV_MATRIX.read_text(encoding='utf-8')

    def test_remote_commands_have_configurable_timeout(self):
        for script in (self.e2e, self.matrix):
            self.assertIn('COMMAND_TIMEOUT=${COMMAND_TIMEOUT:-30}', script)
            self.assertIn(
                'timeout --foreground "${ACTIVE_COMMAND_TIMEOUT}s"',
                script)
            self.assertIn('"ConnectTimeout=$ACTIVE_COMMAND_TIMEOUT"',
                          script)
            self.assertIn('if [[ -n "${ACTIVE_COMMAND_TIMEOUT:-}" ]]',
                          script)

    def test_e2e_waits_cap_commands_to_remaining_deadline(self):
        self.assertIn('run_until_deadline "$deadline" "$@"', self.e2e)
        self.assertIn('latest_migration_status 2>/dev/null || true)',
                      self.e2e)
        self.assertIn('run_until_deadline "$deadline" port_status', self.e2e)

    def test_matrix_forwards_command_timeout_to_cases(self):
        self.assertIn('COMMAND_TIMEOUT="$COMMAND_TIMEOUT"', self.matrix)
        self.assertIn(
            'run_until_deadline "$deadline" snapshot_node "$ssh_host"',
            self.matrix)

    def test_bfv_uses_migration_stable_guest_interface_name(self):
        self.assertIn('guest_iface="nic${port_id//-/}"', self.e2e)
        self.assertIn('guest_iface=${guest_iface:0:15}', self.e2e)
        self.assertNotIn('ip -4 -o addr show eth0', self.e2e)

    def test_bfv_start_failpoint_reads_the_nova_project_profile(self):
        self.assertIn(
            '"/1.0/profiles/$instance_name?project=$INCUS_PROJECT"',
            self.e2e)

    def test_bfv_start_failpoint_waits_for_target_instance(self):
        target_ready = 'until incus "$DEST_SSH" list "$instance_name" \\'
        target_instance = '--format csv -c s >/dev/null 2>&1; do'
        delete_vif = 'ip link delete "$vif_source"'

        self.assertIn(target_ready, self.e2e)
        self.assertIn(target_instance, self.e2e)
        self.assertLess(self.e2e.index(target_ready),
                        self.e2e.index(delete_vif))
        self.assertNotIn(
            'until ip link show "$vif_source"', self.e2e)


class FullCheckpointMigrationContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.host = HOST_PREFLIGHT.read_text(encoding='utf-8')
        cls.e2e = LIVE_E2E.read_text(encoding='utf-8')

    def test_host_validates_inventories_before_clean_state_assertions(self):
        self.assertIn(
            '"/1.0/instances?project=$incus_project_query&recursion=2"',
            self.host)
        self.assertIn(
            '"/1.0/profiles?project=$incus_project_query&recursion=1"',
            self.host)
        self.assertNotIn(
            '--project "$INCUS_PROJECT" query', self.host)
        self.assertGreaterEqual(
            self.host.count(
                "type == \"array\" and all(.[]; type == \"object\")"),
            2)
        inventory = self.host.index('nova_instance_inventory=$(')
        running = self.host.index('pass "running guest LXCFS audit"')
        autostart = self.host.index('pass "Nova instance autostart"')
        self.assertLess(inventory, running)
        self.assertLess(inventory, autostart)
        self.assertIn('inventory_valid=false', self.host)
        self.assertIn('profile_inventory_valid=false', self.host)

    def test_host_binds_inventory_to_explicit_nova_project_authority(self):
        authority = self.host.index(
            'configured_incus_project=$(crudini --get "$NOVA_CONFIG" '
            'incus project')
        instance_inventory = self.host.index('nova_instance_inventory=')
        profile_inventory = self.host.index('nova_profile_inventory=')
        self.assertLess(authority, instance_inventory)
        self.assertLess(authority, profile_inventory)
        self.assertIn(
            '[[ -z "$configured_incus_project" ]]', self.host)
        self.assertIn(
            '[[ "$configured_incus_project" != "$INCUS_PROJECT" ]]',
            self.host)
        self.assertIn('incus_project_valid=false', self.host)
        self.assertGreaterEqual(
            self.host.count('[[ "$incus_project_valid" != true ]]'), 2)
        self.assertIn(
            '"/1.0/projects/$incus_project_query"', self.host)
        self.assertIn('$value | @uri', self.host)

    def test_host_requires_bounded_bfv_reimage_event_timeout(self):
        self.assertIn(
            'MIN_REIMAGE_TIMEOUT_PER_GB=${MIN_REIMAGE_TIMEOUT_PER_GB:-60}',
            self.host)
        self.assertIn(
            'crudini --get "$NOVA_CONFIG" DEFAULT \\\n'
            '    reimage_timeout_per_gb', self.host)
        self.assertIn(
            'reimage_timeout_per_gb >= MIN_REIMAGE_TIMEOUT_PER_GB',
            self.host)

    def test_host_requires_profile_local_and_expanded_false(self):
        compact = ' '.join(self.host.split())
        self.assertIn(
            '.profiles != [.name]', self.host)
        self.assertIn(
            '.config["migration.incremental.memory"] != "false"', compact)
        self.assertIn(
            '.expanded_config["migration.incremental.memory"] != "false"',
            compact)
        self.assertIn(
            '$named[0].config["migration.incremental.memory"] != "false"',
            compact)
        self.assertIn('Nova CRIU full checkpoint', self.host)

    def test_host_missing_named_profile_is_always_unsafe(self):
        self.assertIn(
            'if ($named | length) != 1 then', self.host)
        self.assertIn(
            'Nova instance ownership', self.host)
        self.assertIn('invalid records:', self.host)

    def test_host_audits_every_dedicated_project_instance_owner(self):
        self.assertIn(
            '.config["user.openstack.uuid"] as $owner', self.host)
        self.assertIn(
            '$named[0].config["user.openstack.uuid"] != $owner', self.host)
        self.assertIn(
            '.expanded_config["user.openstack.uuid"] != $owner', self.host)

    def test_live_e2e_checks_every_effective_configuration_layer(self):
        self.assertIn('verify_full_checkpoint_policy()', self.e2e)
        self.assertIn(
            '"/1.0/instances/$instance_name"', self.e2e)
        self.assertIn(
            '"/1.0/profiles/$instance_name"', self.e2e)
        self.assertIn(
            '"${path}?project=${INCUS_PROJECT}"', self.e2e)
        self.assertNotIn('incus_remote "$host" query', self.e2e)
        self.assertGreaterEqual(
            self.e2e.count('type == "object"'), 2)
        self.assertIn(
            '.expanded_config["migration.incremental.memory"] == "false"',
            self.e2e)
        self.assertGreaterEqual(self.e2e.count('== $nova_uuid'), 3)
        self.assertGreaterEqual(
            self.e2e.count('--arg nova_uuid "$server_id"'), 2)
        for layer in (
                '$named[0].config["security.privileged"]',
                '.config["security.privileged"]',
                '.expanded_config["security.privileged"]'):
            self.assertIn(layer, self.host)
        self.assertGreaterEqual(
            self.e2e.count('.config["security.privileged"]'), 2)
        self.assertIn(
            '.expanded_config["security.privileged"]', self.e2e)
        for token in ('true', '1', 'yes', 'on'):
            expected = '$value == "{}"'.format(token)
            self.assertIn(expected, self.e2e)
            self.assertIn(expected, self.host)
        self.assertGreaterEqual(
            self.e2e.count('verify_full_checkpoint_policy'), 5)

    def test_restore_injection_uses_persistent_marker_not_outer_journal(self):
        self.assertIn(
            'openstack-incus-e2e-criu-${server_id}.marker', self.e2e)
        self.assertIn(
            'request reached target restore without depending on stdout',
            self.e2e)
        self.assertNotIn('journalctl', self.e2e)

    def test_live_e2e_waits_for_a_stable_guest_boot_before_pid_baseline(self):
        ready = self.e2e.index('test -f /root/criu-e2e-ready')
        cloud_init = self.e2e.index('cloud-init status 2>/dev/null')
        stable_pid = self.e2e.index('stable_pid=$(incus_remote')
        baseline = self.e2e.index('source_pid=$(incus_remote')
        self.assertLess(ready, cloud_init)
        self.assertLess(cloud_init, stable_pid)
        self.assertLess(stable_pid, baseline)
        self.assertIn('kill -0 "$pid"', self.e2e)
        self.assertIn('"$observed_pid" == "$stable_pid"', self.e2e)

    def test_live_e2e_selects_root_format_specific_default_image(self):
        self.assertIn(
            'LOCAL_ROOT_IMAGE=${LOCAL_ROOT_IMAGE:-'
            'alpine-3.21-cloud-incus-criu-fuse}',
            self.e2e)
        self.assertIn(
            'BFV_ROOT_IMAGE=${BFV_ROOT_IMAGE:-alpine-3.21-criu-bfv-fuse}',
            self.e2e)
        selector = self.e2e.index('if [[ -z "$IMAGE" ]]')
        boot = self.e2e.index('if [[ "$BOOT_FROM_VOLUME" == "1" ]]', selector)
        self.assertLess(selector, boot)
        self.assertIn('IMAGE=$BFV_ROOT_IMAGE', self.e2e[boot:])
        self.assertIn('IMAGE=$LOCAL_ROOT_IMAGE', self.e2e[boot:])


if __name__ == '__main__':
    unittest.main()
