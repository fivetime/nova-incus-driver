#!/usr/bin/env bash
# Validate the Nova -> Incus -> Neutron ML2/OVN lifecycle on a DevStack host.

set -euo pipefail

IMAGE=${IMAGE:-cirros-0.6.3-x86_64-incus}
FLAVOR=${FLAVOR:-c1}
NETWORK=${NETWORK:-private}
SERVER=${SERVER:-incus-e2e-$RANDOM}
TIMEOUT=${TIMEOUT:-90}
INCUS_PROJECT=${INCUS_PROJECT:-nova}

incus_nova() {
    incus --project "$INCUS_PROJECT" "$@"
}

wait_status() {
    local expected=$1
    local deadline=$((SECONDS + TIMEOUT))
    local current
    while ((SECONDS < deadline)); do
        current=$(openstack server show "$SERVER" -f value -c status 2>/dev/null || true)
        [[ "$current" == "$expected" ]] && return 0
        [[ "$current" == "ERROR" ]] && break
        sleep 2
    done
    openstack server show "$SERVER" || true
    echo "Server did not reach $expected (current: ${current:-missing})" >&2
    return 1
}

cleanup() {
    openstack server delete "$SERVER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

openstack server create \
    --flavor "$FLAVOR" \
    --image "$IMAGE" \
    --network "$NETWORK" \
    --wait "$SERVER" >/dev/null
wait_status ACTIVE

server_id=$(openstack server show "$SERVER" -f value -c id)
port_id=$(openstack port list --server "$server_id" -f value -c ID)
fixed_ip=$(openstack port show "$port_id" -f json -c fixed_ips |
    python3 -c 'import ast,json,sys; value=json.load(sys.stdin)["fixed_ips"]; value=ast.literal_eval(value) if isinstance(value,str) else value; print(value[0]["ip_address"])')
instance_name=
while IFS= read -r name; do
    if [[ "$(incus_nova config get "$name" user.openstack.uuid 2>/dev/null || true)" == "$server_id" ]]; then
        instance_name=$name
        break
    fi
done < <(incus_nova list --format csv -c n)
[[ -n "$instance_name" ]]

incus_nova exec "$instance_name" -- wget -qO- -T 10 \
    http://169.254.169.254/openstack/latest/meta_data.json |
    python3 -c 'import json,sys; expected=sys.argv[1]; assert json.load(sys.stdin)["uuid"] == expected' "$server_id"

iface=$(ovs-vsctl --data=bare --no-heading --columns=name find Interface \
    "external_ids:iface-id=$port_id")
[[ -n "$iface" ]]
[[ "$(ovs-vsctl get Interface "$iface" external_ids:ovn-installed)" == '"true"' ]]

incus_nova exec "$instance_name" -- sh -c \
    "printf '%s\n' '$server_id' > /root/openstack-incus-e2e && sync"
openstack server stop "$SERVER"
wait_status SHUTOFF
openstack server start "$SERVER"
wait_status ACTIVE
openstack server reboot --hard "$SERVER"
wait_status ACTIVE
[[ "$(incus_nova exec "$instance_name" -- cat /root/openstack-incus-e2e)" == "$server_id" ]]

# Rebuild must replace the rootfs while preserving the Neutron attachment.
openstack server rebuild --image "$IMAGE" "$SERVER" >/dev/null
wait_status ACTIVE
[[ "$(openstack port list --server "$server_id" -f value -c ID)" == "$port_id" ]]
! incus_nova exec "$instance_name" -- test -e /root/openstack-incus-e2e
iface=$(ovs-vsctl --data=bare --no-heading --columns=name find Interface \
    "external_ids:iface-id=$port_id")
[[ -n "$iface" ]]
[[ "$(ovs-vsctl get Interface "$iface" external_ids:ovn-installed)" == '"true"' ]]

trap - EXIT
openstack server delete "$SERVER"
deadline=$((SECONDS + TIMEOUT))
while openstack server show "$server_id" >/dev/null 2>&1; do
    ((SECONDS < deadline)) || { echo "Server deletion timed out" >&2; exit 1; }
    sleep 2
done

! incus_nova list --format csv -c n | grep -Fxq "$instance_name"
! openstack port show "$port_id" >/dev/null 2>&1
! ovs-vsctl --data=bare --no-heading --columns=name find Interface \
    "external_ids:iface-id=$port_id" | grep -q .

echo "PASS server=$server_id fixed_ip=$fixed_ip port=$port_id instance=$instance_name"
