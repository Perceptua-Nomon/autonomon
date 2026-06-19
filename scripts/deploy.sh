#!/usr/bin/env bash
# deploy.sh — Deploy the autonomon plugin to the Raspberry Pi over SSH.
#
# Usage:
#   ./scripts/deploy.sh [--local] [--skip-tests] [<version>] [<pi-host>]
#
# Arguments:
#   --local        Deploy the current local source tree (synced via rsync).
#                  Bypasses git fetch/checkout on the Pi. Version is read from
#                  pyproject.toml. Ignored if a version argument is also given.
#   --skip-tests   Skip the 'make test' step on the Pi. Useful for iterating
#                  quickly during development when tests have already passed.
#   version        Git tag to deploy (e.g. "v0.2.0"). If omitted, the script
#                  finds and deploys the latest semver tag. Ignored if --local.
#   pi-host        SSH host (user@host or plain hostname). Overrides
#                  NOMON_PI_HOST. If omitted and NOMON_PI_HOST is unset, runs
#                  locally — useful when already SSH'd into the Pi.
#
# Examples:
#   # Deploy local code from a dev machine to the Pi over SSH:
#   ./scripts/deploy.sh --local perceptua@perceptua
#
#   # Deploy latest release from a dev machine to the Pi over SSH:
#   ./scripts/deploy.sh perceptua@perceptua
#
#   # Deploy a specific version from a dev machine to the Pi over SSH:
#   ./scripts/deploy.sh v0.2.0 perceptua@perceptua
#
#   # Deploy local code directly on the Pi (no SSH needed):
#   ./scripts/deploy.sh --local
#
# Environment (read from .env.device in the repo root if present):
#   NOMON_PI_HOST          SSH target — "user@host" or plain hostname.
#   NOMON_SSH_KEY          Path to SSH private key (optional).
#   NOMON_SUDO_PASS        Optional sudo password for non-interactive sudo.
#   NOMON_REMOTE_DIR       Absolute path to the autonomon repo on the Pi.
#                          Defaults to ${HOME}/perceptua-nomon/autonomon.
#   NOMON_NOMOTHETIC_DIR   Absolute path to the nomothetic repo on the Pi.
#                          Defaults to ${HOME}/perceptua-nomon/nomothetic.
#                          The autonomon package is installed into nomothetic's
#                          .venv so that AutonomyPluginManager can discover it.
#
# What the script does (release mode):
#   1. Saves the current installed autonomon version for rollback.
#   2. Fetches tags from origin and checks out the target version.
#   3. Installs autonomon into nomothetic's .venv: .venv/bin/pip install .
#   4. Optionally runs tests (uv run pytest).
#   5. Verifies the nomon-autonomon CLI is importable and reports version.
#   6. Reloads nomothetic's AutonomyPluginManager if nomothetic-api.service
#      is running (via SIGHUP or service reload).
#
# What the script does (--local mode):
#   1. Reads the version from pyproject.toml.
#   2. Syncs the local source tree to the Pi via rsync.
#   3. Installs in editable mode: .venv/bin/pip install -e .
#   4–6. Same as release mode.
#
# Rollback:
#   If any step fails (release mode), the script checks out the previous git
#   ref and reinstalls it before exiting with code 2.
#
# Exit codes:
#   0  Deploy successful.
#   1  Usage / configuration error (no changes made on the Pi).
#   2  Deploy failed; rollback was performed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "${SCRIPT_DIR}")"

# ── Help ───────────────────────────────────────────────────────────────────────

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    sed -n '2,57p' "$0" | sed 's/^# \?//'
    exit 0
fi

# ── Load .env.device ──────────────────────────────────────────────────────────

