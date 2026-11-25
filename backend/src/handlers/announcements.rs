//! Announcements of an episode (Instagram). Hosts and admins of the episode
//! create, edit and cancel them; admins set the limits.

use std::sync::Arc;

use axum::{
    extract::{Path, Query, State},
    http::HeaderMap,
    response::IntoResponse,
    Json,
};
use chrono::{DateTime, Duration, NaiveDate, Utc};
use chrono_tz::Europe::Berlin;
use serde::{Deserialize, Serialize};

use super::api::{require_admin, require_show_editor};
use crate::announcements::{
    self, check_limits, check_limits_send_now, is_slot_time, to_db_time, Announcement,
    AnnouncementSettings, AnnouncementSlot, Booked, ACTIVE_STATES, KEEP_REQUESTED_AT,
};
use crate::{models, series, AppError, AppState, Result};

const CHANNEL: &str = "instagram";
const HEADLINE_MAX: usize = 18;
const CAPTION_MAX: usize = 600;
/// How far around a send time other posts can matter (day and week limits).
const NEIGHBOURHOOD_DAYS: i64 = 8;

fn parse_slot(slot: &str) -> Result<AnnouncementSlot> {
    AnnouncementSlot::parse(slot)
        .ok_or_else(|| AppError::BadRequest("Slot must be 'pre', 'onair' or 'post'".to_string()))
}

/// Episode start in UTC: its own start time, else the series start time.
pub(crate) async fn episode_start(
    state: &AppState,
    show: &models::Show,
) -> Result<Option<DateTime<Utc>>> {
    let series_start: Option<(Option<String>,)> = sqlx::query_as(
        "SELECT t.start_time FROM shows s JOIN show_templates t ON t.id = s.template_id \
         WHERE s.id = ?",
    )
    .bind(show.id)
    .fetch_optional(&state.db)
    .await?;
    let start = show
        .start_time
        .clone()
        .or_else(|| series_start.and_then(|(t,)| t));
    let (Some(day), Some(time)) = (
        NaiveDate::parse_from_str(&show.date, "%Y-%m-%d").ok(),
        start.as_deref().and_then(series::parse_hhmm),
    ) else {
        return Ok(None);
    };
    Ok(series::berlin_to_utc(day.and_time(time)))
}

fn parse_time(value: &str) -> Result<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(value)
        .map(|t| t.with_timezone(&Utc))
        .map_err(|_| AppError::BadRequest("scheduled_at must be an RFC 3339 time".to_string()))
}

/// Active announcements with a send time from `from` to `to` (plus the days
/// the day and week limits look at), except the row of (`show_id`, `slot`).
pub(crate) async fn others_near(
    conn: &mut sqlx::SqliteConnection,
    show_id: i64,
    slot: AnnouncementSlot,
    from: DateTime<Utc>,
    to: DateTime<Utc>,
) -> Result<Vec<Booked>> {
    let rows: Vec<(i64, String, String)> = sqlx::query_as(&format!(
        "SELECT show_id, slot, scheduled_at FROM announcements \
         WHERE state IN {ACTIVE_STATES} AND channel = ? AND scheduled_at IS NOT NULL \
           AND scheduled_at >= ? AND scheduled_at <= ? AND NOT (show_id = ? AND slot = ?)"
    ))
    .bind(CHANNEL)
    .bind(to_db_time(from - Duration::days(NEIGHBOURHOOD_DAYS)))
    .bind(to_db_time(to + Duration::days(NEIGHBOURHOOD_DAYS)))
    .bind(show_id)
    .bind(slot.as_str())
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows
        .into_iter()
        .filter_map(|(show_id, slot, at)| {
            Some(Booked {
                show_id,
                slot: AnnouncementSlot::parse(&slot)?,
                scheduled_at: parse_time(&at).ok()?,
            })
        })
        .collect())
}

/// A sent row, or one the scheduler is sending right now, can't change.
fn refuse_if_sent(row: &Announcement) -> Result<()> {
    if row.state == "sent" {
        return Err(AppError::Conflict(
            "This announcement was already sent".to_string(),
        ));
    }
    if row.send_claimed_at.is_some() {
        return Err(AppError::Conflict(
            "This announcement is being sent right now".to_string(),
        ));
    }
    Ok(())
}

async fn load(
    conn: &mut sqlx::SqliteConnection,
    show_id: i64,
    slot: AnnouncementSlot,
) -> Result<Option<Announcement>> {
    Ok(
        sqlx::query_as(
            "SELECT * FROM announcements WHERE show_id = ? AND slot = ? AND channel = ?",
        )
        .bind(show_id)
        .bind(slot.as_str())
        .bind(CHANNEL)
        .fetch_optional(&mut *conn)
        .await?,
    )
}

