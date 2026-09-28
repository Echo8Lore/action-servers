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
#   - A needrestart drop-in so package upgrades never restart runner units (OPS-49)
#   - Base packages (curl wget git jq unzip htop python3 python3-pip
#     python3-venv build-essential, etc.)
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

# ── Preflight: per-project package lists (runners/packages.d/*.txt) ──────
# One file per project, one package per line; '#' starts a comment. A line may
# list alternatives, "a | b": the first with an apt candidate is installed (for
# renames such as Ubuntu 24.04's t64 transition, e.g. libzbar0t64 | libzbar0).
# Every file is applied on every host, so all CI boxes stay identical.
# Read and syntax-checked here, before anything is installed, so a bad list stops
# the run up front instead of halfway. Which alternative wins is decided later,
# once the apt lists are fresh.
if [[ ! -d "$PACKAGES_DIR" ]]; then
  echo "ERROR: ${PACKAGES_DIR} not found; run this script from a checkout of action-servers" >&2
  exit 1
fi
pkg_lines=()      # "file<TAB>alt1|alt2..." per wanted package
shopt -s nullglob
for list in "$PACKAGES_DIR"/*.txt; do
  list_name="${list##*/}"
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    [[ -z "${line//[[:space:]|]/}" ]] && continue
    if [[ "$line" =~ ^[[:space:]]*\| || "$line" =~ \|[[:space:]]*(\||$) ]]; then
      echo "ERROR: ${list_name}: empty alternative in '${line}'" >&2
      exit 1
    fi
    IFS='|' read -r -a alts <<< "$line"
    clean=()
    for alt in "${alts[@]}"; do
      alt="${alt#"${alt%%[![:space:]]*}"}"   # trim leading whitespace
      alt="${alt%"${alt##*[![:space:]]}"}"   # trim trailing whitespace
      if [[ "$alt" =~ [[:space:]] ]]; then
        echo "ERROR: ${list_name}: '${alt}' holds more than one name; put one package per line" \
             "(use 'a | b' only for alternatives)" >&2
        exit 1
      fi
      if [[ ! "$alt" =~ ^[a-z0-9][a-z0-9.+-]+$ ]]; then
        echo "ERROR: ${list_name}: '${alt}' is not a valid package name" >&2
        exit 1
      fi
      clean+=("$alt")
    done
    pkg_lines+=("${list_name}"$'\t'"$(IFS='|'; echo "${clean[*]}")")
  done < "$list"
done
shopt -u nullglob

# ── needrestart: never auto-restart runner units (OPS-49) ────────────────
# Ubuntu's apt hook (/etc/apt/apt.conf.d/99needrestart) runs needrestart in (a)uto
# mode after every dpkg run, unattended-upgrades included, and it restarts every
# service still mapping an upgraded library. For a runner that kills the job it is
# running: on CI-2 on 2026-09-28 a systemd/libc upgrade had the runner units restarted
# dozens of times in six minutes. The drop-in adds actions.runner.* to needrestart's
# override_rc with 0 (= don't restart; listed under "Service restarts being deferred").
# Upgrades still install and every other service is still restarted; runners load the
# new libraries at their next deliberate restart or a reboot (docs/RUNBOOK.md).
# Done before this script's own apt-get runs, so re-running bootstrap on a live host
# can't restart its runners either. Written even where needrestart isn't installed
# (yet): a later install keeps conf.d/ and honours it.
echo ">>> Excluding runner units from needrestart's automatic restarts..."
NEEDRESTART_CONF="${NEEDRESTART_CONF:-/etc/needrestart/needrestart.conf}"
NEEDRESTART_DROPIN="${NEEDRESTART_DROPIN:-/etc/needrestart/conf.d/50-actions-runner.conf}"
# needrestart_decides CONFIG UNIT... prints "UNIT skip|restart" per unit: CONFIG is
# evaluated the way needrestart evaluates its config (perl; the stock needrestart.conf
# then evals every conf.d/*.conf), and UNIT is matched against override_rc as
# needrestart does (default: restart). Dies if CONFIG doesn't parse.
needrestart_decides() {
  perl -e '
    use strict;   # needrestart evals its config under strict, with these in scope
    our %nrconf = (verbosity => 1, override_rc => {});
    my $LOGPREF = "[main]";
    my $f = shift;
    -r $f or die "$f: unreadable\n";
    eval do { local (@ARGV, $/) = $f; <> };
    die "$f: $@" if $@;
    for my $u (@ARGV) {
      my $r = 1;
      for my $re (keys %{ $nrconf{override_rc} }) {
        if ($u =~ /$re/) { $r = $nrconf{override_rc}{$re}; last }
      }
      print "$u ", ($r ? "restart" : "skip"), "\n";
    }' "$@"
}
nr_tmp=$(mktemp)
cat > "$nr_tmp" <<'NEEDRESTART_CONF'
# Managed by action-servers runners/bootstrap-host.sh (OPS-49); edits here are
# overwritten on the next bootstrap run.
# Never restart GitHub Actions runner units automatically after an upgrade: the
# restart kills the job the runner is running. They show up under "Service restarts
# being deferred"; restart them when idle (runner-restart.yml) or reboot.
$nrconf{override_rc}{qr(^actions\.runner\.)} = 0;
NEEDRESTART_CONF
# The drop-in on its own must parse, skip a runner unit and leave other services alone.
if [[ "$(needrestart_decides "$nr_tmp" actions.runner.Owner-repo.name.service ssh.service)" \
      != $'actions.runner.Owner-repo.name.service skip\nssh.service restart' ]]; then
  rm -f "$nr_tmp"
  echo "ERROR: the needrestart drop-in failed its self-check; nothing installed" >&2
  exit 1
fi
install -d -m 0755 "$(dirname "$NEEDRESTART_DROPIN")"
if cmp -s "$nr_tmp" "$NEEDRESTART_DROPIN"; then
  echo "    ${NEEDRESTART_DROPIN} already up to date"
else
  install -m 0644 "$nr_tmp" "$NEEDRESTART_DROPIN"
  echo "    wrote ${NEEDRESTART_DROPIN}"
fi
rm -f "$nr_tmp"
# And the effective config (stock file plus every drop-in) must skip runner units: a
# broken drop-in, or one overriding this one, fails the run here.
if [[ -r "$NEEDRESTART_CONF" ]]; then
  if ! nr_verdict=$(needrestart_decides "$NEEDRESTART_CONF" actions.runner.Owner-repo.name.service) \
     || [[ "$nr_verdict" != *" skip" ]]; then
    echo "ERROR: needrestart would still restart runner units (${NEEDRESTART_CONF}: ${nr_verdict:-does not evaluate});" \
         "check the other files in $(dirname "$NEEDRESTART_DROPIN")" >&2
    exit 1
  fi
  echo "    verified: needrestart skips actions.runner.* units"
else
  echo "    needrestart not installed; the drop-in is in place for a later install"
fi

# ── GitHub CLI apt repo (GitHub's own; Ubuntu's gh lags far behind) ──────
# The keyring is re-downloaded on every run (GitHub rotates it, e.g. 2026-04) to a
# temp file, checked, then moved into place: a failed or partial download never
# replaces a good keyring. Done before the first `apt-get update` when curl is
# already there, so a rotated key never fails that update.
GH_KEYRING=/etc/apt/keyrings/githubcli-archive-keyring.gpg
gh_repo() {
  local tmp
  install -m 0755 -d /etc/apt/keyrings
  tmp=$(mktemp /etc/apt/keyrings/.githubcli.XXXXXX)
  if ! curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o "$tmp" \
     || ! gpg --batch --quiet --show-keys "$tmp" >/dev/null 2>&1; then
    rm -f "$tmp"
    echo "ERROR: could not download a valid GitHub CLI keyring" >&2
    return 1
  fi
  chmod a+r "$tmp"
  mv -f "$tmp" "$GH_KEYRING"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=${GH_KEYRING}] \
https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list
}
gh_repo_done=""
if command -v curl &>/dev/null && command -v gpg &>/dev/null; then
  gh_repo
  gh_repo_done=1
