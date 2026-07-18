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

- `root@10.224.0.21` (`incus-node-01`) is the DevStack controller and first
  Incus compute node. It runs Ubuntu Noble 24.04 and Python 3.12.
- `root@10.224.0.17` (`incus-node-02`) is the second Incus compute node. It
  runs Ubuntu Noble 24.04 and Python 3.12.
- `root@10.224.0.22` (`incus-node-03`) is the third Incus compute node. It
  runs Ubuntu Noble 24.04 and Python 3.12.
- Stable hostnames and `/etc/hosts` self-resolution are configured on all
  three nodes as shown above.
- All three nodes run independent, non-clustered Incus 7.2 daemons and expose
  migration HTTPS only on their management address. Nova, Placement and
  Neutron/OVN own placement and tenant networking.
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
  `ghcr.io/fivetime/incus@sha256:cc8b71093395ca89d4d5d885b84e861d75dcb2de38d7220e68f1cdee239bd72d`,
  built from Incus fork revision
  `5adcaca1ad383362bb824a15845ecd4a85f24ba5`. Treat both values as release
  evidence, not permanent configuration; each release must approve a new
  digest/revision pair and run both production preflight scripts.
- On 2026-07-17 all three Noble nodes passed every
  `tools/openstack-incus-production-preflight.sh` check using that digest and
  revision. The image includes the minimal `aa-exec`, so the result does not
  depend on a runtime container hotfix.
- All three Quadlets use the immutable digest and bind Incus HTTPS only to
  their management address. The migration client key is
  `root:incus-admin 0640`; every trusted client is restricted to the
  zero-instance `nova-preflight` project.
- `tools/openstack-incus-fleet-preflight.sh` passed all cross-node driver,
  Nova, Placement, OVN, Cinder, migration-address, image, and Incus capability
  checks on 2026-07-17 across `.21`, `.17`, and `.22`. Driver hashes were
  identical and all three nova-compute and OVN controller services were
  enabled and up.
- On 2026-07-18, the fleet audit initially produced false TLS-key permission
  failures because node-local copies of the host audit were older than the
  orchestrator. The fleet audit now streams its own adjacent host-preflight
  script to each node through SSH instead of executing a remote checkout.
  The revised audit passed all checks across `.21`, `.17`, and `.22` while the
  nodes still had the older copies, proving the audit policy is supplied by
  the trusted orchestrator.
- After the immutable-image rollout, the complete BFV E2E passed between
  `.21` and `.17`: rejected destination preflight, shared-Ceph zero-copy
  migration, confirm, hard-reboot recovery, reverse migration, revert, data
  volume cleanup, and final resource cleanup.

## Baseline and unit tests

- The target-baseline gate last passed on 2026-07-17 on `.21` with Python
  3.12: `tox -e py312` and `tox -e pep8` both completed with exit code 0.

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

No-copy BFV cross-compute migration has passed the core post-claim fault
matrix and every ordered pair in the three-node test fleet. It is a release
gate that must be rerun for each approved version, not an unfinished
implementation item. The dated evidence below tracks the incremental
hardening.

- The release matrix was consolidated on 2026-07-18 in
  `tools/openstack-incus-bfv-migration-matrix.sh`. On the three-node Noble
  environment, normal preflight/confirm/hard-reboot/revert, post-claim
  data-volume failure, post-claim container-start failure, stopped-instance
  post-claim failure, and reverse-revert failure all passed. Every case
  restored the pre-test OpenStack server/volume inventory and each compute's
  Incus instance/profile/RBD-mapping inventory.
- The first normal matrix attempt on 2026-07-18 failed closed because the
  Neutron port remained `DOWN` for ten minutes after reverse revert even
  though the destination OVS interface was up and OVN had emitted a
  `network-vif-plugged` event. Cleanup was complete and an immediate complete
  rerun passed. This remains an observed OVN control-plane convergence event;
  the release gate intentionally does not suppress or retry past its timeout.