/// GET /api/shows/:id/announcements
pub async fn api_list_announcements(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_show_editor(&state, &headers, id).await?;
    let rows: Vec<Announcement> = sqlx::query_as(
        "SELECT * FROM announcements WHERE show_id = ? \
         ORDER BY CASE slot WHEN 'pre' THEN 0 WHEN 'onair' THEN 1 ELSE 2 END, channel",
    )
    .bind(id)
    .fetch_all(&state.db)
    .await?;
    Ok(Json(rows))
}

/// Where one announcement is, without its text, image or error: what every
/// host may see on another host's episode page (the Social media card).
#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct AnnouncementStatus {
    pub slot: String,
    pub channel: String,
    pub state: String,
    pub scheduled_at: Option<String>,
    pub sent_at: Option<String>,
    /// The public link of a sent post.
    pub permalink: Option<String>,
    /// Set when a sent post's picture can be loaded (`.../:slot/image.jpg`);
    /// changes with the row.
    pub image_version: Option<String>,
}

/// GET /api/shows/:id/announcements/status — read-only, for every signed-in
/// user (like the episode itself). Drafts, captions, images and errors stay
/// with the episode's hosts.
pub async fn api_announcement_status(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let token = crate::auth::get_session_from_headers(&headers);
    crate::auth::get_current_user(&state, token.as_deref())
        .await
        .ok_or_else(|| AppError::Unauthorized("Not authenticated".to_string()))?;
    let exists: Option<(i64,)> = sqlx::query_as("SELECT id FROM shows WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?;
    if exists.is_none() {
        return Err(AppError::NotFound("Show not found".to_string()));
    }
    let rows: Vec<AnnouncementStatus> = sqlx::query_as(
        "SELECT slot, channel, state, scheduled_at, sent_at, \
                CASE WHEN state = 'sent' THEN permalink END AS permalink, \
                CASE WHEN state = 'sent' THEN updated_at END AS image_version \
         FROM announcements WHERE show_id = ? AND state <> 'draft' \
         ORDER BY CASE slot WHEN 'pre' THEN 0 WHEN 'onair' THEN 1 ELSE 2 END, channel",
    )
    .bind(id)
    .fetch_all(&state.db)
    .await?;
    Ok(Json(rows))
}

#[derive(Debug, Serialize)]
pub struct AnnouncementMeta {
    pub paused: bool,
    /// Whether a post this user saves now goes to an admin first.
    pub needs_approval: bool,
    /// The admin service switches; off reads "temporarily not available".
    pub services: crate::handlers::settings::ServiceSettings,
}

/// GET /api/shows/:id/announcements/meta
pub async fn api_announcement_meta(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let (user, _show) = require_show_editor(&state, &headers, id).await?;
    let settings = announcements::get_settings(&state.db).await;
    let mut conn = state.db.acquire().await?;
    let needs_approval = announcements::needs_approval(&mut conn, &settings, Some(&user)).await?;
    Ok(Json(AnnouncementMeta {
        paused: settings.paused,
        needs_approval,
        services: crate::handlers::settings::load_services(&state).await,
    }))
}

const UPLOAD_MAX_BYTES: usize = 10 * 1024 * 1024;

/// POST /api/shows/:id/announcements/uploads — multipart `file`, a JPEG or
/// PNG up to 10 MB. Returns `{ "image_key": ... }` for a later PUT.
pub async fn api_upload_announcement_image(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    mut multipart: axum::extract::Multipart,
) -> Result<impl IntoResponse> {
    require_show_editor(&state, &headers, id).await?;
    let mut data = None;
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|e| AppError::BadRequest(format!("Failed to read multipart field: {e}")))?
    {
        if field.name() == Some("file") {
            data = Some(
                field
                    .bytes()
                    .await
                    .map_err(|e| AppError::BadRequest(format!("Failed to read file: {e}")))?,
            );
        }
    }
    let data = data.ok_or_else(|| AppError::BadRequest("No file provided".to_string()))?;
    if data.len() > UPLOAD_MAX_BYTES {
        return Err(AppError::BadRequest(
            "The photo is larger than 10 MB".to_string(),
        ));
    }
    // By content, not by the client's content type.
    let (ext, content_type) = if data.starts_with(&[0xFF, 0xD8, 0xFF]) {
        ("jpg", "image/jpeg")
    } else if data.starts_with(b"\x89PNG\r\n\x1a\n") {
        ("png", "image/png")
    } else {
        return Err(AppError::BadRequest(
            "The photo must be a JPEG or PNG".to_string(),
        ));
    };
    let key = format!(
        "shows/{id}/announcements/uploads/{}.{ext}",
        uuid::Uuid::new_v4()
    );
    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&key)
        .body(aws_sdk_s3::primitives::ByteStream::from(data))
        .content_type(content_type)
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload photo: {e}")))?;
    Ok(Json(serde_json::json!({ "image_key": key })))
}

