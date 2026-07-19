#!/usr/bin/env bash
# Validate conditional CRIU live migration through the native Nova API.

set -Eeuo pipefail

IMAGE=${IMAGE:-alpine-3.21-cloud-incus-criu-bfv-raw}
FLAVOR=${FLAVOR:-ds512M}
NETWORK=${NETWORK:-public}
SOURCE_HOST=${SOURCE_HOST:-incus-node-01}
DEST_HOST=${DEST_HOST:-incus-node-02}
SOURCE_SSH=${SOURCE_SSH:-root@10.224.0.21}
DEST_SSH=${DEST_SSH:-root@10.224.0.17}
# Ordered host=ssh pairs. For example:
# incus-node-02=root@10.224.0.17,incus-node-03=root@10.224.0.22,incus-node-01=root@10.224.0.21
MIGRATION_TARGETS=${MIGRATION_TARGETS:-$DEST_HOST=$DEST_SSH}
CONTROLLER_SSH=${CONTROLLER_SSH:-}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
SERVER=${SERVER:-incus-live-migration-e2e-$RANDOM}
TIMEOUT=${TIMEOUT:-300}
INCUS_PROJECT=${INCUS_PROJECT:-nova}
KEEP_FAILED=${KEEP_FAILED:-0}
WITH_DATA_VOLUME=${WITH_DATA_VOLUME:-0}
BOOT_FROM_VOLUME=${BOOT_FROM_VOLUME:-0}
ROOT_VOLUME_TYPE=${ROOT_VOLUME_TYPE:-ceph}
ROOT_VOLUME_SIZE=${ROOT_VOLUME_SIZE:-2}
DATA_VOLUME_TYPE=${DATA_VOLUME_TYPE:-ceph}
DATA_VOLUME_SIZE=${DATA_VOLUME_SIZE:-1}
DATA_DEVICE=${DATA_DEVICE:-/dev/vdb}
DATA_VOLUME_COUNT=${DATA_VOLUME_COUNT:-1}
DATA_DEVICES=${DATA_DEVICES:-$DATA_DEVICE}
MANILA_SHARE=${MANILA_SHARE:-}
MANILA_TAG=${MANILA_TAG:-tenant-data}
INJECT_RESTORE_FAILURE=${INJECT_RESTORE_FAILURE:-0}
E2E_LOCK_FILE=${E2E_LOCK_FILE:-/run/lock/openstack-incus-live-migration-e2e.lock}

exec 9>"$E2E_LOCK_FILE"
if ! flock -n 9; then
    echo "Another Incus live-migration E2E owns $E2E_LOCK_FILE" >&2
    exit 2
fi

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)
server_id=
instance_name=
port_id=
user_data=
root_volume_id=
share_id=
shares_url=
token=
manila_marker_path=
volume_ids=()
volume_devices=()
volume_markers=()
restore_failpoint_ssh=
read -r -a requested_data_devices <<<"$DATA_DEVICES"
IFS=',' read -r -a migration_targets <<<"$MIGRATION_TARGETS"
test_sshs=("$SOURCE_SSH")
for target in "${migration_targets[@]}"; do
    [[ "$target" == *=* ]] || {
        echo "MIGRATION_TARGETS entries must use host=ssh syntax" >&2
        exit 2
    }
    test_sshs+=("${target#*=}")
done

remote() {
    local host=$1
    shift
    "${SSH[@]}" "$host" "$@"
}

clear_restore_failpoint() {
    local host=$1
    local _
    for _ in {1..10}; do
        if remote "$host" \
                "podman exec incus umount /usr/local/sbin/criu" \
                >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    # A failed CRIU exec can retain a transient executable reference. A lazy
    # detach removes the test mount from the namespace immediately and lets
    # the kernel release the old reference when that process exits.
    remote "$host" \
        "podman exec incus umount -l /usr/local/sbin/criu" \
        >/dev/null 2>&1
}

