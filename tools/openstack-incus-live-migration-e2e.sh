#!/usr/bin/env bash
# Validate conditional CRIU live migration through the native Nova API.

set -euo pipefail

IMAGE=${IMAGE:-alpine-3.21-cloud-incus-criu}
FLAVOR=${FLAVOR:-ds512M}
NETWORK=${NETWORK:-public}
SOURCE_HOST=${SOURCE_HOST:-incus-node-01}
DEST_HOST=${DEST_HOST:-incus-node-02}
SOURCE_SSH=${SOURCE_SSH:-root@10.224.0.21}
DEST_SSH=${DEST_SSH:-root@10.224.0.17}
CONTROLLER_SSH=${CONTROLLER_SSH:-}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
SERVER=${SERVER:-incus-live-migration-e2e-$RANDOM}
TIMEOUT=${TIMEOUT:-300}
INCUS_PROJECT=${INCUS_PROJECT:-nova}

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)
server_id=
instance_name=
port_id=
user_data=

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

incus_remote() {
    local host=$1
    shift
    remote "$host" incus --project "$INCUS_PROJECT" "$@"
}

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

wait_host() {
    local expected=$1
    local deadline=$((SECONDS + TIMEOUT))
    local current
    while ((SECONDS < deadline)); do
        current=$(openstack server show "$server_id" -f value \
            -c OS-EXT-SRV-ATTR:host 2>/dev/null || true)
        [[ "$current" == "$expected" ]] && return 0
        sleep 2
    done
    echo "Server did not move to $expected (current: ${current:-missing})" >&2
    return 1
}

assert_no_ovs_port() {
    local host=$1
    ! remote "$host" \
        "ovs-vsctl --data=bare --no-heading --columns=name find Interface \
         external_ids:iface-id='$port_id'" | grep -q .
}

diagnose() {
    openstack server show "$server_id" 2>/dev/null || true
    openstack server event list "$server_id" 2>/dev/null || true
    if [[ -n "$instance_name" ]]; then
        incus_remote "$SOURCE_SSH" info "$instance_name" 2>/dev/null || true
        incus_remote "$DEST_SSH" info "$instance_name" 2>/dev/null || true
    fi
}

cleanup() {
    local rc=$?
    if [[ -n "$user_data" ]]; then
        rm -f "$user_data"
    fi
    ((rc == 0)) || diagnose
    if [[ -n "$server_id" ]]; then
        openstack server delete --wait "$server_id" >/dev/null 2>&1 || true
    fi
    if [[ -n "$instance_name" ]]; then
        incus_remote "$SOURCE_SSH" delete "$instance_name" --force \
            >/dev/null 2>&1 || true
        incus_remote "$DEST_SSH" delete "$instance_name" --force \
            >/dev/null 2>&1 || true
        incus_remote "$SOURCE_SSH" profile delete "$instance_name" \
            >/dev/null 2>&1 || true
        incus_remote "$DEST_SSH" profile delete "$instance_name" \
            >/dev/null 2>&1 || true
    fi
    return "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for host in "$SOURCE_SSH" "$DEST_SSH"; do
    incus_remote "$host" query /1.0 |
        grep -q migration_stateful_shifted_root
    remote "$host" "podman exec incus criu check --extra" >/dev/null
done

user_data=$(mktemp)
cat >"$user_data" <<'EOF'
#!/bin/sh
cat >/usr/local/bin/criu-counter-loop <<'SCRIPT'
#!/bin/sh
trap 'exit 0' TERM INT
while :; do
    n=$(cat /root/criu-counter 2>/dev/null || echo 0)
    echo $((n + 1)) >/root/criu-counter
    sleep 1
done
SCRIPT
chmod 0755 /usr/local/bin/criu-counter-loop
cat >/etc/init.d/criu-counter <<'SCRIPT'
#!/sbin/openrc-run
name="CRIU live migration E2E counter"
command="/usr/local/bin/criu-counter-loop"
command_background="yes"
pidfile="/run/criu-counter.pid"
output_log="/var/log/criu-counter.log"
error_log="/var/log/criu-counter.err"
SCRIPT
chmod 0755 /etc/init.d/criu-counter
rc-update add criu-counter default
rc-service criu-counter start
echo ready >/root/criu-e2e-ready
EOF

server_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --image "$IMAGE" --network "$NETWORK" \
    --host "$SOURCE_HOST" --user-data "$user_data" \
    -f value -c id "$SERVER")
