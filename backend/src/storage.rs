use crate::{AppError, AppState, Result};
use aws_sdk_s3::primitives::ByteStream;
use aws_sdk_s3::types::{Delete, ObjectIdentifier};
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

pub async fn delete_prefix(state: &Arc<AppState>, prefix: &str) -> Result<u64> {
    let mut deleted: u64 = 0;
    let mut continuation_token: Option<String> = None;

    loop {
        let mut req = state
            .s3_client
            .list_objects_v2()
            .bucket(&state.config.r2_bucket_name)
            .prefix(prefix.to_string());

        if let Some(token) = &continuation_token {
            req = req.continuation_token(token.to_string());
        }

        let resp = req
            .send()
            .await
            .map_err(|e| AppError::Storage(format!("Failed to list objects: {}", e)))?;

        let keys: Vec<String> = resp
            .contents()
            .iter()
            .filter_map(|o| o.key().map(|k| k.to_string()))
            .collect();

        if !keys.is_empty() {
            // Delete up to 1000 keys per request (S3 API limit)
            for chunk in keys.chunks(1000) {
                let mut objects: Vec<ObjectIdentifier> = Vec::with_capacity(chunk.len());
                for key in chunk {
                    let obj = ObjectIdentifier::builder().key(key).build().map_err(|e| {
                        AppError::Storage(format!("Failed to build object identifier: {}", e))
                    })?;
                    objects.push(obj);
                }

                let delete = Delete::builder()
                    .set_objects(Some(objects))
                    .quiet(true)
                    .build()
                    .map_err(|e| {
                        AppError::Storage(format!("Failed to build delete request: {}", e))
                    })?;

                let out = state
                    .s3_client
                    .delete_objects()
                    .bucket(&state.config.r2_bucket_name)
                    .delete(delete)
                    .send()
                    .await
                    .map_err(|e| AppError::Storage(format!("Failed to delete objects: {}", e)))?;

                deleted = deleted.saturating_add(out.deleted().len() as u64);

                let errors = out.errors();
                if !errors.is_empty() {
                    let msg = errors
                        .iter()
                        .map(|e| {
                            format!(
                                "{}: {}",
                                e.key().unwrap_or("(unknown)"),
                                e.message().unwrap_or("delete failed")
                            )
                        })
                        .collect::<Vec<_>>()
                        .join(", ");
                    return Err(AppError::Storage(format!(
                        "Failed to delete some objects: {}",
                        msg
                    )));
                }
            }
        }

        continuation_token = resp.next_continuation_token().map(|s| s.to_string());
        if continuation_token.is_none() {
            break;
        }
    }

    Ok(deleted)
}

// ─────────────────────────────────────────────────────────────────────────────
// Pending (chunked) upload helpers
// ─────────────────────────────────────────────────────────────────────────────

fn build_pending_key(session_id: &str, file_type: &str, object_name: &str, ext: &str) -> String {
    if ext.is_empty() {
        format!("pending/{}/{}/{}", session_id, file_type, object_name)
    } else {
        format!("pending/{}/{}/{}.{}", session_id, file_type, object_name, ext)
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

/// Upload a file to a pending location with a human-readable name.
pub async fn upload_file_to_pending_named(
    state: &Arc<AppState>,
    session_id: &str,
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

    let key = build_pending_key(session_id, file_type, &safe_name, &ext);

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
