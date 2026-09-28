#!/usr/bin/env python3
"""Static checks on runner-restart.yml's notification contract (OPS-47; stdlib).

Run from the repo root:  python3 -m unittest discover -s ops -v

What they pin:
  1. The restart, verify, ops/ checkout and notification steps all run
     if: always(), so a failed restart command cannot silently skip the page
     (five runs between 2026-09-26 and -28 did exactly that).
  2. The notification's wiring: it reads the restart step's outcome and the verify
     step's output, and its quiet-mode suppression comes FIRST, so an
     auto-restart (runner-health, quiet_unless_online) still only pages a verified
     recovery — the monitor's deduplicated CRITICAL owns a runner that stays offline.
  3. The notification never fails the run (continue-on-error), and never claims
     "restarted and verified online" for a failed restart command.

Nothing here touches the network or any secret.
"""

import pathlib
import re
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
WORKFLOW = ROOT / ".github" / "workflows" / "runner-restart.yml"


def code_lines(path):
    """Non-comment lines (full-line # comments dropped; they may name what is pinned)."""
    return [ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#")]


def steps(path):
    """Steps as (name, body-with-conditions) from the workflow's single job."""
    name = None
    body = []
    out = []
    for ln in code_lines(path):
        if re.match(r"\s*- (?:name|uses):", ln):
            if name is not None:
                out.append((name, "\n".join(body)))
            name = ln.strip()
            body = []
        elif name is not None:
            body.append(ln)
    if name is not None:
        out.append((name, "\n".join(body)))
    return out


class RunnerRestartNotify(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.all_steps = steps(WORKFLOW)
        cls.by_name = {}
        for head, body in cls.all_steps:
            m = re.match(r"- name: (.+)", head)
            if m:
                # A later - name: wins: none of the pinned names repeat here.
                cls.by_name[m.group(1)] = body

    def step(self, name):
        self.assertIn(name, self.by_name, f"step {name!r} not found")
        return self.by_name[name]

    def test_every_outcome_steps_run_always(self):
        for name in ("Verify runner is online", "Telegram notification"):
            self.assertIn("if: always()", self.step(name), name)

    def test_ops_checkout_runs_always(self):
        checkouts = [body for head, body in self.all_steps if head.startswith("- uses:")
                     and "sparse-checkout: ops" in body]
        self.assertTrue(checkouts, "the ops/ checkout step was not found")
        for body in checkouts:
            self.assertIn("if: always()", body, "ops/ checkout")

    def test_restart_step_has_id(self):
        self.assertRegex(self.step("Restart runner service via SSH"), r"id: restart",
                         "the notify step cannot see the restart outcome without id: restart")

    def test_notification_reads_restart_outcome_and_verify(self):
        body = self.step("Telegram notification")
        self.assertIn("RESTART: ${{ steps.restart.outcome }}", body)
        self.assertIn("ONLINE: ${{ steps.verify.outputs.online }}", body)
        self.assertIn("QUIET: ${{ inputs.quiet_unless_online }}", body)

    def test_quiet_suppression_precedes_all_messaging(self):
        body = self.step("Telegram notification")
        quiet = body.index("if [ \"$ONLINE\" != \"true\" ] && [ \"$QUIET\" = \"true\" ]")
        self.assertLess(quiet, body.index("MSG="), "the quiet check must gate every message")

    def test_notification_never_fails_the_run(self):
        self.assertIn("continue-on-error: true", self.step("Telegram notification"))

    def test_no_false_verified_online_on_failed_restart(self):
        body = self.step("Telegram notification")
        # The "restarted and verified online" claim must live in the branch that
        # first excluded a failed restart command.
        online_claims = re.findall(r'MSG="Self-hosted runner \$\{TARGET_RUNNER\} restarted and verified online\."',
                                   body)
        self.assertEqual(len(online_claims), 1, "exactly one verified-online claim")
        claim = online_claims[0]
        failure_branch = body.index('[ "$RESTART" = "failure" ]')
        self.assertLess(failure_branch, body.index(claim),
                        "the failed-restart branch must be checked before any online claim")


if __name__ == "__main__":
    unittest.main()