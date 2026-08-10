#!/usr/bin/env bash
# Verify that the running Ceilometer role carries the Incus billing contract.

set -euo pipefail

ROLE=${1:-}
PYTHON=${RUNTIME_PYTHON:-/opt/stack/data/venv/bin/python}
CEILOMETER_CONF=${CEILOMETER_CONF:-/etc/ceilometer/ceilometer.conf}
CEILOMETER_DIR=${CEILOMETER_DIR:-/opt/stack/ceilometer}

[[ -x "$PYTHON" ]] || {
    echo "NO-GO: Ceilometer runtime Python is unavailable: $PYTHON" >&2
    exit 1
}

case "$ROLE" in
    compute)
        if systemctl is-active --quiet devstack@ceilometer-acompute; then
            compute_unit=devstack@ceilometer-acompute.service
        elif systemctl is-active --quiet ceilometer-incus-compute.service; then
            compute_unit=ceilometer-incus-compute.service
        else
            echo "NO-GO: ceilometer-acompute is not running" >&2
            exit 1
        fi
        [[ "$(crudini --get "$CEILOMETER_CONF" DEFAULT \
            hypervisor_inspector 2>/dev/null || true)" == incus ]] || {
            echo "NO-GO: Ceilometer is not configured for the Incus inspector" >&2
            exit 1
        }
        grep -Eq '^instance_discovery_method[[:space:]]*=[[:space:]]*naive$' \
            "$CEILOMETER_CONF" || {
            echo "NO-GO: Ceilometer compute discovery still depends on libvirt" >&2
            exit 1
        }
        incus_project=$(crudini --get "$CEILOMETER_CONF" incus project \
            2>/dev/null || true)
        [[ -n "$incus_project" && "$incus_project" != default ]] || {
            echo "NO-GO: Ceilometer is inspecting the default Incus project" >&2
            exit 1
        }
        polling_file=$(crudini --get "$CEILOMETER_CONF" polling cfg_file \
            2>/dev/null || true)
        [[ -f "$polling_file" ]] || {
            echo "NO-GO: Incus polling whitelist is missing: $polling_file" >&2
            exit 1
        }
        "$PYTHON" - "$polling_file" <<'PY'
import sys

import yaml

with open(sys.argv[1], encoding="utf-8") as stream:
    document = yaml.safe_load(stream)
if not isinstance(document, dict) or not document.get("sources"):
    raise SystemExit("NO-GO: Incus polling YAML has no sources")
PY
        grep -Eq '^[[:space:]]*- cpu$' "$polling_file" && \
            grep -Eq '^[[:space:]]*- memory\.usage$' "$polling_file" && \
            grep -Eq '^[[:space:]]*- network\.incoming\.bytes$' \
                "$polling_file" || {
            echo "NO-GO: Incus polling whitelist lacks billing meters" >&2
            exit 1
        }
        "$PYTHON" - <<'PY'
from importlib import metadata

from nova.virt.incus.ceilometer import IncusInspector

entries = {
    entry.name: entry.value
    for entry in metadata.entry_points(group="ceilometer.compute.virt")
}
expected = "nova.virt.incus.ceilometer:IncusInspector"
if entries.get("incus") != expected:
    raise SystemExit(
        "NO-GO: Incus inspector entry point is missing: %r" % entries)
if not callable(IncusInspector.inspect_instance):
    raise SystemExit("NO-GO: Incus inspector cannot inspect instances")
if not callable(IncusInspector.inspect_vnics):
    raise SystemExit("NO-GO: Incus inspector cannot inspect vNICs")
print("PASS Ceilometer Incus compute inspector")
PY
        main_pid=$(systemctl show "$compute_unit" --property MainPID --value)
        if [[ ! "$main_pid" =~ ^[1-9][0-9]*$ ]] || \
                ! pgrep -P "$main_pid" >/dev/null; then
            echo "NO-GO: Ceilometer compute worker is not running" >&2
            exit 1
        fi
        active_since=$(systemctl show "$compute_unit" \
            --property ActiveEnterTimestamp --value)
        runtime_log=$(journalctl -u "$compute_unit" --since "$active_since" \
            --no-pager)
        if grep -Eq \
                'ToozConnectionError|Unable to load the hypervisor inspector|Skip loading extension for cpu|Incus instance .* was not found|PollsterPermanentError' \
                <<<"$runtime_log"; then
            echo "NO-GO: Ceilometer compute worker logged a runtime contract failure" >&2
            exit 1
        fi
        local_instances=$(incus list --project "$incus_project" \
            --format csv --columns n 2>/dev/null || true)
        if [[ -n "$local_instances" ]] && ! grep -Fq \
                'Finished polling pollster cpu in the context of incus_compute' \
                <<<"$runtime_log"; then
            echo "NO-GO: Ceilometer has not completed an Incus CPU poll" >&2
            exit 1
        fi
        ;;
    notification)
        systemctl is-active --quiet devstack@ceilometer-anotification || {
            echo "NO-GO: ceilometer-anotification is not running" >&2
            exit 1
        }
        meter=/etc/ceilometer/meters.d/incus-volume-usage.yaml
        mapping="$CEILOMETER_DIR/ceilometer/publisher/data/gnocchi_resources.yaml"
        [[ -r "$meter" && -r "$mapping" ]] || {
            echo "NO-GO: Incus volume meter or Gnocchi mapping is missing" >&2
            exit 1
        }
        for metric in volume.read.requests volume.read.bytes \
                volume.write.requests volume.write.bytes; do
            grep -Fq "name: '$metric'" "$meter" || {
                echo "NO-GO: missing notification meter $metric" >&2
                exit 1
            }
            grep -Fq "$metric:" "$mapping" || {
                echo "NO-GO: missing Gnocchi resource mapping $metric" >&2
                exit 1
            }
        done
        grep -Fq 'name: ceilometer-volume-io' "$mapping" || {
            echo "NO-GO: ceilometer-volume-io archive policy is missing" >&2
            exit 1
        }
        (cd /tmp && "$PYTHON" - "$CEILOMETER_CONF" <<'PY'
import sys

from oslo_config import cfg

from ceilometer import gnocchi_client
from ceilometer import service

conf = cfg.ConfigOpts()
sys.argv = ["ceilometer-incus-preflight", "--config-file", sys.argv[1]]
service.prepare_service(conf=conf)
client = gnocchi_client.get_gnocchiclient(conf)
required = {"instance", "instance_network_interface", "volume"}
available = {item["name"] for item in client.resource_type.list()}
missing = sorted(required - available)
if missing:
    raise SystemExit(
        "NO-GO: missing Gnocchi resource types: %s" % ", ".join(missing))
PY
        )
        notification_unit=devstack@ceilometer-anotification.service
        active_since=$(systemctl show "$notification_unit" \
            --property ActiveEnterTimestamp --value)
        runtime_log=$(journalctl -u "$notification_unit" \
            --since "$active_since" --no-pager)
        if grep -Eq 'ResourceTypeNotFound|ToozConnectionError' \
                <<<"$runtime_log"; then
            echo "NO-GO: Ceilometer notification publisher logged a runtime contract failure" >&2
            exit 1
        fi
        echo "PASS Ceilometer Incus volume notification meters"
        ;;
    *)
        echo "Usage: $0 {compute|notification}" >&2
        exit 2
        ;;
esac
