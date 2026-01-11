#!/bin/bash
# Check synchronization between database and R2 storage
# Reports orphaned R2 objects (not in DB) and missing R2 objects (in DB but not in R2)
#
# Usage: ./sync-check.sh [--fix-orphans]
#        ./sync-check.sh --env /path/to/.env [--fix-orphans]
#
# Environment variables:
#   R2_BUCKET_NAME - Bucket name (default: from .env or 'unheard-artists-prod')
#   DATABASE_PATH  - SQLite database path (default: ../data/unheard.db)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

# Parse --env flag
ENV_FILE="$BACKEND_DIR/.env"
if [[ "${1:-}" == "--env" || "${1:-}" == "-e" ]]; then
    ENV_FILE="${2:-}"
    shift 2
fi

# Load .env if exists
if [[ -f "$ENV_FILE" ]]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

BUCKET="${R2_BUCKET_NAME:-unheard-artists-prod}"
DB_PATH="${DATABASE_PATH:-$BACKEND_DIR/data/unheard.db}"
FIX_ORPHANS="${1:-}"
R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
RCLONE_REMOTE="r2-prod"

# Export R2 credentials for AWS CLI
export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
export AWS_DEFAULT_REGION="auto"

TEMP_DIR="/tmp/r2-sync-check-$$"
mkdir -p "$TEMP_DIR"

