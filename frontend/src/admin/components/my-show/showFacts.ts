import type { Series, UpcomingEpisode } from '../../api';
import { dayAndTime } from '../dashboard/dashboard';
import { rhythmText } from '../show-page/showPage';

/** One row of a Show's details in read mode: "Rhythm · Every week · Sundays · 12:00–14:00". */
export interface ShowFact {
  label: 'Next episode' | 'Rhythm' | 'Instagram';
  value: string;
  /** accent: a booked next episode; muted: nothing booked or set. */
  tone?: 'accent' | 'muted';
}

/**
 * A Show's details as plain text for My Show (#1050): Next episode, Rhythm
 * and Instagram. Every card has the same three, so no value moves to another
 * place when a Show has no rhythm or no episode. `next` is the Show's next
 * episode, null when none is booked, undefined when the episodes could not load.
 */
export function showFacts(show: Series, next: UpcomingEpisode | null | undefined): ShowFact[] {
  const nextFact: ShowFact = next
    ? { label: 'Next episode', value: dayAndTime(next), tone: 'accent' }
    : {
        label: 'Next episode',
        value: next === undefined ? 'Could not load' : 'None booked',
        tone: 'muted',
      };
  return [
    nextFact,
    { label: 'Rhythm', value: rhythmText(show) },
    show.instagram_handle
      ? { label: 'Instagram', value: `@${show.instagram_handle}` }
      : { label: 'Instagram', value: 'Not set', tone: 'muted' },
  ];
}

/** The cover label shows when the next episode is this many days away or less. */
const SOON_DAYS = 7;

/**
 * The cover label of a Show's next episode (#1050): "Today", "Tomorrow" or
 * "in 3 days", counted in calendar days from `today` (Berlin, "YYYY-MM-DD").
 * Null when nothing is booked or it is more than 7 days away.
 */
export function coverCountdown(
  next: UpcomingEpisode | null | undefined,
  today: string
): string | null {
  if (!next) return null;
  // Both dates parse as UTC midnight, so the difference is whole days.
  const days = (Date.parse(next.date) - Date.parse(today)) / 86_400_000;
  if (days < 0 || days > SOON_DAYS) return null;
  if (days === 0) return 'Today';
  if (days === 1) return 'Tomorrow';
  return `in ${days} days`;
}
