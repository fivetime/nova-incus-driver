# Test Status and Topology

This file holds the **dynamic test-progress record** for the openstack-incus
project: the dedicated test topology, and dated validation evidence for each
feature. It is intentionally separated from `AGENTS.md`, which holds the stable
rules and design constraints. Durable rules that were once recorded here have
been distilled into `AGENTS.md` under "Operational and Test Constraints"; this
file keeps the *evidence* (what passed, when, on which node), not the rules.

Entries are append-mostly and are release evidence, not permanent
configuration. Each release must re-validate against its own approved
digest/revision pair.

## Dedicated test topology

- `root@10.224.0.16` (`incus-node-01`) is the current DevStack controller and
  first Incus compute node. It currently runs Ubuntu 26.04 and Python 3.14.
  The already-running DevStack nova-compute remains registered under its old
  startup-time host name `ubuntu`; reconcile that record when deploying the
  multinode topology rather than restarting it blindly.
- `root@10.224.0.17` (`incus-node-02`) is the second Incus compute node. It
  runs Ubuntu 24.04, has 16 CPUs, 31 GiB RAM, and one 400 GB system disk.
- `root@10.224.0.18` (`linstor-node-03`) runs the LINSTOR controller and a
  diskful satellite. It runs Ubuntu 26.04 with kernel 7.0, has 16 CPUs,
  30 GiB RAM, a 200 GB system disk, and a dedicated 1 TiB `/dev/vdb`.
- Stable hostnames and `/etc/hosts` self-resolution are configured on all
  three nodes as shown above.
- `.16` and `.18` currently run Ubuntu 26.04 with system Python 3.14; `.17`
  runs Ubuntu Noble 24.04 with Python 3.12. The three-node topology therefore
  proves mixed-version compatibility, not the required three-node target
  baseline. Unit tests on `.16` use an isolated Python 3.12 environment, but
  that does not replace full Noble host validation. Production release remains
  blocked until the complete multi-node suite passes on Noble/Python 3.12
  computes.
- `.16` uses `incus-ceph` backed by `incus-rootfs-rbd-pool` and `.17` uses
  `incus-ceph-node02` backed by `incus-rootfs-node02-rbd-pool`; Nova points to
  the corresponding local definition. Each pool has a distinct least-privilege
  CephX client.
- Remote unit-test runs must set `INCUS_SDK_PATH=/opt/incus-python-sdk-src`
  (the synchronized SDK path); the default sibling path remains correct for
  local checkouts.

### Storage substrate (LINSTOR/DRBD and Cinder control plane)

- All three nodes have a dedicated 1 TiB `/dev/vdb`, initialized on 2026-07-16
  as `linstor_vg/linstor_thinpool`. Each `linstor-pool` reports 972.55 GiB.
  DRBD 9.3.2 is loaded on all nodes and LINSTOR 1.34.1 reports them online.
  The three-copy `drbd-smoke` resource is `UpToDate` on every node with two
  established peer connections and majority quorum. Ubuntu 26.04 nodes need
  DRBD 9.3.2 source-built for kernel 7.0 and Noble's `drbd-utils` 9.34; this
  public-PPA/source combination is test-only, not the production package path.
- The Cinder stable/2026.1 v3 control plane was deployed on `.16` on
  2026-07-16. The public `block-storage` endpoint is
  `http://10.224.0.16/volume/v3`. The `cinder-scheduler` and LINSTOR-backed
  `cinder-volume` services are up. OpenStack volume
  `6964b960-bdff-400a-a1fc-adadfe9de62e` was created through the v3 API and
  maps to an `UpToDate` three-copy LINSTOR resource on `.16`, `.17`, and `.18`.
  Keep this volume as a backend health baseline.
- The existing Rook pool `cinder-volumes-rbd-pool` is also registered on `.16`
  as the `incus-node-01@ceph` Cinder RBD backend with public volume type `ceph`.
  The test deployment temporarily keeps LINSTOR and Ceph enabled together for
  comparison; this is runtime test state, not the DevStack plugin's mutually
  exclusive reproducible configuration.
- The test nodes now mount preallocated ext4 filesystems of 8 GiB and 2 GiB at
  `/var/lib/incus` and `/var/log/incus`, respectively. This is a test-only
  substitute because `/dev/vdb` is fully assigned to LINSTOR; production must
  use dedicated partitions or LVs. The original directory backups were removed
  only after the full migration E2E passed.

