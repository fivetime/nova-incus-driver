Nova-incus VIF Design Notes
===========================

VIF plugging workflow
---------------------

Nova-Incus makes use of the os-vif interface plugging library to wire Incus
instances into underlying Neutron networking; however there are some
subtle differences between the Nova-Libvirt driver and the Nova-Incus driver
in terms of how the last mile wiring is done to the instances.

In the Nova-Libvirt driver, Libvirt is used to start the instance in a
paused state, which creates the required tap device and any required wiring
to bridges created in previous os-vif plugging events.

The concept of 'start-and-pause' does not exist in Incus, so the driver
creates a veth pair instead, allowing the last mile wiring to be created
in advance of the actual Incus container being created.

The veth pair is created through ``vif_plug_ovs.linux_net``.  This uses the
os-vif ``vif_plug`` privsep context and its restricted network capabilities;
the nova-compute process is not given unrestricted sudo access.  Direct
``processutils.execute(..., run_as_root=True)`` calls are not supported by
modern Nova and must not be reintroduced here.

This allows Neutron to complete the underlying VIF plugging at which
point it will notify Nova and the Nova-Incus driver will create the Incus
container and wire the pre-created veth pair into its profile.

tap/tin veth pairs
------------------

The veth pair created to wire the Incus instance into the underlying Neutron
networking uses the tap and tin prefixes; the tap named device is present
on the host OS, allowing iptables based firewall rules to be applied as
they are for other virt drivers, and the tin named device is passed to
Incus as part of the container profile. Incus will rename this device
internally within the container to an ethNN style name.

The Incus profile devices for network interfaces are created as 'physical'
rather than 'bridged' network devices as the driver handles creation of
the veth pair, rather than Incus (as would happen with a bridged device).

Incus profile interface naming
------------------------------

The name of the interfaces in each containers Incus profile maps to the
devname provided by Neutron as part of VIF plugging - this will typically
be of the format tapXXXXXXX.  This allows for easier identification of
the interface during detachment events later in instance lifecycle.

Prior versions of the nova-incus driver did not take this approach; interface
naming was not consistent depending on when the interface was attached. The
legacy code used to detach interfaces based on MAC address is used as a
fallback in the event that the new style device name is not found, supporting
upgraders from previous versions of the driver.

Supported Interface Types
-------------------------

The Nova-Incus driver has been validated with:

 - OpenvSwitch (ovs) hybrid bridge ports.
 - OpenvSwitch (ovs) standard ports.
 - Linuxbridge (bridge) ports

The initial modernized target is Neutron ML2/OVN with a standard OVS port.
For that path os-vif owns the host-side OVS port and its external IDs. Incus
receives only the peer interface as a physical NIC device.

OVN gateway chassis
-------------------

Only a chassis with a working provider-network L2 uplink may use
``enable-chassis-as-gw``. In the supplied DevStack topology the controller has
that uplink and sets ``ENABLE_CHASSIS_AS_GW=True``. Compute-only nodes set it
to ``False``. They do not need a local external bridge: tenant traffic reaches
the selected gateway chassis over the OVN Geneve overlay.

Marking an isolated compute chassis as a gateway can make OVN schedule a
logical router there. A floating IP may then have correct logical flows but no
physical path to the provider network. Verify both the chassis options in the
OVN southbound database and actual provider connectivity before enabling the
gateway role on additional computes.
