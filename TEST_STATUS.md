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

## 2026-08-13 BFV public snapshot and RBD CoW rerun

The two snapshot optimization failures recorded on 2026-08-12 are resolved
and were rerun against the current three-compute testbed.

- The public BFV snapshot test no longer treats Nova console output as its
  data authority. The source and restored guest marker files are read through
  the owning compute's Incus API over strict host-key-checked SSH. The Alpine
  image also exposed a real cloud-init portability bug: it does not create
  ``/usr/local/sbin`` by default. The user data now creates the helper and
  OpenRC directories explicitly, while ``/dev/console`` is diagnostic-only.
  A subsequent CLI compatibility failure was fixed by parsing the JSON
  ``Volume ID`` field instead of requesting the obsolete ``ID`` column.
- The complete public-API flow passed with source server
  ``5743f42f-6261-47c5-b2a2-460ded838d28``, image
  ``6b4c0090-db8b-4792-a05c-9bf26c5e6ac2`` and restored server
  ``35c9ebbf-1d2c-4451-b4b4-f01438a74a72``. The restored root
  ``80c89005-7bd1-4e86-ab27-7ea0994676c6`` was a different Cinder volume,
  its exact snapshot ID matched the snapshot embedded in the Nova image, and
  both the persisted source marker and the post-boot restore marker matched.
  The script exited zero and removed both servers, the image, both root
  volumes and the Cinder snapshot.
- The missing RBD parent was caused by CephX drift: ``client.cinder`` could
  write the Cinder pool but could not read the Glance image pool, so Cinder
  correctly fell back to download/import and produced a full copy. The
  authoritative Rook ``CephClient/cinder`` now grants only
  ``profile rbd-read-only pool=glance-images-rbd-pool`` in addition to
  ``profile rbd pool=cinder-volumes-rbd-pool``. The operator reconciled the
  change before the final rerun.
- The final CoW probe passed for volume
  ``e6d0a16f-b12e-460d-a8ac-22be75470cad``. Its exact RBD parent was
  ``glance-images-rbd-pool/30c4e059-316d-4208-bf46-c63cbd8a3517@snap``
  with 805306368 bytes of overlap; provisioning took 9 seconds. The volume
  and RBD image were then both absent.

The aggregate release gate now requires ``PUBLIC_API_CINDER_POOL`` and runs
both the public BFV snapshot/restore test and the exact Glance-to-Cinder RBD
parent test. This prevents a future CephX regression from passing as a slower
full-copy implementation.

## 2026-08-12 current-code migration and lifecycle rerun

The three Incus computes were ``incus-node-02``, ``incus-node-03`` and
``incus-node-07``.  The deployed Incus image digest was
``sha256:313636fb020da6f4ed07028ef822ce8eb5930398992522adf1acc942e8f284b0``
at revision ``5df3773c94d32823d2a71127087934b09461fcbb``.  The OpenStack
tree started at ``42dde5f``; the E2E fixes found below are committed with this
evidence entry.

The following current-code reruns passed:

- all eight local/BFV x 0/1 Cinder x 0/1 Manila live-migration cases, with a
  three-node ring for every case (24 migrations);
- all 18 local/BFV x 0/1/3 Cinder x 0/1/3 Manila cardinality cases (54
  migrations), both before and after a real compute reboot and BFV host-loss
  evacuation;
- all six ordered directions for each of the five BFV cold-migration fault
  cases: confirm/revert, post-claim data failure, post-claim start failure,
  stopped-instance failure and reverse-revert;
- maximum BFV + three data volumes + three Manila shares with a destination
  CRIU restore failure, source rollback, unchanged PID, exact target cleanup
  and immediate three-node retry;
- the four local/BFV x one/two initial-data-volume cases, including format,
  marker persistence and hard reboot; QEMU computes had to be temporarily
  disabled because this older script does not constrain its scheduler trait;
- Manila destination pre-mount rejection, retry, compute restart/remount,
  snapshot, create-from-snapshot and reattach marker recovery;
- BFV delete protection, exact Ceph delete ABA, isolated-ID-map overlap on all
  three computes, pause/unpause, shelve/unshelve, read-only config-drive,
  ACTIVE/SHUTOFF reimage, resize revert/confirm and hard-reboot recovery;
- Cinder data-volume attach, guest write, online extension to 2 GiB, cold
  migration, detach, full/incremental backup and cross-compute restore.

Both cardinality runs cleaned every resource created by their cases.  Their
final whole-cloud byte-for-byte aggregate snapshot changed because unrelated
background resources changed during each multi-hour run, so that aggregate
gate returned 1.  An immediate static residual audit passed after both runs;
this is per-case green evidence, not a claim that the otherwise active cloud
was globally immutable for several hours.

The rerun found and fixed three stale assumptions in the resize/data-volume
E2E scripts: Incus commands now use the configured ``nova`` project, volume
metadata validates the v2 ``mountpoint`` field, and expected ``fuse2fs``
warnings cannot contaminate marker stdout.  Cold-migration submission now
treats the Nova migration record and terminal server state as authoritative
instead of OSC's non-zero informational return.  A deliberately triggered
delete/migration race also exposed a periodic ``None.volume_id`` traceback;
missing Nova BDM authority now raises ``InvalidVolume`` and retains durable
evidence.  The complete manager unit suite passed 266/266 after that change.

Two snapshot optimizations remain red and are not release evidence.  The
public BFV snapshot script could not observe its cloud-init console marker on
either admitted Alpine or Ubuntu BFV images, although the source instance was
ACTIVE, so it never entered the snapshot API phase.  Separately, the BFV RBD
CoW probe found that Cinder produced a flattened/full-copy image with no RBD
parent instead of a Glance snapshot clone.  Public snapshot/clone optimization
therefore remains NO-GO even though Cinder full/incremental backup and restore
passed.

The final inventory audit also retained one exact ID-map generation for the
deleted test instance ``97bf8c94-c130-42ca-940d-b37420427c2d``
(``instance-00004d1a``).  Nova, the Incus instance/profile, volume/share
journals and the instance directory are absent, but the committed source claim
has no Incus storage-release receipt.  The periodic replayer correctly refuses
to infer destructive authority from absence and leaves the release intent,
claim and slot visible for manual exact reconciliation.  No etcd key was
deleted to manufacture a clean result.  This residue came from the early
delete-during-cold-migration script error described above and is an additional
release NO-GO until its exact generation is reconciled through an approved
operator procedure.

## 2026-08-10 500-per-compute scale and full Tempest gate: NO-GO

The physical-resource ceiling for this testbed is now **500 Incus system
containers per compute**, not 1,000.  The three-compute release target is
therefore 1,500 instances.  Commit ``908f78f`` reached that target with every
instance ``ACTIVE`` and no Nova instance faults.  The final checkpoint was
balanced across ``incus-node-02``, ``incus-node-03`` and ``incus-node-07``;
the API submission rate was 0.914838 instances/s and the all-active rate was
0.132841 instances/s.

