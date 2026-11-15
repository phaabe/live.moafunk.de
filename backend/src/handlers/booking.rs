//! Booking API for host episodes: Book Slot, the upload-first pre-recorded
//! set, Replace file, Cancel booking and Publish.
//!
//! Series routes take the slug and allow the series owner or an admin.
//! Episode routes take the show id and allow an admin, the host or the
//! creator (`require_show_editor`). Everyone else gets 403.

use std::sync::Arc;

use axum::{
    extract::{Multipart, Path, Query, State},
    http::{HeaderMap, StatusCode},
    response::IntoResponse,
    Json,
};
use chrono::{DateTime, NaiveDate, Utc};
use chrono_tz::Europe::Berlin;
use serde::{Deserialize, Serialize};
use serde_json::json;

use super::api::{self, require_show_creator, require_show_editor};
use super::series::{load_series_for_editor, SeriesRow};
use crate::series::{self as model, EpisodeStatus};
use crate::{audio, scheduler, storage, AppError, AppState, Result};

// ============================================================================
// Slots
// ============================================================================

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
struct Slot {
    date: String,
    start_time: String,
    end_time: String,
}

/// The date of the series' latest episode: "Every 2 weeks" counts from it.
async fn latest_episode_date(state: &AppState, series_id: i64) -> Result<Option<NaiveDate>> {
    let date: Option<String> =
        sqlx::query_scalar("SELECT MAX(date) FROM shows WHERE template_id = ?")
            .bind(series_id)
            .fetch_one(&state.db)
            .await?;
    Ok(date.and_then(|d| NaiveDate::parse_from_str(&d, "%Y-%m-%d").ok()))
}

/// Rhythm slots for the next weeks whose start is still ahead, free or not.
async fn rhythm_slots(
    state: &AppState,
    series: &SeriesRow,
    now: DateTime<Utc>,
) -> Result<Vec<Slot>> {
    let Some(rhythm) = series.rhythm() else {
        return Ok(Vec::new());
    };
    let today = now.with_timezone(&Berlin).date_naive();
    let anchor = latest_episode_date(state, series.id).await?;
    Ok(
        model::rhythm_dates(&rhythm, anchor, today, model::SLOT_WEEKS)
            .into_iter()
            .map(|d| d.format("%Y-%m-%d").to_string())
            .filter(|date| {
                scheduler::show_start_utc(date, &rhythm.start_time).is_some_and(|start| start > now)
            })
            .map(|date| Slot {
                date,
                start_time: rhythm.start_time.clone(),
                end_time: rhythm.end_time.clone(),
            })
            .collect(),
    )
}

/// True when any show on the slot's date overlaps it (the same rule as
/// `ensure_no_show_conflict`).
async fn is_taken(state: &AppState, slot: &Slot) -> Result<bool> {
    let others: Vec<(String, Option<String>)> = sqlx::query_as(
        "SELECT start_time, end_time FROM shows WHERE date = ? AND start_time IS NOT NULL",
    )
    .bind(&slot.date)
    .fetch_all(&state.db)
    .await?;
    Ok(others.iter().any(|(start, end)| {
        api::time_windows_overlap(
            &slot.start_time,
            Some(&slot.end_time),
            start,
            end.as_deref(),
        )
    }))
}

/// GET /api/series/:slug/slots — free slots of the rhythm for the next
/// 12 weeks.
pub async fn api_series_slots(
    State(state): State<Arc<AppState>>,
    Path(slug): Path<String>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let user = require_show_creator(&state, &headers).await?;
    let series = load_series_for_editor(&state, &user, &slug).await?;
    let mut free = Vec::new();
    for slot in rhythm_slots(&state, &series, Utc::now()).await? {
        if !is_taken(&state, &slot).await? {
            free.push(slot);
        }
    }
    Ok(Json(json!({ "slots": free })))
}

// ============================================================================
// Book
// ============================================================================

#[derive(Debug, Deserialize)]
pub struct BookRequest {
    date: String,
    start_time: String,
    end_time: String,
    /// "live" | "prerecorded"
    mode: String,
    /// A finished upload of this series; required for "prerecorded".
    upload_id: Option<String>,
}

