#!/usr/bin/env bash
# Destructive BFV evacuation and returning-host quarantine release gate.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SERVER_ID=${SERVER_ID:?Set SERVER_ID to an existing disposable BFV server}
SOURCE_HOST=${SOURCE_HOST:?Set SOURCE_HOST}
DEST_HOST=${DEST_HOST:?Set DEST_HOST}
SOURCE_SSH=${SOURCE_SSH:?Set SOURCE_SSH}
DEST_SSH=${DEST_SSH:?Set DEST_SSH}
CONTROLLER_SSH=${CONTROLLER_SSH:?Set CONTROLLER_SSH}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY}
FENCE_PROVIDER=${FENCE_PROVIDER:?Set FENCE_PROVIDER executable}
SOURCE_FENCE_ID=${SOURCE_FENCE_ID:-$SOURCE_HOST}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
RETURN_AUDIT=${RETURN_AUDIT:-"$SCRIPT_DIR/openstack-incus-returning-host-audit.sh"}
CINDER_RBD_POOL=${CINDER_RBD_POOL:-cinder-volumes-rbd-pool}
INCUS_PROJECT=${INCUS_PROJECT:-nova}
TIMEOUT=${TIMEOUT:-300}

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)
marker="openstack-incus-evacuation-$(date +%s)-$$"
marker_sha=$(printf '%s\n' "$marker" | sha256sum | awk '{print $1}')

remote() {
    local target=$1
    shift
    "${SSH[@]}" "$target" "$@"
}

openstack() {
    local command_line
    printf -v command_line '%q ' "$@"
    remote "$CONTROLLER_SSH" \
        "source $CONTROLLER_OPENRC >/dev/null 2>&1; openstack $command_line"
}

wait_for() {
    local description=$1
    shift
    local deadline=$((SECONDS + TIMEOUT))
    until "$@"; do
        if ((SECONDS >= deadline)); then
            echo "Timed out waiting for $description" >&2
            return 1
        fi
        sleep 3
    done
}

server_field_is() {
    local field=$1 expected=$2
    [[ "$(openstack server show "$SERVER_ID" -f value -c "$field")" == \
       "$expected" ]]
}

source_service_down() {
    [[ "$(openstack compute service list --service nova-compute \
        --host "$SOURCE_HOST" -f value -c State)" == down ]]
}

source_reachable() {
    remote "$SOURCE_SSH" true >/dev/null 2>&1
}

source_fenced() {
    [[ "$("$FENCE_PROVIDER" status "$SOURCE_FENCE_ID")" == off ]]
}

watcher_count() {
    remote "$CONTROLLER_SSH" \
        "rbd status '$CINDER_RBD_POOL/$root_image' --id cinder \
         --format json 2>/dev/null || echo '{\"watchers\":[]}'" |
        jq '.watchers | length'
}

watchers_are() {
    [[ "$(watcher_count)" == "$1" ]]
}

original_status=$(openstack server show "$SERVER_ID" -f value -c status)
[[ "$original_status" == ACTIVE || "$original_status" == SHUTOFF ]] || {
    echo "Server must be ACTIVE or SHUTOFF, got $original_status" >&2
    exit 1
}
actual_source=$(openstack server show "$SERVER_ID" -f value \
    -c OS-EXT-SRV-ATTR:host)
[[ "$actual_source" == "$SOURCE_HOST" ]] || {
    echo "Expected source $SOURCE_HOST, got $actual_source" >&2
    exit 1
}
instance_name=$(openstack server show "$SERVER_ID" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)
root_volume=$(openstack server volume list "$SERVER_ID" -f json |
    jq -r '.[] | select(.Device == "/dev/sda") | ."Volume ID"')
[[ -n "$root_volume" ]] || {
    echo "Server does not have one Cinder BFV root at /dev/sda" >&2
    exit 1
}
root_image="volume-$root_volume"

if [[ "$original_status" == ACTIVE ]]; then
    printf '%s\n' "$marker" |
        remote "$SOURCE_SSH" \
            "podman exec -i incus incus --project '$INCUS_PROJECT' \
             file push - '$instance_name/root/stonith-e2e-marker'"
fi

"$FENCE_PROVIDER" off "$SOURCE_FENCE_ID"
wait_for "source power fencing" source_fenced
openstack compute service set --disable "$SOURCE_HOST" nova-compute
wait_for "Nova source service down" source_service_down

[[ "$(watcher_count)" == 0 ]] || {
    echo "Source is fenced but the BFV root still has a watcher" >&2
    exit 1
}

openstack server evacuate --host "$DEST_HOST" "$SERVER_ID"
wait_for "evacuation host transition" \
    server_field_is OS-EXT-SRV-ATTR:host "$DEST_HOST"
wait_for "evacuation task completion" \
    server_field_is OS-EXT-STS:task_state None

if [[ "$original_status" == ACTIVE ]]; then
    if server_field_is status SHUTOFF; then
        openstack server start "$SERVER_ID"
    fi
    wait_for "target ACTIVE state" server_field_is status ACTIVE
    wait_for "single target watcher" watchers_are 1
    recovered_sha=$(remote "$DEST_SSH" \
        "podman exec incus incus --project '$INCUS_PROJECT' \
         file pull '$instance_name/root/stonith-e2e-marker' -" |
        sha256sum | awk '{print $1}')
    [[ "$recovered_sha" == "$marker_sha" ]] || {
        echo "Root marker did not survive evacuation" >&2
        exit 1
    }
else
    server_field_is status SHUTOFF
    [[ "$(watcher_count)" == 0 ]]
fi

"$FENCE_PROVIDER" on "$SOURCE_FENCE_ID"
wait_for "returning source SSH" source_reachable
remote "$SOURCE_SSH" \
    "test ! -e /run/openstack-incus/compute-admitted"
remote "$SOURCE_SSH" \
    "! systemctl is-active --quiet devstack@n-cpu.service"
if remote "$SOURCE_SSH" \
        "podman exec incus incus --project '$INCUS_PROJECT' list \
         --format json" |
        jq -e '.[] | select(.config["user.openstack.uuid"] != null) |
            select((.status | ascii_downcase) != "stopped")' >/dev/null; then
    echo "Returning source started a tenant container before admission" >&2
    exit 1
fi

RETURNING_HOST="$SOURCE_HOST" \
RETURNING_SSH="$SOURCE_SSH" \
CONTROLLER_SSH="$CONTROLLER_SSH" \
CONTROLLER_OPENRC="$CONTROLLER_OPENRC" \
SSH_IDENTITY="$SSH_IDENTITY" \
CINDER_RBD_POOL="$CINDER_RBD_POOL" \
    "$RETURN_AUDIT"

remote "$SOURCE_SSH" \
    "/usr/local/sbin/openstack-incus-compute-admission admit \
     --reason evacuation-reconciliation-passed; \
     systemctl reset-failed devstack@n-cpu.service; \
     systemctl start devstack@n-cpu.service"

wait_for "stale source record cleanup" \
    remote "$SOURCE_SSH" \
        "! podman exec incus incus --project '$INCUS_PROJECT' \
         info '$instance_name' >/dev/null 2>&1"
openstack compute service set --enable "$SOURCE_HOST" nova-compute
wait_for "single watcher after source admission" \
    watchers_are "$([[ "$original_status" == ACTIVE ]] && echo 1 || echo 0)"

echo "PASS fenced BFV evacuation and returning-host reconciliation"
