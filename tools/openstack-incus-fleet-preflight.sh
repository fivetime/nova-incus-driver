#!/usr/bin/env bash
# Cross-compute production readiness and drift audit.

set -uo pipefail

NOVA_CONTROLLER_RUNTIME_ONLY=false
if (($# > 1)); then
    echo "Usage: $0 [--controller-runtime-only]" >&2
    exit 2
fi
case ${1:-} in
    '') ;;
    --controller-runtime-only)
        NOVA_CONTROLLER_RUNTIME_ONLY=true
        ;;
    *)
        echo "Usage: $0 [--controller-runtime-only]" >&2
        exit 2
        ;;
esac

COMPUTE_NODES=${COMPUTE_NODES:-}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute audit key}
SSH_KNOWN_HOSTS_FILE=${SSH_KNOWN_HOSTS_FILE:-$HOME/.ssh/known_hosts}
EXPECTED_INCUS_IMAGE_DIGEST=${EXPECTED_INCUS_IMAGE_DIGEST:-}
EXPECTED_INCUS_REVISION=${EXPECTED_INCUS_REVISION:-}
EXPECTED_HYPERVISOR_TYPE=${EXPECTED_HYPERVISOR_TYPE:-lxd}
REMOTE_DRIVER=${REMOTE_DRIVER:-/opt/stack/nova/nova/virt/incus}
REMOTE_NOVA_CONFIG=${REMOTE_NOVA_CONFIG:-/etc/nova/nova-cpu.conf}
CONTROLLER_SSH=${CONTROLLER_SSH:-}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
NOVA_API_NODES=${NOVA_API_NODES:-}
NOVA_CONDUCTOR_NODES=${NOVA_CONDUCTOR_NODES:-$NOVA_API_NODES}
REQUIRE_MANILA_MIGRATION_RUNTIME=${REQUIRE_MANILA_MIGRATION_RUNTIME:-false}
REQUIRE_CEILOMETER_RUNTIME=${REQUIRE_CEILOMETER_RUNTIME:-false}
CEILOMETER_COMPUTE_NODES=${CEILOMETER_COMPUTE_NODES:-$COMPUTE_NODES}
CEILOMETER_NOTIFICATION_NODES=${CEILOMETER_NOTIFICATION_NODES:-$NOVA_API_NODES}
NOVA_API_RUNTIME_PYTHON=${NOVA_API_RUNTIME_PYTHON:-}
NOVA_COMPUTE_RUNTIME_PYTHON=${NOVA_COMPUTE_RUNTIME_PYTHON:-}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
HOST_PREFLIGHT=${HOST_PREFLIGHT:-$SCRIPT_DIR/openstack-incus-production-preflight.sh}
RUNTIME_PREFLIGHT=${RUNTIME_PREFLIGHT:-$SCRIPT_DIR/openstack-incus-nova-runtime-preflight.sh}
CEILOMETER_PREFLIGHT=${CEILOMETER_PREFLIGHT:-$SCRIPT_DIR/openstack-incus-ceilometer-runtime-preflight.sh}
RELEASE_DRIVER=${RELEASE_DRIVER:-$SCRIPT_DIR/../nova/virt/incus}

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
failures=0
declare -A seen_migration_addresses=()
declare -A seen_compute_ids=()
expected_idmap_contract=
ceph_pool_records=()

case "$REQUIRE_MANILA_MIGRATION_RUNTIME" in
    true|false) ;;
    *)
        echo "REQUIRE_MANILA_MIGRATION_RUNTIME must be true or false" >&2
        exit 2
        ;;
esac
case "$REQUIRE_CEILOMETER_RUNTIME" in
    true|false) ;;
    *)
        echo "REQUIRE_CEILOMETER_RUNTIME must be true or false" >&2
        exit 2
        ;;
