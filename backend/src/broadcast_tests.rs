//! Broadcast permissions, stream ownership and login warnings against real
//! handlers and SQLite.

use super::*;
use axum::{body::to_bytes, extract::Path, http::StatusCode};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::time::Duration;
use tokio_tungstenite::tungstenite::{client::IntoClientRequest, Message as WsMessage};

async fn state() -> (Arc<AppState>, tempfile::TempDir) {
    state_with(json!({})).await
}

/// `overrides` replaces or adds config fields.
async fn state_with(overrides: Value) -> (Arc<AppState>, tempfile::TempDir) {
    let db = sqlx::SqlitePool::connect("sqlite::memory:").await.unwrap();
    db::run_migrations(&db).await.unwrap();
    let mut config = json!({
        "secret_key": "test-only", "superadmin_password_hash": "unused",
        "r2_account_id": "unused", "r2_access_key_id": "unused", "r2_secret_access_key": "unused"
    });
    config
        .as_object_mut()
        .unwrap()
        .extend(overrides.as_object().unwrap().clone());
    let mut config: Config = serde_json::from_value(config).unwrap();
    // Presigning works offline; any real R2 request fails at once.
    config.r2_endpoint = "http://127.0.0.1:1".to_string();
    let temp = tempfile::tempdir().unwrap();
    let state = Arc::new(AppState {
        recovery_mail: crate::recovery_mail::RecoveryMailer::default(),
        recovery_controls: crate::handlers::recovery::RecoveryControls::default(),
        db,
        s3_client: storage::build_s3_client(&config),
        config,
        stream_state: stream_bridge::new_shared_state(),
        stream_metrics: stream_metrics::new_shared(),
        recording_manager: recording::new_shared_manager(temp.path().to_path_buf()),
        recording_finalizer: Arc::new(tokio::sync::Mutex::new(None)),
        cover_debounce: Arc::new(RwLock::new(HashMap::new())),
        default_cover: tokio::sync::OnceCell::new(),
        telegram_bot: None,
        pending_show_notifications: Arc::new(tokio::sync::Mutex::new(HashMap::new())),
        telegram_edit_sessions: Arc::new(tokio::sync::Mutex::new(HashMap::new())),
        chat_hub: Arc::new(chat_bridge::ChatHub::new()),
    });
    (state, temp)
}

pub(crate) async fn add_user(state: &Arc<AppState>, username: &str, role: &str) -> i64 {
    sqlx::query("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)")
        .bind(username)
        .bind(auth::hash_password("test-password").unwrap())
        .bind(role)
        .execute(&state.db)
        .await
        .unwrap()
        .last_insert_rowid()
}

async fn headers(state: &Arc<AppState>, user_id: i64) -> HeaderMap {
    let token = auth::create_session(state, user_id).await.unwrap();
    let mut headers = HeaderMap::new();
    headers.insert("cookie", format!("session={token}").parse().unwrap());
    headers
}

async fn body(response: impl IntoResponse) -> Value {
    let response = response.into_response();
    assert_eq!(response.status(), StatusCode::OK);
    serde_json::from_slice(&to_bytes(response.into_body(), 1024 * 1024).await.unwrap()).unwrap()
}

