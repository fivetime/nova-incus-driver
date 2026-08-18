#!/usr/bin/env bash
# Prove that a destination rejects an idmap reservation overlapping an instance.

set -Eeuo pipefail

RUN_DESTRUCTIVE=${RUN_DESTRUCTIVE:-false}
if [[ "$RUN_DESTRUCTIVE" != true ]]; then
    echo "Refusing destructive E2E; set RUN_DESTRUCTIVE=true" >&2
    exit 2
fi

IMAGE=${IMAGE:?Set IMAGE to an admitted Incus system-container image}
FLAVOR=${FLAVOR:?Set FLAVOR to an Incus-compatible Flavor}
NETWORK=${NETWORK:?Set NETWORK to a tenant network}
NOVA_HOST=${NOVA_HOST:?Set NOVA_HOST to the target nova-compute hostname}
COMPUTE_SSH=${COMPUTE_SSH:?Set COMPUTE_SSH to the target compute SSH address}
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
NAME=${NAME:-incus-idmap-conflict-$RANDOM}

command -v jq >/dev/null || {
    echo "jq is required for the idmap conflict E2E" >&2
    exit 2
}
[[ -f "$SSH_IDENTITY" && -r "$SSH_IDENTITY" ]] || {
    echo "SSH identity is not a readable regular file: $SSH_IDENTITY" >&2
    exit 2
}
[[ -f "$SSH_KNOWN_HOSTS_FILE" && -r "$SSH_KNOWN_HOSTS_FILE" ]] || {
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

server_id=
instance_name=
attempt_token=
attempt_created=false

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
    printf -v command_line '%q ' incus "$@"
    case "$INCUS_RUNTIME_MODE" in
        podman)
            remote "$COMPUTE_SSH" \
                "podman exec $(printf '%q' "$INCUS_RUNTIME_CONTAINER") $command_line"
            ;;
        kubernetes)
            [[ -n "$INCUS_KUBE_NODE" ]] || return 2
            printf -v kube_command \
                'set -e; pods=$(kubectl -n %q get pod -l application=incus --field-selector spec.nodeName=%q --no-headers -o custom-columns=NAME:.metadata.name); set -- $pods; [ $# -eq 1 ]; kubectl -n %q exec -i "$1" -- %s' \
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
    local deadline=$((SECONDS + TIMEOUT)) current=
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

cleanup() {
    local exit_status=$?
    trap - EXIT INT TERM
    set +e
    if [[ "$attempt_created" == true && -n "$attempt_token" ]]; then
        incus query -X PUT -d '{"state":"aborted"}' \
            "/1.0/migration-attempts/$attempt_token?project=$INCUS_PROJECT" \
            >/dev/null 2>&1 || true
        incus query -X PUT -d '{"state":"settled"}' \
            "/1.0/migration-attempts/$attempt_token?project=$INCUS_PROJECT" \
            >/dev/null 2>&1 || true
        incus query -X DELETE \
            "/1.0/migration-attempts/$attempt_token?project=$INCUS_PROJECT" \
            >/dev/null 2>&1 || true
    fi
    if [[ -n "$server_id" ]]; then
        openstack server delete --wait "$server_id" >/dev/null 2>&1 || true
        wait_absent openstack server show "$server_id" >/dev/null 2>&1 || true
    fi
    exit "$exit_status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

server_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --image "$IMAGE" --network "$NETWORK" \
    --host "$NOVA_HOST" "$NAME" -f value -c id)
wait_value ACTIVE openstack server show "$server_id" -f value -c status
instance_name=$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)
[[ "$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:host)" == "$NOVA_HOST" ]] || \
    fail "Nova did not place the probe on $NOVA_HOST"

instance_document=$(incus query \
    "/1.0/instances/$instance_name?project=$INCUS_PROJECT")
isolated=$(jq -r \
    '(.expanded_config["security.idmap.isolated"] // "false") |
     ascii_downcase' \
    <<<"$instance_document")
idmap_base=$(jq -r \
    '.config["volatile.idmap.base"] //
     .expanded_config["volatile.idmap.base"] //
     .expanded_config["security.idmap.base"] // empty' \
    <<<"$instance_document")
idmap_size=$(jq -r \
    '.expanded_config["security.idmap.size"] // "65536"' \
    <<<"$instance_document")

[[ "$isolated" == true ]] || fail "probe instance does not use an isolated idmap"
[[ "$idmap_base" =~ ^[0-9]+$ ]] || fail "probe instance has no numeric idmap base"
case "$idmap_size" in
    ''|auto|null) idmap_size=65536 ;;
esac
[[ "$idmap_size" =~ ^[1-9][0-9]*$ ]] || \
    fail "probe instance has no positive idmap size"

attempt_token=$(remote "$COMPUTE_SSH" cat /proc/sys/kernel/random/uuid)
resource_name="idmap-probe-${attempt_token%%-*}"
attempt_path="/1.0/migration-attempts/$attempt_token?project=$INCUS_PROJECT"
payload=$(jq -nc \
    --arg resource_name "$resource_name" \
    --argjson idmap_base "$idmap_base" \
    --argjson idmap_size "$idmap_size" \
    '{state:"active",resource_type:"instance",
      resource_name:$resource_name,idmap_base:$idmap_base,
      idmap_size:$idmap_size}')

set +e
conflict_output=$(incus query -X PUT -d "$payload" "$attempt_path" 2>&1)
conflict_status=$?
set -e
if ((conflict_status == 0)); then
    attempt_created=true
    fail "Incus accepted an idmap reservation overlapping $instance_name"
fi
grep -qi 'overlap.*instance' <<<"$conflict_output" || {
    printf '%s\n' "$conflict_output" >&2
    fail "Incus rejected the probe without an explicit instance overlap"
}
if incus query "$attempt_path" >/dev/null 2>&1; then
    fail "rejected migration attempt was persisted"
fi

wait_value ACTIVE openstack server show "$server_id" -f value -c status
[[ "$(incus --project "$INCUS_PROJECT" list "$instance_name" \
    --format csv -c s)" == RUNNING ]] || \
    fail "the overlap probe changed the running instance"

trap - EXIT INT TERM
openstack server delete --wait "$server_id"
wait_absent openstack server show "$server_id"
server_id=
echo "PASS destination rejected an overlapping isolated idmap without state"
