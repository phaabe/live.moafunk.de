import type { Announcement, AnnouncementSlot } from './api';
import { episodeDay } from './episodeRow';
import { nextDayDateStr } from './showTime';

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

/** Default time of the post slot: the day after the episode at 18:00. */
export function defaultPostTime(date: string): string {
  return `${episodeDay(nextDayDateStr(date))} · 18:00`;
}

/** The announcement in a slot that still counts (a cancelled one does not). */
export function inSlot(list: Announcement[], slot: AnnouncementSlot): Announcement | undefined {
  return list.find((a) => a.slot === slot && a.state !== 'cancelled');
}

/** A slot the host may still edit or cancel: set up and not sent. */
export function editable(a: Announcement | undefined): boolean {
  return !!a && a.state !== 'draft' && a.state !== 'sent';
}

/**
 * The row reading of a slot. `trackPublic` only matters for the post slot,
 * which is sent only when the SoundCloud track is public.
 */
export function slotState(
  list: Announcement[],
  slot: AnnouncementSlot,
  trackPublic = true
): string {
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
