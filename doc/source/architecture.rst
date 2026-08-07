OpenStack-Incus Architecture and Scope
======================================

Purpose
-------

This project modernizes the existing Nova-Incus compute driver into a current
Incus system-container driver. It is an incremental evolution of the existing
Nova driver, DevStack plugin, Tempest plugin, VIF design, and tests, not an
unrelated replacement.

The first compatibility target is OpenStack ``stable/2026.1``, Ubuntu Noble,
Python 3.12, and Incus 7.x. Development and integration testing use native
DevStack services first. Container images are a deployment packaging concern,
not the driver architecture.

Release evidence must come from a multi-node topology where every compute uses
that target host and Python baseline. A successful mixed-version test can show
forward compatibility but cannot close the Noble/Python 3.12 release gate.

Control-plane ownership
-----------------------

OpenStack remains the authoritative multi-tenant control plane:

* Keystone owns identity and authentication.
* Nova owns instance records, tenant ownership, lifecycle authorization, and
  compute quotas.
* Scheduler and Placement own cross-node placement and resource claims.
* Neutron ML2/OVN owns ports, IPAM, security groups, logical networking,
  routing, floating IPs, and cross-node Geneve connectivity.
* Glance owns image visibility and image data.
* Cinder owns volume authorization and lifecycle.

Incus is a host-local execution backend controlled only by the local
``nova-compute`` service. Each compute host runs an independent Incus daemon;
Incus clustering is neither required for placement nor for OVN cross-node
traffic. Tenants never receive Incus, Podman, Ceph, OVS, or OVN management API
access.

Every Nova-created Incus instance stores its authoritative Nova UUID in
``user.openstack.uuid``. Startup reconciliation ignores Incus instances
without that marker and all Incus virtual machines. The configured Incus
project is a private driver backend and must not be used for manual containers,
CRIU experiments, or other workloads. Those operations require a separate
Incus project. An administrator can copy or forge ``user.openstack.uuid``;
therefore the marker is an ownership assertion inside this trusted boundary,
not an authentication credential. If multiple containers claim one Nova UUID,
the driver reports an integrity error and returns the UUID only once during
Nova reconciliation. It never guesses which record is safe to delete.

OpenStack tenant identity is not duplicated in Incus. The host-local driver
project isolates Nova-managed records from other host administration, but
remains subordinate to Nova's identity and authorization model.

Network boundary
----------------

The existing VIF approach is preserved and modernized against current Nova,
Neutron, neutron-lib, and os-vif interfaces:

::

   container eth0
       |
   Incus physical NIC
       |
   container-side veth
       |
   host-side veth -> OVS br-int -> ovn-controller -> Geneve

Neutron provides ``network_info``. The driver and os-vif prepare and remove
the host VIF and OVS port; Incus only attaches the container-side interface.
Incus must not create managed tenant OVN networks, ACLs, forwards, zones, or
other resources that duplicate Neutron state.

Interfaces attached after spawn are instance-local Incus devices. Unlike
profile updates, an Incus instance update rolls back when a concurrent
lifecycle operation prevents the runtime device change, so the driver can
safely refresh state and retry it. Existing instances can still have NICs
in their instance-specific profile. Detaching one first masks the inherited
NIC with an instance-local ``type=none`` device, then removes the profile
entry, confirms Incus's documented ``profile change still saved`` partial
success when encountered, and finally removes the mask. This keeps the
effective runtime and persistent device state aligned throughout recovery.
A Neutron VIF-deleted event received while Nova is already deleting the
server only performs the idempotent host VIF unplug; the owning destroy path
removes the Incus instance and profile.

Compute and security boundary
-----------------------------

The driver supports Incus system containers only; Incus VM/QEMU support is out
of scope. A tenant can have root inside its container and use apt, dnf, or yum,
but every instance must be unprivileged, use an isolated ID map, and have
explicit CPU, memory, process, and rootfs limits. Container UID 0 must map to a
non-zero host UID. Privileged containers, nesting, raw LXC configuration,
arbitrary devices, and host bind mounts are prohibited.

The driver enforces this boundary while constructing every instance profile.
``security.privileged`` is always false, ``security.idmap.isolated`` is always
true, and a host without the Incus ``id_map`` API extension is rejected.
Flavor requests for privileged or nested containers fail instead of weakening
the boundary. ``[incus] default_process_limit`` defaults to 1024 processes.
An operator-created flavor can override it with the positive integer extra
spec ``incus:process_limit``. The override cannot exceed
``[incus] maximum_process_limit``, which defaults to 65536. Both configuration
values must be identical on every compute so migration cannot change the
effective fallback or validation policy. PID limits are cgroup isolation, not
a Placement resource inventory; operators must still monitor aggregate host
PID consumption.
For non-BFV instances, the Flavor ``root_gb`` must be at least the positive
``[incus] minimum_root_disk_gb`` limit, which defaults to 1 GiB. The driver
rejects smaller values instead of silently consuming storage that Placement
did not account for. BFV root capacity remains owned and metered by Cinder.
Only driver-generated raw LXC settings are permitted; tenant-supplied raw LXC
configuration remains prohibited.

