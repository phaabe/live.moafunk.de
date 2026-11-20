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

/// SoundCloud API base. Tests point it at a local fake.
fn api_base() -> String {
    #[cfg(not(test))]
    let base = "https://api.soundcloud.com".to_string();
    #[cfg(test)]
    let base = tests::API_BASE.with(|base| base.borrow().clone());
    base
}

/// OAuth token endpoint. Tests use the fake API base for it too.
fn oauth_url() -> String {
    #[cfg(not(test))]
    let url = "https://secure.soundcloud.com/oauth/token".to_string();
    #[cfg(test)]
    let url = format!("{}/oauth/token", api_base());
    url
}

// ============================================================================
// API Response Types
// ============================================================================

#[derive(Debug, Deserialize)]
struct OAuthTokenResponse {
    access_token: String,
    refresh_token: Option<String>,
    expires_in: Option<i64>,
    #[allow(dead_code)]
    token_type: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct SoundCloudTrack {
    pub id: i64,
    pub permalink_url: Option<String>,
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

const ACCESS_KEY: &str = "soundcloud_access_token";
const REFRESH_KEY: &str = "soundcloud_refresh_token";
const EXPIRES_KEY: &str = "soundcloud_token_expires_at";

/// Refresh the access token when it expires within this many seconds.
const REFRESH_MARGIN_SECS: i64 = 300;

/// One refresh at a time: SoundCloud may replace the refresh token on each
/// refresh, so a parallel refresh with the old one would fail.
static REFRESH_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

/// The stored OAuth tokens. `expires_at` is a Unix time; `None` means unknown
/// (a token stored before the expiry was kept).
struct StoredToken {
    access: String,
    refresh: Option<String>,
    expires_at: Option<i64>,
}

impl StoredToken {
    fn expires_within(&self, secs: i64) -> bool {
        self.expires_at
            .is_some_and(|at| at - chrono::Utc::now().timestamp() < secs)
    }
}

fn not_authorized() -> AppError {
    AppError::BadRequest(
        "SoundCloud not authorized. Visit /api/soundcloud/auth to connect your account."
            .to_string(),
    )
}

/// Get a SoundCloud access token.
/// Priority: 1) static config token, 2) DB-stored OAuth token, refreshed when
/// it expires soon, 3) error.
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
        if !token.expires_within(REFRESH_MARGIN_SECS) {
            tracing::debug!("Using DB-stored SoundCloud OAuth token");
            return Ok(token.access);
        }
        if token.refresh.is_some() {
            return refresh_access_token(state).await;
        }
        if !token.expires_within(0) {
            // No refresh token, but still valid for a few minutes.
            return Ok(token.access);
        }
        tracing::warn!("Stored SoundCloud token expired and there is no refresh token");
    }

    // 3. No valid token available
    Err(not_authorized())
}

/// Get a new access token with the stored refresh token and store both.
/// When SoundCloud refuses the refresh token (400/401), the stored tokens are
/// removed, so the status shows "not connected" and the admin reconnects.
async fn refresh_access_token(state: &Arc<AppState>) -> Result<String> {
    let _guard = REFRESH_LOCK.lock().await;
    // Another task may have refreshed while this one waited.
    let token = get_stored_token(state).await?.ok_or_else(not_authorized)?;
    if !token.expires_within(REFRESH_MARGIN_SECS) {
        return Ok(token.access);
    }
    let refresh = token.refresh.ok_or_else(not_authorized)?;
    let (client_id, client_secret) = client_credentials(state)?;

    let resp = reqwest::Client::new()
        .post(oauth_url())
        .form(&[
            ("grant_type", "refresh_token"),
            ("refresh_token", refresh.as_str()),
            ("client_id", client_id),
            ("client_secret", client_secret),
        ])
        .send()
        .await
        .map_err(|e| AppError::Internal(format!("SoundCloud token refresh failed: {}", e)))?;

    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_else(|_| "no body".to_string());
        if matches!(status.as_u16(), 400 | 401) {
            tracing::warn!(%status, "SoundCloud refused the refresh token, removing stored tokens");
            delete_stored_token(state).await?;
        }
        return Err(AppError::Internal(format!(
            "SoundCloud token refresh failed ({}): {}",
            status, body
        )));
    }

    let new: OAuthTokenResponse = resp
        .json()
        .await
        .map_err(|e| AppError::Internal(format!("Failed to parse SoundCloud token: {}", e)))?;
    // Without a new refresh token in the answer, the old one stays valid.
    let refresh = new.refresh_token.as_deref().unwrap_or(&refresh);
    store_token(state, &new.access_token, Some(refresh), new.expires_in).await?;
    tracing::info!("Refreshed SoundCloud access token");
    Ok(new.access_token)
}

