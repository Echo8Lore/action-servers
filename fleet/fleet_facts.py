#!/usr/bin/env python3
"""fleet_facts.py -- runner-side half of .github/workflows/fleet-inventory.yml.

Subcommands (stdlib only; runs on ubuntu-latest's python3):

  parse    RAW            -> FULL json   (everything collect-facts.sh printed, IPs included)
  ips      FULL           -> one IP per line (the workflow ::add-mask::s each of these)
  public   FULL ...       -> PUBLIC json (no IPs, ports, images or server_name lists;
                            apt package names only with a --role, see below)
  hosts    INVENTORY      -> inventory hosts[] as JSON, each with its parity "role"
                            (the collect matrix; empty role = not compared)
  missing  --id ID ...    -> PUBLIC json for a host whose secrets are not set
  failed   --id ID ...    -> PUBLIC json for a configured host that SSH could not reach
  report   INVENTORY DIR  -> fleet-report.json + markdown summary (drift + domain map
                            + package parity between hosts of the same role)

FULL never leaves the job except GPG-encrypted. PUBLIC is what the artifact and the step
summary carry, because on a public repo both are readable by any logged-in GitHub user.
"""

import argparse
import datetime as dt
import ipaddress
import json
import pathlib
import re
import socket
import sys

ABSENT = "__ABSENT__"
# A Debian package name, optionally with the ":arch" apt-mark adds for foreign arches.
PKG_NAME = re.compile(r"^[a-z0-9][a-z0-9.+-]+(:[a-z0-9-]+)?$")


# ── parse ────────────────────────────────────────────────────────────────────
def split_sections(raw):
    sections, cur = {}, None
    for line in raw.splitlines():
        if line.startswith("@@@ "):
            cur = line[4:].strip()
            sections[cur] = []
        elif cur is not None:
            sections[cur].append(line.rstrip("\n"))
    return sections


def absent_reason(lines):
    for ln in lines:
        if ln.startswith(ABSENT):
            return ln[len(ABSENT):].strip() or "absent"
    return None


def parse_raw(raw):
    s = split_sections(raw)
    if "end" not in s:
        raise ValueError("collect-facts.sh output is truncated (no '@@@ end' marker)")
    full = {}

    def simple(name):
        lines = s.get(name, [])
        r = absent_reason(lines)
        return {"state": "absent", "reason": r} if r else lines

    h = simple("hostname")
    full["hostname"] = h[0].strip() if isinstance(h, list) and h else "absent"

    osr = simple("os_release")
    if isinstance(osr, list):
        kv = {}
        for ln in osr:
            if "=" in ln:
                k, v = ln.split("=", 1)
                kv[k.strip()] = v.strip().strip('"')
        full["os"] = {"pretty_name": kv.get("PRETTY_NAME", "unknown"),
                      "id": kv.get("ID", ""), "version_id": kv.get("VERSION_ID", "")}
    else:
        full["os"] = osr

    k = simple("kernel")
    full["kernel"] = k[0].strip() if isinstance(k, list) and k else "absent"

    up = simple("uptime")
    try:
        full["uptime_seconds"] = int(float(up[0])) if isinstance(up, list) and up else None
    except (ValueError, OverflowError):
        full["uptime_seconds"] = None

    d = simple("disk_root")
    full["disk_root"] = d if isinstance(d, dict) else {"state": "absent", "reason": "no df output"}
    if isinstance(d, list) and d:
        f = d[0].split()
        try:
            size, used, avail = int(f[1]), int(f[2]), int(f[3])
            full["disk_root"] = {"size_bytes": size, "used_bytes": used, "avail_bytes": avail,
                                 "used_pct": int(f[4].rstrip("%"))}
        except (IndexError, ValueError):
            full["disk_root"] = {"state": "absent", "reason": "unparseable df output"}

    m = simple("meminfo")
    full["memory"] = m
    if isinstance(m, list):
        kv = {}
        for ln in m:
            parts = ln.replace(":", " ").split()
            if len(parts) >= 2 and parts[1].isdigit():
                kv[parts[0]] = int(parts[1]) * 1024
        if "MemTotal" in kv:
            tot, av = kv["MemTotal"], kv.get("MemAvailable", 0)
            full["memory"] = {"total_bytes": tot, "available_bytes": av,
                              "used_pct": round(100 * (tot - av) / tot) if tot else None}
        else:
            full["memory"] = {"state": "absent", "reason": "no MemTotal"}

    ips = simple("ip_addrs")
    full["ips"] = ips
    if isinstance(ips, list):
        out = []
        for ln in ips:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(str(ipaddress.ip_interface(ln).ip))
            except ValueError:
                pass
        full["ips"] = sorted(set(out))

    units = simple("runner_units")
    if isinstance(units, list):
        rows = []
        for ln in units:
            f = ln.split()
            if f:
                rows.append({"unit": f[0], "load": f[1] if len(f) > 1 else "",
                             "active": f[2] if len(f) > 2 else "",
                             "sub": f[3] if len(f) > 3 else ""})
        full["runner_units"] = rows
    else:
        full["runner_units"] = units

    uf = simple("runner_unit_files")
    if isinstance(uf, list):
        full["runner_unit_files"] = [{"unit": ln.split()[0],
                                      "enabled": ln.split()[1] if len(ln.split()) > 1 else ""}
                                     for ln in uf if ln.strip()]
    else:
        full["runner_unit_files"] = uf

    ng = s.get("nginx_server_names", [])
    r = absent_reason(ng)
    full["nginx"] = ({"state": "absent", "reason": r} if r else
                     {"state": "present",
                      "server_names": sorted({x.strip() for x in ng if x.strip() and x != "__END__"})})

    dk = s.get("docker", [])
    r = absent_reason(dk)
    if r:
        full["docker"] = {"state": "absent", "reason": r}
    else:
        cs = []
        for ln in dk:
            if ln.strip():
                name, _, image = ln.partition("\t")
                cs.append({"name": name, "image": image})
        full["docker"] = {"state": "present", "containers": cs}

    lp = s.get("listening_tcp", [])
    r = absent_reason(lp)
    if r:
        full["listening_tcp"] = {"state": "absent", "reason": r}
    else:
        ports = set()
        addrs = []
        for ln in lp:
            ln = ln.strip()
            if not ln:
                continue
            addrs.append(ln)
            try:
                ports.add(int(ln.rsplit(":", 1)[1]))
            except (IndexError, ValueError):
                pass
        full["listening_tcp"] = {"state": "present", "ports": sorted(ports), "sockets": addrs}

    full["apt_manual"] = parse_apt_manual(s)
    return full


