=================
Image build guide
=================

This project uses three different image artifacts. They are not
interchangeable:

.. list-table:: Image artifacts
   :header-rows: 1
   :widths: 22 28 25 25

   * - Artifact
     - Consumer
     - Required format
     - Build source
   * - Outer Incus runtime image
     - Podman/Quadlet on a compute
     - OCI image pinned by digest
     - Incus fork ``docker/quadlet/Dockerfile``
   * - Incus-managed guest root
     - Glance to Incus image import
     - Unified tar with ``metadata.yaml``, ``templates/``, ``rootfs/``
     - ``publish-incus-image-to-glance.sh``
   * - Cinder BFV guest root
     - Glance RBD to Cinder RBD clone
     - Raw ext4 filesystem containing top-level ``rootfs/``
     - ``publish-incus-bfv-image-to-glance.sh``

Do not upload the outer OCI image to Glance. Do not use a compressed unified
tar as a BFV raw disk. Do not use an ordinary VM ``qcow2`` image as a system
container root.

Outer Incus runtime image
=========================

The compute runtime must be built from the approved Incus fork. The separate
generic ``incus-docker-image`` repository does not prove the fork extensions
required for BFV, shared-Ceph migration, ID-map usage queries, and volume QoS.

Check out an immutable revision and build from the Incus repository root::

  git checkout --detach <approved-incus-commit>
  revision=$(git rev-parse HEAD)
  podman build \
    --file docker/quadlet/Dockerfile \
    --label org.opencontainers.image.revision="$revision" \
    --tag registry.example.com/openstack/incus:2026.1-$revision \
    .
  podman push registry.example.com/openstack/incus:2026.1-$revision

Resolve and record the pushed manifest digest::

  skopeo inspect docker://registry.example.com/openstack/incus:2026.1-$revision

The Dockerfile pins the Alpine base, builds ``incusd`` and ``incus`` from the
fork, builds CRIU and LXC, installs Ceph and host-storage tools, uses GNU
coreutils, and writes ``enable-external-masters`` to
``/etc/criu/default.conf``. Changing any pinned base, CRIU commit, LXC
revision, or package set creates a new release candidate and requires the
full migration gate.

Validate the image before updating Quadlets::

  podman run --rm --entrypoint /usr/sbin/incusd <image> --version
  podman run --rm --entrypoint /usr/local/sbin/criu <image> --version
  podman image inspect <image> \
    --format '{{index .Labels "org.opencontainers.image.revision"}}'

The production preflight also verifies required fork API extensions. A
matching version string alone is insufficient.

Guest image baseline
====================

Every guest image must be a Linux **system-container** root with a working
``/sbin/init``. The driver supports systemd and OpenRC images. The following
capabilities are independent:

* Cloud-init is required for Nova user-data, keypair injection, and generated
  network configuration. It is also required by the repository's public BFV
  snapshot E2E, but the Cinder snapshot API itself does not require it.
* SSH is optional for ordinary workloads. Preinstall and enable it only for
  images admitted to SSH/Tempest validation; do not depend on first-boot
  package downloads before networking has been proven.
* ``fuse2fs`` is required when the guest mounts Nova-managed ext4 Cinder data
  volumes in userspace. Its presence is advertised with
  ``hw_incus_data_volume_fuse=true``.
* Manila shares use compute-host staging mounts exposed into the container.
  The guest does not need an NFS client or Manila credentials for that path.
* CRIU live migration depends on the workload, host kernel, outer Incus
  image, and profile policy. Installing CRIU inside the guest is not required.

Never make a guest privileged to avoid image preparation. The admitted
profile must remain ``security.privileged=false`` with isolated ID maps.

Build an Incus-managed guest image
==================================

The supplied publisher copies an upstream Incus image, expands its rootfs,
optionally installs packages in a chroot, rebuilds a unified tar, and uploads
it to Glance.

Ubuntu Noble example::

  source /etc/openstack/admin-openrc
  sudo --preserve-env=OS_* \
    SOURCE=images:ubuntu/noble/cloud \
    IMAGE_NAME=ubuntu-noble-24.04-cloud-incus \
    PREINSTALL_SSH=true \
    PREINSTALL_PACKAGES='fuse2fs jq' \
    tools/publish-incus-image-to-glance.sh

