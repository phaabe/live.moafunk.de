#!/usr/bin/env bash
# Shared GitNexus reindex helper — the single place the "fresh index, clean tree"
# guarantee lives. Runs ONE synchronous `gitnexus analyze --skip-agents-md`, then
# self-heals any GitNexus-managed tracked doc it may have dirtied, and maintains a
# `.stale` marker so a failed reindex is never silent.
#
# Usage:   gitnexus-reindex.sh [reason]
# Env:     GN_WAIT=1   wait for an in-progress reindex to finish, then run anyway
#                      (used by the synchronous ensure-fresh gate)
#          GN_REPO_ROOT=<path>  override repo-root detection
#
# Callers that want background behaviour invoke this with `( ... ) & disown`.
set -uo pipefail

REASON="${1:-manual}"
REPO_ROOT="${GN_REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
[ -z "$REPO_ROOT" ] && exit 0

GITNEXUS_DIR="$REPO_ROOT/.gitnexus"
REPO_NAME="$(basename "$REPO_ROOT")"
LOG_DIR="$GITNEXUS_DIR/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/${REPO_NAME}.log"
LOCKFILE="$GITNEXUS_DIR/.analyze.lock"
DEBOUNCE_FILE="$GITNEXUS_DIR/.last-analyze-ts"
STALE_MARKER="$GITNEXUS_DIR/.stale"

log() { echo "[$(date -Is 2>/dev/null || date)] $*" >>"$LOG_FILE"; }

lock_alive() { [ -f "$LOCKFILE" ] && kill -0 "$(cat "$LOCKFILE" 2>/dev/null)" 2>/dev/null; }

# If another reindex is running, either wait for it (GN_WAIT) or skip (debounced path).
if lock_alive; then
  if [ "${GN_WAIT:-0}" = "1" ]; then
    log "reindex ($REASON): waiting for in-progress analyze"
    for _ in $(seq 1 180); do lock_alive || break; sleep 1; done
  else
    exit 0
  fi
fi

mkdir -p "$GITNEXUS_DIR"
echo $$ >"$LOCKFILE"
date +%s >"$DEBOUNCE_FILE"
trap 'rm -f "$LOCKFILE"' EXIT

cd "$REPO_ROOT" || exit 0

# Snapshot managed paths that were ALREADY dirty, so we never clobber real edits.
MANAGED="CLAUDE.md AGENTS.md .claude/skills/gitnexus"
before="$(git status --porcelain -- $MANAGED 2>/dev/null)"

log "reindex ($REASON): analyze --skip-agents-md"
npx --yes gitnexus analyze --skip-agents-md >>"$LOG_FILE" 2>&1   # never --skills
rc=$?
log "reindex ($REASON): analyze exited rc=$rc"

# Self-heal: restore ONLY managed paths that analyze itself newly dirtied.
for p in $MANAGED; do
  git status --porcelain -- "$p" 2>/dev/null | grep -q . || continue   # still clean → skip
  printf '%s\n' "$before" | grep -qF -- "$p" && continue               # was dirty before → leave it
  git restore -- "$p" 2>>"$LOG_FILE" || git checkout -- "$p" 2>>"$LOG_FILE"
  log "reindex ($REASON): self-heal restored $p"
done

# Never silently stale: marker on failure, cleared on success.
if [ "$rc" -ne 0 ]; then
  echo "analyze rc=$rc reason=$REASON @ $(date -Is 2>/dev/null || date)" >"$STALE_MARKER"
else
  rm -f "$STALE_MARKER"
fi

exit "$rc"