- The three-node fleet preflight passed on 2026-07-18 after synchronizing the
  authoritative driver tree. It verified identical driver hashes, Incus 7.2
  image digest
  `sha256:cc8b71093395ca89d4d5d885b84e861d75dcb2de38d7220e68f1cdee239bd72d`,
  fork revision `5adcaca1ad383362bb824a15845ecd4a85f24ba5`,
  Nova/Placement readiness, live OVN controllers, and the enabled/up Cinder
  Ceph backend and scheduler.
- The remaining ordered compute pairs `node-01 -> node-03`,
  `node-03 -> node-01`, `node-02 -> node-03`, and `node-03 -> node-02`
  completed the five-case BFV release matrix on 2026-07-18. The matrix runner
  was corrected so every selected case uses the declared source and
  destination instead of alternating two cases in the reverse direction.
- The expanded pair testing made the earlier OVN convergence failure
  deterministic during an injected reverse-revert data-volume failure.
  Neutron returned the binding to the retained source chassis, but the
  existing OVS interface did not cause OVN to reassert `Port_Binding.up`.
  Normal revert and durable-marker automatic recovery now stop the container
  when necessary and recreate retained host VIF wiring before restoring the
  intended RUNNING or SHUTOFF state. The previously failing
  `node-02 -> node-03` reverse-revert case and the complete
  `node-03 -> node-02` matrix then passed with ACTIVE ports and clean runtime
  inventories.

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

## BFV failed-host evacuation

- On 2026-07-18 Nova-managed containers were changed to
  `boot.autostart=false` and nova-compute gained an ephemeral per-boot
  admission token. A real reboot of `incus-node-01` changed its boot ID,
  started Incus with its tenant container STOPPED, left nova-compute failed
  closed and quarantined, and preserved `kernel.core_pattern=/dev/null` after
  Apport was masked. The returning-host audit passed before explicit admission;
  Nova then restored the locally authoritative ACTIVE instance and OVN port.
- `incus-node-02` was rebooted while hosting BFV server
  `7cbb8e9a-3532-4fb7-b3bc-8e4497bc6b72`. It became unreachable, the source
  RBD watcher disappeared, and Nova evacuated the server to
  `incus-node-03`. On return, the source remained quarantined with the stale
  instance STOPPED and no KRBD mapping. The audit verified Nova/Neutron
  ownership on node 03 and exactly one target watcher. After admission, Nova
  removed the stale source record without affecting the ACTIVE target.
- A reverse node-03 to node-02 host-loss evacuation wrote
  `/root/stonith-e2e-marker` before the source disappeared. The recovered file
  on node 02 matched SHA-256
  `13b63404394548890d9c7f7548afd54ef9ac366d243b5f85649368b4f3d83698`;
  the target became ACTIVE with one watcher. When node 03 returned, its two
  stopped records intentionally covered both ownership classes: Nova still
  assigned `instance-0000000f` to node 03, while `instance-00000011` was stale
  after evacuation. The audit accepted the local owner and rejected any early
  start, verified the stale record had no mapping and exactly one watcher on
  node 02, then admission let Nova restore the local owner and delete only the
  stale record. Both distinct roots ended with one watcher on their respective
  authoritative hosts.
- `tools/openstack-incus-bfv-evacuation-e2e.sh` now defines the production
  release protocol for an external provider with `off`, `status`, and `on`
  operations. At this checkpoint the external power gate had not run; the
  later independent KVM-host result below supersedes that interim status.
- The post-test three-node fleet preflight passed with all computes
  `enabled/up`, driver hash
  `24dffc1826f6c2202355a1c359173da9345107eae630a639900ac8e5a2754867`,
  per-boot admission, disabled Incus autostart, persistent core-dump
  containment, Placement, OVN and Cinder Ceph checks all green.

- On 2026-07-17, server `31159ba2-2c52-4ead-98c5-a06ca934178b`
  was booted from Cinder RBD volume
  `f6f81198-2455-43fc-b00a-83e65f99e74b` on `incus-node-01`.
  A rootfs marker was persisted, the source compute and Incus daemon were
  stopped, the remaining LXC monitor and KRBD mapping were explicitly fenced,
  and Nova evacuated the server to `incus-node-03`.
