//! Add, remove and leave the co-hosts of a Show (series) and of one episode.
//!
//! - Only the Show's owner (or an admin) changes the Show's co-hosts. A
//!   co-host may leave.
//! - The owner, a Show co-host (or an admin) changes an episode's co-hosts.
//!   An episode co-host may leave.
//! - Only host accounts can be added: no guests, no admins.

use std::sync::Arc;

use axum::{
    extract::{Path, State},
    http::HeaderMap,
    response::IntoResponse,
    Json,
};
use serde::{Deserialize, Serialize};

use super::api::{require_episode_access, require_show_creator, require_show_manager};
use super::series::load_series_by_slug;
use crate::hosts::{self, EpisodeAccess};
use crate::{models, AppError, AppState, Result};

#[derive(Serialize)]
pub struct HostAccount {
    id: i64,
    username: String,
}

/// GET /api/hosts — every active host account by name: whom Add host offers.
pub async fn api_list_hosts(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    require_show_creator(&state, &headers).await?;
    let users: Vec<models::User> =
        sqlx::query_as("SELECT * FROM users WHERE role = 'host' ORDER BY username COLLATE NOCASE")
            .fetch_all(&state.db)
            .await?;
    let hosts: Vec<HostAccount> = users
        .into_iter()
        .filter(|u| !u.is_expired())
        .map(|u| HostAccount {
            id: u.id,
            username: u.username,
        })
        .collect();
    Ok(Json(hosts))
}

#[derive(Deserialize)]
pub struct AddHostRequest {
    user_id: i64,
}

/// The user to add, or 400 when it is not an active host account.
async fn host_to_add(state: &AppState, user_id: i64) -> Result<models::User> {
    hosts::find_host_account(&state.db, user_id)
        .await?
        .filter(|u| !u.is_expired())
        .ok_or_else(|| AppError::BadRequest("Only host accounts can be added as hosts".to_string()))
}

/// POST /api/series/:slug/hosts — the owner adds a co-host to the Show.
pub async fn api_add_series_host(
    State(state): State<Arc<AppState>>,
    Path(slug): Path<String>,
    headers: HeaderMap,
    Json(req): Json<AddHostRequest>,
) -> Result<impl IntoResponse> {
    let user = require_show_creator(&state, &headers).await?;
    let row = load_series_by_slug(&state, &slug).await?;
    if !user.role_enum().can_access_admin() && row.owner_user_id != user.id {
        return Err(AppError::Forbidden(
            "Only the Show's owner can change its co-hosts".to_string(),
        ));
    }
    let host = host_to_add(&state, req.user_id).await?;
    if host.id == row.owner_user_id {
        return Err(AppError::Conflict(format!(
            "{} already hosts this Show",
            host.username
        )));
    }
    let added = sqlx::query(
        "INSERT OR IGNORE INTO series_hosts (series_id, user_id, added_by) VALUES (?, ?, ?)",
    )
    .bind(row.id)
    .bind(host.id)
    .bind(user.id)
    .execute(&state.db)
    .await?;
    if added.rows_affected() == 0 {
        return Err(AppError::Conflict(format!(
            "{} already hosts this Show",
            host.username
        )));
    }
    Ok(Json(hosts::series_hosts(&state.db, row.id).await?))
}

/// DELETE /api/series/:slug/hosts/:user_id — the owner removes a co-host, or
/// a co-host leaves the Show.
pub async fn api_remove_series_host(
    State(state): State<Arc<AppState>>,
    Path((slug, user_id)): Path<(String, i64)>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let user = require_show_creator(&state, &headers).await?;
    let row = load_series_by_slug(&state, &slug).await?;
    let may =
        user.role_enum().can_access_admin() || row.owner_user_id == user.id || user_id == user.id;
    if !may {
        return Err(AppError::Forbidden(
            "Only the Show's owner can change its co-hosts".to_string(),
        ));
    }
    let removed = sqlx::query("DELETE FROM series_hosts WHERE series_id = ? AND user_id = ?")
        .bind(row.id)
        .bind(user_id)
        .execute(&state.db)
        .await?;
    if removed.rows_affected() == 0 {
        return Err(AppError::NotFound("Not a co-host of this Show".to_string()));
    }
    Ok(Json(hosts::series_hosts(&state.db, row.id).await?))
}

/// POST /api/shows/:id/hosts — the owner or a Show co-host adds a co-host to
/// one episode.
pub async fn api_add_episode_host(
    State(state): State<Arc<AppState>>,
    Path(show_id): Path<i64>,
    headers: HeaderMap,
    Json(req): Json<AddHostRequest>,
) -> Result<impl IntoResponse> {
    let (user, show) = require_show_manager(&state, &headers, show_id).await?;
    let host = host_to_add(&state, req.user_id).await?;
    let current = hosts::episode_hosts(&state.db, &show).await?;
    if current.iter().any(|h| h.user_id == host.id) {
        return Err(AppError::Conflict(format!(
            "{} already hosts this episode",
            host.username
        )));
    }
    sqlx::query("INSERT INTO episode_hosts (show_id, user_id, added_by) VALUES (?, ?, ?)")
        .bind(show.id)
        .bind(host.id)
        .bind(user.id)
        .execute(&state.db)
        .await?;
    Ok(Json(hosts::episode_hosts(&state.db, &show).await?))
}

/// DELETE /api/shows/:id/hosts/:user_id — the owner or a Show co-host removes
/// an episode co-host, or an episode co-host leaves the episode.
pub async fn api_remove_episode_host(
    State(state): State<Arc<AppState>>,
    Path((show_id, user_id)): Path<(i64, i64)>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let (user, show, access) = require_episode_access(&state, &headers, show_id).await?;
    if access != EpisodeAccess::Manage && user_id != user.id {
        return Err(AppError::Forbidden(
            "Only the Show's owner and co-hosts can do this".to_string(),
        ));
    }
    let removed = sqlx::query("DELETE FROM episode_hosts WHERE show_id = ? AND user_id = ?")
        .bind(show.id)
        .bind(user_id)
        .execute(&state.db)
        .await?;
    if removed.rows_affected() == 0 {
        return Err(AppError::NotFound(
            "Not a co-host of this episode".to_string(),
        ));
    }
    Ok(Json(hosts::episode_hosts(&state.db, &show).await?))
}
