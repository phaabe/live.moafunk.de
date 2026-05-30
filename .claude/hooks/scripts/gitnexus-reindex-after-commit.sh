#!/usr/bin/env bash
# PostToolUse hook (matcher: Bash). After a successful `git commit`, schedule a
# background reindex via the shared helper. The tree is already clean at this
# point, and the helper uses --skip-agents-md + self-heal, so it stays clean.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$SCRIPT_DIR/gitnexus-reindex.sh"

INPUT="$(cat)"
CMD="$(printf '%s' "$INPUT" | jq -r '.tool_input.command // .toolInput.command // empty' 2>/dev/null)"
[ -z "$CMD" ] && exit 0

# Only on real commits — skip dry-runs and message-less helpers.
printf '%s' "$CMD" | grep -Eq '(^|[;&|[:space:]])git[[:space:]]+(-[^[:space:]]+[[:space:]]+)*commit([[:space:]]|$)' || exit 0
printf '%s' "$CMD" | grep -Eq -- '--dry-run' && exit 0

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[ -z "$REPO_ROOT" ] && exit 0
[ -d "$REPO_ROOT/.gitnexus" ] || exit 0

( GN_REPO_ROOT="$REPO_ROOT" bash "$HELPER" "post-commit" ) </dev/null >/dev/null 2>&1 &
disown 2>/dev/null || true
exit 0
