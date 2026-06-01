use crate::config::Config;
use crate::{audio, AppError, AppState, Result};
use aws_sdk_s3::primitives::ByteStream;
use serde::Serialize;
use std::sync::Arc;
use uuid::Uuid;

/// Build the S3 client for Cloudflare R2.
///
/// R2 requires path-style addressing and the `auto` region. Critically, it sets
/// checksum calculation/validation to **WhenRequired**: aws-sdk-s3 ≥1.7x
/// defaults to `WhenSupported`, which auto-injects a CRC32 checksum on every
/// request — and that has broken uploads against R2 in the field. With
/// `WhenRequired` the SDK only attaches a checksum when we explicitly ask for one
/// (we use CRC32C on the recording multipart upload, which R2 supports).
pub fn build_s3_client(config: &Config) -> aws_sdk_s3::Client {
    let s3_config = aws_sdk_s3::Config::builder()
        .endpoint_url(&config.r2_endpoint)
        .credentials_provider(aws_sdk_s3::config::Credentials::new(
            &config.r2_access_key_id,
            &config.r2_secret_access_key,
            None,
            None,
            "r2",
        ))
        .region(aws_sdk_s3::config::Region::new("auto"))
        .force_path_style(true)
        .request_checksum_calculation(aws_sdk_s3::config::RequestChecksumCalculation::WhenRequired)
        .response_checksum_validation(aws_sdk_s3::config::ResponseChecksumValidation::WhenRequired)
        .build();
    aws_sdk_s3::Client::from_conf(s3_config)
}

/// An object returned from listing objects in R2/S3.
#[derive(Debug, Clone, Serialize)]
pub struct StorageObject {
    pub key: String,
    pub last_modified: Option<String>,
    pub size: i64,
}

/// Minimum multipart part size (S3 requires ≥5 MiB except the last part).
const MULTIPART_PART_FLOOR: usize = 16 * 1024 * 1024;
/// S3 caps a multipart upload at 10,000 parts.
const MULTIPART_MAX_PARTS: usize = 10_000;

/// Upload `data` to `key` via S3 multipart with an explicit CRC32C checksum.
///
/// - Part size = `max(16 MiB, ceil(len / 10000))`, so any object fits in ≤10k parts.
/// - Each part and the final object carry a CRC32C checksum (R2-supported), set
///   explicitly so the SDK's default-checksum behavior is never relied upon.
/// - On any failure the multipart upload is aborted (and the bucket lifecycle
///   rule sweeps anything an abort misses). Uses a deterministic `key`, so a
///   retry after abort restarts cleanly within R2's 1-write/sec/key limit.
pub async fn upload_multipart(
    client: &aws_sdk_s3::Client,
    bucket: &str,
    key: &str,
    data: Vec<u8>,
    content_type: &str,
) -> Result<()> {
    use aws_sdk_s3::types::{ChecksumAlgorithm, CompletedMultipartUpload, CompletedPart};

    let total = data.len();
    let part_size = std::cmp::max(MULTIPART_PART_FLOOR, total.div_ceil(MULTIPART_MAX_PARTS));

    let create = client
        .create_multipart_upload()
        .bucket(bucket)
        .key(key)
        .content_type(content_type)
        .checksum_algorithm(ChecksumAlgorithm::Crc32C)
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("create_multipart_upload: {}", e)))?;

    let upload_id = create
        .upload_id()
        .ok_or_else(|| AppError::Storage("create_multipart_upload returned no upload_id".into()))?
        .to_string();

    // Upload parts; abort the whole upload if anything fails.
    let upload_result: Result<()> = async {
        let mut completed: Vec<CompletedPart> = Vec::new();
        let mut offset = 0usize;
        let mut part_number = 1i32;
        while offset < total {
            let end = std::cmp::min(offset + part_size, total);
            let chunk = data[offset..end].to_vec();

            let resp = client
                .upload_part()
                .bucket(bucket)
                .key(key)
                .upload_id(&upload_id)
                .part_number(part_number)
                .checksum_algorithm(ChecksumAlgorithm::Crc32C)
                .body(ByteStream::from(chunk))
                .send()
                .await
                .map_err(|e| AppError::Storage(format!("upload_part {}: {}", part_number, e)))?;

            let mut part = CompletedPart::builder()
                .part_number(part_number)
                .e_tag(resp.e_tag().unwrap_or_default());
            if let Some(c) = resp.checksum_crc32_c() {
                part = part.checksum_crc32_c(c);
            }
            completed.push(part.build());

            offset = end;
            part_number += 1;
        }

        client
            .complete_multipart_upload()
            .bucket(bucket)
            .key(key)
            .upload_id(&upload_id)
            .multipart_upload(
                CompletedMultipartUpload::builder()
                    .set_parts(Some(completed))
                    .build(),
            )
            .send()
            .await
            .map_err(|e| AppError::Storage(format!("complete_multipart_upload: {}", e)))?;
        Ok(())
    }
    .await;

    if let Err(e) = upload_result {
        tracing::warn!("Multipart upload to {} failed, aborting: {}", key, e);
        let _ = client
            .abort_multipart_upload()
            .bucket(bucket)
            .key(key)
            .upload_id(&upload_id)
            .send()
            .await;
        return Err(e);
    }

    tracing::info!(
        "Multipart-uploaded {} ({} bytes, {} part(s))",
        key,
        total,
        total.div_ceil(part_size)
    );
    Ok(())
}