/// Retrieve the stored SoundCloud tokens from the app_settings table.
async fn get_stored_token(state: &Arc<AppState>) -> Result<Option<StoredToken>> {
    let rows: Vec<(String, String)> =
        sqlx::query_as("SELECT key, value FROM app_settings WHERE key IN (?, ?, ?)")
            .bind(ACCESS_KEY)
            .bind(REFRESH_KEY)
            .bind(EXPIRES_KEY)
            .fetch_all(&state.db)
            .await?;
    let get = |key: &str| rows.iter().find(|(k, _)| k == key).map(|(_, v)| v.clone());
    Ok(get(ACCESS_KEY).map(|access| StoredToken {
        access,
        refresh: get(REFRESH_KEY),
        expires_at: get(EXPIRES_KEY).and_then(|v| v.parse().ok()),
    }))
}

/// Store SoundCloud OAuth tokens in the app_settings table. A missing refresh
/// token or expiry removes the old value, so it never sticks to a new access token.
pub async fn store_token(
    state: &Arc<AppState>,
    access: &str,
    refresh: Option<&str>,
    expires_in: Option<i64>,
) -> Result<()> {
    let expires_at = expires_in.map(|secs| (chrono::Utc::now().timestamp() + secs).to_string());
    let mut tx = state.db.begin().await?;
    for (key, value) in [
        (ACCESS_KEY, Some(access)),
        (REFRESH_KEY, refresh),
        (EXPIRES_KEY, expires_at.as_deref()),
    ] {
        match value {
            Some(value) => sqlx::query(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, datetime('now'))
                 ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            )
            .bind(key)
            .bind(value),
            None => sqlx::query("DELETE FROM app_settings WHERE key = ?").bind(key),
        }
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    tracing::info!("Stored SoundCloud OAuth tokens in database");
    Ok(())
}

/// Delete the stored SoundCloud tokens from the database.
/// This forces re-authorization on the next upload attempt.
pub async fn delete_stored_token(state: &Arc<AppState>) -> Result<()> {
    sqlx::query("DELETE FROM app_settings WHERE key IN (?, ?, ?)")
        .bind(ACCESS_KEY)
        .bind(REFRESH_KEY)
        .bind(EXPIRES_KEY)
        .execute(&state.db)
        .await?;
    tracing::info!("Deleted stored SoundCloud tokens from database");
    Ok(())
}

/// Check if SoundCloud integration is configured (has client_id for auth flow, or a static token)
pub fn is_configured(state: &Arc<AppState>) -> bool {
    state.config.soundcloud_access_token.is_some() || state.config.soundcloud_client_id.is_some()
}

