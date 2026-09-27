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

pub fn generate_session_token() -> String {
    let mut rng = rand::thread_rng();
    let bytes: [u8; 32] = rng.gen();
    base64_url::encode(&bytes)
}

pub async fn create_session(state: &Arc<AppState>, user_id: i64) -> Result<String> {
    create_session_in_db(&state.db, user_id).await
}

async fn create_session_in_db(pool: &sqlx::SqlitePool, user_id: i64) -> Result<String> {
    let token = generate_session_token();
    let expires_at = Utc::now() + Duration::days(SESSION_DURATION_DAYS);
    let mut tx = pool.begin().await?;

    sqlx::query("INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)")
        .bind(&token)
        .bind(user_id)
        .bind(expires_at.to_rfc3339())
        .execute(&mut *tx)
        .await?;

    sqlx::query(
        "UPDATE users SET first_login_at = COALESCE(first_login_at, datetime('now')) WHERE id = ?",
    )
    .bind(user_id)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;

    Ok(token)
}

/// Admins may broadcast any show. Other users need a host or artist assignment.
pub async fn broadcast_shows(
    pool: &sqlx::SqlitePool,
    user: &models::User,
) -> Result<Vec<models::Show>> {
    Ok(sqlx::query_as(
        "SELECT s.* FROM shows s WHERE ? OR s.host_user_id = ? OR EXISTS (\
         SELECT 1 FROM artist_show_assignments asa JOIN artists a ON a.id = asa.artist_id \
         WHERE asa.show_id = s.id AND a.user_id = ?) ORDER BY s.date DESC, s.id DESC",
    )
    .bind(user.role_enum().can_access_admin())
    .bind(user.id)
    .bind(user.id)
    .fetch_all(pool)
    .await?)
}

/// Check live and rehearsal access before upgrading the WebSocket.
pub async fn authorize_broadcast(
    pool: &sqlx::SqlitePool,
    user: &models::User,
    show_id: Option<i64>,
    test: bool,
) -> Result<()> {
    let Some(show_id) = show_id else {
        if test || user.role_enum().can_access_admin() {
            return Ok(());
        }
        return Err(crate::AppError::Forbidden(
            "Select an assigned show to broadcast".into(),
        ));
    };
    if broadcast_shows(pool, user)
        .await?
        .iter()
        .any(|show| show.id == show_id)
    {
        Ok(())
    } else {
        Err(crate::AppError::Forbidden(
            "You cannot broadcast this show".into(),
        ))
    }
}

