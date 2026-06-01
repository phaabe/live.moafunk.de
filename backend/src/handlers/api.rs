//! JSON API handlers for the admin SPA
//!
//! These endpoints mirror the functionality of the template-based handlers
//! but return JSON responses for the Vue 3 admin panel.

use crate::{ai, auth, models, storage, telegram_notify, video, AppError, AppState, Result};
use axum::{
    extract::{Path, Query, State},
    http::{header, HeaderMap, StatusCode},
    response::IntoResponse,
    Json,
};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

const MAX_ARTISTS_PER_SHOW: i64 = 4;

/// Derive the peaks JSON key from an audio file key.
/// Peaks are stored alongside the audio file with `.peaks.json` extension:
///   `artists/5/track1/ursi murps.mp3` → `artists/5/track1/ursi murps.peaks.json`
fn derive_peaks_key(audio_key: &str) -> String {
    if let Some((base, _ext)) = audio_key.rsplit_once('.') {
        format!("{}.peaks.json", base)
    } else {
        format!("{}.peaks.json", audio_key)
    }
}

// ============================================================================
// Auth API
// ============================================================================

#[derive(Debug, Deserialize)]
pub struct LoginRequest {
    username: String,
    password: String,
}

#[derive(Debug, Serialize)]
pub struct UserResponse {
    id: i64,
    username: String,
    role: String,
    artist_id: Option<i64>,
    artist_name: Option<String>,
    has_show: bool,
    must_change_password: bool,
}

#[derive(Debug, Serialize)]
pub struct LoginResponse {
    user: UserResponse,
    redirect_url: String,
}

pub async fn api_login(
    State(state): State<Arc<AppState>>,
    Json(req): Json<LoginRequest>,
) -> Result<impl IntoResponse> {
    // Look up user by username
    let user: Option<models::User> = sqlx::query_as("SELECT * FROM users WHERE username = ?")
        .bind(&req.username)
        .fetch_optional(&state.db)
        .await?;

    let user = match user {
        Some(u) => u,
        None => {
            return Err(AppError::Unauthorized(
                "Invalid username or password".to_string(),
            ))
        }
    };

    // Verify password
    if !auth::verify_password(&req.password, &user.password_hash) {
        return Err(AppError::Unauthorized(
            "Invalid username or password".to_string(),
        ));
    }

    // Check if account is expired
    if user.is_expired() {
        return Err(AppError::Unauthorized(
            "Your account has expired. Please contact an administrator.".to_string(),
        ));
    }

    // Guests may only sign in on their show's date.
    if !user.is_login_allowed_today() {
        return Err(AppError::Unauthorized(
            "Guest login is only available on the day of the show.".to_string(),
        ));
    }

    // Create session
    let token = auth::create_session(&state, user.id).await?;

    // Look up linked artist and show assignment for the user
    let artist_info: Option<(i64, String)> =
        sqlx::query_as("SELECT a.id, a.name FROM artists a WHERE a.user_id = ?")
            .bind(user.id)
            .fetch_optional(&state.db)
            .await?;

    let (artist_id, artist_name) = match &artist_info {
        Some((id, name)) => (Some(*id), Some(name.clone())),
        None => (None, None),
    };

    let has_show = if let Some((aid, _)) = &artist_info {
        sqlx::query_scalar::<_, i64>(
            "SELECT COUNT(*) FROM artist_show_assignments WHERE artist_id = ?",
        )
        .bind(aid)
        .fetch_one(&state.db)
        .await
        .unwrap_or(0)
            > 0
    } else {
        false
    };

    // Determine redirect based on role
    let redirect_url = match user.role_enum() {
        models::UserRole::Host | models::UserRole::Guest => "/#/stream",
        models::UserRole::Admin | models::UserRole::Superadmin => "/#/artists",
    };

    let cookie = format!(
        "session={}; HttpOnly; Secure; SameSite=Strict; Max-Age={}; Path=/",
        token,
        60 * 60 * 24 * 7 // 7 days
    );

    let response = LoginResponse {
        user: UserResponse {
            id: user.id,
            username: user.username,
            role: user.role,
            artist_id,
            artist_name,
            has_show,
            must_change_password: user.must_change_password,
        },
        redirect_url: redirect_url.to_string(),
    };

    Ok((
        StatusCode::OK,
        [(header::SET_COOKIE, cookie)],
        Json(response),
    ))
}

pub async fn api_logout() -> impl IntoResponse {
    let cookie = "session=; HttpOnly; Secure; SameSite=Strict; Max-Age=0; Path=/";

    (
        StatusCode::OK,
        [(header::SET_COOKIE, cookie.to_string())],
        Json(serde_json::json!({ "success": true })),
    )
}

pub async fn api_me(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let token = auth::get_session_from_headers(&headers);
    let user = auth::get_current_user(&state, token.as_deref()).await;

    match user {
        Some(u) => {
            // Look up linked artist and show assignment
            let artist_info: Option<(i64, String)> =
                sqlx::query_as("SELECT a.id, a.name FROM artists a WHERE a.user_id = ?")
                    .bind(u.id)
                    .fetch_optional(&state.db)
                    .await?;

            let (artist_id, artist_name) = match &artist_info {
                Some((id, name)) => (Some(*id), Some(name.clone())),
                None => (None, None),
            };

            let has_show = if let Some((aid, _)) = &artist_info {
                sqlx::query_scalar::<_, i64>(
                    "SELECT COUNT(*) FROM artist_show_assignments WHERE artist_id = ?",
                )
                .bind(aid)
                .fetch_one(&state.db)
                .await
                .unwrap_or(0)
                    > 0
            } else {
                false
            };

            Ok(Json(UserResponse {
                id: u.id,
                username: u.username,
                role: u.role,
                artist_id,
                artist_name,
                has_show,
                must_change_password: u.must_change_password,
            }))
        }
        None => Err(AppError::Unauthorized("Not authenticated".to_string())),
    }
}

#[derive(Debug, Deserialize)]
pub struct ChangePasswordRequest {
    current_password: String,
    new_password: String,
}

pub async fn api_change_password(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(req): Json<ChangePasswordRequest>,
) -> Result<impl IntoResponse> {
    let token = auth::get_session_from_headers(&headers);
    let user = auth::get_current_user(&state, token.as_deref())
        .await
        .ok_or_else(|| AppError::Unauthorized("Not authenticated".to_string()))?;

    // Check role permission
    if !user.role_enum().can_change_password() {
        return Err(AppError::Forbidden(
            "You don't have permission to change passwords".to_string(),
        ));
    }

    // Verify current password
    if !auth::verify_password(&req.current_password, &user.password_hash) {
        return Err(AppError::BadRequest(
            "Current password is incorrect".to_string(),
        ));
    }

    // Validate new password
    if req.new_password.len() < 8 {
        return Err(AppError::BadRequest(
            "New password must be at least 8 characters".to_string(),
        ));
    }

    // Hash and update password, clearing any forced-change flag
    let new_hash = auth::hash_password(&req.new_password)?;
    sqlx::query("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?")
        .bind(&new_hash)
        .bind(user.id)
        .execute(&state.db)
        .await?;

    Ok(Json(serde_json::json!({ "success": true })))
}

#[derive(Debug, Deserialize)]
pub struct SetInitialPasswordRequest {
    new_password: String,
}

/// Set a self-chosen password on first login, replacing the admin-generated
/// bootstrap password. Unlike `api_change_password`, this does not require the
/// current password — the session proves identity — but it is only usable while
/// `must_change_password` is set, so it cannot bypass verification afterwards.
pub async fn api_set_initial_password(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(req): Json<SetInitialPasswordRequest>,
) -> Result<impl IntoResponse> {
    let token = auth::get_session_from_headers(&headers);
    let user = auth::get_current_user(&state, token.as_deref())
        .await
        .ok_or_else(|| AppError::Unauthorized("Not authenticated".to_string()))?;

    // Only usable while a forced password change is pending.
    if !user.must_change_password {
        return Err(AppError::Forbidden(
            "Password change is not required for this account".to_string(),
        ));
    }

    // Validate new password
    if req.new_password.len() < 8 {
        return Err(AppError::BadRequest(
            "New password must be at least 8 characters".to_string(),
        ));
    }

    // Hash and update password, clearing the forced-change flag
    let new_hash = auth::hash_password(&req.new_password)?;
    sqlx::query("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?")
        .bind(&new_hash)
        .bind(user.id)
        .execute(&state.db)
        .await?;

    Ok(Json(serde_json::json!({ "success": true })))
}

// ============================================================================
// Artists API
// ============================================================================

#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct ArtistListItem {
    pub id: i64,
    pub name: String,
    pub status: String,
    pub created_at: String,
    pub show_titles: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct ArtistsListResponse {
    artists: Vec<ArtistListItem>,
    total: usize,
}

#[derive(Debug, Deserialize)]
pub struct ArtistsQuery {
    filter: Option<String>,
    sort: Option<String>,
    dir: Option<String>,
    /// When true, return only artists not linked to any user account
    unlinked: Option<bool>,
}

pub async fn api_artists_list(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    axum::extract::Query(query): axum::extract::Query<ArtistsQuery>,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    let assignment_filter = query
        .filter
        .filter(|s| !s.is_empty())
        .and_then(|s| match s.as_str() {
            "assigned" | "unassigned" => Some(s),
            _ => None,
        });

    let sort = query.sort.as_deref().unwrap_or("submitted");
    let dir = query.dir.as_deref().unwrap_or("desc");
    let dir = if dir.eq_ignore_ascii_case("asc") {
        "ASC"
    } else {
        "DESC"
    };
    let order_by = match sort {
        "name" => "a.name COLLATE NOCASE",
        "status" => "CASE WHEN asa.show_id IS NULL THEN 0 ELSE 1 END",
        "submitted" => "a.created_at",
        _ => "a.created_at",
    };

    let mut where_clauses: Vec<&str> = Vec::new();
    if let Some(af) = assignment_filter.as_deref() {
        match af {
            "assigned" => where_clauses.push("asa.show_id IS NOT NULL"),
            "unassigned" => where_clauses.push("asa.show_id IS NULL"),
            _ => {}
        }
    }
    if query.unlinked == Some(true) {
        where_clauses.push("a.user_id IS NULL");
    }

    let where_sql = if where_clauses.is_empty() {
        String::new()
    } else {
        format!("WHERE {}", where_clauses.join(" AND "))
    };

    let query_str = format!(
        r#"
        SELECT
            a.id,
            a.name,
            CASE WHEN asa.show_id IS NULL THEN 'unassigned' ELSE 'assigned' END AS status,
            a.created_at,
            group_concat(s.title, ', ') AS show_titles
        FROM artists a
        LEFT JOIN artist_show_assignments asa ON asa.artist_id = a.id
        LEFT JOIN shows s ON s.id = asa.show_id
        {}
        GROUP BY a.id
        ORDER BY {} {}, a.id DESC
        "#,
        where_sql, order_by, dir
    );

    let artists: Vec<ArtistListItem> = sqlx::query_as(&query_str).fetch_all(&state.db).await?;

    let total = artists.len();

    Ok(Json(ArtistsListResponse { artists, total }))
}

#[derive(Debug, Serialize)]
pub struct ShowBrief {
    id: i64,
    title: String,
    date: String,
    cover_url: Option<String>,
    cover_generated_at: Option<String>,
}

#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct AvailableShow {
    id: i64,
    title: String,
    date: String,
    artists_left: i64,
}

#[derive(Debug, Serialize)]
pub struct ArtistDetailResponse {
    id: i64,
    name: String,
    pronouns: String,
    status: String,
    created_at: String,
    mentions: Option<String>,
    upcoming_events: Option<String>,
    music_description: Option<String>,
    ai_bio: Option<String>,
    instagram_caption: Option<String>,
    instagram_posted_at: Option<String>,
    soundcloud: Option<String>,
    instagram: Option<String>,
    bandcamp: Option<String>,
    spotify: Option<String>,
    other_social: Option<String>,
    track1_name: String,
    track2_name: String,
    file_urls: std::collections::HashMap<String, String>,
    shows: Vec<ShowBrief>,
    available_shows: Vec<AvailableShow>,
    active_overlay_preset_id: Option<i64>,
}

