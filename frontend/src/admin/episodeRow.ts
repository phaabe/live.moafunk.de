import type { SeriesEpisode } from './api';

/**
 * Short episode day as the flow writes it: "So 25.10." (German weekday
 * without its dot, then day and month). `date` is "YYYY-MM-DD".
 */
export function episodeDay(date: string): string {
  const [year, month, day] = date.split('-').map(Number);
  const weekday = new Date(Date.UTC(year, month - 1, day))
    .toLocaleDateString('de-DE', { weekday: 'short', timeZone: 'UTC' })
    .replace(/\.$/, '');
  return `${weekday} ${String(day).padStart(2, '0')}.${String(month).padStart(2, '0')}.`;
}

/**
 * Where an episode row on the Show page opens. Upcoming opens the episode, an
 * open wrap-up opens the wrap-up (the same path), a finished one is no link.
 */
export function episodeTarget(episode: Pick<SeriesEpisode, 'id' | 'status'>): string | null {
  return episode.status === 'finished' ? null : `/shows/${episode.id}`;
}