/// HEAD an object and return its size in bytes. Used to verify an upload landed
/// intact before deleting the local copy.
pub async fn head_object_size(client: &aws_sdk_s3::Client, bucket: &str, key: &str) -> Result<u64> {
    let head = client
        .head_object()
        .bucket(bucket)
        .key(key)
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("head_object {}: {}", key, e)))?;
    Ok(head.content_length().unwrap_or(0).max(0) as u64)
}

/// Ensure the bucket has a lifecycle rule that auto-aborts incomplete multipart
/// uploads after 7 days (R2's default behavior when configured). Best-effort:
/// logs and continues on failure so startup never blocks on it.
pub async fn ensure_multipart_abort_lifecycle(client: &aws_sdk_s3::Client, bucket: &str) {
    use aws_sdk_s3::types::{
        AbortIncompleteMultipartUpload, BucketLifecycleConfiguration, ExpirationStatus,
        LifecycleRule, LifecycleRuleFilter,
    };

    let rule = match LifecycleRule::builder()
        .id("abort-incomplete-multipart-uploads")
        .status(ExpirationStatus::Enabled)
        .filter(LifecycleRuleFilter::builder().prefix("").build())
        .abort_incomplete_multipart_upload(
            AbortIncompleteMultipartUpload::builder()
                .days_after_initiation(7)
                .build(),
        )
        .build()
    {
        Ok(r) => r,
        Err(e) => {
            tracing::warn!("Could not build multipart-abort lifecycle rule: {}", e);
            return;
        }
    };

    let config = match BucketLifecycleConfiguration::builder().rules(rule).build() {
        Ok(c) => c,
        Err(e) => {
            tracing::warn!("Could not build lifecycle configuration: {}", e);
            return;
        }
    };

    match client
        .put_bucket_lifecycle_configuration()
        .bucket(bucket)
        .lifecycle_configuration(config)
        .send()
        .await
    {
        Ok(_) => tracing::info!(
            "Ensured multipart-abort lifecycle rule on bucket {}",
            bucket
        ),
        Err(e) => tracing::warn!(
            "Could not set multipart-abort lifecycle rule (continuing): {}",
            e
        ),
    }
}

