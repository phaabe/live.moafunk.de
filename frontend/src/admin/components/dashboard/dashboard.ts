import type { SeriesOverviewItem, UpcomingEpisode } from '../../api';
import { episodeDay } from '../../episodeRow';
import { berlinToUtcDate, nextDayDateStr } from '../../showTime';
import { fmtClock } from '../../composables/useShowClocks';

/**
 * Helpers of the host Dashboard (#1049). Pure, so the cards stay small and
 * the tests can call them without mounting.
 */

/** The public player, for "Listen live". */
export const LISTEN_URL = 'https://live.moafunk.de';

/**
 * The hosts of an episode or a Show. Today that is the one host of its Show;
 * the cards take a list so several hosts per episode (#1047) fit in later.
 */
export function hostsOf(item: Pick<UpcomingEpisode, 'host_username'>): string[] {
  return item.host_username ? [item.host_username] : [];
}

/** True when the signed-in user is one of the hosts. */
export function isMine(hosts: string[], username: string | undefined): boolean {
  return !!username && hosts.includes(username);
}

/** "12:00–14:00", only the start without an end, or null without times. */
export function timeRange(e: Pick<UpcomingEpisode, 'start_time' | 'end_time'>): string | null {
  return e.start_time && e.end_time ? `${e.start_time}–${e.end_time}` : e.start_time;
}

/** "So 25.10. · 12:00–14:00" */
export function dayAndTime(e: Pick<UpcomingEpisode, 'date' | 'start_time' | 'end_time'>): string {
  const time = timeRange(e);
  return time ? `${episodeDay(e.date)} · ${time}` : episodeDay(e.date);
}

/** Start and end of an episode in ms since the epoch. An end at or before the start is the next day. */
export function airWindow(
  e: Pick<UpcomingEpisode, 'date' | 'start_time' | 'end_time'>
): { start: number; end: number | null } | null {
  if (!e.start_time) return null;
  const start = berlinToUtcDate(e.date, e.start_time).getTime();
  if (!e.end_time) return { start, end: null };
  const endDate = e.end_time <= e.start_time ? nextDayDateStr(e.date) : e.date;
  return { start, end: berlinToUtcDate(endDate, e.end_time).getTime() };
}

/** True while `now` is between the episode's start and end. */
export function airsAt(e: Pick<UpcomingEpisode, 'date' | 'start_time' | 'end_time'>, now: number) {
  const window = airWindow(e);
  return !!window && window.end !== null && window.start <= now && now <= window.end;
}

/** How far the episode on air is: "00:42:10" since its start, and the share of its length. */
export function progressOf(
  e: Pick<UpcomingEpisode, 'date' | 'start_time' | 'end_time'>,
  now: number
): { elapsed: string; percent: number | null } | null {
  const window = airWindow(e);
  if (!window) return null;
  const elapsedMs = Math.max(0, now - window.start);
  const length = window.end === null ? 0 : window.end - window.start;
  const percent = length > 0 ? Math.min(100, Math.round((elapsedMs / length) * 100)) : null;
  return { elapsed: fmtClock(elapsedMs), percent };
}

/** "Starts in 1 day 17 h", "Starts in 3 h 5 min", "Starts in 12 min", "Starts now". */
export function startsIn(ms: number): string {
  const minutes = Math.floor(ms / 60_000);
  if (minutes <= 0) return 'Starts now';
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  const mins = minutes % 60;
  if (days > 0) return `Starts in ${days} ${days === 1 ? 'day' : 'days'} ${hours} h`;
  if (hours > 0) return `Starts in ${hours} h ${mins} min`;
  return `Starts in ${mins} min`;
}

/** "Live · Host: lena", "Live · Hosts: lena, jonas" */
export function modeAndHosts(mode: string, hosts: string[]): string {
  if (!hosts.length) return mode;
  return `${mode} · ${hosts.length === 1 ? 'Host' : 'Hosts'}: ${hosts.join(', ')}`;
}

/** The Shows grid: most finished episodes first, then by name. */
export function byEpisodes(shows: SeriesOverviewItem[]): SeriesOverviewItem[] {
  return [...shows].sort(
    (a, b) =>
      b.finished_episodes - a.finished_episodes ||
      a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }) ||
      a.id - b.id
  );
}

/** "1 episode", "31 episodes" */
export function episodeCount(n: number): string {
  return `${n} ${n === 1 ? 'episode' : 'episodes'}`;
}
