//! The recording pipeline with real ffmpeg, real handlers, SQLite and the
//! fake R2: a live show from its WebM chunks to the archive mp3, the finalize
//! merge with its checkpoint resume, and what an R2 or ffmpeg failure leaves.
//! Needs ffmpeg and ffprobe, as the backend does.

use super::*;
use crate::broadcast_tests::{
    add_user, connect_producer, headers, serve_stream_socket_with, state_with, Producer,
};
use crate::fake_r2::FakeR2;
use crate::recording_finalizer::FINALIZE_GRACE;
use axum::{
    body::to_bytes, extract::Path as UrlPath, http::StatusCode, routing::get, Json, Router,
};
use futures_util::{SinkExt, StreamExt};
use handlers::recording::{self, AddMarkerRequest, StartRecordingRequest};
use serde_json::{json, Value};
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::time::Duration;
use tokio_tungstenite::tungstenite::{client::IntoClientRequest, Message as WsMessage};

/// Long enough to pass the "essentially empty" check, so the show is published.
const SHOW_SECS: u32 = 65;
const VERSION: &str = "2026-06-01T20-00-00";
const TRACK_KEY: &str = "artists/1/track1/song.mp3";

/// A 440 Hz tone of `secs` seconds from ffmpeg, encoded with `codec` into
/// the container `format`.
pub(crate) async fn tone(secs: u32, codec: &[&str], format: &str) -> Vec<u8> {
    let output = tokio::process::Command::new("ffmpeg")
        .args(["-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i"])
        .arg(format!(
            "sine=frequency=440:sample_rate=48000:duration={secs}"
        ))
        .args(["-ac", "2"])
        .args(codec)
        .args(["-f", format, "pipe:1"])
        .output()
        .await
        .expect("run ffmpeg");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    output.stdout
}

/// The length of `data` in seconds, as ffprobe reads it.
pub(crate) async fn seconds(data: &[u8]) -> f64 {
    let file = tempfile::NamedTempFile::new().unwrap();
    tokio::fs::write(file.path(), data).await.unwrap();
    audio::get_duration(file.path()).await.unwrap() as f64 / 1000.0
}

async fn add_show(state: &Arc<AppState>, id: i64) {
    sqlx::query("INSERT INTO shows (id, title, date) VALUES (?, 'Night Shift', '2026-06-01')")
        .bind(id)
        .execute(&state.db)
        .await
        .unwrap();
}

async fn row(state: &Arc<AppState>, id: i64, version: &str) -> Option<models::RecordingVersion> {
    db::get_recording_version(&state.db, id, version)
        .await
        .unwrap()
}

/// Recording files and segment dirs left in `dir`.
fn leftovers(dir: &Path) -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(dir)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|name| name.starts_with("recording_"))
        .collect();
    names.sort();
    names
}

async fn json_body(response: axum::response::Response) -> Value {
    serde_json::from_slice(&to_bytes(response.into_body(), 1024 * 1024).await.unwrap()).unwrap()
}

// ── Live shows: the stream WebSocket, the recorder and the stop ────────────

/// The real stream WebSocket with a fake R2, an admin and show `id`.
async fn live_setup(id: i64) -> (Arc<AppState>, tempfile::TempDir, SocketAddr, FakeR2, i64) {
    let (r2, endpoint) = FakeR2::serve().await;
    let (state, temp, addr) = serve_stream_socket_with(json!({
        "stream_output": "icecast", "icecast_url": "pipe:1", "icecast_test_url": "pipe:1",
        "r2_endpoint": endpoint,
    }))
    .await;
    let admin = add_user(&state, "admin", "admin").await;
    add_show(&state, id).await;
    (state, temp, addr, r2, admin)
}

/// Sends `secs` seconds of WebM/Opus in chunks, as the panel's recorder does.
async fn send_audio(producer: &mut Producer, secs: u32) {
    let webm = tone(secs, &["-c:a", "libopus", "-b:a", "32k"], "webm").await;
    for chunk in webm.chunks(16 * 1024) {
        producer
            .send(WsMessage::Binary(chunk.to_vec()))
            .await
            .unwrap();
    }
}

