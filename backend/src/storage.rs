use crate::{AppError, AppState, Result};
use aws_sdk_s3::primitives::ByteStream;
use std::sync::Arc;
use uuid::Uuid;

pub async fn upload_file(
    state: &Arc<AppState>,
    artist_id: i64,
    file_type: &str,
    filename: &str,
    data: Vec<u8>,
    content_type: &str,
) -> Result<String> {
    let ext = std::path::Path::new(filename)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("");
    
    let unique_id = Uuid::new_v4().to_string()[..8].to_string();
    let key = format!("artists/{}/{}/{}.{}", artist_id, file_type, unique_id, ext);

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

pub async fn get_presigned_url(state: &Arc<AppState>, key: &str, expires_in_secs: u64) -> Result<String> {
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
