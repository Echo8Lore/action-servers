#!/usr/bin/env python3
"""Static checks on runner-restart.yml's notification contract (OPS-47, OPS-48; stdlib).

Run from the repo root:  python3 -m unittest discover -s ops -v

What they pin:
  1. The restart, verify, ops/ checkout and notification steps all run
     if: !cancelled(), so a failed restart command cannot silently skip the
     page (five runs between 2026-09-26 and -28 did exactly that) — while a
     CANCELLED dispatch runs none of them: nothing ran on the host, so there is
     nothing to verify, nothing to page, and no 60 s of retries burned (OPS-48
     (a)).
  2. The notification's wiring: it reads the restart step's outcome and the
     verify step's output, and its quiet-mode suppression comes FIRST and
     actually EXITS, so an auto-restart (runner-health, quiet_unless_online)
     still only pages a verified recovery — the monitor's deduplicated CRITICAL
     owns a runner that stays offline (OPS-48 (c): a mutant that replaced the
     quiet block with ':' survived the ordering pin alone).
  3. The notification never fails the run (continue-on-error), and claims
     "restarted and verified online" only when the restart step SUCCEEDED —
     anything else (failure, or a skipped restart step because the first
     checkout failed) is reported as "did not succeed" (OPS-48 (b)).
  4. The verify step passes TARGET_RUNNER to jq as data (--arg), never
     interpolated into the filter string: that was a jq-filter injection for
     anyone with dispatch rights, able to distort the online verdict (OPS-48
     (d)).
  5. The job's name carries the target runner: it is the only API-visible
     place a run says what it was about, and runner-health maps this repo's
     failed runner-restart runs back to their runner through it
     (ops/restart_liveness.py, OPS-48).

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
    """Steps as (name, body-without-conditions) from the workflow's single job."""
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

    def test_every_outcome_steps_run_unless_cancelled(self):
        # !cancelled(), not always() (OPS-48 (a)): the three steps that used to
        # run always() must still run on a failed restart (OPS-47) but skip a
        # cancelled dispatch entirely.
        for name in ("Verify runner is online", "Telegram notification"):
            body = self.step(name)
            self.assertIn('if: ${{ !cancelled() }}', body, name)
            self.assertNotIn("if: always()", body, name)
        checkouts = [body for head, body in self.all_steps if head.startswith("- uses:")
                     and "sparse-checkout: ops" in body]
        self.assertTrue(checkouts, "the ops/ checkout step was not found")
        for body in checkouts:
            self.assertIn('if: ${{ !cancelled() }}', body, "ops/ checkout")

    def test_no_always_left_in_the_workflow(self):
        # A cancelled dispatch must not page or verify anything: no step here
        # may run after cancellation.
        self.assertNotIn("if: always()", WORKFLOW.read_text())

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
        quiet = body.index('if [ "$ONLINE" != "true" ] && [ "$QUIET" = "true" ]')
        self.assertLess(quiet, body.index("MSG="), "the quiet check must gate every message")

    def test_quiet_block_actually_exits(self):
        # OPS-48 (c): the quiet suppression must END THE STEP between its if and
        # fi — a mutant replacing the block with ':' survived the ordering pin
        # alone and would have paged quiet auto-restarts on every run.
        lines = self.step("Telegram notification").splitlines()
        start = next(i for i, ln in enumerate(lines)
                     if '[ "$ONLINE" != "true" ] && [ "$QUIET" = "true" ]' in ln)
        end = next(i for i in range(start + 1, len(lines)) if re.match(r"\s*fi\b", lines[i]))
        block = lines[start:end]
        self.assertTrue(any(re.match(r"\s*exit 0\s*$", ln) for ln in block),
                        "the quiet block must exit 0, not fall through")

    def test_notification_never_fails_the_run(self):
        self.assertIn("continue-on-error: true", self.step("Telegram notification"))

    def test_no_false_verified_online_on_failed_restart(self):
        # OPS-48 (b): the verified-online claim must be gated on RESTART =
        # success, not on the old "!= failure" reasoning — a SKIPPED restart
        # step (the fleet checkout failed; nothing ran on the host) is not a
        # failure and used to claim "restarted and verified online".
        body = self.step("Telegram notification")
        claim = 'MSG="Self-hosted runner ${TARGET_RUNNER} restarted and verified online."'
        self.assertEqual(body.count(claim), 1, "exactly one verified-online claim")
        lines = body.splitlines()
        gate = next(i for i, ln in enumerate(lines)
                    if '[ "$ONLINE" = "true" ] && [ "$RESTART" = "success" ]' in ln)
        at = next(i for i, ln in enumerate(lines) if claim in ln)
        branch_end = next(i for i in range(gate + 1, len(lines))
                          if re.match(r"\s*(elif|else|fi)\b", lines[i]))
        self.assertLess(gate, at, "the claim must live in the RESTART=success branch")
        self.assertLess(at, branch_end, "the claim must live in the RESTART=success branch")
        self.assertNotIn('[ "$RESTART" = "failure" ]', body,
                         "the weak failure-only check is gone")
        # Non-success outcomes are still named, online or not.
        self.assertIn("did not succeed (outcome: ${RESTART})", body)
        self.assertIn("is NOT online", body)

    def test_verify_passes_the_runner_as_jq_data(self):
        # OPS-48 (d): TARGET_RUNNER is operator input; interpolated into the
        # --jq filter string it was a jq-filter injection able to distort the
        # online verdict. It must go in as data (--arg).
        body = self.step("Verify runner is online")
        self.assertIn('--arg target "$TARGET_RUNNER"', body)
        self.assertIn("select(.name == $target)", body)
        self.assertNotIn('select(.name == \\"$TARGET_RUNNER\\"', body)

    def test_job_name_carries_the_target_runner(self):
        # OPS-48: the job's name is the only API-visible place a run says which
        # runner it targeted; ops/restart_liveness.py maps failed runs to their
        # runners through it (the jobs API).
        self.assertIn("name: restart-runner (${{ inputs.runner_name }})",
                      WORKFLOW.read_text())


if __name__ == "__main__":
    unittest.main()