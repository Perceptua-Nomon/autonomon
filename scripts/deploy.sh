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
#   NOMON_ROUTINE_CATALOG_PATH
#                          Absolute path of the routine catalogue this script
#                          publishes for nomothetic to read. Default (shared with
#                          nomothetic/.env.device): /var/lib/nomon/routine_catalog.json.
#   NOMON_VISION_DETECTOR  follow-user detector: yolo-onnx (default) | opencv-dnn |
#                          opencv-hog | fake. Written to /etc/autonomon/autonomon.env;
#                          the CLI loads it. opencv-dnn auto-fetches a small ~23MB
#                          MobileNet-SSD; opencv-hog needs no model at all.
#   NOMON_VISION_MODEL_PATH
#                          Detector weights: yolov8n.onnx (yolo-onnx) or the
#                          MobileNetSSD .caffemodel (opencv-dnn). Auto-fetched to
#                          /var/lib/nomon/models when unset.
#   NOMON_VISION_MODEL_CONFIG
#                          MobileNet-SSD .prototxt (opencv-dnn only). Auto-fetched
#                          alongside the model when unset.
#
# What the script does (release mode):
#   1. Saves the current installed autonomon version for rollback.
#   2. Fetches tags from origin and checks out the target version.
#   3. Creates a fresh venv and installs autonomon with uv sync.
#   4. Optionally runs tests (uv run pytest).
#   5. Verifies the nomon-autonomon CLI is installed and importable.
#   6. Publishes the routine catalogue (NOMON_ROUTINE_CATALOG_PATH) for nomothetic.
#   7. Registers the plugin key with nomothetic and reloads its services.
#
# What the script does (--local mode):
#   1. Reads the version from pyproject.toml.
#   2. Syncs the local source tree to the Pi via rsync.
#   3. Creates a fresh venv and installs autonomon in editable mode.
#   4–7. Same as release mode.
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
    sed -n '2,58p' "$0" | sed 's/^# \?//'
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
            NOMON_PI_HOST|NOMON_SSH_KEY|NOMON_REMOTE_DIR|NOMON_ROUTINE_CATALOG_PATH|NOMON_SUDO_PASS|NOMON_VISION_DETECTOR|NOMON_VISION_MODEL_PATH|NOMON_VISION_MODEL_CONFIG)
                export "${key}=${val}" ;;
        esac
    done < "${ENV_FILE}"
fi

NOMON_SUDO_PASS="$(printf '%s' "${NOMON_SUDO_PASS:-}" | tr -d '\r\n')"
_NOMON_SUDO_PASS_QUOTED="$(printf '%q' "${NOMON_SUDO_PASS}")"
# Pass the catalogue path to the remote as an env var, not a positional arg: over
# SSH, empty positionals (e.g. an unset VERSION) collapse and shift later args, so
# an env var is the robust channel (same approach as NOMON_SKIP_TESTS below).
_NOMON_ROUTINE_CATALOG_PATH_QUOTED="$(printf '%q' "${NOMON_ROUTINE_CATALOG_PATH:-}")"
# Vision config (autonomon-owned; written to /etc/autonomon/autonomon.env on the Pi).
_NOMON_VISION_DETECTOR_QUOTED="$(printf '%q' "${NOMON_VISION_DETECTOR:-}")"
_NOMON_VISION_MODEL_PATH_QUOTED="$(printf '%q' "${NOMON_VISION_MODEL_PATH:-}")"
_NOMON_VISION_MODEL_CONFIG_QUOTED="$(printf '%q' "${NOMON_VISION_MODEL_CONFIG:-}")"

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

# Embed VERSION, DEPLOY_LOCAL, REMOTE_DIR as env vars (not positional args) so
# SSH can't silently drop empty strings and shift subsequent args.
_VERSION_QUOTED="$(printf '%q' "${VERSION}")"
_DEPLOY_LOCAL_QUOTED="$(printf '%q' "${DEPLOY_LOCAL}")"
_REMOTE_DIR_QUOTED="$(printf '%q' "${NOMON_REMOTE_DIR:-}")"

