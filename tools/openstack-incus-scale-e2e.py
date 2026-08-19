#!/usr/bin/env python3
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
"""Run staged Nova/Incus scale validation through public OpenStack APIs.

Every create request carries a run UUID and a separate random cleanup token.
Cleanup discovers resources only by that exact metadata pair, verifies every
recorded UUID against it, and then deletes by UUID. Nova servers are never
selected for deletion by name.
"""

import argparse
import concurrent.futures
import datetime
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import openstack


ARTIFACT_SCHEMA_VERSION = 4
DEFAULT_INCUS_CLI_COMMAND = 'podman exec incus incus'
RUN_METADATA_KEY = 'openstack_incus_scale_run'
CLEANUP_METADATA_KEY = 'openstack_incus_scale_cleanup'
ORDINAL_METADATA_KEY = 'openstack_incus_scale_ordinal'
INCUS_SYSTEM_CONTAINER_TRAIT = 'CUSTOM_INCUS_SYSTEM_CONTAINER'
# Resource-provider traits are placement 1.6; everything else this runner
# reads from Placement is available at or below it.
PLACEMENT_MICROVERSION = 'placement 1.6'
INCUS_STORAGE_POOL_TRAIT_PREFIX = 'CUSTOM_INCUS_STORAGE_POOL_'
ALLOWED_BUILD_STATES = {'ACTIVE', 'BUILD'}
ABSENCE_CONFIRMATIONS = 2
TRANSIENT_DELETE_STATUS_CODES = {409, 429, 500, 502, 503, 504}
GIB = 1024 ** 3
UUID_PATTERN = re.compile(
    r'(?<![0-9A-Fa-f])'
    r'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-'
    r'[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}'
    r'(?![0-9A-Fa-f])')


class ScaleFailure(RuntimeError):
    pass


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def percentile(values, percent):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(
        0, min(len(ordered) - 1, math.ceil(len(ordered) * percent) - 1))
    return ordered[index]


def performance_slo_result(observations, maximums):
    violations = {
        name: {
            'observed': observations[name],
            'maximum': maximum,
        }
        for name, maximum in maximums.items()
        if maximum is not None and observations[name] > maximum
    }
    return {
        'passed': not violations,
        'observed': dict(observations),
        'maximum': dict(maximums),
        'violations': violations,
    }


def minimum_slo_result(observations, minimums):
    violations = {
        name: {
            'observed': observations[name],
            'minimum': minimum,
        }
        for name, minimum in minimums.items()
        if minimum is not None and observations[name] < minimum
    }
    return {
        'passed': not violations,
        'observed': dict(observations),
        'minimum': dict(minimums),
        'violations': violations,
    }


def placement_inventory_slots(inventory, usage, amount):
    """Return schedulable slots for one resource-class inventory."""
    required = (
        'total', 'reserved', 'min_unit', 'max_unit', 'step_size',
        'allocation_ratio')
    if not isinstance(inventory, dict) or any(
            key not in inventory for key in required):
        raise ScaleFailure('Placement inventory is incomplete')
    try:
        total = int(inventory['total'])
        reserved = int(inventory['reserved'])
        minimum = int(inventory['min_unit'])
        maximum = int(inventory['max_unit'])
        step = int(inventory['step_size'])
        ratio = float(inventory['allocation_ratio'])
        used = int(usage)
        amount = int(amount)
    except (TypeError, ValueError) as exc:
        raise ScaleFailure(
            'Placement inventory contains non-numeric values') from exc
    if (
        total < 0 or reserved < 0 or reserved > total or minimum < 1 or
        maximum < minimum or step < 1 or ratio <= 0 or used < 0 or
        amount < minimum or amount > maximum or amount % step
    ):
        return 0
    capacity = int((total - reserved) * ratio)
    return max(0, capacity - used) // amount


def placement_fleet_capacity(providers, requested):
    """Calculate whole-instance capacity across root compute providers."""
    if not providers:
        raise ScaleFailure('Placement provider inventory is empty')
    if not requested:
        raise ScaleFailure('Flavor requests no Placement resources')
    per_host = {}
    for host, provider in providers.items():
        inventories = provider.get('inventories')
        usages = provider.get('usages')
        if not isinstance(inventories, dict) or not isinstance(usages, dict):
            raise ScaleFailure(
                'Placement provider {} lacks inventory or usage'.format(host))
        resource_slots = {}
        for resource_class, amount in requested.items():
            inventory = inventories.get(resource_class)
            if inventory is None:
                resource_slots[resource_class] = 0
                continue
            resource_slots[resource_class] = placement_inventory_slots(
                inventory, usages.get(resource_class, 0), amount)
        per_host[host] = {
            'slots': min(resource_slots.values()),
            'resource_slots': resource_slots,
        }
    return {
        'fleet_slots': sum(item['slots'] for item in per_host.values()),
        'providers': per_host,
    }


def subnet_available_addresses(subnet, used_addresses):
    """Return currently unused addresses in explicit allocation pools."""
    pools = getattr(subnet, 'allocation_pools', None)
    if not isinstance(pools, list) or not pools:
        raise ScaleFailure(
            'subnet {} has no explicit allocation pools'.format(subnet.id))
    available = 0
    valid_addresses = set()
    ranges = []
    for pool in pools:
        if not isinstance(pool, dict) or not pool.get('start') or not pool.get(
                'end'):
            raise ScaleFailure(
                'subnet {} has an invalid allocation pool'.format(subnet.id))
        try:
            start = ipaddress.ip_address(pool['start'])
            end = ipaddress.ip_address(pool['end'])
        except ValueError as exc:
            raise ScaleFailure(
                'subnet {} has an invalid allocation pool'.format(
                    subnet.id)) from exc
        if start.version != end.version or int(end) < int(start):
            raise ScaleFailure(
                'subnet {} has an invalid allocation range'.format(
                    subnet.id))
        ranges.append((start, end))
        available += int(end) - int(start) + 1
        valid_addresses.update(
            address for address in used_addresses
            if start <= address <= end)
    ranges.sort(key=lambda item: int(item[0]))
    for previous, current in zip(ranges, ranges[1:]):
        if previous[0].version != current[0].version:
            raise ScaleFailure(
                'subnet {} mixes IP versions in allocation pools'.format(
                    subnet.id))
        if current[0] <= previous[1]:
            raise ScaleFailure(
                'subnet {} has overlapping allocation pools'.format(
                    subnet.id))
    return available - len(valid_addresses)


def parse_instance_idmap(config, key, instance_label):
    """Parse Incus' durable idmap JSON into normalized UID/GID ranges."""
    value = config.get(key)
    if not value:
        return []
    try:
        mappings = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ScaleFailure(
            '{} has invalid {}'.format(instance_label, key)) from exc
    if not isinstance(mappings, list) or not mappings:
        raise ScaleFailure(
            '{} has empty or invalid {}'.format(instance_label, key))
    normalized = []
    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise ScaleFailure(
                '{} has a non-object idmap entry'.format(instance_label))
        try:
            host_id = int(mapping['Hostid'])
            namespace_id = int(mapping['Nsid'])
            map_range = int(mapping['Maprange'])
        except (KeyError, TypeError, ValueError) as exc:
            raise ScaleFailure(
                '{} has an incomplete idmap entry'.format(
                    instance_label)) from exc
        kinds = [
            kind for kind, field in (
                ('uid', 'Isuid'), ('gid', 'Isgid'))
            if mapping.get(field) is True
        ]
        if (
            not kinds or host_id < 0 or namespace_id < 0 or map_range < 1 or
            host_id + map_range <= host_id
        ):
            raise ScaleFailure(
                '{} has an invalid idmap range'.format(instance_label))
        normalized.extend(
            (kind, host_id, host_id + map_range, namespace_id, map_range)
            for kind in kinds)
    return sorted(set(normalized))


def idmap_overlap_errors(ranges_by_host):
    """Return deployment-wide overlapping UID/GID ranges.

    Independent Incus daemons have no shared view of isolated idmaps. A
    range reused on another compute is therefore a migration blocker even
    though each daemon's host-local allocation is valid.
    """
    errors = []
    fleet_ranges = [
        {**item, 'host': host}
        for host, ranges in ranges_by_host.items()
        for item in ranges
    ]
    for kind in ('uid', 'gid'):
        ordered = sorted(
            (item for item in fleet_ranges if item['kind'] == kind),
            key=lambda item: (
                item['start'], item['end'], item['host'],
                item['instance_uuid']))
        active = []
        for current in ordered:
            active = [
                previous for previous in active
                if previous['end'] > current['start']
            ]
            for previous in active:
                if current['start'] < previous['end']:
                    errors.append({
                        'kind': kind,
                        'first': previous,
                        'second': current,
                    })
            active.append(current)
    return errors


def neutron_binding_errors(stage_servers, ports):
    """Return ports that do not resolve to the server's Nova compute host."""
    errors = {}
    for server_id, server in stage_servers.items():
        server_ports = ports.get(server_id, [])
        expected_host = server_value(
            server, 'compute_host', 'OS-EXT-SRV-ATTR:host')
        if (
            len(server_ports) != 1 or
            str(server_ports[0].status).upper() != 'ACTIVE' or
            server_ports[0].binding_host_id != expected_host
        ):
            errors[server_id] = [
                {
                    'id': port.id,
                    'status': port.status,
                    'binding_host_id': port.binding_host_id,
                    'expected_binding_host_id': expected_host,
                }
                for port in server_ports
            ]
    return errors


def host_storage_threshold(total, minimum_bytes, minimum_percent):
    return max(minimum_bytes, math.ceil(total * minimum_percent / 100.0))


def projected_host_storage(
        baseline, current, checkpoint, final_target,
        minimum_bytes, minimum_percent):
    """Validate current and linearly projected host filesystem headroom."""
    required = host_storage_threshold(
        current['total_bytes'], minimum_bytes, minimum_percent)
    if current['available_bytes'] < required:
        raise ScaleFailure(
            'host filesystem available bytes fell below its hard threshold')
    if current['available_inodes'] * 100 < (
            current['total_inodes'] * minimum_percent):
        raise ScaleFailure(
            'host filesystem available inodes fell below its hard threshold')
    used_before = baseline['total_bytes'] - baseline['available_bytes']
    used_now = current['total_bytes'] - current['available_bytes']
    slope = max(0, used_now - used_before) / checkpoint
    projected = current['available_bytes'] - (
        slope * max(0, final_target - checkpoint))
    if projected < required:
        raise ScaleFailure(
            'host filesystem cannot retain required headroom at the final '
            'checkpoint')
    return {
        'required_available_bytes': required,
        'growth_bytes_per_instance': slope,
        'projected_available_bytes': int(projected),
    }


def validate_ceph_status(status, required_bytes):
    """Validate read-only Ceph health and root-pool capacity evidence."""
    required_keys = {
        'fsid', 'health', 'pool', 'available_bytes',
        'pool_stored_bytes', 'pool_max_bytes', 'raw_used_ratio',
        'nearfull_ratio', 'full_ratio',
    }
    if not isinstance(status, dict) or not required_keys.issubset(status):
        raise ScaleFailure('Ceph status helper returned incomplete evidence')
    if (
        not isinstance(status['fsid'], str) or not status['fsid'] or
        not isinstance(status['pool'], str) or not status['pool'] or
        status['health'] != 'HEALTH_OK'
    ):
        raise ScaleFailure(
            'Ceph cluster identity or health is not production-ready')
    try:
        available = int(status['available_bytes'])
        stored = int(status['pool_stored_bytes'])
        maximum = int(status['pool_max_bytes'])
        used_ratio = float(status['raw_used_ratio'])
        nearfull = float(status['nearfull_ratio'])
        full = float(status['full_ratio'])
    except (TypeError, ValueError) as exc:
        raise ScaleFailure(
            'Ceph status helper returned non-numeric capacity data') from exc
    if (
        available < required_bytes or stored < 0 or maximum < 0 or
        not 0 <= used_ratio < nearfull < full <= 1
    ):
        raise ScaleFailure(
            'Ceph cluster lacks required capacity or full-ratio headroom')
    if maximum and maximum - stored < required_bytes:
        raise ScaleFailure(
            'Ceph RBD pool quota lacks capacity for the scale target')
    return {
        **status,
        'required_bytes': required_bytes,
        'pool_quota_available_bytes': (
            None if maximum == 0 else maximum - stored),
    }


def root_storage_pool_trait(selector):
    suffix = re.sub(r'[^A-Z0-9_]', '_', selector.upper())
    trait = INCUS_STORAGE_POOL_TRAIT_PREFIX + suffix
    if not suffix or len(trait) > 255:
        raise ScaleFailure(
            'invalid Incus root storage pool selector {!r}'.format(selector))
    return trait


def flavor_extra_specs(flavor):
    specs = getattr(flavor, 'extra_specs', None)
    if specs is None and hasattr(flavor, 'to_dict'):
        specs = flavor.to_dict().get('extra_specs')
    if not isinstance(specs, dict):
        raise ScaleFailure(
            'selected Flavor did not expose extra_specs; administrative '
            'Flavor visibility is required')
    return specs


def validate_scale_flavor(flavor):
    specs = flavor_extra_specs(flavor)
    system_trait_key = 'trait:{}'.format(INCUS_SYSTEM_CONTAINER_TRAIT)
    if specs.get(system_trait_key) != 'required':
        raise ScaleFailure(
            'selected Flavor must set {}=required'.format(system_trait_key))

    selector = specs.get('incus:root_storage_pool')
    pool_trait = None
    if selector:
        pool_trait = root_storage_pool_trait(selector)
        pool_trait_key = 'trait:{}'.format(pool_trait)
        if specs.get(pool_trait_key) != 'required':
            raise ScaleFailure(
                'Flavor root pool selector {} must set {}=required'.format(
                    selector, pool_trait_key))
    return {
        'extra_specs': dict(specs),
        'required_traits': [
            INCUS_SYSTEM_CONTAINER_TRAIT,
        ] + ([pool_trait] if pool_trait else []),
        'root_pool_selector': selector,
    }


def host_distribution_result(hosts, eligible_hosts, maximum_skew_percent):
    eligible_hosts = set(eligible_hosts)
    observed_hosts = set(hosts)
    outside = sorted(observed_hosts - eligible_hosts)
    missing = sorted(eligible_hosts - observed_hosts)
    counts = [hosts.get(host, 0) for host in sorted(eligible_hosts)]
    average = sum(counts) / len(counts) if counts else 0.0
    skew_percent = (
        ((max(counts) - min(counts)) / average) * 100.0
        if average else 0.0)
    violation = (
        maximum_skew_percent is not None and
        skew_percent > maximum_skew_percent)
    return {
        'passed': not outside and not missing and not violation,
        'eligible_hosts': sorted(eligible_hosts),
        'outside_hosts': outside,
        'missing_hosts': missing,
        'counts': {
            host: hosts.get(host, 0) for host in sorted(eligible_hosts)
        },
        'observed_skew_percent': skew_percent,
        'maximum_skew_percent': maximum_skew_percent,
    }


def per_compute_distribution_result(
        hosts, eligible_hosts, target_per_compute, minimum_percent,
        maximum_skew_percent):
    """Validate an explicit per-compute checkpoint distribution."""
    result = host_distribution_result(
        hosts, eligible_hosts, maximum_skew_percent)
    minimum = int(math.ceil(target_per_compute * minimum_percent / 100.0))
    below_minimum = {
        host: hosts.get(host, 0)
        for host in sorted(set(eligible_hosts))
        if hosts.get(host, 0) < minimum
    }
    result.update({
        'target_per_compute': target_per_compute,
        'minimum_percent': minimum_percent,
        'minimum_per_compute': minimum,
        'below_minimum': below_minimum,
    })
    result['passed'] = result['passed'] and not below_minimum
    return result


def resolve_checkpoints(checkpoints, per_compute_checkpoints, host_count):
    """Resolve mutually exclusive fleet or per-compute checkpoints."""
    if checkpoints is not None and per_compute_checkpoints is not None:
        raise ScaleFailure(
            'use either fleet checkpoints or per-compute checkpoints')
    if per_compute_checkpoints is not None:
        if host_count < 1:
            raise ScaleFailure(
                'per-compute checkpoints require Incus host mappings')
        return (
            [value * host_count for value in per_compute_checkpoints],
            {
                value * host_count: value
                for value in per_compute_checkpoints
            },
        )
    values = checkpoints if checkpoints is not None else [100, 500, 1000]
    return list(values), {}


def labeled_command(value):
    label, separator, command = value.partition('=')
    if (not separator or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',
                                          label) or not command.strip()):
        raise argparse.ArgumentTypeError(
            'must be LABEL=COMMAND with a simple non-empty label')
    return label, command


