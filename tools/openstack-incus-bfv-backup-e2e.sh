#!/usr/bin/env bash
# Validate Cinder full/incremental backup and cross-compute BFV restore.

set -Eeuo pipefail

IMAGE=${IMAGE:-alpine-3.21-criu-bfv-fuse}
FLAVOR=${FLAVOR:-ds512M}
NETWORK=${NETWORK:-public}
SOURCE_HOST=${SOURCE_HOST:-incus-node-01}
DEST_HOST=${DEST_HOST:-incus-node-02}
SOURCE_SSH=${SOURCE_SSH:-root@10.32.32.130}
DEST_SSH=${DEST_SSH:-root@10.32.32.131}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
VOLUME_TYPE=${VOLUME_TYPE:-ceph}
VOLUME_SIZE=${VOLUME_SIZE:-2}
NAME=${NAME:-incus-bfv-backup-e2e-$RANDOM}
TIMEOUT=${TIMEOUT:-600}
INCUS_PROJECT=${INCUS_PROJECT:-nova}

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)
source_server=
restore_server=
source_volume=
restore_volume=
full_backup=
incremental_backup=

remote() {
    local host=$1
    shift
    "${SSH[@]}" "$host" "$@"
}

wait_value() {
    local command=$1 expected=$2 deadline=$((SECONDS + TIMEOUT)) current
    while ((SECONDS < deadline)); do
        current=$(eval "$command" 2>/dev/null || true)
        [[ "$current" == "$expected" ]] && return 0
        [[ "$current" == error* || "$current" == ERROR ]] && break
        sleep 2
    done
    echo "Timed out waiting for $expected (current: ${current:-missing})" >&2
    return 1
}

wait_absent() {
    local command=$1 deadline=$((SECONDS + TIMEOUT))
    while ((SECONDS < deadline)); do
        ! eval "$command" >/dev/null 2>&1 && return 0
        sleep 2
    done
    return 1
}

cleanup() {
    if [[ -n "$restore_server" ]]; then
        openstack server delete --wait "$restore_server" >/dev/null 2>&1 || true
    fi
    if [[ -n "$source_server" ]]; then
        openstack server delete --wait "$source_server" >/dev/null 2>&1 || true
    fi
    if [[ -n "$incremental_backup" ]]; then
        openstack volume backup delete "$incremental_backup" \
            >/dev/null 2>&1 || true
    fi
    if [[ -n "$full_backup" ]]; then
        openstack volume backup delete "$full_backup" >/dev/null 2>&1 || true
    fi
    if [[ -n "$restore_volume" ]]; then
        openstack volume delete "$restore_volume" >/dev/null 2>&1 || true
    fi
    if [[ -n "$source_volume" ]]; then
        openstack volume delete "$source_volume" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

openstack volume service list --service cinder-backup -f value | \
    grep -Eq '(^|[[:space:]])up([[:space:]]|$)' || {
        echo "An up cinder-backup service is required" >&2
        exit 2
    }

source_volume=$(openstack volume create \
    --image "$IMAGE" --size "$VOLUME_SIZE" --type "$VOLUME_TYPE" \
    "$NAME-root" -f value -c id)
wait_value "openstack volume show '$source_volume' -f value -c status" \
    available

source_server=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --volume "$source_volume" --network "$NETWORK" \
    --host "$SOURCE_HOST" --wait "$NAME-source" -f value -c id)
source_instance=$(openstack server show "$source_server" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)
marker="full-$source_volume"
remote "$SOURCE_SSH" \
    "incus exec --project '$INCUS_PROJECT' '$source_instance' -- sh -c \
    'printf %s \"$marker\" > /root/openstack-backup-marker; sync'"

# A stopped guest gives filesystem consistency. Stateful applications still
# need their own quiesce transaction before this point.
openstack server stop "$source_server"
wait_value "openstack server show '$source_server' -f value -c status" SHUTOFF
full_backup=$(openstack volume backup create --force \
    --name "$NAME-full" "$source_volume" -f value -c id)
wait_value "openstack volume backup show '$full_backup' -f value -c status" \
    available

openstack server start "$source_server"
wait_value "openstack server show '$source_server' -f value -c status" ACTIVE
marker="incremental-$source_volume"
remote "$SOURCE_SSH" \
    "incus exec --project '$INCUS_PROJECT' '$source_instance' -- sh -c \
    'printf %s \"$marker\" > /root/openstack-backup-marker; sync'"
openstack server stop "$source_server"
wait_value "openstack server show '$source_server' -f value -c status" SHUTOFF

incremental_backup=$(openstack volume backup create --force --incremental \
    --name "$NAME-incremental" "$source_volume" -f value -c id)
wait_value \
    "openstack volume backup show '$incremental_backup' -f value -c status" \
    available
[[ "$(openstack volume backup show "$incremental_backup" -f value \
    -c is_incremental)" == True ]]

restore_volume=$(openstack volume create \
    --size "$VOLUME_SIZE" --type "$VOLUME_TYPE" \
    "$NAME-restored-root" -f value -c id)
wait_value "openstack volume show '$restore_volume' -f value -c status" \
    available
openstack volume backup restore --force \
    "$incremental_backup" "$restore_volume"
wait_value "openstack volume show '$restore_volume' -f value -c status" \
    available
openstack volume set --bootable "$restore_volume"

restore_server=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --volume "$restore_volume" --network "$NETWORK" \
    --host "$DEST_HOST" --wait "$NAME-restored" -f value -c id)
restore_instance=$(openstack server show "$restore_server" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)
restored=$(remote "$DEST_SSH" \
    "incus exec --project '$INCUS_PROJECT' '$restore_instance' -- \
    cat /root/openstack-backup-marker")
[[ "$restored" == "$marker" ]]

openstack server delete --wait "$restore_server"
restore_server=
openstack server delete --wait "$source_server"
source_server=
openstack volume backup delete "$incremental_backup"
wait_absent "openstack volume backup show '$incremental_backup'"
incremental_backup=
openstack volume backup delete "$full_backup"
wait_absent "openstack volume backup show '$full_backup'"
full_backup=
openstack volume delete "$restore_volume" "$source_volume"
wait_absent "openstack volume show '$restore_volume'"
wait_absent "openstack volume show '$source_volume'"
restore_volume=
source_volume=

trap - EXIT INT TERM
echo "PASS BFV full/incremental backup and cross-compute restore marker=$marker"
