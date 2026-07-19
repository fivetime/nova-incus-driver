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
without that marker and all Incus virtual machines.

The first milestone retains the existing host-local Incus ``default`` project.
OpenStack tenant identity is not duplicated in Incus. Restricted Incus projects
can be added later as optional defense in depth, subordinate to Nova's identity
and authorization model.

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
For block and copy-on-write storage pools, a zero-disk flavor still receives
the positive ``[incus] minimum_root_disk_gb`` limit, which defaults to 1 GiB.
Only driver-generated raw LXC settings are permitted; tenant-supplied raw LXC
configuration remains prohibited.

Keystone projects and Nova ownership are the authoritative tenant boundary;
Neutron projects own the corresponding OVN logical networks and ports. Each
compute host uses a host-local Incus project as a private driver backend, not
as a tenant-facing control plane. Tenants receive neither the Incus socket nor
Incus API credentials. Nova API policy prevents cross-project instance access,
Neutron isolates overlapping tenant CIDRs, and Incus allocates a distinct host
UID/GID range for every instance through ``security.idmap.isolated``. The
compute service account remains trusted infrastructure and can inspect all
host-local instances, just as a conventional hypervisor can inspect its VMs.

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

The modern Nova ``Diagnostics`` object restricts its ``driver`` field to a
fixed list. This repository carries
``patches/nova/0001-diagnostics-add-incus-driver.patch`` to add the existing
``incus`` hypervisor identifier to the object field, API schema, and API
reference. The DevStack plugin applies that small compatibility patch
explicitly and fails if it no longer applies; the driver never identifies
itself as libvirt. This remains a Nova integration dependency until the value
is accepted upstream.

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
and handover mechanisms. BFV spawn/destroy and Nova's staged confirm/revert
paths have been validated through standard OpenStack APIs and move the same RBD
with no rootfs copy. Destination reachability is checked before source
shutdown. The target retries transient finish operations, retains a claimed
root on persistent failure, and hard reboot reconciles its Cinder data volumes
and Neutron/OVN wiring. Destroy disconnects host os-brick mappings before Nova
releases Cinder attachments. The Incus compute manager can automatically
repair a marked post-claim target while preserving Nova's staged
``VERIFY_RESIZE`` confirm/revert contract. Real data-volume attach failure
and container-start failure injection have validated active-target recovery.
The marker records the intended running or stopped state, and a real stopped
instance test proved recovery does not power it on. Reverse-revert
data-volume failure injection also proved automatic repair after Nova restored
ownership to the retained source. A persistent failure to write the durable
marker remains fail-closed and requires the operator to inspect the retained
owner; the driver never guesses that an external root is safe to delete.

When ``[incus] storage_pool`` is configured, Nova reports ``DISK_GB`` from
that Incus pool's resource API. LVM, Ceph, ZFS, and Btrfs capacity must not be
inferred from the filesystem containing ``/var/lib/incus``.
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

Failed-host evacuation is supported only for Cinder RBD boot-from-volume
instances after the deployment passes the external STONITH release gate, and
is disabled by default. External power
fencing must complete before Nova starts evacuation; Nova's service-down test
does not prove that the old host has lost access to the RBD. Local and
image-backed roots reject evacuation because their pet data is unavailable
when the source host is down. Cold migration requires a reachable, healthy
source Incus daemon so the destination can negotiate the transfer. Cinder BFV
roots use shared Ceph without copying rootfs data.

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
