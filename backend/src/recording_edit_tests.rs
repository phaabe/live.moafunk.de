//! The recording editor: the render filter, validation and peaks as pure
//! code; the handlers against SQLite, the fake R2 and real ffmpeg.

use super::*;
use crate::broadcast_tests::{add_user, headers, state_with};
use crate::fake_r2::FakeR2;
use crate::recording_tests::{seconds, tone};
use axum::body::to_bytes;
use serde_json::Value;

fn edit(start: f64, end: f64, gain_db: f64) -> RecordingEdit {
    RecordingEdit {
        start,
        end,
        gain_db,
        sections: vec![],
        snippets: vec![],
    }
}

fn section(id: &str, start: f64, end: f64, gain_db: f64) -> Section {
    Section {
        id: id.to_string(),
        start,
        end,
        gain_db,
    }
}

fn snippet(slot: i64, start: f64, length: f64) -> Snippet {
    Snippet {
        slot,
        start,
        length,
    }
}

// ── Render filter ───────────────────────────────────────────────────────────

#[test]
fn filter_matches_the_browser_preview() {
    assert_eq!(
        render_filter(&edit(60.0, 540.0, -3.0)),
        "atrim=start=60.000:end=540.000,asetpts=PTS-STARTPTS,volume=-3dB,\
         afade=t=in:d=0.5,afade=t=out:st=478.000:d=2,alimiter=limit=0.97:level=0"
    );

    let mut louder = edit(60.0, 540.0, 0.0);
    louder.sections = vec![section("a", 100.0, 200.0, 6.0)];
    let filter = render_filter(&louder);
    assert!(filter.contains(
        r"volume=0dB,volume=eval=frame:volume='1+0.99526*clip(min(t-40.000\,140.000-t)/0.3\,0\,1)',afade"
    ));

    louder.sections.push(section("b", 300.0, 310.5, -3.0));
    assert!(render_filter(&louder)
        .contains(r"\,1)+-0.29205*clip(min(t-240.000\,250.500-t)/0.3\,0\,1)'"));

    assert!(render_filter(&edit(0.0, 10.0, 2.5)).contains(",volume=2.5dB,"));
}

// ── Validation ──────────────────────────────────────────────────────────────

#[test]
fn a_full_edit_is_valid() {
    let mut e = edit(369.6, 6830.4, 2.5);
    e.sections = vec![
        section("a", 1800.0, 2100.0, 6.0),
        section("b", 2100.0, 2100.6, -12.0),
    ];
    e.snippets = vec![snippet(1, 2712.0, 30.0), snippet(3, 6800.4, 30.0)];
    assert_eq!(validate(&e, Some(6830.4)), Vec::<String>::new());
    // The stored duration may be a little short of the decoded file.
    assert_eq!(validate(&e, Some(6830.0)), Vec::<String>::new());
    assert_eq!(validate(&e, None), Vec::<String>::new());
}

#[test]
fn validation_names_every_broken_rule() {
    let problems = |e: &RecordingEdit, duration: Option<f64>| validate(e, duration);

    assert_eq!(
        problems(&edit(-1.0, 10.0, 0.0), None),
        ["start must be 0 or more"]
    );
    assert_eq!(
        problems(&edit(10.0, 5.0, 0.0), None),
        [
            "start must be before end",
            "The edit must be at least 2.5 s long"
        ]
    );
    assert_eq!(
        problems(&edit(0.0, 2.0, 0.0), None),
        ["The edit must be at least 2.5 s long"]
    );
    assert_eq!(
        problems(&edit(0.0, 100.0, 0.0), Some(90.0)),
        ["end must not be after the end of the recording (90.0 s)"]
    );
    assert_eq!(
        problems(&edit(0.0, 10.0, 12.5), None),
        ["gainDb must be between -12 and 12"]
    );

    let mut e = edit(10.0, 100.0, 0.0);
    e.sections = vec![
        section("short", 20.0, 20.5, 0.0),
        section("out", 5.0, 15.0, 0.0),
        section("loud", 30.0, 40.0, 13.0),
        section("x", 50.0, 60.0, 0.0),
        section("y", 59.0, 70.0, 0.0),
    ];
    assert_eq!(
        problems(&e, None),
        [
            "Section short must be at least 0.6 s long",
            "Section out must lie inside the edit",
            "Section loud: gainDb must be between -12 and 12",
            "Sections x and y overlap",
        ]
    );
    e.sections = (0..21)
        .map(|i| section(&i.to_string(), 10.0 + i as f64, 10.8 + i as f64, 0.0))
        .collect();
    assert_eq!(problems(&e, None), ["At most 20 volume sections"]);

    e.sections = vec![];
    e.snippets = vec![
        snippet(1, 20.0, 10.0),
        snippet(1, 30.0, 10.0),
        snippet(4, 40.0, 4.0),
        snippet(2, 80.0, 30.0),
    ];
    assert_eq!(
        problems(&e, None),
        [
            "At most 3 snippets",
            "Snippet slot 1 is used twice",
            "Snippet slot 4 must be 1, 2 or 3",
            "Snippet 4 must be 5 to 30 s long",
            "Snippet 2 must lie inside the edit",
        ]
    );
    e.snippets = vec![snippet(1, 5.0, 31.0)];
    assert_eq!(
        problems(&e, None),
        [
            "Snippet 1 must be 5 to 30 s long",
            "Snippet 1 must lie inside the edit"
        ]
    );
}