pub async fn api_artist_detail(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    let artist: Option<models::Artist> = sqlx::query_as("SELECT * FROM artists WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?;

    let artist = artist.ok_or_else(|| AppError::NotFound("Artist not found".to_string()))?;

    // Get shows for this artist
    let shows: Vec<models::Show> = sqlx::query_as(
        r#"
        SELECT s.* FROM shows s
        INNER JOIN artist_show_assignments asa ON s.id = asa.show_id
        WHERE asa.artist_id = ?
        ORDER BY s.date DESC
        "#,
    )
    .bind(id)
    .fetch_all(&state.db)
    .await?;

    // Generate presigned URLs for files
    let mut file_urls = std::collections::HashMap::new();
    let pic_key = artist
        .pic_overlay_key
        .as_ref()
        .or(artist.pic_cropped_key.as_ref())
        .or(artist.pic_key.as_ref());
    if let Some(key) = pic_key {
        if let Ok(url) = storage::get_presigned_url(&state, key, 3600).await {
            file_urls.insert("pic".to_string(), url);
        }
    }
    // Expose the untouched original image separately (for overlay editor)
    if let Some(key) = &artist.pic_key {
        if let Ok(url) = storage::get_presigned_url(&state, key, 3600).await {
            file_urls.insert("pic_original".to_string(), url);
        }
    }
    if let Some(key) = &artist.voice_message_key {
        if let Ok(url) = storage::get_presigned_url(&state, key, 3600).await {
            file_urls.insert("voice".to_string(), url);
        }
        // Try to get peaks
        let peaks_key = derive_peaks_key(key);
        if let Ok(url) = storage::get_presigned_url(&state, &peaks_key, 3600).await {
            file_urls.insert("voice_peaks".to_string(), url);
        }
    }
    if let Some(key) = &artist.track1_key {
        if let Ok(url) = storage::get_presigned_url(&state, key, 3600).await {
            file_urls.insert("track1".to_string(), url);
        }
        // Try to get peaks
        let peaks_key = derive_peaks_key(key);
        if let Ok(url) = storage::get_presigned_url(&state, &peaks_key, 3600).await {
            file_urls.insert("track1_peaks".to_string(), url);
        }
    }
    if let Some(key) = &artist.track2_key {
        if let Ok(url) = storage::get_presigned_url(&state, key, 3600).await {
            file_urls.insert("track2".to_string(), url);
        }
        // Try to get peaks
        let peaks_key = derive_peaks_key(key);
        if let Ok(url) = storage::get_presigned_url(&state, &peaks_key, 3600).await {
            file_urls.insert("track2_peaks".to_string(), url);
        }
    }
    if let Some(key) = &artist.track1_video_key {
        if let Ok(url) = storage::get_presigned_url(&state, key, 3600).await {
            file_urls.insert("track1_video".to_string(), url);
        }
    }
    if let Some(key) = &artist.track2_video_key {
        if let Ok(url) = storage::get_presigned_url(&state, key, 3600).await {
            file_urls.insert("track2_video".to_string(), url);
        }
    }

    // Get available shows for assignment
    let available_shows: Vec<AvailableShow> = sqlx::query_as(
        r#"
        SELECT
            s.id,
            s.title,
            s.date,
            (? - COUNT(asa.artist_id)) AS artists_left
        FROM shows s
        LEFT JOIN artist_show_assignments asa ON asa.show_id = s.id
        WHERE s.date >= date('now')
          AND s.id NOT IN (
              SELECT show_id FROM artist_show_assignments WHERE artist_id = ?
          )
        GROUP BY s.id
        HAVING COUNT(asa.artist_id) < ?
        ORDER BY s.date ASC
        "#,
    )
    .bind(MAX_ARTISTS_PER_SHOW)
    .bind(id)
    .bind(MAX_ARTISTS_PER_SHOW)
    .fetch_all(&state.db)
    .await?;

    // Determine status
    let status = if shows.is_empty() {
        "unassigned".to_string()
    } else {
        "assigned".to_string()
    };

    Ok(Json(ArtistDetailResponse {
        id: artist.id,
        name: artist.name,
        pronouns: artist.pronouns,
        status,
        created_at: artist.created_at,
        mentions: artist.mentions,
        upcoming_events: artist.upcoming_events,
        music_description: artist.music_description,
        ai_bio: artist.ai_bio,
        instagram_caption: artist.instagram_caption,
        instagram_posted_at: artist.instagram_posted_at,
        soundcloud: artist.soundcloud,
        instagram: artist.instagram,
        bandcamp: artist.bandcamp,
        spotify: artist.spotify,
        other_social: artist.other_social,
        track1_name: artist.track1_name,
        track2_name: artist.track2_name,
        file_urls,
        shows: {
            let mut show_briefs = Vec::new();
            for s in shows {
                let cover_url = if s.cover_generated_at.is_some() {
                    let cover_key = format!("shows/{}/cover.png", s.id);
                    storage::get_presigned_url(&state, &cover_key, 3600)
                        .await
                        .ok()
                } else {
                    None
                };
                show_briefs.push(ShowBrief {
                    id: s.id,
                    title: s.title,
                    date: s.date,
                    cover_url,
                    cover_generated_at: s.cover_generated_at,
                });
            }
            show_briefs
        },
        available_shows,
        active_overlay_preset_id: artist.active_overlay_preset_id,
    }))
}

pub async fn api_delete_artist(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    // Delete artist (cascades to assignments)
    sqlx::query("DELETE FROM artists WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await?;

    Ok(Json(serde_json::json!({ "success": true })))
}

/// Manually regenerate waveform preview videos for an artist.
/// Runs synchronously so the admin can confirm success before previewing.
pub async fn api_generate_artist_videos(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    // Validate artist exists and has required assets
    let artist: models::Artist = sqlx::query_as("SELECT * FROM artists WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound(format!("Artist {id} not found")))?;

    let has_image = artist.pic_overlay_key.is_some()
        || artist.pic_cropped_key.is_some()
        || artist.pic_key.is_some();
    let has_tracks = artist.track1_key.is_some() || artist.track2_key.is_some();

    if !has_image || !has_tracks {
        return Err(AppError::BadRequest(
            "Artist must have a picture and at least one track to generate videos".to_string(),
        ));
    }

    // Run synchronously — admin waits for completion
    video::generate_and_store_artist_videos(state.clone(), id).await?;

    // Reload artist to get new video keys
    let updated: models::Artist = sqlx::query_as("SELECT * FROM artists WHERE id = ?")
        .bind(id)
        .fetch_one(&state.db)
        .await?;

    Ok(Json(serde_json::json!({
        "success": true,
        "track1_video_key": updated.track1_video_key,
        "track2_video_key": updated.track2_video_key,
    })))
}

#[derive(Debug, Deserialize)]
pub struct AssignShowRequest {
    show_id: i64,
}

pub async fn api_assign_artist_to_show(
    State(state): State<Arc<AppState>>,
    Path(artist_id): Path<i64>,
    headers: HeaderMap,
    Json(req): Json<AssignShowRequest>,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    // Fetch artist name for notification
    let artist_name: String = sqlx::query_scalar("SELECT name FROM artists WHERE id = ?")
        .bind(artist_id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound(format!("Artist {artist_id} not found")))?;

    sqlx::query(
        "INSERT OR IGNORE INTO artist_show_assignments (artist_id, show_id, sort_order) \
         VALUES (?, ?, (SELECT COALESCE(MAX(sort_order), -1) + 1 FROM artist_show_assignments WHERE show_id = ?))",
    )
    .bind(artist_id)
    .bind(req.show_id)
    .bind(req.show_id)
    .execute(&state.db)
    .await?;

    // Regenerate show cover image with the new artist
    let state_for_bio = state.clone();
    let show_id = req.show_id;
    schedule_cover_regeneration(state.clone(), req.show_id);

    // Generate AI show bio from assigned artists' bios (background)
    tokio::spawn(async move {
        if let Err(e) = ai::generate_and_store_show_bio(&state_for_bio, show_id).await {
            tracing::error!("Failed to generate show bio for show {}: {}", show_id, e);
        }
    });

    // Schedule Telegram notification (30-second debounced)
    telegram_notify::schedule_show_update_notification(
        &state,
        req.show_id,
        artist_name,
        telegram_notify::ShowUpdateAction::Added,
    );

    Ok(Json(serde_json::json!({ "success": true })))
}

pub async fn api_unassign_artist_from_show(
    State(state): State<Arc<AppState>>,
    Path((artist_id, show_id)): Path<(i64, i64)>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    // Fetch artist name for notification
    let artist_name: String = sqlx::query_scalar("SELECT name FROM artists WHERE id = ?")
        .bind(artist_id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound(format!("Artist {artist_id} not found")))?;

    sqlx::query("DELETE FROM artist_show_assignments WHERE artist_id = ? AND show_id = ?")
        .bind(artist_id)
        .bind(show_id)
        .execute(&state.db)
        .await?;

    // Regenerate show cover image without the removed artist
    let state_for_bio = state.clone();
    schedule_cover_regeneration(state.clone(), show_id);

    // Regenerate AI show bio from remaining artists' bios (background)
    tokio::spawn(async move {
        if let Err(e) = ai::generate_and_store_show_bio(&state_for_bio, show_id).await {
            tracing::error!("Failed to generate show bio for show {}: {}", show_id, e);
        }
    });

    // Schedule Telegram notification (30-second debounced)
    telegram_notify::schedule_show_update_notification(
        &state,
        show_id,
        artist_name,
        telegram_notify::ShowUpdateAction::Removed,
    );

    Ok(Json(serde_json::json!({ "success": true })))
}

// ============================================================================
// Artist Active Overlay Preset
// ============================================================================

#[derive(Debug, Deserialize)]
pub struct SetActivePresetRequest {
    preset_id: Option<i64>,
}

pub async fn api_set_artist_active_preset(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    Json(req): Json<SetActivePresetRequest>,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    // Verify artist exists
    let artist_exists: bool = sqlx::query_scalar("SELECT COUNT(*) > 0 FROM artists WHERE id = ?")
        .bind(id)
        .fetch_one(&state.db)
        .await?;
    if !artist_exists {
        return Err(AppError::NotFound("Artist not found".to_string()));
    }

    // Validate preset exists if setting one
    if let Some(preset_id) = req.preset_id {
        let preset_exists: bool =
            sqlx::query_scalar("SELECT COUNT(*) > 0 FROM overlay_presets WHERE id = ?")
                .bind(preset_id)
                .fetch_one(&state.db)
                .await?;
        if !preset_exists {
            return Err(AppError::BadRequest("Overlay preset not found".to_string()));
        }
    }

    sqlx::query(
        "UPDATE artists SET active_overlay_preset_id = ?, updated_at = datetime('now') WHERE id = ?",
    )
    .bind(req.preset_id)
    .bind(id)
    .execute(&state.db)
    .await?;

    Ok(Json(serde_json::json!({ "success": true })))
}

// ============================================================================
// Artist Update API
// ============================================================================

#[derive(Debug, Deserialize)]
pub struct UpdateArtistDetailsRequest {
    mentions: Option<String>,
    upcoming_events: Option<String>,
    music_description: Option<String>,
    soundcloud: Option<String>,
    instagram: Option<String>,
    bandcamp: Option<String>,
    spotify: Option<String>,
    other_social: Option<String>,
}

pub async fn api_update_artist_details(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    Json(req): Json<UpdateArtistDetailsRequest>,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    sqlx::query(
        r#"
        UPDATE artists SET
            mentions = ?,
            upcoming_events = ?,
            music_description = ?,
            soundcloud = ?,
            instagram = ?,
            bandcamp = ?,
            spotify = ?,
            other_social = ?,
            updated_at = datetime('now')
        WHERE id = ?
        "#,
    )
    .bind(&req.mentions)
    .bind(&req.upcoming_events)
    .bind(&req.music_description)
    .bind(&req.soundcloud)
    .bind(&req.instagram)
    .bind(&req.bandcamp)
    .bind(&req.spotify)
    .bind(&req.other_social)
    .bind(id)
    .execute(&state.db)
    .await?;

    // Auto-generate AI bio when music_description is provided
    if req
        .music_description
        .as_ref()
        .is_some_and(|d| !d.is_empty())
    {
        let ai_state = state.clone();
        tokio::spawn(async move {
            match crate::ai::generate_and_store_artist_bio(&ai_state, id).await {
                Ok(bio) => tracing::info!(
                    id,
                    len = bio.len(),
                    "AI bio auto-generated on details update"
                ),
                Err(e) => tracing::error!(id, "Background AI bio generation failed: {e:#}"),
            }
        });
    }

    Ok(Json(serde_json::json!({ "success": true })))
}

// ============================================================================
// Artist AI Bio Generation
// ============================================================================

pub async fn api_generate_artist_bio(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    let bio = crate::ai::generate_and_store_artist_bio(&state, id).await?;

    Ok(Json(serde_json::json!({ "success": true, "ai_bio": bio })))
}

// ============================================================================
// Show AI Bio Regeneration
// ============================================================================

pub async fn api_regenerate_show_bio(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    let bio = crate::ai::generate_and_store_show_bio(&state, id).await?;

    Ok(Json(serde_json::json!({ "success": true, "ai_bio": bio })))
}

// ============================================================================
// Artist Instagram Caption Generation
// ============================================================================

pub async fn api_generate_instagram_caption(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    let caption = crate::ai::generate_and_store_instagram_caption(&state, id).await?;

    Ok(Json(
        serde_json::json!({ "success": true, "instagram_caption": caption }),
    ))
}

// ============================================================================
// Artist Instagram Caption Update
// ============================================================================

pub async fn api_update_instagram_caption(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    Json(req): Json<serde_json::Value>,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    let caption = req["instagram_caption"]
        .as_str()
        .ok_or_else(|| AppError::BadRequest("instagram_caption is required".to_string()))?;

    sqlx::query(
        "UPDATE artists SET instagram_caption = ?, updated_at = datetime('now') WHERE id = ?",
    )
    .bind(caption)
    .bind(id)
    .execute(&state.db)
    .await?;

    Ok(Json(
        serde_json::json!({ "success": true, "instagram_caption": caption }),
    ))
}

// ============================================================================
// Artist Instagram Posting
// ============================================================================

pub async fn api_post_artist_to_instagram(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    Json(req): Json<InstagramPostRequest>,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    let artist: models::Artist = sqlx::query_as("SELECT * FROM artists WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound("Artist not found".to_string()))?;

    // Check if already posted (unless force=true)
    if artist.instagram_posted_at.is_some() && !req.force {
        return Ok(Json(InstagramPostResponse {
            success: false,
            media_id: None,
            permalink: None,
            error: Some(
                "This artist was already posted to Instagram. Use force=true to post again."
                    .to_string(),
            ),
            already_posted: true,
        }));
    }

    // Post to Instagram
    let result = crate::instagram::post_artist_to_instagram(&state, &artist, &req.account).await?;

    if result.success {
        // Update instagram_posted_at timestamp
        sqlx::query("UPDATE artists SET instagram_posted_at = datetime('now'), updated_at = datetime('now') WHERE id = ?")
            .bind(id)
            .execute(&state.db)
            .await?;

        tracing::info!(
            "Posted artist {} to Instagram (account={}): {:?}",
            id,
            req.account,
            result.media_id
        );
    }

    Ok(Json(InstagramPostResponse {
        success: result.success,
        media_id: result.media_id,
        permalink: result.permalink,
        error: result.error,
        already_posted: false,
    }))
}

/// POST /api/artists/:id/telegram-preview — Send artist preview to Telegram
pub async fn api_send_artist_telegram_preview(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    let artist: models::Artist = sqlx::query_as("SELECT * FROM artists WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound("Artist not found".to_string()))?;

    // Require instagram_caption
    if artist.instagram_caption.is_none() {
        return Err(AppError::BadRequest(
            "Artist has no Instagram caption. Generate one first.".to_string(),
        ));
    }

    crate::telegram_notify::send_artist_instagram_preview(&state, &artist)
        .await
        .map_err(|e| AppError::Internal(format!("Telegram preview failed: {e}")))?;

    Ok(Json(serde_json::json!({ "success": true })))
}

pub async fn api_update_artist_picture(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    mut multipart: axum::extract::Multipart,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    // Get artist name for file naming
    let artist: Option<models::Artist> = sqlx::query_as("SELECT * FROM artists WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?;
    let artist = artist.ok_or_else(|| AppError::NotFound("Artist not found".to_string()))?;

    let mut new_pic_key: Option<String> = None;
    let mut new_pic_cropped_key: Option<String> = None;
    let mut new_pic_overlay_key: Option<String> = None;

    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|e| AppError::BadRequest(format!("Failed to read multipart field: {}", e)))?
    {
        let field_name = field.name().unwrap_or("").to_string();
        let filename = field.file_name().unwrap_or("image.jpg").to_string();
        let content_type = field.content_type().unwrap_or("image/jpeg").to_string();
        let data = field
            .bytes()
            .await
            .map_err(|e| AppError::BadRequest(format!("Failed to read file data: {}", e)))?;

        if data.is_empty() {
            continue;
        }

        match field_name.as_str() {
            "original" => {
                let key = storage::upload_file_named(
                    &state,
                    id,
                    "pic",
                    &artist.name,
                    &filename,
                    data.to_vec(),
                    &content_type,
                )
                .await?;
                new_pic_key = Some(key);
            }
            "cropped" => {
                let key = storage::upload_file_named(
                    &state,
                    id,
                    "pic-cropped",
                    &format!("{}-cropped", artist.name),
                    &filename,
                    data.to_vec(),
                    &content_type,
                )
                .await?;
                new_pic_cropped_key = Some(key);
            }
            "branded" => {
                let key = storage::upload_file_named(
                    &state,
                    id,
                    "pic-overlay",
                    &format!("{}-overlay", artist.name),
                    &filename,
                    data.to_vec(),
                    &content_type,
                )
                .await?;
                new_pic_overlay_key = Some(key);
            }
            _ => {}
        }
    }

    // Update database with all three keys
    if new_pic_key.is_some() || new_pic_cropped_key.is_some() || new_pic_overlay_key.is_some() {
        sqlx::query(
            r#"
            UPDATE artists SET
                pic_key = COALESCE(?, pic_key),
                pic_cropped_key = COALESCE(?, pic_cropped_key),
                pic_overlay_key = COALESCE(?, pic_overlay_key),
                updated_at = datetime('now')
            WHERE id = ?
            "#,
        )
        .bind(&new_pic_key)
        .bind(&new_pic_cropped_key)
        .bind(&new_pic_overlay_key)
        .bind(id)
        .execute(&state.db)
        .await?;

        // Regenerate waveform preview videos with the new picture
        let video_state = state.clone();
        tokio::spawn(async move {
            if let Err(e) = video::generate_and_store_artist_videos(video_state, id).await {
                tracing::error!(
                    artist_id = id,
                    "Video regeneration after picture update failed: {e:#}"
                );
            }
        });

        // Trigger backup workflow (fire-and-forget)
        super::backup_trigger::trigger_backup(&state.config, id, "profile-picture-update");
    }

    Ok(Json(serde_json::json!({ "success": true })))
}

// ============================================================================
// Overlay Presets API
// ============================================================================

#[derive(Debug, Deserialize)]
pub struct CreatePresetRequest {
    name: String,
    params: serde_json::Value,
    /// 'artist' or 'show' — defaults to 'artist' when omitted
    preset_type: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct UpdatePresetRequest {
    name: Option<String>,
    params: Option<serde_json::Value>,
}

/// GET /api/overlay-presets?type=artist|show  (optional filter)
pub async fn api_list_overlay_presets(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Query(qs): Query<std::collections::HashMap<String, String>>,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    let presets: Vec<models::OverlayPreset> = if let Some(ptype) = qs.get("type") {
        sqlx::query_as("SELECT * FROM overlay_presets WHERE preset_type = ? ORDER BY name ASC")
            .bind(ptype)
            .fetch_all(&state.db)
            .await?
    } else {
        sqlx::query_as("SELECT * FROM overlay_presets ORDER BY name ASC")
            .fetch_all(&state.db)
            .await?
    };

    // Parse the params JSON string back into serde_json::Value for the response
    let items: Vec<serde_json::Value> = presets
        .iter()
        .map(|p| {
            let params_val: serde_json::Value =
                serde_json::from_str(&p.params).unwrap_or(serde_json::Value::Null);
            serde_json::json!({
                "id": p.id,
                "name": p.name,
                "params": params_val,
                "preset_type": p.preset_type,
                "created_at": p.created_at,
                "updated_at": p.updated_at,
            })
        })
        .collect();

    Ok(Json(serde_json::json!({ "presets": items })))
}

pub async fn api_create_overlay_preset(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(req): Json<CreatePresetRequest>,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    if req.name.trim().is_empty() {
        return Err(AppError::BadRequest("Preset name is required".to_string()));
    }

    let params_str = serde_json::to_string(&req.params)
        .map_err(|e| AppError::BadRequest(format!("Invalid params JSON: {}", e)))?;

    let preset_type = req.preset_type.as_deref().unwrap_or("artist");
    if preset_type != "artist" && preset_type != "show" {
        return Err(AppError::BadRequest(
            "preset_type must be 'artist' or 'show'".to_string(),
        ));
    }

    let result =
        sqlx::query("INSERT INTO overlay_presets (name, params, preset_type) VALUES (?, ?, ?)")
            .bind(req.name.trim())
            .bind(&params_str)
            .bind(preset_type)
            .execute(&state.db)
            .await
            .map_err(|e| match e {
                sqlx::Error::Database(ref db_err) if db_err.message().contains("UNIQUE") => {
                    AppError::BadRequest("A preset with that name already exists".to_string())
                }
                other => AppError::Database(other),
            })?;

    let id = result.last_insert_rowid();
    let params_val: serde_json::Value =
        serde_json::from_str(&params_str).unwrap_or(serde_json::Value::Null);

    // Fetch created_at/updated_at from DB
    let created: Option<models::OverlayPreset> =
        sqlx::query_as("SELECT * FROM overlay_presets WHERE id = ?")
            .bind(id)
            .fetch_optional(&state.db)
            .await?;
    let ts = created.as_ref();

    Ok((
        StatusCode::CREATED,
        Json(serde_json::json!({
            "id": id,
            "name": req.name.trim(),
            "params": params_val,
            "preset_type": preset_type,
            "created_at": ts.map(|p| &p.created_at),
            "updated_at": ts.map(|p| &p.updated_at),
        })),
    ))
}

pub async fn api_update_overlay_preset(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    Json(req): Json<UpdatePresetRequest>,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    // Verify preset exists
    let existing: Option<models::OverlayPreset> =
        sqlx::query_as("SELECT * FROM overlay_presets WHERE id = ?")
            .bind(id)
            .fetch_optional(&state.db)
            .await?;
    let existing = existing.ok_or_else(|| AppError::NotFound("Preset not found".to_string()))?;

    let new_name = req
        .name
        .as_deref()
        .map(|n| n.trim())
        .unwrap_or(&existing.name);
    let new_params = match &req.params {
        Some(p) => serde_json::to_string(p)
            .map_err(|e| AppError::BadRequest(format!("Invalid params JSON: {}", e)))?,
        None => existing.params.clone(),
    };

    sqlx::query(
        "UPDATE overlay_presets SET name = ?, params = ?, updated_at = datetime('now') WHERE id = ?",
    )
    .bind(new_name)
    .bind(&new_params)
    .bind(id)
    .execute(&state.db)
    .await
    .map_err(|e| match e {
        sqlx::Error::Database(ref db_err) if db_err.message().contains("UNIQUE") => {
            AppError::BadRequest("A preset with that name already exists".to_string())
        }
        other => AppError::Database(other),
    })?;

    // Return the full updated preset
    let updated: Option<models::OverlayPreset> =
        sqlx::query_as("SELECT * FROM overlay_presets WHERE id = ?")
            .bind(id)
            .fetch_optional(&state.db)
            .await?;
    let updated =
        updated.ok_or_else(|| AppError::NotFound("Preset not found after update".to_string()))?;
    let params_val: serde_json::Value =
        serde_json::from_str(&updated.params).unwrap_or(serde_json::Value::Null);

    Ok(Json(serde_json::json!({
        "id": updated.id,
        "name": updated.name,
        "params": params_val,
        "preset_type": updated.preset_type,
        "created_at": updated.created_at,
        "updated_at": updated.updated_at,
    })))
}

pub async fn api_delete_overlay_preset(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    let result = sqlx::query("DELETE FROM overlay_presets WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await?;

    if result.rows_affected() == 0 {
        return Err(AppError::NotFound("Preset not found".to_string()));
    }

    Ok(Json(serde_json::json!({ "success": true })))
}

// ============================================================================
// Image Proxy (serves R2 images to avoid CORS issues with presigned URLs)
// ============================================================================

/// Proxy an artist image from R2 so the frontend can load it from same-origin.
/// GET /api/artists/:id/image-proxy?type=original|cropped|overlay
pub async fn api_artist_image_proxy(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    Query(params): Query<std::collections::HashMap<String, String>>,
) -> Result<impl IntoResponse> {
    let image_type = params.get("type").map(|s| s.as_str()).unwrap_or("original");

    let artist: Option<models::Artist> = sqlx::query_as("SELECT * FROM artists WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?;

    let artist = artist.ok_or_else(|| AppError::NotFound("Artist not found".to_string()))?;

    let key = match image_type {
        "original" => artist.pic_key.clone(),
        "cropped" => artist.pic_cropped_key.clone().or(artist.pic_key.clone()),
        "overlay" => artist
            .pic_overlay_key
            .clone()
            .or(artist.pic_cropped_key.clone())
            .or(artist.pic_key.clone()),
        _ => artist.pic_key.clone(),
    };

    let key =
        key.ok_or_else(|| AppError::NotFound("No image found for this artist".to_string()))?;

    let (data, content_type) = storage::download_file(&state, &key).await?;

    Ok((
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, content_type),
            (header::CACHE_CONTROL, "private, max-age=300".to_string()),
        ],
        data,
    ))
}

/// Proxy a show cover image from R2 so the frontend can load it from same-origin.
/// GET /api/shows/:id/image-proxy?type=cover|collage
///
/// `type` defaults to `cover` for backwards compatibility.
/// `collage` serves the plain 2×2 image without overlay — used by the overlay editor.
pub async fn api_show_image_proxy(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    Query(params): Query<std::collections::HashMap<String, String>>,
) -> Result<impl IntoResponse> {
    let show: Option<models::Show> = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?;

    let _show = show.ok_or_else(|| AppError::NotFound("Show not found".to_string()))?;

    let image_type = params.get("type").map(|s| s.as_str()).unwrap_or("cover");
    let key = match image_type {
        "collage" => format!("shows/{}/collage.png", id),
        _ => format!("shows/{}/cover.png", id),
    };

    let (data, content_type) = storage::download_file(&state, &key).await?;

    Ok((
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, content_type),
            (header::CACHE_CONTROL, "private, max-age=300".to_string()),
        ],
        data,
    ))
}

/// Save an overlay image for a show to R2.
/// POST /api/shows/:id/overlays
pub async fn api_save_show_overlay(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    mut multipart: axum::extract::Multipart,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    // Verify show exists
    let _show: models::Show = sqlx::query_as::<_, models::Show>("SELECT * FROM shows WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound("Show not found".to_string()))?;

    let mut image_data: Option<Vec<u8>> = None;
    let mut content_type = "image/jpeg".to_string();

    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|e| AppError::BadRequest(format!("Failed to read multipart field: {}", e)))?
    {
        let field_name = field.name().unwrap_or("").to_string();
        if field_name == "image" {
            content_type = field.content_type().unwrap_or("image/jpeg").to_string();
            let data = field
                .bytes()
                .await
                .map_err(|e| AppError::BadRequest(format!("Failed to read image data: {}", e)))?;
            if !data.is_empty() {
                image_data = Some(data.to_vec());
            }
        }
    }

    let data = image_data.ok_or_else(|| AppError::BadRequest("No image provided".to_string()))?;

    let timestamp = chrono::Utc::now().format("%Y%m%d-%H%M%S");
    let uuid_short = &uuid::Uuid::new_v4().to_string()[..8];
    let ext = if content_type.contains("png") {
        "png"
    } else {
        "jpg"
    };
    let key = format!("shows/{}/overlay/{}-{}.{}", id, timestamp, uuid_short, ext);

    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&key)
        .body(aws_sdk_s3::primitives::ByteStream::from(data))
        .content_type(&content_type)
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload show overlay: {}", e)))?;

    let url = storage::get_presigned_url(&state, &key, 3600).await?;

    Ok(Json(serde_json::json!({
        "key": key,
        "url": url,
    })))
}

/// List all overlay images saved for a show in R2.
/// GET /api/shows/:id/overlays
pub async fn api_list_show_overlays(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    let _show: models::Show = sqlx::query_as::<_, models::Show>("SELECT * FROM shows WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound("Show not found".to_string()))?;

    let prefix = format!("shows/{}/overlay/", id);
    let objects = storage::list_objects(&state, &prefix).await?;

    let mut items = Vec::new();
    for obj in &objects {
        let url = storage::get_presigned_url(&state, &obj.key, 3600)
            .await
            .unwrap_or_default();
        items.push(serde_json::json!({
            "key": obj.key,
            "url": url,
            "last_modified": obj.last_modified,
            "size": obj.size,
        }));
    }

    Ok(Json(serde_json::json!({
        "overlays": items,
        "active_key": serde_json::Value::Null,
    })))
}

// ============================================================================
// Overlay Gallery API
// ============================================================================

/// List all overlay images saved for an artist in R2 (pic_overlay/ prefix).
pub async fn api_list_artist_overlays(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    // Verify artist exists
    let artist: Option<models::Artist> = sqlx::query_as("SELECT * FROM artists WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?;
    let artist = artist.ok_or_else(|| AppError::NotFound("Artist not found".to_string()))?;

    let prefix = format!("artists/{}/pic_overlay/", id);
    let objects = storage::list_objects(&state, &prefix).await?;

    let mut items = Vec::new();
    for obj in &objects {
        let url = storage::get_presigned_url(&state, &obj.key, 3600)
            .await
            .unwrap_or_default();
        items.push(serde_json::json!({
            "key": obj.key,
            "url": url,
            "last_modified": obj.last_modified,
            "size": obj.size,
        }));
    }

    // Also include the legacy pic-overlay/ directory (used by the admin picture update)
    let legacy_prefix = format!("artists/{}/pic-overlay/", id);
    let legacy_objects = storage::list_objects(&state, &legacy_prefix).await?;
    for obj in &legacy_objects {
        let url = storage::get_presigned_url(&state, &obj.key, 3600)
            .await
            .unwrap_or_default();
        items.push(serde_json::json!({
            "key": obj.key,
            "url": url,
            "last_modified": obj.last_modified,
            "size": obj.size,
        }));
    }

    Ok(Json(serde_json::json!({
        "artist_id": id,
        "artist_name": artist.name,
        "active_key": artist.pic_overlay_key,
        "overlays": items,
    })))
}

/// Save a new overlay image to R2 without updating the active overlay key.
pub async fn api_save_artist_overlay(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    mut multipart: axum::extract::Multipart,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    // Verify artist exists
    let _artist: models::Artist =
        sqlx::query_as::<_, models::Artist>("SELECT * FROM artists WHERE id = ?")
            .bind(id)
            .fetch_optional(&state.db)
            .await?
            .ok_or_else(|| AppError::NotFound("Artist not found".to_string()))?;

    let mut image_data: Option<Vec<u8>> = None;
    let mut content_type = "image/jpeg".to_string();

    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|e| AppError::BadRequest(format!("Failed to read multipart field: {}", e)))?
    {
        let field_name = field.name().unwrap_or("").to_string();
        if field_name == "image" {
            content_type = field.content_type().unwrap_or("image/jpeg").to_string();
            let data = field
                .bytes()
                .await
                .map_err(|e| AppError::BadRequest(format!("Failed to read image data: {}", e)))?;
            if !data.is_empty() {
                image_data = Some(data.to_vec());
            }
        }
    }

    let data = image_data.ok_or_else(|| AppError::BadRequest("No image provided".to_string()))?;

    // Generate a timestamped unique filename so older images are never replaced
    let timestamp = chrono::Utc::now().format("%Y%m%d-%H%M%S");
    let uuid_short = &uuid::Uuid::new_v4().to_string()[..8];
    let ext = if content_type.contains("png") {
        "png"
    } else {
        "jpg"
    };
    let key = format!(
        "artists/{}/pic_overlay/{}-{}.{}",
        id, timestamp, uuid_short, ext
    );

    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&key)
        .body(aws_sdk_s3::primitives::ByteStream::from(data))
        .content_type(&content_type)
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload overlay: {}", e)))?;

    let url = storage::get_presigned_url(&state, &key, 3600).await?;

    Ok(Json(serde_json::json!({
        "key": key,
        "url": url,
    })))
}

#[derive(Debug, Deserialize)]
pub struct SetActiveOverlayRequest {
    key: String,
}

/// Set a specific overlay image as the active pic_overlay_key for the artist.
pub async fn api_set_active_overlay(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    Json(req): Json<SetActiveOverlayRequest>,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    // Verify artist exists
    let _artist: models::Artist =
        sqlx::query_as::<_, models::Artist>("SELECT * FROM artists WHERE id = ?")
            .bind(id)
            .fetch_optional(&state.db)
            .await?
            .ok_or_else(|| AppError::NotFound("Artist not found".to_string()))?;

    // Verify the key actually belongs to this artist (security check)
    let expected_prefix = format!("artists/{}/", id);
    if !req.key.starts_with(&expected_prefix) {
        return Err(AppError::BadRequest(
            "Invalid overlay key for this artist".to_string(),
        ));
    }

    sqlx::query(
        "UPDATE artists SET pic_overlay_key = ?, updated_at = datetime('now') WHERE id = ?",
    )
    .bind(&req.key)
    .bind(id)
    .execute(&state.db)
    .await?;

    Ok(Json(serde_json::json!({ "success": true })))
}

pub async fn api_update_artist_audio(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    mut multipart: axum::extract::Multipart,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    // Get artist name for file naming
    let artist: Option<models::Artist> = sqlx::query_as("SELECT * FROM artists WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?;
    let artist = artist.ok_or_else(|| AppError::NotFound("Artist not found".to_string()))?;

    let mut new_voice_key: Option<String> = None;
    let mut new_track1_key: Option<String> = None;
    let mut new_track2_key: Option<String> = None;
    let mut track1_name: Option<String> = None;
    let mut track2_name: Option<String> = None;

    // Peaks data
    let mut voice_peaks: Option<String> = None;
    let mut track1_peaks: Option<String> = None;
    let mut track2_peaks: Option<String> = None;

    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|e| AppError::BadRequest(format!("Failed to read multipart field: {}", e)))?
    {
        let field_name = field.name().unwrap_or("").to_string();

        match field_name.as_str() {
            "track1_name" => {
                let value = field
                    .text()
                    .await
                    .map_err(|e| AppError::BadRequest(format!("Failed to read field: {}", e)))?;
                if !value.is_empty() {
                    track1_name = Some(value);
                }
            }
            "track2_name" => {
                let value = field
                    .text()
                    .await
                    .map_err(|e| AppError::BadRequest(format!("Failed to read field: {}", e)))?;
                if !value.is_empty() {
                    track2_name = Some(value);
                }
            }
            "voice_peaks" => {
                voice_peaks =
                    Some(field.text().await.map_err(|e| {
                        AppError::BadRequest(format!("Failed to read peaks: {}", e))
                    })?);
            }
            "track1_peaks" => {
                track1_peaks =
                    Some(field.text().await.map_err(|e| {
                        AppError::BadRequest(format!("Failed to read peaks: {}", e))
                    })?);
            }
            "track2_peaks" => {
                track2_peaks =
                    Some(field.text().await.map_err(|e| {
                        AppError::BadRequest(format!("Failed to read peaks: {}", e))
                    })?);
            }
            "voice" => {
                let filename = field.file_name().unwrap_or("voice.mp3").to_string();
                let content_type = field.content_type().unwrap_or("audio/mpeg").to_string();
                let data = field.bytes().await.map_err(|e| {
                    AppError::BadRequest(format!("Failed to read file data: {}", e))
                })?;

                if !data.is_empty() {
                    let key = storage::upload_file_named(
                        &state,
                        id,
                        "voice",
                        &format!("{}-voice", artist.name),
                        &filename,
                        data.to_vec(),
                        &content_type,
                    )
                    .await?;
                    new_voice_key = Some(key);
                }
            }
            "track1" => {
                let filename = field.file_name().unwrap_or("track1.mp3").to_string();
                let content_type = field.content_type().unwrap_or("audio/mpeg").to_string();
                let data = field.bytes().await.map_err(|e| {
                    AppError::BadRequest(format!("Failed to read file data: {}", e))
                })?;

                if !data.is_empty() {
                    let desired_name = track1_name
                        .clone()
                        .unwrap_or_else(|| format!("{}-track1", artist.name));
                    let key = storage::upload_file_named(
                        &state,
                        id,
                        "track1",
                        &desired_name,
                        &filename,
                        data.to_vec(),
                        &content_type,
                    )
                    .await?;
                    new_track1_key = Some(key);
                }
            }
            "track2" => {
                let filename = field.file_name().unwrap_or("track2.mp3").to_string();
                let content_type = field.content_type().unwrap_or("audio/mpeg").to_string();
                let data = field.bytes().await.map_err(|e| {
                    AppError::BadRequest(format!("Failed to read file data: {}", e))
                })?;

                if !data.is_empty() {
                    let desired_name = track2_name
                        .clone()
                        .unwrap_or_else(|| format!("{}-track2", artist.name));
                    let key = storage::upload_file_named(
                        &state,
                        id,
                        "track2",
                        &desired_name,
                        &filename,
                        data.to_vec(),
                        &content_type,
                    )
                    .await?;
                    new_track2_key = Some(key);
                }
            }
            _ => {}
        }
    }

    // Upload peaks files alongside audio files
    if let (Some(ref key), Some(peaks_json)) = (&new_voice_key, voice_peaks) {
        let peaks_key = format!(
            "{}.peaks.json",
            key.rsplit_once('.').map(|(base, _)| base).unwrap_or(key)
        );
        state
            .s3_client
            .put_object()
            .bucket(&state.config.r2_bucket_name)
            .key(&peaks_key)
            .body(aws_sdk_s3::primitives::ByteStream::from(
                peaks_json.into_bytes(),
            ))
            .content_type("application/json")
            .send()
            .await
            .map_err(|e| AppError::Storage(format!("Failed to upload peaks: {}", e)))?;
    }
    if let (Some(ref key), Some(peaks_json)) = (&new_track1_key, track1_peaks) {
        let peaks_key = format!(
            "{}.peaks.json",
            key.rsplit_once('.').map(|(base, _)| base).unwrap_or(key)
        );
        state
            .s3_client
            .put_object()
            .bucket(&state.config.r2_bucket_name)
            .key(&peaks_key)
            .body(aws_sdk_s3::primitives::ByteStream::from(
                peaks_json.into_bytes(),
            ))
            .content_type("application/json")
            .send()
            .await
            .map_err(|e| AppError::Storage(format!("Failed to upload peaks: {}", e)))?;
    }
    if let (Some(ref key), Some(peaks_json)) = (&new_track2_key, track2_peaks) {
        let peaks_key = format!(
            "{}.peaks.json",
            key.rsplit_once('.').map(|(base, _)| base).unwrap_or(key)
        );
        state
            .s3_client
            .put_object()
            .bucket(&state.config.r2_bucket_name)
            .key(&peaks_key)
            .body(aws_sdk_s3::primitives::ByteStream::from(
                peaks_json.into_bytes(),
            ))
            .content_type("application/json")
            .send()
            .await
            .map_err(|e| AppError::Storage(format!("Failed to upload peaks: {}", e)))?;
    }

    // Build dynamic update query
    let mut updates = Vec::new();

    if new_voice_key.is_some() {
        updates.push("voice_message_key = ?");
    }
    if new_track1_key.is_some() {
        updates.push("track1_key = ?");
    }
    if new_track2_key.is_some() {
        updates.push("track2_key = ?");
    }
    if track1_name.is_some() {
        updates.push("track1_name = ?");
    }
    if track2_name.is_some() {
        updates.push("track2_name = ?");
    }

    if !updates.is_empty() {
        updates.push("updated_at = datetime('now')");
        let sql = format!("UPDATE artists SET {} WHERE id = ?", updates.join(", "));

        let mut query = sqlx::query(&sql);

        if let Some(ref key) = new_voice_key {
            query = query.bind(key);
        }
        if let Some(ref key) = new_track1_key {
            query = query.bind(key);
        }
        if let Some(ref key) = new_track2_key {
            query = query.bind(key);
        }
        if let Some(ref name) = track1_name {
            query = query.bind(name);
        }
        if let Some(ref name) = track2_name {
            query = query.bind(name);
        }

        query.bind(id).execute(&state.db).await?;

        // Regenerate waveform preview videos if tracks changed
        if new_track1_key.is_some() || new_track2_key.is_some() {
            let video_state = state.clone();
            tokio::spawn(async move {
                if let Err(e) = video::generate_and_store_artist_videos(video_state, id).await {
                    tracing::error!(
                        artist_id = id,
                        "Video regeneration after audio update failed: {e:#}"
                    );
                }
            });
        }

        // Trigger backup workflow (fire-and-forget)
        super::backup_trigger::trigger_backup(&state.config, id, "profile-audio-update");
    }

    Ok(Json(serde_json::json!({ "success": true })))
}

// ============================================================================
// Shows API
// ============================================================================

#[derive(Debug, Serialize)]
pub struct ShowListItem {
    id: i64,
    title: String,
    date: String,
    start_time: Option<String>,
    end_time: Option<String>,
    description: Option<String>,
    status: String,
    show_type: String,
    artists: Vec<ArtistBrief>,
}

#[derive(Debug, Serialize)]
pub struct ShowDetailResponse {
    id: i64,
    title: String,
    date: String,
    start_time: Option<String>,
    end_time: Option<String>,
    description: Option<String>,
    ai_bio: Option<String>,
    status: String,
    show_type: String,
    created_at: String,
    updated_at: Option<String>,
    artists: Vec<AssignedArtistInfo>,
    available_artists: Vec<ArtistWithPronouns>,
    artists_left: i32,
    cover_url: Option<String>,
    collage_url: Option<String>,
    cover_generated_at: Option<String>,
    active_overlay_preset_id: Option<i64>,
    recording_url: Option<String>,
    recording_peaks_url: Option<String>,
    recording_filename: Option<String>,
    instagram_posted_at: Option<String>,
    instagram_post_url: Option<String>,
    soundcloud_track_id: Option<String>,
    soundcloud_url: Option<String>,
    soundcloud_uploaded_at: Option<String>,
    soundcloud_public: Option<bool>,
    // Prerecorded media (external/brunchtime shows): present => "upload" mode, absent => "live"
    prerecorded_key: Option<String>,
    prerecorded_filename: Option<String>,
    prerecorded_confirmed_at: Option<String>,
    prerecorded_url: Option<String>,
    // Host assignment (external/brunchtime shows)
    host_user_id: Option<i64>,
    host_username: Option<String>,
    available_hosts: Vec<AvailableHost>,
    /// Intended delivery: "live" or "prerecorded" (changeable after creation).
    stream_mode: Option<String>,
}

/// Rich artist info for show detail page (includes pic_url and audio URLs)
#[derive(Debug, Serialize)]
pub struct AssignedArtistInfo {
    id: i64,
    name: String,
    pronouns: String,
    pic_url: Option<String>,
    voice_url: Option<String>,
    track1_url: Option<String>,
    track2_url: Option<String>,
    track1_peaks_url: Option<String>,
    track2_peaks_url: Option<String>,
    voice_peaks_url: Option<String>,
    has_pic: bool,
}

#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct ArtistBrief {
    id: i64,
    name: String,
}

#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct AvailableHost {
    id: i64,
    username: String,
}

#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct ArtistWithPronouns {
    id: i64,
    name: String,
    pronouns: String,
}

