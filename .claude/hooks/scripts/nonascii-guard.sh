#!/usr/bin/env bash
# PreToolUse hook for the Write / Edit / MultiEdit tools.
#
# Blocks writing any non-ASCII character (byte > 0x7F) into a file: source,
# tests, docs, config. Keeps everything pure ASCII so fancy dashes, smart
# quotes, arrows, non-breaking spaces, emoji, etc. never slip into the repo.
# Use plain ASCII equivalents instead (-> for arrows, -- for dashes, "..." for
# quotes, ... for ellipsis).
#
# Override: set CLAUDE_ALLOW_NONASCII=1 in the session env to permit
# non-ASCII writes (e.g. when a file genuinely needs UTF-8 content).
#
# stdin -> JSON, stderr -> message back to Claude, exit 2 -> block, exit 0 -> allow.

set -uo pipefail

# Any byte above 0x7F. A byte range, not `grep -P`: the macOS grep has no -P,
# and its error (exit 2) would let every write through.
NONASCII=$'[\x80-\xff]'

# Optional global override
if [ "${CLAUDE_ALLOW_NONASCII:-0}" = "1" ]; then
  exit 0
fi

INPUT="$(cat)"

# Pull only the text this tool would introduce into the file:
#   Write     -> .tool_input.content
#   Edit      -> .tool_input.new_string
#   MultiEdit -> .tool_input.edits[].new_string
CONTENT=$(printf '%s' "$INPUT" | jq -r '
  (.tool_input // .toolInput // {}) as $i
  | [ $i.content, $i.new_string, ( ($i.edits // []) | .[].new_string ) ]
  | map(select(. != null))
  | join("\n")
' 2>/dev/null)

[ -z "$CONTENT" ] && exit 0

# Any byte outside the 7-bit ASCII range? (tab/newline are ASCII, so allowed.)
if ! printf '%s' "$CONTENT" | LC_ALL=C grep -q "$NONASCII"; then
  exit 0
fi

FILE=$(printf '%s' "$INPUT" | jq -r '(.tool_input // .toolInput // {}) | (.file_path // .filePath // empty)' 2>/dev/null)

# Offending lines (line number + content), capped so the message stays short.
OFFENDING=$(printf '%s' "$CONTENT" | LC_ALL=C grep -n "$NONASCII" | head -20)

{
  echo "BLOCKED by .claude/hooks/scripts/nonascii-guard.sh"
  echo "The content contains non-ASCII characters. Keep files pure ASCII."
  [ -n "$FILE" ] && echo "File: $FILE"
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