Capacity passed, but the performance SLO did not.  Create API latency was
p50/p95/p99 14.85/30.56/50.78 s.  Create-to-ACTIVE latency was
p50/p95/p99 4,955.62/8,914.69/9,582.82 s, against p95/p99 maxima of
7,200/7,800 s.  Control-plane inventory remained within its independent SLO:
Nova list p95 48.32 s and Neutron list 9.74 s.  The checkpoint audit took
43.71 s, including Incus inventory 10.20 s, Placement consumers 14.65 s,
Neutron ports 9.74 s and idmap etcd inventory 0.63 s.  This is a functional
1,500-container result and a **performance NO-GO**, not an approved baseline.

The exact artifact is
``/opt/stack/openstack-incus-release-evidence/scale-908f78f-pc500-a6e55f15-1f0b-4bc7-94bc-09b4bef2d63d.json``.
Its cleanup-only replay passed after periodic ID-map retirement converged:
Nova, all three Incus projects, Ceph RBD, OVN and the fleet ID-map registry
returned to their pre-run baselines.  The controller VM required 64 GiB RAM;
at 32 GiB Redis and MySQL were OOM-killed during earlier attempts.

The current full supported Tempest selection also remains NO-GO.  With
concurrency 4 it executed 514 tests: 468 passed, 45 skipped and one failed.
The failure was
``ImagesOneServerNegativeTestJSON.test_create_second_image_when_first_image_is_being_saved``.
Nova correctly returned 202 for the first snapshot and the expected 409 for
the second; teardown then timed out because the first snapshot stayed in
``image_pending_upload``.  Compute logs show entry into snapshot at 11:27:55,
no transition to Glance upload, and cancellation only after instance deletion
at 11:32:23.  This isolates the remaining failure to the Incus snapshot
create/publish stage or its concurrency behavior; it is not an API-status
expectation failure.  An immediate concurrency-1 rerun passed, but took
191.84 s.  The full-suite failure is therefore a reproducible snapshot
publish performance-margin problem: a small amount of concurrent backend
load pushes the same operation beyond Tempest teardown's wait budget.

## 2026-08-07 Evacuation unblocked, and the fixes that took

**Evacuation was structurally impossible, now proven working.** A running
instance holds a `committed` ID-map claim with no cleanup proof, and the only
way a claim becomes released is the holding host producing a storage release
receipt -- which a STONITH-powered-off host can never do. Every failed-host
evacuation therefore deadlocked at the destination's pre-check. Confirmed live
before fixing: `IDMapConflict: Rescheduled Incus spawn has an uncleared idmap
host claim`.

The fix is `fence_retire_claim`: external power-fencing evidence substitutes
for the receipt, written to a per-host fence ledger in the same
compare-and-swap that removes the claim and its host index entry. The
destination pre-check needs no change -- the dead host simply leaves
`host_ids`.

Verified end to end on the testbed with a real STONITH of incus-node-02:
BFV instance -> `virsh` power off -> service down -> RBD watcher count 0 ->
evacuation refused (expected) -> `--fence-retire-host-claim` -> evacuation
succeeds, ACTIVE on incus-node-03 -> root marker file intact -> watcher count
1 -> registry holds only the destination's claim -> returning host quarantined
(no admission token, nova-compute refuses to start, containers stopped) ->
returning-host audit 9/9 PASS -> admitted -> stale record disposed of ->
re-enabled.

**Three defects that only the live run could show.**

- The registry audit did not recognise the new fence ledger keys, so the
  first retirement permanently broke every full audit. The per-minute count
  probe was unaffected, which is why the suite stayed green.
- A returning host could not rejoin: `init_host` destroys evacuated-stale
  records, but the claim had been fence-retired, so the plain delete hit the
  fork's release-receipt requirement and nova-compute died on every start.
  Fixed by a new Incus storage-handover state, `detached`, plus a driver path
  that disposes of such a record under recorded fence evidence.
- Adding the disposed materialization token to the ledger changed the shape
  of an already-persisted record, and the parser demanded the new field.
  Deploying it took every compute down at `init_host` -- the same fleet-wide
  latch the token was added to prevent, arriving by a different door.

**Bare ID-map allocations are now reclaimed.** An allocation whose last claim
went away without leaving a release intent -- a local delete against an
unreachable compute, or a fence retirement before the destination claims --
was invisible to both periodic reclaimers, so the slot never came back. The
full audit now adopts it by writing the missing intent and lets the ordinary
replay path apply its usual barrier. Verified by producing one the real way
(stop the compute, local delete, fence-retire the last claim) and watching a
compute audit adopt and release it.

**Fork-side create-path fix, still the performance ceiling.** The
per-fingerprint `ImageOperation` lock recorded above was made read/write in
the fork (`24fa16c6b`, writer-priority, ctx-aware): creates take it shared,
downloads and deletions exclusively, with a second `UseImage_<pool>_<fp>`
lock in the storage layer. Measured on incus-node-01 against the Ceph pool:
**C=1 8 s, C=8 wall 17 s**, against >=64 s before, so the serialized fraction
fell from 0.48 to 0.16. `EnsureImage` itself was never the problem -- it costs
1-2 s and was exonerated by the incusd debug log after I had wrongly accused
it.

**Fleet.** incus-node-01 is control-plane only (no compute service, no
resource provider). Compute is incus-node-02, incus-node-03 and the new
incus-node-07, all 64C/125G. incus-node-04 and the `-kvm` services on 02/03/07
are libvirt computes belonging to separate testing; `m1.tiny` carries no trait
requirement and can schedule there, so Incus test instances must pin their
host or use a flavor with `trait:CUSTOM_INCUS_SYSTEM_CONTAINER=required`.

**Superseded by the 2026-08-10 result above.** The approved physical target is
now 500 instances per compute.  That capacity was reached and cleaned, but its
latency SLO failed, so a formal approved performance baseline is still open.

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

## 2026-08-04 Rollback idempotency and interrupted-detach recovery

Two of the three cases pass on the current candidate:

- **Repeated detach is a clean no-op.** A second detach of an
  already-detached volume leaves it ``available`` and issues no further
  connector work (host disconnect count unchanged). This is what makes
  journal replay safe.
- **A failed migration rolls back in place.** With the destination's
  Incus port blocked, the migration fails, the instance stays ACTIVE on
  its source with an unchanged guest PID, and the destination keeps no
  instance record.

The third case exposed a gap and, in doing so, corrected the test's own
premise. Killing nova-compute two seconds into a detach leaves:

* Cinder stuck in ``detaching``;
* the Nova BDM still present (``deleted=0``);
* the host RBD mapping still present;
* **no journal**, because the process died before the driver was entered.

Host and Nova state are therefore *consistent* — the guest still has its
volume and keeps running. The only wrong thing is Cinder's intermediate
status, which makes the API refuse a retry (``status must be 'in-use'``)
and requires ``cinder reset-state``. Vanilla Nova has the same hole; its
detach failure path calls ``volume_api.roll_detaching`` but a killed
process never reaches it.

