use crate::{audio, AppError, AppState, Result};
use aws_sdk_s3::primitives::ByteStream;
use std::sync::Arc;
use uuid::Uuid;

fn sanitize_object_name(input: &str) -> String {
    let trimmed = input.trim();
    if trimmed.is_empty() {
        return String::new();
    }

    let mut out = String::with_capacity(trimmed.len());
    let mut last_was_sep = false;

    for ch in trimmed.chars() {
        let is_allowed = ch.is_ascii_alphanumeric() || ch == ' ' || ch == '-' || ch == '_';
        let mapped = if is_allowed { ch } else { '-' };

        let is_sep = mapped == ' ' || mapped == '-' || mapped == '_';
        if is_sep {
            if last_was_sep {
                continue;
            }
            last_was_sep = true;
            out.push(mapped);
        } else {
            last_was_sep = false;
            out.push(mapped);
        }
    }

    out.trim_matches([' ', '-', '_']).to_string()
}

fn extract_ext(filename: &str) -> String {
    std::path::Path::new(filename)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_string()
}

fn build_key_with_name(artist_id: i64, file_type: &str, object_name: &str, ext: &str) -> String {
    if ext.is_empty() {
        format!("artists/{}/{}/{}", artist_id, file_type, object_name)
    } else {
        format!(
            "artists/{}/{}/{}.{}",
            artist_id, file_type, object_name, ext
        )
    }
}

pub async fn upload_file_named(
    state: &Arc<AppState>,
    artist_id: i64,
    file_type: &str,
    desired_name: &str,
    original_filename: &str,
    data: Vec<u8>,
    content_type: &str,
) -> Result<String> {
    let ext = extract_ext(original_filename);
    let mut safe_name = sanitize_object_name(desired_name);
    if safe_name.len() > 120 {
        safe_name.truncate(120);
        safe_name = safe_name.trim_matches([' ', '-', '_']).to_string();
    }

    if safe_name.is_empty() {
        let unique_id = Uuid::new_v4().to_string()[..8].to_string();
        safe_name = unique_id;
    }

    let key = build_key_with_name(artist_id, file_type, &safe_name, &ext);

    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&key)
        .body(ByteStream::from(data))
        .content_type(content_type)
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload file: {}", e)))?;

    Ok(key)
}

pub async fn upload_file(
    state: &Arc<AppState>,
    artist_id: i64,
    file_type: &str,
    filename: &str,
    data: Vec<u8>,
    content_type: &str,
) -> Result<String> {
    let ext = extract_ext(filename);

    let unique_id = Uuid::new_v4().to_string()[..8].to_string();
    let key = build_key_with_name(artist_id, file_type, &unique_id, &ext);

    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&key)
        .body(ByteStream::from(data))
        .content_type(content_type)
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload file: {}", e)))?;

    Ok(key)
}

pub async fn download_file(state: &Arc<AppState>, key: &str) -> Result<(Vec<u8>, String)> {
    let response = state
        .s3_client
        .get_object()
        .bucket(&state.config.r2_bucket_name)
        .key(key)
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to download file: {}", e)))?;

    let content_type = response
        .content_type()
        .unwrap_or("application/octet-stream")
        .to_string();

    let data = response
        .body
        .collect()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to read file: {}", e)))?
        .into_bytes()
        .to_vec();

    Ok((data, content_type))
}

pub async fn get_presigned_url(
    state: &Arc<AppState>,
    key: &str,
    expires_in_secs: u64,
) -> Result<String> {
    let presigning_config = aws_sdk_s3::presigning::PresigningConfig::builder()
        .expires_in(std::time::Duration::from_secs(expires_in_secs))
        .build()
        .map_err(|e| AppError::Storage(format!("Failed to create presigning config: {}", e)))?;

    let presigned = state
        .s3_client
        .get_object()
        .bucket(&state.config.r2_bucket_name)
        .key(key)
        .presigned(presigning_config)
        .await
        .map_err(|e| AppError::Storage(format!("Failed to generate presigned URL: {}", e)))?;

    Ok(presigned.uri().to_string())
}