#[derive(Debug, Default, Deserialize)]
pub struct AnnouncementInput {
    /// RFC 3339. `pre`: defaults to `pre_days_before` days before;
    /// `onair`: ignored (episode start); `post`: optional, without it or
    /// while the track is private the post waits for the SoundCloud track
    /// (`waiting_soundcloud`).
    /// Headline: required for pre and post.
    pub scheduled_at: Option<String>,
    pub headline: Option<String>,
    pub caption: Option<String>,
    /// cover | upload | guest (default cover)
    pub image_source: Option<String>,
    pub image_key: Option<String>,
    /// band | split (default band)
    pub layout: Option<String>,
    pub collaborator: Option<bool>,
    /// More collaborator accounts (handles). Feed posts only.
    pub extra_collaborators: Option<Vec<String>>,
    /// Accounts tagged on the image (handles). Feed posts only.
    pub user_tags: Option<Vec<String>>,
    /// `pre` only: "Post now" instead of a slot. The tick sends it within a
    /// minute; still bound by the per-episode, day and week limits.
    pub send_now: Option<bool>,
}

/// Checked image fields: (image_source, image_key).
pub(crate) fn image_of(
    show_id: i64,
    input: &AnnouncementInput,
) -> Result<(&'static str, Option<String>)> {
    let source = match input.image_source.as_deref().unwrap_or("cover") {
        "cover" => return Ok(("cover", None)),
        "upload" => "upload",
        "guest" => "guest",
        _ => {
            return Err(AppError::BadRequest(
                "image_source must be 'cover', 'upload' or 'guest'".to_string(),
            ))
        }
    };
    let prefix = format!("shows/{show_id}/announcements/uploads/");
    match input.image_key.as_deref() {
        Some(key)
            if key.len() > prefix.len() && key.starts_with(&prefix) && !key.contains("..") =>
        {
            Ok((source, Some(key.to_string())))
        }
        _ => Err(AppError::BadRequest(format!(
            "image_key must be an upload of this episode ({prefix}...)"
        ))),
    }
}

