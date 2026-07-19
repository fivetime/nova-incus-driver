Production readiness
====================

This page is the authoritative release checklist for the Incus compute
driver. Historical test notes explain how a result was obtained but do not
reopen a completed implementation item. A release is production-ready only
when all eight gates below have current evidence for the exact driver commit,
Incus image digest, OpenStack deployment, and compute fleet being released.

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

The site acceptance matrix additionally interrupts the management network,
Incus, ``nova-compute``, OVN controller, Cinder connectivity, and the
orchestrator at ownership boundaries. Recovery must fail closed: uncertainty
may leak an inert record for reconciliation, but must never delete the
authoritative RBD or run two owners.

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

Rollback is allowed only while API extensions, database objects, and on-disk
metadata remain readable by the previous build. Never roll back an Incus
server after it has performed an irreversible database migration. In that
case roll forward or restore the complete Incus database and storage metadata
from a coordinated backup.

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
* returning-host reconciliation and final three-node fleet audit;
* known unsupported capabilities from
  ``support_matrix/capabilities.json``;
* approvers, timestamp, maintenance window, and rollback decision.

The release decision is ``NO-GO`` if any required gate was skipped, any
ownership query is ambiguous, or physical fencing has not been validated for
the deployment. Unsupported VM-only or deliberately rejected features do not
block this system-container product when they are accurately advertised.
