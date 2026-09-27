#!/usr/bin/env bash
# PreToolUse hook: Claude's local guard for the architecture epic rules
# (docs/implementation/epic-rules.md).
#
# Blocks:
#   1. A review verdict written in Codex's name. Claude never writes it.
#   2. `gh pr create` without an explicit base, or with base `main` from a
#      branch other than `dev/streaming-architecture` (only release PRs target main).
#   3. `gh pr merge` without `--match-head-commit <sha>`.
#
# Override for an approved `main` hotfix PR (case 2 only): CLAUDE_ALLOW_MAIN_PR=1.
#
# stdin → JSON, stderr → message back to Claude, exit 2 → block, exit 0 → allow.

set -uo pipefail

INPUT="$(cat)"

block() {
  {
    echo "BLOCKED by .claude/hooks/scripts/epic-guard.sh"
    printf '%s\n' "$@"
    echo "Rules: docs/implementation/epic-rules.md"
  } >&2
  exit 2
}

# ---- 1. Verdict in Codex's name, in any tool input --------------------------
TOOL_INPUT=$(printf '%s' "$INPUT" | jq -c '.tool_input // .toolInput // {}' 2>/dev/null)
if printf '%s' "$TOOL_INPUT" | grep -E -q 'Review: (APPROVED|CHANGES REQUESTED) by Codex at [0-9a-f]{40}'; then
  block "Claude must never write a review verdict in Codex's name." \
    "Only Codex posts 'Review: ... by Codex at <sha>'. Claude posts 'by Claude' on Codex's PRs."
fi

CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // .toolInput.command // empty' 2>/dev/null)
[ -z "$CMD" ] && exit 0

is_gh_pr() {
  printf '%s' "$CMD" | grep -E -q -- "(^|[[:space:];|&])gh[[:space:]]+pr[[:space:]]+$1($|[[:space:]])"
}

# ---- 2. PR base ---------------------------------------------------------------
if is_gh_pr create; then
  BASE=$(printf '%s' "$CMD" | sed -nE 's/.*(--base|-B)[[:space:]=]+["'\'']?([^[:space:]"'\'']+).*/\2/p')
  if [ -z "$BASE" ]; then
    block "gh pr create needs an explicit --base." \
      "Epic feature PRs: --base dev/streaming-architecture. Release PRs: --head dev/streaming-architecture --base main."
  fi
  if [ "$BASE" = "main" ] && [ "${CLAUDE_ALLOW_MAIN_PR:-0}" != "1" ]; then
    HEAD=$(printf '%s' "$CMD" | sed -nE 's/.*(--head|-H)[[:space:]=]+["'\'']?([^[:space:]"'\'']+).*/\2/p')
    if [ -z "$HEAD" ]; then
      CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // .workingDirectory // empty' 2>/dev/null)
      [ -z "$CWD" ] && CWD="$(pwd)"
      HEAD=$(git -C "$CWD" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
    fi
    if [ "$HEAD" != "dev/streaming-architecture" ]; then
      block "Only release PRs from dev/streaming-architecture may target main (head was '${HEAD:-unknown}')." \
        "Target dev/streaming-architecture instead." \
        "An approved main hotfix may set CLAUDE_ALLOW_MAIN_PR=1."
    fi
  fi
fi

# ---- 3. Merge with the expected head -------------------------------------------
if is_gh_pr merge; then
  if ! printf '%s' "$CMD" | grep -E -q -- '--match-head-commit([[:space:]=]+)[0-9a-f]{40}'; then
    block "gh pr merge needs --match-head-commit <40-char head SHA>." \
      "Merge only the head the other agent approved; a mismatch aborts the merge."
  fi
fi

exit 0
