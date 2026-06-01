//! WebSocket handler for audio streaming.

use crate::auth::get_current_user;
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
}

/// Grace period after a live WS drops before the recording is finalized.
/// A reconnect within this window resumes the same archive instead of starting
/// a new one — so a flaky connection doesn't fragment the recording.
const FINALIZE_GRACE: std::time::Duration = std::time::Duration::from_secs(30);

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

    // Check if someone else is streaming
    {
        let stream = stream_state.lock().await;
        if stream.is_active() {
            if let Some(ref current_user) = stream.current_user {
                if current_user != &username && !query.force {
                    return Ok((
                        StatusCode::CONFLICT,
                        format!("Stream already active by user '{}'", current_user),
                    )
                        .into_response());
                }
            }
        }
    }

    let show_id = query.show_id;

    // Upgrade to WebSocket
    Ok(ws.on_upgrade(move |socket| {
        handle_stream_socket(socket, state, stream_state, username, show_id)
    }))
}

/// Cancel a pending grace-period finalize, if one is scheduled.
///
/// Called on (re)connect and before an explicit finalize so a transient drop
/// followed by a reconnect keeps appending to the same archive.
async fn cancel_pending_finalize(state: &Arc<AppState>) {
    let mut guard = state.recording_finalizer.lock().await;
    if let Some(handle) = guard.take() {
        handle.abort();
        tracing::debug!("Cancelled pending recording finalize (reconnect or explicit stop)");
    }
}

/// Schedule a deferred finalize after [`FINALIZE_GRACE`]. If the stream becomes
/// active again (reconnect) before it fires — or the cancel handle is aborted —
/// the recording is left running and continues into the same archive.
async fn schedule_grace_finalize(state: Arc<AppState>, show_id: i64) {
    let task_state = state.clone();
    let task = tokio::spawn(async move {
        tokio::time::sleep(FINALIZE_GRACE).await;

        // A reconnect would have made the stream active again.
        if task_state.stream_state.lock().await.is_active() {
            tracing::info!(
                "Grace finalize skipped for show {}: stream is live again",
                show_id
            );
            return;
        }
        // Only finalize if we're still recording the same show.
        if task_state.recording_manager.lock().await.current_show_id() != Some(show_id) {
            return;
        }

        tracing::info!(
            "Grace period elapsed with no reconnect; finalizing recording for show {}",
            show_id
        );
        match crate::handlers::recording::finalize_and_upload(&task_state).await {
            Ok(Some(r)) => tracing::info!(
                "Auto-finalized recording for show {} (version {}, incomplete={})",
                r.show_id,
                r.version,
                r.incomplete
            ),
            Ok(None) => {}
            Err(e) => tracing::error!("Grace finalize failed for show {}: {}", show_id, e),
        }
    });

    let mut guard = state.recording_finalizer.lock().await;
    if let Some(old) = guard.replace(task.abort_handle()) {
        old.abort();
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
) {
    let (mut sender, mut receiver) = socket.split();

    // Start the stream
    let rtmp_destination = state.config.rtmp_destination();
    {
        let mut stream = stream_state.lock().await;
        if let Err(e) = stream
            .start_stream(username.clone(), &rtmp_destination)
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

    // Auto-record on go-live. A reconnect cancels any pending finalize and
    // resumes the existing recording (ensure_recording_started is a no-op when
    // already recording this show). A recording failure here must not kill the
    // live broadcast.
    if let Some(sid) = show_id {
        cancel_pending_finalize(&state).await;
        if let Err(e) = crate::handlers::recording::ensure_recording_started(&state, sid).await {
            tracing::error!("Failed to auto-start recording for show {}: {}", sid, e);
        }
    }

    // Send confirmation
    if let Err(e) = sender.send(Message::Text("connected".into())).await {
        tracing::error!("Failed to send connected message: {}", e);
        let mut stream = stream_state.lock().await;
        let _ = stream.stop_stream().await;
        return;
    }

    tracing::info!("Stream started for user '{}'", username);

    // Notify admin via Telegram (fire-and-forget)
    crate::telegram_notify::notify_stream_start(&state, &username);

    // Process incoming messages (audio chunks). `explicit_stop` distinguishes a
    // deliberate end (finalize immediately) from a network drop (grace period).
    let mut explicit_stop = false;
    while let Some(msg) = receiver.next().await {
        match msg {
            Ok(Message::Binary(data)) => {
                let mut stream = stream_state.lock().await;
                if let Err(e) = stream.write_chunk(&data).await {
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
        cancel_pending_finalize(&state).await;
        match crate::handlers::recording::finalize_and_upload(&state).await {
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
        let active_show = state.recording_manager.lock().await.current_show_id();
        if let Some(sid) = active_show {
            tracing::info!(
                "Stream for show {} dropped; deferring finalize for {}s in case of reconnect",
                sid,
                FINALIZE_GRACE.as_secs()
            );
            schedule_grace_finalize(state.clone(), sid).await;
        }
    }

    tracing::info!("Stream ended for user '{}'", username);

    // Notify admin via Telegram (fire-and-forget)
    crate::telegram_notify::notify_stream_stop(&state, &username);
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
    if user.is_none() {
        return Ok((StatusCode::UNAUTHORIZED, "Not authenticated").into_response());
    }

    let was_active = {
        let mut stream = stream_state.lock().await;
        if stream.is_active() {
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

    if was_active {
        // Admin stop is a deliberate end: finalize the recording immediately
        // rather than waiting for the grace period.
        cancel_pending_finalize(&state).await;
        if let Err(e) = crate::handlers::recording::finalize_and_upload(&state).await {
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