- Nova created a new Cinder attachment, rebound the same Neutron port, claimed
  the existing RBD through `cephext`, and completed the Placement evacuation
  claim. After an API start, the marker, fixed address `10.0.0.47`, and ACTIVE
  port were intact. Ceph reported exactly one watcher on `incus-node-03`.
- Restarting the source Incus and nova-compute caused Nova's standard
  `_destroy_evacuated_instances` path to remove the stale source Incus record
  and source allocation. The target remained ACTIVE and the external RBD was
  untouched. This relies on `cephext.DeleteVolume` releasing only the local
  claim record; Cinder remains the external image owner.
- Stopping or killing the Podman container is **not fencing**. The host LXC
  monitor, mount namespace, KRBD mapping, and Ceph watcher survived the Podman
  daemon container. Production evacuation still requires power fencing or an
  equivalent host-level STONITH implementation and must verify that the source
  has no RBD watcher before starting Nova evacuation. The test used explicit
  monitor termination, unmount, and KRBD unmap only because the controller
  shared the source test VM.
- The server was observed as stopped after the controlled source teardown, so
  Nova correctly restored SHUTOFF on the target. OSC `server evacuate --wait`
  continued polling for ACTIVE even though evacuation had completed. Release
  automation must poll for a terminal task state and the requested/original
  power state instead of assuming every successful evacuation ends ACTIVE.

## BFV shelve and explicit root-volume reimage

- On 2026-07-18 the release-grade reimage gate passed both ACTIVE and SHUTOFF
  explicit BFV reimage at Nova microversion 2.93. The implicit reimage was
  rejected without changing the root marker, Cinder attachment or OVN
  network. The SHUTOFF server remained stopped until explicitly started.
- On 2026-07-18 host-targeted unshelve at Nova microversion 2.91 moved a BFV
  server from `incus-node-01` to `incus-node-02`. Its root marker, Cinder
  attachment, fixed IP, OVN/OVS binding and read-only config drive survived.
- On 2026-07-18 the BFV bootstrap gate proved an Ed25519 keypair, metadata,
  user-data, config-drive UUID/content, read-only mount and soft-reboot
  persistence through the public APIs.
- On 2026-07-18 flavor resize passed on the three-node scheduler topology:
  1 to 2 GiB root, 512 to 1024 MiB RAM, 1 to 2 vCPU and PID 2048 to 4096.
  Cross-host marker preservation, Placement allocations, revert and confirm
  all passed.
- On 2026-07-18 all five BFV migration matrix cases passed: normal
  confirm/revert, data-volume post-claim failure, start post-claim failure,
  stopped-instance post-claim failure and reverse revert. Every case passed
  the residual Nova/Cinder/Incus/RBD audit.
- Tempest's API-only run passed the compute/network lifecycle scenario with
  two guest-data scenarios explicitly skipped because the controller cannot
  reach TCP/22 on the floating network. A validation-enabled run proved that
  all three scenarios reached guest SSH after successful Nova scheduling and
  Ceph volume creation, then failed only at that network boundary. The
  dedicated E2E gates above provide the guest-data evidence.
- The final 2026-07-18 fleet preflight passed on `incus-node-01`,
  `incus-node-02` and `incus-node-03`: immutable image digest and source
  revision, driver hash, Incus 7.2 extensions, AppArmor/cgroups, Ceph access,
  dedicated control filesystems, restricted TLS, Placement, OVN, Nova and
  Cinder were all green. The authoritative Python 3.12 tree then passed 233
  unit tests with 2 documented legacy skips, pep8 and Sphinx `-W`.

- On 2026-07-17, shelving a BFV server reached `SHELVED_OFFLOADED`, removed
  its Incus instance record and Ceph watcher, and left the Cinder attachment
  reserved. Unshelving recreated the server on `incus-node-03` with the same
  rootfs marker and fixed IP, completed the Cinder attachment, and produced
  exactly one target RBD watcher.
