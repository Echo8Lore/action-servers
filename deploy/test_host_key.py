#!/usr/bin/env python3
"""Tests for deploy/host-key.sh and its use in deploy.sh / deploy.yml (OPS-38; stdlib).

Run from the repo root:  python3 -m unittest discover -s deploy -v

What they pin:
  1. VPS_HOST_KEY parsing: `<type> <base64>` and `<host> <type> <base64>` lines are
     rewritten under the deploy-target alias (the host field, possibly an address, is
     dropped); blank/# lines and CRLF are tolerated; anything malformed fails loudly and
     never falls back to unpinned. Unset warns and stays on accept-new.
  2. The ssh options: pinned = StrictHostKeyChecking=yes against the pin file only;
     unpinned = accept-new (per-run file in the workflow, the old default in deploy.sh).
  3. A host-key failure is classified loudly without echoing ssh's stderr (address).
  4. deploy.sh runs a connect-only preflight with the pinned options and stops there on
     a mismatch; every later ssh uses the same options.
  5. deploy.yml: the host-key step runs before any step that talks to the host, every
     ssh/rsync in the deploy job carries the shared options, and ssh-keyscan is gone.
ssh (and node, for deploy.sh's config reader) are stubbed; ssh-keygen is the real one.
Nothing here touches the network.
"""

import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
HELPER = HERE / "host-key.sh"
DEPLOY_SH = HERE / "deploy.sh"
DEPLOY_YML = ROOT / ".github" / "workflows" / "deploy.yml"
RESTART_YML = ROOT / ".github" / "workflows" / "runner-restart.yml"

ADDR = "192.0.2.50"  # TEST-NET-1: must never reach any output
ED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDCudSiOiYSZ8iHzEm+r5pwFYXNktWqGFW3aXJMbn/pY"
ED_FP = "SHA256:nEHWQurrW4s3cqbmkaHeoWy1Qmjgky5HTxA7jWyIYT4"
ED2 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBcGV2xCf9Ahatfo3XdK1v5DHs9HFDpJT/rwE1QTeONs"

HAVE_KEYGEN = shutil.which("ssh-keygen") is not None


def bash(script, **env):
    e = dict(os.environ)
    e.pop("VPS_HOST_KEY", None)
    e.update(env)
    return subprocess.run(["bash", "-c", f'source "{HELPER}"\n{script}'],
                          capture_output=True, text=True, env=e, timeout=30)


