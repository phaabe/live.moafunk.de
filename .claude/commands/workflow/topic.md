---
description: Switch topic cleanly — commit current work, optionally PR + merge to trunk, then start the new topic.
argument-hint: <new topic / next task description>
allowed-tools: Bash(git:*), Bash(gh:*), Read, Edit
model: opus
---

# /topic

Explicitly trigger the topic-switch protocol (the same one the `topic-switch-guard.sh` UserPromptSubmit hook fires automatically when it detects a switch + dirty tree).

Use this when:
- You want to be sure the protocol runs even if the heuristic missed your phrasing.
- You want to "park" current work cleanly before moving on.
- You're about to use plan mode and want everything tidy first.

## Protocol

1. **Survey** — show what's in flight:
   - `git status --short`
   - last 3 commits: `git log --oneline -3`
   - unpushed commits if any: `git log @{u}..HEAD --oneline 2>/dev/null || echo "(no upstream)"`
2. **Ask the user** (one combined question):
   - Commit current changes? (yes / no / show diff)
   - If yes, also open a PR and merge to trunk before switching? (yes / no / draft only)
3. **Execute** the chosen path:
   - **commit only**: `/git.commit`
   - **commit + draft PR**: `/git.commit` then `/git.pr` (uses `--draft`)
   - **commit + PR + merge**: `/git.commit`, `/git.pr`, then confirm and run:
     ```sh
     gh pr merge --squash --delete-branch --auto
     git switch <trunk> && git pull --ff-only
     ```
   - **skip**: acknowledge, leave tree as-is, proceed.
4. **Then** start the new topic from $ARGUMENTS. If $ARGUMENTS is empty, ask the user what the new topic is.

## Hard rules
- Never auto-commit. Never auto-push. Never auto-merge to trunk without explicit user confirmation in this turn.
- If the current branch is `main`/`master`/`trunk`/`production`: skip the PR step, just commit.
- If the user is on a personal branch with no upstream: offer to push and set upstream as part of the PR step.

New topic: $ARGUMENTS
