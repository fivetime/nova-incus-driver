#!/usr/bin/env bash

# DevStack plugin for the Nova Incus system-container driver.

MY_XTRACE=$(set +o | grep xtrace)
set +o xtrace

NOVA_CONF_DIR=${NOVA_CONF_DIR:-/etc/nova}
NOVA_CONF=${NOVA_CONF:-${NOVA_CONF_DIR}/nova.conf}
NOVA_INCUS_DIR=${NOVA_INCUS_DIR:-${DEST}/openstack-incus}
# Retained for compatibility with external DevStack hooks that source this file.
# shellcheck disable=SC2034
NOVA_INCUS_PLUGIN_DIR=$(readlink -f "$(dirname "${BASH_SOURCE[0]}")")

GLANCE_CONF_DIR=${GLANCE_CONF_DIR:-/etc/glance}
GLANCE_API_CONF=${GLANCE_API_CONF:-${GLANCE_CONF_DIR}/glance-api.conf}
CINDER_CONF=${CINDER_CONF:-/etc/cinder/cinder.conf}


function incus_cli {
    local gomaxprocs=${INCUS_CLI_GOMAXPROCS:-$(nproc)}

    # Incus treats non-terminal stdin as a YAML request body. DevStack is
    # commonly driven through SSH, where stdin can remain open indefinitely,
    # so every non-interactive plugin call must receive an explicit EOF.
    sudo env GOMAXPROCS="${gomaxprocs}" incus "$@" </dev/null
}


function setup_pyproject_develop {
    local project_dir=$1
    local project_name

    project_name=$(python3 -c \
        'import pathlib, sys, tomllib; print(tomllib.loads(pathlib.Path(sys.argv[1]).read_text())["project"]["name"])' \
        "${project_dir}/pyproject.toml")
    if [[ -n "${REQUIREMENTS_DIR:-}" ]]; then
        "${REQUIREMENTS_DIR}/.venv/bin/edit-constraints" \
            "${REQUIREMENTS_DIR}/upper-constraints.txt" -- "${project_name}"
    fi
    setup_package "${project_dir}" -e
}


function _incus_version_at_least {
    local installed
    local oldest

    installed=$(incus_cli --version 2>/dev/null | head -n1 | \
        grep -oE '[0-9]+([.][0-9]+)+') || \
        return 1
    oldest=$(printf '%s\n%s\n' "${INCUS_MIN_VERSION}" "${installed}" | \
        sort -V | head -n1)
    [[ "${oldest}" == "${INCUS_MIN_VERSION}" ]]
}


function configure_zabbly_incus_repo {
    local arch
    local codename
    local keyring=/usr/share/keyrings/zabbly-incus-stable.gpg

    arch=$(dpkg --print-architecture)
    codename=$(. /etc/os-release && echo "${VERSION_CODENAME}")

    install_package ca-certificates curl gpg
    curl -fsSL https://pkgs.zabbly.com/key.asc | \
        gpg --dearmor | sudo tee "${keyring}" >/dev/null
    echo "deb [arch=${arch} signed-by=${keyring}] https://pkgs.zabbly.com/incus/stable ${codename} main" | \
        sudo tee /etc/apt/sources.list.d/zabbly-incus-stable.list >/dev/null
    apt_get_update
}


function pre_install_nova_incus {
    echo_summary "Installing Incus compute dependencies"

    if ! is_ubuntu; then
        die $LINENO "The initial nova-incus DevStack target is Ubuntu Noble"
    fi

    install_package apparmor apparmor-utils attr btrfs-progs jq lvm2 rsync
    if [[ "${INCUS_STORAGE_DRIVER}" == "ceph" ]] || \
            [[ -n "${INCUS_BFV_POOL_NAME}" ]] || \
            [[ -n "${INCUS_CINDER_CEPH_POOL}" ]] || \
            [[ -n "${INCUS_CINDER_BACKUP_CEPH_POOL}" ]]; then
        install_package ceph-common
    fi

    if ! command -v incus >/dev/null 2>&1 || ! _incus_version_at_least; then
        configure_zabbly_incus_repo
        # install_package skips packages that are already present, which
        # leaves Noble's Incus 6.0 installed after adding the Zabbly repo.
        # apt_get must resolve the newer candidate explicitly.
        apt_get install incus incus-client
    fi
    if ! _incus_version_at_least; then
        die $LINENO "Incus ${INCUS_MIN_VERSION} or newer is required"
    fi

    if getent group "${INCUS_GROUP}" >/dev/null 2>&1; then
        add_user_to_group "${STACK_USER}" "${INCUS_GROUP}"
    fi
}


