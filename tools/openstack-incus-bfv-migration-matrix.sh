#!/usr/bin/env bash
# Run the release-grade BFV migration fault matrix and reject leaked state.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
E2E_SCRIPT=${E2E_SCRIPT:-"$SCRIPT_DIR/openstack-incus-bfv-migration-e2e.sh"}
FLEET_PREFLIGHT=${FLEET_PREFLIGHT:-"$SCRIPT_DIR/openstack-incus-fleet-preflight.sh"}
COMPUTE_NODES=${COMPUTE_NODES:?Set COMPUTE_NODES to name=ssh pairs}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
SSH_KNOWN_HOSTS_FILE=${SSH_KNOWN_HOSTS_FILE:-$HOME/.ssh/known_hosts}
CONTROLLER_SSH=${CONTROLLER_SSH:?Set CONTROLLER_SSH}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
INCUS_PROJECT=${INCUS_PROJECT:-nova}
INCUS_RUNTIME_MODE=${INCUS_RUNTIME_MODE:-podman}
INCUS_RUNTIME_CONTAINER=${INCUS_RUNTIME_CONTAINER:-incus}
INCUS_KUBE_NAMESPACE=${INCUS_KUBE_NAMESPACE:-openstack}
INCUS_KUBE_NODE_MAP=${INCUS_KUBE_NODE_MAP:-}
RUN_FLEET_PREFLIGHT=${RUN_FLEET_PREFLIGHT:-true}
NAME_PREFIX=${NAME_PREFIX:-incus-bfv-release}
CASES=${CASES:-normal,post-claim-data,post-claim-start,stopped-post-claim-data,reverse-revert}
COMMAND_TIMEOUT=${COMMAND_TIMEOUT:-30}

[[ -f "$SSH_IDENTITY" && -r "$SSH_IDENTITY" ]] || {
    echo "SSH identity is not a readable regular file: $SSH_IDENTITY" >&2
    exit 2
}
[[ -f "$SSH_KNOWN_HOSTS_FILE" && -r "$SSH_KNOWN_HOSTS_FILE" ]] || {
    echo "SSH known_hosts is not a readable regular file: $SSH_KNOWN_HOSTS_FILE" >&2
    exit 2
}
[[ "$COMMAND_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || {
    echo "COMMAND_TIMEOUT must be a positive number of seconds" >&2
    exit 2
}
command -v timeout >/dev/null || {
    echo "timeout command is required" >&2
    exit 2
}
SSH=(
    ssh
    -i "$SSH_IDENTITY"
    -o BatchMode=yes
    -o StrictHostKeyChecking=yes
    -o "UserKnownHostsFile=$SSH_KNOWN_HOSTS_FILE"
)
IFS=, read -ra nodes <<<"$COMPUTE_NODES"
declare -a node_names=() node_ssh=()

for entry in "${nodes[@]}"; do
    [[ "$entry" == *=* ]] || {
        echo "Invalid COMPUTE_NODES entry: $entry" >&2
        exit 2
    }
    node_names+=("${entry%%=*}")
    node_ssh+=("${entry#*=}")
done
(( ${#node_names[@]} >= 2 )) || {
    echo "BFV migration matrix requires at least two computes" >&2
    exit 2
}
if [[ "$INCUS_RUNTIME_MODE" == kubernetes && -z "$INCUS_KUBE_NODE_MAP" ]]; then
    echo "INCUS_KUBE_NODE_MAP is required for Kubernetes runtime mode" >&2
    exit 2
fi

remote() {
    if [[ -n "${ACTIVE_COMMAND_TIMEOUT:-}" ]]; then
        timeout --foreground "${ACTIVE_COMMAND_TIMEOUT}s" \
            "${SSH[@]}" -o "ConnectTimeout=$ACTIVE_COMMAND_TIMEOUT" "$@"
    else
        "${SSH[@]}" "$@"
    fi
}

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

incus_runtime_remote() {
    local host=$1 command_line namespace kube_node
    shift
    printf -v command_line '%q ' "$@"
    case "$INCUS_RUNTIME_MODE" in
        podman)
            remote "$host" \
                "podman exec $(printf '%q' "$INCUS_RUNTIME_CONTAINER") $command_line"
            ;;
        kubernetes)
            kube_node=$(kube_node_for_target "$host") || return 1
            printf -v namespace '%q' "$INCUS_KUBE_NAMESPACE"
            printf -v kube_node '%q' "$kube_node"
            remote "$host" \
                "set -e; pods=\$(kubectl -n $namespace get pod -l application=incus --field-selector \"spec.nodeName=$kube_node\" --no-headers -o custom-columns=NAME:.metadata.name); set -- \$pods; [ \$# -eq 1 ]; kubectl -n $namespace exec \"\$1\" -- $command_line"
            ;;
        *)
            echo "INCUS_RUNTIME_MODE must be podman or kubernetes" >&2
            return 2
            ;;
    esac
}

# Keep residual-state polling bounded when a compute or control-plane endpoint
# becomes unavailable during a matrix case.
run_until_deadline() {
    local deadline=$1
    shift
    local remaining=$((deadline - SECONDS))
    ((remaining > 0)) || return 124

    local ACTIVE_COMMAND_TIMEOUT=$COMMAND_TIMEOUT
    ((ACTIVE_COMMAND_TIMEOUT > remaining)) && ACTIVE_COMMAND_TIMEOUT=$remaining
    "$@"
}

