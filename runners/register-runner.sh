#!/usr/bin/env bash
# register-runner.sh — Register ONE self-hosted GitHub Actions runner on this host.
#
# Supports many runners per host (each in its own dir + its own systemd service) and
# both repo- and org-scoped registration. Run bootstrap-host.sh once before the first
# runner on a new VPS.
#
# Usage:
#   sudo bash register-runner.sh \
#     --scope <repo|org> \
#     --target <owner/repo | owner> \
#     --name <runner-name> \
#     [--labels self-hosted,Linux,X64,extra] \
#     --token <REGISTRATION_TOKEN>
#
# Mint a registration token first (needs admin on the target):
#   repo: gh api -X POST /repos/<owner>/<repo>/actions/runners/registration-token --jq .token
#   org:  gh api -X POST /orgs/<owner>/actions/runners/registration-token       --jq .token
#
# On success it prints the systemd unit name — copy that into fleet/inventory.yml.
#
# Generalized from Weapons_Lore scripts/setup/setup-runner.sh.

set -euo pipefail

SCOPE="" TARGET="" NAME="" TOKEN="" LABELS="self-hosted,Linux,X64"
RUNNER_USER="${RUNNER_USER:-runner}"

# ── Parse args ────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope)  SCOPE="$2"; shift 2 ;;
    --target) TARGET="$2"; shift 2 ;;
    --name)   NAME="$2"; shift 2 ;;
    --labels) LABELS="$2"; shift 2 ;;
    --token)  TOKEN="$2"; shift 2 ;;
    --user)   RUNNER_USER="$2"; shift 2 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

# ── Validate ──────────────────────────────────────────────────────────────
[[ -n "$SCOPE"  ]] || { echo "ERROR: --scope is required (repo|org)" >&2; exit 1; }
[[ -n "$TARGET" ]] || { echo "ERROR: --target is required" >&2; exit 1; }
[[ -n "$NAME"   ]] || { echo "ERROR: --name is required" >&2; exit 1; }
[[ -n "$TOKEN"  ]] || { echo "ERROR: --token is required" >&2; exit 1; }
[[ $EUID -eq 0  ]] || { echo "ERROR: run as root (sudo)" >&2; exit 1; }

case "$SCOPE" in
  repo)
    [[ "$TARGET" == */* ]] || { echo "ERROR: repo scope needs --target owner/repo" >&2; exit 1; }
    RUNNER_URL="https://github.com/${TARGET}" ;;
  org)
    [[ "$TARGET" != */* ]] || { echo "ERROR: org scope needs --target owner (no slash)" >&2; exit 1; }
    RUNNER_URL="https://github.com/${TARGET}" ;;
  *) echo "ERROR: --scope must be 'repo' or 'org'" >&2; exit 1 ;;
esac

# GitHub derives the systemd unit's middle slug from the URL path with '/' -> '-'.
# repo "owner/repo" -> "owner-repo"; org "owner" -> "owner".
TARGET_SLUG="${TARGET//\//-}"
RUNNER_DIR="/opt/runners/${SCOPE}-${TARGET_SLUG}-${NAME}"
EXPECTED_UNIT="actions.runner.${TARGET_SLUG}.${NAME}.service"

echo "=== Registering runner '${NAME}' ==="
echo "  Scope:   ${SCOPE}"
echo "  Target:  ${TARGET}"
echo "  URL:     ${RUNNER_URL}"
echo "  Labels:  ${LABELS}"
echo "  Dir:     ${RUNNER_DIR}"
echo "  User:    ${RUNNER_USER}"
echo ""

if ! id "${RUNNER_USER}" &>/dev/null; then
  echo "ERROR: user '${RUNNER_USER}' not found — run bootstrap-host.sh first" >&2
  exit 1
fi

# ── Download the runner ───────────────────────────────────────────────────
echo ">>> Fetching latest runner release..."
RUNNER_VERSION=$(curl -s https://api.github.com/repos/actions/runner/releases/latest | jq -r '.tag_name' | sed 's/^v//')
RUNNER_ARCH="linux-x64"

mkdir -p "${RUNNER_DIR}"
chown "${RUNNER_USER}:${RUNNER_USER}" "${RUNNER_DIR}"

cd "${RUNNER_DIR}"
if [ ! -f "./config.sh" ]; then
  RUNNER_TAR="actions-runner-${RUNNER_ARCH}-${RUNNER_VERSION}.tar.gz"
  curl -sL "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${RUNNER_TAR}" -o "${RUNNER_TAR}"
  tar xzf "${RUNNER_TAR}"
  rm -f "${RUNNER_TAR}"
  chown -R "${RUNNER_USER}:${RUNNER_USER}" "${RUNNER_DIR}"
fi

# ── Configure ─────────────────────────────────────────────────────────────
echo ">>> Configuring runner against ${RUNNER_URL}..."
su - "${RUNNER_USER}" -c "cd '${RUNNER_DIR}' && ./config.sh \
  --url '${RUNNER_URL}' \
  --token '${TOKEN}' \
  --name '${NAME}' \
  --labels '${LABELS}' \
  --unattended \
  --replace"

# ── Point runner at the shared hosted tool cache ──────────────────────────
# So toolchains pre-seeded with seed-python-toolcache.sh (and friends) are
# visible to actions/setup-* — essential on hosts GitHub's manifest doesn't
# support (non-LTS Ubuntu). The runner loads KEY=VALUE pairs from its .env.
TOOLCACHE_DIR="/opt/hostedtoolcache"
install -d -o "${RUNNER_USER}" -g "${RUNNER_USER}" "${TOOLCACHE_DIR}"
ENV_FILE="${RUNNER_DIR}/.env"
grep -q '^AGENT_TOOLSDIRECTORY=' "${ENV_FILE}" 2>/dev/null \
  || echo "AGENT_TOOLSDIRECTORY=${TOOLCACHE_DIR}" >> "${ENV_FILE}"
chown "${RUNNER_USER}:${RUNNER_USER}" "${ENV_FILE}" 2>/dev/null || true

# ── Install + start systemd service ───────────────────────────────────────
echo ">>> Installing systemd service..."
cd "${RUNNER_DIR}"
./svc.sh install "${RUNNER_USER}"
./svc.sh start

# Resolve the actual unit name (svc.sh prints/creates it; confirm via systemctl).
ACTUAL_UNIT=$(systemctl list-units --all --type=service --no-legend 'actions.runner.*' \
  | awk '{print $1}' | grep -F ".${NAME}.service" | head -1)
ACTUAL_UNIT="${ACTUAL_UNIT:-$EXPECTED_UNIT}"

echo ""
echo "=== Runner '${NAME}' registered and started ==="
echo "  Verify online: ${RUNNER_URL}/settings/actions/runners"
echo "  systemd unit:  ${ACTUAL_UNIT}"
echo ""
echo ">>> Add this to fleet/inventory.yml:"
cat <<YAML
  - name: ${NAME}
    host: <host-id>
    scope: ${SCOPE}
    target: ${TARGET}
    labels: [${LABELS//,/, }]
    systemd_unit: ${ACTUAL_UNIT}
YAML
