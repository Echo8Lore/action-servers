#!/usr/bin/env python3
"""Tests for ops/dispatch_liveness.py (stdlib unittest, no network).

Run from the repo root:  python3 -m unittest discover -s ops -v

What they pin: the schedule file parses (and matches the fleet-dispatch.sh allowlist);
runs are attributed to the right slot by timing; both boxes up -> silent; one box down
-> a NOTICE naming that box; both down -> the "scheduler degraded" WARNING; a missed
dispatch or two, a manual run, a just-due slot and API failures never page.
"""

import contextlib
import datetime as dt
import io
import pathlib
import re
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dispatch_liveness as dl  # noqa: E402

SCHEDULE = (HERE / "systemd" / "fleet-dispatch.schedule").read_text()
HOSTS, TIMERS = dl.parse_schedule(SCHEDULE)
NOW = dt.datetime(2026, 9, 27, 12, 0, 40, tzinfo=dt.timezone.utc)   # just after slot a
WINDOW = dl.DEFAULT_WINDOW_MIN


def fired(slots, now=NOW, window=WINDOW, lag_s=6, skip=()):
    """created_at datetimes for every scheduled dispatch of `slots` (e.g. 'ab') in the
    window up to now, `lag_s` seconds after the slot time, minus the times in `skip`."""
    out = []
    for s in slots:
        for t in dl.slot_times(WF_MINUTES[s], now - dt.timedelta(minutes=window), now):
            if t.strftime("%H:%M") not in skip:
                out.append(t + dt.timedelta(seconds=lag_s))
    return out


WF_MINUTES = {}   # set per test class


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class Schedule(unittest.TestCase):
    def test_file_parses_to_the_decided_design(self):
        self.assertEqual(HOSTS, {"a": "ovh-staging", "b": "ovh-devops-001"})
        self.assertEqual(TIMERS["runner-health.yml"], {"a": [0], "b": [30]})
        self.assertEqual(TIMERS["queue-watchdog.yml"], {"a": [0, 20, 40], "b": [10, 30, 50]})

    def test_combined_cadence_is_even(self):
        # Both slots together: runner-health every 30 min, queue-watchdog every 10.
        for wf, gap in (("runner-health.yml", 30), ("queue-watchdog.yml", 10)):
            mins = sorted(m for ms in TIMERS[wf].values() for m in ms)
            self.assertEqual(mins, list(range(mins[0], 60, gap)), wf)

    def test_matches_the_dispatch_allowlist(self):
        src = (HERE / "fleet-dispatch.sh").read_text()
        allow = re.search(r"^ALLOWED_WORKFLOWS=\((.*)\)$", src, re.M).group(1).split()
        self.assertEqual(sorted(allow), sorted(TIMERS))

    def test_minutes_spec(self):
        self.assertEqual(dl.parse_minutes("00"), [0])
        self.assertEqual(dl.parse_minutes("10/20"), [10, 30, 50])
        for bad in ("60", "x", "00/0", "00/x", "*"):
            with self.assertRaises(ValueError, msg=bad):
                dl.parse_minutes(bad)

    def test_bad_line_fails_loudly(self):
        with self.assertRaises(ValueError):
            dl.parse_schedule("timer runner-health.yml a\n")


