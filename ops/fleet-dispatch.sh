#!/usr/bin/env bash
# fleet-dispatch.sh — dispatch ONE action-servers workflow via the GitHub REST API (OPS-23).
#
#   fleet-dispatch.sh <workflow-file>        e.g.  fleet-dispatch.sh runner-health.yml
#
# Run by the fleet-dispatch@.timer units on both CI boxes (installed to
# /usr/local/bin/fleet-dispatch by ops/install-fleet-dispatch.sh). GitHub throttles
# sub-daily cron to ~6 runs/day; workflow_dispatch is not throttled, so a systemd timer
# that dispatches gives the cadence the cron only declares. The workflow itself still
# runs on a GitHub-hosted runner: nothing here monitors anything.
#
#   POST /repos/Echo8Lore/action-servers/actions/workflows/<file>/dispatches {"ref":"main"}
#
# Env:  FLEET_DISPATCH_TOKEN   fine-grained PAT, Actions read/write on
#                              Echo8Lore/action-servers ONLY. Loaded by systemd from
#                              /home/wl_admin/.config/fleet-dispatch/.env (0600 wl_admin).
# Exit: 0 dispatched
#       2 usage: no argument, or a workflow file not on the allowlist (nothing sent)
#       3 not configured: token empty or not token-shaped (nothing sent)
#       4 rejected: GitHub answered 3xx/4xx (bad/expired token, missing permission,
#         workflow without workflow_dispatch, ...). Not retried: retrying won't fix it.
#       5 failed: network error or 5xx on every attempt (retried, then gave up)
#
# ALLOWLIST. Only the workflows below can be dispatched, whatever the caller passes. The
# PAT's scope (Actions on this one repo) is the real limit; this is defence in depth so
# a caller that can run this script cannot use it to start arbitrary workflows.
#
# The token never reaches argv, stdout or stderr:
#   - the Authorization header goes to curl on stdin (-K -), not in argv (argv is
#     world-readable in /proc and ps), and the variable is unset from the environment
#     curl inherits;
#   - curl runs with -q, so no ~/.curlrc can add or redirect anything;
#   - curl's stderr and the response body go to temp files; only the HTTP status, the
#     curl exit code and GitHub's short error "message" field are logged;
#   - no `set -x`.
# Logs ONE line per run (stdout -> the journal, tag fleet-dispatch).

set -euo pipefail
export LC_ALL=C

ALLOWED_WORKFLOWS=(runner-health.yml queue-watchdog.yml)
REPO="Echo8Lore/action-servers"
REF="main"
API_BASE="${FLEET_DISPATCH_API_BASE:-https://api.github.com}"   # overridden by tests only
ATTEMPTS=3
RETRY_DELAY="${FLEET_DISPATCH_RETRY_DELAY:-10}"                # seconds; tests set 0

log() { printf 'fleet-dispatch: %s\n' "$*"; }

WF="${1:-}"
if [ "$#" -ne 1 ] || [ -z "$WF" ]; then
  log "usage: fleet-dispatch.sh <workflow-file>"
  exit 2
fi
allowed=false
for w in "${ALLOWED_WORKFLOWS[@]}"; do
  [ "$WF" = "$w" ] && allowed=true
done
if [ "$allowed" != true ]; then
  # Printed with %q so a hostile argument cannot inject a fake journal line.
  log "REFUSED $(printf '%q' "$WF"): not on the allowlist (${ALLOWED_WORKFLOWS[*]})"
  exit 2
fi

TOKEN="${FLEET_DISPATCH_TOKEN:-}"
unset FLEET_DISPATCH_TOKEN   # a plain shell variable from here on: curl's env never has it
if [ -z "$TOKEN" ]; then
  log "NOT CONFIGURED: FLEET_DISPATCH_TOKEN is empty - ${WF} not dispatched"
  exit 3
fi
# GitHub tokens are [A-Za-z0-9_]. Anything else (a quote, a newline, a pasted space)
# would corrupt the curl config below, so refuse it rather than quote it.
case "$TOKEN" in
  *[!A-Za-z0-9_]*)
    log "NOT CONFIGURED: FLEET_DISPATCH_TOKEN is not token-shaped - ${WF} not dispatched"
    exit 3 ;;
esac

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

attempt=0
while :; do
  attempt=$((attempt + 1))
  rc=0
  HTTP=$(printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" \
    | curl -q -sS -K - -X POST \
        --connect-timeout 10 --max-time 30 \
        -H 'Accept: application/vnd.github+json' \
        -H 'X-GitHub-Api-Version: 2022-11-28' \
        -H 'User-Agent: action-servers-fleet-dispatch' \
        --data "{\"ref\":\"${REF}\"}" \
        -o "$WORK/body" -w '%{http_code}' \
        "${API_BASE}/repos/${REPO}/actions/workflows/${WF}/dispatches" \
        2> "$WORK/curl.err") || rc=$?

  if [ "$rc" -eq 0 ]; then
    case "$HTTP" in
      2??)
        log "dispatched ${WF} on ${REF} (HTTP ${HTTP}, attempt ${attempt})"
        exit 0 ;;
      5??) why="HTTP ${HTTP}" ;;
      *)
        # GitHub's error JSON carries a short human "message"; it never echoes the token.
        msg=$(grep -o '"message"[[:space:]]*:[[:space:]]*"[^"]*"' "$WORK/body" 2>/dev/null \
                | head -1 | sed 's/^"message"[[:space:]]*:[[:space:]]*//' \
                | tr -cd '[:print:]' | cut -c1-120 || true)
        log "REJECTED ${WF}: HTTP ${HTTP}${msg:+ ${msg}} - not retried"
        exit 4 ;;
    esac
  else
    why="curl exit ${rc}"
  fi

  if [ "$attempt" -ge "$ATTEMPTS" ]; then
    log "FAILED ${WF}: ${why} after ${attempt} attempts"
    exit 5
  fi
  sleep "$((RETRY_DELAY * attempt))"
done
