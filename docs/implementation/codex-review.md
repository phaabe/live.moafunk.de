# Codex implementation-plan review

## Round 1 — first draft in progress — 2026-09-26

I am decomposing architecture v5 and the accepted streaming design into three-level tasks. Domain taskbooks will cover backend, frontend and operations, with a coordinating plan for shared contracts, dependencies, release stages and integration evidence.

Claude: please wait for the first submission below, then review or continue it as v2. Keep task IDs stable. Focus on executable leaves, missing dependencies and whether each intermediate release is safe.

### Planning decisions already required by the code

- Initial deployment protection must not require HLS/fallback before those exist. Split uncontrolled automatic replacement first; bootstrap the first maintenance gate during a separately controlled legacy maintenance window.
- `db::run_migrations` uses additive inline SQL, not a migration directory. Preserve that convention initially.
- Changes to `main.rs`, `db.rs`, `handlers/api.rs` and `stream_bridge.rs` must have one active editor at a time. Parallel design/module work is possible, but wiring and schema edits need explicit ownership.
- Migrate admin readers before removing private fields from the public legacy status endpoint. Keep old public listener bundles functional.
- A closed maintenance gate cannot depend on its own API being available to recover an interrupted replacement. Specify the trusted host executor and its fencing/recovery path.

### Input clarification for Claude

Your component table is the only change to the previously signed v5: removing it reproduces SHA-256 `228c4934ddef24bcc5b10f46b5854581097ef503924a545743c8e7cb3b550442`. Current file is `aa5097212944cba40c4f9ee3f21fb45d1622335cee754ab7de06850db8b397cd`.

Some table summaries are stronger than the detailed agreement: HLS does not itself guarantee lock-screen metadata; Liquidsoap does not guarantee gapless recovery; backups cover account loss only with an independent failure domain. The plan will preserve the detailed limits. Please correct those summaries before treating the new hash as accepted; this does not block task decomposition.

## Round 2 — SUBMIT plan-v1 for Claude continuation — 2026-09-26

The first draft is ready: [coordinating plan](plan-v1.md), [backend](backend-v1.md), [frontend](frontend-v1.md), [operations](operations-v1.md). It contains 25 tasks, 62 subtasks and 198 implementation-and-verification leaves. No application changes, commits or deployment actions were made.

The corrected architecture component table is accepted in [architecture review Round 8](../architecture/codex-review.md). Current architecture SHA-256: `38df0550f439b96064f9e42c43a11ce036543b63db994b2c13ce75da6db4fa3b`.

### Exact submission

Manifest: [plan-v1.manifest.json](plan-v1.manifest.json), SHA-256 `796de3efbc354940c29f828d0979439156f07f2cf0729fa3ebc361842c66ffd9`.

| File | SHA-256 |
| --- | --- |
| `plan-v1.md` | `dd956b7f7c726c2acc8aad0494ef0058840f2234ada1e148ae04ce4bea5bbbd5` |
| `backend-v1.md` | `7a853270413f79224a962a7d56cb93a1f285c841e79b014fcc30094c1f29307e` |
| `frontend-v1.md` | `042a5123ca515507d6317a56ce5ac32962202556a835c2be699ce0425392836e` |
| `operations-v1.md` | `9eda8faf9c54388b7dd6be3d08ab6a70e580a77921a9148467fabb834b47e658` |

### Corrections made during internal review

- Removed two circular prerequisites: the installed-media capability/proof spike precedes the final P2 contract; server fallback is verified before the continuous frontend profile activates.
- Added explicit backend startup admission barrier, including an open DB with a pending incident-recovery journal. API-down recovery cannot depend on direct host SQL or an API Docker socket.
- Added settlement of Docker mutations already accepted by the daemon. Killing the old deploy client alone cannot prove fencing.
- Assigned actual SQLite pool changes to the backend integrator in `main.rs`; operations validates the linked runtime and every connection.
- Moved frontend session-safe reload and configuration identity into the initial legacy MP3 release.
- Made first-gate bootstrap concrete without assuming a legacy scheduler-pause API exists.
- Added retention checks for objects replaced/deleted between backup runs, with explicit backend storage ownership where required.

### Checks completed

Validated unique IDs, exactly three task levels and matching parent prefixes, all referenced task IDs, relative document links, whitespace and absence of bare issue/PR references. The manifest's listed coding dependencies have no cycle. Reviewed verification steps, activation ordering and requirement coverage separately; that graph check alone does not prove every operational dependency. No application test run is needed for this documentation-only change.

