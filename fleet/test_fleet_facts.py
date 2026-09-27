#!/usr/bin/env python3
"""Tests for fleet/fleet_facts.py and fleet/collect-host.sh (stdlib unittest).

Run from the repo root:  python3 -m unittest discover -s fleet -v

What they pin:
  1. The public host JSON and the fleet report never contain IPs, listening ports,
     container names/images or nginx server_names (the repo is public).
  2. Runner drift: missing / misplaced / extra / unverified.
  3. Degraded input never takes the pipeline down: empty or absent sections, truncated
     collector output, malformed host files, a bad GPG key, a failed SSH.
  4. Package parity (OPS-46): apt-mark showmanual is compared between hosts of the
     same role (runner hosts = "ci"; hosting-vps has no role). Only well-formed names
     are kept; a host with no role publishes none; the 30-day report carries only
     counts plus the names that differ.
  5. Host keys (OPS-33): the driver only connects with the pinned-key options, refuses
     a host with no pin, and fails loudly (without leaking an address) on a mismatch.
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
@@@ apt_manual
docker-ce
gh
libzbar0t64
203.0.113.10
Not A Package; rm -rf
__END__
@@@ end
"""

SECTIONS = ["hostname", "os_release", "kernel", "uptime", "disk_root", "meminfo", "ip_addrs",
            "runner_units", "runner_unit_files", "nginx_server_names", "docker", "listening_tcp",
            "apt_manual"]

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


def apt_host(hid, pkgs):
    h = ok_host(hid, [])
    h["apt_manual"] = {"state": "present", "packages": sorted(pkgs)}
    return h


# The fleet as of 2026-09-27 (OPS-46): CI-2 was bootstrapped fresh and then topped up
# by hand, so it has everything CI-1 has plus three packages bootstrap-host.sh installs.
BASE = {"build-essential", "curl", "docker-buildx-plugin", "docker-ce", "gh", "git", "htop",
        "jq", "libzbar0t64", "python3-pip", "python3-venv"}
INV = {"hosts": [{"id": "ovh-staging"}, {"id": "ovh-devops-001"}, {"id": "hosting-vps"}],
       "runners": [{"host": "ovh-staging", "systemd_unit": "actions.runner.A.a.service"},
                   {"host": "ovh-devops-001", "systemd_unit": "actions.runner.B.b.service"}]}


