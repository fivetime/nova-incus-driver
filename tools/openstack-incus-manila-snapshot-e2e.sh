#!/usr/bin/env bash
# Validate Manila snapshot and restore through Nova share attachments.

set -Eeuo pipefail

RUN_DESTRUCTIVE=${RUN_DESTRUCTIVE:-false}
IMAGE=${IMAGE:?Set IMAGE}
FLAVOR=${FLAVOR:?Set FLAVOR}
NETWORK=${NETWORK:?Set NETWORK}
COMPUTE_HOST=${COMPUTE_HOST:?Set COMPUTE_HOST}
COMPUTE_SSH=${COMPUTE_SSH:?Set COMPUTE_SSH}
CONTROLLER_SSH=${CONTROLLER_SSH:-}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
SSH_KNOWN_HOSTS_FILE=${SSH_KNOWN_HOSTS_FILE:-$HOME/.ssh/known_hosts}
SHARE_TYPE=${SHARE_TYPE:-incus-nfs}
SHARE_PROTOCOL=${SHARE_PROTOCOL:-NFS}
SHARE_SIZE=${SHARE_SIZE:-1}
SHARE_ROOT_MODE=${SHARE_ROOT_MODE:-}
NAME=${NAME:-incus-manila-snapshot-e2e-$RANDOM}
TIMEOUT=${TIMEOUT:-600}
INCUS_PROJECT=${INCUS_PROJECT:-nova}
INCUS_RUNTIME_MODE=${INCUS_RUNTIME_MODE:-podman}
INCUS_RUNTIME_CONTAINER=${INCUS_RUNTIME_CONTAINER:-incus}
INCUS_KUBE_NAMESPACE=${INCUS_KUBE_NAMESPACE:-openstack}
INCUS_KUBE_NODE_MAP=${INCUS_KUBE_NODE_MAP:-}
NOVA_INSTANCES_PATH_KUBERNETES=${NOVA_INSTANCES_PATH_KUBERNETES:-/var/lib/nova-incus/instances}
NOVA_INSTANCES_PATH_PODMAN=${NOVA_INSTANCES_PATH_PODMAN:-/opt/stack/data/nova/instances}

[[ "$RUN_DESTRUCTIVE" == true ]] || {
    echo "Set RUN_DESTRUCTIVE=true to run this destructive case" >&2
    exit 2
}
if [[ -n "$SHARE_ROOT_MODE" && ! "$SHARE_ROOT_MODE" =~ ^0?[0-7]{3}$ ]]; then
    echo "SHARE_ROOT_MODE must be an octal mode such as 0777" >&2
    exit 2
fi
if [[ "$INCUS_RUNTIME_MODE" == kubernetes && -z "$INCUS_KUBE_NODE_MAP" ]]; then
    echo "Set INCUS_KUBE_NODE_MAP for Kubernetes mode" >&2
    exit 2
fi

[[ -f "$SSH_IDENTITY" && -r "$SSH_IDENTITY" ]] || {
    echo "SSH identity is not a readable regular file: $SSH_IDENTITY" >&2
    exit 2
}
[[ -f "$SSH_KNOWN_HOSTS_FILE" && -r "$SSH_KNOWN_HOSTS_FILE" ]] || {
    echo "SSH known_hosts is not a readable regular file: $SSH_KNOWN_HOSTS_FILE" >&2
    exit 2
}
SSH=(
    ssh
    -i "$SSH_IDENTITY"
    -o BatchMode=yes
    -o StrictHostKeyChecking=yes
    -o "UserKnownHostsFile=$SSH_KNOWN_HOSTS_FILE"
)
server_id=
source_share=
restored_share=
snapshot_id=
shares_url=
token=

remote() { local host=$1; shift; "${SSH[@]}" "$host" "$@"; }

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

incus_remote() {
    local command_line node
    printf -v command_line '%q ' incus --project "$INCUS_PROJECT" "$@"
    case "$INCUS_RUNTIME_MODE" in
        podman)
            remote "$COMPUTE_SSH" \
                "podman exec $(printf '%q' "$INCUS_RUNTIME_CONTAINER") $command_line"
            ;;
        kubernetes)
            node=$(kube_node_for_target "$COMPUTE_SSH") || return 1
            remote "$COMPUTE_SSH" "set -e; pods=\$(kubectl -n $(printf '%q' "$INCUS_KUBE_NAMESPACE") get pod -l application=incus --field-selector spec.nodeName=$(printf '%q' "$node") --no-headers -o custom-columns=NAME:.metadata.name); set -- \$pods; [ \$# -eq 1 ]; kubectl -n $(printf '%q' "$INCUS_KUBE_NAMESPACE") exec \"\$1\" -- $command_line"
            ;;
        *)
            echo "INCUS_RUNTIME_MODE must be podman or kubernetes" >&2
            return 2
            ;;
    esac
}