#[test]
fn the_edit_json_is_camel_case() {
    let json = r#"{"start":369.6,"end":6830.4,"gainDb":2.5,
        "sections":[{"id":"a","start":1800.0,"end":2100.0,"gainDb":6.0}],
        "snippets":[{"slot":1,"start":2712.0,"length":30.0}]}"#;
    let parsed: RecordingEdit = serde_json::from_str(json).unwrap();
    assert_eq!(parsed.sections[0].gain_db, 6.0);
    let back: Value = serde_json::to_value(&parsed).unwrap();
    assert_eq!(back, serde_json::from_str::<Value>(json).unwrap());
}

// ── Peaks ───────────────────────────────────────────────────────────────────

#[test]
fn peaks_keep_the_loudest_sample_per_bucket() {
    // 400 samples up to half scale, 400 with a full negative one, 200 quiet.
    let mut samples: Vec<i16> = (0..400).map(|i| if i == 7 { 16384 } else { 1 }).collect();
    samples.extend((0..400).map(|i| if i == 399 { i16::MIN } else { -5 }));
    samples.extend(std::iter::repeat_n(3277, 200));
    let bytes: Vec<u8> = samples.iter().flat_map(|s| s.to_le_bytes()).collect();

    let mut builder = PeaksBuilder::default();
    // Odd chunks split samples across feeds.
    for chunk in bytes.chunks(7) {
        builder.feed(chunk);
    }
    assert_eq!(
        builder.finish(),
        Peaks {
            duration: 0.125,
            per_second: 20,
            peaks: vec![0.5, 1.0, 0.1],
        }
    );
    assert_eq!(
        PeaksBuilder::default().finish(),
        Peaks {
            duration: 0.0,
            per_second: 20,
            peaks: vec![],
        }
    );
}

#[test]
fn peaks_sit_next_to_the_source_with_their_own_suffix() {
    assert_eq!(
        peaks_key("recordings/1/v1/final.mp3"),
        "recordings/1/v1/final.editor-peaks.json"
    );
    assert_eq!(peaks_key("shows/rec"), "shows/rec.editor-peaks.json");
}

// ── Handlers ────────────────────────────────────────────────────────────────

struct Setup {
    state: Arc<AppState>,
    _temp: tempfile::TempDir,
    r2: FakeR2,
    headers: HeaderMap,
    id: i64,
}

/// A host with an external show, a fake R2 and no recording yet.
async fn setup() -> Setup {
    let (r2, endpoint) = FakeR2::serve().await;
    let (state, temp) = state_with(json!({ "r2_endpoint": endpoint })).await;
    let host = add_user(&state, "host", "host").await;
    let id: i64 = sqlx::query_scalar(
        "INSERT INTO shows (title, date, show_type, host_user_id) \
         VALUES ('Night Shift', '2026-11-20', 'external', ?) RETURNING id",
    )
    .bind(host)
    .fetch_one(&state.db)
    .await
    .unwrap();
    let headers = headers(&state, host).await;
    Setup {
        state,
        _temp: temp,
        r2,
        headers,
        id,
    }
}

/// A finalized live recording: a `secs` long tone at `key`.
async fn add_finalized(s: &Setup, version: &str, key: &str, secs: u32) {
    sqlx::query(
        "INSERT INTO recording_versions (show_id, version, status, final_key, duration_ms) \
         VALUES (?, ?, 'finalized', ?, ?)",
    )
    .bind(s.id)
    .bind(version)
    .bind(key)
    .bind(i64::from(secs) * 1000)
    .execute(&s.state.db)
    .await
    .unwrap();
    let mp3 = tone(secs, &["-c:a", "libmp3lame", "-b:a", "128k"], "mp3").await;
    s.r2.insert(&s.state, key, &mp3);
}

async fn reply<T: IntoResponse>(result: Result<T>) -> (StatusCode, Value) {
    let response = match result {
        Ok(r) => r.into_response(),
        Err(e) => e.into_response(),
    };
    let status = response.status();
    let body = to_bytes(response.into_body(), 16 << 20).await.unwrap();
    (status, serde_json::from_slice(&body).unwrap_or(Value::Null))
}

