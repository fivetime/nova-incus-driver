#!/usr/bin/env bash
# Public-API E2E for a Cinder data volume attached in the initial server BDM.
#
# Required: RUN_DESTRUCTIVE=true IMAGE=... FLAVOR=... NETWORK=...
# Optional: ROOT_MODE=local|bfv DATA_VOLUME_COUNT=1 VOLUME_TYPE=...
#           ROOT_VOLUME_TYPE=... ROOT_VOLUME_SIZE=20 VOLUME_SIZE=1
#           DATA_DEVICES="/dev/vdb /dev/vdc" TIMEOUT=600 NAME=...
#           EVIDENCE_FILE=/secure/path/case.json
#
# The guest image must provide cloud-init, blkid, mkfs.ext4, fuse2fs, a
# fusermount helper, and either systemd or OpenRC local services. Nova console
# output must be enabled. The test never mounts tenant ext4 with the host
# kernel.

set -Eeuo pipefail

RUN_DESTRUCTIVE=${RUN_DESTRUCTIVE:-false}
if [[ "$RUN_DESTRUCTIVE" != true ]]; then
    echo "Refusing destructive E2E; set RUN_DESTRUCTIVE=true" >&2
    exit 2
fi

IMAGE=${IMAGE:?Set IMAGE to an admitted Incus system-container image}
FLAVOR=${FLAVOR:?Set FLAVOR to an Incus-compatible flavor}
NETWORK=${NETWORK:?Set NETWORK to a tenant network}
ROOT_MODE=${ROOT_MODE:-local}
DATA_VOLUME_COUNT=${DATA_VOLUME_COUNT:-1}
VOLUME_TYPE=${VOLUME_TYPE:-}
ROOT_VOLUME_TYPE=${ROOT_VOLUME_TYPE:-$VOLUME_TYPE}
ROOT_VOLUME_SIZE=${ROOT_VOLUME_SIZE:-20}
VOLUME_SIZE=${VOLUME_SIZE:-1}
DEVICE=${DEVICE:-/dev/vdb}
DATA_DEVICES=${DATA_DEVICES:-}
TIMEOUT=${TIMEOUT:-600}
NAME=${NAME:-incus-initial-data-volume-e2e-$RANDOM}
EVIDENCE_FILE=${EVIDENCE_FILE:-}
# Stateful Incus containers exclude the console device (CRIU cannot restore
# the console PTY), so Nova console output is empty for them. When
# HOST_SSH_MAP maps hypervisor hostnames to SSH targets (host=user@addr,...),
# markers are also read from the guest journal file via incus exec on the
# instance's compute host. Console output remains the primary channel.
HOST_SSH_MAP=${HOST_SSH_MAP:-}
SSH_IDENTITY=${SSH_IDENTITY:-}
SSH_KNOWN_HOSTS_FILE=${SSH_KNOWN_HOSTS_FILE:-$HOME/.ssh/known_hosts}
GUEST_MARKER_LOG=/var/log/openstack-incus-data-e2e.log

command -v openstack >/dev/null
command -v python3 >/dev/null
[[ "$VOLUME_SIZE" =~ ^[1-9][0-9]*$ ]] || {
    echo "VOLUME_SIZE must be a positive integer in GiB" >&2
    exit 2
}
[[ "$ROOT_VOLUME_SIZE" =~ ^[1-9][0-9]*$ ]] || {
    echo "ROOT_VOLUME_SIZE must be a positive integer in GiB" >&2
    exit 2
}
[[ "$DATA_VOLUME_COUNT" =~ ^[0-9]+$ ]] || {
    echo "DATA_VOLUME_COUNT must be a non-negative integer" >&2
    exit 2
}
[[ "$ROOT_MODE" == local || "$ROOT_MODE" == bfv ]] || {
    echo "ROOT_MODE must be local or bfv" >&2
    exit 2
}
if ((DATA_VOLUME_COUNT > 0)) && [[ -z "$VOLUME_TYPE" ]]; then
    echo "VOLUME_TYPE is required when DATA_VOLUME_COUNT is non-zero" >&2
    exit 2