/// PUT /api/shows/:id/announcements/:slot — create or edit (also re-create a
/// cancelled one). Refused once sent.
pub async fn api_put_announcement(
    State(state): State<Arc<AppState>>,
    Path((id, slot)): Path<(i64, String)>,
    headers: HeaderMap,
    Json(input): Json<AnnouncementInput>,
) -> Result<impl IntoResponse> {
    let (user, show) = require_show_editor(&state, &headers, id).await?;
    crate::instagram::ensure_enabled(&state).await?;
    let slot = parse_slot(&slot)?;
    // Say "already sent" before the field and time checks, which a sent row fails too.
    if let Some(row) = load(&mut *state.db.acquire().await?, id, slot).await? {
        refuse_if_sent(&row)?;
    }

    let headline = input.headline.as_deref().map(str::trim).unwrap_or("");
    if slot.is_feed() && headline.is_empty() {
        return Err(AppError::BadRequest("Headline is required".to_string()));
    }
    if headline.chars().count() > HEADLINE_MAX {
        return Err(AppError::BadRequest(format!(
            "Headline is longer than {HEADLINE_MAX} characters"
        )));
    }
    let caption = input.caption.as_deref().map(str::trim).unwrap_or("");
    if caption.chars().count() > CAPTION_MAX {
        return Err(AppError::BadRequest(format!(
            "Caption is longer than {CAPTION_MAX} characters"
        )));
    }
    let layout = match input.layout.as_deref().unwrap_or("band") {
        l @ ("band" | "split") => l,
        _ => {
            return Err(AppError::BadRequest(
                "layout must be 'band' or 'split'".to_string(),
            ))
        }
    };
    let (image_source, image_key) = image_of(id, &input)?;
    let collaborator = input.collaborator.unwrap_or(true);
    let (extra_collaborators, user_tags) =
        people_of(&state, id, slot, collaborator, &input).await?;
    let send_now = input.send_now.unwrap_or(false);
    if send_now && slot != AnnouncementSlot::Pre {
        return Err(AppError::BadRequest(
            "Only the pre announcement can be posted now".to_string(),
        ));
    }

    let settings = announcements::get_settings(&state.db).await;
    let start = episode_start(&state, &show).await?;
    let need_start = || AppError::BadRequest("The episode has no start time".to_string());
    let scheduled_at = match slot {
        AnnouncementSlot::OnAir => Some(start.ok_or_else(need_start)?),
        AnnouncementSlot::Pre => {
            let start = start.ok_or_else(need_start)?;
            let at = match input.scheduled_at.as_deref() {
                _ if send_now => Utc::now(),
                Some(at) => parse_time(at)?,
                None => default_pre(&settings, start).ok_or_else(|| {
                    AppError::BadRequest("No posting slot is configured".to_string())
                })?,
            };
            if at >= start {
                return Err(AppError::BadRequest(
                    "The pre announcement must go out before the episode starts".to_string(),
                ));
            }
            Some(at)
        }
        AnnouncementSlot::Post => {
            let at = input.scheduled_at.as_deref().map(parse_time).transpose()?;
            if let (Some(at), Some(start)) = (at, start) {
                if at <= start {
                    return Err(AppError::BadRequest(
                        "The post announcement must go out after the episode starts".to_string(),
                    ));
                }
            }
            at
        }
    };
    if let Some(at) = scheduled_at.filter(|_| !send_now) {
        // The on-air story goes out at go-live, not at its time, so a host who
        // starts late can still switch it on; a missed one expires later.
        if slot != AnnouncementSlot::OnAir && at <= Utc::now() {
            return Err(AppError::BadRequest(
                "The send time is in the past".to_string(),
            ));
        }
        if slot.is_feed() && !is_slot_time(&settings, at) {
            return Err(AppError::BadRequest(format!(
                "Feed posts can only go out at {} (Berlin time)",
                settings.slots.join(", ")
            )));
        }
    }
    // Check and write in one write transaction, so two saves can't both take
    // the last free slot.
    let mut tx = state.db.begin_with("BEGIN IMMEDIATE").await?;
    if let Some(row) = load(&mut tx, id, slot).await? {
        refuse_if_sent(&row)?;
    }
    // A post for a private track gets no time: it waits, and going public
    // sets its time. Read here, so a privacy switch can't slip in before the write.
    let scheduled_at = match (slot, scheduled_at) {
        (AnnouncementSlot::Post, Some(at)) => {
            let public: bool =
                sqlx::query_scalar("SELECT COALESCE(soundcloud_public, 0) FROM shows WHERE id = ?")
                    .bind(id)
                    .fetch_one(&mut *tx)
                    .await?;
            public.then_some(at)
        }
        (_, at) => at,
    };
    let anchor = scheduled_at
        .zip(start)
        .map(|(at, start)| (at - start).num_minutes());
    // Per episode, also counting posts without a send time yet.
    let (in_episode,): (i64,) = sqlx::query_as(&format!(
        "SELECT COUNT(*) FROM announcements WHERE show_id = ? AND channel = ? AND slot <> ? \
           AND state IN {ACTIVE_STATES}"
    ))
    .bind(id)
    .bind(CHANNEL)
    .bind(slot.as_str())
    .fetch_one(&mut *tx)
    .await?;
    if in_episode >= i64::from(settings.limit_per_episode) {
        return Err(AppError::Conflict(format!(
            "This episode already has {in_episode} announcements (limit {})",
            settings.limit_per_episode
        )));
    }
    if let Some(at) = scheduled_at {
        let candidate = Booked {
            show_id: id,
            slot,
            scheduled_at: at,
        };
        let others = others_near(&mut tx, id, slot, at, at).await?;
        let check = if send_now {
            check_limits_send_now
        } else {
            check_limits
        };
        check(&settings, &candidate, &others).map_err(AppError::Conflict)?;
    }

    let needs_approval = announcements::needs_approval(&mut tx, &settings, Some(&user)).await?;
    // An untimed post waits for SoundCloud; `announcement_send` applies the
    // approval rule when the track goes public and it gets its time.
    let new_state = match (scheduled_at, needs_approval, slot) {
        (None, _, _) => "waiting_soundcloud",
        (_, true, _) => "pending_approval",
        (_, false, AnnouncementSlot::OnAir) => "armed",
        (_, false, _) => "scheduled",
    };

    sqlx::query(
        "INSERT INTO announcements (show_id, slot, channel, state, anchor_offset_minutes, \
           scheduled_at, headline, caption, image_source, image_key, layout, collaborator, \
           extra_collaborators, user_tags, created_by, send_now) \
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) \
         ON CONFLICT (show_id, slot, channel) DO UPDATE SET \
           state = excluded.state, anchor_offset_minutes = excluded.anchor_offset_minutes, \
           scheduled_at = excluded.scheduled_at, headline = excluded.headline, \
           caption = excluded.caption, image_source = excluded.image_source, \
           image_key = excluded.image_key, layout = excluded.layout, \
           collaborator = excluded.collaborator, \
           extra_collaborators = excluded.extra_collaborators, user_tags = excluded.user_tags, \
           send_now = excluded.send_now, updated_at = CURRENT_TIMESTAMP, \
           requested_at = NULL, sent_at = NULL, permalink = NULL, media_id = NULL, error = NULL",
    )
    .bind(id)
    .bind(slot.as_str())
    .bind(CHANNEL)
    .bind(new_state)
    .bind(anchor)
    .bind(scheduled_at.map(to_db_time))
    .bind(headline)
    .bind(caption)
    .bind(image_source)
    .bind(image_key)
    .bind(layout)
    .bind(collaborator)
    .bind(serde_json::json!(extra_collaborators).to_string())
    .bind(serde_json::json!(user_tags).to_string())
    .bind(user.id)
    .bind(send_now)
    .execute(&mut *tx)
    .await?;
    let row = load(&mut tx, id, slot)
        .await?
        .ok_or_else(|| AppError::Internal("Announcement vanished".to_string()))?;
    tx.commit().await?;
    Ok(Json(row))
}

