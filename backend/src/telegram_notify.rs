use std::sync::Arc;

use teloxide::prelude::*;
use teloxide::types::{MessageId, ParseMode, ThreadId};

use crate::{instagram, models, storage, AppState};

/// HTML-escape user-provided text so it doesn't break Telegram's HTML parser.
fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

/// Download a file from a presigned URL into memory.
async fn download_file_bytes(presigned_url: &str) -> Result<Vec<u8>, String> {
    reqwest::get(presigned_url)
        .await
        .map_err(|e| format!("HTTP download failed: {e}"))?
        .error_for_status()
        .map_err(|e| format!("HTTP status error: {e}"))?
        .bytes()
        .await
        .map_err(|e| format!("Failed to read bytes: {e}"))
        .map(|b| b.to_vec())
}

// ---------------------------------------------------------------------------
// Raw Telegram API helpers (bypass teloxide multipart serializer)
// ---------------------------------------------------------------------------
//
// teloxide-core 0.10.x's multipart `PartSerializer` does not implement
// `serialize_newtype_struct`, so any request that carries an `InputFile`
// (which forces multipart encoding) **and** a `ThreadId` (a newtype) will
// panic with "not implemented".  Text-only requests (`send_message`) use
// JSON and are fine — only `send_photo` / `send_audio` are affected.
//
// The helpers below call the Telegram Bot API directly via reqwest multipart
// to work around this limitation.

/// Response envelope from the Telegram Bot API.
#[derive(serde::Deserialize)]
struct TgResponse {
    ok: bool,
    description: Option<String>,
    result: Option<serde_json::Value>,
}

/// Send a photo via the Telegram Bot API using raw reqwest multipart.
///
/// Returns the `message_id` of the sent message on success.
async fn send_photo_raw(
    token: &str,
    chat_id: i64,
    photo_bytes: Vec<u8>,
    filename: String,
    caption: &str,
    parse_mode: &str,
    thread_id: Option<i32>,
    reply_markup: Option<&str>,
) -> Result<i64, String> {
    let photo_part = reqwest::multipart::Part::bytes(photo_bytes)
        .file_name(filename)
        .mime_str("image/png")
        .map_err(|e| format!("mime error: {e}"))?;

    let mut form = reqwest::multipart::Form::new()
        .text("chat_id", chat_id.to_string())
        .text("caption", caption.to_owned())
        .text("parse_mode", parse_mode.to_owned())
        .part("photo", photo_part);

    if let Some(tid) = thread_id {
        form = form.text("message_thread_id", tid.to_string());
    }

    if let Some(markup) = reply_markup {
        form = form.text("reply_markup", markup.to_owned());
    }

    let url = format!("https://api.telegram.org/bot{token}/sendPhoto");
    let resp: TgResponse = reqwest::Client::new()
        .post(&url)
        .multipart(form)
        .send()
        .await
        .map_err(|e| format!("sendPhoto request failed: {e}"))?
        .json()
        .await
        .map_err(|e| format!("sendPhoto response parse failed: {e}"))?;

    if !resp.ok {
        return Err(format!(
            "sendPhoto API error: {}",
            resp.description.unwrap_or_default()
        ));
    }
    let msg_id = resp
        .result
        .and_then(|v| v.get("message_id").and_then(|m| m.as_i64()))
        .unwrap_or(0);
    Ok(msg_id)
}

/// Send a video via the Telegram Bot API using raw reqwest multipart.
///
/// Returns the `message_id` of the sent message on success.
pub async fn send_video_raw(
    token: &str,
    chat_id: i64,
    video_bytes: Vec<u8>,
    filename: String,
    caption: &str,
    parse_mode: &str,
    thread_id: Option<i32>,
    reply_markup: Option<&str>,
) -> Result<i64, String> {
    let video_part = reqwest::multipart::Part::bytes(video_bytes)
        .file_name(filename)
        .mime_str("video/mp4")
        .map_err(|e| format!("mime error: {e}"))?;

    let mut form = reqwest::multipart::Form::new()
        .text("chat_id", chat_id.to_string())
        .text("caption", caption.to_owned())
        .text("parse_mode", parse_mode.to_owned())
        .part("video", video_part);

    if let Some(tid) = thread_id {
        form = form.text("message_thread_id", tid.to_string());
    }

    if let Some(markup) = reply_markup {
        form = form.text("reply_markup", markup.to_owned());
    }

    let url = format!("https://api.telegram.org/bot{token}/sendVideo");
    let resp: TgResponse = reqwest::Client::new()
        .post(&url)
        .multipart(form)
        .send()
        .await
        .map_err(|e| format!("sendVideo request failed: {e}"))?
        .json()
        .await
        .map_err(|e| format!("sendVideo response parse failed: {e}"))?;

    if !resp.ok {
        return Err(format!(
            "sendVideo API error: {}",
            resp.description.unwrap_or_default()
        ));
    }
    let msg_id = resp
        .result
        .and_then(|v| v.get("message_id").and_then(|m| m.as_i64()))
        .unwrap_or(0);
    Ok(msg_id)
}

