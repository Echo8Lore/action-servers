#!/usr/bin/env python3
"""Tests for ops/restart_liveness.py (stdlib unittest, no network).

Run from the repo root:  python3 -m unittest discover -s ops -v

What they pin (OPS-48, PR #24 rework): the auto-restarts are JOBS inside
runner-health's own runs -- the auto-restart job calls runner-restart.yml at
job level, and a reusable workflow called that way creates no
runner-restart.yml run (the first round of PR #24 looked for workflow_call
runs that do not exist, so its alert could never fire). A failed
`auto-restart (...) / restart-runner (<runner>)` leg raises exactly one deduped
CRITICAL restart-path-failing:<runner> condition; healthy, skipped, cancelled
and non-restart jobs never count, and neither does the run's own conclusion
matter; a jobs-API error marks the check incomplete instead of flapping a '?'
key (no false RESOLVED, no double pages); a leg whose name cannot say its
runner (pre-name era) falls back to restart-path-failing:? like offline:?;
counts and URLs stay out of the keys.

Fixtures are trimmed copies of REAL API payloads, fetched read-only with
gh api on 2026-11-04:
- HEALTH_JOBS / HEALTH_RUN: runner-health run 41826084192 (the full healthy
  9-job set, incl. the real skipped `auto-restart` job and the real failed-leg
  step names from runner-restart dispatch run 36364769843's job
  108748880806).
- The composite auto-restart leg name is CONSTRUCTED per the jobs API's
  `<caller leg> / <called job name>` rendering (PR #24 review): no
  auto-restart leg has run in this repo's history -- all 51 failed
  runner-health runs predate the reusable call, and none contains an
  auto-restart job.
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
RUNNER_HEALTH = HERE.parent / ".github" / "workflows" / "runner-health.yml"
NOW = dt.datetime(2026, 11, 4, 14, 0, tzinfo=dt.timezone.utc)
WINDOW = rl.DEFAULT_WINDOW_MIN     # 360 -> the window opens at 08:00Z
SINCE = (NOW - dt.timedelta(minutes=WINDOW)).strftime("%Y-%m-%dT%H:%M:%SZ")
# The exact runs query the helper builds (urlencode'd, like the code does it).
RUNS_Q = ("repos/o/r/actions/workflows/runner-health.yml/runs?"
          + urllib.parse.urlencode({"created": ">=" + SINCE,
                                    "per_page": rl.PER_PAGE, "page": 1}))


def jobs_q(rid):
    return f"repos/o/r/actions/runs/{rid}/jobs?per_page={rl.PER_PAGE}&page=1"


# The caller-side rendering of one auto-restart matrix leg (runner-health.yml's
# auto-restart job; the matrix value is one offline_detail row from
# check-runners). CONSTRUCTED (see module docstring): no leg has run yet.
CALLER_LEG = ("auto-restart ({'name': 'org-runner-01', "
              "'unit': 'actions.runner.Echo8Lore.org-runner-01.service', "
              "'prefix': 'DEVOPS', 'api': 'orgs/Echo8Lore/actions/runners'})")

# A runner-health run, trimmed copy of the real run 41826084192
# (gh api repos/Echo8Lore/action-servers/actions/runs/41826084192, 2026-11-04).
HEALTH_RUN = {
    "id": 41826084192, "name": "Runner Health Monitor", "event": "schedule",
    "status": "completed", "conclusion": "success",
    "created_at": "2026-11-04T12:04:12Z", "run_started_at": "2026-11-04T12:04:12Z",
    "updated_at": "2026-11-04T12:04:40Z", "workflow_id": 294249259,
    "head_branch": "main", "run_number": 414, "run_attempt": 1,
    "head_sha": "5124843308718e1a1d6bacc5ef1452f2bfd9bc33",
    "path": ".github/workflows/runner-health.yml",
    "html_url": "https://github.com/Echo8Lore/action-servers/actions/runs/41826084192",
}


def run(rid, conclusion="failure", created="2026-11-04T11:00:00Z",
        status="completed", event="schedule"):
    """A runner-health run in the real shape (trimmed to the fields the helper
    reads; created=None removes created_at entirely)."""
    r = {**HEALTH_RUN, "id": rid, "conclusion": conclusion, "status": status,
         "event": event,
         "html_url": f"https://github.com/Echo8Lore/action-servers/actions/runs/{rid}"}
    if created is not None:
        r["created_at"] = created
        r["run_started_at"] = created
    else:
        del r["created_at"]
    return r


def failed_leg(target="org-runner-01", conclusion="failure", run_id=41826084192,
               job_id=108748880806, created="2026-11-04T12:04:26Z"):
    """A failed auto-restart leg in a runner-health run. Base: the REAL failed
    restart job of dispatch run 36364769843 (id, steps and runner_name
    verbatim from
    gh api repos/Echo8Lore/action-servers/actions/runs/36364769843/jobs),
    composed into the composite form the jobs API gives a called job inside
    the caller's run: `auto-restart (...) / restart-runner (<target>)`.
    target=None gives the pre-name called job (`... / restart-runner`),
    the real name of runner-restart.yml's job before OPS-48 gave it one."""
    called = f"restart-runner ({target})" if target is not None else "restart-runner"
    return {
        "id": job_id, "run_id": run_id, "workflow_name": "Runner Health Monitor",
        "head_branch": "main", "status": "completed", "conclusion": conclusion,
        "created_at": created, "started_at": created, "completed_at": created,
        "name": f"{CALLER_LEG} / {called}",
        "html_url": f"https://github.com/Echo8Lore/action-servers/actions/runs/{run_id}/job/{job_id}",
        # Verbatim from the real failed restart job (36364769843).
        "steps": [
            {"name": "Set up job", "status": "completed", "conclusion": "success"},
            {"name": "Run actions/checkout@v5", "status": "completed", "conclusion": "success"},
            {"name": "Restart runner service via SSH", "status": "completed", "conclusion": "failure"},
            {"name": "Verify runner is online", "status": "completed", "conclusion": "success"},
            {"name": "Run actions/checkout@v5", "status": "completed", "conclusion": "success"},
            {"name": "Telegram notification", "status": "completed", "conclusion": "success"},
            {"name": "Post Run actions/checkout@v5", "status": "completed", "conclusion": "success"},
            {"name": "Post Run actions/checkout@v5", "status": "completed", "conclusion": "success"},
            {"name": "Complete job", "status": "completed", "conclusion": "success"},
        ],
        "runner_name": "GitHub Actions 1000021827",   # GitHub-hosted: NOT the target
        "runner_group_name": "GitHub Actions", "labels": ["ubuntu-latest"],
    }


