#!/bin/bash
# Restore database from backup
# Usage: ./restore-db.sh <backup-name>
#        ./restore-db.sh --list           # List available backups
#        ./restore-db.sh --latest         # Restore most recent backup
#
# Environment variables:
#   DATABASE_PATH - SQLite database path (default: /app/data/unheard.db)
#   BACKUP_BUCKET - R2 backup bucket name (default: unheard-backups)
#   RCLONE_REMOTE - rclone remote name (default: r2-backup)

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
RCLONE_REMOTE="${RCLONE_REMOTE:-r2-backup}"

ACTION="${1:-}"

echo "=== Database Restore ==="
echo ""

if ! command -v rclone &> /dev/null; then
    echo "Error: rclone is not installed"
    exit 1
fi

list_backups() {
    echo "Available backups in $RCLONE_REMOTE:$BACKUP_BUCKET/db/:"
    echo ""
    rclone lsf "$RCLONE_REMOTE:$BACKUP_BUCKET/db/" --files-only | sort -r | head -20
    echo ""
    echo "(Showing most recent 20)"
}

if [[ -z "$ACTION" ]]; then
    echo "Usage: $0 <backup-name>"
    echo "       $0 --list"
    echo "       $0 --latest"
    echo ""
    list_backups
    exit 1
fi

if [[ "$ACTION" == "--list" ]]; then
    list_backups
    exit 0
fi

if [[ "$ACTION" == "--latest" ]]; then
    BACKUP_NAME=$(rclone lsf "$RCLONE_REMOTE:$BACKUP_BUCKET/db/" --files-only | sort -r | head -1)
    if [[ -z "$BACKUP_NAME" ]]; then
        echo "Error: No backups found"
        exit 1
    fi
    echo "Latest backup: $BACKUP_NAME"
else
    BACKUP_NAME="$ACTION"
fi

echo "Restoring: $BACKUP_NAME"
echo "Target: $DB_PATH"
echo ""

# Confirm with user
read -p "⚠ This will REPLACE the current database. Continue? (yes/no): " CONFIRM
if [[ "$CONFIRM" != "yes" ]]; then
    echo "Aborted"
    exit 1
fi

# Create temp directory
TEMP_DIR="/tmp/db-restore-$$"
mkdir -p "$TEMP_DIR"
trap "rm -rf $TEMP_DIR" EXIT

# Download backup
echo ""
echo "Downloading backup..."
rclone copy "$RCLONE_REMOTE:$BACKUP_BUCKET/db/$BACKUP_NAME" "$TEMP_DIR/" --progress

# Decompress if gzipped
DOWNLOADED_FILE="$TEMP_DIR/$BACKUP_NAME"
if [[ "$BACKUP_NAME" == *.gz ]]; then
    echo "Decompressing..."
    gunzip "$DOWNLOADED_FILE"
    DOWNLOADED_FILE="${DOWNLOADED_FILE%.gz}"
fi

# Verify it's a valid SQLite database
echo "Verifying backup integrity..."
if ! sqlite3 "$DOWNLOADED_FILE" "PRAGMA integrity_check;" | grep -q "ok"; then
    echo "Error: Backup file is not a valid SQLite database"
    exit 1
fi

# Create backup of current database
if [[ -f "$DB_PATH" ]]; then
    CURRENT_BACKUP="${DB_PATH}.pre-restore-$(date +%Y%m%d_%H%M%S)"
    echo "Backing up current database to: $CURRENT_BACKUP"
    cp "$DB_PATH" "$CURRENT_BACKUP"
fi

# Stop the service if running (optional - warn user)
echo ""
echo "⚠ Make sure the backend service is STOPPED before proceeding!"
read -p "Is the service stopped? (yes/no): " CONFIRM
if [[ "$CONFIRM" != "yes" ]]; then
    echo "Please stop the service first: docker compose down"
    exit 1
fi

# Restore
echo ""
echo "Restoring database..."
cp "$DOWNLOADED_FILE" "$DB_PATH"

echo ""
echo "=== Restore Complete ==="
echo "Restored: $BACKUP_NAME -> $DB_PATH"
echo ""
echo "Remember to restart the backend service: docker compose up -d"