class RunnerHealth(unittest.TestCase):
    WF = "runner-health.yml"

    def setUp(self):
        WF_MINUTES.clear()
        WF_MINUTES.update(TIMERS[self.WF])

    def counts(self, runs, now=NOW):
        return dl.assess(TIMERS[self.WF], runs, now, WINDOW)

    def v(self, runs, now=NOW):
        return dl.verdict(self.WF, self.counts(runs, now), HOSTS, WINDOW)

    def test_window_expects_two_per_slot(self):
        # 150 min at 12:00:40: a at 10:00, 11:00 (12:00 is inside the grace); b at 10:30, 11:30.
        self.assertEqual(self.counts([]), {"a": (0, 2), "b": (0, 2)})

    def test_both_boxes_up_is_quiet(self):
        runs = fired("ab")
        self.assertEqual(self.counts(runs), {"a": (2, 2), "b": (2, 2)})
        self.assertIsNone(self.v(runs))

    def test_one_missed_dispatch_does_not_page(self):
        self.assertIsNone(self.v(fired("ab", skip={"11:30"})))

    def test_box_b_down_is_a_notice_naming_it(self):
        level, text = self.v(fired("a"))
        self.assertEqual(level, "NOTICE")
        self.assertIn("slot b (ovh-devops-001)", text)
        self.assertNotIn("slot a (ovh-staging) not", text)
        self.assertIn("half rate", text)

    def test_box_a_down_is_a_notice_naming_it(self):
        level, text = self.v(fired("b"))
        self.assertEqual(level, "NOTICE")
        self.assertIn("dispatch slot a (ovh-staging) not firing", text)

    def test_both_down_is_the_degraded_warning(self):
        level, text = self.v([])
        self.assertEqual(level, "WARNING")
        self.assertIn("scheduler degraded: dispatch timers not firing", text)
        self.assertIn("slot a (ovh-staging) 0/2", text)
        self.assertIn("slot b (ovh-devops-001) 0/2", text)

    def test_manual_runs_count_for_nobody(self):
        # Dispatches at :15 and :45 are clicks, not timers: still degraded.
        manual = [dt.datetime(2026, 9, 27, h, m, 0, tzinfo=dt.timezone.utc)
                  for h in (10, 11) for m in (15, 45)]
        self.assertEqual(self.counts(manual), {"a": (0, 2), "b": (0, 2)})

    def test_github_lag_within_tolerance_counts(self):
        self.assertEqual(self.counts(fired("ab", lag_s=200)), {"a": (2, 2), "b": (2, 2)})

    def test_just_due_slot_is_not_expected(self):
        # At 12:30:20 the 12:30 dispatch may not exist yet; it must not be counted missing.
        now = dt.datetime(2026, 9, 27, 12, 30, 20, tzinfo=dt.timezone.utc)
        runs = [r for r in fired("ab", now=now) if r < now - dt.timedelta(minutes=1)]
        self.assertIsNone(self.v(runs, now))

    def test_several_runs_for_one_slot_count_once(self):
        runs = fired("a") * 3
        self.assertEqual(self.counts(runs)["a"], (2, 2))


class QueueWatchdog(unittest.TestCase):
    WF = "queue-watchdog.yml"

    def setUp(self):
        WF_MINUTES.clear()
        WF_MINUTES.update(TIMERS[self.WF])

    def counts(self, runs):
        return dl.assess(TIMERS[self.WF], runs, NOW, WINDOW)

    def test_slots_ten_minutes_apart_are_told_apart(self):
        c = self.counts(fired("b"))
        self.assertEqual(c["a"][0], 0)
        self.assertEqual(c["b"][0], c["b"][1])

    def test_a_few_misses_tolerated(self):
        runs = fired("ab", skip={"10:10", "10:30", "11:20"})
        self.assertIsNone(dl.verdict(self.WF, self.counts(runs), HOSTS, WINDOW))

    def test_slot_mostly_missing_is_silent(self):
        # Only 2 of 7 slot-b dispatches seen: under half -> that box is flagged.
        b_only = [r for r in fired("b") if r.hour == 11 and r.minute in (30, 50)]
        level, text = dl.verdict(self.WF, self.counts(fired("a") + b_only), HOSTS, WINDOW)
        self.assertEqual(level, "NOTICE")
        self.assertIn("slot b (ovh-devops-001)", text)


class FakeApi:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, path):
        self.calls.append(path)
        for key, value in self.routes.items():
            if key in path:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"unexpected path {path}")


def runs_json(times, event="workflow_dispatch"):
    return {"workflow_runs": [{"event": event, "created_at": iso(t)} for t in times]}