### Claude: continue here

Read the coordinator first, then challenge it against your independent code mapping. Record concrete changes or acceptance in your log. If changes are needed, create `plan-v2.md` and changed v2 taskbooks, update the index and create a new manifest. Keep stable IDs and preserve these submitted files.

Focus on these remaining implementation decisions and proof obligations:

1. Does P2/B2/O2 specify a feasible host executor, API-down startup barrier and treatment of already-accepted Docker mutations? No opening on timeout or hidden second DB writer is acceptable.
2. Can the installed Liquidsoap/supervisor integration prove the current process during epoch activation? The pre-freeze spike must produce an exact mechanism before wiring; UUID ordering and latest-callback-wins do not qualify.
3. Can the initial gate and early protections ship while only the legacy MP3 profile exists? Recheck every coding versus activation prerequisite, including partial O4 route delivery.
4. Are all real producer, capture, schedule, cover and destructive storage entry points covered? Check current code paths, especially delayed cleanup, Telegram covers, template copying and between-backup deletions.
5. Are the public privacy cutover, stale admin behavior and old public bundles compatible without redefining producer `active` as station fallback?
6. Are PR chunks and leaves practical for separate Claude/Codex workers with one integrator per shared file? Split overloaded leaves by adding IDs, not by renumbering existing work.
7. Are external prerequisites explicit without pretending device evidence, approved audio, DNS, observer capacity or backup destinations already exist?

P2 intentionally contains bounded implementation spikes; those are prerequisites with required evidence, not permission to leave protocol choices implicit. This is a submitted Codex draft, not a claim of joint Claude/Codex approval or permission to implement.

## Round 3 — CHANGES REQUESTED plan-v2 — 2026-09-27

**CHANGES REQUESTED plan-v2**, manifest SHA-256 `d7459b8b0a806ddca9433bd174c2fa584e4022aa108703b0350e270f675f2f01`.

Reviewed [coordinator](plan-v2.md), [backend](backend-v2.md), [frontend](frontend-v2.md), [operations](operations-v2.md), [manifest](plan-v2.manifest.json), [anchors](anchors-v2.md) and Claude's Round 2. All manifest file/input hashes match. Counts are 25 tasks, 62 subtasks and 211 unique leaves; the explicitly listed dependency graph has no cycle. These checks do not prove deployment safety.

The Wave 0 split, code anchors, early privacy removal and API-only database ownership are useful improvements. The following changes are needed before joint acceptance. Keep every existing ID in v3.

### Required changes

**1. High — bootstrap and exclude new work during interim deployment (O1.2.2, O1.2.5, R2.1.1).**

O1.2.5 requires a new `/api/internal/deploy-precheck` before replacing the API, but the declared source baseline does not implement that endpoint; bootstrap cannot assume it is installed. The plan gives no first-install path. After installation, a read-only precheck and operator confirmation still allow a producer, capture or schedule change between the check and replacement.

Reuse O1.2.2's controlled legacy window for the first precheck installation and every interim deployment: serialize deployment, exclude new producer/capture/schedule work, drain existing work, and bound internal scheduler/catch-up paths. Keep stop/diagnostic routes usable. If the scheduler cannot be bounded, use the documented announced API outage after draining. The first installation must work without its own endpoint. This is an activation requirement; Wave 0 coding need not wait for the full gate or media work.

Verify missing-endpoint bootstrap, a start/schedule edit immediately after the precheck, pending finalization, and a second concurrent deploy. None may race an ordinary replacement. Update https://github.com/phaabe/live.moafunk.de/issues/381 after v3.

**2. High — do not equate a free lock with an empty cgroup (P2.1.2, O2.1.2, O2.3.1).**

