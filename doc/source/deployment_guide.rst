================
Deployment guide
================

This guide describes a production-oriented deployment of the Nova Incus
system-container driver. It is a runbook, not a substitute for the
site-specific OpenStack, Ceph, network, identity, backup, and fencing designs.
Use :doc:`production_readiness` as the release decision and :doc:`usage` for
detailed feature operations.

Supported deployment model
==========================

The supported baseline is OpenStack 2026.1, Ubuntu Noble compute hosts,
Python 3.12, the repository's Incus fork, and Incus system containers. Each
compute runs one independent, non-clustered Incus daemon. Nova and Placement
own scheduling and capacity; Neutron ML2/OVN owns tenant networking; Cinder
owns BFV roots and data volumes; Manila owns shares.

Do not grant tenants access to the Incus API. The host-local Incus project is
an implementation boundary controlled by ``nova-compute``, not a second
tenant control plane.

The production storage models are:

* an Incus-managed root on an Incus Ceph RBD pool; or
* a Cinder BFV RBD root claimed through the fork's ``cephext`` driver.

Local ZFS, LVM, or directory roots are useful for development but are not
automatically recoverable after host loss. LINSTOR is not part of this
architecture.

Plan and pin the release
========================

Record one immutable release set before changing any host:

* the ``openstack-incus`` commit;
* the Incus fork commit;
* the outer Incus OCI manifest digest and its
  ``org.opencontainers.image.revision`` label;
* the Incus Python SDK fork commit;
* the upstream LXC and CRIU commits embedded in the outer image;
* the Nova, os-brick, python-glanceclient, and Ceilometer patch set;
* the Nova configuration and the Incus project name;
* the Ceph FSID, pool names, and CephX client names; and
* the etcd namespace and TLS identities used for fleet ID maps.

Never deploy a mutable OCI tag in a Quadlet. Keep credentials, private keys,
and site addresses out of this repository and out of image layers.

Host prerequisites
==================

Every compute needs cgroup v2 CPU, memory, I/O, and PID controllers, AppArmor,
OVS/OVN integration, Podman with Quadlet, ``ceph-common`` for os-brick RBD
mapping, GNU coreutils, and synchronized time. Disable host core dumps and
Ubuntu Apport. Provide dedicated, bounded filesystems or logical volumes for
``/var/lib/incus`` and ``/var/log/incus``.

Create the persistent runtime paths before installing the Quadlets::

  install -d -m 0700 /var/lib/incus /var/log/incus
  install -d -m 0711 /var/lib/lxcfs
  systemctl disable --now lxcfs.service || true
  systemctl mask lxcfs.service

``/var/lib/lxcfs`` must be recursively shared. Do not create a new stacked
bind mount on every service restart::

  findmnt -no TARGET,PROPAGATION /var/lib/lxcfs

The outer Incus runtime is built and admitted as described in
:doc:`image_build_guide`. Install the two units and the validator from the
Incus fork's ``deploy/quadlet`` directory::

  install -Dm0644 incus-lxcfs.container \
    /etc/containers/systemd/incus-lxcfs.container
  install -Dm0644 incus-podman.container \
    /etc/containers/systemd/incus-podman.container
  install -Dm0755 validate-runtime.sh \
    /usr/local/sbin/incus-quadlet-validate
  systemctl daemon-reload
  systemctl enable --now incus-lxcfs.service incus-podman.service
  incus-quadlet-validate

Replace each Quadlet's ``Image=`` value with the approved digest before
starting it. The ``incus-podman`` service is the replaceable control plane;
``incus-lxcfs`` is a long-lived data plane. A routine ``incusd`` rollout must
not restart LXCFS. Record guest init PIDs and verify that they remain unchanged
after restarting only ``incus-podman``. Also read ``/proc/meminfo`` in every
running guest; an API health check alone cannot prove that LXCFS remained
connected.

Ceph and Rook configuration
===========================

Create separate pools and least-privilege CephX users for Glance, Cinder
volumes, Cinder backups, and Incus roots. Never reuse the Cinder pool as the
Incus-owned rootfs pool. Enable the ``rbd`` application metadata on each pool
from the Ceph administrative plane.

Fast Glance-to-Cinder BFV creation requires all of the following:

