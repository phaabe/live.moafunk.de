use crate::{auth, AppError, Result};
use serde::Serialize;
use sha2::{Digest, Sha256};
use sqlx::{FromRow, SqliteConnection, SqlitePool, Transaction};

const TOKEN_LIFETIME_SECONDS: i64 = 30 * 60;
const INVALID_LINK: &str = "This link is invalid or expired. Please request a new link.";

#[derive(Serialize)]
pub struct Settings {
    pub eligible: bool,
    pub email: Option<String>,
}

// Deliberately not Debug: link tokens must never appear in logs.
pub struct MailData {
    pub email: String,
    pub username: String,
    pub token: Option<String>,
}

#[derive(FromRow)]
struct Account {
    id: i64,
    username: String,
    password_hash: String,
    role: String,
    unexpired: bool,
}

impl Account {
    fn eligible(&self) -> bool {
        self.unexpired && matches!(self.role.as_str(), "host" | "admin")
    }

    fn mail(&self, email: String, token: Option<String>) -> MailData {
        MailData {
            email,
            username: self.username.clone(),
            token,
        }
    }
}

#[derive(FromRow)]
struct Token {
    user_id: i64,
    pending_email: Option<String>,
}

pub async fn migrate(pool: &SqlitePool) -> std::result::Result<(), sqlx::Error> {
    sqlx::query("CREATE TABLE IF NOT EXISTS recovery_emails (user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, email TEXT NOT NULL UNIQUE)")
        .execute(pool).await?;
    sqlx::query("CREATE TABLE IF NOT EXISTS recovery_tokens (hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, kind TEXT NOT NULL CHECK(kind IN ('verify', 'reset')), pending_email TEXT, expires_at INTEGER NOT NULL)")
        .execute(pool).await?;
    sqlx::query("CREATE INDEX IF NOT EXISTS recovery_tokens_user ON recovery_tokens(user_id)")
        .execute(pool)
        .await?;
    Ok(())
}

pub fn normalize_email(value: &str) -> Result<String> {
    let email = value.trim().to_lowercase();
    let valid = email.len() <= 254
        && !email.chars().any(|c| c.is_whitespace() || c.is_control())
        && email.split_once('@').is_some_and(|(local, domain)| {
            !local.is_empty()
                && local.len() <= 64
                && !domain.contains('@')
                && domain.contains('.')
                && !domain.starts_with('.')
                && !domain.ends_with('.')
        });
    if !valid {
        return Err(AppError::Validation("Enter a valid email address.".into()));
    }
    Ok(email)
}

pub(crate) fn database_error(error: sqlx::Error) -> AppError {
    if let sqlx::Error::Database(ref db) = error {
        if db
            .code()
            .and_then(|code| code.parse::<i32>().ok())
            .is_some_and(|code| matches!(code & 0xff, 5 | 6))
        {
            return AppError::ServiceUnavailable("Please try again in a moment.".into());
        }
    }
    AppError::Database(error)
}

pub(crate) async fn begin(pool: &SqlitePool) -> Result<Transaction<'static, sqlx::Sqlite>> {
    pool.begin_with("BEGIN IMMEDIATE")
        .await
        .map_err(database_error)
}

async fn account(conn: &mut SqliteConnection, id: i64) -> Result<Account> {
    sqlx::query_as("SELECT id, username, password_hash, role, COALESCE(expires_at IS NULL OR unixepoch(expires_at) > unixepoch(), 0) AS unexpired FROM users WHERE id = ?")
        .bind(id).fetch_optional(conn).await.map_err(database_error)?
        .ok_or_else(|| AppError::NotFound("User not found.".into()))
}

fn require_eligible(user: &Account) -> Result<()> {
    if !user.eligible() {
        return Err(AppError::Forbidden(
            "Email recovery is unavailable for this account.".into(),
        ));
    }
    Ok(())
}

fn require_password(user: &Account, password: &str) -> Result<()> {
    if !auth::verify_password(password, &user.password_hash) {
        return Err(AppError::Validation(
            "Current password is incorrect.".into(),
        ));
    }
    Ok(())
}