#[tokio::test]
async fn admin_flow_lists_another_hosts_show_and_login_warning_survives_logout() {
    let (state, _temp) = state().await;
    let host = add_user(&state, "host", "host").await;
    let admin = add_user(&state, "admin", "admin").await;
    let superadmin = add_user(&state, "superadmin", "superadmin").await;
    sqlx::query("INSERT INTO shows (id, title, date, show_type, host_user_id) VALUES (1, 'Other host', '2026-09-26', 'external', ?)")
        .bind(host).execute(&state.db).await.unwrap();
    for id in [admin, superadmin] {
        let headers = headers(&state, id).await;
        let flow = body(
            handlers::api::api_my_show(State(state.clone()), headers.clone())
                .await
                .unwrap(),
        )
        .await;
        assert_eq!(flow["assigned"], true);
        assert_eq!(flow["shows"][0]["id"], 1);
        let list = body(
            handlers::api::api_shows_list(State(state.clone()), headers.clone())
                .await
                .unwrap(),
        )
        .await;
        assert_eq!(list["shows"][0]["host_has_logged_in"], false);
        let detail = body(
            handlers::api::api_show_detail(State(state.clone()), Path(1), headers)
                .await
                .unwrap(),
        )
        .await;
        assert_eq!(detail["host_has_logged_in"], false);
    }
    let headers = headers(&state, admin).await;
    // A failed login must not hide the warning.
    let login = serde_json::from_value(json!({"username": "host", "password": "wrong"})).unwrap();
    assert!(matches!(
        handlers::api::api_login(State(state.clone()), axum::Json(login)).await,
        Err(AppError::Unauthorized(_))
    ));
    let overview = body(
        handlers::api::api_shows_overview(State(state.clone()), headers.clone())
            .await
            .unwrap(),
    )
    .await;
    assert_eq!(overview["shows"][0]["host_has_logged_in"], false);
    let login =
        serde_json::from_value(json!({"username": "host", "password": "test-password"})).unwrap();
    let _ = body(
        handlers::api::api_login(State(state.clone()), axum::Json(login))
            .await
            .unwrap(),
    )
    .await;
    sqlx::query("DELETE FROM sessions WHERE user_id = ?")
        .bind(host)
        .execute(&state.db)
        .await
        .unwrap();
    let detail = body(
        handlers::api::api_show_detail(State(state.clone()), Path(1), headers)
            .await
            .unwrap(),
    )
    .await;
    assert_eq!(detail["host_has_logged_in"], true);
}

