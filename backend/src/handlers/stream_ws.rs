//! WebSocket handler for audio streaming.

use crate::auth::get_current_user;
use crate::stream_bridge::SharedStreamState;
use crate::{AppState, Result};
use axum::{
    extract::{Query, State, WebSocketUpgrade},
    http::StatusCode,
    response::{IntoResponse, Response},
};
use axum::extract::ws::{Message, WebSocket};
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use std::sync::Arc;

#[derive(Deserialize)]
pub struct StreamQuery {
    /// If true, forcefully take over an existing stream
    #[serde(default)]
    pub force: bool,
}

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

    // Upgrade to WebSocket
    Ok(ws.on_upgrade(move |socket| handle_stream_socket(socket, state, stream_state, username)))
}

/// Handle the WebSocket connection for streaming.
async fn handle_stream_socket(
    socket: WebSocket,
    state: Arc<AppState>,
    stream_state: SharedStreamState,
    username: String,
) {
    let (mut sender, mut receiver) = socket.split();

    // Start the stream
    let rtmp_destination = state.config.rtmp_destination();
    {
        let mut stream = stream_state.lock().await;
        if let Err(e) = stream.start_stream(username.clone(), &rtmp_destination).await {
            tracing::error!("Failed to start stream: {}", e);
            let _ = sender.send(Message::Text(format!("error: {}", e).into())).await;
            let _ = sender.close().await;
            return;
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

    // Process incoming messages (audio chunks)
    while let Some(msg) = receiver.next().await {
        match msg {
            Ok(Message::Binary(data)) => {
                let mut stream = stream_state.lock().await;
                if let Err(e) = stream.write_chunk(&data).await {
                    tracing::error!("Failed to write audio chunk: {}", e);
                    let _ = sender.send(Message::Text(format!("error: {}", e).into())).await;
                    break;
                }
            }
            Ok(Message::Text(text)) => {
                // Handle control messages
                if text.as_str() == "stop" {
                    tracing::info!("Received stop command from '{}'", username);
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

    // Stop the stream on disconnect
    {
        let mut stream = stream_state.lock().await;
        if let Err(e) = stream.stop_stream().await {
            tracing::error!("Failed to stop stream: {}", e);
        }
    }

    tracing::info!("Stream ended for user '{}'", username);
}

/// Get current stream status.
pub async fn stream_status(
    State(stream_state): State<SharedStreamState>,
) -> impl IntoResponse {
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

    let mut stream = stream_state.lock().await;
    if stream.is_active() {
        if let Err(e) = stream.stop_stream().await {
            tracing::error!("Failed to stop stream: {}", e);
            return Ok((StatusCode::INTERNAL_SERVER_ERROR, format!("Failed to stop: {}", e)).into_response());
        }
        Ok(axum::Json(serde_json::json!({"success": true, "message": "Stream stopped"})).into_response())
    } else {
        Ok(axum::Json(serde_json::json!({"success": true, "message": "No active stream"})).into_response())
    }
}