That reframes the correct convergence. Under the project's stated
migration principle — a failure leaves the workload in place — an
interrupted detach should be treated as *not having happened*: the
volume stays attached and usable and Cinder returns to ``in-use`` so the
operator can retry. The probe originally asserted the opposite (that the
detach should complete), which is why it reported a failure that was
partly its own premise.

Fixed in ``0ad69cd``: the volume journal is now consumed. It was already
written durably before both connect and disconnect, but nothing ever
read it as work to finish — it was only fail-closed evidence. The new
periodic completes an interrupted disconnect when, and only when, the
exact instance is still local, has no in-flight task, and Nova no longer
maps that volume to it.

Closed by ``da84d47``: ``init_host`` now calls ``roll_detaching`` for a
volume left in ``detaching`` whose block device mapping and host mapping
both still say attached. It runs at startup rather than periodically
because that residue can only be produced by process death, and polling
Cinder per instance every cycle is the kind of hotspot the scale work is
meant to remove. ``holds_volume_attachment()`` keeps the two recovery
mechanisms from fighting over one volume: a journal means the driver
reached the disconnect and journal recovery owns the outcome; a profile
device with no journal means nothing was released.

All three cases now pass through
``tools/openstack-incus-rollback-idempotency-e2e.sh``: the repeated
detach is a no-op, the interrupted detach rolls back to ``in-use`` with
the guest still holding the volume and then retries cleanly to no host
mapping and no journal, and the failed migration leaves the instance
ACTIVE on its source with an unchanged guest PID.

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

- `root@10.32.32.130` (`incus-node-01`) is the DevStack controller and first
  Incus compute node. It runs Ubuntu Noble 24.04 and Python 3.12.
- `root@10.32.32.131` (`incus-node-02`) is the second Incus compute node. It
  runs Ubuntu Noble 24.04 and Python 3.12.
- `root@10.32.32.132` (`incus-node-03`) is the third Incus compute node. It
  runs Ubuntu Noble 24.04 and Python 3.12.
- Addressing is static across three VLAN interfaces, replacing the DHCP
  `10.224.0.0/24` network used before 2026-08-03: `k8s-ctl` (VLAN 11,
  `10.32.32.128/27`) carries management, API and Incus migration traffic;
  `ovn-ext` (VLAN 14, `10.128.32.128/27`) is the `br-ex` provider uplink;
  `ovn-int` (VLAN 15, `10.160.32.128/27`) carries the OVN Geneve tunnels and
  is what each chassis advertises as its `ovn-encap-ip`. Each interface also
  has an IPv6 address; the control plane listens dual-stack, but its service
  catalogue and inter-service URLs remain IPv4.
- Stable hostnames and `/etc/hosts` self-resolution are configured on all
  three nodes as shown above.
- All three nodes run independent, non-clustered Incus 7.2 daemons and expose
  migration HTTPS only on their management address. Nova, Placement and
  Neutron/OVN own placement and tenant networking.
- Remote unit-test runs must set `INCUS_SDK_PATH=/opt/incus-python-sdk-src`
  (the synchronized SDK path); the default sibling path remains correct for
  local checkouts.

### Current storage substrate

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

## 2026-08-04 abandoned migration reservation reclamation (defect 15)

- Fork commits `87a638d77` and `60e7c1e8f`, driver commit `1f6101a`. Image
  `incus-quadlet-candidate:defect15-r11` deployed to all three nodes, and
  `driver.py`/`manager.py` synced to `/opt/stack/nova/nova/virt/incus` with
  `devstack@n-cpu` restarted on each. All running instances survived every
  `incus-podman` restart.
- Starting state across the fleet was **zero** unfinished attempts, so this
  was verified by constructing both halves rather than observing wild ones.
- `incus-node-03`: a registered but never started reservation
  (`900000000+65536`) reported `idmap_active: true` and rejected an
  overlapping token with `ErrIDMapOverlap`. After a full `incusd` restart it
  was still active and still rejected the overlap, which is the required
  behaviour: the create request it fences carries no deadline and can arrive
  after a target restart, so Incus must not expire it.
- Nova released it on the second periodic pass exactly as designed --
  `Deferring release ... until it is seen unchanged twice` at 06:06:07,
  `Released the abandoned Incus migration reservation` at 06:07:10 -- after
  which the same range was immediately claimable by a new token.
- Regression: a throwaway `m1.tiny` instance live migrated node-01 ->
  node-02 and cold migrated node-02 -> node-03 with confirm, both ACTIVE.
  A fleet audit through the new `GET /1.0/migration-attempts` reported
  `still fencing: 0` on all three nodes before and after.
- Unit gates: incus `cmd/incusd`, `db`, `migrationattempt`,
  `idmapreservation`, `storagematerializationattempt`,
  `storagereleasereceipt`, `instance/drivers`, `storage` and `shared/api` all
  pass; openstack-incus 806/806 (the tempest-plugin module needs `tempest`,
  which is absent from the local container) and flake8 clean. Fixing that
  flake8 run also repaired two continuation lines mangled by an earlier batch
  edit and a deprecated `LOG.warn`, both of which had been failing
  `tox -e pep8` since commit `0ad69cd`.

## 2026-08-04 scale hotspot removal and first baseline attempt

Candidate: driver commits `85a156b` (hotspot removal) and `108e0fa` (scale
audit helpers, Placement microversion), deployed to all three computes.

### Testbed configuration this evidence depends on

- Flavor `incus.scale`: 128 MB, 1 vCPU, 1 GB, `incus:root_storage_pool=durable`,
  `incus:process_limit=256`, `trait:CUSTOM_INCUS_SYSTEM_CONTAINER=required`.
- `ram_allocation_ratio = 2.0` and `cpu_allocation_ratio = 20.0` on every
  compute. A container memory limit is a cgroup ceiling, not a reservation,
  so Placement capacity for idle scale guests is expressed as overcommit;
  at 1.0/4.0 the fleet caps at 251 instances per node and 1,000 per compute
  is unreachable by construction. Originals saved as
  `/etc/nova/nova-cpu.conf.pre-scale-baseline`.
- Admin project quotas raised to 4,000 instances/cores and ports.
- `/etc/openstack/clouds.yaml` still pointed at the pre-migration
  `10.224.0.21` identity endpoint and was repaired to `10.32.32.130`;
  original saved as `clouds.yaml.pre-scale-baseline`.
- The runner needed two fixes before it could execute at all: it shipped
  none of its four required evidence helpers, and its Placement provider
  trait read used the SDK default microversion 1.0, where that route 404s.
  Both are in `108e0fa`.
- `--host-*-min-free-bytes` are lowered from the runner defaults for this
  testbed only: `/var/log/incus` is a 2 GB loop volume that a 4 GiB absolute
  floor can never satisfy. The percentage floors still apply.
