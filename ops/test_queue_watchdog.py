#!/usr/bin/env python3
"""Tests for ops/queue_watchdog.py (stdlib unittest, no network).

Run from the repo root:  python3 -m unittest discover -s ops -v
"""

import contextlib
import datetime as dt
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import queue_watchdog as qw  # noqa: E402

NOW = dt.datetime(2026, 9, 27, 12, 0, tzinfo=dt.timezone.utc)


def ago(minutes):
    return (NOW - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def runner(name, labels, status="online", busy=False):
    return {"name": name, "status": status, "busy": busy, "labels": [{"name": l} for l in labels]}


def http_error(code):
    return urllib.error.HTTPError("https://api.github.com/x", code, "err", {}, None)


class FakeApi:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, path):
        self.calls.append(path)
        for prefix, resp in self.routes.items():
            if path.startswith(prefix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise http_error(404)


class Classify(unittest.TestCase):
    RUNNERS = [runner("linux-1", ["self-hosted", "Linux", "X64", "fieldkit"], busy=True),
               runner("linux-2", ["self-hosted", "Linux", "X64"], status="offline")]

    def test_hosted(self):
        self.assertEqual(qw.classify(["windows-latest"], self.RUNNERS), "hosted")

    def test_no_runner_is_the_v014_case(self):
        self.assertEqual(qw.classify(["self-hosted", "fieldkit-win"], self.RUNNERS), "no_runner")

    def test_busy_and_case_insensitive(self):
        self.assertEqual(qw.classify(["Self-Hosted", "linux", "FIELDKIT"], self.RUNNERS), "busy")

    def test_offline(self):
        rs = [runner("a", ["self-hosted", "gpu"], status="offline")]
        self.assertEqual(qw.classify(["self-hosted", "gpu"], rs), "runners_offline")

    def test_idle_match(self):
        rs = [runner("a", ["self-hosted", "Linux"])]
        self.assertEqual(qw.classify(["self-hosted"], rs), "idle_match")

    def test_unknown_when_a_runner_list_was_unreadable(self):
        # Partial visibility must not be reported as a missing runner...
        self.assertEqual(qw.classify(["self-hosted", "x"], [], complete=False), "unknown")
        # ...but a match in what IS visible still classifies normally.
        self.assertEqual(qw.classify(["self-hosted", "fieldkit"], self.RUNNERS, complete=False), "busy")


class Dedupe(unittest.TestCase):
    def job(self, age, cls="no_runner", jid=1):
        return {"repo": "o/r", "job_id": jid, "age_min": age, "class": cls}

    def due(self, jobs, st):
        alert, _, st = qw.due(jobs, st, 30, 360, busy_threshold=120)
        return alert, st

    def test_no_runner_lifecycle(self):
        alert, st = self.due([self.job(10)], {})
        self.assertEqual((alert, st), ([], {}))                       # under threshold
        alert, st = self.due([self.job(31)], st)
        self.assertEqual(len(alert), 1)                               # pages at 30 min
        self.assertEqual(st, {"o/r#1": {"no_runner": [30]}})
        alert, st = self.due([self.job(90)], st)
        self.assertEqual(alert, [])                                   # no re-page
        alert, st = self.due([self.job(361)], st)
        self.assertEqual(len(alert), 1)                               # 6h reminder
        alert, st = self.due([self.job(900)], st)
        self.assertEqual(alert, [])                                   # and no more
        alert, st = self.due([], st)
        self.assertEqual(st, {})                                      # gone -> pruned

    def test_busy_waits_two_hours(self):
        alert, st = self.due([self.job(31, "busy")], {})
        self.assertEqual((alert, st), ([], {}))                       # busy at 31: silent
        alert, st = self.due([self.job(119, "busy")], st)
        self.assertEqual(alert, [])
        alert, st = self.due([self.job(121, "busy")], st)
        self.assertEqual(len(alert), 1)                               # pages at 2h
        self.assertEqual(st, {"o/r#1": {"waiting": [120]}})
        alert, st = self.due([self.job(200, "busy")], st)
        self.assertEqual(alert, [])                                   # once
        alert, st = self.due([self.job(361, "busy")], st)
        self.assertEqual(len(alert), 1)                               # and again at 6h

    def test_idle_match_and_hosted_follow_the_busy_rule(self):
        for cls in ("idle_match", "hosted"):
            self.assertEqual(self.due([self.job(60, cls)], {})[0], [], cls)
            self.assertEqual(len(self.due([self.job(121, cls)], {})[0]), 1, cls)

    def test_missing_classes_keep_30_min(self):
        for cls in ("no_runner", "runners_offline", "unknown"):
            self.assertEqual(len(self.due([self.job(31, cls)], {})[0]), 1, cls)

    def test_class_change_is_new_information(self):
        _, st = self.due([self.job(121, "busy")], {})
        alert, st = self.due([self.job(130, "runners_offline")], st)  # its runner went offline
        self.assertEqual(len(alert), 1)
        self.assertEqual(st, {"o/r#1": {"waiting": [120], "runners_offline": [30]}})
        alert, st = self.due([self.job(140, "runners_offline")], st)
        self.assertEqual(alert, [])

    def test_busy_idle_flap_pages_once_per_threshold(self):
        # One runner toggling busy <-> idle between scans is one waiting job, not two.
        alert, st = self.due([self.job(121, "busy")], {})
        self.assertEqual(len(alert), 1)
        for age, cls in ((130, "idle_match"), (140, "busy"), (150, "hosted"), (160, "idle_match")):
            alert, st = self.due([self.job(age, cls)], st)
            self.assertEqual(alert, [], (age, cls))
        alert, st = self.due([self.job(361, "idle_match")], st)
        self.assertEqual(len(alert), 1)                               # 6h, once
        alert, st = self.due([self.job(370, "busy")], st)
        self.assertEqual(alert, [])
        self.assertEqual(st, {"o/r#1": {"waiting": [120, 360]}})

    def test_message_keeps_the_precise_class(self):
        j = self.job(121, "idle_match")
        j.update(workflow="CI", job="t", labels=["self-hosted"], run_url="u")
        self.assertIn("online and idle", qw.message([j], 30))

    def test_legacy_per_class_state_migrates_into_buckets(self):
        legacy = {"o/r#1": {"busy": [120], "idle_match": [360]}, "o/r#2": {"no_runner": [30]}}
        alert, st = self.due([self.job(370, "busy"), self.job(40, "no_runner", jid=2)], legacy)
        self.assertEqual(alert, [])                                   # both already paged
        self.assertEqual(st, {"o/r#1": {"waiting": [120, 360]}, "o/r#2": {"no_runner": [30]}})

    def test_junk_inside_the_current_class_never_crashes(self):
        for junk in (["x", None, True, 30, {"a": 1}], {"a": 1}, "busy", None, [None], [120.0, False]):
            for bucket_key in ("busy", "waiting", "no_runner"):
                state = {"o/r#1": {bucket_key: junk, 5: [1], "ok": "x"}, 7: {}, "o/r#9": ["legacy", 1]}
                for cls, age in (("busy", 121), ("no_runner", 31)):
                    alert, st = self.due([self.job(age, cls)], state)
                    json.dumps(st)
                    b = qw.bucket(cls)
                    kept = {t for t in (junk if isinstance(junk, list) else [])
                            if isinstance(t, (int, float)) and not isinstance(t, bool)}
                    first = 120 if cls == "busy" else 30
                    already = qw.bucket(bucket_key) == b and first in kept
                    self.assertEqual(len(alert), 0 if already else 1, (junk, bucket_key, cls))
                    self.assertIn(first, st["o/r#1"][b])

    def test_normalize_state_drops_junk(self):
        self.assertEqual(qw.normalize_state({"a": {"busy": [True, "x", 30.0]}, "b": 5, 3: {}}),
                         {"a": {"waiting": [30.0]}})
        self.assertEqual(qw.normalize_state(["not", "a", "dict"]), {})

    def test_first_seen_past_6h_alerts_once(self):
        alert, st = self.due([self.job(400)], {})
        self.assertEqual(len(alert), 1)
        self.assertEqual(st, {"o/r#1": {"no_runner": [30, 360]}})
        self.assertEqual(self.due([self.job(401)], st)[0], [])

    def test_legacy_or_junk_state_entries_are_ignored(self):
        alert, st = self.due([self.job(31)], {"o/r#1": [30], "x#2": "junk"})
        self.assertEqual(len(alert), 1)
        self.assertEqual(st, {"o/r#1": {"no_runner": [30]}})


class Minutes(unittest.TestCase):
    def test_accepts_int_and_float(self):
        self.assertEqual(qw.minutes("30"), 30)
        self.assertEqual(qw.minutes("45.5"), 45.5)

    def test_rejects_junk(self):
        import argparse
        for bad in ("abc", "", "0", "-5", "inf", "nan"):
            with self.assertRaises(argparse.ArgumentTypeError, msg=bad):
                qw.minutes(bad)

    def test_cli_rejects_cleanly(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            qw.main(["scan", "--inventory", "i", "--state", "s", "--new-state", "n",
                     "--message", "m", "--threshold-min", "soon"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("not a number of minutes: 'soon'", err.getvalue())


class Transport(unittest.TestCase):
    def test_connection_reset_mid_read_is_an_api_error(self):
        import http.client
        for exc in (ConnectionResetError("reset"), http.client.RemoteDisconnected("gone"), TimeoutError()):
            def api(path, exc=exc):
                raise exc
            with self.assertRaises(qw.ApiError):
                qw.call(api, "x")


def fleet_api():
    inv = {"runners": [
        {"scope": "org", "target": "Org"},
        {"scope": "repo", "target": "User/FieldKit"}],
        "watch_repos": ["User/wh40k"]}
    routes = {
        "orgs/Org/repos": [{"full_name": "Org/app", "archived": False},
                           {"full_name": "Org/old", "archived": True}],
        # FieldKit: the v0.1.4 case -- a queued run whose job wants a label nobody has.
        "repos/User/FieldKit/actions/runs?status=queued": {"workflow_runs": [
            {"id": 11, "name": "Desktop Build (Windows)", "html_url": "https://github.com/User/FieldKit/actions/runs/11"}]},
        "repos/User/FieldKit/actions/runs?status=in_progress": {"workflow_runs": []},
        "repos/User/FieldKit/actions/runs/11/jobs": {"jobs": [
            {"id": 111, "name": "build", "status": "queued", "created_at": ago(45),
             "labels": ["self-hosted", "fieldkit-win"]}]},
        "repos/User/FieldKit/actions/runners": {"runners": [
            runner("personal-fieldkit-01", ["self-hosted", "Linux", "X64", "fieldkit"])]},
        # Org/app: an in-progress run with one job still queued behind a busy org runner,
        # and one fresh queued job under the threshold.
        "repos/Org/app/actions/runs?status=queued": {"workflow_runs": []},
        "repos/Org/app/actions/runs?status=in_progress": {"workflow_runs": [
            {"id": 22, "name": "CI", "html_url": "https://github.com/Org/app/actions/runs/22"}]},
        "repos/Org/app/actions/runs/22/jobs": {"jobs": [
            {"id": 221, "name": "test (a)", "status": "in_progress", "created_at": ago(50), "labels": ["self-hosted"]},
            {"id": 222, "name": "test (b)", "status": "queued", "created_at": ago(130), "labels": ["self-hosted", "Linux"]},
            {"id": 224, "name": "test (c)", "status": "queued", "created_at": ago(40), "labels": ["self-hosted", "Linux"]},
            {"id": 223, "name": "lint", "status": "queued", "created_at": ago(5), "labels": ["self-hosted"]}]},
        "repos/Org/app/actions/runners": {"runners": []},
        "orgs/Org/actions/runners": {"runners": [runner("org-runner-02", ["self-hosted", "Linux", "X64"], busy=True)]},
        "users/Org": {"type": "Organization"},
        "users/User": {"type": "User"},
        # wh40k: listing runs fails -> warning, scan carries on.
        "repos/User/wh40k/actions/runs": http_error(500),
    }
    return inv, FakeApi(routes)


class Scan(unittest.TestCase):
    def run_scan(self, state=None):
        inv, api = fleet_api()
        warns, logs = [], []
        alerts, st, stuck = qw.scan(inv, state or {}, api, NOW, 30, 360, warns.append, logs.append)
        return alerts, st, stuck, warns, logs, api

    def test_repos_derived_from_inventory(self):
        inv, api = fleet_api()
        self.assertEqual(qw.watched_repos(inv, api, lambda m: None),
                         ["Org/app", "User/FieldKit", "User/wh40k"])      # archived skipped

    def test_finds_and_classifies(self):
        alerts, st, stuck, warns, _, _ = self.run_scan()
        got = {(j["repo"], j["job_id"]): j["class"] for j in stuck}
        self.assertEqual(got, {("User/FieldKit", 111): "no_runner", ("Org/app", 222): "busy"})
        self.assertEqual(len(alerts), 2)
        self.assertEqual(set(st), {"User/FieldKit#111", "Org/app#222"})   # busy 224 @40m: silent
        self.assertTrue(any("User/wh40k" in w for w in warns))

    def test_second_scan_does_not_repage(self):
        _, st, _, _, _, _ = self.run_scan()
        alerts, st2, stuck, _, logs, _ = self.run_scan(st)
        self.assertEqual(alerts, [])
        self.assertEqual(len(stuck), 2)
        self.assertTrue(all("already alerted" in l for l in logs if l.startswith("stuck:")))

    def test_message_has_the_facts(self):
        alerts, _, _, _, _, _ = self.run_scan()
        msg = qw.message(alerts, 30)
        for s in ("User/FieldKit - Desktop Build (Windows) / build", "self-hosted, fieldkit-win",
                  "queued: 45m", "queued: 2h10m", "NO registered runner", "30m when no runner", "2h00m when runners", "https://github.com/User/FieldKit/actions/runs/11",
                  "Org/app - CI / test (b)", "online but busy"):
            self.assertIn(s, msg)

    def test_public_log_omits_names_and_urls(self):
        _, _, _, _, logs, _ = self.run_scan()
        text = "\n".join(logs)
        for s in ("Desktop Build", "https://github.com", "test (b)"):
            self.assertNotIn(s, text)
        self.assertIn("labels=[self-hosted, fieldkit-win]", text)

    def test_user_owned_repo_skips_org_runner_list(self):
        _, _, _, _, _, api = self.run_scan()
        self.assertNotIn("orgs/User/actions/runners?per_page=100", api.calls)
        self.assertIn("orgs/Org/actions/runners?per_page=100", api.calls)

    def test_unreadable_org_runner_list_gives_unknown_not_no_runner(self):
        inv, api = fleet_api()
        api.routes["orgs/Org/actions/runners"] = http_error(403)
        api.routes["repos/Org/app/actions/runs/22/jobs"] = {"jobs": [
            {"id": 222, "name": "t", "status": "queued", "created_at": ago(40), "labels": ["self-hosted", "Linux"]}]}
        _, _, stuck = qw.scan(inv, {}, api, NOW, 30, 360, lambda m: None, lambda m: None)
        self.assertEqual({j["job_id"]: j["class"] for j in stuck}, {111: "no_runner", 222: "unknown"})


class Main(unittest.TestCase):
    def test_missing_token_warns_and_keeps_state(self):
        with tempfile.TemporaryDirectory() as t:
            t = pathlib.Path(t)
            (t / "state.json").write_text(json.dumps({"o/r#1": {"no_runner": [30]}}))
            (t / "inv.json").write_text("{}")
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"GH_TOKEN": ""}), contextlib.redirect_stdout(out):
                rc = qw.main(["scan", "--inventory", str(t / "inv.json"), "--state", str(t / "state.json"),
                              "--new-state", str(t / "new.json"), "--message", str(t / "msg.txt")])
            self.assertEqual(rc, 0)
            self.assertIn("::warning::No RUNNER_HEALTH_PAT", out.getvalue())
            self.assertEqual(json.loads((t / "new.json").read_text()), {"o/r#1": {"no_runner": [30]}})
            self.assertEqual((t / "msg.txt").read_text(), "")

    def test_a_crashing_scan_leaves_the_saved_state_untouched(self):
        # The workflow saves .queue-watchdog/state.json with if: always(); the scan
        # must never write to it, so a crash can at worst keep yesterday's state.
        with tempfile.TemporaryDirectory() as t:
            t = pathlib.Path(t)
            before = json.dumps({"o/r#1": {"waiting": [120]}})
            (t / "state.json").write_text(before)
            (t / "inv.json").write_text(json.dumps({"watch_repos": ["o/r"]}))

            def boom(tok):
                def api(path):
                    raise RuntimeError("unexpected")
                return api
            with mock.patch.dict(os.environ, {"GH_TOKEN": "x"}), \
                    mock.patch.object(qw, "github_api", boom), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError):
                    qw.main(["scan", "--inventory", str(t / "inv.json"), "--state", str(t / "state.json"),
                             "--new-state", str(t / "new.json"), "--message", str(t / "msg.txt")])
            self.assertEqual((t / "state.json").read_text(), before)
            self.assertFalse((t / "new.json").exists())

    def test_poisoned_state_file_does_not_crash_the_scan(self):
        inv, api = fleet_api()
        with tempfile.TemporaryDirectory() as t:
            t = pathlib.Path(t)
            (t / "state.json").write_text(json.dumps(
                {"User/FieldKit#111": {"no_runner": ["x", None, {"a": 1}]}, "Org/app#222": {"busy": {"a": 1}}}))
            (t / "inv.json").write_text(json.dumps(inv))
            with mock.patch.dict(os.environ, {"GH_TOKEN": "x"}), \
                    mock.patch.object(qw, "github_api", lambda tok: api), \
                    mock.patch.object(qw, "now_utc", lambda: NOW), \
                    contextlib.redirect_stdout(io.StringIO()):
                rc = qw.main(["scan", "--inventory", str(t / "inv.json"), "--state", str(t / "state.json"),
                              "--new-state", str(t / "new.json"), "--message", str(t / "msg.txt")])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads((t / "new.json").read_text()),
                             {"User/FieldKit#111": {"no_runner": [30]}, "Org/app#222": {"waiting": [120]}})
            self.assertIn("Queue watchdog: 2 job(s)", (t / "msg.txt").read_text())

    def test_corrupt_state_is_treated_as_empty(self):
        inv, api = fleet_api()
        with tempfile.TemporaryDirectory() as t:
            t = pathlib.Path(t)
            (t / "state.json").write_text("not json")
            (t / "inv.json").write_text(json.dumps(inv))
            with mock.patch.dict(os.environ, {"GH_TOKEN": "x"}), \
                    mock.patch.object(qw, "github_api", lambda tok: api), \
                    mock.patch.object(qw, "now_utc", lambda: NOW), \
                    contextlib.redirect_stdout(io.StringIO()):
                rc = qw.main(["scan", "--inventory", str(t / "inv.json"), "--state", str(t / "state.json"),
                              "--new-state", str(t / "new.json"), "--message", str(t / "msg.txt")])
            self.assertEqual(rc, 0)
            self.assertIn("Queue watchdog: 2 job(s)", (t / "msg.txt").read_text())


if __name__ == "__main__":
    unittest.main()
