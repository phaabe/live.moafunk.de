//! Chunked upload handlers.
//!
//! Splits the submission into multiple requests to stay under Cloudflare's 100MB limit:
//! 1. POST /api/submit/init   – text fields + small images → returns session_id
//! 2. POST /api/submit/file/:session_id?field=track1|track2|voice – upload one large file
//! 3. POST /api/submit/finalize/:session_id – commit the submission

use crate::{AppError, AppState, Result};
use axum::{
    extract::{Multipart, Path, Query, State},
    Json,
};
use image::GenericImageView;
use image::{codecs::jpeg::JpegEncoder, codecs::png::PngEncoder, ColorType, ImageEncoder};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use uuid::Uuid;

use crate::storage;

// ─────────────────────────────────────────────────────────────────────────────
// Helpers (duplicated from submit.rs for now; consider extracting to a shared module)
// ─────────────────────────────────────────────────────────────────────────────

fn normalize_image_content_type(filename: &str, fallback: &str) -> String {
    let ext = std::path::Path::new(filename)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();

    match ext.as_str() {
        "png" => "image/png".to_string(),
        "jpg" | "jpeg" => "image/jpeg".to_string(),
        _ => fallback.to_string(),
    }
}

fn crop_square_image_if_supported(filename: &str, data: &[u8]) -> Option<Vec<u8>> {
    let ext = std::path::Path::new(filename)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();

    let format = match ext.as_str() {
        "png" => image::ImageFormat::Png,
        "jpg" | "jpeg" => image::ImageFormat::Jpeg,
        _ => return None,
    };

    let img = image::load_from_memory_with_format(data, format)
        .or_else(|_| image::load_from_memory(data))
        .ok()?;

    let (w, h) = img.dimensions();
    let side = w.min(h);
    if side == 0 {
        return None;
    }

    let x = (w - side) / 2;
    let y = (h - side) / 2;
    let cropped = img.crop_imm(x, y, side, side);

    let mut out = Vec::new();
    match ext.as_str() {
        "png" => {
            let rgba = cropped.to_rgba8();
            let encoder = PngEncoder::new(&mut out);
            encoder
                .write_image(
                    rgba.as_raw(),
                    rgba.width(),
                    rgba.height(),
                    ColorType::Rgba8.into(),
                )
                .ok()?;
            Some(out)
        }
        "jpg" | "jpeg" => {
            let rgb = cropped.to_rgb8();
            let encoder = JpegEncoder::new_with_quality(&mut out, 92);
            encoder
                .write_image(
                    rgb.as_raw(),
                    rgb.width(),
                    rgb.height(),
                    ColorType::Rgb8.into(),
                )
                .ok()?;
            Some(out)
        }
        _ => None,
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Serialize)]
pub struct InitResponse {
    pub success: bool,
    pub session_id: String,
    pub message: String,
}

#[derive(Serialize)]
pub struct FileUploadResponse {
    pub success: bool,
    pub field: String,
    pub message: String,
}

#[derive(Serialize)]
pub struct FinalizeResponse {
    pub success: bool,
    pub message: String,
    pub artist_id: Option<i64>,
}

#[derive(Deserialize)]
pub struct FileQuery {
    pub field: String,
}

// ─────────────────────────────────────────────────────────────────────────────
// Step 1: Initialize submission (text fields + images < 100MB combined)
// ─────────────────────────────────────────────────────────────────────────────

