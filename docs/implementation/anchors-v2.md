# Baseline code anchors — v2

Owner: Claude. Baseline `13e73de53f02248feede47e1eced6ac2086e38dd`. Paths are relative to the repository root. Line numbers were read from this checkout and will drift: before editing, find the symbol again on the current branch and run GitNexus `impact` on it. "Checked" means Claude read the code for v2; everything else comes from the same survey and should be rechecked when claimed.

## Entry-point inventories

These lists answer "which paths must go through the new rule". A leaf that changes a rule must cover every row, or record why a row does not apply.

### Producer, capture and prerecorded starts (B1, B2.1.3, B4.2)

| Entry point | Anchor | Notes at baseline |
| --- | --- | --- |
| Browser live/rehearsal WebSocket | `GET /ws/stream` → `backend/src/handlers/stream_ws.rs::stream_ws_handler` :48, `handle_stream_socket` :168 | Login-only (checked). Query `StreamQuery` :17 (`force`, `show_id`, `test`); `force` works for any user :82–95 (checked); `test` drops `show_id` :99. |
| Automatic capture on live start | `handlers/recording.rs::ensure_recording_started` :775, called from `handle_stream_socket` | Checks only that the show exists. |
| Manual capture (admin) | `POST /api/recording/start` → `handlers/recording.rs::start_recording` :118 | `require_admin` :100. |
| Manual prerecorded go-live | `POST /api/my-show/go-live` → `handlers/api.rs::api_my_show_go_live` :5495 | Guarded by `require_user_show` :5069. |
| Scheduled prerecorded start | `main.rs` 30-second loop :827 → `scheduler.rs::check_prerecorded_show_start` :360 → `api.rs::start_prerecorded_show_stream` :5536 → `claim_prerecorded_start` :5525 → `start_claimed_prerecorded_stream` :5574 → `stream_bridge.rs::start_prerecorded_stream` :531 | Stops any active stream first :541 (checked). Uses a 4-hour presigned R2 URL. Never recorded. |
| Stop | `POST /api/stream/stop` → `stream_ws.rs::stream_stop` :386 | Login-only; runs `finalize_and_upload`. |
| Manual finalize | `GET /ws/recording/finalize` → `handlers/recording.rs::run_finalize` :1357 | Merges tracks into `final.mp3`. |
| Re-export | `POST /api/shows/:id/recordings/reexport`, CLI `reexport-archive` (`main.rs` :331) → `handlers/recording.rs::reexport_to_archive` :918 | |
| Rehearsal loopback | `GET /ws/stream-test` → `handlers/stream_test_ws.rs` | Does not touch Icecast; out of admission scope. |

### Schedule mutations (B2.2.3)

| Route | Handler (`backend/src/handlers/api.rs`) | Why it matters |
| --- | --- | --- |
| `POST /api/shows` | `api_create_show` :2991 | Adds a start; may copy a template (`copy_template_cover_to_show` :2440) or the default cover (`copy_default_cover_to_show` :2405). |
| `PUT /api/shows/:id` | `api_update_show` :3327 | Moves date/time, changes `stream_mode`. |
| `DELETE /api/shows/:id` | `api_delete_show` :3428 | Removes a reserved start. |
| `POST /api/my-show/confirm` | `api_my_show_confirm` :5463 | Confirms the prerecorded file, so it changes what a scheduled start will play (B4.1.1). |
| `POST/DELETE /api/shows/:id/host` | `api_show_assign_host` :3683 | Changes who may broadcast (B1.1.1). |
| `POST /api/shows/:id/artists`, `DELETE …/artists/:artist_id`, `POST /api/artists/:id/shows` | `api_show_assign_artist` :3529, `api_show_unassign_artist`, `api_assign_artist_to_show` :677 | Changes broadcast permission and public presenter (B1.1.1, B6.1.2). |

Show times are Berlin wall-clock text (`shows.date` + `start_time`/`end_time` "HH:MM", `db.rs` :115–163); `scheduler.rs::show_start_utc` :145 / `show_end_utc` :158 resolve them with `chrono_tz::Europe::Berlin` and return `None` in a DST gap. The container sets `TZ=Europe/Berlin` (`backend/docker-compose.prod.yml`).

### Public routes (B5, O4.2)

| Route | Anchor | Notes |
| --- | --- | --- |
| `GET /api/stream/status` | `main.rs` :724 → `stream_ws.rs::stream_status` :379 → `stream_bridge.rs::get_status` :506, `StreamStatus` :755 | Anonymous. Returns `active`, `user`, `recording`, `recording_path`, `recording_failed`. Readers of `user`: `StreamPage.vue` :82, `DashboardPage.vue` :90 (checked). No reader of `recording_path`/`recording_failed` (checked). |
| `GET /api/stream/metrics` | `main.rs` :725 | Anonymous; read by `FlowOnAir.vue` :344 via `streamApi.metrics` (checked). |
| `/status-json.xsl` | `backend/scripts/deploy_hetzner.sh` :454 | Public proxy to Icecast status; all monitoring uses `host.docker.internal:8010` directly (checked). |
| `/live.mp3`, `/test.mp3` | `deploy_hetzner.sh` :433–452 | `proxy_buffering off`, `proxy_read_timeout 3600s`. nginx vhost is generated only when `SETUP_NGINX=1`. |
| `/metrics` | `main.rs` :726 | Loopback-only by design; nginx returns 404. |

