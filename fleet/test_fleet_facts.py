#!/usr/bin/env python3
"""Tests for fleet/fleet_facts.py and fleet/collect-host.sh (stdlib unittest).

Run from the repo root:  python3 -m unittest discover -s fleet -v

What they pin:
  1. The public host JSON and the fleet report never contain IPs, listening ports,
     container names/images or nginx server_names (the repo is public).
  2. Runner drift: missing / misplaced / extra / unverified.
  3. Degraded input never takes the pipeline down: empty or absent sections, truncated
     collector output, malformed host files, a bad GPG key, a failed SSH.
DNS is stubbed and the driver runs with a stub ssh; nothing here touches the network.
"""

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fleet_facts as ff  # noqa: E402

# Everything in SENSITIVE is present in RAW and must never reach public output.
SENSITIVE = [
    "203.0.113.10", "2001:db8::5", "198.51.100.7", "10.1.2.3",   # addresses
    "8443", "5432",                                               # listening ports
    "secretcorp/api:1.2.3", "postgres:16.4", "prod-db-7", "api-blue",  # containers
    "staging.internal.example", "admin.weaponslore.com",          # server_names
]

RAW = """@@@ hostname
vps-test0001
@@@ os_release
PRETTY_NAME="Ubuntu 24.04.3 LTS"
ID=ubuntu
VERSION_ID="24.04"
@@@ kernel
6.8.0-45-generic
@@@ uptime
90061.20
@@@ disk_root
/dev/sda1 100000 43000 57000 43% /
@@@ meminfo
MemTotal:        4000000 kB
MemAvailable:    1000000 kB
@@@ ip_addrs
203.0.113.10/24
2001:db8::5/64
10.1.2.3/8
@@@ runner_units
actions.runner.Echo8Lore.org-runner-02.service loaded active running
@@@ runner_unit_files
actions.runner.Echo8Lore.org-runner-02.service enabled
@@@ nginx_server_names
weaponslore.com
admin.weaponslore.com
staging.internal.example
__END__
@@@ docker
prod-db-7\tpostgres:16.4
api-blue\tsecretcorp/api:1.2.3
@@@ listening_tcp
0.0.0.0:22
198.51.100.7:8443
127.0.0.1:5432
@@@ end
"""

SECTIONS = ["hostname", "os_release", "kernel", "uptime", "disk_root", "meminfo", "ip_addrs",
            "runner_units", "runner_unit_files", "nginx_server_names", "docker", "listening_tcp"]

DNS = {"weaponslore.com": ["192.0.2.1", "203.0.113.10"]}


def records(domain):
    return DNS.get(domain, [])


def assert_clean(tc, text):
    for s in SENSITIVE:
        tc.assertNotIn(s, text, f"sensitive value {s!r} leaked")


class PublicOutputHasNoSensitiveData(unittest.TestCase):
    def setUp(self):
        self.full = ff.parse_raw(RAW)
        self.pub = ff.make_public(self.full, "hosting-vps", "", ["weaponslore.com"], records)

    def test_full_really_has_the_sensitive_values(self):
        # Guards the leak test against passing vacuously.
        blob = json.dumps(self.full)
        for s in SENSITIVE:
            self.assertIn(s, blob)

    def test_public_json_is_clean(self):
        assert_clean(self, json.dumps(self.pub))

    def test_domain_matched_by_index_not_ip(self):
        d = self.pub["domains"][0]
        self.assertEqual(d["record_count"], 2)
        self.assertEqual(d["matched_record_indexes"], [1])
        self.assertTrue(d["nginx_server_name"])

    def test_report_json_and_markdown_are_clean(self):
        with tempfile.TemporaryDirectory() as t:
            (pathlib.Path(t) / "hosting-vps.json").write_text(json.dumps(self.pub))
            inv = {"hosts": [{"id": "hosting-vps"}], "domains": ["weaponslore.com"], "runners": []}
            with contextlib.redirect_stdout(io.StringIO()):
                rep = ff.build_report(inv, t, records)
            assert_clean(self, json.dumps(rep))
            assert_clean(self, ff.markdown(rep))
            self.assertEqual(rep["domains"][0]["served_by"], ["hosting-vps"])
            self.assertEqual(rep["domains"][0]["unmatched_record_count"], 1)

    def test_ips_subcommand_lists_every_address_for_masking(self):
        with tempfile.TemporaryDirectory() as t:
            f = pathlib.Path(t) / "full.json"
            f.write_text(json.dumps(self.full))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                ff.main(["ips", str(f)])
        got = set(out.getvalue().split())
        self.assertTrue({"203.0.113.10", "2001:db8::5", "10.1.2.3", "198.51.100.7"} <= got)
        self.assertNotIn("0.0.0.0", got)