async fn authenticate(pool: &SqlitePool, id: i64, password: &str) -> Result<String> {
    let user = {
        let mut conn = pool.acquire().await.map_err(database_error)?;
        account(&mut conn, id).await?
    };
    require_eligible(&user)?;
    require_password(&user, password)?;
    Ok(user.password_hash)
}

fn require_same_password(user: &Account, authenticated_hash: &str) -> Result<()> {
    require_eligible(user)?;
    if user.password_hash != authenticated_hash {
        return Err(AppError::Conflict(
            "Your password changed. Please try again.".into(),
        ));
    }
    Ok(())
}

async fn email(conn: &mut SqliteConnection, id: i64) -> Result<Option<String>> {
    sqlx::query_scalar("SELECT email FROM recovery_emails WHERE user_id = ?")
        .bind(id)
        .fetch_optional(conn)
        .await
        .map_err(database_error)
}

fn token_hash(token: &str) -> String {
    format!("{:x}", Sha256::digest(token.as_bytes()))
}

async fn issue_token(
    conn: &mut SqliteConnection,
    user_id: i64,
    kind: &str,
    pending_email: Option<&str>,
) -> Result<String> {
    sqlx::query("DELETE FROM recovery_tokens WHERE expires_at <= ? OR (user_id = ? AND kind = ?)")
        .bind(chrono::Utc::now().timestamp())
        .bind(user_id)
        .bind(kind)
        .execute(&mut *conn)
        .await
        .map_err(database_error)?;
    let token = auth::generate_session_token();
    sqlx::query("INSERT INTO recovery_tokens (hash, user_id, kind, pending_email, expires_at) VALUES (?, ?, ?, ?, ?)")
        .bind(token_hash(&token)).bind(user_id).bind(kind).bind(pending_email)
        .bind(chrono::Utc::now().timestamp() + TOKEN_LIFETIME_SECONDS)
        .execute(conn).await.map_err(database_error)?;
    Ok(token)
}

async fn find_token(conn: &mut SqliteConnection, token: &str, kind: &str) -> Result<Token> {
    if token.len() != 43 {
        return Err(AppError::Validation(INVALID_LINK.into()));
    }
    sqlx::query_as("SELECT user_id, pending_email FROM recovery_tokens WHERE hash = ? AND kind = ? AND expires_at > ?")
        .bind(token_hash(token)).bind(kind).bind(chrono::Utc::now().timestamp())
        .fetch_optional(conn).await.map_err(database_error)?
        .ok_or_else(|| AppError::Validation(INVALID_LINK.into()))
}

async fn clear_tokens(conn: &mut SqliteConnection, id: i64) -> Result<()> {
    sqlx::query("DELETE FROM recovery_tokens WHERE user_id = ?")
        .bind(id)
        .execute(conn)
        .await
        .map_err(database_error)?;
    Ok(())
}

async fn ensure_available(conn: &mut SqliteConnection, id: i64, address: &str) -> Result<()> {
    let taken: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM recovery_emails WHERE email = ? AND user_id != ?)",
    )
    .bind(address)
    .bind(id)
    .fetch_one(conn)
    .await
    .map_err(database_error)?;
    if taken {
        return Err(AppError::Conflict(
            "This email address is already in use.".into(),
        ));
    }
    Ok(())
}

pub async fn settings(pool: &SqlitePool, user_id: i64) -> Result<Settings> {
    let mut conn = pool.acquire().await.map_err(database_error)?;
    let user = account(&mut conn, user_id).await?;
    Ok(Settings {
        eligible: user.eligible(),
        email: email(&mut conn, user_id).await?,
    })
}

pub async fn enroll(
    pool: &SqlitePool,
    user_id: i64,
    address: &str,
    current_password: &str,
) -> Result<MailData> {
    let address = normalize_email(address)?;
    let authenticated_hash = authenticate(pool, user_id, current_password).await?;
    let mut tx = begin(pool).await?;
    let user = account(&mut tx, user_id).await?;
    require_same_password(&user, &authenticated_hash)?;
    ensure_available(&mut tx, user_id, &address).await?;
    let token = issue_token(&mut tx, user_id, "verify", Some(&address)).await?;
    tx.commit().await.map_err(database_error)?;
    Ok(user.mail(address, Some(token)))
}

