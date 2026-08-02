#!/usr/bin/env bash
# Exercise cold migration for local/BFV roots and 0/1/3 data/share counts.

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
E2E=${E2E:-$SCRIPT_DIR/openstack-incus-live-migration-e2e.sh}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
SSH_KNOWN_HOSTS_FILE=${SSH_KNOWN_HOSTS_FILE:-$HOME/.ssh/known_hosts}
NODE01_HOST=${NODE01_HOST:-incus-node-01}
NODE01_SSH=${NODE01_SSH:-root@10.224.0.21}
NODE02_HOST=${NODE02_HOST:-incus-node-02}
NODE02_SSH=${NODE02_SSH:-root@10.224.0.17}
NODE03_HOST=${NODE03_HOST:-incus-node-03}
NODE03_SSH=${NODE03_SSH:-root@10.224.0.22}
LOCAL_IMAGE=${LOCAL_IMAGE:-ubuntu-noble-24.04-cloud-incus}
BFV_IMAGE=${BFV_IMAGE:-ubuntu-noble-24.04-cloud-incus-bfv-raw}
FLAVOR=${FLAVOR:-ds512M}
NETWORK=${NETWORK:-private}
ROOT_VOLUME_TYPE=${ROOT_VOLUME_TYPE:-ceph}
DATA_VOLUME_TYPE=${DATA_VOLUME_TYPE:-ceph}
MANILA_SHARES=${MANILA_SHARES:?Set space-separated Manila share names or IDs}
CARDINALITY_COUNTS=${CARDINALITY_COUNTS:-0 1 3}
TIMEOUT=${TIMEOUT:-900}
MATRIX_CASES=${MATRIX_CASES:-all}
# These maximum-attachment cases exercise VERIFY_RESIZE -> revert on the
# second hop. All other hops confirm, so each case still targets all nodes and
# finishes on its original node.
COLD_REVERT_CASES=${COLD_REVERT_CASES:-local_d3_s3,bfv_d3_s3}

[[ -f "$SSH_IDENTITY" && -r "$SSH_IDENTITY" ]] || {
    echo "SSH identity is not a readable regular file: $SSH_IDENTITY" >&2
    exit 2
}
[[ -f "$SSH_KNOWN_HOSTS_FILE" && -r "$SSH_KNOWN_HOSTS_FILE" ]] || {
    echo "SSH known_hosts is not a readable regular file: $SSH_KNOWN_HOSTS_FILE" >&2
    exit 2
}

MIGRATION_TARGETS="${NODE02_HOST}=${NODE02_SSH},"
MIGRATION_TARGETS+="${NODE03_HOST}=${NODE03_SSH},"
MIGRATION_TARGETS+="${NODE01_HOST}=${NODE01_SSH}"

read -r -a available_shares <<<"$MANILA_SHARES"
read -r -a cardinalities <<<"$CARDINALITY_COUNTS"

[[ "$NODE01_HOST" != "$NODE02_HOST" &&
   "$NODE01_HOST" != "$NODE03_HOST" &&
   "$NODE02_HOST" != "$NODE03_HOST" ]] || {
    echo "Cold migration matrix requires three distinct compute names" >&2
    exit 2
}
[[ "$NODE01_SSH" != "$NODE02_SSH" &&
   "$NODE01_SSH" != "$NODE03_SSH" &&
   "$NODE02_SSH" != "$NODE03_SSH" ]] || {
    echo "Cold migration matrix requires three distinct compute SSH targets" >&2
    exit 2
}