ENV_FILE="${REPO_DIR}/.env.device"
if [[ -f "${ENV_FILE}" ]]; then
    while IFS= read -r line || [[ -n "${line}" ]]; do
        line="${line#"${line%%[![:space:]]*}"}"
        [[ "${line}" =~ ^# || -z "${line}" ]] && continue
        key="${line%%=*}"
        val="${line#*=}"
        val="${val%%#*}"
        val="${val#"${val%%[![:space:]]*}"}"
        val="${val%"${val##*[![:space:]]}"}"
        val="${val#\"}" ; val="${val%\"}"
        val="${val#\'}" ; val="${val%\'}"
        case "${key}" in
            NOMON_PI_HOST|NOMON_SSH_KEY|NOMON_REMOTE_DIR|NOMON_NOMOTHETIC_DIR|NOMON_SUDO_PASS)
                export "${key}=${val}" ;;
        esac
    done < "${ENV_FILE}"
fi

NOMON_SUDO_PASS="$(printf '%s' "${NOMON_SUDO_PASS:-}" | tr -d '\r\n')"
_NOMON_SUDO_PASS_QUOTED="$(printf '%q' "${NOMON_SUDO_PASS}")"

# ── Argument parsing ───────────────────────────────────────────────────────────

DEPLOY_LOCAL=false
SKIP_TESTS=false
_positional_args=()

for _arg in "$@"; do
    case "${_arg}" in
        --local)       DEPLOY_LOCAL=true ;;
        --skip-tests)  SKIP_TESTS=true ;;
        *)             _positional_args+=("${_arg}") ;;
    esac
done

VERSION="${_positional_args[0]:-}"
PI_HOST="${_positional_args[1]:-${NOMON_PI_HOST:-}}"