fi
if [[ "$ROOT_MODE" == bfv && -z "$ROOT_VOLUME_TYPE" ]]; then
    echo "ROOT_VOLUME_TYPE or VOLUME_TYPE is required for BFV" >&2
    exit 2
fi

declare -a devices=()
if ((DATA_VOLUME_COUNT > 0)); then
    if [[ -n "$DATA_DEVICES" ]]; then
        read -r -a devices <<<"$DATA_DEVICES"
    elif ((DATA_VOLUME_COUNT == 1)); then
        devices=("$DEVICE")
    else
        for ((index = 0; index < DATA_VOLUME_COUNT; index++)); do
            octal=$(printf '%03o' "$((98 + index))")
            printf -v suffix '%b' "\\$octal"
            devices+=("/dev/vd$suffix")
        done
    fi
fi
if ((${#devices[@]} != DATA_VOLUME_COUNT)); then
    echo "DATA_DEVICES must contain exactly DATA_VOLUME_COUNT paths" >&2
    exit 2
fi
for device in "${devices[@]}"; do
    [[ "$device" =~ ^/dev/(vd|sd|xvd)[a-z]+$ ]] || {
        echo "Invalid Nova data-disk path: $device" >&2
        exit 2
    }
done

server_id=
server_uuid=
instance_name=
root_volume_id=
declare -a volume_ids=()
pass_message=
user_data=$(mktemp)
token="initial-data-$(date +%s)-$RANDOM-$$"
first_marker="OPENSTACK_INCUS_DATA_FIRST_OK:$token"
reboot_marker="OPENSTACK_INCUS_DATA_REBOOT_OK:$token"

wait_field() {
    local expected=$1
    shift
    local deadline=$((SECONDS + TIMEOUT)) current=
    while ((SECONDS < deadline)); do
        current=$("$@" 2>/dev/null || true)
        [[ "$current" == "$expected" ]] && return 0
        [[ "$current" == ERROR || "$current" == error* ]] && break
        sleep 2
    done
    echo "Timed out waiting for $expected (current: ${current:-missing})" >&2
    return 1
}

resource_exists_exact() {
    local resource=$1 resource_id=$2 ids=
    local -a show_command list_command
    case "$resource" in
        server)
            show_command=(openstack server show "$resource_id")
            list_command=(openstack server list -f value -c ID)
            ;;
        volume)
            show_command=(openstack volume show "$resource_id")
            list_command=(openstack volume list -f value -c ID)
            ;;
        *)
            echo "Unsupported resource type: $resource" >&2
            return 2
            ;;
    esac
    "${show_command[@]}" >/dev/null 2>&1 && return 0
    if ! ids=$("${list_command[@]}" 2>/dev/null); then
        return 2
    fi
    grep -Fqx -- "$resource_id" <<<"$ids" && return 0
    return 1
}

wait_absent() {
    local resource=$1 resource_id=$2
    local deadline=$((SECONDS + TIMEOUT)) lookup_status=
    while ((SECONDS < deadline)); do
        if resource_exists_exact "$resource" "$resource_id"; then
            sleep 2
            continue
        else
            lookup_status=$?
        fi
        [[ "$lookup_status" == 1 ]] && return 0
        sleep 2
    done
    echo "Timed out proving deletion of $resource UUID $resource_id" >&2
    return 1
}

