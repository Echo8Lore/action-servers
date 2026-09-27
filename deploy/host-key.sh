#!/usr/bin/env bash
# host-key.sh — host-key pinning for the per-project deploy path (OPS-38). Source it:
#
#   source deploy/host-key.sh
#   deploy_host_key_setup "$KNOWN_HOSTS_FILE" || exit 1   # reads $VPS_HOST_KEY once
#   deploy_ssh_opts "$KNOWN_HOSTS_FILE" "$DEPLOY_HOST_PINNED"   # fills DEPLOY_SSH_OPTS
#   rc=0; ssh "${DEPLOY_SSH_OPTS[@]}" "$USER@$HOST" true 2> "$ERR" || rc=$?
#   deploy_ssh_check "$rc" "$ERR" || ...
#
# Used by .github/workflows/deploy.yml (fetched from this repo at job.workflow_sha: the
# caller's checkout does not have it) and deploy/deploy.sh. Same conventions as
# fleet/pinned-ssh.sh (OPS-33), but the pin comes from the caller, not from this repo:
# deploy targets are each project's own VPS, named only by that project's secrets.
#
# VPS_HOST_KEY: one or more known_hosts lines for the deploy target, either
# `<type> <base64>` or `<anything> <type> <base64>` (the first field, the host name or
# address, is dropped). Blank and # lines are ignored. Each key is rewritten under
# HostKeyAlias=deploy-target, so the pin matches whatever address VPS_HOST is, and no
# address ever needs to appear in a caller's workflow file or in the logs.
#
# Pinned: StrictHostKeyChecking=yes against that file only. Unpinned: today's
# trust-on-first-use (accept-new) with a loud ::warning::. A malformed pin fails: it
# never quietly falls back to unpinned.

DEPLOY_HOST_KEY_ALIAS=deploy-target

# deploy_host_key_setup <known-hosts-file>
# Reads $VPS_HOST_KEY. Sets DEPLOY_HOST_PINNED=true and writes the normalised pin to the
# file, or sets DEPLOY_HOST_PINNED=false (after a ::warning::) when VPS_HOST_KEY is
# empty. Returns 1, with an ::error::, on a malformed pin.
# shellcheck disable=SC2034  # DEPLOY_HOST_PINNED is read by the sourcing script
deploy_host_key_setup() {
  local kh="$1" line n=0 bad=0 f1 f2 f3 type blob
  local types='^(ssh-(ed25519|rsa|dss)|ecdsa-sha2-nistp(256|384|521)|sk-(ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com)$'
  DEPLOY_HOST_PINNED=false
  if [ -z "${VPS_HOST_KEY//[[:space:]]/}" ]; then
    echo "::warning::Deploy host key NOT verified: VPS_HOST_KEY is not set, so the first key the host presents is trusted (accept-new). Anyone on the path can impersonate the VPS. Pin it: action-servers docs/RUNBOOK.md, Deploy host key."
    return 0
  fi
  : > "$kh" || { echo "::error::cannot write the deploy known_hosts file"; return 1; }
  chmod 600 "$kh"
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    read -r f1 f2 f3 _ <<< "$line"
    [ -z "$f1" ] && continue
    [[ "$f1" == \#* ]] && continue
    if [[ "$f1" =~ $types ]]; then type="$f1" blob="$f2"; else type="$f2" blob="$f3"; fi
    # A marker (@cert-authority / @revoked) or anything else that isn't a plain key
    # line is rejected rather than guessed at.
    if [[ "$f1" == @* ]] || ! [[ "$type" =~ $types ]] || ! [[ "$blob" =~ ^[A-Za-z0-9+/]+={0,2}$ ]]; then
      bad=$((bad + 1)); continue
    fi
    printf '%s %s %s\n' "$DEPLOY_HOST_KEY_ALIAS" "$type" "$blob" >> "$kh"
    n=$((n + 1))
  done <<< "$VPS_HOST_KEY"
  # ssh-keygen parses every line for real (a blob that is valid base64 but not a key of
  # its type fails here). Its output names only the alias, never an address.
  if [ "$bad" -ne 0 ] || [ "$n" -eq 0 ] || ! ssh-keygen -l -f "$kh" > "$kh.fp" 2>/dev/null; then
    echo "::error::VPS_HOST_KEY is set but is not valid known_hosts data (${bad} unparseable line(s), ${n} key(s)). Expected '<type> <base64>' per line, e.g. the output of: ssh-keyscan -t ed25519 <address> | cut -d' ' -f2-. Not deploying unverified (action-servers docs/RUNBOOK.md, Deploy host key)."
    rm -f "$kh.fp"
    return 1
  fi
  DEPLOY_HOST_PINNED=true
  echo "Deploy host key pinned (VPS_HOST_KEY), StrictHostKeyChecking=yes:"
  awk '{print "  " $2, $NF}' "$kh.fp"
  rm -f "$kh.fp"
}

# deploy_ssh_opts <known-hosts-file> <pinned: true|false>
# Fills DEPLOY_SSH_OPTS. No side effects, so every step can call it. Unpinned with a
# file: accept-new into that (per-run) file, i.e. the old keyscan behaviour, then later
# connections in the same run must see the same key. Unpinned with no file (deploy.sh):
# exactly the old `-o StrictHostKeyChecking=accept-new` against ~/.ssh/known_hosts.
deploy_ssh_opts() {
  local kh="$1" pinned="$2"
  if [ "$pinned" = true ]; then
    # GlobalKnownHostsFile=/dev/null: only the pin counts. UpdateHostKeys=no: the server
    # cannot add keys. CheckHostIP=no: the address is never looked up or recorded.
    # The path is quoted inside the value: ssh splits UserKnownHostsFile on spaces.
    # shellcheck disable=SC2034  # read by the sourcing script
    DEPLOY_SSH_OPTS=(-o HostKeyAlias="$DEPLOY_HOST_KEY_ALIAS" -o StrictHostKeyChecking=yes
                     -o UserKnownHostsFile="\"$kh\"" -o GlobalKnownHostsFile=/dev/null
                     -o UpdateHostKeys=no -o CheckHostIP=no)
  elif [ -n "$kh" ]; then
    # shellcheck disable=SC2034
    DEPLOY_SSH_OPTS=(-o HostKeyAlias="$DEPLOY_HOST_KEY_ALIAS" -o StrictHostKeyChecking=accept-new
                     -o UserKnownHostsFile="\"$kh\"" -o GlobalKnownHostsFile=/dev/null
                     -o CheckHostIP=no)
  else
    # shellcheck disable=SC2034
    DEPLOY_SSH_OPTS=(-o StrictHostKeyChecking=accept-new)
  fi
}

# deploy_ssh_check <ssh-exit-code> <ssh-stderr-file>
# Returns 0 on success, 2 on a host-key failure (after a loud ::error::, without ssh's
# stderr), 1 otherwise (the caller decides what to print).
deploy_ssh_check() {
  local rc="$1" err="$2"
  [ "$rc" -eq 0 ] && return 0
  if grep -q 'Host key verification failed' "$err" 2>/dev/null; then
    echo "::error::HOST KEY VERIFICATION FAILED. The deploy target presented a key that does not match the pinned one (VPS_HOST_KEY). Possible man-in-the-middle, or the VPS was reinstalled: verify out of band, then update the caller's VPS_HOST_KEY (action-servers docs/RUNBOOK.md, Deploy host key). Nothing was run on the host."
    return 2
  fi
  return 1
}
