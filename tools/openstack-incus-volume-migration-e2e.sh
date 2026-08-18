#!/usr/bin/env bash
# Validate Cinder attach, data persistence, cold migration and detach.

set -euo pipefail

RUN_DESTRUCTIVE=${RUN_DESTRUCTIVE:-false}
IMAGE=${IMAGE:?Set IMAGE}
FLAVOR=${FLAVOR:?Set FLAVOR}
NETWORK=${NETWORK:?Set NETWORK}
SOURCE_HOST=${SOURCE_HOST:?Set SOURCE_HOST}
DEST_HOST=${DEST_HOST:?Set DEST_HOST}
SOURCE_SSH=${SOURCE_SSH:?Set SOURCE_SSH}
DEST_SSH=${DEST_SSH:?Set DEST_SSH}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
SSH_KNOWN_HOSTS_FILE=${SSH_KNOWN_HOSTS_FILE:-$HOME/.ssh/known_hosts}
INCUS_PROJECT=${INCUS_PROJECT:-nova}
INCUS_RUNTIME_MODE=${INCUS_RUNTIME_MODE:-podman}
INCUS_RUNTIME_CONTAINER=${INCUS_RUNTIME_CONTAINER:-incus}
INCUS_KUBE_NAMESPACE=${INCUS_KUBE_NAMESPACE:-openstack}
INCUS_KUBE_NODE_MAP=${INCUS_KUBE_NODE_MAP:-}
NAME=${NAME:-incus-volume-e2e-$RANDOM}
DEVICE=${DEVICE:-/dev/vdb}
EXTENDED_SIZE_GIB=${EXTENDED_SIZE_GIB:-2}
VOLUME_TYPE=${VOLUME_TYPE:-ceph}
TIMEOUT=${TIMEOUT:-360}

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
volume_id=
instance_name=

remote() { local host=$1; shift; "${SSH[@]}" "$host" "$@"; }

kube_node_for_target() {
    local target=$1 entry
    for entry in ${INCUS_KUBE_NODE_MAP//,/ }; do
        if [[ "${entry%%=*}" == "$target" ]]; then
            printf '%s\n' "${entry#*=}"
            return 0
        fi
    done
    return 1
}

incus_remote() {
    local host=$1 command_line node
    shift
    printf -v command_line '%q ' incus --project "$INCUS_PROJECT" "$@"
    case "$INCUS_RUNTIME_MODE" in
        podman)
            remote "$host" \
                "podman exec $(printf '%q' "$INCUS_RUNTIME_CONTAINER") $command_line"
            ;;
        kubernetes)
            node=$(kube_node_for_target "$host") || return 1
            remote "$host" "set -e; pods=\$(kubectl -n $(printf '%q' "$INCUS_KUBE_NAMESPACE") get pod -l application=incus --field-selector spec.nodeName=$(printf '%q' "$node") --no-headers -o custom-columns=NAME:.metadata.name); set -- \$pods; [ \$# -eq 1 ]; kubectl -n $(printf '%q' "$INCUS_KUBE_NAMESPACE") exec \"\$1\" -- $command_line"
            ;;
        *)
            echo "INCUS_RUNTIME_MODE must be podman or kubernetes" >&2
            return 2
            ;;
    esac
}

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

wait_device_size() {
    local host=$1 expected=$2 deadline=$((SECONDS + TIMEOUT)) current
    while ((SECONDS < deadline)); do
        current=$(incus_remote "$host" exec "$instance_name" -- \
            blockdev --getsize64 "$DEVICE" 2>/dev/null || true)
        [[ "$current" =~ ^[0-9]+$ ]] && ((current >= expected)) && return 0
        sleep 2
    done
    echo "Device size did not reach $expected bytes (current: ${current:-missing})" >&2
    return 1
}

