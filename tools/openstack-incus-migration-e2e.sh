#!/usr/bin/env bash
# Validate transactional Nova cold migration between independent Incus hosts.

set -euo pipefail

IMAGE=${IMAGE:-ubuntu-noble-24.04-cloud-incus}
FLAVOR=${FLAVOR:-ds512M}
NETWORK=${NETWORK:-private}
SECOND_NETWORK=${SECOND_NETWORK:-}
SOURCE_HOST=${SOURCE_HOST:-ubuntu}
DEST_HOST=${DEST_HOST:-incus-node-02}
SOURCE_SSH=${SOURCE_SSH:-root@10.224.0.16}
DEST_SSH=${DEST_SSH:-root@10.224.0.17}
CONTROLLER_SSH=${CONTROLLER_SSH:-}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
SOURCE_MIGRATION_ADDRESS=${SOURCE_MIGRATION_ADDRESS:-https://10.224.0.16:8443}
DEST_MIGRATION_ADDRESS=${DEST_MIGRATION_ADDRESS:-https://10.224.0.17:8443}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
SERVER=${SERVER:-incus-migration-e2e-$RANDOM}
TIMEOUT=${TIMEOUT:-180}

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)
server_id=
instance_name=
second_port_id=

remote() {
    local host=$1
    shift
    "${SSH[@]}" "$host" "$@"
}

if [[ -n "$CONTROLLER_SSH" ]]; then
    openstack() {
        local command_line
        printf -v command_line '%q ' "$@"
        "${SSH[@]}" "$CONTROLLER_SSH" \
            "source $CONTROLLER_OPENRC >/dev/null && openstack $command_line"
    }
fi

wait_status() {
    local expected=$1
    local deadline=$((SECONDS + TIMEOUT))
    local current
    while ((SECONDS < deadline)); do
        current=$(openstack server show "$server_id" -f value -c status \
            2>/dev/null || true)
        [[ "$current" == "$expected" ]] && return 0
        [[ "$current" == "ERROR" ]] && break
        sleep 2
    done
    openstack server show "$server_id" || true
    echo "Server did not reach $expected (current: ${current:-missing})" >&2
    return 1
}

assert_owner() {
    local active_host=$1
    local inactive_host=$2
    local expected_state=$3
    [[ "$(remote "$active_host" \
        "incus list '$instance_name' --format csv -c s")" == \
        "$expected_state" ]]
    ! remote "$inactive_host" "incus info '$instance_name'" \
        >/dev/null 2>&1
}

cleanup() {
    if [[ -n "$server_id" ]]; then
        openstack server delete "$server_id" >/dev/null 2>&1 || true
    fi
    if [[ -n "$instance_name" ]]; then
        remote "$SOURCE_SSH" \
            "incus delete '$instance_name' --force 2>/dev/null || true; \
             incus profile delete '$instance_name' 2>/dev/null || true" || true
        remote "$DEST_SSH" \
            "incus delete '$instance_name' --force 2>/dev/null || true; \
             incus profile delete '$instance_name' 2>/dev/null || true" || true
    fi
    if [[ -n "$second_port_id" ]]; then
        openstack port delete "$second_port_id" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

remote "$SOURCE_SSH" "incus version >/dev/null"
remote "$DEST_SSH" "incus version >/dev/null"
remote "$DEST_SSH" "curl -fsSk '$SOURCE_MIGRATION_ADDRESS/1.0' >/dev/null"
remote "$SOURCE_SSH" "curl -fsSk '$DEST_MIGRATION_ADDRESS/1.0' >/dev/null"

server_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --image "$IMAGE" --network "$NETWORK" \
    --host "$SOURCE_HOST" -f value -c id "$SERVER")
wait_status ACTIVE
instance_name=$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)
port_id=$(openstack port list --server "$server_id" -f value -c ID)
guest_iface="nic${port_id//-/}"
guest_iface=${guest_iface,,}
guest_iface=${guest_iface:0:15}
[[ -n "$guest_iface" ]]
fixed_ip=$(openstack port show "$port_id" -f json -c fixed_ips |
    python3 -c 'import ast,json,sys; v=json.load(sys.stdin)["fixed_ips"]; v=ast.literal_eval(v) if isinstance(v,str) else v; print(v[0]["ip_address"])')