- `--max-host-skew-percent 40` / `--min-per-compute-percent 70` for a
  baseline run only. The fleet does not start balanced, so Nova's RAM
  weigher spends its first few dozen placements correcting the offset before
  it spreads evenly. The release gate keeps its 20/90 defaults.

### The baseline is blocked: destroy costs ~27 s idle and ~312 s under load

Measured on an idle node, one instance, decomposed:

| step | seconds |
| --- | --- |
| graceful stop / `--force` stop | 4.2 / 1.7 |
| `incusd` DELETE with the exact-identity release token | **19.5** |
| Nova destroy when the container is already absent | 11 |
| `rbd trash mv` / `rbd trash rm`, 1 GB image, same pool | 0.19 / 0.19 |

Under the pilot's 16-way concurrency, 23 destroys with no build contention
gave min 31.05 s, median 312.10 s, max 596.11 s, against 1.3 s to deallocate
network. So roughly 20 s of fixed cost inside `incusd` is also being
serialized. Ceph's own image lifecycle accounts for 0.4 s of it, so whatever
`incusd` spends the remaining ~19 s on, it is not removing the image. A
krbd unmap or RBD watcher timeout is the leading hypothesis and is **not yet
verified**; an earlier hypothesis that the 30 s `boot.host_shutdown_timeout`
was the floor was refuted by the stop measurements above.

At 312 s median, deleting the 3,000 instances a 100/500/1,000 per-compute
run creates is days of wall clock. The baseline cannot be recorded until
this is understood.

### One stranded profile per raced delete-during-build

Of 120 pilot instances, 119 released completely: the RBD pool returned to
exactly its 6 pre-existing container roots, and no release intent was left
queued. One instance (`4d6ffd8a-a019-4f83-a13b-460b8bb7907c`) left the Incus
profile `instance-000005e5` on node-02 with no container and no Nova row.

Nothing can reclaim it. The profile carries neither
`user.openstack.cleanup_required` nor
`user.openstack.migration_destination_prepared`, so neither profile recovery
periodic can see it, and the ID-map reconciler correctly refuses to release
the allocation while a profile of that name exists -- it logs "Incus profile
instance-000005e5 still exists; retaining idmap release intent" every 60 s.
The registry therefore holds 7 allocations for 6 servers, permanently.

The fail-closed behaviour is right. The gap was in the retention path
itself: it refuses to write the recovery marker when `profile.used_by` is
non-empty, to avoid claiming a profile another instance uses -- but the
usual reason a cleanup could not finish is that this instance's own
container is still there, which is exactly what makes `used_by` non-empty.
The compute logged both halves in the same second: "Refusing to mark a
foreign or in-use Incus profile for automatic cleanup" and "Incus instance
nova/instance-000005e5 still matches Nova instance 4d6ffd8a-...". Same shape
as the 2026-08 cleanup defects: the marker is withheld at the one moment it
is needed.

Fixed in `aa9c197`: only another instance's usage makes the profile foreign,
so `used_by` is compared against this instance's own container and its
rescue container, and an unparseable reference still counts as foreign.

Verified on the stranded profile itself. Writing the marker that the fix now
writes automatically let `_recover_incus_cleanup_profiles` reclaim the
profile on its next cycle, after which the ID-map reconciler released the
allocation on its own next cycle with no further intervention. The registry
returned from 22 entries to exactly its 19-entry baseline (1 config + 6
allocations + 6 slots + 6 host claims for 6 servers), the RBD pool to its 6
container roots, and all three nodes to zero fencing migration attempts.
Everything downstream of the marker already worked; the marker was the only
broken link.

### Where the ~20 s inside `incusd` goes: 38 Python `ceph` spawns per delete

Sampling every process `incusd` forks during one Nova delete on an idle
node (32.9 s wall, repeated twice with the same result):

| invocations | command |
| --- | --- |
| **38** | `ceph --name client.incus osd map <pool> rbd_directory --format json` |
| 8 | `rbd trash ls` |
| 8 | `rbd info incus_identity_release_<digest>` |
| 5 | `rbd info incus_identity_quarantine_<digest>` |
| 5 + 5 | `rbd info container_nova_<instance>` (json and plain) |
| 1 each | `rbd unmap`, `rbd mv`, `rbd snap ls`, `rbd trash mv`, `rbd trash rm` |

Cumulative sampled runtime attributes 9.8 s of the delete to that one
`ceph` command and 0.2-0.4 s to each `rbd` command. The difference is the
binary: `rbd` is C++, while `ceph` is `/usr/bin/python3 /usr/bin/ceph`, so
each invocation pays interpreter startup plus a mon round-trip.

The arithmetic is exact. `getRBDVolumeIdentity`
(`driver_ceph_identity_release.go:171`) brackets its `rbd info` with two
`getRBDPoolIdentity()` calls, an ABA guard proving the pool was not deleted
and recreated under the read. Eighteen identity lookups per delete times two
pool reads each is thirty-six, plus two more, which is the thirty-eight
observed. The same three images are re-identified five to eight times within
a single delete.

Two independent levers, and they differ in risk:

1. **Redundant lookups.** Re-identifying the same image five to eight times
   inside one locked delete is repeated work, not a guarantee. Collapsing
   that is safety-neutral and is the larger share.
2. **Cost per pool read.** The value needed is only the pool's numeric ID.
   Obtaining it through the Python CLI costs roughly 0.25 s against a few
   milliseconds of real work.

Lever 2 touches the ABA guard's semantics and must not be weakened
unilaterally: caching the pool identity would still catch pool recreation
between operations but no longer within one. Lever 1 requires no such
trade-off. Not yet implemented.

### Pool identity moved to `rados df`: delete 32.9 s -> 17.5 s

Fork commit `40dcbf738`, image `incus-quadlet-candidate:radosdf-r12`, deployed
to all three nodes.

An ultracode study (23 agents) corrected the cost model that four of its own
proposals had been sized against. `getRBDVolumeIdentity` returns on ENOENT
**before** its trailing pool read, so probing an *absent* image costs one
`ceph osd map`, not two. The corrected model reproduces three independent
measurements exactly: 38 invocations, their sampled runtime, and the 5 plain
`rbd info` calls. Composition: 12 standalone fences, 18 single reads from
absent probes, 8 from four found-probe brackets.

Measured per-call cost, five-run averages inside the daemon container:

| client | per call |
| --- | --- |
| `ceph osd map` (Python) | 470 ms |
| `rados df` (C++) | 94 ms |
| `rbd info` (C++) | 164 ms |
| bare `python3 -c pass` | 21 ms |

The last row disproves the obvious explanation: interpreter startup accounts
for none of the gap; the `ceph` CLI is simply a far heavier program. `rados df`
returns the same pool id from the same osdmap under the existing `client.incus`
caps, so `getRBDPoolIdentity` now tries it first and keeps `ceph osd map` as
the authority for any transport failure, permission gap or unreadable output.
The pool must appear exactly once by name with a numeric id or the report is
treated as unusable. No guarantee changed.