guest_marker_ssh_target() {
    local host entry
    host=$(openstack --os-compute-api-version 2.74 server show "$server_id" \
        -f value -c "OS-EXT-SRV-ATTR:host" 2>/dev/null) || return 1
    [[ -n "$host" && -n "$HOST_SSH_MAP" ]] || return 1
    for entry in ${HOST_SSH_MAP//,/ }; do
        if [[ "${entry%%=*}" == "$host" ]]; then
            printf '%s\n' "${entry#*=}"
            return 0
        fi
    done
    return 1
}

guest_marker_log() {
    local target
    target=$(guest_marker_ssh_target) || return 1
    ssh -n -o BatchMode=yes -o StrictHostKeyChecking=no \
        ${SSH_IDENTITY:+-i "$SSH_IDENTITY"} \
        -o UserKnownHostsFile="$SSH_KNOWN_HOSTS_FILE" \
        "$target" "podman exec incus incus exec '$instance_name' \
        --project nova -- cat '$GUEST_MARKER_LOG'" 2>/dev/null
}

wait_console_marker() {
    local marker=$1 deadline=$((SECONDS + TIMEOUT)) output= guest_output=
    while ((SECONDS < deadline)); do
        output=$(openstack console log show "$server_id" 2>/dev/null || true)
        grep -Fqx "$marker" <<<"$output" && return 0
        guest_output=$(guest_marker_log || true)
        grep -Fqx "$marker" <<<"$guest_output" && return 0
        sleep 2
    done
    printf 'console log:\n%s\nguest marker log:\n%s\n' \
        "$output" "$guest_output" >&2
    echo "Console marker was not observed: $marker" >&2
    return 1
}

cleanup() {
    local exit_status=$? cleanup_failed=false lookup_status=
    local candidate=
    trap - EXIT INT TERM
    set +e
    rm -f "$user_data"
    if [[ -n "$server_id" ]]; then
        openstack server delete --wait "$server_id" >/dev/null 2>&1
        if wait_absent server "$server_id"; then
            server_id=
        else
            echo "Cleanup retained server UUID $server_id and its volume" >&2
            cleanup_failed=true
        fi
    fi
    if [[ -z "$server_id" ]]; then
        for candidate in "${volume_ids[@]}" "$root_volume_id"; do
            [[ -n "$candidate" ]] || continue
            if resource_exists_exact volume "$candidate"; then
                wait_field available openstack volume show "$candidate" \
                    -f value -c status >/dev/null 2>&1
                if [[ "$(openstack volume show "$candidate" -f value \
                        -c status 2>/dev/null)" == available ]]; then
                    openstack volume delete "$candidate" >/dev/null 2>&1
                    wait_absent volume "$candidate" || true
                fi
            else
                lookup_status=$?
                [[ "$lookup_status" == 1 ]] && continue
            fi
            if resource_exists_exact volume "$candidate"; then
                echo "Cleanup retained volume UUID $candidate" >&2
                cleanup_failed=true
            else
                lookup_status=$?
                if [[ "$lookup_status" != 1 ]]; then
                    echo "Could not prove cleanup of volume UUID $candidate" >&2
                    cleanup_failed=true
                fi
            fi
        done
    fi
    if [[ "$cleanup_failed" == true && "$exit_status" == 0 ]]; then
        exit_status=1
    elif [[ "$cleanup_failed" == false && "$exit_status" == 0 &&
            -n "$pass_message" ]]; then
        if [[ -n "$EVIDENCE_FILE" ]]; then
            if ! EVIDENCE_SERVER_UUID="$server_uuid" \
                EVIDENCE_INSTANCE_NAME="$instance_name" \
                EVIDENCE_ROOT_VOLUME_ID="$root_volume_id" \
                EVIDENCE_DATA_VOLUME_IDS="$(printf '%s\n' "${volume_ids[@]}")" \
                python3 - "$EVIDENCE_FILE" <<'PY'
import json
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
payload = {
    "schema": 1,
    "server_uuid": os.environ["EVIDENCE_SERVER_UUID"],
    "instance_name": os.environ["EVIDENCE_INSTANCE_NAME"],
    "root_volume_id": os.environ.get("EVIDENCE_ROOT_VOLUME_ID") or None,
    "data_volume_ids": [
        item
        for item in os.environ.get("EVIDENCE_DATA_VOLUME_IDS", "").splitlines()
        if item
    ],
}
temporary = path.with_name(path.name + ".tmp")
temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
temporary.replace(path)
PY
            then
                echo "Failed to persist exact cleanup evidence" >&2
                exit_status=1
            fi
        fi
        if ((exit_status == 0)); then
            echo "$pass_message"
        fi
    fi
    exit "$exit_status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

cat >"$user_data" <<EOF
#!/bin/sh
set -eu

TOKEN='$token'
DEVICES='${devices[*]}'

emit() {
    printf '%s\n' "\$1" >/dev/console 2>/dev/null || true
    printf '%s\n' "\$1" >>'$GUEST_MARKER_LOG'
    sync
}

unmount_fuse() {
    mountpoint=\$1
    if command -v fusermount3 >/dev/null 2>&1; then
        fusermount3 -u "\$mountpoint"
    elif command -v fusermount >/dev/null 2>&1; then
        fusermount -u "\$mountpoint"
    else
        emit "OPENSTACK_INCUS_DATA_ERROR:no-fusermount"
        return 1
    fi
}

check_volume() {
    device=\$1
    phase=\$2
    index=\$3
    mountpoint=/run/openstack-incus-data-e2e-\$index
    marker=\$mountpoint/marker
    count=0
    while [ ! -b "\$device" ] && [ "\$count" -lt 60 ]; do
        count=\$((count + 1))
        sleep 1
    done
    [ -b "\$device" ] || {
        emit "OPENSTACK_INCUS_DATA_ERROR:missing-device:\$device"
        return 1
    }
    command -v blkid >/dev/null 2>&1
    command -v mkfs.ext4 >/dev/null 2>&1
    command -v fuse2fs >/dev/null 2>&1 || {
        emit "OPENSTACK_INCUS_DATA_ERROR:missing-fuse2fs"
        return 1
    }
    if [ "\$(blkid -s TYPE -o value "\$device" 2>/dev/null || true)" != ext4 ]; then
        [ "\$phase" = FIRST ] || {
            emit "OPENSTACK_INCUS_DATA_ERROR:filesystem-missing-after-reboot:\$device"
            return 1
        }
        mkfs.ext4 -F "\$device" >/dev/null
    fi
    mkdir -p "\$mountpoint"
    # Read-write is the fuse2fs default; this build rejects "-o rw+".
    fuse2fs "\$device" "\$mountpoint"
    # A failed FUSE mount would leave the tmpfs directory in place and the
    # marker round-trip below would silently pass against the wrong
    # filesystem, so prove the mountpoint is a live fuse mount first.
    grep -q " \$mountpoint fuse" /proc/mounts || {
        emit "OPENSTACK_INCUS_DATA_ERROR:not-mounted:\$device"
        return 1
    }
    if [ "\$phase" = FIRST ]; then
        printf '%s\n' "\$TOKEN:\$index" >"\$marker"
        sync
    fi
    [ "\$(cat "\$marker" 2>/dev/null || true)" = "\$TOKEN:\$index" ] || {
        emit "OPENSTACK_INCUS_DATA_ERROR:marker-mismatch:\$device"
        unmount_fuse "\$mountpoint" || true
        return 1
    }
    sync
    unmount_fuse "\$mountpoint"
}

check_volumes() {
    phase=\$1
    index=0
    for device in \$DEVICES; do
        check_volume "\$device" "\$phase" "\$index"
        index=\$((index + 1))
    done
    emit "OPENSTACK_INCUS_DATA_\${phase}_OK:\$TOKEN"
}

if [ "\${1:-FIRST}" = REBOOT ]; then
    check_volumes REBOOT
    exit
fi

check_volumes FIRST
install -D -m 0755 "\$0" /usr/local/sbin/openstack-incus-data-e2e
if command -v systemctl >/dev/null 2>&1; then
    cat >/etc/systemd/system/openstack-incus-data-e2e.service <<'UNIT'
[Unit]
Description=Verify initial Cinder data-volume persistence
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/openstack-incus-data-e2e REBOOT

[Install]
WantedBy=multi-user.target
UNIT
    systemctl enable openstack-incus-data-e2e.service
elif command -v rc-update >/dev/null 2>&1 && [ -d /etc/local.d ]; then
    cat >/etc/local.d/openstack-incus-data-e2e.start <<'OPENRC'
#!/bin/sh
exec /usr/local/sbin/openstack-incus-data-e2e REBOOT
OPENRC
    chmod 0755 /etc/local.d/openstack-incus-data-e2e.start
    rc-update add local default >/dev/null
else
    emit "OPENSTACK_INCUS_DATA_ERROR:unsupported-init"
    exit 1
fi
EOF

declare -a server_root_args=() server_data_args=()
if [[ "$ROOT_MODE" == bfv ]]; then
    root_volume_id=$(openstack volume create --size "$ROOT_VOLUME_SIZE" \
        --type "$ROOT_VOLUME_TYPE" --image "$IMAGE" "$NAME-root" \
        -f value -c id)
    wait_field available openstack volume show "$root_volume_id" \
        -f value -c status
    root_bdm="uuid=$root_volume_id,source_type=volume,destination_type=volume"
    root_bdm+=",boot_index=0,delete_on_termination=false"
    server_root_args+=(--block-device "$root_bdm")
else
    server_root_args+=(--image "$IMAGE")
fi

for ((index = 0; index < DATA_VOLUME_COUNT; index++)); do
    volume_id=$(openstack volume create --size "$VOLUME_SIZE" \
        --type "$VOLUME_TYPE" "$NAME-data-$index" -f value -c id)
    volume_ids+=("$volume_id")
    wait_field available openstack volume show "$volume_id" -f value -c status
    bdm="uuid=$volume_id,source_type=volume,destination_type=volume"
    bdm+=",device_name=${devices[$index]},boot_index=-1"
    bdm+=",delete_on_termination=false"
    server_data_args+=(--block-device "$bdm")
done

server_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --network "$NETWORK" "${server_root_args[@]}" \
    --use-config-drive --user-data "$user_data" "${server_data_args[@]}" \
    "$NAME-server" -f value -c id)
server_uuid=$server_id
wait_field ACTIVE openstack server show "$server_id" -f value -c status
instance_name=$(openstack --os-compute-api-version 2.74 server show \
    "$server_id" -f value -c OS-EXT-SRV-ATTR:instance_name)
[[ -n "$instance_name" ]] || {
    echo "Nova did not expose the server instance name" >&2
    exit 1
}
for volume_id in "${volume_ids[@]}"; do
    wait_field in-use openstack volume show "$volume_id" -f value -c status
done
if [[ -n "$root_volume_id" ]]; then
    wait_field in-use openstack volume show "$root_volume_id" \
        -f value -c status
fi

attachment=$(openstack server volume list "$server_id" -f json)
expected_attachments=
for ((index = 0; index < DATA_VOLUME_COUNT; index++)); do
    expected_attachments+="${volume_ids[$index]}=${devices[$index]}"$'\n'
done
ATTACHMENT_JSON="$attachment" EXPECTED_ATTACHMENTS="$expected_attachments" \
    EXPECTED_ROOT_VOLUME_ID="$root_volume_id" \
    python3 - <<'PY'
import json
import os

rows = json.loads(os.environ["ATTACHMENT_JSON"])
expected = {}
for item in os.environ.get("EXPECTED_ATTACHMENTS", "").splitlines():
    volume_id, device = item.split("=", 1)
    expected[volume_id] = device
root_volume_id = os.environ.get("EXPECTED_ROOT_VOLUME_ID", "")
expected_ids = set(expected)
if root_volume_id:
    expected_ids.add(root_volume_id)

row_ids = []
for row in rows:
    volume_id = str(row.get("ID") or row.get("Volume ID") or "")
    if not volume_id:
        raise SystemExit("server volume attachment has no volume UUID")
    row_ids.append(volume_id)

if len(row_ids) != len(set(row_ids)):
    raise SystemExit("server volume attachment inventory contains duplicates")
if set(row_ids) != expected_ids:
    raise SystemExit(
        "server volume attachment set mismatch: actual={} expected={}".format(
            sorted(row_ids), sorted(expected_ids)))

for volume_id, expected_device in expected.items():
    matches = [
        row for row in rows
        if str(row.get("ID") or row.get("Volume ID") or "") == volume_id
    ]
    if len(matches) != 1:
        raise SystemExit(
            "initial data volume {} is not attached exactly once".format(
                volume_id))
    device = str(matches[0].get("Device") or matches[0].get("device") or "")
    if device != expected_device:
        raise SystemExit(
            "initial data volume device mismatch: {} != {}".format(
                device, expected_device))
if root_volume_id and row_ids.count(root_volume_id) != 1:
    raise SystemExit("BFV root volume is not attached exactly once")
PY

wait_console_marker "$first_marker"
openstack server reboot --hard --wait "$server_id"
wait_field ACTIVE openstack server show "$server_id" -f value -c status
wait_console_marker "$reboot_marker"

pass_message="PASS public API initial-data-volume root=$ROOT_MODE server=$server_id data_volumes=$DATA_VOLUME_COUNT"
