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
| `merge` | Confirm the head is still `sha` and the latest Codex verdict is `APPROVED` for it. Confirm the PR only changes files in Claude's lane. Then `gh pr merge <pr> --repo phaabe/live.moafunk.de --squash --match-head-commit <sha>`. Record the merge commit and tests on the linked issue (rules section 6). If the branch still exists, delete it with `git -C <runner dir>/<branch> push origin --delete <branch>`. Clean up the worktree. |
| `fix` | Read every URL in `comments` and the verdict. In the runner worktree, fix each finding with a regression test. If you disagree with a finding, reply to it with reasons instead of changing code. Push. Post one comment: findings addressed, the new head SHA. If you disagree with every finding and push nothing, that comment's first line is exactly `Reply-only fix by Claude at <sha>`; the runner checks for it. |
| `fix-checks` | Open the failing check logs (`gh pr checks`, `gh run view --log-failed`). A failed `epic-guard` status has its reason in the description `gh pr checks` shows (lane, PR body lines, base). Fix the cause, not the check. Push. |
| `resolve-conflict` | `W` is the runner worktree, `B` the PR branch, `S` the PR head (`sha`), `T` the `tip` of the appended attempt pin. Run `git -C W fetch origin`, then `git -C W rebase origin/<base>` (the PR's base). The gate approves it only while `origin/<base>` is `T`; if the base moved on, stop and report `blocked`. On conflicts: fix the files, `git -C W add -- <paths>` (these paths become the record's conflicted files), `git -C W rebase --continue`; or `git -C W rebase --abort`. Never `--skip`. Commit every fix before the proof. Then run the appended `Proof command` exactly (the runner's own `rebase_policy.py prove` from its code root, the pinned runtime or the runner checkout; never the worktree's copy). It runs the suites the PR's paths require and writes the proof; for the frontend suite run `npm ci` in `W/frontend` first. Fix failures, commit, prove again. Publish with exactly `git -C W push --force-with-lease=refs/heads/B:S origin HEAD:refs/heads/B`: the lease is always `S`, never `T`. The gate refuses it without a valid proof for the current `HEAD` or with any uncommitted or untracked change. If the push is refused because the remote moved, stop: never change `S`. Never `git merge`. Do not post a rebase record: the runner posts it after your session. |
| `review` | Review Codex's PR at exactly `sha` in a detached checkout. Read the appended review scope first (none appended: full review). `full`: review the whole PR. `focused` (Codex rebased a head you already reviewed, with a valid rebase record): review exactly (1) `range_diff`, the old and new patch series; (2) `base_diff`, the base changes from the old series base to the target tip that touch the PR's files or its symbols, and check `base_changed_files` for anything else the PR calls or is called by; (3) `conflicted_files`; (4) every URL in `open_findings`: each finding still open stays a finding. If the impact of a base change is unclear, do a full review. Run its tests and probe edge cases. Post each finding as its own comment (`--body-file` for long ones). Then post the verdict alone, the whole body exactly `Review: APPROVED by Claude at <sha>` or `Review: CHANGES REQUESTED by Claude at <sha>`. Re-check the head right before posting; if it moved, stop. |
| `continue` | Resume the claimed leaf or draft PR (see `issue` / `pr`) in the runner worktree. When the work and tests are done, mark the PR ready (`gh pr ready`). If all claimed leaves of the issue have evidence, update the project Status (Done if every leaf is done, else back to Backlog). |
| `claim` | Read the issue and its readiness comment. Pick only leaves marked Ready there. Check file ownership (rules section 2). Comment the claim (leaf IDs, files, branch), set Status to In progress. Work in the runner worktree: the runner already created its branch `feat/<issue>-<slug>` from `origin/dev/312-interim`. Run GitNexus impact before editing code. Open a **draft** PR early. |
| `escalate` | Add label `needs-anton` to the PR. Comment a short summary of the disagreement and the open question for Anton. Stop. |
| `adopt` | The PR has no owner line and its files route to you (`lane` is given). Confirm the head is still `sha` and the body still has no `Executor:`, `Author:` or `Reviewer:` line; if not, stop. Find the issue it implements and its leaf IDs (or `setup`). Write a new body file: the lines `Epic: https://github.com/phaabe/live.moafunk.de/issues/312`, `Executor: Claude`, `Lane: <lane>`, `Reviewer: Codex`, `Leaf IDs: …`, `Issue: <full issue URL>`, one each at line start (move an existing `Epic:` or `Issue:` line there instead of adding a second one), then a blank line, then the rest of the original body unchanged. Write this file directly in the PR body directory named below the action (`$EPIC_BODY_DIR`); the gate accepts no other file. Apply it with exactly `gh api --method PATCH repos/phaabe/live.moafunk.de/pulls/<pr> -F body=@<absolute-file-path>` (the only body edit the runner allows). Comment on the PR that Claude adopted it. Change nothing else. |