compute_mount_namespace_remote() {
    local command_line
    printf -v command_line '%q ' "$@"
    if [[ "$INCUS_RUNTIME_MODE" == kubernetes ]]; then
        remote "$COMPUTE_SSH" \
            "set -e; id=\$(crictl ps --name nova-compute -q); set -- \$id; [ \$# -eq 1 ]; pid=\$(crictl inspect \"\$1\" | jq -er .info.pid); nsenter --target \"\$pid\" --mount --pid -- $command_line"
    else
        remote "$COMPUTE_SSH" "$command_line"
    fi
}

share_staging_root() {
    if [[ "$INCUS_RUNTIME_MODE" == kubernetes ]]; then
        printf '%s/incus-shares\n' "$NOVA_INSTANCES_PATH_KUBERNETES"
    else
        printf '%s/incus-shares\n' "$NOVA_INSTANCES_PATH_PODMAN"
    fi
}

if [[ -n "$CONTROLLER_SSH" ]]; then
    openstack() {
        local command_line
        printf -v command_line '%q ' "$@"
        "${SSH[@]}" "$CONTROLLER_SSH" \
            "source $CONTROLLER_OPENRC >/dev/null 2>&1; openstack $command_line"
    }
fi

wait_value() {
    local command=$1 expected=$2 deadline=$((SECONDS + TIMEOUT)) current
    while ((SECONDS < deadline)); do
        current=$(eval "$command" 2>/dev/null || true)
        [[ "$current" == "$expected" ]] && return 0
        [[ "$current" == error* || "$current" == ERROR ]] && break
        sleep 2
    done
    echo "Timed out waiting for $expected (current: ${current:-missing})" >&2
    return 1
}

api() {
    local method=$1 url=$2 data=${3:-}
    local args=(-fsS -X "$method" -H "X-Auth-Token: $token"
        -H "OpenStack-API-Version: compute 2.97")
    if [[ -n "$data" ]]; then
        args+=(-H "Content-Type: application/json" -d "$data")
    fi
    if [[ -n "$CONTROLLER_SSH" ]]; then
        local command_line
        printf -v command_line '%q ' curl "${args[@]}" "$url"
        "${SSH[@]}" "$CONTROLLER_SSH" "$command_line"
    else
        curl "${args[@]}" "$url"
    fi
}

wait_mapping() {
    local share=$1 expected=$2 body deadline=$((SECONDS + TIMEOUT))
    while ((SECONDS < deadline)); do
        body=$(api GET "$shares_url")
        if python3 -c '
import json
import sys

body = json.load(sys.stdin)
share_id, expected = sys.argv[1:]
found = [item for item in body["shares"] if item["share_id"] == share_id]
if expected == "absent":
    raise SystemExit(0 if not found else 1)
raise SystemExit(0 if found and found[0]["status"] == expected else 1)
' "$share" "$expected" <<<"$body"; then
            return 0
        fi
        sleep 2
    done
    echo "Share mapping $share did not reach $expected: $body" >&2
    return 1
}

attach_share() {
    local share=$1 tag=$2
    api POST "$shares_url" \
        "{\"share\":{\"share_id\":\"$share\",\"tag\":\"$tag\"}}" \
        >/dev/null
    wait_mapping "$share" inactive
}

detach_share() {
    local share=$1
    api DELETE "$shares_url/$share" >/dev/null
    wait_mapping "$share" absent
}