#[derive(Debug, Serialize)]
pub struct ShowsListResponse {
    shows: Vec<ShowListItem>,
    artists: Vec<ArtistBrief>,
}

/// Read-only schedule entry returned to any authenticated user (hosts included).
#[derive(Debug, Serialize)]
pub struct ShowOverviewItem {
    id: i64,
    title: String,
    date: String,
    start_time: Option<String>,
    end_time: Option<String>,
    description: Option<String>,
    status: String,
    show_type: String,
    /// Username of the assigned host (external/brunchtime shows), if any.
    host_username: Option<String>,
    artists: Vec<ArtistBrief>,
}

#[derive(Debug, Serialize)]
pub struct ShowsOverviewResponse {
    shows: Vec<ShowOverviewItem>,
}

/// GET /api/shows-overview — read-only list of **all** shows for any authenticated
/// user. Lets hosts see the full schedule (including other users' shows) without
/// the admin-only assignment data exposed by [`api_shows_list`].
pub async fn api_shows_overview(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let token = auth::get_session_from_headers(&headers);
    auth::get_current_user(&state, token.as_deref())
        .await
        .ok_or_else(|| AppError::Unauthorized("Not authenticated".to_string()))?;

    let shows: Vec<models::Show> =
        sqlx::query_as("SELECT * FROM shows ORDER BY date DESC, id DESC")
            .fetch_all(&state.db)
            .await?;

    let mut items = Vec::new();
    for show in shows {
        let artists: Vec<ArtistBrief> = sqlx::query_as(
            "SELECT a.id, a.name FROM artists a \
             INNER JOIN artist_show_assignments asa ON a.id = asa.artist_id \
             WHERE asa.show_id = ? \
             ORDER BY asa.sort_order, a.name",
        )
        .bind(show.id)
        .fetch_all(&state.db)
        .await?;

        let host_username: Option<String> = if let Some(hid) = show.host_user_id {
            sqlx::query_scalar("SELECT username FROM users WHERE id = ?")
                .bind(hid)
                .fetch_optional(&state.db)
                .await?
        } else {
            None
        };

        items.push(ShowOverviewItem {
            id: show.id,
            title: show.title,
            date: show.date,
            start_time: show.start_time,
            end_time: show.end_time,
            description: show.description,
            status: show.status,
            show_type: show.show_type,
            host_username,
            artists,
        });
    }

    Ok(Json(ShowsOverviewResponse { shows: items }))
}

