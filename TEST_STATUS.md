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

## 2026-08-03 Failed-build idmap claims were never released

Third defect of the same family, also reported from the LB provider:
``Cannot prove terminal failed-build idmap ownership ... retaining its
exact claim``, 22 times in eleven minutes, for claims left by workers
that had failed hours earlier. A retained claim keeps its ID map range
allocated, so later builds keep paying for it.

The cause was one wrong assumption repeated at four layers: **the
failed-build cleanup asked for evidence only a successful build can
produce.** A build that fails before its materialization commits leaves
the claim at ``possible`` with no proof and no storage — exactly the
state the cleanup exists to dispose of. Fixed in ``234fc49``:

1. ``_idmap_claim_instance_name`` required a cleaned claim to learn the
   instance name. Nova's row is authoritative when present; the claim is
   cross-checked only once it can name itself. A purged Nova row still
   requires the cleaned claim.
2. The disposal always requested a release receipt. A claim still at
   ``possible`` after consulting the server has no materialized rootfs
   to release and settles through the materialization abort instead.
3. Attempt parsing required a Ceph identity for the ``clean`` phase.
   ``clean`` only states that nothing is left behind; a volume never
   created cannot be identified. ``materialized`` still requires one.
4. Proof validation required an identity for every reconciled-clean Ceph
   proof. Detach and handover keep that requirement unconditionally; for
   a delete disposition an empty identity means nothing was materialized
   and nothing removed.

Verified on the testbed: both stranded claims released, host claims fell
from eight to six, retained-claim errors stopped. One claim was held at
the last barrier by ``Incus profile still exists; retaining idmap
release intent`` — that barrier was working as designed against a
genuinely orphaned profile, and the claim released on its own once the
profile was removed. Unit suite 788/788.

## 2026-08-03 Two abort-path defects reported from the LB provider

Both were reported by the incus-octavia-provider work, which saw its
load-balancer workers rebuilt in a loop. Neither was a provider defect,
and the two share a shape worth naming: **an abort or cleanup path that
replaces the original error, so only the secondary symptom is visible
during diagnosis.**

- Fork ``88fd0d129`` — ``Invalid RBD image ID: encoding/hex: odd length
  hex string`` killed image-based builds about a minute in. Ceph builds
  an image ID by concatenating an instance ID and a random value
  formatted with ``std::hex``, which does not zero-pad, so odd-length
  IDs are legal and roughly half of all images. Validating with
  ``hex.DecodeString`` rejected them. The ID is only ever compared as a
  string, so byte alignment was never needed; both call sites now check
  for a non-empty lowercase hexadecimal string. The sha256 ownership
  digest keeps ``DecodeString``, where the fixed length makes it right.
  The existing trash test had asserted that ``"abc"`` must be rejected,
  encoding the defect as expected behaviour.
- Driver ``275f176`` — a create slower than the client read timeout left
  its Incus operation running; the abort path then asked Incus to settle
  the attempt, which Incus refuses while the target operation runs, and
  the resulting 409 replaced the original timeout. A recoverable slow
  build became a ``BuildAbortException`` and destroy repeated it. The
  abort now ends the target operation first (cancel when allowed, wait
  until terminal, mirroring ``_settle_instance_migration_operations``),
  and a failure inside the abort no longer masks the build error, since
  Nova's retry decision is made on the exception it receives.

Verified on r9: six sequential builds on incus-node-02, the node that
had logged 27 hex failures in ten minutes, all reached ACTIVE with zero
hex errors fleet-wide. Unit suites: fork storage/instance/incusd green,
driver 784/784.

## 2026-08-03 Manila pre-mount gating and host-reboot recovery

``tools/openstack-incus-manila-gate-recovery-e2e.sh`` (new) passes all
three cases on the three-node testbed, closing the Manila half of the
plan's third item. Migration consistency itself was already proven by
the 2x2x2 live matrix (bfv_manila and bfv_data_manila) and by the CRIU
injection case, where the share followed the instance across the ring
and across a rolled-back failure.

- **gate**: with the destination unable to reach the Manila export
  (NFS blocked toward the controller), ``pre_live_migration`` cannot
  stage the share. The migration fails, the source instance stays
  ACTIVE on its original host with an unchanged guest PID, and the
  destination keeps no share mount and no instance record.
- **retry**: with the block lifted the same migration succeeds and the
  share content is intact on the destination.
- **recovery**: a simulated host reboot (guest force-stopped and the
  host NFS mount lazily unmounted) followed by a nova-compute restart
  re-establishes the mount, resumes the guest, and leaves the share
  writable with its mapping ACTIVE.

The recovery case pins down a semantic worth remembering: host-local
share mounts are re-established only by
``_resume_guests_state -> _mount_all_shares``, which fires when Nova has
to resume a guest that is not running. The Incus share-journal recovery
loop is a different mechanism — it *cleans* journal-only mounts left by
a terminal migration and never remounts under a live guest. An earlier
probe that expected the journal loop to repair a mount lost beneath a
running container was testing a behaviour that was never designed.

## 2026-08-03 initial Cinder data-volume matrix and failed-build rollback

Runtime: the re-addressed static network (10.32.32.128/27, see the
testenv notes), image r8, driver tree at ce3f294. All four matrix cases
of ``openstack-incus-initial-data-volume-e2e.sh`` pass through the
public API — create with data BDMs, guest mkfs + fuse2fs mount + marker
write, hard reboot, cross-boot persistence proof:

- root=local data_volumes=1 and =2
- root=bfv data_volumes=1 and =2

Three harness/guest faults had hidden this since the case was written
(commit e751b2e): stateful containers have no console device (CRIU PTY
exclusion) so markers are now also journaled in the guest and read via
incus exec; this fuse2fs build rejects ``-o rw+`` (rw is its default)
and the mount is now asserted against /proc/mounts so a failed FUSE
mount can never pass the marker round-trip against tmpfs; install -D
creates /usr/local/sbin which the Alpine image lacks.

