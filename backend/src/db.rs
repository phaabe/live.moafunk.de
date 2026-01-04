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
            
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT
        )
        "#,
    )
    .execute(pool)
    .await?;

    // Backfill schema for existing DBs created before columns were added.
    add_column_if_missing(pool, "artists", "pic_cropped_key", "TEXT").await?;
    add_column_if_missing(pool, "artists", "pic_overlay_key", "TEXT").await?;

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

    sqlx::query(
        r#"
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL
        )
        "#,
    )
    .execute(pool)
    .await?;

    tracing::info!("Database migrations completed");
    Ok(())
}
