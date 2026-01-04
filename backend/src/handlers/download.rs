use crate::{auth, models, storage, AppError, AppState, Result};
use axum::{
    extract::{Path, State},
    http::{header, Request, StatusCode},
    response::{IntoResponse, Response},
};
use std::io::Write;
use std::sync::Arc;

fn sanitize_filename(name: &str) -> String {
    name.chars()
        .map(|c| match c {
            '<' | '>' | ':' | '"' | '/' | '\\' | '|' | '?' | '*' => '_',
            _ => c,
        })
        .collect::<String>()
        .trim()
        .to_string()
}

pub async fn download_show(
    State(state): State<Arc<AppState>>,
    Path(show_id): Path<i64>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = auth::get_session_from_cookies(&request);
    if !auth::is_authenticated(&state, token.as_deref()).await {
        return Err(AppError::Unauthorized);
    }

    // Get show
    let show: Option<models::Show> =
        sqlx::query_as("SELECT * FROM shows WHERE id = ?")
            .bind(show_id)
            .fetch_optional(&state.db)
            .await?;

    let show = show.ok_or_else(|| AppError::NotFound("Show not found".to_string()))?;

    // Get assigned artists
    let artists: Vec<models::Artist> = sqlx::query_as(
        r#"
        SELECT a.* FROM artists a
        INNER JOIN artist_show_assignments asa ON a.id = asa.artist_id
        WHERE asa.show_id = ?
        ORDER BY a.name
        "#,
    )
    .bind(show_id)
    .fetch_all(&state.db)
    .await?;

    if artists.is_empty() {
        return Err(AppError::Validation(
            "No artists assigned to this show".to_string(),
        ));
    }

    // Create ZIP in memory
    let mut zip_buffer = Vec::new();
    {
        let mut zip = zip::ZipWriter::new(std::io::Cursor::new(&mut zip_buffer));
        let options = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated);

        for artist in &artists {
            let artist_dir = sanitize_filename(&artist.name);

            // Download and add artist picture
            if let Some(key) = &artist.pic_key {
                if let Ok((data, _)) = storage::download_file(&state, key).await {
                    let ext = std::path::Path::new(key)
                        .extension()
                        .and_then(|e| e.to_str())
                        .unwrap_or("jpg");
                    let path = format!("{}/artist_pic.{}", artist_dir, ext);
                    zip.start_file(&path, options)
                        .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                    zip.write_all(&data)
                        .map_err(|e| AppError::Internal(format!("ZIP write error: {}", e)))?;
                }
            }

            // Download and add voice message
            if let Some(key) = &artist.voice_message_key {
                if let Ok((data, _)) = storage::download_file(&state, key).await {
                    let ext = std::path::Path::new(key)
                        .extension()
                        .and_then(|e| e.to_str())
                        .unwrap_or("mp3");
                    let path = format!("{}/voice_message.{}", artist_dir, ext);
                    zip.start_file(&path, options)
                        .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                    zip.write_all(&data)
                        .map_err(|e| AppError::Internal(format!("ZIP write error: {}", e)))?;
                }
            }

            // Download and add track 1
            if let Some(key) = &artist.track1_key {
                if let Ok((data, _)) = storage::download_file(&state, key).await {
                    let ext = std::path::Path::new(key)
                        .extension()
                        .and_then(|e| e.to_str())
                        .unwrap_or("mp3");
                    let track_name = sanitize_filename(&artist.track1_name);
                    let path = format!("{}/track1_{}.{}", artist_dir, track_name, ext);
                    zip.start_file(&path, options)
                        .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                    zip.write_all(&data)
                        .map_err(|e| AppError::Internal(format!("ZIP write error: {}", e)))?;
                }
            }

            // Download and add track 2
            if let Some(key) = &artist.track2_key {
                if let Ok((data, _)) = storage::download_file(&state, key).await {
                    let ext = std::path::Path::new(key)
                        .extension()
                        .and_then(|e| e.to_str())
                        .unwrap_or("mp3");
                    let track_name = sanitize_filename(&artist.track2_name);
                    let path = format!("{}/track2_{}.{}", artist_dir, track_name, ext);
                    zip.start_file(&path, options)
                        .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                    zip.write_all(&data)
                        .map_err(|e| AppError::Internal(format!("ZIP write error: {}", e)))?;
                }
            }

            // Create info.txt
            let info_content = format!(
                r#"Artist: {}
Pronouns: {}

Track 1: {}
Track 2: {}

Social Media:
- Instagram: {}
- SoundCloud: {}
- Bandcamp: {}
- Spotify: {}
- Other: {}

Upcoming Events:
{}

Things to Mention:
{}
"#,
                artist.name,
                artist.pronouns,
                artist.track1_name,
                artist.track2_name,
                artist.instagram.as_deref().unwrap_or("N/A"),
                artist.soundcloud.as_deref().unwrap_or("N/A"),
                artist.bandcamp.as_deref().unwrap_or("N/A"),
                artist.spotify.as_deref().unwrap_or("N/A"),
                artist.other_social.as_deref().unwrap_or("N/A"),
                artist.upcoming_events.as_deref().unwrap_or("N/A"),
                artist.mentions.as_deref().unwrap_or("N/A"),
            );

            let path = format!("{}/info.txt", artist_dir);
            zip.start_file(&path, options)
                .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
            zip.write_all(info_content.as_bytes())
                .map_err(|e| AppError::Internal(format!("ZIP write error: {}", e)))?;
        }

        // Create README.txt
        let readme = format!(
            r#"UNHEARD Show Package
====================

Show: {}
Generated: {}

Contents
--------
This package contains media files for all artists assigned to this show.

Each artist folder contains:
- artist_pic.* - Artist profile picture (square format)
- voice_message.* - Artist voice message (if provided)
- track1_*.* - First track
- track2_*.* - Second track
- info.txt - Artist information and social media links

Enjoy the show!
"#,
            show.title,
            chrono::Utc::now().format("%Y-%m-%d %H:%M:%S UTC"),
        );

        zip.start_file("README.txt", options)
            .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
        zip.write_all(readme.as_bytes())
            .map_err(|e| AppError::Internal(format!("ZIP write error: {}", e)))?;

        // Create playlist.m3u
        let mut playlist = String::from("#EXTM3U\n\n");
        for artist in &artists {
            let artist_dir = sanitize_filename(&artist.name);
            
            if let Some(key) = &artist.track1_key {
                let ext = std::path::Path::new(key)
                    .extension()
                    .and_then(|e| e.to_str())
                    .unwrap_or("mp3");
                let track_name = sanitize_filename(&artist.track1_name);
                playlist.push_str(&format!(
                    "#EXTINF:-1,{} - {}\n{}/track1_{}.{}\n\n",
                    artist.name, artist.track1_name, artist_dir, track_name, ext
                ));
            }
            
            if let Some(key) = &artist.track2_key {
                let ext = std::path::Path::new(key)
                    .extension()
                    .and_then(|e| e.to_str())
                    .unwrap_or("mp3");
                let track_name = sanitize_filename(&artist.track2_name);
                playlist.push_str(&format!(
                    "#EXTINF:-1,{} - {}\n{}/track2_{}.{}\n\n",
                    artist.name, artist.track2_name, artist_dir, track_name, ext
                ));
            }
        }

        zip.start_file("playlist.m3u", options)
            .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
        zip.write_all(playlist.as_bytes())
            .map_err(|e| AppError::Internal(format!("ZIP write error: {}", e)))?;

        zip.finish()
            .map_err(|e| AppError::Internal(format!("ZIP finish error: {}", e)))?;
    }

    // Generate filename
    let date_str = show.date.split('T').next().unwrap_or(&show.date);
    let show_title = sanitize_filename(&show.title);
    let filename = format!("UNHEARD_{}_{}.zip", date_str, show_title);

    Ok((
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, "application/zip".to_string()),
            (
                header::CONTENT_DISPOSITION,
                format!("attachment; filename=\"{}\"", filename),
            ),
        ],
        zip_buffer,
    )
        .into_response())
}
