#!/usr/bin/env bash
# Cross-compute production readiness and drift audit.

set -uo pipefail

COMPUTE_NODES=${COMPUTE_NODES:?Set host=ssh-target comma-separated nodes}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute audit key}
EXPECTED_INCUS_IMAGE_DIGEST=${EXPECTED_INCUS_IMAGE_DIGEST:?Set approved digest}
EXPECTED_INCUS_REVISION=${EXPECTED_INCUS_REVISION:?Set approved source revision}
REMOTE_DRIVER=${REMOTE_DRIVER:-/opt/stack/nova/nova/virt/lxd}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
HOST_PREFLIGHT=${HOST_PREFLIGHT:-$SCRIPT_DIR/openstack-incus-production-preflight.sh}

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)
failures=0
reference_driver_hash=
declare -A seen_addresses=()

if [[ ! -r "$HOST_PREFLIGHT" ]]; then
    echo "FAIL host preflight script is not readable: $HOST_PREFLIGHT" >&2
    exit 1
fi

pass() {
    printf 'PASS %-38s %s\n' "$1" "${2:-}"
}

fail() {
    printf 'FAIL %-38s %s\n' "$1" "$2" >&2
    failures=$((failures + 1))
}

remote() {
    local target=$1
    shift
    "${SSH[@]}" "$target" "$@"
}

IFS=, read -ra nodes <<<"$COMPUTE_NODES"
for node in "${nodes[@]}"; do
    host=${node%%=*}
    target=${node#*=}
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

    driver_hash=$(remote "$target" \
        "find '$REMOTE_DRIVER' -type f -name '*.py' -print0 | \
         LC_ALL=C sort -z | xargs -0 sha256sum | awk '{print \$1}' | \
         sha256sum | awk '{print \$1}'" \
        2>/dev/null)
    if [[ -z "$reference_driver_hash" ]]; then
        reference_driver_hash=$driver_hash
        pass "$host driver hash" "$driver_hash"
    elif [[ "$driver_hash" == "$reference_driver_hash" ]]; then
        pass "$host driver hash" "$driver_hash"
    else
        fail "$host driver hash" \
            "expected=$reference_driver_hash actual=$driver_hash"
    fi

    https_address=$(remote "$target" \
        "podman exec incus incus config get core.https_address" 2>/dev/null)
    if [[ -n "${seen_addresses[$https_address]:-}" ]]; then
        fail "$host migration address" \
            "duplicate of ${seen_addresses[$https_address]}: $https_address"
    else
        seen_addresses[$https_address]=$host
        pass "$host migration address" "$https_address"
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
    if [[ "$hypervisor_state" == up && "$hypervisor_type" == lxd ]]; then
        pass "$host hypervisor" "up/lxd"
    else
        fail "$host hypervisor" \
            "expected up/lxd, actual=$hypervisor_state/$hypervisor_type"
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
    fi

    ovn_agents=$(openstack network agent list --host "$host" \
        -f json 2>/dev/null)
    if jq -e \
            'any(.[]; .Binary == "ovn-controller" and
             .Alive == true and .State == true)' \
            <<<"$ovn_agents" >/dev/null; then
        pass "$host OVN controller" alive
    else
        fail "$host OVN controller" "no alive/enabled ovn-controller agent"
    fi
done

cinder_services=$(openstack volume service list -f value \
    -c Binary -c Host -c Status -c State 2>/dev/null)
if awk '$3 != "enabled" || $4 != "up" {exit 1}' <<<"$cinder_services"; then
    pass "Cinder services" "all enabled/up"
else
    fail "Cinder services" "one or more services are not enabled/up"
fi
for backend in '@ceph' 'cinder-scheduler'; do
    if grep -q "$backend" <<<"$cinder_services"; then
        pass "Cinder required service:$backend"
    else
        fail "Cinder required service:$backend" "missing"
    fi
done

if ((failures > 0)); then
    echo "FAIL fleet preflight: $failures check(s) failed" >&2
    exit 1
fi

echo "PASS fleet preflight"