if [[ -n "$SECOND_NETWORK" ]]; then
    second_port_id=$(openstack port create \
        "${SERVER}-secondary" --network "$SECOND_NETWORK" -f value -c id)
    openstack server add port "$server_id" "$second_port_id"
    deadline=$((SECONDS + TIMEOUT))
    while [[ "$(openstack port show "$second_port_id" \
            -f value -c status)" != ACTIVE ]]; do
        ((SECONDS < deadline)) || {
            echo "Secondary port activation timed out" >&2
            exit 1
        }
        sleep 2
    done
    second_fixed_ip=$(openstack port show "$second_port_id" \
        -f json -c fixed_ips |
        python3 -c 'import ast,json,sys; v=json.load(sys.stdin)["fixed_ips"]; v=ast.literal_eval(v) if isinstance(v,str) else v; print(v[0]["ip_address"])')
    second_iface=$(remote "$SOURCE_SSH" \
        "incus config get '$instance_name' \
         'volatile.tap${second_port_id:0:11}.name'")
    [[ -n "$second_iface" ]]
    remote "$SOURCE_SSH" \
        "incus exec '$instance_name' -- sh -c \
         'printf \"network:\\n  version: 2\\n  ethernets:\\n    $second_iface:\\n      dhcp4: true\\n\" > /etc/netplan/60-secondary.yaml; \
          chmod 600 /etc/netplan/60-secondary.yaml; netplan apply'"
    deadline=$((SECONDS + TIMEOUT))
    until remote "$SOURCE_SSH" \
            "incus exec '$instance_name' -- ip -4 -o addr show dev \
             '$second_iface' | grep -F ' $second_fixed_ip/'" >/dev/null 2>&1; do
        ((SECONDS < deadline)) || {
            echo "Secondary guest DHCP timed out" >&2
            exit 1
        }
        sleep 2
    done
fi

marker="migration-$server_id"
remote "$SOURCE_SSH" \
    "incus exec '$instance_name' -- sh -c \
     'printf %s "$marker" > /root/nova-migration-marker; sync'"

openstack --os-compute-api-version 2.56 server migrate \
    --host "$DEST_HOST" --wait "$server_id"
wait_status VERIFY_RESIZE
[[ "$(remote "$SOURCE_SSH" \
    "incus list '$instance_name' --format csv -c s")" == "STOPPED" ]]
[[ "$(remote "$DEST_SSH" \
    "incus exec '$instance_name' -- cat /root/nova-migration-marker")" == \
    "$marker" ]]
remote "$DEST_SSH" \
    "incus exec '$instance_name' -- ip -4 addr show dev '$guest_iface' | \
     grep -F '$fixed_ip'"
if [[ -n "$second_port_id" ]]; then
    remote "$DEST_SSH" \
        "incus exec '$instance_name' -- ip -4 addr show '$second_iface' | \
         grep -F '$second_fixed_ip'"
    dest_iface=$(remote "$DEST_SSH" \
        "ovs-vsctl --data=bare --no-heading --columns=name find Interface \
         external_ids:iface-id='$second_port_id'")
    [[ -n "$dest_iface" ]]
    [[ "$(remote "$DEST_SSH" \
        "ovs-vsctl get Interface '$dest_iface' external_ids:ovn-installed")" \
        == '"true"' ]]
fi

openstack server resize confirm "$server_id"
wait_status ACTIVE
assert_owner "$DEST_SSH" "$SOURCE_SSH" RUNNING

# Move back, then reject the migration. The retained source must recover.
openstack --os-compute-api-version 2.56 server migrate \
    --host "$SOURCE_HOST" --wait "$server_id"
wait_status VERIFY_RESIZE
[[ "$(remote "$SOURCE_SSH" \
    "incus exec '$instance_name' -- cat /root/nova-migration-marker")" == \
    "$marker" ]]
openstack server resize revert "$server_id"
wait_status ACTIVE
assert_owner "$DEST_SSH" "$SOURCE_SSH" RUNNING
[[ "$(remote "$DEST_SSH" \
    "incus exec '$instance_name' -- cat /root/nova-migration-marker")" == \
    "$marker" ]]
if [[ -n "$second_port_id" ]]; then
    remote "$DEST_SSH" \
        "incus exec '$instance_name' -- ip -4 addr show '$second_iface' | \
         grep -F '$second_fixed_ip'"
fi

trap - EXIT
openstack server delete --wait "$server_id"
! remote "$SOURCE_SSH" "incus info '$instance_name'" >/dev/null 2>&1
! remote "$DEST_SSH" "incus info '$instance_name'" >/dev/null 2>&1
! openstack port show "$port_id" >/dev/null 2>&1
if [[ -n "$second_port_id" ]]; then
    openstack port delete "$second_port_id"
    ! remote "$SOURCE_SSH" \
        "ovs-vsctl --data=bare --no-heading --columns=name find Interface \
         external_ids:iface-id='$second_port_id'" | grep -q .
    ! remote "$DEST_SSH" \
        "ovs-vsctl --data=bare --no-heading --columns=name find Interface \
         external_ids:iface-id='$second_port_id'" | grep -q .
    second_port_id=
fi
! remote "$SOURCE_SSH" \
    "ovs-vsctl --data=bare --no-heading --columns=name find Interface \
     external_ids:iface-id='$port_id'" | grep -q .
! remote "$DEST_SSH" \
    "ovs-vsctl --data=bare --no-heading --columns=name find Interface \
     external_ids:iface-id='$port_id'" | grep -q .

echo "PASS server=$server_id instance=$instance_name ip=$fixed_ip confirm=ok revert=ok"
