# Fallback: go live with OBS or BUTT

Use this when the admin web app can't stream (browser, mic or WebSocket
problems). You send audio from a desktop encoder straight into the Liquidsoap
harbor on the Hetzner box. The harbor is the same entry point the backend uses.

```
OBS / BUTT ──SSH tunnel──▶ harbor "live" :8005 ─▶ mksafe ─▶ Icecast /live.mp3
```

## What you lose

- **The website shows "Off air".** The public player only plays when the
  backend says it is streaming (`/api/stream/status`), and the backend can't see
  this path. Share the direct link instead:
  https://admin.live.moafunk.de/live.mp3
- **No recording, no SoundCloud upload, no Telegram post.** These all run in the
  backend. Record locally and upload the show by hand afterwards.
- **No help if the box itself is down.**

## Before you start

- SSH access to the box (`~/.ssh/id_ed25519-hetzner`).
- `HARBOR_LIVE_PASSWORD` and `HARBOR_TEST_PASSWORD` from Bitwarden Secrets
  Manager.
- The web app stream is stopped. The harbor takes only one sender per input.

## 1. Open the tunnel

Port 8005 is not open to the internet, and the harbor login is plain text. Keep
this running for the whole show:

```bash
ssh -N -L 8005:127.0.0.1:8005 -i ~/.ssh/id_ed25519-hetzner root@178.104.160.103
```

## 2a. BUTT (recommended)

BUTT is made for Icecast. It reconnects by itself and can record while it
streams.

| Setting | Value |
| --- | --- |
| Server type | Icecast |
| Address / port | `127.0.0.1` / `8005` |
| Mountpoint | `live` (rehearsal: `test`) |
| User | `source` |
| Password | `HARBOR_LIVE_PASSWORD` (rehearsal: `HARBOR_TEST_PASSWORD`) |
| TLS | off (the tunnel already encrypts) |
| Codec | MP3, 320 kbps |

Turn on local recording in BUTT's record settings.

## 2b. OBS

OBS has no Icecast output. Use its FFmpeg recording output instead.
Settings → Output → Output Mode **Advanced** → **Recording** tab:

| Setting | Value |
| --- | --- |
| Type | Custom Output (FFmpeg) |
| FFmpeg Output Type | Output to URL |
| URL | `icecast://source:<HARBOR_LIVE_PASSWORD>@127.0.0.1:8005/live` |
| Container format | `mp3` |
| Audio encoder | `libmp3lame`, 320 kbps |

- Use MP3. FFmpeg labels the stream `audio/mpeg`, and Liquidsoap picks its
  decoder from that label, so Ogg/Opus would fail.
- **Start Recording** = on air. **Stop Recording** = off air.
- OBS does not reconnect. If the stream stops, press Start Recording again.
- This output is now busy, so OBS can't also record to a file. Record
  somewhere else.
- OBS saves the password in plain text in its profile.

## 3. Rehearse on /test

Send to the `test` mount with the test password. Listen at
https://admin.live.moafunk.de/test.mp3. Expect about 12 s of delay.

## 4. Go live

Switch the mount to `live` and use the live password. Check
https://admin.live.moafunk.de/live.mp3 and post the link on Telegram and
Instagram.

## 5. After the show

Stop the encoder. Liquidsoap plays silence until the next sender connects, and
listeners stay connected. Close the tunnel. Upload the local recording by hand.
