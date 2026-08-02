=====
Usage
=====

Deploy with the DevStack plugin and the settings in
``devstack/local.conf.sample``.  Once stacking has completed, verify the
Incus compute provider before creating an instance::

    source /opt/stack/devstack/openrc admin admin
    openstack compute service list --service nova-compute
    openstack resource provider list
    openstack resource provider inventory list <provider-uuid>
    openstack resource provider trait list <provider-uuid>

The provider must report ``VCPU``, ``MEMORY_MB`` and ``DISK_GB`` inventory and
the ``CUSTOM_INCUS_SYSTEM_CONTAINER`` trait. Placement initializes its
standard resource classes when ``placement-api`` starts. If a development
stack was interrupted between database migration and service startup, restart
``devstack@placement-api`` before restarting ``devstack@n-cpu``.

Create a system container on a Neutron network::

    source /opt/stack/devstack/openrc demo demo
    openstack server create \
        --flavor c1 \
        --image cirros-0.6.3-x86_64-incus \
        --network private \
        --wait incus-e2e
    openstack server show incus-e2e

The server must become ``ACTIVE`` and have a Neutron fixed IP. On the compute
host, verify that Incus and OVS refer to the same instance and Neutron port::

    incus list
    incus config show <incus-instance> --expanded
    ovs-vsctl --columns=name,external_ids list interface

The Incus instance must be a ``CONTAINER`` with
``security.privileged=false``, ``security.idmap.isolated=true`` and the
configured memory, process and root disk limits. The OVS interface must carry
the Neutron ``iface-id``, the Nova instance ``vm-uuid`` and, with OVN, an
``ovn-installed=true`` external ID. DHCP address assignment and those OVS/OVN
bindings prove the network attachment. ICMP tests additionally depend on the
tenant security group and logical router policy.

Automated lifecycle test
------------------------

Run the API-level lifecycle and network smoke test on an all-in-one DevStack
host after sourcing the demo credentials::

    source /opt/stack/devstack/openrc demo demo
    /opt/stack/openstack-incus/tools/openstack-incus-e2e.sh

The script creates a uniquely named server and validates DHCP, metadata, the
OVN-installed OVS interface, stop/start, hard reboot rootfs persistence,
rebuild rootfs replacement with Neutron port preservation, and complete
deletion of the Nova server, Incus instance, Neutron port, and OVS interface.
Environment variables ``IMAGE``, ``FLAVOR``, ``NETWORK``, ``SERVER`` and
``TIMEOUT`` override its defaults.

Ubuntu Noble system-container image
------------------------------------

The Glance payload must be a unified Incus image containing
``metadata.yaml``, ``templates/`` and an expanded ``rootfs/`` directory. A
``rootfs.squashfs`` file placed inside an outer tar is not a valid unified
payload: Incus treats it as an ordinary file and the instance cannot execute
``/sbin/init``.

On the DevStack host, source admin credentials and publish the current Noble
cloud container image::

    source /opt/stack/devstack/openrc admin admin
    tools/publish-incus-image-to-glance.sh

The script defaults to ``images:ubuntu/noble/cloud`` and the Glance name
``ubuntu-noble-24.04-cloud-incus``. ``SOURCE``, ``IMAGE_NAME``, ``WORK_DIR``
and ``LOCAL_ALIAS`` can override those values.

The driver maps Nova's user-data to ``cloud-init.user-data``, the selected
Nova keypair to the NoCloud ``public-keys`` metadata, and Nova
``network_info`` to ``cloud-init.network-config``.  The generated cloud-init
v2 network configuration assigns Neutron's fixed IPv4/IPv6 addresses,
routes, DNS servers, and MTU to stable Incus interface names.  Each interface
is named ``nic`` followed by the first 12 hexadecimal characters of its
Neutron port UUID.  The same name is stored on the Incus NIC device and in
cloud-init network-config, so removing or adding another port cannot renumber
the remaining interfaces.  The mapping also survives reboot and migration.
It deliberately does not use netplan's MAC matching because that is rendered
as ``PermanentMACAddress=`` and does not match a veth.

Vendor-data performs an idempotent network-manager reload for netplan,
NetworkManager, systemd-networkd, and Debian or Alpine ifupdown images that
generate their network configuration after the manager has started.  A
missing activation backend or a failed activation command is reported to
the guest console and syslog and causes cloud-init to record a failed stage.
Nova does not wait for an in-guest readiness acknowledgement, so operators
must monitor cloud-init and console output when guest network readiness is a
service-level requirement.

Addresses are generated from the fixed IPs allocated by Neutron, including
fixed IPs on DHCP-enabled subnets.  A Neutron port deliberately created with
``--no-fixed-ip`` remains an L2-only interface; the driver does not start a
DHCP client behind Neutron's allocation and port-security model.

A tenant can therefore use the normal Nova API without Incus-specific
options::

    source /opt/stack/devstack/openrc demo demo
    openstack server create \
        --flavor d1 \
        --image ubuntu-noble-24.04-cloud-incus \
        --network private \
        --security-group default \
        --key-name noble-incus-key \
        --user-data devstack/noble-user-data.yaml \
        --wait noble-incus-e2e

The sample cloud-config installs ``openssh-server`` because the upstream
Incus cloud image does not include an SSH daemon. It also installs ``jq`` and
writes persistent verification files. Configure at least one DNS resolver on
the Neutron subnet; an empty ``dns_nameservers`` field leaves systemd-resolved
without an upstream server::

    openstack subnet set --dns-nameserver 8.8.8.8 private-subnet

Snapshot and restore
--------------------

Nova snapshots a running system container through a temporary Incus storage
snapshot. The instance remains running while Incus publishes the snapshot and
the driver uploads the unified image to Glance. Temporary Incus snapshots and
images are removed on both success and failure::

    openstack server image create \
        --name noble-incus-snapshot \
        --wait noble-incus-e2e
    openstack image show noble-incus-snapshot
    openstack server create \
        --flavor d1 \
        --image noble-incus-snapshot \
        --network private \
        --key-name noble-incus-key \
        --wait noble-incus-restored

This is a crash-consistent filesystem snapshot, not an application-consistent
backup. Quiesce databases and other stateful applications in the guest when
their own consistency protocol requires it. The default
``[incus] request_timeout`` is 300 seconds because publishing a large rootfs
can exceed the short timeout suitable for ordinary Incus API requests.

Maintainers can validate the complete snapshot path across two independent
Incus computes with the repository test script. Source the admin credentials
because the script uses the Nova ``--host`` test-only scheduling override::

    source /opt/stack/devstack/openrc admin admin
    SSH_IDENTITY=/path/to/test-key \
      tools/openstack-incus-snapshot-e2e.sh

The script writes a marker into the source rootfs, snapshots it to an active
Glance ``raw/bare`` image, restores it on the second compute, and checks the
marker and Neutron port binding. It also compares the source Incus snapshot
and image inventories before and after the operation to detect leaked
temporary objects. Normal production requests omit ``--host`` and remain
subject to Scheduler and Placement decisions.

Selecting an Incus-managed root pool
-------------------------------------

Operators can expose several Incus pools without allowing a tenant to inject
an arbitrary pool name. Configure ``[incus] root_storage_pools`` as a mapping
of stable selector names to host-local Incus pool names. A Flavor selects a
mapping entry and requires the Placement trait reported for that selector::

    [incus]
    storage_pool = ceph-rootfs
    shared_storage_pool_capacity_gb = 6000
    root_storage_pools = durable:ceph-rootfs,local-nvme:local-zfs
    root_storage_pool_resource_classes = local-nvme:CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME_DISK_GB

    openstack flavor set incus-local-nvme \
      --property incus:root_storage_pool=local-nvme \
      --property trait:CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME=required \
      --property resources:CUSTOM_INCUS_STORAGE_POOL_LOCAL_NVME_DISK_GB=20

When the extra spec is absent, ``storage_pool`` is used. Unknown selectors are
rejected during spawn. Selector characters other than letters, digits and
underscore become underscores in the upper-case trait name; configurations
that would produce the same trait are rejected.

The ``durable`` selector above is an alias for the default ``storage_pool``.
It deliberately has no entry in
``root_storage_pool_resource_classes`` or
``shared_root_storage_pool_capacities_gb``: its capacity is already published
once as the compute provider's standard ``DISK_GB`` inventory. A physical pool
may be reported by exactly one Placement inventory. Startup fails if two
selectors reuse one resource class, two custom inventories resolve to the same
driver/cluster/source identity, or a custom inventory reports the default pool
a second time.

Every selector that resolves to a physical pool other than ``storage_pool``
must have its own entry in ``root_storage_pool_resource_classes``. This keeps
the selected pool's capacity in the same Placement claim as the instance
instead of silently charging only the default ``DISK_GB`` inventory.
``incus:root_storage_pool`` is valid only for Nova-managed roots. Do not put it
on a Flavor used for boot-from-volume: Cinder Volume Type and its RBD pool
select the BFV backend, and ``boot_from_volume_storage_pools`` maps that RBD
pool to the matching Incus ``cephext`` pool.

The storage Trait proves that the destination has the named pool. For a
node-local Btrfs, LVM, or ZFS pool, map its selector to a custom resource class
and set the Flavor's ``resources:<class>`` amount exactly equal to that
Flavor's ``root_gb``. The driver rejects a mismatch, and Placement then
accounts each local pool independently. The standard ``DISK_GB`` request is
still present and acts as an additional host-level ceiling.

Shared Ceph capacity must not be published in full independently by every
compute. Assign each compute a fixed Placement slice with
``shared_storage_pool_capacity_gb`` for the default pool and
``shared_root_storage_pool_capacities_gb`` for selectable pools. The sum of
all slices must not exceed the operator-approved cluster-wide budget after
reserving capacity for Cinder, Glance, recovery and Ceph health. The driver
reports zero local usage for a shared slice because Placement allocations are
the accounting authority. It rejects a shared pool without a slice and rejects
a slice configured for a local pool. Every selectable shared-pool slice must
have a matching custom resource class; unused slice entries are rejected at
``nova-compute`` startup. Local pools cannot provide automatic evacuation after
host loss and require remote snapshots or backups.

Cinder boot-from-volume
-----------------------

The Incus fork can use a Cinder Ceph RBD volume as the container root disk
through its ``cephext`` storage driver. Configure a pool that references the
same Ceph pool and least-privilege CephX user as the Cinder backend::

    INCUS_BFV_POOL_NAME=cinder-bfv
    INCUS_BFV_CEPH_POOL=cinder-volumes-rbd-pool
    INCUS_BFV_CEPH_USER=cinder
    INCUS_BFV_CEPH_CLUSTER_NAME=ceph

DevStack creates the pool and writes the Cinder-to-Incus mapping to ``[incus]
boot_from_volume_storage_pools``. Outside DevStack, create one ``cephext``
pool for every Cinder RBD pool accepted for BFV and configure the mapping as
``cinder-rbd-pool:incus-pool`` pairs separated by commas. The driver selects
the pool from Cinder's authoritative ``connection_info`` rather than from the
Volume Type name.

