//! The admin bot against a fake Telegram and real SQLite: command parsing,
//! the admin and topic checks, message texts and buttons, edit sessions.
//! Every message the bot posts must go to the MoafunkBot topic.

use super::*;
use crate::fake_r2::FakeR2;
use crate::fake_telegram::{telegram_state, FakeTelegram, ADMIN_CHAT, TOPIC};
use serde_json::{json, Value};

const BOT_NAME: &str = "MoafunkBot";
const OTHER_CHAT: i64 = -2002;

fn message(chat: i64, thread: Option<i32>, extra: Value) -> Message {
    let mut msg = json!({
        "message_id": 10, "date": 1_700_000_000,
        "chat": {"id": chat, "type": "supergroup", "title": "Group", "is_forum": true},
        "from": {"id": 5, "is_bot": false, "first_name": "Ada", "last_name": "L"},
    });
    if let Some(thread) = thread {
        msg["message_thread_id"] = json!(thread);
        msg["is_topic_message"] = json!(true);
    }
    msg.as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    serde_json::from_value(msg).unwrap()
}

fn text_message(chat: i64, thread: Option<i32>, text: &str) -> Message {
    message(chat, thread, json!({ "text": text }))
}

/// A button press on a photo preview with caption "Preview" in `chat`.
fn callback(chat: i64, data: &str) -> CallbackQuery {
    serde_json::from_value(json!({
        "id": "cb-1",
        "from": {"id": 5, "is_bot": false, "first_name": "Ada"},
        "chat_instance": "ci",
        "data": data,
        "message": {
            "message_id": 50, "date": 1_700_000_000,
            "chat": {"id": chat, "type": "supergroup", "title": "Group", "is_forum": true},
            "message_thread_id": TOPIC, "is_topic_message": true,
            "photo": [{"file_id": "p", "file_unique_id": "pu", "width": 1, "height": 1}],
            "caption": "Preview"
        }
    }))
    .unwrap()
}

fn bot(state: &Arc<AppState>) -> Bot {
    state.telegram_bot.clone().unwrap()
}

async fn command(state: &Arc<AppState>, chat: i64, thread: Option<i32>, input: &str) {
    let cmd = Command::parse(input, BOT_NAME).unwrap();
    answer(
        bot(state),
        text_message(chat, thread, input),
        cmd,
        state.clone(),
    )
    .await
    .unwrap();
}

/// Runs a command in the admin topic and returns the text of the last reply.
async fn reply(state: &Arc<AppState>, fake: &FakeTelegram, input: &str) -> String {
    fake.clear();
    command(state, ADMIN_CHAT, Some(TOPIC), input).await;
    fake.assert_all_sends_in_topic();
    fake.of("sendmessage").last().unwrap().text().to_string()
}

async fn press(state: &Arc<AppState>, chat: i64, data: &str) {
    handle_callback_query(bot(state), callback(chat, data), state.clone())
        .await
        .unwrap();
}

/// The text of the last answerCallbackQuery (the toast the admin sees).
fn toast(fake: &FakeTelegram) -> Option<String> {
    fake.of("answercallbackquery")
        .last()
        .unwrap()
        .str("text")
        .map(str::to_string)
}

async fn add_artist(state: &Arc<AppState>, name: &str) -> i64 {
    sqlx::query_scalar(
        "INSERT INTO artists (name, pronouns, track1_name, track2_name) \
         VALUES (?, 'they/them', 'One', 'Two') RETURNING id",
    )
    .bind(name)
    .fetch_one(&state.db)
    .await
    .unwrap()
}

/// `add_artist` with extra columns: `cols` and `values` are SQL lists.
async fn add_artist_with(state: &Arc<AppState>, name: &str, cols: &str, values: &str) -> i64 {
    sqlx::query_scalar(&format!(
        "INSERT INTO artists (name, pronouns, track1_name, track2_name, {cols}) \
         VALUES (?, 'they/them', 'One', 'Two', {values}) RETURNING id"
    ))
    .bind(name)
    .fetch_one(&state.db)
    .await
    .unwrap()
}

