#!/usr/bin/env bash
# Validate mounted-volume I/O while one of three LINSTOR replicas is offline.

set -euo pipefail

IMAGE=${IMAGE:-ubuntu-noble-cloud-incus-tempest}
FLAVOR=${FLAVOR:-d1}
NETWORK=${NETWORK:-private}
SOURCE_HOST=${SOURCE_HOST:-incus-node-01}
VOLUME_TYPE=${VOLUME_TYPE:-linstor}
NAME=${NAME:-incus-linstor-outage-$RANDOM}
STATE_DIR=${STATE_DIR:-/tmp/$NAME}
TIMEOUT=${TIMEOUT:-360}

server_id=
volume_id=
instance_name=
device=

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

wait_signal() {
    local signal=$1 deadline=$((SECONDS + TIMEOUT))
    while ((SECONDS < deadline)); do
        [[ -e "$STATE_DIR/$signal" ]] && return 0
        sleep 1
    done
    echo "Timed out waiting for $STATE_DIR/$signal" >&2
    return 1
}

cleanup() {
    if [[ -n "$instance_name" ]]; then
        incus exec "$instance_name" -- fusermount3 -u /mnt/cinder \
            >/dev/null 2>&1 || true
    fi
    if [[ -n "$server_id" && -n "$volume_id" ]]; then
        openstack server remove volume "$server_id" "$volume_id" \
            >/dev/null 2>&1 || true
    fi
    [[ -n "$server_id" ]] && \
        openstack server delete --wait "$server_id" >/dev/null 2>&1 || true
    if [[ -n "$volume_id" ]]; then
        openstack volume set --state available "$volume_id" \
            >/dev/null 2>&1 || true
        openstack volume delete "$volume_id" >/dev/null 2>&1 || true
    fi
    rm -rf "$STATE_DIR"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "$STATE_DIR"
chmod 0700 "$STATE_DIR"

volume_id=$(openstack volume create --size 1 --type "$VOLUME_TYPE" \
    "$NAME-volume" -f value -c id)
wait_value "openstack volume show '$volume_id' -f value -c status" available
server_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --image "$IMAGE" --network "$NETWORK" \
    --host "$SOURCE_HOST" --wait "$NAME" -f value -c id)
instance_name=$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)

openstack server add volume --device /dev/vdb "$server_id" "$volume_id"
wait_value "openstack volume show '$volume_id' -f value -c status" in-use
device=$(openstack server volume list "$server_id" -f value -c Device | \
    head -n1)
[[ "$device" =~ ^/dev/[A-Za-z0-9][A-Za-z0-9._-]*$ ]]

incus exec "$instance_name" -- sh -ceu \
    "mkfs.ext4 -F '$device' >/dev/null
     mkdir -p /mnt/cinder
     fuse2fs '$device' /mnt/cinder
     printf before-outage > /mnt/cinder/marker
     sync"

printf '%s\n' "$server_id" >"$STATE_DIR/server-id"
printf '%s\n' "$volume_id" >"$STATE_DIR/volume-id"
printf '%s\n' "$instance_name" >"$STATE_DIR/instance-name"
touch "$STATE_DIR/ready"

wait_signal outage
incus exec "$instance_name" -- sh -ceu \
    "test \"\$(cat /mnt/cinder/marker)\" = before-outage
     printf during-outage > /mnt/cinder/marker
     dd if=/dev/zero of=/mnt/cinder/quorum-write bs=1M count=16 conv=fsync \
       status=none
     test \"\$(cat /mnt/cinder/marker)\" = during-outage"
touch "$STATE_DIR/outage-io-passed"

wait_signal restored
incus exec "$instance_name" -- sh -ceu \
    "test \"\$(cat /mnt/cinder/marker)\" = during-outage
     test \"\$(stat -c %s /mnt/cinder/quorum-write)\" = 16777216
     printf after-recovery > /mnt/cinder/marker
     sync
     fusermount3 -u /mnt/cinder"

openstack server remove volume "$server_id" "$volume_id"
wait_value "openstack volume show '$volume_id' -f value -c status" available
openstack server delete --wait "$server_id"
server_id=
openstack volume delete "$volume_id"
volume_id=

trap - EXIT INT TERM
rm -rf "$STATE_DIR"
echo "PASS one-replica outage quorum I/O and recovery"
