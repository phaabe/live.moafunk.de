//! Announcements API against real handlers and SQLite.

use super::*;
use crate::broadcast_tests::{add_user, headers, state};
use axum::{
    body::{to_bytes, Body},
    extract::{FromRequest, Multipart, Path, Query},
    http::{Request, StatusCode},
    Json,
};
use handlers::announcements as ann;
use serde_json::{json, Value};

/// A future episode of `host`, 20:00 Berlin. 2030-06-05 is a Wednesday.
async fn episode(state: &Arc<AppState>, id: i64, host: i64, date: &str) {
    sqlx::query(
        "INSERT INTO shows (id, title, date, show_type, host_user_id, start_time) \
         VALUES (?, 'Episode', ?, 'external', ?, '20:00')",
    )
    .bind(id)
    .bind(date)
    .bind(host)
    .execute(&state.db)
    .await
    .unwrap();
}

async fn json_of(response: impl IntoResponse) -> (StatusCode, Value) {
    let response = response.into_response();
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 1024 * 1024).await.unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

/// The status and body of a handler result.
async fn outcome<T: IntoResponse>(result: Result<T>) -> (StatusCode, Value) {
    match result {
        Ok(r) => json_of(r).await,
        Err(e) => json_of(e).await,
    }
}

/// Pre and post need a headline; tests that don't care get one.
fn with_headline(slot: &str, mut body: Value) -> Value {
    if slot != "onair" && body.get("headline").is_none() {
        body["headline"] = json!("Tonight");
    }
    body
}

async fn put(
    state: &Arc<AppState>,
    user: i64,
    show: i64,
    slot: &str,
    body: Value,
) -> (StatusCode, Value) {
    outcome(
        ann::api_put_announcement(
            State(state.clone()),
            Path((show, slot.to_string())),
            headers(state, user).await,
            Json(serde_json::from_value(with_headline(slot, body)).unwrap()),
        )
        .await,
    )
    .await
}

async fn cancel(state: &Arc<AppState>, user: i64, show: i64, slot: &str) -> (StatusCode, Value) {
    outcome(
        ann::api_cancel_announcement(
            State(state.clone()),
            Path((show, slot.to_string())),
            headers(state, user).await,
        )
        .await,
    )
    .await
}

async fn list(state: &Arc<AppState>, user: i64, show: i64) -> (StatusCode, Value) {
    outcome(
        ann::api_list_announcements(State(state.clone()), Path(show), headers(state, user).await)
            .await,
    )
    .await
}

async fn slots(state: &Arc<AppState>, user: i64, show: i64, slot: &str) -> (StatusCode, Value) {
    outcome(
        ann::api_announcement_slots(
            State(state.clone()),
            Path(show),
            Query(ann::SlotsQuery {
                slot: slot.to_string(),
            }),
            headers(state, user).await,
        )
        .await,
    )
    .await
}

async fn set(state: &Arc<AppState>, key: &str, value: &str) {
    db::set_setting(&state.db, key, value).await.unwrap();
}

async fn mark(state: &Arc<AppState>, show: i64, slot: &str, new_state: &str) {
    sqlx::query("UPDATE announcements SET state = ? WHERE show_id = ? AND slot = ?")
        .bind(new_state)
        .bind(show)
        .bind(slot)
        .execute(&state.db)
        .await
        .unwrap();
}

/// 18:00 / 12:00 Berlin (summer time, UTC+2) on a June 2030 day.
fn at(day: u32, hour: u32) -> String {
    format!("2030-06-{day:02}T{:02}:00:00Z", hour - 2)
}

#[tokio::test]
async fn pre_defaults_to_two_days_before_and_needs_approval_for_a_new_host() {
    let (state, _t) = state().await;
    let host = add_user(&state, "host", "host").await;
    episode(&state, 1, host, "2030-06-05").await;

    let (status, row) = put(&state, host, 1, "pre", json!({ "headline": "Tonight" })).await;
    assert_eq!(status, StatusCode::OK, "{row}");
    assert_eq!(row["scheduled_at"], at(3, 18));
    assert_eq!(row["anchor_offset_minutes"], -(2 * 24 * 60 + 120));
    assert_eq!(row["state"], "pending_approval");
    assert_eq!(row["image_source"], "cover");
    assert_eq!(row["collaborator"], true);

    // On air: the episode start, a story, no slot time needed.
    let (status, row) = put(&state, host, 1, "onair", json!({})).await;
    assert_eq!(status, StatusCode::OK, "{row}");
    assert_eq!(row["scheduled_at"], "2030-06-05T18:00:00Z");
    assert_eq!(row["anchor_offset_minutes"], 0);

    // Post without a time: set later when the track goes public.
    let (status, row) = put(&state, host, 1, "post", json!({})).await;
    assert_eq!(status, StatusCode::OK, "{row}");
    assert_eq!(row["scheduled_at"], Value::Null);
    assert_eq!(row["anchor_offset_minutes"], Value::Null);
    assert_eq!(row["state"], "waiting_soundcloud");

    let (_, body) = list(&state, host, 1).await;
    let slots: Vec<&str> = body
        .as_array()
        .unwrap()
        .iter()
        .map(|a| a["slot"].as_str().unwrap())
        .collect();
    assert_eq!(slots, ["pre", "onair", "post"]);
}