Keystone projects and Nova ownership are the authoritative tenant boundary;
Neutron projects own the corresponding OVN logical networks and ports. Each
compute host uses a host-local Incus project as a private driver backend, not
as a tenant-facing control plane. Tenants receive neither the Incus socket nor
Incus API credentials. That Incus project is dedicated to one
``nova-compute`` driver: operators and test tools must not create unrelated
containers or VMs in it. The frequent Nova name inventory intentionally uses
the Incus non-recursive list endpoint; ownership and recovery audits use the
expanded ``user.openstack.uuid`` metadata and report duplicate claims as
integrity errors. Nova API policy prevents cross-project instance access,
Neutron isolates overlapping tenant CIDRs. In a migration-enabled deployment,
the driver allocates a fixed UID/GID range for every instance from a persistent
HA etcd registry, stores the result in Nova ``system_metadata``, and writes
``security.idmap.base`` and ``security.idmap.size`` explicitly. Incus still
performs the node-local overlap check. Host-local Incus first-fit allocation is
not globally unique and must never be used as a migration fallback. The
allocator range, slot size, namespace, and subordinate UID/GID ranges are an
immutable migration-domain contract. The compute service account remains
trusted infrastructure and can inspect all host-local instances, just as a
conventional hypervisor can inspect its VMs.

Each allocation also carries a sorted set of persistent Nova compute-node
UUID claims. A source, destination, or evacuation target adds its claim before
creating any profile, VIF, storage attachment, or container. Claims never
expire by time. Cleanup removes a claim only after that node positively proves
that its Incus record, profile, instance directory, Cinder journals, and Manila
mount journals are absent. Final Nova deletion first writes an exact-generation
release intent into the same HA etcd namespace. Creating that intent and adding
a new host claim are mutually exclusive compare-and-swap operations. The slot
is reusable only after Nova is finally deleted and the claim set is empty.
Thus an offline former source or a returning evacuated host causes a bounded,
visible capacity leak instead of host UID/GID reuse. Retiring such a claim
requires explicit STONITH evidence; a hostname or elapsed timeout is never
sufficient authority.

A compute may later receive the same allocation generation after its previous
host materialization has an acknowledged ``cleaned`` proof. The allocator
replaces that old host claim with the new materialization token in one etcd
compare-and-swap while keeping the host index present. Concurrent periodic
retirement either removes the old token first, after which the new claim is
added, or loses its exact-token CAS to the replacement; it can never remove the
new claim through an empty-index ABA window. Periodic reconciliation performs
Incus inventory and storage-proof I/O outside the cross-process instance lock,
then takes a short lock only to re-read Nova ownership, revalidate the exact
allocation/host/materialization token, and retire it.

Materialization state is carried by the per-host claim, not by any
generation-wide flag: a claim moves ``unmaterialized`` to ``possible`` to
``committed`` to ``cleaned``. Immediately before the first Incus create
request, Nova holds the same instance UUID lock used by final deletion,
revalidates that the exact local claim is still ``unmaterialized``, marks it
``possible`` through etcd CAS, and keeps the lock until Incus accepts or
rejects the create request. ``possible`` is deliberately ambiguous: it states
that the rootfs may or may not exist, which is what makes an interrupted
create recoverable, either by promoting the claim once the server proves the
materialization committed or by settling it through the materialization
abort. The spawn, cold-migration target, evacuation target, and
live-migration receive paths all use this transaction. Every start or restart
requires the exact local host claim to exist in a materialized state, and
both release-intent and release-proof keys to be absent.

Deletion racing an unfinished build must not demand evidence that only a
successful build produces. A claim still ``unmaterialized`` while Incus holds
no materialization attempt record proves the create request was never issued:
registration precedes the ``possible`` transition, and a registered attempt is
only deleted after its cleanup proof made the claim ``cleaned``. That exact
state pair is the single sanctioned proof-free disposal — the allocator
abandons the never-registered claim in one compare-and-swap. A claim beyond
``unmaterialized`` without a server attempt record is registry corruption and
stays fail-closed. Likewise, a delete that finds neither an allocation nor a
local host claim treats stale Nova metadata as a cache, not a resource, and
proceeds; a bare allocation without a local claim is left to the terminal
failed-build reconciler, which fences it by proving absence.

Final deletion of a materialized generation is a distributed evidence chain.
Nova first writes the immutable release intent, then sends the Incus instance
DELETE with the allocation UUID as release token and the Nova instance UUID as
owner. Incus deletes or normalizes the root storage and returns a complete,
digest-bound storage receipt. Nova persists the complete receipt as immutable
etcd proof before acknowledging the node-local receipt; only then may it
retire the host claim and release the slot. Lost DELETE and ACK responses are
replayed from those two durable records. A generation that never crossed the
materialization barrier may be released without a storage receipt, but only
after the complete local and all-project Incus inventory is proven absent.
Uncertain state always retains the claim and slot.

