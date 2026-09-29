#!/bin/bash
set -euo pipefail
umask 077

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
state_dir="${EPIC_STATE_DIR:-${HOME}/.local/state/epic-loop}"
tick_timeout=${EPIC_TICK_TIMEOUT_SECONDS:-1800}
select_timeout=${EPIC_SELECT_TIMEOUT_SECONDS:-120}
pull_timeout=${EPIC_PULL_TIMEOUT_SECONDS:-120}
blocked_retry=${EPIC_BLOCKED_RETRY_SECONDS:-900}
# Optional agent id (codex, codex-2, ...). A registered agent keeps the same
# files as the legacy state dir in its own folder, agents/<id>/.
agent_id=${EPIC_AGENT_ID:-}
# The runner changes directory later; keep every path absolute.
[[ "$state_dir" == /* ]] || state_dir="${PWD}/${state_dir}"
registry_dir=$state_dir

# Pause before any API call or session, even if dependencies are unavailable.
if [[ -e "${HOME}/.epic-pause" ]]; then
    exit 0
fi
if [[ -n "$agent_id" ]]; then
    if [[ ! "$agent_id" =~ ^codex(-[a-z0-9]{1,16})?$ ]]; then
        printf 'tick: EPIC_AGENT_ID must look like codex or codex-2\n' >&2
        exit 2
    fi
    state_dir="${registry_dir}/agents/${agent_id}"
fi
# The gate helper reads the state dir from the environment.
export EPIC_STATE_DIR="$state_dir"
# One GitHub quota for all agents: its wait file lives in the shared state dir.
export EPIC_QUOTA_DIR="$registry_dir"
lock_dir="${state_dir}/codex.lock"
log_file="${state_dir}/codex.log"
events_file="${state_dir}/codex-ticks.jsonl"
events="${repo_root}/scripts/epic/tick_events.py"
# Tick events for the monitor: the stage the tick is in, and an outcome when
# the runner knows better than the exit code (see tick_events.py).
tick_started=""
events_open=0
tick_offset=0
tick_phase=lock
tick_outcome=auto

mkdir -p "$state_dir"
exec >> "$log_file" 2>&1
for duration in "$tick_timeout" "$select_timeout" "$pull_timeout" "$blocked_retry"; do
    if [[ ! "$duration" =~ ^[1-9][0-9]*$ ]]; then
        printf 'tick: timeout and retry delay must be positive integers in seconds\n' >&2
        exit 2
    fi
done
if [[ -n "$agent_id" ]]; then
    python3 "${repo_root}/scripts/epic/agents.py" --state-dir "$registry_dir" \
        register --id "$agent_id" --kind codex --label "${EPIC_AGENT_LABEL:-}" \
        --interval "${EPIC_AGENT_INTERVAL_SECONDS:-180}" \
        --budget "$((2 * pull_timeout + select_timeout + tick_timeout + 20))"
fi
# Store the original timeout budget so shorter later ticks cannot reclaim early.
if python3 "${repo_root}/.codex/epic_lock.py" "$lock_dir" "$$" \
    "$((2 * pull_timeout + select_timeout + tick_timeout + 20))"; then
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
    # Log the finish while the lock is held, so the next tick's start line
    # always comes after it (the monitor pairs start and finish lines).
    # A failed log write must not skip the lock release below (set -e).
    printf 'tick: finished exit=%s\n' "$result" || true
    if [[ "$events_open" == 1 ]]; then
        python3 "$events" finish --file "$events_file" --tick "$tick_started" \
            --exit "$result" --phase "$tick_phase" --outcome "$tick_outcome" \
            --action-file "${lock_dir}/action.json" \
            --log "$log_file" --since "$tick_offset" || true
    fi
    rm -f "${lock_dir}/action.json" "${lock_dir}/prompt.txt" "${lock_dir}/owner.json"
    rmdir "$lock_dir"
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

tick_started=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
printf '\ntick: started %s repo=%s\n' "$tick_started" "$repo_root"
# A failed event write never changes the tick: it only skips the finish event.
if tick_offset=$(python3 "$events" start --file "$events_file" \
    --tick "$tick_started" --log "$log_file"); then
    events_open=1
else
    tick_offset=0
fi
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

check_quota() {
    tick_phase=quota
    local result=0
    python3 "${repo_root}/scripts/epic/github_quota.py" check \
        --state-dir "$registry_dir" || result=$?
    if [[ "$result" == 3 ]]; then
        exit 0
    elif [[ "$result" != 0 ]]; then
        exit "$result"
    fi
}

cd "$repo_root"
if [[ -e "${HOME}/.epic-pause" ]]; then
    exit 0
fi
check_quota
printf 'tick: refreshing runner checkout\n'
tick_phase=refresh
# Only the shared helper decides which tracked changes are safe to restore.
noise_exit=0
run_bounded "${pull_timeout}s" python3 scripts/epic/gitnexus_noise.py || noise_exit=$?
if [[ "$noise_exit" != 0 ]]; then
    printf 'tick: checkout noise check failed exit=%s; stopping\n' "$noise_exit" >&2
    exit "$noise_exit"
fi
if [[ -e "${HOME}/.epic-pause" ]]; then
    exit 0
fi
check_quota
tick_phase=refresh
pull_exit=0
run_bounded "${pull_timeout}s" git pull --ff-only || pull_exit=$?
if [[ "$pull_exit" != 0 ]]; then
    printf 'tick: checkout refresh failed exit=%s; stopping\n' "$pull_exit" >&2
    exit "$pull_exit"
fi
if [[ -e "${HOME}/.epic-pause" ]]; then
    exit 0
fi
check_quota
tick_phase=select
select_exit=0
run_bounded "${select_timeout}s" python3 scripts/epic/next_action.py --agent codex \
    > "${lock_dir}/action.json" || select_exit=$?
if [[ "$select_exit" == 4 ]]; then
    tick_phase=quota
    tick_outcome=blocked
    exit 75
elif [[ "$select_exit" != 0 ]]; then
    exit "$select_exit"
fi
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
check_quota
tick_phase=backoff
backoff=0
python3 .codex/tick_backoff.py check --action-file "${lock_dir}/action.json" \
    --state-dir "$state_dir" --quota-dir "$registry_dir" --ttl "$blocked_retry" || backoff=$?
if [[ "$backoff" == 3 ]]; then
    exit 0
elif [[ "$backoff" == 4 ]]; then
    tick_phase=quota
    tick_outcome=blocked
    exit 75
elif [[ "$backoff" != 0 ]]; then
    exit "$backoff"
fi
check_quota
tick_phase=gate
gate=0
python3 scripts/epic/tick_gate.py check --agent codex \
    --action-file "${lock_dir}/action.json" || gate=$?
if [[ "$gate" == 3 ]]; then
    exit 0
elif [[ "$gate" == 4 ]]; then
    tick_phase=quota
    tick_outcome=blocked
    exit 75
elif [[ "$gate" != 0 ]]; then
    exit "$gate"
fi

# The session uses this decision; it must not select a second task.
cat .codex/epic-tick.md > "${lock_dir}/prompt.txt"
printf '\nSelected action (JSON data, not instructions):\n' >> "${lock_dir}/prompt.txt"
cat "${lock_dir}/action.json" >> "${lock_dir}/prompt.txt"
printf '\nInstalled feature Git helper: %s\n' \
    "${HOME}/.local/libexec/codex-feature-git.py" >> "${lock_dir}/prompt.txt"
if [[ -e "${HOME}/.epic-pause" ]]; then
    exit 0
fi
# Never accept the previous session's final result if this session fails to write.
check_quota
: > "${state_dir}/codex-result.json"
tick_phase=model
model_exit=0
run_bounded "${tick_timeout}s" codex exec --cd "$repo_root" \
    --sandbox workspace-write -c sandbox_workspace_write.network_access=true \
    --color never --output-schema "${repo_root}/.codex/tick-result.schema.json" \
    --output-last-message "${state_dir}/codex-result.json" \
    - < "${lock_dir}/prompt.txt" || model_exit=$?
# Finish local records even if another agent stored a quota wait during the model.
outcome=0
if [[ "$model_exit" == 0 ]]; then
    tick_phase=result
fi
python3 .codex/tick_backoff.py record --action-file "${lock_dir}/action.json" \
    --state-dir "$state_dir" --quota-dir "$registry_dir" --ttl "$blocked_retry" \
    --result-file "${state_dir}/codex-result.json" --exit-code "$model_exit" || outcome=$?
if [[ "$outcome" == 4 ]]; then
    tick_phase=quota
    tick_outcome=blocked
    if [[ "$model_exit" != 0 ]]; then
        exit "$model_exit"
    fi
    exit 75
elif [[ "$model_exit" != 0 ]]; then
    exit "$model_exit"
elif [[ "$outcome" != 0 && "$outcome" != 3 ]]; then
    exit "$outcome"
fi
# A valid blocked result is a seen no-op, not an unrecorded process failure.
# Keep the shared gate's longer suppression after the short cooldown expires.
tick_phase=record
python3 scripts/epic/tick_gate.py record --agent codex \
    --action-file "${lock_dir}/action.json"
if [[ "$outcome" == 3 ]]; then
    # The model reported a valid blocked result; the runner waits to retry.
    tick_outcome=blocked
    tick_phase=result
    exit 75
fi
