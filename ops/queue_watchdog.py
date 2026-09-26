#!/usr/bin/env python3
"""queue_watchdog.py -- find jobs stuck in the queue and say why (OPS-30).

A job whose `runs-on` labels match no online runner waits silently until GitHub cancels
it at 24h (FieldKit v0.1.4's Windows build). This scans the fleet's repos for jobs
queued longer than a threshold, classifies each one, and produces ONE Telegram message
for the jobs that newly crossed a threshold.

  queue_watchdog.py scan --inventory INV.json --state STATE.json \
      --new-state NEW.json --message MSG.txt \
      [--threshold-min 30] [--busy-threshold-min 120] [--repeat-min 360]

Thresholds are per class (owner decision): a job nobody can run -- no_runner,
runners_offline, unknown -- pages after --threshold-min (30). A job that is merely
waiting its turn -- busy, idle_match, hosted -- pages after --busy-threshold-min (120),
because a backlog usually clears on its own. hosted follows the busy rule: a
GitHub-hosted job always has somewhere to run eventually (the hosted pool), so a long
wait there is a concurrency/billing backlog, not a missing runner. Every class pages
once more at --repeat-min (6 h) if the job is still queued.

Env: GH_TOKEN (RUNNER_HEALTH_PAT). Missing -> warning, exit 0.

Classification (per job, from the runners the repo can use: its repo-level runners
plus its org's runners):
  no_runner        self-hosted labels that NO registered runner carries  (v0.1.4)
  runners_offline  matching runners exist, none online
  busy             a matching runner is online but busy                  (backlog)
  idle_match       a matching runner is online and idle (group restriction? lag?)
  hosted           no "self-hosted" label: GitHub-hosted queue/concurrency/billing
  unknown          nothing visible matches, but a runner list was unreadable, so a
                   missing runner cannot be claimed (token lacks org/repo admin?)

Dedupe: STATE maps "<repo>#<job id>" -> {bucket: [thresholds already alerted]}. The
waiting classes (busy, idle_match, hosted) share the bucket "waiting", so a runner
flapping busy<->idle does not page twice; each blocking class has its own bucket. A job
alerts once per threshold per bucket: e.g. waiting at 120 and 360. A move from waiting
to a blocking class (its runner goes offline -> runners_offline) is new information and
may alert once more. The message always names the precise class. State is normalized
on read (junk values dropped), so a bad cache entry cannot crash every later scan.
Jobs no longer queued drop out of the state. The workflow keeps STATE in the Actions cache and
only adopts NEW state after Telegram accepted the message, so a failed send retries.

Stdlib only. The API is injected (`api(path) -> json`) so tests need no network.
"""

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"


def github_api(token):
    def api(path):
        req = urllib.request.Request(f"{API}/{path.lstrip('/')}", headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "action-servers-queue-watchdog"})
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
        # URLError and TimeoutError are OSErrors too; OSError also covers
        # RemoteDisconnected / ConnectionResetError raised mid-read.
        raise ApiError(type(e).__name__) from None


# ── repos ────────────────────────────────────────────────────────────────────
def watched_repos(inv, api, warn):
    """Repo-scope runner targets + every repo of an org-scope target + watch_repos."""
    repos = set()
    for r in inv.get("runners") or []:
        if r.get("scope") == "repo" and r.get("target"):
            repos.add(r["target"])
    for org in sorted({r["target"] for r in inv.get("runners") or []
                       if r.get("scope") == "org" and r.get("target")}):
        page = 1
        while True:
            try:
                batch = call(api, f"orgs/{org}/repos?per_page=100&page={page}")
            except ApiError as e:
                warn(f"cannot list repos of {org} ({e})")
                break
            repos |= {x["full_name"] for x in batch if not x.get("archived")}
            if len(batch) < 100:
                break
            page += 1
    repos |= set(inv.get("watch_repos") or [])
    return sorted(repos)