wait_status ACTIVE
wait_host "$SOURCE_HOST"
instance_name=$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)
port_id=$(openstack port list --server "$server_id" -f value -c ID)
fixed_ip=$(openstack port show "$port_id" -f json -c fixed_ips |
    python3 -c 'import ast,json,sys; v=json.load(sys.stdin)["fixed_ips"]; v=ast.literal_eval(v) if isinstance(v,str) else v; print(v[0]["ip_address"])')

deadline=$((SECONDS + TIMEOUT))
until incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
        test -f /root/criu-e2e-ready; do
    ((SECONDS < deadline)) || {
        echo "Guest bootstrap timed out" >&2
        exit 1
    }
    sleep 2
done

source_pid=$(incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
    cat /run/criu-counter.pid)
source_counter=$(incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
    cat /root/criu-counter)
[[ "$source_pid" =~ ^[0-9]+$ ]]
[[ "$source_counter" =~ ^[0-9]+$ ]]
incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
    sh -c "ps -o pid,ppid | grep -Eq '^ *$source_pid +1$'"

openstack server migrate --live-migration --host "$DEST_HOST" \
    --wait "$server_id"
wait_status ACTIVE
wait_host "$DEST_HOST"

! incus_remote "$SOURCE_SSH" info "$instance_name" >/dev/null 2>&1
[[ "$(incus_remote "$DEST_SSH" list "$instance_name" \
    --format csv -c s)" == RUNNING ]]
dest_pid=$(incus_remote "$DEST_SSH" exec "$instance_name" -- \
    cat /run/criu-counter.pid)
dest_counter=$(incus_remote "$DEST_SSH" exec "$instance_name" -- \
    cat /root/criu-counter)
[[ "$dest_pid" == "$source_pid" ]]
((dest_counter > source_counter))
sleep 3
later_counter=$(incus_remote "$DEST_SSH" exec "$instance_name" -- \
    cat /root/criu-counter)
((later_counter > dest_counter))

guest_iface=$(incus_remote "$DEST_SSH" config get "$instance_name" \
    "volatile.tap${port_id:0:11}.name")
[[ -n "$guest_iface" ]]
incus_remote "$DEST_SSH" exec "$instance_name" -- \
    ip link show "$guest_iface" >/dev/null
dest_ovs_iface=$(remote "$DEST_SSH" \
    "ovs-vsctl --data=bare --no-heading --columns=name find Interface \
     external_ids:iface-id='$port_id'")
[[ -n "$dest_ovs_iface" ]]
[[ "$(remote "$DEST_SSH" \
    "ovs-vsctl get Interface '$dest_ovs_iface' external_ids:ovn-installed")" \
    == '"true"' ]]
[[ "$(openstack port show "$port_id" -f value -c binding_host_id)" == \
    "$DEST_HOST" ]]
[[ "$(openstack port show "$port_id" -f value -c status)" == ACTIVE ]]
assert_no_ovs_port "$SOURCE_SSH"

trap - EXIT
rm -f "$user_data"
openstack server delete --wait "$server_id"
! incus_remote "$SOURCE_SSH" info "$instance_name" >/dev/null 2>&1
! incus_remote "$DEST_SSH" info "$instance_name" >/dev/null 2>&1
! openstack port show "$port_id" >/dev/null 2>&1
assert_no_ovs_port "$SOURCE_SSH"
assert_no_ovs_port "$DEST_SSH"

echo "PASS server=$server_id instance=$instance_name ip=$fixed_ip pid=$source_pid counter=$source_counter->$later_counter"
