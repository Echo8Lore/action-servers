#!/usr/bin/env python3
"""Tests for ops/fleet-dispatch.sh, its systemd units and its installer (OPS-23; stdlib).

Run from the repo root:  python3 -m unittest discover -s ops -v

What they pin:
  1. fleet-dispatch.sh: the token never reaches curl's argv or environment, stdout or
     stderr (it goes on curl's stdin, -K -); workflows off the allowlist are refused
     before anything is sent; 5xx and network errors are retried, 4xx are not; every
     outcome has its documented exit code and ONE log line.
  2. The units: the service runs as wl_admin from the 0600 .env with the hardening set,
     the timer does not burst after downtime, and the rendered slot units pass
     `systemd-analyze verify` (when systemd-analyze is installed).
  3. The installer: --check renders each slot's OnCalendar from the schedule file and
     verifies it without root; and in the script, the wl_admin and root guards come
     before anything that changes the box. (Install mode itself is not executed here:
     it needs root and changes the host.)
curl is stubbed; nothing here touches the network or installs anything.
"""

import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE / "fleet-dispatch.sh"
INSTALLER = HERE / "install-fleet-dispatch.sh"
SERVICE = HERE / "systemd" / "fleet-dispatch@.service"
TIMER = HERE / "systemd" / "fleet-dispatch@.timer"

TOKEN = "github_pat_11TESTONLY0000000000_faketokenfortestsXYZ"

CURL_STUB = r"""#!/usr/bin/env bash
# Stub curl: records argv, stdin and environment per call; answers from $STUB/responses
# (line N = call N: "<http_code> <exit_code> [body]").
n=$(( $(cat "$STUB/count" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$STUB/count"
printf '%s\n' "$@" > "$STUB/argv.$n"
cat > "$STUB/stdin.$n"
env > "$STUB/env.$n"
out=""
while [ "$#" -gt 0 ]; do
  [ "$1" = "-o" ] && out="$2"
  shift
done
line=$(sed -n "${n}p" "$STUB/responses")
[ -n "$line" ] || line=$(tail -n 1 "$STUB/responses")
read -r code rc body <<<"$line"
[ -n "$out" ] && printf '%s' "${body:-}" > "$out"
if [ "$rc" != 0 ]; then echo "curl: (6) stub failure" >&2; exit "$rc"; fi
printf '%s' "$code"
"""


class Dispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stub = pathlib.Path(self.tmp.name)
        (self.stub / "bin").mkdir()
        curl = self.stub / "bin" / "curl"
        curl.write_text(CURL_STUB)
        curl.chmod(curl.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, *args, responses=("204 0",), token=TOKEN):
        (self.stub / "responses").write_text("\n".join(responses) + "\n")
        env = {"PATH": f"{self.stub / 'bin'}:/usr/bin:/bin", "STUB": str(self.stub),
               "FLEET_DISPATCH_RETRY_DELAY": "0", "HOME": self.tmp.name}
        if token is not None:
            env["FLEET_DISPATCH_TOKEN"] = token
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True,
                              text=True, env=env, timeout=30)

    def calls(self):
        c = self.stub / "count"
        return int(c.read_text()) if c.exists() else 0

    def read(self, kind, n):
        return (self.stub / f"{kind}.{n}").read_text()

    def assert_one_line(self, p):
        self.assertEqual(len(p.stdout.strip().splitlines()), 1, p.stdout)
        self.assertEqual(p.stderr, "")
        self.assertNotIn(TOKEN, p.stdout + p.stderr)

    # ── success + token hygiene ──────────────────────────────────────────────
    def test_dispatches_with_token_on_stdin_only(self):
        p = self.run_script("runner-health.yml")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assert_one_line(p)
        self.assertIn("dispatched runner-health.yml on main (HTTP 204, attempt 1)", p.stdout)
        self.assertEqual(self.calls(), 1)
        argv = self.read("argv", 1).splitlines()
        self.assertNotIn(TOKEN, "\n".join(argv))                 # not in argv
        self.assertNotIn(TOKEN, self.read("env", 1))             # not in curl's env
        self.assertIn(f'header = "Authorization: Bearer {TOKEN}"', self.read("stdin", 1))
        self.assertEqual(argv[0], "-q")                          # no ~/.curlrc
        self.assertIn("-K", argv)
        self.assertEqual(argv[argv.index("-K") + 1], "-")
        self.assertIn("https://api.github.com/repos/Echo8Lore/action-servers/actions/"
                      "workflows/runner-health.yml/dispatches", argv)
        self.assertEqual(argv[argv.index("--data") + 1], '{"ref":"main"}')
        self.assertIn("--max-time", argv)
        self.assertIn("--connect-timeout", argv)

    def test_queue_watchdog_is_allowed(self):
        p = self.run_script("queue-watchdog.yml")
        self.assertEqual(p.returncode, 0)
        self.assertIn("workflows/queue-watchdog.yml/dispatches", self.read("argv", 1))

    # ── allowlist / usage ────────────────────────────────────────────────────
    def test_allowlist_refuses_other_workflows_before_sending(self):
        for wf in ("deploy.yml", "runner-restart.yml", "../../x", "runner-health.yml/../deploy.yml",
                   "RUNNER-HEALTH.YML", "runner-health", "runner-health.yml\nfake line"):
            p = self.run_script(wf)
            self.assertEqual(p.returncode, 2, wf)
            self.assertIn("REFUSED", p.stdout)
            self.assert_one_line(p)
        self.assertEqual(self.calls(), 0)

    def test_usage(self):
        for args in ((), ("runner-health.yml", "queue-watchdog.yml"), ("",)):
            p = self.run_script(*args)
            self.assertEqual(p.returncode, 2, args)
        self.assertEqual(self.calls(), 0)

    # ── token missing / malformed ────────────────────────────────────────────
    def test_missing_token_exit_3(self):
        for tok in (None, ""):
            p = self.run_script("runner-health.yml", token=tok)
            self.assertEqual(p.returncode, 3)
            self.assertIn("NOT CONFIGURED", p.stdout)
        self.assertEqual(self.calls(), 0)

    def test_malformed_token_exit_3_and_not_echoed(self):
        for tok in ('abc"def', "abc def", "abc\nurl = http://evil", "tok$(id)"):
            p = self.run_script("runner-health.yml", token=tok)
            self.assertEqual(p.returncode, 3, tok)
            self.assertNotIn(tok, p.stdout + p.stderr)
        self.assertEqual(self.calls(), 0)

    # ── retry policy ─────────────────────────────────────────────────────────
    def test_retries_5xx_then_succeeds(self):
        p = self.run_script("runner-health.yml", responses=("502 0", "503 0", "204 0"))
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.calls(), 3)
        self.assertIn("attempt 3", p.stdout)
        self.assert_one_line(p)

    def test_retries_network_error_then_succeeds(self):
        p = self.run_script("runner-health.yml", responses=("000 6", "204 0"))
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.calls(), 2)

    def test_gives_up_after_three_5xx_exit_5(self):
        p = self.run_script("runner-health.yml", responses=("500 0",))
        self.assertEqual(p.returncode, 5)
        self.assertEqual(self.calls(), 3)
        self.assertIn("FAILED runner-health.yml: HTTP 500 after 3 attempts", p.stdout)
        self.assert_one_line(p)

    def test_gives_up_after_three_network_errors_exit_5(self):
        p = self.run_script("runner-health.yml", responses=("000 28",))
        self.assertEqual(p.returncode, 5)
        self.assertEqual(self.calls(), 3)
        self.assertIn("curl exit 28", p.stdout)
        self.assertNotIn("stub failure", p.stdout + p.stderr)   # curl stderr not echoed

    def test_4xx_not_retried_exit_4(self):
        for code in ("401", "403", "404", "422", "429"):
            (self.stub / "count").unlink(missing_ok=True)
            body = '{"message":"Bad credentials","documentation_url":"https://docs"}'
            p = self.run_script("runner-health.yml", responses=(f"{code} 0 {body}",))
            self.assertEqual(p.returncode, 4, code)
            self.assertEqual(self.calls(), 1, code)
            self.assertIn(f"REJECTED runner-health.yml: HTTP {code}", p.stdout)
            self.assertIn('"Bad credentials"', p.stdout)
            self.assert_one_line(p)

    def test_script_has_no_xtrace(self):
        src = SCRIPT.read_text()
        self.assertNotRegex(src, r"(?m)^\s*set\s+-[a-z]*x")
        self.assertNotIn("bash -x", src)