Alpine example::

  source /etc/openstack/admin-openrc
  sudo --preserve-env=OS_* \
    SOURCE=images:alpine/3.21/cloud \
    IMAGE_NAME=alpine-3.21-cloud-incus \
    PREINSTALL_SSH=true \
    PREINSTALL_PACKAGES='e2fsprogs-extra jq' \
    tools/publish-incus-image-to-glance.sh

The exact upstream alias must exist in the configured Incus image remote.
Package names differ by distribution. The publisher removes SSH host keys so
each instance generates a unique identity.

Inspect the resulting Glance artifact::

  openstack image show <image-id> -f json
  openstack image save --file /tmp/incus-unified.tar.gz <image-id>
  tar -tzf /tmp/incus-unified.tar.gz | \
    grep -E '^(metadata.yaml|templates/|rootfs/sbin/init)'

The archive must contain expanded ``rootfs/`` content. An outer tar holding
``rootfs.squashfs`` as an ordinary file is invalid.

Build a Cinder BFV guest image
==============================

Start with the same unified tar, then convert it to a real ext4 image. The
converter requires root for its temporary loop mount::

  source /etc/openstack/admin-openrc
  sudo --preserve-env=OS_* \
    UNIFIED_TAR=/var/tmp/ubuntu-noble-24.04-cloud-incus.tar.gz \
    IMAGE_NAME=ubuntu-noble-24.04-cloud-incus-bfv \
    IMAGE_SIZE_MIB=2048 \
    OUTPUT=/var/tmp/ubuntu-noble-24.04-cloud-incus-bfv.raw \
    tools/publish-incus-bfv-image-to-glance.sh

The tool performs these checks before upload:

* ``rootfs/sbin/init`` exists;
* the ext4 image retains at least 15 percent free headroom;
* ``e2fsck -f -n`` succeeds;
* the Incus-owned ``.incus-idmap`` provenance marker is mode ``0600``; and
* the Glance properties describe BFV, rootfs layout, ID-map provenance, and
  optional ``fuse2fs`` support.

The expected properties include::

  hw_incus_boot_from_volume=true
  hw_incus_rootfs_idmap_provenance=v1
  hw_incus_rootfs_layout=rootfs-directory
  hw_incus_data_volume_fuse=true  # only when fuse2fs is executable

The filesystem root contains a directory named ``rootfs`` because ``cephext``
claims and mounts that directory as the container root. Flattening the
contents directly into the filesystem root violates the contract.

Glance RBD publishing contract
==============================

For optimized BFV creation, upload the BFV image as ``raw/bare`` into a
Glance RBD store in the same Ceph cluster as Cinder. Enable
``show_image_direct_url=true`` and verify::

  direct_url=$(openstack image show <image-id> -f json | \
    jq -er '.properties.direct_url')
  case "$direct_url" in rbd://*) ;; *) exit 1 ;; esac

The Cinder CephX user needs read-only access to the Glance pool as documented
in :doc:`deployment_guide`. Without it, Cinder downloads and imports the
image, creating a valid but flattened full copy.

Prove the optimized path through the public API, not by timing alone::

  RUN_DESTRUCTIVE=true \
  IMAGE=<bfv-image-id> \
  VOLUME_TYPE=<ceph-volume-type> \
  CINDER_POOL=cinder-volumes-rbd-pool \
  CINDER_USER=cinder \
    tools/openstack-incus-bfv-cow-e2e.sh

The test requires a parent in the Glance pool, a named protected snapshot,
positive RBD overlap, and complete cleanup of the Cinder volume and RBD image.

Cloud-init and networking
=========================

The driver supplies NoCloud user-data, keypair metadata, vendor-data, and
cloud-init v2 network configuration. Interface names are derived from
Neutron port UUIDs and remain stable across reboot and migration. An image
must not hard-code ``eth0``.

Validate both systemd-networkd/NetworkManager and OpenRC/ifupdown images as
applicable. The guest must consume the static addresses, routes, DNS, and MTU
allocated by Neutron. Configure a DNS resolver on the subnet when first-boot
package access is required.

Do not use console output as the only readiness or data-integrity authority.
System containers and their init systems differ in how they expose
``/dev/console``. The public BFV snapshot E2E writes durable guest marker
files, reads them through the owning compute's Incus API, and uses console
logs only as diagnostics. Its user-data creates ``/usr/local/sbin`` and
``/etc/local.d`` because minimal Alpine images need not pre-create them.

