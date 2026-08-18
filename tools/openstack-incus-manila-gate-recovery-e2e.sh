#!/usr/bin/env bash
# Manila destination pre-mount gating and nova-compute restart recovery.
#
# The script owns the Nova server and share mapping, but consumes an existing
# available Manila share. It supports both Podman and Kubernetes Incus
# runtimes. In Kubernetes mode the mount failure is injected only into the
# destination nova-compute Pod mount namespace.

set -Eeuo pipefail

RUN_DESTRUCTIVE=${RUN_DESTRUCTIVE:-false}
IMAGE=${IMAGE:?Set IMAGE to an Incus guest image}
FLAVOR=${FLAVOR:?Set FLAVOR}
NETWORK=${NETWORK:?Set NETWORK}
SHARE=${SHARE:?Set SHARE to an available Manila share}
SHARE_ROOT_MODE=${SHARE_ROOT_MODE:-}
SOURCE_HOST=${SOURCE_HOST:?Set SOURCE_HOST to the Nova source hostname}
DEST_HOST=${DEST_HOST:?Set DEST_HOST to the Nova destination hostname}
SOURCE_SSH=${SOURCE_SSH:?Set SOURCE_SSH to the source compute SSH target}
DEST_SSH=${DEST_SSH:?Set DEST_SSH to the destination compute SSH target}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
SSH_KNOWN_HOSTS_FILE=${SSH_KNOWN_HOSTS_FILE:-$HOME/.ssh/known_hosts}
INCUS_PROJECT=${INCUS_PROJECT:-nova}
INCUS_RUNTIME_MODE=${INCUS_RUNTIME_MODE:-podman}
INCUS_RUNTIME_CONTAINER=${INCUS_RUNTIME_CONTAINER:-incus}
INCUS_KUBE_NAMESPACE=${INCUS_KUBE_NAMESPACE:-openstack}
INCUS_KUBE_NODE_MAP=${INCUS_KUBE_NODE_MAP:-}
NOVA_INSTANCES_PATH_PODMAN=${NOVA_INSTANCES_PATH_PODMAN:-/opt/stack/data/nova/instances}
NOVA_INSTANCES_PATH_KUBERNETES=${NOVA_INSTANCES_PATH_KUBERNETES:-/var/lib/nova-incus/instances}
TAG=${TAG:-tenant-data}
NAME=${NAME:-incus-manila-gate-$RANDOM}
TIMEOUT=${TIMEOUT:-600}
CASES=${CASES:-gate,retry,recovery}
KEEP_FAILED=${KEEP_FAILED:-false}

[[ "$RUN_DESTRUCTIVE" == true ]] || {
    echo "Set RUN_DESTRUCTIVE=true to run this destructive case" >&2
    exit 2
}
[[ -r "$SSH_IDENTITY" && -r "$SSH_KNOWN_HOSTS_FILE" ]] || {
    echo "SSH identity and known_hosts must be readable" >&2
    exit 2
}
if [[ "$INCUS_RUNTIME_MODE" == kubernetes && -z "$INCUS_KUBE_NODE_MAP" ]]; then
    echo "Set INCUS_KUBE_NODE_MAP for Kubernetes mode" >&2
    exit 2
fi
if [[ -n "$SHARE_ROOT_MODE" && ! "$SHARE_ROOT_MODE" =~ ^0?[0-7]{3}$ ]]; then
    echo "SHARE_ROOT_MODE must be an octal mode such as 0777" >&2
    exit 2
fi

SSH=(ssh -n -i "$SSH_IDENTITY" -o BatchMode=yes
    -o StrictHostKeyChecking=yes
    -o "UserKnownHostsFile=$SSH_KNOWN_HOSTS_FILE")
server_id=
instance_name=
share_id=
shares_url=
token=
mount_failpoint=0
marker_path=

fail() { echo "FAIL $1" >&2; exit 1; }
case_selected() { [[ ",$CASES," == *",$1,"* ]]; }
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

