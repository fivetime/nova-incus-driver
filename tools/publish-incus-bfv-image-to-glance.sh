#!/usr/bin/env bash
# Convert a unified Incus image tar into a mountable ext4 BFV root image.

set -Eeuo pipefail

UNIFIED_TAR=${UNIFIED_TAR:?Set UNIFIED_TAR to the unified Incus tar}
IMAGE_NAME=${IMAGE_NAME:?Set IMAGE_NAME for the Glance BFV image}
OUTPUT=${OUTPUT:-${UNIFIED_TAR%.*}.bfv.raw}
IMAGE_SIZE_MIB=${IMAGE_SIZE_MIB:-512}
VISIBILITY=${VISIBILITY:-public}
IMAGE_STORE=${IMAGE_STORE:-}
IMAGE_IMPORT_TIMEOUT=${IMAGE_IMPORT_TIMEOUT:-600}

command -v openstack >/dev/null
command -v mkfs.ext4 >/dev/null
if [[ -n "$IMAGE_STORE" ]]; then
    command -v python3 >/dev/null
    [[ "$IMAGE_IMPORT_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || {
        echo "IMAGE_IMPORT_TIMEOUT must be a positive integer" >&2
        exit 1
    }
fi
[[ $EUID -eq 0 ]] || {
    echo "This command requires root for the loop mount" >&2
    exit 1
}
[[ "$IMAGE_SIZE_MIB" =~ ^[1-9][0-9]*$ ]] || {
    echo "IMAGE_SIZE_MIB must be a positive integer" >&2
    exit 1
}

work_dir=$(mktemp -d)
mount_dir="$work_dir/root"
loop_device=
created_image_id=
cleanup() {
    local status=$?
    if mountpoint -q "$mount_dir"; then
        umount "$mount_dir" || status=$?
    fi
    if [[ -n "$loop_device" ]]; then
        losetup -d "$loop_device" || status=$?
    fi
    if ((status != 0)) && [[ -n "$created_image_id" ]]; then
        openstack image delete "$created_image_id" >/dev/null 2>&1 || true
    fi
    rm -rf "$work_dir"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "$mount_dir" "$(dirname "$OUTPUT")"
truncate -s "${IMAGE_SIZE_MIB}MiB" "$OUTPUT"
mkfs.ext4 -q -F -L incus-rootfs "$OUTPUT"
loop_device=$(losetup --find --show "$OUTPUT")
mount "$loop_device" "$mount_dir"
tar -xf "$UNIFIED_TAR" -C "$mount_dir"

[[ -x "$mount_dir/rootfs/sbin/init" ||
   -L "$mount_dir/rootfs/sbin/init" ]] || {
    echo "Unified image does not contain rootfs/sbin/init" >&2
    exit 1
}

data_volume_fuse=false
for fuse2fs_path in usr/bin/fuse2fs bin/fuse2fs usr/sbin/fuse2fs sbin/fuse2fs; do
    if [[ -x "$mount_dir/rootfs/$fuse2fs_path" ]]; then
        data_volume_fuse=true
        break
    fi
done

# The marker is Incus-owned metadata beside rootfs, so it follows every RBD
# clone, snapshot and backup without being visible inside the guest.
printf '%s\n' '{"version":1,"state":"stable","idmap":[]}' \
    >"$mount_dir/.incus-idmap"
chmod 0600 "$mount_dir/.incus-idmap"

# Keep enough headroom for first-boot writes and filesystem metadata.
used_kib=$(du -sk "$mount_dir" | awk '{print $1}')
total_kib=$((IMAGE_SIZE_MIB * 1024))
((used_kib * 100 < total_kib * 85)) || {
    echo "Rootfs consumes more than 85% of the output image" >&2
    exit 1
}

sync
umount "$mount_dir"
losetup -d "$loop_device"
loop_device=
e2fsck -f -n "$OUTPUT"
file -s "$OUTPUT"

image_properties=()
if [[ "$data_volume_fuse" == true ]]; then
    image_properties+=(--property hw_incus_data_volume_fuse=true)
fi

openstack image delete "$IMAGE_NAME" >/dev/null 2>&1 || true
image_args=(
    "$IMAGE_NAME"
    --disk-format raw
    --container-format bare
    --property hw_incus_boot_from_volume=true
    --property hw_incus_rootfs_idmap_provenance=v1
    --property hw_incus_rootfs_layout=rootfs-directory
    "${image_properties[@]}"
)

if [[ -n "$IMAGE_STORE" ]]; then
    [[ "$IMAGE_STORE" =~ ^[A-Za-z0-9._-]+$ ]] || {
        echo "IMAGE_STORE contains unsupported characters" >&2
        exit 1
    }
    created_image_id=$(openstack image create "${image_args[@]}" --private \
        --file "$OUTPUT" -f value -c id)
    # --store accepts multiple values, so --wait terminates that option before
    # the positional image ID. Its status wait is insufficient for an already
    # active copy-image source; the store-specific poll below is authoritative.
    openstack image import --method copy-image --store "$IMAGE_STORE" \
        --wait "$created_image_id"

    deadline=$((SECONDS + IMAGE_IMPORT_TIMEOUT))
    while ((SECONDS < deadline)); do
        stores=$(openstack image show "$created_image_id" -f json | python3 -c '
import json
import sys

stores = json.load(sys.stdin).get("properties", {}).get("stores", "")
print(",".join(store.strip() for store in stores.split(",") if store.strip()))
')
        if [[ ",$stores," == *",$IMAGE_STORE,"* ]]; then
            break
        fi
        sleep 5
    done

    mapfile -t image_stores < <(
        openstack image show "$created_image_id" -f json | python3 -c '
import json
import sys

stores = json.load(sys.stdin).get("properties", {}).get("stores", "")
for store in stores.split(","):
    if store.strip():
        print(store.strip())
'
    )
    [[ " ${image_stores[*]} " == *" $IMAGE_STORE "* ]] || {
        echo "Image was not copied to requested store $IMAGE_STORE" >&2
        exit 1
    }
    for store in "${image_stores[@]}"; do
        if [[ "$store" != "$IMAGE_STORE" ]]; then
            openstack image delete --store "$store" "$created_image_id"
        fi
    done
    openstack image set "--$VISIBILITY" "$created_image_id"
else
    openstack image create "${image_args[@]}" "--$VISIBILITY" --file "$OUTPUT"
    created_image_id=$(openstack image show "$IMAGE_NAME" -f value -c id)
fi

openstack image show "$created_image_id" \
    -c id -c status -c size -c properties
created_image_id=