async fn get_edit(s: &Setup) -> (StatusCode, Value) {
    reply(api_get_recording_edit(State(s.state.clone()), Path(s.id), s.headers.clone()).await).await
}

async fn put_edit(s: &Setup, body: &str) -> (StatusCode, Value) {
    reply(
        api_put_recording_edit(
            State(s.state.clone()),
            Path(s.id),
            s.headers.clone(),
            Bytes::from(body.to_string()),
        )
        .await,
    )
    .await
}

async fn apply(s: &Setup) -> (StatusCode, Value) {
    reply(api_apply_recording_edit(State(s.state.clone()), Path(s.id), s.headers.clone()).await)
        .await
}

async fn get_peaks(s: &Setup) -> (StatusCode, Value) {
    reply(api_get_recording_peaks(State(s.state.clone()), Path(s.id), s.headers.clone()).await)
        .await
}

/// The editor view once the render is over.
async fn rendered(s: &Setup) -> Value {
    for _ in 0..240 {
        let (_, view) = get_edit(s).await;
        if view["status"] != "rendering" {
            return view;
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
    panic!("the render did not finish");
}

async fn set_status(s: &Setup, status: &str) {
    sqlx::query("UPDATE recording_edits SET status = ? WHERE show_id = ?")
        .bind(status)
        .bind(s.id)
        .execute(&s.state.db)
        .await
        .unwrap();
}

async fn edited_key(s: &Setup) -> Option<String> {
    load_row(&s.state.db, s.id, Source::Show)
        .await
        .unwrap()
        .unwrap()
        .edited_key
}

async fn show(s: &Setup) -> models::Show {
    sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(s.id)
        .fetch_one(&s.state.db)
        .await
        .unwrap()
}

const EDIT: &str = r#"{"start":2.0,"end":14.0,"gainDb":2.5,
    "sections":[{"id":"a","start":5.0,"end":8.0,"gainDb":6.0}],
    "snippets":[{"slot":1,"start":3.0,"length":5.0}]}"#;

#[tokio::test]
async fn edit_round_trip_and_bad_input() {
    let s = setup().await;

    let (status, view) = get_edit(&s).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        view,
        json!({"file": "show", "filename": null, "source": null, "edit": null,
               "status": "none", "error": null, "edited_url": null, "rendered_at": null})
    );
    assert_eq!(put_edit(&s, EDIT).await.0, StatusCode::NOT_FOUND);

    // An own file: its length is not stored, so `end` is not checked against it.
    sqlx::query("UPDATE shows SET recording_key = 'shows/1/set.wav' WHERE id = ?")
        .bind(s.id)
        .execute(&s.state.db)
        .await
        .unwrap();
    let (_, view) = get_edit(&s).await;
    assert_eq!(view["status"], "none");
    assert_eq!(view["file"], "own");
    assert_eq!(view["filename"], "recording.mp3");
    assert!(view["source"]["url"]
        .as_str()
        .unwrap()
        .contains("shows/1/set.wav"));
    assert_eq!(view["source"]["duration"], Value::Null);

    let (status, saved) = put_edit(&s, EDIT).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(saved["status"], "draft");
    assert_eq!(saved["edit"], serde_json::from_str::<Value>(EDIT).unwrap());
    assert_eq!(get_edit(&s).await.1, saved);

    let (status, bad) = put_edit(
        &s,
        r#"{"start":10,"end":5,"gainDb":20,"sections":[],"snippets":[{"slot":4,"start":6,"length":5}]}"#,
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(
        bad["problems"],
        json!([
            "start must be before end",
            "The edit must be at least 2.5 s long",
            "gainDb must be between -12 and 12",
            "Snippet slot 4 must be 1, 2 or 3",
            "Snippet 4 must lie inside the edit",
        ])
    );
    assert!(bad["error"]
        .as_str()
        .unwrap()
        .starts_with("start must be before end; "));

    for body in ["{", r#"{"start":1}"#, r#"{"start":"x","end":5,"gainDb":0}"#] {
        let (status, bad) = put_edit(&s, body).await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
        assert!(bad["problems"][0]
            .as_str()
            .unwrap()
            .starts_with("Invalid edit JSON"));
    }
    // Bad input leaves the saved edit alone.
    assert_eq!(get_edit(&s).await.1, saved);

    // Another host may not edit this show.
    let other = add_user(&s.state, "other", "host").await;
    let other_headers = headers(&s.state, other).await;
    let result = api_get_recording_edit(State(s.state.clone()), Path(s.id), other_headers).await;
    assert_eq!(reply(result).await.0, StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn apply_renders_the_cut_and_keeps_the_last_good_file() {
    let s = setup().await;
    let source = format!("recordings/{}/v1/final.mp3", s.id);
    add_finalized(&s, "v1", &source, 20).await;

    // Nothing saved yet.
    assert_eq!(apply(&s).await.0, StatusCode::BAD_REQUEST);
    // The stored length (20 s) limits the end.
    let (status, bad) = put_edit(&s, r#"{"start":2,"end":21,"gainDb":0}"#).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(
        bad["problems"],
        json!(["end must not be after the end of the recording (20.0 s)"])
    );
    let (_, view) = put_edit(&s, EDIT).await;
    assert_eq!(view["source"]["duration"], 20.0);

    // A running render blocks a second one and edits.
    set_status(&s, "rendering").await;
    assert_eq!(apply(&s).await.0, StatusCode::CONFLICT);
    assert_eq!(put_edit(&s, EDIT).await.0, StatusCode::CONFLICT);
    set_status(&s, "draft").await;

    let (status, body) = apply(&s).await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body, json!({"status": "rendering"}));
    let view = rendered(&s).await;
    assert_eq!(view["status"], "done", "{view}");
    assert_eq!(view["error"], Value::Null);
    assert!(view["rendered_at"].is_string());
    let key = edited_key(&s).await.unwrap();
    assert!(
        key.starts_with(&format!("recordings/{}/edited/show-", s.id)),
        "{key}"
    );
    assert!(view["edited_url"].as_str().unwrap().contains(&key));
    let edited = s.r2.object(&s.state, &key).unwrap();
    let length = seconds(&edited).await;
    assert!((length - 12.0).abs() < 0.1, "edited file is {length} s");
    // The original is untouched.
    assert!(s.r2.object(&s.state, &source).is_some());

    let detail = reply(
        crate::handlers::api::api_show_detail(
            State(s.state.clone()),
            Path(s.id),
            s.headers.clone(),
        )
        .await,
    )
    .await
    .1;
    assert_eq!(detail["recording_edit_status"], "done");
    assert_eq!(detail["recording_edited_seconds"], 12.0);
    assert_eq!(detail["recording_edited_at"], view["rendered_at"]);
    assert!(detail["edited_recording_url"]
        .as_str()
        .unwrap()
        .contains(&key));

    let upload = upload_source(&s.state, &show(&s).await)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        (upload.file.key.as_str(), upload.file.filename.as_str()),
        (key.as_str(), "v1-edited.mp3")
    );
    assert_eq!((upload.source, upload.edited), (Source::Show, true));

    // A failed render keeps the last good file as the upload source.
    let original = s.r2.object(&s.state, &source).unwrap();
    s.r2.objects
        .lock()
        .unwrap()
        .remove(&format!("{}/{source}", s.state.config.r2_bucket_name));
    assert_eq!(apply(&s).await.0, StatusCode::ACCEPTED);
    let view = rendered(&s).await;
    assert_eq!(view["status"], "failed");
    assert!(view["error"]
        .as_str()
        .unwrap()
        .contains("Failed to download"));
    assert!(view["edited_url"].as_str().unwrap().contains(&key));
    assert_eq!(edited_key(&s).await.as_deref(), Some(key.as_str()));

    // The next good render replaces the old file and deletes it.
    s.r2.insert(&s.state, &source, &original);
    let old = format!("recordings/{}/edited/old.mp3", s.id);
    s.r2.insert(&s.state, &old, b"old");
    sqlx::query("UPDATE recording_edits SET edited_key = ? WHERE show_id = ?")
        .bind(&old)
        .bind(s.id)
        .execute(&s.state.db)
        .await
        .unwrap();
    assert_eq!(apply(&s).await.0, StatusCode::ACCEPTED);
    assert_eq!(rendered(&s).await["status"], "done");
    assert_ne!(edited_key(&s).await.as_deref(), Some(old.as_str()));
    // The render deletes the old file after it saved "done".
    for _ in 0..40 {
        if s.r2.object(&s.state, &old).is_none() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert!(s.r2.object(&s.state, &old).is_none());

    // A newer recording makes the edited file stale: upload the new recording.
    let newer = format!("recordings/{}/v2/final.mp3", s.id);
    add_finalized(&s, "v2", &newer, 5).await;
    let view = get_edit(&s).await.1;
    assert_eq!(view["status"], "draft");
    assert_eq!(view["edited_url"], Value::Null);
    let upload = upload_source(&s.state, &show(&s).await)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        (upload.file.key.as_str(), upload.file.filename.as_str()),
        (newer.as_str(), "v2.mp3")
    );
    assert_eq!((upload.source, upload.edited), (Source::Show, false));
}

#[tokio::test]
async fn peaks_are_computed_once_then_served_from_r2() {
    let s = setup().await;
    assert_eq!(get_peaks(&s).await.0, StatusCode::NOT_FOUND);

    // A streamed show: the archive mp3 sits in the shows bucket.
    let archive = "shows/external/2026-11-20-night-shift/night-shift.mp3";
    sqlx::query(
        "INSERT INTO recording_versions (show_id, version, status, archive_key) \
         VALUES (?, 'v1', 'raw', ?)",
    )
    .bind(s.id)
    .bind(archive)
    .execute(&s.state.db)
    .await
    .unwrap();
    let mp3 = tone(20, &["-c:a", "libmp3lame", "-b:a", "128k"], "mp3").await;
    let in_shows = format!("{}/{archive}", s.state.config.r2_shows_bucket_name);
    s.r2.objects.lock().unwrap().insert(in_shows.clone(), mp3);

    assert_eq!(
        get_peaks(&s).await,
        (StatusCode::ACCEPTED, json!({"status": "processing"}))
    );
    let peaks = poll_peaks(&s).await;
    assert_eq!(peaks.0, StatusCode::OK, "{}", peaks.1);
    let peaks = peaks.1;
    assert_eq!(peaks["per_second"], 20);
    let duration = peaks["duration"].as_f64().unwrap();
    assert!((duration - 20.0).abs() < 0.1, "{duration}");
    let values = peaks["peaks"].as_array().unwrap();
    assert!(values.len().abs_diff(400) <= 2, "{}", values.len());
    // The test tone peaks near 0.08 after the 8 kHz mono decode.
    assert!(values[100].as_f64().unwrap() > 0.05);
    let cached = "shows/external/2026-11-20-night-shift/night-shift.editor-peaks.json";
    assert!(s.r2.object(&s.state, cached).is_some());
    // Served from the cache, no new job.
    assert_eq!(get_peaks(&s).await.1, peaks);

    // A failed job answers 500 once; the next request tries again.
    s.r2.objects.lock().unwrap().clear();
    assert_eq!(get_peaks(&s).await.0, StatusCode::ACCEPTED);
    let (status, failed) = poll_peaks(&s).await;
    assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR);
    assert!(failed["error"]
        .as_str()
        .unwrap()
        .contains("Failed to download"));
    assert_eq!(get_peaks(&s).await.0, StatusCode::ACCEPTED);
}