runtime_remote() {
    local kind=$1 host=$2 command_line node label
    shift 2
    printf -v command_line '%q ' "$@"
    case "$INCUS_RUNTIME_MODE:$kind" in
        podman:incus)
            remote "$host" \
                "podman exec $(printf '%q' "$INCUS_RUNTIME_CONTAINER") $command_line"
            ;;
        podman:compute)
            remote "$host" "$command_line"
            ;;
        kubernetes:incus|kubernetes:compute)
            node=$(kube_node_for_target "$host") || return 1
            if [[ "$kind" == incus ]]; then
                label='application=incus'
            else
                label='application=nova,component=compute-incus'
            fi
            remote "$host" "set -e; pods=\$(kubectl -n $(printf '%q' "$INCUS_KUBE_NAMESPACE") get pod -l $(printf '%q' "$label") --field-selector spec.nodeName=$(printf '%q' "$node") --no-headers -o custom-columns=NAME:.metadata.name); set -- \$pods; [ \$# -eq 1 ]; kubectl -n $(printf '%q' "$INCUS_KUBE_NAMESPACE") exec \"\$1\" -- $command_line"
            ;;
        *)
            echo "Unsupported runtime mode/kind: $INCUS_RUNTIME_MODE/$kind" >&2
            return 2
            ;;
    esac
}

incus_remote() { local host=$1; shift; runtime_remote incus "$host" "$@"; }
compute_remote() { local host=$1; shift; runtime_remote compute "$host" "$@"; }

compute_mount_namespace_remote() {
    local host=$1 command_line
    shift
    printf -v command_line '%q ' "$@"
    if [[ "$INCUS_RUNTIME_MODE" == kubernetes ]]; then
        remote "$host" \
            "set -e; id=\$(crictl ps --name nova-compute -q); set -- \$id; [ \$# -eq 1 ]; pid=\$(crictl inspect \"\$1\" | jq -er .info.pid); nsenter --target \"\$pid\" --mount --pid -- $command_line"
    else
        remote "$host" "$command_line"
    fi
}

share_staging_root() {
    if [[ "$INCUS_RUNTIME_MODE" == kubernetes ]]; then
        printf '%s/incus-shares\n' "$NOVA_INSTANCES_PATH_KUBERNETES"
    else
        printf '%s/incus-shares\n' "$NOVA_INSTANCES_PATH_PODMAN"
    fi
}

api() {
    local method=$1 url=$2 data=${3:-}
    local args=(-fsS -X "$method" -H "X-Auth-Token: $token"
        -H "OpenStack-API-Version: compute 2.97")
    [[ -n "$data" ]] && args+=(-H 'Content-Type: application/json' -d "$data")
    curl "${args[@]}" "$url"
}

wait_status() {
    local expected=$1 current= deadline=$((SECONDS + TIMEOUT))
    while ((SECONDS < deadline)); do
        current=$(openstack server show "$server_id" -f value -c status 2>/dev/null || true)
        [[ "$current" == "$expected" ]] && return 0
        sleep 3
    done
    echo "Server did not reach $expected (current=${current:-missing})" >&2
    return 1
}

wait_mapping() {
    local expected=$1 body= deadline=$((SECONDS + TIMEOUT))
    while ((SECONDS < deadline)); do
        body=$(api GET "$shares_url")
        if python3 -c 'import json,sys; d=json.load(sys.stdin); sid,expected=sys.argv[1:]; raise SystemExit(0 if any(x["share_id"] == sid and x["status"] == expected for x in d["shares"]) else 1)' "$share_id" "$expected" <<<"$body"; then
            return 0
        fi
        sleep 3
    done
    echo "Share mapping did not reach $expected: $body" >&2
    return 1
}

server_host() {
    openstack --os-compute-api-version 2.74 server show "$server_id" \
        -f value -c OS-EXT-SRV-ATTR:host
}

guest_pid() {
    local host=$1
    incus_remote "$host" incus --project "$INCUS_PROJECT" info "$instance_name" |
        sed -n 's/^PID: //p'
}

clear_mount_failpoint() {
    ((mount_failpoint)) || return 0
    compute_mount_namespace_remote "$DEST_SSH" sh -c \
        'umount -l /sbin/mount.ceph 2>/dev/null || true'
    mount_failpoint=0
}

