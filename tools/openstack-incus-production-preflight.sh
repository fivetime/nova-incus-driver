#!/usr/bin/env bash
# Fail-closed host readiness audit for a production Nova Incus compute.

set -uo pipefail

TARGET_OS_VERSION=${TARGET_OS_VERSION:-24.04}
TARGET_PYTHON_VERSION=${TARGET_PYTHON_VERSION:-3.12}
EXPECTED_INCUS_IMAGE=${EXPECTED_INCUS_IMAGE:-ghcr.io/fivetime/incus:alpine-novm}
EXPECTED_INCUS_IMAGE_DIGEST=${EXPECTED_INCUS_IMAGE_DIGEST:-}
EXPECTED_INCUS_REVISION=${EXPECTED_INCUS_REVISION:-}
EXPECTED_INCUS_GROUP_MEMBERS=${EXPECTED_INCUS_GROUP_MEMBERS:-stack}
INCUS_CONTAINER=${INCUS_CONTAINER:-incus}
INCUS_SERVICE=${INCUS_SERVICE:-incus-podman.service}
NOVA_SERVICE=${NOVA_SERVICE:-devstack@n-cpu.service}
NOVA_CONFIG=${NOVA_CONFIG:-/etc/nova/nova-cpu.conf}
INCUS_SHARE_MOUNT_ROOT=${INCUS_SHARE_MOUNT_ROOT:-/opt/stack/data/nova/instances/incus-shares}
BFV_POOL=${BFV_POOL:-cinder-bfv}
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

if [[ $EUID -ne 0 ]]; then
    fail "execution user" "run as root"
else
    pass "execution user" root
fi

for command_name in podman jq findmnt crudini; do
    check_command "$command_name"
done

for container_command in aa-exec apparmor_parser ceph incus incusd lxcfs rbd; do
    if podman exec "$INCUS_CONTAINER" sh -c \
            "command -v '$container_command'" >/dev/null 2>&1; then
        pass "container:$container_command"
    else
        fail "container:$container_command" "not installed in Incus image"
    fi
done

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

if systemctl is-active --quiet "$INCUS_SERVICE"; then
    pass "$INCUS_SERVICE" active
else
    fail "$INCUS_SERVICE" "not active"
fi
if systemctl is-enabled --quiet "$INCUS_SERVICE"; then
    pass "$INCUS_SERVICE enabled"
else
    fail "$INCUS_SERVICE enabled" "not enabled"
fi
if systemctl is-active --quiet "$NOVA_SERVICE"; then
    pass "$NOVA_SERVICE" active
else
    fail "$NOVA_SERVICE" "not active"
fi

image_name=$(podman inspect "$INCUS_CONTAINER" --format '{{.ImageName}}' \
    2>/dev/null)
repo_digests=$(podman image inspect "$image_name" \
    --format '{{range .RepoDigests}}{{println .}}{{end}}' 2>/dev/null)
expected_repository=${EXPECTED_INCUS_IMAGE%%:*}
if [[ -z "$EXPECTED_INCUS_IMAGE_DIGEST" ]]; then
    check_equal "Incus image name" "$EXPECTED_INCUS_IMAGE" "$image_name"
    fail "Incus image digest pin" \
        "set EXPECTED_INCUS_IMAGE_DIGEST"
else
    expected_pinned_image="${expected_repository}@${EXPECTED_INCUS_IMAGE_DIGEST}"
    check_equal "Incus image name" "$expected_pinned_image" "$image_name"
fi
if [[ -n "$EXPECTED_INCUS_IMAGE_DIGEST" ]] && grep -Fqx \
        "$expected_pinned_image" \
        <<<"$repo_digests"; then
    pass "Incus image digest" "$EXPECTED_INCUS_IMAGE_DIGEST"
elif [[ -n "$EXPECTED_INCUS_IMAGE_DIGEST" ]]; then
    fail "Incus image digest" \
        "expected digest is absent from image RepoDigests"
fi
image_revision=$(podman image inspect "$image_name" \
    --format '{{index .Labels "org.opencontainers.image.revision"}}' \
    2>/dev/null)
if [[ -z "$EXPECTED_INCUS_REVISION" ]]; then
    fail "Incus source revision" \
        "set EXPECTED_INCUS_REVISION (actual=$image_revision)"
else
    check_equal "Incus source revision" \
        "$EXPECTED_INCUS_REVISION" "$image_revision"
fi
quadlet_image=$(systemctl cat "$INCUS_SERVICE" 2>/dev/null |
    sed -n 's/^Image=//p' | tail -n1)
if [[ "$quadlet_image" == *@sha256:* ]]; then
    pass "Quadlet immutable image" "$quadlet_image"
else
    fail "Quadlet immutable image" \
        "Image= must use an immutable @sha256 reference"