/// Checked extra collaborators and tagged accounts of a save. A story takes
/// neither: its lists stay empty.
async fn people_of(
    state: &Arc<AppState>,
    show_id: i64,
    slot: AnnouncementSlot,
    collaborator: bool,
    input: &AnnouncementInput,
) -> Result<(Vec<String>, Vec<String>)> {
    if !slot.is_feed() {
        return Ok((Vec::new(), Vec::new()));
    }
    let clean = |list: &Option<Vec<String>>| {
        announcements::clean_handles(list.as_deref().unwrap_or(&[])).map_err(AppError::BadRequest)
    };
    let mut extra = clean(&input.extra_collaborators)?;
    let tags = clean(&input.user_tags)?;
    let show = announcements::show_handle(&state.db, show_id)
        .await?
        .filter(|_| collaborator);
    // The show's own account is the `collaborator` flag, not an extra.
    if let Some(h) = &show {
        extra.retain(|x| !x.eq_ignore_ascii_case(h));
    }
    if extra.len() + usize::from(show.is_some()) > announcements::MAX_COLLABORATORS {
        return Err(AppError::BadRequest(format!(
            "A post can have at most {} collaborators",
            announcements::MAX_COLLABORATORS
        )));
    }
    if tags.len() > announcements::MAX_USER_TAGS {
        return Err(AppError::BadRequest(format!(
            "A post can tag at most {} accounts",
            announcements::MAX_USER_TAGS
        )));
    }
    Ok((extra, tags))
}

/// `pre_days_before` days before the start, at the first slot from 18:00 on
/// (else the last slot) of that day.
fn default_pre(settings: &AnnouncementSettings, start: DateTime<Utc>) -> Option<DateTime<Utc>> {
    let day = pre_day(settings, start);
    let slot = settings
        .slots
        .iter()
        .find(|s| s.as_str() >= EVENING)
        .or(settings.slots.last())?;
    series::berlin_to_utc(day.and_time(series::parse_hhmm(slot)?))
}

/// Preferred feed time: from this one on (Berlin "HH:MM").
const EVENING: &str = "18:00";

/// The Berlin day of the default pre post.
fn pre_day(settings: &AnnouncementSettings, start: DateTime<Utc>) -> NaiveDate {
    start.with_timezone(&Berlin).date_naive() - Duration::days(i64::from(settings.pre_days_before))
}

/// The first Berlin day of the default post time: `post_days_after` days
/// after the episode day, but not before today (track public late).
pub(crate) fn post_day(
    settings: &AnnouncementSettings,
    start: Option<DateTime<Utc>>,
    now: DateTime<Utc>,
) -> NaiveDate {
    let today = now.with_timezone(&Berlin).date_naive();
    start.map_or(today, |start| {
        let day = start.with_timezone(&Berlin).date_naive()
            + Duration::days(i64::from(settings.post_days_after));
        day.max(today)
    })
}

/// DELETE /api/shows/:id/announcements/:slot — cancel. Refused once sent.
pub async fn api_cancel_announcement(
    State(state): State<Arc<AppState>>,
    Path((id, slot)): Path<(i64, String)>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_show_editor(&state, &headers, id).await?;
    let slot = parse_slot(&slot)?;
    let mut tx = state.db.begin_with("BEGIN IMMEDIATE").await?;
    let row = load(&mut tx, id, slot)
        .await?
        .ok_or_else(|| AppError::NotFound("No announcement in this slot".to_string()))?;
    refuse_if_sent(&row)?;
    sqlx::query(
        "UPDATE announcements SET state = 'cancelled', updated_at = CURRENT_TIMESTAMP \
         WHERE id = ?",
    )
    .bind(row.id)
    .execute(&mut *tx)
    .await?;
    let row = load(&mut tx, id, slot)
        .await?
        .ok_or_else(|| AppError::Internal("Announcement vanished".to_string()))?;
    tx.commit().await?;
    Ok(Json(row))
}

#[derive(Debug, Deserialize)]
pub struct SlotsQuery {
    pub slot: String,
}

