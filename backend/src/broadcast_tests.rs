//! Broadcast permissions and login warnings against real handlers and SQLite.

use super::*;
use axum::{body::to_bytes, extract::Path, http::StatusCode};
use serde_json::{json, Value};

async fn state() -> (Arc<AppState>, tempfile::TempDir) {
    let db = sqlx::SqlitePool::connect("sqlite::memory:").await.unwrap();
    db::run_migrations(&db).await.unwrap();
    let mut config: Config = serde_json::from_value(json!({
        "secret_key": "test-only", "superadmin_password_hash": "unused",
        "r2_account_id": "unused", "r2_access_key_id": "unused", "r2_secret_access_key": "unused",
        // Never push a test stream to the production default.
        "rtmp_url": "rtmp://127.0.0.1:1/live"
    }))
    .unwrap();
    // Local dead end so presigning works offline.
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
        recording_finalizer: Arc::default(),
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
