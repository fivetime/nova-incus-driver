#!/usr/bin/env bash
# Exercise an exact-ID Ceph root deletion through a dependent-clone/ABA retry.

set -Eeuo pipefail

RUN_DESTRUCTIVE=${RUN_DESTRUCTIVE:-false}
if [[ "$RUN_DESTRUCTIVE" != true ]]; then
    echo "Refusing destructive E2E; set RUN_DESTRUCTIVE=true" >&2
    exit 2
fi

IMAGE=${IMAGE:?Set IMAGE to an admitted Incus system-container image}
FLAVOR=${FLAVOR:?Set FLAVOR to an Incus-compatible Flavor}
NETWORK=${NETWORK:?Set NETWORK to a tenant network}
NOVA_HOST=${NOVA_HOST:?Set NOVA_HOST to the target nova-compute hostname}
COMPUTE_SSH=${COMPUTE_SSH:?Set COMPUTE_SSH to the target compute SSH address}
EXPECTED_ROOT_POOL=${EXPECTED_ROOT_POOL:?Set EXPECTED_ROOT_POOL to the Incus-owned Ceph root pool}
CONTROLLER_SSH=${CONTROLLER_SSH:-}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
SSH_KNOWN_HOSTS_FILE=${SSH_KNOWN_HOSTS_FILE:-$HOME/.ssh/known_hosts}
INCUS_PROJECT=${INCUS_PROJECT:-nova}
INCUS_RUNTIME_MODE=${INCUS_RUNTIME_MODE:-podman}
INCUS_RUNTIME_CONTAINER=${INCUS_RUNTIME_CONTAINER:-incus}
INCUS_KUBE_NAMESPACE=${INCUS_KUBE_NAMESPACE:-openstack}
INCUS_KUBE_NODE=${INCUS_KUBE_NODE:-}
KUBE_CONTROL_SSH=${KUBE_CONTROL_SSH:-}
TIMEOUT=${TIMEOUT:-600}
RUN_UUID=${RUN_UUID:-$(python3 -c 'import uuid; print(uuid.uuid4())')}
NAME=${NAME:-incus-ceph-aba-${RUN_UUID}}
E2E_LOCK_FILE=${E2E_LOCK_FILE:-/run/lock/openstack-incus-ceph-aba-e2e.lock}

exec 9>"$E2E_LOCK_FILE"
if ! flock -n 9; then
    echo "Another exact-delete E2E owns $E2E_LOCK_FILE" >&2
    exit 2
fi

for command_name in jq sha256sum base64 flock awk python3; do
    command -v "$command_name" >/dev/null || {
        echo "$command_name is required for the exact-delete E2E" >&2
        exit 2
    }
done
[[ "$RUN_UUID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]] || {
    echo "RUN_UUID must be a canonical UUIDv4" >&2
    exit 2
}
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
instance_name=
mutation_started=false
test_complete=false
clone_name="incus_identity_test_clone_${RUN_UUID//-/}"
snapshot_name="identity_hold_${RUN_UUID//-/}"
b_device=

remote() {
    local host=$1
    shift
    "${SSH[@]}" "$host" "$@"
}

openstack() {
    if [[ -z "$CONTROLLER_SSH" ]]; then
        command openstack "$@"
        return
    fi

    local command_line
    printf -v command_line '%q ' "$@"
    remote "$CONTROLLER_SSH" \
        "source $CONTROLLER_OPENRC >/dev/null 2>&1; openstack $command_line"
}

runtime() {
    local command_line kube_command
    case "$INCUS_RUNTIME_MODE" in
        podman)
            printf -v command_line '%q ' podman exec \
                "$INCUS_RUNTIME_CONTAINER" "$@"
            remote "$COMPUTE_SSH" "$command_line"
            ;;
        kubernetes)
            [[ -n "$INCUS_KUBE_NODE" ]] || return 2
            printf -v command_line '%q ' "$@"
            printf -v kube_command \
                'set -e; pods=$(kubectl -n %q get pod -l application=incus --field-selector spec.nodeName=%q --no-headers -o custom-columns=NAME:.metadata.name); set -- $pods; [ $# -eq 1 ]; kubectl -n %q exec -i "$1" -- %s' \
                "$INCUS_KUBE_NAMESPACE" "$INCUS_KUBE_NODE" \
                "$INCUS_KUBE_NAMESPACE" "$command_line"
            if [[ -n "$KUBE_CONTROL_SSH" ]]; then
                remote "$KUBE_CONTROL_SSH" "$kube_command"
            else
                bash -c "$kube_command"
            fi
            ;;
        *) return 2 ;;
    esac
}

