# Project Development Context

## Resume Source Of Truth

- Do not derive the backlog from an old planning section or capability audit.
  Cross-check `doc/source/support_matrix/capabilities.json`, `TEST_STATUS.md`,
  current E2E scripts, and recent Git history before declaring work missing.
  `doc/source/capability_matrix_historical.md` is retained only as an early
  source audit. A release gate that has already passed is recurring validation,
  not unfinished implementation.

## Local Upstream Source Trees

Use the following local repositories as the authoritative, current API
baselines when developing and testing this project:

- Nova: `C:\MyProjects\OpenSource\openstack\nova`
- Neutron: `C:\MyProjects\OpenSource\openstack-neutron`
- neutron-lib: `C:\MyProjects\OpenSource\openstack-neutron-lib`
- os-vif: `C:\MyProjects\OpenSource\os-vif`
- ovsdbapp: `C:\MyProjects\OpenSource\ovsdbapp`
- Open vSwitch: `C:\MyProjects\OpenSource\ovs`
- OVN: `C:\MyProjects\IaasProjects\OpenStack\ovn`
- Placement: `C:\MyProjects\OpenSource\placement`
- Cinder: `C:\MyProjects\OpenSource\cinder`
- os-brick: `C:\MyProjects\OpenSource\openstack\os-brick`
- Glance: `C:\MyProjects\OpenSource\glance`
- OpenStack requirements: `C:\MyProjects\OpenSource\openstack\requirements`
- Tempest: `C:\MyProjects\OpenSource\openstack\tempest`
- DevStack: `C:\MyProjects\OpenSource\openstack\devstack`
- Incus Python SDK: `C:\MyProjects\IaasProjects\Incus\incus-python-sdk`
- Incus server: `C:\MyProjects\IaasProjects\Incus\incus`

The former ovsdbapp checkout at
`C:\MyProjects\IaasProjects\OpenStack\openstack-ovsdbapp` was deleted because
it was outdated. Do not reference or recreate that path; use
`C:\MyProjects\OpenSource\ovsdbapp` exclusively.

Do not infer modern Nova or Neutron interfaces from the legacy `nova-lxd`
code in this repository. Compare driver method signatures, objects, Placement
integration, os-vif handling, Neutron port binding, OVS/OVSDB behavior,
storage attachment, image handling, dependency constraints, configuration
conventions, and tests against the local upstream trees above before
implementing changes.

## Architecture Direction

- Develop and debug against the upstream DevStack framework first. In the
  DevStack environment, `nova-compute`, Incus, OVS, OVN, and related agents
  run as native host services. Container images are a later packaging concern
  and must not define the driver architecture.
- Each compute node runs an independent, non-clustered Incus daemon.
- One `nova-compute` service manages its local Incus daemon.
- Nova Scheduler and Placement own cross-node placement and resource claims.
- Neutron ML2/OVN owns tenant networking, IPAM, security groups, OVS ports,
  OVN logical resources, and cross-node Geneve connectivity.
- Incus supplies system-container lifecycle and attaches the container-side
  interface to the host veth prepared for Neutron.
- Incus must not independently manage the same tenant OVN logical networks.
- Incus-managed Ceph RBD storage pools are the intended production rootfs
  backend. Incus 7.2 rejects an external host block device as an instance root
  disk (``Root disk entry may not have a source property set``), so a
  Cinder/os-brick device must not be forced over ``/`` with ``raw.lxc`` or a
  host bind mount. Cinder remains authoritative for separately attached data
  volumes; Nova remains authoritative for instance lifecycle, placement, and
  migration orchestration. Local Incus rootfs pools are development-only.
- Image-backed roots still use Incus data transfer during cold migration and
  are development-only. Cinder BFV roots use the fork's ``cephext`` claim and
  handover protocol: source and destination transfer exclusive ownership of
  the same RBD without copying rootfs data. Spawn/destroy, forward confirm,
  reverse revert, retained-target hard-reboot recovery, and destination-TCP
  preflight failure have passed real multi-node OpenStack tests. Production
  enablement still requires systematic fault injection after each ownership
  transition plus automatic Nova/Cinder/Neutron reconciliation.
- OpenStack tenancy is authoritative in Keystone, Nova, Placement, Neutron,
  Glance, and Cinder. Incus is a host-local compute backend controlled only by
  `nova-compute`; tenants must never receive Incus API access.
