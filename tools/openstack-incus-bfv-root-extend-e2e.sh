#!/usr/bin/env bash
# Validate BFV root growth, recovery, migration persistence and shrink refusal.

set -euo pipefail

IMAGE=${IMAGE:?Set IMAGE to a raw rootfs-directory Glance image}
FLAVOR=${FLAVOR:-m1.small}
NETWORK=${NETWORK:-private}
VOLUME_TYPE=${VOLUME_TYPE:-ceph}
SOURCE_HOST=${SOURCE_HOST:-incus-node-01}
DEST_HOST=${DEST_HOST:-incus-node-02}
SOURCE_SSH=${SOURCE_SSH:-root@10.32.32.130}
DEST_SSH=${DEST_SSH:-root@10.32.32.131}
CONTROLLER_SSH=${CONTROLLER_SSH:-$SOURCE_SSH}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
TIMEOUT=${TIMEOUT:-600}
NAME=${NAME:-incus-bfv-root-extend-$RANDOM}
INITIAL_SIZE=${INITIAL_SIZE:-2}
FIRST_SIZE=${FIRST_SIZE:-3}
FINAL_SIZE=${FINAL_SIZE:-4}
KEEP_RESOURCES_ON_FAILURE=${KEEP_RESOURCES_ON_FAILURE:-false}

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)
server_id=
volume_id=
instance_name=
resize2fs_path=
resize2fs_blocked=false

remote() {
    local host=$1
    shift
    "${SSH[@]}" "$host" "$@"
}

openstack() {
    local command_line
    printf -v command_line '%q ' "$@"
    remote "$CONTROLLER_SSH" \
        "source $CONTROLLER_OPENRC >/dev/null 2>&1; openstack $command_line"
}

incus() {
    local host=$1
    shift
    local command_line
    printf -v command_line '%q ' "$@"
    remote "$host" \
        "podman exec incus incus --project nova $command_line"
}

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

wait_value() {
    local expected=$1
    shift
    local deadline=$((SECONDS + TIMEOUT)) current
    while ((SECONDS < deadline)); do
        current=$("$@" 2>/dev/null || true)
        [[ "$current" == "$expected" ]] && return 0
        [[ "$current" == ERROR ]] && break
        sleep 2
    done
    fail "timed out waiting for $expected (current: ${current:-missing})"
}

server_status() {
    openstack server show "$server_id" -f value -c status
}

volume_status() {
    openstack volume show "$volume_id" -f value -c status
}

instance_root_size() {
    local host=$1
    incus "$host" config device get "$instance_name" root size
}

assert_guest_size() {
    local host=$1 expected_gib=$2
    local expected_bytes=$((expected_gib * 1024 * 1024 * 1024))
    local source block_bytes fs_bytes
    source=$(incus "$host" exec "$instance_name" -- findmnt -n -o SOURCE /)
    source=${source%%\[*}
    block_bytes=$(remote "$host" \
        "podman exec incus blockdev --getsize64 '$source'")
    fs_bytes=$(incus "$host" exec "$instance_name" -- \
        stat -f -c '%b %S' / | awk '{print $1 * $2}')
    ((block_bytes == expected_bytes)) ||
        fail "root block size is $block_bytes, expected $expected_bytes"
    ((fs_bytes >= expected_bytes * 9 / 10)) ||
        fail "root filesystem did not grow near $expected_bytes ($fs_bytes)"
}

assert_rbd_size() {
    local host=$1 expected_gib=$2
    local expected_bytes=$((expected_gib * 1024 * 1024 * 1024)) actual
    actual=$(remote "$host" \
        "podman exec incus rbd info \
         cinder-volumes-rbd-pool/volume-$volume_id --id cinder \
         --format json | jq -r .size")
    [[ "$actual" == "$expected_bytes" ]] ||
        fail "RBD size is $actual, expected $expected_bytes"
}

cleanup() {
    local status=$?
    if [[ "$resize2fs_blocked" == true ]]; then
        remote "$DEST_SSH" \
            "podman exec incus umount '$resize2fs_path'" \
            >/dev/null 2>&1 || true
        resize2fs_blocked=false
    fi
    if ((status != 0)) && [[ "$KEEP_RESOURCES_ON_FAILURE" == true ]]; then
        echo "Preserving failed E2E resources:" >&2
        echo "  server_id=$server_id" >&2
        echo "  volume_id=$volume_id" >&2
        echo "  instance_name=$instance_name" >&2
        echo "  resize2fs_path=$resize2fs_path" >&2
        exit "$status"
    fi
    if [[ -n "$server_id" ]]; then
        openstack server delete --wait "$server_id" >/dev/null 2>&1 || true
    fi
    if [[ -n "$volume_id" ]]; then
        wait_value available volume_status >/dev/null 2>&1 || true
        openstack volume delete "$volume_id" >/dev/null 2>&1 || true
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

((INITIAL_SIZE < FIRST_SIZE && FIRST_SIZE < FINAL_SIZE)) ||
    fail "sizes must satisfy INITIAL_SIZE < FIRST_SIZE < FINAL_SIZE"

volume_id=$(openstack volume create --type "$VOLUME_TYPE" --image "$IMAGE" \
    --size "$INITIAL_SIZE" --bootable -f value -c id "${NAME}-root")
wait_value available volume_status
server_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --volume "$volume_id" --network "$NETWORK" \
    --host "$SOURCE_HOST" -f value -c id "$NAME")
wait_value ACTIVE server_status
instance_name=$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)