/// Check if SoundCloud has a usable access token (static, or stored in DB).
/// A stored token without a refresh token stops counting once it expired.
/// This is an async check unlike is_configured().
pub async fn has_token(state: &Arc<AppState>) -> bool {
    if state.config.soundcloud_access_token.is_some() {
        return true;
    }
    matches!(
        get_stored_token(state).await,
        Ok(Some(token)) if token.refresh.is_some() || !token.expires_within(0)
    )
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
                    // The form asks for a profile URL; keep it, build one from a handle.
                    let ig_link = if ig.starts_with("http") {
                        ig.clone()
                    } else {
                        format!("https://instagram.com/{}", ig.trim_start_matches('@'))
                    };
                    socials.push(format!("Instagram: {}", ig_link));
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
    ensure_enabled(state).await?;
    let access_token = get_access_token(state).await?;

    // Fetch show
    let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(show_id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound("Show not found".to_string()))?;

    // Resolve the audio source, in priority order:
    //   1. a manually attached recording (pre-recorded / UNHEARD shows),
    //   2. the latest *finalized* live recording (final.mp3, default bucket),
    //   3. the latest streamed *archive* MP3 (shows bucket) — streamed shows stay
    //      `raw` with only an archive_key, so this is the auto-upload source (#251).
    let (recording_bucket, recording_key, source_filename) = match show.recording_key.as_ref() {
        Some(key) => (
            state.config.r2_bucket_name.clone(),
            key.clone(),
            show.recording_filename
                .clone()
                .unwrap_or_else(|| "recording.mp3".to_string()),
        ),
        None => {
            if let Some(version) =
                crate::db::get_latest_finalized_recording_version(&state.db, show_id).await?
            {
                let final_key = version.final_key.clone().ok_or_else(|| {
                    AppError::BadRequest("Finalized recording has no file to upload".to_string())
                })?;
                (
                    state.config.r2_bucket_name.clone(),
                    final_key,
                    format!("{}.mp3", version.version),
                )
            } else if let Some(version) =
                crate::db::get_latest_archived_recording_version(&state.db, show_id).await?
            {
                let archive_key = version.archive_key.clone().ok_or_else(|| {
                    AppError::BadRequest("Archived recording has no file to upload".to_string())
                })?;
                (
                    state.config.r2_shows_bucket_name.clone(),
                    archive_key,
                    format!("{}.mp3", version.version),
                )
            } else {
                return Err(AppError::BadRequest(
                    "Show has no recording to upload".to_string(),
                ));
            }
        }
    };

    tracing::info!(
        show_id = show_id,
        bucket = recording_bucket.as_str(),
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

    // Download recording from R2 (from whichever bucket the source lives in)
    let (recording_bytes, recording_content_type) =
        storage::download_file_from_bucket(state, &recording_bucket, &recording_key).await?;
    tracing::info!(
        show_id = show_id,
        size_mb = recording_bytes.len() as f64 / 1_048_576.0,
        content_type = recording_content_type.as_str(),
        "Downloaded recording from R2"
    );
    let upload_seconds = upload_seconds_for(recording_bytes.len());

    // Determine filename for the upload
    let filename = source_filename.as_str();

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

    // The SoundCloud API cannot replace a track's audio in place (asset_data is
    // upload-only; PUT changes metadata/artwork only). To make a newer recording
    // supersede the show's track without piling up duplicates, delete the old track
    // first, then create a fresh one. Tradeoff: the new track gets a new URL and
    // drops the old track's plays/comments — acceptable for auto-archived private
    // shows. Deleting the old track also removes it from the show playlist, so the
    // playlist step below just adds the replacement.
    let client = reqwest::Client::new();
    if let Some(old_track_id) = show.soundcloud_track_id.as_deref() {
        match delete_track(&client, &access_token, old_track_id).await {
            Ok(()) => tracing::info!(
                show_id = show_id,
                old_track_id = old_track_id,
                "Deleted previous SoundCloud track before re-upload"
            ),
            Err(e) => tracing::warn!(
                show_id = show_id,
                old_track_id = old_track_id,
                error = %e,
                "Failed to delete previous SoundCloud track (continuing with a new upload)"
            ),
        }
    }

    // Staging only: make room on the test account first. An account over its
    // upload time refuses every upload, so cleaning up after an upload would
    // never run again. Best effort.
    if let Err(e) = cleanup_old_tracks(state, &access_token, upload_seconds).await {
        tracing::error!(error = %e, "Failed to delete old tracks before the upload");
    }

    let resp = client
        .post(format!("{}/tracks", api_base()))
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

    let track_numeric_id = track.id;
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

    // Group the track under a per-show-title playlist so every recording of a
    // recurring show lands in the same SoundCloud playlist. Best-effort: a
    // failure here must not fail the upload that already succeeded.
    if let Err(e) =
        ensure_track_in_show_playlist(&access_token, &show.title, track_numeric_id).await
    {
        tracing::error!(
            show_id = show_id,
            playlist = show.title.as_str(),
            error = %e,
            "Uploaded track but failed to add it to the show playlist"
        );
    }

    Ok(SoundCloudUploadResult {
        success: true,
        track_id: Some(track_id),
        track_url: Some(track_url),
        error: None,
    })
}

/// Delete a SoundCloud track by id. Used to supersede a show's previous track when
/// a newer recording is uploaded (the API can't replace audio in place). A 404 is
/// treated as success — the track is already gone.
async fn delete_track(client: &reqwest::Client, access_token: &str, track_id: &str) -> Result<()> {
    let resp = client
        .delete(format!("{}/tracks/{}", api_base(), track_id))
        .header("Authorization", format!("OAuth {}", access_token))
        .send()
        .await
        .map_err(|e| AppError::Internal(format!("Delete track request failed: {}", e)))?;

    if resp.status().is_success() || resp.status() == reqwest::StatusCode::NOT_FOUND {
        Ok(())
    } else {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        Err(AppError::Internal(format!(
            "Delete track failed ({}): {}",
            status, body
        )))
    }
}

#[derive(Debug, Deserialize)]
struct Me {
    permalink: String,
    quota: Option<Quota>,
}

/// Upload time of the account. A Free account has 2 hours.
#[derive(Debug, Deserialize)]
struct Quota {
    #[serde(default)]
    unlimited_upload_quota: bool,
    upload_seconds_left: Option<i64>,
}

#[derive(Debug, Deserialize)]
struct TrackSummary {
    id: i64,
    /// Length in milliseconds.
    duration: Option<i64>,
}

/// How many of `tracks` (newest first) stay before an upload; the rest is
/// deleted. The upload adds one track, so `keep - 1` stay. Then the oldest go
/// until the account has upload time left: SoundCloud refuses an upload (401)
/// while `upload_seconds_left` is 0 or less, and takes any length above 0.
/// So the oldest go until more time is left than the new track (`needed`
/// seconds) takes: a long track must not leave the account without time for
/// the next upload. Deleted time seems to come back with a delay (an upload
/// 0.1 s after a delete still got 401), so it is freed one upload early.
fn tracks_to_keep(
    tracks: &[TrackSummary],
    keep: usize,
    seconds_left: Option<i64>,
    needed: i64,
) -> usize {
    let seconds = |t: &TrackSummary| t.duration.unwrap_or(0) / 1000;
    let mut kept = (keep.max(1) - 1).min(tracks.len());
    if let Some(mut left) = seconds_left {
        left += tracks[kept..].iter().map(seconds).sum::<i64>();
        while left <= needed && kept > 0 {
            kept -= 1;
            left += seconds(&tracks[kept]);
        }
    }
    kept
}

/// Upload time (seconds) a file of `bytes` takes on SoundCloud: its length.
// shortcut: the length at 64 kbit/s, never shorter for an MP3 of 64 kbit/s or
// more; a lower bitrate can still leave the account over its time.
fn upload_seconds_for(bytes: usize) -> i64 {
    (bytes / 8_000) as i64
}

/// The items of a list answer. SoundCloud wraps a paginated list in
/// `{"collection": [...]}`; the old plain array is deprecated but still read.
fn list_items<T: serde::de::DeserializeOwned>(
    body: &str,
) -> std::result::Result<Vec<T>, serde_json::Error> {
    let mut value: serde_json::Value = serde_json::from_str(body)?;
    if let Some(collection) = value.get_mut("collection") {
        value = collection.take();
    }
    serde_json::from_value(value)
}

/// The station's production account. The staging cleanup refuses to run on it.
const STATION_ACCOUNT: &str = "radio-moafunk";

/// Permalink (the account name in the profile URL) of the token's account.
async fn me_permalink(access_token: &str) -> Result<String> {
    Ok(fetch_me(access_token).await?.permalink)
}

/// The token's account (`GET /me`).
async fn fetch_me(access_token: &str) -> Result<Me> {
    let resp = reqwest::Client::new()
        .get(format!("{}/me", api_base()))
        .header("Authorization", format!("OAuth {}", access_token))
        .send()
        .await
        .map_err(|e| AppError::Internal(format!("SoundCloud /me request failed: {}", e)))?;
    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(AppError::Internal(format!(
            "SoundCloud /me failed ({}): {}",
            status, body
        )));
    }
    resp.json()
        .await
        .map_err(|e| AppError::Internal(format!("Failed to parse SoundCloud /me: {}", e)))
}

