//! Booking API against real handlers, SQLite and a small fake R2.

use super::*;
use crate::broadcast_tests::{add_user, headers, state_with};
use crate::series::{RepeatRule, Rhythm};
use axum::{
    body::{to_bytes, Body, Bytes},
    extract::{FromRequest, Multipart, Path, Query},
    http::{Method, Request, StatusCode, Uri},
};
use chrono::Datelike;
use handlers::booking;
use serde_json::{json, Value};
use std::sync::Mutex;

// ── Fake R2: path-style PUT / GET / DELETE and CopyObject in memory ─────────

type Objects = Arc<Mutex<HashMap<String, Vec<u8>>>>;

async fn fake_r2_object(
    State(objects): State<Objects>,
    method: Method,
    uri: Uri,
    headers: HeaderMap,
    body: Bytes,
) -> axum::response::Response {
    // Path style: /<bucket>/<key>
    let key = uri.path().trim_start_matches('/').to_string();
    let mut objects = objects.lock().unwrap();
    let not_found = || {
        (
            StatusCode::NOT_FOUND,
            [("content-type", "application/xml")],
            "<?xml version=\"1.0\"?><Error><Code>NoSuchKey</Code><Message>no</Message></Error>",
        )
            .into_response()
    };
    match method {
        Method::PUT => {
            if let Some(source) = headers.get("x-amz-copy-source") {
                let source = source.to_str().unwrap().trim_start_matches('/');
                let Some(data) = objects.get(source).cloned() else {
                    return not_found();
                };
                objects.insert(key, data);
                return (
                    StatusCode::OK,
                    [("content-type", "application/xml")],
                    "<?xml version=\"1.0\"?><CopyObjectResult><ETag>\"e\"</ETag>\
                     <LastModified>2026-01-01T00:00:00.000Z</LastModified></CopyObjectResult>",
                )
                    .into_response();
            }
            objects.insert(key, body.to_vec());
            (StatusCode::OK, [("etag", "\"e\"")]).into_response()
        }
        Method::GET => match objects.get(&key) {
            Some(data) => (StatusCode::OK, data.clone()).into_response(),
            None => not_found(),
        },
        Method::DELETE => {
            objects.remove(&key);
            StatusCode::NO_CONTENT.into_response()
        }
        _ => StatusCode::METHOD_NOT_ALLOWED.into_response(),
    }
}

async fn booking_state() -> (Arc<AppState>, tempfile::TempDir, Objects) {
    let objects: Objects = Arc::default();
    let app = axum::Router::new()
        .fallback(fake_r2_object)
        .with_state(objects.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let (state, temp) = state_with(json!({ "r2_endpoint": endpoint })).await;
    (state, temp, objects)
}

// ── Helpers ─────────────────────────────────────────────────────────────────

fn sundays() -> Rhythm {
    Rhythm {
        repeat_rule: RepeatRule::Weekly,
        weekday: 7,
        start_time: "12:00".to_string(),
        end_time: "14:00".to_string(),
    }
}

/// A series of `owner` with the Sunday 12:00–14:00 rhythm. Returns (id, slug).
async fn series(state: &Arc<AppState>, owner: i64) -> (i64, String) {
    crate::series::insert_series(&state.db, owner, "Brunchtime", None, Some(&sundays()))
        .await
        .unwrap()
}

async fn json_of(response: impl IntoResponse) -> (StatusCode, Value) {
    let response = response.into_response();
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 1024 * 1024).await.unwrap();
    (status, serde_json::from_slice(&bytes).unwrap())
}

async fn slots(state: &Arc<AppState>, user: i64, slug: &str) -> Result<Vec<Value>> {
    let response = booking::api_series_slots(
        State(state.clone()),
        Path(slug.to_string()),
        headers(state, user).await,
    )
    .await?;
    Ok(json_of(response).await.1["slots"]
        .as_array()
        .unwrap()
        .clone())
}

async fn book(
    state: &Arc<AppState>,
    user: i64,
    slug: &str,
    slot: &Value,
    mode: &str,
    upload_id: Option<&str>,
) -> Result<Value> {
    let request = serde_json::from_value(json!({
        "date": slot["date"], "start_time": slot["start_time"], "end_time": slot["end_time"],
        "mode": mode, "upload_id": upload_id
    }))
    .unwrap();
    let response = booking::api_book_episode(
        State(state.clone()),
        Path(slug.to_string()),
        headers(state, user).await,
        axum::Json(request),
    )
    .await?;
    let (status, body) = json_of(response).await;
    assert_eq!(status, StatusCode::CREATED);
    Ok(body)
}

