# Phase 2 — Icecast parallel-run runbook (#173–#175)

> Milestone #12 ("Stream rework: recording hardening + Icecast migration"), parent #164.
> Phase 0 (#168) and Phase 1 (recording hardening) are **done**. This is the Phase-2
> setup: stand up **Icecast-KH + Liquidsoap** on the relay box serving a **parallel
> `/test` mount**, with **zero disruption** to the live FLV/HLS listeners.
>
> Everything here runs **on the Hetzner relay box** (where NodeMediaServer lives) plus
> one client-side iOS check. The relay is **not** in this repo. Nothing in the backend
> (Lightsail) or frontend changes for the parallel run.

## Phase-0 facts this builds on

- **NodeMediaServer = 2.4.9** (HLS+FLV). Leave it **completely untouched**. Do **not** `npm install`/upgrade it (v4 is FLV-only → breaks `index.m3u8`).
- Relay host: 1× Xeon vCPU, **2 GB RAM**, kernel 5.4, **960-day uptime** (unpatched). Treat it as fragile — additive services only, no reboots without a plan.
- **Peak ≈ 300 listeners.** Do NOT load-test 300 against `/test` during a live show on this box. 300 × 128 kbps ≈ **38 Mbps** sustained — that's the Phase-4 bandwidth/SPOF problem, not a Phase-2 one.
- **iOS listeners are confirmed and required.** ⇒ **The public Icecast mount MUST be MP3 or AAC, NEVER Ogg/Opus** (iOS Safari can't decode Opus/Ogg → silent lockout of every iPhone/iPad). Use `%mp3` (universal) for the test; `%fdkaac` is an option later. An Opus mount may only ever be an *additional* mount.

## Current audio flow (unchanged by this phase)

```
Broadcaster browser ──WebSocket /ws/stream──▶ Backend (Lightsail, ffmpeg WebM/Opus→AAC)
                                                   │
                                                   ▼ RTMP push
                                   rtmp://stream.moafunk.de/live/stream-io
                                                   │
                                          NodeMediaServer 2.4.9 (Hetzner)
                                                   ├──▶ HLS  /live/stream-io/index.m3u8   (iOS listeners)
                                                   └──▶ FLV  /live/stream-io.flv          (desktop listeners)
```

The backend RTMP target is `config.rtmp_destination()` = `rtmp_url`/`rtmp_stream_key`
(defaults `rtmp://stream.moafunk.de/live` + `stream-io`). We do not touch it.

## Target after this phase (additive)

```
NodeMediaServer 2.4.9 ──(local pull rtmp://127.0.0.1:1935/live/stream-io)──▶ Liquidsoap
                                                                                  │ %mp3 128k
                                                                                  ▼
                                                            Icecast-KH :8010  /test.mp3   ◀── NEW, parallel
   (existing HLS/FLV listeners keep working, untouched)
```

Listeners on HLS/FLV are unaffected. The new `/test.mp3` mount is reachable only by us until Phase 3 cutover.

---

## Step 0 — Pre-flight (read-only)

```bash
ssh <relay-host>
# Confirm NMS is on RTMP 1935 and its HTTP port (HLS/admin). NMS 2.x default HTTP = 8000.
sudo ss -ltnp | grep -E ':1935|:8000|:8010|:8443' || true
# ^ note what's already bound. Icecast must NOT collide with NMS's 8000.
free -m            # headroom check (2 GB box)
df -h /            # disk for logs
nproc              # 1 vCPU
```

**Port plan:** NMS keeps **1935** (RTMP) + **8000** (its HTTP/HLS). Icecast takes **8010** (plain HTTP, behind a TLS proxy later). If 8010 is taken, pick another free high port and use it consistently below.

---

## Step 1 — Smoke-test the Icecast mount with plain ffmpeg (before Liquidsoap)

Prove the mount + iOS playback with the simplest possible producer first; add Liquidsoap (#174) only once this works.

### 1a. Install Icecast-KH

Icecast-KH (karlheyes fork) is not in apt. Two paths:

- **Quickest for the parallel test** — stock `icecast2` from apt is protocol-compatible and fine to validate the mount + iOS playback. Swap to KH before Phase-3 cutover (KH = better stats, listener limits, relays for Phase 4).
  ```bash
  sudo apt-get update && sudo apt-get install -y icecast2
  # When the debconf wizard appears, set source/relay/admin passwords (note them).
  ```
- **Icecast-KH proper** (recommended target): build from source.
  ```bash
  sudo apt-get install -y build-essential libxml2-dev libxslt1-dev libvorbis-dev \
       libssl-dev libcurl4-openssl-dev libogg-dev libtheora-dev libspeex-dev
  cd /usr/local/src && sudo git clone https://github.com/karlheyes/icecast-kh.git
  cd icecast-kh && sudo ./autogen.sh && sudo ./configure && sudo make && sudo make install
  ```

### 1b. Minimal `icecast.xml`

Place at `/etc/icecast2/icecast.xml` (apt) or `/usr/local/etc/icecast.xml` (KH source). Replace the `<*-password>` values.

```xml
<icecast>
  <location>Berlin</location>
  <admin>admin@moafunk.de</admin>
  <limits>
    <clients>350</clients>          <!-- headroom over the ~300 peak; not a load test -->
    <sources>4</sources>
    <queue-size>524288</queue-size>
    <burst-on-connect>1</burst-on-connect>
    <burst-size>65536</burst-size>  <!-- fast start; keep modest on a 2 GB box -->
  </limits>
  <authentication>
    <source-password>CHANGE_ME_SOURCE</source-password>
    <relay-password>CHANGE_ME_RELAY</relay-password>
    <admin-user>admin</admin-user>
    <admin-password>CHANGE_ME_ADMIN</admin-password>
  </authentication>
  <listen-socket>
    <port>8010</port>              <!-- NOT 8000 (NMS uses it) -->
  </listen-socket>
  <mount type="normal">
    <mount-name>/test.mp3</mount-name>
    <max-listeners>50</max-listeners>   <!-- test mount: keep small -->
    <public>0</public>
  </mount>
  <fileserve>1</fileserve>
  <paths>
    <logdir>/var/log/icecast2</logdir>
    <webroot>/usr/share/icecast2/web</webroot>
    <adminroot>/usr/share/icecast2/admin</adminroot>
  </paths>
  <logging><loglevel>3</loglevel></logging>
</icecast>
```

Start it: `sudo systemctl enable --now icecast2` (apt) or run the KH binary under a unit you add. Confirm it's up: `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8010/` → `200` (use GET, not `-I`/HEAD — Icecast returns 400 to HEAD).

### 1c. Pull RTMP → MP3 → Icecast with ffmpeg (run during a LIVE show so there's audio)

```bash
ffmpeg -hide_banner -loglevel warning \
  -i rtmp://127.0.0.1:1935/live/stream-io \
  -c:a libmp3lame -b:a 128k -ar 44100 -ac 2 \
  -content_type audio/mpeg -f mp3 \
  icecast://source:CHANGE_ME_SOURCE@127.0.0.1:8010/test.mp3
```

---

## Step 2 — Validate (this is the gate)

```bash
# Mount is live and serving MP3 (GET, not HEAD — Icecast answers HEAD with 400):
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' --max-time 3 http://127.0.0.1:8010/test.mp3
# → 200 audio/mpeg
# Confirm the audio is NOT silent (silence ≈ -91 dB; a real broadcast is much louder):
curl -s --max-time 5 http://127.0.0.1:8010/test.mp3 -o /tmp/t.mp3
ffmpeg -hide_banner -i /tmp/t.mp3 -af volumedetect -f null - 2>&1 | grep mean_volume
# From your laptop (open the relay's firewall to your IP for 8010, or SSH-tunnel):
ssh -L 8010:127.0.0.1:8010 <relay-host>        # then use http://127.0.0.1:8010/test.mp3 locally
```

> If `/test.mp3` plays but is **silent**, the source isn't reaching the producer
> (Liquidsoap's `mksafe` is filling with silence) — not a mount problem. See Step 3.

- **Desktop browser:** `<audio controls src="http://127.0.0.1:8010/test.mp3">` plays.
- **iPhone Safari (MANDATORY gate):** navigate Safari directly to `http://<relay>:8010/test.mp3` (or an HTML page with the `<audio>` tag served over the SAME http origin). It must play. ⚠️ It will **not** play if embedded in an `https://` page over `http://` audio (mixed-content block) — that's expected; production fixes it with TLS (Step 4). For this gate, test over plain http directly.
- **Icecast admin:** `http://<relay>:8010/admin/stats.xml` (admin creds) shows the source connected + listener count.

If iOS plays the `/test.mp3` → the codec/transport decision is validated. Proceed.

---

## Step 3 — Replace ffmpeg with Liquidsoap (#174)

Liquidsoap gives fallback-to-silence, scheduling, and a clean configurable producer. **Use Liquidsoap 2.x** (has `input.ffmpeg`); the apt version on an old Ubuntu may be 1.4 — if so install via opam or the official 2.x package.

```bash
liquidsoap --version    # want 2.x
```

`moafunk.liq`:

```liquidsoap
# --- source: pull the live RTMP locally from NMS ---
src = input.ffmpeg(format="flv", "rtmp://127.0.0.1:1935/live/stream-io")
# never let the mount 404 if the broadcaster drops: fall back to silence
radio = mksafe(src)

# --- output: MP3 to Icecast /test.mp3 (iOS-safe codec) ---
output.icecast(
  %mp3(bitrate=128, samplerate=44100, stereo=true),
  host="127.0.0.1", port=8010,
  password="CHANGE_ME_SOURCE",
  mount="/test.mp3",
  name="Moafunk (Icecast test)",
  description="Phase-2 parallel-run test mount",
  genre="radio",
  radio
)
```

Run it (foreground for the test): `liquidsoap moafunk.liq`. Re-run the Step 2 validation against the Liquidsoap-fed mount. Once stable, wrap it in a systemd unit (`Restart=always`).

**#174 parameterization:** make `rtmp://127.0.0.1:1935/live/stream-io`, the Icecast host/port/password, and the mount name config (env or a small `.liq` settings block), so the same script serves both `/test.mp3` and (Phase 3) `/live.mp3`.

---

## Step 4 — TLS for the mount (needed before any public/iOS use beyond the http smoke test)

Public listeners come over HTTPS (the site is `https://`), so the mount must be TLS to avoid mixed-content blocking on iOS. Terminate TLS in front of Icecast (don't expose 8010 publicly):

- nginx/Caddy reverse proxy on the relay, `proxy_pass http://127.0.0.1:8010;` with a real cert, **or**
- the same edge that already fronts `stream.moafunk.de`.

Note: Cloudflare's proxy **does not** offload continuous-stream bandwidth (a live Icecast mount isn't cacheable), so it doesn't solve the 300-listener egress on its own — see Phase 4.

---

## Step 5 — Broadcaster preview player (#175)

Add the validated mount to the admin UI as a preview so the broadcaster hears the Icecast output before cutover:

- A simple `<audio controls :src="icecastPreviewUrl">` in the admin stream view, pointed at the TLS `/test.mp3` URL via a new `VITE_STREAM_ICECAST_TEST_URL` env var.
- This is the first *repo* change of Phase 2 — small and behind a config var. Do it only after Step 2/4 prove the mount on iOS.

---

## Rollback

Everything here is **additive**. To back out: `sudo systemctl stop icecast2` (and stop Liquidsoap/ffmpeg). NMS, the backend RTMP push, and all existing HLS/FLV listeners are never touched at any point.

## Hand-off to Phase 3 (#176) and Phase 4 (#177–178)

- **Phase 3 cutover:** stand up a `/live.mp3` mount the same way, soak-test, then flip the frontend's stream source to it (collapse `VITE_STREAM_HLS_URL` + `VITE_STREAM_FLV_URL` into a single Icecast URL — do this as an env-var flip during a show, not a code deploy). Decommission NMS HLS/FLV only after the soak passes.
- **Phase 4 SPOF/CDN (now mandatory at ~300 listeners):** the single 2 GB box can't be the only origin. Use Icecast-KH **master→relay** (slave Icecast servers pulling the mount) behind a load balancer, or a streaming CDN. Plus a patch/reboot plan for the 960-day-uptime host. The ~38 Mbps sustained egress is the sizing input.

## Checklist

- [ ] Step 0 ports/headroom confirmed; Icecast port (8010) free, NMS untouched
- [ ] Icecast running, `/test.mp3` mount defined
- [ ] ffmpeg smoke test serves MP3 to the mount (during a live show)
- [ ] **iPhone Safari plays `/test.mp3`** ← gate
- [ ] Liquidsoap replaces ffmpeg, `mksafe` fallback verified (drop the source → silence, no 404)
- [ ] TLS proxy in front of the mount
- [ ] Broadcaster preview player wired in admin (#175)
- [ ] Rollback rehearsed (stop services → listeners unaffected)
