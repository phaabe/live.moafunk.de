use crate::{auth, models, storage, AppError, AppState, Result};
use axum::{
    extract::{Path, State},
    http::Request,
    response::{Html, IntoResponse, Redirect, Response},
};
use std::sync::Arc;

const MAX_ARTISTS_PER_SHOW: i64 = 4;

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

pub async fn index() -> Redirect {
    Redirect::to("/artists")
}

pub async fn artists_list(
    State(state): State<Arc<AppState>>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    if !auth::is_authenticated(&state, token.as_deref()).await {
        return Ok(Redirect::to("/login").into_response());
    }

    let query_params: std::collections::HashMap<String, String> = request
        .uri()
        .query()
        .and_then(|q| serde_urlencoded::from_str(q).ok())
        .unwrap_or_default();

    let status_filter = query_params
        .get("status_filter")
        .cloned()
        .filter(|s| !s.is_empty());

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
        "status" => "a.status",
        "submitted" => "a.created_at",
        _ => "a.created_at",
    };

    let flash_message = query_params.get("msg").cloned().filter(|s| !s.is_empty());
    let flash_kind = query_params.get("kind").cloned().filter(|s| !s.is_empty());

    #[derive(Debug, Clone, serde::Serialize, sqlx::FromRow)]
    struct ArtistListRow {
        pub id: i64,
        pub name: String,
        pub pronouns: String,
        pub status: String,
        pub created_at: String,
        pub show_titles: Option<String>,
    }

    let artists: Vec<ArtistListRow> = if let Some(status) = &status_filter {
        let query = format!(
            r#"
            SELECT
                a.id,
                a.name,
                a.pronouns,
                a.status,
                a.created_at,
                group_concat(s.title, ', ') AS show_titles
            FROM artists a
            LEFT JOIN artist_show_assignments asa ON asa.artist_id = a.id
            LEFT JOIN shows s ON s.id = asa.show_id
            WHERE a.status = ?
            GROUP BY a.id
            ORDER BY {} {}, a.id DESC
            "#,
            order_by, dir
        );
        sqlx::query_as(&query)
            .bind(status)
            .fetch_all(&state.db)
            .await?
    } else {
        let query = format!(
            r#"
            SELECT
                a.id,
                a.name,
                a.pronouns,
                a.status,
                a.created_at,
                group_concat(s.title, ', ') AS show_titles
            FROM artists a
            LEFT JOIN artist_show_assignments asa ON asa.artist_id = a.id
            LEFT JOIN shows s ON s.id = asa.show_id
            GROUP BY a.id
            ORDER BY {} {}, a.id DESC
            "#,
            order_by, dir
        );
        sqlx::query_as(&query).fetch_all(&state.db).await?
    };

    let mut context = tera::Context::new();
    context.insert("artists", &artists);
    context.insert("status_filter", &status_filter);
    context.insert("flash_message", &flash_message);
    context.insert("flash_kind", &flash_kind);
    context.insert("sort", &sort);
    context.insert("dir", &dir.to_lowercase());

    let html = state.templates.render("artists.html", &context)?;
    Ok(Html(html).into_response())
}