class Units(unittest.TestCase):
    def directives(self, path):
        out = {}
        for line in path.read_text().splitlines():
            if line and not line.startswith(("#", "[")) and "=" in line:
                k, v = line.split("=", 1)
                out.setdefault(k, []).append(v)
        return out

    def test_service(self):
        d = self.directives(SERVICE)
        self.assertEqual(d["Type"], ["oneshot"])
        self.assertEqual(d["User"], ["wl_admin"])
        self.assertEqual(d["EnvironmentFile"], ["/home/wl_admin/.config/fleet-dispatch/.env"])
        self.assertEqual(d["ExecStart"], ["/usr/local/bin/fleet-dispatch %i.yml"])
        for k, v in (("NoNewPrivileges", "yes"), ("ProtectSystem", "strict"),
                     ("ProtectHome", "yes"), ("PrivateTmp", "yes"),
                     ("PrivateDevices", "yes"), ("CapabilityBoundingSet", "")):
            self.assertEqual(d.get(k), [v], k)
        self.assertNotIn("DynamicUser", d)   # no new users (SECURITY_POLICY rule 6)

    def test_timer(self):
        d = self.directives(TIMER)
        self.assertEqual(d["Persistent"], ["false"])
        self.assertEqual(d["RandomizedDelaySec"], ["0"])
        self.assertIn("AccuracySec", d)
        self.assertNotIn("OnCalendar", d)    # per slot, from the installer's drop-in
        self.assertEqual(d["WantedBy"], ["timers.target"])


class Installer(unittest.TestCase):
    def check(self, *args):
        return subprocess.run(["bash", str(INSTALLER), "--check", *args],
                              capture_output=True, text=True, timeout=60)

    def test_check_renders_each_slot(self):
        want = {"a": {"runner-health": "*:00:00", "queue-watchdog": "*:00/20:00"},
                "b": {"runner-health": "*:30:00", "queue-watchdog": "*:10/20:00"}}
        for slot, timers in want.items():
            p = self.check("--slot", slot)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            for name, cal in timers.items():
                self.assertRegex(p.stdout, rf"fleet-dispatch@{name}\.timer\s+"
                                           rf"OnCalendar=\*-\*-\* {re.escape(cal)} UTC")
            if shutil.which("systemd-analyze"):
                self.assertIn("systemd-analyze verify: ok", p.stdout)

    def test_check_requires_a_valid_slot(self):
        for args in ((), ("--slot", "c"), ("--slot", "")):
            p = self.check(*args)
            self.assertEqual(p.returncode, 2, args)

    def test_unknown_argument_is_usage(self):
        p = subprocess.run(["bash", str(INSTALLER), "--check", "--bogus"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 2)

    def test_guards_precede_every_change(self):
        """Source order: the wl_admin and root guards sit above the first command that
        changes the box, so a box without wl_admin (or a non-root caller) is refused
        before anything is written."""
        lines = INSTALLER.read_text().splitlines()
        body = [(i, ln) for i, ln in enumerate(lines) if not ln.lstrip().startswith("#")]
        guard_user = next(i for i, ln in body if 'id -u "$RUN_USER"' in ln and "if !" in ln)
        guard_root = next(i for i, ln in body if '"$(id -u)" -ne 0' in ln)
        mutating = re.compile(r"\b(systemctl|install -[dm]|chown|mv -f|rm -[rf]+|rmdir)\b")
        check_end = next(i for i, ln in body if ln.strip() == "exit 0")  # end of --check
        first_change = next(i for i, ln in body if i > check_end and mutating.search(ln)
                            and "list-timers" not in ln and "()" not in ln)
        self.assertLess(guard_user, first_change)
        self.assertLess(guard_root, first_change)
        self.assertIn("REFUSED: no ${RUN_USER} user on this box", lines[guard_user + 1])

    def test_token_never_echoed(self):
        src = INSTALLER.read_text()
        self.assertIn("read -rs", src)
        self.assertNotRegex(src, r"(?m)^\s*set\s+-[a-z]*x")
        self.assertNotRegex(src, r"echo[^\n]*\$TOKEN")
        self.assertIn("umask 077", src)


if __name__ == "__main__":
    unittest.main()
