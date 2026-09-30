#!/bin/bash
set -euo pipefail
umask 077

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
state_dir="${EPIC_STATE_DIR:-${HOME}/.local/state/epic-loop}"
tick_timeout=${EPIC_TICK_TIMEOUT_SECONDS:-1800}
select_timeout=${EPIC_SELECT_TIMEOUT_SECONDS:-120}
recheck_timeout=0
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
if [[ "${EPIC_SHARED_READER:-}" == 1 ]]; then
    # Validate the snapshot deadline before registration or any GitHub read.
    recheck_timeout=$(python3 - "${repo_root}/scripts/epic" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import github_state
try:
    print(github_state.settings().recheck)
except github_state.ConfigError as error:
    print(f"tick: {error}", file=sys.stderr)
    sys.exit(2)
PY
    )
fi
# Fresh backoff lookup and action recheck each have a separate bounded read.
# Preparation can run for several candidates. This budget estimates one;
# the live runner PID keeps the lock even when candidate scanning takes longer.
budget=$((4 * pull_timeout + select_timeout + 2 * recheck_timeout + 60 + tick_timeout + 20))
if [[ -n "$agent_id" ]]; then
    python3 "${repo_root}/scripts/epic/agents.py" --state-dir "$registry_dir" \
        register --id "$agent_id" --kind codex --label "${EPIC_AGENT_LABEL:-}" \
        --interval "${EPIC_AGENT_INTERVAL_SECONDS:-180}" \
        --budget "$budget"
fi
# Store the original timeout budget so shorter later ticks cannot reclaim early.
if python3 "${repo_root}/.codex/epic_lock.py" "$lock_dir" "$$" \
    "$budget"; then
    :
else
    result=$?
    if [[ "$result" == 75 ]]; then
        exit 0
    fi
    exit "$result"
fi

