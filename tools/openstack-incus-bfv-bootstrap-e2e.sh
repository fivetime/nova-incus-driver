#!/usr/bin/env bash
# Validate Nova keypair, metadata, user-data and config-drive delivery to BFV.

set -euo pipefail

IMAGE=${IMAGE:?Set IMAGE to a raw rootfs-directory Glance image}
FLAVOR=${FLAVOR:-ds512M}
NETWORK=${NETWORK:-private}
VOLUME_SIZE=${VOLUME_SIZE:-5}
VOLUME_TYPE=${VOLUME_TYPE:-ceph}
SOURCE_HOST=${SOURCE_HOST:-incus-node-01}
SOURCE_SSH=${SOURCE_SSH:-root@10.224.0.21}
CONTROLLER_SSH=${CONTROLLER_SSH:-root@10.224.0.21}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}
TIMEOUT=${TIMEOUT:-600}
NAME=${NAME:-incus-bfv-bootstrap-$RANDOM}
KEY_NAME=${KEY_NAME:-$NAME-key}
REMOTE_USER_DATA=${REMOTE_USER_DATA:-/tmp/$NAME-user-data}
REMOTE_KEY=${REMOTE_KEY:-/tmp/$NAME-key}

SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o StrictHostKeyChecking=no)
server_id=
volume_id=
instance_name=

remote() {
    local host=$1
    shift
    "${SSH[@]}" "$host" "$@"
}

openstack() {
    local command_line
    printf -v command_line '%q ' "$@"
    remote "$CONTROLLER_SSH" \
        "source $CONTROLLER_OPENRC >/dev/null 2>&1; openstack $command_line"
}

incus() {
    local command_line
    printf -v command_line '%q ' "$@"
    remote "$SOURCE_SSH" \
        "podman exec incus incus --project nova $command_line"
}

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

wait_value() {
    local expected=$1
    shift
    local deadline=$((SECONDS + TIMEOUT)) current
    while ((SECONDS < deadline)); do
        current=$("$@" 2>/dev/null || true)
        [[ "$current" == "$expected" ]] && return 0
        [[ "$current" == ERROR ]] && break
        sleep 2
    done
    fail "timed out waiting for $expected (current: ${current:-missing})"
}

server_status() {
    openstack server show "$server_id" -f value -c status
}

volume_status() {
    openstack volume show "$volume_id" -f value -c status
}

cleanup() {
    local status=$?
    [[ -z "$server_id" ]] ||
        openstack server delete --wait "$server_id" >/dev/null 2>&1 || true
    if [[ -n "$volume_id" ]]; then
        wait_value available volume_status >/dev/null 2>&1 || true
        openstack volume delete "$volume_id" >/dev/null 2>&1 || true
    fi
    openstack keypair delete "$KEY_NAME" >/dev/null 2>&1 || true
    remote "$CONTROLLER_SSH" rm -f \
        "$REMOTE_USER_DATA" "$REMOTE_KEY" "$REMOTE_KEY.pub" \
        >/dev/null 2>&1 || true
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

marker="bootstrap-$RANDOM-$RANDOM"
remote "$CONTROLLER_SSH" \
    "printf '%s\n' '#!/bin/sh' \
     'printf %s $marker > /root/nova-user-data-marker' \
     > '$REMOTE_USER_DATA'; chmod 0600 '$REMOTE_USER_DATA'"
remote "$CONTROLLER_SSH" \
    "rm -f '$REMOTE_KEY' '$REMOTE_KEY.pub'; \
     ssh-keygen -q -t ed25519 -N '' -f '$REMOTE_KEY'"
openstack keypair create --public-key "$REMOTE_KEY.pub" "$KEY_NAME" \
    >/dev/null
public_key=$(remote "$CONTROLLER_SSH" cat "$REMOTE_KEY.pub")

volume_id=$(openstack volume create --image "$IMAGE" --size "$VOLUME_SIZE" \
    --type "$VOLUME_TYPE" "$NAME-root" -f value -c id)
wait_value available volume_status
server_id=$(openstack --os-compute-api-version 2.74 server create \
    --flavor "$FLAVOR" --volume "$volume_id" --network "$NETWORK" \
    --host "$SOURCE_HOST" --key-name "$KEY_NAME" \
    --property release_gate=bootstrap --user-data "$REMOTE_USER_DATA" \
    --config-drive true "$NAME" -f value -c id)
wait_value ACTIVE server_status
instance_name=$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)

deadline=$((SECONDS + TIMEOUT))
while ((SECONDS < deadline)); do
    value=$(incus exec "$instance_name" -- \
        cat /root/nova-user-data-marker 2>/dev/null || true)
    [[ "$value" == "$marker" ]] && break
    sleep 2
done
[[ "${value:-}" == "$marker" ]] || fail "cloud-init did not run user-data"

incus exec "$instance_name" -- grep -Fq "$public_key" \
    /root/.ssh/authorized_keys ||
    fail "Nova keypair is absent from root authorized_keys"
metadata=$(incus exec "$instance_name" -- \
    cat /config-drive/openstack/latest/meta_data.json)
python3 -c 'import json,sys
data = json.load(sys.stdin)
assert data["uuid"] == sys.argv[1]
assert data["meta"]["release_gate"] == "bootstrap"
assert sys.argv[2] in data["public_keys"]
' "$server_id" "$KEY_NAME" <<<"$metadata"
if incus exec "$instance_name" -- touch /config-drive/write-must-fail \
        >/dev/null 2>&1; then
    fail "config-drive is writable"
fi

openstack server set --property runtime_metadata=updated "$server_id"
[[ "$(openstack server show "$server_id" -f json -c properties |
    python3 -c 'import json,sys
print(json.load(sys.stdin)["properties"]["runtime_metadata"])
')" == updated ]]

openstack server reboot "$server_id"
wait_value ACTIVE server_status
[[ "$(incus exec "$instance_name" -- \
    cat /root/nova-user-data-marker)" == "$marker" ]]

trap - EXIT INT TERM
openstack server delete --wait "$server_id"
server_id=
wait_value available volume_status
openstack volume delete "$volume_id"
volume_id=
openstack keypair delete "$KEY_NAME"
remote "$CONTROLLER_SSH" rm -f "$REMOTE_USER_DATA"
remote "$CONTROLLER_SSH" rm -f "$REMOTE_KEY" "$REMOTE_KEY.pub"

echo "PASS BFV keypair, metadata, user-data, config-drive and soft reboot"
