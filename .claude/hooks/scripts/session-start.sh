#!/usr/bin/env bash
# SessionStart hook — prints a one-line context greeting.
set -uo pipefail

CWD="$(pwd)"
DATE="$(date '+%Y-%m-%d %H:%M')"

# Try to gather git context if we're in a repo
BRANCH=""
LAST_COMMIT=""
if git -C "$CWD" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  BRANCH=$(git -C "$CWD" rev-parse --abbrev-ref HEAD 2>/dev/null)
  LAST_COMMIT=$(git -C "$CWD" log -1 --pretty='%h %s' 2>/dev/null)
fi

{
  echo "── session started · $DATE ──"
  echo "cwd:    $CWD"
  if [ -n "$BRANCH" ]; then
    echo "branch: $BRANCH"
    echo "head:   $LAST_COMMIT"
  fi
} >&2

exit 0
