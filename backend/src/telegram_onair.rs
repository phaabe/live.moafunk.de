//! The Telegram on-air message of an episode (ticket 1056): the bot posts it to
//! the Moafunk channel when the episode goes live (staging: the test group),
//! with the cover, an uploaded picture or no picture, silent or not, and pinned
//! while the episode is on air.
//!
//! Own table and module, apart from the Instagram announcements. A send claims
//! its row first (`send_claimed_at`), so a message goes out at most once, also
//! across a restart. Admins switch it off on the Config page.

use std::sync::Arc;

use axum::{
    extract::{Path, State},
    http::HeaderMap,
    response::IntoResponse,
    Json,
};
use chrono::{DateTime, Duration, Utc};
use serde::{Deserialize, Serialize};
use teloxide::prelude::*;
use teloxide::types::{InputFile, MessageId};

use crate::announcements::to_db_time;
use crate::handlers::announcements::episode_start;
use crate::handlers::api::require_show_editor;
use crate::{db, storage, telegram_notify, AppError, AppState, Result};

/// Telegram's limits: a text message, and the caption of a photo.
const TEXT_MAX: usize = 4096;
const CAPTION_MAX: usize = 1024;
/// A send takes seconds; an older claim means the process died while sending.
const STALE_CLAIM: Duration = Duration::minutes(15);
/// Still scheduled this long after the episode start: it never went live.
const MISSED: Duration = Duration::hours(12);