/// One second of 8 kHz mono silence.
fn wav_one_second() -> Vec<u8> {
    let rate: u32 = 8000;
    let data_len = rate * 2;
    let mut wav = Vec::new();
    wav.extend_from_slice(b"RIFF");
    wav.extend_from_slice(&(36 + data_len).to_le_bytes());
    wav.extend_from_slice(b"WAVEfmt ");
    wav.extend_from_slice(&16u32.to_le_bytes());
    wav.extend_from_slice(&1u16.to_le_bytes()); // PCM
    wav.extend_from_slice(&1u16.to_le_bytes()); // mono
    wav.extend_from_slice(&rate.to_le_bytes());
    wav.extend_from_slice(&(rate * 2).to_le_bytes());
    wav.extend_from_slice(&2u16.to_le_bytes());
    wav.extend_from_slice(&16u16.to_le_bytes());
    wav.extend_from_slice(b"data");
    wav.extend_from_slice(&data_len.to_le_bytes());
    wav.resize(wav.len() + data_len as usize, 0);
    wav
}

const BOUNDARY: &str = "booking-test-boundary";

async fn chunk_form(data: &[u8]) -> Multipart {
    let mut body = format!(
        "--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"chunk\"; filename=\"blob\"\r\n\
         Content-Type: application/octet-stream\r\n\r\n"
    )
    .into_bytes();
    body.extend_from_slice(data);
    body.extend_from_slice(format!("\r\n--{BOUNDARY}--\r\n").as_bytes());
    let request = Request::builder()
        .header(
            "content-type",
            format!("multipart/form-data; boundary={BOUNDARY}"),
        )
        .body(Body::from(body))
        .unwrap();
    Multipart::from_request(request, &()).await.unwrap()
}

async fn upload_init(
    state: &Arc<AppState>,
    user: i64,
    slug: &str,
    filename: &str,
    chunks: u32,
) -> Result<String> {
    let request =
        serde_json::from_value(json!({"filename": filename, "total_chunks": chunks})).unwrap();
    let response = booking::api_series_upload_init(
        State(state.clone()),
        Path(slug.to_string()),
        headers(state, user).await,
        axum::Json(request),
    )
    .await?;
    Ok(json_of(response).await.1["session_id"]
        .as_str()
        .unwrap()
        .to_string())
}

async fn upload_chunk(
    state: &Arc<AppState>,
    user: i64,
    slug: &str,
    session: &str,
    index: u32,
    data: &[u8],
) -> Result<()> {
    booking::api_series_upload_chunk(
        State(state.clone()),
        Path((slug.to_string(), session.to_string())),
        Query(serde_json::from_value(json!({ "index": index })).unwrap()),
        headers(state, user).await,
        chunk_form(data).await,
    )
    .await
    .map(|_| ())
}

async fn upload_finalize(
    state: &Arc<AppState>,
    user: i64,
    slug: &str,
    session: &str,
) -> Result<Value> {
    let response = booking::api_series_upload_finalize(
        State(state.clone()),
        Path((slug.to_string(), session.to_string())),
        headers(state, user).await,
    )
    .await?;
    Ok(json_of(response).await.1)
}

/// The full upload: two chunks of a one-second WAV. Returns the upload id.
async fn upload_set(state: &Arc<AppState>, user: i64, slug: &str, filename: &str) -> String {
    let wav = wav_one_second();
    let (first, second) = wav.split_at(wav.len() / 2);
    let session = upload_init(state, user, slug, filename, 2).await.unwrap();
    upload_chunk(state, user, slug, &session, 0, first)
        .await
        .unwrap();
    upload_chunk(state, user, slug, &session, 1, second)
        .await
        .unwrap();
    let done = upload_finalize(state, user, slug, &session).await.unwrap();
    assert_eq!(done["filename"], filename);
    assert_eq!(done["duration"], 1, "duration in seconds from ffprobe");
    done["upload_id"].as_str().unwrap().to_string()
}

#[derive(sqlx::FromRow, Debug)]
struct Episode {
    title: String,
    template_id: Option<i64>,
    host_user_id: Option<i64>,
    created_by: Option<i64>,
    show_type: String,
    stream_mode: Option<String>,
    prerecorded_key: Option<String>,
    prerecorded_filename: Option<String>,
    prerecorded_confirmed_at: Option<String>,
    wrapup_published_at: Option<String>,
}

async fn episode(state: &Arc<AppState>, id: i64) -> Option<Episode> {
    sqlx::query_as(
        "SELECT title, template_id, host_user_id, created_by, show_type, stream_mode, \
         prerecorded_key, prerecorded_filename, prerecorded_confirmed_at, wrapup_published_at \
         FROM shows WHERE id = ?",
    )
    .bind(id)
    .fetch_optional(&state.db)
    .await
    .unwrap()
}

