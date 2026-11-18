//! Sends Instagram announcements: due feed posts from the scheduler tick, the
//! on-air story when its episode goes live, and the post time once the
//! episode's SoundCloud track is public.
//!
//! Each send claims its row first (`send_claimed_at`), so a row is posted at
//! most once, also across a restart. A claim older than [`STALE_CLAIM`] means
//! the process died while sending: the row becomes `failed`, and an admin
//! checks Instagram before a retry.
//!
//! Rows in `pending_approval` are never sent. When their time passes they stay
//! pending, so the admin still sees them; approving moves a feed post to the
//! next free slot.

use std::sync::Arc;

use chrono::{DateTime, Duration, Utc};
use chrono_tz::Europe::Berlin;

use crate::announcement_image::{
    announcement_for_show, load_announcement_image, render_announcement_jpeg,
    store_announcement_jpeg, AnnouncementKind, AnnouncementLayout, PreviewQuery,
};
use crate::announcements::{
    self, to_db_time, Announcement, AnnouncementSettings, AnnouncementSlot, KEEP_REQUESTED_AT,
};
use crate::instagram::{is_quota_used_up, InstagramClient, PublishedMedia};
use crate::{models, storage, telegram_notify, AppError, AppState, Result};

/// A send takes a few minutes at most (quota, container poll, publish).
const STALE_CLAIM: Duration = Duration::minutes(15);

/// The scheduler tick (every 60 s).
pub async fn check_due_announcements(state: &Arc<AppState>, now: DateTime<Utc>) {
    let settings = announcements::get_settings(&state.db).await;
    if settings.paused {
        return;
    }
    if let Err(e) = unlock_waiting_posts(state, now).await {
        tracing::error!("Announcements: post unlock failed: {e}");
    }
    if let Err(e) = fail_interrupted_sends(state, now).await {
        tracing::error!("Announcements: stale claim check failed: {e}");
    }
    if let Err(e) = expire_missed_stories(state, now).await {
        tracing::error!("Announcements: missed story check failed: {e}");
    }
    let due: Vec<i64> = match sqlx::query_scalar(
        "SELECT id FROM announcements WHERE channel = 'instagram' AND state = 'scheduled' \
           AND scheduled_at <= ? AND send_claimed_at IS NULL ORDER BY scheduled_at, id",
    )
    .bind(to_db_time(now))
    .fetch_all(&state.db)
    .await
    {
        Ok(ids) => ids,
        Err(e) => {
            tracing::error!("Announcements: failed to query due rows: {e}");
            return;
        }
    };
    for id in due {
        send(state, &settings, id, "scheduled", now).await;
    }
    // In case the go-live hook missed it.
    if let Some(show_id) = crate::handlers::series::on_air_show_id(state).await {
        announce_on_air(state, show_id, now).await;
    }
}

/// The episode went live: send its on-air story if the host left it armed.
pub async fn announce_on_air(state: &Arc<AppState>, show_id: i64, now: DateTime<Utc>) {
    let settings = announcements::get_settings(&state.db).await;
    if settings.paused {
        return;
    }
    let id: Option<i64> = match sqlx::query_scalar(
        "SELECT id FROM announcements WHERE show_id = ? AND slot = 'onair' \
           AND channel = 'instagram' AND state = 'armed' AND send_claimed_at IS NULL",
    )
    .bind(show_id)
    .fetch_optional(&state.db)
    .await
    {
        Ok(id) => id,
        Err(e) => {
            tracing::error!("Announcements: failed to load on-air story of show {show_id}: {e}");
            return;
        }
    };
    if let Some(id) = id {
        send(state, &settings, id, "armed", now).await;
    }
}

/// Every post that waits for SoundCloud and whose track is now public gets
/// the default post time (`post_days_after` days after the episode, evening
/// slot first, else the next free slot; from now on if that passed) and then
/// the approval rule.
pub async fn unlock_waiting_posts(state: &Arc<AppState>, now: DateTime<Utc>) -> Result<()> {
    let settings = announcements::get_settings(&state.db).await;
    let rows: Vec<(i64, i64, Option<i64>)> = sqlx::query_as(
        "SELECT a.id, a.show_id, a.created_by FROM announcements a \
         JOIN shows s ON s.id = a.show_id \
         WHERE a.state = 'waiting_soundcloud' AND a.slot = 'post' AND s.soundcloud_public = 1 ORDER BY a.id",
    )
    .fetch_all(&state.db)
    .await?;
    for (id, show_id, created_by) in rows {
        let show: Option<models::Show> = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
            .bind(show_id)
            .fetch_optional(&state.db)
            .await?;
        let start = match &show {
            Some(show) => crate::handlers::announcements::episode_start(state, show).await?,
            None => None,
        };
        let creator: Option<models::User> = sqlx::query_as("SELECT * FROM users WHERE id = ?")
            .bind(created_by)
            .fetch_optional(&state.db)
            .await?;

        let first_day = crate::handlers::announcements::post_day(&settings, start, now);
        let mut tx = state.db.begin_with("BEGIN IMMEDIATE").await?;
        let Some(at) = crate::handlers::announcements::next_free_feed_time(
            &mut tx,
            &settings,
            show_id,
            AnnouncementSlot::Post,
            first_day,
            now,
        )
        .await?
        else {
            tracing::error!("Announcements: no free feed time for the post of show {show_id}");
            continue;
        };
        let pending = announcements::needs_approval(&mut tx, &settings, creator.as_ref()).await?;
        sqlx::query(&format!(
            "UPDATE announcements SET state = ?, {KEEP_REQUESTED_AT}, scheduled_at = ?, \
               anchor_offset_minutes = ?, updated_at = CURRENT_TIMESTAMP \
             WHERE id = ? AND state = 'waiting_soundcloud'"
        ))
        .bind(if pending {
            "pending_approval"
        } else {
            "scheduled"
        })
        .bind(to_db_time(at))
        .bind(to_db_time(at))
        .bind(start.map(|s| (at - s).num_minutes()))
        .bind(id)
        .execute(&mut *tx)
        .await?;
        tx.commit().await?;
    }
    Ok(())
}

