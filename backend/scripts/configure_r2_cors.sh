#!/usr/bin/env bash
set -euo pipefail

# Configure CORS for the R2 bucket to allow audio/image playback from admin panel.
#
# Requirements:
# - wrangler CLI installed and authenticated
#
# Usage:
#   ./backend/scripts/configure_r2_cors.sh
#
# Or with custom bucket/origins:
#   BUCKET=my-bucket ORIGINS="https://example.com,http://localhost:5173" ./backend/scripts/configure_r2_cors.sh

BUCKET="${BUCKET:-unheard-artists-media}"
ACCOUNT_ID="${ACCOUNT_ID:-4acacbddb37198e8eed490e4b7c752ee}"

# Default allowed origins (localhost for dev, production domain)
ORIGINS="${ORIGINS:-http://localhost:3000,http://localhost:8000,https://live.moafunk.de}"

# Check wrangler is installed
if ! command -v wrangler &> /dev/null; then
    echo "Error: wrangler is not installed."
    echo "Install with: npm install -g wrangler"
    exit 1
fi

# Check wrangler is authenticated
if ! wrangler whoami &> /dev/null; then
    echo "Error: wrangler is not authenticated."
    echo "Run: wrangler login"
    exit 1
fi

echo "Configuring CORS for R2 bucket: $BUCKET"
echo "Account ID: $ACCOUNT_ID"
echo "Allowed origins: $ORIGINS"
echo ""

# Convert comma-separated origins to JSON array
IFS=',' read -ra ORIGIN_ARRAY <<< "$ORIGINS"
ORIGINS_JSON=$(printf '%s\n' "${ORIGIN_ARRAY[@]}" | jq -R . | jq -sc .)

# Create CORS rules JSON (wrangler format with nested "allowed" object)
# Keys: allowed.origins, allowed.methods, allowed.headers, exposeHeaders (camelCase), maxAgeSeconds (camelCase)
CORS_RULES=$(cat <<EOF
{
  "rules": [
    {
      "allowed": {
        "origins": $ORIGINS_JSON,
        "methods": ["GET", "HEAD"],
        "headers": ["*"]
      },
      "exposeHeaders": ["Content-Length", "Content-Type", "Content-Range", "Accept-Ranges"],
      "maxAgeSeconds": 86400
    }
  ]
}
EOF
)

echo "CORS Rules:"
echo "$CORS_RULES" | jq .
echo ""

# Create temporary file for CORS rules
CORS_FILE=$(mktemp)
echo "$CORS_RULES" > "$CORS_FILE"

# Apply CORS rules using file
echo "Applying CORS rules..."
wrangler r2 bucket cors set "$BUCKET" --file "$CORS_FILE" --force

# Cleanup temp file
rm -f "$CORS_FILE"

echo ""
echo "✅ CORS configured successfully!"
echo ""
echo "Verifying configuration..."
wrangler r2 bucket cors list "$BUCKET"