/// Send an audio file via the Telegram Bot API using raw reqwest multipart.
async fn send_audio_raw(
    token: &str,
    chat_id: i64,
    audio_bytes: Vec<u8>,
    filename: String,
    title: &str,
    performer: &str,
    thread_id: Option<i32>,
    reply_to_message_id: Option<i32>,
) -> Result<(), String> {
    let audio_part = reqwest::multipart::Part::bytes(audio_bytes)
        .file_name(filename)
        .mime_str("audio/mpeg")
        .map_err(|e| format!("mime error: {e}"))?;

    let mut form = reqwest::multipart::Form::new()
        .text("chat_id", chat_id.to_string())
        .text("title", title.to_owned())
        .text("performer", performer.to_owned())
        .part("audio", audio_part);

    if let Some(tid) = thread_id {
        form = form.text("message_thread_id", tid.to_string());
    }
    if let Some(rid) = reply_to_message_id {
        // Telegram expects reply_parameters as JSON
        form = form.text(
            "reply_parameters",
            serde_json::json!({ "message_id": rid }).to_string(),
        );
    }

    let url = format!("https://api.telegram.org/bot{token}/sendAudio");
    let resp: TgResponse = reqwest::Client::new()
        .post(&url)
        .multipart(form)
        .send()
        .await
        .map_err(|e| format!("sendAudio request failed: {e}"))?
        .json()
        .await
        .map_err(|e| format!("sendAudio response parse failed: {e}"))?;

    if !resp.ok {
        return Err(format!(
            "sendAudio API error: {}",
            resp.description.unwrap_or_default()
        ));
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Raw Telegram edit helpers
// ---------------------------------------------------------------------------

/// Edit the caption of an existing message.
pub async fn edit_message_caption_raw(
    token: &str,
    chat_id: i64,
    message_id: i64,
    caption: &str,
    parse_mode: &str,
    reply_markup: Option<&str>,
) -> Result<(), String> {
    let mut body = serde_json::json!({
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": caption,
        "parse_mode": parse_mode,
    });

    if let Some(markup) = reply_markup {
        let markup_val: serde_json::Value =
            serde_json::from_str(markup).map_err(|e| format!("reply_markup JSON parse: {e}"))?;
        body["reply_markup"] = markup_val;
    }

    let url = format!("https://api.telegram.org/bot{token}/editMessageCaption");
    let resp: TgResponse = reqwest::Client::new()
        .post(&url)
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("editMessageCaption request failed: {e}"))?
        .json()
        .await
        .map_err(|e| format!("editMessageCaption response parse failed: {e}"))?;

    if !resp.ok {
        return Err(format!(
            "editMessageCaption API error: {}",
            resp.description.unwrap_or_default()
        ));
    }
    Ok(())
}

/// Edit the media (photo or video) of an existing message.
///
/// `media_type` should be `"photo"` or `"video"`.
pub async fn edit_message_media_raw(
    token: &str,
    chat_id: i64,
    message_id: i64,
    media_bytes: Vec<u8>,
    filename: String,
    media_type: &str,
    caption: &str,
    parse_mode: &str,
    reply_markup: Option<&str>,
) -> Result<(), String> {
    let mime = match media_type {
        "video" => "video/mp4",
        _ => "image/png",
    };

    let file_part = reqwest::multipart::Part::bytes(media_bytes)
        .file_name(filename)
        .mime_str(mime)
        .map_err(|e| format!("mime error: {e}"))?;

    let media_json = serde_json::json!({
        "type": media_type,
        "media": "attach://file",
        "caption": caption,
        "parse_mode": parse_mode,
    });

    let mut form = reqwest::multipart::Form::new()
        .text("chat_id", chat_id.to_string())
        .text("message_id", message_id.to_string())
        .text("media", media_json.to_string())
        .part("file", file_part);

    if let Some(markup) = reply_markup {
        form = form.text("reply_markup", markup.to_owned());
    }

    let url = format!("https://api.telegram.org/bot{token}/editMessageMedia");
    let resp: TgResponse = reqwest::Client::new()
        .post(&url)
        .multipart(form)
        .send()
        .await
        .map_err(|e| format!("editMessageMedia request failed: {e}"))?
        .json()
        .await
        .map_err(|e| format!("editMessageMedia response parse failed: {e}"))?;

    if !resp.ok {
        return Err(format!(
            "editMessageMedia API error: {}",
            resp.description.unwrap_or_default()
        ));
    }
    Ok(())
}

/// Edit (or remove) the reply markup (inline keyboard) of an existing message.
///
/// Pass `None` to remove all buttons.
pub async fn edit_message_reply_markup_raw(
    token: &str,
    chat_id: i64,
    message_id: i64,
    reply_markup: Option<&str>,
) -> Result<(), String> {
    let mut body = serde_json::json!({
        "chat_id": chat_id,
        "message_id": message_id,
    });

    if let Some(markup) = reply_markup {
        let markup_val: serde_json::Value =
            serde_json::from_str(markup).map_err(|e| format!("reply_markup JSON parse: {e}"))?;
        body["reply_markup"] = markup_val;
    }

    let url = format!("https://api.telegram.org/bot{token}/editMessageReplyMarkup");
    let resp: TgResponse = reqwest::Client::new()
        .post(&url)
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("editMessageReplyMarkup request failed: {e}"))?
        .json()
        .await
        .map_err(|e| format!("editMessageReplyMarkup response parse failed: {e}"))?;

    if !resp.ok {
        return Err(format!(
            "editMessageReplyMarkup API error: {}",
            resp.description.unwrap_or_default()
        ));
    }
    Ok(())
}

/// Send a text message via the Telegram Bot API using raw JSON POST.
///
/// Returns the sent message ID on success.
pub async fn send_message_raw(
    token: &str,
    chat_id: i64,
    text: &str,
    thread_id: Option<i32>,
    reply_markup: Option<&str>,
) -> Result<i64, String> {
    let mut body = serde_json::json!({
        "chat_id": chat_id,
        "text": text,
    });

    if let Some(tid) = thread_id {
        body["message_thread_id"] = serde_json::json!(tid);
    }

    if let Some(markup) = reply_markup {
        let markup_val: serde_json::Value =
            serde_json::from_str(markup).map_err(|e| format!("reply_markup JSON parse: {e}"))?;
        body["reply_markup"] = markup_val;
    }

    let url = format!("https://api.telegram.org/bot{token}/sendMessage");
    let resp: TgResponse = reqwest::Client::new()
        .post(&url)
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("sendMessage request failed: {e}"))?
        .json()
        .await
        .map_err(|e| format!("sendMessage response parse failed: {e}"))?;

    if !resp.ok {
        return Err(format!(
            "sendMessage API error: {}",
            resp.description.unwrap_or_default()
        ));
    }

    let msg_id = resp
        .result
        .and_then(|r| r.get("message_id").and_then(|v| v.as_i64()))
        .ok_or("sendMessage: missing message_id in response")?;
    Ok(msg_id)
}

