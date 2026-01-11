//! Chunked upload handlers for show recordings.
//!
//! Splits large recording uploads into multiple requests to stay under Cloudflare's 100MB limit:
//! 1. POST /api/shows/:id/upload-recording/init - Initialize upload, returns session_id
//! 2. POST /api/shows/:id/upload-recording/chunk/:session_id?index=N - Upload chunk N (0-indexed)
//! 3. POST /api/shows/:id/upload-recording/finalize/:session_id - Assemble chunks and save

use crate::{storage, AppError, AppState, Result};
use axum::{
    extract::{Multipart, Path, Query, State},
    http::HeaderMap,
    response::IntoResponse,
    Json,
};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use uuid::Uuid;

use super::api::require_admin;

// ─────────────────────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Deserialize)]
pub struct InitRequest {
    /// Original filename (for extension detection)
    pub filename: String,
    /// Total file size in bytes
    pub total_size: u64,
    /// Number of chunks to expect
    pub total_chunks: u32,
    /// Optional: waveform peaks JSON (can also be sent in finalize)
    pub peaks: Option<String>,
}

#[derive(Serialize)]
pub struct InitResponse {
    pub success: bool,
    pub session_id: String,
    pub message: String,
}

#[derive(Deserialize)]
pub struct ChunkQuery {
    /// 0-indexed chunk number
    pub index: u32,
}

#[derive(Serialize)]
pub struct ChunkResponse {
    pub success: bool,
    pub index: u32,
    pub received_bytes: usize,
    pub message: String,
}

#[derive(Serialize)]
pub struct FinalizeResponse {
    pub success: bool,
    pub key: String,
    pub recording_url: Option<String>,
    pub recording_peaks_url: Option<String>,
    pub message: String,
}

// ─────────────────────────────────────────────────────────────────────────────
// Step 1: Initialize recording upload
// ─────────────────────────────────────────────────────────────────────────────

pub async fn init_recording_upload(
    State(state): State<Arc<AppState>>,
    Path(show_id): Path<i64>,
    headers: HeaderMap,
    Json(req): Json<InitRequest>,
) -> Result<Json<InitResponse>> {
    require_admin(&state, &headers).await?;

    tracing::info!(
        "Chunked recording upload: init for show_id={}, filename={}, total_size={}, total_chunks={}",
        show_id,
        req.filename,
        req.total_size,
        req.total_chunks
    );

    // Verify show exists
    let show_exists: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM shows WHERE id = ?)")
        .bind(show_id)
        .fetch_one(&state.db)
        .await?;

    if !show_exists {
        return Err(AppError::NotFound("Show not found".to_string()));
    }

    // Generate session ID
    let session_id = Uuid::new_v4().to_string();
    let expires_at = chrono::Utc::now() + chrono::Duration::hours(2);

    // Store pending upload metadata
    sqlx::query(
        r#"
        INSERT INTO pending_recording_uploads (
            session_id, show_id, filename, total_size, total_chunks, peaks_json, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        "#,
    )
    .bind(&session_id)
    .bind(show_id)
    .bind(&req.filename)
    .bind(req.total_size as i64)
    .bind(req.total_chunks as i32)
    .bind(&req.peaks)
    .bind(expires_at.to_rfc3339())
    .execute(&state.db)
    .await?;

    Ok(Json(InitResponse {
        success: true,
        session_id,
        message: format!("Upload initialized. Send {} chunks next.", req.total_chunks),
    }))
}

// ─────────────────────────────────────────────────────────────────────────────
// Step 2: Upload a single chunk
// ─────────────────────────────────────────────────────────────────────────────