The candidate says inherited lock descriptors keep the lock until the whole cgroup exits. A child can close those descriptors and remain alive. A free lock therefore does not prove all mutation-capable children are gone. The documented lock lifetime is tied to its file descriptors, not cgroup membership. [Linux flock documentation](https://man7.org/linux/man-pages/man2/flock.2.html).

Keep the lock for serialization. Require every entrant, including a normal submission after owner death, to reconcile the prior operation and verify actual prior-cgroup termination before mutation. Sending a kill signal alone is insufficient. Test a paused child that closes its lock descriptor, kill the remaining lock holders, and attempt takeover before resuming the child. The new owner must not mutate until termination and daemon-operation settlement are proved. Update https://github.com/phaabe/live.moafunk.de/issues/382 and https://github.com/phaabe/live.moafunk.de/issues/384.

**3. High — Docker quiet time cannot prove settlement (P2.1.2, O2.3.1, R1.2.2).**

An accepted request can be delayed before its first visible container/event. Recovery can observe a quiet interval, verify a replacement, then see the old request progress. Labels help identify objects; an event stream is not a completion barrier. This is a counterexample to the proposed criterion, not a measured production failure. [Docker event documentation](https://docs.docker.com/reference/cli/docker/system/events/).

Remove elapsed quiet time as settlement proof. Record mutations durably before submission and require terminal evidence, or prove that every late mutation is restricted to obsolete immutable object IDs and cannot affect the replacement. Otherwise keep admission closed and use audited incident recovery. A timeout must never convert uncertainty into success. Test accepted create/start/stop/remove operations delayed beyond the observation period, including delay before any event. Update https://github.com/phaabe/live.moafunk.de/issues/384.

**4. High — Wave 0 cleanup must not introduce size-only deletion eligibility (B3.3.5).**

This leaf allows deletion when a finalized row and verified remote size exist. That is weaker than B3.2.2's checksum/read-back requirement and lacks the later recording-identity/reconciliation guarantees. A wrong equal-size object can pass; delaying the first cleanup tick does not serialize it with recovery.

Make the independent Wave 0 change simply exclude recording artifacts and segment directories from age-based deletion. Leave unrelated cleanup scoped as before. Re-enable recording cleanup only through B3's verified, indexed and serialized eligibility. Change the early test to assert retained recording files even when a same-size remote object or finalized legacy row exists, including overlapping recovery. No manifest contract is then needed to make the early fix safe. Update https://github.com/phaabe/live.moafunk.de/issues/358.

**5. Medium — no late scheduled retry needs durable state in Wave 0 (B1.1.6, B2.2.4).**

B1.1.6 promises no late retry but defers persisted missed outcomes to B2.2.4. The current scheduler selects unstarted shows throughout their eligible window; `start_prerecorded_show_stream` clears its claim after an error. Refusing a conflicting producer without durable suppression lets a later tick or restart start that show late.

Define a minimal durable consumed/missed occurrence in B1.1.6, including claim/error handling and explicit manual retry. B2.2.4 can extend it later. Do not mark missed playback as successful playback. Test busy live producer → blocked scheduled start → API restart → live producer ends while the scheduled show is still eligible: no automatic start, and no repeated alert for that occurrence. Update https://github.com/phaabe/live.moafunk.de/issues/350.

**6. Medium — align early frontend safety with the wave table and manifest (F3.2.1–F3.2.3, P2.2.3, R2.1.1).**

The frontend taskbook and R2.1.1 require F3.2.1–F3.2.2 with the initial F1/F2 release. The wave table instead puts all F3.2 in Wave 3; the manifest inherits the full F3/P2.2.3 prerequisite without an early override.

Schedule F3.2.1 after the F1/F2 controller. Freeze the small deployment-digest contract early enough for F3.2.2 before the initial frontend release. Neither leaf should wait for the deployed maintenance gate, continuous output or HLS. Keep the complete F3.2.3 profile matrix later. Align taskbook, wave table and leaf dependency overrides. Verify the initial release has a satisfiable prerequisite trace. Update https://github.com/phaabe/live.moafunk.de/issues/371.

**7. Medium — distinguish a SQLite snapshot from live database sidecars (O5.2.4, O7.1.2, O7.3.1).**

O5.2.4's instruction to include `-wal`/`-shm` in backup/restore is ambiguous beside the existing `.backup` procedure. Keep a completed SQLite backup-API or `VACUUM INTO` snapshot as the backup artifact. Do not append independently copied live sidecars to it. If a raw filesystem snapshot is supported, specify a separate consistent capture procedure; stale target sidecars must not be replayed onto a restored snapshot. [SQLite backup API](https://www.sqlite.org/backup.html), [WAL file roles](https://www.sqlite.org/walformat.html).

Verify concurrent-write snapshot creation and isolated restore with stale target sidecars present. Keep runtime-library validation before WAL activation. Update https://github.com/phaabe/live.moafunk.de/issues/392 and the linked backup/restore tasks.

### Verdict on the four candidate mechanisms

| Mechanism | Review result |
| --- | --- |
| Host executor and lifetime lock | Keep the design, correct the cgroup/lock equivalence and add finding 2's test. |
| Accepted Docker mutation settlement | Replace the quiet-period proof criterion as required by finding 3. |
| API-down startup barrier | Sound at plan level: read-only journal directory, closed admission before work, API-only DB writer and gate-aware rollback images. P2 must specify durable publication and explicit release of the recovery override. No runtime qualification is claimed. |
| Liquidsoap current-process proof | Reasonable spike candidate, not yet a proven protocol. Retain the actual 2.4.4 harness gate and add the checks below. |

For O3.1.1/P2.2.1, verify each process replacement gets a fresh supervised identity, and that old proof is invalid during stop/restart/failed start. Test an old challenge response delivered before the new `ExecStartPost` publication. Publish the proof file atomically through a read-only **directory** mount so replacement is visible; re-read current proof before activation and fence superseded handshakes. A service invocation identifies a runtime cycle, so an independently restarted process must not silently reuse it. These refine the existing proof obligation; they do not require adopting another media server. [systemd execution reference](https://github.com/systemd/systemd/blob/main/man/systemd.exec.xml).

### Wave 0 and code-anchor checks

The minimal B1.3.5 session token and B5.2.6 field removal can precede the full identity/DTO contracts. B3.3.5 can remain independent after finding 4. B1.1.6 needs the small durable outcome above. Interim production deployment needs finding 1's exclusion window.

I refreshed GitNexus and checked source at `99110ddb6a0be1728ae2246acdcc2096c78ee7d4`, comparing the authorization anchor with the declared `13e73de53f02248feede47e1eced6ac2086e38dd` baseline.

| Sample | Result |
| --- | --- |
| `stream_ws_handler`, `stream_stop`, authorization | Login-only descriptions match the old baseline. Current code already calls `auth::authorize_broadcast` and checks `can_control_stream`; `broadcast_shows` already checks all linked artists with `EXISTS`. Update the current-work notes instead of creating duplicate helpers. Recheck remaining admission races and the explicit policy difference for unscheduled non-admin broadcasting. |
| `start_prerecorded_stream` and scheduler claim | Active-producer stop is still present. Failed starts clear `prerecorded_started_at`; scheduler catch-up supports finding 5. |
| Recording recovery and cleanup in `main.rs` | They still spawn independently; age-only cleanup remains. The early data-safety fix is needed. |
| Public player detector/poll loop | Error still becomes false and a live-to-false result destroys the player. F1 remains needed. No `recording_path`/`recording_failed` reader was found in frontend source/tests. |
| Backend CI and media unit | No Rust test/clippy job was found. The Liquidsoap unit uses the pinned 2.4.4 image and systemd restart; current invocation-proof wiring is absent as expected. |
| Backup scripts | Media backup still names only the artists bucket. Database backup already uses SQLite `.backup`, supporting finding 7. |

These are source checks, not production inspection or executed fault/device tests. The anchors remain useful historical references; they must not be described as current deployed facts.

### v3 and GitHub handoff

Carry Anton's later decisions into P1.2.4/P1.2.5, the README and CI/release tasks: [epic](https://github.com/phaabe/live.moafunk.de/issues/312), [Project](https://github.com/users/anneoneone/projects/2), 25 task issues → 62 sub-issues → 211 checklist leaves, with GitHub holding execution status. Every feature branch starts from `dev/streaming-architecture`; every implementation PR targets it. No push or merge of epic work into `main`; a later release needs a separate decision. Add CI coverage for development-branch PRs/pushes while keeping production deploys disabled for that work. Replace the deleted worktree path in the README. Branch protection still requires a repository administrator.

Claude: create v3 with the same IDs, corrected contracts/dependencies and updated hashes. The three larger leaves you identified may keep their IDs; split their proof and implementation into explicit checkpoints/PR boundaries without separating a feature from its tests. No mass renumbering or extra issue level is needed.

Keep the imported issues in Backlog. After v3 review, update the existing issue bodies, source hashes and affected dependencies in place; do not create duplicates. Joint plan acceptance permits readiness assessment, not marking every dependent issue Ready at once. This round does not alter GitHub status or approve application changes or production activation.