marker="bfv-root-extend-$server_id"
incus "$SOURCE_SSH" exec "$instance_name" -- sh -c \
    "printf %s '$marker' > /root/bfv-root-extend-marker; sync"
marker_hash=$(incus "$SOURCE_SSH" exec "$instance_name" -- \
    sha256sum /root/bfv-root-extend-marker | awk '{print $1}')

openstack --os-volume-api-version 3.42 \
    volume set --size "$FIRST_SIZE" "$volume_id"
wait_value in-use volume_status
wait_value "$((FIRST_SIZE * 1024 * 1024 * 1024))B" \
    instance_root_size "$SOURCE_SSH"
assert_rbd_size "$SOURCE_SSH" "$FIRST_SIZE"
assert_guest_size "$SOURCE_SSH" "$FIRST_SIZE"

openstack server reboot --hard "$server_id"
wait_value ACTIVE server_status
assert_guest_size "$SOURCE_SSH" "$FIRST_SIZE"

openstack --os-compute-api-version 2.56 server migrate \
    --host "$DEST_HOST" --wait "$server_id"
wait_value VERIFY_RESIZE server_status
openstack server resize confirm "$server_id"
wait_value ACTIVE server_status

resize2fs_path=$(remote "$DEST_SSH" \
    "podman exec incus sh -c 'command -v resize2fs'")
[[ -n "$resize2fs_path" ]] || fail "resize2fs is missing from Incus image"
remote "$DEST_SSH" \
    "podman exec incus mount --bind /bin/false '$resize2fs_path'"
resize2fs_blocked=true
failure_since=$(remote "$DEST_SSH" date --iso-8601=seconds)
openstack --os-volume-api-version 3.42 \
    volume set --size "$FINAL_SIZE" "$volume_id"
wait_value in-use volume_status
assert_rbd_size "$DEST_SSH" "$FINAL_SIZE"

deadline=$((SECONDS + TIMEOUT))
while ((SECONDS < deadline)); do
    if remote "$DEST_SSH" \
            "journalctl -u devstack@n-cpu --since '$failure_since' --no-pager |
             grep -q 'Extend volume failed.*$volume_id'"; then
        break
    fi
    sleep 2
done
((SECONDS < deadline)) || fail "Nova did not report the injected grow failure"
[[ "$(instance_root_size "$DEST_SSH")" == \
    "$((FIRST_SIZE * 1024 * 1024 * 1024))B" ]] ||
    fail "failed growth unexpectedly updated the Incus root size"

remote "$DEST_SSH" "podman exec incus umount '$resize2fs_path'"
resize2fs_blocked=false
openstack server reboot --hard "$server_id"
wait_value ACTIVE server_status
wait_value "$((FINAL_SIZE * 1024 * 1024 * 1024))B" \
    instance_root_size "$DEST_SSH"
assert_guest_size "$DEST_SSH" "$FINAL_SIZE"

openstack --os-compute-api-version 2.56 server migrate \
    --host "$SOURCE_HOST" --wait "$server_id"
wait_value VERIFY_RESIZE server_status
openstack server resize revert "$server_id"
wait_value ACTIVE server_status
assert_guest_size "$DEST_SSH" "$FINAL_SIZE"
[[ "$(incus "$DEST_SSH" exec "$instance_name" -- \
    sha256sum /root/bfv-root-extend-marker | awk '{print $1}')" == \
    "$marker_hash" ]] || fail "root marker changed after extend/migration"

if openstack --os-volume-api-version 3.42 \
        volume set --size "$FIRST_SIZE" "$volume_id"; then
    fail "Cinder accepted a BFV root shrink request"
fi
assert_rbd_size "$DEST_SSH" "$FINAL_SIZE"
assert_guest_size "$DEST_SSH" "$FINAL_SIZE"

trap - EXIT INT TERM
openstack server delete --wait "$server_id"
server_id=
wait_value available volume_status
openstack volume delete "$volume_id"
volume_id=

echo "PASS BFV root ${INITIAL_SIZE}GiB->${FIRST_SIZE}GiB->${FINAL_SIZE}GiB growth, recovery, migration and shrink refusal"