function install_nova_incus {
    echo_summary "Installing the Incus Python SDK and nova-incus"

    if [[ ! -d "${INCUS_PYTHON_SDK_DIR}/.git" ]]; then
        git_clone "${INCUS_PYTHON_SDK_REPO}" \
            "${INCUS_PYTHON_SDK_DIR}" "${INCUS_PYTHON_SDK_BRANCH}"
    fi
    # DevStack's setup_develop still reads setup.cfg before falling back to
    # pyproject.toml, which aborts for a modern pyproject-only package.
    setup_pyproject_develop "${INCUS_PYTHON_SDK_DIR}"
    setup_develop "${NOVA_INCUS_DIR}"

    if is_true "${INCUS_APPLY_NOVA_DIAGNOSTICS_PATCH}"; then
        local diagnostics_patch
        diagnostics_patch="${NOVA_INCUS_DIR}/patches/nova/0001-diagnostics-add-lxd-driver.patch"
        if grep -q 'LXD = "lxd"' "${NOVA_DIR}/nova/objects/fields.py" && \
                grep -q "'lxd'," \
                    "${NOVA_DIR}/nova/api/openstack/compute/schemas/server_diagnostics.py"; then
            echo "Nova diagnostics already accepts the lxd driver"
        elif git -C "${NOVA_DIR}" apply --check "${diagnostics_patch}"; then
            git -C "${NOVA_DIR}" apply "${diagnostics_patch}"
        else
            die $LINENO \
                "Nova diagnostics compatibility patch does not apply cleanly"
        fi
    fi

    # Nova's source checkout is a regular Python package, so an editable
    # external distribution cannot extend nova.virt with another namespace
    # path. Keep this repository authoritative and deploy its driver package
    # into the DevStack Nova checkout on every stack run.
    mkdir -p "${NOVA_DIR}/nova/virt/lxd"
    sudo rsync -a --delete --chown="${STACK_USER}:${STACK_USER}" \
        "${NOVA_INCUS_DIR}/nova/virt/lxd/" \
        "${NOVA_DIR}/nova/virt/lxd/"
}