/// Waits until the server has taken every chunk sent so far: it answers a
/// ping only after the frames before it.
async fn received(producer: &mut Producer) {
    producer
        .send(WsMessage::Text("ping:1".into()))
        .await
        .unwrap();
    let pong = async {
        while let Some(Ok(message)) = producer.next().await {
            if matches!(message, WsMessage::Text(ref text) if text.starts_with("pong:")) {
                return;
            }
        }
        panic!("socket closed before the pong");
    };
    tokio::time::timeout(Duration::from_secs(30), pong)
        .await
        .expect("no pong");
}

/// Ends the episode as the panel's Stop does. The server closes the socket
/// after the finalize, so this returns once the recording is stored.
async fn stop(mut producer: Producer) {
    producer.send(WsMessage::Text("stop".into())).await.unwrap();
    let drained = async { while let Some(Ok(_)) = producer.next().await {} };
    tokio::time::timeout(Duration::from_secs(120), drained)
        .await
        .expect("server kept the socket open after stop");
}

/// The version and file of the running recording.
async fn session(state: &Arc<AppState>) -> (String, PathBuf) {
    let manager = state.recording_manager.lock().await;
    (
        manager.get_status().version.unwrap(),
        manager.get_temp_file_path().unwrap(),
    )
}

/// The main path: a live show sends WebM/Opus chunks and stops. The real
/// recorder, join, R2 upload and mp3 transcode leave the raw recording, its
/// markers, a row and the archive mp3, and nothing on disk.
#[tokio::test]
async fn a_stopped_live_show_lands_in_r2_and_the_shows_archive() {
    let (state, temp, addr, r2, admin) = live_setup(31).await;
    let mut producer = connect_producer(&state, addr, admin, "show_id=31").await;
    let (version, _) = session(&state).await;
    send_audio(&mut producer, SHOW_SECS).await;

    // The panel marks a track while the show runs.
    let marker = recording_marker_handler(
        State(state.clone()),
        headers(&state, admin).await,
        Json(AddMarkerRequest {
            artist_id: 1,
            track_type: "track1".to_string(),
            track_key: TRACK_KEY.to_string(),
            duration_ms: 3000,
            offset_ms: 2000,
            volume: 100,
        }),
    )
    .await
    .into_response();
    assert_eq!(marker.status(), StatusCode::OK);
    let status = json_body(
        recording_status_handler(State(state.clone()))
            .await
            .into_response(),
    )
    .await;
    assert_eq!(status["active"], true);
    assert_eq!(status["show_id"], 31);
    assert_eq!(status["marker_count"], 1);

    stop(producer).await;

    let row = row(&state, 31, &version).await.expect("a recording row");
    assert_eq!(row.status, "raw", "{:?}", row.error_message);
    assert_eq!(row.marker_count, 1);
    let ms = row.duration_ms.unwrap();
    assert!((64_000..=66_500).contains(&ms), "duration {ms} ms");

    let raw = r2
        .object(&state, &format!("recordings/31/{version}/raw.webm"))
        .expect("the raw recording in R2");
    assert!((seconds(&raw).await - SHOW_SECS as f64).abs() < 1.5);
    let markers: Value = serde_json::from_slice(
        &r2.object(&state, &format!("recordings/31/{version}/markers.json"))
            .unwrap(),
    )
    .unwrap();
    assert_eq!(markers[0]["track_key"], TRACK_KEY);
    assert_eq!(markers[0]["offset_ms"], 2000);

    let archive_key = row.archive_key.expect("the archive key on the row");
    assert!(archive_key.starts_with("shows/"), "{archive_key}");
    let mp3 = r2
        .object_in(&state.config.r2_shows_bucket_name, &archive_key)
        .expect("the archive mp3");
    assert!(mp3.starts_with(b"ID3"), "an mp3 file");
    assert!((seconds(&mp3).await - SHOW_SECS as f64).abs() < 1.5);

    assert_eq!(leftovers(temp.path()), Vec::<String>::new());
}

