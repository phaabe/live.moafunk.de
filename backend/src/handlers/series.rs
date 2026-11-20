//! Host API for show series ("Shows" in the host flow). Every route only sees
//! the caller's own series; another user's slug is a 404.

use std::sync::Arc;

use axum::{
    extract::{Multipart, Path, State},
    http::{HeaderMap, StatusCode},
    response::IntoResponse,
    Json,
};
use chrono::Utc;
use serde::Serialize;

use super::api::require_show_creator;
use crate::series::{self, EpisodeStatus, RepeatRule, Rhythm};
use crate::{storage, AppError, AppState, Result};

#[derive(sqlx::FromRow)]
pub(super) struct SeriesRow {
    pub(super) id: i64,
    slug: String,
    pub(super) name: String,
    description: Option<String>,
    pub(super) cover_key: Option<String>,
    pub(super) owner_user_id: i64,
    repeat_rule: Option<String>,
    weekday: Option<i64>,
    start_time: Option<String>,
    end_time: Option<String>,
    instagram_handle: Option<String>,
}

const SERIES_COLUMNS: &str = "id, slug, name, description, cover_key, owner_user_id, \
                              repeat_rule, weekday, start_time, end_time, instagram_handle";

/// A series by slug for its owner or an admin (booking endpoints). Another
/// host gets 403; an unknown slug is 404.
pub(super) async fn load_series_for_editor(
    state: &Arc<AppState>,
    user: &crate::models::User,
    slug: &str,
) -> Result<SeriesRow> {
    let row: SeriesRow = sqlx::query_as(&format!(
        "SELECT {SERIES_COLUMNS} FROM show_templates WHERE slug = ?"
    ))
    .bind(slug)
    .fetch_optional(&state.db)
    .await?
    .ok_or_else(|| AppError::NotFound("Show not found".to_string()))?;
    if row.owner_user_id != user.id && !user.role_enum().can_access_admin() {
        return Err(AppError::Forbidden(
            "You can only book episodes of your own shows".to_string(),
        ));
    }
    Ok(row)
}

impl SeriesRow {
    pub(super) fn rhythm(&self) -> Option<Rhythm> {
        Rhythm::from_columns(
            self.repeat_rule.as_deref(),
            self.weekday,
            self.start_time.as_deref(),
            self.end_time.as_deref(),
        )
    }
}

#[derive(Debug, Serialize)]
pub struct SeriesItem {
    id: i64,
    slug: String,
    name: String,
    description: Option<String>,
    cover_url: Option<String>,
    rhythm: Option<Rhythm>,
    /// e.g. "Sundays · 12:00–14:00"; null while the series has no rhythm.
    rhythm_line: Option<String>,
    /// The host's own Instagram account, without '@'.
    instagram_handle: Option<String>,
}

async fn series_item(state: &Arc<AppState>, row: SeriesRow) -> SeriesItem {
    let rhythm = row.rhythm();
    let cover_url = match &row.cover_key {
        Some(key) => storage::get_presigned_url(state, key, 3600).await.ok(),
        None => None,
    };
    SeriesItem {
        id: row.id,
        rhythm_line: rhythm.as_ref().map(Rhythm::line),
        rhythm,
        slug: row.slug,
        name: row.name,
        description: row.description,
        instagram_handle: row.instagram_handle,
        cover_url,
    }
}

/// GET /api/series — the caller's series, by name.
pub async fn api_list_series(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let user = require_show_creator(&state, &headers).await?;
    let rows: Vec<SeriesRow> = sqlx::query_as(&format!(
        "SELECT {SERIES_COLUMNS} FROM show_templates \
         WHERE owner_user_id = ? AND slug IS NOT NULL ORDER BY name COLLATE NOCASE, id"
    ))
    .bind(user.id)
    .fetch_all(&state.db)
    .await?;

    let mut items = Vec::with_capacity(rows.len());
    for row in rows {
        items.push(series_item(&state, row).await);
    }
    Ok(Json(serde_json::json!({ "series": items })))
}

#[derive(Default)]
struct CreateSeriesForm {
    name: Option<String>,
    description: Option<String>,
    repeat_rule: Option<String>,
    weekday: Option<String>,
    start_time: Option<String>,
    end_time: Option<String>,
    instagram_handle: Option<String>,
    cover: Option<Vec<u8>>,
}

