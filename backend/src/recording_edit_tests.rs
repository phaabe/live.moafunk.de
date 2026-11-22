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
    load_row(&s.state.db, s.id)
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
        json!({"source": null, "edit": null, "status": "none", "error": null,
               "edited_url": null, "rendered_at": null})
    );
    assert_eq!(put_edit(&s, EDIT).await.0, StatusCode::NOT_FOUND);

    // A manual upload: its length is not stored, so `end` is not checked against it.
    sqlx::query("UPDATE shows SET recording_key = 'shows/1/set.wav' WHERE id = ?")
        .bind(s.id)
        .execute(&s.state.db)
        .await
        .unwrap();
    let (_, view) = get_edit(&s).await;
    assert_eq!(view["status"], "none");
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
        key.starts_with(&format!("recordings/{}/edited/", s.id)),
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
        (upload.key.as_str(), upload.filename.as_str()),
        (key.as_str(), "v1-edited.mp3")
    );

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
        (upload.key.as_str(), upload.filename.as_str()),
        (newer.as_str(), "v2.mp3")
    );
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
    let row = load_row(&s.state.db, s.id).await.unwrap().unwrap();
    assert_eq!(row.status, "failed");
    assert!(row.error_message.unwrap().contains("restarted"));
}
