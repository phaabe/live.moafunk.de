#!/bin/bash
# One headless Claude tick on the architecture epic.
#
# Starts a model session only when there is work: pause, lock contention, idle,
# stop, a stored GitHub quota wait and repeated no-op actions (tick_gate.py)
# exit 0 without one. A quota wait (github_quota.py) also means zero GitHub
# calls; a read that hits the GraphQL quota stores the wait and ends the tick
# with exit 75, before any gate record or model session. The wait is checked
# again before each later GitHub read and before the model starts, because the
# other runner may store it at any time; a quota stop never writes the repeat
# gate, so the action runs again after the reset. Each session
# is fresh, so a tick never re-sends an old conversation. The model and effort
# follow the action: bookkeeping actions use a smaller model.
#
# The selector lists every action in priority order. The tick runs the first
# one it can: its target is not locked by another runner (target_lock.py), it
# still matches GitHub, and it is no suppressed repeat (tick_gate.py). So one
# blocked target never starves the others. The target lock is held on file
# descriptors 8 and 9 until the tick and all its children exited.
#
# Shared reader (EPIC_SHARED_READER=1, off by default; github_state.py): the
# selector reads the shared REST snapshot. Its exit 5 (read blocked) ends the
# tick as blocked with exit 75. After the gate check, `next_action.py
# --recheck` reads the target fresh within EPIC_RECHECK_TIMEOUT_SECONDS: 6
# (stale) skips that candidate, 5 or a timeout ends the tick as blocked. In
# both cases no model starts and no gate record or cooldown is written; the
# state the gate saw is discarded. The recheck time is part of the lock and
# registration budgets. The model session gets EPIC_ACTION_FILE and
# EPIC_TRUSTED_ROOT, so the write checks (write_checks.py) know the action.
#
# Feature worktrees (runner_worktree.py): an action that edits a branch runs
# only in <dir>/<branch> under one fixed directory (EPIC_WORKTREE_DIR, default
# live.moafunk.de-<agent>-wt next to this checkout). The runner creates or
# resumes it after the gate check. When Git refuses the branch because another
# checkout holds it, the log says `handoff needed: <branch> in <path>`, no model
# starts and that checkout stays untouched; the candidate is skipped like a
# suppressed repeat. The model gets the path as EPIC_WORKTREE and --add-dir.
# The worktree step also writes the runner context (context.json in the lock
# dir): the branch, base and PR the permission gate checks git writes against.
#
# Blocked-target cooldown (tick_cooldown.py): a candidate whose action on this
# target, head (and base, for resolve-conflict) came back blocked is skipped for
# EPIC_BLOCKED_COOLDOWN_SECONDS (default 4 hours), even for `continue`; the
# tick tries the next candidate. Only runner evidence sets it: the model's
# structured result (claude-result-schema.json) or a failed verify with the PR
# still open at the selected head. A model exit or timeout is caught, so these
# paths still verify. A blocked tick writes no repeat-gate record, so the retry
# after expiry or a new base reaches the model.
#
# Rebase policy (rebase_policy.py, shared with the Codex runner): before a
# `resolve-conflict` model, `attempt-check` pins the base tip for this attempt
# and skips a key (PR, head, target tip) that already failed
# EPIC_REBASE_ATTEMPT_LIMIT times (default 2); at the limit it adds the label
# needs-anton instead, and retries only that post while it fails. The attempt
# counts from `attempt-start`, right before the model; after verify it is
# recorded as succeeded, failed or (GitHub quota) void. After the session the
# runner posts the rebase record for a proven push (`publish`). The permission
# gate refuses the lease push without a valid test proof, and verify needs the
# proof and the record. Before a `review`, `scope` works out whether a rebase
# record allows a focused review; the prompt carries the result.
#
# Run from a scheduler or a loop, for example:
#   while true; do /bin/bash scripts/epic/claude-tick.sh; sleep 600; done
set -euo pipefail
umask 077

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
state_dir="${EPIC_STATE_DIR:-${HOME}/.local/state/epic-loop}"
tick_timeout=${EPIC_TICK_TIMEOUT_SECONDS:-1800}
select_timeout=${EPIC_SELECT_TIMEOUT_SECONDS:-120}
# Optional agent id (claude, claude-2, ...). A registered agent keeps the same
# files as the legacy state dir in its own folder, agents/<id>/.
agent_id=${EPIC_AGENT_ID:-}
# The runner changes directory later; keep every path absolute.
[[ "$state_dir" == /* ]] || state_dir="${PWD}/${state_dir}"
registry_dir=$state_dir
worktree_dir="${EPIC_WORKTREE_DIR:-$(dirname "$repo_root")/live.moafunk.de-${agent_id:-claude}-wt}"
if [[ "$worktree_dir" != /* ]]; then
    printf 'tick: EPIC_WORKTREE_DIR must be an absolute path\n' >&2
    exit 2
fi

if [[ -e "${HOME}/.epic-pause" ]]; then
    exit 0
fi
if [[ -n "$agent_id" ]]; then
    if [[ ! "$agent_id" =~ ^claude(-[a-z0-9]{1,16})?$ ]]; then
        printf 'tick: EPIC_AGENT_ID must look like claude or claude-2\n' >&2
        exit 2
    fi
    state_dir="${registry_dir}/agents/${agent_id}"
fi
# The Python helpers read the state dir from the environment, after the cd.
export EPIC_STATE_DIR="$state_dir"
# One GitHub quota for all agents: its wait file lives in the shared state dir.
export EPIC_QUOTA_DIR="$registry_dir"
lock_dir="${state_dir}/claude.lock"
log_file="${state_dir}/claude.log"
events_file="${state_dir}/claude-ticks.jsonl"
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
shared_reader=0
recheck_timeout=0
if [[ "${EPIC_SHARED_READER:-}" == 1 ]]; then
    shared_reader=1
    recheck_timeout=${EPIC_RECHECK_TIMEOUT_SECONDS:-60}
fi
for duration in "$tick_timeout" "$select_timeout"; do
    if [[ ! "$duration" =~ ^[1-9][0-9]*$ ]]; then
        printf 'tick: timeout must be a positive integer in seconds\n' >&2
        exit 2
    fi
done
if [[ "$shared_reader" == 1 && ! "$recheck_timeout" =~ ^[1-9][0-9]*$ ]]; then
    printf 'tick: timeout must be a positive integer in seconds\n' >&2
    exit 2
fi
# Lock and registration budget: selection, recheck, model and cleanup.
budget=$((select_timeout + recheck_timeout + tick_timeout + 10))
if [[ -n "$agent_id" ]]; then
    python3 "${repo_root}/scripts/epic/agents.py" --state-dir "$registry_dir" \
        register --id "$agent_id" --kind claude --label "${EPIC_AGENT_LABEL:-}" \
        --interval "${EPIC_AGENT_INTERVAL_SECONDS:-600}" \
        --budget "$budget"
fi
if command -v timeout >/dev/null 2>&1; then
    timeout_bin=timeout
elif command -v gtimeout >/dev/null 2>&1; then
    timeout_bin=gtimeout
else
    printf 'tick: GNU timeout or gtimeout is required\n' >&2
    exit 1
fi

# 0 when GitHub may be read. A stored quota wait ends the tick with exit 0 and
# no GitHub call; a bad wait file stops it with that error.
quota_open() {
    local result=0
    python3 "${repo_root}/scripts/epic/github_quota.py" check \
        --state-dir "$registry_dir" || result=$?
    if [[ "$result" == 0 ]]; then
        return 0
    elif [[ "$result" == 3 ]]; then
        return 1
    fi
    exit "$result"
}
quota_stop() {
    printf 'tick: stopped on the GitHub GraphQL quota; wait stored\n' >&2
    tick_outcome=blocked
    tick_phase=quota
    exit 75
}
# A blocked or timed-out GitHub read (shared reader): no action from it.
read_blocked() {
    printf 'tick: GitHub read blocked in %s; no action\n' "$1" >&2
    tick_outcome=blocked
    tick_phase=$1
    exit 75
}

if ! quota_open; then
    exit 0
fi

# Same lock helper as the Codex runner, with its own lock directory.
if python3 "${repo_root}/.codex/epic_lock.py" "$lock_dir" "$$" "$budget"; then
    :
else
    result=$?
    if [[ "$result" == 75 ]]; then
        exit 0
    fi
    exit "$result"
fi
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
    rm -f "${lock_dir}/action.json" "${lock_dir}/prompt.txt" "${lock_dir}/owner.json" \
        "${lock_dir}/context.json" "${lock_dir}/result.json" "${lock_dir}/cooldown.json" \
        "${lock_dir}/attempt.json" "${lock_dir}/scope.json"
    rmdir "$lock_dir"
}
# Stop the running child (selector or model) before the lock is released, so a
# stopped runner never leaves a model working while a new tick starts.
child_pid=""
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

# Runs "$@" under timeout in the background so the traps above can fire.
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

tick_started=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
printf '\ntick: started %s repo=%s\n' "$tick_started" "$repo_root"
# A failed event write never changes the tick: it only skips the finish event.
if tick_offset=$(python3 "$events" start --file "$events_file" \
    --tick "$tick_started" --log "$log_file"); then
    events_open=1
else
    tick_offset=0
fi
cd "$repo_root"
tick_phase=refresh
# Keep the runner on the latest scripts and tick instructions. The checkout only
# runs ticks, so a failed fast-forward (local changes, diverged) stops the tick.
# First restore GitNexus-only changes in AGENTS.md / CLAUDE.md; any other
# tracked change stops the tick untouched (gitnexus_noise.py).
if ! python3 scripts/epic/gitnexus_noise.py; then
    exit 1
fi
if ! git pull -q --ff-only; then
    printf 'tick: git pull --ff-only failed; fix the runner checkout\n' >&2
    exit 1
fi
tick_phase=select
select=0
run_bounded "${select_timeout}s" \
    python3 scripts/epic/next_action.py --agent claude --candidates \
    > "${lock_dir}/action.json" || select=$?
case "$select" in
    0) ;;
    3) exit 0 ;;
    4) quota_stop ;;
    5) read_blocked select ;;
    *) exit "$select" ;;
esac
candidates=$(cat "${lock_dir}/action.json")
printf '%s\n' "$candidates"
# The other runner may have stored a quota wait while the selector ran.
if ! quota_open; then
    tick_phase=quota
    exit 0
fi

release_target() {
    exec 8>&- 9>&-
}
# The gate check saved the state it saw for its record; a candidate that does
# not run must not leave it behind.
discard_seen() {
    rm -f "${state_dir}/claude-gate-seen.json"
}
# Locks the targets of action.json on fds 8 and 9. 1 when another runner holds one.
# Called from `if`, where set -e is off: every failure exits explicitly.
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
    fi
    if [[ "${#targets[@]}" -gt 2 ]]; then
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

# No cap: a cap that restarts at the top every tick starves later candidates
# while the first ones stay blocked. A skipped candidate costs one REST read.
selected=0
worktree=""
while IFS= read -r candidate; do
    if [[ -z "$candidate" ]]; then
        continue
    fi
    printf '%s\n' "$candidate" > "${lock_dir}/action.json"
    action=$(python3 -c 'import json, sys; print(json.load(sys.stdin)["action"])' \
        < "${lock_dir}/action.json")
    case "$action" in
        idle|stop) exit 0 ;;
        merge|escalate) model=sonnet effort=low ;;
        adopt) model=sonnet effort=medium ;;
        claim) model=opus effort=medium ;;
        review|fix|fix-checks|resolve-conflict|continue) model=opus effort=high ;;
        *)
            printf 'tick: unknown action %s\n' "$action" >&2
            exit 1
            ;;
    esac
    tick_phase=lock
    if ! lock_target; then
        printf 'tick: %s target locked by another runner; next candidate\n' "$action"
        continue
    fi
    # A skipped candidate's gate (or the other runner) may have stored a quota
    # wait: no further GitHub read then.
    if ! quota_open; then
        tick_phase=quota
        exit 0
    fi
    # A resolve-conflict key at its attempt limit starts no model; the check
    # posts the escalation label (again, if the last post failed).
    tick_phase=backoff
    if [[ "$action" == resolve-conflict ]]; then
        attempts=0
        python3 scripts/epic/rebase_policy.py attempt-check --state-dir "$registry_dir" \
            --agent claude --action-file "${lock_dir}/action.json" \
            --out "${lock_dir}/attempt.json" || attempts=$?
        case "$attempts" in
            0) ;;
            3)
                release_target
                printf 'tick: %s reached its attempt limit; next candidate\n' "$action"
                continue
                ;;
            4) quota_stop ;;
            5) read_blocked backoff ;;
            *) exit "$attempts" ;;
        esac
    fi
    # A blocked target cools down: no model, next candidate. No gate state yet.
    cooldown=0
    python3 scripts/epic/tick_cooldown.py check --state-dir "$registry_dir" \
        --action-file "${lock_dir}/action.json" \
        --seen-file "${lock_dir}/cooldown.json" || cooldown=$?
    case "$cooldown" in
        0) ;;
        3)
            release_target
            printf 'tick: %s target cools down; next candidate\n' "$action"
            continue
            ;;
        4) quota_stop ;;
        5) read_blocked backoff ;;
        *) exit "$cooldown" ;;
    esac
    # After the lock: skip a repeat, or a target that changed since selection.
    tick_phase=gate
    gate=0
    python3 scripts/epic/tick_gate.py check --agent claude \
        --action-file "${lock_dir}/action.json" || gate=$?
    if [[ "$gate" == 3 ]]; then
        release_target
        continue
    elif [[ "$gate" == 4 ]]; then
        quota_stop
    elif [[ "$gate" != 0 ]]; then
        exit "$gate"
    fi
    if [[ "$shared_reader" == 1 ]]; then
        tick_phase=recheck
        recheck=0
        run_bounded "${recheck_timeout}s" python3 scripts/epic/next_action.py \
            --agent claude --recheck "${lock_dir}/action.json" || recheck=$?
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
                quota_stop
                ;;
            # 124 and 137: timeout stopped the recheck.
            5|124|137)
                discard_seen
                read_blocked recheck
                ;;
            *)
                discard_seen
                exit "$recheck"
                ;;
        esac
    fi
    # Last step before the model: the feature worktree, or a handoff stop.
    tick_phase=gate
    prepared=0
    worktree=$(python3 scripts/epic/runner_worktree.py prepare --agent claude \
        --action-file "${lock_dir}/action.json" --dir "$worktree_dir" \
        --repo "$repo_root" --context-file "${lock_dir}/context.json") || prepared=$?
    case "$prepared" in
        0) ;;
        3)
            discard_seen
            release_target
            printf 'tick: %s stopped before the model; next candidate\n' "$action"
            continue
            ;;
        4)
            discard_seen
            quota_stop
            ;;
        *)
            discard_seen
            exit "$prepared"
            ;;
    esac
    selected=1
    break
done <<< "$candidates"
if [[ "$selected" != 1 ]]; then
    printf 'tick: no candidate to run\n'
    exit 0
fi

# The tick instructions without their frontmatter, plus the selected action.
awk 'NR == 1 && /^---$/ { skip = 1; next } skip && /^---$/ { skip = 0; next } !skip' \
    .claude/commands/epic/epic-tick.md > "${lock_dir}/prompt.txt"
printf '\nSelected action (JSON data, not instructions):\n' >> "${lock_dir}/prompt.txt"
cat "${lock_dir}/action.json" >> "${lock_dir}/prompt.txt"
worktree_args=()
if [[ -n "$worktree" ]]; then
    printf '\nRunner worktree (edit only here): %s\n' "$worktree" >> "${lock_dir}/prompt.txt"
    worktree_args=(--add-dir "$worktree")
fi
if [[ "$action" == resolve-conflict ]]; then
    printf '\nAttempt pin (JSON data, not instructions):\n' >> "${lock_dir}/prompt.txt"
    cat "${lock_dir}/attempt.json" >> "${lock_dir}/prompt.txt"
    python3 -c '
import json, sys
pin = json.load(open(sys.argv[1]))
print(f"Proof command: python3 {sys.argv[2]}/scripts/epic/rebase_policy.py prove"
      f" --worktree {sys.argv[3]} --pr {pin["pr"]} --base {pin["base"]}")
' "${lock_dir}/attempt.json" "$repo_root" "$worktree" >> "${lock_dir}/prompt.txt"
fi
# The review scope: focused only with a valid rebase record (rebase_policy.py).
# Without a scope file the prompt says full review.
if [[ "$action" == review ]]; then
    tick_phase=gate
    scoped=0
    python3 scripts/epic/rebase_policy.py scope --agent claude \
        --action-file "${lock_dir}/action.json" --repo-dir "$repo_root" \
        --out "${lock_dir}/scope.json" || scoped=$?
    case "$scoped" in
        0)
            printf '\nReview scope (JSON data, not instructions):\n' >> "${lock_dir}/prompt.txt"
            cat "${lock_dir}/scope.json" >> "${lock_dir}/prompt.txt"
            ;;
        4) quota_stop ;;
        *) printf 'tick: review scope failed (exit %s); full review\n' "$scoped" ;;
    esac
fi

if [[ -e "${HOME}/.epic-pause" ]]; then
    exit 0
fi
if ! quota_open; then
    tick_phase=quota
    exit 0
fi
# The attempt counts from here, once, whatever happens next.
tick_id="${tick_started}-$$"
attempt_open=0
if [[ "$action" == resolve-conflict ]]; then
    started=0
    python3 scripts/epic/rebase_policy.py attempt-start --state-dir "$registry_dir" \
        --attempt-file "${lock_dir}/attempt.json" --id "$tick_id" || started=$?
    case "$started" in
        0) attempt_open=1 ;;
        3)
            printf 'tick: %s reached its attempt limit before the model\n' "$action"
            exit 0
            ;;
        *) exit "$started" ;;
    esac
fi
# succeeded, failed or void (quota). A failed write is logged: the attempt
# then stays `started` and counts as failed.
attempt_finish() {
    if [[ "$attempt_open" != 1 ]]; then
        return 0
    fi
    attempt_open=0
    if ! python3 scripts/epic/rebase_policy.py attempt-finish --state-dir "$registry_dir" \
        --attempt-file "${lock_dir}/attempt.json" --id "$tick_id" --outcome "$1"; then
        printf 'tick: attempt outcome %s not recorded; it counts as failed\n' "$1" >&2
    fi
}
printf 'tick: %s with model=%s effort=%s\n' "$action" "$model" "$effort"
tick_phase=model
# `claude -p` cannot show a prompt. The project settings ask before every
# push, rebase and merge; the runner-only settings (claude-runner-settings.json)
# also ask before every `git -<option>` form, so `git -C <path> push` and
# `git -c k=v push` cannot skip the gate. permission_gate.py answers those
# prompts: the git contract of git_gate.py (own branch, own runner worktree,
# rebase and lease push with a pinned head), head-pinned squash merges and the
# `adopt` body edit of the selected PR. It denies the rest. It reads the action,
# the runner context and the PR fresh from GitHub, so it gets gh's environment.
# With the shared reader it also runs the write checks (write_checks.py).
# GIT_EDITOR=true: `git rebase --continue` must not wait for an editor.
gate_config=$(python3 -c '
import json, os, sys
env = {
    "EPIC_STATE_DIR": sys.argv[2],
    "EPIC_ACTION_FILE": sys.argv[3],
    "EPIC_TRUSTED_ROOT": sys.argv[4],
    "EPIC_CONTEXT_FILE": sys.argv[5],
    "EPIC_WORKTREE_DIR": sys.argv[6],
    "EPIC_ATTEMPT_FILE": sys.argv[7],
}
for name, value in os.environ.items():
    if name.startswith(("EPIC_", "GH_")) or name in (
        "HOME", "PATH", "USER", "LOGNAME", "TMPDIR", "XDG_CONFIG_HOME"
    ):
        env.setdefault(name, value)
print(json.dumps({"mcpServers": {"epic-gate": {
    "command": "python3", "args": [sys.argv[1]], "env": env}}}))
' "${repo_root}/scripts/epic/permission_gate.py" "$state_dir" "${lock_dir}/action.json" \
    "$repo_root" "${lock_dir}/context.json" "$worktree_dir" "${lock_dir}/attempt.json")
# The write-check hook (.claude/hooks/scripts/epic_guard.py) reads these two.
# The session's JSON result goes to result.json and then into the log. Its exit
# (124 or 137 on timeout) is kept: the action is still verified.
model_exit=0
run_bounded "${tick_timeout}s" \
    env EPIC_ACTION_FILE="${lock_dir}/action.json" EPIC_TRUSTED_ROOT="$repo_root" \
    EPIC_WORKTREE="$worktree" EPIC_ATTEMPT_FILE="${lock_dir}/attempt.json" GIT_EDITOR=true \
    claude -p --model "$model" --effort "$effort" --permission-mode auto \
    --settings "${repo_root}/scripts/epic/claude-runner-settings.json" \
    --mcp-config "$gate_config" --permission-prompt-tool mcp__epic-gate__approve \
    --output-format json \
    --json-schema "$(cat "${repo_root}/scripts/epic/claude-result-schema.json")" \
    ${worktree_args[@]+"${worktree_args[@]}"} < "${lock_dir}/prompt.txt" \
    > "${lock_dir}/result.json" || model_exit=$?
cat "${lock_dir}/result.json" || true
printf '\ntick: model exit=%s\n' "$model_exit"
# A session can exit 0 while its push or merge was denied. Check GitHub.
# A quota wait (stored by the other runner during the session, or by verify)
# ends the tick before the gate record, so the unverified action is not
# skipped as a repeat after the reset.
if ! quota_open; then
    printf 'tick: not verified, GitHub quota wait\n' >&2
    attempt_finish void
    tick_outcome=blocked
    tick_phase=quota
    exit 75
fi
# The owner's rebase record, for a proven push of this attempt. Nothing to
# publish (exit 1) or a failed post leaves verify to fail the tick.
if [[ "$action" == resolve-conflict ]]; then
    tick_phase=record
    published=0
    python3 scripts/epic/rebase_policy.py publish --agent claude \
        --attempt-file "${lock_dir}/attempt.json" --worktree "$worktree" \
        --rebases-file "${state_dir}/claude-rebases.json" || published=$?
    if [[ "$published" == 4 ]]; then
        attempt_finish void
        quota_stop
    fi
fi
tick_phase=verify
verify=0
verify_args=()
if [[ "$action" == resolve-conflict ]]; then
    verify_args=(--attempt-file "${lock_dir}/attempt.json")
fi
# With a worktree, the new PR head must be that worktree's finished work: a
# refused lease push or an unfinished rebase stays a failed tick.
python3 scripts/epic/tick_verify.py --agent claude --worktree "$worktree" \
    ${verify_args[@]+"${verify_args[@]}"} \
    --action-file "${lock_dir}/action.json" --since "$tick_started" || verify=$?
if [[ "$verify" == 4 ]]; then
    attempt_finish void
    quota_stop
fi
# Evidence decides: cooldown (no gate record), gate record, or neither.
tick_phase=result
recorded=0
python3 scripts/epic/tick_cooldown.py record --state-dir "$registry_dir" \
    --action-file "${lock_dir}/action.json" --seen-file "${lock_dir}/cooldown.json" \
    --result-file "${lock_dir}/result.json" --model-exit "$model_exit" \
    --verify-exit "$verify" || recorded=$?
if [[ "$recorded" == 4 ]]; then
    attempt_finish void
elif [[ "$verify" == 0 ]]; then
    attempt_finish succeeded
else
    attempt_finish failed
fi
case "$recorded" in
    # Landed, or (after a model exit 0) not landed without evidence: record
    # the gate, so a failure is reported without a retry storm.
    0|1)
        tick_phase=record
        python3 scripts/epic/tick_gate.py record --agent claude \
            --action-file "${lock_dir}/action.json"
        ;;
    3)
        discard_seen
        if [[ "$model_exit" == 0 ]]; then
            tick_outcome=blocked
        fi
        ;;
    4)
        discard_seen
        printf 'tick: not verified, model reported the GitHub quota\n' >&2
        tick_outcome=blocked
        tick_phase=quota
        exit 75
        ;;
    6) discard_seen ;;
    *)
        discard_seen
        exit "$recorded"
        ;;
esac
if [[ "$model_exit" != 0 ]]; then
    tick_phase=model
    exit "$model_exit"
fi
if [[ "$verify" != 0 ]]; then
    tick_phase=verify
fi
exit "$verify"