pub async fn verify(pool: &SqlitePool, value: &str) -> Result<Option<MailData>> {
    let mut tx = begin(pool).await?;
    let token = find_token(&mut tx, value, "verify").await?;
    let user = account(&mut tx, token.user_id).await?;
    require_eligible(&user)?;
    let address = token
        .pending_email
        .ok_or_else(|| AppError::Validation(INVALID_LINK.into()))?;
    ensure_available(&mut tx, user.id, &address).await?;
    let previous = email(&mut tx, user.id).await?;
    sqlx::query("INSERT INTO recovery_emails (user_id, email) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET email = excluded.email")
        .bind(user.id).bind(&address).execute(&mut *tx).await.map_err(database_error)?;
    clear_tokens(&mut tx, user.id).await?;
    tx.commit().await.map_err(database_error)?;
    Ok(previous
        .filter(|old| old != &address)
        .map(|old| user.mail(old, None)))
}

pub async fn remove(
    pool: &SqlitePool,
    user_id: i64,
    current_password: &str,
) -> Result<Option<MailData>> {
    let authenticated_hash = authenticate(pool, user_id, current_password).await?;
    let mut tx = begin(pool).await?;
    let user = account(&mut tx, user_id).await?;
    require_same_password(&user, &authenticated_hash)?;
    let previous = clear_email(&mut tx, user_id).await?;
    tx.commit().await.map_err(database_error)?;
    Ok(previous.map(|old| user.mail(old, None)))
}

async fn clear_email(conn: &mut SqliteConnection, id: i64) -> Result<Option<String>> {
    let previous = email(conn, id).await?;
    sqlx::query("DELETE FROM recovery_emails WHERE user_id = ?")
        .bind(id)
        .execute(&mut *conn)
        .await
        .map_err(database_error)?;
    clear_tokens(conn, id).await?;
    Ok(previous)
}

pub async fn admin_clear(
    pool: &SqlitePool,
    actor_id: i64,
    target_id: i64,
) -> Result<Option<MailData>> {
    let mut tx = begin(pool).await?;
    let actor = account(&mut tx, actor_id).await?;
    let target = account(&mut tx, target_id).await?;
    let allowed = actor.unexpired
        && match actor.role.as_str() {
            "superadmin" => matches!(target.role.as_str(), "admin" | "host" | "guest"),
            "admin" => matches!(target.role.as_str(), "host" | "guest"),
            _ => false,
        };
    if !allowed {
        return Err(AppError::Forbidden(
            "You cannot clear recovery email for this user.".into(),
        ));
    }
    let previous = clear_email(&mut tx, target_id).await?;
    tx.commit().await.map_err(database_error)?;
    Ok(previous.map(|old| target.mail(old, None)))
}

pub async fn request_reset(pool: &SqlitePool, address: &str) -> Result<Option<MailData>> {
    let address = normalize_email(address)?;
    let mut tx = begin(pool).await?;
    let id: Option<i64> = sqlx::query_scalar("SELECT user_id FROM recovery_emails WHERE email = ?")
        .bind(&address)
        .fetch_optional(&mut *tx)
        .await
        .map_err(database_error)?;
    let Some(id) = id else { return Ok(None) };
    let user = account(&mut tx, id).await?;
    if !user.eligible() {
        return Ok(None);
    }
    let token = issue_token(&mut tx, id, "reset", None).await?;
    tx.commit().await.map_err(database_error)?;
    Ok(Some(user.mail(address, Some(token))))
}

