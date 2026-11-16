//! The checks at two trust boundaries, against real handlers, SQLite and the
//! fake R2: the public artist submission (no login) and the admin-only
//! artist downloads. Also the admin's chunked show recording upload.

use super::*;
use crate::broadcast_tests::{add_user, headers, state_with};
use crate::fake_r2::FakeR2;
use axum::{
    body::Body,
    extract::{FromRequest, Multipart, Path, Query},
    http::{Request, StatusCode},
    Json,
};
use handlers::{download, submit_chunked, upload_recording_chunked};
use serde_json::{json, Value};
use std::time::Duration;

const BOUNDARY: &str = "submit-test-boundary";
const MIB: usize = 1024 * 1024;

/// A fake R2 and a state with `overrides` on top of the test config.
async fn setup(overrides: Value) -> (Arc<AppState>, tempfile::TempDir, FakeR2) {
    let (r2, endpoint) = FakeR2::serve().await;
    let mut config = overrides;
    config["r2_endpoint"] = json!(endpoint);
    let (state, temp) = state_with(config).await;
    (state, temp, r2)
}

/// A multipart form of `(name, file name, data)` parts.
async fn form(parts: &[(&str, Option<&str>, &[u8])]) -> Multipart {
    let mut body = Vec::new();
    for (name, file, data) in parts {
        let disposition = match file {
            Some(file) => format!(
                "Content-Disposition: form-data; name=\"{name}\"; filename=\"{file}\"\r\n\
                 Content-Type: application/octet-stream"
            ),
            None => format!("Content-Disposition: form-data; name=\"{name}\""),
        };
        body.extend_from_slice(format!("--{BOUNDARY}\r\n{disposition}\r\n\r\n").as_bytes());
        body.extend_from_slice(data);
        body.extend_from_slice(b"\r\n");
    }
    body.extend_from_slice(format!("--{BOUNDARY}--\r\n").as_bytes());
    let request = Request::builder()
        .header(
            "content-type",
            format!("multipart/form-data; boundary={BOUNDARY}"),
        )
        .body(Body::from(body))
        .unwrap();
    Multipart::from_request(request, &()).await.unwrap()
}

fn status(response: impl IntoResponse) -> StatusCode {
    response.into_response().status()
}

fn all_keys(state: &AppState, r2: &FakeR2) -> Vec<String> {
    r2.keys_in(&state.config.r2_bucket_name, "")
}

async fn artist_count(state: &Arc<AppState>) -> i64 {
    sqlx::query_scalar("SELECT COUNT(*) FROM artists")
        .fetch_one(&state.db)
        .await
        .unwrap()
}

// ── Public submission ───────────────────────────────────────────────────────

/// Step 1 of the form: text fields and the picture. Returns the session.
async fn submit_init(state: &Arc<AppState>) -> String {
    let form = form(&[
        ("artist-name", None, b"Nova"),
        ("pronouns", None, b"she/her"),
        ("music-description", None, b"Slow ambient"),
        ("track1-name", None, b"One"),
        ("track2-name", None, b"Two"),
        ("no-voice-message", None, b"on"),
        ("artist-pic", Some("pic.jpg"), b"picture"),
    ])
    .await;
    let Json(init) = submit_chunked::submit_init(State(state.clone()), form)
        .await
        .unwrap();
    init.session_id
}

/// Step 2: one file for `field`.
async fn submit_file(
    state: &Arc<AppState>,
    session: &str,
    field: &str,
    file: &str,
    data: &[u8],
) -> StatusCode {
    let form = form(&[("file", Some(file), data)]).await;
    status(
        submit_chunked::submit_file(
            State(state.clone()),
            Path(session.to_string()),
            Query(submit_chunked::FileQuery {
                field: field.to_string(),
            }),
            form,
        )
        .await,
    )
}

/// Step 3: commit the submission.
async fn submit_finalize(
    state: &Arc<AppState>,
    session: &str,
) -> crate::Result<Json<submit_chunked::FinalizeResponse>> {
    submit_chunked::submit_finalize(State(state.clone()), Path(session.to_string())).await
}

async fn chunk_init(
    state: &Arc<AppState>,
    session: &str,
    field: &str,
    total_size: u64,
    total_chunks: u32,
) -> crate::Result<Json<submit_chunked::FileChunkInitResponse>> {
    submit_chunked::submit_file_chunk_init(
        State(state.clone()),
        Path(session.to_string()),
        Query(submit_chunked::FileQuery {
            field: field.to_string(),
        }),
        Json(submit_chunked::FileChunkInitRequest {
            filename: "big.mp3".to_string(),
            total_size,
            total_chunks,
            peaks: None,
        }),
    )
    .await
}

