#!/usr/bin/env bash
# Public-API E2E for Nova image-create of a Cinder BFV server and restore.
#
# Required: RUN_DESTRUCTIVE=true IMAGE=... FLAVOR=... NETWORK=...
#           VOLUME_TYPE=...
# Optional: VOLUME_SIZE=5 TIMEOUT=900 NAME=...
#
# The guest image must provide cloud-init and either systemd or OpenRC local
# services. Nova console output and Cinder volume snapshots must be enabled.
# The selected Cinder type must map to an Incus cephext BFV pool.

set -Eeuo pipefail

RUN_DESTRUCTIVE=${RUN_DESTRUCTIVE:-false}
if [[ "$RUN_DESTRUCTIVE" != true ]]; then
    echo "Refusing destructive E2E; set RUN_DESTRUCTIVE=true" >&2
    exit 2
fi

IMAGE=${IMAGE:?Set IMAGE to an admitted BFV rootfs-directory image}
FLAVOR=${FLAVOR:?Set FLAVOR to an Incus-compatible flavor}
NETWORK=${NETWORK:?Set NETWORK to a tenant network}
VOLUME_TYPE=${VOLUME_TYPE:?Set VOLUME_TYPE to the Cinder backend under test}
VOLUME_SIZE=${VOLUME_SIZE:-5}
TIMEOUT=${TIMEOUT:-900}
NAME=${NAME:-incus-bfv-snapshot-public-api-e2e-$RANDOM}

command -v openstack >/dev/null
command -v python3 >/dev/null
[[ "$VOLUME_SIZE" =~ ^[1-9][0-9]*$ ]] || {
    echo "VOLUME_SIZE must be a positive integer in GiB" >&2
    exit 2
}

source_server=
restore_server=
source_volume=
restore_volume=
snapshot_image=
snapshot_ids=()
pass_message=
user_data=$(mktemp)
token="bfv-snapshot-$(date +%s)-$RANDOM-$$"
source_marker="OPENSTACK_INCUS_BFV_SOURCE_OK:$token"
restore_marker="OPENSTACK_INCUS_BFV_RESTORE_OK:$token"

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
        image)
            show_command=(openstack image show "$resource_id")
            list_command=(openstack image list -f value -c ID)
            ;;
        volume-snapshot)
            show_command=(openstack volume snapshot show "$resource_id")
            list_command=(openstack volume snapshot list -f value -c ID)
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

wait_console_marker() {
    local server=$1 marker=$2 deadline=$((SECONDS + TIMEOUT)) output=
    while ((SECONDS < deadline)); do
        output=$(openstack console log show "$server" 2>/dev/null || true)
        grep -Fqx "$marker" <<<"$output" && return 0
        sleep 2
    done
    printf '%s\n' "$output" >&2
    echo "Console marker was not observed: $marker" >&2
    return 1
}

delete_server() {
    local server=$1
    [[ -z "$server" ]] && return 0
    openstack server delete --wait "$server" >/dev/null 2>&1
    wait_absent server "$server"
}

delete_available_volume() {
    local volume=$1 lookup_status=
    [[ -z "$volume" ]] && return 0
    if resource_exists_exact volume "$volume"; then
        :
    else
        lookup_status=$?
        [[ "$lookup_status" == 1 ]] && return 0
        return 1
    fi
    wait_field available openstack volume show "$volume" \
        -f value -c status >/dev/null 2>&1 || return 1
    openstack volume delete "$volume" >/dev/null 2>&1
    wait_absent volume "$volume"
}

