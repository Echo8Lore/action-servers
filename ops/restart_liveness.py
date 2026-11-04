#!/usr/bin/env python3
"""restart_liveness.py -- did runner-health's auto-restart path itself fail?

runner-health restarts every offline runner by calling runner-restart.yml in
quiet mode (quiet_unless_online): only a VERIFIED recovery pages from the
restart itself, because the monitor's own deduplicated CRITICAL already owns
the offline runner. A restart path that keeps failing (bad unit name, SSH
broken, unit that will not start) therefore pages nothing beyond that CRITICAL
repeating every 6 h, and its "Auto-restart firing" line reads like the problem
is being handled (OPS-48).

  restart_liveness.py check --repo OWNER/REPO --message MSG.txt [--keys KEYS.txt]
                            [--incomplete FILE] [--window-min 360]

Env: GH_TOKEN (the job's GITHUB_TOKEN; actions: read). Missing -> warning, exit 0.

The auto-restarts are JOBS inside this workflow's own runs, not runs of
runner-restart.yml: the auto-restart job calls the restart workflow at job
level (`uses: ./.github/workflows/runner-restart.yml`), and a reusable workflow
called that way creates no run of its own -- runner-restart.yml's only runs are
the manual (workflow_dispatch) probes (independent review of PR #24). The
helper therefore lists this repo's recent runner-health runs and reads each
run's jobs, and counts the failed legs of the auto-restart job. Their names are
`auto-restart (...) / restart-runner (<runner>)`: the jobs API renders a called
job as `<caller job leg> / <called job name>`, and the called job's name
carries the target (runner-restart.yml, OPS-48). A leg of the pre-name shape
(`... / restart-runner`, job id only) cannot say its runner and maps to `?`,
like offline:? -- that is stable: the name never changes while the run sits in
the window. No event filter: every run of this workflow is a monitor run, and
manual test dispatches of runner-health restart real offline runners too.

API failures never page and never resolve. Any error (or a missing token)
marks the check INCOMPLETE: the --incomplete file gets "true", the findings
that could be read still page, and runner-health passes --incomplete to
alert_dedupe.py decide, which then never sends "RESOLVED - fleet healthy" on a
run that could not check (the false RESOLVED of PR #24's first round). A run
whose jobs cannot be listed is skipped whole -- counting it against '?' made
the '?' key flap on transient blips (two extra pages per blip, review
should-fix 2); the next run re-reads it instead.

Per runner with at least one failed leg inside the window, one finding
(OPS-75: severity, Action, link):

  CRITICAL - restart path failing: <N> auto-restart attempt(s) for <runner>
  failed in the last <W> h - latest: <job url>.
  Action: the auto-restart cannot recover <runner> - open the linked restart
  job's log in the monitor run, and SSH its host (fleet/inventory.yml) if it is
  still offline. It will not self-heal until this is fixed.

KEYS.txt gets one `restart-path-failing:<runner>` line per runner for
runner-health's alert dedupe (ops/alert_dedupe.py): the condition pages once and
then repeats only on the dedupe's 6 h cadence, not on every monitor run.
Counts and URLs stay out of the keys. Every condition stops paging once the
last failed run ages out of the window, so a path that starts succeeding is not
cried about forever.

Public repo, public log: the log carries runner names, counts and job URLs only.

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
WORKFLOW = "runner-health.yml"    # whose runs carry the auto-restart jobs
CALLER_JOB = "auto-restart"       # runner-health.yml's job that calls runner-restart.yml
CALLED_JOB = "restart-runner"     # runner-restart.yml's job; its name carries the target
JOB_SEP = " / "                   # how the jobs API renders a called job: <caller> / <called>
# A restart leg that timed out is as broken as one that failed: either way the
# automated path did not get the runner back (cancelled legs are operator kills
# and never count).
FAILED_CONCLUSIONS = ("failure", "timed_out")
DEFAULT_WINDOW_MIN = 360     # the alert dedupe's repeat cadence (alert_dedupe.py)
PER_PAGE = 100


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


def list_runs(api, repo, since):
    """This repo's runner-health runs created since `since` (datetime), every
    page. No status or event filter: a failed auto-restart leg fails its run,
    but the run that is still in progress can already carry a failed leg, and
    its conclusion cannot be pre-filtered server-side."""
    runs, page = [], 1
    while True:
        q = urllib.parse.urlencode({"created": ">=" + since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                    "per_page": PER_PAGE, "page": page})
        data = call(api, f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?{q}")
        batch = data.get("workflow_runs") or []
        runs.extend(batch)
        if len(batch) < PER_PAGE:
            return runs
        page += 1


def run_jobs(api, repo, run_id):
    """A run's jobs, every page."""
    jobs, page = [], 1
    while True:
        data = call(api, f"repos/{repo}/actions/runs/{run_id}/jobs"
                         f"?per_page={PER_PAGE}&page={page}")
        batch = data.get("jobs") or []
        jobs.extend(batch)
        if len(batch) < PER_PAGE:
            return jobs
        page += 1