pub async fn upload_recording_chunk(
    State(state): State<Arc<AppState>>,
    Path((show_id, session_id)): Path<(i64, String)>,
    Query(query): Query<ChunkQuery>,
    headers: HeaderMap,
    mut multipart: Multipart,
) -> Result<Json<ChunkResponse>> {
    require_admin(&state, &headers).await?;

    let chunk_index = query.index;
    tracing::info!(
        "Chunked recording upload: chunk {} for session_id={}, show_id={}",
        chunk_index,
        session_id,
        show_id
    );

    // Verify session exists and matches show_id
    let row = sqlx::query(
        "SELECT show_id, total_chunks FROM pending_recording_uploads WHERE session_id = ? AND expires_at > datetime('now')",
    )
    .bind(&session_id)
    .fetch_optional(&state.db)
    .await?;

    let row = row.ok_or_else(|| {
        AppError::BadRequest("Upload session not found or expired. Please start over.".to_string())
    })?;

    let db_show_id: i64 = sqlx::Row::get(&row, "show_id");
    let total_chunks: i32 = sqlx::Row::get(&row, "total_chunks");

    if db_show_id != show_id {
        return Err(AppError::BadRequest(
            "Session does not match show ID".to_string(),
        ));
    }

    if chunk_index >= total_chunks as u32 {
        return Err(AppError::BadRequest(format!(
            "Chunk index {} exceeds total chunks {}",
            chunk_index, total_chunks
        )));
    }

    // Read chunk data from multipart
    let mut chunk_data: Option<Vec<u8>> = None;

    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|e| AppError::BadRequest(format!("Failed to read multipart: {}", e)))?
    {
        let name = field.name().unwrap_or("").to_string();
        if name == "chunk" || name == "file" {
            let data = field
                .bytes()
                .await
                .map_err(|e| AppError::BadRequest(format!("Failed to read chunk: {}", e)))?
                .to_vec();
            chunk_data = Some(data);
        }
    }

    let data =
        chunk_data.ok_or_else(|| AppError::BadRequest("No chunk data provided".to_string()))?;
    let received_bytes = data.len();

    // Store chunk in S3 with temporary key
    let chunk_key = format!("pending-recordings/{}/chunk-{:04}", session_id, chunk_index);

    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&chunk_key)
        .body(aws_sdk_s3::primitives::ByteStream::from(data))
        .content_type("application/octet-stream")
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload chunk: {}", e)))?;

    // Track which chunks have been received
    sqlx::query(
        "INSERT OR REPLACE INTO pending_recording_chunks (session_id, chunk_index, chunk_key, size_bytes) VALUES (?, ?, ?, ?)",
    )
    .bind(&session_id)
    .bind(chunk_index as i32)
    .bind(&chunk_key)
    .bind(received_bytes as i64)
    .execute(&state.db)
    .await?;

    tracing::info!(
        "Chunked recording upload: chunk {} stored, {} bytes",
        chunk_index,
        received_bytes
    );

    Ok(Json(ChunkResponse {
        success: true,
        index: chunk_index,
        received_bytes,
        message: format!("Chunk {} uploaded successfully.", chunk_index),
    }))
}

// ─────────────────────────────────────────────────────────────────────────────
// Step 3: Finalize - assemble chunks and save recording
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Deserialize, Default)]
pub struct FinalizeRequest {
    /// Optional: waveform peaks JSON (if not provided in init)
    pub peaks: Option<String>,
}

