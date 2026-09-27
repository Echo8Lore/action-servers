#!/usr/bin/env python3
"""dispatch_liveness.py -- are the OPS-23 dispatch timers still firing?

The cadence-dependent workflows (runner-health, queue-watchdog) are dispatched by
systemd timers on both CI boxes, because GitHub throttles their cron to ~6 runs/day. If
the timers die silently, the monitors fall back to that throttled cron and nobody
notices. This counts each workflow's recent workflow_dispatch runs and says when a
slot has gone quiet.

  dispatch_liveness.py check --schedule ops/systemd/fleet-dispatch.schedule \
      --repo OWNER/REPO --message MSG.txt [--keys KEYS.txt] [--window-min 150]

Env: GH_TOKEN (the job's GITHUB_TOKEN; actions: read). Missing -> warning, exit 0.

From the schedule file it knows when each slot (a = one CI box, b = the other) should
have dispatched each workflow in the last --window-min minutes. Every
workflow_dispatch run is attributed to the scheduled time it is nearest to, within
TOLERANCE_MIN (the timers fire on the second, and GitHub creates the run a few seconds
later); a run near no slot time (someone clicked "Run workflow") counts for nobody. A
slot is SILENT for a workflow when fewer than half of its expected dispatches have a
run. Per workflow:
  - every slot silent  -> WARNING "scheduler degraded: dispatch timers not firing". The
                          monitor is back on GitHub cron (~6/day).
  - one slot silent    -> NOTICE naming that slot's box: the cadence is at half rate
                          (that box is down, or its timers/token are broken).
  - otherwise          -> nothing.
The "half of expected" bar tolerates a missed dispatch or two (a GitHub blip, a box
reboot) without paging; the 150-minute window gives runner-health at least two
expected dispatches per slot, so one miss is never a silent slot.

A slot's times are computed only up to GRACE_MIN before now, so a dispatch that is due
this very minute but not created yet is not expected. Failures to read the API warn and
alert nothing: this check must never be the thing that pages falsely.

KEYS.txt gets one line per finding, `scheduler:<level>:<workflow>:<quiet slots>` (no
counts), for runner-health's alert dedupe (ops/alert_dedupe.py): a finding pages once,
not again every run just because a count moved.

Public repo, public log: the log carries workflow names, slot letters, inventory host
ids and counts only.

Stdlib only. The API is injected (`api(path) -> json`) so tests need no network.
"""

import argparse
import datetime as dt
import json
import math
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
TOLERANCE_MIN = 4      # max distance of a run from its slot time
GRACE_MIN = 3          # slot times this close to now are not expected yet
DEFAULT_WINDOW_MIN = 150


def github_api(token):
    def api(path):
        req = urllib.request.Request(f"{API}/{path.lstrip('/')}", headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "action-servers-dispatch-liveness"})
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


# ── schedule ─────────────────────────────────────────────────────────────────
def parse_minutes(spec):
    """'MM' -> [MM]; 'MM/step' -> [MM, MM+step, ...] below 60 (systemd minute field)."""
    start, _, step = spec.partition("/")
    if not start.isdigit() or not 0 <= int(start) <= 59:
        raise ValueError(f"bad minutes spec: {spec!r}")
    if not step:
        return [int(start)]
    if not step.isdigit() or int(step) < 1:
        raise ValueError(f"bad minutes spec: {spec!r}")
    return list(range(int(start), 60, int(step)))


def parse_schedule(text):
    """-> (hosts {slot: host id}, timers {workflow: {slot: [minutes]}})."""
    hosts, timers = {}, {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].split()
        if not line:
            continue
        if line[0] == "slot" and len(line) == 3:
            hosts[line[1]] = line[2]
        elif line[0] == "timer" and len(line) == 4:
            timers.setdefault(line[1], {})[line[2]] = parse_minutes(line[3])
        else:
            raise ValueError(f"schedule line {n}: cannot parse {raw.strip()!r}")
    return hosts, timers


# ── attribution ──────────────────────────────────────────────────────────────
def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def slot_times(minutes, start, end):
    """Every datetime in (start, end] whose minute is in `minutes` (second 0)."""
    t = start.replace(second=0, microsecond=0)
    out = []
    while t <= end:
        if t > start and t.minute in minutes:
            out.append(t)
        t += dt.timedelta(minutes=1)
    return out