- Nova compute API microversion 2.93 correctly rejected an implicit BFV
  rebuild. An explicit
  `server rebuild --reimage-boot-volume --image ubuntu-noble-incus-bfv-rbd`
  reimaged the Cinder root volume and returned the server to ACTIVE on the
  same host with fixed IP `10.0.0.25`.
- The pre-rebuild `/root/rebuild-marker` was absent afterwards, as required
  for a destructive reimage. The root filesystem was mounted from
  `/dev/rbd0[/rootfs]`, Cinder reported one attached root volume, and Ceph
  reported exactly one watcher on `incus-node-03`.
- Nova's default reimage transaction destroys the Incus instance and profile
  before it calls the driver's root-volume detach hook. That detach must be
  idempotent only for a validated Cinder RBD root at
  `instance.root_device_name`; missing profiles for data-volume detach remain
  errors. Unit coverage enforces both sides of this boundary.
- Incus instances managed by Nova are in the `nova` Incus project. Operational
  checks and audit scripts must use `--project nova` or `--all-projects`;
  querying the default project alone produces a false "instance not found".

## BFV Flavor resize

- On 2026-07-17, BFV server
  `1f6cf422-4a9e-4f62-a3c5-21ed36817d67` was resized and confirmed from
  Flavor `ds512M` (`root_gb=5`, RAM 512 MiB) to `ds1G` (`root_gb=10`,
  RAM 1024 MiB). It moved from `incus-node-03` to `incus-node-02` and
  returned ACTIVE with fixed IP `10.0.0.25`.
- The root Cinder RBD remained exactly 5368709120 bytes before and after the
  resize. Flavor `root_gb` is intentionally ignored for BFV capacity; only a
  separate Cinder extend operation may change that volume.
- The rootfs marker and filesystem identifier `78ca08dac04f118b` were
  preserved. The destination expanded root device used the `cinder-bfv`
  `cephext` pool and contained no `size` key. Ceph reported exactly one
  watcher, on `incus-node-02`.
- Local-root resize still rejects a smaller `root_gb` before stopping the
  source. BFV resize permits both directions because Cinder independently
  owns root capacity, matching Nova's libvirt semantics.

## Config-drive BFV resize

- On 2026-07-17, BFV server
  `7cbb8e9a-3532-4fb7-b3bc-8e4497bc6b72`
  (`instance-00000011`) was created with config-drive on `incus-node-03`,
  resized from `ds512M` to `ds1G` on `incus-node-02`, and confirmed through
  the public OpenStack API. It returned ACTIVE with fixed IP `10.0.0.16`.
- The source and destination config-drive manifests had the same SHA-256
  digest, `6eb6a82d0cd2ea28f14855165e5c1064cfede4918bfee518fa2fffd699325390`.
  The destination directory was owned by its isolated idmap UID `1196608`;
  container root could read `openstack/latest/meta_data.json`, while a write
  failed with `Read-only file system`.
- The Cinder root RBD remained exactly 5 GiB, retained its Glance RBD snapshot
  parent, and had exactly one watcher on `incus-node-02`. Resize confirm
  removed both the source Incus record and the complete source instance
  directory. The destination kept the same config-drive contents and Neutron
  address.
- The E2E exposed an Incus transition detail now covered by a unit contract:
  before a migrated target starts, `volatile.idmap.current` can still contain
  the source mapping while `volatile.idmap.next` contains the target mapping.
  Config-drive ownership therefore resolves `next`, then `current`, then
  `last_state`.

## BFV pause and shelve

- On 2026-07-18, server
  `7cbb8e9a-3532-4fb7-b3bc-8e4497bc6b72` passed the public Nova
  pause/unpause workflow on `incus-node-02`. Nova transitioned
  `ACTIVE -> PAUSED -> ACTIVE` while Incus transitioned
  `RUNNING -> FROZEN -> RUNNING`. Fixed IP `10.0.0.16`, readable read-only
  config-drive contents, and the single root RBD watcher were preserved.