fi

# ── Base packages ─────────────────────────────────────────────────────────
echo ">>> Installing base packages..."
apt-get update -qq
apt-get install -y -qq \
  curl wget git jq unzip htop build-essential \
  ca-certificates gnupg lsb-release \
  libssl-dev pkg-config \
  python3 python3-pip python3-venv

# ── GitHub CLI ────────────────────────────────────────────────────────────
echo ">>> Installing GitHub CLI..."
if [[ -z "$gh_repo_done" ]]; then
  gh_repo
  apt-get update -qq
fi
apt-get install -y -qq gh
echo "gh: $(gh --version | head -n1)"

# ── Per-project packages (resolved from the preflight lists) ─────────────
echo ">>> Installing per-project packages from ${PACKAGES_DIR}..."
has_candidate() {
  local c
  c=$(apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/ {print $2; exit}')
  [[ -n "$c" && "$c" != "(none)" ]]
}
project_pkgs=()
for entry in "${pkg_lines[@]}"; do
  list_name="${entry%%$'\t'*}"
  IFS='|' read -r -a alts <<< "${entry#*$'\t'}"
  chosen=""
  for alt in "${alts[@]}"; do
    if has_candidate "$alt"; then chosen="$alt"; break; fi
  done
  if [[ -z "$chosen" ]]; then
    echo "ERROR: ${list_name}: no installable package among '${alts[*]}'" >&2
    exit 1
  fi
  project_pkgs+=("$chosen")
done
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
# In particular docker-ce plus Ubuntu's docker-compose-v2 (or docker-buildx) clash:
# both ship binaries under /usr/libexec/docker/cli-plugins.
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
