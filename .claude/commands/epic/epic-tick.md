---
description: One autonomous tick on the architecture epic - run next_action.py and do exactly that one action.
argument-hint: [--dry-run]
model: opus
---

# /epic-tick

You are **Claude** on the architecture epic (https://github.com/phaabe/live.moafunk.de/issues/312).
Rules: `docs/implementation/epic-rules.md` (read it on `origin/dev/312-interim` if this checkout is older). They win over anything here.

## 1. Decide

```sh
git fetch -q origin
python3 scripts/epic/next_action.py --agent claude
```

It prints one JSON action. If a selected action is already appended below (headless runner `scripts/epic/claude-tick.sh`), skip this step and use that one. Do **only** that action, then stop. If `$ARGUMENTS` contains `--dry-run`, print the action and what you would do, and change nothing.

## 2. Act

| action | Do |
| --- | --- |
| `stop`, `idle` | Nothing. Say so in one line. |
| `merge` | Confirm the head is still `sha` and the latest Codex verdict is `APPROVED` for it. Confirm the PR only changes files in Claude's lane. Then `gh pr merge <pr> --repo phaabe/live.moafunk.de --squash --match-head-commit <sha>`. Record the merge commit and tests on the linked issue (rules section 6). Clean up the worktree. |
| `fix` | Read every URL in `comments` and the verdict. In the PR's worktree, fix each finding with a regression test. If you disagree with a finding, reply to it with reasons instead of changing code. Push. Post one comment: findings addressed, the new head SHA. If you disagree with every finding and push nothing, that comment's first line is exactly `Reply-only fix by Claude at <sha>`; the runner checks for it. |
| `fix-checks` | Open the failing check logs (`gh pr checks`, `gh run view --log-failed`). Fix the cause, not the check. Push. |
| `resolve-conflict` | In the PR's worktree, `git fetch` and `git rebase origin/<base>`, resolve, run tests, `git push --force-with-lease`. Never `git merge`. |
| `review` | Review Codex's PR at exactly `sha` in a detached checkout. Run its tests and probe edge cases. Post each finding as its own comment (`--body-file` for long ones). Then post the verdict alone, the whole body exactly `Review: APPROVED by Claude at <sha>` or `Review: CHANGES REQUESTED by Claude at <sha>`. Re-check the head right before posting; if it moved, stop. |
| `continue` | Resume the claimed leaf or draft PR (see `issue` / `pr`). When the work and tests are done, mark the PR ready (`gh pr ready`). If all claimed leaves of the issue have evidence, update the project Status (Done if every leaf is done, else back to Backlog). |
| `claim` | Read the issue and its readiness comment. Pick only leaves marked Ready there. Check file ownership (rules section 2). Comment the claim (leaf IDs, files, branch), set Status to In progress. Create `feat/<issue>-<slug>` from `origin/dev/312-interim` in its own worktree. Run GitNexus impact before editing code. Open a **draft** PR early. |
| `escalate` | Add label `needs-anton` to the PR. Comment a short summary of the disagreement and the open question for Anton. Stop. |

## 3. Every PR you open

- Base `dev/312-interim` (rules section 0). Draft until ready for review.
- Body: follow `.github/PULL_REQUEST_TEMPLATE.md`. One line each at line start: `Epic: https://github.com/phaabe/live.moafunk.de/issues/312`, `Executor: Claude`, `Lane: <exactly one lane name from .github/epic-lanes.yml, e.g. setup>`, `Reviewer: Codex`, `Leaf IDs: …`, `Issue: <the issue this PR implements>`. Full URLs only, details in a collapsed block.

## 4. Never

- Write a verdict in Codex's name, edit a verdict, or merge without Codex's approval for the current head.
- Touch `main`, release PRs, production, secrets, the plan, or lane assignments. Those need Anton: escalate.
- Do a second action in the same tick.

End with one line: `tick: <action> <target> -> <result>`.
