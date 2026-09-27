#!/usr/bin/env python3
"""alert_dedupe.py -- decide whether runner-health's alert pages, from condition KEYS.

At the dispatch-timer cadence (every 30 min, OPS-23) an undeduped alert would page the
same incident ~48x a day. runner-health.yml's alert job builds its message as before,
then asks this helper whether to send it:

  alert_dedupe.py decide --state STATE.json --new-state NEW.json \
      --message MSG.txt --out SEND.txt --run-url URL \
      [--any-offline true] [--offline-runners JSON] [--disk true] \
      [--stale-count N] [--stale-summary TEXT] [--scheduler-keys TEXT] \
      [--incomplete true] [--test-alert true] [--repeat-min 360]

The fingerprint is sha256 over the SORTED condition keys, never the prose, so wording,
counts and timestamps in the message don't re-page:
  offline:<runner>                                one per offline runner
  disk                                            any check-disk leg failed (the matrix
                                                  gives the alert job no per-host result)
  stale:<summary line>                            one per listed stale run (repo, name,
                                                  start time: stable while it is stuck)
  scheduler:<level>:<workflow>:<quiet slots>      from dispatch_liveness.py --keys (the
                                                  h/e counts are not part of it)

Decision:
  keys, fingerprint changed (or never paged)   -> send the message; state = {fp, now}
  keys, same fingerprint, >= --repeat-min old  -> send it again, marked REPEAT
  keys, same fingerprint, younger              -> suppress; state unchanged
  no keys, last page had a fingerprint         -> send one "RESOLVED - fleet healthy";
                                                  state cleared
  no keys, nothing paged                       -> nothing
  --incomplete (a check job failed, so "no keys" may just mean "not checked")
                                               -> never RESOLVED; state kept
  --test-alert                                 -> a message with keys is sent regardless
                                                  (the run's purpose is to prove paging)

SEND.txt is written empty when nothing should be sent. STATE is only read; the new
state goes to NEW.json, and the workflow adopts it only after Telegram accepted the
message, so a failed send pages again next run.

Stdlib only.
"""

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import sys

REPEAT_MIN = 360
RESOLVED = "RESOLVED - fleet healthy: every condition from the last page has cleared."


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def truthy(s):
    return str(s).strip().lower() == "true"


def condition_keys(any_offline="", offline_runners="", disk="", stale_count="",
                   stale_summary="", scheduler_keys=""):
    keys = set()
    if truthy(any_offline):
        try:
            names = json.loads(offline_runners or "[]")
            if not isinstance(names, list):
                raise ValueError
            keys |= {f"offline:{n}" for n in names if isinstance(n, str) and n}
        except ValueError:
            pass
        if not any(k.startswith("offline:") for k in keys):
            keys.add("offline:?")
    if truthy(disk):
        keys.add("disk")
    if str(stale_count).strip() not in ("", "0"):
        # The summary joins lines with a literal "\n" (jq join("\\n")); accept both.
        lines = [ln.strip() for part in (stale_summary or "").split("\\n")
                 for ln in part.splitlines()]
        lines = [ln for ln in lines if ln]
        keys |= {f"stale:{ln}" for ln in lines} or {f"stale:count={stale_count}"}
    for ln in (scheduler_keys or "").splitlines():
        if ln.strip().startswith("scheduler:"):
            keys.add(ln.strip())
    return sorted(keys)


def fingerprint(keys):
    return hashlib.sha256("\n".join(sorted(keys)).encode()).hexdigest() if keys else ""


def normalize_state(raw):
    if not isinstance(raw, dict):
        return {"fingerprint": "", "paged_at": None}
    fp = raw.get("fingerprint") if isinstance(raw.get("fingerprint"), str) else ""
    paged = raw.get("paged_at")
    try:
        dt.datetime.fromisoformat(str(paged).replace("Z", "+00:00"))
    except ValueError:
        paged = None
    return {"fingerprint": fp, "paged_at": paged if fp else None}


def decide(state, keys, message, now, run_url="", repeat_min=REPEAT_MIN,
           test_alert=False, incomplete=False):
    """-> (action, text to send or "", new state). action is one of
    page | repeat | suppress | resolved | none."""
    state = normalize_state(state)
    fp = fingerprint(keys)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if fp:
        if test_alert or fp != state["fingerprint"] or not state["paged_at"]:
            return "page", message, {"fingerprint": fp, "paged_at": stamp}
        paged = dt.datetime.fromisoformat(state["paged_at"].replace("Z", "+00:00"))
        if now - paged >= dt.timedelta(minutes=repeat_min):
            hours = int((now - paged).total_seconds() // 3600)
            return ("repeat", f"REPEAT - unchanged for {hours} h, still unresolved\n\n{message}",
                    {"fingerprint": fp, "paged_at": stamp})
        return "suppress", "", state
    if state["fingerprint"] and not incomplete:
        text = f"Runner Health Monitor\n\n{RESOLVED}\n" + (f"{run_url}\n" if run_url else "")
        return "resolved", text, {"fingerprint": "", "paged_at": None}
    return "none", "", state


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("decide")
    p.add_argument("--state", required=True)
    p.add_argument("--new-state", required=True)
    p.add_argument("--message", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--run-url", default="")
    p.add_argument("--any-offline", default="")
    p.add_argument("--offline-runners", default="")
    p.add_argument("--disk", default="")
    p.add_argument("--stale-count", default="")
    p.add_argument("--stale-summary", default="")
    p.add_argument("--scheduler-keys", default="")
    p.add_argument("--incomplete", default="")
    p.add_argument("--test-alert", default="")
    p.add_argument("--repeat-min", type=int, default=REPEAT_MIN)
    a = ap.parse_args(argv)

    try:
        state = json.loads(pathlib.Path(a.state).read_text())
    except (OSError, ValueError):
        state = {}
    try:
        message = pathlib.Path(a.message).read_text()
    except OSError:
        message = ""
    keys = condition_keys(a.any_offline, a.offline_runners, a.disk, a.stale_count,
                          a.stale_summary, a.scheduler_keys)
    action, text, new_state = decide(state, keys, message, now_utc(), a.run_url,
                                     a.repeat_min, truthy(a.test_alert), truthy(a.incomplete))
    pathlib.Path(a.out).write_text(text)
    pathlib.Path(a.new_state).write_text(json.dumps(new_state, sort_keys=True))
    # Keys can hold runner names and stale-run names; both already appear in this log.
    print(f"alert dedupe: {action} ({len(keys)} condition key(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