def restart_target(name):
    """A job name from a runner-health run -> (is auto-restart leg, target or
    None). The called part is matched LAST (`rfind`): the composite always ends
    with it, and nothing in the caller-side rendering can shadow it. `?` legs
    (called job without the target-carrying name) return (True, None)."""
    name = (name or "").strip()
    i = name.rfind(f"{JOB_SEP}{CALLED_JOB}")
    if i < 0 or not name.startswith(CALLER_JOB):
        return False, None
    rest = name[i + len(JOB_SEP) + len(CALLED_JOB):].strip()
    if rest.startswith("(") and rest.endswith(")") and rest[1:-1].strip():
        return True, rest[1:-1].strip()
    return True, None


def check(api, repo, now, window_min, warn, log):
    """-> (findings, complete). findings: (text, dedupe key), one per runner
    with a failed auto-restart leg inside the window, sorted by runner.
    complete: False when any API call failed -- the caller must treat the
    absence of keys as "not checked", never as "cleared"."""
    since = now - dt.timedelta(minutes=window_min)
    try:
        runs = list_runs(api, repo, since)
    except ApiError as e:
        warn(f"restart liveness: cannot list {WORKFLOW} runs ({e}); not checked")
        return [], False
    legs = []    # (target-or-None, job, run's raw created_at)
    complete = True
    for r in runs:
        rid = r.get("id")
        if rid is None:
            continue
        raw_created = r.get("created_at")
        if raw_created:
            try:
                if parse_ts(raw_created) < since:
                    continue    # the API's created filter is trusted, but the window is pinned here too
            except ValueError:
                warn(f"restart liveness: cannot parse created_at of run {rid}; "
                     "counting it inside the window")
        else:
            warn(f"restart liveness: run {rid} has no created_at; "
                 "counting it inside the window")
        try:
            jobs = run_jobs(api, repo, rid)
        except ApiError as e:
            # Not '?': that flapped (review should-fix 2). The run is skipped
            # and the check is marked incomplete; the next run re-reads it.
            warn(f"restart liveness: cannot list run {rid}'s jobs ({e}); that run not checked")
            complete = False
            continue
        for job in jobs:
            if job.get("conclusion") not in FAILED_CONCLUSIONS:
                continue
            is_leg, target = restart_target(job.get("name"))
            if is_leg:
                legs.append((target, job, raw_created))
    per = {}
    for target, job, run_created in legs:
        stamp = None
        for s in (job.get("created_at"), run_created):
            if s:
                try:
                    stamp = parse_ts(s)
                    break
                except ValueError:
                    pass
        per.setdefault(target or "?", []).append((job.get("html_url") or "", stamp))
    findings = []
    for target in sorted(per):
        log(f"restart path failing: {len(per[target])} failed auto-restart leg(s) "
            f"for {target if target != '?' else 'an unknown runner'}")
        text = verdict(target, per[target], window_min)
        findings.append((text, f"restart-path-failing:{target}"))
        warn(text)   # visible in the run log even when the dedupe suppresses the page
    if not findings and complete:
        log("restart path healthy: no failed auto-restart attempts.")
    return findings, complete


def latest_url(failures):
    """The newest leg's URL ('' when none): the deep link to the restart job
    that failed last. Legs without a timestamp never win over a stamped one."""
    best_url, best_stamp = "", None
    for url, stamp in failures:
        if not url:
            continue
        if not best_url:
            best_url = url
        if stamp is not None and (best_stamp is None or stamp > best_stamp):
            best_url, best_stamp = url, stamp
    return best_url


def verdict(target, failures, window_min):
    """One runner's failed legs -> the finding text (severity, Action, link)."""
    latest = latest_url(failures)
    who = target if target != "?" else "a runner (its restart job does not say which)"
    text = (f"CRITICAL - restart path failing: {len(failures)} auto-restart attempt(s) "
            f"for {who} failed in the last {window_text(window_min)}"
            + (f" - latest: {latest}" if latest else "") + ".\n"
            f"Action: the auto-restart cannot recover {who} - open the linked restart "
            "job's log in the monitor run, and SSH its host (fleet/inventory.yml) if "
            "it is still offline. It will not self-heal until this is fixed.")
    return text


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check")
    p.add_argument("--repo", required=True)
    p.add_argument("--message", required=True)
    p.add_argument("--keys")
    p.add_argument("--incomplete")
    p.add_argument("--window-min", type=int, default=DEFAULT_WINDOW_MIN)
    a = ap.parse_args(argv)

    warn = lambda m: print(f"::warning::{m}")
    log = print
    pathlib.Path(a.message).write_text("")
    if a.keys:
        pathlib.Path(a.keys).write_text("")
    # Fail-safe: INCOMPLETE starts "true" and is only cleared by a check that
    # really completed -- a crash, an API error or a missing token leaves it
    # (the workflow then never sends a false RESOLVED; review should-fix 1).
    if a.incomplete:
        pathlib.Path(a.incomplete).write_text("true\n")
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        warn("restart liveness: no GH_TOKEN - not checked.")
        return 0
    findings, complete = check(github_api(token), a.repo, now_utc(),
                              a.window_min, warn, log)
    if findings:
        pathlib.Path(a.message).write_text("\n\n".join(t for t, _ in findings) + "\n")
        if a.keys:
            pathlib.Path(a.keys).write_text("".join(f"{k}\n" for _, k in findings))
    if a.incomplete and complete:
        pathlib.Path(a.incomplete).write_text("false\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())