import type { Series, SeriesEpisode, SeriesHost } from '../../api';
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

/** "Host: lena"; a Show with several hosts (later) reads "Hosts: lena, jonas". */
export function hostsLine(hosts: readonly SeriesHost[] | undefined): string {
  const names = (hosts ?? []).map((h) => h.username);
  if (!names.length) return '';
  return `${names.length === 1 ? 'Host' : 'Hosts'}: ${names.join(', ')}`;
}

/** The SoundCloud tag of a past episode. */
export function soundcloudLabel(state: SeriesEpisode['soundcloud']): string | null {
  if (state === 'public') return 'Public on SoundCloud';
  if (state === 'private') return 'Private on SoundCloud';
  return null;
}
