#!/usr/bin/env bash
# ============================================================================
# deploy.sh — Config-driven local/fallback deploy to a VPS.
#
# Use when GitHub Actions is unavailable, or for the very first bring-up. The
# reusable workflow (.github/workflows/deploy.yml) is the normal path.
#
# Usage:
#   ./deploy.sh                      # full deploy (confirmation prompt)
#   ./deploy.sh --config path.json   # use a specific config (default: ./config.json)
#   ./deploy.sh --dry-run            # preview, no changes
#   ./deploy.sh --yes                # skip the confirmation prompt
#
# Config schema: see config.example.json. Everything project-specific (container
# names, health endpoint, optional reverse proxy, DB backup, smoke hook) lives there,
# so this script is not tied to any one app. Generalized from Weapons_Lore
# scripts/deploy.sh.
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.json"
DRY_RUN=false
ASSUME_YES=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)  CONFIG_FILE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --yes)     ASSUME_YES=true; shift ;;
    *) echo "Unknown flag: $1"; exit 1 ;;
  esac
done

info()  { echo "[INFO]  $*"; }
warn()  { echo "[WARN]  $*"; }
error() { echo "[ERROR] $*" >&2; }
pass()  { echo "[PASS]  $*"; }
fail()  { echo "[FAIL]  $*" >&2; }

for cmd in ssh tar node; do
  command -v "$cmd" &>/dev/null || { error "Required command not found: $cmd"; exit 1; }
done
[ -f "$CONFIG_FILE" ] || { error "Config not found: $CONFIG_FILE (cp config.example.json config.json)"; exit 1; }

# ── Config accessor (no jq dependency) ──────────────────────────────────────
cfg() {
  node -e '
    const c = JSON.parse(require("fs").readFileSync(process.argv[1],"utf8"));
    const v = process.argv[2].split(".").reduce((o,k)=> (o==null?undefined:o[k]), c);
    if (v==null) process.stdout.write("");
    else if (Array.isArray(v)) process.stdout.write(v.join(" "));
    else if (typeof v==="object") process.stdout.write(JSON.stringify(v));
    else process.stdout.write(String(v));
  ' "$CONFIG_FILE" "$1"
}

VPS_HOST=$(cfg target.ip)
VPS_PORT=$(cfg target.ssh_port); VPS_PORT=${VPS_PORT:-22}
VPS_DOMAIN=$(cfg target.domain)
SSH_USER=$(cfg users.root); [ -n "$SSH_USER" ] || SSH_USER=$(cfg users.service)
SSH_KEY=$(cfg keys.root_private)
REMOTE_PATH=$(cfg app.remote_path)
COMPOSE_PATH=$(cfg app.compose_path); COMPOSE_PATH=${COMPOSE_PATH:-docker-compose.yml}
CONTAINERS=$(cfg app.containers)            # space-separated
PROXY_CONTAINER=$(cfg app.proxy_container)
HEALTH_PATH=$(cfg app.health.path); HEALTH_PATH=${HEALTH_PATH:-/api/health}
HEALTH_EXPECT=$(cfg app.health.expect); HEALTH_EXPECT=${HEALTH_EXPECT:-'"status":"ok"'}
DB_REMOTE=$(cfg app.db.remote_file)
SMOKE_HOOK=$(cfg app.hooks.smoke)

