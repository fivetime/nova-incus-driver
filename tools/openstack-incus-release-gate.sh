#!/usr/bin/env bash
# Aggregate fail-closed release gate for a Nova Incus production candidate.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
RUN_FLEET=${RUN_FLEET:-false}
RUN_MIGRATION_MATRIX=${RUN_MIGRATION_MATRIX:-false}
RUN_DESTRUCTIVE_FENCE=${RUN_DESTRUCTIVE_FENCE:-false}
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

for setting in RUN_FLEET RUN_MIGRATION_MATRIX RUN_DESTRUCTIVE_FENCE; do
    require_bool "$setting"
done

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
    literal_credential_pattern="(password|secret|token)[[:space:]]*=[[:space:]]*[$single_quote$double_quote][^\$<{$single_quote$double_quote]"
    if git grep -n -I -E \
        "$literal_credential_pattern" \
        -- . \
        ":(exclude)nova/tests/**" \
        ":(exclude)nova_lxd_tempest_plugin/tests/**" \
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

if [[ "$RUN_FLEET" == true ]]; then
    run "fleet preflight" "$SCRIPT_DIR/openstack-incus-fleet-preflight.sh"
else
    echo "NO-GO: RUN_FLEET=false; no production release decision" >&2
fi

if [[ "$RUN_MIGRATION_MATRIX" == true ]]; then
    run "BFV migration fault matrix" \
        "$SCRIPT_DIR/openstack-incus-bfv-migration-matrix.sh"
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
    # shellcheck disable=SC1090
    source "$EVACUATION_E2E_ENV"
    run "destructive external-fence evacuation" \
        "$SCRIPT_DIR/openstack-incus-bfv-evacuation-e2e.sh"
else
    echo "NO-GO: RUN_DESTRUCTIVE_FENCE=false; no production release decision" >&2
fi

if [[ "$RUN_FLEET" == true &&
      "$RUN_MIGRATION_MATRIX" == true &&
      "$RUN_DESTRUCTIVE_FENCE" == true ]]; then
    echo "PASS production release gate"
else
    echo "PASS non-destructive checks; production decision remains NO-GO"
fi

echo "Evidence: $evidence"
