#!/usr/bin/env bash
# Validate conditional CRIU live migration through the native Nova API.

set -Eeuo pipefail

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
KEEP_FAILED=${KEEP_FAILED:-0}
WITH_DATA_VOLUME=${WITH_DATA_VOLUME:-0}
DATA_VOLUME_TYPE=${DATA_VOLUME_TYPE:-ceph}
DATA_VOLUME_SIZE=${DATA_VOLUME_SIZE:-1}
DATA_DEVICE=${DATA_DEVICE:-/dev/vdb}

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)
server_id=
instance_name=
port_id=
user_data=
volume_id=
volume_marker="INCUS_LIVE_VOLUME_${RANDOM}_$(date +%s)"

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

wait_migration() {
    local deadline=$((SECONDS + TIMEOUT))
    local status
    while ((SECONDS < deadline)); do
        status=$(openstack server migration list --server "$server_id" \
            -f value -c Status 2>/dev/null | head -n1 || true)
        case "${status,,}" in
            completed)
                return 0
                ;;
            failed|error)
                openstack server migration list --server "$server_id" \
                    || true
                echo "Live migration failed (status: $status)" >&2
                return 1
                ;;
        esac
        sleep 2
    done
    echo "Live migration status timed out (current: ${status:-missing})" >&2
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

on_error() {
    local rc=$?
    local line=$1
    local command=$2
    printf 'FAIL rc=%s line=%s command=%s\n' \
        "$rc" "$line" "$command" >&2
    return "$rc"
}

cleanup() {
    local rc=$?
    if [[ -n "$user_data" ]]; then
        rm -f "$user_data"
    fi
    ((rc == 0)) || diagnose
    if ((rc != 0)) && [[ "$KEEP_FAILED" == "1" ]]; then
        printf 'Keeping failed resources for diagnosis: server=%s instance=%s port=%s\n' \
            "${server_id:-unset}" "${instance_name:-unset}" \
            "${port_id:-unset}" >&2
        return "$rc"
    fi
    if [[ -n "$server_id" ]]; then
        openstack server delete --wait "$server_id" >/dev/null 2>&1 || true
    fi
    if [[ -n "$volume_id" ]]; then
        openstack volume delete "$volume_id" >/dev/null 2>&1 || true
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
trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for host in "$SOURCE_SSH" "$DEST_SSH"; do
    remote "$host" incus query /1.0 |
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

if [[ "$WITH_DATA_VOLUME" == "1" ]]; then
    volume_id=$(openstack volume create \
        --type "$DATA_VOLUME_TYPE" --size "$DATA_VOLUME_SIZE" \
        -f value -c id "${SERVER}-data")
    openstack server add volume --device "$DATA_DEVICE" \
        "$server_id" "$volume_id"
    deadline=$((SECONDS + TIMEOUT))
    until [[ "$(openstack volume show "$volume_id" -f value -c status)" == \
            "in-use" ]]; do
        ((SECONDS < deadline)) || {
            echo "Cinder data volume attachment timed out" >&2
            exit 1
        }
        sleep 2
    done
    # Nova may normalize the requested virtio-style name to its canonical
    # SCSI BDM name. Always use the attachment record as the authority.
    DATA_DEVICE=$(openstack server volume list "$server_id" -f json |
        python3 -c 'import json,sys
volume_id=sys.argv[1]
rows=json.load(sys.stdin)
print(next(row["Device"] for row in rows
           if row["Volume ID"] == volume_id))' "$volume_id")
    until incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
            test -b "$DATA_DEVICE"; do
        ((SECONDS < deadline)) || {
            echo "Cinder data device did not appear in the container" >&2
            exit 1
        }
        sleep 2
    done
    printf '%s' "$volume_marker" |
        incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
            dd of="$DATA_DEVICE" bs=1 conv=fsync status=none
fi

source_pid=$(incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
    cat /run/criu-counter.pid)
source_counter=$(incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
    cat /root/criu-counter)
[[ "$source_pid" =~ ^[0-9]+$ ]]
[[ "$source_counter" =~ ^[0-9]+$ ]]

openstack server migrate --live-migration --host "$DEST_HOST" \
    --wait "$server_id"
wait_migration
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

if [[ "$WITH_DATA_VOLUME" == "1" ]]; then
    [[ "$(openstack volume show "$volume_id" -f value -c status)" == \
        "in-use" ]]
    incus_remote "$DEST_SSH" exec "$instance_name" -- \
        test -b "$DATA_DEVICE"
    restored_marker=$(incus_remote "$DEST_SSH" exec "$instance_name" -- \
        dd if="$DATA_DEVICE" bs=1 count="${#volume_marker}" status=none)
    [[ "$restored_marker" == "$volume_marker" ]]
    target_volume_source=$(incus_remote "$DEST_SSH" profile device get \
        "$instance_name" "$volume_id" source)
    [[ "$target_volume_source" == /dev/* ]]
    remote "$DEST_SSH" test -b "$target_volume_source"
fi

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
if [[ -n "$volume_id" ]]; then
    deadline=$((SECONDS + TIMEOUT))
    until [[ "$(openstack volume show "$volume_id" -f value -c status)" == \
            "available" ]]; do
        ((SECONDS < deadline)) || {
            echo "Cinder data volume did not detach after server delete" >&2
            exit 1
        }
        sleep 2
    done
    openstack volume delete "$volume_id"
fi
! incus_remote "$SOURCE_SSH" info "$instance_name" >/dev/null 2>&1
! incus_remote "$DEST_SSH" info "$instance_name" >/dev/null 2>&1
! openstack port show "$port_id" >/dev/null 2>&1
assert_no_ovs_port "$SOURCE_SSH"
assert_no_ovs_port "$DEST_SSH"

echo "PASS server=$server_id instance=$instance_name ip=$fixed_ip pid=$source_pid counter=$source_counter->$later_counter volume=${volume_id:-none}"
