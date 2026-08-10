Scale hotspot elimination design
================================

Status: **code-level hotspot elimination implemented**. The current
100/500/1,000-instance production baseline and release gate are still
deferred; ``TEST_STATUS.md`` remains authoritative for that incomplete
evidence. This chapter records the implemented design for removing the
super-linear driver and periodic audit costs before that gate is rerun.
It complements :doc:`architecture` (which documents behavior that already
exists) and :doc:`production_readiness` (which defines the release evidence).

Baseline: what is already linear
--------------------------------

The working tree already contains the first round of inventory work, with
single-flight generation-guarded caches and rotating-cursor batching:

- One recursive instance inventory snapshot per 10 seconds serves
  ``get_info``, ``get_num_instances``, ``list_instance_uuids``,
  ``list_migration_recovery_candidates``, and vCPU accounting, so Nova's
  power-state reconciliation costs one Incus request per interval instead of
  one per instance.
- One ``/1.0/metrics`` scrape per second (maximum) serves ``block_stats``
  and ``get_all_volume_usage``.
- A single-use host resource snapshot halves per-ResourceTracker-cycle host
  probing.
- Per-host etcd recovery starts from the exact ``hosts/<host_id>/`` reverse
  index. Release intents for those claims are fetched by exact keys in one
  transaction; no compute scans the fleet-wide ``releases/`` prefix. Only
  the elected audit coordinator reads full registry families.

Every mutating driver path invalidates the caches before and after the
Incus write, so a raced periodic read can never publish a stale snapshot.
This baseline is validated by the cache single-flight unit tests and by the
idle-soak phase of ``tools/openstack-incus-scale-e2e.py``.

Hotspot 1 (fixed): exact absence proofs no longer scan every project
--------------------------------------------------------------------

``IncusDriver._all_project_idmap_resources_absent`` used to issue two
unscoped recursive listings (instances and profiles) and scan every record.
Calling that proof for every release candidate made deletion and recovery
effectively O(N²).

Design
~~~~~~

The final absence proof is now an indexed Incus operation rather than a
cached assertion:

1. **Indexed exact query on current Incus.** The ``idmap_usage`` API
   extension adds ``GET /1.0/idmap-usage?owner=<uuid>&base=<n>&size=<n>``.
   It returns every instance and profile, across all projects, whose
   effective ``user.openstack.uuid`` matches or whose effective half-open
   ID-map range overlaps the request. Candidate selection uses partial
   expression indexes containing only those idmap keys, not arbitrary large
   instance configuration values. The database query implements expanded
   configuration semantics: a non-empty ``volatile.idmap.base`` takes
   precedence over local ``security.idmap.base``; local values override
   profiles and, otherwise, the last applied profile value for each key wins.
   A missing, empty, or ``auto`` ID-map size is 65536. Profiles themselves are
   also returned.
2. **Equivalent stored forms cannot evade the index.** UUID matching is
   case-, brace-, hyphen-, and ``urn:uuid:``-insensitive. Base and size are
   compared numerically, so historical values with leading zeroes are the
   same range. Malformed, zero-sized, or overflowing stored ranges make the
   query fail instead of being interpreted as absent. The request also
   rejects a zero-sized or overflowing uint32 range.
3. **The exact result is authoritative for its database snapshot.** Nova
   calls the API immediately before retiring the local claim or completing a
   release.
   It validates the response shape and retains on any foreign match. The
   instance's own profile is the only allowed match, under the existing
   project/name rule. No TTL result or local inventory cache authorizes a
   release. The response is not a linearizable reservation: it does not
   include Incus in-memory transient or node-local migration-attempt
   reservations and it cannot cover an unmanaged Incus mutation after the
   response. Production therefore keeps Incus management dedicated and
   serializes Nova allocation actors through the external registry. This API
   must not be used to authorize releases while arbitrary actors can mutate
   Incus concurrently.
4. **Rolling upgrades fail closed without weakening old nodes.** When the
   extension is absent, Nova retains the old fresh recursive all-project
   instance/profile scan as the final proof. A shared immutable snapshot may
   screen a legacy periodic batch, but snapshot absence never authorizes a
   release; each actual retirement repeats a fresh scan.

The recovery loops also avoid generating candidates unnecessarily. Host
claim reconciliation performs one ``hosts/<host_id>/`` prefix read and one
Nova ``InstanceList.get_by_host`` read, removes all live local owners, then
rotates and truncates the stale candidate list to 100. Thus 1,000 live claims
and no stale claim perform no Incus inventory or exact absence query. The
filter is deliberately before truncation, so a stale claim behind a large
live prefix is handled in the same cycle. If the Nova bulk read fails, the
loop falls back to the previous exact per-claim checks.

Release replay starts from that same host index and fetches the corresponding
release intents by exact keys in one etcd transaction. It never scans the
global release prefix. Only intents that actually exist enter the bounded
100-candidate lifecycle path. Those pending candidates still require exact
Nova ownership, allocator-generation, cleanup-proof, and CAS checks one by
one; this bounded O(K), ``K <= 100``, work is the safety-authoritative state
transition itself and cannot be replaced by a non-transactional cache or
ordinary bulk read.

For a normal destroy on an Incus server with ``idmap_usage``, the final
cross-project proof is one indexed query instead of two O(N) recursive
listings. With the production 65536-wide allocator geometry its cost is
O(log N + matches). Resources that explicitly configure another width are
kept in covering partial indexes and add O(C + F), where C is the number of
custom-size configuration entries and F is their attached-profile fan-out;
this preserves arbitrary interval-overlap correctness without slowing the
normal fixed-width path. A legacy server remains O(N) for that final proof.
Periodic active-claim screening is O(claims-on-host + live-instances-on-host)
with no per-live-claim Incus request.

