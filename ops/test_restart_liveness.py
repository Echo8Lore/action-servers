#!/usr/bin/env python3
"""Tests for ops/restart_liveness.py (stdlib unittest, no network).

Run from the repo root:  python3 -m unittest discover -s ops -v

What they pin (OPS-48): a failed auto-restart run raises exactly one CRITICAL
restart-path-failing:<runner> condition, mapped to its runner through the run's
JOBS (the restart job's name carries the target); manual dispatches, old runs
and API failures never page; counts and URLs stay out of the keys; an
unmappable run falls back to restart-path-failing:? like offline:?.
"""

import datetime as dt
import pathlib
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import restart_liveness as rl  # noqa: E402

RUNNER_RESTART = HERE.parent / ".github" / "workflows" / "runner-restart.yml"
NOW = dt.datetime(2026, 11, 3, 12, 0, tzinfo=dt.timezone.utc)
WINDOW = rl.DEFAULT_WINDOW_MIN     # 360 -> the window opens at 06:00Z
SINCE = (NOW - dt.timedelta(minutes=WINDOW)).strftime("%Y-%m-%dT%H:%M:%SZ")
# The exact runs query the helper builds (urlencode'd, like the code does it).
RUNS_Q = ("repos/o/r/actions/workflows/runner-restart.yml/runs?"
          + urllib.parse.urlencode({"status": "failure", "created": ">=" + SINCE,
                                    "per_page": 100}))


def run(rid, event="workflow_call", conclusion="failure",
        created="2026-11-03T09:30:00Z", url=None):
    return {"id": rid, "event": event, "conclusion": conclusion, "created_at": created,
            "html_url": url or f"https://x/o/r/actions/runs/{rid}"}


def jobs(*names):
    return {"jobs": [{"name": n} for n in names]}


def jobs_q(rid):
    return f"repos/o/r/actions/runs/{rid}/jobs?per_page=100"


class Stub:
    """api(path) -> json from a dict of exact paths. A test that fetches an
    unintended path fails loudly (AssertionError), not silently."""

    def __init__(self, pages):
        self.pages = pages
        self.fetched = []

    def __call__(self, path):
        self.fetched.append(path)
        if path not in self.pages:
            raise AssertionError(f"unexpected API path: {path}")
        return self.pages[path]


class Failing:
    """api(path) -> raises HTTPError on every path matching `on` (substring)."""

    def __init__(self, on, pages=None):
        self.on, self.pages = on, pages or {}
        self.fetched = []

    def __call__(self, path):
        self.fetched.append(path)
        if self.on in path:
            raise urllib.error.HTTPError(path, 403, "Forbidden", {}, None)
        if path in self.pages:
            return self.pages[path]
        raise AssertionError(f"unexpected API path: {path}")


