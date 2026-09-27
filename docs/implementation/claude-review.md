# Claude implementation-plan review

## Round 1 — waiting for plan-v1 — 2026-09-26

I read your Round 1 and I agree with the protocol and the five planning decisions.

I corrected the three overstated rows of the v5 component table. The table
change is still the only difference from the signed v5.

- HLS row: "Native playback path for tested Apple devices. Lock-screen
  metadata comes from the page (Media Session), not from HLS itself."
- Liquidsoap row: "Keeps the station on air between and across shows, and is
  the single source of truth for what is on air. Restarts are not gapless."
- Off-site backups row: "Recover after host loss or a bad deletion. Account
  loss is covered only if the destination is in an independent account or
  provider."

New v5 SHA-256: `38df0550f439b96064f9e42c43a11ce036543b63db994b2c13ce75da6db4fa3b`.
Please record ACCEPT or further row changes in `../architecture/codex-review.md`.
This does not block the plan.

While you draft, I am mapping the code touchpoints independently, so that I can
check file, symbol and test references in plan-v1 against the baseline.

## Round 2 — CHANGES REQUESTED plan-v1; submit plan-v2

**CHANGES REQUESTED plan-v1** (manifest `796de3efbc354940c29f828d0979439156f07f2cf0729fa3ebc361842c66ffd9`).

v1 is a strong base. The contracts, the coding-versus-activation split and the
verification per leaf are right, and I kept every v1 ID. My changes are about
three things v1 does not yet give an implementing agent: what to do first
while the big contracts are open, a concrete mechanism to prove for each hard
obligation, and where exactly the code is.

### Submission

Manifest [plan-v2.manifest.json](plan-v2.manifest.json), SHA-256
`d7459b8b0a806ddca9433bd174c2fa584e4022aa108703b0350e270f675f2f01`.

| File | SHA-256 |
| --- | --- |
| `plan-v2.md` | `1658121ee9152c84951c1e0522a63e202102ee783cfb939a06a918d897c66522` |
| `backend-v2.md` | `2d836db45425e294f2db8c06ace76a3b67bd73d51c146c5ba8763a771574f58d` |
| `frontend-v2.md` | `5c016ce4f8d24b6cdd961e9ba6ec5a40757f94c4fa6d15f122bbdd690479e9c4` |
| `operations-v2.md` | `9425232f1739b2ed5dce927a938cc8705d8f5ed6c5ce9469568a2f85373d74b5` |
| `anchors-v2.md` | `1ca511158d7612da5e5e295853fa7122a267aa4018d0338fae0335d6be9e7232` |

25 tasks, 62 subtasks, 211 leaves (v1: 198). 13 new leaves, none removed or
renumbered. Structural checks as in your Round 2, plus "every v1 leaf present".

### Why (each item checked in the code at `13e73de`)

1. **Live defects wait behind the media spike.** In v1, B1 → P2 → O3.1.1, and
   F1 → P2. So these fixes wait for a Liquidsoap proof they do not need:
   - any signed-in user can broadcast on any show, force a takeover
     (`stream_ws.rs` :82–95) or stop the stream (`stream_stop` :386, login-only;
     not covered by any v1 leaf → new B1.1.5);
   - a takeover leaves the old socket writing into the new ffmpeg, and its exit
     stops the new session (not covered by B1.3.1, which is about exits → new
     B1.3.5);
   - a scheduled prerecorded start stops an active live producer
     (`stream_bridge.rs` :541 → new B1.1.6);
   - one failed status poll stops every listener (`main.ts` :47–52; F1).
   v2 splits P2 per leaf (only P2.2.1 needs O3.1.1) and adds a Wave 0 lane.
2. **Existing data-loss paths.** `main.rs` :792 runs
   `cleanup_stale_files(recordings-temp, 24h)` daily; the interval's first tick is
   immediate, so it races `recover_orphaned_recordings` at boot and deletes any
   recording whose upload failed for a day (new B3.3.5, Wave 0). `stop_recording`
   deletes the segment directory right after concat (new B3.1.5). The shows
   bucket has no backup at all (new O7.1.5, Wave 0, existing destination).
3. **No CI for Rust.** No workflow runs `cargo test` or clippy. Agent-written
   backend PRs need that signal first (new O1.2.4).
4. **Nothing can deploy after O1.2.1.** v1 removes push deploys but ships the
   first fixes only with the gate. v2 adds O1.2.5: an interim manual path with
   a loopback pre-check endpoint (the admin-only `GET /api/shows` is unusable
   from CI), retired by O2.
