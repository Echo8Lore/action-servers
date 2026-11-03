#!/usr/bin/env python3
"""restart_liveness.py -- did runner-health's auto-restart path itself fail?

runner-health restarts every offline runner by calling runner-restart.yml in
quiet mode (quiet_unless_online): only a VERIFIED recovery pages from the
restart itself, because the monitor's own deduplicated CRITICAL already owns the
offline runner. A restart path that keeps failing (bad unit name, SSH broken,
unit that will not start) therefore pages nothing beyond that CRITICAL
repeating every 6 h, and its "Auto-restart firing" line reads like the problem
is being handled (OPS-48).

  restart_liveness.py check --repo OWNER/REPO --message MSG.txt [--keys KEYS.txt]
                            [--window-min 360]

Env: GH_TOKEN (the job's GITHUB_TOKEN; actions: read). Missing -> warning, exit 0.

It lists this repo's recent FAILED runner-restart runs (the Actions runs API)
and maps each to its runner through the run's jobs (the jobs API): the restart
job is named `restart-runner (<runner>)` (runner-restart.yml, OPS-48) -- the
run's own name and the jobs' runner_name field (GitHub-hosted) both say nothing
about the target. Only runs the auto-restart itself fired (event workflow_call)
count: a manual dispatch pages every outcome by itself (OPS-47), including the
deliberate probe dispatches used to test that path, and must not look like a
broken auto-restart. API failures warn and alert nothing: this check must never
be the thing that pages falsely.

Per runner with at least one failed run inside the window, one finding
(OPS-75: severity, Action, link):

  CRITICAL - restart path failing: <N> auto-restart run(s) for <runner> failed
  in the last <W> h - latest: <run url>.
  Action: the auto-restart cannot recover <runner> - check the restart run's
  logs, and SSH its host (fleet/inventory.yml) if it is still offline. It will
  not self-heal until this is fixed.

KEYS.txt gets one `restart-path-failing:<runner>` line per runner for
runner-health's alert dedupe (ops/alert_dedupe.py): the condition pages once and
then repeats only on the dedupe's 6 h cadence, not on every monitor run.
Counts and URLs stay out of the keys. A failed run whose jobs cannot be listed
(or whose job does not name its target) maps to `?`, like offline:?; every
condition stops paging once the last failed run ages out of the window, so a
path that starts succeeding is not cried about forever.

Public repo, public log: the log carries runner names, counts and run URLs only.

Stdlib only. The API is injected (`api(path) -> json`) so tests need no network.
"""

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
WORKFLOW = "runner-restart.yml"
DEFAULT_WINDOW_MIN = 360     # the alert dedupe's repeat cadence (alert_dedupe.py)
JOB_PREFIX = "restart-runner ("   # the called workflow's single job: name carries the target