cleanup() {
    if [[ -n "$server_id" && -n "$volume_id" ]]; then
        openstack server remove volume "$server_id" "$volume_id" \
            >/dev/null 2>&1 || true
    fi
    [[ -n "$server_id" ]] && \
        openstack server delete --wait "$server_id" >/dev/null 2>&1 || true
    [[ -n "$volume_id" ]] && \
        openstack volume delete "$volume_id" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

[[ "$DEVICE" =~ ^/dev/[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
    echo "DEVICE must be a direct block device path under /dev" >&2
    exit 2
}
[[ "$EXTENDED_SIZE_GIB" =~ ^[1-9][0-9]*$ ]] || {
    echo "EXTENDED_SIZE_GIB must be a positive integer" >&2
    exit 2
}

if ! openstack volume service list >/dev/null 2>&1; then
    echo "Cinder v3 endpoint is required for this test" >&2
    exit 2
fi

volume_id=$(openstack volume create --size 1 --type "$VOLUME_TYPE" \
    "$NAME-volume" -f value -c id)
wait_value "openstack volume show '$volume_id' -f value -c status" available
server_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --image "$IMAGE" --network "$NETWORK" \
    --host "$SOURCE_HOST" --wait "$NAME" -f value -c id)
instance_name=$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)

openstack server add volume --device "$DEVICE" "$server_id" "$volume_id"
wait_value "openstack volume show '$volume_id' -f value -c status" in-use
DEVICE=$(openstack server volume list "$server_id" -f value -c Device | \
    head -n1)
[[ "$DEVICE" =~ ^/dev/[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
    echo "Nova returned an invalid volume device path: $DEVICE" >&2
    exit 1
}
incus_remote "$SOURCE_SSH" profile device show "$instance_name" |
    grep -F "$volume_id:"
incus_remote "$SOURCE_SSH" profile get "$instance_name" \
    "user.openstack.volume.$volume_id" | python3 -c \
    'import json,sys; data=json.load(sys.stdin); assert data["mountpoint"].startswith("/dev/")'
marker="cinder-$server_id"
incus_remote "$SOURCE_SSH" exec "$instance_name" -- sh -c \
    "command -v fuse2fs >/dev/null; mkfs.ext4 -F '$DEVICE' >/dev/null; \
     mkdir -p /mnt/cinder; fuse2fs '$DEVICE' /mnt/cinder; \
     printf %s '$marker' > /mnt/cinder/marker; \
     sync; umount /mnt/cinder"

openstack --os-volume-api-version 3.42 volume set \
    --size "$EXTENDED_SIZE_GIB" "$volume_id"
wait_value "openstack volume show '$volume_id' -f value -c size" \
    "$EXTENDED_SIZE_GIB"
wait_device_size "$SOURCE_SSH" "$((EXTENDED_SIZE_GIB * 1024 * 1024 * 1024))"

openstack --os-compute-api-version 2.56 server migrate \
    --host "$DEST_HOST" "$server_id" || true
wait_value "openstack server show '$server_id' -f value -c status" VERIFY_RESIZE
restored_marker=$(incus_remote "$DEST_SSH" exec "$instance_name" -- sh -c \
    "fuse2fs '$DEVICE' /mnt/cinder >/dev/null 2>&1; \
     cat /mnt/cinder/marker; umount /mnt/cinder")
[[ "$restored_marker" == "$marker" ]]
openstack server resize confirm "$server_id"
wait_value "openstack server show '$server_id' -f value -c status" ACTIVE

openstack server remove volume "$server_id" "$volume_id"
wait_value "openstack volume show '$volume_id' -f value -c status" available
! incus_remote "$DEST_SSH" profile device show "$instance_name" |
    grep -F "$volume_id:"
[[ -z "$(incus_remote "$DEST_SSH" profile get "$instance_name" \
    "user.openstack.volume.$volume_id")" ]]

trap - EXIT INT TERM
openstack server delete --wait "$server_id"
server_id=
openstack volume delete "$volume_id"
volume_id=
echo "PASS volume attach=data extend=${EXTENDED_SIZE_GIB}GiB migrate=$DEST_HOST detach=clean"