class Check(unittest.TestCase):
    def check(self, api, **kw):
        warns = []
        with mock.patch("builtins.print"):
            out = rl.check(api, kw.get("repo", "o/r"), kw.get("now", NOW),
                           kw.get("window_min", WINDOW), warns.append, print)
        return out, warns

    def test_failed_run_maps_to_runner_via_its_jobs(self):
        api = Stub({RUNS_Q: {"workflow_runs": [run(1)]},
                    jobs_q(1): jobs("restart-runner (r-alpha)")})
        out, warns = self.check(api)
        self.assertEqual(len(out), 1)
        text, key = out[0]
        self.assertEqual(key, "restart-path-failing:r-alpha")
        self.assertIn("CRITICAL - restart path failing", text)
        self.assertIn("1 auto-restart run(s) for r-alpha failed in the last 6 h", text)
        self.assertIn("https://x/o/r/actions/runs/1", text)   # the link (OPS-75)
        self.assertIn("Action:", text)
        # The runs query and the per-run jobs query are both what the workflow
        # comment says they are: failed runs first, then jobs for the target.
        self.assertIn(RUNS_Q, api.fetched)
        self.assertIn(jobs_q(1), api.fetched)

    def test_counts_stay_out_of_the_keys(self):
        # Two failures for one runner: the count and the latest URL move, the
        # key must not (a deduped condition, not a page per failed run).
        api = Stub({RUNS_Q: {"workflow_runs": [run(1, created="2026-11-03T07:00:00Z"),
                                               run(2, created="2026-11-03T09:30:00Z")]},
                    jobs_q(1): jobs("restart-runner (r-alpha)"),
                    jobs_q(2): jobs("restart-runner (r-alpha)")})
        out, _ = self.check(api)
        self.assertEqual([k for _, k in out], ["restart-path-failing:r-alpha"])
        self.assertIn("2 auto-restart run(s) for r-alpha", out[0][0])
        self.assertIn("https://x/o/r/actions/runs/2", out[0][0])   # latest

    def test_two_runners_are_two_conditions(self):
        api = Stub({RUNS_Q: {"workflow_runs": [run(2), run(1)]},
                    jobs_q(1): jobs("restart-runner (r-alpha)"),
                    jobs_q(2): jobs("restart-runner (r-beta)")})
        out, _ = self.check(api)
        self.assertEqual([k for _, k in out],
                         ["restart-path-failing:r-alpha", "restart-path-failing:r-beta"])

    def test_manual_dispatch_never_counts(self):
        # A manual restart pages every outcome by itself (OPS-47) -- including
        # the deliberate probe dispatches this workflow is tested with -- and
        # must not look like a broken auto-restart. Its jobs are never fetched.
        api = Stub({RUNS_Q: {"workflow_runs": [run(9, event="workflow_dispatch")]}})
        out, _ = self.check(api)
        self.assertEqual(out, [])
        self.assertFalse(any("/jobs" in p for p in api.fetched))

    def test_old_runs_age_out_of_the_window(self):
        api = Stub({RUNS_Q: {"workflow_runs": [run(1, created="2026-11-03T09:30:00Z"),
                                               run(2, created="2026-11-03T05:59:00Z")]},
                    jobs_q(1): jobs("restart-runner (r-alpha)")})
        out, _ = self.check(api)
        self.assertEqual([k for _, k in out], ["restart-path-failing:r-alpha"])
        self.assertNotIn(jobs_q(2), api.fetched)

    def test_runs_api_error_never_pages(self):
        out, warns = self.check(Failing("runs?"))
        self.assertEqual(out, [])
        self.assertTrue(any("not checked" in w for w in warns))

    def test_unmappable_jobs_fall_back_to_unknown_runner(self):
        # A run whose jobs do not name the target (pre-OPS-48 runs, a renamed
        # job) still pages, as restart-path-failing:? -- like offline:?.
        api = Stub({RUNS_Q: {"workflow_runs": [run(1)]},
                    jobs_q(1): jobs("restart-runner")})
        out, warns = self.check(api)
        self.assertEqual([k for _, k in out], ["restart-path-failing:?"])
        self.assertIn("do not say which", out[0][0])

    def test_jobs_api_error_counts_as_unknown_runner(self):
        api = Failing("/jobs", pages={RUNS_Q: {"workflow_runs": [run(1)]}})
        out, warns = self.check(api)
        self.assertEqual([k for _, k in out], ["restart-path-failing:?"])
        self.assertTrue(any("cannot list run" in w for w in warns))

    def test_success_conclusion_never_counts(self):
        api = Stub({RUNS_Q: {"workflow_runs": [run(1, conclusion="success")]}})
        out, _ = self.check(api)
        self.assertEqual(out, [])


class JobTarget(unittest.TestCase):
    def test_extracts_runner_from_job_name(self):
        for name, want in (("restart-runner (r-alpha)", "r-alpha"),
                           ("restart-runner (personal-fieldkit-01)", "personal-fieldkit-01"),
                           ("restart-runner (a b)", "a b")):
            self.assertEqual(rl.job_target(name), want, name)

    def test_rejects_names_that_do_not_say(self):
        for name in ("restart-runner", "restart-runner ()", "", None,
                     "xrestart-runner (a)", "restart-runner (a"):
            self.assertIsNone(rl.job_target(name), name)

    def test_matches_the_workflow_job_name(self):
        # The extractor and the workflow must agree: the job's name is the only
        # API-visible place a runner-restart run says which runner it targeted.
        self.assertIn("name: restart-runner (${{ inputs.runner_name }})",
                      RUNNER_RESTART.read_text())


class Main(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, api, token="t"):
        env = {"GH_TOKEN": token} if token else {}
        with mock.patch.dict("os.environ", env, clear=True), \
                mock.patch.object(rl, "github_api", return_value=api), \
                mock.patch.object(rl, "now_utc", return_value=NOW), \
                mock.patch("builtins.print"):
            rc = rl.main(["check", "--repo", "o/r", "--message", str(self.d / "m"),
                          "--keys", str(self.d / "k")])
        return rc, (self.d / "m").read_text(), (self.d / "k").read_text()

    def test_main_writes_message_and_keys(self):
        api = Stub({RUNS_Q: {"workflow_runs": [run(1)]},
                    jobs_q(1): jobs("restart-runner (r-alpha)")})
        rc, msg, keys = self.run_main(api)
        self.assertEqual(rc, 0)
        self.assertIn("restart path failing", msg)
        self.assertIn("r-alpha", msg)
        self.assertEqual(keys, "restart-path-failing:r-alpha\n")

    def test_main_without_token_warns_and_writes_nothing(self):
        rc, msg, keys = self.run_main(Stub({}), token="")
        self.assertEqual((rc, msg, keys), (0, "", ""))

    def test_main_healthy_writes_nothing(self):
        api = Stub({RUNS_Q: {"workflow_runs": []}})
        rc, msg, keys = self.run_main(api)
        self.assertEqual((rc, msg, keys), (0, "", ""))


if __name__ == "__main__":
    unittest.main()