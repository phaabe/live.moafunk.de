#!/usr/bin/env bash
# PreToolUse hook for the Bash tool.
#
# Blocks non-ASCII characters (byte > 0x7F) in TEXT-AUTHORING commands:
#   - git commit (message / body / heredoc)      -> commit messages
#   - glab mr|issue create|update|note           -> MRs, tickets, notes
#   - gh   pr|issue create|edit|comment          -> PRs, issues, comments
# Companion to nonascii-guard.sh (which covers Write/Edit/MultiEdit = code &
# code comments). Together they keep code, comments, commits, MRs and tickets
# pure ASCII. Use plain ASCII equivalents (-> for arrows, -- for dashes,
# "..." for quotes, ... for ellipsis).
#
# Scoped on purpose: it does NOT scan every bash command, so reading or
# grepping UTF-8 data (cat/grep/rg on a file that contains non-ASCII) is fine.
#
# Override: set CLAUDE_ALLOW_NONASCII=1 in the session env.
#
# stdin -> JSON, stderr -> message back to Claude, exit 2 -> block, exit 0 -> allow.

set -uo pipefail

# Any byte above 0x7F. A byte range, not `grep -P`: the macOS grep has no -P,
# and its error (exit 2) would let every write through.
NONASCII=$'[\x80-\xff]'

# Optional global override (shared with nonascii-guard.sh)
if [ "${CLAUDE_ALLOW_NONASCII:-0}" = "1" ]; then
  exit 0
fi

INPUT="$(cat)"
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // .toolInput.command // empty' 2>/dev/null)
[ -z "$CMD" ] && exit 0

# Is this a text-authoring command? (git commit / glab MR|issue / gh PR|issue).
# Tolerate leading env vars and `git -c key=val` / global flags before the verb.
is_authoring=0
# git commit (with or without --amend); same shape as branch-guard.sh
if printf '%s' "$CMD" | grep -E -q -- '(^|[[:space:];|&(])git([[:space:]]+(-[A-Za-z]|--[A-Za-z][A-Za-z=._/-]*|-c[[:space:]][^[:space:]]+))*[[:space:]]+commit($|[[:space:]])'; then
  is_authoring=1
fi
# glab mr|issue create|update|note
if printf '%s' "$CMD" | grep -E -q -- '(^|[[:space:];|&(])glab[[:space:]]+(mr|issue)[[:space:]]+(create|update|note)($|[[:space:]])'; then
  is_authoring=1
fi
# gh pr|issue create|edit|comment
if printf '%s' "$CMD" | grep -E -q -- '(^|[[:space:];|&(])gh[[:space:]]+(pr|issue)[[:space:]]+(create|edit|comment)($|[[:space:]])'; then
  is_authoring=1
fi

[ "$is_authoring" -eq 0 ] && exit 0

# Any byte outside the 7-bit ASCII range? (tab/newline are ASCII, so allowed.)
if ! printf '%s' "$CMD" | LC_ALL=C grep -q "$NONASCII"; then
  exit 0
fi

# Offending lines (line number + content), capped so the message stays short.
OFFENDING=$(printf '%s' "$CMD" | LC_ALL=C grep -n "$NONASCII" | head -20)

{
  echo "BLOCKED by .claude/hooks/scripts/nonascii-bash-guard.sh"
  echo "This commit / MR / ticket / comment command contains non-ASCII characters."
  echo "Keep authored text (commit messages, MR & issue bodies, notes) pure ASCII."
  echo ""
  echo "Offending lines:"
  printf '%s\n' "$OFFENDING"
  echo ""
  echo "Replace with plain ASCII, e.g.:"
  echo "  arrows ->  ->   |  dashes -- --   |  smart quotes -> \" '   |  ellipsis -> ..."
  echo ""
  echo "Override (use sparingly): set env CLAUDE_ALLOW_NONASCII=1 in the session env"
} >&2

exit 2
