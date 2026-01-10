use crate::{auth, models, storage, AppError, AppState, Result};
use axum::{
    extract::{Path, State},
    http::Request,
    response::{Html, IntoResponse, Redirect, Response},
};
use std::sync::Arc;

const MAX_ARTISTS_PER_SHOW: i64 = 4;

fn derive_show_status(show_date: &str, today: &str) -> String {
    // Dates are normalized to `YYYY-MM-DD`, so lexicographic compares work.
    if show_date < today {
        "completed".to_string()
    } else {
        "scheduled".to_string()
    }
}

#[derive(Debug, sqlx::FromRow, serde::Serialize)]
struct AvailableShow {
    id: i64,
    title: String,
    date: String,
    artists_left: i64,
}

fn normalize_show_date(input: &str) -> String {
    let trimmed = input.trim();
    if trimmed.is_empty() {
        return String::new();
    }

    // Accept legacy values like `YYYY-MM-DDTHH:MM` or `YYYY-MM-DD HH:MM` and keep the date.
    let first = trimmed
        .split('T')
        .next()
        .unwrap_or(trimmed)
        .split(' ')
        .next()
        .unwrap_or(trimmed);

    if first.len() >= 10 {
        first.chars().take(10).collect()
    } else {
        first.to_string()
    }
}

fn redirect_with_flash(base: &str, kind: &str, msg: String) -> Response {
    let mut params = std::collections::BTreeMap::new();
    params.insert("kind".to_string(), kind.to_string());
    params.insert("msg".to_string(), msg);
    let qs = serde_urlencoded::to_string(params).unwrap_or_default();
    Redirect::to(&format!("{}?{}", base, qs)).into_response()
}

fn get_session_token<B>(request: &Request<B>) -> Option<String> {
    auth::get_session_from_cookies(request)
}

pub async fn index(
    State(state): State<Arc<AppState>>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;

    match user {
        Some(u) => {
            // Redirect based on role
            match u.role_enum() {
                models::UserRole::Artist => Ok(Redirect::to("/stream").into_response()),
                models::UserRole::Admin | models::UserRole::Superadmin => {
                    Ok(Redirect::to("/artists").into_response())
                }
            }
        }
        None => Ok(Redirect::to("/login").into_response()),
    }
}

