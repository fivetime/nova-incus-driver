# Copyright 2016 Canonical Ltd
# All Rights Reserved.
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
from unittest import mock

from nova import context
from nova import exception
from nova.network import model as network_model
from nova import test
from nova.tests.unit import fake_instance
from nova.virt.incus import vif
from oslo_concurrency import processutils

GATEWAY = network_model.IP(address='101.168.1.1', type='gateway')
DNS_BRIDGE = network_model.IP(address='8.8.8.8', type=None)
SUBNET = network_model.Subnet(
    cidr='101.168.1.0/24', dns=[DNS_BRIDGE], gateway=GATEWAY,
    routes=None, dhcp_server='191.168.1.1')
NETWORK = network_model.Network(
    id='ab7b876b-2c1c-4bb2-afa1-f9f4b6a28053', bridge='br0', label=None,
    subnets=[SUBNET], bridge_interface=None, vlan=99, mtu=1000)
OVS_VIF = network_model.VIF(
    id='da5cc4bf-f16c-4807-a0b6-911c7c67c3f8', address='ca:fe:de:ad:be:ef',
    network=NETWORK, type=network_model.VIF_TYPE_OVS,
    devname='tapda5cc4bf-f1',
    ovs_interfaceid='7b6812a6-b044-4596-b3c5-43a8ec431638',
    details={network_model.VIF_DETAILS_OVS_HYBRID_PLUG: False})
OVS_HYBRID_VIF = network_model.VIF(
    id='da5cc4bf-f16c-4807-a0b6-911c7c67c3f8', address='ca:fe:de:ad:be:ef',
    network=NETWORK, type=network_model.VIF_TYPE_OVS,
    devname='tapda5cc4bf-f1',
    ovs_interfaceid='7b6812a6-b044-4596-b3c5-43a8ec431638',
    details={network_model.VIF_DETAILS_OVS_HYBRID_PLUG: True})
TAP_VIF = network_model.VIF(
    id='da5cc4bf-f16c-4807-a0b6-911c7c67c3f8', address='ca:fe:de:ad:be:ee',
    network=NETWORK, type=network_model.VIF_TYPE_TAP,
    devname='tapda5cc4bf-f1',
    details={'mac_address': 'aa:bb:cc:dd:ee:ff'})
LB_VIF = network_model.VIF(
    id='da5cc4bf-f16c-4807-a0b6-911c7c67c3f8', address='ca:fe:de:ad:be:ed',
    network=NETWORK, type=network_model.VIF_TYPE_BRIDGE,
    devname='tapda5cc4bf-f1')

INSTANCE = fake_instance.fake_instance_obj(
    context.get_admin_context(), name='test')


class GetVifDevnameTest(test.NoDBTestCase):
    """Tests for get_vif_devname."""

    def test_get_vif_guest_devname_is_stable_and_linux_compatible(self):
        an_vif = {
            'id': '01234567-89ab-cdef-0123-456789abcdef',
        }

        devname = vif.get_vif_guest_devname(an_vif)

        self.assertEqual('nic0123456789ab', devname)
        self.assertLessEqual(len(devname), vif.GUEST_NIC_NAME_LEN)

    def test_get_vif_devname_devname_exists(self):
        an_vif = {
            'id': 'da5cc4bf-f16c-4807-a0b6-911c7c67c3f8',
            'devname': 'oth1',
        }

        devname = vif.get_vif_devname(an_vif)

        self.assertEqual('oth1', devname)

    def test_get_vif_devname_devname_nonexistent(self):
        an_vif = {
            'id': 'da5cc4bf-f16c-4807-a0b6-911c7c67c3f8',
        }

        devname = vif.get_vif_devname(an_vif)

        self.assertEqual('nicda5cc4bf-f1', devname)


