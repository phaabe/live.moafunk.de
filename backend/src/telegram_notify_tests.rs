//! Admin notifications against a fake Telegram, a fake R2 and real SQLite:
//! texts, buttons, the on/off switch and the MoafunkBot topic on every send.

use super::*;
use crate::fake_r2::FakeR2;
use crate::fake_telegram::{telegram_state, FakeTelegram};
use serde_json::{json, Value};

async fn setup() -> (Arc<AppState>, tempfile::TempDir, FakeTelegram, FakeR2) {
    let (r2, endpoint) = FakeR2::serve().await;
    let (state, temp, fake) = telegram_state(json!({
        "r2_endpoint": endpoint,
        "admin_base_url": "https://admin.test/",
    }))
    .await;
    (state, temp, fake, r2)
}

async fn notifications(state: &Arc<AppState>, on: bool) {
    crate::db::set_setting(&state.db, "notifications_enabled", &on.to_string())
        .await
        .unwrap();
}

async fn add_artist(state: &Arc<AppState>, cols: &str, values: &str) -> models::Artist {
    let id: i64 = sqlx::query_scalar(&format!(
        "INSERT INTO artists (name, pronouns, track1_name, track2_name{cols}) \
         VALUES ('Nova', 'she/her', 'One', 'Two'{values}) RETURNING id"
    ))
    .fetch_one(&state.db)
    .await
    .unwrap();
    artist(state, id).await
}

async fn artist(state: &Arc<AppState>, id: i64) -> models::Artist {
    sqlx::query_as("SELECT * FROM artists WHERE id = ?")
        .bind(id)
        .fetch_one(&state.db)
        .await
        .unwrap()
}

async fn add_show(state: &Arc<AppState>, cover: bool) -> i64 {
    sqlx::query_scalar(
        "INSERT INTO shows (title, date, ai_bio, cover_generated_at) \
         VALUES ('Late <Night>', '2099-01-02', 'Show bio', ?) RETURNING id",
    )
    .bind(cover.then_some("2026-01-01T00:00:00Z"))
    .fetch_one(&state.db)
    .await
    .unwrap()
}

async fn assign(state: &Arc<AppState>, show: i64, artist: i64) {
    sqlx::query("INSERT INTO artist_show_assignments (artist_id, show_id) VALUES (?, ?)")
        .bind(artist)
        .bind(show)
        .execute(&state.db)
        .await
        .unwrap();
}

fn artist_value(extra: Value) -> models::Artist {
    let mut artist = json!({
        "id": 7, "name": "Nova & Co", "pronouns": "she/her",
        "track1_name": "<One>", "track2_name": "Two", "status": "unassigned",
        "no_voice_message": false, "created_at": "", "updated_at": ""
    });
    artist
        .as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    serde_json::from_value(artist).unwrap()
}

// ── Pure text and buttons ───────────────────────────────────────────────────

#[test]
fn captions_are_cut_at_a_word() {
    assert_eq!(truncate_caption("short", 10), "short");
    assert_eq!(truncate_caption("exactly10!", 10), "exactly10!");
    assert_eq!(truncate_caption("one two three four", 10), "one two…");
    // No space: a hard cut.
    assert_eq!(truncate_caption("abcdefghijkl", 10), "abcdefghi…");
}

#[test]
fn html_is_escaped() {
    assert_eq!(html_escape("a < b & c > d"), "a &lt; b &amp; c &gt; d");
}

#[test]
fn artist_caption_escapes_text_and_ends_with_the_profile_link() {
    let artist = artist_value(json!({
        "music_description": "Dub & <noise>",
        "instagram": "@nova", "soundcloud": "", "bandcamp": "nova.bandcamp",
        "mentions": "Thanks", "upcoming_events": ""
    }));
    let caption = build_artist_caption(&artist, "https://admin.test/#/artists/7");
    assert!(caption.starts_with("🎤 <b>New artist submitted: Nova &amp; Co (ID: 7)</b>"));
    assert!(
        caption.contains("🎵 <b>Track 1:</b> &lt;One&gt;"),
        "{caption}"
    );
    assert!(caption.contains("📝 Dub &amp; &lt;noise&gt;"), "{caption}");
    // Empty socials are left out.
    assert!(caption.contains("<b>IG:</b> @nova | <b>BC:</b> nova.bandcamp"));
    assert!(!caption.contains("SC:") && !caption.contains("Events"));
    assert!(caption.contains("💬 <b>Mentions:</b> Thanks"));
    assert!(caption.ends_with("<a href=\"https://admin.test/#/artists/7\">Open artist profile</a>"));

    // A long text is cut, but the link stays.
    let long = artist_value(json!({ "music_description": "word ".repeat(400) }));
    let caption = build_artist_caption(&long, "https://admin.test/#/artists/7");
    assert!(
        caption.chars().count() <= 1024,
        "{}",
        caption.chars().count()
    );
    assert!(caption.contains("…\n\n<a href="), "{caption}");
}

