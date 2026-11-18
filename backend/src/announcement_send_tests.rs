//! Sending announcements against a fake Graph API, fake R2, fake Telegram and
//! real SQLite, with `now` injected.

use super::*;
use crate::broadcast_tests::{add_user, headers};
use crate::fake_instagram::FakeInstagram;
use crate::fake_r2::FakeR2;
use crate::fake_telegram::{telegram_state, FakeTelegram};
use crate::handlers::announcements as ann;
use axum::{
    body::to_bytes,
    extract::{Path, Query, State},
    http::StatusCode,
    response::IntoResponse,
};
use chrono::TimeZone;
use serde_json::{json, Value};

const SHOW: i64 = 1;

async fn setup() -> (
    Arc<AppState>,
    tempfile::TempDir,
    FakeTelegram,
    FakeInstagram,
    FakeR2,
) {
    let (r2, endpoint) = FakeR2::serve().await;
    let (state, temp, tg) = telegram_state(json!({
        "r2_endpoint": endpoint,
        "instagram_access_token_dev": "dev-env-token",
        "instagram_business_account_id_dev": "ig-dev",
    }))
    .await;
    let ig = FakeInstagram::serve().await;
    (state, temp, tg, ig, r2)
}

/// Berlin local time (June 2030, UTC+2) as UTC.
fn berlin(day: u32, hour: u32) -> DateTime<Utc> {
    Berlin
        .with_ymd_and_hms(2030, 6, day, hour, 0, 0)
        .unwrap()
        .with_timezone(&Utc)
}

/// Episode `id` of series "Late Night Bass" (@nightbass) on 2030-06-`day`,
/// 20:00–22:00 Berlin.
async fn episode(state: &Arc<AppState>, id: i64, host: i64, day: u32) {
    let (series, _) = crate::series::insert_series(
        &state.db,
        host,
        &format!("Late Night Bass {id}"),
        None,
        None,
    )
    .await
    .unwrap();
    sqlx::query("UPDATE show_templates SET name = 'Late Night Bass', instagram_handle = 'nightbass' WHERE id = ?")
        .bind(series)
        .execute(&state.db)
        .await
        .unwrap();
    sqlx::query(
        "INSERT INTO shows (id, title, date, show_type, host_user_id, start_time, end_time, \
           template_id) VALUES (?, 'Bass Episode', ?, 'external', ?, '20:00', '22:00', ?)",
    )
    .bind(id)
    .bind(format!("2030-06-{day:02}"))
    .bind(host)
    .bind(series)
    .execute(&state.db)
    .await
    .unwrap();
}

async fn add(
    state: &Arc<AppState>,
    show: i64,
    slot: &str,
    row_state: &str,
    at: Option<DateTime<Utc>>,
    created_by: i64,
) -> i64 {
    sqlx::query(
        "INSERT INTO announcements (show_id, slot, state, scheduled_at, headline, caption, \
           created_by) VALUES (?, ?, ?, ?, 'Tonight', 'Deep cuts all night.', ?)",
    )
    .bind(show)
    .bind(slot)
    .bind(row_state)
    .bind(at.map(to_db_time))
    .bind(created_by)
    .execute(&state.db)
    .await
    .unwrap()
    .last_insert_rowid()
}

async fn row(state: &Arc<AppState>, id: i64) -> Announcement {
    load(state, id).await.unwrap()
}

fn media_posts(ig: &FakeInstagram) -> Vec<crate::fake_instagram::IgCall> {
    ig.calls()
        .into_iter()
        .filter(|c| c.route == "POST /v24.0/ig-dev/media")
        .collect()
}

async fn outcome<T: IntoResponse>(result: Result<T>) -> (StatusCode, Value) {
    let response = match result {
        Ok(r) => r.into_response(),
        Err(e) => e.into_response(),
    };
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 1024 * 1024).await.unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