/// Remove entries in `dir` whose modification time is older than `max_age`.
///
/// Handles both stale recording files (`recording_*.webm`) and orphaned segment
/// directories (`*.segs/` left by a crashed recorder). Returns the count removed.
/// Best-effort: a missing dir is a no-op, and per-entry errors are logged, not
/// propagated, so a scheduled sweep never aborts mid-way.
pub async fn cleanup_stale_files(
    dir: &std::path::Path,
    max_age: std::time::Duration,
) -> std::io::Result<usize> {
    let mut entries = match tokio::fs::read_dir(dir).await {
        Ok(e) => e,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(0),
        Err(e) => return Err(e),
    };

    let now = std::time::SystemTime::now();
    let mut removed = 0usize;
    while let Some(entry) = entries.next_entry().await? {
        let meta = match entry.metadata().await {
            Ok(m) => m,
            Err(_) => continue,
        };
        let modified = match meta.modified() {
            Ok(m) => m,
            Err(_) => continue,
        };
        let age = now
            .duration_since(modified)
            .unwrap_or(std::time::Duration::ZERO);
        if age < max_age {
            continue;
        }

        let path = entry.path();
        let result = if meta.is_dir() {
            tokio::fs::remove_dir_all(&path).await
        } else {
            tokio::fs::remove_file(&path).await
        };
        match result {
            Ok(()) => {
                removed += 1;
                tracing::info!("Cleaned up stale temp entry {:?}", path);
            }
            Err(e) => tracing::warn!("Failed to remove stale temp entry {:?}: {}", path, e),
        }
    }
    Ok(removed)
}

/// List all objects in the bucket matching a given key prefix.
pub async fn list_objects(state: &Arc<AppState>, prefix: &str) -> Result<Vec<StorageObject>> {
    let mut objects = Vec::new();
    let mut continuation_token: Option<String> = None;

    loop {
        let mut req = state
            .s3_client
            .list_objects_v2()
            .bucket(&state.config.r2_bucket_name)
            .prefix(prefix);

        if let Some(token) = &continuation_token {
            req = req.continuation_token(token);
        }

        let output = req
            .send()
            .await
            .map_err(|e| AppError::Storage(format!("Failed to list objects: {}", e)))?;

        for obj in output.contents() {
            let key = obj.key().unwrap_or_default().to_string();
            let last_modified =
                obj.last_modified()
                    .and_then(|t: &aws_sdk_s3::primitives::DateTime| {
                        t.fmt(aws_sdk_s3::primitives::DateTimeFormat::DateTime).ok()
                    });
            let size = obj.size().unwrap_or(0);
            objects.push(StorageObject {
                key,
                last_modified,
                size,
            });
        }

        if output.is_truncated() == Some(true) {
            continuation_token = output.next_continuation_token().map(|s| s.to_string());
        } else {
            break;
        }
    }

    Ok(objects)
}

pub fn sanitize_object_name(input: &str) -> String {
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

/// Upload a show-template cover image to S3
pub async fn upload_template_cover(
    state: &Arc<AppState>,
    template_id: i64,
    data: Vec<u8>,
) -> Result<String> {
    let key = format!("templates/{}/cover.png", template_id);

    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&key)
        .body(ByteStream::from(data))
        .content_type("image/png")
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload template cover: {}", e)))?;

    Ok(key)
}

