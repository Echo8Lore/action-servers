# Onboarding a project

How to put a repo on the shared runner fleet and give it one-line deploys. Works for
both **org** repos (`Echo8Lore/*`) and **personal** repos.

## Prerequisites (once per fleet)

- A VPS bootstrapped with `runners/bootstrap-host.sh`.
- A token source with admin scope on the org + your personal repos. A fine-grained PAT
  or a GitHub App works; the `gh` CLI authenticated as you is simplest for 2 owners.
- Fleet secrets configured on **this** (`action-servers`) repo for the monitor:
  `RUNNER_HEALTH_PAT`, one `<PREFIX>_VPS_HOST` / `_USERNAME` / `_SSH_KEY` triple per
  `hosts:` entry in `fleet/inventory.yml` (today `DEVOPS`, `DEVOPS001`, `HOSTING`),
  and `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` for alerts (without them the monitor
  runs but cannot alert).
- Each `hosts:` entry's SSH host key pinned in `fleet/known_hosts` as
  `<host-id> ssh-ed25519 AAAA...` (keyed by id, never by address; fingerprint checked
  on the host first; see RUNBOOK "Host key changed"). A host without a pinned key is
  never connected to: its inventory, disk check and restart legs fail.

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
| `VPS_HOST_KEY` | (optional, **recommended**) the VPS's public SSH host key, `<type> <base64>` (one line per key; a leading host field is dropped). Every deploy ssh/rsync then verifies it with `StrictHostKeyChecking=yes` and a mismatch fails before anything runs on the host. Unset: the first key the host presents is trusted, with a warning on every run. How to get it: RUNBOOK **Deploy host key**. Can instead be passed as the `vps_host_key` input (not both) |
| `PRODUCTION_URL` | (optional) base URL for the health check |
| `DEPLOY_ENV_JSON` | (optional) JSON object written to `.env` on first deploy, e.g. `{"NODE_ENV":"production","PORT":"3000"}` |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | (optional) deploy success/failure message via Hermes's bot (`ops/notify-telegram.sh`, `sendMessage` only). If either is missing the notify job skips with a notice |
| `SLACK_WEBHOOK_URL` | **Deprecated, ignored** (OPS-35). Still accepted so callers that pass it explicitly don't break; remove it from your caller |

Telegram deploy notifications only reach a caller whose own repo (or org) exposes
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to the call (via `secrets: inherit` or an
explicit `secrets:` mapping); action-servers' own secrets are not visible to callers.

> Because `action-servers` is public, `uses: <you>/action-servers/...@v1` resolves from
> repos under **either** owner. Pin to a tag (`@v1`) so projects aren't broken by infra
> changes; move the tag forward when you want them to pick up updates.

A host key is public, so the `vps_host_key` input is also fine: in `<type> <base64>`
form it names no address, and it is reviewable in the caller's diff (a secret can't be
read back to check which key is pinned). Never put a `<address> <type> <key>` line, or
an `ssh-keyscan -H` hash, in a public workflow file.

## 4. Local / first-time deploy (fallback)

```bash
cp deploy/config.example.json deploy/config.json   # edit for the project
./deploy/deploy.sh --dry-run                        # preview
./deploy/deploy.sh                                  # deploy (confirms first)
```

Set `target.host_key` in `config.json` (or `VPS_HOST_KEY` in the environment) to pin the
host key here too; `deploy.sh` needs `deploy/host-key.sh` next to it.

## Checklist

- [ ] Runner online for the repo (org runner, or a registered repo runner)
- [ ] `fleet/inventory.yml` updated + committed (personal runners only)
- [ ] New host? Its host key pinned in `fleet/known_hosts` (same PR as its `hosts:` entry)
- [ ] `deploy.yml` added to the project, pinned to `@v1`
- [ ] Deploy secrets set on the project (or org), including `VPS_HOST_KEY` (fingerprint checked on the host)
- [ ] First deploy green (containers + health gate pass)