/// Polls the peaks until they are no longer processing.
async fn poll_peaks(s: &Setup) -> (StatusCode, Value) {
    for _ in 0..240 {
        let reply = get_peaks(s).await;
        if reply.0 != StatusCode::ACCEPTED {
            return reply;
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
    panic!("the peaks job did not finish");
}

#[tokio::test]
async fn a_restart_frees_a_stuck_render() {
    let s = setup().await;
    sqlx::query(
        "INSERT INTO recording_edits (show_id, edit_json, status) VALUES (?, '{}', 'rendering')",
    )
    .bind(s.id)
    .execute(&s.state.db)
    .await
    .unwrap();
    crate::db::run_migrations(&s.state.db).await.unwrap();
    let row = load_row(&s.state.db, s.id, Source::Show)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(row.status, "failed");
    assert!(row.error_message.unwrap().contains("restarted"));
}

// ── Two files: the show recording and an own file ───────────────────────────

async fn put_source(s: &Setup, source: &str) -> (StatusCode, Value) {
    let body = serde_json::from_value(json!({ "source": source })).unwrap();
    reply(
        api_put_recording_source(
            State(s.state.clone()),
            Path(s.id),
            s.headers.clone(),
            Json(body),
        )
        .await,
    )
    .await
}

async fn detail(s: &Setup) -> Value {
    let result = crate::handlers::api::api_show_detail(
        State(s.state.clone()),
        Path(s.id),
        s.headers.clone(),
    )
    .await;
    let (status, body) = reply(result).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    body
}

/// Runs `query` with the show id bound once.
async fn sql(s: &Setup, query: &str) {
    sqlx::query(query)
        .bind(s.id)
        .execute(&s.state.db)
        .await
        .unwrap();
}

async fn upload(s: &Setup) -> (Source, bool, String, String) {
    let u = upload_source(&s.state, &show(s).await)
        .await
        .unwrap()
        .unwrap();
    (u.source, u.edited, u.file.key, u.file.filename)
}

/// A finalized take of 20 s, only in the database.
async fn add_take(s: &Setup) -> String {
    let take = format!("recordings/{}/v1/final.mp3", s.id);
    sqlx::query(
        "INSERT INTO recording_versions (show_id, version, status, final_key, duration_ms) \
         VALUES (?, 'v1', 'finalized', ?, 20000)",
    )
    .bind(s.id)
    .bind(&take)
    .execute(&s.state.db)
    .await
    .unwrap();
    take
}

const OWN_KEY: &str = "recordings/2026-11-20-night-shift.wav";
const OWN_EDITED: &str = "recordings/1/edited/own-20261120-120000.mp3";

/// The host picks one of the two files; each keeps its own edit, and
/// SoundCloud gets the picked one.
#[tokio::test]
async fn the_host_picks_one_of_two_files_and_each_keeps_its_edit() {
    let s = setup().await;
    // No file yet: the show recording is picked, and neither can be picked.
    assert_eq!(picked_source(&show(&s).await), Source::Show);
    assert_eq!(
        put_source(&s, "own").await,
        (
            StatusCode::BAD_REQUEST,
            json!({"error": "There is no own file."})
        )
    );
    assert_eq!(
        put_source(&s, "show").await.1["error"],
        "There is no show recording yet."
    );

    // A take: the show recording.
    let take = add_take(&s).await;
    assert_eq!(picked_source(&show(&s).await), Source::Show);
    assert_eq!(put_edit(&s, EDIT).await.0, StatusCode::OK);

    // An own file, not picked yet: the own file wins, as before.
    sqlx::query("UPDATE shows SET recording_key = ?, recording_filename = 'set.wav' WHERE id = ?")
        .bind(OWN_KEY)
        .bind(s.id)
        .execute(&s.state.db)
        .await
        .unwrap();
    assert_eq!(picked_source(&show(&s).await), Source::Own);
    let view = get_edit(&s).await.1;
    assert_eq!(
        (
            &view["file"],
            &view["filename"],
            &view["edit"],
            &view["status"]
        ),
        (
            &json!("own"),
            &json!("set.wav"),
            &Value::Null,
            &json!("none")
        )
    );
    // Its length is not known, so 30 s is fine.
    let own_edit = r#"{"start":1.0,"end":30.0,"gainDb":-2.0,"sections":[],"snippets":[]}"#;
    assert_eq!(put_edit(&s, own_edit).await.0, StatusCode::OK);
    assert_eq!(
        upload(&s).await,
        (Source::Own, false, OWN_KEY.into(), "set.wav".into())
    );

    // Back to the show recording: its edit was left alone.
    assert_eq!(
        put_source(&s, "show").await,
        (StatusCode::OK, json!({"source": "show"}))
    );
    assert_eq!(picked_source(&show(&s).await), Source::Show);
    let view = get_edit(&s).await.1;
    assert_eq!(
        (&view["file"], &view["filename"]),
        (&json!("show"), &json!("v1.mp3"))
    );
    assert_eq!(view["edit"], serde_json::from_str::<Value>(EDIT).unwrap());
    assert_eq!(
        upload(&s).await,
        (Source::Show, false, take.clone(), "v1.mp3".into())
    );

    // A render of the own file goes up only while the own file is picked.
    sql(
        &s,
        &format!(
            "UPDATE recording_edits SET status = 'done', edited_key = '{OWN_EDITED}', \
             source_key = '{OWN_KEY}', edited_seconds = 29.0 \
             WHERE show_id = ? AND source = 'own'"
        ),
    )
    .await;
    assert_eq!(upload(&s).await.2, take);
    assert_eq!(put_source(&s, "own").await.0, StatusCode::OK);
    assert_eq!(
        upload(&s).await,
        (
            Source::Own,
            true,
            OWN_EDITED.into(),
            "set-edited.mp3".into()
        )
    );
    let view = get_edit(&s).await.1;
    assert_eq!(
        view["edit"],
        serde_json::from_str::<Value>(own_edit).unwrap()
    );
    assert_eq!(view["status"], "done");
    let rows: Vec<(String, String)> = sqlx::query_as(
        "SELECT source, status FROM recording_edits WHERE show_id = ? ORDER BY source",
    )
    .bind(s.id)
    .fetch_all(&s.state.db)
    .await
    .unwrap();
    assert_eq!(
        rows,
        [
            ("own".into(), "done".into()),
            ("show".into(), "draft".into())
        ]
    );

    // Deleting the own file goes back to the show recording.
    let admin = add_user(&s.state, "admin", "admin").await;
    let result = crate::handlers::api::api_delete_show_recording(
        State(s.state.clone()),
        Path(s.id),
        headers(&s.state, admin).await,
    )
    .await;
    assert_eq!(reply(result).await.0, StatusCode::OK);
    let after = show(&s).await;
    assert_eq!((after.recording_key, after.recording_source), (None, None));
    assert_eq!(picked_source(&show(&s).await), Source::Show);
}

/// The show detail describes the picked file, its downloads and whether
/// SoundCloud still has it.
#[tokio::test]
async fn the_show_detail_says_whether_soundcloud_has_the_picked_file() {
    let s = setup().await;
    let take = add_take(&s).await;

    // No track yet.
    let d = detail(&s).await;
    assert_eq!(d["recording_source"], "show");
    assert_eq!(
        (
            &d["soundcloud_source"],
            &d["soundcloud_edited"],
            &d["soundcloud_current"]
        ),
        (&Value::Null, &json!(false), &Value::Null)
    );
    let download = d["recording_download_url"].as_str().unwrap();
    assert!(download.contains(&take), "{download}");
    assert!(
        download.contains("response-content-disposition=attachment%3B%20filename%3D%22v1.mp3%22"),
        "{download}"
    );
    assert_eq!(d["recording_original_download_url"], Value::Null);

    // A track from before the sent file was stored.
    sql(
        &s,
        "UPDATE shows SET soundcloud_track_id = '555' WHERE id = ?",
    )
    .await;
    assert_eq!(detail(&s).await["soundcloud_current"], Value::Null);

    // SoundCloud has the take.
    sqlx::query(
        "UPDATE shows SET soundcloud_source = 'show', soundcloud_source_key = ?, \
         soundcloud_edited = 0 WHERE id = ?",
    )
    .bind(&take)
    .bind(s.id)
    .execute(&s.state.db)
    .await
    .unwrap();
    let d = detail(&s).await;
    assert_eq!(
        (&d["soundcloud_source"], &d["soundcloud_current"]),
        (&json!("show"), &json!(true))
    );

    // The host picks an own file with an edit: SoundCloud has another file.
    sqlx::query(
        "UPDATE shows SET recording_key = ?, recording_filename = 'set.wav', \
         recording_source = 'own' WHERE id = ?",
    )
    .bind(OWN_KEY)
    .bind(s.id)
    .execute(&s.state.db)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO recording_edits (show_id, source, edit_json, status, edited_key, \
             source_key, edited_seconds, rendered_at) \
         VALUES (?, 'own', '{}', 'done', ?, ?, 29.0, '2026-11-20 12:00:00')",
    )
    .bind(s.id)
    .bind(OWN_EDITED)
    .bind(OWN_KEY)
    .execute(&s.state.db)
    .await
    .unwrap();
    let d = detail(&s).await;
    assert_eq!(d["recording_source"], "own");
    assert_eq!(d["soundcloud_current"], false);
    assert_eq!(d["recording_edit_status"], "done");
    assert_eq!(d["recording_edited_seconds"], 29.0);
    let edited = d["recording_download_url"].as_str().unwrap();
    assert!(edited.contains(OWN_EDITED), "{edited}");
    assert!(edited.contains("set-edited.mp3"), "{edited}");
    let original = d["recording_original_download_url"].as_str().unwrap();
    assert!(original.contains(OWN_KEY), "{original}");
    assert!(original.contains("filename%3D%22set.wav%22"), "{original}");

    // Uploaded again: SoundCloud has the edit of the own file.
    sqlx::query(
        "UPDATE shows SET soundcloud_source = 'own', soundcloud_source_key = ?, \
         soundcloud_edited = 1 WHERE id = ?",
    )
    .bind(OWN_EDITED)
    .bind(s.id)
    .execute(&s.state.db)
    .await
    .unwrap();
    let d = detail(&s).await;
    assert_eq!(
        (
            &d["soundcloud_source"],
            &d["soundcloud_edited"],
            &d["soundcloud_current"]
        ),
        (&json!("own"), &json!(true), &json!(true))
    );
}

/// A table from before the two files is rebuilt once: an edit of a show
/// with an own file was of that file, every other edit of the show recording.
#[tokio::test]
async fn an_old_edits_table_gets_the_file_of_each_edit() {
    let s = setup().await;
    let own: i64 = sqlx::query_scalar(
        "INSERT INTO shows (title, date, show_type, recording_key) \
         VALUES ('Own', '2026-11-21', 'external', 'recordings/own.mp3') RETURNING id",
    )
    .fetch_one(&s.state.db)
    .await
    .unwrap();
    for query in [
        "DROP TABLE recording_edits",
        "CREATE TABLE recording_edits (
            show_id INTEGER PRIMARY KEY REFERENCES shows(id) ON DELETE CASCADE,
            edit_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            error_message TEXT,
            edited_key TEXT,
            source_key TEXT,
            edited_seconds REAL,
            rendered_at TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )",
    ] {
        sqlx::query(query).execute(&s.state.db).await.unwrap();
    }
    sqlx::query(
        "INSERT INTO recording_edits (show_id, edit_json, status, edited_key, source_key) \
         VALUES (?, '{\"a\":1}', 'done', 'edited/a.mp3', 'final/a.mp3'), \
                (?, '{\"b\":2}', 'rendering', NULL, NULL)",
    )
    .bind(s.id)
    .bind(own)
    .execute(&s.state.db)
    .await
    .unwrap();

    // Twice: the second run changes nothing.
    for _ in 0..2 {
        crate::db::run_migrations(&s.state.db).await.unwrap();
    }
    let rows: Vec<(i64, String, String, String, Option<String>)> = sqlx::query_as(
        "SELECT show_id, source, edit_json, status, edited_key FROM recording_edits \
         ORDER BY show_id",
    )
    .fetch_all(&s.state.db)
    .await
    .unwrap();
    assert_eq!(
        rows,
        [
            (
                s.id,
                "show".into(),
                r#"{"a":1}"#.into(),
                "done".into(),
                Some("edited/a.mp3".into())
            ),
            // A stuck render is freed as before.
            (
                own,
                "own".into(),
                r#"{"b":2}"#.into(),
                "failed".into(),
                None
            ),
        ]
    );
    // Keyed by show and file now: a second edit of the same show fits.
    sqlx::query("INSERT INTO recording_edits (show_id, source, edit_json) VALUES (?, 'own', '{}')")
        .bind(s.id)
        .execute(&s.state.db)
        .await
        .unwrap();
}

