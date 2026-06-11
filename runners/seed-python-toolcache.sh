#!/usr/bin/env bash
# seed-python-toolcache.sh — Make actions/setup-python work on hosts GitHub's
# version manifest doesn't support (e.g. non-LTS Ubuntu like 25.04).
#
# actions/setup-python downloads CPython builds compiled per Ubuntu LTS release;
# on a non-LTS host it errors ("version X not found for Ubuntu YY.YY"). This seeds
# a *relocatable* CPython (astral-sh/python-build-standalone, which bundles pip)
# into the shared hosted tool cache in the exact layout setup-python expects, so
# it finds the version locally and skips the manifest entirely.
#
# Usage:
#   sudo bash seed-python-toolcache.sh <X.Y|X.Y.Z> [TOOLCACHE_DIR]
#   sudo bash seed-python-toolcache.sh 3.13
#
# Runners must point at the same cache: register-runner.sh writes
# AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache into each runner's .env. Restart a
# runner after seeding a new version for in-flight services to pick it up.

set -euo pipefail

PYVER="${1:?usage: seed-python-toolcache.sh <X.Y|X.Y.Z> [TOOLCACHE_DIR]}"
TOOLCACHE="${2:-/opt/hostedtoolcache}"
RUNNER_USER="${RUNNER_USER:-runner}"
TRIPLE="x86_64-unknown-linux-gnu"

[[ $EUID -eq 0 ]] || { echo "ERROR: run as root (sudo)" >&2; exit 1; }

# Locate the newest python-build-standalone install_only asset for this version.
# Use releases/latest + the assets endpoint (paged): the releases *list* endpoint
# truncates each release's 800+ assets, which hides the build we need.
VERRE="${PYVER//./\\.}"
RE="^cpython-${VERRE}(\\.[0-9]+)?\\+.*${TRIPLE}-install_only\\.tar\\.gz$"
REPO="astral-sh/python-build-standalone"
AUTH=(); [ -n "${GITHUB_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer ${GITHUB_TOKEN}")
echo ">>> Locating python-build-standalone asset for $PYVER ($TRIPLE)..."
RID=$(curl -fsSL "${AUTH[@]}" "https://api.github.com/repos/${REPO}/releases/latest" | jq -r '.id')
[ -n "$RID" ] && [ "$RID" != "null" ] || { echo "ERROR: could not resolve latest release" >&2; exit 1; }
URL=""; page=1
while :; do
  CHUNK=$(curl -fsSL "${AUTH[@]}" "https://api.github.com/repos/${REPO}/releases/${RID}/assets?per_page=100&page=${page}")
  CNT=$(echo "$CHUNK" | jq 'length')
  [ "${CNT:-0}" -eq 0 ] && break
  URL=$(echo "$CHUNK" | jq -r --arg re "$RE" '[.[] | select(.name|test($re)) | .browser_download_url] | first // empty')
  [ -n "$URL" ] && break
  [ "$CNT" -lt 100 ] && break
  page=$((page+1))
done
[ -n "$URL" ] || { echo "ERROR: no install_only asset found for cpython-$PYVER $TRIPLE" >&2; exit 1; }

FULL=$(echo "$URL" | grep -oE 'cpython-[0-9]+\.[0-9]+\.[0-9]+' | head -1 | sed 's/cpython-//')
DEST="$TOOLCACHE/Python/$FULL/x64"
echo "    version: $FULL"
echo "    asset:   $URL"

if [ -f "$TOOLCACHE/Python/$FULL/x64.complete" ]; then
  echo ">>> Python $FULL already seeded at $DEST — nothing to do."
  exit 0
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo ">>> Downloading..."
curl -fsSL "$URL" -o "$TMP/py.tar.gz"
tar -xzf "$TMP/py.tar.gz" -C "$TMP"     # extracts to $TMP/python/

mkdir -p "$DEST"
cp -a "$TMP/python/." "$DEST/"
# setup-python expects `python` on x64/bin.
ln -sf python3 "$DEST/bin/python" 2>/dev/null || true

# Mark the version complete (the sentinel setup-python checks) + own it.
touch "$TOOLCACHE/Python/$FULL/x64.complete"
id "$RUNNER_USER" &>/dev/null && chown -R "$RUNNER_USER:$RUNNER_USER" "$TOOLCACHE/Python"

echo ">>> Seeded Python $FULL -> $DEST"
"$DEST/bin/python" --version
"$DEST/bin/python" -m pip --version
echo ">>> Done. Ensure runners set AGENT_TOOLSDIRECTORY=$TOOLCACHE and restart them."