Create a server from an existing bootable Cinder volume with the standard
OpenStack API::

    openstack server create --flavor m1.small \
      --volume <volume-uuid> --network private <server-name>

The driver requires exactly one ``boot_index=0`` volume. It must be a
read-write, non-encrypted, non-multiattach RBD volume whose image is named
``volume-<volume-uuid>``. The ``cephext`` pool source must equal Cinder's RBD
pool. Flavor ``root_gb`` is not applied to a BFV root disk.

The volume filesystem must be directly mountable and contain a top-level
``rootfs/`` directory. Incus validates that layout while claiming the volume.
Custom Glance properties may document the image contract, but Nova's typed
``ImageMetaProps`` does not reliably carry arbitrary properties into the
compute driver and a BFV BDM does not carry ``volume_image_metadata``. They
therefore are not a security gate; the server-side claim validation is.

Do not upload the unified ``.tar.gz`` used by the Incus-managed root model as
a Glance ``raw`` BFV image. Compression is a transport format, not a disk
format, and an RBD clone preserves those gzip bytes verbatim. Convert the
unified tar into a real ext4 image and upload it with the dedicated tool::

    source /opt/stack/devstack/openrc admin admin
    sudo --preserve-env=OS_* \
      UNIFIED_TAR=/path/to/alpine-unified.tar.gz \
      IMAGE_NAME=alpine-3.21-cloud-incus-criu-bfv-raw \
      tools/publish-incus-bfv-image-to-glance.sh

The tool checks the top-level ``rootfs/sbin/init``, filesystem headroom and
ext4 consistency before publishing the image. The unified tar remains the
correct Glance payload for the Incus-managed root model; the ext4 raw image is
the correct payload for Cinder BFV.

Glance-to-Cinder RBD clone preparation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Fast BFV provisioning requires more than placing Cinder on Ceph. Glance must
use an RBD store in the same Ceph cluster, expose image locations with
``show_image_direct_url=True``, and store the image as ``raw``. The Cinder RBD
driver accepts different Glance and Cinder pools when their FSID matches, but
the Cinder CephX client must have read access to the Glance pool. Keep Glance
and Cinder write permissions scoped to their own pools.

The expected image location is an
``rbd://<fsid>/<pool>/<image>/<snapshot>`` URL. Cinder verifies the FSID and
opens the protected Glance snapshot before cloning it into the Cinder pool. If
Glance uses the file store, hides the direct URL, publishes a non-raw image, or
Cinder cannot read the Glance pool, Cinder falls back to downloading and
writing the image data. That fallback is functionally correct but turns BFV
volume creation from a metadata clone into a full data copy.

The test-environment readiness check must verify all of the following::

    grep -E '^(enabled_backends|default_backend|rbd_store_pool|show_image_direct_url)' \
      /etc/glance/glance-api.conf
    openstack image show <image-id> -f json
    rbd --id cinder -p <cinder-pool> info volume-<volume-uuid>

Also inspect the Cinder volume log for the RBD ``clone_image`` path and verify
the new image's RBD parent/snapshot relationship. Provisioning time by itself
is not proof that the optimized path was used. The release gate performs all
of those checks and removes its test volume afterward::

    source /opt/stack/devstack/openrc admin admin
    tools/openstack-incus-bfv-cow-e2e.sh

Server deletion first removes the Incus instance and releases the RBD mapping,
then Nova/Cinder detach the volume. Incus must never delete an externally owned
RBD image. A Cinder ``delete_on_termination`` policy, when requested, is
handled only after the Incus watcher is gone.

Cinder data volumes
-------------------

For non-BFV instances the root filesystem remains an Incus-managed storage
volume. In production, configure that pool with the Ceph RBD driver. Do not
bypass either supported root model with ``raw.lxc`` or host bind mounts.

Cinder volumes are attached as block devices inside the container. The driver
uses the connector properties reported by os-brick, connects the volume on the
compute host, and adds the resulting path as an Incus ``unix-block`` device.
The production contract currently accepts only Ceph RBD connections with a
stable ``pool/image`` name. Other os-brick protocols are rejected before any
Incus, profile, journal, or connector side effect; accepting a volume that
cannot be proven and reconnected after a compute restart would create an
instance that can boot once but cannot safely start again. For example::

    openstack server add volume --device /dev/vdb <server> <volume>

The tenant operating system owns partitioning, formatting, mounting, and
``/etc/fstab`` configuration for the attached device. Detaching removes the
Incus device before asking os-brick to disconnect the host path. A failed
Incus attach rolls back the os-brick connection.

When one or more data volumes are present in the initial server BDM, the
Glance image must advertise ``hw_incus_data_volume_fuse=true``. This is a
fail-closed declaration that the guest contains the configured userspace
filesystem helper; the production default requires an executable
``fuse2fs``. Nova preserves the property as
``image_hw_incus_data_volume_fuse`` in instance system metadata. The gate is
not required for a server created without initial data volumes. Online attach
also verifies the helper inside a running guest before os-brick changes host
state.

Before creating an Incus profile or invoking os-brick, spawn validates the
complete initial data-volume set: unique Cinder volume IDs and guest paths,
read-write access, RBD protocol and stable image identity, supported QoS, and
non-multiattach ownership. Nova queries Cinder's authoritative encryption
metadata for every initial root and data ``DriverVolumeBlockDevice``; it does
not trust an optional flag in the BDM. Any deterministic rejection aborts the
build without rescheduling it to another identical compute host.

Release acceptance exercises both root models with zero, one, and three
initial data volumes. Use distinct admitted Glance images for the unified local
root and raw ext4 BFV formats::

    RUN_DESTRUCTIVE=true \
      LOCAL_IMAGE=ubuntu-noble-incus \
      BFV_IMAGE=ubuntu-noble-incus-bfv \
      FLAVOR=incus.small NETWORK=private VOLUME_TYPE=ceph \
      tools/openstack-incus-initial-data-volume-matrix.sh

Data-volume Cinder front-end QoS requires the Incus fork's
``unix_block_limits`` API extension. Without it the driver rejects QoS before
connecting the volume, because ordinary Incus 7.x cannot enforce limits on a
``unix-block`` device. Root-disk QoS uses Flavor ``quota:disk_read_*`` and
``quota:disk_write_*`` extra specs for an Incus-managed root, or Cinder
front-end read/write QoS for a BFV root. Do not combine both authorities on a
BFV root. Total, burst, and same-direction bytes-plus-IOPS combinations are
unsupported and rejected on every disk type.

The production acceptance test must inspect all three enforcement layers: the
Cinder attachment's front-end ``qos_specs``, the instance profile's
``limits.read``/``limits.write`` values, and the instance cgroup's ``io.max``
entry for the mapped block-device major and minor numbers. Test direct I/O in
both directions, then detach through OpenStack and verify that the profile
device, cgroup entry, and host block mapping are all removed.

The requested container device is restricted to Nova-style data-disk names
under ``/dev``: ``vd*``, ``sd*``, or ``xvd*``. Special nodes such as
``/dev/null``, nested paths, and ordinary host files are rejected. Before
calling os-brick, the driver also rejects a volume ID that is already present
in the instance profile or a target path occupied by another volume. This
prevents an attachment retry or corrupted block-device mapping from replacing
an existing container device.

The resolved os-brick source has a separate validation rule: it must be a
direct device-node path under ``/dev``, but may use connector-specific names
such as ``/dev/dm-0`` or ``/dev/rbd0``. Invalid source paths are disconnected
from the compute host as part of the failed attach rollback.

Encrypted Cinder volumes and multiattach are currently rejected. They must
not be advertised until os-brick encryptor lifecycle and concurrent device
ownership have dedicated end-to-end coverage.

Every ordinary power-on, including host-boot resume, reconciles the complete
desired Cinder BDM set before starting the container. A stopped instance may
remove an extra mapping only when Nova's profile metadata or fsync'd host
journal proves ownership. An opaque ``unix-block`` device, missing BDM payload,
or any mismatch on a running instance fails closed without cleanup. Successful
reconciliation requires the profile devices, recovery metadata, journal set,
and stable RBD mappings to match Nova exactly.

Cold migration
--------------

Cold migration is disabled by default as an operator safety gate. It is a
supported capability after the BFV migration release matrix passes on every
enabled compute pair. Image-backed roots use Incus pull data transfer and are
intended only for development. Cinder BFV roots use the fork's shared-Ceph
handover to claim the same authoritative RBD without copying it.
Both source and destination must advertise
``migration_shared_ceph_storage_ready_fence``. This fail-closed protocol
allows the target to claim only after the source has durably recorded the
attempt and unmounted the root; mixed versions cannot silently negotiate the
older handover.
Both paths retain the stopped source record until Nova confirms the operation.
On confirm, the source record and profile are deleted; an explicit Nova revert
performs the reverse BFV handover and restores Cinder and Neutron ownership
before restarting the source.

For BFV, destination TCP reachability is checked before the source instance is
read or stopped. That preflight raises Nova's ``InstanceFaultRollback`` on
failure, preserving the original ACTIVE instance and its storage/network
ownership. Real firewall fault injection has verified this behavior. Failures
after the destination may have claimed the RBD remain deliberately
non-destructive. The driver retries target VIF, data-volume and start actions,
retains a claimed target on persistent failure, and a standard hard reboot
reconciles its Cinder data-volume mappings and Neutron/OVN wiring before
starting it. Instance deletion disconnects all os-brick data-volume mappings
while the Incus profile still contains the persisted connector metadata;
Cinder attachments are released only after that succeeds. Persistent
post-claim failures can be recovered by the optional
``IncusComputeManager``. It consumes only an explicit driver-owned recovery
marker, rebuilds the os-brick and OVN runtime state, and deliberately leaves
the server in ``VERIFY_RESIZE``. The operator or tenant must still confirm or
revert the staged migration through Nova. Active data-volume attach failure,
active container-start failure, stopped-instance data-volume failure, and
reverse-revert data-volume failure have passed real fault injection and
cleanup. The marker preserves ``running`` versus ``stopped`` so recovery does
not power on a SHUTOFF instance. Production enablement requires this suite to
pass on every compute pair and the fail-closed recovery procedure in
:doc:`production_readiness` to be accepted by operations.

Migration destinations also carry
``user.openstack.migration_destination_prepared`` from the initial profile
create. This marker is independent of host-side Manila journals, so restarting
``nova-compute`` after a journal was consumed does not turn a mounted share or
partially wired target into an untracked orphan. Do not clear this marker by
hand. Automatic recovery validates its migration UUID, instance UUID, fixed
idmap and Incus attempt state. A committed attempt is never cleanup authority;
an ownership conflict is deliberately retained rather than guessed.

Fail-closed BFV recovery
------------------------

If ``finish_migration`` reports that a claimed target was retained but the
durable marker could not be written, do not delete either Incus record, clear
``volatile.migration.storage_handover``, detach the root volume, or run
``rbd rm``. Disable scheduling to both involved computes while ownership is
audited::

    openstack compute service set --disable <source-host> nova-compute
    openstack compute service set --disable <target-host> nova-compute
    openstack server show <server-uuid> \
      -c status -c OS-EXT-SRV-ATTR:host -c OS-EXT-SRV-ATTR:instance_name
    openstack volume attachment list --volume-id <root-volume-uuid>
    openstack port list --server <server-uuid> \
      -c ID -c Status -c binding_host_id

