#!/usr/bin/env bash
# install-fleet-dispatch.sh — install the OPS-23 dispatch timers on ONE CI box.
#
#   sudo bash ops/install-fleet-dispatch.sh --slot a|b [--token-stdin] [--rotate-token]
#   sudo bash ops/install-fleet-dispatch.sh --uninstall
#        bash ops/install-fleet-dispatch.sh --check --slot a|b     # no root, no changes
#
# Run from a checkout of this repo on the box, by the operator or EDI, with sudo.
# Slot a = ovh-staging (CI-1), slot b = ovh-devops-001 (CI-2); ops/systemd/
# fleet-dispatch.schedule says when each slot fires. Idempotent: re-running re-installs
# the same files and keeps the existing token unless --rotate-token is given.
#
# Installs:
#   /usr/local/bin/fleet-dispatch                          <- ops/fleet-dispatch.sh
#   /etc/systemd/system/fleet-dispatch@.service            <- ops/systemd/
#   /etc/systemd/system/fleet-dispatch@.timer              <- ops/systemd/
#   /etc/systemd/system/fleet-dispatch@<name>.timer.d/10-slot.conf   (rendered: OnCalendar)
#   /home/wl_admin/.config/fleet-dispatch/.env             FLEET_DISPATCH_TOKEN=... (0600 wl_admin)
# then verifies the units with systemd-analyze, enables + starts the timers (starting a
# timer dispatches nothing; the first dispatch is the next slot), and lists them.
#
# Token: a fine-grained PAT with Actions read/write on Echo8Lore/action-servers only.
# Prompted with `read -s` on the terminal, or read from stdin with --token-stdin (e.g.
# piped from `bws secret get ...`). It is never echoed, never put in argv, and written
# with umask 077 straight into the .env (SECURITY_POLICY rule 1: Bitwarden + one 0600
# .env owned by the running user). wl_admin is the box's existing admin user; no new
# user is created (rule 6).
#
# --uninstall disables and removes everything above, including the .env. Revoke the PAT
# in GitHub too if the box is being retired.
#
# Exit: 0 ok | 2 usage, not root, or no wl_admin on this box | 3 token missing/malformed
#       | 4 systemd-analyze verify failed (nothing enabled)

set -euo pipefail
export LC_ALL=C

RUN_USER=wl_admin
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCHEDULE="$HERE/systemd/fleet-dispatch.schedule"
UNIT_SRC="$HERE/systemd"
SCRIPT_SRC="$HERE/fleet-dispatch.sh"
BIN=/usr/local/bin/fleet-dispatch
UNIT_DIR=/etc/systemd/system

usage() {
  sed -n '3,6p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 2
}

SLOT="" MODE=install TOKEN_STDIN=false ROTATE=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --slot)         [ "$#" -ge 2 ] || usage; SLOT="$2"; shift 2 ;;
    --slot=*)       SLOT="${1#--slot=}"; shift ;;
    --token-stdin)  TOKEN_STDIN=true; shift ;;
    --rotate-token) ROTATE=true; shift ;;
    --uninstall)    MODE=uninstall; shift ;;
    --check)        MODE=check; shift ;;
    -h|--help)      usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

# ── schedule: "timer <workflow.yml> <slot> <minutes>" lines ──────────────────
# Prints "<name> <minutes>" for the slot, name = workflow file without .yml.
timers_for_slot() {
  awk -v s="$1" '$1 == "timer" && $3 == s { n = $2; sub(/\.yml$/, "", n); print n, $4 }' "$SCHEDULE"
}
all_timer_names() {
  awk '$1 == "timer" { n = $2; sub(/\.yml$/, "", n); print n }' "$SCHEDULE" | sort -u
}

render_dropin() {   # $1 = minutes (MM or MM/step)
  case "$1" in
    [0-5][0-9]|[0-5][0-9]/[1-9]|[0-5][0-9]/[1-5][0-9]) ;;
    *) echo "bad minutes spec in $SCHEDULE: $1" >&2; exit 2 ;;
  esac
  printf '# Rendered by ops/install-fleet-dispatch.sh from ops/systemd/fleet-dispatch.schedule\n'
  printf '# (slot %s). Re-run the installer to change it; do not edit by hand.\n' "$SLOT"
  printf '[Timer]\nOnCalendar=*-*-* *:%s:00 UTC\n' "$1"
}

