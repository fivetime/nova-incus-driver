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
IDMAP_FENCE_PROVIDER=${IDMAP_FENCE_PROVIDER:-$FENCE_PROVIDER}
SOURCE_FENCE_ID=${SOURCE_FENCE_ID:-$SOURCE_HOST}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
RETURN_AUDIT=${RETURN_AUDIT:-"$SCRIPT_DIR/openstack-incus-returning-host-audit.sh"}
CINDER_RBD_POOL=${CINDER_RBD_POOL:-cinder-volumes-rbd-pool}
CINDER_RBD_CLIENT=${CINDER_RBD_CLIENT:-cinder}
INCUS_PROJECT=${INCUS_PROJECT:-nova}
INCUS_RUNTIME_MODE=${INCUS_RUNTIME_MODE:-podman}
INCUS_RUNTIME_CONTAINER=${INCUS_RUNTIME_CONTAINER:-incus}
INCUS_KUBE_NAMESPACE=${INCUS_KUBE_NAMESPACE:-openstack}
INCUS_KUBE_NODE_MAP=${INCUS_KUBE_NODE_MAP:-}
INCUS_KUBE_ADMISSION_LABEL_KEY=${INCUS_KUBE_ADMISSION_LABEL_KEY:-openstack-incus-admitted}
INCUS_KUBE_ADMISSION_LABEL_VALUE=${INCUS_KUBE_ADMISSION_LABEL_VALUE:-enabled}
TIMEOUT=${TIMEOUT:-}

if [[ "$INCUS_RUNTIME_MODE" == kubernetes && -z "$INCUS_KUBE_NODE_MAP" ]]; then
    echo "Set INCUS_KUBE_NODE_MAP to SSH-target=Kubernetes-node mappings" >&2
    exit 2
fi
if [[ "$INCUS_RUNTIME_MODE" == kubernetes ]] &&
   { [[ ! "$INCUS_KUBE_ADMISSION_LABEL_KEY" =~ ^[A-Za-z0-9./_-]+$ ]] ||
     [[ ! "$INCUS_KUBE_ADMISSION_LABEL_VALUE" =~ ^[A-Za-z0-9._-]+$ ]]; }; then
    echo "Invalid Kubernetes Incus admission label" >&2
    exit 2
fi

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)
marker="openstack-incus-evacuation-$(date +%s)-$$"
marker_sha=$(printf '%s\n' "$marker" | sha256sum | awk '{print $1}')

remote() {
    local target=$1
    shift
    "${SSH[@]}" -n "$target" "$@"
}

remote_stdin() {
    local target=$1
    shift
    "${SSH[@]}" "$target" "$@"
}

kube_node_for_target() {
    local target=$1 entry
    for entry in ${INCUS_KUBE_NODE_MAP//,/ }; do
        if [[ "${entry%%=*}" == "$target" ]]; then
            printf '%s\n' "${entry#*=}"
            return 0
        fi
    done
    return 1
}

incus_runtime_remote() {
    local host=$1 command_line namespace kube_node
    shift
    printf -v command_line '%q ' "$@"

    case "$INCUS_RUNTIME_MODE" in
        podman)
            remote "$host" \
                "podman exec $(printf '%q' "$INCUS_RUNTIME_CONTAINER") $command_line"
            ;;
        kubernetes)
            kube_node=$(kube_node_for_target "$host") || return 1
            printf -v namespace '%q' "$INCUS_KUBE_NAMESPACE"
            printf -v kube_node '%q' "$kube_node"
            remote "$host" \
                "set -e; pods=\$(kubectl -n $namespace get pod -l application=incus --field-selector \"spec.nodeName=$kube_node\" --no-headers -o custom-columns=NAME:.metadata.name); set -- \$pods; [ \$# -eq 1 ]; kubectl -n $namespace exec \"\$1\" -- $command_line"
            ;;
        *)
            echo "INCUS_RUNTIME_MODE must be podman or kubernetes" >&2
            return 2
            ;;
    esac
}