Result on an idle node, three runs: **17.44 / 17.48 / 17.59 s**, against 32.9 s
before. Process sampling during the delete shows zero `ceph` invocations
remaining; 28 `rados` and 34 `rbd`. Fleet returned to baseline after each probe
(6 servers, 19 registry entries, 6 RBD roots).

The guard is now assertable, which it was not. `fakeCephIdentityReleaseStore`
reported a constant pool id that no test ever changed, so replacing
`verifyRBDIdentityReleasePool`'s body with `return nil` passed the entire
suite and every pool fence in that file was vacuously satisfied.
`TestFakeStoreCanMovePoolUnderTheCaller` now proves both that a moved pool is
refused and that it is the fence doing the refusing.

**This still does not unblock the 3000-instance gate**: 27.4 h -> 14.6 h. The
remaining ~17.5 s is 34 `rbd` forks plus unmount, `wipeDirectory`, the
operations machinery and DB work, none of which was investigated. The lever
that unblocks the gate is concurrency -- deletes are serialized per instance
today, and `pool_load.go:149` builds a fresh driver per load, so no shared
state prevents parallelism.

Deliberately not done, each costing a named guarantee for 1.3-2.2 s:
skipping the duplicate absence proof before `VolumeDBDelete`, skipping
`HasVolumeIdentity` on a completed receipt `GET`, and restructuring
`prepareRBDIdentityTombstone`. Not worth the review budget after this change.

Flagged, unrelated to this work: `reconcileStorageMaterializationAttemptsAfterRestart`
(`cmd/incusd/storage_materialization_create.go:487`) loops serially and runs
synchronously at `daemon.go:1589` before `instance.LoadNodeAll`, so a
500-instance compute rebooting after a mass failure blocks daemon startup for
N x ~30 s. `reconcileMigrationAttemptsAfterRestart`, added in `87a638d77`,
sits at the same point with the same serial shape; its work is bounded by
unfinished migration attempts rather than instance count, so its exposure is
smaller, but the pattern is identical.

### Step 0 measurements: what the 17.5 s is, and proof that deletes serialize

Run after `40dcbf738` (`rados df`) on an otherwise idle fleet. No code changed
to obtain any of this.

**1. nova-compute burns almost no CPU.** `py-spy record --idle` over a 40 s
window containing one delete: 3999 samples, of which 37.17 s sit in
`eventlet/hubs/epolls.py:31 do_poll`. Every `nova/virt/incus` frame together
totals under 1.5 s. Note the methodological limit that produced this: eventlet
green threads are invisible to `py-spy` while blocked, because a blocked
greenlet is not on any OS thread stack. The conclusion stands and is the useful
one -- **the delete is round trips, not computation** -- but py-spy cannot
attribute the waiting, so it is the wrong tool for the rest of this work.

**2. Nova DEBUG logs cannot attribute it either.** Gaps between consecutive log
lines across one 19.7 s delete: 4.72 s, 3.76 s, 2.24 s, 1.54 s, 1.25 s, 1.04 s,
each bounded by nothing but unrelated `ovsdbapp` poller wakeups. The driver
logs nothing while an incusd or etcd call is in flight.

**3. Direct three-way split**, by driving each stage separately against one
instance:

| stage | now | before `40dcbf738` |
| --- | --- | --- |
| `incus stop --force` | 1.79 s | 1.7 s |
| `incusd` DELETE carrying the release token | **8.07 s** | 19.5 s |
| Nova destroy with the container already absent | **8.72 s** | 11 s |

The two halves are now within 8 % of each other. Either is worth attacking;
neither dominates.

**4. Concurrent deletes are ~73 % serialized.** Four instances on one compute,
deleted simultaneously: **55.76 s**. Fully serial would be 4 x 17.5 = 70 s,
fully parallel ~20 s. Effective concurrency is 4 / (55.76/17.5) = **1.25x**.
Solving 17.5 x (1 + 3f) = 55.76 gives **f = 0.73**: about three quarters of
each delete sits inside a serialized section.

That matches the scope of the lock at `nova/virt/incus/driver.py:7790`, which
is held across lines 7790-7843 -- stop, delete, receipt settlement, `_cleanup`
and idmap claim retirement. The call passes `lock_path` positionally into
`lockutils.lock`'s first parameter, which is `name`, so the in-process
semaphore is keyed on the constant `/var/lib/nova/instances/locks` and every
destroy in the nova-compute process shares one mutex. `lock_file_prefix`
reaches only `external_lock`, so the on-disk flock file stays per instance and
cross-process exclusion is intact: this is a throughput defect, not a
correctness defect. The same call shape is at `:4687` (Glance image sync, so
creates block deletes) and `:10079` (snapshot). Roughly nine other
`lockutils.lock` sites in the same file pass a per-instance name plus an
explicit `lock_path=` keyword, so this is a slip rather than a design.

Independent support beyond the 4-way measurement: a FIFO serializer over 16
predicts median 8.5xS and max 16xS; the scale run's observed 312 s / 596 s
give S = 36.7 s and 37.25 s, agreeing to 1.5 %.

**Projection, to be confirmed rather than believed:** at C=8 and T=17.5 s,
3000 deletes take 1.8 h against 14.6 h today. The next ceiling is already
named and unmeasured -- `cmd/incusd/instances_get.go:399` loads every instance
inside one cluster transaction on a one-connection pool (`db.go:141`), and the
delete path issues six such listings.

### Concurrency ladder after the lock fix: the gate is unblocked

Driver commit `55302b9`, deployed to all three computes. All deletes on one
compute, `incus.scale` instances, otherwise idle fleet.

| C | wall | per delete | deletes/hour | effective concurrency |
| --- | --- | --- | --- | --- |
| 1 | 17.57 s | 17.57 s | 205 | 1.0x |
| 2 | 18.06 s | 9.03 s | 399 | 1.9x |
| 4 | 19.87 s | 4.97 s | 725 | 3.5x |
| 8 | 22.70 s | 2.84 s | 1269 | 6.2x |
| 16 | 30.98 s | 1.94 s | 1859 | 9.1x |

The same C=4 point measured **55.76 s** before the fix, at 1.25x effective
concurrency. It is now 19.87 s at 3.5x -- 2.8x faster on identical hardware
with no change to what a delete does.

**Gate arithmetic.** 3000 instances is 1000 per compute with all three
computes working in parallel. At C=8 that is 3807 deletes/hour fleet-wide,
so 0.79 h; at C=16, 5577/hour, so 0.54 h. Against 14.6 h before this session
and 27.4 h before the `rados df` change. **Teardown is no longer what makes
the 100/500/1000 gate impractical.**

Returns start bending after C=8 (6.2x at 8, 9.1x at 16), which is the next
ceiling appearing. It is already named and still unmeasured:
`cmd/incusd/instances_get.go:399` loads every instance inside one cluster
transaction on a one-connection pool (`internal/server/db/db.go:141`), and the
delete path issues six such listings. Not worth attacking until the gate has
actually been run at scale.

