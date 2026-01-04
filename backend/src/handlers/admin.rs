use crate::{auth, models, storage, AppError, AppState, Result};
use axum::{
    extract::{Path, State},
    http::Request,
    response::{Html, IntoResponse, Redirect, Response},
};
use std::sync::Arc;

fn get_session_token<B>(request: &Request<B>) -> Option<String> {
    auth::get_session_from_cookies(request)
}

async fn require_auth<B>(state: &Arc<AppState>, request: &Request<B>) -> Result<()> {
    let token = get_session_token(request);
    if !auth::is_authenticated(state, token.as_deref()).await {
        return Err(AppError::Unauthorized);
    }
    Ok(())
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

    let status_filter: Option<String> = request
        .uri()
        .query()
        .and_then(|q| {
            q.split('&')
                .find(|p| p.starts_with("status_filter="))
                .map(|p| p.trim_start_matches("status_filter=").to_string())
        })
        .filter(|s| !s.is_empty());

    let artists: Vec<models::Artist> = if let Some(status) = &status_filter {
        sqlx::query_as("SELECT * FROM artists WHERE status = ? ORDER BY created_at DESC")
            .bind(status)
            .fetch_all(&state.db)
            .await?
    } else {
        sqlx::query_as("SELECT * FROM artists ORDER BY created_at DESC")
            .fetch_all(&state.db)
            .await?
    };

    let mut context = tera::Context::new();
    context.insert("artists", &artists);
    context.insert("status_filter", &status_filter);

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
    if let Some(key) = &artist.pic_key {
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

    // Get available shows for assignment (scheduled, not already assigned)
    let available_shows: Vec<models::Show> = sqlx::query_as(
        r#"
                SELECT * FROM shows
                WHERE status = 'scheduled'
                    AND id NOT IN (
                        SELECT show_id FROM artist_show_assignments WHERE artist_id = ?
                    )
                ORDER BY date ASC
                "#,
    )
    .bind(id)
    .fetch_all(&state.db)
    .await?;

    let artist_with_shows = models::ArtistWithShows { artist, shows };

    let mut context = tera::Context::new();
    context.insert("artist", &artist_with_shows);
    context.insert("file_urls", &file_urls);
    context.insert("available_shows", &available_shows);

    let html = state.templates.render("artist_detail.html", &context)?;
    Ok(Html(html).into_response())
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

    sqlx::query("INSERT OR IGNORE INTO artist_show_assignments (artist_id, show_id) VALUES (?, ?)")
        .bind(artist_id)
        .bind(form.show_id)
        .execute(&state.db)
        .await?;

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

    let shows: Vec<models::Show> = sqlx::query_as("SELECT * FROM shows ORDER BY date DESC")
        .fetch_all(&state.db)
        .await?;

    // Get artist counts for each show
    let mut shows_with_counts: Vec<serde_json::Value> = Vec::new();
    for show in shows {
        let count: i64 =
            sqlx::query_scalar("SELECT COUNT(*) FROM artist_show_assignments WHERE show_id = ?")
                .bind(show.id)
                .fetch_one(&state.db)
                .await?;

        shows_with_counts.push(serde_json::json!({
            "show": show,
            "artist_count": count,
        }));
    }

    let mut context = tera::Context::new();
    context.insert("shows", &shows_with_counts);

    let html = state.templates.render("shows.html", &context)?;
    Ok(Html(html).into_response())
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

    sqlx::query("INSERT INTO shows (title, date, description) VALUES (?, ?, ?)")
        .bind(&form.title)
        .bind(&form.date)
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

    // Get available artists (approved, not assigned)
    let assigned_ids: Vec<i64> = artists.iter().map(|a| a.id).collect();
    let available_artists: Vec<models::Artist> = if assigned_ids.is_empty() {
        sqlx::query_as("SELECT * FROM artists WHERE status = 'approved' ORDER BY name")
            .fetch_all(&state.db)
            .await?
    } else {
        let placeholders = assigned_ids
            .iter()
            .map(|_| "?")
            .collect::<Vec<_>>()
            .join(",");
        let query = format!(
            "SELECT * FROM artists WHERE status = 'approved' AND id NOT IN ({}) ORDER BY name",
            placeholders
        );
        let mut q = sqlx::query_as(&query);
        for id in &assigned_ids {
            q = q.bind(id);
        }
        q.fetch_all(&state.db).await?
    };

    let mut context = tera::Context::new();
    context.insert("show", &show);
    context.insert("artists", &artists);
    context.insert("available_artists", &available_artists);

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

    sqlx::query("INSERT OR IGNORE INTO artist_show_assignments (artist_id, show_id) VALUES (?, ?)")
        .bind(form.artist_id)
        .bind(show_id)
        .execute(&state.db)
        .await?;

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