#[tokio::test]
async fn approval_modes_and_admin_skip() {
    let (state, _t) = state().await;
    let host = add_user(&state, "host", "host").await;
    let admin = add_user(&state, "admin", "admin").await;
    for (id, date) in [
        (1, "2030-06-05"),
        (2, "2030-06-12"),
        (3, "2030-06-19"),
        (4, "2030-06-26"),
    ] {
        episode(&state, id, host, date).await;
    }

    // Admin: never pending; on-air armed.
    let (_, row) = put(&state, admin, 1, "onair", json!({})).await;
    assert_eq!(row["state"], "armed");
    let (_, row) = put(&state, admin, 1, "pre", json!({})).await;
    assert_eq!(row["state"], "scheduled");

    // first_two: after two sent posts, the host's posts go straight out.
    put(&state, host, 2, "pre", json!({})).await;
    put(&state, host, 3, "pre", json!({})).await;
    mark(&state, 2, "pre", "sent").await;
    let (_, row) = put(&state, host, 4, "onair", json!({})).await;
    assert_eq!(
        row["state"], "pending_approval",
        "one sent post is not enough"
    );
    mark(&state, 3, "pre", "sent").await;
    let (_, row) = put(&state, host, 4, "onair", json!({})).await;
    assert_eq!(row["state"], "armed");

    set(&state, "announce_approval_mode", "always").await;
    let (_, row) = put(&state, host, 4, "pre", json!({})).await;
    assert_eq!(row["state"], "pending_approval");

    set(&state, "announce_approval_mode", "never").await;
    let fresh = add_user(&state, "fresh", "host").await;
    episode(&state, 5, fresh, "2030-07-03").await;
    let (_, row) = put(&state, fresh, 5, "pre", json!({})).await;
    assert_eq!(row["state"], "scheduled");
}

#[tokio::test]
async fn slot_clash_day_and_not_a_slot() {
    let (state, _t) = state().await;
    let host = add_user(&state, "host", "host").await;
    episode(&state, 1, host, "2030-06-05").await;
    episode(&state, 2, host, "2030-06-06").await;

    let (status, _) = put(&state, host, 1, "pre", json!({ "scheduled_at": at(3, 18) })).await;
    assert_eq!(status, StatusCode::OK);

    let (status, body) = put(&state, host, 2, "pre", json!({ "scheduled_at": at(3, 18) })).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert!(body.to_string().contains("already taken"), "{body}");

    let (status, body) = put(&state, host, 2, "pre", json!({ "scheduled_at": at(3, 12) })).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert!(body.to_string().contains("per day"), "{body}");

    let (status, body) = put(&state, host, 2, "pre", json!({ "scheduled_at": at(4, 17) })).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(body.to_string().contains("only go out at"), "{body}");

    // Must be before the start; must be in the future.
    let (status, _) = put(&state, host, 2, "pre", json!({ "scheduled_at": at(7, 18) })).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    episode(&state, 3, host, "2020-01-08").await;
    let (status, _) = put(&state, host, 3, "pre", json!({})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);

    // Slots endpoint shows the taken time with its reason.
    let (status, body) = slots(&state, host, 2, "pre").await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let times = body.as_array().unwrap();
    let taken = times
        .iter()
        .find(|t| t["scheduled_at"] == at(3, 18))
        .unwrap();
    assert_eq!(taken["free"], false);
    assert!(taken["reason"].as_str().unwrap().contains("taken"));
    let free = times
        .iter()
        .find(|t| t["scheduled_at"] == at(5, 18))
        .unwrap();
    assert_eq!(free["free"], true);
    assert!(times
        .iter()
        .all(|t| t["scheduled_at"].as_str().unwrap() < "2030-06-06T18:00:00Z"));
    // Default: pre_days_before (2) days ahead, evening slot.
    let defaults = |body: &Value| -> Vec<Value> {
        body.as_array()
            .unwrap()
            .iter()
            .filter(|t| t["default"] == true)
            .cloned()
            .collect()
    };
    let d = defaults(&body);
    assert_eq!(d.len(), 1);
    assert_eq!(d[0]["scheduled_at"], at(4, 18));

    // That evening taken (and its noon refused by the day limit): the
    // default falls back to another free time.
    put(&state, host, 1, "pre", json!({ "scheduled_at": at(4, 18) })).await;
    let d = defaults(&slots(&state, host, 2, "pre").await.1);
    assert_eq!(d.len(), 1);
    assert_eq!(d[0]["free"], true);
    assert_ne!(d[0]["scheduled_at"], at(4, 18));

    // Post: empty while the track is not public.
    assert_eq!(slots(&state, host, 2, "post").await.1, json!([]));
    sqlx::query("UPDATE shows SET soundcloud_public = 1 WHERE id = 2")
        .execute(&state.db)
        .await
        .unwrap();
    let (_, body) = slots(&state, host, 2, "post").await;
    assert!(!body.as_array().unwrap().is_empty());
    assert_eq!(defaults(&body).len(), 1);
}