if [[ -n "${PI_HOST}" ]]; then
    SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
    if [[ -n "${NOMON_SSH_KEY:-}" ]]; then
        SSH_OPTS+=(-i "${NOMON_SSH_KEY}")
    fi
    echo "==> Deploying autonomon${VERSION:+ ${VERSION}} → ${PI_HOST}"
    RUN_CMD=(ssh "${SSH_OPTS[@]}" "${PI_HOST}" "NOMON_SKIP_TESTS=${SKIP_TESTS} NOMON_SUDO_PASS=${_NOMON_SUDO_PASS_QUOTED} NOMON_ROUTINE_CATALOG_PATH=${_NOMON_ROUTINE_CATALOG_PATH_QUOTED} NOMON_VISION_DETECTOR=${_NOMON_VISION_DETECTOR_QUOTED} NOMON_VISION_MODEL_PATH=${_NOMON_VISION_MODEL_PATH_QUOTED} NOMON_VISION_MODEL_CONFIG=${_NOMON_VISION_MODEL_CONFIG_QUOTED} NOMON_DEPLOY_VERSION=${_VERSION_QUOTED} NOMON_DEPLOY_LOCAL=${_DEPLOY_LOCAL_QUOTED} NOMON_DEPLOY_REMOTE_DIR=${_REMOTE_DIR_QUOTED} bash -ls")
else
    echo "==> Deploying autonomon${VERSION:+ ${VERSION}} locally"
    export NOMON_SKIP_TESTS="${SKIP_TESTS}"
    export NOMON_ROUTINE_CATALOG_PATH="${NOMON_ROUTINE_CATALOG_PATH:-}"
    export NOMON_VISION_DETECTOR="${NOMON_VISION_DETECTOR:-}"
    export NOMON_VISION_MODEL_PATH="${NOMON_VISION_MODEL_PATH:-}"
    export NOMON_VISION_MODEL_CONFIG="${NOMON_VISION_MODEL_CONFIG:-}"
    export NOMON_DEPLOY_VERSION="${VERSION}"
    export NOMON_DEPLOY_LOCAL="${DEPLOY_LOCAL}"
    export NOMON_DEPLOY_REMOTE_DIR="${NOMON_REMOTE_DIR:-}"
    RUN_CMD=(bash -ls)
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

"${RUN_CMD[@]}" << 'END_REMOTE'
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

readonly REQUESTED_VERSION="${NOMON_DEPLOY_VERSION:-}"
readonly DEPLOY_LOCAL="${NOMON_DEPLOY_LOCAL:-false}"
readonly REMOTE_DIR="${NOMON_DEPLOY_REMOTE_DIR:-${HOME}/perceptua-nomon/autonomon}"
readonly CATALOG_PATH="${NOMON_ROUTINE_CATALOG_PATH:-/var/lib/nomon/routine_catalog.json}"
readonly SKIP_TESTS="${NOMON_SKIP_TESTS:-false}"

if ! command -v uv >/dev/null 2>&1; then
    echo "Error: uv not found in PATH. Install uv: https://docs.astral.sh/uv/getting-started/" >&2
    exit 1
fi

# ── Save current installed version for rollback ────────────────────────────────

if [[ -f "${REMOTE_DIR}/.venv/bin/python" ]]; then
    PREV_VERSION="$("${REMOTE_DIR}/.venv/bin/python" -c 'import autonomon; print(autonomon.__version__)' 2>/dev/null || echo "")"
    if [[ -n "${PREV_VERSION}" ]]; then
        echo "  Current installed autonomon: ${PREV_VERSION}"
    else
        echo "  autonomon not currently installed."
    fi
else
    echo "  No previous venv found."
fi

# ── Resolve target version (pre-flight) ───────────────────────────────────────

if [[ "${DEPLOY_LOCAL}" == "true" ]]; then
    if [[ ! -d "${REMOTE_DIR}" ]]; then
        echo "Error: ${REMOTE_DIR} does not exist on the Pi." >&2
        exit 1
    fi
    TARGET="${REQUESTED_VERSION}"
    echo "==> Target: ${TARGET} (local source)"
else
    echo "==> Fresh clone from origin..."
    _github_repo="https://github.com/perceptua-nomon/autonomon"
    _tmp_clone="$(mktemp -d)"
    git clone --quiet "${_github_repo}" "${_tmp_clone}/autonomon"

    # Backup existing repo and move fresh clone into place
    if [[ -d "${REMOTE_DIR}" ]]; then
        mv "${REMOTE_DIR}" "${REMOTE_DIR}.backup.$$"
    fi
    mv "${_tmp_clone}/autonomon" "${REMOTE_DIR}"
    rm -rf "${_tmp_clone}"
    echo "  Clone complete ✓"

    TARGET="${REQUESTED_VERSION}"
    if [[ -z "${TARGET}" ]]; then
        TARGET="$(git -C "${REMOTE_DIR}" tag --list 'v*' --sort=-version:refname | head -1)"
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

    echo "==> Target: ${TARGET}"
fi

cd "${REMOTE_DIR}"

