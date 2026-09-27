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

Status on 2026-09-28. Design agreed; parts are still pending.

| Layer | Status |
| --- | --- |
| Branch protection on `main` and `dev/streaming-architecture` | Active since 2026-09-27, but it requires an approving review that the agents cannot give. Requested: 0 required approvals on `dev/streaming-architecture`. Until then see section 0 |
| Required `epic-guard` check (reads rules and lane map from the target branch, never from the PR) | Pending: Codex's setup PR `ci/312-epic-guard` to `main` |
| Shared review and merge checker (lane map, latest counterpart verdict for the real head, green checks) | Pending: Codex's setup work |
| Claude local hook `.claude/hooks/scripts/epic-guard.sh` | Installed with this file. It accepts PR create/merge only as one plain `gh` command and checks: no verdict in Codex's name, PR base, and `--match-head-commit` with a 40-char SHA. It is a guard against mistakes, not a security boundary. It does not check lanes, the actual approval or checks yet; it will call the shared checker once that exists |
| Codex local hook | Pending: Codex's setup work; it must apply in every worktree |
| Separate agent identities | Deferred until needed. Until then the verdict author is self-declared |

Until the pending layers exist, the author checks lanes, the counterpart
verdict for the current head and green checks by hand before merging.
