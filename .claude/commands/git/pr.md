---
description: Open a draft PR for the current branch using `gh`. Generates title + body from commit log + diff. Can also handle the merge step.
argument-hint: [base branch, default: trunk]  |  --merge to also merge it once approved
allowed-tools: Bash(git:*), Bash(gh:*), Read
model: opus
---

# /pr

Open a pull request for the current branch — the only sanctioned way to integrate work into the trunk (the `merge-guard.sh` hook blocks local `git merge <ref>`).

## Workflow

1. Determine base: $ARGUMENTS if it looks like a branch name, otherwise `git rev-parse --abbrev-ref origin/HEAD` (typically `origin/main`).
2. Push the current branch with upstream tracking if not already pushed: `git push -u origin HEAD`.
3. Read commits since base: `git log --oneline base..HEAD`.
4. Read the cumulative diff: `git diff base...HEAD`.
5. Build PR title:
   - If a single Conventional Commit on the branch → use its subject as title.
   - Otherwise → synthesise a title in Conventional Commit format covering the dominant change.
6. Build PR body using this template:

   ```
   ## What
   <2–4 bullets describing the change>

   ## Why
   <motivation, ticket / issue reference>

   ## How
   <implementation notes if non-obvious>

   ## Test plan
   - [ ] <step 1>
   - [ ] <step 2>

   ## Screenshots / output
   <if UI or CLI changes>

   ## Risk
   <low / medium / high — what could go wrong, what's the rollback>
   ```

7. Open as draft:
   ```sh
   gh pr create --draft --base <base> --title "..." --body-file <tmp>
   ```
   Add `--web` if the user prefers to open in browser.
8. Show the PR URL.

## Optional: also merge

If the user passed `--merge` (or said "and merge"):

1. Mark the PR ready for review: `gh pr ready <num>`.
2. Wait / poll for required checks if any: `gh pr checks <num> --watch`.
3. Merge with squash + branch deletion: `gh pr merge <num> --squash --delete-branch`.
4. Update local trunk: `git switch <trunk> && git pull --ff-only`.

## Rules

- Always create as `--draft` first unless explicitly told to skip the draft step.
- Never `git push --force` to the trunk.
- Never run a local `git merge <ref>` to integrate — the merge-guard hook blocks it. Use `gh pr merge` instead.