Host claims use Nova's node-persistent ``$state_path/compute_id`` UUID rather
than ``CONF.host``. Reinstalling a compute creates a new identity and cannot
silently inherit an old host claim. The production fleet gate requires this
file on every compute and rejects duplicate UUIDs.

The allocator client enforces HTTPS, mutual TLS, and etcd username/password
authentication by default. The etcd role grants read/write access only to the
configured namespace prefix; the HTTP gateway cannot derive this authorization
from the client certificate common name. Transport authentication and prefix
authorization are both part of the isolation boundary, not optional
deployment hardening. ``idmap_allocator_allow_insecure=true`` exists only for an
isolated development testbed and fails the production preflight. If Nova
already has an allocation generation in ``system_metadata`` but either etcd
record is absent, the driver does not create a replacement generation. It
freezes spawn and migration until the registry is explicitly recovered from a
complete Nova and Incus inventory. This prevents a stale etcd restore from
silently authorizing reuse of a live container's host UID/GID range.
The allocator audits the complete bidirectional registry before and after a
slot-changing transaction, and every ownership transaction compares the
immutable namespace configuration. A partial instance/slot pair therefore
blocks admission instead of allowing a second instance to reuse its range.
Normal operations use exact keys. A low-frequency full audit detects unrelated
registry corruption and permanently latches that ``nova-compute`` process
fail-closed; repair requires an operator audit followed by a compute restart.
The gateway listener keeps ``client-cert-auth=false`` while setting a dedicated
``trusted-ca-file``. etcd 3.4 through 3.6 still require and verify the client
certificate in this combination; false disables only the unsupported gateway
CN-to-user mapping. Token authentication supplies the prefix-restricted etcd
identity. An invalid token triggers one synchronized reauthentication and one
request retry; permission and transport failures never trigger that replay.
The all-project inventory proof uses a separate unscoped local SDK client.
Reusing the Nova-project client would send both ``project=nova`` and
``all-projects=true`` and Incus correctly rejects that ambiguous request.
Admission also verifies the allocator credentials with the running compute
process's effective UID, GID, and supplementary groups.

``[incus] allow_instance_swap`` defaults to false, so tenant memory cannot
spill into host swap unless the compute operator explicitly changes that
policy. Nova ephemeral disks are rejected in the first milestone: the legacy
implementation used host paths and therefore bypassed the rootfs storage
quota. They can be enabled only after they are implemented as separately
quota-controlled Incus storage volumes.

Console output is read through the Incus instance log API and Nova returns at
most the final 100 KiB. The driver does not configure a second console logfile
under ``/var/log`` or change ownership and permissions on Incus host paths.
The generated config drive is the only driver-created host directory exposed
to an instance, and it is attached read-only. A tenant root process must not
be able to write through ``/config-drive`` into Nova's ``instances_path``.
When incusd itself runs in Podman, the daemon container must receive Nova's
configured ``instances_path`` at the identical absolute path and read-only.
For the DevStack default this is:

.. code-block:: ini

   Volume=/opt/stack/data/nova/instances:/opt/stack/data/nova/instances:ro

Without this bind mount, Nova can build the config-drive directory on the
host but Incus correctly rejects the disk device because its source is not
visible inside the daemon container.

``instances_path`` is also the durable transaction store for Cinder
connect/disconnect recovery and Manila staging ownership. A containerized
``nova-compute`` must therefore mount the node's persistent host directory at
the configured absolute path. An ``emptyDir`` is not valid: deleting or
restarting the compute pod would discard the only host-side record needed to
finish an uncertain os-brick disconnect or prove ownership of a staged share.
The Nova and incusd containers must see the same path, but only Nova receives
write access to the complete directory.

When Manila shares are enabled, Nova mounts each export below the dedicated
``incus-shares`` subtree. That subtree must also be passed to incusd at the
same absolute path with recursive mount propagation:

.. code-block:: ini

   Volume=/opt/stack/data/nova/instances/incus-shares:/opt/stack/data/nova/instances/incus-shares:rw,rshared

The more-specific bind overrides the read-only parent only for Manila staging.
``rshared`` is required for live migration: NFS or CephFS mounts created later
by nova-compute must propagate into incusd, and CRIU must be able to resolve
the external mount's propagation master during checkpoint and restore.
``rslave`` is sufficient only when Manila live migration is disabled. The
``incus-shares`` directory and each per-instance directory use mode ``0711``:
mapped container root can traverse the path for CRIU ``open_tree(2)`` but
cannot list staged shares. Every parent above ``incus-shares`` must likewise
grant other execute/search permission (or an equivalent ACL). Do not make the
complete ``instances_path`` writable: that would unnecessarily expose config
drives and other Nova host state to the privileged daemon container.