/// The API restarts during a live show (ticket 1098): the old process left
/// its recorder's segments and the empty file of the session start. Boot
/// recovery leaves them to the producer, which reconnects and goes on in the
/// same version. Stop then saves one recording with the audio from before
/// and after the restart.
#[tokio::test]
async fn an_api_restart_mid_show_keeps_one_recording() {
    const BEFORE_SECS: u32 = 30;
    let (state, temp, addr, r2, admin) = live_setup(34).await;
    // What the killed recorder wrote, as it writes it.
    let artifact = temp.path().join(format!("recording_34_{VERSION}.webm"));
    let seg_dir = artifact.with_extension("segs");
    std::fs::create_dir(&seg_dir).unwrap();
    let status = tokio::process::Command::new("ffmpeg")
        .args(["-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i"])
        .arg(format!(
            "sine=frequency=440:sample_rate=48000:duration={BEFORE_SECS}"
        ))
        .args(["-ac", "2", "-c:a", "aac", "-b:a", "192k"])
        .args(["-f", "segment", "-segment_time", "10"])
        .args(["-segment_format", "mpegts", "-reset_timestamps", "1"])
        .arg(seg_dir.join("seg_%05d.ts"))
        .status()
        .await
        .unwrap();
    assert!(status.success());
    std::fs::write(&artifact, b"").unwrap();

    let counts = recording::recover_once(&state, temp.path(), FINALIZE_GRACE).await;
    assert_eq!(counts.deferred, 1);

    let mut producer = connect_producer(&state, addr, admin, "show_id=34").await;
    assert_eq!(
        session(&state).await.0,
        VERSION,
        "the producer goes on in it"
    );
    send_audio(&mut producer, SHOW_SECS).await;
    received(&mut producer).await;
    stop(producer).await;

    let rows: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM recording_versions WHERE show_id = 34")
            .fetch_one(&state.db)
            .await
            .unwrap();
    assert_eq!(rows, 1, "one recording");
    let row = row(&state, 34, VERSION).await.expect("the cut-off version");
    assert_eq!(row.status, "raw", "{:?}", row.error_message);
    let total = (BEFORE_SECS + SHOW_SECS) as f64;
    let mp3 = r2
        .object_in(
            &state.config.r2_shows_bucket_name,
            &row.archive_key.expect("published"),
        )
        .unwrap();
    assert!(
        (seconds(&mp3).await - total).abs() < 2.0,
        "the archive has the audio from before and after the restart"
    );
    assert_eq!(leftovers(temp.path()), Vec::<String>::new());

    // The pass after the grace finds nothing left to do.
    let counts = recording::recover_once(&state, temp.path(), Duration::ZERO).await;
    assert_eq!(counts, Default::default());
}

/// The join ffmpeg fails at stop (a directory blocks its output file). The
/// finalize writes no row, keeps the segments and stores the markers and a
/// finalize note that no audio was lost. The next boot joins the segments
/// again and, with that note, publishes the show.
#[tokio::test]
async fn a_failed_join_keeps_the_segments_and_the_next_boot_publishes_them() {
    let (state, temp, addr, r2, admin) = live_setup(32).await;
    let mut producer = connect_producer(&state, addr, admin, "show_id=32").await;
    let (version, artifact) = session(&state).await;
    send_audio(&mut producer, SHOW_SECS).await;
    let _ = tokio::fs::remove_file(&artifact).await;
    tokio::fs::create_dir(&artifact).await.unwrap();

    stop(producer).await;

    assert!(row(&state, 32, &version).await.is_none(), "no row");
    let prefix = format!("recordings/32/{version}/");
    let note: Value = serde_json::from_slice(
        &r2.object(&state, &format!("{prefix}finalize.json"))
            .unwrap(),
    )
    .unwrap();
    assert_eq!(note, json!({ "write_failure": null }));
    assert_eq!(
        r2.keys_in(&state.config.r2_bucket_name, &prefix),
        [
            format!("{prefix}finalize.json"),
            format!("{prefix}markers.json")
        ],
        "no raw upload"
    );
    let seg_dir = artifact.with_extension("segs");
    let segments = std::fs::read_dir(&seg_dir)
        .unwrap()
        .filter(|entry| stream_bridge::is_segment_file(&entry.as_ref().unwrap().path()))
        .count();
    assert!(segments >= 6, "{segments} segments kept");

    // The disk is fixed and the server restarts.
    tokio::fs::remove_dir(&artifact).await.unwrap();
    recording::recover_orphaned_recordings(&state, temp.path()).await;

    let row = row(&state, 32, &version)
        .await
        .expect("recovery writes the row");
    assert_eq!(row.status, "raw", "{:?}", row.error_message);
    let mp3 = r2
        .object_in(
            &state.config.r2_shows_bucket_name,
            &row.archive_key.expect("published"),
        )
        .unwrap();
    assert!((seconds(&mp3).await - SHOW_SECS as f64).abs() < 1.5);
    assert_eq!(leftovers(temp.path()), Vec::<String>::new());
}