async fn chunk(
    state: &Arc<AppState>,
    file_session: &str,
    field: &str,
    index: u32,
    data: &[u8],
) -> StatusCode {
    let query = [
        ("field".to_string(), field.to_string()),
        ("index".to_string(), index.to_string()),
    ];
    status(
        submit_chunked::submit_file_chunk(
            State(state.clone()),
            Path(file_session.to_string()),
            Query(query.into_iter().collect()),
            form(&[("chunk", Some("blob"), data)]).await,
        )
        .await,
    )
}

async fn chunk_finalize(state: &Arc<AppState>, file_session: &str, field: &str) -> StatusCode {
    status(
        submit_chunked::submit_file_chunk_finalize(
            State(state.clone()),
            Path(file_session.to_string()),
            Query(submit_chunked::FileQuery {
                field: field.to_string(),
            }),
        )
        .await,
    )
}

/// A double click or a retry finalizes a session again. Only the first
/// finalize creates the artist; the others return it. Once the background
/// job has moved the files, the session is gone.
#[tokio::test]
async fn a_session_makes_one_artist_however_often_it_is_finalized() {
    let (state, _temp, r2) = setup(json!({})).await;
    let session = submit_init(&state).await;
    for field in ["track1", "track2"] {
        assert_eq!(
            submit_file(&state, &session, field, "song.mp3", b"mp3 audio").await,
            StatusCode::OK
        );
    }

    let first = submit_finalize(&state, &session).await.unwrap().0;
    let again = submit_finalize(&state, &session).await.unwrap().0;

    let artist_id = first.artist_id.expect("an artist");
    assert_eq!(again.artist_id, Some(artist_id), "the same artist");
    assert_eq!(artist_count(&state).await, 1);

    // The background job moves the files and ends the session.
    let done = async {
        loop {
            let status: String = sqlx::query_scalar("SELECT status FROM artists WHERE id = ?")
                .bind(artist_id)
                .fetch_one(&state.db)
                .await
                .unwrap();
            if status != "processing" {
                return status;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    };
    let status = tokio::time::timeout(Duration::from_secs(20), done)
        .await
        .expect("the background job did not finish");
    assert_eq!(status, "unassigned");
    let track1: String = sqlx::query_scalar("SELECT track1_key FROM artists WHERE id = ?")
        .bind(artist_id)
        .fetch_one(&state.db)
        .await
        .unwrap();
    assert!(
        track1.starts_with(&format!("artists/{artist_id}/")),
        "{track1}"
    );
    assert_eq!(r2.object(&state, &track1).unwrap(), b"mp3 audio");

    let late = submit_finalize(&state, &session).await;
    assert_eq!(status_of(late), StatusCode::BAD_REQUEST);
    assert_eq!(artist_count(&state).await, 1);
}

fn status_of<T: IntoResponse>(result: crate::Result<T>) -> StatusCode {
    status(result)
}

/// Every step after the first needs a live session. An unknown or expired
/// one is refused and nothing reaches R2.
#[tokio::test]
async fn unknown_and_expired_sessions_are_refused() {
    let (state, _temp, r2) = setup(json!({})).await;
    let unknown = "00000000-0000-0000-0000-000000000000";

    assert_eq!(
        submit_file(&state, unknown, "track1", "song.mp3", b"audio").await,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(
        status_of(chunk_init(&state, unknown, "track1", 10, 1).await),
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        chunk(&state, unknown, "track1", 0, b"audio").await,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        chunk_finalize(&state, unknown, "track1").await,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        status_of(submit_finalize(&state, unknown).await),
        StatusCode::BAD_REQUEST
    );
    assert!(
        all_keys(&state, &r2).is_empty(),
        "{:?}",
        all_keys(&state, &r2)
    );

    // A session lives one hour. Its end is stored so SQLite compares it right.
    let session = submit_init(&state).await;
    let in_an_hour: bool = sqlx::query_scalar(
        "SELECT expires_at > datetime('now', '+59 minutes') \
         AND expires_at < datetime('now', '+61 minutes') \
         FROM pending_submissions WHERE session_id = ?",
    )
    .bind(&session)
    .fetch_one(&state.db)
    .await
    .unwrap();
    assert!(in_an_hour);

    sqlx::query(
        "UPDATE pending_submissions SET expires_at = datetime('now', '-1 minute') \
         WHERE session_id = ?",
    )
    .bind(&session)
    .execute(&state.db)
    .await
    .unwrap();
    let keys = all_keys(&state, &r2);

    assert_eq!(
        submit_file(&state, &session, "track1", "song.mp3", b"audio").await,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(
        status_of(chunk_init(&state, &session, "track1", 10, 1).await),
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        status_of(submit_finalize(&state, &session).await),
        StatusCode::BAD_REQUEST
    );
    assert_eq!(all_keys(&state, &r2), keys, "no new object");
    assert_eq!(artist_count(&state).await, 0);
}

/// Only track1, track2 and voice take a file, and only an audio file.
#[tokio::test]
async fn unknown_fields_and_non_audio_files_are_refused() {
    let (state, _temp, r2) = setup(json!({})).await;
    let session = submit_init(&state).await;
    let keys = all_keys(&state, &r2);

    for field in ["pic", "track1_key = 'x', voice", "track3"] {
        assert_eq!(
            submit_file(&state, &session, field, "song.mp3", b"audio").await,
            StatusCode::BAD_REQUEST,
            "{field}"
        );
        assert_eq!(
            status_of(chunk_init(&state, &session, field, 10, 1).await),
            StatusCode::BAD_REQUEST,
            "{field}"
        );
    }
    assert_eq!(
        submit_file(&state, &session, "track1", "tool.exe", b"MZ").await,
        StatusCode::BAD_REQUEST
    );

    assert_eq!(all_keys(&state, &r2), keys, "no new object");
    let uploaded: Option<String> = sqlx::query_scalar(
        "SELECT track1_original_key FROM pending_submissions WHERE session_id = ?",
    )
    .bind(&session)
    .fetch_one(&state.db)
    .await
    .unwrap();
    assert_eq!(uploaded, None);
}

/// The file size limit holds for a single upload and for the chunked one,
/// where the client chooses the size and the chunk count.
#[tokio::test]
async fn the_size_limit_holds_for_single_and_chunked_uploads() {
    let (state, _temp, r2) = setup(json!({ "max_file_size_mb": 1 })).await;
    let session = submit_init(&state).await;
    let keys = all_keys(&state, &r2);

    assert_eq!(
        submit_file(&state, &session, "track1", "big.mp3", &vec![0; MIB + 1]).await,
        StatusCode::PAYLOAD_TOO_LARGE
    );
    assert_eq!(
        status_of(chunk_init(&state, &session, "track1", MIB as u64 + 1, 2).await),
        StatusCode::PAYLOAD_TOO_LARGE
    );

    // The client says 1000 bytes in 2 chunks, then sends more.
    let file = chunk_init(&state, &session, "track1", 1000, 2)
        .await
        .unwrap()
        .0
        .file_session_id;
    assert_eq!(
        chunk(&state, &file, "track1", 2, b"past the end").await,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(
        chunk(&state, &file, "track1", 0, &vec![0; MIB + 1]).await,
        StatusCode::PAYLOAD_TOO_LARGE
    );
    for index in 0..2 {
        assert_eq!(
            chunk(&state, &file, "track1", index, &vec![0; MIB * 3 / 4]).await,
            StatusCode::OK
        );
    }
    assert_eq!(
        chunk_finalize(&state, &file, "track1").await,
        StatusCode::PAYLOAD_TOO_LARGE
    );

    let uploaded: Option<String> = sqlx::query_scalar(
        "SELECT track1_original_key FROM pending_submissions WHERE session_id = ?",
    )
    .bind(&session)
    .fetch_one(&state.db)
    .await
    .unwrap();
    assert_eq!(uploaded, None, "the too large file is not taken");
    let audio: Vec<String> = all_keys(&state, &r2)
        .into_iter()
        .filter(|key| !keys.contains(key) && !key.starts_with("pending-file-chunks/"))
        .collect();
    assert!(audio.is_empty(), "{audio:?}");
}

/// A chunked file within the limit goes through the same path as a single
/// upload: it is assembled in order and counts for the finalize.
#[tokio::test]
async fn a_chunked_file_within_the_limit_is_assembled() {
    let (state, _temp, r2) = setup(json!({})).await;
    let session = submit_init(&state).await;
    let file = chunk_init(&state, &session, "track1", 6, 2)
        .await
        .unwrap()
        .0
        .file_session_id;
    assert_eq!(
        chunk(&state, &file, "track1", 1, b"def").await,
        StatusCode::OK
    );
    assert_eq!(
        chunk(&state, &file, "track1", 0, b"abc").await,
        StatusCode::OK
    );

    assert_eq!(
        chunk_finalize(&state, &file, "track1").await,
        StatusCode::OK
    );

    let key: String = sqlx::query_scalar(
        "SELECT track1_original_key FROM pending_submissions WHERE session_id = ?",
    )
    .bind(&session)
    .fetch_one(&state.db)
    .await
    .unwrap();
    assert_eq!(r2.object(&state, &key).unwrap(), b"abcdef");
    assert!(r2
        .keys_in(&state.config.r2_bucket_name, "pending-file-chunks/")
        .is_empty());
}

// ── Admin downloads ─────────────────────────────────────────────────────────

/// A small real PNG, so the PDF can embed the picture.
fn png() -> Vec<u8> {
    let mut data = Vec::new();
    image::DynamicImage::new_rgb8(8, 8)
        .write_to(
            &mut std::io::Cursor::new(&mut data),
            image::ImageFormat::Png,
        )
        .unwrap();
    data
}

async fn add_artist(state: &Arc<AppState>, r2: &FakeR2) -> i64 {
    let id: i64 = sqlx::query_scalar(
        "INSERT INTO artists (name, pronouns, track1_name, track2_name, status, \
         pic_key, track1_key, track2_key) \
         VALUES ('Nova', 'she/her', 'One', 'Two', 'unassigned', \
         'artists/1/pic/nova.png', 'artists/1/track1/one.mp3', 'artists/1/track2/two.mp3') \
         RETURNING id",
    )
    .fetch_one(&state.db)
    .await
    .unwrap();
    r2.insert(state, "artists/1/pic/nova.png", &png());
    r2.insert(state, "artists/1/track1/one.mp3", b"track one");
    r2.insert(state, "artists/1/track2/two.mp3", b"track two");
    id
}

fn request(cookie: Option<&HeaderMap>) -> Request<Body> {
    let mut request = Request::builder().body(Body::empty()).unwrap();
    if let Some(headers) = cookie {
        request.headers_mut().extend(headers.clone());
    }
    request
}

/// Artist files are for admins only: without a login 401, a host 403, an
/// admin gets the file.
#[tokio::test]
async fn only_admins_download_artist_files() {
    let (state, _temp, r2) = setup(json!({})).await;
    let artist = add_artist(&state, &r2).await;
    let host = headers(&state, add_user(&state, "host", "host").await).await;
    let admin = headers(&state, add_user(&state, "admin", "admin").await).await;

    for (route, expected_type) in [
        ("package", "application/zip"),
        ("audio", "application/zip"),
        ("images", "application/zip"),
        ("pdf", "application/pdf"),
    ] {
        let call = |user: Option<&HeaderMap>| {
            let state = state.clone();
            let request = request(user);
            async move {
                let path = Path(artist);
                let state = State(state);
                match route {
                    "package" => download::download_artist(state, path, request).await,
                    "audio" => download::download_artist_audio(state, path, request).await,
                    "images" => download::download_artist_images(state, path, request).await,
                    _ => download::download_artist_pdf(state, path, request).await,
                }
                .into_response()
            }
        };

        assert_eq!(
            call(None).await.status(),
            StatusCode::UNAUTHORIZED,
            "{route}"
        );
        assert_eq!(
            call(Some(&host)).await.status(),
            StatusCode::FORBIDDEN,
            "{route}"
        );
        let response = call(Some(&admin)).await;
        assert_eq!(response.status(), StatusCode::OK, "{route}");
        assert_eq!(response.headers()["content-type"], expected_type, "{route}");
    }

    // The show packages hold the same artist files.
    for user in [None, Some(&host)] {
        let response = download::download_show_package(
            State(state.clone()),
            Path((1, "all-data".to_string())),
            request(user),
        )
        .await
        .into_response();
        let expected = match user {
            None => StatusCode::UNAUTHORIZED,
            Some(_) => StatusCode::FORBIDDEN,
        };
        assert_eq!(response.status(), expected);
    }
}

// ── Admin: chunked show recording upload ────────────────────────────────────

async fn recording_init(
    state: &Arc<AppState>,
    show: i64,
    user: &HeaderMap,
    total_chunks: u32,
) -> crate::Result<Json<upload_recording_chunked::InitResponse>> {
    upload_recording_chunked::init_recording_upload(
        State(state.clone()),
        Path(show),
        user.clone(),
        Json(upload_recording_chunked::InitRequest {
            filename: "show.mp3".to_string(),
            total_size: 9,
            total_chunks,
            peaks: None,
        }),
    )
    .await
}

async fn recording_chunk(
    state: &Arc<AppState>,
    show: i64,
    session: &str,
    user: &HeaderMap,
    index: u32,
    data: &[u8],
) -> StatusCode {
    status(
        upload_recording_chunked::upload_recording_chunk(
            State(state.clone()),
            Path((show, session.to_string())),
            Query(upload_recording_chunked::ChunkQuery { index }),
            user.clone(),
            form(&[("chunk", Some("blob"), data)]).await,
        )
        .await,
    )
}

async fn recording_finalize(
    state: &Arc<AppState>,
    show: i64,
    session: &str,
    user: &HeaderMap,
) -> StatusCode {
    status(
        upload_recording_chunked::finalize_recording_upload(
            State(state.clone()),
            Path((show, session.to_string())),
            user.clone(),
            Json(upload_recording_chunked::FinalizeRequest::default()),
        )
        .await,
    )
}

/// An admin uploads a show recording in chunks, out of order. The finalize
/// waits for every chunk, joins them in order, stores the recording on the
/// show and removes the chunks. Hosts, other shows, chunks past the end and
/// expired sessions are refused.
#[tokio::test]
async fn a_chunked_show_recording_is_joined_in_order() {
    let (state, _temp, r2) = setup(json!({})).await;
    for id in [1, 2] {
        sqlx::query("INSERT INTO shows (id, title, date) VALUES (?, 'Night Shift', '2026-06-01')")
            .bind(id)
            .execute(&state.db)
            .await
            .unwrap();
    }
    let host = headers(&state, add_user(&state, "host", "host").await).await;
    let admin = headers(&state, add_user(&state, "admin", "admin").await).await;

    assert_eq!(
        status_of(recording_init(&state, 1, &host, 3).await),
        StatusCode::FORBIDDEN
    );
    let session = recording_init(&state, 1, &admin, 3)
        .await
        .unwrap()
        .0
        .session_id;

    assert_eq!(
        recording_chunk(&state, 1, &session, &host, 0, b"aaa").await,
        StatusCode::FORBIDDEN
    );
    assert_eq!(
        recording_chunk(&state, 2, &session, &admin, 0, b"aaa").await,
        StatusCode::BAD_REQUEST,
        "another show"
    );
    assert_eq!(
        recording_chunk(&state, 1, &session, &admin, 3, b"ddd").await,
        StatusCode::BAD_REQUEST,
        "past the end"
    );
    for (index, data) in [(2, b"ccc"), (0, b"aaa")] {
        assert_eq!(
            recording_chunk(&state, 1, &session, &admin, index, data).await,
            StatusCode::OK
        );
    }
    assert_eq!(
        recording_finalize(&state, 1, &session, &admin).await,
        StatusCode::BAD_REQUEST,
        "a chunk is missing"
    );
    assert_eq!(
        recording_chunk(&state, 1, &session, &admin, 1, b"bbb").await,
        StatusCode::OK
    );

    assert_eq!(
        recording_finalize(&state, 1, &session, &admin).await,
        StatusCode::OK
    );

    let key: String = sqlx::query_scalar("SELECT recording_key FROM shows WHERE id = 1")
        .fetch_one(&state.db)
        .await
        .unwrap();
    assert_eq!(r2.object(&state, &key).unwrap(), b"aaabbbccc");
    assert!(r2
        .keys_in(&state.config.r2_bucket_name, "pending-recordings/")
        .is_empty());
    assert_eq!(
        recording_finalize(&state, 1, &session, &admin).await,
        StatusCode::BAD_REQUEST,
        "the session is used up"
    );

    // An upload session lives two hours; after that it is refused.
    let late = recording_init(&state, 1, &admin, 1)
        .await
        .unwrap()
        .0
        .session_id;
    let in_two_hours: bool = sqlx::query_scalar(
        "SELECT expires_at > datetime('now', '+119 minutes') \
         AND expires_at < datetime('now', '+121 minutes') \
         FROM pending_recording_uploads WHERE session_id = ?",
    )
    .bind(&late)
    .fetch_one(&state.db)
    .await
    .unwrap();
    assert!(in_two_hours);
    sqlx::query(
        "UPDATE pending_recording_uploads SET expires_at = datetime('now', '-1 minute') \
         WHERE session_id = ?",
    )
    .bind(&late)
    .execute(&state.db)
    .await
    .unwrap();
    assert_eq!(
        recording_chunk(&state, 1, &late, &admin, 0, b"aaa").await,
        StatusCode::BAD_REQUEST
    );
}
