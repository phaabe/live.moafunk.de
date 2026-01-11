use crate::{auth, image_overlay, models, pdf, storage, AppError, AppState, Result};
use axum::{
    extract::{Path, State},
    http::{header, Request, StatusCode},
    response::{IntoResponse, Response},
};
use std::io::Write;
use std::sync::Arc;

/// Helper to check if user has admin access
fn require_admin(user: Option<&models::User>) -> Result<()> {
    match user {
        Some(u) if u.role_enum().can_access_admin() => Ok(()),
        _ => Err(AppError::Unauthorized("Unauthorized".to_string())),
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ShowPackage {
    Recording,
    SocialMedia,
    AllData,
}

impl ShowPackage {
    fn parse(s: &str) -> Option<Self> {
        match s.trim().to_ascii_lowercase().as_str() {
            "recording" | "rec" => Some(Self::Recording),
            "social-media" | "social" | "media" => Some(Self::SocialMedia),
            "all-data" | "all" | "all-material" | "material" => Some(Self::AllData),
            _ => None,
        }
    }

    fn filename_suffix(self) -> &'static str {
        match self {
            Self::Recording => "recording",
            Self::SocialMedia => "social-media",
            Self::AllData => "all-data",
        }
    }
}

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

async fn fetch_show_and_artists(
    state: &Arc<AppState>,
    show_id: i64,
) -> Result<(models::Show, Vec<models::Artist>)> {
    let show: Option<models::Show> = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(show_id)
        .fetch_optional(&state.db)
        .await?;

    let show = show.ok_or_else(|| AppError::NotFound("Show not found".to_string()))?;

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

    Ok((show, artists))
}

fn zip_filename(show: &models::Show, package: ShowPackage) -> String {
    let date_str = show.date.split('T').next().unwrap_or(&show.date);
    let show_title = sanitize_filename(&show.title);
    format!(
        "UNHEARD_{}_{}_{}.zip",
        date_str,
        show_title,
        package.filename_suffix()
    )
}

fn audio_filename(artist_name: &str, tag: &str, title: &str, ext: &str) -> String {
    let artist = sanitize_filename(artist_name);
    let title = sanitize_filename(title);
    let tag = sanitize_filename(tag);
    if title.trim().is_empty() {
        format!("{} - {}.{}", artist, tag, ext)
    } else {
        format!("{} - {} - {}.{}", artist, tag, title, ext)
    }
}

fn artist_zip_filename(artist: &models::Artist) -> String {
    let name = sanitize_filename(&artist.name);
    format!("UNHEARD_artist_{}_{}.zip", name, artist.id)
}

async fn fetch_artist_and_show(
    state: &Arc<AppState>,
    artist_id: i64,
) -> Result<(models::Artist, Option<models::Show>)> {
    let artist: Option<models::Artist> = sqlx::query_as("SELECT * FROM artists WHERE id = ?")
        .bind(artist_id)
        .fetch_optional(&state.db)
        .await?;
    let artist = artist.ok_or_else(|| AppError::NotFound("Artist not found".to_string()))?;

    let show: Option<models::Show> = sqlx::query_as(
        r#"
        SELECT s.* FROM shows s
        INNER JOIN artist_show_assignments asa ON asa.show_id = s.id
        WHERE asa.artist_id = ?
        LIMIT 1
        "#,
    )
    .bind(artist_id)
    .fetch_optional(&state.db)
    .await?;

    Ok((artist, show))
}

pub async fn download_artist(
    State(state): State<Arc<AppState>>,
    Path(artist_id): Path<i64>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = auth::get_session_from_cookies(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;
    require_admin(user.as_ref())?;

    let (artist, show) = fetch_artist_and_show(&state, artist_id).await?;
    let artist_dir = sanitize_filename(&artist.name);

    let mut zip_buffer = Vec::new();
    {
        let mut zip = zip::ZipWriter::new(std::io::Cursor::new(&mut zip_buffer));
        let options = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated);

        if let Some(key) = &artist.pic_key {
            if let Ok((data, _)) = storage::download_file(&state, key).await {
                let ext = std::path::Path::new(key)
                    .extension()
                    .and_then(|e| e.to_str())
                    .unwrap_or("jpg");
                let path = format!("{}/artist_pic_original.{}", artist_dir, ext);
                zip.start_file(&path, options)
                    .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                zip.write_all(&data)
                    .map_err(|e| AppError::Internal(format!("ZIP write error: {}", e)))?;
            }
        }

        if let Some(key) = &artist.pic_cropped_key {
            if let Ok((data, _)) = storage::download_file(&state, key).await {
                let ext = std::path::Path::new(key)
                    .extension()
                    .and_then(|e| e.to_str())
                    .unwrap_or("jpg");
                let path = format!("{}/artist_pic_cropped.{}", artist_dir, ext);
                zip.start_file(&path, options)
                    .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                zip.write_all(&data)
                    .map_err(|e| AppError::Internal(format!("ZIP write error: {}", e)))?;
            }
        }

        if let Some(key) = &artist.pic_overlay_key {
            if let Ok((data, _)) = storage::download_file(&state, key).await {
                let ext = std::path::Path::new(key)
                    .extension()
                    .and_then(|e| e.to_str())
                    .unwrap_or("png");
                let path = format!("{}/artist_pic_overlay.{}", artist_dir, ext);
                zip.start_file(&path, options)
                    .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                zip.write_all(&data)
                    .map_err(|e| AppError::Internal(format!("ZIP write error: {}", e)))?;
            }
        }

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

        // Include original voice message file if available
        if let Some(key) = &artist.voice_original_key {
            if let Ok((data, _)) = storage::download_file(&state, key).await {
                let ext = std::path::Path::new(key)
                    .extension()
                    .and_then(|e| e.to_str())
                    .unwrap_or("bin");
                let path = format!("{}/originals/voice_message_original.{}", artist_dir, ext);
                zip.start_file(&path, options)
                    .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                zip.write_all(&data)
                    .map_err(|e| AppError::Internal(format!("ZIP write error: {}", e)))?;
            }
        }

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

        // Include original track1 file if available
        if let Some(key) = &artist.track1_original_key {
            if let Ok((data, _)) = storage::download_file(&state, key).await {
                let ext = std::path::Path::new(key)
                    .extension()
                    .and_then(|e| e.to_str())
                    .unwrap_or("bin");
                let track_name = sanitize_filename(&artist.track1_name);
                let path = format!(
                    "{}/originals/track1_{}_original.{}",
                    artist_dir, track_name, ext
                );
                zip.start_file(&path, options)
                    .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                zip.write_all(&data)
                    .map_err(|e| AppError::Internal(format!("ZIP write error: {}", e)))?;
            }
        }

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

        // Include original track2 file if available
        if let Some(key) = &artist.track2_original_key {
            if let Ok((data, _)) = storage::download_file(&state, key).await {
                let ext = std::path::Path::new(key)
                    .extension()
                    .and_then(|e| e.to_str())
                    .unwrap_or("bin");
                let track_name = sanitize_filename(&artist.track2_name);
                let path = format!(
                    "{}/originals/track2_{}_original.{}",
                    artist_dir, track_name, ext
                );
                zip.start_file(&path, options)
                    .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                zip.write_all(&data)
                    .map_err(|e| AppError::Internal(format!("ZIP write error: {}", e)))?;
            }
        }

        let assigned_show_line = if let Some(s) = &show {
            format!("{} ({})", s.title, s.date)
        } else {
            "N/A".to_string()
        };
        let voice_line = if artist.no_voice_message {
            "Artist opted out".to_string()
        } else if artist.voice_message_key.is_some() {
            "Uploaded".to_string()
        } else {
            "Not uploaded".to_string()
        };

        let info_content = format!(
            r#"Artist: {}
Pronouns: {}
Status: {}
Assigned Show: {}

Track 1: {}
Track 2: {}
Voice Message: {}

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
            artist.status,
            assigned_show_line,
            artist.track1_name,
            artist.track2_name,
            voice_line,
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

        zip.finish()
            .map_err(|e| AppError::Internal(format!("ZIP finish error: {}", e)))?;
    }

    let filename = artist_zip_filename(&artist);
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

/// Download only audio files for an artist (voice message + tracks)
pub async fn download_artist_audio(
    State(state): State<Arc<AppState>>,
    Path(artist_id): Path<i64>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = auth::get_session_from_cookies(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;
    require_admin(user.as_ref())?;

    let (artist, _show) = fetch_artist_and_show(&state, artist_id).await?;
    let artist_dir = sanitize_filename(&artist.name);

    let mut zip_buffer = Vec::new();
    {
        let mut zip = zip::ZipWriter::new(std::io::Cursor::new(&mut zip_buffer));
        let options = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated);

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

        zip.finish()
            .map_err(|e| AppError::Internal(format!("ZIP finish error: {}", e)))?;
    }

    let name = sanitize_filename(&artist.name);
    let filename = format!("UNHEARD_artist_{}_audio.zip", name);
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

/// Download only image files for an artist (original, cropped, overlay)
pub async fn download_artist_images(
    State(state): State<Arc<AppState>>,
    Path(artist_id): Path<i64>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = auth::get_session_from_cookies(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;
    require_admin(user.as_ref())?;

    let (artist, _show) = fetch_artist_and_show(&state, artist_id).await?;
    let artist_dir = sanitize_filename(&artist.name);

    let mut zip_buffer = Vec::new();
    {
        let mut zip = zip::ZipWriter::new(std::io::Cursor::new(&mut zip_buffer));
        let options = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated);

        if let Some(key) = &artist.pic_key {
            if let Ok((data, _)) = storage::download_file(&state, key).await {
                let ext = std::path::Path::new(key)
                    .extension()
                    .and_then(|e| e.to_str())
                    .unwrap_or("jpg");
                let path = format!("{}/artist_pic_original.{}", artist_dir, ext);
                zip.start_file(&path, options)
                    .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                zip.write_all(&data)
                    .map_err(|e| AppError::Internal(format!("ZIP write error: {}", e)))?;
            }
        }

        if let Some(key) = &artist.pic_cropped_key {
            if let Ok((data, _)) = storage::download_file(&state, key).await {
                let ext = std::path::Path::new(key)
                    .extension()
                    .and_then(|e| e.to_str())
                    .unwrap_or("jpg");
                let path = format!("{}/artist_pic_cropped.{}", artist_dir, ext);
                zip.start_file(&path, options)
                    .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                zip.write_all(&data)
                    .map_err(|e| AppError::Internal(format!("ZIP write error: {}", e)))?;
            }
        }

        if let Some(key) = &artist.pic_overlay_key {
            if let Ok((data, _)) = storage::download_file(&state, key).await {
                let ext = std::path::Path::new(key)
                    .extension()
                    .and_then(|e| e.to_str())
                    .unwrap_or("png");
                let path = format!("{}/artist_pic_overlay.{}", artist_dir, ext);
                zip.start_file(&path, options)
                    .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                zip.write_all(&data)
                    .map_err(|e| AppError::Internal(format!("ZIP write error: {}", e)))?;
            }
        }

        zip.finish()
            .map_err(|e| AppError::Internal(format!("ZIP finish error: {}", e)))?;
    }

    let name = sanitize_filename(&artist.name);
    let filename = format!("UNHEARD_artist_{}_images.zip", name);
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

/// Download artist info as PDF
pub async fn download_artist_pdf(
    State(state): State<Arc<AppState>>,
    Path(artist_id): Path<i64>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = auth::get_session_from_cookies(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;
    require_admin(user.as_ref())?;

    let (artist, show) = fetch_artist_and_show(&state, artist_id).await?;

    let assigned_show_line = if let Some(s) = &show {
        format!("{} ({})", s.title, s.date)
    } else {
        "N/A".to_string()
    };

    let voice_line = if artist.no_voice_message {
        "Artist opted out".to_string()
    } else if artist.voice_message_key.is_some() {
        "Uploaded".to_string()
    } else {
        "Not uploaded".to_string()
    };

    let pdf_data = pdf::generate_artist_pdf(&artist, &assigned_show_line, &voice_line)?;

    let name = sanitize_filename(&artist.name);
    let filename = format!("UNHEARD_artist_{}_info.pdf", name);
    Ok((
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, "application/pdf".to_string()),
            (
                header::CONTENT_DISPOSITION,
                format!("attachment; filename=\"{}\"", filename),
            ),
        ],
        pdf_data,
    )
        .into_response())
}

pub async fn download_show_package(
    State(state): State<Arc<AppState>>,
    Path((show_id, package)): Path<(i64, String)>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = auth::get_session_from_cookies(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;
    require_admin(user.as_ref())?;

    let pkg = ShowPackage::parse(&package)
        .ok_or_else(|| AppError::Validation("Invalid download package".to_string()))?;

    download_show_impl(state, show_id, pkg).await
}

async fn download_show_impl(
    state: Arc<AppState>,
    show_id: i64,
    package: ShowPackage,
) -> Result<Response> {
    let (show, artists) = fetch_show_and_artists(&state, show_id).await?;

    let mut zip_buffer = Vec::new();
    {
        let mut zip = zip::ZipWriter::new(std::io::Cursor::new(&mut zip_buffer));
        let options = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated);

        match package {
            ShowPackage::Recording => {
                // PDF with all artist infos
                let pdf_bytes = pdf::build_recording_infos_pdf(&show, &artists);
                zip.start_file("artists.pdf", options)
                    .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                zip.write_all(&pdf_bytes)
                    .map_err(|e| AppError::Internal(format!("ZIP write error: {}", e)))?;

                // Audio files
                for artist in &artists {
                    if let Some(key) = &artist.voice_message_key {
                        if let Ok((data, _)) = storage::download_file(&state, key).await {
                            let ext = std::path::Path::new(key)
                                .extension()
                                .and_then(|e| e.to_str())
                                .unwrap_or("mp3");
                            let filename = audio_filename(&artist.name, "T0", "voicemail", ext);
                            let path = format!("audio/{}", filename);
                            zip.start_file(&path, options)
                                .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                            zip.write_all(&data).map_err(|e| {
                                AppError::Internal(format!("ZIP write error: {}", e))
                            })?;
                        }
                    }

                    if let Some(key) = &artist.track1_key {
                        if let Ok((data, _)) = storage::download_file(&state, key).await {
                            let ext = std::path::Path::new(key)
                                .extension()
                                .and_then(|e| e.to_str())
                                .unwrap_or("mp3");
                            let filename =
                                audio_filename(&artist.name, "T1", &artist.track1_name, ext);
                            let path = format!("audio/{}", filename);
                            zip.start_file(&path, options)
                                .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                            zip.write_all(&data).map_err(|e| {
                                AppError::Internal(format!("ZIP write error: {}", e))
                            })?;
                        }
                    }

                    if let Some(key) = &artist.track2_key {
                        if let Ok((data, _)) = storage::download_file(&state, key).await {
                            let ext = std::path::Path::new(key)
                                .extension()
                                .and_then(|e| e.to_str())
                                .unwrap_or("mp3");
                            let filename =
                                audio_filename(&artist.name, "T2", &artist.track2_name, ext);
                            let path = format!("audio/{}", filename);
                            zip.start_file(&path, options)
                                .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                            zip.write_all(&data).map_err(|e| {
                                AppError::Internal(format!("ZIP write error: {}", e))
                            })?;
                        }
                    }
                }
            }

            ShowPackage::SocialMedia => {
                // Overall cover (2x2 grid)
                let mut collage_items: Vec<(String, Vec<u8>, String)> = Vec::new();
                for artist in &artists {
                    if collage_items.len() >= 4 {
                        break;
                    }
                    let collage_key = artist
                        .pic_cropped_key
                        .as_ref()
                        .or(artist.pic_key.as_ref())
                        .or(artist.pic_overlay_key.as_ref());
                    if let Some(key) = collage_key {
                        if let Ok((data, _)) = storage::download_file(&state, key).await {
                            let ext = std::path::Path::new(key)
                                .extension()
                                .and_then(|e| e.to_str())
                                .unwrap_or("jpg")
                                .to_string();
                            collage_items.push((artist.name.clone(), data, ext));
                        }
                    }
                }

                if !collage_items.is_empty() {
                    if let Some(collage_png) =
                        image_overlay::build_show_collage(&state, collage_items).await
                    {
                        zip.start_file("cover_overall.png", options)
                            .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                        zip.write_all(&collage_png)
                            .map_err(|e| AppError::Internal(format!("ZIP write error: {}", e)))?;
                    }
                }

                // Per-artist cover (cropped image with overlay)
                for artist in &artists {
                    let Some(key) = &artist.pic_overlay_key else {
                        continue;
                    };
                    if let Ok((data, _)) = storage::download_file(&state, key).await {
                        let ext = std::path::Path::new(key)
                            .extension()
                            .and_then(|e| e.to_str())
                            .unwrap_or("png");
                        let filename =
                            format!("cover_artist/{}.{}", sanitize_filename(&artist.name), ext);
                        zip.start_file(&filename, options)
                            .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                        zip.write_all(&data)
                            .map_err(|e| AppError::Internal(format!("ZIP write error: {}", e)))?;
                    }
                }
            }

            ShowPackage::AllData => {
                // Everything regarding all artists.
                let mut collage_items: Vec<(String, Vec<u8>, String)> = Vec::new();

                for artist in &artists {
                    let artist_dir = sanitize_filename(&artist.name);

                    // Original / cropped / overlay pictures (if present)
                    if let Some(key) = &artist.pic_key {
                        if let Ok((data, _)) = storage::download_file(&state, key).await {
                            let ext = std::path::Path::new(key)
                                .extension()
                                .and_then(|e| e.to_str())
                                .unwrap_or("jpg");
                            let path = format!("{}/artist_pic_original.{}", artist_dir, ext);
                            zip.start_file(&path, options)
                                .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                            zip.write_all(&data).map_err(|e| {
                                AppError::Internal(format!("ZIP write error: {}", e))
                            })?;
                        }
                    }

                    if let Some(key) = &artist.pic_cropped_key {
                        if let Ok((data, _)) = storage::download_file(&state, key).await {
                            let ext = std::path::Path::new(key)
                                .extension()
                                .and_then(|e| e.to_str())
                                .unwrap_or("jpg");
                            let path = format!("{}/artist_pic_cropped.{}", artist_dir, ext);
                            zip.start_file(&path, options)
                                .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                            zip.write_all(&data).map_err(|e| {
                                AppError::Internal(format!("ZIP write error: {}", e))
                            })?;
                        }
                    }

                    if let Some(key) = &artist.pic_overlay_key {
                        if let Ok((data, _)) = storage::download_file(&state, key).await {
                            let ext = std::path::Path::new(key)
                                .extension()
                                .and_then(|e| e.to_str())
                                .unwrap_or("png");
                            let path = format!("{}/artist_pic_overlay.{}", artist_dir, ext);
                            zip.start_file(&path, options)
                                .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                            zip.write_all(&data).map_err(|e| {
                                AppError::Internal(format!("ZIP write error: {}", e))
                            })?;
                        }
                    }

                    // Build collage from cropped images.
                    if collage_items.len() < 4 {
                        let collage_key = artist
                            .pic_cropped_key
                            .as_ref()
                            .or(artist.pic_key.as_ref())
                            .or(artist.pic_overlay_key.as_ref());
                        if let Some(key) = collage_key {
                            if let Ok((data, _)) = storage::download_file(&state, key).await {
                                let ext = std::path::Path::new(key)
                                    .extension()
                                    .and_then(|e| e.to_str())
                                    .unwrap_or("jpg")
                                    .to_string();
                                collage_items.push((artist.name.clone(), data, ext));
                            }
                        }
                    }

                    // Voice message
                    if let Some(key) = &artist.voice_message_key {
                        if let Ok((data, _)) = storage::download_file(&state, key).await {
                            let ext = std::path::Path::new(key)
                                .extension()
                                .and_then(|e| e.to_str())
                                .unwrap_or("mp3");
                            let path = format!("{}/voice_message.{}", artist_dir, ext);
                            zip.start_file(&path, options)
                                .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                            zip.write_all(&data).map_err(|e| {
                                AppError::Internal(format!("ZIP write error: {}", e))
                            })?;
                        }
                    }

                    // Original voice message (for full artist package)
                    if let Some(key) = &artist.voice_original_key {
                        if let Ok((data, _)) = storage::download_file(&state, key).await {
                            let ext = std::path::Path::new(key)
                                .extension()
                                .and_then(|e| e.to_str())
                                .unwrap_or("bin");
                            let path =
                                format!("{}/originals/voice_message_original.{}", artist_dir, ext);
                            zip.start_file(&path, options)
                                .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                            zip.write_all(&data).map_err(|e| {
                                AppError::Internal(format!("ZIP write error: {}", e))
                            })?;
                        }
                    }

                    // Tracks
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
                            zip.write_all(&data).map_err(|e| {
                                AppError::Internal(format!("ZIP write error: {}", e))
                            })?;
                        }
                    }

                    // Original track1 (for full artist package)
                    if let Some(key) = &artist.track1_original_key {
                        if let Ok((data, _)) = storage::download_file(&state, key).await {
                            let ext = std::path::Path::new(key)
                                .extension()
                                .and_then(|e| e.to_str())
                                .unwrap_or("bin");
                            let track_name = sanitize_filename(&artist.track1_name);
                            let path = format!(
                                "{}/originals/track1_{}_original.{}",
                                artist_dir, track_name, ext
                            );
                            zip.start_file(&path, options)
                                .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                            zip.write_all(&data).map_err(|e| {
                                AppError::Internal(format!("ZIP write error: {}", e))
                            })?;
                        }
                    }

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
                            zip.write_all(&data).map_err(|e| {
                                AppError::Internal(format!("ZIP write error: {}", e))
                            })?;
                        }
                    }

                    // Original track2 (for full artist package)
                    if let Some(key) = &artist.track2_original_key {
                        if let Ok((data, _)) = storage::download_file(&state, key).await {
                            let ext = std::path::Path::new(key)
                                .extension()
                                .and_then(|e| e.to_str())
                                .unwrap_or("bin");
                            let track_name = sanitize_filename(&artist.track2_name);
                            let path = format!(
                                "{}/originals/track2_{}_original.{}",
                                artist_dir, track_name, ext
                            );
                            zip.start_file(&path, options)
                                .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                            zip.write_all(&data).map_err(|e| {
                                AppError::Internal(format!("ZIP write error: {}", e))
                            })?;
                        }
                    }

                    // info.txt
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

                if !collage_items.is_empty() {
                    if let Some(collage_png) =
                        image_overlay::build_show_collage(&state, collage_items).await
                    {
                        zip.start_file("cover.png", options)
                            .map_err(|e| AppError::Internal(format!("ZIP error: {}", e)))?;
                        zip.write_all(&collage_png)
                            .map_err(|e| AppError::Internal(format!("ZIP write error: {}", e)))?;
                    }
                }
            }
        }

        zip.finish()
            .map_err(|e| AppError::Internal(format!("ZIP finish error: {}", e)))?;
    }

    let filename = zip_filename(&show, package);
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

pub async fn download_show(
    State(state): State<Arc<AppState>>,
    Path(show_id): Path<i64>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = auth::get_session_from_cookies(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;
    require_admin(user.as_ref())?;

    // Legacy endpoint: default to all-data.
    download_show_impl(state, show_id, ShowPackage::AllData).await
}
