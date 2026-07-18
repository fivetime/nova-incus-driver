#!/usr/bin/env bash
# Validate pause/unpause and shelve/unshelve for a Cinder BFV container.

set -euo pipefail

IMAGE=${IMAGE:?Set IMAGE to a raw rootfs-directory Glance image}
FLAVOR=${FLAVOR:-ds512M}
NETWORK=${NETWORK:-private}
VOLUME_SIZE=${VOLUME_SIZE:-5}
VOLUME_TYPE=${VOLUME_TYPE:-ceph}
SOURCE_HOST=${SOURCE_HOST:-incus-node-01}
DEST_HOST=${DEST_HOST:-incus-node-02}
COMPUTE_HOSTS=${COMPUTE_HOSTS:-incus-node-01,incus-node-02,incus-node-03}
COMPUTE_SSH=${COMPUTE_SSH:-root@10.224.0.21,root@10.224.0.17,root@10.224.0.22}
CONTROLLER_SSH=${CONTROLLER_SSH:-root@10.224.0.21}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
INCUS_PROJECT=${INCUS_PROJECT:-nova}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
TIMEOUT=${TIMEOUT:-600}
NAME=${NAME:-incus-bfv-lifecycle-$RANDOM}

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)
IFS=, read -r -a compute_hosts <<< "$COMPUTE_HOSTS"
IFS=, read -r -a compute_ssh <<< "$COMPUTE_SSH"

server_id=
volume_id=
instance_name=
fixed_ip=

(( ${#compute_hosts[@]} == ${#compute_ssh[@]} )) || {
    echo "COMPUTE_HOSTS and COMPUTE_SSH must have the same entry count" >&2
    exit 2
}

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
        "podman exec -i incus incus --project $(printf '%q' "$INCUS_PROJECT") $command_line"
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

wait_absent() {
    local deadline=$((SECONDS + TIMEOUT))
    while ((SECONDS < deadline)); do
        "$@" >/dev/null 2>&1 || return 0
        sleep 2
    done
    fail "timed out waiting for resource deletion"
}

server_status() {
    openstack server show "$server_id" -f value -c status
}

volume_status() {
    openstack volume show "$volume_id" -f value -c status
}

attached_host_count() {
    openstack volume show "$volume_id" -f json -c attachments |
        python3 -c 'import json,sys
print(len(json.load(sys.stdin)["attachments"]))
'
}

host_ssh() {
    local wanted=$1 index
    for index in "${!compute_hosts[@]}"; do
        if [[ "${compute_hosts[$index]}" == "$wanted" ]]; then
            printf '%s\n' "${compute_ssh[$index]}"
            return
        fi
    done
    fail "Nova selected unmapped compute host $wanted"
}

assert_single_owner() {
    local expected_host=$1 expected_ssh owner_count=0 index state
    expected_ssh=$(host_ssh "$expected_host")
    for index in "${!compute_hosts[@]}"; do
        state=$(incus "${compute_ssh[$index]}" list "$instance_name" \
            --format csv -c s 2>/dev/null || true)
        if [[ -n "$state" ]]; then
            ((owner_count += 1))
            [[ "${compute_ssh[$index]}" == "$expected_ssh" ]] ||
                fail "Incus owner is not on Nova host $expected_host"
            [[ "$state" == RUNNING ]] ||
                fail "Incus owner is not running (state=$state)"
        fi
    done
    ((owner_count == 1)) || fail "expected one Incus owner, found $owner_count"
}

cleanup() {
    local exit_status=$?
    if [[ -n "$server_id" ]]; then
        openstack server delete --wait "$server_id" >/dev/null 2>&1 || true
    fi
    if [[ -n "$volume_id" ]]; then
        wait_value available volume_status >/dev/null 2>&1 || true
        openstack volume delete "$volume_id" >/dev/null 2>&1 || true
        wait_absent openstack volume show "$volume_id" >/dev/null 2>&1 || true
    fi
    exit "$exit_status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

volume_id=$(openstack volume create --image "$IMAGE" --size "$VOLUME_SIZE" \
    --type "$VOLUME_TYPE" "$NAME-root" -f value -c id)
wait_value available volume_status

server_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --volume "$volume_id" --network "$NETWORK" \
    --config-drive true --host "$SOURCE_HOST" --wait "$NAME" -f value -c id)
wait_value ACTIVE server_status
instance_name=$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)
fixed_ip=$(openstack server show "$server_id" -f json -c addresses |
    python3 -c 'import json,sys
addresses = next(iter(json.load(sys.stdin)["addresses"].values()))
print(addresses[0] if isinstance(addresses[0], str) else addresses[0]["addr"])
')
source_ssh=$(host_ssh "$SOURCE_HOST")

marker="shelve-$server_id"
printf '%s' "$marker" |
    incus "$source_ssh" exec "$instance_name" -- tee /root/shelve-marker \
    >/dev/null
[[ "$(incus "$source_ssh" exec "$instance_name" -- \
    cat /root/shelve-marker)" == "$marker" ]]

openstack server pause "$server_id"
wait_value PAUSED server_status
[[ "$(incus "$source_ssh" list "$instance_name" --format csv -c s)" == \
    FROZEN ]]
openstack server unpause "$server_id"
wait_value ACTIVE server_status
assert_single_owner "$SOURCE_HOST"

openstack server shelve "$server_id"
wait_value SHELVED_OFFLOADED server_status
[[ "$(attached_host_count)" == 0 ]]
[[ "$(volume_status)" == reserved ]]
for index in "${!compute_hosts[@]}"; do
    ! incus "${compute_ssh[$index]}" info "$instance_name" >/dev/null 2>&1 ||
        fail "offloaded instance remains on ${compute_hosts[$index]}"
done

[[ "$DEST_HOST" != "$SOURCE_HOST" ]] ||
    fail "DEST_HOST must differ from SOURCE_HOST"
openstack --os-compute-api-version 2.91 server unshelve \
    --host "$DEST_HOST" "$server_id"
wait_value ACTIVE server_status
owner_host=$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:host)
[[ "$owner_host" == "$DEST_HOST" ]] ||
    fail "unshelve selected $owner_host instead of $DEST_HOST"
owner_ssh=$(host_ssh "$owner_host")
assert_single_owner "$owner_host"
[[ "$(attached_host_count)" == 1 ]]
[[ "$(volume_status)" == in-use ]]
[[ "$(incus "$owner_ssh" exec "$instance_name" -- \
    cat /root/shelve-marker)" == "$marker" ]]
incus "$owner_ssh" exec "$instance_name" -- \
    test -r /config-drive/openstack/latest/meta_data.json
if incus "$owner_ssh" exec "$instance_name" -- \
        touch /config-drive/write-must-fail >/dev/null 2>&1; then
    fail "config-drive is writable after unshelve"
fi
[[ "$(openstack server show "$server_id" -f json -c addresses |
    python3 -c 'import json,sys
addresses = next(iter(json.load(sys.stdin)["addresses"].values()))
print(addresses[0] if isinstance(addresses[0], str) else addresses[0]["addr"])
')" == "$fixed_ip" ]]

echo "PASS server=$server_id volume=$volume_id owner=$owner_host ip=$fixed_ip"
