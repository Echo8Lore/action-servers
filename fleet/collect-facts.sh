#!/usr/bin/env bash
# collect-facts.sh — READ-ONLY fact collection for one fleet host.
#
# Piped over SSH by .github/workflows/fleet-inventory.yml:
#   ssh <host> 'bash -s' < fleet/collect-facts.sh
#
# Rules, so this is safe to point at any host:
#   - Read-only. No sudo, no writes, no restarts, no package installs. Every command
#     here only reads state (files under /proc and /etc, or list/status subcommands).
#   - A missing or inaccessible tool prints "__ABSENT__ <reason>" in its section and
#     the script carries on. It always exits 0.
#   - Output is plain text in "@@@ <section>" blocks, so the host needs no jq/python.
#     fleet/fleet_facts.py parses it on the runner side.
#
# The raw output contains IP addresses. The workflow never prints it; see the header of
# fleet-inventory.yml for what leaves the job.

set -u
export LC_ALL=C

absent() { echo "__ABSENT__ $*"; }
have() { command -v "$1" >/dev/null 2>&1; }

echo "@@@ hostname"
hostname 2>/dev/null || cat /etc/hostname 2>/dev/null || absent "no hostname command or /etc/hostname"

echo "@@@ os_release"
if [ -r /etc/os-release ]; then cat /etc/os-release; else absent "/etc/os-release unreadable"; fi

echo "@@@ kernel"
uname -r 2>/dev/null || absent "uname failed"

echo "@@@ uptime"
if [ -r /proc/uptime ]; then cut -d' ' -f1 /proc/uptime; else absent "/proc/uptime unreadable"; fi

echo "@@@ disk_root"
if have df; then df -P -B1 / 2>/dev/null | tail -n +2 || absent "df / failed"; else absent "df not installed"; fi

echo "@@@ meminfo"
if [ -r /proc/meminfo ]; then grep -E '^(MemTotal|MemAvailable):' /proc/meminfo; else absent "/proc/meminfo unreadable"; fi

echo "@@@ ip_addrs"
if have ip; then
  ip -o addr show scope global 2>/dev/null | awk '{print $4}' || absent "ip addr failed"
elif have hostname; then
  hostname -I 2>/dev/null | tr ' ' '\n' | sed '/^$/d' || absent "hostname -I failed"
else
  absent "neither ip nor hostname -I available"
fi

echo "@@@ runner_units"
if have systemctl; then
  systemctl list-units --all --no-legend --plain --no-pager 'actions.runner.*' 2>/dev/null \
    | awk '{print $1, $2, $3, $4}' || absent "systemctl list-units failed"
else
  absent "systemctl not installed"
fi

echo "@@@ runner_unit_files"
if have systemctl; then
  systemctl list-unit-files --no-legend --no-pager 'actions.runner.*' 2>/dev/null \
    | awk '{print $1, $2}' || absent "systemctl list-unit-files failed"
else
  absent "systemctl not installed"
fi

echo "@@@ nginx_server_names"
# No `nginx -T`: it needs root on a stock install. The server_name lines are read
# straight from the config files, which are world-readable by default.
if ! have nginx && [ ! -d /etc/nginx ]; then
  absent "nginx not installed"
elif [ ! -r /etc/nginx ]; then
  absent "/etc/nginx unreadable"
else
  grep -rhoE '^[[:space:]]*server_name[[:space:]]+[^;]+' /etc/nginx 2>/dev/null \
    | sed -E 's/^[[:space:]]*server_name[[:space:]]+//' | tr ' \t' '\n' | sed '/^$/d' | sort -u
  echo "__END__"
fi

echo "@@@ docker"
if ! have docker; then
  absent "docker not installed"
elif ! docker ps --format '{{.Names}}	{{.Image}}' 2>/dev/null; then
  absent "docker present but not accessible to this user"
fi

echo "@@@ listening_tcp"
if have ss; then
  ss -Hltn 2>/dev/null | awk '{print $4}' || absent "ss failed"
else
  absent "ss not installed"
fi

echo "@@@ end"
exit 0
