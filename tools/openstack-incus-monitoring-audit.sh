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
CONTROL_FS_WARNING_PERCENT=${CONTROL_FS_WARNING_PERCENT:-80}
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
[[ "$CONSOLE_LOG_WARNING_BYTES" =~ ^[1-9][0-9]*$ ]] ||
    { echo "CONSOLE_LOG_WARNING_BYTES must be positive" >&2; exit 2; }

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
