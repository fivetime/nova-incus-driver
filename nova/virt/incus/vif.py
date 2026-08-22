# Copyright (c) 2015 Canonical Ltd
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from oslo_concurrency import processutils
from oslo_log import log as logging

from nova import conf
from nova import exception
from nova.network import model as network_model
from nova.network import os_vif_util
from nova.privsep import linux_net

import os_vif
from vif_plug_ovs import linux_net as ovs_linux_net
from vif_plug_ovs.ovsdb import ovsdb_lib


CONF = conf.CONF

LOG = logging.getLogger(__name__)

GUEST_NIC_NAME_LEN = 15


def get_vif_devname(vif):
    """Get device name for a given vif."""
    if 'devname' in vif:
        return vif['devname']
    return ("nic" + vif['id'])[:network_model.NIC_NAME_LEN]


def get_vif_internal_devname(vif):
    """Get the internal device name for a given vif."""
    return get_vif_devname(vif).replace('tap', 'tin')


def get_vif_guest_devname(vif):
    """Return a migration-stable guest interface name for a Neutron port."""
    port_id = str(vif['id']).replace('-', '').lower()
    return ('nic' + port_id)[:GUEST_NIC_NAME_LEN]


def _create_veth_pair(dev1_name, dev2_name, mtu=None):
    """Create a veth pair through os-vif's constrained privsep context."""
    ovs_linux_net.create_veth_pair(dev1_name, dev2_name, mtu)


def _is_ovs_vif_port(vif):
    return vif['type'] == 'ovs' and not vif.is_hybrid_plug_enabled()


def _get_bridge_config(vif):
    return {
        'bridge': vif['network']['bridge'],
        'mac_address': vif['address']}


def _get_ovs_config(vif):
    return {
        'bridge': vif['network']['bridge'],
        'mac_address': vif['address']}


def _get_tap_config(vif):
    return {'mac_address': vif['address']}


def _create_ovs_vif_cmd(bridge, dev, iface_id, mac,
                        instance_id, interface_type=None):
    cmd = ['--', '--if-exists', 'del-port', dev, '--',
           'add-port', bridge, dev,
           '--', 'set', 'Interface', dev,
           'external-ids:iface-id=%s' % iface_id,
           'external-ids:iface-status=active',
           'external-ids:attached-mac=%s' % mac,
           'external-ids:vm-uuid=%s' % instance_id]
    if interface_type:
        cmd += ['type=%s' % interface_type]
    return cmd


def _create_ovs_vif_port(bridge, dev, iface_id, mac, instance_id,
                         mtu=None, interface_type=None):
    ovsdb_lib.BaseOVS(CONF.os_vif_ovs).create_ovs_vif_port(
        bridge, dev, iface_id, mac, instance_id, mtu=mtu,
        interface_type=interface_type)


def _delete_ovs_vif_port(bridge, dev, delete_dev=True):
    ovsdb_lib.BaseOVS(CONF.os_vif_ovs).delete_ovs_vif_port(
        bridge, dev, delete_netdev=delete_dev)


CONFIG_GENERATORS = {
    'bridge': _get_bridge_config,
    'ovs': _get_ovs_config,
    'tap': _get_tap_config,
}


def get_config(vif):
    """Get Incus specific config for a vif."""
    vif_type = vif['type']

    try:
        return CONFIG_GENERATORS[vif_type](vif)
    except KeyError:
        raise exception.NovaException(
            'Unsupported vif type: {}'.format(vif_type))


# VIF_TYPE_OVS = 'ovs'
# VIF_TYPE_BRIDGE = 'bridge'
def _post_plug_wiring_veth_and_bridge(instance, vif):
    """Create the veth pair before os-vif wires its host-side device.

    :param instance: the instance to plug into the bridge
    :type instance: ???
    :param vif: the virtual interface to plug into the bridge
    :type vif: :class:`nova.network.model.VIF`
    """
    network = vif.get('network')
    mtu = network.get_meta('mtu') if network else None
    v1_name = get_vif_devname(vif)
    v2_name = get_vif_internal_devname(vif)
    if not linux_net.device_exists(v1_name):
        _create_veth_pair(v1_name, v2_name, mtu)
    else:
        linux_net.set_device_mtu(v1_name, mtu)


POST_PLUG_WIRING = {
    'bridge': _post_plug_wiring_veth_and_bridge,
    'ovs': _post_plug_wiring_veth_and_bridge,
}


def _post_plug_wiring(instance, vif):
    """Perform nova-incus specific post os-vif plug processing

    Perform any post os-vif plug wiring required to network
    the instance Incus container with the underlying Neutron
    network infrastructure

    :param instance: the instance to plug into the bridge
    :type instance: ???
    :param vif: the virtual interface to plug into the bridge
    :type vif: :class:`nova.network.model.VIF`
    """

    LOG.debug("Performing post plug wiring for VIF {}".format(vif),
              instance=instance)
    vif_type = vif['type']

    try:
        POST_PLUG_WIRING[vif_type](instance, vif)
        LOG.debug("Post plug wiring step for VIF {} done".format(vif),
                  instance=instance)
    except KeyError:
        LOG.debug("No post plug wiring step "
                  "for vif type: {}".format(vif_type),
                  instance=instance)


# VIF_TYPE_OVS = 'ovs'
# VIF_TYPE_BRIDGE = 'bridge'
def _post_unplug_wiring_delete_veth(instance, vif):
    """Remove the host-side veth this driver created for one VIF.

    Safe to call for a VIF that never had one: checking for the device
    first keeps a port that was never wired from logging a failure trace
    on every unplug.

    :param instance: the instance to plug into the bridge
    :type instance: ???
    :param vif: the virtual interface to plug into the bridge
    :type vif: :class:`nova.network.model.VIF`
    """
    v1_name = get_vif_devname(vif)
    if not linux_net.device_exists(v1_name):
        return
    try:
        linux_net.delete_net_dev(v1_name)
    except processutils.ProcessExecutionError:
        LOG.exception("Failed to delete veth for vif {}".format(vif),
                      instance=instance)