impl CreateSeriesForm {
    async fn read(mut multipart: Multipart) -> Result<Self> {
        let mut form = Self::default();
        while let Some(field) = multipart
            .next_field()
            .await
            .map_err(|e| AppError::BadRequest(format!("Failed to read multipart field: {e}")))?
        {
            let name = field.name().unwrap_or("").to_string();
            if name == "cover" {
                let data = field
                    .bytes()
                    .await
                    .map_err(|e| AppError::BadRequest(format!("Failed to read cover: {e}")))?;
                if !data.is_empty() {
                    form.cover = Some(data.to_vec());
                }
                continue;
            }
            let slot = match name.as_str() {
                "name" => &mut form.name,
                "description" => &mut form.description,
                "repeat_rule" => &mut form.repeat_rule,
                "weekday" => &mut form.weekday,
                "start_time" => &mut form.start_time,
                "end_time" => &mut form.end_time,
                "instagram_handle" => &mut form.instagram_handle,
                _ => continue,
            };
            let text = field
                .text()
                .await
                .map_err(|e| AppError::BadRequest(format!("Failed to read {name}: {e}")))?;
            *slot = Some(text);
        }
        Ok(form)
    }

    /// The Instagram handle: `None` when the field was not sent, `Some(None)`
    /// when it was sent empty (clears it). A leading '@' is dropped.
    fn instagram_handle(&self) -> Result<Option<Option<String>>> {
        let Some(raw) = self.instagram_handle.as_deref() else {
            return Ok(None);
        };
        let handle = raw.trim();
        let handle = handle.strip_prefix('@').unwrap_or(handle);
        if handle.is_empty() {
            return Ok(Some(None));
        }
        let valid = handle.len() <= 30
            && handle
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '.' || c == '_');
        if !valid {
            return Err(AppError::BadRequest(
                "Instagram handle may only use letters, digits, '.' and '_' (max 30)".to_string(),
            ));
        }
        Ok(Some(Some(handle.to_string())))
    }

    /// The rhythm, or `None` for "No fixed rhythm" (`repeat_rule=none`).
    fn rhythm(&self) -> Result<Option<Rhythm>> {
        if self.repeat_rule.as_deref().map(str::trim) == Some("none") {
            return Ok(None);
        }
        let repeat_rule = self
            .repeat_rule
            .as_deref()
            .and_then(RepeatRule::parse)
            .ok_or_else(|| {
                AppError::BadRequest("Repeats must be 'weekly', 'biweekly' or 'none'".to_string())
            })?;
        let weekday = self
            .weekday
            .as_deref()
            .and_then(|d| d.trim().parse::<i64>().ok())
            .filter(|d| (1..=7).contains(d))
            .ok_or_else(|| {
                AppError::BadRequest("Day must be 1 (Monday) to 7 (Sunday)".to_string())
            })?;
        let start = self.start_time.as_deref().unwrap_or("");
        let end = self.end_time.as_deref().unwrap_or("");
        match (series::parse_hhmm(start), series::parse_hhmm(end)) {
            (Some(s), Some(e)) if s < e => {}
            (Some(_), Some(_)) => {
                return Err(AppError::BadRequest(
                    "The end time must be after the start time".to_string(),
                ))
            }
            _ => {
                return Err(AppError::BadRequest(
                    "Start and end time must be HH:MM".to_string(),
                ))
            }
        }
        Ok(Some(Rhythm {
            repeat_rule,
            weekday,
            start_time: start.to_string(),
            end_time: end.to_string(),
        }))
    }
}

/// POST /api/series — multipart: `name`, `description`, `repeat_rule`
/// (`weekly` | `biweekly` | `none`), `weekday` (1-7, Monday first),
/// `start_time`, `end_time` ("HH:MM"), and an optional `cover` image file.
/// With `none` (No fixed rhythm) the day and times are not needed: each
/// episode is booked on a free date and time.
pub async fn api_create_series(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    multipart: Multipart,
) -> Result<impl IntoResponse> {
    let user = require_show_creator(&state, &headers).await?;
    let form = CreateSeriesForm::read(multipart).await?;

    let name = form.name.as_deref().map(str::trim).unwrap_or("");
    if name.is_empty() {
        return Err(AppError::BadRequest("Name is required".to_string()));
    }
    let description = form
        .description
        .as_deref()
        .map(str::trim)
        .filter(|d| !d.is_empty());
    let rhythm = form.rhythm()?;
    let instagram_handle = form.instagram_handle()?.flatten();

    let (id, slug) =
        series::insert_series(&state.db, user.id, name, description, rhythm.as_ref()).await?;
    if instagram_handle.is_some() {
        sqlx::query("UPDATE show_templates SET instagram_handle = ? WHERE id = ?")
            .bind(&instagram_handle)
            .bind(id)
            .execute(&state.db)
            .await?;
    }

    if let Some(cover) = form.cover {
        // Same storage as template covers. No series without its cover:
        // remove the row again if the upload fails.
        match storage::upload_template_cover(&state, id, cover).await {
            Ok(key) => {
                sqlx::query("UPDATE show_templates SET cover_key = ? WHERE id = ?")
                    .bind(key)
                    .bind(id)
                    .execute(&state.db)
                    .await?;
            }
            Err(e) => {
                sqlx::query("DELETE FROM show_templates WHERE id = ?")
                    .bind(id)
                    .execute(&state.db)
                    .await?;
                return Err(e);
            }
        }
    }

    let row = load_own_series(&state, user.id, &slug).await?;
    Ok((StatusCode::CREATED, Json(series_item(&state, row).await)))
}

