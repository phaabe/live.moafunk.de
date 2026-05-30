---
description: Stage relevant files and create a Conventional Commit from the current diff.
argument-hint: [optional extra context for the commit message]
allowed-tools: Bash(git status:*), Bash(git diff:*), Bash(git log:*), Bash(git add:*), Bash(git commit:*), Bash(git switch:*), Bash(git rev-parse:*), Bash(.claude/hooks/scripts/gitnexus-ensure-fresh.sh), mcp__gitnexus__detect_changes, Read
model: opus
---

# /commit

Create a Conventional Commit for the current changes.

## Pre-flight: GitNexus freshness gate (do this first, every time)

1. Run the **synchronous freshness gate** so the index provably matches the code before any impact/scope check:
   ```sh
   bash "$CLAUDE_PROJECT_DIR/.claude/hooks/scripts/gitnexus-ensure-fresh.sh"
   ```
   It blocks only when the index is stale (then reindexes in the foreground). If it exits non-zero, warn the user the index may be stale and let them decide whether to continue.
2. Then run `gitnexus_detect_changes()` to confirm your changes only touch the expected symbols / execution flows (per CLAUDE.md). Surface anything unexpected.

## Pre-flight: branch protection (do this every time, after the freshness gate)

1. Check the current branch: `git rev-parse --abbrev-ref HEAD`.
2. If the current branch is `main`, `master`, `trunk`, `production`, `prod`, `release`, `develop`, or `dev`:
   - **STOP.** Do not commit on these.
   - Synthesise a branch name from the diff context (or from the user's $ARGUMENTS): `<type>/<kebab-slug>`. Use `feat/`, `fix/`, `chore/`, `docs/`, `refactor/`, `test/`, `perf/`, `ci/`, or `build/`.
   - Show the proposed branch name to the user, ask for confirmation, then run:
     ```sh
     git switch -c <type>/<slug>
     ```
   - Only after the new branch exists, continue with the commit workflow below.
3. The `branch-guard.sh` PreToolUse hook will block the commit anyway if you forget — but try not to forget.

## Workflow

1. Run `git status --short` and `git diff --stat` to see what's changed.
2. Run `git diff` (and `git diff --staged` if anything is staged) to read the actual changes.
3. Choose the right Conventional Commit type:
   - `feat` — new user-facing capability
   - `fix` — bug fix
   - `refactor` — behaviour-preserving restructure
   - `perf` — measurable performance improvement
   - `test` — tests only
   - `docs` — docs only
   - `style` — formatting only (no code change)
   - `chore` — tooling, deps, build, CI
   - `ci` — CI config
   - `build` — build system / external deps
4. Optional scope in parentheses: `feat(auth): ...` — pick from the touched directory or module.
5. Subject ≤ 72 chars, imperative mood, no trailing period.
6. Body (optional, only if non-trivial): what + why, hard-wrapped at 72 cols.
7. `BREAKING CHANGE:` footer ONLY if a public API broke.
8. If the user passed extra context as `$ARGUMENTS`, weave it into the body.
9. Stage the right files (`git add -p` mentally — don't blindly `git add .`). If unrelated changes exist, stage only the cohesive set and tell the user what's left.
10. Show the proposed commit message, ask for confirmation, then commit.

## Hard rules
- One commit = one concern.
- Never commit directly to a protected branch (see Pre-flight).
- Never amend a pushed commit on a shared branch.
- Never add `Co-authored-by: Claude` (per global setting).

Extra context from user: $ARGUMENTS