# Render the unit set for $SLOT into directory $1 (ExecStart pointing at $2).
render_units() {
  local dir="$1" bin="$2" name mins
  mkdir -p "$dir"
  sed "s#^ExecStart=/usr/local/bin/fleet-dispatch #ExecStart=${bin} #" \
    "$UNIT_SRC/fleet-dispatch@.service" > "$dir/fleet-dispatch@.service"
  cp "$UNIT_SRC/fleet-dispatch@.timer" "$dir/fleet-dispatch@.timer"
  while read -r name mins; do
    mkdir -p "$dir/fleet-dispatch@${name}.timer.d"
    render_dropin "$mins" > "$dir/fleet-dispatch@${name}.timer.d/10-slot.conf"
  done < <(timers_for_slot "$SLOT")
}

verify_units() {   # $1 = unit search dir
  local units=() name
  while read -r name _; do
    units+=("fleet-dispatch@${name}.timer" "fleet-dispatch@${name}.service")
  done < <(timers_for_slot "$SLOT")
  # Trailing ':' keeps the default search path, so timers.target etc. resolve.
  SYSTEMD_UNIT_PATH="$1:" systemd-analyze verify "${units[@]}"
}

need_slot() {
  case "$SLOT" in
    a|b) ;;
    *) echo "--slot a|b is required (a = ovh-staging / CI-1, b = ovh-devops-001 / CI-2)" >&2; exit 2 ;;
  esac
  if [ -z "$(timers_for_slot "$SLOT")" ]; then
    echo "no timers for slot $SLOT in $SCHEDULE" >&2; exit 2
  fi
}

# ── --check: render + verify into a temp dir; no root, no changes ────────────
if [ "$MODE" = check ]; then
  need_slot
  WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
  bash -n "$SCRIPT_SRC"
  cp "$SCRIPT_SRC" "$WORK/fleet-dispatch"; chmod 0755 "$WORK/fleet-dispatch"
  render_units "$WORK/units" "$WORK/fleet-dispatch"
  echo "slot ${SLOT}: would install"
  while read -r name mins; do
    printf '  fleet-dispatch@%s.timer  %s\n' "$name" \
      "$(grep '^OnCalendar=' "$WORK/units/fleet-dispatch@${name}.timer.d/10-slot.conf")"
  done < <(timers_for_slot "$SLOT")
  if id -u "$RUN_USER" >/dev/null 2>&1; then
    echo "${RUN_USER}: present"
  else
    echo "${RUN_USER}: MISSING (a real install would refuse on this box)"
  fi
  if command -v systemd-analyze >/dev/null 2>&1; then
    verify_units "$WORK/units" || { echo "systemd-analyze verify FAILED" >&2; exit 4; }
    echo "systemd-analyze verify: ok"
  else
    echo "systemd-analyze not found: units not verified"
  fi
  exit 0
fi

# ── install / uninstall: the box must have wl_admin, and we must be root ─────
if ! id -u "$RUN_USER" >/dev/null 2>&1; then
  echo "REFUSED: no ${RUN_USER} user on this box. The dispatch timers only go on the CI boxes (ovh-staging, ovh-devops-001)." >&2
  exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "REFUSED: run with sudo (installs systemd units)." >&2
  exit 2
fi

RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
# fleet-dispatch@.service hardcodes EnvironmentFile=/home/wl_admin/...; the installer
# must write the .env exactly there, or the unit and the installer disagree.
if [ "$RUN_HOME" != "/home/${RUN_USER}" ]; then
  echo "REFUSED: ${RUN_USER}'s home is '${RUN_HOME}', not /home/${RUN_USER}; the unit's EnvironmentFile would not match." >&2
  exit 2
fi
ENV_DIR="$RUN_HOME/.config/fleet-dispatch"
ENV_FILE="$ENV_DIR/.env"

list_timers() {
  systemctl list-timers --all --no-pager 'fleet-dispatch@*' || true
}

if [ "$MODE" = uninstall ]; then
  for name in $(all_timer_names); do
    systemctl disable --now "fleet-dispatch@${name}.timer" 2>/dev/null || true
    systemctl stop "fleet-dispatch@${name}.service" 2>/dev/null || true
    rm -rf "$UNIT_DIR/fleet-dispatch@${name}.timer.d"
  done
  rm -f "$UNIT_DIR/fleet-dispatch@.service" "$UNIT_DIR/fleet-dispatch@.timer" "$BIN"
  rm -f "$ENV_FILE"
  rmdir "$ENV_DIR" 2>/dev/null || true
  systemctl daemon-reload
  systemctl reset-failed 'fleet-dispatch@*' 2>/dev/null || true
  echo "fleet-dispatch uninstalled (units, script and ${ENV_FILE} removed)."
  echo "If this box is retired, also revoke the PAT in GitHub and remove it from Bitwarden."
  list_timers
  exit 0
