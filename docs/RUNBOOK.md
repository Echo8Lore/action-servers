# Fleet runbook

Day-2 operations for the runner fleet and deploys. Most of this is automated by
`runner-health.yml` (hourly); this is the manual fallback and reference.

## Quick reference

| Symptom | Action |
|---|---|
| Runner shows offline | Auto-restart fires hourly; force it: run **Restart Self-Hosted Runner** with the runner's name + unit, or `systemctl restart <unit>` on the host |
| Disk >85% on host | `docker system prune` / `builder prune` (below) |
| Job stuck in-progress >60m | Cancel the run in the Actions UI; check the runner is healthy |
| Deploy failed | Re-run the deploy workflow; or `deploy/deploy.sh` locally; rollback = re-deploy previous ref |
| Token expired (registration) | Mint a fresh one with `gh api ... registration-token` |
| Queue-watchdog alert: "NO registered runner has these labels" (pages at 30 min) | The job's `runs-on` labels match no runner: register one with those labels, or fix the workflow's `runs-on`. It will otherwise be cancelled at 24 h |
| Queue-watchdog alert: "online but busy" (pages at 2 h) | Backlog, not a missing runner: wait, cancel superseded runs, or add runner capacity for those labels |
| Prove the queue watchdog pages (fire drill) | Dispatch **Queue Watchdog Self-Test**, then **Queue Watchdog** with `threshold_minutes: 1`; expect a "NO registered runner" Telegram page; then `gh run cancel` the self-test run promptly (a forgotten one pages again at 30 min and 6 h) |
| `HOST KEY VERIFICATION FAILED` in a fleet job | The host presented a key that isn't the one pinned in `fleet/known_hosts`. Don't re-pin blindly: see **Host key changed** below |
| `HOST KEY VERIFICATION FAILED` in a deploy | The project's VPS presented a key other than its `VPS_HOST_KEY`. Nothing ran on the host. Verify as in **Host key changed** steps 1-2, then update the project's pin: **Deploy host key** |
| `Deploy host key NOT verified` warning | The caller passes no `VPS_HOST_KEY`: pin it (**Deploy host key**) |
| Unsure which host serves a domain, or what runs where | Run **Fleet Inventory** (daily; dispatchable) and read its step summary. Poll, don't trust notes |

## Runners

### List runners on a host
```bash
systemctl list-units 'actions.runner.*' --type=service
journalctl -u 'actions.runner.*' -n 100 --no-pager     # recent logs
```

### Restart a runner
```bash
# Preferred: the workflow (handles cgroup kill + API verify)
#   Actions -> Restart Self-Hosted Runner -> runner_name + systemd_unit
# Manual on the host:
sudo systemctl restart actions.runner.<target-slug>.<name>.service
```

### Add a runner
Use `runners/register-runner.sh` (see ONBOARDING.md). Update `fleet/inventory.yml`.

### Remove a runner
```bash
cd /opt/runners/<scope>-<target-slug>-<name>
sudo ./svc.sh stop && sudo ./svc.sh uninstall
TOKEN=$(gh api -X POST /<orgs|repos>/<target>/actions/runners/remove-token --jq .token)
sudo -u runner ./config.sh remove --token "$TOKEN"
cd / && sudo rm -rf /opt/runners/<scope>-<target-slug>-<name>
# Delete its entry from fleet/inventory.yml and commit.
```

## Disk

```bash
df -h /
sudo docker system df
sudo docker builder prune --keep-storage 5g -f        # trim build cache
sudo docker image prune -a --filter 'until=168h' -f   # remove images >7 days old
```
The monitor warns at 85%. If a host fills repeatedly, tighten the prune schedule or
add a second host (see Scaling).

## Stale jobs

The monitor flags in-progress runs older than 60 min. To clear one:
```bash
gh run cancel <run-id> -R <owner>/<repo>
```
Then confirm the runner that was holding it is online (restart if not).

## Tokens & secrets

- **Runner registration tokens** are short-lived (≈1h) — mint fresh each time.
- **`RUNNER_HEALTH_PAT`** (admin scope on org + personal repos) powers the monitor and
  restart-verify. Rotate by issuing a new PAT and updating the secret on this repo.
- **`<PREFIX>_VPS_*`** secrets are the SSH path the monitor/restart/inventory use to
  reach each host: `DEVOPS` (ovh-staging), `DEVOPS001` (ovh-devops-001), `HOSTING`
  (hosting-vps). Rotate the key on the host and update that prefix's `_VPS_SSH_KEY`.
- Per-project deploy secrets (`VPS_*`, `DEPLOY_ENV_JSON`) live on each project repo,
  including the optional `VPS_HOST_KEY` pin (**Deploy host key**).
- **Host keys** are not secrets: each fleet host's ED25519 key is committed in
  `fleet/known_hosts` under its inventory id, and every fleet SSH call (inventory,
  disk check, restart) verifies it with `StrictHostKeyChecking=yes` (OPS-33).

## Host key changed

