#!/usr/bin/env python3
"""Tests for ops/alert_dedupe.py and its wiring in runner-health.yml (stdlib, no network).

Run from the repo root:  python3 -m unittest discover -s ops -v

Cases: first page; same condition within 6 h suppressed; condition changed pages;
6 h repeat; resolved (once); test_alert bypass; a failed send does not adopt the state
(decide never writes STATE, and the workflow adopts NEW only after notify-telegram.sh
exits 0); keys ignore prose and counts; per-host disk keys from the jobs API legs
(OPS-45), a second host crossing pages at once, API failure falls back to one "disk" key.
"""

import datetime as dt
import json
import pathlib
import re
import sys
import tempfile
import unittest
import unittest.mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import alert_dedupe as ad  # noqa: E402

ROOT = HERE.parent
RUNNER_HEALTH = ROOT / ".github" / "workflows" / "runner-health.yml"
RUNNER_RESTART = ROOT / ".github" / "workflows" / "runner-restart.yml"
T0 = dt.datetime(2026, 9, 27, 12, 0, tzinfo=dt.timezone.utc)
MSG = "Runner Health Monitor\n\nCRITICAL - Runner(s) OFFLINE: r1\n"


def later(minutes):
    return T0 + dt.timedelta(minutes=minutes)


class Keys(unittest.TestCase):
    def test_offline_runners_become_keys(self):
        self.assertEqual(ad.condition_keys("true", '["r2","r1"]'), ["offline:r1", "offline:r2"])
        self.assertEqual(ad.condition_keys("false", '["r1"]'), [])
        self.assertEqual(ad.condition_keys("true", "not json"), ["offline:?"])

    def test_disk_and_stale(self):
        summary = "  - o/a: CI — started 2026-09-27T10:00:00Z\\n  - o/b: Build — started 2026-09-27T09:00:00Z"
        keys = ad.condition_keys(disk="true", stale_count="2", stale_summary=summary)
        self.assertEqual(keys, ["disk", "stale:- o/a: CI — started 2026-09-27T10:00:00Z",
                                "stale:- o/b: Build — started 2026-09-27T09:00:00Z"])
        self.assertEqual(ad.condition_keys(stale_count="3"), ["stale:count=3"])
        self.assertEqual(ad.condition_keys(stale_count="0", stale_summary="x"), [])

    def test_scheduler_keys_only(self):
        self.assertEqual(ad.condition_keys(scheduler_keys="scheduler:NOTICE:runner-health.yml:b\n\njunk\n"),
                         ["scheduler:NOTICE:runner-health.yml:b"])

    def test_fingerprint_is_order_free_and_prose_free(self):
        a = ad.fingerprint(["offline:r1", "disk"])
        self.assertEqual(a, ad.fingerprint(["disk", "offline:r1"]))
        self.assertNotEqual(a, ad.fingerprint(["disk"]))
        self.assertEqual(ad.fingerprint([]), "")


def leg(host, step):
    return json.dumps({"name": f"check-disk ({host})", "step": step})


LEGS = "\n".join([leg("ovh-staging", "Disk under threshold"),
                   leg("hosting-vps", "SSH reachable"),
                   leg("ovh-devops-001", "Host key matches fleet/known_hosts")]) + "\n"