#[derive(Debug, Serialize)]
pub struct SlotTime {
    pub scheduled_at: String,
    pub free: bool,
    /// Why the time is taken.
    pub reason: Option<String>,
    /// The suggested time; exactly one free item has it.
    pub default: bool,
}

/// GET /api/shows/:id/announcements/slots?slot=pre|post — feed times before
/// (pre) or after (post) the episode start, free or taken. Post is empty
/// while the SoundCloud track is not public; its default is the time
/// `unlock_waiting_posts` would pick.
pub async fn api_announcement_slots(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    Query(query): Query<SlotsQuery>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let (_user, show) = require_show_editor(&state, &headers, id).await?;
    let slot = parse_slot(&query.slot)?;
    if !slot.is_feed() {
        return Err(AppError::BadRequest(
            "The on-air story goes out at the episode start".to_string(),
        ));
    }
    if slot == AnnouncementSlot::Post && show.soundcloud_public != Some(true) {
        return Ok(Json(Vec::new()));
    }
    let start = episode_start(&state, &show)
        .await?
        .ok_or_else(|| AppError::BadRequest("The episode has no start time".to_string()))?;
    let settings = announcements::get_settings(&state.db).await;
    let now = Utc::now();
    let (from, to) = match slot {
        AnnouncementSlot::Pre => {
            let days = 7.max(i64::from(settings.pre_days_before) + 1);
            (start - Duration::days(days), start)
        }
        _ => {
            let days = 7.max(i64::from(settings.post_days_after) + 1);
            (start, start.max(now) + Duration::days(days))
        }
    };
    let mut conn = state.db.acquire().await?;
    let others = others_near(&mut conn, id, slot, from, to).await?;

    let first_day = from.with_timezone(&Berlin).date_naive();
    // (time, Berlin day, Berlin "HH:MM", check)
    let mut found = Vec::new();
    for day in first_day
        .iter_days()
        .take((to - from).num_days() as usize + 2)
    {
        for hhmm in &settings.slots {
            let Some(at) =
                series::parse_hhmm(hhmm).and_then(|t| series::berlin_to_utc(day.and_time(t)))
            else {
                continue;
            };
            if at <= now || at <= from || at >= to {
                continue;
            }
            let candidate = Booked {
                show_id: id,
                slot,
                scheduled_at: at,
            };
            found.push((at, day, hhmm, check_limits(&settings, &candidate, &others)));
        }
    }

    let free = |i: &usize| found[*i].3.is_ok();
    let default = if slot == AnnouncementSlot::Pre {
        // The pre day and evening, then the pre day, then any.
        let pre_day = pre_day(&settings, start);
        (0..found.len())
            .filter(free)
            .find(|&i| found[i].1 == pre_day && found[i].2.as_str() >= EVENING)
            .or_else(|| {
                (0..found.len())
                    .filter(free)
                    .find(|&i| found[i].1 == pre_day)
            })
            .or_else(|| (0..found.len()).find(free))
    } else {
        // As `next_free_feed_time`: from the post day on, per day evening first.
        let post_day = post_day(&settings, Some(start), now);
        (0..found.len())
            .filter(free)
            .filter(|&i| found[i].1 >= post_day)
            .min_by_key(|&i| (found[i].1, found[i].2.as_str() < EVENING, found[i].2))
            .or_else(|| (0..found.len()).find(free))
    };

    let times: Vec<SlotTime> = found
        .into_iter()
        .enumerate()
        .map(|(i, (at, _, _, check))| SlotTime {
            scheduled_at: to_db_time(at),
            free: check.is_ok(),
            reason: check.err(),
            default: default == Some(i),
        })
        .collect();
    Ok(Json(times))
}

/// GET /api/settings/announcements (admin)
pub async fn api_get_announcement_settings(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;
    Ok(Json(announcements::get_settings(&state.db).await))
}

/// PUT /api/settings/announcements (admin) — the full settings object.
pub async fn api_put_announcement_settings(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(mut input): Json<AnnouncementSettings>,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;
    input.slots = announcements::parse_slots(&input.slots.join(","))
        .filter(|s| !s.is_empty())
        .ok_or_else(|| AppError::BadRequest("Slots must be one or more HH:MM times".to_string()))?;
    input.hashtags = input.hashtags.trim().to_string();
    announcements::save_settings(&state.db, &input).await?;
    Ok(Json(input))
}