* Glance and Cinder use RBD pools in the same Ceph cluster;
* Glance stores the image as raw and exposes its direct URL;
* the direct URL is
  ``rbd://<fsid>/<glance-pool>/<image>/<protected-snapshot>``; and
* ``client.cinder`` can read, but not write, the Glance pool.

When Rook owns ``client.cinder``, store this ``CephClient`` in the Ceph
provider cluster's Git/IaC source of truth. Applying only an imperative
``ceph auth caps`` command is not durable, and patching only the live CRD is
not sufficient if an older manifest can recreate it later::

  apiVersion: ceph.rook.io/v1
  kind: CephClient
  metadata:
    name: cinder
    namespace: rook-ceph
  spec:
    secretName: openstack-incus-ceph-client-cinder
    caps:
      mon: profile rbd
      mgr: >-
        profile rbd pool=cinder-volumes-rbd-pool,
        profile rbd-read-only pool=glance-images-rbd-pool
      osd: >-
        profile rbd pool=cinder-volumes-rbd-pool,
        profile rbd-read-only pool=glance-images-rbd-pool

Apply the reviewed manifest to the **Ceph provider cluster**, not merely to an
external consumer cluster that happens to have Rook CRDs installed::

  kubectl apply -f cephclient-cinder.yaml
  kubectl -n rook-ceph wait --for=jsonpath='{.status.phase}'=Ready \
    cephclient/cinder --timeout=120s
  kubectl -n rook-ceph get cephclient cinder -o yaml
  kubectl -n rook-ceph exec deploy/rook-ceph-tools -- \
    ceph auth get client.cinder

Require ``status.observedGeneration`` to equal ``metadata.generation`` and
verify both ``mgr`` and ``osd`` capabilities. Copy the generated keyring to
Cinder hosts through the site's secret-management system. A packaged service
normally uses ``root:cinder 0640``; DevStack uses ``root:stack 0640``. A
root-only keyring can make an administrator's ``rbd`` command pass while the
Cinder service fails.

Configure Glance with the RBD store and direct locations, for example::

  [DEFAULT]
  enabled_backends = rbd:rbd
  show_image_direct_url = true

  [glance_store]
  default_backend = rbd

  [rbd]
  rbd_store_pool = glance-images-rbd-pool
  rbd_store_user = glance
  rbd_store_ceph_conf = /etc/ceph/ceph.conf

Sites that retain S3 as the ordinary image store can expose RBD only for BFV
publication. With the OpenStack-Helm chart in this workspace, apply
``values_overrides/glance/s3-rbd-bfv.yaml`` after synchronizing the Rook
``client.glance`` key into the configured OpenStack namespace Secret. The
override deliberately sets chart ``storage: rbd`` so the API pods receive the
Ceph configuration and keyring, while keeping ``glance_store.default_backend``
set to ``s3``. Publish BFV images with ``IMAGE_STORE=rbd``; do not rely on the
default store.

Configure Cinder's RBD backend to use only its volume pool::

  [ceph]
  volume_driver = cinder.volume.drivers.rbd.RBDDriver
  volume_backend_name = ceph
  rbd_pool = cinder-volumes-rbd-pool
  rbd_user = cinder
  rbd_ceph_conf = /etc/ceph/ceph.conf

Create a matching Incus ``cephext`` pool on every BFV-capable compute and map
the authoritative Cinder RBD pool name to it::

  incus storage create cinder-bfv cephext \
    source=cinder-volumes-rbd-pool ceph.user.name=cinder \
    ceph.cluster_name=ceph

  [incus]
  boot_from_volume_storage_pools = \
    cinder-volumes-rbd-pool:cinder-bfv

The exact pool keys are validated by the fork and the production preflight;
do not replace this with a host bind or ``raw.lxc`` root mount.

Incus project and storage
=========================

Create one restricted, host-local Nova project with images and networks
disabled. Use the same project name in ``nova.conf`` and every inventory
audit::

  incus project create nova \
    features.images=false features.networks=false \
    features.profiles=true features.storage.volumes=true

Nova/Neutron compute nodes must not contain Incus-managed tenant networks.
OVN and OVS own tenant interfaces. Audit before admission::

  incus network list --all-projects --columns emn --format csv

Every row must report ``NO`` in the managed column. Do not delete a legacy
network until all profile and instance references have been reviewed.