**No residue.** The ladder created and deleted 31 instances, 16 of them
simultaneously. Afterwards the fleet returned to exactly its baseline: 6
servers, 19 ID-map registry entries (1 config + 6 allocations + 6 slots + 6
host claims), 6 RBD container roots, and the pre-existing instance profiles on
each node. Concurrency bought throughput without loosening any of the release
protocol's proofs -- which is the expected result, since the defect was an
in-process semaphore key and the on-disk exclusion was per instance all along.

### 2026-08-04 first real 100/500/1000 gate attempt: fails at checkpoint 1, on creates

Run `fcdf4f64`, driver `55302b9`, image `radosdf-r12`, flavor `incus.scale`,
concurrency 32, per-compute checkpoints 100/500/1000.

**Result: FAIL at the first checkpoint** --
`unexpected server states at checkpoint 300: {'ERROR': 1}`. One instance of
300. The gate is fail-closed, so the whole run rolled back and neither the
500 nor the 1000 checkpoint was reached.

**Two preflight checks earned their keep before anything was created.** The
first attempt was refused with `subnet ... has 252 available addresses, fewer
than the 3000 scale target` -- the long-used network is a /24. A dedicated
`incus-scale-net` (10.100.0.0/20, 4081 usable) was created for this. Without
that check the run would have failed at instance 252 and left hundreds of
half-built instances to clean up.

**The one failure was a client-side read timeout, not a build error:**

```
requests.exceptions.ConnectionError: _UnixSocketHTTPConnectionPool: Read timed out.
  pylxd/models/operation.py:104 -> GET /1.0/operations/<id>/wait
```

A hypothesis that had to be measured away before acting on it: pylxd hard
codes `SOCKET_CONNECTION_TIMEOUT = 60` and assigns it to `self.timeout` in
`_UnixSocketHTTPConnection`, which reads as though the configured
`[incus] request_timeout = 300` can never reach the unix socket path. **It is
wrong.** The failing instance began spawning at 13:41:09 and raised at
13:46:53, so it waited **344 s**, consistent with 300 s plus overhead: the
hard-coded constant is the *connect* timeout, and urllib3 overrides the read
timeout per request. The SDK needs no change.

**The real finding is the create side.** Spawn durations across all three
computes during this run, 142 samples at concurrency 32:

| | seconds |
| --- | --- |
| min | 17.8 |
| p50 | 106.9 |
| p90 | 208.8 |
| p99 | 295.6 |
| max | 300.7 |

The minimum is the uncontended cost and matches a single delete (17.5 s). At
32 in flight the median is six times that and the tail reaches the 300 s
client ceiling, which is what produced the single ERROR.

So the gate blocker has moved rather than gone:

| | uncontended | under concurrency |
| --- | --- | --- |
| delete | 17.5 s | 9.1x effective at C=16, 1859/hour |
| create | ~17.8 s | p50 107 s at C=32, tail hits the 300 s timeout |

**Not yet separated:** whether the create queueing is caused by concurrency
itself or by the ID-map registry growing to 300 instances during the run --
every allocation runs a full registry audit inline, so both scale together
here. A C in {1,2,4,8} ladder against a fixed registry size separates them,
which is the same method that isolated the delete-side lock.

**Host distribution was healthy**, which was the pilot's failure mode: among
placed instances 29/26/29 across the three computes. The RAM weigher absorbs
node-01's pre-existing offset once the run is large enough, as predicted.

**Evidence gap:** no `--telemetry-command` was passed, so the incusd and
nova-compute CPU/RSS/FD ceilings that `scale_design.rst` requires were not
captured. Any run that is meant to produce the recorded baseline must pass
telemetry helpers.

### The gate run wedged every periodic task on one compute

Found while investigating why four ID-map registry entries would not converge
after the failed gate run. This is more serious than the create latency it was
found alongside.

**Symptom.** `incus-node-01` reported `devstack@n-cpu` active, the eventlet hub
idle in `epoll`, and log lines still flowing at 18-22/minute -- but a live
100-second window contained **zero** periodic task executions. The last one
had run 42 minutes earlier, and the last one logged in that final batch was
`_replay_incus_idmap_releases`.

**Root cause.** A SIGUSR2 guru meditation report (1703 lines) showed exactly
one live driver green thread, stopped at
`nova/virt/incus/driver.py:5888` -- the `lockutils.lock(...)` acquiring the
per-instance claim lock inside `_promote_idmap_claim_if_server_committed`,
reached from `_replay_incus_idmap_releases` at `manager.py:1104`. Several
other green threads sat in `eventlet/semaphore.py:107 acquire`. The lock was
never released.

**Why one stuck lock is fatal.** oslo_service runs every periodic task for a
service in a *single* green thread, sequentially. One periodic blocking
forever therefore stops **all** of them on that compute: ID-map release
replay, host claim reconciliation, Cinder volume journal recovery, profile
recovery, abandoned migration reservation release, and Nova's own power state
sync. Nothing converges again until the process is restarted, and nothing
reports that this has happened -- the service still answers as healthy.

**Confirmation.** Restarting `devstack@n-cpu` on node-01 resumed periodics (19
executions in the first 70 seconds) and the registry converged from 23 back to
exactly its 19-entry baseline within one cycle, with no other intervention.

**Method note.** `py-spy dump` was useless here: it showed only the idle hub,
because a blocked eventlet green thread is not on any OS thread stack. SIGUSR2
is the tool for this failure mode.

**Correction to an earlier reading.** The single ERROR instance from the gate
run looked at first like a three-way deadlock between the delete guard at
`manager.py:920`, the release replay's Nova-state check, and the reconcile
path. It is not. The guard is transient: it refuses only until the reconcile
periodic settles a claim still at `possible`, and a manual delete issued later
succeeded in 45 seconds. What made it look permanent was this wedge -- the
reconcile periodic that would have cleared it was dead.

**Consequence for the gate.** A compute whose periodic loop dies after ~300
creates cannot be asked for 1000. This has to be understood before the gate is
attempted again, and it is independent of the create latency below.

### Creates cap at 2.1x concurrency, and 86% of it is one per-image lock

Measured after `7700759`, on a compute whose periodic loop was verified alive
in every rung (a wedged loop invalidated the earlier gate numbers).

**Concurrency ladder, all creates pinned to one compute, same image:**

| C | wall | per create | effective | solved f | delete at same C |
| --- | --- | --- | --- | --- | --- |
| 1 | 29.53 s | 29.53 s | 1.00x | - | 1.00x |
| 2 | 44.63 s | 22.32 s | 1.32x | 0.51 | 1.95x |
| 4 | 67.35 s | 16.84 s | 1.75x | 0.43 | 3.50x |
| 8 | 132.16 s | 16.52 s | 1.79x | 0.50 | 6.20x |
| 16 | 244.63 s | 15.29 s | **1.93x** | 0.48 | **9.10x** |