def validate_soak_window(seconds, periodic_interval, minimum_cycles):
    if seconds == 0:
        return {
            'enabled': False,
            'configured_seconds': 0,
            'periodic_interval_seconds': periodic_interval,
            'minimum_periodic_cycles': minimum_cycles,
        }
    required = periodic_interval * minimum_cycles
    if seconds < required:
        raise ScaleFailure(
            'idle soak is {} seconds but {} seconds are required to cover '
            '{} periodic-task intervals'.format(
                seconds, required, minimum_cycles))
    return {
        'enabled': True,
        'configured_seconds': seconds,
        'periodic_interval_seconds': periodic_interval,
        'minimum_periodic_cycles': minimum_cycles,
        'covered_periodic_cycles': int(seconds // periodic_interval),
    }


def _canonical_json(value):
    try:
        return json.dumps(
            value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
            allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ScaleFailure('inventory value is not JSON serializable') from exc


def normalize_idmap_inventory(value):
    """Normalize an exact etcd ID-map key snapshot.

    The helper must decode etcd keys and values. Values remain schema opaque so
    current allocation records and later claim/materialization records can be
    audited with the same interface.
    """
    if not isinstance(value, dict):
        raise ScaleFailure('ID-map inventory must return a JSON object')
    revision = value.get('revision')
    entries = value.get('entries')
    if (isinstance(revision, bool) or
            not isinstance(revision, (int, str)) or
            not str(revision).isdigit() or int(revision) < 1):
        raise ScaleFailure(
            'ID-map inventory must include a positive etcd revision')
    if not isinstance(entries, list):
        raise ScaleFailure('ID-map inventory entries must be a JSON list')
    normalized = {}
    for index, entry in enumerate(entries):
        if (not isinstance(entry, dict) or
                not isinstance(entry.get('key'), str) or
                not entry.get('key') or 'value' not in entry):
            raise ScaleFailure(
                'ID-map inventory entry {} must contain key and value'
                .format(index))
        key = entry['key']
        if key in normalized:
            raise ScaleFailure(
                'ID-map inventory contains duplicate key {}'.format(key))
        canonical = _canonical_json(entry['value'])
        normalized[key] = {
            'digest': hashlib.sha256(canonical.encode('utf-8')).hexdigest(),
            'value': entry['value'],
        }
    return {
        'revision': str(revision),
        'entries': normalized,
    }


def idmap_inventory_references(inventory, instance_uuids):
    """Associate opaque current/future ID-map records with exact UUIDs."""
    wanted = set(instance_uuids)
    references = {instance_uuid: [] for instance_uuid in sorted(wanted)}
    for key, entry in inventory['entries'].items():
        material = '{}\n{}'.format(key, _canonical_json(entry['value']))
        found = {
            value.lower() for value in UUID_PATTERN.findall(material)
        }
        for instance_uuid in wanted & found:
            references[instance_uuid].append(key)
    return references


def idmap_inventory_delta(baseline, current, instance_uuids):
    if int(current['revision']) < int(baseline['revision']):
        raise ScaleFailure(
            'ID-map inventory revision moved backwards from {} to {}'
            .format(baseline['revision'], current['revision']))
    before = baseline['entries']
    now = current['entries']
    references = idmap_inventory_references(current, instance_uuids)
    run_keys = sorted({
        key for keys in references.values() for key in keys
    })
    baseline_references = idmap_inventory_references(
        baseline, instance_uuids)
    changed_baseline = sorted(
        key for key in set(before) & set(now)
        if before[key]['digest'] != now[key]['digest'])
    return {
        'baseline_revision': baseline['revision'],
        'current_revision': current['revision'],
        'baseline_count': len(before),
        'current_count': len(now),
        'added_keys': sorted(set(now) - set(before)),
        'removed_keys': sorted(set(before) - set(now)),
        'changed_baseline_keys': changed_baseline,
        'run_key_count': len(run_keys),
        'run_keys': run_keys,
        'run_key_digests': {
            key: now[key]['digest'] for key in run_keys
        },
        'run_keys_by_instance': references,
        'baseline_run_keys_by_instance': baseline_references,
    }


def summarize_telemetry_samples(samples):
    if not samples:
        return {'sample_count': 0, 'hosts': {}}
    hosts = {}
    all_hosts = sorted({
        host for sample in samples
        for host in sample.get('hosts', {})
    })
    for host in all_hosts:
        snapshots = [
            sample['hosts'][host] for sample in samples
            if host in sample.get('hosts', {})
        ]
        processes = {}
        labels = sorted({
            label for snapshot in snapshots
            for label in snapshot.get('processes', {})
        })
        for label in labels:
            values = [
                snapshot['processes'].get(label, {})
                for snapshot in snapshots
            ]
            cpu_values = [float(value.get('cpu_seconds', 0.0))
                          for value in values]
            processes[label] = {
                'cpu_seconds_delta': max(
                    0.0, cpu_values[-1] - cpu_values[0]),
                'peak_rss_bytes': max(
                    int(value.get('rss_bytes', 0)) for value in values),
                'peak_fd_count': max(
                    int(value.get('fd_count', 0)) for value in values),
                'peak_process_count': max(
                    int(value.get('process_count', 0)) for value in values),
            }
        first_cpu = snapshots[0].get('host_cpu_ticks', {})
        last_cpu = snapshots[-1].get('host_cpu_ticks', {})
        total_delta = max(
            0, int(last_cpu.get('total', 0)) -
            int(first_cpu.get('total', 0)))
        idle_delta = max(
            0, int(last_cpu.get('idle', 0)) -
            int(first_cpu.get('idle', 0)))
        busy_percent = (
            100.0 * max(0, total_delta - idle_delta) / total_delta
            if total_delta else 0.0)
        hosts[host] = {
            'processes': processes,
            'host_cpu_busy_percent': busy_percent,
            'minimum_available_memory_bytes': min(
                int(snapshot.get('memory_available_bytes', 0))
                for snapshot in snapshots),
        }
    return {
        'sample_count': len(samples),
        'first_collected_at': samples[0].get('collected_at'),
        'last_collected_at': samples[-1].get('collected_at'),
        'hosts': hosts,
    }


def chunked(values, size):
    values = list(values)
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def atomic_json_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix='.{}.'.format(path.name), suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def reserve_artifact(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ScaleFailure(
            'artifact already exists: {}; choose a new path'.format(path)) \
            from exc
    os.close(descriptor)


def artifact_wal_path(path):
    return Path('{}.wal'.format(path))


def append_json_record(path, value):
    encoded = (
        json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n'
    ).encode('utf-8')
    descriptor = os.open(
        path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        written = 0
        while written < len(encoded):
            written += os.write(descriptor, encoded[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def apply_artifact_record(state, record):
    if not isinstance(record, dict) or record.get('type') != 'server-created':
        raise ScaleFailure('artifact WAL contains an unsupported record')
    try:
        server_id = str(uuid.UUID(record['server_id']))
        submitted_epoch = float(record['submitted_epoch'])
        accepted_epoch = float(record['accepted_epoch'])
        create_latency = float(record['create_latency'])
    except (KeyError, TypeError, ValueError) as exc:
        raise ScaleFailure('artifact WAL contains an invalid create record') \
            from exc
    if (not math.isfinite(submitted_epoch) or
            not math.isfinite(accepted_epoch) or
            not math.isfinite(create_latency) or
            accepted_epoch < submitted_epoch or create_latency < 0):
        raise ScaleFailure('artifact WAL contains invalid timing data')

    server_ids = state.setdefault('server_ids', [])
    if server_id not in server_ids:
        server_ids.append(server_id)
    submitted = state.setdefault('submitted_epoch', {})
    accepted = state.setdefault('accepted_epoch', {})
    latencies = state.setdefault('create_latencies', {})
    for mapping, value, label in (
            (submitted, submitted_epoch, 'submitted_epoch'),
            (accepted, accepted_epoch, 'accepted_epoch'),
            (latencies, create_latency, 'create_latency')):
        existing = mapping.get(server_id)
        if existing is not None and float(existing) != value:
            raise ScaleFailure(
                'artifact WAL conflicts with {} for {}'.format(
                    label, server_id))
        mapping[server_id] = value
    instance_name = record.get('instance_name')
    if instance_name:
        names = state.setdefault('instance_names', {})
        existing = names.get(server_id)
        if existing is not None and existing != instance_name:
            raise ScaleFailure(
                'artifact WAL conflicts with instance name for {}'.format(
                    server_id))
        if instance_name in {
                value for key, value in names.items() if key != server_id}:
            raise ScaleFailure(
                'artifact WAL contains duplicate instance name {}'.format(
                    instance_name))
        names[server_id] = instance_name


def server_value(server, attribute, legacy_key=None):
    value = getattr(server, attribute, None)
    if value is not None:
        return value
    if legacy_key is not None and hasattr(server, 'to_dict'):
        return server.to_dict().get(legacy_key)
    return None


def server_metadata(server):
    metadata = server_value(server, 'metadata', 'metadata')
    return metadata if isinstance(metadata, dict) else {}


def positive_int(value):
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('must be an integer') from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError('must be greater than zero')
    return parsed


def positive_float(value):
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('must be a number') from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError(
            'must be a finite number greater than zero')
    return parsed


def nonnegative_float(value):
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('must be a number') from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError(
            'must be a finite number greater than or equal to zero')
    return parsed


def uuid_value(value):
    try:
        return str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError('must be a UUID') from exc


def incus_host_mapping(value):
    nova_host, separator, ssh_target = value.partition('=')
    if (not separator or not nova_host or not ssh_target or
            ssh_target.startswith('-') or
            any(item.isspace() for item in value)):
        raise argparse.ArgumentTypeError(
            'must be NOVA_HOST=SSH_TARGET without whitespace')
    return nova_host, ssh_target


def parse_checkpoints(value):
    try:
        values = [int(item) for item in value.split(',') if item]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            'checkpoints must be comma-separated integers') from exc
    if not values or values != sorted(set(values)) or values[0] < 1:
        raise argparse.ArgumentTypeError(
            'checkpoints must be unique, positive, and increasing')
    return values


def quota_requirement(name, limit, used, requested, reserved=0):
    values = {
        'limit': limit,
        'used': used,
        'requested': requested,
        'reserved': reserved,
    }
    try:
        values = {key: int(value) for key, value in values.items()}
    except (TypeError, ValueError) as exc:
        raise ScaleFailure(
            '{} quota returned non-integer values: {}'.format(
                name, values)) from exc
    if values['used'] < 0 or values['requested'] < 0:
        raise ScaleFailure('{} quota returned invalid usage'.format(name))
    if values['reserved'] < 0:
        raise ScaleFailure('{} quota returned invalid reservation'.format(
            name))
    required_total = (
        values['used'] + values['reserved'] + values['requested'])
    if values['limit'] >= 0 and required_total > values['limit']:
        raise ScaleFailure(
            '{} quota is insufficient: limit={}, used={}, reserved={}, '
            'requested={}'.format(
                name, values['limit'], values['used'], values['reserved'],
                values['requested']))
    values['required_total'] = required_total
    return values


def inventory_names(label, value):
    if not isinstance(value, list):
        raise ScaleFailure(
            '{} inventory command must return a JSON list'.format(label))
    names = []
    for item in value:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = item.get('name')
        else:
            name = None
        if not isinstance(name, str) or not name:
            raise ScaleFailure(
                '{} inventory contains an entry without a name'.format(
                    label))
        names.append(name)
    if len(names) != len(set(names)):
        raise ScaleFailure(
            '{} inventory contains duplicate names'.format(label))
    return sorted(names)


def response_json(response, description):
    try:
        value = response.json()
    except (AttributeError, ValueError) as exc:
        raise ScaleFailure(
            '{} did not return JSON'.format(description)) from exc
    if not isinstance(value, dict):
        raise ScaleFailure(
            '{} did not return a JSON object'.format(description))
    return value


def is_transient_delete_error(exc):
    status_code = getattr(exc, 'status_code', None)
    if status_code in TRANSIENT_DELETE_STATUS_CODES:
        return True
    return (
        status_code == 400 and
        any(token in str(exc).lower()
            for token in ('busy', 'conflict', 'in progress')))


def load_cleanup_artifact(path):
    path = Path(path).resolve()
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScaleFailure(
            'cannot read cleanup artifact {}: {}'.format(path, exc)) from exc
    if value.get('schema_version') != ARTIFACT_SCHEMA_VERSION:
        raise ScaleFailure(
            'unsupported artifact schema in {}'.format(path))
    wal = artifact_wal_path(path)
    if wal.exists():
        try:
            with wal.open('r', encoding='utf-8') as stream:
                lines = stream.readlines()
                for line_number, line in enumerate(lines, 1):
                    if not line.endswith('\n'):
                        if line_number != len(lines):
                            raise ScaleFailure(
                                'artifact WAL has an incomplete record at '
                                'line {}'.format(line_number))
                        value['wal_torn_tail_ignored'] = True
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ScaleFailure(
                            'artifact WAL contains invalid JSON at line {}'
                            .format(line_number)) from exc
                    apply_artifact_record(value, record)
        except OSError as exc:
            raise ScaleFailure(
                'cannot read artifact WAL {}: {}'.format(wal, exc)) from exc
    try:
        value['run_id'] = str(uuid.UUID(value['run_id']))
        value['cleanup_token'] = str(uuid.UUID(value['cleanup_token']))
        server_ids = [str(uuid.UUID(item))
                      for item in value.get('server_ids', [])]
    except (KeyError, TypeError, ValueError) as exc:
        raise ScaleFailure(
            'artifact contains an invalid run, cleanup, or server UUID') \
            from exc
    if len(server_ids) != len(set(server_ids)):
        raise ScaleFailure('artifact contains duplicate server UUIDs')
    if not value.get('project_id'):
        raise ScaleFailure('artifact does not identify its OpenStack project')
    value['server_ids'] = server_ids
    return path, value


class ScaleRun:
    def __init__(self, args, connection=None):
        self.args = args
        self.connection = connection or self._connect(args)
        self.run_id = args.run_id or str(uuid.uuid4())
        self.cleanup_token = str(uuid.uuid4())
        self.prefix = '{}-{}'.format(
            args.name_prefix, self.run_id.split('-', 1)[0])
        self.project_id = self.connection.current_project_id
        if not self.project_id:
            raise ScaleFailure('project-scoped OpenStack credentials required')

        artifact = args.artifact
        if artifact is None:
            artifact = 'openstack-incus-scale-{}.json'.format(self.run_id)
        self.artifact = Path(artifact).resolve()

        self.server_ids = []
        self.instance_names = {}
        self.create_latencies = {}
        self.submitted_epoch = {}
        self.accepted_epoch = {}
        self.active_epoch = {}
        self.delete_latencies = []
        self.delete_attempts = {}
        self.checkpoints = []
        self.soak_result = {
            'attempted': False,
            'completed': False,
        }
        self.preflight_result = {}
        self.external_inventory_baseline = {}
        self.idmap_inventory_baseline = {}
        self.inventory_command_fingerprints = {}
        self.telemetry_command_fingerprints = {}
        self.artifact_recovery = {}
        self.started_at = utc_now()
        self.ended_at = None
        self.status = 'initialized'
        self.failure = None
        self.cleanup_result = {
            'attempted': False,
            'completed': False,
        }
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()

        self.image = self.connection.image.find_image(
            args.image, ignore_missing=False)
        self.flavor = self.connection.compute.find_flavor(
            args.flavor, ignore_missing=False)
        self.flavor_contract = validate_scale_flavor(self.flavor)
        self.network = self.connection.network.find_network(
            args.network, ignore_missing=False)
        self.resources = {
            'image_id': self.image.id,
            'flavor_id': self.flavor.id,
            'network_id': self.network.id,
            'flavor_contract': self.flavor_contract,
        }
        reserve_artifact(self.artifact)

    @staticmethod
    def _connect(args):
        kwargs = {
            'app_name': 'openstack-incus-scale-e2e',
            'app_version': '1',
        }
        if args.cloud:
            kwargs['cloud'] = args.cloud
        return openstack.connect(**kwargs)

    @classmethod
    def from_cleanup_artifact(cls, args, connection=None):
        artifact, state = load_cleanup_artifact(args.cleanup_artifact)
        self = cls.__new__(cls)
        self.args = args
        self.connection = connection or self._connect(args)
        self.project_id = self.connection.current_project_id
        if self.project_id != state['project_id']:
            try:
                self.connection.close()
            finally:
                raise ScaleFailure(
                    'artifact project {} does not match current project {}'
                    .format(state['project_id'], self.project_id))
        self.run_id = state['run_id']
        self.cleanup_token = state['cleanup_token']
        self.prefix = state.get('prefix', 'incus-scale-recovery')
        self.artifact = artifact
        self.server_ids = list(state['server_ids'])
        instance_names = state.get('instance_names', {})
        if (not isinstance(instance_names, dict) or
                any(server_id not in self.server_ids or
                    not isinstance(instance_name, str) or
                    not instance_name
                    for server_id, instance_name in instance_names.items()) or
                len(set(instance_names.values())) != len(instance_names)):
            raise ScaleFailure(
                'artifact contains invalid Nova instance-name mappings')
        self.instance_names = dict(instance_names)
        raw_create_latencies = state.get('create_latencies', {})
        if not isinstance(raw_create_latencies, dict):
            raw_create_latencies = {}
        self.create_latencies = {
            server_id: float(value)
            for server_id, value in raw_create_latencies.items()
            if server_id in self.server_ids
        }
        self.submitted_epoch = dict(state.get('submitted_epoch', {}))
        self.accepted_epoch = dict(state.get('accepted_epoch', {}))
        self.active_epoch = dict(state.get('active_epoch', {}))
        self.delete_latencies = []
        self.delete_attempts = {}
        self.checkpoints = list(state.get('checkpoints', []))
        self.soak_result = dict(state.get('idle_soak', {
            'attempted': False,
            'completed': False,
        }))
        self.preflight_result = dict(state.get('preflight', {}))
        self.external_inventory_baseline = dict(
            state.get('external_inventory_baseline', {}))
        self.idmap_inventory_baseline = dict(
            state.get('idmap_inventory_baseline', {}))
        self.inventory_command_fingerprints = dict(
            state.get('inventory_command_fingerprints', {}))
        self.telemetry_command_fingerprints = dict(
            state.get('telemetry_command_fingerprints', {}))
        self.artifact_recovery = dict(
            state.get('artifact_recovery', {}))
        if state.get('wal_torn_tail_ignored'):
            self.artifact_recovery['wal_torn_tail_ignored'] = True
        self.started_at = state.get('started_at', utc_now())
        self.ended_at = None
        self.status = 'cleanup'
        self.failure = state.get('failure')
        self.cleanup_result = {
            'attempted': False,
            'completed': False,
        }
        stored_hosts = state.get('incus_hosts', [])
        try:
            stored_hosts = [
                (item[0], item[1]) for item in stored_hosts
                if isinstance(item, (list, tuple)) and len(item) == 2 and all(
                    isinstance(value, str) and value for value in item)
            ]
        except (TypeError, ValueError) as exc:
            raise ScaleFailure(
                'artifact contains invalid Incus host mappings') from exc
        if len(stored_hosts) != len(state.get('incus_hosts', [])):
            raise ScaleFailure(
                'artifact contains invalid Incus host mappings')
        if not args.incus_host:
            args.incus_host = stored_hosts
        elif stored_hosts:
            stored_nova_hosts = {item[0] for item in stored_hosts}
            override_nova_hosts = {item[0] for item in args.incus_host}
            if stored_nova_hosts != override_nova_hosts:
                raise ScaleFailure(
                    'cleanup host override must map the artifact Nova hosts')
        if (len({item[0] for item in args.incus_host}) !=
                len(args.incus_host) or
                len({item[1] for item in args.incus_host}) !=
                len(args.incus_host)):
            raise ScaleFailure('cleanup Incus host mappings must be unique')
        stored_project = state.get('incus_project', 'nova')
        if args.incus_project is None:
            args.incus_project = stored_project
        elif args.incus_project != stored_project:
            raise ScaleFailure(
                'cleanup Incus project must match the artifact')
        if not args.incus_project:
            raise ScaleFailure('artifact contains an invalid Incus project')
        stored_cli_command = state.get(
            'incus_cli_command', DEFAULT_INCUS_CLI_COMMAND)
        if not isinstance(stored_cli_command, str) or not stored_cli_command:
            raise ScaleFailure(
                'artifact contains an invalid Incus CLI command')
        if args.incus_cli_command is None:
            args.incus_cli_command = stored_cli_command
        elif args.incus_cli_command != stored_cli_command:
            raise ScaleFailure(
                'cleanup Incus CLI command must match the artifact')
        self.resources = dict(state.get('resources', {}))
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self.image = None
        self.flavor = None
        self.flavor_contract = dict(
            self.resources.get('flavor_contract', {}))
        self.network = None
        return self

    def _state(self):
        return {
            'schema_version': ARTIFACT_SCHEMA_VERSION,
            'run_id': self.run_id,
            'cleanup_token': self.cleanup_token,
            'project_id': self.project_id,
            'prefix': self.prefix,
            'started_at': self.started_at,
            'ended_at': self.ended_at,
            'status': self.status,
            'failure': self.failure,
            'server_ids': list(self.server_ids),
            'instance_names': dict(self.instance_names),
            'create_latencies': dict(
                getattr(self, 'create_latencies', {})),
            'submitted_epoch': dict(
                getattr(self, 'submitted_epoch', {})),
            'accepted_epoch': dict(getattr(self, 'accepted_epoch', {})),
            'active_epoch': dict(getattr(self, 'active_epoch', {})),
            'checkpoints': list(self.checkpoints),
            'idle_soak': dict(getattr(self, 'soak_result', {})),
            'preflight': dict(self.preflight_result),
            'external_inventory_baseline': dict(
                getattr(self, 'external_inventory_baseline', {})),
            'idmap_inventory_baseline': dict(
                getattr(self, 'idmap_inventory_baseline', {})),
            'inventory_command_fingerprints': dict(
                getattr(self, 'inventory_command_fingerprints', {})),
            'telemetry_command_fingerprints': dict(
                getattr(self, 'telemetry_command_fingerprints', {})),
            'artifact_recovery': dict(
                getattr(self, 'artifact_recovery', {})),
            'cleanup': dict(self.cleanup_result),
            'resources': dict(self.resources),
            'incus_hosts': list(self.args.incus_host),
            'incus_project': self.args.incus_project,
            'incus_cli_command': getattr(
                self.args, 'incus_cli_command', DEFAULT_INCUS_CLI_COMMAND),
            'artifact': str(self.artifact),
        }

    def _save_locked(self):
        atomic_json_write(self.artifact, self._state())
        try:
            artifact_wal_path(self.artifact).unlink()
        except FileNotFoundError:
            pass

    def save(self):
        with self._state_lock:
            self._save_locked()

    def _journal_created_server(
            self, server_id, instance_name, submitted_epoch,
            accepted_epoch, latency):
        record = {
            'type': 'server-created',
            'server_id': server_id,
            'instance_name': instance_name,
            'submitted_epoch': submitted_epoch,
            'accepted_epoch': accepted_epoch,
            'create_latency': latency,
        }
        append_json_record(artifact_wal_path(self.artifact), record)

    def _owns_server(self, server):
        metadata = server_metadata(server)
        return (
            metadata.get(RUN_METADATA_KEY) == self.run_id and
            metadata.get(CLEANUP_METADATA_KEY) == self.cleanup_token)

    def _list_project_servers(self):
        started = time.monotonic()
        servers = list(self.connection.compute.servers(details=True))
        return {server.id: server for server in servers}, (
            time.monotonic() - started)

    def _quota_preflight(self):
        target = self.args.checkpoints[-1]
        try:
            vcpus = int(self.flavor.vcpus)
            ram = int(self.flavor.ram)
        except (TypeError, ValueError) as exc:
            raise ScaleFailure(
                'selected flavor does not expose integer vCPU and RAM values'
            ) from exc
        if vcpus < 1 or ram < 1:
            raise ScaleFailure(
                'selected flavor must provide positive vCPU and RAM values')

        limits = self.connection.compute.get_limits()
        absolute = getattr(limits, 'absolute', None)
        if absolute is None:
            raise ScaleFailure('Nova did not return absolute quota limits')
        nova = {
            'instances': quota_requirement(
                'Nova instances', absolute.instances,
                absolute.instances_used, target),
            'cores': quota_requirement(
                'Nova cores', absolute.total_cores,
                absolute.total_cores_used, target * vcpus),
            'ram_mib': quota_requirement(
                'Nova RAM', absolute.total_ram,
                absolute.total_ram_used, target * ram),
        }
        try:
            metadata_limit = int(absolute.server_meta)
        except (TypeError, ValueError) as exc:
            raise ScaleFailure(
                'Nova did not return an integer server metadata quota') \
                from exc
        if metadata_limit >= 0 and metadata_limit < 3:
            raise ScaleFailure(
                'Nova server metadata quota must allow at least three keys')

        neutron_quota = self.connection.network.get_quota(
            self.project_id, details=True)
        port_quota = getattr(neutron_quota, 'ports', None)
        if not isinstance(port_quota, dict):
            raise ScaleFailure(
                'Neutron did not return detailed port quota usage')
        try:
            neutron = {
                'ports': quota_requirement(
                    'Neutron ports',
                    port_quota['limit'],
                    port_quota['used'],
                    target,
                    port_quota.get('reserved', 0)),
            }
        except KeyError as exc:
            raise ScaleFailure(
                'Neutron detailed port quota is missing {}'.format(exc)) \
                from exc
        return {
            'target': target,
            'flavor_vcpus': vcpus,
            'flavor_ram_mib': ram,
            'nova': nova,
            'neutron': neutron,
        }

    def _network_capacity_preflight(self):
        """Prove every subnet has enough addresses for the final target."""
        target = self.args.checkpoints[-1]
        subnets = list(
            self.connection.network.subnets(network_id=self.network.id))
        if not subnets:
            raise ScaleFailure(
                'scale network has no subnet with allocatable addresses')
        used_by_subnet = {subnet.id: set() for subnet in subnets}
        for port in self.connection.network.ports(network_id=self.network.id):
            for fixed_ip in getattr(port, 'fixed_ips', []) or []:
                if not isinstance(fixed_ip, dict):
                    raise ScaleFailure(
                        'Neutron port contains invalid fixed_ips data')
                subnet_id = fixed_ip.get('subnet_id')
                address = fixed_ip.get('ip_address')
                if subnet_id not in used_by_subnet or not address:
                    continue
                try:
                    used_by_subnet[subnet_id].add(
                        ipaddress.ip_address(address))
                except ValueError as exc:
                    raise ScaleFailure(
                        'Neutron port contains an invalid fixed IP') from exc

        evidence = {}
        for subnet in subnets:
            available = subnet_available_addresses(
                subnet, used_by_subnet[subnet.id])
            if available < target:
                raise ScaleFailure(
                    'subnet {} has {} available addresses, fewer than the '
                    '{} scale target'.format(subnet.id, available, target))
            evidence[subnet.id] = {
                'available_addresses': available,
                'used_addresses': len(used_by_subnet[subnet.id]),
            }
        return evidence

    def _runtime_contract(self):
        try:
            vcpus = int(self.flavor.vcpus)
            ram_mib = int(self.flavor.ram)
            root_gb = int(self.flavor.disk)
        except (TypeError, ValueError) as exc:
            raise ScaleFailure(
                'selected Flavor must expose integer vcpus, ram and disk') \
                from exc
        specs = self.flavor_contract['extra_specs']
        process_limit = self.args.expected_process_limit
        if process_limit is None:
            try:
                process_limit = int(specs['incus:process_limit'])
            except (KeyError, TypeError, ValueError) as exc:
                raise ScaleFailure(
                    'set --expected-process-limit or configure a positive '
                    'incus:process_limit Flavor extra spec') from exc
        if process_limit < 1:
            raise ScaleFailure('expected process limit must be positive')
        if root_gb < 1:
            raise ScaleFailure(
                'scale Flavor disk/root_gb must be positive')
        placement_resources = {
            'VCPU': vcpus,
            'MEMORY_MB': ram_mib,
            'DISK_GB': root_gb,
        }
        for key, value in specs.items():
            if not key.startswith('resources:'):
                continue
            resource_class = key.split(':', 1)[1]
            try:
                amount = int(value)
            except (TypeError, ValueError) as exc:
                raise ScaleFailure(
                    'Flavor {} must be an integer'.format(key)) from exc
            if amount < 1:
                raise ScaleFailure(
                    'Flavor {} must be positive'.format(key))
            placement_resources[resource_class] = amount
        return {
            'type': 'container',
            'status': 'Running',
            'root_pool': self.args.expected_root_pool,
            'root_size': '{}GB'.format(root_gb),
            'config': {
                'limits.cpu': str(vcpus),
                'limits.memory': '{}MiB'.format(ram_mib),
                'limits.processes': str(process_limit),
                'security.idmap.isolated': 'true',
                'security.privileged': 'false',
            },
            'placement_resources': placement_resources,
        }

    def _placement_get(self, path, description, params=None):
        placement = getattr(self.connection, 'placement', None)
        if placement is None or not hasattr(placement, 'get'):
            raise ScaleFailure(
                'OpenStackSDK connection does not expose Placement REST')
        try:
            # Provider traits arrived in placement 1.6 and the SDK
            # negotiates 1.0 by default, where that route 404s. Ask for the
            # exact microversion this validation needs instead of whatever
            # the client happens to default to.
            response = placement.get(
                path, params=params,
                headers={'OpenStack-API-Version': PLACEMENT_MICROVERSION})
        except Exception as exc:
            raise ScaleFailure(
                '{} failed: {}: {}'.format(
                    description, type(exc).__name__, exc)) from exc
        return response_json(response, description)

    def _placement_provider_inventory(self, nova_hosts):
        providers = {}
        required_traits = set(
            self.flavor_contract.get('required_traits', []))
        for host in sorted(nova_hosts):
            result = self._placement_get(
                '/resource_providers',
                'Placement provider lookup for {}'.format(host),
                params={'name': host})
            matches = [
                provider for provider in result.get(
                    'resource_providers', [])
                if isinstance(provider, dict) and provider.get('name') == host
            ]
            if len(matches) != 1 or not matches[0].get('uuid'):
                raise ScaleFailure(
                    'Placement must return exactly one provider named {}'
                    .format(host))
            provider_uuid = matches[0]['uuid']
            trait_result = self._placement_get(
                '/resource_providers/{}/traits'.format(provider_uuid),
                'Placement traits for {}'.format(host))
            traits = trait_result.get('traits')
            if not isinstance(traits, list):
                raise ScaleFailure(
                    'Placement did not return traits for {}'.format(host))
            missing_traits = sorted(required_traits - set(traits))
            if missing_traits:
                raise ScaleFailure(
                    'Placement provider {} is missing Flavor-required '
                    'traits {}'.format(host, missing_traits))
            usage_result = self._placement_get(
                '/resource_providers/{}/usages'.format(provider_uuid),
                'Placement usage for {}'.format(host))
            usages = usage_result.get('usages')
            if not isinstance(usages, dict):
                raise ScaleFailure(
                    'Placement did not return usages for {}'.format(host))
            inventory_result = self._placement_get(
                '/resource_providers/{}/inventories'.format(provider_uuid),
                'Placement inventories for {}'.format(host))
            inventories = inventory_result.get('inventories')
            if not isinstance(inventories, dict):
                raise ScaleFailure(
                    'Placement did not return inventories for {}'.format(
                        host))
            providers[host] = {
                'uuid': provider_uuid,
                'traits': sorted(traits),
                'usages': dict(usages),
                'inventories': dict(inventories),
            }
        return providers

    def _host_storage_snapshot(self):
        """Read host-local Incus state/log filesystem and inode usage."""
        paths = ('/var/lib/incus', '/var/log/incus')
        code = (
            'import json,os,subprocess\n'
            'result={}\n'
            'for path in ' + repr(paths) + ':\n'
            ' s=os.statvfs(path)\n'
            ' du=int(subprocess.check_output(['
            '"du","-x","-B1","-s",path],text=True).split()[0])\n'
            ' result[path]={"total_bytes":s.f_blocks*s.f_frsize,'
            '"available_bytes":s.f_bavail*s.f_frsize,'
            '"total_inodes":s.f_files,'
            '"available_inodes":s.f_favail,"du_bytes":du}\n'
            'print(json.dumps(result,sort_keys=True))')
        command = 'python3 -c {}'.format(shlex.quote(code))
        snapshots = {}
        for nova_host, ssh_target in self.args.incus_host:
            output = self.remote_output(ssh_target, command)
            try:
                value = json.loads(output)
            except json.JSONDecodeError as exc:
                raise ScaleFailure(
                    'host storage audit returned invalid JSON on {}'.format(
                        ssh_target)) from exc
            if set(value) != set(paths) or any(
                    not isinstance(stats, dict) for stats in value.values()):
                raise ScaleFailure(
                    'host storage audit returned incomplete data on {}'
                    .format(ssh_target))
            snapshots[nova_host] = value
        return snapshots

    def _validate_initial_host_storage(self, snapshots):
        evidence = {}
        for host, paths in snapshots.items():
            evidence[host] = {}
            for path, stats in paths.items():
                required = host_storage_threshold(
                    stats['total_bytes'],
                    self.args.host_initial_min_free_bytes,
                    self.args.host_initial_min_free_percent)
                inode_percent = (
                    100.0 * stats['available_inodes'] /
                    max(1, stats['total_inodes']))
                if (
                    stats['available_bytes'] < required or
                    inode_percent < self.args.host_initial_min_inode_percent
                ):
                    raise ScaleFailure(
                        '{}:{} lacks initial byte or inode headroom'.format(
                            host, path))
                evidence[host][path] = {
                    **stats,
                    'required_available_bytes': required,
                    'available_inode_percent': inode_percent,
                }
        return evidence

    def _checkpoint_host_storage(self, checkpoint):
        snapshots = self._host_storage_snapshot()
        baseline = (
            self.preflight_result.get('fleet', {})
            .get('host_storage_baseline', {}))
        if set(snapshots) != set(baseline):
            raise ScaleFailure(
                'host storage fleet changed after preflight')
        evidence = {}
        final_target = self.args.checkpoints[-1]
        for host, paths in snapshots.items():
            if set(paths) != set(baseline[host]):
                raise ScaleFailure(
                    'host storage paths changed on {}'.format(host))
            evidence[host] = {}
            for path, stats in paths.items():
                projection = projected_host_storage(
                    baseline[host][path], stats, checkpoint, final_target,
                    self.args.host_runtime_min_free_bytes,
                    self.args.host_runtime_min_free_percent)
                inode_percent = (
                    100.0 * stats['available_inodes'] /
                    max(1, stats['total_inodes']))
                if inode_percent < self.args.host_runtime_min_inode_percent:
                    raise ScaleFailure(
                        '{}:{} inode headroom fell below the runtime '
                        'threshold'.format(host, path))
                evidence[host][path] = {
                    **stats,
                    **projection,
                    'available_inode_percent': inode_percent,
                }
        return evidence

    def _fleet_preflight(self, initial=True):
        if not self.args.incus_host:
            return {}
        services = list(
            self.connection.compute.services(binary='nova-compute'))
        enabled = {}
        duplicate_hosts = set()
        for service in services:
            if str(service.status).lower() != 'enabled':
                continue
            if not service.host:
                raise ScaleFailure(
                    'Nova returned an enabled compute without a host')
            if service.host in enabled:
                duplicate_hosts.add(service.host)
            enabled[service.host] = str(service.state).lower()
        if duplicate_hosts:
            raise ScaleFailure(
                'Nova returned duplicate enabled compute services: {}'.format(
                    sorted(duplicate_hosts)))
        if not enabled:
            raise ScaleFailure(
                'Nova returned no enabled nova-compute services')
        mapped = {item[0] for item in self.args.incus_host}
        missing = sorted(mapped - set(enabled))
        if missing:
            raise ScaleFailure(
                'Incus host mappings are not enabled Nova computes: {}'
                .format(missing))
        if self.args.min_compute_hosts != len(mapped):
            raise ScaleFailure(
                '--min-compute-hosts must equal the {} mapped Incus computes'
                .format(len(mapped)))
        down = sorted(
            host for host in mapped if enabled[host] != 'up')
        if down:
            raise ScaleFailure(
                'mapped Incus nova-compute services are not up: {}'.format(
                    down))
        providers = self._placement_provider_inventory(mapped)
        result = {
            'incus_compute_hosts': sorted(mapped),
            'other_enabled_compute_hosts': sorted(set(enabled) - mapped),
            'count': len(mapped),
            'placement_providers': providers,
        }
        if initial:
            requested = self.preflight_result['runtime_contract'][
                'placement_resources']
            placement_capacity = placement_fleet_capacity(
                providers, requested)
            target = self.args.checkpoints[-1]
            if placement_capacity['fleet_slots'] < target:
                raise ScaleFailure(
                    'Placement exposes {} whole-instance slots across Incus '
                    'computes, fewer than the {} scale target'.format(
                        placement_capacity['fleet_slots'], target))
            result['placement_capacity'] = placement_capacity
            result['host_storage_baseline'] = (
                self._validate_initial_host_storage(
                    self._host_storage_snapshot()))
        return result

    def _run_inventory_command(self, label, command):
        if not command:
            raise ScaleFailure(
                '{} inventory command is required'.format(label))
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise ScaleFailure(
                '{} inventory command is invalid: {}'.format(
                    label, exc)) from exc
        if not argv:
            raise ScaleFailure(
                '{} inventory command is empty'.format(label))
        try:
            result = subprocess.run(
                argv,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.args.audit_command_timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ScaleFailure(
                '{} inventory command failed: {}'.format(label, exc)) \
                from exc
        if result.returncode:
            raise ScaleFailure(
                '{} inventory command exited {}: {}'.format(
                    label, result.returncode, result.stderr.strip()))
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ScaleFailure(
                '{} inventory command returned invalid JSON'.format(label)) \
                from exc
        return inventory_names(label, value)

    def _run_json_object_command(self, label, command):
        if not command:
            raise ScaleFailure(
                '{} command is required'.format(label))
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise ScaleFailure(
                '{} command is invalid: {}'.format(label, exc)) from exc
        if not argv:
            raise ScaleFailure('{} command is empty'.format(label))
        try:
            result = subprocess.run(
                argv,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.args.audit_command_timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ScaleFailure(
                '{} command failed: {}'.format(label, exc)) from exc
        if result.returncode:
            raise ScaleFailure(
                '{} command exited {}: {}'.format(
                    label, result.returncode, result.stderr.strip()))
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ScaleFailure(
                '{} command returned invalid JSON'.format(label)) from exc
        if not isinstance(value, dict):
            raise ScaleFailure(
                '{} command returned a non-object'.format(label))
        return value

    @staticmethod
    def _command_fingerprint(command):
        return hashlib.sha256(command.encode('utf-8')).hexdigest()

    def _idmap_inventory(self):
        command = self.args.idmap_inventory_command
        expected = self.inventory_command_fingerprints.get('idmap_keys')
        actual = self._command_fingerprint(command)
        if expected and actual != expected:
            raise ScaleFailure(
                'ID-map inventory helper does not match the run artifact')
        value = self._run_json_object_command(
            'ID-map etcd inventory', command)
        return normalize_idmap_inventory(value)

    def audit_idmap_inventory(self, instance_uuids, require_present=True):
        baseline = self.idmap_inventory_baseline
        if (not isinstance(baseline, dict) or
                not isinstance(baseline.get('entries'), dict) or
                'revision' not in baseline):
            raise ScaleFailure(
                'ID-map etcd baseline is missing from the run artifact')
        current = self._idmap_inventory()
        evidence = idmap_inventory_delta(
            baseline, current, instance_uuids)
        preexisting = {
            instance_uuid: keys
            for instance_uuid, keys in
            evidence['baseline_run_keys_by_instance'].items() if keys
        }
        if preexisting:
            raise ScaleFailure(
                'ID-map baseline already referenced this run: {}'.format(
                    dict(list(preexisting.items())[:10])))
        references = evidence['run_keys_by_instance']
        if require_present:
            missing = sorted(
                instance_uuid for instance_uuid in instance_uuids
                if not references.get(instance_uuid))
            if missing:
                raise ScaleFailure(
                    'ID-map etcd inventory has no key for {} run instance(s): '
                    '{}'.format(len(missing), missing[:10]))
        else:
            residual = {
                instance_uuid: keys
                for instance_uuid, keys in references.items() if keys
            }
            known_digests = {}
            for checkpoint in getattr(self, 'checkpoints', []):
                known_digests.update(
                    checkpoint.get('idmap_etcd', {}).get(
                        'run_key_digests', {}))
            soak_idmap = (
                getattr(self, 'soak_result', {})
                .get('backend_audit', {}).get('idmap_etcd', {}))
            known_digests.update(soak_idmap.get('run_key_digests', {}))
            unchanged_known = sorted(
                key for key, digest in known_digests.items()
                if key in current['entries'] and
                current['entries'][key]['digest'] == digest)
            if residual or unchanged_known:
                raise ScaleFailure(
                    'ID-map etcd cleanup left run keys: references={}, '
                    'unchanged_known={}'.format(
                        dict(list(residual.items())[:10]),
                        unchanged_known[:10]))
            evidence['unchanged_known_run_keys'] = []
        return evidence

    def _ceph_status(self, required_bytes=0):
        value = self._run_json_object_command(
            'Ceph status', self.args.ceph_status_command)
        return validate_ceph_status(value, required_bytes)

    def audit_ceph(self):
        baseline = self.preflight_result.get('ceph')
        if not isinstance(baseline, dict):
            raise ScaleFailure('Ceph baseline is missing from preflight')
        current = self._ceph_status()
        for key in ('fsid', 'pool'):
            if current[key] != baseline[key]:
                raise ScaleFailure(
                    'Ceph {} changed after preflight'.format(key))
        return current

    def _external_inventory(self):
        commands = {
            'rbd_images': self.args.rbd_inventory_command,
            'ovn_lsps': self.args.ovn_lsp_inventory_command,
            'ceph_status': self.args.ceph_status_command,
        }
        fingerprints = {
            label: self._command_fingerprint(command)
            for label, command in commands.items()
        }
        expected = {
            label: self.inventory_command_fingerprints.get(label)
            for label in commands
        }
        if any(expected.values()) and fingerprints != expected:
            raise ScaleFailure(
                'inventory helper commands do not match the run artifact')
        rbd_images = self._run_inventory_command(
            'RBD', commands['rbd_images'])
        invalid_rbd = [
            name for name in rbd_images
            if not name.startswith(
                ('container_', 'zombie_container_'))
        ]
        if invalid_rbd:
            raise ScaleFailure(
                'RBD inventory helper returned non-container images: {}'
                .format(invalid_rbd[:10]))
        return {
            'rbd_images': rbd_images,
            'ovn_lsps': self._run_inventory_command(
                'OVN LSP', commands['ovn_lsps']),
        }

    def preflight(self):
        nova_hosts = [item[0] for item in self.args.incus_host]
        ssh_targets = [item[1] for item in self.args.incus_host]
        if len(set(nova_hosts)) != len(nova_hosts):
            raise ScaleFailure('mapped Nova compute hosts must be unique')
        if len(set(ssh_targets)) != len(ssh_targets):
            raise ScaleFailure('Incus SSH targets must be unique')
        if (self.args.incus_host and
                len(self.args.incus_host) < self.args.min_compute_hosts):
            raise ScaleFailure(
                'fewer Incus SSH targets than --min-compute-hosts')

        servers, _latency = self._list_project_servers()
        collisions = sorted(
            server_id for server_id, server in servers.items()
            if self._owns_server(server))
        if collisions:
            raise ScaleFailure(
                'cleanup-token collision with existing servers: {}'.format(
                    collisions[:10]))
        self.preflight_result = self._quota_preflight()
        self.preflight_result[
            'network_capacity'] = self._network_capacity_preflight()
        self.preflight_result['flavor_contract'] = dict(
            self.flavor_contract)
        self.preflight_result['runtime_contract'] = self._runtime_contract()
        self.preflight_result['fleet'] = self._fleet_preflight()
        required_ceph_bytes = (
            self.args.checkpoints[-1] * int(self.flavor.disk) * GIB)
        self.preflight_result['ceph'] = self._ceph_status(
            required_ceph_bytes)
        self.inventory_command_fingerprints = {
            label: self._command_fingerprint(command)
            for label, command in {
                'rbd_images': self.args.rbd_inventory_command,
                'ovn_lsps': self.args.ovn_lsp_inventory_command,
                'ceph_status': self.args.ceph_status_command,
                'idmap_keys': self.args.idmap_inventory_command,
            }.items()
        }
        self.external_inventory_baseline = self._external_inventory()
        self.idmap_inventory_baseline = self._idmap_inventory()
        baseline_run_keys = {
            instance_uuid: keys
            for instance_uuid, keys in idmap_inventory_references(
                self.idmap_inventory_baseline, self.server_ids).items()
            if keys
        }
        if baseline_run_keys:
            raise ScaleFailure(
                'ID-map baseline unexpectedly references this run: {}'
                .format(dict(list(baseline_run_keys.items())[:10])))
        self.preflight_result['external_inventory_baseline_counts'] = {
            label: len(values)
            for label, values in self.external_inventory_baseline.items()
        }
        self.preflight_result['idmap_inventory_baseline'] = {
            'revision': self.idmap_inventory_baseline['revision'],
            'key_count': len(self.idmap_inventory_baseline['entries']),
        }
        self.telemetry_command_fingerprints = {
            label: self._command_fingerprint(command)
            for label, command in self.args.telemetry_command
        }
        self.preflight_result['telemetry'] = self.collect_telemetry(
            'preflight')
        self.save()

    def create_one(self, ordinal):
        if self._stop_event.is_set():
            raise concurrent.futures.CancelledError()
        submitted_epoch = time.time()
        started = time.monotonic()
        create_options = {}
        if getattr(self.args, 'pin_to_incus_hosts', False):
            hosts = sorted(host for host, _target in self.args.incus_host)
            host = hosts[(ordinal - 1) % len(hosts)]
            create_options['availability_zone'] = '{}:{}'.format(
                self.args.availability_zone, host)
        server = self.connection.compute.create_server(
            name='{}-{:04d}'.format(self.prefix, ordinal),
            image_id=self.image.id,
            flavor_id=self.flavor.id,
            networks=[{'uuid': self.network.id}],
            metadata={
                RUN_METADATA_KEY: self.run_id,
                CLEANUP_METADATA_KEY: self.cleanup_token,
                ORDINAL_METADATA_KEY: str(ordinal),
            },
            **create_options,
        )
        latency = time.monotonic() - started
        accepted_epoch = time.time()
        server_id = str(server.id)
        if not server_id:
            raise ScaleFailure('Nova create response did not include an ID')
        with self._state_lock:
            if server_id in self.server_ids:
                raise ScaleFailure(
                    'Nova returned duplicate server ID {}'.format(server_id))
            instance_name = server_value(
                server, 'instance_name', 'OS-EXT-SRV-ATTR:instance_name')
            if instance_name:
                if instance_name in self.instance_names.values():
                    raise ScaleFailure(
                        'Nova returned duplicate instance name {}'.format(
                            instance_name))
            self._journal_created_server(
                server_id, instance_name, submitted_epoch, accepted_epoch,
                latency)
            self.server_ids.append(server_id)
            if instance_name:
                self.instance_names[server_id] = instance_name
            self.create_latencies[server_id] = latency
            self.submitted_epoch[server_id] = submitted_epoch
            self.accepted_epoch[server_id] = accepted_epoch
        return server_id

    @staticmethod
    def _future_failure(future, ordinal):
        if future.cancelled():
            return None
        try:
            future.result()
        except concurrent.futures.CancelledError:
            return None
        except Exception as exc:
            return ordinal, '{}: {}'.format(type(exc).__name__, exc)
        return None

    def _create_range(self, start, target):
        print(
            'Creating servers {}..{} with concurrency {}'.format(
                start, target, self.args.concurrency),
            flush=True)
        failures = []
        ordinals = iter(range(start, target + 1))
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.args.concurrency)
        pending = {}

        def fill_pending():
            while (len(pending) < self.args.concurrency and
                   not self._stop_event.is_set()):
                try:
                    ordinal = next(ordinals)
                except StopIteration:
                    return
                future = executor.submit(self.create_one, ordinal)
                pending[future] = ordinal

        try:
            fill_pending()
            while pending:
                done, _not_done = concurrent.futures.wait(
                    pending, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    ordinal = pending.pop(future)
                    failure = self._future_failure(future, ordinal)
                    if failure is not None:
                        failures.append(failure)
                        self._stop_event.set()
                if failures or self._stop_event.is_set():
                    break
                fill_pending()
        finally:
            if failures or self._stop_event.is_set():
                for future in pending:
                    future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
            for future, ordinal in pending.items():
                failure = self._future_failure(future, ordinal)
                if failure is not None and failure not in failures:
                    failures.append(failure)

        if failures:
            failures.sort(key=lambda item: item[0])
            raise ScaleFailure(
                '{} create request(s) failed; first ordinal {}: {}'.format(
                    len(failures), failures[0][0], failures[0][1]))
        if self._stop_event.is_set():
            raise ScaleFailure('run interrupted')

    def create_until(self, target):
        start = len(self.server_ids) + 1
        if start > target:
            return
        wave_size = getattr(self.args, 'create_wave_size', None)
        if wave_size is None:
            self._create_range(start, target)
            return

        while start <= target:
            wave_target = min(target, start + wave_size - 1)
            self._create_range(start, wave_target)
            if wave_target < target:
                print(
                    'Waiting for creation wave through {}'.format(
                        wave_target),
                    flush=True)
                self.wait_active(wave_target)
            start = wave_target + 1

    def list_run_servers(self, server_ids):
        wanted = set(server_ids)
        servers, latency = self._list_project_servers()
        return {
            server_id: server for server_id, server in servers.items()
            if server_id in wanted
        }, latency

    def _record_instance_names(self, servers):
        updates = {}
        for server_id, server in servers.items():
            instance_name = server_value(
                server, 'instance_name', 'OS-EXT-SRV-ATTR:instance_name')
            if not instance_name:
                continue
            current = self.instance_names.get(server_id)
            if current is not None and current != instance_name:
                raise ScaleFailure(
                    'Nova instance name changed for {}'.format(server_id))
            if current is None:
                if (instance_name in self.instance_names.values() or
                        instance_name in updates.values()):
                    raise ScaleFailure(
                        'Nova returned duplicate instance name {}'.format(
                            instance_name))
                updates[server_id] = instance_name
        if updates:
            with self._state_lock:
                self.instance_names.update(updates)
                self._save_locked()
        return updates

    def wait_active(self, target):
        stage_ids = list(self.server_ids[:target])
        deadline = time.monotonic() + self.args.stage_timeout
        started = time.monotonic()
        last_summary = None
        list_latencies = []
        missing_counts = {server_id: 0 for server_id in stage_ids}
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                raise ScaleFailure('run interrupted')
            servers, latency = self.list_run_servers(stage_ids)
            self._record_instance_names(servers)
            list_latencies.append(latency)
            states = {}
            unexpected = {}
            for server_id in stage_ids:
                state = 'MISSING'
                if server_id in servers:
                    missing_counts[server_id] = 0
                    status = getattr(servers[server_id], 'status', None)
                    state = str(status).upper() if status else 'UNKNOWN'
                    if (state == 'ACTIVE' and
                            server_id not in self.active_epoch):
                        self.active_epoch[server_id] = time.time()
                    if state not in ALLOWED_BUILD_STATES:
                        unexpected[state] = unexpected.get(state, 0) + 1
                else:
                    missing_counts[server_id] += 1
                states[state] = states.get(state, 0) + 1

            if unexpected:
                raise ScaleFailure(
                    'unexpected server states at checkpoint {}: {}'.format(
                        target, unexpected))
            persistently_missing = sorted(
                server_id for server_id, count in missing_counts.items()
                if count >= ABSENCE_CONFIRMATIONS)
            if persistently_missing:
                raise ScaleFailure(
                    '{} server(s) were absent from two Nova inventories at '
                    'checkpoint {}: {}'.format(
                        len(persistently_missing), target,
                        persistently_missing[:10]))
            if states.get('ACTIVE') == target:
                self.save()
                return (
                    servers,
                    list_latencies,
                    time.monotonic() - started,
                )

            summary = json.dumps(states, sort_keys=True)
            if summary != last_summary:
                print(
                    'Checkpoint {} state: {}'.format(target, summary),
                    flush=True)
                last_summary = summary
            time.sleep(self.args.poll_interval)

        raise ScaleFailure(
            'checkpoint {} did not become ACTIVE within {} seconds'.format(
                target, self.args.stage_timeout))

    def list_run_ports(self, server_ids):
        wanted = set(server_ids)
        ports = {}
        started = time.monotonic()
        for batch in chunked(sorted(wanted), self.args.query_chunk_size):
            for port in self.connection.network.ports(device_id=batch):
                if port.device_id in wanted:
                    ports.setdefault(port.device_id, []).append(port)
        return ports, time.monotonic() - started

    def remote_output(self, host, command):
        try:
            result = subprocess.run(
                [
                    'ssh',
                    '-o', 'BatchMode=yes',
                    '-o', 'StrictHostKeyChecking=yes',
                    '-o', 'ConnectTimeout={}'.format(
                        self.args.ssh_connect_timeout),
                    host,
                    command,
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.args.ssh_command_timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ScaleFailure(
                'remote audit failed on {}: {}'.format(host, exc)) from exc
        if result.returncode:
            raise ScaleFailure(
                'remote audit failed on {}: {}'.format(
                    host, result.stderr.strip()))
        return result.stdout

    def remote_json(self, host, command):
        output = self.remote_output(host, command)
        try:
            value = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ScaleFailure(
                'remote audit returned invalid JSON on {}: {}'.format(
                    host, exc)) from exc
        if not isinstance(value, list):
            raise ScaleFailure(
                'remote audit returned non-list JSON on {}'.format(host))
        return value

    def remote_json_object(self, host, command):
        output = self.remote_output(host, command)
        try:
            value = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ScaleFailure(
                'remote audit returned invalid JSON on {}: {}'.format(
                    host, exc)) from exc
        if not isinstance(value, dict):
            raise ScaleFailure(
                'remote audit returned non-object JSON on {}'.format(host))
        return value

    @staticmethod
    def _host_telemetry_command():
        code = (
            'import json,os,time\n'
            'result={"collected_epoch":time.time(),"processes":{}}\n'
            'clock=os.sysconf(os.sysconf_names["SC_CLK_TCK"])\n'
            'page=os.sysconf("SC_PAGE_SIZE")\n'
            'for label in ("incusd","nova_compute"):\n'
            ' result["processes"][label]={"process_count":0,'
            '"cpu_seconds":0.0,"rss_bytes":0,"fd_count":0}\n'
            'for name in os.listdir("/proc"):\n'
            ' if not name.isdigit(): continue\n'
            ' if int(name)==os.getpid(): continue\n'
            ' root="/proc/"+name\n'
            ' try:\n'
            '  raw=open(root+"/cmdline","rb").read()\n'
            '  argv=[part.decode("utf-8","replace") for part in '
            'raw.split(b"\\0") if part]\n'
            '  comm=open(root+"/comm",encoding="utf-8").read().strip()\n'
            '  stat=open(root+"/stat",encoding="utf-8").read()\n'
            '  rest=stat[stat.rfind(")")+2:].split()\n'
            '  if rest[0]=="Z": continue\n'
            '  cpu=(int(rest[11])+int(rest[12]))/clock\n'
            '  rss=int(rest[21])*page\n'
            '  fds=len(os.listdir(root+"/fd"))\n'
            ' except (FileNotFoundError,PermissionError,ProcessLookupError,'
            'ValueError,IndexError): continue\n'
            ' executable=os.path.basename(argv[0]) if argv else ""\n'
            ' module=argv[argv.index("-m")+1] if "-m" in argv and '
            'argv.index("-m")+1<len(argv) else ""\n'
            ' matched=[]\n'
            ' if comm=="incusd" or executable=="incusd": '
            'matched.append("incusd")\n'
            ' if executable in ("nova-compute","nova-compute-incus") or '
            'module in ("nova.cmd.compute",'
            '"nova.virt.incus.cmd.compute"): matched.append("nova_compute")\n'
            ' for label in matched:\n'
            '   item=result["processes"][label]\n'
            '   item["process_count"]+=1; item["cpu_seconds"]+=cpu\n'
            '   item["rss_bytes"]+=rss; item["fd_count"]+=fds\n'
            'cpu=[int(value) for value in '
            'open("/proc/stat").readline().split()[1:]]\n'
            'result["host_cpu_ticks"]={"total":sum(cpu),'
            '"idle":sum(cpu[3:5])}\n'
            'mem={}\n'
            'for line in open("/proc/meminfo"):\n'
            ' key,value=line.split(":",1)\n'
            ' mem[key]=int(value.split()[0])*1024\n'
            'result["memory_total_bytes"]=mem.get("MemTotal",0)\n'
            'result["memory_available_bytes"]=mem.get("MemAvailable",0)\n'
            'result["load_average"]=[float(value) for value in '
            'open("/proc/loadavg").read().split()[:3]]\n'
            'print(json.dumps(result,sort_keys=True))')
        return 'python3 -c {}'.format(shlex.quote(code))

    def collect_telemetry(self, scope):
        snapshot = {
            'scope': scope,
            'collected_at': utc_now(),
            'collected_epoch': time.time(),
            'hosts': {},
            'hooks': {},
        }
        command = self._host_telemetry_command()
        for nova_host, ssh_target in self.args.incus_host:
            snapshot['hosts'][nova_host] = self.remote_json_object(
                ssh_target, command)
        for label, helper in self.args.telemetry_command:
            expected = self.telemetry_command_fingerprints.get(label)
            actual = self._command_fingerprint(helper)
            if expected and expected != actual:
                raise ScaleFailure(
                    'telemetry helper {} does not match the run artifact'
                    .format(label))
            snapshot['hooks'][label] = self._run_json_object_command(
                'telemetry {}'.format(label), helper)
        return snapshot

    def audit_incus(self, servers):
        if not self.args.incus_host:
            return {}

        expected = {}
        for server_id, server in servers.items():
            instance_name = server_value(
                server, 'instance_name', 'OS-EXT-SRV-ATTR:instance_name')
            compute_host = server_value(
                server, 'compute_host', 'OS-EXT-SRV-ATTR:host')
            if not instance_name:
                raise ScaleFailure(
                    'Nova did not expose OS-EXT-SRV-ATTR:instance_name')
            if not compute_host:
                raise ScaleFailure(
                    'Nova did not expose OS-EXT-SRV-ATTR:host')
            expected[server_id] = {
                'instance_name': instance_name,
                'compute_host': compute_host,
            }

        owners = {}
        profiles_by_host = {}
        inventory = self._incus_inventory()
        for nova_host, resources in inventory.items():
            profiles_by_host[nova_host] = {
                profile.get('name'): profile
                for profile in resources['profiles']
                if isinstance(profile, dict) and profile.get('name')
            }
            for instance in resources['instances']:
                if not isinstance(instance, dict):
                    raise ScaleFailure(
                        'Incus returned a non-object instance on {}'.format(
                            resources['ssh_target']))
                instance_uuid = (
                    instance.get('config', {}).get('user.openstack.uuid'))
                if instance_uuid in expected:
                    owners.setdefault(instance_uuid, []).append({
                        'nova_host': nova_host,
                        'ssh_target': resources['ssh_target'],
                        'name': instance.get('name'),
                        'profiles': instance.get('profiles', []),
                        'instance': instance,
                    })

        missing = sorted(set(expected) - set(owners))
        duplicates = {
            instance_uuid: entries
            for instance_uuid, entries in owners.items()
            if len(entries) != 1
        }
        wrong_names = {}
        wrong_hosts = {}
        duplicate_profiles = {}
        missing_profiles = {}
        runtime_errors = {}
        idmap_ranges = {}
        runtime_contract = self.preflight_result.get(
            'runtime_contract', {})
        for instance_uuid, entries in owners.items():
            if len(entries) != 1:
                continue
            owner = entries[0]
            expected_name = expected[instance_uuid]['instance_name']
            expected_host = expected[instance_uuid]['compute_host']
            if owner['name'] != expected_name:
                wrong_names[instance_uuid] = {
                    'expected': expected_name,
                    'actual': owner['name'],
                    'nova_host': owner['nova_host'],
                }
            if owner['nova_host'] != expected_host:
                wrong_hosts[instance_uuid] = {
                    'expected': expected_host,
                    'actual': owner['nova_host'],
                    'ssh_target': owner['ssh_target'],
                }
            profile = profiles_by_host[owner['nova_host']].get(expected_name)
            if profile is None or expected_name not in owner['profiles']:
                missing_profiles[instance_uuid] = {
                    'profile': expected_name,
                    'nova_host': owner['nova_host'],
                }
            profile_hosts = sorted(
                nova_host for nova_host, profiles in profiles_by_host.items()
                if expected_name in profiles)
            if len(profile_hosts) > 1:
                duplicate_profiles[instance_uuid] = profile_hosts
            instance = owner['instance']
            errors = []
            if instance.get('type') != runtime_contract.get('type'):
                errors.append(
                    'type={!r}'.format(instance.get('type')))
            if instance.get('status') != runtime_contract.get('status'):
                errors.append(
                    'status={!r}'.format(instance.get('status')))
            expanded_config = instance.get('expanded_config', {})
            if not isinstance(expanded_config, dict):
                errors.append('expanded_config is not an object')
                expanded_config = {}
            for key, expected_value in runtime_contract.get(
                    'config', {}).items():
                actual = expanded_config.get(key)
                if key.startswith('security.'):
                    actual = str(actual).lower()
                if actual != expected_value:
                    errors.append(
                        '{}={!r} expected {!r}'.format(
                            key, actual, expected_value))
            local_config = instance.get('config', {})
            label = '{}:{}'.format(owner['nova_host'], owner['name'])
            try:
                current_idmap = parse_instance_idmap(
                    local_config, 'volatile.idmap.current', label)
                if not current_idmap:
                    raise ScaleFailure(
                        '{} has no current isolated idmap'.format(label))
                next_idmap = parse_instance_idmap(
                    local_config, 'volatile.idmap.next', label)
                if next_idmap and next_idmap != current_idmap:
                    raise ScaleFailure(
                        '{} current and next idmaps differ'.format(label))
                try:
                    fixed_base = int(expanded_config[
                        'security.idmap.base'])
                    fixed_size = int(expanded_config[
                        'security.idmap.size'])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ScaleFailure(
                        '{} has no fixed allocator-owned idmap'.format(
                            label)) from exc
                if fixed_base < 1 or fixed_size < 1:
                    raise ScaleFailure(
                        '{} has an invalid fixed idmap'.format(label))
                for kind in ('uid', 'gid'):
                    host_ranges = [
                        (start, end, namespace_id, map_range)
                        for current_kind, start, end, namespace_id, map_range
                        in current_idmap if current_kind == kind
                    ]
                    if host_ranges != [(
                            fixed_base, fixed_base + fixed_size,
                            0, fixed_size)]:
                        raise ScaleFailure(
                            '{} {} map does not match fixed base/size'.format(
                                label, kind))
                for kind, start, end, namespace_id, map_range in (
                        current_idmap):
                    idmap_ranges.setdefault(
                        owner['nova_host'], []).append({
                            'kind': kind,
                            'start': start,
                            'end': end,
                            'namespace_id': namespace_id,
                            'map_range': map_range,
                            'instance_uuid': instance_uuid,
                            'instance_name': owner['name'],
                        })
            except ScaleFailure as exc:
                errors.append(str(exc))
            expanded_devices = instance.get('expanded_devices', {})
            root = (
                expanded_devices.get('root')
                if isinstance(expanded_devices, dict) else None)
            if not isinstance(root, dict):
                errors.append('expanded root device is missing')
            else:
                expected_root = {
                    'type': 'disk',
                    'path': '/',
                    'pool': runtime_contract.get('root_pool'),
                    'size': runtime_contract.get('root_size'),
                }
                for key, expected_value in expected_root.items():
                    if root.get(key) != expected_value:
                        errors.append(
                            'root.{}={!r} expected {!r}'.format(
                                key, root.get(key), expected_value))
            if errors:
                runtime_errors[instance_uuid] = {
                    'nova_host': owner['nova_host'],
                    'errors': errors,
                }
        idmap_overlaps = idmap_overlap_errors(idmap_ranges)
        if (missing or duplicates or wrong_names or wrong_hosts or
                duplicate_profiles or missing_profiles or runtime_errors or
                idmap_overlaps):
            raise ScaleFailure(
                'Incus ownership audit failed: missing={}, duplicates={}, '
                'wrong_names={}, wrong_hosts={}, duplicate_profiles={}, '
                'missing_profiles={}, runtime_errors={}, '
                'idmap_overlaps={}'.format(
                    missing[:10],
                    dict(list(duplicates.items())[:10]),
                    dict(list(wrong_names.items())[:10]),
                    dict(list(wrong_hosts.items())[:10]),
                    dict(list(duplicate_profiles.items())[:10]),
                    dict(list(missing_profiles.items())[:10]),
                    dict(list(runtime_errors.items())[:10]),
                    idmap_overlaps[:10]))
        return {
            'instance_owners': len(owners),
            'profiles': len(expected),
            'idmap_ranges': sum(
                len(ranges) for ranges in idmap_ranges.values()),
            'runtime_contract': runtime_contract,
        }

    def _incus_inventory(self):
        inventory = {}
        project = shlex.quote(self.args.incus_project)
        cli = getattr(
            self.args, 'incus_cli_command', DEFAULT_INCUS_CLI_COMMAND)
        command_instances = (
            '{} --project {} list --columns=n --format=json'.format(
                cli, project))
        command_profiles = (
            '{} --project {} profile list --format=json'.format(
                cli, project))
        for nova_host, ssh_target in self.args.incus_host:
            instances = self.remote_json(ssh_target, command_instances)
            profiles = self.remote_json(ssh_target, command_profiles)
            for kind, values in (
                    ('instance', instances), ('profile', profiles)):
                invalid = [
                    value for value in values
                    if (not isinstance(value, dict) or
                        not isinstance(value.get('name'), str) or
                        not value.get('name') or
                        not isinstance(value.get('config'), dict) or
                        (kind == 'instance' and
                         (not isinstance(value.get('profiles'), list) or
                          not isinstance(
                              value.get('expanded_config'), dict) or
                          not isinstance(
                              value.get('expanded_devices'), dict))))
                ]
                if invalid:
                    raise ScaleFailure(
                        'Incus returned {} {} object(s) without config on {}'
                        .format(len(invalid), kind, ssh_target))
            inventory[nova_host] = {
                'ssh_target': ssh_target,
                'instances': instances,
                'profiles': profiles,
            }
        return inventory

    def audit_cleanup_residuals(self, server_ids):
        server_ids = set(server_ids)
        if (not server_ids and
                not self.external_inventory_baseline):
            return {
                'no_resources_created': True,
                'neutron_ports': 0,
                'incus_instances': 0,
                'incus_profiles': 0,
                'placement_consumers': 0,
            }
        ports, _latency = self.list_run_ports(server_ids)
        residual_ports = {
            server_id: [port.id for port in server_ports]
            for server_id, server_ports in ports.items() if server_ports
        }

        residual_instances = {}
        residual_profiles = {}
        if self.args.incus_host:
            inventory = self._incus_inventory()
            recovered_names = {}
            for nova_host, resources in inventory.items():
                for instance in resources['instances']:
                    if not isinstance(instance, dict):
                        continue
                    instance_uuid = (
                        instance.get('config', {}).get('user.openstack.uuid'))
                    if instance_uuid in server_ids:
                        instance_name = instance.get('name')
                        if instance_name:
                            recovered_names[instance_uuid] = instance_name
                        residual_instances.setdefault(
                            instance_uuid, []).append({
                                'nova_host': nova_host,
                                'name': instance_name,
                            })
            name_updates = {}
            for server_id, instance_name in recovered_names.items():
                current = self.instance_names.get(server_id)
                if current is not None and current != instance_name:
                    raise ScaleFailure(
                        'Incus and Nova instance names disagree for {}'.format(
                            server_id))
                if current is None:
                    name_updates[server_id] = instance_name
            if name_updates:
                with self._state_lock:
                    self.instance_names.update(name_updates)
                    self._save_locked()
            missing_names = sorted(server_ids - set(self.instance_names))
            if missing_names:
                raise ScaleFailure(
                    'cannot prove Incus profile cleanup without Nova instance '
                    'names for {}'.format(missing_names[:10]))
            expected_profile_names = {
                instance_name: server_id
                for server_id, instance_name in self.instance_names.items()
                if server_id in server_ids
            }
            for nova_host, resources in inventory.items():
                for profile in resources['profiles']:
                    if not isinstance(profile, dict):
                        continue
                    profile_name = profile.get('name')
                    if profile_name in expected_profile_names:
                        profile_uuid = expected_profile_names[profile_name]
                        residual_profiles.setdefault(
                            profile_uuid, []).append({
                                'nova_host': nova_host,
                                'name': profile_name,
                            })
        if residual_ports or residual_instances or residual_profiles:
            raise ScaleFailure(
                'cleanup residual audit failed: ports={}, instances={}, '
                'profiles={}'.format(
                    dict(list(residual_ports.items())[:10]),
                    dict(list(residual_instances.items())[:10]),
                    dict(list(residual_profiles.items())[:10])))

        placement_consumers = {}
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.args.audit_concurrency) as executor:
            futures = {
                executor.submit(
                    self._placement_allocations,
                    server_id,
                    True): server_id
                for server_id in sorted(server_ids)
            }
            for future in concurrent.futures.as_completed(futures):
                server_id = futures[future]
                allocations = future.result()
                if allocations:
                    placement_consumers[server_id] = allocations

        current_provider_usages = self._placement_usage_snapshot()
        baseline_providers = (
            self.preflight_result.get('fleet', {})
            .get('placement_providers', {}))
        baseline_provider_usages = {
            host: provider.get('usages')
            for host, provider in baseline_providers.items()
        }
        provider_usage_mismatch = {
            host: {
                'baseline': baseline_provider_usages.get(host),
                'current': current_provider_usages.get(host),
            }
            for host in sorted(set(baseline_provider_usages) |
                               set(current_provider_usages))
            if baseline_provider_usages.get(host) !=
            current_provider_usages.get(host)
        }

        current_external = self._external_inventory()
        external_mismatch = {
            label: {
                'added': sorted(
                    set(current_external.get(label, [])) -
                    set(self.external_inventory_baseline.get(label, []))),
                'removed': sorted(
                    set(self.external_inventory_baseline.get(label, [])) -
                    set(current_external.get(label, []))),
            }
            for label in ('rbd_images', 'ovn_lsps')
            if set(current_external.get(label, [])) !=
            set(self.external_inventory_baseline.get(label, []))
        }

        if (placement_consumers or provider_usage_mismatch or
                external_mismatch):
            raise ScaleFailure(
                'cleanup residual audit failed: placement_consumers={}, '
                'provider_usages={}, external_inventory={}'.format(
                    dict(list(placement_consumers.items())[:10]),
                    provider_usage_mismatch,
                    external_mismatch))
        idmap = self.audit_idmap_inventory(
            server_ids, require_present=False)
        return {
            'neutron_ports': 0,
            'incus_instances': 0,
            'incus_profiles': 0,
            'placement_consumers': 0,
            'placement_provider_usages': current_provider_usages,
            'idmap_etcd': idmap,
            'rbd_images': {
                'baseline_count': len(
                    self.external_inventory_baseline['rbd_images']),
                'residual_count': 0,
            },
            'ovn_lsps': {
                'baseline_count': len(
                    self.external_inventory_baseline['ovn_lsps']),
                'residual_count': 0,
            },
        }

    def _placement_allocations(self, server_id, allow_missing=False):
        try:
            result = self._placement_get(
                '/allocations/{}'.format(server_id),
                'Placement allocations for {}'.format(server_id))
        except ScaleFailure as exc:
            cause = exc.__cause__
            if allow_missing and getattr(cause, 'status_code', None) == 404:
                return {}
            raise
        allocations = result.get('allocations')
        if not isinstance(allocations, dict):
            raise ScaleFailure(
                'Placement allocations for {} are not an object'.format(
                    server_id))
        return allocations

    def _placement_usage_snapshot(self):
        providers = (
            self.preflight_result.get('fleet', {})
            .get('placement_providers', {}))
        if not providers:
            raise ScaleFailure(
                'Placement provider baseline is missing from preflight')
        result = {}
        for host, provider in providers.items():
            provider_uuid = provider.get('uuid')
            usage_result = self._placement_get(
                '/resource_providers/{}/usages'.format(provider_uuid),
                'Placement usage for {}'.format(host))
            usages = usage_result.get('usages')
            if not isinstance(usages, dict):
                raise ScaleFailure(
                    'Placement did not return usages for {}'.format(host))
            result[host] = dict(usages)
        return result

    def audit_placement(self, server_ids):
        providers = (
            self.preflight_result.get('fleet', {})
            .get('placement_providers', {}))
        provider_uuids = {
            provider['uuid'] for provider in providers.values()
        }
        expected_resources = self.preflight_result.get(
            'runtime_contract', {}).get('placement_resources', {})
        failures = {}

        def audit_one(server_id):
            allocations = self._placement_allocations(server_id)
            if not allocations:
                return server_id, 'consumer has no allocations'
            outside = sorted(set(allocations) - provider_uuids)
            if outside:
                return (
                    server_id,
                    'allocated on non-Incus providers {}'.format(outside),
                )
            totals = {}
            for allocation in allocations.values():
                resources = (
                    allocation.get('resources')
                    if isinstance(allocation, dict) else None)
                if not isinstance(resources, dict):
                    return server_id, 'allocation resources are invalid'
                for resource_class, amount in resources.items():
                    totals[resource_class] = (
                        totals.get(resource_class, 0) + amount)
            wrong = {
                resource_class: {
                    'actual': totals.get(resource_class),
                    'expected': amount,
                }
                for resource_class, amount in expected_resources.items()
                if totals.get(resource_class) != amount
            }
            if wrong:
                return server_id, 'resource mismatch {}'.format(wrong)
            return server_id, None

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.args.audit_concurrency) as executor:
            futures = [
                executor.submit(audit_one, server_id)
                for server_id in sorted(server_ids)
            ]
            for future in concurrent.futures.as_completed(futures):
                server_id, error = future.result()
                if error:
                    failures[server_id] = error
        if failures:
            raise ScaleFailure(
                'Placement consumer audit failed for {} server(s): {}'
                .format(
                    len(failures),
                    dict(list(sorted(failures.items()))[:10])))
        current_usages = self._placement_usage_snapshot()
        baseline_totals = {}
        current_totals = {}
        for host, provider in providers.items():
            for resource_class, amount in provider.get(
                    'usages', {}).items():
                baseline_totals[resource_class] = (
                    baseline_totals.get(resource_class, 0) + amount)
            for resource_class, amount in current_usages.get(
                    host, {}).items():
                current_totals[resource_class] = (
                    current_totals.get(resource_class, 0) + amount)
        usage_delta = {
            resource_class: (
                current_totals.get(resource_class, 0) -
                baseline_totals.get(resource_class, 0))
            for resource_class in set(baseline_totals) | set(current_totals)
        }
        wrong_delta = {
            resource_class: {
                'actual': usage_delta.get(resource_class, 0),
                'expected': amount * len(server_ids),
            }
            for resource_class, amount in expected_resources.items()
            if usage_delta.get(resource_class, 0) != amount * len(server_ids)
        }
        if wrong_delta:
            raise ScaleFailure(
                'Placement provider usage delta does not match run '
                'consumers: {}'.format(wrong_delta))
        return {
            'consumer_count': len(server_ids),
            'provider_usages': current_usages,
            'provider_usage_delta': usage_delta,
        }

    def audit_external_inventory(self, ports):
        current = self._external_inventory()
        baseline = self.external_inventory_baseline
        evidence = {}
        for label in ('rbd_images', 'ovn_lsps'):
            if label not in baseline:
                raise ScaleFailure(
                    '{} baseline is missing'.format(label))
            before = set(baseline[label])
            now = set(current[label])
            removed = sorted(before - now)
            if removed:
                raise ScaleFailure(
                    '{} lost baseline resources during the run: {}'.format(
                        label, removed[:10]))
            evidence[label] = {
                'baseline_count': len(before),
                'current_count': len(now),
                'added': sorted(now - before),
            }

        expected_port_ids = {
            str(port.id)
            for server_ports in ports.values()
            for port in server_ports
        }
        missing_lsps = sorted(
            expected_port_ids - set(current['ovn_lsps']))
        if missing_lsps:
            raise ScaleFailure(
                'OVN LSP inventory is missing {} run port(s): {}'.format(
                    len(missing_lsps), missing_lsps[:10]))
        added_lsps = (
            set(current['ovn_lsps']) - set(baseline['ovn_lsps']))
        unexpected_lsps = sorted(added_lsps - expected_port_ids)
        if unexpected_lsps:
            raise ScaleFailure(
                'OVN LSP inventory gained resources outside this run: {}'
                .format(unexpected_lsps[:10]))
        missing_names = sorted(set(ports) - set(self.instance_names))
        if missing_names:
            raise ScaleFailure(
                'cannot derive RBD names without Nova instance names for {}'
                .format(missing_names[:10]))
        project_prefix = (
            '' if self.args.incus_project == 'default'
            else '{}_'.format(self.args.incus_project))
        expected_rbd_images = {
            'container_{}{}'.format(
                project_prefix, self.instance_names[server_id])
            for server_id in ports
        }
        missing_rbd_images = sorted(
            expected_rbd_images - set(current['rbd_images']))
        if missing_rbd_images:
            raise ScaleFailure(
                'RBD inventory is missing {} exact run root image(s): {}'
                .format(
                    len(missing_rbd_images), missing_rbd_images[:10]))
        added_rbd_images = (
            set(current['rbd_images']) - set(baseline['rbd_images']))
        unexpected_rbd_images = sorted(
            added_rbd_images - expected_rbd_images)
        if unexpected_rbd_images:
            raise ScaleFailure(
                'RBD inventory gained resources outside this run: {}'
                .format(unexpected_rbd_images[:10]))
        evidence['expected_ovn_lsp_count'] = len(expected_port_ids)
        evidence['expected_rbd_images'] = sorted(expected_rbd_images)
        return evidence

    def audit_checkpoint(
            self, target, servers, list_latencies, active_seconds):
        audit_started = time.monotonic()
        audit_seconds = {}

        def timed(label, callback):
            started = time.monotonic()
            try:
                return callback()
            finally:
                audit_seconds[label] = time.monotonic() - started

        stage_ids = set(self.server_ids[:target])
        stage_servers = {
            server_id: server for server_id, server in servers.items()
            if server_id in stage_ids
        }
        hosts = {}
        for server in stage_servers.values():
            host = server_value(
                server, 'compute_host', 'OS-EXT-SRV-ATTR:host')
            if not host:
                raise ScaleFailure(
                    'Nova did not expose OS-EXT-SRV-ATTR:host')
            hosts[host] = hosts.get(host, 0) + 1
        if len(hosts) < self.args.min_compute_hosts:
            raise ScaleFailure(
                'checkpoint {} used {} compute host(s), fewer than required {}'
                .format(target, len(hosts), self.args.min_compute_hosts))
        eligible_hosts = [
            item[0] for item in self.args.incus_host
        ]
        per_compute_target = getattr(
            self.args, 'per_compute_target_by_total', {}).get(target)
        if per_compute_target is None:
            distribution = host_distribution_result(
                hosts, eligible_hosts, self.args.max_host_skew_percent)
        else:
            distribution = per_compute_distribution_result(
                hosts, eligible_hosts, per_compute_target,
                self.args.min_per_compute_percent,
                self.args.max_host_skew_percent)
        if not distribution['passed']:
            raise ScaleFailure(
                'checkpoint {} host distribution failed: {}'.format(
                    target, distribution))

        ports, port_list_latency = timed(
            'neutron_port_inventory',
            lambda: self.list_run_ports(stage_ids))
        bad_ports = neutron_binding_errors(stage_servers, ports)
        if bad_ports:
            raise ScaleFailure(
                'Neutron port audit failed for {} server(s): {}'.format(
                    len(bad_ports),
                    dict(list(bad_ports.items())[:10])))

        incus = timed(
            'incus_inventory', lambda: self.audit_incus(stage_servers))
        fleet = timed(
            'fleet_services_and_placement',
            lambda: self._fleet_preflight(initial=False))
        host_storage = timed(
            'host_storage', lambda: self._checkpoint_host_storage(target))
        placement = timed(
            'placement_consumers', lambda: self.audit_placement(stage_ids))
        ceph = timed('ceph_status', self.audit_ceph)
        external_inventory = timed(
            'rbd_and_ovn_inventory',
            lambda: self.audit_external_inventory(ports))
        idmap_inventory = timed(
            'idmap_etcd_inventory',
            lambda: self.audit_idmap_inventory(stage_ids))
        telemetry = timed(
            'telemetry',
            lambda: self.collect_telemetry(
                'checkpoint-{}'.format(target)))
        create_to_active = []
        for server_id in stage_ids:
            submitted = self.submitted_epoch.get(server_id)
            active = self.active_epoch.get(server_id)
            if submitted is None or active is None or active < submitted:
                raise ScaleFailure(
                    'missing valid create-to-ACTIVE timing for {}'.format(
                        server_id))
            create_to_active.append(active - submitted)
        previous_target = (
            self.checkpoints[-1]['target'] if self.checkpoints else 0)
        incremental_ids = set(
            self.server_ids[previous_target:target])
        if len(incremental_ids) != target - previous_target:
            raise ScaleFailure(
                'checkpoint incremental server set is incomplete')
        try:
            first_submitted = min(
                self.submitted_epoch[server_id]
                for server_id in incremental_ids)
            last_accepted = max(
                self.accepted_epoch[server_id]
                for server_id in incremental_ids)
            last_active = max(
                self.active_epoch[server_id]
                for server_id in incremental_ids)
            cumulative_first_submitted = min(
                self.submitted_epoch[server_id] for server_id in stage_ids)
            cumulative_last_accepted = max(
                self.accepted_epoch[server_id] for server_id in stage_ids)
            cumulative_last_active = max(
                self.active_epoch[server_id] for server_id in stage_ids)
        except KeyError as exc:
            raise ScaleFailure(
                'missing throughput timing for {}'.format(exc)) from exc
        submit_seconds = max(last_accepted - first_submitted, 1e-9)
        all_active_seconds = max(last_active - first_submitted, 1e-9)
        incremental_count = len(incremental_ids)
        throughput_observations = {
            'submit_per_second': incremental_count / submit_seconds,
            'all_active_per_second': (
                incremental_count / all_active_seconds),
        }
        throughput_slo = minimum_slo_result(
            throughput_observations,
            {
                'submit_per_second': self.args.min_submit_throughput,
                'all_active_per_second': self.args.min_active_throughput,
            })
        create_latencies = [
            self.create_latencies[server_id] for server_id in stage_ids
            if server_id in self.create_latencies
        ]
        if len(create_latencies) != target:
            raise ScaleFailure(
                'missing create API timing for {} server(s)'.format(
                    target - len(create_latencies)))
        business_observations = {
            'create_api_p95': percentile(
                create_latencies, 0.95),
            'create_to_active_p95': percentile(create_to_active, 0.95),
            'create_to_active_p99': percentile(create_to_active, 0.99),
        }
        query_observations = {
            'nova_list_p95': percentile(list_latencies, 0.95),
            'neutron_list_seconds': port_list_latency,
        }
        business_thresholds = {
            'create_api_p95': getattr(
                self.args, 'max_create_api_p95', None),
            'create_to_active_p95': getattr(
                self.args, 'max_active_p95', None),
            'create_to_active_p99': getattr(
                self.args, 'max_active_p99', None),
        }
        query_thresholds = {
            'nova_list_p95': getattr(
                self.args, 'max_nova_list_p95', None),
            'neutron_list_seconds': getattr(
                self.args, 'max_neutron_list_seconds', None),
        }
        performance_slo = performance_slo_result(
            business_observations, business_thresholds)
        query_slo = performance_slo_result(
            query_observations, query_thresholds)
        audit_seconds['total'] = time.monotonic() - audit_started
        checkpoint = {
            'target': target,
            'target_per_compute': per_compute_target,
            'completed_at': utc_now(),
            'active_seconds': active_seconds,
            'hosts': hosts,
            'host_distribution': distribution,
            'nova_list_seconds': {
                'p50': percentile(list_latencies, 0.50),
                'p95': percentile(list_latencies, 0.95),
                'max': max(list_latencies),
            },
            'neutron_list_seconds': port_list_latency,
            'create_seconds': {
                'p50': percentile(create_latencies, 0.50),
                'p95': percentile(create_latencies, 0.95),
                'p99': percentile(create_latencies, 0.99),
                'max': max(create_latencies),
            },
            'create_to_active_seconds': {
                'p50': percentile(create_to_active, 0.50),
                'p95': business_observations['create_to_active_p95'],
                'p99': business_observations['create_to_active_p99'],
                'max': max(create_to_active),
            },
            'performance_slo': performance_slo,
            'control_plane_query_slo': query_slo,
            'audit_seconds': audit_seconds,
            'throughput': {
                'scope': 'incremental_checkpoint_stage',
                'server_count': incremental_count,
                'submit_window_seconds': submit_seconds,
                'all_active_window_seconds': all_active_seconds,
                'slo': throughput_slo,
                'cumulative': {
                    'server_count': target,
                    'submit_per_second': target / max(
                        cumulative_last_accepted -
                        cumulative_first_submitted, 1e-9),
                    'all_active_per_second': target / max(
                        cumulative_last_active -
                        cumulative_first_submitted, 1e-9),
                },
            },
            'fleet': fleet,
            'host_storage': host_storage,
            'incus': incus,
            'placement': placement,
            'ceph': ceph,
            'external_inventory': external_inventory,
            'idmap_etcd': idmap_inventory,
            'telemetry': telemetry,
        }
        self.checkpoints.append(checkpoint)
        self.save()
        print(json.dumps(checkpoint, indent=2, sort_keys=True), flush=True)
        if not performance_slo['passed']:
            raise ScaleFailure(
                'checkpoint {} performance SLO violation: {}'.format(
                    target, performance_slo['violations']))
        if not query_slo['passed']:
            raise ScaleFailure(
                'checkpoint {} control-plane query SLO violation: {}'
                .format(target, query_slo['violations']))
        if not throughput_slo['passed']:
            raise ScaleFailure(
                'checkpoint {} throughput SLO violation: {}'.format(
                    target, throughput_slo['violations']))

    def idle_soak(self, target):
        contract = validate_soak_window(
            self.args.idle_soak_seconds,
            self.args.periodic_task_interval_seconds,
            self.args.minimum_soak_periodic_cycles)
        if not contract['enabled']:
            self.soak_result = {
                'attempted': False,
                'completed': False,
                **contract,
            }
            self.save()
            return

        stage_ids = set(self.server_ids[:target])
        if len(stage_ids) != target:
            raise ScaleFailure('idle soak server set is incomplete')
        started = time.monotonic()
        deadline = started + self.args.idle_soak_seconds
        samples = []
        inventory_query_seconds = []
        self.soak_result = {
            'attempted': True,
            'completed': False,
            'started_at': utc_now(),
            **contract,
            'samples': samples,
        }
        self.save()

        final_servers = {}
        while True:
            if self._stop_event.is_set():
                raise ScaleFailure('run interrupted during idle soak')
            query_started = time.monotonic()
            servers, _latency = self._list_project_servers()
            inventory_query_seconds.append(time.monotonic() - query_started)
            owned = {
                server_id: server for server_id, server in servers.items()
                if self._owns_server(server)
            }
            if set(owned) != stage_ids:
                raise ScaleFailure(
                    'idle soak run inventory changed: missing={}, added={}'
                    .format(
                        sorted(stage_ids - set(owned))[:10],
                        sorted(set(owned) - stage_ids)[:10]))
            states = {
                server_id: str(getattr(server, 'status', '')).upper()
                for server_id, server in owned.items()
            }
            not_active = {
                server_id: state for server_id, state in states.items()
                if state != 'ACTIVE'
            }
            if not_active:
                raise ScaleFailure(
                    'idle soak found non-ACTIVE run servers: {}'.format(
                        dict(list(sorted(not_active.items()))[:10])))
            final_servers = owned
            sample = self.collect_telemetry(
                'idle-soak-{}'.format(len(samples)))
            samples.append(sample)
            self.soak_result['samples'] = list(samples)
            self.soak_result['elapsed_seconds'] = time.monotonic() - started
            self.save()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(self.args.telemetry_interval, remaining))

        audit_seconds = {}

        def timed(label, callback):
            audit_started = time.monotonic()
            try:
                return callback()
            finally:
                audit_seconds[label] = time.monotonic() - audit_started

        ports, port_list_seconds = timed(
            'neutron_port_inventory',
            lambda: self.list_run_ports(stage_ids))
        bad_ports = neutron_binding_errors(final_servers, ports)
        if bad_ports:
            raise ScaleFailure(
                'idle soak Neutron port audit failed: {}'.format(
                    dict(list(bad_ports.items())[:10])))
        hosts = {}
        for server in final_servers.values():
            host = server_value(
                server, 'compute_host', 'OS-EXT-SRV-ATTR:host')
            if not host:
                raise ScaleFailure(
                    'Nova did not expose a compute host during idle soak')
            hosts[host] = hosts.get(host, 0) + 1
        eligible_hosts = [item[0] for item in self.args.incus_host]
        per_compute_target = getattr(
            self.args, 'per_compute_target_by_total', {}).get(target)
        if per_compute_target is None:
            distribution = host_distribution_result(
                hosts, eligible_hosts, self.args.max_host_skew_percent)
        else:
            distribution = per_compute_distribution_result(
                hosts, eligible_hosts, per_compute_target,
                self.args.min_per_compute_percent,
                self.args.max_host_skew_percent)
        if not distribution['passed']:
            raise ScaleFailure(
                'idle soak host distribution failed: {}'.format(
                    distribution))
        backend = {
            'fleet': timed(
                'fleet_services_and_placement',
                lambda: self._fleet_preflight(initial=False)),
            'host_storage': timed(
                'host_storage',
                lambda: self._checkpoint_host_storage(target)),
            'incus': timed(
                'incus_inventory',
                lambda: self.audit_incus(final_servers)),
            'placement': timed(
                'placement_consumers',
                lambda: self.audit_placement(stage_ids)),
            'ceph': timed('ceph_status', self.audit_ceph),
            'external_inventory': timed(
                'rbd_and_ovn_inventory',
                lambda: self.audit_external_inventory(ports)),
            'idmap_etcd': timed(
                'idmap_etcd_inventory',
                lambda: self.audit_idmap_inventory(stage_ids)),
        }
        elapsed = time.monotonic() - started
        covered_cycles = int(
            elapsed // self.args.periodic_task_interval_seconds)
        if covered_cycles < self.args.minimum_soak_periodic_cycles:
            raise ScaleFailure(
                'idle soak covered only {} periodic intervals'.format(
                    covered_cycles))
        self.soak_result = {
            'attempted': True,
            'completed': True,
            'started_at': self.soak_result['started_at'],
            'completed_at': utc_now(),
            **contract,
            'actual_seconds': elapsed,
            'covered_periodic_cycles': covered_cycles,
            'server_count': target,
            'hosts': hosts,
            'host_distribution': distribution,
            'inventory_query_seconds': {
                'p50': percentile(inventory_query_seconds, 0.50),
                'p95': percentile(inventory_query_seconds, 0.95),
                'max': max(inventory_query_seconds),
            },
            'neutron_list_seconds': port_list_seconds,
            'telemetry_samples': samples,
            'telemetry_summary': summarize_telemetry_samples(samples),
            'backend_audit': backend,
            'audit_seconds': {
                **audit_seconds,
                'total': sum(audit_seconds.values()),
            },
        }
        self.save()
        print(json.dumps(
            {'idle_soak': self.soak_result},
            indent=2, sort_keys=True), flush=True)

    def delete_one(self, server_id):
        while True:
            with self._state_lock:
                attempts = self.delete_attempts.get(server_id, 0)
                if attempts >= self.args.delete_request_attempts:
                    return (
                        server_id,
                        'delete retry limit {} exhausted'.format(attempts),
                    )
                self.delete_attempts[server_id] = attempts + 1
                attempt = attempts + 1

            try:
                server = self.connection.compute.get_server(server_id)
            except Exception as exc:
                if getattr(exc, 'status_code', None) == 404:
                    return None
                return server_id, 'ownership lookup failed: {}: {}'.format(
                    type(exc).__name__, exc)
            if server is None:
                return (
                    server_id,
                    'ownership lookup returned no resource or 404 error',
                )
            if not self._owns_server(server):
                return (
                    server_id,
                    'exact run and cleanup metadata no longer match',
                )

            started = time.monotonic()
            try:
                self.connection.compute.delete_server(
                    server_id, ignore_missing=True)
                return None
            except Exception as exc:
                if (not is_transient_delete_error(exc) or
                        attempt >= self.args.delete_request_attempts):
                    return server_id, '{}: {}'.format(
                        type(exc).__name__, exc)
                time.sleep(
                    self.args.delete_retry_backoff *
                    min(2 ** (attempt - 1), 16))
            finally:
                with self._state_lock:
                    self.delete_latencies.append(
                        time.monotonic() - started)

    def _delete_many(self, server_ids):
        failures = []
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.args.delete_concurrency) as executor:
            futures = [
                executor.submit(self.delete_one, server_id)
                for server_id in sorted(server_ids)
            ]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result is not None:
                    failures.append(result)
        return failures

    def _record_discovered_ids(self, server_ids):
        new_ids = sorted(set(server_ids) - set(self.server_ids))
        if not new_ids:
            return []
        with self._state_lock:
            self.server_ids.extend(new_ids)
            self._save_locked()
        return new_ids

    def _verify_exact_absence(self, server_ids):
        present = []
        failures = []

        def get_one(server_id):
            try:
                server = self.connection.compute.get_server(server_id)
            except Exception as exc:
                if getattr(exc, 'status_code', None) == 404:
                    return None
                return server_id, '{}: {}'.format(type(exc).__name__, exc)
            if server is None:
                return server_id, 'get_server returned no resource or error'
            if not self._owns_server(server):
                return (
                    server_id,
                    'exact run and cleanup metadata no longer match',
                )
            return server_id, None

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.args.delete_concurrency) as executor:
            futures = [
                executor.submit(get_one, server_id)
                for server_id in sorted(server_ids)
            ]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result is None:
                    continue
                server_id, error = result
                if error is None:
                    present.append(server_id)
                else:
                    failures.append((server_id, error))
        return sorted(present), sorted(failures)

    def cleanup(self):
        cleanup_started = time.monotonic()
        cleanup_audit_seconds = 0.0
        if not hasattr(self, 'delete_latencies'):
            self.delete_latencies = []
        self.cleanup_result = {
            'attempted': True,
            'completed': False,
            'started_at': utc_now(),
        }
        self.save()

        servers, _latency = self._list_project_servers()
        owned = {
            server_id: server for server_id, server in servers.items()
            if self._owns_server(server)
        }
        recorded_present = set(self.server_ids) & set(servers)
        ownership_mismatch = sorted(recorded_present - set(owned))
        if ownership_mismatch:
            raise ScaleFailure(
                'refusing to delete recorded IDs without the exact run and '
                'cleanup metadata: {}'.format(ownership_mismatch[:10]))

        self._record_discovered_ids(owned)
        self._record_instance_names(owned)
        known_ids = set(self.server_ids)
        present_owned = known_ids & set(owned)
        if present_owned:
            print(
                'Deleting {} servers owned by run {}'.format(
                    len(present_owned), self.run_id),
                flush=True)
        delete_failures = self._delete_many(present_owned)

        deadline = time.monotonic() + self.args.cleanup_timeout
        absent_counts = {server_id: 0 for server_id in known_ids}
        last_list_error = None
        last_residual_error = None
        quiet_scans = 0
        last_owned_seen = time.monotonic()
        while time.monotonic() < deadline:
            try:
                servers, _latency = self._list_project_servers()
                last_list_error = None
            except Exception as exc:
                last_list_error = '{}: {}'.format(type(exc).__name__, exc)
                time.sleep(self.args.poll_interval)
                continue

            late_owned = {
                server_id for server_id, server in servers.items()
                if self._owns_server(server) and server_id not in known_ids
            }
            if late_owned:
                quiet_scans = 0
                last_owned_seen = time.monotonic()
                self._record_discovered_ids(late_owned)
                self._record_instance_names({
                    server_id: servers[server_id]
                    for server_id in late_owned
                })
                known_ids.update(late_owned)
                absent_counts.update(
                    {server_id: 0 for server_id in late_owned})
                delete_failures.extend(self._delete_many(late_owned))
            elif any(self._owns_server(server)
                     for server in servers.values()):
                quiet_scans = 0
                last_owned_seen = time.monotonic()
            else:
                quiet_scans += 1

            known_present = known_ids & set(servers)
            mismatched = sorted(
                server_id for server_id in known_present
                if not self._owns_server(servers[server_id]))
            if mismatched:
                raise ScaleFailure(
                    'refusing to retry deletion after metadata ownership '
                    'changed for {}'.format(mismatched[:10]))
            retryable = {
                server_id for server_id in known_present
                if self.delete_attempts.get(server_id, 0) <
                self.args.delete_request_attempts
            }
            if retryable:
                delete_failures.extend(self._delete_many(retryable))

            existing_ids = set(servers)
            for server_id in known_ids:
                if server_id in existing_ids:
                    absent_counts[server_id] = 0
                else:
                    absent_counts[server_id] += 1
            remaining = {
                server_id for server_id, confirmations
                in absent_counts.items()
                if confirmations < ABSENCE_CONFIRMATIONS
            }
            quiet_seconds = time.monotonic() - last_owned_seen
            if (not remaining and
                    quiet_scans >= ABSENCE_CONFIRMATIONS and
                    quiet_seconds >= self.args.cleanup_settle_time):
                audit_started = time.monotonic()
                exact_present, exact_failures = self._verify_exact_absence(
                    known_ids)
                cleanup_audit_seconds += time.monotonic() - audit_started
                if exact_failures:
                    raise ScaleFailure(
                        'exact cleanup verification failed for {} server(s): '
                        '{}'.format(
                            len(exact_failures), exact_failures[:10]))
                if exact_present:
                    for server_id in exact_present:
                        absent_counts[server_id] = 0
                    quiet_scans = 0
                    last_owned_seen = time.monotonic()
                    print(
                        'Exact verification found {} server(s) still present'
                        .format(len(exact_present)),
                        flush=True)
                    time.sleep(self.args.poll_interval)
                    continue
                try:
                    audit_started = time.monotonic()
                    residual_audit = self.audit_cleanup_residuals(known_ids)
                    cleanup_audit_seconds += (
                        time.monotonic() - audit_started)
                    last_residual_error = None
                except ScaleFailure as exc:
                    cleanup_audit_seconds += (
                        time.monotonic() - audit_started)
                    last_residual_error = str(exc)
                    print(
                        'Waiting for backend cleanup: {}'.format(
                            last_residual_error),
                        flush=True)
                    time.sleep(self.args.poll_interval)
                    continue
                cleanup_seconds = time.monotonic() - cleanup_started
                business_cleanup_seconds = max(
                    0.0, cleanup_seconds - cleanup_audit_seconds)
                delete_p95 = percentile(self.delete_latencies, 0.95)
                max_delete_p95 = getattr(
                    self.args, 'max_delete_api_p95', None)
                max_cleanup = getattr(
                    self.args, 'max_cleanup_seconds', None)
                performance_slo = performance_slo_result(
                    {
                        'delete_api_p95': delete_p95,
                        'cleanup_seconds': business_cleanup_seconds,
                    },
                    {
                        'delete_api_p95': max_delete_p95,
                        'cleanup_seconds': max_cleanup,
                    })
                self.cleanup_result = {
                    'attempted': True,
                    'completed': True,
                    'started_at': self.cleanup_result['started_at'],
                    'completed_at': utc_now(),
                    'server_count': len(known_ids),
                    'delete_request_failures': delete_failures,
                    'absence_confirmations': ABSENCE_CONFIRMATIONS,
                    'settle_seconds': self.args.cleanup_settle_time,
                    'exact_get_confirmed': len(known_ids),
                    'residual_audit': residual_audit,
                    'delete_api_seconds': {
                        'p50': percentile(self.delete_latencies, 0.50),
                        'p95': delete_p95,
                        'p99': percentile(self.delete_latencies, 0.99),
                        'max': max(self.delete_latencies, default=0.0),
                    },
                    'cleanup_seconds': cleanup_seconds,
                    'business_cleanup_seconds': business_cleanup_seconds,
                    'audit_seconds': cleanup_audit_seconds,
                    'performance_slo': performance_slo,
                }
                self.save()
                if not performance_slo['passed']:
                    raise ScaleFailure(
                        'cleanup performance SLO violation: {}'.format(
                            performance_slo['violations']))
                return
            if remaining:
                message = (
                    'Waiting for {} server deletion confirmation(s)'.format(
                        len(remaining)))
            else:
                message = 'Waiting for clean inventory confirmation'
            print(message, flush=True)
            time.sleep(self.args.poll_interval)

        remaining = sorted(
            server_id for server_id, confirmations in absent_counts.items()
            if confirmations < ABSENCE_CONFIRMATIONS)
        detail = ''
        if last_list_error:
            detail = '; last list error={}'.format(last_list_error)
        if delete_failures:
            detail += '; delete request failures={}'.format(
                delete_failures[:10])
        if last_residual_error:
            detail += '; last residual error={}'.format(
                last_residual_error)
        raise ScaleFailure(
            'cleanup did not reach a settled exact-absence state: '
            'remaining={}, quiet_scans={}, required_quiet_seconds={}{}'.format(
                remaining[:10], quiet_scans, self.args.cleanup_settle_time,
                detail))

    def run(self):
        self.status = 'running'
        self.save()
        self.preflight()
        for checkpoint in self.args.checkpoints:
            if self._stop_event.is_set():
                raise ScaleFailure('run interrupted')
            stage_started = time.monotonic()
            self.create_until(checkpoint)
            servers, list_latencies, active_seconds = self.wait_active(
                checkpoint)
            if self.args.create_wave_size is not None:
                active_seconds = time.monotonic() - stage_started
            self.audit_checkpoint(
                checkpoint, servers, list_latencies, active_seconds)
        self.idle_soak(self.args.checkpoints[-1])
        self.status = 'validated'
        self.save()

    def close(self):
        self.connection.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cloud', default=None)
    parser.add_argument('--image')
    parser.add_argument('--flavor')
    parser.add_argument('--network')
    parser.add_argument(
        '--checkpoints', type=parse_checkpoints,
        help='fleet-wide cumulative checkpoints (default: 100,500,1000)')
    parser.add_argument(
        '--per-compute-checkpoints', type=parse_checkpoints,
        help=(
            'cumulative target per mapped Incus compute; for three hosts '
            '100,500,1000 becomes fleet totals 300,1500,3000'))
    parser.add_argument(
        '--pin-to-incus-hosts', action='store_true',
        help=(
            'round-robin creates across the mapped Incus hosts using an '
            'explicit Nova availability-zone host target; requires '
            '--per-compute-checkpoints'))
    parser.add_argument(
        '--availability-zone', default='nova',
        help='Nova availability zone used with --pin-to-incus-hosts')
    parser.add_argument('--concurrency', type=positive_int, default=16)
    parser.add_argument(
        '--create-wave-size', type=positive_int,
        help=(
            'maximum fleet-wide create requests submitted before waiting for '
            'that wave to become ACTIVE; separates capacity validation from '
            'scheduler-allocation burst testing'))
    parser.add_argument(
        '--delete-concurrency', type=positive_int, default=16)
    parser.add_argument('--stage-timeout', type=positive_int, default=7200)
    parser.add_argument('--cleanup-timeout', type=positive_int, default=7200)
    parser.add_argument(
        '--cleanup-settle-time', type=positive_float, default=30.0)
    parser.add_argument('--poll-interval', type=positive_float, default=10.0)
    parser.add_argument('--query-chunk-size', type=positive_int, default=100)
    parser.add_argument('--audit-concurrency', type=positive_int, default=16)
    parser.add_argument('--max-create-api-p95', type=positive_float)
    parser.add_argument('--max-active-p95', type=positive_float)
    parser.add_argument('--max-active-p99', type=positive_float)
    parser.add_argument('--max-nova-list-p95', type=positive_float)
    parser.add_argument('--max-neutron-list-seconds', type=positive_float)
    parser.add_argument('--max-delete-api-p95', type=positive_float)
    parser.add_argument('--max-cleanup-seconds', type=positive_float)
    parser.add_argument('--min-submit-throughput', type=positive_float)
    parser.add_argument('--min-active-throughput', type=positive_float)
    parser.add_argument(
        '--max-host-skew-percent', type=positive_float, default=20.0)
    parser.add_argument(
        '--min-per-compute-percent', type=positive_float, default=90.0,
        help=(
            'minimum share of each explicit per-compute checkpoint required '
            'on every mapped host'))
    parser.add_argument('--min-compute-hosts', type=positive_int)
    parser.add_argument(
        '--expected-root-pool',
        help='exact Incus storage-pool name required on every root device')
    parser.add_argument(
        '--expected-process-limit', type=positive_int,
        help=(
            'exact limits.processes value; when omitted the Flavor must '
            'provide incus:process_limit'))
    parser.add_argument(
        '--rbd-inventory-command',
        help=(
            'argv-only helper command returning a JSON list of all '
            'container_ and zombie_container_ root image names in the '
            'audited Incus RBD pool'))
    parser.add_argument(
        '--ovn-lsp-inventory-command',
        help=(
            'argv-only helper command returning a JSON list of all OVN '
            'logical switch port names'))
    parser.add_argument(
        '--ceph-status-command',
        help=(
            'argv-only helper returning Ceph health, cluster/pool identity, '
            'capacity, quota and full-ratio JSON evidence'))
    parser.add_argument(
        '--idmap-inventory-command',
        help=(
            'argv-only helper returning {"revision":...,"entries":['
            '{"key":...,"value":...}]} for the complete configured ID-map '
            'etcd namespace'))
    parser.add_argument(
        '--telemetry-command', action='append', default=[],
        type=labeled_command, metavar='LABEL=COMMAND',
        help=(
            'optional argv-only JSON-object telemetry helper; repeat for '
            'Nova, Incus, etcd, or site metrics'))
    parser.add_argument(
        '--audit-command-timeout', type=positive_int, default=120)
    parser.add_argument(
        '--idle-soak-seconds', type=nonnegative_float, default=0.0,
        help='idle validation window; use 900-1200 for release evidence')
    parser.add_argument(
        '--telemetry-interval', type=positive_float, default=60.0)
    parser.add_argument(
        '--periodic-task-interval-seconds', type=positive_float,
        default=60.0,
        help='longest Nova/driver periodic interval that the soak must cover')
    parser.add_argument(
        '--minimum-soak-periodic-cycles', type=positive_int, default=3)
    parser.add_argument(
        '--delete-request-attempts', type=positive_int, default=10)
    parser.add_argument(
        '--delete-retry-backoff', type=positive_float, default=1.0)
    parser.add_argument('--name-prefix', default='incus-scale')
    parser.add_argument('--run-id', type=uuid_value)
    parser.add_argument('--artifact')
    parser.add_argument(
        '--cleanup-artifact',
        help=(
            'only clean exact resources recorded/discovered for this '
            'artifact'))
    parser.add_argument(
        '--incus-host', action='append', default=[],
        type=incus_host_mapping, metavar='NOVA_HOST=SSH_TARGET',
        help='map a Nova host to its Incus SSH target; repeat for every node')
    parser.add_argument('--incus-project')
    parser.add_argument(
        '--incus-cli-command',
        help=(
            'remote shell command prefix that invokes the Incus CLI; '
            'defaults to the legacy Podman deployment command'))
    parser.add_argument(
        '--ssh-connect-timeout', type=positive_int, default=10)
    parser.add_argument(
        '--ssh-command-timeout', type=positive_int, default=120)
    parser.add_argument(
        '--host-initial-min-free-bytes', type=positive_int,
        default=4 * GIB)
    parser.add_argument(
        '--host-initial-min-free-percent', type=positive_float,
        default=30.0)
    parser.add_argument(
        '--host-initial-min-inode-percent', type=positive_float,
        default=30.0)
    parser.add_argument(
        '--host-runtime-min-free-bytes', type=positive_int,
        default=2 * GIB)
    parser.add_argument(
        '--host-runtime-min-free-percent', type=positive_float,
        default=20.0)
    parser.add_argument(
        '--host-runtime-min-inode-percent', type=positive_float,
        default=20.0)
    parser.add_argument('--keep', action='store_true')
    args = parser.parse_args(argv)

    if args.cleanup_artifact:
        if args.keep:
            parser.error('--keep cannot be used with --cleanup-artifact')
        if args.artifact:
            parser.error('--artifact cannot be used with --cleanup-artifact')
        if args.per_compute_checkpoints is not None:
            parser.error(
                '--per-compute-checkpoints cannot be used with cleanup-only')
    else:
        missing = [
            option for option in ('image', 'flavor', 'network')
            if not getattr(args, option)
        ]
        if missing:
            parser.error(
                'the following arguments are required: {}'.format(
                    ', '.join('--{}'.format(option) for option in missing)))
        if args.incus_project is None:
            args.incus_project = 'nova'
        if args.incus_cli_command is None:
            args.incus_cli_command = DEFAULT_INCUS_CLI_COMMAND
        for option in (
                'expected_root_pool',
                'rbd_inventory_command',
                'ovn_lsp_inventory_command',
                'ceph_status_command',
                'idmap_inventory_command'):
            if not getattr(args, option):
                parser.error(
                    '--{} is required for fail-closed scale evidence'.format(
                        option.replace('_', '-')))
    if args.cleanup_artifact:
        for option in (
                'rbd_inventory_command',
                'ovn_lsp_inventory_command',
                'ceph_status_command',
                'idmap_inventory_command'):
            if not getattr(args, option):
                parser.error(
                    '--{} is required for fail-closed cleanup evidence'
                    .format(option.replace('_', '-')))
    if not args.name_prefix:
        parser.error('--name-prefix cannot be empty')
    if args.pin_to_incus_hosts:
        if args.per_compute_checkpoints is None:
            parser.error(
                '--pin-to-incus-hosts requires --per-compute-checkpoints')
        if not args.incus_host:
            parser.error('--pin-to-incus-hosts requires --incus-host')
        if not args.availability_zone or ':' in args.availability_zone:
            parser.error(
                '--availability-zone must be a non-empty zone name without '
                "':'")
    if not args.cleanup_artifact and not args.incus_project:
        parser.error('--incus-project cannot be empty')
    if args.min_per_compute_percent > 100:
        parser.error('--min-per-compute-percent cannot exceed 100')
    if len({label for label, _command in args.telemetry_command}) != len(
            args.telemetry_command):
        parser.error('--telemetry-command labels must be unique')
    if args.min_compute_hosts is None:
        args.min_compute_hosts = len(args.incus_host) or 1
    try:
        args.checkpoints, args.per_compute_target_by_total = (
            resolve_checkpoints(
                args.checkpoints, args.per_compute_checkpoints,
                len(args.incus_host)))
        validate_soak_window(
            args.idle_soak_seconds,
            args.periodic_task_interval_seconds,
            args.minimum_soak_periodic_cycles)
    except ScaleFailure as exc:
        parser.error(str(exc))
    if args.cleanup_settle_time >= args.cleanup_timeout:
        parser.error(
            '--cleanup-settle-time must be less than --cleanup-timeout')
    if args.poll_interval >= min(
            args.stage_timeout, args.cleanup_timeout):
        parser.error(
            '--poll-interval must be less than stage and cleanup timeouts')
    for option in (
            'host_initial_min_free_percent',
            'host_initial_min_inode_percent',
            'host_runtime_min_free_percent',
            'host_runtime_min_inode_percent'):
        if getattr(args, option) > 100:
            parser.error(
                '--{} cannot exceed 100'.format(option.replace('_', '-')))
    return args


def main(argv=None):
    args = parse_args(argv)
    run = None
    failure = None
    cleanup_only = bool(args.cleanup_artifact)
    try:
        if cleanup_only:
            run = ScaleRun.from_cleanup_artifact(args)
        else:
            run = ScaleRun(args)

        def stop(_signum, _frame):
            run._stop_event.set()
            raise KeyboardInterrupt

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)

        if cleanup_only:
            run.cleanup()
        else:
            run.run()
    except BaseException as exc:
        failure = exc
        print(
            'FAIL: {}: {}'.format(type(exc).__name__, exc),
            file=sys.stderr, flush=True)
    finally:
        if run is not None:
            if not cleanup_only and not args.keep:
                try:
                    run.cleanup()
                except Exception as cleanup_exc:
                    print(
                        'CLEANUP FAIL: {}: {}'.format(
                            type(cleanup_exc).__name__, cleanup_exc),
                        file=sys.stderr, flush=True)
                    if failure is None:
                        failure = cleanup_exc
                    elif run.failure is None:
                        run.failure = '{}; cleanup: {}'.format(
                            failure, cleanup_exc)
            elif not cleanup_only:
                run.cleanup_result = {
                    'attempted': False,
                    'completed': False,
                    'kept_by_request': True,
                }

            if failure is not None:
                run.status = 'failed'
                if run.failure is None:
                    run.failure = '{}: {}'.format(
                        type(failure).__name__, failure)
            elif cleanup_only:
                run.status = 'cleanup-complete'
            elif args.keep:
                run.status = 'validated-resources-kept'
            else:
                run.status = 'passed'
            run.ended_at = utc_now()
            try:
                run.save()
            except Exception as save_exc:
                print(
                    'ARTIFACT FAIL: {}: {}'.format(
                        type(save_exc).__name__, save_exc),
                    file=sys.stderr, flush=True)
                if failure is None:
                    failure = save_exc
            try:
                run.close()
            except Exception as close_exc:
                print(
                    'CONNECTION CLOSE FAIL: {}: {}'.format(
                        type(close_exc).__name__, close_exc),
                    file=sys.stderr, flush=True)
                if failure is None:
                    failure = close_exc
                    run.status = 'failed'
                    run.failure = '{}: {}'.format(
                        type(close_exc).__name__, close_exc)
                    try:
                        run.save()
                    except Exception:
                        pass

    if failure is not None:
        return 1
    if cleanup_only:
        print(
            'PASS cleanup run={} artifact={}'.format(
                run.run_id, run.artifact),
            flush=True)
    else:
        print(
            'PASS run={} checkpoints={} artifact={}'.format(
                run.run_id, args.checkpoints, run.artifact),
            flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
