use crate::{AppError, AppState, Result};
use axum::{
    extract::{Multipart, State},
    Json,
};
use std::sync::Arc;

use crate::models::SubmitResponse;
use crate::storage;

pub async fn submit_form(
    State(state): State<Arc<AppState>>,
    mut multipart: Multipart,
) -> Result<Json<SubmitResponse>> {
    tracing::info!("Received form submission");

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

    // File data
    let mut artist_pic: Option<(String, Vec<u8>, String)> = None;
    let mut voice_message: Option<(String, Vec<u8>, String)> = None;
    let mut track1_file: Option<(String, Vec<u8>, String)> = None;
    let mut track2_file: Option<(String, Vec<u8>, String)> = None;

    let max_size = state.config.max_file_size_bytes();

    while let Some(field) = multipart.next_field().await.map_err(|e| {
        tracing::error!("Failed to read multipart field: {}", e);
        AppError::Validation(format!("Failed to read form field: {}", e))
    })? {
        let name = field.name().unwrap_or("").to_string();
        tracing::debug!("Processing field: {}", name);

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

                artist_pic = Some((filename, data.to_vec(), content_type));
            }
            "voice-message" => {
                let filename = field.file_name().unwrap_or("").to_string();
                if !filename.is_empty() {
                    let content_type = field.content_type().unwrap_or("audio/mpeg").to_string();
                    let data = field.bytes().await.map_err(|e| {
                        AppError::Validation(format!("Failed to read voice message: {}", e))
                    })?;

                    if data.len() as u64 > max_size {
                        return Err(AppError::FileTooLarge(state.config.max_file_size_mb));
                    }

                    if !data.is_empty() {
                        voice_message = Some((filename, data.to_vec(), content_type));
                    }
                }
            }
            "track1-file" => {
                let filename = field.file_name().unwrap_or("track1.mp3").to_string();
                let content_type = field.content_type().unwrap_or("audio/mpeg").to_string();
                let data = field
                    .bytes()
                    .await
                    .map_err(|e| AppError::Validation(format!("Failed to read track1: {}", e)))?;

                if data.len() as u64 > max_size {
                    return Err(AppError::FileTooLarge(state.config.max_file_size_mb));
                }

                track1_file = Some((filename, data.to_vec(), content_type));
            }
            "track2-file" => {
                let filename = field.file_name().unwrap_or("track2.mp3").to_string();
                let content_type = field.content_type().unwrap_or("audio/mpeg").to_string();
                let data = field
                    .bytes()
                    .await
                    .map_err(|e| AppError::Validation(format!("Failed to read track2: {}", e)))?;

                if data.len() as u64 > max_size {
                    return Err(AppError::FileTooLarge(state.config.max_file_size_mb));
                }

                track2_file = Some((filename, data.to_vec(), content_type));
            }
            _ => {}
        }
    }

    // Validate required fields
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
    if artist_pic.is_none() {
        return Err(AppError::Validation(
            "Artist picture is required".to_string(),
        ));
    }
    if track1_file.is_none() {
        return Err(AppError::Validation("Track 1 file is required".to_string()));
    }
    if track2_file.is_none() {
        return Err(AppError::Validation("Track 2 file is required".to_string()));
    }

    // Insert artist record
    let result = sqlx::query(
        r#"
        INSERT INTO artists (
            name, pronouns, track1_name, track2_name, no_voice_message,
            instagram, soundcloud, bandcamp, spotify, other_social,
            upcoming_events, mentions, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
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
    .execute(&state.db)
    .await?;

    let artist_id = result.last_insert_rowid();

    // Upload files to R2
    let (pic_filename, pic_data, pic_content_type) = artist_pic.unwrap();
    let pic_key = storage::upload_file(
        &state,
        artist_id,
        "pic",
        &pic_filename,
        pic_data,
        &pic_content_type,
    )
    .await?;

    let (track1_filename, track1_data, track1_content_type) = track1_file.unwrap();
    let track1_key = storage::upload_file(
        &state,
        artist_id,
        "track1",
        &track1_filename,
        track1_data,
        &track1_content_type,
    )
    .await?;

    let (track2_filename, track2_data, track2_content_type) = track2_file.unwrap();
    let track2_key = storage::upload_file(
        &state,
        artist_id,
        "track2",
        &track2_filename,
        track2_data,
        &track2_content_type,
    )
    .await?;

    let voice_key = if let Some((filename, data, content_type)) = voice_message {
        Some(
            storage::upload_file(&state, artist_id, "voice", &filename, data, &content_type)
                .await?,
        )
    } else {
        None
    };

    // Update artist with file keys
    sqlx::query(
        r#"
        UPDATE artists SET
            pic_key = ?,
            track1_key = ?,
            track2_key = ?,
            voice_message_key = ?
        WHERE id = ?
        "#,
    )
    .bind(&pic_key)
    .bind(&track1_key)
    .bind(&track2_key)
    .bind(&voice_key)
    .bind(artist_id)
    .execute(&state.db)
    .await?;

    Ok(Json(SubmitResponse {
        success: true,
        message: "Thank you for your submission! We'll be in touch soon.".to_string(),
        artist_id: Some(artist_id),
    }))
}
