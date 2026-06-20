# Greenfield (no-NMS) rehearsal — issue #194 decision (B).
#
# The backend ffmpeg copies the broadcaster's Opus (no re-encode) straight into
# Liquidsoap's HARBOR (which speaks the Icecast SOURCE protocol); Liquidsoap
# performs the single lossy MP3 encode to Icecast. This is the path that
# REPLACES NodeMediaServer: no RTMP and no NMS anywhere in the chain.
#
#   backend ffmpeg ──icecast://…@liquidsoap:8005/live (Ogg/Opus)──▶ input.harbor ─▶ mksafe ─▶ Icecast /live.mp3
#
# Compare with moafunk.liq (the NMS parallel-run rehearsal), which instead PULLS
# RTMP from NodeMediaServer via input.ffmpeg.

# Harbor input: receive the producer's source push on port 8005, mount "live".
# Same wire protocol as an Icecast source, so the backend's
# `-c:a copy -f ogg icecast://source:pw@host:port/mount` output targets it
# unchanged. Liquidsoap sniffs the Ogg/Opus container automatically.
src = input.harbor("live", port=8005, password="harborpw")

# Never let the public mount 404 if the producer drops — fall back to silence.
radio = mksafe(src)

# Output: MP3 to Icecast /live.mp3 (iOS-safe codec; NEVER Opus/Ogg for the
# public mount). The ONLY lossy encode in the chain — runs at 256 kbps to match
# prod (the internal harbor leg is lossless Opus passthrough).
output.icecast(
  %mp3(bitrate=256, samplerate=44100, stereo=true),
  host="icecast",
  port=8000,
  password="localsource",
  mount="/live.mp3",
  name="Moafunk (no-NMS rehearsal)",
  description="Greenfield backend->harbor->Icecast chain (#194 decision B)",
  genre="radio",
  radio
)