# The real jobs of runner-health run 41826084192 (healthy: all success/skipped),
# trimmed to the fields the helper reads. Source:
# gh api repos/Echo8Lore/action-servers/actions/runs/41826084192/jobs (2026-11-04).
HEALTH_JOBS = {"jobs": [
    {"id": 126561045445, "run_id": 41826084192, "workflow_name": "Runner Health Monitor",
     "head_branch": "main", "status": "completed", "conclusion": "success",
     "created_at": "2026-11-04T12:04:12Z", "started_at": "2026-11-04T12:04:15Z",
     "completed_at": "2026-11-04T12:04:23Z", "name": "check-stale-jobs",
     "html_url": "https://github.com/Echo8Lore/action-servers/actions/runs/41826084192/job/126561045445",
     "steps": [{"name": "Set up job", "status": "completed", "conclusion": "success"},
               {"name": "Run actions/checkout@v5", "status": "completed", "conclusion": "success"},
               {"name": "Detect stale runs", "status": "completed", "conclusion": "success"},
               {"name": "Post Run actions/checkout@v5", "status": "completed", "conclusion": "success"},
               {"name": "Complete job", "status": "completed", "conclusion": "success"}],
     "runner_name": "GitHub Actions 1000025006", "runner_group_name": "GitHub Actions",
     "labels": ["ubuntu-latest"]},
    {"id": 126561045675, "run_id": 41826084192, "workflow_name": "Runner Health Monitor",
     "head_branch": "main", "status": "completed", "conclusion": "success",
     "created_at": "2026-11-04T12:04:12Z", "started_at": "2026-11-04T12:04:15Z",
     "completed_at": "2026-11-04T12:04:25Z", "name": "check-runners",
     "html_url": "https://github.com/Echo8Lore/action-servers/actions/runs/41826084192/job/126561045675",
     "steps": [{"name": "Set up job", "status": "completed", "conclusion": "success"},
               {"name": "Run actions/checkout@v5", "status": "completed", "conclusion": "success"},
               {"name": "Check all runners across inventory scopes", "status": "completed", "conclusion": "success"},
               {"name": "Post Run actions/checkout@v5", "status": "completed", "conclusion": "success"},
               {"name": "Complete job", "status": "completed", "conclusion": "success"}],
     "runner_name": "GitHub Actions 1000025005", "runner_group_name": "GitHub Actions",
     "labels": ["ubuntu-latest"]},
    {"id": 126561045693, "run_id": 41826084192, "workflow_name": "Runner Health Monitor",
     "head_branch": "main", "status": "completed", "conclusion": "success",
     "created_at": "2026-11-04T12:04:12Z", "started_at": "2026-11-04T12:04:15Z",
     "completed_at": "2026-11-04T12:04:19Z", "name": "load-inventory",
     "html_url": "https://github.com/Echo8Lore/action-servers/actions/runs/41826084192/job/126561045693",
     "steps": [{"name": "Set up job", "status": "completed", "conclusion": "success"},
               {"name": "Run actions/checkout@v5", "status": "completed", "conclusion": "success"},
               {"name": "Read hosts", "status": "completed", "conclusion": "success"},
               {"name": "Post Run actions/checkout@v5", "status": "completed", "conclusion": "success"},
               {"name": "Complete job", "status": "completed", "conclusion": "success"}],
     "runner_name": "GitHub Actions 1000025007", "runner_group_name": "GitHub Actions",
     "labels": ["ubuntu-latest"]},
    {"id": 126561047159, "run_id": 41826084192, "workflow_name": "Runner Health Monitor",
     "head_branch": "main", "status": "completed", "conclusion": "skipped",
     "created_at": "2026-11-04T12:04:13Z", "started_at": "2026-11-04T12:04:13Z",
     "completed_at": "2026-11-04T12:04:12Z", "name": "check-scheduler",
     "html_url": "https://github.com/Echo8Lore/action-servers/actions/runs/41826084192/job/126561047159",
     "steps": [], "runner_name": None, "runner_group_name": None,
     "labels": ["ubuntu-latest"]},
    {"id": 126561104660, "run_id": 41826084192, "workflow_name": "Runner Health Monitor",
     "head_branch": "main", "status": "completed", "conclusion": "success",
     "created_at": "2026-11-04T12:04:20Z", "started_at": "2026-11-04T12:04:22Z",
     "completed_at": "2026-11-04T12:04:28Z", "name": "check-disk (ovh-devops-001)",
     "html_url": "https://github.com/Echo8Lore/action-servers/actions/runs/41826084192/job/126561104660",
     "steps": [{"name": "Set up job", "status": "completed", "conclusion": "success"},
               {"name": "Run actions/checkout@v5", "status": "completed", "conclusion": "success"},
               {"name": "Probe ovh-devops-001 disk", "status": "completed", "conclusion": "success"},
               {"name": "Host key matches fleet/known_hosts", "status": "completed", "conclusion": "skipped"},
               {"name": "SSH reachable", "status": "completed", "conclusion": "skipped"},
               {"name": "Disk under threshold", "status": "completed", "conclusion": "success"},
               {"name": "Post Run actions/checkout@v5", "status": "completed", "conclusion": "success"},
               {"name": "Complete job", "status": "completed", "conclusion": "success"}],
     "runner_name": "GitHub Actions 1000025009", "runner_group_name": "GitHub Actions",
     "labels": ["ubuntu-latest"]},
    {"id": 126561104734, "run_id": 41826084192, "workflow_name": "Runner Health Monitor",
     "head_branch": "main", "status": "completed", "conclusion": "success",
     "created_at": "2026-11-04T12:04:20Z", "started_at": "2026-11-04T12:04:22Z",
     "completed_at": "2026-11-04T12:04:28Z", "name": "check-disk (ovh-staging)",
     "html_url": "https://github.com/Echo8Lore/action-servers/actions/runs/41826084192/job/126561104734",
     "steps": [{"name": "Set up job", "status": "completed", "conclusion": "success"},
               {"name": "Run actions/checkout@v5", "status": "completed", "conclusion": "success"},
               {"name": "Probe ovh-staging disk", "status": "completed", "conclusion": "success"},
               {"name": "Host key matches fleet/known_hosts", "status": "completed", "conclusion": "skipped"},
               {"name": "SSH reachable", "status": "completed", "conclusion": "skipped"},
               {"name": "Disk under threshold", "status": "completed", "conclusion": "success"},
               {"name": "Post Run actions/checkout@v5", "status": "completed", "conclusion": "success"},
               {"name": "Complete job", "status": "completed", "conclusion": "success"}],
     "runner_name": "GitHub Actions 1000025008", "runner_group_name": "GitHub Actions",
     "labels": ["ubuntu-latest"]},
    {"id": 126561104984, "run_id": 41826084192, "workflow_name": "Runner Health Monitor",
     "head_branch": "main", "status": "completed", "conclusion": "success",
     "created_at": "2026-11-04T12:04:20Z", "started_at": "2026-11-04T12:04:23Z",
     "completed_at": "2026-11-04T12:04:29Z", "name": "check-disk (hosting-vps)",
     "html_url": "https://github.com/Echo8Lore/action-servers/actions/runs/41826084192/job/126561104984",
     "steps": [{"name": "Set up job", "status": "completed", "conclusion": "success"},
               {"name": "Run actions/checkout@v5", "status": "completed", "conclusion": "success"},
               {"name": "Probe hosting-vps disk", "status": "completed", "conclusion": "success"},
               {"name": "Host key matches fleet/known_hosts", "status": "completed", "conclusion": "skipped"},
               {"name": "SSH reachable", "status": "completed", "conclusion": "skipped"},
               {"name": "Disk under threshold", "status": "completed", "conclusion": "success"},
               {"name": "Post Run actions/checkout@v5", "status": "completed", "conclusion": "success"},
               {"name": "Complete job", "status": "completed", "conclusion": "success"}],
     "runner_name": "GitHub Actions 1000025010", "runner_group_name": "GitHub Actions",
     "labels": ["ubuntu-latest"]},
    # The real skipped auto-restart job (no offline runners that run): the
    # caller-side job with no matrix legs, no called part.
    {"id": 126561152208, "run_id": 41826084192, "workflow_name": "Runner Health Monitor",
     "head_branch": "main", "status": "completed", "conclusion": "skipped",
     "created_at": "2026-11-04T12:04:26Z", "started_at": "2026-11-04T12:04:26Z",
     "completed_at": "2026-11-04T12:04:25Z", "name": "auto-restart",
     "html_url": "https://github.com/Echo8Lore/action-servers/actions/runs/41826084192/job/126561152208",
     "steps": [], "runner_name": None, "runner_group_name": None, "labels": []},
    {"id": 126561183418, "run_id": 41826084192, "workflow_name": "Runner Health Monitor",
     "head_branch": "main", "status": "completed", "conclusion": "success",
     "created_at": "2026-11-04T12:04:29Z", "started_at": "2026-11-04T12:04:33Z",
     "completed_at": "2026-11-04T12:04:39Z", "name": "alert",
     "html_url": "https://github.com/Echo8Lore/action-servers/actions/runs/41826084192/job/126561183418",
     "steps": [{"name": "Set up job", "status": "completed", "conclusion": "success"},
               {"name": "Run actions/checkout@v5", "status": "completed", "conclusion": "success"},
               {"name": "Restore dedupe state", "status": "completed", "conclusion": "success"},
               {"name": "Build and send alert", "status": "completed", "conclusion": "success"},
               {"name": "Save dedupe state", "status": "completed", "conclusion": "success"},
               {"name": "Post Run actions/checkout@v5", "status": "completed", "conclusion": "success"},
               {"name": "Complete job", "status": "completed", "conclusion": "success"}],
     "runner_name": "GitHub Actions 1000025011", "runner_group_name": "GitHub Actions",
     "labels": ["ubuntu-latest"]},
]}


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


