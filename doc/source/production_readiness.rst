Production readiness
====================

This page is the authoritative release checklist for the Incus compute
driver. Historical test notes explain how a result was obtained but do not
reopen a completed implementation item. A release is production-ready only
when all eight gates below have current evidence for the exact driver commit,
Incus image digest, OpenStack deployment, and compute fleet being released.
The presence of an E2E script or a passing unit-test contract is implementation
evidence, not proof that the current candidate passed that E2E. Never carry a
dated result forward across a driver, Incus image, Nova patch, configuration,
or fleet change.

1. Release source and immutable artifacts
-----------------------------------------

* The worktree is clean and the reviewed commit is pushed to a protected
  release branch.
* Unit tests, pep8, shell syntax, capability schema validation, and the Sphinx
  warning-as-error build pass from that commit.
* Every compute uses the same immutable ``ghcr.io/fivetime/incus`` digest,
  Incus source revision, and Nova driver hash.
* Repository and deployment files contain no credentials, private keys, or
  site fence secrets.

The local/static half is implemented by
``tools/openstack-incus-release-gate.sh``. Fleet identity is enforced by
``tools/openstack-incus-fleet-preflight.sh``. The latter hashes the Python
files in its own release tree and requires every deployed driver tree to match
that hash byte for byte; equality between computes alone is not sufficient.

2. Host and fleet readiness
---------------------------

Run the strict production and fleet preflights. They verify Ubuntu Noble,
Python 3.12, Incus 7.x, required API extensions, cgroup v2, AppArmor, disabled
core dumps, protected sockets and keys, a dedicated Incus control filesystem,
immutable container images, compute admission, disabled guest autostart,
Placement inventory, OVN agents, and the Cinder Ceph backend.

No warning is accepted as a pass. A failed node is disabled and quarantined
until the complete preflight succeeds again.

For a containerized compute, verify that ``CONF.instances_path`` is a
node-persistent ``hostPath`` (or an equivalent node-persistent mount), not
``emptyDir``. ``$state_path/compute_id`` is Nova's persistent compute identity
and must survive service and pod replacement; the fleet gate rejects missing
or duplicate identities. Exact-generation ID-map host claims and final release
intents live in the shared HA etcd registry, not in a host-local outbox, so any
surviving compute can finish a release after the deleting node loses its local
disk. An offline host claim still blocks reuse until the host returns and proves
cleanup, or an operator supplies external STONITH evidence and explicitly
retires it. Restart the ``nova-compute`` pod between injected Cinder and
Manila failure phases and prove that the new process completes the retained
volume/share journals. The incusd runtime must see the identical absolute path
read-only, with only the ``incus-shares`` subtree over-mounted
``rw,rshared`` when Manila is enabled.

Race the periodic retirement of an acknowledged ``cleaned`` ID-map host claim
against a new materialization claim for the same allocation generation and
compute UUID for at least 50 rounds. Both linearization orders must retain the
new exact token and keep the compute UUID in the allocation host index. Also
verify that Incus instance/profile inventory calls run outside the
cross-process claim lock; only exact Nova/etcd revalidation and retirement may
run inside its final critical section.

The compute image must contain GNU coreutils ``timeout`` at
``/usr/bin/timeout``. The release gate must prove that
``timeout --version`` identifies GNU coreutils, then inject a helper that
exceeds both ``share_mount_timeout`` and ``share_unmount_timeout``. Verify that
the complete child process tree is terminated within the configured bound plus
the five-second kill grace, no lazy or forced unmount is issued, and the owner
journal remains after an unmount timeout.

Exercise Manila transactions at 32 and 64 mappings. Instrument
``psutil.disk_partitions(all=True)`` and require exactly one mount-table scan
for each complete attach, detach, live-migration staging, or cold-migration
staging transaction. Inject failure while hydrating the final mapping and
prove no mount occurred. Then inject a later mount failure and prove previously
changed mappings roll back in reverse order while the original exception is
reported. Inject two unmount failures and prove every mapping was attempted,
both failed mappings remain ``ERROR``, and Manila access was not revoked.

