#!/usr/bin/env bash
# Validate Ceph-backed Incus rootfs persistence, quota and host containment.

set -euo pipefail

RUN_DESTRUCTIVE=${RUN_DESTRUCTIVE:-false}
if [[ "$RUN_DESTRUCTIVE" != true ]]; then
    echo "Refusing destructive E2E; set RUN_DESTRUCTIVE=true" >&2
    exit 2
fi

IMAGE=${IMAGE:-ubuntu-noble-cloud-incus-tempest}
FLAVOR=${FLAVOR:-d1}
NETWORK=${NETWORK:-private}
SOURCE_HOST=${SOURCE_HOST:-incus-node-01}
SOURCE_SSH=${SOURCE_SSH:-root@10.32.32.132}
INCUS_PROJECT=${INCUS_PROJECT:-nova}
INCUS_POOL_NAME=${INCUS_POOL_NAME:-incus-ceph}
WRITE_MIB=${WRITE_MIB:-1024}
MAX_HOST_GROWTH_MIB=${MAX_HOST_GROWTH_MIB:-256}
TIMEOUT=${TIMEOUT:-360}
NAME=${NAME:-incus-ceph-rootfs-$RANDOM}

if [[ "$SOURCE_SSH" != local ]]; then
    SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
    SSH_KNOWN_HOSTS_FILE=${SSH_KNOWN_HOSTS_FILE:-$HOME/.ssh/known_hosts}
    [[ -f "$SSH_IDENTITY" && -r "$SSH_IDENTITY" ]] || {
        echo "SSH identity is not a readable regular file: $SSH_IDENTITY" >&2
        exit 2
    }
    [[ -f "$SSH_KNOWN_HOSTS_FILE" &&
       -r "$SSH_KNOWN_HOSTS_FILE" ]] || {
        echo "SSH known_hosts is not a readable regular file: $SSH_KNOWN_HOSTS_FILE" >&2
        exit 2
    }
    SSH=(
        ssh
        -i "$SSH_IDENTITY"
        -o BatchMode=yes
        -o StrictHostKeyChecking=yes
        -o "UserKnownHostsFile=$SSH_KNOWN_HOSTS_FILE"
    )
fi
server_id=
instance_name=

remote() {
    if [[ "$SOURCE_SSH" == local ]]; then
        bash -c "$*"
    else
        "${SSH[@]}" "$SOURCE_SSH" "$@"
    fi
}

incus() {
    local command_line
    if [[ "${1:-}" == query ]]; then
        printf -v command_line '%q ' podman exec incus incus "$@"
    else
        printf -v command_line '%q ' \
            podman exec incus incus --project "$INCUS_PROJECT" "$@"
    fi
    remote "$command_line"
}

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

wait_value() {
    local command=$1 expected=$2 deadline=$((SECONDS + TIMEOUT)) current
    while ((SECONDS < deadline)); do
        current=$(eval "$command" 2>/dev/null || true)
        [[ "$current" == "$expected" ]] && return 0
        sleep 2
    done
    fail "timed out waiting for $expected (current: ${current:-missing})"
}

cleanup() {
    [[ -n "$server_id" ]] && \
        openstack server delete --wait "$server_id" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

[[ "$WRITE_MIB" =~ ^[1-9][0-9]*$ ]] || fail "WRITE_MIB must be positive"
[[ "$MAX_HOST_GROWTH_MIB" =~ ^[1-9][0-9]*$ ]] || \
    fail "MAX_HOST_GROWTH_MIB must be positive"

pool_driver=$(incus query "/1.0/storage-pools/$INCUS_POOL_NAME" | \
    jq -r '.driver')
[[ "$pool_driver" == ceph ]] || \
    fail "Incus pool $INCUS_POOL_NAME uses $pool_driver, expected ceph"

server_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --image "$IMAGE" --network "$NETWORK" \
    --host "$SOURCE_HOST" "$NAME" -f value -c id)
wait_value "openstack server show '$server_id' -f value -c status" ACTIVE
instance_name=$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)

incus config get --expanded "$instance_name" security.privileged | \
    grep -Fxi false >/dev/null || fail "instance is not explicitly unprivileged"
[[ -z "$(incus config get --expanded "$instance_name" raw.idmap)" ]] || \
    fail "instance has a raw.idmap override"
incus exec "$instance_name" -- sh -c 'test "$(id -u)" = 0'

root_gib=$(openstack flavor show "$FLAVOR" -f value -c disk)
[[ "$root_gib" =~ ^[1-9][0-9]*$ ]] || fail "flavor root disk is not finite"
profile_size=$(incus profile device get "$instance_name" root size)
[[ "$profile_size" == "${root_gib}GB" ]] || \
    fail "Incus root quota is $profile_size, expected ${root_gib}GB"

# Measure after image import/start so the Glance and Incus image caches do not
# get mistaken for tenant rootfs consumption.
host_used_before=$(remote "df -B1 --output=used /var/lib/incus | tail -n1")
marker="ceph-rootfs-$server_id"
incus exec "$instance_name" -- sh -ceu \
    "printf %s '$marker' > /var/tmp/persistence-marker; \
     fallocate -l ${WRITE_MIB}MiB /var/tmp/tenant-data; sync"
host_used_during=$(remote "df -B1 --output=used /var/lib/incus | tail -n1")
host_growth=$((host_used_during - host_used_before))
max_growth=$((MAX_HOST_GROWTH_MIB * 1024 * 1024))
((host_growth <= max_growth)) || \
    fail "host system filesystem grew by $host_growth bytes"

openstack server reboot --hard "$server_id"
wait_value "openstack server show '$server_id' -f value -c status" ACTIVE
restored=$(incus exec "$instance_name" -- cat /var/tmp/persistence-marker)
[[ "$restored" == "$marker" ]] || fail "rootfs marker did not persist"

if incus exec "$instance_name" -- \
        fallocate -l "$((root_gib + 1))GiB" /var/tmp/quota-overflow \
        >/dev/null 2>&1; then
    fail "rootfs allocation larger than the flavor quota succeeded"
fi
incus exec "$instance_name" -- rm -f \
    /var/tmp/quota-overflow /var/tmp/tenant-data

trap - EXIT INT TERM
openstack server delete --wait "$server_id"
server_id=
echo "PASS Ceph rootfs persistence, quota, container root and host containment"
