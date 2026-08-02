#!/usr/bin/env bash
# Validate live or cold migration through the native Nova API.

set -Eeuo pipefail

IMAGE=${IMAGE:-alpine-3.21-criu-bfv-fuse}
FLAVOR=${FLAVOR:-ds512M}
NETWORK=${NETWORK:-public}
SOURCE_HOST=${SOURCE_HOST:-incus-node-01}
DEST_HOST=${DEST_HOST:-incus-node-02}
SOURCE_SSH=${SOURCE_SSH:-root@10.224.0.21}
DEST_SSH=${DEST_SSH:-root@10.224.0.17}
# Ordered host=ssh pairs. For example:
# incus-node-02=root@10.224.0.17,incus-node-03=root@10.224.0.22,incus-node-01=root@10.224.0.21
MIGRATION_TARGETS=${MIGRATION_TARGETS:-$DEST_HOST=$DEST_SSH}
# live keeps the source process identity; cold uses Nova's
# VERIFY_RESIZE/confirm/revert state machine.
MIGRATION_MODE=${MIGRATION_MODE:-live}
# Comma- or space-separated cold actions, one per MIGRATION_TARGETS entry.
# Empty means confirm every cold-migration hop.
MIGRATION_ACTIONS=${MIGRATION_ACTIONS:-}
CONTROLLER_SSH=${CONTROLLER_SSH:-}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
SSH_KNOWN_HOSTS_FILE=${SSH_KNOWN_HOSTS_FILE:-$HOME/.ssh/known_hosts}
SERVER=${SERVER:-incus-${MIGRATION_MODE}-migration-e2e-$RANDOM}
TIMEOUT=${TIMEOUT:-300}
INCUS_PROJECT=${INCUS_PROJECT:-nova}
KEEP_FAILED=${KEEP_FAILED:-0}
WITH_DATA_VOLUME=${WITH_DATA_VOLUME:-0}
BOOT_FROM_VOLUME=${BOOT_FROM_VOLUME:-0}
ROOT_VOLUME_TYPE=${ROOT_VOLUME_TYPE:-ceph}
ROOT_VOLUME_SIZE=${ROOT_VOLUME_SIZE:-2}
DATA_VOLUME_TYPE=${DATA_VOLUME_TYPE:-ceph}
DATA_VOLUME_SIZE=${DATA_VOLUME_SIZE:-1}
DATA_DEVICE=${DATA_DEVICE:-/dev/vdb}
DATA_VOLUME_COUNT=${DATA_VOLUME_COUNT:-1}
DATA_DEVICES=${DATA_DEVICES:-$DATA_DEVICE}
MANILA_SHARE=${MANILA_SHARE:-}
MANILA_TAG=${MANILA_TAG:-tenant-data}
# Space-separated share names or IDs and optional one-to-one guest tags.
# MANILA_SHARE/MANILA_TAG remain supported for single-share callers.
MANILA_SHARES=${MANILA_SHARES:-$MANILA_SHARE}
MANILA_TAGS=${MANILA_TAGS:-}
INJECT_RESTORE_FAILURE=${INJECT_RESTORE_FAILURE:-0}
REQUIRE_MANAGED_CEPH_ROOT=${REQUIRE_MANAGED_CEPH_ROOT:-0}
E2E_LOCK_FILE=${E2E_LOCK_FILE:-/run/lock/openstack-incus-live-migration-e2e.lock}