## B

| Leaf | Anchor |
| --- | --- |
| B1.1.1–B1.1.3 | `stream_ws.rs::stream_ws_handler` :48 (auth :56–71, conflict/force :82–95); `api.rs::require_user_show` :5069 is show-scoped and may be reused inside the helper, not as the helper. |
| B1.1.5 | `stream_ws.rs::stream_stop` :386. Callers: `FlowOnAir.vue` :391, `FlowStreaming.vue` :69 via `streamApi.stop`. |
| B1.1.6 | `stream_bridge.rs::start_prerecorded_stream` :531, stop at :541 (checked); monitor task :597–637. |
| B1.2.x, B1.3.x | `stream_bridge.rs::StreamState` :135 (`current_user`, `ffmpeg_stdin`, `ffmpeg_handle`, recording fields), `is_active` :190, `start_stream` :212, `stop_stream` :266, `write_chunk` :310; `stream_ws.rs::schedule_grace_finalize` :122, `FINALIZE_GRACE` :35. |
| B1.3.5 | `write_chunk` :310 has no session argument; takeover path is `stream_ws_handler` → `start_stream` :212 while the old `handle_stream_socket` loop continues. Admin reconnect: `useStreamSocket.ts` `connect(force, showId, test)` :139, auto-reconnect :208–221, `FlowOnAir.vue` :508 `connect(true, show.id)`. |
| B2.1.x | Background tasks in `main.rs`: :753 user expiry, :781 orphan recovery, :792 temp cleanup, :809 missing-recording check, :827 prerecorded start, :840 metrics poller, :846 Telegram bot, :849/:921 previews. The B2.3.5 barrier must run before :781–:921 spawn. |
| B2.2.4 | `scheduler.rs::check_prerecorded_show_start` :360, `check_missing_recordings` :197 (runs only when the Telegram bot is configured). |
| B3.1.x | Recorder: `stream_bridge.rs::start_recording` :363 (WebM → AAC 192k → 10-second TS segments in `./data/recordings-temp/recording_{show}_{ts}.segs/`), `stop_recording` :452, `concat_segments` :684. Sessions and markers: `backend/src/recording.rs::RecordingSession::new` :62, `RecordingManager` :182 (`start` :245, `stop` :268). Temp dir hard-coded in `main.rs` :271. |
| B3.1.5 | Segment directory removal inside `stop_recording` at `stream_bridge.rs` :493. |
| B3.2.x | `handlers/recording.rs::finalize_and_upload` :312, `upload_artifact_and_record` :385 (local delete :562 after a size check), `MIN_RECORDING_SECS` :270, `publish_stream_to_shows_archive` :1621; `storage.rs::upload_multipart` :55, `head_object_size` :155, `build_show_archive_key` :416; `db.rs::create_recording_version` :681; SoundCloud `soundcloud.rs::auto_upload_on_finalize` :473 (called from `handlers/recording.rs` :365 and :1300). |
| B3.3.1, B3.3.5 | `storage.rs::cleanup_stale_files` :225 (deletes anything older than `max_age`, checked); scheduled in `main.rs` :792–804 with a 24-hour interval whose first tick is immediate (checked); `handlers/recording.rs::recover_orphaned_recordings` :630, spawned at `main.rs` :781 (checked). |
| B4.x | `api.rs::api_my_show_confirm` :5463, `upload_prerecorded_to_r2` (put at :5655), `api_my_show_delete_upload` :5611; `shows.prerecorded_key`, `prerecorded_confirmed_at`, `prerecorded_started_at` (`db.rs` :203). |
| B5.2.x | See the public routes table. |
| B6 | Cover writers: `api.rs::api_upload_show_cover` :4802 → `storage.rs::upload_show_cover` :741; `api.rs::api_save_show_overlay` :1454; `copy_default_cover_to_show` :2405; `copy_template_cover_to_show` :2440; `schedule_cover_regeneration` :2309; `telegram.rs` :1703 (`storage::upload_show_cover`); `storage.rs::delete_show_cover` :807; template covers `storage.rs::upload_template_cover` :763 (`templates/{id}/cover.png`); station default `api.rs::ensure_default_cover_exists` :2362. |
| All B | Pool `main.rs` :243–246 (`max_connections(5)`, no explicit pragmas); `db.rs::run_migrations` :28–594 with `add_column_if_missing` :5; no migration version table; test DB helper `db.rs::mem_db` :939; tests are inline `#[cfg(test)]` modules, no `backend/tests/`. |

## F

