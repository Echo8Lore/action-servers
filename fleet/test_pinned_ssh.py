#!/usr/bin/env python3
"""Tests for fleet/pinned-ssh.sh and its callers (OPS-41; stdlib unittest).

Run from the repo root:  python3 -m unittest discover -s fleet -v

What they pin:
  1. Against a real sshd (run as `sshd -i` behind a ProxyCommand: no port, no network),
     the pinned options connect when fleet/known_hosts sits under a path containing a
     space, and a wrong pin is still refused as a host-key failure. ssh splits
     UserKnownHostsFile on spaces, so the value must carry its own quotes.
  2. Every fleet SSH path uses the pinned options: runner-health.yml, runner-restart.yml
     and fleet/collect-host.sh (which fleet-inventory.yml runs) call pinned_ssh_opts and
     pinned_ssh_check, every ssh they run starts with "${PINNED_SSH_OPTS[@]}", and none
     of them (nor fleet-inventory.yml) carries accept-new, ssh-keyscan, its own
     StrictHostKeyChecking, or appleboy/ssh-action. collect-host.sh's behaviour with a
     stub ssh is covered in test_fleet_facts.py; this is the static half, for the inline
     ssh in the workflows that nothing else runs.
"""

import getpass
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
WORKFLOWS = ROOT / ".github" / "workflows"
PINNED_SSH = HERE / "pinned-ssh.sh"
SSHD = shutil.which("sshd") or ("/usr/sbin/sshd" if os.access("/usr/sbin/sshd", os.X_OK) else None)


def code_lines(path):
    """Non-comment lines (full-line # comments dropped; they may name what is banned)."""
    return [ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#")]


@unittest.skipUnless(SSHD and shutil.which("ssh") and shutil.which("ssh-keygen"),
                     "needs OpenSSH client and server binaries")
class RealSshd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = pathlib.Path(self.tmp.name)
        for name in ("hostkey", "userkey", "otherkey"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.t / name)],
                           check=True)
        auth = self.t / "authorized_keys"
        auth.write_text((self.t / "userkey.pub").read_text())
        auth.chmod(0o600)
        self.cfg = self.t / "sshd config"
        self.cfg.write_text(f"HostKey {self.t / 'hostkey'}\n"
                            f"AuthorizedKeysFile {auth}\n"
                            "StrictModes no\nUsePAM no\nPidFile none\n"
                            "PasswordAuthentication no\nKbdInteractiveAuthentication no\n")

    def tearDown(self):
        self.tmp.cleanup()

    def pin(self, dirname, keyname):
        d = self.t / dirname / "fleet"
        d.mkdir(parents=True, exist_ok=True)
        kh = d / "known_hosts"
        ktype, blob = (self.t / f"{keyname}.pub").read_text().split()[:2]
        kh.write_text(f"# test pin\nh {ktype} {blob}\n")
        return kh

    def connect(self, known_hosts):
        script = ('source "$PINNED_SSH"; pinned_ssh_opts h "$KEY" || exit 9\n'
                  'rc=0; ssh -F none "${PINNED_SSH_OPTS[@]}" -o ProxyCommand="$PROXY" '
                  '"$REMOTE_USER@h" "echo REMOTE_OK" 2> "$ERR" || rc=$?\n'
                  'krc=0; pinned_ssh_check h "$rc" "$ERR" || krc=$?\n'
                  'echo "rc=$rc krc=$krc"\n')
        env = dict(os.environ, PINNED_SSH=str(PINNED_SSH), PINNED_KNOWN_HOSTS=str(known_hosts),
                   KEY=str(self.t / "userkey"), ERR=str(self.t / "ssh.err"),
                   REMOTE_USER=getpass.getuser(), PROXY=f"{SSHD} -i -f '{self.cfg}'")
        p = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True,
                           timeout=60)
        return p.stdout, (self.t / "ssh.err").read_text() if (self.t / "ssh.err").exists() else ""

    def test_path_with_space_connects(self):
        out, err = self.connect(self.pin("My Repos", "hostkey"))
        self.assertIn("REMOTE_OK", out, err)
        self.assertIn("rc=0 krc=0", out, err)

    def test_path_without_space_connects(self):
        out, err = self.connect(self.pin("repo", "hostkey"))
        self.assertIn("rc=0 krc=0", out, err)

    def test_wrong_pin_under_space_path_is_a_host_key_failure(self):
        out, err = self.connect(self.pin("My Repos", "otherkey"))
        self.assertNotIn("REMOTE_OK", out)
        self.assertIn("krc=2", out, err)
        self.assertIn("::error::h: HOST KEY VERIFICATION FAILED", out)