Fitting `T(C) = T1 x (1 + (C-1)f)` gives f = 0.48 from three independent
rungs. That model predicted C=16 at 242 s against 244.63 s measured, a **1%
error**, so a single serialized segment explains the whole curve -- not
multiple contended resources. The ceiling is 1/f ~ **2.1x**, unreachable by
raising C.

**Where it serializes.** Five SIGUSR2 guru meditation reports taken during a
C=16 burst put 37 of the driver green-thread samples at the same frame:
`nova/virt/incus/driver.py:7354`, the `self.client.instances.create(...,
wait=True)` lambda. The threads are waiting on incusd, not on a Nova lock --
unlike the delete-side defect.

**Decisive experiment.** `internal/server/storage/backend.go:5005` locks
`OperationLockName("EnsureImage", pool, VolumeTypeImage, "", fingerprint)`,
which is per image fingerprint, and every instance in these runs used one
image. Prediction fixed in advance: splitting the same concurrency across two
fingerprints gives two independent serial chains, so ~70-90 s, while ~126 s
refutes it. On an empty fleet (0 servers, registry holding only its config
record):

| run | wall |
| --- | --- |
| baseline A, C=1 | 29.55 s |
| baseline B, C=1 | 29.02 s |
| CONTROL, C=8 one image | 126.56 s |
| **TEST, C=8 split 4+4** | **78.55 s** |

Confirmed, 1.61x faster. Solving `126.56 = 29.5 + 7s` and
`78.55 = 29.5 + 3s_img + 7s_glob` with `s_img + s_glob = s` gives
**s_img = 12.0 s and s_glob = 1.9 s**: 86% of the serialized segment is the
per-fingerprint lock and only 1.9 s is global.

Two confounds were ruled out by measurement rather than argument. The two
baselines differ by 1.8% despite one image being 28 MB and the other far
larger, so image size does not drive create cost and the split needs no
normalization. And C=1 measured 29.55 s on an empty fleet against 29.53 s on
a fleet of 6 with 19 registry entries, so registry size contributes nothing
at this scale -- the queueing is concurrency alone.

**Consequence for the gate.** Every instance a gate run creates uses the same
image, so all 3000 contend on one lock. `EnsureImage` holds it across a
cluster database transaction and the volume config comparisons even when the
image volume already exists, and that cluster pool has a single connection.

## 2026-08-11 CRIU incremental-memory safety correction

- The 2026-07-19 statement that failed CRIU pre-dumps safely fall back to a
  full final checkpoint is historical evidence for the then-tested Incus fork,
  not the current safety contract. A failed pre-dump can clear soft-dirty state
  before its image generation is durable, so reusing an older parent may
  restore stale memory.
- New Nova-managed instances explicitly set
  ``migration.incremental.memory=false`` in both their dedicated profile and
  instance-local configuration even while live migration is disabled. Source
  pre-check requires exact profile/local Nova ownership, effective
  ``migration.stateful=true``, and an unprivileged guest before it normalizes
  older instances under the profile lock, re-reads profile/local/expanded
  configuration, and requires the dedicated profile to be the only attached
  profile.
- The source emits a versioned full-checkpoint attestation only after its
  locked validation succeeds. A new destination rejects old-source migration
  data without that attestation in the custom compute manager before Manila
  staging or upstream Cinder attachment creation. The driver repeats the gate
  before Incus idmap/profile, VIF, or host-side Cinder preparation, and also
  rejects a serialized source profile without the explicit ``false`` value.
- ``live_migration`` re-checks the complete source profile plus fresh local and
  expanded configuration under the profile lock, and re-checks the staged
  destination profile immediately before generating migration data.
- Rolling upgrade keeps migration frozen through an Incus fleet update, then
  upgrades and restarts every API and conductor. The controller-only fleet gate
  must find ``IncusLiveMigrateData`` version 1.6 or newer before computes roll;
  each restarted compute is then gated at 1.6, and a normal full-fleet gate is
  required after all computes are current and before migration is unfrozen.
  New-source to old-destination compatibility is not claimed while conductors
  are mixed.
- Focused unit, contract, lint, documentation, and real three-node migration
  results for this correction must be recorded before it becomes release
  evidence.

## 2026-08-14 production Incus base and representative migration smoke

- Deployed Incus chart revision 8 and Nova chart revision 42 on
  ``container1`` and ``container2``. Both ``incusd`` and
  ``nova-compute-incus`` DaemonSet pods were Ready after rollout. The Incus
  image was pinned to
  ``ghcr.io/fivetime/incus@sha256:fb23c582de6db85046ef3216df291e7f05629e8da5661a2593c0700e9bb07592``
  from Incus commit ``a7866b1c2``.
- Replacing the incusd pod preserved a running guest's Incus init PID
  (``629403``), guest counter process PID (``702``), persistent marker, and
  increasing counter. This proves the deployed Kubernetes mount/runtime
  contract re-attached to that running container; it is not a substitute for
  a host reboot test.
- A fresh Alpine 3.21/OpenRC image completed a full-checkpoint live migration
  from ``container1`` to ``container2``. The durable marker remained
  ``alpine-marker-20260814``, the guest counter PID remained ``852``, the
  counter advanced from 6 to 111, OVN binding moved to the destination, the
  source instance/profile disappeared, and no ``migration_pre-dump_*`` log
  was created. The exact migration UUID was
  ``0fe177dd-82db-4aef-b842-b42cf23433cf``.
- That Alpine package set did not provide ``fuse2fs``. It therefore qualifies
  only the root-only live smoke and must not advertise Cinder data-volume FUSE
  support or stand in for the 2x2x2/2x3x3 data-volume matrices.
- The tested Ubuntu Noble/systemd image failed CRIU restore first on a nested
  UTS namespace and then on a service mount namespace. No general removal of
  systemd hardening was accepted as a fix. It remains suitable for the tested
  cold/lifecycle path but is not qualified for live migration until its exact
  enabled-unit set passes the complete failure matrix.
- After writing node-local Nova ``[DEFAULT] my_ip`` from the validated Incus
  migration listener, the Ubuntu image completed cold migration to
  ``container2`` and resize confirm. Marker and counter data persisted; the
  guest PID changed as expected for cold migration. Migration UUID:
  ``a57773aa-170e-464d-8832-bf589f7c2dcd``.
- Both smoke servers were deleted through Nova. The two Incus APIs then
  reported no instances or instance profiles, their Neutron ports were
  absent, the exact empty host mount directories were removed, and no current
  etcd allocation remained for either server UUID.

This is representative production smoke evidence only. The 8-combination
live matrix, 18-combination capacity matrix, maximum-mount restore-failure
case, BFV cold matrix, Cinder lifecycle matrix, Manila lifecycle matrix, and
host-loss evacuation matrix were **not** rerun by this entry.