/// An episode of the series inserted directly (e.g. one in the past).
async fn insert_episode(state: &Arc<AppState>, series_id: i64, host: i64, date: &str) -> i64 {
    sqlx::query(
        "INSERT INTO shows (title, date, start_time, end_time, show_type, host_user_id, \
         created_by, stream_mode, template_id) \
         VALUES ('Brunchtime', ?, '12:00', '14:00', 'external', ?, ?, 'live', ?)",
    )
    .bind(date)
    .bind(host)
    .bind(host)
    .bind(series_id)
    .execute(&state.db)
    .await
    .unwrap()
    .last_insert_rowid()
}

async fn cancel(state: &Arc<AppState>, user: i64, id: i64) -> Result<()> {
    booking::api_cancel_booking(State(state.clone()), Path(id), headers(state, user).await)
        .await
        .map(|_| ())
}

async fn publish(state: &Arc<AppState>, user: i64, id: i64) -> Result<()> {
    booking::api_publish_episode(State(state.clone()), Path(id), headers(state, user).await)
        .await
        .map(|_| ())
}

async fn replace_file(state: &Arc<AppState>, user: i64, id: i64, upload_id: &str) -> Result<()> {
    let request = serde_json::from_value(json!({ "upload_id": upload_id })).unwrap();
    booking::api_replace_prerecorded(
        State(state.clone()),
        Path(id),
        headers(state, user).await,
        axum::Json(request),
    )
    .await
    .map(|_| ())
}

// ── Tests ───────────────────────────────────────────────────────────────────

#[tokio::test]
async fn slots_follow_the_rhythm_and_leave_out_taken_times() {
    let (state, _temp, _r2) = booking_state().await;
    let host = add_user(&state, "host", "host").await;
    let (_, slug) = series(&state, host).await;

    let free = slots(&state, host, &slug).await.unwrap();
    assert!((11..=12).contains(&free.len()), "12 weeks of Sundays");
    let today = chrono::Utc::now()
        .with_timezone(&chrono_tz::Europe::Berlin)
        .date_naive();
    for slot in &free {
        let date =
            chrono::NaiveDate::parse_from_str(slot["date"].as_str().unwrap(), "%Y-%m-%d").unwrap();
        assert_eq!(date.weekday(), chrono::Weekday::Sun);
        assert!(date >= today && date < today + chrono::Duration::weeks(12));
        assert_eq!(
            (&slot["start_time"], &slot["end_time"]),
            (&json!("12:00"), &json!("14:00"))
        );
    }

    // Any show on that date that overlaps the slot takes it.
    sqlx::query(
        "INSERT INTO shows (title, date, start_time, end_time) VALUES ('Other', ?, '13:00', '15:00')",
    )
    .bind(free[0]["date"].as_str().unwrap())
    .execute(&state.db)
    .await
    .unwrap();
    let after = slots(&state, host, &slug).await.unwrap();
    assert_eq!(after, free[1..]);

    // A series without a rhythm has no slots.
    let (_, bare) = crate::series::insert_series(&state.db, host, "Bare", None, None)
        .await
        .unwrap();
    assert!(slots(&state, host, &bare).await.unwrap().is_empty());
}

#[tokio::test]
async fn booking_a_live_slot_creates_one_linked_episode() {
    let (state, _temp, _r2) = booking_state().await;
    let host = add_user(&state, "host", "host").await;
    let (series_id, slug) = series(&state, host).await;
    let free = slots(&state, host, &slug).await.unwrap();

    let booked = book(&state, host, &slug, &free[0], "live", None)
        .await
        .unwrap();
    let id = booked["id"].as_i64().unwrap();
    let row = episode(&state, id).await.unwrap();
    assert_eq!(row.title, "Brunchtime #1");
    assert_eq!(row.template_id, Some(series_id));
    assert_eq!((row.host_user_id, row.created_by), (Some(host), Some(host)));
    assert_eq!(row.show_type, "external");
    assert_eq!(row.stream_mode.as_deref(), Some("live"));
    assert!(row.prerecorded_key.is_none());
    assert!(!slots(&state, host, &slug).await.unwrap().contains(&free[0]));

    // The slot is taken now: 409 and no second row.
    let count = || async {
        sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM shows")
            .fetch_one(&state.db)
            .await
            .unwrap()
    };
    let before = count().await;
    assert!(matches!(
        book(&state, host, &slug, &free[0], "live", None).await,
        Err(AppError::Conflict(_))
    ));
    assert_eq!(count().await, before);

    // Only the rhythm's times, in the future, can be booked.
    let mut off_rhythm = free[1].clone();
    off_rhythm["start_time"] = json!("13:00");
    let past = json!({"date": "2020-01-05", "start_time": "12:00", "end_time": "14:00"});
    for slot in [off_rhythm, past] {
        assert!(matches!(
            book(&state, host, &slug, &slot, "live", None).await,
            Err(AppError::BadRequest(_))
        ));
    }
    assert_eq!(count().await, before);
}

