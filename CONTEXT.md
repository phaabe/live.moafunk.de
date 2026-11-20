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
else hangs off. (Code: `Show` / `ShowDetail`.) *Terminology watch:* the host
flow calls this an **Episode**, and uses **Show** for the series below.

**Series** — A host's recurring programme (for example "Brunchtime") with a
name, description, cover and rhythm; it owns its episodes. The host flow calls
it a **Show**. Its owner is the only host who may book, edit or start its
episodes; every host may view them. (Code: `show_templates`, `Series`.)

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

## Promotion

**Announcement** — An Instagram post or story about one episode. Hosts post
through the station account (`@moafunk_radio`); they never see its login. An
episode has three **announcement slots**, each optional:
- **Pre-announcement** — feed post (4:5) before air. Default: 2 days before, 18:00.
- **On-air story** — story (9:16), sent automatically at go-live. On by default;
  the host can switch it off. A story carries no link, so it shows
  "listen at live.moafunk.de" as text.
- **Post-announcement** — feed post (4:5) after the show. The host can set it up
  in wrap-up at any time; it is sent only after the SoundCloud track is public.
  Default: next day, 18:00.

Do not call an announcement a "moment". **Moment** means an on-air timecode
(Mark moment in the Studio). Moment clips for Instagram are a later feature.

**Channel** — Where an announcement goes. Only **Instagram** is built.
**Telegram** stays a channel for later.

**Station account** — The Instagram account a server posts to. Staging posts to
the test account; production posts to `@moafunk_radio`.

**Collaborator** — The host's own Instagram account (Instagram handle on the
show). A feed post invites it as collaborator, so the post also shows on the
host's profile. Stories take no collaborator.

**Queued post** — An announcement the host set up to send by itself at its time.

**Post slot** — A fixed send time: 12:00 or 18:00 Europe/Berlin. The on-air
story has no post slot; it goes at go-live.

**Episode limit** — Max announcements per episode. Default 3 (one per slot).

**Station limits** — Max sends for the whole station account. Defaults: 1 feed
post per day, 5 feed posts per week, 6 stories per day.

**Full slot** — A post slot the station limits do not allow. The post moves to
the next free slot. It is never dropped. The host sees why it moved.

**Scheduling policy** — The episode limit, station limits, post slots, days
before for the pre-announcement, and the approval mode. Set by an admin on the
Instagram queue page. Hosts pick only from free slots.

**Approval mode** — When an admin must approve a post before it is sent:
- **First posts** (default) — the first 2 posts of a new host.
- **Always** — every post.
- **Never** — no approval; admins get a Telegram notice per post.

**Pause** — One admin switch that stops all posting. Queued posts keep their
time and wait until posting resumes.

**Send state** — Where an announcement is in its life:
- **Draft** — started, not scheduled yet.
- **Needs approval** — waits for an admin (see approval mode).
- **Scheduled** — has a post slot; sends at that time.
- **Armed** — on-air story, waits for go-live.
- **Waiting for SoundCloud** — post-announcement, waits for the public track.
- **Sent** — posted; shows the post link.
- **Failed** — the send errored; an admin can retry it.
- **Cancelled** — the host cancelled it before it was sent.
- **Rejected** — an admin did not approve it. The host can set it up again.

**Edit window** — The host can edit or cancel an announcement only before it is
sent. After that the caption is frozen (Instagram cannot edit it). Only an admin
removes a sent post, by hand in the Instagram app.

**Content freshness** — The image is rendered from the station template at send
time, from the image the host picked (episode cover, uploaded photo or guest
photo) plus the headline (max 18 characters). The caption (max 600 characters)
is the host's text, plus the date line and station hashtags.

*Terminology watch:* the admin show and artist Instagram posts
(`postToInstagram` → `instagram_posted_at`) are an older, separate path; they
are not announcements. Telegram has an on-demand **preview** send, and
`scheduler.rs` also sends a fixed *artist-preview cadence* (day N after a show →
artist N).

**Publish (SoundCloud)** — Uploading a finished recording to SoundCloud and
setting its visibility (public/private). Separate from an Announcement, but a
Post-announcement waits for the public SoundCloud track.

## Lifecycle

**Phase** — Where a show is in its life, *derived* from air time + record state.
Three-phase vocabulary for the dashboard; phases are **soft** — they drive
emphasis/default view, they never lock a panel:
- **Prep** — now < air time: edit metadata, choose media mode, assign host, queue
  pre-announcement.
- **Broadcast** — between air time and scheduled end: countdown, then a launchpad
  + "● ON AIR" status strip. Actual stream operation happens in the fullscreen
  `/stream` flow, not the cockpit. Go-live sends the on-air story.
- **Wrap-up** — past scheduled end or a recording exists: recording status,
  publish the auto-uploaded SoundCloud track (make public), queue
  post-announcement.
