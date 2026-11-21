//! Instagram stories, feed posts, publishing quota, account choice and token
//! refresh against a fake Graph API, a fake Telegram and real SQLite.

use super::*;
use crate::fake_instagram::FakeInstagram;
use crate::fake_telegram::{telegram_state, FakeTelegram};
use serde_json::{json, Value};

/// Dev account in the env, plus `extra` config.
fn dev_env(extra: Value) -> Value {
    let mut config = json!({
        "instagram_access_token_dev": "dev-env-token",
        "instagram_business_account_id_dev": "ig-dev",
    });
    config
        .as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    config
}

async fn setup(
    config: Value,
) -> (
    Arc<AppState>,
    tempfile::TempDir,
    FakeTelegram,
    FakeInstagram,
) {
    let (state, temp, telegram) = telegram_state(config).await;
    let instagram = FakeInstagram::serve().await;
    (state, temp, telegram, instagram)
}

async fn store_token(state: &AppState, account: &str, token: &str, refreshed_at: &str) {
    sqlx::query(
        "INSERT INTO instagram_tokens (account, access_token, expires_at, refreshed_at) \
         VALUES (?, ?, '2001-01-01 00:00:00', ?)",
    )
    .bind(account)
    .bind(token)
    .bind(refreshed_at)
    .execute(&state.db)
    .await
    .unwrap();
}

/// `(access_token, expires_at)` stored for `account`.
async fn stored(state: &AppState, account: &str) -> Option<(String, Option<String>)> {
    sqlx::query_as("SELECT access_token, expires_at FROM instagram_tokens WHERE account = ?")
        .bind(account)
        .fetch_optional(&state.db)
        .await
        .unwrap()
}

async fn client(state: &AppState) -> InstagramClient {
    InstagramClient::for_environment(&state.config, &state.db)
        .await
        .unwrap()
}

#[tokio::test]
async fn story_is_published_with_and_without_mention() {
    let (state, _temp, _tg, ig) = setup(dev_env(json!({}))).await;
    let client = client(&state).await;

    let published = client
        .publish_story_image("https://r2.test/story.jpg", Some("@host_handle"))
        .await
        .unwrap();
    assert_eq!(published.media_id, "media-1");
    assert_eq!(
        published.permalink.as_deref(),
        Some("https://www.instagram.com/p/media-1/")
    );
    assert_eq!(
        ig.routes(),
        [
            "GET /v24.0/ig-dev/content_publishing_limit",
            "POST /v24.0/ig-dev/media",
            "GET /v24.0/container-1",
            "POST /v24.0/ig-dev/media_publish",
            "GET /v24.0/media-1",
        ]
    );
    let media = ig.call("/ig-dev/media");
    assert_eq!(media.param("media_type"), Some("STORIES"));
    assert_eq!(media.param("image_url"), Some("https://r2.test/story.jpg"));
    assert_eq!(
        media.json("user_tags"),
        json!([{"username": "host_handle"}])
    );
    assert_eq!(media.param("access_token"), Some("dev-env-token"));
    assert_eq!(
        ig.call("/media_publish").param("creation_id"),
        Some("container-1")
    );

    client
        .publish_story_image("https://r2.test/story.jpg", None)
        .await
        .unwrap();
    let last_media = ig
        .calls()
        .into_iter()
        .rfind(|c| c.route.ends_with("/ig-dev/media"))
        .unwrap();
    assert_eq!(last_media.param("user_tags"), None);
}

#[tokio::test]
async fn feed_image_has_caption_alt_text_and_collaborator() {
    let (state, _temp, _tg, ig) = setup(dev_env(json!({}))).await;

    let published = client(&state)
        .await
        .publish_feed_image(
            "https://r2.test/feed.jpg",
            "Tonight on air",
            Some("Poster with the host photo"),
            &["@host_handle".to_string()],
            &[],
        )
        .await
        .unwrap();
    assert_eq!(published.media_id, "media-1");

    let media = ig.call("/ig-dev/media");
    assert_eq!(media.param("media_type"), None);
    assert_eq!(media.param("image_url"), Some("https://r2.test/feed.jpg"));
    assert_eq!(media.param("caption"), Some("Tonight on air"));
    assert_eq!(media.param("alt_text"), Some("Poster with the host photo"));
    assert_eq!(media.json("collaborators"), json!(["host_handle"]));
    assert_eq!(media.param("user_tags"), None);
}

#[tokio::test]
async fn feed_image_tags_accounts_side_by_side() {
    let (state, _temp, _tg, ig) = setup(dev_env(json!({}))).await;

    client(&state)
        .await
        .publish_feed_image(
            "https://r2.test/feed.jpg",
            "Tonight on air",
            None,
            &[],
            &[
                "@guest".to_string(),
                "label".to_string(),
                "dj.three".to_string(),
            ],
        )
        .await
        .unwrap();

    let media = ig.call("/ig-dev/media");
    assert_eq!(
        media.json("user_tags"),
        json!([
            { "username": "guest", "x": 0.25, "y": 0.5 },
            { "username": "label", "x": 0.5, "y": 0.5 },
            { "username": "dj.three", "x": 0.75, "y": 0.5 }
        ])
    );
}