def ok_host(hid, units):
    return {"id": hid, "status": "ok",
            "runner_units": [{"unit": u, "load": "loaded", "active": "active", "sub": "running"} for u in units],
            "runner_unit_files": [], "domains": []}


class Drift(unittest.TestCase):
    def test_four_classes(self):
        inv = {"runners": [
            {"name": "ok", "host": "a", "systemd_unit": "actions.runner.X.ok.service"},
            {"name": "gone", "host": "a", "systemd_unit": "actions.runner.X.gone.service"},
            {"name": "moved", "host": "b", "systemd_unit": "actions.runner.X.moved.service"},
            {"name": "dark", "host": "c", "systemd_unit": "actions.runner.X.dark.service"},
        ]}
        hosts = [ok_host("a", ["actions.runner.X.ok.service", "actions.runner.X.moved.service"]),
                 ok_host("b", ["actions.runner.Y.stray.service"]),
                 {"id": "c", "status": "not_configured"}]
        d = ff.drift(inv, hosts)
        self.assertEqual(d["missing"], [{"unit": "actions.runner.X.gone.service", "expected_host": "a"}])
        self.assertEqual(d["misplaced"], [{"unit": "actions.runner.X.moved.service",
                                           "expected_host": "b", "found_on": ["a"]}])
        self.assertEqual(d["extra"], [{"unit": "actions.runner.Y.stray.service", "host": "b"}])
        self.assertEqual(d["unverified"], [{"unit": "actions.runner.X.dark.service",
                                            "expected_host": "c", "reason": "not_configured"}])

    def test_no_drift(self):
        inv = {"runners": [{"host": "a", "systemd_unit": "actions.runner.X.ok.service"}]}
        d = ff.drift(inv, [ok_host("a", ["actions.runner.X.ok.service"])])
        self.assertFalse(any(d.values()))


class DegradedInputNeverRaises(unittest.TestCase):
    def roundtrip(self, raw):
        full = ff.parse_raw(raw)
        pub = ff.make_public(full, "h", "", ["weaponslore.com"], records)
        json.dumps(pub)
        with tempfile.TemporaryDirectory() as t:
            (pathlib.Path(t) / "h.json").write_text(json.dumps(pub))
            with contextlib.redirect_stdout(io.StringIO()):
                rep = ff.build_report({"hosts": [{"id": "h"}], "domains": ["weaponslore.com"],
                                       "runners": [{"host": "h", "systemd_unit": "actions.runner.X.a.service"}]},
                                      t, records)
            ff.markdown(rep)
        return pub, rep

    def test_empty_sections(self):
        raw = "".join(f"@@@ {s}\n" for s in SECTIONS) + "@@@ end\n"
        pub, rep = self.roundtrip(raw)
        self.assertIsNone(pub["disk_root_used_pct"])   # the reproduced crash: df with no rows
        self.assertIsNone(pub["memory_used_pct"])
        self.assertEqual(rep["drift"]["missing"][0]["unit"], "actions.runner.X.a.service")

    def test_all_absent(self):
        raw = "".join(f"@@@ {s}\n__ABSENT__ not here\n" for s in SECTIONS) + "@@@ end\n"
        pub, _ = self.roundtrip(raw)
        self.assertEqual(pub["nginx"]["state"], "absent")
        self.assertEqual(pub["docker"]["state"], "absent")
        self.assertEqual(pub["listening_tcp"]["state"], "absent")

    def test_garbage_values(self):
        raw = RAW.replace("43% /", "lots /").replace("90061.20", "soon").replace("4000000 kB", "x kB")
        self.roundtrip(raw)

    def test_degraded_full_dict(self):
        full = {"disk_root": [], "memory": [], "os": [], "nginx": None, "docker": "x",
                "listening_tcp": [], "runner_units": None, "ips": ["not-an-ip"]}
        pub = ff.make_public(full, "h", "", ["weaponslore.com"], records)
        self.assertIsNone(pub["disk_root_used_pct"])

    def test_truncated_output_is_a_parse_error_not_a_crash(self):
        # parse_raw refuses it (ValueError); collect-host.sh turns that into collect_failed
        # (see CollectHostScript.test_truncated_collector_output).
        with self.assertRaises(ValueError):
            ff.parse_raw(RAW.split("@@@ docker")[0])

    def test_empty_and_malformed_host_files_become_unparseable(self):
        with tempfile.TemporaryDirectory() as t:
            (pathlib.Path(t) / "empty.json").write_text("")
            (pathlib.Path(t) / "list.json").write_text("[1, 2]")
            (pathlib.Path(t) / "junk.json").write_text(json.dumps(
                {"id": "junk", "status": "ok", "domains": [{"domain": 5}, "x"], "runner_units": [1]}))
            inv = {"hosts": [{"id": "empty"}, {"id": "list"}, {"id": "junk"}, {"id": "gone"}],
                   "domains": ["weaponslore.com"], "runners": []}
            with contextlib.redirect_stdout(io.StringIO()):
                rep = ff.build_report(inv, t, records)
            ff.markdown(rep)
        st = {h["id"]: h["status"] for h in rep["hosts"]}
        self.assertEqual(st, {"empty": "unparseable", "list": "unparseable", "junk": "ok", "gone": "no_result"})