/// Copy a file from one S3 key to another and delete the source.
/// Returns the new key.
pub async fn move_file(state: &Arc<AppState>, source_key: &str, dest_key: &str) -> Result<String> {
    // Download the file
    let (data, content_type) = download_file(state, source_key).await?;

    // Upload to the new location
    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(dest_key)
        .body(ByteStream::from(data))
        .content_type(&content_type)
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to copy file: {}", e)))?;

    // Delete the original
    let _ = state
        .s3_client
        .delete_object()
        .bucket(&state.config.r2_bucket_name)
        .key(source_key)
        .send()
        .await;

    Ok(dest_key.to_string())
}

/// Move a file from pending location to final artist location.
/// Extracts the filename from the pending key and creates a new key under artists/{artist_id}/.
pub async fn move_pending_to_artist(
    state: &Arc<AppState>,
    pending_key: &str,
    artist_id: i64,
    file_type: &str,
) -> Result<String> {
    // Extract the filename part from the pending key
    // Format: pending/{session_id}/{type}/{filename}.{ext}
    let filename = pending_key.rsplit('/').next().unwrap_or("file");

    let ext = extract_ext(filename);
    let name_without_ext = if ext.is_empty() {
        filename.to_string()
    } else {
        filename.trim_end_matches(&format!(".{}", ext)).to_string()
    };

    let dest_key = build_key_with_name(artist_id, file_type, &name_without_ext, &ext);

    move_file(state, pending_key, &dest_key).await
}

// ─────────────────────────────────────────────────────────────────────────────
// Pending (chunked) upload helpers
// ─────────────────────────────────────────────────────────────────────────────

fn build_pending_key(session_id: &str, file_type: &str, object_name: &str, ext: &str) -> String {
    if ext.is_empty() {
        format!("pending/{}/{}/{}", session_id, file_type, object_name)
    } else {
        format!(
            "pending/{}/{}/{}.{}",
            session_id, file_type, object_name, ext
        )
    }
}

/// Upload a file to a pending (session-based) location.
pub async fn upload_file_to_pending(
    state: &Arc<AppState>,
    session_id: &str,
    file_type: &str,
    filename: &str,
    data: Vec<u8>,
    content_type: &str,
) -> Result<String> {
    let ext = extract_ext(filename);
    let unique_id = Uuid::new_v4().to_string()[..8].to_string();
    let key = build_pending_key(session_id, file_type, &unique_id, &ext);

    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&key)
        .body(ByteStream::from(data))
        .content_type(content_type)
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload file: {}", e)))?;

    Ok(key)
}

/// Upload a show cover image to S3
pub async fn upload_show_cover(
    state: &Arc<AppState>,
    show_id: i64,
    data: Vec<u8>,
) -> Result<String> {
    let key = format!("shows/{}/cover.png", show_id);

    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&key)
        .body(ByteStream::from(data))
        .content_type("image/png")
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload show cover: {}", e)))?;

    Ok(key)
}

/// Delete a show cover image from S3
pub async fn delete_show_cover(state: &Arc<AppState>, show_id: i64) -> Result<()> {
    let key = format!("shows/{}/cover.png", show_id);

    state
        .s3_client
        .delete_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&key)
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to delete show cover: {}", e)))?;

    Ok(())
}

/// Upload a show recording file to S3
/// Stored under recordings/DATETIME-SHOWNAME.ext
pub async fn upload_show_recording(
    state: &Arc<AppState>,
    date: &str,
    show_title: &str,
    original_filename: &str,
    data: Vec<u8>,
    content_type: &str,
) -> Result<String> {
    let ext = extract_ext(original_filename);
    let safe_title = sanitize_object_name(show_title);

    let key = if ext.is_empty() {
        format!("recordings/{}-{}", date, safe_title)
    } else {
        format!("recordings/{}-{}.{}", date, safe_title, ext)
    };

    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&key)
        .body(ByteStream::from(data))
        .content_type(content_type)
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload show recording: {}", e)))?;

    Ok(key)
}

