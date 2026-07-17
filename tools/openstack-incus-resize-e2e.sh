#!/usr/bin/env bash
# Validate flavor resize PID/cgroup limits, Placement and confirm/revert.

set -euo pipefail

IMAGE=${IMAGE:-ubuntu-noble-cloud-incus-ssh}
NETWORK=${NETWORK:-private}
SOURCE_HOST=${SOURCE_HOST:-ubuntu}
DEST_HOST=${DEST_HOST:-incus-node-02}
SOURCE_SSH=${SOURCE_SSH:-root@10.224.0.16}
DEST_SSH=${DEST_SSH:-root@10.224.0.17}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
SERVER=${SERVER:-incus-resize-e2e-$RANDOM}
TIMEOUT=${TIMEOUT:-180}

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)
small_flavor="${SERVER}-small"
large_flavor="${SERVER}-large"
server_id=

remote() {
    local host=$1
    shift
    "${SSH[@]}" "$host" "$@"
}

wait_status() {
    local expected=$1
    local deadline=$((SECONDS + TIMEOUT))
    local current
    while ((SECONDS < deadline)); do
        current=$(openstack server show "$server_id" -f value -c status \
            2>/dev/null || true)
        [[ "$current" == "$expected" ]] && return 0
        [[ "$current" == ERROR ]] && break
        sleep 2
    done
    openstack server show "$server_id" || true
    echo "Server did not reach $expected (current: ${current:-missing})" >&2
    return 1
}

assert_limits() {
    local host=$1 expected_pids=$2 expected_memory=$3 expected_cpus=$4
    [[ "$(remote "$host" \
        "incus exec '$instance_name' -- cat /sys/fs/cgroup/pids.max")" \
        == "$expected_pids" ]]
    [[ "$(remote "$host" \
        "incus exec '$instance_name' -- cat /sys/fs/cgroup/memory.max")" \
        == "$expected_memory" ]]
    [[ "$(remote "$host" "incus exec '$instance_name' -- nproc")" \
        == "$expected_cpus" ]]
}

assert_allocations() {
    local expected_vcpus=$1 expected_memory=$2 expected_disk=$3
    openstack resource provider allocation show "$server_id" -f json |
        python3 -c 'import json,sys
expected = dict(zip(("VCPU", "MEMORY_MB", "DISK_GB"), map(int, sys.argv[1:])))
allocations = json.load(sys.stdin)
assert any(item["resources"] == expected for item in allocations), allocations
' "$expected_vcpus" "$expected_memory" "$expected_disk"
}

cleanup() {
    if [[ -n "$server_id" ]]; then
        openstack server delete --wait "$server_id" >/dev/null 2>&1 || true
    fi
    openstack flavor delete "$small_flavor" "$large_flavor" \
        >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

openstack flavor create "$small_flavor" --vcpus 1 --ram 512 --disk 5 \
    >/dev/null
openstack flavor set "$small_flavor" --property incus:process_limit=2048
openstack flavor create "$large_flavor" --vcpus 2 --ram 1024 --disk 8 \
    >/dev/null
openstack flavor set "$large_flavor" --property incus:process_limit=4096

server_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$small_flavor" --image "$IMAGE" --network "$NETWORK" \
    --host "$SOURCE_HOST" --wait "$SERVER" -f value -c id)
instance_name=$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)
assert_limits "$SOURCE_SSH" 2048 512000000 1
assert_allocations 1 512 5
marker="resize-$server_id"
remote "$SOURCE_SSH" \
    "incus exec '$instance_name' -- sh -c \
     'printf %s "$marker" > /root/nova-resize-marker; sync'"

openstack server resize --flavor "$large_flavor" --wait "$server_id"
wait_status VERIFY_RESIZE
[[ "$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:host)" == "$DEST_HOST" ]]
assert_limits "$DEST_SSH" 4096 1024000000 2
[[ "$(remote "$DEST_SSH" \
    "incus exec '$instance_name' -- cat /root/nova-resize-marker")" == \
    "$marker" ]]

openstack server resize revert "$server_id"
wait_status ACTIVE
[[ "$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:host)" == "$SOURCE_HOST" ]]
assert_limits "$SOURCE_SSH" 2048 512000000 1
assert_allocations 1 512 5

openstack server resize --flavor "$large_flavor" --wait "$server_id"
wait_status VERIFY_RESIZE
assert_limits "$DEST_SSH" 4096 1024000000 2
openstack server resize confirm "$server_id"
wait_status ACTIVE
assert_limits "$DEST_SSH" 4096 1024000000 2
assert_allocations 2 1024 8

trap - EXIT INT TERM
openstack server delete --wait "$server_id"
server_id=
openstack flavor delete "$small_flavor" "$large_flavor"

echo "PASS instance=$instance_name pid=2048->4096 revert=ok confirm=ok"