#[tokio::test]
async fn due_pre_is_sent_once_with_caption_alt_text_and_collaborator() {
    let (state, _t, tg, ig, r2) = setup().await;
    let host = add_user(&state, "host", "host").await;
    episode(&state, SHOW, host, 5).await;
    let id = add(&state, SHOW, "pre", "scheduled", Some(berlin(3, 18)), host).await;

    // Not due yet.
    check_due_announcements(&state, berlin(3, 17)).await;
    assert!(ig.calls().is_empty(), "{:?}", ig.routes());

    let now = berlin(3, 18) + Duration::seconds(30);
    check_due_announcements(&state, now).await;
    let a = row(&state, id).await;
    assert_eq!(a.state, "sent");
    assert_eq!(a.sent_at.as_deref(), Some(to_db_time(now).as_str()));
    assert_eq!(a.media_id.as_deref(), Some("media-1"));
    assert_eq!(
        a.permalink.as_deref(),
        Some("https://www.instagram.com/p/media-1/")
    );
    assert_eq!(a.send_claimed_at, None);

    let posts = media_posts(&ig);
    assert_eq!(posts.len(), 1);
    let post = &posts[0];
    assert_eq!(
        post.param("caption"),
        Some("Deep cuts all night.\n\n#moafunk")
    );
    assert_eq!(post.param("alt_text"), Some("Tonight – Late Night Bass"));
    assert_eq!(post.json("collaborators"), json!(["nightbass"]));
    assert!(post
        .param("image_url")
        .unwrap()
        .contains("shows/1/announcements/pre.jpg"));
    let jpeg = r2.object(&state, "shows/1/announcements/pre.jpg").unwrap();
    assert_eq!(
        image::guess_format(&jpeg).unwrap(),
        image::ImageFormat::Jpeg
    );

    // A second tick (or a restart) does not post again.
    check_due_announcements(&state, now + Duration::minutes(1)).await;
    assert_eq!(media_posts(&ig).len(), 1);

    let notes = tg.of("sendmessage");
    assert_eq!(notes.len(), 1);
    assert!(notes[0]
        .text()
        .contains("pre announcement sent: Bass Episode"));
    assert!(notes[0]
        .text()
        .contains("https://www.instagram.com/p/media-1/"));
    tg.assert_all_sends_in_topic();
}

