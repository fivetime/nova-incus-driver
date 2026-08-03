#!/usr/bin/env bash
# Destructive 30-instance BFV evacuation and returning-host release gate.

set +u
# shellcheck source=/dev/null
source /opt/stack/devstack/openrc admin admin >/dev/null 2>&1
set -euo pipefail

PHASE=${1:?Usage: $0 online|fence-evacuate|retry-evacuate|return|cleanup}
STATE=${STATE:-/root/incus-evac-30.state}
SOURCE_HOST=${SOURCE_HOST:-incus-node-02}
DEST_HOST=${DEST_HOST:-incus-node-03}
SOURCE_SSH=${SOURCE_SSH:-root@10.32.32.131}
DEST_SSH=${DEST_SSH:-root@10.32.32.132}
CONTROLLER_SSH=${CONTROLLER_SSH:-root@10.32.32.130}
SSH_IDENTITY=${SSH_IDENTITY:-/root/.ssh/openstack-incus-vm_ed25519}
FENCE_PROVIDER=${FENCE_PROVIDER:-/usr/local/sbin/openstack-incus-fence-agent-provider}
RETURN_AUDIT=${RETURN_AUDIT:-/opt/stack/openstack-incus/tools/openstack-incus-returning-host-audit.sh}
EXPECTED_COUNT=${EXPECTED_COUNT:-30}
TIMEOUT=${TIMEOUT:-1200}
CINDER_RBD_POOL=${CINDER_RBD_POOL:-cinder-volumes-rbd-pool}

SSH=(ssh -n -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

wait_for() {
    local description=$1
    shift
    local deadline=$((SECONDS + TIMEOUT))
    until "$@"; do
        ((SECONDS < deadline)) || fail "timed out waiting for $description"
        sleep 3
    done
}

server_rows() {
    grep '^SERVER ' "$STATE"
}

assert_state_count() {
    local count
    count=$(server_rows | wc -l)
    [[ "$count" == "$EXPECTED_COUNT" ]] ||
        fail "state has $count servers, expected $EXPECTED_COUNT"
}

service_is() {
    local expected=$1
    [[ "$(openstack compute service list --service nova-compute \
        --host "$SOURCE_HOST" -f value -c State)" == "$expected" ]]
}

all_servers_on_host() {
    local expected_host=$1 expected_status=$2
    local _kind number server volume host status task count=0
    while read -r _kind number server volume; do
        host=$(openstack server show "$server" -f value \
            -c OS-EXT-SRV-ATTR:host 2>/dev/null) || return 1
        status=$(openstack server show "$server" -f value -c status)
        task=$(openstack server show "$server" -f value \
            -c OS-EXT-STS:task_state)
        [[ "$host" == "$expected_host" &&
           "$status" == "$expected_status" &&
           "$task" == None ]] || return 1
        count=$((count + 1))
    done < <(server_rows)
    [[ "$count" == "$EXPECTED_COUNT" ]]
}

all_watchers_are() {
    local expected=$1
    local _kind number server volume count image_id output
    while read -r _kind number server volume; do
        image_id=$(rados --id cinder -p "$CINDER_RBD_POOL" get \
            "rbd_id.volume-$volume" - 2>/dev/null | tail -c +5)
        [[ "$image_id" =~ ^[0-9a-f]+$ ]] || return 1
        output=$(rados --id cinder -p "$CINDER_RBD_POOL" \
            listwatchers "rbd_header.$image_id") || return 1
        count=$(sed '/^[[:space:]]*$/d' <<<"$output" | wc -l)
        [[ "$count" == "$expected" ]] || return 1
    done < <(server_rows)
}

verify_attachments() {
    local attachments _kind number server volume total matching count=0
    attachments=$(openstack volume attachment list -f json)
    while read -r _kind number server volume; do
        total=$(jq -r --arg volume "$volume" \
            '[.[] | select(."Volume ID" == $volume)] | length' \
            <<<"$attachments")
        matching=$(jq -r --arg volume "$volume" --arg server "$server" \
            '[.[] | select(."Volume ID" == $volume and
                ."Server ID" == $server and .Status == "attached")] | length' \
            <<<"$attachments")
        [[ "$total" == 1 && "$matching" == 1 ]] ||
            fail "$number attachment total=$total matching=$matching"
        count=$((count + 1))
    done < <(server_rows)
    [[ "$count" == "$EXPECTED_COUNT" ]]
}