/// R2 refuses writes when the show is stopped. The joined file stays on disk
/// and no row is written. R2 cannot take a finalize note either, so the next
/// boot uploads the file as possibly incomplete: failed, not published. The
/// admin's re-export then publishes it, stores the archive key and clears
/// the failure.
#[tokio::test]
async fn with_r2_down_at_stop_the_recording_waits_for_recovery_and_re_export() {
    let (state, temp, addr, r2, admin) = live_setup(33).await;
    // The show streams unrecorded; the admin starts the recording by hand.
    let mut producer = connect_producer(&state, addr, admin, "show_id=33&record=false").await;
    let start = recording_start_handler(
        State(state.clone()),
        headers(&state, admin).await,
        Json(StartRecordingRequest { show_id: 33 }),
    )
    .await
    .into_response();
    assert_eq!(start.status(), StatusCode::OK);
    let (version, artifact) = session(&state).await;
    send_audio(&mut producer, SHOW_SECS).await;
    received(&mut producer).await;

    r2.refuse_writes(true);
    let stopped = recording_stop_handler(State(state.clone()), headers(&state, admin).await)
        .await
        .into_response();

    assert_eq!(stopped.status(), StatusCode::INTERNAL_SERVER_ERROR);
    assert!(row(&state, 33, &version).await.is_none(), "no row");
    let size = std::fs::metadata(&artifact)
        .expect("the joined file stays")
        .len();
    assert!(size > 0);
    assert!(!artifact.with_extension("segs").exists(), "the join worked");
    assert!(r2
        .keys_in(&state.config.r2_bucket_name, "recordings/33/")
        .is_empty());
    stop(producer).await;

    // R2 is back and the server restarts.
    r2.refuse_writes(false);
    recording::recover_orphaned_recordings(&state, temp.path()).await;

    let recovered = row(&state, 33, &version)
        .await
        .expect("recovery writes the row");
    assert_eq!(recovered.status, "failed");
    assert!(
        recovered
            .error_message
            .as_deref()
            .unwrap()
            .contains("without a finalize note"),
        "{:?}",
        recovered.error_message
    );
    assert_eq!(
        recovered.archive_key, None,
        "a failed recording is not published"
    );
    let raw_key = format!("recordings/33/{version}/raw.webm");
    assert_eq!(r2.object(&state, &raw_key).unwrap().len() as u64, size);
    assert_eq!(leftovers(temp.path()), Vec::<String>::new());

    let reexport = reexport_recording_handler(
        State(state.clone()),
        headers(&state, admin).await,
        UrlPath(33),
        None,
    )
    .await
    .into_response();

    assert_eq!(reexport.status(), StatusCode::OK);
    let archive_key = json_body(reexport).await["archive_key"]
        .as_str()
        .unwrap()
        .to_string();
    let row = row(&state, 33, &version).await.unwrap();
    assert_eq!(row.status, "raw");
    assert_eq!(row.archive_key.as_deref(), Some(archive_key.as_str()));
    let mp3 = r2
        .object_in(&state.config.r2_shows_bucket_name, &archive_key)
        .unwrap();
    assert!((seconds(&mp3).await - SHOW_SECS as f64).abs() < 1.5);
}