if [[ -n "${VERSION}" && ! "${VERSION}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Error: version must start with 'v' followed by semver (e.g. v0.2.0)" >&2
    exit 1
fi

# ── Local mode: resolve version from pyproject.toml ───────────────────────────

if [[ "${DEPLOY_LOCAL}" == true ]]; then
    _raw_version="$(grep -m1 '^version' "${REPO_DIR}/pyproject.toml" \
        | sed -E 's/.*version\s*=\s*"([^"]+)".*/\1/')"
    if [[ -z "${_raw_version}" ]]; then
        echo "Error: could not determine version from pyproject.toml" >&2
        exit 1
    fi
    VERSION="v${_raw_version}"
    echo "==> Local deploy: autonomon ${VERSION}"
fi

# ── SSH helpers ────────────────────────────────────────────────────────────────

if [[ -n "${PI_HOST}" ]]; then
    SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
    if [[ -n "${NOMON_SSH_KEY:-}" ]]; then
        SSH_OPTS+=(-i "${NOMON_SSH_KEY}")
    fi
    echo "==> Deploying autonomon${VERSION:+ ${VERSION}} → ${PI_HOST}"
    RUN_CMD=(ssh "${SSH_OPTS[@]}" "${PI_HOST}" "NOMON_SKIP_TESTS=${SKIP_TESTS} NOMON_SUDO_PASS=${_NOMON_SUDO_PASS_QUOTED} bash -ls \"\$@\"" --)
else
    echo "==> Deploying autonomon${VERSION:+ ${VERSION}} locally"
    export NOMON_SKIP_TESTS="${SKIP_TESTS}"
    RUN_CMD=(bash -ls --)
fi

# ── Local mode: rsync source tree to Pi ───────────────────────────────────────

if [[ "${DEPLOY_LOCAL}" == true && -n "${PI_HOST}" ]]; then
    _remote_dir="${NOMON_REMOTE_DIR:-}"
    _rsync_dest="${PI_HOST}:${_remote_dir:-~/perceptua-nomon/autonomon/}"
    RSYNC_OPTS=(--archive --compress --delete
        --exclude='.git/'
        --exclude='__pycache__/'
        --exclude='*.pyc'
        --exclude='.venv/'
        --exclude='*.egg-info/'
        --exclude='htmlcov/'
    )
    if [[ -n "${NOMON_SSH_KEY:-}" ]]; then
        RSYNC_OPTS+=(-e "ssh -i ${NOMON_SSH_KEY} -o StrictHostKeyChecking=accept-new")
    else
        RSYNC_OPTS+=(-e "ssh -o StrictHostKeyChecking=accept-new")
    fi
    _remote_dir_for_ssh="${_remote_dir:-~/perceptua-nomon/autonomon}"
    echo "==> Ensuring remote deploy directory exists: ${_remote_dir_for_ssh}"
    ssh "${SSH_OPTS[@]}" "${PI_HOST}" "bash -s" -- "${_remote_dir_for_ssh}" <<'EO_MKREMOTE'
set -euo pipefail
_dest="$1"
[[ "${_dest}" == ~/* ]] && _dest="${HOME}/${_dest#~/}"
mkdir -p "${_dest}"
EO_MKREMOTE
    echo "==> Syncing local source → ${_rsync_dest}..."
    rsync "${RSYNC_OPTS[@]}" "${REPO_DIR}/" "${_rsync_dest}"
    echo "  Sync complete ✓"
fi

# ── Remote deploy script ───────────────────────────────────────────────────────
# All steps below run on the Pi (remote or local) via a single shell session.

"${RUN_CMD[@]}" "${VERSION}" "${DEPLOY_LOCAL}" \
    "${NOMON_REMOTE_DIR:-}" "${NOMON_NOMOTHETIC_DIR:-}" << 'END_REMOTE'
set -euo pipefail

if [[ -n "${NOMON_SUDO_PASS:-}" ]]; then
    _askpass_script="$(mktemp)"
    chmod 700 "${_askpass_script}"
    printf '#!/usr/bin/env sh\nprintf "%%s\n" "%s"\n' "${NOMON_SUDO_PASS}" > "${_askpass_script}"
    export SUDO_ASKPASS="${_askpass_script}"
    trap 'rm -f "${_askpass_script}"' EXIT
    sudo() { command sudo -A "$@"; }
else
    sudo() { command sudo "$@"; }
fi

readonly REQUESTED_VERSION="$1"
readonly DEPLOY_LOCAL="${2:-false}"
readonly REMOTE_DIR="${3:-${HOME}/perceptua-nomon/autonomon}"
readonly NOMOTHETIC_DIR="${4:-${HOME}/perceptua-nomon/nomothetic}"
readonly SKIP_TESTS="${NOMON_SKIP_TESTS:-false}"
readonly PKG_DIR="${REMOTE_DIR}/autonomon"
readonly NOMOTHETIC_VENV="${NOMOTHETIC_DIR}/.venv"

if [[ ! -d "${REMOTE_DIR}" ]]; then
    echo "Error: ${REMOTE_DIR} does not exist on the Pi." >&2
    exit 1
fi

if [[ ! -d "${NOMOTHETIC_VENV}" ]]; then
    echo "Error: nomothetic venv not found at ${NOMOTHETIC_VENV}." >&2
    echo "  Ensure nomothetic is deployed first." >&2
    exit 1
fi

cd "${REMOTE_DIR}"

# ── Save current installed version for rollback ────────────────────────────────

PREV_VERSION="$("${NOMOTHETIC_VENV}/bin/pip" show autonomon 2>/dev/null \
    | grep '^Version:' | awk '{print $2}' || echo "")"
if [[ -n "${PREV_VERSION}" ]]; then
    echo "  Current installed autonomon: ${PREV_VERSION}"
else
    echo "  autonomon not currently installed."
fi

# ── Save current git ref for rollback (release mode only) ─────────────────────

if [[ "${DEPLOY_LOCAL}" != "true" ]]; then
    PREV_REF="$(git rev-parse HEAD)"
    PREV_LABEL="$(git describe --tags --exact-match HEAD 2>/dev/null \
                  || git rev-parse --short HEAD)"
    echo "  Current git ref: ${PREV_LABEL}"
fi

# ── Resolve target version (pre-flight) ───────────────────────────────────────

if [[ "${DEPLOY_LOCAL}" == "true" ]]; then
    TARGET="${REQUESTED_VERSION}"
    echo "==> Target: ${TARGET} (local source)"
else
    echo "==> Fetching tags from origin..."
    git fetch --tags --quiet

    TARGET="${REQUESTED_VERSION}"
    if [[ -z "${TARGET}" ]]; then
        TARGET="$(git tag --list 'v*' --sort=-version:refname | head -1)"
        if [[ -z "${TARGET}" ]]; then
            echo "Error: no semver tags found in the repository." >&2
            exit 1
        fi
        echo "  Latest release tag: ${TARGET}"
    fi

    if [[ ! "${TARGET}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "Error: resolved tag '${TARGET}' is not a valid semver tag." >&2
        exit 1
    fi

    CURRENT_TAG="$(git describe --tags --exact-match HEAD 2>/dev/null || true)"
    if [[ "${CURRENT_TAG}" == "${TARGET}" ]]; then
        echo "  Note: already on ${TARGET}; re-running install and verification."
    fi

    echo "==> Target: ${TARGET}"
fi

# ── Rollback helper ────────────────────────────────────────────────────────────

_ROLLING_BACK=0

rollback() {
    [[ "${_ROLLING_BACK}" -eq 1 ]] && exit 2
    _ROLLING_BACK=1

    echo "" >&2
    echo "!! Deployment failed. Rolling back..." >&2

    if [[ "${DEPLOY_LOCAL}" != "true" && -n "${PREV_REF:-}" ]]; then
        git checkout --quiet "${PREV_REF}" || true
    fi

    if [[ -n "${PREV_VERSION:-}" ]]; then
        echo "  Reinstalling autonomon ${PREV_VERSION}..." >&2
        "${NOMOTHETIC_VENV}/bin/pip" install --quiet "${PKG_DIR}" 2>&1 || true
    elif [[ -n "${PREV_VERSION+x}" ]]; then
        echo "  autonomon was not installed before; removing..." >&2
        "${NOMOTHETIC_VENV}/bin/pip" uninstall -y autonomon 2>&1 || true
    fi

    echo "!! Rollback complete." >&2
    exit 2
}

trap rollback ERR

# ── Checkout target version (release mode only) ────────────────────────────────

if [[ "${DEPLOY_LOCAL}" != "true" ]]; then
    echo "==> Checking out ${TARGET}..."
    git checkout --quiet "${TARGET}"
fi

# ── Install into nomothetic's venv ────────────────────────────────────────────

echo "==> Installing autonomon into nomothetic venv (${NOMOTHETIC_VENV})..."
if [[ "${DEPLOY_LOCAL}" == "true" ]]; then
    "${NOMOTHETIC_VENV}/bin/pip" install --quiet -e "${PKG_DIR}"
else
    "${NOMOTHETIC_VENV}/bin/pip" install --quiet "${PKG_DIR}"
fi
echo "  Install complete ✓"

# ── Optional tests ─────────────────────────────────────────────────────────────

if [[ "${SKIP_TESTS}" == "true" ]]; then
    echo "==> Skipping tests (--skip-tests flag set)."
else
    echo "==> Running tests..."
    if command -v uv >/dev/null 2>&1 && [[ -f "${PKG_DIR}/uv.lock" ]]; then
        (cd "${PKG_DIR}" && uv sync --all-extras --quiet && uv run pytest tests/ -q)
    else
        "${NOMOTHETIC_VENV}/bin/pip" install --quiet pytest pytest-asyncio
        "${NOMOTHETIC_VENV}/bin/pytest" "${PKG_DIR}/tests/" -q
    fi
    echo "  Tests passed ✓"
fi

# ── Verify installation ────────────────────────────────────────────────────────

echo "==> Verifying installation..."
_installed_version="$("${NOMOTHETIC_VENV}/bin/pip" show autonomon \
    | grep '^Version:' | awk '{print $2}')"
echo "  autonomon ${_installed_version} installed ✓"

_cli_path="$(find "${NOMOTHETIC_VENV}/bin" -name "nomon-autonomon" 2>/dev/null | head -1)"
if [[ -z "${_cli_path}" ]]; then
    echo "Error: nomon-autonomon CLI not found in ${NOMOTHETIC_VENV}/bin" >&2
    exit 1
fi
echo "  CLI: ${_cli_path} ✓"

_manifest="$("${NOMOTHETIC_VENV}/bin/python" -c \
    'from autonomon.routines import nomon_manifest; print(nomon_manifest["name"], nomon_manifest["routines"])')"
echo "  Manifest: ${_manifest} ✓"

# ── Plugin auth: key + env file (ADR-019) ─────────────────────────────────────
# Generate an Ed25519 private key on-device (never leaves the Pi) and write the
# plugin env file pointing at it. The public half is registered with nomothetic
# *after* it is confirmed up (below). The JWT itself is never written to disk —
# the CLI acquires a fresh one at runtime via challenge-response.

PLUGIN_KEY_PATH="/etc/autonomon/plugin.key"
PLUGIN_ENV_FILE="/etc/autonomon/autonomon.env"
LOCAL_API_URL="https://127.0.0.1:8443"
PLUGIN_NAME="autonomon"

# Reuse nomothetic's service user so the key is owned by the account that runs
# the plugin subprocess; default to 'nomon' if not configured.
SERVICE_USER="nomon"
if [[ -f /etc/nomothetic/nomothetic.env ]]; then
    _su="$(grep -E '^\s*NOMON_SERVICE_USER\s*=' /etc/nomothetic/nomothetic.env \
           | tail -1 | cut -d= -f2- | tr -d ' "'"'"'')"
    [[ -n "${_su}" ]] && SERVICE_USER="${_su}"
fi

echo "==> Generating plugin key (idempotent) at ${PLUGIN_KEY_PATH}..."
sudo mkdir -p "$(dirname "${PLUGIN_KEY_PATH}")"
# Generate as the service user so the private key is owned by it from the start.
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    SERVICE_USER="$(whoami)"
fi
PLUGIN_PUBKEY="$(sudo -u "${SERVICE_USER}" "${NOMOTHETIC_VENV}/bin/python" \
    -m autonomon.plugin_auth generate-key "${PLUGIN_KEY_PATH}")"
echo "  Key ready (owner: ${SERVICE_USER}) ✓"

DEVICE_HOSTNAME="$(hostname)"
echo "==> Writing plugin env file ${PLUGIN_ENV_FILE}..."
_tmp_env="$(mktemp)"
cat > "${_tmp_env}" <<EOF_PLUGIN_ENV
# autonomon plugin environment (written by deploy.sh). No secret token here:
# the device JWT is acquired at runtime via Ed25519 challenge-response (ADR-019).
NOMON_DEVICE_URL=${LOCAL_API_URL}
NOMON_PLUGIN_KEY=${PLUGIN_KEY_PATH}
NOMON_PLUGIN_NAME=${PLUGIN_NAME}
NOMON_DEVICE_ID=${DEVICE_HOSTNAME}
EOF_PLUGIN_ENV
sudo mv -f "${_tmp_env}" "${PLUGIN_ENV_FILE}"
sudo chmod 644 "${PLUGIN_ENV_FILE}"
echo "  Env file written ✓"

# ── Reload nomothetic so plugin auth endpoints + discovery are fresh ──────────

_NOMOTHETIC_RUNNING=false
if command -v systemctl >/dev/null 2>&1; then
    if sudo systemctl is-active --quiet nomothetic-api.service 2>/dev/null; then
        echo "==> Reloading nomothetic-api.service to pick up plugin changes..."
        sudo systemctl reload-or-restart nomothetic-api.service
        _NOMOTHETIC_RUNNING=true
        echo "  nomothetic-api reloaded ✓"
    fi
fi

# ── Register the public key with nomothetic (localhost only) ──────────────────
# Registration is idempotent: re-running deploy with the same key is a no-op.

echo "==> Registering plugin public key with nomothetic (${LOCAL_API_URL})..."
# Run as the key's owner: the private key is 0600 owned by SERVICE_USER, and the
# register step reads it to derive the public half. Running as the deploy user
# would hit a permission error whenever deploy user != SERVICE_USER.
_register() {
    sudo -u "${SERVICE_USER}" "${NOMOTHETIC_VENV}/bin/python" -m autonomon.plugin_auth register \
        --device-url "${LOCAL_API_URL}" --plugin "${PLUGIN_NAME}" --key "${PLUGIN_KEY_PATH}"
}
# nomothetic may take a moment to come back after a reload; retry briefly.
_registered=false
for _attempt in 1 2 3 4 5 6; do
    if _register; then
        _registered=true
        break
    fi
    sleep 2
done
if [[ "${_registered}" == "true" ]]; then
    echo "  Public key registered ✓"
else
    echo "  WARNING: could not register the public key automatically." >&2
    echo "  nomothetic may not be running locally. Register manually with:" >&2
    echo "    ${NOMOTHETIC_VENV}/bin/python -m autonomon.plugin_auth register \\" >&2
    echo "      --device-url ${LOCAL_API_URL} --plugin ${PLUGIN_NAME} --key ${PLUGIN_KEY_PATH}" >&2
fi

echo ""
echo "✓ autonomon ${TARGET} deployed successfully to ${DEVICE_HOSTNAME}."
echo "  Plugin env: ${PLUGIN_ENV_FILE}"
echo "  Run a routine with:"
echo "    set -a; . ${PLUGIN_ENV_FILE}; set +a; \\"
echo "    NOMON_PLUGIN_PARAMS='{\"routine\":\"explore\"}' ${NOMOTHETIC_VENV}/bin/nomon-autonomon"
END_REMOTE