#[tokio::test]
async fn quota_is_read_from_the_api() {
    let (state, _temp, _tg, ig) = setup(dev_env(json!({}))).await;
    ig.set_quota(7, 50);

    let quota = client(&state)
        .await
        .content_publishing_quota()
        .await
        .unwrap();
    assert_eq!(quota, (7, 50));
    let call = ig.call("/content_publishing_limit");
    assert_eq!(call.param("fields"), Some("quota_usage,config"));
}

#[tokio::test]
async fn used_up_quota_refuses_to_publish() {
    let (state, _temp, _tg, ig) = setup(dev_env(json!({}))).await;
    ig.set_quota(100, 100);
    let client = client(&state).await;

    let err = client
        .publish_story_image("https://r2.test/story.jpg", None)
        .await
        .unwrap_err();
    assert!(err.to_string().contains("quota used up"), "{err}");
    let err = client
        .publish_feed_image("https://r2.test/feed.jpg", "x", None, &[], &[])
        .await
        .unwrap_err();
    assert!(err.to_string().contains("quota used up"), "{err}");
    assert!(
        ig.routes()
            .iter()
            .all(|r| r.ends_with("/content_publishing_limit")),
        "{:?}",
        ig.routes()
    );
}

#[tokio::test]
async fn refresh_stores_per_account_and_leaves_the_other_alone() {
    let (state, _temp, tg, ig) = setup(dev_env(json!({}))).await;
    // A prod row from a copied prod DB: prod is not configured here.
    store_token(&state, "prod", "prod-stored", "2000-01-01 00:00:00").await;

    refresh_tokens(&state).await;

    let refresh = ig.call("/refresh_access_token");
    assert_eq!(refresh.route, "GET /refresh_access_token");
    assert_eq!(refresh.param("grant_type"), Some("ig_refresh_token"));
    assert_eq!(refresh.param("access_token"), Some("dev-env-token"));
    let (token, expires_at) = stored(&state, "dev").await.unwrap();
    assert_eq!(token, "refreshed-dev-env-token");
    assert!(expires_at.unwrap().as_str() > "2026");
    assert_eq!(
        stored(&state, "prod").await,
        Some((
            "prod-stored".to_string(),
            Some("2001-01-01 00:00:00".to_string())
        ))
    );
    assert!(tg.calls().is_empty(), "no alert for a 60-day token");

    // Refreshed less than a day ago: the next run skips it.
    refresh_tokens(&state).await;
    assert_eq!(ig.calls().len(), 1, "{:?}", ig.routes());

    // The posting client now uses the stored token.
    client(&state)
        .await
        .content_publishing_quota()
        .await
        .unwrap();
    assert_eq!(
        ig.call("/content_publishing_limit").param("access_token"),
        Some("refreshed-dev-env-token")
    );
}

#[tokio::test]
async fn refresh_failure_alerts_admins_without_the_token() {
    let (state, _temp, tg, ig) = setup(dev_env(json!({}))).await;
    ig.fail_refresh();

    refresh_tokens(&state).await;

    assert_eq!(stored(&state, "dev").await, None);
    let alerts = tg.of("sendmessage");
    assert_eq!(alerts.len(), 1);
    let text = alerts[0].text();
    assert!(text.contains("refresh failed for dev"), "{text}");
    assert!(text.contains("Session has expired"), "{text}");
    assert!(!text.contains("dev-env-token"), "{text}");
    tg.assert_all_sends_in_topic();
}

#[tokio::test]
async fn near_expiry_alerts_admins() {
    let (state, _temp, tg, ig) = setup(dev_env(json!({}))).await;
    ig.set_expires_in(10 * 24 * 60 * 60);

    refresh_tokens(&state).await;

    let alerts = tg.of("sendmessage");
    assert_eq!(alerts.len(), 1);
    assert!(alerts[0].text().contains("expires in 10 days"));
    tg.assert_all_sends_in_topic();
}

#[tokio::test]
async fn account_defaults_to_dev() {
    let (state, _temp, _tg, ig) = setup(dev_env(json!({}))).await;
    assert_eq!(state.config.instagram_account, "dev");

    client(&state)
        .await
        .content_publishing_quota()
        .await
        .unwrap();
    assert_eq!(ig.routes(), ["GET /v24.0/ig-dev/content_publishing_limit"]);
}

#[tokio::test]
async fn prod_without_env_token_posts_nothing() {
    let (state, _temp, _tg, ig) = setup(dev_env(json!({
        "instagram_account": "prod",
        "instagram_business_account_id_prod": "ig-prod",
    })))
    .await;
    // Even a stored prod token does not count without the env token.
    store_token(&state, "prod", "prod-stored", "2000-01-01 00:00:00").await;

    let err = InstagramClient::for_environment(&state.config, &state.db)
        .await
        .err()
        .unwrap();
    assert!(err.to_string().contains("'prod'"), "{err}");
    assert!(ig.calls().is_empty());
}

#[tokio::test]
async fn stored_token_overrides_env_only_for_a_configured_account() {
    let (state, _temp, _tg, ig) = setup(dev_env(json!({
        "instagram_account": "prod",
        "instagram_access_token_prod": "prod-env-token",
        "instagram_business_account_id_prod": "ig-prod",
    })))
    .await;
    store_token(&state, "prod", "prod-stored", "2000-01-01 00:00:00").await;

    client(&state)
        .await
        .content_publishing_quota()
        .await
        .unwrap();
    let call = ig.call("/ig-prod/content_publishing_limit");
    assert_eq!(call.param("access_token"), Some("prod-stored"));
}