child_pid=""
review_log=""
review_log_saved=0
archive_review_output() {
    if [[ -n "$review_log" && "$review_log_saved" == 0 && -f "$review_log" ]]; then
        if cat "$review_log"; then
            review_log_saved=1
        else
            printf 'tick: review output retained in %s; log copy failed\n' "$review_log" >&2
        fi
    fi
}
cleanup_review() {
    if [[ -f "${lock_dir}/review-context.json" ]]; then
        if run_bounded "${pull_timeout}s" python3 "${repo_root}/.codex/review_worktree.py" cleanup \
            --runner "$repo_root" --context-file "${lock_dir}/review-context.json" < /dev/null; then
            :
        else
            if ! python3 -c '
import json, sys
context = json.load(open(sys.argv[1]))
print("tick: review cleanup incomplete; retained path: " + context["worktree"], file=sys.stderr)
' "${lock_dir}/review-context.json"; then
                printf 'tick: review cleanup incomplete; inspect %s\n' "${lock_dir}/review-context.json" >&2
            fi
        fi
    fi
}
# The adopt body directory of this tick; removed on exit.
body_dir=""
unset EPIC_BODY_DIR EPIC_BODY_DIR_ID
cleanup() {
    local result=$?
    trap '' HUP INT TERM
    # A signal can exit from inside the model's redirected shell function.
    # Restore the tick log before copying the review log, never into itself.
    exec >> "$log_file" 2>&1
    archive_review_output
    # Child termination precedes this trap, including timeout and handled signals.
    # Evidence is already outside the checkout. Cleanup cannot change its result.
    cleanup_review
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
    rm -f "${state_dir}/feature-git-context.json" "${lock_dir}/action.json" "${lock_dir}/assignment.json" "${lock_dir}/prompt.txt" "${lock_dir}/owner.json" "${lock_dir}/worktree.txt" "${lock_dir}/review-context.json"
    if [[ -n "$body_dir" ]]; then
        rm -rf "$body_dir"
    fi
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

# Revoke context left by a killed tick before selecting another action.
rm -f "${state_dir}/feature-git-context.json"
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

discard_seen() {
    rm -f "${state_dir}/codex-gate-seen.json"
}

read_blocked() {
    discard_seen
    printf 'tick: fresh GitHub read blocked in %s; stopping\n' "$tick_phase" >&2
    tick_outcome=blocked
    exit 75
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
run_bounded "${select_timeout}s" python3 scripts/epic/next_action.py --agent codex --candidates \
    > "${lock_dir}/action.json" || select_exit=$?
if [[ "$select_exit" == 4 ]]; then
    tick_phase=quota
    tick_outcome=blocked
    exit 75
elif [[ "$select_exit" == 5 ]]; then
    read_blocked
elif [[ "$select_exit" == 6 ]]; then
    discard_seen
    exit 0
elif [[ "$select_exit" != 0 ]]; then
    exit "$select_exit"
fi
candidates=$(cat "${lock_dir}/action.json")
printf '%s\n' "$candidates"

release_target() {
    exec 8>&- 9>&-
}

# Called from `if`, so every error must be handled explicitly.
lock_target() {
    local listed path result=0
    local targets=()
    listed=$(python3 scripts/epic/target_lock.py paths \
        --action-file "${lock_dir}/action.json") || exit 1
    while IFS= read -r path; do
        if [[ -n "$path" ]]; then
            targets+=("$path")
        fi
    done <<< "$listed"
    if [[ "${#targets[@]}" == 0 ]]; then
        return 0
    elif [[ "${#targets[@]}" -gt 2 ]]; then
        printf 'tick: an action has at most two targets\n' >&2
        exit 1
    fi
    exec 8>> "${targets[0]}" || exit 1
    if [[ "${#targets[@]}" == 2 ]]; then
        exec 9>> "${targets[1]}" || exit 1
        python3 scripts/epic/target_lock.py acquire --fd 8 --fd 9 || result=$?
    else
        python3 scripts/epic/target_lock.py acquire --fd 8 || result=$?
    fi
    if [[ "$result" == 0 ]]; then
        return 0
    fi
    release_target
    if [[ "$result" == 75 ]]; then
        return 1
    fi
    exit "$result"
}

# Try every candidate, but start at most one model session.
# Candidates come on fd 3, so a loop command that reads stdin cannot eat them.
selected=0
target_blocked=0
model_root="$repo_root"
while IFS= read -r -u 3 candidate; do
    if [[ -z "$candidate" ]]; then
        continue
    fi
    if [[ -e "${HOME}/.epic-pause" ]]; then
        exit 0
    fi
    check_quota
    tick_phase=select
    printf '%s\n' "$candidate" > "${lock_dir}/action.json"
    action=$(python3 -c '
import json, sys
value = json.load(sys.stdin)
action = value.get("action") if isinstance(value, dict) else None
if action not in {"stop", "idle", "merge", "fix", "fix-checks", "resolve-conflict",
                  "review", "continue", "claim", "escalate", "adopt"}:
    raise SystemExit("tick: invalid selector action")
print(action)
' < "${lock_dir}/action.json")
    case "$action" in
        idle|stop) exit 0 ;;
    esac
    tick_phase=lock
    if ! lock_target; then
        printf 'tick: target locked by another runner; next candidate\n'
        continue
    fi
    # Delivery has its own fresh checks and must not wait on model cooldowns.
    if [[ "$action" == review ]]; then
        tick_phase=verify
        delivery_exit=0
        run_bounded "${pull_timeout}s" python3 .codex/review_delivery.py resume \
            --runner "$repo_root" --action-file "${lock_dir}/action.json" \
            --state-dir "$state_dir" || delivery_exit=$?
        case "$delivery_exit" in
            0)
                discard_seen
                printf 'tick: saved review delivery confirmed; no model needed\n'
                exit 0
                ;;
            3) ;;
            4)
                discard_seen
                tick_phase=quota
                tick_outcome=blocked
                exit 75
                ;;
            7)
                discard_seen
                target_blocked=1
                release_target
                printf 'tick: saved review delivery refused; next candidate\n'
                continue
                ;;
            *) read_blocked ;;
        esac
    fi
    check_quota
    tick_phase=backoff
    backoff=0
    if [[ "${EPIC_SHARED_READER:-}" == 1 ]]; then
        run_bounded "${recheck_timeout}s" python3 .codex/tick_backoff.py check \
            --action-file "${lock_dir}/action.json" --state-dir "$state_dir" \
            --quota-dir "$registry_dir" --ttl "$blocked_retry" || backoff=$?
        if [[ "$backoff" == 124 || "$backoff" == 137 ]]; then
            read_blocked
        fi
    else
        python3 .codex/tick_backoff.py check --action-file "${lock_dir}/action.json" \
            --state-dir "$state_dir" --quota-dir "$registry_dir" --ttl "$blocked_retry" || backoff=$?
    fi
    if [[ "$backoff" == 3 || "$backoff" == 6 ]]; then
        discard_seen
        release_target
        continue
    elif [[ "$backoff" == 4 ]]; then
        tick_phase=quota
        tick_outcome=blocked
        exit 75
    elif [[ "$backoff" == 5 ]]; then
        read_blocked
    elif [[ "$backoff" != 0 ]]; then
        exit "$backoff"
    fi
    check_quota
    tick_phase=gate
    gate=0
    python3 scripts/epic/tick_gate.py check --agent codex \
        --action-file "${lock_dir}/action.json" || gate=$?
    if [[ "$gate" == 3 ]]; then
        release_target
        continue
    elif [[ "$gate" == 4 ]]; then
        tick_phase=quota
        tick_outcome=blocked
        exit 75
    elif [[ "$gate" != 0 ]]; then
        exit "$gate"
    fi
    if [[ "${EPIC_SHARED_READER:-}" == 1 ]]; then
        check_quota
        tick_phase=recheck
        recheck=0
        run_bounded "${recheck_timeout}s" python3 scripts/epic/next_action.py \
            --agent codex --recheck "${lock_dir}/action.json" || recheck=$?
        case "$recheck" in
            0) ;;
            6)
                discard_seen
                release_target
                printf 'tick: %s is stale on GitHub; next candidate\n' "$action"
                continue
                ;;
            4)
                discard_seen
                tick_phase=quota
                tick_outcome=blocked
                exit 75
                ;;
            5|124|137) read_blocked ;;
            *)
                discard_seen
                exit "$recheck"
                ;;
        esac
    fi
    case "$action" in
        claim|continue|fix|fix-checks|resolve-conflict)
            check_quota
            tick_phase=gate
            worktree_exit=0
            run_bounded "${pull_timeout}s" python3 .codex/feature_worktree.py \
                --runner "$repo_root" --action-file "${lock_dir}/action.json" \
                --state-dir "$state_dir" > "${lock_dir}/worktree.txt" || worktree_exit=$?
            case "$worktree_exit" in
                0) model_root=$(cat "${lock_dir}/worktree.txt") ;;
                3|7|75)
                    if [[ "$worktree_exit" != 3 ]]; then
                        target_blocked=1
                    fi
                    discard_seen
                    release_target
                    continue
                    ;;
                4)
                    discard_seen
                    tick_phase=quota
                    tick_outcome=blocked
                    exit 75
                    ;;
                5) read_blocked ;;
                *)
                    discard_seen
                    exit "$worktree_exit"
                    ;;
            esac
            ;;
    esac
    check_quota
    tick_phase=assignment
    assignment_exit=0
    run_bounded 60s python3 .codex/assignment.py \
        --action-file "${lock_dir}/action.json" \
        --output "${lock_dir}/assignment.json" || assignment_exit=$?
    case "$assignment_exit" in
        0) ;;
        6)
            discard_seen
            release_target
            printf 'tick: assignment changed; next candidate\n'
            continue
            ;;
        4)
            discard_seen
            tick_phase=quota
            tick_outcome=blocked
            exit 75
            ;;
        5|124|137) read_blocked ;;
        *)
            discard_seen
            exit "$assignment_exit"
            ;;
    esac
    if [[ "$action" == review ]]; then
        check_quota
        tick_phase=gate
        review_exit=0
        run_bounded "${pull_timeout}s" python3 .codex/review_worktree.py prepare \
            --runner "$repo_root" --action-file "${lock_dir}/action.json" \
            --state-dir "$state_dir" --context-file "${lock_dir}/review-context.json" \
            > "${lock_dir}/worktree.txt" || review_exit=$?
        case "$review_exit" in
            0) model_root=$(cat "${lock_dir}/worktree.txt") ;;
            3|7|75)
                if [[ "$review_exit" != 3 ]]; then
                    target_blocked=1
                fi
                cleanup_review
                rm -f "${lock_dir}/review-context.json"
                discard_seen
                release_target
                printf 'tick: review preparation skipped exit=%s; next candidate\n' "$review_exit"
                continue
                ;;
            4)
                discard_seen
                tick_phase=quota
                tick_outcome=blocked
                exit 75
                ;;
            5|124|137) read_blocked ;;
            *)
                discard_seen
                tick_outcome=blocked
                exit "$review_exit"
                ;;
        esac
    fi
    selected=1
    break