esac
if [[ "$NOVA_CONTROLLER_RUNTIME_ONLY" == true ]]; then
    if [[ "$REQUIRE_MANILA_MIGRATION_RUNTIME" != true ]]; then
        echo "NOVA_CONTROLLER_RUNTIME_ONLY requires " \
            "REQUIRE_MANILA_MIGRATION_RUNTIME=true" >&2
        exit 2
    fi
    if [[ "$REQUIRE_CEILOMETER_RUNTIME" == true ]]; then
        echo "NOVA_CONTROLLER_RUNTIME_ONLY does not audit Ceilometer" >&2
        exit 2
    fi
else
    [[ -n "$COMPUTE_NODES" ]] || {
        echo "Set COMPUTE_NODES to host=ssh-target comma-separated nodes" >&2
        exit 2
    }
    [[ -n "$EXPECTED_INCUS_IMAGE_DIGEST" ]] || {
        echo "Set EXPECTED_INCUS_IMAGE_DIGEST to the approved digest" >&2
        exit 2
    }
    [[ -n "$EXPECTED_INCUS_REVISION" ]] || {
        echo "Set EXPECTED_INCUS_REVISION to the approved source revision" >&2
        exit 2
    }
fi

if [[ "$NOVA_CONTROLLER_RUNTIME_ONLY" != true && ! -r "$HOST_PREFLIGHT" ]]; then
    echo "FAIL host preflight script is not readable: $HOST_PREFLIGHT" >&2
    exit 1
fi
if [[ ! -r "$RUNTIME_PREFLIGHT" ]]; then
    echo "FAIL runtime preflight script is not readable: $RUNTIME_PREFLIGHT" >&2
    exit 1
fi
if [[ "$NOVA_CONTROLLER_RUNTIME_ONLY" != true && \
      "$REQUIRE_CEILOMETER_RUNTIME" == true && \
      ! -r "$CEILOMETER_PREFLIGHT" ]]; then
    echo "FAIL Ceilometer preflight is not readable: $CEILOMETER_PREFLIGHT" >&2
    exit 1
fi
if [[ "$NOVA_CONTROLLER_RUNTIME_ONLY" != true && ! -d "$RELEASE_DRIVER" ]]; then
    echo "FAIL release driver tree is not a directory: $RELEASE_DRIVER" >&2
    exit 1
fi

driver_tree_hash() {
    local file
    while IFS= read -r -d '' file; do
        sed 's/\r$//' "$file" | sha256sum | awk '{print $1}'
    done < <(find "$1" -type f -name '*.py' -print0 | LC_ALL=C sort -z) |
        sha256sum | awk '{print $1}'
}

expected_driver_hash=
if [[ "$NOVA_CONTROLLER_RUNTIME_ONLY" != true ]]; then
    expected_driver_hash=$(driver_tree_hash "$RELEASE_DRIVER")
fi

pass() {
    printf 'PASS %-38s %s\n' "$1" "${2:-}"
}