#[tokio::test]
async fn hashtags_setting_and_no_collaborator() {
    let (state, _t, _tg, ig, _r2) = setup().await;
    let host = add_user(&state, "host", "host").await;
    episode(&state, SHOW, host, 5).await;
    let id = add(&state, SHOW, "pre", "scheduled", Some(berlin(3, 18)), host).await;
    sqlx::query("UPDATE announcements SET collaborator = 0, caption = NULL WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await
        .unwrap();
    crate::db::set_setting(&state.db, "announce_hashtags", "#moafunk #radio")
        .await
        .unwrap();

    check_due_announcements(&state, berlin(3, 18)).await;
    let post = &media_posts(&ig)[0];
    assert_eq!(post.param("caption"), Some("#moafunk #radio"));
    assert_eq!(post.param("collaborators"), None);
}

#[tokio::test]
async fn on_air_story_goes_out_at_go_live_unless_cancelled() {
    let (state, _t, _tg, ig, _r2) = setup().await;
    let host = add_user(&state, "host", "host").await;
    episode(&state, SHOW, host, 5).await;
    episode(&state, 2, host, 6).await;
    let live = add(&state, SHOW, "onair", "armed", Some(berlin(5, 20)), host).await;
    let off = add(&state, 2, "onair", "cancelled", Some(berlin(6, 20)), host).await;

    // The tick never sends an armed story by its time alone.
    check_due_announcements(&state, berlin(5, 21)).await;
    assert!(ig.calls().is_empty(), "{:?}", ig.routes());

    announce_on_air(&state, SHOW, berlin(5, 20)).await;
    let a = row(&state, live).await;
    assert_eq!(a.state, "sent");
    let story = &media_posts(&ig)[0];
    assert_eq!(story.param("media_type"), Some("STORIES"));
    assert_eq!(
        story.json("user_tags"),
        json!([{ "username": "nightbass" }])
    );
    // A reconnect of the same show does not post again.
    announce_on_air(&state, SHOW, berlin(5, 20)).await;
    assert_eq!(media_posts(&ig).len(), 1);

    announce_on_air(&state, 2, berlin(6, 20)).await;
    assert_eq!(media_posts(&ig).len(), 1);
    assert_eq!(row(&state, off).await.state, "cancelled");
}

#[tokio::test]
async fn an_armed_story_of_an_episode_that_never_went_live_expires() {
    let (state, _t, _tg, ig, _r2) = setup().await;
    let host = add_user(&state, "host", "host").await;
    episode(&state, SHOW, host, 5).await;
    let story = add(&state, SHOW, "onair", "armed", Some(berlin(5, 20)), host).await;

    // Still armed during and soon after the booked time: the host may start late.
    check_due_announcements(&state, berlin(6, 7)).await;
    assert_eq!(row(&state, story).await.state, "armed");

    check_due_announcements(&state, berlin(6, 9)).await;
    let a = row(&state, story).await;
    assert_eq!(a.state, "cancelled");
    assert!(a.error.unwrap_or_default().contains("did not go live"));
    // A late go-live no longer posts it.
    announce_on_air(&state, SHOW, berlin(6, 10)).await;
    assert!(media_posts(&ig).is_empty());
}

#[tokio::test]
async fn post_unlock_sets_the_time_then_approval_and_pending_is_never_sent() {
    let (state, _t, _tg, ig, _r2) = setup().await;
    let host = add_user(&state, "host", "host").await;
    let admin = add_user(&state, "admin", "admin").await;
    episode(&state, SHOW, host, 5).await;
    episode(&state, 2, host, 12).await;
    let by_host = add(&state, SHOW, "post", "waiting_soundcloud", None, host).await;
    let by_admin = add(&state, 2, "post", "waiting_soundcloud", None, admin).await;

    // Not public yet: nothing changes.
    unlock_waiting_posts(&state, berlin(5, 23)).await.unwrap();
    assert_eq!(row(&state, by_host).await.state, "waiting_soundcloud");

    sqlx::query("UPDATE shows SET soundcloud_public = 1")
        .execute(&state.db)
        .await
        .unwrap();
    unlock_waiting_posts(&state, berlin(5, 23)).await.unwrap();
    let a = row(&state, by_host).await;
    assert_eq!(a.state, "pending_approval", "a new host needs approval");
    assert_eq!(a.scheduled_at, Some(to_db_time(berlin(6, 18))));
    // 18:00 on the 6th is 2 days − 2 h after the 5th 20:00 start.
    assert_eq!(a.anchor_offset_minutes, Some(22 * 60));
    let b = row(&state, by_admin).await;
    assert_eq!(b.state, "scheduled");
    // The 6th is taken (one feed post a day): the next day.
    assert_eq!(b.scheduled_at, Some(to_db_time(berlin(7, 18))));

    // Pending stays pending after its time; only the approved one goes out.
    check_due_announcements(&state, berlin(7, 19)).await;
    assert_eq!(row(&state, by_host).await.state, "pending_approval");
    assert_eq!(row(&state, by_admin).await.state, "sent");
    assert_eq!(media_posts(&ig).len(), 1);
}

#[tokio::test]
async fn full_day_pushes_the_post_to_the_next_free_slot() {
    let (state, _t, _tg, _ig, _r2) = setup().await;
    let admin = add_user(&state, "admin", "admin").await;
    crate::db::set_setting(&state.db, "announce_feed_per_day", "2")
        .await
        .unwrap();
    episode(&state, SHOW, admin, 5).await;
    episode(&state, 2, admin, 9).await;
    episode(&state, 3, admin, 10).await;
    // The 6th: evening taken, noon still free.
    add(&state, 2, "pre", "scheduled", Some(berlin(6, 18)), admin).await;
    let post = add(&state, SHOW, "post", "waiting_soundcloud", None, admin).await;
    sqlx::query("UPDATE shows SET soundcloud_public = 1 WHERE id = 1")
        .execute(&state.db)
        .await
        .unwrap();
    unlock_waiting_posts(&state, berlin(5, 23)).await.unwrap();
    assert_eq!(
        row(&state, post).await.scheduled_at,
        Some(to_db_time(berlin(6, 12)))
    );

    // The 6th full: the 7th.
    sqlx::query(
        "UPDATE announcements SET state = 'waiting_soundcloud', scheduled_at = NULL WHERE id = ?",
    )
    .bind(post)
    .execute(&state.db)
    .await
    .unwrap();
    add(&state, 3, "pre", "scheduled", Some(berlin(6, 12)), admin).await;
    unlock_waiting_posts(&state, berlin(5, 23)).await.unwrap();
    assert_eq!(
        row(&state, post).await.scheduled_at,
        Some(to_db_time(berlin(7, 18)))
    );
}

#[tokio::test]
async fn paused_sends_nothing() {
    let (state, _t, _tg, ig, _r2) = setup().await;
    let host = add_user(&state, "host", "host").await;
    episode(&state, SHOW, host, 5).await;
    let pre = add(&state, SHOW, "pre", "scheduled", Some(berlin(3, 18)), host).await;
    let story = add(&state, SHOW, "onair", "armed", Some(berlin(5, 20)), host).await;
    crate::db::set_setting(&state.db, "announce_paused", "true")
        .await
        .unwrap();

    check_due_announcements(&state, berlin(5, 20)).await;
    announce_on_air(&state, SHOW, berlin(5, 20)).await;
    assert!(ig.calls().is_empty(), "{:?}", ig.routes());
    assert_eq!(row(&state, pre).await.state, "scheduled");
    assert_eq!(row(&state, story).await.state, "armed");
}

#[tokio::test]
async fn a_failed_send_keeps_the_error_and_an_admin_can_retry() {
    let (state, _t, tg, ig, _r2) = setup().await;
    let host = add_user(&state, "host", "host").await;
    let admin = add_user(&state, "admin", "admin").await;
    episode(&state, SHOW, host, 5).await;
    let id = add(&state, SHOW, "pre", "scheduled", Some(berlin(3, 18)), host).await;
    ig.fail_publish(true);

    check_due_announcements(&state, berlin(3, 18)).await;
    let a = row(&state, id).await;
    assert_eq!(a.state, "failed");
    let error = a.error.unwrap();
    assert!(error.contains("not an Instagram Business"), "{error}");
    assert!(!error.contains("dev-env-token"), "{error}");
    let notes = tg.of("sendmessage");
    assert_eq!(notes.len(), 1);
    assert!(notes[0].text().contains("pre announcement failed"));
    tg.assert_all_sends_in_topic();

    // Only failed rows can be retried; the host can't.
    let retry = |user| {
        let state = state.clone();
        async move {
            outcome(
                ann::api_admin_retry_announcement(
                    State(state.clone()),
                    Path(id),
                    headers(&state, user).await,
                )
                .await,
            )
            .await
        }
    };
    assert_eq!(retry(host).await.0, StatusCode::FORBIDDEN);
    let (status, body) = retry(admin).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["state"], "scheduled");
    assert_eq!(body["error"], Value::Null);
    let at = body["scheduled_at"].as_str().unwrap().to_string();
    assert!(at > to_db_time(Utc::now()), "next free slot: {at}");
    assert_eq!(retry(admin).await.0, StatusCode::CONFLICT);

    ig.fail_publish(false);
    let at = DateTime::parse_from_rfc3339(&at)
        .unwrap()
        .with_timezone(&Utc);
    check_due_announcements(&state, at).await;
    assert_eq!(row(&state, id).await.state, "sent");
}

