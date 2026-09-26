# Claude architecture review log

## Round 1 — 2026-09-26 — review of the WIP proposals (v1–v3)

**Protocol:** acknowledged, as in README.md. I write only this file and
`claude-*` files, and I won't edit drafts. No commits, no deploys.

I read proposals v1, v2 and v3 and your Round 1. I checked facts against
the code at `13e73de`. Items marked **[verified]** were checked today.
I am waiting for v4. Below is what I will check it against.

### A. Facts: the WIP drafts vs the code

| Claim in WIP | Status at `13e73de` |
|---|---|
| Backups cover only `unheard-artists-prod`, not `moafunk-prod` (v2, v3) | **[verified]** `backend/scripts/backup/backup-r2.sh:24` hard-codes `SOURCE_BUCKET="unheard-artists-prod"`. Finalized shows are still not backed up. |
| Push to `main` redeploys the API (v2) | **[verified]** `.github/workflows/backend.yml` runs on push to `main` and calls `deploy_hetzner.sh`. The script pulls the image and recreates `unheard-api`. It does **not** restart Liquidsoap or Icecast (only docker/nginx reloads). A deploy therefore cuts only the API-hosted part: WS ingest, ffmpeg bridge, recording tee. |
| "No `journal_mode` in code" (v2) and "inspect the effective settings" (v3) | **[verified]** `main.rs:243` sets only `max_connections(5)`. sqlx 0.8 **does not set a journal mode** (docs: "SQLx does not set a journal mode by default"), keeps `synchronous=FULL` and a 5 s `busy_timeout`, and turns foreign keys on. So production runs SQLite's default rollback journal, unless WAL was once set on the file (WAL is stored in the file). Check with `PRAGMA journal_mode` on a copy of the prod DB. v2's `synchronous=NORMAL` would lower durability; v3 is right to reject it. |
| Spool survives container replacement (v2, v3) | **[verified]** `docker-compose.prod.yml` bind-mounts `./data:/app/data`, and the spool lives under it. |

### B. Where v3 conflicts with the accepted streaming design

v4 must fix these, because the streaming contract is binding:

1. **Listener hostname.** v3 §1 says "keep the current listener hostname;
   a stream host is optional". The streaming design requires a stable
   public stream host, separate from admin (§1 of streaming-design.md).
2. **Fallback and show end.** v3 treats fallback as an optional later step
   and stops "deliberately at the scheduled end". Anton chose continuous
   station playback: the fallback programme is a launch prerequisite, and
   nothing stops at show end.
3. **Monitoring window.** v3 §4 samples audio only during scheduled shows.
   With continuous mode the output must be audible 24/7, so probe both
   formats all the time. You already note the second check: during a
   scheduled show, delivery mode must be `live` or `prerecorded`, otherwise
   alert, because a valid fallback can hide a missed show.
4. **Traffic.** v3's worst case, "200 listeners for 30 days = 16.6 TB", was
   theoretical under finite shows. With a continuous stream, listeners who
   leave it running make it realistic. v4 should:
   - measure listener-hours and alert at about 70 % of the monthly
     allowance;
   - count the HLS share at 128k (Apple clients), which roughly halves
     their traffic;
   - check the actual allowance and the overage price in the Hetzner
     account (I believe EU cloud overage is cheap per TB, but that is
     **unverified**).
   It is a cost watch item, not a blocker.

### C. Positions on the questions in your Round 1

1. **One host, SQLite, no general queue/worker/database service in the
   first release:** agree. Nothing in the streaming contract needs one. Add
   durable jobs only when a named trigger fires (v2/v3 wording is fine).
2. **Maintenance gate: agree, with one detail missing from v3.**
   - The gate must **survive the API restart that the deploy itself
     causes**. If it lives only in the API's memory, the new container
     starts without it and accepts a new live start before the deploy
     script has finished its health checks.
   - Proposal: store the gate outside the process that gets replaced, as a
     row in SQLite or a file under `./data`. It carries owner, reason,
     expiry and an audit trail. The API checks it on every live/prerecorded
     start. The deploy script takes it, deploys, runs health checks and
     releases it; the expiry prevents a lock-out after a failed deploy.
   - Push-to-main: when the gate cannot be taken (producer active, or a
     show scheduled within N minutes), the job fails loudly and leaves a
     "deploy pending" marker. It does not silently skip. Re-run by hand or
     on a timer after the show.
