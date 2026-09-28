#!/usr/bin/env python3
"""Tests for the needrestart drop-in in runners/bootstrap-host.sh (OPS-49).

Run from the repo root:  python3 -m unittest discover -s runners -v

What they pin:
  1. bootstrap writes /etc/needrestart/conf.d/50-actions-runner.conf with
     $nrconf{override_rc}{qr(^actions\\.runner\\.)} = 0; and does so before its first
     apt-get (apt's needrestart hook would otherwise restart live runners mid-bootstrap).
  2. The block is idempotent (an up-to-date file is left alone, a changed one is
     rewritten), verifies the effective config, and fails on a drop-in that breaks
     parsing or undoes the override.
  3. Evaluated the way needrestart evaluates it (a stub of Ubuntu's needrestart.conf:
     strict, $LOGPREF, the conf.d loop), runner units are skipped while other services
     are still restarted, and fleet/collect-facts.sh reaches the same verdict.
The block runs against a temp dir, never /etc; perl is needed (as on every Ubuntu host).
"""

import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BOOTSTRAP = (ROOT / "runners" / "bootstrap-host.sh").read_text()
COLLECT = (ROOT / "fleet" / "collect-facts.sh").read_text()
DROPIN_LINE = r"$nrconf{override_rc}{qr(^actions\.runner\.)} = 0;"
RUNNERS = ["actions.runner.Echo8Lore.org-runner-02.service",
           "actions.runner.ResearchMonkey-FieldKit.personal-fieldkit-02.service",
           "actions.runner.Echo8Lore-Weapons_Lore.vps-ci-runner-01.service"]

# Trimmed from Ubuntu 24.04's /etc/needrestart/needrestart.conf (needrestart 3.6):
# the same override_rc shape and the same conf.d loop, which uses needrestart's own
# lexical $LOGPREF and runs under its `use strict`.
STUB_CONF = r"""
$nrconf{override_rc} = {
    qr(^dbus) => 0,
    qr(^systemd-logind) => 0,
};
if(-d q(@CONFD@)) {
      foreach my $fn (sort <@CONFD@/*.conf>) {
	      print STDERR "$LOGPREF eval $fn\n" if($nrconf{verbosity} > 1);
	      eval do { local(@ARGV, $/) = $fn; <>};
	      die "Error parsing $fn: $@" if($@);
      }
}
"""


def section(text, start, end):
    m = re.search(re.escape(start) + r".*?(?=" + re.escape(end) + ")", text, re.S)
    assert m, f"section {start!r} not found"
    return m.group(0)


BLOCK = section(BOOTSTRAP, "# ── needrestart:", "# ── GitHub CLI apt repo")
DECIDES = section(BOOTSTRAP, "needrestart_decides() {", "\nnr_tmp=") + "\n"
COLLECT_NR = section(COLLECT, 'echo "@@@ needrestart"', 'echo "@@@ reboot_required"')


class Static(unittest.TestCase):
    def test_dropin_path_and_override_line(self):
        self.assertIn('NEEDRESTART_DROPIN="${NEEDRESTART_DROPIN:-/etc/needrestart/conf.d/50-actions-runner.conf}"',
                      BLOCK)
        body = section(BLOCK, "<<'NEEDRESTART_CONF'\n", "\nNEEDRESTART_CONF\n")
        self.assertEqual([ln for ln in body.splitlines() if not ln.startswith("#")][1:], [DROPIN_LINE])

    def test_written_before_the_first_apt_get(self):
        self.assertLess(BOOTSTRAP.index("# ── needrestart:"), BOOTSTRAP.index("apt-get"))


@unittest.skipUnless(shutil.which("perl"), "perl not installed")
class Behaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.confd = self.tmp / "conf.d"
        self.conf = self.tmp / "needrestart.conf"
        self.conf.write_text(STUB_CONF.replace("@CONFD@", str(self.confd)))
        self.dropin = self.confd / "50-actions-runner.conf"

    def run_block(self, conf=None):
        env = dict(os.environ, NEEDRESTART_CONF=str(conf or self.conf),
                   NEEDRESTART_DROPIN=str(self.dropin))
        return subprocess.run(["bash", "-c", "set -euo pipefail\n" + BLOCK], env=env,
                              capture_output=True, text=True, timeout=30)

    def decide(self, *units):
        p = subprocess.run(["bash", "-c", DECIDES + 'needrestart_decides "$@"', "x",
                            str(self.conf), *units], capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return dict(ln.split() for ln in p.stdout.splitlines())

    def collect(self):
        script = COLLECT_NR.replace("/etc/needrestart/needrestart.conf", str(self.conf))
        p = subprocess.run(["bash", "-c", "have() { command -v \"$1\" >/dev/null 2>&1; }\n"
                            "absent() { echo \"__ABSENT__ $*\"; }\n" + script],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.splitlines()[1:]

    def test_the_stock_config_alone_restarts_runners(self):
        # What CI-2 did on 2026-09-28; guards the tests below against passing vacuously.
        self.assertEqual(set(self.decide(*RUNNERS).values()), {"restart"})
        self.assertEqual(self.collect(), ["runner_restart restart"])

    def test_writes_verifies_and_skips_only_runner_units(self):
        p = self.run_block()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("wrote", p.stdout)
        self.assertIn("verified: needrestart skips actions.runner.* units", p.stdout)
        self.assertEqual(self.dropin.stat().st_mode & 0o777, 0o644)
        self.assertIn(DROPIN_LINE + "\n", self.dropin.read_text())
        got = self.decide(*RUNNERS, "ssh.service", "containerd.service", "dbus.service",
                          "my-actions.runner.x.service")
        self.assertEqual(got, {**{u: "skip" for u in RUNNERS}, "ssh.service": "restart",
                               "containerd.service": "restart", "dbus.service": "skip",
                               "my-actions.runner.x.service": "restart"})
        self.assertEqual(self.collect(), ["runner_restart skip"])

    def test_idempotent(self):
        self.assertEqual(self.run_block().returncode, 0)
        first = self.dropin.read_text()
        os.utime(self.dropin, (1, 1))
        p = self.run_block()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("already up to date", p.stdout)
        self.assertEqual(self.dropin.stat().st_mtime, 1)   # not rewritten
        self.dropin.write_text("# hand-edited\n")
        p = self.run_block()
        self.assertIn("wrote", p.stdout)
        self.assertEqual(self.dropin.read_text(), first)

    def test_a_broken_drop_in_fails_the_run(self):
        self.confd.mkdir()
        (self.confd / "10-broken.conf").write_text("this is { not perl\n")
        p = self.run_block()
        self.assertEqual(p.returncode, 1)
        self.assertIn("ERROR: needrestart would still restart runner units", p.stderr)
        self.assertEqual(self.collect(), ["__ABSENT__ needrestart config does not evaluate"])

    def test_a_drop_in_undoing_the_override_fails_the_run(self):
        self.confd.mkdir()
        (self.confd / "90-reset.conf").write_text("$nrconf{override_rc} = {};\n")
        p = self.run_block()
        self.assertEqual(p.returncode, 1)
        self.assertIn("ERROR: needrestart would still restart runner units", p.stderr)

    def test_without_needrestart_the_drop_in_is_still_written(self):
        p = self.run_block(conf=self.tmp / "absent.conf")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("needrestart not installed", p.stdout)
        self.assertTrue(self.dropin.exists())
        self.conf = self.tmp / "absent.conf"
        self.assertEqual(self.collect(), ["__ABSENT__ needrestart not installed"])


if __name__ == "__main__":
    unittest.main()