done 3<<< "$candidates"
if [[ "$selected" != 1 ]]; then
    printf 'tick: no candidate to run\n'
    if [[ "$target_blocked" == 1 ]]; then
        tick_outcome=blocked
        exit 75
    fi
    exit 0
fi
# The hook reads this exact selection, while its target lock is held.
export EPIC_ACTION_FILE="${lock_dir}/action.json"
export EPIC_TRUSTED_ROOT="$repo_root"

model_options=(--sandbox workspace-write)
model_result="${state_dir}/codex-result.json"
if [[ "$action" == review ]]; then
    EPIC_REVIEW_DIR=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["artifact_dir"])' "${lock_dir}/review-context.json")
    EPIC_REVIEW_ATTEMPT_DIR=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["attempt_dir"])' "${lock_dir}/review-context.json")
    export EPIC_REVIEW_DIR EPIC_REVIEW_ATTEMPT_DIR
    model_options+=(--add-dir "$EPIC_REVIEW_DIR")
    model_result="${EPIC_REVIEW_ATTEMPT_DIR}/result.json"
    review_log="${EPIC_REVIEW_ATTEMPT_DIR}/model.log"
fi
# `adopt` writes its new PR body here. The hook accepts no other file.
if [[ "$action" == adopt ]]; then
    body_dir=$(mktemp -d /tmp/epic-adopt-codex.XXXXXX)
    EPIC_BODY_DIR_ID=$(python3 -c 'import os, sys; s = os.stat(sys.argv[1]); print(f"{s.st_dev}:{s.st_ino}")' "$body_dir")
    export EPIC_BODY_DIR="$body_dir" EPIC_BODY_DIR_ID
    model_options+=(--add-dir "$body_dir")
