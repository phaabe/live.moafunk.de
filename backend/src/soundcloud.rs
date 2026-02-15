//! SoundCloud API client for uploading show recordings
//!
//! This module handles:
//! - OAuth2 client credentials token exchange
//! - Uploading tracks with artwork (as private)
//! - Toggling track privacy (private/public)
//! - Building track descriptions from AI bio + artist socials
//!
//! Reference: https://developers.soundcloud.com/docs/api/guide

use crate::{models, storage, AppError, AppState, Result};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

const SOUNDCLOUD_OAUTH_URL: &str = "https://secure.soundcloud.com/oauth/token";
const SOUNDCLOUD_API_BASE: &str = "https://api.soundcloud.com";

// ============================================================================
// API Response Types
// ============================================================================

#[derive(Debug, Deserialize)]
struct OAuthTokenResponse {
    access_token: String,
    #[allow(dead_code)]
    expires_in: Option<u64>,
    #[allow(dead_code)]
    token_type: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct SoundCloudTrack {
    pub id: i64,
    pub permalink_url: Option<String>,
    pub title: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct SoundCloudUploadResult {
    pub success: bool,
    pub track_id: Option<String>,
    pub track_url: Option<String>,
    pub error: Option<String>,
}

// ============================================================================
// OAuth Token
// ============================================================================

/// Get a SoundCloud access token.
/// Priority: 1) static config token, 2) DB-stored OAuth token, 3) error.
/// The client_credentials grant does NOT grant upload permissions —
/// use the authorization_code flow via /api/soundcloud/auth.
pub async fn get_access_token(state: &Arc<AppState>) -> Result<String> {
    // 1. Prefer static access token from config
    if let Some(ref token) = state.config.soundcloud_access_token {
        tracing::debug!("Using static SoundCloud access token from config");
        return Ok(token.clone());
    }

    // 2. Check DB-stored OAuth token (from authorization_code flow)
    if let Some(token) = get_stored_token(state).await? {
        tracing::debug!("Using DB-stored SoundCloud OAuth token");
        return Ok(token);
    }

    // 3. No valid token available
    Err(AppError::BadRequest(
        "SoundCloud not authorized. Visit /api/soundcloud/auth to connect your account."
            .to_string(),
    ))
}

/// Retrieve the stored SoundCloud access token from the app_settings table.
async fn get_stored_token(state: &Arc<AppState>) -> Result<Option<String>> {
    let row: Option<(String,)> =
        sqlx::query_as("SELECT value FROM app_settings WHERE key = 'soundcloud_access_token'")
            .fetch_optional(&state.db)
            .await?;
    Ok(row.map(|(v,)| v))
}

/// Store a SoundCloud access token in the app_settings table.
pub async fn store_token(state: &Arc<AppState>, token: &str) -> Result<()> {
    sqlx::query(
        "INSERT INTO app_settings (key, value, updated_at) VALUES ('soundcloud_access_token', ?, datetime('now'))
         ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
    )
    .bind(token)
    .execute(&state.db)
    .await?;
    tracing::info!("Stored SoundCloud access token in database");
    Ok(())
}

/// Delete the stored SoundCloud access token from the database.
/// This forces re-authorization on the next upload attempt.
pub async fn delete_stored_token(state: &Arc<AppState>) -> Result<()> {
    sqlx::query("DELETE FROM app_settings WHERE key = 'soundcloud_access_token'")
        .execute(&state.db)
        .await?;
    tracing::info!("Deleted stored SoundCloud access token from database");
    Ok(())
}

/// Check if SoundCloud integration is configured (has client_id for auth flow, or a static token)
pub fn is_configured(state: &Arc<AppState>) -> bool {
    state.config.soundcloud_access_token.is_some() || state.config.soundcloud_client_id.is_some()
}

/// Check if SoundCloud has a valid access token (static, or stored in DB).
/// This is an async check unlike is_configured().
pub async fn has_token(state: &Arc<AppState>) -> bool {
    if state.config.soundcloud_access_token.is_some() {
        return true;
    }
    matches!(get_stored_token(state).await, Ok(Some(_)))
}

// ============================================================================
// Description Builder
// ============================================================================

/// Build a SoundCloud track description from the show's AI bio and assigned artists' social links.
pub async fn build_description(state: &Arc<AppState>, show_id: i64) -> Result<String> {
    // Fetch show for AI bio
    let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(show_id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound("Show not found".to_string()))?;

    // Fetch assigned artists
    let artists: Vec<models::Artist> = sqlx::query_as(
        r#"
        SELECT a.* FROM artists a
        INNER JOIN artist_show_assignments asa ON a.id = asa.artist_id
        WHERE asa.show_id = ?
        ORDER BY a.name COLLATE NOCASE
        "#,
    )
    .bind(show_id)
    .fetch_all(&state.db)
    .await?;

    let mut parts: Vec<String> = Vec::new();

    // Add AI-generated show bio
    if let Some(ref bio) = show.ai_bio {
        parts.push(bio.clone());
    }

    // Add artist social links
    if !artists.is_empty() {
        parts.push(String::new()); // blank line separator

        for artist in &artists {
            let mut socials: Vec<String> = Vec::new();

            if let Some(ref sc) = artist.soundcloud {
                if !sc.is_empty() {
                    // Normalize to full URL if just a handle
                    let sc_link = if sc.starts_with("http") {
                        sc.clone()
                    } else {
                        format!("https://soundcloud.com/{}", sc.trim_start_matches('@'))
                    };
                    socials.push(format!("SoundCloud: {}", sc_link));
                }
            }

            if let Some(ref ig) = artist.instagram {
                if !ig.is_empty() {
                    let ig_handle = ig.trim_start_matches('@');
                    socials.push(format!("Instagram: https://instagram.com/{}", ig_handle));
                }
            }

            if !socials.is_empty() {
                parts.push(format!("{}\n{}", artist.name, socials.join("\n")));
            }
        }
    }

    Ok(parts.join("\n\n"))
}

/// Build a track title from show title and assigned artist names.
/// Format: "Show Title — Artist A, Artist B"
pub async fn build_title(state: &Arc<AppState>, show: &models::Show) -> Result<String> {
    let artists: Vec<(String,)> = sqlx::query_as(
        r#"
        SELECT a.name FROM artists a
        INNER JOIN artist_show_assignments asa ON a.id = asa.artist_id
        WHERE asa.show_id = ?
        ORDER BY a.name COLLATE NOCASE
        "#,
    )
    .bind(show.id)
    .fetch_all(&state.db)
    .await?;

    if artists.is_empty() {
        Ok(show.title.clone())
    } else {
        let names: Vec<&str> = artists.iter().map(|(n,)| n.as_str()).collect();
        Ok(format!("{} — {}", show.title, names.join(", ")))
    }
}

// ============================================================================
// Upload Track
// ============================================================================

/// Upload a show's final recording to SoundCloud as a private track.
/// Sets title, description, artwork from the show's cover image.
/// Stores the resulting track_id and URL in the database.
pub async fn upload_track(state: &Arc<AppState>, show_id: i64) -> Result<SoundCloudUploadResult> {
    let access_token = get_access_token(state).await?;

    // Fetch show
    let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(show_id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound("Show not found".to_string()))?;

    // Ensure recording exists
    let recording_key = show
        .recording_key
        .as_ref()
        .ok_or_else(|| AppError::BadRequest("Show has no recording to upload".to_string()))?;

    tracing::info!(
        show_id = show_id,
        recording_key = recording_key.as_str(),
        "Starting SoundCloud upload"
    );

    // Build title and description
    let title = build_title(state, &show).await?;
    let description = build_description(state, show_id).await?;

    tracing::debug!(
        show_id = show_id,
        title = title.as_str(),
        "SoundCloud track title built"
    );

    // Download recording from R2
    let (recording_bytes, recording_content_type) =
        storage::download_file(state, recording_key).await?;
    tracing::info!(
        show_id = show_id,
        size_mb = recording_bytes.len() as f64 / 1_048_576.0,
        content_type = recording_content_type.as_str(),
        "Downloaded recording from R2"
    );

    // Determine filename for the upload
    let filename = show
        .recording_filename
        .as_deref()
        .unwrap_or("recording.mp3");

    // Build multipart form
    let recording_part = reqwest::multipart::Part::bytes(recording_bytes)
        .file_name(filename.to_string())
        .mime_str(&recording_content_type)
        .map_err(|e| AppError::Internal(format!("Failed to set MIME type: {}", e)))?;

    let mut form = reqwest::multipart::Form::new()
        .text("track[title]", title.clone())
        .text("track[description]", description)
        .text("track[sharing]", "private".to_string())
        .text("track[tag_list]", "moafunk radio live".to_string())
        .part("track[asset_data]", recording_part);

    // Try to attach cover artwork
    let cover_key = format!("shows/{}/cover.png", show_id);
    match storage::download_file(state, &cover_key).await {
        Ok((cover_bytes, _)) => {
            let artwork_part = reqwest::multipart::Part::bytes(cover_bytes)
                .file_name("cover.png".to_string())
                .mime_str("image/png")
                .map_err(|e| {
                    AppError::Internal(format!("Failed to set artwork MIME type: {}", e))
                })?;
            form = form.part("track[artwork_data]", artwork_part);
            tracing::debug!(show_id = show_id, "Attached cover artwork");
        }
        Err(e) => {
            tracing::warn!(
                show_id = show_id,
                error = %e,
                "Could not fetch cover image, uploading without artwork"
            );
        }
    }

    // Upload to SoundCloud
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/tracks", SOUNDCLOUD_API_BASE))
        .header("Authorization", format!("OAuth {}", access_token))
        .multipart(form)
        .send()
        .await
        .map_err(|e| AppError::Internal(format!("SoundCloud upload request failed: {}", e)))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_else(|_| "no body".to_string());
        tracing::error!(
            show_id = show_id,
            status = %status,
            body = body.as_str(),
            "SoundCloud upload failed"
        );
        return Ok(SoundCloudUploadResult {
            success: false,
            track_id: None,
            track_url: None,
            error: Some(format!("SoundCloud API error ({}): {}", status, body)),
        });
    }

    let track: SoundCloudTrack = resp
        .json()
        .await
        .map_err(|e| AppError::Internal(format!("Failed to parse SoundCloud response: {}", e)))?;

    let track_id = track.id.to_string();
    let track_url = track
        .permalink_url
        .unwrap_or_else(|| format!("https://soundcloud.com/radio-moafunk/{}", track.id));

    tracing::info!(
        show_id = show_id,
        track_id = track_id.as_str(),
        track_url = track_url.as_str(),
        "Successfully uploaded to SoundCloud"
    );

    // Store in database
    sqlx::query(
        "UPDATE shows SET soundcloud_track_id = ?, soundcloud_url = ?, soundcloud_uploaded_at = datetime('now'), soundcloud_public = 0 WHERE id = ?"
    )
    .bind(&track_id)
    .bind(&track_url)
    .bind(show_id)
    .execute(&state.db)
    .await?;

    Ok(SoundCloudUploadResult {
        success: true,
        track_id: Some(track_id),
        track_url: Some(track_url),
        error: None,
    })
}

// ============================================================================
// Privacy Toggle
// ============================================================================

/// Set a SoundCloud track's sharing to public or private.
pub async fn set_track_privacy(
    state: &Arc<AppState>,
    show_id: i64,
    public: bool,
) -> Result<SoundCloudUploadResult> {
    let access_token = get_access_token(state).await?;

    // Fetch show to get track ID
    let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(show_id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound("Show not found".to_string()))?;

    let track_id = show.soundcloud_track_id.as_ref().ok_or_else(|| {
        AppError::BadRequest("Show has not been uploaded to SoundCloud".to_string())
    })?;

    let sharing = if public { "public" } else { "private" };

    tracing::info!(
        show_id = show_id,
        track_id = track_id.as_str(),
        sharing = sharing,
        "Setting SoundCloud track privacy"
    );

    let client = reqwest::Client::new();
    let resp = client
        .put(format!("{}/tracks/{}", SOUNDCLOUD_API_BASE, track_id))
        .header("Authorization", format!("OAuth {}", access_token))
        .header("Content-Type", "application/json")
        .json(&serde_json::json!({
            "track": {
                "sharing": sharing
            }
        }))
        .send()
        .await
        .map_err(|e| AppError::Internal(format!("SoundCloud privacy update failed: {}", e)))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_else(|_| "no body".to_string());
        tracing::error!(
            show_id = show_id,
            track_id = track_id.as_str(),
            status = %status,
            body = body.as_str(),
            "SoundCloud privacy update failed"
        );
        return Ok(SoundCloudUploadResult {
            success: false,
            track_id: Some(track_id.clone()),
            track_url: show.soundcloud_url.clone(),
            error: Some(format!("SoundCloud API error ({}): {}", status, body)),
        });
    }

