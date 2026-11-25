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
    /// Permalink of the logged-in account (`GET /me`).
    me: Arc<Mutex<String>>,
    /// Track ids of the account (`GET /me/tracks`), in SoundCloud's order.
    /// Each track is one minute long.
    tracks: Arc<Mutex<Vec<i64>>>,
    /// `quota.upload_seconds_left` of `GET /me`; `None` leaves the quota out.
    seconds_left: Arc<Mutex<Option<i64>>>,
    /// Tracks that `GET /tracks/:id` reports as public; others are private.
    public: Arc<Mutex<Vec<i64>>>,
}

impl FakeSoundCloud {
    async fn serve() -> Self {
        let fake = Self::default();
        *fake.permalink.lock().unwrap() = Some("https://soundcloud.com/radio-moafunk/new".into());
        *fake.me.lock().unwrap() = "moafunk-test".into();
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
        ("POST", ["oauth", "token"]) if body["grant_type"] == "refresh_token" => Json(json!({
            "access_token": "refreshed-access", "refresh_token": "refreshed-refresh",
            "expires_in": 3600, "token_type": "bearer"
        }))
        .into_response(),
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
        ("GET", ["tracks", id]) => {
            let id: i64 = id.parse().unwrap();
            let sharing = if fake.public.lock().unwrap().contains(&id) {
                "public"
            } else {
                "private"
            };
            Json(json!({ "id": id, "sharing": sharing })).into_response()
        }
        ("GET", ["me"]) => {
            let mut me = json!({ "id": 1, "permalink": *fake.me.lock().unwrap() });
            if let Some(left) = *fake.seconds_left.lock().unwrap() {
                me["quota"] =
                    json!({ "unlimited_upload_quota": false, "upload_seconds_left": left });
            }
            Json(me).into_response()
        }
        ("GET", ["me", "tracks"]) => list_answer(
            &uri,
            fake.tracks
                .lock()
                .unwrap()
                .iter()
                .map(|id| json!({ "id": id, "duration": 60_000 }))
                .collect(),
        ),
        ("GET", ["me", "playlists"]) => list_answer(
            &uri,
            playlists
                .iter()
                .map(|(id, title, _)| json!({"id": id, "title": title}))
                .collect(),
        ),
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

/// A list as SoundCloud answers it: wrapped in `collection` for a paginated
/// request, a plain array without `linked_partitioning=true`.
fn list_answer(uri: &Uri, items: Vec<Value>) -> Response {
    if uri
        .query()
        .unwrap_or_default()
        .contains("linked_partitioning=true")
    {
        Json(json!({ "collection": items, "next_href": null })).into_response()
    } else {
        Json(items).into_response()
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

/// Value of a query parameter in a URL.
fn query_param(url: &str, key: &str) -> String {
    let prefix = format!("{key}=");
    url.split(['?', '&'])
        .find_map(|part| part.strip_prefix(prefix.as_str()))
        .unwrap_or_else(|| panic!("no {key} in {url}"))
        .to_string()
}

/// Open a login like the Connect button and return its OAuth state.
fn login_state(state: &Arc<AppState>) -> String {
    query_param(&get_auth_url(state).unwrap(), "state")
}

async fn stored_token(state: &Arc<AppState>) -> Option<String> {
    get_stored_token(state).await.unwrap().map(|t| t.access)
}

async fn stored_refresh(state: &Arc<AppState>) -> Option<String> {
    get_stored_token(state)
        .await
        .unwrap()
        .and_then(|t| t.refresh)
}

/// Seconds until the stored access token expires.
async fn stored_expires_in(state: &Arc<AppState>) -> Option<i64> {
    let at = get_stored_token(state).await.unwrap()?.expires_at?;
    Some(at - chrono::Utc::now().timestamp())
}

/// Move the stored expiry to `secs` from now (negative: already expired).
async fn set_expires_in(state: &Arc<AppState>, secs: i64) {
    sqlx::query("UPDATE app_settings SET value = ? WHERE key = 'soundcloud_token_expires_at'")
        .bind((chrono::Utc::now().timestamp() + secs).to_string())
        .execute(&state.db)
        .await
        .unwrap();
}

/// A state with client credentials and no static token, connected through the
/// code exchange (access "new-access", refresh "new-refresh", 1 h).
async fn connected() -> (Arc<AppState>, tempfile::TempDir, FakeSoundCloud, FakeR2) {
    let (state, temp, fake, r2) = setup(json!({
        "soundcloud_access_token": null,
        "soundcloud_client_id": "cid",
        "soundcloud_client_secret": "secret",
        "soundcloud_redirect_uri": "https://live.test/cb"
    }))
    .await;
    exchange_code(&state, "the-code", &login_state(&state))
        .await
        .unwrap();
    fake.calls.lock().unwrap().clear();
    (state, temp, fake, r2)
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

/// A show with a finalized live take in R2 and no own file.
async fn show_with_take(state: &Arc<AppState>, r2: &FakeR2) -> i64 {
    let id = add_show(state, "Late Night", Some("Show bio")).await;
    sqlx::query(
        "INSERT INTO recording_versions (show_id, version, status, final_key) \
         VALUES (?, 'v1', 'finalized', 'final/v1.mp3')",
    )
    .bind(id)
    .execute(&state.db)
    .await
    .unwrap();
    r2.insert(state, "final/v1.mp3", b"take");
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

    store_token(&state, "first", None, None).await.unwrap();
    store_token(&state, "second", None, None).await.unwrap();
    assert_eq!(get_access_token(&state).await.unwrap(), "second");
    assert!(has_token(&state).await);

    delete_stored_token(&state).await.unwrap();
    assert!(!has_token(&state).await);

    let (state, _temp, _fake, _r2) = setup(json!({})).await;
    store_token(&state, "stored", None, None).await.unwrap();
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
    let url = get_auth_url(&state).unwrap();
    assert!(
        url.starts_with(
            "https://secure.soundcloud.com/authorize?response_type=code&client_id=cid\
             &redirect_uri=https%3A%2F%2Flive.test%2Fapi%2Fsoundcloud%2Fcallback\
             &code_challenge="
        ),
        "{url}"
    );
    assert_eq!(query_param(&url, "code_challenge_method"), "S256");
    // RFC 7636 appendix B.
    assert_eq!(
        pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    );
    assert_eq!(query_param(&url, "code_challenge").len(), 43);
    // The state names the server, so production can forward staging logins.
    let oauth_state = query_param(&url, "state");
    assert!(oauth_state.starts_with("production."), "{oauth_state}");
    assert_eq!(oauth_state.len(), "production.".len() + 43);
    let again = get_auth_url(&state).unwrap();
    assert_ne!(query_param(&url, "state"), query_param(&again, "state"));
    let status = get_status(&state).await;
    assert!(status.configured && !status.authorized);
    assert!(status.auth_url.is_some());

    let (state, _temp) = crate::broadcast_tests::state().await;
    assert!(get_auth_url(&state).is_err());
    let status = get_status(&state).await;
    assert!(!status.configured && !status.authorized && status.auth_url.is_none());
}

/// The code exchange stores the access token, the refresh token and the expiry.
#[tokio::test]
async fn code_exchange_stores_the_tokens() {
    let (state, _temp, fake, _r2) = setup(json!({
        "soundcloud_access_token": null,
        "soundcloud_client_id": "cid",
        "soundcloud_client_secret": "secret",
        "soundcloud_redirect_uri": "https://live.test/cb"
    }))
    .await;

    let url = get_auth_url(&state).unwrap();
    assert_eq!(
        exchange_code(&state, "the-code", &query_param(&url, "state"))
            .await
            .unwrap(),
        "new-access"
    );
    let mut body = fake.call("POST /oauth/token").body;
    let verifier = body
        .as_object_mut()
        .unwrap()
        .remove("code_verifier")
        .unwrap();
    assert_eq!(
        pkce_challenge(verifier.as_str().unwrap()),
        query_param(&url, "code_challenge")
    );
    assert_eq!(
        body,
        json!({
            "grant_type": "authorization_code", "code": "the-code",
            "client_id": "cid", "client_secret": "secret", "redirect_uri": "https://live.test/cb"
        })
    );
    assert_eq!(stored_token(&state).await.as_deref(), Some("new-access"));
    assert_eq!(stored_refresh(&state).await.as_deref(), Some("new-refresh"));
    let expires_in = stored_expires_in(&state).await.unwrap();
    assert!((3590..=3600).contains(&expires_in), "{expires_in}");
    assert_eq!(get_access_token(&state).await.unwrap(), "new-access");
    assert_eq!(fake.routes().len(), 1, "a fresh token is not refreshed");

    // A refused code keeps the old tokens.
    fake.fail("POST /oauth/token", 401);
    let err = exchange_code(&state, "bad", &login_state(&state))
        .await
        .unwrap_err()
        .to_string();
    assert!(err.contains("401") && err.contains("nope"), "{err}");
    assert_eq!(stored_token(&state).await.as_deref(), Some("new-access"));
    assert_eq!(stored_refresh(&state).await.as_deref(), Some("new-refresh"));

    // A new access token without refresh token or expiry drops the old ones.
    store_token(&state, "manual", None, None).await.unwrap();
    assert_eq!(stored_refresh(&state).await, None);
    assert_eq!(stored_expires_in(&state).await, None);

    // Disconnect removes all three.
    store_token(&state, "a", Some("r"), Some(60)).await.unwrap();
    delete_stored_token(&state).await.unwrap();
    let left: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM app_settings WHERE key LIKE 'soundcloud%'")
            .fetch_one(&state.db)
            .await
            .unwrap();
    assert_eq!(left, 0);
}

/// A token that expires within 5 minutes is refreshed before the upload; the
/// new tokens are stored and used, and the next call needs no refresh.
#[tokio::test]
async fn expiring_token_is_refreshed_before_the_upload() {
    let (state, _temp, fake, r2) = connected().await;
    let id = show_with_recording(&state, &r2).await;
    set_expires_in(&state, 120).await;

    let result = upload_track(&state, id).await.unwrap();
    assert!(result.success, "{:?}", result.error);
    assert_eq!(
        fake.call("POST /oauth/token").body,
        json!({
            "grant_type": "refresh_token", "refresh_token": "new-refresh",
            "client_id": "cid", "client_secret": "secret"
        })
    );
    assert_eq!(
        fake.call("POST /tracks").auth.as_deref(),
        Some("OAuth refreshed-access")
    );
    assert_eq!(
        stored_token(&state).await.as_deref(),
        Some("refreshed-access")
    );
    assert_eq!(
        stored_refresh(&state).await.as_deref(),
        Some("refreshed-refresh")
    );
    assert!(stored_expires_in(&state).await.unwrap() > 3000);

    fake.calls.lock().unwrap().clear();
    assert_eq!(get_access_token(&state).await.unwrap(), "refreshed-access");
    assert!(fake.routes().is_empty());
}

/// A finalize after the token expired uploads without a manual reconnect.
#[tokio::test]
async fn finalize_after_expiry_refreshes_and_uploads() {
    let (state, _temp, fake, r2) = setup(json!({
        "soundcloud_access_token": null,
        "soundcloud_client_id": "cid",
        "soundcloud_client_secret": "secret",
        "soundcloud_auto_upload": true
    }))
    .await;
    exchange_code(&state, "the-code", &login_state(&state))
        .await
        .unwrap();
    let id = show_with_take(&state, &r2).await;
    set_expires_in(&state, -3600).await;
    assert!(
        has_token(&state).await,
        "a refresh token keeps it connected"
    );

    auto_upload_on_finalize(&state, id).await;
    assert_eq!(
        fake.call("POST /tracks").auth.as_deref(),
        Some("OAuth refreshed-access")
    );
    assert_eq!(
        show(&state, id).await.soundcloud_track_id.as_deref(),
        Some("555")
    );
}

/// Parallel callers refresh once: the second waits and uses the new token.
#[tokio::test]
async fn parallel_callers_refresh_once() {
    let (state, _temp, fake, _r2) = connected().await;
    set_expires_in(&state, -1).await;
    let (a, b) = tokio::join!(get_access_token(&state), get_access_token(&state));
    assert_eq!(a.unwrap(), "refreshed-access");
    assert_eq!(b.unwrap(), "refreshed-access");
    assert_eq!(fake.routes(), vec!["POST /oauth/token"]);
}

/// Without a refresh token, an expired token is not connected anymore: the
/// status asks to reconnect and nothing is sent to SoundCloud.
#[tokio::test]
async fn expired_token_without_refresh_token_needs_a_reconnect() {
    let (state, _temp, fake, _r2) = connected().await;
    store_token(&state, "old", None, Some(120)).await.unwrap();
    // Still valid for 2 minutes: used as it is.
    assert_eq!(get_access_token(&state).await.unwrap(), "old");
    assert!(has_token(&state).await);

    set_expires_in(&state, -1).await;
    assert!(!has_token(&state).await);
    assert!(!get_status(&state).await.authorized);
    let err = get_access_token(&state).await.unwrap_err().to_string();
    assert!(err.contains("Visit /api/soundcloud/auth"), "{err}");
    assert!(fake.routes().is_empty());
}

/// SoundCloud refusing the refresh token removes the stored tokens, so the
/// status shows "not connected". A server error keeps them for a retry.
#[tokio::test]
async fn refused_refresh_clears_the_tokens() {
    let (state, _temp, fake, _r2) = connected().await;
    set_expires_in(&state, -1).await;

    fake.fail("POST /oauth/token", 500);
    let err = get_access_token(&state).await.unwrap_err().to_string();
    assert!(err.contains("refresh failed (500"), "{err}");
    assert_eq!(stored_refresh(&state).await.as_deref(), Some("new-refresh"));
    assert!(has_token(&state).await);

    fake.fail("POST /oauth/token", 400);
    let err = get_access_token(&state).await.unwrap_err().to_string();
    assert!(err.contains("refresh failed (400"), "{err}");
    assert_eq!(stored_token(&state).await, None);
    assert!(!has_token(&state).await);
}

#[tokio::test]
async fn code_exchange_needs_client_credentials() {
    let (state, _temp, fake, _r2) = setup(json!({ "soundcloud_client_id": "cid" })).await;
    let err = exchange_code(&state, "c", &login_state(&state))
        .await
        .unwrap_err()
        .to_string();
    assert!(
        err.contains("SOUNDCLOUD_CLIENT_SECRET not configured"),
        "{err}"
    );
    assert!(fake.routes().is_empty());
}

/// With SOUNDCLOUD_ACCOUNT set, only a login of that account is stored.
#[tokio::test]
async fn code_exchange_refuses_another_account() {
    let (state, _temp, fake, _r2) = setup(json!({
        "soundcloud_access_token": null,
        "soundcloud_client_id": "cid",
        "soundcloud_client_secret": "secret",
        "soundcloud_account": "radio-moafunk"
    }))
    .await;
    let err = exchange_code(&state, "c", &login_state(&state))
        .await
        .unwrap_err()
        .to_string();
    assert!(
        err.contains("account 'radio-moafunk', but you logged in as 'moafunk-test'"),
        "{err}"
    );
    assert_eq!(
        fake.call("GET /me").auth.as_deref(),
        Some("OAuth new-access")
    );
    assert_eq!(stored_token(&state).await, None);
    assert!(!has_token(&state).await);

    // The right account (in any case) is stored.
    *fake.me.lock().unwrap() = "Radio-Moafunk".into();
    exchange_code(&state, "c", &login_state(&state))
        .await
        .unwrap();
    assert_eq!(stored_token(&state).await.as_deref(), Some("new-access"));
}

/// The check asks SoundCloud for the account with the current token, and
/// refreshes an expiring token first, like an upload would.
#[tokio::test]
async fn connection_check_returns_the_account() {
    let (state, _temp, fake, _r2) = connected().await;
    assert_eq!(check_connection(&state).await.unwrap(), "moafunk-test");
    assert_eq!(
        fake.call("GET /me").auth.as_deref(),
        Some("OAuth new-access")
    );

    set_expires_in(&state, -1).await;
    fake.calls.lock().unwrap().clear();
    assert_eq!(check_connection(&state).await.unwrap(), "moafunk-test");
    assert_eq!(fake.routes(), vec!["POST /oauth/token", "GET /me"]);

    delete_stored_token(&state).await.unwrap();
    let err = check_connection(&state).await.unwrap_err().to_string();
    assert!(err.contains("Visit /api/soundcloud/auth"), "{err}");
}

/// SoundCloud wraps a paginated list in `collection`. The old code read only
/// a plain array, so the staging cleanup and the show playlists failed live.
#[test]
fn list_items_reads_paginated_and_plain_lists() {
    let ids = |body: &str| -> Vec<i64> {
        list_items::<TrackSummary>(body)
            .unwrap()
            .into_iter()
            .map(|t| t.id)
            .collect()
    };
    assert_eq!(
        ids(r#"{"collection":[{"id":2},{"id":1}],"next_href":null}"#),
        vec![2, 1]
    );
    assert_eq!(ids(r#"[{"id":3}]"#), vec![3]);
    let err = list_items::<TrackSummary>(r#"{"errors":[]}"#)
        .unwrap_err()
        .to_string();
    assert!(err.contains("expected a sequence"), "{err}");
}

// ── Staging cleanup ─────────────────────────────────────────────────────────

/// The routes from the first `DELETE` to the upload (`POST /tracks`).
fn deletes_before_the_upload(fake: &FakeSoundCloud) -> Vec<String> {
    let routes = fake.routes();
    let upload = routes.iter().position(|r| r == "POST /tracks").unwrap();
    routes[..upload]
        .iter()
        .filter(|r| r.starts_with("DELETE"))
        .cloned()
        .collect()
}

/// On staging, an upload first deletes old tracks of the test account, so
/// that with the new track the newest SOUNDCLOUD_KEEP_TRACKS tracks stay.
#[tokio::test]
async fn staging_upload_keeps_the_newest_tracks() {
    let (state, _temp, fake, r2) = setup(json!({
        "app_env": "staging",
        "soundcloud_account": "moafunk-test",
        "soundcloud_keep_tracks": 2
    }))
    .await;
    *fake.tracks.lock().unwrap() = vec![300, 100, 200];
    let id = show_with_recording(&state, &r2).await;

    assert!(upload_track(&state, id).await.unwrap().success);
    assert_eq!(
        deletes_before_the_upload(&fake),
        vec!["DELETE /tracks/200", "DELETE /tracks/100"]
    );
    assert_eq!(
        fake.routes()
            .iter()
            .filter(|r| r.starts_with("DELETE"))
            .count(),
        2
    );
}

/// SoundCloud refuses every upload while the account has no upload time left.
/// So the oldest tracks go before the upload, until there is time again.
#[tokio::test]
async fn staging_upload_makes_room_on_an_account_over_its_upload_time() {
    let (state, _temp, fake, r2) = setup(json!({
        "app_env": "staging",
        "soundcloud_account": "moafunk-test",
        "soundcloud_keep_tracks": 10
    }))
    .await;
    *fake.tracks.lock().unwrap() = vec![300, 100, 200];
    // 90 s over: two tracks of one minute must go.
    *fake.seconds_left.lock().unwrap() = Some(-90);
    let id = show_with_recording(&state, &r2).await;

    assert!(upload_track(&state, id).await.unwrap().success);
    assert_eq!(
        deletes_before_the_upload(&fake),
        vec!["DELETE /tracks/200", "DELETE /tracks/100"]
    );
}

#[test]
fn tracks_to_keep_counts_tracks_and_upload_time() {
    // Newest first, lengths in minutes.
    let tracks = |minutes: &[i64]| -> Vec<TrackSummary> {
        minutes
            .iter()
            .enumerate()
            .map(|(i, m)| TrackSummary {
                id: 100 - i as i64,
                duration: Some(m * 60_000),
            })
            .collect()
    };
    let soak = tracks(&[92, 1, 1, 92]);
    // The upload adds one track: 2 of "keep 3" stay.
    assert_eq!(tracks_to_keep(&soak, 3, None, 0), 2);
    assert_eq!(tracks_to_keep(&soak, 1, None, 0), 0);
    // Time left: nothing more goes.
    assert_eq!(tracks_to_keep(&soak, 10, Some(1), 0), 4);
    // 66 minutes over: only the old soak track (92 min) goes.
    assert_eq!(tracks_to_keep(&soak, 10, Some(-66 * 60), 0), 3);
    // Exactly 0 left is still refused: one more track goes.
    assert_eq!(tracks_to_keep(&soak, 10, Some(-92 * 60), 0), 2);
    // Far over: everything goes.
    assert_eq!(tracks_to_keep(&soak, 10, Some(-100_000), 0), 0);
    // The tracks cut by the count already free time.
    assert_eq!(tracks_to_keep(&soak, 4, Some(-66 * 60), 0), 3);
    assert_eq!(tracks_to_keep(&[], 10, Some(-5), 0), 0);
    // The new track needs its time too: with 30 minutes left, a 92-minute
    // soak track first frees the old soak track (92 min).
    assert_eq!(tracks_to_keep(&soak, 10, Some(30 * 60), 92 * 60), 3);
    // A short track fits in the time left: nothing goes.
    assert_eq!(tracks_to_keep(&soak, 10, Some(30 * 60), 60), 4);
    // More than the account can ever hold: everything goes.
    assert_eq!(tracks_to_keep(&soak, 10, Some(0), 100_000), 0);
}

#[test]
fn upload_seconds_for_counts_an_mp3_never_shorter_than_it_is() {
    // 92 minutes at 64 kbit/s: exactly its length.
    assert_eq!(upload_seconds_for(92 * 60 * 8_000), 92 * 60);
    // The same at 320 kbit/s counts five times as long.
    assert_eq!(upload_seconds_for(92 * 60 * 40_000), 5 * 92 * 60);
    assert_eq!(upload_seconds_for(0), 0);
}

/// A long recording gets its upload time before the upload: the oldest tracks
/// go until more time is left than the new track takes.
#[tokio::test]
async fn staging_upload_frees_the_time_a_long_recording_takes() {
    let (state, _temp, fake, r2) = setup(json!({
        "app_env": "staging",
        "soundcloud_account": "moafunk-test",
        "soundcloud_keep_tracks": 10
    }))
    .await;
    *fake.tracks.lock().unwrap() = vec![300, 100, 200];
    // One minute left, the recording counts 150 s: two tracks of one minute go.
    *fake.seconds_left.lock().unwrap() = Some(60);
    let id = show_with_recording(&state, &r2).await;
    r2.insert(&state, "shows/rec.mp3", &vec![0u8; 150 * 8_000]);

    assert!(upload_track(&state, id).await.unwrap().success);
    assert_eq!(
        deletes_before_the_upload(&fake),
        vec!["DELETE /tracks/200", "DELETE /tracks/100"]
    );
}

/// The cleanup deletes nothing outside staging, without an account pin, or
/// with a token of another account. The upload itself still succeeds.
#[tokio::test]
async fn cleanup_never_touches_other_accounts() {
    for (overrides, me) in [
        (
            json!({ "app_env": "production", "soundcloud_account": "moafunk-test" }),
            "moafunk-test",
        ),
        (json!({ "app_env": "staging" }), "moafunk-test"),
        (
            json!({ "app_env": "staging", "soundcloud_account": "moafunk-test" }),
            "radio-moafunk",
        ),
        // A wrong pin on the station account itself.
        (
            json!({ "app_env": "staging", "soundcloud_account": "Radio-Moafunk" }),
            "radio-moafunk",
        ),
    ] {
        let mut config = json!({ "soundcloud_keep_tracks": 1 });
        config
            .as_object_mut()
            .unwrap()
            .extend(overrides.as_object().unwrap().clone());
        let (state, _temp, fake, r2) = setup(config).await;
        *fake.me.lock().unwrap() = me.into();
        *fake.tracks.lock().unwrap() = vec![555, 100, 200];
        let id = show_with_recording(&state, &r2).await;

        assert!(upload_track(&state, id).await.unwrap().success);
        let routes = fake.routes();
        assert!(
            !routes.iter().any(|r| r.starts_with("DELETE")),
            "{overrides} as {me}: {routes:?}"
        );
    }
}

/// A login state works once and for 10 minutes. Without one, nothing is sent
/// to SoundCloud and the admin is asked to click Connect again.
#[tokio::test]
async fn login_state_works_once_and_for_ten_minutes() {
    let (state, _temp, fake, _r2) = setup(json!({
        "soundcloud_access_token": null,
        "soundcloud_client_id": "cid",
        "soundcloud_client_secret": "secret"
    }))
    .await;
    let refused = |oauth_state: String| {
        let state = state.clone();
        async move {
            exchange_code(&state, "c", &oauth_state)
                .await
                .unwrap_err()
                .to_string()
        }
    };

    for oauth_state in ["", "made-up"] {
        let err = refused(oauth_state.to_string()).await;
        assert!(err.contains("Click Connect again"), "{err}");
    }
    assert!(fake.routes().is_empty());

    let used = login_state(&state);
    exchange_code(&state, "c", &used).await.unwrap();
    assert!(refused(used).await.contains("Click Connect again"));

    let old = login_state(&state);
    for login in PENDING_LOGINS.lock().unwrap().iter_mut() {
        if login.state == old {
            login.created -= LOGIN_TTL_SECS;
        }
    }
    assert!(refused(old).await.contains("Click Connect again"));
    assert_eq!(fake.routes(), vec!["POST /oauth/token"]);
}

/// SoundCloud has one redirect URI per app (production's). Production sends a
/// login started on staging on to SOUNDCLOUD_FORWARD_URL with the same code
/// and state; its own logins and anything without the setting stay here.
#[tokio::test]
async fn production_forwards_staging_logins() {
    let forward =
        json!({ "soundcloud_forward_url": "https://staging.test/api/soundcloud/callback" });
    let (prod, _temp) = crate::broadcast_tests::state_with(forward.clone()).await;
    assert_eq!(
        forward_login(&prod, "a b&c", "staging.xyz").as_deref(),
        Some("https://staging.test/api/soundcloud/callback?code=a+b%26c&state=staging.xyz")
    );
    assert_eq!(forward_login(&prod, "c", "production.xyz"), None);
    assert_eq!(forward_login(&prod, "c", ""), None);

    // Staging never forwards (no loop), and without the setting nothing is sent on.
    let mut on_staging = forward.clone();
    on_staging["app_env"] = json!("staging");
    let (staging, _temp) = crate::broadcast_tests::state_with(on_staging).await;
    assert_eq!(forward_login(&staging, "c", "staging.xyz"), None);
    let (plain, _temp) = crate::broadcast_tests::state().await;
    assert_eq!(forward_login(&plain, "c", "staging.xyz"), None);

    // Staging's own login states carry the prefix production looks for.
    let mut staging_app = json!({ "app_env": "staging", "soundcloud_client_id": "cid" });
    staging_app["soundcloud_redirect_uri"] = json!("https://admin.test/api/soundcloud/callback");
    let (staging, _temp) = crate::broadcast_tests::state_with(staging_app).await;
    let url = get_auth_url(&staging).unwrap();
    assert!(query_param(&url, "state").starts_with("staging."), "{url}");
    assert!(url.contains("redirect_uri=https%3A%2F%2Fadmin.test%2Fapi%2Fsoundcloud%2Fcallback"));
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
    add_artist(
        &state,
        id,
        "Kai",
        Some("https://soundcloud.com/kai"),
        Some("https://www.instagram.com/kai/"),
    )
    .await;
    add_artist(&state, id, "Mute", Some(""), Some("")).await;
    assert_eq!(
        build_description(&state, id).await.unwrap(),
        "Show bio\n\n\n\n\
         Kai\nSoundCloud: https://soundcloud.com/kai\nInstagram: https://www.instagram.com/kai/\n\n\
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
    // Which file went up.
    assert_eq!(
        (
            stored.soundcloud_source.as_deref(),
            stored.soundcloud_source_key.as_deref(),
            stored.soundcloud_edited
        ),
        (Some("own"), Some("shows/rec.mp3"), Some(false))
    );

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

/// A rendered edit of the current recording goes up instead of it, named
/// after it; an edit of an older recording does not.
#[tokio::test]
async fn upload_prefers_the_edited_recording() {
    let (state, _temp, fake, r2) = setup(json!({})).await;
    let id = show_with_recording(&state, &r2).await;
    sqlx::query(
        "INSERT INTO recording_edits (show_id, source, edit_json, status, edited_key, source_key) \
         VALUES (?, 'own', '{}', 'done', 'recordings/1/edited/a.mp3', 'shows/rec.mp3')",
    )
    .bind(id)
    .execute(&state.db)
    .await
    .unwrap();
    r2.insert(&state, "recordings/1/edited/a.mp3", b"edited!");
    upload_track(&state, id).await.unwrap();
    assert_eq!(
        fake.call("POST /tracks").body["track[asset_data]"],
        json!({"file": "set-edited.mp3", "len": 7})
    );
    let stored = show(&state, id).await;
    assert_eq!(
        (
            stored.soundcloud_source_key.as_deref(),
            stored.soundcloud_edited
        ),
        (Some("recordings/1/edited/a.mp3"), Some(true))
    );

    sqlx::query("UPDATE recording_edits SET source_key = 'shows/older.mp3' WHERE show_id = ?")
        .bind(id)
        .execute(&state.db)
        .await
        .unwrap();
    fake.calls.lock().unwrap().clear();
    upload_track(&state, id).await.unwrap();
    assert_eq!(
        fake.call("POST /tracks").body["track[asset_data]"],
        json!({"file": "set.mp3", "len": 9})
    );
}

#[tokio::test]
async fn upload_errors() {
    let (state, _temp, fake, r2) = setup(json!({ "soundcloud_access_token": null })).await;
    let id = show_with_recording(&state, &r2).await;
    // No token: nothing is sent.
    assert!(upload_track(&state, id).await.is_err());
    assert!(fake.routes().is_empty());

    store_token(&state, "stored", None, None).await.unwrap();
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

/// The booking stays; the announcement tick unsets it only if the track is
/// still private at the booked time.
#[tokio::test]
async fn back_to_private_keeps_the_booked_post_time() {
    let (state, _temp, _fake, _r2) = setup(json!({})).await;
    let id = add_show(&state, "Late Night", None).await;
    sqlx::query("UPDATE shows SET soundcloud_track_id = '555', soundcloud_public = 1 WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await
        .unwrap();
    sqlx::query(
        "INSERT INTO announcements (show_id, slot, state, scheduled_at) \
         VALUES (?, 'post', 'scheduled', '2026-11-22T17:00:00Z'), (?, 'pre', 'scheduled', '2026-11-20T17:00:00Z')",
    )
    .bind(id)
    .bind(id)
    .execute(&state.db)
    .await
    .unwrap();

    assert!(set_track_privacy(&state, id, false).await.unwrap().success);
    let rows: Vec<(String, String, Option<String>)> = sqlx::query_as(
        "SELECT slot, state, scheduled_at FROM announcements WHERE show_id = ? ORDER BY slot",
    )
    .bind(id)
    .fetch_all(&state.db)
    .await
    .unwrap();
    assert_eq!(
        rows,
        [
            (
                "post".into(),
                "scheduled".into(),
                Some("2026-11-22T17:00:00Z".into())
            ),
            (
                "pre".into(),
                "scheduled".into(),
                Some("2026-11-20T17:00:00Z".into())
            ),
        ]
    );
}

#[tokio::test]
async fn a_track_made_public_on_soundcloud_is_marked_for_an_unsent_post() {
    let (state, _temp, fake, _r2) = setup(json!({})).await;
    let waiting = add_show(&state, "Late Night", None).await;
    let no_post = add_show(&state, "Early Bird", None).await;
    for (id, track) in [(waiting, "555"), (no_post, "556")] {
        sqlx::query("UPDATE shows SET soundcloud_track_id = ?, soundcloud_public = 0 WHERE id = ?")
            .bind(track)
            .bind(id)
            .execute(&state.db)
            .await
            .unwrap();
    }
    sqlx::query(
        "INSERT INTO announcements (show_id, slot, state) VALUES (?, 'post', 'waiting_soundcloud')",
    )
    .bind(waiting)
    .execute(&state.db)
    .await
    .unwrap();

    // Still private on SoundCloud: nothing changes.
    assert!(sync_public_tracks(&state).await.unwrap().is_empty());
    assert_eq!(
        fake.routes(),
        ["GET /tracks/555"],
        "only episodes with a waiting post"
    );
    assert_eq!(
        fake.call("GET /tracks/555").auth.as_deref(),
        Some("OAuth static-token")
    );

    fake.public.lock().unwrap().extend([555, 556]);
    assert_eq!(sync_public_tracks(&state).await.unwrap(), [waiting]);
    assert_eq!(show(&state, waiting).await.soundcloud_public, Some(true));
    assert_eq!(show(&state, no_post).await.soundcloud_public, Some(false));

    // Marked public: not asked again.
    fake.calls.lock().unwrap().clear();
    assert!(sync_public_tracks(&state).await.unwrap().is_empty());
    assert!(fake.routes().is_empty());

    // A failed check leaves the track private and asks again next time.
    sqlx::query("UPDATE shows SET soundcloud_public = 0 WHERE id = ?")
        .bind(waiting)
        .execute(&state.db)
        .await
        .unwrap();
    fake.fail("GET /tracks/555", 500);
    assert!(sync_public_tracks(&state).await.unwrap().is_empty());
    assert_eq!(show(&state, waiting).await.soundcloud_public, Some(false));
}

#[tokio::test]
async fn auto_upload_runs_only_when_switched_on() {
    let (state, _temp, fake, r2) = setup(json!({ "soundcloud_auto_upload": false })).await;
    let id = show_with_take(&state, &r2).await;
    auto_upload_on_finalize(&state, id).await;
    assert!(fake.routes().is_empty());

    let (state, _temp, fake, r2) = setup(json!({ "soundcloud_auto_upload": true })).await;
    // A new take leaves a picked own file alone.
    let own = show_with_recording(&state, &r2).await;
    auto_upload_on_finalize(&state, own).await;
    assert!(fake.routes().is_empty());

    let id = show_with_take(&state, &r2).await;
    auto_upload_on_finalize(&state, 999).await;
    assert!(fake.routes().is_empty());
    auto_upload_on_finalize(&state, id).await;
    assert!(fake.routes().contains(&"POST /tracks".to_string()));
    let stored = show(&state, id).await;
    assert_eq!(stored.soundcloud_track_id.as_deref(), Some("555"));
    assert_eq!(
        (
            stored.soundcloud_source.as_deref(),
            stored.soundcloud_source_key.as_deref()
        ),
        (Some("show"), Some("final/v1.mp3"))
    );
}

/// SoundCloud turned off by an admin: uploads and privacy changes are refused,
/// auto-upload is skipped, and nothing reaches SoundCloud.
#[tokio::test]
async fn turned_off_refuses_every_write() {
    let (state, _temp, fake, r2) = setup(json!({ "soundcloud_auto_upload": true })).await;
    let id = show_with_take(&state, &r2).await;
    crate::db::set_setting(&state.db, crate::db::SOUNDCLOUD_ENABLED, "false")
        .await
        .unwrap();

    let err = upload_track(&state, id).await.unwrap_err();
    assert!(
        matches!(err, AppError::ServiceUnavailable(ref m) if m == "SoundCloud is temporarily not available."),
        "{err}"
    );
    assert!(matches!(
        set_track_privacy(&state, id, true).await,
        Err(AppError::ServiceUnavailable(_))
    ));
    auto_upload_on_finalize(&state, id).await;
    assert!(fake.calls.lock().unwrap().is_empty(), "{:?}", fake.routes());

    // Back on: the upload goes through.
    crate::db::set_setting(&state.db, crate::db::SOUNDCLOUD_ENABLED, "true")
        .await
        .unwrap();
    assert!(upload_track(&state, id).await.unwrap().success);
}
