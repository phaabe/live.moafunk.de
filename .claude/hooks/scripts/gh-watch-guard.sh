#!/usr/bin/env bash
# PreToolUse hook for the Bash tool.
#
# Blocks CI watchers that burn the GitHub GraphQL budget:
#   - gh pr checks ... --watch   (GraphQL poll every 10 s)
#   - gh run watch ...           (GraphQL lookup + poll every 3 s, one run only)
#
# Use instead: python3 scripts/gh_checks/wait_checks.py <pr>  (REST, 60 s)
#
# Override: CLAUDE_ALLOW_GH_WATCH=1
#
# stdin → JSON, stderr → message back to Claude, exit 2 → block, exit 0 → allow.

set -uo pipefail

[ "${CLAUDE_ALLOW_GH_WATCH:-0}" = "1" ] && exit 0

INPUT="$(cat)"
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // .toolInput.command // empty' 2>/dev/null)
[ -z "$CMD" ] && exit 0

# Drop quoted strings (commit messages, echo text), then check each shell
# segment on its own, with `gh` in command position.
# Newlines become \036 so quotes can span lines; leftover ones separate commands.
UNQUOTED=$(printf '%s' "$CMD" | tr '\n' '\036' \
  | sed -E "s/'[^']*'//g; s/\"([^\"\\\\]|\\\\.)*\"//g" | tr '\036' ';')
LEAD='^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*gh[[:space:]]+'
BLOCK=0
while IFS= read -r SEG; do
  if printf '%s' "$SEG" | grep -E -q -- "${LEAD}run[[:space:]]+watch([[:space:]]|$)"; then
    BLOCK=1
  elif printf '%s' "$SEG" | grep -E -q -- "${LEAD}pr[[:space:]]+checks([[:space:]]|$)" \
    && printf '%s' "$SEG" | grep -E -q -- '[[:space:]]--watch([[:space:]=]|$)'; then
    BLOCK=1
  fi
done <<EOF
$(printf '%s\n' "$UNQUOTED" | awk '{ gsub(/&&|\|\||;|\|/, "\n"); print }')
EOF

[ "$BLOCK" = "0" ] && exit 0

{
  echo "BLOCKED by .claude/hooks/scripts/gh-watch-guard.sh"
  echo "\`gh pr checks --watch\` and \`gh run watch\` poll GitHub GraphQL and use up the hourly budget."
  echo ""
  echo "Use instead (REST, polls every 60 s, exits non-zero on failure):"
  echo "  python3 scripts/gh_checks/wait_checks.py <pr-number>"
  echo "Then merge with the printed SHA:"
  echo "  gh pr merge <pr-number> --squash --delete-branch --match-head-commit <sha>"
  echo ""
  echo "Override (use sparingly): CLAUDE_ALLOW_GH_WATCH=1"
} >&2
exit 2