- BFV server `9d497f51-4c69-49b6-8e8f-820369d10e29` passed two public
  shelve/unshelve cycles on `incus-node-03`. `SHELVED_OFFLOADED` removed the
  Incus record and local instance directory, removed the Cinder attachment,
  and left the root volume `reserved`. Unshelve restored ACTIVE, fixed IP
  `10.0.0.34`, one Cinder attachment, one Incus owner, and one Ceph watcher.
- A marker written and immediately verified in `/root/shelve-marker`
  survived offload and unshelve on the same Cinder RBD. The recreated
  config-drive remained readable and read-only. Its file digest changed
  because Nova regenerates metadata including `random_seed`; byte stability
  is guaranteed for the driver's cold-migration transfer, not for Nova
  shelve/unshelve.
- `tools/openstack-incus-bfv-lifecycle-e2e.sh` makes these checks repeatable
  across a configured compute inventory and cleans its server and root volume.
  Its clean automated rerun passed with server
  `c5056251-d1f1-4eaf-ad51-1d033111f174`, root volume
  `40c5e8c8-6e98-4bb1-9b61-188b0671f711`, owner `incus-node-03`, and fixed IP
  `10.0.0.54`.

## Instance diagnostics

- On 2026-07-18, the explicit Nova diagnostics compatibility patch was
  applied on all three nodes and the public Nova API returned HTTP 200 through
  cross-host RPC for BFV server
  `7cbb8e9a-3532-4fb7-b3bc-8e4497bc6b72` on `incus-node-02`.
- Microversion 2.48 reported `driver=lxd`, `hypervisor=incus`, running state,
  Flavor vCPU count, aggregate CPU time, runtime memory, accurate `eth0`
  counters, and uptime derived from Incus `started_at`. The tested BFV
  container returned an uptime of 7013 seconds. The root disk's unavailable
  block I/O fields are null. Microversion 2.47 returned the corresponding
  legacy CPU, memory, and NIC dictionary.
- Diagnostics testing exposed and fixed the pre-existing Flavor conversion
  from Nova MiB to Incus decimal `MB`. New profiles now use `MiB`; the complete
  Python 3.12 suite passed 220 tests with 2 intentional legacy pylxd skips.
  Existing instances retain their prior configured limit until an explicit
  lifecycle operation reapplies their Flavor.
- Incus 7.2 already exports container cgroup v2 `io.stat` counters through
  `/1.0/metrics`. For BFV root volume
  `bb10c287-05c5-4ef0-b165-7473a9e8dbfe`, `block_stats` returned
  `[2409, 103727104, 166, 2715648, 0]` before a 64 MiB synchronized write and
  `[2411, 103800832, 174, 69824512, 0]` after the metrics cache refreshed.
  The write-byte increase was exactly 67,108,864 bytes.
- A temporary 1 GiB Ceph data volume was attached through the public Nova API
  as `/dev/sdb`. After real filesystem I/O, the driver mapped its profile
  source to `rbd2` and returned 55 reads/1,110,016 read bytes and 161
  writes/34,242,560 written bytes. `get_all_volume_usage` attributed those
  counters to the correct Cinder UUID. The test volume was detached and
  deleted.
- With `volume_usage_poll_interval=15` on all three computes, Nova's native
  `_poll_volume_usage` periodic task ran without errors. For the same BFV root,
  `nova_cell1.volume_usage_cache` contained `curr_reads=2421`,
  `curr_read_bytes=104484864`, `curr_writes=196`, and
  `curr_write_bytes=69935104`, attributed to the correct instance and Cinder
  volume UUID.

## Ceilometer volume I/O metering

- On 2026-07-18, Ceilometer notification and Gnocchi 4.7 services were
  deployed on `incus-node-01`; all three computes used
  `volume_usage_poll_interval=60`. The standard Nova `volume.usage`
  notification carried the current project/user IDs, instance UUID, Cinder
  volume UUID, and all four cumulative counters.
