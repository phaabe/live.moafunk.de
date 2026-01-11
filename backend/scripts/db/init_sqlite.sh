#!/usr/bin/env bash
set -euo pipefail

# Initialize a fresh SQLite database with the current schema.
# This is safe to run on a new or empty file. If the DB exists, it will be backed up first.
#
# Usage:
#   DB_PATH=/opt/unheard-backend/data/unheard.db ./backend/scripts/db/init_sqlite.sh
#   ./backend/scripts/db/init_sqlite.sh                  # uses ./data/unheard.db relative to repo
#
# Notes:
# - This mirrors the schema defined in src/db.rs (run_migrations).
# - It does NOT seed users; the app will seed the superadmin on first start if none exist.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DB_PATH="${DB_PATH:-$REPO_ROOT/backend/data/unheard.db}"

mkdir -p "$(dirname "$DB_PATH")"

if [[ -f "$DB_PATH" ]]; then
  TS=$(date +%Y%m%d-%H%M%S)
  BACKUP="${DB_PATH}.bak-${TS}"
  echo "Backing up existing DB to $BACKUP"
  cp "$DB_PATH" "$BACKUP"
fi

echo "Initializing schema at $DB_PATH"

sqlite3 "$DB_PATH" <<'SQL'
PRAGMA foreign_keys = ON;

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
    updated_at TEXT,
    track1_original_key TEXT,
    track2_original_key TEXT,
    voice_original_key TEXT
);

CREATE TABLE IF NOT EXISTS shows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    date TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT,
    cover_generated_at TEXT,
    recording_key TEXT
);

CREATE TABLE IF NOT EXISTS artist_show_assignments (
    artist_id INTEGER NOT NULL,
    show_id INTEGER NOT NULL,
    PRIMARY KEY (artist_id, show_id),
    FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE,
    FOREIGN KEY (show_id) REFERENCES shows(id) ON DELETE CASCADE
);

CREATE TRIGGER IF NOT EXISTS trg_max_artists_per_show
BEFORE INSERT ON artist_show_assignments
BEGIN
    SELECT CASE
        WHEN (SELECT COUNT(*) FROM artist_show_assignments WHERE show_id = NEW.show_id) >= 4
        THEN RAISE(ABORT, 'show has maximum number of artists (4)')
    END;
END;

DELETE FROM artist_show_assignments
WHERE rowid NOT IN (
    SELECT MAX(rowid)
    FROM artist_show_assignments
    GROUP BY artist_id
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_one_show_per_artist
ON artist_show_assignments(artist_id);

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
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

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
);

DELETE FROM pending_submissions WHERE expires_at < datetime('now');
SQL

echo "Done."