async fn add_show(state: &Arc<AppState>, title: &str, date: &str) -> i64 {
    sqlx::query_scalar("INSERT INTO shows (title, date) VALUES (?, ?) RETURNING id")
        .bind(title)
        .bind(date)
        .fetch_one(&state.db)
        .await
        .unwrap()
}

async fn assign(state: &Arc<AppState>, show: i64, artist: i64, order: i32) {
    sqlx::query(
        "INSERT INTO artist_show_assignments (artist_id, show_id, sort_order) VALUES (?, ?, ?)",
    )
    .bind(artist)
    .bind(show)
    .bind(order)
    .execute(&state.db)
    .await
    .unwrap();
}

async fn session(state: &Arc<AppState>) -> Option<models::TelegramEditSession> {
    state
        .telegram_edit_sessions
        .lock()
        .await
        .get(&ADMIN_CHAT)
        .cloned()
}

async fn start_session(
    state: &Arc<AppState>,
    artist: Option<i64>,
    field: models::TelegramEditField,
) {
    state.telegram_edit_sessions.lock().await.insert(
        ADMIN_CHAT,
        models::TelegramEditSession {
            show_id: 0,
            artist_id: artist,
            preview_chat_id: ADMIN_CHAT,
            preview_message_id: 77,
            field,
            track_number: Some(1),
            video_msg_id: Some(78),
        },
    );
}

// ── Commands ────────────────────────────────────────────────────────────────

#[test]
fn commands_parse_ids_text_and_bot_mentions() {
    let parse = |input| Command::parse(input, BOT_NAME);
    assert!(matches!(parse("/help"), Ok(Command::Help)));
    assert!(matches!(parse("/start"), Ok(Command::Start)));
    assert!(matches!(parse("/artist 42"), Ok(Command::Artist(42))));
    assert!(matches!(
        parse("/generate_bio@MoafunkBot 7"),
        Ok(Command::GenerateBio(7))
    ));
    assert!(matches!(
        parse("/post_show_instagram 3"),
        Ok(Command::PostShowInstagram(3))
    ));
    assert!(matches!(
        parse("/edit_caption 5 New caption text"),
        Ok(Command::EditCaption(args)) if args == "5 New caption text"
    ));

    // Bad ids, unknown commands and commands for another bot are not ours.
    for input in [
        "/artist abc",
        "/artist",
        "/show 1 2",
        "/unknown",
        "/artist@OtherBot 1",
    ] {
        assert!(parse(input).is_err(), "{input}");
    }

    let help = Command::descriptions().to_string();
    for name in [
        "/artists",
        "/edit_caption",
        "/preview_show_instagram",
        "/stream_status",
        "/stats",
    ] {
        assert!(help.contains(name), "{name} missing in help:\n{help}");
    }
}

#[test]
fn timecodes_accept_minutes_or_seconds() {
    assert_eq!(parse_timecode("1:30"), Ok(90));
    assert_eq!(parse_timecode(" 0:45 "), Ok(45));
    assert_eq!(parse_timecode("90"), Ok(90));
    assert_eq!(parse_timecode("0"), Ok(0));
    assert_eq!(
        parse_timecode("1:75"),
        Err("Seconds must be 0-59, got 75".to_string())
    );
    assert!(parse_timecode("a:10")
        .unwrap_err()
        .contains("Invalid minutes"));
    assert!(parse_timecode("-5")
        .unwrap_err()
        .contains("Invalid timecode"));
    assert!(parse_timecode("").is_err());
}

#[test]
fn preview_keyboard_has_publish_and_edit() {
    let keyboard = serde_json::to_value(preview_keyboard(9)).unwrap();
    assert_eq!(
        keyboard["inline_keyboard"][0][0]["callback_data"],
        "ig_publish:9"
    );
    assert_eq!(
        keyboard["inline_keyboard"][0][1]["callback_data"],
        "ig_edit:9"
    );
}