On both computes, inspect the records and RBD watchers without modifying
them::

    podman exec incus incus info <instance-name>
    podman exec incus incus config show <instance-name> --expanded
    rbd status cinder-volumes-rbd-pool/volume-<root-volume-uuid> --id cinder

Proceed only when Nova host, Cinder attachment, Neutron binding, the target
record's ``user.openstack.uuid``, and the sole RBD watcher identify the same
target compute. Resolve the Incus database/API failure, then write the desired
state to that target's per-instance profile::

    # Use "stopped" when the pre-migration server was SHUTOFF.
    podman exec incus incus profile set <instance-name> \
      user.openstack.recovery_required=running

The custom manager repairs os-brick mappings and OVN/OVS wiring, restores the
recorded power state, removes the marker, and leaves the request in
``VERIFY_RESIZE``. Verify all four ownership sources again, then explicitly
confirm or revert through Nova. Re-enable scheduling only after the opposite
compute has no active watcher and the release E2E cleanup checks pass. If the
evidence disagrees or there is more than one watcher, stop: that is a fencing
incident, not a condition to repair by guessing.

Nova 2026.1 selects the compute manager from an internal service mapping and
does not honor the historical ``compute_manager`` configuration option.
Computes must therefore start with the project's wrapper entry point::

    python -m nova.virt.incus.cmd.compute --config-file /etc/nova/nova-cpu.conf

The packaged ``nova-incus-compute`` console script is equivalent. The DevStack
plugin installs a systemd drop-in automatically. Using the stock
``nova-compute`` command disables automatic recovery even when
``migration_auto_recovery=true``.

Production compute preflight
----------------------------

Run the fail-closed host audit before enabling a compute service and after
every host, Incus image, Nova driver, Ceph credential, or storage change::

    sudo EXPECTED_INCUS_IMAGE_DIGEST=sha256:<approved-manifest-digest> \
      EXPECTED_INCUS_REVISION=<approved-full-git-commit> \
      tools/openstack-incus-production-preflight.sh

The audit requires Ubuntu Noble, Python 3.12, cgroup v2 CPU/I/O/memory/PID
controllers, AppArmor, disabled host core dumps, the custom compute-manager
launcher, an immutable Quadlet image reference, all fork API extensions, the
``cephext`` BFV pool, restricted migration certificates/project, exact Unix
socket ownership, private-key mode ``0600``, Ceph credentials, active time
synchronization, and independently bounded ``/var/lib/incus`` and
``/var/log/incus`` filesystems. A missing expected digest or source revision is
a failure; a mutable ``:alpine-novm`` tag is never sufficient evidence.

The migration private key can instead be ``root:<nova-compute-group> 0640``.
In that layout the audit also requires the compute service's Incus group to
contain exactly the configured service user, so the group-readable key does
not become a shared operator credential.

After every compute passes the host audit, run the cross-compute audit from a
controller with OpenStack administrator credentials::

      COMPUTE_NODES='compute-1=root@192.0.2.10,compute-2=root@192.0.2.11' \
      CONTROLLER_SSH=root@192.0.2.20 \
      SSH_IDENTITY=/root/.ssh/compute-audit \
      EXPECTED_INCUS_IMAGE_DIGEST=sha256:<approved-manifest-digest> \
      EXPECTED_INCUS_REVISION=<approved-full-git-commit> \
      tools/openstack-incus-fleet-preflight.sh

The fleet audit fails on driver drift, duplicate migration addresses, disabled
or down Nova computes, missing Placement inventory or the system-container
trait, dead OVN controllers, or unavailable Cinder Ceph backend and scheduler.
It streams its adjacent ``openstack-incus-production-preflight.sh`` to every
node and executes that exact content through ``bash -s``. Remote checkouts are
therefore not trusted and cannot silently run an older audit policy. Run it
before enabling a new compute, after upgrades, and as a scheduled compliance
check. Any failure is a release or admission blocker; do not maintain an
allow-list of ignored checks.

When ``CONTROLLER_SSH`` is set, fleet-wide OpenStack queries run through that
host after sourcing ``CONTROLLER_OPENRC``. Otherwise the orchestrator must
provide an authenticated local ``openstack`` command.