#[tokio::test]
async fn feed_per_week_stories_per_day_and_per_episode() {
    let (state, _t) = state().await;
    let host = add_user(&state, "host", "host").await;
    // Mon 2030-06-03 .. Sun 2030-06-09; episodes the day after each post.
    for day in 3..=9u32 {
        episode(&state, day as i64, host, &format!("2030-06-{:02}", day + 1)).await;
    }
    for day in 3..=7u32 {
        let (status, body) = put(
            &state,
            host,
            day as i64,
            "pre",
            json!({ "scheduled_at": at(day, 18) }),
        )
        .await;
        assert_eq!(status, StatusCode::OK, "{body}");
    }
    let (status, body) = put(&state, host, 8, "pre", json!({ "scheduled_at": at(8, 18) })).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert!(body.to_string().contains("per week"), "{body}");

    // A cancelled post frees its place in the week.
    cancel(&state, host, 3, "pre").await;
    let (status, _) = put(&state, host, 8, "pre", json!({ "scheduled_at": at(8, 18) })).await;
    assert_eq!(status, StatusCode::OK);

    // Stories per day.
    set(&state, "announce_stories_per_day", "1").await;
    episode(&state, 20, host, "2030-07-01").await;
    sqlx::query(
        "INSERT INTO shows (id, title, date, show_type, host_user_id, start_time) \
         VALUES (21, 'Late', '2030-07-01', 'external', ?, '22:00')",
    )
    .bind(host)
    .execute(&state.db)
    .await
    .unwrap();
    assert_eq!(
        put(&state, host, 20, "onair", json!({})).await.0,
        StatusCode::OK
    );
    let (status, body) = put(&state, host, 21, "onair", json!({})).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert!(body.to_string().contains("stories"), "{body}");

    // Per episode, also counting a post without a time.
    set(&state, "announce_limit_per_episode", "1").await;
    assert_eq!(
        put(&state, host, 9, "post", json!({})).await.0,
        StatusCode::OK
    );
    let (status, body) = put(&state, host, 9, "onair", json!({})).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert!(body.to_string().contains("limit 1"), "{body}");
}

