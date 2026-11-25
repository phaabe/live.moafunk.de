//! The Telegram on-air message against a fake Telegram, a fake R2 and real
//! SQLite: saving, the go-live send, pin and unpin, and the tick.

use super::*;
use crate::broadcast_tests::{add_user, headers};
use crate::fake_r2::FakeR2;
use crate::fake_telegram::{telegram_state, Call, FakeTelegram};
use axum::http::StatusCode;
use serde_json::{json, Value};

/// A channel id in Telegram's -100… form, so the permalink is a t.me/c/ link.
const CHANNEL: i64 = -1001234567890;
const EPISODE: i64 = 7;

/// Bot, channel and fake R2; an episode of `host` on 2030-06-05 at 20:00 Berlin.
async fn setup() -> (
    Arc<AppState>,
    tempfile::TempDir,
    FakeTelegram,
    FakeR2,
    HeaderMap,
) {
    let (r2, endpoint) = FakeR2::serve().await;
    let (state, temp, fake) =
        telegram_state(json!({ "telegram_announce_chat_id": CHANNEL, "r2_endpoint": endpoint }))
            .await;
    let host = add_user(&state, "host", "host").await;
    sqlx::query(
        "INSERT INTO shows (id, title, date, show_type, host_user_id, start_time) \
         VALUES (?, 'Night Bass', '2030-06-05', 'external', ?, '20:00')",
    )
    .bind(EPISODE)
    .bind(host)
    .execute(&state.db)
    .await
    .unwrap();
    let headers = headers(&state, host).await;
    (state, temp, fake, r2, headers)
}

async fn reply<T: IntoResponse>(result: Result<T>) -> (StatusCode, Value) {
    let response = match result {
        Ok(value) => value.into_response(),
        Err(e) => e.into_response(),
    };
    let status = response.status();
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    (status, serde_json::from_slice(&body).unwrap_or(Value::Null))
}

async fn put(state: &Arc<AppState>, headers: &HeaderMap, body: Value) -> (StatusCode, Value) {
    let input = serde_json::from_value(body).unwrap();
    reply(
        api_put_onair_message(
            State(state.clone()),
            Path(EPISODE),
            headers.clone(),
            Json(input),
        )
        .await,
    )
    .await
}

async fn cancel(state: &Arc<AppState>, headers: &HeaderMap) -> (StatusCode, Value) {
    reply(api_cancel_onair_message(State(state.clone()), Path(EPISODE), headers.clone()).await)
        .await
}

async fn row(state: &Arc<AppState>) -> OnAirMessage {
    load(state, EPISODE).await.unwrap().unwrap()
}

/// Calls to the channel (admin notices go to the admin group).
fn to_channel(fake: &FakeTelegram) -> Vec<Call> {
    fake.calls()
        .into_iter()
        .filter(|c| c.int("chat_id") == Some(CHANNEL))
        .collect()
}

/// A bool parameter: JSON true, or "true" in a multipart form.
fn flag(call: &Call, key: &str) -> bool {
    matches!(call.params.get(key), Some(Value::Bool(true))) || call.str(key) == Some("true")
}