#[tokio::test]
async fn a_set_is_uploaded_first_then_booked_as_prerecorded() {
    let (state, _temp, objects) = booking_state().await;
    let host = add_user(&state, "host", "host").await;
    let (_, slug) = series(&state, host).await;
    let free = slots(&state, host, &slug).await.unwrap();

    assert!(matches!(
        upload_init(&state, host, &slug, "set.flac", 1).await,
        Err(AppError::BadRequest(_))
    ));
    // Not all chunks: finalize refuses.
    let partial = upload_init(&state, host, &slug, "part.wav", 2)
        .await
        .unwrap();
    upload_chunk(&state, host, &slug, &partial, 0, b"half")
        .await
        .unwrap();
    assert!(matches!(
        upload_finalize(&state, host, &slug, &partial).await,
        Err(AppError::BadRequest(_))
    ));

    // Without a finished upload there is no pre-recorded booking.
    assert!(matches!(
        book(&state, host, &slug, &free[0], "prerecorded", None).await,
        Err(AppError::BadRequest(_))
    ));
    assert!(matches!(
        book(&state, host, &slug, &free[0], "prerecorded", Some(&partial)).await,
        Err(AppError::BadRequest(_))
    ));

    let upload = upload_set(&state, host, &slug, "set.wav").await;
    // Finalize is safe to repeat.
    let again = upload_finalize(&state, host, &slug, &upload).await.unwrap();
    assert_eq!(
        (&again["filename"], &again["duration"]),
        (&json!("set.wav"), &json!(1))
    );

    let booked = book(&state, host, &slug, &free[0], "prerecorded", Some(&upload))
        .await
        .unwrap();
    assert_eq!(booked["mode"], "prerecorded");
    let row = episode(&state, booked["id"].as_i64().unwrap())
        .await
        .unwrap();
    let key = row.prerecorded_key.expect("the set is linked");
    assert_eq!(row.prerecorded_filename.as_deref(), Some("set.wav"));
    assert_eq!(row.stream_mode.as_deref(), Some("prerecorded"));
    assert!(row.prerecorded_confirmed_at.is_some());
    {
        let objects = objects.lock().unwrap();
        let stored = &objects[&format!("{}/{key}", state.config.r2_bucket_name)];
        assert_eq!(stored, &wav_one_second(), "the joined chunks are the file");
        assert!(
            !objects
                .keys()
                .any(|k| k.contains("/chunk-") && k.contains(&upload)),
            "the chunks are removed"
        );
    }

    // One file plays in one episode.
    assert!(matches!(
        book(&state, host, &slug, &free[1], "prerecorded", Some(&upload)).await,
        Err(AppError::BadRequest(_))
    ));
}

#[tokio::test]
async fn replacing_the_file_keeps_the_booking() {
    let (state, _temp, _r2) = booking_state().await;
    let host = add_user(&state, "host", "host").await;
    let (_, slug) = series(&state, host).await;
    let free = slots(&state, host, &slug).await.unwrap();
    let first = upload_set(&state, host, &slug, "first.wav").await;
    let id = book(&state, host, &slug, &free[0], "prerecorded", Some(&first))
        .await
        .unwrap()["id"]
        .as_i64()
        .unwrap();
    let before = episode(&state, id).await.unwrap();

    let second = upload_set(&state, host, &slug, "second.wav").await;
    replace_file(&state, host, id, &second).await.unwrap();
    let after = episode(&state, id).await.unwrap();
    assert_eq!(after.prerecorded_filename.as_deref(), Some("second.wav"));
    assert_ne!(after.prerecorded_key, before.prerecorded_key);
    assert_eq!(after.stream_mode.as_deref(), Some("prerecorded"));
    assert_eq!(
        after.prerecorded_confirmed_at,
        before.prerecorded_confirmed_at
    );
    assert!(
        slots(&state, host, &slug).await.unwrap()[0] != free[0],
        "still booked"
    );

    // A live episode has no file to replace.
    let live = book(&state, host, &slug, &free[1], "live", None)
        .await
        .unwrap()["id"]
        .as_i64()
        .unwrap();
    let third = upload_set(&state, host, &slug, "third.wav").await;
    assert!(matches!(
        replace_file(&state, host, live, &third).await,
        Err(AppError::BadRequest(_))
    ));
}

