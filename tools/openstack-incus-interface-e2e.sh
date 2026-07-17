#!/usr/bin/env bash
# Validate Neutron/OVN interface hotplug and hot-unplug on an Incus server.

set -euo pipefail

SERVER=${SERVER:?Set SERVER to an ACTIVE Incus-backed Nova server}
NETWORK=${NETWORK:-private}
PORT_NAME=${PORT_NAME:-incus-hotplug-e2e-$RANDOM}
TIMEOUT=${TIMEOUT:-60}
INCUS_PROJECT=${INCUS_PROJECT:-nova}

incus_nova() {
    incus --project "$INCUS_PROJECT" "$@"
}

port_id=
cleanup() {
    if [[ -n "$port_id" ]]; then
        openstack server remove port "$SERVER" "$port_id" \
            >/dev/null 2>&1 || true
        openstack port delete "$port_id" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

server_id=$(openstack server show "$SERVER" -f value -c id)
instance_name=
while IFS= read -r name; do
    if [[ "$(incus_nova config get "$name" user.openstack.uuid \
            2>/dev/null || true)" == "$server_id" ]]; then
        instance_name=$name
        break
    fi
done < <(incus_nova list --format csv -c n)
[[ -n "$instance_name" ]]

port_id=$(openstack port create "$PORT_NAME" --network "$NETWORK" \
    -f value -c id)
openstack server add port "$SERVER" "$port_id"

deadline=$((SECONDS + TIMEOUT))
while [[ "$(openstack port show "$port_id" -f value -c status)" != ACTIVE ]]; do
    ((SECONDS < deadline)) || { echo "Port activation timed out" >&2; exit 1; }
    sleep 2
done

iface=$(ovs-vsctl --data=bare --no-heading --columns=name find Interface \
    "external_ids:iface-id=$port_id")
[[ -n "$iface" ]]
[[ "$(ovs-vsctl get Interface "$iface" external_ids:ovn-installed)" == '"true"' ]]

guest_iface=$(incus_nova config get "$instance_name" "volatile.$iface.name")
[[ -n "$guest_iface" ]]
incus_nova exec "$instance_name" -- ip link set "$guest_iface" up
incus_nova exec "$instance_name" -- sh -c 'command -v dhcpcd' >/dev/null
incus_nova exec "$instance_name" -- dhcpcd -x "$guest_iface" >/dev/null 2>&1 || true
incus_nova exec "$instance_name" -- dhcpcd -4 -L -1 -t "$TIMEOUT" "$guest_iface"
fixed_ip=$(openstack port show "$port_id" -f json -c fixed_ips |
    python3 -c 'import ast,json,sys; value=json.load(sys.stdin)["fixed_ips"]; value=ast.literal_eval(value) if isinstance(value,str) else value; print(value[0]["ip_address"])')
incus_nova exec "$instance_name" -- ip -4 -o addr show dev "$guest_iface" |
    grep -Fq " $fixed_ip/"

openstack server remove port "$SERVER" "$port_id"
deadline=$((SECONDS + TIMEOUT))
while [[ "$(openstack port show "$port_id" -f value -c status)" != DOWN ]]; do
    ((SECONDS < deadline)) || { echo "Port deactivation timed out" >&2; exit 1; }
    sleep 2
done

! incus_nova config show "$instance_name" --expanded | grep -q "$iface"
! ovs-vsctl --data=bare --no-heading --columns=name find Interface \
    "external_ids:iface-id=$port_id" | grep -q .
openstack port delete "$port_id"
port_id=
trap - EXIT

echo "PASS server=$server_id instance=$instance_name interface=$guest_iface ip=$fixed_ip"
