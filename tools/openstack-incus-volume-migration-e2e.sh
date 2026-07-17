#!/usr/bin/env bash
# Validate Cinder attach, data persistence, cold migration and detach.

set -euo pipefail

IMAGE=${IMAGE:-ubuntu-noble-cloud-incus-fuse}
FLAVOR=${FLAVOR:-m1.tiny}
NETWORK=${NETWORK:-private}
SOURCE_HOST=${SOURCE_HOST:-incus-node-01}
DEST_HOST=${DEST_HOST:-incus-node-02}
SOURCE_SSH=${SOURCE_SSH:-root@10.224.0.16}
DEST_SSH=${DEST_SSH:-root@10.224.0.17}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
NAME=${NAME:-incus-volume-e2e-$RANDOM}
DEVICE=${DEVICE:-/dev/vdb}
EXTENDED_SIZE_GIB=${EXTENDED_SIZE_GIB:-2}
VOLUME_TYPE=${VOLUME_TYPE:-linstor}
TIMEOUT=${TIMEOUT:-360}

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)
server_id=
volume_id=
instance_name=

remote() { local host=$1; shift; "${SSH[@]}" "$host" "$@"; }

wait_value() {
    local command=$1 expected=$2 deadline=$((SECONDS + TIMEOUT)) current
    while ((SECONDS < deadline)); do
        current=$(eval "$command" 2>/dev/null || true)
        [[ "$current" == "$expected" ]] && return 0
        sleep 2
    done
    echo "Timed out waiting for $expected (current: ${current:-missing})" >&2
    return 1
}

wait_device_size() {
    local host=$1 expected=$2 deadline=$((SECONDS + TIMEOUT)) current
    while ((SECONDS < deadline)); do
        current=$(remote "$host" \
            "incus exec '$instance_name' -- blockdev --getsize64 '$DEVICE'" \
            2>/dev/null || true)
        [[ "$current" =~ ^[0-9]+$ ]] && ((current >= expected)) && return 0
        sleep 2
    done
    echo "Device size did not reach $expected bytes (current: ${current:-missing})" >&2
    return 1
}

cleanup() {
    if [[ -n "$server_id" && -n "$volume_id" ]]; then
        openstack server remove volume "$server_id" "$volume_id" \
            >/dev/null 2>&1 || true
    fi
    [[ -n "$server_id" ]] && \
        openstack server delete --wait "$server_id" >/dev/null 2>&1 || true
    [[ -n "$volume_id" ]] && \
        openstack volume delete "$volume_id" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

[[ "$DEVICE" =~ ^/dev/[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
    echo "DEVICE must be a direct block device path under /dev" >&2
    exit 2
}
[[ "$EXTENDED_SIZE_GIB" =~ ^[1-9][0-9]*$ ]] || {
    echo "EXTENDED_SIZE_GIB must be a positive integer" >&2
    exit 2
}

if ! openstack volume service list >/dev/null 2>&1; then
    echo "Cinder v3 endpoint is required for this test" >&2
    exit 2
fi

volume_id=$(openstack volume create --size 1 --type "$VOLUME_TYPE" \
    "$NAME-volume" -f value -c id)
wait_value "openstack volume show '$volume_id' -f value -c status" available
server_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --image "$IMAGE" --network "$NETWORK" \
    --host "$SOURCE_HOST" --wait "$NAME" -f value -c id)
instance_name=$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)

openstack server add volume --device "$DEVICE" "$server_id" "$volume_id"
wait_value "openstack volume show '$volume_id' -f value -c status" in-use
DEVICE=$(openstack server volume list "$server_id" -f value -c Device | \
    head -n1)
[[ "$DEVICE" =~ ^/dev/[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
    echo "Nova returned an invalid volume device path: $DEVICE" >&2
    exit 1
}
remote "$SOURCE_SSH" \
    "incus profile device show '$instance_name' | grep -F '$volume_id:'"
remote "$SOURCE_SSH" \
    "incus profile get '$instance_name' \
     'user.openstack.volume.$volume_id' | python3 -c \
     'import json,sys; data=json.load(sys.stdin); assert data[\"path\"].startswith(\"/dev/\")'"
marker="cinder-$server_id"
remote "$SOURCE_SSH" "incus exec '$instance_name' -- sh -c \
    'command -v fuse2fs >/dev/null; mkfs.ext4 -F $DEVICE >/dev/null; \
     mkdir -p /mnt/cinder; fuse2fs $DEVICE /mnt/cinder; \
     printf %s $marker > /mnt/cinder/marker; \
     sync; umount /mnt/cinder'"

openstack --os-volume-api-version 3.42 volume set \
    --size "$EXTENDED_SIZE_GIB" "$volume_id"
wait_value "openstack volume show '$volume_id' -f value -c size" \
    "$EXTENDED_SIZE_GIB"
wait_device_size "$SOURCE_SSH" "$((EXTENDED_SIZE_GIB * 1024 * 1024 * 1024))"

openstack --os-compute-api-version 2.56 server migrate \
    --host "$DEST_HOST" --wait "$server_id"
wait_value "openstack server show '$server_id' -f value -c status" VERIFY_RESIZE
restored_marker=$(remote "$DEST_SSH" \
    "incus exec '$instance_name' -- sh -c \
     'fuse2fs $DEVICE /mnt/cinder; cat /mnt/cinder/marker; \
      umount /mnt/cinder'")
[[ "$restored_marker" == "$marker" ]]
openstack server resize confirm "$server_id"
wait_value "openstack server show '$server_id' -f value -c status" ACTIVE

openstack server remove volume "$server_id" "$volume_id"
wait_value "openstack volume show '$volume_id' -f value -c status" available
! remote "$DEST_SSH" \
    "incus profile device show '$instance_name' | grep -F '$volume_id:'"
[[ -z "$(remote "$DEST_SSH" \
    "incus profile get '$instance_name' \
     'user.openstack.volume.$volume_id'")" ]]

trap - EXIT INT TERM
openstack server delete --wait "$server_id"
server_id=
openstack volume delete "$volume_id"
volume_id=
echo "PASS volume attach=data extend=${EXTENDED_SIZE_GIB}GiB migrate=$DEST_HOST detach=clean"
