import type { Announcement, AnnouncementSlot, AnnouncementTime } from './api';
import { episodeDay } from './episodeRow';
import { addDays, format, parseISO } from 'date-fns';

/** Texts and rules of the Promotion card, the composer and the wrap-up (CONTEXT.md "Promotion"). */

export const HEADLINE_MAX = 18;
export const CAPTION_MAX = 600;
/** Episode limit: one announcement per slot. */
export const EPISODE_LIMIT = 3;

/** An Instagram handle without "@" (the API stores it so). */
const HANDLE = /^[A-Za-z0-9._]{1,30}$/;

/** Strip a leading "@" and spaces; "" means no collaborator. */
export function cleanHandle(input: string): string {
  return input.trim().replace(/^@+/, '');
}

export function handleError(handle: string): string | null {
  return handle === '' || HANDLE.test(handle)
    ? null
    : 'Use up to 30 letters, numbers, dots or underscores.';
}

/** "Fr 23.10. · 18:00" in Berlin time, from an RFC 3339 instant. */
export function postTime(iso: string): string {
  const berlin = new Date(iso).toLocaleString('sv-SE', { timeZone: 'Europe/Berlin' });
  return `${episodeDay(berlin.slice(0, 10))} · ${berlin.slice(11, 16)}`;
}

/** Instagram takes at most 3 collaborators and 20 tagged accounts on a post (the backend checks it too). */
export const COLLABORATORS_MAX = 3;
export const TAGS_MAX = 20;

/** The slot names on the card, in the composer and on its buttons. */
export const SLOT_NAMES: Record<AnnouncementSlot, string> = {
  pre: 'Pre-announcement',
  onair: 'On-air story',
  post: 'Post-announcement',
};

/** One day of the composer's slot picker: "Fr 23.10." and its times ("18:00"). */
export interface TimeDay {
  day: string;
  times: (AnnouncementTime & { time: string })[];
}

/** The offered times grouped by their Berlin day, in the given order. */
export function groupTimes(times: AnnouncementTime[]): TimeDay[] {
  const days: TimeDay[] = [];
  for (const t of times) {
    const [day, time] = postTime(t.scheduled_at).split(' · ');
    const last = days[days.length - 1];
    if (last?.day === day) last.times.push({ ...t, time });
    else days.push({ day, times: [{ ...t, time }] });
  }
  return days;
}

/** Announcement photo upload: JPEG or PNG, max 10 MB (the backend checks it too). */
export const PHOTO_MAX_MB = 10;
const PHOTO_TYPES = ['image/jpeg', 'image/png'];

/** Why a picked photo cannot be uploaded, or null when it can. */
export function photoError(file: File): string | null {
  if (!PHOTO_TYPES.includes(file.type)) return 'Use a JPEG or PNG photo.';
  if (file.size > PHOTO_MAX_MB * 1024 * 1024)
    return `The photo is too big. Max ${PHOTO_MAX_MB} MB.`;
  return null;
}

export const PAUSED_NOTE = 'Posting is paused by the station.';
/** An admin turned the Instagram connection off. */
export const INSTAGRAM_OFF_NOTE = 'Instagram is temporarily not available.';

/**
 * "Moved from 12:00: the station already posts that day." when the post was moved
 * off the time the host picked. The day is named only when it changed.
 */
export function movedNote(a: Announcement | undefined): string | null {
  if (!a?.requested_at || !a.scheduled_at) return null;
  if (new Date(a.requested_at).getTime() === new Date(a.scheduled_at).getTime()) return null;
  const from = postTime(a.requested_at);
  const sameDay = from.slice(0, -8) === postTime(a.scheduled_at).slice(0, -8);
  return `Moved from ${sameDay ? from.slice(-5) : from}: the station already posts that day.`;
}

/**
 * Default time of the post slot: 3 days after the episode at 18:00.
 * shortcut: the station default; the admin setting `post_days_after` is not readable by hosts.
 */
export function defaultPostTime(date: string): string {
  return `${episodeDay(format(addDays(parseISO(date), 3), 'yyyy-MM-dd'))} · 18:00`;
}

/**
 * What the slot readings need of an announcement: the full row (the episode's
 * hosts) or its status (every other host, no error text).
 */
export type SlotReading = Pick<Announcement, 'slot' | 'state' | 'scheduled_at' | 'sent_at'> & {
  error?: string | null;
};

/** The announcement in a slot that still counts (a cancelled one does not). */
export function inSlot<T extends SlotReading>(list: T[], slot: AnnouncementSlot): T | undefined {
  return list.find((a) => a.slot === slot && a.state !== 'cancelled');
}

/** A slot the host may still edit or cancel: set up and not sent. */
export function editable(a: SlotReading | undefined): boolean {
  return !!a && a.state !== 'draft' && a.state !== 'sent';
}

/**
 * The row reading of a slot. `trackPublic` only matters for the post slot,
 * which is sent only when the SoundCloud track is public.
 */
export function slotState(list: SlotReading[], slot: AnnouncementSlot, trackPublic = true): string {
  const a = inSlot(list, slot);
  if (!a) {
    return list.some((x) => x.slot === slot && x.state === 'cancelled')
      ? 'Cancelled'
      : 'Not set up';
  }
  const at = a.scheduled_at ? postTime(a.scheduled_at) : '';
  switch (a.state) {
    case 'draft':
      return 'Not set up';
    case 'sent':
      return a.sent_at ? `Sent · ${postTime(a.sent_at)}` : 'Sent';
    case 'failed':
      return `Failed · ${a.error || 'Instagram did not take the post.'}`;
    case 'pending_approval':
      return at ? `Needs approval · ${at}` : 'Needs approval';
    default:
      if (slot === 'post' && !trackPublic)
        return at ? `${at} · waits for the public track` : 'Waiting for SoundCloud';
      return at ? `Scheduled · ${at}` : 'Scheduled';
  }
}