pub async fn artists_list(
    State(state): State<Arc<AppState>>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;

    let user = match user {
        Some(u) => u,
        None => return Ok(Redirect::to("/login").into_response()),
    };

    // Only admin/superadmin can access artists page
    if !user.role_enum().can_access_admin() {
        return Ok(Redirect::to("/stream").into_response());
    }

    let query_params: std::collections::HashMap<String, String> = request
        .uri()
        .query()
        .and_then(|q| serde_urlencoded::from_str(q).ok())
        .unwrap_or_default();

    let assignment_filter = query_params
        .get("assignment_filter")
        .cloned()
        .filter(|s| !s.is_empty())
        .and_then(|s| match s.as_str() {
            "assigned" | "unassigned" => Some(s),
            _ => None,
        });

    let sort = query_params
        .get("sort")
        .map(|s| s.as_str())
        .unwrap_or("submitted");
    let dir = query_params
        .get("dir")
        .map(|s| s.as_str())
        .unwrap_or("desc");
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

    let flash_message = query_params.get("msg").cloned().filter(|s| !s.is_empty());
    let flash_kind = query_params.get("kind").cloned().filter(|s| !s.is_empty());

    #[derive(Debug, Clone, serde::Serialize, sqlx::FromRow)]
    struct ArtistListRow {
        pub id: i64,
        pub name: String,
        pub status: String,
        pub created_at: String,
        pub show_titles: Option<String>,
    }

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

    let query = format!(
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

    let artists: Vec<ArtistListRow> = sqlx::query_as(&query).fetch_all(&state.db).await?;

    let mut context = tera::Context::new();
    context.insert("artists", &artists);
    context.insert("assignment_filter", &assignment_filter);
    context.insert("flash_message", &flash_message);
    context.insert("flash_kind", &flash_kind);
    context.insert("sort", &sort);
    context.insert("dir", &dir.to_lowercase());
    context.insert("user_role", &user.role);
    context.insert("username", &user.username);

    let html = state.templates.render("artists.html", &context)?;
    Ok(Html(html).into_response())
}

pub async fn artist_detail(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;

    let user = match user {
        Some(u) => u,
        None => return Ok(Redirect::to("/login").into_response()),
    };

    // Only admin/superadmin can access artist detail
    if !user.role_enum().can_access_admin() {
        return Ok(Redirect::to("/stream").into_response());
    }

    let query_params: std::collections::HashMap<String, String> = request
        .uri()
        .query()
        .and_then(|q| serde_urlencoded::from_str(q).ok())
        .unwrap_or_default();
    let flash_message = query_params.get("msg").cloned().filter(|s| !s.is_empty());
    let flash_kind = query_params.get("kind").cloned().filter(|s| !s.is_empty());

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
    // Prefer branded > cropped > original for preview.
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

    // Get available shows for assignment (future/today, not already assigned, and with remaining slots)
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

    let artist_with_shows = models::ArtistWithShows { artist, shows };

    let mut context = tera::Context::new();
    context.insert("artist", &artist_with_shows);
    context.insert("file_urls", &file_urls);
    context.insert("available_shows", &available_shows);
    context.insert("flash_message", &flash_message);
    context.insert("flash_kind", &flash_kind);
    context.insert("user_role", &user.role);
    context.insert("username", &user.username);

    let html = state.templates.render("artist_detail.html", &context)?;
    Ok(Html(html).into_response())
}

pub async fn delete_artist(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;
    if user.is_none() || !user.as_ref().unwrap().role_enum().can_access_admin() {
        return Err(AppError::Unauthorized);
    }

    // Ensure the artist exists (also gives a nicer error than deleting 0 rows).
    let artist: Option<models::Artist> = sqlx::query_as("SELECT * FROM artists WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?;
    let artist = artist.ok_or_else(|| AppError::NotFound("Artist not found".to_string()))?;

    let redirect_with_flash = |kind: &str, msg: String| {
        let mut params = std::collections::BTreeMap::new();
        params.insert("kind".to_string(), kind.to_string());
        params.insert("msg".to_string(), msg);
        let qs = serde_urlencoded::to_string(params).unwrap_or_default();
        Redirect::to(&format!("/artists?{}", qs)).into_response()
    };

    // Delete ALL objects under this artist's prefix (covers historical/extra uploads too).
    let prefix = format!("artists/{}/", id);
    if let Err(e) = storage::delete_prefix(&state, &prefix).await {
        tracing::error!(artist_id = id, error = %e, "Failed to delete artist storage prefix");
        return Ok(redirect_with_flash(
            "error",
            format!(
                "Failed to delete files for '{}'. Please try again.",
                artist.name
            ),
        ));
    }

    // Delete DB row last.
    if let Err(e) = sqlx::query("DELETE FROM artists WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await
    {
        tracing::error!(artist_id = id, error = %e, "Failed to delete artist row");
        return Ok(redirect_with_flash(
            "error",
            format!(
                "Deleted files, but failed to delete '{}' from the database.",
                artist.name
            ),
        ));
    }

    Ok(redirect_with_flash(
        "success",
        format!("Deleted artist '{}' and all uploaded files.", artist.name),
    ))
}

pub async fn assign_show(
    State(state): State<Arc<AppState>>,
    Path(artist_id): Path<i64>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;
    if user.is_none() || !user.as_ref().unwrap().role_enum().can_access_admin() {
        return Err(AppError::Unauthorized);
    }

    let bytes = axum::body::to_bytes(request.into_body(), 1024)
        .await
        .map_err(|e| AppError::Validation(format!("Failed to read body: {}", e)))?;
    let form: models::AssignShowForm = serde_urlencoded::from_bytes(&bytes)
        .map_err(|e| AppError::Validation(format!("Failed to parse form: {}", e)))?;

    // If already assigned, keep it idempotent.
    let already_assigned: Option<i64> = sqlx::query_scalar(
        "SELECT 1 FROM artist_show_assignments WHERE artist_id = ? AND show_id = ? LIMIT 1",
    )
    .bind(artist_id)
    .bind(form.show_id)
    .fetch_optional(&state.db)
    .await?;
    if already_assigned.is_some() {
        return Ok(Redirect::to(&format!("/artists/{}", artist_id)).into_response());
    }

    // Enforce max artists per show.
    let current_count: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM artist_show_assignments WHERE show_id = ?")
            .bind(form.show_id)
            .fetch_one(&state.db)
            .await?;
    if current_count >= MAX_ARTISTS_PER_SHOW {
        return Ok(redirect_with_flash(
            &format!("/artists/{}", artist_id),
            "error",
            format!(
                "This show already has {} artists assigned.",
                MAX_ARTISTS_PER_SHOW
            ),
        ));
    }

    let mut tx = state.db.begin().await?;
    // One show per artist: reassign by replacing any existing assignment.
    sqlx::query("DELETE FROM artist_show_assignments WHERE artist_id = ?")
        .bind(artist_id)
        .execute(&mut *tx)
        .await?;

    if let Err(e) = sqlx::query(
        "INSERT OR IGNORE INTO artist_show_assignments (artist_id, show_id) VALUES (?, ?)",
    )
    .bind(artist_id)
    .bind(form.show_id)
    .execute(&mut *tx)
    .await
    {
        tx.rollback().await.ok();
        // Trigger-based enforcement can fail here; surface it as a user-facing message.
        tracing::warn!(artist_id, show_id = form.show_id, error = %e, "Failed to assign show");
        return Ok(redirect_with_flash(
            &format!("/artists/{}", artist_id),
            "error",
            "Could not assign: show already has 4 artists.".to_string(),
        ));
    }

    sqlx::query(
        "UPDATE artists SET status = 'assigned', updated_at = datetime('now') WHERE id = ?",
    )
    .bind(artist_id)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;

    Ok(Redirect::to(&format!("/artists/{}", artist_id)).into_response())
}

pub async fn unassign_show(
    State(state): State<Arc<AppState>>,
    Path((artist_id, show_id)): Path<(i64, i64)>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;
    if user.is_none() || !user.as_ref().unwrap().role_enum().can_access_admin() {
        return Err(AppError::Unauthorized);
    }

    sqlx::query("DELETE FROM artist_show_assignments WHERE artist_id = ? AND show_id = ?")
        .bind(artist_id)
        .bind(show_id)
        .execute(&state.db)
        .await?;

    sqlx::query(
        "UPDATE artists SET status = 'unassigned', updated_at = datetime('now') WHERE id = ?",
    )
    .bind(artist_id)
    .execute(&state.db)
    .await?;

    Ok(Redirect::to(&format!("/artists/{}", artist_id)).into_response())
}

pub async fn shows_list(
    State(state): State<Arc<AppState>>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;

    let user = match user {
        Some(u) => u,
        None => return Ok(Redirect::to("/login").into_response()),
    };

    // Only admin/superadmin can access shows page
    if !user.role_enum().can_access_admin() {
        return Ok(Redirect::to("/stream").into_response());
    }

    let query_params: std::collections::HashMap<String, String> = request
        .uri()
        .query()
        .and_then(|q| serde_urlencoded::from_str(q).ok())
        .unwrap_or_default();

    let status_filter = query_params
        .get("status_filter")
        .cloned()
        .filter(|s| s == "scheduled" || s == "completed");

    let flash_message = query_params.get("msg").cloned().filter(|s| !s.is_empty());
    let flash_kind = query_params.get("kind").cloned().filter(|s| !s.is_empty());

    let sort = query_params
        .get("sort")
        .map(|s| s.as_str())
        .unwrap_or("date");
    let dir_input = query_params
        .get("dir")
        .map(|s| s.as_str())
        .unwrap_or("desc");
    let dir = if dir_input.eq_ignore_ascii_case("asc") {
        "ASC"
    } else {
        "DESC"
    };
    let order_by = match sort {
        "title" => "title COLLATE NOCASE",
        "status" => "date", // apply status sort after deriving it
        "date" => "date",
        "artists" => "date", // sorted after counts are computed
        _ => "date",
    };

    let today: String = sqlx::query_scalar("SELECT date('now')")
        .fetch_one(&state.db)
        .await?;

    // Fetch first, then derive status and apply filter.
    let query = format!("SELECT * FROM shows ORDER BY {} {}, id DESC", order_by, dir);
    let mut shows: Vec<models::Show> = sqlx::query_as(&query).fetch_all(&state.db).await?;
    for show in &mut shows {
        show.status = derive_show_status(&show.date, &today);
    }
    if let Some(status) = &status_filter {
        shows.retain(|s| s.status == *status);
    }

    // If requested, sort by derived status.
    if sort == "status" {
        let ascending = dir == "ASC";
        shows.sort_by(|a, b| {
            if ascending {
                a.status
                    .cmp(&b.status)
                    .then_with(|| a.date.cmp(&b.date))
                    .then_with(|| a.id.cmp(&b.id))
            } else {
                b.status
                    .cmp(&a.status)
                    .then_with(|| b.date.cmp(&a.date))
                    .then_with(|| b.id.cmp(&a.id))
            }
        });
    }

    // Get artist counts for each show
    let mut shows_with_counts: Vec<serde_json::Value> = Vec::new();
    for show in shows {
        let count: i64 =
            sqlx::query_scalar("SELECT COUNT(*) FROM artist_show_assignments WHERE show_id = ?")
                .bind(show.id)
                .fetch_one(&state.db)
                .await?;

        let artists_left = (MAX_ARTISTS_PER_SHOW - count).max(0);

        shows_with_counts.push(serde_json::json!({
            "show": show,
            "artist_count": count,
            "artists_left": artists_left,
        }));
    }

    // If requested, sort by artist count after we computed it.
    if sort == "artists" {
        let ascending = dir == "ASC";
        shows_with_counts.sort_by(|a, b| {
            let ac = a.get("artist_count").and_then(|v| v.as_i64()).unwrap_or(0);
            let bc = b.get("artist_count").and_then(|v| v.as_i64()).unwrap_or(0);
            if ascending {
                ac.cmp(&bc)
            } else {
                bc.cmp(&ac)
            }
        });
    }

    let mut context = tera::Context::new();
    context.insert("shows", &shows_with_counts);
    context.insert("status_filter", &status_filter);
    context.insert("flash_message", &flash_message);
    context.insert("flash_kind", &flash_kind);
    context.insert("sort", &sort);
    context.insert("dir", &dir.to_lowercase());
    context.insert("user_role", &user.role);
    context.insert("username", &user.username);

    let html = state.templates.render("shows.html", &context)?;
    Ok(Html(html).into_response())
}

pub async fn delete_show(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;
    if user.is_none() || !user.as_ref().unwrap().role_enum().can_access_admin() {
        return Err(AppError::Unauthorized);
    }

    let redirect_with_flash = |kind: &str, msg: String| {
        let mut params = std::collections::BTreeMap::new();
        params.insert("kind".to_string(), kind.to_string());
        params.insert("msg".to_string(), msg);
        let qs = serde_urlencoded::to_string(params).unwrap_or_default();
        Redirect::to(&format!("/shows?{}", qs)).into_response()
    };

    let show: Option<models::Show> = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?;
    let show = show.ok_or_else(|| AppError::NotFound("Show not found".to_string()))?;

    if let Err(e) = sqlx::query("DELETE FROM shows WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await
    {
        tracing::error!(show_id = id, error = %e, "Failed to delete show row");
        return Ok(redirect_with_flash(
            "error",
            format!("Failed to delete show '{}'. Please try again.", show.title),
        ));
    }

    Ok(redirect_with_flash(
        "success",
        format!("Deleted show '{}' and removed all assignments.", show.title),
    ))
}

pub async fn create_show(
    State(state): State<Arc<AppState>>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;
    if user.is_none() || !user.as_ref().unwrap().role_enum().can_access_admin() {
        return Err(AppError::Unauthorized);
    }

    let bytes = axum::body::to_bytes(request.into_body(), 4096)
        .await
        .map_err(|e| AppError::Validation(format!("Failed to read body: {}", e)))?;
    let form: models::CreateShowForm = serde_urlencoded::from_bytes(&bytes)
        .map_err(|e| AppError::Validation(format!("Failed to parse form: {}", e)))?;

    let date = normalize_show_date(&form.date);
    if date.is_empty() {
        return Ok(redirect_with_flash(
            "/shows",
            "error",
            "Date is required.".to_string(),
        ));
    }

    sqlx::query("INSERT INTO shows (title, date, description) VALUES (?, ?, ?)")
        .bind(&form.title)
        .bind(&date)
        .bind(&form.description)
        .execute(&state.db)
        .await?;

    Ok(Redirect::to("/shows").into_response())
}

pub async fn show_detail(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;

    let user = match user {
        Some(u) => u,
        None => return Ok(Redirect::to("/login").into_response()),
    };

    // Only admin/superadmin can access show detail
    if !user.role_enum().can_access_admin() {
        return Ok(Redirect::to("/stream").into_response());
    }

    let query_params: std::collections::HashMap<String, String> = request
        .uri()
        .query()
        .and_then(|q| serde_urlencoded::from_str(q).ok())
        .unwrap_or_default();
    let flash_message = query_params.get("msg").cloned().filter(|s| !s.is_empty());
    let flash_kind = query_params.get("kind").cloned().filter(|s| !s.is_empty());

    let show: Option<models::Show> = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?;

    let mut show = show.ok_or_else(|| AppError::NotFound("Show not found".to_string()))?;

    let today: String = sqlx::query_scalar("SELECT date('now')")
        .fetch_one(&state.db)
        .await?;
    show.status = derive_show_status(&show.date, &today);

    // Get assigned artists
    let artists: Vec<models::Artist> = sqlx::query_as(
        r#"
        SELECT a.* FROM artists a
        INNER JOIN artist_show_assignments asa ON a.id = asa.artist_id
        WHERE asa.show_id = ?
        ORDER BY a.name
        "#,
    )
    .bind(id)
    .fetch_all(&state.db)
    .await?;

    // Generate presigned URLs for artist pictures (used for small thumbnails in show detail).
    let mut artist_pic_urls: std::collections::HashMap<String, String> =
        std::collections::HashMap::new();
    for artist in &artists {
        // Prefer branded > cropped > original for preview.
        let pic_key = artist
            .pic_overlay_key
            .as_ref()
            .or(artist.pic_cropped_key.as_ref())
            .or(artist.pic_key.as_ref());
        if let Some(key) = pic_key {
            if let Ok(url) = storage::get_presigned_url(&state, key, 3600).await {
                artist_pic_urls.insert(artist.id.to_string(), url);
            }
        }
    }

    let artist_count = artists.len() as i64;
    let artists_left = (MAX_ARTISTS_PER_SHOW - artist_count).max(0);

    // Get available artists (not assigned to any show)
    let available_artists: Vec<models::Artist> = if artists_left == 0 {
        Vec::new()
    } else {
        sqlx::query_as(
            "SELECT * FROM artists WHERE id NOT IN (SELECT artist_id FROM artist_show_assignments) ORDER BY name",
        )
        .fetch_all(&state.db)
        .await?
    };

    let mut context = tera::Context::new();
    context.insert("show", &show);
    context.insert("artists", &artists);
    context.insert("available_artists", &available_artists);
    context.insert("artists_left", &artists_left);
    context.insert("artist_pic_urls", &artist_pic_urls);
    context.insert("flash_message", &flash_message);
    context.insert("flash_kind", &flash_kind);
    context.insert("user_role", &user.role);
    context.insert("username", &user.username);

    let html = state.templates.render("show_detail.html", &context)?;
    Ok(Html(html).into_response())
}

pub async fn assign_artist(
    State(state): State<Arc<AppState>>,
    Path(show_id): Path<i64>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;
    if user.is_none() || !user.as_ref().unwrap().role_enum().can_access_admin() {
        return Err(AppError::Unauthorized);
    }

    let bytes = axum::body::to_bytes(request.into_body(), 1024)
        .await
        .map_err(|e| AppError::Validation(format!("Failed to read body: {}", e)))?;
    let form: models::AssignArtistForm = serde_urlencoded::from_bytes(&bytes)
        .map_err(|e| AppError::Validation(format!("Failed to parse form: {}", e)))?;

    // If already assigned, keep it idempotent.
    let already_assigned: Option<i64> = sqlx::query_scalar(
        "SELECT 1 FROM artist_show_assignments WHERE artist_id = ? AND show_id = ? LIMIT 1",
    )
    .bind(form.artist_id)
    .bind(show_id)
    .fetch_optional(&state.db)
    .await?;
    if already_assigned.is_some() {
        return Ok(Redirect::to(&format!("/shows/{}", show_id)).into_response());
    }

    // Enforce max artists per show.
    let current_count: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM artist_show_assignments WHERE show_id = ?")
            .bind(show_id)
            .fetch_one(&state.db)
            .await?;
    if current_count >= MAX_ARTISTS_PER_SHOW {
        return Ok(redirect_with_flash(
            &format!("/shows/{}", show_id),
            "error",
            format!(
                "This show already has {} artists assigned.",
                MAX_ARTISTS_PER_SHOW
            ),
        ));
    }

    let mut tx = state.db.begin().await?;
    // One show per artist: reassign by replacing any existing assignment.
    sqlx::query("DELETE FROM artist_show_assignments WHERE artist_id = ?")
        .bind(form.artist_id)
        .execute(&mut *tx)
        .await?;

    if let Err(e) = sqlx::query(
        "INSERT OR IGNORE INTO artist_show_assignments (artist_id, show_id) VALUES (?, ?)",
    )
    .bind(form.artist_id)
    .bind(show_id)
    .execute(&mut *tx)
    .await
    {
        tx.rollback().await.ok();
        tracing::warn!(show_id, artist_id = form.artist_id, error = %e, "Failed to assign artist");
        return Ok(redirect_with_flash(
            &format!("/shows/{}", show_id),
            "error",
            "Could not assign: show already has 4 artists.".to_string(),
        ));
    }

    sqlx::query(
        "UPDATE artists SET status = 'assigned', updated_at = datetime('now') WHERE id = ?",
    )
    .bind(form.artist_id)
    .execute(&mut *tx)
    .await?;

    tx.commit().await?;

    Ok(Redirect::to(&format!("/shows/{}", show_id)).into_response())
}

pub async fn unassign_artist(
    State(state): State<Arc<AppState>>,
    Path((show_id, artist_id)): Path<(i64, i64)>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;
    if user.is_none() || !user.as_ref().unwrap().role_enum().can_access_admin() {
        return Err(AppError::Unauthorized);
    }

    sqlx::query("DELETE FROM artist_show_assignments WHERE artist_id = ? AND show_id = ?")
        .bind(artist_id)
        .bind(show_id)
        .execute(&state.db)
        .await?;

    sqlx::query(
        "UPDATE artists SET status = 'unassigned', updated_at = datetime('now') WHERE id = ?",
    )
    .bind(artist_id)
    .execute(&state.db)
    .await?;

    Ok(Redirect::to(&format!("/shows/{}", show_id)).into_response())
}

pub async fn update_show_date(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;
    if user.is_none() || !user.as_ref().unwrap().role_enum().can_access_admin() {
        return Err(AppError::Unauthorized);
    }

    #[derive(Debug, serde::Deserialize)]
    struct UpdateShowDateForm {
        date: String,
    }

    let bytes = axum::body::to_bytes(request.into_body(), 1024)
        .await
        .map_err(|e| AppError::Validation(format!("Failed to read body: {}", e)))?;
    let form: UpdateShowDateForm = serde_urlencoded::from_bytes(&bytes)
        .map_err(|e| AppError::Validation(format!("Failed to parse form: {}", e)))?;

    let date = normalize_show_date(&form.date);
    if date.is_empty() {
        return Ok(redirect_with_flash(
            &format!("/shows/{}", id),
            "error",
            "Date is required.".to_string(),
        ));
    }

    sqlx::query("UPDATE shows SET date = ?, updated_at = datetime('now') WHERE id = ?")
        .bind(&date)
        .bind(id)
        .execute(&state.db)
        .await?;

    Ok(redirect_with_flash(
        &format!("/shows/{}", id),
        "success",
        "Updated show date.".to_string(),
    ))
}

pub async fn update_show_description(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;
    if user.is_none() || !user.as_ref().unwrap().role_enum().can_access_admin() {
        return Err(AppError::Unauthorized);
    }

    #[derive(Debug, serde::Deserialize)]
    struct UpdateShowDescriptionForm {
        description: String,
    }

    let bytes = axum::body::to_bytes(request.into_body(), 16 * 1024)
        .await
        .map_err(|e| AppError::Validation(format!("Failed to read body: {}", e)))?;
    let form: UpdateShowDescriptionForm = serde_urlencoded::from_bytes(&bytes)
        .map_err(|e| AppError::Validation(format!("Failed to parse form: {}", e)))?;

    sqlx::query(
        "UPDATE shows SET description = NULLIF(?, ''), updated_at = datetime('now') WHERE id = ?",
    )
    .bind(form.description.trim())
    .bind(id)
    .execute(&state.db)
    .await?;

    Ok(redirect_with_flash(
        &format!("/shows/{}", id),
        "success",
        "Updated show description.".to_string(),
    ))
}

// =====================
// Stream Page
// =====================

pub async fn stream_page(
    State(state): State<Arc<AppState>>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;

    let user = match user {
        Some(u) => u,
        None => return Ok(Redirect::to("/login").into_response()),
    };

    let mut context = tera::Context::new();
    context.insert("user_role", &user.role);
    context.insert("username", &user.username);

    let html = state.templates.render("stream.html", &context)?;
    Ok(Html(html).into_response())
}

// =====================
// User Management
// =====================

pub async fn users_list(
    State(state): State<Arc<AppState>>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;

    let user = match user {
        Some(u) => u,
        None => return Ok(Redirect::to("/login").into_response()),
    };

    // Only admin/superadmin can access user management
    if !user.role_enum().can_manage_users() {
        return Ok(Redirect::to("/stream").into_response());
    }

    let query_params: std::collections::HashMap<String, String> = request
        .uri()
        .query()
        .and_then(|q| serde_urlencoded::from_str(q).ok())
        .unwrap_or_default();

    let flash_message = query_params.get("msg").cloned().filter(|s| !s.is_empty());
    let flash_kind = query_params.get("kind").cloned().filter(|s| !s.is_empty());
    let generated_password = query_params
        .get("generated_password")
        .cloned()
        .filter(|s| !s.is_empty());

    #[derive(Debug, Clone, serde::Serialize, sqlx::FromRow)]
    struct UserListRow {
        pub id: i64,
        pub username: String,
        pub role: String,
        pub created_by_username: Option<String>,
        pub expires_at: Option<String>,
        pub created_at: String,
        pub is_expired: bool,
    }

    let now = chrono::Utc::now().format("%Y-%m-%d").to_string();

    let users: Vec<UserListRow> = sqlx::query_as(
        r#"
        SELECT 
            u.id,
            u.username,
            u.role,
            creator.username AS created_by_username,
            u.expires_at,
            u.created_at,
            CASE WHEN u.expires_at IS NOT NULL AND u.expires_at < datetime('now') THEN 1 ELSE 0 END AS is_expired
        FROM users u
        LEFT JOIN users creator ON creator.id = u.created_by
        ORDER BY u.created_at DESC
        "#,
    )
    .fetch_all(&state.db)
    .await?;

    let mut context = tera::Context::new();
    context.insert("users", &users);
    context.insert("user_role", &user.role);
    context.insert("username", &user.username);
    context.insert("flash_message", &flash_message);
    context.insert("flash_kind", &flash_kind);
    context.insert("generated_password", &generated_password);
    context.insert("now", &now);

    let html = state.templates.render("users.html", &context)?;
    Ok(Html(html).into_response())
}

pub async fn create_user(
    State(state): State<Arc<AppState>>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    let current_user = auth::get_current_user(&state, token.as_deref()).await;

    let current_user = match current_user {
        Some(u) => u,
        None => return Ok(Redirect::to("/login").into_response()),
    };

    // Only admin/superadmin can create users
    if !current_user.role_enum().can_manage_users() {
        return Ok(Redirect::to("/stream").into_response());
    }

    let bytes = axum::body::to_bytes(request.into_body(), 4096)
        .await
        .map_err(|e| AppError::Validation(format!("Failed to read body: {}", e)))?;
    let form: models::CreateUserForm = serde_urlencoded::from_bytes(&bytes)
        .map_err(|e| AppError::Validation(format!("Failed to parse form: {}", e)))?;

    // Validate role
    let role = models::UserRole::from_str(&form.role)
        .ok_or_else(|| AppError::Validation("Invalid role".to_string()))?;

    // Only superadmin can create admin or superadmin accounts
    if (role == models::UserRole::Superadmin || role == models::UserRole::Admin)
        && !current_user.role_enum().can_manage_superadmins()
    {
        return Ok(redirect_with_flash(
            "/users",
            "error",
            "Only superadmin can create admin accounts.".to_string(),
        ));
    }

    // Check if username already exists
    let existing: Option<i64> = sqlx::query_scalar("SELECT id FROM users WHERE username = ?")
        .bind(&form.username)
        .fetch_optional(&state.db)
        .await?;

    if existing.is_some() {
        return Ok(redirect_with_flash(
            "/users",
            "error",
            format!("Username '{}' already exists.", form.username),
        ));
    }

    // Generate password
    let password = auth::generate_password();
    let password_hash = auth::hash_password(&password)?;

    // Parse expires_at if provided (for artist accounts)
    let expires_at = if role == models::UserRole::Artist {
        form.expires_at
            .as_deref()
            .filter(|s| !s.is_empty())
            .map(|s| {
                // Convert date to datetime at end of day
                format!("{}T23:59:59", s)
            })
    } else {
        None
    };

    // Insert user
    sqlx::query(
        "INSERT INTO users (username, password_hash, role, created_by, expires_at) VALUES (?, ?, ?, ?, ?)"
    )
    .bind(&form.username)
    .bind(&password_hash)
    .bind(role.as_str())
    .bind(current_user.id)
    .bind(&expires_at)
    .execute(&state.db)
    .await?;

    // Redirect with generated password in query params (one-time display)
    let mut params = std::collections::BTreeMap::new();
    params.insert("kind".to_string(), "success".to_string());
    params.insert(
        "msg".to_string(),
        format!("User '{}' created.", form.username),
    );
    params.insert("generated_password".to_string(), password);
    let qs = serde_urlencoded::to_string(params).unwrap_or_default();

    Ok(Redirect::to(&format!("/users?{}", qs)).into_response())
}

pub async fn delete_user(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    let current_user = auth::get_current_user(&state, token.as_deref()).await;

    let current_user = match current_user {
        Some(u) => u,
        None => return Ok(Redirect::to("/login").into_response()),
    };

    // Only admin/superadmin can delete users
    if !current_user.role_enum().can_manage_users() {
        return Ok(Redirect::to("/stream").into_response());
    }

    // Get the user to delete
    let target_user: Option<models::User> = sqlx::query_as("SELECT * FROM users WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await?;

    let target_user = match target_user {
        Some(u) => u,
        None => {
            return Ok(redirect_with_flash(
                "/users",
                "error",
                "User not found.".to_string(),
            ))
        }
    };

    // Cannot delete yourself
    if target_user.id == current_user.id {
        return Ok(redirect_with_flash(
            "/users",
            "error",
            "Cannot delete your own account.".to_string(),
        ));
    }

    // Only superadmin can delete admin or superadmin accounts
    if (target_user.role_enum() == models::UserRole::Superadmin
        || target_user.role_enum() == models::UserRole::Admin)
        && !current_user.role_enum().can_manage_superadmins()
    {
        return Ok(redirect_with_flash(
            "/users",
            "error",
            "Only superadmin can delete admin accounts.".to_string(),
        ));
    }

    // Delete user (sessions will cascade)
    sqlx::query("DELETE FROM users WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await?;

    Ok(redirect_with_flash(
        "/users",
        "success",
        format!("Deleted user '{}'.", target_user.username),
    ))
}

// =====================
// Change Password
// =====================

pub async fn change_password_page(
    State(state): State<Arc<AppState>>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;

    let user = match user {
        Some(u) => u,
        None => return Ok(Redirect::to("/login").into_response()),
    };

    // Only admin/superadmin can change their password
    if !user.role_enum().can_change_password() {
        return Ok(Redirect::to("/stream").into_response());
    }

    let query_params: std::collections::HashMap<String, String> = request
        .uri()
        .query()
        .and_then(|q| serde_urlencoded::from_str(q).ok())
        .unwrap_or_default();

    let flash_message = query_params.get("msg").cloned().filter(|s| !s.is_empty());
    let flash_kind = query_params.get("kind").cloned().filter(|s| !s.is_empty());

    let mut context = tera::Context::new();
    context.insert("user_role", &user.role);
    context.insert("username", &user.username);
    context.insert("flash_message", &flash_message);
    context.insert("flash_kind", &flash_kind);

    let html = state.templates.render("change_password.html", &context)?;
    Ok(Html(html).into_response())
}

pub async fn change_password(
    State(state): State<Arc<AppState>>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    let user = auth::get_current_user(&state, token.as_deref()).await;

    let user = match user {
        Some(u) => u,
        None => return Ok(Redirect::to("/login").into_response()),
    };

    // Only admin/superadmin can change their password
    if !user.role_enum().can_change_password() {
        return Ok(Redirect::to("/stream").into_response());
    }

    let bytes = axum::body::to_bytes(request.into_body(), 4096)
        .await
        .map_err(|e| AppError::Validation(format!("Failed to read body: {}", e)))?;
    let form: models::ChangePasswordForm = serde_urlencoded::from_bytes(&bytes)
        .map_err(|e| AppError::Validation(format!("Failed to parse form: {}", e)))?;

    // Verify current password
    if !auth::verify_password(&form.current_password, &user.password_hash) {
        return Ok(redirect_with_flash(
            "/change-password",
            "error",
            "Current password is incorrect.".to_string(),
        ));
    }

    // Check new password confirmation
    if form.new_password != form.confirm_password {
        return Ok(redirect_with_flash(
            "/change-password",
            "error",
            "New passwords do not match.".to_string(),
        ));
    }

    // Validate new password length
    if form.new_password.len() < 8 {
        return Ok(redirect_with_flash(
            "/change-password",
            "error",
            "New password must be at least 8 characters.".to_string(),
        ));
    }

    // Hash and update password
    let new_hash = auth::hash_password(&form.new_password)?;

    sqlx::query("UPDATE users SET password_hash = ?, updated_at = datetime('now') WHERE id = ?")
        .bind(&new_hash)
        .bind(user.id)
        .execute(&state.db)
        .await?;

    Ok(redirect_with_flash(
        "/change-password",
        "success",
        "Password changed successfully.".to_string(),
    ))
}