@unittest.skipUnless(HAVE_KEYGEN, "ssh-keygen not installed")
class Setup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.kh = pathlib.Path(self.tmp.name) / "kh"

    def tearDown(self):
        self.tmp.cleanup()

    def setup(self, key):
        p = bash(f'deploy_host_key_setup "{self.kh}"; rc=$?; echo "rc=$rc pinned=$DEPLOY_HOST_PINNED"',
                 VPS_HOST_KEY=key)
        return p

    def pins(self):
        return [ln.split() for ln in self.kh.read_text().splitlines()]

    def test_unset_warns_and_stays_unpinned(self):
        for key in ("", "  \n\t"):
            p = self.setup(key)
            self.assertIn("rc=0 pinned=false", p.stdout)
            self.assertIn("::warning::Deploy host key NOT verified", p.stdout)
            self.assertFalse(self.kh.exists())

    def test_bare_type_and_key(self):
        p = self.setup(ED)
        self.assertIn("rc=0 pinned=true", p.stdout)
        self.assertEqual(self.pins(), [["deploy-target"] + ED.split()])
        self.assertIn(ED_FP, p.stdout)
        self.assertNotIn("::warning::", p.stdout)
        self.assertEqual(self.kh.stat().st_mode & 0o777, 0o600)

    def test_host_field_is_dropped_and_never_printed(self):
        for host in (ADDR, f"[{ADDR}]:2222", "|1|abc=|def=", "vps.example.com," + ADDR):
            p = self.setup(f"{host} {ED} some comment")
            self.assertIn("rc=0 pinned=true", p.stdout, host)
            self.assertEqual(self.pins(), [["deploy-target"] + ED.split()])
            self.assertNotIn(ADDR, p.stdout + p.stderr + self.kh.read_text())

    def test_multiple_lines_comments_blanks_crlf(self):
        key = f"# pinned 2026-09-26\r\n\r\n{ADDR} {ED}\r\n  {ED2}\r\n"
        p = self.setup(key)
        self.assertIn("rc=0 pinned=true", p.stdout)
        self.assertEqual(self.pins(), [["deploy-target"] + ED.split(), ["deploy-target"] + ED2.split()])

    def test_malformed_fails_closed(self):
        bad = [
            "not a key",
            "# only a comment",
            f"@cert-authority * {ED}",
            f"@revoked {ADDR} {ED}",
            "ssh-ed25519 AAAA...",                       # placeholder from the example config
            "ssh-ed25519 aGVsbG8gd29ybGQ=",              # base64, but not an ed25519 key
            f"{ED}\ngarbage line",                       # one bad line spoils the pin
            "ssh-foo AAAAC3NzaC1lZDI1NTE5",
            f"{ED}\nssh-ed25519 aGVsbG8gd29ybGQ=",       # one real key + one well-formed junk
        ]
        for key in bad:
            p = self.setup(key)
            self.assertIn("rc=1 pinned=false", p.stdout, key)
            self.assertIn("::error::VPS_HOST_KEY is set but is not valid", p.stdout, key)
            self.assertNotIn(ADDR, p.stdout + p.stderr)
            self.assertFalse(self.kh.exists(), f"half-written pin left behind: {key!r}")


class Opts(unittest.TestCase):
    def opts(self, kh, pinned):
        p = bash(f'deploy_ssh_opts "{kh}" {pinned}; printf "%s\\n" "${{DEPLOY_SSH_OPTS[@]}}"')
        self.assertEqual(p.returncode, 0, p.stderr)
        return [a for a in p.stdout.splitlines() if a != "-o"]

    def test_pinned(self):
        o = self.opts("/run/kh", "true")
        for want in ("HostKeyAlias=deploy-target", "StrictHostKeyChecking=yes",
                     'UserKnownHostsFile="/run/kh"', "GlobalKnownHostsFile=/dev/null",
                     "UpdateHostKeys=no", "CheckHostIP=no"):
            self.assertIn(want, o)
        self.assertNotIn("StrictHostKeyChecking=accept-new", o)

    def test_unpinned_per_run_file(self):
        o = self.opts("/run/kh", "false")
        self.assertIn("StrictHostKeyChecking=accept-new", o)
        self.assertIn("HostKeyAlias=deploy-target", o)
        self.assertIn('UserKnownHostsFile="/run/kh"', o)
        self.assertIn("CheckHostIP=no", o)

    def test_unpinned_no_file_is_the_old_default(self):
        self.assertEqual(self.opts("", "false"), ["StrictHostKeyChecking=accept-new"])


class Check(unittest.TestCase):
    def check(self, rc, err_text):
        with tempfile.NamedTemporaryFile("w", suffix=".err", delete=False) as f:
            f.write(err_text)
        try:
            p = bash(f'deploy_ssh_check {rc} "{f.name}"; echo "verdict=$?"')
        finally:
            os.unlink(f.name)
        return p

    def test_verdicts(self):
        banner = (f"@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\n"
                  f"Host key for {ADDR} has changed and you have requested strict checking.\n"
                  "Host key verification failed.\n")
        p = self.check(255, banner)
        self.assertIn("verdict=2", p.stdout)
        self.assertIn("::error::HOST KEY VERIFICATION FAILED", p.stdout)
        self.assertNotIn(ADDR, p.stdout + p.stderr)
        p = self.check(255, f"ssh: connect to host {ADDR} port 22: Connection refused\n")
        self.assertIn("verdict=1", p.stdout)
        self.assertNotIn("::error::", p.stdout)
        self.assertNotIn(ADDR, p.stdout)
        self.assertIn("verdict=0", self.check(0, "").stdout)


