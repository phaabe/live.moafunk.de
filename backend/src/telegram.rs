//! Telegram bot for admin control of the UN/HEARD backend.
//!
//! Provides commands to list artists/shows, trigger AI generation,
//! preview and publish Instagram posts, and check stream status.
//!
//! The bot runs as a long-polling task alongside the HTTP server.
//! It is disabled (no-op) when `TELEGRAM_BOT_TOKEN` is not set.

use crate::{ai, instagram, models, storage, video, AppError, AppState};
use std::sync::Arc;
use teloxide::{
    dispatching::Dispatcher,
    prelude::*,
    types::{InputFile, MessageId, ThreadId},
    utils::command::BotCommands,
};

type HandlerResult = Result<(), Box<dyn std::error::Error + Send + Sync>>;

/// Bot command definitions.
///
/// Command names are derived from variant names using snake_case.
/// teloxide auto-generates `/help` output from the `description` attributes.
#[derive(BotCommands, Clone)]
#[command(
    rename_rule = "snake_case",
    description = "UN/HEARD Bot — manage artists, shows & streams\n"
)]
pub enum Command {
    #[command(description = "show this help")]
    Start,
    #[command(description = "show this help")]
    Help,
    #[command(description = "list unassigned artists")]
    Artists,
    #[command(description = "list upcoming shows")]
    Shows,
    #[command(description = "<artist_id> — artist details")]
    Artist(i64),
    #[command(description = "<show_id> — show details")]
    Show(i64),
    #[command(description = "<artist_id> — generate AI bio + IG caption")]
    GenerateBio(i64),
    #[command(description = "<artist_id> — generate track preview videos")]
    GenerateVideos(i64),
    #[command(description = "<artist_id> — preview IG caption + image")]
    PreviewInstagram(i64),
    #[command(description = "<artist_id> <text> — replace IG caption")]
    EditCaption(String),
    #[command(description = "<artist_id> — publish artist to Instagram")]
    PostInstagram(i64),
    #[command(description = "<show_id> — publish show cover to Instagram")]
    PostShowInstagram(i64),
    #[command(description = "is the stream live?")]
    StreamStatus,
    #[command(description = "artists, shows & stream summary")]
    Stats,
}

/// Start the Telegram bot dispatcher. No-op if bot token is not configured.
///
/// This function runs indefinitely via long-polling. Call via `tokio::spawn`
/// so it runs alongside the HTTP server.
pub async fn run(state: Arc<AppState>) {
    let bot = match &state.telegram_bot {
        Some(bot) => bot.clone(),
        None => {
            tracing::debug!("Telegram bot not configured, skipping");
            return;
        }
    };

    tracing::info!("Starting Telegram bot (long-polling)");

    let handler = Update::filter_message()
        .branch(dptree::entry().filter_command::<Command>().endpoint(answer))
        .branch(
            // Catch-all: silently ignore non-command messages (no "Unhandled update" warnings)
            dptree::entry().endpoint(
                |_bot: Bot, _msg: Message, _state: Arc<AppState>| async move {
                    Ok::<(), Box<dyn std::error::Error + Send + Sync>>(())
                },
            ),
        );

    Dispatcher::builder(bot, handler)
        .dependencies(dptree::deps![state])
        .build()
        .dispatch()
        .await;
}

/// Top-level command handler. Auth check → dispatch → reply (or error).
async fn answer(bot: Bot, msg: Message, cmd: Command, state: Arc<AppState>) -> HandlerResult {
    let chat_id = msg.chat.id;
    let thread_id = state.config.telegram_topic_id;

    // Auth: only respond to the configured admin chat
    if let Some(admin_id) = state.config.telegram_admin_chat_id {
        if chat_id.0 != admin_id {
            send_msg(&bot, chat_id, thread_id, "⛔ Unauthorized").await?;
            return Ok(());
        }
    } else {
        // No admin chat ID configured — silently ignore
        return Ok(());
    }

    // If topic is configured, only respond to messages from that topic
    if let Some(tid) = thread_id {
        let msg_thread = msg.thread_id.map(|t| t.0 .0);
        if msg_thread != Some(tid) {
            return Ok(());
        }
    }

    if let Err(e) = handle_command(&bot, chat_id, thread_id, cmd, &state).await {
        send_msg(&bot, chat_id, thread_id, &format!("❌ Error: {e}")).await?;
    }

    Ok(())
}

