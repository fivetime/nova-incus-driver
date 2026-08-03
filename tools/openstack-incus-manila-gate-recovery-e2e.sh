#!/usr/bin/env bash
# Manila destination pre-mount gating and host-reboot startup recovery.
#
# Three cases, all through the public API with a share this script owns:
#
#   gate     Destination NFS is unreachable, so pre_live_migration cannot
#            stage the share. The migration must fail as if nothing had
#            happened: source instance ACTIVE on its original host with an
#            unchanged process, and no mount or instance residue on the
#            destination.
#   retry    With the block removed the same migration succeeds and the
#            share content survives.
#   recovery A host reboot loses both the guest process and the host NFS
#            mount. Nova's _resume_guests_state -> _mount_all_shares path
#            must re-establish the mount and resume the guest with working
#            share access. (The Incus share-journal recovery loop is a
#            different mechanism: it cleans journal-only mounts left by a
#            terminal migration, it never remounts for a live instance.)
#
# Required: RUN_DESTRUCTIVE=true SSH_IDENTITY=... IMAGE=... FLAVOR=...
#           NETWORK=... SOURCE_HOST=... DEST_HOST=... DEST_SSH=...
#           CONTROLLER_IP=<address the destination reaches Manila on>

set -Eo pipefail
# The openrc path carries its cloud arguments, so it must word-split; it
# also reads unset variables, which -u would turn into a silent exit.
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
set +u
# shellcheck disable=SC2086
source $CONTROLLER_OPENRC >/dev/null 2>&1
set -u

RUN_DESTRUCTIVE=${RUN_DESTRUCTIVE:-false}
IMAGE=${IMAGE:-alpine-3.21-cloud-incus-criu-fuse}
FLAVOR=${FLAVOR:-m1.tiny}
NETWORK=${NETWORK:-public}
SOURCE_HOST=${SOURCE_HOST:-incus-node-01}
DEST_HOST=${DEST_HOST:-incus-node-02}
DEST_SSH=${DEST_SSH:-root@10.32.32.131}
CONTROLLER_IP=${CONTROLLER_IP:-10.32.32.130}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
SHARE_TYPE=${SHARE_TYPE:-incus-nfs}
SHARE_PROTO=${SHARE_PROTO:-NFS}
SHARE_SIZE=${SHARE_SIZE:-1}
CLIENT_CIDR=${CLIENT_CIDR:-10.32.32.128/27}
TAG=${TAG:-tenant-data}
NAME=${NAME:-incus-manila-gate-$RANDOM}
TIMEOUT=${TIMEOUT:-420}
CASES=${CASES:-gate,retry,recovery}

[[ "$RUN_DESTRUCTIVE" == true ]] || {
    echo "Set RUN_DESTRUCTIVE=true to run this destructive case" >&2
    exit 2
}

SSH=(ssh -n -o BatchMode=yes -o StrictHostKeyChecking=no -i "$SSH_IDENTITY")
server_id=
share_id=
blocked=0

fail() { echo "FAIL $1" >&2; exit 1; }

case_selected() { [[ ",$CASES," == *",$1,"* ]]; }

dest() { "${SSH[@]}" "$DEST_SSH" "$@"; }

dest_incus() {
    # --project must precede the subcommand: appending it would land after
    # the "--" separator of exec and be handed to the guest command.
    dest "podman exec incus incus --project nova $*"
}

unblock_nfs() {
    ((blocked)) || return 0
    dest "iptables -D OUTPUT -p tcp -d $CONTROLLER_IP --dport 2049 -j REJECT" \
        >/dev/null 2>&1
    blocked=0
}

cleanup() {
    local status=$?
    set +e
    unblock_nfs
    if [[ -n "$server_id" ]]; then
        openstack server stop "$server_id" >/dev/null 2>&1
        wait_status SHUTOFF >/dev/null 2>&1
        [[ -n "$share_id" ]] &&
            api DELETE "$shares_url/$share_id" >/dev/null 2>&1
        openstack server delete --wait "$server_id" >/dev/null 2>&1
    fi
    [[ -n "$share_id" ]] &&
        openstack share delete "$share_id" >/dev/null 2>&1
    exit "$status"
}

