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
    }
    if let Some(key) = &artist.track1_key {
        if let Ok(url) = storage::get_presigned_url(&state, key, 3600).await {
            file_urls.insert("track1".to_string(), url);
        }
    }
    if let Some(key) = &artist.track2_key {
        if let Ok(url) = storage::get_presigned_url(&state, key, 3600).await {
            file_urls.insert("track2".to_string(), url);
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

#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct ArtistBrief {
    id: i64,
    name: String,
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

    let artists: Vec<ArtistBrief> = sqlx::query_as(
        r#"
        SELECT a.id, a.name FROM artists a
        INNER JOIN artist_show_assignments asa ON a.id = asa.artist_id
        WHERE asa.show_id = ?
        "#,
    )
    .bind(id)
    .fetch_all(&state.db)
    .await?;

    Ok(Json(ShowListItem {
        id: show.id,
        title: show.title,
        date: show.date,
        description: show.description,
        status: show.status,
        artists,
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

    Ok((
        StatusCode::CREATED,
        Json(serde_json::json!({
            "id": show_id,
            "title": req.title,
            "date": req.date,
            "description": req.description,
            "status": "scheduled",
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

    sqlx::query("DELETE FROM shows WHERE id = ?")
        .bind(id)
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
        if target.role == "superadmin" && current_user.role != "superadmin" {
            return Err(AppError::Forbidden(
                "Only superadmins can delete superadmin users".to_string(),
            ));
        }
    }

    sqlx::query("DELETE FROM users WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await?;

    Ok(Json(serde_json::json!({ "success": true })))
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