fi

need_slot

# ── token ────────────────────────────────────────────────────────────────────
RUN_GROUP="$(id -gn "$RUN_USER")"
# ~/.config is created only if missing; an existing one keeps its owner and mode.
[ -d "$RUN_HOME/.config" ] || install -d -m 0700 -o "$RUN_USER" -g "$RUN_GROUP" "$RUN_HOME/.config"
install -d -m 0700 -o "$RUN_USER" -g "$RUN_GROUP" "$ENV_DIR"

if [ -s "$ENV_FILE" ] && [ "$ROTATE" != true ]; then
  echo "token: keeping the existing ${ENV_FILE} (pass --rotate-token to replace it)"
else
  TOKEN=""
  if [ "$TOKEN_STDIN" = true ]; then
    IFS= read -r TOKEN || true
  elif [ -t 0 ]; then
    IFS= read -rs -p "Fine-grained PAT (Actions read/write on Echo8Lore/action-servers only): " TOKEN || true
    echo
  else
    echo "no terminal to prompt on: pass the token on stdin with --token-stdin" >&2
    exit 3
  fi
  TOKEN="${TOKEN%$'\r'}"
  case "$TOKEN" in
    ''|*[!A-Za-z0-9_]*)
      echo "token empty or not token-shaped (expected [A-Za-z0-9_], e.g. github_pat_...); nothing written" >&2
      exit 3 ;;
  esac
  TMP_ENV="$(umask 077; mktemp "$ENV_DIR/.env.XXXXXX")"
  ( umask 077; printf 'FLEET_DISPATCH_TOKEN=%s\n' "$TOKEN" > "$TMP_ENV" )
  unset TOKEN
  chown "$RUN_USER:$RUN_GROUP" "$TMP_ENV"
  chmod 0600 "$TMP_ENV"
  mv -f "$TMP_ENV" "$ENV_FILE"
  echo "token: written to ${ENV_FILE} (0600 ${RUN_USER})"
fi

# ── files ────────────────────────────────────────────────────────────────────
install -m 0755 -o root -g root "$SCRIPT_SRC" "$BIN"
install -m 0644 -o root -g root "$UNIT_SRC/fleet-dispatch@.service" "$UNIT_DIR/fleet-dispatch@.service"
install -m 0644 -o root -g root "$UNIT_SRC/fleet-dispatch@.timer" "$UNIT_DIR/fleet-dispatch@.timer"
# Drop the drop-ins of every known timer first, so a slot change (or a timer removed
# from the schedule) leaves nothing stale behind.
for name in $(all_timer_names); do
  rm -rf "$UNIT_DIR/fleet-dispatch@${name}.timer.d"
done
while read -r name mins; do
  install -d -m 0755 "$UNIT_DIR/fleet-dispatch@${name}.timer.d"
  render_dropin "$mins" > "$UNIT_DIR/fleet-dispatch@${name}.timer.d/10-slot.conf"
  chmod 0644 "$UNIT_DIR/fleet-dispatch@${name}.timer.d/10-slot.conf"
done < <(timers_for_slot "$SLOT")

systemctl daemon-reload
if ! verify_units "$UNIT_DIR"; then
  echo "systemd-analyze verify FAILED: timers NOT enabled. Fix, or run --uninstall." >&2
  exit 4
fi
echo "systemd-analyze verify: ok"

# This slot's timers are enabled and (re)started, so a changed OnCalendar takes effect.
# Any other known timer without a drop-in (a workflow this slot doesn't run) is disabled.
while read -r name _; do
  systemctl enable --quiet "fleet-dispatch@${name}.timer"
  systemctl restart "fleet-dispatch@${name}.timer"
done < <(timers_for_slot "$SLOT")
for name in $(all_timer_names); do
  if [ ! -d "$UNIT_DIR/fleet-dispatch@${name}.timer.d" ]; then
    systemctl disable --now "fleet-dispatch@${name}.timer" 2>/dev/null || true
  fi
done

echo "fleet-dispatch installed for slot ${SLOT}."
list_timers
