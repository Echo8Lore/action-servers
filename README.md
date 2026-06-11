# action-servers

General-purpose, self-hosted **CI/build runners** and **deploy/ops automation** for all
my projects — the personal account and the `Echo8Lore` org — running on a shared VPS
fleet.

This repo is intentionally **public** so its reusable workflows can be referenced from
repos under *either* owner (GitHub forbids cross-owner references to private reusable
workflows). It contains **no secrets** — every credential is injected at run time via
GitHub repo/org secrets or a local, gitignored config file.

> Extracted and generalized from the `Weapons_Lore` DevOps layer. The n8n / agent
> automation stack stays in that project; this repo is runners + deploy/ops only.

## What's here

| Path | Purpose |
|---|---|
| `runners/bootstrap-host.sh` | One-time per-VPS setup: Node 20, Docker, Playwright/Chromium, base tools |
| `runners/register-runner.sh` | Register one self-hosted runner (repo- or org-scoped); supports many runners per host |
| `fleet/inventory.example.yml` | Template for the runner inventory the monitor reads |
| `deploy/deploy.sh` | Config-driven local/fallback deploy to a VPS |
| `deploy/config.example.json` | Per-project deploy config schema |
| `.github/workflows/deploy.yml` | **Reusable** deploy workflow — projects `uses:` this |
| `.github/workflows/runner-health.yml` | Fleet health monitor (cron) — liveness, disk, stale jobs, auto-restart |
| `.github/workflows/runner-restart.yml` | Parameterized runner restart (dispatch + `workflow_call`) |
| `docs/ONBOARDING.md` | How to add a project (runner + deploy + secrets) |
| `docs/RUNBOOK.md` | Operate the fleet: restart, scale, disk, token rotation |

## Runner topology

GitHub has no user-account runner level — a runner binds to a **repo** or an **org**.

- **Org repos** (`Echo8Lore/*`) → one or two **org-level** runners, shared by every org
  repo automatically.
- **Personal repos** → one **repo-level** runner each.

Both kinds run as separate `systemd` services on the same VPS (multi-runner-per-host).
Every registration is recorded in `fleet/inventory.yml` (gitignored), which drives the
health monitor.

## Quick start

```bash
# 1. One-time, per VPS (as root):
sudo bash runners/bootstrap-host.sh

# 2. Register a runner (mint the token first — see docs/ONBOARDING.md):
sudo bash runners/register-runner.sh \
  --scope org --target Echo8Lore --name org-runner-01 \
  --labels self-hosted,Linux,X64 --token <REGISTRATION_TOKEN>

# 3. Record it in fleet/inventory.yml and commit.
```

See **[docs/ONBOARDING.md](docs/ONBOARDING.md)** to wire a project's CI and deploy, and
**[docs/RUNBOOK.md](docs/RUNBOOK.md)** for day-2 operations.