/// Only the admin group's MoafunkBot topic gets answers. Other chats get a
/// refusal, other topics of the admin group get nothing.
#[tokio::test]
async fn only_the_admin_topic_gets_answers() {
    let (state, _temp, fake) = telegram_state(json!({})).await;

    command(&state, OTHER_CHAT, None, "/stats").await;
    let calls = fake.calls();
    assert_eq!(calls.len(), 1, "{calls:?}");
    assert_eq!(calls[0].int("chat_id"), Some(OTHER_CHAT));
    assert_eq!(calls[0].text(), "⛔ Unauthorized");

    fake.clear();
    command(&state, ADMIN_CHAT, Some(99), "/help").await;
    command(&state, ADMIN_CHAT, None, "/help").await;
    assert!(fake.calls().is_empty(), "{:?}", fake.calls());

    let help = reply(&state, &fake, "/help").await;
    assert!(help.contains("/artists"), "{help}");

    // Without an admin chat the bot answers nobody.
    let (state, _temp, fake) = telegram_state(json!({ "telegram_admin_chat_id": null })).await;
    command(&state, ADMIN_CHAT, Some(TOPIC), "/help").await;
    command(&state, OTHER_CHAT, None, "/help").await;
    assert!(fake.calls().is_empty(), "{:?}", fake.calls());
}

#[tokio::test]
async fn list_and_detail_commands_show_status() {
    let (state, _temp, fake) = telegram_state(json!({})).await;
    assert_eq!(
        reply(&state, &fake, "/artists").await,
        "No unassigned artists."
    );
    assert_eq!(reply(&state, &fake, "/shows").await, "No upcoming shows.");

    let nova = add_artist_with(
        &state,
        "Nova",
        "pic_key, ai_bio, instagram, soundcloud, spotify",
        "'pic.jpg', 'Bio', '@nova', '', 'sp/nova'",
    )
    .await;
    let kai = add_artist(&state, "Kai").await;
    let show = add_show(&state, "Late Night", "2099-01-02").await;
    add_show(&state, "Past", "2000-01-01").await;
    assign(&state, show, kai, 0).await;

    let artists = reply(&state, &fake, "/artists").await;
    assert!(
        artists.starts_with("📋 Unassigned artists (1):"),
        "{artists}"
    );
    assert!(
        artists.contains(&format!("📷🤖·· [{nova}] Nova")),
        "{artists}"
    );
    assert!(!artists.contains("Kai"), "{artists}");

    let shows = reply(&state, &fake, "/shows").await;
    assert!(
        shows.contains(&format!("·· [{show}] 2099-01-02 — Late Night (1 artists)")),
        "{shows}"
    );
    assert!(!shows.contains("Past"), "{shows}");

    let detail = reply(&state, &fake, &format!("/artist {nova}")).await;
    assert!(
        detail.starts_with(&format!("🎤 Nova (ID: {nova})")),
        "{detail}"
    );
    assert!(detail.contains("📷 Picture: ✅"), "{detail}");
    assert!(detail.contains("🎬 Videos: ❌"), "{detail}");
    assert!(detail.contains("📸 Posted: not yet"), "{detail}");
    // Empty socials are left out.
    assert!(detail.contains("IG: @nova") && detail.contains("Spotify: sp/nova"));
    assert!(!detail.contains("SC:"), "{detail}");

    let detail = reply(&state, &fake, &format!("/show {show}")).await;
    assert!(detail.contains("Date: 2099-01-02"), "{detail}");
    assert!(
        detail.contains(&format!("Artists (1):\n  [{kai}] Kai")),
        "{detail}"
    );

    let stats = reply(&state, &fake, "/stats").await;
    assert!(
        stats.contains("🎤 Artists: 2 total, 1 unassigned"),
        "{stats}"
    );
    assert!(stats.contains("📅 Upcoming shows: 1"), "{stats}");
    assert!(stats.contains("📡 Inactive"), "{stats}");

    assert_eq!(
        reply(&state, &fake, "/stream_status").await,
        "📡 No active stream."
    );
}