/// PUT /api/series/:slug — the owner edits a series. Same multipart fields as
/// POST /api/series; `cover` is optional and kept when left out. The slug
/// stays, so links keep working. Booked episodes keep their date and time.
pub async fn api_update_series(
    State(state): State<Arc<AppState>>,
    Path(slug): Path<String>,
    headers: HeaderMap,
    multipart: Multipart,
) -> Result<impl IntoResponse> {
    let user = require_show_creator(&state, &headers).await?;
    let row = load_own_series(&state, user.id, &slug).await?;
    let form = CreateSeriesForm::read(multipart).await?;

    let name = form.name.as_deref().map(str::trim).unwrap_or("");
    if name.is_empty() {
        return Err(AppError::BadRequest("Name is required".to_string()));
    }
    let description = form
        .description
        .as_deref()
        .map(str::trim)
        .filter(|d| !d.is_empty());
    let rhythm = form.rhythm()?;
    let instagram_handle = form
        .instagram_handle()?
        .unwrap_or_else(|| row.instagram_handle.clone());
    let cover_key = match form.cover {
        Some(cover) => Some(storage::upload_template_cover(&state, row.id, cover).await?),
        None => row.cover_key.clone(),
    };

    sqlx::query(
        "UPDATE show_templates SET name = ?, description = ?, cover_key = ?, repeat_rule = ?, \
         weekday = ?, start_time = ?, end_time = ?, instagram_handle = ? WHERE id = ?",
    )
    .bind(name)
    .bind(description)
    .bind(cover_key)
    .bind(rhythm.as_ref().map(|r| r.repeat_rule.as_str()))
    .bind(rhythm.as_ref().map(|r| r.weekday))
    .bind(rhythm.as_ref().map(|r| r.start_time.as_str()))
    .bind(rhythm.as_ref().map(|r| r.end_time.as_str()))
    .bind(instagram_handle)
    .bind(row.id)
    .execute(&state.db)
    .await?;

    let row = load_own_series(&state, user.id, &slug).await?;
    Ok(Json(series_item(&state, row).await))
}

async fn load_own_series(state: &Arc<AppState>, owner_id: i64, slug: &str) -> Result<SeriesRow> {
    sqlx::query_as(&format!(
        "SELECT {SERIES_COLUMNS} FROM show_templates WHERE slug = ? AND owner_user_id = ?"
    ))
    .bind(slug)
    .bind(owner_id)
    .fetch_optional(&state.db)
    .await?
    .ok_or_else(|| AppError::NotFound("Show not found".to_string()))
}

/// The episode on air, so its end time does not end it: the one being
/// recorded (the session lasts through the reconnect grace after a drop), else
/// the live stream's show, which has no session when "Record this show" is off.
pub(crate) async fn on_air_show_id(state: &AppState) -> Option<i64> {
    let recorded = state.recording_manager.lock().await.current_show_id();
    if recorded.is_some() {
        return recorded;
    }
    state.stream_state.lock().await.on_air_show_id()
}

