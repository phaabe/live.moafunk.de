//! WebSocket handler for the live-chat bridge (#278).
//!
//! Fans the Telegram discussion group's messages out to the panel and posts
//! host replies back through the bot. Frames are JSON with a `type` field:
//!
//! - server → panel: `{"type":"history","messages":[ChatMessage…]}` on
//!   connect, then `{"type":"message","message":ChatMessage}` per message,
//!   `{"type":"error","error":"…"}` when a reply can't be delivered.
//! - panel → server: a plain text frame is a host reply.

use crate::auth::get_current_user;
use crate::chat_bridge::ChatMessage;
use crate::{AppState, Result};
use axum::extract::ws::{Message, WebSocket};
use axum::{
    extract::{State, WebSocketUpgrade},
    http::StatusCode,
    response::{IntoResponse, Response},
};
use futures_util::{SinkExt, StreamExt};
use std::sync::Arc;
use teloxide::prelude::*;

/// Longest accepted host reply — matches Telegram's own message limit.
const MAX_REPLY_CHARS: usize = 4096;

pub async fn chat_ws_handler(
    ws: WebSocketUpgrade,
    State(state): State<Arc<AppState>>,
    headers: axum::http::HeaderMap,
) -> Result<Response> {
    // Session-cookie auth, same pattern as stream_ws.
    let token = headers
        .get(axum::http::header::COOKIE)
        .and_then(|v| v.to_str().ok())
        .and_then(|cookies| {
            cookies.split(';').find_map(|cookie| {
                cookie
                    .trim()
                    .strip_prefix("session=")
                    .map(|token| token.to_string())
            })
        });

    let user = match get_current_user(&state, token.as_deref()).await {
        Some(u) => u,
        None => {
            return Ok((StatusCode::UNAUTHORIZED, "Not authenticated").into_response());
        }
    };

    let username = user.username.clone();
    Ok(ws.on_upgrade(move |socket| handle_chat_socket(socket, state, username)))
}

async fn handle_chat_socket(socket: WebSocket, state: Arc<AppState>, username: String) {
    let (mut sender, mut receiver) = socket.split();

    // Subscribe BEFORE snapshotting history — the other order drops any
    // message published in between (in neither snapshot nor subscription).
    // A message caught by both is deduped client-side by id.
    let mut rx = state.chat_hub.subscribe();

    // History first, so a panel connecting mid-show has context.
    let history = state.chat_hub.recent().await;
    let frame = serde_json::json!({ "type": "history", "messages": history });
    if sender.send(Message::Text(frame.to_string())).await.is_err() {
        return;
    }

    loop {
        tokio::select! {
            broadcast = rx.recv() => {
                match broadcast {
                    Ok(msg) => {
                        let frame = serde_json::json!({ "type": "message", "message": msg });
                        if sender.send(Message::Text(frame.to_string())).await.is_err() {
                            break;
                        }
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(skipped)) => {
                        // Self-heal: a fresh history frame replaces the panel's
                        // list, closing the gap the lag just created.
                        tracing::warn!("Chat panel for '{}' lagged, skipped {} messages", username, skipped);
                        let history = state.chat_hub.recent().await;
                        let frame = serde_json::json!({ "type": "history", "messages": history });
                        if sender.send(Message::Text(frame.to_string())).await.is_err() {
                            break;
                        }
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
                }
            }
            incoming = receiver.next() => {
                match incoming {
                    Some(Ok(Message::Text(text))) => {
                        if let Some(err) = send_host_reply(&state, &username, text.trim()).await {
                            let frame = serde_json::json!({ "type": "error", "error": err });
                            if sender.send(Message::Text(frame.to_string())).await.is_err() {
                                break;
                            }
                        }
                    }
                    Some(Ok(Message::Close(_))) | None => break,
                    Some(Ok(_)) => {}
                    Some(Err(e)) => {
                        tracing::debug!("Chat WS error for '{}': {}", username, e);
                        break;
                    }
                }
            }
        }
    }
}

/// Build the group message for a host reply, validating the COMPOSED length
/// against Telegram's limit — the prefix counts too. Pure — unit tested.
/// `Ok(None)` = nothing to send (empty reply), `Err` = user-facing rejection.
fn compose_host_reply(username: &str, text: &str) -> std::result::Result<Option<String>, String> {
    if text.is_empty() {
        return Ok(None);
    }
    let composed = format!("🎙 {username}: {text}");
    if composed.chars().count() > MAX_REPLY_CHARS {
        let budget =
            MAX_REPLY_CHARS.saturating_sub(composed.chars().count() - text.chars().count());
        return Err(format!("Reply too long (max {budget} characters)"));
    }
    Ok(Some(composed))
}

/// Post a host reply to the Telegram group and echo it into the hub.
/// Returns a user-facing error string on failure, None on success or no-op.
async fn send_host_reply(state: &Arc<AppState>, username: &str, text: &str) -> Option<String> {
    let composed = match compose_host_reply(username, text) {
        Ok(Some(c)) => c,
        Ok(None) => return None,
        Err(e) => return Some(e),
    };

    let bot = match &state.telegram_bot {
        Some(b) => b.clone(),
        None => return Some("Telegram bot is not configured on this server".to_string()),
    };
    let chat_id = match state.config.telegram_live_chat_id {
        Some(id) => id,
        None => {
            return Some("Live chat group is not configured (TELEGRAM_LIVE_CHAT_ID)".to_string())
        }
    };

    let sent = bot.send_message(ChatId(chat_id), composed).await;

    match sent {
        Ok(msg) => {
            // Telegram never delivers a bot its own messages, so echo it into
            // the hub ourselves — every panel (incl. the sender) sees it.
            state
                .chat_hub
                .publish(ChatMessage {
                    id: msg.id.0 as i64,
                    author: username.to_string(),
                    text: text.to_string(),
                    ts: chrono::Utc::now().timestamp(),
                    host: true,
                })
                .await;
            None
        }
        Err(e) => {
            tracing::error!("Failed to post host reply to Telegram: {}", e);
            Some("Could not deliver the reply to Telegram".to_string())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compose_prefixes_and_passes_normal_replies() {
        assert_eq!(
            compose_host_reply("anton", "hello chat"),
            Ok(Some("🎙 anton: hello chat".to_string()))
        );
    }

    #[test]
    fn compose_skips_empty_replies() {
        assert_eq!(compose_host_reply("anton", ""), Ok(None));
    }

    #[test]
    fn compose_rejects_when_prefix_pushes_past_the_limit() {
        // Text alone fits exactly; prefix must tip it over.
        let text = "x".repeat(MAX_REPLY_CHARS);
        let result = compose_host_reply("anton", &text);
        assert!(result.is_err());
        // The reported budget accounts for the prefix.
        let budget = MAX_REPLY_CHARS - "🎙 anton: ".chars().count();
        assert!(result.unwrap_err().contains(&format!("{budget}")));
    }

    #[test]
    fn compose_accepts_text_at_the_composed_limit() {
        let prefix_chars = "🎙 anton: ".chars().count();
        let text = "x".repeat(MAX_REPLY_CHARS - prefix_chars);
        assert!(compose_host_reply("anton", &text).is_ok());
    }
}
