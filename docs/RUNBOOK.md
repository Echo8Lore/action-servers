# Fleet runbook

Day-2 operations for the runner fleet and deploys. Most of this is automated by
`runner-health.yml`; this is the manual fallback and reference.

**How often the monitors really run** (OPS-23). GitHub throttles every sub-daily cron
to about 6 runs/day, whatever the cron says. The cadence comes from the dispatch timers
on the two CI boxes (**Fleet dispatch timers** below):

| Monitor | Both CI boxes' timers up | One box | Cron backstop only |
|---|---|---|---|
| `runner-health.yml` (liveness, disk, stale jobs, auto-restart) | every 30 min | hourly | ~every 4-5 h |
| `queue-watchdog.yml` (jobs stuck in the queue) | every 10 min | every 20 min | ~every 4-5 h |

Because runner-health can run every 30 min, its Telegram alert is deduplicated
(`ops/alert_dedupe.py`): an incident pages when it starts, when its set of conditions
changes (another runner goes offline, another host fails its disk check, a new stale
run, a scheduler finding), every 6 h while unchanged ("REPEAT"), and once more as
"RESOLVED - fleet healthy" when everything clears. Auto-restart only notifies a restart that brought the runner back online; a
runner that stays offline is the monitor's CRITICAL. A **Run workflow** with
`test_alert` always sends. A manual restart dispatch notifies every outcome, including
a failed one (OPS-47): the message says whether the restart command failed and whether
the runner is online anyway.

Disk alerts are per host (OPS-45): the alert job reads which `check-disk (<host>)` legs
failed, and at which step, from the run's jobs API, and names the hosts by cause:
"Disk over threshold on: ...", "SSH unreachable (or secrets missing), disk not
checked, on: ..." or "HOST KEY MISMATCH (or no pinned key), disk not checked, on: ..."
(keys `disk:<host>`, `disk-ssh:<host>`, `disk-hostkey:<host>`). A second host failing
while the first is already paged therefore pages at once, and so does a host whose
cause changes. Usage percentages are never part of the key, so a host creeping from
86% to 90% does not re-page. If the jobs API call fails, the alert falls back to one
fleet-wide line "Disk over threshold (or SSH unreachable, or host key mismatch) on a
fleet host" (key `disk`) and a log warning: open the run's check-disk legs to see which.

## Quick reference

| Symptom | Action |
|---|---|
| Runner shows offline | Auto-restart fires on every monitor run (cadence above); force it: run **Restart Self-Hosted Runner** with the runner's name + unit, or `systemctl restart <unit>` on the host |
| Alert: "Disk over threshold on: <host>" | `docker system prune` / `builder prune` on that host (**Disk** below) |
| Alert: "SSH unreachable ... disk not checked, on: <host>" | The monitor could not log in (host down, `<PREFIX>_VPS_*` secrets missing or wrong, or `df` unreadable). The leg's Probe step has the `::error::`. If that host's runners are offline too, the box is down |
| Alert: "HOST KEY MISMATCH ... on: <host>" | Nothing was run on the host. See **Host key changed** below; don't re-pin blindly |
| Runner got "shutdown signal" around 06:00-07:00 UTC | unattended-upgrades + needrestart restarted it: check the host has the drop-in (**Package upgrades and runners** below; Fleet Inventory warns if not) |
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
| Alert: "scheduler degraded: dispatch timers not firing" | Neither CI box has dispatched that workflow for ~2.5 h: the monitors are back on the throttled cron (~6/day). Usually an expired/revoked PAT (both boxes fail at once) or both boxes down. See **Fleet dispatch timers → Troubleshooting** |
| Alert: "NOTICE - dispatch slot a/b (<host>) not firing" | One box's timers are silent; cadence is at half rate. That box is down (the same run likely reports its runners offline / SSH failing), or its timers or `.env` are broken. See **Troubleshooting** |

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

