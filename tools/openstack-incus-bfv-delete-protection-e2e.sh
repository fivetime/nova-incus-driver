#!/usr/bin/env bash
# Prove that deleting a BFV server releases, but never deletes, its Cinder root.

set -euo pipefail

RUN_DESTRUCTIVE=${RUN_DESTRUCTIVE:-false}
IMAGE=${IMAGE:?Set IMAGE to an admitted BFV Glance image ID}
FLAVOR=${FLAVOR:?Set FLAVOR to an Incus-compatible flavor ID}
NETWORK=${NETWORK:?Set NETWORK to a tenant network ID}
VOLUME_TYPE=${VOLUME_TYPE:?Set VOLUME_TYPE to the Cinder volume type under test}
VOLUME_SIZE=${VOLUME_SIZE:-5}
COMPUTE_HOSTS=${COMPUTE_HOSTS:?Set COMPUTE_HOSTS to comma-separated Nova hosts}
COMPUTE_SSH=${COMPUTE_SSH:?Set COMPUTE_SSH to matching SSH targets}
CONTROLLER_SSH=${CONTROLLER_SSH:?Set CONTROLLER_SSH to the OpenStack/Ceph controller}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
SSH_KNOWN_HOSTS_FILE=${SSH_KNOWN_HOSTS_FILE:-$HOME/.ssh/known_hosts}
INCUS_PROJECT=${INCUS_PROJECT:-nova}
CINDER_RBD_POOL=${CINDER_RBD_POOL:?Set CINDER_RBD_POOL to the selected Cinder RBD pool}
CINDER_RBD_USER=${CINDER_RBD_USER:-cinder}
TIMEOUT=${TIMEOUT:-900}
NAME=${NAME:-incus-bfv-delete-protection-$RANDOM}

if [[ "$RUN_DESTRUCTIVE" != true ]]; then
    echo "Refusing destructive BFV delete-protection E2E; set RUN_DESTRUCTIVE=true" >&2
    exit 2
fi

[[ -f "$SSH_IDENTITY" && -r "$SSH_IDENTITY" ]] || {
    echo "SSH identity is not a readable regular file: $SSH_IDENTITY" >&2
    exit 2
}
[[ -f "$SSH_KNOWN_HOSTS_FILE" && -r "$SSH_KNOWN_HOSTS_FILE" ]] || {
    echo "SSH known_hosts is not a readable regular file: $SSH_KNOWN_HOSTS_FILE" >&2
    exit 2
}

