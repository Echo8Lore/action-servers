#!/usr/bin/env python3
"""Tests for the committed fleet/known_hosts (OPS-33; stdlib unittest).

Run from the repo root:  python3 -m unittest discover -s fleet -v

What they pin:
  1. Every host in fleet/inventory.yml has exactly one pinned key, and every pin
     belongs to an inventory host (a removed host's key is removed too).
  2. Entries are keyed by host id: no address, no `ssh-keyscan -H` hash (a hashed
     IPv4 entry is brute-forceable back to the address; this repo is public).
  3. Each key is a well-formed ssh-ed25519 blob, and the SHA256 fingerprints listed in
     the header comment match the keys (so the comment cannot drift from the pins).
"""

import base64
import hashlib
import pathlib
import re
import struct
import unittest

HERE = pathlib.Path(__file__).resolve().parent
KNOWN_HOSTS = HERE / "known_hosts"
INVENTORY = HERE / "inventory.yml"


def inventory_host_ids():
    # No YAML parser in the stdlib: the hosts[] ids are the only "  - id:" lines.
    return re.findall(r"^  - id:\s*([A-Za-z0-9._-]+)", INVENTORY.read_text(), re.M)


def pins():
    out = []
    for ln in KNOWN_HOSTS.read_text().splitlines():
        if ln.strip() and not ln.lstrip().startswith("#"):
            out.append(ln.split())
    return out


def fingerprint(blob_b64):
    digest = hashlib.sha256(base64.b64decode(blob_b64)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class KnownHosts(unittest.TestCase):
    def test_one_pin_per_inventory_host(self):
        ids = inventory_host_ids()
        self.assertTrue(ids, "no hosts parsed from inventory.yml")
        names = [p[0] for p in pins()]
        self.assertEqual(sorted(names), sorted(ids))
        self.assertEqual(len(names), len(set(names)), "duplicate pin")

    def test_keyed_by_id_not_address(self):
        for p in pins():
            name = p[0]
            self.assertFalse(name.startswith("|1|"), f"hashed entry: {name}")
            self.assertNotRegex(name, r"^\[?\d+\.\d+\.\d+\.\d+", "IPv4 address")
            self.assertNotIn(":", name, "IPv6 address or [host]:port")
            self.assertNotIn(",", name, "multiple names/addresses on one line")
            self.assertNotIn("@", name, "marker (@cert-authority/@revoked) not expected")

    def test_keys_are_well_formed_ed25519(self):
        for p in pins():
            self.assertEqual(len(p), 3, f"expected '<id> <type> <key>': {p}")
            _, ktype, blob = p
            self.assertEqual(ktype, "ssh-ed25519")
            raw = base64.b64decode(blob, validate=True)
            (n,) = struct.unpack(">I", raw[:4])
            self.assertEqual(raw[4:4 + n].decode(), ktype)
            (m,) = struct.unpack(">I", raw[4 + n:8 + n])
            self.assertEqual(m, 32)
            self.assertEqual(len(raw), 8 + n + m)

    def test_header_fingerprints_match_keys(self):
        listed = dict(re.findall(r"^#\s+([A-Za-z0-9._-]+)\s+(SHA256:\S+)\s*$",
                                 KNOWN_HOSTS.read_text(), re.M))
        actual = {p[0]: fingerprint(p[2]) for p in pins()}
        self.assertEqual(listed, actual)


if __name__ == "__main__":
    unittest.main()