pub async fn submit_init(
    State(state): State<Arc<AppState>>,
    mut multipart: Multipart,
) -> Result<Json<InitResponse>> {
    tracing::info!("Chunked upload: init");

    let mut artist_name = String::new();
    let mut pronouns = String::new();
    let mut track1_name = String::new();
    let mut track2_name = String::new();
    let mut no_voice_message = false;
    let mut instagram: Option<String> = None;
    let mut soundcloud: Option<String> = None;
    let mut bandcamp: Option<String> = None;
    let mut spotify: Option<String> = None;
    let mut other_social: Option<String> = None;
    let mut upcoming_events: Option<String> = None;
    let mut mentions: Option<String> = None;

    let mut artist_pic_original: Option<(String, Vec<u8>, String)> = None;
    let mut artist_pic_cropped: Option<(String, Vec<u8>, String)> = None;
    let mut artist_pic_branded: Option<(String, Vec<u8>, String)> = None;

    let max_size = state.config.max_file_size_bytes();

    while let Some(field) = multipart.next_field().await.map_err(|e| {
        tracing::error!("Failed to read multipart field: {}", e);
        AppError::Validation(format!("Failed to read form field: {}", e))
    })? {
        let name = field.name().unwrap_or("").to_string();

        match name.as_str() {
            "artist-name" => {
                artist_name = field.text().await.map_err(|e| {
                    AppError::Validation(format!("Failed to read artist name: {}", e))
                })?;
            }
            "pronouns" => {
                pronouns = field
                    .text()
                    .await
                    .map_err(|e| AppError::Validation(format!("Failed to read pronouns: {}", e)))?;
            }
            "track1-name" => {
                track1_name = field.text().await.map_err(|e| {
                    AppError::Validation(format!("Failed to read track1 name: {}", e))
                })?;
            }
            "track2-name" => {
                track2_name = field.text().await.map_err(|e| {
                    AppError::Validation(format!("Failed to read track2 name: {}", e))
                })?;
            }
            "no-voice-message" => {
                let value = field.text().await.unwrap_or_default();
                no_voice_message = value == "on" || value == "true";
            }
            "instagram" => {
                instagram = Some(field.text().await.unwrap_or_default()).filter(|s| !s.is_empty())
            }
            "soundcloud" => {
                soundcloud = Some(field.text().await.unwrap_or_default()).filter(|s| !s.is_empty())
            }
            "bandcamp" => {
                bandcamp = Some(field.text().await.unwrap_or_default()).filter(|s| !s.is_empty())
            }
            "spotify" => {
                spotify = Some(field.text().await.unwrap_or_default()).filter(|s| !s.is_empty())
            }
            "other-social" => {
                other_social =
                    Some(field.text().await.unwrap_or_default()).filter(|s| !s.is_empty())
            }
            "upcoming-events" => {
                upcoming_events =
                    Some(field.text().await.unwrap_or_default()).filter(|s| !s.is_empty())
            }
            "mentions" => {
                mentions = Some(field.text().await.unwrap_or_default()).filter(|s| !s.is_empty())
            }
            "artist-pic" => {
                let filename = field.file_name().unwrap_or("image.jpg").to_string();
                let content_type = field.content_type().unwrap_or("image/jpeg").to_string();
                let data = field.bytes().await.map_err(|e| {
                    AppError::Validation(format!("Failed to read artist pic: {}", e))
                })?;
                if data.len() as u64 > max_size {
                    return Err(AppError::FileTooLarge(state.config.max_file_size_mb));
                }
                let normalized = normalize_image_content_type(&filename, &content_type);
                artist_pic_original = Some((filename, data.to_vec(), normalized));
            }
            "artist-pic-cropped" => {
                let filename = field
                    .file_name()
                    .unwrap_or("artist_cropped.jpg")
                    .to_string();
                let content_type = field.content_type().unwrap_or("image/jpeg").to_string();
                let data = field.bytes().await.map_err(|e| {
                    AppError::Validation(format!("Failed to read cropped artist pic: {}", e))
                })?;
                if data.len() as u64 > max_size {
                    return Err(AppError::FileTooLarge(state.config.max_file_size_mb));
                }
                let normalized = normalize_image_content_type(&filename, &content_type);
                artist_pic_cropped = Some((filename, data.to_vec(), normalized));
            }
            "artist-pic-branded" => {
                let filename = field
                    .file_name()
                    .unwrap_or("artist_branded.jpg")
                    .to_string();
                let content_type = field.content_type().unwrap_or("image/jpeg").to_string();
                let data = field.bytes().await.map_err(|e| {
                    AppError::Validation(format!("Failed to read branded artist pic: {}", e))
                })?;
                if data.len() as u64 > max_size {
                    return Err(AppError::FileTooLarge(state.config.max_file_size_mb));
                }
                let normalized = normalize_image_content_type(&filename, &content_type);
                artist_pic_branded = Some((filename, data.to_vec(), normalized));
            }
            _ => {}
        }
    }

    // Validate required text fields
    if artist_name.is_empty() {
        return Err(AppError::Validation("Artist name is required".to_string()));
    }
    if pronouns.is_empty() {
        return Err(AppError::Validation("Pronouns are required".to_string()));
    }
    if track1_name.is_empty() {
        return Err(AppError::Validation("Track 1 name is required".to_string()));
    }
    if track2_name.is_empty() {
        return Err(AppError::Validation("Track 2 name is required".to_string()));
    }
    if artist_pic_original.is_none() {
        return Err(AppError::Validation(
            "Artist picture is required".to_string(),
        ));
    }

    // Generate session ID
    let session_id = Uuid::new_v4().to_string();

    // Upload images to R2 immediately (they're small, always fit in this request)
    // We use session_id as a temporary prefix; finalize will move them to artist_id prefix.
    let (pic_filename, pic_data, pic_content_type) = artist_pic_original.unwrap();

    // For pending submissions we store directly with session prefix
    let pic_key = storage::upload_file_to_pending(
        &state,
        &session_id,
        "pic",
        &pic_filename,
        pic_data.clone(),
        &pic_content_type,
    )
    .await?;

    let (pic_cropped_filename, pic_cropped_data, pic_cropped_content_type) =
        if let Some((f, d, ct)) = artist_pic_cropped {
            (f, d, ct)
        } else {
            let cropped = crop_square_image_if_supported(&pic_filename, &pic_data)
                .unwrap_or_else(|| pic_data.clone());
            (pic_filename.clone(), cropped, pic_content_type.clone())
        };

    let pic_cropped_key = storage::upload_file_to_pending(
        &state,
        &session_id,
        "pic_cropped",
        &pic_cropped_filename,
        pic_cropped_data.clone(),
        &pic_cropped_content_type,
    )
    .await?;

    let (pic_overlay_filename, pic_overlay_data, pic_overlay_content_type) =
        if let Some((f, d, ct)) = artist_pic_branded {
            (f, d, ct)
        } else {
            (
                pic_cropped_filename.clone(),
                pic_cropped_data.clone(),
                pic_cropped_content_type.clone(),
            )
        };

    let pic_overlay_key = storage::upload_file_to_pending(
        &state,
        &session_id,
        "pic_overlay",
        &pic_overlay_filename,
        pic_overlay_data,
        &pic_overlay_content_type,
    )
    .await?;

    // Store in pending_submissions table
    let expires_at = chrono::Utc::now() + chrono::Duration::hours(1);
    sqlx::query(
        r#"
        INSERT INTO pending_submissions (
            session_id, artist_name, pronouns, track1_name, track2_name, no_voice_message,
            instagram, soundcloud, bandcamp, spotify, other_social, upcoming_events, mentions,
            pic_key, pic_cropped_key, pic_overlay_key, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        "#,
    )
    .bind(&session_id)
    .bind(&artist_name)
    .bind(&pronouns)
    .bind(&track1_name)
    .bind(&track2_name)
    .bind(no_voice_message)
    .bind(&instagram)
    .bind(&soundcloud)
    .bind(&bandcamp)
    .bind(&spotify)
    .bind(&other_social)
    .bind(&upcoming_events)
    .bind(&mentions)
    .bind(&pic_key)
    .bind(&pic_cropped_key)
    .bind(&pic_overlay_key)
    .bind(expires_at.to_rfc3339())
    .execute(&state.db)
    .await?;

    tracing::info!("Chunked upload: init complete, session_id={}", session_id);

    Ok(Json(InitResponse {
        success: true,
        session_id,
        message: "Session initialized. Upload track files next.".to_string(),
    }))
}

// ─────────────────────────────────────────────────────────────────────────────
// Step 2: Upload a single file (track1, track2, or voice)
// ─────────────────────────────────────────────────────────────────────────────

pub async fn submit_file(
    State(state): State<Arc<AppState>>,
    Path(session_id): Path<String>,
    Query(query): Query<FileQuery>,
    mut multipart: Multipart,
) -> Result<Json<FileUploadResponse>> {
    let field_name = query.field.as_str();
    tracing::info!(
        "Chunked upload: file upload, session_id={}, field={}",
        session_id,
        field_name
    );

    // Verify session exists
    let row = sqlx::query("SELECT artist_name, track1_name, track2_name FROM pending_submissions WHERE session_id = ? AND expires_at > datetime('now')")
        .bind(&session_id)
        .fetch_optional(&state.db)
        .await?;

    let row = row.ok_or_else(|| {
        AppError::Validation("Session not found or expired. Please start over.".to_string())
    })?;

    let artist_name: String = sqlx::Row::get(&row, "artist_name");
    let track1_name: String = sqlx::Row::get(&row, "track1_name");
    let track2_name: String = sqlx::Row::get(&row, "track2_name");

    let max_size = state.config.max_file_size_bytes();

    let mut file_data: Option<(String, Vec<u8>, String)> = None;
    let mut peaks_data: Option<String> = None;

    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|e| AppError::Validation(format!("Failed to read form field: {}", e)))?
    {
        let name = field.name().unwrap_or("").to_string();
        if name == "file" || name == field_name {
            let filename = field.file_name().unwrap_or("file").to_string();
            let content_type = field
                .content_type()
                .unwrap_or("application/octet-stream")
                .to_string();
            let data = field
                .bytes()
                .await
                .map_err(|e| AppError::Validation(format!("Failed to read file: {}", e)))?;

            if data.len() as u64 > max_size {
                return Err(AppError::FileTooLarge(state.config.max_file_size_mb));
            }

            file_data = Some((filename, data.to_vec(), content_type));
        } else if name == "peaks" {
            // Waveform peaks JSON from frontend
            peaks_data = Some(
                field
                    .text()
                    .await
                    .map_err(|e| AppError::Validation(format!("Failed to read peaks: {}", e)))?,
            );
        }
    }

    let (filename, data, content_type) =
        file_data.ok_or_else(|| AppError::Validation("No file provided".to_string()))?;

    // Determine the storage key based on field type
    let (db_column, desired_name) = match field_name {
        "track1" => (
            "track1_key",
            format!("{} - {}", artist_name.trim(), track1_name.trim()),
        ),
        "track2" => (
            "track2_key",
            format!("{} - {}", artist_name.trim(), track2_name.trim()),
        ),
        "voice" => (
            "voice_key",
            format!("{} - voice-message", artist_name.trim()),
        ),
        _ => {
            return Err(AppError::Validation(format!(
                "Invalid field: {}. Must be track1, track2, or voice.",
                field_name
            )));
        }
    };

    let key = storage::upload_file_to_pending_named(
        &state,
        &session_id,
        field_name,
        &desired_name,
        &filename,
        data,
        &content_type,
    )
    .await?;

    // Store waveform peaks JSON alongside the audio file if provided
    if let Some(peaks_json) = peaks_data {
        let peaks_key = format!("{}.peaks.json", key.trim_end_matches(|c: char| c != '.').trim_end_matches('.'));
        storage::upload_file_to_pending(
            &state,
            &session_id,
            &format!("{}_peaks", field_name),
            &format!("{}.peaks.json", desired_name),
            peaks_json.into_bytes(),
            "application/json",
        )
        .await?;
        tracing::debug!("Uploaded peaks for {} at {}", field_name, peaks_key);
    }

    // Update pending_submissions with the file key
    let update_sql = format!(
        "UPDATE pending_submissions SET {} = ? WHERE session_id = ?",
        db_column
    );
    sqlx::query(&update_sql)
        .bind(&key)
        .bind(&session_id)
        .execute(&state.db)
        .await?;

    tracing::info!(
        "Chunked upload: file {} uploaded, session_id={}",
        field_name,
        session_id
    );

    Ok(Json(FileUploadResponse {
        success: true,
        field: field_name.to_string(),
        message: format!("{} uploaded successfully.", field_name),
    }))
}