/// Dispatch a parsed command to its handler and send the response.
async fn handle_command(
    bot: &Bot,
    chat_id: ChatId,
    thread_id: Option<i32>,
    cmd: Command,
    state: &Arc<AppState>,
) -> HandlerResult {
    match cmd {
        Command::Start | Command::Help => {
            send_msg(
                bot,
                chat_id,
                thread_id,
                &Command::descriptions().to_string(),
            )
            .await?;
        }
        Command::Artists => {
            send_text(bot, chat_id, thread_id, &cmd_artists(state).await?).await?;
        }
        Command::Shows => {
            send_text(bot, chat_id, thread_id, &cmd_shows(state).await?).await?;
        }
        Command::Artist(id) => {
            send_text(bot, chat_id, thread_id, &cmd_artist(state, id).await?).await?;
        }
        Command::Show(id) => {
            send_text(bot, chat_id, thread_id, &cmd_show(state, id).await?).await?;
        }
        Command::GenerateBio(id) => {
            send_msg(bot, chat_id, thread_id, "🤖 Generating bio + caption…").await?;
            send_text(bot, chat_id, thread_id, &cmd_generate_bio(state, id).await?).await?;
        }
        Command::GenerateVideos(id) => {
            send_msg(bot, chat_id, thread_id, "🎬 Generating videos…").await?;
            send_text(
                bot,
                chat_id,
                thread_id,
                &cmd_generate_videos(state, id).await?,
            )
            .await?;
        }
        Command::PreviewInstagram(id) => {
            cmd_preview_instagram(bot, chat_id, thread_id, state, id).await?;
        }
        Command::EditCaption(args) => {
            send_text(
                bot,
                chat_id,
                thread_id,
                &cmd_edit_caption(state, &args).await?,
            )
            .await?;
        }
        Command::PostInstagram(id) => {
            send_msg(bot, chat_id, thread_id, "📸 Publishing to Instagram…").await?;
            send_text(
                bot,
                chat_id,
                thread_id,
                &cmd_post_instagram(state, id).await?,
            )
            .await?;
        }
        Command::PostShowInstagram(id) => {
            send_msg(bot, chat_id, thread_id, "📸 Publishing show to Instagram…").await?;
            send_text(
                bot,
                chat_id,
                thread_id,
                &cmd_post_show_instagram(state, id).await?,
            )
            .await?;
        }
        Command::StreamStatus => {
            send_text(bot, chat_id, thread_id, &cmd_stream_status(state).await?).await?;
        }
        Command::Stats => {
            send_text(bot, chat_id, thread_id, &cmd_stats(state).await?).await?;
        }
    }

    Ok(())
}

// ============================================================================
// Helpers
// ============================================================================

/// Send a single text message, optionally targeting a forum topic.
async fn send_msg(
    bot: &Bot,
    chat_id: ChatId,
    thread_id: Option<i32>,
    text: &str,
) -> Result<(), teloxide::RequestError> {
    let mut req = bot.send_message(chat_id, text);
    if let Some(tid) = thread_id {
        req = req.message_thread_id(ThreadId(MessageId(tid)));
    }
    req.await?;
    Ok(())
}

/// Send a photo, optionally targeting a forum topic.
async fn send_img(
    bot: &Bot,
    chat_id: ChatId,
    thread_id: Option<i32>,
    photo: InputFile,
) -> Result<(), teloxide::RequestError> {
    let mut req = bot.send_photo(chat_id, photo);
    if let Some(tid) = thread_id {
        req = req.message_thread_id(ThreadId(MessageId(tid)));
    }
    req.await?;
    Ok(())
}

/// Send a text message, splitting into chunks if it exceeds Telegram's 4096-char limit.
async fn send_text(
    bot: &Bot,
    chat_id: ChatId,
    thread_id: Option<i32>,
    text: &str,
) -> Result<(), teloxide::RequestError> {
    const MAX_LEN: usize = 4096;

    if text.len() <= MAX_LEN {
        send_msg(bot, chat_id, thread_id, text).await?;
        return Ok(());
    }

    let mut chunk = String::new();
    for line in text.lines() {
        if chunk.len() + line.len() + 1 > MAX_LEN && !chunk.is_empty() {
            send_msg(bot, chat_id, thread_id, &std::mem::take(&mut chunk)).await?;
        }
        if !chunk.is_empty() {
            chunk.push('\n');
        }
        chunk.push_str(line);
    }
    if !chunk.is_empty() {
        send_msg(bot, chat_id, thread_id, &chunk).await?;
    }

    Ok(())
}