POST_UNPLUG_WIRING = {
    'bridge': _post_unplug_wiring_delete_veth,
    'ovs': _post_unplug_wiring_delete_veth,
}


def _post_unplug_wiring(instance, vif):
    """Perform nova-incus specific post os-vif unplug processing

    Perform any post os-vif unplug wiring required to remove
    network interfaces assocaited with a incus container.

    :param instance: the instance to plug into the bridge
    :type instance: :class:`nova.db.sqlalchemy.models.Instance`
    :param vif: the virtual interface to plug into the bridge
    :type vif: :class:`nova.network.model.VIF`
    """

    LOG.debug("Performing post unplug wiring for VIF {}".format(vif),
              instance=instance)
    vif_type = vif['type']

    try:
        POST_UNPLUG_WIRING[vif_type](instance, vif)
        LOG.debug("Post unplug wiring for VIF {} done".format(vif),
                  instance=instance)
    except KeyError:
        LOG.debug("No post unplug wiring step "
                  "for vif type: {}".format(vif_type),
                  instance=instance)


class IncusGenericVifDriver(object):
    """Generic VIF driver for Incus networking."""

    def __init__(self):
        os_vif.initialize()

    def plug(self, instance, vif):
        vif_type = vif['type']
        # Hybrid plug expects an iptables firewall on a qbr bridge; this
        # driver's firewall is a deliberate no-op for OVN-enforced security
        # groups, so accepting hybrid plug would silently run the guest
        # with no security groups at all.
        is_hybrid = getattr(vif, 'is_hybrid_plug_enabled', lambda: False)
        if vif_type == network_model.VIF_TYPE_OVS and is_hybrid():
            raise exception.InternalError(
                'ovs hybrid plug requires an iptables firewall this driver '
                'does not provide; configure Neutron for OVN security '
                'groups (ovs_hybrid_plug=false) on Incus computes')
        instance_info = os_vif_util.nova_to_osvif_instance(instance)

        # The device must exist before os-vif can attach it to an OVS or
        # Linux bridge. Incus receives the peer through its physical NIC.
        _post_plug_wiring(instance, vif)

        # os-vif exclusively owns host bridge and OVS port configuration.
        if vif_type == network_model.VIF_TYPE_OVS:
            vif['delegate_create'] = True
        vif_obj = os_vif_util.nova_to_osvif_vif(vif)
        if vif_obj is not None:
            os_vif.plug(vif_obj, instance_info)
        else:
            # Legacy non-os-vif codepath
            func = getattr(self, 'plug_%s' % vif_type, None)
            if not func:
                raise exception.InternalError(
                    "Unexpected vif_type=%s" % vif_type
                )
            func(instance, vif)

    def reassert(self, instance, vif):
        """Reassert host wiring without deleting the container's veth peer."""
        if _is_ovs_vif_port(vif):
            _delete_ovs_vif_port(
                vif['network']['bridge'], get_vif_devname(vif),
                delete_dev=False)
        self.plug(instance, vif)

    def unplug(self, instance, vif):
        vif_type = vif['type']
        if vif_type in {
                network_model.VIF_TYPE_BINDING_FAILED,
                network_model.VIF_TYPE_UNBOUND}:
            # os-vif has nothing to undo for a port Neutron never bound,
            # and asking it would fail. This driver's own veth is a
            # different matter: it was created by plug() before anything
            # was bound, its name derives from the port ID rather than
            # the VIF type, and it therefore survives a port that later
            # reports binding-failed. Leaving it stranded a host device
            # per failed binding, forever.
            LOG.warning(
                'Skipping os-vif unplug for Neutron %(type)s VIF '
                '%(vif)s; removing the veth this driver created',
                {'type': vif_type, 'vif': vif.get('id')}, instance=instance)
            _post_unplug_wiring_delete_veth(instance, vif)
            return
        instance_info = os_vif_util.nova_to_osvif_instance(instance)

        # Try os-vif codepath first
        vif_obj = os_vif_util.nova_to_osvif_vif(vif)
        if vif_obj is not None:
            os_vif.unplug(vif_obj, instance_info)
        else:
            # Legacy non-os-vif codepath
            func = getattr(self, 'unplug_%s' % vif_type, None)
            if not func:
                raise exception.InternalError(
                    "Unexpected vif_type=%s" % vif_type
                )
            func(instance, vif)

        _post_unplug_wiring(instance, vif)

    def plug_tap(self, instance, vif):
        """Plug a VIF_TYPE_TAP virtual interface."""
        v1_name = get_vif_devname(vif)
        v2_name = get_vif_internal_devname(vif)
        network = vif.get('network')
        mtu = network.get_meta('mtu') if network else None
        # NOTE(jamespage): For nova-incus this is really a veth pair
        #                  so that a) security rules get applied on the host
        #                  and b) that the container can still be wired.
        if not linux_net.device_exists(v1_name):
            _create_veth_pair(v1_name, v2_name, mtu)
        else:
            linux_net.set_device_mtu(v1_name, mtu)

    def unplug_tap(self, instance, vif):
        """Unplug a VIF_TYPE_TAP virtual interface."""
        dev = get_vif_devname(vif)
        try:
            linux_net.delete_net_dev(dev)
        except processutils.ProcessExecutionError:
            LOG.exception("Failed while unplugging vif for instance",
                          instance=instance)
