//! JSON API handlers for the admin SPA
//!
//! These endpoints mirror the functionality of the template-based handlers
//! but return JSON responses for the Vue 3 admin panel.

use crate::{auth, models, storage, AppError, AppState, Result};
use axum::{
    extract::{Path, State},
    http::{header, HeaderMap, StatusCode},
    response::IntoResponse,
    Json,
};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

const MAX_ARTISTS_PER_SHOW: i64 = 4;

/// Derive the peaks JSON key from an audio file key.
/// E.g. "pending/abc123/track1/Artist - Track.mp3" -> "pending/abc123/track1_peaks/Artist - Track.peaks.json"
fn derive_peaks_key(audio_key: &str) -> String {
    // The peaks file is stored with "_peaks" suffix on the field type and ".peaks.json" extension
    // Original: pending/{session}/track1/{name}.{ext}
    // Peaks:    pending/{session}/track1_peaks/{name}.peaks.json
    if let Some(last_dot) = audio_key.rfind('.') {
        let base = &audio_key[..last_dot];
        // Replace the field type part (e.g., track1 -> track1_peaks)
        // Find the last occurrence of track1/, track2/, or voice/
        let key = if base.contains("/track1/") {
            base.replacen("/track1/", "/track1_peaks/", 1)
        } else if base.contains("/track2/") {
            base.replacen("/track2/", "/track2_peaks/", 1)
        } else if base.contains("/voice/") {
            base.replacen("/voice/", "/voice_peaks/", 1)
        } else {
            // Fallback: just append _peaks to directory
            base.to_string()
        };
        format!("{}.peaks.json", key)
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

    // Create session
    let token = auth::create_session(&state, user.id).await?;

    // Determine redirect based on role
    let redirect_url = match user.role_enum() {
        models::UserRole::Artist => "/#/stream",
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
        Some(u) => Ok(Json(UserResponse {
            id: u.id,
            username: u.username,
            role: u.role,
        })),
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

    // Hash and update password
    let new_hash = auth::hash_password(&req.new_password)?;
    sqlx::query("UPDATE users SET password_hash = ? WHERE id = ?")
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
        soundcloud: artist.soundcloud,
        instagram: artist.instagram,
        bandcamp: artist.bandcamp,
        spotify: artist.spotify,
        other_social: artist.other_social,
        track1_name: artist.track1_name,
        track2_name: artist.track2_name,
        file_urls,
        shows: shows
            .into_iter()
            .map(|s| ShowBrief {
                id: s.id,
                title: s.title,
                date: s.date,
            })
            .collect(),
        available_shows,
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

    sqlx::query("INSERT OR IGNORE INTO artist_show_assignments (artist_id, show_id) VALUES (?, ?)")
        .bind(artist_id)
        .bind(req.show_id)
        .execute(&state.db)
        .await?;

    Ok(Json(serde_json::json!({ "success": true })))
}

pub async fn api_unassign_artist_from_show(
    State(state): State<Arc<AppState>>,
    Path((artist_id, show_id)): Path<(i64, i64)>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    sqlx::query("DELETE FROM artist_show_assignments WHERE artist_id = ? AND show_id = ?")
        .bind(artist_id)
        .bind(show_id)
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
    .bind(&req.soundcloud)
    .bind(&req.instagram)
    .bind(&req.bandcamp)
    .bind(&req.spotify)
    .bind(&req.other_social)
    .bind(id)
    .execute(&state.db)
    .await?;

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
    }

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
    description: Option<String>,
    status: String,
    artists: Vec<ArtistBrief>,
}

#[derive(Debug, Serialize)]
pub struct ShowDetailResponse {
    id: i64,
    title: String,
    date: String,
    description: Option<String>,
    status: String,
    created_at: String,
    updated_at: Option<String>,
    artists: Vec<AssignedArtistInfo>,
    available_artists: Vec<ArtistWithPronouns>,
    artists_left: i32,
    cover_url: Option<String>,
    cover_generated_at: Option<String>,
    recording_url: Option<String>,
    recording_peaks_url: Option<String>,
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
            description: show.description,
            status: show.status,
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
    let cover_data = crate::image_overlay::build_show_collage(state, Vec::new())
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

/// Actually performs the cover regeneration (called after debounce)
async fn do_regenerate_show_cover(state: &Arc<AppState>, show_id: i64) -> Option<String> {
    // Get assigned artists with their pic keys
    let artists: Vec<ArtistWithFileKeys> = sqlx::query_as(
        r#"
        SELECT a.id, a.name, a.pronouns, a.pic_key, a.pic_cropped_key, a.pic_overlay_key,
               a.voice_message_key, a.track1_key, a.track2_key
        FROM artists a
        INNER JOIN artist_show_assignments asa ON a.id = asa.artist_id
        WHERE asa.show_id = ?
        ORDER BY a.name COLLATE NOCASE
        LIMIT 4
        "#,
    )
    .bind(show_id)
    .fetch_all(&state.db)
    .await
    .ok()?;

    // Collect artist images for collage (empty vec = all black tiles)
    let mut collage_items: Vec<(String, Vec<u8>, String)> = Vec::new();
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
                collage_items.push((artist.name.clone(), data, ext));
            }
        }
    }

    // Generate collage (works with 0-4 items, missing slots show as black)
    let collage_png = crate::image_overlay::build_show_collage(state, collage_items).await?;

    // Upload to S3
    let key = storage::upload_show_cover(state, show_id, collage_png)
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
    require_admin(&state, &headers).await?;

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
        ORDER BY a.name COLLATE NOCASE
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

    // Calculate artists left (max 4)
    let artists_left = (4 - artists.len() as i32).max(0);

    // Get available artists (NOT assigned to ANY show - truly unassigned)
    let available_artists: Vec<ArtistWithPronouns> = if artists_left > 0 {
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

    // Only return cover URL if cover was actually generated
    let cover_url = if show.cover_generated_at.is_some() {
        let cover_key = format!("shows/{}/cover.png", id);
        storage::get_presigned_url(&state, &cover_key, 3600)
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

    Ok(Json(ShowDetailResponse {
        id: show.id,
        title: show.title,
        date: show.date,
        description: show.description,
        status: show.status,
        created_at: show.created_at,
        updated_at: show.updated_at,
        artists,
        available_artists,
        artists_left,
        cover_url,
        cover_generated_at: show.cover_generated_at,
        recording_url,
        recording_peaks_url,
    }))
}

#[derive(Debug, Deserialize)]
pub struct CreateShowRequest {
    title: String,
    date: String,
    description: Option<String>,
}

pub async fn api_create_show(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(req): Json<CreateShowRequest>,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

    let result = sqlx::query(
        "INSERT INTO shows (title, date, description, status) VALUES (?, ?, ?, 'scheduled')",
    )
    .bind(&req.title)
    .bind(&req.date)
    .bind(&req.description)
    .execute(&state.db)
    .await?;

    let show_id = result.last_insert_rowid();

    // Copy default cover synchronously so it's ready when frontend loads
    let cover_url = if let Some(key) = copy_default_cover_to_show(&state, show_id).await {
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
            "description": req.description,
            "status": "scheduled",
            "cover_url": cover_url,
            "cover_generated_at": cover_generated_at,
        })),
    ))
}