class DiskLegs(unittest.TestCase):
    def test_parse_classifies_by_failed_step(self):
        self.assertEqual(ad.parse_disk_legs(LEGS), [("hosting-vps", "ssh"),
                                                    ("ovh-devops-001", "hostkey"),
                                                    ("ovh-staging", "disk")])

    def test_parse_unknown_step_and_bare_names(self):
        text = leg("a", "Probe a disk") + "\n" + leg("b", "") + "\ncheck-disk (c)\n"
        self.assertEqual(ad.parse_disk_legs(text), [("a", "unknown"), ("b", "unknown"),
                                                    ("c", "unknown")])

    def test_parse_unusable_is_none(self):
        for text in (None, "", "\n", "junk\n", '{"name": "alert"}\n', "check-disk ()\n",
                     '{"name": 5}\n[1]\n'):
            self.assertIsNone(ad.parse_disk_legs(text), text)

    def test_per_host_keys(self):
        keys = ad.condition_keys(disk="true", disk_legs=ad.parse_disk_legs(LEGS))
        self.assertEqual(keys, ["disk-hostkey:ovh-devops-001", "disk-ssh:hosting-vps",
                                "disk:ovh-staging"])
        # An unknown failing step still names the host, under the plain disk key.
        self.assertEqual(ad.condition_keys(disk="true", disk_legs=[("a", "unknown")]),
                         ["disk:a"])

    def test_legs_ignored_unless_check_disk_failed(self):
        self.assertEqual(ad.condition_keys(disk="false", disk_legs=ad.parse_disk_legs(LEGS)), [])
        self.assertEqual(ad.disk_message("false", ad.parse_disk_legs(LEGS)), "")

    def test_api_failure_falls_back_to_one_disk_key(self):
        # No legs file (gh api failed), or it listed nothing usable: degraded, not silent.
        for legs in (None, []):
            self.assertEqual(ad.condition_keys(disk="true", disk_legs=legs), ["disk"])
            self.assertEqual(ad.disk_message("true", legs), ad.DISK_FALLBACK)

    def test_message_names_hosts_per_cause(self):
        msg = ad.disk_message("true", ad.parse_disk_legs(LEGS + leg("x-host", "Disk under threshold")))
        self.assertIn("Disk over threshold on: ovh-staging, x-host", msg)
        self.assertIn("SSH unreachable (or secrets missing), disk not checked, on: hosting-vps", msg)
        self.assertIn("HOST KEY MISMATCH (or no pinned key), disk not checked, on: ovh-devops-001", msg)
        self.assertNotIn("cause not reported", msg)

    def test_keys_carry_no_usage(self):
        # Fingerprint = keys only: usage % and threshold never enter a key.
        keys = ad.condition_keys(disk="true", disk_legs=ad.parse_disk_legs(leg("h", "Disk under threshold")))
        self.assertEqual(keys, ["disk:h"])

    def test_second_host_crossing_pages_immediately(self):
        one = ad.condition_keys(disk="true", disk_legs=[("ovh-staging", "disk")])
        _, _, st = ad.decide({}, one, "m1", T0)
        self.assertEqual(ad.decide(st, one, "m1", later(30))[0], "suppress")
        two = ad.condition_keys(disk="true", disk_legs=[("hosting-vps", "disk"), ("ovh-staging", "disk")])
        action, text, _ = ad.decide(st, two, "m2", later(30))
        self.assertEqual((action, text), ("page", "m2"))

    def test_cause_change_on_same_host_pages(self):
        _, _, st = ad.decide({}, ["disk-ssh:h"], "m", T0)
        self.assertEqual(ad.decide(st, ["disk:h"], "m", later(30))[0], "page")

    def test_steps_match_workflow(self):
        # DISK_STEPS names must be the check-disk job's step names.
        text = RUNNER_HEALTH.read_text()
        job = text[text.index("\n  check-disk:\n"):text.index("\n  check-stale-jobs:")]
        for name in ad.DISK_STEPS:
            self.assertIn(f"      - name: {name}\n", job)
        self.assertIn("name: check-disk (${{ matrix.host.id }})", job)


