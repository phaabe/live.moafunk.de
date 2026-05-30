#!/usr/bin/env bash
# SessionStart hook: ensure the GitNexus index for the current repo exists and is
# fresh. Builds/refreshes in the background (never blocks the session) via the
# shared reindex helper, and surfaces a loud warning if a prior reindex failed.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$SCRIPT_DIR/gitnexus-reindex.sh"

CWD="$(pwd)"
REPO_ROOT="$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null || true)"
[ -z "$REPO_ROOT" ] && exit 0

REPO_NAME="$(basename "$REPO_ROOT")"
GITNEXUS_DIR="$REPO_ROOT/.gitnexus"
LOG_DIR="$GITNEXUS_DIR/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/${REPO_NAME}.log"

# Never silently stale: a prior failed reindex left a marker.
if [ -f "$GITNEXUS_DIR/.stale" ]; then
  echo "⚠ GitNexus STALE for $REPO_NAME — last reindex failed ($(cat "$GITNEXUS_DIR/.stale"))." >&2
  echo "  Fix: npx gitnexus analyze --skip-agents-md   (tail: $LOG_FILE)" >&2
fi

bg_reindex() { ( GN_REPO_ROOT="$REPO_ROOT" bash "$HELPER" "$1" ) </dev/null >/dev/null 2>&1 & disown 2>/dev/null || true; }

# Portable mtime in epoch seconds (BSD vs GNU stat).
if [ "$(uname -s)" = "Darwin" ]; then mtime() { stat -f %m "$1" 2>/dev/null || echo 0; }
else mtime() { stat -c %Y "$1" 2>/dev/null || echo 0; }; fi

# Case 1: no index yet → build it.
if [ ! -d "$GITNEXUS_DIR" ]; then
  echo "GitNexus: no index for $REPO_NAME — building in background. Tail: $LOG_FILE" >&2
  bg_reindex "session-start: no index"
  exit 0
fi

# Case 2: index exists — check freshness (index mtime vs HEAD commit ts + dirty count).
LAST_INDEX_TS="$(mtime "$GITNEXUS_DIR")"
LAST_COMMIT_TS="$(git -C "$REPO_ROOT" log -1 --format=%ct 2>/dev/null || echo 0)"
LAST_DIRTY_FILES="$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
case "$LAST_INDEX_TS" in *[!0-9]* | "") LAST_INDEX_TS=0 ;; esac
case "$LAST_COMMIT_TS" in *[!0-9]* | "") LAST_COMMIT_TS=0 ;; esac
case "$LAST_DIRTY_FILES" in *[!0-9]* | "") LAST_DIRTY_FILES=0 ;; esac

STALE=0
[ "$LAST_COMMIT_TS" -gt "$LAST_INDEX_TS" ] && STALE=1
[ "$LAST_DIRTY_FILES" -gt 5 ] && STALE=1

if [ "$STALE" -eq 1 ]; then
  echo "GitNexus: index for $REPO_NAME is stale — refreshing in background. Tail: $LOG_FILE" >&2
  bg_reindex "session-start: stale (commit_ts=$LAST_COMMIT_TS index_ts=$LAST_INDEX_TS dirty=$LAST_DIRTY_FILES)"
else
  AGE_MIN=$(( ( $(date +%s) - LAST_INDEX_TS ) / 60 ))
  echo "GitNexus: index for $REPO_NAME is fresh (last refresh ${AGE_MIN}m ago). Use gitnexus_* tools first." >&2
fi

exit 0