/// Refuse a token of another account than `SOUNDCLOUD_ACCOUNT` (when set).
async fn check_account(state: &AppState, access_token: &str) -> Result<()> {
    let Some(want) = state.config.soundcloud_account.as_deref() else {
        return Ok(());
    };
    let got = me_permalink(access_token).await?;
    if got.eq_ignore_ascii_case(want) {
        return Ok(());
    }
    Err(AppError::BadRequest(format!(
        "This server uploads to the SoundCloud account '{want}', but you logged in as '{got}'. \
         Log out of SoundCloud, then connect with '{want}'."
    )))
}

/// Staging only, before an upload: make room on the test account. With the
/// new track, the newest `SOUNDCLOUD_KEEP_TRACKS` tracks stay; more go while
/// the account has no upload time left (see `tracks_to_keep`), or less than
/// the new track's `upload_seconds`. Needs
/// APP_ENV=staging, SOUNDCLOUD_ACCOUNT and a token of that account, so it can
/// never delete station tracks.
async fn cleanup_old_tracks(
    state: &AppState,
    access_token: &str,
    upload_seconds: i64,
) -> Result<()> {
    let Some(keep) = state.config.soundcloud_keep_tracks else {
        return Ok(());
    };
    if !state.config.app_env.trim().eq_ignore_ascii_case("staging") {
        return Err(AppError::Internal(
            "SOUNDCLOUD_KEEP_TRACKS works only with APP_ENV=staging; no track deleted".to_string(),
        ));
    }
    let Some(account) = state.config.soundcloud_account.as_deref() else {
        return Err(AppError::Internal(
            "SOUNDCLOUD_KEEP_TRACKS needs SOUNDCLOUD_ACCOUNT; no track deleted".to_string(),
        ));
    };
    // Even a wrong pin never cleans the station account.
    if account.eq_ignore_ascii_case(STATION_ACCOUNT) {
        return Err(AppError::Internal(format!(
            "SOUNDCLOUD_ACCOUNT is the station account '{STATION_ACCOUNT}'; no track deleted"
        )));
    }
    let me = fetch_me(access_token).await?;
    if !me.permalink.eq_ignore_ascii_case(account) {
        return Err(AppError::Internal(format!(
            "The token belongs to '{}', not to '{account}'; no track deleted",
            me.permalink
        )));
    }
    let seconds_left = me
        .quota
        .filter(|q| !q.unlimited_upload_quota)
        .and_then(|q| q.upload_seconds_left);

    let client = reqwest::Client::new();
    let resp = client
        .get(format!("{}/me/tracks", api_base()))
        .header("Authorization", format!("OAuth {}", access_token))
        .query(&[("limit", "200"), ("linked_partitioning", "true")])
        .send()
        .await
        .map_err(|e| AppError::Internal(format!("List tracks request failed: {}", e)))?;
    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(AppError::Internal(format!(
            "List tracks failed ({}): {}",
            status, body
        )));
    }
    // shortcut: one page of 200 tracks; enough while cleanup runs before every upload.
    let body = resp
        .text()
        .await
        .map_err(|e| AppError::Internal(format!("Failed to read tracks: {}", e)))?;
    let mut tracks: Vec<TrackSummary> = list_items(&body)
        .map_err(|e| AppError::Internal(format!("Failed to parse tracks: {}", e)))?;
    // Newest first: SoundCloud track ids grow over time.
    tracks.sort_by_key(|a| std::cmp::Reverse(a.id));
    let kept = tracks_to_keep(&tracks, keep, seconds_left, upload_seconds);
    for track in &tracks[kept..] {
        delete_track(&client, access_token, &track.id.to_string()).await?;
        tracing::info!(
            track_id = track.id,
            "Deleted old SoundCloud track (staging cleanup)"
        );
    }
    Ok(())
}