def github_api(token):
    def api(path):
        req = urllib.request.Request(f"{API}/{path.lstrip('/')}", headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "action-servers-restart-liveness"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    return api


class ApiError(Exception):
    pass


def call(api, path):
    try:
        return api(path)
    except urllib.error.HTTPError as e:
        raise ApiError(f"HTTP {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ApiError(type(e).__name__) from None


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def window_text(window_min):
    return f"{window_min // 60} h" if window_min % 60 == 0 else f"{window_min} min"


def failed_runs(api, repo, since):
    """This repo's runner-restart runs that concluded failure since `since`
    (datetime). The server filters by status and created; the client re-checks
    event and conclusion, which is the authoritative filter."""
    q = urllib.parse.urlencode({"status": "failure",
                                "created": ">=" + since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "per_page": 100})
    data = call(api, f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?{q}")
    return [r for r in data.get("workflow_runs") or []
            if r.get("event") == "workflow_call"
            and r.get("conclusion") == "failure"
            and r.get("created_at")]


def job_target(name):
    """Job name -> the runner it targeted (`restart-runner (<runner>)`), or
    None when the name does not say (pre-OPS-48 runs, or a renamed job)."""
    name = (name or "").strip()
    if name.startswith(JOB_PREFIX) and name.endswith(")"):
        target = name[len(JOB_PREFIX):-1].strip()
        if target:
            return target
    return None


def run_target(api, repo, run_id):
    """The runner a runner-restart run targeted, from the run's jobs (the jobs
    API): the restart job's name carries the target (OPS-48). None when it
    cannot be told."""
    data = call(api, f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")
    for job in data.get("jobs") or []:
        target = job_target(job.get("name"))
        if target:
            return target
    return None


def group_failures(api, repo, runs, now, window_min, warn):
    """runs (already failed auto-restart runs) -> {runner: [(url, created), ...]}.
    Runs older than the window are dropped (the API's created filter is trusted,
    but the window is pinned here too); a run whose jobs cannot be listed counts
    against '?' so it still pages; an unparseable timestamp counts as inside the
    window (err toward paging, like a dedupe cache eviction)."""
    since = now - dt.timedelta(minutes=window_min)
    per = {}
    for r in runs:
        try:
            created = parse_ts(r["created_at"])
            if created < since:
                continue
        except ValueError:
            warn(f"restart liveness: cannot parse created_at of run {r.get('id')}; "
                 "counting it inside the window")
            created = now
        try:
            target = run_target(api, repo, r["id"])
        except ApiError as e:
            warn(f"restart liveness: cannot list run {r.get('id')}'s jobs ({e}); "
                 "counting it against an unknown runner")
            target = None
        per.setdefault(target or "?", []).append((r.get("html_url") or "", created))
    return per


def verdict(target, failures, window_min):
    """One runner's failed runs -> the finding text (severity, Action, link)."""
    latest, newest = "", None
    for url, created in failures:
        if url and (newest is None or created > newest):
            latest, newest = url, created
    who = target if target != "?" else "a runner (its run's jobs do not say which)"
    text = (f"CRITICAL - restart path failing: {len(failures)} auto-restart run(s) "
            f"for {who} failed in the last {window_text(window_min)}"
            + (f" - latest: {latest}" if latest else "") + ".\n"
            f"Action: the auto-restart cannot recover {who} - check the restart run's "
            "logs, and SSH its host (fleet/inventory.yml) if it is still offline. "
            "It will not self-heal until this is fixed.")
    return text


def check(api, repo, now, window_min, warn, log):
    """-> list of (text, dedupe key) findings, one per runner whose auto-restart
    failed inside the window, sorted by runner."""
    since = now - dt.timedelta(minutes=window_min)
    try:
        runs = failed_runs(api, repo, since)
    except ApiError as e:
        warn(f"restart liveness: cannot list {WORKFLOW} runs ({e}); not checked")
        return []
    per = group_failures(api, repo, runs, now, window_min, warn)
    findings = []
    for target in sorted(per):
        log(f"restart path failing: {len(per[target])} failed auto-restart run(s) "
            f"for {target if target != '?' else 'an unknown runner'}")
        text = verdict(target, per[target], window_min)
        findings.append((text, f"restart-path-failing:{target}"))
        warn(text)   # visible in the run log even when the dedupe suppresses the page
    return findings


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check")
    p.add_argument("--repo", required=True)
    p.add_argument("--message", required=True)
    p.add_argument("--keys")
    p.add_argument("--window-min", type=int, default=DEFAULT_WINDOW_MIN)
    a = ap.parse_args(argv)

    warn = lambda m: print(f"::warning::{m}")
    log = print
    pathlib.Path(a.message).write_text("")
    if a.keys:
        pathlib.Path(a.keys).write_text("")
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        warn("restart liveness: no GH_TOKEN - not checked.")
        return 0
    findings = check(github_api(token), a.repo, now_utc(), a.window_min, warn, log)
    if findings:
        pathlib.Path(a.message).write_text("\n\n".join(t for t, _ in findings) + "\n")
        if a.keys:
            pathlib.Path(a.keys).write_text("".join(f"{k}\n" for _, k in findings))
    else:
        log("restart path healthy: no failed auto-restart runs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())