verify_markers() {
    local _kind number server volume instance expected actual count=0
    while read -r _kind number server volume; do
        instance=$(openstack server show "$server" -f value \
            -c OS-EXT-SRV-ATTR:instance_name)
        expected="evac-marker-$server-$volume"
        actual=$("${SSH[@]}" "$DEST_SSH" \
            "podman exec incus incus --project nova file pull \
             '$instance/root/evacuation-marker' -" |
            sed $'1s/^\xEF\xBB\xBF//' | tr -d '\r\n')
        [[ "$actual" == "$expected" ]] ||
            fail "$number root marker mismatch"
        count=$((count + 1))
    done < <(server_rows)
    [[ "$count" == "$EXPECTED_COUNT" ]]
}

source_is_fenced() {
    [[ "$("$FENCE_PROVIDER" status "$SOURCE_HOST")" == off ]]
}

source_is_powered_on() {
    [[ "$("$FENCE_PROVIDER" status "$SOURCE_HOST")" == on ]]
}

source_is_reachable() {
    "${SSH[@]}" "$SOURCE_SSH" true >/dev/null 2>&1
}

source_incus_ready() {
    "${SSH[@]}" "$SOURCE_SSH" \
        "podman exec incus incus query /1.0 >/dev/null 2>&1"
}

case "$PHASE" in
online)
    assert_state_count
    service_is up || fail "$SOURCE_HOST compute service is not up"
    rejected=0
    while read -r _kind number server volume; do
        if output=$(openstack server evacuate --host "$DEST_HOST" \
                "$server" 2>&1); then
            fail "online evacuation was accepted for $number: $output"
        fi
        [[ "$output" == *"is still in use"* ]] ||
            fail "unexpected online rejection for $number: $output"
        rejected=$((rejected + 1))
    done < <(server_rows)
    [[ "$rejected" == "$EXPECTED_COUNT" ]]
    all_servers_on_host "$SOURCE_HOST" ACTIVE ||
        fail "online rejection changed server ownership or state"
    verify_attachments
    all_watchers_are 1 || fail "online rejection changed RBD watchers"
    echo "PASS online evacuation rejected all $EXPECTED_COUNT servers safely"
    ;;

fence-evacuate)
    assert_state_count
    all_servers_on_host "$SOURCE_HOST" ACTIVE ||
        fail "servers are not all ACTIVE on source"
    "$FENCE_PROVIDER" off "$SOURCE_HOST"
    wait_for "source power off" source_is_fenced
    openstack compute service set --disable "$SOURCE_HOST" nova-compute
    wait_for "Nova source service down" service_is down
    wait_for "all source RBD watchers to disappear" all_watchers_are 0

    while read -r _kind number server volume; do
        openstack server evacuate --host "$DEST_HOST" "$server" >/dev/null
    done < <(server_rows)
    wait_for "all evacuations to finish" \
        all_servers_on_host "$DEST_HOST" ACTIVE
    wait_for "all destination RBD watchers" all_watchers_are 1
    verify_attachments
    verify_markers
    echo "PASS fenced evacuation recovered all $EXPECTED_COUNT servers"
    ;;