class Decide(unittest.TestCase):
    KEYS = ["offline:r1"]

    def first(self):
        return ad.decide({}, self.KEYS, MSG, T0)

    def test_first_page(self):
        action, text, st = self.first()
        self.assertEqual(action, "page")
        self.assertEqual(text, MSG)
        self.assertEqual(st, {"fingerprint": ad.fingerprint(self.KEYS),
                              "paged_at": "2026-09-27T12:00:00Z"})

    def test_same_condition_within_6h_suppressed(self):
        _, _, st = self.first()
        for m in (30, 60, 359):
            action, text, st2 = ad.decide(st, self.KEYS, MSG + f"(count {m})", later(m))
            self.assertEqual((action, text), ("suppress", ""), m)
            self.assertEqual(st2, st)

    def test_condition_changed_pages(self):
        _, _, st = self.first()
        action, text, st2 = ad.decide(st, ["offline:r1", "disk"], "new msg", later(30))
        self.assertEqual(action, "page")
        self.assertEqual(text, "new msg")
        self.assertNotEqual(st2["fingerprint"], st["fingerprint"])
        # A condition clearing while another stays is a change too.
        action, _, _ = ad.decide(st2, ["disk"], "m", later(60))
        self.assertEqual(action, "page")

    def test_6h_repeat(self):
        _, _, st = self.first()
        action, text, st2 = ad.decide(st, self.KEYS, MSG, later(360))
        self.assertEqual(action, "repeat")
        self.assertTrue(text.startswith("REPEAT - unchanged for 6 h"))
        self.assertIn(MSG, text)
        self.assertEqual(st2["paged_at"], "2026-09-27T18:00:00Z")
        # ...and then quiet again for the next 6 h.
        self.assertEqual(ad.decide(st2, self.KEYS, MSG, later(390))[0], "suppress")

    def test_resolved_once(self):
        _, _, st = self.first()
        action, text, st2 = ad.decide(st, [], "", later(30), run_url="https://x/run/1")
        self.assertEqual(action, "resolved")
        self.assertIn("RESOLVED - fleet healthy", text)
        self.assertIn("https://x/run/1", text)
        self.assertEqual(st2, {"fingerprint": "", "paged_at": None})
        self.assertEqual(ad.decide(st2, [], "", later(60))[:2], ("none", ""))

    def test_incomplete_never_resolves(self):
        _, _, st = self.first()
        action, text, st2 = ad.decide(st, [], "", later(30), incomplete=True)
        self.assertEqual((action, text), ("none", ""))
        self.assertEqual(st2, st)

    def test_healthy_from_scratch_is_silent(self):
        self.assertEqual(ad.decide({}, [], "", T0)[:2], ("none", ""))

    def test_test_alert_bypasses_dedupe(self):
        _, _, st = self.first()
        action, text, _ = ad.decide(st, self.KEYS, MSG, later(30), test_alert=True)
        self.assertEqual((action, text), ("page", MSG))

    def test_junk_state_is_a_first_page(self):
        for junk in (None, [], "x", {"fingerprint": 5}, {"fingerprint": "ab", "paged_at": "never"}):
            self.assertEqual(ad.decide(junk, self.KEYS, MSG, T0)[0], "page", junk)


class Main(unittest.TestCase):
    def run_main(self, state, **kw):
        d = pathlib.Path(self.tmp.name)
        (d / "state.json").write_text(json.dumps(state))
        (d / "msg").write_text(kw.pop("message", MSG))
        args = ["decide", "--state", str(d / "state.json"), "--new-state", str(d / "new.json"),
                "--message", str(d / "msg"), "--out", str(d / "out")]
        for k, v in kw.items():
            args += [f"--{k.replace('_', '-')}", v]
        with unittest.mock.patch.object(ad, "now_utc", return_value=T0), \
                unittest.mock.patch("builtins.print"):
            self.assertEqual(ad.main(args), 0)
        return (d / "out").read_text(), json.loads((d / "new.json").read_text()), \
            json.loads((d / "state.json").read_text())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_send_failure_does_not_adopt_state(self):
        # decide never writes STATE: if the send then fails, the workflow keeps the old
        # file and the next run pages again.
        out, new, old = self.run_main({}, any_offline="true", offline_runners='["r1"]')
        self.assertEqual(out, MSG)
        self.assertEqual(old, {})
        self.assertTrue(new["fingerprint"])

    def test_disk_legs_file_and_fallback(self):
        d = pathlib.Path(self.tmp.name)
        (d / "legs").write_text(LEGS)
        want = ad.fingerprint(["disk-hostkey:ovh-devops-001", "disk-ssh:hosting-vps",
                               "disk:ovh-staging"])
        _, new, _ = self.run_main({}, disk="true", disk_legs=str(d / "legs"))
        self.assertEqual(new["fingerprint"], want)
        # Missing file (the workflow deletes it when gh api fails): one "disk" key.
        _, new, _ = self.run_main({}, disk="true", disk_legs=str(d / "missing"))
        self.assertEqual(new["fingerprint"], ad.fingerprint(["disk"]))

    def test_disk_message_command(self):
        d = pathlib.Path(self.tmp.name)
        (d / "legs").write_text(leg("hosting-vps", "Disk under threshold"))
        with unittest.mock.patch("builtins.print") as pr:
            self.assertEqual(ad.main(["disk-message", "--disk", "true", "--legs", str(d / "legs")]), 0)
        self.assertIn("Disk over threshold on: hosting-vps", pr.call_args[0][0])
        with unittest.mock.patch("builtins.print") as pr:
            ad.main(["disk-message", "--disk", "true", "--legs", str(d / "missing")])
        self.assertEqual(pr.call_args[0][0], ad.DISK_FALLBACK)

    def test_suppressed_writes_empty_out(self):
        st = {"fingerprint": ad.fingerprint(["offline:r1"]), "paged_at": "2026-09-27T11:30:00Z"}
        out, new, _ = self.run_main(st, any_offline="true", offline_runners='["r1"]')
        self.assertEqual(out, "")
        self.assertEqual(new, st)