class Check(unittest.TestCase):
    def run_check(self, routes):
        warns, logs = [], []
        api = FakeApi(routes)
        out = dl.check(SCHEDULE, api, "Echo8Lore/action-servers", NOW, WINDOW,
                       warns.append, logs.append)
        return out, warns, logs, api

    def all_fired(self, wf):
        WF_MINUTES.clear()
        WF_MINUTES.update(TIMERS[wf])
        return fired("ab")

    def test_healthy(self):
        out, warns, logs, api = self.run_check({
            "runner-health.yml": runs_json(self.all_fired("runner-health.yml")),
            "queue-watchdog.yml": runs_json(self.all_fired("queue-watchdog.yml"))})
        self.assertEqual(out, [])
        self.assertEqual(warns, [])
        self.assertTrue(all("event=workflow_dispatch" in p for p in api.calls))
        self.assertTrue(all("created=%3E%3D" in p for p in api.calls))

    def test_nothing_dispatched_is_degraded_for_both(self):
        out, warns, _, _ = self.run_check({"runner-health.yml": runs_json([]),
                                           "queue-watchdog.yml": runs_json([])})
        self.assertEqual([lvl for lvl, _, _ in out], ["WARNING", "WARNING"])
        self.assertEqual([k for _, _, k in out],
                         ["scheduler:WARNING:queue-watchdog.yml:a,b",
                          "scheduler:WARNING:runner-health.yml:a,b"])

    def test_key_names_the_quiet_slot_without_counts(self):
        WF_MINUTES.clear()
        WF_MINUTES.update(TIMERS["runner-health.yml"])
        out, _, _, _ = self.run_check({
            "runner-health.yml": runs_json(fired("a")),
            "queue-watchdog.yml": runs_json(self.all_fired("queue-watchdog.yml"))})
        self.assertEqual([k for _, _, k in out], ["scheduler:NOTICE:runner-health.yml:b"])

    def test_schedule_runs_are_not_dispatches(self):
        rh = self.all_fired("runner-health.yml")
        out, _, _, _ = self.run_check({
            "runner-health.yml": runs_json(rh, event="schedule"),
            "queue-watchdog.yml": runs_json(self.all_fired("queue-watchdog.yml"))})
        self.assertEqual(len(out), 1)
        self.assertIn("runner-health.yml", out[0][1])

    def test_api_failure_warns_and_never_pages(self):
        err = urllib.error.HTTPError("https://api.github.com/x", 502, "bad", {}, None)
        out, warns, _, _ = self.run_check({"runner-health.yml": err,
                                           "queue-watchdog.yml": OSError("down")})
        self.assertEqual(out, [])
        self.assertEqual(len(warns), 2)
        self.assertTrue(all("not checked" in w for w in warns))

    def test_main_without_token_is_quiet(self):
        with tempfile.TemporaryDirectory() as d:
            msg = pathlib.Path(d) / "m"
            sched = pathlib.Path(d) / "s"
            sched.write_text(SCHEDULE)
            buf = io.StringIO()
            with mock.patch.dict("os.environ", {"GH_TOKEN": ""}), \
                    contextlib.redirect_stdout(buf):
                rc = dl.main(["check", "--schedule", str(sched), "--repo", "o/r",
                              "--message", str(msg)])
            self.assertEqual(rc, 0)
            self.assertEqual(msg.read_text(), "")
            self.assertIn("::warning::", buf.getvalue())

    def test_main_writes_the_message(self):
        with tempfile.TemporaryDirectory() as d:
            msg = pathlib.Path(d) / "m"
            keys = pathlib.Path(d) / "k"
            sched = pathlib.Path(d) / "s"
            sched.write_text(SCHEDULE)
            api = FakeApi({"runner-health.yml": runs_json([]),
                           "queue-watchdog.yml": runs_json([])})
            with mock.patch.dict("os.environ", {"GH_TOKEN": "t"}), \
                    mock.patch.object(dl, "github_api", return_value=api), \
                    mock.patch.object(dl, "now_utc", return_value=NOW), \
                    contextlib.redirect_stdout(io.StringIO()):
                rc = dl.main(["check", "--schedule", str(sched), "--repo", "o/r",
                              "--message", str(msg), "--keys", str(keys)])
            self.assertEqual(rc, 0)
            self.assertEqual(len(keys.read_text().splitlines()), 2)
            text = msg.read_text()
            self.assertEqual(text.count("scheduler degraded"), 2)
            self.assertNotIn("\\", text)   # the alert job prints it with printf %b


if __name__ == "__main__":
    unittest.main()
