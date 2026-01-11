use crate::{models, AppState, Result};
use argon2::{
    password_hash::{PasswordHash, PasswordHasher, PasswordVerifier, SaltString},
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

/// Hash a password using Argon2
pub fn hash_password(password: &str) -> Result<String> {
    let salt = SaltString::generate(&mut rand::thread_rng());
    let argon2 = Argon2::default();
    let hash = argon2
        .hash_password(password.as_bytes(), &salt)
        .map_err(|e| crate::AppError::Internal(format!("Password hashing failed: {}", e)))?;
    Ok(hash.to_string())
}

/// Generate a random password for new user accounts
pub fn generate_password() -> String {
    const CHARSET: &[u8] = b"ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789";
    let mut rng = rand::thread_rng();
    (0..16)
        .map(|_| {
            let idx = rng.gen_range(0..CHARSET.len());
            CHARSET[idx] as char
        })
        .collect()
}

pub fn generate_session_token() -> String {
    let mut rng = rand::thread_rng();
    let bytes: [u8; 32] = rng.gen();
    base64_url::encode(&bytes)
}

pub async fn create_session(state: &Arc<AppState>, user_id: i64) -> Result<String> {
    let token = generate_session_token();
    let expires_at = Utc::now() + Duration::days(SESSION_DURATION_DAYS);

    sqlx::query("INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)")
        .bind(&token)
        .bind(user_id)
        .bind(expires_at.to_rfc3339())
        .execute(&state.db)
        .await?;

    Ok(token)
}

/// Get the current user from a session token
pub async fn get_current_user(state: &Arc<AppState>, token: Option<&str>) -> Option<models::User> {
    let token = token?;

    let user: Option<models::User> = sqlx::query_as(
        r#"
        SELECT u.* FROM users u
        INNER JOIN sessions s ON s.user_id = u.id
        WHERE s.token = ? AND s.expires_at > datetime('now')
        "#,
    )
    .bind(token)
    .fetch_optional(&state.db)
    .await
    .ok()?;

    user
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

/// Get session token from HeaderMap (for use in handlers that receive headers directly)
pub fn get_session_from_headers(headers: &axum::http::HeaderMap) -> Option<String> {
    headers
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
