#!/usr/bin/env bash
# collect-host.sh — runner-side driver for one host leg of fleet-inventory.yml.
#
# Inputs (env): HOST_ID, PREFIX, SSH_HOST, SSH_USER, SSH_KEY, DOMAINS (comma list),
#               GPG_PUBKEY (optional ASCII-armored public key)
# Output: out/<HOST_ID>.json            public facts (no IPs, ports, images, server_names)
#         out/fleet-full-<HOST_ID>.json.gpg   full facts, only if GPG_PUBKEY is set
#
# Exits 0 once it has written the public JSON: an unconfigured or unreachable host is
# a reported fact, not a failed leg. The exception is the host key (OPS-33): a host
# with no pinned key in fleet/known_hosts, or one presenting a different key, still
# gets its JSON (status no_pinned_key / host_key_mismatch) but fails the leg, loudly.
# Nothing here prints the raw collector output; every discovered IP is masked before
# any line that could contain one.

set -euo pipefail

: "${HOST_ID:?}" "${PREFIX:?}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FACTS="python3 ${HERE}/fleet_facts.py"
OUT="${OUT_DIR:-out}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUT"

missing=()
[ -n "${SSH_HOST:-}" ] || missing+=("${PREFIX}_VPS_HOST")
[ -n "${SSH_USER:-}" ] || missing+=("${PREFIX}_VPS_USERNAME")
[ -n "${SSH_KEY:-}" ] || missing+=("${PREFIX}_VPS_SSH_KEY")
if [ "${#missing[@]}" -gt 0 ]; then
  echo "${HOST_ID}: not configured (missing ${missing[*]})"
  $FACTS missing --id "$HOST_ID" --secrets "$(IFS=,; echo "${missing[*]}")" > "${OUT}/${HOST_ID}.json"
  exit 0
fi

# The host secret is masked by GitHub already. If it is a DNS name, its resolved
# addresses are not, and ssh can print them -- mask those too, before connecting.
echo "::add-mask::${SSH_HOST}"
while IFS= read -r ip; do
  [ -n "$ip" ] && echo "::add-mask::${ip}"
done < <(getent ahosts "$SSH_HOST" 2>/dev/null | awk '{print $1}' | sort -u)

# Host key pinned in fleet/known_hosts under this host id (fleet/pinned-ssh.sh).
# shellcheck source=fleet/pinned-ssh.sh
source "${HERE}/pinned-ssh.sh"
printf '%s\n' "$SSH_KEY" > "$WORK/key"
chmod 600 "$WORK/key"
if ! pinned_ssh_opts "$HOST_ID" "$WORK/key"; then
  $FACTS failed --id "$HOST_ID" --status no_pinned_key > "${OUT}/${HOST_ID}.json"
  exit 1
fi
SSH_CMD="${SSH_CMD:-ssh}"   # overridable for local testing only

# ssh's stderr is never printed: its messages can carry addresses the masks above
# do not cover. Only the exit code (and a host-key verdict) is reported.
rc=0
$SSH_CMD "${PINNED_SSH_OPTS[@]}" "${SSH_USER}@${SSH_HOST}" 'bash -s' \
  < "${HERE}/collect-facts.sh" > "$WORK/raw.txt" 2> "$WORK/ssh.err" || rc=$?
krc=0
pinned_ssh_check "$HOST_ID" "$rc" "$WORK/ssh.err" || krc=$?
if [ "$krc" -eq 2 ]; then
  $FACTS failed --id "$HOST_ID" --status host_key_mismatch > "${OUT}/${HOST_ID}.json"
  exit 1
fi
if [ "$rc" -ne 0 ]; then
  echo "${HOST_ID}: SSH failed (exit ${rc})"
  echo "::warning::${HOST_ID}: unreachable over SSH (exit ${rc})"
  $FACTS failed --id "$HOST_ID" --status unreachable > "${OUT}/${HOST_ID}.json"
  exit 0
fi

if ! $FACTS parse "$WORK/raw.txt" > "$WORK/full.json" 2> "$WORK/parse.err"; then
  echo "::warning::${HOST_ID}: collector output unparseable"
  $FACTS failed --id "$HOST_ID" --status collect_failed > "${OUT}/${HOST_ID}.json"
  exit 0
fi

# Mask every address the host reported before anything else is printed.
while IFS= read -r ip; do
  [ -n "$ip" ] && echo "::add-mask::${ip}"
done < <($FACTS ips "$WORK/full.json")

# Written to a temp file and moved into place only on success, so a crash never
# uploads an empty/partial host file.
if $FACTS public "$WORK/full.json" --id "$HOST_ID" --target "$SSH_HOST" --domains "${DOMAINS:-}" \
     > "$WORK/public.json" 2> "$WORK/public.err"; then
  mv "$WORK/public.json" "${OUT}/${HOST_ID}.json"
else
  echo "::warning::${HOST_ID}: building the public facts failed"
  $FACTS failed --id "$HOST_ID" --status collect_failed > "${OUT}/${HOST_ID}.json"
  exit 0
fi

# Optional and best-effort: a bad key must not cost the public report.
if [ -n "${GPG_PUBKEY:-}" ]; then
  export GNUPGHOME="$WORK/gnupg"
  mkdir -m 700 "$GNUPGHOME"
  FPR=""
  if printf '%s\n' "$GPG_PUBKEY" | gpg --batch --quiet --import 2>/dev/null; then
    FPR=$(gpg --batch --with-colons --list-keys 2>/dev/null | awk -F: '/^fpr:/ {print $10; exit}')
  fi
  if [ -n "$FPR" ] && gpg --batch --quiet --trust-model always --encrypt --recipient "$FPR" \
       --output "${OUT}/fleet-full-${HOST_ID}.json.gpg" "$WORK/full.json" 2>/dev/null; then
    echo "${HOST_ID}: full facts encrypted to ${FPR}"
  else
    rm -f "${OUT}/fleet-full-${HOST_ID}.json.gpg"
    echo "::warning::${HOST_ID}: FLEET_REPORT_GPG_PUBKEY could not be used; full facts not retained"
  fi
else
  echo "${HOST_ID}: FLEET_REPORT_GPG_PUBKEY not set; full facts (IPs, ports, images, server_names) not retained"
fi

echo "${HOST_ID}: collected"
