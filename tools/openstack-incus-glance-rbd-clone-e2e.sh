#!/usr/bin/env bash
# Prove that Cinder creates an RBD COW clone from a Glance RBD image.

set -euo pipefail

IMAGE=${IMAGE:?Set IMAGE to the Glance image name or ID}
VOLUME_TYPE=${VOLUME_TYPE:?Set VOLUME_TYPE to the Cinder RBD volume type}
CINDER_POOL=${CINDER_POOL:?Set CINDER_POOL to the Cinder RBD pool}
CINDER_USER=${CINDER_USER:-cinder}
VOLUME_SIZE=${VOLUME_SIZE:-2}
TIMEOUT=${TIMEOUT:-300}
NAME=${NAME:-glance-rbd-clone-proof-$RANDOM}

volume_id=

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

cleanup() {
    local exit_status=$?
    if [[ -n "$volume_id" ]]; then
        openstack volume delete "$volume_id" >/dev/null 2>&1 || true
    fi
    exit "$exit_status"
}
trap cleanup EXIT

[[ "$VOLUME_SIZE" =~ ^[1-9][0-9]*$ ]] || \
    fail "VOLUME_SIZE must be a positive integer"
[[ "$CINDER_POOL" =~ ^[A-Za-z0-9_.-]+$ ]] || \
    fail "CINDER_POOL contains unsupported characters"
[[ "$CINDER_USER" =~ ^[A-Za-z0-9_.-]+$ ]] || \
    fail "CINDER_USER contains unsupported characters"
command -v openstack >/dev/null || fail "openstack is required"
command -v rbd >/dev/null || fail "rbd is required"
command -v jq >/dev/null || fail "jq is required"

image_json=$(openstack image show "$IMAGE" -f json)
image_id=$(jq -r '.id' <<<"$image_json")
disk_format=$(jq -r '.disk_format' <<<"$image_json")
direct_url=$(jq -r '.properties.direct_url // .direct_url // empty' \
    <<<"$image_json")
[[ "$disk_format" == raw ]] || fail "Glance image must use raw disk format"
[[ "$direct_url" == rbd://* ]] || fail "Glance image has no RBD direct URL"

url_path=${direct_url#rbd://}
IFS=/ read -r glance_fsid glance_pool glance_image glance_snapshot \
    <<<"$url_path"
[[ -n "$glance_fsid" && -n "$glance_pool" && -n "$glance_image" &&
   -n "$glance_snapshot" ]] || fail "Glance RBD direct URL is incomplete"
[[ "$glance_image" == "$image_id" ]] || \
    fail "Glance direct URL image does not match the image ID"

ceph_fsid=$(ceph fsid --id "$CINDER_USER")
[[ "$glance_fsid" == "$ceph_fsid" ]] || \
    fail "Glance and Cinder do not use the same Ceph cluster"

volume_id=$(openstack volume create --type "$VOLUME_TYPE" --image "$image_id" \
    --size "$VOLUME_SIZE" -f value -c id "$NAME")
deadline=$((SECONDS + TIMEOUT))
while ((SECONDS < deadline)); do
    status=$(openstack volume show "$volume_id" -f value -c status)
    [[ "$status" == available ]] && break
    [[ "$status" == error* ]] && fail "Cinder volume entered $status"
    sleep 2
done
[[ "${status:-}" == available ]] || fail "timed out creating Cinder volume"

volume_json=$(rbd info "$CINDER_POOL/volume-$volume_id" \
    --id "$CINDER_USER" --format json)
parent_pool=$(jq -r '.parent.pool // empty' <<<"$volume_json")
parent_image=$(jq -r '.parent.image // empty' <<<"$volume_json")
parent_snapshot=$(jq -r '.parent.snapshot // empty' <<<"$volume_json")
parent_overlap=$(jq -r '.parent.overlap // 0' <<<"$volume_json")
volume_bytes=$(jq -r '.size' <<<"$volume_json")

[[ "$parent_pool" == "$glance_pool" ]] || \
    fail "RBD parent pool is $parent_pool, expected $glance_pool"
[[ "$parent_image" == "$glance_image" ]] || \
    fail "RBD parent image is $parent_image, expected $glance_image"
[[ "$parent_snapshot" == "$glance_snapshot" ]] || \
    fail "RBD parent snapshot is $parent_snapshot, expected $glance_snapshot"
[[ "$parent_overlap" -eq "$volume_bytes" ]] || \
    fail "RBD clone does not have full parent overlap"

openstack volume delete "$volume_id"
volume_id=
echo "PASS Glance RBD image -> Cinder RBD COW clone"
