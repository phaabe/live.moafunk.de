# Implementation plan v2 — continuous station and server architecture

Status: v2 by Claude, continuing Codex's v1. Awaiting Codex review. Planning only. No implementation or production qualification is claimed. Why each change was made: [claude-review.md](claude-review.md) Round 2.

What v2 changes, in short:

- **Wave 0 lane.** Nine defects at baseline are fixable now and do not need the media contracts; they no longer wait behind P2/O3 (see [Wave schedule](#wave-schedule)).
- **Dependencies at leaf level.** P2 no longer blocks as one unit. Only P2.2.1 needs the O3.1.1 media spike, and only P2.1.2 needs O1.1.
- **Concrete candidate mechanisms** for the host executor, Docker settlement, API-down startup barrier and Liquidsoap epoch proof, to be proven or replaced in P2 (see [Candidate mechanisms](#candidate-mechanisms-to-prove-in-p2)).
- **Baseline code anchors** for every leaf that edits existing code: [anchors-v2.md](anchors-v2.md).
- **Agent execution rules** (P1.2.4, P1.2.5): issues, branches, leaf checklist and hand-off.

New leaf IDs: P1.2.4, P1.2.5, B1.1.5, B1.1.6, B1.3.5, B3.1.5, B3.3.5, B5.2.6, O1.2.4, O1.2.5, O4.2.4, O5.2.4, O7.1.5. No v1 ID was removed or renumbered.

Baseline: `13e73de53f02248feede47e1eced6ac2086e38dd`, branch `worktree-docs-architecture-diagrams`. Architecture input: [v5](../architecture/live-moafunk.proposal-v5.md), SHA-256 `38df0550f439b96064f9e42c43a11ce036543b63db994b2c13ce75da6db4fa3b`. Streaming input: [accepted design](../stream-rework/streaming-design.md) and [decision record](../stream-rework/streaming-design-decisions.md). Source documents govern requirements; disagreements must be resolved in the review logs before implementation.

## How to execute this plan

The task hierarchy has exactly three levels: task (`B3`), subtask (`B3.2`), implementation-and-verification leaf (`B3.2.3`). Do not create a fourth level. A leaf is a work checkpoint, not a separate commit. Keep each feature and its tests together; use the PR boundaries in each taskbook. Leave IDs stable when revising the plan.

| Taskbook | Responsibility | Assigned implementation lane |
| --- | --- | --- |
| This document, P1–P2 | Baseline, shared contracts, fixtures and ownership | Coordinator with domain owners |
| [Backend B1–B6](backend-v2.md) | Admission, sessions, recording, prerecorded assets, public metadata/artwork | Backend owner and one integration editor |
| [Frontend F1–F7](frontend-v2.md) | Player, recovery, Media Session, admin consumers, physical-device evidence | Public-player owner; separate admin owner |
| [Operations O1–O7](operations-v2.md) | Deployment, media harness, HLS, resources, observer, backups | Host/media owner; separate monitoring/backup owner |
| [Baseline anchors](anchors-v2.md) | File, symbol and line for each leaf that edits existing code; entry-point inventories | Read before claiming a leaf; recheck lines against the current branch |
| This document, R1–R3 | Cross-component faults, rollout, operating handoff | Coordinator, test owner and operator |

Use the existing Rust, Vitest and shell tooling. Add a framework only if an identified test cannot be expressed safely with the current tools. Mocks test policy; real media tools test codecs; physical devices test iOS. No layer substitutes for the next.

PR checks: in `backend/`, run focused Rust tests, `cargo test`, `cargo fmt --check` and `cargo clippy --all-targets -- -D warnings`; in `frontend/`, run focused Vitest tests, `npm test -- --run`, `npm run typecheck`, `npm run lint` and `npm run build`. Validate changed Compose/nginx/Liquidsoap configurations with their actual installed tools and run affected harness tests. Record baseline failures separately. A documentation-only planning change needs structural/link checks, not application execution.

For every claimed leaf completion, record ID, owner, commit/artifact, tests and outcome in a new `execution-log.md`. Use `not started / claimed / in progress / verified / blocked` plus a concrete blocker. “Merged” is not evidence that a device or production gate passed. Keep fixtures and implementation in the same feature PR. Before code edits, refresh the actual worktree baseline, consult GitNexus and perform required impact analysis; the shared index can describe another checkout. Do not silently discard unrelated changes.

### Parallel ownership

Each implementer claims files before editing. One backend integrator serializes changes to `backend/src/{main,db,stream_bridge}.rs` and `backend/src/handlers/api.rs`. New domain modules may be developed in parallel after P2. One public-player owner integrates F1–F5 because they share `main.ts` and `player.ts`. F6 may proceed separately; one editor owns `frontend/src/admin/api/index.ts`. Operations owns workflows, deployment scripts, nginx and Liquidsoap files. Backend owners submit wiring requirements to operations rather than concurrently editing those files.

Before merging a PR, rebase onto the current integration branch, rerun affected tests and inspect the combined behavior. Use feature branches and PRs; do not merge locally. This plan does not authorize implementation, commits, service changes or external purchases during the planning exchange.

### Shared contracts to freeze in P2

Names marked proposed are implementation choices to confirm in P2, not APIs already present. Do not independently invent different versions in each lane.

| Contract | Proposed implementation and invariant | Producers → consumers |
| --- | --- | --- |
| Broadcast identity | `broadcast.rs::BroadcastIdentity`: random UUID, restart-safe runtime generation, owner, optional authorized show, source kind, actual start. Cleanup compares identity; rehearsal stays private. | B1 → B2/B3/B4/B5/O3 |
| Admission and maintenance | `maintenance.rs::AdmissionPermit`; persistent operation-ID state machine; one serialized decision covers gate, pending starts and schedule edits. Versioned machine errors distinguish maintenance conflict, insufficient reserve, asset not ready and forbidden. | B2 → B1/B3/B4, O2, admin integration R1.1 |
| Host executor | Proposed root-owned `/usr/local/libexec/moafunk-deploy`, persistent operation journal under `/var/lib/moafunk/deploy/`, lifetime lock under `/run/lock/`, and systemd-managed executor cgroup. Root recovery fences the old executor and mutation-capable descendants, reconciles actual service state, and may restore a known compatible image while API is down. API remains the DB writer; no Docker socket in API. Journal records executor progress, not a second independent admission authority. | O2 ↔ B2 |
| API-down recovery boundary | Ordinary recovery resumes an already closed persisted operation. If no closed operation can be established because API is unavailable, use an audited incident procedure: fence the API/runtime first, then start a recovery mode that holds admissions closed before any scheduler/ingest/capture/background startup. The recovered API records/reconciles the operation before explicit release. P2 must prove this bootstrap order; direct host SQL and opening on timeout are excluded. | O2 ↔ B2/main startup |
| Recording manifest | `recording_manifest.rs::RecordingManifest`: versioned UUID, producer generation, closed ordered files, markers, incomplete reason, state and verified immutable object references. Atomic file publication plus idempotent DB reconciliation, not a cross-filesystem transaction. | B3 → B2/O5/O7 |
| Ready prerecorded asset | `programme_assets.rs::{ReadyProgramme, PlaybackPin}` bind immutable confirmed revision/checksum to validated local bytes. Readiness is not a producer claim; pin and final revision check occur under admission. | B4 → B1/B2/O3/O5 |
| Output authority | `output_state.rs::OutputSnapshot`: supervised process epoch, event sequence, delivery generation, mode, associated broadcast UUID and effective UTC time. Epoch activation needs fresh proof of the current service; unknown callbacks never activate themselves. | O3 → B5/F5/O6 |
| Internal routes | Proposed `/api/internal/output/*` and `/api/internal/maintenance/*`, authenticated through existing loopback port 8000 and denied by every public proxy. Exact methods, payload limits, authentication, replay rules and error fixtures freeze in P2. | O2/O3 ↔ B2/B5/O4 |
| Public status | Stable `/now-playing.json` uses the accepted schema, nullable unknown fields, opaque revision, ETag and five-second caching. Legacy `/api/stream/status` retains producer `{active}`. Proposed authenticated `/api/admin/stream/status` supplies authorized operational detail. | B5/B6 → F1/F5/F6/O4/O6 |
| Artwork/presenter | Nullable `shows.public_presenter`; `artwork.rs::publish_cover_revision` is the only cover publication entry. Exact revision/transform/size URL serves persisted immutable bytes. No account login as public presenter. | B6 → F5/F6/O4/O7 |
| Release capabilities | Versioned, operator-promoted capability record binds verified MP3/continuous/HLS features to exact media/configuration artifacts. Normal executor checks deployed evidence and active profile; callers cannot waive checks with a flag. Frontend artifact embeds allowlisted public profile/configuration digest. | O1/O2/O4/R2 → B2/F3/F4 |

### Candidate mechanisms to prove in P2

v1 left these as proof obligations. v2 names one concrete candidate for each, so the P2 spikes test something specific. A candidate is not frozen: if its proof fails, P2 records the failure and the replacement before dependent wiring starts.

| Obligation | Candidate | What must be proven (leaf) |
| --- | --- | --- |
| Executor lifetime and fencing | `systemd-run --unit=moafunk-deploy-<op> --property=KillMode=control-group flock /run/lock/moafunk-deploy.lock execute.sh <op>`. All descendants inherit the lock descriptor, so the lock is free only when the whole cgroup is gone. Recovery: `systemctl kill --signal=KILL moafunk-deploy-<op>`, then acquire the lock. The journal is `/var/lib/moafunk/deploy/journal.json`, written by atomic rename. | A paused or orphaned child keeps the lock; lost SSH/CI does not release it; recovery cannot proceed while any descendant lives (O2.1.2, R1.2.2). |
| Docker requests already accepted | Every API container carries labels `moafunk.op=<op>` and the target digest. After fencing, recovery waits until no container of the service is `created`, `restarting` or in starting health for a bounded quiet period (`docker events --filter label=moafunk.op`), then converges to the recorded target and removes containers of superseded operations. Unknown outcome: keep the lock and the gate closed. | Kill the client after `docker run`/`compose up` was accepted; the late container must not become the verified service (O2.3.1). Measure the quiet period. |
| API-down startup barrier | The executor journal directory is mounted read-only into the API (for example `/run/moafunk-deploy`). A recovery start also sets `MOAFUNK_ADMISSION=closed`. `main.rs` reads the DB gate, the journal and the variable before spawning schedulers, ingest or background tasks. Any closed or unreadable input keeps admission closed; diagnostics and reconciliation routes stay up. The API remains the only SQLite writer. | An open DB gate plus an incomplete journal, a corrupt journal, and a rollback image without the barrier code (O2.3.2, B2.3.5). The last case is why rollback targets must be gate-aware images only after bootstrap. |
| Current Liquidsoap process | `liquidsoap.service` passes systemd's per-start `INVOCATION_ID` into the container and writes it to a host file mounted read-only into the API. The script serves `GET /moafunk/epoch?nonce=…` with `harbor.http.register` on the existing harbor port and echoes nonce, epoch and invocation ID. The API activates an epoch only from this synchronous reply, and only when the invocation ID matches the file. Callbacks from any other epoch are recorded but never activate. | `harbor.http.register` exists in 2.4.4 with this use; the variable reaches the script; after a restart the old process cannot answer and its late callbacks are rejected (O3.1.1 before P2.2.1). |

P2 must settle ownership and schemas, not implement a generic RPC/job framework. Use bounded local adapters for the installed media version. If a proposed name conflicts with an existing route, change it once in the shared fixtures and this table before dependent wiring.

### Dependency and PR order

“Coding prerequisite” means the shared interface or prior module must exist. “Enablement gate” means the feature may be developed with fixtures but cannot be activated yet. Descriptive dependencies in taskbooks resolve through this table; in particular B6 uses the P2 public DTO and does not wait for B5 deployment.

| Work | Coding prerequisites | Enablement gate / PR order |
| --- | --- | --- |
| P1, O1 | None; use current baseline | Inventory and split build/deploy first |
| P2 (v2: per leaf) | P2.1.1, P2.1.3, P2.2.2, P2.2.3: P1. P2.1.2: P1 + O1.1. P2.2.1: P1 + O3.1.1 | Freeze each contract before the wiring that consumes it; a pending P2.2.1 blocks only B5.1, O3.3 and F5 activation |
| F1 → F2 | v2: P1 only (legacy `{active}` exists at baseline) | Wave 0: ship legacy-compatible MP3 fixes first |
| B1 → B2 | v2: B1.1 and B1.3.5 need the P2.1.1 authorization matrix only (Wave 0); B1.2–B1.3 need P2.1.1 identity; B2 needs B1 + P2.1.1 + P2.1.2 | Wave 0 fixes ship through O1.2.5; B2 needs the O1 controlled legacy window and O2 recovery proof |
| O2 | P2; B2 API fixtures allow independent script work | Bootstrap gate only after B2 and O2 tests pass; no future-HLS dependency |
| O5, O6, O7 | O1 inventory; P2 where schemas matter | Early current-system protections; repeat coverage as new storage/formats arrive |
| B3 | B1/P2; O5 resource interfaces | Persistent spool, crash tests and O7 recovery; B2 still waits for full finalization until then |
| O3 | O3.1.1 spike starts after P1/O1.1; later integration uses P2 identities/descriptors | Approved local assets; B1/B5 integration; legacy-compatible F1/F2 deployed before server cutover |
| B4 | B1/B2/P2; O3 command harness for final media integration | Local revision/pin, quota and handover tests |
| B5, B6 | v2: B5 needs P2.2.1 + P2.2.2 and B1 identity; B5.2.6 needs nothing (Wave 0); B6 needs P2.2.2 | B5 authority needs O3; B6 ready art before advertised show art |
| F6 | P2 private-status/show DTOs | Backend additive routes/field → new admin → B5.2.4 privacy removal |
| O4 | O4.1 encoder work uses O3; O4.2 routes use P2 and can proceed independently | Publication persistence/restart tests and public-host handoff |
| F3 | F1/F2/P2 | F3.2.1–F3.2.2 safety fixes ship early; continuous behavior requires server + O6 MP3 decode gate |
| F5 | F1/F2/P2; can use fixtures | B5/B6/O4 public metadata/routes; integrate sequentially with F3/F4 |
| F4 | F2/F3/P2 | O4 HLS proof and F7 before native-HLS default |
| F7, R1 | Baseline/protocol can start in P1 | Integrated candidate from relevant B/F/O tasks for final evidence |
| R2 | Relevant R1/F7 gates, O6/O7 | Promote one profile at a time; each rollback remains executable |
| R3 | R2 evidence | Operator handoff and actual limits recorded |

### Wave schedule

Waves order the work for parallel Claude/Codex workers. A wave starts when its listed prerequisites are verified, not when the previous wave is fully done. Within a wave, rows in different lanes can run in parallel; rows in the same lane touch shared files and run in sequence under that lane's integrator.

| Wave | Lane | Leaves | Why now | Prerequisite |
| --- | --- | --- | --- | --- |
| 0 | Ops | O1.2.4 backend CI | Every later backend PR needs a red/green signal; CI runs no Rust tests today | None |
| 0 | Ops | O1.2.1 build-only push, then O1.2.5 interim manual deploy | A push to `main` restarts the API today and drops a live show | O1.2.4 |
| 0 | Backend integrator | B1.1.1–B1.1.3, B1.1.5 authorization; B1.3.5 write binding | Any signed-in user can broadcast on any show, force a takeover or stop the stream; a takeover corrupts the new stream | P2.1.1 authorization matrix (small, first P2 deliverable) |
| 0 | Backend integrator | B1.1.6 prerecorded does not kill live | A scheduled prerecorded start silently stops a live host today | None |
| 0 | Backend integrator | B3.3.5 stop age-only recording deletion | Unuploaded recordings are deleted after 24 h, and at boot before recovery | None |
| 0 | Backend integrator | B5.2.6 drop path/error from public status | The public endpoint leaks a server path and error text; no admin reader needs them | None |
| 0 | Ops | O4.2.4 close unneeded public routes; O7.1.5 back up the shows bucket | Public Icecast status; archived show MP3s have no backup copy | O1.1.1 for the route list |
| 0 | Public player | F1, F2 | One failed status poll stops every listener today | P1 |
| 0 | Coordinator | P1, O1.1, O3.1.1 spike, P2.1.1 | Baseline, decisions register, authorization matrix and the media proof that later contracts need | None |
| 1 | All | P2.1.2, P2.1.3, P2.2.2, P2.2.3; O5.2.4 SQLite decision; O6.2.2 human alert path; O7.1.1–O7.1.3 | Contracts for the gate, recording and public DTOs; known monitoring and backup gaps | Wave 0 P1/O1.1 |
| 2 | Backend + Ops | B1.1.4, B1.2–B1.3 identity, B2 gate, O2 executor, O1.2.2 bootstrap window | Controlled deploys before larger changes | P2.1.1, P2.1.2 |
| 3 | Backend + Ops | B3 recording durability, O5 limits, B4 staging, F3.2 safe updates, F6 admin migration, B6 artwork | Data safety and additive contracts | Gate bootstrapped (R2.1.2) for production activation; coding can start in Wave 1 |
| 4 | Media + Backend + Player | P2.2.1, O3 fallback and authority, B5, F3.1, F5, O4.2 routes | Continuous MP3 with truthful metadata | O3.1.1 proof, approved fallback/ident (operator), F1/F2 deployed |
| 5 | Media + Player + Devices | O4.1 HLS, F4, F7, R1.3 | Native HLS only after device gates | Wave 4 in production, physical devices (operator) |
| 6 | Release | R1 remaining, R2, R3 | Rollout, evidence, hand-over | Relevant waves verified |

Wave 0 fixes deploy only through O1.2.5 after O1.2.1, in a show-free window, with the operator confirming no rehearsal or finalization is running. They must not wait for the maintenance gate; the gate is itself deployed through this path during the O1.2.2 bootstrap.

Suggested feature PRs follow the taskbooks: lifecycle/recovery; authorization/session ownership; maintenance state/admission; host executor; recording manifests/reconciliation; preload/pinning; output authority; presenter/art publication; authenticated admin migration; public privacy cutover; fallback/lossless media; HLS/routes; public metadata/Media Session; transport selection. Monitoring, backup and resource work can proceed alongside them. Split a parent task where its stated PR boundaries require it; do not combine unrelated features for convenience. Every production API/admin PR still uses the gate after bootstrap.

### Runtime defaults and promotion rules

| Area | Initial setting; change only with recorded evidence |
| --- | --- |
| Audio | MP3 256 kbps/44.1 kHz stereo; AAC-LC 128 kbps/48 kHz stereo; no extra lossy intermediate |
| HLS | MPEG-TS candidate; 4-second target, 10-segment window, at least 6 ready segments; 1-second playlist and 60-second segment cache |
| Origin retention | At least 120 seconds after removal and at least segment duration plus longest containing playlist; persistent monotonic sequence/discontinuity state |
| Fallback | Qualified producer → approved local fallback → local emergency ident; 3-second source-loss grace, 2-second valid return; safety silence is an alerted failure |
| Status | Visible polling 10 seconds, timeout 5 seconds, one request; first failure stale, third may warn; no audio reset |
| Authority | Snapshot every 5 seconds, unknown after 15 seconds, programme history at least 10 minutes |
| Recovery | Initial 8-second no-progress threshold; 1/2/4/8/16/30-second delays ±20%; reset after 30 healthy seconds; 3 HLS failures then one-way MP3 fallback per session |
| Resources | One heavy non-live job; live ingest/capture/MP3/AAC excluded. Archive priority. Explicit storage reserves and bounded queues |
| Database | WAL/FULL/5-second busy timeout only after actual linked SQLite/filesystem validation and WAL-reset fix check; all connections covered |
| Maintenance | Reserved deploy + rollback + margin, initially at least 30 minutes; expiry never opens admission |
| Backups/observer | Daily metadata target, 30 daily snapshots and ≥30-day replaced/deleted media retention; external decode candidate every 60 seconds/3 failures; tune quiet-content grace |

Profiles: **legacy MP3** retains confirmed producer off-air behavior; **continuous MP3** requires locally survivable fallback; **continuous HLS eligible** adds qualified native HLS while keeping MP3. Code can contain a disabled feature. Activation requires evidence. The final target's ordinary API maintenance verifies both formats; the early legacy profile cannot depend on formats not yet installed. Capability promotion is monotonic during normal rollout; removing an active capability requires an explicit tested media/profile rollback.

## P1 — Capture the implementation baseline and prepare reproducible evidence

Owner: coordinator. Files: new `docs/implementation/{baseline,execution-log,device-matrix}.md`, fixtures under the current test directories. Coding dependencies: none. PR boundary: baseline/protocol documentation and necessary fixture helpers. Rollback: no runtime behavior changes.

### P1.1 — Record actual scope and external prerequisites

- [ ] **P1.1.1** Record current worktree commit, deployed image/configuration digests, repository versus production facts, supported routes and data/storage owners with O1. Recheck the architecture/streaming input hashes. **Verify:** another implementer can identify exactly what was inspected and what still needs production inspection.
- [ ] **P1.1.2** Create an operator decision register for domain/DNS ownership, approved fallback/ident, physical devices, external observer/recipient, backup destination/account/cost, longest supported show, capacity/traffic allowance and maintenance windows. Assign an owner and required-before task to each missing item; use no secret values. v2 adds two decisions: whether prerecorded shows are archived and published like live ones (at baseline they are never recorded or archived), and where the R1.3.2 200-listener test runs (a separate representative host may need a purchase; a quiet-hours test on production needs explicit approval and a stop condition). **Verify:** coding can proceed with fixtures, but every dependent activation is visibly blocked until its prerequisite is supplied.
- [ ] **P1.1.3** Record current playback and recording behavior with synthetic approved fixtures: live/rehearsal/prerecorded, natural stop, marker persistence, API failure and current public/private status shape. **Verify:** later improvements compare against a reproducible baseline, not memory or an emulated iPhone.

### P1.2 — Make agent handoffs and tests repeatable

- [ ] **P1.2.1** Create the execution log with stable IDs, file claims, feature PR boundaries, verification evidence and coding/activation status. Agree one editor for every shared hotspot. **Verify:** two parallel workers can claim independent work without touching the same integration file.
- [ ] **P1.2.2** Inventory test commands and existing failures from the actual baseline; define deterministic clocks, process/object-store fakes and small media fixtures only where needed. Keep credentials and private programme audio out of fixtures. **Verify:** a clean checkout runs the documented focused tests without production access.
- [ ] **P1.2.3** Create an isolated media/fault test protocol with unique ports, temporary volumes, disabled publishing/bot/live output and explicit teardown. Tie each evidence run to artifact/configuration and media-tool versions. **Verify:** running the harness cannot target production by an omitted parameter or inherited environment default.
- [ ] **P1.2.4** After Anton approves implementation, create one GitHub issue per task (25) with the leaves as a checklist, the project's `type::*` and most specific `project::*` labels, and the matching milestone (for example B1 → `type::backend`, `project::Stream`, *Webbased Streaming*; B3 → `project::recording`, *Webbased Recording (UNHEARD)*; O7 → `type::ci`, `project::Backup`, *Infrastructure & Secrets*). Branches are `<type>/<issue>-<slug>`; every issue/PR reference uses its full URL. **Verify:** each leaf ID appears in exactly one issue, and no issue is created before approval.
- [ ] **P1.2.5** Add the agent leaf checklist to `execution-log.md`: (1) claim the leaf and its files in the log; (2) rebase and recheck the [anchors](anchors-v2.md) against the current branch; (3) GitNexus `impact` on every symbol to be edited and report the risk; (4) write the failing test first where the leaf is a bug fix; (5) implement the smallest correct diff; (6) run the PR checks above plus `gitnexus detect_changes`; (7) record commit, tests and outcome, and hand shared-file wiring to the lane integrator. A worker never edits a file claimed by another open leaf. **Verify:** a fresh worker can execute one Wave 0 leaf from the log and anchors alone, without reading the review history.

## P2 — Freeze contracts before cross-component wiring

Owner: coordinator with backend/frontend/operations owners. Files: new `docs/implementation/contracts.md`, shared JSON/state fixtures placed with consuming tests, minimal proposed type stubs if implementation is later authorized. Coding dependencies: P1; O1.1 installed version/paths and the explicitly pre-freeze O3.1.1 capability/proof spike for host contracts. PR boundary: contract and fixture agreement. Rollback: revise before implementation; after consumers ship, version contracts additively.

### P2.1 — Agree identity, admission and recovery boundaries

- [ ] **P2.1.1** Freeze broadcast and admission types, lock order, cancellation behavior, authorization matrix and all start/stop/recording/schedule entry points. Specify request/error payloads for maintenance, capacity and prerecorded readiness. **Verify:** B1/B2/B4 and admin owners walk the same simultaneous-start and schedule-edit fixtures to the same outcome.
- [ ] **P2.1.2** Specify the host executor journal/lock/cgroup lifecycle, authenticated internal methods, operation-ID CAS and API-down recovery startup order from the table above. Include SSH/CI loss, killed parent with surviving child, host reboot and a Docker request whose outcome is uncertain. **Verify:** a written state trace proves no old executor can mutate after a replacement owner proceeds, and no scheduler starts before recovered gate state is known.
- [ ] **P2.1.3** Freeze recording manifest/schema versions, object verification, marker acknowledgement, per-recording serialization, legacy recovery and resource-permit interfaces. Define raw segments versus finalized derivatives and which verified artifacts are required before deleting each. **Verify:** each filesystem/DB/object-store crash point has a discoverable next action and no “object exists, delete local” shortcut.

### P2.2 — Agree output, metadata and release contracts

- [ ] **P2.2.1** Freeze source descriptors, callback/snapshot schemas and a concrete current-supervised-process proof mechanism supported by the O3 harness. Bind a fresh challenge/snapshot to the service instance; UUID randomness or accepting the latest HTTP callback is insufficient. **Verify:** old and current processes responding out of order cannot activate the old epoch, including after API restart.
- [ ] **P2.2.2** Freeze public/admin/show/artwork DTOs and route/error/cache fixtures, including unknown/stale output, nullable fields, opaque revisions, role access and every cover writer. Freeze canonical public URLs while retaining aliases. **Verify:** backend and frontend tests consume matching fixtures and anonymous outputs contain no private account/path data.
- [ ] **P2.2.3** Freeze profile/capability and deployment-identity formats, promotion ownership and compatibility matrix for old public bundles, current admin and rollback images. Include the staged public-field removal and schema-compatible rollback rules. **Verify:** no normal deployment can self-declare HLS healthy or bypass admission, and same-commit frontend configuration changes produce a new artifact identity.

## R1 — Prove the integrated system under faults and load

Owner: integration/test lead with domain owners. Files: focused integration tests plus new dated `docs/implementation/evidence/` reports and isolated harness scripts. Coding dependencies: P2; build fixtures early. Final evidence needs the relevant domain implementations. PR boundary: integration regressions with the feature they expose; final evidence as a separate report. Rollback: stop the isolated experiment, retain evidence and leave failed capabilities disabled.

### R1.1 — Complete operator-visible integration

- [ ] **R1.1.1** Wire B2/B3/B4 machine errors and statuses into the existing admin start/capture/schedule/recording flows: closed gate/reason, reserved window, preload readiness, missed start, backlog/incomplete archive and explicit retry. Assign these files to the F6 admin owner; inventory exact components before editing. **Verify:** a rejected action never displays success, maintenance gives an actionable explanation, and an explicit retry cannot silently authorize takeover or late scheduled playout.
- [ ] **R1.1.2** Verify the public/admin privacy sequence with old and current bundle fixtures, then inventory all remaining public-status consumers before B5.2.4. **Verify:** old public listeners retain producer `{active}`, current admin works through authenticated detail, and stale admin bundles fail safely or refresh without recovering private anonymous fields.
- [ ] **R1.1.3** Run one full source/identity scenario across browser live, rehearsal, scheduled local prerecord and fallback with presenter/cover changes and delayed producer cleanup. **Verify:** audible selected source, output generation, public page, Media Session and direct ICY agree within measured transport/foreground lag; capture contains the intended producer only.

### R1.2 — Exercise maintenance and durable data boundaries

- [ ] **R1.2.1** Race maintenance against live/rehearsal/capture starts, pending child startup, schedule edits and reserved-window extension. Include API crash/replacement and overrun at airtime. **Verify:** no unauthorized new work crosses the gate, existing allowed work finishes, missed shows alert without late autoplay, and fallback stays audible during safe replacement.
- [ ] **R1.2.2** Pause/kill the executor at each state and after starting a mutation-capable child; lose CI/SSH and resume the old process after explicit recovery. Include API-unavailable rollback and host reboot recovery. **Verify:** at most one owner can mutate, incomplete operations remain closed and stale children cannot replace the newly verified service.
- [ ] **R1.2.3** Kill capture/finalization across segment/marker/manifest/upload/DB/cleanup boundaries; inject corrupt equal-size objects, R2 outage, full recorder queue and disk pressure. **Verify:** no unverified local archive is removed, committed markers and versions recover without duplication, incomplete tails are visible and healthy live audio continues.

### R1.3 — Qualify media, capacity and independent recovery

- [ ] **R1.3.1** Run O3/O4 faults: changed/partial prerecorded download, source handover, Icecast failure while HLS runs, Liquidsoap graceful restart/crash, delayed epoch callbacks, metadata outage and clock corrections/DST fixtures. **Verify:** publication/identity rules hold; independent decode distinguishes wrong source, missing audio, stale metadata and expected nonseamless restart behavior. Attach F7 locked-device evidence where required.
- [ ] **R1.3.2** Run the bounded 200-listener mixed-format test on an isolated representative host with both encoders, live capture and one permitted heavy job; use a remote load generator and controlled request rates. Record CPU/RAM/I/O/FDs/egress, segment availability, recording integrity and headroom against O5 budgets. **Verify:** no starvation or violated reserves; reduce supported capacity or resize before promotion if measured limits fail. Do not infer capacity from a local loopback test.
- [ ] **R1.3.3** Disconnect origin reachability and miss a scheduled producer while fallback remains healthy; then perform O7 isolated restore including a replaced/deleted retained object. **Verify:** an external human alert arrives without the origin bot/API, schedule freshness is shown, retained backups restore correct references and measured recovery time is reported.

## R2 — Roll out tested profiles without interrupting sessions unnecessarily

Owner: release coordinator and operator. Files: release evidence, capability record, workflow/configuration artifact references and rollback runbook. Coding dependencies: domain work as listed per stage. PR boundary: configuration promotion separate from feature delivery. Rollback: exact previously tested artifact/configuration pair; never disable a required gate merely to make deployment pass.

### R2.1 — Establish protections on the current service

- [ ] **R2.1.1** Apply O1 build/deploy separation and O5/O6/O7 current-system alarms/backups; release F1/F2 legacy MP3 fixes plus F3.2.1–F3.2.2 session-safe update protection and configuration identity. Keep future HLS/continuous capabilities disabled. **Verify:** a normal push builds without restarting API or Liquidsoap, status failure leaves audio playing and the existing active-show flow remains compatible.
- [ ] **R2.1.2** Install B1/B2/O2 in the controlled legacy maintenance window after full finalization; rehearse rollback and API-down recovery before making the gate the only ordinary API deployment route. **Verify:** subsequent API/admin deployments require the persisted gate and lifetime host executor lock; initial bootstrap evidence does not falsely claim continuous fallback or HLS readiness.
- [ ] **R2.1.3** Enable B3 recovery and B4 staging after storage/crash tests. Continue to wait for finalization until durable pending-archive handoff has independent evidence; record any later enablement separately. **Verify:** a rollback image can read existing state or the gate stays closed until compatible recovery; pending archives are preserved.

### R2.2 — Introduce authoritative continuous MP3 and metadata

- [ ] **R2.2.1** Deploy additive B5/B6 endpoints/schema, F6 admin consumers and R1 admin feedback, then perform B5.2.4 privacy removal. Publish stable MP3/playlist/artwork/JSON routes and old aliases through O4's staged route work. **Verify:** anonymous privacy and old-listener compatibility tests pass against the deployed artifact pair.
- [ ] **R2.2.2** Activate O3 approved local fallback/ident and authoritative source reports in an explicit media window with MP3 rollback available. Validate API/R2-down local continuity and external MP3 decode before deploying F3 continuous MP3 plus F5 metadata. **Verify:** current continuous clients survive show end; cached legacy clients keep their documented show behavior until a safe refresh, with no forced reload during listening.
- [ ] **R2.2.3** Observe two complete shows and between-show playback with live/prerecorded transitions, real devices, metrics and alert receipt. **Verify:** continuous capability is promoted only with actual records; unresolved source/recording/privacy faults block wider promotion even if HTTP probes pass.

### R2.3 — Promote native HLS only after physical qualification

- [ ] **R2.3.1** Publish O4 HLS as a disabled-by-default/canary path; run restart/retention/conformance and F7 device gates against the exact media configuration. **Verify:** stable URLs/sequence, old segments, audio decode and locked-device behavior pass; failures keep continuous MP3 as the supported default.
- [ ] **R2.3.2** Promote F4 only for the recorded qualified client rule; bind the enabled HLS artifact to O6 external HLS decode coverage and the capability record. Confirm the target maintenance profile now verifies both formats. **Verify:** other clients keep MP3, three failed HLS recoveries switch once, and a same-commit config rollback selects MP3 after a session-safe update.
- [ ] **R2.3.3** Rehearse frontend, API and media rollback separately and record the final image/schema/configuration/artwork compatibility set. **Verify:** frontend rollback requires a real build/deploy, API rollback preserves gate/data, media rollback handles its announced interruption, and no path prunes HLS state, recordings or approved fallback assets.

## R3 — Hand over the verified operating contract

Owner: coordinator and operator. Files: new operating/recovery runbooks linked from project documentation and final acceptance report. Dependencies: relevant R2 evidence. PR boundary: operating documentation with commands tested in the isolated environment. Rollback: correct inaccurate instructions; documentation acceptance never substitutes for missing operational tests.

### R3.1 — Make routine and incident work executable

- [ ] **R3.1.1** Document normal API deployment, closed-gate recovery, compatible rollback, media window, graceful nginx reload, host maintenance and explicit incident override. Include actor, expected interruption, command inputs, stop conditions and verification. Use the Bitwarden workflow when implementation needs secrets; document references only. **Verify:** a second operator completes an isolated deployment and API-down recovery without undocumented access or direct DB edits.
- [ ] **R3.1.2** Document recording backlog/retry, reserve pressure, missed show, stale metadata, backup lag and restore procedures; schedule monthly and post-backup-change restore exercises. **Verify:** each alert links an actionable runbook with an owner; destructive cleanup is never the default remedy for unverified recordings.
- [ ] **R3.1.3** Publish a short public stream contract with canonical URLs, M3U/PLS, metadata/artwork examples, data-use figures and tested clients. State origin-loss, cross-page audio and suspended-JavaScript metadata limits. **Verify:** examples resolve on the accepted release and no untested AirPlay/CarPlay/home-screen claim is included.

### R3.2 — Close the implementation against evidence

- [ ] **R3.2.1** Audit every leaf and requirement mapping below; distinguish implemented, activated, verified and deferred. Attach exact artifact hashes and failed/unsupported device cases. **Verify:** no unpassed mandatory requirement is described as complete; any scope change has an explicit recorded decision.
- [ ] **R3.2.2** Record measured capacity, actual traffic allowance, recovery point/retention and restore time. Record triggers for separate ingest, independent origin and independent metadata publication. **Verify:** one unrecovered announced-show origin outage triggers the agreed standby review, and no unmeasured availability or recovery-time guarantee is published.

## Requirement coverage and review checklist

| Accepted requirement | Primary tasks | Integrated evidence |
| --- | --- | --- |
| Architecture §2–3 process/public boundaries; API-down fallback | O1–O4, B2/B5 | R1.1.3, R1.2.2, R2.2.2 |
| Architecture §4 admission, reservation, fencing, rollback | P2.1, B1/B2, O2 | R1.2.1–R1.2.2, R2.1.2 |
| Architecture §5 recording durability, markers, cleanup, optional jobs | B3, O5 | R1.2.3, R2.1.3 |
| Architecture §5–6 local prerecorded revision/pin, no late autoplay | B4, B2.2, O3/O5 | R1.1.3, R1.3.1 |
| Architecture §6–7 SQLite, clocks, reserves, capacity/egress | O1/O5, B2/B3 | R1.2.3, R1.3.1–R1.3.2 |
| Architecture §7 independent observer, missing producer, human alert | O6 | R1.3.3 |
| Architecture §7 retained backups, both buckets, isolated restore | O7, B3/B6 | R1.3.3, R3.1.2 |
| Streaming §1 stable host/aliases, CORS, direct clients | O1/O4, F3 | R2.2.1, F7.2 |
| Streaming §2 HLS/lossless source, restart, retention | O3/O4, B4, F4 | R1.3.1, F7, R2.3 |
| Streaming §3 lifecycle, status unknown, cancellation, deployments | F1–F3 | F7, R2.1.1 |
| Streaming §4 auth/session/output epochs/privacy | B1/B5, F6, O3 | R1.1, R1.3.1 |
| Streaming §5 presenter and every immutable artwork writer | B6, F5/F6, O4/O7 | R1.1.3, R1.3.3 |
| Streaming §6 continuous selection, truthful Media Session/ICY | O3, B5/B6, F3/F5 | F7, R2.2.2–R2.2.3 |
| Streaming §7–8 real devices, faults, origin limits | O6, F7, R1–R3 | Exact release evidence and explicit unsupported cases |
| v2: baseline defects (auth, takeover corruption, prerecorded kills live, public leak, age-only deletion, missing CI, push-triggered restart, unbacked shows bucket, public Icecast status) | Wave 0: B1.1.1–B1.1.3, B1.1.5, B1.1.6, B1.3.5, B3.3.5, B5.2.6, O1.2.1, O1.2.4, O1.2.5, O4.2.4, O7.1.5, F1, F2 | Regression test per defect in its own PR; R2.1.1 |
| v2: SQLite library in the WAL-reset range | O5.2.4, then O5.2.1–O5.2.2 | `sqlite_version()` from the built binary and the backup tool |

v2 hand-off to Codex: please review the v2 changes listed at the top against v1 and the code, especially the Wave 0 lane (does any Wave 0 leaf secretly need a later contract?), the four candidate mechanisms, and the anchors. Record `ACCEPT plan-v2` with the manifest hash, or concrete changes, in `codex-review.md`; a v3 may follow the same ID rules.

v1 text kept for history — Claude's first review should challenge: whether API-down recovery is implementable without a second DB writer; whether supervisor epoch proof survives stale replies; whether code/activation dependencies contain a cycle; whether bootstrap is possible before new media exists; whether the public privacy cutover preserves cached listeners; and whether every data-deletion path has durable evidence. Continue in `plan-v2.md` and changed v2 taskbooks if fixes are needed, preserving IDs and v1 history. Review the exact submitted files using the manifest; do not infer joint approval from this draft.