/// An on-air story still armed this long after the booked start: the episode
/// never went live (or was cancelled), so a late go-live must not post it.
const MISSED_STORY: Duration = Duration::hours(12);

/// Cancel armed on-air stories whose episode never went live.
pub async fn expire_missed_stories(state: &Arc<AppState>, now: DateTime<Utc>) -> Result<()> {
    sqlx::query(
        "UPDATE announcements SET state = 'cancelled', \
           error = 'The episode did not go live, so the story was not sent', \
           updated_at = CURRENT_TIMESTAMP \
         WHERE slot = 'onair' AND state = 'armed' AND send_claimed_at IS NULL \
           AND scheduled_at IS NOT NULL AND scheduled_at < ?",
    )
    .bind(to_db_time(now - MISSED_STORY))
    .execute(&state.db)
    .await?;
    Ok(())
}

/// Claims older than [`STALE_CLAIM`] belong to a send that a crash or
/// restart cut off. It may have posted, so it is never sent again on its own.
async fn fail_interrupted_sends(state: &Arc<AppState>, now: DateTime<Utc>) -> Result<()> {
    let stale: Vec<i64> = sqlx::query_scalar(
        "SELECT id FROM announcements WHERE send_claimed_at IS NOT NULL AND send_claimed_at < ?",
    )
    .bind(to_db_time(now - STALE_CLAIM))
    .fetch_all(&state.db)
    .await?;
    for id in stale {
        let error = "The send was interrupted (restart?). Check Instagram before you retry.";
        finish(state, id, "failed", None, Some(error), now).await?;
        notify_result(state, id).await;
    }
    Ok(())
}

/// Claim row `id` in state `from`, publish it and store the result.
async fn send(
    state: &Arc<AppState>,
    settings: &AnnouncementSettings,
    id: i64,
    from: &str,
    now: DateTime<Utc>,
) {
    let claimed = sqlx::query(
        "UPDATE announcements SET send_claimed_at = ? \
         WHERE id = ? AND state = ? AND send_claimed_at IS NULL",
    )
    .bind(to_db_time(now))
    .bind(id)
    .bind(from)
    .execute(&state.db)
    .await;
    match claimed {
        Ok(r) if r.rows_affected() == 1 => {}
        Ok(_) => return, // another tick or the go-live hook has it
        Err(e) => {
            tracing::error!("Announcements: failed to claim {id}: {e}");
            return;
        }
    }

    let result = match load(state, id).await {
        Ok(row) => publish(state, settings, &row).await,
        Err(e) => Err(e),
    };
    let stored = match result {
        Ok(media) => finish(state, id, "sent", Some(&media), None, now).await,
        Err(e) if is_quota_used_up(&e) => {
            tracing::warn!("Announcements: {e}; {id} waits for the next tick");
            quota_notice(state, now, &e).await;
            release(state, id).await
        }
        Err(e) => {
            tracing::error!("Announcements: send of {id} failed: {e}");
            finish(state, id, "failed", None, Some(&e.to_string()), now).await
        }
    };
    match stored {
        Ok(()) => notify_result(state, id).await,
        // The claim stays; the stale-claim check marks it failed later.
        Err(e) => tracing::error!("Announcements: failed to store the result of {id}: {e}"),
    }
}