### Package upgrades and runners (needrestart, OPS-49)
Both CI boxes run unattended-upgrades daily (`apt-daily-upgrade.timer`, ~06:00-07:00
UTC). Ubuntu's apt hook then runs needrestart in automatic mode, which restarts every
service still using an upgraded library. For a runner that kills its running job ("The
runner has received a shutdown signal"; CI-2, 2026-09-28). `runners/bootstrap-host.sh`
therefore installs `/etc/needrestart/conf.d/50-actions-runner.conf`:
```perl
$nrconf{override_rc}{qr(^actions\.runner\.)} = 0;
```
Security updates still install and every other service is still restarted; the runner
units are listed under "Service restarts being deferred" instead. Check a host:
```bash
cat /etc/needrestart/conf.d/50-actions-runner.conf
sudo needrestart -r l      # list mode, restarts nothing; outdated runners appear under "deferred"
```
**Fleet Inventory** reports it per host ("Upgrades vs runners" table) and warns about a
runner host whose upgrades would restart its runners.

The runners keep running the old libraries until they restart. Pick them up deliberately:
- **Restart the units when idle.** Check the runner is not busy (`gh api
  /orgs/<org>/actions/runners --jq '.runners[] | {name, busy}'`, or the repo's
  `/repos/<owner>/<repo>/actions/runners`), then **Restart Self-Hosted Runner**
  (runner-restart.yml does not check `busy`; a restart kills a running job).
- **Reboot when `/var/run/reboot-required` exists** (kernel or libc upgrades;
  `/var/run/reboot-required.pkgs` says which). Fleet Inventory's "Reboot required"
  column shows it. Reboot one CI box at a time, when its runners are idle, so the other
  box keeps the queue moving; the runner units are enabled and come back on boot.
  Unattended-upgrades never reboots on its own (`Unattended-Upgrade::Automatic-Reboot`
  is unset, default false).

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
- **`FLEET_DISPATCH_TOKEN`** (fine-grained PAT, Actions read/write on
  `Echo8Lore/action-servers` only) lets the CI boxes' timers dispatch the monitors. Not a
  GitHub secret: canonical copy in Bitwarden, runtime copy in
  `/home/wl_admin/.config/fleet-dispatch/.env` (0600 `wl_admin`) on each CI box. Rotate:
  **Fleet dispatch timers → Rotate the PAT**.
- **`<PREFIX>_VPS_*`** secrets are the SSH path the monitor/restart/inventory use to
  reach each host: `DEVOPS` (ovh-staging), `DEVOPS001` (ovh-devops-001), `HOSTING`
  (hosting-vps). Rotate the key on the host and update that prefix's `_VPS_SSH_KEY`.
- Per-project deploy secrets (`VPS_*`, `DEPLOY_ENV_JSON`) live on each project repo,
  including the optional `VPS_HOST_KEY` pin (**Deploy host key**).
- **Host keys** are not secrets: each fleet host's ED25519 key is committed in
  `fleet/known_hosts` under its inventory id, and every fleet SSH call (inventory,
  disk check, restart) verifies it with `StrictHostKeyChecking=yes` (OPS-33).

## Fleet dispatch timers

GitHub throttles sub-daily cron to ~6 runs/day (OPS-23), but does not throttle
`workflow_dispatch`. So a systemd timer on **each** CI box dispatches the cadence-bound
workflows through the REST API
(`POST /repos/Echo8Lore/action-servers/actions/workflows/<file>/dispatches`, ref
`main`). The workflows still run on GitHub-hosted runners: the boxes only pull the
trigger, so a monitor never runs on a host it monitors. The two boxes interleave, so
together they give the target cadence and either one alone gives half of it. The crons
stay in the workflows as the backstop.

| Slot | Box | `runner-health.yml` | `queue-watchdog.yml` |
|---|---|---|---|
| a | `ovh-staging` (CI-1) | :00 | :00, :20, :40 |
| b | `ovh-devops-001` (CI-2) | :30 | :10, :30, :50 |

Times are UTC. The source of truth is `ops/systemd/fleet-dispatch.schedule`; the
installer and the liveness check both read it.

What goes on each box (`ops/install-fleet-dispatch.sh`):

| Path | What |
|---|---|
| `/usr/local/bin/fleet-dispatch` | `ops/fleet-dispatch.sh`: dispatches one workflow. Allowlisted to `runner-health.yml` and `queue-watchdog.yml`; retries 5xx/network errors twice, never 4xx; one journal line per run |
| `/etc/systemd/system/fleet-dispatch@.service` | oneshot, `User=wl_admin`, sandboxed (`ProtectSystem=strict`, `ProtectHome=yes`, `NoNewPrivileges`, ...) |
| `/etc/systemd/system/fleet-dispatch@.timer` + `fleet-dispatch@<name>.timer.d/10-slot.conf` | the timer; the drop-in holds the slot's `OnCalendar`. `Persistent=false`: no burst of missed dispatches after downtime |
| `/home/wl_admin/.config/fleet-dispatch/.env` | `FLEET_DISPATCH_TOKEN=...`, mode 0600, owner `wl_admin` (SECURITY_POLICY rule 1) |

Exit codes of `fleet-dispatch` (in the journal as the unit's status): 0 dispatched, 2
not on the allowlist or bad usage, 3 token missing or malformed, 4 GitHub refused it
(4xx: token expired or revoked, missing permission), 5 network/5xx on all 3 attempts.

### The PAT

One fine-grained PAT serves both boxes. On GitHub: **Settings → Developer settings →
Fine-grained tokens → Generate new token**:

- Resource owner **Echo8Lore**; Repository access **Only select repositories →
  action-servers**.
- Repository permissions: **Actions: Read and write** (Metadata: read is added
  automatically). Nothing else.
- Expiry: whatever the org allows. If the org requires approval for fine-grained
  tokens, approve it under the org's **Settings → Personal access tokens → Pending
  requests**.

Store it in Bitwarden (canonical copy). It never goes into chat, a ticket, shell
history or a GitHub secret. Accepted risk (operator decision on OPS-23): CI jobs on the
box can read the `.env`, since the runner user is in the docker group. The blast radius
is dispatch, re-run and cancel of action-servers workflows.

### Install on a box

Once per box, as `wl_admin` (NOPASSWD sudo), from an up-to-date checkout of this repo:

```bash
git clone https://github.com/Echo8Lore/action-servers.git ~/action-servers 2>/dev/null \
  || git -C ~/action-servers pull --ff-only
cd ~/action-servers
bash ops/install-fleet-dispatch.sh --check --slot a     # a on ovh-staging, b on ovh-devops-001
sudo bash ops/install-fleet-dispatch.sh --slot a        # prompts for the PAT (hidden input)
```

Paste the PAT from Bitwarden at the hidden prompt. The installer writes the `.env`
(0600 `wl_admin`), installs the script and units, runs `systemd-analyze verify`,
enables the timers and lists them. Starting a timer dispatches nothing; the first
dispatch is the box's next slot. The installer is idempotent: re-running it re-installs
the files and keeps the existing token.

Installer exit codes: 0 ok; 2 refused (bad arguments, not root, no `wl_admin`, or
`wl_admin`'s home is not `/home/wl_admin`) and nothing changed; 3 token empty or
malformed, and nothing written; **4 `systemd-analyze verify` failed**. After exit 4
the script, units and `.env` **are installed but the timers are NOT enabled** (a
re-install over a working box may leave the old timers running on the new files).
Read the verify error, then either fix the cause (usually a stale checkout: `git
pull --ff-only`) and re-run the installer, or run `--uninstall` to go back to nothing.
`systemctl list-timers --all 'fleet-dispatch@*'` shows which state you're in.

Non-interactive (EDI/Hermes, when the PAT is in its Bitwarden Secrets Manager project
and so in its environment): pipe it on stdin, never in argv:

```bash
printf '%s\n' "$FLEET_DISPATCH_TOKEN" \
  | ssh <ci-box> 'cd ~/action-servers && sudo bash ops/install-fleet-dispatch.sh --slot a --token-stdin'
```

Then, once **both** boxes have been dispatching for at least 2.5 h (the check's
window), turn on the liveness check. Setting it earlier makes the first checks report
the missing slots:

```bash
gh variable set FLEET_DISPATCH_ENABLED -R Echo8Lore/action-servers --body true
```

### Verify

```bash
systemctl list-timers --all 'fleet-dispatch@*'           # NEXT/LAST per timer
systemctl list-units --all 'fleet-dispatch@*'             # timers active; a service shows 'failed' if its last run failed
journalctl -u 'fleet-dispatch@*' --since -2h --no-pager   # one line per dispatch, with the exit status
sudo systemctl start fleet-dispatch@runner-health.service # dispatch once now (optional)
```

Use the journal, not `systemctl status <service>`, for past runs: a oneshot that
succeeded is unloaded between runs, so `status` often shows nothing useful.

A good journal line: `fleet-dispatch: dispatched runner-health.yml on main (HTTP 204,
attempt 1)`. On GitHub, the runs show as **workflow_dispatch** events:

```bash
gh run list -R Echo8Lore/action-servers --workflow runner-health.yml \
  --event workflow_dispatch --limit 20 --json createdAt,status
```

### Rotate the PAT

After a leak (rotation is reactive, SECURITY_POLICY rule 4) or when it expires: issue
a new PAT as above, update Bitwarden, revoke the old one, then on **each** box:

```bash
cd ~/action-servers && sudo bash ops/install-fleet-dispatch.sh --slot <a|b> --rotate-token
```

Check with `journalctl -u 'fleet-dispatch@*' -n 5` after the next slot.

### Uninstall

```bash
cd ~/action-servers && sudo bash ops/install-fleet-dispatch.sh --uninstall
```

This stops and disables the timers and removes the script, the units and the `.env`.
If both boxes are uninstalled, set `FLEET_DISPATCH_ENABLED` to `false` (or delete it)
first, or runner-health will report the scheduler as degraded. Revoke the PAT if it is
no longer used anywhere.

### What the scheduler alerts mean

runner-health's `check-scheduler` job (on only when `FLEET_DISPATCH_ENABLED` is `true`)
counts, for runner-health and queue-watchdog, the `workflow_dispatch` runs of the last
150 min. It attributes each run to the slot whose scheduled time it is nearest to (up to
4 min off), and calls a slot **silent** when fewer than half of its expected dispatches
arrived. Manual dispatches away from the slot times are ignored. A GitHub API error
only warns in the log and never alerts.

One blind spot: a manual `gh workflow run` (or **Run workflow**) within 4 min of a
slot time counts for that slot, since timer and manual runs come from the same token
owner and can't be told apart. That can only hide a missing dispatch (a false
negative), never raise a false alarm, and only for the one slot time it lands on.

These findings go through runner-health's alert dedupe like every other condition: a
finding pages when it appears, when the set of silent slots changes, and every 6 h
while it lasts, not every run. The dispatch counts are not part of the fingerprint.

- **WARNING - scheduler degraded: dispatch timers not firing for <workflow>**: every
  slot is silent. That monitor is back on the throttled cron (~6/day). Because both
  boxes stopped at once, suspect the shared PAT first (expired, revoked, or the org
  withdrew its approval): the journal on either box shows `REJECTED ...: HTTP 401` or
  `403`. Then check that the boxes are up.
- **NOTICE - dispatch slot <a|b> (<host>) not firing for <workflow>**: that one box is
  silent and the cadence is at half rate. If the same alert also lists that box's
  runners offline or its disk check failing, the box is down. Otherwise check its timers.

### Troubleshooting

On the silent box:

```bash
systemctl list-timers --all 'fleet-dispatch@*'    # timers missing or NEXT empty -> re-run the installer
journalctl -u 'fleet-dispatch@*' -n 20 --no-pager
```

| Journal says | Fix |
|---|---|
| `NOT CONFIGURED` / unit failed to load its EnvironmentFile | `.env` missing or empty: re-run the installer with `--rotate-token` |
| `REJECTED ...: HTTP 401` | PAT expired or revoked: **Rotate the PAT** |
| `REJECTED ...: HTTP 403` or `404` | PAT lacks Actions read/write on action-servers, or the org hasn't approved it |
| `REJECTED ...: HTTP 422` | the workflow lost its `workflow_dispatch` trigger, or `main` is gone |
| `FAILED ...: curl exit N` / `HTTP 5xx` | network or GitHub outage. It retries on the next slot; nothing to do unless it persists |
| nothing at all | timer not enabled: `sudo systemctl enable --now fleet-dispatch@<name>.timer`, or re-run the installer |

### Re-measure the cadence

Don't trust the timers either. Use the OPS-23 method, per workflow, over at least a day:

```bash
gh run list -R Echo8Lore/action-servers --workflow runner-health.yml --limit 100 \
  --json createdAt,event \
  | jq -r 'group_by(.event)[] | "\(.[0].event): \(length) runs, \(map(.createdAt) | min) .. \(map(.createdAt) | max)"'
```

Runs per day = count / span (in days) for each event. Expected with both boxes:
runner-health ~48/day of `workflow_dispatch` plus ~6 `schedule`; queue-watchdog ~144/day
(`--limit 100` then spans under a day, which is enough).

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
  minutes are spent solely by the monitor/restart/inventory/watchdog jobs
  (`ubuntu-latest`). With both dispatch timers up that is ~48 runner-health and ~144
  queue-watchdog runs a day, all short; hosted minutes are free because this repo is
  public.