/// Whether a just-finalized recording should be auto-uploaded to SoundCloud.
/// Pure decision so the policy is testable: upload only when the feature is
/// enabled and the integration is configured. Not gated on "already uploaded":
/// a newer recording deliberately *replaces* the show's existing track audio.
/// Refuse every SoundCloud write while an admin has turned SoundCloud off.
pub async fn ensure_enabled(state: &Arc<AppState>) -> Result<()> {
    if crate::db::is_service_enabled(&state.db, crate::db::SOUNDCLOUD_ENABLED).await {
        Ok(())
    } else {
        Err(AppError::ServiceUnavailable(
            "SoundCloud is temporarily not available.".to_string(),
        ))
    }
}

pub(crate) fn should_auto_upload(enabled: bool, configured: bool) -> bool {
    enabled && configured
}

/// Best-effort automatic SoundCloud upload, fired when a recording finishes
/// finalizing (streamed shows via `upload_session`, UNHEARD via
/// `handle_finalize_socket`). Uploads as a private track with the show's title +
/// description via [`upload_track`] — which, if the show already has a track,
/// replaces that track's audio in place so a re-recording updates the same
/// SoundCloud track. Never returns an error: every failure is logged and swallowed
/// so it can run detached. No-ops when disabled or unconfigured.
pub async fn auto_upload_on_finalize(state: &Arc<AppState>, show_id: i64) {
    if !should_auto_upload(state.config.soundcloud_auto_upload, is_configured(state)) {
        tracing::debug!(
            show_id = show_id,
            enabled = state.config.soundcloud_auto_upload,
            configured = is_configured(state),
            "Skipping SoundCloud auto-upload (disabled or not configured)"
        );
        return;
    }
    if ensure_enabled(state).await.is_err() {
        tracing::info!(
            show_id,
            "Skipping SoundCloud auto-upload (turned off by an admin)"
        );
        return;
    }

    let show: models::Show = match sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(show_id)
        .fetch_optional(&state.db)
        .await
    {
        Ok(Some(show)) => show,
        Ok(None) => {
            tracing::warn!(show_id, "SoundCloud auto-upload: show not found");
            return;
        }
        Err(e) => {
            tracing::error!(show_id, error = %e, "SoundCloud auto-upload: failed to load show");
            return;
        }
    };

    if show.soundcloud_track_id.is_some() {
        tracing::info!(
            show_id,
            "Auto-upload: superseding show's SoundCloud track (delete + re-upload)"
        );
    } else {
        tracing::info!(show_id, "Auto-uploading finalized recording to SoundCloud");
    }
    match upload_track(state, show_id).await {
        Ok(result) if result.success => {
            if let Some(ref url) = result.track_url {
                let title = build_title(state, &show)
                    .await
                    .unwrap_or_else(|_| format!("Show #{show_id}"));
                crate::telegram_notify::notify_soundcloud_upload(state, show_id, &title, url);
            }
            tracing::info!(show_id, "SoundCloud auto-upload succeeded");
        }
        Ok(result) => {
            tracing::error!(
                show_id,
                error = result.error.as_deref().unwrap_or("unknown"),
                "SoundCloud auto-upload failed"
            );
        }
        Err(e) => {
            tracing::error!(show_id, error = %e, "SoundCloud auto-upload errored");
        }
    }
}

// ============================================================================
// Playlists (one per show title)
// ============================================================================

#[derive(Debug, Deserialize)]
struct PlaylistSummary {
    id: i64,
    title: Option<String>,
}

#[derive(Debug, Deserialize)]
struct PlaylistDetail {
    #[serde(default)]
    tracks: Vec<PlaylistTrackRef>,
}

#[derive(Debug, Deserialize)]
struct PlaylistTrackRef {
    id: i64,
}