#[derive(sqlx::FromRow)]
struct FinishedUpload {
    session_id: String,
    filename: String,
    file_key: String,
    duration_secs: Option<i64>,
}

async fn finished_upload(
    conn: &mut sqlx::SqliteConnection,
    series_id: i64,
    upload_id: Option<&str>,
) -> Result<FinishedUpload> {
    let Some(upload_id) = upload_id else {
        return Err(AppError::BadRequest(
            "Upload the set before you book a pre-recorded episode".to_string(),
        ));
    };
    sqlx::query_as(
        "SELECT session_id, filename, file_key, duration_secs FROM series_uploads \
         WHERE session_id = ? AND series_id = ? AND file_key IS NOT NULL \
           AND expires_at > datetime('now')",
    )
    .bind(upload_id)
    .bind(series_id)
    .fetch_optional(&mut *conn)
    .await?
    .ok_or_else(|| {
        AppError::BadRequest(
            "The upload is not finished or has expired. Upload the set again.".to_string(),
        )
    })
}

/// POST /api/series/:slug/episodes — book a free slot as a live or a
/// pre-recorded episode. Returns the new episode.
pub async fn api_book_episode(
    State(state): State<Arc<AppState>>,
    Path(slug): Path<String>,
    headers: HeaderMap,
    Json(req): Json<BookRequest>,
) -> Result<impl IntoResponse> {
    let user = require_show_creator(&state, &headers).await?;
    let series = load_series_for_editor(&state, &user, &slug).await?;

    let slot = Slot {
        date: req.date,
        start_time: req.start_time,
        end_time: req.end_time,
    };
    if !rhythm_slots(&state, &series, Utc::now())
        .await?
        .contains(&slot)
    {
        return Err(AppError::BadRequest(
            "Pick a date and time from this show's slots".to_string(),
        ));
    }
    // Someone may have taken the slot since the list was loaded: 409.
    api::ensure_no_show_conflict(
        &state,
        &slot.date,
        Some(&slot.start_time),
        Some(&slot.end_time),
        None,
    )
    .await?;
    let prerecorded = match req.mode.as_str() {
        "live" => false,
        "prerecorded" => true,
        _ => {
            return Err(AppError::BadRequest(
                "Mode must be 'live' or 'prerecorded'".to_string(),
            ))
        }
    };

    let mut tx = state.db.begin().await?;
    let upload = if prerecorded {
        Some(finished_upload(&mut tx, series.id, req.upload_id.as_deref()).await?)
    } else {
        None
    };
    let number: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM shows WHERE template_id = ?")
        .bind(series.id)
        .fetch_one(&mut *tx)
        .await?;
    let title = format!("{} #{}", series.name, number + 1);
    let stream_mode = if prerecorded { "prerecorded" } else { "live" };
    let confirmed_at = upload.as_ref().map(|_| Utc::now().to_rfc3339());

    let id = sqlx::query(
        "INSERT INTO shows (title, date, status, show_type, start_time, end_time, \
         host_user_id, created_by, stream_mode, template_id, \
         prerecorded_key, prerecorded_filename, prerecorded_confirmed_at) \
         VALUES (?, ?, 'scheduled', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    )
    .bind(&title)
    .bind(&slot.date)
    .bind(api::HOST_DEFAULT_SHOW_TYPE)
    .bind(&slot.start_time)
    .bind(&slot.end_time)
    // The series owner hosts; an admin booking for a host stays the creator.
    .bind(series.owner_user_id)
    .bind(user.id)
    .bind(stream_mode)
    .bind(series.id)
    .bind(upload.as_ref().map(|u| u.file_key.as_str()))
    .bind(upload.as_ref().map(|u| u.filename.as_str()))
    .bind(&confirmed_at)
    .execute(&mut *tx)
    .await?
    .last_insert_rowid();
    if let Some(upload) = &upload {
        // One file plays in one episode.
        sqlx::query("DELETE FROM series_uploads WHERE session_id = ?")
            .bind(&upload.session_id)
            .execute(&mut *tx)
            .await?;
    }
    tx.commit().await?;

    // Seed the cover like POST /api/shows: the series cover, else the default.
    let cover = match &series.cover_key {
        Some(key) => api::copy_template_cover_to_show(&state, key, id).await,
        None => None,
    };
    if cover.is_none() {
        api::copy_default_cover_to_show(&state, id).await;
    }

    Ok((
        StatusCode::CREATED,
        Json(json!({
            "id": id,
            "title": title,
            "date": slot.date,
            "start_time": slot.start_time,
            "end_time": slot.end_time,
            "mode": stream_mode,
            "prerecorded_filename": upload.as_ref().map(|u| &u.filename),
            "duration": upload.as_ref().and_then(|u| u.duration_secs),
        })),
    ))
}

// ============================================================================
// Upload first: chunked upload of a pre-recorded set for a series
// ============================================================================

/// Upload sessions and finished, unbooked files expire after this.
const UPLOAD_TTL: &str = "+24 hours";

fn audio_content_type(filename: &str) -> Option<&'static str> {
    let ext = filename.rsplit_once('.')?.1.to_ascii_lowercase();
    match ext.as_str() {
        "mp3" => Some("audio/mpeg"),
        "wav" => Some("audio/wav"),
        _ => None,
    }
}