#[tokio::test]
async fn edit_caption_command_checks_input() {
    let (state, _temp, fake) = telegram_state(json!({})).await;
    let nova = add_artist(&state, "Nova").await;

    let usage = reply(&state, &fake, "/edit_caption 5").await;
    assert!(usage.starts_with("❌ Error:") && usage.contains("Usage: /edit_caption"));
    let bad_id = reply(&state, &fake, "/edit_caption x new").await;
    assert!(bad_id.contains("Invalid artist ID"), "{bad_id}");
    let missing = reply(&state, &fake, "/edit_caption 999 new").await;
    assert!(missing.contains("Artist 999 not found"), "{missing}");
    let missing = reply(&state, &fake, "/artist 999").await;
    assert!(missing.starts_with("❌ Error:") && missing.contains("Artist 999 not found"));

    let long = "word ".repeat(30);
    let done = reply(&state, &fake, &format!("/edit_caption {nova} {long}")).await;
    assert!(done.starts_with(&format!("✅ Caption updated for artist {nova}.")));
    assert!(done.ends_with('…'), "{done}");
    let caption: String = sqlx::query_scalar("SELECT instagram_caption FROM artists WHERE id = ?")
        .bind(nova)
        .fetch_one(&state.db)
        .await
        .unwrap();
    assert_eq!(caption, long.trim());
}

/// Telegram takes at most 4096 characters per message.
#[tokio::test]
async fn long_replies_are_split_by_line() {
    let (state, _temp, fake) = telegram_state(json!({})).await;
    let text: Vec<String> = (0..300)
        .map(|i| format!("line {i:03} {}", "x".repeat(20)))
        .collect();
    let text = text.join("\n");
    send_text(&bot(&state), ChatId(ADMIN_CHAT), Some(TOPIC), &text)
        .await
        .unwrap();

    let sent = fake.of("sendmessage");
    assert_eq!(sent.len(), 3);
    assert!(sent.iter().all(|c| c.text().len() <= 4096));
    let joined: Vec<&str> = sent.iter().map(|c| c.text()).collect();
    assert_eq!(joined.join("\n"), text);
    fake.assert_all_sends_in_topic();
}

/// teloxide panics on a photo upload with a topic id; the picture must go
/// out through the raw API.
#[tokio::test]
async fn preview_instagram_sends_caption_and_picture() {
    let (r2, endpoint) = FakeR2::serve().await;
    let (state, _temp, fake) = telegram_state(json!({ "r2_endpoint": endpoint })).await;
    let nova = add_artist_with(
        &state,
        "Nova",
        "instagram_caption, pic_key, pic_overlay_key",
        "'Hello IG', 'a/pic.jpg', 'a/overlay.png'",
    )
    .await;
    r2.insert(&state, "a/overlay.png", b"overlay");

    command(
        &state,
        ADMIN_CHAT,
        Some(TOPIC),
        &format!("/preview_instagram {nova}"),
    )
    .await;
    let sent = fake.of("sendmessage");
    assert_eq!(
        sent[0].text(),
        format!("📝 Caption for Nova (ID: {nova}):\n\nHello IG")
    );
    // The overlay wins over the original picture.
    let photo = &fake.of("sendphoto")[0];
    assert_eq!(photo.params["photo"]["len"], 7);
    fake.assert_all_sends_in_topic();
}

// ── Buttons ─────────────────────────────────────────────────────────────────

#[tokio::test]
async fn buttons_from_other_chats_are_refused() {
    let (state, _temp, fake) = telegram_state(json!({})).await;
    let show = add_show(&state, "Late Night", "2099-01-02").await;
    press(&state, OTHER_CHAT, &format!("ig_edit_caption:{show}")).await;
    assert_eq!(fake.calls().len(), 1);
    assert_eq!(toast(&fake).as_deref(), Some("⛔ Unauthorized"));
    assert!(session(&state).await.is_none());
}

