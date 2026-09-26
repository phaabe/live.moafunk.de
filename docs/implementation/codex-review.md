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
