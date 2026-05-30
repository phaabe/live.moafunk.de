---
description: Create and switch to a new branch using a Conventional-Commit-style name derived from a description.
argument-hint: <short description of the work>
allowed-tools: Bash(git status:*), Bash(git branch:*), Bash(git switch:*), Bash(git checkout:*), Bash(git pull:*), Bash(git fetch:*)
model: sonnet
---

# /branch

Create and switch to a new branch.

## Workflow

1. Verify working tree is clean (`git status --short`). If not, ask whether to stash or abort.
2. Pull latest trunk: `git fetch origin && git switch <trunk>` and pull.
3. Pick a type prefix from the description ($ARGUMENTS):
   - `feat/` — new feature
   - `fix/` — bug fix
   - `chore/` — tooling / deps / housekeeping
   - `docs/` — docs only
   - `refactor/` — restructure
   - `test/` — tests only
   - `spike/` — exploration / throwaway
4. Slug the description: lowercase, kebab-case, ≤ 50 chars.
5. `git switch -c <type>/<slug>`.
6. Confirm: print `git status` and the new branch name.

Description: $ARGUMENTS
