#!/bin/bash
# Manage the host collector for the current macOS login session.
set -euo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
cd "$repo_root"
runtime="${repo_root}/tools/agent-monitoring/runtime"
domain="gui/$(id -u)"
label="de.moafunk.agent-monitoring"
service="${domain}/${label}"

case "${1:-status}" in
    start)
        if launchctl print "$service" >/dev/null 2>&1; then
            printf 'Collector is already registered: %s\n' "$service"
            exit 0
        fi
        mkdir -p "${runtime}/metrics"
        python3 - "$repo_root" "$runtime" "$label" <<'PY'
import os
from pathlib import Path
import plistlib
import sys

root, runtime, label = sys.argv[1:]
config = {
    "Label": label,
    "ProgramArguments": [sys.executable, f"{root}/scripts/epic/monitor.py"],
    "WorkingDirectory": root,
    "EnvironmentVariables": {"PATH": os.environ["PATH"]},
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 30,
    "ProcessType": "Background",
    "StandardOutPath": f"{runtime}/collector.log",
    "StandardErrorPath": f"{runtime}/collector.log",
}
with Path(runtime, "collector.plist").open("wb") as output:
    plistlib.dump(config, output)
PY
        docker compose -f tools/agent-monitoring/compose.yaml up -d
        launchctl bootstrap "$domain" "${runtime}/collector.plist"
        printf 'Dashboard: http://127.0.0.1:13000/d/epic-agents\n'
        ;;
    stop)
        if launchctl print "$service" >/dev/null 2>&1; then
            launchctl bootout "$service"
        else
            printf 'Collector is not registered\n'
        fi
        docker compose -f tools/agent-monitoring/compose.yaml down
        ;;
    status)
        launchctl print "$service"
        docker compose -f tools/agent-monitoring/compose.yaml ps
        ;;
    *)
        printf 'Usage: bash tools/agent-monitoring/service.sh start|stop|status\n' >&2
        exit 2
        ;;
esac