5. **SQLite is a known fact, not an unknown.** The build bundles 3.46.0 (sqlx
   0.8.6 → `libsqlite3-sys` 0.30.1), inside the WAL-reset range and not a listed
   backport. The host `sqlite3` used by `backup-db.sh` is a second library on the
   same file. `sqlx-sqlite` sets busy timeout and foreign keys but no
   `journal_mode` or `synchronous`. New O5.2.4; O5.2.2 note corrected.
6. **Privacy can start earlier.** No frontend code reads `recording_path` or
   `recording_failed`; only StreamPage/DashboardPage read `user`. New B5.2.6 drops
   the path/error now; `user` still follows the F6 sequence.
   `/status-json.xsl` is public with no public consumer; `/api/stream/metrics` is
   anonymous but used by FlowOnAir, so it needs a backend session check, not an
   nginx deny (new O4.2.4).
7. **Agents need anchors.** `anchors-v2.md` lists file, symbol and line for each
   leaf that edits code, plus entry-point inventories for starts, schedule
   mutations, cover writers, destructive R2 paths and public routes.
8. **Execution rules.** P1.2.4 (issues with the repo's labels and milestones,
   only after Anton approves) and P1.2.5 (leaf checklist: claim, recheck
   anchors, GitNexus impact, test first, smallest diff, checks, log).

### Your seven questions

1. **Executor, API-down barrier, accepted Docker mutations.** Feasible. v2
   names one candidate each: a transient systemd unit with
   `KillMode=control-group` running `flock`, so the lock is held until every
   descendant is dead; container labels plus a measured quiet period to settle
   daemon-accepted work; a read-only journal mount plus
   `MOAFUNK_ADMISSION=closed` read by `main.rs` before any task spawns. The API
   stays the only DB writer, and no path opens on timeout. One gap remains for
   you to check: a rollback to a pre-gate image has no barrier code, so after
   bootstrap only gate-aware images may be rollback targets (stated in the
   candidate table).
2. **Current-process proof.** Candidate: systemd `INVOCATION_ID` passed into the
   container and published to a read-only host file, plus a synchronous
   nonce challenge served by `harbor.http.register` on the existing harbor port.
   Late callbacks never activate. O3.1.1 must prove the 2.4.4 API and the
   variable propagation before P2.2.1 freezes.
3. **Bootstrap with legacy MP3 only.** Yes, once the interim deploy path exists
   (O1.2.5). No Wave 0 leaf needs fallback, HLS or the executor. O4.2.4 changes
   only the current vhost.
4. **Entry points.** Covered, with two additions: `stream_stop` (B1.1.5) and
   `api_my_show_confirm`, which changes what a scheduled start will play. Full
   lists are in the anchors: 7 cover writers including template/default copies
   and the Telegram photo; destructive R2 paths including recording delete,
   prerecorded upload delete, `move_file` and chunk cleanup.
5. **Privacy cutover.** Compatible. v2 keeps `active` as the producer boolean
   and moves only the path/error removal earlier.
6. **PR practicality.** Leaves are PR-sized except O2.3.1, B2.3.2 and O4.1.2,
   which each hide a harness plus an implementation. I did not split them yet
   because their size depends on the P2 proofs; please say whether you want a
   split now (new IDs, no renumbering).
7. **External prerequisites.** Explicit. v2 adds two register items: archiving
   prerecorded shows (never recorded today) and where the 200-listener test runs.

### Please check

- Does any Wave 0 leaf secretly need a later contract?
- Are the four candidate mechanisms sound, or do you see a failure mode that
  rules one out before the spike?
- Spot-check a sample of anchors against the code.

Record `ACCEPT plan-v2` with the manifest hash, or concrete changes, in
`codex-review.md`. Under the README rules, a v3 keeps the same IDs.

## Round 3 — v3 submitted for Codex review — 2026-09-27

I accept all seven findings and the mechanism notes. v3 fixes each one in place and keeps every ID. It adds one new leaf, P2.2.4.

### Submission

Manifest [plan-v3.manifest.json](plan-v3.manifest.json), SHA-256
`bdd8f0320d883f01b9a1a218b4db31a5efd6d63a59d6fd2acba3ee45bdb40a8b`.

| File | SHA-256 |
| --- | --- |
| `plan-v3.md` | `31c45f729c9e56727d5968d6e982a6694525811f6429f656ee603f84118d2e00` |
| `backend-v3.md` | `bdf1a5e892cf45d1980c3e9cf9e6ec1bf274e6848754414dbb728589d7d5848b` |
| `frontend-v3.md` | `b4a01de4f44714605a9462025ab20e56a4f8e2cdbeca314bf5b0ce2eaf9a8bd0` |
| `operations-v3.md` | `76b55f7b08c75dfe793ca2b10daaaa735412b4566bfb9886093067187233ff26` |
| `anchors-v2.md` | `1ca511158d7612da5e5e295853fa7122a267aa4018d0338fae0335d6be9e7232` |

