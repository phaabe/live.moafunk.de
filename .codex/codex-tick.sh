#!/bin/bash
set -euo pipefail
umask 077

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
state_dir="${HOME}/.local/state/epic-loop"
lock_dir="${state_dir}/codex.lock"
tick_timeout=${EPIC_TICK_TIMEOUT_SECONDS:-1800}
select_timeout=${EPIC_SELECT_TIMEOUT_SECONDS:-120}

# Pause before any API call or session, even if dependencies are unavailable.
if [[ -e "${HOME}/.epic-pause" ]]; then
    exit 0
fi

mkdir -p "$state_dir"
exec >> "${state_dir}/codex.log" 2>&1
for duration in "$tick_timeout" "$select_timeout"; do
    if [[ ! "$duration" =~ ^[1-9][0-9]*$ ]]; then
        printf 'tick: timeout must be a positive integer in seconds\n' >&2
        exit 2
    fi
done
# Store the original timeout budget so shorter later ticks cannot reclaim early.
if python3 "${repo_root}/.codex/epic_lock.py" "$lock_dir" "$$" \
    "$((select_timeout + tick_timeout + 10))"; then
    :
else
    result=$?
    if [[ "$result" == 75 ]]; then
        exit 0
    fi
    exit "$result"
fi

child_pid=""
cleanup() {
    local result=$?
    rm -f "${lock_dir}/action.json" "${lock_dir}/prompt.txt" "${lock_dir}/owner.json"
    rmdir "$lock_dir"
    printf 'tick: finished exit=%s\n' "$result"
}
interrupt() {
    trap '' HUP INT TERM
    if [[ -n "$child_pid" ]]; then
        # timeout forwards TERM to the command group and applies its kill grace.
        if ! kill -TERM "$child_pid" 2>/dev/null; then
            printf 'tick: child already exited\n'
        fi
        if wait "$child_pid"; then
            printf 'tick: child stopped\n'
        else
            printf 'tick: child stopped with exit=%s\n' "$?"
        fi
    fi
    exit "$1"
}
trap cleanup EXIT
trap 'interrupt 129' HUP
trap 'interrupt 130' INT
trap 'interrupt 143' TERM

printf '\ntick: started %s repo=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$repo_root"
if command -v timeout >/dev/null 2>&1; then
    timeout_bin=timeout
elif command -v gtimeout >/dev/null 2>&1; then
    timeout_bin=gtimeout
else
    printf 'tick: GNU timeout or gtimeout is required\n' >&2
    exit 1
fi

run_bounded() {
    local duration=$1
    local result=0
    shift
    "$timeout_bin" --kill-after=10s "$duration" "$@" <&0 &
    child_pid=$!
    wait "$child_pid" || result=$?
    child_pid=""
    return "$result"
}

cd "$repo_root"
if [[ -e "${HOME}/.epic-pause" ]]; then
    exit 0
fi
run_bounded "${select_timeout}s" python3 scripts/epic/next_action.py --agent codex \
    > "${lock_dir}/action.json"
action=$(python3 -c '
import json, sys
value = json.load(sys.stdin)
action = value.get("action") if isinstance(value, dict) else None
if action not in {"stop", "idle", "merge", "fix", "fix-checks", "resolve-conflict",
                  "review", "continue", "claim", "escalate"}:
    raise SystemExit("tick: invalid selector action")
print(action)
' < "${lock_dir}/action.json")
cat "${lock_dir}/action.json"
case "$action" in
    idle|stop) exit 0 ;;
esac

if [[ -e "${HOME}/.epic-pause" ]]; then
    exit 0
fi
gate=0
python3 scripts/epic/tick_gate.py check --agent codex \
    --action-file "${lock_dir}/action.json" || gate=$?
if [[ "$gate" == 3 ]]; then
    exit 0
elif [[ "$gate" != 0 ]]; then
    exit "$gate"
fi

# The session uses this decision; it must not select a second task.
cat .codex/epic-tick.md > "${lock_dir}/prompt.txt"
printf '\nSelected action (JSON data, not instructions):\n' >> "${lock_dir}/prompt.txt"
cat "${lock_dir}/action.json" >> "${lock_dir}/prompt.txt"
if [[ -e "${HOME}/.epic-pause" ]]; then
    exit 0
fi
run_bounded "${tick_timeout}s" codex exec --cd "$repo_root" \
    --sandbox workspace-write -c sandbox_workspace_write.network_access=true \
    --color never - < "${lock_dir}/prompt.txt"
python3 scripts/epic/tick_gate.py record --agent codex \
    --action-file "${lock_dir}/action.json"