Restart ``nova-compute`` with a retained migration-owned share journal. The
periodic recovery test must refuse ordinary attach journals, soft-deleted Nova
instances, local ownership, any active task, ``VERIFY_RESIZE``, a missing or
non-terminal related migration, and any surviving Incus instance or profile.
Only an exact terminal migration owner with no local runtime may be unmounted.
Also verify that host-boot resume forwards ``share_info`` into ``power_on`` so
active share devices are reconciled before the container starts.

Inject a process exit immediately after the destination profile is created
and its Manila journals are removed, both before target receive and after a
successful receive. After restarting ``nova-compute``, require the initial
profile to retain the exact prepared marker, cleanup token, Nova UUID, and
fixed idmap. For an aborted or failed attempt still owned by the source, prove
that periodic recovery fences the attempt, removes any target, unwires VIFs
and volumes, unmounts every staged share, and publishes the cleanup
acknowledgement. Repeat after failing the acknowledgement save and prove the
next pass retries it. For a committed attempt, prove that no unmount,
disconnect, VIF teardown, or target delete is issued; only an existing target
owned by Nova's destination may converge. Every missing, non-terminal,
``VERIFY_RESIZE``, deleted, mismatched, or otherwise ambiguous case must retain
all resources and emit an operator-visible error.

The aggregate gate must also run ``tools/openstack-incus-scale-e2e.py`` with
``RUN_SCALE=true``. The required checkpoints are 100, 500 and 1,000
simultaneously ``ACTIVE`` servers **per compute**
(``SCALE_PER_COMPUTE_CHECKPOINTS``); the runner multiplies them by the number
of mapped hosts, so on the mandated three-node fleet the cumulative figures
are 300, 1,500 and 3,000. At every checkpoint the runner
requires every server to be on the explicitly mapped Incus compute subset,
one active Neutron port bound to a host for every server, and exactly one
matching Incus instance and instance profile on the same Incus host. It
validates the runtime instance type, status, root pool, root size, CPU, memory,
PID and unprivileged-idmap limits. It also validates each Placement consumer
allocation, the aggregate provider usage delta, the underlying RBD inventory,
and the OVN logical switch ports. It records create, all-``ACTIVE``, Nova-list,
Neutron-list and throughput evidence in the JSON release artifact.

Configure the aggregate gate with immutable IDs and the complete enabled
compute inventory::

  RUN_TEMPEST=true
  TEMPEST_DIR=/opt/stack/tempest
  TEMPEST_BIN=/opt/stack/tempest/.tox/tempest/bin/tempest
  TEMPEST_CONFIG=/opt/stack/tempest/etc/tempest.conf
  RUN_PUBLIC_API_E2E=true
  PUBLIC_API_INITIAL_IMAGE=<admitted-non-bfv-image-uuid>
  PUBLIC_API_BFV_IMAGE=<admitted-bfv-image-uuid>
  PUBLIC_API_FLAVOR=<incus-flavor-uuid>
  PUBLIC_API_NETWORK=<tenant-network-uuid>
  PUBLIC_API_VOLUME_TYPE=<ceph-rbd-volume-type>
  RUN_SCALE=true
  SCALE_IMAGE=<admitted-image-uuid>
  SCALE_FLAVOR=<scale-flavor-uuid>
  SCALE_NETWORK=<scale-network-uuid>
  SCALE_INCUS_HOSTS="compute-1=root@192.0.2.11 compute-2=root@192.0.2.12 compute-3=root@192.0.2.13"
  SCALE_MIN_COMPUTE_HOSTS=3
  SCALE_EXPECTED_ROOT_POOL=ceph-rootfs
  SCALE_EXPECTED_PROCESS_LIMIT=64
  SCALE_RBD_INVENTORY_COMMAND="/root/bin/list-incus-root-rbds-json"
  SCALE_OVN_LSP_INVENTORY_COMMAND="/root/bin/list-ovn-lsps-json"
  SCALE_CEPH_STATUS_COMMAND="/root/bin/show-incus-root-ceph-status-json"
  SCALE_HOST_INITIAL_MIN_FREE_BYTES=<bytes>
  SCALE_HOST_INITIAL_MIN_FREE_PERCENT=<percent>
  SCALE_HOST_INITIAL_MIN_INODE_PERCENT=<percent>
  SCALE_HOST_RUNTIME_MIN_FREE_BYTES=<bytes>
  SCALE_HOST_RUNTIME_MIN_FREE_PERCENT=<percent>
  SCALE_HOST_RUNTIME_MIN_INODE_PERCENT=<percent>
  SCALE_MAX_HOST_SKEW_PERCENT=<percent>
  SCALE_MIN_SUBMIT_THROUGHPUT=<requests-per-second>
  SCALE_MIN_ACTIVE_THROUGHPUT=<servers-per-second>
  SCALE_MAX_CREATE_API_P95=<seconds>
  SCALE_MAX_ACTIVE_P95=<seconds>
  SCALE_MAX_ACTIVE_P99=<seconds>
  SCALE_MAX_NOVA_LIST_P95=<seconds>
  SCALE_MAX_NEUTRON_LIST_SECONDS=<seconds>
  SCALE_MAX_DELETE_API_P95=<seconds>
  SCALE_MAX_CLEANUP_SECONDS=<seconds>

