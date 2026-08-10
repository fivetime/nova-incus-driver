Nova Coupling and Upgrade Matrix
================================

Why this file exists
--------------------

This driver reaches into Nova's compute manager and carries patches against
Nova itself. Public driver methods are contract; the rest is not. A private
method can change its signature, its call site, the state it is called with,
or disappear entirely in any release, and none of that will fail to import --
it will fail at runtime, on a specific path, possibly only under failure
conditions.

``tox.ini`` pins Nova to ``stable/2026.1``, so nothing here moves until the
pin does. This file exists for the release where it moves. Each entry records
what the override is for, **the Nova behaviour it depends on**, and how to
tell whether that behaviour still holds. Without the middle column an upgrade
becomes archaeology: the code says what it does, not what it assumed.

Read this before raising the Nova pin, and update it in the same change that
adds or removes an override.

How to re-verify
----------------

For every entry:

1. Read Nova's current implementation of the method. Confirm the signature,
   and confirm the assumption in the "Depends on" column still describes what
   Nova does -- not merely that the method still exists.
2. Run the named tests. They encode the assumption; a passing suite with a
   changed assumption means the test is asserting the wrong thing, which has
   happened here before.
3. For anything touching deletion, migration or the ID-map registry, run the
   corresponding end-to-end script from ``tools/``. Unit tests cannot show a
   changed call order.

An override whose assumption no longer holds is not automatically a bug to
fix in this driver: sometimes Nova gained the behaviour natively and the
override should be deleted. Prefer deleting.

Overridden private methods
--------------------------

Eighteen. These are the ones with no contract.

Failed-build ownership barrier
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``_cleanup_allocated_networks``, ``_cleanup_volumes``,
``_nil_out_instance_obj_host_and_node``

**For**: a build that failed after the driver took durable ownership of an
ID-map generation, Neutron ports or Cinder volumes must not have that
ownership released until the driver has proven the local resources are gone.
Each override consults ``_failed_build_cleanup_barrier`` and returns without
calling ``super()`` when the barrier withholds that particular resource,
logging why at CRITICAL.

**Depends on**: Nova calling exactly these three methods, separately, on the
failed-build path, and tolerating them not running -- that is, treating the
resources as still owned rather than assuming release. Also on the barrier
being consulted before Nova has already released anything.

**Re-verify**: ``test_manager.py`` failed-build barrier tests; then a real
failed build (a spawn that dies after materialization) and confirm the
instance retains its port, volume and host assignment until the driver's
reconciler disposes of them.

**If Nova changes**: the risk is silent. If Nova stops calling one of these,
that resource is released while the driver still believes it owns it, and the
next build can reuse an ID-map range that is still in use. Check the call
sites in ``ComputeManager._do_build_and_run_instance`` first.

Deletion
~~~~~~~~

``_delete_instance``

**For**: fencing the fleet-wide ID-map release around Nova's destructive
delete, so the allocation generation is retired only with proof.

**Depends on**: ``_delete_instance`` being the single funnel for instance
deletion, including local delete, and being called with BDMs already loaded.

**Re-verify**: delete an instance normally, delete one whose compute is down
(local delete), and confirm the registry ends with no allocation and no
release intent in both cases.

``_shutdown_instance``

**For**: settling volume usage counters once per instance before Incus
removes the block devices, rather than once per attached volume.

**Depends on**: ``_shutdown_instance`` running before the driver's
``destroy``, and ``_notify_volume_usage_detach`` being called per BDM from
within it.

**Re-verify**: delete an instance with two data volumes; metering should
settle once, and a metering failure must not prevent deletion.

``_notify_volume_usage_detach``

**For**: the settlement delay above. Gated on
``volume_usage_poll_interval > 0``.

**Depends on**: Nova calling it per BDM during shutdown, and tolerating the
delay.

**Re-verify**: with volume usage polling enabled, confirm counters are
recorded; with it disabled, confirm no delay is paid.

Migration
~~~~~~~~~

``_terminate_volume_connections``

**For**: journalling each cold-migration Cinder attachment rotation before
Nova creates the replacement attachment, and replaying every acknowledged
phase without guessing after an ambiguous create response.

**Depends on**: Nova calling this on the source after the driver has stopped
and detached the guest, while the instance and migration still identify the
source host.

**Re-verify**: the cold attachment-rotation unit matrix, then cold migrate a
multi-volume instance and interrupt the source compute at each rotation phase.

``_post_live_migration_remove_source_vol_connections``

**For**: retiring the source attachment only after the driver has durably
recorded local release, without deleting the BDM that now names the target
attachment.

**Depends on**: Nova passing the old source BDMs after the destination BDM
and attachment are already authoritative.

**Re-verify**: live migrate local and BFV guests with data volumes, inject a
source attachment-delete failure, and require periodic recovery to converge.

``_live_migration_cleanup_flags``

**For**: Nova's base implementation recognises only libvirt and Hyper-V
migration data, so an Incus live migration would not request destination
cleanup. This returns ``(True, False)`` for Incus data: clean up the
destination, never delete the shared root storage.

**Depends on**: the two-flag return contract keeping its meaning
``(do_cleanup, destroy_disks)``, and on Nova continuing to consult the
driver's migrate_data type here.

**Re-verify**: ``tools/openstack-incus-live-migration-matrix.sh``. A changed
flag meaning would delete shared storage -- verify by reading Nova's use of
the return value, not only by running the matrix.

``_rollback_live_migration``

**For**: restoring the source runtime after the control-plane rollback, so a
failed live migration leaves the guest running where it was.

**Depends on**: rollback being invoked on the source with the instance still
owned there.

**Re-verify**: ``tools/openstack-incus-rollback-idempotency-e2e.sh`` case C.

``_finish_resize_helper``

