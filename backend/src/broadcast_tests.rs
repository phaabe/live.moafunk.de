//! Broadcast permissions, stream ownership and login warnings against real
//! handlers and SQLite.

use super::*;
use axum::{body::to_bytes, extract::Path, http::StatusCode};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::time::Duration;
use tokio_tungstenite::tungstenite::{client::IntoClientRequest, Message as WsMessage};

pub(crate) async fn state() -> (Arc<AppState>, tempfile::TempDir) {
    state_with(json!({})).await
}

/// `overrides` replaces or adds config fields.
pub(crate) async fn state_with(overrides: Value) -> (Arc<AppState>, tempfile::TempDir) {
    let db = sqlx::SqlitePool::connect("sqlite::memory:").await.unwrap();
    db::run_migrations(&db).await.unwrap();
    let mut config = json!({
        "secret_key": "test-only", "superadmin_password_hash": "unused",
        "r2_account_id": "unused", "r2_access_key_id": "unused", "r2_secret_access_key": "unused",
        // Never push a test stream to the production default.
        "rtmp_url": "rtmp://127.0.0.1:1/live"
    });
    config
        .as_object_mut()
        .unwrap()
        .extend(overrides.as_object().unwrap().clone());
    let mut config: Config = serde_json::from_value(config).unwrap();
    // Local dead end so presigning works offline, unless the test brings a
    // fake R2 of its own. `r2_endpoint` is computed, so serde skips it.
    config.r2_endpoint = overrides
        .get("r2_endpoint")
        .and_then(Value::as_str)
        .unwrap_or("http://127.0.0.1:1")
        .to_string();
    let temp = tempfile::tempdir().unwrap();
    let state = Arc::new(AppState {
        recovery_mail: crate::recovery_mail::RecoveryMailer::default(),
        recovery_controls: crate::handlers::recovery::RecoveryControls::default(),
        db,
        s3_client: storage::build_s3_client(&config),
        config,
        stream_state: stream_bridge::new_shared_state(),
        soundcheck_state: stream_bridge::new_shared_state(),
        stream_metrics: stream_metrics::new_shared(),
        recording_manager: recording::new_shared_manager(temp.path().to_path_buf()),
        recording_finalizer: Arc::default(),
        cover_debounce: Arc::new(RwLock::new(HashMap::new())),
        default_cover: tokio::sync::OnceCell::new(),
        telegram_bot: None,
        pending_show_notifications: Arc::new(tokio::sync::Mutex::new(HashMap::new())),
        telegram_edit_sessions: Arc::new(tokio::sync::Mutex::new(HashMap::new())),
        chat_hub: Arc::new(chat_bridge::ChatHub::new()),
        peaks_jobs: Default::default(),
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

pub(crate) async fn headers(state: &Arc<AppState>, user_id: i64) -> HeaderMap {
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

// ── Stream ownership: a replaced producer leaves its successor alone ────────

/// First bytes of a WebM stream: the producer ffmpeg keeps waiting for more.
const WEBM_MAGIC: [u8; 4] = [0x1a, 0x45, 0xdf, 0xa3];

pub(crate) type Producer =
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

/// Producers, live and rehearsal, run a real ffmpeg that muxes to its
/// discarded stdout, so these tests need no Icecast or Telegram.
async fn serve_stream_socket() -> (Arc<AppState>, tempfile::TempDir, std::net::SocketAddr) {
    serve_stream_socket_with(json!({
        "stream_output": "icecast", "icecast_url": "pipe:1", "icecast_test_url": "pipe:1"
    }))
    .await
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

pub(crate) async fn connect_producer(
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

async fn hosted_show(state: &Arc<AppState>, host: i64) {
    sqlx::query(
        "INSERT INTO shows (id, title, date, host_user_id) VALUES (1, 'Hosted', '2026-09-26', ?)",
    )
    .bind(host)
    .execute(&state.db)
    .await
    .unwrap();
}

/// "Record this show" unticked: the show streams and counts as on air, but
/// nothing records it, also after a reconnect.
#[tokio::test]
async fn unticked_record_streams_the_show_without_recording_it() {
    let (state, _temp, addr) = serve_stream_socket().await;
    let host = add_user(&state, "host", "host").await;
    hosted_show(&state, host).await;

    let first = connect_producer(&state, addr, host, "show_id=1&record=false").await;
    assert_eq!(state.recording_manager.lock().await.current_show_id(), None);
    assert!(!state.stream_state.lock().await.is_recording());
    assert_eq!(handlers::series::on_air_show_id(&state).await, Some(1));

    // A network drop: still on air during the reconnect grace.
    close(first).await;
    assert!(!state.stream_state.lock().await.is_active());
    assert_eq!(handlers::series::on_air_show_id(&state).await, Some(1));

    let mut back = connect_producer(&state, addr, host, "show_id=1&record=false").await;
    assert_eq!(state.recording_manager.lock().await.current_show_id(), None);
    assert_eq!(handlers::series::on_air_show_id(&state).await, Some(1));

    // End Episode: no longer on air, and the episode counts as ended.
    back.send(WsMessage::Text("stop".into())).await.unwrap();
    close(back).await;
    assert_eq!(handlers::series::on_air_show_id(&state).await, None);
    let (ended, recorded): (Option<String>, Option<bool>) =
        sqlx::query_as("SELECT live_ended_at, live_recorded FROM shows WHERE id = 1")
            .fetch_one(&state.db)
            .await
            .unwrap();
    assert!(ended.is_some());
    assert_eq!(recorded, Some(false));
}

/// The record flag defaults to on: a live show is recorded as before.
#[tokio::test]
async fn a_live_show_is_recorded_by_default() {
    let (state, _temp, addr) = serve_stream_socket().await;
    let host = add_user(&state, "host", "host").await;
    hosted_show(&state, host).await;

    let _socket = connect_producer(&state, addr, host, "show_id=1").await;
    assert_eq!(
        state.recording_manager.lock().await.current_show_id(),
        Some(1)
    );
    assert_eq!(handlers::series::on_air_show_id(&state).await, Some(1));
    assert!(state.stream_state.lock().await.is_recording());
}

#[tokio::test]
async fn reconnect_survives_the_late_close_of_the_replaced_socket() {
    let (state, _temp, addr) = serve_stream_socket().await;
    let host = add_user(&state, "host", "host").await;
    // A network drop leaves the old socket open on the server while the
    // panel reconnects. The old one closes much later (nginx: 300 s idle).
    let replaced = connect_producer(&state, addr, host, "test=true").await;
    let mut current = connect_producer(&state, addr, host, "test=true").await;
    close(replaced).await;

    assert!(state.soundcheck_state.lock().await.is_active());
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
    let mut replaced = connect_producer(&state, addr, host, "test=true").await;
    let mut current = connect_producer(&state, addr, admin, "test=true&force=true").await;

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
        let stream = state.soundcheck_state.lock().await;
        assert!(stream.is_active());
        assert_eq!(stream.current_user.as_deref(), Some("admin"));
    }
    current
        .send(WsMessage::Text("ping:1".into()))
        .await
        .unwrap();
    assert!(next_text(&mut current).await.starts_with("pong:"));
}

/// Staging soak, 2026-11-18: a soundcheck from the host's second tab ended
/// the live show. It must be refused and leave the show on air. A reload of
/// the live page still takes over.
#[tokio::test]
async fn soundcheck_from_the_hosts_second_tab_is_refused() {
    let (state, _temp, addr) = serve_stream_socket().await;
    let host = add_user(&state, "host", "host").await;
    hosted_show(&state, host).await;
    let mut live = connect_producer(&state, addr, host, "show_id=1").await;
    live.send(WsMessage::Binary(WEBM_MAGIC.to_vec()))
        .await
        .unwrap();
    live.send(WsMessage::Text("ping:1".into())).await.unwrap();
    assert!(next_text(&mut live).await.starts_with("pong:"));

    let mut request = format!("ws://{addr}/ws?test=true")
        .into_client_request()
        .unwrap();
    request.headers_mut().extend(headers(&state, host).await);
    let (mut soundcheck, _) = tokio_tungstenite::connect_async(request).await.unwrap();
    assert_eq!(
        next_text(&mut soundcheck).await,
        "error: Soundcheck not started: you are on air right now"
    );
    close(soundcheck).await;
    assert!(!state.soundcheck_state.lock().await.is_active());

    {
        let stream = state.stream_state.lock().await;
        assert!(stream.get_status().active, "the live show must stay on air");
        assert_eq!(stream.current_user.as_deref(), Some("host"));
        assert!(stream.is_recording());
    }
    assert_eq!(
        state.recording_manager.lock().await.current_show_id(),
        Some(1)
    );
    live.send(WsMessage::Text("ping:2".into())).await.unwrap();
    assert!(next_text(&mut live).await.starts_with("pong:"));

    // A reload of the live page takes over, as before.
    let _reloaded = connect_producer(&state, addr, host, "show_id=1").await;
    live.send(WsMessage::Binary(WEBM_MAGIC.to_vec()))
        .await
        .unwrap();
    assert_eq!(
        next_text(&mut live).await,
        "error: Another connection took over the stream"
    );
}

/// The next host soundchecks while another show is on air. The soundcheck
/// runs apart from the show: it neither stops the show nor its recording.
#[tokio::test]
async fn another_user_can_soundcheck_while_a_show_is_on_air() {
    let (state, _temp, addr) = serve_stream_socket().await;
    let host = add_user(&state, "host", "host").await;
    let next = add_user(&state, "next", "host").await;
    hosted_show(&state, host).await;
    let mut live = connect_producer(&state, addr, host, "show_id=1").await;
    live.send(WsMessage::Binary(WEBM_MAGIC.to_vec()))
        .await
        .unwrap();

    let mut soundcheck = connect_producer(&state, addr, next, "test=true").await;
    soundcheck
        .send(WsMessage::Binary(WEBM_MAGIC.to_vec()))
        .await
        .unwrap();
    soundcheck
        .send(WsMessage::Text("ping:1".into()))
        .await
        .unwrap();
    assert!(next_text(&mut soundcheck).await.starts_with("pong:"));
    assert_eq!(
        state.soundcheck_state.lock().await.current_user.as_deref(),
        Some("next")
    );
    soundcheck
        .send(WsMessage::Text("stop".into()))
        .await
        .unwrap();
    close(soundcheck).await;

    {
        let stream = state.stream_state.lock().await;
        assert!(stream.get_status().active, "the show must stay on air");
        assert_eq!(stream.current_user.as_deref(), Some("host"));
        assert!(stream.is_recording());
    }
    assert_eq!(
        state.recording_manager.lock().await.current_show_id(),
        Some(1)
    );
    live.send(WsMessage::Text("ping:1".into())).await.unwrap();
    assert!(next_text(&mut live).await.starts_with("pong:"));
}

/// The host's connection breaks without a close, so the server still holds
/// the dead live socket, but no audio feeds `/live` any more. The host's
/// soundcheck before going live again must work, and Go Live resumes the
/// show. (A drop the server sees is covered by
/// `a_rehearsal_during_the_reconnect_grace_keeps_the_live_recording`.)
#[tokio::test]
async fn host_soundchecks_after_a_silent_drop_and_goes_live_again() {
    let (state, _temp, addr) = serve_stream_socket().await;
    let host = add_user(&state, "host", "host").await;
    hosted_show(&state, host).await;
    let mut dead = connect_producer(&state, addr, host, "show_id=1").await;
    dead.send(WsMessage::Binary(WEBM_MAGIC.to_vec()))
        .await
        .unwrap();
    dead.send(WsMessage::Text("ping:1".into())).await.unwrap();
    assert!(next_text(&mut dead).await.starts_with("pong:"));
    state.stream_state.lock().await.age_audio_for_test();

    let mut soundcheck = connect_producer(&state, addr, host, "test=true").await;
    soundcheck
        .send(WsMessage::Text("stop".into()))
        .await
        .unwrap();
    close(soundcheck).await;
    assert_eq!(
        state.recording_manager.lock().await.current_show_id(),
        Some(1),
        "the soundcheck leaves the show's recording alone"
    );

    let _back = connect_producer(&state, addr, host, "show_id=1").await;
    {
        let stream = state.stream_state.lock().await;
        assert!(stream.get_status().active);
        assert!(stream.is_recording());
    }
    assert_eq!(
        state.recording_manager.lock().await.current_show_id(),
        Some(1)
    );
    dead.send(WsMessage::Binary(WEBM_MAGIC.to_vec()))
        .await
        .unwrap();
    assert_eq!(
        next_text(&mut dead).await,
        "error: Another connection took over the stream"
    );
}

/// An admin takes over a host's recorded show. A stop from the host's old tab
/// must neither end the takeover nor finalize the show's recording under it.
#[tokio::test]
async fn replaced_host_stop_leaves_the_takeover_and_its_recording_alone() {
    let (state, _temp, addr) = serve_stream_socket().await;
    let host = add_user(&state, "host", "host").await;
    let admin = add_user(&state, "admin", "admin").await;
    sqlx::query(
        "INSERT INTO shows (id, title, date, host_user_id) VALUES (1, 'Hosted', '2026-09-26', ?)",
    )
    .bind(host)
    .execute(&state.db)
    .await
    .unwrap();
    let mut replaced = connect_producer(&state, addr, host, "show_id=1").await;
    let mut current = connect_producer(&state, addr, admin, "show_id=1&force=true").await;
    assert_eq!(
        state.recording_manager.lock().await.current_show_id(),
        Some(1)
    );

    replaced.send(WsMessage::Text("stop".into())).await.unwrap();
    close(replaced).await;

    assert_eq!(
        state.recording_manager.lock().await.current_show_id(),
        Some(1),
        "the show's recording must not be finalized"
    );
    {
        let stream = state.stream_state.lock().await;
        assert!(stream.is_active());
        assert_eq!(stream.current_user.as_deref(), Some("admin"));
        assert!(stream.is_recording());
    }
    current
        .send(WsMessage::Text("ping:1".into()))
        .await
        .unwrap();
    assert!(next_text(&mut current).await.starts_with("pong:"));
}

/// The takeover drops and waits out its reconnect grace. A stop from the
/// host's old tab in that time must not finalize the recording, so the
/// takeover resumes the same archive when it reconnects.
#[tokio::test]
async fn replaced_host_stop_during_the_takeovers_grace_keeps_its_recording() {
    let (state, _temp, addr) = serve_stream_socket().await;
    let host = add_user(&state, "host", "host").await;
    let admin = add_user(&state, "admin", "admin").await;
    sqlx::query(
        "INSERT INTO shows (id, title, date, host_user_id) VALUES (1, 'Hosted', '2026-09-26', ?)",
    )
    .bind(host)
    .execute(&state.db)
    .await
    .unwrap();
    let mut replaced = connect_producer(&state, addr, host, "show_id=1").await;
    let takeover = connect_producer(&state, addr, admin, "show_id=1&force=true").await;
    let archive = state.recording_manager.lock().await.get_temp_file_path();
    assert!(archive.is_some());

    // A network drop: no stop, so the recording waits for a reconnect.
    close(takeover).await;
    assert!(!state.stream_state.lock().await.is_active());

    replaced.send(WsMessage::Text("stop".into())).await.unwrap();
    close(replaced).await;
    assert_eq!(
        state.recording_manager.lock().await.current_show_id(),
        Some(1),
        "the show's recording must not be finalized"
    );

    let mut back = connect_producer(&state, addr, admin, "show_id=1").await;
    assert_eq!(
        state.recording_manager.lock().await.get_temp_file_path(),
        archive,
        "the reconnect resumes the same archive"
    );
    back.send(WsMessage::Text("ping:1".into())).await.unwrap();
    assert!(next_text(&mut back).await.starts_with("pong:"));
}

// ── B1.1.6: prerecorded occurrences ─────────────────────────────────────────

const SHOW_START: &str = "2026-09-26T18:00:00+00:00"; // 20:00 Berlin (CEST)

async fn prerecorded_show(state: &Arc<AppState>, host: i64) {
    sqlx::query(
        "INSERT INTO shows (id, title, date, start_time, end_time, show_type, host_user_id, \
         stream_mode, prerecorded_key, prerecorded_confirmed_at) \
         VALUES (1, 'Tape', '2026-09-26', '20:00', '22:00', 'external', ?, 'prerecorded', \
         'shows/tape.mp3', datetime('now'))",
    )
    .bind(host)
    .execute(&state.db)
    .await
    .unwrap();
}

async fn show(state: &Arc<AppState>) -> models::Show {
    sqlx::query_as("SELECT * FROM shows WHERE id = 1")
        .fetch_one(&state.db)
        .await
        .unwrap()
}

/// Another producer (a live broadcast) is on air.
async fn producer_busy(state: &Arc<AppState>) {
    let child = tokio::process::Command::new("sleep")
        .arg("30")
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    state
        .stream_state
        .lock()
        .await
        .set_active_for_test("live-host", child);
}

async fn producer_ends(state: &Arc<AppState>) {
    state.stream_state.lock().await.stop_stream().await.unwrap();
}

async fn on_air(state: &Arc<AppState>) -> Option<String> {
    let stream = state.stream_state.lock().await;
    stream
        .is_active()
        .then(|| stream.current_user.clone())
        .flatten()
}

async fn scheduler_tick(state: &Arc<AppState>) -> handlers::api::Admission {
    let start = chrono::DateTime::parse_from_rfc3339(SHOW_START)
        .unwrap()
        .with_timezone(&chrono::Utc);
    handlers::api::start_scheduled_prerecorded_occurrence(state, &show(state).await, "host", start)
        .await
        .unwrap()
}

async fn go_live(state: &Arc<AppState>, headers: &HeaderMap, retry: bool) -> Result<()> {
    handlers::api::api_my_show_go_live(
        State(state.clone()),
        Query(handlers::api::GoLiveQuery { show_id: 1, retry }),
        headers.clone(),
    )
    .await
    .map(|_| ())
}

/// (status, manual_retries, latest retry's operator, latest retry's result)
async fn occurrence(
    state: &Arc<AppState>,
) -> Option<(String, i64, Option<String>, Option<String>)> {
    sqlx::query_as(
        "SELECT o.status, o.manual_retries, \
                (SELECT retried_by FROM prerecorded_retries r \
                 WHERE r.show_id = o.show_id AND r.scheduled_start_utc = o.scheduled_start_utc \
                 ORDER BY r.id DESC LIMIT 1), \
                (SELECT result FROM prerecorded_retries r \
                 WHERE r.show_id = o.show_id AND r.scheduled_start_utc = o.scheduled_start_utc \
                 ORDER BY r.id DESC LIMIT 1) \
         FROM prerecorded_occurrences o WHERE o.show_id = 1 AND o.scheduled_start_utc = ?",
    )
    .bind(SHOW_START)
    .fetch_optional(&state.db)
    .await
    .unwrap()
}

async fn show_claimed(state: &Arc<AppState>) -> bool {
    show(state).await.prerecorded_started_at.is_some()
}

/// A pre-recorded episode booked through Book Slot starts by itself at air
/// time: booking sets everything the scheduler needs, scheduler.rs unchanged.
#[tokio::test]
async fn a_booked_prerecorded_episode_starts_by_itself_at_air_time() {
    let (state, _temp) = state().await;
    let host = add_user(&state, "host", "host").await;
    let rhythm =
        series::Rhythm::from_columns(Some("weekly"), Some(7), Some("20:00"), Some("22:00"))
            .unwrap();
    let (series_id, slug) = series::insert_series(&state.db, host, "Tape", None, Some(&rhythm))
        .await
        .unwrap();
    // A finished upload, as finalize leaves it.
    sqlx::query(
        "INSERT INTO series_uploads (session_id, series_id, user_id, filename, total_chunks, \
         file_key, duration_secs, expires_at) \
         VALUES ('up', ?, ?, 'tape.mp3', 1, 'series-uploads/up/tape.mp3', 7200, datetime('now', '+1 hour'))",
    )
    .bind(series_id)
    .bind(host)
    .execute(&state.db)
    .await
    .unwrap();
    let headers = headers(&state, host).await;

    let slots = body(
        handlers::booking::api_series_slots(
            State(state.clone()),
            Path(slug.clone()),
            headers.clone(),
        )
        .await
        .unwrap(),
    )
    .await;
    let slot = &slots["slots"][0];
    let request = serde_json::from_value(json!({
        "date": slot["date"], "start_time": slot["start_time"], "end_time": slot["end_time"],
        "mode": "prerecorded", "upload_id": "up"
    }))
    .unwrap();
    let booked = handlers::booking::api_book_episode(
        State(state.clone()),
        Path(slug),
        headers,
        axum::Json(request),
    )
    .await
    .unwrap()
    .into_response();
    assert_eq!(booked.status(), StatusCode::CREATED);
    let booked: Value =
        serde_json::from_slice(&to_bytes(booked.into_body(), 1024 * 1024).await.unwrap()).unwrap();

    let episode: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(booked["id"].as_i64().unwrap())
        .fetch_one(&state.db)
        .await
        .unwrap();
    assert_eq!(episode.stream_mode.as_deref(), Some("prerecorded"));
    assert!(episode.prerecorded_confirmed_at.is_some());
    assert!(episode.prerecorded_started_at.is_none());

    // The scheduler's own start call, at the booked air time.
    let air_time =
        scheduler::show_start_utc(&episode.date, episode.start_time.as_deref().unwrap()).unwrap();
    let admission =
        handlers::api::start_scheduled_prerecorded_occurrence(&state, &episode, "host", air_time)
            .await
            .unwrap();
    assert_eq!(admission, handlers::api::Admission::Started);
    let started: Option<String> =
        sqlx::query_scalar("SELECT prerecorded_started_at FROM shows WHERE id = ?")
            .bind(episode.id)
            .fetch_one(&state.db)
            .await
            .unwrap();
    assert!(started.is_some());
    assert_eq!(on_air(&state).await.as_deref(), Some("host"));
}

/// Review finding 1: opening or reloading the on-air page calls Go Live on its
/// own. That must not start a missed occurrence; only an explicit retry may.
#[tokio::test]
async fn automatic_go_live_cannot_start_a_missed_occurrence_but_explicit_retry_can() {
    let (state, _temp) = state().await;
    let host = add_user(&state, "host", "host").await;
    prerecorded_show(&state, host).await;
    let headers = headers(&state, host).await;

    producer_busy(&state).await;
    assert!(matches!(
        scheduler_tick(&state).await,
        handlers::api::Admission::Missed(_)
    ));
    db::run_migrations(&state.db).await.unwrap(); // API restart
    producer_ends(&state).await; // still inside the show's window

    for _ in 0..2 {
        assert_eq!(
            scheduler_tick(&state).await,
            handlers::api::Admission::AlreadyMissed,
            "no second start and no second alert"
        );
        let page_load = go_live(&state, &headers, false).await;
        assert!(matches!(page_load, Err(AppError::Conflict(_))));
    }
    assert_eq!(on_air(&state).await, None);
    assert!(!show_claimed(&state).await);
    assert_eq!(occurrence(&state).await.unwrap().1, 0, "no retry recorded");

    if let Err(e) = go_live(&state, &headers, true).await {
        panic!("explicit retry must start: {e}");
    }
    assert_eq!(on_air(&state).await.as_deref(), Some("host"));
    assert_eq!(
        occurrence(&state).await,
        Some((
            "missed".to_string(),
            1,
            Some("host".to_string()),
            Some("started".to_string())
        )),
        "the retry is logged as a retry; missed playback never becomes 'started'"
    );
    producer_ends(&state).await;
}

/// The waiting room's automatic Go Live shares the scheduler's occurrence claim.
#[tokio::test]
async fn refused_page_admission_consumes_the_occurrence_for_the_scheduler() {
    let (state, _temp) = state().await;
    let host = add_user(&state, "host", "host").await;
    prerecorded_show(&state, host).await;
    let headers = headers(&state, host).await;

    producer_busy(&state).await;
    assert!(matches!(
        go_live(&state, &headers, false).await,
        Err(AppError::Conflict(_))
    ));
    assert_eq!(occurrence(&state).await.unwrap().0, "missed");
    producer_ends(&state).await;
    assert_eq!(
        scheduler_tick(&state).await,
        handlers::api::Admission::AlreadyMissed
    );
    assert_eq!(on_air(&state).await, None);
}

/// Review finding 2: the scheduler claims the occurrence but loses the show
/// claim to another attempt that later fails. The scheduler must not record
/// 'started'; the next tick records the real result.
#[tokio::test]
async fn losing_the_show_claim_never_records_playback_busy_failure() {
    let (state, _temp) = state().await;
    let host = add_user(&state, "host", "host").await;
    prerecorded_show(&state, host).await;
    let headers = headers(&state, host).await;
    producer_busy(&state).await;

    // Another attempt holds the show claim while it starts.
    sqlx::query("UPDATE shows SET prerecorded_started_at = datetime('now') WHERE id = 1")
        .execute(&state.db)
        .await
        .unwrap();
    assert_eq!(
        scheduler_tick(&state).await,
        handlers::api::Admission::Skipped
    );
    assert_eq!(occurrence(&state).await, None, "not recorded as started");

    // That attempt fails with producer busy and clears its show claim.
    sqlx::query("UPDATE shows SET prerecorded_started_at = NULL WHERE id = 1")
        .execute(&state.db)
        .await
        .unwrap();
    let mut alerts = 0;
    for _ in 0..3 {
        if matches!(
            scheduler_tick(&state).await,
            handlers::api::Admission::Missed(_)
        ) {
            alerts += 1;
        }
    }
    assert_eq!(alerts, 1, "exactly one alert for the occurrence");
    assert_eq!(occurrence(&state).await.unwrap().0, "missed");

    // The missed occurrence stays eligible for an explicit retry.
    producer_ends(&state).await;
    if let Err(e) = go_live(&state, &headers, true).await {
        panic!("explicit retry must start: {e}");
    }
    assert_eq!(occurrence(&state).await.unwrap().1, 1);
    producer_ends(&state).await;
}

#[tokio::test]
async fn losing_the_show_claim_never_records_playback_transient_failure() {
    let (state, _temp) = state().await;
    let host = add_user(&state, "host", "host").await;
    prerecorded_show(&state, host).await;

    sqlx::query("UPDATE shows SET prerecorded_started_at = datetime('now') WHERE id = 1")
        .execute(&state.db)
        .await
        .unwrap();
    assert_eq!(
        scheduler_tick(&state).await,
        handlers::api::Admission::Skipped
    );
    assert_eq!(occurrence(&state).await, None);

    // That attempt fails transiently and clears its show claim.
    sqlx::query("UPDATE shows SET prerecorded_started_at = NULL WHERE id = 1")
        .execute(&state.db)
        .await
        .unwrap();
    assert_eq!(
        scheduler_tick(&state).await,
        handlers::api::Admission::Started
    );
    assert_eq!(occurrence(&state).await.unwrap().0, "started");
    assert_eq!(on_air(&state).await.as_deref(), Some("host"));
    producer_ends(&state).await;
}

// ── Recording finalize ──────────────────────────────────────────────────────

#[tokio::test]
async fn inactive_admin_stop_finalizes_recording_during_grace() {
    for role in ["admin", "superadmin"] {
        let (state, _temp) = state().await;
        let admin = add_user(&state, role, role).await;
        let headers = headers(&state, admin).await;
        let artifact = {
            let mut manager = state.recording_manager.lock().await;
            manager.start(1).await.unwrap();
            manager.get_temp_file_path().unwrap()
        };
        // Exercise the real detach, but fail locally before any R2 operation.
        tokio::fs::remove_file(artifact).await.unwrap();
        state
            .recording_finalizer
            .schedule(handlers::recording::LiveFinalize::new(&state), 1);
        assert!(!state.stream_state.lock().await.is_active());

        let stopped = tokio::time::timeout(
            std::time::Duration::from_secs(5),
            handlers::stream_ws::stream_stop(
                State(state.clone()),
                State(state.stream_state.clone()),
                headers,
            ),
        )
        .await
        .expect("admin stop must not wait for the 30-second grace period")
        .unwrap();

        assert_eq!(body(stopped).await["message"], "Stream stopped", "{role}");
        assert_eq!(
            state.recording_manager.lock().await.current_show_id(),
            None,
            "{role} must detach the recording even with no live producer"
        );
    }
}

/// A detached recording whose upload stays pending until the test releases it.
struct PendingUpload {
    session: std::sync::atomic::AtomicBool,
    started: tokio::sync::Notify,
    release: tokio::sync::Notify,
}

impl crate::recording_finalizer::FinalizeOps for PendingUpload {
    type Session = ();

    fn detach(
        &self,
        _show: Option<i64>,
    ) -> futures_util::future::BoxFuture<'_, std::result::Result<Option<()>, String>> {
        let session = self
            .session
            .swap(false, std::sync::atomic::Ordering::SeqCst)
            .then_some(());
        Box::pin(async move { Ok(session) })
    }

    fn upload(
        &self,
        _session: (),
    ) -> futures_util::future::BoxFuture<
        '_,
        std::result::Result<handlers::recording::FinalizedRecording, String>,
    > {
        Box::pin(async move {
            self.started.notify_one();
            self.release.notified().await;
            Ok(handlers::recording::FinalizedRecording {
                show_id: 1,
                version: "test".into(),
                marker_count: 0,
                raw_key: "raw".into(),
                markers_key: "markers".into(),
                incomplete: false,
            })
        })
    }

    fn stream_active(&self) -> futures_util::future::BoxFuture<'_, bool> {
        Box::pin(async { false })
    }
}

#[tokio::test]
async fn inactive_admin_stop_waits_for_running_upload() {
    let (state, _temp) = state().await;
    let admin = add_user(&state, "admin", "admin").await;
    let headers = headers(&state, admin).await;
    let upload = Arc::new(PendingUpload {
        session: std::sync::atomic::AtomicBool::new(true),
        started: tokio::sync::Notify::new(),
        release: tokio::sync::Notify::new(),
    });
    let running = tokio::spawn({
        let finalizer = state.recording_finalizer.clone();
        let upload = upload.clone();
        async move { finalizer.finalize_now(upload).await }
    });
    upload.started.notified().await;
    assert!(!state.stream_state.lock().await.is_active());

    let stop = handlers::stream_ws::stream_stop(
        State(state.clone()),
        State(state.stream_state.clone()),
        headers,
    );
    tokio::pin!(stop);
    assert!(
        tokio::time::timeout(std::time::Duration::from_millis(100), &mut stop)
            .await
            .is_err(),
        "admin stop must wait for the running upload"
    );
    upload.release.notify_one();
    let stopped = tokio::time::timeout(std::time::Duration::from_secs(5), stop)
        .await
        .expect("admin stop must finish after the upload")
        .unwrap();
    assert_eq!(body(stopped).await["message"], "Stream stopped");
    assert_eq!(running.await.unwrap().unwrap().unwrap().show_id, 1);
}

#[tokio::test]
async fn inactive_host_stop_leaves_another_hosts_recording_untouched() {
    let (state, _temp) = state().await;
    let owner = add_user(&state, "owner", "host").await;
    let other = add_user(&state, "other", "host").await;
    sqlx::query(
        "INSERT INTO shows (id, title, date, host_user_id) VALUES (1, 'Hosted', '2026-09-26', ?)",
    )
    .bind(owner)
    .execute(&state.db)
    .await
    .unwrap();
    state.recording_manager.lock().await.start(1).await.unwrap();
    assert!(!state.stream_state.lock().await.is_active());

    let stopped = handlers::stream_ws::stream_stop(
        State(state.clone()),
        State(state.stream_state.clone()),
        headers(&state, other).await,
    )
    .await
    .unwrap();

    assert_eq!(body(stopped).await["message"], "No active stream");
    assert_eq!(
        state.recording_manager.lock().await.current_show_id(),
        Some(1)
    );
}

#[tokio::test]
async fn finalize_detach_takes_only_the_expected_show() {
    use crate::recording_finalizer::FinalizeOps;
    let (state, _temp) = state().await;
    state.recording_manager.lock().await.start(1).await.unwrap();
    let ops = handlers::recording::LiveFinalize::new(&state);
    // A grace finalize of show 2 must not end show 1's recording.
    assert!(ops.detach(Some(2)).await.unwrap().is_none());
    let current = state.recording_manager.lock().await.current_show_id();
    assert_eq!(current, Some(1));
    assert!(ops.detach(Some(1)).await.unwrap().is_some());
    let current = state.recording_manager.lock().await.current_show_id();
    assert_eq!(current, None);
    assert!(ops.detach(None).await.unwrap().is_none());
}

/// A live recording waits in its grace period when a prerecorded show starts.
/// The grace end would see the prerecorded stream as live and keep waiting, so
/// the start finalizes the recording at once, for another show and the same one.
#[tokio::test]
async fn prerecorded_start_finalizes_a_recording_in_its_grace_period() {
    for waiting in [2, 1] {
        let (state, _temp) = state().await;
        let host = add_user(&state, "host", "host").await;
        prerecorded_show(&state, host).await;
        let artifact = {
            let mut manager = state.recording_manager.lock().await;
            manager.start(waiting).await.unwrap();
            manager.get_temp_file_path().unwrap()
        };
        // Exercise the real detach, but fail locally before any R2 operation.
        tokio::fs::remove_file(artifact).await.unwrap();
        state
            .recording_finalizer
            .schedule(handlers::recording::LiveFinalize::new(&state), waiting);

        assert_eq!(
            scheduler_tick(&state).await,
            handlers::api::Admission::Started,
            "show {waiting}"
        );
        assert_eq!(on_air(&state).await.as_deref(), Some("host"));
        assert_eq!(
            state.recording_manager.lock().await.current_show_id(),
            None,
            "the recording of show {waiting} is detached"
        );
    }
}

#[tokio::test]
async fn failed_prerecorded_start_leaves_a_waiting_recording_alone() {
    let (state, _temp) = state().await;
    let host = add_user(&state, "host", "host").await;
    prerecorded_show(&state, host).await;
    state.recording_manager.lock().await.start(2).await.unwrap();
    state
        .recording_finalizer
        .schedule(handlers::recording::LiveFinalize::new(&state), 2);
    producer_busy(&state).await;

    assert!(matches!(
        scheduler_tick(&state).await,
        handlers::api::Admission::Missed(_)
    ));
    assert_eq!(
        state.recording_manager.lock().await.current_show_id(),
        Some(2)
    );
}

/// A host may switch a show created as live to prerecorded on the show page.
/// Confirming the upload must save that, or the scheduler never starts it.
#[tokio::test]
async fn confirming_an_upload_makes_a_live_show_prerecorded() {
    let (state, _temp) = state().await;
    let host = add_user(&state, "host", "host").await;
    sqlx::query(
        "INSERT INTO shows (id, title, date, start_time, end_time, host_user_id, stream_mode, prerecorded_key) \
         VALUES (1, 'Tape', '2026-11-08', '12:00', '14:00', ?, 'live', 'shows/1/tape.mp3')",
    )
    .bind(host)
    .execute(&state.db)
    .await
    .unwrap();

    body(
        handlers::api::api_my_show_confirm(
            State(state.clone()),
            Query(handlers::api::ShowIdQuery { show_id: 1 }),
            headers(&state, host).await,
        )
        .await
        .unwrap(),
    )
    .await;

    let (mode, confirmed): (String, Option<String>) =
        sqlx::query_as("SELECT stream_mode, prerecorded_confirmed_at FROM shows WHERE id = 1")
            .fetch_one(&state.db)
            .await
            .unwrap();
    assert_eq!(mode, "prerecorded");
    assert!(confirmed.is_some());
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
    let mut socket = connect_producer(&state, addr, host, "test=true").await;
    socket.send(WsMessage::Text("ping:1".into())).await.unwrap();
    assert!(next_text(&mut socket).await.starts_with("pong:"));

    assert_eq!(
        next_text(&mut socket).await,
        handlers::stream_ws::NO_AUDIO_ERROR
    );
    close(socket).await;
    assert!(!state.soundcheck_state.lock().await.is_active());
}

#[tokio::test]
async fn first_audio_chunk_lifts_the_no_audio_deadline() {
    let (state, _temp, addr) = serve_stream_socket_with(
        json!({ "icecast_test_url": "pipe:1", "stream_first_audio_timeout_secs": 1 }),
    )
    .await;
    let host = add_user(&state, "host", "host").await;
    let mut socket = connect_producer(&state, addr, host, "test=true").await;
    socket
        .send(WsMessage::Binary(WEBM_MAGIC.to_vec()))
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_millis(1500)).await;

    socket.send(WsMessage::Text("ping:1".into())).await.unwrap();
    // An error frame for the deadline would arrive before the pong.
    assert!(next_text(&mut socket).await.starts_with("pong:"));
    assert!(state.soundcheck_state.lock().await.is_active());
}

/// The Studio soundcheck's "Server" check reads `chunks` from the pong: it
/// must be 0 while no audio arrived and count each chunk after.
#[tokio::test]
async fn test_broadcast_pong_counts_the_audio_the_server_got() {
    let (state, _temp, addr) = serve_stream_socket().await;
    let host = add_user(&state, "host", "host").await;
    let mut socket = connect_producer(&state, addr, host, "test=true").await;

    async fn counters(socket: &mut Producer) -> (Option<u64>, Option<u64>) {
        socket.send(WsMessage::Text("ping:1".into())).await.unwrap();
        let pong: Value =
            serde_json::from_str(next_text(socket).await.strip_prefix("pong:").unwrap()).unwrap();
        (pong["chunks"].as_u64(), pong["bytes"].as_u64())
    }

    assert_eq!(counters(&mut socket).await, (Some(0), Some(0)));
    for _ in 0..4 {
        socket
            .send(WsMessage::Binary(WEBM_MAGIC.to_vec()))
            .await
            .unwrap();
    }
    assert_eq!(
        counters(&mut socket).await,
        (Some(4), Some(4 * WEBM_MAGIC.len() as u64))
    );
    close(socket).await;
}

/// A tone as WebM/Opus, laid out like the browser's MediaRecorder stream.
async fn webm_tone(frequency: u32, seconds: u32) -> Vec<u8> {
    let out = tokio::process::Command::new("ffmpeg")
        .args(["-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i"])
        .arg(format!(
            "sine=frequency={frequency}:duration={seconds}:sample_rate=48000"
        ))
        .args(["-ac", "2", "-c:a", "libopus", "-f", "webm"])
        .args(["-cluster_time_limit", "250", "pipe:1"])
        .output()
        .await
        .expect("run ffmpeg");
    assert!(out.status.success(), "ffmpeg could not make the tone");
    out.stdout
}

/// Where the stream's header ends: at its first Cluster.
fn webm_header_len(webm: &[u8]) -> usize {
    webm.windows(4)
        .position(|w| w == [0x1f, 0x43, 0xb6, 0x75])
        .expect("a WebM stream has a Cluster")
}

async fn send_audio(socket: &mut Producer, bytes: &[u8]) {
    for chunk in bytes.chunks(4096) {
        socket
            .send(WsMessage::Binary(chunk.to_vec()))
            .await
            .unwrap();
    }
}

/// Seconds of audio in the recording that `stop_recording` joins.
async fn recorded_seconds(state: &Arc<AppState>) -> f64 {
    let joined = state
        .stream_state
        .lock()
        .await
        .stop_recording()
        .await
        .unwrap()
        .expect("a recording");
    let out = tokio::process::Command::new("ffprobe")
        .args(["-v", "error", "-show_entries", "format=duration"])
        .args(["-of", "csv=p=0"])
        .arg(&joined)
        .output()
        .await
        .expect("run ffprobe");
    String::from_utf8_lossy(&out.stdout).trim().parse().unwrap()
}

/// The panel reconnects by itself, also after a long drop. The episode and
/// its recording wait for it, and the reconnect continues the recorder's
/// WebM stream: one recording with all the audio.
#[tokio::test]
async fn a_drop_longer_than_30_s_still_ends_in_one_recording() {
    let (state, _temp, addr) = serve_stream_socket().await;
    let host = add_user(&state, "host", "host").await;
    hosted_show(&state, host).await;
    let tone = webm_tone(440, 4).await;
    let (sent, rest) = tone.split_at(tone.len() / 2);

    let mut first = connect_producer(&state, addr, host, "show_id=1").await;
    first
        .send(WsMessage::Text("stream:a".into()))
        .await
        .unwrap();
    send_audio(&mut first, sent).await;
    let archive = state.recording_manager.lock().await.get_temp_file_path();
    close(first).await;

    tokio::time::pause();
    tokio::time::advance(Duration::from_secs(40)).await;
    tokio::time::resume();
    assert_eq!(handlers::series::on_air_show_id(&state).await, Some(1));

    let mut back = connect_producer(&state, addr, host, "show_id=1&resume=a").await;
    assert_eq!(
        state.recording_manager.lock().await.get_temp_file_path(),
        archive,
        "the reconnect resumes the same recording"
    );
    // The new producer needs the header; the recorder has it already.
    send_audio(&mut back, &tone[..webm_header_len(&tone)]).await;
    send_audio(&mut back, rest).await;
    close(back).await;

    let seconds = recorded_seconds(&state).await;
    assert!((seconds - 4.0).abs() < 0.3, "recorded {seconds} s of 4 s");
}

/// A reload starts a new WebM stream in the same episode. The recorder
/// cannot read a second WebM header, so a new recorder process writes on
/// into the same recording.
#[tokio::test]
async fn a_new_webm_stream_continues_the_same_recording() {
    let (state, _temp, addr) = serve_stream_socket().await;
    let host = add_user(&state, "host", "host").await;
    hosted_show(&state, host).await;

    let mut first = connect_producer(&state, addr, host, "show_id=1").await;
    first
        .send(WsMessage::Text("stream:a".into()))
        .await
        .unwrap();
    send_audio(&mut first, &webm_tone(440, 3).await).await;
    close(first).await;

    let mut reloaded = connect_producer(&state, addr, host, "show_id=1").await;
    reloaded
        .send(WsMessage::Text("stream:b".into()))
        .await
        .unwrap();
    send_audio(&mut reloaded, &webm_tone(880, 3).await).await;
    close(reloaded).await;

    let seconds = recorded_seconds(&state).await;
    assert!((seconds - 6.0).abs() < 0.3, "recorded {seconds} s of 6 s");
}

/// After a drop the browser re-sends what the server may have missed, from
/// the last offset the server confirmed. The overlap is recorded once.
#[tokio::test]
async fn audio_re_sent_after_a_drop_is_recorded_once() {
    let (state, _temp, addr) = serve_stream_socket().await;
    let host = add_user(&state, "host", "host").await;
    hosted_show(&state, host).await;
    let tone = webm_tone(440, 4).await;
    let mid = tone.len() / 2;

    let mut first = connect_producer(&state, addr, host, "show_id=1").await;
    first
        .send(WsMessage::Text("stream:a".into()))
        .await
        .unwrap();
    send_audio(&mut first, &tone[..mid]).await;
    first.send(WsMessage::Text("ping:1".into())).await.unwrap();
    let pong: Value =
        serde_json::from_str(next_text(&mut first).await.strip_prefix("pong:").unwrap()).unwrap();
    assert_eq!(
        (pong["stream"].as_str(), pong["offset"].as_u64()),
        (Some("a"), Some(mid as u64))
    );
    close(first).await;

    // The browser had confirmed less than the server got.
    let at = mid - 5000;
    let mut back = connect_producer(&state, addr, host, "show_id=1&resume=a").await;
    send_audio(&mut back, &tone[..webm_header_len(&tone)]).await;
    back.send(WsMessage::Text(format!("at:{at}")))
        .await
        .unwrap();
    send_audio(&mut back, &tone[at..]).await;
    close(back).await;

    // Every byte of the stream reached the recorder exactly once.
    let received = state.stream_state.lock().await.webm_received().1;
    assert_eq!(received, tone.len() as u64);
    let seconds = recorded_seconds(&state).await;
    assert!((seconds - 4.0).abs() < 0.3, "recorded {seconds} s of 4 s");
}

/// After a reload the Studio asks for a new soundcheck while the live show
/// waits for its reconnect. The rehearsal must not end that recording or
/// reach it.
#[tokio::test]
async fn a_rehearsal_during_the_reconnect_grace_keeps_the_live_recording() {
    let (state, _temp, addr) = serve_stream_socket().await;
    let host = add_user(&state, "host", "host").await;
    hosted_show(&state, host).await;

    let mut live = connect_producer(&state, addr, host, "show_id=1").await;
    live.send(WsMessage::Text("stream:a".into())).await.unwrap();
    send_audio(&mut live, &webm_tone(440, 3).await).await;
    let archive = state.recording_manager.lock().await.get_temp_file_path();
    close(live).await;

    let mut rehearsal = connect_producer(&state, addr, host, "test=true").await;
    rehearsal
        .send(WsMessage::Text("stream:t".into()))
        .await
        .unwrap();
    send_audio(&mut rehearsal, &webm_tone(660, 2).await).await;
    rehearsal
        .send(WsMessage::Text("stop".into()))
        .await
        .unwrap();
    close(rehearsal).await;
    assert_eq!(
        state.recording_manager.lock().await.get_temp_file_path(),
        archive,
        "the live recording still waits for its reconnect"
    );

    let mut back = connect_producer(&state, addr, host, "show_id=1").await;
    back.send(WsMessage::Text("stream:b".into())).await.unwrap();
    send_audio(&mut back, &webm_tone(880, 3).await).await;
    close(back).await;

    let seconds = recorded_seconds(&state).await;
    assert!(
        (seconds - 6.0).abs() < 0.3,
        "recorded {seconds} s, want the 6 s of the live show only"
    );
}
