#!/usr/bin/env bash
# Prove a failed initial data-volume build rolls back without residue.
#
# The driver refuses to spawn when a data BDM is requested but the Glance
# image does not declare hw_incus_data_volume_fuse=true. This case drives
# that refusal through the public API and asserts libvirt-like rollback:
# the server lands in ERROR with the designed ImageUnacceptable fault, the
# reserved volume returns to available, no hypervisor or idmap state leaks,
# and both resources delete cleanly.
#
# Required: RUN_DESTRUCTIVE=true UNSUPPORTED_IMAGE=<image without the
# property> FLAVOR=... NETWORK=... VOLUME_TYPE=...

set -Eeuo pipefail

RUN_DESTRUCTIVE=${RUN_DESTRUCTIVE:-false}
UNSUPPORTED_IMAGE=${UNSUPPORTED_IMAGE:?Set UNSUPPORTED_IMAGE to an image without hw_incus_data_volume_fuse}
FLAVOR=${FLAVOR:?Set FLAVOR}
NETWORK=${NETWORK:?Set NETWORK}
VOLUME_TYPE=${VOLUME_TYPE:?Set VOLUME_TYPE}
VOLUME_SIZE=${VOLUME_SIZE:-1}
TIMEOUT=${TIMEOUT:-420}
NAME=${NAME:-incus-initial-data-rollback-$RANDOM}

[[ "$RUN_DESTRUCTIVE" == true ]] || {
    echo "Set RUN_DESTRUCTIVE=true to run this destructive case" >&2
    exit 2
}

fuse_property=$(openstack image show "$UNSUPPORTED_IMAGE" -f json \
    -c properties 2>/dev/null |
    grep -c "hw_incus_data_volume_fuse.*true" || true)
[[ "$fuse_property" == 0 ]] || {
    echo "UNSUPPORTED_IMAGE must not declare hw_incus_data_volume_fuse" >&2
    exit 2
}

server_id=
volume_id=

cleanup() {
    set +e
    [[ -n "$server_id" ]] &&
        openstack server delete --wait "$server_id" >/dev/null 2>&1
    [[ -n "$volume_id" ]] &&
        openstack volume delete "$volume_id" >/dev/null 2>&1
}
trap cleanup EXIT

wait_field() {
    local expected=$1 deadline=$((SECONDS + TIMEOUT)) current=
    shift
    while ((SECONDS < deadline)); do
        current=$("$@" 2>/dev/null || true)
        [[ "$current" == "$expected" ]] && return 0
        sleep 3
    done
    echo "Timed out waiting for $expected (current: ${current:-missing})" >&2
    return 1
}

volume_id=$(openstack volume create --size "$VOLUME_SIZE" \
    --type "$VOLUME_TYPE" "$NAME-vol" -f value -c id)
wait_field available openstack volume show "$volume_id" -f value -c status

server_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --image "$UNSUPPORTED_IMAGE" --network "$NETWORK" \
    --block-device "uuid=$volume_id,source_type=volume,\
destination_type=volume,device_name=/dev/vdb,boot_index=-1,\
delete_on_termination=false" \
    "$NAME-server" -f value -c id)

wait_field ERROR openstack server show "$server_id" -f value -c status
fault=$(openstack server show "$server_id" -f json -c fault 2>/dev/null)
grep -q "hw_incus_data_volume_fuse" <<<"$fault" || {
    echo "Server fault does not carry the designed image-capability" \
        "refusal: $fault" >&2
    exit 1
}
grep -q "has no attribute" <<<"$fault" && {
    echo "Server fault leaked a raw AttributeError: $fault" >&2
    exit 1
}

wait_field available openstack volume show "$volume_id" -f value -c status

openstack server delete --wait "$server_id"
server_id=
openstack volume delete "$volume_id"
volume_id=
trap - EXIT

echo "PASS initial data-volume failed-build rollback" \
    "image=$UNSUPPORTED_IMAGE volume_released=available residue=none"
