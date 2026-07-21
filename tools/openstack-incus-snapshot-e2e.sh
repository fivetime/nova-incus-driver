#!/usr/bin/env bash
# Validate snapshot-to-Glance and cross-compute restore without leaked objects.

set -euo pipefail

IMAGE=${IMAGE:-alpine-3.21-cloud-incus-criu}
FLAVOR=${FLAVOR:-ds512M}
NETWORK=${NETWORK:-public}
SOURCE_HOST=${SOURCE_HOST:-incus-node-01}
DEST_HOST=${DEST_HOST:-incus-node-02}
SOURCE_SSH=${SOURCE_SSH:-root@10.224.0.21}
DEST_SSH=${DEST_SSH:-root@10.224.0.17}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
NAME=${NAME:-incus-snapshot-e2e-$RANDOM}
TIMEOUT=${TIMEOUT:-240}

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)
source_id=
restore_id=
snapshot_id=
source_instance=
restore_instance=
snapshot_name="${NAME}-image"
restore_name="${NAME}-restore"

remote() {
    local host=$1
    shift
    "${SSH[@]}" "$host" "$@"
}

wait_port_active() {
    local server=$1 deadline=$((SECONDS + TIMEOUT))
    while ((SECONDS < deadline)); do
        if openstack port list --server "$server" -f json |
            python3 -c 'import json,sys
ports = json.load(sys.stdin)
raise SystemExit(not ports or any(p["Status"] != "ACTIVE" for p in ports))
'; then
            return 0
        fi
        sleep 2
    done
    openstack port list --server "$server"
    echo "Neutron ports did not become ACTIVE" >&2
    return 1
}

cleanup() {
    if [[ -n "$restore_id" ]]; then
        openstack server delete --wait "$restore_id" >/dev/null 2>&1 || true
    fi
    if [[ -n "$source_id" ]]; then
        openstack server delete --wait "$source_id" >/dev/null 2>&1 || true
    fi
    if [[ -n "$snapshot_id" ]]; then
        openstack image delete "$snapshot_id" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

source_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --image "$IMAGE" --network "$NETWORK" \
    --host "$SOURCE_HOST" --wait "$NAME" -f value -c id)
source_instance=$(openstack server show "$source_id" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)
marker="snapshot-$source_id"
remote "$SOURCE_SSH" \
    "incus exec '$source_instance' -- sh -c \
     'printf %s \"$marker\" > /root/nova-snapshot-marker; sync'"

snapshots_before=$(remote "$SOURCE_SSH" \
    "incus snapshot list '$source_instance' --format csv -c n | sort")
images_before=$(remote "$SOURCE_SSH" \
    "incus image list --format csv -c f | sort")

snapshot_id=$(openstack server image create --name "$snapshot_name" \
    --wait "$source_id" -f value -c id | sed '/^[[:space:]]*$/d' | tail -n1)
[[ "$(openstack image show "$snapshot_id" -f value -c status)" == active ]]
[[ "$(openstack image show "$snapshot_id" -f value -c disk_format)" == raw ]]
[[ "$(openstack image show "$snapshot_id" -f value -c container_format)" == bare ]]
[[ "$(openstack server show "$source_id" -f value -c status)" == ACTIVE ]]

snapshots_after=$(remote "$SOURCE_SSH" \
    "incus snapshot list '$source_instance' --format csv -c n | sort")
images_after=$(remote "$SOURCE_SSH" \
    "incus image list --format csv -c f | sort")
[[ "$snapshots_after" == "$snapshots_before" ]]
[[ "$images_after" == "$images_before" ]]

restore_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --image "$snapshot_id" --network "$NETWORK" \
    --host "$DEST_HOST" --wait "$restore_name" -f value -c id)
restore_instance=$(openstack server show "$restore_id" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)
[[ "$(openstack server show "$restore_id" -f value \
    -c OS-EXT-SRV-ATTR:host)" == "$DEST_HOST" ]]
[[ "$(remote "$DEST_SSH" \
    "incus exec '$restore_instance' -- cat /root/nova-snapshot-marker")" == \
    "$marker" ]]
wait_port_active "$restore_id"
port_hosts=$(openstack port list --server "$restore_id" -f value -c ID |
    xargs -r -n1 openstack port show -f value -c binding_host_id)
[[ -n "$port_hosts" ]] && ! grep -qvx "$DEST_HOST" <<<"$port_hosts"

trap - EXIT INT TERM
openstack server delete --wait "$restore_id" "$source_id"
restore_id=
source_id=
openstack image delete "$snapshot_id"
snapshot_id=

echo "PASS source=$source_instance snapshot=Glance restore=$restore_instance host=$DEST_HOST network=ACTIVE"
