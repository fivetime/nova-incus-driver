#!/usr/bin/env bash
# Inspect the Nova modules and entry point inside each running service namespace.

set -euo pipefail

ROLE=${1:-${ROLE:-}}
RUNTIME_PROCESS_REGEX=${RUNTIME_PROCESS_REGEX:-}
RUNTIME_PYTHON=${RUNTIME_PYTHON:-}
MIN_INCUS_MIGRATE_DATA_VERSION=${MIN_INCUS_MIGRATE_DATA_VERSION:-1.6}

if [[ ! "$MIN_INCUS_MIGRATE_DATA_VERSION" =~ ^[0-9]+(\.[0-9]+)+$ ]]; then
    echo "MIN_INCUS_MIGRATE_DATA_VERSION must be a numeric object version" >&2
    exit 2
fi

case "$ROLE" in
    api)
        RUNTIME_PROCESS_REGEX=${RUNTIME_PROCESS_REGEX:-'(nova-api|nova_api|nova\.api\.openstack\.compute\.wsgi|uwsgi.*nova)'}
        ;;
    compute)
        RUNTIME_PROCESS_REGEX=${RUNTIME_PROCESS_REGEX:-'(nova-incus-compute|nova\.virt\.incus\.cmd\.compute)'}
        ;;
    conductor)
        RUNTIME_PROCESS_REGEX=${RUNTIME_PROCESS_REGEX:-'(nova-conductor)'}
        ;;
    *)
        echo "Usage: $0 {api|compute|conductor}" >&2
        exit 2
        ;;
esac

command -v nsenter >/dev/null || {
    echo "nsenter is required for Nova runtime namespace inspection" >&2
    exit 2
}

declare -a runtime_pids=()
for proc in /proc/[0-9]*; do
    [[ -r "$proc/cmdline" ]] || continue
    cmdline=$(tr '\0' ' ' <"$proc/cmdline" 2>/dev/null || true)
    if [[ "$ROLE" == api && \
          "$cmdline" =~ (nova-api-meta|nova_api_meta|nova-api-metadata) ]]; then
        continue
    fi
    [[ -n "$cmdline" && "$cmdline" =~ $RUNTIME_PROCESS_REGEX ]] || continue
    runtime_pids+=("${proc##*/}")
done

