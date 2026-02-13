use std::sync::Arc;

use teloxide::prelude::*;
use teloxide::types::{InputFile, MessageId, ParseMode, ReplyParameters, ThreadId};

use crate::{models, storage, AppState};

/// HTML-escape user-provided text so it doesn't break Telegram's HTML parser.
fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

/// Download a file from a presigned URL into memory and wrap it as an `InputFile`.
///
/// `InputFile::url()` is not implemented in teloxide-core 0.10.x's multipart
/// serializer, so we fetch the bytes ourselves and use `InputFile::memory()`.
async fn download_input_file(presigned_url: &str, filename: String) -> Result<InputFile, String> {
    let bytes = reqwest::get(presigned_url)
        .await
        .map_err(|e| format!("HTTP download failed: {e}"))?
        .error_for_status()
        .map_err(|e| format!("HTTP status error: {e}"))?
        .bytes()
        .await
        .map_err(|e| format!("Failed to read bytes: {e}"))?;
    Ok(InputFile::memory(bytes).file_name(filename))
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
        "🎤 <b>{}</b> ({})",
        html_escape(&artist.name),
        html_escape(&artist.pronouns)
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
    let photo_msg_id = if let Some(ref pic_key) = artist
        .pic_overlay_key
        .as_ref()
        .or(artist.pic_cropped_key.as_ref())
        .or(artist.pic_key.as_ref())
    {
        let url_str = storage::get_presigned_url(state, pic_key, 3600)
            .await
            .map_err(|e| format!("Presigned URL for photo failed: {e}"))?;

        // Extract a filename from the key for Telegram
        let filename = pic_key.rsplit('/').next().unwrap_or("photo.jpg").to_owned();
        let input_file = download_input_file(&url_str, filename).await?;

        let mut req = bot
            .send_photo(chat, input_file)
            .caption(caption)
            .parse_mode(ParseMode::Html);
        if let Some(tid) = state.config.telegram_topic_id {
            req = req.message_thread_id(ThreadId(MessageId(tid)));
        }
        match req.await {
            Ok(msg) => Some(msg.id),
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
    let send_audio = |bot: teloxide::Bot,
                      chat: ChatId,
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
        let input_file = match download_input_file(&url_str, filename).await {
            Ok(f) => f,
            Err(e) => {
                tracing::warn!("Download audio {key} failed: {e}");
                return;
            }
        };

        let mut req = bot
            .send_audio(chat, input_file)
            .title(title)
            .performer(performer);
        if let Some(tid) = topic_id {
            req = req.message_thread_id(ThreadId(MessageId(tid)));
        }
        if let Some(msg_id) = reply_to {
            req = req.reply_parameters(ReplyParameters::new(msg_id));
        }
        if let Err(e) = req.await {
            tracing::warn!("Failed to send audio {key}: {e}");
        }
    };

    // --- Audio messages (fire-and-forget style, but awaited sequentially) ---

    // Voice mail
    if let Some(ref voice_key) = artist.voice_message_key {
        send_audio(
            bot.clone(),
            chat,
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
        send_audio(
            bot.clone(),
            chat,
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
        send_audio(
            bot.clone(),
            chat,
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