/// Edit the text and/or reply markup of an existing text message.
pub async fn edit_message_text_raw(
    token: &str,
    chat_id: i64,
    message_id: i64,
    text: &str,
    reply_markup: Option<&str>,
) -> Result<(), String> {
    let mut body = serde_json::json!({
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    });

    if let Some(markup) = reply_markup {
        let markup_val: serde_json::Value =
            serde_json::from_str(markup).map_err(|e| format!("reply_markup JSON parse: {e}"))?;
        body["reply_markup"] = markup_val;
    }

    let url = format!("https://api.telegram.org/bot{token}/editMessageText");
    let resp: TgResponse = reqwest::Client::new()
        .post(&url)
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("editMessageText request failed: {e}"))?
        .json()
        .await
        .map_err(|e| format!("editMessageText response parse failed: {e}"))?;

    if !resp.ok {
        return Err(format!(
            "editMessageText API error: {}",
            resp.description.unwrap_or_default()
        ));
    }
    Ok(())
}

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

/// Send an HTML-formatted message to the configured admin Telegram chat.
///
/// Like [`notify`], but sets `ParseMode::Html` so the message can contain
/// clickable `<a href="…">` links and other HTML formatting.
///
/// No-op if `telegram_bot` or `telegram_admin_chat_id` is not configured.
/// Errors are logged but never propagated.
pub async fn notify_html(state: &Arc<AppState>, message: &str) {
    let (Some(bot), Some(chat_id)) = (&state.telegram_bot, state.config.telegram_admin_chat_id)
    else {
        return;
    };

    let chat = ChatId(chat_id);
    let mut req = bot.send_message(chat, message).parse_mode(ParseMode::Html);
    if let Some(tid) = state.config.telegram_topic_id {
        req = req.message_thread_id(ThreadId(MessageId(tid)));
    }
    if let Err(e) = req.await {
        tracing::warn!("Telegram notification failed: {e}");
    }
}

// ---------------------------------------------------------------------------
// Rich artist preview
// ---------------------------------------------------------------------------

/// Build the HTML caption for the artist photo message.
///
/// Includes all artist-provided text (not AI-generated fields).
/// Truncates to 1024 chars (Telegram photo caption limit).
fn build_artist_caption(artist: &models::Artist, profile_url: &str) -> String {
    let mut parts: Vec<String> = Vec::new();

    // Header: name + pronouns
    parts.push(format!(
        "🎤 <b>New artist submitted: {} (ID: {})</b>",
        html_escape(&artist.name),
        artist.id
    ));

    // Track names
    parts.push(format!(
        "🎵 <b>Track 1:</b> {}\n🎵 <b>Track 2:</b> {}",
        html_escape(&artist.track1_name),
        html_escape(&artist.track2_name)
    ));

    // Music description
    if let Some(ref desc) = artist.music_description {
        if !desc.is_empty() {
            parts.push(format!("📝 {}", html_escape(desc)));
        }
    }

    // Socials block
    let mut socials = Vec::new();
    if let Some(ref v) = artist.instagram {
        if !v.is_empty() {
            socials.push(format!("<b>IG:</b> {}", html_escape(v)));
        }
    }
    if let Some(ref v) = artist.soundcloud {
        if !v.is_empty() {
            socials.push(format!("<b>SC:</b> {}", html_escape(v)));
        }
    }
    if let Some(ref v) = artist.bandcamp {
        if !v.is_empty() {
            socials.push(format!("<b>BC:</b> {}", html_escape(v)));
        }
    }
    if let Some(ref v) = artist.spotify {
        if !v.is_empty() {
            socials.push(format!("<b>Spotify:</b> {}", html_escape(v)));
        }
    }
    if let Some(ref v) = artist.other_social {
        if !v.is_empty() {
            socials.push(format!("<b>Other:</b> {}", html_escape(v)));
        }
    }
    if !socials.is_empty() {
        parts.push(socials.join(" | "));
    }

    // Mentions
    if let Some(ref v) = artist.mentions {
        if !v.is_empty() {
            parts.push(format!("💬 <b>Mentions:</b> {}", html_escape(v)));
        }
    }

    // Upcoming events
    if let Some(ref v) = artist.upcoming_events {
        if !v.is_empty() {
            parts.push(format!("📅 <b>Events:</b> {}", html_escape(v)));
        }
    }

    // Profile link (always last)
    parts.push(format!("<a href=\"{profile_url}\">Open artist profile</a>"));

    let caption = parts.join("\n\n");

    // Telegram photo caption limit is 1024 characters.
    // If we exceed it, truncate and keep the profile link.
    if caption.len() <= 1024 {
        return caption;
    }

    // Reserve space for the link line + ellipsis separator
    let link_line = format!("<a href=\"{profile_url}\">Open artist profile</a>");
    let budget = 1024 - link_line.len() - 4; // 4 = "\n\n…\n"  (join + ellipsis)
    let truncated: String = caption.chars().take(budget).collect();
    format!("{truncated}…\n\n{link_line}")
}