#[derive(Debug, Clone, Serialize, sqlx::FromRow)]
pub struct OnAirMessage {
    pub id: i64,
    pub show_id: i64,
    /// scheduled | sent | failed | cancelled
    pub state: String,
    pub text: String,
    /// cover | upload | none
    pub image_source: String,
    pub image_key: Option<String>,
    /// Sent without a notification sound.
    pub silent: bool,
    /// Pin it while the episode is on air.
    pub pin: bool,
    /// The episode start when it was saved.
    pub scheduled_at: Option<String>,
    pub sent_at: Option<String>,
    pub permalink: Option<String>,
    /// Pinned right now.
    pub pinned: bool,
    pub error: Option<String>,
    pub created_by: Option<i64>,
    pub created_at: String,
    pub updated_at: String,
    #[serde(skip)]
    pub send_claimed_at: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct OnAirInput {
    pub text: String,
    /// cover (default) | upload | none
    pub image_source: Option<String>,
    pub image_key: Option<String>,
    pub silent: Option<bool>,
    pub pin: Option<bool>,
}

/// Saving and sending need the bot, the channel and the admin switch.
async fn ready(state: &AppState) -> Result<(Bot, ChatId)> {
    let off = || AppError::ServiceUnavailable("Telegram is temporarily not available.".to_string());
    if !db::is_service_enabled(&state.db, db::TELEGRAM_ONAIR_ENABLED).await {
        return Err(off());
    }
    let bot = state.telegram_bot.clone().ok_or_else(off)?;
    let chat = state.config.telegram_announce_chat_id.ok_or_else(off)?;
    Ok((bot, ChatId(chat)))
}

/// Checked picture fields: (image_source, image_key).
fn picture_of(show_id: i64, input: &OnAirInput) -> Result<(&'static str, Option<String>)> {
    match input.image_source.as_deref().unwrap_or("cover") {
        "cover" => Ok(("cover", None)),
        "none" => Ok(("none", None)),
        "upload" => {
            let prefix = format!("shows/{show_id}/announcements/uploads/");
            match input.image_key.as_deref() {
                Some(key)
                    if key.len() > prefix.len()
                        && key.starts_with(&prefix)
                        && !key.contains("..") =>
                {
                    Ok(("upload", Some(key.to_string())))
                }
                _ => Err(AppError::BadRequest(format!(
                    "image_key must be an upload of this episode ({prefix}...)"
                ))),
            }
        }
        _ => Err(AppError::BadRequest(
            "image_source must be 'cover', 'upload' or 'none'".to_string(),
        )),
    }
}

/// Telegram counts UTF-16 code units; a picture's caption is shorter.
fn check_text(text: &str, with_picture: bool) -> Result<()> {
    if text.is_empty() {
        return Err(AppError::BadRequest("Text is required".to_string()));
    }
    let max = if with_picture { CAPTION_MAX } else { TEXT_MAX };
    if text.encode_utf16().count() > max {
        let with = if with_picture { " with a picture" } else { "" };
        return Err(AppError::BadRequest(format!(
            "Text is longer than {max} characters{with}"
        )));
    }
    Ok(())
}

/// A sent message, or one being sent right now, can't change.
fn refuse_if_sent(row: &OnAirMessage) -> Result<()> {
    if row.state == "sent" {
        return Err(AppError::Conflict(
            "The on-air message was already sent".to_string(),
        ));
    }
    if row.send_claimed_at.is_some() {
        return Err(AppError::Conflict(
            "The on-air message is being sent right now".to_string(),
        ));
    }
    Ok(())
}

async fn load(state: &AppState, show_id: i64) -> Result<Option<OnAirMessage>> {
    Ok(
        sqlx::query_as("SELECT * FROM telegram_onair_messages WHERE show_id = ?")
            .bind(show_id)
            .fetch_optional(&state.db)
            .await?,
    )
}

/// GET /api/shows/:id/telegram/onair — the episode's on-air message, or null.
pub async fn api_get_onair_message(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_show_editor(&state, &headers, id).await?;
    Ok(Json(load(&state, id).await?))
}

/// PUT /api/shows/:id/telegram/onair — create or edit (also after a cancel or
/// a failure). It goes out when the episode goes live. Refused once sent.
pub async fn api_put_onair_message(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    Json(input): Json<OnAirInput>,
) -> Result<impl IntoResponse> {
    let (user, show) = require_show_editor(&state, &headers, id).await?;
    let _ = ready(&state).await?;
    if let Some(row) = load(&state, id).await? {
        refuse_if_sent(&row)?;
    }
    let text = input.text.trim();
    let (image_source, image_key) = picture_of(id, &input)?;
    check_text(text, image_source != "none")?;
    let start = episode_start(&state, &show)
        .await?
        .ok_or_else(|| AppError::BadRequest("The episode has no start time".to_string()))?;

    // The WHERE keeps a send that claimed the row in between.
    let saved = sqlx::query(
        "INSERT INTO telegram_onair_messages \
           (show_id, state, text, image_source, image_key, silent, pin, scheduled_at, created_by) \
         VALUES (?, 'scheduled', ?, ?, ?, ?, ?, ?, ?) \
         ON CONFLICT (show_id) DO UPDATE SET state = 'scheduled', text = excluded.text, \
           image_source = excluded.image_source, image_key = excluded.image_key, \
           silent = excluded.silent, pin = excluded.pin, scheduled_at = excluded.scheduled_at, \
           created_by = excluded.created_by, sent_at = NULL, chat_id = NULL, message_id = NULL, \
           permalink = NULL, pinned = 0, error = NULL, updated_at = CURRENT_TIMESTAMP \
         WHERE telegram_onair_messages.state <> 'sent' \
           AND telegram_onair_messages.send_claimed_at IS NULL",
    )
    .bind(id)
    .bind(text)
    .bind(image_source)
    .bind(image_key)
    .bind(input.silent.unwrap_or(false))
    .bind(input.pin.unwrap_or(true))
    .bind(to_db_time(start))
    .bind(user.id)
    .execute(&state.db)
    .await?;
    let row = load(&state, id)
        .await?
        .ok_or_else(|| AppError::Internal("On-air message vanished".to_string()))?;
    if saved.rows_affected() == 0 {
        refuse_if_sent(&row)?;
    }
    Ok(Json(row))
}

/// DELETE /api/shows/:id/telegram/onair — cancel. Refused once sent.
pub async fn api_cancel_onair_message(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_show_editor(&state, &headers, id).await?;
    sqlx::query(
        "UPDATE telegram_onair_messages SET state = 'cancelled', updated_at = CURRENT_TIMESTAMP \
         WHERE show_id = ? AND state IN ('scheduled', 'failed') AND send_claimed_at IS NULL",
    )
    .bind(id)
    .execute(&state.db)
    .await?;
    let row = load(&state, id)
        .await?
        .ok_or_else(|| AppError::NotFound("This episode has no on-air message".to_string()))?;
    if row.state != "cancelled" {
        refuse_if_sent(&row)?;
    }
    Ok(Json(row))
}

/// The episode went live: send its on-air message.
pub async fn on_air(state: &Arc<AppState>, show_id: i64, now: DateTime<Utc>) {
    let Ok((bot, chat)) = ready(state).await else {
        return;
    };
    let claimed = sqlx::query(
        "UPDATE telegram_onair_messages SET send_claimed_at = ? \
         WHERE show_id = ? AND state = 'scheduled' AND send_claimed_at IS NULL",
    )
    .bind(to_db_time(now))
    .bind(show_id)
    .execute(&state.db)
    .await;
    match claimed {
        Ok(r) if r.rows_affected() == 1 => {}
        Ok(_) => return, // nothing scheduled, or the other hook has it
        Err(e) => {
            tracing::error!("On-air message: failed to claim show {show_id}: {e}");
            return;
        }
    }
    let row = match load(state, show_id).await {
        Ok(Some(row)) => row,
        Ok(None) => return,
        Err(e) => {
            tracing::error!("On-air message: failed to load show {show_id}: {e}");
            return;
        }
    };

    let stored = match send(state, &bot, chat, &row).await {
        Ok(sent) => {
            sqlx::query(
                "UPDATE telegram_onair_messages SET state = 'sent', sent_at = ?, chat_id = ?, \
                   message_id = ?, permalink = ?, pinned = ?, error = ?, send_claimed_at = NULL, \
                   updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            )
            .bind(to_db_time(now))
            .bind(chat.0)
            .bind(sent.message_id.0)
            .bind(sent.permalink)
            .bind(sent.pinned)
            .bind(sent.warning)
            .bind(row.id)
            .execute(&state.db)
            .await
        }
        Err(e) => {
            tracing::error!("On-air message of show {show_id} failed: {e}");
            sqlx::query(
                "UPDATE telegram_onair_messages SET state = 'failed', error = ?, \
                   send_claimed_at = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            )
            .bind(e.to_string())
            .bind(row.id)
            .execute(&state.db)
            .await
        }
    };
    match stored {
        Ok(_) => notify_result(state, show_id).await,
        // The claim stays; the tick marks it failed later.
        Err(e) => {
            tracing::error!("On-air message: failed to store the result of show {show_id}: {e}")
        }
    }
}

struct Sent {
    message_id: MessageId,
    permalink: Option<String>,
    pinned: bool,
    /// Sent, but not pinned.
    warning: Option<String>,
}

async fn send(state: &Arc<AppState>, bot: &Bot, chat: ChatId, row: &OnAirMessage) -> Result<Sent> {
    let refused =
        |e: teloxide::RequestError| AppError::Internal(format!("Telegram refused it: {e}"));
    let message = match picture(state, row).await? {
        Some(bytes) => bot
            .send_photo(chat, InputFile::memory(bytes).file_name("on-air.jpg"))
            .caption(row.text.clone())
            .disable_notification(row.silent)
            .await
            .map_err(refused)?,
        None => bot
            .send_message(chat, row.text.clone())
            .disable_notification(row.silent)
            .await
            .map_err(refused)?,
    };
    let mut warning = None;
    let pinned = row.pin
        && match bot
            .pin_chat_message(chat, message.id)
            .disable_notification(true)
            .await
        {
            Ok(_) => true,
            Err(e) => {
                warning = Some(format!("Sent, but not pinned: {e}"));
                false
            }
        };
    Ok(Sent {
        message_id: message.id,
        permalink: message.url().map(|u| u.to_string()),
        pinned,
        warning,
    })
}

/// The picture's bytes: the upload or the episode cover; none for 'none'.
/// An episode without a cover sends the text alone.
async fn picture(state: &Arc<AppState>, row: &OnAirMessage) -> Result<Option<Vec<u8>>> {
    let key = match (row.image_source.as_str(), &row.image_key) {
        ("none", _) => return Ok(None),
        ("upload", Some(key)) => key.clone(),
        _ => format!("shows/{}/cover.png", row.show_id),
    };
    match storage::download_file(state, &key).await {
        Ok((data, _)) => Ok(Some(data)),
        Err(e) if row.image_source == "cover" => {
            tracing::warn!("On-air message: no cover for show {} ({e})", row.show_id);
            Ok(None)
        }
        Err(e) => Err(e),
    }
}

/// The scheduler tick (every 60 s): send the message of the live episode (in
/// case the go-live hook missed it), cancel missed ones, fail interrupted
/// sends and unpin messages whose episode is off air.
pub async fn tick(state: &Arc<AppState>, now: DateTime<Utc>) {
    if let Some(show_id) = crate::handlers::series::on_air_show_id(state).await {
        on_air(state, show_id, now).await;
    }
    if let Err(e) = cancel_missed(state, now).await {
        tracing::error!("On-air message: missed check failed: {e}");
    }
    if let Err(e) = fail_interrupted(state, now).await {
        tracing::error!("On-air message: stale claim check failed: {e}");
    }
    if let Err(e) = unpin_off_air(state).await {
        tracing::error!("On-air message: unpin check failed: {e}");
    }
}

async fn cancel_missed(state: &Arc<AppState>, now: DateTime<Utc>) -> Result<()> {
    sqlx::query(
        "UPDATE telegram_onair_messages SET state = 'cancelled', \
           error = 'The episode did not go live, so the message was not sent', \
           updated_at = CURRENT_TIMESTAMP \
         WHERE state = 'scheduled' AND send_claimed_at IS NULL AND scheduled_at < ?",
    )
    .bind(to_db_time(now - MISSED))
    .execute(&state.db)
    .await?;
    Ok(())
}

/// A claim this old belongs to a send that a crash cut off. It may have
/// posted, so it is never sent again on its own.
async fn fail_interrupted(state: &Arc<AppState>, now: DateTime<Utc>) -> Result<()> {
    let stale: Vec<i64> = sqlx::query_scalar(
        "SELECT show_id FROM telegram_onair_messages \
         WHERE send_claimed_at IS NOT NULL AND send_claimed_at < ?",
    )
    .bind(to_db_time(now - STALE_CLAIM))
    .fetch_all(&state.db)
    .await?;
    for show_id in stale {
        sqlx::query(
            "UPDATE telegram_onair_messages SET state = 'failed', send_claimed_at = NULL, \
               error = 'The send was interrupted (restart?). Check the channel before you retry.', \
               updated_at = CURRENT_TIMESTAMP WHERE show_id = ?",
        )
        .bind(show_id)
        .execute(&state.db)
        .await?;
        notify_result(state, show_id).await;
    }
    Ok(())
}

/// Unpin every pinned message whose episode is off air. A pre-recorded stream
/// does not name its episode, so while one runs nothing is unpinned.
async fn unpin_off_air(state: &Arc<AppState>) -> Result<()> {
    let Some(bot) = state.telegram_bot.clone() else {
        return Ok(());
    };
    let live = crate::handlers::series::on_air_show_id(state).await;
    let unnamed_stream = {
        let stream = state.stream_state.lock().await;
        stream.is_active() && stream.on_air_show_id().is_none()
    };
    if unnamed_stream {
        return Ok(());
    }
    let pinned: Vec<(i64, i64, i64)> = sqlx::query_as(
        "SELECT show_id, chat_id, message_id FROM telegram_onair_messages \
         WHERE pinned = 1 AND chat_id IS NOT NULL AND message_id IS NOT NULL",
    )
    .fetch_all(&state.db)
    .await?;
    for (show_id, chat, message) in pinned {
        if live == Some(show_id) {
            continue;
        }
        let Ok(message) = i32::try_from(message) else {
            continue;
        };
        if let Err(e) = bot
            .unpin_chat_message(ChatId(chat))
            .message_id(MessageId(message))
            .await
        {
            // No retry every minute: an admin unpins it by hand.
            let title = show_title(state, show_id).await;
            telegram_notify::notify(
                state,
                &format!("⚠️ Telegram on-air message of {title} is still pinned: {e}"),
            )
            .await;
        }
        sqlx::query(
            "UPDATE telegram_onair_messages SET pinned = 0, updated_at = CURRENT_TIMESTAMP \
             WHERE show_id = ?",
        )
        .bind(show_id)
        .execute(&state.db)
        .await?;
    }
    Ok(())
}

async fn show_title(state: &AppState, show_id: i64) -> String {
    sqlx::query_scalar("SELECT title FROM shows WHERE id = ?")
        .bind(show_id)
        .fetch_optional(&state.db)
        .await
        .ok()
        .flatten()
        .unwrap_or_default()
}

/// Technical alert for a sent or failed message.
async fn notify_result(state: &Arc<AppState>, show_id: i64) {
    let Ok(Some(row)) = load(state, show_id).await else {
        return;
    };
    let title = show_title(state, show_id).await;
    let msg = match row.state.as_str() {
        "sent" => {
            let mut msg = format!(
                "📣 Telegram on-air message sent: {title}\n{}",
                row.permalink.as_deref().unwrap_or("(no link)")
            );
            if let Some(warning) = &row.error {
                msg.push_str(&format!("\n⚠️ {warning}"));
            }
            msg
        }
        "failed" => format!(
            "⚠️ Telegram on-air message failed: {title}\n{}",
            row.error.as_deref().unwrap_or_default()
        ),
        _ => return,
    };
    telegram_notify::notify(state, &msg).await;
}

#[cfg(test)]
#[path = "telegram_onair_tests.rs"]
mod tests;