class RestartTarget(unittest.TestCase):
    def test_extracts_target_from_the_composite_name(self):
        for name, want in ((f"{CALLER_LEG} / restart-runner (org-runner-01)",
                            (True, "org-runner-01")),
                           ("auto-restart / restart-runner (x)", (True, "x")),
                           (f"{CALLER_LEG} / restart-runner (a b)", (True, "a b")),
                           (f"auto-restart ({{'name': 'z'}}) / restart-runner (z)", (True, "z"))):
            self.assertEqual(rl.restart_target(name), want, name)

    def test_pre_name_leg_is_unmappable(self):
        # The real called job name before OPS-48 (verbatim from dispatch run
        # 36364769843's job): an auto-restart leg that cannot say its runner.
        for name in (f"{CALLER_LEG} / restart-runner",
                     "auto-restart / restart-runner",
                     f"{CALLER_LEG} / restart-runner ()",
                     f"{CALLER_LEG} / restart-runner (a"):
            self.assertEqual(rl.restart_target(name), (True, None), name)

    def test_rejects_everything_else(self):
        for name in ("auto-restart",                          # the real skipped job
                     "check-disk (ovh-staging)",              # a real failed-leg shape
                     "restart-runner",                        # a dispatch run's job: never a leg
                     "restart-runner (x)",                     # ditto, with the OPS-48 name
                     "alert", "check-runners", "", None):
            self.assertEqual(rl.restart_target(name), (False, None), name)

    def test_matches_the_workflow_jobs(self):
        # The extractor and the workflows must agree: the caller job is
        # runner-health.yml's auto-restart, the called job is
        # runner-restart.yml's restart-runner, whose name carries the target.
        health = RUNNER_HEALTH.read_text()
        restart = RUNNER_RESTART.read_text()
        self.assertIn(f"  {rl.CALLER_JOB}:\n", health)
        self.assertIn("uses: ./.github/workflows/runner-restart.yml", health)
        self.assertIn(f"  {rl.CALLED_JOB}:\n", restart)
        self.assertIn("name: restart-runner (${{ inputs.runner_name }})", restart)