/// The first free feed time on Berlin day `first_day` or later, after
/// `after`. Per day the evening slots come first, then the earlier ones.
/// `None` when nothing is free within a year.
pub(crate) async fn next_free_feed_time(
    conn: &mut sqlx::SqliteConnection,
    settings: &AnnouncementSettings,
    show_id: i64,
    slot: AnnouncementSlot,
    first_day: NaiveDate,
    after: DateTime<Utc>,
) -> Result<Option<DateTime<Utc>>> {
    const DAYS: i64 = 366;
    let others = others_near(conn, show_id, slot, after, after + Duration::days(DAYS + 1)).await?;
    let (evening, early): (Vec<&String>, Vec<&String>) =
        settings.slots.iter().partition(|s| s.as_str() >= EVENING);
    for day in first_day.iter_days().take(DAYS as usize) {
        for hhmm in evening.iter().chain(&early) {
            let Some(at) =
                series::parse_hhmm(hhmm).and_then(|t| series::berlin_to_utc(day.and_time(t)))
            else {
                continue;
            };
            let candidate = Booked {
                show_id,
                slot,
                scheduled_at: at,
            };
            if at > after && check_limits(settings, &candidate, &others).is_ok() {
                return Ok(Some(at));
            }
        }
    }
    Ok(None)
}

// ── Admin queue ─────────────────────────────────────────────────────────────

/// One row of the admin queue.
#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct AdminAnnouncement {
    pub id: i64,
    pub show_id: i64,
    pub show_title: String,
    /// The series name; the episode title for an episode without a series.
    pub series_name: String,
    pub host_handle: Option<String>,
    pub slot: String,
    pub state: String,
    pub scheduled_at: Option<String>,
    pub requested_at: Option<String>,
    pub sent_at: Option<String>,
    pub permalink: Option<String>,
    pub error: Option<String>,
    pub send_now: bool,
}

const ADMIN_SELECT: &str = "SELECT a.id, a.show_id, s.title AS show_title, \
       COALESCE(t.name, s.title) AS series_name, t.instagram_handle AS host_handle, \
       a.slot, a.state, a.scheduled_at, a.requested_at, a.sent_at, a.permalink, a.error, \
       a.send_now \
     FROM announcements a JOIN shows s ON s.id = a.show_id \
     LEFT JOIN show_templates t ON t.id = s.template_id";

async fn admin_row(conn: &mut sqlx::SqliteConnection, id: i64) -> Result<AdminAnnouncement> {
    sqlx::query_as(&format!("{ADMIN_SELECT} WHERE a.id = ?"))
        .bind(id)
        .fetch_optional(conn)
        .await?
        .ok_or_else(|| AppError::NotFound("Announcement not found".to_string()))
}

#[derive(Debug, Deserialize)]
pub struct RangeQuery {
    pub from: String,
    pub to: String,
}

/// Start of a Berlin day in UTC.
fn berlin_midnight(day: NaiveDate) -> Result<DateTime<Utc>> {
    series::berlin_to_utc(day.and_time(chrono::NaiveTime::MIN))
        .ok_or_else(|| AppError::BadRequest(format!("No midnight on {day}")))
}

/// GET /api/admin/announcements?from=YYYY-MM-DD&to=YYYY-MM-DD (admin) — rows
/// sent or due in the Berlin days `from` to `to` (inclusive).
pub async fn api_admin_list_announcements(
    State(state): State<Arc<AppState>>,
    Query(query): Query<RangeQuery>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;
    let day = |v: &str| {
        NaiveDate::parse_from_str(v, "%Y-%m-%d")
            .map_err(|_| AppError::BadRequest("from and to must be YYYY-MM-DD".to_string()))
    };
    let from = berlin_midnight(day(&query.from)?)?;
    let to = berlin_midnight(day(&query.to)? + Duration::days(1))?;
    let rows: Vec<AdminAnnouncement> = sqlx::query_as(&format!(
        "{ADMIN_SELECT} WHERE COALESCE(a.sent_at, a.scheduled_at) >= ? \
           AND COALESCE(a.sent_at, a.scheduled_at) < ? \
         ORDER BY COALESCE(a.sent_at, a.scheduled_at), a.id"
    ))
    .bind(to_db_time(from))
    .bind(to_db_time(to))
    .fetch_all(&state.db)
    .await?;
    Ok(Json(rows))
}

async fn load_by_id(conn: &mut sqlx::SqliteConnection, id: i64) -> Result<Announcement> {
    sqlx::query_as("SELECT * FROM announcements WHERE id = ?")
        .bind(id)
        .fetch_optional(conn)
        .await?
        .ok_or_else(|| AppError::NotFound("Announcement not found".to_string()))
}

