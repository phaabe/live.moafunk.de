//! SoundCloud against a fake API, a fake R2 and real SQLite: tokens, the
//! upload request, playlists and error paths. These pin today's behavior
//! before the refresh-token and multi-part upload work.

use super::*;
use crate::fake_r2::FakeR2;
use axum::{
    body::Body,
    extract::{FromRequest, Multipart, Request, State},
    http::{header, Method, StatusCode, Uri},
    response::{IntoResponse, Response},
    Json,
};
use serde_json::{json, Value};
use std::cell::RefCell;
use std::collections::HashMap;
use std::sync::Mutex;

thread_local! {
    /// API base of this test thread. The default is a dead local port, so a
    /// test can never reach the real SoundCloud.
    pub(super) static API_BASE: RefCell<String> = RefCell::new("http://127.0.0.1:1".to_string());
}

/// One API call: `METHOD /path`, the auth header and the body. Multipart text
/// fields are strings; a file part is `{"file": name, "len": n}`.
#[derive(Debug, Clone)]
struct ScCall {
    route: String,
    auth: Option<String>,
    body: Value,
}

/// A playlist as `(id, title, track ids)`.
type Playlist = (i64, String, Vec<i64>);

#[derive(Clone, Default)]
struct FakeSoundCloud {
    calls: Arc<Mutex<Vec<ScCall>>>,
    /// The caller's playlists.
    playlists: Arc<Mutex<Vec<Playlist>>>,
    /// Routes that fail with this status.
    failing: Arc<Mutex<HashMap<String, u16>>>,
    /// `permalink_url` of an uploaded track; `None` leaves it out.
    permalink: Arc<Mutex<Option<String>>>,
}