async fn delete_object(state: &AppState, key: &str) {
    if let Err(e) = state
        .s3_client
        .delete_object()
        .bucket(&state.config.r2_bucket_name)
        .key(key)
        .send()
        .await
    {
        tracing::warn!("Could not delete upload object {key}: {e}");
    }
}

/// Drop expired upload sessions with their chunks and files. Runs before a
/// new upload starts; a failed delete leaves an object behind, not a row.
async fn sweep_expired_uploads(state: &AppState) {
    let expired: Vec<(String, Option<String>)> = match sqlx::query_as(
        "SELECT session_id, file_key FROM series_uploads WHERE expires_at <= datetime('now')",
    )
    .fetch_all(&state.db)
    .await
    {
        Ok(rows) => rows,
        Err(e) => {
            tracing::warn!("Could not list expired series uploads: {e}");
            return;
        }
    };
    for (session_id, file_key) in expired {
        let chunks: Vec<String> =
            sqlx::query_scalar("SELECT chunk_key FROM series_upload_chunks WHERE session_id = ?")
                .bind(&session_id)
                .fetch_all(&state.db)
                .await
                .unwrap_or_default();
        for key in chunks.iter().chain(file_key.iter()) {
            delete_object(state, key).await;
        }
        if let Err(e) = sqlx::query("DELETE FROM series_uploads WHERE session_id = ?")
            .bind(&session_id)
            .execute(&state.db)
            .await
        {
            tracing::warn!("Could not delete expired series upload {session_id}: {e}");
        }
    }
}

#[derive(Debug, Deserialize)]
pub struct UploadInitRequest {
    filename: String,
    total_chunks: u32,
}

/// POST /api/series/:slug/upload/init — start a chunked MP3 or WAV upload.
pub async fn api_series_upload_init(
    State(state): State<Arc<AppState>>,
    Path(slug): Path<String>,
    headers: HeaderMap,
    Json(req): Json<UploadInitRequest>,
) -> Result<impl IntoResponse> {
    let user = require_show_creator(&state, &headers).await?;
    let series = load_series_for_editor(&state, &user, &slug).await?;
    let filename = req.filename.trim();
    if audio_content_type(filename).is_none() {
        return Err(AppError::BadRequest(
            "Upload an MP3 or WAV file".to_string(),
        ));
    }
    if req.total_chunks == 0 {
        return Err(AppError::BadRequest(
            "An upload needs at least one chunk".to_string(),
        ));
    }
    sweep_expired_uploads(&state).await;

    let session_id = uuid::Uuid::new_v4().to_string();
    sqlx::query(
        "INSERT INTO series_uploads (session_id, series_id, user_id, filename, total_chunks, expires_at) \
         VALUES (?, ?, ?, ?, ?, datetime('now', ?))",
    )
    .bind(&session_id)
    .bind(series.id)
    .bind(user.id)
    .bind(filename)
    .bind(i64::from(req.total_chunks))
    .bind(UPLOAD_TTL)
    .execute(&state.db)
    .await?;

    Ok(Json(json!({ "session_id": session_id })))
}