Release distributions must be built with the version explicitly pinned because
the modernized repository retains historical ``nova-incus`` Git tags::

    PBR_VERSION=2026.1.0 python -m build
    twine check dist/*

The release workflow performs this build and verifies the installed
``nova-incus-compute`` entry point. Do not allow PBR to infer a production
version from repository history.

Bind ``core.https_address`` to the explicit migration-management address, not
``:8443``, ``0.0.0.0``, or ``[::]``. Every trusted migration client must be
restricted to the ``nova-preflight`` project. That project has zero container
and VM limits and cannot be used as a tenant API.

Production nodes should use dedicated partitions or LVs with monitoring and
alerts for Incus state and logs. The development topology uses preallocated
8 GiB and 2 GiB loop-backed ext4 files to prove hard capacity isolation where
its only dedicated disk is already owned by LINSTOR. That is a bounded test
substitute, not a production storage recommendation.

The legacy image-backed Incus pull path copies rootfs data and does not satisfy
the production persistence requirement. Cinder BFV is the production model:
the Incus fork supplies externally owned root-disk claim/handover and the Nova
driver orchestrates forward confirm and reverse revert with the same RBD. These
zero-copy paths and the core post-claim recovery matrix have passed multi-node
integration tests. Keep cold migration operator-gated until the same release
suite passes on every production compute pair.

Run the complete release matrix from a trusted external orchestrator. It
executes normal confirm/revert, migration preflight rejection, data-volume and
container-start post-claim recovery, stopped-instance recovery, and reverse
revert recovery. After every case it requires the Incus instance, profile, and
RBD mapping inventories on every compute to return to their pre-test snapshots.
It also requires the OpenStack server and volume inventories to match their
pre-test snapshots and finishes with the fleet preflight::

    IMAGE=ubuntu-noble-incus-bfv-rbd \
      COMPUTE_NODES='incus-node-01=root@192.0.2.10,incus-node-02=root@192.0.2.11' \
      CONTROLLER_SSH=root@192.0.2.10 \
      SSH_IDENTITY=/root/.ssh/compute-audit \
      EXPECTED_INCUS_IMAGE_DIGEST=sha256:<approved-manifest-digest> \
      EXPECTED_INCUS_REVISION=<approved-full-git-commit> \
      tools/openstack-incus-bfv-migration-matrix.sh

Run the matrix for every ordered production compute pair. A matrix failure,
resource inventory difference, residual-state finding, or fleet-preflight
failure blocks release admission.

Revert and durable-marker recovery recreate retained host VIF wiring while the
container is stopped. This is required for OVN to reassert
``Port_Binding.up`` after the Neutron binding returns to the original chassis;
an idempotent plug of the pre-existing OVS interface is not sufficient.

Each compute must expose its Incus HTTPS API only on a protected migration
network and set both options::

    [incus]
    allow_cold_migration = true
    idmap_allocator_endpoint = https://etcd.example:2379
    idmap_allocator_namespace = region-one-cell1
    idmap_allocator_base = 500000000
    idmap_allocator_size = 65536
    idmap_allocator_count = 10000
    idmap_allocator_audit_interval = 60
    idmap_allocator_allow_insecure = false
    idmap_allocator_ca_cert = /etc/nova/idmap-etcd/ca.crt
    idmap_allocator_client_cert = /etc/nova/idmap-etcd/client.crt
    idmap_allocator_client_key = /etc/nova/idmap-etcd/client.key
    idmap_allocator_username = nova-incus
    idmap_allocator_password_file = /etc/nova/idmap-etcd/password
    migration_address = https://192.0.2.10:8443
    migration_preflight_tls_cert = /etc/nova/incus-preflight/client.crt
    migration_preflight_tls_key = /etc/nova/incus-preflight/client.key
    migration_preflight_tls_ca = /etc/nova/incus-preflight/default-server.crt
    migration_preflight_project = nova-preflight
    migration_preflight_server_names = 192.0.2.10:compute-1,192.0.2.11:compute-2
    migration_preflight_tls_ca_by_server = 192.0.2.10:/etc/nova/incus-preflight/compute-1.crt,192.0.2.11:/etc/nova/incus-preflight/compute-2.crt

The allocator directory must be searchable and every configured TLS or password
file must be readable by the effective ``User=`` and ``Group=`` of the
``nova-compute`` systemd service. A deployment that runs ``User=stack`` with
``Group=incus-admin`` can use ``root:incus-admin`` ownership, mode ``0750`` on
the directory, ``0644`` on public certificates, and ``0640`` on the private key
and password. Testing with ``sudo -u stack`` alone is insufficient because it
can retain supplementary groups that the service process does not have; the
production preflight checks the credentials from the running process.

``migration_address`` must be an HTTPS origin without a path. Firewall the
listener so it is reachable only from Nova compute nodes. The ordinary driver
connection remains the local Unix socket; tenants must never receive Incus API
or migration credentials.

Every compute in one migration domain must use the same idmap allocator
configuration. Mutual TLS authenticates the transport, while an etcd user and
role must restrict the identity to the exact
``/openstack-incus/idmaps/v3/<namespace>/`` prefix. Client-certificate common
name authentication is not supported by the etcd HTTP gateway used by
``etcd3gw`` and is not a substitute for this RBAC policy. The
registry stores permanent instance-to-slot and slot-to-instance records with a
single compare-and-swap transaction; UUID hashing selects only the first probe
slot and is not the uniqueness mechanism. Each allocation indexes exact host
claims. A claim binds the allocation UUID (``A``), persistent Nova
``compute_id`` (``H``), per-materialization UUID (``T``), and Nova instance
UUID (``U``), and has one of ``unmaterialized``, ``possible``, or ``cleaned``
states. An allocator outage rejects new
spawn, migration, unshelve, and evacuation without falling back to Incus
first-fit allocation. Existing running workloads continue and stop remains
available, but starting or rebooting a stopped generation fails closed because
Nova must prove that the exact local claim remains authoritative. Immediately
before each Incus create request, the driver creates an ``unmaterialized``
claim and, immediately before the POST, changes that exact ``T`` to
``possible`` through etcd CAS. Spawn, migration receive, evacuation, start,
and reboot therefore cannot race slot release or reuse an earlier create
attempt's proof.

Final Nova deletion first writes an exact-generation release intent to HA
etcd. That intent atomically blocks new claims and transitions to
``possible``. A claim cannot be retired merely because a local lookup is
empty. Incus must provide either a digest-bound materialization-attempt proof
(``not-materialized`` or ``reconciled-clean``), or a complete
``storage_release_receipt_v2`` for the same ``A/H/T/U`` and ID map.
Materialization proofs must include ``baseline_clean=true`` in their canonical
digest. This proves Incus checked that the exact root-storage binding was clean
before registering ``T``; a missing or false value is rejected rather than
treated as an older compatible proof. Receipt outcomes are ``deleted``,
``normalized``, or ``detached``; retained-storage
outcomes require immutable storage identity and prove only that the local
claim was released, not that the external volume was deleted. Nova first
persists the exact proof in etcd and only then acknowledges it to Incus. Lost
POST, proof-persist, receipt ACK, and claim-retirement responses are replayed
idempotently. Every compute can replay the shared
intent, which survives loss of the deleting node's local disk. A claim owned
by an offline or decommissioned node deliberately prevents reuse until the
node returns or an operator supplies external fencing proof. Uncertain or
corrupt records remain reserved for reconciliation rather than reuse.

``state_path`` must still be node-persistent because Nova stores its immutable
``compute_id`` there and Cinder/Manila cleanup journals live below the instance
directory. ``emptyDir``, tmpfs, and a container-writable overlay are invalid.
HTTPS with a trusted CA, a client certificate/key, and an etcd username plus
password file is enforced by the driver. Keep the password file readable only
by the ``nova-compute`` service account. Inline passwords and endpoint userinfo
are rejected. ``idmap_allocator_allow_insecure=true`` permits HTTP without
RBAC only for an isolated DevStack testbed and is rejected by the production
preflight.

The supported etcd 3.4, 3.5, and 3.6 gateway listener intentionally combines
these settings::

    listen-client-urls: https://127.0.0.1:2379
    cert-file: /etc/etcd/pki/server.crt
    key-file: /etc/etcd/pki/server.key
    trusted-ca-file: /etc/etcd/pki/nova-client-ca.crt
    client-cert-auth: false
    enable-grpc-gateway: true
    auth-token: jwt,pub-key=/etc/etcd/pki/auth.pub,priv-key=/etc/etcd/pki/auth.key,sign-method=RS256,ttl=15m

This is not a plaintext or one-way TLS configuration. In all three supported
etcd series, a non-empty ``trusted-ca-file`` makes the TLS listener require and
verify a client certificate even when ``client-cert-auth`` is false. The false
value only disables deriving an etcd user from the certificate common name,
which the JSON gRPC gateway cannot forward correctly. The subsequent etcd
username/password exchange supplies the authorization identity and returns a
token whose role is restricted to the allocator prefix. Use a client CA
dedicated to Nova computes and enable etcd RBAC. Do not set
``client-cert-auth=true`` on this gateway listener.

The idmap registry should use a dedicated etcd cluster. A second listener or a
reverse proxy is neither required nor a substitute for the two checks above;
``client-cert-auth`` is member-wide rather than per-listener, and a proxy
cannot restore the gateway's lost client-CN identity. Other applications on
the same etcd cluster must also use token authentication instead of CN
authentication. If that cannot be changed, isolate the idmap registry in a
separate cluster.

Prefer JWT tokens in production and distribute the same protected signing
material to every etcd member. Simple tokens are member-local and are intended
for development. The allocator reauthenticates exactly once when etcd reports
``invalid auth token`` (for example after member restart, token expiry, or an
auth-policy change), rereading the protected password file so credential
rotation does not require restarting ``nova-compute``. Permission failures and
transport errors are not treated as token expiry and remain fail closed.

Treat etcd restore as an allocation freeze, not as an online rollback. Disable
all computes for new ownership-changing work, inventory Nova system metadata
and every Incus project, rebuild both sides of every allocation record, and
only then re-enable scheduling. A Nova generation whose registry record is
missing fails closed and is never recreated implicitly. Restoring an older
snapshot and immediately admitting new instances can otherwise reuse a slot
owned by a container created after that snapshot.

Create a registry only for a new, proven-empty migration domain while all
Nova creates, deletes, migrations, evacuations, and returning-host workflows
are frozen::

    tools/openstack-incus-idmap-registry.py \
      --endpoint https://etcd.example:2379 \
      --namespace region-one-cell1 \
      --base 500000000 --size 65536 --count 10000 \
      --ca-cert /etc/nova/idmap-etcd/ca.crt \
      --client-cert /etc/nova/idmap-etcd/client.crt \
      --client-key /etc/nova/idmap-etcd/client.key \
      --username nova-incus \
      --password-file /etc/nova/idmap-etcd/password \
      --bootstrap-empty --confirm-frozen

The command refuses a namespace containing any orphan key and verifies that
no concurrent writer appeared while it created the immutable configuration.
Normal driver initialization never performs this bootstrap. For DevStack,
``INCUS_IDMAP_ALLOCATOR_BOOTSTRAP_EMPTY=True`` invokes the same operation and
must be enabled for one initial frozen ``stack.sh`` run only, then disabled.
A missing configuration afterwards is treated as possible registry loss and
keeps ownership-changing operations unavailable.

Take a canonical registry audit after each rollout and retain it with the etcd
backup set::

    tools/openstack-incus-idmap-registry.py \
      --endpoint https://etcd.example:2379 \
      --namespace region-one-cell1 \
      --base 500000000 --size 65536 --count 10000 \
      --ca-cert /etc/nova/idmap-etcd/ca.crt \
      --client-cert /etc/nova/idmap-etcd/client.crt \
      --client-key /etc/nova/idmap-etcd/client.key \
      --username nova-incus \
      --password-file /etc/nova/idmap-etcd/password \
      > idmap-registry.json

The audit is one read-only prefix query and rejects an unexpected, duplicate,
or one-sided instance/slot/host-index record. The export schema is explicitly
``openstack-incus-idmap-registry/v3`` and contains every claim's exact ``T``,
state, and cleanup proof. A v2 document is rejected; there is no automatic
upgrade. During a documented allocation freeze, an operator can restore the
exact allocation generations, host claims, proofs, and release intents without
overwriting any existing key::

    tools/openstack-incus-idmap-registry.py <same connection options> \
      --restore-file idmap-registry.json --confirm-frozen

Restore is idempotent and atomically writes each allocation pair together with
its exact host indexes and optional release intent. It rejects a partial pair,
a different owner, configuration drift, or a registry allocation absent from
the frozen input document. The confirmation flag does not freeze Nova by
itself; the operator must first disable scheduling and prove there are no
creates, deletes, migrations, evacuations, or returning hosts in progress.

Manual retirement is limited to a claim that is already ``cleaned`` and whose
exact Incus proof is present in a frozen v3 document. Fencing alone is not
cleanup proof. Freeze the fleet, take a fresh canonical audit, and then retire
only the exact proven claim recorded in that audit::

    tools/openstack-incus-idmap-registry.py <same connection options> \
      --restore-file idmap-registry.json --confirm-frozen \
      --retire-host-claim <instance-uuid> --host-id <compute-uuid>

The command rejects ``unmaterialized`` and ``possible`` claims. A permanently
lost compute without a durable exact proof continues to reserve its range
until the host and storage state can be reconciled; an operator assertion or
TTL is never accepted as a substitute.

Enabling the allocator on a fleet with existing instances requires an offline
inventory/import operation. Distinct UUIDs that already reuse a range on
different computes cannot be imported as globally unique. Those legacy
instances must be stopped and rebased during maintenance before migration is
enabled. New and old allocation modes must not run concurrently in one
migration domain. Rebase is a data-changing operation: Incus recursively
unshifts and shifts a non-idmapped rootfs on its next start. Take a
storage-native rollback point first. BFV roots require explicit Cinder
snapshot/attachment coordination and must not be batch-rebased as ordinary
Incus-owned Ceph roots.

For OpenStack-Helm, combine the selected storage override with
``values_overrides/nova/incus-migration.yaml``. The DaemonSet derives each
``migration_address`` from ``status.hostIP``, writes a node-local Nova config,
mounts the migration Secret, and idempotently registers two restricted client
identities in the local Incus daemon. The Secret must contain
``migration.crt``, ``migration.key``, ``preflight.crt``, ``preflight.key`` and
``ca.crt``. Every Incus HTTPS server certificate must be issued by that CA and
contain its Kubernetes node InternalIP as an IP subjectAltName. The Helm chart
does not issue or rotate the server certificate; that remains part of the
Incus host PKI and Podman deployment lifecycle.

BFV destination readiness uses a separate TLS client identity. Trust each
compute's client certificate on every destination, restricted to a dedicated
``nova-preflight`` Incus project. The project must prohibit instances and
project-owned storage while retaining profiles, which Incus requires for a
restricted project::

    incus project create nova-preflight
    incus project set nova-preflight \
        limits.containers=0 limits.virtual-machines=0 \
        features.images=false features.networks=false \
        features.storage.volumes=false features.storage.buckets=false \
        user.openstack.preflight_protocol=1 \
        'user.openstack.bfv_storage_pools={"cinder-volumes-rbd-pool":"cinder-bfv"}'
    incus project set nova-preflight restricted=true
    incus config trust add-certificate peer-client.crt \
        --name nova-preflight-compute-2 --restricted \
        --projects nova-preflight

The Cinder RBD pool value is authoritative from ``rbd_pool`` in Cinder's
active backend configuration; do not infer it from an Incus pool name. The
readiness contract, target API extensions, and target ``cephext`` driver are
checked before the source container is read or stopped.

For self-signed Incus server certificates, pin the exact certificate per
destination with ``migration_preflight_tls_ca_by_server``. A concatenated file
of unrelated self-signed certificates is not sufficient because pylxd pins the
presented Incus certificate fingerprint. When certificates contain DNS SANs
but Nova supplies migration IP addresses, use
``migration_preflight_server_names`` and ensure those names resolve to the
intended addresses. Protect client private keys as nova-compute credentials,
rotate them per node, and remove the old trust fingerprint after rotation.

The ``dir`` backend or distinct per-compute Ceph pools can be used for
disposable functional validation, where Incus copies the rootfs over the
migration network. This copied-root layout is not promoted by successful BFV
tests and must not be used as the production persistence model.

Read-only Cinder attachments are also rejected. Incus 7.x ``unix-block``
devices do not provide a read-only option and explicitly expose block devices
for reading and writing. Device-node permissions are not a security boundary
against root inside a system container, so the driver does not claim that
``mode=0440`` enforces Cinder ``access_mode=ro``. Only ``rw`` or an omitted
access mode is accepted.

The driver persists the JSON-serializable os-brick ``device_info`` in the
instance-specific Incus profile under a ``user.openstack.volume.*`` key. This
retains connector cleanup data such as the mapped path and temporary Ceph
configuration across nova-compute restarts. Tenants must not receive Incus API
or profile access. Legacy profiles without this metadata fall back to their
stored block-device source path.

Online Cinder extension is advertised to Nova. The driver asks the protocol
connector to refresh the host mapping and rejects a missing or smaller size
reported by os-brick. Because an Incus ``unix-block`` device exposes that host
block device directly, the updated capacity is visible without a QEMU resize
layer. The tenant still owns filesystem growth, for example ``resize2fs`` or
``xfs_growfs`` after the block device has expanded.

The multipath connector options are reserved for a future, separately
certified non-RBD data-volume contract. Keep ``volume_use_multipath`` and
``volume_enforce_multipath`` disabled for the currently supported Ceph RBD
path.

After a Cinder backend is configured, maintainers can validate attach,
filesystem persistence, cross-compute cold migration, and detach cleanup::

    source /opt/stack/devstack/openrc admin admin
    SSH_IDENTITY=/path/to/test-key \
      tools/openstack-incus-volume-migration-e2e.sh

A Cinder v3 endpoint and an ``up`` scheduler are control-plane prerequisites,
not proof that block storage exists. With ``c-vol`` disabled, a volume request
is expected to enter ``error`` because no backend can claim it. Do not enable
DevStack's loop-backed LVM backend on the controller or compute system disk to
make this test pass. Production-like validation requires a dedicated backend
and an ``up`` ``cinder-volume`` service before running the script above.

For an existing Ceph cluster, enable ``c-vol`` and configure a dedicated RBD
pool through DevStack::

    INCUS_CINDER_CEPH_POOL=cinder-volumes
    INCUS_CINDER_CEPH_USER=cinder
    INCUS_CINDER_CEPH_CONF=/etc/ceph/ceph.conf
    INCUS_CINDER_CEPH_CLUSTER_NAME=ceph

Provision ``ceph.conf`` and
``/etc/ceph/ceph.client.cinder.keyring`` on the Cinder volume host before
stacking, and provision the client configuration and credentials required by
os-brick on every compute. The plugin consumes an existing cluster; it does not
create pools, CephX users, monitors, or OSDs. The Cinder pool must not be the
Incus rootfs pool. Configure either the LINSTOR variables or the Ceph variables,
never both.

The keyring must be readable by the Cinder service account without being
world-readable. A packaged deployment normally uses ``root:cinder 0640``; this
DevStack environment uses ``root:stack 0640``. A root-only ``0600`` keyring can
make the root ``rbd`` CLI succeed while the Cinder Python RADOS client fails to
initialize.

Validate the backend through the public API with an explicit volume type; an
``up`` service record alone is not sufficient::

    openstack volume type create ceph
    openstack volume type set --property volume_backend_name=ceph ceph
    openstack volume create --size 1 --type ceph ceph-smoke

After attachment, the Incus driver maps the Cinder RBD through os-brick and
adds a ``unix-block`` device to the instance profile. Unprivileged containers
must use a userspace filesystem implementation. The production default is
``[incus] data_volume_mount_fuse=ext4=fuse2fs``: the driver enables Incus
mount syscall interception in every instance profile and rejects attachment to
a running guest that does not provide ``fuse2fs``. Install the Ubuntu/Debian
``fuse2fs`` package (or the distribution equivalent) in every supported base
image, and publish it with ``hw_incus_data_volume_fuse=true``. The supplied
Glance publishing tools add the property only after finding an executable
``fuse2fs`` in the rootfs. Do not replace this with
``security.syscalls.intercept.mount.allowed=ext4`` for untrusted tenants;
that passes tenant-controlled filesystem data into the host kernel and can
expose the compute node to filesystem-parser vulnerabilities. Cinder
online extension refreshes the KRBD and container block-device size; the tenant
must then unmount as appropriate and grow its filesystem. Backend validation
must finish by detaching and confirming that both the host RBD mapping and the
Cinder RBD image are removed after volume deletion.

Cinder RBD snapshots and volumes created from snapshots preserve the filesystem
contents and use Ceph copy-on-write parent/clone relationships. Validate the
clone by attaching it through Nova and reading tenant data from inside the
unprivileged container, then delete the clone before deleting its snapshot and
source volume. Snapshot and clone are not backups: they remain dependent on the
same Ceph failure domain.

For Cinder backup, enable ``c-bak`` and use a second, dedicated RBD pool and a
pool-scoped CephX client::

    enable_service c-bak
    INCUS_CINDER_BACKUP_CEPH_POOL=cinder-backups
    INCUS_CINDER_BACKUP_CEPH_USER=cinder-backup
    INCUS_CINDER_BACKUP_CEPH_CONF=/etc/ceph/ceph.conf

The backup client needs ``profile rbd`` only for the backup pool. The source
volume is opened with the Cinder volume backend credentials, so do not grant
the backup client access to the Cinder volume or Incus rootfs pools. Provision
``/etc/ceph/ceph.client.cinder-backup.keyring`` as ``root:cinder 0640`` for a
packaged deployment or ``root:stack 0640`` for DevStack. The plugin consumes
the pre-created pool and client; it does not create Ceph resources.

Validate full backup, incremental backup, restore into a new Ceph volume, and
cleanup through the public APIs::

    source /opt/stack/devstack/openrc admin admin
    SSH_IDENTITY=/path/to/test-key \
      tools/openstack-incus-ceph-backup-e2e.sh

The preceding test covers a Cinder data volume. Validate the same native
Cinder backup path for a boot-from-volume root separately. The BFV test stops
the source container for filesystem consistency, creates full and incremental
backups, restores the incremental chain into a new bootable volume, boots that
volume on another compute, and reads the tenant marker from the restored
rootfs::

    source /opt/stack/devstack/openrc admin admin
    SSH_IDENTITY=/path/to/test-key \
      tools/openstack-incus-bfv-backup-e2e.sh

Backup ownership follows storage ownership:

* Cinder BFV roots and Cinder data volumes use ``cinder-backup``. They support
  full and incremental backup and restore into a new Cinder volume.
* Incus-managed roots, including local ZFS/LVM and Incus-owned Ceph roots, are
  not Cinder volumes. Use Nova snapshot-to-Glance and validate restore with
  ``tools/openstack-incus-snapshot-e2e.sh``. This is a crash-consistent image
  unless the tenant application is quiesced first.
* Manila shares use the snapshot, replication, or backup feature exposed by
  their Manila backend. A Nova snapshot does not include share contents.
  For a backend advertising ``snapshot_support`` and
  ``create_share_from_snapshot_support``, validate tenant data restoration
  with ``tools/openstack-incus-manila-snapshot-e2e.sh``. A Manila snapshot in
  the same backend is still not an independent disaster-recovery copy.

  Capability extra specs are applied when a share is created and are not
  retroactive. Configure the Share Type before creating protected shares::

      openstack share type set incus-nfs --extra-specs \
        snapshot_support=True \
        create_share_from_snapshot_support=True \
        revert_to_snapshot_support=True \
        mount_snapshot_support=True

OpenStack does not provide one atomic operation spanning a Nova root, multiple
Cinder volumes, and multiple Manila shares. An application-consistent service
backup must quiesce the application, record all resource UUIDs in a manifest,
create the owner-specific backups while writes remain frozen, and then thaw
the application. Restore must use that manifest rather than assuming that
independently created snapshots represent the same point in time.

This test was completed against the production-like Ceph backend on
2026-07-16. The Ceph driver used native RBD differential export/import, the
restored system container read the incremental marker, and cleanup left no
backup images, test volume images, or KRBD mappings. A backup pool in the same
Ceph cluster protects against volume deletion and logical corruption, but is
not a separate failure domain. Production disaster recovery still requires a
remote cluster, replication, or an independently protected backup target.

Initialize each pre-created RBD pool from the Ceph administrative plane before
giving the restricted clients access::

    ceph osd pool application enable incus-rootfs rbd
    ceph osd pool application enable cinder-volumes rbd
    ceph osd pool application enable cinder-backups rbd

Do not grant compute-node clients permission to change pool application
metadata. Without this one-time initialization, ``rbd pool init`` fails for a
least-privilege client and Incus storage-pool creation cannot complete.

Before changing either service, run the read-only Ceph preflight from the
controller::

    ROOTFS_POOL=incus-rootfs-node01 \
      DEST_ROOTFS_POOL=incus-rootfs-node02 \
      ROOTFS_USER=incus-node01 DEST_ROOTFS_USER=incus-node02 \
      CINDER_POOL=cinder-volumes \
      SSH_IDENTITY=/path/to/test-key \
      tools/openstack-incus-ceph-preflight.sh

It verifies that the rootfs and Cinder pools differ and that both compute
nodes have the required configuration, keyrings, host ``rbd`` executable, and
pool access. The host dependency is intentional: Incus uses the Ceph client
inside its Podman container for ``cephext`` rootfs operations, while Nova's
os-brick runs on the compute host and needs the host ``ceph-common`` package
for Cinder data-volume map/unmap. Installing it only in the Incus image is not
sufficient. It does not
create or delete any Ceph resource. After Incus and Cinder have been
configured, repeat it with ``CHECK_CONFIGURED_BACKENDS=True`` to also verify
the selected Incus storage driver/source and Cinder RBD backend settings.

Independent Incus daemons must not register the same Ceph OSD pool. Incus 7.2
names an instance root volume deterministically, so a migration into another
daemon using the same RBD namespace fails with ``Volume already exists on
storage but not in database``. Use a distinct rootfs pool and least-privilege
CephX client for each independent compute. This is a verified compatibility
layout, but a pool per compute is not the preferred large-scale design. Incus
does not currently expose an RBD namespace setting; adding namespace support
to its Ceph driver is required before consolidating many independent computes
into one OSD pool safely.

After the backend preflight passes, validate a temporary Ceph-backed system
container::

    SSH_IDENTITY=/path/to/test-key \
      INCUS_POOL_NAME=incus-ceph \
      tools/openstack-incus-ceph-rootfs-e2e.sh

When running the script directly on the source compute/controller, avoid
installing an SSH private key on that host and use ``SOURCE_SSH=local`` instead.

The test requires a finite-root-disk flavor. It verifies that tenant root is
root only inside an unprivileged container, the Incus root quota matches the
flavor, `/var/tmp` data survives a hard reboot, an allocation larger than the
quota fails, and the compute system filesystem does not grow materially while
the test file exists. The host-growth measurement begins after instance/image
creation so an initial image cache fill is not misclassified as rootfs data.

The migration path disconnects every non-root Cinder volume from the source
host before connecting it on the destination. A source disconnect failure
restores the Incus device definition and previously disconnected volumes
before restarting the source container. Volume swap similarly connects and
validates the replacement device before changing the Incus profile, and
disconnects the replacement if that profile update fails.
Do not enable ``allow_resize_to_same_host``.

Run the two-way confirm/revert regression from a host with administrative
OpenStack credentials and SSH access to both computes::

    SSH_IDENTITY=/path/to/test-key \
      SOURCE_HOST=incus-node-01 DEST_HOST=incus-node-02 \
      SOURCE_SSH=root@192.0.2.10 DEST_SSH=root@192.0.2.11 \
      SOURCE_MIGRATION_ADDRESS=https://192.0.2.10:8443 \
      DEST_MIGRATION_ADDRESS=https://192.0.2.11:8443 \
      tools/openstack-incus-migration-e2e.sh

The script verifies bidirectional migration API reachability, rootfs marker
preservation, fixed-IP retention, confirm source deletion, revert source
recovery, and final Incus, Neutron port, and OVS interface cleanup.

Live migration is documented in the CRIU section below. It is disabled by
default and remains workload-dependent even after all pre-checks pass.

BFV evacuation is supported only for the shared-Ceph BFV workflow with an
external STONITH or power fencing system that proves the failed source cannot
access Ceph. It remains disabled by default so a deployment cannot accidentally
omit that prerequisite::

    [incus]
    allow_bfv_evacuate = true

Nova's service-down check is only a soft prerequisite and is not fencing.
Evacuation accepts exactly one ``boot_index=0`` Cinder RBD root, validates the
destination ``cephext`` pool and Incus handover extensions, then delegates
Placement, Cinder attachment, Neutron rebinding, and spawn to Nova's default
rebuild workflow. Local/image-backed roots are rejected to preserve pet data.
Shared Ceph does not make ``instance_on_disk`` true on a destination whose
host-local Incus database has no instance record.

Computes must install ``openstack-incus-compute-admission`` and the supplied
``nova-incus-admission.conf`` systemd drop-in. Every Nova-created container
must have ``boot.autostart=false``. A reboot removes the admission token, so
Incus starts for inspection while nova-compute and all tenant workloads remain
stopped. Do not recreate the token from a boot script.

Before admitting a returning host, disable its Nova service and run the
fail-closed ownership audit from a trusted orchestrator::

    RETURNING_HOST=incus-node-02 \
      RETURNING_SSH=root@192.0.2.12 \
      CONTROLLER_SSH=root@192.0.2.10 \
      SSH_IDENTITY=/path/to/audit-key \
      tools/openstack-incus-returning-host-audit.sh

The audit rejects a running local container, an enabled compute service,
misbound Neutron port, stale local RBD mapping, non-BFV stale root, or watcher
count inconsistent with the authoritative target's ACTIVE or SHUTOFF state.
It also requires exactly one attached Cinder root record for the Nova server.
Only after it passes may an operator admit and start the compute::

    ssh root@192.0.2.12 \
      'openstack-incus-compute-admission admit \
         --reason ownership-reconciled &&
       systemctl reset-failed devstack@n-cpu &&
       systemctl start devstack@n-cpu'

Production fencing providers must implement ``off``, ``status``, and ``on``
commands and report ``off`` only after the source cannot access Ceph. Run the
destructive release gate with a disposable BFV server. The orchestrator and
every Keystone, Nova, Neutron, Cinder, Placement, and Glance endpoint used by
the test must remain available after the source is powered off. The gate
resolves the endpoint inventory before fencing and rejects an endpoint hosted
on the source::

    sudo apt-get install \
      fence-agents-ipmilan fence-agents-redfish fence-agents-virsh
    sudo install -o root -g root -m 0755 \
      tools/openstack-incus-fence-agent-provider \
      /usr/local/sbin/openstack-incus-fence-agent-provider
    sudo install -d -o root -g root -m 0700 \
      /etc/openstack-incus/fence.d \
      /etc/openstack-incus/fence-secrets
    sudo install -o root -g root -m 0600 node-02.json \
      /etc/openstack-incus/fence.d/node-02.json
    sudo install -o root -g root -m 0600 node-02.password \
      /etc/openstack-incus/fence-secrets/node-02

The supplied ``openstack-incus-fence-agent-provider`` accepts ``ipmilan``,
``redfish``, and ``virsh`` JSON configurations. Start from the examples in
``etc/openstack-incus/fence.d``. Configuration and password files must be
root-owned regular files with mode ``0600``; the password is sent through the
standard fence-agent stdin protocol and never placed in argv. Redfish TLS
verification is enabled unless ``ssl_insecure`` is explicitly set. The
provider follows the ClusterLabs status convention: return code 0 means on and
return code 2 means off, and it rejects output that contradicts that code.
Validate every target without changing its power state::

    FENCE_IDS=incus-node-01,incus-node-02,incus-node-03 \
      tools/openstack-incus-fence-preflight.sh

Then run the destructive release gate with a disposable BFV server::

    SERVER_ID=<uuid> \
      SOURCE_HOST=incus-node-02 DEST_HOST=incus-node-03 \
      SOURCE_SSH=root@192.0.2.12 DEST_SSH=root@192.0.2.13 \
      CONTROLLER_SSH=root@192.0.2.10 \
      SSH_IDENTITY=/path/to/audit-key \
      FENCE_PROVIDER=/usr/local/sbin/openstack-incus-fence-agent-provider \
      tools/openstack-incus-bfv-evacuation-e2e.sh

The gate writes and hashes a rootfs marker, fences the source, requires zero
watchers before evacuation, validates the target attachment/network/single
watcher, powers the source back on, proves quarantine, audits mixed local and
stale ownership, admits the source, and waits for stale-record cleanup.
It reads Nova's ``service_down_time`` before fencing. Its timeout defaults to
that value plus 120 seconds, and an explicitly shorter timeout is rejected
before any power action.
Successful ``status`` calls alone do not prove fencing. Production release
still requires this real ``off``/``on`` test through the same BMC or PDU path
that automation will use. Do not use ``fence_virsh``, SSH shutdown, stopping
Podman, or disabling a switch port as evidence unless that mechanism is an
independent, fail-closed power control plane and has been threat-modelled as
the site's actual STONITH implementation. ``fence_virsh`` is valid for a
KVM-based test compute only when it runs from outside that guest against an
independent libvirt host. Its root-owned ``0600`` SSH identity and exact domain
name are mandatory. Physical production computes must use BMC or PDU fencing.

Monitoring probe
----------------

Install ``tools/openstack-incus-monitoring-audit.sh`` on an independently
hosted monitoring node and run it with the same immutable identity inputs as
the fleet preflight::

    COMPUTE_NODES='node-01=root@10.0.0.11,node-02=root@10.0.0.12' \
    SSH_IDENTITY=/etc/openstack-incus/monitor_ed25519 \
    EXPECTED_INCUS_IMAGE_DIGEST='sha256:...' \
    EXPECTED_INCUS_REVISION='...' \
    CONTROLLER_SSH=root@10.0.0.10 \
    FENCE_EVIDENCE_FILE=/var/lib/openstack-incus/last-fence.log \
      tools/openstack-incus-monitoring-audit.sh

The command exits non-zero for an unaudited active compute, fleet drift,
control-filesystem or log pressure, a pending storage handover, a durable
recovery marker, or inconsistent BFV ownership. For every BFV runtime it
correlates the Nova host and state, Cinder attachment, Incus state, Ceph
watcher, fleet-wide KRBD mapping, Neutron binding, and fleet-wide OVS owner.
The labels include the instance and root-volume UUIDs so the notification is
actionable. Every running instance is also checked for PID, memory, swap, and
OOM cgroup signals and for ``/``, ``/run``, and ``/dev/shm`` pressure. Set
``INSTANCE_PRESSURE_WARNING_PERCENT`` to the desired warning threshold; its
default is 90. Configure the monitoring system to alert on both a non-zero
result and missing probe data. ``FENCE_EVIDENCE_FILE`` must be root-owned,
not writable by group or other, contain the successful terminal record
emitted by the evacuation E2E, and be newer than
``FENCE_EVIDENCE_MAX_AGE_SECONDS`` (30 days by default). The probe logs its
SHA-256. Alert delivery remains site-specific and must be tested separately.

Rescue and unrescue are also disabled. The legacy implementation depended on
binding a directory from the compute host into a rescue container, which is
not valid for an Incus-managed Ceph or LVM root volume and violates the host
filesystem isolation boundary. Rescue must remain unavailable until it can
attach the retained root volume through a storage-pool-native Incus API.
The Incus compute manager rejects rescue, unrescue, suspend, and resume before
performing resource or power operations. It restores the task state, preserves
the prior VM state, and records an explicit OpenStack failure exception in the
server action event. These Nova APIs are asynchronous, so the initial request
may still be accepted; operators and clients must inspect the action event.
Immediate ``4xx`` feedback in Horizon requires an upstream Nova API capability
gate.
CRIU live migration is an opt-in, best-effort capability. It is disabled by
default and is accepted only for a running, unprivileged system container
created with ``migration.stateful=true``. Enabling the driver option causes
new instance profiles to use Incus's shifted on-disk rootfs layout, because
CRIU restore cannot recreate a detached idmapped root mount. Existing
instances must be stopped and converted before they can pass pre-checks.

Config drives, privileged containers, block migration, and extra disk devices
that are not Nova-managed Cinder data volumes remain rejected. Shared-Ceph
boot-from-volume roots use the Incus ordered handover protocol. Nova-managed
Cinder data volumes use Nova's native temporary destination attachment and a
destination-local os-brick mapping.

Incus-managed roots on a shared ``ceph`` pool also use zero-copy ordered
handover. Both computes must advertise
``migration_live_shared_ceph_storage`` and
``migration_shared_ceph_storage_ready_fence``, and expose the same Ceph FSID,
OSD pool, and ``ceph`` driver. Local ``dir``, LVM, Btrfs, and ZFS roots instead transfer
their rootfs data; they cannot use shared-storage handover.
The source profile's host-specific ``unix-block source`` paths are excluded
from migration data and rebuilt from the destination connection information.
Successful migration disconnects the source mapping after CRIU restore.
Destination preparation failure atomically removes its mappings, Incus
profile, VIFs, and firewall state before Nova restores the source attachment.
Encrypted, read-only, and multiattach volumes remain unsupported.

Active Manila NFS and CephFS mappings are mounted on the destination before
the Incus transfer. On success the source staging mount is removed; rollback
removes the destination staging mount. NFS access must cover every eligible
compute on an isolated storage network. Configure the same CIDR on all
computes; a per-host default prevents cross-node migration::

    [DEFAULT]
    my_shared_fs_storage_ip = 10.224.0.0/24

Every compute that enables Manila must provide GNU coreutils ``timeout`` at
``/usr/bin/timeout``. BusyBox ``timeout`` is rejected during ``init_host``
because it does not provide the termination contract used around privileged
mount helpers. Configure independent bounds for mount and unmount::

    [incus]
    enable_manila_shares = true
    share_mount_timeout = 30
    share_unmount_timeout = 30

On expiry GNU ``timeout`` sends ``TERM`` and, after a five-second grace period,
``KILL``. The driver never uses lazy or forced unmount. A timed-out unmount
retains the fsync'd owner journal and leaves the Nova mapping in ``ERROR`` for
an explicit retry. The compute image must install GNU coreutils even when the
base distribution already provides a BusyBox applet with the same command
name.

The attach and migration paths fetch access rules for every mapping before the
first host mount. CephFS credentials remain memory-only apart from the
short-lived mode ``0600`` secret file consumed by the kernel mount helper; the
journal never contains a Ceph secret. A later failure rolls back only changes
made by the current transaction and does so in reverse order. Detach attempts
all mappings before reporting failures, and Manila access is revoked only
after the Incus device and host mount have been removed.

With the DevStack plugin set ``INCUS_MANILA_ACCESS_CIDR`` to that CIDR.
Firewall the share network so tenants cannot originate traffic from this
trusted range. A single-node deployment can use a host ``/32``.
CRIU treats the host-staged share as an external mount master. The approved
outer image therefore contains ``/etc/criu/default.conf`` with exactly
``enable-external-masters``. The image entrypoint and production preflight
fail when this immutable setting is absent. Do not add arbitrary CRIU options
through a writable host bind.

Mismatched architecture, kernel, or Incus versions are rejected. CRIU support
remains workload-dependent. Containers with
complex systemd services, external sockets, unsupported kernel resources, or
processes created through a host-side ``incus exec`` session may fail their
checkpoint. Failure is recoverable, but successful migration is never
guaranteed merely because pre-checks passed.

CRIU pre-copy is an optimization, not a correctness requirement. The approved
Incus fork terminates pre-copy cleanly and falls back to a full final
checkpoint when a CRIU pre-dump fails. It also retries an incremental final
checkpoint once without a parent image when the source is still running.
These fallbacks increase the stop interval for that migration but prevent a
transient pre-copy failure from corrupting the migration protocol or making an
immediate retry fail.

Configure a dedicated TLS client certificate trusted by every Incus server
and restricted to the ``nova`` project. Do not reuse the read-only
``nova-preflight`` identity::

    [incus]
    allow_live_migration = true
    migration_address = https://192.0.2.10:8443
    migration_tls_cert = /etc/nova/incus-migration/client.crt
    migration_tls_key = /etc/nova/incus-migration/client.key
    migration_tls_ca = /etc/nova/incus-migration/default-server.crt
    migration_tls_ca_by_server = 192.0.2.10:/etc/nova/incus-migration/compute-1.crt,192.0.2.11:/etc/nova/incus-migration/compute-2.crt

Both the source and destination outer novm images must contain the same
approved CRIU build plus ``iptables-restore``, ``ip6tables-restore``,
``iptables-legacy-restore`` and ``ip6tables-legacy-restore``.
They must also contain GNU tar. CRIU invokes ``tar --no-unquote`` while
checkpointing non-empty tmpfs mounts such as ``/dev/shm``; BusyBox tar causes
the source checkpoint to fail before the target restore begins. The image
build and production preflight test this exact option.
``criu check --extra`` must pass on every compute. The driver uses staged
destination creation. A successful destination create operation is Incus's
authoritative signal that CRIU restore and migration control completed. Nova
then enters its normal post-migration flow, force-stops the source record only
if Incus still reports it running, and deletes that record. A destination
failure is removed before Nova recovery, and a stopped source is restarted
during rollback.

CRIU is packaged inside the outer ``alpine-novm`` image and is executed by
``incusd`` there. It must not be installed on the compute host. The Podman
container still uses the host kernel and namespaces, so matching kernel
versions and CRIU-compatible host features remain mandatory.

CRIU checkpoints contain the source user-namespace IDs. The destination must
therefore reserve the exact source ``volatile.idmap.base`` through the
temporary migration profile. Migration pre-check fails if the base is missing,
invalid, or already used on the destination. The Nova project must have
``features.profiles=true`` so each temporary profile is owned and cleaned up
inside that project rather than the Incus default project.

The outer Podman deployment must bind ``/run/incus`` from a host runtime
directory so Incus does not lose LXC runtime configuration when the outer
container is recreated. The Incus state bind must use recursive shared mount
propagation so CRIU can resolve the external master of
``/dev/.incus-mounts``::

    Volume=/var/lib/incus:/var/lib/incus:rshared
    Volume=/run/incus-podman:/run/incus:rshared

Both daemons must advertise the
``migration_stateful_shifted_root`` API extension. This prevents an
unmodified Incus server from being mistaken for a server that can restore a
shifted stateful root.

Independent Incus daemons must also pass the project-qualified LXC name to the
CRIU restore helper. Otherwise CRIU can restore and resume every process while
the Incus API looks up a different monitor name and incorrectly reports the
instance as stopped. The approved fork must include Incus commits
``826c25cd9`` (normalize mixed CRIU image ownership) and ``20c12bce3``
(project-qualified restore monitor name), plus revision ``80ba579c2`` or later
for CRIU pre-copy/full-final fallback, or equivalent upstream fixes.

Run the Nova API and Neutron/OVN regression in both directions::

    SSH_IDENTITY=/path/to/test-key \
      SOURCE_HOST=incus-node-02 DEST_HOST=incus-node-03 \
      SOURCE_SSH=root@192.0.2.11 DEST_SSH=root@192.0.2.12 \
      tools/openstack-incus-live-migration-e2e.sh

The test must preserve the exact guest PID, observe a continuously increasing
counter, move the Nova host and Neutron binding, find the OVN-installed OVS
interface only on the destination, and leave no instance, profile, port, OVS
interface, or Placement allocation after deletion. Set ``KEEP_FAILED=1`` to
retain a failed instance and its CRIU logs for diagnosis; the default is to
clean all test resources.

Run the complete root/data/share matrix after the single-path diagnostic
passes::

    SSH_IDENTITY=/path/to/test-key \
      NODE01_SSH=root@192.0.2.10 \
      NODE02_SSH=root@192.0.2.11 \
      NODE03_SSH=root@192.0.2.12 \
      MANILA_SHARE=incus-e2e-share \
      tools/openstack-incus-live-migration-matrix.sh

The matrix covers local and Cinder BFV roots, with and without a Cinder data
volume, and with and without a Manila share. Every case follows
``node01 -> node02 -> node03 -> node01`` and requires the Nova server and
Cinder volume inventories to match their pre-test baselines after cleanup.
Use ``MATRIX_CASES=local_basic,bfv_data_manila`` only for diagnosis; release
evidence requires the default ``all``.

The release gate must additionally run the maximum combination with
``INJECT_RESTORE_FAILURE=1`` and at least two Cinder data volumes. It must
prove that the injected failure reached target CRIU restore, that Nova restored
the still-running source with its original PID and increasing counter, and that
an immediate retry succeeds without stale RBD mappings, Manila mounts, or OVS
ports.

The driver and E2E path do not impose a fixed Cinder-volume or Manila-share
count. Validate attachment cardinality separately with independent shares::

    SSH_IDENTITY=/path/to/test-key \
      MANILA_SHARES="share-a share-b share-c" \
      CARDINALITY_COUNTS="0 1 3" \
      tools/openstack-incus-live-migration-cardinality-matrix.sh

This runs both root models across zero, one, and three data volumes and
shares, for 18 three-hop cases. Increase the final cardinality for a site's
acceptance limit. ``MANILA_SHARES`` and optional ``MANILA_TAGS`` are
space-separated one-to-one lists; tags must be unique within an instance.
If ``DATA_DEVICES`` supplies fewer hints than ``DATA_VOLUME_COUNT``, Nova
assigns the remaining device names. "Arbitrary count" means there is no
driver hard-coded maximum; Nova, Cinder, Manila and project quotas, Linux
device limits, CRIU checkpoint size, and compute/storage capacity remain
authoritative limits.

Before running the matrix after a host reboot, verify that every compute's
fixed management address is present and that Incus is actually listening on
its configured migration address. A configured ``core.https_address`` alone
is not sufficient if Incus started before that address became available.
When the Manila LVM backend uses a loop-backed file in a test environment,
restore the loop device and activate the volume group before starting
``manila-share``. Production Manila backends should use persistent storage
with equivalent boot ordering rather than an unmanaged loop device.

Set ``SECOND_NETWORK=private`` when running the migration E2E to attach a
second Neutron port before migration. The extended check persists guest
netplan configuration, verifies the secondary fixed IP and OVN-installed OVS
interface on the destination, then verifies the same interface after rejecting
the return migration::

    SECOND_NETWORK=private \
      SSH_IDENTITY=/path/to/test-key \
      tools/openstack-incus-migration-e2e.sh

Tempest scenarios
-----------------

The ``nova-incus-tempest-plugin`` entry point exposes API-only scenarios for
basic compute/network lifecycle and Cinder volume attachment. Tempest must not
connect directly to a host-local Incus daemon because scheduling may place the
instance on any compute node and tenants never receive Incus API access.

Enable Tempest in DevStack, generate its configuration, and list or run the
plugin scenarios with::

    tempest run --list-tests \
      --regex nova_incus_tempest_plugin.tests.scenario
    tempest run \
      --regex nova_incus_tempest_plugin.tests.scenario

The volume scenario requires a configured Cinder backend. Test discovery can
be validated without Cinder, but that is not evidence that attach, persistence,
detach, or migration work against the production storage backend.

For a two-compute DevStack job, the plugin can publish the prepared Ubuntu
Noble image and generate the tested Tempest settings automatically::

    INCUS_TEMPEST_BUILD_IMAGE=True
    INCUS_TEMPEST_MIN_COMPUTE_NODES=2
    INCUS_TEMPEST_FLAVOR_REF=d1
    INCUS_TEMPEST_FLAVOR_REF_ALT=d2
    INCUS_TEMPEST_VOLUME_TYPE=ceph
    INCUS_TEMPEST_RUN_VALIDATION=True
    INCUS_ALLOW_COLD_MIGRATION=True
    INCUS_TEMPEST_ENABLE_EXPERIMENTAL_COLD_MIGRATION=True

The build host must be able to access the configured
``INCUS_TEMPEST_IMAGE_SOURCE`` (by default
``images:ubuntu/noble/cloud``). The generated image contains
``openssh-server`` and ``fuse2fs``. When ``c-vol`` is enabled, the plugin pins
the Tempest Cinder microversion range to 3.42 so attached-volume extension is
tested with the correct API semantics. Cold migration is advertised to
Tempest only when both cold-migration options are true and
``INCUS_TEMPEST_MIN_COMPUTE_NODES`` is at least two. The separate Tempest
option prevents a deployment from treating successful-path test coverage as a
production-safe rollback guarantee.

The ``d1`` flavor has a 5 GiB root disk and is used because the documented
development Incus pool is only 20 GiB. It is a test-environment capacity
constraint, not a production system-container disk limit.

Set ``validation.run_validation=true`` only when the Tempest runner can reach
the configured floating network. With validation disabled, the basic scenario
still covers dynamic credentials, keypairs, security groups, tenant network
creation, config-drive build, Nova/Incus lifecycle, Neutron port activation,
and resource cleanup, but it does not prove guest SSH or floating-IP traffic.
The two Cinder scenarios are explicitly skipped in that mode because their
data-integrity assertions require commands inside the guest. Set
``INCUS_TEMPEST_VOLUME_TYPE`` to an online production backend; otherwise
Cinder may select a stale or disabled default volume type before Nova is
involved.

SSH validation also requires a system-container image that already contains
and enables an SSH server. Incus cloud images can contain cloud-init and accept
the injected key while omitting ``openssh-server`` entirely. The compute driver
must not install guest packages. Build or publish a prepared test image instead
of relying on cloud-init to install SSH over an as-yet unvalidated network.

The supplied publisher can prepare such an image without starting a privileged
build container::

    PREINSTALL_SSH=true \
      IMAGE_NAME=ubuntu-noble-cloud-incus-ssh \
      SOURCE=images:ubuntu/noble/cloud \
      sudo -E tools/publish-incus-image-to-glance.sh

It installs SSH in the extracted rootfs and removes generated host keys before
uploading the unified tar to Glance. Each instance therefore generates its own
SSH host identity on first boot.

The driver exposes config-drive contents read-only at ``/config-drive`` inside
the system container. Tempest validates ``openstack/latest/meta_data.json`` and
``network_data.json`` there. The guest may also process those files with
cloud-init, but tests and applications must not depend on cloud-init's private
``/var/lib/cloud`` cache layout.

Config-drive construction, profile attachment, firewall setup, and container
start are one spawn rollback boundary. A failure removes the Incus instance,
profile, VIF state, and instance directory instead of leaving a guest that Nova
believes failed.

Cross-host cold migration and resize transfer the original config-drive
contents rather than regenerating incomplete metadata without the original
injected files or one-time administrator password. The source creates a
``tar.gz-v1`` payload with a declared compressed size and SHA-256 digest. The
destination validates the encoding, digest, archive paths, entry types, file
count, compressed bytes, and expanded bytes before it claims target storage.
It then applies the target container's idmap and publishes the directory with
an atomic rename. The default limits are 512 entries and 8 MiB; adjust
``configdrive_migration_max_files`` and
``configdrive_migration_max_bytes`` only with corresponding RPC and
``instances_path`` capacity planning.

The config-drive is mounted read-only. Host directories use mode ``0500`` and
files use ``0400`` under the container root's isolated host UID. Source-host
contents remain available for revert and are removed only on resize confirm.

BFV pause and shelve
--------------------

Pause and unpause map to Incus freeze and unfreeze. They retain the running
instance's Cinder attachment, Neutron binding, config-drive, and Ceph watcher.
They do not release memory; use stop or shelve when capacity must be released.

Shelve offload deletes the host-local Incus record and instance directory,
releases the Cinder attachment, and leaves the BFV root volume reserved by
Nova. Unshelve claims the same Cinder RBD on the selected compute, recreates
the read-only config-drive, and restores the Neutron port. Rootfs data remains
on the Cinder volume. Config-drive contents are regenerated by Nova during
unshelve and can contain a new random seed, so they are not byte-for-byte
stable as they are during cold migration.

Validate both workflows against every release and production storage backend::

    IMAGE=ubuntu-noble-incus-bfv-rbd \
    SSH_IDENTITY=~/.ssh/openstack-incus-vm_ed25519 \
      tools/openstack-incus-bfv-lifecycle-e2e.sh

Interface hotplug
-----------------

Nova interface attach creates and plugs the Neutron port through os-vif, then
adds the container-side veth to the running Incus instance. Interface detach
removes the Incus device and os-vif/OVS state. Validate both directions with::

    SERVER=<active-server> NETWORK=private \
      tools/openstack-incus-interface-e2e.sh

The guest operating system owns interface configuration after boot. A newly
attached interface uses its deterministic ``nic<port-id-prefix>`` name, but it
remains unconfigured until the tenant updates the guest network configuration
or starts an appropriate DHCP client. The Nova driver must not rewrite guest
network configuration during hotplug. Detaching another port does not rename
the remaining interfaces, so existing addresses cannot silently move to a
different Neutron port.

Flavor swap limits
------------------

Swap is disabled by default. On a compute with real host swap, set
``[incus] allow_instance_swap=true`` and restart ``nova-compute``. The driver
then reports ``CUSTOM_INCUS_SWAP``. A swap-enabled Flavor must require it::

    openstack flavor create system-container-swap \
      --ram 1024 --vcpus 1 --disk 20 --swap 256
    openstack flavor set system-container-swap \
      --property trait:CUSTOM_INCUS_SYSTEM_CONTAINER=required \
      --property trait:CUSTOM_INCUS_SWAP=required

For this example, the expanded Incus configuration must contain
``limits.memory.swap=256MiB`` and the host cgroup must contain
``memory.swap.max=268435456``. Check the cgroup value as the authority. In a
Podman deployment without working LXCFS passthrough, the guest may still see
host-wide totals in ``/proc/meminfo`` even though cgroup enforcement is
correct.

Flavor process limits
---------------------

Every system container receives a finite PID limit. A flavor can request a
higher or lower value with::

    openstack flavor set system-container-large \
      --property incus:process_limit=4096

If the extra spec is absent, the driver uses
``[incus] default_process_limit``. Values must be positive integers no greater
than ``[incus] maximum_process_limit``; zero, negative, unlimited and malformed
values are rejected. Keep both settings identical on all computes. Placement
does not account for aggregate PID capacity, so monitor host ``pids.current``
and ``pids.events`` in addition to enforcing each instance limit.

The resize regression creates temporary 1-vCPU/512-MiB/PID-2048 and
2-vCPU/1024-MiB/PID-4096 flavors. It verifies Incus cgroups and Placement after
both revert and confirm::

    SSH_IDENTITY=/path/to/test-key \
      tools/openstack-incus-resize-e2e.sh

Same-host resize is deliberately unsupported and
``allow_resize_to_same_host`` must remain ``False``. Rootfs growth is verified
only on quota-capable Incus pools; the development ``dir`` backend cannot
enforce the Flavor disk allocation and is not evidence of production storage
isolation. Rootfs shrink is always rejected by the driver before the source
container is stopped. Nova's API only handles selected zero-disk cases and
must not be treated as the storage-safety boundary.

Instance diagnostics
--------------------

Nova's standardized ``Diagnostics.driver`` field is a closed enum and does not
contain ``incus``. The driver reports the existing ``libvirt`` enum value for
RPC and API compatibility and reports ``hypervisor=incus`` as the actual
runtime. No Nova source patch is required, and an unmodified conductor can
deserialize the object.

For microversion 2.48 and later, ``GET /servers/{uuid}/diagnostics`` reports
the Incus aggregate CPU time in nanoseconds, memory in MiB, and cumulative
per-interface packet, byte, error, and drop counters. CPU time is an instance
aggregate, so the single CPU detail has a null ID while ``num_cpus`` retains
the Flavor vCPU count. On servers with the
``instance_state_started_at`` extension, ``uptime`` is calculated from the
container PID 1 start time reported by Incus; it therefore resets after a real
container restart. It is null on older servers rather than being guessed from
Nova's instance creation time. Disk entries describe the devices but their
I/O fields are null because Nova's standardized diagnostics disks do not
carry the Cinder volume identity needed for billing. Do not use this endpoint
for volume I/O billing.

Microversions before 2.48 receive Nova's legacy flat diagnostics dictionary.
Its memory values are KiB and its CPU and NIC values are the same Incus
cumulative counters.

Volume I/O telemetry
--------------------

Set Nova's ``[DEFAULT] volume_usage_poll_interval`` to a positive number to
enable native ``volume.usage`` polling. The DevStack plugin defaults
``INCUS_VOLUME_USAGE_POLL_INTERVAL`` to 60 seconds. The driver reads Incus'
cgroup v2 disk metrics and returns cumulative read/write requests and bytes
for both BFV roots and hot-plugged Cinder data volumes.

The compute host must expose the same ``/dev`` RBD mappings to nova-compute
and the Podman-hosted Incus daemon. RBD links must retain Cinder's
``volume-<uuid>`` naming. A missing, ambiguous, or incomplete mapping is
skipped rather than attributed to the wrong tenant. Incus caches metrics for
eight seconds. ``IncusComputeManager`` therefore waits nine seconds before a
hot detach or instance shutdown finalizes volume usage. Instance shutdown
waits once regardless of volume count. This bounded latency is intentional:
without it, an immediate detach can reuse the previous metrics response and
lose the last I/O interval. Metering errors are logged but do not prevent
instance deletion. Packaged computes must run ``nova-incus-compute`` rather
than the stock ``nova-compute`` entry point to obtain this final-settlement
behavior.
Counters can reset when an instance restarts or migrates; Nova's volume usage
cache owns conversion of cumulative counters into notification totals.

Cinder volume migration and cross-backend retype invoke Nova's internal
volume-swap path. The Incus driver rejects this operation before connecting or
changing either device. Unlike libvirt/QEMU, an Incus ``unix-block`` profile
update cannot copy the old device into the replacement or atomically pivot
active I/O. Treat online volume migration and retype as unsupported until the
driver implements a crash-consistent block-copy protocol. This API is also
restricted to Cinder; a tenant request to update ``os-volume_attachments`` for
an ordinary volume returns HTTP 409.

This restriction applies to every volume that is still attached, including a
volume attached to a ``SHUTOFF`` instance: Cinder still delegates that
operation to Nova's ``swap_volume`` contract. It does not restrict detached
volumes. For the supported production workflow, detach the data volume,
perform Cinder retype or migration while it is ``available``, and attach it
again. Cinder then owns the complete backend-native or host-assisted copy and
Nova does not participate. Do not represent a stopped-but-attached copy as
offline retype.

The standardized server diagnostics response still leaves its anonymous disk
I/O fields null. Use Nova ``volume.usage`` notifications and the associated
Ceilometer volume meters for billable per-volume I/O.

Ceilometer ``stable/2026.1`` neither defines meters for Nova's
``volume.usage`` notification nor maps their names to its Gnocchi ``volume``
resource by default. When
``ceilometer-anotification`` is enabled, the DevStack plugin installs
``incus-volume-usage.yaml`` under ``/etc/ceilometer/meters.d``. It publishes
the following cumulative Gnocchi metrics, keyed by the Cinder volume UUID:

* ``volume.read.requests``
* ``volume.read.bytes``
* ``volume.write.requests``
* ``volume.write.bytes``

The plugin also applies the matching Gnocchi resource-map patch to the
Ceilometer checkout and removes DevStack's global ``archive_policy`` publisher
override. That override would otherwise ignore all per-metric policy choices.
Production packaging must install the meter file, carry the resource-map
patch until it is available upstream, and restart the Ceilometer notification
agent. Keep the meter type cumulative: Nova's volume usage cache handles
counter resets and reports lifetime totals across polling intervals. The
resource mapping assigns the dedicated ``ceilometer-volume-io`` policy. It
retains ``mean``, ``rate:mean``, and ``rate:sum`` at five-minute granularity.
Billing should consume ``rate:sum`` for the counter increase in an archive
window. ``rate:mean`` is useful for trends, while plain ``mean`` is only the
average cumulative counter value inside that window.

BFV root volume extension
-------------------------

Cinder BFV roots support grow-only online extension. Use Cinder API
microversion 3.42 or later to extend an attached root volume. Cinder first
grows the RBD image and emits Nova's ``volume-extended`` event. The compute
driver then sets the exact byte size on the instance-local Incus root device;
the ``cephext`` driver verifies that it matches the externally owned RBD and
grows the filesystem without resizing the RBD itself.

The size is deliberately stored on the instance-local root device, not only
on its profile. Incus materializes a local root device when it claims a BFV
volume, and that device overrides the profile device with the same name.
Updating only the profile would record the requested value without applying
it to the running root filesystem.

If filesystem growth fails, the Nova event reports failure and the old Incus
device size remains in place. The Cinder volume is not rolled back. A later
hard reboot reconciles the Cinder BDM ``volume_size`` with the local root
device and retries the idempotent filesystem growth before restarting the
container. Cold migration and revert rebuild the destination root device with
that same Cinder size. Root-volume shrinking is unsupported and is rejected
by Cinder before Incus is called.

Run ``tools/openstack-incus-bfv-root-extend-e2e.sh`` as a release gate. It
checks online growth, reboot persistence, confirmed migration, injected
filesystem-growth failure and reboot recovery, reverse handover during
resize revert, persistent guest data, and shrink refusal.
