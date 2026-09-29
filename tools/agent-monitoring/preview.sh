#!/bin/bash
# Show the dashboards with fixture data for one design scenario.
#   tools/agent-monitoring/preview.sh normal   # then open http://127.0.0.1:13001
# A separate Compose project on other ports; the real stack keeps running.
# No GitHub calls; the fixture state dir is made under $TMPDIR.
# Stop: Ctrl-C, then
#   docker compose -p agent-monitoring-preview -f tools/agent-monitoring/compose.yaml down
set -euo pipefail
scenario="${1:?usage: preview.sh normal|single|busy|full|late|failing|collision|retired|paused|stale}"
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
state_dir="${AGENT_PREVIEW_STATE_DIR:-${TMPDIR:-/tmp}/agent-monitoring-preview/state}"
export AGENT_GRAFANA_PORT="${AGENT_GRAFANA_PORT:-13001}"
export AGENT_PROMETHEUS_PORT="${AGENT_PROMETHEUS_PORT:-19091}"
cd "$repo_root"
# Its own runtime dir: the collector's checkpoints and metrics stay untouched.
output=tools/agent-monitoring/runtime-preview/metrics
mkdir -p "$state_dir"
# Build the state first, so Alloy mounts a dir that already has the logs.
python3 scripts/epic/fixtures.py "$scenario" --state-dir "$state_dir" --output "$output" --once
# Alloy reads the fixture logs, not the real ones.
EPIC_STATE_DIR="$state_dir" AGENT_RUNTIME=./runtime-preview docker compose -p agent-monitoring-preview \
    -f tools/agent-monitoring/compose.yaml up -d
printf 'Preview: http://127.0.0.1:%s (scenario %s)\n' "$AGENT_GRAFANA_PORT" "$scenario"
exec python3 scripts/epic/fixtures.py "$scenario" --state-dir "$state_dir" --output "$output"
