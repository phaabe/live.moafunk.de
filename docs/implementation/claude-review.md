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