# deploy.sh reads its config with `node -e`; this stub answers the same lookups.
NODE_STUB = r'''#!/usr/bin/env python3
import json, sys
c = json.load(open(sys.argv[3]))
v = c
for k in sys.argv[4].split("."):
    v = v.get(k) if isinstance(v, dict) else None
if v is None: v = ""
elif isinstance(v, list): v = " ".join(map(str, v))
elif isinstance(v, dict): v = json.dumps(v)
sys.stdout.write(str(v))
'''


@unittest.skipUnless(HAVE_KEYGEN, "ssh-keygen not installed")
class DeploySh(unittest.TestCase):
    """Runs the real deploy.sh with stub ssh/node; stops at or just after the preflight."""

    def run_deploy(self, ssh_body, host_key=None, env_key=None):
        with tempfile.TemporaryDirectory() as tmp:
            t = pathlib.Path(tmp)
            (t / "bin").mkdir()
            for name, body in (("ssh", "#!/usr/bin/env bash\n"
                                       'printf "%s\\n" "$@" >> "$CALLS"; echo "--" >> "$CALLS"\n'
                                       + ssh_body + "\n"),
                               ("node", NODE_STUB)):
                (t / "bin" / name).write_text(body)
                (t / "bin" / name).chmod(0o755)
            d = t / "deploy"
            d.mkdir()
            shutil.copy(DEPLOY_SH, d / "deploy.sh")
            shutil.copy(HELPER, d / "host-key.sh")
            target = {"ip": ADDR, "ssh_port": 22}
            if host_key is not None:
                target["host_key"] = host_key
            cfg = {"target": target, "users": {"root": "u"},
                   "app": {"remote_path": "/nonexistent/app", "containers": ["c"]}}
            import json
            (d / "config.json").write_text(json.dumps(cfg))
            e = dict(os.environ, PATH=f"{t / 'bin'}:{os.environ['PATH']}", CALLS=str(t / "calls"))
            e.pop("VPS_HOST_KEY", None)
            if env_key is not None:
                e["VPS_HOST_KEY"] = env_key
            p = subprocess.run(["bash", str(d / "deploy.sh"), "--yes"], cwd=tmp,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, env=e, timeout=60)
            calls = (t / "calls").read_text() if (t / "calls").exists() else ""
            return p, [c.strip().splitlines() for c in calls.split("--\n") if c.strip()]

    def test_wrong_pin_stops_at_preflight(self):
        p, calls = self.run_deploy('echo "Host key verification failed." >&2; exit 255', host_key=ED)
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error::HOST KEY VERIFICATION FAILED", p.stdout)
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0][-1], "true")            # connect-only, no command
        self.assertIn("StrictHostKeyChecking=yes", calls[0])
        self.assertNotIn("Files synced", p.stdout)

    def test_pinned_options_reach_every_later_ssh(self):
        # Preflight passes; the next ssh (the first real command) fails and stops set -e.
        p, calls = self.run_deploy('[ "${!#}" = true ] && exit 0; cat >/dev/null; exit 1', env_key=ED)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(len(calls), 2, calls)
        self.assertEqual(calls[0][-1], "true")
        for c in calls:
            self.assertIn("StrictHostKeyChecking=yes", c)
            self.assertIn("HostKeyAlias=deploy-target", c)
        self.assertIn("SSH preflight (host key pinned)", p.stdout)

    def test_env_key_overrides_config(self):
        p, calls = self.run_deploy('exit 255', host_key="garbage", env_key=ED)
        self.assertNotIn("not valid", p.stdout)
        self.assertIn("StrictHostKeyChecking=yes", calls[0])

    def test_malformed_pin_never_connects(self):
        p, calls = self.run_deploy('exit 0', host_key="ssh-ed25519 AAAA...")
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error::VPS_HOST_KEY is set but is not valid", p.stdout)
        self.assertEqual(calls, [])

    def test_every_ssh_carries_the_options(self):
        # Static: the stubbed runs stop early, so later ssh calls are checked in the source.
        seen = 0
        for ln in DEPLOY_SH.read_text().splitlines():
            code = ln.strip()
            if code.startswith("#") or code.startswith("for cmd in") or code.startswith("echo"):
                continue  # comments, the `command -v` loop, printed hints
            for m in re.finditer(r"(?:^|[\s(|;{])ssh ", code):
                seen += 1
                self.assertRegex(code[m.end():], r'^(-n )?"\$\{SSH_OPTS\[@\]\}"', code)
        self.assertGreaterEqual(seen, 6)

    def test_unpinned_warns_and_keeps_accept_new(self):
        p, calls = self.run_deploy('echo "Permission denied (publickey)." >&2; exit 255')
        self.assertEqual(p.returncode, 1)
        self.assertIn("::warning::Deploy host key NOT verified", p.stdout)
        self.assertIn("Permission denied", p.stderr)
        self.assertEqual(len(calls), 1)
        self.assertIn("StrictHostKeyChecking=accept-new", calls[0])
        self.assertNotIn("HostKeyAlias=deploy-target", calls[0])