wait_status() {
    local expected=$1 deadline=$((SECONDS + TIMEOUT)) current=
    while ((SECONDS < deadline)); do
        current=$(openstack server show "$server_id" -f value -c status \
            2>/dev/null)
        [[ "$current" == "$expected" ]] && return 0
        sleep 3
    done
    echo "Server did not reach $expected (current: ${current:-missing})" >&2
    return 1
}

server_host() {
    openstack --os-compute-api-version 2.74 server show "$server_id" \
        -f value -c OS-EXT-SRV-ATTR:host
}

guest_pid() {
    dest_incus "info $instance_name" 2>/dev/null | grep -oP 'PID: \K[0-9]+'
}

source_pid() {
    "${SSH[@]}" "$SOURCE_SSH" \
        "podman exec incus incus --project nova info $instance_name" |
        grep -oP 'PID: \K[0-9]+'
}

api() {
    local method=$1 url=$2 data=${3:-}
    local args=(-fsS -X "$method" -H "X-Auth-Token: $token"
        -H "OpenStack-API-Version: compute 2.97")
    [[ -n "$data" ]] && args+=(-H "Content-Type: application/json" -d "$data")
    curl "${args[@]}" "$url"
}

SOURCE_SSH=${SOURCE_SSH:-root@$CONTROLLER_IP}
trap cleanup EXIT INT TERM

share_id=$(openstack share create "$SHARE_PROTO" "$SHARE_SIZE" \
    --name "$NAME-share" --share-type "$SHARE_TYPE" -f value -c id)
deadline=$((SECONDS + TIMEOUT))
while ((SECONDS < deadline)); do
    share_status=$(openstack share show "$share_id" -f value -c status)
    [[ "$share_status" == available ]] && break
    [[ "$share_status" == error ]] && fail "share creation errored"
    sleep 5
done
[[ "$share_status" == available ]] || fail "share never became available"
openstack share access create "$share_id" ip "$CLIENT_CIDR" \
    --access-level rw >/dev/null
deadline=$((SECONDS + TIMEOUT))
while ((SECONDS < deadline)); do
    access_state=$(openstack share access list "$share_id" -f value -c State |
        head -1)
    [[ "$access_state" == active ]] && break
    sleep 3
done
[[ "$access_state" == active ]] || fail "share access rule never activated"

server_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --image "$IMAGE" --network "$NETWORK" \
    --host "$SOURCE_HOST" "$NAME-server" -f value -c id)
wait_status ACTIVE
instance_name=$(openstack --os-compute-api-version 2.74 server show \
    "$server_id" -f value -c OS-EXT-SRV-ATTR:instance_name)

token=$(openstack token issue -f value -c id)
endpoint=$(openstack endpoint list --service nova --interface public \
    -f value -c URL | head -1)