Manila migration capability is enforced on both sides of every move. The
source provider must advertise ``CUSTOM_INCUS_MANILA_COLD_MIGRATION`` for a
cold migration or resize and ``CUSTOM_INCUS_MANILA_LIVE_MIGRATION`` for a live
migration. For scheduler-mediated moves the Nova API adds the corresponding
trait to ``RequestSpec.root_required``; Placement therefore returns only
destination compute resource providers with the same capability. The legacy
forced live-migration path bypasses scheduler selection, so the API checks the
specified destination provider directly before changing instance task state.
The checks use the exact source and destination compute-node UUIDs. They do
not rely on aggregate homogeneity, and instances without Manila mappings do
not inherit either constraint. The scheduling half of this Nova-core patch
runs in ``nova-api``; every API replica must use the patched Nova build before
either migration capability is advertised. The transaction hooks
``_pre_deny_share``, ``_prepare_live_migration_check_data``, and
``_complete_live_migration_rollback`` run in every ``nova-compute`` and the
service must start through ``nova-incus-compute`` so
``IncusComputeManager`` overrides them. Updating only the API or only the
Incus compute image leaves half of the ordering contract absent.

The fleet preflight enters the mount namespace of every running API and
compute process and imports Nova with that runtime's Python environment. It
checks the API capability gates, all three core hooks, the Incus manager
overrides and its actual entry point. It then checks the provider's advertised
``CUSTOM_INCUS_MANILA_SHARE``, ``CUSTOM_INCUS_MANILA_COLD_MIGRATION``, and
``CUSTOM_INCUS_MANILA_LIVE_MIGRATION`` traits through Placement. Comparing a
source-tree hash or finding a patch file on disk is not runtime evidence.

Share mount and migration staging use one fail-closed transaction per Nova
instance. Before the first mount, the compute manager hydrates every mapping
with Manila's protocol-specific access data and obtains ephemeral CephFS
credentials. A failure in this phase has no host mount side effect. For each
mapping the driver then writes and fsyncs an ownership journal, performs the
host mount, updates the Incus profile, and removes the journal only after the
durable profile update is observed. If a later mapping fails, mappings changed
by that transaction are detached and unmounted in reverse order. An uncertain
failing mapping retains its journal rather than guessing whether a profile
update committed.

Unmount is deliberately exhaustive: every requested mapping is attempted even
when an earlier mapping fails. Nova reports the original single failure, or a
recognizable aggregate after multiple failures, and retains the affected
mapping and instance in ``ERROR``. ``deny_share`` removes the profile device
and normal host mount before it asks Manila to revoke access or deletes the
Nova mapping. Therefore an unmount failure cannot silently revoke the only
credential while a host mount remains active.

Periodic journal recovery is not an orphan reaper. It considers only journals
owned by a UUID migration token, takes the same per-instance external lock as
migration staging, and requires a live non-local Nova instance with no task,
no pending verify-resize, and only terminal migration records involving the
current compute. The driver then rechecks that neither the local Incus
instance nor its profile exists and that the exact journal set is unchanged.
Deleted instances, ordinary attach journals, active mounts with no matching
ownership proof, and ambiguous migration records are retained for operator
inspection.

The destination profile is an independent recovery record. Live and cold
migration write ``user.openstack.migration_destination_prepared`` in the
initial profile create, before VIF wiring, instance receive, or removal of a
Manila staging journal. Its value must equal the cleanup token and the same
profile must bind the Nova instance UUID and fixed idmap base and size. This
closes the crash window in which mounted shares survived but their journal had
already been consumed. A separate periodic pass re-reads the profile, the
exact Nova migration UUID, the Incus migration-attempt fence, and the instance
record under the per-instance recovery lock.

Only a terminal, non-committed attempt whose Nova instance is still owned by
the source authorizes target deletion, VIF and volume rollback, Manila
unmount, and a token-bound cleanup acknowledgement. A committed attempt never
authorizes cleanup: recovery requires both the target instance and Nova
destination ownership, then converges ownership without unmounting storage.
Missing or conflicting ownership, an active Nova task, ``VERIFY_RESIZE``, a
deleted instance, a non-terminal migration, or an uncertain Incus result is
retained for inspection. The prepared marker is removed only by the durable
cleanup acknowledgement or committed-attempt finalization. A failed marker
save therefore remains visible to the next periodic retry.

The manager builds one normalized mount-table index for a complete share
transaction. Lookup is then constant time per mapping, so 32- and 64-share
attach, detach, and migration staging do not rescan the host mount table once
per share.

The modern Nova ``Diagnostics`` object restricts its ``driver`` field to a
closed list that does not contain ``incus``. The driver therefore uses Nova's
existing ``libvirt`` diagnostics enum as the nearest standard system-container
category while retaining ``hypervisor=incus``. This avoids modifying every
Nova RPC consumer and prevents object deserialization failures in an
unmodified conductor. It does not claim that libvirt implements this driver.

Incus diagnostics provide truthful aggregate CPU time, memory, and per-NIC
cumulative counters. Uptime is derived from the
``instance_state_started_at`` extension, which reports the current container
PID 1 start time, and is left unset when that capability is absent.