- Tenant containers must use unprivileged isolated ID maps, explicit
  CPU/memory/process/rootfs limits, and no Incus, Podman, Ceph, OVN, or host API
  access. Restricted Incus projects may be added as defense in depth, but are
  not the source of OpenStack tenant identity or a prerequisite for Nova
  multi-tenancy.
- Incus 7.x `unix-block` devices have no read-only property. Reject Cinder
  `access_mode=ro` rather than relying on device-node mode bits against
  container root. Encrypted volumes and multiattach also remain unsupported.
- Root disk QoS maps only independent read/write IOPS or bytes/s limits. Reject
  `total_*`, burst, and same-direction bytes+IOPS combinations rather than
  approximating their semantics. A BFV root uses either Flavor disk QoS or
  Cinder front-end QoS, never both. Data-volume QoS requires the Incus fork's
  `unix_block_limits` extension and maps to `limits.read`/`limits.write` on a
  `unix-block`; otherwise reject it. Do not work around missing
  support with driver-generated `raw.lxc` cgroup entries. The generic Incus
  mechanism lives in `internal/server/device/unix_block_limits.go` and is
  offered upstream as `lxc/incus#3648`. (Validation evidence: see
  `TEST_STATUS.md`.)
- Restrict container-side Cinder targets to Nova data-disk names under
  `/dev` (`vd*`, `sd*`, or `xvd*`) and reject duplicate volume IDs or occupied
  target paths before invoking os-brick. Keep os-brick source validation
  separate so legitimate direct connector paths such as `/dev/dm-0` and
  `/dev/rbd0` remain usable.
- Rescue/unrescue are intentionally unsupported. Do not restore the legacy
  host-rootfs bind mount; a future rescue design must attach the retained
  Incus-managed root volume through storage-pool-native APIs and work with the
  production Ceph RBD rootfs backend.
- Reject unsupported asynchronous suspend/resume/rescue/unrescue operations in
  ``IncusComputeManager`` before resource or power side effects. Revert the
  task state, preserve the prior VM state, and record an explicit Nova
  ``Instance*Failure`` event. Driver exceptions cannot turn an already-cast
  asynchronous API request into a synchronous HTTP 4xx; that requires an
  upstream Nova API capability gate.
- Failed-host evacuation is an operator-gated BFV-only capability. Keep
  ``[incus] allow_bfv_evacuate`` disabled unless an external STONITH or power
  fencing system proves the source host cannot access Ceph before Nova starts
  evacuation. Nova's service-down test is not fencing. The destination has no
  host-local Incus record before spawn, so shared Ceph must not be represented
  by returning true from ``instance_on_disk``. Local/image-backed pet roots
  must always reject evacuation because their data is unavailable.
- Every Nova-managed Incus instance must set ``boot.autostart=false``.
  Incus may start on a returning host for reconciliation, but nova-compute
  requires the ephemeral `/run/openstack-incus/compute-admitted` token.
  Never create that token automatically at boot. Admission follows external
  fencing and `tools/openstack-incus-returning-host-audit.sh`; Nova then owns
  stale evacuated-record cleanup and resuming locally authoritative guests.
- The repository's production fence adapter is
  `tools/openstack-incus-fence-agent-provider`. Keep its schema restricted to
  standard `ipmilan` and `redfish` agents, pass secrets only through the fence
  agent stdin protocol, and fail closed on insecure files or ambiguous status.
  Adapter tests do not replace a real BMC/PDU `off`/`on` evacuation gate.
- Ubuntu Apport rewrites `kernel.core_pattern` after systemd-sysctl. Production
  computes must mask `apport.service` as well as persist and apply
  `kernel.core_pattern=/dev/null`.

## Modernization Policy

- The first compatibility target is OpenStack `stable/2026.1`, Ubuntu Noble,
  Incus 7.x, and Python 3.12. Use the local upstream source trees to validate
  that baseline and note any deliberate compatibility with newer `master`
  interfaces separately.
- Modernize this existing project incrementally. Preserve its Nova compute
  driver, DevStack plugin, Tempest plugin, supported lifecycle behavior, and
  test intent where they remain valid. Do not replace it with an unrelated
  greenfield implementation merely to rename LXD concepts.
- Replace legacy LXD-specific behavior with current Incus `/1.0/instances`
  APIs through the local Incus Python SDK.