Each whitespace-separated ``SCALE_INCUS_HOSTS`` entry maps Nova's exact
``OS-EXT-SRV-ATTR:host`` value to the corresponding SSH target. The runner
requires those mappings and ``SCALE_MIN_COMPUTE_HOSTS`` to equal the intended
Incus compute subset, and requires every mapped service to be enabled and
``up``. Other enabled computes, such as libvirt nodes, are allowed but may not
receive any run server. The selected Flavor must set
``trait:CUSTOM_INCUS_SYSTEM_CONTAINER=required``. A Flavor selecting
``incus:root_storage_pool`` must also require the corresponding
``CUSTOM_INCUS_STORAGE_POOL_*`` trait, and every mapped Placement provider
must expose all required traits. Preload and verify every SSH host key; the
audit uses strict host-key checking and non-interactive authentication.
``SCALE_CLOUD`` optionally selects a ``clouds.yaml`` entry. The concurrency,
delete concurrency, audit concurrency, bounded delete retry attempts/backoff,
inventory-command timeout, stage and cleanup timeouts, poll interval, Neutron
query chunk size, cleanup settle time, Incus project, and checkpoint list have
``SCALE_*`` overrides. The checkpoint list is
``SCALE_PER_COMPUTE_CHECKPOINTS`` and an override must still include the
per-compute values 100, 500 and 1,000. ``SCALE_MIN_COMPUTE_HOSTS`` must
exactly equal the number of entries in ``SCALE_INCUS_HOSTS``.
Project-scoped administrative credentials are required because Nova host and
Neutron binding attributes are part of the audit. The runner targets
OpenStackSDK 4.10 from OpenStack 2026.1.
All seven latency limits, both minimum throughput limits, and the maximum host
skew percentage are mandatory for a release decision. Submit throughput uses
the first request start through the last accepted response in the incremental
checkpoint stage. All-``ACTIVE`` throughput uses that stage's first request
through its last server becoming ``ACTIVE``. This excludes the intentional
audit pause between checkpoints. The artifact also records cumulative
throughput across the complete staged run. The JSON stores those epochs and
true submit-to-``ACTIVE`` latency rather than only the create API response
time. A resource cleanup that succeeds but exceeds its SLO is recorded as
completed with the exact observed violation, and the release gate still fails.

