#!/usr/bin/env bash
# Validate explicit BFV reimage in ACTIVE/SHUTOFF and implicit rejection.

set -euo pipefail

RUN_DESTRUCTIVE=${RUN_DESTRUCTIVE:-false}
IMAGE=${IMAGE:?Set IMAGE to a raw rootfs-directory Glance image}
FLAVOR=${FLAVOR:?Set FLAVOR}
NETWORK=${NETWORK:?Set NETWORK}
VOLUME_SIZE=${VOLUME_SIZE:-5}
VOLUME_TYPE=${VOLUME_TYPE:-ceph}
SOURCE_HOST=${SOURCE_HOST:-incus-node-01}
SOURCE_SSH=${SOURCE_SSH:-root@10.32.32.130}
CONTROLLER_SSH=${CONTROLLER_SSH:-}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
SSH_KNOWN_HOSTS_FILE=${SSH_KNOWN_HOSTS_FILE:-$HOME/.ssh/known_hosts}
INCUS_PROJECT=${INCUS_PROJECT:-nova}
INCUS_RUNTIME_MODE=${INCUS_RUNTIME_MODE:-podman}
INCUS_RUNTIME_CONTAINER=${INCUS_RUNTIME_CONTAINER:-incus}
INCUS_KUBE_NAMESPACE=${INCUS_KUBE_NAMESPACE:-openstack}
INCUS_KUBE_NODE=${INCUS_KUBE_NODE:-}
KUBE_CONTROL_SSH=${KUBE_CONTROL_SSH:-}
TIMEOUT=${TIMEOUT:-600}
NAME=${NAME:-incus-bfv-reimage-$RANDOM}

[[ "$RUN_DESTRUCTIVE" == true ]] || {
    echo "Set RUN_DESTRUCTIVE=true to run this destructive case" >&2
    exit 2
}
[[ -r "$SSH_IDENTITY" && -r "$SSH_KNOWN_HOSTS_FILE" ]] || {
    echo "SSH identity and known_hosts must be readable" >&2
    exit 2
}
if [[ "$INCUS_RUNTIME_MODE" == kubernetes && -z "$INCUS_KUBE_NODE" ]]; then
    echo "Set INCUS_KUBE_NODE for Kubernetes mode" >&2
    exit 2
fi

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=yes
    -o "UserKnownHostsFile=$SSH_KNOWN_HOSTS_FILE")
server_id=
volume_id=
instance_name=
port_id=

remote() {
    local host=$1
    shift
    "${SSH[@]}" "$host" "$@"
}

openstack() {
    if [[ -z "$CONTROLLER_SSH" ]]; then
        command openstack "$@"
        return
    fi
    local command_line
    printf -v command_line '%q ' "$@"
    remote "$CONTROLLER_SSH" \
        "source $CONTROLLER_OPENRC >/dev/null 2>&1; openstack $command_line"
}

incus() {
    local command_line kube_command
    printf -v command_line '%q ' incus --project "$INCUS_PROJECT" "$@"
    case "$INCUS_RUNTIME_MODE" in
        podman)
            remote "$SOURCE_SSH" \
                "podman exec $(printf '%q' "$INCUS_RUNTIME_CONTAINER") $command_line"
            ;;
        kubernetes)
            printf -v kube_command \
                'set -e; pods=$(kubectl -n %q get pod -l application=incus --field-selector spec.nodeName=%q --no-headers -o custom-columns=NAME:.metadata.name); set -- $pods; [ $# -eq 1 ]; kubectl -n %q exec "$1" -- %s' \
                "$INCUS_KUBE_NAMESPACE" "$INCUS_KUBE_NODE" \
                "$INCUS_KUBE_NAMESPACE" "$command_line"
            if [[ -n "$KUBE_CONTROL_SSH" ]]; then
                remote "$KUBE_CONTROL_SSH" "$kube_command"
            else
                bash -c "$kube_command"
            fi
            ;;
        *) return 2 ;;
    esac
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

attachment_count() {
    openstack volume show "$volume_id" -f json -c attachments |
        python3 -c 'import json,sys
print(len(json.load(sys.stdin)["attachments"]))
'
}

assert_network() {
    [[ "$(openstack port show "$port_id" -f value -c status)" == ACTIVE ]] ||
        fail "Neutron port is not ACTIVE"
    local iface
    iface=$(remote "$SOURCE_SSH" \
        "ovs-vsctl --data=bare --no-heading --columns=name find Interface \
         external_ids:iface-id=$port_id")
    [[ -n "$iface" ]] || fail "OVS interface is missing"
}

cleanup() {
    local status=$?
    if [[ -z "$server_id" ]]; then
        server_id=$(openstack server list --all-projects --name "^$NAME$" \
            -f value -c ID 2>/dev/null | head -n1 || true)
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

volume_id=$(openstack volume create --image "$IMAGE" --size "$VOLUME_SIZE" \
    --type "$VOLUME_TYPE" "$NAME-root" -f value -c id)
wait_value available volume_status
server_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --volume "$volume_id" --network "$NETWORK" \
    --host "$SOURCE_HOST" "$NAME" -f value -c id)
wait_value ACTIVE server_status
instance_name=$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)
port_id=$(openstack port list --server "$server_id" -f value -c ID)

marker="must-survive-rejected-rebuild-$server_id"
incus exec "$instance_name" -- sh -c \
    "printf %s '$marker' > /root/reimage-rejection-marker; sync"

# BFV rebuild without the explicit destructive opt-in must fail unchanged.
if openstack --os-compute-api-version 2.93 server rebuild \
        --image "$IMAGE" "$server_id"; then
    fail "implicit BFV rebuild unexpectedly succeeded"
fi
wait_value ACTIVE server_status
[[ "$(incus exec "$instance_name" -- \
    cat /root/reimage-rejection-marker)" == "$marker" ]] ||
    fail "rejected rebuild changed the root filesystem"
[[ "$(attachment_count)" == 1 ]] ||
    fail "rejected rebuild changed the Cinder attachment"
assert_network

# Explicit reimage is destructive and must replace the running rootfs.
openstack --os-compute-api-version 2.93 server rebuild \
    --reimage-boot-volume --image "$IMAGE" "$server_id"
wait_value ACTIVE server_status
if incus exec "$instance_name" -- \
        test -e /root/reimage-rejection-marker; then
    fail "ACTIVE reimage retained old rootfs data"
fi
[[ "$(attachment_count)" == 1 ]]
assert_network

# Reimage a stopped pet and preserve its requested power state.
incus exec "$instance_name" -- sh -c \
    "printf stopped > /root/shutoff-reimage-marker; sync"
openstack server stop "$server_id"
wait_value SHUTOFF server_status
openstack --os-compute-api-version 2.93 server rebuild \
    --reimage-boot-volume --image "$IMAGE" "$server_id"
wait_value SHUTOFF server_status
[[ "$(attachment_count)" == 1 ]]
openstack server start "$server_id"
wait_value ACTIVE server_status
if incus exec "$instance_name" -- test -e /root/shutoff-reimage-marker; then
    fail "SHUTOFF reimage retained old rootfs data"
fi
assert_network

trap - EXIT INT TERM
openstack server delete --wait "$server_id"
server_id=
wait_value available volume_status
openstack volume delete "$volume_id"
volume_id=

echo "PASS BFV ACTIVE/SHUTOFF explicit reimage and implicit rejection"
