use std::sync::Arc;

use axum::{
    extract::State,
    http::HeaderMap,
    response::IntoResponse,
    Json,
};
use serde::{Deserialize, Serialize};

use crate::{auth, db, telegram_notify, AppError, AppState, Result};

#[derive(Serialize)]
pub struct NotificationSettingsResponse {
    pub enabled: bool,
}

#[derive(Deserialize)]
pub struct UpdateNotificationSettings {
    pub enabled: bool,
}

/// GET /api/settings/notifications — returns the current notification toggle state.
///
/// Requires admin or superadmin role.
pub async fn get_notifications(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let token = auth::get_session_from_headers(&headers);
    let user = auth::get_current_user(&state, token.as_deref())
        .await
        .ok_or_else(|| AppError::Unauthorized("Not authenticated".to_string()))?;

    if !user.role_enum().can_access_admin() {
        return Err(AppError::Forbidden(
            "Admin access required".to_string(),
        ));
    }

    let enabled = db::is_notifications_enabled(&state.db).await;
    Ok(Json(NotificationSettingsResponse { enabled }))
}

/// PUT /api/settings/notifications — update the notification toggle state.
///
/// Requires admin or superadmin role.
pub async fn set_notifications(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(req): Json<UpdateNotificationSettings>,
) -> Result<impl IntoResponse> {
    let token = auth::get_session_from_headers(&headers);
    let user = auth::get_current_user(&state, token.as_deref())
        .await
        .ok_or_else(|| AppError::Unauthorized("Not authenticated".to_string()))?;

    if !user.role_enum().can_access_admin() {
        return Err(AppError::Forbidden(
            "Admin access required".to_string(),
        ));
    }

    let value = if req.enabled { "true" } else { "false" };
    db::set_setting(&state.db, "notifications_enabled", value)
        .await
        .map_err(AppError::Database)?;

    tracing::info!(
        user = %user.username,
        enabled = req.enabled,
        "Notification toggle updated"
    );

    // Always report via Telegram (bypasses the guard so "disabled" messages still send)
    let emoji = if req.enabled { "🔔" } else { "🔕" };
    let status = if req.enabled { "ENABLED" } else { "DISABLED" };
    let msg = format!(
        "{emoji} Notifications {status} by {user}",
        user = user.username
    );
    telegram_notify::notify_unconditional(&state, &msg).await;

    Ok(Json(NotificationSettingsResponse {
        enabled: req.enabled,
    }))
}
