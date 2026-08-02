Scale hotspot elimination design
================================

Status: **approved design, implementation pending**. This chapter records the
agreed design for removing the remaining super-linear driver and periodic
audit costs before the 100/500/1,000-instance release gate becomes routine.
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
- All etcd periodic lookups outside the full audit use exact keys or
  bounded prefixes (``releases/``, ``hosts/<host_id>/``), never a full
  namespace scan, and every recovery periodic rotates a cursor over a
  bounded batch.

Every mutating driver path invalidates the caches before and after the
Incus write, so a raced periodic read can never publish a stale snapshot.
This baseline is validated by the cache single-flight unit tests and by the
idle-soak phase of ``tools/openstack-incus-scale-e2e.py``.

Remaining hotspot 1: all-project absence proofs are O(N²)
---------------------------------------------------------

``IncusDriver._all_project_idmap_resources_absent`` and its spawn-attempt
sibling each issue two unscoped recursive listings (instances and profiles)
and scan every record. The idmap host-claim reconciliation and release
replay periodics call them up to twice per candidate, with batches of up to
100 candidates per 60-second cycle. When the number of pending claims or
release intents scales with instance count, the per-cycle cost is
effectively O(N²).

Design
~~~~~~

Split the proof into a cheap screening phase and an authoritative
confirmation phase:

1. **One shared all-project snapshot per periodic cycle.** At the start of
   each reconciliation/replay cycle the manager fetches the two unscoped
   listings once and passes the snapshot to every candidate evaluation in
   that batch. Cost per cycle becomes O(N), independent of batch size.
2. **Snapshot presence is authoritative negative evidence.** If the
   snapshot shows a matching instance, profile, UUID reference, or idmap
   range overlap, the candidate is blocked immediately — a stale snapshot
   can only over-retain, never over-release, which is the fail-closed
   direction.
3. **Snapshot absence is only a screen.** A candidate whose resources are
   absent from the snapshot proceeds to the existing exact per-candidate
   proof (fresh all-project listing plus local path and journal checks)
   immediately before the claim is retired or the release intent is
   completed. The authoritative absent-proof semantics required by the
   registry specification are unchanged; the exact scan simply runs only
   for candidates that are actually about to release, which is a
   per-lifecycle event rather than a per-cycle event.

Amortized cost drops from O(batch × N) per cycle to O(N) per cycle plus
O(N) per actual retirement. No registry schema, claim state, or proof
digest changes are required.

Remaining hotspot 2: the full registry audit is O(hosts × N) per minute
-----------------------------------------------------------------------

``_audit_incus_idmap_allocator`` reads the entire registry namespace
(instances, slots, releases, host claims) through one linearizable
``get_prefix`` every ``idmap_allocator_audit_interval`` (default 60 s) on
**every** compute. Fleet-wide this is O(hosts × N) etcd reads and JSON
parses per minute, all of it steady-state overhead on a healthy registry.

Design
~~~~~~

Keep the full audit as the integrity authority, but stop paying for it
every cycle on every host:

1. **Full audit stays at process start** (``init_host``) and remains the
   fail-closed latch. Unchanged.
2. **Cheap drift probe every cycle.** Between full audits each cycle runs a
   count-only probe: one ``keys_only``/count range per key family plus the
   config-key generation read. The probe checks the family cardinality
   invariants (``len(instances) == len(slots)``, host-claim count equals
   the sum of ``host_ids`` lengths, release count bounded by instance
   count) and the config fingerprint. Cost is O(1) request count and O(1)
   payload per cycle.
3. **Full audit every K cycles with per-host jitter.** Default one full
   audit per 15 minutes (config: ``idmap_allocator_full_audit_interval``,
   minimum 300 s), with a random per-process phase offset so a fleet does
   not synchronize its full scans. Any drift-probe mismatch, any CAS
   conflict surprise, and any registry mutation error immediately schedules
   an out-of-band full audit before the next mutation is allowed.
4. **Latch semantics unchanged.** Integrity failure from either the probe
   escalation or the periodic full audit permanently latches the process
   closed exactly as today.

Fleet-wide steady-state audit cost drops from O(hosts × N) per minute to
O(hosts) per minute plus O(hosts × N) per 15 minutes, without weakening
the integrity guarantee (every corruption class the full audit detects is
still detected, at worst 15 minutes later, and every local mutation error
still forces an immediate full audit).

Remaining hotspot 3: duplicate full profile listings per recovery cycle
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
instead of two.

Performance baseline and regression gate
----------------------------------------

The fail-closed scale runner (``tools/openstack-incus-scale-e2e.py``) is
the measurement instrument; the release gate already refuses to run it
without explicit SLO limits. The remaining work is to *record* the first
approved baseline:

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