#[tokio::test]
async fn hosts_save_and_change_the_message_until_it_is_sent() {
    let (state, _temp, _fake, _r2, host) = setup().await;

    let (code, saved) = put(
        &state,
        &host,
        json!({ "text": " We are live! ", "image_source": "none" }),
    )
    .await;
    assert_eq!(code, StatusCode::OK, "{saved}");
    assert_eq!(saved["state"], "scheduled");
    assert_eq!(saved["text"], "We are live!");
    assert_eq!(saved["pin"], true);
    assert_eq!(saved["silent"], false);
    // 20:00 Berlin summer time.
    assert_eq!(saved["scheduled_at"], "2030-06-05T18:00:00Z");

    let long = "x".repeat(1025);
    assert_eq!(
        put(&state, &host, json!({ "text": "" })).await.0,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(
        put(&state, &host, json!({ "text": long })).await.0,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(
        put(
            &state,
            &host,
            json!({ "text": long, "image_source": "none" })
        )
        .await
        .0,
        StatusCode::OK
    );
    let foreign = json!({ "text": "Hi", "image_source": "upload", "image_key": "shows/8/announcements/uploads/a.jpg" });
    assert_eq!(put(&state, &host, foreign).await.0, StatusCode::BAD_REQUEST);
    let own = json!({ "text": "Hi", "image_source": "upload", "image_key": "shows/7/announcements/uploads/a.jpg" });
    assert_eq!(put(&state, &host, own).await.0, StatusCode::OK);

    let other = add_user(&state, "other", "host").await;
    let other = headers(&state, other).await;
    assert_ne!(
        put(&state, &other, json!({ "text": "Hi" })).await.0,
        StatusCode::OK
    );

    db::set_setting(&state.db, db::TELEGRAM_ONAIR_ENABLED, "false")
        .await
        .unwrap();
    assert_eq!(
        put(&state, &host, json!({ "text": "Hi" })).await.0,
        StatusCode::SERVICE_UNAVAILABLE
    );
}

#[tokio::test]
async fn it_goes_out_once_at_go_live_silent_and_pinned() {
    let (state, _temp, fake, _r2, host) = setup().await;
    let body = json!({ "text": "Live now", "image_source": "none", "silent": true, "pin": true });
    assert_eq!(put(&state, &host, body).await.0, StatusCode::OK);

    on_air(&state, EPISODE, Utc::now()).await;
    on_air(&state, EPISODE, Utc::now()).await;

    let calls = to_channel(&fake);
    assert_eq!(calls.len(), 2, "{calls:?}");
    assert_eq!(calls[0].method, "sendmessage");
    assert_eq!(calls[0].text(), "Live now");
    assert!(flag(&calls[0], "disable_notification"));
    assert_eq!(calls[1].method, "pinchatmessage");

    let sent = row(&state).await;
    assert_eq!(sent.state, "sent");
    assert!(sent.pinned);
    assert!(sent.error.is_none());
    assert!(
        sent.permalink
            .as_deref()
            .unwrap()
            .starts_with("https://t.me/c/1234567890/"),
        "{:?}",
        sent.permalink
    );
    assert_eq!(
        put(&state, &host, json!({ "text": "Again" })).await.0,
        StatusCode::CONFLICT
    );
    assert_eq!(cancel(&state, &host).await.0, StatusCode::CONFLICT);
}

#[tokio::test]
async fn the_cover_goes_as_a_photo_and_a_missing_cover_sends_the_text() {
    let (state, _temp, fake, r2, host) = setup().await;
    assert_eq!(
        put(&state, &host, json!({ "text": "With cover", "pin": false }))
            .await
            .0,
        StatusCode::OK
    );
    // No cover yet: the text goes out alone.
    on_air(&state, EPISODE, Utc::now()).await;
    let calls = to_channel(&fake);
    assert_eq!(calls.len(), 1, "{calls:?}");
    assert_eq!(calls[0].method, "sendmessage");

    sqlx::query("UPDATE telegram_onair_messages SET state = 'failed'")
        .execute(&state.db)
        .await
        .unwrap();
    fake.clear();
    r2.insert(&state, "shows/7/cover.png", b"cover bytes");
    assert_eq!(
        put(&state, &host, json!({ "text": "With cover", "pin": false }))
            .await
            .0,
        StatusCode::OK
    );
    on_air(&state, EPISODE, Utc::now()).await;
    let calls = to_channel(&fake);
    assert_eq!(calls.len(), 1, "{calls:?}");
    assert_eq!(calls[0].method, "sendphoto");
    assert_eq!(calls[0].text(), "With cover");
    assert!(!flag(&calls[0], "disable_notification"));
    assert!(!row(&state).await.pinned);
}

#[tokio::test]
async fn a_refused_send_fails_and_can_be_saved_again() {
    let (state, _temp, fake, _r2, host) = setup().await;
    assert_eq!(
        put(
            &state,
            &host,
            json!({ "text": "Live", "image_source": "none" })
        )
        .await
        .0,
        StatusCode::OK
    );
    fake.fail(Some("sendmessage"));
    on_air(&state, EPISODE, Utc::now()).await;
    let failed = row(&state).await;
    assert_eq!(failed.state, "failed");
    assert!(failed.error.unwrap().contains("Telegram refused it"));

    fake.fail(None);
    assert_eq!(
        put(
            &state,
            &host,
            json!({ "text": "Live", "image_source": "none" })
        )
        .await
        .0,
        StatusCode::OK
    );
    let again = row(&state).await;
    assert_eq!(again.state, "scheduled");
    assert!(again.error.is_none());
}

#[tokio::test]
async fn a_pin_that_fails_still_counts_as_sent() {
    let (state, _temp, fake, _r2, host) = setup().await;
    assert_eq!(
        put(
            &state,
            &host,
            json!({ "text": "Live", "image_source": "none" })
        )
        .await
        .0,
        StatusCode::OK
    );
    fake.fail(Some("pinchatmessage"));
    on_air(&state, EPISODE, Utc::now()).await;
    let sent = row(&state).await;
    assert_eq!(sent.state, "sent");
    assert!(!sent.pinned);
    assert!(sent.error.unwrap().starts_with("Sent, but not pinned"));
}

#[tokio::test]
async fn nothing_goes_out_when_switched_off_or_cancelled() {
    let (state, _temp, fake, _r2, host) = setup().await;
    assert_eq!(
        put(
            &state,
            &host,
            json!({ "text": "Live", "image_source": "none" })
        )
        .await
        .0,
        StatusCode::OK
    );

    db::set_setting(&state.db, db::TELEGRAM_ONAIR_ENABLED, "false")
        .await
        .unwrap();
    on_air(&state, EPISODE, Utc::now()).await;
    assert!(to_channel(&fake).is_empty());
    assert_eq!(row(&state).await.state, "scheduled");

    db::set_setting(&state.db, db::TELEGRAM_ONAIR_ENABLED, "true")
        .await
        .unwrap();
    let (code, cancelled) = cancel(&state, &host).await;
    assert_eq!(code, StatusCode::OK);
    assert_eq!(cancelled["state"], "cancelled");
    on_air(&state, EPISODE, Utc::now()).await;
    assert!(to_channel(&fake).is_empty());
}

#[tokio::test]
async fn the_tick_cancels_missed_fails_interrupted_and_unpins_off_air() {
    let (state, _temp, fake, _r2, _host) = setup().await;
    let now = Utc::now();
    for (show, title) in [(8, "Missed"), (9, "Cut off"), (10, "Pinned")] {
        sqlx::query(
            "INSERT INTO shows (id, title, date, show_type, start_time) \
             VALUES (?, ?, '2030-06-05', 'external', '20:00')",
        )
        .bind(show)
        .bind(title)
        .execute(&state.db)
        .await
        .unwrap();
    }
    sqlx::query(
        "INSERT INTO telegram_onair_messages (show_id, state, text, scheduled_at, send_claimed_at, \
           chat_id, message_id, pinned) VALUES \
         (8, 'scheduled', 'a', ?, NULL, NULL, NULL, 0), \
         (9, 'scheduled', 'b', ?, ?, NULL, NULL, 0), \
         (10, 'sent', 'c', ?, NULL, ?, 77, 1)",
    )
    .bind(to_db_time(now - Duration::hours(13)))
    .bind(to_db_time(now))
    .bind(to_db_time(now - Duration::minutes(20)))
    .bind(to_db_time(now - Duration::hours(2)))
    .bind(CHANNEL)
    .execute(&state.db)
    .await
    .unwrap();

    tick(&state, now).await;

    let state_of = |show: i64| {
        let state = state.clone();
        async move { load(&state, show).await.unwrap().unwrap() }
    };
    let missed = state_of(8).await;
    assert_eq!(missed.state, "cancelled");
    assert!(missed.error.unwrap().contains("did not go live"));
    let cut_off = state_of(9).await;
    assert_eq!(cut_off.state, "failed");
    assert!(cut_off.send_claimed_at.is_none());
    assert!(!state_of(10).await.pinned);

    let unpins = fake.of("unpinchatmessage");
    assert_eq!(unpins.len(), 1, "{:?}", fake.calls());
    assert_eq!(unpins[0].int("chat_id"), Some(CHANNEL));
    assert_eq!(unpins[0].int("message_id"), Some(77));
}
