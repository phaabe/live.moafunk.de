//! Telegram bot for admin control of the UN/HEARD backend.
//!
//! Provides commands to list artists/shows, trigger AI generation,
//! preview and publish Instagram posts, and check stream status.
//!
//! The bot runs as a long-polling task alongside the HTTP server.
//! It is disabled (no-op) when `TELEGRAM_BOT_TOKEN` is not set.

use crate::{ai, instagram, models, storage, telegram_notify, video, AppError, AppState};
use std::sync::Arc;
use teloxide::{
    dispatching::Dispatcher,
    net::Download,
    prelude::*,
    types::{
        CallbackQuery, ForceReply, InputFile, InputMedia, InputMediaPhoto,
        MaybeInaccessibleMessage, MessageId, ThreadId,
    },
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
    #[command(description = "<show_id> — preview show IG post on Telegram")]
    PreviewShowInstagram(i64),
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

    let handler = dptree::entry()
        .branch(Update::filter_callback_query().endpoint(handle_callback_query))
        .branch(
            Update::filter_message()
                .branch(dptree::entry().filter_command::<Command>().endpoint(answer))
                .branch(
                    // Catch-all: handle edit session replies, ignore everything else
                    dptree::entry().endpoint(handle_non_command_message),
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
        Command::PreviewShowInstagram(id) => {
            send_msg(bot, chat_id, thread_id, "📱 Sending show preview…").await?;
            match telegram_notify::send_show_instagram_preview(state, id).await {
                Ok(()) => {
                    send_msg(
                        bot,
                        chat_id,
                        thread_id,
                        "✅ Preview sent with publish/edit buttons.",
                    )
                    .await?;
                }
                Err(e) => {
                    send_msg(bot, chat_id, thread_id, &format!("❌ Preview failed: {e}")).await?;
                }
            }
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
         WHERE asa.show_id = ? ORDER BY asa.sort_order, a.name COLLATE NOCASE",
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
        // Update instagram_posted_at and permalink in DB
        let _ = sqlx::query(
            "UPDATE shows SET instagram_posted_at = datetime('now'), instagram_post_url = ? WHERE id = ?",
        )
        .bind(&result.permalink)
        .bind(id)
        .execute(&state.db)
        .await;

        // Prompt for artist post order after successful publish
        if let Err(e) = telegram_notify::send_sort_order_prompt(state, id).await {
            tracing::warn!("Failed to send sort order prompt for show {id}: {e}");
        }

        let link_info = result
            .permalink
            .as_deref()
            .map(|url| format!("\n🔗 {url}"))
            .unwrap_or_default();
        Ok(format!(
            "✅ Published {} to Instagram ({})\nMedia ID: {}{}",
            show.title,
            account,
            result.media_id.unwrap_or_default(),
            link_info,
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

// ============================================================================
// Callback query handler (inline keyboard buttons)
// ============================================================================

/// Handle inline keyboard button presses (callback queries).
///
/// Dispatches based on callback_data prefix:
/// - `ig_publish:{show_id}` — publish show to Instagram
/// - `ig_edit:{show_id}` — edit mode (placeholder)
async fn handle_callback_query(bot: Bot, q: CallbackQuery, state: Arc<AppState>) -> HandlerResult {
    let data = match q.data.as_deref() {
        Some(d) => d,
        None => return Ok(()),
    };

    // Auth: only respond to callbacks from the admin chat
    if let Some(admin_id) = state.config.telegram_admin_chat_id {
        if let Some(ref msg) = q.message {
            if msg.chat().id.0 != admin_id {
                bot.answer_callback_query(&q.id)
                    .text("⛔ Unauthorized")
                    .await?;
                return Ok(());
            }
        }
    }

    if let Some(id_str) = data.strip_prefix("ig_publish:") {
        let show_id: i64 = match id_str.parse() {
            Ok(id) => id,
            Err(_) => {
                bot.answer_callback_query(&q.id)
                    .text("❌ Invalid show ID")
                    .await?;
                return Ok(());
            }
        };

        // Acknowledge the button press
        bot.answer_callback_query(&q.id)
            .text("📸 Publishing to Instagram…")
            .await?;

        // Immediately remove buttons and show "please wait" to prevent double-clicks
        if let Some(MaybeInaccessibleMessage::Regular(ref msg)) = q.message {
            let _ = bot.edit_message_reply_markup(msg.chat.id, msg.id).await;
            if let Some(current_caption) = msg.caption() {
                let progress = telegram_notify::truncate_caption(
                    &format!("{current_caption}\n\n⏳ Publishing to Instagram…"),
                    1024,
                );
                let _ = bot
                    .edit_message_caption(msg.chat.id, msg.id)
                    .caption(progress)
                    .await;
            }
        }

        // Fetch show and publish
        let result = cmd_post_show_instagram(&state, show_id).await;

        // Update the message caption to show the result
        if let Some(MaybeInaccessibleMessage::Regular(msg)) = q.message {
            let status_text = match &result {
                Ok(text) => text.clone(),
                Err(e) => format!("❌ Error: {e}"),
            };

            // Replace "Publishing…" with final status
            if let Some(current_caption) = msg.caption() {
                // Remove the temporary "⏳ Publishing…" line if present
                let base_caption = current_caption
                    .trim_end_matches("\n\n⏳ Publishing to Instagram…")
                    .to_string();
                let new_caption = telegram_notify::truncate_caption(
                    &format!("{base_caption}\n\n{status_text}"),
                    1024,
                );
                let _ = bot
                    .edit_message_caption(msg.chat.id, msg.id)
                    .caption(new_caption)
                    .await;
            }
        }
    } else if let Some(id_str) = data.strip_prefix("ig_edit:") {
        let show_id: i64 = match id_str.parse() {
            Ok(id) => id,
            Err(_) => {
                bot.answer_callback_query(&q.id)
                    .text("❌ Invalid show ID")
                    .await?;
                return Ok(());
            }
        };

        // Show edit sub-menu: Caption / Image / Cancel
        bot.answer_callback_query(&q.id).await?;

        if let Some(MaybeInaccessibleMessage::Regular(msg)) = q.message {
            let edit_keyboard = serde_json::json!({
                "inline_keyboard": [[
                    { "text": "📝 Caption", "callback_data": format!("ig_edit_caption:{show_id}") },
                    { "text": "🖼️ Image", "callback_data": format!("ig_edit_image:{show_id}") },
                    { "text": "❌ Cancel", "callback_data": format!("ig_edit_cancel:{show_id}") }
                ]]
            });

            let _ = bot
                .edit_message_reply_markup(msg.chat.id, msg.id)
                .reply_markup(serde_json::from_value(edit_keyboard).unwrap())
                .await;
        }
    } else if let Some(id_str) = data.strip_prefix("ig_edit_caption:") {
        handle_edit_caption_callback(&bot, &q, &state, id_str).await?;
    } else if let Some(id_str) = data.strip_prefix("ig_edit_image:") {
        handle_edit_image_callback(&bot, &q, &state, id_str).await?;
    } else if let Some(id_str) = data.strip_prefix("ig_edit_cancel:") {
        handle_edit_cancel_callback(&bot, &q, &state, id_str).await?;

    // ── Artist Instagram preview callbacks ──
    } else if let Some(id_str) = data.strip_prefix("aig_pub:") {
        handle_aig_pub(&bot, &q, &state, id_str).await?;
    } else if let Some(id_str) = data.strip_prefix("aig_cap:") {
        handle_aig_cap(&bot, &q, &state, id_str).await?;
    } else if let Some(id_str) = data.strip_prefix("aig_img:") {
        handle_aig_img(&bot, &q, &state, id_str).await?;
    } else if let Some(rest) = data.strip_prefix("aig_vid:") {
        handle_aig_vid(&bot, &q, &state, rest).await?;
    } else if let Some(rest) = data.strip_prefix("aig_sort:") {
        handle_aig_sort(&bot, &q, &state, rest).await?;
    } else if data.starts_with("aig_sort_noop:") {
        // No-op: clicking the artist name label does nothing
        bot.answer_callback_query(&q.id).await?;
    } else {
        bot.answer_callback_query(&q.id)
            .text("Unknown action")
            .await?;
    }

    Ok(())
}

// ────────────────────────────────────────────────────────────────────────
// Edit mode handlers
// ────────────────────────────────────────────────────────────────────────

/// Build the standard Publish / Edit inline keyboard for a show preview.
fn preview_keyboard(show_id: i64) -> teloxide::types::InlineKeyboardMarkup {
    serde_json::from_value(serde_json::json!({
        "inline_keyboard": [[
            { "text": "📸 Publish to Instagram", "callback_data": format!("ig_publish:{show_id}") },
            { "text": "✏️ Edit", "callback_data": format!("ig_edit:{show_id}") }
        ]]
    }))
    .unwrap()
}

/// Handle "📝 Caption" button — start a caption edit session.
async fn handle_edit_caption_callback(
    bot: &Bot,
    q: &CallbackQuery,
    state: &Arc<AppState>,
    id_str: &str,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let show_id: i64 = id_str.parse().map_err(|_| "invalid show ID")?;
    bot.answer_callback_query(&q.id).await?;

    if let Some(MaybeInaccessibleMessage::Regular(msg)) = &q.message {
        let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
            .bind(show_id)
            .fetch_optional(&state.db)
            .await?
            .ok_or_else(|| format!("Show {show_id} not found"))?;

        // Store edit session
        {
            let mut sessions = state.telegram_edit_sessions.lock().await;
            sessions.insert(
                msg.chat.id.0,
                models::TelegramEditSession {
                    show_id,
                    artist_id: None,
                    preview_chat_id: msg.chat.id.0,
                    preview_message_id: msg.id.0,
                    field: models::TelegramEditField::Caption,
                    track_number: None,
                    video_msg_id: None,
                },
            );
        }

        // Send ForceReply prompt
        let prompt = format!(
            "📝 Reply to this message with the new caption for *{}*.",
            show.title
        );
        let mut req = bot.send_message(msg.chat.id, &prompt);
        req = req.reply_markup(teloxide::types::ReplyMarkup::ForceReply(ForceReply::new()));
        if let Some(tid) = state.config.telegram_topic_id {
            req = req.message_thread_id(ThreadId(teloxide::types::MessageId(tid)));
        }
        req.await?;
    }
    Ok(())
}

/// Handle "🖼️ Image" button — start an image edit session.
async fn handle_edit_image_callback(
    bot: &Bot,
    q: &CallbackQuery,
    state: &Arc<AppState>,
    id_str: &str,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let show_id: i64 = id_str.parse().map_err(|_| "invalid show ID")?;
    bot.answer_callback_query(&q.id).await?;

    if let Some(MaybeInaccessibleMessage::Regular(msg)) = &q.message {
        let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
            .bind(show_id)
            .fetch_optional(&state.db)
            .await?
            .ok_or_else(|| format!("Show {show_id} not found"))?;

        // Store edit session
        {
            let mut sessions = state.telegram_edit_sessions.lock().await;
            sessions.insert(
                msg.chat.id.0,
                models::TelegramEditSession {
                    show_id,
                    artist_id: None,
                    preview_chat_id: msg.chat.id.0,
                    preview_message_id: msg.id.0,
                    field: models::TelegramEditField::Image,
                    track_number: None,
                    video_msg_id: None,
                },
            );
        }

        // Send ForceReply prompt
        let prompt = format!(
            "🖼️ Reply to this message with the new cover image for *{}*.",
            show.title
        );
        let mut req = bot.send_message(msg.chat.id, &prompt);
        req = req.reply_markup(teloxide::types::ReplyMarkup::ForceReply(ForceReply::new()));
        if let Some(tid) = state.config.telegram_topic_id {
            req = req.message_thread_id(ThreadId(teloxide::types::MessageId(tid)));
        }
        req.await?;
    }
    Ok(())
}

/// Handle "❌ Cancel" button — clear session, restore original keyboard.
async fn handle_edit_cancel_callback(
    bot: &Bot,
    q: &CallbackQuery,
    state: &Arc<AppState>,
    id_str: &str,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let show_id: i64 = id_str.parse().map_err(|_| "invalid show ID")?;
    bot.answer_callback_query(&q.id)
        .text("✅ Edit cancelled")
        .await?;

    // Clear any pending session for this chat
    if let Some(MaybeInaccessibleMessage::Regular(msg)) = &q.message {
        {
            let mut sessions = state.telegram_edit_sessions.lock().await;
            sessions.remove(&msg.chat.id.0);
        }
        // Restore Publish/Edit keyboard
        let _ = bot
            .edit_message_reply_markup(msg.chat.id, msg.id)
            .reply_markup(preview_keyboard(show_id))
            .await;
    }
    Ok(())
}

// ────────────────────────────────────────────────────────────────────────
// Artist Instagram preview callback handlers (aig_*)
// ────────────────────────────────────────────────────────────────────────

/// Handle "📤 Publish" button — post artist to Instagram.
async fn handle_aig_pub(
    bot: &Bot,
    q: &CallbackQuery,
    state: &Arc<AppState>,
    id_str: &str,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let artist_id: i64 = id_str.parse().map_err(|_| "invalid artist ID")?;

    bot.answer_callback_query(&q.id)
        .text("📤 Publishing to Instagram…")
        .await?;

    let artist: models::Artist = sqlx::query_as("SELECT * FROM artists WHERE id = ?")
        .bind(artist_id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| format!("Artist {artist_id} not found"))?;

    // Remove buttons and show progress
    let token = state.config.telegram_bot_token.as_deref().unwrap_or_default();
    let chat_id = state.config.telegram_admin_chat_id.unwrap_or_default();

    if let Some(preview_msg_id) = artist.telegram_preview_message_id {
        let caption = artist.instagram_caption.as_deref().unwrap_or("");
        let progress_caption = telegram_notify::truncate_caption(
            &format!("{caption}\n\n⏳ Publishing to Instagram…"),
            1024,
        );
        let _ = telegram_notify::edit_message_caption_raw(
            token, chat_id, preview_msg_id, &progress_caption, "", None,
        )
        .await;
    }

    // Publish
    let account = state.config.telegram_instagram_account();
    let result = instagram::post_artist_to_instagram(state, &artist, account).await;

    match result {
        Ok(ref post_result) if post_result.success => {
            // Update DB
            let now = chrono::Utc::now().to_rfc3339();
            let _ = sqlx::query("UPDATE artists SET instagram_posted_at = ? WHERE id = ?")
                .bind(&now)
                .bind(artist_id)
                .execute(&state.db)
                .await;

            // Update caption to show success
            if let Some(preview_msg_id) = artist.telegram_preview_message_id {
                let caption = artist.instagram_caption.as_deref().unwrap_or("");
                let success_caption = telegram_notify::truncate_caption(
                    &format!("{caption}\n\n✅ Published to Instagram!"),
                    1024,
                );
                let _ = telegram_notify::edit_message_caption_raw(
                    token, chat_id, preview_msg_id, &success_caption, "", None,
                )
                .await;
            }
        }
        Ok(ref post_result) => {
            let err_msg = post_result.error.as_deref().unwrap_or("Unknown error");
            tracing::error!("Instagram post failed for artist {artist_id}: {err_msg}");
            // Restore buttons with error
            if let Some(preview_msg_id) = artist.telegram_preview_message_id {
                let caption = artist.instagram_caption.as_deref().unwrap_or("");
                let error_caption = telegram_notify::truncate_caption(
                    &format!("{caption}\n\n❌ Error: {err_msg}"),
                    1024,
                );
                let markup = telegram_notify::build_artist_preview_keyboard(
                    artist_id,
                    artist.track1_video_key.is_some(),
                    artist.track2_video_key.is_some(),
                );
                let _ = telegram_notify::edit_message_caption_raw(
                    token, chat_id, preview_msg_id, &error_caption, "", Some(&markup),
                )
                .await;
            }
        }
        Err(e) => {
            tracing::error!("Instagram post error for artist {artist_id}: {e}");
            // Restore buttons with error
            if let Some(preview_msg_id) = artist.telegram_preview_message_id {
                let caption = artist.instagram_caption.as_deref().unwrap_or("");
                let error_caption = telegram_notify::truncate_caption(
                    &format!("{caption}\n\n❌ Error: {e}"),
                    1024,
                );
                let markup = telegram_notify::build_artist_preview_keyboard(
                    artist_id,
                    artist.track1_video_key.is_some(),
                    artist.track2_video_key.is_some(),
                );
                let _ = telegram_notify::edit_message_caption_raw(
                    token, chat_id, preview_msg_id, &error_caption, "", Some(&markup),
                )
                .await;
            }
        }
    }

    Ok(())
}

/// Handle "✏️ Caption" button — start an artist caption edit session.
async fn handle_aig_cap(
    bot: &Bot,
    q: &CallbackQuery,
    state: &Arc<AppState>,
    id_str: &str,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let artist_id: i64 = id_str.parse().map_err(|_| "invalid artist ID")?;
    bot.answer_callback_query(&q.id).await?;

    if let Some(MaybeInaccessibleMessage::Regular(msg)) = &q.message {
        // Load artist name for the prompt
        let name: String = sqlx::query_scalar("SELECT name FROM artists WHERE id = ?")
            .bind(artist_id)
            .fetch_optional(&state.db)
            .await?
            .unwrap_or_else(|| format!("Artist {artist_id}"));

        // Store edit session
        {
            let mut sessions = state.telegram_edit_sessions.lock().await;
            sessions.insert(
                msg.chat.id.0,
                models::TelegramEditSession {
                    show_id: 0,
                    artist_id: Some(artist_id),
                    preview_chat_id: msg.chat.id.0,
                    preview_message_id: msg.id.0,
                    field: models::TelegramEditField::Caption,
                    track_number: None,
                    video_msg_id: None,
                },
            );
        }

        // Send ForceReply prompt
        let prompt = format!("📝 Reply with the new caption for *{name}*.");
        let mut req = bot.send_message(msg.chat.id, &prompt);
        req = req.reply_markup(teloxide::types::ReplyMarkup::ForceReply(ForceReply::new()));
        if let Some(tid) = state.config.telegram_topic_id {
            req = req.message_thread_id(ThreadId(MessageId(tid)));
        }
        req.await?;
    }

    Ok(())
}

/// Handle "🖼 Image" button — start an artist image replacement session.
async fn handle_aig_img(
    bot: &Bot,
    q: &CallbackQuery,
    state: &Arc<AppState>,
    id_str: &str,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let artist_id: i64 = id_str.parse().map_err(|_| "invalid artist ID")?;
    bot.answer_callback_query(&q.id).await?;

    if let Some(MaybeInaccessibleMessage::Regular(msg)) = &q.message {
        let name: String = sqlx::query_scalar("SELECT name FROM artists WHERE id = ?")
            .bind(artist_id)
            .fetch_optional(&state.db)
            .await?
            .unwrap_or_else(|| format!("Artist {artist_id}"));

        // Store edit session
        {
            let mut sessions = state.telegram_edit_sessions.lock().await;
            sessions.insert(
                msg.chat.id.0,
                models::TelegramEditSession {
                    show_id: 0,
                    artist_id: Some(artist_id),
                    preview_chat_id: msg.chat.id.0,
                    preview_message_id: msg.id.0,
                    field: models::TelegramEditField::Image,
                    track_number: None,
                    video_msg_id: None,
                },
            );
        }

        // Send ForceReply prompt
        let prompt = format!("🖼 Reply with a new photo for *{name}*.");
        let mut req = bot.send_message(msg.chat.id, &prompt);
        req = req.reply_markup(teloxide::types::ReplyMarkup::ForceReply(ForceReply::new()));
        if let Some(tid) = state.config.telegram_topic_id {
            req = req.message_thread_id(ThreadId(MessageId(tid)));
        }
        req.await?;
    }

    Ok(())
}

/// Handle "🎬 Video N" button — start a timecode session for video regeneration.
///
/// `rest` format: `{artist_id}:{track_number}` e.g. `42:1`
async fn handle_aig_vid(
    bot: &Bot,
    q: &CallbackQuery,
    state: &Arc<AppState>,
    rest: &str,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let parts: Vec<&str> = rest.splitn(2, ':').collect();
    if parts.len() != 2 {
        bot.answer_callback_query(&q.id)
            .text("❌ Invalid callback data")
            .await?;
        return Ok(());
    }
    let artist_id: i64 = parts[0].parse().map_err(|_| "invalid artist ID")?;
    let track_num: u8 = parts[1].parse().map_err(|_| "invalid track number")?;

    if track_num != 1 && track_num != 2 {
        bot.answer_callback_query(&q.id)
            .text("❌ Invalid track number")
            .await?;
        return Ok(());
    }

    let artist: models::Artist = sqlx::query_as("SELECT * FROM artists WHERE id = ?")
        .bind(artist_id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| format!("Artist {artist_id} not found"))?;

    let vid_msg_id = if track_num == 1 {
        artist.telegram_video1_message_id
    } else {
        artist.telegram_video2_message_id
    };

    if vid_msg_id.is_none() {
        bot.answer_callback_query(&q.id)
            .text("❌ No video message found for this track")
            .await?;
        return Ok(());
    }

    bot.answer_callback_query(&q.id).await?;

    if let Some(MaybeInaccessibleMessage::Regular(msg)) = &q.message {
        // Store edit session
        {
            let mut sessions = state.telegram_edit_sessions.lock().await;
            sessions.insert(
                msg.chat.id.0,
                models::TelegramEditSession {
                    show_id: 0,
                    artist_id: Some(artist_id),
                    preview_chat_id: msg.chat.id.0,
                    preview_message_id: msg.id.0,
                    field: models::TelegramEditField::Timecode,
                    track_number: Some(track_num),
                    video_msg_id: vid_msg_id,
                },
            );
        }

        // Send ForceReply prompt
        let prompt = format!(
            "🎬 Reply with a start timecode for track {track_num} (e.g. `1:30` or `90` for seconds, `0` for beginning)."
        );
        let mut req = bot.send_message(msg.chat.id, &prompt);
        req = req.reply_markup(teloxide::types::ReplyMarkup::ForceReply(ForceReply::new()));
        if let Some(tid) = state.config.telegram_topic_id {
            req = req.message_thread_id(ThreadId(MessageId(tid)));
        }
        req.await?;
    }

    Ok(())
}

/// Parse a timecode string into seconds.
///
/// Supports formats:
/// - `"1:30"` → 90
/// - `"0:45"` → 45
/// - `"90"` → 90
/// - `"0"` → 0
pub fn parse_timecode(input: &str) -> Result<u32, String> {
    let input = input.trim();
    if let Some((min_str, sec_str)) = input.split_once(':') {
        let minutes: u32 = min_str
            .parse()
            .map_err(|_| format!("Invalid minutes: '{min_str}'"))?;
        let seconds: u32 = sec_str
            .parse()
            .map_err(|_| format!("Invalid seconds: '{sec_str}'"))?;
        if seconds >= 60 {
            return Err(format!("Seconds must be 0-59, got {seconds}"));
        }
        Ok(minutes * 60 + seconds)
    } else {
        input
            .parse::<u32>()
            .map_err(|_| format!("Invalid timecode: '{input}'. Use M:SS or plain seconds."))
    }
}

/// Handle ⬆️/⬇️ sort order reorder callback.
///
/// Callback data format: `aig_sort:{show_id}:{artist_id}:{direction}`
/// where direction is "up" or "down".
async fn handle_aig_sort(
    bot: &Bot,
    q: &CallbackQuery,
    state: &Arc<AppState>,
    rest: &str,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let parts: Vec<&str> = rest.splitn(3, ':').collect();
    if parts.len() != 3 {
        bot.answer_callback_query(&q.id)
            .text("❌ Invalid sort callback data")
            .await?;
        return Ok(());
    }
    let show_id: i64 = parts[0].parse().map_err(|_| "invalid show ID")?;
    let artist_id: i64 = parts[1].parse().map_err(|_| "invalid artist ID")?;
    let direction = parts[2]; // "up" or "down"

    // Fetch all artists for this show in current sort order
    let artists: Vec<models::Artist> = sqlx::query_as(
        "SELECT a.* FROM artists a \
         INNER JOIN artist_show_assignments asa ON a.id = asa.artist_id \
         WHERE asa.show_id = ? ORDER BY asa.sort_order, a.name COLLATE NOCASE",
    )
    .bind(show_id)
    .fetch_all(&state.db)
    .await?;

    // Find position of the target artist
    let pos = match artists.iter().position(|a| a.id == artist_id) {
        Some(p) => p,
        None => {
            bot.answer_callback_query(&q.id)
                .text("❌ Artist not found in show")
                .await?;
            return Ok(());
        }
    };

    // Determine swap target
    let swap_pos = match direction {
        "up" if pos > 0 => pos - 1,
        "down" if pos < artists.len() - 1 => pos + 1,
        _ => {
            bot.answer_callback_query(&q.id)
                .text("⚠️ Can't move further")
                .await?;
            return Ok(());
        }
    };

    // Swap sort_order values
    let current_order = pos as i32;
    let swap_order = swap_pos as i32;
    crate::db::set_artist_sort_order(&state.db, show_id, artists[pos].id, swap_order).await?;
    crate::db::set_artist_sort_order(&state.db, show_id, artists[swap_pos].id, current_order)
        .await?;

    // Re-fetch for the updated order
    let artists: Vec<models::Artist> = sqlx::query_as(
        "SELECT a.* FROM artists a \
         INNER JOIN artist_show_assignments asa ON a.id = asa.artist_id \
         WHERE asa.show_id = ? ORDER BY asa.sort_order, a.name COLLATE NOCASE",
    )
    .bind(show_id)
    .fetch_all(&state.db)
    .await?;

    let artist_pairs: Vec<(i64, String)> = artists.iter().map(|a| (a.id, a.name.clone())).collect();
    let keyboard = telegram_notify::build_sort_order_keyboard(show_id, &artist_pairs);

    // Fetch show title for the message text
    let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(show_id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| format!("Show {show_id} not found"))?;

    let text = format!(
        "📋 Set artist post order for {}\n\n\
         Day 1 after show → first artist, Day 2 → second, etc.",
        show.title
    );

    // Edit the sort order message in-place
    if let Some(MaybeInaccessibleMessage::Regular(msg)) = &q.message {
        let token = state
            .config
            .telegram_bot_token
            .as_deref()
            .ok_or("Telegram bot token not configured")?;
        let _ = telegram_notify::edit_message_text_raw(
            token,
            msg.chat.id.0,
            msg.id.0 as i64,
            &text,
            Some(&keyboard),
        )
        .await;
    }

    bot.answer_callback_query(&q.id).text("✅ Reordered").await?;
    Ok(())
}

/// Handle non-command messages: check for active edit sessions.
///
/// If an edit session is active for this chat, the message is treated as
/// the new caption (text) or new image (photo). Otherwise, silently ignored.
async fn handle_non_command_message(bot: Bot, msg: Message, state: Arc<AppState>) -> HandlerResult {
    let chat_id = msg.chat.id.0;

    // Check for active edit session
    let session = {
        let sessions = state.telegram_edit_sessions.lock().await;
        sessions.get(&chat_id).cloned()
    };

    let session = match session {
        Some(s) => {
            tracing::info!("Edit session found for chat {chat_id}: field={:?}, artist_id={:?}", s.field, s.artist_id);
            s
        }
        None => return Ok(()), // No active session — silently ignore
    };

    match session.field {
        models::TelegramEditField::Caption => {
            let new_caption = match msg.text() {
                Some(text) => text.to_string(),
                None => {
                    let mut req = bot.send_message(
                        msg.chat.id,
                        "❌ Please send a text message for the caption.",
                    );
                    if let Some(tid) = state.config.telegram_topic_id {
                        req = req.message_thread_id(ThreadId(MessageId(tid)));
                    }
                    req.await?;
                    return Ok(());
                }
            };

            if let Some(artist_id) = session.artist_id {
                // ── Artist-level caption edit ──
                let _ = sqlx::query(
                    "UPDATE artists SET instagram_caption = ?, updated_at = datetime('now') WHERE id = ?",
                )
                .bind(&new_caption)
                .bind(artist_id)
                .execute(&state.db)
                .await;

                // Rebuild keyboard from artist
                let artist: models::Artist =
                    sqlx::query_as("SELECT * FROM artists WHERE id = ?")
                        .bind(artist_id)
                        .fetch_optional(&state.db)
                        .await?
                        .ok_or_else(|| format!("Artist {artist_id} not found"))?;

                let markup = telegram_notify::build_artist_preview_keyboard(
                    artist_id,
                    artist.track1_video_key.is_some(),
                    artist.track2_video_key.is_some(),
                );

                // Update preview message caption via raw API
                if let Some(token) = state.config.telegram_bot_token.as_deref() {
                    let truncated = telegram_notify::truncate_caption(&new_caption, 1024);
                    let _ = telegram_notify::edit_message_caption_raw(
                        token,
                        session.preview_chat_id,
                        session.preview_message_id as i64,
                        &truncated,
                        "",
                        Some(&markup),
                    )
                    .await;
                }
            } else {
                // ── Show-level caption edit ──
                let _ = sqlx::query("UPDATE shows SET ai_bio = ? WHERE id = ?")
                    .bind(&new_caption)
                    .bind(session.show_id)
                    .execute(&state.db)
                    .await;

                let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
                    .bind(session.show_id)
                    .fetch_optional(&state.db)
                    .await?
                    .ok_or_else(|| format!("Show {} not found", session.show_id))?;

                let full_caption = instagram::build_show_caption(&state, &show)
                    .await
                    .map_err(|e| format!("Failed to build caption: {e}"))?;

                let truncated = telegram_notify::truncate_caption(&full_caption, 1024);
                let preview_chat = ChatId(session.preview_chat_id);
                let preview_msg = MessageId(session.preview_message_id);
                let _ = bot
                    .edit_message_caption(preview_chat, preview_msg)
                    .caption(&truncated)
                    .reply_markup(preview_keyboard(session.show_id))
                    .await;
            }

            // Clear session
            {
                let mut sessions = state.telegram_edit_sessions.lock().await;
                sessions.remove(&chat_id);
            }

            let mut req = bot.send_message(msg.chat.id, "✅ Caption updated.");
            if let Some(tid) = state.config.telegram_topic_id {
                req = req.message_thread_id(ThreadId(MessageId(tid)));
            }
            req.await?;
        }
        models::TelegramEditField::Image => {
            // Get the largest photo from the message
            let photo = match msg.photo() {
                Some(photos) => photos.last().unwrap(), // last = largest resolution
                None => {
                    let mut req = bot.send_message(msg.chat.id, "❌ Please send a photo.");
                    if let Some(tid) = state.config.telegram_topic_id {
                        req = req.message_thread_id(ThreadId(MessageId(tid)));
                    }
                    req.await?;
                    return Ok(());
                }
            };

            // Download the photo from Telegram
            let file = bot.get_file(&photo.file.id).await?;
            let mut photo_bytes: Vec<u8> = Vec::new();
            bot.download_file(&file.path, &mut photo_bytes).await?;

            if let Some(artist_id) = session.artist_id {
                // ── Artist-level image replacement ──
                let key = format!("artists/{artist_id}/overlay/telegram_upload.jpg");

                state
                    .s3_client
                    .put_object()
                    .bucket(&state.config.r2_bucket_name)
                    .key(&key)
                    .body(aws_sdk_s3::primitives::ByteStream::from(photo_bytes.clone()))
                    .content_type("image/jpeg")
                    .send()
                    .await
                    .map_err(|e| format!("R2 upload failed: {e}"))?;

                let _ = sqlx::query(
                    "UPDATE artists SET pic_overlay_key = ?, updated_at = datetime('now') WHERE id = ?",
                )
                .bind(&key)
                .bind(artist_id)
                .execute(&state.db)
                .await;

                // Rebuild keyboard
                let artist: models::Artist =
                    sqlx::query_as("SELECT * FROM artists WHERE id = ?")
                        .bind(artist_id)
                        .fetch_optional(&state.db)
                        .await?
                        .ok_or_else(|| format!("Artist {artist_id} not found"))?;

                let markup = telegram_notify::build_artist_preview_keyboard(
                    artist_id,
                    artist.track1_video_key.is_some(),
                    artist.track2_video_key.is_some(),
                );

                let caption = artist
                    .instagram_caption
                    .as_deref()
                    .unwrap_or("(no caption)");
                let truncated = telegram_notify::truncate_caption(caption, 1024);

                // Edit the preview message with the new photo
                if let Some(token) = state.config.telegram_bot_token.as_deref() {
                    let _ = telegram_notify::edit_message_media_raw(
                        token,
                        session.preview_chat_id,
                        session.preview_message_id as i64,
                        photo_bytes,
                        "artist_preview.jpg".to_string(),
                        "photo",
                        &truncated,
                        "",
                        Some(&markup),
                    )
                    .await;
                }
            } else {
                // ── Show-level image replacement ──
                storage::upload_show_cover(&state, session.show_id, photo_bytes.clone()).await?;

                let now = chrono::Utc::now().to_rfc3339();
                let _ = sqlx::query("UPDATE shows SET cover_generated_at = ? WHERE id = ?")
                    .bind(&now)
                    .bind(session.show_id)
                    .execute(&state.db)
                    .await;

                let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
                    .bind(session.show_id)
                    .fetch_optional(&state.db)
                    .await?
                    .ok_or_else(|| format!("Show {} not found", session.show_id))?;

                let full_caption = instagram::build_show_caption(&state, &show)
                    .await
                    .map_err(|e| format!("Failed to build caption: {e}"))?;
                let truncated = telegram_notify::truncate_caption(&full_caption, 1024);

                let preview_chat = ChatId(session.preview_chat_id);
                let preview_msg = MessageId(session.preview_message_id);
                let new_media = InputMedia::Photo(
                    InputMediaPhoto::new(InputFile::memory(photo_bytes)).caption(&truncated),
                );
                let _ = bot
                    .edit_message_media(preview_chat, preview_msg, new_media)
                    .reply_markup(preview_keyboard(session.show_id))
                    .await;
            }

            // Clear session
            {
                let mut sessions = state.telegram_edit_sessions.lock().await;
                sessions.remove(&chat_id);
            }

            let mut req = bot.send_message(msg.chat.id, "✅ Image updated.");
            if let Some(tid) = state.config.telegram_topic_id {
                req = req.message_thread_id(ThreadId(MessageId(tid)));
            }
            req.await?;
        }
        models::TelegramEditField::Timecode => {
            tracing::info!("Timecode edit session active for chat {chat_id}, artist_id={:?}", session.artist_id);
            let input = match msg.text() {
                Some(text) => text.to_string(),
                None => {
                    let mut req = bot.send_message(
                        msg.chat.id,
                        "❌ Please send a timecode (e.g. 1:30 or 90).",
                    );
                    if let Some(tid) = state.config.telegram_topic_id {
                        req = req.message_thread_id(ThreadId(MessageId(tid)));
                    }
                    req.await?;
                    return Ok(());
                }
            };

            let offset_secs = match parse_timecode(&input) {
                Ok(secs) => {
                    tracing::info!("Parsed timecode '{input}' → {secs} seconds");
                    secs
                }
                Err(err_msg) => {
                    // Re-insert session so user can try again
                    let mut req = bot.send_message(
                        msg.chat.id,
                        format!("❌ {err_msg}"),
                    );
                    if let Some(tid) = state.config.telegram_topic_id {
                        req = req.message_thread_id(ThreadId(MessageId(tid)));
                    }
                    req.await?;
                    return Ok(());
                    // Session stays in the map (wasn't removed yet)
                }
            };

            let artist_id = match session.artist_id {
                Some(id) => id,
                None => {
                    let mut sessions = state.telegram_edit_sessions.lock().await;
                    sessions.remove(&chat_id);
                    return Ok(());
                }
            };

            let track_num = session.track_number.unwrap_or(1);
            let video_msg_id = session.video_msg_id;

            // Remove session before processing (may take a while)
            {
                let mut sessions = state.telegram_edit_sessions.lock().await;
                sessions.remove(&chat_id);
            }

            // Send "generating" message
            let timecode_display = if offset_secs >= 60 {
                format!("{}:{:02}", offset_secs / 60, offset_secs % 60)
            } else {
                format!("0:{:02}", offset_secs)
            };
            let mut req = bot.send_message(
                msg.chat.id,
                format!("⏳ Regenerating video from {timecode_display}..."),
            );
            if let Some(tid) = state.config.telegram_topic_id {
                req = req.message_thread_id(ThreadId(MessageId(tid)));
            }
            req.await?;

            // Run the heavy video work inside a closure that returns Result
            // so we can report errors to the user instead of silently failing
            let result: Result<String, String> = async {
                // Load artist to get image and track keys
                let artist: models::Artist =
                    sqlx::query_as("SELECT * FROM artists WHERE id = ?")
                        .bind(artist_id)
                        .fetch_optional(&state.db)
                        .await
                        .map_err(|e| format!("DB error: {e}"))?
                        .ok_or_else(|| format!("Artist {artist_id} not found"))?;

                let image_key = artist
                    .pic_overlay_key
                    .as_ref()
                    .or(artist.pic_cropped_key.as_ref())
                    .or(artist.pic_key.as_ref())
                    .ok_or_else(|| format!("Artist {artist_id} has no image"))?;

                let track_key = if track_num == 1 {
                    artist.track1_key.as_ref()
                } else {
                    artist.track2_key.as_ref()
                }
                .ok_or_else(|| format!("Artist {artist_id} has no track {track_num}"))?;

                tracing::info!(
                    "Generating video for artist {artist_id} track {track_num} offset={offset_secs}s image={image_key} track={track_key}"
                );

                // Generate video with new offset
                let video_bytes = video::generate_track_preview_video(
                    &state,
                    image_key,
                    track_key,
                    "",
                    30,
                    offset_secs,
                )
                .await
                .map_err(|e| format!("Video generation failed: {e}"))?;

                tracing::info!("Video generated: {} bytes", video_bytes.len());

                // Upload to R2 (overwrite existing video)
                let video_r2_key = format!("artists/{artist_id}/track{track_num}_video/preview.mp4");
                state
                    .s3_client
                    .put_object()
                    .bucket(&state.config.r2_bucket_name)
                    .key(&video_r2_key)
                    .body(aws_sdk_s3::primitives::ByteStream::from(video_bytes.clone()))
                    .content_type("video/mp4")
                    .send()
                    .await
                    .map_err(|e| format!("R2 upload failed: {e}"))?;

                // Edit the video message in-place if we have the message ID
                if let (Some(token), Some(vid_mid)) =
                    (state.config.telegram_bot_token.as_deref(), video_msg_id)
                {
                    let track_name = if track_num == 1 {
                        &artist.track1_name
                    } else {
                        &artist.track2_name
                    };
                    let caption = format!("🎵 Track {track_num}: {track_name}");
                    match telegram_notify::edit_message_media_raw(
                        token,
                        session.preview_chat_id,
                        vid_mid,
                        video_bytes,
                        "preview.mp4".to_string(),
                        "video",
                        &caption,
                        "",
                        None,
                    )
                    .await
                    {
                        Ok(_) => tracing::info!("Video message {vid_mid} replaced in chat {}", session.preview_chat_id),
                        Err(e) => {
                            tracing::error!("Failed to replace video message: {e}");
                            return Err(format!("Video generated & uploaded, but failed to replace message: {e}"));
                        }
                    }
                } else {
                    tracing::warn!("No bot token or video_msg_id — cannot replace video message in chat");
                }

                Ok(format!("✅ Video {track_num} regenerated from {timecode_display}"))
            }
            .await;

            let reply_text = match result {
                Ok(msg_text) => msg_text,
                Err(err) => {
                    tracing::error!("Timecode video regeneration failed: {err}");
                    format!("❌ {err}")
                }
            };

            let mut req = bot.send_message(msg.chat.id, &reply_text);
            if let Some(tid) = state.config.telegram_topic_id {
                req = req.message_thread_id(ThreadId(MessageId(tid)));
            }
            req.await?;
        }
    }

    Ok(())
}