## 2026-08-18 production lifecycle and migration matrix rerun

This entry supersedes the release-evidence limitation in the 2026-08-14
smoke entry. It does not rewrite that earlier result: the larger matrices were
run only after the production fixes and rollouts described here.

**Frozen production inputs.** Nova Helm revision 75 and Incus Helm revision 16
were deployed on both container computes. The two ``nova-compute-incus`` pods
used image ID
``ghcr.io/fivetime/openstackhelm/nova-incus@sha256:0a8f4f803dfdc1cfe40c7fb83ef85d0092a5e91c6455aa19c84cee3a9d11508c``;
the two ``incusd`` pods used
``ghcr.io/fivetime/incus@sha256:d61acad06073232f668a870f125c647afce04380b49deb33e39a51e13de9460d``.
Nova revision 75 raised ``[DEFAULT] reimage_timeout_per_gb`` to 120 seconds
after proving that the former 20-second default timed out before Cinder's
successful BFV reimage completion event. The production preflight now rejects
a value below the configured safety floor.

**Live migration matrices.** The 2 x 3 x 3 cardinality matrix covered local
and BFV roots, 0/1/3 Cinder data volumes, and 0/1/3 Manila shares. All 18
combinations completed the ordered three-hop ring
``container1 -> container2 -> container1 -> container2``: 54/54 live
migrations passed. Every case checked the unchanged guest PID, advancing
counter, root/data/share markers, exact Cinder attachment identity, OVN
destination ownership, source absence, and post-delete journal/profile/mount
cleanup. The maximum BFV case with three data volumes and three shares also
passed a target CRIU restore-failure injection: the source resumed with its
original PID and data, target resources were removed, and the immediate
three-hop retry passed. The post-reboot matrix log on ``control1`` is
``/tmp/live-cardinality-post-reboot.log`` (709 lines, SHA-256
``8dd9f7065a8f11cfd6f7ecaa11a5ca566017abed986986f5a2582c836626cdd7``)
and returned zero with its final residual-state audit green.

**Host restart coverage.** With the cloud quiescent, ``container1`` and then
``container2`` were physically rebooted one at a time. Their boot times and
boot IDs were respectively ``2026-08-17 20:08:18`` /
``1a6d1c3d-28c9-47bd-90a0-0ef0c8e35a40`` and
``2026-08-17 20:15:07`` /
``2413a485-eafd-4e6c-af46-702ff2b33cf7``. Kubernetes, Incus, LXCFS, OVS/OVN,
and Nova compute returned healthy before the next node was touched. The full
18-combination/54-migration matrix above was then rerun after both reboots.

**Cold, storage, and lifecycle coverage.** The BFV cold-migration matrix ran
normal confirm and revert, post-claim data-volume failure, target container
start failure, SHUTOFF failure, and reverse-revert failure in both ordered
node directions. The initial data-volume matrix covered local/BFV roots with
one/two volumes through create, format, marker write, hard reboot, and delete.
Manila covered destination pre-mount rejection, retry, compute restart
reattach, snapshot, create-from-snapshot, and marker recovery. Cinder covered
attach/detach, online extend, snapshot/clone, full plus incremental
backup/restore, and cold-migration attachment rotation. BFV lifecycle covered
pause/unpause, hard reboot, shelve-offload/unshelve, ACTIVE and SHUTOFF
explicit reimage, exact-delete ABA protection, and deletion protection. The
ID-map overlap rejection and leak recovery gates also passed.

**Final cleanup and remaining gate.** Nova reported no instances or active
migrations, and both host-local Incus inventories were empty. One detached
test data volume (``121a0324-437d-4074-a3a2-b4ea024b0a4c``) was verified by
exact name, ``available`` state, and zero attachments before deletion. The
Glance/Cinder read-only image cache volume was deliberately retained. Both
Incus compute services remained ``enabled/up``.

A destructive failed-host BFV evacuation was not run. This site currently has
no independent IPMI/Redfish/PDU fence provider, and
``[incus] allow_bfv_evacuate`` correctly remains false. An SSH shutdown, pod
deletion, or Nova service-down flag is not STONITH and must not be recorded as
evacuation evidence. Production BFV evacuation remains NO-GO until an
external fence proves the source cannot access Ceph and the returning-host
audit/admission sequence is exercised.

## 2026-08-21 three-node 500-per-node capacity rerun

The production rerun used ``container1``, ``container2``, and ``container3``
with Nova Helm revision 89. All three ``nova-compute-incus`` pods used
``ghcr.io/fivetime/openstackhelm/nova-incus@sha256:4ba36be3b354a11277514f1ea27b1370c189413453eb1d9c4ced422f3d5c52ac``
and completed the run with zero pod restarts. The image contained
openstack-incus ``7f90fde`` (bounded retry for transient etcd transaction
failures); the scale runner contained ``8362aec`` (15-instance bounded create
waves), and Nova used ``[DEFAULT] vif_plugging_timeout = 900`` from
openstack-helm ``34104e7ab``.

The pin smoke passed with one instance on each compute. The formal run
``959aaf77-08bb-4605-962a-7b6c989521bf`` then reached every requested
checkpoint with exact balanced placement: 100, 200, 300, 400, and 500
instances per compute. All 1,500 instances reached ``ACTIVE``. At every
checkpoint the runner verified Incus instance/profile ownership, Ceph root
images, OVN logical switch ports, Placement consumers/provider usage, Neutron
ports, etcd ID-map ownership, process limits, host storage, Ceph health, and a
ten-cycle periodic-task soak. The OVN inventory adapter was corrected during
the exercise to query each northbound Raft member until the current leader
returned a complete result; a follower's leader-only local socket is not a
valid fixed audit endpoint.

The functional capacity and integrity checks passed, but the formal result is
**not an all-green performance gate**. Incremental create-to-ACTIVE throughput
at 100/200/300/400/500 instances per compute was respectively 0.12762,
0.09354, 0.07433, 0.05945, and 0.04448 instances/second. The final stage was
below the configured 0.05 minimum; submit throughput was 0.04670/second.
Cumulative create-to-ACTIVE throughput across all 1,500 instances remained
0.06973/second. The evidence artifact is
``/root/openstack-incus-scale-current/evidence/scale-current-pc500-959aaf77-08bb-4605-962a-7b6c989521bf.json``.

At 500 instances per compute, Incus RSS was approximately 10.96 GB per node
and available host memory remained 30.8-32.2 GB. Cleanup removed all 1,500
servers in 6,737 seconds and passed its own SLO: delete API p95 was 0.716
seconds. Final residual counts were zero for Nova servers, Incus instances and
profiles, Neutron ports, Placement consumers, RBD images, and added OVN LSPs;
provider usage returned to zero and etcd ID-map inventory returned exactly to
its three-key baseline. The temporary scale network/subnet/flavor were deleted
and project quotas were restored to 10 instances, 20 cores, 51,200 MB RAM, and
500 ports.
