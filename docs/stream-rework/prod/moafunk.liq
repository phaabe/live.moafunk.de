# Production Liquidsoap producer — issue #194 decision (B), no NMS.
#
# The backend ffmpeg (STREAM_OUTPUT=icecast) copies the broadcaster's Opus
# (no re-encode, `-c:a copy` → Ogg) into Liquidsoap's HARBOR (Icecast SOURCE
# protocol); Liquidsoap performs the single lossy MP3 encode to Icecast-KH. The
# harbor hop is localhost, so the internal leg is lossless. Two independent
# chains give the #173 "separate /test and /live mounts with separate creds":
#
#   backend (ICECAST_URL=…/test) ─▶ harbor "test" :8005 ─▶ mksafe ─▶ Icecast /test.mp3   (preview)
#   backend (ICECAST_URL=…/live) ─▶ harbor "live" :8005 ─▶ mksafe ─▶ Icecast /live.mp3   (public)
#
# "Go live" is the broadcaster switching the producer's target mount test→live
# (an ICECAST_URL change + stream restart). The recording tee is a separate
# ffmpeg, so it is never disturbed by the switch.
#
# Validated chain shape: see ../local-test-harness/ (harbor rehearsal). Pin
# Liquidsoap 2.4.4 in prod (skip 2.4.3 — shared-encoder crash); the
# harbor/mksafe/%mp3 syntax here is stable across 2.x.
#
# Secrets come from the environment (see stream.env.example) — never hard-code.

log.level := 3

# --- credentials (injected via the systemd EnvironmentFile) -------------------
harbor_test_pw = environment.get("HARBOR_TEST_PASSWORD")
harbor_live_pw = environment.get("HARBOR_LIVE_PASSWORD")
ice_source_pw  = environment.get("ICECAST_SOURCE_PASSWORD")
harbor_port    = int_of_string(default=8005, environment.get("HARBOR_PORT"))
ice_host       = "127.0.0.1"
ice_port       = int_of_string(default=8010, environment.get("ICECAST_PORT"))

# MP3 only on the public mounts — iOS Safari can't decode Opus/Ogg (would
# silently lock out every iPhone). This is now the ONLY lossy encode in the
# chain (the backend copies Opus through the harbor untouched), so we run a high
# bitrate here without paying for a second transcode. Source is Opus ~192 kbps;
# 256 kbps MP3 preserves it transparently. ~2x listener egress vs the old 128k.
enc = %mp3(bitrate=256, samplerate=44100, stereo=true)

# --- public chain: harbor "live" → Icecast /live.mp3 --------------------------
# mksafe serves silence (never 404) until the broadcaster connects.
# NOTE: do NOT add blank.strip on a source wrapped in mksafe (CPU-spike breakups,
# liquidsoap #3439/#3474). Dead-air auto-fill would be a fallback to a `single`
# track, not blank.strip+mksafe — left out until a fallback asset exists.
live_in = input.harbor("live", port=harbor_port, password=harbor_live_pw)
output.icecast(
  enc,
  host=ice_host, port=ice_port, password=ice_source_pw,
  mount="/live.mp3",
  name="Moafunk",
  description="Moafunk live radio",
  genre="radio",
  public=true,
  mksafe(live_in)
)

# --- preview chain: harbor "test" → Icecast /test.mp3 -------------------------
# Started only after the live input has its harbor mount. Each chain runs in
# its own clock thread, and Liquidsoap's harbor opens a port without a lock
# (harbor.ml, get_handler). Two inputs that start at the same moment can both
# open port 8005: one fails with EADDRINUSE, and its mount (/test or /live) is
# missing until the next restart. Seen on staging 2026-11-19.
preview_started = ref(false)
def start_preview() =
  if not preview_started() then
    preview_started := true
    test_src = mksafe(input.harbor("test", port=harbor_port, password=harbor_test_pw))
    ignore(output.icecast(
      enc,
      host=ice_host, port=ice_port, password=ice_source_pw,
      mount="/test.mp3",
      name="Moafunk (test)",
      description="Broadcaster preview mount",
      genre="radio",
      public=false,
      test_src
    ))
  end
end
live_in.on_wake_up(synchronous=false, start_preview)