class CollectHostScript(unittest.TestCase):
    """Runs the real driver with a stub ssh (no network)."""

    def run_host(self, fake_ssh_body, fail_python_subcommand=None, **env):
        with tempfile.TemporaryDirectory() as t:
            t = pathlib.Path(t)
            stub = t / "ssh"
            stub.write_text("#!/usr/bin/env bash\n" + fake_ssh_body + "\n")
            stub.chmod(0o755)
            e = dict(os.environ, HOST_ID="h", PREFIX="P", SSH_HOST="192.0.2.50", SSH_USER="u",
                     SSH_KEY="dummy", DOMAINS="", SSH_CMD=str(stub), OUT_DIR=str(t / "out"))
            if fail_python_subcommand:
                # A python3 first on PATH that runs the real interpreter, except that it
                # fails the named fleet_facts.py subcommand.
                bindir = t / "bin"
                bindir.mkdir()
                py = bindir / "python3"
                py.write_text("#!/usr/bin/env bash\n"
                              f'for a in "$@"; do [ "$a" = "{fail_python_subcommand}" ] && exit 1; done\n'
                              f'exec "{sys.executable}" "$@"\n')
                py.chmod(0o755)
                e["PATH"] = f"{bindir}{os.pathsep}{e['PATH']}"
            e.update(env)
            p = subprocess.run(["bash", str(HERE / "collect-host.sh")], env=e,
                               capture_output=True, text=True, timeout=60)
            outs = {f.name: f.read_text() for f in (t / "out").glob("*")} if (t / "out").exists() else {}
        return p, outs

    def test_not_configured(self):
        p, outs = self.run_host("exit 1", SSH_KEY="")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(outs["h.json"])["status"], "not_configured")

    def test_ssh_failure_prints_no_stderr(self):
        p, outs = self.run_host("echo 'Connection closed by 198.51.100.99 port 22' >&2; exit 255")
        self.assertEqual(p.returncode, 0)
        self.assertNotIn("198.51.100.99", p.stdout + p.stderr)
        self.assertIn("SSH failed (exit 255)", p.stdout)
        self.assertEqual(json.loads(outs["h.json"])["status"], "unreachable")

    def test_truncated_collector_output(self):
        body = "cat <<'EOF'\n" + RAW.split("@@@ docker")[0] + "EOF"
        p, outs = self.run_host(body)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(outs["h.json"])["status"], "collect_failed")

    def test_failed_public_step_still_writes_valid_host_json(self):
        # Pins the collect_failed fallback: a crash in `public` must not leave an empty
        # out/<id>.json, which the report job would then be handed.
        body = "cat <<'EOF'\n" + RAW + "EOF"
        p, outs = self.run_host(body, fail_python_subcommand="public")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("h.json", outs)
        self.assertEqual(json.loads(outs["h.json"])["status"], "collect_failed")

    def test_bad_gpg_key_still_ships_public_json(self):
        body = "cat <<'EOF'\n" + RAW + "EOF"
        p, outs = self.run_host(body, GPG_PUBKEY="not a key")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(json.loads(outs["h.json"])["status"], "ok")
        self.assertNotIn("fleet-full-h.json.gpg", outs)
        self.assertIn("could not be used", p.stdout)
        # Apart from the ::add-mask:: commands themselves, the log carries no addresses.
        assert_clean(self, "\n".join(ln for ln in p.stdout.splitlines()
                                     if not ln.startswith("::add-mask::")))
        assert_clean(self, outs["h.json"])


if __name__ == "__main__":
    unittest.main()