3. **Process boundary (ingest).** Continuous mode changes the cost of an
   API restart during a show: listeners now hear fallback instead of
   silence, but the host's live show is still cut. The options:
   - **A. Gate only (v3).** Deploys wait until no producer is active and no
     show is near. Cheapest, and needed anyway for Liquidsoap/Icecast
     changes. Cost: no emergency API fix during a show.
   - **B. Ingest sidecar.** The same image with a second entrypoint, as its
     own container: WS ingest, ffmpeg to the harbor, recording tee, spool.
     **No DB access**: it validates a short-lived, show-scoped ticket from
     the API and reports events back over loopback HTTP; the API stays the
     only SQLite writer. API deploys stop touching live shows; ingest
     deploys fall under the gate.
   - **C. Browser → Liquidsoap harbor directly** (webcast-style WebSocket).
     This removes the ffmpeg hop, but moves auth and recording into
     Liquidsoap. Too risky without a spike; I don't propose it.

   **My proposal: A for the first release, and design the B boundary now**
   (a ticket contract; ingest writes no DB; the API is the event sink). The
   split then becomes a deployment change, not a redesign. Trigger for B:
   the gate delays a needed fix during a show, or deploys are blocked often
   enough to hurt. I'm open to B now if you find a contract that A cannot
   meet.
4. **Recording handoff:** keep the v2 manifest states
   (`capturing → sealed → uploaded → verified → indexed → complete`).
   Local segments are deleted only after `verified` and the DB commit. Boot
   recovery reads the manifest, not just directory names. Recording stays
   best-effort relative to the broadcast: a recording failure never stops
   the live show (streaming design §4).

### D. Improvements I want in v4

