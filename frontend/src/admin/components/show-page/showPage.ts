import type { Series, SeriesEpisode, SeriesHost, SeriesSlot } from '../../api';
import { berlinToUtcDate, nextDayDateStr } from '../../showTime';

// Pure helpers of the show detail page (#/shows/<slug>, issue 1051).

/** Upcoming episodes soonest first, past ones newest first. */
export function splitEpisodes(episodes: readonly SeriesEpisode[]): {
  upcoming: SeriesEpisode[];
  past: SeriesEpisode[];
} {
  const key = (e: SeriesEpisode) => `${e.date} ${e.start_time ?? ''}`;
  const upcoming = episodes
    .filter((e) => e.status === 'upcoming')
    .sort((a, b) => key(a).localeCompare(key(b)) || a.id - b.id);
  const past = episodes.filter((e) => e.status !== 'upcoming');
  return { upcoming, past };
}

/** "12:00–14:00", or the start alone. */
export function episodeTime(e: Pick<SeriesEpisode, 'start_time' | 'end_time'>): string {
  if (e.start_time && e.end_time) return `${e.start_time}–${e.end_time}`;
  return e.start_time ?? '';
}

/** Time left as the design writes it: "2 days 4 h", "5 h 20 min", "12 min". */
export function timeLeft(ms: number): string {
  const minutes = Math.max(1, Math.ceil(ms / 60000));
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  const mins = minutes % 60;
  if (days > 0) return `${days} ${days === 1 ? 'day' : 'days'}${hours ? ` ${hours} h` : ''}`;
  if (hours > 0) return `${hours} h${mins ? ` ${mins} min` : ''}`;
  return `${mins} min`;
}

function startMs(e: SeriesEpisode): number | null {
  return e.start_time ? berlinToUtcDate(e.date, e.start_time).getTime() : null;
}

function endMs(e: SeriesEpisode): number | null {
  if (!e.end_time) return null;
  const overnight = !!e.start_time && e.end_time <= e.start_time;
  return berlinToUtcDate(overnight ? nextDayDateStr(e.date) : e.date, e.end_time).getTime();
}

/** The episode's air time has come (it is still upcoming, so not over yet). */
export function hasStarted(e: SeriesEpisode, now: number): boolean {
  const start = startMs(e);
  return start !== null && now >= start;
}

/** "Starts in 2 days 4 h"; "On air now · until 14:00" once it started. */
export function countdown(e: SeriesEpisode, now: number): string {
  const start = startMs(e);
  if (start === null) return '';
  if (now < start) return `Starts in ${timeLeft(start - now)}`;
  const end = endMs(e);
  return end !== null && now < end ? `On air now · until ${e.end_time}` : 'On air now';
}

/** The flow's next-step text of the owner's next episode. */
export function nextStep(e: SeriesEpisode, now: number): string {
  const started = hasStarted(e, now);
  if (e.mode === 'prerecorded') {
    if (e.has_file === false)
      return 'Upload your set before air time. Without a file nothing plays.';
    if (started) return 'The set is playing.';
    return 'Nothing to do. The set plays at air time.';
  }
  if (started) return 'The Studio is open.';
  return 'Test your audio in the Studio before air time.';
}

/** "Every week · Sundays · 12:00–14:00", "Every other Friday · 22:00–23:30". */
export function rhythmText(series: Pick<Series, 'rhythm' | 'rhythm_line'>): string {
  if (!series.rhythm || !series.rhythm_line) return 'No fixed rhythm';
  return series.rhythm.repeat_rule === 'weekly'
    ? `Every week · ${series.rhythm_line}`
    : series.rhythm_line;
}

/** "Host: lena"; a Show with several hosts reads "Hosts: lena, jonas". */
export function hostsLine(hosts: readonly SeriesHost[] | undefined): string {
  const names = (hosts ?? []).map((h) => h.username);
  if (!names.length) return '';
  return `${names.length === 1 ? 'Host' : 'Hosts'}: ${names.join(', ')}`;
}

/** "lena, jonas · mira": the Show's hosts, then the episode's own co-hosts. */
export function episodeHostsText(hosts: readonly SeriesHost[] | undefined): string {
  return hostGroups(hosts)
    .map((group) => group.map((h) => h.username).join(', '))
    .join(' · ');
}

/** The Show's hosts, then the episode's own co-hosts; empty groups left out. */
export function hostGroups(hosts: readonly SeriesHost[] | undefined): SeriesHost[][] {
  const all = hosts ?? [];
  return [all.filter((h) => h.role !== 'episode'), all.filter((h) => h.role === 'episode')].filter(
    (group) => group.length > 0
  );
}

/** The SoundCloud tag of a past episode. */
export function soundcloudLabel(state: SeriesEpisode['soundcloud']): string | null {
  if (state === 'public') return 'Public on SoundCloud';
  if (state === 'private') return 'Private on SoundCloud';
  return null;
}

/** One date of the Schedule card: a booked episode or a free slot. */
export type ScheduleItem =
  | { kind: 'episode'; key: string; date: string; episode: SeriesEpisode }
  | { kind: 'free'; key: string; date: string; slot: SeriesSlot };

/** Upcoming episodes and free slots in one list, soonest first. */
export function scheduleItems(
  upcoming: readonly SeriesEpisode[],
  slots: readonly SeriesSlot[]
): ScheduleItem[] {
  const items: ScheduleItem[] = [
    ...upcoming.map((episode) => ({
      kind: 'episode' as const,
      key: `e${episode.id}`,
      date: episode.date,
      episode,
    })),
    ...slots.map((slot) => ({
      kind: 'free' as const,
      key: `f${slot.date} ${slot.start_time}`,
      date: slot.date,
      slot,
    })),
  ];
  const at = (i: ScheduleItem) =>
    `${i.date} ${(i.kind === 'episode' ? i.episode.start_time : i.slot.start_time) ?? ''}`;
  return items.sort((a, b) => at(a).localeCompare(at(b)));
}

/** The countdown split for the card: "Starts in" + "2 days 4 h", "On air now" + "until 14:00". */
export function countdownParts(
  e: SeriesEpisode,
  now: number
): { label: string; value: string } | null {
  const text = countdown(e, now);
  if (!text) return null;
  if (text.startsWith('Starts in ')) return { label: 'Starts in', value: text.slice(10) };
  const [label, value = ''] = text.split(' · ');
  return { label, value };
}

/** "AT" for "Antons Test Show": the cover tile of a Show without a cover. */
export function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0].toUpperCase())
    .join('');
}