function configure_nova_incus_storage {
    echo_summary "Initializing the host-local Incus storage pool"

    local -a storage_args

    incus_cli admin waitready --timeout=60

    # Keep Nova-owned instances separate from manually managed or retained
    # instances in the daemon's default project. Nova refuses to register a
    # new compute service when its driver reports instances that do not exist
    # in the current cell database.
    if [[ "${INCUS_PROJECT}" != "default" ]] && \
            ! incus_cli project show "${INCUS_PROJECT}" >/dev/null 2>&1; then
        incus_cli project create "${INCUS_PROJECT}" \
            -c features.images=false \
            -c features.profiles=false
    fi

    if [[ -n "${INCUS_BFV_POOL_NAME}" ]]; then
        if ! incus_cli project show \
                "${INCUS_MIGRATION_PREFLIGHT_PROJECT}" >/dev/null 2>&1; then
            incus_cli project create "${INCUS_MIGRATION_PREFLIGHT_PROJECT}" \
                -c features.images=false \
                -c features.profiles=true
        fi
        incus_cli project set "${INCUS_MIGRATION_PREFLIGHT_PROJECT}" \
            features.profiles=true \
            restricted=true \
            limits.containers=0 \
            limits.virtual-machines=0 \
            user.openstack.preflight_protocol=1 \
            "user.openstack.bfv_pool=${INCUS_BFV_POOL_NAME}" \
            "user.openstack.cinder_rbd_pool=${INCUS_BFV_CEPH_POOL}"
    fi

    if ! incus_cli storage show "${INCUS_POOL_NAME}" >/dev/null 2>&1; then
        storage_args=(
            storage create "${INCUS_POOL_NAME}" "${INCUS_STORAGE_DRIVER}"
        )
        case "${INCUS_STORAGE_DRIVER}" in
            ceph)
                storage_args+=(
                    "ceph.cluster_name=${INCUS_CEPH_CLUSTER_NAME}"
                )
                if [[ -n "${INCUS_STORAGE_SOURCE}" ]]; then
                    storage_args+=("source=${INCUS_STORAGE_SOURCE}")
                fi
                if [[ -n "${INCUS_CEPH_OSD_POOL_NAME}" ]]; then
                    storage_args+=(
                        "ceph.osd.pool_name=${INCUS_CEPH_OSD_POOL_NAME}"
                    )
                fi
                if [[ "${INCUS_CEPH_FORCE_REUSE,,}" == "true" ]]; then
                    storage_args+=("ceph.osd.force_reuse=true")
                fi
                ;;
            linstor)
                if [[ -z "${INCUS_STORAGE_SOURCE}" ]]; then
                    die $LINENO \
                        "INCUS_STORAGE_SOURCE is required for LINSTOR"
                fi
                storage_args+=("source=${INCUS_STORAGE_SOURCE}")
                ;;
            dir)
                if [[ -n "${INCUS_STORAGE_SOURCE}" ]]; then
                    storage_args+=("source=${INCUS_STORAGE_SOURCE}")
                fi
                ;;
            *)
                if [[ -n "${INCUS_STORAGE_SOURCE}" ]]; then
                    storage_args+=("source=${INCUS_STORAGE_SOURCE}")
                else
                    storage_args+=("size=${INCUS_STORAGE_SIZE}")
                    warn "Creating loop-backed ${INCUS_STORAGE_DRIVER} storage for development only"
                fi
                ;;
        esac
        incus_cli "${storage_args[@]}"
    fi

    if ! incus_cli profile show default >/dev/null 2>&1; then
        incus_cli profile create default
    fi
    if ! incus_cli profile device get default root pool \
        >/dev/null 2>&1; then
        incus_cli profile device add default root disk \
            path=/ pool="${INCUS_POOL_NAME}"
    fi

    if [[ -n "${INCUS_BFV_POOL_NAME}" ]]; then
        if [[ -z "${INCUS_BFV_CEPH_POOL}" ]]; then
            die $LINENO \
                "INCUS_BFV_CEPH_POOL is required with INCUS_BFV_POOL_NAME"
        fi
        if ! incus_cli query /1.0 | jq -e \
            '.api_extensions | index("storage_driver_cephext")' \
            >/dev/null; then
            die $LINENO "Incus does not support storage_driver_cephext"
        fi
        if ! incus_cli storage show "${INCUS_BFV_POOL_NAME}" \
                >/dev/null 2>&1; then
            incus_cli storage create "${INCUS_BFV_POOL_NAME}" cephext \
                "source=${INCUS_BFV_CEPH_POOL}" \
                "ceph.user.name=${INCUS_BFV_CEPH_USER}" \
                "ceph.cluster_name=${INCUS_BFV_CEPH_CLUSTER_NAME}"
        fi
    fi
}


