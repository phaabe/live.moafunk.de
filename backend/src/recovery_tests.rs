//! Recovery behavior through the HTTP handlers and real SQLite.
use super::*;
use axum::{extract::Path, http::StatusCode, routing::post, Json};
use serde_json::{json, Value};
use tokio::sync::mpsc;

async fn state() -> (
    Arc<AppState>,
    tempfile::TempDir,
    mpsc::Receiver<Value>,
    tokio::task::JoinHandle<()>,
) {
    let (sender, receiver) = mpsc::channel::<Value>(32);
    let mail = Router::new().route(
        "/",
        post(move |Json(body): Json<Value>| {
            let sender = sender.clone();
            async move {
                sender.send(body).await.unwrap();
                StatusCode::OK
            }
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = format!("http://{}/", listener.local_addr().unwrap());
    let server = tokio::spawn(async { axum::serve(listener, mail).await.unwrap() });
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
        recovery_mail: recovery_mail::RecoveryMailer::for_test(&endpoint),
        recovery_controls: handlers::recovery::RecoveryControls::default(),
        stream_state: stream_bridge::new_shared_state(),
        stream_metrics: stream_metrics::new_shared(),
        recording_manager: recording::new_shared_manager(temp.path().to_path_buf()),
        recording_finalizer: Arc::default(),
        cover_debounce: Arc::new(RwLock::new(HashMap::new())),
        default_cover: tokio::sync::OnceCell::new(),
        telegram_bot: None,
        pending_show_notifications: Arc::new(tokio::sync::Mutex::new(HashMap::new())),
        telegram_edit_sessions: Arc::new(tokio::sync::Mutex::new(HashMap::new())),
        chat_hub: Arc::new(chat_bridge::ChatHub::new()),
    });
    (state, temp, receiver, server)
}

async fn add_user(state: &Arc<AppState>, name: &str, role: &str) -> i64 {
    sqlx::query("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)")
        .bind(name)
        .bind(auth::hash_password("old-password").unwrap())
        .bind(role)
        .execute(&state.db)
        .await
        .unwrap()
        .last_insert_rowid()
}
async fn headers(state: &Arc<AppState>, id: i64) -> HeaderMap {
    let token = auth::create_session(state, id).await.unwrap();
    let mut headers = HeaderMap::new();
    headers.insert("cookie", format!("session={token}").parse().unwrap());
    headers
}
async fn enroll(state: &Arc<AppState>, id: i64) {
    let mail = recovery::enroll(&state.db, id, "host@example.org", "old-password")
        .await
        .unwrap();
    recovery::verify(&state.db, mail.token.as_deref().unwrap())
        .await
        .unwrap();
}
fn request(value: Value) -> handlers::recovery::EmailRequest {
    serde_json::from_value(value).unwrap()
}
fn peer() -> axum::extract::ConnectInfo<std::net::SocketAddr> {
    axum::extract::ConnectInfo("127.0.0.1:5000".parse().unwrap())
}

#[tokio::test]
async fn email_reset_revokes_sessions_and_allows_username_login() {
    let (state, _temp, mut mail, server) = state().await;
    let id = add_user(&state, "radio-host", "host").await;
    let old_headers = headers(&state, id).await;
    let app = handlers::recovery::routes()
        .route("/api/auth/login", post(handlers::api::api_login))
        .with_state(state.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let http = tokio::spawn(async move {
        axum::serve(
            listener,
            app.into_make_service_with_connect_info::<std::net::SocketAddr>(),
        )
        .await
        .unwrap();
    });
    let client = reqwest::Client::new();
    let response = client
        .post(format!("{base}/api/auth/password-reset/request"))
        .json(&json!({"email":"x".repeat(8192)}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::PAYLOAD_TOO_LARGE);
    let response = client
        .post(format!("{base}/api/auth/recovery-email"))
        .headers(old_headers.clone())
        .json(&json!({"email":" HOST@example.org ","current_password":"old-password"}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let verification = tokio::time::timeout(std::time::Duration::from_secs(5), mail.recv())
        .await
        .unwrap()
        .unwrap();
    let token = mail_token(&verification);
    let response = client
        .get(format!("{base}/api/auth/recovery-email/verify"))
        .query(&[("token", token)])
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::METHOD_NOT_ALLOWED);
    let response = client
        .post(format!("{base}/api/auth/recovery-email/verify"))
        .json(&json!({"token":token}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let response = client
        .post(format!("{base}/api/auth/password-reset/request"))
        .json(&json!({"email":"host@example.org"}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let delivered = tokio::time::timeout(std::time::Duration::from_secs(5), mail.recv())
        .await
        .unwrap()
        .unwrap();
    assert!(delivered["text"].as_str().unwrap().contains("radio-host"));
    let response = client
        .post(format!("{base}/api/auth/password-reset/complete"))
        .json(&json!({"token":mail_token(&delivered),"new_password":"new-password"}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let old_session = auth::get_session_from_headers(&old_headers);
    assert!(auth::get_current_user(&state, old_session.as_deref())
        .await
        .is_none());
    for (password, expected) in [
        ("new-password", StatusCode::OK),
        ("old-password", StatusCode::UNAUTHORIZED),
    ] {
        let response = client
            .post(format!("{base}/api/auth/login"))
            .json(&json!({"username":"radio-host","password":password}))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), expected);
    }
    http.abort();
    server.abort();
}

fn mail_token(mail: &Value) -> &str {
    mail["text"]
        .as_str()
        .unwrap()
        .split("token=")
        .nth(1)
        .unwrap()
        .lines()
        .next()
        .unwrap()
}

#[tokio::test]
async fn public_request_is_generic_and_limits_unknown_addresses_too() {
    let (state, _temp, _mail, server) = state().await;
    let id = add_user(&state, "host", "host").await;
    enroll(&state, id).await;
    let mut bodies = Vec::new();
    for address in ["host@example.org", "unknown@example.org"] {
        bodies.push(
            handlers::recovery::request_reset(
                State(state.clone()),
                peer(),
                HeaderMap::new(),
                Json(request(json!({"email":address}))),
            )
            .await
            .unwrap()
            .0,
        );
    }
    assert_eq!(bodies[0], bodies[1]);
    for _ in 1..5 {
        let response = handlers::recovery::request_reset(
            State(state.clone()),
            peer(),
            HeaderMap::new(),
            Json(request(json!({"email":" UNKNOWN@example.org "}))),
        )
        .await
        .unwrap();
        assert_eq!(response.0["success"], true);
    }
    let error = handlers::recovery::request_reset(
        State(state),
        peer(),
        HeaderMap::new(),
        Json(request(json!({"email":"unknown@example.org"}))),
    )
    .await
    .unwrap_err();
    assert_eq!(
        error.into_response().status(),
        StatusCode::TOO_MANY_REQUESTS
    );
    server.abort();
}

#[tokio::test]
async fn all_existing_password_handlers_invalidate_reset_links() {
    let (state, _temp, _mail, server) = state().await;
    let id = add_user(&state, "host", "host").await;
    let admin = add_user(&state, "admin", "admin").await;
    enroll(&state, id).await;
    let user_headers = headers(&state, id).await;
    let token = recovery::request_reset(&state.db, "host@example.org")
        .await
        .unwrap()
        .unwrap()
        .token
        .unwrap();
    let req = serde_json::from_value(
        json!({"current_password":"old-password","new_password":"chosen-password"}),
    )
    .unwrap();
    handlers::api::api_change_password(State(state.clone()), user_headers.clone(), Json(req))
        .await
        .unwrap();
    assert!(
        recovery::complete_reset(&state.db, &token, "attacker-password")
            .await
            .is_err()
    );
    let token = recovery::request_reset(&state.db, "host@example.org")
        .await
        .unwrap()
        .unwrap()
        .token
        .unwrap();
    handlers::api::api_reset_password(State(state.clone()), Path(id), headers(&state, admin).await)
        .await
        .unwrap();
    assert!(
        recovery::complete_reset(&state.db, &token, "attacker-password")
            .await
            .is_err()
    );
    let token = recovery::request_reset(&state.db, "host@example.org")
        .await
        .unwrap()
        .unwrap()
        .token
        .unwrap();
    let req = serde_json::from_value(json!({"new_password":"final-password"})).unwrap();
    handlers::api::api_set_initial_password(State(state.clone()), user_headers, Json(req))
        .await
        .unwrap();
    assert!(
        recovery::complete_reset(&state.db, &token, "attacker-password")
            .await
            .is_err()
    );
    server.abort();
}
