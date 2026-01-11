#!/bin/bash
# Restore R2 media from backup
# Usage: ./restore-r2.sh [--list]           # List available snapshots
#        ./restore-r2.sh <snapshot-date>    # Restore from dated snapshot
#        ./restore-r2.sh --latest           # Restore from r2-latest
#
# Environment variables:
#   R2_BUCKET_NAME - Target bucket name (default: unheard-artists-prod)
#   BACKUP_BUCKET  - Source backup bucket (default: unheard-backups)
#   RCLONE_SOURCE  - rclone remote for target bucket (default: r2-prod)
#   RCLONE_REMOTE  - rclone remote for backup bucket (default: r2-backup)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

# Load .env if exists
if [[ -f "$BACKEND_DIR/.env" ]]; then
    set -a
    source "$BACKEND_DIR/.env"
    set +a
fi

TARGET_BUCKET="${R2_BUCKET_NAME:-unheard-artists-prod}"
BACKUP_BUCKET="${BACKUP_BUCKET:-unheard-backups}"
RCLONE_TARGET="${RCLONE_SOURCE:-r2-prod}"
RCLONE_BACKUP="${RCLONE_REMOTE:-r2-backup}"

ACTION="${1:-}"

echo "=== R2 Media Restore ==="
echo ""

if ! command -v rclone &> /dev/null; then
    echo "Error: rclone is not installed"
    exit 1
fi

list_snapshots() {
    echo "Available snapshots in $RCLONE_BACKUP:$BACKUP_BUCKET/r2-snapshots/:"
    echo ""
    rclone lsf "$RCLONE_BACKUP:$BACKUP_BUCKET/r2-snapshots/" --dirs-only | sort -r
    echo ""
    echo "Also available: r2-latest (incremental sync backup)"
}

if [[ -z "$ACTION" ]]; then
    echo "Usage: $0 <snapshot-date>  (e.g., 2025-01-11)"
    echo "       $0 --latest"
    echo "       $0 --list"
    echo ""
    list_snapshots
    exit 1
fi

if [[ "$ACTION" == "--list" ]]; then
    list_snapshots
    exit 0
fi

if [[ "$ACTION" == "--latest" ]]; then
    SOURCE_PATH="$RCLONE_BACKUP:$BACKUP_BUCKET/r2-latest"
    echo "Restoring from: r2-latest"
else
    SOURCE_PATH="$RCLONE_BACKUP:$BACKUP_BUCKET/r2-snapshots/$ACTION"
    echo "Restoring from: snapshot $ACTION"
fi

echo "Target: $RCLONE_TARGET:$TARGET_BUCKET"
echo ""

# Check source exists
if ! rclone lsd "$SOURCE_PATH" &>/dev/null && ! rclone lsf "$SOURCE_PATH" --files-only &>/dev/null; then
    echo "Error: Backup source not found: $SOURCE_PATH"
    echo ""
    list_snapshots
    exit 1
fi

# Show sizes
SOURCE_SIZE=$(rclone size "$SOURCE_PATH" --json 2>/dev/null | jq -r '.bytes // 0' | numfmt --to=iec 2>/dev/null || echo "unknown")
SOURCE_COUNT=$(rclone size "$SOURCE_PATH" --json 2>/dev/null | jq -r '.count // 0')
echo "Backup contains $SOURCE_COUNT objects ($SOURCE_SIZE)"

TARGET_SIZE=$(rclone size "$RCLONE_TARGET:$TARGET_BUCKET" --json 2>/dev/null | jq -r '.bytes // 0' | numfmt --to=iec 2>/dev/null || echo "unknown")
TARGET_COUNT=$(rclone size "$RCLONE_TARGET:$TARGET_BUCKET" --json 2>/dev/null | jq -r '.count // 0')
echo "Target contains $TARGET_COUNT objects ($TARGET_SIZE)"
echo ""

# Confirm
echo "⚠ This will SYNC the backup to the target bucket."
echo "  Files in target not in backup will be DELETED!"
read -p "Continue? (yes/no): " CONFIRM
if [[ "$CONFIRM" != "yes" ]]; then
    echo "Aborted"
    exit 1
fi

# Perform restore
echo ""
echo "Restoring R2 media..."
rclone sync "$SOURCE_PATH" "$RCLONE_TARGET:$TARGET_BUCKET" \
    --progress \
    --stats 30s \
    --transfers 8 \
    --checkers 16

echo ""
echo "=== Restore Complete ==="
echo "Restored: $SOURCE_PATH -> $RCLONE_TARGET:$TARGET_BUCKET"
