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
TIMEOUT=${TIMEOUT:-}

SSH=(ssh -n -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)
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

nova_service_down_time=$(
    remote "$CONTROLLER_SSH" \
        "crudini --get /etc/nova/nova.conf DEFAULT service_down_time \
         2>/dev/null || echo 60"
)
[[ "$nova_service_down_time" =~ ^[0-9]+$ ]] || {
    echo "Invalid Nova service_down_time: $nova_service_down_time" >&2
    exit 1
}
minimum_timeout=$((nova_service_down_time + 120))
if [[ -z "$TIMEOUT" ]]; then
    TIMEOUT=$minimum_timeout
elif ((TIMEOUT < minimum_timeout)); then
    echo "TIMEOUT=$TIMEOUT is unsafe: Nova service_down_time is" \
         "$nova_service_down_time; use at least $minimum_timeout seconds" >&2
    exit 1
fi

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

source_incus_ready() {
    remote "$SOURCE_SSH" \
        "podman exec incus incus query /1.0 >/dev/null 2>&1"
}

control_plane_is_independent() {
    local endpoint host address source_address=${SOURCE_SSH#*@}
    local -A source_addresses=()
    local -a endpoints=()

    source_addresses["$source_address"]=1
    while IFS= read -r address; do
        [[ -n "$address" ]] && source_addresses["$address"]=1
    done < <(getent ahostsv4 "$source_address" 2>/dev/null |
        awk '{print $1}' | sort -u)

    mapfile -t endpoints < <(openstack endpoint list -f json |
        jq -r '.[] | .URL // .url // empty' | sort -u)
    (( ${#endpoints[@]} > 0 )) || {
        echo "OpenStack endpoint inventory is empty" >&2
        return 1
    }

    for endpoint in "${endpoints[@]}"; do
        host=$(python3 -c \
            'import sys; from urllib.parse import urlsplit; print(urlsplit(sys.argv[1]).hostname or "")' \
            "$endpoint") || {
            echo "Cannot determine host for OpenStack endpoint: $endpoint" \
                >&2
            return 1
        }
        [[ -n "$host" ]] || {
            echo "Cannot determine host for OpenStack endpoint: $endpoint" \
                >&2
            return 1
        }
        if [[ -n "${source_addresses[$host]:-}" ]]; then
            echo "OpenStack endpoint $endpoint is hosted on fence source" \
                "$source_address" >&2
            return 1
        fi
        while IFS= read -r address; do
            if [[ -n "${source_addresses[$address]:-}" ]]; then
                echo "OpenStack endpoint $endpoint resolves to fence source" \
                    "$source_address" >&2
                return 1
            fi
        done < <(getent ahostsv4 "$host" 2>/dev/null |
            awk '{print $1}' | sort -u)
    done
}

source_fenced() {
    [[ "$("$FENCE_PROVIDER" status "$SOURCE_FENCE_ID")" == off ]]
}

watcher_count() {
    local image_id output
    image_id=$(remote "$CONTROLLER_SSH" \
        "rados --id cinder -p '$CINDER_RBD_POOL' get \
         'rbd_id.$root_image' - 2>/dev/null | tail -c +5")
    [[ "$image_id" =~ ^[0-9a-f]+$ ]] || return 1
    output=$(remote "$CONTROLLER_SSH" \
        "rados --id cinder -p '$CINDER_RBD_POOL' listwatchers \
         'rbd_header.$image_id'") || return 1
    sed '/^[[:space:]]*$/d' <<<"$output" | wc -l
}

watchers_are() {
    [[ "$(watcher_count)" == "$1" ]]
}

mapping_count() {
    local target=$1
    remote "$target" \
        "rbd device list --format json --id cinder 2>/dev/null || echo '[]'" |
        jq --arg image "$root_image" \
            '[.[] | select(.name == $image)] | length'
}

mappings_are() {
    local target=$1 expected=$2
    [[ "$(mapping_count "$target")" == "$expected" ]]
}

attachment_is_unique() {
    local attachments total matching
    attachments=$(openstack volume attachment list -f json)
    total=$(jq -r --arg volume "$root_volume" \
        '[.[] | select(."Volume ID" == $volume)] | length' \
        <<<"$attachments")
    matching=$(jq -r \
        --arg volume "$root_volume" --arg server "$SERVER_ID" \
        '[.[] | select(."Volume ID" == $volume and
            ."Server ID" == $server and .Status == "attached")] | length' \
        <<<"$attachments")
    [[ "$total" == 1 && "$matching" == 1 ]]
}

ovs_port_count() {
    local target=$1 port_id=$2
    remote "$target" \
        "ovs-vsctl --data=bare --no-heading --columns=name \
         find Interface external_ids:iface-id='$port_id'" |
        sed '/^[[:space:]]*$/d' |
        wc -l
}

network_owner_is_destination() {
    local port_id
    for port_id in "${server_ports[@]}"; do
        [[ "$(ovs_port_count "$SOURCE_SSH" "$port_id")" == 0 ]] || return 1
        [[ "$(ovs_port_count "$DEST_SSH" "$port_id")" == 1 ]] || return 1
        [[ "$(openstack port show "$port_id" -f value \
            -c binding_host_id)" == "$DEST_HOST" ]] || return 1
    done
}

bfv_evacuation_enabled() {
    local target=$1
    [[ "$(remote "$target" \
        "crudini --get /etc/nova/nova-cpu.conf incus \
         allow_bfv_evacuate 2>/dev/null || echo false" |
        tr '[:upper:]' '[:lower:]')" == true ]]
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
mapfile -t server_ports < <(
    openstack port list --server "$SERVER_ID" -f value -c ID
)
(( ${#server_ports[@]} > 0 )) || {
    echo "Server does not have a Neutron port" >&2
    exit 1
}

[[ "$CONTROLLER_SSH" != "$SOURCE_SSH" &&
   "$CONTROLLER_SSH" != "$DEST_SSH" ]] || {
    echo "CONTROLLER_SSH must be independent of source and destination" >&2
    exit 1
}
control_plane_is_independent || {
    echo "OpenStack control plane is not independent of the fence source" >&2
    exit 1
}
for target in "$SOURCE_SSH" "$DEST_SSH"; do
    bfv_evacuation_enabled "$target" || {
        echo "BFV evacuation is disabled on $target; refusing to fence" >&2
        exit 1
    }
done

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
wait_for "returning source Incus daemon" source_incus_ready
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
    bash "$RETURN_AUDIT"

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
wait_for "single Cinder root attachment after reconciliation" \
    attachment_is_unique
wait_for "source RBD mapping cleanup" mappings_are "$SOURCE_SSH" 0
wait_for "destination RBD mapping ownership" \
    mappings_are "$DEST_SSH" \
    "$([[ "$original_status" == ACTIVE ]] && echo 1 || echo 0)"
wait_for "unique destination Neutron/OVS ownership" \
    network_owner_is_destination

echo "PASS fenced BFV evacuation and returning-host reconciliation"
