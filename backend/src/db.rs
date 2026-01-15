use crate::config::Config;
use sqlx::Row;
use sqlx::SqlitePool;

async fn add_column_if_missing(
    pool: &SqlitePool,
    table: &str,
    column: &str,
    decl: &str,
) -> Result<(), sqlx::Error> {
    let pragma = format!("PRAGMA table_info({})", table);
    let rows = sqlx::query(&pragma).fetch_all(pool).await?;

    let exists = rows.iter().any(|row| {
        let name: String = row.get("name");
        name == column
    });

    if exists {
        return Ok(());
    }

    let alter = format!("ALTER TABLE {} ADD COLUMN {} {}", table, column, decl);
    sqlx::query(&alter).execute(pool).await?;
    Ok(())
}

pub async fn run_migrations(pool: &SqlitePool) -> Result<(), sqlx::Error> {
    sqlx::query(
        r#"
        CREATE TABLE IF NOT EXISTS artists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            pronouns TEXT NOT NULL,
            
            pic_key TEXT,
            pic_cropped_key TEXT,
            pic_overlay_key TEXT,
            voice_message_key TEXT,
            no_voice_message INTEGER NOT NULL DEFAULT 0,
            
            track1_name TEXT NOT NULL,
            track1_key TEXT,
            track2_name TEXT NOT NULL,
            track2_key TEXT,
            
            instagram TEXT,
            soundcloud TEXT,
            bandcamp TEXT,
            spotify TEXT,
            other_social TEXT,
            
            upcoming_events TEXT,
            mentions TEXT,
            
            status TEXT NOT NULL DEFAULT 'unassigned',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT
        )
        "#,
    )
    .execute(pool)
    .await?;

    // Normalize legacy artist statuses (pending/approved/rejected) to the new model.
    // Keep DB values consistent with the assignment table.
    sqlx::query(
        "UPDATE artists SET status = 'unassigned' WHERE status NOT IN ('assigned', 'unassigned')",
    )
    .execute(pool)
    .await?;
    sqlx::query(
        "UPDATE artists SET status = 'assigned' WHERE id IN (SELECT artist_id FROM artist_show_assignments)",
    )
    .execute(pool)
    .await?;

    // Backfill schema for existing DBs created before columns were added.
    add_column_if_missing(pool, "artists", "pic_cropped_key", "TEXT").await?;
    add_column_if_missing(pool, "artists", "pic_overlay_key", "TEXT").await?;

    // Original audio file keys (stored separately from mp3 conversions)
    add_column_if_missing(pool, "artists", "track1_original_key", "TEXT").await?;
    add_column_if_missing(pool, "artists", "track2_original_key", "TEXT").await?;
    add_column_if_missing(pool, "artists", "voice_original_key", "TEXT").await?;

    sqlx::query(
        r#"
        CREATE TABLE IF NOT EXISTS shows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            date TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'scheduled',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT
        )
        "#,
    )
    .execute(pool)
    .await?;

    // Add cover_generated_at column for tracking cover regeneration
    add_column_if_missing(pool, "shows", "cover_generated_at", "TEXT").await?;

    // Add recording_key column for storing final show recording
    add_column_if_missing(pool, "shows", "recording_key", "TEXT").await?;

    // Add recording_filename column to store original filename
    add_column_if_missing(pool, "shows", "recording_filename", "TEXT").await?;

    // Normalize legacy datetime-local values (e.g. 2026-01-04T20:00) into YYYY-MM-DD.
    // We keep the column type as TEXT, but only store the date portion going forward.
    sqlx::query(
        r#"
        UPDATE shows
        SET date = substr(date, 1, 10)
        WHERE length(date) > 10
          AND date GLOB '????-??-??*'
        "#,
    )
    .execute(pool)
    .await?;

    sqlx::query(
        r#"
        CREATE TABLE IF NOT EXISTS artist_show_assignments (
            artist_id INTEGER NOT NULL,
            show_id INTEGER NOT NULL,
            PRIMARY KEY (artist_id, show_id),
            FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE,
            FOREIGN KEY (show_id) REFERENCES shows(id) ON DELETE CASCADE
        )
        "#,
    )
    .execute(pool)
    .await?;

    // Enforce: a show can have at most 4 assigned artists.
    // This protects all code paths and avoids race conditions.
    sqlx::query(
        r#"
        CREATE TRIGGER IF NOT EXISTS trg_max_artists_per_show
        BEFORE INSERT ON artist_show_assignments
        BEGIN
            SELECT CASE
                WHEN (SELECT COUNT(*) FROM artist_show_assignments WHERE show_id = NEW.show_id) >= 4
                THEN RAISE(ABORT, 'show has maximum number of artists (4)')
            END;
        END;
        "#,
    )
    .execute(pool)
    .await?;

    // Enforce: an artist can be assigned to at most 1 show.
    // If a DB already contains multiple assignments per artist, keep the most recent one.
    sqlx::query(
        r#"
        DELETE FROM artist_show_assignments
        WHERE rowid NOT IN (
            SELECT MAX(rowid)
            FROM artist_show_assignments
            GROUP BY artist_id
        )
        "#,
    )
    .execute(pool)
    .await?;

    sqlx::query(
        r#"
        CREATE UNIQUE INDEX IF NOT EXISTS idx_one_show_per_artist
        ON artist_show_assignments(artist_id)
        "#,
    )
    .execute(pool)
    .await?;

    // Users table for role-based authentication
    sqlx::query(
        r#"
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'artist',
            created_by INTEGER,
            expires_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT,
            FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
        )
        "#,
    )
    .execute(pool)
    .await?;

    // Migration: add user_id column to existing sessions table if missing
    add_column_if_missing(pool, "sessions", "user_id", "INTEGER").await?;

    sqlx::query(
        r#"
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        "#,
    )
    .execute(pool)
    .await?;

    // Pending submissions for chunked uploads (each file sent separately to stay under 100MB)
    sqlx::query(
        r#"
        CREATE TABLE IF NOT EXISTS pending_submissions (
            session_id TEXT PRIMARY KEY,
            artist_name TEXT NOT NULL,
            pronouns TEXT NOT NULL,
            track1_name TEXT NOT NULL,
            track2_name TEXT NOT NULL,
            no_voice_message INTEGER NOT NULL DEFAULT 0,
            instagram TEXT,
            soundcloud TEXT,
            bandcamp TEXT,
            spotify TEXT,
            other_social TEXT,
            upcoming_events TEXT,
            mentions TEXT,
            pic_key TEXT,
            pic_cropped_key TEXT,
            pic_overlay_key TEXT,
            track1_key TEXT,
            track2_key TEXT,
            voice_key TEXT,
            track1_original_key TEXT,
            track2_original_key TEXT,
            voice_original_key TEXT,
            track1_conversion_status TEXT DEFAULT 'none',
            track2_conversion_status TEXT DEFAULT 'none',
            voice_conversion_status TEXT DEFAULT 'none',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL
        )
        "#,
    )
    .execute(pool)
    .await?;

    // Add original audio key columns for pending submissions if missing
    add_column_if_missing(pool, "pending_submissions", "track1_original_key", "TEXT").await?;
    add_column_if_missing(pool, "pending_submissions", "track2_original_key", "TEXT").await?;
    add_column_if_missing(pool, "pending_submissions", "voice_original_key", "TEXT").await?;

    // Add conversion status columns (none, pending, completed, failed)
    add_column_if_missing(
        pool,
        "pending_submissions",
        "track1_conversion_status",
        "TEXT DEFAULT 'none'",
    )
    .await?;
    add_column_if_missing(
        pool,
        "pending_submissions",
        "track2_conversion_status",
        "TEXT DEFAULT 'none'",
    )
    .await?;
    add_column_if_missing(
        pool,
        "pending_submissions",
        "voice_conversion_status",
        "TEXT DEFAULT 'none'",
    )
    .await?;

    // Clean up expired pending submissions (older than 1 hour)
    sqlx::query("DELETE FROM pending_submissions WHERE expires_at < datetime('now')")
        .execute(pool)
        .await?;

    // Pending recording uploads for chunked uploads (to bypass Cloudflare 100MB limit)
    sqlx::query(
        r#"
        CREATE TABLE IF NOT EXISTS pending_recording_uploads (
            session_id TEXT PRIMARY KEY,
            show_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            total_size INTEGER NOT NULL,
            total_chunks INTEGER NOT NULL,
            peaks_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            FOREIGN KEY (show_id) REFERENCES shows(id) ON DELETE CASCADE
        )
        "#,
    )
    .execute(pool)
    .await?;

    // Pending recording chunks (tracks which chunks have been uploaded)
    sqlx::query(
        r#"
        CREATE TABLE IF NOT EXISTS pending_recording_chunks (
            session_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            chunk_key TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (session_id, chunk_index),
            FOREIGN KEY (session_id) REFERENCES pending_recording_uploads(session_id) ON DELETE CASCADE
        )
        "#,
    )
    .execute(pool)
    .await?;

    // Clean up expired pending recording uploads
    sqlx::query("DELETE FROM pending_recording_uploads WHERE expires_at < datetime('now')")
        .execute(pool)
        .await?;

    // Pending file uploads for chunked submit form uploads (to bypass Cloudflare 100MB limit per file)
    sqlx::query(
        r#"
        CREATE TABLE IF NOT EXISTS pending_file_uploads (
            file_session_id TEXT PRIMARY KEY,
            parent_session_id TEXT NOT NULL,
            field TEXT NOT NULL,
            filename TEXT NOT NULL,
            total_size INTEGER NOT NULL,
            total_chunks INTEGER NOT NULL,
            peaks_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            FOREIGN KEY (parent_session_id) REFERENCES pending_submissions(session_id) ON DELETE CASCADE
        )
        "#,
    )
    .execute(pool)
    .await?;

    // Add peaks_json columns to pending_submissions for each file type
    add_column_if_missing(pool, "pending_submissions", "peaks_json_track1", "TEXT").await?;
    add_column_if_missing(pool, "pending_submissions", "peaks_json_track2", "TEXT").await?;
    add_column_if_missing(pool, "pending_submissions", "peaks_json_voice", "TEXT").await?;

    // Clean up expired pending file uploads
    sqlx::query("DELETE FROM pending_file_uploads WHERE expires_at < datetime('now')")
        .execute(pool)
        .await?;

    tracing::info!("Database migrations completed");
    Ok(())
}

/// Seed the superadmin user if no users exist
pub async fn seed_superadmin(pool: &SqlitePool, config: &Config) -> Result<(), sqlx::Error> {
    // Check if any users exist
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM users")
        .fetch_one(pool)
        .await?;

    if count == 0 {
        tracing::info!(
            "No users found, seeding superadmin user: {}",
            config.superadmin_username
        );

        sqlx::query(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'superadmin')",
        )
        .bind(&config.superadmin_username)
        .bind(&config.superadmin_password_hash)
        .execute(pool)
        .await?;

        tracing::info!("Superadmin user created successfully");
    }

    Ok(())
}
