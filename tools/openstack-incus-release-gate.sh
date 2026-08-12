#!/usr/bin/env bash
# Aggregate fail-closed release gate for a Nova Incus production candidate.
# shellcheck disable=SC2016  # Embedded bash/Python programs expand remotely.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
RUN_FLEET=${RUN_FLEET:-false}
RUN_TEMPEST=${RUN_TEMPEST:-false}
RUN_PUBLIC_API_E2E=${RUN_PUBLIC_API_E2E:-false}
RUN_SCALE=${RUN_SCALE:-false}
RUN_MIGRATION_MATRIX=${RUN_MIGRATION_MATRIX:-false}
RUN_MIGRATION_MANILA=${RUN_MIGRATION_MANILA:-false}
RUN_DESTRUCTIVE_FENCE=${RUN_DESTRUCTIVE_FENCE:-false}
SSH_KNOWN_HOSTS_FILE=${SSH_KNOWN_HOSTS_FILE:-$HOME/.ssh/known_hosts}
CONTROLLER_OPENRC=${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}
PHASE_FLEET_PASSED=false
PHASE_TEMPEST_PASSED=false
PHASE_PUBLIC_API_E2E_PASSED=false
PHASE_SCALE_PASSED=false
PHASE_MIGRATION_MATRIX_PASSED=false
PHASE_DESTRUCTIVE_FENCE_PASSED=false
ARTIFACT_DIR=${ARTIFACT_DIR:-"$REPO_ROOT/../openstack-incus-release-evidence"}
PYTHON=${PYTHON:-python3}
TOX=${TOX:-tox}

mkdir -p "$ARTIFACT_DIR"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
evidence="$ARTIFACT_DIR/release-gate-$timestamp.log"
exec > >(tee "$evidence") 2>&1

run() {
    printf '\n=== %s ===\n' "$1"
    shift
    "$@"
}

require_bool() {
    case "${!1}" in
        true|false) ;;
        *)
            echo "$1 must be true or false" >&2
            exit 2
            ;;
    esac
}