restart_compute_pod() {
    local node old_pod= new_pod= deadline
    [[ "$INCUS_RUNTIME_MODE" == kubernetes ]] || {
        remote "$DEST_SSH" systemctl restart nova-compute
        return
    }
    node=$(kube_node_for_target "$DEST_SSH") || return 1
    old_pod=$(remote "$DEST_SSH" "kubectl -n $(printf '%q' "$INCUS_KUBE_NAMESPACE") get pod -l application=nova,component=compute-incus --field-selector spec.nodeName=$(printf '%q' "$node") --no-headers -o custom-columns=NAME:.metadata.name")
    [[ -n "$old_pod" && "$old_pod" != *$'\n'* ]] || return 1
    remote "$DEST_SSH" "kubectl -n $(printf '%q' "$INCUS_KUBE_NAMESPACE") delete pod $(printf '%q' "$old_pod") --wait=false"
    deadline=$((SECONDS + TIMEOUT))
    while ((SECONDS < deadline)); do
        new_pod=$(remote "$DEST_SSH" "kubectl -n $(printf '%q' "$INCUS_KUBE_NAMESPACE") get pod -l application=nova,component=compute-incus --field-selector spec.nodeName=$(printf '%q' "$node") --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null" || true)
        if [[ -n "$new_pod" && "$new_pod" != "$old_pod" ]] && \
                remote "$DEST_SSH" "kubectl -n $(printf '%q' "$INCUS_KUBE_NAMESPACE") wait --for=condition=Ready pod/$(printf '%q' "$new_pod") --timeout=10s" >/dev/null 2>&1; then
            return 0
        fi
        sleep 5
    done
    return 1
}

cleanup() {
    local status=$?
    set +e
    clear_mount_failpoint
    if ((status != 0)) && [[ "$KEEP_FAILED" == true ]]; then
        echo "KEEP_FAILED preserved server=$server_id share=$share_id" >&2
        return
    fi
    if [[ -n "$server_id" ]] && openstack server show "$server_id" >/dev/null 2>&1; then
        if [[ -n "$marker_path" && -n "$instance_name" ]]; then
            if [[ "$(server_host 2>/dev/null)" == "$DEST_HOST" ]]; then
                marker_host=$DEST_SSH
            else
                marker_host=$SOURCE_SSH
            fi
            incus_remote "$marker_host" incus --project "$INCUS_PROJECT" \
                exec "$instance_name" -- rm -f "$marker_path" \
                >/dev/null 2>&1
        fi
        openstack server stop "$server_id" >/dev/null 2>&1
        wait_status SHUTOFF >/dev/null 2>&1
        [[ -n "$shares_url" && -n "$share_id" ]] && \
            api DELETE "$shares_url/$share_id" >/dev/null 2>&1
        openstack server delete --wait "$server_id" >/dev/null 2>&1
    fi
    exit "$status"
}
trap cleanup EXIT INT TERM

share_id=$(openstack share show "$SHARE" -f value -c id)
[[ "$(openstack share show "$share_id" -f value -c status)" == available ]] ||
    fail "share is not available"

server_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --image "$IMAGE" --network "$NETWORK" \
    --host "$SOURCE_HOST" "$NAME-server" -f value -c id)
wait_status ACTIVE
instance_name=$(openstack --os-compute-api-version 2.74 server show \
    "$server_id" -f value -c OS-EXT-SRV-ATTR:instance_name)
marker_path="/mnt/manila/$TAG/marker-$server_id"

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
wait_mapping inactive
openstack server start "$server_id"
wait_status ACTIVE
wait_mapping active
staging="$(share_staging_root)/$server_id/$share_id"
if [[ -n "$SHARE_ROOT_MODE" ]]; then
    compute_mount_namespace_remote "$SOURCE_SSH" \
        chmod "$SHARE_ROOT_MODE" "$staging"
fi
deadline=$((SECONDS + TIMEOUT))
until incus_remote "$SOURCE_SSH" incus --project "$INCUS_PROJECT" exec \
        "$instance_name" -- sh -c \
        "echo '$NAME' > '$marker_path' && sync" \
        >/dev/null 2>&1; do
    if ((SECONDS >= deadline)); then
    compute_remote "$SOURCE_SSH" stat -c 'host-share=%a:%u:%g' "$staging" || true
    incus_remote "$SOURCE_SSH" incus --project "$INCUS_PROJECT" exec \
        "$instance_name" -- sh -c \
        "id; stat -c 'guest-share=%a:%u:%g' '/mnt/manila/$TAG'; grep -F '/mnt/manila/$TAG' /proc/self/mountinfo" || true
    fail "guest cannot write the attached share"
    fi
    sleep 2