class GetConfigTest(test.NoDBTestCase):
    """Tests for get_config."""

    def setUp(self):
        super(GetConfigTest, self).setUp()
        self.CONF_patcher = mock.patch('nova.virt.incus.vif.CONF')
        self.CONF = self.CONF_patcher.start()
        self.CONF.firewall_driver = 'nova.virt.firewall.NoopFirewallDriver'

    def tearDown(self):
        super(GetConfigTest, self).tearDown()
        self.CONF_patcher.stop()

    def test_get_config_bad_vif_type(self):
        """Unsupported vif types raise an exception."""
        an_vif = network_model.VIF(
            id='da5cc4bf-f16c-4807-a0b6-911c7c67c3f8',
            address='ca:fe:de:ad:be:ef',
            network=NETWORK, type='invalid',
            devname='tapda5cc4bf-f1',
            ovs_interfaceid='7b6812a6-b044-4596-b3c5-43a8ec431638')

        self.assertRaises(
            exception.NovaException, vif.get_config, an_vif)

    def test_get_config_bridge(self):
        expected = {'bridge': 'br0', 'mac_address': 'ca:fe:de:ad:be:ef'}
        an_vif = network_model.VIF(
            id='da5cc4bf-f16c-4807-a0b6-911c7c67c3f8',
            address='ca:fe:de:ad:be:ef',
            network=NETWORK, type='bridge',
            devname='tapda5cc4bf-f1',
            ovs_interfaceid='7b6812a6-b044-4596-b3c5-43a8ec431638')

        config = vif.get_config(an_vif)

        self.assertEqual(expected, config)

    def test_get_config_ovs_bridge(self):
        expected = {
            'bridge': 'br0', 'mac_address': 'ca:fe:de:ad:be:ef'}
        an_vif = network_model.VIF(
            id='da5cc4bf-f16c-4807-a0b6-911c7c67c3f8',
            address='ca:fe:de:ad:be:ef',
            network=NETWORK, type='ovs',
            devname='tapda5cc4bf-f1',
            ovs_interfaceid='7b6812a6-b044-4596-b3c5-43a8ec431638')

        config = vif.get_config(an_vif)

        self.assertEqual(expected, config)

    def test_get_config_ovs_hybrid(self):
        expected = {
            'bridge': 'qbrda5cc4bf-f1', 'mac_address': 'ca:fe:de:ad:be:ef'}
        an_vif = network_model.VIF(
            id='da5cc4bf-f16c-4807-a0b6-911c7c67c3f8',
            address='ca:fe:de:ad:be:ef',
            network=NETWORK, type='ovs',
            devname='tapda5cc4bf-f1',
            ovs_interfaceid='7b6812a6-b044-4596-b3c5-43a8ec431638',
            details={network_model.VIF_DETAILS_OVS_HYBRID_PLUG: True})

        config = vif.get_config(an_vif)

        self.assertEqual(expected, config)

    def test_get_config_tap(self):
        expected = {'mac_address': 'ca:fe:de:ad:be:ef'}
        an_vif = network_model.VIF(
            id='da5cc4bf-f16c-4807-a0b6-911c7c67c3f8',
            address='ca:fe:de:ad:be:ef',
            network=NETWORK, type='tap',
            devname='tapda5cc4bf-f1',
            ovs_interfaceid='7b6812a6-b044-4596-b3c5-43a8ec431638')

        config = vif.get_config(an_vif)

        self.assertEqual(expected, config)


