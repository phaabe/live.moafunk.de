import { addDays, format, parseISO, startOfWeek } from 'date-fns';
import type { InstagramStatus, QueuedAnnouncement } from './api';
import { episodeDay } from './episodeRow';

/** Berlin wall clock of an instant: ["YYYY-MM-DD", "HH:MM"]. */
function berlin(iso: string): [string, string] {
  const [date, time] = new Date(iso)
    .toLocaleString('sv-SE', { timeZone: 'Europe/Berlin' })
    .split(' ');
  return [date, time.slice(0, 5)];
}

/** "15.01." */
function dayMonth(iso: string): string {
  const [date] = berlin(iso);
  return `${date.slice(8, 10)}.${date.slice(5, 7)}.`;
}

/** Monday to Sunday of the Berlin week of `today` (YYYY-MM-DD), both included. */
export function weekOf(today: string): { from: string; to: string; title: string } {
  const monday = startOfWeek(parseISO(today), { weekStartsOn: 1 });
  const sunday = addDays(monday, 6);
  const sameMonth = monday.getMonth() === sunday.getMonth();
  const start = format(monday, sameMonth ? 'dd.' : 'dd.MM.');
  return {
    from: format(monday, 'yyyy-MM-dd'),
    to: format(sunday, 'yyyy-MM-dd'),
    title: `This week · ${start}–${format(sunday, 'dd.MM.')}`,
  };
}

const SLOT_LABEL: Record<string, string> = { pre: 'Pre', onair: 'On air', post: 'Post' };

const STATE_LABEL: Record<string, string> = {
  draft: 'Draft',
  pending_approval: 'Needs approval',
  scheduled: 'Scheduled',
  armed: 'Armed',
  waiting_soundcloud: 'Waiting for SoundCloud',
  sent: 'Posted',
  failed: 'Failed',
  cancelled: 'Cancelled',
  rejected: 'Rejected',
};

/** A queue row as the flow writes it: "Fr 23.10. · 18:00 · Pre". */
export function rowWhen(row: QueuedAnnouncement): string {
  const at = row.sent_at ?? row.scheduled_at;
  const [date, time] = at ? berlin(at) : ['', ''];
  // An on-air story goes out when the episode goes live, not at a set time.
  const notYet = row.slot === 'onair' && !['sent', 'failed'].includes(row.state);
  return [date && episodeDay(date), notYet ? 'at go-live' : time, SLOT_LABEL[row.slot] ?? row.slot]
    .filter(Boolean)
    .join(' · ');
}

/** "Brunchtime · lena · Needs approval", with the error of a failed post. */
export function rowMeta(row: QueuedAnnouncement): string {
  return [row.series_name, row.host_handle, STATE_LABEL[row.state] ?? row.state, row.error]
    .filter(Boolean)
    .join(' · ');
}

/** Rows that count against the station limits. */
const counts = (row: QueuedAnnouncement) =>
  !['draft', 'failed', 'cancelled', 'rejected'].includes(row.state);

/** Feed posts this week and stories today (Berlin date `today`). */
export function usage(
  rows: QueuedAnnouncement[],
  today: string
): { feed: number; stories: number } {
  const live = rows.filter(counts);
  const day = (r: QueuedAnnouncement) => {
    const at = r.sent_at ?? r.scheduled_at;
    return at ? berlin(at)[0] : '';
  };
  return {
    feed: live.filter((r) => r.slot !== 'onair').length,
    stories: live.filter((r) => r.slot === 'onair' && day(r) === today).length,
  };
}

const WARN_MS = 14 * 24 * 60 * 60 * 1000;

/** The account pill: which account this server posts to and how long its token lasts. */
export function statusPill(
  status: InstagramStatus,
  now: Date
): { text: string; tone: 'ok' | 'warn' | 'failed' } {
  const who = `Posts to @${status.account_username}${status.account === 'dev' ? ' (test account)' : ''}`;
  if (!status.token_expires_at) return { text: `${who} · no token`, tone: 'failed' };
  const left = new Date(status.token_expires_at).getTime() - now.getTime();
  const day = dayMonth(status.token_expires_at);
  if (left <= 0) return { text: `${who} · token expired ${day}`, tone: 'failed' };
  if (left < WARN_MS) return { text: `${who} · token expires ${day}`, tone: 'warn' };
  return { text: `${who} · token valid to ${day}`, tone: 'ok' };
}

/** "Token refreshed 16.11. · API quota 3 / 25", or "" when the server knows neither. */
export function statusDetail(status: InstagramStatus): string {
  return [
    status.token_refreshed_at && `Token refreshed ${dayMonth(status.token_refreshed_at)}`,
    status.quota_used !== null &&
      status.quota_total !== null &&
      `API quota ${status.quota_used} / ${status.quota_total}`,
  ]
    .filter(Boolean)
    .join(' · ');
}

/** "12:00, 18:00" → ["12:00", "18:00"], or null when a time is not HH:MM. */
export function parseSlots(text: string): string[] | null {
  const slots = text
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
  return slots.length && slots.every((s) => /^([01]\d|2[0-3]):[0-5]\d$/.test(s)) ? slots : null;
}