retry-evacuate)
    assert_state_count
    source_is_fenced || fail "$SOURCE_HOST is not fenced"
    service_is down || fail "$SOURCE_HOST compute service is not down"
    declare -A attempts=()
    retry_deadline=$((SECONDS + TIMEOUT))
    while ! all_servers_on_host "$DEST_HOST" ACTIVE; do
        ((SECONDS < retry_deadline)) ||
            fail "timed out retrying failed evacuations"
        submitted=0
        while read -r _kind number server volume; do
            status=$(openstack server show "$server" -f value -c status)
            host=$(openstack server show "$server" -f value \
                -c OS-EXT-SRV-ATTR:host)
            case "$status:$host" in
            ACTIVE:"$DEST_HOST"|REBUILD:*)
                ;;
            ERROR:*)
                attempts["$server"]=$(( ${attempts["$server"]:-0} + 1 ))
                (( attempts["$server"] <= 3 )) ||
                    fail "$number exceeded three evacuation retries"
                openstack server evacuate --host "$DEST_HOST" \
                    "$server" >/dev/null
                submitted=$((submitted + 1))
                ((submitted % 5 != 0)) || sleep 5
                ;;
            *)
                fail "$number has unexpected state $status on $host"
                ;;
            esac
        done < <(server_rows)
        sleep 10
    done
    wait_for "all evacuation retries to finish" \
        all_servers_on_host "$DEST_HOST" ACTIVE
    wait_for "all destination RBD watchers" all_watchers_are 1
    verify_attachments
    verify_markers
    echo "PASS evacuation retry recovered all $EXPECTED_COUNT servers"
    ;;

return)
    assert_state_count
    if source_is_fenced; then
        "$FENCE_PROVIDER" on "$SOURCE_HOST"
    elif ! source_is_powered_on; then
        fail "cannot determine $SOURCE_HOST power state"
    fi
    wait_for "returning source SSH" source_is_reachable
    wait_for "returning source Incus" source_incus_ready
    "${SSH[@]}" "$SOURCE_SSH" \
        "test ! -e /run/openstack-incus/compute-admitted"
    "${SSH[@]}" "$SOURCE_SSH" \
        "! systemctl is-active --quiet devstack@n-cpu.service"
    if "${SSH[@]}" "$SOURCE_SSH" \
            "podman exec incus incus --project nova list --format json" |
            jq -e '.[] | select(.config["user.openstack.uuid"] != null) |
                select((.status | ascii_downcase) != "stopped")' >/dev/null; then
        fail "returning source started a tenant container"
    fi

    RETURNING_HOST="$SOURCE_HOST" \
    RETURNING_SSH="$SOURCE_SSH" \
    CONTROLLER_SSH="$CONTROLLER_SSH" \
    SSH_IDENTITY="$SSH_IDENTITY" \
    CINDER_RBD_POOL="$CINDER_RBD_POOL" \
        bash "$RETURN_AUDIT"

    "${SSH[@]}" "$SOURCE_SSH" \
        "/usr/local/sbin/openstack-incus-compute-admission admit \
         --reason batch-evacuation-reconciliation-passed; \
         systemctl reset-failed devstack@n-cpu.service; \
         systemctl start devstack@n-cpu.service"
    wait_for "source Nova service up" service_is up

    stale_records_absent() {
        local records
        records=$("${SSH[@]}" "$SOURCE_SSH" \
            "podman exec incus incus --project nova list --format json" |
            jq '[.[] | select(.config["user.openstack.uuid"] != null)] |
                length')
        [[ "$records" == 0 ]]
    }
    wait_for "stale source records cleanup" stale_records_absent
    openstack compute service set --enable "$SOURCE_HOST" nova-compute
    all_servers_on_host "$DEST_HOST" ACTIVE ||
        fail "authoritative server ownership changed after source return"
    all_watchers_are 1 || fail "RBD watcher ownership is not unique"
    verify_attachments
    verify_markers
    echo "PASS returning source quarantined and reconciled $EXPECTED_COUNT servers"
    ;;

cleanup)
    assert_state_count
    while read -r _kind number server volume; do
        openstack server delete "$server" || true
    done < <(server_rows)
    wait_for "server deletion" bash -c \
        "! openstack server list --all-projects -f value -c ID |
           grep -Fxf <(awk '/^SERVER / {print \$3}' '$STATE') >/dev/null"
    while read -r _kind _number _server volume; do
        for _ in {1..120}; do
            status=$(openstack volume show "$volume" -f value \
                -c status 2>/dev/null || true)
            [[ "$status" == available || -z "$status" ]] && break
            sleep 2
        done
        openstack volume delete "$volume" || true
    done < <(server_rows)
    echo "PASS batch evacuation resources deleted"
    ;;

*)
    fail "unknown phase: $PHASE"
    ;;
esac