#[tokio::test]
async fn websocket_upgrade_rejects_unassigned_hosts_and_allows_admins() {
    let (state, _temp) = state().await;
    let host = add_user(&state, "host", "host").await;
    let other = add_user(&state, "other", "host").await;
    let admin = add_user(&state, "admin", "admin").await;
    let superadmin = add_user(&state, "superadmin", "superadmin").await;
    sqlx::query(
        "INSERT INTO shows (id, title, date, host_user_id) VALUES (1, 'Hosted', '2026-09-26', ?)",
    )
    .bind(host)
    .execute(&state.db)
    .await
    .unwrap();
    let router = Router::new()
        .route("/ws", get(stream_ws_handler))
        .with_state(state.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    let client = reqwest::Client::new();
    // Rehearsal exercises the real upgrade handler without starting FFmpeg or R2.
    for (id, status) in [(host, 101), (other, 403), (admin, 101), (superadmin, 101)] {
        let response = client
            .get(format!("http://{addr}/ws?show_id=1&test=true"))
            .headers(headers(&state, id).await)
            .header("connection", "upgrade")
            .header("upgrade", "websocket")
            .header("sec-websocket-version", "13")
            .header("sec-websocket-key", "dGhlIHNhbXBsZSBub25jZQ==")
            .send()
            .await
            .unwrap();
        assert_eq!(response.status().as_u16(), status);
    }
    server.abort();
}

#[tokio::test]
async fn stop_handler_allows_admins_and_owner_but_rejects_another_host() {
    let (state, temp) = state().await;
    let host = add_user(&state, "host", "host").await;
    let other = add_user(&state, "other", "host").await;
    let admin = add_user(&state, "admin", "admin").await;
    let superadmin = add_user(&state, "superadmin", "superadmin").await;
    // A local FFmpeg process waits for input. No broadcast or external storage.
    let target = stream_bridge::PushTarget::Rtmp {
        destination: temp
            .path()
            .join("stream.flv")
            .to_string_lossy()
            .into_owned(),
    };
    for allowed in [host, admin, superadmin] {
        state
            .stream_state
            .lock()
            .await
            .start_stream("host".into(), &target, true)
            .await
            .unwrap();
        let denied = handlers::stream_ws::stream_stop(
            State(state.clone()),
            State(state.stream_state.clone()),
            headers(&state, other).await,
        )
        .await;
        assert!(matches!(denied, Err(AppError::Forbidden(_))));
        assert!(state.stream_state.lock().await.is_active());
        let stopped = body(
            handlers::stream_ws::stream_stop(
                State(state.clone()),
                State(state.stream_state.clone()),
                headers(&state, allowed).await,
            )
            .await
            .unwrap(),
        )
        .await;
        assert_eq!(stopped["message"], "Stream stopped");
        assert!(!state.stream_state.lock().await.is_active());
    }
}

/// First bytes of a WebM stream: the producer ffmpeg keeps waiting for more.
const WEBM_MAGIC: [u8; 4] = [0x1a, 0x45, 0xdf, 0xa3];

pub(crate) type Producer =
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

/// Rehearsal producers run a real ffmpeg that muxes to its discarded stdout,
/// so these tests need no Icecast, recording or Telegram.
async fn serve_stream_socket() -> (Arc<AppState>, tempfile::TempDir, std::net::SocketAddr) {
    serve_stream_socket_with(json!({ "icecast_test_url": "pipe:1" })).await
}

/// Serves the real stream WebSocket handler at `/ws`, with `overrides`
/// applied to the config (for example the producer targets).
pub(crate) async fn serve_stream_socket_with(
    overrides: Value,
) -> (Arc<AppState>, tempfile::TempDir, std::net::SocketAddr) {
    let (state, temp) = state_with(overrides).await;
    let router = Router::new()
        .route("/ws", get(stream_ws_handler))
        .with_state(state.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    (state, temp, addr)
}

pub(crate) async fn go_live(
    state: &Arc<AppState>,
    addr: std::net::SocketAddr,
    user_id: i64,
    query: &str,
) -> Producer {
    let mut request = format!("ws://{addr}/ws?{query}")
        .into_client_request()
        .unwrap();
    request.headers_mut().extend(headers(state, user_id).await);
    let (mut socket, _) = tokio_tungstenite::connect_async(request).await.unwrap();
    assert_eq!(next_text(&mut socket).await, "connected");
    socket
}

async fn next_text(socket: &mut Producer) -> String {
    match tokio::time::timeout(Duration::from_secs(10), socket.next()).await {
        Ok(Some(Ok(WsMessage::Text(text)))) => text.to_string(),
        other => panic!("expected a text frame, got {other:?}"),
    }
}

/// Closes `socket` and waits until the server drops the connection, which it
/// does only after the handler has run its disconnect path.
async fn close(mut socket: Producer) {
    let _ = socket.close(None).await;
    let drained = async { while let Some(Ok(_)) = socket.next().await {} };
    tokio::time::timeout(Duration::from_secs(10), drained)
        .await
        .expect("server kept the socket open");
}

#[tokio::test]
async fn reconnect_survives_the_late_close_of_the_replaced_socket() {
    let (state, _temp, addr) = serve_stream_socket().await;
    let host = add_user(&state, "host", "host").await;
    // A network drop leaves the old socket open on the server while the
    // panel reconnects. The old one closes much later (nginx: 300 s idle).
    let replaced = go_live(&state, addr, host, "test=true").await;
    let mut current = go_live(&state, addr, host, "test=true").await;
    close(replaced).await;

    assert!(state.stream_state.lock().await.is_active());
    current
        .send(WsMessage::Binary(WEBM_MAGIC.to_vec()))
        .await
        .unwrap();
    current
        .send(WsMessage::Text("ping:1".into()))
        .await
        .unwrap();
    // A refused chunk would have produced an error frame before the pong.
    assert!(next_text(&mut current).await.starts_with("pong:"));
}

#[tokio::test]
async fn forced_takeover_survives_the_replaced_host() {
    let (state, _temp, addr) = serve_stream_socket().await;
    let host = add_user(&state, "host", "host").await;
    let admin = add_user(&state, "admin", "admin").await;
    let mut replaced = go_live(&state, addr, host, "test=true").await;
    let mut current = go_live(&state, addr, admin, "test=true&force=true").await;

    // The host's audio no longer reaches the admin's ffmpeg, and its
    // disconnect leaves the admin's stream running.
    replaced
        .send(WsMessage::Binary(WEBM_MAGIC.to_vec()))
        .await
        .unwrap();
    assert_eq!(
        next_text(&mut replaced).await,
        "error: Another connection took over the stream"
    );
    close(replaced).await;

    {
        let stream = state.stream_state.lock().await;
        assert!(stream.is_active());
        assert_eq!(stream.current_user.as_deref(), Some("admin"));
    }
    current
        .send(WsMessage::Text("ping:1".into()))
        .await
        .unwrap();
    assert!(next_text(&mut current).await.starts_with("pong:"));
}

/// A prerecorded show taking the air ends the live show's recording: its
/// producer was replaced or waits out its reconnect grace, and the grace end
/// would see the prerecorded stream as live and keep waiting.
#[tokio::test]
async fn prerecorded_start_finalizes_the_live_recording() {
    let (state, _temp) = state().await;
    let host = add_user(&state, "host", "host").await;
    sqlx::query(
        "INSERT INTO shows (id, title, date, host_user_id, prerecorded_key, prerecorded_confirmed_at) \
         VALUES (2, 'Tape', '2026-10-15', ?, 'shows/2/tape.mp3', datetime('now'))",
    )
    .bind(host)
    .execute(&state.db)
    .await
    .unwrap();
    let artifact = {
        let mut manager = state.recording_manager.lock().await;
        manager.start(1).await.unwrap();
        manager.get_temp_file_path().unwrap()
    };
    // Finalize for real, but fail locally before any R2 request.
    let _ = tokio::fs::remove_file(artifact).await;

    let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = 2")
        .fetch_one(&state.db)
        .await
        .unwrap();
    handlers::api::start_prerecorded_show_stream(&state, &show, "host")
        .await
        .unwrap();
    assert_eq!(state.recording_manager.lock().await.current_show_id(), None);
}

/// Pings keep a socket open without audio. Such a connection is ended with an
/// error instead of staying "on air" while listeners hear silence.
#[tokio::test]
async fn connection_without_audio_is_ended() {
    let (state, _temp, addr) = serve_stream_socket_with(
        json!({ "icecast_test_url": "pipe:1", "stream_first_audio_timeout_secs": 1 }),
    )
    .await;
    let host = add_user(&state, "host", "host").await;
    let mut socket = go_live(&state, addr, host, "test=true").await;
    socket.send(WsMessage::Text("ping:1".into())).await.unwrap();
    assert!(next_text(&mut socket).await.starts_with("pong:"));

    assert_eq!(
        next_text(&mut socket).await,
        handlers::stream_ws::NO_AUDIO_ERROR
    );
    close(socket).await;
    assert!(!state.stream_state.lock().await.is_active());
}

#[tokio::test]
async fn first_audio_chunk_lifts_the_no_audio_deadline() {
    let (state, _temp, addr) = serve_stream_socket_with(
        json!({ "icecast_test_url": "pipe:1", "stream_first_audio_timeout_secs": 1 }),
    )
    .await;
    let host = add_user(&state, "host", "host").await;
    let mut socket = go_live(&state, addr, host, "test=true").await;
    socket
        .send(WsMessage::Binary(WEBM_MAGIC.to_vec()))
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_millis(1500)).await;

    socket.send(WsMessage::Text("ping:1".into())).await.unwrap();
    // An error frame for the deadline would arrive before the pong.
    assert!(next_text(&mut socket).await.starts_with("pong:"));
    assert!(state.stream_state.lock().await.is_active());
}
