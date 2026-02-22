//! WebSocket handler for stream loopback testing.
//!
//! Accepts audio chunks from the browser, buffers ~10 seconds,
//! then plays them back to the sender at real-time pace (250ms intervals).
//! This validates the full capture → encode → network → decode pipeline
//! without actually going live on RTMP.

use crate::auth::get_current_user;
use crate::{AppState, Result};
use axum::extract::ws::{Message, WebSocket};
use axum::{
    extract::{State, WebSocketUpgrade},
    http::StatusCode,
    response::{IntoResponse, Response},
};
use futures_util::{SinkExt, StreamExt};
use std::sync::Arc;
use std::time::Duration;

/// Maximum number of chunks to buffer (~10s at 250ms per chunk = 40 chunks).
const MAX_BUFFER_CHUNKS: usize = 50;

/// Maximum total buffer size in bytes (~500KB, generous for 10s of Opus at 192kbps).
const MAX_BUFFER_BYTES: usize = 512 * 1024;

/// Interval between sending playback chunks back to the client.
const PLAYBACK_INTERVAL: Duration = Duration::from_millis(250);

/// WebSocket upgrade handler for the stream test loopback.
///
/// Authentication is done via session cookie (same pattern as stream_ws.rs).
/// No conflict checking — this is a private loopback, not a real stream.
pub async fn stream_test_ws_handler(
    ws: WebSocketUpgrade,
    State(state): State<Arc<AppState>>,
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

    tracing::info!("Stream test WebSocket upgrade for user '{}'", user.username);

    Ok(ws.on_upgrade(move |socket| handle_stream_test(socket, user.username)))
}

/// Handle the loopback test WebSocket session.
///
/// Protocol:
///   Client → Server:
///     Binary(data)  — audio chunk to buffer
///     Text("play")  — stop buffering, begin playback
///     Text("stop")  — abort test
///     Close         — abort test
///
///   Server → Client:
///     Text("ready")          — connection established, ready for chunks
///     Text("playback-start") — beginning to send buffered chunks
///     Binary(data)           — buffered audio chunk (during playback)
///     Text("playback-done")  — all chunks sent
///     Text("error: ...")     — error message
async fn handle_stream_test(socket: WebSocket, username: String) {
    let (mut sender, mut receiver) = socket.split();

    // Send ready confirmation
    if sender.send(Message::Text("ready".into())).await.is_err() {
        return;
    }

    // Buffer for incoming audio chunks
    let mut buffer: Vec<Vec<u8>> = Vec::with_capacity(MAX_BUFFER_CHUNKS);
    let mut total_bytes: usize = 0;

    tracing::debug!("Stream test: buffering started for '{}'", username);

    // Phase 1: receive and buffer audio chunks until "play" command
    loop {
        match receiver.next().await {
            Some(Ok(Message::Binary(data))) => {
                // Guard against oversized buffers
                if buffer.len() >= MAX_BUFFER_CHUNKS || total_bytes + data.len() > MAX_BUFFER_BYTES
                {
                    tracing::debug!(
                        "Stream test: buffer full ({} chunks, {} bytes) for '{}' — ignoring further chunks",
                        buffer.len(),
                        total_bytes,
                        username
                    );
                    // Don't error out — just stop accepting. Client may send "play" next.
                    continue;
                }

                total_bytes += data.len();
                buffer.push(data.to_vec());
            }
            Some(Ok(Message::Text(text))) => {
                match text.as_str() {
                    "play" => {
                        tracing::info!(
                            "Stream test: play command received for '{}' ({} chunks, {} bytes)",
                            username,
                            buffer.len(),
                            total_bytes
                        );
                        break;
                    }
                    "stop" => {
                        tracing::info!("Stream test: stop command from '{}'", username);
                        return;
                    }
                    _ => {
                        // Unknown text command — ignore
                    }
                }
            }
            Some(Ok(Message::Close(_))) => {
                tracing::debug!(
                    "Stream test: client closed during buffering ('{}')",
                    username
                );
                return;
            }
            Some(Ok(Message::Ping(data))) => {
                if sender.send(Message::Pong(data)).await.is_err() {
                    return;
                }
            }
            Some(Ok(_)) => {}
            Some(Err(e)) => {
                tracing::debug!("Stream test: WebSocket error during buffering: {}", e);
                return;
            }
            None => {
                // Stream ended
                return;
            }
        }
    }

    // Phase 2: play back buffered chunks at real-time pace
    if buffer.is_empty() {
        let _ = sender
            .send(Message::Text("error: no audio data received".into()))
            .await;
        return;
    }

    // Signal playback start
    if sender
        .send(Message::Text("playback-start".into()))
        .await
        .is_err()
    {
        return;
    }

    tracing::debug!(
        "Stream test: playing back {} chunks for '{}'",
        buffer.len(),
        username
    );

    for (i, chunk) in buffer.into_iter().enumerate() {
        // Send the chunk
        if sender.send(Message::Binary(chunk.into())).await.is_err() {
            tracing::debug!("Stream test: send failed at chunk {} for '{}'", i, username);
            return;
        }

        // Wait 250ms between chunks (real-time pace)
        // Skip the sleep after the last chunk
        if i < MAX_BUFFER_CHUNKS {
            tokio::time::sleep(PLAYBACK_INTERVAL).await;
        }
    }

    // Signal playback done
    let _ = sender.send(Message::Text("playback-done".into())).await;

    tracing::info!("Stream test: playback complete for '{}'", username);
}