- Ceilometer `stable/2026.1` has two upstream gaps for this Nova notification:
  its default meters file does not create volume I/O samples, and its Gnocchi
  `volume` resource mapping does not accept the four metric names. The
  DevStack plugin now installs a separate declarative meter file and applies
  a narrowly scoped Ceilometer resource-map patch.
- A 96 MiB synchronized write inside BFV instance `instance-0000000f`
  produced a Gnocchi `volume` resource keyed by Cinder UUID
  `f6f81198-2455-43fc-b00a-83e65f99e74b`. Its project and user ownership
  matched Keystone, and Gnocchi stored all four metrics:
  `volume.read.requests=2571`, `volume.read.bytes=103952384`,
  `volume.write.requests=538`, and
  `volume.write.bytes=112140288` at the tested archive interval.
- A subsequent hard reboot reset the container's cgroup counters. Nova
  detected the lower values and moved the pre-reboot current counters into
  `tot_*`; cumulative write bytes increased monotonically from `112214016`
  to `147578880` after another 32 MiB synchronized write. Restarting
  `nova-compute` alone did not reset or duplicate the counters.
- Cold migration of the same BFV instance from `incus-node-02` to
  `incus-node-03` followed by a 48 MiB synchronized write preserved both the
  Cinder UUID and the cumulative series. Nova moved the source counters into
  totals and reported `201699328` cumulative write bytes on the destination.
- The lab's OVN Northbound database had been recreated by the interrupted
  Ceilometer DevStack run while the restored Neutron SQL database retained
  the authoritative networks. This made the migrated port DOWN despite
  correct veth and OVS wiring. After backing up both OVN databases,
  `neutron-ovn-db-sync-util --ovn-neutron_sync_mode repair` reconstructed the
  logical state; the port became ACTIVE, DHCP restored `10.0.0.25`, and
  cross-node traffic to `10.0.0.16` and `10.0.0.30` passed.
- The four cumulative metrics use the dedicated `ceilometer-volume-io`
  archive policy with `mean`, `rate:mean`, and `rate:sum`. DevStack's global
  Gnocchi archive-policy override is removed so the resource mapping can
  select that policy. Billing must query `rate:sum` for billable I/O in each
  five-minute window; `rate:mean` is a trend and plain `mean` is the average
  cumulative counter value. A controlled 20 MiB synchronized write in a new
  window produced exactly `20971520` for `volume.write.bytes` with
  `rate:sum`; the corresponding write-request increase was 14 and an idle
  window returned zero.
- A temporary Ceph data volume was hot-attached to running BFV instance
  `instance-0000000f`. Tenant `fuse2fs` mounted it without allowing the host
  kernel to parse tenant filesystem input. Nova attributed its counters to
  Cinder volume `928417f8-78b8-4c7a-8ada-fb1eee1cc619`; a controlled second
  write produced `16809984` bytes in Gnocchi `rate:sum` (16 MiB plus ext4
  metadata). Hot detach moved the final counters to `tot_*`, emitted the final
  Gnocchi sample, removed the Incus device and metadata, unmapped the host
  RBD, returned the volume to `available`, and allowed clean deletion.
- The first `delete_on_termination` test exposed Incus' fixed eight-second
  `/1.0/metrics` cache: an immediate final read reused stale counters and
  omitted the last write. `IncusComputeManager` now waits nine seconds before
  final detach settlement, once per instance shutdown. A cache-pinned E2E
  then wrote 8 MiB, added 4 MiB, and immediately deleted server
  `aaab2011-27fd-4d92-852d-48a37e2597b5`. Nova finalized exactly
  `12582912` write bytes and Cinder automatically deleted volume
  `31da9d03-b2e0-4418-91a0-d612348a530e`. The observed server-delete duration
  was 18.25 seconds, including the deliberate nine-second consistency wait.
  After the lifecycle fixes, the full Python 3.12 suite passed 228 tests with
  2 intentional legacy pylxd skips; pep8 and warning-as-error documentation
  builds passed.