pub async fn api_shows_list(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    let shows: Vec<models::Show> =
        sqlx::query_as("SELECT * FROM shows ORDER BY date DESC, id DESC")
            .fetch_all(&state.db)
            .await?;

    // Get artists for each show
    let mut show_items = Vec::new();
    for show in shows {
        let artists: Vec<ArtistBrief> = sqlx::query_as(
            r#"
            SELECT a.id, a.name FROM artists a
            INNER JOIN artist_show_assignments asa ON a.id = asa.artist_id
            WHERE asa.show_id = ?
            "#,
        )
        .bind(show.id)
        .fetch_all(&state.db)
        .await?;

        show_items.push(ShowListItem {
            id: show.id,
            title: show.title,
            date: show.date,
            start_time: show.start_time,
            end_time: show.end_time,
            description: show.description,
            status: show.status,
            show_type: show.show_type,
            artists,
        });
    }

    // Get all artists for assignment dropdown
    let all_artists: Vec<ArtistBrief> =
        sqlx::query_as("SELECT id, name FROM artists ORDER BY name COLLATE NOCASE")
            .fetch_all(&state.db)
            .await?;

    Ok(Json(ShowsListResponse {
        shows: show_items,
        artists: all_artists,
    }))
}

/// Helper struct to fetch artist with pic and audio keys
#[derive(Debug, sqlx::FromRow)]
struct ArtistWithFileKeys {
    id: i64,
    name: String,
    pronouns: String,
    pic_key: Option<String>,
    pic_cropped_key: Option<String>,
    pic_overlay_key: Option<String>,
    voice_message_key: Option<String>,
    track1_key: Option<String>,
    track2_key: Option<String>,
}

/// Debounce delay before regenerating show cover (5 seconds)
const COVER_DEBOUNCE_DELAY: std::time::Duration = std::time::Duration::from_secs(5);

/// Schedule debounced cover regeneration for a show.
/// This spawns a background task that waits for COVER_DEBOUNCE_DELAY before
/// actually regenerating. If another request comes in during that time,
/// the timer resets.
fn schedule_cover_regeneration(state: Arc<AppState>, show_id: i64) {
    tokio::spawn(async move {
        let request_time = tokio::time::Instant::now();

        // Record this request time
        {
            let mut debounce = state.cover_debounce.write().await;
            debounce.insert(show_id, request_time);
        }

        // Wait for debounce delay
        tokio::time::sleep(COVER_DEBOUNCE_DELAY).await;

        // Check if our request is still the latest one
        let should_proceed = {
            let debounce = state.cover_debounce.read().await;
            debounce
                .get(&show_id)
                .map(|t| *t == request_time)
                .unwrap_or(false)
        };

        if !should_proceed {
            // A newer request came in, skip this one
            tracing::debug!(
                "Skipping cover regeneration for show {} - superseded by newer request",
                show_id
            );
            return;
        }

        // Clean up debounce entry
        {
            let mut debounce = state.cover_debounce.write().await;
            if debounce
                .get(&show_id)
                .map(|t| *t == request_time)
                .unwrap_or(false)
            {
                debounce.remove(&show_id);
            }
        }

        // Actually regenerate the cover
        tracing::info!("Regenerating cover for show {}", show_id);
        let _ = do_regenerate_show_cover(&state, show_id).await;
    });
}

/// S3 key for the default cover template
const DEFAULT_COVER_KEY: &str = "shows/_default/cover.png";

/// Ensure the default cover exists in S3 (called at startup)
pub async fn ensure_default_cover_exists(state: &Arc<AppState>) -> crate::Result<()> {
    // Check if default cover already exists in S3
    let exists = state
        .s3_client
        .head_object()
        .bucket(&state.config.r2_bucket_name)
        .key(DEFAULT_COVER_KEY)
        .send()
        .await
        .is_ok();

    if exists {
        tracing::info!("Default cover already exists in S3");
        return Ok(());
    }

    tracing::info!("Generating and uploading default cover to S3...");

    // Generate cover with empty items (all black tiles)
    let cover_data = crate::image_overlay::build_plain_collage(Vec::new())
        .await
        .ok_or_else(|| crate::AppError::Internal("Failed to generate default cover".to_string()))?;

    // Upload to S3 at default location
    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(DEFAULT_COVER_KEY)
        .body(aws_sdk_s3::primitives::ByteStream::from(cover_data.clone()))
        .content_type("image/png")
        .send()
        .await
        .map_err(|e| crate::AppError::Storage(format!("Failed to upload default cover: {}", e)))?;

    // Also cache in memory
    let _ = state.default_cover.set(cover_data);

    tracing::info!("Default cover uploaded to S3: {}", DEFAULT_COVER_KEY);
    Ok(())
}

/// Copy the default cover from S3 to a new show's location
async fn copy_default_cover_to_show(state: &Arc<AppState>, show_id: i64) -> Option<String> {
    let dest_key = format!("shows/{}/cover.png", show_id);
    let source = format!("{}/{}", state.config.r2_bucket_name, DEFAULT_COVER_KEY);

    // Use S3 copy operation (faster than download + upload)
    let result = state
        .s3_client
        .copy_object()
        .bucket(&state.config.r2_bucket_name)
        .copy_source(&source)
        .key(&dest_key)
        .content_type("image/png")
        .send()
        .await;

    if let Err(e) = result {
        tracing::warn!("Failed to copy default cover for show {}: {}", show_id, e);
        return None;
    }

    // Update cover_generated_at timestamp
    let now = chrono::Utc::now().to_rfc3339();
    let _ = sqlx::query("UPDATE shows SET cover_generated_at = ? WHERE id = ?")
        .bind(&now)
        .bind(show_id)
        .execute(&state.db)
        .await;

    tracing::info!("Default cover copied to show {}: {}", show_id, dest_key);
    Some(dest_key)
}

/// Copy a show template's cover into a show's cover slot (S3-to-S3) and stamp
/// `cover_generated_at`. Mirrors `copy_default_cover_to_show` but sources the
/// template cover instead of the global default.
async fn copy_template_cover_to_show(
    state: &Arc<AppState>,
    template_cover_key: &str,
    show_id: i64,
) -> Option<String> {
    let dest_key = format!("shows/{}/cover.png", show_id);
    let source = format!("{}/{}", state.config.r2_bucket_name, template_cover_key);

    let result = state
        .s3_client
        .copy_object()
        .bucket(&state.config.r2_bucket_name)
        .copy_source(&source)
        .key(&dest_key)
        .content_type("image/png")
        .send()
        .await;

    if let Err(e) = result {
        tracing::warn!(
            "Failed to copy template cover {} to show {}: {}",
            template_cover_key,
            show_id,
            e
        );
        return None;
    }

    let now = chrono::Utc::now().to_rfc3339();
    let _ = sqlx::query("UPDATE shows SET cover_generated_at = ? WHERE id = ?")
        .bind(&now)
        .bind(show_id)
        .execute(&state.db)
        .await;

    tracing::info!("Template cover copied to show {}: {}", show_id, dest_key);
    Some(dest_key)
}

/// Actually performs the cover regeneration (called after debounce).
///
/// 1. Builds a plain 2×2 collage (no text/branding) and uploads to `shows/{id}/collage.png`.
/// 2. If the show has an active overlay preset, applies it and uploads the branded
///    result to `shows/{id}/cover.png`. Otherwise, copies the plain collage as cover.
async fn do_regenerate_show_cover(state: &Arc<AppState>, show_id: i64) -> Option<String> {
    // Fetch the show (need title + active_overlay_preset_id)
    let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(show_id)
        .fetch_optional(&state.db)
        .await
        .ok()
        .flatten()?;

    // Get assigned artists with their pic keys
    let artists: Vec<ArtistWithFileKeys> = sqlx::query_as(
        r#"
        SELECT a.id, a.name, a.pronouns, a.pic_key, a.pic_cropped_key, a.pic_overlay_key,
               a.voice_message_key, a.track1_key, a.track2_key
        FROM artists a
        INNER JOIN artist_show_assignments asa ON a.id = asa.artist_id
        WHERE asa.show_id = ?
        ORDER BY asa.sort_order, a.name COLLATE NOCASE
        LIMIT 4
        "#,
    )
    .bind(show_id)
    .fetch_all(&state.db)
    .await
    .ok()?;

    // Collect artist images for plain collage (empty vec = all black tiles)
    let mut collage_items: Vec<(Vec<u8>, String)> = Vec::new();
    for artist in &artists {
        let pic_key = artist
            .pic_cropped_key
            .as_ref()
            .or(artist.pic_key.as_ref())
            .or(artist.pic_overlay_key.as_ref());

        if let Some(key) = pic_key {
            if let Ok((data, _)) = storage::download_file(state, key).await {
                let ext = std::path::Path::new(key)
                    .extension()
                    .and_then(|e| e.to_str())
                    .unwrap_or("jpg")
                    .to_string();
                collage_items.push((data, ext));
            }
        }
    }

    // 1. Generate plain 2×2 collage (no text, no branding)
    let collage_png = crate::image_overlay::build_plain_collage(collage_items).await?;

    // Upload plain collage to R2
    if let Err(e) = storage::upload_show_collage(state, show_id, collage_png.clone()).await {
        tracing::warn!("Failed to upload plain collage for show {}: {}", show_id, e);
    }

    // 2. Determine cover: apply overlay preset if one is active, else use plain collage
    let cover_png = if let Some(preset_id) = show.active_overlay_preset_id {
        // Fetch preset params JSON
        let preset_params: Option<String> =
            sqlx::query_scalar("SELECT params FROM overlay_presets WHERE id = ?")
                .bind(preset_id)
                .fetch_optional(&state.db)
                .await
                .ok()
                .flatten();

        if let Some(params_json) = preset_params {
            match serde_json::from_str::<crate::image_overlay::OverlayParams>(&params_json) {
                Ok(params) => {
                    // Collect artist names for tile labels
                    let tile_names: Vec<String> = artists.iter().map(|a| a.name.clone()).collect();
                    // Apply overlay preset to the plain collage
                    match crate::image_overlay::apply_overlay_preset(
                        collage_png.clone(),
                        params,
                        show.title.clone(),
                        Some(tile_names),
                    )
                    .await
                    {
                        Some(branded) => branded,
                        None => {
                            tracing::warn!(
                                "Failed to apply overlay preset {} to show {}, using plain collage",
                                preset_id,
                                show_id
                            );
                            collage_png
                        }
                    }
                }
                Err(e) => {
                    tracing::warn!(
                        "Failed to deserialize overlay preset {} params for show {}: {}",
                        preset_id,
                        show_id,
                        e
                    );
                    collage_png
                }
            }
        } else {
            tracing::warn!(
                "Active overlay preset {} not found for show {}, using plain collage",
                preset_id,
                show_id
            );
            collage_png
        }
    } else {
        // No active preset — cover is just the plain collage
        collage_png
    };

    // Upload cover to R2
    let key = storage::upload_show_cover(state, show_id, cover_png)
        .await
        .ok()?;

    // Update cover_generated_at timestamp
    let now = chrono::Utc::now().to_rfc3339();
    let _ = sqlx::query("UPDATE shows SET cover_generated_at = ? WHERE id = ?")
        .bind(&now)
        .bind(show_id)
        .execute(&state.db)
        .await;

    tracing::info!("Cover regenerated for show {}: {}", show_id, key);
    Some(key)
}

pub async fn api_show_detail(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    // Any authenticated user may VIEW a show's detail. Hosts can open shows from
    // the "All Shows" overview; write access is enforced per-endpoint (see
    // require_show_editor) and edit controls are role-gated in the UI.
    let token = auth::get_session_from_headers(&headers);
    let viewer = auth::get_current_user(&state, token.as_deref())
        .await
        .ok_or_else(|| AppError::Unauthorized("Not authenticated".to_string()))?;
    let viewer_is_admin = viewer.role_enum().can_access_admin();

    let show: Option<models::Show> = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?;

    let show = show.ok_or_else(|| AppError::NotFound("Show not found".to_string()))?;

    // Get assigned artists with file keys for URL generation and audio availability
    let assigned_artists_raw: Vec<ArtistWithFileKeys> = sqlx::query_as(
        r#"
        SELECT a.id, a.name, a.pronouns, a.pic_key, a.pic_cropped_key, a.pic_overlay_key,
               a.voice_message_key, a.track1_key, a.track2_key
        FROM artists a
        INNER JOIN artist_show_assignments asa ON a.id = asa.artist_id
        WHERE asa.show_id = ?
        ORDER BY asa.sort_order, a.name COLLATE NOCASE
        "#,
    )
    .bind(id)
    .fetch_all(&state.db)
    .await?;

    // Generate presigned URLs for artist pictures and audio files
    let mut artists = Vec::new();
    for a in assigned_artists_raw {
        let pic_key = a
            .pic_overlay_key
            .as_ref()
            .or(a.pic_cropped_key.as_ref())
            .or(a.pic_key.as_ref());
        let pic_url = if let Some(key) = pic_key {
            storage::get_presigned_url(&state, key, 3600).await.ok()
        } else {
            None
        };
        let voice_url = if let Some(key) = &a.voice_message_key {
            storage::get_presigned_url(&state, key, 3600).await.ok()
        } else {
            None
        };
        let track1_url = if let Some(key) = &a.track1_key {
            storage::get_presigned_url(&state, key, 3600).await.ok()
        } else {
            None
        };
        let track2_url = if let Some(key) = &a.track2_key {
            storage::get_presigned_url(&state, key, 3600).await.ok()
        } else {
            None
        };
        // Try to get peaks URLs (stored alongside audio files with .peaks.json suffix)
        let track1_peaks_url = if let Some(key) = &a.track1_key {
            let peaks_key = derive_peaks_key(key);
            storage::get_presigned_url(&state, &peaks_key, 3600)
                .await
                .ok()
        } else {
            None
        };
        let track2_peaks_url = if let Some(key) = &a.track2_key {
            let peaks_key = derive_peaks_key(key);
            storage::get_presigned_url(&state, &peaks_key, 3600)
                .await
                .ok()
        } else {
            None
        };
        let voice_peaks_url = if let Some(key) = &a.voice_message_key {
            let peaks_key = derive_peaks_key(key);
            storage::get_presigned_url(&state, &peaks_key, 3600)
                .await
                .ok()
        } else {
            None
        };
        artists.push(AssignedArtistInfo {
            id: a.id,
            name: a.name,
            pronouns: a.pronouns,
            has_pic: pic_key.is_some(),
            pic_url,
            voice_url,
            track1_url,
            track2_url,
            track1_peaks_url,
            track2_peaks_url,
            voice_peaks_url,
        });
    }

    // Calculate artists left (max 4) — only for UNHEARD shows
    let is_unheard = show.show_type == "unheard";
    let artists_left = if is_unheard {
        (4 - artists.len() as i32).max(0)
    } else {
        0
    };

    // Get available artists (NOT assigned to ANY show - truly unassigned) — only for
    // UNHEARD shows, and only for admins (the assignment dropdown is admin-only).
    let available_artists: Vec<ArtistWithPronouns> =
        if viewer_is_admin && is_unheard && artists_left > 0 {
            sqlx::query_as(
                r#"
            SELECT a.id, a.name, a.pronouns FROM artists a
            WHERE a.id NOT IN (
                SELECT DISTINCT artist_id FROM artist_show_assignments
            )
            ORDER BY a.name COLLATE NOCASE
            "#,
            )
            .fetch_all(&state.db)
            .await?
        } else {
            Vec::new()
        };

    // Resolve host user for non-UNHEARD shows
    let (host_user_id, host_username, available_hosts) = if !is_unheard {
        let host_username: Option<String> = if let Some(hid) = show.host_user_id {
            sqlx::query_scalar("SELECT username FROM users WHERE id = ?")
                .bind(hid)
                .fetch_optional(&state.db)
                .await?
        } else {
            None
        };

        // Available hosts: all users with role 'host' or 'admin' — admin-only,
        // since reassigning a host is an admin action.
        let hosts: Vec<AvailableHost> = if viewer_is_admin {
            sqlx::query_as(
                r#"
            SELECT u.id, u.username FROM users u
            WHERE u.role IN ('host', 'admin')
            ORDER BY u.username COLLATE NOCASE
            "#,
            )
            .fetch_all(&state.db)
            .await?
        } else {
            Vec::new()
        };

        (show.host_user_id, host_username, hosts)
    } else {
        (None, None, Vec::new())
    };

    // Only return cover URL if cover was actually generated
    let cover_url = if show.cover_generated_at.is_some() {
        let cover_key = format!("shows/{}/cover.png", id);
        storage::get_presigned_url(&state, &cover_key, 3600)
            .await
            .ok()
    } else {
        None
    };

    // Presigned URL for the plain collage (may not exist for legacy shows)
    let collage_url = if show.cover_generated_at.is_some() {
        let collage_key = format!("shows/{}/collage.png", id);
        storage::get_presigned_url(&state, &collage_key, 3600)
            .await
            .ok()
    } else {
        None
    };

    // Generate presigned URL for recording if it exists
    let recording_url = if let Some(ref key) = show.recording_key {
        storage::get_presigned_url(&state, key, 3600).await.ok()
    } else {
        None
    };

    // Generate presigned URL for recording peaks if they exist
    let recording_peaks_url = if let Some(ref key) = show.recording_key {
        let peaks_key = format!(
            "{}.peaks.json",
            key.rsplit_once('.').map(|(base, _)| base).unwrap_or(key)
        );
        storage::get_presigned_url(&state, &peaks_key, 3600)
            .await
            .ok()
    } else {
        None
    };

    // Generate presigned URL for the prerecorded file if it exists (external/brunchtime upload mode)
    let prerecorded_url = if let Some(ref key) = show.prerecorded_key {
        storage::get_presigned_url(&state, key, 3600).await.ok()
    } else {
        None
    };

    Ok(Json(ShowDetailResponse {
        id: show.id,
        title: show.title,
        date: show.date,
        start_time: show.start_time,
        end_time: show.end_time,
        description: show.description,
        ai_bio: show.ai_bio,
        status: show.status,
        show_type: show.show_type,
        created_at: show.created_at,
        updated_at: show.updated_at,
        artists,
        available_artists,
        artists_left,
        cover_url,
        collage_url,
        cover_generated_at: show.cover_generated_at,
        active_overlay_preset_id: show.active_overlay_preset_id,
        recording_url,
        recording_peaks_url,
        recording_filename: show.recording_filename,
        instagram_posted_at: show.instagram_posted_at,
        instagram_post_url: show.instagram_post_url,
        soundcloud_track_id: show.soundcloud_track_id,
        soundcloud_url: show.soundcloud_url,
        soundcloud_uploaded_at: show.soundcloud_uploaded_at,
        soundcloud_public: show.soundcloud_public,
        prerecorded_key: show.prerecorded_key,
        prerecorded_filename: show.prerecorded_filename,
        prerecorded_confirmed_at: show.prerecorded_confirmed_at,
        prerecorded_url,
        host_user_id,
        host_username,
        available_hosts,
        stream_mode: show.stream_mode,
    }))
}

