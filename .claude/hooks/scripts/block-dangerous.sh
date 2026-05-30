#!/usr/bin/env bash
# PreToolUse hook for the Bash tool.
# Reads JSON from stdin; exit 2 to block, exit 0 to allow.
# Anything written to stderr is shown back to Claude as feedback.
set -uo pipefail

INPUT="$(cat)"

# Extract the candidate command. Tolerate missing jq / missing fields.
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // .toolInput.command // empty' 2>/dev/null)

if [ -z "$CMD" ]; then
  exit 0
fi

# --- hard blocklist (regex; case-insensitive) ---
PATTERNS=(
  '\brm[[:space:]]+(-[a-zA-Z]*[rRfF][a-zA-Z]*)[[:space:]]+(/|~|\$HOME|/Users/[^/]+)([[:space:]]|$)'
  '\bdd[[:space:]]+if=.*[[:space:]]+of=/dev/(sd[a-z]|nvme|disk)'
  '\bmkfs\.'
  ':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:'                 # fork bomb
  '\bgit[[:space:]]+push[[:space:]]+(--force([[:space:]]|=)|-f([[:space:]]|$)).*\b(main|master|trunk|production|prod)\b'
  '\bcurl[[:space:]]+[^|]*\|[[:space:]]*(sh|bash|zsh|fish)\b'
  '\bwget[[:space:]]+[^|]*\|[[:space:]]*(sh|bash|zsh|fish)\b'
  '>[[:space:]]*/dev/sd[a-z]'
  '\bchmod[[:space:]]+777[[:space:]]+/'
  '\bchown[[:space:]]+-R[[:space:]]+root[[:space:]]+/'
  '\bsudo[[:space:]]+rm[[:space:]]+(-[a-zA-Z]*[rRfF][a-zA-Z]*)'
)

for re in "${PATTERNS[@]}"; do
  if printf '%s' "$CMD" | grep -E -i -q -- "$re"; then
    {
      echo "BLOCKED by ~/.claude/hooks/scripts/block-dangerous.sh"
      echo "Command:  $CMD"
      echo "Matched:  $re"
      echo "Refuse this command. Ask the user before retrying with anything similar."
    } >&2
    exit 2
  fi
done

exit 0