exec 9>"$E2E_LOCK_FILE"
if ! flock -n 9; then
    echo "Another Incus migration E2E owns $E2E_LOCK_FILE" >&2
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
instance_name=
port_id=
user_data=
root_volume_id=
shares_url=
token=
volume_ids=()
volume_devices=()
volume_markers=()
share_ids=()
share_tags=()
share_markers=()
manila_marker_paths=()
share_mounts=()
restore_failpoint_ssh=
managed_root_pool=
managed_root_driver=
managed_root_ceph_pool=
managed_root_ceph_user=
managed_root_rbd_image=
managed_root_rbd_id=
managed_root_pool_id=
root_marker=
read -r -a requested_data_devices <<<"$DATA_DEVICES"
read -r -a requested_manila_shares <<<"$MANILA_SHARES"
read -r -a requested_manila_tags <<<"$MANILA_TAGS"
IFS=',' read -r -a migration_targets <<<"$MIGRATION_TARGETS"
case "$MIGRATION_MODE" in
    live)
        [[ -z "$MIGRATION_ACTIONS" ]] || {
            echo "MIGRATION_ACTIONS is valid only for cold migration" >&2
            exit 2
        }
        migration_actions=()
        ;;
    cold)
        normalized_actions=${MIGRATION_ACTIONS//,/ }
        read -r -a migration_actions <<<"$normalized_actions"
        if ((${#migration_actions[@]} == 0)); then
            for _ in "${migration_targets[@]}"; do
                migration_actions+=(confirm)
            done
        fi
        ((${#migration_actions[@]} == ${#migration_targets[@]})) || {
            echo "MIGRATION_ACTIONS must match MIGRATION_TARGETS count" >&2
            exit 2
        }
        for action in "${migration_actions[@]}"; do
            [[ "$action" == confirm || "$action" == revert ]] || {
                echo "Unsupported cold-migration action: $action" >&2
                exit 2
            }
        done
        ;;
    *)
        echo "MIGRATION_MODE must be live or cold" >&2
        exit 2
        ;;
esac
if [[ "$MIGRATION_MODE" == cold && "$INJECT_RESTORE_FAILURE" == "1" ]]; then
    echo "INJECT_RESTORE_FAILURE is supported only for live migration" >&2
    exit 2
fi
test_sshs=("$SOURCE_SSH")
for target in "${migration_targets[@]}"; do
    [[ "$target" == *=* ]] || {
        echo "MIGRATION_TARGETS entries must use host=ssh syntax" >&2
        exit 2
    }
    test_sshs+=("${target#*=}")
done

remote() {
    local host=$1
    shift
    "${SSH[@]}" "$host" "$@"
}

clear_restore_failpoint() {
    local host=$1
    local _
    for _ in {1..10}; do
        if remote "$host" \
                "podman exec incus umount /usr/local/sbin/criu" \
                >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    # A failed CRIU exec can retain a transient executable reference. A lazy
    # detach removes the test mount from the namespace immediately and lets
    # the kernel release the old reference when that process exits.
    remote "$host" \
        "podman exec incus umount -l /usr/local/sbin/criu" \
        >/dev/null 2>&1
}

assert_fails() {
    local description=$1
    shift
    if "$@"; then
        echo "Expected failure: $description" >&2
        return 1
    fi
}

if [[ -n "$CONTROLLER_SSH" ]]; then
    openstack() {
        local command_line
        printf -v command_line '%q ' "$@"
        "${SSH[@]}" "$CONTROLLER_SSH" \
            "source $CONTROLLER_OPENRC >/dev/null && openstack $command_line"
    }
fi

incus_remote() {
    local host=$1
    shift
    local command_line
    printf -v command_line '%q ' incus --project "$INCUS_PROJECT" "$@"
    "${SSH[@]}" "$host" "$command_line"
}

managed_root_image_id() {
    local host=$1 command_line
    printf -v command_line \
        'rbd --id %q --pool %q info %q --format json | jq -er .id' \
        "$managed_root_ceph_user" "$managed_root_ceph_pool" \
        "$managed_root_rbd_image"
    remote "$host" "$command_line"
}

managed_root_pool_identity() {
    local host=$1 command_line
    printf -v command_line \
        'rados --id %q df --format json | jq -er --arg pool %q %q' \
        "$managed_root_ceph_user" "$managed_root_ceph_pool" \
        '.pools[] | select(.name == $pool) | .id'
    remote "$host" "$command_line"
}

managed_root_exact_mapping_count() {
    local host=$1 command_line python_program
    python_program='import json,pathlib,sys
pool,image,image_id,pool_id=sys.argv[1:]
count=0
for row in json.load(sys.stdin):
    if row.get("pool") != pool or row.get("name") != image:
        continue
    root=pathlib.Path("/sys/bus/rbd/devices") / str(row["id"])
    observed_image=(root / "image_id").read_text().strip()
    observed_pool=(root / "pool_id").read_text().strip()
    if observed_image == image_id and observed_pool == pool_id:
        count += 1
print(count)'
    printf -v command_line \
        'set -o pipefail; rbd --id %q device list --format json | python3 -c %q %q %q %q %q' \
        "$managed_root_ceph_user" "$python_program" \
        "$managed_root_ceph_pool" "$managed_root_rbd_image" \
        "$managed_root_rbd_id" "$managed_root_pool_id"
    remote "$host" "$command_line"
}

managed_root_exact_watcher_count() {
    local host=$1 command_line
    printf -v command_line \
        "set -euo pipefail; rados --id %q --pool %q listwatchers %q | awk 'NF {count++} END {print count + 0}'" \
        "$managed_root_ceph_user" "$managed_root_ceph_pool" \
        "rbd_header.$managed_root_rbd_id"
    remote "$host" "$command_line"
}

managed_root_exact_object_exists() {
    local host=$1 command_line
    printf -v command_line '%q ' rados --id "$managed_root_ceph_user" \
        --pool "$managed_root_ceph_pool" stat \
        "rbd_header.$managed_root_rbd_id"
    remote "$host" "$command_line" >/dev/null
}

assert_managed_root_owner() {
    local active_ssh=$1 inactive_ssh=$2
    local active_count inactive_count watcher_count current_id current_pool_id

    [[ -n "$managed_root_rbd_id" ]] || return 0
    current_id=$(managed_root_image_id "$active_ssh")
    current_pool_id=$(managed_root_pool_identity "$active_ssh")
    [[ "$current_id" == "$managed_root_rbd_id" ]] || {
        echo "Managed root image ID changed: $current_id != $managed_root_rbd_id" >&2
        return 1
    }
    [[ "$current_pool_id" == "$managed_root_pool_id" ]] || {
        echo "Managed root pool ID changed: $current_pool_id != $managed_root_pool_id" >&2
        return 1
    }

    active_count=$(managed_root_exact_mapping_count "$active_ssh")
    inactive_count=$(managed_root_exact_mapping_count "$inactive_ssh")
    watcher_count=$(managed_root_exact_watcher_count "$active_ssh")
    [[ "$active_count" == 1 ]] || {
        echo "Active host has $active_count exact managed-root mappings" >&2
        return 1
    }
    [[ "$inactive_count" == 0 ]] || {
        echo "Inactive host retained $inactive_count exact managed-root mappings" >&2
        return 1
    }
    ((watcher_count <= 1)) || {
        echo "Managed root has $watcher_count exact Ceph watchers" >&2
        return 1
    }
}

wait_status() {
    local expected=$1
    local deadline=$((SECONDS + TIMEOUT))
    local current
    while ((SECONDS < deadline)); do
        current=$(openstack server show "$server_id" -f value -c status \
            2>/dev/null || true)
        [[ "$current" == "$expected" ]] && return 0
        [[ "$current" == "ERROR" ]] && break
        sleep 2
    done
    openstack server show "$server_id" || true
    echo "Server did not reach $expected (current: ${current:-missing})" >&2
    return 1
}

wait_host() {
    local expected=$1
    local deadline=$((SECONDS + TIMEOUT))
    local current
    while ((SECONDS < deadline)); do
        current=$(openstack server show "$server_id" -f value \
            -c OS-EXT-SRV-ATTR:host 2>/dev/null || true)
        [[ "$current" == "$expected" ]] && return 0
        sleep 2
    done
    echo "Server did not move to $expected (current: ${current:-missing})" >&2
    return 1
}

wait_migration() {
    local deadline=$((SECONDS + TIMEOUT))
    local status
    while ((SECONDS < deadline)); do
        status=$(openstack server migration list --server "$server_id" \
            -f value -c Status 2>/dev/null | head -n1 || true)
        case "${status,,}" in
            completed)
                return 0
                ;;
            failed|error)
                openstack server migration list --server "$server_id" \
                    || true
                echo "Live migration failed (status: $status)" >&2
                return 1
                ;;
        esac
        sleep 2
    done
    echo "Live migration status timed out (current: ${status:-missing})" >&2
    return 1
}

share_api() {
    local method=$1 url=$2 data=${3:-}
    local args=(-fsS -X "$method" -H "X-Auth-Token: $token"
        -H "OpenStack-API-Version: compute 2.97")
    if [[ -n "$data" ]]; then
        args+=(-H "Content-Type: application/json" -d "$data")
    fi
    curl "${args[@]}" "$url"
}

wait_share_status() {
    local share_id=$1 expected=$2
    local deadline=$((SECONDS + TIMEOUT))
    local body
    while ((SECONDS < deadline)); do
        body=$(share_api GET "$shares_url")
        if python3 -c \
                'import json,sys
data=json.load(sys.stdin)
share_id,expected=sys.argv[1:]
raise SystemExit(0 if any(
    item["share_id"] == share_id and item["status"] == expected
    for item in data["shares"]) else 1)' \
                "$share_id" "$expected" <<<"$body"; then
            return 0
        fi
        sleep 2
    done
    echo "Manila share did not reach $expected: $body" >&2
    return 1
}

wait_share_absent() {
    local share_id=$1
    local deadline=$((SECONDS + TIMEOUT))
    while ((SECONDS < deadline)); do
        if ! share_api GET "$shares_url" | grep -Fq "$share_id"; then
            return 0
        fi
        sleep 2
    done
    echo "Manila share mapping did not disappear: $share_id" >&2
    return 1
}

assert_no_ovs_port() {
    local host=$1
    ! remote "$host" \
        "ovs-vsctl --data=bare --no-heading --columns=name find Interface \
         external_ids:iface-id='$port_id'" | grep -q .
}

diagnose() {
    openstack server show "$server_id" 2>/dev/null || true
    openstack server event list "$server_id" 2>/dev/null || true
    if [[ -n "$instance_name" ]]; then
        local host
        for host in "${test_sshs[@]}"; do
            incus_remote "$host" info "$instance_name" 2>/dev/null || true
        done
    fi
}

on_error() {
    local rc=$?
    local line=$1
    local command=$2
    printf 'FAIL rc=%s line=%s command=%s\n' \
        "$rc" "$line" "$command" >&2
    return "$rc"
}

cleanup() {
    local rc=$?
    # EXIT cleanup must continue after a diagnostic or best-effort deletion
    # fails; errexit would otherwise strand the very resources being audited.
    set +e
    if [[ -n "$restore_failpoint_ssh" ]]; then
        clear_restore_failpoint "$restore_failpoint_ssh" || true
        restore_failpoint_ssh=
    fi
    if [[ -n "$user_data" ]]; then
        rm -f "$user_data"
    fi
    ((rc == 0)) || diagnose
    if ((rc != 0)) && [[ "$KEEP_FAILED" == "1" ]]; then
        printf 'Keeping failed resources for diagnosis: server=%s instance=%s port=%s\n' \
            "${server_id:-unset}" "${instance_name:-unset}" \
            "${port_id:-unset}" >&2
        return "$rc"
    fi
    if [[ -n "$server_id" ]]; then
        if ((${#share_ids[@]} > 0)) && [[ -n "$shares_url" ]]; then
            openstack server stop "$server_id" >/dev/null 2>&1 || true
            local share_id
            for share_id in "${share_ids[@]}"; do
                share_api DELETE "$shares_url/$share_id" \
                    >/dev/null 2>&1 || true
            done
        fi
        openstack server delete --wait "$server_id" >/dev/null 2>&1 || true
    fi
    local volume_id volume_status deadline
    for volume_id in "${volume_ids[@]}"; do
        deadline=$((SECONDS + 60))
        while volume_status=$(openstack volume show "$volume_id" \
                -f value -c status 2>/dev/null); do
            [[ "$volume_status" == "available" || "$volume_status" == "error" ]] && break
            ((SECONDS >= deadline)) && break
            sleep 2
        done
        openstack volume delete "$volume_id" >/dev/null 2>&1 || true
    done
    if [[ -n "$root_volume_id" ]]; then
        deadline=$((SECONDS + 60))
        while volume_status=$(openstack volume show "$root_volume_id" \
                -f value -c status 2>/dev/null); do
            [[ "$volume_status" == "available" || "$volume_status" == "error" ]] && break
            ((SECONDS >= deadline)) && break
            sleep 2
        done
        openstack volume delete "$root_volume_id" >/dev/null 2>&1 || true
    fi
    if [[ -n "$instance_name" ]]; then
        local host observed_uuid profile_uuid
        for host in "${test_sshs[@]}"; do
            observed_uuid=$(incus_remote "$host" config get \
                "$instance_name" user.openstack.uuid 2>/dev/null || true)
            if [[ -n "$observed_uuid" && "$observed_uuid" == "$server_id" ]]; then
                incus_remote "$host" delete "$instance_name" --force \
                    >/dev/null 2>&1 || true
            elif incus_remote "$host" info "$instance_name" \
                    >/dev/null 2>&1; then
                echo "Refusing cleanup of $host/$instance_name: " \
                    "Nova UUID is ${observed_uuid:-missing}, expected $server_id" >&2
            fi

            profile_uuid=$(incus_remote "$host" profile get \
                "$instance_name" user.openstack.uuid 2>/dev/null || true)
            if [[ -n "$profile_uuid" && "$profile_uuid" == "$server_id" ]]; then
                incus_remote "$host" profile delete "$instance_name" \
                    >/dev/null 2>&1 || true
            elif incus_remote "$host" profile show "$instance_name" \
                    >/dev/null 2>&1; then
                echo "Refusing cleanup of $host profile $instance_name: " \
                    "Nova UUID is ${profile_uuid:-missing}, expected $server_id" >&2
            fi
        done
    fi
    return "$rc"
}
trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for host in "${test_sshs[@]}"; do
    server_extensions=$(remote "$host" incus query /1.0)
    if [[ "$MIGRATION_MODE" == live ]]; then
        grep -q migration_stateful_shifted_root <<<"$server_extensions"
        if [[ "$BOOT_FROM_VOLUME" == "1" ]]; then
            grep -q migration_live_shared_cephext_storage \
                <<<"$server_extensions"
        fi
        # Recover from a test process killed before its EXIT trap could remove
        # the restore failpoint. On an unmodified mount this returns EINVAL.
        clear_restore_failpoint "$host" || true
        remote "$host" "podman exec incus criu check --extra" >/dev/null
    elif [[ "$BOOT_FROM_VOLUME" == "1" ]]; then
        for extension in storage_driver_cephext \
                migration_shared_ceph_storage \
                instance_storage_handover_proof \
                migration_shared_ceph_storage_ready_fence; do
            grep -q "$extension" <<<"$server_extensions"
        done
    fi
done

user_data=$(mktemp)
cat >"$user_data" <<'EOF'
#!/bin/sh
cat >/usr/local/bin/criu-counter-loop <<'SCRIPT'
#!/bin/sh
echo $$ >/run/criu-counter.pid
trap 'exit 0' TERM INT
echo $$ >/run/criu-counter.pid
while :; do
    n=$(cat /root/criu-counter 2>/dev/null || echo 0)
    echo $((n + 1)) >/root/criu-counter
    sleep 1
done
SCRIPT
chmod 0755 /usr/local/bin/criu-counter-loop
if command -v rc-update >/dev/null 2>&1; then
cat >/etc/init.d/criu-counter <<'SCRIPT'
#!/sbin/openrc-run
name="CRIU live migration E2E counter"
command="/usr/local/bin/criu-counter-loop"
command_background="yes"
pidfile="/run/criu-counter.pid"
output_log="/var/log/criu-counter.log"
error_log="/var/log/criu-counter.err"
depend() {
    before networking
}
SCRIPT
chmod 0755 /etc/init.d/criu-counter
rc-update add criu-counter boot
rc-service criu-counter start
else
cat >/etc/systemd/system/criu-counter.service <<'UNIT'
[Unit]
Description=CRIU live migration E2E counter

[Service]
ExecStart=/usr/local/bin/criu-counter-loop
Restart=always

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now criu-counter.service
fi
echo ready >/root/criu-e2e-ready
EOF

if [[ "$BOOT_FROM_VOLUME" == "1" ]]; then
    root_volume_id=$(openstack volume create \
        --type "$ROOT_VOLUME_TYPE" --size "$ROOT_VOLUME_SIZE" \
        --image "$IMAGE" --bootable -f value -c id "${SERVER}-root")
    deadline=$((SECONDS + TIMEOUT))
    until [[ "$(openstack volume show "$root_volume_id" \
            -f value -c status)" == "available" ]]; do
        ((SECONDS < deadline)) || {
            echo "Cinder BFV root creation timed out" >&2
            exit 1
        }
        sleep 2
    done
    server_id=$(openstack --os-compute-api-version 2.74 server create \
        --flavor "$FLAVOR" --volume "$root_volume_id" --network "$NETWORK" \
        --host "$SOURCE_HOST" --user-data "$user_data" \
        -f value -c id "$SERVER")
else
    server_id=$(openstack --os-compute-api-version 2.74 server create \
        --flavor "$FLAVOR" --image "$IMAGE" --network "$NETWORK" \
        --host "$SOURCE_HOST" --user-data "$user_data" \
        -f value -c id "$SERVER")
fi
wait_status ACTIVE
wait_host "$SOURCE_HOST"
instance_name=$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)
port_id=$(openstack port list --server "$server_id" -f value -c ID)
fixed_ip=$(openstack port show "$port_id" -f json -c fixed_ips |
    python3 -c 'import ast,json,sys; v=json.load(sys.stdin)["fixed_ips"]; v=ast.literal_eval(v) if isinstance(v,str) else v; print(v[0]["ip_address"])')

deadline=$((SECONDS + TIMEOUT))
until incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
        test -f /root/criu-e2e-ready; do
    ((SECONDS < deadline)) || {
        echo "Guest bootstrap timed out" >&2
        exit 1
    }
    sleep 2
done

root_marker="INCUS_${MIGRATION_MODE^^}_ROOT_${RANDOM}_$(date +%s)"
incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
    sh -c "printf '%s' '$root_marker' >/root/incus-migration-e2e-marker; sync"
[[ "$(incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
    cat /root/incus-migration-e2e-marker)" == "$root_marker" ]]

if [[ "$BOOT_FROM_VOLUME" != "1" ]]; then
    managed_root_pool=$(incus_remote "$SOURCE_SSH" profile device get \
        "$instance_name" root pool)
    managed_root_driver=$(incus_remote "$SOURCE_SSH" storage list \
        --format csv | awk -F, -v pool="$managed_root_pool" \
        '$1 == pool {print $2}')
    if [[ "$managed_root_driver" == ceph ]]; then
        managed_root_ceph_pool=$(incus_remote "$SOURCE_SSH" storage get \
            "$managed_root_pool" ceph.osd.pool_name)
        if [[ -z "$managed_root_ceph_pool" ]]; then
            managed_root_ceph_pool=$(incus_remote "$SOURCE_SSH" storage get \
                "$managed_root_pool" source)
        fi
        managed_root_ceph_user=$(incus_remote "$SOURCE_SSH" storage get \
            "$managed_root_pool" ceph.user.name)
        managed_root_ceph_user=${managed_root_ceph_user:-admin}
        managed_root_rbd_image="container_${INCUS_PROJECT}_${instance_name}"
        managed_root_rbd_id=$(managed_root_image_id "$SOURCE_SSH")
        managed_root_pool_id=$(managed_root_pool_identity "$SOURCE_SSH")
        [[ -n "$managed_root_rbd_id" &&
           "$managed_root_pool_id" =~ ^[0-9]+$ ]]
    elif [[ "$REQUIRE_MANAGED_CEPH_ROOT" == "1" ]]; then
        echo "Instance root pool $managed_root_pool uses " \
            "${managed_root_driver:-an unknown driver}, expected ceph" >&2
        exit 1
    fi
fi

if [[ -n "$managed_root_rbd_id" ]]; then
    for host in "${test_sshs[@]}"; do
        server_extensions=$(remote "$host" incus query /1.0)
        required_extensions=(
            storage_materialization_attempt_v1
            storage_release_receipt_v2
            migration_shared_ceph_storage_ready_fence
        )
        if [[ "$MIGRATION_MODE" == live ]]; then
            required_extensions+=(migration_live_shared_ceph_storage)
        else
            required_extensions+=(migration_shared_ceph_storage)
        fi
        for extension in "${required_extensions[@]}"; do
            grep -Fq "\"$extension\"" <<<"$server_extensions" || {
                echo "$host does not advertise required extension $extension" >&2
                exit 1
            }
        done
    done
    assert_managed_root_owner \
        "$SOURCE_SSH" "${migration_targets[0]#*=}"
fi

if ((${#requested_manila_shares[@]} > 0)); then
    if ((${#requested_manila_tags[@]} > 0 &&
         ${#requested_manila_tags[@]} != ${#requested_manila_shares[@]})); then
        echo "MANILA_TAGS must be empty or match MANILA_SHARES count" >&2
        exit 2
    fi
    token=$(openstack token issue -f value -c id)
    endpoint=$(openstack endpoint list --service nova --interface public \
        -f value -c URL | head -n1)
    project_id=$(openstack server show "$server_id" -f value -c project_id)
    endpoint=${endpoint//\%\(project_id\)s/$project_id}
    shares_url="$endpoint/servers/$server_id/shares"

    openstack server stop "$server_id"
    wait_status SHUTOFF
    for index in "${!requested_manila_shares[@]}"; do
        share_ref=${requested_manila_shares[index]}
        share_id=$(openstack share show "$share_ref" -f value -c id)
        if ((${#requested_manila_tags[@]} > 0)); then
            share_tag=${requested_manila_tags[index]}
        elif ((${#requested_manila_shares[@]} == 1)); then
            share_tag=$MANILA_TAG
        else
            share_tag="${MANILA_TAG}-$((index + 1))"
        fi
        [[ " ${share_ids[*]} " != *" $share_id "* ]] || {
            echo "MANILA_SHARES contains duplicate share: $share_ref" >&2
            exit 2
        }
        [[ " ${share_tags[*]} " != *" $share_tag "* ]] || {
            echo "Manila guest tags must be unique: $share_tag" >&2
            exit 2
        }
        share_ids+=("$share_id")
        share_tags+=("$share_tag")
        share_api POST "$shares_url" \
            "{\"share\":{\"share_id\":\"$share_id\",\"tag\":\"$share_tag\"}}" \
            >/dev/null
        wait_share_status "$share_id" inactive
    done
    openstack server start "$server_id"
    wait_status ACTIVE

    deadline=$((SECONDS + TIMEOUT))
    until incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
            test -s /run/criu-counter.pid; do
        ((SECONDS < deadline)) || {
            echo "CRIU counter did not restart after Manila staging" >&2
            exit 1
        }
        sleep 2
    done
    for index in "${!share_ids[@]}"; do
        share_id=${share_ids[index]}
        share_tag=${share_tags[index]}
        share_mount="/opt/stack/data/nova/instances/incus-shares/$server_id/$share_id"
        share_mounts+=("$share_mount")
        until incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
                grep -Fq "/mnt/manila/$share_tag" /proc/self/mountinfo; do
            ((SECONDS < deadline)) || {
                echo "Manila share did not appear: $share_id" >&2
                exit 1
            }
            sleep 2
        done
        manila_marker="MANILA_LIVE_${index}_${RANDOM}_$(date +%s)"
        manila_marker_path="/mnt/manila/$share_tag/live-marker-$server_id"
        share_markers+=("$manila_marker")
        manila_marker_paths+=("$manila_marker_path")
        until incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
                sh -c "printf '%s' '$manila_marker' >'$manila_marker_path'"; do
            ((SECONDS < deadline)) || {
                echo "Manila share was mounted but not writable: $share_id" >&2
                exit 1
            }
            sleep 2
        done
        [[ "$(incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
            cat "$manila_marker_path")" == "$manila_marker" ]]
        remote "$SOURCE_SSH" findmnt -rn "$share_mount" >/dev/null
    done
fi

if [[ "$WITH_DATA_VOLUME" == "1" ]]; then
    [[ "$DATA_VOLUME_COUNT" =~ ^[1-9][0-9]*$ ]]
    for ((index = 0; index < DATA_VOLUME_COUNT; index++)); do
        volume_id=$(openstack volume create \
            --type "$DATA_VOLUME_TYPE" --size "$DATA_VOLUME_SIZE" \
            -f value -c id "${SERVER}-data-$((index + 1))")
        volume_ids+=("$volume_id")
        if [[ -n "${requested_data_devices[index]:-}" ]]; then
            openstack server add volume \
                --device "${requested_data_devices[index]}" \
                "$server_id" "$volume_id"
        else
            openstack server add volume "$server_id" "$volume_id"
        fi
        deadline=$((SECONDS + TIMEOUT))
        until [[ "$(openstack volume show "$volume_id" \
                -f value -c status)" == "in-use" ]]; do
            ((SECONDS < deadline)) || {
                echo "Cinder data volume attachment timed out: $volume_id" \
                    >&2
                exit 1
            }
            sleep 2
        done
        # Nova may normalize a requested virtio-style name to its canonical
        # SCSI BDM name. Always use the attachment record as the authority.
        data_device=$(openstack server volume list "$server_id" -f json |
            python3 -c 'import json,sys
volume_id=sys.argv[1]
rows=json.load(sys.stdin)
print(next(row["Device"] for row in rows
           if row["Volume ID"] == volume_id))' "$volume_id")
        volume_devices+=("$data_device")
        volume_marker="INCUS_LIVE_VOLUME_${index}_${RANDOM}_$(date +%s)"
        volume_markers+=("$volume_marker")
        until incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
                test -b "$data_device"; do
            ((SECONDS < deadline)) || {
                echo "Cinder data device did not appear: $data_device" >&2
                exit 1
            }
            sleep 2
        done
        incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
            sh -c "printf '%s' '$volume_marker' | \
                dd of='$data_device' bs=1 conv=fsync status=none"
    done
fi

source_pid=$(incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
    cat /run/criu-counter.pid)
source_counter=$(incus_remote "$SOURCE_SSH" exec "$instance_name" -- \
    cat /root/criu-counter)
[[ "$source_pid" =~ ^[0-9]+$ ]]
[[ "$source_counter" =~ ^[0-9]+$ ]]

current_host=$SOURCE_HOST
current_ssh=$SOURCE_SSH
current_counter=$source_counter
later_counter=$source_counter

inject_and_verify_restore_rollback() {
    local target_host=$1 target_ssh=$2
    local rollback_pid rollback_counter index volume_id image_name
    local deadline status share_id share_mount injection_since
    local source_migration_log target_migration_log

    injection_since=$(( $(date +%s) - 2 ))
    restore_failpoint_ssh=$target_ssh
    remote "$target_ssh" \
        "podman exec incus mount --bind /bin/false /usr/local/sbin/criu"
    # OSC can return zero when Nova accepted the request and subsequently
    # rolled it back. The migration record, not the client exit status, is
    # authoritative for the injected failure.
    openstack server migrate --live-migration --host "$target_host" \
        --wait "$server_id" || true
    clear_restore_failpoint "$target_ssh"
    restore_failpoint_ssh=
    deadline=$((SECONDS + TIMEOUT))
    while ((SECONDS < deadline)); do
        status=$(openstack server migration list --server "$server_id" \
            -f value -c Status 2>/dev/null | head -n1 || true)
        [[ "${status,,}" == failed || "${status,,}" == error ]] && break
        sleep 2
    done
    [[ "${status,,}" == failed || "${status,,}" == error ]]

    source_migration_log=$(remote "$current_ssh" journalctl \
        -u incus-podman.service --since "@$injection_since" --no-pager)
    target_migration_log=$(remote "$target_ssh" journalctl \
        -u incus-podman.service --since "@$injection_since" --no-pager)
    grep -Fq "Failed migration on target" <<<"$target_migration_log"
    # The source logs the target's restore failure after it is propagated over
    # the migration control channel.  This is expected and proves that the
    # source did not independently fail before the target attempted restore.
    grep -F "Failed migration on source" <<<"$source_migration_log" |
        grep -Fq "Error from migration control target"
    if grep -Fq "Failed reading migration index header" \
            <<<"$target_migration_log"; then
        echo "Failure injection did not reach target CRIU restore" >&2
        return 1
    fi

    wait_status ACTIVE
    wait_host "$current_host"
    [[ "$(incus_remote "$current_ssh" list "$instance_name" \
        --format csv -c s)" == RUNNING ]]
    assert_fails "target instance must be absent after rollback" \
        incus_remote "$target_ssh" info "$instance_name" >/dev/null 2>&1

    rollback_pid=$(incus_remote "$current_ssh" exec "$instance_name" -- \
        cat /run/criu-counter.pid)
    rollback_counter=$(incus_remote "$current_ssh" exec "$instance_name" -- \
        cat /root/criu-counter)
    [[ "$rollback_pid" == "$source_pid" ]]
    ((rollback_counter > current_counter))
    current_counter=$rollback_counter

    [[ "$(openstack port show "$port_id" -f value \
        -c binding_host_id)" == "$current_host" ]]
    [[ "$(openstack port show "$port_id" -f value -c status)" == ACTIVE ]]
    assert_no_ovs_port "$target_ssh"

    if [[ "$BOOT_FROM_VOLUME" == "1" ]]; then
        image_name="volume-$root_volume_id"
        remote "$current_ssh" \
            "rbd device list --format json --id cinder | jq -e \
             '.[] | select(.name == \"$image_name\")'" >/dev/null
        assert_fails "target BFV mapping must be absent after rollback" \
            remote "$target_ssh" \
            "rbd device list --format json --id cinder | jq -e \
             '.[] | select(.name == \"$image_name\")'" >/dev/null
    fi
    for volume_id in "${volume_ids[@]}"; do
        image_name="volume-$volume_id"
        remote "$current_ssh" \
            "rbd device list --format json --id cinder | jq -e \
             '.[] | select(.name == \"$image_name\")'" >/dev/null
        assert_fails "target data-volume mapping must be absent after rollback" \
            remote "$target_ssh" \
            "rbd device list --format json --id cinder | jq -e \
             '.[] | select(.name == \"$image_name\")'" >/dev/null
    done
    for index in "${!share_ids[@]}"; do
        share_id=${share_ids[index]}
        share_mount=${share_mounts[index]}
        remote "$current_ssh" findmnt -rn "$share_mount" >/dev/null
        assert_fails "target Manila staging mount must be absent after rollback" \
            remote "$target_ssh" findmnt -rn "$share_mount" >/dev/null
    done
    assert_managed_root_owner "$current_ssh" "$target_ssh"
    echo "PASS injected live-restore failure rolled back to $current_host"
}

wait_incus_absent() {
    local host=$1 deadline=$((SECONDS + TIMEOUT))
    local names
    while true; do
        names=$(incus_remote "$host" list --format csv -c n)
        ! grep -Fqx -- "$instance_name" <<<"$names" && return 0
        ((SECONDS < deadline)) || {
            echo "Incus instance remained on $host after migration action" >&2
            return 1
        }
        sleep 2
    done
}

wait_profile_absent() {
    local host=$1 deadline=$((SECONDS + TIMEOUT))
    local names
    while true; do
        names=$(incus_remote "$host" profile list --format csv -c n)
        ! grep -Fqx -- "$instance_name" <<<"$names" && return 0
        ((SECONDS < deadline)) || {
            echo "Incus profile remained on $host after cleanup" >&2
            return 1
        }
        sleep 2
    done
}

assert_rbd_mapping_owner() {
    local active_ssh=$1 inactive_ssh=$2 image_name=$3
    local deadline=$((SECONDS + TIMEOUT)) mappings

    remote "$active_ssh" \
        "rbd device list --format json --id cinder | jq -e \
         '.[] | select(.name == \"$image_name\")'" >/dev/null
    while true; do
        mappings=$(remote "$inactive_ssh" \
            "rbd device list --format json --id cinder | jq -r \
             '.[] | select(.name == \"$image_name\") | .device'")
        [[ -z "$mappings" ]] && return 0
        ((SECONDS < deadline)) || {
            echo "Inactive host retained RBD mapping $image_name" >&2
            return 1
        }
        sleep 2
    done
}

wait_mount_absent() {
    local host=$1 mount_path=$2 deadline=$((SECONDS + TIMEOUT))
    local mounts

    while true; do
        mounts=$(remote "$host" findmnt -rn -o TARGET)
        ! grep -Fqx -- "$mount_path" <<<"$mounts" && return 0
        ((SECONDS < deadline)) || {
            echo "Inactive host retained Manila mount $mount_path" >&2
            return 1
        }
        sleep 2
    done
}

wait_ovs_port_absent() {
    local host=$1 deadline=$((SECONDS + TIMEOUT)) interfaces

    while true; do
        interfaces=$(remote "$host" \
            "ovs-vsctl --data=bare --no-heading --columns=name \
             find Interface external_ids:iface-id='$port_id'")
        [[ -z "$interfaces" ]] && return 0
        ((SECONDS < deadline)) || {
            echo "Inactive host retained OVS port for $port_id" >&2
            return 1
        }
        sleep 2
    done
}

verify_guest_persistent_state() {
    local host=$1
    local index volume_id data_device volume_marker restored_marker
    local target_volume_source manila_marker_path manila_marker share_mount
    local restored_manila_marker

    [[ "$(incus_remote "$host" exec "$instance_name" -- \
        cat /root/incus-migration-e2e-marker)" == "$root_marker" ]]
    for index in "${!volume_ids[@]}"; do
        volume_id=${volume_ids[index]}
        data_device=${volume_devices[index]}
        volume_marker=${volume_markers[index]}
        [[ "$(openstack volume show "$volume_id" -f value -c status)" == \
            "in-use" ]]
        incus_remote "$host" exec "$instance_name" -- test -b "$data_device"
        restored_marker=$(incus_remote "$host" exec "$instance_name" -- \
            dd if="$data_device" bs=1 count="${#volume_marker}" status=none)
        [[ "$restored_marker" == "$volume_marker" ]]
        target_volume_source=$(incus_remote "$host" profile device get \
            "$instance_name" "$volume_id" source)
        [[ "$target_volume_source" == /dev/* ]]
        remote "$host" test -b "$target_volume_source"
    done
    for index in "${!share_ids[@]}"; do
        manila_marker_path=${manila_marker_paths[index]}
        manila_marker=${share_markers[index]}
        share_mount=${share_mounts[index]}
        restored_manila_marker=$(incus_remote "$host" \
            exec "$instance_name" -- cat "$manila_marker_path")
        [[ "$restored_manila_marker" == "$manila_marker" ]]
        remote "$host" findmnt -rn "$share_mount" >/dev/null
    done
}

verify_openstack_volume_attachments() {
    local expected=()
    local server_volumes attachments

    if [[ "$BOOT_FROM_VOLUME" == "1" ]]; then
        expected+=("$root_volume_id")
    fi
    expected+=("${volume_ids[@]}")
    server_volumes=$(openstack server volume list "$server_id" -f json)
    printf '%s' "$server_volumes" | python3 -c '
import json
import sys

server_id = sys.argv[1]
expected = sys.argv[2:]
rows = json.load(sys.stdin)
actual = [row["Volume ID"] for row in rows]
if len(actual) != len(set(actual)) or sorted(actual) != sorted(expected):
    raise SystemExit(
        "server {} volume set differs: actual={} expected={}".format(
            server_id, actual, expected))' "$server_id" "${expected[@]}"

    ((${#expected[@]} > 0)) || return 0
    attachments=$(openstack volume attachment list -f json \
        -c 'Volume ID' -c 'Server ID' -c Status)
    printf '%s' "$attachments" | python3 -c '
import json
import sys

server_id = sys.argv[1]
all_rows = json.load(sys.stdin)
for volume_id in sys.argv[2:]:
    rows = [
        row for row in all_rows
        if row["Volume ID"] == volume_id
    ]
    if len(rows) != 1:
        raise SystemExit(
            "volume {} attachment cardinality is {}".format(
                volume_id, len(rows)))
    row = rows[0]
    if row["Server ID"] != server_id or row["Status"].lower() != "attached":
        raise SystemExit(
            "volume {} attachment is not owned by {}: {}".format(
                volume_id, server_id, row))' "$server_id" "${expected[@]}"
}

verify_share_api_active() {
    local body

    ((${#share_ids[@]} > 0)) || return 0
    body=$(share_api GET "$shares_url")
    printf '%s' "$body" | python3 -c '
import json
import sys

expected = sys.argv[1:]
rows = json.load(sys.stdin)["shares"]
matches = [row for row in rows if row["share_id"] in expected]
if len(matches) != len(expected):
    raise SystemExit(
        "share mapping cardinality differs: actual={} expected={}".format(
            matches, expected))
if any(row["status"] != "active" for row in matches):
    raise SystemExit(
        "share mapping is not active: {}".format(matches))' "${share_ids[@]}"
}

assert_inactive_storage_absent() {
    local active_ssh=$1 inactive_ssh=$2
    local index volume_id share_mount

    if [[ "$BOOT_FROM_VOLUME" == "1" ]]; then
        assert_rbd_mapping_owner "$active_ssh" "$inactive_ssh" \
            "volume-$root_volume_id"
    fi
    for volume_id in "${volume_ids[@]}"; do
        assert_rbd_mapping_owner "$active_ssh" "$inactive_ssh" \
            "volume-$volume_id"
    done
    for index in "${!share_ids[@]}"; do
        share_mount=${share_mounts[index]}
        wait_mount_absent "$inactive_ssh" "$share_mount"
    done
}

verify_active_network() {
    local active_host=$1 active_ssh=$2
    local guest_iface ovs_iface

    # The driver names the guest NIC device nic<port-id-hex> truncated to
    # the kernel IFNAMSIZ budget; the in-guest interface carries that name.
    guest_iface="nic${port_id//-/}"
    guest_iface=${guest_iface:0:15}
    [[ -n "$guest_iface" ]]
    incus_remote "$active_ssh" exec "$instance_name" -- \
        ip link show "$guest_iface" >/dev/null
    incus_remote "$active_ssh" exec "$instance_name" -- \
        ip -o addr show "$guest_iface" | grep -Fq "$fixed_ip"
    ovs_iface=$(remote "$active_ssh" \
        "ovs-vsctl --data=bare --no-heading --columns=name find Interface \
         external_ids:iface-id='$port_id'")
    [[ -n "$ovs_iface" ]]
    [[ "$(remote "$active_ssh" \
        "ovs-vsctl get Interface '$ovs_iface' \
         external_ids:ovn-installed")" == '"true"' ]]
    [[ "$(openstack port show "$port_id" -f value -c binding_host_id)" == \
        "$active_host" ]]
    [[ "$(openstack port show "$port_id" -f value -c status)" == ACTIVE ]]
}

verify_network_owner() {
    local active_host=$1 active_ssh=$2 inactive_ssh=$3

    verify_active_network "$active_host" "$active_ssh"
    wait_ovs_port_absent "$inactive_ssh"
}

migrate_and_verify() {
    local target_host=$1 target_ssh=$2 action=${3:-confirm}
    local dest_pid dest_counter rollback_counter root_image volume_id

    if [[ "$MIGRATION_MODE" == live ]]; then
        openstack server migrate --live-migration --host "$target_host" \
            --wait "$server_id"
        wait_migration
        wait_status ACTIVE
    else
        openstack --os-compute-api-version 2.56 server migrate \
            --host "$target_host" --wait "$server_id"
        wait_status VERIFY_RESIZE
    fi
    wait_host "$target_host"

    if [[ "$MIGRATION_MODE" == live ]]; then
        wait_incus_absent "$current_ssh"
    else
        [[ "$(incus_remote "$current_ssh" list "$instance_name" \
            --format csv -c s)" == STOPPED ]]
    fi
    [[ "$(incus_remote "$target_ssh" list "$instance_name" \
        --format csv -c s)" == RUNNING ]]
    dest_pid=$(incus_remote "$target_ssh" exec "$instance_name" -- \
        cat /run/criu-counter.pid)
    dest_counter=$(incus_remote "$target_ssh" exec "$instance_name" -- \
        cat /root/criu-counter)
    [[ "$dest_pid" =~ ^[0-9]+$ ]]
    [[ "$dest_counter" =~ ^[0-9]+$ ]]
    if [[ "$MIGRATION_MODE" == live ]]; then
        [[ "$dest_pid" == "$source_pid" ]]
        ((dest_counter > current_counter))
    else
        ((dest_counter >= current_counter))
    fi

    if [[ "$BOOT_FROM_VOLUME" == "1" ]]; then
        [[ "$(openstack volume show "$root_volume_id" -f value -c status)" == \
            "in-use" ]]
        root_image="volume-$root_volume_id"
        remote "$target_ssh" \
            "rbd device list --format json --id cinder | jq -e \
             '.[] | select(.name == \"$root_image\")'" >/dev/null
        if [[ "$MIGRATION_MODE" == live ]]; then
            assert_fails "source BFV mapping must be absent after migration" \
                remote "$current_ssh" \
                "rbd device list --format json --id cinder | jq -e \
                 '.[] | select(.name == \"$root_image\")'" >/dev/null
        fi
    fi
    verify_guest_persistent_state "$target_ssh"
    verify_openstack_volume_attachments
    verify_share_api_active
    sleep 3
    later_counter=$(incus_remote "$target_ssh" exec "$instance_name" -- \
        cat /root/criu-counter)
    ((later_counter > dest_counter))
    verify_active_network "$target_host" "$target_ssh"

    # Cold migration retains the source until confirm/revert, including any
    # source-side OVS and Manila staging state. Only live migration can prove
    # the old endpoint absent before this point.
    if [[ "$MIGRATION_MODE" == live ]]; then
        assert_inactive_storage_absent "$target_ssh" "$current_ssh"
        verify_network_owner "$target_host" "$target_ssh" "$current_ssh"
        assert_managed_root_owner "$target_ssh" "$current_ssh"
        current_host=$target_host
        current_ssh=$target_ssh
        current_counter=$later_counter
        return
    fi

    # At VERIFY_RESIZE shared-root authority has already moved: the target owns
    # sole exact pool/image mapping and the stopped source owns none.
    assert_managed_root_owner "$target_ssh" "$current_ssh"

    # At VERIFY_RESIZE the target must already expose every persistent marker.
    # The action then decides which side is authoritative and which side must
    # be completely cleaned.
    case "$action" in
        confirm)
            openstack server resize confirm "$server_id"
            wait_status ACTIVE
            wait_host "$target_host"
            wait_incus_absent "$current_ssh"
            verify_guest_persistent_state "$target_ssh"
            verify_openstack_volume_attachments
            verify_share_api_active
            assert_inactive_storage_absent "$target_ssh" "$current_ssh"
            verify_network_owner "$target_host" "$target_ssh" "$current_ssh"
            assert_managed_root_owner "$target_ssh" "$current_ssh"
            current_host=$target_host
            current_ssh=$target_ssh
            current_counter=$later_counter
            echo "PASS cold-migration confirm target=$target_host"
            ;;
        revert)
            openstack server resize revert "$server_id"
            wait_status ACTIVE
            wait_host "$current_host"
            wait_incus_absent "$target_ssh"
            [[ "$(incus_remote "$current_ssh" list "$instance_name" \
                --format csv -c s)" == RUNNING ]]
            verify_guest_persistent_state "$current_ssh"
            verify_openstack_volume_attachments
            verify_share_api_active
            rollback_counter=$(incus_remote "$current_ssh" \
                exec "$instance_name" -- cat /root/criu-counter)
            [[ "$rollback_counter" =~ ^[0-9]+$ ]]
            ((rollback_counter >= current_counter))
            assert_inactive_storage_absent "$current_ssh" "$target_ssh"
            verify_network_owner "$current_host" "$current_ssh" "$target_ssh"
            assert_managed_root_owner "$current_ssh" "$target_ssh"
            current_counter=$rollback_counter
            later_counter=$rollback_counter
            echo "PASS cold-migration revert target=$target_host"
            ;;
    esac
}

if [[ "$INJECT_RESTORE_FAILURE" == "1" ]]; then
    inject_and_verify_restore_rollback \
        "${migration_targets[0]%%=*}" "${migration_targets[0]#*=}"
fi

for index in "${!migration_targets[@]}"; do
    target=${migration_targets[index]}
    migrate_and_verify "${target%%=*}" "${target#*=}" \
        "${migration_actions[index]:-confirm}"
done

rm -f "$user_data"
user_data=
if ((${#share_ids[@]} > 0)); then
    for manila_marker_path in "${manila_marker_paths[@]}"; do
        incus_remote "$current_ssh" exec "$instance_name" -- \
            rm -f "$manila_marker_path"
    done
    openstack server stop "$server_id"
    wait_status SHUTOFF
    for index in "${!share_ids[@]}"; do
        share_id=${share_ids[index]}
        share_mount=${share_mounts[index]}
        share_api DELETE "$shares_url/$share_id" >/dev/null
        wait_share_absent "$share_id"
        for host in "${test_sshs[@]}"; do
            assert_fails "Manila staging mount must be absent after detach" \
                remote "$host" findmnt -rn "$share_mount" >/dev/null
        done
    done
fi
openstack server delete --wait "$server_id"
for volume_id in "${volume_ids[@]}"; do
    deadline=$((SECONDS + TIMEOUT))
    until [[ "$(openstack volume show "$volume_id" -f value -c status)" == \
            "available" ]]; do
        ((SECONDS < deadline)) || {
            echo "Cinder data volume did not detach after server delete" >&2
            exit 1
        }
        sleep 2
    done
    openstack volume delete "$volume_id"
done
if [[ -n "$root_volume_id" ]]; then
    deadline=$((SECONDS + TIMEOUT))
    until [[ "$(openstack volume show "$root_volume_id" \
            -f value -c status)" == "available" ]]; do
        ((SECONDS < deadline)) || {
            echo "Cinder root volume did not detach after server delete" >&2
            exit 1
        }
        sleep 2
    done
    openstack volume delete "$root_volume_id"
fi
assert_fails "Neutron port must be absent after server delete" \
    openstack port show "$port_id" >/dev/null 2>&1
for host in "${test_sshs[@]}"; do
    wait_incus_absent "$host"
    wait_profile_absent "$host"
    wait_ovs_port_absent "$host"
done
if [[ -n "$managed_root_rbd_id" ]]; then
    assert_fails "exact managed Ceph root RBD must be absent after server delete" \
        managed_root_exact_object_exists "$SOURCE_SSH" >/dev/null 2>&1
fi

result="PASS mode=$MIGRATION_MODE server=$server_id instance=$instance_name ip=$fixed_ip pid=$source_pid counter=$source_counter->$later_counter hops=${#migration_targets[@]} actions=${MIGRATION_ACTIONS:-all-confirm} root_volume=${root_volume_id:-local} managed_root_pool_id=${managed_root_pool_id:-none} managed_root_rbd_id=${managed_root_rbd_id:-none} data_volumes=${volume_ids[*]:-none} manila_shares=${share_ids[*]:-none}"
server_id=
instance_name=
port_id=
root_volume_id=
volume_ids=()
share_ids=()
trap - EXIT
echo "$result"
