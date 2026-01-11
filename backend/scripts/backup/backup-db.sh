#!/bin/bash
# Backup SQLite database to R2 backup bucket
# Usage: ./backup-db.sh [--local-only]
#
# Environment variables:
#   DATABASE_PATH    - SQLite database path (default: /app/data/unheard.db)
#   BACKUP_BUCKET    - R2 backup bucket name (default: unheard-backups)
#   BACKUP_RETENTION - Number of backups to keep (default: 28 = 4 weeks daily)
#   RCLONE_REMOTE    - rclone remote name for backup bucket (default: r2-backup)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

# Load .env if exists
if [[ -f "$BACKEND_DIR/.env" ]]; then
    set -a
    source "$BACKEND_DIR/.env"
    set +a
fi

DB_PATH="${DATABASE_PATH:-/app/data/unheard.db}"
BACKUP_BUCKET="${BACKUP_BUCKET:-unheard-backups}"
BACKUP_RETENTION="${BACKUP_RETENTION:-28}"
RCLONE_REMOTE="${RCLONE_REMOTE:-r2-backup}"
LOCAL_ONLY="${1:-}"

TIMESTAMP=$(date +%Y-%m-%d_%H-%M-%S)
BACKUP_NAME="unheard-db-$TIMESTAMP.db"
LOCAL_BACKUP_DIR="/tmp/db-backups"
LOCAL_BACKUP_PATH="$LOCAL_BACKUP_DIR/$BACKUP_NAME"

echo "=== Database Backup ==="
echo "Source: $DB_PATH"
echo "Timestamp: $TIMESTAMP"
echo ""

# Check database exists
if [[ ! -f "$DB_PATH" ]]; then
    echo "Error: Database not found at $DB_PATH"
    exit 1
fi

# Create local backup directory
mkdir -p "$LOCAL_BACKUP_DIR"

# Create backup using SQLite's backup command (safe for concurrent access)
echo "Creating local backup..."
sqlite3 "$DB_PATH" ".backup '$LOCAL_BACKUP_PATH'"

# Compress backup
echo "Compressing backup..."
gzip "$LOCAL_BACKUP_PATH"
LOCAL_BACKUP_PATH="${LOCAL_BACKUP_PATH}.gz"
BACKUP_NAME="${BACKUP_NAME}.gz"

BACKUP_SIZE=$(du -h "$LOCAL_BACKUP_PATH" | cut -f1)
echo "  Created: $LOCAL_BACKUP_PATH ($BACKUP_SIZE)"

if [[ "$LOCAL_ONLY" == "--local-only" ]]; then
    echo ""
    echo "Local-only mode: Skipping R2 upload"
    echo "Backup saved to: $LOCAL_BACKUP_PATH"
    exit 0
fi

# Upload to R2 backup bucket
echo ""
echo "Uploading to R2..."
if ! command -v rclone &> /dev/null; then
    echo "Error: rclone is not installed"
    echo "Install with: curl https://rclone.org/install.sh | sudo bash"
    exit 1
fi

rclone copy "$LOCAL_BACKUP_PATH" "$RCLONE_REMOTE:$BACKUP_BUCKET/db/" --progress

echo "  Uploaded to: $RCLONE_REMOTE:$BACKUP_BUCKET/db/$BACKUP_NAME"

# Cleanup old local backups (keep last 3 locally)
echo ""
echo "Cleaning up old local backups (keeping last 3)..."
ls -t "$LOCAL_BACKUP_DIR"/unheard-db-*.db.gz 2>/dev/null | tail -n +4 | xargs -r rm -v

# Cleanup old remote backups
echo ""
echo "Cleaning up old remote backups (keeping last $BACKUP_RETENTION)..."
REMOTE_BACKUPS=$(rclone lsf "$RCLONE_REMOTE:$BACKUP_BUCKET/db/" --files-only 2>/dev/null | sort -r)
BACKUP_COUNT=$(echo "$REMOTE_BACKUPS" | grep -c . || true)

if [[ $BACKUP_COUNT -gt $BACKUP_RETENTION ]]; then
    TO_DELETE=$(echo "$REMOTE_BACKUPS" | tail -n +$((BACKUP_RETENTION + 1)))
    while IFS= read -r file; do
        [[ -z "$file" ]] && continue
        echo "  Deleting: $file"
        rclone delete "$RCLONE_REMOTE:$BACKUP_BUCKET/db/$file"
    done <<< "$TO_DELETE"
else
    echo "  No old backups to delete ($BACKUP_COUNT backups exist)"
fi

echo ""
echo "=== Backup Complete ==="
echo "Local: $LOCAL_BACKUP_PATH"
echo "Remote: $RCLONE_REMOTE:$BACKUP_BUCKET/db/$BACKUP_NAME"
