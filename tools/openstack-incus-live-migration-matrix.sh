#!/usr/bin/env bash
# Exercise every supported root/data/share live-migration combination.

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
E2E=${E2E:-$SCRIPT_DIR/openstack-incus-live-migration-e2e.sh}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
SSH_KNOWN_HOSTS_FILE=${SSH_KNOWN_HOSTS_FILE:-$HOME/.ssh/known_hosts}
NODE01_HOST=${NODE01_HOST:-incus-node-01}
NODE01_SSH=${NODE01_SSH:-root@10.32.32.130}
NODE02_HOST=${NODE02_HOST:-incus-node-02}
NODE02_SSH=${NODE02_SSH:-root@10.32.32.131}
NODE03_HOST=${NODE03_HOST:-incus-node-03}
NODE03_SSH=${NODE03_SSH:-root@10.32.32.132}
LOCAL_IMAGE=${LOCAL_IMAGE:-alpine-3.21-cloud-incus-criu-fuse}
BFV_IMAGE=${BFV_IMAGE:-alpine-3.21-criu-bfv-fuse}
MANILA_SHARE=${MANILA_SHARE:-incus-e2e-share}
TIMEOUT=${TIMEOUT:-420}
MATRIX_CASES=${MATRIX_CASES:-all}

[[ -f "$SSH_KNOWN_HOSTS_FILE" && -r "$SSH_KNOWN_HOSTS_FILE" ]] || {
    echo "SSH known_hosts is not a readable regular file: $SSH_KNOWN_HOSTS_FILE" >&2
    exit 2
}
MIGRATION_TARGETS=${MIGRATION_TARGETS:-"${NODE02_HOST}=${NODE02_SSH},${NODE03_HOST}=${NODE03_SSH},${NODE01_HOST}=${NODE01_SSH}"}

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

baseline_servers=$(openstack server list --all-projects -f value -c ID | sort)
baseline_volumes=$(openstack volume list --all-projects -f value -c ID | sort)
baseline_allocations=$(placement_allocations)

case_selected() {
    local case_name=$1
    [[ "$MATRIX_CASES" == all ]] ||
        [[ ",$MATRIX_CASES," == *",$case_name,"* ]]
}

run_case() {
    local case_name=$1 bfv=$2 data=$3 share=$4
    local image=$LOCAL_IMAGE share_name=

    case_selected "$case_name" || return 0
    if [[ "$bfv" == 1 ]]; then
        image=$BFV_IMAGE
    fi
    if [[ "$share" == 1 ]]; then
        share_name=$MANILA_SHARE
    fi

    echo "=== live migration matrix: $case_name ==="
    env \
        SSH_IDENTITY="$SSH_IDENTITY" \
        SSH_KNOWN_HOSTS_FILE="$SSH_KNOWN_HOSTS_FILE" \
        SOURCE_HOST="$NODE01_HOST" \
        SOURCE_SSH="$NODE01_SSH" \
        MIGRATION_TARGETS="$MIGRATION_TARGETS" \
        IMAGE="$image" \
        BOOT_FROM_VOLUME="$bfv" \
        WITH_DATA_VOLUME="$data" \
        DATA_VOLUME_COUNT=1 \
        DATA_DEVICES=/dev/vdb \
        MANILA_SHARE="$share_name" \
        INJECT_RESTORE_FAILURE=0 \
        TIMEOUT="$TIMEOUT" \
        SERVER="incus-live-matrix-${case_name//_/-}-$RANDOM" \
        "$E2E"
    echo "PASS live migration matrix case=$case_name"
}

run_case local_basic 0 0 0
run_case local_data 0 1 0
run_case local_manila 0 0 1
run_case local_data_manila 0 1 1
run_case bfv_basic 1 0 0
run_case bfv_data 1 1 0
run_case bfv_manila 1 0 1
run_case bfv_data_manila 1 1 1

final_servers=$(openstack server list --all-projects -f value -c ID | sort)
final_volumes=$(openstack volume list --all-projects -f value -c ID | sort)
final_allocations=$(placement_allocations)
[[ "$final_servers" == "$baseline_servers" ]]
[[ "$final_volumes" == "$baseline_volumes" ]]
[[ "$final_allocations" == "$baseline_allocations" ]]

if [[ "$MATRIX_CASES" == all ]]; then
    echo "PASS Incus live migration 2x2x2 matrix and residual-state audit"
else
    echo "PASS Incus live migration selected cases=$MATRIX_CASES and residual-state audit"
fi
