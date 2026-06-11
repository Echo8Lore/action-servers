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
- **`DEVOPS_VPS_*`** secrets are the SSH path the monitor/restart use to reach the
  primary host. Rotate the key on the host and update `DEVOPS_VPS_SSH_KEY`.
- Per-project deploy secrets (`VPS_*`, `DEPLOY_ENV_JSON`) live on each project repo.

## Deploys

- Normal path: push to `main` → project's `deploy.yml` calls the reusable workflow.
- Manual: `Actions → Deploy → Run workflow`.
- Local fallback: `./deploy/deploy.sh` (config-driven; `--dry-run` to preview).
- **Rollback is code-only**: re-deploy a previous commit/tag. The deploy tags the prior
  image `:rollback` before recreating. DB migrations are forward-only — a code rollback
  does not revert schema.

## Scaling

- **More parallelism for the org:** register a second org runner on the same (or a new)
  host — GitHub distributes jobs round-robin.
- **A second host:** bootstrap it, register runners, add a `hosts:` entry to the
  inventory with its own `ssh_secret_prefix`, and add the matching `<PREFIX>_VPS_*`
  secrets. (Per-host restart currently uses the `DEVOPS_*` set; a second host's restart
  needs its own secret set wired into `runner-restart.yml`.)
- **Cost note:** all CI on self-hosted runners ≈ the VPS bill only; GitHub-hosted
  minutes are spent solely by the monitor/restart jobs (`ubuntu-latest`), which are
  cheap and infrequent.
