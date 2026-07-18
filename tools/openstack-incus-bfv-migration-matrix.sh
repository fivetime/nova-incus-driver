#!/usr/bin/env bash
# Run the release-grade BFV migration fault matrix and reject leaked state.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
E2E_SCRIPT=${E2E_SCRIPT:-"$SCRIPT_DIR/openstack-incus-bfv-migration-e2e.sh"}
FLEET_PREFLIGHT=${FLEET_PREFLIGHT:-"$SCRIPT_DIR/openstack-incus-fleet-preflight.sh"}
COMPUTE_NODES=${COMPUTE_NODES:?Set COMPUTE_NODES to name=ssh pairs}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
CONTROLLER_SSH=${CONTROLLER_SSH:?Set CONTROLLER_SSH}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
RUN_FLEET_PREFLIGHT=${RUN_FLEET_PREFLIGHT:-true}
NAME_PREFIX=${NAME_PREFIX:-incus-bfv-release}
CASES=${CASES:-normal,post-claim-data,post-claim-start,stopped-post-claim-data,reverse-revert}

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)
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

remote() {
    "${SSH[@]}" "$@"
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
    remote "$ssh_host" \
        "{ podman exec incus incus --project nova list --format csv -c n;
           echo --;
           podman exec incus incus --project nova profile list --format csv -c n;
           echo --;
           rbd device list --format json --id cinder 2>/dev/null |
             jq -S '[.[] | {id, pool, namespace, name, device}]'; }"
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
        "$@" \
        bash "$E2E_SCRIPT"
    for ssh_host in "${node_ssh[@]}"; do
        before=${node_baseline["$ssh_host"]}
        deadline=$((SECONDS + 90))
        while ((SECONDS < deadline)); do
            after=$(snapshot_node "$ssh_host")
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
    current_resources=$(snapshot_resources)
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
        bash "$FLEET_PREFLIGHT"
fi

echo "PASS BFV migration release matrix and residual-state audit"
