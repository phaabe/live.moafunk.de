use std::sync::Arc;

use teloxide::prelude::*;
use teloxide::types::{MessageId, ThreadId};

use crate::AppState;

/// Send a text message to the configured admin Telegram chat.
///
/// No-op if `telegram_bot` or `telegram_admin_chat_id` is not configured.
/// Errors are logged but never propagated — notifications must not break main flows.
pub async fn notify(state: &Arc<AppState>, message: &str) {
    let (Some(bot), Some(chat_id)) = (&state.telegram_bot, state.config.telegram_admin_chat_id)
    else {
        return;
    };

    let chat = ChatId(chat_id);
    let mut req = bot.send_message(chat, message);
    if let Some(tid) = state.config.telegram_topic_id {
        req = req.message_thread_id(ThreadId(MessageId(tid)));
    }
    if let Err(e) = req.await {
        tracing::warn!("Telegram notification failed: {e}");
    }
}

/// Notify admin about a new artist submission.
///
/// Spawns a detached tokio task so the caller is never blocked.
pub fn notify_artist_submission(state: &Arc<AppState>, artist_id: i64, artist_name: &str) {
    let state = state.clone();
    let name = artist_name.to_owned();
    tokio::spawn(async move {
        notify(
            &state,
            &format!("🎤 New artist submitted: {name} (ID: {artist_id})"),
        )
        .await;
    });
}

/// Notify admin that a live stream has started.
///
/// Spawns a detached tokio task so the caller is never blocked.
pub fn notify_stream_start(state: &Arc<AppState>, username: &str) {
    let state = state.clone();
    let user = username.to_owned();
    tokio::spawn(async move {
        notify(&state, &format!("📡 Stream started by {user}")).await;
    });
}

/// Notify admin that a live stream has ended.
///
/// Spawns a detached tokio task so the caller is never blocked.
pub fn notify_stream_stop(state: &Arc<AppState>, username: &str) {
    let state = state.clone();
    let user = username.to_owned();
    tokio::spawn(async move {
        notify(&state, &format!("📡 Stream ended ({user})")).await;
    });
}
