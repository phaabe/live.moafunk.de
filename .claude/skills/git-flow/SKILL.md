---
name: git-flow
description: Use whenever a task involves git operations — commits, branches, rebasing, PR descriptions, conflict resolution, history rewriting. Provides Conventional Commits rules, branch strategy, conflict-resolution patterns, and PR description templates.
---

# Git Flow Skill

Authoritative reference for git operations in this environment.

## Conventional Commits

Format: `<type>(<scope>)?: <subject>`

| type | use for |
|------|---------|
| feat | new user-facing capability |
| fix | bug fix |
| refactor | behaviour-preserving restructure |
| perf | measurable performance improvement |
| test | tests only |
| docs | docs only |
| style | formatting / whitespace only |
| chore | tooling / housekeeping |
| ci | CI config |
| build | build system / external deps |
| revert | revert of previous commit |

Subject ≤ 72 chars, imperative ("add", not "added" / "adds"), lowercase first letter, no trailing period. Body is optional, hard-wrap 72 cols, separated from subject by blank line. `BREAKING CHANGE:` footer only if a public API broke.

## Branch naming

`<type>/<kebab-case-summary>` — same `<type>` palette as commits. Cap at 50 chars.

Examples: `feat/user-avatar-upload`, `fix/null-pointer-in-cache`, `chore/bump-deps-2026-q2`.

## Conflict resolution

1. **Read both sides before deciding.** Don't accept-theirs / accept-ours blind.
2. Identify the *intent* of each side. If unclear, look at the commit that introduced each (`git log -p -1 <sha>`).
3. **Hand-merge logic conflicts.** Auto-merge tools handle whitespace, not semantics.
4. After resolving every file, run the test suite **before** `git rebase --continue`.
5. If a rebase has > 5 conflicting commits, consider `git rebase --interactive` to squash first, then resolve once.

## Force-push policy

- `git push --force` on `main` / `master`: **never**.
- `git push --force-with-lease` on personal feature branches: **OK** after rebase.
- On a shared feature branch: coordinate first; prefer merge over rebase.

## PR description template

```markdown
## What
<2–4 bullets>

## Why
<motivation, ticket / issue ref>

## How
<implementation notes if non-obvious>

## Test plan
- [ ] step 1
- [ ] step 2

## Risk
<low / medium / high — what could go wrong, what's the rollback>
```

## Recovery cheatsheet

| problem | command |
|---------|---------|
| Just committed wrong message | `git commit --amend` |
| Need to undo last commit but keep changes | `git reset --soft HEAD~1` |
| Need to undo last commit AND changes | `git reset --hard HEAD~1` (destructive) |
| Lost work after `reset --hard` | `git reflog` to find the SHA, `git reset --hard <sha>` |
| Branch broken, want pristine trunk | `git fetch origin && git switch -C <new> origin/<trunk>` |