// ─────────────────────────────────────────────────────────────────────────────
// Step 3: Finalize submission
// ─────────────────────────────────────────────────────────────────────────────

pub async fn submit_finalize(
    State(state): State<Arc<AppState>>,
    Path(session_id): Path<String>,
) -> Result<Json<FinalizeResponse>> {
    tracing::info!("Chunked upload: finalize, session_id={}", session_id);

    // Fetch the pending submission
    let row = sqlx::query(
        r#"
        SELECT artist_name, pronouns, track1_name, track2_name, no_voice_message,
               instagram, soundcloud, bandcamp, spotify, other_social, upcoming_events, mentions,
               pic_key, pic_cropped_key, pic_overlay_key, track1_key, track2_key, voice_key
        FROM pending_submissions
        WHERE session_id = ? AND expires_at > datetime('now')
        "#,
    )
    .bind(&session_id)
    .fetch_optional(&state.db)
    .await?;

    let row = row.ok_or_else(|| {
        AppError::Validation("Session not found or expired. Please start over.".to_string())
    })?;

    let artist_name: String = sqlx::Row::get(&row, "artist_name");
    let pronouns: String = sqlx::Row::get(&row, "pronouns");
    let track1_name: String = sqlx::Row::get(&row, "track1_name");
    let track2_name: String = sqlx::Row::get(&row, "track2_name");
    let no_voice_message: bool = sqlx::Row::get(&row, "no_voice_message");
    let instagram: Option<String> = sqlx::Row::get(&row, "instagram");
    let soundcloud: Option<String> = sqlx::Row::get(&row, "soundcloud");
    let bandcamp: Option<String> = sqlx::Row::get(&row, "bandcamp");
    let spotify: Option<String> = sqlx::Row::get(&row, "spotify");
    let other_social: Option<String> = sqlx::Row::get(&row, "other_social");
    let upcoming_events: Option<String> = sqlx::Row::get(&row, "upcoming_events");
    let mentions: Option<String> = sqlx::Row::get(&row, "mentions");
    let pic_key: Option<String> = sqlx::Row::get(&row, "pic_key");
    let pic_cropped_key: Option<String> = sqlx::Row::get(&row, "pic_cropped_key");
    let pic_overlay_key: Option<String> = sqlx::Row::get(&row, "pic_overlay_key");
    let track1_key: Option<String> = sqlx::Row::get(&row, "track1_key");
    let track2_key: Option<String> = sqlx::Row::get(&row, "track2_key");
    let voice_key: Option<String> = sqlx::Row::get(&row, "voice_key");

    // Validate required files
    if track1_key.is_none() {
        return Err(AppError::Validation(
            "Track 1 not uploaded. Please upload track1 before finalizing.".to_string(),
        ));
    }
    if track2_key.is_none() {
        return Err(AppError::Validation(
            "Track 2 not uploaded. Please upload track2 before finalizing.".to_string(),
        ));
    }
    if !no_voice_message && voice_key.is_none() {
        return Err(AppError::Validation(
            "Voice message not uploaded. Please upload voice or check 'no voice message'."
                .to_string(),
        ));
    }

    // Insert artist record
    let result = sqlx::query(
        r#"
        INSERT INTO artists (
            name, pronouns, track1_name, track2_name, no_voice_message,
            instagram, soundcloud, bandcamp, spotify, other_social,
            upcoming_events, mentions, status,
            pic_key, pic_cropped_key, pic_overlay_key, track1_key, track2_key, voice_message_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unassigned', ?, ?, ?, ?, ?, ?)
        "#,
    )
    .bind(&artist_name)
    .bind(&pronouns)
    .bind(&track1_name)
    .bind(&track2_name)
    .bind(no_voice_message)
    .bind(&instagram)
    .bind(&soundcloud)
    .bind(&bandcamp)
    .bind(&spotify)
    .bind(&other_social)
    .bind(&upcoming_events)
    .bind(&mentions)
    .bind(&pic_key)
    .bind(&pic_cropped_key)
    .bind(&pic_overlay_key)
    .bind(&track1_key)
    .bind(&track2_key)
    .bind(&voice_key)
    .execute(&state.db)
    .await?;

    let artist_id = result.last_insert_rowid();

    // Delete the pending submission
    sqlx::query("DELETE FROM pending_submissions WHERE session_id = ?")
        .bind(&session_id)
        .execute(&state.db)
        .await?;

    tracing::info!(
        "Chunked upload: finalized, artist_id={}, session_id={}",
        artist_id,
        session_id
    );

    Ok(Json(FinalizeResponse {
        success: true,
        message: "Thank you for your submission! We'll be in touch soon.".to_string(),
        artist_id: Some(artist_id),
    }))
}