# ── jobs ─────────────────────────────────────────────────────────────────────
def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def queued_jobs(repo, api, now, warn):
    """Jobs in status 'queued', from queued AND in_progress runs (a matrix or a later
    job can sit in the queue while its run is already in progress)."""
    out = []
    for status in ("queued", "in_progress"):
        try:
            runs = call(api, f"repos/{repo}/actions/runs?status={status}&per_page=100")
        except ApiError as e:
            warn(f"{repo}: cannot list {status} runs ({e})")
            continue
        for run in runs.get("workflow_runs") or []:
            try:
                jobs = call(api, f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100")
            except ApiError as e:
                warn(f"{repo}: cannot list jobs of a run ({e})")
                continue
            for j in jobs.get("jobs") or []:
                if j.get("status") != "queued" or not j.get("created_at"):
                    continue
                age = (now - parse_ts(j["created_at"])).total_seconds() / 60
                out.append({"repo": repo, "job_id": j["id"], "job": j.get("name", ""),
                            "workflow": run.get("name", ""), "labels": j.get("labels") or [],
                            "age_min": int(age), "run_url": run.get("html_url", "")})
    return out


# ── runners + classification ─────────────────────────────────────────────────
def owner_is_user(owner, api, cache):
    key = f"type:{owner}"
    if key not in cache:
        try:
            cache[key] = call(api, f"users/{owner}").get("type") == "User"
        except ApiError:
            cache[key] = False         # unknown: assume an org, so a missing list counts
    return cache[key]


def runners_for(repo, api, cache):
    """Runners the repo can use: repo-level + org-level (if the owner is an org).

    -> (runners, complete). `complete` is False when some list that could hold a
    matching runner was unreadable (e.g. the token lacks org admin): then "no runner
    matches" is not evidence of a missing runner."""
    owner = repo.split("/")[0]
    sources = [(f"repo:{repo}", f"repos/{repo}/actions/runners?per_page=100")]
    if not owner_is_user(owner, api, cache):
        sources.append((f"org:{owner}", f"orgs/{owner}/actions/runners?per_page=100"))
    runners, complete = [], True
    for key, path in sources:
        if key not in cache:
            try:
                cache[key] = call(api, path).get("runners") or []
            except ApiError:
                cache[key] = None
        if cache[key] is None:
            complete = False
        else:
            runners += cache[key]
    return runners, complete


def classify(labels, runners, complete=True):
    want = {l.lower() for l in labels}
    if "self-hosted" not in want:
        return "hosted"
    match = [r for r in runners or [] if want <= {l["name"].lower() for l in r.get("labels") or []}]
    if not match:
        return "no_runner" if complete else "unknown"
    online = [r for r in match if r.get("status") == "online"]
    if not online:
        return "runners_offline"
    if all(r.get("busy") for r in online):
        return "busy"
    return "idle_match"


EXPLAIN = {
    "no_runner": "NO registered runner has these labels - it will wait until GitHub cancels it at 24h",
    "runners_offline": "matching runner(s) exist but none is online",
    "busy": "a matching runner is online but busy (backlog)",
    "idle_match": "a matching runner is online and idle (runner-group restriction or assignment lag?)",
    "hosted": "GitHub-hosted labels (hosted queue, concurrency or billing limit)",
    "unknown": "no visible runner matches, but a runner list was unreadable (token lacks admin on this repo/org?)",
}


# ── dedupe ───────────────────────────────────────────────────────────────────
WAITING_CLASSES = {"busy", "idle_match", "hosted"}   # will run eventually: page later
WAITING = "waiting"                                  # their shared dedupe bucket


def first_threshold(cls, threshold, busy_threshold):
    return busy_threshold if cls in WAITING_CLASSES else threshold


def bucket(cls):
    """Dedupe bucket. The waiting classes share one, so a runner flapping busy<->idle
    between scans does not page twice; each blocking class keeps its own, so waiting ->
    runners_offline still pages once."""
    return WAITING if cls in WAITING_CLASSES else cls


def clean_thresholds(raw):
    return {t for t in (raw if isinstance(raw, list) else [])
            if isinstance(t, (int, float)) and not isinstance(t, bool)}


def normalize_state(state):
    """Whatever the cache holds, return {job key: {bucket: [thresholds]}} of sane values.

    The state is read back every run; one junk value must never crash a scan, because
    a crashed scan re-saves the same state and the watchdog would then stay silent for
    good. Legacy per-class entries ({"busy": [...]}) fold into their bucket."""
    out = {}
    for key, entry in (state.items() if isinstance(state, dict) else []):
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        merged = {}
        for cls, raw in entry.items():
            if not isinstance(cls, str):
                continue
            ts = clean_thresholds(raw)
            if ts:
                merged.setdefault(bucket(cls), set()).update(ts)
        if merged:
            out[key] = {b: sorted(ts) for b, ts in merged.items()}
    return out


def due(jobs, state, threshold, repeat, busy_threshold=120):
    """-> (jobs to alert now, jobs past their class threshold, new state).

    A job alerts once per threshold crossed, per dedupe bucket (see bucket())."""
    state = normalize_state(state)
    new_state, alert, stuck = {}, [], []
    for j in jobs:
        key = f"{j['repo']}#{j['job_id']}"
        prev = state.get(key, {})
        first = first_threshold(j["class"], threshold, busy_threshold)
        crossed = {t for t in (first, repeat) if t >= first and j["age_min"] >= t}
        if not crossed:
            continue
        stuck.append(j)
        b = bucket(j["class"])
        done = set(prev.get(b, []))
        if crossed - done:
            alert.append(j)
        entry = {k: list(v) for k, v in prev.items()}
        entry[b] = sorted(done | crossed)
        new_state[key] = entry
    return alert, stuck, new_state


def fmt_age(m):
    m = int(m)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def message(alerts, threshold, busy_threshold=120):
    lines = [f"Queue watchdog: {len(alerts)} job(s) stuck in the queue "
             f"(pages after {fmt_age(threshold)} when no runner can take a job, "
             f"{fmt_age(busy_threshold)} when runners are just busy)", ""]
    for j in alerts:
        lines += [f"{j['repo']} - {j['workflow']} / {j['job']}",
                  f"  labels: {', '.join(j['labels']) or '(none)'}",
                  f"  queued: {fmt_age(j['age_min'])}",
                  f"  why: {EXPLAIN[j['class']]}",
                  f"  {j['run_url']}", ""]
    return "\n".join(lines).rstrip() + "\n"


def minutes(text):
    """argparse type: a positive number of minutes. A dispatch input may arrive as
    "45" or "45.5"; anything else is rejected with a clear message."""
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number of minutes: {text!r}") from None
    if not (v > 0 and v != float("inf")):
        raise argparse.ArgumentTypeError(f"minutes must be a positive number, got {text!r}")
    return v


# ── cli ──────────────────────────────────────────────────────────────────────
def scan(inv, state, api, now, threshold, repeat, warn, log, busy_threshold=120):
    repos = watched_repos(inv, api, warn)
    log(f"watching {len(repos)} repo(s)")
    cache, candidates = {}, []
    lowest = min(threshold, busy_threshold)
    for repo in repos:
        for j in queued_jobs(repo, api, now, warn):
            if j["age_min"] < lowest:
                continue              # too young for any class: skip the runner lookups
            j["class"] = classify(j["labels"], *runners_for(repo, api, cache))
            candidates.append(j)
    alerts, stuck, new_state = due(candidates, state, threshold, repeat, busy_threshold)
    # Public log: repo, labels, age and class only. Workflow/job names and run URLs of
    # private repos go to Telegram, not here.
    for j in stuck:
        log(f"stuck: {j['repo']} job {j['job_id']} labels=[{', '.join(j['labels'])}] "
            f"queued {fmt_age(j['age_min'])} -> {j['class']}"
            + (" (alerting)" if j in alerts else " (already alerted)"))
    return alerts, new_state, stuck


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan")
    p.add_argument("--inventory", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--new-state", required=True)
    p.add_argument("--message", required=True)
    p.add_argument("--threshold-min", type=minutes, default=30)
    p.add_argument("--busy-threshold-min", type=minutes, default=120)
    p.add_argument("--repeat-min", type=minutes, default=360)
    a = ap.parse_args(argv)

    warn = lambda m: print(f"::warning::{m}")
    log = print
    pathlib.Path(a.message).write_text("")
    token = os.environ.get("GH_TOKEN", "")
    try:
        state = normalize_state(json.loads(pathlib.Path(a.state).read_text()))
    except (OSError, ValueError):
        state = {}
    if not token:
        warn("No RUNNER_HEALTH_PAT configured - queue watchdog cannot scan.")
        pathlib.Path(a.new_state).write_text(json.dumps(state))
        return 0
    inv = json.loads(pathlib.Path(a.inventory).read_text())
    now = now_utc()
    alerts, new_state, stuck = scan(inv, state, github_api(token), now,
                                    a.threshold_min, a.repeat_min, warn, log,
                                    busy_threshold=a.busy_threshold_min)
    pathlib.Path(a.new_state).write_text(json.dumps(new_state, indent=1, sort_keys=True))
    if alerts:
        pathlib.Path(a.message).write_text(message(alerts, a.threshold_min, a.busy_threshold_min))
    log(f"{len(stuck)} job(s) over threshold, {len(alerts)} new alert(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