/// Ensure a playlist named exactly `show_title` exists and contains `track_id`.
/// Finds the caller's playlist by title (case-insensitive), otherwise creates a
/// private one. Adding to an existing playlist re-sends the union of its current
/// tracks and the new one (SoundCloud replaces the whole track list on update).
async fn ensure_track_in_show_playlist(
    access_token: &str,
    show_title: &str,
    track_id: i64,
) -> Result<()> {
    let client = reqwest::Client::new();

    match find_playlist_by_title(&client, access_token, show_title).await? {
        Some(playlist_id) => {
            let mut ids = get_playlist_track_ids(&client, access_token, playlist_id).await?;
            if ids.contains(&track_id) {
                tracing::debug!(playlist_id, track_id, "Track already in show playlist");
                return Ok(());
            }
            ids.push(track_id);
            set_playlist_tracks(&client, access_token, playlist_id, &ids).await?;
            tracing::info!(
                playlist_id,
                track_id,
                "Added track to existing show playlist"
            );
        }
        None => {
            let playlist_id = create_playlist(&client, access_token, show_title, track_id).await?;
            tracing::info!(
                playlist_id,
                track_id,
                playlist = show_title,
                "Created show playlist"
            );
        }
    }
    Ok(())
}

/// Find one of the caller's playlists whose title matches `title` (case-insensitive).
async fn find_playlist_by_title(
    client: &reqwest::Client,
    access_token: &str,
    title: &str,
) -> Result<Option<i64>> {
    let resp = client
        .get(format!("{}/me/playlists", api_base()))
        .header("Authorization", format!("OAuth {}", access_token))
        .query(&[("limit", "200"), ("linked_partitioning", "true")])
        .send()
        .await
        .map_err(|e| AppError::Internal(format!("List playlists request failed: {}", e)))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(AppError::Internal(format!(
            "List playlists failed ({}): {}",
            status, body
        )));
    }

    let body = resp
        .text()
        .await
        .map_err(|e| AppError::Internal(format!("Failed to read playlists: {}", e)))?;
    let playlists: Vec<PlaylistSummary> = list_items(&body)
        .map_err(|e| AppError::Internal(format!("Failed to parse playlists: {}", e)))?;

    let wanted = title.trim().to_lowercase();
    Ok(playlists
        .into_iter()
        .find(|p| {
            p.title
                .as_deref()
                .map(|t| t.trim().to_lowercase() == wanted)
                .unwrap_or(false)
        })
        .map(|p| p.id))
}

/// Fetch the current track ids of a playlist.
async fn get_playlist_track_ids(
    client: &reqwest::Client,
    access_token: &str,
    playlist_id: i64,
) -> Result<Vec<i64>> {
    let resp = client
        .get(format!("{}/playlists/{}", api_base(), playlist_id))
        .header("Authorization", format!("OAuth {}", access_token))
        .send()
        .await
        .map_err(|e| AppError::Internal(format!("Get playlist request failed: {}", e)))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(AppError::Internal(format!(
            "Get playlist failed ({}): {}",
            status, body
        )));
    }

    let detail: PlaylistDetail = resp
        .json()
        .await
        .map_err(|e| AppError::Internal(format!("Failed to parse playlist: {}", e)))?;
    Ok(detail.tracks.into_iter().map(|t| t.id).collect())
}

/// Replace a playlist's track list (SoundCloud PUT semantics).
async fn set_playlist_tracks(
    client: &reqwest::Client,
    access_token: &str,
    playlist_id: i64,
    track_ids: &[i64],
) -> Result<()> {
    let tracks: Vec<serde_json::Value> = track_ids
        .iter()
        .map(|id| serde_json::json!({ "id": id }))
        .collect();
    let resp = client
        .put(format!("{}/playlists/{}", api_base(), playlist_id))
        .header("Authorization", format!("OAuth {}", access_token))
        .json(&serde_json::json!({ "playlist": { "tracks": tracks } }))
        .send()
        .await
        .map_err(|e| AppError::Internal(format!("Update playlist request failed: {}", e)))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(AppError::Internal(format!(
            "Update playlist failed ({}): {}",
            status, body
        )));
    }
    Ok(())
}

/// Create a private playlist with a single initial track. Returns its id.
async fn create_playlist(
    client: &reqwest::Client,
    access_token: &str,
    title: &str,
    track_id: i64,
) -> Result<i64> {
    let resp = client
        .post(format!("{}/playlists", api_base()))
        .header("Authorization", format!("OAuth {}", access_token))
        .json(&serde_json::json!({
            "playlist": {
                "title": title,
                "sharing": "private",
                "tracks": [{ "id": track_id }],
            }
        }))
        .send()
        .await
        .map_err(|e| AppError::Internal(format!("Create playlist request failed: {}", e)))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(AppError::Internal(format!(
            "Create playlist failed ({}): {}",
            status, body
        )));
    }

    let created: PlaylistSummary = resp
        .json()
        .await
        .map_err(|e| AppError::Internal(format!("Failed to parse created playlist: {}", e)))?;
    Ok(created.id)
}

// ============================================================================
// Privacy Toggle
// ============================================================================

