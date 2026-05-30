#!/usr/bin/env bash
# Synchronous freshness gate. Blocks until the GitNexus index provably matches the
# current code, then exits 0. Called as a pre-step by /git.commit, /workflow.ship,
# /quality.review, and the impact-analysis preflight — the points where the index
# is relied upon for a decision. Exits non-zero only if a reindex genuinely fails.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$SCRIPT_DIR/gitnexus-reindex.sh"

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[ -z "$REPO_ROOT" ] && { echo "ensure-fresh: not in a git repo — skipping." >&2; exit 0; }
GITNEXUS_DIR="$REPO_ROOT/.gitnexus"

is_fresh() {
  # Fresh iff: no failure marker, index newer than HEAD, and no uncommitted source changes.
  [ -d "$GITNEXUS_DIR" ] || return 1
  [ -f "$GITNEXUS_DIR/.stale" ] && return 1
  local idx head dirty
  if [ "$(uname -s)" = "Darwin" ]; then idx="$(stat -f %m "$GITNEXUS_DIR" 2>/dev/null || echo 0)"
  else idx="$(stat -c %Y "$GITNEXUS_DIR" 2>/dev/null || echo 0)"; fi
  head="$(git -C "$REPO_ROOT" log -1 --format=%ct 2>/dev/null || echo 0)"
  dirty="$(git -C "$REPO_ROOT" status --porcelain -- ':!.gitnexus' 2>/dev/null | wc -l | tr -d ' ')"
  case "$idx$head$dirty" in *[!0-9]*) return 1 ;; esac
  [ "$head" -gt "$idx" ] && return 1
  [ "$dirty" -gt 0 ] && return 1
  return 0
}

if is_fresh; then
  echo "GitNexus: index fresh — proceeding." >&2
  exit 0
fi

echo "GitNexus: index stale — refreshing synchronously before continuing…" >&2
GN_WAIT=1 GN_REPO_ROOT="$REPO_ROOT" bash "$HELPER" "ensure-fresh"
rc=$?

if [ "$rc" -ne 0 ]; then
  echo "⚠ GitNexus: synchronous reindex FAILED (rc=$rc). Index may be stale — proceed with caution." >&2
  echo "  Tail: $GITNEXUS_DIR/logs/$(basename "$REPO_ROOT").log" >&2
  exit "$rc"
fi

echo "GitNexus: index refreshed — proceeding." >&2
exit 0
