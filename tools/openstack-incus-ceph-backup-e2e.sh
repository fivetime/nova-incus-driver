#!/usr/bin/env bash
# Validate full and incremental Ceph backup plus restore through public APIs.

set -euo pipefail

RUN_DESTRUCTIVE=${RUN_DESTRUCTIVE:-false}
IMAGE=${IMAGE:?Set IMAGE}
FLAVOR=${FLAVOR:?Set FLAVOR}
NETWORK=${NETWORK:?Set NETWORK}
COMPUTE_HOST=${COMPUTE_HOST:?Set COMPUTE_HOST}
COMPUTE_SSH=${COMPUTE_SSH:?Set COMPUTE_SSH}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
SSH_KNOWN_HOSTS_FILE=${SSH_KNOWN_HOSTS_FILE:-$HOME/.ssh/known_hosts}
INCUS_PROJECT=${INCUS_PROJECT:-nova}
INCUS_RUNTIME_MODE=${INCUS_RUNTIME_MODE:-podman}
INCUS_RUNTIME_CONTAINER=${INCUS_RUNTIME_CONTAINER:-incus}
INCUS_KUBE_NAMESPACE=${INCUS_KUBE_NAMESPACE:-openstack}
INCUS_KUBE_NODE_MAP=${INCUS_KUBE_NODE_MAP:-}
VOLUME_TYPE=${VOLUME_TYPE:-ceph}
NAME=${NAME:-incus-ceph-backup-e2e-$RANDOM}
TIMEOUT=${TIMEOUT:-900}

[[ "$RUN_DESTRUCTIVE" == true ]] || {
    echo "Set RUN_DESTRUCTIVE=true to run this destructive case" >&2
    exit 2
}
[[ -r "$SSH_IDENTITY" && -r "$SSH_KNOWN_HOSTS_FILE" ]] || {
    echo "SSH identity and known_hosts must be readable" >&2
    exit 2
}
if [[ "$INCUS_RUNTIME_MODE" == kubernetes && -z "$INCUS_KUBE_NODE_MAP" ]]; then
    echo "Set INCUS_KUBE_NODE_MAP for Kubernetes mode" >&2
    exit 2
fi

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=yes
    -o "UserKnownHostsFile=$SSH_KNOWN_HOSTS_FILE")
server_id=
source_id=
restore_id=
full_id=
incremental_id=
snapshot_id=
clone_id=
instance_name=

remote() { "${SSH[@]}" "$COMPUTE_SSH" "$@"; }

kube_node_for_target() {
    local entry
    for entry in ${INCUS_KUBE_NODE_MAP//,/ }; do
        if [[ "${entry%%=*}" == "$COMPUTE_SSH" ]]; then
            printf '%s\n' "${entry#*=}"
            return 0
        fi
    done
    return 1
}

incus_remote() {
    local command_line node
    printf -v command_line '%q ' incus --project "$INCUS_PROJECT" "$@"
    case "$INCUS_RUNTIME_MODE" in
        podman)
            remote "podman exec $(printf '%q' "$INCUS_RUNTIME_CONTAINER") $command_line"
            ;;
        kubernetes)
            node=$(kube_node_for_target) || return 1
            remote "set -e; pods=\$(kubectl -n $(printf '%q' "$INCUS_KUBE_NAMESPACE") get pod -l application=incus --field-selector spec.nodeName=$(printf '%q' "$node") --no-headers -o custom-columns=NAME:.metadata.name); set -- \$pods; [ \$# -eq 1 ]; kubectl -n $(printf '%q' "$INCUS_KUBE_NAMESPACE") exec \"\$1\" -- $command_line"
            ;;
        *)
            return 2
            ;;
    esac
}

wait_value() {
    local command=$1 expected=$2 deadline=$((SECONDS + TIMEOUT)) current
    while ((SECONDS < deadline)); do
        current=$(eval "$command" 2>/dev/null || true)
        [[ "$current" == "$expected" ]] && return 0
        [[ "$current" == error* ]] && break
        sleep 2
    done
    echo "Timed out waiting for $expected (current: ${current:-missing})" >&2
    return 1
}

wait_absent() {
    local command=$1 deadline=$((SECONDS + TIMEOUT))
    while ((SECONDS < deadline)); do
        ! eval "$command" >/dev/null 2>&1 && return 0
        sleep 2
    done
    return 1
}

detach() {
    local volume=$1
    openstack server remove volume "$server_id" "$volume"
    wait_value "openstack volume show '$volume' -f value -c status" available
}