#[test]
fn show_update_caption_lists_current_artists() {
    let show: models::Show = serde_json::from_value(json!({
        "id": 3, "title": "Late <Night>", "date": "2099-01-02", "status": "scheduled",
        "show_type": "unheard", "delivery": "live",
        "created_at": "", "updated_at": ""
    }))
    .unwrap_or_else(|e| panic!("show json: {e}"));
    let context = |action| ShowUpdateContext {
        show_id: 3,
        artist_name: "Kai & Co".to_string(),
        action,
    };
    let caption = build_show_update_caption(
        &show,
        &[artist_value(json!({}))],
        &context(ShowUpdateAction::Added),
        "https://admin.test/#/shows/3",
    );
    assert_eq!(
        caption,
        "🎪 <b>Show updated: Late &lt;Night&gt;</b>\n\n\
         ➕ <b>Kai &amp; Co</b> was added\n\n\
         🎤 <b>Current Artists:</b>\n• Nova &amp; Co\n\n\
         🔗 <a href=\"https://admin.test/#/shows/3\">Open show page</a>"
    );
    let caption = build_show_update_caption(&show, &[], &context(ShowUpdateAction::Removed), "u");
    assert!(caption.contains("➖ <b>Kai &amp; Co</b> was removed"));
    assert!(caption.contains("🎤 No artists assigned yet."));
}

fn rows(markup: &str) -> Vec<Vec<String>> {
    let markup: Value = serde_json::from_str(markup).unwrap();
    markup["inline_keyboard"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| {
            row.as_array()
                .unwrap()
                .iter()
                .map(|b| {
                    format!(
                        "{}={}",
                        b["text"].as_str().unwrap(),
                        b["callback_data"].as_str().unwrap()
                    )
                })
                .collect()
        })
        .collect()
}

#[test]
fn artist_preview_keyboard_shows_video_buttons_per_track() {
    assert_eq!(
        rows(&build_artist_preview_keyboard(7, true, true)),
        vec![
            vec!["📤 Publish=aig_pub:7"],
            vec!["✏️ Caption=aig_cap:7", "🖼 Image=aig_img:7"],
            vec!["🎬 Video 1=aig_vid:7:1", "🎬 Video 2=aig_vid:7:2"],
        ]
    );
    assert_eq!(
        rows(&build_artist_preview_keyboard(7, false, true))[2],
        vec!["🎬 Video 2=aig_vid:7:2"]
    );
    assert_eq!(
        rows(&build_artist_preview_keyboard(7, false, false)).len(),
        2
    );
}

#[test]
fn sort_keyboard_has_no_up_on_top_and_no_down_at_the_bottom() {
    let artists = [
        (1, "Ann".to_string()),
        (2, "Bo".to_string()),
        (3, "Cy".to_string()),
    ];
    assert_eq!(
        rows(&build_sort_order_keyboard(9, &artists)),
        vec![
            vec!["Ann (#1)=aig_sort_noop:9:1", "⬇️=aig_sort:9:1:down"],
            vec![
                "⬆️=aig_sort:9:2:up",
                "Bo (#2)=aig_sort_noop:9:2",
                "⬇️=aig_sort:9:2:down"
            ],
            vec!["⬆️=aig_sort:9:3:up", "Cy (#3)=aig_sort_noop:9:3"],
        ]
    );
    assert_eq!(
        rows(&build_sort_order_keyboard(9, &artists[..1])),
        vec![vec!["Ann (#1)=aig_sort_noop:9:1"]]
    );
}

// ── Sending ─────────────────────────────────────────────────────────────────