IFS=, read -r -a compute_hosts <<< "$COMPUTE_HOSTS"
IFS=, read -r -a compute_ssh <<< "$COMPUTE_SSH"
(( ${#compute_hosts[@]} > 0 )) || {
    echo "COMPUTE_HOSTS cannot be empty" >&2
    exit 2
}
(( ${#compute_hosts[@]} == ${#compute_ssh[@]} )) || {
    echo "COMPUTE_HOSTS and COMPUTE_SSH must have the same entry count" >&2
    exit 2
}

SSH=(
    ssh
    -i "$SSH_IDENTITY"
    -o BatchMode=yes
    -o StrictHostKeyChecking=yes
    -o "UserKnownHostsFile=$SSH_KNOWN_HOSTS_FILE"
)
server_id=
volume_id=
instance_name=
original_rbd_image_id=
original_rbd_pool_id=

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
        [[ "$current" == ERROR || "$current" == error* ]] && break
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

cinder_attachment_count() {
    openstack volume attachment list -f json | python3 -c '
import json
import sys

volume_id = sys.argv[1]
attachments = json.load(sys.stdin)
print(sum(1 for item in attachments
          if item.get("Volume ID", item.get("volume_id")) == volume_id))
' "$volume_id"
}

volume_attachment_count() {
    openstack volume show "$volume_id" -f json -c attachments |
        python3 -c 'import json, sys; print(len(json.load(sys.stdin)["attachments"]))'
}

rbd_image_name() {
    printf 'volume-%s\n' "$volume_id"
}

rbd_image_exists() {
    local image command_line
    image=$(rbd_image_name)
    printf -v command_line '%q ' rbd --id "$CINDER_RBD_USER" info \
        "$CINDER_RBD_POOL/$image"
    remote "$CONTROLLER_SSH" "$command_line" >/dev/null
}

rbd_image_id() {
    local image command_line
    image=$(rbd_image_name)
    printf -v command_line \
        'rbd --id %q --pool %q info %q --format json | jq -er .id' \
        "$CINDER_RBD_USER" "$CINDER_RBD_POOL" "$image"
    remote "$CONTROLLER_SSH" "$command_line"
}

rbd_pool_id() {
    local command_line
    printf -v command_line \
        'rados --id %q df --format json | jq -er --arg pool %q %q' \
        "$CINDER_RBD_USER" "$CINDER_RBD_POOL" \
        '.pools[] | select(.name == $pool) | .id'
    remote "$CONTROLLER_SSH" "$command_line"
}

exact_rbd_object_exists() {
    local command_line
    printf -v command_line '%q ' rados --id "$CINDER_RBD_USER" \
        --pool "$CINDER_RBD_POOL" stat \
        "rbd_header.$original_rbd_image_id"
    remote "$CONTROLLER_SSH" "$command_line" >/dev/null
}

watcher_count() {
    local image_id command_line output
    image_id=${original_rbd_image_id:-$(rbd_image_id)}
    [[ "$image_id" =~ ^[0-9a-f]+$ ]] ||
        fail "cannot resolve RBD image ID for $(rbd_image_name)"
    printf -v command_line '%q ' rados --id "$CINDER_RBD_USER" -p \
        "$CINDER_RBD_POOL" listwatchers "rbd_header.$image_id"
    output=$(remote "$CONTROLLER_SSH" "$command_line") ||
        fail "cannot query Ceph watchers for $(rbd_image_name)"
    sed '/^[[:space:]]*$/d' <<<"$output" | wc -l
}

host_rbd_mapping_count() {
    local host=$1 image command_line python_program
    image=$(rbd_image_name)
    python_program='import json,pathlib,sys
pool,image,image_id,pool_id=sys.argv[1:]
count=0
for row in json.load(sys.stdin):
    if row.get("pool") != pool or row.get("name") != image:
        continue
    root=pathlib.Path("/sys/bus/rbd/devices") / str(row["id"])
    if ((root / "image_id").read_text().strip() == image_id and
            (root / "pool_id").read_text().strip() == pool_id):
        count += 1
print(count)'
    printf -v command_line \
        'set -o pipefail; rbd device list --format json --id %q | python3 -c %q %q %q %q %q' \
        "$CINDER_RBD_USER" "$python_program" "$CINDER_RBD_POOL" \
        "$image" "$original_rbd_image_id" "$original_rbd_pool_id"
    remote "$host" "$command_line"
}

instance_or_profile_exists() {
    local host=$1
    incus "$host" info "$instance_name" >/dev/null 2>&1 ||
        incus "$host" profile show "$instance_name" >/dev/null 2>&1
}

assert_runtime_released() {
    local index host mappings
    [[ "$(cinder_attachment_count)" == 0 ]] ||
        fail "Cinder still reports an attachment for root volume $volume_id"
    [[ "$(volume_attachment_count)" == 0 ]] ||
        fail "Cinder volume still stores attachment metadata for $volume_id"
    [[ "$(watcher_count)" == 0 ]] ||
        fail "Ceph still reports a watcher for BFV root $volume_id"

    for index in "${!compute_hosts[@]}"; do
        host=${compute_ssh[$index]}
        ! instance_or_profile_exists "$host" ||
            fail "${compute_hosts[$index]} still has Incus instance/profile $instance_name"
        mappings=$(host_rbd_mapping_count "$host")
        [[ "$mappings" == 0 ]] ||
            fail "${compute_hosts[$index]} still maps BFV root $volume_id"
    done
}

cleanup() {
    local exit_status=$?
    if [[ -n "$server_id" ]]; then
        openstack server delete --wait "$server_id" >/dev/null 2>&1 || true
        wait_absent openstack server show "$server_id" >/dev/null 2>&1 || true
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
rbd_image_exists || fail "Cinder BFV root RBD image was not created"
original_rbd_image_id=$(rbd_image_id)
original_rbd_pool_id=$(rbd_pool_id)
[[ "$original_rbd_image_id" =~ ^[0-9a-f]+$ ]] || \
    fail "Cinder BFV root has no immutable RBD image ID"
[[ "$original_rbd_pool_id" =~ ^[0-9]+$ ]] || \
    fail "Cinder BFV root has no immutable RBD pool ID"

server_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --volume "$volume_id" --network "$NETWORK" \
    --config-drive true --wait "$NAME" -f value -c id)
wait_value ACTIVE server_status
instance_name=$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)

openstack server delete --wait "$server_id"
wait_absent openstack server show "$server_id"
server_id=
wait_value available volume_status
rbd_image_exists ||
    fail "Nova/Incus deletion removed the Cinder-owned BFV root RBD image"
[[ "$(rbd_image_id)" == "$original_rbd_image_id" ]] || \
    fail "Nova/Incus deletion replaced the Cinder-owned BFV RBD image"
[[ "$(rbd_pool_id)" == "$original_rbd_pool_id" ]] || \
    fail "Nova/Incus deletion changed the Cinder-owned BFV RBD pool"
assert_runtime_released

openstack volume delete "$volume_id"
wait_absent openstack volume show "$volume_id"
if rbd_image_exists; then
    fail "Cinder deletion did not remove BFV root RBD image"
fi
if exact_rbd_object_exists; then
    fail "Cinder deletion retained the exact BFV root RBD object"
fi
volume_id=

trap - EXIT INT TERM
echo "PASS BFV root deletion protection: Nova released $NAME; Cinder deleted its RBD"