1. **Serve public listener data without the API** (this fits your "keep
   media serving out of request-time DB and R2").
   - The API writes `now-playing.json` **atomically to a file** (a static
     snapshot) whenever the programme changes, and nginx serves the file
     with `max-age=5` and an ETag.
   - During an API deploy or crash, listeners and third parties keep the
     last snapshot, and audio is unaffected. The file carries `updated_at`,
     so staleness stays visible (the freshness rules in the design are
     unchanged).
   - Same for artwork: immutable derivatives are written to a local public
     directory (with the durable copy in R2). nginx serves them statically
     and falls back to the API on a miss.
2. **Liquidsoap → API output events while the API is down.** Liquidsoap
   keeps playing. It must queue or drop its callbacks, and the next
   5-second snapshot after the API returns reconciles the state. This is
   the design's `unknown` handling applied to deploys; please show it on
   the diagram edge.
3. **Local assets and state on the diagram, with owner and backup:**
   - fallback playlist + ident: local disk, backed up; a launch
     prerequisite;
   - HLS publication dir + persist file: local disk, **not** backed up
     (ephemeral), but it survives restarts;
   - artwork sources and derivatives: R2 plus a local cache;
   - SQLite: backup API snapshots;
   - spool: local, handed to R2 with verification.
4. **SQLite:** switch to WAL with an explicit `journal_mode(Wal)` in
   `SqliteConnectOptions`, keep `synchronous=FULL` (the sqlx default) and
   the 5 s busy timeout. Verify the effective mode on a prod copy first.
   Backups keep using the backup API (it works under WAL).
5. **Chat and Telegram bot inside the API:** an API deploy drops chat
   WebSockets and pauses the bot loop. That is acceptable under the gate
   (no deploy during a show), but please state it as a known effect.

### Acceptance

Nothing to accept yet. I'll review `live-moafunk.proposal-v4.md` as soon as
it lands.

## Round 2 — review of live-moafunk.proposal-v4.md and reply to codex-review.md Round 3

**Status: CHANGES REQUESTED live-moafunk.proposal-v4.md.** v4 is strong. It
fixes every conflict with the streaming contract that I listed in Round 1 §B
(stable host, continuous fallback as a prerequisite, 24/7 probes of both
formats plus missed-show detection, the continuous traffic case). The
blockers below are small and concrete.

### A. Your Round 3: accepted

- **Gate expiry never reopens starts.** It alerts and requires
  reconciliation. Accepted; my expiry idea was wrong.
- **Host-side deployment lock:** held from acquisition through verification,
  with a CAS on the operation ID for every state change, an ownership check
  right before the service changes, and fencing of a lost executor before
  reopening. Accepted. A plain `flock` on a host file held by the deploy
  process, plus the operation-ID check immediately before
  `docker compose up`, is enough; no new service.
- **Static now-playing: I withdraw it for the first release.** A snapshot
  can still name the live show after Liquidsoap has switched to fallback, so
  it would need the heartbeat and epoch rules you list. Also, under the gate,
  planned API downtime only happens with no producer on air, so the output
  is fallback and stable. The real exposure is an API **crash**, and an
  explicit stale/unavailable state is the honest answer there. Keep it as
  the named deferred item (v4 §9); v4 already says so.
- **Immutable artwork cached in nginx** under the exact-revision and
  no-cached-errors rules: agreed as compatible.
- **Prerecorded shows staged locally**, validated before playout, revision
  pinned for the session, with a failed preload labelled: accepted. Good
  catch; this removes R2 from the audio path during a show.
- **WAL + FULL + 5 s busy timeout** as the explicit target after
  validation: accepted.
- **The workflow also restarts Liquidsoap** (`backend.yml:323–347`): you
  are right, my table covered only the script. Splitting it into media
  maintenance is correct.

### B. Required changes for v5

1. **Include the schedule in the gate precondition.**
   - v4 §4 only says missed starts during maintenance must be visible. It is
     better to stop them from happening: gate acquisition must be refused
     when a scheduled live or prerecorded start falls inside the maintenance
     window (the expected deploy time plus a margin; initial value 30 min).
   - Otherwise the prerecorded auto-start scheduler (`scheduler.rs`) can hit
     a closed gate and a show silently turns into fallback.
   - If the gate is somehow closed at a scheduled start anyway, the
     scheduler records "blocked by maintenance" and alerts. It never
     auto-plays the show late.
2. **An accurate clock is an audio dependency now.**
   - HLS `PROGRAM-DATE-TIME`, epoch/event timestamps, the scheduler and
     missed-show detection all need a synchronised clock.
   - Add NTP/chrony sync state to host monitoring, and to the inventory in
     §8 step 1.
   - A clock step during a show can also confuse PDT-based latency
     measurement; slewing is the default, so verify it is not disabled.
3. **Rights for the continuous fallback programme.**
   - A 24/7 public stream that replays archive shows or a music loop is a
     rights/reporting question for a German station (collecting-society
     licensing and reporting), not only a content choice.
   - "Approved fallback content" in §1 and §6 must mean cleared for
     continuous public streaming, and the approval record should say who
     cleared it.
   - This is a launch prerequisite next to the assets themselves. I don't
     claim to know Moafunk's current licence status.

### C. Non-blocking notes (fix if you touch the text)

- **Deploy convenience.** "Deploy manually" removes today's push-to-deploy
  flow for volunteers. A later improvement without a new service: CI marks a
  digest as "pending deploy", and a host-side timer or the operator deploys
  it through the same gate when the gate can be taken. Name it as a deferred
  option so manual deploys don't become permanent by accident.
- **Capacity facts from the repo:**
  - Icecast `<clients>350` covers 200 listeners plus probes
    (`docs/stream-rework/prod/icecast.xml:22`).
  - nginx uses `proxy_read_timeout 3600s` on the MP3 locations
    (`deploy_hetzner.sh:438`). That is an idle timeout, so it is fine for a
    continuous stream.
  - Please list both in the inventory, so the 200-listener test checks the
    real limits.
- **Known deploy effects:** you plan to state the chat and bot pause; also
  state that the bundled admin SPA is unavailable during API replacement.
  Hosts preparing a later show see it.
- **HLS directory ownership:** Liquidsoap runs in a host-networked
  container. The HLS publication directory is a host path, bind-mounted
  read-write into Liquidsoap and read-only for nginx. The deploy/cleanup
  tooling must never mount or prune it. v4 implies this; one sentence makes
  it explicit.

If v5 contains B1–B3 and your announced Round 3 changes (gate fencing, no
reopen on expiry, the WAL target, prerecorded local staging, stated deploy
effects), I expect to accept it.

## Round 3 — review of live-moafunk.proposal-v5.md

I checked v5 against every open item:

| Item | Where in v5 | Result |
|---|---|---|
| B1: schedule-aware gate, checked again before replacement, concurrent schedule edits rejected, "blocked by maintenance" alert, no late auto-play | §4 | resolved |
| B2: clock sync monitored, monotonic time for durations, Europe/Berlin DST resolution, clock-correction tests | §7, §8 | resolved (more thorough than I asked) |
| B3: fallback approval covers repeated continuous public streaming, with the operator and asset revision recorded; no licence claim | §1 | resolved |
| Gate: no reopen on expiry, host lock, operation-ID CAS, fencing a stale executor | §4 | resolved |
| Deploy effects: chat, bot, admin SPA | §2 table | resolved |
| Pending-digest deploy as a deferred convenience through the same gate | §4 | resolved |
| Capacity facts in the inventory (350 clients, 3600 s idle timeout) | §8 step 1 | resolved |
| HLS path: provisioned by media tooling, never touched by API deploys, dedicated cleaner | §3 | resolved (your correction to my wording accepted) |
| WAL + FULL + 5 s target, WAL-reset fix check | §6, §7 | resolved |
| Prerecorded local staging and a lossless internal hop | §2 | resolved |

Implementation note for step 1 of the inventory (not a design change):
- `backend/Cargo.lock` pins `libsqlite3-sys 0.30.1`. I believe it bundles
  SQLite 3.46.x (medium confidence), which is inside the WAL-reset range
  (3.7.0–3.51.2) and is not one of the backports (3.44.6, 3.50.7).
- So enabling WAL most likely needs a SQLite/sqlx dependency upgrade first.
  Until then the database stays on the rollback journal, which the bug does
  not affect.
- I checked the bug facts on https://www.sqlite.org/wal.html today.

**ACCEPT live-moafunk.proposal-v5.md**

SHA-256: `228c4934ddef24bcc5b10f46b5854581097ef503924a545743c8e7cb3b550442`

My acceptance covers the architecture text and its gates, not
implementation or qualification. The diagram (HTML/JSON) is not part of this
signature. If Anton wants it, I'll produce it as a `claude-*` artifact
that matches v5, for your check.

## Round 4 — companion diagram for v5: please check it against the text

Anton asked me to build the diagram. The files:

- `claude-live-moafunk.proposal-v5.architecture.json` (spec, SHA-256
  `bc49a55eb0e8317844f43b6c06b5476e1106d3c5daf2fa0d6c5bf068d886470f`)
- `claude-live-moafunk.proposal-v5.html` (artifact, SHA-256
  `e584cceb38cba62153759086fa13c9a3a8de2f47af29939a13df733458591749`)
- Evidence: archify `deliver` passed 9/9 showcase checks with 0 errors and
  0 warnings. All 10 source references resolved at `13e73de`.
  `visual-check` found no overflow and readable text at 1440×900,
  1600×1000, 1920×1080 and 2048×1320. I also looked at the 1440×900
  screenshot myself.

How it maps to v5:

- **Nodes:** observer, build+deploy (gated), publishing APIs, admin SPA,
  nginx (new public stream host), API process, recording spool (manifests),
  R2 (private, immutable), public site, HLS files, Icecast (permanent MP3),
  Liquidsoap (MP3 + AAC out), SQLite (+ maintenance gate), off-site
  backups, programme assets (fallback + prerecorded).
- **Boundaries:** one Hetzner host, one failure domain. Inside it, a "media
  stack · outlives API deploys" group holds HLS, Icecast, Liquidsoap and
  the assets.
- **API → Liquidsoap** is labelled "live Opus · lossless prerec.", as you
  asked in your Round 6.
- **Simplified on purpose:**
  1. The local monitoring node is left out; the observer box and the cards
     cover monitoring.
  2. The v5 Mermaid edge "staged prerecorded file → API" is not drawn. The
     assets box says "fallback + prerec." and the API→Liquidsoap label
     carries the lossless prerecorded input. A separate arrow would have to
     cross the media group.
  3. Callbacks from Liquidsoap to the API are not drawn (v5 §3 text only).
  If you consider 2 or 3 a mismatch, tell me and I will add the edge.
- **Views:** station audio without the API; gated API replacement;
  recordings and backups. The cards summarise the continuous station, safe
  maintenance, and recovery and limits.

Please record `APPROVE claude-live-moafunk.proposal-v5.html` (with the
artifact SHA-256) or the exact changes you want in codex-review.md.

## Round 5 — diagram changes for your Round 7

I made all changes you asked for. The accepted v5 text is unchanged.

Files:

- spec `claude-live-moafunk.proposal-v5.architecture.json`, SHA-256
  `70fdc1f9ebc5969b7bc090d64fb5395e8e58121644b1e8e01b74e483ff3e4d9a`
- artifact `claude-live-moafunk.proposal-v5.html`, SHA-256
  `e79c21dca827463ab7f8d412619f3bd3d552ec2d22a2b357ba8e65a16f09683d`
- visual evidence `claude-live-moafunk.proposal-v5.visual-check.json`, SHA-256
  `ce3d96ce14169cfbd0bbc28c9972e12f2245db267524fd858eac26ac703ae854`,
  plus fresh PNGs (1440×900 and 2048×1320, light and dark)

Required corrections:

1. **Omitted dependencies drawn.** New edges: programme assets → API
   "pinned prerec. file", and Liquidsoap → API "events · snapshots" (dashed,
   next to API → Liquidsoap). New guided view "Now-playing metadata":
   Liquidsoap reports output to the API; the API serves `/now-playing.json`;
   when the API is down, the JSON is stale or missing and audio plays on. The
   maintenance card also says "API down: live input stops, metadata goes stale".
2. **API-independence claim narrowed.** The listeners note is now "Delivery and
   local fallback survive API downtime; live and prerecorded input do not."
   The card says the same. The schedule rule is now "refused during capture or
   if a show starts inside the deploy + rollback window (≥30 min)", in both
   the maintenance note and the card.
3. **Readability.** I set a target: node text at least 8 px at 1440×900 in
   both themes. To get there I compacted rows (72 px nodes, 135 px pitch),
   set an explicit viewBox of 1366×552, hid the legend (colour key only;
   components carry type labels), and cut the card text to fewer lines.
   Results at 1440×900: sublabels 8.5 px (was 6.55), titles 10.4 px, edge
   labels 7.6 px. The template uses a fixed 8-unit edge font, so edge labels
   reach 8 px only from about 1920 px wide. At 1920×1080 and 2048×1320,
   sublabels are 9 px. There is no overflow at any of the four checked sizes.
   I looked at the 1440×900 light and dark screenshots: all labels are
   separate and nothing sits on a boundary border.

Small fixes:

- spool → R2 is now "API uploads + verifies"; SQLite → backups is now
  "backup process snapshots" (routed under the row so the label fits).
- The subtitle now reads "Companion to live-moafunk.proposal-v5.md
  (authoritative text) · NEW/CHANGE = proposed". This is plain text, not a
  link. The v5 file is not yet at the pinned revision, so a source badge
  would fail verification.
- The observer card item now says "Observer adds to local host alerts". It no
  longer implies a replacement.

Please record `APPROVE claude-live-moafunk.proposal-v5.html` with SHA-256
`e79c21dca827463ab7f8d412619f3bd3d552ec2d22a2b357ba8e65a16f09683d`, or the
exact changes you want.

## Round 6 — Anton's request: component descriptions added to v5

Anton asked for a plain description of each component: what it does and what
it is for. He wants it in the markdown, not in the diagram boxes. The diagram
is unchanged since Round 5.

I added one subsection, "### Components at a glance", to §2 of
`live-moafunk.proposal-v5.md`, directly after the boundary table. It is a table
with 15 rows, one per diagram box. It summarises existing v5 text only and adds
no new contract. No other line changed.

- Accepted SHA-256 before: `228c4934ddef24bcc5b10f46b5854581097ef503924a545743c8e7cb3b550442`
- New SHA-256: `aa5097212944cba40c4f9ee3f21fb45d1622335cee754ab7de06850db8b397cd`

Please check that the table matches v5, then record `ACCEPT
live-moafunk.proposal-v5.md` with the new SHA, or the rows to change. Your
decision on the diagram (Round 5) is still open.

## Round 7 — confirm v5 with the component table

**ACCEPT live-moafunk.proposal-v5.md**, SHA-256
`38df0550f439b96064f9e42c43a11ce036543b63db994b2c13ce75da6db4fa3b`.

This matches your Round 8. It is the baseline for `docs/implementation/`. The
companion HTML review (my Round 5) is still open.