#[derive(sqlx::FromRow)]
struct OpenUpload {
    filename: String,
    total_chunks: i64,
    file_key: Option<String>,
    duration_secs: Option<i64>,
}

async fn open_upload(state: &AppState, series_id: i64, session_id: &str) -> Result<OpenUpload> {
    sqlx::query_as(
        "SELECT filename, total_chunks, file_key, duration_secs FROM series_uploads \
         WHERE session_id = ? AND series_id = ? AND expires_at > datetime('now')",
    )
    .bind(session_id)
    .bind(series_id)
    .fetch_optional(&state.db)
    .await?
    .ok_or_else(|| {
        AppError::BadRequest("Upload not found or expired. Please start over.".to_string())
    })
}

#[derive(Debug, Deserialize)]
pub struct ChunkQuery {
    index: u32,
}

/// POST /api/series/:slug/upload/chunk/:session_id?index=N — one chunk
/// (multipart field `chunk`).
pub async fn api_series_upload_chunk(
    State(state): State<Arc<AppState>>,
    Path((slug, session_id)): Path<(String, String)>,
    Query(query): Query<ChunkQuery>,
    headers: HeaderMap,
    mut multipart: Multipart,
) -> Result<impl IntoResponse> {
    let user = require_show_creator(&state, &headers).await?;
    let series = load_series_for_editor(&state, &user, &slug).await?;
    let upload = open_upload(&state, series.id, &session_id).await?;
    if upload.file_key.is_some() {
        return Err(AppError::BadRequest(
            "This upload is already finished".to_string(),
        ));
    }
    if i64::from(query.index) >= upload.total_chunks {
        return Err(AppError::BadRequest(format!(
            "Chunk index {} exceeds total chunks {}",
            query.index, upload.total_chunks
        )));
    }

    let mut data: Option<Vec<u8>> = None;
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|e| AppError::BadRequest(format!("Failed to read multipart: {e}")))?
    {
        if matches!(field.name(), Some("chunk" | "file")) {
            let bytes = field
                .bytes()
                .await
                .map_err(|e| AppError::BadRequest(format!("Failed to read chunk: {e}")))?;
            data = Some(bytes.to_vec());
        }
    }
    let data = data.ok_or_else(|| AppError::BadRequest("No chunk data provided".to_string()))?;
    let size = data.len();

    let chunk_key = format!(
        "series-uploads/{}/{}/chunk-{:04}",
        series.id, session_id, query.index
    );
    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&chunk_key)
        .body(aws_sdk_s3::primitives::ByteStream::from(data))
        .content_type("application/octet-stream")
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload chunk: {e}")))?;
    sqlx::query(
        "INSERT OR REPLACE INTO series_upload_chunks (session_id, chunk_index, chunk_key, size_bytes) \
         VALUES (?, ?, ?, ?)",
    )
    .bind(&session_id)
    .bind(i64::from(query.index))
    .bind(&chunk_key)
    .bind(size as i64)
    .execute(&state.db)
    .await?;

    Ok(Json(
        json!({ "index": query.index, "received_bytes": size }),
    ))
}

/// Duration in whole seconds, read with ffprobe from a temporary copy.
async fn probe_duration_secs(data: &[u8], filename: &str, session_id: &str) -> Result<i64> {
    let ext = filename.rsplit_once('.').map(|(_, e)| e).unwrap_or("bin");
    let path = std::env::temp_dir().join(format!("series-upload-{session_id}.{ext}"));
    tokio::fs::write(&path, data)
        .await
        .map_err(|e| AppError::Internal(format!("Failed to write temp upload: {e}")))?;
    let duration = audio::get_duration(&path).await;
    let _ = tokio::fs::remove_file(&path).await;
    let ms = duration.map_err(|e| {
        tracing::warn!("ffprobe could not read upload {session_id}: {e}");
        AppError::BadRequest("This file is not a readable MP3 or WAV".to_string())
    })?;
    Ok(((ms + 500) / 1000) as i64)
}