- A separate data-volume lifecycle E2E expanded Ceph volume
  `2f091345-9b81-4e23-b7ac-980cdad7fe58` online from 1 GiB to 2 GiB. Cinder,
  host KRBD, and container `/dev/sdb` all reported 2 GiB. A write at a 1.5 GiB
  offset succeeded and its cumulative series grew continuously from 8 MiB to
  12 MiB under the same Cinder UUID.
- A forced snapshot and Cinder RBD clone preserved matching SHA-256 values for
  both the first 8 MiB and a 4 MiB region at the 1.5 GiB offset. Independent
  writes were attributed separately: the source finalized at 16 MiB and clone
  `5b4681c3-9b6b-46ea-8c83-a485201d9602` at 6 MiB. The disposable instance,
  volumes, and snapshot were removed.
- Nova correctly rejected a tenant-initiated volume replacement with HTTP 409
  because `os-volume_attachments` swap is reserved for Cinder migration.
- On 2026-07-18, the pre-created 28.5 GiB DevStack LVM backend was temporarily
  enabled to exercise a real attached-volume retype from Ceph to LVM. The
  operation exposed a critical driver defect: Cinder created an empty target
  and delegated the block copy/pivot to Nova, while the Incus driver replaced
  the `unix-block` profile device without copying data. The pre-migration
  16 MiB SHA-256 changed from `53c2d6...` to the all-zero digest `080acf...`.
  Cinder then deleted the old RBD as though migration had succeeded.
- `swap_volume` now raises `NotImplementedError` before connecting or changing
  either device. A second real retype attempt preserved Ceph volume
  `d8bbe861-5af0-4b6c-bfac-aa817aaa7752`, its attachment, and its exact
  `9ff314...` SHA-256 while Cinder rolled back the temporary LVM target. Online
  volume migration/retype is explicitly unsupported until a crash-consistent
  block-copy protocol exists. The temporary LVM backend was disabled again.
- Unsupported suspend, resume, rescue, and unrescue now fail in the Incus
  compute manager before power, network, or storage side effects. Unit coverage
  verifies task-state rollback, VM-state preservation, instance-fault
  recording, and failed action-event recording for all four operations. A real
  suspend request against instance
  `7cbb8e9a-3532-4fb7-b3bc-8e4497bc6b72` left it `ACTIVE` with no task state
  and recorded `compute_suspend_instance` as `Error` with the explicit
  `InstanceSuspendFailure` reason. The Python 3.12 suite passed 237 tests with
  2 intentional legacy pylxd skips; pep8 and warning-as-error docs passed.
- A BFV root-growth E2E expanded a running Cinder root from 2 GiB to 3 GiB,
  verified the RBD, Incus local root device, and ext4 filesystem, hard
  rebooted it, and confirmed a cold migration to `incus-node-02`. Binding
  `/bin/false` over the Incus image's `resize2fs` then forced the 3 GiB to
  4 GiB filesystem-growth step to fail while Cinder retained the larger RBD
  and Incus retained the old device size. After removing the fault, a hard
  reboot reconciled the Cinder BDM size and grew ext4 to 4 GiB. Reverse
  migration/revert preserved the root marker and size, and Cinder rejected a
  shrink request. The final Python 3.12 suite passed 229 tests with 2
  intentional legacy pylxd skips; pep8 and warning-as-error docs passed.
