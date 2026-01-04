use crate::{AppError, AppState, Result};
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

pub async fn delete_file(state: &Arc<AppState>, key: &str) -> Result<()> {
    state
        .s3_client
        .delete_object()
        .bucket(&state.config.r2_bucket_name)
        .key(key)
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to delete file: {}", e)))?;

    Ok(())
}
