# Architecture implementation planning

Status: Codex submitted v1. Claude continues with an independent review. This is planning only, not authorization to implement or deploy. Joint acceptance is pending.

## Current version: v2 (Claude, awaiting Codex review)

Start with [plan-v2.md](plan-v2.md). Taskbooks: [backend](backend-v2.md), [frontend](frontend-v2.md), [operations](operations-v2.md); code locations: [anchors](anchors-v2.md); hashes and dependency graph: [plan-v2.manifest.json](plan-v2.manifest.json). 25 tasks, 62 subtasks, 211 leaves. Review: [claude-review.md](claude-review.md) Round 2.

## Read the submitted plan (v1, kept as history)

Start with [plan-v1.md](plan-v1.md): shared contracts, coding dependencies, parallel ownership, staged rollout and requirement coverage.

| Document | Tasks | Subtasks | Concrete steps |
| --- | --- | --- | --- |
| [Coordinator and release](plan-v1.md) | 5 | 12 | 35 |
| [Backend](backend-v1.md) | 6 | 16 | 67 |
| [Frontend and devices](frontend-v1.md) | 7 | 14 | 35 |
| [Operations and recovery](operations-v1.md) | 7 | 20 | 61 |
| Total | 25 | 62 | 198 |

[Submission manifest](plan-v1.manifest.json) records exact hashes and the checked coding-dependency graph. [Codex review](codex-review.md) contains the handoff; [Claude review](claude-review.md) contains Claude's response. Every concrete step includes verification; parent tasks define ownership, dependencies, PR scope and rollback.

## Shared location

`/Users/anton/git/2_jobs/live.moafunk.de/.claude/worktrees/docs-architecture-diagrams/docs/implementation/`

Code baseline: `13e73de53f02248feede47e1eced6ac2086e38dd` on `worktree-docs-architecture-diagrams`. Recheck the baseline before implementation; the GitNexus index may describe a different checkout.

Inputs: [architecture v5](../architecture/live-moafunk.proposal-v5.md), [streaming design](../stream-rework/streaming-design.md), and both architecture review logs. The architecture's newly added component-description table is explanatory; the detailed contracts and their explicit limits govern the plan.

## Ownership and review

- Codex owns `plan-v1.md`, the `*-v1.md` taskbooks it submits and `codex-review.md`.
- Claude owns `claude-review.md` and may continue by creating `plan-v2.md` and corresponding changed taskbooks. Preserve submitted v1 files as review history.
- Each work item has exactly three levels: task, subtask, leaf. Leaf IDs are stable across revisions; do not renumber accepted IDs. Mark removed/superseded leaves and link their replacements.
- Review dependencies, deployment order, API/data contracts, parallel edit conflicts, meaningful tests, rollback and full requirement coverage. A long task list alone is insufficient.
- Record `CHANGES REQUESTED <plan version>` or `ACCEPT <plan version>` for the exact version and linked taskbook hashes. Internal Codex reviewers do not count as Claude's review.
- Do not implement application changes, publish issues, commit, deploy or buy services during planning. External prerequisites and operator actions must be named tasks, with coding that can proceed independently kept unblocked.

Anton asked Codex to start and Claude to continue. The first submission is complete. Preserve its files while reviewing or writing v2; it is not joint approval until Claude reviews it.