- On 2026-07-18, a production fence-provider adapter was added for
  ClusterLabs `fence_ipmilan` and `fence_redfish`. It accepts only a
  whitelisted JSON schema, opens root-owned `0600` configuration and password
  files with `O_NOFOLLOW`, caps them at 64 KiB, passes the password through
  fence-agent stdin rather than argv, and rejects ambiguous power status.
  Six Nova unit tests, flake8, a real-process fake-agent integration test, an
  insecure-mode rejection, and the warning-as-error documentation build
  passed on `incus-node-01`. This checkpoint validated the adapter but not
  external power; the later independent KVM-host gate below completed that
  requirement.
  The final full regression passed 244 tests with 2 intentional legacy pylxd
  skips. Pep8, all shell syntax checks, the fence preflight positive/negative
  tests, and warning-as-error documentation passed. Fleet preflight then
  passed on all three computes with Incus 7.2, image digest
  `sha256:cc8b7109...239bd72d`, revision `5adcaca1...f24ba5`,
  identical driver hash `24dffc18...754867`, current admission tokens,
  disabled instance autostart, enabled/up Nova services, live OVN agents,
  Placement inventories, and the Cinder Ceph backend.
  The destructive evacuation gate now finishes with explicit post-admission
  assertions for one Cinder root attachment, zero source KRBD mappings, the
  expected destination mapping count, destination Neutron binding, exactly
  one destination OVS interface per server port, and no matching source OVS
  interface. The OVS/KRBD/binding queries were replayed against the retained
  node-03 to node-02 evacuation state: node-02 had one interface and mapping,
  node-03 had neither, and Neutron named node-02 as owner.
- On 2026-07-18, the external STONITH release gate completed against the
  independent KVM host `10.224.0.9`. The authoritative mapping was
  `incus-node-02` (`10.224.0.17`) to libvirt domain
  `ubuntu-24-incus-test`; the source and destination were separate from the
  orchestrating controller. ClusterLabs `fence_virsh` forcibly powered the
  source off, Ceph reached zero watchers, Nova declared the service down after
  its configured 720-second threshold, and Nova evacuated BFV server
  `7cbb8e9a-3532-4fb7-b3bc-8e4497bc6b72` to `incus-node-03`.
- The target preserved the rootfs marker, Cinder root attachment, fixed
  Neutron port and exactly one RBD watcher. After independent power-on, the
  old source had no admission token, nova-compute was failed, every local
  workload was stopped, its record was stale, its KRBD mapping was absent,
  and the returning-host ownership audit passed. Explicit admission let
  Nova remove the stale Incus record; the final gate also proved zero source
  KRBD/OVS owners, one destination KRBD/OVS owner, one Cinder attachment and
  the destination Neutron binding.
- The first real attempt exposed the ClusterLabs convention that status
  returns 2 for OFF; the provider now accepts only 0/ON or 2/OFF and rejects
  contradictions. The second attempt exposed `TIMEOUT=600` below Nova's
  `service_down_time=720`; the gate now derives a minimum threshold before
  fencing. Both failures stopped before evacuation and were recovered through
  quarantine, ownership audit and explicit admission.
- The KVM host also exposed a lab-only cold-boot defect: the `.17` 1 TiB
  volume was an extensionless symlinked libvirt volume that AppArmor could
  not reopen after power loss. It was converted without copying data to the
  real file source `/data/libvirt/images/data.qcow2`; the guest's vdb/LVM
  layout remained intact and subsequent fence off/on succeeded. This is
  infrastructure evidence, not an Incus workaround.
- After the successful destructive gate, all three libvirt domains were
  running and all three Nova computes were enabled/up. The strict fleet audit
  passed every Incus 7.2 image/revision, admission, autostart, Ceph,
  Placement, OVN and Cinder check. BFV evacuation is therefore supported with
  shared Ceph and a prevalidated external STONITH provider; physical
  deployments must repeat the gate through their production BMC or PDU.
  The final Python 3.12 regression passed 247 tests with 2 intentional legacy
  pylxd skips; pep8, all shell syntax checks, capability JSON validation and
  the warning-as-error documentation build passed.

- Re-running DevStack to add Cinder rebuilt the test Nova databases. Three
  retained Incus containers (`instance-00000008`, `instance-00000009`, and
  `instance-0000000a`) therefore no longer have Nova database records. Their
  former UUIDs are preserved on `.16` as `user.openstack.orphaned_uuid`;
  `user.openstack.uuid` is intentionally unset so the new nova-compute service
  does not claim them. **Do not present these as current OpenStack instances or
  delete them without explicit approval.**