/// Upload a plain show collage (no text/branding) to S3
pub async fn upload_show_collage(
    state: &Arc<AppState>,
    show_id: i64,
    data: Vec<u8>,
) -> Result<String> {
    let key = format!("shows/{}/collage.png", show_id);

    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&key)
        .body(ByteStream::from(data))
        .content_type("image/png")
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload show collage: {}", e)))?;

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
/// Stored under recordings/original-filename with sanitization
pub async fn upload_show_recording(
    state: &Arc<AppState>,
    date: &str,
    show_title: &str,
    original_filename: &str,
    data: Vec<u8>,
    content_type: &str,
) -> Result<String> {
    // Sanitize the original filename while preserving extension
    let safe_filename = sanitize_object_name(original_filename);
    let safe_filename = if safe_filename.is_empty() {
        // Fallback to date-title if filename sanitization results in empty string
        let ext = extract_ext(original_filename);
        let safe_title = sanitize_object_name(show_title);
        if ext.is_empty() {
            format!("{}-{}", date, safe_title)
        } else {
            format!("{}-{}.{}", date, safe_title, ext)
        }
    } else {
        safe_filename
    };

    let key = format!("recordings/{}", safe_filename);

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

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn cleanup_removes_old_keeps_fresh_and_tolerates_missing_dir() {
        use std::time::Duration;

        // Missing directory → no-op, not an error.
        let missing = std::path::Path::new("/tmp/does-not-exist-xyz-170");
        assert_eq!(
            cleanup_stale_files(missing, Duration::ZERO).await.unwrap(),
            0
        );

        let dir = tempfile::TempDir::new().unwrap();
        // A stale file and a stale segment directory.
        tokio::fs::write(dir.path().join("recording_1.webm"), b"x")
            .await
            .unwrap();
        tokio::fs::create_dir(dir.path().join("recording_1.segs"))
            .await
            .unwrap();
        tokio::fs::write(dir.path().join("recording_1.segs/seg_00000.ts"), b"y")
            .await
            .unwrap();

        // max_age 1h: nothing is that old yet → kept.
        assert_eq!(
            cleanup_stale_files(dir.path(), Duration::from_secs(3600))
                .await
                .unwrap(),
            0
        );
        assert!(dir.path().join("recording_1.webm").exists());

        // max_age 0: everything qualifies → file + dir removed (2 entries).
        assert_eq!(
            cleanup_stale_files(dir.path(), Duration::ZERO)
                .await
                .unwrap(),
            2
        );
        assert!(!dir.path().join("recording_1.webm").exists());
        assert!(!dir.path().join("recording_1.segs").exists());
    }

    /// Integration test against a **real R2 bucket**. Validates the checksum
    /// landmine fix end-to-end: a multi-part upload with explicit CRC32C must
    /// succeed, HEAD must report the exact size, and the round-tripped bytes must
    /// match (proving no checksum-related corruption/rejection).
    ///
    /// Ignored by default (hits the network). Run with R2 creds available:
    ///   cargo test --bin unheard-backend -- --ignored r2_multipart_roundtrip
    /// Skips gracefully if the R2 environment is not configured.
    #[tokio::test]
    #[ignore = "requires real R2 credentials"]
    async fn r2_multipart_roundtrip() {
        dotenvy::dotenv().ok();
        let config = match Config::from_env() {
            Ok(c) => c,
            Err(e) => {
                eprintln!("Skipping R2 integration test — config not available: {e}");
                return;
            }
        };
        let client = build_s3_client(&config);
        let bucket = config.r2_bucket_name.clone();

        // ~18 MiB forces a multi-part upload (16 MiB floor → 2 parts).
        let size = 18 * 1024 * 1024;
        let data: Vec<u8> = (0..size).map(|i| (i % 251) as u8).collect();
        let key = format!("recordings/_inttest/{}/raw.bin", Uuid::new_v4());

        upload_multipart(
            &client,
            &bucket,
            &key,
            data.clone(),
            "application/octet-stream",
        )
        .await
        .expect("multipart upload to R2 failed");

        let remote_size = head_object_size(&client, &bucket, &key)
            .await
            .expect("head_object failed");
        assert_eq!(remote_size, data.len() as u64, "remote size mismatch");

        // Round-trip the bytes to prove integrity.
        let resp = client
            .get_object()
            .bucket(&bucket)
            .key(&key)
            .send()
            .await
            .expect("get_object failed");
        let got = resp.body.collect().await.expect("read body").into_bytes();
        assert_eq!(got.len(), data.len(), "downloaded length mismatch");
        assert!(got[..] == data[..], "downloaded bytes differ from uploaded");

        // Cleanup.
        let _ = client
            .delete_object()
            .bucket(&bucket)
            .key(&key)
            .send()
            .await;
    }
}