def is_ip(x):
    try:
        ipaddress.ip_address(x)
        return True
    except ValueError:
        return False


def package_names(lines):
    """Only well-formed package names survive, so nothing else can reach the public
    report through this section (an address-shaped line is dropped too)."""
    return sorted({x.strip() for x in lines
                   if PKG_NAME.match(x.strip()) and not is_ip(x.strip())})


def parse_apt_manual(s):
    if "apt_manual" not in s:   # a collector from before OPS-46
        return {"state": "absent", "reason": "not collected"}
    lines = s["apt_manual"]
    r = absent_reason(lines)
    if r:
        return {"state": "absent", "reason": r}
    return {"state": "present", "packages": package_names(ln for ln in lines if ln != "__END__")}


# ── public ───────────────────────────────────────────────────────────────────
def resolve(name, family):
    try:
        infos = socket.getaddrinfo(name, None, family, socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    return sorted({i[4][0] for i in infos})


def target_ips(target):
    """IPs of the SSH target (a secret: IP literal or DNS name)."""
    if not target:
        return set()
    try:
        return {str(ipaddress.ip_address(target))}
    except ValueError:
        return set(resolve(target, socket.AF_INET)) | set(resolve(target, socket.AF_INET6))


def domain_records(domain):
    """Sorted A + AAAA records. Legs refer to records by index into this list."""
    return resolve(domain, socket.AF_INET) + resolve(domain, socket.AF_INET6)


def name_matches(domain, server_names):
    for sn in server_names:
        sn = sn.lower().rstrip(".")
        if sn == domain or (sn.startswith("*.") and domain.endswith(sn[1:])) \
                or (sn.startswith(".") and (domain == sn[1:] or domain.endswith(sn))):
            return True
    return False


def state_of(section):
    return section.get("state", "present") if isinstance(section, dict) else "absent"


def as_dict(x):
    """Every FULL section is read through this: a degraded section is a list, None or
    missing, and must yield "absent"/None rather than an AttributeError."""
    return x if isinstance(x, dict) else {}


def make_public(full, host_id, target, domains, records_for=domain_records, role=""):
    full = as_dict(full)
    host_ips = set()
    for ip in full.get("ips") if isinstance(full.get("ips"), list) else []:
        try:
            host_ips.add(str(ipaddress.ip_address(ip)))
        except ValueError:
            pass
    host_ips |= target_ips(target)
    nginx = as_dict(full.get("nginx"))
    names = nginx.get("server_names") if isinstance(nginx.get("server_names"), list) else []
    doms = []
    for d in domains:
        recs = records_for(d)
        matched = [i for i, ip in enumerate(recs) if ip in host_ips]
        doms.append({"domain": d, "record_count": len(recs), "matched_record_indexes": matched,
                     "nginx_server_name": name_matches(d, names) if state_of(nginx) == "present" else None})
    public_ips = [ipaddress.ip_address(ip) for ip in host_ips if ipaddress.ip_address(ip).is_global]
    v4 = sum(1 for ip in public_ips if ip.version == 4)
    docker = as_dict(full.get("docker"))
    ports = as_dict(full.get("listening_tcp"))
    units = full.get("runner_units")
    unit_files = full.get("runner_unit_files")
    containers = docker.get("containers") if isinstance(docker.get("containers"), list) else []
    apt = as_dict(full.get("apt_manual"))
    apt_pkgs = apt.get("packages") if isinstance(apt.get("packages"), list) else None
    port_list = ports.get("ports") if isinstance(ports.get("ports"), list) else []
    return {
        "id": host_id,
        "status": "ok",
        "hostname": full.get("hostname") if isinstance(full.get("hostname"), str) else "absent",
        "os": as_dict(full.get("os")).get("pretty_name", "absent"),
        "uptime_seconds": full.get("uptime_seconds"),
        "disk_root_used_pct": as_dict(full.get("disk_root")).get("used_pct"),
        "memory_used_pct": as_dict(full.get("memory")).get("used_pct"),
        "runner_units": units if isinstance(units, list) else {"state": "absent", "reason": as_dict(units).get("reason")},
        "runner_unit_files": unit_files if isinstance(unit_files, list) else {"state": "absent"},
        "nginx": {"state": state_of(nginx), "server_name_count": len(names)},
        "docker": {"state": state_of(docker),
                   "container_count": len(containers) if state_of(docker) == "present" else None,
                   "reason": docker.get("reason")},
        "listening_tcp": {"state": state_of(ports),
                          "port_count": len(port_list) if state_of(ports) == "present" else None},
        "public_ip_counts": {"ipv4": v4, "ipv6": len(public_ips) - v4},
        "domains": doms,
        "apt_manual": public_apt(apt, apt_pkgs, role),
    }


def public_apt(apt, pkgs, role):
    """Package names travel in the (1-day) per-host artifact only for a host in a parity
    role, because the report job needs them to compare. A host with no role
    (hosting-vps, the internet-facing one) publishes none: a full package list is a
    recon map. The report itself keeps only counts plus the differences."""
    if state_of(apt) != "present" or pkgs is None:
        return {"state": "absent", "reason": apt.get("reason") or "not collected"}
    names = package_names(p for p in pkgs if isinstance(p, str))
    if not role:
        return {"state": "not_compared", "package_count": len(names)}
    return {"state": "present", "packages": names}


# ── report ───────────────────────────────────────────────────────────────────
def discovered_units(host):
    units = set()
    for key in ("runner_units", "runner_unit_files"):
        v = host.get(key)
        if isinstance(v, list):
            units |= {r["unit"] for r in v if isinstance(r, dict)
                      and str(r.get("unit", "")).endswith(".service")}
    return units


def drift(inventory, hosts):
    by_id = {h["id"]: h for h in hosts}
    ok = {hid for hid, h in by_id.items() if h.get("status") == "ok"}
    found = {hid: discovered_units(by_id[hid]) for hid in ok}
    expected = {}
    for r in inventory.get("runners", []):
        if r.get("systemd_unit"):
            expected[r["systemd_unit"]] = r.get("host")
    out = {"missing": [], "misplaced": [], "extra": [], "unverified": []}
    for unit, want in sorted(expected.items()):
        where = sorted(hid for hid, us in found.items() if unit in us)
        if want in where:
            continue
        if where:
            out["misplaced"].append({"unit": unit, "expected_host": want, "found_on": where})
        elif want in ok:
            out["missing"].append({"unit": unit, "expected_host": want})
        else:
            out["unverified"].append({"unit": unit, "expected_host": want,
                                      "reason": by_id.get(want, {}).get("status", "host not in inventory")})
    for hid in sorted(ok):
        for unit in sorted(found[hid]):
            if unit not in expected:
                out["extra"].append({"unit": unit, "host": hid})
    return out


def host_roles(inventory):
    """host id -> role. A host's explicit `role:` wins; otherwise a host that carries
    runners in inventory.yml is "ci", and any other host (hosting-vps) has no role and
    is left out of the parity check."""
    runner_hosts = {r.get("host") for r in inventory.get("runners") or [] if isinstance(r, dict)}
    roles = {}
    for h in inventory.get("hosts") or []:
        if not isinstance(h, dict) or not isinstance(h.get("id"), str):
            continue
        role = h.get("role") if isinstance(h.get("role"), str) and h.get("role") else (
            "ci" if h["id"] in runner_hosts else None)
        roles[h["id"]] = role
    return roles


def matrix_hosts(inventory):
    """inventory hosts[] for the collect matrix, each with its derived role ("" = none)."""
    roles = host_roles(inventory)
    return [dict(h, role=roles.get(h.get("id")) or "") for h in inventory.get("hosts") or []
            if isinstance(h, dict)]


def host_packages(h):
    """(set of package names, None) or (None, reason it cannot be compared)."""
    if h.get("status") != "ok":
        return None, h.get("status") or "no status"
    apt = as_dict(h.get("apt_manual"))
    pkgs = apt.get("packages")
    if apt.get("state") != "present" or not isinstance(pkgs, list):
        return None, f"apt_manual {apt.get('reason') or apt.get('state') or 'not collected'}"
    return {p for p in pkgs if isinstance(p, str)}, None


def package_parity(inventory, hosts):
    """Per role: packages installed by hand (apt-mark showmanual) on some of the role's
    hosts but not on others. Hosts that could not be read are listed as unverified."""
    by_id = {h.get("id"): h for h in hosts}
    roles = host_roles(inventory)
    out = []
    for role in sorted({r for r in roles.values() if r}):
        ids = [hid for hid, r in roles.items() if r == role]
        pkgs, unverified = {}, []
        for hid in ids:
            got, why = host_packages(by_id.get(hid, {"status": "no_result"}))
            if got is None:
                unverified.append({"host": hid, "reason": why})
            else:
                pkgs[hid] = got
        diffs = []
        if len(pkgs) >= 2:
            union, common = set().union(*pkgs.values()), set.intersection(*pkgs.values())
            for p in sorted(union - common):
                diffs.append({"package": p,
                              "present_on": sorted(h for h in pkgs if p in pkgs[h]),
                              "missing_on": sorted(h for h in pkgs if p not in pkgs[h])})
        out.append({"role": role, "hosts": ids, "compared": sorted(pkgs),
                    "unverified": unverified, "differences": diffs})
    return out


def host_domains(h):
    """A host's domain entries, skipping anything malformed."""
    doms = h.get("domains")
    for e in doms if isinstance(doms, list) else []:
        if isinstance(e, dict) and isinstance(e.get("domain"), str):
            idx = e.get("matched_record_indexes")
            yield {"domain": e["domain"],
                   "record_count": e.get("record_count") if isinstance(e.get("record_count"), int) else 0,
                   "matched": {i for i in idx if isinstance(i, int)} if isinstance(idx, list) else set(),
                   "nginx_server_name": e.get("nginx_server_name") is True}


def domain_map(domains, hosts, records_for=domain_records):
    rows = []
    for d in domains:
        # Resolved here too, so the count is right even when no host leg reported.
        count, served, union, nginx = len(records_for(d)), [], set(), []
        for h in hosts:
            for e in host_domains(h):
                if e["domain"] != d:
                    continue
                count = max(count, e["record_count"])
                if e["matched"]:
                    served.append(h["id"])
                    union |= e["matched"]
                if e["nginx_server_name"]:
                    nginx.append(h["id"])
        nginx = sorted(nginx)
        rows.append({"domain": d, "record_count": count, "served_by": sorted(served),
                     "unmatched_record_count": max(count - len(union), 0),
                     "nginx_server_name_on": nginx})
    return rows


def fmt_uptime(sec):
    if not isinstance(sec, (int, float)):
        return "?"
    d, rem = divmod(int(sec), 86400)
    return f"{d}d {rem // 3600}h"


def markdown(report):
    L = ["## Fleet inventory", "",
         f"Polled {report['generated_at']}. IP addresses, listening ports, container images and "
         "nginx server_name lists are deliberately left out of this public summary.", "",
         "| Host | Status | Hostname | OS | Uptime | Disk / | Mem | Runner units | nginx | docker |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    pct = lambda v: "?" if v is None else f"{v}%"
    for h in report["hosts"]:
        if h.get("status") != "ok":
            L.append(f"| {h.get('id')} | **{h.get('status')}** | | | | | | | | |")
            continue
        units = h.get("runner_units")
        if isinstance(units, list):
            ustr = ", ".join(f"{u.get('unit')} ({u.get('active')}/{u.get('sub')})"
                             for u in units if isinstance(u, dict)) or "none"
        else:
            ustr = "absent"
        dk = as_dict(h.get("docker"))
        dstr = f"{dk.get('container_count')} running" if dk.get("state") == "present" else "absent"
        L.append(f"| {h.get('id')} | ok | {h.get('hostname')} | {h.get('os')} | {fmt_uptime(h.get('uptime_seconds'))} | "
                 f"{pct(h.get('disk_root_used_pct'))} | {pct(h.get('memory_used_pct'))} | {ustr} | "
                 f"{as_dict(h.get('nginx')).get('state', 'absent')} | {dstr} |")
    L += ["", "### Domains", "", "| Domain | DNS records | Served by (IP match) | Unmatched records | nginx server_name on |",
          "|---|---|---|---|---|"]
    for d in report["domains"]:
        L.append(f"| {d['domain']} | {d['record_count']} | {', '.join(d['served_by']) or '**none**'} | "
                 f"{d['unmatched_record_count']} | {', '.join(d['nginx_server_name_on']) or '-'} |")
    dr = report["drift"]
    L += ["", "### Runner drift vs fleet/inventory.yml", ""]
    if not any(dr.values()):
        L.append("No drift.")
    for kind in ("missing", "misplaced", "extra", "unverified"):
        for e in dr[kind]:
            L.append(f"- **{kind}**: `{e['unit']}` " + ", ".join(f"{k}={v}" for k, v in e.items() if k != "unit"))
    L += ["", "### Package parity (apt-mark showmanual, hosts of the same role)", "",
          "Roles come from fleet/inventory.yml: hosts that carry runners are `ci`; a host with no "
          "runners and no `role:` is not compared."]
    for g in report.get("package_parity", []):
        L += ["", f"**{g['role']}** ({', '.join(g['hosts'])})", ""]
        for u in g["unverified"]:
            L.append(f"- not compared: {u['host']} ({u['reason']})")
        if len(g["compared"]) < 2:
            L.append("- fewer than two hosts could be compared.")
        elif not g["differences"]:
            L.append(f"- No differences between {', '.join(g['compared'])}.")
        else:
            L += ["| Package | Present on | Missing on |", "|---|---|---|"]
            for d in g["differences"]:
                L.append(f"| {d['package']} | {', '.join(d['present_on'])} | **{', '.join(d['missing_on'])}** |")
    return "\n".join(L) + "\n"


def warnings(report):
    w = []
    for h in report["hosts"]:
        if h["status"] == "not_configured":
            w.append(f"{h['id']}: not configured (missing secrets: {', '.join(h.get('missing_secrets', []))})")
        elif h["status"] != "ok":
            w.append(f"{h['id']}: {h['status']}")
    for d in report["domains"]:
        if not d["record_count"]:
            w.append(f"{d['domain']}: does not resolve (no A/AAAA records)")
        elif not d["served_by"]:
            w.append(f"{d['domain']}: no polled host matches its DNS records")
        elif d["unmatched_record_count"]:
            w.append(f"{d['domain']}: {d['unmatched_record_count']} DNS record(s) match no polled host")
    for kind in ("missing", "misplaced", "extra"):
        for e in report["drift"][kind]:
            w.append(f"runner drift ({kind}): {e['unit']}")
    for g in report.get("package_parity", []):
        lacking = {}
        for d in g["differences"]:
            for hid in d["missing_on"]:
                lacking.setdefault(hid, []).append(d["package"])
        for hid in sorted(lacking):
            w.append(f"package parity ({g['role']}): {hid} lacks {', '.join(lacking[hid])} "
                     f"(installed on other {g['role']} hosts)")
        for u in g["unverified"]:
            if not u["reason"].startswith("apt_manual"):
                continue   # an unreachable/unconfigured host is already warned about above
            w.append(f"package parity ({g['role']}): {u['host']} not compared ({u['reason']})")
    return w


def load_host_files(hostdir):
    """One unreadable host file must not cost the whole fleet its report."""
    found = {}
    for f in sorted(pathlib.Path(hostdir).glob("*.json")):
        try:
            h = json.loads(f.read_text())
            if not isinstance(h, dict) or not isinstance(h.get("id"), str):
                raise ValueError("not a host object")
        except (OSError, ValueError) as e:
            print(f"::warning::{f.stem}: host result unparseable ({type(e).__name__})")
            h = {"id": f.stem, "status": "unparseable"}
        found[h["id"]] = h
    return found


def report_host(h):
    """The host as the 30-day report shows it: apt_manual cut down to a count. Package
    names reach the report only through package_parity[].differences."""
    if "apt_manual" not in h:
        return h
    apt = as_dict(h.get("apt_manual"))
    pkgs = apt.get("packages")
    out = {"state": apt.get("state") if isinstance(apt.get("state"), str) else "absent"}
    if isinstance(pkgs, list):
        out["package_count"] = len(pkgs)
    elif isinstance(apt.get("package_count"), int):
        out["package_count"] = apt["package_count"]
    if isinstance(apt.get("reason"), str):
        out["reason"] = apt["reason"]
    return dict(h, apt_manual=out)


def build_report(inv, hostdir, records_for=domain_records):
    found = load_host_files(hostdir)
    hosts = [found.get(h["id"], {"id": h["id"], "status": "no_result"}) for h in inv.get("hosts", [])]
    parity = package_parity(inv, hosts)
    hosts = [report_host(h) for h in hosts]
    report = {"generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "hosts": hosts, "domains": domain_map(inv.get("domains") or [], hosts, records_for),
              "drift": drift(inv, hosts), "package_parity": parity}
    report["warnings"] = warnings(report)
    return report


# ── cli ──────────────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parse"); p.add_argument("raw")
    p = sub.add_parser("ips"); p.add_argument("full")
    p = sub.add_parser("public"); p.add_argument("full"); p.add_argument("--id", required=True)
    p.add_argument("--target", default=""); p.add_argument("--domains", default="")
    p.add_argument("--role", default="")
    p = sub.add_parser("hosts"); p.add_argument("inventory")
    p = sub.add_parser("missing"); p.add_argument("--id", required=True); p.add_argument("--secrets", default="")
    p = sub.add_parser("failed"); p.add_argument("--id", required=True); p.add_argument("--status", required=True)
    p = sub.add_parser("report"); p.add_argument("inventory"); p.add_argument("hostdir")
    p.add_argument("--json-out", required=True); p.add_argument("--md-out", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "parse":
        json.dump(parse_raw(pathlib.Path(a.raw).read_text()), sys.stdout, indent=1)
    elif a.cmd == "ips":
        full = json.loads(pathlib.Path(a.full).read_text())
        ips = set(full["ips"]) if isinstance(full.get("ips"), list) else set()
        sockets = full.get("listening_tcp", {}).get("sockets", []) if isinstance(full.get("listening_tcp"), dict) else []
        for s in sockets:
            host = s.rsplit(":", 1)[0].strip("[]").split("%")[0]
            try:
                ips.add(str(ipaddress.ip_address(host)))
            except ValueError:
                pass
        print("\n".join(sorted(ip for ip in ips if ip not in ("0.0.0.0", "::"))))
    elif a.cmd == "public":
        full = json.loads(pathlib.Path(a.full).read_text())
        doms = [d for d in a.domains.split(",") if d.strip()]
        json.dump(make_public(full, a.id, a.target, [d.strip() for d in doms], role=a.role),
                  sys.stdout, indent=1)
    elif a.cmd == "hosts":
        inv = json.loads(pathlib.Path(a.inventory).read_text())
        json.dump(matrix_hosts(inv), sys.stdout, separators=(",", ":"))
    elif a.cmd == "missing":
        json.dump({"id": a.id, "status": "not_configured",
                   "missing_secrets": [x for x in a.secrets.split(",") if x]}, sys.stdout, indent=1)
    elif a.cmd == "failed":
        json.dump({"id": a.id, "status": a.status}, sys.stdout, indent=1)
    elif a.cmd == "report":
        inv = json.loads(pathlib.Path(a.inventory).read_text())
        report = build_report(inv, pathlib.Path(a.hostdir))
        pathlib.Path(a.json_out).write_text(json.dumps(report, indent=1) + "\n")
        pathlib.Path(a.md_out).write_text(markdown(report))
        for w in report["warnings"]:
            print(f"::warning::{w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