The RBD and OVN inventory commands are operator-owned, read-only helper
executables. Each must write one JSON list to stdout and return non-zero on
authentication, endpoint, parsing, or completeness failure. Entries may be
strings or objects with a non-empty ``name``. The RBD helper lists every image
whose name starts with ``container_`` or ``zombie_container_`` in the exact
Ceph pool backing ``SCALE_EXPECTED_ROOT_POOL``; persistent Incus ``image_*``
cache entries are deliberately outside the root-residual inventory. The OVN
helper lists every logical switch port name in the deployment. Do not hide
command failure with ``|| true``. The runner records a baseline before creation,
requires every run Neutron port to exist as an OVN LSP, requires at least one
exact ``container_<project>_<Nova-instance-name>`` RBD root image per
image-backed server, and requires both inventories to return exactly to
baseline after cleanup.

The Ceph status helper writes one JSON object containing non-empty ``fsid`` and
``pool`` strings, ``health=HEALTH_OK``, integer ``available_bytes``,
``pool_stored_bytes`` and ``pool_max_bytes``, and numeric ``raw_used_ratio``,
``nearfull_ratio`` and ``full_ratio``. It must fail rather than emit a
fabricated healthy or zero-capacity result when any Ceph query is incomplete.

The Tempest phase first records the discovered tests, proves that the
``nova-incus-tempest-plugin`` entry point resolves to the renamed package,
then runs its system-container scenarios and the complete standard
``tempest.api.compute`` public API suite selected by
``tools/openstack-incus-tempest-include-list.txt``. Unsupported capabilities
must be disabled in ``tempest.conf`` consistently with the versioned support
matrix; removing a failing supported test from the include list is not an
acceptable release workaround.

Before creation, the runner fails if current Nova instance, core, or RAM quota
or detailed Neutron port quota cannot hold the maximum checkpoint. Operators
must separately reserve enough Incus root-pool, Ceph, OVN, DHCP, API, message
queue, and database capacity for 1,000 instances; quota headroom alone is not
capacity evidence. The default create and delete concurrency is 16, and work
submission stops after the first failed bounded batch instead of queueing the
rest of a stage.

Each create carries an exact run UUID and a separate random cleanup token.
The artifact uses a small fsync'd append-only WAL per accepted create rather
than rewriting an ever-growing JSON document for every server. Each checkpoint
atomically compacts that WAL into the main artifact; cleanup replays an
uncompacted WAL after an orchestrator crash.
Cleanup verifies that metadata pair, discovers a create whose API response was
lost, and deletes only the resulting server UUIDs. Known transient delete API
failures are retried with a bounded exponential backoff, with the exact
metadata ownership pair re-read before every attempt. Cleanup requires two
clean inventory observations plus a 30-second quiet window followed by an
exact UUID ``GET`` for every server. It then requires zero Neutron ports and
zero Incus instances carrying any run server UUID, plus zero profiles with the
exact Nova instance names persisted in the artifact. Placement consumer
allocations must be gone, provider usages and the complete RBD/OVN inventories
must equal their pre-run baselines. Backend residual checks are retried until
the bounded cleanup deadline so normal asynchronous deletion can settle; the
final decision remains fail-closed. It never selects a Nova server for
deletion by name. The artifact stores SHA-256 fingerprints of both inventory
helper command lines and cleanup refuses a different helper.
Do not use ``--keep`` for release evidence. After an orchestrator crash, pass
the scale JSON and the same inventory helper commands back through the
runner's ``--cleanup-artifact`` mode before repeating the gate.

3. External fencing and failed-host evacuation
----------------------------------------------

Production uses an independently hosted IPMI, Redfish, or PDU agent. Stopping
Podman, Incus, ``nova-compute``, or a host network service is not fencing.
Before enabling ``[incus] allow_bfv_evacuate``:

#. Execute ``tools/openstack-incus-fence-preflight.sh`` for every compute.
#. Run ``tools/openstack-incus-bfv-evacuation-e2e.sh`` from a controller that
   is not the source or destination. Every OpenStack API endpoint used by the
   test must also remain independent of the source; the script rejects an
   endpoint hostname or address that resolves to the fence source.
#. Prove the source is powered off and has zero Ceph watchers before Nova
   evacuation starts.