# Resolve relative SSH key against the config's directory.
if [ -n "$SSH_KEY" ] && [[ "$SSH_KEY" != /* ]]; then
  CFG_DIR="$(cd "$(dirname "$CONFIG_FILE")" && pwd)"
  SSH_KEY="$(cd "$CFG_DIR" && cd "$(dirname "$SSH_KEY")" && pwd)/$(basename "$SSH_KEY")"
fi
SSH_OPTS="-o StrictHostKeyChecking=accept-new -p $VPS_PORT"
[ -n "$SSH_KEY" ] && [ -f "$SSH_KEY" ] && SSH_OPTS="$SSH_OPTS -i $SSH_KEY"
SSH_TARGET="$SSH_USER@$VPS_HOST"

[ -n "$VPS_HOST" ]    || { error "target.ip missing in config"; exit 1; }
[ -n "$REMOTE_PATH" ] || { error "app.remote_path missing in config"; exit 1; }
[ -n "$CONTAINERS" ]  || { error "app.containers missing in config"; exit 1; }

info "Target: $SSH_TARGET:$REMOTE_PATH (${VPS_DOMAIN:-no domain})"
info "Containers: $CONTAINERS ${PROXY_CONTAINER:+(+proxy $PROXY_CONTAINER)}"

# ── Confirmation ────────────────────────────────────────────────────────────
if [ "$DRY_RUN" = false ] && [ "$ASSUME_YES" = false ]; then
  echo ""
  echo "=========================================="
  echo "  DEPLOY  ->  $VPS_HOST  (${VPS_DOMAIN:-N/A})"
  echo "  Path: $REMOTE_PATH"
  echo "=========================================="
  read -rp "Type 'deploy' to confirm: " CONFIRM
  [ "$CONFIRM" = "deploy" ] || { info "Aborted."; exit 0; }
fi

PROJECT_DIR="$(pwd)"
TAR_EXCLUDES=(--exclude='node_modules' --exclude='.git' --exclude='*.db'
  --exclude='_qa' --exclude='test-results' --exclude='playwright-report'
  --exclude='__pycache__' --exclude='.env' --exclude='.env.*')

if [ "$DRY_RUN" = true ]; then
  info "[DRY RUN] Would sync $(pwd) -> $SSH_TARGET:$REMOTE_PATH"
  info "[DRY RUN] Files (first 30):"
  (cd "$PROJECT_DIR" && tar cf - "${TAR_EXCLUDES[@]}" . 2>/dev/null) | tar tf - | head -30
  info "[DRY RUN] Would rebuild compose ($COMPOSE_PATH), verify [$CONTAINERS], health $HEALTH_PATH"
  exit 0
fi

# ── Transfer (tar over ssh — Windows/rsync friendly) ────────────────────────
TEMP_DIR="/home/$SSH_USER/app_temp"
info "Transferring to $SSH_TARGET:$TEMP_DIR ..."
ssh $SSH_OPTS "$SSH_TARGET" "rm -rf $TEMP_DIR && mkdir -p $TEMP_DIR"
(cd "$PROJECT_DIR" && tar czf - "${TAR_EXCLUDES[@]}" .) | ssh $SSH_OPTS "$SSH_TARGET" "tar xzf - -C $TEMP_DIR"
pass "Files synced"

# ── Remote deploy ───────────────────────────────────────────────────────────
DB_EXCLUDE=""
[ -n "$DB_REMOTE" ] && DB_EXCLUDE="--exclude '$DB_REMOTE'"

info "Deploying on VPS..."
ssh $SSH_OPTS "$SSH_TARGET" bash -s <<DEPLOY
set -e
sudo rsync -av $DB_EXCLUDE $TEMP_DIR/ $REMOTE_PATH/
sudo chown -R $SSH_USER:$SSH_USER $REMOTE_PATH
cd $REMOTE_PATH
sudo docker compose -f '$COMPOSE_PATH' down 2>/dev/null || true
sudo docker compose -f '$COMPOSE_PATH' up -d --build
rm -rf $TEMP_DIR
DEPLOY
pass "Deploy commands completed"

# ── Post-deploy gates ───────────────────────────────────────────────────────
info "Verifying..."
GATES_PASSED=0; GATES_TOTAL=3
PRIMARY=$(echo "$CONTAINERS" | awk '{print $1}')

# Gate 1: containers running
ALL_UP=true
for c in $CONTAINERS ${PROXY_CONTAINER:-}; do
  RUNNING=$(ssh $SSH_OPTS "$SSH_TARGET" "sudo docker ps -q -f name=$c -f status=running")
  [ -z "$RUNNING" ] && { ALL_UP=false; fail "Container not running: $c"; }
done
if [ "$ALL_UP" = true ]; then pass "Gate 1/3: containers running"; GATES_PASSED=$((GATES_PASSED+1)); fi

# Gate 2: optional smoke hook (inside primary container)
if [ -n "$SMOKE_HOOK" ]; then
  SMOKE=$(ssh $SSH_OPTS "$SSH_TARGET" "sudo docker exec $PRIMARY sh -c \"$SMOKE_HOOK\"" 2>&1) || true
  if echo "$SMOKE" | grep -q 'OK'; then pass "Gate 2/3: smoke hook passed"; GATES_PASSED=$((GATES_PASSED+1));
  else fail "Gate 2/3: smoke hook failed: $SMOKE"; fi
else
  info "Gate 2/3: no smoke hook configured — skipped"; GATES_PASSED=$((GATES_PASSED+1))
fi

# Gate 3: health check
sleep 5
HEALTH_URL="http://$VPS_HOST$HEALTH_PATH"
if curl -Lskf --max-time 10 "$HEALTH_URL" 2>/dev/null | grep -q "$HEALTH_EXPECT"; then
  pass "Gate 3/3: health check passed ($HEALTH_URL)"; GATES_PASSED=$((GATES_PASSED+1))
elif [ -n "$VPS_DOMAIN" ] && curl -skf --max-time 10 "https://$VPS_DOMAIN$HEALTH_PATH" 2>/dev/null | grep -q "$HEALTH_EXPECT"; then
  pass "Gate 3/3: health check passed (https://$VPS_DOMAIN$HEALTH_PATH)"; GATES_PASSED=$((GATES_PASSED+1))
else
  fail "Gate 3/3: health check failed"
fi

echo ""
echo "=========================================="
if [ "$GATES_PASSED" -eq "$GATES_TOTAL" ]; then
  echo "  DEPLOY SUCCESSFUL — $GATES_PASSED/$GATES_TOTAL gates"
else
  echo "  DEPLOY COMPLETED WITH ISSUES — $GATES_PASSED/$GATES_TOTAL gates"
  echo "  Debug: ssh $SSH_TARGET 'sudo docker ps -a && sudo docker logs $PRIMARY'"
fi
echo "  Rollback is code-only (re-deploy a previous ref). DB migrations are forward-only."
echo "=========================================="
[ "$GATES_PASSED" -eq "$GATES_TOTAL" ] || exit 1