class Check(unittest.TestCase):
    def check(self, api, **kw):
        warns = []
        with mock.patch("builtins.print"):
            out, complete = rl.check(api, kw.get("repo", "o/r"), kw.get("now", NOW),
                                     kw.get("window_min", WINDOW), warns.append, print)
        return out, complete, warns

    def test_failed_leg_maps_to_runner_via_its_jobs(self):
        api = Stub({RUNS_Q: {"workflow_runs": [run(1)]},
                    jobs_q(1): {"jobs": HEALTH_JOBS["jobs"] + [failed_leg()]}})
        out, complete, warns = self.check(api)
        self.assertEqual(len(out), 1)
        self.assertTrue(complete)
        text, key = out[0]
        self.assertEqual(key, "restart-path-failing:org-runner-01")
        self.assertIn("CRITICAL - restart path failing", text)
        self.assertIn("1 auto-restart attempt(s) for org-runner-01 failed in the last 6 h", text)
        # The link (OPS-75) deep-links the failed restart JOB, not the whole run.
        self.assertIn("https://github.com/Echo8Lore/action-servers/actions/runs/41826084192/job/108748880806", text)
        self.assertIn("Action:", text)
        self.assertIn(RUNS_Q, api.fetched)
        self.assertIn(jobs_q(1), api.fetched)

    def test_real_healthy_payload_pages_nothing(self):
        # The unmodified real jobs payload of run 41826084192: no finding, and
        # the check is complete.
        api = Stub({RUNS_Q: {"workflow_runs": [HEALTH_RUN]}, jobs_q(41826084192): HEALTH_JOBS})
        out, complete, _ = self.check(api)
        self.assertEqual(out, [])
        self.assertTrue(complete)

    def test_counts_stay_out_of_the_keys(self):
        # Two failed legs for one runner: the count and the latest URL move, the
        # key must not (a deduped condition, not a page per failed attempt).
        api = Stub({RUNS_Q: {"workflow_runs": [run(1, created="2026-11-04T09:00:00Z"),
                                              run(2, created="2026-11-04T11:30:00Z")]},
                    jobs_q(1): {"jobs": [failed_leg(job_id=1, created="2026-11-04T09:00:30Z")]},
                    jobs_q(2): {"jobs": [failed_leg(job_id=2, created="2026-11-04T11:30:30Z")]}})
        out, _, _ = self.check(api)
        self.assertEqual([k for _, k in out], ["restart-path-failing:org-runner-01"])
        self.assertIn("2 auto-restart attempt(s) for org-runner-01", out[0][0])
        self.assertIn("job/2", out[0][0])    # latest

    def test_two_runners_are_two_conditions(self):
        api = Stub({RUNS_Q: {"workflow_runs": [run(2), run(1)]},
                    jobs_q(1): {"jobs": [failed_leg(target="r-alpha", job_id=1)]},
                    jobs_q(2): {"jobs": [failed_leg(target="r-beta", job_id=2)]}})
        out, _, _ = self.check(api)
        self.assertEqual([k for _, k in out],
                         ["restart-path-failing:r-alpha", "restart-path-failing:r-beta"])

    def test_healthy_skipped_cancelled_and_other_failed_jobs_never_count(self):
        # Only FAILED auto-restart legs count: a failed check-disk leg, a
        # skipped auto-restart (the real one) and legs that succeeded or were
        # cancelled never page -- and the check stays complete.
        jobs = {"jobs": HEALTH_JOBS["jobs"] + [
            failed_leg(target="r-ok", conclusion="success", job_id=10),
            failed_leg(target="r-skip", conclusion="skipped", job_id=11),
            failed_leg(target="r-cancel", conclusion="cancelled", job_id=12),
            {"name": "check-disk (ovh-staging)", "conclusion": "failure",
             "created_at": "2026-11-04T12:04:20Z",
             "html_url": "https://github.com/Echo8Lore/action-servers/actions/runs/41826084192/job/99"},
        ]}
        api = Stub({RUNS_Q: {"workflow_runs": [run(1)]}, jobs_q(1): jobs})
        out, complete, _ = self.check(api)
        self.assertEqual(out, [])
        self.assertTrue(complete)

    def test_timed_out_leg_counts(self):
        # A restart that hung until the job timed out is as broken as a failed
        # one: the automated path did not get the runner back.
        api = Stub({RUNS_Q: {"workflow_runs": [run(1)]},
                    jobs_q(1): {"jobs": [failed_leg(conclusion="timed_out")]}})
        out, _, _ = self.check(api)
        self.assertEqual([k for _, k in out], ["restart-path-failing:org-runner-01"])

    def test_in_progress_run_counts(self):
        # The CURRENT run (this monitor run itself) is still in progress while
        # its alert job checks: its conclusion is null, so the runs query must
        # not pre-filter by status -- the leg that already failed this run
        # pages this run, not only the next.
        api = Stub({RUNS_Q: {"workflow_runs": [run(1, conclusion=None, status="in_progress",
                                                  created="2026-11-04T13:58:00Z")]},
                    jobs_q(1): {"jobs": [failed_leg()]}})
        out, _, _ = self.check(api)
        self.assertEqual([k for _, k in out], ["restart-path-failing:org-runner-01"])

    def test_runs_query_targets_runner_health_without_status_or_event_filter(self):
        # The first round of PR #24 queried runner-restart.yml runs filtered to
        # event=workflow_call -- runs that do not exist (the blocker). The
        # query must list runner-health.yml's runs, unfiltered.
        self.assertIn("runner-health.yml", RUNS_Q)
        self.assertNotIn("runner-restart.yml", RUNS_Q)
        self.assertNotIn("status=", RUNS_Q)
        self.assertNotIn("event=", RUNS_Q)

    def test_old_runs_age_out_of_the_window(self):
        api = Stub({RUNS_Q: {"workflow_runs": [run(1, created="2026-11-04T09:30:00Z"),
                                              run(2, created="2026-11-04T05:59:00Z")]},
                    jobs_q(1): {"jobs": [failed_leg()]}})
        out, _, _ = self.check(api)
        self.assertEqual([k for _, k in out], ["restart-path-failing:org-runner-01"])
        self.assertNotIn(jobs_q(2), api.fetched)

    def test_run_without_created_at_counts_inside_the_window(self):
        api = Stub({RUNS_Q: {"workflow_runs": [run(1, created=None)]},
                    jobs_q(1): {"jobs": [failed_leg()]}})
        out, _, warns = self.check(api)
        self.assertEqual([k for _, k in out], ["restart-path-failing:org-runner-01"])
        self.assertTrue(any("no created_at" in w for w in warns))
        self.assertIn(jobs_q(1), api.fetched)   # scanned, not skipped

    def test_runs_api_error_never_pages_and_is_incomplete(self):
        out, complete, warns = self.check(Failing("runs?"))
        self.assertEqual(out, [])
        self.assertFalse(complete)
        self.assertTrue(any("not checked" in w for w in warns))

    def test_jobs_api_error_skips_the_run_and_marks_incomplete_no_flap(self):
        # Should-fix 2: a transient jobs-API error must not mint a '?' key
        # (that flapped -- two extra pages per blip). The unreadable run is
        # skipped and the check is marked incomplete; the readable run's
        # findings still page.
        api = Failing("runs/1/jobs", pages={RUNS_Q: {"workflow_runs": [run(1), run(2)]},
                                      jobs_q(2): {"jobs": [failed_leg(target="r-beta", job_id=2)]}})
        out, complete, warns = self.check(api)
        self.assertEqual([k for _, k in out], ["restart-path-failing:r-beta"])
        self.assertFalse(complete)
        self.assertTrue(any("run 1" in w and "not checked" in w for w in warns))

    def test_unmappable_pre_name_leg_falls_back_to_unknown_runner(self):
        # A leg whose called job does not name the target (pre-OPS-48 shape)
        # still pages, as restart-path-failing:? -- like offline:?; the data
        # was read, so the key is stable run over run.
        api = Stub({RUNS_Q: {"workflow_runs": [run(1)]},
                    jobs_q(1): {"jobs": [failed_leg(target=None)]}})
        out, complete, _ = self.check(api)
        self.assertEqual([k for _, k in out], ["restart-path-failing:?"])
        self.assertTrue(complete)
        self.assertIn("does not say which", out[0][0])