fi

manila_enabled=$(crudini --get "$NOVA_CONFIG" incus enable_manila_shares \
    2>/dev/null || true)
if [[ "${manila_enabled,,}" == "true" ]]; then
    share_mount=$(podman inspect "$INCUS_CONTAINER" --format '{{json .Mounts}}' |
        jq -c --arg path "$INCUS_SHARE_MOUNT_ROOT" \
            '.[] | select(.Source == $path and .Destination == $path)')
    if [[ -z "$share_mount" ]]; then
        fail "Manila Incus mount" \
            "$INCUS_SHARE_MOUNT_ROOT is not passed into incusd"
    elif jq -e '.RW == true and
            (.Propagation == "rslave" or .Propagation == "rshared")' \
            <<<"$share_mount" >/dev/null; then
        pass "Manila Incus mount" "rw,$(jq -r .Propagation <<<"$share_mount")"
    else
        fail "Manila Incus mount" \
            "must be rw with rslave or rshared propagation"
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
for extension in storage_driver_cephext migration_shared_ceph_storage \
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
if [[ "$https_address" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+$ ]]; then
    pass "Incus HTTPS bind" "$https_address"
else
    fail "Incus HTTPS bind" \
        "must use an explicit IPv4 migration address, actual=$https_address"
fi
trust_json=$(podman exec "$INCUS_CONTAINER" incus config trust list \
    --format json 2>/dev/null)
if jq -e --arg project "$PREFLIGHT_PROJECT" \
        'length > 0 and all(.[];
         .restricted == true and .projects == [$project])' \
        <<<"$trust_json" >/dev/null; then
    pass "Incus TLS client restrictions" "$PREFLIGHT_PROJECT only"
else
    fail "Incus TLS client restrictions" \
        "every trusted client must be restricted to $PREFLIGHT_PROJECT"
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
check_equal "preflight BFV pool" "$BFV_POOL" \
    "$(jq -r '(.metadata.config // .config)
        ["user.openstack.bfv_pool"] // empty' <<<"$project_json")"
preflight_cinder_pool=$(jq -r '(.metadata.config // .config)
    ["user.openstack.cinder_rbd_pool"] // empty' <<<"$project_json")
if [[ -n "$preflight_cinder_pool" ]]; then
    pass "preflight Cinder RBD pool" "$preflight_cinder_pool"
else
    fail "preflight Cinder RBD pool" "missing"
fi

pool_json=$(podman exec "$INCUS_CONTAINER" incus query \
    "/1.0/storage-pools/$BFV_POOL" 2>/dev/null)
check_equal "BFV pool driver" cephext \
    "$(jq -r '.metadata.driver // .driver // empty' <<<"$pool_json")"
bfv_source=$(jq -r '.metadata.config.source // .config.source // empty' \
    <<<"$pool_json")
if [[ -n "$bfv_source" ]]; then
    pass "BFV pool source" "$bfv_source"
else
    fail "BFV pool source" "missing"
fi

compute_driver=$(crudini --get "$NOVA_CONFIG" DEFAULT compute_driver \
    2>/dev/null)
check_equal "Nova compute driver" incus.IncusDriver "$compute_driver"
check_equal "Nova BFV pool" "$BFV_POOL" \
    "$(crudini --get "$NOVA_CONFIG" incus \
        boot_from_volume_storage_pool 2>/dev/null)"
if [[ "$REQUIRE_COLD_MIGRATION" == true ]]; then
    check_equal "Nova cold migration" true \
        "$(crudini --get "$NOVA_CONFIG" incus \
            allow_cold_migration 2>/dev/null | tr '[:upper:]' '[:lower:]')"
    check_equal "Nova migration address" "https://$https_address" \
        "$(crudini --get "$NOVA_CONFIG" incus \
            migration_address 2>/dev/null)"
    migration_finish_retries=$(crudini --get "$NOVA_CONFIG" incus \
        migration_finish_retries 2>/dev/null || true)
    if [[ "$migration_finish_retries" =~ ^[0-9]+$ ]] &&
            ((migration_finish_retries >= 10)); then
        pass "Nova migration finish retries" "$migration_finish_retries"
    else
        fail "Nova migration finish retries" \
            "expected at least 10, actual=${migration_finish_retries:-missing}"
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
unsafe_autostart=$(podman exec "$INCUS_CONTAINER" incus \
    --project nova list --format json 2>/dev/null |
    jq -r '.[] | select(.config["user.openstack.uuid"] != null) |
        select(.config["boot.autostart"] != "false") | .name')
if [[ -z "$unsafe_autostart" ]]; then
    pass "Nova instance autostart" disabled
else
    fail "Nova instance autostart" \
        "must be false: $(tr '\n' ',' <<<"$unsafe_autostart")"
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