class Wiring(unittest.TestCase):
    """runner-health.yml's alert job: dedupe cache restored/saved, concurrency on the
    alert job, state adopted only after a successful send, test_alert passed through."""

    def alert_job(self):
        text = RUNNER_HEALTH.read_text()
        start = text.index("\n  alert:\n")
        end = text.index("\n  auto-restart:", start)
        return text[start:end]

    def test_cache_and_concurrency(self):
        job = self.alert_job()
        self.assertRegex(job, r"concurrency:\s*\n\s+group: runner-health-alert\s*\n\s+cancel-in-progress: false")
        self.assertIn("actions/cache/restore@v5", job)
        self.assertIn("restore-keys: runner-health-alert-", job)
        self.assertRegex(job, r"(?s)if: always\(\)\s*\n\s+uses: actions/cache/save@v5")
        self.assertEqual(job.count("key: runner-health-alert-${{ github.run_id }}"), 2)

    def test_state_adopted_only_after_successful_send(self):
        job = self.alert_job()
        send = job.index("bash ops/notify-telegram.sh < \"$RUNNER_TEMP/send.txt\"")
        adopt = [m.start() for m in re.finditer(r'cp "\$RUNNER_TEMP/new-state.json" \.runner-health/state.json', job)]
        self.assertTrue(adopt)
        guarded = job[send:adopt[0]]
        self.assertIn('if [ "$rc" -eq 0 ]', guarded)
        self.assertIn("--test-alert \"$TEST_ALERT\"", job)

    def test_disk_legs_from_jobs_api_with_fallback(self):
        job = self.alert_job()
        self.assertIn("GH_TOKEN: ${{ github.token }}", job)
        self.assertIn('gh api --paginate "repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}/jobs', job)
        self.assertIn('select(.name | startswith("check-disk ("))', job)
        self.assertIn('rm -f "$DISK_LEGS"', job)          # API failure -> no file -> "disk"
        self.assertIn('--disk-legs "$DISK_LEGS"', job)
        perms = RUNNER_HEALTH.read_text().split("\npermissions:\n", 1)[1].split("\n\n", 1)[0]
        self.assertRegex(perms, r"actions: (read|write)")

    def test_restart_quiet_flag_from_runner_health(self):
        self.assertIn("quiet_unless_online: true", RUNNER_HEALTH.read_text())
        restart = RUNNER_RESTART.read_text()
        call = restart[restart.index("  workflow_call:"):restart.index("\npermissions:")]
        self.assertIn("quiet_unless_online:", call)
        dispatch = restart[restart.index("  workflow_dispatch:"):restart.index("  workflow_call:")]
        self.assertNotIn("quiet_unless_online", dispatch)   # manual runs keep the failure notice


if __name__ == "__main__":
    unittest.main()