    // Update DB
    sqlx::query("UPDATE shows SET soundcloud_public = ? WHERE id = ?")
        .bind(public as i32)
        .bind(show_id)
        .execute(&state.db)
        .await?;

    tracing::info!(
        show_id = show_id,
        track_id = track_id.as_str(),
        public = public,
        "SoundCloud track privacy updated"
    );

    Ok(SoundCloudUploadResult {
        success: true,
        track_id: Some(track_id.clone()),
        track_url: show.soundcloud_url.clone(),
        error: None,
    })
}

// ============================================================================
// OAuth Authorization Code Flow
// ============================================================================

const SOUNDCLOUD_AUTHORIZE_URL: &str = "https://secure.soundcloud.com/authorize";

/// Build the SoundCloud OAuth authorization URL that the admin should visit.
pub fn get_auth_url(state: &Arc<AppState>) -> Result<String> {
    let client_id =
        state.config.soundcloud_client_id.as_ref().ok_or_else(|| {
            AppError::BadRequest("SOUNDCLOUD_CLIENT_ID not configured".to_string())
        })?;

    let redirect_uri = &state.config.soundcloud_redirect_uri;

    // Simple percent-encoding for the redirect URI (only chars that need encoding in our URLs)
    let encoded_redirect = redirect_uri.replace(':', "%3A").replace('/', "%2F");

    let url = format!(
        "{}?response_type=code&client_id={}&redirect_uri={}",
        SOUNDCLOUD_AUTHORIZE_URL, client_id, encoded_redirect,
    );

    Ok(url)
}