/// Puts `row` back in the queue. A story with `keep` is armed again; one
/// without goes out at once. A feed post keeps a future `keep` time, else it
/// moves to the next free slot.
async fn requeue(
    conn: &mut sqlx::SqliteConnection,
    settings: &AnnouncementSettings,
    row: &Announcement,
    keep: Option<DateTime<Utc>>,
    now: DateTime<Utc>,
) -> Result<()> {
    let slot = parse_slot(&row.slot)?;
    let (new_state, at) = match (slot, keep) {
        // An approved story still waits for its episode to go live.
        (AnnouncementSlot::OnAir, Some(at)) => ("armed", at),
        (AnnouncementSlot::OnAir, None) => ("scheduled", now),
        (_, Some(at)) if at > now => ("scheduled", at),
        _ => {
            let today = now.with_timezone(&Berlin).date_naive();
            let at = next_free_feed_time(conn, settings, row.show_id, slot, today, now)
                .await?
                .ok_or_else(|| {
                    AppError::Conflict("No free feed time in the next year".to_string())
                })?;
            ("scheduled", at)
        }
    };
    sqlx::query(&format!(
        "UPDATE announcements SET state = ?, {KEEP_REQUESTED_AT}, scheduled_at = ?, \
           error = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?"
    ))
    .bind(new_state)
    .bind(to_db_time(at))
    .bind(to_db_time(at))
    .bind(row.id)
    .execute(conn)
    .await?;
    Ok(())
}

/// POST /api/admin/announcements/:id/approve — pending_approval → scheduled
/// (on-air: armed). A feed post whose time passed while it waited moves to
/// the next free slot; a "Post now" one goes out at the next tick.
pub async fn api_admin_approve_announcement(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;
    let settings = announcements::get_settings(&state.db).await;
    let mut tx = state.db.begin_with("BEGIN IMMEDIATE").await?;
    let row = load_by_id(&mut tx, id).await?;
    if row.state != "pending_approval" {
        return Err(AppError::Conflict(
            "Only a pending announcement can be approved".to_string(),
        ));
    }
    if row.send_now {
        sqlx::query(
            "UPDATE announcements SET state = 'scheduled', scheduled_at = ?, error = NULL, \
               updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        )
        .bind(to_db_time(Utc::now()))
        .bind(id)
        .execute(&mut *tx)
        .await?;
    } else {
        let at = row.scheduled_at.as_deref().map(parse_time).transpose()?;
        requeue(&mut tx, &settings, &row, at, Utc::now()).await?;
    }
    let out = admin_row(&mut tx, id).await?;
    tx.commit().await?;
    Ok(Json(out))
}

/// POST /api/admin/announcements/:id/reject — → rejected. Refused once sent.
pub async fn api_admin_reject_announcement(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;
    let mut tx = state.db.begin_with("BEGIN IMMEDIATE").await?;
    let row = load_by_id(&mut tx, id).await?;
    refuse_if_sent(&row)?;
    sqlx::query(
        "UPDATE announcements SET state = 'rejected', updated_at = CURRENT_TIMESTAMP \
         WHERE id = ?",
    )
    .bind(id)
    .execute(&mut *tx)
    .await?;
    let out = admin_row(&mut tx, id).await?;
    tx.commit().await?;
    Ok(Json(out))
}

/// POST /api/admin/announcements/:id/retry — failed → scheduled: a story at
/// once, a feed post at the next free slot.
pub async fn api_admin_retry_announcement(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;
    let settings = announcements::get_settings(&state.db).await;
    let mut tx = state.db.begin_with("BEGIN IMMEDIATE").await?;
    let row = load_by_id(&mut tx, id).await?;
    if row.state != "failed" {
        return Err(AppError::Conflict(
            "Only a failed announcement can be retried".to_string(),
        ));
    }
    requeue(&mut tx, &settings, &row, None, Utc::now()).await?;
    let out = admin_row(&mut tx, id).await?;
    tx.commit().await?;
    Ok(Json(out))
}

#[derive(Debug, Serialize)]
pub struct InstagramStatus {
    pub account: String,
    pub account_username: &'static str,
    pub token_expires_at: Option<String>,
    pub token_refreshed_at: Option<String>,
    pub quota_used: Option<u32>,
    pub quota_total: Option<u32>,
}

/// GET /api/admin/instagram/status (admin) — the account announcements post
/// to, its stored token dates and the publishing quota (null when unknown).
pub async fn api_admin_instagram_status(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;
    let account = state.config.instagram_account.clone();
    let token: Option<(Option<String>, Option<String>)> =
        sqlx::query_as("SELECT expires_at, refreshed_at FROM instagram_tokens WHERE account = ?")
            .bind(&account)
            .fetch_optional(&state.db)
            .await?;
    let (token_expires_at, token_refreshed_at) = token.unwrap_or_default();
    let quota =
        match crate::instagram::InstagramClient::for_environment(&state.config, &state.db).await {
            Ok(client) => client.content_publishing_quota().await.ok(),
            Err(_) => None,
        };
    Ok(Json(InstagramStatus {
        account_username: if account == "prod" {
            "moafunk_radio"
        } else {
            "moafunk_tester"
        },
        account,
        token_expires_at,
        token_refreshed_at,
        quota_used: quota.map(|q| q.0),
        quota_total: quota.map(|q| q.1),
    }))
}