- Preserve Nova multi-tenancy through Nova's existing instance ownership,
  quotas, Placement allocations, Neutron ports, Glance visibility, and Cinder
  attachment authorization. For the first milestone, retain the legacy
  host-local Incus `default` project so modernization does not change the
  driver's ownership model. A deterministic OpenStack-project-to-restricted-
  Incus-project mapping can be introduced later as optional defense in depth;
  it must remain subordinate to OpenStack identity and authorization rather
  than becoming a second tenant control plane.
- Target Python 3 only.
- Implement and test the basic system-container lifecycle before enabling
  migration, resize, snapshots, Cinder attachment, or other advanced features.
- Unsupported Nova capabilities must be reported explicitly rather than
  silently emulated.

## Confirmed Functional Scope

- This project supports Incus system containers only. Incus virtual-machine
  support and QEMU integration are out of scope.
- The first milestone must provide create, query, start, stop, reboot, delete,
  console/log access where supported, host restart recovery, and periodic
  reconciliation without requiring an Incus cluster.
- Preserve the existing veth wiring model: Neutron supplies `network_info`,
  the driver and os-vif prepare the host side and OVS `br-int` port, and Incus
  receives the container-side veth as a physical NIC. Neutron ML2/OVN is the
  only owner of tenant IPAM, security groups, logical switches, logical ports,
  routing, floating IPs, and cross-node Geneve networking.
- Incus must not create managed tenant OVN networks, ACLs, forwards, zones, or
  duplicate Neutron state. It does not require an Incus cluster for cross-node
  tenant connectivity.
- Preserve the legacy image formats initially supported by the driver
  (`root-tar`, `squashfs`, and applicable `raw` container artifacts), then
  validate and narrow their exact Glance metadata and import behavior against
  Incus 7.x. Ordinary VM `qcow2` images must not be silently treated as system
  container root filesystems.
- Ceph RBD is the production instance-rootfs backend. Software installed with
  apt, dnf, or yum and normal rootfs data must persist across instance, Incus,
  and compute-host restarts.
- Rootfs size, Nova quotas/Placement allocations, and backend Ceph capacity
  controls must prevent tenant rootfs writes from exhausting the compute
  host's system disk. Incus/Podman data, logs, image caches, swap, and other
  control-plane paths remain separate host-capacity concerns and require their
  own bounded filesystems and monitoring.
- `/run`, `/dev`, and `/dev/shm` remain ephemeral runtime mounts. They must not
  be persisted to Ceph; their memory and possible swap impact is bounded by
  instance cgroups and host swap policy.
- Container root is allowed for tenant administration inside the system
  container, including apt/dnf/yum. It must map to a non-root host UID and must
  not imply host root, privileged-container, nested-container, raw LXC,
  arbitrary device, or host bind-mount access.

## Delivery Order

1. Make packaging, linting, unit tests, and DevStack integration work on the
   target Python/OpenStack baseline.
2. Modernize the SDK connection and basic Incus instance lifecycle while
   preserving the existing driver structure.
3. Modernize Placement inventory and resource traits.
4. Modernize os-vif and Neutron ML2/OVN wiring and validate multinode traffic.
5. Validate Incus-managed Ceph RBD rootfs persistence, quotas, host-disk
   containment, and cross-node migration. In parallel, modernize Cinder data
   volume attachment through os-brick and validate attachment movement with
   LINSTOR/DRBD before testing the production Cinder Ceph backend.
6. Add optional restricted-project defense in depth without redefining Nova
   tenancy.
7. Implement advanced features such as snapshot, resize, migration, and Cinder
   attachment only after their end-to-end semantics and security are tested.
- Never permit an Incus rootfs shrink. `migrate_disk_and_power_off` must reject
  a target Flavor whose `root_gb` is lower than the current Flavor before it
  queries, stops, or otherwise changes the source container. Nova's API-level
  zero-disk check is not a general disk-shrink guard.

## Operational and Test Constraints

These are the durable rules that emerged from the dedicated test environment.
The dated evidence that established them (which node, when, what passed) lives
in `TEST_STATUS.md`; this section keeps only the rules.

### Storage layering and flattening ownership

