#!/usr/bin/env bash
# Prove that Glance-to-Cinder BFV provisioning uses an RBD COW clone.

set -Eeuo pipefail

IMAGE=${IMAGE:-alpine-3.21-criu-bfv-fuse}
VOLUME_TYPE=${VOLUME_TYPE:-ceph}
CINDER_POOL=${CINDER_POOL:-cinder-volumes-rbd-pool}
CINDER_USER=${CINDER_USER:-cinder}
SIZE=${SIZE:-2}
NAME=${NAME:-incus-bfv-cow-e2e-$RANDOM}
TIMEOUT=${TIMEOUT:-300}

volume_id=

wait_absent() {
    local deadline=$((SECONDS + TIMEOUT))
    while ((SECONDS < deadline)); do
        if ! openstack volume show "$volume_id" >/dev/null 2>&1 &&
                ! rbd --id "$CINDER_USER" --pool "$CINDER_POOL" \
                    info "volume-$volume_id" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
}

cleanup() {
    if [[ -n "$volume_id" ]]; then
        openstack volume delete "$volume_id" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

direct_url=$(openstack image show "$IMAGE" -f json | \
    jq -er '.properties.direct_url')
[[ "$direct_url" == rbd://* ]] || {
    echo "Glance image does not expose an RBD direct URL" >&2
    exit 1
}

glance_pool=$(python3 - "$direct_url" <<'PY'
import sys
from urllib.parse import urlsplit

parts = [part for part in urlsplit(sys.argv[1]).path.split('/') if part]
if len(parts) < 3:
    raise SystemExit('invalid Glance RBD direct URL')
print(parts[0])
PY
)

started=$SECONDS
volume_id=$(openstack volume create --image "$IMAGE" --size "$SIZE" \
    --type "$VOLUME_TYPE" "$NAME" -f value -c id)
deadline=$((SECONDS + TIMEOUT))
while ((SECONDS < deadline)); do
    status=$(openstack volume show "$volume_id" -f value -c status \
        2>/dev/null || true)
    [[ "$status" == available ]] && break
    [[ "$status" == error* ]] && {
        openstack volume show "$volume_id"
        exit 1
    }
    sleep 2
done
[[ "${status:-}" == available ]]

info=$(rbd --id "$CINDER_USER" --pool "$CINDER_POOL" \
    info "volume-$volume_id" --format json)
parent_pool=$(jq -r '.parent.pool // empty' <<<"$info")
parent_image=$(jq -r '.parent.image // empty' <<<"$info")
parent_snapshot=$(jq -r '.parent.snapshot // empty' <<<"$info")
overlap=$(jq -r '.parent.overlap // .overlap // 0' <<<"$info")
[[ "$parent_pool" == "$glance_pool" ]]
[[ -n "$parent_image" && -n "$parent_snapshot" ]]
((overlap > 0))

openstack volume delete "$volume_id"
wait_absent
elapsed=$((SECONDS - started))
trap - EXIT

echo "PASS BFV RBD COW clone volume=$volume_id parent=$parent_pool/$parent_image@$parent_snapshot overlap=$overlap elapsed=${elapsed}s"
