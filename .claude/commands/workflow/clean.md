---
description: Clean working tree — drop untracked junk, prune stale branches, vacuum caches.
allowed-tools: Bash(git:*), Bash(rm:*), Bash(find:*), Bash(npm:*), Bash(pnpm:*), Bash(yarn:*), Bash(docker:*), Read
model: sonnet
---

# /clean

Interactive cleanup. **Confirm before each destructive step.**

## Workflow

1. `git status --short` — list untracked files. Show the user; ask which to delete vs. add to `.gitignore`.
2. `git branch --merged <trunk>` — list local branches already merged. Offer to delete.
3. `git remote prune origin --dry-run` then real run.
4. Optional: clean dependency caches (only if asked):
   - `rm -rf node_modules && pnpm install`
   - `pip cache purge`
   - `cargo clean`
   - `docker system prune` (gated behind explicit confirmation; user-level command).
5. Show disk space recovered (`du -sh` before/after).

Never delete anything the user has not explicitly approved.