class IncusGenericVifDriverTest(test.NoDBTestCase):
    """Tests for IncusGenericVifDriver."""

    def setUp(self):
        super(IncusGenericVifDriverTest, self).setUp()
        self.vif_driver = vif.IncusGenericVifDriver()

    @mock.patch.object(vif, '_post_plug_wiring')
    @mock.patch('nova.virt.incus.vif.os_vif')
    def test_plug_ovs(self, os_vif, _post_plug_wiring):
        self.vif_driver.plug(INSTANCE, OVS_VIF)

        self.assertEqual(
            'tapda5cc4bf-f1', os_vif.plug.call_args[0][0].vif_name)
        self.assertTrue(
            os_vif.plug.call_args[0][0].port_profile.create_port)
        self.assertEqual(
            'instance-00000001', os_vif.plug.call_args[0][1].name)
        _post_plug_wiring.assert_called_with(INSTANCE, OVS_VIF)

    @mock.patch.object(vif, '_delete_ovs_vif_port')
    @mock.patch.object(vif.IncusGenericVifDriver, 'plug')
    def test_reassert_ovs_preserves_veth(self, plug, delete_ovs_vif_port):
        self.vif_driver.reassert(INSTANCE, OVS_VIF)

        delete_ovs_vif_port.assert_called_once_with(
            'br0', 'tapda5cc4bf-f1', delete_dev=False)
        plug.assert_called_once_with(INSTANCE, OVS_VIF)

    @mock.patch.object(vif.ovsdb_lib, 'BaseOVS')
    def test_delete_ovs_port_uses_os_vif_ovsdb(self, base_ovs):
        vif._delete_ovs_vif_port(
            'br0', 'tapda5cc4bf-f1', delete_dev=False)

        base_ovs.assert_called_once_with(vif.CONF.os_vif_ovs)
        base_ovs.return_value.delete_ovs_vif_port.assert_called_once_with(
            'br0', 'tapda5cc4bf-f1', delete_netdev=False)

    @mock.patch.object(vif, '_post_unplug_wiring')
    @mock.patch('nova.virt.incus.vif.os_vif')
    def test_unplug_ovs(self, os_vif, _post_unplug_wiring):
        self.vif_driver.unplug(INSTANCE, OVS_VIF)

        self.assertEqual(
            'tapda5cc4bf-f1', os_vif.unplug.call_args[0][0].vif_name)
        self.assertEqual(
            'instance-00000001', os_vif.unplug.call_args[0][1].name)
        _post_unplug_wiring.assert_called_with(INSTANCE, OVS_VIF)

    @mock.patch.object(vif, '_post_plug_wiring')
    @mock.patch.object(vif, '_create_veth_pair')
    @mock.patch('nova.virt.incus.vif.os_vif')
    def test_plug_tap(self, os_vif, _create_veth_pair, _post_plug_wiring):
        self.vif_driver.plug(INSTANCE, TAP_VIF)
        os_vif.plug.assert_not_called()
        _create_veth_pair.assert_called_with('tapda5cc4bf-f1',
                                             'tinda5cc4bf-f1',
                                             1000)
        _post_plug_wiring.assert_called_with(INSTANCE, TAP_VIF)

    @mock.patch.object(vif, '_post_unplug_wiring')
    @mock.patch('nova.virt.incus.vif.linux_net')
    @mock.patch('nova.virt.incus.vif.os_vif')
    def test_unplug_tap(self, os_vif, linux_net, _post_unplug_wiring):
        self.vif_driver.unplug(INSTANCE, TAP_VIF)
        os_vif.plug.assert_not_called()
        linux_net.delete_net_dev.assert_called_with('tapda5cc4bf-f1')
        _post_unplug_wiring.assert_called_with(INSTANCE, TAP_VIF)

    @mock.patch.object(vif, '_post_unplug_wiring')
    @mock.patch('nova.virt.incus.vif.os_vif')
    def test_unplug_binding_failed_is_idempotent(
            self, os_vif, _post_unplug_wiring):
        failed_vif = {'id': 'failed-port', 'type': 'binding_failed'}

        self.vif_driver.unplug(INSTANCE, failed_vif)

        os_vif.unplug.assert_not_called()
        _post_unplug_wiring.assert_not_called()