Failed-build rollback is proven by the new
``openstack-incus-initial-data-rollback-e2e.sh``: a data BDM plus an
image without ``hw_incus_data_volume_fuse`` is refused with the designed
ImageUnacceptable fault, the reserved volume returns to available, no
hypervisor or idmap state leaks, and both resources delete cleanly. The
probe first exposed a real defect (fixed in ce3f294):
ImageMetaProps.get raises AttributeError for unregistered custom
properties, so the absent capability crashed spawn with a raw
AttributeError and three scheduler retries instead of the designed
refusal.

Operator warning from the same session: never delete an Incus instance
record out-of-band for a protocol-protected instance. The receipt,
registry-claim and Nova-metadata ledgers cross-check each other and a
bypassed delete strands them in a mutually-locked state whose only exit
is a Nova DB-level purge (instances.deleted, BDM, instance_mappings,
placement allocation, Cinder reset-state).

## 2026-08-03 CRIU restore-failure injection case passes in full

Runtime: all three nodes on ``incus-quadlet-candidate:776a23411-dirty-
20260803-r8``; the test fleet was rebuilt mid-day by a site outage
(DHCP re-address to .12/.13/.17) whose recovery is itself recorded
below. Log: node01 ``/tmp/lminject10.log``.

The case (``INJECT_RESTORE_FAILURE=1`` on bfv_data_manila): bind-mount
``/bin/false`` over criu on the target, live-migrate, then require
libvirt-like semantics — the failure must leave the workload as if
nothing happened, and an immediate retry must succeed.

- PASS injected live-restore failure rolled back to incus-node-01:
  migration record failed, source container restored from its
  checkpoint with the original PID, guest counter continued, target
  fully fenced (no instance, no profile, no krbd mappings, no staging,
  attempt settled).
- PASS the same instance then completed the three-hop ring
  01 -> 02 -> 03 -> 01: PID 282 held, counter 39 -> 367, Cinder data
  volume and Manila share followed, residual-state audit clean. The
  second hop leaves the previously-fenced host, proving no poisoned
  state survives a fenced failure.

Six defect layers were peeled to get here, each with its own commit and
regression coverage (fork / driver):

1. Durable profile markers aborted migrations when the backup.yaml
   resync failed ("profile change still saved") — driver ``079513c``.
2. backup.yaml refresh mounted volumes mid-handover or on pool-less
   records — fork ``bbb222ed3`` + ``1b47f4221``.
3. A failed migration receive leaks the volume mount and its reference
   count; the detached claim release refused forever and fencing
   deadlocked into the ambiguous-ownership stop — fork ``39df936f0``.
4. finalize_live_migration_rollback (restart source from checkpoint,
   restore ownership, reassert VIFs) was never wired into the rollback
   — driver ``48ef103``.
5. finalize's destination-acknowledgement barrier treated a mid-cleanup
   profile as terminal instead of not-ready — driver ``af7b2e6``.