- "Layering" means RBD copy-on-write, not a runtime overlay rootfs.
  Incus-managed roots clone the Incus image-cache snapshot; BFV roots clone a
  Glance RBD snapshot through Cinder. Long-lived clones may pin obsolete image
  snapshots, but Nova must not directly flatten either ownership model. Incus
  owns flattening of ordinary roots; Cinder owns BFV flattening and
  stable/2026.1 already provides `rbd_max_clone_depth`,
  `rbd_flatten_volume_from_snapshot`, and `rbd_concurrent_flatten_operations`.
  A future Nova policy may select a backend or volume type, but execution
  remains with the storage owner.
- Never configure two independent Incus daemons against the same Ceph OSD pool
  without the fork's per-server image prefix and shared-storage handover.
  Upstream Incus 7.2 has no Ceph RBD namespace or configurable volume-prefix
  setting, so plain shared use fails with `Volume already exists on storage but
  not in database`. Per-compute pools are the fallback workaround; a
  consolidated production pool depends on the fork's shared-pool mechanisms.

### Swap, console, and crash-dump containment

- Nova Flavor `swap` is MB and maps to Incus `limits.memory.swap=<value>MiB`;
  zero maps to `false`. Keep `[incus] allow_instance_swap` as an operator gate
  and reject non-zero swap when it is disabled. The host must provide real,
  capacity-planned swap; cgroup v2 limits quantity but not shared swap-device
  IO latency. Eligible computes report the `CUSTOM_INCUS_SWAP` trait and
  swap-enabled Flavors must set `trait:CUSTOM_INCUS_SWAP=required` so Placement
  excludes ineligible hosts; the runtime operator gate is defense in depth.
- Incus 7.2 sets the LXC console buffer and disk size to `auto` (Ubuntu Noble
  liblxc resolves both to 128 KiB). Monitor the aggregate `/var/log/incus`
  filesystem. Compute provisioning must also set host `kernel.core_pattern` to
  `/dev/null` or explicitly disable/bound systemd-coredump storage, because
  container process crashes follow that host-global policy.

### Guest reboot/poweroff semantics

- Guest reboot/poweroff is confined by the PID namespace: restart exits PID 1
  as SIGHUP to the parent, halt/poweroff as SIGINT, and remaining namespace
  processes are killed. Never claim `reboot -f` is data-safe. Guest poweroff
  leaves Incus STOPPED while Nova may remain ACTIVE until the default 600 s
  power-state reconciliation; lifecycle event subscription is a tracked latency
  improvement, with periodic reconciliation retained as fallback.

### Custom compute manager and BFV recovery

- OpenStack 2026.1 does not consume a `compute_manager` setting when starting
  `nova-compute`; its service manager mapping is process-local and hard-coded.
  The project therefore provides `nova-incus-compute` and
  `python -m nova.virt.lxd.cmd.compute`, which select
  `nova.virt.lxd.manager.IncusComputeManager` before calling the upstream Nova
  compute entry point. DevStack installs a systemd drop-in for this command.
  Never claim automatic BFV recovery when a compute still starts the stock
  `nova-compute` binary.

### Data-volume attachment and filesystem safety

- Cinder data-volume attach/detach is executed by host-side os-brick, not by
  the Incus container. Every compute using the Cinder RBD backend must install
  host `ceph-common` and hold the scoped Cinder keyring/configuration; placing
  `ceph-common` only in `ghcr.io/fivetime/incus:alpine-novm` covers Incus
  rootfs operations but is insufficient for data volumes.
- Do not enable `security.syscalls.intercept.mount.allowed=ext4`: tenant-owned
  filesystem input must not be parsed by the host kernel. Tenant images that
  mount Cinder ext4 data volumes must include `fuse2fs` and use it explicitly.
- Keep `linstor_volume_downsize_factor=0` so Cinder's advertised size is not
  smaller than Nova's requested byte count.
- Keep `swap_volume` explicitly unsupported. Cinder attached-volume retype
  creates an empty target and expects the Nova driver to copy and pivot the
  active block device. Replacing an Incus `unix-block` profile entry does not
  perform that copy and caused confirmed data loss in E2E testing. Do not
  re-enable swap until a crash-consistent block-copy and recovery protocol is
  implemented and tested across process/node failure.
- Detached-volume retype and migration remain supported Cinder operations and
  require no Incus driver work. The production workflow is detach, wait for
  `available`, retype/migrate through Cinder, then attach. A volume attached
  to a SHUTOFF instance still enters Nova `swap_volume` and must be rejected.