openstack() {
    local command_line
    printf -v command_line '%q ' "$@"
    remote "$CONTROLLER_SSH" \
        "source $CONTROLLER_OPENRC >/dev/null 2>&1; openstack $command_line"
}

snapshot_resources() {
    {
        openstack server list --all-projects -f value -c ID | sort
        echo --
        openstack volume list --all-projects -f value -c ID | sort
    }
}

snapshot_node() {
    local ssh_host=$1
    incus_runtime_remote "$ssh_host" incus --project "$INCUS_PROJECT" \
        list --format csv -c n
    echo --
    incus_runtime_remote "$ssh_host" incus --project "$INCUS_PROJECT" \
        profile list --format csv -c n
    echo --
    remote "$ssh_host" \
        "rbd device list --format json --id cinder 2>/dev/null |
         jq -S '[.[] | {id, pool, namespace, name, device}]'"
}

run_case() {
    local case_name=$1 source_index=$2 dest_index=$3
    shift 3
    local test_name="${NAME_PREFIX}-${case_name}" ssh_host before after
    local deadline
    declare -A node_baseline=()
    for ssh_host in "${node_ssh[@]}"; do
        node_baseline["$ssh_host"]=$(snapshot_node "$ssh_host")
    done
    echo "=== BFV matrix: $case_name (${node_names[$source_index]} -> ${node_names[$dest_index]}) ==="
    env \
        NAME="$test_name" \
        SOURCE_HOST="${node_names[$source_index]}" \
        DEST_HOST="${node_names[$dest_index]}" \
        SOURCE_SSH="${node_ssh[$source_index]}" \
        DEST_SSH="${node_ssh[$dest_index]}" \
        DEST_MIGRATION_IP="${node_ssh[$dest_index]#*@}" \
        CONTROLLER_SSH="$CONTROLLER_SSH" \
        CONTROLLER_OPENRC="$CONTROLLER_OPENRC" \
        SSH_IDENTITY="$SSH_IDENTITY" \
        SSH_KNOWN_HOSTS_FILE="$SSH_KNOWN_HOSTS_FILE" \
        COMMAND_TIMEOUT="$COMMAND_TIMEOUT" \
        "$@" \
        bash "$E2E_SCRIPT"
    for ssh_host in "${node_ssh[@]}"; do
        before=${node_baseline["$ssh_host"]}
        deadline=$((SECONDS + 90))
        while ((SECONDS < deadline)); do
            after=$(run_until_deadline "$deadline" snapshot_node "$ssh_host")
            [[ "$after" == "$before" ]] && break
            sleep 2
        done
        [[ "$after" == "$before" ]] || {
            echo "$ssh_host runtime inventory changed after $case_name" >&2
            diff -u <(printf '%s\n' "$before") <(printf '%s\n' "$after") || true
            exit 1
        }
    done
}

case_enabled() {
    [[ ",$CASES," == *",$1,"* ]]
}

baseline=$(snapshot_resources)

if case_enabled normal; then
    run_case normal 0 1 \
        INJECT_PREFLIGHT_FAILURE=true \
        INJECT_POST_CLAIM_FAILURE=false \
        INJECT_REVERT_FAILURE=false
fi
if case_enabled post-claim-data; then
    run_case post-claim-data 0 1 \
        INJECT_PREFLIGHT_FAILURE=false \
        INJECT_POST_CLAIM_FAILURE=true \
        POST_CLAIM_FAILPOINT=data-volume
fi
if case_enabled post-claim-start; then
    run_case post-claim-start 0 1 \
        INJECT_PREFLIGHT_FAILURE=false \
        INJECT_POST_CLAIM_FAILURE=true \
        POST_CLAIM_FAILPOINT=start
fi
if case_enabled stopped-post-claim-data; then
    run_case stopped-post-claim-data 0 1 \
        INJECT_PREFLIGHT_FAILURE=false \
        INJECT_POST_CLAIM_FAILURE=true \
        POST_CLAIM_FAILPOINT=data-volume \
        MIGRATE_STOPPED_INSTANCE=true
fi
if case_enabled reverse-revert; then
    run_case reverse-revert 0 1 \
        INJECT_PREFLIGHT_FAILURE=false \
        INJECT_POST_CLAIM_FAILURE=false \
        INJECT_REVERT_FAILURE=true
fi

deadline=$((SECONDS + 90))
while ((SECONDS < deadline)); do
    current_resources=$(run_until_deadline "$deadline" snapshot_resources)
    [[ "$current_resources" == "$baseline" ]] && break
    sleep 2
done
[[ "$current_resources" == "$baseline" ]] || {
    echo "OpenStack server/volume inventory changed across the BFV matrix" >&2
    diff -u <(printf '%s\n' "$baseline") <(snapshot_resources) || true
    exit 1
}

if [[ "$RUN_FLEET_PREFLIGHT" == true ]]; then
    COMPUTE_NODES="$COMPUTE_NODES" \
        CONTROLLER_SSH="$CONTROLLER_SSH" \
        CONTROLLER_OPENRC="$CONTROLLER_OPENRC" \
        SSH_IDENTITY="$SSH_IDENTITY" \
        SSH_KNOWN_HOSTS_FILE="$SSH_KNOWN_HOSTS_FILE" \
        bash "$FLEET_PREFLIGHT"
fi

echo "PASS BFV migration release matrix and residual-state audit"