#[tokio::test]
async fn bad_button_data_gets_an_answer() {
    let (state, _temp, fake) = telegram_state(json!({})).await;
    for (data, expected) in [
        ("ig_publish:abc", "❌ Invalid show ID"),
        ("ig_edit:abc", "❌ Invalid show ID"),
        ("aig_vid:7", "❌ Invalid callback data"),
        ("aig_vid:7:3", "❌ Invalid track number"),
        ("aig_sort:1:2", "❌ Invalid sort callback data"),
        ("something", "Unknown action"),
    ] {
        press(&state, ADMIN_CHAT, data).await;
        assert_eq!(toast(&fake).as_deref(), Some(expected), "{data}");
    }
    // The name label of the sort prompt does nothing.
    fake.clear();
    press(&state, ADMIN_CHAT, "aig_sort_noop:1:2").await;
    assert_eq!(fake.calls().len(), 1);
    assert_eq!(toast(&fake), None);
}

/// Edit opens Caption / Image / Cancel; Caption starts a reply session;
/// Cancel ends it and brings back Publish / Edit.
#[tokio::test]
async fn show_preview_edit_flow() {
    let (state, _temp, fake) = telegram_state(json!({})).await;
    let show = add_show(&state, "Late Night", "2099-01-02").await;

    press(&state, ADMIN_CHAT, &format!("ig_edit:{show}")).await;
    let markup = fake.of("editmessagereplymarkup");
    assert_eq!(
        markup[0].buttons(),
        vec![vec![
            format!("ig_edit_caption:{show}"),
            format!("ig_edit_image:{show}"),
            format!("ig_edit_cancel:{show}"),
        ]]
    );

    for (data, field, prompt) in [
        ("ig_edit_caption", "Caption", "new caption for *Late Night*"),
        ("ig_edit_image", "Image", "new cover image for *Late Night*"),
    ] {
        fake.clear();
        press(&state, ADMIN_CHAT, &format!("{data}:{show}")).await;
        let session = session(&state).await.unwrap();
        assert_eq!(format!("{:?}", session.field), field);
        assert_eq!((session.show_id, session.artist_id), (show, None));
        assert_eq!(session.preview_message_id, 50);
        let sent = &fake.of("sendmessage")[0];
        assert!(sent.text().contains(prompt), "{}", sent.text());
        assert_eq!(sent.markup()["force_reply"], true);
        fake.assert_all_sends_in_topic();
    }

    fake.clear();
    press(&state, ADMIN_CHAT, &format!("ig_edit_cancel:{show}")).await;
    assert!(session(&state).await.is_none());
    assert_eq!(toast(&fake).as_deref(), Some("✅ Edit cancelled"));
    assert_eq!(
        fake.of("editmessagereplymarkup")[0].buttons(),
        vec![vec![
            format!("ig_publish:{show}"),
            format!("ig_edit:{show}")
        ]]
    );

    // A show that does not exist starts no session.
    assert!(handle_callback_query(
        bot(&state),
        callback(ADMIN_CHAT, "ig_edit_caption:999"),
        state.clone()
    )
    .await
    .is_err());
    assert!(session(&state).await.is_none());
}

