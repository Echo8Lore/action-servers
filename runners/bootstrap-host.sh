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
#   - Base packages (curl wget git jq unzip htop python3-pip python3-venv
#     build-essential, etc.)
#   - GitHub CLI (gh, from GitHub's apt repo)
#   - Per-project packages from runners/packages.d/*.txt (all of them, see below)
#   - Node.js 20 (via NodeSource)
#   - Docker CE + Buildx + Compose plugins
#   - Playwright system deps + Chromium (for the 'runner' user)
#   - A dedicated 'runner' service user in the docker group
#
# Every CI host gets the same package set (OPS-46): runners of any project can land
# on any CI box (org runners serve every repo), and fleet-inventory.yml reports
# package parity between them. Keep that true by adding packages HERE (shared) or
# in runners/packages.d/<project>.txt (one project's system deps), never by hand.
#
# Generalized from Weapons_Lore scripts/setup/setup-runner.sh (WEAP-361), with the
# repo-specific registration split out into register-runner.sh.

set -euo pipefail

RUNNER_USER="${RUNNER_USER:-runner}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGES_DIR="${PACKAGES_DIR:-${HERE}/packages.d}"

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
  curl wget git jq unzip htop build-essential \
  ca-certificates gnupg lsb-release \
  libssl-dev pkg-config \
  python3-pip python3-venv

# ── GitHub CLI (GitHub's apt repo; Ubuntu's own gh lags far behind) ──────
echo ">>> Installing GitHub CLI..."
if [[ ! -s /etc/apt/keyrings/githubcli-archive-keyring.gpg ]]; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
  chmod a+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
fi
if [[ ! -f /etc/apt/sources.list.d/github-cli.list ]]; then
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] \
https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list
  apt-get update -qq
fi
apt-get install -y -qq gh
echo "gh: $(gh --version | head -n1)"

# ── Per-project packages (runners/packages.d/*.txt) ──────────────────────
# One file per project, one package per line; '#' starts a comment. A line may
# list alternatives, "a | b": the first with an apt candidate is installed (for
# renames such as Ubuntu 24.04's t64 transition, e.g. libzbar0t64 | libzbar0).
# Every file is applied on every host, so all CI boxes stay identical.
echo ">>> Installing per-project packages from ${PACKAGES_DIR}..."
has_candidate() {
  local c
  c=$(apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/ {print $2; exit}')
  [[ -n "$c" && "$c" != "(none)" ]]
}
if [[ ! -d "$PACKAGES_DIR" ]]; then
  echo "ERROR: ${PACKAGES_DIR} not found; run this script from a checkout of action-servers" >&2
  exit 1
fi
project_pkgs=()
shopt -s nullglob
for list in "$PACKAGES_DIR"/*.txt; do
  list_name="${list##*/}"
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    [[ -z "${line//[[:space:]|]/}" ]] && continue
    chosen=""
    IFS='|' read -r -a alts <<< "$line"
    for alt in "${alts[@]}"; do
      alt="${alt//[[:space:]]/}"
      if [[ ! "$alt" =~ ^[a-z0-9][a-z0-9.+-]+$ ]]; then
        echo "ERROR: ${list_name}: '${alt}' is not a valid package name" >&2
        exit 1
      fi
      if [[ -z "$chosen" ]] && has_candidate "$alt"; then
        chosen="$alt"
      fi
    done
    if [[ -z "$chosen" ]]; then
      echo "ERROR: ${list_name}: no installable package for '${line}'" >&2
      exit 1
    fi
    project_pkgs+=("$chosen")
  done < "$list"
done
shopt -u nullglob
if [[ ${#project_pkgs[@]} -gt 0 ]]; then
  echo "    ${project_pkgs[*]}"
  apt-get install -y -qq "${project_pkgs[@]}"
else
  echo "    (none)"
fi

# ── Node.js 20 (NodeSource) ──────────────────────────────────────────────
echo ">>> Installing Node.js 20..."
if ! command -v node &>/dev/null || [[ "$(node -v)" != v20* ]]; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y -qq nodejs
fi
echo "Node: $(node -v), npm: $(npm -v)"

# ── Docker CE ─────────────────────────────────────────────────────────────
# Buildx and Compose come from Docker's repo too. They are (re)ensured on every
# run, not only on first install, so a host whose Docker predates them catches up.
# A Docker not from Docker's repo (e.g. Ubuntu's docker.io) is left alone: its
# plugins are different packages, and mixing the two conflicts.
echo ">>> Installing Docker..."
if ! command -v docker &>/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
    gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/ubuntu \
    $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io
fi
if dpkg-query -W -f='${Status}' docker-ce 2>/dev/null | grep -q 'install ok installed'; then
  apt-get install -y -qq docker-buildx-plugin docker-compose-plugin
else
  echo "WARN: docker is not Docker CE (docker-ce package not installed);" \
       "install docker-buildx-plugin and docker-compose-plugin by hand for parity"
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

# ── Shared hosted tool cache ──────────────────────────────────────────────
# actions/setup-* read RUNNER_TOOL_CACHE (set from AGENT_TOOLSDIRECTORY by each
# runner's .env). Pre-seed toolchains here with seed-python-toolcache.sh so they
# work on hosts GitHub's manifest doesn't support (non-LTS Ubuntu).
echo ">>> Creating shared hosted tool cache (/opt/hostedtoolcache)..."
install -d -o "${RUNNER_USER}" -g "${RUNNER_USER}" /opt/hostedtoolcache

echo ""
echo "=== Host bootstrap complete ==="
echo "Installed:"
echo "  Node.js: $(node -v)"
echo "  npm:     $(npm -v)"
echo "  Docker:  $(docker --version)"
echo "  gh:      $(gh --version | head -n1)"
echo "  Playwright Chromium: installed for user '${RUNNER_USER}'"
echo ""
echo "Next: register one or more runners with register-runner.sh"
echo "  sudo bash register-runner.sh --scope org --target <owner> --name <name> --token <TOKEN>"
