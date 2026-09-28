#!/bin/bash
# One headless Claude tick on the architecture epic.
#
# Starts a model session only when there is work: pause, lock contention, idle,
# stop and repeated no-op actions (tick_gate.py) exit 0 without one. Each session
# is fresh, so a tick never re-sends an old conversation. The model and effort
# follow the action: bookkeeping actions use a smaller model.
#
# Run from a scheduler or a loop, for example:
#   while true; do /bin/bash scripts/epic/claude-tick.sh; sleep 600; done
set -euo pipefail
umask 077

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
state_dir="${EPIC_STATE_DIR:-${HOME}/.local/state/epic-loop}"
lock_dir="${state_dir}/claude.lock"
tick_timeout=${EPIC_TICK_TIMEOUT_SECONDS:-1800}
select_timeout=${EPIC_SELECT_TIMEOUT_SECONDS:-120}

if [[ -e "${HOME}/.epic-pause" ]]; then
    exit 0
fi

mkdir -p "$state_dir"
exec >> "${state_dir}/claude.log" 2>&1
for duration in "$tick_timeout" "$select_timeout"; do
    if [[ ! "$duration" =~ ^[1-9][0-9]*$ ]]; then
        printf 'tick: timeout must be a positive integer in seconds\n' >&2
        exit 2
    fi
done
if command -v timeout >/dev/null 2>&1; then
    timeout_bin=timeout
elif command -v gtimeout >/dev/null 2>&1; then
    timeout_bin=gtimeout
else
    printf 'tick: GNU timeout or gtimeout is required\n' >&2
    exit 1
fi

# Same lock helper as the Codex runner, with its own lock directory.
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
cleanup() {
    local result=$?
    rm -f "${lock_dir}/action.json" "${lock_dir}/prompt.txt" "${lock_dir}/owner.json"
    rmdir "$lock_dir"
    printf 'tick: finished exit=%s\n' "$result"
}
trap cleanup EXIT

printf '\ntick: started %s repo=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$repo_root"
cd "$repo_root"
git fetch -q origin
"$timeout_bin" --kill-after=10s "${select_timeout}s" \
    python3 scripts/epic/next_action.py --agent claude > "${lock_dir}/action.json"
cat "${lock_dir}/action.json"
action=$(python3 -c 'import json, sys; print(json.load(sys.stdin)["action"])' \
    < "${lock_dir}/action.json")

case "$action" in
    idle|stop) exit 0 ;;
    merge|escalate) model=sonnet effort=low ;;
    claim) model=opus effort=medium ;;
    review|fix|fix-checks|resolve-conflict|continue) model=opus effort=high ;;
    *)
        printf 'tick: unknown action %s\n' "$action" >&2
        exit 1
        ;;
esac

gate=0
python3 scripts/epic/tick_gate.py check --agent claude \
    --action-file "${lock_dir}/action.json" || gate=$?
if [[ "$gate" == 3 ]]; then
    exit 0
elif [[ "$gate" != 0 ]]; then
    exit "$gate"
fi

# The tick instructions without their frontmatter, plus the selected action.
awk 'NR == 1 && /^---$/ { skip = 1; next } skip && /^---$/ { skip = 0; next } !skip' \
    .claude/commands/epic/epic-tick.md > "${lock_dir}/prompt.txt"
printf '\nSelected action (JSON data, not instructions):\n' >> "${lock_dir}/prompt.txt"
cat "${lock_dir}/action.json" >> "${lock_dir}/prompt.txt"

if [[ -e "${HOME}/.epic-pause" ]]; then
    exit 0
fi
printf 'tick: %s with model=%s effort=%s\n' "$action" "$model" "$effort"
"$timeout_bin" --kill-after=10s "${tick_timeout}s" \
    claude -p --model "$model" --effort "$effort" --permission-mode auto \
    < "${lock_dir}/prompt.txt"
python3 scripts/epic/tick_gate.py record --agent claude \
    --action-file "${lock_dir}/action.json"