Per-volume telemetry uses Incus' Prometheus metrics endpoint, which exports
cgroup v2 ``io.stat`` read/write byte and request counters by host block
device. The driver maps a data volume through the profile's validated
``unix-block`` source, and maps a BFV root through the unique
``/dev/rbd/<pool>/volume-<uuid>`` link. ``block_stats`` and
``get_all_volume_usage`` then return Nova's native cumulative Cinder counters.
The generic diagnostics disk list still leaves I/O fields unset because that
API has no disk identifier with which to express the volume association.

Incus configures the LXC console as an in-memory ring buffer and bounded disk
log. With Ubuntu Noble liblxc, its ``auto`` value is 128 KiB for each. The
actual host path is ``/var/log/incus/<instance>/console.log``. Operators must
still bound and monitor the aggregate filesystem, and treat an Incus
``message too long`` response after console flooding as a per-instance console
availability event. The Nova response truncation is not the host storage
limit.

Container process core dumps follow the host-global ``kernel.core_pattern``.
Compute-node provisioning must set it to ``/dev/null`` or disable storage in
``systemd-coredump``. If core retention is explicitly required, its
``ProcessSizeMax`` and ``MaxUse`` must be reduced and its storage placed on a
separate bounded filesystem; tenant-triggerable defaults based on a percentage
of the host root disk are not acceptable.

Guest-initiated power transitions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The PID namespace confines guest ``reboot(2)`` calls. Restart/restart2 exits
namespace PID 1 with SIGHUP as observed by its parent; halt/poweroff uses
SIGINT. When namespace PID 1 exits, the kernel SIGKILLs all remaining
processes in that namespace. Incus therefore restarts the same container for
a guest reboot and leaves it stopped for a guest poweroff without rebooting
the compute host.

The persistent rootfs, instance configuration, MAC and Neutron allocation
survive. A new PID and network namespace is created and distribution startup
scripts reapply namespaced sysctls. The running kernel never changes; packages
such as ``linux-image-*`` only add files to the rootfs. A forced reboot remains
host-confined but is not storage-safe: skipping orderly service shutdown and
``sync(2)`` can lose application data.

After guest poweroff, Incus reports STOPPED immediately while Nova can remain
ACTIVE until ``_sync_power_states`` runs. Nova defaults
``sync_power_state_interval`` to 600 seconds. A future Incus lifecycle-event
consumer should reduce this user-visible delay, but periodic reconciliation
must remain the recovery path for missed events.

``/run``, ``/dev``, and ``/dev/shm`` remain ephemeral runtime mounts and must
not be persisted to Ceph. They consume cgroup-accounted memory and can affect
host swap, so instance memory limits, fixed host swap capacity, OOM monitoring,
and aggregate memory planning are required.

When the operator explicitly enables swap, Nova Flavor ``swap`` maps from MB
to Incus ``limits.memory.swap=<value>MiB``. A zero Flavor value maps to
``limits.memory.swap=false``. Incus applies the size as additional per-instance
swap through cgroup v2 ``memory.swap.max``; it does not expose a swap block
device or permit the tenant to run ``swapon``. A non-zero Flavor is rejected
while the operator gate is disabled rather than silently ignoring its SLA.
An eligible compute reports ``CUSTOM_INCUS_SWAP`` only when the gate is
enabled and the host has non-zero ``SwapTotal``. Every non-zero-swap Flavor
must require ``trait:CUSTOM_INCUS_SWAP=required`` so Placement filters hosts
without backing swap before the runtime policy check.

The compute host must provide real swap. Operators must either reserve at
least the sum of enabled instance swap quotas or define an explicit
overcommit policy. Swap IO is still a shared host path and the memory cgroup
does not provide storage latency isolation, so swap-enabled Flavors should be
an explicit product class backed by local SSD/NVMe rather than the default or
a network block device.

LXCFS virtualizes the memory values visible inside a container when it is
available and correctly passed into the containerized Incus daemon. It is a
reporting convenience, not the enforcement boundary. Operators must validate
``memory.max`` and ``memory.swap.max`` in the instance's host cgroup; a guest
``/proc/meminfo`` that displays host totals does not disable those limits.

Storage and persistence
-----------------------

Layering semantics
~~~~~~~~~~~~~~~~~~

System-container layering is storage-level RBD copy-on-write, not a Docker
overlay filesystem mounted over the running rootfs. In the Incus-managed
root-disk model, an instance is an RBD clone of the read-only Incus image-cache
snapshot. Many instances share the immutable base blocks and consume space
only for their changes. Keeping the root disk persistent does not remove this
creation-speed or space-deduplication benefit. Disabling the Ceph clone path
turns instance creation into a full copy and is not the production default.

In the Cinder BFV model the same layering benefit moves out of Incus. Glance
stores a raw image and protected snapshot in RBD, and Cinder clones that
snapshot into ``volume-<uuid>``. Incus then claims the already-created volume;
its own image cache is not involved. This optimized path requires the matching
Glance RBD and direct-URL preparation described in the usage guide.

For a long-lived pet container, the base image remains its creation source but
is no longer the current runtime truth: tenant package installation and system
configuration continuously change the persistent root disk. Rebuild still has
defined destructive replacement semantics, but should not be treated as a
routine rebase of that drifted rootfs.

