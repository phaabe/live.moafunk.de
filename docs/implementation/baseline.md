# Test baseline and agent handoff

Work item: https://github.com/phaabe/live.moafunk.de/issues/339
Epic: https://github.com/phaabe/live.moafunk.de/issues/312

Status: documentation evidence recorded; review pending. The local results
below cover their recorded commits only. Executable media isolation remains
open; no device is qualified by these tests.

## Scope and ownership

- Executor: Codex. Reviewer: Claude. Lane: coordination.
- Claimed leaves: P1.2.1, P1.2.2, P1.2.3 and P1.2.5.
- Claimed files: `docs/implementation/baseline.md`,
  `docs/implementation/device-matrix.md` and
  `docs/implementation/test-protocol.md`.
- Branch: `feat/339-test-handoff-baseline`, based on `dev/312-interim` at
  `162556178ff83cea104d7cc36babe3ad8a0321fc`.
- Claim: https://github.com/phaabe/live.moafunk.de/issues/339#issuecomment-5879306146

Follow [the epic rules](epic-rules.md). GitHub issues and project fields hold
execution status and file claims. This file holds reproducible test evidence.
Use the shared P1/O1.1 production inventory for host observations; do not create
a second inventory here. Fixture code needs a file claim and owner agreement
before editing another lane's files.

## Leaf evidence