#[derive(sqlx::FromRow)]
struct EpisodeRow {
    id: i64,
    title: String,
    date: String,
    start_time: Option<String>,
    end_time: Option<String>,
    stream_mode: Option<String>,
    wrapup_published_at: Option<String>,
    live_ended_at: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct EpisodeItem {
    id: i64,
    title: String,
    date: String,
    start_time: Option<String>,
    end_time: Option<String>,
    /// "live" | "prerecorded"
    mode: &'static str,
    /// "Live" | "Pre-recorded"
    mode_label: &'static str,
    status: EpisodeStatus,
    /// "Upcoming" | "Finished · wrap-up open" | "Finished"
    status_label: &'static str,
}

#[derive(Debug, Serialize)]
pub struct SeriesDetail {
    #[serde(flatten)]
    series: SeriesItem,
    /// Newest first.
    episodes: Vec<EpisodeItem>,
}

/// GET /api/series/:slug — one of the caller's series with its episodes.
pub async fn api_get_series(
    State(state): State<Arc<AppState>>,
    Path(slug): Path<String>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let user = require_show_creator(&state, &headers).await?;
    let row = load_own_series(&state, user.id, &slug).await?;
    let rhythm_end = row.rhythm().map(|r| r.end_time);

    let rows: Vec<EpisodeRow> = sqlx::query_as(
        "SELECT id, title, date, start_time, end_time, stream_mode, wrapup_published_at, \
                live_ended_at \
         FROM shows WHERE template_id = ? \
         ORDER BY date DESC, start_time DESC, id DESC",
    )
    .bind(row.id)
    .fetch_all(&state.db)
    .await?;

    let on_air = on_air_show_id(&state).await;
    let now = Utc::now();
    let episodes = rows
        .into_iter()
        .map(|e| {
            let end = series::ended_by_host(
                series::episode_end_utc(
                    &e.date,
                    e.start_time.as_deref(),
                    e.end_time.as_deref(),
                    rhythm_end.as_deref(),
                ),
                e.live_ended_at.as_deref(),
            );
            let status = series::episode_status(
                end,
                e.wrapup_published_at.is_some(),
                on_air == Some(e.id),
                now,
            );
            let (mode, mode_label) = series::mode_of(e.stream_mode.as_deref());
            EpisodeItem {
                id: e.id,
                title: e.title,
                date: e.date,
                start_time: e.start_time,
                end_time: e.end_time,
                mode,
                mode_label,
                status,
                status_label: status.label(),
            }
        })
        .collect();

    Ok(Json(SeriesDetail {
        series: series_item(&state, row).await,
        episodes,
    }))
}

#[derive(sqlx::FromRow)]
struct UpcomingRow {
    id: i64,
    date: String,
    start_time: Option<String>,
    end_time: Option<String>,
    stream_mode: Option<String>,
    wrapup_published_at: Option<String>,
    live_ended_at: Option<String>,
    show_name: String,
    host_username: String,
}

/// One row of the host Dashboard.
#[derive(Debug, Serialize)]
pub struct UpcomingEpisode {
    id: i64,
    date: String,
    start_time: Option<String>,
    end_time: Option<String>,
    show_name: String,
    host_username: String,
    /// "live" | "prerecorded"
    mode: &'static str,
    /// "Live" | "Pre-recorded"
    mode_label: &'static str,
}

/// GET /api/upcoming-episodes — the upcoming episodes of all hosts' series,
/// by date and start time.
pub async fn api_upcoming_episodes(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_show_creator(&state, &headers).await?;
    // An episode ends at most a day after its date, so older dates are over.
    let rows: Vec<UpcomingRow> = sqlx::query_as(
        "SELECT s.id, s.date, COALESCE(s.start_time, t.start_time) AS start_time, \
                COALESCE(s.end_time, t.end_time) AS end_time, s.stream_mode, \
                s.wrapup_published_at, s.live_ended_at, t.name AS show_name, \
                u.username AS host_username \
         FROM shows s \
         JOIN show_templates t ON t.id = s.template_id \
         JOIN users u ON u.id = t.owner_user_id \
         WHERE t.slug IS NOT NULL AND s.date >= date('now', '-2 days') \
         ORDER BY s.date, start_time, s.id",
    )
    .fetch_all(&state.db)
    .await?;

    let on_air = on_air_show_id(&state).await;
    let now = Utc::now();
    let episodes: Vec<UpcomingEpisode> = rows
        .into_iter()
        .filter(|e| {
            let end = series::ended_by_host(
                series::episode_end_utc(
                    &e.date,
                    e.start_time.as_deref(),
                    e.end_time.as_deref(),
                    None,
                ),
                e.live_ended_at.as_deref(),
            );
            series::episode_status(
                end,
                e.wrapup_published_at.is_some(),
                on_air == Some(e.id),
                now,
            ) == EpisodeStatus::Upcoming
        })
        .map(|e| {
            let (mode, mode_label) = series::mode_of(e.stream_mode.as_deref());
            UpcomingEpisode {
                id: e.id,
                date: e.date,
                start_time: e.start_time,
                end_time: e.end_time,
                show_name: e.show_name,
                host_username: e.host_username,
                mode,
                mode_label,
            }
        })
        .collect();

    Ok(Json(serde_json::json!({ "episodes": episodes })))
}