if [[ "$MATRIX_CASES" == all ]]; then
    ((${#cardinalities[@]} == 3)) || {
        echo "Full cold matrix requires exactly CARDINALITY_COUNTS='0 1 3'" >&2
        exit 2
    }
    for required_count in 0 1 3; do
        [[ " ${cardinalities[*]} " == *" $required_count "* ]] || {
            echo "Full cold matrix is missing cardinality $required_count" >&2
            exit 2
        }
    done
    ((${#available_shares[@]} >= 3)) || {
        echo "Full cold matrix requires at least three Manila shares" >&2
        exit 2
    }
    [[ ",$COLD_REVERT_CASES," == *",local_d3_s3,"* &&
       ",$COLD_REVERT_CASES," == *",bfv_d3_s3,"* ]] || {
        echo "Full cold matrix must revert local_d3_s3 and bfv_d3_s3" >&2
        exit 2
    }
    declare -A unique_shares=()
    for share in "${available_shares[@]:0:3}"; do
        [[ -z ${unique_shares["$share"]+x} ]] || {
            echo "Full cold matrix requires three distinct Manila shares" >&2
            exit 2
        }
        unique_shares["$share"]=1
    done
fi

placement_allocations() {
    local provider
    while IFS= read -r provider; do
        openstack resource provider show --allocations -f json "$provider" |
            python3 -c '
import json
import sys

data = json.load(sys.stdin)
provider = data["uuid"]
for consumer, allocation in sorted(data["allocations"].items()):
    resources = ",".join(
        f"{name}={value}"
        for name, value in sorted(allocation["resources"].items()))
    print(f"{provider} {consumer} {resources}")'
    done < <(openstack resource provider list -f value -c uuid | sort)
}

select_shares() {
    local count=$1
    ((count <= ${#available_shares[@]})) || {
        echo "Need $count Manila shares, only ${#available_shares[@]} supplied" \
            >&2
        return 1
    }
    local selected=()
    local index
    for ((index = 0; index < count; index++)); do
        selected+=("${available_shares[index]}")
    done
    printf '%s' "${selected[*]}"
}

case_selected() {
    local case_name=$1
    [[ "$MATRIX_CASES" == all ]] ||
        [[ ",$MATRIX_CASES," == *",$case_name,"* ]]
}

case_reverts() {
    local case_name=$1
    [[ ",$COLD_REVERT_CASES," == *",$case_name,"* ]]
}

run_case() {
    local root_model=$1 data_count=$2 share_count=$3
    local case_name="${root_model}_d${data_count}_s${share_count}"
    local bfv=0 image=$LOCAL_IMAGE selected_shares=
    local migration_actions=confirm,confirm,confirm

    case_selected "$case_name" || return 0
    if [[ "$root_model" == bfv ]]; then
        bfv=1
        image=$BFV_IMAGE
    fi
    if case_reverts "$case_name"; then
        migration_actions=confirm,revert,confirm
    fi
    selected_shares=$(select_shares "$share_count")

    echo "=== cold migration cardinality matrix: $case_name ==="
    env \
        SSH_IDENTITY="$SSH_IDENTITY" \
        SSH_KNOWN_HOSTS_FILE="$SSH_KNOWN_HOSTS_FILE" \
        SOURCE_HOST="$NODE01_HOST" \
        SOURCE_SSH="$NODE01_SSH" \
        MIGRATION_TARGETS="$MIGRATION_TARGETS" \
        MIGRATION_MODE=cold \
        MIGRATION_ACTIONS="$migration_actions" \
        IMAGE="$image" \
        FLAVOR="$FLAVOR" \
        NETWORK="$NETWORK" \
        BOOT_FROM_VOLUME="$bfv" \
        ROOT_VOLUME_TYPE="$ROOT_VOLUME_TYPE" \
        DATA_VOLUME_TYPE="$DATA_VOLUME_TYPE" \
        WITH_DATA_VOLUME="$((data_count > 0 ? 1 : 0))" \
        DATA_VOLUME_COUNT="$data_count" \
        DATA_DEVICES= \
        MANILA_SHARES="$selected_shares" \
        MANILA_TAGS= \
        INJECT_RESTORE_FAILURE=0 \
        TIMEOUT="$TIMEOUT" \
        SERVER="incus-cold-cardinality-${case_name//_/-}-$RANDOM" \
        bash "$E2E"
    echo "PASS cold migration cardinality case=$case_name"
}

baseline_servers=$(openstack server list --all-projects -f value -c ID | sort)
baseline_volumes=$(openstack volume list --all-projects -f value -c ID | sort)
baseline_ports=$(openstack port list --device-owner compute:nova \
    -f value -c ID | sort)
baseline_allocations=$(placement_allocations)

for root_model in local bfv; do
    for data_count in "${cardinalities[@]}"; do
        [[ "$data_count" =~ ^[0-9]+$ ]]
        for share_count in "${cardinalities[@]}"; do
            [[ "$share_count" =~ ^[0-9]+$ ]]
            run_case "$root_model" "$data_count" "$share_count"
        done
    done
done

final_servers=$(openstack server list --all-projects -f value -c ID | sort)
final_volumes=$(openstack volume list --all-projects -f value -c ID | sort)
final_ports=$(openstack port list --device-owner compute:nova \
    -f value -c ID | sort)
final_allocations=$(placement_allocations)
[[ "$final_servers" == "$baseline_servers" ]]
[[ "$final_volumes" == "$baseline_volumes" ]]
[[ "$final_ports" == "$baseline_ports" ]]
[[ "$final_allocations" == "$baseline_allocations" ]]

if [[ "$MATRIX_CASES" == all ]]; then
    echo "PASS Incus cold migration 2x3x3 cardinality matrix and residual-state audit"
else
    echo "PASS Incus cold migration selected cardinality cases=$MATRIX_CASES and residual-state audit"
fi