| Leaf | Evidence before completion |
| --- | --- |
| P1.2.1 | Claim and In progress status are recorded on the issue. The parallel-worker evidence below links two overlapping claims and their final diffs: six backend files and four workflow files, with no shared path. Activation is not verified. |
| P1.2.2 | Commands, local results and fixture boundaries are recorded below. Keep failures and unrun checks visible when repeating the baseline on another commit. |
| P1.2.3 | [Protocol](test-protocol.md) defines the isolation and fault gates. The current harness has fixed ports and inherits Docker context; executable refusal tests are still required. |
| P1.2.5 | [Checklist posted on the epic](https://github.com/phaabe/live.moafunk.de/issues/312#issuecomment-5890843126). The fresh-worker walkthrough below reproduces P1.2.2 focused checks from a clean checkout without reading review history. |

P1.2.4 is already done except revision maintenance, per the
[readiness comment](https://github.com/phaabe/live.moafunk.de/issues/339#issuecomment-5855257018).
No accepted plan revision is changed by this work. Keep its source links and
unique leaf IDs intact.

## Parallel-worker evidence: 2026-09-28

| Worker | Claim and reviewed result | Final file scope |
| --- | --- | --- |
| Claude, B1.1.6 | [Claim at 00:46 UTC](https://github.com/phaabe/live.moafunk.de/issues/350#issuecomment-5861386934); [final diff](https://github.com/phaabe/live.moafunk.de/pull/410/files) at `a2a836b0df17c8772ffea04a40c0a10f5a1b668f`; merged at 12:18 UTC. | Six files under `backend/src/`: `broadcast_tests.rs`, `db.rs`, `handlers/api.rs`, `scheduler.rs`, `stream_bridge.rs` and `telegram_notify.rs`. |
| Codex, O1.2.4 | [Claim at 08:38 UTC](https://github.com/phaabe/live.moafunk.de/issues/381#issuecomment-5866447216); [final diff](https://github.com/phaabe/live.moafunk.de/pull/417/files) at `50a804fa086e314f096fd967a4c3d1c954c3444d`; merged at 12:30 UTC. | Four files under `.github/workflows/`: `README.md`, `epic-ci.yml`, `frontend.yml` and `tests/integration-ci.test.cjs`. |

Both claims were open from 08:38 to 12:18 UTC, on separate feature branches
targeting `dev/312-interim`. Comparing the complete final file lists gives
zero shared paths. Only Claude edited the backend integration hotspots;
Codex's broader workflow claim also reserved `backend.yml`, which its final
diff did not change. Claude's final diff includes the notification helper in
the same backend lane, as recorded in its PR body.

This verifies independent file ownership for those two workers. It does not
prove atomic multi-runner claims; that remains the separate work tracked in
https://github.com/phaabe/live.moafunk.de/issues/456.

On 2026-09-29 the [project](https://github.com/users/anneoneone/projects/2)
records this issue as In progress, Executor Codex, Wave 0 and Area Coordination.
Activation is Not applicable for this documentation work; this field is not
media, device or production verification.

## Remaining ops work

P1.2.3 remains on https://github.com/phaabe/live.moafunk.de/issues/339. A separate
ops PR must claim the launcher, configuration and refusal-test files under
`docs/stream-rework/local-test-harness/` before editing. Its acceptance checks
are the [media protocol](test-protocol.md): refusal before unsafe execution,
two concurrent isolated runs, independent media decoding and scoped teardown.
Keep the issue In progress until the required evidence exists. Documentation
review does not close this gate or establish activation evidence.

Do not list P1.2.3 in this documentation PR's `Leaf IDs:` metadata. The runner
counts leaves named by merged PRs as completed prerequisites, even when the
issue checklist is still open. The ops PR may list P1.2.3 after its evidence
passes. The runner also treats an issue URL in a merged PR's `Issue:` metadata
as a completed prerequisite. Work requiring media isolation must therefore wait
for P1.2.3 evidence, not merely a merged PR naming this issue. No new issue or
duplicate leaf is needed.

## Baseline capture format

For each focused test run, record the commit, working directory, exact command,
tool versions, exit status, passed/failed/skipped counts and evidence link.
Distinguish pre-existing failures from failures introduced by the change.
Keep credentials and private programme audio out of fixtures and evidence.

## Reproduce the local baseline

Start with a clean checkout of `e319efe17e376e5a0cdaa26b8ebefe10d4114418`.
Check `git status --short` before installing dependencies. Do not copy a
production environment file, start the application or run ignored service tests.
Dependency installation needs package-registry access, not production access.

Run each command separately from the directory shown. Stop on a failed command,
record its output and cause, then resolve the prerequisite before retrying.

| Directory | Command | Purpose |
| --- | --- | --- |
| `frontend` | `npm ci --ignore-scripts --no-audit --no-fund` | Install the locked test dependencies; this local baseline does not need lifecycle scripts. |
| repository root | `python3 -m unittest discover -s scripts/epic_guard -p 'test_*.py'` | Guard policy and mocked GitHub collection. |
| repository root | `node --test .github/workflows/tests/*.test.cjs` | CI triggers, permissions and deployment exclusions; requires the frontend install first. |
| `frontend` | `npm test -- --run` | All Vitest policy and composable tests. |
| `frontend` | `npm run lint` | ESLint. |
| `frontend` | `npm run typecheck` | TypeScript. |
| `backend` | `cargo +1.98.0 fmt --check` | Rust formatting with the CI toolchain. |
| `backend` | `cargo +1.98.0 test --locked --all-targets` | Rust tests; needs FFmpeg and the platform build dependencies. Never add `--ignored`. |
| `backend` | `cargo +1.98.0 clippy --locked --all-targets -- -D warnings` | Strict plan check; record existing warnings separately from regressions. |
| `frontend` | `npm run build` | Build check required for frontend changes. |

CI uses Node 20 and Rust 1.98.0 on Ubuntu 24.04. Its Clippy command does not
promote warnings to errors. A local pass does not replace the current-head CI
checks or the stricter plan check. Documentation-only changes need structural
and link checks; the application runs here establish the requested baseline.

### Recorded run: 2026-09-29

Source: `e319efe17e376e5a0cdaa26b8ebefe10d4114418`, initially clean worktree.
Host: macOS, Apple Silicon. Python 3.13.7; Node 26.3.0; npm 11.16.0;
Vitest 1.6.1; Rust toolchain 1.98.0; host FFmpeg 7.1.
Node differs from CI, so these results are not a Node 20 qualification.

| Check | Result |
| --- | --- |
| Locked frontend install | Exit 0; 321 packages installed. Deprecation warnings only. |
| Guard tests | Exit 0; 42 passed. |
| Workflow tests before dependency install completed | Exit 1; missing `js-yaml`. Setup-order failure, not a failing workflow assertion. |
| Workflow tests after dependency install | Exit 0; 12 passed. |
| Frontend tests | Exit 0; 23 files, 168 tests passed. Network-error messages are expected mocked error cases. |
| Frontend lint and types | Both exit 0. |
| Rust formatting | Exit 0. |
| Rust tests | Exit 0; 105 passed, zero failed, two existing real-R2 tests ignored. Build took 17m 29s; tests took 9.79s. |
| Strict Clippy and frontend build | Not run in this capture. |
| Media harness and physical devices | Not run; see the protocol and device matrix. |

No test failure has been waived. The initial workflow failure was resolved by
the documented dependency install. Do not reuse these counts for newer code.

The [workflow Clippy inventory](../../.github/workflows/README.md#clippy-baseline)
records 38 warning locations at `59edabaa430e24f05f9398d9cd33019eaa60d7f4`.
That is historical evidence, not a strict Clippy result for either run here.

### Fresh-worker walkthrough: 2026-09-29

A fresh Codex worker started from the P1.2.2 issue, readiness comment,
[anchors](anchors-v2.md), canonical rules and repository instructions, without
reading PR review history. It used the existing documentation claim and
[PR](https://github.com/phaabe/live.moafunk.de/pull/452), with the coordinator
retaining sole ownership of these files.

Source: `e0b7de2bb7e090e8832ddfae5ad81d8424254674`, in a separate detached
checkout. `git status --short` was empty before installing dependencies and
after verification. Only these three documentation files differ from the
recorded `e319efe17e376e5a0cdaa26b8ebefe10d4114418` baseline; application and
test sources match. The worker used the exact commands and directories in
the reproduction table above.

Host: macOS arm64. Python 3.13.7; Node 26.3.0; npm 11.16.0; Vitest 1.6.1;
Cargo 1.98.0. Node still differs from CI's Node 20.

| Repeated check | Result |
| --- | --- |
| Locked frontend install | Exit 0; 321 packages. |
| Guard tests | Exit 0; 42 passed. |
| Workflow tests | Exit 0; 12 passed, zero failed or skipped. |
| Frontend tests | Exit 0; 23 files, 168 tests passed. |
| Frontend lint and types | Both exit 0. |
| Rust formatting | Exit 0. |
| Backend tests, strict Clippy and frontend build | Not rerun; do not attribute the earlier backend results to this walkthrough. |
| Media harness and devices | Not run. |

Anchor corrections found by the worker, at that source:

- O1.2.4's historical statement that workflows lack Rust tests and Clippy is
  obsolete. `epic-ci.yml` runs formatting, tests and Clippy at lines 53, 57 and
  61. The historical anchor file remains a baseline, not current code.
- Frontend tests also live in `frontend/src/admin/composables/__tests__/`.
- Frontend workflow lint/test locations moved to lines 95/98; integration
  frontend checks are at `epic-ci.yml` lines 88–100.
- Canonical rules section 0 selects `dev/312-interim`, overriding the issue's
  historical integration-branch wording.

Outcome: the fresh worker reproduced the focused baseline without production
access or review-history guidance. This supplies P1.2.5 walkthrough evidence
for the P1.2.2 documentation/test-inventory leaf. It does not test a new runtime
feature, executable media isolation or device behavior.

## Focused tests and fixture boundaries

| Change area | Focused command (inside its package) | Existing isolation |
| --- | --- | --- |
| Stream status policy | `npm test -- --run tests/streamDetector.test.ts src/__tests__/streamDetector.test.ts` | Both suites stub network responses; keep both directories in scope. |
| Upload and bitrate decisions | `npm test -- --run tests/useUploadHealth.test.ts tests/useAutoBitrate.test.ts` | Controlled counters and fake timers; no real upload needed. |
| Show timing and refresh | `npm test -- --run src/admin/composables/__tests__/useShowPhase.test.ts src/admin/composables/__tests__/useMetadataRefresh.test.ts` | Fake clocks and controlled responses; restore timers after each test. |
| Audio sample-rate selection | `npm test -- --run tests/useAudioCapture.test.ts` | Audio-track stubs, including 96 kHz and missing settings; not hardware evidence. |
| Broadcast admission and occurrences | `cargo +1.98.0 test --locked broadcast_tests` | In-memory SQLite, temporary files, loopback endpoints, disabled bot and a `sleep` child for producer contention. |
| Stream process lifecycle | `cargo +1.98.0 test --locked stream_bridge::tests` | Temporary recording files and controlled child processes; inspect the selected cases before adding media faults. |

The broadcast fixture constructs its configuration directly instead of loading
operator defaults. Its WebSocket server binds `127.0.0.1:0`. Storage presigning
uses a loopback dead end; this does not test object-store retries or durability.
The two ignored real-R2 tests in storage and recording are outside this
baseline. They require operator credentials; do not enable them. No credentials
or private programme audio belong in fixtures.

Add fakes only for a concrete missing assertion: injected clock/jitter for
timed policy; a supervised child with controlled exit for lifecycle faults; a
loopback object-store fake for partial writes and retry results. Generate short
synthetic tones or silence for codec tests and record their hashes and tool
versions. Do not infer media decode or device behavior from policy tests.
New fixture files need a recorded file claim and agreement from their lane
owner before editing.

## Worker handoff checklist

1. Read the issue, readiness comment, current epic assignments and canonical
   rules. Confirm the exact leaf is Ready for your executor and its prerequisites
   have evidence. Check pause and quota gates before GitHub operations.
2. Claim leaf IDs, exact files, branch and executor on the issue. Record Status
   and Activation separately. Check other open claims; resolve overlap before
   editing. One worker owns each shared integration file.
3. Create a separate worktree from `origin/dev/312-interim` while section 0 of
   the rules applies. Recheck [anchors](anchors-v2.md) at that commit; historical
   line numbers are search hints, not current source locations.
4. Refresh the worktree's GitNexus index. Query the relevant flow, inspect its
   symbols and run upstream impact before editing; report callers, processes
   and risk. Check cross-repo impact when the repository belongs to a group.
5. For a bug, first reproduce it with a failing regression test. Implement the
   smallest change inside the file claim. Hand shared-file wiring to its owner.
6. Run focused tests, applicable package checks and `gitnexus detect-changes`.
   Record exact commands, counts, failures, source/artifact and configuration
   identity. Link device or activation evidence separately.
7. Open a draft using the PR template and full issue URLs, targeting the active
   integration branch. Mark it ready only after verification. Record commit,
   tests, outcome and unresolved gates on the issue. Require the counterpart's
   unedited current-head verdict and green checks before merge.

Walkthrough acceptance: a fresh worker must follow one Ready Wave 0 leaf using
its issue and anchors without review-history archaeology. Record the chosen
leaf, claim, anchor corrections, focused commands, resulting PR and outcome.
The walkthrough and pair of non-overlapping claims are recorded above.
Executable P1.2.3 verification remains open in the ops lane.
