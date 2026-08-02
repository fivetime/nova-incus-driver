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
            'must use 0.0.0.0:PORT or an explicit IPv4:PORT',
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
        self.assertIn(
            'select(.config["user.openstack.uuid"] != null)',
            self.host)
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


if __name__ == '__main__':
    unittest.main()