#. Prove one target attachment, watcher, Neutron binding, and OVS owner.
#. Power the source on, prove it remains quarantined, run
   ``tools/openstack-incus-returning-host-audit.sh``, then explicitly admit it.

The independent KVM ``fence_virsh`` gate is release evidence for the software
workflow. Each physical site must repeat it with its real BMC or PDU.

4. Failure-injection matrix
---------------------------

Run ``tools/openstack-incus-bfv-migration-matrix.sh`` on every directed
compute pair. Its required cases cover destination preflight failure,
post-claim data-volume failure, post-claim start failure, stopped-instance
recovery, reverse-revert failure, normal confirm, and residual-state audits.
The aggregate gate derives all six directed pairs from exactly three explicit
``MIGRATION_COMPUTE_NODES`` entries and refuses defaults for images, Flavor,
network, Cinder types, or shares::

  RUN_MIGRATION_MATRIX=true
  MIGRATION_COMPUTE_NODES="compute-1=root@192.0.2.11,compute-2=root@192.0.2.12,compute-3=root@192.0.2.13"
  SSH_IDENTITY=/root/.ssh/incus-release
  SSH_KNOWN_HOSTS_FILE=/root/.ssh/known_hosts
  CONTROLLER_SSH=root@192.0.2.10
  MIGRATION_LOCAL_IMAGE=<admitted-criu-local-image-uuid>
  MIGRATION_BFV_IMAGE=<admitted-criu-bfv-image-uuid>
  MIGRATION_FLAVOR=<incus-flavor-uuid>
  MIGRATION_NETWORK=<tenant-network-uuid>
  MIGRATION_ROOT_VOLUME_TYPE=<ceph-bfv-type>
  MIGRATION_DATA_VOLUME_TYPE=<ceph-data-type>
  MIGRATION_MANILA_SHARES="<share-uuid-1> <share-uuid-2> <share-uuid-3>"

Preload the exact controller and compute host keys into
``SSH_KNOWN_HOSTS_FILE`` over a separately authenticated channel. The release
gate verifies every ``COMPUTE_NODES``, ``MIGRATION_COMPUTE_NODES`` and remote
``CONTROLLER_SSH`` host with ``ssh-keygen -F`` before running any remote phase.
The fleet and migration scripts use ``BatchMode=yes``,
``StrictHostKeyChecking=yes`` and that exact ``UserKnownHostsFile``; they never
learn or replace a host key during a release run.

Also run ``tools/openstack-incus-live-migration-matrix.sh`` across three
computes. It covers the 2x2x2 combination of local/BFV root, absent/present
Cinder data volume, and absent/present Manila share. Every combination must
complete a three-hop round trip and leave the Nova and Cinder inventories
unchanged. The production preflight must confirm that the outer Incus image
contains GNU tar with ``--no-unquote`` support so CRIU can checkpoint
non-empty tmpfs mounts.

Run the maximum BFV + two Cinder data volumes + Manila combination once more
with target CRIU restore failure injection. The source must resume with
continuous process state, all destination ownership must be removed, and an
immediate retry must succeed. A failed optional CRIU pre-dump must degrade to a
full final checkpoint rather than aborting the migration stream.

Run ``tools/openstack-incus-live-migration-cardinality-matrix.sh`` with at
least three independent Manila shares. The release inputs must identify a
real NFS share and a different real CephFS share among those three; three
unique UUIDs without protocol verification are not evidence. The matching NFS
and CephFS share types must advertise ``snapshot_support=True`` and
``create_share_from_snapshot_support=True``, and
``tools/openstack-incus-manila-snapshot-e2e.sh`` must create, snapshot,
restore, attach, read and delete a fresh share on each backend. The real
snapshot operations, rather than the share-type declaration alone, prove the
backend capability. The default ``0 1 3`` cardinalities cover both root models
crossed with zero, one, and multiple Cinder data volumes and Manila shares.
Every one of the 18 cases must complete the three-hop round trip and restore
Nova, Cinder, Neutron, and Placement to their baselines. Repeat the BFV +
three-data-volume + three-share maximum with target restore failure injection.

