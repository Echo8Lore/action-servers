#!/usr/bin/env bash
# bootstrap-host.sh — One-time setup for a VPS that will host self-hosted GitHub
# Actions runners. Installs the shared toolchain only; it does NOT register any
# runner (use register-runner.sh for that, once per repo/org you want served).
#
# Usage:
#   sudo bash bootstrap-host.sh
#
# Prerequisites:
#   - Ubuntu 22.04+ with root/sudo access
#
# Installs (idempotent — safe to re-run):
#   - Base packages (curl wget git jq unzip build-essential, etc.)
#   - Node.js 20 (via NodeSource)
#   - Docker CE + Compose plugin
#   - Playwright system deps + Chromium (for the 'runner' user)
#   - A dedicated 'runner' service user in the docker group
#
# Generalized from Weapons_Lore scripts/setup/setup-runner.sh (WEAP-361), with the
# repo-specific registration split out into register-runner.sh.

set -euo pipefail

RUNNER_USER="${RUNNER_USER:-runner}"

echo "=== action-servers — host bootstrap ==="
echo "Service user: ${RUNNER_USER}"
echo ""

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: run as root (sudo bash bootstrap-host.sh)" >&2
  exit 1
fi

# ── Base packages ─────────────────────────────────────────────────────────
echo ">>> Installing base packages..."
apt-get update -qq
apt-get install -y -qq \
  curl wget git jq unzip build-essential \
  ca-certificates gnupg lsb-release \
  libssl-dev pkg-config

# ── Node.js 20 (NodeSource) ──────────────────────────────────────────────
echo ">>> Installing Node.js 20..."
if ! command -v node &>/dev/null || [[ "$(node -v)" != v20* ]]; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y -qq nodejs
fi
echo "Node: $(node -v), npm: $(npm -v)"

# ── Docker CE ─────────────────────────────────────────────────────────────
echo ">>> Installing Docker..."
if ! command -v docker &>/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
    gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/ubuntu \
    $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
echo "Docker: $(docker --version)"

# ── Runner service user ───────────────────────────────────────────────────
echo ">>> Setting up service user '${RUNNER_USER}'..."
if ! id "${RUNNER_USER}" &>/dev/null; then
  useradd -m -s /bin/bash "${RUNNER_USER}"
fi
usermod -aG docker "${RUNNER_USER}"

# ── Playwright system deps + Chromium (best-effort) ───────────────────────
# Installed at the host level so any project's runner can run browser tests
# without re-installing system libraries each job. Per-project Playwright npm
# versions are still resolved from each repo's lockfile at job time.
# Non-fatal: a runner host without browsers is still useful for non-E2E CI, and
# install-deps can lag new Ubuntu releases — don't block bootstrap on it.
echo ">>> Installing Playwright system dependencies + Chromium (best-effort)..."
if npx --yes playwright install-deps chromium; then
  su - "${RUNNER_USER}" -c "npx --yes playwright install chromium" \
    || echo "WARN: Playwright browser download failed — install later if a project needs browsers"
else
  echo "WARN: Playwright system deps unavailable (unsupported distro?) — skipping; install later if needed"
fi

echo ""
echo "=== Host bootstrap complete ==="
echo "Installed:"
echo "  Node.js: $(node -v)"
echo "  npm:     $(npm -v)"
echo "  Docker:  $(docker --version)"
echo "  Playwright Chromium: installed for user '${RUNNER_USER}'"
echo ""
echo "Next: register one or more runners with register-runner.sh"
echo "  sudo bash register-runner.sh --scope org --target <owner> --name <name> --token <TOKEN>"