/// POST /api/series/:slug/upload/finalize/:session_id — join the chunks,
/// store the set and return its file name and duration in seconds. Safe to
/// call again after it succeeded.
pub async fn api_series_upload_finalize(
    State(state): State<Arc<AppState>>,
    Path((slug, session_id)): Path<(String, String)>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let user = require_show_creator(&state, &headers).await?;
    let series = load_series_for_editor(&state, &user, &slug).await?;
    let upload = open_upload(&state, series.id, &session_id).await?;
    if upload.file_key.is_some() {
        return Ok(Json(json!({
            "upload_id": session_id,
            "filename": upload.filename,
            "duration": upload.duration_secs,
        })));
    }

    let chunks: Vec<String> = sqlx::query_scalar(
        "SELECT chunk_key FROM series_upload_chunks WHERE session_id = ? ORDER BY chunk_index",
    )
    .bind(&session_id)
    .fetch_all(&state.db)
    .await?;
    if chunks.len() as i64 != upload.total_chunks {
        return Err(AppError::BadRequest(format!(
            "Missing chunks: received {} of {}",
            chunks.len(),
            upload.total_chunks
        )));
    }

    let mut data = Vec::new();
    for key in &chunks {
        let object = state
            .s3_client
            .get_object()
            .bucket(&state.config.r2_bucket_name)
            .key(key)
            .send()
            .await
            .map_err(|e| AppError::Storage(format!("Failed to fetch chunk {key}: {e}")))?;
        let bytes = object
            .body
            .collect()
            .await
            .map_err(|e| AppError::Storage(format!("Failed to read chunk {key}: {e}")))?
            .into_bytes();
        data.extend_from_slice(&bytes);
    }

    let duration = probe_duration_secs(&data, &upload.filename, &session_id).await?;
    let content_type = audio_content_type(&upload.filename).unwrap_or("application/octet-stream");
    let file_key = format!(
        "series-uploads/{}/{}/{}",
        series.id,
        session_id,
        storage::sanitize_object_name(&upload.filename)
    );
    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&file_key)
        .body(aws_sdk_s3::primitives::ByteStream::from(data))
        .content_type(content_type)
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to store the set: {e}")))?;

    sqlx::query(
        "UPDATE series_uploads SET file_key = ?, duration_secs = ?, expires_at = datetime('now', ?) \
         WHERE session_id = ?",
    )
    .bind(&file_key)
    .bind(duration)
    .bind(UPLOAD_TTL)
    .bind(&session_id)
    .execute(&state.db)
    .await?;
    sqlx::query("DELETE FROM series_upload_chunks WHERE session_id = ?")
        .bind(&session_id)
        .execute(&state.db)
        .await?;
    for key in &chunks {
        delete_object(&state, key).await;
    }

    Ok(Json(json!({
        "upload_id": session_id,
        "filename": upload.filename,
        "duration": duration,
    })))
}

// ============================================================================
// Episode: replace file, cancel, publish
// ============================================================================

async fn series_id_of(state: &AppState, show_id: i64) -> Result<Option<i64>> {
    Ok(
        sqlx::query_scalar("SELECT template_id FROM shows WHERE id = ?")
            .bind(show_id)
            .fetch_one(&state.db)
            .await?,
    )
}

#[derive(Debug, Deserialize)]
pub struct ReplaceFileRequest {
    upload_id: String,
}