def deploy_job_steps():
    """(name, run-block) for each step of deploy.yml's deploy job (no YAML parser)."""
    text = DEPLOY_YML.read_text()
    job = text[text.index("\n  deploy:\n"):text.index("\n  notify:\n")]
    steps = re.split(r"\n      - ", job)[1:]
    out = []
    for s in steps:
        m = re.match(r"(?:name|uses): *(.*)", s)
        out.append((m.group(1).strip() if m else s.splitlines()[0], s))
    return out


class Workflow(unittest.TestCase):
    def test_no_keyscan_or_bare_accept_new(self):
        text = DEPLOY_YML.read_text()
        code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotIn("ssh-keyscan", code)
        self.assertNotIn("StrictHostKeyChecking", code)   # only via host-key.sh

    def test_host_key_step_precedes_every_remote_step(self):
        steps = deploy_job_steps()
        names = [n for n, _ in steps]
        verify = names.index("Verify VPS host key")
        self.assertLess(names.index("Fetch host-key helper"), verify)
        for i, (name, body) in enumerate(steps):
            if i != verify and re.search(r"\b(ssh|rsync) ", body) and "run:" in body:
                self.assertGreater(i, verify, name)

    def test_every_ssh_and_rsync_uses_the_shared_options(self):
        seen = 0
        for name, body in deploy_job_steps():
            if "run:" not in body:
                continue
            for ln in body.split("run:", 1)[1].splitlines():
                code = ln.strip()
                if code.startswith("#") or code.startswith("sudo rsync"):  # remote-side rsync
                    continue
                for m in re.finditer(r"(?:^|[\s(;{])(ssh|rsync) ", code):
                    seen += 1
                    rest = code[m.end():]
                    if m.group(1) == "ssh":
                        self.assertRegex(rest, r'^(-n )?"\$\{DEPLOY_SSH_OPTS\[@\]\}"', f"{name}: {code}")
                    else:
                        self.assertIn('-e "$RSH"', rest, f"{name}: {code}")
                        self.assertIn('''RSH="ssh$(printf " '%s'" "${DEPLOY_SSH_OPTS[@]}")"''', body)
        self.assertGreaterEqual(seen, 9)

    def test_rsync_excludes_the_helper_checkout(self):
        self.assertIn("--exclude .action-servers", DEPLOY_YML.read_text())

    def test_runner_restart_notifier_follows_workflow_sha(self):
        text = RESTART_YML.read_text()
        self.assertNotIn("ref: main", text)
        self.assertEqual(text.count("ref: ${{ job.workflow_sha }}"), 2)


if __name__ == "__main__":
    unittest.main()
