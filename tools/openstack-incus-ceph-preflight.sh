#!/usr/bin/env bash
# Read-only validation for an existing Incus and Cinder Ceph deployment.

set -euo pipefail

ROOTFS_POOL=${ROOTFS_POOL:?Set ROOTFS_POOL to the Incus-only RBD pool}
DEST_ROOTFS_POOL=${DEST_ROOTFS_POOL:?Set DEST_ROOTFS_POOL to the destination Incus RBD pool}
CINDER_POOL=${CINDER_POOL:?Set CINDER_POOL to the Cinder-only RBD pool}
GLANCE_POOL=${GLANCE_POOL:-}
ROOTFS_USER=${ROOTFS_USER:-incus}
DEST_ROOTFS_USER=${DEST_ROOTFS_USER:-incus-node02}
CINDER_USER=${CINDER_USER:-cinder}
CEPH_CLUSTER=${CEPH_CLUSTER:-ceph}
CEPH_CONF=${CEPH_CONF:-/etc/ceph/ceph.conf}
INCUS_POOL_NAME=${INCUS_POOL_NAME:-incus-ceph}
DEST_INCUS_POOL_NAME=${DEST_INCUS_POOL_NAME:-incus-ceph-node02}
SOURCE_SSH=${SOURCE_SSH:-root@10.32.32.132}
DEST_SSH=${DEST_SSH:-root@10.32.32.131}
CONTROLLER_SSH=${CONTROLLER_SSH:-$SOURCE_SSH}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
CHECK_CONFIGURED_BACKENDS=${CHECK_CONFIGURED_BACKENDS:-False}

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

remote() {
    local host=$1
    shift
    "${SSH[@]}" "$host" "$@"
}

validate_name() {
    local label=$1 value=$2
    [[ "$value" =~ ^[A-Za-z0-9_.-]+$ ]] || \
        fail "$label contains unsupported shell characters: $value"
}

validate_client() {
    local host=$1 user=$2 pool=$3 keyring
    keyring="/etc/ceph/${CEPH_CLUSTER}.client.${user}.keyring"

    echo "Checking client.${user} access to ${pool} on ${host}"
    remote "$host" \
        "command -v ceph >/dev/null && command -v rbd >/dev/null && \
         test -r '$CEPH_CONF' && test -r '$keyring' && \
         ceph --conf '$CEPH_CONF' --name 'client.$user' health >/dev/null && \
         rbd --conf '$CEPH_CONF' --id '$user' --pool '$pool' ls >/dev/null"
}

validate_incus_backend() {
    local host=$1 pool_name=$2 expected_source=$3 driver source
    driver=$(remote "$host" \
        "incus query '/1.0/storage-pools/$pool_name' | \
         jq -r '.driver'")
    source=$(remote "$host" \
        "incus query '/1.0/storage-pools/$pool_name' | \
         jq -r '.config.source'")
    [[ "$driver" == ceph ]] || \
        fail "$host Incus pool driver is $driver, expected ceph"
    [[ "$source" == "$expected_source" ]] || \
        fail "$host Incus pool source is $source, expected $expected_source"
}

validate_cinder_backend() {
    local actual_pool actual_user
    actual_pool=$(remote "$CONTROLLER_SSH" \
        "crudini --get /etc/cinder/cinder.conf ceph rbd_pool")
    actual_user=$(remote "$CONTROLLER_SSH" \
        "crudini --get /etc/cinder/cinder.conf ceph rbd_user")
    [[ "$actual_pool" == "$CINDER_POOL" ]] || \
        fail "Cinder rbd_pool is $actual_pool, expected $CINDER_POOL"
    [[ "$actual_user" == "$CINDER_USER" ]] || \
        fail "Cinder rbd_user is $actual_user, expected $CINDER_USER"
}

validate_name ROOTFS_POOL "$ROOTFS_POOL"
validate_name DEST_ROOTFS_POOL "$DEST_ROOTFS_POOL"
validate_name CINDER_POOL "$CINDER_POOL"
[[ -z "$GLANCE_POOL" ]] || validate_name GLANCE_POOL "$GLANCE_POOL"
validate_name ROOTFS_USER "$ROOTFS_USER"
validate_name DEST_ROOTFS_USER "$DEST_ROOTFS_USER"
validate_name CINDER_USER "$CINDER_USER"
validate_name CEPH_CLUSTER "$CEPH_CLUSTER"
validate_name INCUS_POOL_NAME "$INCUS_POOL_NAME"
validate_name DEST_INCUS_POOL_NAME "$DEST_INCUS_POOL_NAME"
[[ "$ROOTFS_POOL" != "$DEST_ROOTFS_POOL" ]] || \
    fail "Independent Incus computes must not share an RBD pool"
[[ "$ROOTFS_POOL" != "$CINDER_POOL" && \
   "$DEST_ROOTFS_POOL" != "$CINDER_POOL" ]] || \
    fail "Incus rootfs and Cinder must not share an RBD pool"

command -v ssh >/dev/null || fail "ssh is required"

validate_client "$SOURCE_SSH" "$ROOTFS_USER" "$ROOTFS_POOL"
validate_client "$DEST_SSH" "$DEST_ROOTFS_USER" "$DEST_ROOTFS_POOL"
validate_client "$SOURCE_SSH" "$CINDER_USER" "$CINDER_POOL"
validate_client "$DEST_SSH" "$CINDER_USER" "$CINDER_POOL"
validate_client "$CONTROLLER_SSH" "$CINDER_USER" "$CINDER_POOL"
if [[ -n "$GLANCE_POOL" ]]; then
    validate_client "$SOURCE_SSH" "$CINDER_USER" "$GLANCE_POOL"
    validate_client "$DEST_SSH" "$CINDER_USER" "$GLANCE_POOL"
    validate_client "$CONTROLLER_SSH" "$CINDER_USER" "$GLANCE_POOL"
fi

if [[ "${CHECK_CONFIGURED_BACKENDS,,}" == true ]]; then
    validate_incus_backend \
        "$SOURCE_SSH" "$INCUS_POOL_NAME" "$ROOTFS_POOL"
    validate_incus_backend \
        "$DEST_SSH" "$DEST_INCUS_POOL_NAME" "$DEST_ROOTFS_POOL"
    validate_cinder_backend
fi

echo "PASS Ceph credentials, isolated pools and host access"
