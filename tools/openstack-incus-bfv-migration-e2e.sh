#!/usr/bin/env bash
# Validate Cinder BFV zero-copy migration, confirm, revert and hard recovery.

set -euo pipefail

IMAGE=${IMAGE:?Set IMAGE to a raw rootfs-directory Glance image}
FLAVOR=${FLAVOR:-m1.small}
NETWORK=${NETWORK:-private}
INCUS_PROJECT=${INCUS_PROJECT:-nova}
VOLUME_SIZE=${VOLUME_SIZE:-2}
VOLUME_TYPE=${VOLUME_TYPE:-ceph}
SOURCE_HOST=${SOURCE_HOST:-incus-node-01}
DEST_HOST=${DEST_HOST:-incus-node-02}
SOURCE_SSH=${SOURCE_SSH:-root@10.224.0.16}
DEST_SSH=${DEST_SSH:-root@10.224.0.17}
DEST_MIGRATION_IP=${DEST_MIGRATION_IP:-${DEST_SSH#*@}}
MIGRATION_PORT=${MIGRATION_PORT:-8443}
INJECT_PREFLIGHT_FAILURE=${INJECT_PREFLIGHT_FAILURE:-true}
INJECT_POST_CLAIM_FAILURE=${INJECT_POST_CLAIM_FAILURE:-false}
INJECT_REVERT_FAILURE=${INJECT_REVERT_FAILURE:-false}
POST_CLAIM_FAILPOINT=${POST_CLAIM_FAILPOINT:-data-volume}
MIGRATE_STOPPED_INSTANCE=${MIGRATE_STOPPED_INSTANCE:-false}
TEST_DATA_VOLUME_RECOVERY=${TEST_DATA_VOLUME_RECOVERY:-true}
KEEP_RESOURCES_ON_FAILURE=${KEEP_RESOURCES_ON_FAILURE:-false}
CONTROLLER_SSH=${CONTROLLER_SSH:-$SOURCE_SSH}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
TIMEOUT=${TIMEOUT:-600}
NAME=${NAME:-incus-bfv-migration-$RANDOM}

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)
server_id=
volume_id=
data_volume_id=
instance_name=
port_id=
preflight_rule_installed=false
post_claim_failpoint_path=
start_failpoint_pid=

remote() {
    local host=$1
    shift
    "${SSH[@]}" "$host" "$@"
}

openstack() {
    local command_line
    printf -v command_line '%q ' "$@"
    remote "$CONTROLLER_SSH" \
        "source $CONTROLLER_OPENRC >/dev/null 2>&1; openstack $command_line"
}

incus() {
    local host=$1
    shift
    local command_line
    printf -v command_line '%q ' "$@"
    if [[ "${1:-}" == query ]]; then
        remote "$host" "podman exec incus incus $command_line"
    else
        remote "$host" \
            "podman exec incus incus --project $(printf '%q' "$INCUS_PROJECT") $command_line"
    fi
}

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

wait_value() {
    local expected=$1
    shift
    local deadline=$((SECONDS + TIMEOUT)) current
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

wait_host_rbd_unmapped() {
    local host=$1 volume=$2 deadline=$((SECONDS + TIMEOUT)) mappings
    while ((SECONDS < deadline)); do
        mappings=$(remote "$host" \
            "rbd device list --format json --id cinder 2>/dev/null | \
             jq -r '.[] | select(.name == \"volume-$volume\") | .device'" ||
            true)
        [[ -z "$mappings" ]] && return 0
        sleep 2
    done
    fail "$host still maps Cinder volume $volume"
}

server_status() {
    openstack server show "$server_id" -f value -c status
}

volume_status() {
    openstack volume show "$volume_id" -f value -c status
}

attachment_status() {
    local expected_volume=${1:-$volume_id}
    # Some openstackclient releases accept --volume-id but still return every
    # attachment. Filter explicitly so another server cannot change the value.
    openstack volume attachment list -f value -c 'Volume ID' -c Status | \
        awk -v id="$expected_volume" '$1 == id {print $2}'
}

port_status() {
    openstack port show "$port_id" -f value -c status
}

recovery_marker_status() {
    if incus "$DEST_SSH" profile show "$instance_name" | \
            grep -q "user.openstack.recovery_required:"; then
        echo present
    else
        echo absent
    fi
}

dump_network_diagnostics() {
    local host
    echo "=== Neutron port ===" >&2
    openstack port show "$port_id" -f json >&2 || true
    for host in "$SOURCE_SSH" "$DEST_SSH"; do
        echo "=== $host Incus/OVS network state ===" >&2
        incus "$host" profile show "$instance_name" >&2 || true
        incus "$host" exec "$instance_name" -- ip -d link >&2 || true
        remote "$host" \
            "ovs-vsctl --columns=name,ofport,link_state,external_ids \
             list Interface 2>/dev/null | grep -A4 -B1 '$port_id' || true" \
            >&2 || true
    done
}

wait_port_active() {
    local deadline=$((SECONDS + TIMEOUT)) current
    while ((SECONDS < deadline)); do
        current=$(port_status 2>/dev/null || true)
        [[ "$current" == ACTIVE ]] && return 0
        sleep 2
    done
    dump_network_diagnostics
    fail "timed out waiting for ACTIVE Neutron port (current: ${current:-missing})"
}

latest_migration_status() {
    openstack server migration list --server "$server_id" \
        -f value -c ID -c Status | sort -nr | awk 'NR == 1 {print $2}'
}

assert_owner() {
    local owner=$1 stale=$2 expected_host=$3
    [[ "$(incus "$owner" list "$instance_name" --format csv -c s)" == \
        RUNNING ]] || fail "owner instance is not running on $owner"
    ! incus "$stale" info "$instance_name" >/dev/null 2>&1 || \
        fail "stale instance still exists on $stale"
    [[ "$(openstack server show "$server_id" -f value \
        -c OS-EXT-SRV-ATTR:host)" == "$expected_host" ]] || \
        fail "Nova host does not match Incus owner"
    [[ "$(openstack port show "$port_id" -f value -c binding_host_id)" == \
        "$expected_host" ]] || fail "Neutron binding host is stale"
    wait_port_active
    wait_value attached attachment_status
}

cleanup() {
    local exit_status=$?
    if [[ -n "$post_claim_failpoint_path" ]]; then
        remote "$DEST_SSH" umount "$post_claim_failpoint_path" \
            >/dev/null 2>&1 || true
        post_claim_failpoint_path=
    fi
    if [[ -n "$start_failpoint_pid" ]]; then
        remote "$DEST_SSH" kill "$start_failpoint_pid" >/dev/null 2>&1 || true
        start_failpoint_pid=
    fi
    if [[ "$preflight_rule_installed" == true ]]; then
        remote "$SOURCE_SSH" iptables -D OUTPUT -p tcp \
            -d "$DEST_MIGRATION_IP" --dport "$MIGRATION_PORT" \
            -j REJECT >/dev/null 2>&1 || true
        preflight_rule_installed=false
    fi
    if ((exit_status != 0)) && \
            [[ "$KEEP_RESOURCES_ON_FAILURE" == true ]]; then
        echo "Preserving failed E2E resources for diagnosis:" \
            "server=$server_id root_volume=$volume_id" \
            "data_volume=$data_volume_id" >&2
        return
    fi
    if [[ -n "$server_id" ]]; then
        openstack server delete --wait "$server_id" >/dev/null 2>&1 || true
    fi
    if [[ -n "$volume_id" ]]; then
        wait_value available volume_status >/dev/null 2>&1 || true
        openstack volume delete "$volume_id" >/dev/null 2>&1 || true
        wait_absent openstack volume show "$volume_id" >/dev/null 2>&1 || true
    fi
    if [[ -n "$data_volume_id" ]]; then
        wait_value available openstack volume show "$data_volume_id" \
            -f value -c status >/dev/null 2>&1 || true
        openstack volume delete "$data_volume_id" >/dev/null 2>&1 || true
        wait_absent openstack volume show "$data_volume_id" \
            >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

[[ "$VOLUME_SIZE" =~ ^[1-9][0-9]*$ ]] || fail "VOLUME_SIZE must be positive"
for host in "$SOURCE_SSH" "$DEST_SSH"; do
    incus "$host" query /1.0 | grep -q migration_shared_ceph_storage || \
        fail "$host lacks migration_shared_ceph_storage"
    incus "$host" query /1.0 | grep -q storage_driver_cephext || \
        fail "$host lacks storage_driver_cephext"
    incus "$host" storage show cinder-bfv | grep -q '^driver: cephext$' || \
        fail "$host cinder-bfv pool is not cephext"
done

volume_id=$(openstack volume create --type "$VOLUME_TYPE" --image "$IMAGE" \
    --size "$VOLUME_SIZE" --bootable -f value -c id "${NAME}-root")
wait_value available volume_status

server_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --volume "$volume_id" --network "$NETWORK" \
    --host "$SOURCE_HOST" -f value -c id "$NAME")
wait_value ACTIVE server_status
instance_name=$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)
port_id=$(openstack port list --server "$server_id" -f value -c ID)
if [[ "$TEST_DATA_VOLUME_RECOVERY" == true ]]; then
    data_volume_id=$(openstack volume create --type "$VOLUME_TYPE" --size 1 \
        -f value -c id "${NAME}-data")
    wait_value available openstack volume show "$data_volume_id" \
        -f value -c status
    openstack server add volume "$server_id" "$data_volume_id" >/dev/null
    wait_value attached attachment_status "$data_volume_id"
fi
fixed_ip=$(incus "$SOURCE_SSH" exec "$instance_name" -- sh -c \
    "ip -4 -o addr show eth0 | awk '{print \$4}'")
[[ -n "$fixed_ip" ]] || fail "source instance has no eth0 IPv4 address"
marker="bfv-migration-$server_id"
incus "$SOURCE_SSH" exec "$instance_name" -- sh -c \
    "printf %s '$marker' > /root/nova-bfv-migration-marker; sync"

if [[ "$INJECT_PREFLIGHT_FAILURE" == true ]]; then
    remote "$SOURCE_SSH" iptables -I OUTPUT 1 -p tcp \
        -d "$DEST_MIGRATION_IP" --dport "$MIGRATION_PORT" -j REJECT
    preflight_rule_installed=true
    # InstanceFaultRollback restores ACTIVE, so OSC's generic --wait cannot
    # distinguish this expected rejection from a migration that never ran.
    openstack --os-compute-api-version 2.56 server migrate \
        --host "$DEST_HOST" "$server_id" || true
    wait_value error latest_migration_status
    wait_value ACTIVE server_status
    assert_owner "$SOURCE_SSH" "$DEST_SSH" "$SOURCE_HOST"
    remote "$SOURCE_SSH" iptables -D OUTPUT -p tcp \
        -d "$DEST_MIGRATION_IP" --dport "$MIGRATION_PORT" -j REJECT
    preflight_rule_installed=false
fi

if [[ "$MIGRATE_STOPPED_INSTANCE" == true ]]; then
    [[ "$INJECT_POST_CLAIM_FAILURE" == true ]] || \
        fail "stopped-instance mode requires post-claim injection"
    openstack server stop "$server_id"
    wait_value SHUTOFF server_status
fi

if [[ "$INJECT_POST_CLAIM_FAILURE" == true ]]; then
    case "$POST_CLAIM_FAILPOINT" in
        data-volume)
            [[ -n "$data_volume_id" ]] || \
                fail "data-volume injection requires a test data volume"
            post_claim_failpoint_path=/usr/bin/rbd
            ;;
        start)
            vif_source=$(remote "$SOURCE_SSH" \
                "podman exec incus incus query \
                 '/1.0/profiles/$instance_name' |
                 jq -r '(.metadata.devices // .devices)[] |
                   select(.type == \"nic\" and .nictype == \"physical\") |
                   .parent'")
            [[ -n "$vif_source" ]] || fail "cannot determine Incus VIF source"
            start_failpoint_pid=$(remote "$DEST_SSH" \
                "nohup sh -c 'until ip link show \"$vif_source\" \
                 >/dev/null 2>&1; do sleep 0.02; done; \
                 ip link delete \"$vif_source\"' \
                 >/tmp/openstack-incus-start-failpoint.log 2>&1 & echo \$!")
            ;;
        *)
            fail "unsupported POST_CLAIM_FAILPOINT: $POST_CLAIM_FAILPOINT"
            ;;
    esac
    if [[ -n "$post_claim_failpoint_path" ]]; then
        remote "$DEST_SSH" mount --bind /bin/false \
            "$post_claim_failpoint_path"
    fi
    openstack --os-compute-api-version 2.56 server migrate \
        --host "$DEST_HOST" "$server_id" || true
    wait_value finished latest_migration_status
    wait_value VERIFY_RESIZE server_status
    if [[ -n "$post_claim_failpoint_path" ]]; then
        remote "$DEST_SSH" umount "$post_claim_failpoint_path"
        post_claim_failpoint_path=
    fi
    start_failpoint_pid=

    # The optional IncusComputeManager repairs runtime state, but deliberately
    # preserves Nova's VERIFY_RESIZE confirm/revert contract.
    expected_incus_state=RUNNING
    expected_server_state=ACTIVE
    if [[ "$MIGRATE_STOPPED_INSTANCE" == true ]]; then
        expected_incus_state=STOPPED
        expected_server_state=SHUTOFF
    fi
    wait_value "$expected_incus_state" incus "$DEST_SSH" list \
        "$instance_name" --format csv -c s
    wait_value VERIFY_RESIZE server_status
    wait_value absent recovery_marker_status
    wait_value attached attachment_status "$data_volume_id"
    [[ "$(openstack server show "$server_id" -f value \
        -c OS-EXT-SRV-ATTR:host)" == "$DEST_HOST" ]] || \
        fail "Nova ownership did not converge on the recovered target"
    [[ "$(openstack port show "$port_id" -f value \
        -c binding_host_id)" == "$DEST_HOST" ]] || \
        fail "Neutron ownership did not converge on the recovered target"

    openstack server resize confirm "$server_id"
    wait_value "$expected_server_state" server_status
    openstack server delete --wait "$server_id"
    server_id=
    wait_host_rbd_unmapped "$SOURCE_SSH" "$data_volume_id"
    wait_host_rbd_unmapped "$DEST_SSH" "$data_volume_id"
    wait_value available volume_status
    openstack volume delete "$volume_id"
    wait_absent openstack volume show "$volume_id"
    volume_id=
    wait_value available openstack volume show "$data_volume_id" \
        -f value -c status
    openstack volume delete "$data_volume_id"
    wait_absent openstack volume show "$data_volume_id"
    data_volume_id=
    echo "PASS BFV post-claim automatic recovery and cleanup"
    exit 0
fi

openstack --os-compute-api-version 2.56 server migrate \
    --host "$DEST_HOST" --wait "$server_id"
wait_value VERIFY_RESIZE server_status
[[ "$(incus "$SOURCE_SSH" list "$instance_name" --format csv -c s)" == \
    STOPPED ]] || fail "source is not retained stopped before confirm"
[[ "$(incus "$DEST_SSH" exec "$instance_name" -- \
    cat /root/nova-bfv-migration-marker)" == "$marker" ]] || \
    fail "root marker missing on destination"
[[ "$(incus "$DEST_SSH" exec "$instance_name" -- sh -c \
    "ip -4 -o addr show eth0 | awk '{print \$4}'")" == "$fixed_ip" ]] || \
    fail "fixed IP changed after migration"
openstack server resize confirm "$server_id"
wait_value ACTIVE server_status
assert_owner "$DEST_SSH" "$SOURCE_SSH" "$DEST_HOST"

# Model a post-claim start failure. Standard Nova hard reboot must start the
# retained stopped owner without changing its root volume or OpenStack owner.
incus "$DEST_SSH" stop "$instance_name"
if [[ -n "$data_volume_id" ]]; then
    incus "$DEST_SSH" profile device remove "$instance_name" "$data_volume_id"
    incus "$DEST_SSH" profile unset "$instance_name" \
        "user.openstack.volume.$data_volume_id"
    data_device=$(remote "$DEST_SSH" \
        "podman exec incus rbd device list --format json --id cinder | \
         jq -r '.[] | select(.name == \"volume-$data_volume_id\") | .device'")
    [[ -n "$data_device" ]] || fail "data RBD mapping is missing before injection"
    remote "$DEST_SSH" \
        "podman exec incus rbd device unmap '$data_device' --id cinder"
fi
openstack server reboot --hard "$server_id"
wait_value ACTIVE server_status
wait_value RUNNING incus "$DEST_SSH" list "$instance_name" --format csv -c s
[[ "$(incus "$DEST_SSH" exec "$instance_name" -- \
    cat /root/nova-bfv-migration-marker)" == "$marker" ]] || \
    fail "root marker missing after hard-reboot recovery"
if [[ -n "$data_volume_id" ]]; then
    incus "$DEST_SSH" profile show "$instance_name" | \
        grep -q "user.openstack.volume.$data_volume_id:" || \
        fail "hard reboot did not restore data-volume connector metadata"
    incus "$DEST_SSH" profile show "$instance_name" | \
        grep -q "^  $data_volume_id:" || \
        fail "hard reboot did not restore the data-volume device"
    wait_value attached attachment_status "$data_volume_id"
fi
assert_owner "$DEST_SSH" "$SOURCE_SSH" "$DEST_HOST"

openstack --os-compute-api-version 2.56 server migrate \
    --host "$SOURCE_HOST" --wait "$server_id"
wait_value VERIFY_RESIZE server_status
if [[ "$INJECT_REVERT_FAILURE" == true ]]; then
    post_claim_failpoint_path=/usr/bin/rbd
    remote "$DEST_SSH" mount --bind /bin/false "$post_claim_failpoint_path"
    openstack server resize revert "$server_id"
    wait_value present recovery_marker_status
    remote "$DEST_SSH" umount "$post_claim_failpoint_path"
    post_claim_failpoint_path=
    wait_value absent recovery_marker_status
else
    openstack server resize revert "$server_id"
fi
wait_value ACTIVE server_status
assert_owner "$DEST_SSH" "$SOURCE_SSH" "$DEST_HOST"
[[ "$(incus "$DEST_SSH" exec "$instance_name" -- \
    cat /root/nova-bfv-migration-marker)" == "$marker" ]] || \
    fail "root marker missing after revert"

trap - EXIT INT TERM
openstack server delete --wait "$server_id"
server_id=
if [[ -n "$data_volume_id" ]]; then
    wait_host_rbd_unmapped "$SOURCE_SSH" "$data_volume_id"
    wait_host_rbd_unmapped "$DEST_SSH" "$data_volume_id"
fi
wait_value available volume_status
openstack volume delete "$volume_id"
wait_absent openstack volume show "$volume_id"
volume_id=
if [[ -n "$data_volume_id" ]]; then
    wait_value available openstack volume show "$data_volume_id" \
        -f value -c status
    openstack volume delete "$data_volume_id"
    wait_absent openstack volume show "$data_volume_id"
    data_volume_id=
fi
echo "PASS BFV preflight failure, zero-copy confirm, revert and hard-reboot recovery"