# ── Rollback helper ────────────────────────────────────────────────────────────

_ROLLING_BACK=0

rollback() {
    [[ "${_ROLLING_BACK}" -eq 1 ]] && exit 2
    _ROLLING_BACK=1

    echo "" >&2
    echo "!! Deployment failed. Rolling back..." >&2

    # In release mode, restore from backup; in local mode, just reinstall.
    if [[ "${DEPLOY_LOCAL}" != "true" ]]; then
        # Find and restore the backup directory if it exists
        for _backup in "${REMOTE_DIR}".backup.*; do
            if [[ -d "${_backup}" ]]; then
                rm -rf "${REMOTE_DIR}"
                mv "${_backup}" "${REMOTE_DIR}"
                echo "  Restored from backup: ${_backup}" >&2
                break
            fi
        done
    fi

    if [[ -n "${PREV_VERSION:-}" ]]; then
        echo "  Recreating venv and reinstalling autonomon ${PREV_VERSION}..." >&2
        (cd "${REMOTE_DIR}" && rm -rf .venv && uv venv --system-site-packages --quiet && uv sync --quiet) 2>&1 || true
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

# ── Set up venv and install dependencies ────────────────────────────────────────

echo "==> Setting up venv..."
(cd "${REMOTE_DIR}" && rm -rf .venv && uv venv --system-site-packages --quiet)
echo "  Venv created ✓"

echo "==> Installing autonomon and dependencies..."
(cd "${REMOTE_DIR}" && uv sync --quiet)
echo "  Install complete ✓"

# ── Optional tests ─────────────────────────────────────────────────────────────

if [[ "${SKIP_TESTS}" == "true" ]]; then
    echo "==> Skipping tests (--skip-tests flag set)."
else
    echo "==> Running tests..."
    (cd "${REMOTE_DIR}" && uv sync --all-extras --quiet && uv run pytest tests/ -q)
    echo "  Tests passed ✓"
fi

# ── Verify installation ────────────────────────────────────────────────────────

echo "==> Verifying installation..."
_installed_version="$("${REMOTE_DIR}/.venv/bin/python" -c 'import autonomon; print(autonomon.__version__)' 2>/dev/null || echo 'unknown')"
echo "  autonomon ${_installed_version} installed ✓"

_cli_path="${REMOTE_DIR}/.venv/bin/nomon-autonomon"
if [[ ! -f "${_cli_path}" ]]; then
    echo "Error: nomon-autonomon CLI not found at ${_cli_path}" >&2
    exit 1
fi
echo "  CLI: ${_cli_path} ✓"

_manifest="$("${REMOTE_DIR}/.venv/bin/python" -c \
    'from autonomon.routines import nomon_manifest; print(nomon_manifest["name"], nomon_manifest["routines"])')"
echo "  Manifest: ${_manifest} ✓"

# ── Publish the routine catalogue for nomothetic ──────────────────────────────
# Decoupling (ADR-005): nomothetic and autonomon run from separate venvs and never
# import each other. autonomon publishes its catalogue — routine names, parameter
# schemas, version, and the absolute path to *this* venv's nomon-autonomon CLI —
# to a shared file that nomothetic reads to both list routines
# (GET /api/routines/available) and launch them. Written under sudo because the
# default location (/var/lib/nomon) is root-owned; made world-readable so the
# nomothetic service user can read it.

echo "==> Publishing routine catalogue to ${CATALOG_PATH}..."
sudo mkdir -p "$(dirname "${CATALOG_PATH}")"
sudo "${REMOTE_DIR}/.venv/bin/python" -m autonomon.routines.publish "${CATALOG_PATH}"
sudo chmod 644 "${CATALOG_PATH}"
_published="$("${REMOTE_DIR}/.venv/bin/python" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["routines"])' "${CATALOG_PATH}")"
echo "  Catalogue published (routines: ${_published}) ✓"

# ── Vision detector config (follow-user routine) ─────────────────────────────
# Autonomon owns its vision config end-to-end (ADR-004/005): the chosen detector
# and any model path are written into THIS project's env file
# (/etc/autonomon/autonomon.env, below) and the nomon-autonomon CLI loads them at
# startup. nomothetic carries none of this. Configure via .env.device:
#   NOMON_VISION_DETECTOR    = yolo-onnx (default) | opencv-hog | fake
#   NOMON_VISION_MODEL_PATH  = path to yolov8n.onnx (yolo-onnx only; auto-fetched
#                              to /var/lib/nomon/models when unset)

VISION_DETECTOR="${NOMON_VISION_DETECTOR:-yolo-onnx}"
VISION_MODEL_PATH="${NOMON_VISION_MODEL_PATH:-}"
VISION_MODEL_CONFIG="${NOMON_VISION_MODEL_CONFIG:-}"
echo "==> Vision detector: ${VISION_DETECTOR}"

case "${VISION_DETECTOR}" in
    opencv-dnn)
        # MobileNet-SSD via cv2.dnn (in opencv-python-headless) + a small model.
        echo "==> Installing OpenCV vision extra (cv2.dnn)..."
        (cd "${REMOTE_DIR}" && uv sync --extra vision-opencv --quiet)
        echo "  vision-opencv installed ✓"
        if [[ -z "${VISION_MODEL_PATH}" || -z "${VISION_MODEL_CONFIG}" ]]; then
            VISION_MODEL_DIR="/var/lib/nomon/models"
            _proto="${VISION_MODEL_DIR}/MobileNetSSD_deploy.prototxt"
            _caffe="${VISION_MODEL_DIR}/MobileNetSSD_deploy.caffemodel"
            if [[ ! -f "${_proto}" || ! -f "${_caffe}" ]]; then
                echo "==> Fetching MobileNet-SSD model..."
                sudo mkdir -p "${VISION_MODEL_DIR}"
                # Fetch into a dir we own, then move into the root-owned location.
                _model_tmp="$(mktemp -d)"
                MODEL_DIR="${_model_tmp}" \
                    PROTO_PATH="${_model_tmp}/MobileNetSSD_deploy.prototxt" \
                    MODEL_PATH="${_model_tmp}/MobileNetSSD_deploy.caffemodel" \
                    bash "${REMOTE_DIR}/scripts/fetch_mobilenet_ssd.sh"
                sudo mv -f "${_model_tmp}/MobileNetSSD_deploy.prototxt" "${_proto}"
                sudo mv -f "${_model_tmp}/MobileNetSSD_deploy.caffemodel" "${_caffe}"
                rm -rf "${_model_tmp}"
                sudo chmod 644 "${_proto}" "${_caffe}"
                echo "  Model ready: ${_caffe} ✓"
            else
                echo "==> MobileNet-SSD already present: ${_caffe} ✓"
            fi
            VISION_MODEL_PATH="${_caffe}"
            VISION_MODEL_CONFIG="${_proto}"
        fi
        ;;
    opencv-hog)
        # No model download — the SVM ships inside OpenCV. Just the light extra.
        echo "==> Installing OpenCV vision extra (no model download needed)..."
        (cd "${REMOTE_DIR}" && uv sync --extra vision-opencv --quiet)
        echo "  vision-opencv installed ✓"
        ;;
    yolo-onnx)
        echo "==> Installing ONNX vision extra..."
        (cd "${REMOTE_DIR}" && uv sync --extra vision --quiet)
        echo "  vision installed ✓"
        if [[ -z "${VISION_MODEL_PATH}" ]]; then
            VISION_MODEL_DIR="/var/lib/nomon/models"
            VISION_MODEL_PATH="${VISION_MODEL_DIR}/yolov8n.onnx"
            if [[ ! -f "${VISION_MODEL_PATH}" ]]; then
                echo "==> Fetching vision model (yolov8n.onnx)..."
                sudo mkdir -p "${VISION_MODEL_DIR}"
                # Fetch into a dir we own, then move into the root-owned location.
                _model_tmp="$(mktemp -d)"
                MODEL_DIR="${_model_tmp}" MODEL_PATH="${_model_tmp}/yolov8n.onnx" \
                    bash "${REMOTE_DIR}/scripts/fetch_model.sh"
                sudo mv -f "${_model_tmp}/yolov8n.onnx" "${VISION_MODEL_PATH}"
                rm -rf "${_model_tmp}"
                sudo chmod 644 "${VISION_MODEL_PATH}"
                echo "  Model ready: ${VISION_MODEL_PATH} ✓"
            else
                echo "==> Vision model already present: ${VISION_MODEL_PATH} ✓"
            fi
        fi
        ;;
    fake)
        echo "  (fake detector — no vision deps or model needed)"
        ;;
    *)
        echo "  WARNING: unknown NOMON_VISION_DETECTOR='${VISION_DETECTOR}';" >&2
        echo "  the follow-user routine will error at start. Expected:" >&2
        echo "  yolo-onnx | opencv-hog | fake." >&2
        ;;
