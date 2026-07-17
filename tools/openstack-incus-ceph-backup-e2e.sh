#!/usr/bin/env bash
# Validate full and incremental Ceph backup plus restore through public APIs.

set -euo pipefail

IMAGE=${IMAGE:-ubuntu-noble-cloud-incus-fuse}
FLAVOR=${FLAVOR:-m1.small}
NETWORK=${NETWORK:-private}
COMPUTE_HOST=${COMPUTE_HOST:-incus-node-01}
COMPUTE_SSH=${COMPUTE_SSH:-root@10.224.0.16}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
VOLUME_TYPE=${VOLUME_TYPE:-ceph}
NAME=${NAME:-incus-ceph-backup-e2e-$RANDOM}
TIMEOUT=${TIMEOUT:-360}

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)
server_id=
source_id=
restore_id=
full_id=
incremental_id=
instance_name=

remote() { "${SSH[@]}" "$COMPUTE_SSH" "$@"; }

wait_value() {
    local command=$1 expected=$2 deadline=$((SECONDS + TIMEOUT)) current
    while ((SECONDS < deadline)); do
        current=$(eval "$command" 2>/dev/null || true)
        [[ "$current" == "$expected" ]] && return 0
        [[ "$current" == error* ]] && break
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

detach() {
    local volume=$1
    openstack server remove volume "$server_id" "$volume"
    wait_value "openstack volume show '$volume' -f value -c status" available
}

cleanup() {
    for volume in "$restore_id" "$source_id"; do
        [[ -n "$server_id" && -n "$volume" ]] && \
            openstack server remove volume "$server_id" "$volume" \
                >/dev/null 2>&1 || true
    done
    [[ -n "$incremental_id" ]] && \
        openstack volume backup delete "$incremental_id" >/dev/null 2>&1 || true
    [[ -n "$full_id" ]] && \
        openstack volume backup delete "$full_id" >/dev/null 2>&1 || true
    [[ -n "$server_id" ]] && \
        openstack server delete --wait "$server_id" >/dev/null 2>&1 || true
    [[ -n "$restore_id" ]] && \
        openstack volume delete "$restore_id" >/dev/null 2>&1 || true
    [[ -n "$source_id" ]] && \
        openstack volume delete "$source_id" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

openstack volume service list --service cinder-backup -f value | \
    grep -Eq '(^|[[:space:]])up([[:space:]]|$)' || {
        echo "An up cinder-backup service is required" >&2
        exit 2
    }

server_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --image "$IMAGE" --network "$NETWORK" \
    --host "$COMPUTE_HOST" --wait "$NAME" -f value -c id)
instance_name=$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)
source_id=$(openstack volume create --size 1 --type "$VOLUME_TYPE" \
    "$NAME-source" -f value -c id)
wait_value "openstack volume show '$source_id' -f value -c status" available

openstack server add volume --device /dev/vdb "$server_id" "$source_id"
wait_value "openstack volume show '$source_id' -f value -c status" in-use
device=$(openstack server volume list "$server_id" -f value -c Device | head -n1)
marker="full-$source_id"
remote "incus exec '$instance_name' -- sh -c \
    'mkfs.ext4 -F $device >/dev/null; mkdir -p /mnt/cinder; \
     fuse2fs $device /mnt/cinder; printf %s $marker > /mnt/cinder/marker; \
     sync; fusermount -u /mnt/cinder'"
detach "$source_id"

full_id=$(openstack volume backup create --name "$NAME-full" \
    "$source_id" -f value -c id)
wait_value "openstack volume backup show '$full_id' -f value -c status" \
    available

openstack server add volume --device /dev/vdb "$server_id" "$source_id"
wait_value "openstack volume show '$source_id' -f value -c status" in-use
marker="incremental-$source_id"
remote "incus exec '$instance_name' -- sh -c \
    'fuse2fs $device /mnt/cinder; printf %s $marker > /mnt/cinder/marker; \
     sync; fusermount -u /mnt/cinder'"
detach "$source_id"

incremental_id=$(openstack volume backup create --incremental \
    --name "$NAME-incremental" "$source_id" -f value -c id)
wait_value \
    "openstack volume backup show '$incremental_id' -f value -c status" \
    available
[[ "$(openstack volume backup show "$incremental_id" -f value \
    -c is_incremental)" == True ]]

restore_id=$(openstack volume create --size 1 --type "$VOLUME_TYPE" \
    "$NAME-restore" -f value -c id)
wait_value "openstack volume show '$restore_id' -f value -c status" available
openstack volume backup restore --force "$incremental_id" "$restore_id"
wait_value "openstack volume show '$restore_id' -f value -c status" available
openstack server add volume --device /dev/vdb "$server_id" "$restore_id"
wait_value "openstack volume show '$restore_id' -f value -c status" in-use
restored=$(remote "incus exec '$instance_name' -- sh -c \
    'fuse2fs -o ro $device /mnt/cinder; cat /mnt/cinder/marker; \
     fusermount -u /mnt/cinder'")
[[ "$restored" == "$marker" ]]
detach "$restore_id"

openstack volume backup delete "$incremental_id"
wait_absent "openstack volume backup show '$incremental_id'"
incremental_id=
openstack volume backup delete "$full_id"
wait_absent "openstack volume backup show '$full_id'"
full_id=
openstack volume delete "$restore_id"
wait_absent "openstack volume show '$restore_id'"
restore_id=
openstack volume delete "$source_id"
wait_absent "openstack volume show '$source_id'"
source_id=
openstack server delete --wait "$server_id"
server_id=

trap - EXIT INT TERM
echo "PASS full backup, incremental backup, restore, and cleanup"
