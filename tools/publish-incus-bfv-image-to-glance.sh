#!/usr/bin/env bash
# Convert a unified Incus image tar into a mountable ext4 BFV root image.

set -Eeuo pipefail

UNIFIED_TAR=${UNIFIED_TAR:?Set UNIFIED_TAR to the unified Incus tar}
IMAGE_NAME=${IMAGE_NAME:?Set IMAGE_NAME for the Glance BFV image}
OUTPUT=${OUTPUT:-${UNIFIED_TAR%.*}.bfv.raw}
IMAGE_SIZE_MIB=${IMAGE_SIZE_MIB:-512}
VISIBILITY=${VISIBILITY:-public}

command -v openstack >/dev/null
command -v mkfs.ext4 >/dev/null
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
cleanup() {
    local status=$?
    if mountpoint -q "$mount_dir"; then
        umount "$mount_dir" || status=$?
    fi
    if [[ -n "$loop_device" ]]; then
        losetup -d "$loop_device" || status=$?
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

openstack image delete "$IMAGE_NAME" >/dev/null 2>&1 || true
openstack image create "$IMAGE_NAME" \
    "--$VISIBILITY" --disk-format raw --container-format bare \
    --property hw_incus_boot_from_volume=true \
    --property hw_incus_rootfs_layout=rootfs-directory \
    --file "$OUTPUT"
openstack image show "$IMAGE_NAME" -c id -c status -c size -c properties