/// PUT /api/shows/:id/prerecorded — swap the set of a booked pre-recorded
/// episode for a finished upload. The booking (date, time, mode, confirmed)
/// stays as it is.
pub async fn api_replace_prerecorded(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    Json(req): Json<ReplaceFileRequest>,
) -> Result<impl IntoResponse> {
    let (_user, show) = require_show_editor(&state, &headers, id).await?;
    if show.stream_mode.as_deref() != Some("prerecorded") {
        return Err(AppError::BadRequest(
            "Only a pre-recorded episode has a file to replace".to_string(),
        ));
    }
    if show.prerecorded_started_at.is_some() {
        return Err(AppError::Conflict("The set is already playing".to_string()));
    }
    let series_id = series_id_of(&state, id).await?.ok_or_else(|| {
        AppError::BadRequest("This episode does not belong to a show".to_string())
    })?;

    let mut tx = state.db.begin().await?;
    let upload = finished_upload(&mut tx, series_id, Some(&req.upload_id)).await?;
    sqlx::query("UPDATE shows SET prerecorded_key = ?, prerecorded_filename = ? WHERE id = ?")
        .bind(&upload.file_key)
        .bind(&upload.filename)
        .bind(id)
        .execute(&mut *tx)
        .await?;
    sqlx::query("DELETE FROM series_uploads WHERE session_id = ?")
        .bind(&upload.session_id)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;

    Ok(Json(json!({
        "id": id,
        "prerecorded_filename": upload.filename,
        "duration": upload.duration_secs,
    })))
}

/// DELETE /api/shows/:id/booking — cancel an episode before it starts. The
/// slot is free again afterwards.
pub async fn api_cancel_booking(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let (_user, show) = require_show_editor(&state, &headers, id).await?;
    let start = show
        .start_time
        .as_deref()
        .and_then(|t| scheduler::show_start_utc(&show.date, t));
    if start.is_none_or(|start| Utc::now() >= start) {
        return Err(AppError::BadRequest(
            "Only an episode that has not started yet can be cancelled".to_string(),
        ));
    }
    let slug: Option<String> = sqlx::query_scalar(
        "SELECT t.slug FROM shows s JOIN show_templates t ON t.id = s.template_id WHERE s.id = ?",
    )
    .bind(id)
    .fetch_optional(&state.db)
    .await?
    .flatten();

    sqlx::query("DELETE FROM shows WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await?;
    let _ = storage::delete_show_cover(&state, id).await;

    Ok(Json(json!({ "success": true, "series_slug": slug })))
}

/// POST /api/shows/:id/publish — close the wrap-up of an ended episode:
/// "Finished · wrap-up open" becomes "Finished". Before the end: 400.
/// Already published: 409.
pub async fn api_publish_episode(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let (_user, show) = require_show_editor(&state, &headers, id).await?;
    let (published_at, rhythm_end): (Option<String>, Option<String>) = sqlx::query_as(
        "SELECT s.wrapup_published_at, t.end_time FROM shows s \
         LEFT JOIN show_templates t ON t.id = s.template_id WHERE s.id = ?",
    )
    .bind(id)
    .fetch_one(&state.db)
    .await?;
    let end = model::episode_end_utc(
        &show.date,
        show.start_time.as_deref(),
        show.end_time.as_deref(),
        rhythm_end.as_deref(),
    );
    let on_air = state.recording_manager.lock().await.current_show_id() == Some(id);
    match model::episode_status(end, published_at.is_some(), on_air, Utc::now()) {
        EpisodeStatus::Upcoming => {
            return Err(AppError::BadRequest(
                "The episode has not ended yet".to_string(),
            ))
        }
        EpisodeStatus::Finished => {
            return Err(AppError::Conflict(
                "This episode is already published".to_string(),
            ))
        }
        EpisodeStatus::WrapupOpen => {}
    }
    let done = sqlx::query(
        "UPDATE shows SET wrapup_published_at = datetime('now') \
         WHERE id = ? AND wrapup_published_at IS NULL",
    )
    .bind(id)
    .execute(&state.db)
    .await?;
    if done.rows_affected() == 0 {
        return Err(AppError::Conflict(
            "This episode is already published".to_string(),
        ));
    }

    let status = EpisodeStatus::Finished;
    Ok(Json(json!({
        "id": id,
        "status": status,
        "status_label": status.label(),
    })))
}
