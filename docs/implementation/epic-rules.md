# Architecture epic rules

These rules apply to all work on the architecture epic
(https://github.com/phaabe/live.moafunk.de/issues/312). Claude and Codex follow
them in every session. This file is the only full copy; `CLAUDE.md` and
`AGENTS.md` only point here. Change it only through a reviewed PR.

Plan: [plan-v4.md](plan-v4.md) (jointly accepted). Execution status: the epic
and its project (https://github.com/users/anneoneone/projects/2).

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

The machine-readable map is `.github/epic-lanes.yml` once it exists. Until
then, this table and the assignments on the epic apply. New assignments are
recorded on the epic before anyone edits.

## 3. Branches and PRs

- Branch `<type>/<issue>-<slug>` from `dev/streaming-architecture`, in its own
  worktree.
- Every feature PR targets `dev/streaming-architecture`. Never target `main`;
  only release PRs from `dev/streaming-architecture` do.
- The PR body names the issue URL, the leaf IDs, the lane and the reviewer.
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

- Server side: branch protection on `main` and `dev/streaming-architecture`,
  and the required `epic-guard` check once it exists. The check reads the rules
  and lane map from the target branch, never from the PR.
- Local: Claude and Codex each run a hook around the shared checker.
- Known gap: the verdict author is self-declared until the agents have separate
  identities.