### Approved image and preflight evidence

- The approved test image is
  `ghcr.io/fivetime/incus@sha256:25b9975c9d3524bdf75c90f2fc499ceea591f82cc06b67073c92aed92ba9f025`,
  built from Incus fork revision
  `0f5cc4c41f0da3973a9d220d10b0f0f8daadda19`. Treat both values as release
  evidence, not permanent configuration; each release must approve a new
  digest/revision pair and run both production preflight scripts.
- On 2026-07-17 `.17` passed every `tools/openstack-incus-production-preflight.sh`
  check using image manifest digest
  `sha256:25b9975c9d3524bdf75c90f2fc499ceea591f82cc06b67073c92aed92ba9f025`
  from fork revision `0f5cc4c41f0da3973a9d220d10b0f0f8daadda19`.
- All three test Quadlets now use that immutable digest and bind Incus HTTPS
  only to their `10.224.0.x` management address. Migration client keys are
  `stack:stack 0600`; every trusted client is restricted to the zero-instance
  `nova-preflight` project. The complete BFV E2E passed after these changes.
- `tools/openstack-incus-fleet-preflight.sh` passed all cross-node driver,
  Nova, Placement, OVN, Cinder, migration-address, image, and Incus capability
  checks on 2026-07-17. Its only failures were the documented Ubuntu/Python
  baseline mismatches on `.16` and `.18`.

## Baseline and unit tests

- The target-baseline gate last passed on 2026-07-17 on `.17` with Python
  3.12: `tox -e py312` ran 161 tests (159 passed, 2 intentional legacy skips,
  0 failed), and `tox -e pep8` passed.

## Root-disk QoS (data volumes)

- Data-volume QoS was validated end to end on `incus-node-01` with the
  `unix_block_limits` fork extension: a Cinder `consumer=front-end` type using
  `read_bytes_sec=10000000` and `write_bytes_sec=5000000` produced an Incus
  `unix-block` device with exact byte values and host cgroup `io.max` for the
  mapped RBD major:minor. Direct I/O measured 10.0 MB/s reads and 5.0 MB/s
  writes. OpenStack detach removed the profile device and metadata, cleared the
  `io.max` entry, disconnected `/dev/rbd1`, and returned the volume to
  `available`. Test QoS, type, and volume resources were deleted afterward.
- The Incus mechanism was subsequently refactored so its main implementation
  lives in `internal/server/device/unix_block_limits.go`; it no longer changes
  `disk.go` or stores volatile major/minor state. Required/API-attached devices
  and `required=false` devices that appear later both apply limits. Removing
  the delayed host device removed the container node and cleared `io.max`.
  The generic upstream contribution is `lxc/incus#3648`; the PR is ready for
  maintainer review.

## BFV lifecycle (spawn/destroy/reboot)

- Standard OpenStack BFV create, hard reboot, delete, and reclaim passed on
  2026-07-17 on `.16`. Nova created an unprivileged Ubuntu Noble Incus system
  container from a Cinder Ceph volume through the fork's `cephext` pool and
  attached its Neutron port through OVN/OVS. A rootfs marker survived stop/start,
  hard reboot, server deletion, and recreation from the same volume. Deletion
  removed the Incus instance and RBD watcher, cleared the Cinder attachment,
  returned the volume to `available`, and did not delete the Cinder RBD image.
- Standard Nova hard reboot recovery was validated on 2026-07-17. A retained
  BFV owner was stopped directly to model a post-claim start failure. This
  exposed a pre-existing driver bug: `get_info()` called the Incus runtime
  state endpoint for a stopped container, received `Invalid PID -1`, and caused
  `nova-compute` to exit during `init_host`. The driver now reports Nova
  `SHUTDOWN` directly when the Incus status is `Stopped`, without querying a
  runtime PID, and `reboot()` starts a stopped instance instead of calling
  `restart()`. After deployment, nova-compute initialized successfully, consumed
  the queued hard-reboot request, started the same BFV target, and returned the
  instance to `ACTIVE` with its original RBD and `10.0.0.44` address.
