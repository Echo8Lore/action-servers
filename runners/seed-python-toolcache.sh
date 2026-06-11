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
VERRE="${PYVER//./\\.}"
echo ">>> Locating python-build-standalone asset for $PYVER ($TRIPLE)..."
URL=$(curl -fsSL "https://api.github.com/repos/astral-sh/python-build-standalone/releases?per_page=8" \
  | jq -r --arg re "cpython-${VERRE}(\\.[0-9]+)?\\+.*${TRIPLE}-install_only\\.tar\\.gz$" \
    '[.[].assets[].browser_download_url | select(test($re))] | first // empty')
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