incus_query() {
    runtime incus query "$@"
}

incus() {
    runtime incus --project "$INCUS_PROJECT" "$@"
}

ceph_rbd() {
    runtime rbd --cluster "$ceph_cluster" --id "$ceph_user" \
        --pool "$ceph_pool" "$@"
}

ceph_rados() {
    runtime rados --cluster "$ceph_cluster" --id "$ceph_user" \
        --pool "$ceph_pool" "$@"
}

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

skip() {
    echo "SKIP: $*" >&2
    exit 77
}

wait_value() {
    local expected=$1
    shift
    local deadline=$((SECONDS + TIMEOUT)) current=
    while ((SECONDS < deadline)); do
        current=$("$@" 2>/dev/null || true)
        [[ "$current" == "$expected" ]] && return 0
        [[ "$current" == ERROR ]] && break
        sleep 2
    done
    fail "timed out waiting for $expected (current: ${current:-missing})"
}

wait_absent() {
    local deadline=$((SECONDS + TIMEOUT))
    while ((SECONDS < deadline)); do
        "$@" >/dev/null 2>&1 || return 0
        sleep 2
    done
    fail "timed out waiting for resource deletion"
}

operation_path() {
    local response=$1 path=
    path=$(jq -er '
        if type == "string" then .
        elif type == "object" then
          (.operation // .metadata.operation // .metadata.id // .id // empty)
        else empty end' <<<"$response" 2>/dev/null || true)
    if [[ -z "$path" && "$response" == /1.0/operations/* ]]; then
        path=$response
    fi
    if [[ "$path" =~ ^[0-9a-f-]{36}$ ]]; then
        path="/1.0/operations/$path"
    fi
    [[ "$path" == /1.0/operations/* ]] || \
        fail "Incus delete returned no operation path: $response"
    printf '%s\n' "$path"
}

wait_operation() {
    local path=$1 expected=$2
    local deadline=$((SECONDS + TIMEOUT)) document status error_message
    while ((SECONDS < deadline)); do
        document=$(incus_query "$path") || \
            fail "could not query started Incus operation $path"
        status=$(jq -r '.metadata.status // .status // empty' \
            <<<"$document")
        case "${status,,}" in
            success)
                [[ "$expected" == success ]] || \
                    fail "Incus operation unexpectedly succeeded"
                printf '%s\n' "$document"
                return 0
                ;;
            failure|cancelled)
                error_message=$(jq -r \
                    '.metadata.err // .err // .error // empty' \
                    <<<"$document")
                [[ "$expected" == failure ]] || \
                    fail "Incus operation failed: $error_message"
                [[ -n "$error_message" ]] || \
                    fail "failed Incus operation returned no error"
                printf '%s\n' "$document"
                return 0
                ;;
            running|pending|'') ;;
            *) fail "unexpected Incus operation status: $status" ;;
        esac
        sleep 1
    done
    fail "timed out waiting for Incus operation $path"
}

delete_instance_with_receipt() {
    local expected=$1 response path
    response=$(incus_query -X DELETE "$instance_delete_url") || \
        fail "Incus rejected the exact-binding delete request"
    path=$(operation_path "$response")
    wait_operation "$path" "$expected"
}

exact_mapping_count() {
    local pool_id=$1 image_id=$2 python_program
    python_program='import pathlib,sys
pool_id,image_id=sys.argv[1:]
root=pathlib.Path("/sys/bus/rbd/devices")
count=0
for entry in root.iterdir():
    if not entry.is_dir():
        continue
    if ((entry / "pool_id").read_text().strip() == pool_id and
            (entry / "image_id").read_text().strip() == image_id):
        count += 1
print(count)'
    runtime python3 -c "$python_program" "$pool_id" "$image_id"
}

rbd_identity() {
    local image_name=$1
    ceph_rbd info "$image_name" --format json | jq -ce \
        --argjson pool_id "$a_pool_id" \
        '{pool_id:$pool_id,id:.id,block_name_prefix:.block_name_prefix}'
}

rbd_name_exists() {
    local image_name=$1 listing
    listing=$(ceph_rbd ls --format json) || \
        fail "could not list RBD images while checking $image_name"
    jq -e --arg name "$image_name" 'index($name) != null' \
        <<<"$listing" >/dev/null
}

assert_incus_instance_absent() {
    local inventory
    inventory=$(incus_query "/1.0/instances?project=$INCUS_PROJECT&recursion=1") || \
        fail "could not list Incus instances before proving record absence"
    if jq -e --arg name "$instance_name" \
            '.[] | select(.name == $name)' <<<"$inventory" >/dev/null; then
        fail "Incus instance record $instance_name still exists"
    fi
}

assert_nova_server_absent() {
    local inventory
    inventory=$(openstack server list --all-projects -f json -c ID) || \
        fail "could not list Nova servers before proving server absence"
    if jq -e --arg id "$server_id" \
            '.[] | select((.ID // .Id // .id) == $id)' \
            <<<"$inventory" >/dev/null; then
        fail "Nova server $server_id still exists"
    fi
}

assert_exact_header_absent() {
    local image_id=$1 objects
    objects=$(ceph_rados ls) || \
        fail "could not list Ceph objects before proving header absence"
    if grep -Fxq "rbd_header.$image_id" <<<"$objects"; then
        fail "exact RBD header rbd_header.$image_id still exists"
    fi
}

map_b() {
    b_device=$(ceph_rbd map "$rbd_name") || \
        fail "could not map replacement RBD B"
    [[ "$b_device" == /dev/* ]] || fail "invalid B mapping: $b_device"
}

unmap_b() {
    [[ -n "$b_device" ]] || return 0
    ceph_rbd unmap "$b_device"
    b_device=
}

write_b_marker() {
    local marker=$1 marker_b64
    marker_b64=$(printf '%s' "$marker" | base64 | tr -d '\r\n')
    map_b
    runtime sh -ceu \
        "printf %s '$marker_b64' | base64 -d | dd of='$b_device' bs=1 seek=4096 conv=notrunc,fsync status=none"
    unmap_b
}

read_b_marker() {
    local length=$1 result
    map_b
    result=$(runtime dd if="$b_device" bs=1 skip=4096 count="$length" \
        status=none | base64 | tr -d '\r\n')
    unmap_b
    printf '%s\n' "$result"
}

remove_b_exact() {
    local current trash_listing
    current=$(rbd_identity "$rbd_name") || \
        fail "could not revalidate replacement RBD B before cleanup"
    [[ "$current" == "$b_identity" ]] || \
        fail "refusing to clean replacement name with a changed identity"
    ceph_rbd trash mv "$rbd_name"
    trash_listing=$(ceph_rbd trash ls --format json) || \
        fail "could not list RBD trash after moving replacement B"
    jq -e --arg id "$b_image_id" \
        '.[] | select(.id == $id)' <<<"$trash_listing" >/dev/null || \
        fail "replacement RBD B did not enter trash under its exact ID"
    ceph_rbd trash rm "$b_image_id"
}

cleanup() {
    local exit_status=$?
    trap - EXIT INT TERM
    set +e
    if [[ -n "$b_device" ]]; then
        ceph_rbd unmap "$b_device" >/dev/null 2>&1 || true
    fi
    if [[ "$test_complete" != true && "$mutation_started" == true ]]; then
        echo "SAFE-LEAK: preserving UUID-scoped ABA evidence for inspection: $RUN_UUID" >&2
        echo "SAFE-LEAK: server=${server_id:-none} instance=${instance_name:-none} clone=$clone_name" >&2
        exit "$exit_status"
    fi
    if [[ -n "$server_id" ]]; then
        openstack server delete --wait "$server_id" >/dev/null 2>&1 || true
    fi
    exit "$exit_status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ "$INCUS_RUNTIME_MODE" == podman ]]; then
    remote "$COMPUTE_SSH" \
        "command -v podman >/dev/null && test \"\$(podman inspect -f '{{.State.Running}}' $(printf '%q' "$INCUS_RUNTIME_CONTAINER"))\" = true" || \
        fail "compute host has no running Podman Incus runtime"
fi
runtime sh -ceu '
    for command_name in incus python3 ceph rbd rados dd base64; do
        command -v "$command_name" >/dev/null || {
            echo "missing runtime command: $command_name" >&2
            exit 1
        }
    done
' || fail "Incus runtime lacks an exact-delete E2E dependency"

server_document=$(incus_query /1.0) || fail "could not query Incus server"
missing_extensions=()
for extension in storage_materialization_attempt_v1 storage_release_receipt_v2; do
    jq -e --arg extension "$extension" \
        '.api_extensions | index($extension) != null' \
        <<<"$server_document" >/dev/null || missing_extensions+=("$extension")
done
((${#missing_extensions[@]} == 0)) || \
    skip "unverified exact-delete prerequisites: ${missing_extensions[*]}"

pool_document=$(incus_query "/1.0/storage-pools/$EXPECTED_ROOT_POOL") || \
    fail "could not query expected Incus root pool $EXPECTED_ROOT_POOL"
[[ "$(jq -r .driver <<<"$pool_document")" == ceph ]] || \
    skip "expected root pool $EXPECTED_ROOT_POOL is not the Incus-owned Ceph driver"
ceph_pool=$(jq -er '.config["ceph.osd.pool_name"] // .config.source' \
    <<<"$pool_document")
ceph_user=$(jq -er '.config["ceph.user.name"] // "admin"' \
    <<<"$pool_document")
ceph_cluster=$(jq -er '.config["ceph.cluster_name"] // "ceph"' \
    <<<"$pool_document")
pool_map=$(runtime ceph --cluster "$ceph_cluster" \
    --name "client.$ceph_user" osd map "$ceph_pool" rbd_directory \
    --format json) || fail "could not query the configured Ceph pool"
preflight_pool_id=$(jq -er .pool_id <<<"$pool_map")
[[ "$preflight_pool_id" =~ ^[0-9]+$ ]] || \
    fail "Ceph returned no numeric pool identity"
ceph_rbd ls --format json >/dev/null || \
    fail "could not list the configured RBD pool"
ceph_rados ls >/dev/null || \
    fail "could not list objects in the configured Ceph pool"

server_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --image "$IMAGE" --network "$NETWORK" \
    --host "$NOVA_HOST" "$NAME" -f value -c id)
wait_value ACTIVE openstack server show "$server_id" -f value -c status
instance_name=$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)
[[ "$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:host)" == "$NOVA_HOST" ]] || \
    fail "Nova did not place the probe on $NOVA_HOST"

instance_document=$(incus_query \
    "/1.0/instances/$instance_name?project=$INCUS_PROJECT") || \
    fail "could not query the created Incus instance"
root_pool=$(jq -er '.expanded_devices.root.pool' <<<"$instance_document")
[[ "$root_pool" == "$EXPECTED_ROOT_POOL" ]] || \
    fail "Nova selected root pool $root_pool, expected $EXPECTED_ROOT_POOL"
materialization_id=$(jq -er \
    '.config["user.openstack.rootfs_materialization_id"]' \
    <<<"$instance_document")
owner=$(jq -er '.config["user.openstack.uuid"]' <<<"$instance_document")
allocation_id=$(jq -er \
    '.config["user.openstack.idmap_allocation_id"]' \
    <<<"$instance_document")
compute_id=$(jq -er '.config["user.openstack.compute_id"]' \
    <<<"$instance_document")

attempt_document=$(incus_query \
    "/1.0/storage-materialization-attempts/$materialization_id?project=$INCUS_PROJECT") || \
    fail "could not query the committed root materialization"
[[ "$(jq -r .state <<<"$attempt_document")" == committed ]] || \
    fail "root materialization is not committed"
[[ "$(jq -r .finished <<<"$attempt_document")" == true ]] || \
    fail "root materialization has not finished"
[[ "$(jq -r .storage_driver <<<"$attempt_document")" == ceph ]] || \
    fail "root materialization is not owned by the ceph driver"
storage_identity=$(jq -er .storage_identity <<<"$attempt_document" \
    2>/dev/null || true)
if [[ -z "$storage_identity" ]] || ! identity_document=$(jq -ce \
        'select(
          (.pool_id | type) == "number" and
          (.id | type) == "string" and
          (.block_name_prefix | type) == "string")' \
        <<<"$storage_identity" 2>/dev/null); then
    openstack server delete --wait "$server_id" >/dev/null
    server_id=
    skip "Incus does not expose a canonical exact Ceph storage identity"
fi
a_pool_id=$(jq -r .pool_id <<<"$identity_document")
a_image_id=$(jq -r .id <<<"$identity_document")
a_block_prefix=$(jq -r .block_name_prefix <<<"$identity_document")
[[ "$a_block_prefix" == "rbd_data.$a_image_id" ]] || \
    fail "materialization identity has a mismatched block prefix"
[[ "$(jq -r .allocation_id <<<"$attempt_document")" == "$allocation_id" ]] || \
    fail "attempt allocation ID does not match the instance"
[[ "$(jq -r .compute_id <<<"$attempt_document")" == "$compute_id" ]] || \
    fail "attempt compute ID does not match the instance"
[[ "$(jq -r .owner <<<"$attempt_document")" == "$owner" ]] || \
    fail "attempt owner does not match the instance"

storage_volume=$(jq -er .storage_volume <<<"$attempt_document")
[[ "$(jq -r .instance_name <<<"$attempt_document")" == \
    "$instance_name" ]] || fail "attempt instance name does not match Nova"
[[ "$(jq -r .storage_pool <<<"$attempt_document")" == "$root_pool" ]] || \
    fail "attempt storage pool does not match the instance root"
expected_storage_volume="${INCUS_PROJECT}_${instance_name}"
if [[ "$INCUS_PROJECT" == default ]]; then
    expected_storage_volume=$instance_name
fi
[[ "$storage_volume" == "$expected_storage_volume" ]] || \
    fail "public attempt returned an unexpected root volume name: $storage_volume"
rbd_name="container_${storage_volume}"
actual_identity=$(rbd_identity "$rbd_name") || \
    fail "could not read the original RBD A identity"
[[ "$actual_identity" == "$identity_document" ]] || \
    fail "RBD A identity does not match its materialization"
[[ "$preflight_pool_id" == "$a_pool_id" ]] || \
    fail "Ceph pool ID does not match the materialization"

openstack server stop "$server_id"
wait_value SHUTOFF openstack server show "$server_id" -f value -c status
[[ "$(exact_mapping_count "$a_pool_id" "$a_image_id")" == 0 ]] || \
    fail "RBD A remained mapped after the instance stopped"

ceph_rbd snap create "$rbd_name@$snapshot_name"
ceph_rbd snap protect "$rbd_name@$snapshot_name"
# rbd clone applies --pool only to the source snap spec; the destination
# image must be pool-qualified or it lands in the default "rbd" pool.
ceph_rbd clone "$rbd_name@$snapshot_name" "$ceph_pool/$clone_name"
mutation_started=true

instance_delete_url="/1.0/instances/$instance_name?project=$INCUS_PROJECT&rootfs-idmap-release-token=$materialization_id&rootfs-idmap-release-owner=$owner&rootfs-idmap-allocation-id=$allocation_id&rootfs-idmap-compute-id=$compute_id"
failed_operation=$(delete_instance_with_receipt failure)
grep -qi 'dependent clones' <<<"$failed_operation" || \
    fail "initial delete failed without reporting its dependent clone"

tombstone_digest=$(printf '%s' "$storage_identity" | sha256sum | \
    awk '{print $1}')
tombstone_name="incus_identity_release_$tombstone_digest"
tombstone_identity=$(rbd_identity "$tombstone_name") || \
    fail "failed deletion did not retain RBD A under its exact tombstone"
[[ "$tombstone_identity" == "$identity_document" ]] || \
    fail "RBD tombstone does not contain the original A identity"
if rbd_name_exists "$rbd_name"; then
    fail "the original RBD name was not reserved into a tombstone"
fi

receipt_query="/1.0/storage-release-receipts/$materialization_id?project=$INCUS_PROJECT&rootfs-idmap-release-owner=$owner&rootfs-idmap-allocation-id=$allocation_id&rootfs-idmap-compute-id=$compute_id&instance=$instance_name&idmap-base=$(jq -r .idmap_base <<<"$attempt_document")&idmap-size=$(jq -r .idmap_size <<<"$attempt_document")"
set +e
pending_output=$(incus_query "$receipt_query" 2>&1)
pending_status=$?
set -e
((pending_status != 0)) || fail "dependent-clone receipt completed early"
grep -qi 'not complete' <<<"$pending_output" || {
    printf '%s\n' "$pending_output" >&2
    fail "pending receipt query failed without an explicit incomplete state"
}

a_size=$(ceph_rbd info "$tombstone_name" --format json | jq -er .size) || \
    fail "could not read tombstoned RBD A size"
b_size_mib=$(((a_size + 1048575) / 1048576))
ceph_rbd create --size "$b_size_mib" "$rbd_name"
b_identity=$(rbd_identity "$rbd_name") || \
    fail "could not read replacement RBD B identity"
[[ "$b_identity" != "$identity_document" ]] || \
    fail "replacement RBD B reused A's immutable identity"
b_image_id=$(jq -r .id <<<"$b_identity")
b_marker="replacement-b-$RUN_UUID"
b_marker_b64=$(printf '%s' "$b_marker" | base64 | tr -d '\r\n')
write_b_marker "$b_marker"
[[ "$(read_b_marker "${#b_marker}")" == "$b_marker_b64" ]] || \
    fail "replacement RBD B marker failed its immediate fsync/readback"
[[ "$(exact_mapping_count "$a_pool_id" "$b_image_id")" == 0 ]] || \
    fail "replacement RBD B remained mapped after marker creation"

ceph_rbd rm "$clone_name"
if rbd_name_exists "$clone_name"; then
    fail "dependent clone remained after its explicit removal"
fi
delete_instance_with_receipt success >/dev/null
assert_incus_instance_absent

[[ "$(rbd_identity "$rbd_name")" == "$b_identity" ]] || \
    fail "old A retry changed replacement RBD B identity"
[[ "$(read_b_marker "${#b_marker}")" == "$b_marker_b64" ]] || \
    fail "old A retry changed replacement RBD B content"
[[ "$(exact_mapping_count "$a_pool_id" "$a_image_id")" == 0 ]] || \
    fail "old RBD A retained an exact host mapping"
assert_exact_header_absent "$a_image_id"

receipt_document=$(incus_query "$receipt_query") || \
    fail "completed exact-delete receipt is not queryable"
[[ "$(jq -r .state <<<"$receipt_document")" == complete ]] || \
    fail "exact-delete receipt is not complete"
[[ "$(jq -r .outcome <<<"$receipt_document")" == deleted ]] || \
    fail "exact-delete receipt does not prove deletion"
[[ "$(jq -r .storage_identity <<<"$receipt_document")" == \
    "$storage_identity" ]] || fail "receipt identity changed across ABA retry"
receipt_digest=$(jq -er .digest <<<"$receipt_document")
receipt_replay=$(incus_query "$receipt_query") || \
    fail "completed exact-delete receipt could not be replayed"
[[ "$(jq -r .state <<<"$receipt_replay")" == complete ]] || \
    fail "replayed exact-delete receipt is not complete"
[[ "$(jq -r .digest <<<"$receipt_replay")" == "$receipt_digest" ]] || \
    fail "replayed exact-delete receipt changed its canonical digest"

# The normal Nova retry consumes the already-complete receipt, persists it in
# the allocator and ACKs it before retiring the exact host claim.
openstack server delete --wait "$server_id"
wait_absent openstack server show "$server_id"
assert_nova_server_absent
ack_url="$receipt_query&receipt-digest=$receipt_digest"
incus_query -X DELETE "$ack_url" >/dev/null || \
    fail "the Nova-ACKed receipt is not idempotently retired"
incus_query -X DELETE "$ack_url" >/dev/null || \
    fail "the retired exact-delete receipt did not accept ACK replay"
set +e
retired_output=$(incus_query "$receipt_query" 2>&1)
retired_status=$?
set -e
((retired_status != 0)) || fail "ACKed receipt remains visible through GET"
grep -qi 'not found' <<<"$retired_output" || {
    printf '%s\n' "$retired_output" >&2
    fail "ACKed receipt failed lookup for a reason other than retirement"
}

[[ "$(rbd_identity "$rbd_name")" == "$b_identity" ]] || \
    fail "Nova cleanup changed replacement RBD B identity"
[[ "$(read_b_marker "${#b_marker}")" == "$b_marker_b64" ]] || \
    fail "Nova cleanup changed replacement RBD B content"
remove_b_exact
assert_exact_header_absent "$b_image_id"

server_id=
test_complete=true
trap - EXIT INT TERM
echo "PASS exact Ceph deletion removed A=$a_image_id, preserved B=$b_image_id, and ACKed receipt=$materialization_id"
