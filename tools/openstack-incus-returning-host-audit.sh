#!/usr/bin/env bash
# Fail-closed ownership audit before admitting a returning Incus compute.

set -euo pipefail

RETURNING_HOST=${RETURNING_HOST:?Set RETURNING_HOST to the Nova host name}
RETURNING_SSH=${RETURNING_SSH:?Set RETURNING_SSH to the SSH target}
CONTROLLER_SSH=${CONTROLLER_SSH:?Set CONTROLLER_SSH}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute audit key}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
INCUS_PROJECT=${INCUS_PROJECT:-nova}
CINDER_RBD_POOL=${CINDER_RBD_POOL:-cinder-volumes-rbd-pool}

SSH=(ssh -n -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)
failures=0

pass() {
    printf 'PASS %-36s %s\n' "$1" "${2:-}"
}

fail() {
    printf 'FAIL %-36s %s\n' "$1" "$2" >&2
    failures=$((failures + 1))
}

remote() {
    "${SSH[@]}" "$RETURNING_SSH" "$@"
}

openstack() {
    local command_line
    printf -v command_line '%q ' "$@"
    "${SSH[@]}" "$CONTROLLER_SSH" \
        "source $CONTROLLER_OPENRC >/dev/null 2>&1; openstack $command_line"
}

if remote /usr/local/sbin/openstack-incus-compute-admission check \
        >/dev/null 2>&1; then
    fail "returning-host quarantine" "host is already admitted"
else
    pass "returning-host quarantine" enforced
fi

compute_state=$(remote \
    "systemctl is-active devstack@n-cpu.service 2>/dev/null || true")
if [[ "$compute_state" == active ]]; then
    fail "nova-compute service" "must not run before reconciliation"
else
    pass "nova-compute service" "${compute_state:-inactive}"
fi

service_status=$(openstack compute service list --service nova-compute \
    --host "$RETURNING_HOST" -f value -c Status 2>/dev/null || true)
if [[ "$service_status" == disabled ]]; then
    pass "Nova scheduling" disabled
else
    fail "Nova scheduling" "disable $RETURNING_HOST before audit"
fi

mapfile -t records < <(remote \
    "podman exec incus incus --project '$INCUS_PROJECT' list --format json" |
    jq -r '.[] | select(.config["user.openstack.uuid"] != null) |
        [.name, .status, .config["user.openstack.uuid"]] | @tsv')

for record in "${records[@]}"; do
    IFS=$'\t' read -r instance_name status instance_uuid <<<"$record"
    label="$instance_name/$instance_uuid"
    if [[ "${status,,}" == stopped ]]; then
        pass "$label runtime" stopped
    else
        fail "$label runtime" "returning host has $status workload"
        continue
    fi

    nova_host=$(openstack server show "$instance_uuid" -f value \
        -c OS-EXT-SRV-ATTR:host 2>/dev/null || true)
    nova_status=$(openstack server show "$instance_uuid" -f value \
        -c status 2>/dev/null || true)
    if [[ -z "$nova_host" ]]; then
        fail "$label Nova record" missing
        continue
    fi

    root_image=$(remote \
        "podman exec incus incus query \
         '/1.0/instances/$instance_name?project=$INCUS_PROJECT&recursion=1'" |
        jq -r '.expanded_devices.root[
            "initial.ceph.rbd.image_name"] // empty')

    if [[ "$nova_host" == "$RETURNING_HOST" ]]; then
        pass "$label ownership" "local authoritative record"
        continue
    fi

    pass "$label ownership" "stale; authoritative host=$nova_host"
    port_mismatch=
    while IFS= read -r port_id; do
        binding_host=$(openstack port show "$port_id" -f value \
            -c binding_host_id 2>/dev/null || true)
        if [[ "$binding_host" != "$nova_host" ]]; then
            port_mismatch+="${port_id}:${binding_host:-unbound} "
        fi
    done < <(openstack port list --server "$instance_uuid" -f value -c ID)
    if [[ -z "$port_mismatch" ]]; then
        pass "$label Neutron binding" "$nova_host"
    else
        fail "$label Neutron binding" "not owned by $nova_host: $port_mismatch"
    fi

    if [[ -z "$root_image" ]]; then
        fail "$label root" "stale local-root instance cannot be evacuated"
        continue
    fi
    root_volume=${root_image#volume-}
    attachments=$(openstack volume attachment list -f json)
    attachment_total=$(jq -r --arg volume "$root_volume" \
        '[.[] | select(."Volume ID" == $volume)] | length' <<<"$attachments")
    attachment_match=$(jq -r \
        --arg volume "$root_volume" --arg server "$instance_uuid" \
        '[.[] | select(."Volume ID" == $volume and
            ."Server ID" == $server and .Status == "attached")] | length' \
        <<<"$attachments")
    if [[ "$attachment_total" == 1 && "$attachment_match" == 1 ]]; then
        pass "$label Cinder attachment" "single attached root"
    else
        fail "$label Cinder attachment" \
            "total=$attachment_total matching=$attachment_match"
    fi

    local_mapping=$(remote \
        "rbd device list --format json --id cinder 2>/dev/null || echo '[]'" |
        jq -r --arg image "$root_image" \
            '.[] | select(.name == $image) | .device')
    if [[ -z "$local_mapping" ]]; then
        pass "$label local RBD mapping" absent
    else
        fail "$label local RBD mapping" "$local_mapping"
    fi

    image_id=$(remote \
        "rados --id cinder -p '$CINDER_RBD_POOL' get \
         'rbd_id.$root_image' - 2>/dev/null | tail -c +5" || true)
    if [[ ! "$image_id" =~ ^[0-9a-f]+$ ]]; then
        fail "$label Ceph watcher" "cannot resolve RBD image ID"
        continue
    fi
    watcher_output=$(remote \
        "rados --id cinder -p '$CINDER_RBD_POOL' listwatchers \
         'rbd_header.$image_id'" 2>/dev/null) || {
        fail "$label Ceph watcher" "cannot query RBD header"
        continue
    }
    watcher_count=$(sed '/^[[:space:]]*$/d' <<<"$watcher_output" | wc -l)
    if [[ "$nova_status" == ACTIVE ]]; then
        expected_watchers=1
    elif [[ "$nova_status" == SHUTOFF ]]; then
        expected_watchers=0
    else
        fail "$label Nova status" \
            "expected ACTIVE or SHUTOFF, actual=${nova_status:-missing}"
        continue
    fi
    if [[ "$watcher_count" == "$expected_watchers" ]]; then
        pass "$label Ceph watcher" \
            "$watcher_count watcher(s) for $nova_status target"
    else
        fail "$label Ceph watcher" \
            "expected=$expected_watchers actual=$watcher_count"
    fi
done

if ((failures > 0)); then
    echo "FAIL returning-host audit: $failures check(s) failed" >&2
    exit 1
fi

echo "PASS returning-host ownership audit; host may be explicitly admitted"
