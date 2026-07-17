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

from oslo_config import cfg


incus_group = cfg.OptGroup(
    "incus",
    title="Incus compute driver options",
)

incus_opts = [
    cfg.StrOpt(
        "endpoint",
        default="/var/lib/incus/unix.socket",
        help="Incus Unix socket path or HTTPS API endpoint.",
    ),
    cfg.StrOpt(
        "project",
        default="default",
        help="Host-local Incus project managed by nova-compute.",
    ),
    cfg.StrOpt(
        "root_dir",
        default="/var/lib/incus",
        help="Incus state directory used for host-local filesystem checks.",
    ),
    cfg.StrOpt(
        "storage_pool",
        default=None,
        help="Incus storage pool used for instance root filesystems.",
    ),
    cfg.StrOpt(
        "boot_from_volume_storage_pool",
        default=None,
        help=(
            "Incus cephext storage pool used to claim Cinder RBD root "
            "volumes. It must reference the same Ceph pool as Cinder."
        ),
    ),
    cfg.IntOpt(
        "minimum_root_disk_gb",
        default=1,
        min=1,
        help="Minimum enforced instance rootfs size when root_gb is zero.",
    ),
    cfg.IntOpt(
        "default_process_limit",
        default=1024,
        min=1,
        help=(
            "Default maximum number of processes allowed in a system "
            "container when its flavor omits incus:process_limit."
        ),
    ),
    cfg.IntOpt(
        "maximum_process_limit",
        default=65536,
        min=1,
        help=(
            "Operator ceiling for the incus:process_limit flavor extra "
            "spec. The default process limit must not exceed this value."
        ),
    ),
    cfg.BoolOpt(
        "allow_instance_swap",
        default=False,
        help=(
            "Permit a non-zero Nova Flavor swap value to become the "
            "container's additional cgroup swap limit. The compute host "
            "must provide and capacity-plan real swap."
        ),
    ),
    cfg.BoolOpt(
        "volume_use_multipath",
        default=False,
        help="Use multipath-capable os-brick connectors for Cinder volumes.",
    ),
    cfg.BoolOpt(
        "volume_enforce_multipath",
        default=False,
        help=(
            "Reject Cinder volume attachment when multipath is requested "
            "but unavailable. Requires volume_use_multipath."
        ),
    ),
    cfg.IntOpt(
        "num_volume_scan_tries",
        default=3,
        min=1,
        help="Number of os-brick device discovery attempts.",
    ),
    cfg.IntOpt(
        "request_timeout",
        default=300,
        min=1,
        help=(
            "Timeout in seconds for Incus API requests. Image publish and "
            "export operations can take several minutes for large rootfs "
            "volumes."
        ),
    ),
    cfg.StrOpt(
        "tls_cert",
        default=None,
        help="Client certificate for an HTTPS Incus endpoint.",
    ),
    cfg.StrOpt(
        "tls_key",
        default=None,
        help="Client private key for an HTTPS Incus endpoint.",
        secret=True,
    ),
    cfg.StrOpt(
        "tls_ca",
        default=None,
        help="CA bundle used to verify an HTTPS Incus endpoint.",
    ),
    cfg.BoolOpt(
        "allow_live_migration",
        default=False,
        deprecated_for_removal=True,
        help=(
            "Deprecated compatibility option. Live migration is rejected; "
            "cold migration is a separate experimental capability."
        ),
    ),
    cfg.BoolOpt(
        "allow_cold_migration",
        default=False,
        help=(
            "Enable experimental Incus cold migration. Cinder BFV sources "
            "perform an authenticated destination readiness check before "
            "shutdown and use shared-Ceph handover; image-backed roots use "
            "Incus data transfer. Enable only after the BFV fault-injection "
            "release tests pass on every production compute pair."
        ),
    ),
    cfg.BoolOpt(
        "allow_bfv_evacuate",
        default=False,
        help=(
            "Allow Nova evacuation only for instances whose root disk is a "
            "Cinder RBD volume claimed through a cephext storage pool. The "
            "operator must provide external fencing that proves the source "
            "compute cannot access the volume before evacuation starts."
        ),
    ),
    cfg.StrOpt(
        "migration_address",
        default=None,
        help=(
            "Externally reachable HTTPS Incus API origin used by destination "
            "computes to pull migration data, for example "
            "https://192.0.2.10:8443."
        ),
    ),
    cfg.IntOpt(
        "migration_port",
        default=8443,
        min=1,
        max=65535,
        help="TCP port used by the destination Incus migration endpoint.",
    ),
    cfg.IntOpt(
        "migration_preflight_timeout",
        default=5,
        min=1,
        help=(
            "Seconds to wait for the destination Incus endpoint before "
            "stopping a BFV source instance."
        ),
    ),
    cfg.StrOpt(
        "migration_preflight_tls_cert",
        default=None,
        help=(
            "Client certificate for the restricted Incus identity used to "
            "verify a BFV migration destination before source shutdown."
        ),
    ),
    cfg.StrOpt(
        "migration_preflight_tls_key",
        default=None,
        secret=True,
        help="Private key for migration_preflight_tls_cert.",
    ),
    cfg.StrOpt(
        "migration_preflight_tls_ca",
        default=None,
        help=(
            "CA bundle used to verify remote Incus migration endpoints. "
            "Every destination certificate must contain its migration "
            "hostname or address in subjectAltName."
        ),
    ),
    cfg.StrOpt(
        "migration_preflight_project",
        default="nova-preflight",
        help=(
            "Restricted Incus project containing the destination BFV "
            "readiness contract."
        ),
    ),
    cfg.DictOpt(
        "migration_preflight_server_names",
        default={},
        help=(
            "Optional destination-address to TLS server-name mapping for "
            "Incus certificates that contain DNS SANs but not migration IP "
            "SANs. Every mapped name must resolve to the intended address."
        ),
    ),
    cfg.DictOpt(
        "migration_preflight_tls_ca_by_server",
        default={},
        help=(
            "Optional destination-address to server certificate or CA path "
            "mapping. Use this to pin different self-signed Incus server "
            "certificates per compute node. Entries override "
            "migration_preflight_tls_ca."
        ),
    ),
    cfg.IntOpt(
        "migration_finish_retries",
        default=3,
        min=1,
        help=(
            "Attempts for destination VIF wiring, data-volume attachment, "
            "and instance start during cold migration."
        ),
    ),
    cfg.IntOpt(
        "migration_finish_retry_interval",
        default=2,
        min=0,
        help="Seconds between destination cold-migration recovery attempts.",
    ),
    cfg.BoolOpt(
        "migration_auto_recovery",
        default=False,
        help=(
            "Automatically retry a stopped BFV migration target explicitly "
            "marked by the driver as requiring post-claim recovery."
        ),
    ),
    cfg.IntOpt(
        "migration_recovery_interval",
        default=60,
        min=10,
        help="Seconds between post-claim BFV target recovery scans.",
    ),
]


def register_opts(conf=cfg.CONF):
    conf.register_group(incus_group)
    conf.register_opts(incus_opts, group=incus_group)


def list_opts():
    return {incus_group: incus_opts}