/// Hosts may control their own stream; admins may also take over or stop another.
pub fn can_control_stream(user: &models::User, current_user: Option<&str>) -> bool {
    user.role_enum().can_access_admin() || current_user == Some(user.username.as_str())
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

#[cfg(test)]
mod tests {
    use super::*;

    async fn pool() -> sqlx::SqlitePool {
        let pool = sqlx::SqlitePool::connect("sqlite::memory:").await.unwrap();
        crate::db::run_migrations(&pool).await.unwrap();
        pool
    }

    async fn user(pool: &sqlx::SqlitePool, role: &str) -> models::User {
        sqlx::query_as(
            "INSERT INTO users (username, password_hash, role) VALUES (?, 'test', ?) RETURNING *",
        )
        .bind(role)
        .bind(role)
        .fetch_one(pool)
        .await
        .unwrap()
    }

    #[tokio::test]
    async fn broadcast_access_keeps_hosts_scoped_and_allows_both_admin_roles() {
        let pool = pool().await;
        let host = user(&pool, "host").await;
        let guest = user(&pool, "guest").await;
        let admin = user(&pool, "admin").await;
        let superadmin = user(&pool, "superadmin").await;
        sqlx::query("INSERT INTO shows (id, title, date, host_user_id) VALUES (1, 'Hosted', '2026-09-26', ?), (2, 'Other', '2026-09-26', NULL)")
            .bind(host.id).execute(&pool).await.unwrap();

        for admin in [&admin, &superadmin] {
            assert_eq!(broadcast_shows(&pool, admin).await.unwrap().len(), 2);
            authorize_broadcast(&pool, admin, Some(1), false)
                .await
                .unwrap();
            authorize_broadcast(&pool, admin, Some(2), false)
                .await
                .unwrap();
            assert!(authorize_broadcast(&pool, admin, Some(999), false)
                .await
                .is_err());
            assert!(can_control_stream(admin, Some(&host.username)));
        }
        assert_eq!(broadcast_shows(&pool, &host).await.unwrap().len(), 1);
        authorize_broadcast(&pool, &host, Some(1), false)
            .await
            .unwrap();
        for user in [&host, &guest] {
            assert!(matches!(
                authorize_broadcast(&pool, user, Some(2), false).await,
                Err(crate::AppError::Forbidden(_))
            ));
            assert!(authorize_broadcast(&pool, user, Some(2), true)
                .await
                .is_err());
            assert!(authorize_broadcast(&pool, user, None, false).await.is_err());
            authorize_broadcast(&pool, user, None, true).await.unwrap();
            assert!(!can_control_stream(user, Some(&admin.username)));
        }
        assert!(can_control_stream(&host, Some(&host.username)));
        assert!(broadcast_shows(&pool, &guest).await.unwrap().is_empty());

        // Artist assignments grant access without requiring host_user_id.
        sqlx::query("INSERT INTO artists (id, name, pronouns, track1_name, track2_name, user_id) VALUES (1, 'Artist', '', '', '', ?)")
            .bind(guest.id).execute(&pool).await.unwrap();
        sqlx::query("INSERT INTO artist_show_assignments (artist_id, show_id) VALUES (1, 2)")
            .execute(&pool)
            .await
            .unwrap();
        authorize_broadcast(&pool, &guest, Some(2), false)
            .await
            .unwrap();
        assert_eq!(broadcast_shows(&pool, &guest).await.unwrap().len(), 1);
        assert!(authorize_broadcast(&pool, &guest, Some(1), false)
            .await
            .is_err());
    }

    #[tokio::test]
    async fn successful_session_records_login_after_logout_and_migration_rerun() {
        let pool = pool().await;
        let host = user(&pool, "host").await;
        let first: Option<String> =
            sqlx::query_scalar("SELECT first_login_at FROM users WHERE id = ?")
                .bind(host.id)
                .fetch_one(&pool)
                .await
                .unwrap();
        assert!(first.is_none());
        create_session_in_db(&pool, host.id).await.unwrap();
        let first: String = sqlx::query_scalar("SELECT first_login_at FROM users WHERE id = ?")
            .bind(host.id)
            .fetch_one(&pool)
            .await
            .unwrap();
        sqlx::query("DELETE FROM sessions")
            .execute(&pool)
            .await
            .unwrap();
        crate::db::run_migrations(&pool).await.unwrap();
        create_session_in_db(&pool, host.id).await.unwrap();
        let later: String = sqlx::query_scalar("SELECT first_login_at FROM users WHERE id = ?")
            .bind(host.id)
            .fetch_one(&pool)
            .await
            .unwrap();
        assert_eq!(first, later);
    }

    #[tokio::test]
    async fn migration_backfills_existing_sessions_without_inventing_logins() {
        let pool = pool().await;
        let host = user(&pool, "host").await;
        let guest = user(&pool, "guest").await;
        sqlx::query("INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES ('legacy', ?, '2026-07-01 10:00:00', '2026-07-08 10:00:00')")
            .bind(host.id).execute(&pool).await.unwrap();
        crate::db::run_migrations(&pool).await.unwrap();
        let first: String = sqlx::query_scalar("SELECT first_login_at FROM users WHERE id = ?")
            .bind(host.id)
            .fetch_one(&pool)
            .await
            .unwrap();
        assert_eq!(first, "2026-07-01 10:00:00");
        let first: Option<String> =
            sqlx::query_scalar("SELECT first_login_at FROM users WHERE id = ?")
                .bind(guest.id)
                .fetch_one(&pool)
                .await
                .unwrap();
        assert!(first.is_none());
    }
}
