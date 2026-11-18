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
    self, check_limits, is_slot_time, to_db_time, Announcement, AnnouncementSettings,
    AnnouncementSlot, ApprovalMode, Booked, ACTIVE_STATES,
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
async fn episode_start(state: &AppState, show: &models::Show) -> Result<Option<DateTime<Utc>>> {
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
async fn others_near(
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

#[derive(Debug, Default, Deserialize)]
pub struct AnnouncementInput {
    /// RFC 3339. `pre`: defaults to `pre_days_before` days before;
    /// `onair`: ignored (episode start); `post`: optional, without it the
    /// post waits for the SoundCloud track (`waiting_soundcloud`).
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
}

/// Checked image fields: (image_source, image_key).
fn image_of(show_id: i64, input: &AnnouncementInput) -> Result<(&'static str, Option<String>)> {
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
    let slot = parse_slot(&slot)?;

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

    let settings = announcements::get_settings(&state.db).await;
    let start = episode_start(&state, &show).await?;
    let need_start = || AppError::BadRequest("The episode has no start time".to_string());
    let scheduled_at = match slot {
        AnnouncementSlot::OnAir => Some(start.ok_or_else(need_start)?),
        AnnouncementSlot::Pre => {
            let start = start.ok_or_else(need_start)?;
            let at = match input.scheduled_at.as_deref() {
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
    if let Some(at) = scheduled_at {
        if at <= Utc::now() {
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
    let anchor = scheduled_at
        .zip(start)
        .map(|(at, start)| (at - start).num_minutes());

    // Check and write in one write transaction, so two saves can't both take
    // the last free slot.
    let mut tx = state.db.begin_with("BEGIN IMMEDIATE").await?;
    if load(&mut tx, id, slot)
        .await?
        .is_some_and(|row| row.state == "sent")
    {
        return Err(AppError::Conflict(
            "This announcement was already sent".to_string(),
        ));
    }
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
        check_limits(&settings, &candidate, &others).map_err(AppError::Conflict)?;
    }

    let needs_approval = !user.role_enum().can_access_admin()
        && match settings.approval_mode {
            ApprovalMode::Always => true,
            ApprovalMode::Never => false,
            ApprovalMode::FirstTwo => {
                let (sent,): (i64,) = sqlx::query_as(
                    "SELECT COUNT(*) FROM announcements WHERE created_by = ? AND state = 'sent'",
                )
                .bind(user.id)
                .fetch_one(&mut *tx)
                .await?;
                sent < 2
            }
        };
    // shortcut: an untimed post waits for SoundCloud before approval; the
    // scheduler applies the approval rule when it sets the time.
    let new_state = match (scheduled_at, needs_approval, slot) {
        (None, _, _) => "waiting_soundcloud",
        (_, true, _) => "pending_approval",
        (_, false, AnnouncementSlot::OnAir) => "armed",
        (_, false, _) => "scheduled",
    };

    sqlx::query(
        "INSERT INTO announcements (show_id, slot, channel, state, anchor_offset_minutes, \
           scheduled_at, headline, caption, image_source, image_key, layout, collaborator, \
           created_by) \
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) \
         ON CONFLICT (show_id, slot, channel) DO UPDATE SET \
           state = excluded.state, anchor_offset_minutes = excluded.anchor_offset_minutes, \
           scheduled_at = excluded.scheduled_at, headline = excluded.headline, \
           caption = excluded.caption, image_source = excluded.image_source, \
           image_key = excluded.image_key, layout = excluded.layout, \
           collaborator = excluded.collaborator, updated_at = CURRENT_TIMESTAMP, \
           sent_at = NULL, permalink = NULL, media_id = NULL, error = NULL",
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
    .bind(input.collaborator.unwrap_or(true))
    .bind(user.id)
    .execute(&mut *tx)
    .await?;
    let row = load(&mut tx, id, slot)
        .await?
        .ok_or_else(|| AppError::Internal("Announcement vanished".to_string()))?;
    tx.commit().await?;
    Ok(Json(row))
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
    if row.state == "sent" {
        return Err(AppError::Conflict(
            "This announcement was already sent".to_string(),
        ));
    }
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
/// (pre) or in the week after (post) the episode start, free or taken. Post
/// is empty while the SoundCloud track is not public.
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
    let days = 7.max(i64::from(settings.pre_days_before) + 1);
    let (from, to) = match slot {
        AnnouncementSlot::Pre => (start - Duration::days(days), start),
        _ => (start, start + Duration::days(7)),
    };
    let mut conn = state.db.acquire().await?;
    let others = others_near(&mut conn, id, slot, from, to).await?;

    let now = Utc::now();
    let first_day = from.with_timezone(&Berlin).date_naive();
    // (time, Berlin day, Berlin "HH:MM", check)
    let mut found = Vec::new();
    for day in first_day.iter_days().take(days as usize + 2) {
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

    // Preference: (pre: the pre day) and evening, then the pre day, then any.
    let pre_day = (slot == AnnouncementSlot::Pre).then(|| pre_day(&settings, start));
    let on_day = |day: NaiveDate| pre_day.is_none_or(|d| d == day);
    let free = |i: &usize| found[*i].3.is_ok();
    let default = (0..found.len())
        .filter(free)
        .find(|&i| on_day(found[i].1) && found[i].2.as_str() >= EVENING)
        .or_else(|| (0..found.len()).filter(free).find(|&i| on_day(found[i].1)))
        .or_else(|| (0..found.len()).find(free));

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
    announcements::save_settings(&state.db, &input).await?;
    Ok(Json(input))
}