#[derive(Debug, Deserialize)]
pub struct UpdateShowRequest {
    title: Option<String>,
    date: Option<String>,
    description: Option<String>,
}

pub async fn api_update_show(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    Json(req): Json<UpdateShowRequest>,
) -> Result<impl IntoResponse> {
    require_admin(&state, &headers).await?;

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

    sqlx::query("INSERT OR IGNORE INTO artist_show_assignments (artist_id, show_id) VALUES (?, ?)")
        .bind(req.artist_id)
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
    schedule_cover_regeneration(state, show_id);

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
    schedule_cover_regeneration(state, show_id);

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
        "SELECT id, username, role, expires_at, created_at FROM users ORDER BY created_at DESC",
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

    // Artist users must have an expiration date
    if req.role == "artist" && req.expires_at.is_none() {
        return Err(AppError::BadRequest(
            "Expiration date is required for artist users".to_string(),
        ));
    }

    // Generate random password
    let password = auth::generate_session_token()[..16].to_string();
    let password_hash = auth::hash_password(&password)?;

    let result = sqlx::query(
        "INSERT INTO users (username, password_hash, role, expires_at) VALUES (?, ?, ?, ?)",
    )
    .bind(&req.username)
    .bind(&password_hash)
    .bind(&req.role)
    .bind(&req.expires_at)
    .execute(&state.db)
    .await?;

    let user_id = result.last_insert_rowid();

    // Fetch the created user
    let user: UserListItem =
        sqlx::query_as("SELECT id, username, role, expires_at, created_at FROM users WHERE id = ?")
            .bind(user_id)
            .fetch_one(&state.db)
            .await?;

    Ok((
        StatusCode::CREATED,
        Json(CreateUserResponse { user, password }),
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
        "artist" => 1,
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

    // Artist users must have an expiration date
    if final_role == "artist" {
        // If changing to artist or already artist, check expires_at
        let final_expires_at = req.expires_at.as_ref().or(target.expires_at.as_ref());
        if final_expires_at.is_none() {
            return Err(AppError::BadRequest(
                "Expiration date is required for artist users".to_string(),
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

    // Fetch updated user
    let user: UserListItem =
        sqlx::query_as("SELECT id, username, role, expires_at, created_at FROM users WHERE id = ?")
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
        "artist" => 1,
        "admin" => 2,
        "superadmin" => 3,
        _ => 0,
    };

    if role_level(&target.role) >= role_level(&current_user.role) {
        return Err(AppError::Forbidden(
            "Cannot reset password for users at or above your role level".to_string(),
        ));
    }

    // Generate new password
    let password = auth::generate_session_token()[..16].to_string();
    let password_hash = auth::hash_password(&password)?;

    sqlx::query("UPDATE users SET password_hash = ? WHERE id = ?")
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

    // Save recording_key in the database
    sqlx::query("UPDATE shows SET recording_key = ? WHERE id = ?")
        .bind(&key)
        .bind(id)
        .execute(&state.db)
        .await?;

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

    // Clear recording_key in the database
    sqlx::query("UPDATE shows SET recording_key = NULL WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await?;

    tracing::info!("Deleted recording for show {}: {}", id, recording_key);

    Ok(Json(serde_json::json!({
        "success": true,
    })))
}

// ============================================================================
// Helpers
// ============================================================================

async fn require_admin(state: &Arc<AppState>, headers: &HeaderMap) -> Result<models::User> {
    let token = auth::get_session_from_headers(headers);
    let user = auth::get_current_user(state, token.as_deref())
        .await
        .ok_or_else(|| AppError::Unauthorized("Not authenticated".to_string()))?;

    if !user.role_enum().can_access_admin() {
        return Err(AppError::Forbidden("Admin access required".to_string()));
    }

    Ok(user)
}