Hotspot 2 (fixed): the full registry audit was O(hosts * N) per minute
------------------------------------------------------------------------

``_audit_incus_idmap_allocator`` used to read the entire registry namespace
(instances, slots, releases, host claims) on every compute. It also discovered
a coordinator by listing Nova compute services from every host. The first cost
was O(hosts * N); the second approached O(hosts * hosts) control-plane work.

Design
~~~~~~

The allocator now elects one auditor with an etcd lease and makes that lease's
exact value the fleet health generation:

1. **Lease-backed election.** A process compare-and-swaps an absent
   ``/openstack-incus/idmaps/v3-control/<namespace>/coordinator`` key to
   ``pending`` under an etcd lease. The lease lifetime is three audit cycles.
   There is no Nova service inventory query. An absent key after lease expiry
   permits one caller to acquire it; every acquisition requires a complete
   audit before the key may become ``healthy``.
2. **Available, fail-closed audit transition.** Every audit receives a new UUID
   generation. Initial acquisition and lease takeover publish ``pending`` and
   reject sensitive work until a complete audit succeeds. A routine audit by
   the existing owner retains the previous leased ``healthy`` generation while
   scanning, so an O(G) scan does not stop fleet lifecycle operations. Those
   operations still compare that exact value, its positive lease ID, absence of
   the failure key, and their exact ownership records in one transaction. Audit
   success atomically rotates to a new healthy generation. An ambiguous audit
   atomically replaces the old generation with ``pending``; a content error
   publishes the sticky failure. A value restored without its lease must also
   be taken over as ``pending`` and fully re-audited.
3. **Sticky fleet failure.** A content-level integrity error writes the first
   failure to the sibling ``failure`` key without a lease. Every process reads
   it before sensitive work and fails closed. If publishing that failure itself
   fails, the coordinator remains ``pending``; its lease expiry lets another
   process take over and repeat a full audit, never declare an ambiguous scan
   healthy.
4. **Coordinator-only probe and full scan.** Between full audits the owner runs
   one count-only transaction over the four registry families. The response is
   O(1), although etcd may do O(N) server work to count a prefix. A cardinality
   mismatch escalates to a full scan rather than becoming failure evidence by
   itself. Content relationships remain the full audit's job, bounded by
   ``idmap_allocator_full_audit_interval`` (default 900 s).
5. **Rolling upgrade compatibility.** The coordination keys deliberately live
   outside ``/openstack-incus/idmaps/v3/<namespace>/``. An old binary therefore
   does not encounter an unknown record in its strict audit. Deployments must
   grant the new sibling prefix in etcd RBAC *before* starting a new binary.
   Old computes continue their legacy per-process audits but cannot honor the
   new sticky failure or lease generation. The first upgrade that introduces
   this protocol must therefore freeze ownership-changing instance operations,
   upgrade every compute in the migration domain, and unfreeze only after one
   new coordinator publishes ``healthy``. Once every binary understands this
   protocol, ordinary rolling updates retain the fleet generation and only the
   lease owner scans.

Followers pay one bounded exact etcd transaction per sensitive operation and
one two-key health read per periodic cycle. Fleet-wide periodic work is one
O(N) count pass per audit cycle and one O(N) full scan per full-audit interval;
election itself is O(1) and no longer adds O(hosts * hosts) Nova queries. A
failed owner is taken over after at most the lease TTL, and the takeover always
performs a full audit before admitting work.

Hotspot 3 (fixed): duplicate full profile listings per recovery cycle
-----------------------------------------------------------------------

``_recover_incus_cleanup_profiles`` and
``_recover_incus_destination_profiles`` each issue their own
``GET /1.0/profiles?recursion=1`` at the same spacing. The two periodics
run back to back in the same process.

Design: both periodics consume one shared profile snapshot fetched at most
once per recovery interval through the same generation-guarded single-flight
helper the instance inventory already uses (TTL = half the recovery
interval). Candidate *action* paths keep their exact per-profile reads; only
candidate *discovery* shares the snapshot. Cost: one O(N) listing per cycle
instead of two. Any instance-inventory invalidation drops this snapshot with
it, so a mutation cannot be served a pre-mutation profile list.

Performance baseline and regression gate
----------------------------------------

The fail-closed scale runner (``tools/openstack-incus-scale-e2e.py``) is
the measurement instrument; the release gate already refuses to run it
without explicit SLO limits. The remaining work is to *record* the first
approved baseline. Completing the code changes above does not make this
evidence gate green:

1. Run the gate's scale phase at per-compute checkpoints 100/500/1,000 on
   the three-node testbed with the candidate that contains the hotspot
   fixes above.
2. Record in ``TEST_STATUS.md``: the seven latency SLO observations
   (p50/p95/p99 as reported), both throughput figures, per-audit-phase
   timings, idle-soak inventory query percentiles, and the telemetry
   ceilings observed for ``incusd`` and ``nova-compute`` CPU/RSS/FD.
3. Derive the release SLO limits from that baseline (observed p95 plus
   agreed headroom, not aspirational numbers), so later regressions fail
   the gate against measured reality.
4. Re-run the idle-soak phase after the hotspot fixes and record the etcd
   request-rate delta attributable to the audit redesign (the drift probe
   makes this directly observable in the ``idmap_etcd_inventory`` audit
   phase timing).

Explicit non-goals
------------------

- No etcd watch streams. A watch would reduce audit latency but introduces
  reconnect/compaction failure modes and a second consistency mechanism;
  the CAS-plus-periodic-audit model stays authoritative.
- No caching of etcd reads that feed mutations. Every CAS keeps reading its
  exact compare keys; only Incus inventory listings and audit scheduling
  change.
- No relaxation of the fail-closed latch, the release-intent barrier, or
  the per-compute absence proof that guards range release.
