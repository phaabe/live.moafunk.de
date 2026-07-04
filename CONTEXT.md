# CONTEXT — Ubiquitous Language (Show Host Dashboard)

> Glossary only. No implementation details, no decisions, no to-dos. When a term
> here conflicts with how code names something, the conflict is called out so we
> can reconcile it deliberately rather than by accident.
>
> Scope: the admin SPA's per-show host workflow (`frontend/src/admin`) and the
> backend social/recording surfaces that back it.

## Core entities

**Show** — A single scheduled episode with a date, start/end time, title,
description, cover image, and zero-or-more assigned artists. The unit everything
else hangs off. (Code: `Show` / `ShowDetail`.)

**Show type** — Distinguishes editorial formats. `unheard` shows use the legacy
info-card detail layout; `external` / "brunchtime" shows use the newer dashboard
layout. *Terminology watch:* code and comments use "external" and "brunchtime"
interchangeably — treat them as one type until we decide on a single canonical
word.

**Host** — The person responsible for a show's broadcast. Either a standing user
(role `host`) or the admin themselves. Distinct from **Guest** below.

**Guest** — A one-off host identity created for a single show: can only log in on
the show date and is removed afterwards. A Guest *is* a kind of host for that
show, but is not a standing user. (When we say "assign a host" the target may be
either a standing user or a freshly created Guest.)

## Broadcast

**Media mode** — How a show reaches air. Exactly one of:
- **Live** — the host streams in real time (go-live story).
- **Upload** — a pre-recorded audio file is played out at air time (upload story).

*Terminology watch:* the UI says **Upload**, the host-flow composable says
**`prerecorded`**, and `useHostFlow.UploadMode` is `'prerecorded' | 'live'`.
Same concept, three spellings. Canonical user-facing word: **Upload**.

**Air time** — A show's scheduled start instant (Berlin wall-clock, stored as
date + `start_time`). The countdown counts down to this.

**Go live / Playout** — The act of taking a show to air. For a Live show this is
the host starting their stream; for an Upload show it is the scheduled/auto
playout of the confirmed file. Both converge on the **On-air** state.

**On-air** — The window between a show's air time and its scheduled end, during
which it is actually broadcasting.

**Recording** — A captured audio artifact of a broadcast. Two distinct origins,
do NOT conflate:
- **Live capture** — automatically recorded from a Live broadcast (`latest_recording`;
  states: processing / ready / failed).
- **Manual upload** — a file an admin uploads after the fact (`recording_*`).

## Promotion (needs reconciliation — see terminology watch)

**Announcement** — A social-media post about a show. A show has at most **two
announcement slots**, each optional:
- **Pre-announcement** — promotes the upcoming show; published before air.
- **Post-announcement** — shares the recording; published after the show.

**Channel** — A destination for an announcement: **Instagram** and/or **Telegram**.
A single announcement slot may target one or both channels.

**Queued post** — An announcement the host has authored and left to publish
automatically at its scheduled time (as opposed to publishing on the spot).

**Schedule anchor** — Every scheduled announcement time is defined *relative to
the show's date/time*, so it moves automatically when the show is rescheduled.
Two anchor models exist:
- **Offset** — a duration before/after a show landmark, e.g. "3 h before air".
- **Time-of-day** — a wall-clock time on a day expressed relative to the show,
  e.g. "18:00 on the day before the show". Still recomputes if the show moves.

**Scheduling policy** — Which anchor model *and its timing value* governs an
announcement's automatic send. Owned entirely by an **admin in Configuration**,
one policy **per channel × per slot** (Instagram-pre, Instagram-post,
Telegram-pre, Telegram-post are four independent policies) — deliberately *not*
editable on the show dashboard. On the dashboard the host owns only *what*
(caption/content), *where* (which channels), and *whether* (slot on/off); the
send time is derived and shown read-only, with a manual "Post now" override.

*Terminology watch — GAP TO BUILD:* none of this exists yet. Today Instagram
posts fire **immediately** (`postToInstagram` → `instagram_posted_at`), Telegram
has an on-demand **preview** send, and `scheduler.rs` auto-sends only a fixed
*artist-preview cadence* (day N after a show → artist N). The slot / queued-post /
scheduling-policy model above is new.

**Send state** — Where a queued announcement is in its life, tracked *per
channel* (so one slot can be IG-sent / TG-pending):
- **Pending** — queued, not yet fired; fully editable and cancelable.
- **Sent** — published; read-only, shows the live post link, guarded manual re-post only.
- **Failed** — the send was attempted and errored; eligible for retry.
- **Cancelled** — the host un-queued it before it fired.

**Content freshness** — When a queued post fires, its **caption is frozen** (the
exact text the host authored) but its **media is resolved live** at send time:
the image is the show's current cover, and a post-announcement's SoundCloud link
is whatever exists then (it usually doesn't exist yet when the post is written).

**Publish (SoundCloud)** — Uploading a finished recording to SoundCloud and
setting its visibility (public/private). Separate from an Announcement, though a
Post-announcement often links to the published SoundCloud track.

## Lifecycle

**Phase** — Where a show is in its life, *derived* from air time + record state.
Three-phase vocabulary for the dashboard; phases are **soft** — they drive
emphasis/default view, they never lock a panel:
- **Prep** — now < air time: edit metadata, choose media mode, assign host, queue
  pre-announcement.
- **Broadcast** — between air time and scheduled end: countdown, then a launchpad
  + "● ON AIR" status strip. Actual stream operation happens in the fullscreen
  `/stream` flow, not the cockpit.
- **Wrap-up** — past scheduled end or a recording exists: recording status,
  publish the auto-uploaded SoundCloud track (make public), queue
  post-announcement.