async fn load(state: &Arc<AppState>, id: i64) -> Result<Announcement> {
    sqlx::query_as("SELECT * FROM announcements WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound(format!("Announcement {id} not found")))
}

async fn release(state: &Arc<AppState>, id: i64) -> Result<()> {
    sqlx::query("UPDATE announcements SET send_claimed_at = NULL WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await?;
    Ok(())
}

async fn finish(
    state: &Arc<AppState>,
    id: i64,
    new_state: &str,
    media: Option<&PublishedMedia>,
    error: Option<&str>,
    now: DateTime<Utc>,
) -> Result<()> {
    sqlx::query(
        "UPDATE announcements SET state = ?, sent_at = ?, permalink = ?, media_id = ?, \
           error = ?, send_claimed_at = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
    )
    .bind(new_state)
    .bind(media.map(|_| to_db_time(now)))
    .bind(media.and_then(|m| m.permalink.clone()))
    .bind(media.map(|m| m.media_id.clone()))
    .bind(error)
    .bind(id)
    .execute(&state.db)
    .await?;
    Ok(())
}

/// Render the image now (current cover or upload), store it and publish it.
async fn publish(
    state: &Arc<AppState>,
    settings: &AnnouncementSettings,
    row: &Announcement,
) -> Result<PublishedMedia> {
    let kind = AnnouncementKind::from_slot(&row.slot)
        .ok_or_else(|| AppError::Internal(format!("Unknown slot {}", row.slot)))?;
    let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(row.show_id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound(format!("Show {} not found", row.show_id)))?;
    let handle: Option<String> = sqlx::query_scalar(
        "SELECT t.instagram_handle FROM shows s JOIN show_templates t ON t.id = s.template_id \
         WHERE s.id = ?",
    )
    .bind(row.show_id)
    .fetch_optional(&state.db)
    .await?
    .flatten()
    .filter(|h: &String| !h.is_empty());

    let image_key = row
        .image_key
        .as_deref()
        .filter(|_| row.image_source != "cover");
    let image = load_announcement_image(state, row.show_id, image_key).await?;
    let headline = row.headline.clone().unwrap_or_default();
    let layout = if row.layout == "split" {
        AnnouncementLayout::Split
    } else {
        AnnouncementLayout::Band
    };
    let query = PreviewQuery {
        headline: headline.clone(),
        layout,
    };
    let input = announcement_for_show(state, &show, kind, query, image).await?;
    let show_name = input.show_name.clone();
    let jpeg = tokio::task::spawn_blocking(move || render_announcement_jpeg(&input))
        .await
        .map_err(|e| AppError::Internal(format!("Render task failed: {e}")))?
        .map_err(|e| AppError::Internal(format!("Render failed: {e:#}")))?;
    let key = store_announcement_jpeg(state, row.show_id, kind, jpeg).await?;
    let url = storage::get_presigned_url(state, &key, 3600).await?;

    let client = InstagramClient::for_environment(&state.config, &state.db).await?;
    if kind == AnnouncementKind::OnAir {
        return client.publish_story_image(&url, handle.as_deref()).await;
    }
    let caption = feed_caption(row.caption.as_deref(), &settings.hashtags);
    let alt_text = format!("{headline} – {show_name}");
    let collaborators: Vec<String> = handle.filter(|_| row.collaborator).into_iter().collect();
    client
        .publish_feed_image(&url, &caption, Some(&alt_text), &collaborators)
        .await
}

/// The host's caption plus the station hashtags.
fn feed_caption(caption: Option<&str>, hashtags: &str) -> String {
    let parts: Vec<&str> = [caption.unwrap_or("").trim(), hashtags.trim()]
        .into_iter()
        .filter(|p| !p.is_empty())
        .collect();
    parts.join("\n\n")
}

/// One admin notice per Berlin day while the quota is used up.
async fn quota_notice(state: &Arc<AppState>, now: DateTime<Utc>, e: &AppError) {
    const KEY: &str = "announce_quota_notice_day";
    let today = now.with_timezone(&Berlin).date_naive().to_string();
    if crate::db::get_setting(&state.db, KEY).await.ok().flatten() == Some(today.clone()) {
        return;
    }
    if let Err(err) = crate::db::set_setting(&state.db, KEY, &today).await {
        tracing::error!("Announcements: failed to save quota notice day: {err}");
        return;
    }
    let msg = format!("⏳ Instagram announcements wait: {e}. They go out when the quota frees up.");
    telegram_notify::notify(state, &msg).await;
}

/// Admin notice for a sent or failed row.
async fn notify_result(state: &Arc<AppState>, id: i64) {
    let Ok(row) = load(state, id).await else {
        return;
    };
    let title: String = sqlx::query_scalar("SELECT title FROM shows WHERE id = ?")
        .bind(row.show_id)
        .fetch_optional(&state.db)
        .await
        .ok()
        .flatten()
        .unwrap_or_default();
    let slot = &row.slot;
    let msg = match row.state.as_str() {
        "sent" => format!(
            "📸 Instagram {slot} announcement sent: {title}\n{}",
            row.permalink.as_deref().unwrap_or("(no link)")
        ),
        "failed" => format!(
            "⚠️ Instagram {slot} announcement failed: {title}\n{}",
            row.error.as_deref().unwrap_or_default()
        ),
        _ => return,
    };
    telegram_notify::notify(state, &msg).await;
}

#[cfg(test)]
#[path = "announcement_send_tests.rs"]
mod tests;