esac

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
# Make directory writable by SERVICE_USER so key generation succeeds.
sudo chown "${SERVICE_USER}:" "$(dirname "${PLUGIN_KEY_PATH}")" 2>/dev/null || \
    sudo chmod u+w "$(dirname "${PLUGIN_KEY_PATH}")"
PLUGIN_PUBKEY="$(sudo -u "${SERVICE_USER}" "${REMOTE_DIR}/.venv/bin/python" \
    -m autonomon.plugin_auth generate-key "${PLUGIN_KEY_PATH}")"
echo "  Key ready (owner: ${SERVICE_USER}) ✓"

DEVICE_HOSTNAME="$(hostname)"
echo "==> Writing plugin env file ${PLUGIN_ENV_FILE}..."
_tmp_env="$(mktemp)"
cat > "${_tmp_env}" <<EOF_PLUGIN_ENV
# autonomon plugin environment (written by deploy.sh). No secret token here:
# the device JWT is acquired at runtime via Ed25519 challenge-response (ADR-019).
# The nomon-autonomon CLI loads this file at startup (non-overriding), so these
# reach routines whether launched manually or by nomothetic — which carries none
# of autonomon's config (ADR-004/005).
NOMON_DEVICE_URL=${LOCAL_API_URL}
NOMON_PLUGIN_KEY=${PLUGIN_KEY_PATH}
NOMON_PLUGIN_NAME=${PLUGIN_NAME}
NOMON_DEVICE_ID=${DEVICE_HOSTNAME}
NOMON_VISION_DETECTOR=${VISION_DETECTOR}
EOF_PLUGIN_ENV
if [[ -n "${VISION_MODEL_PATH}" ]]; then
    echo "NOMON_VISION_MODEL_PATH=${VISION_MODEL_PATH}" >> "${_tmp_env}"
