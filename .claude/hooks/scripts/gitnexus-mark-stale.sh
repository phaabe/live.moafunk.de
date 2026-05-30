#!/usr/bin/env bash
# PostToolUse hook for Edit/Write/MultiEdit: schedule a background reindex
# (debounced 60s) via the shared helper. Only refreshes an existing index.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$SCRIPT_DIR/gitnexus-reindex.sh"

INPUT="$(cat)"
FILE="$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // .toolInput.file_path // .tool_input.path // .toolInput.path // empty' 2>/dev/null)"
[ -z "$FILE" ] && exit 0
[ ! -f "$FILE" ] && exit 0

REPO_ROOT="$(git -C "$(dirname "$FILE")" rev-parse --show-toplevel 2>/dev/null || true)"
[ -z "$REPO_ROOT" ] && exit 0
GITNEXUS_DIR="$REPO_ROOT/.gitnexus"
[ -d "$GITNEXUS_DIR" ] || exit 0   # only refresh an existing index

# Debounce: skip if a reindex was scheduled < 60s ago.
DEBOUNCE_FILE="$GITNEXUS_DIR/.last-analyze-ts"
NOW="$(date +%s)"
LAST="$(cat "$DEBOUNCE_FILE" 2>/dev/null || echo 0)"
case "$LAST" in *[!0-9]* | "") LAST=0 ;; esac
[ "$(( NOW - LAST ))" -lt 60 ] && exit 0

( GN_REPO_ROOT="$REPO_ROOT" bash "$HELPER" "post-edit ($(basename "$FILE"))" ) </dev/null >/dev/null 2>&1 &
disown 2>/dev/null || true
exit 0