incus_runtime_remote_stdin() {
    local host=$1 command_line namespace kube_node
    shift
    printf -v command_line '%q ' "$@"

    case "$INCUS_RUNTIME_MODE" in
        podman)
            remote_stdin "$host" \
                "podman exec -i $(printf '%q' "$INCUS_RUNTIME_CONTAINER") $command_line"
            ;;
        kubernetes)
            kube_node=$(kube_node_for_target "$host") || return 1
            printf -v namespace '%q' "$INCUS_KUBE_NAMESPACE"
            printf -v kube_node '%q' "$kube_node"
            remote_stdin "$host" \
                "set -e; pods=\$(kubectl -n $namespace get pod -l application=incus --field-selector \"spec.nodeName=$kube_node\" --no-headers -o custom-columns=NAME:.metadata.name); set -- \$pods; [ \$# -eq 1 ]; kubectl -n $namespace exec -i \"\$1\" -- $command_line"
            ;;
        *)
            echo "INCUS_RUNTIME_MODE must be podman or kubernetes" >&2
            return 2
            ;;
    esac
}

compute_runtime_remote() {
    local host=$1 command_line namespace kube_node
    shift
    printf -v command_line '%q ' "$@"

    case "$INCUS_RUNTIME_MODE" in
        podman)
            remote "$host" "$command_line"
            ;;
        kubernetes)
            kube_node=$(kube_node_for_target "$host") || return 1
            printf -v namespace '%q' "$INCUS_KUBE_NAMESPACE"
            printf -v kube_node '%q' "$kube_node"
            remote "$host" \
                "set -e; pods=\$(kubectl -n $namespace get pod -l application=nova,component=compute-incus --field-selector \"spec.nodeName=$kube_node\" --no-headers -o custom-columns=NAME:.metadata.name); set -- \$pods; [ \$# -eq 1 ]; kubectl -n $namespace exec \"\$1\" -- $command_line"
            ;;
        *)
            echo "INCUS_RUNTIME_MODE must be podman or kubernetes" >&2
            return 2
            ;;
    esac
}

kube_compute_daemonset_is_guarded() {
    local namespace key value
    printf -v namespace '%q' "$INCUS_KUBE_NAMESPACE"
    printf -v key '%q' "$INCUS_KUBE_ADMISSION_LABEL_KEY"
    printf -v value '%q' "$INCUS_KUBE_ADMISSION_LABEL_VALUE"
    remote "$CONTROLLER_SSH" \
        "kubectl -n $namespace get daemonset nova-compute-incus -o json | jq -e --arg key $key --arg value $value '.spec.template.spec.nodeSelector[\$key] == \$value' >/dev/null"
}

kube_source_node_label_is() {
    local expected=$1 node key
    node=$(kube_node_for_target "$SOURCE_SSH") || return 1
    printf -v node '%q' "$node"
    printf -v key '%q' "$INCUS_KUBE_ADMISSION_LABEL_KEY"
    [[ "$(remote "$CONTROLLER_SSH" \
        "kubectl get node $node -o json | jq -r --arg key $key '.metadata.labels[\$key] // empty'")" == "$expected" ]]
}

kube_quarantine_source_compute() {
    local node key
    node=$(kube_node_for_target "$SOURCE_SSH") || return 1
    printf -v node '%q' "$node"
    printf -v key '%q' "$INCUS_KUBE_ADMISSION_LABEL_KEY"
    remote "$CONTROLLER_SSH" \
        "kubectl label node $node \"$key-\" --overwrite >/dev/null"
}

kube_admit_source_compute() {
    local node key value
    node=$(kube_node_for_target "$SOURCE_SSH") || return 1
    printf -v node '%q' "$node"
    printf -v key '%q' "$INCUS_KUBE_ADMISSION_LABEL_KEY"
    printf -v value '%q' "$INCUS_KUBE_ADMISSION_LABEL_VALUE"
    remote "$CONTROLLER_SSH" \
        "kubectl label node $node \"$key=$value\" --overwrite >/dev/null"
}

kube_source_compute_absent() {
    local node namespace count
    node=$(kube_node_for_target "$SOURCE_SSH") || return 1
    printf -v node '%q' "$node"
    printf -v namespace '%q' "$INCUS_KUBE_NAMESPACE"
    count=$(remote "$CONTROLLER_SSH" \
        "kubectl -n $namespace get pod -l application=nova,component=compute-incus --field-selector \"spec.nodeName=$node\" -o json | jq '[.items[] | select(.metadata.deletionTimestamp == null)] | length'")
    [[ "$count" == 0 ]]
}