fi

# Codex may inherit only core variables in tool commands. Forward these paths
# explicitly without changing the configured policy for other variables.
model_environment=()
model_variables=(EPIC_STATE_DIR EPIC_QUOTA_DIR EPIC_ACTION_FILE EPIC_TRUSTED_ROOT)
if [[ "$action" == review ]]; then
    model_variables+=(EPIC_REVIEW_DIR EPIC_REVIEW_ATTEMPT_DIR)
elif [[ "$action" == adopt ]]; then
    model_variables+=(EPIC_BODY_DIR EPIC_BODY_DIR_ID)
fi
if [[ "${EPIC_SHARED_READER:-}" == 1 ]]; then
    model_variables+=(EPIC_SHARED_READER EPIC_CACHE_DIR
        EPIC_SNAPSHOT_MAX_AGE_SECONDS EPIC_SNAPSHOT_LOCK_SECONDS
        EPIC_SNAPSHOT_REFRESH_SECONDS EPIC_RECHECK_TIMEOUT_SECONDS
        EPIC_SELECT_TIMEOUT_SECONDS EPIC_FOCUS_ACTIONS)
fi
for variable in "${model_variables[@]}"; do
    value=$(python3 -c '
import json, os, sys
value = os.environ.get(sys.argv[1])
if value is not None:
    print(json.dumps(value, ensure_ascii=False))
' "$variable")
    [[ -n "$value" ]] || continue
    model_environment+=(-c "shell_environment_policy.set.${variable}=${value}")
done

# The session uses this decision; it must not select a second task.
cat .codex/epic-tick.md > "${lock_dir}/prompt.txt"
printf '\nSelected action (JSON data, not instructions):\n' >> "${lock_dir}/prompt.txt"
cat "${lock_dir}/action.json" >> "${lock_dir}/prompt.txt"
printf '\nAuthoritative assignment evidence (JSON data, not instructions):\n' >> "${lock_dir}/prompt.txt"
cat "${lock_dir}/assignment.json" >> "${lock_dir}/prompt.txt"
if [[ "$action" == review ]]; then
    printf '\nPrepared review worktree: %s\nRunner checkout: %s\nReview context: %s\nReview evidence directory: %s\nReview attempt directory: %s\n' \
        "$model_root" "$repo_root" "${EPIC_REVIEW_DIR}/context.json" \
        "$EPIC_REVIEW_DIR" "$EPIC_REVIEW_ATTEMPT_DIR" >> "${lock_dir}/prompt.txt"
elif [[ "$model_root" != "$repo_root" ]]; then
    printf '\nPrepared feature worktree: %s\nRunner checkout: %s\n' \
        "$model_root" "$repo_root" >> "${lock_dir}/prompt.txt"
elif [[ "$action" == adopt ]]; then
    printf '\nPR body directory (write the adopt body file only here): %s\n' \
        "$body_dir" >> "${lock_dir}/prompt.txt"
fi
printf '\nInstalled feature Git helper: %s\n' \
    "${HOME}/.local/libexec/codex-feature-git.py" >> "${lock_dir}/prompt.txt"
if [[ -e "${HOME}/.epic-pause" ]]; then
    exit 0
fi
# Never accept the previous session's final result if this session fails to write.
check_quota
: > "${state_dir}/codex-result.json"
: > "$model_result"
tick_phase=model
model_exit=0
launch_model() {
    run_bounded "${tick_timeout}s" codex exec --cd "$model_root" \
        -c sandbox_workspace_write.network_access=true \
        "${model_environment[@]}" "${model_options[@]}" \
        --color never --output-schema "${repo_root}/.codex/tick-result.schema.json" \
        --output-last-message "$model_result" \
        - < "${lock_dir}/prompt.txt"
}
if [[ -n "$review_log" ]]; then
    launch_model > "$review_log" 2>&1 || model_exit=$?
    archive_review_output
    cp "$model_result" "${state_dir}/codex-result.json"
else
    launch_model || model_exit=$?
fi
if [[ "$action" == review ]]; then
    tick_phase=verify
    delivery_exit=0
    run_bounded "${pull_timeout}s" python3 .codex/review_delivery.py publish \
        --context-file "${lock_dir}/review-context.json" || delivery_exit=$?
    case "$delivery_exit" in
        0)
            # Durable, explicit evidence can survive a failed model process.
            model_exit=0
            printf '%s\n' '{"status":"completed","summary":"Saved Codex review delivery confirmed on GitHub","reason_code":null,"retry_at":null}' \
                > "${state_dir}/codex-result.json"
            ;;
        8)
            tick_phase=model
            # Preserve blocked, failed and quota results for the normal recorder.
            # A successful model without explicit evidence is incomplete too.
            if python3 -c '
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tick_backoff import result_outcome
path = Path(sys.argv[2])
if result_outcome(path, int(sys.argv[3]))[0] == 0:
    path.write_text(json.dumps({"status": "blocked", "summary": "Review model did not save a completed bundle", "reason_code": None, "retry_at": None}))