A fleet job fails with `HOST KEY VERIFICATION FAILED` (fleet-inventory reports the host
as `host_key_mismatch`) when the host presents a key other than the pinned one. The
usual cause is a reinstalled VPS or regenerated host keys; the other one is a
man-in-the-middle. Treat it as the second until you've shown it's the first:

1. **Verify out of band.** Log in over a path you trust (the OVH KVM/rescue console, or
   an SSH session whose key you already trust) and read the host's own key:
   ```bash
   ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
   ```
2. **Compare** with what the network presents, from a machine you control:
   ```bash
   ssh-keyscan -t ed25519 <address> 2>/dev/null | ssh-keygen -lf -
   ```
   The two SHA256 fingerprints must be identical. If they differ, stop: something on
   the path is answering for the host.
3. **Re-pin by PR.** Replace the host's line in `fleet/known_hosts`, keyed by the
   inventory id, never the address (`ssh-keyscan -H` hashes are brute-forceable back to
   an IPv4 address, and this repo is public):
   ```bash
   ssh-keyscan -t ed25519 <address> 2>/dev/null | awk -v id=<host-id> '{print id, $2, $3}'
   ```
   Update the fingerprint in the file's header comment too (`Fleet Tests` checks that it
   matches). Merge, then re-run **Fleet Inventory** to confirm the host is `ok`.

Removing a host: delete its `hosts:` entry and its `fleet/known_hosts` line together.

## Deploy host key

Project VPSes aren't in the fleet inventory, so the reusable deploy workflow can't pin
them in `fleet/known_hosts`: the caller passes the pin, as the `VPS_HOST_KEY` secret or
the `vps_host_key` input (OPS-38). `deploy/host-key.sh` rewrites it under a fixed alias
(`HostKeyAlias=deploy-target`), so it matches whatever address `VPS_HOST` holds, and
every ssh/rsync in the deploy job uses `StrictHostKeyChecking=yes` against it alone. A
connect-only preflight runs first: a mismatch fails there, before authentication, so no
remote command runs. Without a pin the job warns and trusts the first key it sees (the
old behaviour). `deploy/deploy.sh` reads the same pin from `target.host_key` or
`VPS_HOST_KEY`.

To pin (or re-pin after a verified reinstall):

1. Read the key on the host over a path you trust (console, or a session you already
   trust): `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`.
2. From a machine you control, fetch what the network presents and compare the SHA256
   fingerprints; they must be identical:
   ```bash
   ssh-keyscan -t ed25519 <address> 2>/dev/null | cut -d' ' -f2- | tee /tmp/hk
   ssh-keygen -lf /tmp/hk
   ```
3. Put the `ssh-ed25519 AAAA...` line (no address) in the project's `VPS_HOST_KEY`
   secret: `gh secret set VPS_HOST_KEY -R <owner>/<repo> < /tmp/hk`. Or, since a host key
   is public, pass that same line as the `vps_host_key` input in the caller. Never
   commit a line that names the address, or an `ssh-keyscan -H` hash.
4. Re-run the deploy: its **Verify VPS host key** step logs the pinned fingerprint and
   `SSH to the deploy target OK (host key pinned)`.

Several lines are allowed (e.g. an ed25519 and an rsa key, or the old and new key
during a planned host-key rotation; drop the old one afterwards). A malformed pin fails
the deploy; it never falls back to unverified.

## Deploys

- Normal path: push to `main` → project's `deploy.yml` calls the reusable workflow.
- Manual: `Actions → Deploy → Run workflow`.
- Local fallback: `./deploy/deploy.sh` (config-driven; `--dry-run` to preview).
- Host key: pinned by the caller's `VPS_HOST_KEY` (**Deploy host key**); a mismatch
  fails the **Verify VPS host key** step before anything touches the host.
- Notification: the reusable workflow's `notify` job sends one Telegram message (success
  or failure) when the caller passes `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`; without
  them it skips with a notice. It never fails the deploy.
- **Rollback is code-only**: re-deploy a previous commit/tag. The deploy tags the prior
  image `:rollback` before recreating. DB migrations are forward-only — a code rollback
  does not revert schema.

## Scaling

- **More parallelism for the org:** register a second org runner on the same (or a new)
  host — GitHub distributes jobs round-robin.
- **A second host:** bootstrap it, register runners, add a `hosts:` entry to the
  inventory with its own `ssh_secret_prefix`, pin its host key in `fleet/known_hosts`
  (as in **Host key changed**, step 3, after checking the fingerprint on the host), and
  add the matching `<PREFIX>_VPS_*` secrets. The monitor's disk check and auto-restart pick each host's secret set from
  its prefix automatically; manual restarts take it as the `ssh_secret_prefix` input.
- **Poll-only hosts:** a `hosts:` entry doesn't need runners. `hosting-vps` (the web
  host) is polled for facts and disk but never carries runners (OPS-17); a runner unit
  found there shows up as `extra` drift in the fleet-inventory report.
- **Cost note:** all CI on self-hosted runners ≈ the VPS bill only; GitHub-hosted
  minutes are spent solely by the monitor/restart/inventory/watchdog jobs (`ubuntu-latest`), which are
  cheap and infrequent.