done

if case_selected gate; then
    pid_before=$(guest_pid "$SOURCE_SSH")
    compute_mount_namespace_remote "$DEST_SSH" \
        mount --bind /bin/false /sbin/mount.ceph
    mount_failpoint=1
    openstack server migrate --live-migration --host "$DEST_HOST" \
        --wait "$server_id" >/dev/null 2>&1 || true
    clear_mount_failpoint
    wait_status ACTIVE
    [[ "$(server_host)" == "$SOURCE_HOST" ]] ||
        fail "instance left the source after the gated failure"
    [[ "$(guest_pid "$SOURCE_SSH")" == "$pid_before" ]] ||
        fail "source guest PID changed after the gated failure"
    staging="$(share_staging_root)/$server_id/$share_id"
    ! compute_remote "$DEST_SSH" findmnt -rn "$staging" >/dev/null 2>&1 ||
        fail "destination retained the gated share mount"
    ! incus_remote "$DEST_SSH" incus --project "$INCUS_PROJECT" info \
        "$instance_name" >/dev/null 2>&1 ||
        fail "destination retained an instance record"
    echo "PASS Manila destination pre-mount gate failed cleanly"
fi

if case_selected retry; then
    openstack server migrate --live-migration --host "$DEST_HOST" \
        --wait "$server_id"
    wait_status ACTIVE
    [[ "$(server_host)" == "$DEST_HOST" ]] ||
        fail "retry did not land on the destination"
    incus_remote "$DEST_SSH" incus --project "$INCUS_PROJECT" exec \
        "$instance_name" -- cat "$marker_path" | grep -Fxq "$NAME" ||
        fail "share content was lost across retry"
    echo "PASS Manila migration retry preserved share content"
fi

if case_selected recovery; then
    [[ "$(server_host)" == "$DEST_HOST" ]] ||
        fail "recovery requires the successful retry on destination"
    staging="$(share_staging_root)/$server_id/$share_id"
    compute_remote "$DEST_SSH" findmnt -rn "$staging" >/dev/null ||
        fail "share staging mount is absent before restart"
    incus_remote "$DEST_SSH" incus --project "$INCUS_PROJECT" stop \
        "$instance_name" --force
    compute_mount_namespace_remote "$DEST_SSH" umount -l "$staging"
    restart_compute_pod || fail "nova-compute Pod did not restart"
    deadline=$((SECONDS + TIMEOUT))
    while ((SECONDS < deadline)); do
        if compute_remote "$DEST_SSH" findmnt -rn "$staging" >/dev/null 2>&1 && \
                [[ "$(incus_remote "$DEST_SSH" incus --project "$INCUS_PROJECT" list "$instance_name" --format csv -c s 2>/dev/null | tr -d '\r')" == RUNNING ]]; then
            break
        fi
        sleep 5
    done
    compute_remote "$DEST_SSH" findmnt -rn "$staging" >/dev/null ||
        fail "share was not remounted after compute restart"
    [[ "$(incus_remote "$DEST_SSH" incus --project "$INCUS_PROJECT" list "$instance_name" --format csv -c s | tr -d '\r')" == RUNNING ]] ||
        fail "guest was not resumed after compute restart"
    incus_remote "$DEST_SSH" incus --project "$INCUS_PROJECT" exec \
        "$instance_name" -- cat "$marker_path" | grep -Fxq "$NAME" ||
        fail "guest lost share access after compute restart"
    deadline=$((SECONDS + TIMEOUT))
    until incus_remote "$DEST_SSH" incus --project "$INCUS_PROJECT" exec \
            "$instance_name" -- sh -c \
            "echo recovered >> '$marker_path' && sync" \
            >/dev/null 2>&1; do
        ((SECONDS < deadline)) || fail "recovered share is not writable"
        sleep 2
    done
    wait_mapping active
    echo "PASS Manila compute restart remounted the share and resumed the guest"
fi

echo "PASS Incus Manila gate/retry/recovery cases=$CASES server=$server_id share=$share_id"