- After the Cinder control-plane deployment, the Nova -> Incus -> Neutron/OVN
  lifecycle E2E passed again on `.16` with server
  `4494d2d6-fb20-4f20-a7f6-1a1819f9a79c`, including metadata, stop/start,
  hard reboot, rebuild, OVN interface validation, and deletion cleanup.

## BFV no-copy migration

No-copy BFV cross-compute migration remains incomplete in the Nova driver;
post-claim fault injection at each ownership transition is the open production
blocker. The dated evidence below tracks the incremental hardening.

- The first hard guard landed on 2026-07-17: source preparation and destination
  finish both require `migration_shared_ceph_storage`,
  `storage_driver_cephext`, and a `cephext` pool backed by the Cinder RBD pool.
  Migration metadata explicitly records whether the root is BFV, and the
  destination rejects disagreement with its Nova BDM instead of silently
  falling back to rootfs copying. This is a prerequisite guard, not completion
  of the production workflow.
- A real `.16` to `.17` migration attempt on 2026-07-17 proved the guard: the
  destination had the `cinder-bfv` pool but its Nova configuration omitted
  `boot_from_volume_storage_pool`, so `finish_migration` rejected the claim
  before a target instance or RBD watcher was created. Nova nevertheless left
  the instance assigned to `.17` in `ERROR` and the Cinder attachment in
  `attaching`; the test instance was recovered to `.16` with its original RBD,
  attachment, OVN port, and sole watcher intact. This confirms destination
  capability/configuration must move into an earlier Nova pre-check and that
  manager/driver failure rollback must restore host, attachment, and networking
  ownership atomically before BFV migration can be called production-ready.
- After fixing the migration loops to exclude `boot_index=0` from the os-brick
  data-volume detach/attach path, a real `.16` to `.17` BFV cold migration and
  confirm passed on 2026-07-17. Nova reached `VERIFY_RESIZE`; the sole RBD
  watcher moved from `.16` to `.17`; Cinder attachment and the OVN port moved
  to `.17`; both Incus records carried a committed handover marker; and confirm
  deleted the stopped source record without deleting the RBD. The instance
  returned to `ACTIVE` on `.17` with its original `10.0.0.44` address and
  mounted the same 2 GiB root RBD. The forward migration/confirm path is now
  proven zero-copy.
- The native Nova revert path was validated on 2026-07-17. A migration
  from `.17` to `.16` reached `VERIFY_RESIZE`, after which `server resize
  revert` deleted the committed target record without deleting the RBD and
  restarted the retained source record on `.17`. Nova returned to `ACTIVE`,
  Cinder attachment and the OVN port returned to `.17`, the RBD again had one
  `.17` watcher, and a marker written before migration remained in the rootfs.
  The driver fix was to exclude `boot_index=0` from the os-brick attach loop in
  `finish_revert_migration`, just as in forward migration. Normal forward
  confirm and revert are therefore both proven zero-copy.
- The first failure-state hardening landed on 2026-07-17. Once destination
  creation has returned an Incus instance for a BFV migration, later data-disk
  attach or start failure retains the target instance, profile, and VIF rather
  than deleting the new RBD owner. If create raises after a possible server-side
  success, the driver queries the instance name: an existing target is retained,
  an explicit 404 permits cleanup, and a failed ownership query is treated as
  uncertain and therefore non-destructive.
- Cold migration remains experimental while post-claim failure injection is
  incomplete. On 2026-07-17 the BFV source added a destination TCP/8443
  preflight before reading or stopping the Incus instance. An unreachable
  destination is raised as `InstanceFaultRollback` so Nova preserves the
  original ACTIVE state. Real `.17 -> .18` traffic rejection proved the source
  stayed RUNNING/ACTIVE, the target stayed absent, and Cinder/Neutron ownership
  stayed on `.17`.
- `tools/openstack-incus-bfv-migration-e2e.sh` now makes that preflight fault
  part of the repeatable release test before exercising forward confirm,
  retained-owner hard reboot, and reverse revert. It passed on 2026-07-17 with
  `.17` as source and `.18` as destination using `ubuntu-noble-incus-bfv-rbd`;
  cleanup left no server, volume, instance, or iptables rule, and all three
  nova-compute services remained enabled/up. Run it from a trusted external
  orchestrator with `INJECT_PREFLIGHT_FAILURE=true`. This does not replace
  post-claim fault injection.