/// All password writers use this inside their transaction, retaining their own
/// forced-change and session behavior while revoking outstanding reset links.
pub async fn update_password(
    conn: &mut SqliteConnection,
    user_id: i64,
    hash: &str,
    must_change: bool,
) -> Result<()> {
    sqlx::query("UPDATE users SET password_hash = ?, must_change_password = ?, updated_at = datetime('now') WHERE id = ?")
        .bind(hash).bind(must_change).bind(user_id).execute(&mut *conn).await.map_err(database_error)?;
    sqlx::query("DELETE FROM recovery_tokens WHERE user_id = ? AND kind = 'reset'")
        .bind(user_id)
        .execute(conn)
        .await
        .map_err(database_error)?;
    Ok(())
}

pub async fn complete_reset(
    pool: &SqlitePool,
    value: &str,
    new_password: &str,
) -> Result<MailData> {
    if new_password.len() < 8 {
        return Err(AppError::Validation(
            "Password must be at least 8 characters.".into(),
        ));
    }
    // Reject invalid links before spending time on Argon2, then recheck inside
    // the write transaction so concurrent submissions cannot both succeed.
    {
        let mut conn = pool.acquire().await.map_err(database_error)?;
        find_token(&mut conn, value, "reset").await?;
    }
    let hash = auth::hash_password(new_password)?;
    let mut tx = begin(pool).await?;
    let token = find_token(&mut tx, value, "reset").await?;
    let user = account(&mut tx, token.user_id).await?;
    require_eligible(&user)?;
    let address = email(&mut tx, user.id)
        .await?
        .ok_or_else(|| AppError::Validation(INVALID_LINK.into()))?;
    update_password(&mut tx, user.id, &hash, false).await?;
    clear_tokens(&mut tx, user.id).await?;
    sqlx::query("DELETE FROM sessions WHERE user_id = ?")
        .bind(user.id)
        .execute(&mut *tx)
        .await
        .map_err(database_error)?;
    tx.commit().await.map_err(database_error)?;
    Ok(user.mail(address, None))
}

#[cfg(test)]
mod tests {
    use super::*;
    use sqlx::sqlite::{SqliteConnectOptions, SqlitePoolOptions};
    use std::{sync::Arc, time::Duration};

    const PASSWORD: &str = "original-password";

    async fn database() -> (tempfile::TempDir, SqlitePool) {
        let dir = tempfile::tempdir().unwrap();
        let options = SqliteConnectOptions::new()
            .filename(dir.path().join("recovery.sqlite"))
            .create_if_missing(true)
            .foreign_keys(true)
            .busy_timeout(Duration::from_secs(5));
        let pool = SqlitePoolOptions::new()
            .max_connections(2)
            .connect_with(options)
            .await
            .unwrap();
        crate::db::run_migrations(&pool).await.unwrap();
        migrate(&pool).await.unwrap();
        (dir, pool)
    }

    async fn user(pool: &SqlitePool, name: &str, role: &str) -> i64 {
        let hash = auth::hash_password(PASSWORD).unwrap();
        sqlx::query("INSERT INTO users(username, password_hash, role, must_change_password) VALUES (?, ?, ?, 1)")
            .bind(name).bind(hash).bind(role).execute(pool).await.unwrap().last_insert_rowid()
    }

    async fn verified(pool: &SqlitePool, id: i64, address: &str) {
        let mail = enroll(pool, id, address, PASSWORD).await.unwrap();
        verify(pool, mail.token.as_deref().unwrap()).await.unwrap();
    }

    async fn reset_token(pool: &SqlitePool, address: &str) -> String {
        request_reset(pool, address)
            .await
            .unwrap()
            .unwrap()
            .token
            .unwrap()
    }

    async fn count(pool: &SqlitePool, table: &str, id: i64) -> i64 {
        sqlx::query_scalar(&format!("SELECT COUNT(*) FROM {table} WHERE user_id = ?"))
            .bind(id)
            .fetch_one(pool)
            .await
            .unwrap()
    }