#[tokio::test]
async fn artist_preview_buttons_start_sessions() {
    let (state, _temp, fake) = telegram_state(json!({})).await;
    let nova = add_artist_with(&state, "Nova", "telegram_video2_message_id", "88").await;

    press(&state, ADMIN_CHAT, &format!("aig_cap:{nova}")).await;
    let s = session(&state).await.unwrap();
    assert_eq!(format!("{:?}", s.field), "Caption");
    assert_eq!(s.artist_id, Some(nova));
    assert!(fake.of("sendmessage")[0]
        .text()
        .contains("caption for *Nova*"));

    press(&state, ADMIN_CHAT, &format!("aig_img:{nova}")).await;
    assert_eq!(
        format!("{:?}", session(&state).await.unwrap().field),
        "Image"
    );

    // Track 1 has no video message to replace.
    press(&state, ADMIN_CHAT, &format!("aig_vid:{nova}:1")).await;
    assert_eq!(
        toast(&fake).as_deref(),
        Some("❌ No video message found for this track")
    );
    press(&state, ADMIN_CHAT, &format!("aig_vid:{nova}:2")).await;
    let s = session(&state).await.unwrap();
    assert_eq!(format!("{:?}", s.field), "Timecode");
    assert_eq!((s.track_number, s.video_msg_id), (Some(2), Some(88)));
    let prompt = fake.of("sendmessage").last().unwrap().text().to_string();
    assert!(prompt.contains("start timecode for track 2"), "{prompt}");
    fake.assert_all_sends_in_topic();
}

#[tokio::test]
async fn sort_buttons_swap_neighbours() {
    let (state, _temp, fake) = telegram_state(json!({})).await;
    let show = add_show(&state, "Late Night", "2099-01-02").await;
    let a = add_artist(&state, "Ann").await;
    let b = add_artist(&state, "Bo").await;
    let c = add_artist(&state, "Cy").await;
    for (i, id) in [a, b, c].into_iter().enumerate() {
        assign(&state, show, id, i as i32).await;
    }
    let order = || async {
        sqlx::query_scalar::<_, i64>(
            "SELECT artist_id FROM artist_show_assignments WHERE show_id = ? ORDER BY sort_order",
        )
        .bind(show)
        .fetch_all(&state.db)
        .await
        .unwrap()
    };

    press(&state, ADMIN_CHAT, &format!("aig_sort:{show}:{b}:up")).await;
    assert_eq!(order().await, vec![b, a, c]);
    assert_eq!(toast(&fake).as_deref(), Some("✅ Reordered"));
    let edit = &fake.of("editmessagetext")[0];
    assert_eq!(edit.int("message_id"), Some(50));
    assert!(edit.text().contains("post order for Late Night"));
    assert_eq!(
        edit.buttons()[0],
        vec![
            format!("aig_sort_noop:{show}:{b}"),
            format!("aig_sort:{show}:{b}:down")
        ]
    );

    // The first artist cannot go up, the last cannot go down.
    for data in [
        format!("aig_sort:{show}:{b}:up"),
        format!("aig_sort:{show}:{c}:down"),
    ] {
        press(&state, ADMIN_CHAT, &data).await;
        assert_eq!(toast(&fake).as_deref(), Some("⚠️ Can't move further"));
    }
    press(&state, ADMIN_CHAT, &format!("aig_sort:{show}:999:up")).await;
    assert_eq!(toast(&fake).as_deref(), Some("❌ Artist not found in show"));
    assert_eq!(order().await, vec![b, a, c]);
}

// ── Messages that are not commands ──────────────────────────────────────────

#[tokio::test]
async fn live_chat_messages_reach_the_panel() {
    let (state, _temp, fake) = telegram_state(json!({ "telegram_live_chat_id": OTHER_CHAT })).await;
    let send = |msg: Message| handle_non_command_message(bot(&state), msg, state.clone());

    send(text_message(OTHER_CHAT, None, "hello radio"))
        .await
        .unwrap();
    let bot_user = json!({"from": {"id": 6, "is_bot": true, "first_name": "Bot"}, "text": "beep"});
    send(message(OTHER_CHAT, None, bot_user)).await.unwrap();
    let as_channel = json!({
        "text": "anon",
        "sender_chat": {"id": -3003, "type": "channel", "title": "Chan"}
    });
    send(message(OTHER_CHAT, None, as_channel)).await.unwrap();

    let recent = state.chat_hub.recent().await;
    assert_eq!(recent.len(), 1);
    assert_eq!(recent[0].author, "Ada L");
    assert_eq!(recent[0].text, "hello radio");
    assert!(!recent[0].host);
    assert!(fake.calls().is_empty());

    // Without a session, admin chat messages are ignored.
    send(text_message(ADMIN_CHAT, Some(TOPIC), "just talking"))
        .await
        .unwrap();
    assert!(fake.calls().is_empty());
}