/// The settings switch silences `notify` and `notify_html`, but not the
/// message about the switch itself.
#[tokio::test]
async fn notifications_follow_the_switch() {
    let (state, _temp, fake, _r2) = setup().await;
    notify(&state, "plain").await;
    notify_html(&state, "<b>html</b>").await;
    let sent = fake.of("sendmessage");
    assert_eq!(sent[0].text(), "plain");
    assert_eq!(sent[0].str("parse_mode"), None);
    assert_eq!(sent[1].str("parse_mode"), Some("HTML"));

    notifications(&state, false).await;
    fake.clear();
    notify(&state, "muted").await;
    notify_html(&state, "muted").await;
    assert!(fake.calls().is_empty());
    notify_unconditional(&state, "🔕 off").await;
    assert_eq!(fake.of("sendmessage")[0].text(), "🔕 off");
    fake.assert_all_sends_in_topic();

    // A Telegram error is logged, never raised.
    fake.fail(Some("sendmessage"));
    notify_unconditional(&state, "still fine").await;
}

#[tokio::test]
async fn event_notifications_texts() {
    let (state, _temp, fake, _r2) = setup().await;
    notify_stream_start(&state, "dj");
    fake.wait_for(1).await;
    notify_stream_stop(&state, "dj");
    fake.wait_for(2).await;
    notify_soundcloud_upload(&state, 3, "A & B", "https://sc.test/x?a=1&b=2");
    fake.wait_for(3).await;
    notify_instagram_published(&state, 3, "<Show>", Some("https://ig.test/p"));
    fake.wait_for(4).await;
    notify_instagram_published(&state, 3, "Show", None);
    fake.wait_for(5).await;
    notify_prerecorded_missed(&state, 3, "Tape", "2099-01-02T20:00:00Z", "dj is live");
    let calls = fake.wait_for(6).await;

    let texts: Vec<&str> = calls.iter().map(|c| c.text()).collect();
    assert_eq!(texts[0], "📡 Stream started by dj");
    assert_eq!(texts[1], "📡 Stream ended (dj)");
    assert_eq!(
        texts[2],
        "☁️ SoundCloud upload complete\n\n<b>A &amp; B</b> (show #3)\n\n\
         <a href=\"https://sc.test/x?a=1&amp;b=2\">Open on SoundCloud</a>"
    );
    assert_eq!(
        texts[3],
        "📸 Published to Instagram\n\n<b>&lt;Show&gt;</b> (show #3)\n\n\
         <a href=\"https://ig.test/p\">View on Instagram</a>"
    );
    assert_eq!(
        texts[4],
        "📸 Published to Instagram\n\n<b>Show</b> (show #3)"
    );
    assert!(texts[5].starts_with(
        "⚠️ Prerecorded show 'Tape' (id 3) did not start at 2099-01-02T20:00:00Z: dj is live."
    ));
    fake.assert_all_sends_in_topic();
}

#[tokio::test]
async fn sort_prompt_lists_artists_in_post_order() {
    let (state, _temp, fake, _r2) = setup().await;
    let show = add_show(&state, false).await;
    send_sort_order_prompt(&state, show).await.unwrap();
    assert!(fake.calls().is_empty(), "no artists, no prompt");

    let nova = add_artist(&state, "", "").await;
    assign(&state, show, nova.id).await;
    send_sort_order_prompt(&state, show).await.unwrap();
    let prompt = &fake.of("sendmessage")[0];
    assert!(prompt
        .text()
        .starts_with("📋 Set artist post order for Late <Night>"));
    assert_eq!(
        prompt.buttons(),
        vec![vec![format!("aig_sort_noop:{show}:{}", nova.id)]]
    );
    fake.assert_all_sends_in_topic();

    assert!(send_sort_order_prompt(&state, 999)
        .await
        .unwrap_err()
        .contains("not found"));
    fake.fail(Some("sendmessage"));
    assert!(send_sort_order_prompt(&state, show)
        .await
        .unwrap_err()
        .contains("test failure"));
}

