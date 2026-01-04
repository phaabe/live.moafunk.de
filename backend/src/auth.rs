use crate::{AppState, Result};
use argon2::{
    password_hash::{PasswordHash, PasswordVerifier},
    Argon2,
};
use axum::http::header::COOKIE;
use axum::http::Request;
use chrono::{Duration, Utc};
use rand::Rng;
use std::sync::Arc;

const SESSION_COOKIE_NAME: &str = "session";
const SESSION_DURATION_DAYS: i64 = 7;

pub fn verify_password(password: &str, hash: &str) -> bool {
    let parsed_hash = match PasswordHash::new(hash) {
        Ok(h) => h,
        Err(_) => return false,
    };

    Argon2::default()
        .verify_password(password.as_bytes(), &parsed_hash)
        .is_ok()
}

pub fn generate_session_token() -> String {
    let mut rng = rand::thread_rng();
    let bytes: [u8; 32] = rng.gen();
    base64_url::encode(&bytes)
}

pub async fn create_session(state: &Arc<AppState>) -> Result<String> {
    let token = generate_session_token();
    let expires_at = Utc::now() + Duration::days(SESSION_DURATION_DAYS);

    sqlx::query("INSERT INTO sessions (token, expires_at) VALUES (?, ?)")
        .bind(&token)
        .bind(expires_at.to_rfc3339())
        .execute(&state.db)
        .await?;

    Ok(token)
}

pub async fn validate_session(state: &Arc<AppState>, token: &str) -> bool {
    let result = sqlx::query_scalar::<_, String>(
        "SELECT token FROM sessions WHERE token = ? AND expires_at > datetime('now')",
    )
    .bind(token)
    .fetch_optional(&state.db)
    .await;

    matches!(result, Ok(Some(_)))
}

pub fn get_session_from_cookies<B>(request: &Request<B>) -> Option<String> {
    request
        .headers()
        .get(COOKIE)?
        .to_str()
        .ok()?
        .split(';')
        .find_map(|cookie| {
            let cookie = cookie.trim();
            if cookie.starts_with(&format!("{}=", SESSION_COOKIE_NAME)) {
                Some(cookie[SESSION_COOKIE_NAME.len() + 1..].to_string())
            } else {
                None
            }
        })
}

pub async fn is_authenticated(state: &Arc<AppState>, token: Option<&str>) -> bool {
    match token {
        Some(t) => validate_session(state, t).await,
        None => false,
    }
}