cleanup() {
    if [[ -n "$server_id" ]]; then
        openstack server stop "$server_id" >/dev/null 2>&1 || true
        wait_value "openstack server show '$server_id' -f value -c status" \
            SHUTOFF >/dev/null 2>&1 || true
        if [[ -n "$source_share" && -n "$shares_url" ]]; then
            api DELETE "$shares_url/$source_share" >/dev/null 2>&1 || true
        fi
        if [[ -n "$restored_share" && -n "$shares_url" ]]; then
            api DELETE "$shares_url/$restored_share" >/dev/null 2>&1 || true
        fi
        openstack server delete --wait "$server_id" >/dev/null 2>&1 || true
    fi
    if [[ -n "$restored_share" ]]; then
        openstack share delete --wait "$restored_share" >/dev/null 2>&1 || true
    fi
    if [[ -n "$snapshot_id" ]]; then
        openstack share snapshot delete --wait "$snapshot_id" \
            >/dev/null 2>&1 || true
    fi
    if [[ -n "$source_share" ]]; then
        openstack share delete --wait "$source_share" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

snapshot_support=$(openstack share type show "$SHARE_TYPE" -f json | \
    python3 -c '
import ast
import json
import sys

data = {
    str(key).strip().lower().replace(" ", "_"): value
    for key, value in json.load(sys.stdin).items()
}
specs = data.get("optional_extra_specs") or {}
if isinstance(specs, str):
    try:
        specs = ast.literal_eval(specs)
    except (SyntaxError, ValueError):
        specs = {}
if not isinstance(specs, dict):
    specs = {}
required = ("snapshot_support", "create_share_from_snapshot_support")
values = [
    str(specs.get(key, "False")).replace("<is>", "").strip().lower()
    for key in required
]
print("true" if all(value == "true" for value in values) else "false")
')
[[ "$snapshot_support" == true ]] || {
    echo "Share type $SHARE_TYPE must support snapshots and create-from-snapshot" >&2
    exit 2
}

server_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --image "$IMAGE" --network "$NETWORK" \
    --host "$COMPUTE_HOST" --wait "$NAME-server" -f value -c id)
openstack server stop "$server_id"
wait_value "openstack server show '$server_id' -f value -c status" SHUTOFF
instance_name=$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)

source_share=$(openstack share create "$SHARE_PROTOCOL" "$SHARE_SIZE" \
    --share-type "$SHARE_TYPE" --name "$NAME-source" --wait \
    -f value -c id)

token=$(openstack token issue -f value -c id)
endpoint=$(openstack endpoint list --service nova --interface public \
    -f value -c URL | head -1)
project_id=$(openstack server show "$server_id" -f value -c project_id)
endpoint=${endpoint//\%\(project_id\)s/$project_id}
shares_url="$endpoint/servers/$server_id/shares"

attach_share "$source_share" source
openstack server start "$server_id"
wait_value "openstack server show '$server_id' -f value -c status" ACTIVE
wait_mapping "$source_share" active
if [[ -n "$SHARE_ROOT_MODE" ]]; then
    source_staging="$(share_staging_root)/$server_id/$source_share"
    compute_mount_namespace_remote chmod "$SHARE_ROOT_MODE" "$source_staging"
fi
marker="manila-snapshot-$source_share"
marker_path="/mnt/manila/source/backup-marker-$server_id"
deadline=$((SECONDS + TIMEOUT))
until incus_remote exec "$instance_name" -- sh -c \
        "printf %s '$marker' > '$marker_path'; sync" >/dev/null 2>&1; do
    ((SECONDS < deadline)) || {
        echo "Source share did not become writable" >&2
        exit 1
    }
    sleep 2
done
openstack server stop "$server_id"
wait_value "openstack server show '$server_id' -f value -c status" SHUTOFF
detach_share "$source_share"

snapshot_id=$(openstack share snapshot create --name "$NAME-snapshot" \
    --wait "$source_share" -f value -c id)
restored_share=$(openstack share create "$SHARE_PROTOCOL" "$SHARE_SIZE" \
    --snapshot-id "$snapshot_id" --share-type "$SHARE_TYPE" \
    --name "$NAME-restored" --wait -f value -c id)

attach_share "$restored_share" restored
openstack server start "$server_id"
wait_value "openstack server show '$server_id' -f value -c status" ACTIVE
deadline=$((SECONDS + TIMEOUT))
restored=
until restored=$(incus_remote exec "$instance_name" -- \
        cat "/mnt/manila/restored/backup-marker-$server_id" 2>/dev/null); do
    ((SECONDS < deadline)) || {
        echo "Restored share marker did not appear" >&2
        exit 1
    }
    sleep 2
done
[[ "$restored" == "$marker" ]]
openstack server stop "$server_id"
wait_value "openstack server show '$server_id' -f value -c status" SHUTOFF
detach_share "$restored_share"

openstack server delete --wait "$server_id"
server_id=
openstack share delete --wait "$restored_share"
restored_share=
openstack share snapshot delete --wait "$snapshot_id"
snapshot_id=
openstack share delete --wait "$source_share"
source_share=

trap - EXIT INT TERM
echo "PASS Manila snapshot, restored share, and tenant marker=$marker"
