Public API contract
===================

This driver does not add a Nova API extension. Operators and tenants use the
standard Nova, Neutron, Glance and Cinder APIs. Nova owns request schemas,
policy, quotas, task states and HTTP response translation; the Incus driver
implements only the compute operations invoked by Nova's compute manager.

The first production target is OpenStack ``stable/2026.1``, Nova compute API
2.1 through the deployment's advertised maximum, Cinder v3, Ubuntu Noble,
Python 3.12 and Incus 7.x.

Required microversions
----------------------

============================== ============ ==================================
Operation                      Microversion Contract
============================== ============ ==================================
Create on a requested host     Nova 2.74    Administrative release tests use
                                            ``host``; ordinary tenant creates
                                            remain scheduler driven.
Standardized diagnostics       Nova 2.48    Returns the Nova diagnostics
                                            schema. Earlier versions receive
                                            the legacy driver dictionary.
Unshelve to a requested host   Nova 2.91    Used to prove cross-compute BFV
                                            unshelve and OVN rebinding.
Explicit BFV root reimage      Nova 2.93    Requires
                                            ``reimage_boot_volume=true``.
Extend an attached volume      Cinder 3.42  Cinder grows the RBD first; Nova
                                            then refreshes the Incus device and
                                            grows a supported filesystem.
============================== ============ ==================================

Operations without a row above use the normal API minimum accepted by the
OpenStack services. Cold resize, confirm, revert, pause, unpause, shelve,
metadata, keypairs, config drive, interface attach/detach, data-volume
attach/detach and server deletion do not require a driver-specific
microversion.

Validated behavior
------------------

.. list-table::
   :header-rows: 1
   :widths: 28 72

   * - API workflow
     - Release contract
   * - BFV create/delete
     - The Cinder root remains the authoritative volume and has one attachment
       and one RBD owner while the server is running.
   * - BFV rebuild/reimage
     - An implicit destructive rebuild is rejected without changing root data,
       attachment or Neutron state. Explicit ACTIVE and SHUTOFF reimage
       replaces root contents and preserves the requested power state.
   * - Shelve/unshelve
     - Offload removes the Incus instance. A 2.91 host-targeted unshelve
       recreates it on the selected compute with the same Cinder root, fixed IP
       and read-only config drive.
   * - Cold resize
     - Nova selects the destination. PID, memory, CPU, root size and Placement
       allocations follow the new flavor. Revert restores the source contract;
       confirm commits it.
   * - BFV cold migration
     - Confirm, revert, stopped migration and post-claim failures use the
       shared-Ceph handover protocol without copying root data.
   * - Bootstrap
     - Keypair, metadata, user-data and config-drive delivery survive a soft
       reboot. The config drive is read-only inside the container.

Error contract
--------------

The following errors are intentional product boundaries rather than missing
API routes:

.. list-table::
   :header-rows: 1
   :widths: 42 8 50

   * - Request
     - HTTP
     - Contract
   * - BFV rebuild without explicit reimage
     - 400
     - Nova rejects the request before destructive root replacement.
   * - Online Cinder volume replacement
     - 409
     - ``swap_volume`` is rejected because containers have no safe live
       block-copy primitive.

Authentication failures, policy denials, quota failures, missing resources,
invalid server states and scheduler capacity failures retain Nova's standard
response codes. They are framework behavior and must not be translated by the
driver. Asynchronous operations can initially return ``202`` and later expose
a failed action event or ``ERROR`` server state. In particular, unsupported
live migration fails its Nova migration pre-check, and invalid flavor limits
fail the build/resize operation; callers must inspect server events rather
than assume a synchronous HTTP error.

Suspend, resume, rescue, and unrescue are asynchronous Nova compute actions.
The Incus compute manager rejects them before any power, network, or storage
side effect, clears the task state, preserves the previous VM state, and
records the corresponding ``Instance*Failure`` in the action event. The
initial API request can nevertheless be accepted before the compute service
reports that failure. A synchronous ``4xx`` response in Horizon or the
OpenStack CLI requires a generic capability gate in the upstream Nova API;
changing only the virt driver exception cannot provide that behavior.

Release evidence
----------------

The public API release gates are:

* ``tools/openstack-incus-bfv-reimage-e2e.sh``
* ``tools/openstack-incus-bfv-lifecycle-e2e.sh``
* ``tools/openstack-incus-bfv-bootstrap-e2e.sh``
* ``tools/openstack-incus-resize-e2e.sh``
* ``tools/openstack-incus-bfv-migration-matrix.sh``
* ``nova_incus_tempest_plugin.tests.scenario``

The migration matrix is fail-closed and audits Nova servers, Cinder volumes,
Incus instances/profiles and host RBD mappings after every case. Tempest SSH
validation is a separate environment capability: when the runner cannot reach
the floating network, API lifecycle tests still run and guest-data scenarios
are explicitly skipped. Dedicated E2E gates continue to validate guest data
through the compute nodes.
