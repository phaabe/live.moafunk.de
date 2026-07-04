# Show Host Dashboard — Cockpit/Tabs A/B + Queued Announcements

> **Status:** approved plan, not yet implemented (as of 2026-07-04).
> Companion docs: [`CONTEXT.md`](../../CONTEXT.md) (glossary / ubiquitous language)
> and [ADR-0001](../adr/0001-scheduled-announcements-model.md) (data model).
> Future sessions: execute PRs in order below; PR 0 (this file) is done.

## Context

The admin SPA already covers ~80% of the desired host workflow, but scattered:
`ShowDetailPage.vue` (metadata, host/guest, media mode, countdown banner; SoundCloud,
immediate Instagram post and Telegram preview exist but only in the UNHEARD branch —
external shows have **no promotion or wrap-up UI today**, `ShowSocialChannels` is a
static placeholder) plus the fullscreen `/stream/*` host flow (upload→confirm /
live→on-air). An interview session (2026-07-04) resolved the design;
its durable outputs are already on disk:

- `CONTEXT.md` (repo root) — glossary: announcement slots, schedule anchors, scheduling
  policy, send states, content freshness, soft phases.
- `docs/adr/0001-scheduled-announcements-model.md` — data-model decision (accepted).

This plan turns those decisions into implementation. **Two deliverables:**

1. **A/B of two dashboard shells** (phase-aware cockpit vs explicit tabs) over shared
   panel components, wired to real data at `/shows/:id?layout=cockpit|tabs`.
2. **Queued announcements** — the one genuinely new subsystem: per show up to two
   optional slots (pre/post), shared caption, IG and/or TG, auto-sent at a time derived
   from admin-configured per-channel×per-slot policies.

## Decisions locked in the interview (do not re-litigate)