def assess(slots, runs, now, window_min):
    """slots {slot: [minutes]}, runs [created_at datetimes].
    -> {slot: (hits, expected)}: expected = slot times in the window, hits = how many of
    them have a run attributed to them."""
    start = now - dt.timedelta(minutes=window_min)
    due_end = now - dt.timedelta(minutes=GRACE_MIN)
    # Candidate times reach a little past the window on both sides so a run is always
    # compared with its true nearest slot time, not just the ones still expected.
    edge = dt.timedelta(minutes=TOLERANCE_MIN + 1)
    candidates = sorted((t, s) for s, mins in slots.items()
                        for t in slot_times(mins, start - edge, now + edge))
    hit = set()
    for created in runs:
        best = min(candidates, key=lambda c: abs((created - c[0]).total_seconds()),
                   default=None)
        if best and abs((created - best[0]).total_seconds()) <= TOLERANCE_MIN * 60:
            hit.add(best)
    result = {}
    for s, mins in slots.items():
        expected = slot_times(mins, start, due_end)
        result[s] = (sum((t, s) in hit for t in expected), len(expected))
    return result


def silent(hits, expected):
    return expected > 0 and hits < math.ceil(expected / 2)


def verdict(workflow, counts, hosts, window_min):
    """-> (level, text) or None. level is 'WARNING' or 'NOTICE'."""
    quiet = sorted(s for s, (h, e) in counts.items() if silent(h, e))
    if not quiet:
        return None
    detail = ", ".join(f"slot {s} ({hosts.get(s, '?')}) {h}/{e}"
                       for s, (h, e) in sorted(counts.items()))
    if len(quiet) == len(counts):
        return ("WARNING", f"WARNING - scheduler degraded: dispatch timers not firing for "
                f"{workflow} ({detail} dispatches in the last {window_min} min). It is back "
                f"on GitHub cron (~6 runs/day). Check the timers on both CI boxes "
                f"(RUNBOOK: fleet dispatch timers).")
    boxes = ", ".join(f"slot {s} ({hosts.get(s, '?')})" for s in quiet)
    return ("NOTICE", f"NOTICE - dispatch {boxes} not firing for {workflow} ({detail} "
            f"dispatches in the last {window_min} min): cadence at half rate. That box "
            f"is down or its fleet-dispatch timers/token are broken.")


def dispatch_runs(api, repo, workflow, since):
    q = urllib.parse.urlencode({"event": "workflow_dispatch", "per_page": 100,
                                "created": ">=" + since.strftime("%Y-%m-%dT%H:%M:%SZ")})
    data = call(api, f"repos/{repo}/actions/workflows/{workflow}/runs?{q}")
    return [parse_ts(r["created_at"]) for r in data.get("workflow_runs") or []
            if r.get("event") == "workflow_dispatch" and r.get("created_at")]


def check(schedule_text, api, repo, now, window_min, warn, log):
    """-> list of (level, text, dedupe key) findings."""
    hosts, timers = parse_schedule(schedule_text)
    since = now - dt.timedelta(minutes=window_min + TOLERANCE_MIN + 1)
    findings = []
    for workflow, slots in sorted(timers.items()):
        try:
            runs = dispatch_runs(api, repo, workflow, since)
        except ApiError as e:
            warn(f"dispatch liveness: cannot list {workflow} runs ({e}); not checked")
            continue
        counts = assess(slots, runs, now, window_min)
        log(f"{workflow}: " + ", ".join(f"slot {s} ({hosts.get(s, '?')}) {h}/{e}"
                                        for s, (h, e) in sorted(counts.items())))
        v = verdict(workflow, counts, hosts, window_min)
        if v:
            quiet = ",".join(sorted(s for s, (h, e) in counts.items() if silent(h, e)))
            findings.append((v[0], v[1], f"scheduler:{v[0]}:{workflow}:{quiet}"))
            warn(v[1])
    return findings


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check")
    p.add_argument("--schedule", required=True)
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
        warn("dispatch liveness: no GH_TOKEN - not checked.")
        return 0
    findings = check(pathlib.Path(a.schedule).read_text(), github_api(token), a.repo,
                     now_utc(), a.window_min, warn, log)
    if findings:
        pathlib.Path(a.message).write_text("\n\n".join(t for _, t, _ in findings) + "\n")
        if a.keys:
            pathlib.Path(a.keys).write_text("".join(f"{k}\n" for _, _, k in findings))
    else:
        log("dispatch timers firing on every slot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
