# Local rehearsal producer: pull the live RTMP from NMS, re-emit MP3 to Icecast.
# Mirrors the prod Liquidsoap script in ../phase-2-icecast-runbook.md, but points
# at docker service names (nms, icecast) and the throwaway "stream-io-test" key.

# Source: pull the test stream locally from NodeMediaServer.
src = input.ffmpeg(format="flv", "rtmp://nms:1935/live/stream-io-test")

# Never let the mount 404 if the source is absent — fall back to silence.
radio = mksafe(src)

# Output: MP3 to Icecast /test.mp3 (iOS-safe codec; NEVER Opus/Ogg for the public mount).
output.icecast(
  %mp3(bitrate=128, samplerate=44100, stereo=true),
  host="icecast",
  port=8000,
  password="localsource",
  mount="/test.mp3",
  name="Moafunk (local test)",
  description="Local Phase-2 parallel-run rehearsal",
  genre="radio",
  radio
)