For every Manila migration test, query Placement rather than assuming that
the compute aggregate is homogeneous. The exact source and every eligible
destination compute resource provider must advertise
``CUSTOM_INCUS_MANILA_COLD_MIGRATION`` or
``CUSTOM_INCUS_MANILA_LIVE_MIGRATION`` for the operation under test. As a
negative gate, temporarily remove the operation trait from one otherwise
eligible destination and prove a scheduler-mediated move never selects it.
Remove the trait from the source and prove the API rejects the move before
changing instance task state. Also exercise the legacy forced-live-migration
request against a destination without the trait and prove that it is rejected
before any Placement allocation, Neutron binding, Cinder attachment, or
Manila staging mount is created.

Before advertising either trait, enumerate every ``nova-api`` host in
``NOVA_API_NODES`` and set ``RUN_MIGRATION_MANILA=true``. The fleet gate must
inspect every running API process and every running ``nova-compute`` process,
not merely a source file on the host. The API runtime must contain the Manila
source/destination scheduling gate. Every compute runtime must contain the
Nova core hooks ``_pre_deny_share``,
``_prepare_live_migration_check_data``, and
``_complete_live_migration_rollback``, start through
``nova-incus-compute``, and load their ``IncusComputeManager`` overrides. The
corresponding Placement provider must expose the share, cold-migration and
live-migration traits. A patch on only one service role, a stopped patched
container, or an unselected Python installation is a release blocker.
The inspector derives a systemd uWSGI ``--venv`` or the running process PATH
when possible. A container runtime that rewrites both must set
``NOVA_API_RUNTIME_PYTHON`` or ``NOVA_COMPUTE_RUNTIME_PYTHON`` to the
interpreter inside that service's mount namespace; pointing it at a host-side
or unrelated virtualenv is invalid evidence.

The site acceptance matrix additionally interrupts the management network,
Incus, ``nova-compute``, OVN controller, Cinder connectivity, and the
orchestrator at ownership boundaries. Recovery must fail closed: uncertainty
may leak an inert record for reconciliation, but must never delete the
authoritative RBD or run two owners.

Volume-journal crash recovery must use the production Ceph RBD connector. The
journal intentionally strips passwords, keys, keyrings, secrets, and tokens.
For RBD, restart the compute process after the host mapping exists but before
the device metadata commit, then prove that the protected host keyring allows
the idempotent reconnect and cleanup to complete. Do not certify a non-RBD
connector whose recovery depends on an expired or single-use
``connection_info`` secret; it remains unsupported until the connector can
reacquire credentials without persisting them in the journal.

5. Monitoring and ownership alarms
----------------------------------

Production monitoring must alert on:

* a failed or ambiguous fence operation;
* an admitted compute whose admission token is absent or stale;
* a returning host running ``nova-compute`` before reconciliation;
* more than one Cinder attachment, Ceph watcher, KRBD mapping, Neutron binding,
  or OVS owner for an instance root;
* stale ``pending`` handover state or a durable Nova recovery marker;
* Incus console-log, host coredump, control-filesystem, tmpfs, PID, memory,
  swap, and rootfs limit pressure;
* Nova, Placement, Cinder, Neutron/OVN, Incus, image digest, or driver-hash
  drift.

Alerts must carry the instance, volume, source host, target host, and last
successful fence evidence. Automatic recovery stops when ownership is
ambiguous.

Run ``tools/openstack-incus-monitoring-audit.sh`` from the independently
hosted monitoring plane on every collection interval. It is a fail-closed
probe for fleet drift, compute admission, control-filesystem pressure,
unbounded Incus logs, pending handovers, recovery markers, and BFV ownership.
For every discovered BFV root it correlates the Nova host and power state,
Cinder attachment, Incus runtime, Ceph watcher, fleet-wide KRBD mapping,
Neutron binding, and fleet-wide OVS owner by instance and volume ID. A
non-zero result must page the compute/storage owner; successful probe output
is not a substitute for configuring the monitoring system's notification
route. For every running instance it also checks PID, memory, swap, and OOM
cgroup signals plus ``/``, ``/run``, and ``/dev/shm`` usage. Set
``FENCE_EVIDENCE_FILE`` to the root-owned, non-writable terminal log from the
last successful external-fence evacuation. The probe checks its terminal
result, age, and SHA-256; ``FENCE_EVIDENCE_MAX_AGE_SECONDS`` defaults to 30
days. The notification route remains site-specific and must be tested
separately.