fi
if [[ -n "${VISION_MODEL_CONFIG}" ]]; then
    echo "NOMON_VISION_MODEL_CONFIG=${VISION_MODEL_CONFIG}" >> "${_tmp_env}"
fi
sudo mv -f "${_tmp_env}" "${PLUGIN_ENV_FILE}"
sudo chmod 644 "${PLUGIN_ENV_FILE}"
echo "  Env file written ✓"

# ── Reload nomothetic to pick up the plugin key (catalogue is file-based) ───────
# The routine catalogue is now published to a file (autonomon ADR-005) so nomothetic
# does not need a restart to read it. The reload is only for plugin auth: the
# newly-generated Ed25519 key must be registered with the freshly-started service.

_NOMOTHETIC_RUNNING=false
if command -v systemctl >/dev/null 2>&1; then
    if sudo systemctl is-active --quiet nomothetic-api.service 2>/dev/null; then
        echo "==> Reloading nomothetic-api.service to pick up the plugin key..."
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
    sudo -u "${SERVICE_USER}" "${REMOTE_DIR}/.venv/bin/python" -m autonomon.plugin_auth register \
        --device-url "${LOCAL_API_URL}" --plugin "${PLUGIN_NAME}" --key "${PLUGIN_KEY_PATH}" \
        2>/dev/null
}
# nomothetic may take a moment to come back after a reload; retry briefly.
_registered=false
for _attempt in 1 2 3 4 5 6; do
    if _register; then
        _registered=true
        break
    fi
    [[ ${_attempt} -lt 6 ]] && sleep 2
done
if [[ "${_registered}" == "true" ]]; then
    echo "  Public key registered ✓"
else
    echo "  WARNING: could not register the public key automatically." >&2
    echo "  nomothetic may not be running locally. Register manually with:" >&2
    echo "    ${REMOTE_DIR}/.venv/bin/python -m autonomon.plugin_auth register \\" >&2
    echo "      --device-url ${LOCAL_API_URL} --plugin ${PLUGIN_NAME} --key ${PLUGIN_KEY_PATH}" >&2
fi

echo ""
echo "✓ autonomon ${TARGET} deployed successfully to ${DEVICE_HOSTNAME}."
echo "  Plugin env: ${PLUGIN_ENV_FILE}"
echo "  Routine catalogue: ${CATALOG_PATH} (nomothetic reads this to list/launch routines)"
echo "  Run a routine with:"
echo "    set -a; . ${PLUGIN_ENV_FILE}; set +a; \\"
echo "    NOMON_PLUGIN_PARAMS='{\"routine\":\"explore\"}' ${REMOTE_DIR}/.venv/bin/nomon-autonomon"

# Clean up backup directory from release deploy (if deployment succeeded)
if [[ "${DEPLOY_LOCAL}" != "true" ]]; then
    for _backup in "${REMOTE_DIR}".backup.*; do
        if [[ -d "${_backup}" ]]; then
            rm -rf "${_backup}"
        fi
    done
fi
END_REMOTE
