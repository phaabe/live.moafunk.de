//! Recovery endpoints. Database changes finish before mail is scheduled.
use crate::{
    auth, recovery,
    recovery_mail::{self, MailKind},
    AppError, AppState, Result,
};
use axum::{
    extract::{ConnectInfo, Path, State},
    http::HeaderMap,
    Json,
};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    net::{IpAddr, SocketAddr},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

const WINDOW: Duration = Duration::from_secs(15 * 60);
const MAX_KEYS: usize = 10_000;
const REQUEST_MESSAGE: &str = "If this address is eligible, a reset link will be sent.";

pub fn routes() -> axum::Router<Arc<AppState>> {
    use axum::{
        extract::DefaultBodyLimit,
        routing::{get, post},
        Router,
    };
    Router::new()
        .route("/api/auth/recovery", get(settings))
        .route("/api/auth/recovery-email", post(enroll))
        .route("/api/auth/recovery-email/remove", post(remove))
        .route("/api/auth/recovery-email/verify", post(verify))
        .route("/api/auth/password-reset/request", post(request_reset))
        .route("/api/auth/password-reset/complete", post(complete_reset))
        .route("/api/users/:id/recovery-email/clear", post(admin_clear))
        .layer(DefaultBodyLimit::max(8192))
}

#[derive(Default)]
pub struct RecoveryControls {
    limits: Mutex<HashMap<String, (Instant, u32)>>,
    trust_proxy: bool,
}

impl RecoveryControls {
    pub fn from_env() -> Result<Self> {
        let trust_proxy = match std::env::var("RECOVERY_TRUST_PROXY").as_deref() {
            Err(std::env::VarError::NotPresent) | Ok("false") => false,
            Ok("true") => true,
            _ => {
                return Err(AppError::Config(
                    "RECOVERY_TRUST_PROXY must be true or false".into(),
                ))
            }
        };
        Ok(Self {
            trust_proxy,
            ..Self::default()
        })
    }

    fn check(&self, key: String, max: u32) -> Result<()> {
        self.check_at(key, max, Instant::now())
    }

    fn check_at(&self, key: String, max: u32, now: Instant) -> Result<()> {
        let mut limits = self
            .limits
            .lock()
            .map_err(|_| AppError::ServiceUnavailable("Please try again.".into()))?;
        limits.retain(|_, (start, _)| now.duration_since(*start) < WINDOW);
        if limits.len() >= MAX_KEYS && !limits.contains_key(&key) {
            return Err(rate_error());
        }
        let (_, count) = limits.entry(key).or_insert((now, 0));
        if *count >= max {
            return Err(rate_error());
        }
        *count += 1;
        Ok(())
    }

    fn ip(&self, peer: SocketAddr, headers: &HeaderMap) -> IpAddr {
        // Enable only behind a proxy that overwrites X-Real-IP and blocks direct access.
        if self.trust_proxy {
            if let Some(ip) = headers
                .get("x-real-ip")
                .and_then(|v| v.to_str().ok())
                .and_then(|s| s.parse().ok())
            {
                return ip;
            }
        }
        peer.ip()
    }
}

fn rate_error() -> AppError {
    AppError::TooManyRequests("Too many attempts. Please try again in 15 minutes.".into())
}
fn success() -> Json<Value> {
    Json(json!({"success": true}))
}
fn require_mail(state: &AppState) -> Result<()> {
    if !state.recovery_mail.enabled() {
        return Err(AppError::ServiceUnavailable(
            "Email recovery is not available. Contact an administrator.".into(),
        ));
    }
    Ok(())
}
async fn current_user(state: &Arc<AppState>, headers: &HeaderMap) -> Result<i64> {
    let session = auth::get_session_from_headers(headers);
    auth::get_current_user(state, session.as_deref())
        .await
        .map(|u| u.id)
        .ok_or_else(|| AppError::Unauthorized("Not authenticated".into()))
}
fn public_limit(state: &AppState, peer: SocketAddr, headers: &HeaderMap) -> Result<()> {
    state.recovery_controls.check(
        format!("ip:{}", state.recovery_controls.ip(peer, headers)),
        20,
    )
}

