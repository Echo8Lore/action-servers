#!/usr/bin/env python3
"""Static checks on deploy.yml's file sync and on addresses in the tree (OPS-39; stdlib).

Run from the repo root:  python3 -m unittest discover -s deploy -v

What they pin:
  1. The runner-to-app_temp rsync takes its excludes from a bash array expanded as
     "${EXCLUDES[@]}". The old unquoted string form handed rsync the literal pattern
     '*.db' (quotes included), so *.db files were synced. When rsync is installed, the
     EXCLUDES line from deploy.yml is run against a local tree to prove *.db and
     node_modules/ are left out.
  2. No public IPv4 or IPv6 address appears in any tracked file. This repo is public
     and real addresses stay in secrets; examples and tests use RFC 5737 TEST-NET
     addresses (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24), RFC 3849 2001:db8::/32,
     or private/loopback ranges. The check is by range (ipaddress .is_global), so the
     real addresses never have to be written down here. Four-part version strings
     (v1.2.3.4, 1.2.3.4.5) are not addresses and are skipped.
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

# A dotted quad not glued to a word or another dot-number: skips v1.2.3.4 and
# 1.2.3.4.5, still matches an address that ends a sentence ("... 192.0.2.1.").
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\w)")
# Hex groups and colons: starts with a hex group, has at least two colons, not glued
# to a word, colon or dot (so host:port, times and 0.0.0.0:22 don't start a match).
# Leading-"::" forms (::1, ::add-mask::, a[::2]) are never public, so they're skipped.
# ipaddress decides whether it really is an address; anything it rejects (a time, a
# fingerprint) is ignored.
IPV6 = re.compile(r"(?<![\w:.])(?=[0-9A-Fa-f]{1,4}:[0-9A-Fa-f]*:)[0-9A-Fa-f:]+(?![\w:]|\.\w)")


def public_addresses(line):
    """Global (public) IPv4/IPv6 addresses in a line of text."""
    found = []
    for pattern, cls in ((IPV4, ipaddress.IPv4Address), (IPV6, ipaddress.IPv6Address)):
        for m in pattern.findall(line):
            try:
                ip = cls(m)
            except ValueError:
                continue  # not an address (octet > 255, a time, a fingerprint)
            if ip.is_global:
                found.append(ip)
    return found


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


class Matcher(unittest.TestCase):
    # Synthetic public addresses are built from parts at runtime so the tree scan
    # below doesn't flag this file. They are well-known public resolvers, not fleet hosts.
    GLOBAL4 = "8.8." + "4.4"
    GLOBAL6 = "2606:4700:" + ":1111"

    def test_documentation_and_private_ranges_pass(self):
        for line in ('"ip": "203.0.113.10"', "ssh 192.0.2.50", "198.51.100.7:8443",
                     "10.1.2.3/8", "127.0.0.1", "0.0.0.0:22", "inet6 2001:db8::5/64",
                     "fe80::1", "::1", "::"):
            self.assertEqual(public_addresses(line), [], line)

    def test_global_addresses_are_flagged(self):
        for line in (f'"ip": "{self.GLOBAL4}"', f"ends a sentence {self.GLOBAL4}.",
                     f"{self.GLOBAL4}:22", f"inet6 {self.GLOBAL6}/64",
                     f"[{self.GLOBAL6}]:443", f"to {self.GLOBAL6}."):
            self.assertEqual(len(public_addresses(line)), 1, line)

    def test_non_addresses_are_ignored(self):
        for line in ("uses: tool@v1.2.3.4", "version 8.8.4.4.1",
                     "Date: 19:39:29", "MD5:aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99",
                     "a[::2]", "echo ::add-mask::x", "std::vector", "SHA256:nEHWQurrW4s3cqbmkaHeoWy1"):
            self.assertEqual(public_addresses(line), [], line)


@unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "not a git checkout")
class NoPublicAddresses(unittest.TestCase):
    def test_tracked_files_hold_no_public_address(self):
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
                if public_addresses(line):
                    # Report file:line only, never the address itself.
                    hits.append(f"{name}:{n}")
        self.assertEqual(hits, [], "public IP address in tracked files; use RFC 5737 "
                                   "TEST-NET (192.0.2.x, 198.51.100.x, 203.0.113.x) or "
                                   "2001:db8::/32")


if __name__ == "__main__":
    unittest.main()