cleanup() {
    local exit_status=$? snapshot cleanup_failed=false lookup_status=
    local -a remaining_snapshots=()
    trap - EXIT INT TERM
    set +e
    rm -f "$user_data"

    if [[ -n "$restore_server" ]] && delete_server "$restore_server"; then
        restore_server=
    fi
    if [[ -n "$source_server" ]] && delete_server "$source_server"; then
        source_server=
    fi
    if [[ -z "$restore_server" && -n "$restore_volume" ]]; then
        delete_available_volume "$restore_volume" && restore_volume=
    fi
    if [[ -z "$restore_server" && -z "$restore_volume" &&
          -n "$snapshot_image" ]]; then
        openstack image delete "$snapshot_image" >/dev/null 2>&1
        if wait_absent image "$snapshot_image"; then
            snapshot_image=
        fi
    fi
    if [[ -z "$snapshot_image" ]]; then
        for snapshot in "${snapshot_ids[@]}"; do
            if resource_exists_exact volume-snapshot "$snapshot"; then
                :
            else
                lookup_status=$?
                if [[ "$lookup_status" == 1 ]]; then
                    continue
                fi
                remaining_snapshots+=("$snapshot")
                continue
            fi
            openstack volume snapshot delete "$snapshot" >/dev/null 2>&1
            if ! wait_absent volume-snapshot "$snapshot"; then
                remaining_snapshots+=("$snapshot")
            fi
        done
        snapshot_ids=("${remaining_snapshots[@]}")
    fi
    if [[ -z "$source_server" && ${#snapshot_ids[@]} -eq 0 &&
          -n "$source_volume" ]]; then
        delete_available_volume "$source_volume" && source_volume=
    fi

    [[ -z "$restore_server" ]] ||
        echo "Cleanup retained restore server UUID $restore_server" >&2
    [[ -z "$source_server" ]] ||
        echo "Cleanup retained source server UUID $source_server" >&2
    [[ -z "$restore_volume" ]] ||
        echo "Cleanup retained restore root volume UUID $restore_volume" >&2
    [[ -z "$snapshot_image" ]] ||
        echo "Cleanup retained snapshot image UUID $snapshot_image" >&2
    [[ ${#snapshot_ids[@]} -eq 0 ]] ||
        echo "Cleanup retained Cinder snapshot UUIDs: ${snapshot_ids[*]}" >&2
    [[ -z "$source_volume" ]] ||
        echo "Cleanup retained source root volume UUID $source_volume" >&2
    if [[ -n "$restore_server" || -n "$source_server" ||
          -n "$restore_volume" || -n "$snapshot_image" ||
          ${#snapshot_ids[@]} -ne 0 || -n "$source_volume" ]]; then
        cleanup_failed=true
    fi
    if [[ "$cleanup_failed" == true && "$exit_status" == 0 ]]; then
        exit_status=1
    elif [[ "$cleanup_failed" == false && "$exit_status" == 0 &&
            -n "$pass_message" ]]; then
        echo "$pass_message"
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
MARKER=/root/openstack-incus-bfv-snapshot-marker

emit() {
    printf '%s\n' "\$1" >/dev/console
}

cat > /usr/local/sbin/openstack-incus-bfv-restore-check <<'CHECK'
#!/bin/sh
set -eu
TOKEN='$token'
MARKER=/root/openstack-incus-bfv-snapshot-marker
if [ "\$(cat "\$MARKER" 2>/dev/null || true)" = "\$TOKEN" ]; then
    printf '%s\n' "OPENSTACK_INCUS_BFV_RESTORE_OK:\$TOKEN" >/dev/console
else
    printf '%s\n' "OPENSTACK_INCUS_BFV_ERROR:marker-mismatch" >/dev/console
    exit 1
fi
CHECK
chmod 0755 /usr/local/sbin/openstack-incus-bfv-restore-check

printf '%s\n' "\$TOKEN" >"\$MARKER"
sync

if command -v systemctl >/dev/null 2>&1; then
    cat >/etc/systemd/system/openstack-incus-bfv-restore-check.service <<'UNIT'
[Unit]
Description=Verify Cinder BFV snapshot data after restore
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/openstack-incus-bfv-restore-check

[Install]
WantedBy=multi-user.target
UNIT
    systemctl enable openstack-incus-bfv-restore-check.service
elif command -v rc-update >/dev/null 2>&1 && [ -d /etc/local.d ]; then
    cat >/etc/local.d/openstack-incus-bfv-restore-check.start <<'OPENRC'
#!/bin/sh
exec /usr/local/sbin/openstack-incus-bfv-restore-check
OPENRC
    chmod 0755 /etc/local.d/openstack-incus-bfv-restore-check.start
    rc-update add local default >/dev/null
else
    emit "OPENSTACK_INCUS_BFV_ERROR:unsupported-init"
    exit 1
fi

emit "OPENSTACK_INCUS_BFV_SOURCE_OK:\$TOKEN"
EOF

source_volume=$(openstack volume create --image "$IMAGE" \
    --size "$VOLUME_SIZE" --type "$VOLUME_TYPE" "$NAME-source-root" \
    -f value -c id)
wait_field available openstack volume show "$source_volume" \
    -f value -c status

source_server=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --volume "$source_volume" --network "$NETWORK" \
    --use-config-drive --user-data "$user_data" "$NAME-source" \
    -f value -c id)
wait_field ACTIVE openstack server show "$source_server" -f value -c status
wait_console_marker "$source_server" "$source_marker"

# Stop before image-create so the Cinder root snapshot is crash-consistent.
# Applications still need their own guest quiesce transaction.
openstack server stop "$source_server"
wait_field SHUTOFF openstack server show "$source_server" -f value -c status

snapshot_image=$(openstack server image create \
    --name "$NAME-image" --wait "$source_server" -f value -c id |
    sed '/^[[:space:]]*$/d' | tail -n1)
[[ -n "$snapshot_image" ]] || {
    echo "Nova did not return the snapshot image UUID" >&2
    exit 1
}
wait_field active openstack image show "$snapshot_image" -f value -c status

mapfile -t snapshot_ids < <(
    openstack image show "$snapshot_image" -f json |
        python3 -c 'import json
import sys

image = json.load(sys.stdin)
props = image.get("properties") or image.get("Properties") or {}
bdm = (
    props.get("block_device_mapping")
    if isinstance(props, dict)
    else None
)
if bdm is None:
    bdm = image.get("block_device_mapping")
if isinstance(bdm, str):
    bdm = json.loads(bdm)
if not isinstance(bdm, list):
    raise SystemExit("snapshot image has no block_device_mapping list")
snapshots = []
for entry in bdm:
    if not isinstance(entry, dict):
        continue
    source_type = entry.get("source_type")
    snapshot = entry.get("snapshot_id")
    if source_type == "snapshot":
        snapshot = snapshot or entry.get("uuid")
    if snapshot:
        snapshots.append(str(snapshot))
if not snapshots:
    raise SystemExit("snapshot image has no Cinder snapshot")
print("\n".join(dict.fromkeys(snapshots)))
')
(( ${#snapshot_ids[@]} == 1 )) || {
    echo "BFV image-create must expose exactly one root snapshot UUID" >&2
    exit 1
}
for snapshot in "${snapshot_ids[@]}"; do
    wait_field available openstack volume snapshot show "$snapshot" \
        -f value -c status
done
snapshot_detail=$(openstack volume snapshot show "${snapshot_ids[0]}" -f json)
SNAPSHOT_JSON="$snapshot_detail" python3 - "$source_volume" <<'PY'
import json
import os
import sys

snapshot = json.loads(os.environ["SNAPSHOT_JSON"])
source_volume = sys.argv[1]
actual = (
    snapshot.get("volume_id") or snapshot.get("Volume ID") or
    snapshot.get("Volume")
)
if str(actual or "") != source_volume:
    raise SystemExit(
        "Nova image-create snapshot does not belong to the source root "
        "volume")
PY

# The image's BDM is authoritative. Adding another generated boot volume would
# create a second boot_index=0 mapping and would not test snapshot restoration.
restore_server=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --image "$snapshot_image" --network "$NETWORK" \
    --use-config-drive "$NAME-restore" -f value -c id)
wait_field ACTIVE openstack server show "$restore_server" -f value -c status

mapfile -t restore_volumes < <(
    openstack server volume list "$restore_server" -f value -c ID |
        sed '/^[[:space:]]*$/d'
)
(( ${#restore_volumes[@]} == 1 )) || {
    echo "Restored BFV server must have exactly one Cinder root volume" >&2
    exit 1
}
restore_volume=${restore_volumes[0]}
[[ "$restore_volume" != "$source_volume" ]] || {
    echo "Restore reused the source root volume instead of a snapshot clone" >&2
    exit 1
}
wait_field in-use openstack volume show "$restore_volume" -f value -c status
restore_detail=$(openstack volume show "$restore_volume" -f json)
RESTORE_JSON="$restore_detail" python3 - "${snapshot_ids[0]}" <<'PY'
import json
import os
import sys

volume = json.loads(os.environ["RESTORE_JSON"])
expected_snapshot = sys.argv[1]
actual = (
    volume.get("snapshot_id") or volume.get("Snapshot ID") or
    volume.get("Snapshot")
)
if str(actual or "") != expected_snapshot:
    raise SystemExit(
        "restored BFV root volume was not created from the Nova image "
        "snapshot")
PY
wait_console_marker "$restore_server" "$restore_marker"

pass_message="PASS public API BFV snapshot source=$source_server image=$snapshot_image restore=$restore_server root=$restore_volume"
