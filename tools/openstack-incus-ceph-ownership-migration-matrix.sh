#!/usr/bin/env bash
# Release probe for ordinary Incus-owned Ceph root migration ownership.

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
E2E=${E2E:-$SCRIPT_DIR/openstack-incus-live-migration-e2e.sh}
RUN_DESTRUCTIVE=${RUN_DESTRUCTIVE:-false}
IMAGE=${IMAGE:?Set IMAGE to a CRIU-capable admitted container image}
FLAVOR=${FLAVOR:?Set FLAVOR to a flavor selecting an ordinary Ceph root pool}
NETWORK=${NETWORK:?Set NETWORK to a tenant network}
SOURCE_HOST=${SOURCE_HOST:?Set SOURCE_HOST to the first Nova compute}
SOURCE_SSH=${SOURCE_SSH:?Set SOURCE_SSH to the first compute SSH target}
NODE02_HOST=${NODE02_HOST:?Set NODE02_HOST to the second Nova compute}
NODE02_SSH=${NODE02_SSH:?Set NODE02_SSH to the second compute SSH target}
NODE03_HOST=${NODE03_HOST:?Set NODE03_HOST to the third Nova compute}
NODE03_SSH=${NODE03_SSH:?Set NODE03_SSH to the third compute SSH target}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
SSH_KNOWN_HOSTS_FILE=${SSH_KNOWN_HOSTS_FILE:-$HOME/.ssh/known_hosts}
CONTROLLER_SSH=${CONTROLLER_SSH:-}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
TIMEOUT=${TIMEOUT:-900}
RUN_UUID=${RUN_UUID:-$(python3 -c 'import uuid; print(uuid.uuid4())')}

if [[ "$RUN_DESTRUCTIVE" != true ]]; then
    echo "Refusing Ceph migration ownership matrix; set RUN_DESTRUCTIVE=true" >&2
    exit 2
fi

[[ -x "$E2E" || -r "$E2E" ]] || {
    echo "Shared migration E2E is not readable: $E2E" >&2
    exit 2
}
[[ -f "$SSH_IDENTITY" && -r "$SSH_IDENTITY" ]] || {
    echo "SSH identity is not a readable regular file: $SSH_IDENTITY" >&2
    exit 2
}
[[ -f "$SSH_KNOWN_HOSTS_FILE" && -r "$SSH_KNOWN_HOSTS_FILE" ]] || {
    echo "SSH known_hosts is not readable: $SSH_KNOWN_HOSTS_FILE" >&2
    exit 2
}
[[ "$RUN_UUID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]] || {
    echo "RUN_UUID must be a canonical lowercase UUIDv4" >&2
    exit 2
}
[[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]] || {
    echo "TIMEOUT must be a positive integer" >&2
    exit 2
}

declare -A seen_hosts=() seen_ssh=()
for host in "$SOURCE_HOST" "$NODE02_HOST" "$NODE03_HOST"; do
    [[ -z "${seen_hosts[$host]:-}" ]] || {
        echo "Ceph ownership matrix requires three distinct Nova hosts" >&2
        exit 2
    }
    seen_hosts[$host]=1
done
for target in "$SOURCE_SSH" "$NODE02_SSH" "$NODE03_SSH"; do
    [[ -z "${seen_ssh[$target]:-}" ]] || {
        echo "Ceph ownership matrix requires three distinct SSH targets" >&2
        exit 2
    }
    seen_ssh[$target]=1
done

common=(
    RUN_DESTRUCTIVE=true
    IMAGE="$IMAGE"
    FLAVOR="$FLAVOR"
    NETWORK="$NETWORK"
    SOURCE_HOST="$SOURCE_HOST"
    SOURCE_SSH="$SOURCE_SSH"
    CONTROLLER_SSH="$CONTROLLER_SSH"
    CONTROLLER_OPENRC="$CONTROLLER_OPENRC"
    SSH_IDENTITY="$SSH_IDENTITY"
    SSH_KNOWN_HOSTS_FILE="$SSH_KNOWN_HOSTS_FILE"
    TIMEOUT="$TIMEOUT"
    BOOT_FROM_VOLUME=0
    WITH_DATA_VOLUME=0
    MANILA_SHARES=
    REQUIRE_MANAGED_CEPH_ROOT=1
    KEEP_FAILED=0
)

env "${common[@]}" \
    SERVER="incus-ceph-cold-$RUN_UUID" \
    MIGRATION_MODE=cold \
    MIGRATION_TARGETS="$NODE02_HOST=$NODE02_SSH,$SOURCE_HOST=$SOURCE_SSH,$NODE03_HOST=$NODE03_SSH" \
    MIGRATION_ACTIONS=confirm,revert,confirm \
    INJECT_RESTORE_FAILURE=0 \
    bash "$E2E"

env "${common[@]}" \
    SERVER="incus-ceph-live-$RUN_UUID" \
    MIGRATION_MODE=live \
    MIGRATION_TARGETS="$NODE02_HOST=$NODE02_SSH,$NODE03_HOST=$NODE03_SSH" \
    MIGRATION_ACTIONS= \
    INJECT_RESTORE_FAILURE=1 \
    bash "$E2E"

echo "PASS ordinary Ceph ownership migration matrix run_uuid=$RUN_UUID"
