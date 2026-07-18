#!/usr/bin/env bash
# Fail-closed fleet and ownership signals for a production monitoring probe.

set -uo pipefail

COMPUTE_NODES=${COMPUTE_NODES:?Set host=ssh-target comma-separated nodes}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute audit key}
EXPECTED_INCUS_IMAGE_DIGEST=${EXPECTED_INCUS_IMAGE_DIGEST:?Set approved digest}
EXPECTED_INCUS_REVISION=${EXPECTED_INCUS_REVISION:?Set approved source revision}
CONTROLLER_SSH=${CONTROLLER_SSH:?Set CONTROLLER_SSH}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
INCUS_PROJECT=${INCUS_PROJECT:-nova}
CINDER_RBD_POOL=${CINDER_RBD_POOL:-cinder-volumes-rbd-pool}
FENCE_EVIDENCE_FILE=${FENCE_EVIDENCE_FILE:-}
FENCE_EVIDENCE_MAX_AGE_SECONDS=${FENCE_EVIDENCE_MAX_AGE_SECONDS:-2592000}
CONTROL_FS_WARNING_PERCENT=${CONTROL_FS_WARNING_PERCENT:-80}
INSTANCE_PRESSURE_WARNING_PERCENT=${INSTANCE_PRESSURE_WARNING_PERCENT:-90}
CONSOLE_LOG_WARNING_BYTES=${CONSOLE_LOG_WARNING_BYTES:-268435456}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
FLEET_PREFLIGHT=${FLEET_PREFLIGHT:-$SCRIPT_DIR/openstack-incus-fleet-preflight.sh}

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)
failures=0
declare -A mapping_owners=()
declare -A mapping_counts=()
declare -A runtime_hosts=()
declare -A runtime_names=()
declare -A runtime_images=()
declare -A runtime_states=()

pass() {
    printf 'PASS %-40s %s\n' "$1" "${2:-}"
}

fail() {
    printf 'FAIL %-40s %s\n' "$1" "$2" >&2
    failures=$((failures + 1))
}

check_ratio() {
    local label=$1 current=$2 maximum=$3
    if [[ ! "$current" =~ ^[0-9]+$ ]]; then
        fail "$label" "current value unavailable: ${current:-empty}"
    elif [[ "$maximum" == max ]]; then
        fail "$label" "limit is unlimited"
    elif [[ ! "$maximum" =~ ^[0-9]+$ ]] || ((maximum <= 0)); then
        fail "$label" "invalid limit: ${maximum:-empty}"
    elif ((current * 100 < maximum * INSTANCE_PRESSURE_WARNING_PERCENT)); then
        pass "$label" "$current/$maximum"
    else
        fail "$label" \
            "$current/$maximum >= $INSTANCE_PRESSURE_WARNING_PERCENT%"
    fi
}

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

[[ "$CONTROL_FS_WARNING_PERCENT" =~ ^[1-9][0-9]?$ ]] ||
    { echo "CONTROL_FS_WARNING_PERCENT must be between 1 and 99" >&2; exit 2; }
[[ "$INSTANCE_PRESSURE_WARNING_PERCENT" =~ ^[1-9][0-9]?$ ]] ||
    { echo "INSTANCE_PRESSURE_WARNING_PERCENT must be between 1 and 99" >&2; exit 2; }
[[ "$CONSOLE_LOG_WARNING_BYTES" =~ ^[1-9][0-9]*$ ]] ||
    { echo "CONSOLE_LOG_WARNING_BYTES must be positive" >&2; exit 2; }
[[ "$FENCE_EVIDENCE_MAX_AGE_SECONDS" =~ ^[1-9][0-9]*$ ]] ||
    { echo "FENCE_EVIDENCE_MAX_AGE_SECONDS must be positive" >&2; exit 2; }

if [[ -z "$FENCE_EVIDENCE_FILE" ]]; then
    fail "external fence evidence" "FENCE_EVIDENCE_FILE is not set"
elif [[ ! -f "$FENCE_EVIDENCE_FILE" ]]; then
    fail "external fence evidence" "$FENCE_EVIDENCE_FILE is not a file"