pub async fn finalize_recording_upload(
    State(state): State<Arc<AppState>>,
    Path((show_id, session_id)): Path<(i64, String)>,
    headers: HeaderMap,
    Json(req): Json<FinalizeRequest>,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    tracing::info!(
        "Chunked recording upload: finalize for session_id={}, show_id={}",
        session_id,
        show_id
    );

    // Fetch pending upload metadata
    let upload_row = sqlx::query(
        r#"
        SELECT show_id, filename, total_size, total_chunks, peaks_json
        FROM pending_recording_uploads
        WHERE session_id = ? AND expires_at > datetime('now')
        "#,
    )
    .bind(&session_id)
    .fetch_optional(&state.db)
    .await?;

    let upload_row = upload_row.ok_or_else(|| {
        AppError::BadRequest("Upload session not found or expired. Please start over.".to_string())
    })?;

    let db_show_id: i64 = sqlx::Row::get(&upload_row, "show_id");
    let filename: String = sqlx::Row::get(&upload_row, "filename");
    let total_chunks: i32 = sqlx::Row::get(&upload_row, "total_chunks");
    let peaks_json: Option<String> = sqlx::Row::get(&upload_row, "peaks_json");

    if db_show_id != show_id {
        return Err(AppError::BadRequest(
            "Session does not match show ID".to_string(),
        ));
    }

    // Verify all chunks are present
    let received_chunks: i32 =
        sqlx::query_scalar("SELECT COUNT(*) FROM pending_recording_chunks WHERE session_id = ?")
            .bind(&session_id)
            .fetch_one(&state.db)
            .await?;

    if received_chunks != total_chunks {
        return Err(AppError::BadRequest(format!(
            "Missing chunks: received {} of {} expected",
            received_chunks, total_chunks
        )));
    }

    // Fetch show details for the recording key
    let show: crate::models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(show_id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound("Show not found".to_string()))?;

    // Fetch all chunk keys in order
    let chunk_rows: Vec<(String, i64)> = sqlx::query_as(
        "SELECT chunk_key, size_bytes FROM pending_recording_chunks WHERE session_id = ? ORDER BY chunk_index ASC",
    )
    .bind(&session_id)
    .fetch_all(&state.db)
    .await?;

    // Assemble all chunks into a single buffer
    let mut assembled_data = Vec::new();
    for (chunk_key, _size) in &chunk_rows {
        let get_result = state
            .s3_client
            .get_object()
            .bucket(&state.config.r2_bucket_name)
            .key(chunk_key)
            .send()
            .await
            .map_err(|e| {
                AppError::Storage(format!("Failed to fetch chunk {}: {}", chunk_key, e))
            })?;

        let chunk_bytes = get_result
            .body
            .collect()
            .await
            .map_err(|e| AppError::Storage(format!("Failed to read chunk body: {}", e)))?
            .into_bytes();

        assembled_data.extend_from_slice(&chunk_bytes);
    }

    tracing::info!(
        "Assembled {} bytes from {} chunks",
        assembled_data.len(),
        chunk_rows.len()
    );

    // Determine content type from filename
    let content_type = match filename
        .rsplit('.')
        .next()
        .unwrap_or("")
        .to_lowercase()
        .as_str()
    {
        "mp3" => "audio/mpeg",
        "wav" => "audio/wav",
        "flac" => "audio/flac",
        "ogg" => "audio/ogg",
        "m4a" | "aac" => "audio/mp4",
        _ => "application/octet-stream",
    };

    // Upload the assembled recording
    let key = storage::upload_show_recording(
        &state,
        &show.date,
        &show.title,
        &filename,
        assembled_data,
        content_type,
    )
    .await?;

    // Store peaks JSON if provided (prefer finalize request, fallback to init)
    let final_peaks = req.peaks.or(peaks_json);
    let mut recording_peaks_url: Option<String> = None;

    if let Some(peaks_data) = final_peaks {
        let peaks_key = format!(
            "{}.peaks.json",
            key.rsplit_once('.').map(|(base, _)| base).unwrap_or(&key)
        );
        state
            .s3_client
            .put_object()
            .bucket(&state.config.r2_bucket_name)
            .key(&peaks_key)
            .body(aws_sdk_s3::primitives::ByteStream::from(
                peaks_data.into_bytes(),
            ))
            .content_type("application/json")
            .send()
            .await
            .map_err(|e| AppError::Storage(format!("Failed to upload peaks: {}", e)))?;

        recording_peaks_url = storage::get_presigned_url(&state, &peaks_key, 3600)
            .await
            .ok();
        tracing::debug!("Uploaded recording peaks at {}", peaks_key);
    }

    // Save recording_key and original filename in the database
    sqlx::query("UPDATE shows SET recording_key = ?, recording_filename = ? WHERE id = ?")
        .bind(&key)
        .bind(&filename)
        .bind(show_id)
        .execute(&state.db)
        .await?;

    // Clean up: delete chunks from S3 and database
    for (chunk_key, _) in &chunk_rows {
        let _ = state
            .s3_client
            .delete_object()
            .bucket(&state.config.r2_bucket_name)
            .key(chunk_key)
            .send()
            .await;
    }

    sqlx::query("DELETE FROM pending_recording_chunks WHERE session_id = ?")
        .bind(&session_id)
        .execute(&state.db)
        .await?;

    sqlx::query("DELETE FROM pending_recording_uploads WHERE session_id = ?")
        .bind(&session_id)
        .execute(&state.db)
        .await?;

    // Generate presigned URL for the uploaded file
    let recording_url = storage::get_presigned_url(&state, &key, 3600).await.ok();

    tracing::info!("Chunked recording upload: finalized, key={}", key);

    Ok(Json(FinalizeResponse {
        success: true,
        key,
        recording_url,
        recording_peaks_url,
        message: "Recording uploaded successfully.".to_string(),
    }))
}