// ── Finalize: the merge of the raw recording with its tracks ────────────────

/// The real finalize WebSocket with a fake R2, an admin, show `id` and a
/// row for its recording.
async fn finalize_setup(id: i64) -> (Arc<AppState>, tempfile::TempDir, SocketAddr, FakeR2, i64) {
    let (r2, endpoint) = FakeR2::serve().await;
    let (state, temp) = state_with(json!({ "r2_endpoint": endpoint })).await;
    let router = Router::new()
        .route(
            "/ws/recording/finalize",
            get(recording::finalize_ws_handler),
        )
        .with_state(state.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    let admin = add_user(&state, "admin", "admin").await;
    add_show(&state, id).await;
    db::create_recording_version(&state.db, id, VERSION, &raw_key(id), &markers_key(id), 1)
        .await
        .unwrap();
    // The finalize works in the server's own dir; an aborted earlier test
    // run may have left this show's files there.
    let _ = tokio::fs::remove_dir_all(session_dir(id)).await;
    (state, temp, addr, r2, admin)
}

fn raw_key(id: i64) -> String {
    format!("recordings/{id}/{VERSION}/raw.webm")
}

fn markers_key(id: i64) -> String {
    format!("recordings/{id}/{VERSION}/markers.json")
}

fn final_key(id: i64) -> String {
    format!("recordings/{id}/{VERSION}/final.mp3")
}

fn checkpoint_key(id: i64) -> String {
    format!("recordings/{id}/{VERSION}/checkpoint.json")
}

fn session_dir(id: i64) -> PathBuf {
    PathBuf::from("./data/finalize-temp").join(format!("{id}_{VERSION}"))
}

/// Stores an 8 s raw recording (MPEG-TS, as the recorder's join writes it),
/// `track` and a marker that plays the track for 3 s from second 2.
async fn seed(state: &Arc<AppState>, r2: &FakeR2, id: i64, track: &[u8]) {
    let raw = tone(8, &["-c:a", "aac", "-b:a", "128k"], "mpegts").await;
    r2.insert(state, &raw_key(id), &raw);
    r2.insert(state, TRACK_KEY, track);
    let markers = json!([{
        "offset_ms": 2000, "duration_ms": 3000, "artist_id": 1,
        "track_type": "track1", "track_key": TRACK_KEY, "volume": 100
    }]);
    r2.insert(state, &markers_key(id), markers.to_string().as_bytes());
}

/// Runs the finalize WebSocket to its end and returns its progress messages.
async fn finalize(state: &Arc<AppState>, addr: SocketAddr, admin: i64, id: i64) -> Vec<Value> {
    let mut request = format!("ws://{addr}/ws/recording/finalize?show_id={id}&version={VERSION}")
        .into_client_request()
        .unwrap();
    request.headers_mut().extend(headers(state, admin).await);
    let (mut socket, _) = tokio_tungstenite::connect_async(request).await.unwrap();
    let mut messages = Vec::new();
    let read = async {
        while let Some(Ok(message)) = socket.next().await {
            if let WsMessage::Text(text) = message {
                messages.push(serde_json::from_str(&text).unwrap());
            }
        }
    };
    tokio::time::timeout(Duration::from_secs(60), read)
        .await
        .expect("the finalize did not end");
    messages
}

fn checkpoint(state: &Arc<AppState>, r2: &FakeR2, id: i64) -> Value {
    serde_json::from_slice(&r2.object(state, &checkpoint_key(id)).expect("a checkpoint")).unwrap()
}

/// A finalize stops after the merge (R2 refuses the final.mp3 upload, as if
/// the server died before it). The row is failed and the checkpoint keeps
/// what is done. The next run resumes: it reuses the downloaded raw recording
/// and the merged mp3, uploads that mp3 and marks the row finalized.
#[tokio::test]
async fn an_interrupted_finalize_resumes_from_its_checkpoint() {
    let id = 41;
    let (state, _temp, addr, r2, admin) = finalize_setup(id).await;
    let track = tone(4, &["-c:a", "libmp3lame", "-b:a", "128k"], "mp3").await;
    seed(&state, &r2, id, &track).await;
    r2.refuse_writes_to(&state, Some(&final_key(id)));

    let first = finalize(&state, addr, admin, id).await;

    assert_eq!(first.last().unwrap()["phase"], "error", "{first:?}");
    let failed = row(&state, id, VERSION).await.unwrap();
    assert_eq!(failed.status, "failed");
    assert!(
        failed
            .error_message
            .as_deref()
            .unwrap()
            .contains("final.mp3"),
        "{:?}",
        failed.error_message
    );
    let saved = checkpoint(&state, &r2, id);
    assert_eq!(saved["phase"], "uploading");
    assert_eq!(saved["raw_downloaded"], true);
    assert_eq!(saved["merge_complete"], true);
    let merged = tokio::fs::read(session_dir(id).join("final.mp3"))
        .await
        .unwrap();
    // The raw recording is 8 s; the track ends at second 5, inside it.
    assert!((seconds(&merged).await - 8.0).abs() < 0.5);

    r2.refuse_writes_to(&state, None);
    let second = finalize(&state, addr, admin, id).await;

    assert_eq!(second[0]["resumed"], true, "{second:?}");
    let details: Vec<&str> = second
        .iter()
        .map(|message| message["detail"].as_str().unwrap())
        .collect();
    assert!(details.contains(&"Raw recording cached"), "{details:?}");
    assert!(details.contains(&"Merge cached"), "{details:?}");
    assert_eq!(second.last().unwrap()["phase"], "complete");
    assert_eq!(
        r2.object(&state, &final_key(id)).unwrap(),
        merged,
        "the first run's merge"
    );
    assert_eq!(r2.count(&state, "GET", &raw_key(id)), 1, "one raw download");
    assert_eq!(r2.count(&state, "GET", TRACK_KEY), 1, "one track download");
    let finalized = row(&state, id, VERSION).await.unwrap();
    assert_eq!(finalized.status, "finalized");
    assert_eq!(finalized.final_key, Some(final_key(id)));
    assert!(
        r2.object(&state, &checkpoint_key(id)).is_none(),
        "no checkpoint"
    );
    assert!(!session_dir(id).exists(), "no local files");
}

/// The merge ffmpeg fails (the track is not audio). The finalize ends with
/// an error, the row is failed with ffmpeg's reason, and the raw recording
/// stays in R2 and on disk, with a checkpoint at the merge.
#[tokio::test]
async fn a_failing_merge_marks_the_row_failed_and_keeps_the_recording() {
    let id = 43;
    let (state, _temp, addr, r2, admin) = finalize_setup(id).await;
    seed(&state, &r2, id, b"not audio").await;
    let raw = r2.object(&state, &raw_key(id)).unwrap();

    let messages = finalize(&state, addr, admin, id).await;

    let last = messages.last().unwrap();
    assert_eq!(last["phase"], "error");
    assert!(
        last["detail"]
            .as_str()
            .unwrap()
            .contains("FFmpeg merge failed"),
        "{last}"
    );
    let failed = row(&state, id, VERSION).await.unwrap();
    assert_eq!(failed.status, "failed");
    assert!(failed
        .error_message
        .as_deref()
        .unwrap()
        .contains("FFmpeg merge failed"));
    assert_eq!(r2.object(&state, &raw_key(id)).unwrap(), raw, "raw kept");
    assert!(r2.object(&state, &final_key(id)).is_none());
    let saved = checkpoint(&state, &r2, id);
    assert_eq!(saved["phase"], "merging");
    assert_eq!(saved["merge_complete"], false);
    assert!(session_dir(id).join("raw.webm").exists(), "kept to resume");

    tokio::fs::remove_dir_all(session_dir(id)).await.unwrap();
}