class Main(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, api, token="t", incomplete=True):
        env = {"GH_TOKEN": token} if token else {}
        args = ["check", "--repo", "o/r", "--message", str(self.d / "m"),
                "--keys", str(self.d / "k")]
        if incomplete:
            args += ["--incomplete", str(self.d / "inc")]
        with mock.patch.dict("os.environ", env, clear=True), \
                mock.patch.object(rl, "github_api", return_value=api), \
                mock.patch.object(rl, "now_utc", return_value=NOW), \
                mock.patch("builtins.print"):
            rc = rl.main(args)
        inc = self.d / "inc"
        return rc, (self.d / "m").read_text(), (self.d / "k").read_text(), \
            (inc.read_text() if inc.exists() else None)

    def test_main_writes_message_keys_and_clears_incomplete(self):
        api = Stub({RUNS_Q: {"workflow_runs": [run(1)]},
                    jobs_q(1): {"jobs": [failed_leg()]}})
        rc, msg, keys, inc = self.run_main(api)
        self.assertEqual(rc, 0)
        self.assertIn("restart path failing", msg)
        self.assertIn("org-runner-01", msg)
        self.assertEqual(keys, "restart-path-failing:org-runner-01\n")
        self.assertEqual(inc, "false\n")

    def test_main_healthy_writes_nothing_and_clears_incomplete(self):
        api = Stub({RUNS_Q: {"workflow_runs": [HEALTH_RUN]},
                    jobs_q(41826084192): HEALTH_JOBS})
        rc, msg, keys, inc = self.run_main(api)
        self.assertEqual((rc, msg, keys, inc), (0, "", "", "false\n"))

    def test_main_runs_api_error_leaves_incomplete(self):
        # No keys AND not fully checked: the alert must not treat this as
        # "cleared" (no false RESOLVED, review should-fix 1).
        rc, msg, keys, inc = self.run_main(Failing("runs?"))
        self.assertEqual((rc, msg, keys, inc), (0, "", "", "true\n"))

    def test_main_without_token_marks_incomplete(self):
        rc, msg, keys, inc = self.run_main(Stub({}), token="")
        self.assertEqual((rc, msg, keys, inc), (0, "", "", "true\n"))

    def test_main_crash_leaves_incomplete_true(self):
        # Fail-safe: the marker starts "true" and is only cleared by a check
        # that really completed -- even a helper crash leaves it.
        class Crash:
            def __call__(self, path):
                raise AssertionError("helper bug")
        with self.assertRaises(AssertionError):
            self.run_main(Crash())
        self.assertEqual((self.d / "inc").read_text(), "true\n")

    def test_main_without_incomplete_arg(self):
        # The marker is optional: older callers get the old behavior.
        api = Stub({RUNS_Q: {"workflow_runs": [HEALTH_RUN]},
                    jobs_q(41826084192): HEALTH_JOBS})
        rc, msg, keys, inc = self.run_main(api, incomplete=False)
        self.assertEqual((rc, msg, keys, inc), (0, "", "", None))


if __name__ == "__main__":
    unittest.main()