((${#runtime_pids[@]} > 0)) || {
    echo "NO-GO: no running Nova $ROLE process matched $RUNTIME_PROCESS_REGEX" >&2
    exit 1
}

read -r -d '' CONTRACT <<'PY' || true
import inspect
import json
import os
import stat
import sys
import types


role, pid, cmdline, minimum_migrate_data_version = sys.argv[1:]
checks = []


def require(condition, description):
    if not condition:
        raise RuntimeError(description)
    checks.append(description)


def code_names(callable_object):
    callable_object = inspect.unwrap(callable_object)
    code = getattr(callable_object, "__code__", None)
    require(code is not None, "callable exposes Python bytecode")
    names = set()

    def visit(candidate):
        names.update(candidate.co_names)
        for constant in candidate.co_consts:
            if isinstance(constant, str):
                names.add(constant)
            elif isinstance(constant, types.CodeType):
                visit(constant)

    visit(code)
    return names


from nova.compute import manager as nova_manager
from nova import utils as nova_utils
from nova.objects import base as nova_object_base
from nova.objects import register_all
from oslo_utils import versionutils

register_all()
registered_migrate_data = nova_object_base.NovaObjectRegistry.obj_classes().get(
    "IncusLiveMigrateData", []
)
require(
    bool(registered_migrate_data),
    "Nova runtime registers IncusLiveMigrateData",
)
registered_migrate_versions = [
    versionutils.convert_version_to_tuple(candidate.VERSION)
    for candidate in registered_migrate_data
]
minimum_migrate_data_version_tuple = versionutils.convert_version_to_tuple(
    minimum_migrate_data_version
)
require(
    max(registered_migrate_versions, default=(0, 0))
    >= minimum_migrate_data_version_tuple,
    "Nova runtime registers IncusLiveMigrateData version {} or newer".format(
        minimum_migrate_data_version
    ),
)

core_hooks = (
    "_pre_deny_share",
    "_prepare_live_migration_check_data",
    "_complete_live_migration_rollback",
)
for hook in core_hooks:
    require(
        callable(getattr(nova_manager.ComputeManager, hook, None)),
        "Nova ComputeManager core hook {}".format(hook),
    )

sdk_adapter_source = "".join(
    inspect.getsource(nova_utils.get_sdk_adapter).split())
for token in (
    "ks_identity.V3Token(",
    "load_session_from_conf_options(CONF,confgrp,auth=token_auth)",
    "session=token_session",
    "oslo_conf=CONF",
):
    require(
        token in sdk_adapter_source,
        "Nova user-token SDK connections honor service configuration",
    )

if role == "api":
    from nova.api.openstack.compute import server_shares
    from nova.compute import api as compute_api

    require(
        callable(getattr(compute_api, "_require_share_migration_capability", None)),
        "Nova API Manila migration capability gate",
    )
    require(
        "_require_share_migration_capability" in code_names(compute_api.API.resize),
        "Nova API cold migration invokes capability gate",
    )
    require(
        "CUSTOM_INCUS_MANILA_COLD_MIGRATION" in code_names(
            compute_api.API.resize
        ),
        "Nova API cold migration trait",
    )
    require(
        "_require_share_migration_capability" in code_names(
            compute_api.API.live_migrate
        ),
        "Nova API live migration invokes capability gate",
    )
    require(
        "CUSTOM_INCUS_MANILA_LIVE_MIGRATION" in code_names(
            compute_api.API.live_migrate
        ),
        "Nova API live migration trait",
    )
    require(
        "task_state" in code_names(
            server_shares.ServerSharesController._check_instance_in_valid_state
        ),
        "Nova API rejects share mutation during an instance task",
    )
elif role == "compute":
    from glanceclient.common import http as glance_http
    from nova.image import glance as nova_glance
    from os_brick.initiator.connectors import rbd
    from pylxd import client as pylxd_client
    from nova.virt.incus import driver as incus_driver
    from nova.virt.incus import manager as incus_manager
    from nova.virt.incus.cmd import compute as incus_compute

    require(
        "nova-incus-compute" in cmdline
        or "nova.virt.incus.cmd.compute" in cmdline,
        "running compute uses the Incus entry point",
    )
    require(
        incus_compute.INCUS_COMPUTE_MANAGER
        == "nova.virt.incus.manager.IncusComputeManager",
        "Incus entry point selects IncusComputeManager",
    )
    for hook in core_hooks:
        implementation = incus_manager.IncusComputeManager.__dict__.get(hook)
        require(callable(implementation), "Incus manager overrides {}".format(hook))
        require(
            implementation is not getattr(nova_manager.ComputeManager, hook),
            "Incus manager owns {} implementation".format(hook),
        )

    expected_traits = {
        "INCUS_MANILA_SHARE_TRAIT": "CUSTOM_INCUS_MANILA_SHARE",
        "INCUS_MANILA_LIVE_MIGRATION_TRAIT":
            "CUSTOM_INCUS_MANILA_LIVE_MIGRATION",
        "INCUS_MANILA_COLD_MIGRATION_TRAIT":
            "CUSTOM_INCUS_MANILA_COLD_MIGRATION",
    }
    provider_names = code_names(incus_driver.IncusDriver.update_provider_tree)
    for symbol, value in expected_traits.items():
        require(
            getattr(incus_driver, symbol, None) == value,
            "Incus driver defines {}".format(value),
        )
        require(
            symbol in provider_names,
            "Incus provider-tree update uses {}".format(symbol),
        )
    require(
        "image_size" in code_names(nova_glance.GlanceImageServiceV2._upload_data),
        "Nova sends seekable Glance upload size",
    )
    require(
        "IterableWithLength" in code_names(glance_http._BaseHTTPClient._chunk_body),
        "python-glanceclient preserves seekable upload length",
    )
    rbd_source = code_names(rbd.RBDConnector._local_attach_volume)
    rbd_disconnect_source = code_names(rbd.RBDConnector.disconnect_volume)
    require(
        "noudev" not in rbd_source and "noudev" not in rbd_disconnect_source,
        "os-brick waits for host udev to settle RBD map and unmap",
    )
    require(
        stat.S_ISSOCK(os.stat("/run/udev/control").st_mode),
        "Nova compute shares the host udev control socket",
    )
    require(
        "_find_root_device" in rbd_source,
        "os-brick resolves the kernel RBD path without udev links",
    )
    require(
        callable(getattr(pylxd_client, "_UnixAdapter", None)),
        "Incus Python SDK supports Unix socket endpoints",
    )

print(json.dumps({
    "role": role,
    "pid": int(pid),
    "cmdline": cmdline,
    "nova_module": inspect.getfile(nova_manager),
    "checks": checks,
}, sort_keys=True))
PY

failures=0
validated=0
for pid in "${runtime_pids[@]}"; do
    [[ -r "/proc/$pid/cmdline" ]] || {
        echo "NO-GO: Nova $ROLE process $pid disappeared during inspection" >&2
        failures=$((failures + 1))
        continue
    }
    cmdline=$(tr '\0' ' ' <"/proc/$pid/cmdline")
    proc_path=$(tr '\0' '\n' <"/proc/$pid/environ" |
        sed -n 's/^PATH=//p' | tail -n 1)
    proc_pythonpath=$(tr '\0' '\n' <"/proc/$pid/environ" |
        sed -n 's/^PYTHONPATH=//p' | tail -n 1)
    proc_path=${proc_path:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}

    runtime_python=$RUNTIME_PYTHON
    if [[ -z "$runtime_python" ]]; then
        process_command=${cmdline%% *}
        if [[ "${process_command##*/}" == python* ]] && \
                nsenter --target "$pid" --mount -- \
                test -x "$process_command" 2>/dev/null; then
            # /proc/PID/exe resolves a virtualenv Python symlink to the base
            # interpreter and loses pyvenv.cfg. Preserve the invoked path.
            runtime_python=$process_command
        fi
    fi
    if [[ -z "$runtime_python" ]]; then
        service_unit=$(sed -n \
            's#^0::.*\/\([^/]*\.service\)$#\1#p' \
            "/proc/$pid/cgroup" | tail -n 1)
        if [[ -n "$service_unit" ]] && command -v systemctl >/dev/null; then
            service_exec=$(systemctl show "$service_unit" \
                -p ExecStart --value 2>/dev/null || true)
            if [[ "$service_exec" =~ --(venv|virtualenv)(=|[[:space:]])([^[:space:];}]+) ]]; then
                venv_python=${BASH_REMATCH[3]}/bin/python
                if nsenter --target "$pid" --mount -- \
                        test -x "$venv_python" 2>/dev/null; then
                    runtime_python=$venv_python
                fi
            fi
        fi
    fi
    if [[ -z "$runtime_python" ]]; then
        runtime_python=$(nsenter --target "$pid" --mount -- \
            env PATH="$proc_path" sh -c \
            'command -v python3 || command -v python' 2>/dev/null || true)
    fi
    if [[ -z "$runtime_python" ]]; then
        process_exe=$(readlink "/proc/$pid/exe" 2>/dev/null || true)
        if [[ "${process_exe##*/}" == python* ]]; then
            runtime_python=$process_exe
        fi
    fi
    if [[ -z "$runtime_python" ]]; then
        echo "NO-GO: cannot locate Python in Nova $ROLE process $pid namespace" >&2
        failures=$((failures + 1))
        continue
    fi

    if printf '%s\n' "$CONTRACT" | nsenter --target "$pid" --mount -- \
            env -i PATH="$proc_path" PYTHONPATH="$proc_pythonpath" \
            HOME=/tmp "$runtime_python" - "$ROLE" "$pid" "$cmdline" \
            "$MIN_INCUS_MIGRATE_DATA_VERSION"; then
        validated=$((validated + 1))
    else
        echo "NO-GO: Nova $ROLE runtime contract failed for pid=$pid cmd=$cmdline" >&2
        failures=$((failures + 1))
    fi
done

if ((failures > 0 || validated == 0)); then
    echo "NO-GO: Nova $ROLE runtime validation failed ($failures failures)" >&2
    exit 1
fi

echo "PASS Nova $ROLE runtime contract processes=$validated"
