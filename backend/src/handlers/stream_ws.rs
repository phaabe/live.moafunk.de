//! WebSocket handler for audio streaming.

use crate::auth::get_current_user;
use crate::handlers::recording::LiveFinalize;
use crate::recording_finalizer::FINALIZE_GRACE;
use crate::stream_bridge::SharedStreamState;
use crate::{AppState, Result};
use axum::extract::ws::{Message, WebSocket};
use axum::{
    extract::{Query, State, WebSocketUpgrade},
    http::StatusCode,
    response::{IntoResponse, Response},
};
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use std::sync::Arc;

#[derive(Deserialize)]
pub struct StreamQuery {
    /// If true, forcefully take over an existing stream
    #[serde(default)]
    pub force: bool,
    /// Show being broadcast. When present, recording auto-starts for this show
    /// on go-live (no separate frontend call) and finalizes when the stream ends.
    pub show_id: Option<i64>,
    /// Rehearsal mode: push to the private `/test` Icecast mount instead of
    /// `/live`. No recording, no Telegram, and excluded from the public on-air
    /// signal — the broadcaster can hear exactly what listeners would, privately,
    /// before going live. Requires `ICECAST_TEST_URL` to be configured.
    #[serde(default)]
    pub test: bool,
}

/// Inter-chunk cadence gap above which a chunk counts as "late" (#277).
/// The browser's MediaRecorder emits every 250 ms. Note this measures the
/// receive cadence as seen by this task — network stall AND server-side
/// processing (relay write / lock contention) both widen it — so treat it
/// as "delivery fell behind", not as a pure network metric.
const LATE_CHUNK_GAP: std::time::Duration = std::time::Duration::from_millis(1000);

/// WebSocket upgrade handler for streaming.
///
/// Authentication is done via session cookie.
/// If another user is streaming, the connection is rejected unless `?force=true`.
pub async fn stream_ws_handler(
    ws: WebSocketUpgrade,
    State(state): State<Arc<AppState>>,
    State(stream_state): State<SharedStreamState>,
    Query(query): Query<StreamQuery>,
    headers: axum::http::HeaderMap,
) -> Result<Response> {
    // Extract session token from cookie header
    let token = headers
        .get(axum::http::header::COOKIE)
        .and_then(|v| v.to_str().ok())
        .and_then(|cookies| {
            cookies.split(';').find_map(|cookie| {
                let cookie = cookie.trim();
                if cookie.starts_with("session=") {
                    Some(cookie[8..].to_string())
                } else {
                    None
                }
            })
        });

    // Authenticate user
    let user = get_current_user(&state, token.as_deref()).await;
    let user = match user {
        Some(u) => u,
        None => {
            return Ok((StatusCode::UNAUTHORIZED, "Not authenticated").into_response());
        }
    };

    let username = user.username.clone();

    crate::auth::authorize_broadcast(&state.db, &user, query.show_id, query.test).await?;

    // Check if someone else is streaming
    {
        let stream = stream_state.lock().await;
        if stream.is_active() {
            if let Some(ref current_user) = stream.current_user {
                if current_user != &username
                    && (!query.force || !crate::auth::can_control_stream(&user, Some(current_user)))
                {
                    return Ok((
                        StatusCode::CONFLICT,
                        format!("Stream already active by user '{}'", current_user),
                    )
                        .into_response());
                }
            }
        }
    }

    // A test broadcast never records, so it ignores show_id entirely.
    let test = query.test;
    let show_id = if test { None } else { query.show_id };

    // Upgrade to WebSocket
    Ok(ws.on_upgrade(move |socket| {
        handle_stream_socket(socket, state, stream_state, username, show_id, test)
    }))
}

/// A live producer dropped: finalize its show's recording after
/// [`FINALIZE_GRACE`] unless a producer goes live first, so a reconnect
/// resumes the same archive.
async fn defer_finalize(state: &Arc<AppState>) {
    let active_show = state.recording_manager.lock().await.current_show_id();
    if let Some(sid) = active_show {
        tracing::info!(
            "Stream for show {} dropped; deferring finalize for {}s in case of reconnect",
            sid,
            FINALIZE_GRACE.as_secs()
        );
        state
            .recording_finalizer
            .schedule(LiveFinalize::new(state), sid);
    }
}

