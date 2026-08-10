#!/usr/bin/env bash
# Fail-closed host readiness audit for a production Nova Incus compute.

set -uo pipefail

TARGET_OS_VERSION=${TARGET_OS_VERSION:-24.04}
TARGET_PYTHON_VERSION=${TARGET_PYTHON_VERSION:-3.12}
EXPECTED_INCUS_IMAGE=${EXPECTED_INCUS_IMAGE:-ghcr.io/fivetime/incus:alpine-novm}
EXPECTED_INCUS_IMAGE_DIGEST=${EXPECTED_INCUS_IMAGE_DIGEST:-}
EXPECTED_INCUS_REVISION=${EXPECTED_INCUS_REVISION:-}
EXPECTED_INCUS_LXCFS_IMAGE=${EXPECTED_INCUS_LXCFS_IMAGE:-$EXPECTED_INCUS_IMAGE}
EXPECTED_INCUS_LXCFS_IMAGE_DIGEST=${EXPECTED_INCUS_LXCFS_IMAGE_DIGEST:-$EXPECTED_INCUS_IMAGE_DIGEST}
EXPECTED_INCUS_LXCFS_REVISION=${EXPECTED_INCUS_LXCFS_REVISION:-$EXPECTED_INCUS_REVISION}
EXPECTED_INCUS_GROUP_MEMBERS=${EXPECTED_INCUS_GROUP_MEMBERS:-stack}
INCUS_CONTAINER=${INCUS_CONTAINER:-incus}
INCUS_LXCFS_CONTAINER=${INCUS_LXCFS_CONTAINER:-incus-lxcfs}
INCUS_SERVICE=${INCUS_SERVICE:-incus-podman.service}
INCUS_LXCFS_SERVICE=${INCUS_LXCFS_SERVICE:-incus-lxcfs.service}
INCUS_PROJECT=${INCUS_PROJECT:-nova}
INCUS_RUNTIME_ROOT=${INCUS_RUNTIME_ROOT:-/run/incus-podman}
INCUS_LXCFS_ROOT=${INCUS_LXCFS_ROOT:-/var/lib/lxcfs}
NOVA_SERVICE=${NOVA_SERVICE:-devstack@n-cpu.service}
NOVA_CONFIG=${NOVA_CONFIG:-/etc/nova/nova-cpu.conf}
INCUS_SHARE_MOUNT_ROOT=${INCUS_SHARE_MOUNT_ROOT:-/opt/stack/data/nova/instances/incus-shares}
PREFLIGHT_PROJECT=${PREFLIGHT_PROJECT:-nova-preflight}
MIN_FREE_PERCENT=${MIN_FREE_PERCENT:-20}
REQUIRE_COLD_MIGRATION=${REQUIRE_COLD_MIGRATION:-true}
REQUIRE_DEDICATED_CONTROL_FS=${REQUIRE_DEDICATED_CONTROL_FS:-true}

failures=0

pass() {
    printf 'PASS %-34s %s\n' "$1" "${2:-}"
}

fail() {
    printf 'FAIL %-34s %s\n' "$1" "$2" >&2
    failures=$((failures + 1))
}

check_equal() {
    local label=$1 expected=$2 actual=$3
    if [[ "$actual" == "$expected" ]]; then
        pass "$label" "$actual"
    else
        fail "$label" "expected=$expected actual=${actual:-missing}"
    fi
}

check_command() {
    if command -v "$1" >/dev/null 2>&1; then
        pass "command:$1" "$(command -v "$1")"
    else
        fail "command:$1" "not installed"
    fi
}

service_identity_can_read() {
    local path=$1 pid uid gid groups

    pid=$(systemctl show "$NOVA_SERVICE" -p MainPID --value 2>/dev/null)
    [[ "$pid" =~ ^[1-9][0-9]*$ && -r "/proc/$pid/status" ]] || return 1
    uid=$(awk '/^Uid:/ {print $2}' "/proc/$pid/status")
    gid=$(awk '/^Gid:/ {print $2}' "/proc/$pid/status")
    groups=$(awk '/^Groups:/ {$1=""; sub(/^ +/, ""); gsub(/ +/, ","); print}' \
        "/proc/$pid/status")
    if [[ -n "$groups" ]]; then
        setpriv --reuid="$uid" --regid="$gid" --groups="$groups" \
            test -r "$path"
    else
        setpriv --reuid="$uid" --regid="$gid" --clear-groups \
            test -r "$path"
    fi
}