#[test]
fn source_names() {
    for source in [Source::Show, Source::Own] {
        assert_eq!(Source::parse(source.as_str()), Some(source));
        assert_eq!(
            serde_json::to_value(source).unwrap(),
            json!(source.as_str())
        );
    }
    assert_eq!(Source::parse("other"), None);
}

// ── Snippet clips ───────────────────────────────────────────────────────────

#[test]
fn a_snippet_clip_has_the_volume_of_the_edit() {
    let mut e = edit(10.0, 100.0, -2.0);
    e.sections = vec![section("a", 40.0, 60.0, 6.0)];
    e.snippets = vec![snippet(1, 45.0, 10.0)];
    let f = snippet_filter(&e, &e.snippets[0]);
    assert!(f.starts_with("atrim=start=45.000:end=55.000,asetpts=PTS-STARTPTS,volume=-2dB,"));
    // The section counts from the snippet start: it began 5 s before it.
    assert!(f.contains("clip(min(t--5.000\\,15.000-t)"), "{f}");
    assert!(
        f.ends_with("afade=t=out:st=8.000:d=2,alimiter=limit=0.97:level=0"),
        "{f}"
    );
}

/// Streams and length of a media file, as ffprobe reads them.
async fn probe(data: &[u8]) -> Value {
    let file = tempfile::NamedTempFile::new().unwrap();
    tokio::fs::write(file.path(), data).await.unwrap();
    let out = tokio::process::Command::new("ffprobe")
        .args(["-v", "error", "-of", "json", "-show_entries"])
        .args(["stream=codec_type,width,height:format=duration"])
        .arg(file.path())
        .output()
        .await
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice(&out.stdout).unwrap()
}

