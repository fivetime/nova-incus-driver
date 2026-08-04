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

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import uuid


SCRIPT = (
    Path(__file__).resolve().parents[2] /
    'tools' / 'openstack-incus-scale-e2e.py')
if importlib.util.find_spec('openstack') is None:
    sys.modules['openstack'] = types.ModuleType('openstack')
SPEC = importlib.util.spec_from_file_location(
    'openstack_incus_scale_e2e', SCRIPT)
scale = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scale)


def resource(resource_id, **values):
    return types.SimpleNamespace(id=resource_id, **values)


class FakeCompute:
    def __init__(self, server_snapshots):
        self.server_snapshots = list(server_snapshots)
        self.deleted = []
        self.current = {}

    def servers(self, details=True):
        if len(self.server_snapshots) > 1:
            snapshot = self.server_snapshots.pop(0)
        else:
            snapshot = self.server_snapshots[0]
        self.current = {server.id: server for server in snapshot}
        return iter(snapshot)

    def delete_server(self, server_id, **kwargs):
        self.deleted.append((server_id, kwargs))

    def get_server(self, server_id):
        if server_id in self.current:
            return self.current[server_id]
        error = RuntimeError('not found')
        error.status_code = 404
        raise error


class FakeResponse:
    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


class ScaleRunnerTest(unittest.TestCase):
    def test_positive_float_rejects_non_finite_thresholds(self):
        for value in ('nan', 'inf', '-inf'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                        argparse.ArgumentTypeError,
                        'finite number greater than zero'):
                    scale.positive_float(value)

    def make_cleanup_run(self, compute, artifact, server_ids=None):
        run = scale.ScaleRun.__new__(scale.ScaleRun)
        run.args = types.SimpleNamespace(
            cleanup_timeout=2,
            cleanup_settle_time=0.001,
            delete_request_attempts=3,
            delete_retry_backoff=0.001,
            delete_concurrency=2,
            audit_concurrency=2,
            incus_host=[],
            incus_project='nova',
            poll_interval=0.001,
            query_chunk_size=100,
        )
        run.connection = types.SimpleNamespace(
            compute=compute,
            network=types.SimpleNamespace(ports=lambda **_query: []),
        )
        run.run_id = str(uuid.uuid4())
        run.cleanup_token = str(uuid.uuid4())
        run.project_id = str(uuid.uuid4())
        run.prefix = 'scale-test'
        run.artifact = Path(artifact)
        run.server_ids = list(server_ids or [])
        run.instance_names = {}
        run.create_latencies = {}
        run.checkpoints = []
        run.preflight_result = {}
        run.external_inventory_baseline = {
            'rbd_images': [],
            'ovn_lsps': [],
        }
        run.started_at = scale.utc_now()
        run.ended_at = None
        run.status = 'failed'
        run.failure = None
        run.cleanup_result = {
            'attempted': False,
            'completed': False,
        }
        run.resources = {}
        run.delete_attempts = {}
        run._state_lock = threading.Lock()
        run._stop_event = threading.Event()
        run.audit_cleanup_residuals = lambda _server_ids: {
            'neutron_ports': 0,
            'incus_instances': 0,
            'incus_profiles': 0,
        }
        return run

    @staticmethod
    def incus_instance(server_id, name, profiles=None):
        try:
            ordinal = int(uuid.UUID(server_id)) % 100000
        except ValueError:
            ordinal = sum(ord(character) for character in server_id)
        fixed_base = 100000 + ordinal * 65536
        idmap = scale.json.dumps([
            {
                'Isuid': True,
                'Isgid': True,
                'Hostid': fixed_base,
                'Nsid': 0,
                'Maprange': 65536,
            },
        ])
        return {
            'name': name,
            'type': 'container',
            'status': 'Running',
            'config': {
                'user.openstack.uuid': server_id,
                'volatile.idmap.current': idmap,
                'volatile.idmap.next': idmap,
            },
            'expanded_config': {
                'limits.cpu': '1',
                'limits.memory': '128MiB',
                'limits.processes': '64',
                'security.idmap.isolated': 'true',
                'security.idmap.base': str(fixed_base),
                'security.idmap.size': '65536',
                'security.privileged': 'false',
            },
            'expanded_devices': {
                'root': {
                    'type': 'disk',
                    'path': '/',
                    'pool': 'ceph-rootfs',
                    'size': '1GB',
                },
            },
            'profiles': profiles or [name],
        }

    @staticmethod
    def runtime_contract():
        return {
            'type': 'container',
            'status': 'Running',
            'root_pool': 'ceph-rootfs',
            'root_size': '1GB',
            'config': {
                'limits.cpu': '1',
                'limits.memory': '128MiB',
                'limits.processes': '64',
                'security.idmap.isolated': 'true',
                'security.privileged': 'false',
            },
            'placement_resources': {
                'VCPU': 1,
                'MEMORY_MB': 128,
                'DISK_GB': 1,
            },
        }

    @staticmethod
    def owned_server(run, server_id):
        return resource(
            server_id,
            metadata={
                scale.RUN_METADATA_KEY: run.run_id,
                scale.CLEANUP_METADATA_KEY: run.cleanup_token,
            })

    def test_parse_checkpoints_requires_increasing_unique_values(self):
        self.assertEqual([100, 500, 1000],
                         scale.parse_checkpoints('100,500,1000'))
        for value in ('', '0,100', '100,100', '500,100', 'one'):
            with self.subTest(value=value):
                self.assertRaises(
                    scale.argparse.ArgumentTypeError,
                    scale.parse_checkpoints,
                    value,
                )

    def test_artifact_reservation_is_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / 'scale.json'
            scale.reserve_artifact(artifact)
            with self.assertRaisesRegex(
                    scale.ScaleFailure, 'already exists'):
                scale.reserve_artifact(artifact)

    def test_incus_host_mapping_requires_explicit_nova_host(self):
        self.assertEqual(
            ('compute-1', 'root@10.0.0.11'),
            scale.incus_host_mapping('compute-1=root@10.0.0.11'),
        )
        for value in ('root@10.0.0.11', '=root@host', 'compute=', 'a=b c'):
            with self.subTest(value=value):
                self.assertRaises(
                    scale.argparse.ArgumentTypeError,
                    scale.incus_host_mapping,
                    value,
                )

    def test_quota_requirement_accepts_unlimited_and_rejects_shortfall(self):
        result = scale.quota_requirement(
            'instances', -1, 20, 1000, reserved=3)
        self.assertEqual(1023, result['required_total'])
        with self.assertRaisesRegex(scale.ScaleFailure, 'insufficient'):
            scale.quota_requirement(
                'instances', 1000, 20, 1000, reserved=3)

    def test_placement_capacity_applies_ratio_reserved_and_bottleneck(self):
        inventory = {
            'total': 64,
            'reserved': 4,
            'min_unit': 1,
            'max_unit': 64,
            'step_size': 1,
            'allocation_ratio': 4.0,
        }
        self.assertEqual(
            110,
            scale.placement_inventory_slots(inventory, 20, 2))
        providers = {
            'compute-1': {
                'inventories': {
                    'VCPU': inventory,
                    'MEMORY_MB': {
                        **inventory,
                        'total': 128000,
                        'reserved': 0,
                        'allocation_ratio': 1.0,
                        'max_unit': 128000,
                    },
                },
                'usages': {'VCPU': 20, 'MEMORY_MB': 0},
            },
            'compute-2': {
                'inventories': {
                    'VCPU': inventory,
                    'MEMORY_MB': {
                        **inventory,
                        'total': 128000,
                        'reserved': 0,
                        'allocation_ratio': 1.0,
                        'max_unit': 128000,
                    },
                },
                'usages': {'VCPU': 20, 'MEMORY_MB': 0},
            },
        }
        result = scale.placement_fleet_capacity(
            providers, {'VCPU': 2, 'MEMORY_MB': 2000})
        self.assertEqual(128, result['fleet_slots'])
        self.assertEqual(64, result['providers']['compute-1']['slots'])

    def test_placement_capacity_rejects_invalid_or_missing_inventory(self):
        inventory = {
            'total': 64,
            'reserved': 0,
            'min_unit': 2,
            'max_unit': 64,
            'step_size': 2,
            'allocation_ratio': 1.0,
        }
        self.assertEqual(
            0, scale.placement_inventory_slots(inventory, 0, 3))
        result = scale.placement_fleet_capacity(
            {
                'compute-1': {
                    'inventories': {'VCPU': inventory},
                    'usages': {},
                },
            },
            {'VCPU': 2, 'CUSTOM_ROOT': 1})
        self.assertEqual(0, result['fleet_slots'])

    def test_subnet_capacity_accounts_only_used_pool_addresses(self):
        subnet = resource(
            'subnet-1',
            allocation_pools=[
                {'start': '192.0.2.10', 'end': '192.0.2.19'},
            ])
        used = {
            scale.ipaddress.ip_address('192.0.2.10'),
            scale.ipaddress.ip_address('192.0.2.50'),
        }
        self.assertEqual(
            9, scale.subnet_available_addresses(subnet, used))

    def test_idmap_overlap_is_global_and_same_kind_only(self):
        ranges = {
            'compute-1': [
                {
                    'kind': 'uid', 'start': 100000, 'end': 165536,
                    'instance_uuid': 'a',
                },
                {
                    'kind': 'uid', 'start': 165536, 'end': 231072,
                    'instance_uuid': 'b',
                },
                {
                    'kind': 'gid', 'start': 120000, 'end': 180000,
                    'instance_uuid': 'c',
                },
            ],
            'compute-2': [
                {
                    'kind': 'uid', 'start': 100000, 'end': 165536,
                    'instance_uuid': 'd',
                },
            ],
        }
        overlaps = scale.idmap_overlap_errors(ranges)
        self.assertEqual(1, len(overlaps))
        self.assertEqual(
            {'compute-1', 'compute-2'},
            {overlaps[0]['first']['host'], overlaps[0]['second']['host']})
        ranges['compute-1'].append({
            'kind': 'uid', 'start': 165000, 'end': 200000,
            'instance_uuid': 'e',
        })
        self.assertEqual(4, len(scale.idmap_overlap_errors(ranges)))

    def test_parse_instance_idmap_rejects_malformed_ranges(self):
        valid = scale.json.dumps([
            {
                'Isuid': True,
                'Isgid': True,
                'Hostid': 100000,
                'Nsid': 0,
                'Maprange': 65536,
            },
        ])
        self.assertEqual(
            2,
            len(scale.parse_instance_idmap(
                {'volatile.idmap.current': valid},
                'volatile.idmap.current', 'instance-1')))
        invalid = scale.json.dumps([
            {
                'Isuid': True,
                'Hostid': -1,
                'Nsid': 0,
                'Maprange': 65536,
            },
        ])
        with self.assertRaisesRegex(scale.ScaleFailure, 'invalid idmap'):
            scale.parse_instance_idmap(
                {'volatile.idmap.current': invalid},
                'volatile.idmap.current', 'instance-1')

    def test_neutron_binding_requires_exact_nova_compute_host(self):
        server = resource(
            'server-1', compute_host='compute-1')
        good = resource(
            'port-1', status='ACTIVE', binding_host_id='compute-1')
        bad = resource(
            'port-1', status='ACTIVE', binding_host_id='compute-2')
        self.assertEqual(
            {},
            scale.neutron_binding_errors(
                {'server-1': server}, {'server-1': [good]}))
        self.assertIn(
            'server-1',
            scale.neutron_binding_errors(
                {'server-1': server}, {'server-1': [bad]}))

    def test_host_storage_projection_fails_before_final_exhaustion(self):
        baseline = {
            'total_bytes': 10 * scale.GIB,
            'available_bytes': 6 * scale.GIB,
            'total_inodes': 1000,
            'available_inodes': 800,
        }
        healthy = {
            **baseline,
            'available_bytes': 5.9 * scale.GIB,
            'available_inodes': 790,
        }
        result = scale.projected_host_storage(
            baseline, healthy, 100, 1000,
            2 * scale.GIB, 20)
        self.assertGreater(result['projected_available_bytes'], 2 * scale.GIB)
        exhausted = {
            **baseline,
            'available_bytes': 5 * scale.GIB,
            'available_inodes': 790,
        }
        with self.assertRaisesRegex(
                scale.ScaleFailure, 'final checkpoint'):
            scale.projected_host_storage(
                baseline, exhausted, 100, 1000,
                2 * scale.GIB, 20)

    def test_ceph_status_requires_health_capacity_and_pool_quota(self):
        status = {
            'fsid': str(uuid.uuid4()),
            'health': 'HEALTH_OK',
            'pool': 'incus-rootfs',
            'available_bytes': 200 * scale.GIB,
            'pool_stored_bytes': 10 * scale.GIB,
            'pool_max_bytes': 50 * scale.GIB,
            'raw_used_ratio': 0.2,
            'nearfull_ratio': 0.85,
            'full_ratio': 0.95,
        }
        result = scale.validate_ceph_status(status, 30 * scale.GIB)
        self.assertEqual(40 * scale.GIB,
                         result['pool_quota_available_bytes'])
        with self.assertRaisesRegex(scale.ScaleFailure, 'quota'):
            scale.validate_ceph_status(status, 50 * scale.GIB)
        with self.assertRaisesRegex(scale.ScaleFailure, 'production-ready'):
            scale.validate_ceph_status(
                {**status, 'health': 'HEALTH_WARN'}, scale.GIB)

    def test_performance_slo_result_reports_exact_violations(self):
        result = scale.performance_slo_result(
            {
                'create_to_active_p95': 12.0,
                'nova_list_p95': 0.5,
            },
            {
                'create_to_active_p95': 10.0,
                'nova_list_p95': 1.0,
            })

        self.assertFalse(result['passed'])
        self.assertEqual({
            'create_to_active_p95': {
                'observed': 12.0,
                'maximum': 10.0,
            },
        }, result['violations'])

    def test_performance_slo_result_allows_disabled_threshold(self):
        result = scale.performance_slo_result(
            {'create_api_p95': 100.0},
            {'create_api_p95': None})

        self.assertTrue(result['passed'])
        self.assertEqual({}, result['violations'])

    def test_minimum_slo_result_rejects_low_throughput(self):
        result = scale.minimum_slo_result(
            {'submit_per_second': 9.0},
            {'submit_per_second': 10.0})

        self.assertFalse(result['passed'])
        self.assertEqual({
            'submit_per_second': {
                'observed': 9.0,
                'minimum': 10.0,
            },
        }, result['violations'])

    def test_scale_flavor_requires_system_and_pool_traits(self):
        flavor = resource(
            str(uuid.uuid4()),
            extra_specs={
                'trait:CUSTOM_INCUS_SYSTEM_CONTAINER': 'required',
                'incus:root_storage_pool': 'local-nvme',
                'trait:CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME': 'required',
            })

        result = scale.validate_scale_flavor(flavor)

        self.assertEqual(
            [
                'CUSTOM_INCUS_SYSTEM_CONTAINER',
                'CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME',
            ],
            result['required_traits'])
        flavor.extra_specs.pop(
            'trait:CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME')
        with self.assertRaisesRegex(
                scale.ScaleFailure, 'LOCAL_NVME'):
            scale.validate_scale_flavor(flavor)
        flavor.extra_specs = {}
        with self.assertRaisesRegex(
                scale.ScaleFailure, 'SYSTEM_CONTAINER'):
            scale.validate_scale_flavor(flavor)

    def test_host_distribution_rejects_outside_and_skew(self):
        outside = scale.host_distribution_result(
            {'incus-1': 5, 'libvirt-1': 5},
            {'incus-1', 'incus-2'},
            25.0)
        self.assertFalse(outside['passed'])
        self.assertEqual(['libvirt-1'], outside['outside_hosts'])
        self.assertEqual(['incus-2'], outside['missing_hosts'])

        skew = scale.host_distribution_result(
            {'incus-1': 80, 'incus-2': 20},
            {'incus-1', 'incus-2'},
            25.0)
        self.assertFalse(skew['passed'])
        self.assertEqual(120.0, skew['observed_skew_percent'])

    def test_per_compute_checkpoints_expand_and_enforce_each_host(self):
        totals, targets = scale.resolve_checkpoints(
            None, [100, 500, 1000], 3)

        self.assertEqual([300, 1500, 3000], totals)
        self.assertEqual({300: 100, 1500: 500, 3000: 1000}, targets)
        result = scale.per_compute_distribution_result(
            {'compute-1': 95, 'compute-2': 100, 'compute-3': 105},
            {'compute-1', 'compute-2', 'compute-3'},
            100, 90.0, 20.0)
        self.assertTrue(result['passed'])
        self.assertEqual(90, result['minimum_per_compute'])
        result = scale.per_compute_distribution_result(
            {'compute-1': 89, 'compute-2': 100, 'compute-3': 111},
            {'compute-1', 'compute-2', 'compute-3'},
            100, 90.0, 25.0)
        self.assertFalse(result['passed'])
        self.assertEqual({'compute-1': 89}, result['below_minimum'])

    def test_parse_args_expands_explicit_per_compute_targets(self):
        args = scale.parse_args([
            '--image', 'image',
            '--flavor', 'flavor',
            '--network', 'network',
            '--per-compute-checkpoints', '100,500,1000',
            '--incus-host', 'compute-1=ssh-1',
            '--incus-host', 'compute-2=ssh-2',
            '--incus-host', 'compute-3=ssh-3',
            '--expected-root-pool', 'ceph-rootfs',
            '--rbd-inventory-command', '/bin/rbd-inventory',
            '--ovn-lsp-inventory-command', '/bin/ovn-inventory',
            '--ceph-status-command', '/bin/ceph-status',
            '--idmap-inventory-command', '/bin/idmap-inventory',
        ])

        self.assertEqual([300, 1500, 3000], args.checkpoints)
        self.assertEqual(3, args.min_compute_hosts)
        self.assertEqual(500, args.per_compute_target_by_total[1500])

    def test_idmap_inventory_is_schema_opaque_but_uuid_exact(self):
        instance_id = str(uuid.uuid4())
        baseline = scale.normalize_idmap_inventory({
            'revision': 10,
            'entries': [],
        })
        current = scale.normalize_idmap_inventory({
            'revision': 20,
            'entries': [
                {
                    'key': '/openstack-incus/idmaps/v2/prod/instances/' +
                           instance_id,
                    'value': {'allocation_id': str(uuid.uuid4())},
                },
                {
                    'key': '/openstack-incus/idmaps/v2/prod/slots/7',
                    'value': {
                        'instance_uuid': instance_id,
                        'future_field': {'materialization_state': 'possible'},
                    },
                },
            ],
        })

        result = scale.idmap_inventory_delta(
            baseline, current, {instance_id})

        self.assertEqual(2, result['run_key_count'])
        self.assertEqual(
            2, len(result['run_keys_by_instance'][instance_id]))
        with self.assertRaisesRegex(scale.ScaleFailure, 'revision'):
            scale.normalize_idmap_inventory({'entries': []})
        with self.assertRaisesRegex(scale.ScaleFailure, 'duplicate key'):
            scale.normalize_idmap_inventory({
                'revision': 21,
                'entries': [
                    {'key': '/same', 'value': {}},
                    {'key': '/same', 'value': {}},
                ],
            })

    def test_idmap_query_failure_is_not_an_empty_inventory(self):
        run = scale.ScaleRun.__new__(scale.ScaleRun)
        command = '/bin/idmap-inventory'
        run.args = types.SimpleNamespace(
            idmap_inventory_command=command)
        run.inventory_command_fingerprints = {
            'idmap_keys': scale.ScaleRun._command_fingerprint(command),
        }

        def fail(*_args, **_kwargs):
            raise scale.ScaleFailure('etcd query timed out')

        run._run_json_object_command = fail
        with self.assertRaisesRegex(scale.ScaleFailure, 'timed out'):
            run._idmap_inventory()

    def test_soak_window_and_telemetry_summary(self):
        contract = scale.validate_soak_window(900, 60, 10)
        self.assertTrue(contract['enabled'])
        self.assertEqual(15, contract['covered_periodic_cycles'])
        with self.assertRaisesRegex(scale.ScaleFailure, 'required'):
            scale.validate_soak_window(120, 60, 3)
        samples = [
            {
                'collected_at': 'start',
                'hosts': {
                    'compute-1': {
                        'host_cpu_ticks': {'total': 1000, 'idle': 600},
                        'memory_available_bytes': 1000,
                        'processes': {
                            'incusd': {
                                'cpu_seconds': 10,
                                'rss_bytes': 100,
                                'fd_count': 10,
                                'process_count': 1,
                            },
                        },
                    },
                },
            },
            {
                'collected_at': 'end',
                'hosts': {
                    'compute-1': {
                        'host_cpu_ticks': {'total': 1200, 'idle': 700},
                        'memory_available_bytes': 900,
                        'processes': {
                            'incusd': {
                                'cpu_seconds': 14,
                                'rss_bytes': 120,
                                'fd_count': 12,
                                'process_count': 1,
                            },
                        },
                    },
                },
            },
        ]

        summary = scale.summarize_telemetry_samples(samples)

        self.assertEqual(2, summary['sample_count'])
        self.assertEqual(
            4.0,
            summary['hosts']['compute-1']['processes']['incusd'][
                'cpu_seconds_delta'])
        self.assertEqual(
            50.0, summary['hosts']['compute-1'][
                'host_cpu_busy_percent'])

    def test_artifact_wal_replays_create_and_rejects_partial_record(self):
        server_id = str(uuid.uuid4())
        state = {
            'schema_version': scale.ARTIFACT_SCHEMA_VERSION,
            'run_id': str(uuid.uuid4()),
            'cleanup_token': str(uuid.uuid4()),
            'project_id': str(uuid.uuid4()),
            'server_ids': [],
        }
        record = {
            'type': 'server-created',
            'server_id': server_id,
            'instance_name': 'instance-00000001',
            'submitted_epoch': 10.0,
            'accepted_epoch': 11.0,
            'create_latency': 1.0,
        }
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / 'scale.json'
            scale.atomic_json_write(artifact, state)
            scale.append_json_record(scale.artifact_wal_path(artifact), record)

            _path, loaded = scale.load_cleanup_artifact(artifact)

            self.assertEqual([server_id], loaded['server_ids'])
            self.assertEqual(
                11.0, loaded['accepted_epoch'][server_id])
            scale.artifact_wal_path(artifact).write_text(
                '{"type":"server-created"}', encoding='utf-8')
            _path, loaded = scale.load_cleanup_artifact(artifact)
            self.assertTrue(loaded['wal_torn_tail_ignored'])

    def test_create_appends_wal_without_rewriting_artifact(self):
        server_id = str(uuid.uuid4())
        run = scale.ScaleRun.__new__(scale.ScaleRun)
        run.args = types.SimpleNamespace()
        run.connection = types.SimpleNamespace(
            compute=types.SimpleNamespace(
                create_server=lambda **_kwargs: resource(server_id)))
        run.image = resource(str(uuid.uuid4()))
        run.flavor = resource(str(uuid.uuid4()))
        run.network = resource(str(uuid.uuid4()))
        run.prefix = 'scale'
        run.run_id = str(uuid.uuid4())
        run.cleanup_token = str(uuid.uuid4())
        run.server_ids = []
        run.instance_names = {}
        run.create_latencies = {}
        run.submitted_epoch = {}
        run.accepted_epoch = {}
        run._state_lock = threading.Lock()
        run._stop_event = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            run.artifact = Path(directory) / 'scale.json'
            scale.reserve_artifact(run.artifact)
            original_atomic_write = scale.atomic_json_write

            def unexpected_write(*_args, **_kwargs):
                raise AssertionError('create path rewrote the full artifact')

            scale.atomic_json_write = unexpected_write
            try:
                self.assertEqual(server_id, run.create_one(1))
            finally:
                scale.atomic_json_write = original_atomic_write

            wal = scale.artifact_wal_path(run.artifact)
            self.assertTrue(wal.exists())
            self.assertEqual(1, len(wal.read_text().splitlines()))

    def test_release_gate_wires_fail_closed_scale_evidence(self):
        gate = (
            SCRIPT.parents[1] /
            'tools' / 'openstack-incus-release-gate.sh'
        ).read_text(encoding='utf-8')
        for variable, option in (
                ('SCALE_EXPECTED_ROOT_POOL', '--expected-root-pool'),
                ('SCALE_EXPECTED_PROCESS_LIMIT',
                 '--expected-process-limit'),
                ('SCALE_RBD_INVENTORY_COMMAND',
                 '--rbd-inventory-command'),
                ('SCALE_OVN_LSP_INVENTORY_COMMAND',
                 '--ovn-lsp-inventory-command'),
                ('SCALE_CEPH_STATUS_COMMAND',
                 '--ceph-status-command'),
                ('SCALE_IDMAP_INVENTORY_COMMAND',
                 '--idmap-inventory-command'),
                ('SCALE_MAX_HOST_SKEW_PERCENT',
                 '--max-host-skew-percent'),
                ('SCALE_MIN_SUBMIT_THROUGHPUT',
                 '--min-submit-throughput'),
                ('SCALE_MIN_ACTIVE_THROUGHPUT',
                 '--min-active-throughput'),
                ('SCALE_HOST_INITIAL_MIN_FREE_BYTES',
                 '--host-initial-min-free-bytes'),
                ('SCALE_HOST_INITIAL_MIN_FREE_PERCENT',
                 '--host-initial-min-free-percent'),
                ('SCALE_HOST_INITIAL_MIN_INODE_PERCENT',
                 '--host-initial-min-inode-percent'),
                ('SCALE_HOST_RUNTIME_MIN_FREE_BYTES',
                 '--host-runtime-min-free-bytes'),
                ('SCALE_HOST_RUNTIME_MIN_FREE_PERCENT',
                 '--host-runtime-min-free-percent'),
                ('SCALE_HOST_RUNTIME_MIN_INODE_PERCENT',
                 '--host-runtime-min-inode-percent')):
            with self.subTest(variable=variable):
                self.assertIn(': "${' + variable + ':?', gate)
                self.assertIn(option + ' "$' + variable + '"', gate)
        self.assertIn('scale evidence summary', gate)
        self.assertIn('scale artifact WAL was not compacted', gate)
        self.assertIn(
            'SCALE_PER_COMPUTE_CHECKPOINTS=${'
            'SCALE_PER_COMPUTE_CHECKPOINTS:-100,500,1000}', gate)
        self.assertIn(
            '--per-compute-checkpoints '
            '"$SCALE_PER_COMPUTE_CHECKPOINTS"', gate)
        self.assertNotIn('--checkpoints "$SCALE_CHECKPOINTS"', gate)
        self.assertIn(
            'SCALE_IDLE_SOAK_SECONDS=${SCALE_IDLE_SOAK_SECONDS:-900}',
            gate)
        self.assertIn(
            '--idle-soak-seconds "$SCALE_IDLE_SOAK_SECONDS"', gate)
        self.assertIn(
            'SCALE_IDLE_SOAK_SECONDS must be an integer of at least 900',
            gate)
        self.assertIn('evidence["schema_version"] == 4', gate)
        self.assertIn(
            'checkpoint["incus"]["idmap_ranges"] >= target * 2',
            gate)
        self.assertIn(
            'checkpoint["incus"]["profiles"] == target',
            gate)
        self.assertIn(
            'residual["rbd_images"]["residual_count"] == 0',
            gate)
        self.assertIn(
            'evidence["cleanup"]["performance_slo"]["passed"] is True',
            gate)
        self.assertIn(
            'validate_idmap_delta(', gate)
        self.assertIn(
            'checkpoint["idmap_etcd"]', gate)
        self.assertIn(
            'cleanup_idmap.get("run_key_count") == 0', gate)
        self.assertIn(
            'soak.get("attempted") is True and '
            'soak.get("completed") is True', gate)
        self.assertIn(
            'validate_telemetry(preflight["telemetry"]', gate)
        self.assertIn(
            'summary_telemetry.get("sample_count") == len(samples)', gate)
        self.assertIn(
            'checkpoint["control_plane_query_slo"]["passed"] is True',
            gate)
        self.assertIn(
            'evidence["cleanup"].get("business_cleanup_seconds")', gate)
        self.assertNotIn('assert evidence[', gate)
        self.assertNotIn('assert checkpoint[', gate)

    def test_release_gate_accepts_complete_schema_four_evidence(self):
        gate = (
            SCRIPT.parents[1] /
            'tools' / 'openstack-incus-release-gate.sh'
        ).read_text(encoding='utf-8')
        block = gate[gate.index('run "scale evidence summary"'):]
        start = block.index("<<'PY'\n") + len("<<'PY'\n")
        summary_program = block[start:block.index('\nPY\n', start)]

        host = 'compute-1'
        server_ids = [str(uuid.UUID(int=index + 1)) for index in range(1000)]

        def telemetry(sequence):
            return {
                'captured_at': scale.utc_now(),
                'sequence': sequence,
                'hosts': {
                    host: {
                        'memory_total_bytes': 1024,
                        'memory_available_bytes': 512,
                        'host_cpu_ticks': {
                            'total': 100 + sequence,
                            'idle': 50 + sequence,
                        },
                        'processes': {
                            name: {
                                'process_count': 1,
                                'cpu_seconds': float(sequence),
                                'rss_bytes': 128,
                                'fd_count': 8,
                            }
                            for name in ('incusd', 'nova_compute')
                        },
                    },
                },
            }

        def idmap_evidence(ids, revision):
            keys_by_instance = {
                server_id: ['/openstack-incus/idmaps/' + server_id]
                for server_id in ids
            }
            keys = sorted(
                key for values in keys_by_instance.values() for key in values)
            return {
                'baseline_revision': '1',
                'current_revision': str(revision),
                'baseline_count': 0,
                'current_count': len(keys),
                'added_keys': keys,
                'removed_keys': [],
                'changed_baseline_keys': [],
                'run_key_count': len(keys),
                'run_keys': keys,
                'run_key_digests': {key: 'a' * 64 for key in keys},
                'run_keys_by_instance': keys_by_instance,
                'baseline_run_keys_by_instance': {
                    server_id: [] for server_id in ids
                },
            }

        baseline_ceph = {
            'health': 'HEALTH_OK',
            'required_bytes': 1,
            'fsid': 'test-fsid',
            'pool': 'incus-rootfs',
        }
        checkpoints = []
        for revision, target in enumerate((100, 500, 1000), start=2):
            ids = server_ids[:target]
            checkpoints.append({
                'target': target,
                'target_per_compute': target,
                'performance_slo': {'passed': True},
                'control_plane_query_slo': {'passed': True},
                'throughput': {'slo': {'passed': True}},
                'host_distribution': {
                    'passed': True,
                    'target_per_compute': target,
                    'minimum_per_compute': int(target * 0.9),
                    'below_minimum': {},
                    'counts': {host: target},
                },
                'incus': {
                    'instance_owners': target,
                    'profiles': target,
                    'idmap_ranges': target * 2,
                },
                'placement': {'consumer_count': target},
                'audit_seconds': {'total': 1.0, 'incus': 0.5},
                'idmap_etcd': idmap_evidence(ids, revision),
                'telemetry': telemetry(revision),
                'host_storage': {
                    host: {
                        path: {
                            'total_bytes': 1024,
                            'available_bytes': 512,
                            'total_inodes': 100,
                            'available_inodes': 50,
                        }
                        for path in ('/var/lib/incus', '/var/log/incus')
                    },
                },
                'ceph': dict(baseline_ceph),
                'external_inventory': {
                    'expected_ovn_lsp_count': target,
                    'expected_rbd_images': [
                        'container-' + server_id for server_id in ids
                    ],
                },
            })

        final_idmap = idmap_evidence(server_ids, 5)
        process_summary = {
            name: {
                'cpu_seconds_delta': 1.0,
                'peak_rss_bytes': 128,
                'peak_fd_count': 8,
                'peak_process_count': 1,
            }
            for name in ('incusd', 'nova_compute')
        }
        cleanup_idmap = {
            **idmap_evidence(server_ids, 6),
            'current_count': 0,
            'added_keys': [],
            'run_key_count': 0,
            'run_keys': [],
            'run_key_digests': {},
            'run_keys_by_instance': {
                server_id: [] for server_id in server_ids
            },
            'unchanged_known_run_keys': [],
        }
        evidence = {
            'schema_version': 4,
            'run_id': str(uuid.uuid4()),
            'project_id': str(uuid.uuid4()),
            'status': 'passed',
            'failure': None,
            'server_ids': server_ids,
            'instance_names': {
                server_id: 'instance-' + str(index)
                for index, server_id in enumerate(server_ids)
            },
            'preflight': {
                'ceph': baseline_ceph,
                'fleet': {'incus_compute_hosts': [host]},
                'idmap_inventory_baseline': {
                    'revision': '1',
                    'key_count': 0,
                },
                'telemetry': telemetry(1),
            },
            'idmap_inventory_baseline': {
                'revision': '1',
                'entries': {},
            },
            'inventory_command_fingerprints': {'idmap_keys': 'a' * 64},
            'checkpoints': checkpoints,
            'idle_soak': {
                'attempted': True,
                'completed': True,
                'configured_seconds': 900,
                'actual_seconds': 900.1,
                'minimum_periodic_cycles': 10,
                'covered_periodic_cycles': 15,
                'server_count': 1000,
                'host_distribution': {
                    'passed': True,
                    'target_per_compute': 1000,
                },
                'telemetry_samples': [telemetry(4), telemetry(5)],
                'telemetry_summary': {
                    'sample_count': 2,
                    'hosts': {
                        host: {
                            'host_cpu_busy_percent': 50.0,
                            'minimum_available_memory_bytes': 512,
                            'processes': process_summary,
                        },
                    },
                },
                'backend_audit': {
                    'idmap_etcd': final_idmap,
                    'incus': {'instance_owners': 1000},
                    'placement': {'consumer_count': 1000},
                },
                'audit_seconds': {'total': 1.0, 'incus': 0.5},
            },
            'cleanup': {
                'completed': True,
                'server_count': 1000,
                'cleanup_seconds': 20.0,
                'business_cleanup_seconds': 15.0,
                'audit_seconds': 5.0,
                'performance_slo': {'passed': True},
                'residual_audit': {
                    'neutron_ports': 0,
                    'incus_instances': 0,
                    'incus_profiles': 0,
                    'placement_consumers': 0,
                    'rbd_images': {'residual_count': 0},
                    'ovn_lsps': {'residual_count': 0},
                    'idmap_etcd': cleanup_idmap,
                },
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / 'scale.json'
            artifact.write_text(json.dumps(evidence), encoding='utf-8')
            result = subprocess.run(
                [
                    sys.executable, '-', str(artifact), '100,500,1000',
                    '1', '90', '900', '10',
                ],
                input=summary_program,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual([100, 500, 1000], summary['targets'])
        self.assertEqual(900.1, summary['idle_soak_seconds'])

    def test_fleet_preflight_allows_mapped_incus_subset(self):
        services = [
            resource(
                str(uuid.uuid4()),
                host='compute-1',
                status='enabled',
                state='up',
            ),
            resource(
                str(uuid.uuid4()),
                host='compute-2',
                status='enabled',
                state='up',
            ),
            resource(
                str(uuid.uuid4()),
                host='compute-disabled',
                status='disabled',
                state='down',
            ),
        ]
        run = scale.ScaleRun.__new__(scale.ScaleRun)
        run.args = types.SimpleNamespace(
            incus_host=[
                ('compute-1', 'ssh-1'),
                ('compute-2', 'ssh-2'),
            ],
            min_compute_hosts=2,
        )
        run.connection = types.SimpleNamespace(
            compute=types.SimpleNamespace(
                services=lambda **_query: services))
        run.flavor_contract = {
            'required_traits': [scale.INCUS_SYSTEM_CONTAINER_TRAIT],
        }
        run._placement_provider_inventory = lambda hosts: {
            host: {
                'uuid': str(uuid.uuid4()),
                'traits': [scale.INCUS_SYSTEM_CONTAINER_TRAIT],
                'usages': {},
            }
            for host in hosts
        }

        result = run._fleet_preflight(initial=False)

        self.assertEqual(2, result['count'])
        self.assertEqual([], result['other_enabled_compute_hosts'])
        run.args.incus_host = [('compute-1', 'ssh-1')]
        run.args.min_compute_hosts = 1
        result = run._fleet_preflight(initial=False)
        self.assertEqual(['compute-2'], result['other_enabled_compute_hosts'])
        run.args.min_compute_hosts = 2
        with self.assertRaisesRegex(
                scale.ScaleFailure, 'must equal'):
            run._fleet_preflight(initial=False)

    def test_create_until_stops_submitting_after_first_bounded_batch(self):
        run = scale.ScaleRun.__new__(scale.ScaleRun)
        run.args = types.SimpleNamespace(concurrency=3)
        run.server_ids = []
        run._stop_event = threading.Event()
        submitted = []

        def create_one(ordinal):
            submitted.append(ordinal)
            if ordinal == 1:
                raise RuntimeError('create failed')
            return str(uuid.uuid4())

        run.create_one = create_one
        with self.assertRaisesRegex(scale.ScaleFailure, 'ordinal 1'):
            run.create_until(1000)
        self.assertTrue(submitted)
        self.assertLessEqual(max(submitted), 3)

    def test_cleanup_deletes_only_exact_metadata_pair_and_recovers_id(self):
        recorded_id = str(uuid.uuid4())
        recovered_id = str(uuid.uuid4())
        unrelated_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            compute = FakeCompute([])
            run = self.make_cleanup_run(
                compute,
                Path(directory) / 'scale.json',
                [recorded_id],
            )
            unrelated = resource(
                unrelated_id,
                metadata={
                    scale.RUN_METADATA_KEY: run.run_id,
                    scale.CLEANUP_METADATA_KEY: str(uuid.uuid4()),
                })
            compute.server_snapshots = [
                [
                    self.owned_server(run, recorded_id),
                    self.owned_server(run, recovered_id),
                    unrelated,
                ],
                [unrelated],
                [unrelated],
            ]

            run.cleanup()

        deleted = {item[0] for item in compute.deleted}
        self.assertEqual({recorded_id, recovered_id}, deleted)
        self.assertIn(recovered_id, run.server_ids)
        self.assertTrue(run.cleanup_result['completed'])
        for _server_id, kwargs in compute.deleted:
            self.assertEqual({'ignore_missing': True}, kwargs)

    def test_cleanup_discovers_server_that_appears_after_initial_scan(self):
        late_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            compute = FakeCompute([])
            run = self.make_cleanup_run(
                compute,
                Path(directory) / 'scale.json',
            )
            compute.server_snapshots = [
                [],
                [],
                [self.owned_server(run, late_id)],
                [],
                [],
            ]

            run.cleanup()

        self.assertEqual(
            {late_id}, {item[0] for item in compute.deleted})
        self.assertGreaterEqual(len(compute.deleted), 1)
        self.assertTrue(run.cleanup_result['completed'])

    def test_cleanup_retries_transient_backend_residual(self):
        with tempfile.TemporaryDirectory() as directory:
            compute = FakeCompute([[], [], [], []])
            run = self.make_cleanup_run(
                compute,
                Path(directory) / 'scale.json',
            )
            calls = {'count': 0}

            def residual(_server_ids):
                calls['count'] += 1
                if calls['count'] == 1:
                    raise scale.ScaleFailure('OVN cleanup is pending')
                return {'ovn_lsps': {'residual_count': 0}}

            run.audit_cleanup_residuals = residual

            run.cleanup()

        self.assertEqual(2, calls['count'])
        self.assertTrue(run.cleanup_result['completed'])

    def test_cleanup_refuses_recorded_id_with_mismatched_metadata(self):
        recorded_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            compute = FakeCompute([])
            run = self.make_cleanup_run(
                compute,
                Path(directory) / 'scale.json',
                [recorded_id],
            )
            compute.server_snapshots = [[resource(
                recorded_id,
                metadata={
                    scale.RUN_METADATA_KEY: run.run_id,
                    scale.CLEANUP_METADATA_KEY: str(uuid.uuid4()),
                })]]

            with self.assertRaisesRegex(
                    scale.ScaleFailure, 'refusing to delete'):
                run.cleanup()

        self.assertEqual([], compute.deleted)

    def test_cleanup_artifact_restores_incus_inventory_mapping(self):
        project_id = str(uuid.uuid4())
        state = {
            'schema_version': scale.ARTIFACT_SCHEMA_VERSION,
            'run_id': str(uuid.uuid4()),
            'cleanup_token': str(uuid.uuid4()),
            'project_id': project_id,
            'server_ids': [str(uuid.uuid4())],
            'incus_hosts': [['compute-1', 'root@10.0.0.11']],
            'incus_project': 'nova-custom',
        }
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / 'scale.json'
            scale.atomic_json_write(artifact, state)
            args = types.SimpleNamespace(
                cleanup_artifact=str(artifact),
                cloud=None,
                incus_host=[],
                incus_project=None,
            )
            connection = types.SimpleNamespace(
                current_project_id=project_id)

            run = scale.ScaleRun.from_cleanup_artifact(
                args, connection=connection)

        self.assertEqual(
            [('compute-1', 'root@10.0.0.11')],
            run.args.incus_host,
        )
        self.assertEqual('nova-custom', run.args.incus_project)

    def test_exact_absence_distinguishes_present_not_found_and_error(self):
        present_id = str(uuid.uuid4())
        absent_id = str(uuid.uuid4())
        failed_id = str(uuid.uuid4())

        def get_server(server_id):
            if server_id == present_id:
                return resource(
                    server_id,
                    metadata={
                        scale.RUN_METADATA_KEY: run.run_id,
                        scale.CLEANUP_METADATA_KEY: run.cleanup_token,
                    })
            error = RuntimeError('lookup failed')
            error.status_code = 404 if server_id == absent_id else 503
            raise error

        run = scale.ScaleRun.__new__(scale.ScaleRun)
        run.args = types.SimpleNamespace(delete_concurrency=2)
        run.run_id = str(uuid.uuid4())
        run.cleanup_token = str(uuid.uuid4())
        run.connection = types.SimpleNamespace(
            compute=types.SimpleNamespace(get_server=get_server))

        present, failures = run._verify_exact_absence(
            [present_id, absent_id, failed_id])

        self.assertEqual([present_id], present)
        self.assertEqual(failed_id, failures[0][0])
        self.assertIn('lookup failed', failures[0][1])

    def test_neutron_port_queries_are_chunked(self):
        server_ids = [str(uuid.uuid4()) for _item in range(5)]
        calls = []

        def ports(device_id):
            calls.append(device_id)
            return [
                resource(
                    str(uuid.uuid4()),
                    device_id=server_id,
                    status='ACTIVE',
                    binding_host_id='compute-1',
                )
                for server_id in device_id
            ]

        run = scale.ScaleRun.__new__(scale.ScaleRun)
        run.args = types.SimpleNamespace(query_chunk_size=2)
        run.connection = types.SimpleNamespace(
            network=types.SimpleNamespace(ports=ports))

        result, _latency = run.list_run_ports(server_ids)

        self.assertEqual(5, len(result))
        self.assertEqual([2, 2, 1], [len(call) for call in calls])

    def test_delete_retries_transient_failure_with_ownership_check(self):
        server_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        cleanup_token = str(uuid.uuid4())
        server = resource(
            server_id,
            metadata={
                scale.RUN_METADATA_KEY: run_id,
                scale.CLEANUP_METADATA_KEY: cleanup_token,
            })
        calls = {'get': 0, 'delete': 0}

        def get_server(_server_id):
            calls['get'] += 1
            return server

        def delete_server(_server_id, **_kwargs):
            calls['delete'] += 1
            if calls['delete'] < 3:
                error = RuntimeError('conflict')
                error.status_code = 409
                raise error

        run = scale.ScaleRun.__new__(scale.ScaleRun)
        run.args = types.SimpleNamespace(
            delete_request_attempts=3,
            delete_retry_backoff=0.001,
        )
        run.connection = types.SimpleNamespace(
            compute=types.SimpleNamespace(
                get_server=get_server,
                delete_server=delete_server,
            ))
        run.run_id = run_id
        run.cleanup_token = cleanup_token
        run.delete_attempts = {}
        run.delete_latencies = []
        run._state_lock = threading.Lock()

        self.assertIsNone(run.delete_one(server_id))
        self.assertEqual(3, calls['get'])
        self.assertEqual(3, calls['delete'])

    def test_delete_400_is_transient_only_for_busy_operation(self):
        busy = RuntimeError('Instance is busy running a delete operation')
        busy.status_code = 400
        invalid = RuntimeError('Invalid server UUID')
        invalid.status_code = 400

        self.assertTrue(scale.is_transient_delete_error(busy))
        self.assertFalse(scale.is_transient_delete_error(invalid))

    def test_placement_audit_checks_consumer_and_provider_delta(self):
        server_id = str(uuid.uuid4())
        provider_id = str(uuid.uuid4())

        def placement_get(path, params=None, headers=None):
            if path == '/allocations/{}'.format(server_id):
                return FakeResponse({
                    'allocations': {
                        provider_id: {
                            'resources': {
                                'VCPU': 1,
                                'MEMORY_MB': 128,
                                'DISK_GB': 1,
                            },
                        },
                    },
                })
            if path == '/resource_providers/{}/usages'.format(provider_id):
                return FakeResponse({
                    'usages': {
                        'VCPU': 11,
                        'MEMORY_MB': 1152,
                        'DISK_GB': 101,
                    },
                })
            raise AssertionError((path, params))

        run = scale.ScaleRun.__new__(scale.ScaleRun)
        run.args = types.SimpleNamespace(audit_concurrency=2)
        run.connection = types.SimpleNamespace(
            placement=types.SimpleNamespace(get=placement_get))
        run.preflight_result = {
            'runtime_contract': self.runtime_contract(),
            'fleet': {
                'placement_providers': {
                    'compute-1': {
                        'uuid': provider_id,
                        'usages': {
                            'VCPU': 10,
                            'MEMORY_MB': 1024,
                            'DISK_GB': 100,
                        },
                    },
                },
            },
        }

        result = run.audit_placement({server_id})

        self.assertEqual(1, result['consumer_count'])
        self.assertEqual(1, result['provider_usage_delta']['VCPU'])

    def test_external_inventory_requires_run_lsps_and_rbd_delta(self):
        server_id = str(uuid.uuid4())
        port_id = str(uuid.uuid4())
        run = scale.ScaleRun.__new__(scale.ScaleRun)
        run.external_inventory_baseline = {
            'rbd_images': ['base-image'],
            'ovn_lsps': ['existing-port'],
        }
        run.args = types.SimpleNamespace(incus_project='nova')
        run.instance_names = {
            server_id: 'instance-00000001',
        }
        run._external_inventory = lambda: {
            'rbd_images': [
                'base-image',
                'container_nova_instance-00000001',
            ],
            'ovn_lsps': ['existing-port', port_id],
        }
        ports = {
            server_id: [resource(port_id)],
        }

        result = run.audit_external_inventory(ports)

        self.assertEqual(
            ['container_nova_instance-00000001'],
            result['rbd_images']['added'])
        run._external_inventory = lambda: {
            'rbd_images': [
                'base-image',
                'container_nova_instance-00000001',
            ],
            'ovn_lsps': ['existing-port'],
        }
        with self.assertRaisesRegex(scale.ScaleFailure, 'missing'):
            run.audit_external_inventory(ports)

    def test_external_inventory_rejects_changed_helper(self):
        run = scale.ScaleRun.__new__(scale.ScaleRun)
        run.args = types.SimpleNamespace(
            rbd_inventory_command='/bin/rbd-helper',
            ovn_lsp_inventory_command='/bin/ovn-helper',
            ceph_status_command='/bin/ceph-helper',
        )
        run.inventory_command_fingerprints = {
            'rbd_images': 'not-the-current-hash',
            'ovn_lsps': 'not-the-current-hash',
            'ceph_status': 'not-the-current-hash',
        }

        with self.assertRaisesRegex(
                scale.ScaleFailure, 'do not match'):
            run._external_inventory()

    def test_cleanup_residual_audit_finds_profile_by_instance_name(self):
        server_id = str(uuid.uuid4())
        run = scale.ScaleRun.__new__(scale.ScaleRun)
        run.args = types.SimpleNamespace(
            incus_host=[('compute-1', 'compute-ssh')],
            incus_project='nova',
            query_chunk_size=100,
        )
        run.connection = types.SimpleNamespace(
            network=types.SimpleNamespace(ports=lambda **_query: []))
        run.instance_names = {server_id: 'orphan-profile'}
        run.artifact = Path(tempfile.gettempdir()) / 'unused-scale.json'
        run._state_lock = threading.Lock()

        def remote_json(_host, command):
            if 'profile list' in command:
                return [{
                    'name': 'orphan-profile',
                    'config': {},
                }]
            return []

        run.remote_json = remote_json

        with self.assertRaisesRegex(scale.ScaleFailure, 'profiles='):
            run.audit_cleanup_residuals([server_id])

    def test_incus_audit_requires_profile_on_instance_owner(self):
        server_id = str(uuid.uuid4())
        instance_name = 'instance-00000001'
        server = resource(
            server_id,
            instance_name=instance_name,
            compute_host='compute-1',
            metadata={},
        )
        run = scale.ScaleRun.__new__(scale.ScaleRun)
        run.args = types.SimpleNamespace(
            incus_host=[('compute-1', 'compute-ssh')],
            incus_project="nova's",
        )
        commands = []

        def remote_json(host, command):
            commands.append((host, command))
            if 'profile list' in command:
                return [{
                    'name': instance_name,
                    'config': {},
                }]
            return [self.incus_instance(server_id, instance_name)]

        run.remote_json = remote_json
        run.preflight_result = {
            'runtime_contract': self.runtime_contract(),
        }

        result = run.audit_incus({server_id: server})

        self.assertEqual(1, result['instance_owners'])
        self.assertNotIn('--recursion', commands[0][1])
        self.assertIn('--columns=n', commands[0][1])
        self.assertIn("'nova'\"'\"'s'", commands[0][1])

    def test_incus_audit_rejects_owner_on_wrong_nova_host(self):
        server_id = str(uuid.uuid4())
        instance_name = 'instance-00000002'
        server = resource(
            server_id,
            instance_name=instance_name,
            compute_host='compute-1',
            metadata={},
        )
        run = scale.ScaleRun.__new__(scale.ScaleRun)
        run.args = types.SimpleNamespace(
            incus_host=[('compute-2', 'compute-2-ssh')],
            incus_project='nova',
        )

        def remote_json(_host, command):
            if 'profile list' in command:
                return [{
                    'name': instance_name,
                    'config': {},
                }]
            return [self.incus_instance(server_id, instance_name)]

        run.remote_json = remote_json
        run.preflight_result = {
            'runtime_contract': self.runtime_contract(),
        }

        with self.assertRaisesRegex(scale.ScaleFailure, 'wrong_hosts'):
            run.audit_incus({server_id: server})

    def test_incus_audit_rejects_duplicate_profile_on_second_host(self):
        server_id = str(uuid.uuid4())
        instance_name = 'instance-00000003'
        server = resource(
            server_id,
            instance_name=instance_name,
            compute_host='compute-1',
            metadata={},
        )
        run = scale.ScaleRun.__new__(scale.ScaleRun)
        run.args = types.SimpleNamespace(
            incus_host=[
                ('compute-1', 'compute-1-ssh'),
                ('compute-2', 'compute-2-ssh'),
            ],
            incus_project='nova',
        )

        def remote_json(host, command):
            if 'profile list' in command:
                return [{'name': instance_name, 'config': {}}]
            if host == 'compute-1-ssh':
                return [self.incus_instance(server_id, instance_name)]
            return []

        run.remote_json = remote_json
        run.preflight_result = {
            'runtime_contract': self.runtime_contract(),
        }

        with self.assertRaisesRegex(
                scale.ScaleFailure, 'duplicate_profiles'):
            run.audit_incus({server_id: server})

    def test_incus_audit_rejects_runtime_limit_drift(self):
        server_id = str(uuid.uuid4())
        instance_name = 'instance-00000004'
        server = resource(
            server_id,
            instance_name=instance_name,
            compute_host='compute-1',
            metadata={},
        )
        run = scale.ScaleRun.__new__(scale.ScaleRun)
        run.args = types.SimpleNamespace(
            incus_host=[('compute-1', 'compute-1-ssh')],
            incus_project='nova',
        )
        instance = self.incus_instance(server_id, instance_name)
        instance['expanded_config']['limits.memory'] = 'unlimited'

        def remote_json(_host, command):
            if 'profile list' in command:
                return [{'name': instance_name, 'config': {}}]
            return [instance]

        run.remote_json = remote_json
        run.preflight_result = {
            'runtime_contract': self.runtime_contract(),
        }

        with self.assertRaisesRegex(
                scale.ScaleFailure, 'runtime_errors'):
            run.audit_incus({server_id: server})


class PlacementMicroversionTest(unittest.TestCase):
    """Provider traits are placement 1.6; the SDK negotiates 1.0."""

    def _run(self, response):
        run = scale.ScaleRun.__new__(scale.ScaleRun)
        calls = []

        class _Placement:
            def get(self, path, params=None, headers=None):
                calls.append((path, params, headers))
                return response

        run.connection = types.SimpleNamespace(placement=_Placement())
        return run, calls

    def test_every_placement_read_pins_the_microversion(self):
        response = types.SimpleNamespace(
            status_code=200, headers={},
            json=lambda: {'traits': ['CUSTOM_INCUS_SYSTEM_CONTAINER']})
        run, calls = self._run(response)

        run._placement_get('/resource_providers/x/traits', 'traits')

        self.assertEqual(1, len(calls))
        self.assertEqual(
            {'OpenStack-API-Version': scale.PLACEMENT_MICROVERSION},
            calls[0][2])
        self.assertEqual('placement 1.6', scale.PLACEMENT_MICROVERSION)


if __name__ == '__main__':
    unittest.main()