#[tokio::test]
async fn used_up_quota_keeps_it_scheduled_with_one_notice_a_day() {
    let (state, _t, tg, ig, _r2) = setup().await;
    let host = add_user(&state, "host", "host").await;
    episode(&state, SHOW, host, 5).await;
    let id = add(&state, SHOW, "pre", "scheduled", Some(berlin(3, 18)), host).await;
    ig.set_quota(100, 100);

    check_due_announcements(&state, berlin(3, 18)).await;
    check_due_announcements(&state, berlin(3, 19)).await;
    let a = row(&state, id).await;
    assert_eq!(a.state, "scheduled");
    assert_eq!(a.send_claimed_at, None);
    assert!(media_posts(&ig).is_empty());
    let notes = tg.of("sendmessage");
    assert_eq!(notes.len(), 1, "one notice a day");
    assert!(notes[0].text().contains("quota used up"));

    ig.set_quota(3, 100);
    check_due_announcements(&state, berlin(3, 20)).await;
    assert_eq!(row(&state, id).await.state, "sent");
}

#[tokio::test]
async fn a_send_cut_off_by_a_restart_becomes_failed_not_resent() {
    let (state, _t, _tg, ig, _r2) = setup().await;
    let host = add_user(&state, "host", "host").await;
    episode(&state, SHOW, host, 5).await;
    let id = add(&state, SHOW, "pre", "scheduled", Some(berlin(3, 18)), host).await;
    sqlx::query("UPDATE announcements SET send_claimed_at = ? WHERE id = ?")
        .bind(to_db_time(berlin(3, 18)))
        .bind(id)
        .execute(&state.db)
        .await
        .unwrap();

    check_due_announcements(&state, berlin(3, 18) + Duration::minutes(5)).await;
    assert_eq!(row(&state, id).await.state, "scheduled", "still in flight");
    check_due_announcements(&state, berlin(3, 18) + Duration::minutes(20)).await;
    let a = row(&state, id).await;
    assert_eq!(a.state, "failed");
    assert!(a.error.unwrap().contains("interrupted"));
    assert!(ig.calls().is_empty(), "{:?}", ig.routes());
}