    #[tokio::test]
    async fn enrollment_reset_and_password_login() {
        let (_dir, pool) = database().await;
        let id = user(&pool, "host", "host").await;
        let mail = enroll(&pool, id, " Host@Example.COM ", PASSWORD)
            .await
            .unwrap();
        assert_eq!(mail.email, "host@example.com");
        assert_eq!(mail.username, "host");
        assert!(settings(&pool, id).await.unwrap().email.is_none());
        assert!(request_reset(&pool, "host@example.com")
            .await
            .unwrap()
            .is_none());
        let token = mail.token.unwrap();
        let stored: String =
            sqlx::query_scalar("SELECT hash FROM recovery_tokens WHERE user_id = ?")
                .bind(id)
                .fetch_one(&pool)
                .await
                .unwrap();
        assert_ne!(stored, token);
        assert_eq!(stored.len(), 64);
        verify(&pool, &token).await.unwrap();
        assert!(verify(&pool, &token).await.is_err());
        sqlx::query("INSERT INTO sessions(token, user_id, expires_at) VALUES ('session', ?, '2099-01-01T00:00:00')")
            .bind(id).execute(&pool).await.unwrap();
        let reset = reset_token(&pool, "HOST@example.com").await;
        let pending = enroll(&pool, id, "replacement@example.com", PASSWORD)
            .await
            .unwrap();
        let notification = complete_reset(&pool, &reset, "changed-password")
            .await
            .unwrap();
        assert_eq!(notification.email, "host@example.com");
        assert!(notification.token.is_none());
        let (hash, forced, role, expires): (String, bool, String, Option<String>) = sqlx::query_as(
            "SELECT password_hash, must_change_password, role, expires_at FROM users WHERE id = ?",
        )
        .bind(id)
        .fetch_one(&pool)
        .await
        .unwrap();
        assert!(auth::verify_password("changed-password", &hash));
        assert!(!auth::verify_password(PASSWORD, &hash));
        assert!(!forced);
        assert_eq!(role, "host");
        assert!(expires.is_none());
        assert_eq!(count(&pool, "sessions", id).await, 0);
        assert_eq!(count(&pool, "recovery_tokens", id).await, 0);
        assert!(complete_reset(&pool, &reset, "another-password")
            .await
            .is_err());
        assert!(verify(&pool, pending.token.as_deref().unwrap())
            .await
            .is_err());
    }