6. The forced release left the stale in-memory reference count behind,
   poisoning the next outbound migration from that host ("Failed
   releasing source root volume after CRIU checkpoint: In use") — fork
   ``3301d99d7``.

Also fixed in the harness: wait_migration now judges the newest
migration Id, since the injected failure stays in the server's
migration history.

### Site outage recovery evidence (same day)

The full-fleet reboot with DHCP re-addressing exercised the recovery
posture designed into the system, all of which held:

- Returning-host admission quarantined all three computes
  ("admission token missing"); the ownership audit passed on each and
  the hosts were explicitly admitted (disable -> audit -> admit ->
  enable).
- The Geneve prefsrc netplan pin from 2026-07-26 kept tunnel routes
  correct through the address change.
- The idmap registry etcd bound to old DHCP addresses and could not
  form quorum. Repaired without data loss via 0.0.0.0 listeners and
  hostname peer URLs (certificates carry DNS SANs; /etc/hosts pins the
  stable service addresses), then the registry was returned to its
  pure-config zero baseline: all orphan claims and slots from the
  pre-fix failure runs were backed up
  (node01 /root/idmap-registry-backup-20260803T052220.tsv) and removed.
- Manila's LVM backing loop device does not survive reboot; a
  ``manila-lvm-loop.service`` unit now reattaches it before m-shr.
  Residual resource locks and a wedged queued_to_deny rule from dead
  instances had to be removed before shares worked again.
- Operator procedures validated: two-step attempt settlement
  (PUT aborted, then PUT settled), receipt-bound instance DELETE with
  the four A/H/T/U query parameters, and orphan source records whose
  volume was legitimately deleted require stripping the OpenStack
  provenance keys (possible only since the r6 key registration) before
  a plain delete.

## 2026-08-03 BFV live-migration matrix complete (backup.yaml handover fix)

Runtime: all three nodes upgraded to
``incus-quadlet-candidate:776a23411-dirty-20260803-r5`` (r4 + the fork
backup.yaml handover gate, fork commit ``bbb222ed3``). Podman deployment
trap: ``docker save`` tarballs load as ``docker.io/library/...`` while the
quadlet references ``localhost/...``, so every ``podman load`` must be
followed by ``podman tag`` or the service loops trying to pull from a
nonexistent localhost registry.

The last BFV live-migration blocker was misdiagnosed until the Incus error
text was read carefully: ``The following instances failed to update
(profile change still saved)`` means the profile change — the durable
cleanup-recovery marker Nova writes in ``post_live_migration`` — had
already persisted in the Incus database. Only the per-instance
``backup.yaml`` resync failed, because it mounts the root volume while the
migration destination holds the sole RBD watcher. backup.yaml is a
convenience copy of database state, so the fix is to not write it during
the handover window, exactly as libvirt never writes domain metadata into
shared volumes mid-migration:

- Fork ``bbb222ed3``: ``instance.StorageHandoverInProgress`` (any of the
  handover / receive-complete / delete-protection markers) gates both the
  lxc and qemu ``UpdateBackupFile``; a recoverably stale backup.yaml is
  preferred over mounting storage whose authoritative owner may be
  remote — the same contract ``DeleteInstance`` already follows.
- Driver ``54b97a0``: the source ``MIGRATION_OPERATION_KEY`` profile write
  in ``live_migration`` is skipped for shared-storage roots
  (``_live_migration_shares_root_storage``, keyed on the root pool driver
  ceph/cephext, failing open to the write). The key is a redundant hint:
  ``_settle_instance_migration_operations`` enumerates operations itself.

With both layers deployed the full BFV half of the 2x2x2 matrix passed in
sequence on the three-node ring (01 -> 02 -> 03 -> 01, all-confirm,
residual-state audit clean, logs node01 ``/tmp/lmbfv5.log`` and
``/tmp/lmbfv6.log``):

- ``bfv_basic``: PID 653 held, counter 9 -> 266.
- ``bfv_data`` (one Cinder data volume): PID 653 held, counter 22 -> 296,
  volume followed.
- ``bfv_manila`` (one Manila share): PID 282 held, counter 19 -> 276,
  share followed.
- ``bfv_data_manila``: PID 282 held, counter 36 -> 313, volume and share
  both followed.

Together with the 2026-08-02 local half this re-proves the complete
2x2x2 live-migration matrix on the current candidate. Housekeeping: the
two obsolete Glance images (``alpine-3.21-cloud-incus-criu`` and
``alpine-3.21-cloud-incus-criu-bfv-raw``) are deleted; the published set
is ``alpine-3.21-cloud-incus-criu-fuse`` and ``alpine-3.21-criu-bfv-fuse``.
Matrix invocations from node01 need ``SSH_IDENTITY=/root/.ssh/
openstack-incus-test`` and a sourced admin openrc.

Still open from the 2026-08-02 entry: the stale-migration-attempt
reservation leak (blocks the ``INJECT_RESTORE_FAILURE=1`` evidence), the
stranded cleanup-token recovery gap, and the initial-data-volume console
marker failure.

## 2026-08-02 P0 three-node fault validation (candidate still NO-GO overall)

Runtime: all three nodes on locally built image
``incus-quadlet-candidate:776a23411-dirty-20260802-r4`` (merged upstream +
fork patches + empty-metadata parse fix + GNU coreutils for ``rbd trash
mv``); driver tree deployed from this worktree; shared root pool recreated
on every node with distinct ``ceph.rbd.image_prefix`` (node01-/node02-/
node03-) after discovering the prefixes had been lost in the 2026-07-26
rebuild. Registry: dedicated TLS etcd, namespace ``region-one-cell1``.

Passed with archived logs (node01 ``/tmp/matrix-run4.log`` and task logs):

- ``tox -e py312,pep8``: 500 driver/manager/idmap unit tests + 156 script
  contract tests, all green on the exact deployed tree.
- ``openstack-incus-idmap-conflict-e2e.sh`` on all three nodes:
  destination rejects an overlapping isolated idmap without persisting
  state. Full spawn->delete lifecycle proven around it; etcd registry
  returned to its 2-orphan baseline afterwards (zero leakage).
- ``openstack-incus-bfv-delete-protection-e2e.sh`` across all three
  computes: Nova delete released but never deleted the Cinder root
  (immutable image and pool IDs unchanged, zero attachments/watchers/
  instances/profiles/mappings fleet-wide); Cinder delete then removed the
  exact ``rbd_header``. Requires the re-published BFV image ``7e55e290``
  carrying the ``.incus-idmap`` provenance marker.
- ``openstack-incus-ceph-exact-delete-aba-e2e.sh`` (node01 orchestrator):
  dependent-clone delete failure -> identity tombstone -> same-name
  replacement B -> retried delete removed exactly A, preserved B, and the
  v2 receipt digest replayed idempotently.
- ``openstack-incus-ceph-ownership-migration-matrix.sh`` cold case:
  node01->node02 confirm, ->node01 revert, ->node03 confirm; CRIU-visible
  PID and counters continuous, managed root RBD ID stable across all
  three ownership transfers.
- Interrupted exact-delete recovery: a delete killed mid-flight (receipt
  pending + tombstone + no instance record) converged on retry once GNU
  date was present; pool returned to pristine.

Live migration re-proven on the current candidate (2026-08-02, after the
interception fix):

- ``local_basic``: three-hop ring incus-node-01 -> 02 -> 03 -> 01, PID
  held at 649 across every hop, guest counter 19 -> 274, managed root
  RBD ID unchanged, residual-state audit clean.
- ``local_data`` (one Cinder data volume): same three-hop ring, PID held
  at 653, counter 36 -> 330, the data volume followed the instance, root
  RBD ID unchanged, residual audit clean. This restores the capability
  the 2026-07-19/20 matrix had proven and the 2026-07-22 interception
  commit had silently destroyed.

Two test-bed defects had to be cleared to get there, neither of them in
driver code:

- The Alpine test image had no ``fuse2fs``, so the online-attach probe
  added by the same 2026-07-22 commit rejected every data volume. In
  Alpine ``fuse2fs`` is its own package, not part of
  ``e2fsprogs-extra``. Rebuilt and published as
  ``alpine-3.21-cloud-incus-criu-fuse``; the publish script only
  understood ``apt-get`` and now has an ``apk`` branch, and it stamps
  ``hw_incus_data_volume_fuse=true`` automatically once the binary is
  present.
- ``incus-volume-journal`` on incus-node-02 was owned by ``root`` with
  mode 755 while nova-compute runs as ``stack``, so every data-volume
  attach failed with ``Permission denied``. All three nodes are now
  ``stack:incus-admin`` mode 700. This is exactly the kind of silent
  environment drift that belongs in the fleet preflight.

Known open items from this run:

- **Stale migration attempts are never reclaimed, and they compound.**
  When a live migration fails during pre-check or scheduling, nothing
  calls the attempt abort path, so the attempt stays at
  ``state=active, finished=0`` forever. Because
  ``GetMigrationAttemptIDMapReservations`` filters only on
  ``finished = 0``, that isolated ID-map range stays reserved
  permanently and every later migration of any instance whose range
  overlaps is rejected with ``Migration attempt idmap reservation
  overlaps another attempt``. Each failed migration locks one more
  range. These records are also invisible to operators: ``GET
  /1.0/migration-attempts/<token>`` answers ``not found`` for them, so
  they can only be listed by reading the node database directly
  (``sqlite3 /var/lib/incus/database/local.db "SELECT token,
  resource_name, state, finished, idmap_base FROM migration_attempts
  WHERE finished = 0;"``). Retire them through the protocol endpoint
  (``incus query -X PUT -d '{"state":"aborted"}'
  "/1.0/migration-attempts/<token>?project=nova"``), never by deleting
  rows. Fix directions: use the already-present ``daemon_start`` column
  (currently always 0) to treat attempts from a previous daemon
  generation as dead; abort the attempt on every pre-check failure
  path; and make the GET/list endpoints show these records so
  ``openstack-incus-monitoring-audit.sh`` can scan for them.
- **Unconditional syscall interception breaks CRIU live migration for
  every instance (regression, release blocker).**
  ``flavor._data_volume_mounts`` is in ``_CONFIG_FILTER_MAP`` with no
  gate, so every Nova Incus instance is created with
  ``security.syscalls.intercept.mount=true``. After a CRIU restore LXC
  cannot re-attach the seccomp notify proxy (``Failed to add seccomp
  notify handler for 7 to mainloop`` -> ``lxc_poll: Failed to setup
  seccomp proxy``) and the container is torn down, which Nova reports
  as ``CRIU-restored Incus instance is not running``. The timeline is
  conclusive: the live migration matrix passed on 2026-07-20 and the
  interception was introduced on 2026-07-22 by ``611ca3d Require FUSE
  mounts for Cinder data volumes``. Proven by experiment: unsetting
  ``security.syscalls.intercept.mount`` and ``.mount.fuse`` on one
  instance and restarting it made the same live migration succeed in
  51 s, ACTIVE on incus-node-02, process and IP preserved, source
  clean. Note that the ``ipv4/ipv6: Address already assigned`` lines in
  the restore log are harmless noise about loopback; CRIU itself
  reports ``Restore finished successfully. Tasks resumed.``
  Fix requires deciding when interception is enabled — see the open
  design question below.

Correction to an earlier version of this entry: it claimed that this
project refuses live migration for instances with Cinder volumes and
proposed gating interception on the presence of data volumes. That
premise is wrong. ``check_can_live_migrate_source`` calls
``_validate_live_migration_data_volumes``, which *validates* that each
``unix-block`` profile device matches a Nova data BDM rather than
rejecting it, and the 2026-07-19/20 evidence below records a complete
2x2x2 matrix passing with one Cinder data volume and one Manila share
per case. Live migration with attached data volumes is a supported,
previously proven capability; the AGENTS.md text about refusing Cinder
volumes describes the original minimal scenario of the first
implementation, not current behaviour.

The real shape of the problem:

- Seccomp mount interception and CRIU live migration are mutually
  exclusive for a given container, because LXC cannot re-attach its
  notify proxy to restored processes. Interception is a convenience so
  that tenants can use plain ``mount -t ext4`` and ``/etc/fstab``; the
  security contract is satisfied either way, because in both cases the
  ext4 metadata is parsed by ``fuse2fs`` in userspace and never by the
  host kernel.
- Before 2026-07-22 no instance had interception, guests ran
  ``fuse2fs`` explicitly as AGENTS.md documents, and live migration
  with data volumes worked. Enabling interception for every instance
  traded that proven capability for the convenience, silently and
  fleet-wide.
- The fix therefore should restore the default rather than narrow it:
  do not enable interception by default, and if the convenience is
  wanted for a particular workload, make it an explicit opt-in (Flavor
  extra spec or image property) whose documented consequence is that
  the instance cannot live migrate. Enabling it per instance also has
  to happen at creation, because seccomp settings only apply at
  container start.
- The matrix live case with ``INJECT_RESTORE_FAILURE=1`` has therefore
  still not produced its evidence, but the two blockers that hid the
  real behaviour are now understood (the reservation leak above and the
  conductor deadlock below).

Test-bed configuration requirement discovered by this run:

- **``[conductor] workers`` must be at least 2 (we set 4).** DevStack
  defaults to ``workers = 1``. The conductor blocks synchronously
  waiting for ``check_can_live_migrate_destination``; the destination
  compute then runs ``_claim_pci_for_instance_vifs`` ->
  ``instance.get_network_info()``, whose lazy load has to travel back
  to the conductor over RPC because compute processes run with
  ``DISABLE_DB_ACCESS = True``. With a single worker that reply can
  never be served, both sides wait out ``rpc_response_timeout``
  (60 s by default), the conductor logs ``Skipping host: Timeout while
  checking if we can live migrate``, and after all hosts are tried the
  request fails with ``NoValidHost``. Cold migration is unaffected
  because it does not nest synchronous RPCs this way. Applied on
  incus-node-01 in both ``nova.conf`` and ``nova_cell1.conf`` (originals
  kept as ``*.before-conductor-workers``) and reflected in
  ``devstack/local.conf.sample``. This is a test-bed configuration
  defect, not a driver defect, but any validation that exercises nested
  synchronous RPC needs it.
- Eleven defects were found and fixed by this validation (spawn attempt
  journal threading, empty RBD image-meta parse, final-delete release
  lock self-deadlock, versioned protocol extension names in production
  preflight, busybox date breaking ``rbd trash mv``, release-ACK receipt
  type mismatch, Cinder encryption metadata false positive, printf
  continuation argument splitting in three e2e helpers, ``jq fromjson``
  misuse, unqualified ``rbd clone`` destination pool, and the shared-Ceph
  destination preflight reading redacted pool config through the
  restricted identity). The preflight now defers exact cluster/source
  equality to the Incus migration negotiation whenever the destination
  redacts pool config, and rejects driver-class mismatches outright.
- etcd registry carries two pre-existing orphans (allocation
  ``b04e0f9e`` slot 106 with no claims; proof-less claim ``b87f9694``
  slot 1747). Both are artifacts of pre-fix code, block nothing, and
  require the frozen-fleet registry CLI to retire.
- node02 retains the stale ``incus-ceph-node02`` pool whose OSD pool
  deletion needs Ceph admin-plane credentials.

## 2026-07-31 current candidate release evidence (NO-GO)

- The current worktree contains the new shared-Ceph fencing/idmap changes,
  initial-data-volume and BFV-snapshot public API scripts, Manila destination
  staging and rollback changes, Cinder recovery journals, inventory
  performance work, and the fail-closed 100/500/1,000 scale runner.
- Unit, static, documentation, and script-contract results do not replace
  destructive evidence for the exact candidate. At the time of this entry the
  current candidate has not yet completed the three-node public API E2Es, the
  BFV fault matrix, the complete local/BFV + Cinder + Manila migration
  matrices, the full Tempest selection, or the real 100/500/1,000 concurrent
  instance run.
- Historical results below remain useful regression context, but they do not
  authorize a production release of this candidate. The release decision
  remains ``NO-GO`` until the aggregate gate archives failure-free evidence
  and exact-ID cleanup for all required phases.
- Cinder recovery journals deliberately omit credentials. Automatic
  crash-recovery evidence applies to the production Ceph RBD connector with a
  reusable, protected host keyring. Non-RBD connectors that depend on
  expiring or one-time ``connection_info`` secrets remain unsupported until
  connector-specific credential reacquisition and crash recovery are proven.
- The release gate now requires runtime namespace inspection of every listed
  ``nova-api`` replica and every Incus ``nova-compute`` before Manila
  migration evidence can run. It also requires real NFS and CephFS shares,
  snapshot-capable types, and a successful public-API snapshot/restore cycle
  on both backends. These checks have not yet run against the current
  candidate, so their presence does not change the ``NO-GO`` decision.

## Dedicated test topology

- `root@10.224.0.15` (`incus-node-01`) is the DevStack controller and first
  Incus compute node. It runs Ubuntu Noble 24.04 and Python 3.12.
- `root@10.224.0.17` (`incus-node-02`) is the second Incus compute node. It
  runs Ubuntu Noble 24.04 and Python 3.12.
- `root@10.224.0.16` (`incus-node-03`) is the third Incus compute node. It
  runs Ubuntu Noble 24.04 and Python 3.12.
- Stable hostnames and `/etc/hosts` self-resolution are configured on all
  three nodes as shown above.
- All three nodes run independent, non-clustered Incus 7.2 daemons and expose
  migration HTTPS only on their management address. Nova, Placement and
  Neutron/OVN own placement and tenant networking.
- Remote unit-test runs must set `INCUS_SDK_PATH=/opt/incus-python-sdk-src`
  (the synchronized SDK path); the default sibling path remains correct for
  local checkouts.

### Current storage substrate

- The current Noble test nodes do not have `/dev/vdb`; the earlier
  LINSTOR/DRBD topology was removed when `.21` and `.22` were reinstalled.
  LINSTOR results later in this file are historical evidence only.
- The external Rook cluster supplies `glance-images-rbd-pool`,
  `cinder-volumes-rbd-pool`, `nvme-rep3-rbd-pool`,
  `cinder-backups-rbd-pool`, and the Incus rootfs pool. Glance, both Cinder
  volume backends, Cinder Backup, and every Incus compute use pool-scoped
  CephX clients.
- Every compute exposes two dynamic BFV mappings:
  `cinder-volumes-rbd-pool:cinder-bfv` and
  `nvme-rep3-rbd-pool:cinder-nvme-bfv`. The restricted `nova-preflight`
  project publishes the same mapping as JSON for authenticated destination
  readiness checks.
- Every compute also has an independent 7.5-GiB `incus-local-zpool` exposed as
  `local-zfs`. It is intentionally non-HA and exercises Flavor-selected local
  roots, capacity inventory, snapshot export and cross-node restore.
- `/var/lib/incus` and `/var/log/incus` remain bounded by dedicated loop-backed
  filesystems in this test topology. Production requires dedicated partitions,
  LVs, or equivalent hard quotas rather than loop files.

### Current release-candidate warning

- The 2026-07-21 shared-Ceph live-handover candidate is running through a
  temporary read-only bind of the locally built `incusd` binary on all three
  nodes. The previously approved GHCR digest below does not contain that final
  patch. It remains historical evidence and must not be promoted as the next
  production digest.
- Final release admission still requires review, an explicit user-approved
  commit/push, GHCR rebuild, digest-pinned three-node rollout, and a clean fleet
  preflight against the new Incus revision.

### 2026-07-21 release-candidate evidence

- The complete 2x2x2 live-migration matrix passed on `.21`, `.17`, and `.22`:
  Incus-managed Ceph or Cinder BFV root, zero or one Cinder data volume, and
  zero or one Manila share. Every case completed `.21 -> .17 -> .22 -> .21`,
  preserving PID/CRIU state, increasing guest counters, root and data content,
  Manila content, OVN ownership, and baseline-equal cleanup.
- A Flavor-selected local ZFS root completed the same three-hop test through
  the non-shared copy path. A second Cinder backend
  (`nvme-rep3-rbd-pool:cinder-nvme-bfv`) completed BFV three-hop shared-Ceph
  migration, proving dynamic volume-type-to-cephext-pool selection.
- The Incus-owned Ceph live-handover rollback path was failure-injected on the
  first target. The target released its claim without deleting the RBD root,
  the source restored its checkpoint with the original PID and increasing
  counter, and the immediate `.21 -> .17 -> .22 -> .21` retry passed with the
  same RBD image ID and clean final deletion.
- Cinder data-volume and BFV-root full/incremental backup and restore passed.
  The restored BFV root booted on another compute and retained its marker.
  Incus-managed Ceph and local-ZFS roots passed Nova snapshot-to-Glance and
  cross-compute restore. Manila snapshot, create-from-snapshot, reattach and
  marker recovery passed. Glance-RBD to Cinder-RBD provisioning retained a
  real RBD parent snapshot and non-zero overlap rather than copying the image.
- A destructive BFV evacuation powered off `.17` through the external
  hypervisor fence provider, waited for Nova's 720-second service-down gate,
  proved zero source RBD watchers, evacuated to `.22`, and verified a non-empty
  rootfs marker. On return, `.17` remained quarantined with nova-compute
  stopped; the ownership audit proved the old record stale, the Cinder
  attachment and OVN binding unique, and the target the sole watcher. Explicit
  admission removed the stale record, restored the compute heartbeat, removed
  `COMPUTE_STATUS_DISABLED` from Placement, and returned all three services to
  `enabled/up`.
- That exercise corrected two test/recovery defects: stdin-bearing `incus file
  push` no longer uses SSH `-n`, and returning-host scheduling is enabled only
  after the compute heartbeat is up, followed by an explicit Placement-trait
  eligibility check.
- `tox -e py312` passed 317/317 tests, `tox -e pep8` passed, Sphinx completed
  with `-W --keep-going`, changed shell scripts passed ShellCheck, all seven
  Incus storage/migration Helm overrides passed lint and combined rendering,
  and all four worktrees passed `git diff --check` (line-ending notices only).
- The exact uncommitted Incus fork diff was overlaid on a clean
  `057da9998` checkout on Linux. `gofmt -d` was empty, the complete
  `internal/server/instance/drivers` package test suite passed, and
  `go build ./cmd/incusd` succeeded.
- After synchronizing the authoritative driver tree, the three-node fleet
  preflight passed with identical driver hashes, every nova-compute
  `enabled/up`, Nova-compatible `hypervisor_type=lxc`, valid Placement
  inventory/traits, live OVN controllers, both BFV mappings, and the current
  digest/revision pair. This proves the running test baseline is internally
  consistent; it does not supersede the immutable-image warning above.
- Rebooting `.17` also proved why the pending immutable image rebuild is a
  release gate: the old image lost the test-time `zpool` installation and Nova
  resource reporting failed closed. The candidate Dockerfile installs and CI
  verifies `zfs`/`zpool`; the new digest must be rebuilt and rolled out before
  release approval.

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
- On 2026-07-18 the post-Manila and interactive-console targeted suites
  passed on `.21`: all 173 `test_driver` tests and all four `test_console`
  tests passed. The signature audit caught and removed an extra `share_info`
  argument from `spawn()`, restoring an exact stable/2026.1
  `ComputeDriver.spawn` contract.

## Manila share lifecycle

- Manila stable/2026.1 was deployed on `.21` with an LVM-backed NFS share.
  Nova compute API microversion 2.97 attached share
  `50d66111-edaf-44a4-80e7-7bf51c0d1c51` to an Incus instance on `.17`.
  The mapping reached `inactive`, server start changed it to `active`, and
  Manila installed an `rw` IP access rule for the compute host.
- The first run exposed two deployment requirements now enforced by the
  plugin and production preflight: every compute needs a complete `[manila]`
  keystoneauth group, and a Podman-hosted incusd needs the dedicated
  `incus-shares` subtree at the same path with `rw,rslave` propagation. The
  parent Nova `instances_path` remains read-only.
- After correction, the container saw a real `rw,nosuid,nodev` NFS mount,
  wrote `MANILA_E2E_20260718`, and the same data was read from the Manila
  backend. Stop plus API detach removed the Nova mapping, Manila access rule,
  Incus device, host mount, and staging directory.
- `tools/openstack-incus-manila-e2e.sh` repeated the complete attach, start,
  guest write, stop, detach, and cleanup workflow and returned `PASS` on
  2026-07-18.

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
- At this checkpoint cold migration still remained experimental while
  post-claim failure injection was incomplete. On 2026-07-17 the BFV source
  added a destination TCP/8443
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
  leaving data KRBD mappings on both computes. `IncusDriver.cleanup()` now
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
  `nova_incus_tempest_plugin.tests.scenario.test_volume_ops`. On 2026-07-16 both
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
  active unit tests, pep8 and Sphinx `-W`. Two obsolete modules for the
  removed ``pylxd.deprecated`` migration/session API were skipped at that
  historical checkpoint and have since been deleted with their unreachable
  production session wrapper.

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
- Microversion 2.48 reported `driver=incus`, `hypervisor=incus`, running state,
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
- On 2026-07-19, the current Python 3.12 regression passed all 261 collected
  tests: 259 passed and the same 2 legacy pylxd migration/session tests were
  intentionally skipped. Flake8, all Bash syntax checks, capability JSON
  validation, ``git diff --check``, and the warning-as-error Sphinx build
  passed.
- Tempest's ``test_server_basic_ops`` passed against the public Nova API.
  The two stock volume scenario tests skipped because SSH validation is
  deliberately disabled in this lab; dedicated project E2E tests remain the
  authority for Cinder data-volume and BFV behavior. The DevStack Tempest
  feature flags now advertise the verified change-password, pause, and
  console-output operations while explicitly disabling unsupported suspend,
  rescue, and upstream serial-console scenarios.
- The Nova 2.97 Manila share API was exercised end to end with the Manila LVM
  NFS backend: attach mounted the share inside a running Incus system
  container and detach removed it. The automated
  ``tools/openstack-incus-manila-e2e.sh`` gate passed. All Incus Quadlets now
  expose the dedicated Nova share staging directory as ``rw,rslave`` while
  retaining the parent instances path as read-only.
- The final three-node fleet audit initially rejected a missing migration
  recovery/TLS configuration on ``incus-node-01`` and different driver hashes
  on ``incus-node-02`` and ``incus-node-03``. After restoring the controller
  compute settings, synchronizing the authoritative driver tree, and
  restarting all three compute services, the complete audit passed. All
  nodes now report driver hash
  ``185719734fe282f8cc645d89d42d93cca21e00588b744892534175a8d02bcd1d``,
  Incus 7.2 image digest ``sha256:cc8b7109...239bd72d``, revision
  ``5adcaca1...f24ba5``, current admission tokens, enabled/up Nova services,
  live OVN controllers, valid Placement inventories and traits, and the
  enabled/up Cinder Ceph backend.

- Re-running DevStack to add Cinder rebuilt the test Nova databases. Three
  retained Incus containers (`instance-00000008`, `instance-00000009`, and
  `instance-0000000a`) therefore no longer have Nova database records. Their
  former UUIDs are preserved on `.16` as `user.openstack.orphaned_uuid`;
  `user.openstack.uuid` is intentionally unset so the new nova-compute service
  does not claim them. **Do not present these as current OpenStack instances or
  delete them without explicit approval.**

## 2026-07-19 CRIU live migration

- The approved Alpine no-VM image contains CRIU 4.2 and passed
  ``criu check --extra`` inside the outer Podman container; CRIU is not
  installed on the compute host.
- A real Nova API migration initially restored every process successfully but
  Incus reported the target as stopped. The CRIU log ended with
  ``Restore finished successfully. Tasks resumed.`` and the guest counter kept
  increasing. The target LXC monitor used the bare instance name while the
  non-default ``nova`` project queried its project-qualified name.
- Incus commit ``20c12bce3`` passes the project-qualified name to
  ``forkmigrate``. Together with ``826c25cd9``, which normalizes mixed
  namespace ownership in received CRIU images, the fix is published in
  ``ghcr.io/fivetime/incus:alpine-novm`` digest
  ``sha256:7e0a91bf7f52311d82276ccb81f90bde39d8482d5485c6f258ba99cdfc3b1807``.
- Nova API plus Neutron/OVN migration passed from ``incus-node-02`` to
  ``incus-node-03`` and back. Both runs preserved guest PID ``644``, advanced
  the persistent counter from ``2`` to ``47``, moved the Nova host and
  Neutron binding, verified the OVN-installed OVS interface only on the
  destination, and removed the instance, profile, Neutron port, OVS interface,
  and Placement allocation after deletion.
- Ten targeted Python 3.12 unit tests passed for project-scoped cleanup,
  migration URL construction, isolated idmap propagation, source force-stop,
  failure recovery, and normal power-on behavior. ``bash -n``,
  ``py_compile``, and ``git diff --check`` also passed.
- The complete focused Python 3.12 regression subsequently passed all 217
  Incus client, flavor, and driver tests. All three compute hosts reported no
  host-installed CRIU binary, while the identical outer image exposed CRIU
  4.2 at ``/usr/local/sbin/criu``. A missing-destination-CRIU pre-check kept
  source PID ``644`` running with counter progress from ``3`` to ``13`` and
  left no target instance, profile, or OVS interface.
- Incus-managed rootfs plus one Cinder Ceph data volume passed native Nova
  live migration from ``incus-node-01`` to ``incus-node-02`` and from
  ``incus-node-02`` to ``incus-node-03``. Both runs preserved guest PID
  ``644``, advanced the process counter, recreated the destination os-brick
  mapping, preserved a raw-block marker, moved the Cinder attachment and
  Neutron binding, and cleaned the server, volume, Incus, RBD, OVS, Neutron,
  and Placement resources.
- Removing the destination Cinder CephX keyring forced destination volume
  preparation to fail. The source remained ``ACTIVE`` on ``incus-node-02``;
  its counter advanced, raw-block marker remained intact, and its sole Cinder
  attachment remained authoritative. The corrected rollback left no target
  instance, profile, RBD mapping, or OVS interface.
- Support remains opt-in and best-effort. Passing pre-checks proves compatible
  infrastructure, not that every tenant process or external resource can be
  checkpointed. BFV, Manila shares, privileged containers, config drives,
  encrypted/read-only/multiattach volumes, and unsupported extra devices
  remain rejected.

## 2026-07-19 complete live-migration matrix

- The earlier BFV and Manila rejection statement above is historical. The
  current driver supports shared-Ceph BFV roots, Nova-managed Cinder data
  volumes, and active Manila mounts during conditional CRIU live migration.
- ``tools/openstack-incus-live-migration-matrix.sh`` passed all eight
  local/BFV-root, absent/present Cinder-data-volume, and absent/present
  Manila-share combinations. Every case completed
  ``node01 -> node02 -> node03 -> node01``, preserved the guest PID and
  increasing counter, moved the Neutron/OVN owner, preserved root/data/share
  contents, and restored the Nova and Cinder inventories to their baselines.
- The strict maximum case used a BFV root, two Cinder data volumes, and a
  Manila share. A target-side CRIU restore failure was injected and observed
  in the target Incus log; Nova restored the running source with the same PID
  and advancing counter, removed target RBD/share/OVS ownership, and an
  immediate retry then succeeded.
- The retry exposed an optional CRIU pre-dump failure. Incus revision
  ``80ba579c257e034d049d855b9173e06c73aa7e09`` now ends pre-copy cleanly and
  falls back to a full final checkpoint. The focused Go driver package test,
  all 308 Python tests (306 passed and two documented legacy suites skipped),
  flake8, capability JSON validation, targeted ShellCheck, and
  warning-as-error Sphinx build passed.
- GHCR image
  ``ghcr.io/fivetime/incus@sha256:25b57d845276773ca219ae3f9dc0e0da3db7262dc0f2308d53c7fb9b7ac48088``
  embeds that exact Incus revision. All three computes run the immutable
  digest and independently passed the production preflight, including Incus
  7.2, GNU tar ``--no-unquote``, migration extensions, Ceph, Manila,
  AppArmor, cgroups, admission, TLS, dedicated control filesystems, and Nova
  service checks.
- The final independent residual audit found no Nova servers, Cinder volumes,
  compute Neutron ports, Placement allocations, Incus instances or non-default
  profiles, KRBD mappings, instance-level Manila staging mounts, or OVS tap
  interfaces. The controller's Manila service-backend LVM mount is expected
  infrastructure and was not counted as an instance mount. Historical
  resources without a Nova, Neutron, or Cinder owner, including one final
  orphaned OVS veth pair on node01, were identified and removed before the
  clean audit.

## 2026-07-20 attachment-cardinality live-migration matrix

- The E2E runner now accepts arbitrary-length ``DATA_VOLUME_COUNT`` and
  space-separated ``MANILA_SHARES``/``MANILA_TAGS`` lists. It verifies every
  volume marker, share marker, host staging mount, RBD mapping, OVN owner and
  cleanup operation independently on every hop.
- The first local-root + three-data-volume + three-share run exposed a real
  multi-share detach bug: removing the first share treated the non-empty
  per-instance parent as an error. The driver now ignores only the expected
  ``ENOTEMPTY``/``EEXIST`` parent result while preserving all share-local
  unmount and removal failures. A focused regression test covers this case.
- ``tools/openstack-incus-live-migration-cardinality-matrix.sh`` passed all
  18 local/BFV-root x 0/1/3 Cinder-data-volume x 0/1/3 independent-Manila-share
  combinations. Each case completed
  ``node01 -> node02 -> node03 -> node01`` with PID continuity, increasing
  state, per-resource data preservation and baseline-equal Nova, Cinder,
  Neutron and Placement inventories: 54 successful live migrations.
- BFV + three Cinder data volumes + three Manila shares also passed a
  target-side CRIU restore failure injection, source rollback with the same
  PID, per-resource destination cleanup, and immediate retry.
- The final post-change suite ran 308 Python tests with no skips or failures.
  Bash syntax, ShellCheck, three-node Incus/RBD/Manila/OVS residue audits and
  the OpenStack allocation audit passed.
- The complete 18-case matrix was repeated after the compute-node reboot and
  failed-host evacuation exercise. All 54 live migrations passed again, and
  the final Nova server, Cinder volume, Neutron port, Manila access/lock,
  Incus instance, host NFS mount and Placement allocation inventories were
  empty or baseline-equal. The rerun also proved two deployment preconditions:
  a compute's fixed management address must exist before Incus binds its
  migration listener, and a loop-backed Manila LVM test backend must restore
  its loop device and activate its VG before ``manila-share`` starts. These
  were test-infrastructure failures, not migration-driver failures.

## 2026-07-20 30-instance failed-host evacuation matrix

- Thirty 1-vCPU/512-MiB BFV system containers with independent Cinder RBD
  roots were created on ``incus-node-02``. While its Nova service remained
  up, all 30 native ``openstack server evacuate`` requests returned HTTP 400
  ``Compute service ... is still in use``. All servers stayed ACTIVE on the
  source with one attachment and one watcher per root.
- The independent KVM host then powered the source domain off through
  ``fence_virsh``. Nova reported the service down and direct RBD-header
  queries proved zero watchers before any evacuation was submitted.
- The first target attempt exposed a missing production Ceph permission:
  Cinder volumes were RBD clones of Glance images, but ``client.cinder`` had
  no access to the Glance parent pool. It now has only
  ``profile rbd-read-only pool=glance-images-rbd-pool`` in addition to its
  existing Cinder-pool profile. The same credentials on ``incus-node-03``
  then opened the parent chain, and all 30 retries became ACTIVE there.
- Every recovered root preserved its unique marker. The final target state
  had 30 unique Cinder attachments and exactly one RBD-header watcher per
  volume. Watcher gates now query ``rbd_header.<image-id>`` with ``rados``;
  they no longer treat ``rbd status`` parent-pool ``EPERM`` as zero watchers.
- When the old source domain was powered on, its DHCP address changed from
  ``10.224.0.17`` to ``10.224.0.23``. This proved the production requirement
  for static management addressing or DHCP reservations. At the actual
  address, the admission token was absent, nova-compute was failed, all 30
  stale Incus records were STOPPED, and none had a local KRBD mapping.
- The returning-host audit passed all 30 records: Nova and Neutron named
  ``incus-node-03``, each root had one matching Cinder attachment and target
  watcher, and the source had no mapping. Explicit admission then started
  nova-compute, Nova removed all 30 stale source records, and the destination
  remained the sole owner. All temporary servers, volumes, Flavor and quota
  changes were removed after the test.
- The final fleet audit passed all three hosts. Its driver hash now
  normalizes CRLF to LF before hashing, so equivalent Windows-deployed Python
  files do not create false drift alerts while any code-character change
  remains detectable.

## 2026-07-27 concurrent-operation destroy E2E

- A disposable Incus 7.2 system container and instance-specific profile were
  created on `incus-node-01` in the `nova` project. A real asynchronous
  restart operation was held in `RUNNING` state while
  `IncusDriver.destroy()` attempted to remove the instance.
- The SDK returned the production failure shape from
  `GET /1.0/operations/<id>/wait`: outer HTTP status `200`, with
  `metadata.status_code=400` and
  `metadata.err="... Instance is busy running a \"restart\" operation"`.
  The driver extracted the nested status and message and classified the
  operation as transient.
- The first delete attempt observed the busy operation. The retry refreshed
  the Incus instance state, stopped the instance after restart completed, and
  deleted it. Cleanup ran exactly once and only after the instance was
  confirmed absent; both the disposable container and its profile were gone
  after the test.
- The focused destroy suite passed 9 tests, including synchronous and
  asynchronous API error shapes, retry exhaustion without cleanup, and the
  running-state refresh after a concurrent restart. The complete Python 3.12
  suite passed all 328 tests, and `tox -e pep8` passed.

## 2026-07-27 concurrent VIF detach E2E

- A disposable Incus 7.2 container on `incus-node-01` inherited a p2p NIC
  from its Nova-style instance profile. The driver first applied an
  instance-local `type=none` mask before changing the profile.
- The E2E harness started a real asynchronous stop operation immediately
  before the profile update. Incus returned HTTP 500 with
  `The following instances failed to update (profile change still saved)`
  and identified the competing stop operation.
- The driver confirmed that the profile change was persisted, retried the
  transactional instance update after stop settled, and removed the
  temporary mask. The final profile, local instance devices, and expanded
  instance devices all omitted the NIC, while the host VIF unplug ran exactly
  once.
- New hot-attached interfaces now use transactional instance-local devices.
  A VIF-deleted event received with Nova task state `DELETING` does not
  contend with Incus profile, stop, or delete operations; destroy remains
  responsible for the instance-specific profile.
- A second real container overrode the same NIC locally while retaining its
  profile definition, matching a possible migration or upgrade state. Detach
  replaced the same-name local NIC directly with `type=none`; the expanded
  device view reported the mask after that first update and no device after
  the final update. The profile NIC was therefore never re-exposed between
  operations. A focused unit test also proves that a differently named local
  NIC remains present until the profile device has been masked.
- The focused attach, detach, destroy, and reboot suite passed 26 tests. The
  complete Python 3.12 suite passed all 334 tests, `tox -e pep8` passed, and
  all disposable containers and profiles were removed.