// ─────────────────────────────────────────────────────────────────────────────
// Audio upload helpers (with mp3 conversion)
// ─────────────────────────────────────────────────────────────────────────────

/// Result of uploading an audio file with conversion.
/// Contains both the mp3 key (for regular use) and original key (for backup).
#[derive(Debug, Clone)]
pub struct AudioUploadResult {
    /// Key for the converted MP3 file (used for playback, wavesurfer, downloads)
    pub mp3_key: String,
    /// Key for the original uploaded file (backup, used only in full artist package)
    pub original_key: String,
}

/// Upload an audio file, converting it to MP3 if needed.
/// Stores both the original file and the converted MP3.
///
/// # Arguments
/// * `state` - App state with S3 client
/// * `artist_id` - The artist ID for storage path
/// * `file_type` - Type of file (track1, track2, voice)
/// * `desired_name` - Human-readable name for the file
/// * `original_filename` - Original filename with extension
/// * `data` - Raw audio file bytes
/// * `content_type` - MIME type of the original file
///
/// # Returns
/// * `AudioUploadResult` with both mp3_key and original_key
pub async fn upload_audio_with_conversion(
    state: &Arc<AppState>,
    artist_id: i64,
    file_type: &str,
    desired_name: &str,
    original_filename: &str,
    data: Vec<u8>,
    content_type: &str,
) -> Result<AudioUploadResult> {
    let original_ext = extract_ext(original_filename);
    let mut safe_name = sanitize_object_name(desired_name);
    if safe_name.len() > 120 {
        safe_name.truncate(120);
        safe_name = safe_name.trim_matches([' ', '-', '_']).to_string();
    }
    if safe_name.is_empty() {
        safe_name = Uuid::new_v4().to_string()[..8].to_string();
    }

    // Upload original file
    let original_key = build_key_with_name(
        artist_id,
        &format!("{}_original", file_type),
        &safe_name,
        &original_ext,
    );

    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&original_key)
        .body(ByteStream::from(data.clone()))
        .content_type(content_type)
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload original audio: {}", e)))?;

    // Convert to MP3 if not already mp3
    let mp3_data = if audio::is_mp3(original_filename) {
        data
    } else {
        audio::convert_to_mp3(&data, original_filename, &state.config).await?
    };

    // Upload MP3 file
    let mp3_key = build_key_with_name(artist_id, file_type, &safe_name, "mp3");

    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&mp3_key)
        .body(ByteStream::from(mp3_data))
        .content_type("audio/mpeg")
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload MP3 audio: {}", e)))?;

    tracing::info!(
        "Uploaded audio for artist {}: mp3={}, original={}",
        artist_id,
        mp3_key,
        original_key
    );

    Ok(AudioUploadResult {
        mp3_key,
        original_key,
    })
}

/// Result of starting an async audio upload with background conversion.
#[derive(Debug, Clone)]
pub struct AsyncAudioUploadResult {
    /// Key for the original uploaded file (immediately available)
    pub original_key: String,
    /// Key where the converted MP3 will be stored (may not exist yet if conversion is pending)
    pub mp3_key: String,
}