#[derive(Debug, Deserialize)]
pub struct CreateShowRequest {
    title: String,
    date: String,
    description: Option<String>,
    show_type: Option<String>,
    start_time: Option<String>,
    end_time: Option<String>,
    /// Optional show template (owned by the caller) to seed the cover from.
    template_id: Option<i64>,
    /// Optional user to present the show. Admins only; ignored for hosts (who
    /// always self-assign).
    host_user_id: Option<i64>,
    /// Intended delivery: "live" or "prerecorded". Defaults to "live".
    stream_mode: Option<String>,
}

/// Default show type for host-created shows. Hosts can't pick a type, and host
/// assignment is reserved for non-UNHEARD shows (see `api_show_assign_host`).
const HOST_DEFAULT_SHOW_TYPE: &str = "external";

pub async fn api_create_show(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(req): Json<CreateShowRequest>,
) -> Result<impl IntoResponse> {
    let user = require_show_creator(&state, &headers).await?;
    let is_admin = user.role_enum().can_access_admin();

    // If a template is referenced, it must exist and be owned by the caller.
    // We use its cover (if any) for the new show.
    let template_cover_key: Option<String> = if let Some(template_id) = req.template_id {
        let row: Option<(Option<String>,)> = sqlx::query_as(
            "SELECT cover_key FROM show_templates WHERE id = ? AND owner_user_id = ?",
        )
        .bind(template_id)
        .bind(user.id)
        .fetch_optional(&state.db)
        .await?;
        match row {
            Some((cover_key,)) => cover_key,
            None => return Err(AppError::NotFound("Template not found".to_string())),
        }
    } else {
        None
    };

    // Hosts get a constrained show: forced type, self-assigned, no description /
    // end time. Admins get full control over every field.
    let (show_type, description, end_time, host_user_id) = if is_admin {
        let show_type = req.show_type.as_deref().unwrap_or("unheard");
        if !matches!(show_type, "unheard" | "brunchtime" | "external") {
            return Err(AppError::BadRequest(format!(
                "Invalid show_type: '{}'. Must be 'unheard', 'brunchtime', or 'external'",
                show_type
            )));
        }
        // Validate the assignee exists, if one was provided.
        if let Some(host_user_id) = req.host_user_id {
            let exists: Option<(i64,)> = sqlx::query_as("SELECT id FROM users WHERE id = ?")
                .bind(host_user_id)
                .fetch_optional(&state.db)
                .await?;
            if exists.is_none() {
                return Err(AppError::BadRequest("Assignee user not found".to_string()));
            }
        }
        (
            show_type.to_string(),
            req.description.clone(),
            req.end_time.clone(),
            req.host_user_id,
        )
    } else {
        (
            HOST_DEFAULT_SHOW_TYPE.to_string(),
            None,
            None,
            Some(user.id),
        )
    };

    // Delivery mode: accept only the two known values, default to "live".
    let stream_mode = match req.stream_mode.as_deref() {
        Some("prerecorded") => "prerecorded",
        _ => "live",
    };

    let result = sqlx::query(
        "INSERT INTO shows (title, date, description, status, show_type, start_time, end_time, host_user_id, stream_mode) VALUES (?, ?, ?, 'scheduled', ?, ?, ?, ?, ?)",
    )
    .bind(&req.title)
    .bind(&req.date)
    .bind(&description)
    .bind(&show_type)
    .bind(&req.start_time)
    .bind(&end_time)
    .bind(host_user_id)
    .bind(stream_mode)
    .execute(&state.db)
    .await?;

    let show_id = result.last_insert_rowid();

    // Seed the cover synchronously so it's ready when the frontend loads: prefer
    // the chosen template's cover, otherwise fall back to the default cover.
    let copied_key = match &template_cover_key {
        Some(tpl_key) => copy_template_cover_to_show(&state, tpl_key, show_id).await,
        None => None,
    };
    let copied_key = match copied_key {
        Some(key) => Some(key),
        None => copy_default_cover_to_show(&state, show_id).await,
    };
    let cover_url = if let Some(key) = copied_key {
        storage::get_presigned_url(&state, &key, 3600).await.ok()
    } else {
        None
    };

    // Fetch the cover_generated_at timestamp we just set
    let cover_generated_at: Option<String> =
        sqlx::query_scalar("SELECT cover_generated_at FROM shows WHERE id = ?")
            .bind(show_id)
            .fetch_optional(&state.db)
            .await?
            .flatten();

    Ok((
        StatusCode::CREATED,
        Json(serde_json::json!({
            "id": show_id,
            "title": req.title,
            "date": req.date,
            "description": description,
            "status": "scheduled",
            "show_type": show_type,
            "start_time": req.start_time,
            "end_time": end_time,
            "host_user_id": host_user_id,
            "stream_mode": stream_mode,
            "cover_url": cover_url,
            "cover_generated_at": cover_generated_at,
        })),
    ))
}

// ============================================================================
// Show Templates — reusable (name + cover + description) bundles, per user
// ============================================================================

#[derive(Debug, Serialize)]
pub struct ShowTemplateItem {
    id: i64,
    name: String,
    description: Option<String>,
    cover_url: Option<String>,
    created_at: String,
}

/// GET /api/show-templates — list the current user's templates (with presigned covers).
pub async fn api_list_show_templates(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let user = require_show_creator(&state, &headers).await?;

    let templates: Vec<models::ShowTemplate> = sqlx::query_as(
        "SELECT * FROM show_templates WHERE owner_user_id = ? ORDER BY created_at DESC, id DESC",
    )
    .bind(user.id)
    .fetch_all(&state.db)
    .await?;

    let mut items = Vec::with_capacity(templates.len());
    for t in templates {
        let cover_url = match &t.cover_key {
            Some(key) => storage::get_presigned_url(&state, key, 3600).await.ok(),
            None => None,
        };
        items.push(ShowTemplateItem {
            id: t.id,
            name: t.name,
            description: t.description,
            cover_url,
            created_at: t.created_at,
        });
    }

    Ok(Json(serde_json::json!({ "templates": items })))
}

#[derive(Debug, Deserialize)]
pub struct CreateShowTemplateRequest {
    name: String,
    description: Option<String>,
}

/// POST /api/show-templates — create a template owned by the current user.
pub async fn api_create_show_template(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(req): Json<CreateShowTemplateRequest>,
) -> Result<impl IntoResponse> {
    let user = require_show_creator(&state, &headers).await?;

    let name = req.name.trim();
    if name.is_empty() {
        return Err(AppError::BadRequest(
            "Template name is required".to_string(),
        ));
    }

    let result = sqlx::query(
        "INSERT INTO show_templates (owner_user_id, name, description) VALUES (?, ?, ?)",
    )
    .bind(user.id)
    .bind(name)
    .bind(&req.description)
    .execute(&state.db)
    .await?;

    let id = result.last_insert_rowid();

    Ok((
        StatusCode::CREATED,
        Json(serde_json::json!({
            "id": id,
            "name": name,
            "description": req.description,
        })),
    ))
}

/// POST /api/show-templates/:id/cover — upload a cover image for a template.
pub async fn api_upload_template_cover(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    mut multipart: axum::extract::Multipart,
) -> Result<impl IntoResponse> {
    let user = require_show_creator(&state, &headers).await?;

    // Template must exist and be owned by the caller.
    let owned: Option<(i64,)> =
        sqlx::query_as("SELECT id FROM show_templates WHERE id = ? AND owner_user_id = ?")
            .bind(id)
            .bind(user.id)
            .fetch_optional(&state.db)
            .await?;
    if owned.is_none() {
        return Err(AppError::NotFound("Template not found".to_string()));
    }

    let mut file_data: Option<Vec<u8>> = None;
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|e| AppError::BadRequest(format!("Failed to read multipart field: {}", e)))?
    {
        if field.name().unwrap_or("") == "file" {
            file_data = Some(
                field
                    .bytes()
                    .await
                    .map_err(|e| AppError::BadRequest(format!("Failed to read file: {}", e)))?
                    .to_vec(),
            );
        }
    }

    let data = file_data.ok_or_else(|| AppError::BadRequest("No file provided".to_string()))?;

    let key = storage::upload_template_cover(&state, id, data).await?;
    sqlx::query(
        "UPDATE show_templates SET cover_key = ?, updated_at = datetime('now') WHERE id = ?",
    )
    .bind(&key)
    .bind(id)
    .execute(&state.db)
    .await?;

    let cover_url = storage::get_presigned_url(&state, &key, 3600).await.ok();

    Ok(Json(serde_json::json!({
        "success": true,
        "cover_url": cover_url,
    })))
}

/// DELETE /api/show-templates/:id — delete a template owned by the current user.
pub async fn api_delete_show_template(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let user = require_show_creator(&state, &headers).await?;

    let result = sqlx::query("DELETE FROM show_templates WHERE id = ? AND owner_user_id = ?")
        .bind(id)
        .bind(user.id)
        .execute(&state.db)
        .await?;

    if result.rows_affected() == 0 {
        return Err(AppError::NotFound("Template not found".to_string()));
    }

    Ok(Json(serde_json::json!({ "success": true })))
}

#[derive(Debug, Deserialize)]
pub struct UpdateShowRequest {
    title: Option<String>,
    date: Option<String>,
    description: Option<String>,
    ai_bio: Option<String>,
    show_type: Option<String>,
    start_time: Option<String>,
    end_time: Option<String>,
    stream_mode: Option<String>,
}

pub async fn api_update_show(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    Json(req): Json<UpdateShowRequest>,
) -> Result<impl IntoResponse> {
    // Admins (any show) or the assigned host (their own show) may update.
    let (user, _show) = require_show_editor(&state, &headers, id).await?;

    // show_type and ai_bio are admin-only fields; hosts have no UI for them.
    if !user.role_enum().can_access_admin() && (req.show_type.is_some() || req.ai_bio.is_some()) {
        return Err(AppError::Forbidden(
            "Only admins can change show type or AI bio".to_string(),
        ));
    }

    // Build dynamic update query
    let mut updates = Vec::new();
    let mut binds: Vec<String> = Vec::new();

    if let Some(title) = &req.title {
        updates.push("title = ?");
        binds.push(title.clone());
    }
    if let Some(date) = &req.date {
        updates.push("date = ?");
        binds.push(date.clone());
    }
    if let Some(description) = &req.description {
        updates.push("description = ?");
        binds.push(description.clone());
    }
    if let Some(ai_bio) = &req.ai_bio {
        updates.push("ai_bio = ?");
        binds.push(ai_bio.clone());
    }
    if let Some(show_type) = &req.show_type {
        if !matches!(show_type.as_str(), "unheard" | "brunchtime" | "external") {
            return Err(AppError::BadRequest(format!(
                "Invalid show_type: '{}'. Must be 'unheard', 'brunchtime', or 'external'",
                show_type
            )));
        }
        updates.push("show_type = ?");
        binds.push(show_type.clone());
    }
    if let Some(start_time) = &req.start_time {
        updates.push("start_time = ?");
        binds.push(start_time.clone());
    }
    if let Some(end_time) = &req.end_time {
        updates.push("end_time = ?");
        binds.push(end_time.clone());
    }
    if let Some(stream_mode) = &req.stream_mode {
        if !matches!(stream_mode.as_str(), "live" | "prerecorded") {
            return Err(AppError::BadRequest(format!(
                "Invalid stream_mode: '{}'. Must be 'live' or 'prerecorded'",
                stream_mode
            )));
        }
        updates.push("stream_mode = ?");
        binds.push(stream_mode.clone());
    }

    if updates.is_empty() {
        return Err(AppError::BadRequest("No fields to update".to_string()));
    }

    let query_str = format!("UPDATE shows SET {} WHERE id = ?", updates.join(", "));
    let mut query = sqlx::query(&query_str);
    for bind in &binds {
        query = query.bind(bind);
    }
    query = query.bind(id);
    query.execute(&state.db).await?;

    // Return updated show
    let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(id)
        .fetch_one(&state.db)
        .await?;

    Ok(Json(serde_json::json!({
        "id": show.id,
        "title": show.title,
        "date": show.date,
        "description": show.description,
        "status": show.status,
    })))
}