/// Set a SoundCloud track's sharing to public or private.
/// How often the scheduler asks SoundCloud about tracks that posts wait for.
pub const PUBLIC_CHECK_EVERY: chrono::Duration = chrono::Duration::minutes(15);

/// A track made public on soundcloud.com, not in the app, never set
/// `soundcloud_public`. For every episode whose post-announcement waits for
/// SoundCloud and whose track is private here, ask SoundCloud for the
/// track's sharing and mark the public ones. Returns the shows marked public.
pub async fn sync_public_tracks(state: &Arc<AppState>) -> Result<Vec<i64>> {
    let rows: Vec<(i64, String)> = sqlx::query_as(
        "SELECT DISTINCT s.id, s.soundcloud_track_id FROM shows s \
         JOIN announcements a ON a.show_id = s.id \
         WHERE a.slot = 'post' AND a.state = 'waiting_soundcloud' \
           AND s.soundcloud_track_id IS NOT NULL AND COALESCE(s.soundcloud_public, 0) = 0 \
         ORDER BY s.id",
    )
    .fetch_all(&state.db)
    .await?;
    if rows.is_empty() || !is_configured(state) || !has_token(state).await {
        return Ok(Vec::new());
    }
    let access_token = get_access_token(state).await?;

    #[derive(Deserialize)]
    struct Sharing {
        sharing: Option<String>,
    }
    let client = reqwest::Client::new();
    let mut marked = Vec::new();
    for (show_id, track_id) in rows {
        let resp = client
            .get(format!("{}/tracks/{}", api_base(), track_id))
            .header("Authorization", format!("OAuth {}", access_token))
            .send()
            .await;
        let track = match resp {
            Ok(r) if r.status().is_success() => r.json::<Sharing>().await.ok(),
            Ok(r) => {
                tracing::warn!(show_id, track_id, status = %r.status(), "SoundCloud track check refused");
                None
            }
            Err(e) => {
                tracing::warn!(show_id, track_id, "SoundCloud track check failed: {e}");
                None
            }
        };
        if track.and_then(|t| t.sharing).as_deref() == Some("public") {
            sqlx::query("UPDATE shows SET soundcloud_public = 1 WHERE id = ?")
                .bind(show_id)
                .execute(&state.db)
                .await?;
            tracing::info!(
                show_id,
                track_id,
                "SoundCloud track is public on SoundCloud"
            );
            marked.push(show_id);
        }
    }
    Ok(marked)
}

