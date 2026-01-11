#!/bin/bash
# Backup R2 media bucket to backup bucket (incremental sync)
# Usage: ./backup-r2.sh [--full]
#
# Environment variables:
#   R2_BUCKET_NAME   - Source bucket name (default: unheard-artists-prod)
#   BACKUP_BUCKET    - Destination backup bucket (default: unheard-backups)
#   BACKUP_RETENTION - Number of full snapshots to keep (default: 2)
#   RCLONE_REMOTE    - rclone remote for backup bucket (default: r2-backup)
#   RCLONE_SOURCE    - rclone remote for source bucket (default: r2-prod)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

# Load .env if exists
if [[ -f "$BACKEND_DIR/.env" ]]; then
    set -a
    source "$BACKEND_DIR/.env"
    set +a
fi

SOURCE_BUCKET="${R2_BUCKET_NAME:-unheard-artists-prod}"
BACKUP_BUCKET="${BACKUP_BUCKET:-unheard-backups}"
BACKUP_RETENTION="${BACKUP_RETENTION:-2}"
RCLONE_SOURCE="${RCLONE_SOURCE:-r2-prod}"
RCLONE_BACKUP="${RCLONE_REMOTE:-r2-backup}"
FULL_BACKUP="${1:-}"

TIMESTAMP=$(date +%Y-%m-%d)

echo "=== R2 Media Backup ==="
echo "Source: $RCLONE_SOURCE:$SOURCE_BUCKET"
echo "Destination: $RCLONE_BACKUP:$BACKUP_BUCKET"
echo "Date: $TIMESTAMP"
echo ""

if ! command -v rclone &> /dev/null; then
    echo "Error: rclone is not installed"
    echo "Install with: curl https://rclone.org/install.sh | sudo bash"
    exit 1
fi

# Check source bucket is accessible
echo "Checking source bucket..."
if ! rclone lsd "$RCLONE_SOURCE:$SOURCE_BUCKET" &>/dev/null; then
    echo "Error: Cannot access source bucket $RCLONE_SOURCE:$SOURCE_BUCKET"
    echo "Check rclone configuration"
    exit 1
fi

SOURCE_COUNT=$(rclone size "$RCLONE_SOURCE:$SOURCE_BUCKET" --json 2>/dev/null | jq -r '.count // 0')
SOURCE_SIZE=$(rclone size "$RCLONE_SOURCE:$SOURCE_BUCKET" --json 2>/dev/null | jq -r '.bytes // 0' | numfmt --to=iec 2>/dev/null || echo "unknown")
echo "  Source contains $SOURCE_COUNT objects ($SOURCE_SIZE)"

if [[ "$FULL_BACKUP" == "--full" ]]; then
    # Full snapshot backup (dated folder)
    DEST_PATH="$RCLONE_BACKUP:$BACKUP_BUCKET/r2-snapshots/$TIMESTAMP"
    echo ""
    echo "Creating full snapshot: $DEST_PATH"
    
    rclone sync "$RCLONE_SOURCE:$SOURCE_BUCKET" "$DEST_PATH" \
        --progress \
        --stats 30s \
        --transfers 8 \
        --checkers 16
    
    # Cleanup old snapshots
    echo ""
    echo "Cleaning up old snapshots (keeping last $BACKUP_RETENTION)..."
    
    SNAPSHOTS=$(rclone lsf "$RCLONE_BACKUP:$BACKUP_BUCKET/r2-snapshots/" --dirs-only 2>/dev/null | sort -r)
    SNAPSHOT_COUNT=$(echo "$SNAPSHOTS" | grep -c . || true)
    
    if [[ $SNAPSHOT_COUNT -gt $BACKUP_RETENTION ]]; then
        TO_DELETE=$(echo "$SNAPSHOTS" | tail -n +$((BACKUP_RETENTION + 1)))
        while IFS= read -r dir; do
            [[ -z "$dir" ]] && continue
            echo "  Deleting snapshot: $dir"
            rclone purge "$RCLONE_BACKUP:$BACKUP_BUCKET/r2-snapshots/$dir"
        done <<< "$TO_DELETE"
    else
        echo "  No old snapshots to delete ($SNAPSHOT_COUNT exist)"
    fi
else
    # Incremental sync to "latest" folder
    DEST_PATH="$RCLONE_BACKUP:$BACKUP_BUCKET/r2-latest"
    echo ""
    echo "Incremental sync to: $DEST_PATH"
    
    rclone sync "$RCLONE_SOURCE:$SOURCE_BUCKET" "$DEST_PATH" \
        --progress \
        --stats 30s \
        --transfers 8 \
        --checkers 16
fi

echo ""
echo "=== R2 Backup Complete ==="
echo "Destination: $DEST_PATH"

# Show backup size
BACKUP_SIZE=$(rclone size "$DEST_PATH" --json 2>/dev/null | jq -r '.bytes // 0' | numfmt --to=iec 2>/dev/null || echo "unknown")
echo "Backup size: $BACKUP_SIZE"