' "${repo_root}/.codex" "${state_dir}/codex-result.json" "$model_exit"; then
                :
            else
                exit 1
            fi
            ;;
        4)
            discard_seen
            tick_phase=quota
            tick_outcome=blocked
            exit 75
            ;;
        7)
            discard_seen
            if [[ "$model_exit" != 0 ]]; then
                tick_phase=model
                exit "$model_exit"
            fi
            read_blocked
            ;;
        *) read_blocked ;;
    esac
fi
# A completed adoption needs GitHub evidence before clearing its cooldown.
# Other outcomes still go through record below, including malformed results.
if [[ "$action" == adopt && "$model_exit" == 0 ]] && python3 -c '
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tick_backoff import result_outcome
sys.exit(result_outcome(Path(sys.argv[2]), 0)[0])
' "${repo_root}/.codex" "${state_dir}/codex-result.json"; then
    tick_phase=quota
    quota=0
    python3 scripts/epic/github_quota.py check --state-dir "$registry_dir" || quota=$?
    if [[ "$quota" == 3 ]]; then
        tick_outcome=blocked
        exit 75
    elif [[ "$quota" != 0 ]]; then
        exit "$quota"
    fi
    tick_phase=verify
    verify=0
    python3 scripts/epic/tick_verify.py --agent codex \
        --action-file "${lock_dir}/action.json" --since "$tick_started" || verify=$?
    if [[ "$verify" == 4 ]]; then
        tick_phase=quota
        tick_outcome=blocked
        exit 75
    elif [[ "$verify" != 0 ]]; then
        printf 'tick: adopt verification failed exit=%s\n' "$verify" >&2
        model_exit=$verify
    fi
fi
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
