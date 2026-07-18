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

The driver maps Nova's user-data to ``cloud-init.user-data`` and the selected
Nova keypair to the NoCloud ``public-keys`` metadata. A tenant can therefore
use the normal Nova API without Incus-specific options::

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

Cinder boot-from-volume
-----------------------

The Incus fork can use a Cinder Ceph RBD volume as the container root disk
through its ``cephext`` storage driver. Configure a pool that references the
same Ceph pool and least-privilege CephX user as the Cinder backend::

    INCUS_BFV_POOL_NAME=cinder-bfv
    INCUS_BFV_CEPH_POOL=cinder-volumes-rbd-pool
    INCUS_BFV_CEPH_USER=cinder
    INCUS_BFV_CEPH_CLUSTER_NAME=ceph

DevStack creates the pool and writes ``[incus]
boot_from_volume_storage_pool``. Outside DevStack, create the ``cephext`` pool
with the Incus API and set that Nova option explicitly.

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
is not proof that the optimized path was used.

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
This covers the LINSTOR DRBD driver's ``local`` connection, LINSTOR iSCSI, and
Ceph RBD local attachment. For example::

    openstack server add volume --device /dev/vdb <server> <volume>

The tenant operating system owns partitioning, formatting, mounting, and
``/etc/fstab`` configuration for the attached device. Detaching removes the
Incus device before asking os-brick to disconnect the host path. A failed
Incus attach rolls back the os-brick connection.

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

Cold migration
--------------

Cold migration is disabled by default and remains experimental. Image-backed
roots use Incus pull data transfer. Cinder BFV roots instead use the fork's
shared-Ceph handover to claim the same authoritative RBD without copying it.
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
not power on a SHUTOFF instance. Production enablement still requires this
suite to pass on every compute pair and an operator runbook for the
fail-closed case where the durable marker itself cannot be written.

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

    python -m nova.virt.lxd.cmd.compute --config-file /etc/nova/nova-cpu.conf

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
the modernized repository retains historical ``nova-lxd`` Git tags::

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
    migration_address = https://192.0.2.10:8443
    migration_preflight_tls_cert = /etc/nova/incus-preflight/client.crt
    migration_preflight_tls_key = /etc/nova/incus-preflight/client.key
    migration_preflight_tls_ca = /etc/nova/incus-preflight/default-server.crt
    migration_preflight_project = nova-preflight
    migration_preflight_server_names = 192.0.2.10:compute-1,192.0.2.11:compute-2
    migration_preflight_tls_ca_by_server = 192.0.2.10:/etc/nova/incus-preflight/compute-1.crt,192.0.2.11:/etc/nova/incus-preflight/compute-2.crt

``migration_address`` must be an HTTPS origin without a path. Firewall the
listener so it is reachable only from Nova compute nodes. The ordinary driver
connection remains the local Unix socket; tenants must never receive Incus API
or migration credentials.

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
        user.openstack.bfv_pool=cinder-bfv \
        user.openstack.cinder_rbd_pool=cinder-volumes-rbd-pool
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

For iSCSI, Fibre Channel, or NVMe backends with redundant paths, configure
os-brick through the Incus driver group::

    [incus]
    volume_use_multipath = true
    volume_enforce_multipath = true
    num_volume_scan_tries = 7

``volume_enforce_multipath`` prevents a silent fallback to a single path and
requires ``volume_use_multipath``. Keep both disabled for a backend such as
Ceph RBD that does not use host multipath. Every compute node must use the
same policy and have the required initiator, multipath daemon, and os-brick
privileged-command dependencies installed.

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
must use a userspace filesystem implementation such as ``fuse2fs``. Cinder
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

Live migration is not supported. BFV evacuation is experimental and disabled
by default. Enable it only after integrating an external STONITH or power
fencing system that proves the failed source cannot access Ceph::

    [incus]
    allow_bfv_evacuate = true

Nova's service-down check is only a soft prerequisite and is not fencing.
Evacuation accepts exactly one ``boot_index=0`` Cinder RBD root, validates the
destination ``cephext`` pool and Incus handover extensions, then delegates
Placement, Cinder attachment, Neutron rebinding, and spawn to Nova's default
rebuild workflow. Local/image-backed roots are rejected to preserve pet data.
Shared Ceph does not make ``instance_on_disk`` true on a destination whose
host-local Incus database has no instance record.

Rescue and unrescue are also disabled. The legacy implementation depended on
binding a directory from the compute host into a rescue container, which is
not valid for an Incus-managed Ceph or LVM root volume and violates the host
filesystem isolation boundary. Rescue must remain unavailable until it can
attach the retained root volume through a storage-pool-native Incus API.
The deprecated ``allow_live_migration`` option has no effect. Incus CRIU/live
migration must not be enabled through this driver because the legacy SDK path
does not implement Nova's confirm/recover ownership protocol. A planned host
maintenance test can use the experimental cold migration path above, subject
to its documented manual-recovery boundary.

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
      --regex nova_lxd_tempest_plugin.tests.scenario
    tempest run \
      --regex nova_lxd_tempest_plugin.tests.scenario

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

The guest operating system owns interface configuration. For example, an
Ubuntu image whose netplan matches only its boot-time ``eth0`` leaves a newly
attached ``eth1`` down until the tenant updates netplan or starts a DHCP client.
The Nova driver must not rewrite guest network configuration during hotplug.

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

The DevStack plugin applies the Nova compatibility patch required for the
standard diagnostics ``driver=lxd`` value by default. Set
``INCUS_APPLY_NOVA_DIAGNOSTICS_PATCH=False`` only when the Nova tree already
contains equivalent support. Deployment fails rather than silently applying
a patch that no longer matches the selected Nova release.

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