kube_source_compute_ready() {
    local node namespace count
    node=$(kube_node_for_target "$SOURCE_SSH") || return 1
    printf -v node '%q' "$node"
    printf -v namespace '%q' "$INCUS_KUBE_NAMESPACE"
    count=$(remote "$CONTROLLER_SSH" \
        "kubectl -n $namespace get pod -l application=nova,component=compute-incus --field-selector \"spec.nodeName=$node\" -o json | jq '[.items[] | select(.metadata.deletionTimestamp == null) | select(any(.status.conditions[]?; .type == \"Ready\" and .status == \"True\"))] | length'")
    [[ "$count" == 1 ]]
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

source_service_up() {
    [[ "$(openstack compute service list --service nova-compute \
        --host "$SOURCE_HOST" -f value -c State)" == up ]]
}

source_placement_enabled() {
    local provider_uuid traits
    provider_uuid=$(openstack resource provider list --name "$SOURCE_HOST" \
        -f value -c uuid)
    [[ "$provider_uuid" =~ ^[0-9a-f-]{36}$ ]] || return 1
    traits=$(openstack resource provider trait list "$provider_uuid" \
        -f value -c name)
    ! grep -Fxq COMPUTE_STATUS_DISABLED <<<"$traits"
}

source_reachable() {
    remote "$SOURCE_SSH" true >/dev/null 2>&1
}

source_incus_ready() {
    incus_runtime_remote "$SOURCE_SSH" incus query /1.0 >/dev/null 2>&1
}

source_instance_absent() {
    ! incus_runtime_remote "$SOURCE_SSH" incus --project "$INCUS_PROJECT" \
        info "$instance_name" >/dev/null 2>&1
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
    image_id=$(compute_runtime_remote "$DEST_SSH" rados \
        --id "$CINDER_RBD_CLIENT" -p "$CINDER_RBD_POOL" get \
        "rbd_id.$root_image" - 2>/dev/null | tail -c +5)
    [[ "$image_id" =~ ^[0-9a-f]+$ ]] || return 1
    output=$(compute_runtime_remote "$DEST_SSH" rados \
        --id "$CINDER_RBD_CLIENT" -p "$CINDER_RBD_POOL" listwatchers \
        "rbd_header.$image_id") || return 1
    sed '/^[[:space:]]*$/d' <<<"$output" | wc -l
}

watchers_are() {
    [[ "$(watcher_count)" == "$1" ]]
}

mapping_count() {
    local target=$1
    remote "$target" \
        "rbd device list --format json 2>/dev/null || echo '[]'" |
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
    if [[ "$INCUS_RUNTIME_MODE" == kubernetes ]]; then
        compute_runtime_remote "$target" grep -Eiq \
            '^[[:space:]]*allow_bfv_evacuate[[:space:]]*=[[:space:]]*true[[:space:]]*$' \
            /etc/nova/nova-incus.conf
    else
        [[ "$(remote "$target" \
            "crudini --get /etc/nova/nova-cpu.conf incus \
             allow_bfv_evacuate 2>/dev/null || echo false" |
            tr '[:upper:]' '[:lower:]')" == true ]]
    fi
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
if [[ "$INCUS_RUNTIME_MODE" == kubernetes ]]; then
    kube_compute_daemonset_is_guarded || {
        echo "nova-compute-incus DaemonSet is not guarded by the configured admission label" >&2
        exit 1
    }
    kube_source_node_label_is "$INCUS_KUBE_ADMISSION_LABEL_VALUE" || {
        echo "Source Kubernetes node is not admitted before evacuation" >&2
        exit 1
    }
fi

if [[ "$original_status" == ACTIVE ]]; then
    printf '%s\n' "$marker" |
        incus_runtime_remote_stdin "$SOURCE_SSH" incus \
            --project "$INCUS_PROJECT" file push - \
            "$instance_name/root/stonith-e2e-marker"
fi

"$FENCE_PROVIDER" off "$SOURCE_FENCE_ID"
fenced_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
wait_for "source power fencing" source_fenced
openstack compute service set --disable "$SOURCE_HOST" nova-compute
if [[ "$INCUS_RUNTIME_MODE" == kubernetes ]]; then
    kube_quarantine_source_compute
fi
wait_for "Nova source service down" source_service_down

wait_for "fenced source BFV watcher retirement" watchers_are 0

# The dead source's committed claim structurally blocks every rescheduled
# spawn of this allocation generation; only recorded fence evidence may
# dispose of it. Without this step the evacuation below fails its idmap
# pre-check by design.
IDMAP_REGISTRY_TOOL=${IDMAP_REGISTRY_TOOL:?Set IDMAP_REGISTRY_TOOL to the openstack-incus-idmap-registry.py invocation prefix (interpreter, script and connection arguments)}
FENCE_OPERATOR=${FENCE_OPERATOR:?Set FENCE_OPERATOR to the operator identity recorded in the fence ledger}
source_host_id=$(openstack resource provider list --name "$SOURCE_HOST"     -f value -c uuid)
[[ "$source_host_id" =~ ^[0-9a-f-]{36}$ ]] || {
    echo "Cannot resolve the source compute node UUID" >&2
    exit 1
}
remote "$CONTROLLER_SSH" "$IDMAP_REGISTRY_TOOL \
    --fence-retire-host-claim '$SERVER_ID' \
    --host-id '$source_host_id' \
    --fence-plug '$SOURCE_FENCE_ID' \
    --fence-provider '$IDMAP_FENCE_PROVIDER' \
    --fence-agent '$FENCE_PROVIDER' --fenced-at '$fenced_at' \
    --operator '$FENCE_OPERATOR' \
    --fence-evidence 'BFV root watcher count 0'" >/dev/null || {
    echo "Fence-based claim retirement failed" >&2
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
    recovered_sha=$(incus_runtime_remote "$DEST_SSH" incus \
        --project "$INCUS_PROJECT" file pull \
        "$instance_name/root/stonith-e2e-marker" - |
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
if [[ "$INCUS_RUNTIME_MODE" == kubernetes ]]; then
    wait_for "returning source compute quarantine" kube_source_compute_absent
    ! kube_source_node_label_is "$INCUS_KUBE_ADMISSION_LABEL_VALUE"
else
    remote "$SOURCE_SSH" \
        "test ! -e /run/openstack-incus/compute-admitted"
    remote "$SOURCE_SSH" \
        "! systemctl is-active --quiet devstack@n-cpu.service"
fi
if incus_runtime_remote "$SOURCE_SSH" incus --project "$INCUS_PROJECT" \
        list --format json |
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
CINDER_RBD_CLIENT="$CINDER_RBD_CLIENT" \
CEPH_QUERY_SSH="$DEST_SSH" \
INCUS_PROJECT="$INCUS_PROJECT" \
INCUS_RUNTIME_MODE="$INCUS_RUNTIME_MODE" \
INCUS_RUNTIME_CONTAINER="$INCUS_RUNTIME_CONTAINER" \
INCUS_KUBE_NAMESPACE="$INCUS_KUBE_NAMESPACE" \
INCUS_KUBE_NODE_MAP="$INCUS_KUBE_NODE_MAP" \
INCUS_KUBE_ADMISSION_LABEL_KEY="$INCUS_KUBE_ADMISSION_LABEL_KEY" \
INCUS_KUBE_ADMISSION_LABEL_VALUE="$INCUS_KUBE_ADMISSION_LABEL_VALUE" \
    bash "$RETURN_AUDIT"

if [[ "$INCUS_RUNTIME_MODE" == kubernetes ]]; then
    kube_admit_source_compute
    wait_for "returning source compute pod readiness" kube_source_compute_ready
else
    remote "$SOURCE_SSH" \
        "/usr/local/sbin/openstack-incus-compute-admission admit \
         --reason evacuation-reconciliation-passed; \
         systemctl reset-failed devstack@n-cpu.service; \
         systemctl start devstack@n-cpu.service"
fi

wait_for "returning Nova compute heartbeat" source_service_up
wait_for "stale source record cleanup" source_instance_absent
openstack compute service set --enable "$SOURCE_HOST" nova-compute
wait_for "returning source Placement eligibility" source_placement_enabled
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