For an Incus-owned shared Ceph root pool, configure a distinct Ceph pool and
an explicit per-compute Placement capacity slice. The sum of slices must not
exceed the operator-approved cluster budget after reserves for Glance,
Cinder, recovery, and Ceph health.

Nova driver installation
========================

Deploy the driver into the same Python environment as Nova. The repository's
DevStack plugin is the reference integration and applies the required runtime
patches fail-closed. For packaged deployment, make the same patch set part of
the immutable Nova build; do not patch running files by hand.

Nova, os-brick, python-glanceclient, Ceilometer, and Manila are upstream
dependencies, not project forks. Their local checkouts must remain clean API
baselines used to generate and validate the canonical patch files in this
repository. Manila itself is not patched; the Manila-related downstream
changes patch Nova's scheduling and compute-manager paths.
The rationale, role ownership, rebase procedure, upstream tracking duty, and
removal criteria for those files are defined in
:doc:`upgrade_matrix`. Package maintainers and operators must follow that
policy rather than preserving changes in an upstream checkout.

The Incus server and Incus Python SDK use a different delivery model: both are
maintained forks. Pin their exact commits in the release manifest and artifact
provenance. ``INCUS_PYTHON_SDK_BRANCH=main`` is convenient for development but
is not an immutable production pin. Fork ownership, rebase, and upstream
removal rules are defined in :doc:`upgrade_matrix` alongside the non-fork
patch policy.

LXC is not one of those forks. Build the outer image from the official
``https://github.com/lxc/lxc.git`` source at a reviewed commit. The retired
``fivetime/lxc`` repository and its old CRIU cgroup-finalization branch are
not valid production inputs because upstream LXC already contains the
complete replacement.

The custom ``IncusLiveMigrateData`` object must be importable by every
conductor before any upgraded compute advertises version 1.6. Upgrade and
restart all API/conductor services first, pass the controller-only runtime
gate, then roll computes one at a time. Keep live migration frozen while
conductors are mixed-version.

Nova 2026.1 must start the project's manager wrapper, not the stock
``nova-compute`` executable::

  nova-incus-compute --config-file /etc/nova/nova-cpu.conf

The minimum compute configuration resembles the following. Values are site
specific and placeholders must not be copied literally::

  [DEFAULT]
  compute_driver = incus.IncusDriver
  my_shared_fs_storage_ip = 192.0.2.0/24

  [incus]
  project = nova
  storage_pool = ceph-rootfs
  shared_storage_pool_capacity_gb = <per-compute-slice>
  boot_from_volume_storage_pools = \
    cinder-volumes-rbd-pool:cinder-bfv
  allow_cold_migration = false
  allow_live_migration = false
  allow_bfv_evacuate = false
  migration_auto_recovery = true
  enable_manila_shares = true
  manila_cephfs_cluster_fsid = <canonical-ceph-fsid>
  manila_cephfs_filesystem_name = <manila-cephfs-name>
  share_mount_timeout = 30
  share_unmount_timeout = 30

CephFS exports returned by Manila commonly use the legacy
``monitor:port,...:/absolute/path`` form. Ceph clusters may expose multiple
filesystems, so that form does not identify the filesystem by itself. The
Incus driver requires the cluster FSID and the Manila filesystem name and
converts the export to Ceph's unambiguous
``client@fsid.filesystem=/absolute/path`` device syntax. Configure the same
values on every eligible compute. A missing, malformed, or inconsistent value
fails the share attach before a host mount is created. The selected filesystem
must be the one used by every CephFS share type schedulable to that compute;
use host aggregates to separate backends that use different CephFS names.

Use ``nova.virt.incus.config.list_opts`` or the generated Nova sample config
as the authority for option names. Do not infer options from this abbreviated
example. In particular, live migration, cold migration, and BFV evacuation
remain disabled until their release matrices pass.

Compute admission is enforced by the systemd admission drop-in and the
ephemeral ``/run/openstack-incus/compute-admitted`` token, not by a Nova
``[incus]`` option. When live migration is enabled, the driver writes and
validates ``migration.incremental.memory=false`` in the Nova-owned Incus
profile and instance config; it is likewise an Incus instance key, not a
``nova.conf`` option.

Fleet ID maps
=============

All migration-capable computes must share an authenticated etcd v3 namespace
for globally unique ID-map ownership. Use a deployment-unique namespace,
mTLS or authenticated HTTPS, and an account restricted to that namespace and
its control sibling. Bootstrap an empty registry exactly once while the fleet
is frozen, then disable bootstrap mode.

