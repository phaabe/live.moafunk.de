---
status: accepted
---

# Scheduled announcements: dedicated table, one row per (show, slot)

## Context

The show host dashboard introduces **queued announcements**: up to two optional
slots per show (pre-announcement / post-announcement), each with one shared
caption, each targeting Instagram and/or Telegram, each channel firing
automatically at a time derived from an admin-configured scheduling policy. This
is genuinely new — today Instagram posts fire immediately (`instagram_posted_at`),
Telegram sends an on-demand preview, and `scheduler.rs` only auto-sends a fixed
artist-preview cadence. Nothing models a host-authored, schedulable, cancelable
post with per-channel send state.

## Decision

Store queued announcements in a new `scheduled_announcements` table, **one row
per (show, slot)** — at most two rows per show. The caption lives once on the
row (it is shared across channels). Each channel is represented by its own set
of columns: `{ig,tg}_enabled`, `_scheduled_at`, `_state`
(pending/sent/failed/cancelled), `_fired_at`, `_post_url`, `_error`. The
`scheduler.rs` tick polls for rows with any channel pending and due, resolves
media live (current cover; SoundCloud link for post slots), sends per channel,
and records the outcome independently per channel.

The legacy immediate-post fields on `shows` (`instagram_posted_at`,
`instagram_post_url`, `telegram_preview_sent_at`, `telegram_*_message_id`) are
**left in place** for the immediate-post / unheard path. The new table is the
source of truth for external-show queued announcements; the two coexist and are
not migrated into one another.

## Considered alternatives

- **Fully normalized** (`announcement_slots` + `announcement_deliveries`, one
  delivery row per channel): cleaner for N channels, but a join and two tables
  for a fixed 2×2 shape — over-built for exactly two channels with no plans for
  more.
- **Widen the `shows` table** with ~14 announcement columns: no new table, but
  `shows` is already wide and mixing volatile queue/send state into the core
  entity makes both harder to reason about.

## Consequences

- Adding a third channel later is a migration (acceptable — there are exactly
  two and none planned).
- Two announcement-tracking mechanisms coexist (legacy `shows` columns + the new
  table); a future engineer must know the new table owns external-show queued
  posts. This coexistence is deliberate, not an oversight.