#[tokio::test]
async fn admin_queue_list_approve_reject_and_status() {
    let (state, _t, _tg, _ig, _r2) = setup().await;
    let host = add_user(&state, "host", "host").await;
    let admin = add_user(&state, "admin", "admin").await;
    episode(&state, SHOW, host, 5).await;
    episode(&state, 2, host, 20).await;
    let pre = add(
        &state,
        SHOW,
        "pre",
        "pending_approval",
        Some(berlin(3, 18)),
        host,
    )
    .await;
    let story = add(
        &state,
        SHOW,
        "onair",
        "pending_approval",
        Some(berlin(5, 20)),
        host,
    )
    .await;
    let other = add(&state, 2, "pre", "scheduled", Some(berlin(18, 18)), host).await;
    // Sent late in the evening of the 5th: Berlin day 5, UTC still day 5.
    sqlx::query("UPDATE announcements SET state = 'sent', sent_at = ? WHERE id = ?")
        .bind(to_db_time(berlin(5, 23)))
        .bind(story)
        .execute(&state.db)
        .await
        .unwrap();

    let list = |user, from: &str, to: &str| {
        let state = state.clone();
        let q = ann::RangeQuery {
            from: from.to_string(),
            to: to.to_string(),
        };
        async move {
            outcome(
                ann::api_admin_list_announcements(
                    State(state.clone()),
                    Query(q),
                    headers(&state, user).await,
                )
                .await,
            )
            .await
        }
    };
    assert_eq!(
        list(host, "2030-06-01", "2030-06-30").await.0,
        StatusCode::FORBIDDEN
    );
    let (status, body) = list(admin, "2030-06-03", "2030-06-05").await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let ids: Vec<i64> = body
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["id"].as_i64().unwrap())
        .collect();
    assert_eq!(ids, [pre, story], "`to` is inclusive, the 18th is out");
    let first = &body[0];
    assert_eq!(first["show_title"], "Bass Episode");
    assert_eq!(first["series_name"], "Late Night Bass");
    assert_eq!(first["host_handle"], "nightbass");
    assert_eq!(first["slot"], "pre");
    assert_eq!(first["state"], "pending_approval");
    assert_eq!(first["scheduled_at"], to_db_time(berlin(3, 18)));
    assert_eq!(first["sent_at"], Value::Null);
    assert_eq!(first["permalink"], Value::Null);
    assert_eq!(
        list(admin, "2030-06-03", "bad").await.0,
        StatusCode::BAD_REQUEST
    );

    let act = |user, id, what: &'static str| {
        let state = state.clone();
        async move {
            let h = headers(&state, user).await;
            let s = State(state.clone());
            outcome(match what {
                "approve" => ann::api_admin_approve_announcement(s, Path(id), h)
                    .await
                    .map(IntoResponse::into_response),
                _ => ann::api_admin_reject_announcement(s, Path(id), h)
                    .await
                    .map(IntoResponse::into_response),
            })
            .await
        }
    };
    assert_eq!(act(host, pre, "approve").await.0, StatusCode::FORBIDDEN);
    assert_eq!(act(host, other, "reject").await.0, StatusCode::FORBIDDEN);

    // A future time stays.
    let (status, body) = act(admin, pre, "approve").await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["state"], "scheduled");
    assert_eq!(body["scheduled_at"], to_db_time(berlin(3, 18)));
    assert_eq!(act(admin, pre, "approve").await.0, StatusCode::CONFLICT);

    // A time that passed while pending moves to the next free slot.
    sqlx::query("UPDATE announcements SET state = 'pending_approval', scheduled_at = '2020-01-01T17:00:00Z' WHERE id = ?")
        .bind(pre)
        .execute(&state.db)
        .await
        .unwrap();
    let (_, body) = act(admin, pre, "approve").await;
    assert_eq!(body["state"], "scheduled");
    assert!(body["scheduled_at"].as_str().unwrap() > to_db_time(Utc::now()).as_str());

    let (status, body) = act(admin, other, "reject").await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["state"], "rejected");
    assert_eq!(
        act(admin, story, "reject").await.0,
        StatusCode::CONFLICT,
        "sent"
    );

    let status = |user| {
        let state = state.clone();
        async move {
            outcome(
                ann::api_admin_instagram_status(State(state.clone()), headers(&state, user).await)
                    .await,
            )
            .await
        }
    };
    assert_eq!(status(host).await.0, StatusCode::FORBIDDEN);
    let (code, body) = status(admin).await;
    assert_eq!(code, StatusCode::OK, "{body}");
    assert_eq!(
        body,
        json!({
            "account": "dev", "account_username": "moafunk_tester",
            "token_expires_at": null, "token_refreshed_at": null,
            "quota_used": 3, "quota_total": 100,
        })
    );
}

