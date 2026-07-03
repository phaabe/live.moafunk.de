#!/usr/bin/env bash
set -euo pipefail

# Configure CORS for the R2 buckets to allow audio/image playback from the admin panel.
#
# WaveSurfer fetch()es recording audio to draw its waveform, which is CORS-gated.
# BOTH buckets the admin reads from need the policy:
#   - the default recordings bucket (raw/finalized takes)
#   - the shows archive bucket `moafunk-prod` (auto-published streamed-show mp3s;
#     see publish_stream_to_shows_archive / RecordingVersion.archive_key)
# The S3 object token cannot manage CORS (PutBucketCors -> AccessDenied), so this
# runs through wrangler (Cloudflare account auth).
#
# Requirements:
# - wrangler CLI installed and authenticated (`wrangler login`)
#
# Usage:
#   ./backend/scripts/configure_r2_cors.sh
#
# Or with custom buckets/origins:
#   BUCKETS="my-bucket other-bucket" ORIGINS="https://example.com,http://localhost:5173" ./backend/scripts/configure_r2_cors.sh
#   BUCKET=one-bucket ./backend/scripts/configure_r2_cors.sh   # single-bucket override (back-compat)

# Space-separated list of buckets to configure. `BUCKET` (singular) still works
# as a single-bucket override for back-compat.
BUCKETS="${BUCKETS:-${BUCKET:-unheard-artists-dev moafunk-prod}}"
ACCOUNT_ID="${ACCOUNT_ID:-4acacbddb37198e8eed490e4b7c752ee}"

# Default allowed origins (localhost for dev, production domains)
# Includes port 5173 for Vite default dev server
ORIGINS="${ORIGINS:-http://localhost:3000,http://localhost:5173,http://localhost:8000,https://live.moafunk.de,https://admin.live.moafunk.de}"

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

echo "Configuring CORS for R2 buckets: $BUCKETS"
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
trap 'rm -f "$CORS_FILE"' EXIT

# Apply the same rules to every bucket the admin reads from
for bucket in $BUCKETS; do
    echo "Applying CORS rules to '$bucket'..."
    wrangler r2 bucket cors set "$bucket" --file "$CORS_FILE" --force
    echo ""
    echo "Verifying '$bucket'..."
    wrangler r2 bucket cors list "$bucket"
    echo ""
done

echo "✅ CORS configured successfully for: $BUCKETS"