assert_fails() {
    local description=$1
    shift
    if "$@"; then
        echo "Expected failure: $description" >&2
        return 1
    fi
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
    local command_line
    printf -v command_line '%q ' incus --project "$INCUS_PROJECT" "$@"
    "${SSH[@]}" "$host" "$command_line"
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

share_api() {
    local method=$1 url=$2 data=${3:-}
    local args=(-fsS -X "$method" -H "X-Auth-Token: $token"
        -H "OpenStack-API-Version: compute 2.97")
    if [[ -n "$data" ]]; then
        args+=(-H "Content-Type: application/json" -d "$data")
    fi
    curl "${args[@]}" "$url"
}

wait_share_status() {
    local expected=$1
    local deadline=$((SECONDS + TIMEOUT))
    local body
    while ((SECONDS < deadline)); do
        body=$(share_api GET "$shares_url")
        if python3 -c \
                'import json,sys
data=json.load(sys.stdin)
share_id,expected=sys.argv[1:]
raise SystemExit(0 if any(
    item["share_id"] == share_id and item["status"] == expected
    for item in data["shares"]) else 1)' \
                "$share_id" "$expected" <<<"$body"; then
            return 0
        fi
        sleep 2
    done
    echo "Manila share did not reach $expected: $body" >&2
    return 1
}

wait_share_absent() {
    local deadline=$((SECONDS + TIMEOUT))
    while ((SECONDS < deadline)); do
        if ! share_api GET "$shares_url" | grep -Fq "$share_id"; then
            return 0
        fi
        sleep 2
    done
    echo "Manila share mapping did not disappear: $share_id" >&2
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
        local host
        for host in "${test_sshs[@]}"; do
            incus_remote "$host" info "$instance_name" 2>/dev/null || true
        done
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
    # EXIT cleanup must continue after a diagnostic or best-effort deletion
    # fails; errexit would otherwise strand the very resources being audited.
    set +e
    if [[ -n "$restore_failpoint_ssh" ]]; then
        clear_restore_failpoint "$restore_failpoint_ssh" || true
        restore_failpoint_ssh=
    fi
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
        if [[ -n "$share_id" && -n "$shares_url" ]]; then
            openstack server stop "$server_id" >/dev/null 2>&1 || true
            share_api DELETE "$shares_url/$share_id" \
                >/dev/null 2>&1 || true
        fi
        openstack server delete --wait "$server_id" >/dev/null 2>&1 || true
    fi
    local volume_id volume_status deadline
    for volume_id in "${volume_ids[@]}"; do
        deadline=$((SECONDS + 60))
        while volume_status=$(openstack volume show "$volume_id" \
                -f value -c status 2>/dev/null); do
            [[ "$volume_status" == "available" || "$volume_status" == "error" ]] && break
            ((SECONDS >= deadline)) && break
            sleep 2
        done
        openstack volume delete "$volume_id" >/dev/null 2>&1 || true
    done
    if [[ -n "$root_volume_id" ]]; then
        deadline=$((SECONDS + 60))
        while volume_status=$(openstack volume show "$root_volume_id" \
                -f value -c status 2>/dev/null); do
            [[ "$volume_status" == "available" || "$volume_status" == "error" ]] && break
            ((SECONDS >= deadline)) && break
            sleep 2
        done
        openstack volume delete "$root_volume_id" >/dev/null 2>&1 || true
    fi
    if [[ -n "$instance_name" ]]; then
        local host
        for host in "${test_sshs[@]}"; do
            incus_remote "$host" delete "$instance_name" --force \
                >/dev/null 2>&1 || true
            incus_remote "$host" profile delete "$instance_name" \
                >/dev/null 2>&1 || true
        done
    fi
    return "$rc"
}
trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for host in "${test_sshs[@]}"; do
    remote "$host" incus query /1.0 |
        grep -q migration_stateful_shifted_root
    if [[ "$BOOT_FROM_VOLUME" == "1" ]]; then
        remote "$host" incus query /1.0 |
            grep -q migration_live_shared_cephext_storage
    fi
    # Recover from a test process killed before its EXIT trap could remove the
    # restore failpoint. On an unmodified mount this harmlessly returns EINVAL.
    clear_restore_failpoint "$host" || true
    remote "$host" "podman exec incus criu check --extra" >/dev/null
done

user_data=$(mktemp)
cat >"$user_data" <<'EOF'
#!/bin/sh
cat >/usr/local/bin/criu-counter-loop <<'SCRIPT'
#!/bin/sh
echo $$ >/run/criu-counter.pid
trap 'exit 0' TERM INT
echo $$ >/run/criu-counter.pid
while :; do
    n=$(cat /root/criu-counter 2>/dev/null || echo 0)
    echo $((n + 1)) >/root/criu-counter
    sleep 1
done
SCRIPT
chmod 0755 /usr/local/bin/criu-counter-loop
if command -v rc-update >/dev/null 2>&1; then
cat >/etc/init.d/criu-counter <<'SCRIPT'
#!/sbin/openrc-run
name="CRIU live migration E2E counter"
command="/usr/local/bin/criu-counter-loop"
command_background="yes"
pidfile="/run/criu-counter.pid"
output_log="/var/log/criu-counter.log"
error_log="/var/log/criu-counter.err"
depend() {
    before networking
}
SCRIPT
chmod 0755 /etc/init.d/criu-counter
rc-update add criu-counter boot
rc-service criu-counter start
else
cat >/etc/systemd/system/criu-counter.service <<'UNIT'
[Unit]
Description=CRIU live migration E2E counter

[Service]
ExecStart=/usr/local/bin/criu-counter-loop
Restart=always

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now criu-counter.service
fi
echo ready >/root/criu-e2e-ready
EOF

if [[ "$BOOT_FROM_VOLUME" == "1" ]]; then
    root_volume_id=$(openstack volume create \
        --type "$ROOT_VOLUME_TYPE" --size "$ROOT_VOLUME_SIZE" \
        --image "$IMAGE" --bootable -f value -c id "${SERVER}-root")
    deadline=$((SECONDS + TIMEOUT))
    until [[ "$(openstack volume show "$root_volume_id" \
            -f value -c status)" == "available" ]]; do
        ((SECONDS < deadline)) || {
            echo "Cinder BFV root creation timed out" >&2
            exit 1
        }
        sleep 2
    done
    server_id=$(openstack --os-compute-api-version 2.74 server create \
        --flavor "$FLAVOR" --volume "$root_volume_id" --network "$NETWORK" \
        --host "$SOURCE_HOST" --user-data "$user_data" \
        -f value -c id "$SERVER")
else
    server_id=$(openstack --os-compute-api-version 2.74 server create \
        --flavor "$FLAVOR" --image "$IMAGE" --network "$NETWORK" \
        --host "$SOURCE_HOST" --user-data "$user_data" \
        -f value -c id "$SERVER")
fi
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

if [[ -n "$MANILA_SHARE" ]]; then
    share_id=$(openstack share show "$MANILA_SHARE" -f value -c id)
    token=$(openstack token issue -f value -c id)
    endpoint=$(openstack endpoint list --service nova --interface public \
        -f value -c URL | head -n1)
    project_id=$(openstack server show "$server_id" -f value -c project_id)
    endpoint=${endpoint//\%\(project_id\)s/$project_id}
    shares_url="$endpoint/servers/$server_id/shares"

    openstack server stop "$server_id"
    wait_status SHUTOFF
    share_api POST "$shares_url" \
        "{\"share\":{\"share_id\":\"$share_id\",\"tag\":\"$MANILA_TAG\"}}" \
        >/dev/null
    wait_share_status inactive
    openstack server start "$server_id"
    wait_status ACTIVE

    deadline=$((SECONDS + TIMEOUT))
    until incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
            test -s /run/criu-counter.pid; do
        ((SECONDS < deadline)) || {
            echo "CRIU counter did not restart after Manila staging" >&2
            exit 1
        }
        sleep 2
    done
    until incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
            grep -Fq "/mnt/manila/$MANILA_TAG" /proc/self/mountinfo; do
        ((SECONDS < deadline)) || {
            echo "Manila share did not appear in the source container" >&2
            exit 1
        }
        sleep 2
    done
    manila_marker="MANILA_LIVE_${RANDOM}_$(date +%s)"
    manila_marker_path="/mnt/manila/$MANILA_TAG/live-marker-$server_id"
    until incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
            sh -c "printf '%s' '$manila_marker' >'$manila_marker_path'"; do
        ((SECONDS < deadline)) || {
            echo "Manila share was mounted but not writable" >&2
            exit 1
        }
        sleep 2
    done
    [[ "$(incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
        cat "$manila_marker_path")" == "$manila_marker" ]]
fi

if [[ "$WITH_DATA_VOLUME" == "1" ]]; then
    [[ "$DATA_VOLUME_COUNT" =~ ^[1-9][0-9]*$ ]]
    ((${#requested_data_devices[@]} >= DATA_VOLUME_COUNT)) || {
        echo "DATA_DEVICES has fewer entries than DATA_VOLUME_COUNT" >&2
        exit 1
    }
    for ((index = 0; index < DATA_VOLUME_COUNT; index++)); do
        volume_id=$(openstack volume create \
            --type "$DATA_VOLUME_TYPE" --size "$DATA_VOLUME_SIZE" \
            -f value -c id "${SERVER}-data-$((index + 1))")
        volume_ids+=("$volume_id")
        openstack server add volume \
            --device "${requested_data_devices[index]}" \
            "$server_id" "$volume_id"
        deadline=$((SECONDS + TIMEOUT))
        until [[ "$(openstack volume show "$volume_id" \
                -f value -c status)" == "in-use" ]]; do
            ((SECONDS < deadline)) || {
                echo "Cinder data volume attachment timed out: $volume_id" \
                    >&2
                exit 1
            }
            sleep 2
        done
        # Nova may normalize a requested virtio-style name to its canonical
        # SCSI BDM name. Always use the attachment record as the authority.
        data_device=$(openstack server volume list "$server_id" -f json |
            python3 -c 'import json,sys
volume_id=sys.argv[1]
rows=json.load(sys.stdin)
print(next(row["Device"] for row in rows
           if row["Volume ID"] == volume_id))' "$volume_id")
        volume_devices+=("$data_device")
        volume_marker="INCUS_LIVE_VOLUME_${index}_${RANDOM}_$(date +%s)"
        volume_markers+=("$volume_marker")
        until incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
                test -b "$data_device"; do
            ((SECONDS < deadline)) || {
                echo "Cinder data device did not appear: $data_device" >&2
                exit 1
            }
            sleep 2
        done
        incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
            sh -c "printf '%s' '$volume_marker' | \
                dd of='$data_device' bs=1 conv=fsync status=none"
    done
fi

source_pid=$(incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
    cat /run/criu-counter.pid)
source_counter=$(incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
    cat /root/criu-counter)
[[ "$source_pid" =~ ^[0-9]+$ ]]
[[ "$source_counter" =~ ^[0-9]+$ ]]

current_host=$SOURCE_HOST
current_ssh=$SOURCE_SSH
current_counter=$source_counter
later_counter=$source_counter

inject_and_verify_restore_rollback() {
    local target_host=$1 target_ssh=$2
    local rollback_pid rollback_counter volume_id image_name
    local deadline status share_mount injection_since
    local source_migration_log target_migration_log

    injection_since=$(( $(date +%s) - 2 ))
    restore_failpoint_ssh=$target_ssh
    remote "$target_ssh" \
        "podman exec incus mount --bind /bin/false /usr/local/sbin/criu"
    # OSC can return zero when Nova accepted the request and subsequently
    # rolled it back. The migration record, not the client exit status, is
    # authoritative for the injected failure.
    openstack server migrate --live-migration --host "$target_host" \
        --wait "$server_id" || true
    clear_restore_failpoint "$target_ssh"
    restore_failpoint_ssh=
    deadline=$((SECONDS + TIMEOUT))
    while ((SECONDS < deadline)); do
        status=$(openstack server migration list --server "$server_id" \
            -f value -c Status 2>/dev/null | head -n1 || true)
        [[ "${status,,}" == failed || "${status,,}" == error ]] && break
        sleep 2
    done
    [[ "${status,,}" == failed || "${status,,}" == error ]]

    source_migration_log=$(remote "$current_ssh" journalctl \
        -u incus-podman.service --since "@$injection_since" --no-pager)
    target_migration_log=$(remote "$target_ssh" journalctl \
        -u incus-podman.service --since "@$injection_since" --no-pager)
    grep -Fq "Failed migration on target" <<<"$target_migration_log"
    # The source logs the target's restore failure after it is propagated over
    # the migration control channel.  This is expected and proves that the
    # source did not independently fail before the target attempted restore.
    grep -F "Failed migration on source" <<<"$source_migration_log" |
        grep -Fq "Error from migration control target"
    if grep -Fq "Failed reading migration index header" \
            <<<"$target_migration_log"; then
        echo "Failure injection did not reach target CRIU restore" >&2
        return 1
    fi

    wait_status ACTIVE
    wait_host "$current_host"
    [[ "$(incus_remote "$current_ssh" list "$instance_name" \
        --format csv -c s)" == RUNNING ]]
    assert_fails "target instance must be absent after rollback" \
        incus_remote "$target_ssh" info "$instance_name" >/dev/null 2>&1

    rollback_pid=$(incus_remote "$current_ssh" exec "$instance_name" -- \
        cat /run/criu-counter.pid)
    rollback_counter=$(incus_remote "$current_ssh" exec "$instance_name" -- \
        cat /root/criu-counter)
    [[ "$rollback_pid" == "$source_pid" ]]
    ((rollback_counter > current_counter))
    current_counter=$rollback_counter

    [[ "$(openstack port show "$port_id" -f value \
        -c binding_host_id)" == "$current_host" ]]
    [[ "$(openstack port show "$port_id" -f value -c status)" == ACTIVE ]]
    assert_no_ovs_port "$target_ssh"

    if [[ "$BOOT_FROM_VOLUME" == "1" ]]; then
        image_name="volume-$root_volume_id"
        remote "$current_ssh" \
            "rbd device list --format json --id cinder | jq -e \
             '.[] | select(.name == \"$image_name\")'" >/dev/null
        assert_fails "target BFV mapping must be absent after rollback" \
            remote "$target_ssh" \
            "rbd device list --format json --id cinder | jq -e \
             '.[] | select(.name == \"$image_name\")'" >/dev/null
    fi
    for volume_id in "${volume_ids[@]}"; do
        image_name="volume-$volume_id"
        remote "$current_ssh" \
            "rbd device list --format json --id cinder | jq -e \
             '.[] | select(.name == \"$image_name\")'" >/dev/null
        assert_fails "target data-volume mapping must be absent after rollback" \
            remote "$target_ssh" \
            "rbd device list --format json --id cinder | jq -e \
             '.[] | select(.name == \"$image_name\")'" >/dev/null
    done
    if [[ -n "$share_id" ]]; then
        share_mount="/opt/stack/data/nova/instances/incus-shares/$server_id/$share_id"
        remote "$current_ssh" findmnt -rn "$share_mount" >/dev/null
        assert_fails "target Manila staging mount must be absent after rollback" \
            remote "$target_ssh" findmnt -rn "$share_mount" >/dev/null
    fi
    echo "PASS injected live-restore failure rolled back to $current_host"
}

migrate_and_verify() {
    local target_host=$1 target_ssh=$2
    local dest_pid dest_counter index volume_id data_device volume_marker
    local restored_marker target_volume_source restored_manila_marker
    local guest_iface dest_ovs_iface root_image

    openstack server migrate --live-migration --host "$target_host" \
        --wait "$server_id"
    wait_migration
    wait_status ACTIVE
    wait_host "$target_host"

    assert_fails "source instance must be absent after migration" \
        incus_remote "$current_ssh" info "$instance_name" >/dev/null 2>&1
    [[ "$(incus_remote "$target_ssh" list "$instance_name" \
        --format csv -c s)" == RUNNING ]]
    dest_pid=$(incus_remote "$target_ssh" exec "$instance_name" -- \
        cat /run/criu-counter.pid)
    dest_counter=$(incus_remote "$target_ssh" exec "$instance_name" -- \
        cat /root/criu-counter)
    [[ "$dest_pid" == "$source_pid" ]]
    ((dest_counter > current_counter))

    if [[ "$BOOT_FROM_VOLUME" == "1" ]]; then
        [[ "$(openstack volume show "$root_volume_id" -f value -c status)" == \
            "in-use" ]]
        root_image="volume-$root_volume_id"
        assert_fails "source BFV mapping must be absent after migration" \
            remote "$current_ssh" \
            "rbd device list --format json --id cinder | jq -e \
             '.[] | select(.name == \"$root_image\")'" >/dev/null
        remote "$target_ssh" \
            "rbd device list --format json --id cinder | jq -e \
             '.[] | select(.name == \"$root_image\")'" >/dev/null
    fi
    sleep 3
    later_counter=$(incus_remote "$target_ssh" exec "$instance_name" -- \
        cat /root/criu-counter)
    ((later_counter > dest_counter))

    if [[ "$WITH_DATA_VOLUME" == "1" ]]; then
        for index in "${!volume_ids[@]}"; do
            volume_id=${volume_ids[index]}
            data_device=${volume_devices[index]}
            volume_marker=${volume_markers[index]}
            [[ "$(openstack volume show "$volume_id" -f value -c status)" == \
                "in-use" ]]
            incus_remote "$target_ssh" exec "$instance_name" -- \
                test -b "$data_device"
            restored_marker=$(incus_remote "$target_ssh" \
                exec "$instance_name" -- \
                dd if="$data_device" bs=1 count="${#volume_marker}" \
                status=none)
            [[ "$restored_marker" == "$volume_marker" ]]
            target_volume_source=$(incus_remote "$target_ssh" \
                profile device get "$instance_name" "$volume_id" source)
            [[ "$target_volume_source" == /dev/* ]]
            remote "$target_ssh" test -b "$target_volume_source"
        done
    fi

    if [[ -n "$share_id" ]]; then
        restored_manila_marker=$(incus_remote "$target_ssh" \
            exec "$instance_name" -- cat "$manila_marker_path")
        [[ "$restored_manila_marker" == "$manila_marker" ]]
        share_mount="/opt/stack/data/nova/instances/incus-shares/$server_id/$share_id"
        remote "$target_ssh" findmnt -rn "$share_mount" >/dev/null
        assert_fails "source Manila staging mount must be absent after migration" \
            remote "$current_ssh" findmnt -rn "$share_mount" >/dev/null
    fi

    guest_iface=$(incus_remote "$target_ssh" config get "$instance_name" \
        "volatile.tap${port_id:0:11}.name")
    [[ -n "$guest_iface" ]]
    incus_remote "$target_ssh" exec "$instance_name" -- \
        ip link show "$guest_iface" >/dev/null
    dest_ovs_iface=$(remote "$target_ssh" \
        "ovs-vsctl --data=bare --no-heading --columns=name find Interface \
         external_ids:iface-id='$port_id'")
    [[ -n "$dest_ovs_iface" ]]
    [[ "$(remote "$target_ssh" \
        "ovs-vsctl get Interface '$dest_ovs_iface' \
         external_ids:ovn-installed")" == '"true"' ]]
    [[ "$(openstack port show "$port_id" -f value -c binding_host_id)" == \
        "$target_host" ]]
    [[ "$(openstack port show "$port_id" -f value -c status)" == ACTIVE ]]
    assert_no_ovs_port "$current_ssh"

    current_host=$target_host
    current_ssh=$target_ssh
    current_counter=$later_counter
}

if [[ "$INJECT_RESTORE_FAILURE" == "1" ]]; then
    inject_and_verify_restore_rollback \
        "${migration_targets[0]%%=*}" "${migration_targets[0]#*=}"
fi

for target in "${migration_targets[@]}"; do
    migrate_and_verify "${target%%=*}" "${target#*=}"
done

trap - EXIT
rm -f "$user_data"
if [[ -n "$share_id" ]]; then
    incus_remote "$current_ssh" exec "$instance_name" -- \
        rm -f "$manila_marker_path"
    openstack server stop "$server_id"
    wait_status SHUTOFF
    share_api DELETE "$shares_url/$share_id" >/dev/null
    wait_share_absent
    for host in "${test_sshs[@]}"; do
        assert_fails "Manila staging mount must be absent after detach" \
            remote "$host" findmnt -rn "$share_mount" >/dev/null
    done
fi
openstack server delete --wait "$server_id"
for volume_id in "${volume_ids[@]}"; do
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
done
if [[ -n "$root_volume_id" ]]; then
    deadline=$((SECONDS + TIMEOUT))
    until [[ "$(openstack volume show "$root_volume_id" \
            -f value -c status)" == "available" ]]; do
        ((SECONDS < deadline)) || {
            echo "Cinder root volume did not detach after server delete" >&2
            exit 1
        }
        sleep 2
    done
    openstack volume delete "$root_volume_id"
fi
assert_fails "Neutron port must be absent after server delete" \
    openstack port show "$port_id" >/dev/null 2>&1
for host in "${test_sshs[@]}"; do
    assert_fails "Incus instance must be absent after server delete" \
        incus_remote "$host" info "$instance_name" >/dev/null 2>&1
    assert_no_ovs_port "$host"
done

echo "PASS server=$server_id instance=$instance_name ip=$fixed_ip pid=$source_pid counter=$source_counter->$later_counter hops=${#migration_targets[@]} root_volume=${root_volume_id:-local} data_volumes=${volume_ids[*]:-none} manila_share=${share_id:-none}"