- Post-claim data-volume attachment failure recovery passed on 2026-07-17
  with `.17` as source and `.18` as destination. The test bind-mounted
  `/bin/false` over the destination `rbd` command after the BFV root claim.
  Nova completed the ownership transition into `VERIFY_RESIZE`; the custom
  `IncusComputeManager` then restored the data-volume mapping, OVN runtime
  wiring, and container state after the failpoint was removed. It cleared the
  durable recovery marker without auto-confirming the resize. Explicit confirm
  returned the server to `ACTIVE`, and deletion left no test RBD mappings.
- Post-claim container-start failure recovery also passed on 2026-07-17.
  The test removed the destination veth peer after os-vif wiring, allowing
  the BFV root claim and Cinder data-volume connection to complete before
  Incus start failed. The custom manager recreated OVN/OVS runtime wiring,
  started the retained owner, cleared the durable recovery marker, and kept
  Nova in `VERIFY_RESIZE` until explicit confirmation.
- Recovery markers now encode the intended `running` or `stopped` state
  (legacy `true` remains compatible). A real SHUTOFF BFV migration with an
  injected post-claim data-volume failure recovered the mapping and OVN
  ownership, cleared the marker, remained stopped, and returned to SHUTOFF
  after explicit confirmation. The manager must never turn recovery into an
  implicit power-on.
- Reverse-revert recovery passed real fault injection on 2026-07-17. During
  `.18 -> .17` migration revert, `/usr/bin/rbd` was blocked on retained owner
  `.18`; its data-volume attachment exhausted bounded retries and wrote the
  durable marker. After the failpoint was removed, the manager repaired the
  RBD mapping, VIF and runtime state. Nova, Cinder and Neutron ownership
  remained on `.18`, the port returned ACTIVE, and final deletion left no
  mappings or resources.
- The normal forward-confirm, destructive hard-reboot repair, reverse-revert
  path passed three consecutive `.17 -> .18` runs on 2026-07-17. A prior run
  had timed out with the rebound Neutron port in `DOWN`, but this did not
  recur. The BFV E2E now dumps the complete Neutron port plus both computes'
  Incus and OVS interface state if port activation times out.
- The expanded BFV E2E passed again on 2026-07-17 after adding a Cinder data
  volume and destructive hard-reboot recovery injection. A subsequent audit
  exposed that instance deletion could release the Cinder attachment while
  leaving data KRBD mappings on both computes. `LXDDriver.cleanup()` now
  disconnects data volumes before deleting their Incus profile; disconnect
  failure retains the profile metadata and aborts destroy for a safe retry.
  The rerun asserted both `.17` and `.18` had no mapping before volume delete.
- BFV preflight authenticates to the destination with a dedicated TLS identity
  restricted to a `nova-preflight` project. It validates the two fork API
  extensions, readiness protocol version, target BFV pool name, the
  authoritative Cinder `rbd_pool`, and the target pool's `cephext` driver before
  source shutdown. Per-destination server certificate pinning is required for
  the test environment because pylxd treats a self-signed verify file as an
  exact fingerprint, not a multi-certificate CA bundle. Real mismatch testing
  on 2026-07-17 kept `.17` ACTIVE/RUNNING with no `.18` record; the corrected
  contract then passed the complete four-minute BFV migration E2E.

## Image-backed (copy-based) migration and lifecycle E2E

- Ceph-rootfs cold migration with distinct per-compute pools passed on
  2026-07-16 using `tools/openstack-incus-migration-e2e.sh`: `.16` to `.17`
  confirm and the reverse migration followed by revert preserved the rootfs
  marker, Neutron address, active ownership, and cleanup.
- The 2026-07-16 shared-pool migration test failed exactly as Incus'
  force-reuse warning predicts: the target reported `Volume already exists on
  storage but not in database`. (This motivated the fork's per-server image
  prefix / shared-pool handover work.)
- `tools/openstack-incus-ceph-rootfs-e2e.sh` (destructive) passed on 2026-07-16
  on `.16`: a 1 GiB `/var/tmp` allocation persisted across hard reboot, the
  5 GB Flavor quota rejected an oversized allocation, container root remained
  unprivileged on the host, and the compute system filesystem stayed below the
  256 MiB growth guard.
