#!/usr/bin/env python3
"""Static checks on deploy.yml's file sync and on addresses in the tree (OPS-39; stdlib).

Run from the repo root:  python3 -m unittest discover -s deploy -v

What they pin:
  1. The runner-to-app_temp rsync takes its excludes from a bash array expanded as
     "${EXCLUDES[@]}". The old unquoted string form handed rsync the literal pattern
     '*.db' (quotes included), so *.db files were synced. When rsync is installed, the
     EXCLUDES line from deploy.yml is run against a local tree to prove *.db and
     node_modules/ are left out.
  2. No public IPv4 address appears in any tracked file. This repo is public and real
     addresses stay in secrets; examples and tests use RFC 5737 TEST-NET addresses
     (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24) or private/loopback ranges. The
     check is by range, so the real addresses never have to be written down here.
Nothing here touches the network.
"""

import ipaddress
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
DEPLOY_YML = ROOT / ".github" / "workflows" / "deploy.yml"

IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


def deploy_step():
    """The run: script of the 'Deploy to VPS' step."""
    text = DEPLOY_YML.read_text()
    start = text.index("- name: Deploy to VPS")
    end = text.index("\n      - name:", start)
    return text[start:end]


class ExcludesArray(unittest.TestCase):
    def setUp(self):
        self.step = deploy_step()

    def excludes_line(self):
        lines = [l.strip() for l in self.step.splitlines() if l.strip().startswith("EXCLUDES=")]
        self.assertEqual(len(lines), 1, lines)
        return lines[0]

    def test_excludes_is_an_array(self):
        line = self.excludes_line()
        self.assertRegex(line, r"^EXCLUDES=\(.*\)$")
        self.assertIn("--exclude '*.db'", line)
        self.assertIn("--exclude node_modules", line)

    def test_rsync_expands_the_array_quoted(self):
        rsyncs = [l.strip() for l in self.step.splitlines()
                  if l.strip().startswith("rsync ") and "EXCLUDES" in l]
        self.assertEqual(len(rsyncs), 1, rsyncs)
        self.assertIn('"${EXCLUDES[@]}"', rsyncs[0])

    def test_no_string_expansion_left(self):
        # $EXCLUDES / ${EXCLUDES} would expand only the first element of the array.
        self.assertIsNone(re.search(r"\$\{?EXCLUDES(?!\[@\])", self.step))

    @unittest.skipUnless(shutil.which("rsync"), "rsync not installed")
    def test_excludes_really_exclude(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = pathlib.Path(tmp) / "src"
            dst = pathlib.Path(tmp) / "dst"
            (src / "node_modules").mkdir(parents=True)
            (src / "node_modules" / "x.js").write_text("x")
            (src / "server").mkdir()
            (src / "server" / "app.db").write_text("db")
            (src / "foo.db").write_text("db")
            (src / "keep.txt").write_text("k")
            dst.mkdir()
            script = f'{self.excludes_line()}\nrsync -a "${{EXCLUDES[@]}}" "$1/" "$2/"\n'
            p = subprocess.run(["bash", "-c", script, "bash", str(src), str(dst)],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertTrue((dst / "keep.txt").exists())
            self.assertFalse((dst / "foo.db").exists())
            self.assertFalse((dst / "server" / "app.db").exists())
            self.assertFalse((dst / "node_modules").exists())


@unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "not a git checkout")
class NoPublicAddresses(unittest.TestCase):
    def test_tracked_files_hold_no_public_ipv4(self):
        files = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], check=True,
                               capture_output=True, text=True).stdout.split("\0")
        hits = []
        for name in filter(None, files):
            path = ROOT / name
            if not path.is_file():
                continue
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                for m in IPV4.findall(line):
                    try:
                        ip = ipaddress.IPv4Address(m)
                    except ValueError:
                        continue  # not an address (octet > 255)
                    if ip.is_global:
                        # Report file:line only, never the address itself.
                        hits.append(f"{name}:{n}")
        self.assertEqual(hits, [], "public IPv4 address in tracked files; use RFC 5737 "
                                   "TEST-NET (192.0.2.x, 198.51.100.x, 203.0.113.x)")


if __name__ == "__main__":
    unittest.main()