/// Exchange an authorization code for an access token and store it in the database.
pub async fn exchange_code(state: &Arc<AppState>, code: &str) -> Result<String> {
    let client_id =
        state.config.soundcloud_client_id.as_ref().ok_or_else(|| {
            AppError::BadRequest("SOUNDCLOUD_CLIENT_ID not configured".to_string())
        })?;
    let client_secret = state
        .config
        .soundcloud_client_secret
        .as_ref()
        .ok_or_else(|| {
            AppError::BadRequest("SOUNDCLOUD_CLIENT_SECRET not configured".to_string())
        })?;
    let redirect_uri = &state.config.soundcloud_redirect_uri;

    let client = reqwest::Client::new();
    let resp = client
        .post(SOUNDCLOUD_OAUTH_URL)
        .form(&[
            ("grant_type", "authorization_code"),
            ("code", code),
            ("client_id", client_id),
            ("client_secret", client_secret),
            ("redirect_uri", redirect_uri),
        ])
        .send()
        .await
        .map_err(|e| AppError::Internal(format!("SoundCloud token exchange failed: {}", e)))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_else(|_| "no body".to_string());
        return Err(AppError::Internal(format!(
            "SoundCloud token exchange failed ({}): {}",
            status, body
        )));
    }

    let token_resp: OAuthTokenResponse = resp
        .json()
        .await
        .map_err(|e| AppError::Internal(format!("Failed to parse SoundCloud token: {}", e)))?;

    // Store the token in DB for persistence across restarts
    store_token(state, &token_resp.access_token).await?;

    tracing::info!("SoundCloud OAuth authorization successful, token stored");
    Ok(token_resp.access_token)
}

/// Get the current SoundCloud authorization status.
#[derive(Debug, Serialize)]
pub struct SoundCloudStatus {
    pub configured: bool,
    pub authorized: bool,
    pub auth_url: Option<String>,
}

pub async fn get_status(state: &Arc<AppState>) -> SoundCloudStatus {
    let configured = is_configured(state);
    let authorized = has_token(state).await;
    // Always provide auth_url when configured, so the admin can reconnect
    let auth_url = if configured {
        get_auth_url(state).ok()
    } else {
        None
    };

    SoundCloudStatus {
        configured,
        authorized,
        auth_url,
    }
}
