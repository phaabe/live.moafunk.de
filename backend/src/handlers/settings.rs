use std::sync::Arc;

use axum::{extract::State, http::HeaderMap, response::IntoResponse, Json};
use serde::{Deserialize, Serialize};

use crate::{auth, db, telegram_notify, AppError, AppState, Result};

/// The Moafunkbot switches on the Config page.
#[derive(Serialize)]
pub struct NotificationSettingsResponse {
    /// Technical alerts: bot messages to the admin group.
    pub enabled: bool,
    /// The live chat between the Telegram live chat group and On Air.
    pub live_chat: bool,
}

/// Each field is optional, so one switch never resets another.
#[derive(Deserialize)]
pub struct UpdateNotificationSettings {
    pub enabled: Option<bool>,
    pub live_chat: Option<bool>,
}

async fn load_notifications(state: &AppState) -> NotificationSettingsResponse {
    NotificationSettingsResponse {
        enabled: db::is_notifications_enabled(&state.db).await,
        live_chat: db::is_service_enabled(&state.db, db::TELEGRAM_LIVE_CHAT_ENABLED).await,
    }
}

/// GET /api/settings/notifications — returns the Moafunkbot switches.
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
        return Err(AppError::Forbidden("Admin access required".to_string()));
    }

    Ok(Json(load_notifications(&state).await))
}

/// PUT /api/settings/notifications — sets the Moafunkbot switches it is given.
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
        return Err(AppError::Forbidden("Admin access required".to_string()));
    }

    for (key, name, on) in [
        ("notifications_enabled", "Notifications", req.enabled),
        (db::TELEGRAM_LIVE_CHAT_ENABLED, "Live chat", req.live_chat),
    ] {
        let Some(on) = on else { continue };
        let value = if on { "true" } else { "false" };
        db::set_setting(&state.db, key, value)
            .await
            .map_err(AppError::Database)?;

        tracing::info!(
            user = %user.username,
            switch = name,
            enabled = on,
            "Notification toggle updated"
        );

        // Always report via Telegram (bypasses the guard so "disabled" messages still send)
        let emoji = if on { "🔔" } else { "🔕" };
        let status = if on { "ENABLED" } else { "DISABLED" };
        let msg = format!("{emoji} {name} {status} by {user}", user = user.username);
        telegram_notify::notify_unconditional(&state, &msg).await;
    }

    Ok(Json(load_notifications(&state).await))
}

/// The admin switches for the outside services. Off means hosts see
/// "temporarily not available" and every post or upload there is refused.
#[derive(Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct ServiceSettings {
    pub soundcloud: bool,
    pub instagram: bool,
}

pub async fn load_services(state: &AppState) -> ServiceSettings {
    ServiceSettings {
        soundcloud: db::is_service_enabled(&state.db, db::SOUNDCLOUD_ENABLED).await,
        instagram: db::is_service_enabled(&state.db, db::INSTAGRAM_ENABLED).await,
    }
}

/// GET /api/settings/services — any signed-in user; hosts need it to show
/// "temporarily not available".
pub async fn get_services(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let token = auth::get_session_from_headers(&headers);
    auth::get_current_user(&state, token.as_deref())
        .await
        .ok_or_else(|| AppError::Unauthorized("Not authenticated".to_string()))?;
    Ok(Json(load_services(&state).await))
}

/// PUT /api/settings/services — admin only; sets both switches.
pub async fn set_services(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(req): Json<ServiceSettings>,
) -> Result<impl IntoResponse> {
    let token = auth::get_session_from_headers(&headers);
    let user = auth::get_current_user(&state, token.as_deref())
        .await
        .ok_or_else(|| AppError::Unauthorized("Not authenticated".to_string()))?;
    if !user.role_enum().can_access_admin() {
        return Err(AppError::Forbidden("Admin access required".to_string()));
    }

    let before = load_services(&state).await;
    for (key, on) in [
        (db::SOUNDCLOUD_ENABLED, req.soundcloud),
        (db::INSTAGRAM_ENABLED, req.instagram),
    ] {
        db::set_setting(&state.db, key, if on { "true" } else { "false" })
            .await
            .map_err(AppError::Database)?;
    }

    for (name, was, now) in [
        ("SoundCloud", before.soundcloud, req.soundcloud),
        ("Instagram", before.instagram, req.instagram),
    ] {
        if was != now {
            let status = if now { "turned ON" } else { "turned OFF" };
            tracing::info!(user = %user.username, service = name, on = now, "Service switch changed");
            let msg = format!("{name} {status} by {}", user.username);
            telegram_notify::notify_unconditional(&state, &msg).await;
        }
    }

    Ok(Json(load_services(&state).await))
}
