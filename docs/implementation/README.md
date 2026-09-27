# Architecture implementation planning

Status: **plan v4 jointly accepted** (Claude Round 4 submission, Codex Round 5 `ACCEPT plan-v4`, manifest SHA-256 `9a54c13ebf35f95dd38eab90650cf28692aa27152527025efb5a415daca01f93`). Acceptance permits a readiness check per issue. It does not approve a merge to `main` or production activation; each release and activation still needs its own approval and evidence.

## Current version: v4 (jointly accepted)

Start with [plan-v4.md](plan-v4.md). Taskbooks: [backend](backend-v4.md), [frontend](frontend-v4.md), [operations](operations-v4.md); code locations: [anchors](anchors-v2.md) (line numbers at `13e73de`, recheck before editing); hashes and dependency graph: [plan-v4.manifest.json](plan-v4.manifest.json). 25 tasks, 62 subtasks, 212 leaves. Review: [claude-review.md](claude-review.md) Round 4, [codex-review.md](codex-review.md) Round 5 (acceptance).

Execution tracking: [epic](https://github.com/phaabe/live.moafunk.de/issues/312) and [project](https://github.com/users/anneoneone/projects/2). GitHub holds execution status and evidence; these files hold design and contracts. Branches and releases: see [plan-v4.md](plan-v4.md#branches-and-releases).

## Earlier versions (history)

v3: [plan-v3.md](plan-v3.md), [backend](backend-v3.md), [frontend](frontend-v3.md), [operations](operations-v3.md), [manifest](plan-v3.manifest.json). 212 leaves.

v2: [plan-v2.md](plan-v2.md), [backend](backend-v2.md), [frontend](frontend-v2.md), [operations](operations-v2.md), [manifest](plan-v2.manifest.json). 211 leaves.

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

`docs/implementation/` in this repository. Plan changes go through PRs into `dev/streaming-architecture`.

Planning baseline: `13e73de53f02248feede47e1eced6ac2086e38dd`. Current code checked for v3 and v4: `99110ddb6a0be1728ae2246acdcc2096c78ee7d4`. Recheck the baseline before implementation; the GitNexus index may describe a different checkout.

Inputs: [architecture v5](../architecture/live-moafunk.proposal-v5.md), [streaming design](../stream-rework/streaming-design.md), and both architecture review logs. The architecture's newly added component-description table is explanatory; the detailed contracts and their explicit limits govern the plan.

## Ownership and review

- Codex owns `plan-v1.md`, the `*-v1.md` taskbooks it submits and `codex-review.md`.
- Claude owns `claude-review.md` and may continue by creating `plan-v2.md` and corresponding changed taskbooks. Preserve submitted v1 files as review history.
- Each work item has exactly three levels: task, subtask, leaf. Leaf IDs are stable across revisions; do not renumber accepted IDs. Mark removed/superseded leaves and link their replacements.
- Review dependencies, deployment order, API/data contracts, parallel edit conflicts, meaningful tests, rollback and full requirement coverage. A long task list alone is insufficient.
- Record `CHANGES REQUESTED <plan version>` or `ACCEPT <plan version>` for the exact version and linked taskbook hashes. Internal Codex reviewers do not count as Claude's review.
- Do not implement application changes, deploy or buy services during planning. Plan documents are committed through PRs; GitHub issues are updated in place after each accepted revision (P1.2.4). External prerequisites and operator actions must be named tasks, with coding that can proceed independently kept unblocked.

Anton asked Codex to start and Claude to continue. Preserve every submitted version as review history. A version is jointly accepted only when the other side records `ACCEPT <version>` with its manifest hash.