#[tokio::test]
async fn caption_reply_updates_the_artist_preview() {
    let (state, _temp, fake) = telegram_state(json!({})).await;
    let nova = add_artist_with(&state, "Nova", "track1_video_key", "'v1.mp4'").await;
    start_session(&state, Some(nova), models::TelegramEditField::Caption).await;

    // A photo is not a caption; the session stays.
    let photo =
        json!({"photo": [{"file_id": "p", "file_unique_id": "pu", "width": 1, "height": 1}]});
    handle_non_command_message(
        bot(&state),
        message(ADMIN_CHAT, Some(TOPIC), photo),
        state.clone(),
    )
    .await
    .unwrap();
    assert_eq!(
        fake.of("sendmessage")[0].text(),
        "❌ Please send a text message for the caption."
    );
    assert!(session(&state).await.is_some());

    fake.clear();
    handle_non_command_message(
        bot(&state),
        text_message(ADMIN_CHAT, Some(TOPIC), "Fresh caption"),
        state.clone(),
    )
    .await
    .unwrap();
    let caption: String = sqlx::query_scalar("SELECT instagram_caption FROM artists WHERE id = ?")
        .bind(nova)
        .fetch_one(&state.db)
        .await
        .unwrap();
    assert_eq!(caption, "Fresh caption");
    let edit = &fake.of("editmessagecaption")[0];
    assert_eq!(edit.int("message_id"), Some(77));
    assert_eq!(edit.text(), "Fresh caption");
    assert_eq!(
        edit.buttons(),
        vec![
            vec![format!("aig_pub:{nova}")],
            vec![format!("aig_cap:{nova}"), format!("aig_img:{nova}")],
            vec![format!("aig_vid:{nova}:1")],
        ]
    );
    assert_eq!(fake.of("sendmessage")[0].text(), "✅ Caption updated.");
    assert!(session(&state).await.is_none());
    fake.assert_all_sends_in_topic();
}

#[tokio::test]
async fn image_reply_replaces_the_artist_picture() {
    let (r2, endpoint) = FakeR2::serve().await;
    let (state, _temp, fake) = telegram_state(json!({ "r2_endpoint": endpoint })).await;
    let nova = add_artist_with(&state, "Nova", "instagram_caption", "'Cap'").await;
    start_session(&state, Some(nova), models::TelegramEditField::Image).await;
    fake.serve_file(b"new photo");

    let photo = json!({"photo": [
        {"file_id": "small", "file_unique_id": "s", "width": 1, "height": 1},
        {"file_id": "large", "file_unique_id": "l", "width": 9, "height": 9}
    ]});
    handle_non_command_message(
        bot(&state),
        message(ADMIN_CHAT, Some(TOPIC), photo),
        state.clone(),
    )
    .await
    .unwrap();

    // The largest size is downloaded and stored as the overlay.
    assert_eq!(fake.of("getfile")[0].str("file_id"), Some("large"));
    let key = format!("artists/{nova}/overlay/telegram_upload.jpg");
    assert_eq!(r2.object(&state, &key).as_deref(), Some(&b"new photo"[..]));
    let stored: String = sqlx::query_scalar("SELECT pic_overlay_key FROM artists WHERE id = ?")
        .bind(nova)
        .fetch_one(&state.db)
        .await
        .unwrap();
    assert_eq!(stored, key);
    let edit = &fake.of("editmessagemedia")[0];
    assert_eq!(edit.int("message_id"), Some(77));
    let media: Value = serde_json::from_str(edit.str("media").unwrap()).unwrap();
    assert_eq!(
        (media["type"].as_str(), media["caption"].as_str()),
        (Some("photo"), Some("Cap"))
    );
    assert_eq!(fake.of("sendmessage")[0].text(), "✅ Image updated.");
    assert!(session(&state).await.is_none());
    fake.assert_all_sends_in_topic();
}