class PackageParity(unittest.TestCase):
    def fleet(self):
        return [apt_host("ovh-staging", BASE),
                apt_host("ovh-devops-001", BASE | {"docker-compose-plugin", "unzip", "wget"}),
                {**ok_host("hosting-vps", []), "apt_manual": {"state": "not_compared", "package_count": 1}}]

    def report(self, hosts):
        with tempfile.TemporaryDirectory() as t:
            for h in hosts:
                (pathlib.Path(t) / f"{h['id']}.json").write_text(json.dumps(h))
            with contextlib.redirect_stdout(io.StringIO()):
                return ff.build_report(INV, t, records)

    def test_report_json_carries_no_package_lists(self):
        hosts = self.fleet()
        # Even a leg that (wrongly) shipped a roleless host's list gets cut to a count.
        hosts[2] = apt_host("hosting-vps", {"nginx", "secret-sauce"})
        rep = self.report(hosts)

        def walk(x):
            if isinstance(x, dict):
                self.assertNotIn("packages", x)
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(rep)
        blob = json.dumps(rep) + ff.markdown(rep)
        for p in BASE | {"nginx", "secret-sauce"}:
            self.assertNotIn(f'"{p}"', blob)
        apt = {h["id"]: h["apt_manual"] for h in rep["hosts"]}
        self.assertEqual(apt["ovh-staging"], {"state": "present", "package_count": len(BASE)})
        self.assertEqual(apt["hosting-vps"], {"state": "present", "package_count": 2})
        # The names that differ are all the report publishes.
        self.assertEqual(sorted(d["package"] for d in rep["package_parity"][0]["differences"]),
                         ["docker-compose-plugin", "unzip", "wget"])

    def test_parse_keeps_only_package_names(self):
        full = ff.parse_raw(RAW)
        self.assertEqual(full["apt_manual"], {"state": "present",
                                              "packages": ["docker-ce", "gh", "libzbar0t64"]})
        pub = ff.make_public(full, "h", "", [], records, role="ci")
        self.assertEqual(pub["apt_manual"]["packages"], ["docker-ce", "gh", "libzbar0t64"])

    def test_roleless_host_publishes_no_names(self):
        full = ff.parse_raw(RAW)
        for role in ("", None):
            pub = ff.make_public(full, "hosting-vps", "", [], records, role=role)
            self.assertEqual(pub["apt_manual"], {"state": "not_compared", "package_count": 3})
            self.assertNotIn("libzbar0t64", json.dumps(pub))
        # The default is roleless, so a caller that forgets the role fails closed.
        self.assertNotIn("packages", ff.make_public(full, "h", "", [], records)["apt_manual"])

    def test_matrix_hosts_carry_role(self):
        got = {h["id"]: h for h in ff.matrix_hosts(INV)}
        self.assertEqual({k: v["role"] for k, v in got.items()},
                         {"ovh-staging": "ci", "ovh-devops-001": "ci", "hosting-vps": ""})
        with tempfile.TemporaryDirectory() as t:
            f = pathlib.Path(t) / "inv.json"
            f.write_text(json.dumps(dict(INV, hosts=[{"id": "ovh-staging", "ssh_secret_prefix": "DEVOPS"}])))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                ff.main(["hosts", str(f)])
        self.assertEqual(json.loads(out.getvalue()),
                         [{"id": "ovh-staging", "ssh_secret_prefix": "DEVOPS", "role": "ci"}])

    def test_old_collector_and_absent_section(self):
        old = RAW.split("@@@ apt_manual")[0] + "@@@ end\n"
        self.assertEqual(ff.parse_raw(old)["apt_manual"], {"state": "absent", "reason": "not collected"})
        gone = RAW.split("@@@ apt_manual")[0] + "@@@ apt_manual\n__ABSENT__ apt-mark not installed\n@@@ end\n"
        pub = ff.make_public(ff.parse_raw(gone), "h", "", [], records, role="ci")
        self.assertEqual(pub["apt_manual"], {"state": "absent", "reason": "apt-mark not installed"})

    def test_roles(self):
        roles = ff.host_roles(INV)
        self.assertEqual(roles, {"ovh-staging": "ci", "ovh-devops-001": "ci", "hosting-vps": None})
        inv = dict(INV, hosts=INV["hosts"][:2] + [{"id": "hosting-vps", "role": "web"}])
        self.assertEqual(ff.host_roles(inv)["hosting-vps"], "web")

    def test_ci1_lacks_what_ci2_was_bootstrapped_with(self):
        [g] = ff.package_parity(INV, self.fleet())   # hosting-vps: no role, no group
        self.assertEqual(g["role"], "ci")
        self.assertEqual(g["compared"], ["ovh-devops-001", "ovh-staging"])
        self.assertEqual(g["unverified"], [])
        self.assertEqual(g["differences"], [
            {"package": p, "present_on": ["ovh-devops-001"], "missing_on": ["ovh-staging"]}
            for p in ("docker-compose-plugin", "unzip", "wget")])

    def test_report_warns_and_summarises(self):
        rep = self.report(self.fleet())
        self.assertIn("package parity (ci): ovh-staging lacks docker-compose-plugin, unzip, wget "
                      "(installed on other ci hosts)", rep["warnings"])
        self.assertFalse(any("hosting-vps" in w or "nginx" in w for w in rep["warnings"]))
        md = ff.markdown(rep)
        self.assertIn("| unzip | ovh-devops-001 | **ovh-staging** |", md)
        self.assertIn("**ci** (ovh-staging, ovh-devops-001)", md)

    def test_parity_clean(self):
        hosts = [apt_host("ovh-staging", BASE), apt_host("ovh-devops-001", BASE)]
        [g] = ff.package_parity(INV, hosts)
        self.assertEqual(g["differences"], [])
        rep = {"generated_at": "x", "hosts": hosts, "domains": [], "drift": ff.drift(INV, hosts),
               "package_parity": [g]}
        self.assertFalse(any("package parity" in w for w in ff.warnings(rep)))
        self.assertIn("No differences between ovh-devops-001, ovh-staging.", ff.markdown(rep))

    def test_unreadable_hosts_are_unverified_not_differences(self):
        no_apt = ok_host("ovh-devops-001", [])
        [g] = ff.package_parity(INV, [apt_host("ovh-staging", BASE), no_apt])
        self.assertEqual(g["differences"], [])
        self.assertEqual(g["unverified"], [{"host": "ovh-devops-001", "reason": "apt_manual not collected"}])
        rep = {"hosts": [], "domains": [], "drift": {"missing": [], "misplaced": [], "extra": []},
               "package_parity": [g]}
        self.assertEqual(ff.warnings(rep),
                         ["package parity (ci): ovh-devops-001 not compared (apt_manual not collected)"])
        # An unreachable host is warned about once, as unreachable, not again here.
        [g] = ff.package_parity(INV, [apt_host("ovh-staging", BASE), {"id": "ovh-devops-001", "status": "unreachable"}])
        self.assertEqual(g["unverified"], [{"host": "ovh-devops-001", "reason": "unreachable"}])
        self.assertEqual(ff.warnings(dict(rep, package_parity=[g])), [])

    def test_garbage_apt_values_never_raise(self):
        for apt in (None, [], "x", {"state": "present"}, {"state": "present", "packages": [1, None, "gh"]}):
            h = ok_host("ovh-devops-001", [])
            h["apt_manual"] = apt
            ff.package_parity(INV, [apt_host("ovh-staging", BASE), h])
        pub = ff.make_public({"apt_manual": {"state": "present", "packages": [1, "gh", "10.1.2.3"]}},
                             "h", "", [], records, role="ci")
        self.assertEqual(pub["apt_manual"]["packages"], ["gh"])


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
            kh = t / "known_hosts"
            kh.write_text("# test pins\nh ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDCudSiOiYSZ8iHzEm+r5pwFYXNktWqGFW3aXJMbn/pY\n")
            e = dict(os.environ, HOST_ID="h", PREFIX="P", SSH_HOST="192.0.2.50", SSH_USER="u",
                     SSH_KEY="dummy", DOMAINS="", SSH_CMD=str(stub), OUT_DIR=str(t / "out"),
                     PINNED_KNOWN_HOSTS=str(kh), ROLE="")
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

    def test_ssh_runs_with_pinned_host_key_options(self):
        # The stub records its argv into out/ (collector output is irrelevant here).
        p, outs = self.run_host('printf "%s\\n" "$@" > "$OUT_DIR/ssh.args"; exit 255')
        args = outs["ssh.args"].splitlines()
        for opt in ("HostKeyAlias=h", "StrictHostKeyChecking=yes", "GlobalKnownHostsFile=/dev/null",
                    "UpdateHostKeys=no", "CheckHostIP=no"):
            self.assertIn(opt, args)
        # The path carries its own quotes: ssh splits UserKnownHostsFile on spaces.
        self.assertTrue(any(a.startswith('UserKnownHostsFile="') and a.endswith('known_hosts"')
                            for a in args), args)
        self.assertFalse(any("accept-new" in a for a in args))

    def test_no_pinned_key_refuses_to_connect(self):
        p, outs = self.run_host('touch "$OUT_DIR/ssh.called"; exit 0', HOST_ID="unpinned")
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error::unpinned: no pinned host key", p.stdout)
        self.assertNotIn("ssh.called", outs)
        self.assertEqual(json.loads(outs["unpinned.json"])["status"], "no_pinned_key")

    def test_pin_lookup_is_by_exact_id(self):
        # "h" is pinned; "hh" (a prefix match) is not.
        p, outs = self.run_host("exit 0", HOST_ID="hh")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(json.loads(outs["hh.json"])["status"], "no_pinned_key")

    def test_host_key_mismatch_fails_loudly_without_leaking(self):
        body = ("echo '@@@@@@@@@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@@@@@' >&2\n"
                "echo 'Host key for 198.51.100.99 has changed' >&2\n"
                "echo 'Host key verification failed.' >&2\n"
                "exit 255")
        p, outs = self.run_host(body)
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error::h: HOST KEY VERIFICATION FAILED", p.stdout)
        self.assertNotIn("198.51.100.99", p.stdout + p.stderr)
        self.assertEqual(json.loads(outs["h.json"])["status"], "host_key_mismatch")

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

    def test_role_decides_whether_the_leg_ships_package_names(self):
        body = "cat <<'EOF'\n" + RAW + "EOF"
        _, outs = self.run_host(body)                       # ROLE unset: roleless
        self.assertEqual(json.loads(outs["h.json"])["apt_manual"]["state"], "not_compared")
        self.assertNotIn("libzbar0t64", outs["h.json"])
        _, outs = self.run_host(body, ROLE="ci")
        self.assertEqual(json.loads(outs["h.json"])["apt_manual"]["packages"],
                         ["docker-ce", "gh", "libzbar0t64"])

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