#[tokio::test]
async fn show_preview_sends_cover_with_publish_buttons() {
    let (state, _temp, fake, r2) = setup().await;
    let bare = add_show(&state, false).await;
    assert_eq!(
        send_show_instagram_preview(&state, bare).await.unwrap_err(),
        "Show has no cover image. Assign artists first."
    );

    let show = add_show(&state, true).await;
    // The cover is not in R2 yet.
    assert!(send_show_instagram_preview(&state, show).await.is_err());
    r2.insert(&state, &format!("shows/{show}/cover.png"), b"cover");

    fake.fail(Some("sendphoto"));
    assert!(send_show_instagram_preview(&state, show).await.is_err());
    let sent_at = || async {
        sqlx::query_scalar::<_, Option<String>>(
            "SELECT telegram_preview_sent_at FROM shows WHERE id = ?",
        )
        .bind(show)
        .fetch_one(&state.db)
        .await
        .unwrap()
    };
    assert_eq!(sent_at().await, None);

    fake.fail(None);
    fake.clear();
    send_show_instagram_preview(&state, show).await.unwrap();
    let photo = &fake.of("sendphoto")[0];
    assert_eq!(photo.params["photo"]["len"], 5);
    assert_eq!(photo.text(), "Late <Night>\n\nShow bio");
    assert_eq!(photo.str("parse_mode"), Some(""));
    assert_eq!(
        photo.buttons(),
        vec![vec![
            format!("ig_publish:{show}"),
            format!("ig_edit:{show}")
        ]]
    );
    assert!(sent_at().await.is_some());
    fake.assert_all_sends_in_topic();

    notifications(&state, false).await;
    fake.clear();
    send_show_instagram_preview(&state, show).await.unwrap();
    assert!(fake.calls().is_empty());
}

#[tokio::test]
async fn artist_preview_sends_photo_and_track_videos() {
    let (state, _temp, fake, r2) = setup().await;
    let no_caption = add_artist(&state, "", "").await;
    assert_eq!(
        send_artist_instagram_preview(&state, &no_caption)
            .await
            .unwrap_err(),
        "Artist has no Instagram caption. Generate one first."
    );

    let nova = add_artist(
        &state,
        ", instagram_caption, pic_cropped_key, track1_video_key, track2_video_key",
        ", 'IG caption', 'a/crop.jpg', 'a/v1.mp4', 'a/v2.mp4'",
    )
    .await;
    r2.insert(&state, "a/crop.jpg", b"crop");
    r2.insert(&state, "a/v1.mp4", b"video one");
    // Track 2's video is missing in R2: that track is skipped, the rest goes out.

    send_artist_instagram_preview(&state, &nova).await.unwrap();
    let photo = &fake.of("sendphoto")[0];
    assert_eq!(photo.text(), "IG caption");
    assert_eq!(photo.buttons().len(), 3);
    let videos = fake.of("sendvideo");
    assert_eq!(videos.len(), 1);
    assert_eq!(videos[0].text(), "🎵 Track 1: One");
    fake.assert_all_sends_in_topic();

    let stored = artist(&state, nova.id).await;
    assert!(stored.telegram_preview_message_id.is_some());
    assert!(stored.telegram_video1_message_id.is_some());
    assert_eq!(stored.telegram_video2_message_id, None);
    assert!(stored.telegram_artist_preview_sent_at.is_some());
}

/// A new submission: the picture with the caption, then the voice message
/// and both tracks as replies to it.
#[tokio::test]
async fn submission_preview_sends_picture_then_audio_replies() {
    let (state, _temp, fake, r2) = setup().await;
    let nova = add_artist(
        &state,
        ", pic_key, voice_message_key, track1_key, track2_key",
        ", 'a/pic.jpg', 'a/voice.mp3', 'a/one.mp3', 'a/two.mp3'",
    )
    .await;
    for key in ["a/pic.jpg", "a/voice.mp3", "a/one.mp3", "a/two.mp3"] {
        r2.insert(&state, key, key.as_bytes());
    }

    notify_artist_submission(&state, nova.id, "Nova");
    let calls = fake.wait_for(4).await;
    assert_eq!(calls[0].method, "sendphoto");
    assert_eq!(calls[0].str("parse_mode"), Some("HTML"));
    assert!(calls[0].text().ends_with(&format!(
        "<a href=\"https://admin.test/#/artists/{}\">Open artist profile</a>",
        nova.id
    )));
    let photo_id = 101;
    let titles: Vec<&str> = calls[1..].iter().map(|c| c.str("title").unwrap()).collect();
    assert_eq!(titles, vec!["Nova – voice message", "One", "Two"]);
    for audio in &calls[1..] {
        assert_eq!(audio.method, "sendaudio");
        assert_eq!(audio.str("performer"), Some("Nova"));
        let reply: Value = serde_json::from_str(audio.str("reply_parameters").unwrap()).unwrap();
        assert_eq!(reply["message_id"], photo_id);
    }
    fake.assert_all_sends_in_topic();
}