impl FakeSoundCloud {
    async fn serve() -> Self {
        let fake = Self::default();
        *fake.permalink.lock().unwrap() = Some("https://soundcloud.com/radio-moafunk/new".into());
        let app = axum::Router::new()
            .fallback(fake_request)
            .with_state(fake.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        API_BASE.with(|b| *b.borrow_mut() = base);
        fake
    }

    fn routes(&self) -> Vec<String> {
        self.calls
            .lock()
            .unwrap()
            .iter()
            .map(|c| c.route.clone())
            .collect()
    }

    fn call(&self, route: &str) -> ScCall {
        self.calls
            .lock()
            .unwrap()
            .iter()
            .find(|c| c.route == route)
            .cloned()
            .unwrap_or_else(|| panic!("no call {route}: {:?}", self.routes()))
    }

    fn fail(&self, route: &str, status: u16) {
        self.failing
            .lock()
            .unwrap()
            .insert(route.to_string(), status);
    }
}

async fn fake_request(
    State(fake): State<FakeSoundCloud>,
    method: Method,
    uri: Uri,
    request: Request,
) -> Response {
    let route = format!("{method} {}", uri.path());
    let auth = request
        .headers()
        .get(header::AUTHORIZATION)
        .map(|v| v.to_str().unwrap().to_string());
    let content_type = request
        .headers()
        .get(header::CONTENT_TYPE)
        .map(|v| v.to_str().unwrap().to_string())
        .unwrap_or_default();
    let body = if content_type.starts_with("multipart/form-data") {
        let mut form = Multipart::from_request(request.map(Body::new), &())
            .await
            .unwrap();
        let mut fields = serde_json::Map::new();
        while let Some(field) = form.next_field().await.unwrap() {
            let name = field.name().unwrap().to_string();
            let value = match field.file_name().map(str::to_string) {
                Some(file) => json!({"file": file, "len": field.bytes().await.unwrap().len()}),
                None => json!(field.text().await.unwrap()),
            };
            fields.insert(name, value);
        }
        Value::Object(fields)
    } else {
        let bytes = axum::body::to_bytes(request.into_body(), usize::MAX)
            .await
            .unwrap();
        serde_json::from_slice(&bytes).unwrap_or_else(|_| {
            // A form body (the OAuth token exchange).
            let pairs: Vec<(String, String)> = serde_urlencoded::from_bytes(&bytes).unwrap();
            json!(pairs.into_iter().collect::<HashMap<_, _>>())
        })
    };
    fake.calls.lock().unwrap().push(ScCall {
        route: route.clone(),
        auth,
        body: body.clone(),
    });

    if let Some(status) = fake.failing.lock().unwrap().get(&route) {
        return (StatusCode::from_u16(*status).unwrap(), "nope").into_response();
    }
    let path: Vec<&str> = uri.path().trim_start_matches('/').split('/').collect();
    let mut playlists = fake.playlists.lock().unwrap();
    match (method.as_str(), path.as_slice()) {
        ("POST", ["oauth", "token"]) => Json(json!({
            "access_token": "new-access", "refresh_token": "new-refresh",
            "expires_in": 3600, "token_type": "bearer"
        }))
        .into_response(),
        ("POST", ["tracks"]) => {
            let mut track = json!({ "id": 555 });
            if let Some(url) = fake.permalink.lock().unwrap().clone() {
                track["permalink_url"] = json!(url);
            }
            (StatusCode::CREATED, Json(track)).into_response()
        }
        ("DELETE" | "PUT", ["tracks", _]) => Json(json!({})).into_response(),
        ("GET", ["me", "playlists"]) => Json(
            playlists
                .iter()
                .map(|(id, title, _)| json!({"id": id, "title": title}))
                .collect::<Vec<_>>(),
        )
        .into_response(),
        ("GET", ["playlists", id]) => {
            let id: i64 = id.parse().unwrap();
            let (_, _, tracks) = playlists.iter().find(|p| p.0 == id).unwrap();
            let tracks: Vec<Value> = tracks.iter().map(|t| json!({ "id": t })).collect();
            Json(json!({ "id": id, "tracks": tracks })).into_response()
        }
        ("PUT", ["playlists", _]) => Json(json!({})).into_response(),
        ("POST", ["playlists"]) => {
            let title = body["playlist"]["title"].as_str().unwrap().to_string();
            playlists.push((900, title.clone(), vec![]));
            (
                StatusCode::CREATED,
                Json(json!({ "id": 900, "title": title })),
            )
                .into_response()
        }
        _ => StatusCode::NOT_FOUND.into_response(),
    }
}

/// A fake SoundCloud, a fake R2 and a state with a static token.
async fn setup(overrides: Value) -> (Arc<AppState>, tempfile::TempDir, FakeSoundCloud, FakeR2) {
    let (r2, endpoint) = FakeR2::serve().await;
    let mut config = json!({ "soundcloud_access_token": "static-token", "r2_endpoint": endpoint });
    config
        .as_object_mut()
        .unwrap()
        .extend(overrides.as_object().unwrap().clone());
    let (state, temp) = crate::broadcast_tests::state_with(config).await;
    let fake = FakeSoundCloud::serve().await;
    (state, temp, fake, r2)
}

async fn add_show(state: &Arc<AppState>, title: &str, bio: Option<&str>) -> i64 {
    sqlx::query_scalar(
        "INSERT INTO shows (title, date, ai_bio) VALUES (?, '2099-01-02', ?) RETURNING id",
    )
    .bind(title)
    .bind(bio)
    .fetch_one(&state.db)
    .await
    .unwrap()
}

async fn add_artist(
    state: &Arc<AppState>,
    show: i64,
    name: &str,
    sc: Option<&str>,
    ig: Option<&str>,
) {
    let id: i64 = sqlx::query_scalar(
        "INSERT INTO artists (name, pronouns, track1_name, track2_name, soundcloud, instagram) \
         VALUES (?, 'they/them', 'One', 'Two', ?, ?) RETURNING id",
    )
    .bind(name)
    .bind(sc)
    .bind(ig)
    .fetch_one(&state.db)
    .await
    .unwrap();
    sqlx::query("INSERT INTO artist_show_assignments (artist_id, show_id) VALUES (?, ?)")
        .bind(id)
        .bind(show)
        .execute(&state.db)
        .await
        .unwrap();
}

async fn show(state: &Arc<AppState>, id: i64) -> models::Show {
    sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(id)
        .fetch_one(&state.db)
        .await
        .unwrap()
}

async fn stored_token(state: &Arc<AppState>) -> Option<String> {
    get_stored_token(state).await.unwrap()
}

/// A show with a manually attached recording in R2.
async fn show_with_recording(state: &Arc<AppState>, r2: &FakeR2) -> i64 {
    let id = add_show(state, "Late Night", Some("Show bio")).await;
    sqlx::query("UPDATE shows SET recording_key = 'shows/rec.mp3', recording_filename = 'set.mp3' WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await
        .unwrap();
    r2.insert(state, "shows/rec.mp3", b"recording");
    id
}

#[test]
fn auto_upload_only_when_enabled_and_configured() {
    // Happy path: on + configured (fires even if the show already has a
    // track — a new recording replaces that track's audio).
    assert!(should_auto_upload(true, true));

    // Either blocker prevents the upload.
    assert!(!should_auto_upload(false, true)); // kill-switch off
    assert!(!should_auto_upload(true, false)); // not configured
    assert!(!should_auto_upload(false, false));
}

// ── Tokens ──────────────────────────────────────────────────────────────────

/// The static token wins, then the stored one; without both the admin is
/// sent to the auth flow.
#[tokio::test]
async fn access_token_prefers_config_then_database() {
    let (state, _temp) = crate::broadcast_tests::state().await;
    assert!(!is_configured(&state));
    assert!(!has_token(&state).await);
    let err = get_access_token(&state).await.unwrap_err().to_string();
    assert!(err.contains("Visit /api/soundcloud/auth"), "{err}");

    store_token(&state, "first").await.unwrap();
    store_token(&state, "second").await.unwrap();
    assert_eq!(get_access_token(&state).await.unwrap(), "second");
    assert!(has_token(&state).await);

    delete_stored_token(&state).await.unwrap();
    assert!(!has_token(&state).await);

    let (state, _temp, _fake, _r2) = setup(json!({})).await;
    store_token(&state, "stored").await.unwrap();
    assert_eq!(get_access_token(&state).await.unwrap(), "static-token");
    assert!(is_configured(&state));
}

#[tokio::test]
async fn auth_url_and_status() {
    let (state, _temp) = crate::broadcast_tests::state_with(json!({
        "soundcloud_client_id": "cid",
        "soundcloud_redirect_uri": "https://live.test/api/soundcloud/callback"
    }))
    .await;
    assert_eq!(
        get_auth_url(&state).unwrap(),
        "https://secure.soundcloud.com/authorize?response_type=code&client_id=cid\
         &redirect_uri=https%3A%2F%2Flive.test%2Fapi%2Fsoundcloud%2Fcallback"
    );
    let status = get_status(&state).await;
    assert!(status.configured && !status.authorized);
    assert!(status.auth_url.is_some());

    let (state, _temp) = crate::broadcast_tests::state().await;
    assert!(get_auth_url(&state).is_err());
    let status = get_status(&state).await;
    assert!(!status.configured && !status.authorized && status.auth_url.is_none());
}

/// The code exchange stores only the access token. The refresh token in the
/// answer is dropped today (https://github.com/phaabe/live.moafunk.de/issues/302).
#[tokio::test]
async fn code_exchange_stores_the_access_token() {
    let (state, _temp, fake, _r2) = setup(json!({
        "soundcloud_access_token": null,
        "soundcloud_client_id": "cid",
        "soundcloud_client_secret": "secret",
        "soundcloud_redirect_uri": "https://live.test/cb"
    }))
    .await;

    assert_eq!(
        exchange_code(&state, "the-code").await.unwrap(),
        "new-access"
    );
    let call = fake.call("POST /oauth/token");
    assert_eq!(
        call.body,
        json!({
            "grant_type": "authorization_code", "code": "the-code",
            "client_id": "cid", "client_secret": "secret", "redirect_uri": "https://live.test/cb"
        })
    );
    assert_eq!(stored_token(&state).await.as_deref(), Some("new-access"));
    let keys: Vec<String> =
        sqlx::query_scalar("SELECT key FROM app_settings WHERE key LIKE 'soundcloud%'")
            .fetch_all(&state.db)
            .await
            .unwrap();
    assert_eq!(keys, vec!["soundcloud_access_token"]);
    assert_eq!(get_access_token(&state).await.unwrap(), "new-access");

    // A refused code keeps the old token.
    fake.fail("POST /oauth/token", 401);
    let err = exchange_code(&state, "bad").await.unwrap_err().to_string();
    assert!(err.contains("401") && err.contains("nope"), "{err}");
    assert_eq!(stored_token(&state).await.as_deref(), Some("new-access"));
}

#[tokio::test]
async fn code_exchange_needs_client_credentials() {
    let (state, _temp, fake, _r2) = setup(json!({ "soundcloud_client_id": "cid" })).await;
    let err = exchange_code(&state, "c").await.unwrap_err().to_string();
    assert!(
        err.contains("SOUNDCLOUD_CLIENT_SECRET not configured"),
        "{err}"
    );
    assert!(fake.routes().is_empty());
}

// ── Title and description ───────────────────────────────────────────────────

#[tokio::test]
async fn title_lists_artists_by_name() {
    let (state, _temp) = crate::broadcast_tests::state().await;
    let id = add_show(&state, "Late Night", None).await;
    assert_eq!(
        build_title(&state, &show(&state, id).await).await.unwrap(),
        "Late Night"
    );
    add_artist(&state, id, "zoe", None, None).await;
    add_artist(&state, id, "Adam", None, None).await;
    assert_eq!(
        build_title(&state, &show(&state, id).await).await.unwrap(),
        "Late Night — Adam, zoe"
    );
}

#[tokio::test]
async fn description_has_bio_and_artist_links() {
    let (state, _temp) = crate::broadcast_tests::state().await;
    let id = add_show(&state, "Late Night", Some("Show bio")).await;
    assert_eq!(build_description(&state, id).await.unwrap(), "Show bio");

    add_artist(&state, id, "Nova", Some("@nova"), Some("@nova.ig")).await;
    add_artist(&state, id, "Kai", Some("https://soundcloud.com/kai"), None).await;
    add_artist(&state, id, "Mute", Some(""), Some("")).await;
    assert_eq!(
        build_description(&state, id).await.unwrap(),
        "Show bio\n\n\n\n\
         Kai\nSoundCloud: https://soundcloud.com/kai\n\n\
         Nova\nSoundCloud: https://soundcloud.com/nova\nInstagram: https://instagram.com/nova.ig"
    );

    let none = add_show(&state, "Empty", None).await;
    assert_eq!(build_description(&state, none).await.unwrap(), "");
    assert!(build_description(&state, 999).await.is_err());
}

// ── Upload ──────────────────────────────────────────────────────────────────

/// The upload is a private track with title, description, tags, the audio
/// and the show cover; the result lands on the show and in its playlist.
#[tokio::test]
async fn upload_sends_a_private_track_and_stores_it() {
    let (state, _temp, fake, r2) = setup(json!({})).await;
    let id = show_with_recording(&state, &r2).await;
    add_artist(&state, id, "Nova", None, None).await;
    r2.insert(&state, &format!("shows/{id}/cover.png"), b"cover");

    let result = upload_track(&state, id).await.unwrap();
    assert!(result.success);
    assert_eq!(result.track_id.as_deref(), Some("555"));
    assert_eq!(
        result.track_url.as_deref(),
        Some("https://soundcloud.com/radio-moafunk/new")
    );

    let upload = fake.call("POST /tracks");
    assert_eq!(upload.auth.as_deref(), Some("OAuth static-token"));
    assert_eq!(
        upload.body,
        json!({
            "track[title]": "Late Night — Nova",
            // An artist without socials leaves the blank separator.
            "track[description]": "Show bio\n\n",
            "track[sharing]": "private",
            "track[tag_list]": "moafunk radio live",
            "track[asset_data]": {"file": "set.mp3", "len": 9},
            "track[artwork_data]": {"file": "cover.png", "len": 5},
        })
    );

    let stored = show(&state, id).await;
    assert_eq!(stored.soundcloud_track_id.as_deref(), Some("555"));
    assert_eq!(
        stored.soundcloud_url.as_deref(),
        Some("https://soundcloud.com/radio-moafunk/new")
    );
    assert!(stored.soundcloud_uploaded_at.is_some());
    assert_eq!(stored.soundcloud_public, Some(false));

    // A new playlist named like the show, holding the track.
    let create = fake.call("POST /playlists");
    assert_eq!(
        create.body,
        json!({"playlist": {"title": "Late Night", "sharing": "private", "tracks": [{"id": 555}]}})
    );
}

/// Without a manual recording, the newest finalized version wins over the
/// newest streamed archive; the archive comes from the shows bucket.
#[tokio::test]
async fn upload_source_order() {
    let (state, _temp, fake, r2) = setup(json!({})).await;
    let id = add_show(&state, "Late Night", None).await;

    let err = upload_track(&state, id).await.unwrap_err().to_string();
    assert!(err.contains("Show has no recording to upload"), "{err}");

    sqlx::query(
        "INSERT INTO recording_versions (show_id, version, status, archive_key) \
         VALUES (?, 'v1', 'raw', 'archive/v1.mp3')",
    )
    .bind(id)
    .execute(&state.db)
    .await
    .unwrap();
    r2.objects.lock().unwrap().insert(
        format!("{}/archive/v1.mp3", state.config.r2_shows_bucket_name),
        b"archived".to_vec(),
    );
    upload_track(&state, id).await.unwrap();
    assert_eq!(
        fake.call("POST /tracks").body["track[asset_data]"],
        json!({"file": "v1.mp3", "len": 8})
    );
    // No cover in R2: the upload goes out without artwork.
    assert!(fake
        .call("POST /tracks")
        .body
        .get("track[artwork_data]")
        .is_none());

    sqlx::query(
        "INSERT INTO recording_versions (show_id, version, status, final_key) \
         VALUES (?, 'v0', 'finalized', 'final/v0.mp3')",
    )
    .bind(id)
    .execute(&state.db)
    .await
    .unwrap();
    r2.insert(&state, "final/v0.mp3", b"final");
    fake.calls.lock().unwrap().clear();
    upload_track(&state, id).await.unwrap();
    assert_eq!(
        fake.call("POST /tracks").body["track[asset_data]"],
        json!({"file": "v0.mp3", "len": 5})
    );
}

#[tokio::test]
async fn upload_errors() {
    let (state, _temp, fake, r2) = setup(json!({ "soundcloud_access_token": null })).await;
    let id = show_with_recording(&state, &r2).await;
    // No token: nothing is sent.
    assert!(upload_track(&state, id).await.is_err());
    assert!(fake.routes().is_empty());

    store_token(&state, "stored").await.unwrap();
    assert!(upload_track(&state, 999).await.is_err());

    // The recording is missing in R2.
    r2.objects.lock().unwrap().clear();
    assert!(upload_track(&state, id).await.is_err());
    assert!(fake.routes().is_empty());

    // SoundCloud refuses the upload: a failed result, the show is unchanged.
    r2.insert(&state, "shows/rec.mp3", b"recording");
    fake.fail("POST /tracks", 500);
    let result = upload_track(&state, id).await.unwrap();
    assert!(!result.success);
    assert_eq!(
        result.error.as_deref(),
        Some("SoundCloud API error (500 Internal Server Error): nope")
    );
    assert_eq!(
        fake.call("POST /tracks").auth.as_deref(),
        Some("OAuth stored")
    );
    assert_eq!(show(&state, id).await.soundcloud_track_id, None);
    assert!(!fake.routes().iter().any(|r| r.contains("playlists")));
}

/// A new recording replaces the show's track: delete the old one first. A
/// failed delete does not stop the new upload.
#[tokio::test]
async fn reupload_deletes_the_old_track_first() {
    let (state, _temp, fake, r2) = setup(json!({})).await;
    let id = show_with_recording(&state, &r2).await;
    sqlx::query("UPDATE shows SET soundcloud_track_id = '111', soundcloud_public = 1 WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await
        .unwrap();
    *fake.permalink.lock().unwrap() = None;

    upload_track(&state, id).await.unwrap();
    let routes = fake.routes();
    assert_eq!(&routes[..2], &["DELETE /tracks/111", "POST /tracks"]);
    let stored = show(&state, id).await;
    assert_eq!(stored.soundcloud_track_id.as_deref(), Some("555"));
    // Without a permalink in the answer, the URL is built from the id.
    assert_eq!(
        stored.soundcloud_url.as_deref(),
        Some("https://soundcloud.com/radio-moafunk/555")
    );
    // The new track starts private.
    assert_eq!(stored.soundcloud_public, Some(false));

    sqlx::query("UPDATE shows SET soundcloud_track_id = '222' WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await
        .unwrap();
    fake.fail("DELETE /tracks/222", 500);
    assert!(upload_track(&state, id).await.unwrap().success);
}

// ── Playlists ───────────────────────────────────────────────────────────────

/// An existing playlist (title matched without case or spaces) gets the
/// union of its tracks and the new one; a track already in it is not sent.
#[tokio::test]
async fn upload_adds_the_track_to_the_show_playlist() {
    let (state, _temp, fake, r2) = setup(json!({})).await;
    let id = show_with_recording(&state, &r2).await;
    fake.playlists.lock().unwrap().extend([
        (700, "Other".to_string(), vec![1]),
        (701, " late night ".to_string(), vec![1, 2]),
    ]);

    upload_track(&state, id).await.unwrap();
    assert_eq!(
        fake.call("GET /me/playlists").auth.as_deref(),
        Some("OAuth static-token")
    );
    assert_eq!(
        fake.call("PUT /playlists/701").body,
        json!({"playlist": {"tracks": [{"id": 1}, {"id": 2}, {"id": 555}]}})
    );
    assert!(!fake.routes().contains(&"POST /playlists".to_string()));

    fake.playlists.lock().unwrap()[1].2.push(555);
    fake.calls.lock().unwrap().clear();
    upload_track(&state, id).await.unwrap();
    assert!(fake.routes().contains(&"GET /playlists/701".to_string()));
    assert!(!fake.routes().contains(&"PUT /playlists/701".to_string()));
}

/// The playlist step is best effort: the upload still counts.
#[tokio::test]
async fn playlist_errors_do_not_fail_the_upload() {
    let (state, _temp, fake, r2) = setup(json!({})).await;
    let id = show_with_recording(&state, &r2).await;
    fake.fail("GET /me/playlists", 500);
    assert!(upload_track(&state, id).await.unwrap().success);
    assert_eq!(
        show(&state, id).await.soundcloud_track_id.as_deref(),
        Some("555")
    );

    // The helpers report each failing step.
    let client = reqwest::Client::new();
    let err = find_playlist_by_title(&client, "t", "x")
        .await
        .unwrap_err()
        .to_string();
    assert!(err.contains("List playlists failed (500"), "{err}");
    fake.fail("POST /playlists", 403);
    let err = create_playlist(&client, "t", "x", 1)
        .await
        .unwrap_err()
        .to_string();
    assert!(err.contains("Create playlist failed (403"), "{err}");
    fake.playlists
        .lock()
        .unwrap()
        .push((5, "x".to_string(), vec![]));
    fake.fail("PUT /playlists/5", 422);
    let err = set_playlist_tracks(&client, "t", 5, &[1])
        .await
        .unwrap_err()
        .to_string();
    assert!(err.contains("Update playlist failed (422"), "{err}");
    fake.fail("GET /playlists/5", 404);
    assert!(get_playlist_track_ids(&client, "t", 5).await.is_err());
    // A track that is already gone counts as deleted.
    fake.fail("DELETE /tracks/9", 404);
    assert!(delete_track(&client, "t", "9").await.is_ok());
    fake.fail("DELETE /tracks/9", 403);
    assert!(delete_track(&client, "t", "9").await.is_err());
}

// ── Privacy and auto-upload ─────────────────────────────────────────────────

#[tokio::test]
async fn privacy_toggle_updates_the_track() {
    let (state, _temp, fake, _r2) = setup(json!({})).await;
    let id = add_show(&state, "Late Night", None).await;
    let err = set_track_privacy(&state, id, true)
        .await
        .unwrap_err()
        .to_string();
    assert!(err.contains("has not been uploaded"), "{err}");

    sqlx::query("UPDATE shows SET soundcloud_track_id = '555', soundcloud_url = 'u', soundcloud_public = 0 WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await
        .unwrap();
    let result = set_track_privacy(&state, id, true).await.unwrap();
    assert!(result.success);
    assert_eq!(result.track_url.as_deref(), Some("u"));
    assert_eq!(
        fake.call("PUT /tracks/555").body,
        json!({"track": {"sharing": "public"}})
    );
    assert_eq!(show(&state, id).await.soundcloud_public, Some(true));

    fake.fail("PUT /tracks/555", 500);
    let result = set_track_privacy(&state, id, false).await.unwrap();
    assert!(!result.success);
    assert!(result.error.unwrap().contains("500"));
    assert_eq!(show(&state, id).await.soundcloud_public, Some(true));
}

#[tokio::test]
async fn auto_upload_runs_only_when_switched_on() {
    let (state, _temp, fake, r2) = setup(json!({ "soundcloud_auto_upload": false })).await;
    let id = show_with_recording(&state, &r2).await;
    auto_upload_on_finalize(&state, id).await;
    assert!(fake.routes().is_empty());

    let (state, _temp, fake, r2) = setup(json!({ "soundcloud_auto_upload": true })).await;
    let id = show_with_recording(&state, &r2).await;
    auto_upload_on_finalize(&state, 999).await;
    assert!(fake.routes().is_empty());
    auto_upload_on_finalize(&state, id).await;
    assert!(fake.routes().contains(&"POST /tracks".to_string()));
    assert_eq!(
        show(&state, id).await.soundcloud_track_id.as_deref(),
        Some("555")
    );
}