6. Upgrade and rollback
-----------------------

Validate one compute at a time: disable scheduling, drain or stop workloads,
quarantine the service, upgrade the driver and immutable Incus image, run the
host preflight, explicitly admit it, and re-enable scheduling. Test local-root
development instances, BFV roots, attached data volumes, OVN ports, and
recovery markers across the upgrade.

After a fenced host returns, keep scheduling disabled while the ownership
audit removes stale instance records. Admit and start ``nova-compute``, wait
until its service state is ``up``, and only then enable the service. Finally,
require the root resource provider to lose ``COMPUTE_STATUS_DISABLED`` before
declaring the host schedulable. Enabling the service while its heartbeat is
still down causes Nova to defer that Placement synchronization and can leave a
healthy process temporarily unable to receive builds.

Rollback is allowed only while API extensions, database objects, and on-disk
metadata remain readable by the previous build. Never roll back an Incus
server after it has performed an irreversible database migration. In that
case roll forward or restore the complete Incus database and storage metadata
from a coordinated backup.

Before release, prove all storage-owner recovery paths independently:

* ``openstack-incus-snapshot-e2e.sh`` for an Incus-managed root, including a
  local root pool selected through its production Flavor;
* ``openstack-incus-ceph-backup-e2e.sh`` for a Cinder data volume; and
* ``openstack-incus-bfv-backup-e2e.sh`` for a Cinder BFV root restored and
  booted on another compute; and
* ``openstack-incus-manila-snapshot-e2e.sh`` for every Manila backend that
  advertises snapshot and create-from-snapshot support.

The release record must identify the independent failure domain holding each
backup. A backup RBD pool in the source Ceph cluster is not sufficient evidence
for recovery from loss of that cluster.

7. Operations and security acceptance
--------------------------------------

Operators must rehearse fencing, evacuation, stale handover inspection,
returning-host audit, explicit admission, Ceph/Cinder outage handling, OVN
repair, capacity exhaustion, backup restore, and escalation. Tenant access is
limited to Nova APIs and unprivileged system containers; tenants never receive
Incus, Podman, Ceph, OVS/OVN, or host API access.

Keep fence secrets in root-owned ``0600`` files and pass them to standard fence
agents through stdin. Restrict ``incus-admin`` membership, preserve
``boot.autostart=false``, disable host core dumps, bound console logs and
tmpfs, and enforce Flavor-derived CPU, memory, swap, PID, rootfs, and I/O
limits.

8. Release record and go/no-go
------------------------------

Archive the output of ``tools/openstack-incus-release-gate.sh`` with:

* Git commit and tree status;
* Incus image digest and source revision;
* OpenStack release, configuration hashes, and compute inventory;
* every directed migration-matrix result;
* one destructive external-fence evacuation per fencing implementation;
* ``openstack-incus-bfv-cow-e2e.sh`` proving that Glance-to-Cinder BFV
  provisioning retains an RBD parent and non-zero overlap;
* the scale JSON proving the 100, 500 and 1,000 per-compute checkpoints,
  complete
  Incus/Neutron ownership audits, and successful exact-ID cleanup;
* returning-host reconciliation and final three-node fleet audit;
* known unsupported capabilities from
  ``support_matrix/capabilities.json``;
* approvers, timestamp, maintenance window, and rollback decision.

The release decision is ``NO-GO`` if any required gate was skipped, any
ownership query is ambiguous, or physical fencing has not been validated for
the deployment. Unsupported VM-only or deliberately rejected features do not
block this system-container product when they are accurately advertised.