class FleetSshWiring(unittest.TestCase):
    CALLERS = {
        "runner-health.yml": WORKFLOWS / "runner-health.yml",
        "runner-restart.yml": WORKFLOWS / "runner-restart.yml",
        "collect-host.sh": HERE / "collect-host.sh",
    }
    FLEET_INVENTORY = WORKFLOWS / "fleet-inventory.yml"
    # An ssh (or collect-host.sh's overridable $SSH_CMD) run as a command: followed by
    # an argument, not by `;`, `)` etc. (e.g. `verdict ssh ;;`). ssh-keygen/ssh-agent
    # don't match (no space after "ssh").
    SSH_CALL = re.compile(r'(?<![\w/.$-])(?:ssh|\$SSH_CMD|\$\{SSH_CMD\})[ \t]+(?=[^\s;|&)#])')

    def ssh_calls(self, path):
        calls = []
        for ln in code_lines(path):
            code = ln.strip()
            if code.startswith("echo") and "$(" not in code and "`" not in code:
                continue  # a printed message, not a command
            for m in self.SSH_CALL.finditer(code):
                calls.append((code, code[m.end():]))
        return calls

    def test_no_trust_on_first_use_or_ssh_action(self):
        for name, path in {**self.CALLERS, "fleet-inventory.yml": self.FLEET_INVENTORY}.items():
            code = "\n".join(code_lines(path))
            for banned in ("accept-new", "ssh-keyscan", "StrictHostKeyChecking",
                           "appleboy/ssh-action", "UserKnownHostsFile"):
                # assertFalse, not assertNotIn: the message names the file, not its text.
                self.assertFalse(banned in code, f"{name}: {banned} (use fleet/pinned-ssh.sh)")
            self.assertIsNone(re.search(r"uses:\s*\S*ssh", code), f"{name}: an SSH action")

    def test_callers_use_pinned_ssh_opts_and_check(self):
        for name, path in self.CALLERS.items():
            code = "\n".join(code_lines(path))
            self.assertRegex(code, r"source \S*pinned-ssh\.sh", name)
            self.assertRegex(code, r'pinned_ssh_opts "\$HOST_ID"', name)
            self.assertRegex(code, r'pinned_ssh_check "\$HOST_ID" "\$rc"', name)

    def test_every_ssh_starts_with_the_pinned_options(self):
        for name, path in self.CALLERS.items():
            calls = self.ssh_calls(path)
            self.assertTrue(calls, f"{name}: no ssh call found (matcher out of date?)")
            for code, rest in calls:
                self.assertTrue(rest.startswith('"${PINNED_SSH_OPTS[@]}"'), f"{name}: {code}")

    def test_collect_host_ssh_cmd_defaults_to_ssh(self):
        self.assertTrue('SSH_CMD="${SSH_CMD:-ssh}"' in (HERE / "collect-host.sh").read_text())

    def test_fleet_inventory_sshes_only_through_collect_host(self):
        self.assertEqual(self.ssh_calls(self.FLEET_INVENTORY), [])
        self.assertTrue("run: bash fleet/collect-host.sh" in self.FLEET_INVENTORY.read_text(),
                        "fleet-inventory.yml no longer runs fleet/collect-host.sh")

    def test_matcher(self):
        # Self-test so a matcher regression can't quietly turn the checks above vacuous.
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write('ssh "${PINNED_SSH_OPTS[@]}" u@h true\n'
                    'X=$(ssh -o BatchMode=yes u@h true)\n'
                    'echo "$(ssh u@h true)"\n'
                    'echo "`ssh u@h true`"\n'
                    'a && ssh u@h\n'
                    '$SSH_CMD -i k u@h\n'
                    '# ssh u@h (comment)\n'
                    'echo "try: ssh u@h"\n'
                    'verdict ssh ;;\n'
                    'ssh-keygen -l -f x\n'
                    'eval "$(ssh-agent -s)"\n')
        try:
            rests = [r for _, r in self.ssh_calls(pathlib.Path(f.name))]
        finally:
            os.unlink(f.name)
        self.assertEqual(rests, ['"${PINNED_SSH_OPTS[@]}" u@h true', "-o BatchMode=yes u@h true)",
                                 'u@h true)"', 'u@h true`"', "u@h", "-i k u@h"])


if __name__ == "__main__":
    unittest.main()