// ============================================================================
// Command Handlers
// ============================================================================

/// /artists — List unassigned artists with status indicators.
async fn cmd_artists(state: &Arc<AppState>) -> crate::Result<String> {
    let artists: Vec<models::Artist> = sqlx::query_as(
        "SELECT * FROM artists \
         WHERE id NOT IN (SELECT DISTINCT artist_id FROM artist_show_assignments) \
         ORDER BY created_at DESC",
    )
    .fetch_all(&state.db)
    .await?;

    if artists.is_empty() {
        return Ok("No unassigned artists.".to_string());
    }

    let mut text = format!("📋 Unassigned artists ({}):\n\n", artists.len());
    for a in &artists {
        let pic = if a.pic_key.is_some() { "📷" } else { "·" };
        let bio = if a.ai_bio.is_some() { "🤖" } else { "·" };
        let vid = if a.track1_video_key.is_some() {
            "🎬"
        } else {
            "·"
        };
        let cap = if a.instagram_caption.is_some() {
            "📝"
        } else {
            "·"
        };
        text.push_str(&format!(
            "{}{}{}{} [{}] {}\n",
            pic, bio, vid, cap, a.id, a.name
        ));
    }
    text.push_str("\n📷pic 🤖bio 🎬video 📝caption");
    Ok(text)
}

/// /shows — List upcoming shows with assigned artist counts.
async fn cmd_shows(state: &Arc<AppState>) -> crate::Result<String> {
    let shows: Vec<models::Show> =
        sqlx::query_as("SELECT * FROM shows WHERE date >= date('now') ORDER BY date ASC")
            .fetch_all(&state.db)
            .await?;

    if shows.is_empty() {
        return Ok("No upcoming shows.".to_string());
    }

    let mut text = format!("📅 Upcoming shows ({}):\n\n", shows.len());
    for s in &shows {
        let count: (i64,) =
            sqlx::query_as("SELECT COUNT(*) FROM artist_show_assignments WHERE show_id = ?")
                .bind(s.id)
                .fetch_one(&state.db)
                .await?;

        let cover = if s.cover_generated_at.is_some() {
            "🖼"
        } else {
            "·"
        };
        let rec = if s.recording_key.is_some() {
            "🎙"
        } else {
            "·"
        };
        text.push_str(&format!(
            "{}{} [{}] {} — {} ({} artists)\n",
            cover, rec, s.id, s.date, s.title, count.0
        ));
    }
    text.push_str("\n🖼cover 🎙recording");
    Ok(text)
}

/// /artist <id> — Detailed artist view.
async fn cmd_artist(state: &Arc<AppState>, id: i64) -> crate::Result<String> {
    let artist: models::Artist = sqlx::query_as("SELECT * FROM artists WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound(format!("Artist {id} not found")))?;

    let check = |opt: &Option<String>| if opt.is_some() { "✅" } else { "❌" };

    let mut text = format!("🎤 {} (ID: {})\n", artist.name, artist.id);
    text.push_str(&format!("Pronouns: {}\n", artist.pronouns));
    text.push_str(&format!("Status: {}\n\n", artist.status));

    text.push_str(&format!(
        "Track 1: \"{}\" {}\n",
        artist.track1_name,
        check(&artist.track1_key)
    ));
    text.push_str(&format!(
        "Track 2: \"{}\" {}\n\n",
        artist.track2_name,
        check(&artist.track2_key)
    ));

    text.push_str(&format!("📷 Picture: {}\n", check(&artist.pic_key)));
    text.push_str(&format!("🤖 AI Bio: {}\n", check(&artist.ai_bio)));
    text.push_str(&format!("🎬 Videos: {}\n", check(&artist.track1_video_key)));
    text.push_str(&format!(
        "📝 Caption: {}\n",
        check(&artist.instagram_caption)
    ));
    text.push_str(&format!(
        "📸 Posted: {}\n",
        artist.instagram_posted_at.as_deref().unwrap_or("not yet")
    ));

    // Socials
    let socials = [
        ("IG", &artist.instagram),
        ("SC", &artist.soundcloud),
        ("BC", &artist.bandcamp),
        ("Spotify", &artist.spotify),
    ];
    for (label, val) in &socials {
        if let Some(v) = val {
            if !v.is_empty() {
                text.push_str(&format!("\n{label}: {v}"));
            }
        }
    }

    Ok(text)
}

/// /show <id> — Detailed show view with assigned artists.
async fn cmd_show(state: &Arc<AppState>, id: i64) -> crate::Result<String> {
    let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound(format!("Show {id} not found")))?;

    let mut text = format!("📅 {} (ID: {})\n", show.title, show.id);
    text.push_str(&format!("Date: {}\n", show.date));
    text.push_str(&format!("Status: {}\n", show.status));

    if let Some(ref desc) = show.description {
        if !desc.is_empty() {
            text.push_str(&format!("Description: {desc}\n"));
        }
    }

    text.push_str(&format!(
        "\n🖼 Cover: {}\n",
        if show.cover_generated_at.is_some() {
            "✅"
        } else {
            "❌"
        }
    ));
    text.push_str(&format!(
        "🎙 Recording: {}\n",
        if show.recording_key.is_some() {
            "✅"
        } else {
            "❌"
        }
    ));
    text.push_str(&format!(
        "📸 Posted: {}\n",
        show.instagram_posted_at.as_deref().unwrap_or("not yet")
    ));

    // Assigned artists
    let artists: Vec<models::Artist> = sqlx::query_as(
        "SELECT a.* FROM artists a \
         INNER JOIN artist_show_assignments asa ON a.id = asa.artist_id \
         WHERE asa.show_id = ? ORDER BY a.name",
    )
    .bind(id)
    .fetch_all(&state.db)
    .await?;

    if artists.is_empty() {
        text.push_str("\nNo artists assigned.");
    } else {
        text.push_str(&format!("\nArtists ({}):\n", artists.len()));
        for a in &artists {
            text.push_str(&format!("  [{}] {}\n", a.id, a.name));
        }
    }

    Ok(text)
}