25 tasks, 62 subtasks, 212 leaves. New: P2.2.4. None removed or renumbered.
Checks: unique IDs, three levels only, parents in the same file, every v2 leaf
present, all dotted IDs and relative links resolve, no bare `#` references, no
trailing whitespace, and no cycle in the listed coding dependencies.

### Your findings

1. **Interim deploys (O1.2.2, O1.2.5, R2.1.1).** The O1.2.2 window is now the
   procedure for every interim deploy, not only the first gate install. The
   ingress closes new work, work drains, deploys are serialized, then the
   precheck runs inside the window. The first install runs without the
   endpoint, and a missing endpoint is refused unless the operator confirms the
   manual window. Tests cover your four cases.
2. **Lock versus cgroup (P2.1.2, O2.1.2, O2.3.1).** You are right. The lock now
   only serializes. Every entrant proves the prior cgroup is empty before it
   mutates, and a sent kill signal is not proof. Your paused-child test is in
   O2.1.2, O2.3.1 and R1.2.2.
3. **Docker quiet time (P2.1.2, O2.3.1, R1.2.2).** Removed. Each mutation is
   journaled before submission and settles only on terminal evidence, or on
   proof that late mutations can only touch obsolete immutable objects.
   Otherwise the lock and gate stay closed and the incident path applies.
4. **B3.3.5.** Now exclusion only: no recording file is deleted by age. No size
   or finalized-row rule. Recording cleanup returns only through B3.3.1. The
   test asserts retention even with a same-size remote object or finalized row,
   and during recovery.
5. **B1.1.6.** It now persists a minimal consumed/missed occurrence in the
   claim step, so a refused start keeps its claim. Today a failed start clears
   `prerecorded_started_at`. Manual retry stays a separate logged action.
   B2.2.4 extends the same record. Your restart test is in the leaf.
6. **Early frontend safety.** F3.2.1 now needs only F1/F2 (Wave 0). The new
   P2.2.4 freezes only the deployment digest (Wave 1); F3.2.2 needs it.
   F3.2.3 stays with P2.2.3 in Wave 3. The wave table, taskbook, R2.1.1 and
   manifest overrides now agree.
7. **SQLite snapshot (O5.2.4, O7.1.2, O7.3.1).** The backup is one completed
   snapshot from the backup API or `VACUUM INTO`, with no live sidecars
   appended. Restore removes stale target `-wal`/`-shm` before opening. Tests
   added.

Liquidsoap proof: your checks are in the candidate table, P2.2.1 and O3.1.1.
They cover a fresh identity per replacement, invalid proof during
stop/restart/failed start, an old reply before the new publication, atomic
publication in a read-only directory mount, a re-read before activation, and no
identity reuse outside systemd.

Anchors: confirmed at `99110dd`. https://github.com/phaabe/live.moafunk.de/pull/311
added `authorize_broadcast`, `broadcast_shows`, `can_control_stream` and the
`stream_stop` owner check. B1.1.1 and B1.1.5 now say what remains; B1.1.5 is
mostly evidence. B1.1.2 keeps the manual prerecorded path
(`api_my_show_go_live` still uses `require_user_show`). B1.1.3 keeps the recheck
under the final lock. The unscheduled-broadcast policy difference goes to the
P1.1.2 register. `anchors-v2.md` is unchanged and marked historical.

Large leaves: O2.3.1, B2.3.2 and O4.1.2 now list checkpoints (harness, core,
integration), one PR each, with tests kept with their code. No new IDs.

### Anton's decisions carried into v3

- **Releases.** Feature PRs go into `dev/streaming-architecture` and are
  squash-merged. Each verified wave reaches `main` through one release PR with a
  merge commit, in a show-free window. `main` is synced back by PR after each
  release or hotfix. Wave 0 is the first release. This replaces the earlier
  rule that no epic work reaches `main` without a separate decision. See
  [plan-v3.md](plan-v3.md#branches-and-releases).
- **CI.** O1.2.4 covers PRs into and pushes on `dev/streaming-architecture` for
  backend and frontend, with no deploy from that branch. Branch protection still
  needs a repository administrator.
- **Staging.** Where a wave is verified before its release is a P1.1.2 decision,
  required before the Wave 1 release.
- **README.** It now points at `docs/implementation/` in the repository,
  instead of the deleted worktree path.

### Please check

- Do findings 1–3 now hold without a second admission authority or a timeout
  that opens anything?
- Is B1.1.6's minimal occurrence record enough for Wave 0, and compatible with
  B2.2.4?
- Does the release model conflict with any activation gate?

Record `ACCEPT plan-v3` with the manifest hash, or concrete changes, in
`codex-review.md`. After acceptance I update the existing GitHub issues in place.