| Topic | Decision |
|---|---|
| Page structure | Try BOTH: phase-aware cockpit AND tabbed layout, real SPA, shared panels, `?layout=` toggle; delete loser after A/B |
| Scope | External/"brunchtime" shows only; roles host/guest/admin. **UNHEARD is legacy — untouched** |
| Phases | Soft (emphasis/default-tab only, nothing locked). Prep = now<air; Broadcast = air≤now≤end (`isShowRunning` logic); Wrap-up = now>end OR recording exists |
| Live moment | Stays in fullscreen `/stream/*` flow. Cockpit Broadcast phase = launchpad (countdown + Go live / Review upload) + "● ON AIR" status strip. No live controls in cockpit |
| Announcement slots | Exactly two per show (pre + post), each optional |
| Scheduling | Anchors relative to show datetime (reschedule ⇒ times move). Two models: offset ("3h before air") and time-of-day ("18:00 day before"). Admin picks model AND value in Configuration, per channel×slot (4 policies: IG-pre, IG-post, TG-pre, TG-post). NOT editable on show dashboard |
| Host owns | Caption (what), channel toggles (where), slot on/off (whether). Sees resolved send time read-only + "Post now" escape hatch |
| Content | One shared caption per slot; "✨ Draft with AI" (reuse `ai.rs`); image = show cover. Post-announce: SoundCloud link auto-appended clickable on TG, plain/"link in bio" on IG |
| Freshness | Caption frozen at authoring; media resolved live at send (current cover, current SC link) |
| Lifecycle | Editable/cancelable until sent; then read-only + post link; per-channel send state (pending/sent/failed/cancelled); guarded manual re-post |
| Data model | New `scheduled_announcements` table, one row per (show, slot), channels as columns (ADR-0001). Legacy `shows.instagram_posted_at` etc. coexist, not migrated |
| Wrap-up | Recording status card + SC "make public" primary action (auto-upload-private already ships, #260) + post-announce composer; manual upload/capture = fallbacks |

## Codebase facts (verified 2026-07-04)

- **Settings storage**: SQLite `app_settings` key/value (`db.rs:206`), helpers
  `db::get_setting`/`db::set_setting` (`db.rs:884/893`). Route pattern:
  `/api/settings/notifications` → `handlers/settings.rs` (`get_notifications`/`set_notifications`,
  admin-gated). Frontend: `settingsApi` in `frontend/src/admin/api/index.ts:823`.
- **Migrations**: hand-rolled idempotent `run_migrations` in `db.rs:28-594` —
  `CREATE TABLE IF NOT EXISTS` + `add_column_if_missing()` helper. No sqlx-migrate.
- **Scheduler**: detached `tokio::spawn` + `tokio::time::interval` loops in `main.rs`
  (e.g. prerecorded auto-start every 30 s → `scheduler::check_prerecorded_show_start`,
  `main.rs:822`). Pattern to copy for the announcement tick.
- **Instagram**: `POST /api/shows/:id/instagram` → `api_post_show_to_instagram`
  (`handlers/api.rs:4435`) → `instagram::post_show_to_instagram`. **Caveat:** that handler
  is `require_admin`-gated (`api.rs:4441`) and stamps the legacy `instagram_posted_at`
  (`api.rs:4469`) — queued sends must bypass it and call a new **caption-override variant
  at the `instagram.rs` layer** (`build_show_caption` is `instagram.rs:939`; cover
  presigned 1 h from key `shows/{id}/cover.png`, `instagram.rs:923`), writing only the
  new table's columns, never the legacy ones (ADR-0001 coexistence).
- **Telegram**: previews via `telegram_notify::send_show_instagram_preview`
  (`telegram_notify.rs:1188`). New announcement send must pass
  `config.telegram_topic_id` (`config.rs:143-232` already defaults/normalizes it — the
  MoafunkBot topic, see #223/#224) or it leaks into General.
- **SoundCloud**: auto-upload private on finalize (`soundcloud::auto_upload_on_finalize`,
  `soundcloud.rs:443`, spawned from `handlers/recording.rs:1290`); make-public =
  `POST /api/shows/:id/soundcloud/privacy` → `soundcloud::set_track_privacy`
  (`soundcloud.rs:702`). `shows.soundcloud_url/_public` columns exist.
- **AI captions**: `ai.rs` — `generate_and_store_instagram_caption` (`ai.rs:253`),
  `call_openai` helper (`ai.rs:80`, currently **private — needs `pub(crate)`**). Reuse
  `call_openai` with a new announcement prompt.
- **Show reschedule hook point**: `api_update_show` (`api.rs:3327`, PUT `main.rs:582`)
  is the only place date/start/end change — single recompute hook.
- **Guests**: `router.ts:246-253` lands role `guest` on `/shows/:id`; the route allows
  guests (`router.ts:106`). The new shells WILL render for guests on day-of-show login.
- **Child-table pattern**: show child tables declare
  `FOREIGN KEY (show_id) REFERENCES shows(id) ON DELETE CASCADE` (`db.rs:245,464,540`);
  composite-unique pattern at `show_recordings` `UNIQUE(show_id, version)` (`db.rs:540`).
  sqlx enables `PRAGMA foreign_keys`, so cascades fire.
- **External-dashboard UI to extract**: `ShowDetailPage.vue` lines ~1008-1217
  (`v-if="!isUnheard"` branch): `ShowDeadlineBanner`, hero (cover/title/desc), air-date +
  host grid (incl. guest creation), `ShowMediaCard` + `ShowSocialChannels` grid.
  Existing subcomponents live in `frontend/src/admin/components/show-detail/`.
- **Phase math**: reuse `isShowRunning`/`isShowEnded`/`berlinToUtcDate` from
  `useHostFlow.ts:166-182` (extract into a shared util — they're duplicated in
  `ShowDetailPage.vue:140` already).
- **API client pattern**: typed arrow methods on `xxxApi` objects calling
  `api.get/post/put/delete<T>()` (`api/index.ts`).

## Implementation

### PR 0 — persist this plan into the repo ✅ (this file)

### PR 1 — `refactor(dashboard): extract shared show panels` (frontend only, strictly behavior-preserving)
Create `frontend/src/admin/components/show-cockpit/panels/`:
- `IdentityPanel.vue` — hero: cover upload + title + description inline edit (from `:1022-1063`)
- `ScheduleHostPanel.vue` — air date/time edit + host/guest assignment (from `:1064-1204`)
- `MediaPanel.vue` — wraps `ShowMediaCard` + `ShowDeadlineBanner` + `enterFlow`/`launchFlow` handoff
- `PromotionPanel.vue` — placeholder rendering current `ShowSocialChannels` (composer lands in PR 4)

**No WrapUpPanel here** — SoundCloud + recording UI live in the UNHEARD+admin-only branch
today (`:1414`, `:1474-1540`); surfacing them on external shows is a behavior change and
belongs in PR 2. The `/stream` handoff survives extraction: `useHostFlow` is a
module-level singleton (`useHostFlow.ts:53-67`), callable from any component.

Extract `useShowPhase.ts` composable (Prep/Broadcast/Wrap-up derivation) + move the
Berlin-time helpers into one shared module (duplicated today in `useHostFlow.ts:150` and
`ShowDetailPage.vue:140`). `ShowDetailPage.vue` external branch renders the panels —
pixel-equivalent. Vitest: `useShowPhase` boundary cases (before air, during, overnight
end<start, ended, recording-exists).

### PR 2 — `feat(dashboard): cockpit + tabbed shells behind ?layout` (+ WrapUpPanel)
`pages/show-cockpit/` with `CockpitShell.vue` (phase rail, emphasis + auto-scroll to
current phase, all panels always rendered) and `TabbedShell.vue` (Details · Broadcast ·
Promotion · Wrap-up; default tab = derived phase; all tabs clickable). Route `/shows/:id`
picks shell from `?layout=` (persist last choice in `localStorage`), external shows only —
UNHEARD keeps the legacy branch untouched. Broadcast area = countdown/launchpad +
ON-AIR strip linking into `/stream/on-air`.

New `WrapUpPanel.vue` (deliberate new UI for external shows): live-recording card
(`latest_recording` processing/ready/failed states) + SC status + make-public toggle,
**role-gated** — SC publish/privacy actions admin-only (matching today's backend gates),
recording playback for host too.

**Guest handling (explicit):** guests land here via `landingRoute` (`router.ts:246`).
Render for `guest`: Identity/Schedule read-only, MediaPanel active (it's their show-day
tool), Promotion/WrapUp hidden. Verify with a guest login in the A/B check.

### PR 3 — `feat(promotion): scheduled_announcements table + policy config` (backend + ConfigPage)
- `db.rs` (`run_migrations`): `CREATE TABLE IF NOT EXISTS scheduled_announcements` —
  `id, show_id, slot ('pre'|'post'), caption, created_by, created_at, updated_at`, per
  channel `ig_enabled, ig_scheduled_at, ig_state, ig_fired_at, ig_post_url, ig_error` +
  same for `tg_`. **Constraints (in the CREATE, can't retrofit):**
  `UNIQUE(show_id, slot)` and `FOREIGN KEY (show_id) REFERENCES shows(id) ON DELETE
  CASCADE` — copy the `show_recordings` pattern (`db.rs:540`). States:
  pending/sent/failed/cancelled. **`_scheduled_at` is stored UTC**; time-of-day policies
  are Berlin wall-clock, converted via the `show_start_utc`/`show_end_utc` helpers
  (`scheduler.rs:145-170`) so DST is handled once.
- Policies as `app_settings` keys: `announce_policy_{ig,tg}_{pre,post}`, JSON
  `{model: 'offset'|'time_of_day', offset_minutes?, time?, day_delta?, ig_account?}`
  (IG account dev/prod lives on the policy). **Seed sensible defaults in
  `run_migrations`** (`INSERT OR IGNORE`, like `notifications_enabled`, `db.rs:219`) so
  a policy row always exists; composer additionally refuses to enable a channel whose
  policy fails to resolve. Admin-gated GET/PUT `/api/settings/announcement-policies`
  following `settings.rs` pattern.
- Resolution function (pure, unit-tested): policy + show date/start/end → UTC instant;
  recompute all *pending* `_scheduled_at` in `api_update_show` (`api.rs:3327`) when
  date/start/end change, and on policy save.
- ConfigPage: policy editor card (4 rows: channel×slot; model select + value inputs + IG account).

### PR 4 — `feat(promotion): announcement composer in PromotionPanel`
CRUD endpoints `/api/shows/:id/announcements` (list / upsert per slot / cancel) +
"post now" + **guarded re-post** (sent → explicit confirm, mirroring the existing IG
`force` flag). **Auth: host-of-show OR admin** (NOT the `require_admin` gate of the
legacy IG handler — new handlers, new gate; guests excluded). These endpoints and the
tick write **only** `scheduled_announcements` columns, never the legacy
`instagram_posted_at` / `telegram_preview_sent_at` (ADR-0001 coexistence).
Composer per slot: caption textarea, "✨ Draft with AI" (new endpoint reusing
`ai::call_openai` — make it `pub(crate)` — with show title/description context), IG/TG
toggles, resolved send time read-only, per-channel state chips (pending →
editable/cancel; sent → read-only + post link + guarded re-post; failed → error +
manual retry). Frontend `announcementsApi` group following existing pattern.

### PR 5 — `feat(promotion): scheduler tick sends queued announcements`
New `scheduler::check_scheduled_announcements` (60 s interval spawn in `main.rs`,
copying the auto-start spawn pattern): select rows where any channel `pending` and
`_scheduled_at <= now`; per channel — resolve cover presigned URL live; IG via the
caption-override variant at the `instagram.rs` layer; TG via new `telegram_notify` send
passing `config.telegram_topic_id`; post-slot: append current `soundcloud_url` (TG
clickable, IG plain text).

**Send semantics (explicit):** atomic CAS `UPDATE ... SET _state='sent', _fired_at=now
WHERE id=? AND _state='pending'` **before** the network call (rows-affected 0 ⇒ someone
else took it, skip); on send error, a second `UPDATE sent→failed` with `_error`. Small
crash window (marked sent, crash before send) is accepted — single-process deployment,
same class as existing `prerecorded_started_at` guard. `failed` rows are **never
auto-retried** — manual retry from the composer only. IG-sent/TG-failed is naturally
independent via per-channel columns. Alert ops Telegram on failure.
`cargo test`: due-math (incl. DST boundary), per-channel independence, no-double-fire
(CAS), reschedule-recompute.

### PR 6 — A/B decision
Use both layouts against real shows; delete the losing shell; drop `?layout=`.

## Verification

- **PR 1**: `npm test` + `npm run typecheck`; visually diff `/shows/:id` external show before/after (no change).
- **PR 2**: drive one real external show through Prep→Broadcast→Wrap-up in both layouts (`?layout=cockpit`, `?layout=tabs`); confirm `/stream` handoff still works end-to-end (upload story + live story); **log in as a guest** and confirm the gated rendering.
- **PR 3-5**: `cargo test` for resolution math + tick; end-to-end: queue a pre-announcement on a dev show with a 2-min-out policy, confirm IG (dev account `moafunk_tester`) + TG (topic 26) fire, states flip to sent, links recorded; reschedule the show, confirm pending times move; cancel a slot, confirm no fire.
- **Reminder**: run `gitnexus_impact` on `ShowDetailPage`/`useHostFlow` before PR 1 edits; `gitnexus_detect_changes` before each commit (repo convention).

## Open items (decide before PR 3, don't block PRs 1-2)

1. **Post-slot anchor landmark**: show-end offset vs "recording ready" trigger. Recommendation: offset from show end (simpler, deterministic); behavior when the SC link doesn't exist yet at fire time (send without link vs hold) — TBD with Anton.
2. **Failure alert audience**: ops Telegram topic only, or also surface to the host in the dashboard (the `_error` column already enables the latter).
3. Exact PR-2 guest panel gating (proposed above: read-only Identity/Schedule, active Media, no Promotion/WrapUp) — confirm with Anton before building.

## Review record

Plan was adversarially reviewed (Plan agent, 2026-07-04) against the codebase; 8
findings, all incorporated: WrapUpPanel moved out of the "behavior-preserving" PR 1
(it's new UI for external shows); explicit guest handling; new announcement endpoints
get their own host-or-admin gate and never touch legacy IG/TG columns; CAS-before-send
tick semantics with no auto-retry of `failed`; seeded default policies; FK CASCADE +
UNIQUE(show_id, slot) in the CREATE; UTC storage with Berlin-wall-clock policies;
line-ref corrections (`build_show_caption` :939, `auto_upload_on_finalize` :443,
`set_track_privacy` :702, `call_openai` :80 needs `pub(crate)`, topic id via
`config.telegram_topic_id`).