Every compute must use identical base, slot size, slot count, endpoint, and
namespace settings. Run the fleet audit before enabling scheduling. Missing
registry data is never permission to reuse an ID range held by a live or
offline container.

Networking
==========

Configure Neutron ML2/OVN on every compute before registering Nova. The Incus
driver attaches the guest side to the host veth prepared for Neutron; it does
not create an Incus-managed OVN network. Ensure management/migration traffic,
OVN Geneve traffic, provider traffic, and storage traffic follow the site's
separate routing and firewall design.

For Manila NFS or CephFS, the **compute hosts** mount approved exports and
expose those host staging mounts to the container. The guest does not receive
Manila credentials or an NFS mount instruction and does not need an NFS
client for this path. The storage network must be reachable from every
eligible compute, not from tenant networks. Set ``my_shared_fs_storage_ip``
to the isolated compute storage CIDR and prevent tenants from spoofing it.

Compute admission and release
=============================

Keep the Nova compute service disabled until both host preflights pass::

  EXPECTED_INCUS_IMAGE_DIGEST=sha256:<digest> \
  EXPECTED_INCUS_REVISION=<commit> \
    tools/openstack-incus-production-preflight.sh

  CHECK_CONFIGURED_BACKENDS=True \
    tools/openstack-incus-ceph-preflight.sh

Then verify the registered service and Placement provider::

  openstack compute service list --service nova-compute
  openstack resource provider list
  openstack resource provider inventory list <provider-uuid>
  openstack resource provider trait list <provider-uuid>

The provider must advertise standard compute inventory and
``CUSTOM_INCUS_SYSTEM_CONTAINER``. Storage selectors and optional
capabilities must have matching inventories and traits.

Run the aggregate release gate with immutable image, flavor, network, pool,
compute, and SSH inputs. The public storage phase must include both BFV
snapshot/restore and exact RBD parent validation::

  RUN_PUBLIC_API_E2E=true
  PUBLIC_API_BFV_IMAGE=<admitted-bfv-image-uuid>
  PUBLIC_API_VOLUME_TYPE=<ceph-volume-type>
  PUBLIC_API_CINDER_POOL=cinder-volumes-rbd-pool

  tools/openstack-incus-release-gate.sh

The RBD CoW check must show a non-empty parent in the Glance pool and positive
overlap. Fast provisioning time alone is not proof of a clone. A Cinder
download/import fallback is functionally usable but is a release failure for
the optimized BFV contract.

Upgrade procedure
=================

For an Incus control-plane image update:

1. Disable scheduling to the compute and prove that it owns no in-progress
   Nova migration or Incus operation.
2. Record every guest init PID and verify LXCFS from inside each guest.
3. Change only the digest in ``incus-podman.container``.
4. Run ``systemctl daemon-reload`` and restart only
   ``incus-podman.service``.
5. Run ``incus-quadlet-validate`` and compare guest PIDs and ``/proc`` data.
6. Run the compute and fleet preflights before re-enabling scheduling.

Do not restart ``incus-lxcfs`` during an ordinary Incus upgrade. Existing
FUSE superblocks cannot reattach to a replacement LXCFS process; an LXCFS
restart requires draining and restarting affected guests.

For a mixed OpenStack/Incus protocol upgrade, use the staged controller-first
sequence in :doc:`production_readiness`. Never drain workloads through live
migration while the old CRIU parent-chain behavior is still admitted.

Rollback and recovery rules
===========================

Never force-delete Nova rows, Cinder attachments, RBD images, Incus profiles,
or journal files to make an audit green. Disable scheduling and reconcile the
exact Nova instance, BDM attachment ID, Cinder attachment detail, Neutron
binding, Incus profile, journal, host mapping, and RBD watcher.

BFV evacuation additionally requires an external STONITH authority proving
that the source cannot access Ceph. A Nova service-down state is not fencing.
Keep ``allow_bfv_evacuate=false`` until a real BMC, PDU, or independently
hosted test fence passes the destructive release gate.

See :doc:`usage` for the documented manual boundaries around pre-driver cold
migration attachment rotation and Cinder attachment-create response loss.
Ambiguous ownership must remain fail-closed.