    #[tokio::test]
    async fn invalid_expired_and_wrong_kind_links_leave_password_unchanged() {
        let (_dir, pool) = database().await;
        let id = user(&pool, "host", "host").await;
        verified(&pool, id, "host@example.com").await;
        let reset = reset_token(&pool, "host@example.com").await;
        assert!(verify(&pool, &reset).await.is_err());
        assert!(complete_reset(&pool, "invalid", "changed-password")
            .await
            .is_err());
        assert!(complete_reset(&pool, &reset, "short").await.is_err());
        sqlx::query("UPDATE recovery_tokens SET expires_at = 0")
            .execute(&pool)
            .await
            .unwrap();
        assert!(complete_reset(&pool, &reset, "changed-password")
            .await
            .is_err());
        let mut conn = pool.acquire().await.unwrap();
        assert!(auth::verify_password(
            PASSWORD,
            &account(&mut conn, id).await.unwrap().password_hash
        ));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn two_connections_can_consume_a_reset_only_once() {
        let (_dir, pool) = database().await;
        let first = pool.acquire().await.unwrap();
        let second = pool.acquire().await.unwrap();
        drop((first, second));
        let id = user(&pool, "host", "host").await;
        verified(&pool, id, "host@example.com").await;
        let token = reset_token(&pool, "host@example.com").await;
        let barrier = Arc::new(tokio::sync::Barrier::new(2));
        let mut tasks = Vec::new();
        for password in ["password-one", "password-two"] {
            let pool = pool.clone();
            let token = token.clone();
            let barrier = barrier.clone();
            tasks.push(tokio::spawn(async move {
                barrier.wait().await;
                (password, complete_reset(&pool, &token, password).await)
            }));
        }
        let mut winners = Vec::new();
        for task in tasks {
            let (password, result) = task.await.unwrap();
            match result {
                Ok(_) => winners.push(password),
                Err(error) => assert!(matches!(error, AppError::Validation(_))),
            }
        }
        assert_eq!(winners.len(), 1);
        let mut conn = pool.acquire().await.unwrap();
        assert!(auth::verify_password(
            winners[0],
            &account(&mut conn, id).await.unwrap().password_hash
        ));
    }

    #[tokio::test]
    async fn replacement_is_pending_until_verified_and_clears_links() {
        let (_dir, pool) = database().await;
        let id = user(&pool, "host", "host").await;
        verified(&pool, id, "old@example.com").await;
        let reset = reset_token(&pool, "old@example.com").await;
        let pending = enroll(&pool, id, "new@example.com", PASSWORD)
            .await
            .unwrap();
        assert_eq!(
            settings(&pool, id).await.unwrap().email.as_deref(),
            Some("old@example.com")
        );
        assert!(request_reset(&pool, "new@example.com")
            .await
            .unwrap()
            .is_none());
        let notification = verify(&pool, pending.token.as_deref().unwrap())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(notification.email, "old@example.com");
        assert_eq!(
            settings(&pool, id).await.unwrap().email.as_deref(),
            Some("new@example.com")
        );
        assert!(complete_reset(&pool, &reset, "changed-password")
            .await
            .is_err());
        let pending = enroll(&pool, id, "third@example.com", PASSWORD)
            .await
            .unwrap();
        let reset = reset_token(&pool, "new@example.com").await;
        let notification = remove(&pool, id, PASSWORD).await.unwrap().unwrap();
        assert_eq!(notification.email, "new@example.com");
        assert!(settings(&pool, id).await.unwrap().email.is_none());
        assert!(verify(&pool, pending.token.as_deref().unwrap())
            .await
            .is_err());
        assert!(complete_reset(&pool, &reset, "changed-password")
            .await
            .is_err());
    }

    #[tokio::test]
    async fn enrollment_requires_current_password_and_unique_verified_address() {
        let (_dir, pool) = database().await;
        let first = user(&pool, "first", "host").await;
        let second = user(&pool, "second", "admin").await;
        assert!(enroll(&pool, first, "one@example.com", "wrong")
            .await
            .is_err());
        assert!(enroll(&pool, first, "invalid", PASSWORD).await.is_err());
        let first_pending = enroll(&pool, first, "same@example.com", PASSWORD)
            .await
            .unwrap();
        let second_pending = enroll(&pool, second, "SAME@example.com", PASSWORD)
            .await
            .unwrap();
        verify(&pool, first_pending.token.as_deref().unwrap())
            .await
            .unwrap();
        assert!(matches!(
            verify(&pool, second_pending.token.as_deref().unwrap()).await,
            Err(AppError::Conflict(_))
        ));
        assert!(matches!(
            enroll(&pool, second, "same@example.com", PASSWORD).await,
            Err(AppError::Conflict(_))
        ));
        assert!(remove(&pool, first, "wrong").await.is_err());
        assert_eq!(
            settings(&pool, first).await.unwrap().email.as_deref(),
            Some("same@example.com")
        );
    }

    #[tokio::test]
    async fn roles_and_expiry_are_rechecked_when_consuming_links() {
        let (_dir, pool) = database().await;
        let id = user(&pool, "host", "host").await;
        verified(&pool, id, "host@example.com").await;
        let reset = reset_token(&pool, "host@example.com").await;
        let pending = enroll(&pool, id, "pending@example.com", PASSWORD)
            .await
            .unwrap();
        for role in ["guest", "superadmin", "unknown"] {
            sqlx::query("UPDATE users SET role = ? WHERE id = ?")
                .bind(role)
                .bind(id)
                .execute(&pool)
                .await
                .unwrap();
            assert!(!settings(&pool, id).await.unwrap().eligible);
            assert!(request_reset(&pool, "host@example.com")
                .await
                .unwrap()
                .is_none());
            assert!(complete_reset(&pool, &reset, "changed-password")
                .await
                .is_err());
            assert!(verify(&pool, pending.token.as_deref().unwrap())
                .await
                .is_err());
            assert!(enroll(&pool, id, "new@example.com", PASSWORD)
                .await
                .is_err());
        }
        for expiry in ["2000-01-01T00:00:00", "malformed"] {
            sqlx::query("UPDATE users SET role = 'host', expires_at = ? WHERE id = ?")
                .bind(expiry)
                .bind(id)
                .execute(&pool)
                .await
                .unwrap();
            assert!(!settings(&pool, id).await.unwrap().eligible);
            assert!(request_reset(&pool, "host@example.com")
                .await
                .unwrap()
                .is_none());
            assert!(complete_reset(&pool, &reset, "changed-password")
                .await
                .is_err());
        }
        for expiry in ["2099-01-01T00:00:00", "2099-01-01T00:00:00+00:00"] {
            sqlx::query("UPDATE users SET expires_at = ? WHERE id = ?")
                .bind(expiry)
                .bind(id)
                .execute(&pool)
                .await
                .unwrap();
            assert!(settings(&pool, id).await.unwrap().eligible);
        }
        assert!(request_reset(&pool, "unknown@example.com")
            .await
            .unwrap()
            .is_none());
    }

    #[tokio::test]
    async fn admin_clear_enforces_hierarchy_and_clears_pending_and_reset_links() {
        let (_dir, pool) = database().await;
        let host = user(&pool, "host", "host").await;
        let admin = user(&pool, "admin", "admin").await;
        let superadmin = user(&pool, "superadmin", "superadmin").await;
        verified(&pool, host, "host@example.com").await;
        let reset = reset_token(&pool, "host@example.com").await;
        let pending = enroll(&pool, host, "pending@example.com", PASSWORD)
            .await
            .unwrap();
        assert!(admin_clear(&pool, host, admin).await.is_err());
        assert!(admin_clear(&pool, admin, admin).await.is_err());
        assert!(admin_clear(&pool, admin, superadmin).await.is_err());
        let notification = admin_clear(&pool, admin, host).await.unwrap().unwrap();
        assert_eq!(notification.email, "host@example.com");
        assert!(settings(&pool, host).await.unwrap().email.is_none());
        assert!(complete_reset(&pool, &reset, "changed-password")
            .await
            .is_err());
        assert!(verify(&pool, pending.token.as_deref().unwrap())
            .await
            .is_err());
        verified(&pool, admin, "admin@example.com").await;
        admin_clear(&pool, superadmin, admin).await.unwrap();
        assert!(settings(&pool, admin).await.unwrap().email.is_none());
    }

    #[tokio::test]
    async fn password_writer_revokes_reset_links_and_preserves_forced_flag_choice() {
        let (_dir, pool) = database().await;
        let id = user(&pool, "host", "host").await;
        verified(&pool, id, "host@example.com").await;
        for forced in [true, false] {
            let token = reset_token(&pool, "host@example.com").await;
            let hash = auth::hash_password("updated-password").unwrap();
            let mut tx = begin(&pool).await.unwrap();
            update_password(&mut tx, id, &hash, forced).await.unwrap();
            tx.commit().await.unwrap();
            assert!(complete_reset(&pool, &token, "changed-password")
                .await
                .is_err());
            let stored: bool =
                sqlx::query_scalar("SELECT must_change_password FROM users WHERE id = ?")
                    .bind(id)
                    .fetch_one(&pool)
                    .await
                    .unwrap();
            assert_eq!(stored, forced);
        }
    }

    #[tokio::test]
    async fn busy_database_returns_a_retry_error() {
        let (dir, pool) = database().await;
        let competing = SqlitePoolOptions::new()
            .max_connections(1)
            .connect_with(
                SqliteConnectOptions::new()
                    .filename(dir.path().join("recovery.sqlite"))
                    .busy_timeout(Duration::from_millis(20)),
            )
            .await
            .unwrap();
        let tx = begin(&pool).await.unwrap();
        assert!(matches!(
            request_reset(&competing, "unknown@example.com").await,
            Err(AppError::ServiceUnavailable(_))
        ));
        tx.rollback().await.unwrap();
        assert!(request_reset(&competing, "unknown@example.com")
            .await
            .unwrap()
            .is_none());
    }
}