cleanup() {
    rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

# Check for available tool
USE_RCLONE=false
USE_AWS=false
if command -v rclone &> /dev/null && [[ -f "$HOME/.config/rclone/rclone.conf" ]]; then
    USE_RCLONE=true
elif command -v aws &> /dev/null; then
    USE_AWS=true
fi

echo "=== R2 <-> Database Sync Check ==="
echo "Bucket: $BUCKET"
echo "Database: $DB_PATH"
echo ""

if [[ ! -f "$DB_PATH" ]]; then
    echo "Error: Database not found at $DB_PATH"
    exit 1
fi

# Get all keys from database
echo "Fetching keys from database..."
DB_KEYS_FILE="$TEMP_DIR/db_keys.txt"

sqlite3 "$DB_PATH" <<EOF | sort -u > "$DB_KEYS_FILE"
SELECT pic_key FROM artists WHERE pic_key IS NOT NULL AND pic_key != '';
SELECT pic_cropped_key FROM artists WHERE pic_cropped_key IS NOT NULL AND pic_cropped_key != '';
SELECT pic_overlay_key FROM artists WHERE pic_overlay_key IS NOT NULL AND pic_overlay_key != '';
SELECT voice_message_key FROM artists WHERE voice_message_key IS NOT NULL AND voice_message_key != '';
SELECT track1_key FROM artists WHERE track1_key IS NOT NULL AND track1_key != '';
SELECT track2_key FROM artists WHERE track2_key IS NOT NULL AND track2_key != '';
SELECT track1_original_key FROM artists WHERE track1_original_key IS NOT NULL AND track1_original_key != '';
SELECT track2_original_key FROM artists WHERE track2_original_key IS NOT NULL AND track2_original_key != '';
SELECT voice_original_key FROM artists WHERE voice_original_key IS NOT NULL AND voice_original_key != '';
SELECT recording_key FROM shows WHERE recording_key IS NOT NULL AND recording_key != '';
SELECT pic_key FROM pending_submissions WHERE pic_key IS NOT NULL AND pic_key != '';
SELECT pic_cropped_key FROM pending_submissions WHERE pic_cropped_key IS NOT NULL AND pic_cropped_key != '';
SELECT pic_overlay_key FROM pending_submissions WHERE pic_overlay_key IS NOT NULL AND pic_overlay_key != '';
SELECT track1_key FROM pending_submissions WHERE track1_key IS NOT NULL AND track1_key != '';
SELECT track2_key FROM pending_submissions WHERE track2_key IS NOT NULL AND track2_key != '';
SELECT voice_key FROM pending_submissions WHERE voice_key IS NOT NULL AND voice_key != '';
SELECT track1_original_key FROM pending_submissions WHERE track1_original_key IS NOT NULL AND track1_original_key != '';
SELECT track2_original_key FROM pending_submissions WHERE track2_original_key IS NOT NULL AND track2_original_key != '';
SELECT voice_original_key FROM pending_submissions WHERE voice_original_key IS NOT NULL AND voice_original_key != '';
EOF

DB_COUNT=$(wc -l < "$DB_KEYS_FILE" | tr -d ' ')
echo "  Found $DB_COUNT keys in database"

# Get all keys from R2
echo "Fetching keys from R2..."
R2_KEYS_FILE="$TEMP_DIR/r2_keys.txt"

if $USE_RCLONE; then
    rclone lsf "$RCLONE_REMOTE:$BUCKET" --files-only -R 2>/dev/null | sort -u > "$R2_KEYS_FILE"
elif $USE_AWS; then
    aws s3api list-objects-v2 --endpoint-url "$R2_ENDPOINT" --bucket "$BUCKET" --query 'Contents[].Key' --output text 2>/dev/null | tr '\t' '\n' | sort -u > "$R2_KEYS_FILE"
else
    echo "Error: Neither rclone nor aws CLI is available"
    echo "Install rclone: curl https://rclone.org/install.sh | sudo bash"
    exit 1
fi

R2_COUNT=$(wc -l < "$R2_KEYS_FILE" | tr -d ' ')
echo "  Found $R2_COUNT objects in R2"
echo ""

# Find orphans (in R2 but not in DB)
echo "=== Orphaned Objects (in R2, not in DB) ==="
ORPHANS_FILE="$TEMP_DIR/orphans.txt"
comm -23 "$R2_KEYS_FILE" "$DB_KEYS_FILE" > "$ORPHANS_FILE"
ORPHAN_COUNT=$(wc -l < "$ORPHANS_FILE" | tr -d ' ')

if [[ $ORPHAN_COUNT -eq 0 ]]; then
    echo "  None found ✓"
else
    echo "  Found $ORPHAN_COUNT orphaned objects:"
    head -20 "$ORPHANS_FILE" | sed 's/^/    /'
    if [[ $ORPHAN_COUNT -gt 20 ]]; then
        echo "    ... and $((ORPHAN_COUNT - 20)) more"
    fi
    
    if [[ "$FIX_ORPHANS" == "--fix-orphans" ]]; then
        echo ""
        echo "  Deleting orphaned objects..."
        DELETED=0
        while IFS= read -r key; do
            local deleted=false
            if $USE_RCLONE; then
                rclone delete "$RCLONE_REMOTE:$BUCKET/$key" 2>/dev/null && deleted=true
            elif $USE_AWS; then
                aws s3 rm "s3://$BUCKET/$key" --endpoint-url "$R2_ENDPOINT" 2>/dev/null && deleted=true
            fi
            
            if $deleted; then
                echo "    ✓ Deleted: $key"
                ((DELETED++))
            else
                echo "    ✗ Failed: $key"
            fi
        done < "$ORPHANS_FILE"
        echo "  Deleted $DELETED orphaned objects"
    fi
fi
echo ""

# Find missing (in DB but not in R2)
echo "=== Missing Objects (in DB, not in R2) ==="
MISSING_FILE="$TEMP_DIR/missing.txt"
comm -13 "$R2_KEYS_FILE" "$DB_KEYS_FILE" > "$MISSING_FILE"
MISSING_COUNT=$(wc -l < "$MISSING_FILE" | tr -d ' ')

if [[ $MISSING_COUNT -eq 0 ]]; then
    echo "  None found ✓"
else
    echo "  ⚠ Found $MISSING_COUNT missing objects (data integrity issue!):"
    head -20 "$MISSING_FILE" | sed 's/^/    /'
    if [[ $MISSING_COUNT -gt 20 ]]; then
        echo "    ... and $((MISSING_COUNT - 20)) more"
    fi
fi
echo ""

# Summary
echo "=== Summary ==="
echo "  Database keys: $DB_COUNT"
echo "  R2 objects: $R2_COUNT"
echo "  Orphaned (R2 only): $ORPHAN_COUNT"
echo "  Missing (DB only): $MISSING_COUNT"

if [[ $MISSING_COUNT -gt 0 ]]; then
    echo ""
    echo "⚠ WARNING: Missing objects indicate data integrity issues!"
    echo "  These files are referenced in the database but do not exist in R2."
    echo "  Check if they were accidentally deleted or never uploaded."
    exit 1
fi

if [[ $ORPHAN_COUNT -gt 0 && "$FIX_ORPHANS" != "--fix-orphans" ]]; then
    echo ""
    echo "Tip: Run with --fix-orphans to delete orphaned R2 objects"
fi