pub async fn set_track_privacy(
    state: &Arc<AppState>,
    show_id: i64,
    public: bool,
) -> Result<SoundCloudUploadResult> {
    ensure_enabled(state).await?;
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
        .put(format!("{}/tracks/{}", api_base(), track_id))
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
    // Back to private: a post not sent yet must not link to a private track,
    // so it waits again; going public sets its time anew.
    if !public {
        sqlx::query(
            "UPDATE announcements SET state = 'waiting_soundcloud', scheduled_at = NULL, \
               updated_at = CURRENT_TIMESTAMP \
             WHERE show_id = ? AND slot = 'post' AND state IN ('scheduled', 'pending_approval') \
               AND send_claimed_at IS NULL",
        )
        .bind(show_id)
        .execute(&state.db)
        .await?;
    }
    // A public track unlocks the episode's post announcement.
    if public {
        if let Err(e) =
            crate::announcement_send::unlock_waiting_posts(state, chrono::Utc::now()).await
        {
            tracing::error!("Announcements: post unlock failed: {e}");
        }
    }

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

/// A login link works this long and only once.
const LOGIN_TTL_SECS: i64 = 600;
/// Most open login links kept; the oldest goes first.
const MAX_PENDING_LOGINS: usize = 200;

/// An open login: its OAuth `state` and PKCE code verifier.
struct PendingLogin {
    state: String,
    verifier: String,
    created: i64,
}

/// Open logins, in memory: after a restart the admin clicks Connect again.
static PENDING_LOGINS: std::sync::Mutex<Vec<PendingLogin>> = std::sync::Mutex::new(Vec::new());

/// PKCE S256 code challenge of a verifier.
fn pkce_challenge(verifier: &str) -> String {
    use sha2::{Digest, Sha256};
    base64_url::encode(&Sha256::digest(verifier.as_bytes()))
}

/// Open a login and return its OAuth state and PKCE code challenge. The state
/// starts with the server's APP_ENV (`staging.…`), so production can forward
/// staging logins (see [`forward_login`]).
fn start_login(app_env: &str) -> (String, String) {
    let now = chrono::Utc::now().timestamp();
    let login = PendingLogin {
        state: format!(
            "{}.{}",
            app_env.trim().to_ascii_lowercase(),
            crate::auth::generate_session_token()
        ),
        verifier: crate::auth::generate_session_token(),
        created: now,
    };
    let opened = (login.state.clone(), pkce_challenge(&login.verifier));
    let mut pending = PENDING_LOGINS.lock().unwrap_or_else(|e| e.into_inner());
    pending.retain(|l| now - l.created < LOGIN_TTL_SECS);
    if pending.len() >= MAX_PENDING_LOGINS {
        pending.remove(0);
    }
    pending.push(login);
    opened
}

/// The code verifier of an open login. Each state works once.
fn take_login(oauth_state: &str) -> Option<String> {
    let mut pending = PENDING_LOGINS.lock().unwrap_or_else(|e| e.into_inner());
    let i = pending.iter().position(|l| l.state == oauth_state)?;
    let login = pending.remove(i);
    (chrono::Utc::now().timestamp() - login.created < LOGIN_TTL_SECS).then_some(login.verifier)
}

/// Build the SoundCloud OAuth authorization URL that the admin should visit.
/// Each call opens a new login (OAuth state + PKCE, which SoundCloud requires).
pub fn get_auth_url(state: &Arc<AppState>) -> Result<String> {
    let client_id =
        state.config.soundcloud_client_id.as_ref().ok_or_else(|| {
            AppError::BadRequest("SOUNDCLOUD_CLIENT_ID not configured".to_string())
        })?;

    let redirect_uri = &state.config.soundcloud_redirect_uri;

    // Simple percent-encoding for the redirect URI (only chars that need encoding in our URLs)
    let encoded_redirect = redirect_uri.replace(':', "%3A").replace('/', "%2F");

    // Both are base64url, so they need no encoding.
    let (oauth_state, challenge) = start_login(&state.config.app_env);
    let url = format!(
        "{}?response_type=code&client_id={}&redirect_uri={}\
         &code_challenge={}&code_challenge_method=S256&state={}",
        SOUNDCLOUD_AUTHORIZE_URL, client_id, encoded_redirect, challenge, oauth_state,
    );

    Ok(url)
}

/// State prefix of logins started on staging.
const STAGING_STATE_PREFIX: &str = "staging.";

/// Production only: SoundCloud allows one redirect URI per app, so a login
/// started on staging comes back to production's callback. Returns where to
/// send it on (`SOUNDCLOUD_FORWARD_URL` with the same code and state), or
/// `None` to handle the login here. Staging itself never forwards.
pub fn forward_login(state: &AppState, code: &str, oauth_state: &str) -> Option<String> {
    let target = state.config.soundcloud_forward_url.as_deref()?;
    if state.config.app_env.trim().eq_ignore_ascii_case("staging")
        || !oauth_state.starts_with(STAGING_STATE_PREFIX)
    {
        return None;
    }
    let query = serde_urlencoded::to_string([("code", code), ("state", oauth_state)]).ok()?;
    Some(format!("{target}?{query}"))
}

/// The app's SoundCloud client id and secret.
fn client_credentials(state: &AppState) -> Result<(&str, &str)> {
    let client_id = state
        .config
        .soundcloud_client_id
        .as_deref()
        .ok_or_else(|| AppError::BadRequest("SOUNDCLOUD_CLIENT_ID not configured".to_string()))?;
    let client_secret = state
        .config
        .soundcloud_client_secret
        .as_deref()
        .ok_or_else(|| {
            AppError::BadRequest("SOUNDCLOUD_CLIENT_SECRET not configured".to_string())
        })?;
    Ok((client_id, client_secret))
}

/// Exchange an authorization code for an access token and store it in the database.
/// `oauth_state` must be the state of an open login from [`get_auth_url`].
pub async fn exchange_code(state: &Arc<AppState>, code: &str, oauth_state: &str) -> Result<String> {
    let (client_id, client_secret) = client_credentials(state)?;
    let redirect_uri = &state.config.soundcloud_redirect_uri;
    let verifier = take_login(oauth_state).ok_or_else(|| {
        AppError::BadRequest(
            "This SoundCloud login is unknown, used or older than 10 minutes. Click Connect again."
                .to_string(),
        )
    })?;

    let client = reqwest::Client::new();
    let resp = client
        .post(oauth_url())
        .form(&[
            ("grant_type", "authorization_code"),
            ("code", code),
            ("client_id", client_id),
            ("client_secret", client_secret),
            ("redirect_uri", redirect_uri),
            ("code_verifier", verifier.as_str()),
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

    // Only the account this server is meant for (production vs. test account).
    check_account(state, &token_resp.access_token).await?;

    // Store the tokens in DB for persistence across restarts
    store_token(
        state,
        &token_resp.access_token,
        token_resp.refresh_token.as_deref(),
        token_resp.expires_in,
    )
    .await?;

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

/// The account behind the current token, asked live at SoundCloud (`GET /me`).
/// Uses the same token as an upload, so a dead or unrefreshable token fails here.
pub async fn check_connection(state: &Arc<AppState>) -> Result<String> {
    let token = get_access_token(state).await?;
    me_permalink(&token).await
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

#[cfg(test)]
#[path = "soundcloud_tests.rs"]
mod tests;