Long-lived clones can pin old image snapshots. Flattening trades the shared
base blocks for lifecycle independence and must be executed by the component
that owns the volume. Incus owns and must manage flattening for ordinary root
volumes. Cinder owns BFV volumes and its RBD backend already provides
``rbd_max_clone_depth``, ``rbd_flatten_volume_from_snapshot``, and bounded
concurrent flatten operations. Nova may select an operator-defined policy or
volume type, but the compute driver must not directly flatten a Cinder-owned or
Incus-owned RBD image with Ceph credentials. A Flavor extra spec alone is not
an ownership-safe implementation unless the owning service exposes and applies
the requested policy.

Disk QoS ownership
~~~~~~~~~~~~~~~~~~

For an Incus-managed root disk, the driver translates Flavor
``quota:disk_read_iops_sec``, ``quota:disk_write_iops_sec``,
``quota:disk_read_bytes_sec``, and ``quota:disk_write_bytes_sec`` into the
root disk's Incus ``limits.read`` and ``limits.write`` settings. A Cinder BFV
root instead accepts the same read/write family from its front-end volume QoS
specs. Flavor disk QoS and Cinder QoS cannot both govern one BFV root.

Incus permits only one limit representation per direction, so bytes/s and IOPS
cannot be combined for the same direction. OpenStack ``total_*``, burst
``*_max``, and size-scaled IOPS semantics are rejected because mapping them to
Incus ``limits.max`` would change their meaning.

Cinder data volumes are exposed through ``unix-block``. The Incus fork's
``unix_block_limits`` API extension provides lifecycle-aware ``limits.read``
and ``limits.write`` support. The driver maps Cinder
front-end QoS only when that extension is advertised and otherwise rejects it
before os-brick connects the volume. Driver-generated ``raw.lxc`` cgroup rules
are not used because they would bypass Incus device validation and cleanup.

An Incus-managed Ceph RBD storage pool is the production rootfs backend.
Normal rootfs paths, installed packages, users, service configuration, and
application data must survive instance, Incus, and compute-host restarts.
Instance rootfs size, Nova quotas and Placement allocations, and Ceph capacity
limits prevent tenant rootfs writes from exhausting the compute host system
disk.

Two root-disk ownership models are supported. The ordinary model uses an
Incus-managed storage volume whose size comes from the Flavor ``root_gb``.
The boot-from-volume model uses the fork's ``cephext`` driver to claim an
existing Cinder RBD image by name. Cinder owns that volume's lifecycle and
Nova owns its BDM and attachment state; Incus maps, validates, idmaps, and
mounts the filesystem for the system container without copying it. The driver
never substitutes a host block path with ``raw.lxc`` or a bind mount.

The claimed filesystem must be mountable and contain a top-level ``rootfs/``
directory. A single read-write, non-encrypted, non-multiattach Cinder RBD
volume with ``boot_index=0`` is required. The Nova driver verifies that the
volume UUID, RBD image name, configured ``cephext`` pool, and Cinder Ceph pool
agree before asking Incus to claim it. Cinder/os-brick remains authoritative
for separately attached data volumes.

Every BFV filesystem also carries ``.incus-idmap`` beside ``rootfs/``. This
versioned, fsync'd journal records the physical UID/GID map on the filesystem
and is not visible inside the guest. The BFV publishing tool writes a
``stable`` namespace-owned marker into the raw Glance image before upload.
RBD clones, Cinder snapshots, backups, restores, and deep-copy fallbacks then
carry the marker with the data. On claim, ``cephext`` replays any interrupted
transition and remaps from the recorded map to the instance's exact global
idmap before the workload can start.

A markerless external root with no compatible legacy Incus record is rejected;
it is never guessed to be namespace-owned. Existing BFV images must therefore
be republished, and a retained pre-upgrade volume requires an explicit offline
operator repair after its current ownership has been established. Final Nova
deletion normalizes the retained Cinder filesystem and commits ``stable`` with
an empty map before its global idmap slot can be released. Read-only historical
snapshots can still contain shifted numeric IDs, but are inert while unmounted;
any restored clone used as a root must pass through the same ``cephext`` journal
replay. Host-kernel mounts that bypass this protocol are unsupported.

Data-volume connect and disconnect operations use a host-persistent journal
below ``instances_path``. The journal is written and fsync'd before os-brick
changes host state, and retained until the matching Incus profile update and
host cleanup have reached their commit points. It deliberately removes
passwords, keys, keyrings, secrets, and tokens; neither Incus configuration nor
the recovery journal is a credential store. An interrupted ``connecting``
phase recovers the cleanup handle by repeating the same idempotent connector
request.

That recovery contract is production-supported for the tested Ceph RBD
connector, whose scoped CephX identity remains available from the compute
host's protected keyring. The first production contract therefore rejects
every non-RBD data volume before attach side effects, both during spawn and
online attach. A connector that requires an expiring, single-use, or otherwise
non-reacquirable value from ``connection_info`` cannot guarantee automatic
recovery across a ``nova-compute`` process or pod crash. Such a connector stays
unsupported until credential reacquisition and idempotent reconnect behavior
pass the same crash matrix. Persisting its secret in the Incus profile or
journal is not an acceptable workaround.