/// If the picture cannot be sent, a short HTML text goes out instead.
#[tokio::test]
async fn submission_preview_falls_back_to_text() {
    let (state, _temp, fake, _r2) = setup().await;
    let nova = add_artist(&state, ", pic_key", ", 'a/missing.jpg'").await;
    notify_artist_submission(&state, nova.id, "Nova");
    let calls = fake.wait_for(1).await;
    assert_eq!(
        calls[0].text(),
        format!(
            "🎤 New artist submitted: Nova (ID: {id})\n\
             <a href=\"https://admin.test/#/artists/{id}\">Open artist profile</a>",
            id = nova.id
        )
    );
    fake.assert_all_sends_in_topic();

    // Without a picture the caption goes out as text.
    let kai = add_artist(&state, "", "").await;
    fake.clear();
    notify_artist_submission(&state, kai.id, "Kai");
    let calls = fake.wait_for(1).await;
    assert!(calls[0]
        .text()
        .starts_with("🎤 <b>New artist submitted: Nova"));
    assert_eq!(calls[0].str("parse_mode"), Some("HTML"));
}

#[tokio::test]
async fn show_update_sends_cover_or_text() {
    let (state, _temp, fake, r2) = setup().await;
    let show = add_show(&state, true).await;
    let nova = add_artist(&state, "", "").await;
    assign(&state, show, nova.id).await;
    let update = || ShowUpdateContext {
        show_id: show,
        artist_name: "Nova".to_string(),
        action: ShowUpdateAction::Added,
    };

    // The cover is missing in R2: text instead.
    send_show_update_notification(&state, update())
        .await
        .unwrap();
    let text = &fake.of("sendmessage")[0];
    assert!(text.text().contains("• Nova"), "{}", text.text());
    assert!(text.text().ends_with(&format!(
        "<a href=\"https://admin.test/#/shows/{show}\">Open show page</a>"
    )));

    r2.insert(&state, &format!("shows/{show}/cover.png"), b"cover");
    fake.clear();
    send_show_update_notification(&state, update())
        .await
        .unwrap();
    assert_eq!(fake.of("sendphoto").len(), 1);
    assert!(fake.of("sendmessage").is_empty());
    fake.assert_all_sends_in_topic();

    assert!(send_show_update_notification(
        &state,
        ShowUpdateContext {
            show_id: 999,
            ..update()
        }
    )
    .await
    .is_err());
}

/// Quick roster changes end in one message, 30 s after the last change.
#[tokio::test]
async fn show_updates_are_debounced() {
    let (state, _temp, fake, _r2) = setup().await;
    let show = add_show(&state, false).await;
    // Pause after setup: the SQLite pool must connect on real time.
    tokio::time::pause();
    schedule_show_update_notification(&state, show, "A".to_string(), ShowUpdateAction::Added);
    tokio::time::sleep(std::time::Duration::from_secs(10)).await;
    schedule_show_update_notification(&state, show, "B".to_string(), ShowUpdateAction::Removed);
    tokio::time::sleep(std::time::Duration::from_secs(29)).await;
    assert!(fake.calls().is_empty());
    tokio::time::resume();
    let calls = fake.wait_for(1).await;
    assert!(
        calls[0].text().contains("<b>B</b> was removed"),
        "{}",
        calls[0].text()
    );
    tokio::time::sleep(std::time::Duration::from_millis(200)).await;
    assert_eq!(fake.calls().len(), 1, "the first change was replaced");
    fake.assert_all_sends_in_topic();
}
/// Telegram counts characters; a cut inside a multi-byte character must not
/// panic.
#[test]
fn captions_with_emoji_are_cut_safely() {
    let caption = format!("{}🎵 tail", "a".repeat(1021));
    assert_eq!(
        truncate_caption(&caption, 1024),
        format!("{}…", "a".repeat(1021))
    );
}