/// /generate_bio <id> — Generate AI bio + full Instagram caption.
async fn cmd_generate_bio(state: &Arc<AppState>, id: i64) -> crate::Result<String> {
    ai::generate_and_store_artist_bio(state, id).await?;
    let caption = ai::generate_and_store_instagram_caption(state, id).await?;

    let preview: String = caption.chars().take(500).collect();
    let suffix = if caption.chars().count() > 500 {
        "…"
    } else {
        ""
    };

    Ok(format!("✅ Bio + caption generated!\n\n{preview}{suffix}"))
}

/// /generate_videos <id> — Generate track preview waveform videos.
async fn cmd_generate_videos(state: &Arc<AppState>, id: i64) -> crate::Result<String> {
    video::generate_and_store_artist_videos(state.clone(), id).await?;
    Ok("✅ Videos generated successfully!".to_string())
}

/// /preview_instagram <id> — Send caption text + profile pic to chat.
async fn cmd_preview_instagram(
    bot: &Bot,
    chat_id: ChatId,
    thread_id: Option<i32>,
    state: &Arc<AppState>,
    id: i64,
) -> HandlerResult {
    let artist: models::Artist = sqlx::query_as("SELECT * FROM artists WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound(format!("Artist {id} not found")))?;

    // Send caption text
    let caption = artist
        .instagram_caption
        .as_deref()
        .unwrap_or("(no caption generated yet)");
    send_text(
        bot,
        chat_id,
        thread_id,
        &format!("📝 Caption for {} (ID: {id}):\n\n{caption}", artist.name),
    )
    .await?;

    // Send profile picture (priority: overlay → cropped → original)
    let pic_key = artist
        .pic_overlay_key
        .as_ref()
        .or(artist.pic_cropped_key.as_ref())
        .or(artist.pic_key.as_ref());

    if let Some(key) = pic_key {
        match storage::download_file(state, key).await {
            Ok((bytes, _content_type)) => {
                let photo = InputFile::memory(bytes).file_name("preview.jpg");
                send_img(bot, chat_id, thread_id, photo).await?;
            }
            Err(e) => {
                send_msg(
                    bot,
                    chat_id,
                    thread_id,
                    &format!("⚠️ Could not load image: {e}"),
                )
                .await?;
            }
        }
    } else {
        send_msg(bot, chat_id, thread_id, "⚠️ No profile picture available.").await?;
    }

    Ok(())
}

/// /edit_caption <id> <text> — Update an artist's Instagram caption.
async fn cmd_edit_caption(state: &Arc<AppState>, args: &str) -> crate::Result<String> {
    let args = args.trim();
    let space_idx = args.find(' ').ok_or_else(|| {
        AppError::Validation("Usage: /edit_caption <id> <new caption text>".to_string())
    })?;

    let id: i64 = args[..space_idx]
        .parse()
        .map_err(|_| AppError::Validation("Invalid artist ID".to_string()))?;
    let new_caption = args[space_idx + 1..].trim();

    if new_caption.is_empty() {
        return Err(AppError::Validation(
            "Caption text cannot be empty".to_string(),
        ));
    }

    // Verify artist exists
    let _: models::Artist = sqlx::query_as("SELECT * FROM artists WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound(format!("Artist {id} not found")))?;

    sqlx::query(
        "UPDATE artists SET instagram_caption = ?, updated_at = datetime('now') WHERE id = ?",
    )
    .bind(new_caption)
    .bind(id)
    .execute(&state.db)
    .await?;

    let preview: String = new_caption.chars().take(100).collect();
    let suffix = if new_caption.chars().count() > 100 {
        "…"
    } else {
        ""
    };

    Ok(format!(
        "✅ Caption updated for artist {id}.\n\n{preview}{suffix}"
    ))
}

/// /post_instagram <id> — Publish artist to Instagram.
async fn cmd_post_instagram(state: &Arc<AppState>, id: i64) -> crate::Result<String> {
    let artist: models::Artist = sqlx::query_as("SELECT * FROM artists WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound(format!("Artist {id} not found")))?;

    let account = state.config.telegram_instagram_account();
    let result = instagram::post_artist_to_instagram(state, &artist, account).await?;

    if result.success {
        Ok(format!(
            "✅ Published {} to Instagram ({})\nMedia ID: {}",
            artist.name,
            account,
            result.media_id.unwrap_or_default()
        ))
    } else {
        Ok(format!(
            "❌ Instagram publish failed: {}",
            result.error.unwrap_or_default()
        ))
    }
}

/// /post_show_instagram <id> — Publish show cover to Instagram.
async fn cmd_post_show_instagram(state: &Arc<AppState>, id: i64) -> crate::Result<String> {
    let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound(format!("Show {id} not found")))?;

    let account = state.config.telegram_instagram_account();
    let result = instagram::post_show_to_instagram(state, &show, account).await?;

    if result.success {
        Ok(format!(
            "✅ Published {} to Instagram ({})\nMedia ID: {}",
            show.title,
            account,
            result.media_id.unwrap_or_default()
        ))
    } else {
        Ok(format!(
            "❌ Instagram publish failed: {}",
            result.error.unwrap_or_default()
        ))
    }
}

/// /stream_status — Check if stream is active.
async fn cmd_stream_status(state: &Arc<AppState>) -> crate::Result<String> {
    let stream = state.stream_state.lock().await;
    let status = stream.get_status();
    drop(stream);

    if status.active {
        let user = status.user.as_deref().unwrap_or("unknown");
        let rec = if status.recording {
            " 🎙 recording"
        } else {
            ""
        };
        Ok(format!("📡 Stream active — {user} streaming{rec}"))
    } else {
        Ok("📡 No active stream.".to_string())
    }
}

/// /stats — Summary statistics.
async fn cmd_stats(state: &Arc<AppState>) -> crate::Result<String> {
    let total: (i64,) = sqlx::query_as("SELECT COUNT(*) FROM artists")
        .fetch_one(&state.db)
        .await?;

    let unassigned: (i64,) = sqlx::query_as(
        "SELECT COUNT(*) FROM artists \
         WHERE id NOT IN (SELECT DISTINCT artist_id FROM artist_show_assignments)",
    )
    .fetch_one(&state.db)
    .await?;

    let upcoming: (i64,) = sqlx::query_as("SELECT COUNT(*) FROM shows WHERE date >= date('now')")
        .fetch_one(&state.db)
        .await?;

    let stream = state.stream_state.lock().await;
    let stream_active = stream.is_active();
    let stream_user = stream.current_user.clone();
    drop(stream);

    let stream_text = if stream_active {
        format!(
            "📡 Active ({})",
            stream_user.as_deref().unwrap_or("unknown")
        )
    } else {
        "📡 Inactive".to_string()
    };

    Ok(format!(
        "📊 UN/HEARD Stats\n\n\
         🎤 Artists: {} total, {} unassigned\n\
         📅 Upcoming shows: {}\n\
         {stream_text}",
        total.0, unassigned.0, upcoming.0
    ))
}