Cinder data-volume filesystem safety
=====================================

The host attaches a Cinder RBD as an Incus ``unix-block`` device. The guest
owns filesystem formatting and mounting. For untrusted ext4 data, use
``fuse2fs`` in the guest so tenant-controlled filesystem metadata is parsed in
userspace rather than by the compute kernel.

Do not enable host-kernel ext4 mount interception globally. Optional Incus
FUSE mount interception is requested per Flavor with
``incus:intercept_data_volume_mounts=true`` and prevents CRIU live migration
because the seccomp notify proxy cannot be reattached after restore. The
default, migration-compatible path has the guest invoke ``fuse2fs`` itself.

Example guest operation::

  mkdir -p /srv/data
  fuse2fs -o rw+,allow_other /dev/vdb /srv/data

Adapt options to the distribution's FUSE policy. Test unmount, Cinder detach,
online extension, hard reboot, and migration before admitting the image.

Manila image requirements
=========================

Current Manila support is host-mounted NFS or CephFS exposed as an Incus disk
device. Therefore:

* the compute storage network, not the tenant network, reaches the export;
* the compute installs the NFS/CephFS mount helper and GNU ``timeout``;
* the guest needs no NFS client, Ceph secret, libnfs helper, direct-volume
  agent, or virtiofs daemon for this integration; and
* share data is outside Nova root snapshots and must use Manila snapshot,
  replication, or backup facilities.

An application inside the guest still needs ordinary filesystem permissions
for the exposed mountpoint. Validate the intended UID/GID behavior under the
instance's isolated ID map.

Image acceptance matrix
=======================

Run at least the following for every admitted guest image revision:

#. Create and delete an image-backed instance; verify cloud-init and Neutron
   addressing.
#. Create a BFV instance from the raw image; verify the ``.incus-idmap``
   provenance and root marker.
#. Run the four local/BFV by one/two initial-data-volume cases when
   ``fuse2fs`` support is advertised.
#. Run hard reboot and verify persistent root and data-volume markers.
#. Run public BFV snapshot/restore and verify both durable guest markers::

     RUN_DESTRUCTIVE=true \
     IMAGE=<bfv-image-id> FLAVOR=<flavor> NETWORK=<network> \
     VOLUME_TYPE=<ceph-volume-type> \
     HOST_SSH_MAP='compute-1=root@192.0.2.11,...' \
     SSH_IDENTITY=/path/to/audit-key \
       tools/openstack-incus-bfv-snapshot-public-api-e2e.sh

#. Run the RBD CoW test and confirm the exact Glance parent.
#. If SSH is advertised, run Tempest SSH validation without installing
   packages at first boot.
#. If live migration is advertised, run the complete root/data/share matrix,
   target-restore failure rollback, and immediate retry. Confirm
   ``migration.incremental.memory=false`` in both local and expanded config.
#. Delete all resources and prove Nova, Glance, Cinder, Neutron, Incus,
   os-brick, RBD, and ID-map inventories returned to baseline.

Record the Glance image UUID, checksum, properties, source alias/digest,
package manifest, build command, outer Incus digest/revision, and dated E2E
result. A mutable image name is not release evidence.

Common failures
===============

``No /sbin/init``
  The unified archive is nested incorrectly or a VM disk was uploaded as an
  Incus root. Inspect the archive before upload.

BFV volume has no RBD parent
  Check the Glance direct URL, raw format, Ceph FSID, protected Glance
  snapshot, and ``client.cinder`` read-only Glance-pool capability. Cinder
  logs normally show a permission error followed by download/import when the
  capability is missing.

Cloud-init marker is absent
  Read ``cloud-init status --long`` and its logs through ``incus exec``. Check
  that the image contains cloud-init and a supported init system. Do not
  conclude that data is absent merely because Nova console output is empty.

Initial data-volume build is rejected
  Verify that ``fuse2fs`` is executable in the guest root and that Glance has
  ``hw_incus_data_volume_fuse=true``. The property is an admission contract,
  not a substitute for the executable.

Manila mount is unavailable
  Troubleshoot the compute-to-storage route, Manila access rule, host mount
  helper, timeout binary, staging journal, and Incus device. Installing an NFS
  client in the guest does not repair this host-staged integration.
