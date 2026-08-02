#!/usr/bin/env bash
# Export an Incus container image as a unified rootfs tar and upload to Glance.

set -euo pipefail

SOURCE=${SOURCE:-images:ubuntu/noble/cloud}
IMAGE_NAME=${IMAGE_NAME:-ubuntu-noble-24.04-cloud-incus}
WORK_DIR=${WORK_DIR:-/opt/stack/data/incus-glance-images/$IMAGE_NAME}
LOCAL_ALIAS=${LOCAL_ALIAS:-glance-build-$IMAGE_NAME}
PREINSTALL_SSH=${PREINSTALL_SSH:-false}
PREINSTALL_PACKAGES=${PREINSTALL_PACKAGES:-}

command -v incus >/dev/null
command -v openstack >/dev/null
command -v unsquashfs >/dev/null

mkdir -p "$WORK_DIR"
incus image delete "$LOCAL_ALIAS" >/dev/null 2>&1 || true
if [[ "$SOURCE" == local:* ]]; then
    source_fingerprint=$(incus query \
        "/1.0/images/aliases/${SOURCE#local:}" | jq -r .target)
    incus image alias create "$LOCAL_ALIAS" "$source_fingerprint"
else
    incus image copy "$SOURCE" local: --alias "$LOCAL_ALIAS"
fi
incus image export "$LOCAL_ALIAS" "$WORK_DIR/export"

rm -rf "$WORK_DIR/unified"
mkdir -p "$WORK_DIR/unified/rootfs"
tar -xf "$WORK_DIR/export" -C "$WORK_DIR/unified"

root_file="$WORK_DIR/export.root"
case "$(file -b "$root_file")" in
    Squashfs*)
        unsquashfs -f -d "$WORK_DIR/unified/rootfs" "$root_file" >/dev/null
        ;;
    *gzip*|*tar*)
        tar -xf "$root_file" -C "$WORK_DIR/unified/rootfs"
        ;;
    *)
        echo "Unsupported Incus rootfs export: $(file -b "$root_file")" >&2
        exit 1
        ;;
esac

rootfs="$WORK_DIR/unified/rootfs"
if [[ ! -x "$rootfs/sbin/init" && ! -L "$rootfs/sbin/init" ]]; then
    echo "Incus image does not provide /sbin/init" >&2
    exit 1
fi

if [[ "$PREINSTALL_SSH" == "true" || -n "$PREINSTALL_PACKAGES" ]]; then
    cleanup_chroot_mounts() {
        if mountpoint -q "$rootfs/sys"; then umount -l "$rootfs/sys"; fi
        if mountpoint -q "$rootfs/proc"; then umount -l "$rootfs/proc"; fi
        if mountpoint -q "$rootfs/dev"; then umount -l "$rootfs/dev"; fi
    }
    trap cleanup_chroot_mounts EXIT

    # The chroot shares the host network, but not systemd-resolved's local
    # stub. Use the host's upstream resolver while installing packages.
    rm -f "$rootfs/etc/resolv.conf"
    cp -L /run/systemd/resolve/resolv.conf "$rootfs/etc/resolv.conf"
    mount --bind /dev "$rootfs/dev"
    mount -t proc proc "$rootfs/proc"
    mount -t sysfs sys "$rootfs/sys"
    chroot "$rootfs" apt-get -o Acquire::ForceIPv4=true update
    packages=($PREINSTALL_PACKAGES)
    if [[ "$PREINSTALL_SSH" == "true" ]]; then
        packages+=(openssh-server)
    fi
    if ((${#packages[@]})); then
        chroot "$rootfs" env DEBIAN_FRONTEND=noninteractive \
            apt-get -o Acquire::ForceIPv4=true install -y \
            --no-install-recommends "${packages[@]}"
    fi
    if [[ "$PREINSTALL_SSH" == "true" ]]; then
        chroot "$rootfs" systemctl enable ssh
    fi

    # Instances must generate unique host identities on first boot.
    rm -f "$rootfs"/etc/ssh/ssh_host_*
    rm -rf "$rootfs/var/lib/apt/lists"/*
    rm -f "$rootfs/etc/resolv.conf"
    ln -s ../run/systemd/resolve/stub-resolv.conf "$rootfs/etc/resolv.conf"
    cleanup_chroot_mounts
    trap - EXIT
fi

tar -C "$WORK_DIR/unified" -czf "$WORK_DIR/$IMAGE_NAME.tar.gz" \
    metadata.yaml templates rootfs

image_properties=()
for fuse2fs_path in usr/bin/fuse2fs bin/fuse2fs usr/sbin/fuse2fs sbin/fuse2fs; do
    if [[ -x "$rootfs/$fuse2fs_path" ]]; then
        image_properties+=(--property hw_incus_data_volume_fuse=true)
        break
    fi
done

openstack image delete "$IMAGE_NAME" >/dev/null 2>&1 || true
# python-openstackclient does not expose Glance's root-tar extension, while
# the driver recognizes unified Incus tars by their metadata.yaml content.
openstack image create "$IMAGE_NAME" \
    --public --disk-format raw --container-format bare \
    "${image_properties[@]}" \
    --file "$WORK_DIR/$IMAGE_NAME.tar.gz"

openstack image show "$IMAGE_NAME" -c id -c status -c size