is_ipv4() {
    local address=$1
    local -a octets
    local octet

    IFS=. read -r -a octets <<<"$address"
    if ((${#octets[@]} != 4)); then
        return 1
    fi

    for octet in "${octets[@]}"; do
        if [[ ! "$octet" =~ ^[0-9]+$ ]] ||
                ((10#$octet > 255)); then
            return 1
        fi
    done
}

is_tcp_port() {
    [[ "$1" =~ ^[0-9]+$ ]] && ((10#$1 >= 1 && 10#$1 <= 65535))
}

check_dedicated_fs() {
    local path=$1 root_source path_source usage free
    root_source=$(findmnt -nro SOURCE -T / 2>/dev/null)
    path_source=$(findmnt -nro SOURCE -T "$path" 2>/dev/null)
    usage=$(df -P "$path" 2>/dev/null | awk 'NR == 2 {gsub(/%/, "", $5); print $5}')
    free=$((100 - ${usage:-100}))
    if [[ "$REQUIRE_DEDICATED_CONTROL_FS" == true &&
          "$path_source" == "$root_source" ]]; then
        fail "filesystem:$path" "shares host root filesystem $root_source"
    elif ((free < MIN_FREE_PERCENT)); then
        fail "filesystem:$path" "only ${free}% free"
    else
        pass "filesystem:$path" "source=$path_source free=${free}%"
    fi
}

check_systemd_service() {
    local service=$1

    if systemctl is-active --quiet "$service"; then
        pass "$service" active
    else
        fail "$service" "not active"
    fi
    if systemctl is-enabled --quiet "$service"; then
        pass "$service enabled"
    else
        fail "$service enabled" "not enabled"
    fi
}

check_runtime_image() {
    local label=$1
    local container=$2
    local service=$3
    local expected_image=$4
    local expected_digest=$5
    local expected_revision=$6
    local expected_repository expected_pinned_image image_name image_revision
    local quadlet_image repo_digests

    image_name=$(podman inspect "$container" --format '{{.ImageName}}' \
        2>/dev/null)
    repo_digests=$(podman image inspect "$image_name" \
        --format '{{range .RepoDigests}}{{println .}}{{end}}' 2>/dev/null)
    expected_repository=${expected_image%@*}
    if [[ "${expected_repository##*/}" == *:* ]]; then
        expected_repository=${expected_repository%:*}
    fi
    if [[ -z "$expected_digest" ]]; then
        check_equal "$label image name" "$expected_image" "$image_name"
        fail "$label image digest pin" "set the expected image digest"
    else
        expected_pinned_image="${expected_repository}@${expected_digest}"
        check_equal "$label image name" "$expected_pinned_image" "$image_name"
    fi
    if [[ -n "$expected_digest" ]] && grep -Fqx \
            "$expected_pinned_image" <<<"$repo_digests"; then
        pass "$label image digest" "$expected_digest"
    elif [[ -n "$expected_digest" ]]; then
        fail "$label image digest" \
            "expected digest is absent from image RepoDigests"
    fi
    image_revision=$(podman image inspect "$image_name" \
        --format '{{index .Labels "org.opencontainers.image.revision"}}' \
        2>/dev/null)
    if [[ -z "$expected_revision" ]]; then
        fail "$label source revision" \
            "set the expected source revision (actual=$image_revision)"
    else
        check_equal "$label source revision" \
            "$expected_revision" "$image_revision"
    fi
    quadlet_image=$(systemctl cat "$service" 2>/dev/null |
        sed -n 's/^Image=//p' | tail -n1)
    if [[ "$quadlet_image" == *@sha256:* ]]; then
        pass "$label Quadlet immutable image" "$quadlet_image"
    else
        fail "$label Quadlet immutable image" \
            "Image= must use an immutable @sha256 reference"
    fi
}

if [[ $EUID -ne 0 ]]; then
    fail "execution user" "run as root"
else
    pass "execution user" root
fi

for command_name in awk podman jq findmnt crudini setpriv; do
    check_command "$command_name"
done

for container_command in \
        aa-exec apparmor_parser ceph incus incusd lxcfs rbd tar zfs zpool; do
    if podman exec "$INCUS_CONTAINER" sh -c \
            "command -v '$container_command'" >/dev/null 2>&1; then
        pass "container:$container_command"
    else
        fail "container:$container_command" "not installed in Incus image"
    fi
done

tar_help=$(podman exec "$INCUS_CONTAINER" tar --help 2>&1)
if grep -F -- "--no-unquote" <<<"$tar_help" >/dev/null; then
    pass "CRIU GNU tar support"
else
    fail "CRIU GNU tar support" \
        "container tar must support --no-unquote for tmpfs migration"
fi

# shellcheck disable=SC1091
source /etc/os-release
check_equal "operating system" Ubuntu "${NAME:-}"
check_equal "Ubuntu release" "$TARGET_OS_VERSION" "${VERSION_ID:-}"

python_actual=$(python3 -c \
    'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' \
    2>/dev/null)
check_equal "system Python" "$TARGET_PYTHON_VERSION" "$python_actual"

controllers=$(cat /sys/fs/cgroup/cgroup.controllers 2>/dev/null)
for controller in cpu io memory pids; do
    if grep -qw "$controller" <<<"$controllers"; then
        pass "cgroup v2:$controller"
    else
        fail "cgroup v2:$controller" "controller unavailable"
    fi
done
check_equal "AppArmor enabled" Y \
    "$(cat /sys/module/apparmor/parameters/enabled 2>/dev/null)"
if [[ -w /sys/kernel/security/apparmor/.load ]]; then
    pass "AppArmor policy loader" writable
else
    fail "AppArmor policy loader" \
        "/sys/kernel/security/apparmor/.load unavailable"
fi
check_equal "host core_pattern" /dev/null \
    "$(cat /proc/sys/kernel/core_pattern 2>/dev/null)"

socket_owner=$(stat -c '%U:%G' /var/lib/incus/unix.socket 2>/dev/null)
socket_mode=$(stat -c '%a' /var/lib/incus/unix.socket 2>/dev/null)
check_equal "Incus socket owner" root:incus-admin "$socket_owner"
check_equal "Incus socket mode" 660 "$socket_mode"

check_systemd_service "$INCUS_LXCFS_SERVICE"
check_systemd_service "$INCUS_SERVICE"
if systemctl is-active --quiet lxcfs.service; then
    fail "competing host lxcfs.service" \
        "disable and mask it; $INCUS_LXCFS_SERVICE must be the only owner"
else
    pass "competing host lxcfs.service" inactive
fi
if systemctl is-active --quiet "$NOVA_SERVICE"; then
    pass "$NOVA_SERVICE" active
else
    fail "$NOVA_SERVICE" "not active"
fi

check_runtime_image "Incus LXCFS" "$INCUS_LXCFS_CONTAINER" \
    "$INCUS_LXCFS_SERVICE" "$EXPECTED_INCUS_LXCFS_IMAGE" \
    "$EXPECTED_INCUS_LXCFS_IMAGE_DIGEST" "$EXPECTED_INCUS_LXCFS_REVISION"
check_runtime_image "Incus control" "$INCUS_CONTAINER" "$INCUS_SERVICE" \
    "$EXPECTED_INCUS_IMAGE" "$EXPECTED_INCUS_IMAGE_DIGEST" \
    "$EXPECTED_INCUS_REVISION"

for runtime in \
        "$INCUS_LXCFS_CONTAINER:lxcfs" \
        "$INCUS_CONTAINER:incusd"; do
    runtime_container=${runtime%%:*}
    runtime_role=${runtime#*:}
    if podman inspect "$runtime_container" \
            --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null |
            grep -Fqx "INCUS_RUNTIME_ROLE=$runtime_role"; then
        pass "runtime role:$runtime_container" "$runtime_role"
    else
        fail "runtime role:$runtime_container" \
            "expected INCUS_RUNTIME_ROLE=$runtime_role"
    fi
done

runtime_preserve=$(systemctl show "$INCUS_SERVICE" \
    -p RuntimeDirectoryPreserve --value 2>/dev/null)
check_equal "Incus runtime preservation" restart "$runtime_preserve"
if [[ -d "$INCUS_RUNTIME_ROOT" ]]; then
    pass "Incus runtime root" "$INCUS_RUNTIME_ROOT"
else
    fail "Incus runtime root" "$INCUS_RUNTIME_ROOT is missing"
fi
lxcfs_propagation=$(findmnt -nro PROPAGATION -T "$INCUS_LXCFS_ROOT" \
    2>/dev/null)
if [[ "$lxcfs_propagation" == shared ||
      "$lxcfs_propagation" == rshared ]]; then
    pass "LXCFS mount propagation" "$lxcfs_propagation"
else
    fail "LXCFS mount propagation" \
        "expected shared or rshared, actual=${lxcfs_propagation:-missing}"
fi
if timeout 5 head -n 1 "$INCUS_LXCFS_ROOT/proc/meminfo" \
        >/dev/null 2>&1; then
    pass "host LXCFS response" "$INCUS_LXCFS_ROOT/proc/meminfo"
else
    fail "host LXCFS response" \
        "$INCUS_LXCFS_ROOT/proc/meminfo is unavailable"
fi
if podman exec "$INCUS_LXCFS_CONTAINER" \
        /usr/local/sbin/healthcheck.sh >/dev/null 2>&1; then
    pass "LXCFS container health"
else
    fail "LXCFS container health" "role-specific health check failed"
fi
if podman exec "$INCUS_CONTAINER" \
        /usr/local/sbin/healthcheck.sh >/dev/null 2>&1; then
    pass "Incus container health"
else
    fail "Incus container health" "role-specific health check failed"
fi

network_inventory=$(podman exec "$INCUS_CONTAINER" incus network list \
    --all-projects --format csv -c emn 2>/dev/null)
if [[ $? -ne 0 ]]; then
    fail "managed Incus networks" "failed to query network inventory"
else
    managed_networks=$(awk -F, '$2 == "YES" {print $1 "/" $3}' \
        <<<"$network_inventory")
    if [[ -n "$managed_networks" ]]; then
        fail "managed Incus networks" \
            "unsupported on Neutron nodes: ${managed_networks//$'\n'/ }"
    else
        pass "managed Incus networks" none
    fi
fi

configured_incus_project=
incus_project_query=
incus_project_valid=false
if ! configured_incus_project=$(crudini --get "$NOVA_CONFIG" incus project \
        2>/dev/null) || [[ -z "$configured_incus_project" ]]; then
    fail "Nova Incus project authority" \
        "[incus] project must be explicitly configured in $NOVA_CONFIG"
elif [[ "$configured_incus_project" != "$INCUS_PROJECT" ]]; then
    fail "Nova Incus project authority" \
        "config=$configured_incus_project audit=$INCUS_PROJECT"
elif ! incus_project_query=$(jq -nr --arg value "$configured_incus_project" \
        '$value | @uri') || [[ -z "$incus_project_query" ]]; then
    fail "Nova Incus project authority" \
        "failed to encode configured project $configured_incus_project"
elif configured_project_json=$(podman exec "$INCUS_CONTAINER" incus query \
        "/1.0/projects/$incus_project_query" 2>/dev/null) &&
        jq -e 'type == "object"' <<<"$configured_project_json" >/dev/null; then
    incus_project_valid=true
    pass "Nova Incus project authority" "$configured_incus_project"
else
    fail "Nova Incus project authority" \
        "configured project $configured_incus_project is unavailable"
fi

nova_instance_inventory=
inventory_valid=false
if [[ "$incus_project_valid" != true ]]; then
    fail "Nova instance inventory" \
        "skipped because the configured Incus project is invalid"
elif nova_instance_inventory=$(podman exec "$INCUS_CONTAINER" incus query \
        "/1.0/instances?project=$incus_project_query&recursion=2" \
        2>/dev/null) &&
        jq -e 'type == "array" and all(.[]; type == "object")' \
            <<<"$nova_instance_inventory" >/dev/null; then
    inventory_valid=true
    pass "Nova instance inventory" "valid JSON object array"
else
    fail "Nova instance inventory" \
        "Incus query failed or returned a malformed JSON array"
fi

nova_profile_inventory=
profile_inventory_valid=false
if [[ "$incus_project_valid" != true ]]; then
    fail "Nova profile inventory" \
        "skipped because the configured Incus project is invalid"
elif nova_profile_inventory=$(podman exec "$INCUS_CONTAINER" incus query \
        "/1.0/profiles?project=$incus_project_query&recursion=1" \
        2>/dev/null) &&
        jq -e 'type == "array" and all(.[]; type == "object")' \
            <<<"$nova_profile_inventory" >/dev/null; then
    profile_inventory_valid=true
    pass "Nova profile inventory" "valid JSON object array"
else
    fail "Nova profile inventory" \
        "Incus query failed or returned a malformed JSON array"
fi

# The Nova Incus project is dedicated. Every instance must therefore carry a
# complete local/profile/expanded ownership chain; an absent marker is an
# integrity failure, not evidence that the record can be ignored.
if [[ "$inventory_valid" == true &&
      "$profile_inventory_valid" == true ]]; then
    if unsafe_owners=$(jq -r --slurpfile profiles \
        <(printf '%s\n' "$nova_profile_inventory") '
        .[] |
        . as $instance |
        .config["user.openstack.uuid"] as $owner |
        ($profiles[0] |
            map(select(.name == $instance.name))) as $named |
        select(
            if ($owner | type) != "string" then
                true
            elif ($owner | test(
                "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$") |
                not) then
                true
            elif ($named | length) != 1 then
                true
            else
                $named[0].config["environment.product_name"] !=
                    "OpenStack Nova" or
                $named[0].config["user.openstack.uuid"] != $owner or
                .expanded_config["environment.product_name"] !=
                    "OpenStack Nova" or
                .expanded_config["user.openstack.uuid"] != $owner
            end
        ) |
        .name
    ' <<<"$nova_instance_inventory"); then
        if [[ -z "$unsafe_owners" ]]; then
            pass "Nova instance ownership" "local/profile/expanded match"
        else
            fail "Nova instance ownership" \
                "invalid records: $(tr '\n' ',' <<<"$unsafe_owners")"
        fi
    else
        fail "Nova instance ownership" \
            "failed to evaluate instance/profile inventories"
    fi
fi

running_nova_instances=()
if [[ "$inventory_valid" == true ]]; then
    if running_inventory=$(jq -r '.[] |
        select(.status == "Running") |
        .name
    ' <<<"$nova_instance_inventory"); then
        if [[ -n "$running_inventory" ]]; then
            mapfile -t running_nova_instances <<<"$running_inventory"
        else
            pass "running guest LXCFS audit" "no running Nova instances"
        fi
    else
        fail "running guest LXCFS audit" \
            "failed to evaluate the validated Incus inventory"
    fi
fi
for instance_name in "${running_nova_instances[@]}"; do
    runtime_config="$INCUS_RUNTIME_ROOT/${INCUS_PROJECT}_${instance_name}/lxc.conf"
    if [[ -s "$runtime_config" ]]; then
        pass "guest runtime config:$instance_name" "$runtime_config"
    else
        fail "guest runtime config:$instance_name" \
            "$runtime_config is missing or empty"
    fi
    if podman exec "$INCUS_CONTAINER" incus --project "$INCUS_PROJECT" \
            exec "$instance_name" -- /bin/sh -c \
            'IFS= read -r line < /proc/meminfo && test -n "$line"' \
            >/dev/null 2>&1; then
        pass "guest LXCFS response:$instance_name"
    else
        fail "guest LXCFS response:$instance_name" \
            "/proc/meminfo is unreadable (possible stale FUSE mount)"
    fi
done

manila_enabled=$(crudini --get "$NOVA_CONFIG" incus enable_manila_shares \
    2>/dev/null || true)
if [[ "${manila_enabled,,}" == "true" ]]; then
    manila_access_cidr=$(crudini --get "$NOVA_CONFIG" DEFAULT \
        my_shared_fs_storage_ip 2>/dev/null || true)
    if [[ "$manila_access_cidr" == */* ]]; then
        pass "Manila compute access CIDR" "$manila_access_cidr"
    else
        fail "Manila compute access CIDR" \
            "[DEFAULT] my_shared_fs_storage_ip must be an isolated storage CIDR"
    fi
    share_mount=$(podman inspect "$INCUS_CONTAINER" --format '{{json .Mounts}}' |
        jq -c --arg path "$INCUS_SHARE_MOUNT_ROOT" \
            '.[] | select(.Source == $path and .Destination == $path)')
    if [[ -z "$share_mount" ]]; then
        fail "Manila Incus mount" \
            "$INCUS_SHARE_MOUNT_ROOT is not passed into incusd"
    elif jq -e '.RW == true and .Propagation == "rshared"' \
            <<<"$share_mount" >/dev/null; then
        pass "Manila Incus mount" "rw,$(jq -r .Propagation <<<"$share_mount")"
    else
        fail "Manila Incus mount" \
            "must be rw with rshared propagation for CRIU live migration"
    fi
    host_share_inode=$(stat -Lc '%d:%i' "$INCUS_SHARE_MOUNT_ROOT" \
        2>/dev/null || true)
    incus_share_inode=$(podman exec "$INCUS_CONTAINER" \
        stat -Lc '%d:%i' "$INCUS_SHARE_MOUNT_ROOT" 2>/dev/null || true)
    if [[ -n "$host_share_inode" &&
          "$incus_share_inode" == "$host_share_inode" ]]; then
        pass "Manila runtime mount identity" "$host_share_inode"
    else
        fail "Manila runtime mount identity" \
            "host=$host_share_inode incusd=${incus_share_inode:-missing}; restart $INCUS_SERVICE to clear a stale/deleted bind mount"
    fi
    nova_user=$(systemctl show "$NOVA_SERVICE" -p User --value)
    nova_user=${nova_user:-root}
    if [[ -d "$INCUS_SHARE_MOUNT_ROOT" ]] &&
            sudo -u "$nova_user" test -w "$INCUS_SHARE_MOUNT_ROOT"; then
        pass "Manila staging permissions" \
            "$nova_user can write $INCUS_SHARE_MOUNT_ROOT"
    else
        fail "Manila staging permissions" \
            "$nova_user must be able to create per-instance mount directories"
    fi
    inaccessible_parent=
    current_path=$INCUS_SHARE_MOUNT_ROOT
    while [[ "$current_path" != "/" ]]; do
        mode=$(stat -c '%A' "$current_path" 2>/dev/null || true)
        if [[ ${mode:9:1} != "x" ]]; then
            inaccessible_parent=$current_path
            break
        fi
        current_path=$(dirname "$current_path")
    done
    if [[ -z "$inaccessible_parent" ]]; then
        pass "Manila CRIU path traversal" \
            "all staging parents grant other execute/search"
    else
        fail "Manila CRIU path traversal" \
            "$inaccessible_parent blocks mapped root; require o+x or equivalent ACL"
    fi
fi

incus_version=$(podman exec "$INCUS_CONTAINER" incus version 2>/dev/null |
    awk '/Server version:/ {print $3}')
if [[ "$incus_version" == 7.* ]]; then
    pass "Incus server version" "$incus_version"
else
    fail "Incus server version" "expected 7.x actual=${incus_version:-missing}"
fi

server_json=$(podman exec "$INCUS_CONTAINER" incus query /1.0 2>/dev/null)
for extension in storage_driver_cephext \
        storage_cephext_rootfs_idmap_provenance \
        migration_shared_ceph_storage \
        migration_shared_ceph_storage_ready_fence \
        instance_storage_handover instance_storage_handover_proof \
        migration_attempt_fencing \
        storage_materialization_attempt_v1 \
        storage_release_receipt_v2 \
        unix_block_limits; do
    if jq -e --arg extension "$extension" \
            '(.metadata.api_extensions // .api_extensions) |
             index($extension) != null' <<<"$server_json" >/dev/null; then
        pass "Incus extension:$extension"
    else
        fail "Incus extension:$extension" "missing"
    fi
done

https_address=$(podman exec "$INCUS_CONTAINER" incus config get \
    core.https_address 2>/dev/null)
https_bind_host=${https_address%:*}
https_bind_port=${https_address##*:}
if [[ "$https_address" == "$https_bind_host:$https_bind_port" ]] &&
        { [[ -z "$https_bind_host" ]] || is_ipv4 "$https_bind_host"; } &&
        is_tcp_port "$https_bind_port"; then
    pass "Incus HTTPS bind" "$https_address"
else
    fail "Incus HTTPS bind" \
        "must use :PORT, 0.0.0.0:PORT or an explicit IPv4:PORT, actual=$https_address"
fi
trust_json=$(podman exec "$INCUS_CONTAINER" incus config trust list \
    --format json 2>/dev/null)
if jq -e --arg preflight_project "$PREFLIGHT_PROJECT" \
        'length > 0 and all(.[];
         .restricted == true and
         (.projects == [$preflight_project] or .projects == ["nova"]))' \
        <<<"$trust_json" >/dev/null; then
    pass "Incus TLS client restrictions" \
        "restricted to nova or $PREFLIGHT_PROJECT"
else
    fail "Incus TLS client restrictions" \
        "every trusted client must be restricted to nova or $PREFLIGHT_PROJECT"
fi
project_json=$(podman exec "$INCUS_CONTAINER" incus query \
    "/1.0/projects/$PREFLIGHT_PROJECT" 2>/dev/null)
for project_setting in limits.containers limits.virtual-machines; do
    project_value=$(jq -r --arg key "$project_setting" \
        '(.metadata.config // .config)[$key] // empty' <<<"$project_json")
    check_equal "project:$project_setting" 0 "$project_value"
done
project_restricted=$(jq -r \
    '(.metadata.config // .config).restricted // empty' <<<"$project_json")
check_equal "preflight project restricted" true "$project_restricted"
check_equal "preflight protocol" 1 \
    "$(jq -r '(.metadata.config // .config)
        ["user.openstack.preflight_protocol"] // empty' <<<"$project_json")"

compute_driver=$(crudini --get "$NOVA_CONFIG" DEFAULT compute_driver \
    2>/dev/null)
check_equal "Nova compute driver" incus.IncusDriver "$compute_driver"
nova_state_path=$(crudini --get "$NOVA_CONFIG" DEFAULT state_path \
    2>/dev/null || true)
compute_id_path=${nova_state_path%/}/compute_id
nova_user=$(systemctl show "$NOVA_SERVICE" -p User --value)
nova_user=${nova_user:-root}
if [[ "$nova_state_path" == /* && -d "$nova_state_path" ]] && \
        sudo -u "$nova_user" test -w "$nova_state_path"; then
    pass "Nova persistent state path" \
        "$nova_state_path is persistent-service writable"
else
    fail "Nova persistent state path" \
        "state_path must be an absolute writable directory, actual=${nova_state_path:-missing}"
fi
state_fstype=$(findmnt -n -o FSTYPE -T "$nova_state_path" 2>/dev/null || true)
if [[ -n "$state_fstype" && "$state_fstype" != tmpfs && \
      "$state_fstype" != overlay ]]; then
    pass "Nova persistent state filesystem" "$state_fstype"
else
    fail "Nova persistent state filesystem" \
        "state_path must survive nova-compute restart, fstype=${state_fstype:-unknown}"
fi
compute_id=$(tr -d '[:space:]' <"$compute_id_path" 2>/dev/null || true)
if [[ -f "$compute_id_path" && ! -L "$compute_id_path" ]] && \
        sudo -u "$nova_user" test -r "$compute_id_path" && \
        [[ "$compute_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]; then
    pass "Nova persistent compute identity" "$compute_id"
else
    fail "Nova persistent compute identity" \
        "$compute_id_path must be a readable canonical UUID regular file"
fi
idmap_endpoint=$(crudini --get "$NOVA_CONFIG" incus \
    idmap_allocator_endpoint 2>/dev/null || true)
idmap_namespace=$(crudini --get "$NOVA_CONFIG" incus \
    idmap_allocator_namespace 2>/dev/null || true)
idmap_base=$(crudini --get "$NOVA_CONFIG" incus \
    idmap_allocator_base 2>/dev/null || true)
idmap_size=$(crudini --get "$NOVA_CONFIG" incus \
    idmap_allocator_size 2>/dev/null || true)
idmap_count=$(crudini --get "$NOVA_CONFIG" incus \
    idmap_allocator_count 2>/dev/null || true)
idmap_allow_insecure=$(crudini --get "$NOVA_CONFIG" incus \
    idmap_allocator_allow_insecure 2>/dev/null || printf false)
idmap_ca_cert=$(crudini --get "$NOVA_CONFIG" incus \
    idmap_allocator_ca_cert 2>/dev/null || true)
idmap_client_cert=$(crudini --get "$NOVA_CONFIG" incus \
    idmap_allocator_client_cert 2>/dev/null || true)
idmap_client_key=$(crudini --get "$NOVA_CONFIG" incus \
    idmap_allocator_client_key 2>/dev/null || true)
idmap_username=$(crudini --get "$NOVA_CONFIG" incus \
    idmap_allocator_username 2>/dev/null || true)
idmap_password_file=$(crudini --get "$NOVA_CONFIG" incus \
    idmap_allocator_password_file 2>/dev/null || true)
if [[ "$idmap_endpoint" =~ ^https://[^/]+(:[0-9]+)?$ ]]; then
    pass "idmap allocator endpoint" "$idmap_endpoint"
else
    fail "idmap allocator endpoint" \
        "must be an HTTPS origin, actual=${idmap_endpoint:-missing}"
fi
if [[ "${idmap_allow_insecure,,}" == false ]]; then
    pass "idmap allocator transport policy" "HTTPS and mTLS required"
else
    fail "idmap allocator transport policy" \
        "idmap_allocator_allow_insecure must be false in production"
fi
for idmap_tls_path in \
        "$idmap_ca_cert" "$idmap_client_cert" "$idmap_client_key"; do
    if [[ -n "$idmap_tls_path" && -s "$idmap_tls_path" ]] && \
            service_identity_can_read "$idmap_tls_path"; then
        pass "idmap allocator TLS file" "$idmap_tls_path"
    else
        fail "idmap allocator TLS file" \
            "missing, empty, or unreadable by nova-compute path=${idmap_tls_path:-unset}"
    fi
done
if [[ -n "$idmap_client_key" && -f "$idmap_client_key" ]]; then
    idmap_key_mode=$(stat -c '%a' "$idmap_client_key")
    if (( (8#$idmap_key_mode & 037) == 0 )); then
        pass "idmap allocator client key mode" "$idmap_key_mode"
    else
        fail "idmap allocator client key mode" \
            "must not be accessible by group-write or other, mode=$idmap_key_mode"
    fi
fi
if [[ "$idmap_username" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then
    pass "idmap allocator RBAC user" "$idmap_username"
else
    fail "idmap allocator RBAC user" \
        "invalid value=${idmap_username:-missing}"
fi
if [[ -n "$idmap_password_file" && -f "$idmap_password_file" && \
      -s "$idmap_password_file" ]] && \
        service_identity_can_read "$idmap_password_file"; then
    idmap_password_mode=$(stat -c '%a' "$idmap_password_file")
    if (( (8#$idmap_password_mode & 037) == 0 )); then
        pass "idmap allocator password file" \
            "$idmap_password_file mode=$idmap_password_mode"
    else
        fail "idmap allocator password file" \
            "must not be accessible by group-write or other, mode=$idmap_password_mode"
    fi
else
    fail "idmap allocator password file" \
        "must resolve to a non-empty readable regular file, path=${idmap_password_file:-unset}"
fi
if [[ "$idmap_namespace" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then
    pass "idmap allocator namespace" "$idmap_namespace"
else
    fail "idmap allocator namespace" \
        "invalid value=${idmap_namespace:-missing}"
fi
idmap_end=
if [[ "$idmap_base" =~ ^[0-9]+$ && "$idmap_size" == 65536 && \
      "$idmap_count" =~ ^[1-9][0-9]*$ ]]; then
    idmap_end=$((idmap_base + idmap_size * idmap_count))
    if ((idmap_end <= 4294967296)); then
        pass "idmap allocator geometry" \
            "base=$idmap_base size=$idmap_size count=$idmap_count"
    else
        fail "idmap allocator geometry" "range exceeds uint32"
        idmap_end=
    fi
else
    fail "idmap allocator geometry" \
        "base=${idmap_base:-missing} size=${idmap_size:-missing} count=${idmap_count:-missing}"
fi
if [[ -n "$idmap_end" ]]; then
    for subordinate_file in /etc/subuid /etc/subgid; do
        if podman exec "$INCUS_CONTAINER" awk -F: \
                -v first="$idmap_base" -v end="$idmap_end" \
                '$1 == "root" && $2 <= first && ($2 + $3) >= end {ok=1}
                 END {exit !ok}' "$subordinate_file"; then
            pass "idmap range:$subordinate_file" \
                "$idmap_base-$((idmap_end - 1))"
        else
            fail "idmap range:$subordinate_file" \
                "root subordinate range does not cover allocator geometry"
        fi
    done
fi
migration_address=$(crudini --get "$NOVA_CONFIG" incus \
    migration_address 2>/dev/null || true)
migration_host=
migration_port=
if [[ "$migration_address" =~ ^https://([^/:]+):([0-9]+)$ ]]; then
    migration_host=${BASH_REMATCH[1]}
    migration_port=${BASH_REMATCH[2]}
fi
if [[ -n "$migration_host" ]] &&
        is_ipv4 "$migration_host" &&
        [[ "$migration_host" != 0.0.0.0 ]] &&
        is_tcp_port "$migration_port"; then
    pass "Nova migration address" "$migration_address"

    if [[ "$migration_port" == "$https_bind_port" ]]; then
        pass "migration bind port" "$migration_port"
    else
        fail "migration bind port" \
            "advertised=$migration_port bind=${https_bind_port:-missing}"
    fi

    if python3 - "$migration_host" "$migration_port" <<'PY'
import socket
import sys

try:
    with socket.create_connection(
            (sys.argv[1], int(sys.argv[2])), timeout=5):
        pass
except OSError:
    raise SystemExit(1)
PY
    then
        pass "migration TCP reachability" "$migration_host:$migration_port"
    else
        fail "migration TCP reachability" \
            "cannot connect to $migration_host:$migration_port"
    fi
else
    fail "Nova migration address" \
        "must use https://<non-wildcard IPv4>:PORT, actual=${migration_address:-missing}"
fi
nova_bfv_mappings=$(crudini --get "$NOVA_CONFIG" incus \
    boot_from_volume_storage_pools 2>/dev/null || true)
advertised_bfv_pools=$(jq -r '(.metadata.config // .config)
    ["user.openstack.bfv_storage_pools"] // empty' <<<"$project_json")
if [[ -z "$nova_bfv_mappings" ]]; then
    fail "Nova BFV pool mappings" "missing"
else
    IFS=',' read -r -a bfv_mappings <<<"$nova_bfv_mappings"
    for mapping in "${bfv_mappings[@]}"; do
        bfv_source=${mapping%%:*}
        bfv_pool=${mapping#*:}
        if [[ -z "$bfv_source" || -z "$bfv_pool" || \
              "$bfv_source" == "$mapping" ]]; then
            fail "Nova BFV pool mapping" "invalid entry=$mapping"
            continue
        fi
        pool_json=$(podman exec "$INCUS_CONTAINER" incus query \
            "/1.0/storage-pools/$bfv_pool" 2>/dev/null || true)
        check_equal "BFV pool driver:$bfv_pool" cephext \
            "$(jq -r '.metadata.driver // .driver // empty' \
                <<<"$pool_json")"
        actual_source=$(jq -r \
            '.metadata.config.source // .config.source // empty' \
            <<<"$pool_json")
        check_equal "BFV pool source:$bfv_pool" "$bfv_source" \
            "$actual_source"
        if jq -e --arg source "$bfv_source" --arg pool "$bfv_pool" \
                '.[$source] == $pool' \
                <<<"$advertised_bfv_pools" >/dev/null; then
            pass "preflight BFV mapping:$bfv_pool" "$mapping"
        else
            fail "preflight BFV mapping:$bfv_pool" \
                "missing $mapping in ${advertised_bfv_pools:-empty}"
        fi
    done
fi

root_pool=$(crudini --get "$NOVA_CONFIG" incus storage_pool 2>/dev/null || true)
if [[ -z "$root_pool" ]]; then
    fail "Nova root storage pool" "missing"
else
    root_pool_json=$(podman exec "$INCUS_CONTAINER" incus query \
        "/1.0/storage-pools/$root_pool" 2>/dev/null || true)
    root_pool_driver=$(jq -r '.metadata.driver // .driver // empty' \
        <<<"$root_pool_json")
    if [[ -z "$root_pool_driver" || "$root_pool_driver" == dir ]]; then
        fail "Nova root storage pool" \
            "$root_pool uses unsupported production driver ${root_pool_driver:-missing}"
    else
        pass "Nova root storage pool" "$root_pool ($root_pool_driver)"
    fi
fi

root_pool_mappings=$(crudini --get "$NOVA_CONFIG" incus \
    root_storage_pools 2>/dev/null || true)
if tr ',' '\n' <<<"$root_pool_mappings" | \
        cut -d: -f2- | grep -Fxq "$root_pool"; then
    pass "Nova root storage pool mapping" "$root_pool"
else
    fail "Nova root storage pool mapping" \
        "default pool $root_pool is absent from ${root_pool_mappings:-empty}"
fi
if [[ "$REQUIRE_COLD_MIGRATION" == true ]]; then
    check_equal "Nova cold migration" true \
        "$(crudini --get "$NOVA_CONFIG" incus \
            allow_cold_migration 2>/dev/null | tr '[:upper:]' '[:lower:]')"
    migration_finish_retries=$(crudini --get "$NOVA_CONFIG" incus \
        migration_finish_retries 2>/dev/null || true)
    if [[ "$migration_finish_retries" =~ ^[0-9]+$ ]] &&
            ((migration_finish_retries >= 30)); then
        pass "Nova migration finish retries" "$migration_finish_retries"
    else
        fail "Nova migration finish retries" \
            "expected at least 30, actual=${migration_finish_retries:-missing}"
    fi
fi
check_equal "Nova migration recovery" true \
    "$(crudini --get "$NOVA_CONFIG" incus \
        migration_auto_recovery 2>/dev/null)"
exec_start=$(systemctl show "$NOVA_SERVICE" -p ExecStart --value 2>/dev/null)
if grep -q 'nova\.virt\.incus\.cmd\.compute' <<<"$exec_start"; then
    pass "Nova custom manager launcher"
else
    fail "Nova custom manager launcher" "module entry point not active"
fi
admission_exec=$(
    systemctl cat "$NOVA_SERVICE" 2>/dev/null |
        grep -F 'ExecStartPre=/usr/local/sbin/openstack-incus-compute-admission check' ||
        true
)
if [[ -x /usr/local/sbin/openstack-incus-compute-admission &&
      -n "$admission_exec" ]]; then
    pass "compute admission gate" "installed and required"
else
    fail "compute admission gate" "missing executable or systemd check"
fi
check_equal "Nova guest resume owner" true \
    "$(crudini --get "$NOVA_CONFIG" DEFAULT \
        resume_guests_state_on_host_boot 2>/dev/null | tr '[:upper:]' '[:lower:]')"
if /usr/local/sbin/openstack-incus-compute-admission check 2>/dev/null; then
    pass "compute admission token" current-boot
else
    fail "compute admission token" "active compute is not admitted"
fi
if [[ "$inventory_valid" == true ]]; then
    if unsafe_autostart=$(jq -r '
        .[] |
        select(.config["boot.autostart"] != "false") | .name
    ' <<<"$nova_instance_inventory"); then
        if [[ -z "$unsafe_autostart" ]]; then
            pass "Nova instance autostart" disabled
        else
            fail "Nova instance autostart" \
                "must be false: $(tr '\n' ',' <<<"$unsafe_autostart")"
        fi
    else
        fail "Nova instance autostart" "failed to evaluate inventory"
    fi
fi

live_migration_enabled=invalid
if configured_live_migration=$(crudini --get "$NOVA_CONFIG" incus \
        allow_live_migration 2>/dev/null); then
    configured_live_migration=${configured_live_migration,,}
    if [[ "$configured_live_migration" == true ||
          "$configured_live_migration" == false ]]; then
        live_migration_enabled=$configured_live_migration
        pass "Nova live migration setting" "$live_migration_enabled"
    else
        fail "Nova live migration setting" \
            "allow_live_migration must be true or false"
    fi
else
    fail "Nova live migration setting" \
        "cannot read [incus] allow_live_migration"
fi

if [[ "$inventory_valid" == true &&
      "$profile_inventory_valid" == true ]]; then
    if unsafe_incremental=$(jq -r \
        --arg live_migration_enabled "$live_migration_enabled" \
        --slurpfile profiles \
        <(printf '%s\n' "$nova_profile_inventory") '
        def incus_true:
            (tostring | ascii_downcase) as $value |
            $value == "true" or $value == "1" or
            $value == "yes" or $value == "on";
        .[] |
        . as $instance |
        ($profiles[0] |
            map(select(.name == $instance.name))) as $named |
        select(
            if ($named | length) != 1 then
                true
            else
                .profiles != [.name] or
                .config["migration.incremental.memory"] != "false" or
                .expanded_config["migration.incremental.memory"] !=
                    "false" or
                $named[0].config["migration.incremental.memory"] !=
                    "false" or
                (($named[0].config["security.privileged"] // "false") |
                    incus_true) or
                ((.config["security.privileged"] // "false") |
                    incus_true) or
                ((.expanded_config["security.privileged"] // "false") |
                    incus_true) or
                ($live_migration_enabled == "true" and
                    ($named[0].config["migration.stateful"] != "true" or
                     .expanded_config["migration.stateful"] != "true"))
            end
        ) |
        .name
    ' <<<"$nova_instance_inventory"); then
        if [[ -z "$unsafe_incremental" ]]; then
            pass "Nova CRIU full checkpoint" \
                "profile/local/expanded config are false"
        else
            fail "Nova CRIU full checkpoint" \
                "unsafe Nova instances: $(tr '\n' ',' \
                    <<<"$unsafe_incremental")"
        fi
    else
        fail "Nova CRIU full checkpoint" \
            "failed to evaluate instance/profile inventories"
    fi
fi

incus_group_members=$(getent group incus-admin | awk -F: '{print $4}' |
    tr ',' '\n' | sed '/^$/d' | sort | paste -sd, -)
expected_group_members=$(tr ',' '\n' <<<"$EXPECTED_INCUS_GROUP_MEMBERS" |
    sed '/^$/d' | sort | paste -sd, -)
if [[ "$incus_group_members" == "$expected_group_members" ]]; then
    pass "nova-compute Incus group" "$incus_group_members"
else
    fail "nova-compute Incus group" \
        "expected=$expected_group_members actual=$incus_group_members"
fi

compute_user=$(systemctl show "$NOVA_SERVICE" -p User --value 2>/dev/null)
compute_group=$(systemctl show "$NOVA_SERVICE" -p Group --value 2>/dev/null)
preflight_key=$(crudini --get "$NOVA_CONFIG" incus \
    migration_preflight_tls_key 2>/dev/null)
if [[ -n "$preflight_key" && -f "$preflight_key" ]]; then
    key_owner=$(stat -c '%U' "$preflight_key")
    key_group=$(stat -c '%G' "$preflight_key")
    key_mode=$(stat -c '%a' "$preflight_key")
    if [[ "$key_owner" == "$compute_user" && "$key_mode" == 600 ]] ||
            [[ "$key_owner" == root && "$key_group" == "$compute_group" &&
              "$key_mode" == 640 ]]; then
        pass "migration TLS private key" \
            "$key_owner:$key_group mode=$key_mode"
    else
        fail "migration TLS private key" \
            "expected $compute_user:* 600 or root:$compute_group 640; actual=$key_owner:$key_group $key_mode"
    fi
else
    fail "migration TLS private key" "not configured or missing"
fi

keyring=/etc/ceph/ceph.client.cinder.keyring
if [[ -r "$keyring" ]]; then
    mode=$(stat -c '%a' "$keyring")
    owner=$(stat -c '%U:%G' "$keyring")
    if ((10#$mode <= 640)); then
        pass "Ceph keyring:$keyring" "$owner mode=$mode"
    else
        fail "Ceph keyring:$keyring" "permissions too broad: $mode"
    fi
else
    fail "Ceph keyring:$keyring" "missing or unreadable"
fi

check_dedicated_fs /var/lib/incus
check_dedicated_fs /var/log/incus

if systemctl is-active --quiet systemd-timesyncd ||
        systemctl is-active --quiet chrony ||
        systemctl is-active --quiet chronyd; then
    pass "time synchronization" active
else
    fail "time synchronization" "no active time synchronization service"
fi

if ((failures > 0)); then
    echo "FAIL production preflight: $failures check(s) failed" >&2
    exit 1
fi

echo "PASS production preflight"
