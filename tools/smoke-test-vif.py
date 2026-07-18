# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import argparse
import subprocess
import sys
import uuid

from oslo_config import cfg
import os_vif

from nova.network import model as network_model
from nova.virt.incus import vif as incus_vif


class _Instance:
    def __init__(self, instance_uuid):
        self.uuid = instance_uuid
        self.name = f"smoke-{instance_uuid[:8]}"
        self.project_id = None

    def obj_attr_is_set(self, name):
        return hasattr(self, name)


def _command(*args, check=True):
    result = subprocess.run(
        args, check=False, text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(args)}\n"
            f"{result.stderr}")
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge", default="br-int")
    parser.add_argument("--mtu", type=int, default=1450)
    parser.add_argument(
        "--ovsdb-connection",
        default="unix:/var/run/openvswitch/db.sock")
    args = parser.parse_args()

    port_id = str(uuid.uuid4())
    instance_id = str(uuid.uuid4())
    devname = f"tap{port_id.replace('-', '')[:11]}"
    peer = incus_vif.get_vif_internal_devname({"devname": devname})
    mac = "02:00:%s:%s:%s:%s" % tuple(port_id.replace('-', '')[:8][i:i + 2]
                                      for i in range(0, 8, 2))
    network = network_model.Network(
        id=str(uuid.uuid4()), bridge=args.bridge, label=None,
        subnets=[], bridge_interface=None, vlan=None, mtu=args.mtu)
    vif = network_model.VIF(
        id=port_id,
        address=mac,
        network=network,
        type=network_model.VIF_TYPE_OVS,
        devname=devname,
        ovs_interfaceid=port_id,
        details={network_model.VIF_DETAILS_OVS_HYBRID_PLUG: False})
    driver = incus_vif.IncusGenericVifDriver()
    instance = _Instance(instance_id)
    cfg.CONF.set_override(
        "ovsdb_connection", args.ovsdb_connection, group="os_vif_ovs")
    helper_command = f"sudo -E {sys.prefix}/bin/privsep-helper"
    for group in ("nova_sys_admin", "vif_plug_ovs_privileged"):
        cfg.CONF.set_override(
            "helper_command", helper_command, group=group)
    os_vif.initialize(reset=True)
    plugged = False

    try:
        driver.plug(instance, vif)
        plugged = True
        bridge = _command("ovs-vsctl", "port-to-br", devname)
        iface_id = _command(
            "ovs-vsctl", "get", "Interface", devname,
            "external_ids:iface-id").strip('"')
        attached_mac = _command(
            "ovs-vsctl", "get", "Interface", devname,
            "external_ids:attached-mac").strip('"')
        host_mtu = _command(
            "ip", "-o", "link", "show", "dev", devname).split("mtu ")[1]
        peer_mtu = _command(
            "ip", "-o", "link", "show", "dev", peer).split("mtu ")[1]

        if bridge != args.bridge:
            raise RuntimeError(f"Unexpected bridge: {bridge}")
        if iface_id != port_id:
            raise RuntimeError(f"Unexpected iface-id: {iface_id}")
        if attached_mac.lower() != mac:
            raise RuntimeError(f"Unexpected attached MAC: {attached_mac}")
        if not host_mtu.startswith(str(args.mtu) + " "):
            raise RuntimeError(f"Unexpected host MTU: {host_mtu}")
        if not peer_mtu.startswith(str(args.mtu) + " "):
            raise RuntimeError(f"Unexpected peer MTU: {peer_mtu}")

        print(
            f"PASS bridge={bridge} dev={devname} peer={peer} "
            f"iface_id={iface_id} mtu={args.mtu}")
    finally:
        try:
            if plugged:
                driver.unplug(instance, vif)
        finally:
            _command("ovs-vsctl", "--if-exists", "del-port", devname,
                     check=False)
            _command("ip", "link", "del", devname, check=False)


if __name__ == "__main__":
    main()