pub async fn artist_detail(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    if !auth::is_authenticated(&state, token.as_deref()).await {
        return Ok(Redirect::to("/login").into_response());
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

    // Get available shows for assignment (scheduled, not already assigned, and with remaining slots)
    let available_shows: Vec<AvailableShow> = sqlx::query_as(
        r#"
        SELECT
            s.id,
            s.title,
            s.date,
            (? - COUNT(asa.artist_id)) AS artists_left
        FROM shows s
        LEFT JOIN artist_show_assignments asa ON asa.show_id = s.id
        WHERE s.status = 'scheduled'
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

    let html = state.templates.render("artist_detail.html", &context)?;
    Ok(Html(html).into_response())
}

pub async fn delete_artist(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    if !auth::is_authenticated(&state, token.as_deref()).await {
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
    if !auth::is_authenticated(&state, token.as_deref()).await {
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
    tx.commit().await?;

    Ok(Redirect::to(&format!("/artists/{}", artist_id)).into_response())
}

pub async fn unassign_show(
    State(state): State<Arc<AppState>>,
    Path((artist_id, show_id)): Path<(i64, i64)>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    if !auth::is_authenticated(&state, token.as_deref()).await {
        return Err(AppError::Unauthorized);
    }

    sqlx::query("DELETE FROM artist_show_assignments WHERE artist_id = ? AND show_id = ?")
        .bind(artist_id)
        .bind(show_id)
        .execute(&state.db)
        .await?;

    Ok(Redirect::to(&format!("/artists/{}", artist_id)).into_response())
}

pub async fn update_artist_status(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    if !auth::is_authenticated(&state, token.as_deref()).await {
        return Err(AppError::Unauthorized);
    }

    // Parse form from body
    let bytes = axum::body::to_bytes(request.into_body(), 1024)
        .await
        .map_err(|e| AppError::Validation(format!("Failed to read body: {}", e)))?;
    let form: models::StatusUpdateForm = serde_urlencoded::from_bytes(&bytes)
        .map_err(|e| AppError::Validation(format!("Failed to parse form: {}", e)))?;

    sqlx::query("UPDATE artists SET status = ?, updated_at = datetime('now') WHERE id = ?")
        .bind(&form.status)
        .bind(id)
        .execute(&state.db)
        .await?;

    Ok(Redirect::to(&format!("/artists/{}", id)).into_response())
}

pub async fn shows_list(
    State(state): State<Arc<AppState>>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    if !auth::is_authenticated(&state, token.as_deref()).await {
        return Ok(Redirect::to("/login").into_response());
    }

    let query_params: std::collections::HashMap<String, String> = request
        .uri()
        .query()
        .and_then(|q| serde_urlencoded::from_str(q).ok())
        .unwrap_or_default();

    let status_filter = query_params
        .get("status_filter")
        .cloned()
        .filter(|s| !s.is_empty());

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
        "status" => "status",
        "date" => "date",
        "artists" => "date", // sorted after counts are computed
        _ => "date",
    };

    let shows: Vec<models::Show> = if let Some(status) = &status_filter {
        let query = format!(
            "SELECT * FROM shows WHERE status = ? ORDER BY {} {}, id DESC",
            order_by, dir
        );
        sqlx::query_as(&query)
            .bind(status)
            .fetch_all(&state.db)
            .await?
    } else {
        let query = format!("SELECT * FROM shows ORDER BY {} {}, id DESC", order_by, dir);
        sqlx::query_as(&query).fetch_all(&state.db).await?
    };

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

    let html = state.templates.render("shows.html", &context)?;
    Ok(Html(html).into_response())
}

pub async fn delete_show(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    if !auth::is_authenticated(&state, token.as_deref()).await {
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
    if !auth::is_authenticated(&state, token.as_deref()).await {
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
    if !auth::is_authenticated(&state, token.as_deref()).await {
        return Ok(Redirect::to("/login").into_response());
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
    .bind(id)
    .fetch_all(&state.db)
    .await?;

    let artist_count = artists.len() as i64;
    let artists_left = (MAX_ARTISTS_PER_SHOW - artist_count).max(0);

    // Get available artists (approved, not assigned to any show)
    let available_artists: Vec<models::Artist> = if artists_left == 0 {
        Vec::new()
    } else {
        sqlx::query_as(
            "SELECT * FROM artists WHERE status = 'approved' AND id NOT IN (SELECT artist_id FROM artist_show_assignments) ORDER BY name",
        )
        .fetch_all(&state.db)
        .await?
    };

    let mut context = tera::Context::new();
    context.insert("show", &show);
    context.insert("artists", &artists);
    context.insert("available_artists", &available_artists);
    context.insert("artists_left", &artists_left);
    context.insert("flash_message", &flash_message);
    context.insert("flash_kind", &flash_kind);

    let html = state.templates.render("show_detail.html", &context)?;
    Ok(Html(html).into_response())
}

pub async fn assign_artist(
    State(state): State<Arc<AppState>>,
    Path(show_id): Path<i64>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    if !auth::is_authenticated(&state, token.as_deref()).await {
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

    tx.commit().await?;

    Ok(Redirect::to(&format!("/shows/{}", show_id)).into_response())
}

pub async fn unassign_artist(
    State(state): State<Arc<AppState>>,
    Path((show_id, artist_id)): Path<(i64, i64)>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    if !auth::is_authenticated(&state, token.as_deref()).await {
        return Err(AppError::Unauthorized);
    }

    sqlx::query("DELETE FROM artist_show_assignments WHERE artist_id = ? AND show_id = ?")
        .bind(artist_id)
        .bind(show_id)
        .execute(&state.db)
        .await?;

    Ok(Redirect::to(&format!("/shows/{}", show_id)).into_response())
}

pub async fn update_show_status(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    request: Request<axum::body::Body>,
) -> Result<Response> {
    let token = get_session_token(&request);
    if !auth::is_authenticated(&state, token.as_deref()).await {
        return Err(AppError::Unauthorized);
    }

    let bytes = axum::body::to_bytes(request.into_body(), 1024)
        .await
        .map_err(|e| AppError::Validation(format!("Failed to read body: {}", e)))?;
    let form: models::StatusUpdateForm = serde_urlencoded::from_bytes(&bytes)
        .map_err(|e| AppError::Validation(format!("Failed to parse form: {}", e)))?;

    sqlx::query("UPDATE shows SET status = ?, updated_at = datetime('now') WHERE id = ?")
        .bind(&form.status)
        .bind(id)
        .execute(&state.db)
        .await?;

    Ok(Redirect::to(&format!("/shows/{}", id)).into_response())
}