**For**: holding the share-recovery lock across the whole cold-migration
finish, so Manila shares are staged before the target profile exists.

**Depends on**: this being the single entry point for finishing a resize or
cold migration, and being called on the destination.

**Re-verify**: cold-migrate an instance with a Manila share attached.

``_finish_revert_resize``

**For**: validating and reusing retained source mounts before the source
guest restarts.

**Depends on**: revert running on the original source after the destination
has been torn down.

**Re-verify**: revert a cold migration of an instance with a share.

Volume transactions and startup recovery
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``_attach_volume``, ``_detach_volume``

**For**: wrapping Nova's Cinder/BDM transaction in durable Incus intent and
journal generations, and retiring those records only after the framework's
formal attachment or detachment commit.

**Depends on**: Nova saving the BDM and completing or deleting the Cinder
attachment inside these methods, after the driver call returns. The wrapper
also depends on the existing per-instance operation serialization.

**Re-verify**: the volume-journal crash matrix and
``tools/openstack-incus-rollback-idempotency-e2e.sh`` with nova-compute killed
at every attach and detach commit boundary.

``_init_instance``

**For**: completing a durable cold-source rollback before Nova's generic
startup path can power the guest with partially rotated attachments. It also
restores storage ownership, Placement allocations, Neutron bindings and the
instance migration fields in that order.

**Depends on**: ``init_host`` invoking this method before normal per-instance
recovery, and propagating an exception so systemd retries rather than running
later recovery against an unresolved generation.

**Re-verify**: restart nova-compute with a two-volume source rotation stopped
at each durable phase; the first startup must either converge completely or
fail without clearing ``task_state`` or the generation marker.

Manila shares
~~~~~~~~~~~~~

``_mount_all_shares``, ``_umount_all_shares``

**For**: making share mounting one transaction that undoes completed changes
on failure, and making unmounting attempt every share and report a single
recognisable failure.

**Depends on**: Nova calling these around instance start/stop, and on the
share mapping objects it passes.

**Re-verify**: ``tools/openstack-incus-manila-gate-recovery-e2e.sh``.

Efficiency
~~~~~~~~~~

``_get_host_volume_bdms``

**For**: returning host BDMs without one database query per instance.

**Depends on**: only the return shape. This is the least risky override --
Nova's own version is a straightforward query.

**Re-verify**: the unit test alone is sufficient. If Nova's version becomes
efficient, delete this.

Overridden public methods
-------------------------

Six, and these are contract rather than coupling: ``init_host``,
``pre_live_migration``, and the four that refuse operations Incus system
containers cannot perform (``rescue_instance``, ``unrescue_instance``,
``suspend_instance``, ``resume_instance``). They carry ordinary upgrade risk
and are listed here only so the count in reviews is unambiguous.

Nova patches
------------

Five, in ``patches/nova/``. Unlike the overrides these fail loudly: the patch
will not apply.

``0002-hardware-accept-incus-manila-share-trait``
    Lets Nova accept an Incus Manila share transport. **Re-verify**: the
    patch applies, and a share-backed instance schedules.

``0003-compute-allow-incus-manila-live-migration``
    Schedules Manila migrations by capability rather than by hypervisor type.
    **Re-verify**: live-migrate an instance with a share attached.

``0003-register-incus-live-migrate-data``
    Registers the external Incus live migration data object so Nova can
    serialise it. **Re-verify**: any live migration; without it, the data
    object fails to load over RPC.

``0004-glance-send-seekable-upload-size``
    Sends a seekable upload size to Glance. **Re-verify**: create a snapshot
    and confirm the image size is recorded.

``0005-compute-add-failed-build-allocation-policy``
    Adds the hook the failed-build barrier above depends on. **Re-verify**:
    together with the barrier entries -- if this patch stops applying, those
    three overrides lose their foundation.

Reporting to Nova upstream
--------------------------

Two items here exist only because Nova has no representation for Incus:

- ``hypervisor_type`` reports ``HVType.LXD`` because the enum has no
  ``incus`` value. Adding one upstream would let this report accurately.
- The Manila share trait and live-migration capability patches exist for the
  same reason.

Any upstream movement on those removes coupling rather than adding it.

Cross-project patches
---------------------

Four additional patches are runtime dependencies and must be delivered with
the same release. DevStack applies them to the imported Python environment or
the Ceilometer checkout and the runtime preflight scripts inspect the running
processes rather than trusting files in this repository.

``0001-gnocchi-map-nova-volume-usage-metrics``
    Adds the four Nova ``volume.usage`` metrics, their rate-enabled archive
    policy. Run ``ceilometer-upgrade`` after deploying the mapping so Gnocchi
    has the required ``instance``, ``instance_network_interface`` and
    ``volume`` resource types. **Re-verify**: run the Ceilometer notification
    runtime preflight and observe all four volume metrics in Gnocchi.

``0002-enable-incus-compute-inspector``
    Permits the external Incus compute inspector registered by this package.
    **Re-verify**: run the Ceilometer compute runtime preflight and observe a
    CPU, memory and vNIC sample in Gnocchi.

``0001-rbd-fallback-to-kernel-device-path``
    Makes os-brick RBD map/unmap independent of udev links and returns the
    exact kernel device discovered by ``rbd showmapped``. **Re-verify**: the
    compute runtime preflight must find both ``noudev`` and
    ``_find_root_device`` and a data-volume attach/detach must leave no map.

``0001-preserve-seekable-upload-length``
    Keeps the remaining length of a seekable upload body visible to the HTTP
    client. It complements Nova's ``0004`` image-size header patch.
    **Re-verify**: the compute runtime preflight must find
    ``IterableWithLength`` and a concurrent server snapshot must reach
    ``active`` within the Tempest timeout.
