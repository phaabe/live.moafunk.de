#!/usr/bin/env bash
# Push a synthetic 440 Hz tone into the local NodeMediaServer as the throwaway
# "stream-io-test" key — the same way a real broadcaster's RTMP would arrive,
# but isolated from the real "stream-io" key. Needs ffmpeg on the host.
#
#   ./push-test-tone.sh                # 440 Hz tone
#   ./push-test-tone.sh path/to.mp3    # loop a real file instead (hear music)
#
# Ctrl-C to stop. No host ffmpeg? Run it in a container instead:
#   docker run --rm --network moafunk-stream-test_default jrottenberg/ffmpeg \
#     -re -f lavfi -i sine=frequency=440 -c:a aac -b:a 128k -f flv \
#     rtmp://nms:1935/live/stream-io-test
set -euo pipefail

KEY="stream-io-test"
DEST="rtmp://localhost:1935/live/${KEY}"

if [[ "${1:-}" != "" ]]; then
  echo "Looping ${1} → ${DEST} (Ctrl-C to stop)…"
  exec ffmpeg -hide_banner -re -stream_loop -1 -i "$1" \
    -c:a aac -b:a 192k -ar 44100 -ac 2 -f flv "${DEST}"
else
  echo "Pushing 440 Hz test tone → ${DEST} (Ctrl-C to stop)…"
  exec ffmpeg -hide_banner -re -f lavfi -i "sine=frequency=440:sample_rate=44100" \
    -c:a aac -b:a 128k -ar 44100 -ac 2 -f flv "${DEST}"
fi