pub async fn api_delete_show(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    // Delete the show from database
    sqlx::query("DELETE FROM shows WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await?;

    // Delete cover from S3 (ignore errors - cover may not exist)
    let _ = storage::delete_show_cover(&state, id).await;

    Ok(Json(serde_json::json!({ "success": true })))
}

// ============================================================================
// Show Active Overlay Preset
// ============================================================================

pub async fn api_set_show_active_preset(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    Json(req): Json<SetActivePresetRequest>,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    // Verify show exists
    let show_exists: bool = sqlx::query_scalar("SELECT COUNT(*) > 0 FROM shows WHERE id = ?")
        .bind(id)
        .fetch_one(&state.db)
        .await?;
    if !show_exists {
        return Err(AppError::NotFound("Show not found".to_string()));
    }

    // Validate preset exists if setting one
    if let Some(preset_id) = req.preset_id {
        let preset_exists: bool =
            sqlx::query_scalar("SELECT COUNT(*) > 0 FROM overlay_presets WHERE id = ?")
                .bind(preset_id)
                .fetch_one(&state.db)
                .await?;
        if !preset_exists {
            return Err(AppError::BadRequest("Overlay preset not found".to_string()));
        }
    }

    // Update the active preset
    sqlx::query(
        "UPDATE shows SET active_overlay_preset_id = ?, updated_at = datetime('now') WHERE id = ?",
    )
    .bind(req.preset_id)
    .bind(id)
    .execute(&state.db)
    .await?;

    // Re-render the cover with the new preset (or plain collage if cleared)
    let _ = do_regenerate_show_cover(&state, id).await;

    Ok(Json(serde_json::json!({ "success": true })))
}

/// POST /api/shows/:id/regenerate-cover
/// Explicitly trigger cover regeneration (plain collage + overlay if preset is set).
/// Used by the Overlay Editor when the collage image doesn't exist yet.
pub async fn api_regenerate_show_cover(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    // Verify show exists
    let show_exists: bool = sqlx::query_scalar("SELECT COUNT(*) > 0 FROM shows WHERE id = ?")
        .bind(id)
        .fetch_one(&state.db)
        .await?;
    if !show_exists {
        return Err(AppError::NotFound("Show not found".to_string()));
    }

    let cover_url = do_regenerate_show_cover(&state, id).await;

    Ok(Json(serde_json::json!({
        "success": true,
        "cover_url": cover_url
    })))
}

#[derive(Debug, Deserialize)]
pub struct AssignArtistToShowRequest {
    artist_id: i64,
}

/// Assign an artist to a show (from the show's perspective)
pub async fn api_show_assign_artist(
    State(state): State<Arc<AppState>>,
    Path(show_id): Path<i64>,
    headers: HeaderMap,
    Json(req): Json<AssignArtistToShowRequest>,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    // Check current assignment count
    let count: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM artist_show_assignments WHERE show_id = ?")
            .bind(show_id)
            .fetch_one(&state.db)
            .await?;

    if count >= 4 {
        return Err(AppError::BadRequest(
            "Show already has 4 artists assigned".to_string(),
        ));
    }

    sqlx::query(
        "INSERT OR IGNORE INTO artist_show_assignments (artist_id, show_id, sort_order) \
         VALUES (?, ?, (SELECT COALESCE(MAX(sort_order), -1) + 1 FROM artist_show_assignments WHERE show_id = ?))",
    )
    .bind(req.artist_id)
    .bind(show_id)
    .bind(show_id)
    .execute(&state.db)
    .await?;

    // Fetch the artist with file keys to return
    let artist: ArtistWithFileKeys = sqlx::query_as(
        "SELECT id, name, pronouns, pic_key, pic_cropped_key, pic_overlay_key, voice_message_key, track1_key, track2_key FROM artists WHERE id = ?"
    )
    .bind(req.artist_id)
    .fetch_one(&state.db)
    .await?;

    // Generate presigned URLs
    let pic_url = if let Some(ref key) = artist.pic_cropped_key {
        storage::get_presigned_url(&state, key, 3600).await.ok()
    } else {
        None
    };
    let voice_url = if let Some(ref key) = artist.voice_message_key {
        storage::get_presigned_url(&state, key, 3600).await.ok()
    } else {
        None
    };
    let track1_url = if let Some(ref key) = artist.track1_key {
        storage::get_presigned_url(&state, key, 3600).await.ok()
    } else {
        None
    };
    let track2_url = if let Some(ref key) = artist.track2_key {
        storage::get_presigned_url(&state, key, 3600).await.ok()
    } else {
        None
    };
    // Try to get peaks URLs
    let track1_peaks_url = if let Some(ref key) = artist.track1_key {
        let peaks_key = derive_peaks_key(key);
        storage::get_presigned_url(&state, &peaks_key, 3600)
            .await
            .ok()
    } else {
        None
    };
    let track2_peaks_url = if let Some(ref key) = artist.track2_key {
        let peaks_key = derive_peaks_key(key);
        storage::get_presigned_url(&state, &peaks_key, 3600)
            .await
            .ok()
    } else {
        None
    };
    let voice_peaks_url = if let Some(ref key) = artist.voice_message_key {
        let peaks_key = derive_peaks_key(key);
        storage::get_presigned_url(&state, &peaks_key, 3600)
            .await
            .ok()
    } else {
        None
    };

    let assigned_artist = AssignedArtistInfo {
        id: artist.id,
        name: artist.name,
        pronouns: artist.pronouns,
        pic_url,
        voice_url,
        track1_url,
        track2_url,
        track1_peaks_url,
        track2_peaks_url,
        voice_peaks_url,
        has_pic: artist.pic_key.is_some(),
    };

    // Schedule debounced cover regeneration (async, non-blocking)
    let state_for_bio = state.clone();
    schedule_cover_regeneration(state, show_id);

    // Generate AI show bio from assigned artists' bios (background)
    tokio::spawn(async move {
        if let Err(e) = ai::generate_and_store_show_bio(&state_for_bio, show_id).await {
            tracing::error!("Failed to generate show bio for show {}: {}", show_id, e);
        }
    });

    Ok(Json(
        serde_json::json!({ "success": true, "artist": assigned_artist }),
    ))
}

/// Unassign an artist from a show (from the show's perspective)
pub async fn api_show_unassign_artist(
    State(state): State<Arc<AppState>>,
    Path((show_id, artist_id)): Path<(i64, i64)>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    sqlx::query("DELETE FROM artist_show_assignments WHERE show_id = ? AND artist_id = ?")
        .bind(show_id)
        .bind(artist_id)
        .execute(&state.db)
        .await?;

    // Schedule debounced cover regeneration (async, non-blocking)
    let state_for_bio = state.clone();
    schedule_cover_regeneration(state, show_id);

    // Regenerate AI show bio from remaining artists' bios (background)
    tokio::spawn(async move {
        if let Err(e) = ai::generate_and_store_show_bio(&state_for_bio, show_id).await {
            tracing::error!("Failed to generate show bio for show {}: {}", show_id, e);
        }
    });

    Ok(Json(serde_json::json!({ "success": true })))
}

// ============================================================================
// Show Host Assignment (external/brunchtime shows)
// ============================================================================

#[derive(Debug, Deserialize)]
pub struct AssignHostRequest {
    user_id: i64,
}

/// Assign a host user to a show (for external/brunchtime shows)
pub async fn api_show_assign_host(
    State(state): State<Arc<AppState>>,
    Path(show_id): Path<i64>,
    headers: HeaderMap,
    Json(req): Json<AssignHostRequest>,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    // Verify show exists and is not UNHEARD
    let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(show_id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound("Show not found".to_string()))?;

    if show.show_type == "unheard" {
        return Err(AppError::BadRequest(
            "UNHEARD shows use artist assignments, not host assignments".to_string(),
        ));
    }

    // Verify user exists and has role 'host' or 'admin'
    let user: Option<models::User> = sqlx::query_as("SELECT * FROM users WHERE id = ?")
        .bind(req.user_id)
        .fetch_optional(&state.db)
        .await?;

    let user = user.ok_or_else(|| AppError::NotFound("User not found".to_string()))?;
    if user.role != "host" && user.role != "admin" {
        return Err(AppError::BadRequest(
            "Only users with role 'host' or 'admin' can be assigned to shows".to_string(),
        ));
    }

    sqlx::query("UPDATE shows SET host_user_id = ?, updated_at = datetime('now') WHERE id = ?")
        .bind(req.user_id)
        .bind(show_id)
        .execute(&state.db)
        .await?;

    Ok(Json(serde_json::json!({
        "success": true,
        "host_user_id": req.user_id,
        "host_username": user.username,
    })))
}

/// Unassign the host from a show
pub async fn api_show_unassign_host(
    State(state): State<Arc<AppState>>,
    Path(show_id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    sqlx::query("UPDATE shows SET host_user_id = NULL, updated_at = datetime('now') WHERE id = ?")
        .bind(show_id)
        .execute(&state.db)
        .await?;

    Ok(Json(serde_json::json!({ "success": true })))
}

// ============================================================================
// Users API
// ============================================================================

#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct UserListItem {
    id: i64,
    username: String,
    role: String,
    expires_at: Option<String>,
    created_at: String,
    linked_artist_id: Option<i64>,
    linked_artist_name: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct UsersListResponse {
    users: Vec<UserListItem>,
}

pub async fn api_users_list(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    let users: Vec<UserListItem> = sqlx::query_as(
        "SELECT u.id, u.username, u.role, u.expires_at, u.created_at, \
         a.id AS linked_artist_id, a.name AS linked_artist_name \
         FROM users u LEFT JOIN artists a ON a.user_id = u.id \
         ORDER BY u.created_at DESC",
    )
    .fetch_all(&state.db)
    .await?;

    Ok(Json(UsersListResponse { users }))
}

#[derive(Debug, Deserialize)]
pub struct CreateUserRequest {
    username: String,
    role: String,
    expires_at: Option<String>,
    artist_id: Option<i64>,
}

#[derive(Debug, Serialize)]
pub struct CreateUserResponse {
    user: UserListItem,
    password: String,
}

pub async fn api_create_user(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(req): Json<CreateUserRequest>,
) -> Result<impl IntoResponse> {
    let current_user = require_admin(&state, &headers).await?;

    // Only superadmin can create superadmin users
    if req.role == "superadmin" && current_user.role != "superadmin" {
        return Err(AppError::Forbidden(
            "Only superadmins can create superadmin users".to_string(),
        ));
    }

    // Host users must have an expiration date
    if req.role == "host" && req.expires_at.is_none() {
        return Err(AppError::BadRequest(
            "Expiration date is required for host users".to_string(),
        ));
    }

    // Generate random password
    let password = auth::generate_session_token()[..16].to_string();
    let password_hash = auth::hash_password(&password)?;

    let result = sqlx::query(
        "INSERT INTO users (username, password_hash, role, expires_at, must_change_password) \
         VALUES (?, ?, ?, ?, 1)",
    )
    .bind(&req.username)
    .bind(&password_hash)
    .bind(&req.role)
    .bind(&req.expires_at)
    .execute(&state.db)
    .await?;

    let user_id = result.last_insert_rowid();

    // Link to artist profile if requested
    if let Some(artist_id) = req.artist_id {
        // Validate artist exists and is not already linked
        let existing: Option<(Option<i64>,)> =
            sqlx::query_as("SELECT user_id FROM artists WHERE id = ?")
                .bind(artist_id)
                .fetch_optional(&state.db)
                .await?;

        match existing {
            None => {
                return Err(AppError::NotFound("Artist not found".to_string()));
            }
            Some((Some(_),)) => {
                return Err(AppError::BadRequest(
                    "Artist is already linked to another user".to_string(),
                ));
            }
            Some((None,)) => {
                sqlx::query("UPDATE artists SET user_id = ? WHERE id = ?")
                    .bind(user_id)
                    .bind(artist_id)
                    .execute(&state.db)
                    .await?;
            }
        }
    }

    // Fetch the created user
    let user: UserListItem = sqlx::query_as(
        "SELECT u.id, u.username, u.role, u.expires_at, u.created_at, \
             a.id AS linked_artist_id, a.name AS linked_artist_name \
             FROM users u LEFT JOIN artists a ON a.user_id = u.id \
             WHERE u.id = ?",
    )
    .bind(user_id)
    .fetch_one(&state.db)
    .await?;

    Ok((
        StatusCode::CREATED,
        Json(CreateUserResponse { user, password }),
    ))
}

#[derive(Debug, Deserialize)]
pub struct CreateGuestRequest {
    username: String,
    /// The date (YYYY-MM-DD) on which the guest may log in — the show's date.
    login_date: String,
}

#[derive(Debug, Serialize)]
pub struct CreateGuestResponse {
    user_id: i64,
    username: String,
    /// One-time bootstrap password; the guest must replace it on first login.
    password: String,
    login_date: String,
}

/// Create a date-restricted guest account during show setup.
///
/// Available to any show creator (host or admin). The guest may only log in on
/// `login_date` (the show date) and is auto-removed after it expires.
pub async fn api_create_guest(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(req): Json<CreateGuestRequest>,
) -> Result<impl IntoResponse> {
    let current_user = require_show_creator(&state, &headers).await?;

    let username = req.username.trim();
    if username.is_empty() {
        return Err(AppError::BadRequest("Username is required".to_string()));
    }

    // Validate login_date is a plausible YYYY-MM-DD.
    if chrono::NaiveDate::parse_from_str(&req.login_date, "%Y-%m-%d").is_err() {
        return Err(AppError::BadRequest(
            "login_date must be a valid YYYY-MM-DD date".to_string(),
        ));
    }

    // Reject duplicate usernames up front for a clean error message.
    let existing: Option<(i64,)> = sqlx::query_as("SELECT id FROM users WHERE username = ?")
        .bind(username)
        .fetch_optional(&state.db)
        .await?;
    if existing.is_some() {
        return Err(AppError::BadRequest(
            "A user with this username already exists".to_string(),
        ));
    }

    // Generate a one-time bootstrap password (replaced on first login).
    let password = auth::generate_session_token()[..16].to_string();
    let password_hash = auth::hash_password(&password)?;

    // Expire at the end of the show day so the weekly cleanup removes the guest.
    let expires_at = format!("{}T23:59:59", req.login_date);

    let result = sqlx::query(
        "INSERT INTO users (username, password_hash, role, created_by, expires_at, must_change_password, login_date) \
         VALUES (?, ?, 'guest', ?, ?, 1, ?)",
    )
    .bind(username)
    .bind(&password_hash)
    .bind(current_user.id)
    .bind(&expires_at)
    .bind(&req.login_date)
    .execute(&state.db)
    .await?;

    Ok((
        StatusCode::CREATED,
        Json(CreateGuestResponse {
            user_id: result.last_insert_rowid(),
            username: username.to_string(),
            password,
            login_date: req.login_date,
        }),
    ))
}

pub async fn api_delete_user(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let current_user = require_admin(&state, &headers).await?;

    // Can't delete yourself
    if current_user.id == id {
        return Err(AppError::BadRequest(
            "Cannot delete your own account".to_string(),
        ));
    }

    // Only superadmin can delete superadmin users
    let target_user: Option<models::User> = sqlx::query_as("SELECT * FROM users WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?;

    if let Some(target) = target_user {
        // Only superadmin can delete admin or superadmin users
        if (target.role == "superadmin" || target.role == "admin")
            && current_user.role != "superadmin"
        {
            return Err(AppError::Forbidden(
                "Only superadmins can delete admin users".to_string(),
            ));
        }
    }

    sqlx::query("DELETE FROM users WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await?;

    Ok(Json(serde_json::json!({ "success": true })))
}

#[derive(Deserialize)]
pub struct UpdateUserRequest {
    role: Option<String>,
    expires_at: Option<String>,
    artist_id: Option<serde_json::Value>,
}

pub async fn api_update_user(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    Json(req): Json<UpdateUserRequest>,
) -> Result<impl IntoResponse> {
    let current_user = require_admin(&state, &headers).await?;

    // Get target user
    let target_user: Option<models::User> = sqlx::query_as("SELECT * FROM users WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?;

    let target = target_user.ok_or_else(|| AppError::NotFound("User not found".to_string()))?;

    // Check role hierarchy - can only edit users below your level
    let role_level = |role: &str| match role {
        "host" => 1,
        "admin" => 2,
        "superadmin" => 3,
        _ => 0,
    };

    if role_level(&target.role) >= role_level(&current_user.role) {
        return Err(AppError::Forbidden(
            "Cannot edit users at or above your role level".to_string(),
        ));
    }

    // If changing role, check if new role is below current user's level
    if let Some(ref new_role) = req.role {
        if role_level(new_role) >= role_level(&current_user.role) {
            return Err(AppError::Forbidden(
                "Cannot assign a role at or above your level".to_string(),
            ));
        }
    }

    // Determine final role (either new role or existing)
    let final_role = req.role.as_ref().unwrap_or(&target.role);

    // Host users must have an expiration date
    if final_role == "host" {
        // If changing to host or already host, check expires_at
        let final_expires_at = req.expires_at.as_ref().or(target.expires_at.as_ref());
        if final_expires_at.is_none() {
            return Err(AppError::BadRequest(
                "Expiration date is required for host users".to_string(),
            ));
        }
    }

    // Update user
    if let Some(role) = &req.role {
        sqlx::query("UPDATE users SET role = ? WHERE id = ?")
            .bind(role)
            .bind(id)
            .execute(&state.db)
            .await?;
    }

    if let Some(expires_at) = &req.expires_at {
        sqlx::query("UPDATE users SET expires_at = ? WHERE id = ?")
            .bind(expires_at)
            .bind(id)
            .execute(&state.db)
            .await?;
    }

    // Handle artist linking: null to unlink, number to link
    if let Some(ref artist_id_val) = req.artist_id {
        // First, unlink any currently linked artist
        sqlx::query("UPDATE artists SET user_id = NULL WHERE user_id = ?")
            .bind(id)
            .execute(&state.db)
            .await?;

        // If a new artist_id is provided (not null), link it
        if let Some(artist_id) = artist_id_val.as_i64() {
            let existing: Option<(Option<i64>,)> =
                sqlx::query_as("SELECT user_id FROM artists WHERE id = ?")
                    .bind(artist_id)
                    .fetch_optional(&state.db)
                    .await?;

            match existing {
                None => {
                    return Err(AppError::NotFound("Artist not found".to_string()));
                }
                Some((Some(existing_uid),)) if existing_uid != id => {
                    return Err(AppError::BadRequest(
                        "Artist is already linked to another user".to_string(),
                    ));
                }
                _ => {
                    sqlx::query("UPDATE artists SET user_id = ? WHERE id = ?")
                        .bind(id)
                        .bind(artist_id)
                        .execute(&state.db)
                        .await?;
                }
            }
        }
    }

    // Fetch updated user
    let user: UserListItem = sqlx::query_as(
        "SELECT u.id, u.username, u.role, u.expires_at, u.created_at, \
             a.id AS linked_artist_id, a.name AS linked_artist_name \
             FROM users u LEFT JOIN artists a ON a.user_id = u.id \
             WHERE u.id = ?",
    )
    .bind(id)
    .fetch_one(&state.db)
    .await?;

    Ok(Json(serde_json::json!({ "user": user })))
}

#[derive(Serialize)]
pub struct ResetPasswordResponse {
    password: String,
}

pub async fn api_reset_password(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let current_user = require_admin(&state, &headers).await?;

    // Get target user
    let target_user: Option<models::User> = sqlx::query_as("SELECT * FROM users WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?;

    let target = target_user.ok_or_else(|| AppError::NotFound("User not found".to_string()))?;

    // Check role hierarchy
    let role_level = |role: &str| match role {
        "host" => 1,
        "admin" => 2,
        "superadmin" => 3,
        _ => 0,
    };

    if role_level(&target.role) >= role_level(&current_user.role) {
        return Err(AppError::Forbidden(
            "Cannot reset password for users at or above your role level".to_string(),
        ));
    }

    // Generate new bootstrap password; force the user to choose their own on next login
    let password = auth::generate_session_token()[..16].to_string();
    let password_hash = auth::hash_password(&password)?;

    sqlx::query("UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?")
        .bind(&password_hash)
        .bind(id)
        .execute(&state.db)
        .await?;

    Ok(Json(ResetPasswordResponse { password }))
}

// ============================================================================
// Show Recording Upload
// ============================================================================

pub async fn api_upload_show_recording(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    mut multipart: axum::extract::Multipart,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    // Fetch show to get date and title
    let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound("Show not found".to_string()))?;

    // Process uploaded file
    let mut file_data: Option<(String, Vec<u8>, String)> = None;
    let mut peaks_data: Option<String> = None;

    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|e| AppError::BadRequest(format!("Failed to read multipart: {}", e)))?
    {
        let name = field.name().unwrap_or("").to_string();
        if name == "file" {
            let filename = field.file_name().unwrap_or("recording").to_string();
            let content_type = field
                .content_type()
                .unwrap_or("application/octet-stream")
                .to_string();
            let data = field
                .bytes()
                .await
                .map_err(|e| AppError::BadRequest(format!("Failed to read file: {}", e)))?
                .to_vec();
            file_data = Some((filename, data, content_type));
        } else if name == "peaks" {
            peaks_data = Some(
                field
                    .text()
                    .await
                    .map_err(|e| AppError::BadRequest(format!("Failed to read peaks: {}", e)))?,
            );
        }
    }

    let (filename, data, content_type) =
        file_data.ok_or_else(|| AppError::BadRequest("No file provided".to_string()))?;

    // Upload to recordings/DATE-TITLE.ext
    let key = storage::upload_show_recording(
        &state,
        &show.date,
        &show.title,
        &filename,
        data,
        &content_type,
    )
    .await?;

    // Store peaks JSON alongside the recording if provided
    let mut recording_peaks_url: Option<String> = None;
    if let Some(peaks_json) = peaks_data {
        let peaks_key = format!(
            "{}.peaks.json",
            key.rsplit_once('.').map(|(base, _)| base).unwrap_or(&key)
        );
        state
            .s3_client
            .put_object()
            .bucket(&state.config.r2_bucket_name)
            .key(&peaks_key)
            .body(aws_sdk_s3::primitives::ByteStream::from(
                peaks_json.into_bytes(),
            ))
            .content_type("application/json")
            .send()
            .await
            .map_err(|e| AppError::Storage(format!("Failed to upload peaks: {}", e)))?;

        recording_peaks_url = storage::get_presigned_url(&state, &peaks_key, 3600)
            .await
            .ok();
        tracing::debug!("Uploaded recording peaks at {}", peaks_key);
    }

    // Save recording_key and original filename in the database
    sqlx::query("UPDATE shows SET recording_key = ?, recording_filename = ? WHERE id = ?")
        .bind(&key)
        .bind(&filename)
        .bind(id)
        .execute(&state.db)
        .await?;

    // Auto-upload to SoundCloud in background (fire-and-forget)
    if crate::soundcloud::has_token(&state).await {
        let sc_state = state.clone();
        let sc_show_id = id;
        tokio::spawn(async move {
            tracing::info!(
                show_id = sc_show_id,
                "Auto-uploading recording to SoundCloud"
            );
            match crate::soundcloud::upload_track(&sc_state, sc_show_id).await {
                Ok(result) if result.success => {
                    tracing::info!(
                        show_id = sc_show_id,
                        track_id = ?result.track_id,
                        "SoundCloud auto-upload succeeded"
                    );
                    // Notify admin via Telegram
                    if let Some(ref url) = result.track_url {
                        let title = match sqlx::query_as::<_, crate::models::Show>(
                            "SELECT * FROM shows WHERE id = ?",
                        )
                        .bind(sc_show_id)
                        .fetch_one(&sc_state.db)
                        .await
                        {
                            Ok(show) => crate::soundcloud::build_title(&sc_state, &show)
                                .await
                                .unwrap_or_else(|_| format!("Show #{sc_show_id}")),
                            Err(_) => format!("Show #{sc_show_id}"),
                        };
                        telegram_notify::notify_soundcloud_upload(
                            &sc_state, sc_show_id, &title, url,
                        );
                    }
                }
                Ok(result) => {
                    tracing::warn!(
                        show_id = sc_show_id,
                        error = ?result.error,
                        "SoundCloud auto-upload failed"
                    );
                }
                Err(e) => {
                    tracing::error!(
                        show_id = sc_show_id,
                        error = %e,
                        "SoundCloud auto-upload error"
                    );
                }
            }
        });
    }

    // Generate presigned URL for the uploaded file
    let recording_url = storage::get_presigned_url(&state, &key, 3600).await.ok();

    Ok(Json(serde_json::json!({
        "success": true,
        "key": key,
        "recording_url": recording_url,
        "recording_peaks_url": recording_peaks_url,
    })))
}

// ============================================================================
// Show Recording Delete
// ============================================================================

pub async fn api_delete_show_recording(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    // Fetch show to get recording_key
    let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound("Show not found".to_string()))?;

    // Check if there's a recording to delete
    let recording_key = show
        .recording_key
        .ok_or_else(|| AppError::BadRequest("No recording exists for this show".to_string()))?;

    // Delete recording file from S3
    state
        .s3_client
        .delete_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&recording_key)
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to delete recording: {}", e)))?;

    // Also try to delete peaks file if it exists
    let peaks_key = format!(
        "{}.peaks.json",
        recording_key
            .rsplit_once('.')
            .map(|(base, _)| base)
            .unwrap_or(&recording_key)
    );
    let _ = state
        .s3_client
        .delete_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&peaks_key)
        .send()
        .await;

    // Clear recording_key and recording_filename in the database
    sqlx::query("UPDATE shows SET recording_key = NULL, recording_filename = NULL WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await?;

    tracing::info!("Deleted recording for show {}: {}", id, recording_key);

    Ok(Json(serde_json::json!({
        "success": true,
    })))
}

// ============================================================================
// Instagram Post
// ============================================================================

#[derive(Debug, Deserialize)]
pub struct InstagramPostRequest {
    /// If true, post even if already posted before
    #[serde(default)]
    force: bool,
    /// Instagram account to post to: "dev" (moafunk_tester) or "prod" (moafunk_radio)
    #[serde(default = "default_instagram_account")]
    account: String,
}

fn default_instagram_account() -> String {
    "dev".to_string()
}

#[derive(Debug, Serialize)]
pub struct InstagramPostResponse {
    success: bool,
    media_id: Option<String>,
    permalink: Option<String>,
    error: Option<String>,
    already_posted: bool,
}

pub async fn api_post_show_to_instagram(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    Json(req): Json<InstagramPostRequest>,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    // Fetch show
    let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound("Show not found".to_string()))?;

    // Check if already posted (unless force=true)
    if show.instagram_posted_at.is_some() && !req.force {
        return Ok(Json(InstagramPostResponse {
            success: false,
            media_id: None,
            permalink: None,
            error: Some(
                "This show was already posted to Instagram. Use force=true to post again."
                    .to_string(),
            ),
            already_posted: true,
        }));
    }

    // Post to Instagram
    let result = crate::instagram::post_show_to_instagram(&state, &show, &req.account).await?;

    if result.success {
        // Update instagram_posted_at timestamp and store permalink
        sqlx::query("UPDATE shows SET instagram_posted_at = datetime('now'), instagram_post_url = ? WHERE id = ?")
            .bind(&result.permalink)
            .bind(id)
            .execute(&state.db)
            .await?;

        tracing::info!(
            "Posted show {} to Instagram (account={}): {:?}",
            id,
            req.account,
            result.media_id
        );

        // Notify via Telegram
        crate::telegram_notify::notify_instagram_published(
            &state,
            show.id,
            &show.title,
            result.permalink.as_deref(),
        );
    }

    Ok(Json(InstagramPostResponse {
        success: result.success,
        media_id: result.media_id,
        permalink: result.permalink,
        error: result.error,
        already_posted: false,
    }))
}

// ============================================================================
// Telegram Preview
// ============================================================================

/// Send an Instagram post preview to Telegram with inline Publish/Edit buttons.
pub async fn api_send_telegram_preview(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    // Validate show exists and has a cover
    let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound("Show not found".to_string()))?;

    if show.cover_generated_at.is_none() {
        return Ok(Json(serde_json::json!({
            "success": false,
            "error": "Show has no cover image. Assign artists first."
        })));
    }

    match crate::telegram_notify::send_show_instagram_preview(&state, id).await {
        Ok(()) => Ok(Json(serde_json::json!({ "success": true }))),
        Err(e) => Ok(Json(serde_json::json!({
            "success": false,
            "error": e
        }))),
    }
}