project_id=$(openstack server show "$server_id" -f value -c project_id)
endpoint=${endpoint//\%\(project_id\)s/$project_id}
shares_url="$endpoint/servers/$server_id/shares"

openstack server stop "$server_id"
wait_status SHUTOFF
api POST "$shares_url" \
    "{\"share\":{\"share_id\":\"$share_id\",\"tag\":\"$TAG\"}}" >/dev/null
deadline=$((SECONDS + TIMEOUT))
while ((SECONDS < deadline)); do
    api GET "$shares_url" | grep -q '"status": *"active"' && break
    sleep 3
done
openstack server start "$server_id"
wait_status ACTIVE
"${SSH[@]}" "$SOURCE_SSH" "podman exec incus incus exec $instance_name \
    --project nova -- sh -c 'echo $NAME > /mnt/manila/$TAG/marker && sync'" ||
    fail "guest cannot write the attached share"

if case_selected gate; then
    pid_before=$(source_pid)
    dest "iptables -I OUTPUT -p tcp -d $CONTROLLER_IP --dport 2049 -j REJECT"
    blocked=1
    openstack server migrate --live-migration --host "$DEST_HOST" \
        --wait "$server_id" >/dev/null 2>&1 || true
    sleep 5
    migration_status=$(openstack server migration list --server "$server_id" \
        -f value -c Id -c Status | sort -n | tail -n1 | awk '{print $2}')
    [[ "$migration_status" == failed || "$migration_status" == error ]] ||
        fail "blocked-destination migration reported $migration_status"
    unblock_nfs
    [[ "$(openstack server show "$server_id" -f value -c status)" == ACTIVE ]] ||
        fail "source instance is not ACTIVE after the gated failure"
    [[ "$(server_host)" == "$SOURCE_HOST" ]] ||
        fail "instance left its source host after the gated failure"
    [[ "$(source_pid)" == "$pid_before" ]] ||
        fail "source guest process changed after the gated failure"
    residue=$(dest "findmnt -rn | grep -c incus-shares/$server_id || true")
    [[ "${residue//[$'\r\n']}" == 0 ]] ||
        fail "destination retained $residue share mount(s)"
    records=$(dest "podman exec incus incus list --project nova -f csv -c n |
        grep -c $instance_name || true")
    [[ "${records//[$'\r\n']}" == 0 ]] ||
        fail "destination retained an instance record"
    echo "PASS manila destination gate failed the migration cleanly"
fi

if case_selected retry; then
    openstack server migrate --live-migration --host "$DEST_HOST" \
        --wait "$server_id" >/dev/null 2>&1 || true
    wait_status ACTIVE
    [[ "$(server_host)" == "$DEST_HOST" ]] ||
        fail "retried migration did not land on $DEST_HOST"
    dest_incus "exec $instance_name -- cat /mnt/manila/$TAG/marker" |
        grep -q "$NAME" || fail "share content lost across the retry"
    echo "PASS manila migration retry succeeded with the share intact"
fi

if case_selected recovery; then
    [[ "$(server_host)" == "$DEST_HOST" ]] || {
        openstack server migrate --live-migration --host "$DEST_HOST" \
            --wait "$server_id" >/dev/null 2>&1 || true
        wait_status ACTIVE
    }
    staging=$(dest "findmnt -rn -o TARGET | \
        grep incus-shares/$server_id | head -1")
    staging=${staging//[$'\r\n']}
    [[ -n "$staging" ]] || fail "no host staging mount to lose"
    dest_incus "stop $instance_name --force"
    dest "umount -l '$staging'"
    dest "findmnt -rn '$staging' >/dev/null" &&
        fail "staging mount survived the simulated reboot"
    dest "systemctl restart devstack@n-cpu"
    remounted=0
    deadline=$((SECONDS + TIMEOUT))
    while ((SECONDS < deadline)); do
        dest "findmnt -rn '$staging' >/dev/null" && { remounted=1; break; }
        sleep 5
    done
    ((remounted)) || fail "host share mount was not re-established"
    resumed=0
    deadline=$((SECONDS + TIMEOUT))
    while ((SECONDS < deadline)); do
        [[ "$(dest_incus "list $instance_name --format csv -c s" |
            tr -d '\r')" == RUNNING ]] && { resumed=1; break; }
        sleep 5
    done
    ((resumed)) || fail "guest was not resumed after the host boot"
    dest_incus "exec $instance_name -- cat /mnt/manila/$TAG/marker" |
        grep -q "$NAME" || fail "guest lost share access after recovery"
    dest_incus "exec $instance_name -- sh -c \
        'echo recovered >> /mnt/manila/$TAG/marker && sync'" ||
        fail "recovered share is not writable"
    mapping_status=$(api GET "$shares_url" | python3 -c \
        'import json,sys; print(json.load(sys.stdin)["shares"][0]["status"])')
    [[ "$mapping_status" == active ]] ||
        fail "share mapping is $mapping_status after recovery"
    echo "PASS manila host-reboot recovery remounted and resumed the guest"
fi

echo "PASS Incus Manila gate and recovery cases=$CASES server=$server_id" \
    "share=$share_id"