else
    fence_owner=$(stat -c %U "$FENCE_EVIDENCE_FILE" 2>/dev/null)
    fence_mode=$(stat -c %a "$FENCE_EVIDENCE_FILE" 2>/dev/null)
    fence_mtime=$(stat -c %Y "$FENCE_EVIDENCE_FILE" 2>/dev/null)
    fence_age=$(($(date +%s) - fence_mtime))
    fence_hash=$(sha256sum "$FENCE_EVIDENCE_FILE" | awk '{print $1}')
    if [[ "$fence_owner" != root ]]; then
        fail "external fence evidence owner" "$fence_owner"
    elif ((8#$fence_mode & 8#22)); then
        fail "external fence evidence mode" "$fence_mode is writable by group/other"
    elif ((fence_age < 0 || fence_age > FENCE_EVIDENCE_MAX_AGE_SECONDS)); then
        fail "external fence evidence age" \
            "$fence_age seconds (maximum $FENCE_EVIDENCE_MAX_AGE_SECONDS)"
    elif ! grep -Fqx \
            "PASS fenced BFV evacuation and returning-host reconciliation" \
            "$FENCE_EVIDENCE_FILE"; then
        fail "external fence evidence result" "successful terminal record absent"
    else
        pass "external fence evidence" \
            "age=${fence_age}s sha256=$fence_hash"
    fi
fi

if COMPUTE_NODES="$COMPUTE_NODES" \
        SSH_IDENTITY="$SSH_IDENTITY" \
        EXPECTED_INCUS_IMAGE_DIGEST="$EXPECTED_INCUS_IMAGE_DIGEST" \
        EXPECTED_INCUS_REVISION="$EXPECTED_INCUS_REVISION" \
        CONTROLLER_SSH="$CONTROLLER_SSH" \
        CONTROLLER_OPENRC="$CONTROLLER_OPENRC" \
        bash "$FLEET_PREFLIGHT"; then
    pass "fleet drift"
else
    fail "fleet drift" "strict fleet preflight failed"
fi

IFS=, read -ra nodes <<<"$COMPUTE_NODES"
for node in "${nodes[@]}"; do
    host=${node%%=*}
    target=${node#*=}
    if [[ -z "$host" || -z "$target" || "$host" == "$target" ]]; then
        fail "node declaration" "invalid entry: $node"
        continue
    fi

    compute_state=$(remote "$target" \
        "systemctl is-active devstack@n-cpu.service 2>/dev/null || true")
    if [[ "$compute_state" == active ]]; then
        if remote "$target" \
                /usr/local/sbin/openstack-incus-compute-admission check \
                >/dev/null 2>&1; then
            pass "$host admission" "active/current-boot"
        else
            fail "$host admission" \
                "nova-compute is active without a current admission token"
        fi
    else
        pass "$host nova-compute" "${compute_state:-inactive}"
    fi

    for path in /var/lib/incus /var/log/incus; do
        usage=$(remote "$target" \
            "df -P '$path' 2>/dev/null | awk 'NR == 2 {gsub(/%/, \"\", \$5); print \$5}'")
        if [[ "$usage" =~ ^[0-9]+$ ]] &&
                ((usage < CONTROL_FS_WARNING_PERCENT)); then
            pass "$host $path pressure" "$usage%"
        elif [[ "$usage" =~ ^[0-9]+$ ]]; then
            fail "$host $path pressure" \
                "$usage% >= $CONTROL_FS_WARNING_PERCENT%"
        else
            fail "$host $path pressure" "filesystem usage unavailable"
        fi
    done

    largest_log=$(remote "$target" \
        "find /var/log/incus -xdev -type f -printf '%s\n' 2>/dev/null |
         sort -nr | head -1")
    largest_log=${largest_log:-0}
    if [[ "$largest_log" =~ ^[0-9]+$ ]] &&
            ((largest_log <= CONSOLE_LOG_WARNING_BYTES)); then
        pass "$host Incus log bound" "$largest_log bytes"
    else
        fail "$host Incus log bound" \
            "${largest_log:-unknown} > $CONSOLE_LOG_WARNING_BYTES bytes"
    fi

    instance_json=$(remote "$target" \
        "podman exec incus incus --project '$INCUS_PROJECT' \
         list --format json" 2>/dev/null) || {
        fail "$host Incus inventory" "query failed"
        continue
    }
    pending=$(jq -r \
        '[.[] | select(.config["volatile.migration.storage_handover"] ==
          "pending") | .name] | join(",")' <<<"$instance_json")
    if [[ -z "$pending" ]]; then
        pass "$host pending handover" absent
    else
        fail "$host pending handover" "$pending"
    fi

    profile_json=$(remote "$target" \
        "podman exec incus incus --project '$INCUS_PROJECT' \
         profile list --format json" 2>/dev/null) || {
        fail "$host Incus profiles" "query failed"
        continue
    }
    recovery=$(jq -r \
        '[.[] | select(.config["user.openstack.recovery_required"] != null) |
          .name] | join(",")' <<<"$profile_json")
    if [[ -z "$recovery" ]]; then
        pass "$host recovery marker" absent
    else
        fail "$host recovery marker" "$recovery"
    fi

    while IFS=$'\t' read -r instance_name uuid; do
        [[ -n "$instance_name" ]] || continue
        label="$host $instance_name/${uuid:-unknown}"
        cgroup="/sys/fs/cgroup/lxc.payload.${INCUS_PROJECT}_${instance_name}"
        pids_current=$(remote "$target" \
            "podman exec incus cat '$cgroup/pids.current'" 2>/dev/null)
        pids_max=$(remote "$target" \
            "podman exec incus cat '$cgroup/pids.max'" 2>/dev/null)
        check_ratio "$label PID pressure" "$pids_current" "$pids_max"

        memory_current=$(remote "$target" \
            "podman exec incus cat '$cgroup/memory.current'" 2>/dev/null)
        memory_max=$(remote "$target" \
            "podman exec incus cat '$cgroup/memory.max'" 2>/dev/null)
        check_ratio "$label memory pressure" "$memory_current" "$memory_max"

        swap_current=$(remote "$target" \
            "podman exec incus cat '$cgroup/memory.swap.current'" 2>/dev/null)
        swap_max=$(remote "$target" \
            "podman exec incus cat '$cgroup/memory.swap.max'" 2>/dev/null)
        if [[ "$swap_max" == 0 && "$swap_current" == 0 ]]; then
            pass "$label swap pressure" disabled
        else
            check_ratio "$label swap pressure" "$swap_current" "$swap_max"
        fi

        oom_events=$(remote "$target" \
            "podman exec incus awk '
                /^oom |^oom_kill |^oom_group_kill / {sum += \$2}
                END {print sum + 0}
             ' '$cgroup/memory.events'" 2>/dev/null)
        if [[ "$oom_events" == 0 ]]; then
            pass "$label OOM events" absent
        elif [[ "$oom_events" =~ ^[0-9]+$ ]]; then
            fail "$label OOM events" "$oom_events"
        else
            fail "$label OOM events" "unavailable"
        fi

        guest_df=$(remote "$target" \
            "timeout 15 podman exec incus incus --project '$INCUS_PROJECT' \
             exec '$instance_name' -- df -P / /run /dev/shm" 2>/dev/null) || {
            fail "$label guest filesystem pressure" "query failed"
            continue
        }
        while IFS=$'\t' read -r mount usage; do
            [[ -n "$mount" ]] || continue
            usage=${usage%\%}
            if [[ "$usage" =~ ^[0-9]+$ ]] &&
                    ((usage < INSTANCE_PRESSURE_WARNING_PERCENT)); then
                pass "$label $mount pressure" "$usage%"
            elif [[ "$usage" =~ ^[0-9]+$ ]]; then
                fail "$label $mount pressure" \
                    "$usage% >= $INSTANCE_PRESSURE_WARNING_PERCENT%"
            else
                fail "$label $mount pressure" "usage unavailable"
            fi
        done < <(awk 'NR > 1 {print $6 "\t" $5}' <<<"$guest_df")
    done < <(jq -r '
        .[] | select((.status | ascii_upcase) == "RUNNING") |
        [.name, (.config["user.openstack.uuid"] // "unknown")] | @tsv
    ' <<<"$instance_json")

    while IFS=$'\t' read -r uuid instance_name root_image state; do
        [[ -n "$uuid" && -n "$root_image" ]] || continue
        if [[ -n "${runtime_hosts[$uuid]:-}" ]]; then
            fail "duplicate BFV runtime:$uuid" \
                "volume=${root_image#volume-} hosts=${runtime_hosts[$uuid]},$host"
            continue
        fi
        runtime_hosts[$uuid]=$host
        runtime_names[$uuid]=$instance_name
        runtime_images[$uuid]=$root_image
        runtime_states[$uuid]=$state
    done < <(jq -r '
        .[] |
        (.expanded_devices.root["initial.ceph.rbd.image_name"] //
         .devices.root["initial.ceph.rbd.image_name"] // "") as $root |
        select(.config["user.openstack.uuid"] != null and $root != "") |
        [.config["user.openstack.uuid"], .name, $root,
         (.status | ascii_upcase)] | @tsv
    ' <<<"$instance_json")

    mappings=$(remote "$target" \
        "rbd device list --format json --id cinder 2>/dev/null ||
         printf '[]'" 2>/dev/null)
    while IFS= read -r image; do
        [[ -n "$image" ]] || continue
        mapping_counts[$image]=$((${mapping_counts[$image]:-0} + 1))
        if [[ -n "${mapping_owners[$image]:-}" ]]; then
            fail "duplicate KRBD mapping:$image" \
                "${mapping_owners[$image]},$host"
        else
            mapping_owners[$image]=$host
        fi
    done < <(jq -r '.[].name' <<<"$mappings")
done

attachments=$(openstack volume attachment list -f json 2>/dev/null) || {
    fail "Cinder attachment inventory" "query failed"
    attachments=[]
}

for uuid in "${!runtime_hosts[@]}"; do
    instance_name=${runtime_names[$uuid]}
    root_image=${runtime_images[$uuid]}
    root_volume=${root_image#volume-}
    runtime_host=${runtime_hosts[$uuid]}
    runtime_state=${runtime_states[$uuid]}
    label="$instance_name/$uuid root=$root_volume"

    server=$(openstack server show "$uuid" -f json 2>/dev/null) || {
        fail "$label Nova record" "missing"
        continue
    }
    nova_host=$(jq -r '."OS-EXT-SRV-ATTR:host" // empty' <<<"$server")
    nova_status=$(jq -r '.status // empty' <<<"$server")
    if [[ "$nova_host" == "$runtime_host" ]]; then
        pass "$label Nova owner" "$nova_host"
    else
        fail "$label Nova owner" \
            "runtime=$runtime_host nova=${nova_host:-missing}"
    fi

    attachment_total=$(jq -r --arg volume "$root_volume" \
        '[.[] | select(."Volume ID" == $volume)] | length' \
        <<<"$attachments")
    attachment_match=$(jq -r \
        --arg volume "$root_volume" --arg server "$uuid" \
        '[.[] | select(."Volume ID" == $volume and
            ."Server ID" == $server and .Status == "attached")] | length' \
        <<<"$attachments")
    if [[ "$attachment_total" == 1 && "$attachment_match" == 1 ]]; then
        pass "$label Cinder attachment" unique
    else
        fail "$label Cinder attachment" \
            "total=$attachment_total matching=$attachment_match"
    fi

    watcher_count=$(remote "$CONTROLLER_SSH" \
        "rbd status '$CINDER_RBD_POOL/$root_image' --id cinder \
         --format json 2>/dev/null || echo '{\"watchers\":[]}'" |
        jq '.watchers | length')
    expected_runtime=STOPPED
    expected_count=0
    if [[ "$nova_status" == ACTIVE ]]; then
        expected_runtime=RUNNING
        expected_count=1
    elif [[ "$nova_status" != SHUTOFF ]]; then
        fail "$label Nova status" \
            "expected ACTIVE or SHUTOFF, actual=${nova_status:-missing}"
        continue
    fi
    if [[ "$runtime_state" == "$expected_runtime" ]]; then
        pass "$label Incus state" "$runtime_state"
    else
        fail "$label Incus state" \
            "expected=$expected_runtime actual=$runtime_state"
    fi
    if [[ "$watcher_count" == "$expected_count" ]]; then
        pass "$label Ceph watcher" "$watcher_count"
    else
        fail "$label Ceph watcher" \
            "expected=$expected_count actual=$watcher_count"
    fi
    if [[ "${mapping_counts[$root_image]:-0}" == "$expected_count" ]]; then
        pass "$label KRBD owner" \
            "${mapping_owners[$root_image]:-absent}"
    else
        fail "$label KRBD owner" \
            "expected=$expected_count actual=${mapping_counts[$root_image]:-0}"
    fi

    while IFS= read -r port_id; do
        [[ -n "$port_id" ]] || continue
        binding=$(openstack port show "$port_id" -f value \
            -c binding_host_id 2>/dev/null || true)
        if [[ "$binding" == "$nova_host" ]]; then
            pass "$label Neutron:$port_id" "$binding"
        else
            fail "$label Neutron:$port_id" \
                "expected=$nova_host actual=${binding:-unbound}"
        fi
        ovs_owners=()
        ovs_total=0
        for node in "${nodes[@]}"; do
            host=${node%%=*}
            target=${node#*=}
            count=$(remote "$target" \
                "ovs-vsctl --data=bare --no-heading --columns=name \
                 find Interface external_ids:iface-id='$port_id' 2>/dev/null |
                 sed '/^[[:space:]]*$/d' | wc -l")
            ovs_total=$((ovs_total + count))
            ((count > 0)) && ovs_owners+=("$host:$count")
        done
        if ((ovs_total == expected_count)) &&
                { ((expected_count == 0)) ||
                  [[ "${ovs_owners[*]}" == "$nova_host:1" ]]; }; then
            pass "$label OVS:$port_id" "${ovs_owners[*]:-absent}"
        else
            fail "$label OVS:$port_id" \
                "expected=$nova_host:$expected_count actual=${ovs_owners[*]:-absent}"
        fi
    done < <(openstack port list --server "$uuid" -f value -c ID)
done

if ((failures > 0)); then
    echo "FAIL monitoring audit: $failures signal(s) require action" >&2
    exit 1
fi

echo "PASS monitoring audit"
