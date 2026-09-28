#!/bin/bash
set -euo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
# The collector and the Alloy log mount must read the same state directory.
state_dir="${EPIC_STATE_DIR:-${HOME}/.local/state/epic-loop}"
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
    case "${args[i]}" in
        --state-dir) state_dir="${args[i + 1]:?--state-dir needs a path}" ;;
        --state-dir=*) state_dir="${args[i]#--state-dir=}" ;;
    esac
done
# Resolve against the caller's directory; Compose would resolve a relative
# path against tools/agent-monitoring/.
[[ "$state_dir" == /* ]] || state_dir="${PWD}/${state_dir}"
export EPIC_STATE_DIR="$state_dir"
cd "$repo_root"
mkdir -p tools/agent-monitoring/runtime/metrics
docker compose -f tools/agent-monitoring/compose.yaml up -d
exec python3 scripts/epic/monitor.py "$@" --state-dir "$state_dir"
