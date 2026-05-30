#!/usr/bin/env bash
# PreToolUse hook for the Bash tool.
#
# Blocks `git commit` (and `git commit --amend`) when the current branch is
# a protected one (main/master/trunk/production/release/develop).
# Tells Claude to create a feature branch first via /git.branch.
#
# Override: set CLAUDE_ALLOW_DIRECT_COMMIT=1 in ~/.claude/settings.json env
# to allow commits on protected branches (e.g. for solo repos where main = working branch).
#
# stdin → JSON, stderr → message back to Claude, exit 2 → block, exit 0 → allow.

set -uo pipefail

# Optional global override
if [ "${CLAUDE_ALLOW_DIRECT_COMMIT:-0}" = "1" ]; then
  exit 0
fi

INPUT="$(cat)"
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // .toolInput.command // empty' 2>/dev/null)
[ -z "$CMD" ] && exit 0

# Match `git commit` (with or without args) and `git commit --amend`.
# Tolerate leading env vars (`GIT_AUTHOR_NAME=... git commit ...`).
# Match `git -c key=val commit` form too.
# Don't match unrelated tokens like `git committed`.
if ! printf '%s' "$CMD" | grep -E -q -- '(^|[[:space:];|&])git([[:space:]]+(-[A-Za-z]|--[A-Za-z][A-Za-z=._/-]*|-c[[:space:]][^[:space:]]+))*[[:space:]]+commit($|[[:space:]])'; then
  exit 0
fi

# Find the cwd (Claude Code injects this; fall back to PWD)
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // .workingDirectory // empty' 2>/dev/null)
[ -z "$CWD" ] && CWD="$(pwd)"

# Must be inside a git repo
git -C "$CWD" rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

# Prefer symbolic-ref: it returns the branch name even on an unborn branch
# (zero-commit branch right after `git switch -c …` in a fresh repo). Fall back
# to rev-parse for the genuinely detached case, where symbolic-ref exits non-zero.
BRANCH=$(git -C "$CWD" symbolic-ref --short HEAD 2>/dev/null \
         || git -C "$CWD" rev-parse --abbrev-ref HEAD 2>/dev/null)

# Detached HEAD → block (you can't even commit there cleanly)
if [ "$BRANCH" = "HEAD" ] || [ -z "$BRANCH" ]; then
  {
    echo "BLOCKED by ~/.claude/hooks/scripts/branch-guard.sh"
    echo "You're in detached HEAD state. Create a branch first:"
    echo "  git switch -c <type>/<short-description>"
    echo "Then retry the commit."
  } >&2
  exit 2
fi

# Protected branches (case-sensitive, exact match)
case "$BRANCH" in
  main|master|trunk|production|prod|release|develop|dev)
    {
      echo "BLOCKED by ~/.claude/hooks/scripts/branch-guard.sh"
      echo "Refusing to commit directly to protected branch: $BRANCH"
      echo ""
      echo "Required workflow:"
      echo "  1. Create a feature branch first:"
      echo "       git switch -c <type>/<short-description>      (e.g. feat/user-avatar)"
      echo "     (or invoke the slash command: /git.branch <description>)"
      echo "  2. Re-run the commit."
      echo "  3. Open a PR: /git.pr"
      echo ""
      echo "Override (use sparingly): set env CLAUDE_ALLOW_DIRECT_COMMIT=1 in ~/.claude/settings.json"
      echo "to permit direct commits to protected branches in this repo / globally."
    } >&2
    exit 2
    ;;
esac

exit 0