fail() {
    printf 'FAIL %-38s %s\n' "$1" "$2" >&2
    failures=$((failures + 1))
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

remote() {
    local target=$1
    shift
    "${SSH[@]}" "$target" "$@"
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

if [[ "$REQUIRE_CEILOMETER_RUNTIME" == true ]]; then
    if [[ -z "$CEILOMETER_NOTIFICATION_NODES" ]]; then
        fail "ceilometer notification mappings" \
            "CEILOMETER_NOTIFICATION_NODES must enumerate every host"
    else
        IFS=, read -ra notification_nodes \
            <<<"$CEILOMETER_NOTIFICATION_NODES"
        for node in "${notification_nodes[@]}"; do
            role_name=${node%%=*}
            role_target=${node#*=}
            if remote "$role_target" "bash -s -- notification" \
                    <"$CEILOMETER_PREFLIGHT"; then
                pass "$role_name Ceilometer notification" "volume meters"
            else
                fail "$role_name Ceilometer notification" \
                    "runtime contract failed"
            fi
        done
    fi
fi

if [[ "$REQUIRE_MANILA_MIGRATION_RUNTIME" == true && \
      -z "$NOVA_API_NODES" ]]; then
    fail "nova-api runtime mappings" \
        "NOVA_API_NODES must enumerate every running API host"
elif [[ "$REQUIRE_MANILA_MIGRATION_RUNTIME" == true ]]; then
    IFS=, read -ra api_nodes <<<"$NOVA_API_NODES"
    declare -A seen_api_nodes=()
    for node in "${api_nodes[@]}"; do
        api_name=${node%%=*}
        api_target=${node#*=}
        if [[ -z "$api_name" || -z "$api_target" || \
              "$api_name" == "$api_target" ]]; then
            fail "nova-api runtime mapping" "invalid entry: $node"
            continue
        fi
        if [[ -n "${seen_api_nodes[$api_name]:-}" ]]; then
            fail "nova-api runtime mapping" "duplicate name: $api_name"
            continue
        fi
        seen_api_nodes[$api_name]=1
        printf -v runtime_python_q '%q' "$NOVA_API_RUNTIME_PYTHON"
        if remote "$api_target" \
                "MIN_INCUS_MIGRATE_DATA_VERSION=1.6 \
                 RUNTIME_PYTHON=$runtime_python_q bash -s -- api" \
                <"$RUNTIME_PREFLIGHT"; then
            pass "$api_name nova-api runtime" "patched API and core hooks"
        else
            fail "$api_name nova-api runtime" \
                "running API lacks the required Manila patch contract"
        fi
    done
fi

if [[ "$REQUIRE_MANILA_MIGRATION_RUNTIME" == true && \
      -z "$NOVA_CONDUCTOR_NODES" ]]; then
    fail "nova-conductor runtime mappings" \
        "NOVA_CONDUCTOR_NODES must enumerate every running conductor host"
elif [[ "$REQUIRE_MANILA_MIGRATION_RUNTIME" == true ]]; then
    IFS=, read -ra conductor_nodes <<<"$NOVA_CONDUCTOR_NODES"
    declare -A seen_conductor_nodes=()
    for node in "${conductor_nodes[@]}"; do
        conductor_name=${node%%=*}
        conductor_target=${node#*=}
        if [[ -z "$conductor_name" || -z "$conductor_target" || \
              "$conductor_name" == "$conductor_target" ]]; then
            fail "nova-conductor runtime mapping" "invalid entry: $node"
            continue
        fi
        if [[ -n "${seen_conductor_nodes[$conductor_name]:-}" ]]; then
            fail "nova-conductor runtime mapping" \
                "duplicate name: $conductor_name"
            continue
        fi
        seen_conductor_nodes[$conductor_name]=1
        printf -v runtime_python_q '%q' "$NOVA_API_RUNTIME_PYTHON"
        if remote "$conductor_target" \
                "MIN_INCUS_MIGRATE_DATA_VERSION=1.6 \
                 RUNTIME_PYTHON=$runtime_python_q bash -s -- conductor" \
                <"$RUNTIME_PREFLIGHT"; then
            pass "$conductor_name nova-conductor runtime" \
                "Incus migration object registry"
        else
            fail "$conductor_name nova-conductor runtime" \
                "running conductor cannot deserialize Incus migration data"
        fi
    done
fi

if [[ "$NOVA_CONTROLLER_RUNTIME_ONLY" == true ]]; then
    if ((failures > 0)); then
        echo "NO-GO: Nova controller runtime validation failed " \
            "($failures failures)" >&2
        exit 1
    fi
    echo "PASS Nova controller runtime barrier"
    exit 0
fi

IFS=, read -ra nodes <<<"$COMPUTE_NODES"
for node in "${nodes[@]}"; do
    host=${node%%=*}
    target=${node#*=}

    if [[ "$REQUIRE_CEILOMETER_RUNTIME" == true ]] && \
            grep -Eq "(^|,)$host=$target(,|$)" \
                <<<"$CEILOMETER_COMPUTE_NODES"; then
        if remote "$target" "bash -s -- compute" \
                <"$CEILOMETER_PREFLIGHT"; then
            pass "$host Ceilometer compute" "Incus polling contract"
        else
            fail "$host Ceilometer compute" "runtime contract failed"
        fi
    fi
    if [[ -z "$host" || -z "$target" || "$host" == "$target" ]]; then
        fail "node declaration" "invalid entry: $node"
        continue
    fi

    echo "=== $host ($target) ==="
    if remote "$target" \
            "EXPECTED_INCUS_IMAGE_DIGEST='$EXPECTED_INCUS_IMAGE_DIGEST' \
             EXPECTED_INCUS_REVISION='$EXPECTED_INCUS_REVISION' \
             bash -s" <"$HOST_PREFLIGHT"; then
        pass "$host host preflight"
    else
        fail "$host host preflight" "strict audit failed"
    fi

    if [[ "$REQUIRE_MANILA_MIGRATION_RUNTIME" == true ]]; then
        printf -v runtime_python_q '%q' "$NOVA_COMPUTE_RUNTIME_PYTHON"
        if remote "$target" \
                "MIN_INCUS_MIGRATE_DATA_VERSION=1.6 \
                 RUNTIME_PYTHON=$runtime_python_q bash -s -- compute" \
                <"$RUNTIME_PREFLIGHT"; then
            pass "$host nova-compute runtime" \
                "Incus entry point, manager hooks, and trait code"
        else
            fail "$host nova-compute runtime" \
                "running compute lacks the required Manila patch contract"
        fi
    fi

    driver_hash=$(remote "$target" \
        "while IFS= read -r -d '' file; do \
             sed 's/\r\$//' \"\$file\" | sha256sum | awk '{print \$1}'; \
         done < <(find '$REMOTE_DRIVER' -type f -name '*.py' -print0 | \
             LC_ALL=C sort -z) | sha256sum | awk '{print \$1}'" \
        2>/dev/null)
    if [[ "$driver_hash" == "$expected_driver_hash" ]]; then
        pass "$host driver hash" "$driver_hash"
    else
        fail "$host driver hash" \
            "release=$expected_driver_hash actual=$driver_hash"
    fi

    migration_address=$(remote "$target" \
        "crudini --get '$REMOTE_NOVA_CONFIG' incus migration_address" \
        2>/dev/null)
    migration_host=
    migration_port=
    if [[ "$migration_address" =~ ^https://([^/:]+):([0-9]+)$ ]]; then
        migration_host=${BASH_REMATCH[1]}
        migration_port=${BASH_REMATCH[2]}
    fi
    if [[ -z "$migration_host" ]] ||
            ! is_ipv4 "$migration_host" ||
            [[ "$migration_host" == 0.0.0.0 ]] ||
            ! is_tcp_port "$migration_port"; then
        fail "$host migration address" \
            "invalid advertised endpoint: ${migration_address:-missing}"
    elif [[ -n "${seen_migration_addresses[$migration_address]:-}" ]]; then
        fail "$host migration address" \
            "duplicate of ${seen_migration_addresses[$migration_address]}: $migration_address"
    else
        seen_migration_addresses[$migration_address]=$host
        pass "$host migration address" "$migration_address"
    fi

    idmap_contract=$(remote "$target" \
        "printf '%s|%s|%s|%s|%s|%s|%s' \
          \"\$(crudini --get '$REMOTE_NOVA_CONFIG' incus idmap_allocator_endpoint 2>/dev/null)\" \
          \"\$(crudini --get '$REMOTE_NOVA_CONFIG' incus idmap_allocator_namespace 2>/dev/null)\" \
          \"\$(crudini --get '$REMOTE_NOVA_CONFIG' incus idmap_allocator_base 2>/dev/null)\" \
          \"\$(crudini --get '$REMOTE_NOVA_CONFIG' incus idmap_allocator_size 2>/dev/null)\" \
          \"\$(crudini --get '$REMOTE_NOVA_CONFIG' incus idmap_allocator_count 2>/dev/null)\" \
          \"\$(crudini --get '$REMOTE_NOVA_CONFIG' incus idmap_allocator_allow_insecure 2>/dev/null || printf false)\" \
          \"\$(crudini --get '$REMOTE_NOVA_CONFIG' incus idmap_allocator_username 2>/dev/null)\"" \
        2>/dev/null)
    if [[ "$idmap_contract" == *'||'* || "$idmap_contract" == '|'* || \
          "$idmap_contract" == *'|' ]]; then
        fail "$host idmap allocator" "incomplete contract=$idmap_contract"
    elif [[ -z "$expected_idmap_contract" ]]; then
        expected_idmap_contract=$idmap_contract
        pass "$host idmap allocator" "$idmap_contract"
    elif [[ "$idmap_contract" != "$expected_idmap_contract" ]]; then
        fail "$host idmap allocator" \
            "fleet=$expected_idmap_contract actual=$idmap_contract"
    else
        pass "$host idmap allocator" "$idmap_contract"
    fi

    idmap_password_file=$(remote "$target" \
        "crudini --get '$REMOTE_NOVA_CONFIG' incus idmap_allocator_password_file" \
        2>/dev/null)
    idmap_password_check=$(remote "$target" \
        "pid=\$(systemctl show devstack@n-cpu.service -p MainPID --value); \
         uid=\$(awk '/^Uid:/ {print \$2}' \"/proc/\$pid/status\" 2>/dev/null); \
         gid=\$(awk '/^Gid:/ {print \$2}' \"/proc/\$pid/status\" 2>/dev/null); \
         groups=\$(awk '/^Groups:/ {\$1=\"\"; sub(/^ +/, \"\"); gsub(/ +/, \",\"); print}' \"/proc/\$pid/status\" 2>/dev/null); \
         test -n \"\$uid\" && test -n \"\$gid\" && test -n '$idmap_password_file' && \
         test -f '$idmap_password_file' && test -s '$idmap_password_file' && \
         if test -n \"\$groups\"; then \
             setpriv --reuid=\"\$uid\" --regid=\"\$gid\" --groups=\"\$groups\" test -r '$idmap_password_file'; \
         else \
             setpriv --reuid=\"\$uid\" --regid=\"\$gid\" --clear-groups test -r '$idmap_password_file'; \
         fi && \
         mode=\$(stat -c '%a' '$idmap_password_file') && \
         test \$((8#\$mode & 037)) -eq 0 && printf ready" 2>/dev/null)
    if [[ "$idmap_password_check" == ready ]]; then
        pass "$host idmap allocator password" "$idmap_password_file"
    else
        fail "$host idmap allocator password" \
            "missing, unreadable, or overly permissive: ${idmap_password_file:-unset}"
    fi

    # nova-compute writes the Cinder, Manila and spawn-attempt journals as the
    # service user. A journal directory left owned by root silently fails every
    # data-volume attach with "Permission denied" long after stack time.
    journal_owner_check=$(remote "$target" \
        "state_path=\$(crudini --get '$REMOTE_NOVA_CONFIG' DEFAULT state_path \
             2>/dev/null); \
         pid=\$(systemctl show devstack@n-cpu.service -p MainPID --value); \
         user=\$(stat -c '%U' /proc/\$pid 2>/dev/null); \
         bad=; \
         for d in incus-volume-journal incus-share-journal \
                 incus-spawn-attempts; do \
             p=\"\$state_path/instances/\$d\"; \
             test -d \"\$p\" || continue; \
             owner=\$(stat -c '%U' \"\$p\"); \
             mode=\$(stat -c '%a' \"\$p\"); \
             if test \"\$owner\" != \"\$user\" || \
                     test \$((8#\$mode & 077)) -ne 0; then \
                 bad=\"\$bad \$d(\$owner:\$mode)\"; \
             fi; \
         done; \
         if test -n \"\$bad\"; then printf 'BAD%s' \"\$bad\"; \
         else printf 'ok:%s' \"\$user\"; fi" 2>/dev/null)
    if [[ "$journal_owner_check" == ok:* ]]; then
        pass "$host journal directories" "${journal_owner_check#ok:}"
    else
        fail "$host journal directories" \
            "must be owned by the nova-compute user with no group/other access:${journal_owner_check#BAD}"
    fi

    ceph_pool_lines=$(remote "$target" \
        "podman exec incus incus query '/1.0/storage-pools?recursion=1' | \
         jq -r '.[] | select(.driver==\"ceph\") | \
             [.name, (.config[\"ceph.cluster_name\"] // \"ceph\"), \
              (.config.source // \"\"), \
              (.config[\"ceph.rbd.image_prefix\"] // \"\")] | join(\"|\")'" \
        2>/dev/null)
    if [[ -z "$ceph_pool_lines" ]]; then
        pass "$host ceph root pools" "none"
    else
        while IFS= read -r pool_line; do
            [[ -n "$pool_line" ]] || continue
            ceph_pool_records+=("$host|$pool_line")
        done <<<"$ceph_pool_lines"
        pass "$host ceph root pools" \
            "$(tr '\n' ' ' <<<"$ceph_pool_lines")"
    fi

    compute_id=$(remote "$target" \
        "state_path=\$(crudini --get '$REMOTE_NOVA_CONFIG' DEFAULT state_path 2>/dev/null); \
         test -f \"\$state_path/compute_id\" && \
         tr -d '[:space:]' <\"\$state_path/compute_id\"" 2>/dev/null)
    if [[ ! "$compute_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]; then
        fail "$host compute identity" \
            "missing or invalid persistent compute_id=${compute_id:-missing}"
    elif [[ -n "${seen_compute_ids[$compute_id]:-}" ]]; then
        fail "$host compute identity" \
            "duplicate of ${seen_compute_ids[$compute_id]}: $compute_id"
    else
        seen_compute_ids[$compute_id]=$host
        pass "$host compute identity" "$compute_id"
    fi

    service=$(openstack compute service list --service nova-compute \
        --host "$host" -f value -c Status -c State 2>/dev/null)
    if grep -q '^enabled up$' <<<"$service"; then
        pass "$host nova-compute" "enabled/up"
    else
        fail "$host nova-compute" "expected enabled/up, actual=$service"
    fi

    hypervisor_state=$(openstack hypervisor show "$host" \
        -f value -c state 2>/dev/null)
    hypervisor_type=$(openstack hypervisor show "$host" \
        -f value -c hypervisor_type 2>/dev/null)
    if [[ "$hypervisor_state" == up && \
          "$hypervisor_type" == "$EXPECTED_HYPERVISOR_TYPE" ]]; then
        pass "$host hypervisor" "up/$hypervisor_type"
    else
        fail "$host hypervisor" \
            "expected up/$EXPECTED_HYPERVISOR_TYPE, actual=$hypervisor_state/$hypervisor_type"
    fi

    provider_uuid=$(openstack resource provider list --name "$host" \
        -f value -c uuid 2>/dev/null)
    if [[ -z "$provider_uuid" ]]; then
        fail "$host Placement provider" "missing"
    else
        inventories=$(openstack resource provider inventory list \
            "$provider_uuid" -f value -c resource_class 2>/dev/null)
        traits=$(openstack resource provider trait list \
            "$provider_uuid" -f value -c name 2>/dev/null)
        for resource_class in VCPU MEMORY_MB DISK_GB; do
            grep -qx "$resource_class" <<<"$inventories" ||
                fail "$host inventory:$resource_class" "missing"
        done
        if grep -qx CUSTOM_INCUS_SYSTEM_CONTAINER <<<"$traits"; then
            pass "$host Placement provider" \
                "inventory and system-container trait"
        else
            fail "$host Placement trait" \
                "CUSTOM_INCUS_SYSTEM_CONTAINER missing"
        fi
        if [[ "$REQUIRE_MANILA_MIGRATION_RUNTIME" == true ]]; then
            for manila_trait in \
                CUSTOM_INCUS_MANILA_SHARE \
                CUSTOM_INCUS_MANILA_COLD_MIGRATION \
                CUSTOM_INCUS_MANILA_LIVE_MIGRATION; do
                if grep -qx "$manila_trait" <<<"$traits"; then
                    pass "$host Placement trait" "$manila_trait"
                else
                    fail "$host Placement trait" "$manila_trait missing"
                fi
            done
        fi
    fi

    ovn_agents=$(openstack network agent list --host "$host" -f value \
        -c Binary -c Alive -c State 2>/dev/null)
    if grep -Eq \
            'ovn-controller.*True.*True|True.*ovn-controller.*True|True.*True.*ovn-controller' \
            <<<"$ovn_agents"; then
        pass "$host OVN controller" alive
    else
        fail "$host OVN controller" "no alive/enabled ovn-controller agent"
    fi
done

# Shared Ceph pool image-prefix distinctness. Two Incus daemons sharing one
# OSD pool without distinct per-server image prefixes silently corrupt each
# other's image caches (the failure mode the fork's ceph.rbd.image_prefix
# exists to prevent). Group pools by {cluster, source}; any group used by
# more than one host must carry a non-empty, fleet-unique prefix per host.
declare -A shared_pool_group_hosts=()
declare -A shared_pool_group_prefixes=()
for record in "${ceph_pool_records[@]}"; do
    IFS='|' read -r record_host record_pool record_cluster record_source \
        record_prefix <<<"$record"
    group="$record_cluster|$record_source"
    shared_pool_group_hosts[$group]+="$record_host=$record_pool=$record_prefix"$'\n'
done
for group in "${!shared_pool_group_hosts[@]}"; do
    members=()
    while IFS= read -r member; do
        [[ -n "$member" ]] && members+=("$member")
    done <<<"${shared_pool_group_hosts[$group]}"
    ((${#members[@]} > 1)) || continue
    declare -A group_prefix_owner=()
    for member in "${members[@]}"; do
        IFS='=' read -r member_host member_pool member_prefix <<<"$member"
        if [[ -z "$member_prefix" ]]; then
            fail "$member_host shared ceph pool prefix" \
                "pool $member_pool shares OSD pool ${group#*|} with other hosts but has no ceph.rbd.image_prefix"
        elif [[ -n "${group_prefix_owner[$member_prefix]:-}" ]]; then
            fail "$member_host shared ceph pool prefix" \
                "pool $member_pool prefix $member_prefix duplicates ${group_prefix_owner[$member_prefix]}"
        else
            group_prefix_owner[$member_prefix]=$member_host
            pass "$member_host shared ceph pool prefix" \
                "$member_pool=$member_prefix"
        fi
    done
    unset group_prefix_owner
done

cinder_services=$(openstack volume service list -f value \
    -c Binary -c Host -c Status -c State 2>/dev/null)
for backend in '@ceph' 'cinder-scheduler'; do
    if awk -v required="$backend" \
            'index($0, required) && $(NF-1) == "enabled" && $NF == "up" {
                 found=1
             }
             END {exit !found}' <<<"$cinder_services"; then
        pass "Cinder required service:$backend"
    else
        fail "Cinder required service:$backend" "missing or not enabled/up"
    fi
done

if ((failures > 0)); then
    echo "FAIL fleet preflight: $failures check(s) failed" >&2
    exit 1
fi

echo "PASS fleet preflight"