#[tokio::test]
async fn status_quota_is_null_when_instagram_fails() {
    let (state, _t, _tg, _ig, _r2) = setup().await;
    let admin = add_user(&state, "admin", "admin").await;
    // A dead Graph API host.
    crate::fake_instagram::API_BASE.with(|b| *b.borrow_mut() = "http://127.0.0.1:1".into());
    let (code, body) = outcome(
        ann::api_admin_instagram_status(State(state.clone()), headers(&state, admin).await).await,
    )
    .await;
    assert_eq!(code, StatusCode::OK);
    assert_eq!(body["quota_used"], Value::Null);
    assert_eq!(body["quota_total"], Value::Null);
}

#[tokio::test]
async fn boot_refresh_of_a_young_token_is_skipped_without_alert() {
    let (state, _t, tg, ig, _r2) = setup().await;
    ig.refresh_too_young();
    crate::instagram::refresh_tokens(&state).await;
    assert_eq!(ig.routes(), ["GET /refresh_access_token"]);
    assert!(tg.calls().is_empty(), "{:?}", tg.calls());
}

#[tokio::test]
async fn admin_post_buttons_use_the_stored_token() {
    let (state, _t, _tg, ig, _r2) = setup().await;
    sqlx::query(
        "INSERT INTO instagram_tokens (account, access_token, refreshed_at) \
         VALUES ('dev', 'dev-stored', '2000-01-01 00:00:00')",
    )
    .execute(&state.db)
    .await
    .unwrap();
    InstagramClient::for_account(&state.config, &state.db, "dev")
        .await
        .unwrap()
        .content_publishing_quota()
        .await
        .unwrap();
    assert_eq!(
        ig.call("/content_publishing_limit").param("access_token"),
        Some("dev-stored")
    );
    // Prod is not configured: the env error, not a stored token.
    let err = InstagramClient::for_account(&state.config, &state.db, "prod")
        .await
        .err()
        .unwrap();
    assert!(
        err.to_string().contains("INSTAGRAM_ACCESS_TOKEN_PROD"),
        "{err}"
    );
}
