# Architecture epic rules

These rules apply to all work on the architecture epic
(https://github.com/phaabe/live.moafunk.de/issues/312). Claude and Codex follow
them in every session. This file is the only full copy; `CLAUDE.md` and
`AGENTS.md` only point here. Change it only through a reviewed PR.

Plan: [plan-v4.md](plan-v4.md) (jointly accepted). Execution status: the epic
and its project (https://github.com/users/anneoneone/projects/2).

## 0. Temporary integration branch (until branch protection is fixed)

Branch protection on `dev/streaming-architecture` requires an approving GitHub
review. Both agents use the PR author's account, so no agent PR can get one.
Until the repository admin sets required approvals to 0 on that branch:

- `dev/312-interim` replaces `dev/streaming-architecture` in these rules and in
  every issue of the epic: branch from it, target it, merge into it.
- Review and merge rules (sections 4 and 5) apply unchanged.
- No release PR to `main` from `dev/312-interim`.

When the admin has changed the setting: one PR from `dev/312-interim` into
`dev/streaming-architecture`, approved by Anton and merged with a merge commit.
Then open PRs are retargeted, `dev/312-interim` is deleted and this section is
removed in a reviewed PR.

## 1. Before you start

- Work only on leaves that are **Ready** and assigned to you (project field
  Executor). Ready applies only to the leaves named in the issue's readiness
  comment; the rest of the issue stays blocked.
- Claim the leaf on its issue: comment with the leaf IDs, the files you will
  edit and your branch, then set Status to In progress.
- Check the file owner first. One editor per shared file (see section 2). If a
  file you need belongs to the other lane, ask its owner; do not edit it.
- Recheck the code anchors against the current branch and run GitNexus impact
  before editing code.

## 2. Lanes and file ownership

| Lane | Owner | Files |
| --- | --- | --- |
| Backend integration | Claude | `backend/src/main.rs`, `backend/src/db.rs`, `backend/src/handlers/api.rs`, `backend/src/stream_bridge.rs` and backend leaves assigned to Claude |
| Ops | Codex | `.github/workflows/**`, deployment scripts, nginx, systemd and Liquidsoap configuration |
| Setup | as assigned on the epic | Rule, template, lane-map and guard files, per the epic's setup comment |

Split and shared work:

- **O1.2.5** is split when it starts: Codex owns the deployment workflow, the
  host script and the nginx rule; Claude owns the backend precheck endpoint.
  Agree the endpoint's response and refusal behavior on the issue before
  wiring the two together.
- **P1 and O1.1** use one shared production inventory, not two. Missing
  operator decisions and host observations stay visibly open in it.

The machine-readable map is `.github/epic-lanes.yml` once it exists. Until
then, this table and the assignments on the epic apply. New assignments are
recorded on the epic before anyone edits.

## 3. Branches and PRs

- Branch `<type>/<issue>-<slug>` from `dev/streaming-architecture`, in its own
  worktree.
- Every feature PR targets `dev/streaming-architecture`. Never target `main`.
  Only two kinds of PR target `main`: release PRs from
  `dev/streaming-architecture`, and the one approved `epic-guard` setup PR
  from Codex's branch `ci/312-epic-guard` (Anton's exception before the first
  release). A `main` hotfix needs Anton's approval; the operator then sets
  `CLAUDE_ALLOW_MAIN_PR=1` in the hook environment (for example
  `.claude/settings.local.json` under `env`). An inline assignment on the
  command does not work.
- Run `gh pr create` and `gh pr merge` as one plain `gh` command: no wrappers,
  shell operators, substitutions or flags before the subcommand. Write short
  options with a separate value (`-B main`, not `-Bmain`). A PR body may
  come from a file or a heredoc with a quoted delimiter (`<<'EOF'`). Do not
  create or merge PRs through `gh api` or the GitHub MCP merge tool.
- Use the PR template. Its body has one line each, at the start of the line:
  `Epic:` (the epic URL), `Executor:` (Claude or Codex), `Lane:`, `Reviewer:`
  (the other agent), `Leaf IDs:` (or `setup`) and `Issue:` (the issue this PR
  implements). Only the `Issue:` line links a PR to its work item; other issue
  links, such as dependencies, do not. Both
  agents use one GitHub account, so `Executor:` is how the loop (section 8) and
  the `epic-guard` check tell whose PR it is.
- Open the PR as a draft while working. Mark it ready (`gh pr ready`) only when
  it is ready for review; drafts are not reviewed.
- Keep a feature and its tests in the same PR. Link evidence on the issue.
- While a release is pending, merge only work that belongs to it. Later-wave
  work waits on its branch. Documentation-only changes may merge.

## 4. Review

- The other agent reviews every PR: Codex reviews Claude's PRs, Claude reviews
  Codex's PRs.
- Review the PR's actual head commit, not a local copy.
- The verdict is a standalone comment whose whole body is exactly one line:
  - `Review: APPROVED by Codex at <40-char head SHA>`
  - `Review: CHANGES REQUESTED by Codex at <40-char head SHA>`
  - (`Claude` in the same form for Codex's PRs.)
- Put findings in separate comments.
- **Never write the other agent's verdict.** Both agents share one GitHub
  account, so GitHub cannot stop this; it is a hard rule.
- Do not edit or delete a verdict. A changed verdict counts as no verdict.
- Every new push needs a new verdict for the new head SHA.

## 5. Merge

A feature PR into `dev/streaming-architecture` may be merged by its author when
all of this holds:

1. It targets the right branch and changes only files in its lane.
2. The latest verdict from the other agent is `APPROVED` for the current head
   SHA, with no later `CHANGES REQUESTED`.
3. All required checks are green for that head.

Merge with the expected head: `gh pr merge <n> --squash --match-head-commit <sha>`.
A mismatch aborts; get a new review.

Release PRs to `main` and every production action need Anton's approval and
the evidence the plan requires. A merged PR is never activation evidence.

## 6. After merge

- Record commit, tests and result on the issue. Tick a leaf only when its
  evidence is attached.
- Close an issue only when all its leaves have evidence. PRs into
  `dev/streaming-architecture` do not close issues automatically.

## 7. Enforcement

Status on 2026-09-28. Design agreed; parts are still pending.

| Layer | Status |
| --- | --- |
| Branch protection on `main` and `dev/streaming-architecture` | Active since 2026-09-27, but it requires an approving review that the agents cannot give. Requested: 0 required approvals on `dev/streaming-architecture`. Until then see section 0 |
| Required `epic-guard` check (reads rules and lane map from the target branch, never from the PR) | Pending: Codex's setup PR `ci/312-epic-guard` to `main` |
| Shared review and merge checker (lane map, latest counterpart verdict for the real head, green checks) | Pending: Codex's setup work |
| Claude local hook `.claude/hooks/scripts/epic-guard.sh` | Installed with this file. It accepts PR create/merge only as one plain `gh` command and checks: no verdict in Codex's name, PR base, and `--match-head-commit` with a 40-char SHA. It is a guard against mistakes, not a security boundary. It does not check lanes, the actual approval or checks yet; it will call the shared checker once that exists |
| Codex local hook `.codex/hooks/scripts/epic-guard.sh` | Merged. Active only after it is trusted with `/hooks` in each Codex worktree |
| Loop decision script `scripts/epic/next_action.py` | Read-only; picks one action per tick (section 8). Tested per priority row |
| Separate agent identities | Deferred until needed. Until then the verdict author is self-declared |

Until the pending layers exist, the author checks lanes, the counterpart
verdict for the current head and green checks by hand before merging.

## 8. Autonomous loop

Each agent works in ticks. A tick runs
`python3 scripts/epic/next_action.py --agent <claude|codex>`, does exactly the
one action it prints, and ends. All state is on GitHub, so ticks can stop and
restart at any time.

Priority, first match wins:

1. `stop`: the pause file `~/.epic-pause` exists. Anton creates or removes it.
2. `escalate`: the other agent requested changes for the current head for the
   third time on this PR. Add label `needs-anton`, summarise the open question,
   and stop working on that PR. The loop skips PRs and issues with that label.
3. `merge`: the other agent approved the current head and checks are green.
4. `fix`: the other agent requested changes for the current head.
5. `fix-checks`, then `resolve-conflict`, for the agent's own PRs. The
   `epic-guard` status reports waiting (draft, missing verdict, running
   checks) as pending, so the loop waits on it; a failed guard is a real
   break and gets `fix-checks`.
6. `review`: the other agent's ready (non-draft) PR has no verdict from this
   agent for its current head.
7. `continue`: the agent's draft PR, or its In progress issue without a PR.
8. `claim`: a Ready issue with Executor set to this agent. Not while the agent
   has work to continue or two open PRs.
9. `idle`.

Order inside one step: priority label first (`priority::high`, then
`priority::medium` or no priority label, then `priority::low`; with several,
the highest counts), then the oldest (lowest PR or issue number). A PR takes
the highest priority of its own labels and its `Issue:` tickets. `continue` is
one queue for draft PRs and In progress issues. Claims sort by priority, then
lowest wave, then number. Priority only orders work: it never skips a step and
never bypasses pause, escalation, focus, "Start after", the batch order, lanes
or the two-open-PR limit. `--status` shows each item's priority.

Focus: Anton can limit both loops to some labels by writing them into
`~/.epic-focus`, one per line (for example `project::Stream`). Then only issues
with one of these labels, and PRs whose own labels or `Issue:` ticket have one,
get actions. All other PRs are frozen: no review, fix or merge. They still count
toward the two-open-PR limit. No file or an empty file means all work. Every
PR needs an `Issue:` line naming its ticket, or it is frozen under a focus.

Claude runs ticks headless with `scripts/epic/claude-tick.sh` (or `/epic-tick`
by hand). Codex runs the same script from its own runner. A runner starts a
model session only for real work: pause, idle, stop and a repeat of the last
no-op action (`scripts/epic/tick_gate.py`: same action, no change on GitHub,
younger than 3 hours) start none. Claims follow the "Start after" lines and the
order in the epic's batch table ("Scope, in order"; "then" or an arrow starts
the next stage). A "Start after" line names leaf IDs or ticket URLs. A ticket
counts as done when a merged PR names it in its `Issue:` line. So Anton can set
a whole queue to Ready at once and let each readiness comment name the ticket
before it. Each runner checkout only runs ticks and holds no work. Every
tick starts with `git pull --ff-only` there, so merged changes to
`scripts/epic/`, `.claude/commands/epic/` or `.codex/` apply on the next tick;
a failed pull stops the tick. Before the pull, `claude-tick.sh` runs
`scripts/epic/gitnexus_noise.py`: it restores changes that sit only inside the
GitNexus block of `AGENTS.md` / `CLAUDE.md`, and stops the tick on any other
tracked change without touching it. A headless session cannot answer permission
prompts, and the project settings ask before every push and merge. So
`claude-tick.sh` hands those prompts to `scripts/epic/permission_gate.py`. It
approves only `git push [-u] origin <branch>` and `git push origin --delete
<branch>` for `feat/`, `fix/`, `chore/`, `docs/`, `test/` and `refactor/`
branches, and `gh pr merge <n> --repo phaabe/live.moafunk.de --squash
[--delete-branch] --match-head-commit <sha>`. It denies everything else and
logs each decision to `claude-permissions.log` in the state directory.
`python3 scripts/epic/next_action.py --status`
shows the queue for both agents.

GitHub GraphQL quota: both runners share one wait file,
`github-quota-wait.json` in the state directory (`scripts/epic/github_quota.py`).
When a GitHub read hits the GraphQL quota, also inside an HTTP 200 response,
the script stores the reset time (UTC, from one `rateLimit { resetAt }` query,
never from REST `rate_limit`) and exits 4; the runner ends the tick with exit 75
and starts no model. Until reset plus 60 seconds, a tick makes zero GitHub calls
and starts zero models; it logs the retry time. If the reset time is unknown,
the wait is 15 minutes. A quota wait writes no cooldown or repeat-gate record.
Runners call `github_quota.py check --state-dir <dir>` before each GitHub read
and before starting a model (exit 0 proceed, 3 deferred, 2 bad file), because
the other runner can store a wait at any time, and `record` to store a wait.
The loop never releases to `main`, touches production, or changes the plan or
lanes; those go to Anton.