#[tokio::test]
async fn the_snippets_of_the_picked_file_become_clips_in_slot_order() {
    let s = setup().await;
    assert!(
        post_snippets(&s.state, &show(&s).await)
            .await
            .unwrap()
            .is_none(),
        "no file"
    );
    add_finalized(&s, "v1", "recordings/1/final.mp3", 20).await;
    assert!(
        post_snippets(&s.state, &show(&s).await)
            .await
            .unwrap()
            .is_none(),
        "no edit"
    );
    let body = r#"{"start":1.0,"end":19.0,"gainDb":0.0,"sections":[],
        "snippets":[{"slot":2,"start":12.0,"length":5.0},{"slot":1,"start":2.0,"length":6.0}]}"#;
    assert_eq!(put_edit(&s, body).await.0, StatusCode::OK);

    let (source, e) = post_snippets(&s.state, &show(&s).await)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(source.key, "recordings/1/final.mp3");
    let slots: Vec<i64> = e.snippets.iter().map(|x| x.slot).collect();
    assert_eq!(slots, [1, 2]);

    let mut jpeg = std::io::Cursor::new(Vec::new());
    image::DynamicImage::ImageRgb8(image::RgbImage::from_pixel(
        1080,
        1350,
        image::Rgb([9, 9, 9]),
    ))
    .write_to(&mut jpeg, image::ImageFormat::Jpeg)
    .unwrap();
    let urls = snippet_clips(&s.state, s.id, &source, &e, jpeg.get_ref())
        .await
        .unwrap();
    assert_eq!(urls.len(), 2);
    assert!(
        urls[0].contains("announcements/post-snippet-1.mp4"),
        "{}",
        urls[0]
    );
    assert!(
        urls[1].contains("announcements/post-snippet-2.mp4"),
        "{}",
        urls[1]
    );

    let key = format!("shows/{}/announcements/post-snippet-1.mp4", s.id);
    let p = probe(&s.r2.object(&s.state, &key).unwrap()).await;
    let streams = p["streams"].as_array().unwrap();
    assert!(streams
        .iter()
        .any(|st| st["codec_type"] == "video" && st["width"] == 1080 && st["height"] == 1350));
    assert!(streams.iter().any(|st| st["codec_type"] == "audio"));
    let length: f64 = p["format"]["duration"].as_str().unwrap().parse().unwrap();
    assert!((length - 6.0).abs() < 0.2, "{length}");
}