- Cinder's Python RADOS client requires its keyring to be readable by the
  service user: `root:cinder 0640` in a packaged deployment (`root:stack 0640`
  in DevStack). Root-only `0600` makes the root CLI pass while the RBD driver
  fails initialization with `RADOS object not found`.

### Ceph pool and backup scoping

- Each pool must have a distinct least-privilege CephX client. The Ceph
  administrative plane must enable the `rbd` application metadata on pre-created
  pools; do not broaden compute client caps to permit pool metadata changes.
- The Cinder backup pool must differ from the Cinder volume pool, and its CephX
  client must be scoped only to that backup pool with no access to the volume
  or Incus rootfs pools. Same-cluster backups are operational backups, not an
  independent disaster-recovery failure domain.
- DevStack supports an existing Cinder Ceph backend through
  `INCUS_CINDER_CEPH_POOL`, `INCUS_CINDER_CEPH_USER`, `INCUS_CINDER_CEPH_CONF`,
  and `INCUS_CINDER_CEPH_CLUSTER_NAME`, and the Ceph backup service through
  `INCUS_CINDER_BACKUP_CEPH_POOL`, `INCUS_CINDER_BACKUP_CEPH_USER`, and
  `INCUS_CINDER_BACKUP_CEPH_CONF` when `c-bak` is enabled. These require
  pre-provisioned Ceph configuration and CephX keyrings, must not create or
  reuse the Incus rootfs pool, and LINSTOR/Ceph backend variables are mutually
  exclusive.

### Image admission and preflight gates

- The production BFV image is built from the Incus fork repository
  `docker/alpine-novm/Dockerfile`, not from the separate `incus-docker-image`
  repository. The generic image does not contain `storage_driver_cephext` or
  `migration_shared_ceph_storage` and must never be admitted as an OpenStack
  BFV compute image. Each release approves a new digest/revision pair and runs
  both production preflight scripts.
- `tools/openstack-incus-production-preflight.sh` is the fail-closed compute
  admission gate (baseline, cgroup/AppArmor, core dumps, socket/group
  membership, immutable GHCR digest and OCI revision, fork extensions,
  non-wildcard HTTPS binding, restricted preflight trust/project, BFV pool,
  custom manager, TLS/Ceph key permissions, bounded Incus state/log filesystems,
  time sync). `tools/openstack-incus-ceph-preflight.sh` is read-only; set
  `CHECK_CONFIGURED_BACKENDS=True` only after the backends exist.
- `tools/openstack-incus-ceph-rootfs-e2e.sh` is destructive; never run it
  against a non-Ceph pool (the script checks the pool driver first). Run
  destructive/fault-injection E2E from a separate trusted orchestrator; never
  copy the VM SSH private key onto a cloud node (use `SOURCE_SSH=local` /
  `CONTROLLER_SSH` where supported).
- `tools/openstack-incus-bfv-migration-matrix.sh` is the BFV cold-migration
  release gate. It runs the fault matrix, rejects OpenStack and compute runtime
  inventory drift after cleanup, and then runs fleet preflight. Production
  admission requires it to pass for every ordered compute pair.
- `tools/openstack-incus-fence-preflight.sh` is a non-destructive target and
  credential check. `tools/openstack-incus-bfv-evacuation-e2e.sh` is the
  destructive STONITH release gate; it must run through the same independent
  BMC or PDU control path used in production before BFV evacuation can leave
  `experimental`.
- `tools/openstack-incus-bfv-root-extend-e2e.sh` is the BFV root-growth release
  gate. It validates online growth, migration and revert persistence, injected
  filesystem-growth failure, reboot reconciliation, and shrink refusal. The
  test requires Cinder API microversion 3.42 or later.

### Repository and test-VM workflow

- The local repository is always the authoritative code tree. Synchronize it
  one way to `/opt/openstack-incus-src` with `tools/sync-test-vm.ps1`. Never
  edit project source directly on the VM; remote working trees and test state
  are disposable and may be replaced by `rsync --delete`.
- Authentication uses the dedicated local key
  `C:\Users\Simon\.ssh\openstack-incus-vm_ed25519`. Do not store the VM
  password, private key, or other secrets in this repository.

---

**Dynamic test progress** (dated E2E results, node inventory, image digests,
current environment state) is tracked separately in
[`TEST_STATUS.md`](TEST_STATUS.md) so this file stays a stable specification.