// ============================================================================
// Show with Artists for Recording Page
// ============================================================================

/// Artist info for recording page with track URLs
#[derive(Debug, Serialize)]
pub struct RecordingArtistInfo {
    pub id: i64,
    pub name: String,
    pub pronouns: String,
    pub pic_url: Option<String>,
    pub voice_url: Option<String>,
    pub voice_key: Option<String>,
    pub track1_url: Option<String>,
    pub track1_key: Option<String>,
    pub track1_name: String,
    pub track2_url: Option<String>,
    pub track2_key: Option<String>,
    pub track2_name: String,
}

/// Response for show with artists endpoint (for recording page)
#[derive(Debug, Serialize)]
pub struct ShowWithArtistsResponse {
    pub id: i64,
    pub title: String,
    pub date: String,
    pub description: Option<String>,
    pub status: String,
    pub artists: Vec<RecordingArtistInfo>,
}

/// Helper struct to fetch artist data for recording
#[derive(Debug, sqlx::FromRow)]
struct RecordingArtistRow {
    id: i64,
    name: String,
    pronouns: String,
    pic_key: Option<String>,
    voice_message_key: Option<String>,
    track1_key: Option<String>,
    track1_name: String,
    track2_key: Option<String>,
    track2_name: String,
}

/// GET /api/shows/:id/with-artists
/// Returns show with assigned artists and their track URLs for the recording page
pub async fn api_show_with_artists(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    let show: Option<models::Show> = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?;

    let show = show.ok_or_else(|| AppError::NotFound("Show not found".to_string()))?;

    // Get assigned artists with their track keys
    let assigned_artists_raw: Vec<RecordingArtistRow> = sqlx::query_as(
        r#"
        SELECT a.id, a.name, a.pronouns, a.pic_key, a.voice_message_key,
               a.track1_key, a.track1_name, a.track2_key, a.track2_name
        FROM artists a
        INNER JOIN artist_show_assignments asa ON a.id = asa.artist_id
        WHERE asa.show_id = ?
        ORDER BY asa.sort_order, a.name COLLATE NOCASE
        "#,
    )
    .bind(id)
    .fetch_all(&state.db)
    .await?;

    // Generate presigned URLs for audio files and profile picture
    let mut artists = Vec::new();
    for a in assigned_artists_raw {
        let pic_url = if let Some(key) = &a.pic_key {
            storage::get_presigned_url(&state, key, 3600).await.ok()
        } else {
            None
        };
        let voice_url = if let Some(key) = &a.voice_message_key {
            storage::get_presigned_url(&state, key, 3600).await.ok()
        } else {
            None
        };
        let track1_url = if let Some(key) = &a.track1_key {
            storage::get_presigned_url(&state, key, 3600).await.ok()
        } else {
            None
        };
        let track2_url = if let Some(key) = &a.track2_key {
            storage::get_presigned_url(&state, key, 3600).await.ok()
        } else {
            None
        };
        artists.push(RecordingArtistInfo {
            id: a.id,
            name: a.name,
            pronouns: a.pronouns,
            pic_url,
            voice_url,
            voice_key: a.voice_message_key.clone(),
            track1_url,
            track1_key: a.track1_key.clone(),
            track1_name: a.track1_name,
            track2_url,
            track2_key: a.track2_key.clone(),
            track2_name: a.track2_name,
        });
    }

    Ok(Json(ShowWithArtistsResponse {
        id: show.id,
        title: show.title,
        date: show.date,
        description: show.description,
        status: show.status,
        artists,
    }))
}

// ============================================================================
// SoundCloud Upload
// ============================================================================

#[derive(Debug, Deserialize)]
pub struct SoundCloudPrivacyRequest {
    public: bool,
}

/// GET /api/soundcloud/status
/// Returns SoundCloud configuration and authorization status.
pub async fn api_soundcloud_status(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;
    let status = crate::soundcloud::get_status(&state).await;
    Ok(Json(status))
}

/// GET /api/soundcloud/auth
/// Redirects the admin to SoundCloud's OAuth authorization page.
pub async fn api_soundcloud_auth(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;
    let url = crate::soundcloud::get_auth_url(&state)?;
    Ok(axum::response::Redirect::temporary(&url))
}

/// GET /api/soundcloud/callback?code=xxx
/// OAuth callback — exchanges the code for an access token and stores it.
#[derive(Debug, Deserialize)]
pub struct SoundCloudCallbackQuery {
    code: String,
}

pub async fn api_soundcloud_callback(
    State(state): State<Arc<AppState>>,
    Query(query): Query<SoundCloudCallbackQuery>,
) -> impl IntoResponse {
    match crate::soundcloud::exchange_code(&state, &query.code).await {
        Ok(_) => axum::response::Html(
            r#"<!DOCTYPE html><html><body style="font-family:sans-serif;text-align:center;padding:40px">
            <h2>✅ SoundCloud Connected</h2>
            <p>You can close this tab and return to the admin panel.</p>
            <script>setTimeout(()=>window.close(),3000)</script>
            </body></html>"#
                .to_string(),
        ),
        Err(e) => axum::response::Html(format!(
            r#"<!DOCTYPE html><html><body style="font-family:sans-serif;text-align:center;padding:40px">
            <h2>❌ SoundCloud Authorization Failed</h2>
            <p>{}</p>
            </body></html>"#,
            e
        )),
    }
}

/// POST /api/shows/:id/soundcloud/upload
/// Manually trigger (or re-trigger) SoundCloud upload for a show's recording.
pub async fn api_upload_to_soundcloud(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    if !crate::soundcloud::is_configured(&state) {
        return Err(AppError::BadRequest(
            "SoundCloud is not configured".to_string(),
        ));
    }

    let result = crate::soundcloud::upload_track(&state, id).await?;

    // Notify admin via Telegram on success
    if result.success {
        if let Some(ref url) = result.track_url {
            let title = crate::soundcloud::build_title(
                &state,
                &sqlx::query_as::<_, crate::models::Show>("SELECT * FROM shows WHERE id = ?")
                    .bind(id)
                    .fetch_one(&state.db)
                    .await
                    .map_err(|e| AppError::Database(e))?,
            )
            .await
            .unwrap_or_else(|_| format!("Show #{id}"));
            telegram_notify::notify_soundcloud_upload(&state, id, &title, url);
        }
    }

    Ok(Json(result))
}

/// POST /api/shows/:id/soundcloud/privacy
/// Toggle a SoundCloud track between public and private.
pub async fn api_set_soundcloud_privacy(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    Json(req): Json<SoundCloudPrivacyRequest>,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    if !crate::soundcloud::is_configured(&state) {
        return Err(AppError::BadRequest(
            "SoundCloud is not configured".to_string(),
        ));
    }

    let result = crate::soundcloud::set_track_privacy(&state, id, req.public).await?;

    Ok(Json(result))
}

/// POST /api/soundcloud/disconnect
/// Clear the stored SoundCloud OAuth token, forcing re-authorization.
pub async fn api_soundcloud_disconnect(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;
    crate::soundcloud::delete_stored_token(&state).await?;
    Ok(Json(serde_json::json!({ "success": true })))
}

// ============================================================================
// Manual Cover Upload (for non-UNHEARD show types)
// ============================================================================

pub async fn api_upload_show_cover(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    mut multipart: axum::extract::Multipart,
) -> Result<impl IntoResponse> {
    // Admins (any show) or the assigned host (their own show) may replace the cover.
    let (_user, _show) = require_show_editor(&state, &headers, id).await?;

    // Process uploaded file
    let mut file_data: Option<Vec<u8>> = None;

    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|e| AppError::BadRequest(format!("Failed to read multipart field: {}", e)))?
    {
        let name = field.name().unwrap_or("").to_string();
        if name == "file" {
            let data = field
                .bytes()
                .await
                .map_err(|e| AppError::BadRequest(format!("Failed to read file: {}", e)))?
                .to_vec();
            file_data = Some(data);
        }
    }

    let data = file_data.ok_or_else(|| AppError::BadRequest("No file provided".to_string()))?;

    // Upload cover to S3
    let key = storage::upload_show_cover(&state, id, data).await?;

    // Update cover_generated_at timestamp
    let now = chrono::Utc::now().to_rfc3339();
    sqlx::query("UPDATE shows SET cover_generated_at = ? WHERE id = ?")
        .bind(&now)
        .bind(id)
        .execute(&state.db)
        .await?;

    // Return presigned URL
    let cover_url = storage::get_presigned_url(&state, &key, 3600).await.ok();

    Ok(Json(serde_json::json!({
        "success": true,
        "cover_url": cover_url,
        "cover_generated_at": now,
    })))
}

// ============================================================================
// Artist Flow — My Show
// ============================================================================

#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct MyShowArtist {
    id: i64,
    name: String,
    pronouns: String,
}

#[derive(Debug, Serialize)]
pub struct MyShowInfo {
    id: i64,
    title: String,
    date: String,
    start_time: Option<String>,
    end_time: Option<String>,
    description: Option<String>,
    show_type: String,
    artists: Vec<MyShowArtist>,
    cover_url: Option<String>,
    prerecorded_key: Option<String>,
    prerecorded_filename: Option<String>,
    prerecorded_url: Option<String>,
    prerecorded_confirmed_at: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct MyShowResponse {
    assigned: bool,
    shows: Vec<MyShowInfo>,
}

/// Helper: resolve ALL shows assigned to the current user.
/// Returns shows where the user is either directly assigned as host,
/// or linked via their artist profile.
async fn resolve_user_shows(
    state: &Arc<AppState>,
    user: &models::User,
) -> Result<Vec<models::Show>> {
    let mut all_shows: Vec<models::Show> = Vec::new();

    // Path 1: Direct host assignments (external/brunchtime shows)
    let direct_shows: Vec<models::Show> =
        sqlx::query_as("SELECT * FROM shows WHERE host_user_id = ? ORDER BY date DESC")
            .bind(user.id)
            .fetch_all(&state.db)
            .await?;
    all_shows.extend(direct_shows);

    // Path 2: Linked via artist profile (UNHEARD shows)
    let artist: Option<models::Artist> = sqlx::query_as("SELECT * FROM artists WHERE user_id = ?")
        .bind(user.id)
        .fetch_optional(&state.db)
        .await?;

    if let Some(artist) = artist {
        let artist_shows: Vec<models::Show> = sqlx::query_as(
            "SELECT s.* FROM shows s \
             INNER JOIN artist_show_assignments asa ON asa.show_id = s.id \
             WHERE asa.artist_id = ? \
             ORDER BY s.date DESC",
        )
        .bind(artist.id)
        .fetch_all(&state.db)
        .await?;

        // Deduplicate (a show could match both paths)
        for show in artist_shows {
            if !all_shows.iter().any(|s| s.id == show.id) {
                all_shows.push(show);
            }
        }
    }

    // Sort by date descending
    all_shows.sort_by(|a, b| b.date.cmp(&a.date));
    Ok(all_shows)
}

#[derive(Debug, Serialize)]
pub struct MyShowsListResponse {
    shows: Vec<ShowListItem>,
}

/// GET /api/my-shows — list all shows assigned to the authenticated user
pub async fn api_my_shows_list(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let token = auth::get_session_from_headers(&headers);
    let user = auth::get_current_user(&state, token.as_deref())
        .await
        .ok_or_else(|| AppError::Unauthorized("Not authenticated".to_string()))?;

    let shows = resolve_user_shows(&state, &user).await?;

    let mut show_items = Vec::new();
    for show in shows {
        let artists: Vec<ArtistBrief> = sqlx::query_as(
            "SELECT a.id, a.name FROM artists a \
             INNER JOIN artist_show_assignments asa ON a.id = asa.artist_id \
             WHERE asa.show_id = ? \
             ORDER BY asa.sort_order, a.name",
        )
        .bind(show.id)
        .fetch_all(&state.db)
        .await?;

        show_items.push(ShowListItem {
            id: show.id,
            title: show.title,
            date: show.date,
            start_time: show.start_time,
            end_time: show.end_time,
            description: show.description,
            status: show.status,
            show_type: show.show_type,
            artists,
        });
    }

    Ok(Json(MyShowsListResponse { shows: show_items }))
}

pub async fn api_my_show(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let token = auth::get_session_from_headers(&headers);
    let user = auth::get_current_user(&state, token.as_deref())
        .await
        .ok_or_else(|| AppError::Unauthorized("Not authenticated".to_string()))?;

    let all_shows = resolve_user_shows(&state, &user).await?;

    if all_shows.is_empty() {
        return Ok(Json(MyShowResponse {
            assigned: false,
            shows: vec![],
        }));
    }

    let mut show_infos = Vec::new();
    for show in all_shows {
        // Fetch all artists assigned to this show
        let artists: Vec<MyShowArtist> = sqlx::query_as(
            "SELECT a.id, a.name, a.pronouns FROM artists a \
             INNER JOIN artist_show_assignments asa ON asa.artist_id = a.id \
             WHERE asa.show_id = ? \
             ORDER BY asa.sort_order, a.name",
        )
        .bind(show.id)
        .fetch_all(&state.db)
        .await?;

        // Generate presigned URL for prerecorded file if it exists
        let prerecorded_url = if let Some(ref key) = show.prerecorded_key {
            storage::get_presigned_url(&state, key, 3600).await.ok()
        } else {
            None
        };

        // Generate presigned URL for cover image if it was generated
        let cover_url = if show.cover_generated_at.is_some() {
            let cover_key = format!("shows/{}/cover.png", show.id);
            storage::get_presigned_url(&state, &cover_key, 3600)
                .await
                .ok()
        } else {
            None
        };

        show_infos.push(MyShowInfo {
            id: show.id,
            title: show.title,
            date: show.date,
            start_time: show.start_time,
            end_time: show.end_time,
            description: show.description,
            show_type: show.show_type,
            artists,
            cover_url,
            prerecorded_key: show.prerecorded_key,
            prerecorded_filename: show.prerecorded_filename,
            prerecorded_url,
            prerecorded_confirmed_at: show.prerecorded_confirmed_at,
        });
    }

    Ok(Json(MyShowResponse {
        assigned: true,
        shows: show_infos,
    }))
}

#[derive(Debug, Deserialize)]
pub struct ShowIdQuery {
    pub show_id: i64,
}

/// Helper: authenticate user and resolve a specific show they're assigned to.
/// Requires `show_id` query parameter.
async fn require_user_show(
    state: &Arc<AppState>,
    headers: &HeaderMap,
    show_id: i64,
) -> Result<(models::User, models::Show)> {
    let token = auth::get_session_from_headers(headers);
    let user = auth::get_current_user(state, token.as_deref())
        .await
        .ok_or_else(|| AppError::Unauthorized("Not authenticated".to_string()))?;

    // Verify the user is assigned to this show
    let all_shows = resolve_user_shows(state, &user).await?;
    let show = all_shows
        .into_iter()
        .find(|s| s.id == show_id)
        .ok_or_else(|| AppError::BadRequest("You are not assigned to this show".to_string()))?;

    Ok((user, show))
}

// ─────────────────────────────────────────────────────────────────────────────
// Prerecorded upload — small file (≤50MB)
// ─────────────────────────────────────────────────────────────────────────────

pub async fn api_my_show_upload(
    State(state): State<Arc<AppState>>,
    Query(query): Query<ShowIdQuery>,
    headers: HeaderMap,
    mut multipart: axum::extract::Multipart,
) -> Result<impl IntoResponse> {
    let (_user, show) = require_user_show(&state, &headers, query.show_id).await?;

    let mut file_data: Option<(String, Vec<u8>, String)> = None;

    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|e| AppError::BadRequest(format!("Failed to read multipart: {}", e)))?
    {
        let name = field.name().unwrap_or("").to_string();
        if name == "file" {
            let filename = field.file_name().unwrap_or("prerecorded").to_string();
            let content_type = field
                .content_type()
                .unwrap_or("application/octet-stream")
                .to_string();
            let data = field
                .bytes()
                .await
                .map_err(|e| AppError::BadRequest(format!("Failed to read file: {}", e)))?
                .to_vec();
            file_data = Some((filename, data, content_type));
        }
    }