/// Handle the WebSocket connection for streaming.
///
/// `show_id` (when present) drives automatic recording: it starts when the
/// stream goes live and is finalized when the stream truly ends. A transient
/// disconnect defers finalize for a grace period so a reconnect resumes the
/// same archive rather than fragmenting it.
async fn handle_stream_socket(
    socket: WebSocket,
    state: Arc<AppState>,
    stream_state: SharedStreamState,
    username: String,
    show_id: Option<i64>,
    test: bool,
) {
    let (mut sender, mut receiver) = socket.split();

    // Pick the producer target. A test broadcast pushes to the private `/test`
    // mount; if no test mount is configured, reject rather than leak the
    // rehearsal onto the public `/live`.
    let push_target = if test {
        match state.config.test_producer_target() {
            Some(t) => t,
            None => {
                tracing::warn!("Rejected test broadcast: ICECAST_TEST_URL not configured");
                let _ = sender
                    .send(Message::Text(
                        "error: test stream is not configured on this server".into(),
                    ))
                    .await;
                let _ = sender.close().await;
                return;
            }
        }
    } else {
        state.config.producer_target()
    };

    // Start the stream
    {
        let mut stream = stream_state.lock().await;
        if let Err(e) = stream
            .start_stream(username.clone(), &push_target, test)
            .await
        {
            tracing::error!("Failed to start stream: {}", e);
            let _ = sender
                .send(Message::Text(format!("error: {}", e).into()))
                .await;
            let _ = sender.close().await;
            return;
        }
    }

    // Auto-record on go-live. A reconnect of the same show ends the grace wait
    // and resumes the existing recording (ensure_recording_started is a no-op
    // when already recording this show). A wait of another show, or a rehearsal
    // during it, finalizes that show first, so this producer's audio never
    // reaches its file. A finalize that has started is never aborted. A
    // recording failure here must not kill the live broadcast.
    state
        .recording_finalizer
        .go_live(LiveFinalize::new(&state), show_id)
        .await;
    if let Some(sid) = show_id {
        if let Err(e) = crate::handlers::recording::ensure_recording_started(&state, sid).await {
            tracing::error!("Failed to auto-start recording for show {}: {}", sid, e);
        }
    }

    // Send confirmation
    if let Err(e) = sender.send(Message::Text("connected".into())).await {
        tracing::error!("Failed to send connected message: {}", e);
        let _ = stream_state.lock().await.stop_stream().await;
        defer_finalize(&state).await;
        return;
    }

    tracing::info!(
        "Stream started for user '{}'{}",
        username,
        if test { " (test)" } else { "" }
    );

    // Notify admin via Telegram (fire-and-forget). A test broadcast stays silent
    // — no point pinging the group for a private rehearsal.
    if !test {
        crate::telegram_notify::notify_stream_start(&state, &username);
    }

    // Process incoming messages (audio chunks). `explicit_stop` distinguishes a
    // deliberate end (finalize immediately) from a network drop (grace period).
    //
    // Per-connection delivery telemetry (#277): browsers can't send protocol
    // pings from JS, so the panel sends `ping:<client-timestamp>` text frames
    // and we echo `pong:{...}` with the counters piggybacked — that both
    // measures RTT and confirms how much audio actually arrived.
    let mut explicit_stop = false;
    let mut chunks_received: u64 = 0;
    let mut bytes_received: u64 = 0;
    let mut late_chunks: u64 = 0;
    let mut dropped_chunks: u64 = 0;
    let mut last_chunk_at: Option<std::time::Instant> = None;
    while let Some(msg) = receiver.next().await {
        match msg {
            Ok(Message::Binary(data)) => {
                let now = std::time::Instant::now();
                if let Some(prev) = last_chunk_at {
                    if now.duration_since(prev) > LATE_CHUNK_GAP {
                        late_chunks += 1;
                    }
                }
                last_chunk_at = Some(now);
                chunks_received += 1;
                bytes_received += data.len() as u64;

                let mut stream = stream_state.lock().await;
                if let Err(e) = stream.write_chunk(&data).await {
                    dropped_chunks += 1;
                    tracing::error!("Failed to write audio chunk: {}", e);
                    let _ = sender
                        .send(Message::Text(format!("error: {}", e).into()))
                        .await;
                    break;
                }
            }
            Ok(Message::Text(text)) => {
                // Handle control messages
                if text.as_str() == "stop" {
                    tracing::info!("Received stop command from '{}'", username);
                    explicit_stop = true;
                    break;
                } else if let Some(echo) = text.as_str().strip_prefix("ping:") {
                    // Parse-and-reformat instead of echoing raw text so a
                    // malformed payload can't inject into the JSON reply.
                    if let Ok(echo_ms) = echo.parse::<f64>() {
                        // No `dropped` here: a write error breaks the loop, so
                        // a live pong could only ever report 0. It surfaces in
                        // the end-of-stream log instead.
                        let payload = serde_json::json!({
                            "echo": echo_ms,
                            "chunks": chunks_received,
                            "bytes": bytes_received,
                            "late": late_chunks,
                        });
                        if sender
                            .send(Message::Text(format!("pong:{}", payload).into()))
                            .await
                            .is_err()
                        {
                            break;
                        }
                    }
                }
            }
            Ok(Message::Close(_)) => {
                tracing::info!("WebSocket closed by client '{}'", username);
                break;
            }
            Ok(Message::Ping(data)) => {
                if sender.send(Message::Pong(data)).await.is_err() {
                    break;
                }
            }
            Ok(_) => {}
            Err(e) => {
                tracing::error!("WebSocket error: {}", e);
                break;
            }
        }
    }

    // Stop the FFmpeg stream on disconnect.
    {
        let mut stream = stream_state.lock().await;
        if let Err(e) = stream.stop_stream().await {
            tracing::error!("Failed to stop stream: {}", e);
        }
    }

    // Decide the recording's fate, decoupled from the browser tab:
    // - explicit stop  → finalize + upload now.
    // - network drop    → defer finalize so a reconnect can resume the archive.
    if explicit_stop {
        let finalized = state
            .recording_finalizer
            .finalize_now(LiveFinalize::new(&state))
            .await;
        match finalized {
            Ok(Some(r)) => tracing::info!(
                "Finalized recording for show {} on explicit stop (version {}, incomplete={})",
                r.show_id,
                r.version,
                r.incomplete
            ),
            Ok(None) => {}
            Err(e) => tracing::error!("Finalize on explicit stop failed: {}", e),
        }
    } else {
        defer_finalize(&state).await;
    }

    tracing::info!(
        "Stream ended for user '{}': {} chunks / {} bytes received, {} late, {} dropped",
        username,
        chunks_received,
        bytes_received,
        late_chunks,
        dropped_chunks
    );

    // Notify admin via Telegram (fire-and-forget). Suppressed for a test.
    if !test {
        crate::telegram_notify::notify_stream_stop(&state, &username);
    }
}