Power-on reconciliation treats Nova's BDMs as the desired set and the Incus
profile, host journal, and ``unix-block`` devices as independent observations.
It starts a guest only after those observations converge exactly. Stopped
instances may discard an extra attachment only when durable Nova ownership
metadata proves it; opaque devices and running-instance mismatches are never
mutated automatically.

Independent Incus daemons do not share database metadata and must not register
the same ordinary Ceph OSD pool. ``ceph.osd.force_reuse=true`` only bypasses the
ownership guard; it does not make volume metadata coherent. Distinct
per-compute pools permit image-backed functional migration by copying rootfs
data, but that layout and copy path are not the production target. The fork's
``cephext`` pool instead claims an externally owned Cinder RBD by explicit
image name and participates in the fenced handover protocol.

Production migration must transfer exclusive ownership of one authoritative
root volume without copying its contents. The source must stop the container,
unmount and unmap the volume, and release its lock before the destination can
attach and mount that same volume. Fencing must prevent simultaneous ownership,
and every failure point must have a deterministic rollback or operator recovery
record. The fork implements the required externally owned root-volume claim
and handover mechanisms. New migrations require both daemons to advertise
``migration_shared_ceph_storage_ready_fence``. The source emits the
versioned readiness marker only after its pending attempt is durable and the
root is unmounted; the destination persists deletion protection, waits for
that marker, rechecks the attempt fence, and only then claims the RBD. A mixed
old/new daemon pair is therefore rejected instead of entering the legacy
zero-copy path. BFV spawn/destroy and Nova's staged confirm/revert
paths have been validated through standard OpenStack APIs and move the same RBD
with no rootfs copy. Destination reachability is checked before source
shutdown. The target retries transient finish operations, retains a claimed
root on persistent failure, and hard reboot reconciles its Cinder data volumes
and Neutron/OVN wiring. Destroy disconnects host os-brick mappings before Nova
releases Cinder attachments. The Incus compute manager can automatically
repair a marked post-claim target while preserving Nova's staged
``VERIFY_RESIZE`` confirm/revert contract. Real data-volume attach failure
and container-start failure injection have validated active-target recovery.
Destroy treats a concurrent Incus lifecycle operation as transient. Each
attempt refreshes the instance state before stopping and deleting it, and
profile, VIF, and volume cleanup starts only after the Incus record has been
deleted or is confirmed absent. This preserves the original Incus error and
leaves the complete instance state available for a safe Nova retry when the
operation does not settle within the configured retry window.
The marker records the intended running or stopped state, and a real stopped
instance test proved recovery does not power it on. Reverse-revert
data-volume failure injection also proved automatic repair after Nova restored
ownership to the retained source. A persistent failure to write the durable
marker remains fail-closed and requires the operator to inspect the retained
owner; the driver never guesses that an external root is safe to delete.

When ``[incus] storage_pool`` is a local pool, Nova reports ``DISK_GB`` from
that Incus pool's resource API. A shared ``ceph`` or ``cephext`` pool instead
requires an explicit per-compute
``[incus] shared_storage_pool_capacity_gb`` budget. Reporting the whole shared
cluster independently from every compute would multiply Placement capacity,
so the driver fails closed when that budget is absent. LVM, Ceph, ZFS, and
Btrfs capacity must not be inferred from the filesystem containing
``/var/lib/incus``.
The driver's ``update_provider_tree`` implementation publishes ``VCPU``,
``MEMORY_MB``, and ``DISK_GB`` inventories and preserves externally managed
traits while adding ``CUSTOM_INCUS_SYSTEM_CONTAINER`` to the compute provider.

That guarantee does not cover Incus state, Podman storage, logs, image caches,
backup exports, or swap. Those control-plane paths require separately bounded
filesystems, monitoring, and lifecycle policies.
The production preflight rejects ``/var/lib/incus`` or ``/var/log/incus`` on
the host root filesystem, even when the tenant rootfs itself is on Ceph.
It also requires the outer image by immutable registry digest and verifies its
OCI source revision, because an Incus 7.2 binary without the fork extensions
is not compatible merely because its version number matches.

Images
------

The initial modernization preserves the existing driver's container-image
intent for ``root-tar``, ``squashfs``, and applicable ``raw`` artifacts, then
validates exact Glance metadata and Incus 7.x import behavior. Ordinary VM
``qcow2`` images must not be silently interpreted as container root filesystems.

Delivery milestones
-------------------

1. Modern packaging, Python 3.12 tests, and DevStack integration.
2. Incus SDK connection and basic create/query/start/stop/reboot/delete flows.
3. Placement resource inventory and traits.
4. Current os-vif and Neutron ML2/OVN integration, including multinode traffic.
5. Incus-managed Ceph RBD rootfs persistence, quota, host-disk containment,
   and cross-node migration; Cinder/os-brick data-volume attachment and
   movement are validated independently.