#[tokio::test]
async fn on_air_story_can_be_switched_on_after_the_booked_start() {
    let (state, _t) = state().await;
    let host = add_user(&state, "host", "host").await;
    // Started yesterday 20:00 Berlin: a host who goes live late.
    let yesterday = (chrono::Utc::now() - chrono::Duration::days(1))
        .format("%Y-%m-%d")
        .to_string();
    episode(&state, 1, host, &yesterday).await;

    let (status, row) = put(&state, host, 1, "onair", json!({})).await;
    assert_eq!(status, StatusCode::OK, "{row}");
    // A new host's first posts wait for approval; the point is: not refused.
    assert_eq!(row["state"], "pending_approval");
    // Feed posts still need a future time.
    let (status, body) = put(
        &state,
        host,
        1,
        "post",
        json!({ "scheduled_at": "2020-01-01T17:00:00Z" }),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
}

#[tokio::test]
async fn sent_is_final_and_cancelled_can_be_recreated() {
    let (state, _t) = state().await;
    let host = add_user(&state, "host", "host").await;
    episode(&state, 1, host, "2030-06-05").await;

    put(&state, host, 1, "pre", json!({})).await;
    let (status, row) = cancel(&state, host, 1, "pre").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(row["state"], "cancelled");

    let (status, row) = put(&state, host, 1, "pre", json!({ "headline": "Back" })).await;
    assert_eq!(status, StatusCode::OK, "{row}");
    assert_eq!(row["state"], "pending_approval");
    assert_eq!(row["headline"], "Back");

    mark(&state, 1, "pre", "sent").await;
    assert_eq!(
        put(&state, host, 1, "pre", json!({})).await.0,
        StatusCode::CONFLICT
    );
    // A sent row says so, even when the edit would also fail a field check.
    let (status, body) = put(&state, host, 1, "pre", json!({ "headline": "" })).await;
    assert_eq!(status, StatusCode::CONFLICT, "{body}");
    assert!(body.to_string().contains("already sent"), "{body}");
    assert_eq!(cancel(&state, host, 1, "pre").await.0, StatusCode::CONFLICT);
    assert_eq!(
        cancel(&state, host, 1, "post").await.0,
        StatusCode::NOT_FOUND
    );
}

#[tokio::test]
async fn only_the_host_or_an_admin_has_access() {
    let (state, _t) = state().await;
    let host = add_user(&state, "host", "host").await;
    let other = add_user(&state, "other", "host").await;
    let admin = add_user(&state, "admin", "admin").await;
    episode(&state, 1, host, "2030-06-05").await;
    put(&state, host, 1, "pre", json!({})).await;

    assert_eq!(list(&state, other, 1).await.0, StatusCode::FORBIDDEN);
    assert_eq!(
        put(&state, other, 1, "onair", json!({})).await.0,
        StatusCode::FORBIDDEN
    );
    assert_eq!(
        cancel(&state, other, 1, "pre").await.0,
        StatusCode::FORBIDDEN
    );

    assert_eq!(list(&state, admin, 1).await.0, StatusCode::OK);
    assert_eq!(
        put(&state, admin, 1, "onair", json!({})).await.0,
        StatusCode::OK
    );
    assert_eq!(cancel(&state, admin, 1, "pre").await.0, StatusCode::OK);
    assert_eq!(
        put(&state, admin, 1, "nope", json!({})).await.0,
        StatusCode::BAD_REQUEST
    );

    // Settings are admin-only.
    let host_get =
        ann::api_get_announcement_settings(State(state.clone()), headers(&state, host).await).await;
    assert_eq!(outcome(host_get).await.0, StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn text_and_image_rules() {
    let (state, _t) = state().await;
    let host = add_user(&state, "host", "host").await;
    episode(&state, 1, host, "2030-06-05").await;

    let long = "x".repeat(19);
    assert_eq!(
        put(&state, host, 1, "pre", json!({ "headline": long }))
            .await
            .0,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(
        put(
            &state,
            host,
            1,
            "pre",
            json!({ "caption": "x".repeat(601) })
        )
        .await
        .0,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(
        put(&state, host, 1, "pre", json!({ "layout": "grid" }))
            .await
            .0,
        StatusCode::BAD_REQUEST
    );

    for key in [
        "shows/2/announcements/uploads/a.png",
        "shows/1/announcements/uploads/",
        "shows/1/announcements/uploads/../../x.png",
        "covers/1.png",
    ] {
        let (status, body) = put(
            &state,
            host,
            1,
            "pre",
            json!({ "image_source": "upload", "image_key": key }),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{key}: {body}");
    }
    assert_eq!(
        put(&state, host, 1, "pre", json!({ "image_source": "guest" }))
            .await
            .0,
        StatusCode::BAD_REQUEST
    );

    let (status, row) = put(
        &state,
        host,
        1,
        "pre",
        json!({ "image_source": "guest", "image_key": "shows/1/announcements/uploads/g.jpg",
                "layout": "split", "collaborator": false }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{row}");
    assert_eq!(row["image_key"], "shows/1/announcements/uploads/g.jpg");
    assert_eq!(row["layout"], "split");
    assert_eq!(row["collaborator"], false);

    // Cover ignores a key.
    let (_, row) = put(
        &state,
        host,
        1,
        "pre",
        json!({ "image_source": "cover", "image_key": "covers/1.png" }),
    )
    .await;
    assert_eq!(row["image_key"], Value::Null);
}

#[tokio::test]
async fn admin_settings_round_trip() {
    let (state, _t) = state().await;
    let admin = add_user(&state, "admin", "admin").await;

    let (status, body) = outcome(
        ann::api_get_announcement_settings(State(state.clone()), headers(&state, admin).await)
            .await,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["limit_per_episode"], 3);
    assert_eq!(body["slots"], json!(["12:00", "18:00"]));
    assert_eq!(body["approval_mode"], "first_two");
    assert_eq!(body["pre_days_before"], 2);

    let new = json!({ "limit_per_episode": 2, "feed_per_day": 2, "feed_per_week": 7,
        "stories_per_day": 4, "pre_days_before": 3, "slots": ["19:30", "09:00"], "approval_mode": "always",
        "paused": true });
    let (status, body) = outcome(
        ann::api_put_announcement_settings(
            State(state.clone()),
            headers(&state, admin).await,
            Json(serde_json::from_value(new).unwrap()),
        )
        .await,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let saved = announcements::get_settings(&state.db).await;
    assert_eq!(saved.slots, ["09:00", "19:30"]);
    assert_eq!(saved.approval_mode, announcements::ApprovalMode::Always);
    assert!(saved.paused);
    assert_eq!(saved.feed_per_week, 7);
    assert_eq!(saved.pre_days_before, 3);
    assert_eq!(body["pre_days_before"], 3);

    let bad = json!({ "limit_per_episode": 2, "feed_per_day": 2, "feed_per_week": 7, "pre_days_before": 3,
        "stories_per_day": 4, "slots": ["7pm"], "approval_mode": "never", "paused": false });
    let (status, _) = outcome(
        ann::api_put_announcement_settings(
            State(state.clone()),
            headers(&state, admin).await,
            Json(serde_json::from_value(bad).unwrap()),
        )
        .await,
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);

    // A bad stored value falls back to the default.
    set(&state, "announce_feed_per_day", "lots").await;
    assert_eq!(announcements::get_settings(&state.db).await.feed_per_day, 1);
}

// ── Series Instagram handle ─────────────────────────────────────────────────

const BOUNDARY: &str = "announce-test-boundary";

async fn form(fields: &[(&str, &str)]) -> Multipart {
    let mut body = String::new();
    for (name, value) in fields {
        body.push_str(&format!(
            "--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n"
        ));
    }
    body.push_str(&format!("--{BOUNDARY}--\r\n"));
    let request = Request::builder()
        .header(
            "content-type",
            format!("multipart/form-data; boundary={BOUNDARY}"),
        )
        .body(Body::from(body))
        .unwrap();
    Multipart::from_request(request, &()).await.unwrap()
}

#[tokio::test]
async fn series_instagram_handle_is_validated_kept_and_cleared() {
    let (state, _t) = state().await;
    let host = add_user(&state, "host", "host").await;
    let base = [("name", "Brunchtime"), ("repeat_rule", "none")];

    let bad = [&base[..], &[("instagram_handle", "bad handle!")]].concat();
    let result = handlers::series::api_create_series(
        State(state.clone()),
        headers(&state, host).await,
        form(&bad).await,
    )
    .await;
    assert_eq!(outcome(result).await.0, StatusCode::BAD_REQUEST);
    let too_long = "a".repeat(31);
    let bad = [&base[..], &[("instagram_handle", too_long.as_str())]].concat();
    let result = handlers::series::api_create_series(
        State(state.clone()),
        headers(&state, host).await,
        form(&bad).await,
    )
    .await;
    assert_eq!(outcome(result).await.0, StatusCode::BAD_REQUEST);

    let good = [&base[..], &[("instagram_handle", " @moa.funk_ ")]].concat();
    let result = handlers::series::api_create_series(
        State(state.clone()),
        headers(&state, host).await,
        form(&good).await,
    )
    .await;
    let (status, body) = outcome(result).await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    assert_eq!(body["instagram_handle"], "moa.funk_");
    let slug = body["slug"].as_str().unwrap().to_string();

    let update = |fields: Vec<(&'static str, &'static str)>| {
        let state = state.clone();
        let slug = slug.clone();
        async move {
            let result = handlers::series::api_update_series(
                State(state.clone()),
                Path(slug),
                headers(&state, host).await,
                form(&fields).await,
            )
            .await;
            outcome(result).await
        }
    };
    // Missing field keeps it.
    let (_, body) = update(base.to_vec()).await;
    assert_eq!(body["instagram_handle"], "moa.funk_");
    // Empty clears it.
    let (_, body) = update([&base[..], &[("instagram_handle", "")]].concat()).await;
    assert_eq!(body["instagram_handle"], Value::Null);
}

#[tokio::test]
async fn headline_needed_for_feed_posts_only() {
    let (state, _t) = state().await;
    let host = add_user(&state, "host", "host").await;
    episode(&state, 1, host, "2030-06-05").await;
    for slot in ["pre", "post"] {
        let (status, body) = put(&state, host, 1, slot, json!({ "headline": " " })).await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{slot}: {body}");
    }
    let (status, row) = put(
        &state,
        host,
        1,
        "onair",
        json!({ "headline": "", "caption": "" }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{row}");
    assert_eq!(row["headline"], "");
    let (status, _) = put(
        &state,
        host,
        1,
        "pre",
        json!({ "headline": "Hi", "caption": "" }),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
}

#[tokio::test]
async fn pre_days_before_moves_the_default() {
    let (state, _t) = state().await;
    let host = add_user(&state, "host", "host").await;
    episode(&state, 1, host, "2030-06-15").await;
    set(&state, "announce_pre_days_before", "10").await;
    let (status, row) = put(&state, host, 1, "pre", json!({})).await;
    assert_eq!(status, StatusCode::OK, "{row}");
    assert_eq!(row["scheduled_at"], at(5, 18));
    // The slots window reaches back to that day; the own row does not block it.
    let (_, body) = slots(&state, host, 1, "pre").await;
    let times = body.as_array().unwrap();
    assert!(times.iter().any(|t| t["scheduled_at"] == at(5, 12)));
    let default = times.iter().find(|t| t["default"] == true).unwrap();
    assert_eq!(default["scheduled_at"], at(5, 18));
}

#[tokio::test]
async fn rejected_frees_its_slot_and_can_be_recreated() {
    let (state, _t) = state().await;
    let host = add_user(&state, "host", "host").await;
    episode(&state, 1, host, "2030-06-05").await;
    episode(&state, 2, host, "2030-06-06").await;
    put(&state, host, 1, "pre", json!({ "scheduled_at": at(3, 18) })).await;
    mark(&state, 1, "pre", "rejected").await;

    let (status, body) = put(&state, host, 2, "pre", json!({ "scheduled_at": at(3, 18) })).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let (status, row) = put(&state, host, 1, "pre", json!({ "scheduled_at": at(4, 18) })).await;
    assert_eq!(status, StatusCode::OK, "{row}");
    assert_eq!(row["state"], "pending_approval");
}

#[tokio::test]
async fn booking_arms_the_on_air_story() {
    let (_r2, endpoint) = crate::fake_r2::FakeR2::serve().await;
    let (state, _t) = crate::broadcast_tests::state_with(json!({ "r2_endpoint": endpoint })).await;
    let host = add_user(&state, "host", "host").await;
    let (_, slug) = crate::series::insert_series(&state.db, host, "Brunchtime", None, None)
        .await
        .unwrap();
    let request = serde_json::from_value(json!({
        "date": "2030-06-05", "start_time": "20:00", "end_time": "22:00", "mode": "live"
    }))
    .unwrap();
    let response = handlers::booking::api_book_episode(
        State(state.clone()),
        Path(slug),
        headers(&state, host).await,
        Json(request),
    )
    .await
    .unwrap();
    let (status, episode) = json_of(response).await;
    assert_eq!(status, StatusCode::CREATED, "{episode}");
    let id = episode["id"].as_i64().unwrap();

    let (_, body) = list(&state, host, id).await;
    let rows = body.as_array().unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["slot"], "onair");
    assert_eq!(rows[0]["state"], "armed");
    assert_eq!(rows[0]["scheduled_at"], "2030-06-05T18:00:00Z");
    assert_eq!(rows[0]["anchor_offset_minutes"], 0);
    assert_eq!(rows[0]["created_by"], host);

    // An existing row is kept.
    put(&state, host, id, "onair", json!({ "headline": "Live!" })).await;
    let mut conn = state.db.acquire().await.unwrap();
    announcements::arm_onair(&mut conn, id, "2030-06-05", "20:00", host)
        .await
        .unwrap();
    let (_, body) = list(&state, host, id).await;
    assert_eq!(body[0]["headline"], "Live!");
}

/// The static `slots` route and the `:slot` route live side by side.
#[test]
fn slots_route_does_not_clash_with_slot_param() {
    let _: axum::Router<Arc<AppState>> = axum::Router::new()
        .route(
            "/api/shows/:id/announcements/slots",
            axum::routing::get(ann::api_announcement_slots),
        )
        .route(
            "/api/shows/:id/announcements/meta",
            axum::routing::get(ann::api_announcement_meta),
        )
        .route(
            "/api/shows/:id/announcements/uploads",
            axum::routing::post(ann::api_upload_announcement_image),
        )
        .route(
            "/api/shows/:id/announcements/:slot",
            axum::routing::put(ann::api_put_announcement).delete(ann::api_cancel_announcement),
        );
}

// ── Host signals: meta, requested_at ────────────────────────────────────────

async fn meta(state: &Arc<AppState>, user: i64, show: i64) -> (StatusCode, Value) {
    outcome(
        ann::api_announcement_meta(State(state.clone()), Path(show), headers(state, user).await)
            .await,
    )
    .await
}

#[tokio::test]
async fn meta_tells_paused_and_needs_approval() {
    let (state, _t) = state().await;
    let host = add_user(&state, "host", "host").await;
    let other = add_user(&state, "other", "host").await;
    let admin = add_user(&state, "admin", "admin").await;
    episode(&state, 1, host, "2030-06-05").await;
    episode(&state, 2, host, "2030-06-12").await;

    assert_eq!(
        meta(&state, host, 1).await,
        (
            StatusCode::OK,
            json!({ "paused": false, "needs_approval": true })
        )
    );
    assert_eq!(
        meta(&state, admin, 1).await.1,
        json!({ "paused": false, "needs_approval": false })
    );
    assert_eq!(meta(&state, other, 1).await.0, StatusCode::FORBIDDEN);

    // Two sent posts: the host's next post goes straight out.
    put(&state, host, 1, "pre", json!({})).await;
    put(&state, host, 2, "pre", json!({})).await;
    mark(&state, 1, "pre", "sent").await;
    mark(&state, 2, "pre", "sent").await;
    set(&state, "announce_paused", "true").await;
    assert_eq!(
        meta(&state, host, 1).await.1,
        json!({ "paused": true, "needs_approval": false })
    );
}

#[tokio::test]
async fn approve_move_keeps_the_host_time_until_the_host_saves() {
    let (state, _t) = state().await;
    let host = add_user(&state, "host", "host").await;
    let admin = add_user(&state, "admin", "admin").await;
    episode(&state, 1, host, "2030-06-05").await;
    let (_, row) = put(&state, host, 1, "pre", json!({})).await;
    assert_eq!(row["requested_at"], Value::Null);
    let id = row["id"].as_i64().unwrap();

    // The time passed while it waited for approval: approve moves it.
    sqlx::query("UPDATE announcements SET scheduled_at = '2020-01-01T17:00:00Z' WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await
        .unwrap();
    let (status, body) = outcome(
        ann::api_admin_approve_announcement(
            State(state.clone()),
            Path(id),
            headers(&state, admin).await,
        )
        .await,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["requested_at"], "2020-01-01T17:00:00Z");
    let (_, rows) = list(&state, host, 1).await;
    assert_eq!(rows[0]["requested_at"], "2020-01-01T17:00:00Z");
    assert_ne!(rows[0]["scheduled_at"], "2020-01-01T17:00:00Z");

    // The host picks a time again: nothing was moved.
    let (status, row) = put(&state, host, 1, "pre", json!({ "scheduled_at": at(3, 18) })).await;
    assert_eq!(status, StatusCode::OK, "{row}");
    assert_eq!(row["requested_at"], Value::Null);
}

// ── Photo upload and preview ────────────────────────────────────────────────

fn encoded(format: image::ImageFormat) -> Vec<u8> {
    let img = image::DynamicImage::ImageRgb8(image::RgbImage::from_pixel(
        4,
        4,
        image::Rgb([200, 30, 30]),
    ));
    let mut out = std::io::Cursor::new(Vec::new());
    img.write_to(&mut out, format).unwrap();
    out.into_inner()
}

/// POSTs `data` as multipart field `field` to the upload route of `show`,
/// through a router with the production body limit.
async fn upload(
    state: &Arc<AppState>,
    user: i64,
    show: i64,
    field: &str,
    data: &[u8],
) -> (StatusCode, Value) {
    let app = axum::Router::new()
        .route(
            "/api/shows/:id/announcements/uploads",
            axum::routing::post(ann::api_upload_announcement_image),
        )
        .layer(axum::extract::DefaultBodyLimit::max(
            state.config.max_request_body_bytes(),
        ))
        .with_state(state.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });

    let mut body = format!(
        "--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"{field}\"; \
         filename=\"photo\"\r\nContent-Type: image/jpeg\r\n\r\n"
    )
    .into_bytes();
    body.extend_from_slice(data);
    body.extend_from_slice(format!("\r\n--{BOUNDARY}--\r\n").as_bytes());
    let response = reqwest::Client::new()
        .post(format!(
            "http://{addr}/api/shows/{show}/announcements/uploads"
        ))
        .headers(headers(state, user).await)
        .header(
            "content-type",
            format!("multipart/form-data; boundary={BOUNDARY}"),
        )
        .body(body)
        .send()
        .await
        .unwrap();
    let status = StatusCode::from_u16(response.status().as_u16()).unwrap();
    (status, response.json().await.unwrap_or(Value::Null))
}

#[tokio::test]
async fn photo_upload_takes_jpeg_and_png_only() {
    let (r2, endpoint) = crate::fake_r2::FakeR2::serve().await;
    let (state, _t) = crate::broadcast_tests::state_with(json!({ "r2_endpoint": endpoint })).await;
    let host = add_user(&state, "host", "host").await;
    let other = add_user(&state, "other", "host").await;
    episode(&state, 1, host, "2030-06-05").await;

    let jpeg = encoded(image::ImageFormat::Jpeg);
    let png = encoded(image::ImageFormat::Png);
    for (data, ext) in [(&jpeg, "jpg"), (&png, "png")] {
        let (status, body) = upload(&state, host, 1, "file", data).await;
        assert_eq!(status, StatusCode::OK, "{body}");
        let key = body["image_key"].as_str().unwrap();
        assert!(key.starts_with("shows/1/announcements/uploads/"), "{key}");
        assert!(key.ends_with(&format!(".{ext}")), "{key}");
        assert_eq!(r2.object(&state, key).as_ref(), Some(data));
    }

    // The bytes decide, not the declared content type.
    let (status, _) = upload(&state, host, 1, "file", b"GIF89a not a photo").await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    let mut big = jpeg.clone();
    big.resize(10 * 1024 * 1024 + 1, 0);
    let (status, body) = upload(&state, host, 1, "file", &big).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(body.to_string().contains("10 MB"), "{body}");
    assert_eq!(
        upload(&state, host, 1, "photo", &jpeg).await.0,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(
        upload(&state, other, 1, "file", &jpeg).await.0,
        StatusCode::FORBIDDEN
    );
    assert_eq!(
        r2.keys_in(
            &state.config.r2_bucket_name,
            "shows/1/announcements/uploads/"
        )
        .len(),
        2
    );
}

#[tokio::test]
async fn preview_renders_an_uploaded_photo_and_refuses_a_foreign_key() {
    use crate::announcement_image::{api_announcement_preview, PreviewQuery};
    let (_r2, endpoint) = crate::fake_r2::FakeR2::serve().await;
    let (state, _t) = crate::broadcast_tests::state_with(json!({ "r2_endpoint": endpoint })).await;
    let host = add_user(&state, "host", "host").await;
    episode(&state, 1, host, "2030-06-05").await;
    let (_, body) = upload(&state, host, 1, "file", &encoded(image::ImageFormat::Png)).await;
    let key = body["image_key"].as_str().unwrap().to_string();

    let preview = |source: &str, key: &str| {
        let state = state.clone();
        let image = json!({ "image_source": source, "image_key": key });
        async move {
            api_announcement_preview(
                State(state.clone()),
                Path((1, "pre".to_string())),
                Query(PreviewQuery {
                    headline: "Tonight".into(),
                    layout: Default::default(),
                }),
                Query(serde_json::from_value(image).unwrap()),
                headers(&state, host).await,
            )
            .await
            .map(IntoResponse::into_response)
        }
    };
    let response = preview("upload", &key).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = to_bytes(response.into_body(), 10 * 1024 * 1024)
        .await
        .unwrap();
    assert!(image::load_from_memory(&bytes).is_ok());

    for (source, key) in [
        ("upload", "shows/2/announcements/uploads/x.png"),
        ("guest", "covers/1.png"),
        ("poster", key.as_str()),
    ] {
        let status = outcome(preview(source, key).await).await.0;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{source} {key}");
    }
}