function configure_nova_incus {
    echo_summary "Configuring Nova to use Incus"

    # Ensure ML2 has an allocatable project network type before neutron-api
    # starts. DevStack's post-config phase can run after an existing API
    # service has started during an idempotent stack run, leaving the Geneve
    # allocation table empty until a manual restart.
    if is_service_enabled q-svc neutron-api && \
            [[ -n "${Q_PLUGIN_CONF_FILE:-}" ]] && \
            [[ -n "${Q_ML2_TENANT_NETWORK_TYPE:-}" ]]; then
        iniset "/${Q_PLUGIN_CONF_FILE}" ml2 project_network_types \
            "${Q_ML2_TENANT_NETWORK_TYPE}"
    fi

    # DevStack copies NOVA_CONF to NOVA_CPU_CONF before starting nova-compute.
    # Write both so plugin settings remain correct in every phase and on
    # repeat stack runs.
    local nova_target
    local nova_targets=("${NOVA_CONF}")
    if [[ -n "${NOVA_CPU_CONF:-}" && "${NOVA_CPU_CONF}" != "${NOVA_CONF}" ]]; then
        nova_targets+=("${NOVA_CPU_CONF}")
    fi

    for nova_target in "${nova_targets[@]}"; do
        # Keep the established import path during incremental modernization.
        iniset "${nova_target}" DEFAULT compute_driver lxd.LXDDriver
        iniset "${nova_target}" os_vif_ovs ovsdb_connection \
            unix:/var/run/openvswitch/db.sock
        iniset "${nova_target}" DEFAULT force_config_drive False
        iniset "${nova_target}" incus endpoint /var/lib/incus/unix.socket
        iniset "${nova_target}" incus project "${INCUS_PROJECT}"
        iniset "${nova_target}" incus root_dir /var/lib/incus
        iniset "${nova_target}" incus storage_pool "${INCUS_POOL_NAME}"
        if [[ -n "${INCUS_BFV_POOL_NAME}" ]]; then
            iniset "${nova_target}" incus boot_from_volume_storage_pool \
                "${INCUS_BFV_POOL_NAME}"
        fi
        iniset "${nova_target}" incus default_process_limit \
            "${INCUS_DEFAULT_PROCESS_LIMIT}"
        iniset "${nova_target}" incus maximum_process_limit \
            "${INCUS_MAXIMUM_PROCESS_LIMIT}"
        iniset "${nova_target}" incus volume_use_multipath \
            "${INCUS_VOLUME_USE_MULTIPATH}"
        iniset "${nova_target}" incus volume_enforce_multipath \
            "${INCUS_VOLUME_ENFORCE_MULTIPATH}"
        iniset "${nova_target}" incus num_volume_scan_tries \
            "${INCUS_NUM_VOLUME_SCAN_TRIES}"
        iniset "${nova_target}" incus allow_cold_migration \
            "${INCUS_ALLOW_COLD_MIGRATION}"
        iniset "${nova_target}" incus allow_bfv_evacuate \
            "${INCUS_ALLOW_BFV_EVACUATE}"
        iniset "${nova_target}" incus migration_auto_recovery \
            "${INCUS_MIGRATION_AUTO_RECOVERY}"
        iniset "${nova_target}" incus migration_recovery_interval \
            "${INCUS_MIGRATION_RECOVERY_INTERVAL}"
        if [[ -n "${INCUS_MIGRATION_ADDRESS}" ]]; then
            iniset "${nova_target}" incus migration_address \
                "${INCUS_MIGRATION_ADDRESS}"
        fi
        if [[ -n "${INCUS_MIGRATION_PREFLIGHT_TLS_CERT}" ]]; then
            iniset "${nova_target}" incus migration_preflight_tls_cert \
                "${INCUS_MIGRATION_PREFLIGHT_TLS_CERT}"
        fi
        if [[ -n "${INCUS_MIGRATION_PREFLIGHT_TLS_KEY}" ]]; then
            iniset "${nova_target}" incus migration_preflight_tls_key \
                "${INCUS_MIGRATION_PREFLIGHT_TLS_KEY}"
        fi
        if [[ -n "${INCUS_MIGRATION_PREFLIGHT_TLS_CA}" ]]; then
            iniset "${nova_target}" incus migration_preflight_tls_ca \
                "${INCUS_MIGRATION_PREFLIGHT_TLS_CA}"
        fi
        iniset "${nova_target}" incus migration_preflight_project \
            "${INCUS_MIGRATION_PREFLIGHT_PROJECT}"
        if [[ -n "${INCUS_MIGRATION_PREFLIGHT_SERVER_NAMES}" ]]; then
            iniset "${nova_target}" incus migration_preflight_server_names \
                "${INCUS_MIGRATION_PREFLIGHT_SERVER_NAMES}"
        fi
        if [[ -n "${INCUS_MIGRATION_PREFLIGHT_TLS_CA_BY_SERVER}" ]]; then
            iniset "${nova_target}" incus migration_preflight_tls_ca_by_server \
                "${INCUS_MIGRATION_PREFLIGHT_TLS_CA_BY_SERVER}"
        fi
    done

    if is_service_enabled glance; then
        iniset "${GLANCE_API_CONF}" DEFAULT disk_formats \
            "ami,ari,aki,vhd,raw,iso,qcow2,root-tar,squashfs"
        iniset "${GLANCE_API_CONF}" DEFAULT container_formats \
            "ami,ari,aki,bare,ovf,tgz"
    fi

    sudo install -o root -g root -m 0644 \
        "${NOVA_INCUS_DIR}"/etc/nova/rootwrap.d/*.filters \
        "${NOVA_CONF_DIR}/rootwrap.d"
}


function configure_cinder_linstor {
    if [[ -n "${INCUS_CINDER_LINSTOR_CONTROLLER}" ]] && \
            [[ -n "${INCUS_CINDER_CEPH_POOL}" ]]; then
        die $LINENO "Configure exactly one Cinder backend: LINSTOR or Ceph"
    fi

    if [[ -z "${INCUS_CINDER_LINSTOR_CONTROLLER}" ]] || \
            ! is_service_enabled c-vol; then
        return
    fi

    echo_summary "Configuring the Cinder LINSTOR backend"

    local driver_file="${CINDER_DIR}/cinder/volume/drivers/linstordrv.py"
    local missing='linstor.Resource(str(rsc_name))'
    local fixed='linstor.Resource(str(rsc_name), uri=self.default_uri)'
    local count

    count=$(grep -Fxc "            ${missing}" "${driver_file}" || true)
    if [[ "${count}" -eq 2 ]]; then
        sudo sed -i "s/${missing}/${fixed}/g" "${driver_file}"
    elif grep -Fq "${missing}" "${driver_file}"; then
        die $LINENO "Unexpected Cinder LINSTOR URI call count"
    fi

    iniset "${CINDER_CONF}" DEFAULT enabled_backends linstor
    iniset "${CINDER_CONF}" linstor volume_driver \
        cinder.volume.drivers.linstordrv.LinstorDrbdDriver
    iniset "${CINDER_CONF}" linstor volume_backend_name linstor
    # Cinder registers these driver options in the shared backend group.
    iniset "${CINDER_CONF}" backend_defaults linstor_default_uri \
        "linstor://${INCUS_CINDER_LINSTOR_CONTROLLER}"
    iniset "${CINDER_CONF}" backend_defaults \
        linstor_default_storage_pool_name \
        "${INCUS_CINDER_LINSTOR_STORAGE_POOL}"
    iniset "${CINDER_CONF}" backend_defaults linstor_autoplace_count \
        "${INCUS_CINDER_LINSTOR_REPLICA_COUNT}"
    iniset "${CINDER_CONF}" backend_defaults linstor_volume_downsize_factor \
        "${INCUS_CINDER_LINSTOR_DOWNSIZE_FACTOR}"
    iniset "${CINDER_CONF}" backend_defaults \
        linstor_controller_diskless False
}


function configure_cinder_ceph {
    if [[ -z "${INCUS_CINDER_CEPH_POOL}" ]] || \
            ! is_service_enabled c-vol; then
        return
    fi

    echo_summary "Configuring the Cinder Ceph RBD backend"

    local keyring="/etc/ceph/${INCUS_CINDER_CEPH_CLUSTER_NAME}.client.${INCUS_CINDER_CEPH_USER}.keyring"

    [[ -r "${INCUS_CINDER_CEPH_CONF}" ]] || \
        die $LINENO "Ceph configuration is not readable: ${INCUS_CINDER_CEPH_CONF}"
    [[ -r "${keyring}" ]] || \
        die $LINENO "Cinder CephX keyring is not readable: ${keyring}"
    sudo -u "${STACK_USER}" test -r "${INCUS_CINDER_CEPH_CONF}" || \
        die $LINENO "Cinder service user cannot read: ${INCUS_CINDER_CEPH_CONF}"
    sudo -u "${STACK_USER}" test -r "${keyring}" || \
        die $LINENO "Cinder service user cannot read CephX keyring: ${keyring}"

    iniset "${CINDER_CONF}" DEFAULT enabled_backends ceph
    iniset "${CINDER_CONF}" ceph volume_driver \
        cinder.volume.drivers.rbd.RBDDriver
    iniset "${CINDER_CONF}" ceph volume_backend_name \
        "${INCUS_CINDER_CEPH_BACKEND_NAME}"
    iniset "${CINDER_CONF}" ceph rbd_cluster_name \
        "${INCUS_CINDER_CEPH_CLUSTER_NAME}"
    iniset "${CINDER_CONF}" ceph rbd_pool "${INCUS_CINDER_CEPH_POOL}"
    iniset "${CINDER_CONF}" ceph rbd_user "${INCUS_CINDER_CEPH_USER}"
    iniset "${CINDER_CONF}" ceph rbd_ceph_conf \
        "${INCUS_CINDER_CEPH_CONF}"
    iniset "${CINDER_CONF}" ceph rbd_exclusive_cinder_pool True
}


function configure_cinder_ceph_backup {
    if [[ -z "${INCUS_CINDER_BACKUP_CEPH_POOL}" ]]; then
        return
    fi
    if ! is_service_enabled c-bak; then
        die $LINENO \
            "INCUS_CINDER_BACKUP_CEPH_POOL requires the c-bak service"
    fi
    if [[ -z "${INCUS_CINDER_CEPH_POOL}" ]] || \
            ! is_service_enabled c-vol; then
        die $LINENO "Ceph backup requires the Cinder Ceph volume backend"
    fi
    if [[ "${INCUS_CINDER_BACKUP_CEPH_POOL}" == \
            "${INCUS_CINDER_CEPH_POOL}" ]]; then
        die $LINENO "Cinder volume and backup pools must be different"
    fi

    echo_summary "Configuring the Cinder Ceph backup service"

    local cluster_name="${INCUS_CINDER_CEPH_CLUSTER_NAME}"
    local keyring="/etc/ceph/${cluster_name}.client.${INCUS_CINDER_BACKUP_CEPH_USER}.keyring"

    [[ -r "${INCUS_CINDER_BACKUP_CEPH_CONF}" ]] || \
        die $LINENO "Ceph backup configuration is not readable: ${INCUS_CINDER_BACKUP_CEPH_CONF}"
    [[ -r "${keyring}" ]] || \
        die $LINENO "Cinder backup CephX keyring is not readable: ${keyring}"
    sudo -u "${STACK_USER}" test -r \
        "${INCUS_CINDER_BACKUP_CEPH_CONF}" || \
        die $LINENO "Cinder backup service user cannot read Ceph configuration"
    sudo -u "${STACK_USER}" test -r "${keyring}" || \
        die $LINENO "Cinder backup service user cannot read CephX keyring"

    iniset "${CINDER_CONF}" DEFAULT backup_driver \
        cinder.backup.drivers.ceph.CephBackupDriver
    iniset "${CINDER_CONF}" DEFAULT backup_ceph_conf \
        "${INCUS_CINDER_BACKUP_CEPH_CONF}"
    iniset "${CINDER_CONF}" DEFAULT backup_ceph_user \
        "${INCUS_CINDER_BACKUP_CEPH_USER}"
    iniset "${CINDER_CONF}" DEFAULT backup_ceph_pool \
        "${INCUS_CINDER_BACKUP_CEPH_POOL}"
}


function init_nova_incus {
    local current_store
    local expected_store
    local image_file
    local image_id
    local image_name

    echo_summary "Registering the DevStack system-container test image"
    mkdir -p "${TOP_DIR}/files"

    image_file="cirros-${CIRROS_VERSION}-${CIRROS_ARCH}-lxc.tar.gz"
    image_name="cirros-${CIRROS_VERSION}-${CIRROS_ARCH}-incus"
    if [[ ! -f "${TOP_DIR}/files/${image_file}" ]]; then
        wget --progress=dot:giga -c \
            "http://download.cirros-cloud.net/${CIRROS_VERSION}/${image_file}" \
            -O "${TOP_DIR}/files/${image_file}"
    fi

    expected_store=$(iniget "${GLANCE_API_CONF}" glance_store default_store)
    image_id=$(openstack --os-cloud=devstack-admin image show "${image_name}" \
        -f value -c id 2>/dev/null || true)
    if [[ -n "${image_id}" ]]; then
        current_store=$(openstack --os-cloud=devstack-admin image show \
            "${image_id}" -f json |
            python3 -c 'import json, sys
data = json.load(sys.stdin)
url = data.get("properties", {}).get("direct_url", "")
print(url.partition("://")[0])')
        if [[ -n "${expected_store}" && "${current_store}" != "${expected_store}" ]]; then
            echo_summary "Replacing ${image_name}: Glance store changed from ${current_store:-unknown} to ${expected_store}"
            # A stale image can be impossible to delete after its store
            # driver has been disabled. Remove the canonical name first so
            # subsequent name lookup is deterministic, then clean it up on a
            # best-effort basis.
            openstack --os-cloud=devstack-admin image set \
                --name "${image_name}-stale-${image_id}" "${image_id}"
            openstack --os-cloud=devstack-admin image delete \
                "${image_id}" || true
            image_id=
        fi
    fi

    if [[ -z "${image_id}" ]]; then
        openstack --os-cloud=devstack-admin \
            --os-region-name="${REGION_NAME}" image create "${image_name}" \
            --public --container-format bare --disk-format raw \
            < "${TOP_DIR}/files/${image_file}"
    fi

    if is_service_enabled tempest && \
            [[ "${INCUS_TEMPEST_BUILD_IMAGE,,}" == "true" ]]; then
        echo_summary "Publishing the SSH/FUSE Incus Tempest image"
        SOURCE="${INCUS_TEMPEST_IMAGE_SOURCE}" \
        IMAGE_NAME="${INCUS_TEMPEST_IMAGE_NAME}" \
        PREINSTALL_SSH=true PREINSTALL_PACKAGES=fuse2fs \
            bash "${NOVA_INCUS_DIR}/tools/publish-incus-image-to-glance.sh"
    fi
}


function configure_nova_incus_compute_service {
    echo_summary "Selecting the Incus Nova compute manager"

    if [[ "${USE_SYSTEMD,,}" != "true" ]]; then
        die $LINENO "nova-incus requires DevStack's systemd service backend"
    fi

    local dropin_dir=/etc/systemd/system/devstack@n-cpu.service.d
    sudo mkdir -p "${dropin_dir}"
    sudo tee "${dropin_dir}/nova-incus.conf" >/dev/null <<EOF
[Service]
ExecStart=
ExecStart=${NOVA_BIN_DIR}/python -m nova.virt.lxd.cmd.compute --config-file ${NOVA_CPU_CONF}
EOF
    sudo systemctl daemon-reload
    restart_service devstack@n-cpu.service
}


function test_config_nova_incus {
    if ! is_service_enabled tempest; then
        return
    fi

    local image_name="cirros-${CIRROS_VERSION}-${CIRROS_ARCH}-incus"
    local image_id
    if [[ "${INCUS_TEMPEST_BUILD_IMAGE,,}" == "true" ]]; then
        image_name="${INCUS_TEMPEST_IMAGE_NAME}"
    fi
    image_id=$(openstack image show "${image_name}" -f value -c id)

    iniset "${TEMPEST_CONFIG}" image disk_formats \
        "ami,ari,aki,vhd,raw,iso,root-tar,squashfs"
    iniset "${TEMPEST_CONFIG}" compute image_ref "${image_id}"
    iniset "${TEMPEST_CONFIG}" compute image_ref_alt "${image_id}"
    iniset "${TEMPEST_CONFIG}" compute-feature-enabled shelve False
    iniset "${TEMPEST_CONFIG}" compute-feature-enabled resize True
    iniset "${TEMPEST_CONFIG}" compute-feature-enabled config_drive True
    # The baseline topology delivers metadata through the read-only config
    # drive. Enable this only after deploying and validating OVN metadata
    # agents and the 169.254.169.254 path.
    iniset "${TEMPEST_CONFIG}" compute-feature-enabled metadata_service False
    iniset "${TEMPEST_CONFIG}" compute-feature-enabled \
        attach_encrypted_volume False
    iniset "${TEMPEST_CONFIG}" compute-feature-enabled vnc_console False

    if [[ "${INCUS_TEMPEST_BUILD_IMAGE,,}" == "true" ]]; then
        iniset "${TEMPEST_CONFIG}" validation run_validation True
        iniset "${TEMPEST_CONFIG}" validation image_ssh_user \
            "${INCUS_TEMPEST_IMAGE_SSH_USER}"
        iniset "${TEMPEST_CONFIG}" compute flavor_ref \
            "${INCUS_TEMPEST_FLAVOR_REF}"
        iniset "${TEMPEST_CONFIG}" compute flavor_ref_alt \
            "${INCUS_TEMPEST_FLAVOR_REF_ALT}"
        iniset "${TEMPEST_CONFIG}" compute volume_device_name sdb
        iniset "${TEMPEST_CONFIG}" compute min_compute_nodes \
            "${INCUS_TEMPEST_MIN_COMPUTE_NODES}"
    fi

    if is_service_enabled c-vol; then
        iniset "${TEMPEST_CONFIG}" volume min_microversion 3.42
        iniset "${TEMPEST_CONFIG}" volume max_microversion 3.42
    fi

    local cold_migration=False
    if [[ "${INCUS_TEMPEST_ENABLE_EXPERIMENTAL_COLD_MIGRATION,,}" == "true" ]] && \
            [[ "${INCUS_ALLOW_COLD_MIGRATION,,}" == "true" ]] && \
            ((INCUS_TEMPEST_MIN_COMPUTE_NODES >= 2)); then
        cold_migration=True
    fi
    iniset "${TEMPEST_CONFIG}" compute-feature-enabled cold_migration \
        "${cold_migration}"
}


function cleanup_nova_incus {
    # DevStack clean must not delete persistent tenant instances implicitly.
    :
}


if is_service_enabled nova-incus; then
    if [[ "$1" == "stack" && "$2" == "pre-install" ]]; then
        pre_install_nova_incus
        configure_nova_incus_storage
    elif [[ "$1" == "stack" && "$2" == "install" ]]; then
        install_nova_incus
    elif [[ "$1" == "stack" && "$2" == "post-config" ]]; then
        configure_nova_incus
        configure_cinder_linstor
        configure_cinder_ceph
        configure_cinder_ceph_backup
    elif [[ "$1" == "stack" && "$2" == "extra" ]]; then
        init_nova_incus
        configure_nova_incus_compute_service
    elif [[ "$1" == "stack" && "$2" == "test-config" ]]; then
        test_config_nova_incus
    fi

    if [[ "$1" == "clean" ]]; then
        cleanup_nova_incus
    fi
fi

$MY_XTRACE