## 3. Runner worktree

- For `claim`, `continue`, `fix`, `fix-checks` and `resolve-conflict`, the runner prepares the branch's checkout before you start and appends its path as `Runner worktree`. It sits under the runner's fixed directory, `live.moafunk.de-claude-wt/<branch>` next to the runner checkout. `cd` there first. Edit, test, commit and push only there. The runner checkout holds no feature work.
- Never use `--force` or `--ignore-other-worktrees`. Never switch, reset, stash or remove another checkout.
- Git writes go through the runner's permission gate. Use only these forms, with the absolute worktree path `W` and its branch `B`: `git -C W add -- <paths>`, `git -C W commit --file <message-file>`, `git -C W fetch [-q] origin`, `git -C W push [-u] [-q] origin B`, and for `resolve-conflict` the rebase and lease push above. Reads via `git -C W` allow only `status`, `log`, `diff`, `show`, `rev-parse`, `ls-files`, `merge-base` with simple flags (`--oneline`, `-n <N>`, `--stat`, `--name-only`, ...). Plain `git push` and `git rebase`, `git -c`, other global options (`--no-pager`) and chained commands (`cd W && git push`) are refused.
- A branch checked out elsewhere is a manual handoff: the runner stops before your session and logs `handoff needed: <branch> in <path>`. By hand (`/epic-tick` without the runner), create the worktree under the same directory with plain `git worktree add <dir>/<branch> <branch>`. If Git refuses because the branch is checked out elsewhere, stop and report that line.

## 4. Every PR you open

- Base `dev/312-interim` (rules section 0). Draft until ready for review.
- Body: follow `.github/PULL_REQUEST_TEMPLATE.md`. One line each at line start: `Epic: https://github.com/phaabe/live.moafunk.de/issues/312`, `Executor: Claude`, `Lane: <exactly one lane name from .github/epic-lanes.yml, e.g. setup>`, `Reviewer: Codex`, `Leaf IDs: …`, `Issue: <the issue this PR implements>`. Full URLs only, details in a collapsed block.

## 5. Never

- Write a verdict in Codex's name, edit a verdict, or merge without Codex's approval for the current head.
- Touch `main`, release PRs, production, secrets, the plan, or lane assignments. Those need Anton: escalate.
- Do a second action in the same tick.

End with one line: `tick: <action> <target> -> <result>`.

## 6. Result (headless runner)

The runner asks for a structured result at the end. Set `status` from what really happened, with a one-line `summary`:

- `completed`: you did the action, or found nothing left to do.
- `blocked`: the action cannot succeed until something changes that you cannot change in this tick: a command the permission gate refused, a missing permission, a decision only Anton or Codex can make. Name the blocker in `summary`. The runner then skips this action on this target until the PR head (or, for `resolve-conflict`, the base) changes or the cooldown ends. Comments do not end it.
- `quota`: a GitHub API rate limit stopped you. The runner sets no cooldown.

Do not report `blocked` for a failing test or a conflict you can still fix: fix it.

## 7. Tests and time

The runner stops the session after `EPIC_TICK_TIMEOUT_SECONDS` (default 30 minutes). Work that is not pushed is lost, and the next tick starts again from zero.

- Run the tests for the files you changed first. Run each full suite (`python3 scripts/epic/run_tests.py <suite> -j 10`) in the foreground, when your change is done. Do not run a full suite again on the same commit, except the one rerun after a flaky test (below); after a fix commit (or anything that makes a rebase proof invalid), run it again.
- Never `sleep` to wait for a background test run.
- A test fails that your change does not touch: rerun only that test, at most twice. Fails every time: run it on `origin/<base>` once to know whether the base has it too, then stop debugging it in this session. Passes now: it is flaky; run the full suite once more on the same commit. Only a green full run counts; name the flaky test in your PR comment.
- A red required suite still blocks, also when the base fails too or the test is flaky (rules section 3): do not mark the PR ready, do not approve it, and comment the failure. It goes on only after a fix, or a ticket that Anton approved.
- Commit and push as soon as a step works, and well before the timeout.