pub async fn settings(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<Json<Value>> {
    let user_id = current_user(&state, &headers).await?;
    let settings = recovery::settings(&state.db, user_id).await?;
    Ok(Json(
        json!({"enabled": state.recovery_mail.enabled(), "eligible": settings.eligible, "email": settings.email}),
    ))
}

#[derive(Deserialize)]
pub struct EnrollRequest {
    email: String,
    current_password: String,
}
pub async fn enroll(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(req): Json<EnrollRequest>,
) -> Result<Json<Value>> {
    require_mail(&state)?;
    let user_id = current_user(&state, &headers).await?;
    state
        .recovery_controls
        .check(format!("enroll:{user_id}"), 5)?;
    let mail = recovery::enroll(&state.db, user_id, &req.email, &req.current_password).await?;
    recovery_mail::send(state, mail, MailKind::Verify);
    Ok(success())
}

#[derive(Deserialize)]
pub struct PasswordRequest {
    current_password: String,
}
pub async fn remove(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(req): Json<PasswordRequest>,
) -> Result<Json<Value>> {
    let user_id = current_user(&state, &headers).await?;
    state
        .recovery_controls
        .check(format!("settings:{user_id}"), 10)?;
    if let Some(mail) = recovery::remove(&state.db, user_id, &req.current_password).await? {
        recovery_mail::send(state, mail, MailKind::EmailRemoved);
    }
    Ok(success())
}

#[derive(Deserialize)]
pub struct TokenRequest {
    token: String,
}
pub async fn verify(
    State(state): State<Arc<AppState>>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(req): Json<TokenRequest>,
) -> Result<Json<Value>> {
    public_limit(&state, peer, &headers)?;
    require_mail(&state)?;
    if let Some(mail) = recovery::verify(&state.db, &req.token).await? {
        recovery_mail::send(state, mail, MailKind::EmailRemoved);
    }
    Ok(success())
}

#[derive(Deserialize)]
pub struct EmailRequest {
    email: String,
}
pub async fn request_reset(
    State(state): State<Arc<AppState>>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(req): Json<EmailRequest>,
) -> Result<Json<Value>> {
    public_limit(&state, peer, &headers)?;
    require_mail(&state)?;
    // The same counters and response apply to unknown and ineligible addresses.
    if let Ok(email) = recovery::normalize_email(&req.email) {
        state.recovery_controls.check(format!("email:{email}"), 5)?;
        if let Some(mail) = recovery::request_reset(&state.db, &email).await? {
            recovery_mail::send(state, mail, MailKind::Reset);
        }
    }
    Ok(Json(json!({"success": true, "message": REQUEST_MESSAGE})))
}

#[derive(Deserialize)]
pub struct ResetRequest {
    token: String,
    new_password: String,
}
pub async fn complete_reset(
    State(state): State<Arc<AppState>>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(req): Json<ResetRequest>,
) -> Result<Json<Value>> {
    public_limit(&state, peer, &headers)?;
    require_mail(&state)?;
    let mail = recovery::complete_reset(&state.db, &req.token, &req.new_password).await?;
    recovery_mail::send(state, mail, MailKind::PasswordChanged);
    Ok(success())
}

pub async fn admin_clear(
    State(state): State<Arc<AppState>>,
    Path(target_id): Path<i64>,
    headers: HeaderMap,
) -> Result<Json<Value>> {
    let actor_id = current_user(&state, &headers).await?;
    if let Some(mail) = recovery::admin_clear(&state.db, actor_id, target_id).await? {
        recovery_mail::send(state, mail, MailKind::EmailRemoved);
    }
    Ok(success())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn limits_expire_and_email_counts_apply_across_ips() {
        let controls = RecoveryControls::default();
        let now = Instant::now();
        let key = "email:unknown@example.org".to_string();
        for _ in 0..5 {
            controls.check_at(key.clone(), 5, now).unwrap();
        }
        assert!(matches!(
            controls.check_at(key.clone(), 5, now),
            Err(AppError::TooManyRequests(_))
        ));
        controls.check_at(key, 5, now + WINDOW).unwrap();
        assert_eq!(controls.limits.lock().unwrap().len(), 1);
    }
    #[test]
    fn forwarded_ip_is_ignored_unless_explicitly_trusted() {
        let peer = "127.0.0.1:5000".parse().unwrap();
        let mut headers = HeaderMap::new();
        headers.insert("x-real-ip", "198.51.100.2".parse().unwrap());
        assert_eq!(RecoveryControls::default().ip(peer, &headers), peer.ip());
        let controls = RecoveryControls {
            trust_proxy: true,
            ..Default::default()
        };
        assert_eq!(
            controls.ip(peer, &headers),
            "198.51.100.2".parse::<IpAddr>().unwrap()
        );
    }
}
