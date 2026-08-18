#!/usr/bin/env bash
# Report start-fence states that lack a safe public-API fault injection hook.

set -Eeuo pipefail

FENCE_STATE=${FENCE_STATE:-}

skip() {
    echo "SKIP: FENCE_STATE=$FENCE_STATE is unverified: $*" >&2
    exit 77
}

case "$FENCE_STATE" in
    active)
        skip "the materialization API rejects registration over an existing instance, so an active attempt cannot be attached to a disposable start candidate"
        ;;
    aborted)
        skip "an aborted materialization is terminal and cannot be rebound to an existing instance through the public API"
        ;;
    pending-release)
        skip "there is no public fault hook that pauses after receipt Begin and before exact root release"
        ;;
    uncommitted)
        skip "OpenStack exposes no supported API that makes an incomplete root materialization startable"
        ;;
    possible)
        skip "possible is a Nova allocator claim state and cannot be injected without directly corrupting the shared registry"
        ;;
    '')
        echo "Set FENCE_STATE to active, aborted, pending-release, uncommitted or possible" >&2
        exit 2
        ;;
    *)
        echo "Unknown FENCE_STATE: $FENCE_STATE" >&2
        exit 2
        ;;
esac