class PostPlugTest(test.NoDBTestCase):
    """Tests for post plug operations"""

    def setUp(self):
        super(PostPlugTest, self).setUp()

    @mock.patch.object(vif.linux_net, 'delete_net_dev')
    def test_post_unplug_logs_veth_delete_failure(self, delete_net_dev):
        delete_net_dev.side_effect = processutils.ProcessExecutionError()

        vif._post_unplug_wiring_delete_veth(INSTANCE, OVS_VIF)

        delete_net_dev.assert_called_once_with('tapda5cc4bf-f1')

    @mock.patch('nova.virt.incus.vif._create_veth_pair')
    @mock.patch('nova.virt.incus.vif._add_bridge_port')
    @mock.patch('nova.virt.incus.vif.linux_net')
    def test_post_plug_ovs_hybrid(self,
                                  linux_net,
                                  add_bridge_port,
                                  create_veth_pair):
        linux_net.device_exists.return_value = False

        vif._post_plug_wiring(INSTANCE, OVS_HYBRID_VIF)

        linux_net.device_exists.assert_called_with('tapda5cc4bf-f1')
        create_veth_pair.assert_called_with('tapda5cc4bf-f1',
                                            'tinda5cc4bf-f1',
                                            1000)
        add_bridge_port.assert_not_called()

    @mock.patch('nova.virt.incus.vif._create_veth_pair')
    @mock.patch('nova.virt.incus.vif._add_bridge_port')
    @mock.patch.object(vif, '_create_ovs_vif_port')
    @mock.patch('nova.virt.incus.vif.linux_net')
    def test_post_plug_ovs(self,
                           linux_net,
                           create_ovs_vif_port,
                           add_bridge_port,
                           create_veth_pair):

        linux_net.device_exists.return_value = False

        vif._post_plug_wiring(INSTANCE, OVS_VIF)

        linux_net.device_exists.assert_called_with('tapda5cc4bf-f1')
        create_veth_pair.assert_called_with('tapda5cc4bf-f1',
                                            'tinda5cc4bf-f1',
                                            1000)
        add_bridge_port.assert_not_called()
        create_ovs_vif_port.assert_not_called()

    @mock.patch('nova.virt.incus.vif._create_veth_pair')
    @mock.patch('nova.virt.incus.vif._add_bridge_port')
    @mock.patch('nova.virt.incus.vif.linux_net')
    def test_post_plug_bridge(self,
                              linux_net,
                              add_bridge_port,
                              create_veth_pair):
        linux_net.device_exists.return_value = False

        vif._post_plug_wiring(INSTANCE, LB_VIF)

        linux_net.device_exists.assert_called_with('tapda5cc4bf-f1')
        create_veth_pair.assert_called_with('tapda5cc4bf-f1',
                                            'tinda5cc4bf-f1',
                                            1000)
        add_bridge_port.assert_not_called()

    @mock.patch('nova.virt.incus.vif._create_veth_pair')
    @mock.patch('nova.virt.incus.vif._add_bridge_port')
    @mock.patch('nova.virt.incus.vif.linux_net')
    def test_post_plug_tap(self,
                           linux_net,
                           add_bridge_port,
                           create_veth_pair):
        linux_net.device_exists.return_value = False

        vif._post_plug_wiring(INSTANCE, TAP_VIF)

        linux_net.device_exists.assert_not_called()


class PostUnplugTest(test.NoDBTestCase):
    """Tests for post unplug operations"""

    @mock.patch('nova.virt.incus.vif.linux_net')
    def test_post_unplug_ovs_hybrid(self, linux_net):
        vif._post_unplug_wiring(INSTANCE, OVS_HYBRID_VIF)
        linux_net.delete_net_dev.assert_called_with('tapda5cc4bf-f1')

    @mock.patch('nova.virt.incus.vif.linux_net')
    def test_post_unplug_ovs(self, linux_net):
        vif._post_unplug_wiring(INSTANCE, OVS_VIF)
        linux_net.delete_net_dev.assert_called_with('tapda5cc4bf-f1')

    @mock.patch('nova.virt.incus.vif.linux_net')
    def test_post_unplug_bridge(self, linux_net):
        vif._post_unplug_wiring(INSTANCE, LB_VIF)
        linux_net.delete_net_dev.assert_called_with('tapda5cc4bf-f1')


class MiscHelpersTest(test.NoDBTestCase):
    """Misc tests for vif module"""

    @mock.patch.object(vif.ovs_linux_net, 'create_veth_pair')
    def test_create_veth_pair_uses_os_vif_privsep(self, create_veth_pair):
        vif._create_veth_pair('tapXYZ', 'tinXYZ', 1450)
        create_veth_pair.assert_called_once_with('tapXYZ', 'tinXYZ', 1450)

    def test_is_ovs_vif_port(self):
        self.assertTrue(vif._is_ovs_vif_port(OVS_VIF))
        self.assertFalse(vif._is_ovs_vif_port(OVS_HYBRID_VIF))
        self.assertFalse(vif._is_ovs_vif_port(TAP_VIF))

    @mock.patch.object(vif, 'processutils')
    def test_add_bridge_port(self, processutils):
        vif._add_bridge_port('br-int', 'tapXYZ')
        processutils.execute.assert_called_with(
            'brctl', 'addif', 'br-int', 'tapXYZ', run_as_root=True)