6. Optional restricted-project defense in depth.
7. Transactional resize and Cinder attachment after explicit testing. For a
   Cinder BFV root, the source and destination Incus records transfer ownership
   of the same RBD through the fenced ``cephext`` handover; no rootfs copy is
   made. Both confirm and revert have passed two-node integration tests. Cold
   migration is operator-gated and requires the release fault-injection suite
   on every production compute pair. Snapshot and rebuild are implemented for
   image-backed system containers.
8. Externally owned Cinder BFV root volumes and zero-copy migration are
   implemented. Failure atomicity has been verified across the Incus handover,
   Cinder attachment, Neutron binding, Nova host update, and reverse revert.
   After a BFV target has claimed the RBD, the driver retains the target record,
   profile, and VIF on subsequent failure. A durable profile marker lets the
   Incus manager restore runtime state and intended power state without
   accepting the staged resize.

Until implemented, advanced Nova capabilities must be reported as unsupported
rather than silently emulated or advertised.

CRIU live migration is opt-in, conditional, and best-effort. It is supported
only when the source instance and both independent Incus computes pass the
driver's architecture, kernel, Incus, storage, device, privilege, and CRIU
pre-checks. Passing pre-checks does not guarantee that an arbitrary workload
is checkpointable. Force-complete and post-copy remain unsupported.

An Incus-managed root on a shared ``ceph`` pool uses the same ordered CRIU
cutover principle as BFV without changing ownership layers. The source writes
``volatile.migration.storage_handover=pending``, checkpoints the container,
unmounts the RBD, and only then emits the authenticated readiness marker that
lets a standalone target using the same Ceph FSID, OSD pool, and ``ceph``
driver claim the existing root. The target revalidates the durable migration
attempt after receiving the marker, closing the claim-before-unmount race.
Success transfers
Incus ownership to the target without copying data. A failed target restore
unmounts the claim and removes only the target database record before the
source resumes; it must not delete the RBD. Nova requires the
``migration_live_shared_ceph_storage`` API extension on every live-migration
compute so a mixed-version rollout fails pre-check instead of reaching a
same-name RBD conflict.

Failed-host evacuation is supported only for Cinder RBD boot-from-volume
instances after the deployment passes the external STONITH release gate, and
is disabled by default. External power
fencing must complete before Nova starts evacuation; Nova's service-down test
does not prove that the old host has lost access to the RBD. Local and
image-backed roots reject evacuation because their pet data is unavailable
when the source host is down. Cold migration requires a reachable, healthy
source Incus daemon so the destination can negotiate the transfer. Cinder BFV
roots use shared Ceph without copying rootfs data.

Fence-based claim disposal
---------------------------

A running instance's ID-map claim is ``committed`` with no cleanup proof,
and the only way a claim becomes released is the holding host producing a
storage release receipt. A host that external STONITH powered off can never
produce one, so without a second authority every failed-host evacuation
would deadlock: the destination's rescheduled spawn refuses while any claim
of the allocation generation is unreleased.

External power fencing is that second authority, and the deployment already
requires it to complete before evacuation starts. The operator records it
explicitly::

    openstack-incus-idmap-registry ...         --fence-retire-host-claim <instance-uuid>         --host-id <dead-compute-uuid>         --fence-agent fence_ipmilan --fenced-at 2026-08-07T00:00:00Z         --operator ops@example.com         --fence-evidence "fence_ipmilan --action=off rc=0"

The allocator writes that evidence to a per-host fence ledger and removes
the claim and its host index entry in one compare-and-swap, so the
destination pre-check no longer sees the dead host. The ledger entry keeps
a fence-based disposal permanently distinguishable from a normal cleanup
during audit. The primitive refuses a claim that is already ``cleaned`` --
a host that can produce its own proof must retire through the ordinary
path -- and refuses evidence naming another instance, host or allocation
generation. Never use it for a host that is merely unreachable: the fence
must have actually removed the host's power, because the evidence replaces
the proof that its storage access ended.

Returning-host quarantine
--------------------------

Every Nova-managed Incus instance sets ``boot.autostart=false``. Incus may
therefore start after a host reboot so operators can inspect its local
database, but it cannot resume tenant workloads before Nova reconciles
ownership. Nova is configured with ``resume_guests_state_on_host_boot=true``
and remains the sole component that resumes instances whose authoritative
Nova power state is running.

The nova-compute systemd service also requires an admission token under
``/run/openstack-incus``. Because ``/run`` is recreated at boot, a restarted
or power-cycled compute always returns quarantined. Incus starts, but
nova-compute fails closed until an external controller:

1. disables scheduling to the returning service;
2. proves all local tenant containers are stopped;
3. compares every local UUID with Nova's current host;
4. verifies Neutron bindings and Cinder RBD mappings/watchers;
5. explicitly admits the node.

After admission, Nova's standard evacuated-instance cleanup removes stale
source records, while ``resume_state_on_host_boot`` restores only instances
that Nova still assigns to the returning host. Stopping or restarting the
Podman Incus container is not fencing because host LXC monitors and KRBD
mappings can survive it.
