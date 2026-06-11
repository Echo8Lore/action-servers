# Onboarding a project

How to put a repo on the shared runner fleet and give it one-line deploys. Works for
both **org** repos (`Echo8Lore/*`) and **personal** repos.

## Prerequisites (once per fleet)

- A VPS bootstrapped with `runners/bootstrap-host.sh`.
- A token source with admin scope on the org + your personal repos. A fine-grained PAT
  or a GitHub App works; the `gh` CLI authenticated as you is simplest for 2 owners.
- Fleet secrets configured on **this** (`action-servers`) repo for the monitor:
  `RUNNER_HEALTH_PAT`, `DEVOPS_VPS_HOST`, `DEVOPS_VPS_USERNAME`, `DEVOPS_VPS_SSH_KEY`,
  and optionally `SLACK_WEBHOOK_URL`.

## 1. Give the project a runner

**Org repos** already have one — every `Echo8Lore` repo can use the org-level
runner(s). Skip to step 2.

**Personal repos** need their own repo-level runner. On the VPS:

```bash
# Mint a registration token (repo scope):
TOKEN=$(gh api -X POST /repos/<you>/<repo>/actions/runners/registration-token --jq .token)

sudo bash runners/register-runner.sh \
  --scope repo --target <you>/<repo> \
  --name personal-<repo>-01 \
  --labels self-hosted,Linux,X64,<repo> \
  --token "$TOKEN"
```

Copy the `systemd_unit` it prints into `fleet/inventory.yml`, add a `scope: repo`
entry, and commit. (Org runners are registered the same way with `--scope org --target
Echo8Lore`, minting the token from `/orgs/Echo8Lore/...`.)

## 2. Point CI at the fleet (optional)

In the project's CI workflow, target the self-hosted runner:

```yaml
jobs:
  test:
    runs-on: [self-hosted, Linux, X64]   # add a project label for repo-specific runners
```

Org repos share the org runner automatically; personal repos hit their own.

## 3. Wire up deploy

Add **one file** to the project — `.github/workflows/deploy.yml`:

```yaml
name: Deploy
on:
  push: { branches: [main] }
  workflow_dispatch:
jobs:
  deploy:
    uses: <you>/action-servers/.github/workflows/deploy.yml@v1
    with:
      app_dir: /opt/MyApp
      app_container: myapp_app
      proxy_container: myapp_nginx     # omit if no reverse proxy
      proxy_service: nginx             # omit if no reverse proxy
      health_path: /api/health
      db_remote_file: server/app.db    # omit if no DB to back up
    secrets: inherit
```

Then set these **secrets** on the project repo (or inherit org-level ones):

| Secret | Purpose |
|---|---|
| `VPS_SSH_KEY` | SSH private key for the deploy target |
| `VPS_HOST` | VPS IP/hostname |
| `VPS_USERNAME` | SSH user |
| `PRODUCTION_URL` | (optional) base URL for the health check |
| `DEPLOY_ENV_JSON` | (optional) JSON object written to `.env` on first deploy, e.g. `{"NODE_ENV":"production","PORT":"3000"}` |
| `SLACK_WEBHOOK_URL` | (optional) deploy notifications |

> Because `action-servers` is public, `uses: <you>/action-servers/...@v1` resolves from
> repos under **either** owner. Pin to a tag (`@v1`) so projects aren't broken by infra
> changes; move the tag forward when you want them to pick up updates.

## 4. Local / first-time deploy (fallback)

```bash
cp deploy/config.example.json deploy/config.json   # edit for the project
./deploy/deploy.sh --dry-run                        # preview
./deploy/deploy.sh                                  # deploy (confirms first)
```

## Checklist

- [ ] Runner online for the repo (org runner, or a registered repo runner)
- [ ] `fleet/inventory.yml` updated + committed (personal runners only)
- [ ] `deploy.yml` added to the project, pinned to `@v1`
- [ ] Deploy secrets set on the project (or org)
- [ ] First deploy green (containers + health gate pass)