/// Get current stream status.
pub async fn stream_status(State(stream_state): State<SharedStreamState>) -> impl IntoResponse {
    let stream = stream_state.lock().await;
    let status = stream.get_status();
    axum::Json(status)
}

/// Stop the current stream (admin endpoint).
pub async fn stream_stop(
    State(state): State<Arc<AppState>>,
    State(stream_state): State<SharedStreamState>,
    headers: axum::http::HeaderMap,
) -> Result<impl IntoResponse> {
    // Extract session token from cookie header
    let token = headers
        .get(axum::http::header::COOKIE)
        .and_then(|v| v.to_str().ok())
        .and_then(|cookies| {
            cookies.split(';').find_map(|cookie| {
                let cookie = cookie.trim();
                if cookie.starts_with("session=") {
                    Some(cookie[8..].to_string())
                } else {
                    None
                }
            })
        });

    // Authenticate user
    let user = get_current_user(&state, token.as_deref()).await;
    let Some(user) = user else {
        return Ok((StatusCode::UNAUTHORIZED, "Not authenticated").into_response());
    };

    let was_active = {
        let mut stream = stream_state.lock().await;
        if stream.is_active() {
            if !crate::auth::can_control_stream(&user, stream.current_user.as_deref()) {
                return Err(crate::AppError::Forbidden(
                    "You cannot stop another user's stream".into(),
                ));
            }
            if let Err(e) = stream.stop_stream().await {
                tracing::error!("Failed to stop stream: {}", e);
                return Ok((
                    StatusCode::INTERNAL_SERVER_ERROR,
                    format!("Failed to stop: {}", e),
                )
                    .into_response());
            }
            true
        } else {
            false
        }
    };

    if was_active || user.role_enum().can_access_admin() {
        // Admin stop is a deliberate end: finalize the recording immediately
        // rather than waiting for the grace period.
        let finalized = state
            .recording_finalizer
            .finalize_now(LiveFinalize::new(&state))
            .await;
        if let Err(e) = finalized {
            tracing::error!("Finalize on admin stop failed: {}", e);
        }
        Ok(
            axum::Json(serde_json::json!({"success": true, "message": "Stream stopped"}))
                .into_response(),
        )
    } else {
        Ok(
            axum::Json(serde_json::json!({"success": true, "message": "No active stream"}))
                .into_response(),
        )
    }
}