- Snapshot-to-Glance and cross-compute restore were validated on 2026-07-16
  with `tools/openstack-incus-snapshot-e2e.sh`: a running container on `.16`
  was captured as an active 213 MB Glance `raw/bare` image and restored on
  `.17`; the rootfs marker, Neutron/OVN ACTIVE port binding, source uptime,
  and cleanup of temporary Incus snapshots and published images all passed.

## Glance RBD fast path

- The `.16` Glance backend now defaults to the RBD store
  `glance-images-rbd-pool`, enables `show_image_direct_url`, and uses raw
  container-root images. On 2026-07-17 a Cinder `ceph` volume created from
  `ubuntu-noble-incus-bfv-rbd` had an RBD parent in that Glance pool with the
  same Ceph FSID and a full 2 GiB overlap. This proves the production COW clone
  fast path rather than a download/full-copy path. Validate the RBD parent in
  release tests; elapsed time alone is not sufficient evidence.

## Cinder data volumes (attach / extend / snapshot / backup)

- Tempest covers the public-API workflow in
  `nova_lxd_tempest_plugin.tests.scenario.test_volume_ops`. On 2026-07-16 both
  the attach/FUSE/detach smoke test (45 seconds) and the attached-volume online
  extend plus cross-compute cold-migration test (85 seconds) passed. The latter
  uses Cinder microversion 3.42 and a 5 GiB-root `d1` flavor so it fits the
  development 20 GiB Incus pool. Glance image `ubuntu-noble-cloud-incus-tempest`
  (`777fea9f-21bf-4250-9935-ceedd02f9b64`) contains both `openssh-server` and
  `fuse2fs`. DevStack reproduces these settings with `INCUS_TEMPEST_BUILD_IMAGE=True`,
  `INCUS_TEMPEST_MIN_COMPUTE_NODES=2`, `INCUS_TEMPEST_FLAVOR_REF=d1`, and
  `INCUS_ALLOW_COLD_MIGRATION=True`.
- The Cinder/LINSTOR attach and cold-migration E2E passed on 2026-07-16: a
  three-copy volume was attached to an unprivileged Ubuntu Noble container on
  `.16`, formatted and mounted through container-side `fuse2fs`, extended
  online from 1 GiB to 2 GiB, migrated to `.17`, read back with the original
  marker, detached, and cleaned.
- A 2026-07-16 Cinder Ceph public-API smoke created a 1 GiB Ceph volume,
  attached it to an unprivileged Ceph-rootfs container as `/dev/sdb`, formatted
  and mounted it through `fuse2fs`, persisted a marker, extended it online to
  2 GiB, refreshed the KRBD/container size, grew ext4, read the marker,
  detached, and removed the RBD mapping and image.
- Cinder Ceph snapshot and clone were validated through the public v3 API on
  2026-07-16. A marker written through `fuse2fs` survived detach, RBD snapshot,
  creation of a new Cinder volume from that snapshot, and read-only attachment
  of the clone. `rbd info` showed the clone parent and 1 GiB overlap; all
  resources were removed without residue. This is storage snapshot/clone
  coverage, not backup coverage.
- Cinder Ceph backup was validated through the public v3 API on 2026-07-16
  with an isolated `cinder-backups-rbd-pool` and pool-scoped
  `client.cinder-backup`. A full backup, an incremental child backup, and a
  restore into a new Ceph volume preserved the updated tenant marker. Cinder
  used native RBD differential export/import. Same-cluster backups are
  operational backups, not an independent disaster-recovery failure domain.
- The one-replica outage E2E in `tools/openstack-incus-linstor-outage-e2e.sh`
  passed on 2026-07-16. While a three-copy Cinder volume remained FUSE-mounted
  on `.16`, the `.18` LINSTOR satellite was stopped but its controller remained
  online. The two remaining `UpToDate` replicas sustained marker reads and a
  16 MiB `fsync` write under majority quorum. After `.18` returned, its replica
  resynchronized and the data was read again.

## Current test-environment state notes

- Re-running DevStack to add Cinder rebuilt the test Nova databases. Three
  retained Incus containers (`instance-00000008`, `instance-00000009`, and
  `instance-0000000a`) therefore no longer have Nova database records. Their
  former UUIDs are preserved on `.16` as `user.openstack.orphaned_uuid`;
  `user.openstack.uuid` is intentionally unset so the new nova-compute service
  does not claim them. **Do not present these as current OpenStack instances or
  delete them without explicit approval.**