ssh_target_host() {
    local target=$1 host
    [[ -n "$target" && "$target" != -* && "$target" != *[[:space:]]* ]] || {
        echo "Invalid SSH target: $target" >&2
        return 2
    }
    host=${target##*@}
    if [[ "$host" == \[*\] ]]; then
        host=${host:1:${#host}-2}
    fi
    [[ -n "$host" ]] || {
        echo "SSH target has no host: $target" >&2
        return 2
    }
    printf '%s\n' "$host"
}

require_known_host() {
    local label=$1 target=$2 host
    host=$(ssh_target_host "$target") || exit 2
    ssh-keygen -F "$host" -f "$SSH_KNOWN_HOSTS_FILE" >/dev/null || {
        echo "$label SSH host is absent from known_hosts: $host" >&2
        exit 2
    }
}

require_node_mapping_hosts() {
    local label=$1 mappings=$2 entry target
    local -a entries=()
    IFS=, read -r -a entries <<<"$mappings"
    ((${#entries[@]} > 0)) || {
        echo "$label contains no SSH mappings" >&2
        exit 2
    }
    for entry in "${entries[@]}"; do
        [[ "$entry" == *=* && -n "${entry%%=*}" && -n "${entry#*=}" ]] || {
            echo "Invalid $label entry: $entry" >&2
            exit 2
        }
        target=${entry#*=}
        require_known_host "$label entry ${entry%%=*}" "$target"
    done
}

for setting in \
    RUN_FLEET RUN_TEMPEST RUN_PUBLIC_API_E2E RUN_SCALE RUN_MIGRATION_MATRIX \
    RUN_MIGRATION_MANILA RUN_DESTRUCTIVE_FENCE; do
    require_bool "$setting"
done

if [[ "$RUN_FLEET" == true || "$RUN_MIGRATION_MATRIX" == true ||
      "$RUN_PUBLIC_API_E2E" == true ]]; then
    : "${SSH_IDENTITY:?Set SSH_IDENTITY to the release SSH key}"
    [[ -f "$SSH_IDENTITY" && -r "$SSH_IDENTITY" ]] || {
        echo "SSH identity is not a readable regular file: $SSH_IDENTITY" >&2
        exit 2
    }
    [[ -f "$SSH_KNOWN_HOSTS_FILE" &&
       -r "$SSH_KNOWN_HOSTS_FILE" ]] || {
        echo "SSH known_hosts is not a readable regular file: $SSH_KNOWN_HOSTS_FILE" >&2
        exit 2
    }
    command -v ssh-keygen >/dev/null || {
        echo "ssh-keygen is required for release host identity checks" >&2
        exit 2
    }
fi
if [[ "$RUN_FLEET" == true || "$RUN_PUBLIC_API_E2E" == true ]]; then
    : "${COMPUTE_NODES:?Set COMPUTE_NODES to name=ssh mappings}"
    require_node_mapping_hosts COMPUTE_NODES "$COMPUTE_NODES"
    if [[ "$RUN_FLEET" == true && -n ${CONTROLLER_SSH:-} ]]; then
        require_known_host CONTROLLER_SSH "$CONTROLLER_SSH"
    fi
fi
if [[ "$RUN_MIGRATION_MATRIX" == true ]]; then
    [[ "$RUN_MIGRATION_MANILA" == true ]] || {
        echo "NO-GO: migration matrix requires RUN_MIGRATION_MANILA=true" >&2
        exit 2
    }
    MIGRATION_COMPUTE_NODES=${MIGRATION_COMPUTE_NODES:-${COMPUTE_NODES:-}}
    : "${MIGRATION_COMPUTE_NODES:?Set three name=ssh compute mappings}"
    : "${CONTROLLER_SSH:?Set CONTROLLER_SSH to the API runner}"
    require_node_mapping_hosts \
        MIGRATION_COMPUTE_NODES "$MIGRATION_COMPUTE_NODES"
    require_known_host CONTROLLER_SSH "$CONTROLLER_SSH"
fi
if [[ "$RUN_MIGRATION_MANILA" == true ]]; then
    [[ "$RUN_MIGRATION_MATRIX" == true ]] || {
        echo "RUN_MIGRATION_MANILA requires RUN_MIGRATION_MATRIX=true" >&2
        exit 2
    }
    : "${NOVA_API_NODES:?Enumerate every nova-api host as name=ssh mappings}"
    require_node_mapping_hosts NOVA_API_NODES "$NOVA_API_NODES"
fi

cd "$REPO_ROOT"
run "release identity" bash -c '
    set -euo pipefail
    git status --short --branch
    git show -s --format="commit=%H%ncommit_date=%cI%nsubject=%s" HEAD
    test -z "$(git status --porcelain)" || {
        echo "release worktree is dirty" >&2
        exit 1
    }
'
run "credential pattern audit" bash -c '
    set -euo pipefail
    private_key_pattern="BEGIN (RSA|OPENSSH|EC) PRIVATE"" KEY"
    site_password_pattern="SZzlt""@"
    if git grep -n -I -E \
        "$private_key_pattern|$site_password_pattern" -- .; then
        echo "possible committed credential detected" >&2
        exit 1
    fi
    single_quote=$(printf "\\047")
    double_quote=$(printf "\\042")
    literal_credential_pattern="(password|secret|token)[[:space:]]*=[[:space:]]*[$single_quote$double_quote][^\$<{$single_quote$double_quote]+[$single_quote$double_quote]"
    if git grep -n -I -E \
        "$literal_credential_pattern" \
        -- . \
        ":(exclude)nova/tests/**" \
        ":(exclude)nova_incus_tempest_plugin/tests/**" \
        ":(exclude)doc/source/production_readiness.rst" \
        ":(exclude)etc/openstack-incus/fence.d/*.example"; then
        echo "possible committed literal credential detected" >&2
        exit 1
    fi
'
run "Python unit tests" "$TOX" -e py312
run "pep8" "$TOX" -e pep8
run "shell syntax" bash -c '
    set -euo pipefail
    while IFS= read -r -d "" script; do
        if ! head -n 1 "$script" | grep -Eq "^#!.*(ba)?sh([[:space:]]|$)"; then
            continue
        fi
        bash -n "$script"
    done < <(find tools devstack -type f \( -name "*.sh" -o -perm -u+x \) -print0)
'
run "capability JSON" "$PYTHON" -m json.tool \
    doc/source/support_matrix/capabilities.json
run "documentation" "$TOX" -e docs

if [[ "$RUN_TEMPEST" == true ]]; then
    : "${TEMPEST_DIR:?Set TEMPEST_DIR to the Tempest checkout}"
    : "${TEMPEST_CONFIG:?Set TEMPEST_CONFIG to the generated tempest.conf}"
    TEMPEST_BIN=${TEMPEST_BIN:-tempest}
    TEMPEST_CONCURRENCY=${TEMPEST_CONCURRENCY:-4}
    tempest_executable=$(command -v "$TEMPEST_BIN") || {
        echo "TEMPEST_BIN is not executable: $TEMPEST_BIN" >&2
        exit 2
    }
    tempest_bin_dir=$(dirname -- "$tempest_executable")
    STESTR_BIN=${STESTR_BIN:-"$tempest_bin_dir/stestr"}
    SUBUNIT_STATS_BIN=${SUBUNIT_STATS_BIN:-"$tempest_bin_dir/subunit-stats"}
    SUBUNIT_FILTER_BIN=${SUBUNIT_FILTER_BIN:-"$tempest_bin_dir/subunit-filter"}
    SUBUNIT_LS_BIN=${SUBUNIT_LS_BIN:-"$tempest_bin_dir/subunit-ls"}
    for test_tool in \
        "$STESTR_BIN" "$SUBUNIT_STATS_BIN" \
        "$SUBUNIT_FILTER_BIN" "$SUBUNIT_LS_BIN"; do
        [[ -x "$test_tool" ]] || {
            echo "Tempest result tool is not executable: $test_tool" >&2
            exit 2
        }
    done
    default_tempest_list="$REPO_ROOT/tools/openstack-incus-tempest-include-list.txt"
    default_tempest_exclude_list="$REPO_ROOT/tools/openstack-incus-tempest-exclude-list.txt"
    TEMPEST_INCLUDE_LIST=${TEMPEST_INCLUDE_LIST:-$default_tempest_list}
    TEMPEST_EXCLUDE_LIST=${TEMPEST_EXCLUDE_LIST:-$default_tempest_exclude_list}
    [[ -d "$TEMPEST_DIR" && -r "$TEMPEST_CONFIG" &&
       -r "$TEMPEST_INCLUDE_LIST" && -r "$TEMPEST_EXCLUDE_LIST" ]] || {
        echo "Tempest checkout, config, include list, and exclude list must be readable" >&2
        exit 2
    }
    run "Tempest network validation contract" "$PYTHON" - \
        "$TEMPEST_CONFIG" <<'PY'
import configparser
import sys

config = configparser.ConfigParser()
if not config.read(sys.argv[1]):
    raise SystemExit("cannot read tempest.conf")
if not config.getboolean("validation", "run_validation", fallback=False):
    raise SystemExit(
        "validation.run_validation must be true for the release gate")
PY
    test_list="$ARTIFACT_DIR/tempest-tests-$timestamp.txt"
    (
        cd "$TEMPEST_DIR"
        "$TEMPEST_BIN" run \
            --config-file "$TEMPEST_CONFIG" \
            --list-tests \
            --include-list "$TEMPEST_INCLUDE_LIST" \
            --exclude-list "$TEMPEST_EXCLUDE_LIST"
    ) | tee "$test_list"
    for expected in \
        nova_incus_tempest_plugin.tests.scenario.test_server_basic_ops \
        nova_incus_tempest_plugin.tests.scenario.test_volume_ops \
        tempest.api.compute; do
        grep -q "^$expected" "$test_list" || {
            echo "Tempest discovery did not include $expected" >&2
            exit 1
        }
    done
    required_tempest_tests="$ARTIFACT_DIR/tempest-required-pass-$timestamp.txt"
    grep '^nova_incus_tempest_plugin\.tests\.scenario\.' "$test_list" |
        sort -u >"$required_tempest_tests"
    [[ -s "$required_tempest_tests" ]] || {
        echo "Tempest discovery returned no project scenario tests" >&2
        exit 1
    }
    run "supported Nova public API Tempest regression" \
        bash -c '
            set -euo pipefail
            cd "$1"
            exec "$2" run \
                --config-file "$3" \
                --include-list "$4" \
                --exclude-list "$5" \
                --concurrency "$6" \
                --slowest
        ' _ "$TEMPEST_DIR" "$TEMPEST_BIN" "$TEMPEST_CONFIG" \
        "$TEMPEST_INCLUDE_LIST" "$TEMPEST_EXCLUDE_LIST" \
        "$TEMPEST_CONCURRENCY"
    tempest_stats="$ARTIFACT_DIR/tempest-stats-$timestamp.txt"
    (
        cd "$TEMPEST_DIR"
        "$STESTR_BIN" last --subunit | "$SUBUNIT_STATS_BIN"
    ) | tee "$tempest_stats"
    run "Tempest result contract" "$PYTHON" - "$tempest_stats" <<'PY'
import re
import sys

values = {}
with open(sys.argv[1], encoding="utf-8") as stream:
    for line in stream:
        match = re.fullmatch(
            r"(Total|Passed|Failed|Skipped) tests:\s+(\d+)\s*",
            line)
        if match:
            values[match.group(1).lower()] = int(match.group(2))
required = {"total", "passed", "failed", "skipped"}
if set(values) != required:
    raise SystemExit(
        "subunit-stats output is incomplete: {}".format(values))
if (
    values["total"] <= 0 or values["failed"] != 0 or
    values["passed"] <= 0 or
    values["passed"] + values["skipped"] != values["total"]
):
    raise SystemExit(
        "Tempest result is not a non-empty failure-free run: {}".format(
            values))
PY
    tempest_passed="$ARTIFACT_DIR/tempest-passed-$timestamp.txt"
    (
        cd "$TEMPEST_DIR"
        "$STESTR_BIN" last --subunit |
            "$SUBUNIT_FILTER_BIN" \
                --success --no-error --no-failure --no-skip --no-xfail \
                --no-passthrough |
            "$SUBUNIT_LS_BIN"
    ) | sort -u | tee "$tempest_passed"
    tempest_skipped="$ARTIFACT_DIR/tempest-skipped-$timestamp.txt"
    (
        cd "$TEMPEST_DIR"
        "$STESTR_BIN" last --subunit |
            "$SUBUNIT_FILTER_BIN" \
                --no-success --no-error --no-failure --no-xfail \
                --no-passthrough |
            "$SUBUNIT_LS_BIN"
    ) | sort -u | tee "$tempest_skipped"
    reported_skips=$(awk '/^Skipped tests:/ {print $3}' "$tempest_stats")
    observed_skips=$(wc -l <"$tempest_skipped")
    [[ "$reported_skips" =~ ^[0-9]+$ &&
       "$observed_skips" == "$reported_skips" ]] || {
        echo "Tempest skipped-test evidence is incomplete" >&2
        exit 1
    }
    [[ -s "$tempest_passed" ]] || {
        echo "Tempest result contains no successful test IDs" >&2
        exit 1
    }
    while IFS= read -r required_test; do
        grep -Fqx -- "$required_test" "$tempest_passed" || {
            echo "Required Incus Tempest scenario did not pass: $required_test" >&2
            exit 1
        }
    done <"$required_tempest_tests"
    grep -q '^tempest\.api\.compute\.' "$tempest_passed" || {
        echo "No standard Tempest compute API test passed" >&2
        exit 1
    }
    PHASE_TEMPEST_PASSED=true
else
    echo "NO-GO: RUN_TEMPEST=false; no production release decision" >&2
fi

if [[ "$RUN_FLEET" == true ]]; then
    run "fleet preflight" \
        env \
        SSH_KNOWN_HOSTS_FILE="$SSH_KNOWN_HOSTS_FILE" \
        REQUIRE_MANILA_MIGRATION_RUNTIME="$RUN_MIGRATION_MANILA" \
        NOVA_API_NODES="${NOVA_API_NODES:-}" \
        NOVA_API_RUNTIME_PYTHON="${NOVA_API_RUNTIME_PYTHON:-}" \
        NOVA_COMPUTE_RUNTIME_PYTHON="${NOVA_COMPUTE_RUNTIME_PYTHON:-}" \
        "$SCRIPT_DIR/openstack-incus-fleet-preflight.sh"
    PHASE_FLEET_PASSED=true
else
    echo "NO-GO: RUN_FLEET=false; no production release decision" >&2
fi

if [[ "$RUN_PUBLIC_API_E2E" == true ]]; then
    : "${PUBLIC_API_INITIAL_IMAGE:?Set the admitted non-BFV image ID}"
    : "${PUBLIC_API_BFV_IMAGE:?Set the admitted BFV image ID}"
    : "${PUBLIC_API_FLAVOR:?Set the Incus-compatible Flavor ID}"
    : "${PUBLIC_API_NETWORK:?Set the tenant network ID}"
    : "${PUBLIC_API_VOLUME_TYPE:?Set the Cinder volume type under test}"
    : "${PUBLIC_API_CINDER_POOL:?Set the Cinder RBD pool under test}"
    : "${COMPUTE_NODES:?Set COMPUTE_NODES for exact local cleanup proof}"
    : "${SSH_IDENTITY:?Set SSH_IDENTITY for exact local cleanup proof}"
    : "${NOVA_INSTANCES_PATH:?Set the absolute Nova instances_path}"
    PUBLIC_API_NAME_PREFIX=${PUBLIC_API_NAME_PREFIX:-incus-release-public-api}
    [[ -n "$PUBLIC_API_NAME_PREFIX" ]] || {
        echo "PUBLIC_API_NAME_PREFIX cannot be empty" >&2
        exit 2
    }
    run "initial Cinder data-volume root/cardinality matrix" \
        env \
        RUN_DESTRUCTIVE=true \
        LOCAL_IMAGE="$PUBLIC_API_INITIAL_IMAGE" \
        BFV_IMAGE="$PUBLIC_API_BFV_IMAGE" \
        FLAVOR="$PUBLIC_API_FLAVOR" \
        NETWORK="$PUBLIC_API_NETWORK" \
        VOLUME_TYPE="$PUBLIC_API_VOLUME_TYPE" \
        ROOT_VOLUME_TYPE="${PUBLIC_API_ROOT_VOLUME_TYPE:-$PUBLIC_API_VOLUME_TYPE}" \
        ROOT_VOLUME_SIZE="${PUBLIC_API_BFV_VOLUME_SIZE:-5}" \
        VOLUME_SIZE="${PUBLIC_API_DATA_VOLUME_SIZE:-1}" \
        TIMEOUT="${PUBLIC_API_TIMEOUT:-900}" \
        NAME_PREFIX="$PUBLIC_API_NAME_PREFIX-initial-volume" \
        REQUIRE_HOST_CLEANUP_AUDIT=true \
        COMPUTE_NODES="$COMPUTE_NODES" \
        SSH_IDENTITY="$SSH_IDENTITY" \
        SSH_KNOWN_HOSTS_FILE="$SSH_KNOWN_HOSTS_FILE" \
        NOVA_INSTANCES_PATH="$NOVA_INSTANCES_PATH" \
        "$SCRIPT_DIR/openstack-incus-initial-data-volume-matrix.sh"
    run "BFV snapshot and restore through Nova/Cinder public APIs" \
        env \
        RUN_DESTRUCTIVE=true \
        IMAGE="$PUBLIC_API_BFV_IMAGE" \
        FLAVOR="$PUBLIC_API_FLAVOR" \
        NETWORK="$PUBLIC_API_NETWORK" \
        VOLUME_TYPE="$PUBLIC_API_VOLUME_TYPE" \
        VOLUME_SIZE="${PUBLIC_API_BFV_VOLUME_SIZE:-5}" \
        TIMEOUT="${PUBLIC_API_TIMEOUT:-900}" \
        NAME="$PUBLIC_API_NAME_PREFIX-bfv-snapshot" \
        HOST_SSH_MAP="$COMPUTE_NODES" \
        SSH_IDENTITY="$SSH_IDENTITY" \
        SSH_KNOWN_HOSTS_FILE="$SSH_KNOWN_HOSTS_FILE" \
        "$SCRIPT_DIR/openstack-incus-bfv-snapshot-public-api-e2e.sh"
    run "Glance-to-Cinder BFV RBD copy-on-write clone" \
        env \
        IMAGE="$PUBLIC_API_BFV_IMAGE" \
        VOLUME_TYPE="$PUBLIC_API_VOLUME_TYPE" \
        CINDER_POOL="$PUBLIC_API_CINDER_POOL" \
        CINDER_USER="${PUBLIC_API_CINDER_USER:-cinder}" \
        SIZE="${PUBLIC_API_BFV_VOLUME_SIZE:-5}" \
        TIMEOUT="${PUBLIC_API_TIMEOUT:-900}" \
        NAME="$PUBLIC_API_NAME_PREFIX-bfv-cow" \
        "$SCRIPT_DIR/openstack-incus-bfv-cow-e2e.sh"
    PHASE_PUBLIC_API_E2E_PASSED=true
else
    echo "NO-GO: RUN_PUBLIC_API_E2E=false; no production release decision" >&2
fi

if [[ "$RUN_SCALE" == true ]]; then
    : "${SCALE_IMAGE:?Set SCALE_IMAGE to an immutable admitted image ID}"
    : "${SCALE_FLAVOR:?Set SCALE_FLAVOR to the scale-test Flavor ID}"
    : "${SCALE_NETWORK:?Set SCALE_NETWORK to the scale-test network ID}"
    : "${SCALE_INCUS_HOSTS:?Set SCALE_INCUS_HOSTS to NOVA_HOST=SSH_TARGET mappings}"
    : "${SCALE_MIN_COMPUTE_HOSTS:?Set SCALE_MIN_COMPUTE_HOSTS to the mapped Incus compute count}"
    : "${SCALE_EXPECTED_ROOT_POOL:?Set the exact Incus root pool name}"
    : "${SCALE_EXPECTED_PROCESS_LIMIT:?Set the exact Incus PID limit}"
    : "${SCALE_RBD_INVENTORY_COMMAND:?Set a read-only JSON RBD inventory helper}"
    : "${SCALE_OVN_LSP_INVENTORY_COMMAND:?Set a read-only JSON OVN LSP inventory helper}"
    : "${SCALE_CEPH_STATUS_COMMAND:?Set a read-only JSON Ceph status helper}"
    : "${SCALE_IDMAP_INVENTORY_COMMAND:?Set the exact JSON etcd ID-map inventory helper}"
    : "${SCALE_MAX_HOST_SKEW_PERCENT:?Set the maximum Incus host skew percentage}"
    : "${SCALE_MIN_SUBMIT_THROUGHPUT:?Set the minimum accepted creates per second}"
    : "${SCALE_MIN_ACTIVE_THROUGHPUT:?Set the minimum all-ACTIVE servers per second}"
    : "${SCALE_MAX_CREATE_API_P95:?Set the create API p95 SLO in seconds}"
    : "${SCALE_MAX_ACTIVE_P95:?Set the create-to-ACTIVE p95 SLO in seconds}"
    : "${SCALE_MAX_ACTIVE_P99:?Set the create-to-ACTIVE p99 SLO in seconds}"
    : "${SCALE_MAX_NOVA_LIST_P95:?Set the Nova list p95 SLO in seconds}"
    : "${SCALE_MAX_NEUTRON_LIST_SECONDS:?Set the Neutron list SLO in seconds}"
    : "${SCALE_MAX_DELETE_API_P95:?Set the delete API p95 SLO in seconds}"
    : "${SCALE_MAX_CLEANUP_SECONDS:?Set the total cleanup SLO in seconds}"
    : "${SCALE_HOST_INITIAL_MIN_FREE_BYTES:?Set initial minimum host storage free bytes}"
    : "${SCALE_HOST_INITIAL_MIN_FREE_PERCENT:?Set initial minimum host storage free percentage}"
    : "${SCALE_HOST_INITIAL_MIN_INODE_PERCENT:?Set initial minimum host storage free inode percentage}"
    : "${SCALE_HOST_RUNTIME_MIN_FREE_BYTES:?Set runtime minimum host storage free bytes}"
    : "${SCALE_HOST_RUNTIME_MIN_FREE_PERCENT:?Set runtime minimum host storage free percentage}"
    : "${SCALE_HOST_RUNTIME_MIN_INODE_PERCENT:?Set runtime minimum host storage free inode percentage}"

    SCALE_PER_COMPUTE_CHECKPOINTS=${SCALE_PER_COMPUTE_CHECKPOINTS:-100,500,1000}
    SCALE_MIN_PER_COMPUTE_PERCENT=${SCALE_MIN_PER_COMPUTE_PERCENT:-90}
    SCALE_IDLE_SOAK_SECONDS=${SCALE_IDLE_SOAK_SECONDS:-900}
    SCALE_TELEMETRY_INTERVAL=${SCALE_TELEMETRY_INTERVAL:-60}
    SCALE_PERIODIC_TASK_INTERVAL_SECONDS=${SCALE_PERIODIC_TASK_INTERVAL_SECONDS:-60}
    SCALE_MINIMUM_SOAK_PERIODIC_CYCLES=${SCALE_MINIMUM_SOAK_PERIODIC_CYCLES:-10}
    SCALE_CONCURRENCY=${SCALE_CONCURRENCY:-16}
    SCALE_DELETE_CONCURRENCY=${SCALE_DELETE_CONCURRENCY:-16}
    SCALE_STAGE_TIMEOUT=${SCALE_STAGE_TIMEOUT:-7200}
    SCALE_CLEANUP_TIMEOUT=${SCALE_CLEANUP_TIMEOUT:-7200}
    SCALE_CLEANUP_SETTLE_TIME=${SCALE_CLEANUP_SETTLE_TIME:-30}
    SCALE_POLL_INTERVAL=${SCALE_POLL_INTERVAL:-10}
    SCALE_QUERY_CHUNK_SIZE=${SCALE_QUERY_CHUNK_SIZE:-100}
    SCALE_AUDIT_CONCURRENCY=${SCALE_AUDIT_CONCURRENCY:-16}
    SCALE_AUDIT_COMMAND_TIMEOUT=${SCALE_AUDIT_COMMAND_TIMEOUT:-120}
    SCALE_DELETE_REQUEST_ATTEMPTS=${SCALE_DELETE_REQUEST_ATTEMPTS:-10}
    SCALE_DELETE_RETRY_BACKOFF=${SCALE_DELETE_RETRY_BACKOFF:-1}
    SCALE_INCUS_PROJECT=${SCALE_INCUS_PROJECT:-nova}
    SCALE_SSH_CONNECT_TIMEOUT=${SCALE_SSH_CONNECT_TIMEOUT:-10}
    SCALE_SSH_COMMAND_TIMEOUT=${SCALE_SSH_COMMAND_TIMEOUT:-120}
    SCALE_NAME_PREFIX=${SCALE_NAME_PREFIX:-incus-release-scale}

    for checkpoint in 100 500 1000; do
        case ",$SCALE_PER_COMPUTE_CHECKPOINTS," in
            *",$checkpoint,"*) ;;
            *)
                echo "SCALE_PER_COMPUTE_CHECKPOINTS must include $checkpoint" >&2
                exit 2
                ;;
        esac
    done
    [[ "$SCALE_IDLE_SOAK_SECONDS" =~ ^[0-9]+$ &&
       "$SCALE_IDLE_SOAK_SECONDS" -ge 900 ]] || {
        echo "SCALE_IDLE_SOAK_SECONDS must be an integer of at least 900" >&2
        exit 2
    }

    read -r -a scale_incus_hosts <<< "$SCALE_INCUS_HOSTS"
    ((${#scale_incus_hosts[@]} > 0)) || {
        echo "SCALE_INCUS_HOSTS must contain at least one host mapping" >&2
        exit 2
    }
    [[ "$SCALE_MIN_COMPUTE_HOSTS" == "${#scale_incus_hosts[@]}" ]] || {
        echo "SCALE_MIN_COMPUTE_HOSTS must equal mapped Incus hosts" >&2
        exit 2
    }
    scale_artifact="$ARTIFACT_DIR/scale-$timestamp.json"
    scale_args=(
        "$PYTHON"
        "$SCRIPT_DIR/openstack-incus-scale-e2e.py"
        --image "$SCALE_IMAGE"
        --flavor "$SCALE_FLAVOR"
        --network "$SCALE_NETWORK"
        --per-compute-checkpoints "$SCALE_PER_COMPUTE_CHECKPOINTS"
        --concurrency "$SCALE_CONCURRENCY"
        --delete-concurrency "$SCALE_DELETE_CONCURRENCY"
        --stage-timeout "$SCALE_STAGE_TIMEOUT"
        --cleanup-timeout "$SCALE_CLEANUP_TIMEOUT"
        --cleanup-settle-time "$SCALE_CLEANUP_SETTLE_TIME"
        --poll-interval "$SCALE_POLL_INTERVAL"
        --query-chunk-size "$SCALE_QUERY_CHUNK_SIZE"
        --audit-concurrency "$SCALE_AUDIT_CONCURRENCY"
        --min-compute-hosts "$SCALE_MIN_COMPUTE_HOSTS"
        --expected-root-pool "$SCALE_EXPECTED_ROOT_POOL"
        --expected-process-limit "$SCALE_EXPECTED_PROCESS_LIMIT"
        --rbd-inventory-command "$SCALE_RBD_INVENTORY_COMMAND"
        --ovn-lsp-inventory-command "$SCALE_OVN_LSP_INVENTORY_COMMAND"
        --ceph-status-command "$SCALE_CEPH_STATUS_COMMAND"
        --idmap-inventory-command "$SCALE_IDMAP_INVENTORY_COMMAND"
        --audit-command-timeout "$SCALE_AUDIT_COMMAND_TIMEOUT"
        --idle-soak-seconds "$SCALE_IDLE_SOAK_SECONDS"
        --telemetry-interval "$SCALE_TELEMETRY_INTERVAL"
        --periodic-task-interval-seconds \
            "$SCALE_PERIODIC_TASK_INTERVAL_SECONDS"
        --minimum-soak-periodic-cycles \
            "$SCALE_MINIMUM_SOAK_PERIODIC_CYCLES"
        --delete-request-attempts "$SCALE_DELETE_REQUEST_ATTEMPTS"
        --delete-retry-backoff "$SCALE_DELETE_RETRY_BACKOFF"
        --max-host-skew-percent "$SCALE_MAX_HOST_SKEW_PERCENT"
        --min-per-compute-percent "$SCALE_MIN_PER_COMPUTE_PERCENT"
        --min-submit-throughput "$SCALE_MIN_SUBMIT_THROUGHPUT"
        --min-active-throughput "$SCALE_MIN_ACTIVE_THROUGHPUT"
        --max-create-api-p95 "$SCALE_MAX_CREATE_API_P95"
        --max-active-p95 "$SCALE_MAX_ACTIVE_P95"
        --max-active-p99 "$SCALE_MAX_ACTIVE_P99"
        --max-nova-list-p95 "$SCALE_MAX_NOVA_LIST_P95"
        --max-neutron-list-seconds "$SCALE_MAX_NEUTRON_LIST_SECONDS"
        --max-delete-api-p95 "$SCALE_MAX_DELETE_API_P95"
        --max-cleanup-seconds "$SCALE_MAX_CLEANUP_SECONDS"
        --name-prefix "$SCALE_NAME_PREFIX"
        --artifact "$scale_artifact"
        --incus-project "$SCALE_INCUS_PROJECT"
        --ssh-connect-timeout "$SCALE_SSH_CONNECT_TIMEOUT"
        --ssh-command-timeout "$SCALE_SSH_COMMAND_TIMEOUT"
        --host-initial-min-free-bytes "$SCALE_HOST_INITIAL_MIN_FREE_BYTES"
        --host-initial-min-free-percent "$SCALE_HOST_INITIAL_MIN_FREE_PERCENT"
        --host-initial-min-inode-percent "$SCALE_HOST_INITIAL_MIN_INODE_PERCENT"
        --host-runtime-min-free-bytes "$SCALE_HOST_RUNTIME_MIN_FREE_BYTES"
        --host-runtime-min-free-percent "$SCALE_HOST_RUNTIME_MIN_FREE_PERCENT"
        --host-runtime-min-inode-percent "$SCALE_HOST_RUNTIME_MIN_INODE_PERCENT"
    )
    if [[ -n ${SCALE_CLOUD:-} ]]; then
        scale_args+=(--cloud "$SCALE_CLOUD")
    fi
    for host in "${scale_incus_hosts[@]}"; do
        scale_args+=(--incus-host "$host")
    done
    run "per-compute 100/500/1000 Nova and Incus scale validation" \
        "${scale_args[@]}"
    [[ ! -e "$scale_artifact.wal" ]] || {
        echo "scale artifact WAL was not compacted" >&2
        exit 1
    }
    run "scale evidence summary" "$PYTHON" - \
        "$scale_artifact" "$SCALE_PER_COMPUTE_CHECKPOINTS" \
        "${#scale_incus_hosts[@]}" "$SCALE_MIN_PER_COMPUTE_PERCENT" \
        "$SCALE_IDLE_SOAK_SECONDS" \
        "$SCALE_MINIMUM_SOAK_PERIODIC_CYCLES" <<'PY'
import json
import math
import sys
import uuid

path = sys.argv[1]
expected_per_compute = [int(item) for item in sys.argv[2].split(",")]
host_count = int(sys.argv[3])
minimum_per_compute_percent = float(sys.argv[4])
required_soak_seconds = float(sys.argv[5])
minimum_soak_cycles = int(sys.argv[6])
expected_targets = [value * host_count for value in expected_per_compute]
with open(path, encoding="utf-8") as stream:
    evidence = json.load(stream)


def require(condition, message):
    if not condition:
        raise SystemExit(message)


def finite_nonnegative(value):
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool) and
        math.isfinite(value) and value >= 0)


def validate_telemetry(snapshot, mapped_hosts, description):
    require(
        isinstance(snapshot, dict),
        "{} telemetry is not an object".format(description))
    hosts = snapshot.get("hosts")
    require(
        isinstance(hosts, dict) and set(hosts) == mapped_hosts,
        "{} telemetry host set differs: {}".format(description, hosts))
    for host, value in hosts.items():
        require(
            isinstance(value, dict),
            "{} telemetry for {} is invalid".format(description, host))
        total = value.get("memory_total_bytes")
        available = value.get("memory_available_bytes")
        require(
            finite_nonnegative(total) and total > 0 and
            finite_nonnegative(available) and available <= total,
            "{} memory telemetry for {} is invalid".format(
                description, host))
        cpu = value.get("host_cpu_ticks")
        require(
            isinstance(cpu, dict) and
            finite_nonnegative(cpu.get("total")) and
            finite_nonnegative(cpu.get("idle")) and
            cpu["total"] >= cpu["idle"],
            "{} CPU telemetry for {} is invalid".format(
                description, host))
        processes = value.get("processes")
        require(
            isinstance(processes, dict),
            "{} process telemetry for {} is invalid".format(
                description, host))
        for process in ("incusd", "nova_compute"):
            stats = processes.get(process)
            require(
                isinstance(stats, dict) and
                isinstance(stats.get("process_count"), int) and
                stats["process_count"] >= 1 and
                finite_nonnegative(stats.get("cpu_seconds")) and
                finite_nonnegative(stats.get("rss_bytes")) and
                finite_nonnegative(stats.get("fd_count")),
                "{} {} telemetry for {} is invalid: {}".format(
                    description, process, host, stats))


def validate_idmap_delta(value, expected_count, baseline, description):
    require(
        isinstance(value, dict),
        "{} ID-map evidence is invalid".format(description))
    require(
        value.get("baseline_revision") == baseline["revision"] and
        value.get("baseline_count") == len(baseline["entries"]),
        "{} ID-map baseline differs".format(description))
    require(
        str(value.get("current_revision", "")).isdigit() and
        int(value["current_revision"]) >= int(baseline["revision"]),
        "{} ID-map revision is invalid".format(description))
    run_keys = value.get("run_keys")
    run_key_digests = value.get("run_key_digests")
    by_instance = value.get("run_keys_by_instance")
    baseline_by_instance = value.get("baseline_run_keys_by_instance")
    require(
        isinstance(run_keys, list) and len(run_keys) == len(set(run_keys)) and
        value.get("run_key_count") == len(run_keys) and
        isinstance(run_key_digests, dict) and
        set(run_key_digests) == set(run_keys),
        "{} ID-map run-key evidence is inconsistent".format(description))
    require(
        isinstance(by_instance, dict) and
        len(by_instance) == expected_count and
        all(isinstance(keys, list) and keys for keys in by_instance.values()),
        "{} ID-map instance coverage is incomplete".format(description))
    require(
        isinstance(baseline_by_instance, dict) and
        len(baseline_by_instance) == expected_count and
        all(keys == [] for keys in baseline_by_instance.values()),
        "{} ID-map baseline already references run instances".format(
            description))
    for instance_id in by_instance:
        uuid.UUID(instance_id)
    require(
        set(run_keys).issubset(set(value.get("added_keys", []))),
        "{} ID-map run keys are absent from the baseline delta".format(
            description))


require(
    evidence["schema_version"] == 4,
    "unexpected scale evidence schema: {}".format(
        evidence.get("schema_version")))
uuid.UUID(evidence["run_id"])
uuid.UUID(evidence["project_id"])
require(
    evidence["status"] == "passed",
    "scale run did not pass: {}".format(evidence.get("status")))
require(
    evidence.get("failure") is None,
    "scale evidence contains a failure: {}".format(evidence.get("failure")))
require(
    evidence["cleanup"]["completed"] is True,
    "scale cleanup is incomplete: {}".format(evidence["cleanup"]))
checkpoints = evidence["checkpoints"]
targets = [item["target"] for item in checkpoints]
require(
    targets == expected_targets,
    "scale checkpoints differ: {} != {}".format(
        targets, expected_targets))
require(
    all(target in expected_per_compute for target in (100, 500, 1000)),
    "mandatory scale checkpoints are missing: {}".format(targets))
final_target = expected_targets[-1]
require(
    len(set(evidence["server_ids"])) == final_target,
    "scale artifact server UUID count differs from final target")
require(
    len(evidence["instance_names"]) == final_target,
    "scale artifact instance-name count differs from final target")
require(
    evidence["cleanup"]["server_count"] == final_target,
    "scale cleanup count differs from final target")
preflight = evidence["preflight"]
baseline_ceph = preflight["ceph"]
baseline_idmap = evidence.get("idmap_inventory_baseline")
require(
    isinstance(baseline_idmap, dict) and
    str(baseline_idmap.get("revision", "")).isdigit() and
    int(baseline_idmap["revision"]) > 0 and
    isinstance(baseline_idmap.get("entries"), dict),
    "ID-map etcd baseline is invalid: {}".format(baseline_idmap))
require(
    preflight.get("idmap_inventory_baseline") == {
        "revision": baseline_idmap["revision"],
        "key_count": len(baseline_idmap["entries"]),
    },
    "ID-map preflight summary differs from the exact baseline")
require(
    isinstance(evidence.get("inventory_command_fingerprints"), dict) and
    len(evidence["inventory_command_fingerprints"].get(
        "idmap_keys", "")) == 64,
    "ID-map inventory helper fingerprint is absent")
require(
    baseline_ceph["health"] == "HEALTH_OK",
    "Ceph preflight is unhealthy: {}".format(baseline_ceph))
require(
    baseline_ceph["required_bytes"] > 0,
    "Ceph preflight required no capacity: {}".format(baseline_ceph))
mapped_hosts = set(preflight["fleet"]["incus_compute_hosts"])
require(mapped_hosts, "scale evidence contains no mapped Incus computes")
require(
    len(mapped_hosts) == host_count,
    "mapped Incus host count differs from release input")
validate_telemetry(preflight["telemetry"], mapped_hosts, "preflight")
for index, checkpoint in enumerate(checkpoints):
    target = checkpoint["target"]
    target_per_compute = expected_per_compute[index]
    require(
        checkpoint.get("target_per_compute") == target_per_compute,
        "checkpoint {} per-compute target differs".format(target))
    require(
        checkpoint["performance_slo"]["passed"] is True,
        "checkpoint {} failed its latency SLO".format(target))
    require(
        checkpoint["throughput"]["slo"]["passed"] is True,
        "checkpoint {} failed its throughput SLO".format(target))
    require(
        checkpoint["host_distribution"]["passed"] is True,
        "checkpoint {} failed host distribution".format(target))
    distribution = checkpoint["host_distribution"]
    expected_minimum = math.ceil(
        target_per_compute * minimum_per_compute_percent / 100.0)
    require(
        distribution.get("target_per_compute") == target_per_compute and
        distribution.get("minimum_per_compute") == expected_minimum and
        distribution.get("below_minimum") == {} and
        set(distribution.get("counts", {})) == mapped_hosts and
        sum(distribution["counts"].values()) == target and
        all(value >= expected_minimum
            for value in distribution["counts"].values()),
        "checkpoint {} per-compute distribution is invalid: {}".format(
            target, distribution))
    require(
        checkpoint["incus"]["instance_owners"] == target,
        "checkpoint {} has incomplete Incus ownership".format(target))
    require(
        checkpoint["incus"]["profiles"] == target,
        "checkpoint {} has incomplete Incus profiles".format(target))
    require(
        checkpoint["incus"]["idmap_ranges"] >= target * 2,
        "checkpoint {} has incomplete UID/GID idmaps".format(target))
    require(
        checkpoint["placement"]["consumer_count"] == target,
        "checkpoint {} has incomplete Placement consumers".format(target))
    require(
        checkpoint["control_plane_query_slo"]["passed"] is True,
        "checkpoint {} failed its control-plane query SLO".format(target))
    audit_seconds = checkpoint.get("audit_seconds")
    require(
        isinstance(audit_seconds, dict) and
        finite_nonnegative(audit_seconds.get("total")) and
        all(finite_nonnegative(value) for value in audit_seconds.values()),
        "checkpoint {} audit timing is invalid".format(target))
    validate_idmap_delta(
        checkpoint["idmap_etcd"], target, baseline_idmap,
        "checkpoint {}".format(target))
    validate_telemetry(
        checkpoint["telemetry"], mapped_hosts,
        "checkpoint {}".format(target))
    host_storage = checkpoint["host_storage"]
    require(
        set(host_storage) == mapped_hosts,
        "checkpoint {} host storage fleet differs: {}".format(
            target, host_storage))
    for paths in host_storage.values():
        require(
            set(paths) == {"/var/lib/incus", "/var/log/incus"},
            "checkpoint {} host storage paths differ: {}".format(
                target, paths))
        for stats in paths.values():
            require(
                stats["total_bytes"] > 0 and
                stats["available_bytes"] >= 0 and
                stats["total_inodes"] > 0 and
                stats["available_inodes"] >= 0,
                "checkpoint {} host storage evidence is invalid: {}".format(
                    target, stats))
    ceph = checkpoint["ceph"]
    require(
        ceph["health"] == "HEALTH_OK",
        "checkpoint {} Ceph health failed: {}".format(target, ceph))
    require(
        ceph["fsid"] == baseline_ceph["fsid"] and
        ceph["pool"] == baseline_ceph["pool"],
        "checkpoint {} Ceph identity changed: {}".format(target, ceph))
    external = checkpoint["external_inventory"]
    require(
        external["expected_ovn_lsp_count"] == target,
        "checkpoint {} has incomplete OVN LSP evidence".format(target))
    require(
        len(external["expected_rbd_images"]) == target,
        "checkpoint {} has incomplete RBD evidence".format(target))

soak = evidence.get("idle_soak")
require(
    isinstance(soak, dict) and
    soak.get("attempted") is True and soak.get("completed") is True,
    "idle soak did not complete: {}".format(soak))
require(
    soak.get("configured_seconds") == required_soak_seconds and
    finite_nonnegative(soak.get("actual_seconds")) and
    soak["actual_seconds"] >= required_soak_seconds and
    soak.get("minimum_periodic_cycles") == minimum_soak_cycles and
    soak.get("covered_periodic_cycles", 0) >= minimum_soak_cycles,
    "idle soak duration or periodic coverage is invalid: {}".format(soak))
require(
    soak.get("server_count") == final_target and
    soak.get("host_distribution", {}).get("passed") is True and
    soak["host_distribution"].get("target_per_compute") ==
    expected_per_compute[-1],
    "idle soak server distribution is invalid")
samples = soak.get("telemetry_samples")
summary_telemetry = soak.get("telemetry_summary")
require(
    isinstance(samples, list) and len(samples) >= 2 and
    isinstance(summary_telemetry, dict) and
    summary_telemetry.get("sample_count") == len(samples),
    "idle soak telemetry samples are incomplete")
for index, sample in enumerate(samples):
    validate_telemetry(
        sample, mapped_hosts, "idle soak sample {}".format(index))
require(
    set(summary_telemetry.get("hosts", {})) == mapped_hosts,
    "idle soak telemetry summary host set differs")
for host, value in summary_telemetry["hosts"].items():
    require(
        finite_nonnegative(value.get("host_cpu_busy_percent")) and
        value["host_cpu_busy_percent"] <= 100 and
        finite_nonnegative(value.get("minimum_available_memory_bytes")),
        "idle soak host telemetry summary is invalid for {}".format(host))
    processes = value.get("processes", {})
    for process in ("incusd", "nova_compute"):
        stats = processes.get(process)
        require(
            isinstance(stats, dict) and
            finite_nonnegative(stats.get("cpu_seconds_delta")) and
            finite_nonnegative(stats.get("peak_rss_bytes")) and
            finite_nonnegative(stats.get("peak_fd_count")) and
            stats.get("peak_process_count", 0) >= 1,
            "idle soak {} summary is invalid for {}".format(process, host))
soak_backend = soak.get("backend_audit", {})
validate_idmap_delta(
    soak_backend["idmap_etcd"], final_target, baseline_idmap,
    "idle soak")
require(
    soak_backend["incus"]["instance_owners"] == final_target and
    soak_backend["placement"]["consumer_count"] == final_target,
    "idle soak backend ownership evidence is incomplete")
require(
    isinstance(soak.get("audit_seconds"), dict) and
    all(finite_nonnegative(value)
        for value in soak["audit_seconds"].values()),
    "idle soak audit timings are invalid")

residual = evidence["cleanup"]["residual_audit"]
for key in (
        "neutron_ports", "incus_instances", "incus_profiles",
        "placement_consumers"):
    require(
        residual[key] == 0,
        "cleanup retained {}: {}".format(key, residual[key]))
require(
    residual["rbd_images"]["residual_count"] == 0,
    "cleanup retained RBD images: {}".format(residual))
require(
    residual["ovn_lsps"]["residual_count"] == 0,
    "cleanup retained OVN LSPs: {}".format(residual))
cleanup_idmap = residual.get("idmap_etcd")
require(
    isinstance(cleanup_idmap, dict) and
    cleanup_idmap.get("baseline_revision") == baseline_idmap["revision"] and
    str(cleanup_idmap.get("current_revision", "")).isdigit() and
    int(cleanup_idmap["current_revision"]) >=
    max(
        int(checkpoints[-1]["idmap_etcd"]["current_revision"]),
        int(soak_backend["idmap_etcd"]["current_revision"])) and
    cleanup_idmap.get("run_key_count") == 0 and
    cleanup_idmap.get("run_keys") == [] and
    cleanup_idmap.get("run_key_digests") == {} and
    cleanup_idmap.get("unchanged_known_run_keys") == [] and
    isinstance(cleanup_idmap.get("run_keys_by_instance"), dict) and
    len(cleanup_idmap["run_keys_by_instance"]) == final_target and
    all(keys == []
        for keys in cleanup_idmap["run_keys_by_instance"].values()),
    "cleanup retained ID-map etcd state: {}".format(cleanup_idmap))
require(
    evidence["cleanup"]["performance_slo"]["passed"] is True,
    "cleanup performance SLO failed: {}".format(evidence["cleanup"]))
require(
    finite_nonnegative(evidence["cleanup"].get("cleanup_seconds")) and
    finite_nonnegative(
        evidence["cleanup"].get("business_cleanup_seconds")) and
    finite_nonnegative(evidence["cleanup"].get("audit_seconds")) and
    evidence["cleanup"]["business_cleanup_seconds"] <=
    evidence["cleanup"]["cleanup_seconds"],
    "cleanup business/audit timing evidence is invalid")
summary = {
    "artifact": path,
    "run_id": evidence["run_id"],
    "targets": targets,
    "per_compute_targets": expected_per_compute,
    "mapped_host_count": host_count,
    "status": evidence["status"],
    "cleanup_completed": evidence["cleanup"]["completed"],
    "cleanup_seconds": evidence["cleanup"]["cleanup_seconds"],
    "idle_soak_seconds": soak["actual_seconds"],
}
print(json.dumps(summary, indent=2, sort_keys=True))
PY
    PHASE_SCALE_PASSED=true
else
    echo "NO-GO: RUN_SCALE=false; no production release decision" >&2
fi

controller_openstack() {
    local command_line
    printf -v command_line '%q ' openstack "$@"
    ssh -i "$SSH_IDENTITY" \
        -o BatchMode=yes \
        -o StrictHostKeyChecking=yes \
        -o "UserKnownHostsFile=$SSH_KNOWN_HOSTS_FILE" \
        "$CONTROLLER_SSH" \
        "source $CONTROLLER_OPENRC >/dev/null 2>&1; $command_line"
}

validate_manila_backend() {
    local share_ref=$1 expected_protocol=$2 share_type_ref=$3
    local share_json share_type_json
    share_json=$(controller_openstack share show "$share_ref" -f json)
    share_type_json=$(controller_openstack \
        share type show "$share_type_ref" -f json)
    MANILA_SHARE_JSON="$share_json" \
    MANILA_SHARE_TYPE_JSON="$share_type_json" \
        "$PYTHON" - "$expected_protocol" <<'PY'
import ast
import json
import os
import sys


def normalized(data):
    return {
        str(key).strip().lower().replace(" ", "_"): value
        for key, value in data.items()
    }


def as_mapping(value):
    if isinstance(value, dict):
        return normalized(value)
    if isinstance(value, str):
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return {}
        if isinstance(parsed, dict):
            return normalized(parsed)
    return {}


share = normalized(json.loads(os.environ["MANILA_SHARE_JSON"]))
share_type = normalized(json.loads(os.environ["MANILA_SHARE_TYPE_JSON"]))
expected_protocol = sys.argv[1].upper()
protocol = str(
    share.get("share_proto") or share.get("protocol") or ""
).upper()
if protocol != expected_protocol:
    raise SystemExit(
        "share {} protocol is {}, expected {}".format(
            share.get("id", "unknown"), protocol or "missing",
            expected_protocol))
if str(share.get("status", "")).lower() != "available":
    raise SystemExit(
        "share {} must be available, status={}".format(
            share.get("id", "unknown"), share.get("status", "missing")))

expected_type = {
    str(share_type.get("id") or ""),
    str(share_type.get("name") or ""),
}
actual_type = {
    str(share.get("share_type") or ""),
    str(share.get("share_type_name") or ""),
}
expected_type.discard("")
actual_type.discard("")
if not expected_type or not actual_type.intersection(expected_type):
    raise SystemExit(
        "share {} type {} does not match snapshot type {}".format(
            share.get("id", "unknown"), sorted(actual_type),
            sorted(expected_type)))

specs = as_mapping(
    share_type.get("optional_extra_specs") or
    share_type.get("extra_specs") or {})
missing_snapshot_specs = []
for key in ("snapshot_support", "create_share_from_snapshot_support"):
    value = specs.get(key) or share_type.get(key)
    value = str(value or "").replace("<is>", "").strip()
    if value.lower() != "true":
        missing_snapshot_specs.append(key)
if missing_snapshot_specs:
    raise SystemExit(
        "share type {} must declare these specs True: {}".format(
            share_type.get("name") or share_type.get("id") or "unknown",
            ",".join(missing_snapshot_specs)))

print("{} {} {}".format(
    share.get("id", "unknown"), protocol,
    share_type.get("id") or share_type.get("name") or "unknown"))
PY
}

if [[ "$RUN_MIGRATION_MATRIX" == true ]]; then
    MIGRATION_COMPUTE_NODES=${MIGRATION_COMPUTE_NODES:-${COMPUTE_NODES:-}}
    : "${MIGRATION_COMPUTE_NODES:?Set three name=ssh compute mappings}"
    : "${SSH_IDENTITY:?Set SSH_IDENTITY to the compute test key}"
    : "${CONTROLLER_SSH:?Set CONTROLLER_SSH to the API runner}"
    : "${MIGRATION_LOCAL_IMAGE:?Set the admitted CRIU local-root image ID}"
    : "${MIGRATION_BFV_IMAGE:?Set the admitted CRIU BFV image ID}"
    : "${MIGRATION_FLAVOR:?Set the Incus migration Flavor ID}"
    : "${MIGRATION_NETWORK:?Set the migration tenant network ID}"
    : "${MIGRATION_ROOT_VOLUME_TYPE:?Set the BFV Cinder volume type}"
    : "${MIGRATION_DATA_VOLUME_TYPE:?Set the data-volume Cinder type}"
    : "${MIGRATION_MANILA_SHARES:?Set at least three independent Manila shares}"
    : "${MIGRATION_MANILA_NFS_SHARE:?Set a real NFS share from the matrix}"
    : "${MIGRATION_MANILA_CEPHFS_SHARE:?Set a real CephFS share from the matrix}"
    : "${MIGRATION_MANILA_NFS_SHARE_TYPE:?Set a snapshot-capable NFS share type}"
    : "${MIGRATION_MANILA_CEPHFS_SHARE_TYPE:?Set a snapshot-capable CephFS share type}"

    IFS=, read -r -a migration_nodes <<<"$MIGRATION_COMPUTE_NODES"
    ((${#migration_nodes[@]} == 3)) || {
        echo "MIGRATION_COMPUTE_NODES must contain exactly three mappings" >&2
        exit 2
    }
    declare -a migration_node_names=() migration_node_ssh=()
    declare -A migration_unique_nodes=()
    for entry in "${migration_nodes[@]}"; do
        [[ "$entry" == *=* && -n "${entry%%=*}" && -n "${entry#*=}" ]] || {
            echo "Invalid MIGRATION_COMPUTE_NODES entry: $entry" >&2
            exit 2
        }
        node_name=${entry%%=*}
        node_ssh=${entry#*=}
        [[ -z ${migration_unique_nodes["$node_name"]+x} ]] || {
            echo "Duplicate migration compute name: $node_name" >&2
            exit 2
        }
        migration_unique_nodes["$node_name"]=1
        migration_node_names+=("$node_name")
        migration_node_ssh+=("$node_ssh")
    done
    read -r -a migration_manila_shares <<<"$MIGRATION_MANILA_SHARES"
    ((${#migration_manila_shares[@]} >= 3)) || {
        echo "MIGRATION_MANILA_SHARES must contain at least three shares" >&2
        exit 2
    }
    declare -A migration_unique_shares=()
    for share in "${migration_manila_shares[@]}"; do
        [[ -z ${migration_unique_shares["$share"]+x} ]] || {
            echo "MIGRATION_MANILA_SHARES contains a duplicate: $share" >&2
            exit 2
        }
        migration_unique_shares["$share"]=1
    done
    for required_share in \
        "$MIGRATION_MANILA_NFS_SHARE" \
        "$MIGRATION_MANILA_CEPHFS_SHARE"; do
        [[ -n ${migration_unique_shares["$required_share"]+x} ]] || {
            echo "Required protocol share is absent from MIGRATION_MANILA_SHARES: $required_share" >&2
            exit 2
        }
    done
    [[ "$MIGRATION_MANILA_NFS_SHARE" != \
       "$MIGRATION_MANILA_CEPHFS_SHARE" ]] || {
        echo "NFS and CephFS release evidence must use different shares" >&2
        exit 2
    }
    migration_third_share=
    for share in "${migration_manila_shares[@]}"; do
        if [[ "$share" != "$MIGRATION_MANILA_NFS_SHARE" && \
              "$share" != "$MIGRATION_MANILA_CEPHFS_SHARE" ]]; then
            migration_third_share=$share
            break
        fi
    done
    [[ -n "$migration_third_share" ]] || {
        echo "MIGRATION_MANILA_SHARES needs a third independent share" >&2
        exit 2
    }
    migration_three_shares="$MIGRATION_MANILA_NFS_SHARE "
    migration_three_shares+="$MIGRATION_MANILA_CEPHFS_SHARE "
    migration_three_shares+="$migration_third_share"

    run "Manila NFS snapshot backend preflight" \
        validate_manila_backend \
        "$MIGRATION_MANILA_NFS_SHARE" NFS \
        "$MIGRATION_MANILA_NFS_SHARE_TYPE"
    run "Manila CephFS snapshot backend preflight" \
        validate_manila_backend \
        "$MIGRATION_MANILA_CEPHFS_SHARE" CEPHFS \
        "$MIGRATION_MANILA_CEPHFS_SHARE_TYPE"

    run "Manila NFS snapshot and restore through Nova public API" \
        env \
        IMAGE="$MIGRATION_LOCAL_IMAGE" \
        FLAVOR="$MIGRATION_FLAVOR" \
        NETWORK="$MIGRATION_NETWORK" \
        COMPUTE_HOST="${migration_node_names[0]}" \
        COMPUTE_SSH="${migration_node_ssh[0]}" \
        CONTROLLER_SSH="$CONTROLLER_SSH" \
        CONTROLLER_OPENRC="${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}" \
        SSH_IDENTITY="$SSH_IDENTITY" \
        SSH_KNOWN_HOSTS_FILE="$SSH_KNOWN_HOSTS_FILE" \
        SHARE_TYPE="$MIGRATION_MANILA_NFS_SHARE_TYPE" \
        SHARE_PROTOCOL=NFS \
        NAME="incus-release-manila-nfs-snapshot-$timestamp" \
        "$SCRIPT_DIR/openstack-incus-manila-snapshot-e2e.sh"
    run "Manila CephFS snapshot and restore through Nova public API" \
        env \
        IMAGE="$MIGRATION_LOCAL_IMAGE" \
        FLAVOR="$MIGRATION_FLAVOR" \
        NETWORK="$MIGRATION_NETWORK" \
        COMPUTE_HOST="${migration_node_names[0]}" \
        COMPUTE_SSH="${migration_node_ssh[0]}" \
        CONTROLLER_SSH="$CONTROLLER_SSH" \
        CONTROLLER_OPENRC="${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}" \
        SSH_IDENTITY="$SSH_IDENTITY" \
        SSH_KNOWN_HOSTS_FILE="$SSH_KNOWN_HOSTS_FILE" \
        SHARE_TYPE="$MIGRATION_MANILA_CEPHFS_SHARE_TYPE" \
        SHARE_PROTOCOL=CEPHFS \
        NAME="incus-release-manila-cephfs-snapshot-$timestamp" \
        "$SCRIPT_DIR/openstack-incus-manila-snapshot-e2e.sh"

    for ((node_index = 0;
          node_index < ${#migration_nodes[@]};
          node_index++)); do
        run "isolated idmap conflict: ${migration_node_names[node_index]}" \
            env \
            RUN_DESTRUCTIVE=true \
            IMAGE="$MIGRATION_LOCAL_IMAGE" \
            FLAVOR="$MIGRATION_FLAVOR" \
            NETWORK="$MIGRATION_NETWORK" \
            NOVA_HOST="${migration_node_names[node_index]}" \
            COMPUTE_SSH="${migration_node_ssh[node_index]}" \
            CONTROLLER_SSH="$CONTROLLER_SSH" \
            CONTROLLER_OPENRC="${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}" \
            SSH_IDENTITY="$SSH_IDENTITY" \
            SSH_KNOWN_HOSTS_FILE="$SSH_KNOWN_HOSTS_FILE" \
            NAME="incus-release-idmap-${migration_node_names[node_index]}" \
            bash "$SCRIPT_DIR/openstack-incus-idmap-conflict-e2e.sh"
    done

    # The BFV handover/fault protocol is pairwise. Exercise every direction
    # rather than treating one source/destination ordering as fleet evidence.
    for ((source_index = 0;
          source_index < ${#migration_nodes[@]};
          source_index++)); do
        for ((dest_index = 0;
              dest_index < ${#migration_nodes[@]};
              dest_index++)); do
            ((source_index != dest_index)) || continue
            pair="${migration_nodes[source_index]},"
            pair+="${migration_nodes[dest_index]}"
            pair_name="${migration_node_names[source_index]}-to-"
            pair_name+="${migration_node_names[dest_index]}"
            run "BFV migration fault matrix: $pair_name" \
                env \
                COMPUTE_NODES="$pair" \
                CONTROLLER_SSH="$CONTROLLER_SSH" \
                CONTROLLER_OPENRC="${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}" \
                SSH_IDENTITY="$SSH_IDENTITY" \
                SSH_KNOWN_HOSTS_FILE="$SSH_KNOWN_HOSTS_FILE" \
                IMAGE="$MIGRATION_BFV_IMAGE" \
                FLAVOR="$MIGRATION_FLAVOR" \
                NETWORK="$MIGRATION_NETWORK" \
                VOLUME_TYPE="$MIGRATION_ROOT_VOLUME_TYPE" \
                NAME_PREFIX="incus-release-bfv-$pair_name" \
                RUN_FLEET_PREFLIGHT=false \
                "$SCRIPT_DIR/openstack-incus-bfv-migration-matrix.sh"
        done
    done

    run "local/BFV, Cinder, and Manila 2x2x2 live-migration matrix" \
        env \
        SSH_IDENTITY="$SSH_IDENTITY" \
        SSH_KNOWN_HOSTS_FILE="$SSH_KNOWN_HOSTS_FILE" \
        NODE01_HOST="${migration_node_names[0]}" \
        NODE01_SSH="${migration_node_ssh[0]}" \
        NODE02_HOST="${migration_node_names[1]}" \
        NODE02_SSH="${migration_node_ssh[1]}" \
        NODE03_HOST="${migration_node_names[2]}" \
        NODE03_SSH="${migration_node_ssh[2]}" \
        CONTROLLER_SSH="$CONTROLLER_SSH" \
        CONTROLLER_OPENRC="${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}" \
        LOCAL_IMAGE="$MIGRATION_LOCAL_IMAGE" \
        BFV_IMAGE="$MIGRATION_BFV_IMAGE" \
        FLAVOR="$MIGRATION_FLAVOR" \
        NETWORK="$MIGRATION_NETWORK" \
        ROOT_VOLUME_TYPE="$MIGRATION_ROOT_VOLUME_TYPE" \
        DATA_VOLUME_TYPE="$MIGRATION_DATA_VOLUME_TYPE" \
        MANILA_SHARE="$MIGRATION_MANILA_NFS_SHARE" \
        "$SCRIPT_DIR/openstack-incus-live-migration-matrix.sh"

    run "local/BFV, Cinder, and Manila 2x3x3 cardinality matrix" \
        env \
        SSH_IDENTITY="$SSH_IDENTITY" \
        SSH_KNOWN_HOSTS_FILE="$SSH_KNOWN_HOSTS_FILE" \
        NODE01_HOST="${migration_node_names[0]}" \
        NODE01_SSH="${migration_node_ssh[0]}" \
        NODE02_HOST="${migration_node_names[1]}" \
        NODE02_SSH="${migration_node_ssh[1]}" \
        NODE03_HOST="${migration_node_names[2]}" \
        NODE03_SSH="${migration_node_ssh[2]}" \
        CONTROLLER_SSH="$CONTROLLER_SSH" \
        CONTROLLER_OPENRC="${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}" \
        LOCAL_IMAGE="$MIGRATION_LOCAL_IMAGE" \
        BFV_IMAGE="$MIGRATION_BFV_IMAGE" \
        FLAVOR="$MIGRATION_FLAVOR" \
        NETWORK="$MIGRATION_NETWORK" \
        ROOT_VOLUME_TYPE="$MIGRATION_ROOT_VOLUME_TYPE" \
        DATA_VOLUME_TYPE="$MIGRATION_DATA_VOLUME_TYPE" \
        MANILA_SHARES="$migration_three_shares" \
        "$SCRIPT_DIR/openstack-incus-live-migration-cardinality-matrix.sh"

    run "local/BFV, Cinder, and Manila 2x3x3 cold-migration matrix" \
        env \
        SSH_IDENTITY="$SSH_IDENTITY" \
        SSH_KNOWN_HOSTS_FILE="$SSH_KNOWN_HOSTS_FILE" \
        NODE01_HOST="${migration_node_names[0]}" \
        NODE01_SSH="${migration_node_ssh[0]}" \
        NODE02_HOST="${migration_node_names[1]}" \
        NODE02_SSH="${migration_node_ssh[1]}" \
        NODE03_HOST="${migration_node_names[2]}" \
        NODE03_SSH="${migration_node_ssh[2]}" \
        CONTROLLER_SSH="$CONTROLLER_SSH" \
        CONTROLLER_OPENRC="${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}" \
        LOCAL_IMAGE="$MIGRATION_LOCAL_IMAGE" \
        BFV_IMAGE="$MIGRATION_BFV_IMAGE" \
        FLAVOR="$MIGRATION_FLAVOR" \
        NETWORK="$MIGRATION_NETWORK" \
        ROOT_VOLUME_TYPE="$MIGRATION_ROOT_VOLUME_TYPE" \
        DATA_VOLUME_TYPE="$MIGRATION_DATA_VOLUME_TYPE" \
        MANILA_SHARES="$migration_three_shares" \
        bash "$SCRIPT_DIR/openstack-incus-cold-migration-cardinality-matrix.sh"

    migration_targets="${migration_node_names[1]}=${migration_node_ssh[1]},"
    migration_targets+="${migration_node_names[2]}=${migration_node_ssh[2]},"
    migration_targets+="${migration_node_names[0]}=${migration_node_ssh[0]}"
    run "maximum attachment live-migration restore rollback" \
        env \
        SSH_IDENTITY="$SSH_IDENTITY" \
        SSH_KNOWN_HOSTS_FILE="$SSH_KNOWN_HOSTS_FILE" \
        SOURCE_HOST="${migration_node_names[0]}" \
        SOURCE_SSH="${migration_node_ssh[0]}" \
        MIGRATION_TARGETS="$migration_targets" \
        CONTROLLER_SSH="$CONTROLLER_SSH" \
        CONTROLLER_OPENRC="${CONTROLLER_OPENRC:-/opt/stack/devstack/openrc admin admin}" \
        IMAGE="$MIGRATION_BFV_IMAGE" \
        FLAVOR="$MIGRATION_FLAVOR" \
        NETWORK="$MIGRATION_NETWORK" \
        ROOT_VOLUME_TYPE="$MIGRATION_ROOT_VOLUME_TYPE" \
        DATA_VOLUME_TYPE="$MIGRATION_DATA_VOLUME_TYPE" \
        BOOT_FROM_VOLUME=1 \
        WITH_DATA_VOLUME=1 \
        DATA_VOLUME_COUNT=3 \
        DATA_DEVICES= \
        MANILA_SHARES="$migration_three_shares" \
        MANILA_TAGS= \
        INJECT_RESTORE_FAILURE=1 \
        SERVER="incus-release-max-rollback-$timestamp" \
        "$SCRIPT_DIR/openstack-incus-live-migration-e2e.sh"
    run "post-matrix fleet preflight" \
        env \
        SSH_KNOWN_HOSTS_FILE="$SSH_KNOWN_HOSTS_FILE" \
        REQUIRE_MANILA_MIGRATION_RUNTIME=true \
        NOVA_API_NODES="$NOVA_API_NODES" \
        NOVA_API_RUNTIME_PYTHON="${NOVA_API_RUNTIME_PYTHON:-}" \
        NOVA_COMPUTE_RUNTIME_PYTHON="${NOVA_COMPUTE_RUNTIME_PYTHON:-}" \
        "$SCRIPT_DIR/openstack-incus-fleet-preflight.sh"
    PHASE_MIGRATION_MATRIX_PASSED=true
else
    echo "NO-GO: RUN_MIGRATION_MATRIX=false; no production release decision" >&2
fi

if [[ "$RUN_DESTRUCTIVE_FENCE" == true ]]; then
    : "${EVACUATION_E2E_ENV:?Set EVACUATION_E2E_ENV to a root-owned environment file}"
    [[ $(stat -c %u "$EVACUATION_E2E_ENV") == 0 &&
       $(stat -c %a "$EVACUATION_E2E_ENV") == 600 ]] || {
        echo "EVACUATION_E2E_ENV must be root-owned mode 0600" >&2
        exit 1
    }
    run "destructive external-fence evacuation" \
        bash -c '
            set -euo pipefail
            # shellcheck disable=SC1090
            source "$1"
            exec "$2"
        ' _ "$EVACUATION_E2E_ENV" \
        "$SCRIPT_DIR/openstack-incus-bfv-evacuation-e2e.sh"
    PHASE_DESTRUCTIVE_FENCE_PASSED=true
else
    echo "NO-GO: RUN_DESTRUCTIVE_FENCE=false; no production release decision" >&2
fi

if [[ "$PHASE_FLEET_PASSED" == true &&
      "$PHASE_TEMPEST_PASSED" == true &&
      "$PHASE_PUBLIC_API_E2E_PASSED" == true &&
      "$PHASE_SCALE_PASSED" == true &&
      "$PHASE_MIGRATION_MATRIX_PASSED" == true &&
      "$PHASE_DESTRUCTIVE_FENCE_PASSED" == true ]]; then
    echo "PASS production release gate"
else
    echo "PASS non-destructive checks; production decision remains NO-GO"
    echo "Evidence: $evidence"
    exit 3
fi

echo "Evidence: $evidence"
