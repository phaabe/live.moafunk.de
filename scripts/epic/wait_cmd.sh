#!/usr/bin/env bash
# Run one long command (build, test) and return only when it ends or times out
# (https://github.com/phaabe/live.moafunk.de/issues/426). A model then waits in
# one tool call instead of polling the command many times.
#
#   wait_cmd.sh [--agent <name>] [--timeout <s>] -- <command...>
#
# - The full output goes to <state dir>/<agent>-cmd.log (appended), with a
#   start and an end line. The state dir is $EPIC_STATE_DIR, default
#   ~/.local/state/epic-loop. The agent defaults to the part of $EPIC_AGENT_ID
#   before the first "-" (claude-2 -> claude).
# - Stdout gets only the result: exit code, duration, log path and the last 40
#   lines of this command's output.
# - Exit code: the command's, 124 on timeout (default 900 s), 128+N when this
#   script gets signal N. Usage errors exit 2.
# - The command runs in its own process group. Timeout, INT, TERM and HUP stop
#   the whole group: TERM first, KILL after $WAIT_CMD_KILL_GRACE seconds
#   (default 10).
#
# The environment is never written to the log. The command line is, so do not
# put secrets on it.
set -euo pipefail

usage() {
    printf 'usage: wait_cmd.sh [--agent <name>] [--timeout <seconds>] -- <command...>\n' >&2
    exit 2
}

timeout_s=900
agent=${EPIC_AGENT_ID:-}
agent=${agent%%-*}
while [[ $# -gt 0 ]]; do
    case "$1" in
        --timeout)
            [[ $# -ge 2 ]] || usage
            timeout_s=$2
            shift 2
            ;;
        --agent)
            [[ $# -ge 2 ]] || usage
            agent=$2
            shift 2
            ;;
        --)
            shift
            break
            ;;
        *) usage ;;
    esac
done
[[ $# -gt 0 ]] || usage
if [[ ! "$timeout_s" =~ ^[1-9][0-9]{0,6}$ ]]; then
    printf 'wait_cmd: --timeout must be a positive whole number of seconds\n' >&2
    exit 2
fi
if [[ ! "$agent" =~ ^[a-z][a-z0-9]{0,31}$ ]]; then
    printf 'wait_cmd: give --agent (for example claude or codex)\n' >&2
    exit 2
fi
grace=${WAIT_CMD_KILL_GRACE:-10}
if [[ ! "$grace" =~ ^[0-9]{1,4}$ ]]; then
    printf 'wait_cmd: WAIT_CMD_KILL_GRACE must be a whole number of seconds\n' >&2
    exit 2
fi

state_dir=${EPIC_STATE_DIR:-${HOME}/.local/state/epic-loop}
mkdir -p "$state_dir"
log="${state_dir}/${agent}-cmd.log"
offset=0
work=$(mktemp -d "${TMPDIR:-/tmp}/wait_cmd.XXXXXX")
marker="${work}/timed-out"
child=""
watchdog=""

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Stop the child's whole process group: TERM, then KILL after the grace time.
stop_group() {
    [[ -n "$child" ]] || return 0
    kill -TERM -- "-${child}" 2>/dev/null || return 0
    local i=0
    while [[ "$i" -lt "$grace" ]]; do
        kill -0 -- "-${child}" 2>/dev/null || return 0
        sleep 1
        i=$((i + 1))
    done
    kill -KILL -- "-${child}" 2>/dev/null || true
}

stop_watchdog() {
    if [[ -n "$watchdog" ]]; then
        kill -TERM -- "-${watchdog}" 2>/dev/null || true
        wait "$watchdog" 2>/dev/null || true
        watchdog=""
    fi
}

finish() {
    local rc=$1 note=$2
    local duration=$SECONDS
    local last
    last=$(tail -c "+$((offset + 1))" "$log" | tail -n 40)
    printf '=== %s end: exit=%s duration=%ss%s\n' "$(stamp)" "$rc" "$duration" "$note" >>"$log"
    printf 'wait_cmd: exit=%s duration=%ss%s\n' "$rc" "$duration" "$note"
    printf 'wait_cmd: full output in %s\n' "$log"
    printf -- '--- last 40 lines ---\n%s\n' "$last"
    rm -rf "$work"
    exit "$rc"
}

on_signal() {
    local code=$1
    trap '' INT TERM HUP
    stop_watchdog
    stop_group
    wait "$child" 2>/dev/null || true
    finish "$code" " (stopped by signal)"
}

# Own process groups for the child and the watchdog, so one kill reaches every
# process they start (cargo -> rustc, npm -> node).
set -m
SECONDS=0
printf '=== %s start: cwd=%s timeout=%ss command:' "$(stamp)" "$PWD" "$timeout_s" >>"$log"
printf ' %q' "$@" >>"$log"
printf '\n' >>"$log"
# Where this run's output starts. Another command writing to the same log at
# the same time can mix into the tail; the log itself keeps every line.
offset=$(($(wc -c <"$log")))

trap 'on_signal 130' INT
trap 'on_signal 143' TERM
trap 'on_signal 129' HUP

"$@" </dev/null >>"$log" 2>&1 &
child=$!
(
    sleep "$timeout_s"
    : >"$marker"
    kill -TERM -- "-${child}" 2>/dev/null || true
    sleep "$grace"
    kill -KILL -- "-${child}" 2>/dev/null || true
) </dev/null >/dev/null 2>&1 &
watchdog=$!
# Groups exist now. Job control off again: no "Terminated" job notes on stderr.
set +m

rc=0
wait "$child" || rc=$?
stop_watchdog
if [[ -e "$marker" ]]; then
    # TERM went out already; make sure nothing of the group survives.
    stop_group
    finish 124 " (timeout after ${timeout_s}s)"
fi
finish "$rc" ""
