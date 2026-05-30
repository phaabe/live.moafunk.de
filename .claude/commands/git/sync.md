---
description: Sync the current branch with the upstream trunk (rebase, fall back to merge if rebase fails).
allowed-tools: Bash(git:*)
model: sonnet
---

# /sync

Bring the current branch up to date with the trunk.

## Workflow

1. Save state: `git status --short`. If working tree dirty → ask whether to stash.
2. Determine trunk: `git rev-parse --abbrev-ref origin/HEAD`.
3. Fetch: `git fetch --prune origin`.
4. Rebase: `git rebase origin/<trunk>`.
5. On conflict:
   - Stop, list conflicting files.
   - Walk through them with the user one at a time. Read both sides before resolving.
   - Continue: `git rebase --continue`.
   - Abort if the user wants to back out: `git rebase --abort`.
6. After successful rebase, force-push **only if** the branch is your personal feature branch and not shared (use `--force-with-lease`, never `--force` to main/master).
7. Run the project's smoke test (e.g. `npm test`, `pytest`, `cargo test`) if cheap, to catch broken merges.