#[tokio::test]
async fn cancel_frees_the_slot_only_before_the_start() {
    let (state, _temp, _r2) = booking_state().await;
    let host = add_user(&state, "host", "host").await;
    let (series_id, slug) = series(&state, host).await;
    let free = slots(&state, host, &slug).await.unwrap();
    let id = book(&state, host, &slug, &free[0], "live", None)
        .await
        .unwrap()["id"]
        .as_i64()
        .unwrap();

    cancel(&state, host, id).await.unwrap();
    assert!(episode(&state, id).await.is_none());
    assert_eq!(slots(&state, host, &slug).await.unwrap(), free);

    let started = insert_episode(&state, series_id, host, "2020-01-05").await;
    assert!(matches!(
        cancel(&state, host, started).await,
        Err(AppError::BadRequest(_))
    ));
    assert!(episode(&state, started).await.is_some());
}

#[tokio::test]
async fn publish_closes_the_wrapup_of_an_ended_episode_once() {
    let (state, _temp, _r2) = booking_state().await;
    let host = add_user(&state, "host", "host").await;
    let (series_id, slug) = series(&state, host).await;
    let ended = insert_episode(&state, series_id, host, "2020-01-05").await;

    publish(&state, host, ended).await.unwrap();
    let published = episode(&state, ended).await.unwrap().wrapup_published_at;
    assert!(published.is_some());
    let detail = handlers::series::api_get_series(
        State(state.clone()),
        Path(slug.clone()),
        headers(&state, host).await,
    )
    .await
    .unwrap();
    let (_, detail) = json_of(detail).await;
    assert_eq!(detail["episodes"][0]["status_label"], "Finished");

    // A second publish changes nothing.
    assert!(matches!(
        publish(&state, host, ended).await,
        Err(AppError::Conflict(_))
    ));
    assert_eq!(
        episode(&state, ended).await.unwrap().wrapup_published_at,
        published
    );

    // Before the end there is nothing to publish.
    let free = slots(&state, host, &slug).await.unwrap();
    let upcoming = book(&state, host, &slug, &free[0], "live", None)
        .await
        .unwrap()["id"]
        .as_i64()
        .unwrap();
    assert!(matches!(
        publish(&state, host, upcoming).await,
        Err(AppError::BadRequest(_))
    ));
    assert!(episode(&state, upcoming)
        .await
        .unwrap()
        .wrapup_published_at
        .is_none());
}

#[tokio::test]
async fn other_hosts_get_403_and_admins_may_book() {
    let (state, _temp, _r2) = booking_state().await;
    let alice = add_user(&state, "alice", "host").await;
    let bob = add_user(&state, "bob", "host").await;
    let admin = add_user(&state, "admin", "admin").await;
    let (series_id, slug) = series(&state, alice).await;
    let free = slots(&state, alice, &slug).await.unwrap();
    let booked = book(&state, alice, &slug, &free[0], "live", None)
        .await
        .unwrap()["id"]
        .as_i64()
        .unwrap();
    let ended = insert_episode(&state, series_id, alice, "2020-01-05").await;
    let upload = upload_set(&state, alice, &slug, "set.wav").await;

    let forbidden = |result: Result<()>| matches!(result, Err(AppError::Forbidden(_)));
    assert!(forbidden(slots(&state, bob, &slug).await.map(|_| ())));
    assert!(forbidden(
        book(&state, bob, &slug, &free[1], "live", None)
            .await
            .map(|_| ())
    ));
    assert!(forbidden(
        upload_init(&state, bob, &slug, "set.wav", 1)
            .await
            .map(|_| ())
    ));
    assert!(forbidden(cancel(&state, bob, booked).await));
    assert!(forbidden(publish(&state, bob, ended).await));
    assert!(forbidden(replace_file(&state, bob, booked, &upload).await));
    assert!(episode(&state, booked).await.is_some());

    // An admin books for Alice: Alice hosts, the admin is the creator.
    assert_eq!(
        slots(&state, admin, &slug).await.unwrap().len(),
        free.len() - 1
    );
    let by_admin = book(&state, admin, &slug, &free[1], "live", None)
        .await
        .unwrap()["id"]
        .as_i64()
        .unwrap();
    let row = episode(&state, by_admin).await.unwrap();
    assert_eq!(
        (row.host_user_id, row.created_by),
        (Some(alice), Some(admin))
    );
    publish(&state, admin, ended).await.unwrap();
    cancel(&state, admin, by_admin).await.unwrap();
}
