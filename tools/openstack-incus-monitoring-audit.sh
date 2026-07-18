#!/usr/bin/env bash
# Fail-closed fleet and ownership signals for a production monitoring probe.

set -uo pipefail

COMPUTE_NODES=${COMPUTE_NODES:?Set host=ssh-target comma-separated nodes}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute audit key}
EXPECTED_INCUS_IMAGE_DIGEST=${EXPECTED_INCUS_IMAGE_DIGEST:?Set approved digest}
EXPECTED_INCUS_REVISION=${EXPECTED_INCUS_REVISION:?Set approved source revision}
CONTROLLER_SSH=${CONTROLLER_SSH:?Set CONTROLLER_SSH}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
INCUS_PROJECT=${INCUS_PROJECT:-nova}
CONTROL_FS_WARNING_PERCENT=${CONTROL_FS_WARNING_PERCENT:-80}
CONSOLE_LOG_WARNING_BYTES=${CONSOLE_LOG_WARNING_BYTES:-268435456}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
FLEET_PREFLIGHT=${FLEET_PREFLIGHT:-$SCRIPT_DIR/openstack-incus-fleet-preflight.sh}

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)
failures=0
declare -A mapping_owners=()

pass() {
    printf 'PASS %-40s %s\n' "$1" "${2:-}"
}

fail() {
    printf 'FAIL %-40s %s\n' "$1" "$2" >&2
    failures=$((failures + 1))
}

remote() {
    local target=$1
    shift
    "${SSH[@]}" "$target" "$@"
}

[[ "$CONTROL_FS_WARNING_PERCENT" =~ ^[1-9][0-9]?$ ]] ||
    { echo "CONTROL_FS_WARNING_PERCENT must be between 1 and 99" >&2; exit 2; }
[[ "$CONSOLE_LOG_WARNING_BYTES" =~ ^[1-9][0-9]*$ ]] ||
    { echo "CONSOLE_LOG_WARNING_BYTES must be positive" >&2; exit 2; }

if COMPUTE_NODES="$COMPUTE_NODES" \
        SSH_IDENTITY="$SSH_IDENTITY" \
        EXPECTED_INCUS_IMAGE_DIGEST="$EXPECTED_INCUS_IMAGE_DIGEST" \
        EXPECTED_INCUS_REVISION="$EXPECTED_INCUS_REVISION" \
        CONTROLLER_SSH="$CONTROLLER_SSH" \
        CONTROLLER_OPENRC="$CONTROLLER_OPENRC" \
        bash "$FLEET_PREFLIGHT"; then
    pass "fleet drift"
else
    fail "fleet drift" "strict fleet preflight failed"
fi

IFS=, read -ra nodes <<<"$COMPUTE_NODES"
for node in "${nodes[@]}"; do
    host=${node%%=*}
    target=${node#*=}
    if [[ -z "$host" || -z "$target" || "$host" == "$target" ]]; then
        fail "node declaration" "invalid entry: $node"
        continue
    fi

    compute_state=$(remote "$target" \
        "systemctl is-active devstack@n-cpu.service 2>/dev/null || true")
    if [[ "$compute_state" == active ]]; then
        if remote "$target" \
                /usr/local/sbin/openstack-incus-compute-admission check \
                >/dev/null 2>&1; then
            pass "$host admission" "active/current-boot"
        else
            fail "$host admission" \
                "nova-compute is active without a current admission token"
        fi
    else
        pass "$host nova-compute" "${compute_state:-inactive}"
    fi

    for path in /var/lib/incus /var/log/incus; do
        usage=$(remote "$target" \
            "df -P '$path' 2>/dev/null | awk 'NR == 2 {gsub(/%/, \"\", \$5); print \$5}'")
        if [[ "$usage" =~ ^[0-9]+$ ]] &&
                ((usage < CONTROL_FS_WARNING_PERCENT)); then
            pass "$host $path pressure" "$usage%"
        elif [[ "$usage" =~ ^[0-9]+$ ]]; then
            fail "$host $path pressure" \
                "$usage% >= $CONTROL_FS_WARNING_PERCENT%"
        else
            fail "$host $path pressure" "filesystem usage unavailable"
        fi
    done

    largest_log=$(remote "$target" \
        "find /var/log/incus -xdev -type f -printf '%s\n' 2>/dev/null |
         sort -nr | head -1")
    largest_log=${largest_log:-0}
    if [[ "$largest_log" =~ ^[0-9]+$ ]] &&
            ((largest_log <= CONSOLE_LOG_WARNING_BYTES)); then
        pass "$host Incus log bound" "$largest_log bytes"
    else
        fail "$host Incus log bound" \
            "${largest_log:-unknown} > $CONSOLE_LOG_WARNING_BYTES bytes"
    fi

    instance_json=$(remote "$target" \
        "podman exec incus incus --project '$INCUS_PROJECT' \
         list --format json" 2>/dev/null) || {
        fail "$host Incus inventory" "query failed"
        continue
    }
    pending=$(jq -r \
        '[.[] | select(.config["volatile.migration.storage_handover"] ==
          "pending") | .name] | join(",")' <<<"$instance_json")
    if [[ -z "$pending" ]]; then
        pass "$host pending handover" absent
    else
        fail "$host pending handover" "$pending"
    fi

    profile_json=$(remote "$target" \
        "podman exec incus incus --project '$INCUS_PROJECT' \
         profile list --format json" 2>/dev/null) || {
        fail "$host Incus profiles" "query failed"
        continue
    }
    recovery=$(jq -r \
        '[.[] | select(.config["user.openstack.recovery_required"] != null) |
          .name] | join(",")' <<<"$profile_json")
    if [[ -z "$recovery" ]]; then
        pass "$host recovery marker" absent
    else
        fail "$host recovery marker" "$recovery"
    fi

    mappings=$(remote "$target" \
        "rbd device list --format json --id cinder 2>/dev/null ||
         printf '[]'" 2>/dev/null)
    while IFS= read -r image; do
        [[ -n "$image" ]] || continue
        if [[ -n "${mapping_owners[$image]:-}" ]]; then
            fail "duplicate KRBD mapping:$image" \
                "${mapping_owners[$image]},$host"
        else
            mapping_owners[$image]=$host
        fi
    done < <(jq -r '.[].name' <<<"$mappings")
done

if ((failures > 0)); then
    echo "FAIL monitoring audit: $failures signal(s) require action" >&2
    exit 1
fi

echo "PASS monitoring audit"