| Leaf | Anchor |
| --- | --- |
| F1.2.x | `frontend/src/streamDetector.ts::checkBackendLive` :35 (returns `false` on error), `checkStreamStatus` :16; `frontend/src/main.ts` polling every 8 s :39–55, `destroyPlayer()` on live→not-live :47–52, `startVersionWatcher` :14; `frontend/src/config.ts` :4–22 (`statusUrl`, `icecast`, legacy `hls`/`flv`). |
| F1.1.x, F3.1.1 | `frontend/src/player.ts::initializePlayer` :13, `destroyPlayer` :71, `updateLiveStatus` :111, `play()` refuses unless live :128. No Media Session code exists. |
| F6.1.x | `StreamPage.vue` :82 (`status.user`), `DashboardPage.vue` :90; producer flow `pages/flow/FlowOnAir.vue` (:245, :253, :344, :391, :433, :476–481, :508), `FlowWaiting.vue` (:104 countdown, :190, :203), `FlowStreaming.vue` :58–142; `admin/api/index.ts` `streamApi` :904/:908, `recordingApi` :962, `hostFlowApi.goLive` :1054; routes `admin/router.ts` :109–190. |
| Tests | Vitest 1.x + jsdom (`frontend/vitest.config.ts`); tests in `frontend/tests/`, `frontend/src/__tests__/`, `frontend/src/admin/__tests__/`; CI `frontend.yml` runs lint :88, test :91 and build. |

## O

| Leaf | Anchor |
| --- | --- |
| O1.2.1, O1.2.5 | `.github/workflows/backend.yml`: push triggers :3–55 include `frontend/src/admin/**`; `build-and-push` :72; `deploy-hetzner` :127 runs on push, forces icecast output :148, writes stream URLs :260–273, syncs and restarts Liquidsoap when the `.liq` changed :323–356; `deploy-monitoring` :364 manual only. |
| O1.2.4 | No `cargo test`/`clippy` in any workflow (checked). |
| O2.1.1 | `backend/scripts/deploy_hetzner.sh`: needrestart/live-restore :226–230, compose `pull`/`down`/`up -d` :361–375, health check :390, nginx only with `SETUP_NGINX=1` :404–480, smoke tests :510. |
| O3.x | `docs/stream-rework/prod/moafunk.liq`: env :25–30, `%mp3(bitrate=256, samplerate=44100, stereo=true)` :37, `mksafe(input.harbor("test"))` :44 → `/test.mp3`, `mksafe(input.harbor("live"))` :57 → `/live.mp3`; no fallback, switch, callbacks or HTTP handlers. Harness: `docs/stream-rework/local-test-harness/` (`docker-compose.yml`, `docker-compose.no-nms.yml`, `liquidsoap/`, `icecast/`, `push-test-tone.sh`). Services: systemd `docker run --network host --memory 512m`, `savonet/liquidsoap:v2.4.4`, `moafunk/icecast-kh:kh22`, `/etc/moafunk/stream.env`. Producer push: `stream_bridge.rs` `output_args` :57–108 (`-c:a copy -f ogg` to `icecast://…:8005/live`, or libopus 128k). |
| O4.x | `docs/stream-rework/prod/icecast.xml` limits :21–26 (clients 350, sources 4), port 8010, `/live.mp3` max-listeners 350. |
| O5.2.x | SQLite 3.46.0 bundled: `sqlx` 0.8.6 feature `sqlite` (`backend/Cargo.toml` :26–30) → `libsqlite3-sys` 0.30.1 (`Cargo.lock` :2253) → `sqlite3.h` `SQLITE_VERSION "3.46.0"` (checked). Backups use the host `sqlite3` binary (`backend/scripts/backup/backup-db.sh`). |
| O6.x | `docs/stream-rework/prod/monitoring/`: Prometheus targets `icecast-exporter:9146`, `unheard-api:8000`, blackbox on `host.docker.internal:8010/status-json.xsl`; rules `rules/stream-alerts.yml`; `backend/scripts/deploy_monitoring.sh`. |
| O7 | Backups: `backend/scripts/backup/backup-r2.sh` :24 copies only the artists bucket (checked earlier); `backup-db.sh`; `.github/workflows/backup.yml` runs Sundays 03:00 UTC, on manual trigger and on `repository_dispatch` from `handlers/backup_trigger.rs`. Destructive R2 paths (checked): `api.rs::api_delete_show_recording` :4350 (recording + peaks), `api_my_show_delete_upload` :5611, upload chunk cleanup in `api_my_show_upload_finalize`, `upload_recording_chunked.rs::finalize_recording_upload`, `submit_chunked.rs::submit_file_chunk_finalize`; `storage.rs::move_file` :646 (copy + delete source, used for pending → final at :695); `storage.rs::delete_show_cover` :807; `handlers/recording.rs::delete_checkpoint` :1215; `instagram.rs::cleanup_temp_videos` :1184. Overwrites of fixed keys: `shows/{id}/cover.png` writers (B6 list), `templates/{id}/cover.png`, overlay and peaks writes in `api.rs` (:1503, :1670, :1879–1915, :4257). |
