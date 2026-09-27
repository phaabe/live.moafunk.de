//! Broadcast permissions and login warnings against real handlers and SQLite.

use super::*;
use axum::{body::to_bytes, extract::Path, http::StatusCode};
use serde_json::{json, Value};

async fn state() -> (Arc<AppState>, tempfile::TempDir) {
    let db = sqlx::SqlitePool::connect("sqlite::memory:").await.unwrap();
    db::run_migrations(&db).await.unwrap();
    let config: Config = serde_json::from_value(json!({
        "secret_key": "test-only", "superadmin_password_hash": "unused",
        "r2_account_id": "unused", "r2_access_key_id": "unused", "r2_secret_access_key": "unused"
    }))
    .unwrap();
    let temp = tempfile::tempdir().unwrap();
    let state = Arc::new(AppState {
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

async fn add_user(state: &Arc<AppState>, username: &str, role: &str) -> i64 {
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