/// Upload the original audio file immediately and spawn background conversion.
/// Returns immediately after uploading the original file.
/// The MP3 conversion happens in the background and updates the database when complete.
pub async fn upload_audio_to_pending_async(
    state: &Arc<AppState>,
    session_id: &str,
    file_type: &str,
    desired_name: &str,
    original_filename: &str,
    data: Vec<u8>,
    content_type: &str,
) -> Result<AsyncAudioUploadResult> {
    let original_ext = extract_ext(original_filename);
    let mut safe_name = sanitize_object_name(desired_name);
    if safe_name.len() > 120 {
        safe_name.truncate(120);
        safe_name = safe_name.trim_matches([' ', '-', '_']).to_string();
    }
    if safe_name.is_empty() {
        safe_name = Uuid::new_v4().to_string()[..8].to_string();
    }

    // Upload original file immediately
    let original_key = build_pending_key(
        session_id,
        &format!("{}_original", file_type),
        &safe_name,
        &original_ext,
    );

    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&original_key)
        .body(ByteStream::from(data.clone()))
        .content_type(content_type)
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload original audio: {}", e)))?;

    // Prepare the MP3 key
    let mp3_key = build_pending_key(session_id, file_type, &safe_name, "mp3");

    // If already MP3, just copy the data directly (no conversion needed)
    if audio::is_mp3(original_filename) {
        state
            .s3_client
            .put_object()
            .bucket(&state.config.r2_bucket_name)
            .key(&mp3_key)
            .body(ByteStream::from(data))
            .content_type("audio/mpeg")
            .send()
            .await
            .map_err(|e| AppError::Storage(format!("Failed to upload MP3 audio: {}", e)))?;

        tracing::info!(
            "Uploaded pending audio (already MP3) for session {}: mp3={}, original={}",
            session_id,
            mp3_key,
            original_key
        );

        return Ok(AsyncAudioUploadResult {
            original_key,
            mp3_key,
        });
    }

    // Spawn background conversion task
    let state_clone = state.clone();
    let session_id_owned = session_id.to_string();
    let file_type_owned = file_type.to_string();
    let original_filename_owned = original_filename.to_string();
    let mp3_key_clone = mp3_key.clone();

    tokio::spawn(async move {
        tracing::info!(
            "Starting background audio conversion for session {}, field {}",
            session_id_owned,
            file_type_owned
        );

        let conversion_result =
            audio::convert_to_mp3(&data, &original_filename_owned, &state_clone.config).await;

        match conversion_result {
            Ok(mp3_data) => {
                // Upload the converted MP3
                let upload_result = state_clone
                    .s3_client
                    .put_object()
                    .bucket(&state_clone.config.r2_bucket_name)
                    .key(&mp3_key_clone)
                    .body(ByteStream::from(mp3_data))
                    .content_type("audio/mpeg")
                    .send()
                    .await;

                match upload_result {
                    Ok(_) => {
                        // Update database to mark conversion as completed
                        let status_column = format!("{}_conversion_status", file_type_owned);
                        let key_column = format!("{}_key", file_type_owned);
                        let update_sql = format!(
                            "UPDATE pending_submissions SET {} = 'completed', {} = ? WHERE session_id = ?",
                            status_column, key_column
                        );

                        if let Err(e) = sqlx::query(&update_sql)
                            .bind(&mp3_key_clone)
                            .bind(&session_id_owned)
                            .execute(&state_clone.db)
                            .await
                        {
                            tracing::error!(
                                "Failed to update conversion status for session {}: {}",
                                session_id_owned,
                                e
                            );
                        } else {
                            tracing::info!(
                                "Background conversion completed for session {}, field {}",
                                session_id_owned,
                                file_type_owned
                            );
                        }
                    }
                    Err(e) => {
                        tracing::error!(
                            "Failed to upload converted MP3 for session {}: {}",
                            session_id_owned,
                            e
                        );
                        // Mark as failed
                        let status_column = format!("{}_conversion_status", file_type_owned);
                        let update_sql = format!(
                            "UPDATE pending_submissions SET {} = 'failed' WHERE session_id = ?",
                            status_column
                        );
                        let _ = sqlx::query(&update_sql)
                            .bind(&session_id_owned)
                            .execute(&state_clone.db)
                            .await;
                    }
                }
            }
            Err(e) => {
                tracing::error!(
                    "Audio conversion failed for session {}, field {}: {}",
                    session_id_owned,
                    file_type_owned,
                    e
                );
                // Mark as failed
                let status_column = format!("{}_conversion_status", file_type_owned);
                let update_sql = format!(
                    "UPDATE pending_submissions SET {} = 'failed' WHERE session_id = ?",
                    status_column
                );
                let _ = sqlx::query(&update_sql)
                    .bind(&session_id_owned)
                    .execute(&state_clone.db)
                    .await;
            }
        }
    });

    tracing::info!(
        "Uploaded original and started background conversion for session {}: original={}, mp3={}",
        session_id,
        original_key,
        mp3_key
    );

    Ok(AsyncAudioUploadResult {
        original_key,
        mp3_key,
    })
}