#[tokio::test]
async fn timecode_reply_needs_a_valid_time() {
    let (state, _temp, fake) = telegram_state(json!({})).await;
    let nova = add_artist(&state, "Nova").await;
    start_session(&state, Some(nova), models::TelegramEditField::Timecode).await;

    let send = |text: &str| {
        handle_non_command_message(
            bot(&state),
            text_message(ADMIN_CHAT, Some(TOPIC), text),
            state.clone(),
        )
    };
    send("1:75").await.unwrap();
    assert_eq!(
        fake.of("sendmessage")[0].text(),
        "❌ Seconds must be 0-59, got 75"
    );
    assert!(
        session(&state).await.is_some(),
        "a typo must not end the session"
    );

    // A valid time ends the session; the artist has no picture, so it fails
    // with a message instead of silence.
    fake.clear();
    send("1:05").await.unwrap();
    assert!(session(&state).await.is_none());
    let texts: Vec<String> = fake
        .of("sendmessage")
        .iter()
        .map(|c| c.text().to_string())
        .collect();
    assert_eq!(
        texts,
        vec![
            "⏳ Regenerating video from 1:05...".to_string(),
            format!("❌ Artist {nova} has no image"),
        ]
    );
    fake.assert_all_sends_in_topic();
}

/// Show-level sessions: a caption reply becomes the show bio, a photo reply
/// the new cover; the preview keeps its Publish / Edit buttons.
#[tokio::test]
async fn show_preview_replies_update_bio_and_cover() {
    let (r2, endpoint) = FakeR2::serve().await;
    let (state, _temp, fake) = telegram_state(json!({ "r2_endpoint": endpoint })).await;
    let show = add_show(&state, "Late Night", "2099-01-02").await;
    let start = |field| {
        let state = state.clone();
        async move {
            start_session(&state, None, field).await;
            state
                .telegram_edit_sessions
                .lock()
                .await
                .get_mut(&ADMIN_CHAT)
                .unwrap()
                .show_id = show;
        }
    };

    start(models::TelegramEditField::Caption).await;
    handle_non_command_message(
        bot(&state),
        text_message(ADMIN_CHAT, Some(TOPIC), "New bio"),
        state.clone(),
    )
    .await
    .unwrap();
    let bio: String = sqlx::query_scalar("SELECT ai_bio FROM shows WHERE id = ?")
        .bind(show)
        .fetch_one(&state.db)
        .await
        .unwrap();
    assert_eq!(bio, "New bio");
    let edit = &fake.of("editmessagecaption")[0];
    assert_eq!(edit.text(), "Late Night\n\nNew bio");
    assert_eq!(
        edit.buttons(),
        vec![vec![
            format!("ig_publish:{show}"),
            format!("ig_edit:{show}")
        ]]
    );

    fake.clear();
    fake.serve_file(b"cover bytes");
    start(models::TelegramEditField::Image).await;
    let photo =
        json!({"photo": [{"file_id": "c", "file_unique_id": "cu", "width": 1, "height": 1}]});
    handle_non_command_message(
        bot(&state),
        message(ADMIN_CHAT, Some(TOPIC), photo),
        state.clone(),
    )
    .await
    .unwrap();
    assert!(r2
        .object(&state, &format!("shows/{show}/cover.png"))
        .is_some());
    assert_eq!(fake.of("editmessagemedia").len(), 1);
    assert_eq!(fake.of("sendmessage")[0].text(), "✅ Image updated.");
    assert!(session(&state).await.is_none());
    fake.assert_all_sends_in_topic();
}