    let (filename, data, content_type) =
        file_data.ok_or_else(|| AppError::BadRequest("No file provided".to_string()))?;

    let key = upload_prerecorded_to_r2(&state, show.id, &filename, data, &content_type).await?;

    // Update show
    sqlx::query(
        "UPDATE shows SET prerecorded_key = ?, prerecorded_filename = ?, prerecorded_confirmed_at = NULL WHERE id = ?",
    )
    .bind(&key)
    .bind(&filename)
    .bind(show.id)
    .execute(&state.db)
    .await?;

    let prerecorded_url = storage::get_presigned_url(&state, &key, 3600).await.ok();

    Ok(Json(serde_json::json!({
        "success": true,
        "key": key,
        "prerecorded_url": prerecorded_url,
        "filename": filename,
    })))
}

// ─────────────────────────────────────────────────────────────────────────────
// Prerecorded upload — chunked: init
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Deserialize)]
pub struct PrerecordedInitRequest {
    pub filename: String,
    pub total_size: u64,
    pub total_chunks: u32,
}

pub async fn api_my_show_upload_init(
    State(state): State<Arc<AppState>>,
    Query(query): Query<ShowIdQuery>,
    headers: HeaderMap,
    Json(req): Json<PrerecordedInitRequest>,
) -> Result<impl IntoResponse> {
    let (_user, show) = require_user_show(&state, &headers, query.show_id).await?;

    tracing::info!(
        "Prerecorded upload init: show_id={}, filename={}, total_size={}, total_chunks={}",
        show.id,
        req.filename,
        req.total_size,
        req.total_chunks
    );

    let session_id = uuid::Uuid::new_v4().to_string();
    let expires_at = chrono::Utc::now() + chrono::Duration::hours(2);

    sqlx::query(
        r#"
        INSERT INTO pending_recording_uploads (
            session_id, show_id, filename, total_size, total_chunks, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        "#,
    )
    .bind(&session_id)
    .bind(show.id)
    .bind(&req.filename)
    .bind(req.total_size as i64)
    .bind(req.total_chunks as i32)
    .bind(expires_at.to_rfc3339())
    .execute(&state.db)
    .await?;

    Ok(Json(serde_json::json!({
        "success": true,
        "session_id": session_id,
        "message": format!("Upload initialized. Send {} chunks next.", req.total_chunks),
    })))
}

// ─────────────────────────────────────────────────────────────────────────────
// Prerecorded upload — chunked: chunk
// ─────────────────────────────────────────────────────────────────────────────

pub async fn api_my_show_upload_chunk(
    State(state): State<Arc<AppState>>,
    Path(session_id): Path<String>,
    Query(query): Query<super::upload_recording_chunked::ChunkQueryWithShowId>,
    headers: HeaderMap,
    mut multipart: axum::extract::Multipart,
) -> Result<impl IntoResponse> {
    let (_user, show) = require_user_show(&state, &headers, query.show_id).await?;

    let chunk_index = query.index;
    tracing::info!(
        "Prerecorded upload chunk {} for session_id={}, show_id={}",
        chunk_index,
        session_id,
        show.id
    );

    // Verify session exists and matches
    let row = sqlx::query(
        "SELECT show_id, total_chunks FROM pending_recording_uploads WHERE session_id = ? AND expires_at > datetime('now')",
    )
    .bind(&session_id)
    .fetch_optional(&state.db)
    .await?;

    let row = row.ok_or_else(|| {
        AppError::BadRequest("Upload session not found or expired. Please start over.".to_string())
    })?;

    let db_show_id: i64 = sqlx::Row::get(&row, "show_id");
    let total_chunks: i32 = sqlx::Row::get(&row, "total_chunks");

    if db_show_id != show.id {
        return Err(AppError::BadRequest(
            "Session does not match your show".to_string(),
        ));
    }

    if chunk_index >= total_chunks as u32 {
        return Err(AppError::BadRequest(format!(
            "Chunk index {} exceeds total chunks {}",
            chunk_index, total_chunks
        )));
    }

    // Read chunk data
    let mut chunk_data: Option<Vec<u8>> = None;
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|e| AppError::BadRequest(format!("Failed to read multipart: {}", e)))?
    {
        let name = field.name().unwrap_or("").to_string();
        if name == "chunk" || name == "file" {
            let data = field
                .bytes()
                .await
                .map_err(|e| AppError::BadRequest(format!("Failed to read chunk: {}", e)))?
                .to_vec();
            chunk_data = Some(data);
        }
    }

    let data =
        chunk_data.ok_or_else(|| AppError::BadRequest("No chunk data provided".to_string()))?;
    let received_bytes = data.len();

    // Store chunk in R2
    let chunk_key = format!(
        "pending-prerecorded/{}/chunk-{:04}",
        session_id, chunk_index
    );

    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&chunk_key)
        .body(aws_sdk_s3::primitives::ByteStream::from(data))
        .content_type("application/octet-stream")
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload chunk: {}", e)))?;

    sqlx::query(
        "INSERT OR REPLACE INTO pending_recording_chunks (session_id, chunk_index, chunk_key, size_bytes) VALUES (?, ?, ?, ?)",
    )
    .bind(&session_id)
    .bind(chunk_index as i32)
    .bind(&chunk_key)
    .bind(received_bytes as i64)
    .execute(&state.db)
    .await?;

    Ok(Json(serde_json::json!({
        "success": true,
        "index": chunk_index,
        "received_bytes": received_bytes,
    })))
}

// ─────────────────────────────────────────────────────────────────────────────
// Prerecorded upload — chunked: finalize
// ─────────────────────────────────────────────────────────────────────────────

pub async fn api_my_show_upload_finalize(
    State(state): State<Arc<AppState>>,
    Path(session_id): Path<String>,
    Query(query): Query<ShowIdQuery>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let (_user, show) = require_user_show(&state, &headers, query.show_id).await?;

    tracing::info!(
        "Prerecorded upload finalize for session_id={}, show_id={}",
        session_id,
        show.id
    );

    // Fetch session metadata
    let upload_row = sqlx::query(
        "SELECT show_id, filename, total_chunks FROM pending_recording_uploads WHERE session_id = ? AND expires_at > datetime('now')",
    )
    .bind(&session_id)
    .fetch_optional(&state.db)
    .await?;

    let upload_row = upload_row.ok_or_else(|| {
        AppError::BadRequest("Upload session not found or expired. Please start over.".to_string())
    })?;

    let db_show_id: i64 = sqlx::Row::get(&upload_row, "show_id");
    let filename: String = sqlx::Row::get(&upload_row, "filename");
    let total_chunks: i32 = sqlx::Row::get(&upload_row, "total_chunks");

    if db_show_id != show.id {
        return Err(AppError::BadRequest(
            "Session does not match your show".to_string(),
        ));
    }

    // Verify all chunks received
    let received_chunks: i32 =
        sqlx::query_scalar("SELECT COUNT(*) FROM pending_recording_chunks WHERE session_id = ?")
            .bind(&session_id)
            .fetch_one(&state.db)
            .await?;

    if received_chunks != total_chunks {
        return Err(AppError::BadRequest(format!(
            "Missing chunks: received {} of {} expected",
            received_chunks, total_chunks
        )));
    }

    // Fetch all chunk keys in order
    let chunk_rows: Vec<(String, i64)> = sqlx::query_as(
        "SELECT chunk_key, size_bytes FROM pending_recording_chunks WHERE session_id = ? ORDER BY chunk_index ASC",
    )
    .bind(&session_id)
    .fetch_all(&state.db)
    .await?;

    // Assemble chunks
    let mut assembled_data = Vec::new();
    for (chunk_key, _size) in &chunk_rows {
        let get_result = state
            .s3_client
            .get_object()
            .bucket(&state.config.r2_bucket_name)
            .key(chunk_key)
            .send()
            .await
            .map_err(|e| {
                AppError::Storage(format!("Failed to fetch chunk {}: {}", chunk_key, e))
            })?;

        let chunk_bytes = get_result
            .body
            .collect()
            .await
            .map_err(|e| AppError::Storage(format!("Failed to read chunk body: {}", e)))?
            .into_bytes();

        assembled_data.extend_from_slice(&chunk_bytes);
    }

    tracing::info!(
        "Assembled {} bytes from {} chunks",
        assembled_data.len(),
        chunk_rows.len()
    );

    // Determine content type
    let content_type = match filename
        .rsplit('.')
        .next()
        .unwrap_or("")
        .to_lowercase()
        .as_str()
    {
        "mp3" => "audio/mpeg",
        "wav" => "audio/wav",
        "flac" => "audio/flac",
        "ogg" => "audio/ogg",
        "m4a" | "aac" => "audio/mp4",
        _ => "application/octet-stream",
    };

    let key =
        upload_prerecorded_to_r2(&state, show.id, &filename, assembled_data, content_type).await?;

    // Update show
    sqlx::query(
        "UPDATE shows SET prerecorded_key = ?, prerecorded_filename = ?, prerecorded_confirmed_at = NULL WHERE id = ?",
    )
    .bind(&key)
    .bind(&filename)
    .bind(show.id)
    .execute(&state.db)
    .await?;

    // Clean up chunks
    for (chunk_key, _) in &chunk_rows {
        let _ = state
            .s3_client
            .delete_object()
            .bucket(&state.config.r2_bucket_name)
            .key(chunk_key)
            .send()
            .await;
    }

    sqlx::query("DELETE FROM pending_recording_chunks WHERE session_id = ?")
        .bind(&session_id)
        .execute(&state.db)
        .await?;

    sqlx::query("DELETE FROM pending_recording_uploads WHERE session_id = ?")
        .bind(&session_id)
        .execute(&state.db)
        .await?;

    let prerecorded_url = storage::get_presigned_url(&state, &key, 3600).await.ok();

    Ok(Json(serde_json::json!({
        "success": true,
        "key": key,
        "prerecorded_url": prerecorded_url,
        "filename": filename,
    })))
}

// ─────────────────────────────────────────────────────────────────────────────
// Prerecorded — confirm
// ─────────────────────────────────────────────────────────────────────────────

pub async fn api_my_show_confirm(
    State(state): State<Arc<AppState>>,
    Query(query): Query<ShowIdQuery>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let (_user, show) = require_user_show(&state, &headers, query.show_id).await?;

    if show.prerecorded_key.is_none() {
        return Err(AppError::BadRequest(
            "No prerecorded file uploaded yet".to_string(),
        ));
    }

    let now = chrono::Utc::now().to_rfc3339();
    sqlx::query("UPDATE shows SET prerecorded_confirmed_at = ? WHERE id = ?")
        .bind(&now)
        .bind(show.id)
        .execute(&state.db)
        .await?;

    tracing::info!("Prerecorded confirmed for show_id={}", show.id);

    Ok(Json(serde_json::json!({
        "success": true,
        "confirmed_at": now,
    })))
}

// ─────────────────────────────────────────────────────────────────────────────
// Prerecorded — go live (start streaming the uploaded file)
// ─────────────────────────────────────────────────────────────────────────────

pub async fn api_my_show_go_live(
    State(state): State<Arc<AppState>>,
    Query(query): Query<ShowIdQuery>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let (user, show) = require_user_show(&state, &headers, query.show_id).await?;

    // Must have a confirmed prerecorded file
    if show.prerecorded_key.is_none() {
        return Err(AppError::BadRequest(
            "No prerecorded file uploaded".to_string(),
        ));
    }
    if show.prerecorded_confirmed_at.is_none() {
        return Err(AppError::BadRequest(
            "Prerecorded file not confirmed yet".to_string(),
        ));
    }

    let key = show.prerecorded_key.as_ref().unwrap();

    // Generate a long-lived presigned URL for FFmpeg to read from (4 hours)
    let presigned_url = storage::get_presigned_url(&state, key, 4 * 3600).await?;
    let rtmp_destination = state.config.rtmp_destination();

    tracing::info!(
        "Starting prerecorded stream for show_id={}, user='{}', key='{}'",
        show.id,
        user.username,
        key
    );

    // Start the prerecorded stream via stream bridge
    crate::stream_bridge::start_prerecorded_stream(
        &state.stream_state,
        user.username.clone(),
        &presigned_url,
        &rtmp_destination,
    )
    .await
    .map_err(|e| AppError::Internal(format!("Failed to start prerecorded stream: {}", e)))?;

    // Notify via Telegram
    telegram_notify::notify_stream_start(&state, &user.username);

    Ok(Json(serde_json::json!({
        "success": true,
        "message": "Prerecorded stream started",
    })))
}

// ─────────────────────────────────────────────────────────────────────────────
// Prerecorded — delete (re-upload)
// ─────────────────────────────────────────────────────────────────────────────

pub async fn api_my_show_delete_upload(
    State(state): State<Arc<AppState>>,
    Query(query): Query<ShowIdQuery>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let (_user, show) = require_user_show(&state, &headers, query.show_id).await?;

    // Delete from R2 if exists
    if let Some(ref key) = show.prerecorded_key {
        let _ = state
            .s3_client
            .delete_object()
            .bucket(&state.config.r2_bucket_name)
            .key(key)
            .send()
            .await;
    }

    // Clear DB fields
    sqlx::query(
        "UPDATE shows SET prerecorded_key = NULL, prerecorded_filename = NULL, prerecorded_confirmed_at = NULL WHERE id = ?",
    )
    .bind(show.id)
    .execute(&state.db)
    .await?;

    tracing::info!("Prerecorded upload deleted for show_id={}", show.id);

    Ok(Json(serde_json::json!({ "success": true })))
}

/// Upload prerecorded file to R2 under the prerecorded/ prefix.
async fn upload_prerecorded_to_r2(
    state: &Arc<AppState>,
    show_id: i64,
    filename: &str,
    data: Vec<u8>,
    content_type: &str,
) -> Result<String> {
    let safe_filename = storage::sanitize_object_name(filename);
    let key = format!("prerecorded/{}/{}", show_id, safe_filename);

    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&key)
        .body(aws_sdk_s3::primitives::ByteStream::from(data))
        .content_type(content_type)
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload prerecorded file: {}", e)))?;

    Ok(key)
}

// ============================================================================
// Helpers
// ============================================================================

pub async fn require_admin(state: &Arc<AppState>, headers: &HeaderMap) -> Result<models::User> {
    let token = auth::get_session_from_headers(headers);
    let user = auth::get_current_user(state, token.as_deref())
        .await
        .ok_or_else(|| AppError::Unauthorized("Not authenticated".to_string()))?;

    if !user.role_enum().can_access_admin() {
        return Err(AppError::Forbidden("Admin access required".to_string()));
    }

    Ok(user)
}

/// Require an authenticated user allowed to create shows (host or admin).
///
/// Unlike [`require_admin`], hosts pass this check — they create a constrained,
/// self-assigned show (see [`api_create_show`]).
pub async fn require_show_creator(
    state: &Arc<AppState>,
    headers: &HeaderMap,
) -> Result<models::User> {
    let token = auth::get_session_from_headers(headers);
    let user = auth::get_current_user(state, token.as_deref())
        .await
        .ok_or_else(|| AppError::Unauthorized("Not authenticated".to_string()))?;

    if !user.role_enum().can_create_show() {
        return Err(AppError::Forbidden(
            "Host or admin access required to create shows".to_string(),
        ));
    }

    Ok(user)
}

/// Require a user allowed to EDIT a specific show: admins/superadmins (any show)
/// or the host assigned to this show. Returns the authenticated user and the
/// loaded show so the caller can reuse it.
pub async fn require_show_editor(
    state: &Arc<AppState>,
    headers: &HeaderMap,
    show_id: i64,
) -> Result<(models::User, models::Show)> {
    let token = auth::get_session_from_headers(headers);
    let user = auth::get_current_user(state, token.as_deref())
        .await
        .ok_or_else(|| AppError::Unauthorized("Not authenticated".to_string()))?;

    let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(show_id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound("Show not found".to_string()))?;

    let is_admin = user.role_enum().can_access_admin();
    let is_owner = show.host_user_id == Some(user.id);
    if !is_admin && !is_owner {
        return Err(AppError::Forbidden(
            "You can only edit shows you host".to_string(),
        ));
    }

    Ok((user, show))
}
