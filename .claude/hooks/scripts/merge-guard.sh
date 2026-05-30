#!/usr/bin/env bash
# PreToolUse hook for the Bash tool.
#
# Enforces "merging is only allowed via pull request":
# blocks `git merge <ref>` (and the `git pull <remote> <ref>` form, which
# is also a merge of arbitrary refs).
#
# Allowed:
#   - git merge --abort | --continue | --quit | --skip   (recovery)
#   - git pull                                           (sync your own branch)
#   - git pull --rebase  /  git pull --ff-only           (no merge commit)
#   - gh pr merge ...                                    (the sanctioned path)
#
# Override: CLAUDE_ALLOW_DIRECT_MERGE=1
#
# stdin → JSON, stderr → message back to Claude, exit 2 → block, exit 0 → allow.

set -uo pipefail

[ "${CLAUDE_ALLOW_DIRECT_MERGE:-0}" = "1" ] && exit 0

INPUT="$(cat)"
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // .toolInput.command // empty' 2>/dev/null)
[ -z "$CMD" ] && exit 0

# ---- Helper: extract just the meaningful tokens after `git ...` -----------
# Strip leading env assignments and `git -c key=val` style options to find
# the subcommand and its args.
strip_prefix() {
  local s="$1"
  # Drop leading env-var assignments (FOO=bar BAZ=qux git ...)
  s=$(printf '%s' "$s" | sed -E 's/^([A-Z_][A-Z0-9_]*=[^[:space:]]+[[:space:]]+)+//')
  printf '%s' "$s"
}

# ---- Match `git merge <ref>` ----------------------------------------------
# After stripping prefixes, the command should start with "git" and have
# a "merge" subcommand somewhere in the arg list (after possible `-c k=v`).
is_git_subcommand() {
  local cmd="$1" sub="$2"
  printf '%s' "$cmd" \
    | grep -E -q -- "(^|[[:space:];|&])git([[:space:]]+(-[A-Za-z]|--[A-Za-z][A-Za-z=._/-]*|-c[[:space:]][^[:space:]]+))*[[:space:]]+${sub}($|[[:space:]])"
}

CMD_STRIPPED=$(strip_prefix "$CMD")

# Case 1: `git merge ...`
if is_git_subcommand "$CMD_STRIPPED" "merge"; then
  # Allow recovery flags
  if printf '%s' "$CMD_STRIPPED" | grep -E -q -- 'git[[:space:]]+(-[A-Za-z]+[[:space:]]+|-c[[:space:]][^[:space:]]+[[:space:]]+|--[A-Za-z=._/-]+[[:space:]]+)*merge[[:space:]]+(--abort|--continue|--quit|--skip)([[:space:]]|$)'; then
    exit 0
  fi
  {
    echo "BLOCKED by ~/.claude/hooks/scripts/merge-guard.sh"
    echo "Refusing local \`git merge\`. Merging is only allowed via pull request."
    echo ""
    echo "Use one of:"
    echo "  /git.pr                              # open a draft PR for the current branch"
    echo "  gh pr create --fill --draft          # alternative one-liner"
    echo "  gh pr merge <num> --squash --delete-branch     # merge an existing PR"
    echo "  gh pr merge --auto --squash --delete-branch    # enable auto-merge once checks pass"
    echo ""
    echo "Allowed merge subcommands: --abort, --continue, --quit, --skip (recovery only)."
    echo "Override (use sparingly): CLAUDE_ALLOW_DIRECT_MERGE=1 in ~/.claude/settings.json env."
  } >&2
  exit 2
fi

# Case 2: `git pull <remote> <ref>` form pulls a different branch into the
# current one — that's a merge of arbitrary refs and should go via PR too.
# Plain `git pull` (no positional args) and `git pull --rebase` / `--ff-only`
# are fine.
if is_git_subcommand "$CMD_STRIPPED" "pull"; then
  # Extract everything after the literal "pull" token
  PULL_ARGS=$(printf '%s' "$CMD_STRIPPED" | sed -E 's/.*[[:space:]]pull([[:space:]]+|$)//')
  # Strip flag tokens (-x, --xxx, --xxx=yyy) — what remains is positional
  POSITIONAL=$(printf '%s' "$PULL_ARGS" | tr ' ' '\n' | grep -v '^-' | grep -v '^$' || true)
  POS_COUNT=$(printf '%s' "$POSITIONAL" | grep -c . || true)

  # Allow if no positional args, or if --rebase / --ff-only is present
  if printf '%s' "$PULL_ARGS" | grep -E -q -- '(^|[[:space:]])(--rebase|--ff-only)([[:space:]=]|$)'; then
    exit 0
  fi
  if [ "$POS_COUNT" -lt 2 ]; then
    # 0 or 1 positional → standard upstream pull, allow
    exit 0
  fi

  # 2+ positional args → `git pull <remote> <ref>` merging arbitrary ref
  {
    echo "BLOCKED by ~/.claude/hooks/scripts/merge-guard.sh"
    echo "Refusing \`git pull <remote> <ref>\` — that's a local merge of an arbitrary branch."
    echo ""
    echo "Options:"
    echo "  git fetch <remote> <ref>                    # fetch only, no merge"
    echo "  git pull --rebase <remote> <ref>            # rebase onto, no merge commit"
    echo "  git pull --ff-only <remote> <ref>           # only allow if fast-forward"
    echo "  gh pr merge <num> --squash --delete-branch  # the sanctioned integration path"
    echo ""
    echo "Override (use sparingly): CLAUDE_ALLOW_DIRECT_MERGE=1 in ~/.claude/settings.json env."
  } >&2
  exit 2
fi

exit 0
