#!/usr/bin/env bash
# Validate Nova 2.97 share attach through Manila and an Incus container.

set -euo pipefail

SERVER=${SERVER:?set SERVER to an existing stopped test server}
SHARE=${SHARE:?set SHARE to an available Manila share}
COMPUTE_SSH=${COMPUTE_SSH:?set COMPUTE_SSH to the server compute host}
BACKEND_SSH=${BACKEND_SSH:-}
BACKEND_PATH=${BACKEND_PATH:-}
TAG=${TAG:-tenant-data}
TIMEOUT=${TIMEOUT:-120}
INCUS_PROJECT=${INCUS_PROJECT:-nova}

server_id=$(openstack server show "$SERVER" -f value -c id)
share_id=$(openstack share show "$SHARE" -f value -c id)
instance_name=$(openstack server show "$server_id" -f value \
    -c OS-EXT-SRV-ATTR:instance_name)
token=$(openstack token issue -f value -c id)
endpoint=$(openstack endpoint list --service nova --interface public \
    -f value -c URL | head -1)
project_id=$(openstack server show "$server_id" -f value -c project_id)
endpoint=${endpoint//\%\(project_id\)s/$project_id}
shares_url="$endpoint/servers/$server_id/shares"

api() {
    local method=$1 url=$2 data=${3:-}
    local args=(-fsS -X "$method" -H "X-Auth-Token: $token"
        -H "OpenStack-API-Version: compute 2.97")
    if [[ -n "$data" ]]; then
        args+=(-H "Content-Type: application/json" -d "$data")
    fi
    curl "${args[@]}" "$url"
}

wait_server() {
    local expected=$1 current deadline=$((SECONDS + TIMEOUT))
    while ((SECONDS < deadline)); do
        current=$(openstack server show "$server_id" -f value -c status)
        [[ "$current" == "$expected" ]] && return
        [[ "$current" == ERROR ]] && break
        sleep 2
    done
    echo "Server did not reach $expected (current=${current:-missing})" >&2
    return 1
}

wait_share_status() {
    local expected=$1 body deadline=$((SECONDS + TIMEOUT))
    while ((SECONDS < deadline)); do
        body=$(api GET "$shares_url")
        if python3 -c \
                'import json,sys; d=json.load(sys.stdin); sid=sys.argv[1]; expected=sys.argv[2]; raise SystemExit(0 if any(x["share_id"] == sid and x["status"] == expected for x in d["shares"]) else 1)' \
                "$share_id" "$expected" <<<"$body"; then
            return
        fi
        sleep 2
    done
    echo "Share mapping did not reach $expected: $body" >&2
    return 1
}

cleanup() {
    openstack server stop "$server_id" >/dev/null 2>&1 || true
    wait_server SHUTOFF >/dev/null 2>&1 || true
    api DELETE "$shares_url/$share_id" >/dev/null 2>&1 || true
}
trap cleanup EXIT

[[ "$(openstack server show "$server_id" -f value -c status)" == SHUTOFF ]]
api POST "$shares_url" \
    "{\"share\":{\"share_id\":\"$share_id\",\"tag\":\"$TAG\"}}" >/dev/null
wait_share_status inactive

openstack server start "$server_id"
wait_server ACTIVE
marker="MANILA_E2E_${server_id}"
printf -v guest_command "printf %%s %q > %q" \
    "$marker" "/mnt/manila/$TAG/verified.txt"
printf -v remote_command "incus exec %q --project %q -- sh -c %q" \
    "$instance_name" "$INCUS_PROJECT" "$guest_command"
ssh "$COMPUTE_SSH" "$remote_command"
if [[ -n "$BACKEND_SSH" && -n "$BACKEND_PATH" ]]; then
    [[ "$(ssh "$BACKEND_SSH" "cat '$BACKEND_PATH/verified.txt'")" == "$marker" ]]
fi

openstack server stop "$server_id"
wait_server SHUTOFF
api DELETE "$shares_url/$share_id" >/dev/null
deadline=$((SECONDS + TIMEOUT))
while api GET "$shares_url" | grep -Fq "$share_id"; do
    ((SECONDS < deadline)) || exit 1
    sleep 2
done
printf -v remote_command \
    "findmnt -rn -t nfs,nfs4 -o TARGET | grep -F %q" \
    "/incus-shares/$server_id/$share_id"
! ssh "$COMPUTE_SSH" "$remote_command"
printf -v remote_command "incus profile show %q | grep -F %q" \
    "$instance_name" "manila-$share_id"
! ssh "$COMPUTE_SSH" "$remote_command"

trap - EXIT
echo "PASS server=$server_id share=$share_id tag=$TAG"
