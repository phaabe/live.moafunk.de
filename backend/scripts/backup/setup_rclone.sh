#!/bin/bash
# Setup rclone on Lightsail for R2 backups
# Run this on the Lightsail instance after initial provisioning
#
# Usage: ./setup_rclone.sh
#
# Required environment variables (from .env or passed directly):
#   R2_ACCOUNT_ID        - Cloudflare account ID
#   R2_ACCESS_KEY_ID     - R2 access key
#   R2_SECRET_ACCESS_KEY - R2 secret key
#   R2_BUCKET_NAME       - Production bucket name (default: unheard-artists-prod)
#   BACKUP_BUCKET        - Backup bucket name (default: unheard-backups)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

# Load .env if exists
if [[ -f "$BACKEND_DIR/.env" ]]; then
    set -a
    source "$BACKEND_DIR/.env"
    set +a
fi

# Required variables
R2_ACCOUNT_ID="${R2_ACCOUNT_ID:-}"
R2_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID:-}"
R2_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY:-}"
R2_BUCKET_NAME="${R2_BUCKET_NAME:-unheard-artists-prod}"
BACKUP_BUCKET="${BACKUP_BUCKET:-unheard-backups}"

if [[ -z "$R2_ACCOUNT_ID" || -z "$R2_ACCESS_KEY_ID" || -z "$R2_SECRET_ACCESS_KEY" ]]; then
    echo "Error: R2 credentials not set"
    echo "Required: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY"
    echo ""
    echo "These can be set in .env or passed as environment variables"
    exit 1
fi

R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

echo "=== rclone Setup for R2 Backups ==="
echo ""
echo "R2 Account: $R2_ACCOUNT_ID"
echo "R2 Endpoint: $R2_ENDPOINT"
echo "Production bucket: $R2_BUCKET_NAME"
echo "Backup bucket: $BACKUP_BUCKET"
echo ""

# Install rclone if not present
if ! command -v rclone &> /dev/null; then
    echo "Installing rclone..."
    curl https://rclone.org/install.sh | sudo bash
    echo ""
fi

echo "rclone version: $(rclone version --check | head -1)"
echo ""

# Create rclone config directory
RCLONE_CONFIG_DIR="$HOME/.config/rclone"
mkdir -p "$RCLONE_CONFIG_DIR"

RCLONE_CONFIG="$RCLONE_CONFIG_DIR/rclone.conf"

echo "Creating rclone configuration at $RCLONE_CONFIG..."

# Generate rclone config with two remotes:
# - r2-prod: For accessing the production bucket
# - r2-backup: For accessing the backup bucket (could be same credentials, different bucket)
cat > "$RCLONE_CONFIG" << EOF
# R2 Production bucket remote
[r2-prod]
type = s3
provider = Cloudflare
access_key_id = ${R2_ACCESS_KEY_ID}
secret_access_key = ${R2_SECRET_ACCESS_KEY}
endpoint = ${R2_ENDPOINT}
acl = private

# R2 Backup bucket remote (uses same credentials)
[r2-backup]
type = s3
provider = Cloudflare
access_key_id = ${R2_ACCESS_KEY_ID}
secret_access_key = ${R2_SECRET_ACCESS_KEY}
endpoint = ${R2_ENDPOINT}
acl = private
EOF

chmod 600 "$RCLONE_CONFIG"
echo "  Created $RCLONE_CONFIG"

# Test connectivity
echo ""
echo "Testing R2 connectivity..."

echo -n "  r2-prod:$R2_BUCKET_NAME ... "
if rclone lsd "r2-prod:$R2_BUCKET_NAME" &>/dev/null; then
    echo "✓ OK"
else
    echo "✗ FAILED (bucket may not exist yet)"
fi

echo -n "  r2-backup:$BACKUP_BUCKET ... "
if rclone lsd "r2-backup:$BACKUP_BUCKET" &>/dev/null; then
    echo "✓ OK"
else
    echo "✗ FAILED (bucket may not exist yet)"
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Configured remotes:"
echo "  r2-prod   - Production R2 bucket ($R2_BUCKET_NAME)"
echo "  r2-backup - Backup R2 bucket ($BACKUP_BUCKET)"
echo ""
echo "Usage examples:"
echo "  rclone ls r2-prod:$R2_BUCKET_NAME"
echo "  rclone ls r2-backup:$BACKUP_BUCKET"
echo "  rclone sync r2-prod:$R2_BUCKET_NAME r2-backup:$BACKUP_BUCKET/r2-latest"
echo ""
echo "Run backups with:"
echo "  ./scripts/backup/backup-db.sh"
echo "  ./scripts/backup/backup-r2.sh"
echo "  ./scripts/backup/backup-all.sh"