/// Send a rich artist preview to the admin Telegram chat.
///
/// 1. Photo message — artist image with full HTML caption (text + profile link)
/// 2. Audio messages — voice mail (if present), track 1, track 2
///    (each as a reply to the photo message for grouping)
///
/// Returns `Err` if any critical step fails (DB query, presigned URL for photo).
/// Audio failures are logged but don't cause the function to fail.
async fn send_artist_preview(state: &Arc<AppState>, artist_id: i64) -> Result<(), String> {
    let (Some(bot), Some(chat_id)) = (&state.telegram_bot, state.config.telegram_admin_chat_id)
    else {
        return Ok(()); // Not configured — silently skip
    };

    // Fetch artist from DB
    let artist: models::Artist =
        sqlx::query_as::<_, models::Artist>("SELECT * FROM artists WHERE id = ?")
            .bind(artist_id)
            .fetch_optional(&state.db)
            .await
            .map_err(|e| format!("DB query failed: {e}"))?
            .ok_or_else(|| format!("Artist {artist_id} not found"))?;

    // Build profile URL and caption
    let base = state.config.admin_base_url.trim_end_matches('/');
    let profile_url = format!("{base}/#/artists/{artist_id}");
    let caption = build_artist_caption(&artist, &profile_url);

    let chat = ChatId(chat_id);

    // --- Photo message ---
    let token = state
        .config
        .telegram_bot_token
        .as_deref()
        .unwrap_or_default();
    let photo_msg_id = if let Some(ref pic_key) = artist
        .pic_overlay_key
        .as_ref()
        .or(artist.pic_cropped_key.as_ref())
        .or(artist.pic_key.as_ref())
    {
        let url_str = storage::get_presigned_url(state, pic_key, 3600)
            .await
            .map_err(|e| format!("Presigned URL for photo failed: {e}"))?;

        let filename = pic_key.rsplit('/').next().unwrap_or("photo.jpg").to_owned();
        let photo_bytes = download_file_bytes(&url_str).await?;

        match send_photo_raw(
            token,
            chat_id,
            photo_bytes,
            filename,
            &caption,
            "HTML",
            state.config.telegram_topic_id,
            None,
        )
        .await
        {
            Ok(msg_id) => Some(MessageId(msg_id as i32)),
            Err(e) => {
                tracing::warn!("Failed to send artist photo: {e}");
                return Err(format!("send_photo failed: {e}"));
            }
        }
    } else {
        // No image — send text-only HTML message
        let mut req = bot.send_message(chat, &caption).parse_mode(ParseMode::Html);
        if let Some(tid) = state.config.telegram_topic_id {
            req = req.message_thread_id(ThreadId(MessageId(tid)));
        }
        match req.await {
            Ok(msg) => Some(msg.id),
            Err(e) => {
                tracing::warn!("Failed to send artist caption text: {e}");
                return Err(format!("send_message failed: {e}"));
            }
        }
    };

    // Helper closure to send an audio file as a reply to the photo message
    let send_audio_fn = |token: String,
                         chat_id: i64,
                         key: String,
                         title: String,
                         performer: String,
                         reply_to: Option<MessageId>,
                         topic_id: Option<i32>,
                         state: Arc<AppState>| async move {
        let url_str = match storage::get_presigned_url(&state, &key, 3600).await {
            Ok(u) => u,
            Err(e) => {
                tracing::warn!("Presigned URL for audio {key} failed: {e}");
                return;
            }
        };

        let filename = key.rsplit('/').next().unwrap_or("audio.mp3").to_owned();
        let audio_bytes = match download_file_bytes(&url_str).await {
            Ok(b) => b,
            Err(e) => {
                tracing::warn!("Download audio {key} failed: {e}");
                return;
            }
        };

        if let Err(e) = send_audio_raw(
            &token,
            chat_id,
            audio_bytes,
            filename,
            &title,
            &performer,
            topic_id,
            reply_to.map(|m| m.0),
        )
        .await
        {
            tracing::warn!("Failed to send audio {key}: {e}");
        }
    };

    // --- Audio messages (fire-and-forget style, but awaited sequentially) ---

    // Voice mail
    if let Some(ref voice_key) = artist.voice_message_key {
        send_audio_fn(
            token.to_owned(),
            chat_id,
            voice_key.clone(),
            format!("{} – voice message", artist.name),
            artist.name.clone(),
            photo_msg_id,
            state.config.telegram_topic_id,
            state.clone(),
        )
        .await;
    }

    // Track 1
    if let Some(ref key) = artist.track1_key {
        send_audio_fn(
            token.to_owned(),
            chat_id,
            key.clone(),
            artist.track1_name.clone(),
            artist.name.clone(),
            photo_msg_id,
            state.config.telegram_topic_id,
            state.clone(),
        )
        .await;
    }

    // Track 2
    if let Some(ref key) = artist.track2_key {
        send_audio_fn(
            token.to_owned(),
            chat_id,
            key.clone(),
            artist.track2_name.clone(),
            artist.name.clone(),
            photo_msg_id,
            state.config.telegram_topic_id,
            state.clone(),
        )
        .await;
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// Show update notification
// ---------------------------------------------------------------------------

/// Context for a show update notification
#[derive(Debug, Clone)]
pub struct ShowUpdateContext {
    pub show_id: i64,
    pub artist_name: String,
    pub action: ShowUpdateAction,
}

/// Action taken on a show's artist roster
#[derive(Debug, Clone, Copy)]
pub enum ShowUpdateAction {
    Added,
    Removed,
}

impl ShowUpdateAction {
    fn as_str(&self) -> &'static str {
        match self {
            ShowUpdateAction::Added => "added",
            ShowUpdateAction::Removed => "removed",
        }
    }
}

/// Fetch a show and all its currently assigned artists from the database.
///
/// Returns `Err` if the show doesn't exist or DB query fails.
async fn fetch_show_with_artists(
    state: &Arc<AppState>,
    show_id: i64,
) -> Result<(models::Show, Vec<models::Artist>), String> {
    // Fetch show
    let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(show_id)
        .fetch_optional(&state.db)
        .await
        .map_err(|e| format!("DB query for show failed: {e}"))?
        .ok_or_else(|| format!("Show {show_id} not found"))?;

    // Fetch assigned artists
    let artists: Vec<models::Artist> = sqlx::query_as(
        "SELECT a.* FROM artists a \
         INNER JOIN artist_show_assignments asa ON a.id = asa.artist_id \
         WHERE asa.show_id = ? \
         ORDER BY a.name",
    )
    .bind(show_id)
    .fetch_all(&state.db)
    .await
    .map_err(|e| format!("DB query for artists failed: {e}"))?;

    Ok((show, artists))
}

/// Build the HTML caption for a show update notification.
fn build_show_update_caption(
    show: &models::Show,
    artists: &[models::Artist],
    context: &ShowUpdateContext,
    show_url: &str,
) -> String {
    let mut parts: Vec<String> = Vec::new();

    // Header: show title + action emoji
    let action_emoji = match context.action {
        ShowUpdateAction::Added => "➕",
        ShowUpdateAction::Removed => "➖",
    };
    parts.push(format!(
        "🎪 <b>Show updated: {}</b>",
        html_escape(&show.title),
    ));

    // Action line
    parts.push(format!(
        "{action_emoji} <b>{}</b> was {}",
        html_escape(&context.artist_name),
        context.action.as_str()
    ));

    // Current artists list
    if artists.is_empty() {
        parts.push("🎤 No artists assigned yet.".to_string());
    } else {
        let artist_list: Vec<String> = artists
            .iter()
            .map(|a| format!("• {}", html_escape(&a.name)))
            .collect();
        parts.push(format!(
            "🎤 <b>Current Artists:</b>\n{}",
            artist_list.join("\n")
        ));
    }

    // Show link
    parts.push(format!("🔗 <a href=\"{show_url}\">Open show page</a>"));

    parts.join("\n\n")
}

/// Send a show update notification to the admin Telegram chat.
///
/// Sends the show cover image (if available) with an HTML caption describing
/// the artist assignment change and listing all currently assigned artists.
///
/// Returns `Err` if any critical step fails (DB query, presigned URL for cover).
async fn send_show_update_notification(
    state: &Arc<AppState>,
    context: ShowUpdateContext,
) -> Result<(), String> {
    let (Some(bot), Some(chat_id)) = (&state.telegram_bot, state.config.telegram_admin_chat_id)
    else {
        tracing::info!(
            "Telegram bot not configured, skipping show update notification for show {}",
            context.show_id
        );
        return Ok(()); // Not configured — silently skip
    };

    tracing::debug!(
        "Telegram bot configured, proceeding with show update notification (show_id={}, chat_id={})",
        context.show_id,
        chat_id
    );

    // Fetch show and assigned artists
    let (show, artists) = fetch_show_with_artists(state, context.show_id).await?;

    // Build show URL
    let base = state.config.admin_base_url.trim_end_matches('/');
    let show_url = format!("{base}/#/shows/{}", context.show_id);
    let caption = build_show_update_caption(&show, &artists, &context, &show_url);

    let chat = ChatId(chat_id);

    // Send photo with caption if show has a cover
    let cover_key = show.cover_generated_at.and_then(|_| {
        // Cover exists if cover_generated_at is set
        Some(format!("shows/{}/cover.png", show.id))
    });

    let token = state
        .config
        .telegram_bot_token
        .as_deref()
        .unwrap_or_default();

    if let Some(ref key) = cover_key {
        // Try to send photo, fall back to text if it fails
        match storage::get_presigned_url(state, key, 3600).await {
            Ok(url_str) => {
                let filename = format!("show_{}_cover.png", show.id);
                match download_file_bytes(&url_str).await {
                    Ok(photo_bytes) => {
                        match send_photo_raw(
                            token,
                            chat_id,
                            photo_bytes,
                            filename,
                            &caption,
                            "HTML",
                            state.config.telegram_topic_id,
                            None,
                        )
                        .await
                        {
                            Ok(_) => {
                                tracing::info!(
                                    "Show update notification sent with cover image for show {}",
                                    context.show_id
                                );
                                return Ok(());
                            }
                            Err(e) => {
                                tracing::warn!(
                                    "Failed to send show update photo, falling back to text: {e}"
                                );
                            }
                        }
                    }
                    Err(e) => {
                        tracing::warn!("Failed to download cover image, falling back to text: {e}");
                    }
                }
            }
            Err(e) => {
                tracing::warn!("Failed to get presigned URL for cover, falling back to text: {e}");
            }
        }
    } else {
        tracing::debug!(
            "Show {} has no cover, sending text-only notification",
            context.show_id
        );
    }

    // Send text-only HTML message (fallback or no cover)
    let mut req = bot.send_message(chat, &caption).parse_mode(ParseMode::Html);
    if let Some(tid) = state.config.telegram_topic_id {
        req = req.message_thread_id(ThreadId(MessageId(tid)));
    }
    match req.await {
        Ok(_) => {
            tracing::info!(
                "Show update notification sent (text-only) for show {}",
                context.show_id
            );
            Ok(())
        }
        Err(e) => {
            tracing::warn!("Failed to send show update text message: {e}");
            Err(format!("send_message failed: {e}"))
        }
    }
}

/// Schedule a show update notification with 30-second debouncing.
///
/// If a notification is already pending for this show, it will be canceled
/// and replaced with the new one. This ensures that rapid artist assignment
/// changes result in only a single notification with the final state.
///
/// Spawns a detached tokio task so the caller is never blocked.
pub fn schedule_show_update_notification(
    state: &Arc<AppState>,
    show_id: i64,
    artist_name: String,
    action: ShowUpdateAction,
) {
    tracing::info!(
        "Scheduling Telegram show update notification: show_id={}, artist='{}', action={:?}",
        show_id,
        artist_name,
        action
    );

    let state = state.clone();
    tokio::spawn(async move {
        // Cancel any pending notification for this show
        {
            let mut pending = state.pending_show_notifications.lock().await;
            if let Some(handle) = pending.remove(&show_id) {
                handle.abort();
                tracing::info!("Canceled pending show update notification for show {} (replaced with new notification)", show_id);
            }
        }

        // Spawn new delayed notification task
        let state_for_task = state.clone();
        let context = ShowUpdateContext {
            show_id,
            artist_name: artist_name.clone(),
            action,
        };

        let notification_task = tokio::spawn(async move {
            // Wait 30 seconds for debouncing
            tracing::info!(
                "Waiting 30 seconds before sending show update notification (show_id={}, artist='{}')",
                show_id,
                artist_name
            );
            tokio::time::sleep(tokio::time::Duration::from_secs(30)).await;

            tracing::info!(
                "Sending Telegram show update notification (show_id={}, artist='{}')",
                show_id,
                artist_name
            );

            // Send the notification
            if let Err(e) = send_show_update_notification(&state_for_task, context).await {
                tracing::warn!(
                    "Telegram show update notification failed for show {}: {}",
                    show_id,
                    e
                );
            } else {
                tracing::info!(
                    "Telegram show update notification sent successfully for show {}",
                    show_id
                );
            }

            // Remove self from pending map
            state_for_task
                .pending_show_notifications
                .lock()
                .await
                .remove(&show_id);
        });

        // Store the task handle
        state
            .pending_show_notifications
            .lock()
            .await
            .insert(show_id, notification_task);

        tracing::info!(
            "Show update notification task scheduled for show {} (will send in 30s)",
            show_id
        );
    });
}

/// Notify admin about a new artist submission.
///
/// Sends a rich preview (photo + text + audio) when possible, falling back
/// to a simple HTML text notification if the rich preview fails.
/// Spawns a detached tokio task so the caller is never blocked.
pub fn notify_artist_submission(state: &Arc<AppState>, artist_id: i64, artist_name: &str) {
    let state = state.clone();
    let name = artist_name.to_owned();
    tokio::spawn(async move {
        if let Err(e) = send_artist_preview(&state, artist_id).await {
            tracing::warn!("Rich artist preview failed, falling back to simple notification: {e}");
            let base = state.config.admin_base_url.trim_end_matches('/');
            let url = format!("{base}/#/artists/{artist_id}");
            notify_html(
                &state,
                &format!(
                    "🎤 New artist submitted: {name} (ID: {artist_id})\n<a href=\"{url}\">Open artist profile</a>"
                ),
            )
            .await;
        }
    });
}

/// Notify admin that a SoundCloud upload succeeded.
///
/// Includes the track title and a link to the SoundCloud URL.
/// Spawns a detached tokio task so the caller is never blocked.
pub fn notify_soundcloud_upload(state: &Arc<AppState>, show_id: i64, title: &str, track_url: &str) {
    let state = state.clone();
    let title = title.to_owned();
    let track_url = track_url.to_owned();
    tokio::spawn(async move {
        notify_html(
            &state,
            &format!(
                "☁️ SoundCloud upload complete\n\n\
                 <b>{}</b> (show #{})\n\n\
                 <a href=\"{}\">Open on SoundCloud</a>",
                html_escape(&title),
                show_id,
                html_escape(&track_url),
            ),
        )
        .await;
    });
}

/// Notify admin that a show was published to Instagram.
///
/// Includes the show title and a permalink to the Instagram post.
/// Spawns a detached tokio task so the caller is never blocked.
pub fn notify_instagram_published(
    state: &Arc<AppState>,
    show_id: i64,
    title: &str,
    permalink: Option<&str>,
) {
    let state = state.clone();
    let title = title.to_owned();
    let permalink = permalink.map(|s| s.to_owned());
    tokio::spawn(async move {
        let link_line = match permalink {
            Some(ref url) => format!("\n\n<a href=\"{}\">View on Instagram</a>", html_escape(url)),
            None => String::new(),
        };
        notify_html(
            &state,
            &format!(
                "📸 Published to Instagram\n\n\
                 <b>{}</b> (show #{}){}",
                html_escape(&title),
                show_id,
                link_line,
            ),
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

/// Send an Instagram post preview for a show to the admin Telegram chat.
///
/// Sends the show cover image with the exact Instagram caption and inline
/// keyboard buttons for publishing or editing. This is the approval step
/// before posting to Instagram.
///
/// Returns `Ok(())` on success, or an error string on failure.
pub async fn send_show_instagram_preview(
    state: &Arc<AppState>,
    show_id: i64,
) -> Result<(), String> {
    if state.telegram_bot.is_none() {
        return Err("Telegram bot not configured".to_string());
    }

    let chat_id = state
        .config
        .telegram_admin_chat_id
        .ok_or("Telegram admin chat ID not configured")?;

    let token = state
        .config
        .telegram_bot_token
        .as_deref()
        .ok_or("Telegram bot token not configured")?;

    // Fetch show from DB
    let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(show_id)
        .fetch_optional(&state.db)
        .await
        .map_err(|e| format!("DB query failed: {e}"))?
        .ok_or_else(|| format!("Show {show_id} not found"))?;

    if show.cover_generated_at.is_none() {
        return Err("Show has no cover image. Assign artists first.".to_string());
    }

    // Build the exact Instagram caption
    let caption = instagram::build_show_caption(state, &show)
        .await
        .map_err(|e| format!("Failed to build caption: {e}"))?;

    // Download cover image from R2
    let cover_key = format!("shows/{}/cover.png", show.id);
    let cover_url = storage::get_presigned_url(state, &cover_key, 3600)
        .await
        .map_err(|e| format!("Presigned URL failed: {e}"))?;
    let photo_bytes = download_file_bytes(&cover_url).await?;

    // Build inline keyboard: [Publish] [Edit]
    let reply_markup = serde_json::json!({
        "inline_keyboard": [[
            {
                "text": "📸 Publish to Instagram",
                "callback_data": format!("ig_publish:{show_id}")
            },
            {
                "text": "✏️ Edit",
                "callback_data": format!("ig_edit:{show_id}")
            }
        ]]
    });
    let markup_json =
        serde_json::to_string(&reply_markup).map_err(|e| format!("JSON serialize failed: {e}"))?;

    // Send photo with caption and inline keyboard
    let filename = format!("show_{}_cover.png", show.id);
    send_photo_raw(
        token,
        chat_id,
        photo_bytes,
        filename,
        &caption,
        "", // no parse_mode — caption is plain text (Instagram format)
        state.config.telegram_topic_id,
        Some(&markup_json),
    )
    .await?;

    // Update telegram_preview_sent_at
    let now = chrono::Utc::now().to_rfc3339();
    let _ = sqlx::query("UPDATE shows SET telegram_preview_sent_at = ? WHERE id = ?")
        .bind(&now)
        .bind(show_id)
        .execute(&state.db)
        .await;

    tracing::info!("Instagram preview sent to Telegram for show {show_id}");
    Ok(())
}

// ---------------------------------------------------------------------------
// Artist Instagram preview
// ---------------------------------------------------------------------------

/// Build the inline keyboard for an artist Instagram preview message.
///
/// Row 1: [📤 Publish]
/// Row 2: [✏️ Caption] [🖼 Image]
/// Row 3 (conditional): [🎬 Video 1] [🎬 Video 2]
pub fn build_artist_preview_keyboard(artist_id: i64, has_track1: bool, has_track2: bool) -> String {
    let mut rows = vec![
        // Row 1: Publish
        serde_json::json!([
            { "text": "📤 Publish", "callback_data": format!("aig_pub:{artist_id}") }
        ]),
        // Row 2: Edit caption / image
        serde_json::json!([
            { "text": "✏️ Caption", "callback_data": format!("aig_cap:{artist_id}") },
            { "text": "🖼 Image", "callback_data": format!("aig_img:{artist_id}") }
        ]),
    ];

    // Row 3: Video regeneration buttons (only for tracks that exist)
    let mut video_row = Vec::new();
    if has_track1 {
        video_row.push(serde_json::json!(
            { "text": "🎬 Video 1", "callback_data": format!("aig_vid:{artist_id}:1") }
        ));
    }
    if has_track2 {
        video_row.push(serde_json::json!(
            { "text": "🎬 Video 2", "callback_data": format!("aig_vid:{artist_id}:2") }
        ));
    }
    if !video_row.is_empty() {
        rows.push(serde_json::json!(video_row));
    }

    let markup = serde_json::json!({ "inline_keyboard": rows });
    serde_json::to_string(&markup).unwrap_or_default()
}

/// Download a track preview video from R2 and send it as a Telegram message.
///
/// Returns the sent message_id on success. Logs and returns None on failure.
async fn send_track_video(
    token: &str,
    chat_id: i64,
    thread_id: Option<i32>,
    state: &Arc<AppState>,
    video_key: &str,
    track_number: u8,
    track_name: &str,
) -> Option<i64> {
    let url_str = match storage::get_presigned_url(state, video_key, 3600).await {
        Ok(u) => u,
        Err(e) => {
            tracing::warn!("Presigned URL for track {track_number} video failed: {e}");
            return None;
        }
    };

    let video_bytes = match download_file_bytes(&url_str).await {
        Ok(b) => b,
        Err(e) => {
            tracing::warn!("Download track {track_number} video failed: {e}");
            return None;
        }
    };

    let filename = video_key
        .rsplit('/')
        .next()
        .unwrap_or("video.mp4")
        .to_owned();
    let caption = format!("🎵 Track {track_number}: {track_name}");

    match send_video_raw(
        token,
        chat_id,
        video_bytes,
        filename,
        &caption,
        "",
        thread_id,
        None,
    )
    .await
    {
        Ok(msg_id) => Some(msg_id),
        Err(e) => {
            tracing::warn!("sendVideo for track {track_number} failed: {e}");
            None
        }
    }
}

/// Send an artist's Instagram preview to the admin Telegram chat.
///
/// Sends:
/// 1. The artist's photo (overlay → cropped → original) with Instagram caption
///    and inline keyboard buttons (Publish, Edit Caption, Replace Image, Regen Videos).
/// 2. Track 1 preview video (if track1_video_key exists) as a separate reply.
/// 3. Track 2 preview video (if track2_video_key exists) as a separate reply.
///
/// Stores message IDs in the artists table for later in-place editing.
pub async fn send_artist_instagram_preview(
    state: &Arc<AppState>,
    artist: &models::Artist,
) -> Result<(), String> {
    if state.telegram_bot.is_none() {
        return Err("Telegram bot not configured".to_string());
    }

    let chat_id = state
        .config
        .telegram_admin_chat_id
        .ok_or("Telegram admin chat ID not configured")?;

    let token = state
        .config
        .telegram_bot_token
        .as_deref()
        .ok_or("Telegram bot token not configured")?;

    // Require an instagram_caption
    let caption = artist
        .instagram_caption
        .as_deref()
        .ok_or("Artist has no Instagram caption. Generate one first.")?;

    // Select best available photo: overlay → cropped → original
    let pic_key = artist
        .pic_overlay_key
        .as_ref()
        .or(artist.pic_cropped_key.as_ref())
        .or(artist.pic_key.as_ref())
        .ok_or("Artist has no photo")?;

    // Download photo from R2
    let pic_url = storage::get_presigned_url(state, pic_key, 3600)
        .await
        .map_err(|e| format!("Presigned URL for artist photo failed: {e}"))?;
    let photo_bytes = download_file_bytes(&pic_url).await?;

    // Build inline keyboard
    let has_track1 = artist.track1_video_key.is_some();
    let has_track2 = artist.track2_video_key.is_some();
    let markup_json = build_artist_preview_keyboard(artist.id, has_track1, has_track2);

    // Send photo with caption and inline keyboard
    let filename = format!("artist_{}_preview.png", artist.id);
    let preview_msg_id = send_photo_raw(
        token,
        chat_id,
        photo_bytes,
        filename,
        caption,
        "", // no parse_mode — caption is plain text (Instagram format)
        state.config.telegram_topic_id,
        Some(&markup_json),
    )
    .await?;

    // Store the photo message ID
    let _ = sqlx::query("UPDATE artists SET telegram_preview_message_id = ? WHERE id = ?")
        .bind(preview_msg_id)
        .bind(artist.id)
        .execute(&state.db)
        .await;

    // Send track 1 video if available
    if let Some(ref video_key) = artist.track1_video_key {
        if let Some(vid_msg_id) = send_track_video(
            token,
            chat_id,
            state.config.telegram_topic_id,
            state,
            video_key,
            1,
            &artist.track1_name,
        )
        .await
        {
            let _ = sqlx::query("UPDATE artists SET telegram_video1_message_id = ? WHERE id = ?")
                .bind(vid_msg_id)
                .bind(artist.id)
                .execute(&state.db)
                .await;
        }
    }

    // Send track 2 video if available
    if let Some(ref video_key) = artist.track2_video_key {
        if let Some(vid_msg_id) = send_track_video(
            token,
            chat_id,
            state.config.telegram_topic_id,
            state,
            video_key,
            2,
            &artist.track2_name,
        )
        .await
        {
            let _ = sqlx::query("UPDATE artists SET telegram_video2_message_id = ? WHERE id = ?")
                .bind(vid_msg_id)
                .bind(artist.id)
                .execute(&state.db)
                .await;
        }
    }

    // Update timestamp
    let now = chrono::Utc::now().to_rfc3339();
    let _ = sqlx::query("UPDATE artists SET telegram_artist_preview_sent_at = ? WHERE id = ?")
        .bind(&now)
        .bind(artist.id)
        .execute(&state.db)
        .await;

    tracing::info!(
        "Artist Instagram preview sent to Telegram for artist {} ({})",
        artist.id,
        artist.name
    );
    Ok(())
}

// ---------------------------------------------------------------------------
// Sort order prompt
// ---------------------------------------------------------------------------

/// Build the inline keyboard JSON for the artist sort order prompt.
///
/// Each artist gets a row: [⬆️] [Name (#N)] [⬇️]
/// First artist has no ⬆️, last artist has no ⬇️.
pub fn build_sort_order_keyboard(
    show_id: i64,
    artists: &[(i64, String)], // (artist_id, name)
) -> String {
    let len = artists.len();
    let mut rows: Vec<serde_json::Value> = Vec::new();

    for (i, (artist_id, name)) in artists.iter().enumerate() {
        let mut row = Vec::new();

        if i > 0 {
            row.push(serde_json::json!({
                "text": "⬆️",
                "callback_data": format!("aig_sort:{show_id}:{artist_id}:up")
            }));
        }

        row.push(serde_json::json!({
            "text": format!("{} (#{pos})", name, pos = i + 1),
            "callback_data": format!("aig_sort_noop:{show_id}:{artist_id}")
        }));

        if i < len - 1 {
            row.push(serde_json::json!({
                "text": "⬇️",
                "callback_data": format!("aig_sort:{show_id}:{artist_id}:down")
            }));
        }

        rows.push(serde_json::json!(row));
    }

    let markup = serde_json::json!({ "inline_keyboard": rows });
    serde_json::to_string(&markup).unwrap_or_default()
}

/// Send (or edit) a sort-order prompt message for artist post scheduling.
///
/// Lists artists with ⬆️/⬇️ buttons. Day 1 after show → first artist, etc.
pub async fn send_sort_order_prompt(state: &Arc<AppState>, show_id: i64) -> Result<(), String> {
    let chat_id = state
        .config
        .telegram_admin_chat_id
        .ok_or("Telegram admin chat ID not configured")?;
    let token = state
        .config
        .telegram_bot_token
        .as_deref()
        .ok_or("Telegram bot token not configured")?;

    // Fetch the show title
    let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(show_id)
        .fetch_optional(&state.db)
        .await
        .map_err(|e| format!("DB error: {e}"))?
        .ok_or_else(|| format!("Show {show_id} not found"))?;

    // Fetch assigned artists in sort order
    let artists: Vec<models::Artist> = sqlx::query_as(
        "SELECT a.* FROM artists a \
         INNER JOIN artist_show_assignments asa ON a.id = asa.artist_id \
         WHERE asa.show_id = ? ORDER BY asa.sort_order, a.name COLLATE NOCASE",
    )
    .bind(show_id)
    .fetch_all(&state.db)
    .await
    .map_err(|e| format!("DB error: {e}"))?;

    if artists.is_empty() {
        return Ok(()); // No artists assigned — nothing to prompt
    }

    let artist_pairs: Vec<(i64, String)> = artists.iter().map(|a| (a.id, a.name.clone())).collect();

    let keyboard = build_sort_order_keyboard(show_id, &artist_pairs);

    let text = format!(
        "📋 Set artist post order for {}\n\n\
         Day 1 after show → first artist, Day 2 → second, etc.",
        show.title
    );

    let _ = send_message_raw(
        token,
        chat_id,
        &text,
        state.config.telegram_topic_id,
        Some(&keyboard),
    )
    .await?;

    Ok(())
}
