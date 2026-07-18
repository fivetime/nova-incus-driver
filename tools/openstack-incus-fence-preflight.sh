#!/usr/bin/env bash
# Non-destructive validation of every configured production fence target.

set -euo pipefail

FENCE_PROVIDER=${FENCE_PROVIDER:-/usr/local/sbin/openstack-incus-fence-agent-provider}
FENCE_IDS=${FENCE_IDS:?Set FENCE_IDS to a comma-separated compute fence ID list}

[[ -x "$FENCE_PROVIDER" ]] || {
    echo "Fence provider is not executable: $FENCE_PROVIDER" >&2
    exit 1
}

failures=0
IFS=, read -ra ids <<<"$FENCE_IDS"
for fence_id in "${ids[@]}"; do
    if [[ -z "$fence_id" ]]; then
        echo "FAIL empty fence ID" >&2
        failures=$((failures + 1))
        continue
    fi
    if state=$("$FENCE_PROVIDER" status "$fence_id") &&
            [[ "$state" == on || "$state" == off ]]; then
        printf 'PASS %-38s %s\n' "$fence_id fence target" "$state"
    else
        echo "FAIL $fence_id fence target: status unavailable" >&2
        failures=$((failures + 1))
    fi
done

if ((failures > 0)); then
    echo "FAIL fence preflight: $failures target(s) failed" >&2
    exit 1
fi

echo "PASS fence preflight"
