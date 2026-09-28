#!/bin/bash
set -euo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
cd "$repo_root"
mkdir -p tools/agent-monitoring/runtime/metrics
docker compose -f tools/agent-monitoring/compose.yaml up -d
exec python3 scripts/epic/monitor.py "$@"
