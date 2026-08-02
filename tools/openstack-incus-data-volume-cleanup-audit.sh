#!/usr/bin/env bash
# Prove one completed initial-volume case left no compute-local ownership.
# Run: EVIDENCE_FILE=case.json COMPUTE_NODES=host=ssh,... \
#      SSH_IDENTITY=... SSH_KNOWN_HOSTS_FILE=... NOVA_INSTANCES_PATH=... $0

set -Eeuo pipefail

EVIDENCE_FILE=${EVIDENCE_FILE:?Set EVIDENCE_FILE from the public API E2E}
COMPUTE_NODES=${COMPUTE_NODES:?Set COMPUTE_NODES to host=ssh mappings}
SSH_IDENTITY=${SSH_IDENTITY:?Set SSH_IDENTITY to the compute audit key}
SSH_KNOWN_HOSTS_FILE=${SSH_KNOWN_HOSTS_FILE:?Set SSH_KNOWN_HOSTS_FILE}
NOVA_INSTANCES_PATH=${NOVA_INSTANCES_PATH:?Set the absolute Nova instances_path}
INCUS_PROJECT=${INCUS_PROJECT:-nova}

[[ -r "$EVIDENCE_FILE" && -f "$EVIDENCE_FILE" ]] || {
    echo "Cleanup evidence is not a readable regular file: $EVIDENCE_FILE" >&2
    exit 2
}
[[ -r "$SSH_IDENTITY" && -f "$SSH_IDENTITY" ]] || {
    echo "SSH identity is not a readable regular file: $SSH_IDENTITY" >&2
    exit 2
}
[[ -r "$SSH_KNOWN_HOSTS_FILE" && -f "$SSH_KNOWN_HOSTS_FILE" ]] || {
    echo "SSH known_hosts is not a readable regular file" >&2
    exit 2
}
[[ "$NOVA_INSTANCES_PATH" == /* && "$NOVA_INSTANCES_PATH" != *$'\n'* ]] || {
    echo "NOVA_INSTANCES_PATH must be one absolute path" >&2
    exit 2
}

mapfile -t evidence < <(python3 - "$EVIDENCE_FILE" <<'PY'
import json
import re
import sys
import uuid

with open(sys.argv[1], encoding="utf-8") as stream:
    data = json.load(stream)
if data.get("schema") != 1:
    raise SystemExit("Unsupported cleanup evidence schema")
server_uuid = str(uuid.UUID(data["server_uuid"]))
instance_name = data["instance_name"]
if not re.fullmatch(r"instance-[0-9a-f]+", instance_name):
    raise SystemExit("Invalid Nova instance name in cleanup evidence")
volume_ids = list(data.get("data_volume_ids") or [])
if data.get("root_volume_id"):
    volume_ids.append(data["root_volume_id"])
normalized = [str(uuid.UUID(item)) for item in volume_ids]
if len(normalized) != len(set(normalized)):
    raise SystemExit("Duplicate volume UUID in cleanup evidence")
print(server_uuid)
print(instance_name)
for item in sorted(normalized):
    print(item)
PY
)
for index in "${!evidence[@]}"; do
    evidence[index]=${evidence[index]%$'\r'}
done

((${#evidence[@]} >= 2)) || {
    echo "Cleanup evidence has no server identity" >&2
    exit 2
}
server_uuid=${evidence[0]}
instance_name=${evidence[1]}
volume_ids=("${evidence[@]:2}")
SSH=(ssh -i "$SSH_IDENTITY" -o BatchMode=yes \
    -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$SSH_KNOWN_HOSTS_FILE")

remote() {
    local target=$1
    shift
    "${SSH[@]}" "$target" "$@"
}

IFS=, read -r -a nodes <<<"$COMPUTE_NODES"
((${#nodes[@]} > 0)) || {
    echo "COMPUTE_NODES contains no entries" >&2
    exit 2
}

for entry in "${nodes[@]}"; do
    [[ "$entry" == *=* && -n "${entry%%=*}" && -n "${entry#*=}" ]] || {
        echo "Invalid COMPUTE_NODES entry: $entry" >&2
        exit 2
    }
    host=${entry%%=*}
    target=${entry#*=}

    instance_json=$(remote "$target" podman exec incus incus \
        --project "$INCUS_PROJECT" list --format json) || {
        echo "$host could not query the Incus instance inventory" >&2
        exit 1
    }
    profile_json=$(remote "$target" podman exec incus incus \
        --project "$INCUS_PROJECT" profile list --format json) || {
        echo "$host could not query the Incus profile inventory" >&2
        exit 1
    }
    INSTANCE_JSON="$instance_json" PROFILE_JSON="$profile_json" \
        EXPECTED_INSTANCE_NAME="$instance_name" python3 - <<'PY'
import json
import os

name = os.environ["EXPECTED_INSTANCE_NAME"]
instances = json.loads(os.environ["INSTANCE_JSON"])
profiles = json.loads(os.environ["PROFILE_JSON"])
if any(str(item.get("name") or "") == name for item in instances):
    raise SystemExit("retained exact Incus instance {}".format(name))
if any(str(item.get("name") or "") == name for item in profiles):
    raise SystemExit("retained exact Incus profile {}".format(name))
PY
    remote "$target" test ! -e \
        "$NOVA_INSTANCES_PATH/incus-volume-journal/$server_uuid" || {
        echo "$host retained Cinder connector journal for $server_uuid" >&2
        exit 1
    }

    mapping_json=$(remote "$target" rbd device list --format json) || {
        echo "$host could not query the local KRBD mapping table" >&2
        exit 1
    }
    MAPPING_JSON="$mapping_json" \
        EXPECTED_VOLUME_IDS="$(printf '%s\n' "${volume_ids[@]}")" \
        python3 - <<'PY'
import json
import os

rows = json.loads(os.environ["MAPPING_JSON"])
expected = {
    "volume-" + item
    for item in os.environ.get("EXPECTED_VOLUME_IDS", "").splitlines()
    if item
}
retained = sorted(
    str(row.get("name") or "")
    for row in rows
    if str(row.get("name") or "") in expected
)
if retained:
    raise SystemExit("retained exact KRBD mappings: {}".format(retained))
PY
    echo "PASS $host initial-volume local cleanup"
done

echo "PASS exact initial-volume cleanup server=$server_uuid volumes=${#volume_ids[@]}"