cleanup() {
    for volume in "$restore_id" "$source_id"; do
        if [[ -n "$server_id" && -n "$volume" ]]; then
            openstack server remove volume "$server_id" "$volume" \
                >/dev/null 2>&1 || true
        fi
    done
    if [[ -n "$incremental_id" ]]; then
        openstack volume backup delete "$incremental_id" \
            >/dev/null 2>&1 || true
    fi
    if [[ -n "$full_id" ]]; then
        openstack volume backup delete "$full_id" >/dev/null 2>&1 || true
    fi
    if [[ -n "$server_id" ]]; then
        openstack server delete --wait "$server_id" >/dev/null 2>&1 || true
    fi
    if [[ -n "$restore_id" ]]; then
        openstack volume delete "$restore_id" >/dev/null 2>&1 || true
    fi
    if [[ -n "$clone_id" ]]; then
        openstack volume delete "$clone_id" >/dev/null 2>&1 || true
    fi
    if [[ -n "$snapshot_id" ]]; then
        openstack volume snapshot delete "$snapshot_id" >/dev/null 2>&1 || true
    fi
    if [[ -n "$source_id" ]]; then
        openstack volume delete "$source_id" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

openstack volume service list --service cinder-backup -f value | \
    grep -Eq '(^|[[:space:]])up([[:space:]]|$)' || {
        echo "An up cinder-backup service is required" >&2
        exit 2
    }

server_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --image "$IMAGE" --network "$NETWORK" \
    --host "$COMPUTE_HOST" --wait "$NAME" -f value -c id)
instance_name=$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)
source_id=$(openstack volume create --size 1 --type "$VOLUME_TYPE" \
    "$NAME-source" -f value -c id)
wait_value "openstack volume show '$source_id' -f value -c status" available

openstack server add volume --device /dev/vdb "$server_id" "$source_id"
wait_value "openstack volume show '$source_id' -f value -c status" in-use
device=$(openstack server volume list "$server_id" -f value -c Device | head -n1)
marker="full-$source_id"
incus_remote exec "$instance_name" -- sh -c \
    "printf %s '$marker' | dd of='$device' bs=1 seek=4096 2>/dev/null; sync"
detach "$source_id"

snapshot_id=$(openstack volume snapshot create --volume "$source_id" \
    --property openstack-incus-e2e=true "$NAME-snapshot" -f value -c id)
wait_value "openstack volume snapshot show '$snapshot_id' -f value -c status" \
    available
clone_id=$(openstack volume create --snapshot "$snapshot_id" \
    --type "$VOLUME_TYPE" "$NAME-clone" -f value -c id)
wait_value "openstack volume show '$clone_id' -f value -c status" available
openstack server add volume --device /dev/vdb "$server_id" "$clone_id"
wait_value "openstack volume show '$clone_id' -f value -c status" in-use
clone_device=$(openstack server volume list "$server_id" -f value -c Device | head -n1)
cloned=$(incus_remote exec "$instance_name" -- sh -c \
    "dd if='$clone_device' bs=1 skip=4096 count=${#marker} 2>/dev/null")
[[ "$cloned" == "$marker" ]]
detach "$clone_id"
openstack volume delete "$clone_id"
wait_absent "openstack volume show '$clone_id'"
clone_id=
openstack volume snapshot delete "$snapshot_id"
wait_absent "openstack volume snapshot show '$snapshot_id'"
snapshot_id=

full_id=$(openstack volume backup create --name "$NAME-full" \
    "$source_id" -f value -c id)
wait_value "openstack volume backup show '$full_id' -f value -c status" \
    available

openstack server add volume --device /dev/vdb "$server_id" "$source_id"
wait_value "openstack volume show '$source_id' -f value -c status" in-use
marker="incremental-$source_id"
incus_remote exec "$instance_name" -- sh -c \
    "printf %s '$marker' | dd of='$device' bs=1 seek=8192 2>/dev/null; sync"
detach "$source_id"

incremental_id=$(openstack volume backup create --incremental \
    --name "$NAME-incremental" "$source_id" -f value -c id)
wait_value \
    "openstack volume backup show '$incremental_id' -f value -c status" \
    available
[[ "$(openstack volume backup show "$incremental_id" -f value \
    -c is_incremental)" == True ]]

restore_id=$(openstack volume create --size 1 --type "$VOLUME_TYPE" \
    "$NAME-restore" -f value -c id)
wait_value "openstack volume show '$restore_id' -f value -c status" available
openstack volume backup restore --force "$incremental_id" "$restore_id"
wait_value "openstack volume show '$restore_id' -f value -c status" available
openstack server add volume --device /dev/vdb "$server_id" "$restore_id"
wait_value "openstack volume show '$restore_id' -f value -c status" in-use
restored=$(incus_remote exec "$instance_name" -- sh -c \
    "dd if='$device' bs=1 skip=8192 count=${#marker} 2>/dev/null")
[[ "$restored" == "$marker" ]]
detach "$restore_id"

openstack volume backup delete "$incremental_id"
wait_absent "openstack volume backup show '$incremental_id'"
incremental_id=
openstack volume backup delete "$full_id"
wait_absent "openstack volume backup show '$full_id'"
full_id=
openstack volume delete "$restore_id"
wait_absent "openstack volume show '$restore_id'"
restore_id=
openstack volume delete "$source_id"
wait_absent "openstack volume show '$source_id'"
source_id=
openstack server delete --wait "$server_id"
server_id=

trap - EXIT INT TERM
echo "PASS snapshot/clone, full backup, incremental backup, restore, and cleanup"
