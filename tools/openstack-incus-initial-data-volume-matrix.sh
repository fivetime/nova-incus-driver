#!/usr/bin/env bash
# Run: RUN_DESTRUCTIVE=true LOCAL_IMAGE=... BFV_IMAGE=... FLAVOR=... \
#      NETWORK=... VOLUME_TYPE=... [ROOT_VOLUME_TYPE=...] $0
# Release proof: REQUIRE_HOST_CLEANUP_AUDIT=true COMPUTE_NODES=host=ssh,... \
#      SSH_IDENTITY=... SSH_KNOWN_HOSTS_FILE=... NOVA_INSTANCES_PATH=... $0

set -Eeuo pipefail

RUN_DESTRUCTIVE=${RUN_DESTRUCTIVE:-false}
if [[ "$RUN_DESTRUCTIVE" != true ]]; then
    echo "Refusing destructive matrix; set RUN_DESTRUCTIVE=true" >&2
    exit 2
fi

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
case_prefix=${NAME_PREFIX:-incus-initial-data-matrix-$(date +%s)}
root_modes=${ROOT_MODES:-"local bfv"}
data_counts=${DATA_VOLUME_COUNTS:-"0 1 3"}
LOCAL_IMAGE=${LOCAL_IMAGE:-${IMAGE:-}}
BFV_IMAGE=${BFV_IMAGE:-${IMAGE:-}}
REQUIRE_HOST_CLEANUP_AUDIT=${REQUIRE_HOST_CLEANUP_AUDIT:-false}
evidence=

cleanup() {
    [[ -z "$evidence" ]] || rm -f "$evidence"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

case "$REQUIRE_HOST_CLEANUP_AUDIT" in
    true|false) ;;
    *)
        echo "REQUIRE_HOST_CLEANUP_AUDIT must be true or false" >&2
        exit 2
        ;;
esac
if [[ "$REQUIRE_HOST_CLEANUP_AUDIT" == true ]]; then
    : "${COMPUTE_NODES:?Set COMPUTE_NODES to host=ssh mappings}"
    : "${SSH_IDENTITY:?Set SSH_IDENTITY to the compute audit key}"
    : "${SSH_KNOWN_HOSTS_FILE:?Set SSH_KNOWN_HOSTS_FILE}"
    : "${NOVA_INSTANCES_PATH:?Set the absolute Nova instances_path}"
fi

[[ -n "$LOCAL_IMAGE" ]] || {
    echo "Set LOCAL_IMAGE to the admitted unified Incus image" >&2
    exit 2
}
[[ -n "$BFV_IMAGE" ]] || {
    echo "Set BFV_IMAGE to the admitted Incus BFV image" >&2
    exit 2
}

for root_mode in $root_modes; do
    [[ "$root_mode" == local || "$root_mode" == bfv ]] || {
        echo "Unsupported ROOT_MODES entry: $root_mode" >&2
        exit 2
    }
    for data_count in $data_counts; do
        [[ "$data_count" =~ ^[0-9]+$ ]] || {
            echo "Invalid DATA_VOLUME_COUNTS entry: $data_count" >&2
            exit 2
        }
        echo "=== initial data-volume case root=$root_mode count=$data_count ==="
        case_image=$LOCAL_IMAGE
        [[ "$root_mode" == bfv ]] && case_image=$BFV_IMAGE
        evidence=$(mktemp)
        rm -f "$evidence"
        ROOT_MODE=$root_mode DATA_VOLUME_COUNT=$data_count \
            IMAGE="$case_image" \
            NAME="$case_prefix-$root_mode-$data_count" \
            EVIDENCE_FILE="$evidence" \
            "$script_dir/openstack-incus-initial-data-volume-e2e.sh"
        [[ -s "$evidence" ]] || {
            echo "Initial-volume case produced no cleanup evidence" >&2
            exit 1
        }
        if [[ "$REQUIRE_HOST_CLEANUP_AUDIT" == true ]]; then
            EVIDENCE_FILE="$evidence" \
                "$script_dir/openstack-incus-data-volume-cleanup-audit.sh"
        fi
        rm -f "$evidence"
        evidence=
    done
done

echo "PASS initial data-volume matrix roots=[$root_modes] counts=[$data_counts]"
