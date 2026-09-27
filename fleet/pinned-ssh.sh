#!/usr/bin/env bash
# pinned-ssh.sh — host-key pinning for every fleet SSH call (OPS-33). Source it:
#
#   source fleet/pinned-ssh.sh
#   pinned_ssh_opts "$HOST_ID" "$KEY_FILE" || exit 1    # fills PINNED_SSH_OPTS
#   rc=0; ssh "${PINNED_SSH_OPTS[@]}" "$USER@$HOST" '...' 2> "$ERR" || rc=$?
#   pinned_ssh_check "$HOST_ID" "$rc" "$ERR" || ...
#
# Used by fleet/collect-host.sh (fleet-inventory.yml), runner-health.yml (check-disk)
# and runner-restart.yml. The pinned keys live in fleet/known_hosts, keyed by inventory
# host id (HostKeyAlias), so no address is ever written to the repo.
#
# Neither function prints ssh's stderr: fleet-inventory must never publish an address,
# and the host-key banner adds nothing the ::error:: line below does not say.

PINNED_KNOWN_HOSTS="${PINNED_KNOWN_HOSTS:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/known_hosts}"

# pinned_ssh_opts <host-id> <private-key-file>
# Refuses (returns 1, with an ::error::) a host that has no pinned key: an unpinned
# host is never connected to.
pinned_ssh_opts() {
  local id="$1" key="$2"
  if ! awk -v id="$id" '$1 == id { found = 1 } END { exit !found }' "$PINNED_KNOWN_HOSTS" 2>/dev/null; then
    echo "::error::${id}: no pinned host key in fleet/known_hosts; not connecting (see docs/RUNBOOK.md, Host key changed)"
    return 1
  fi
  # GlobalKnownHostsFile=/dev/null: only the pinned file counts. UpdateHostKeys=no: the
  # server cannot add keys. CheckHostIP=no: the address is never looked up or recorded.
  # The path is quoted inside the value: ssh splits UserKnownHostsFile on spaces (a
  # checkout under "/home/x/My Repos/..." would otherwise name two files, neither real).
  # shellcheck disable=SC2034  # read by the sourcing script
  PINNED_SSH_OPTS=(-i "$key" -o BatchMode=yes -o ConnectTimeout=20
                   -o HostKeyAlias="$id" -o StrictHostKeyChecking=yes
                   -o UserKnownHostsFile="\"$PINNED_KNOWN_HOSTS\"" -o GlobalKnownHostsFile=/dev/null
                   -o UpdateHostKeys=no -o CheckHostIP=no -o LogLevel=ERROR)
}

# pinned_ssh_check <host-id> <ssh-exit-code> <ssh-stderr-file>
# Returns 0 on success, 2 on a host-key failure (after a loud ::error::), 1 otherwise.
pinned_ssh_check() {
  local id="$1" rc="$2" err="$3"
  [ "$rc" -eq 0 ] && return 0
  if grep -q 'Host key verification failed' "$err" 2>/dev/null; then
    echo "::error::${id}: HOST KEY VERIFICATION FAILED. The key the server presented does not match fleet/known_hosts. Possible man-in-the-middle, or the VPS was reinstalled: verify out of band, then re-pin by PR (docs/RUNBOOK.md, Host key changed)."
    return 2
  fi
  return 1
}
