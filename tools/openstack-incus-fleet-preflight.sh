#!/usr/bin/env bash
# Cross-compute production readiness and drift audit.

set -uo pipefail

COMPUTE_NODES=${COMPUTE_NODES:?Set host=ssh-target comma-separated nodes}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute audit key}
EXPECTED_INCUS_IMAGE_DIGEST=${EXPECTED_INCUS_IMAGE_DIGEST:?Set approved digest}
EXPECTED_INCUS_REVISION=${EXPECTED_INCUS_REVISION:?Set approved source revision}
REMOTE_DRIVER=${REMOTE_DRIVER:-/opt/stack/nova/nova/virt/lxd}
CONTROLLER_SSH=${CONTROLLER_SSH:-}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
HOST_PREFLIGHT=${HOST_PREFLIGHT:-$SCRIPT_DIR/openstack-incus-production-preflight.sh}
RELEASE_DRIVER=${RELEASE_DRIVER:-$SCRIPT_DIR/../nova/virt/lxd}

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)
failures=0
declare -A seen_addresses=()

if [[ ! -r "$HOST_PREFLIGHT" ]]; then
    echo "FAIL host preflight script is not readable: $HOST_PREFLIGHT" >&2
    exit 1
fi
if [[ ! -d "$RELEASE_DRIVER" ]]; then
    echo "FAIL release driver tree is not a directory: $RELEASE_DRIVER" >&2
    exit 1
fi

driver_tree_hash() {
    find "$1" -type f -name '*.py' -print0 |
        LC_ALL=C sort -z |
        xargs -0 sha256sum |
        awk '{print $1}' |
        sha256sum |
        awk '{print $1}'
}

expected_driver_hash=$(driver_tree_hash "$RELEASE_DRIVER")

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
    if [[ "$driver_hash" == "$expected_driver_hash" ]]; then
        pass "$host driver hash" "$driver_hash"
    else
        fail "$host driver hash" \
            "release=$expected_driver_hash actual=$driver_hash"
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
