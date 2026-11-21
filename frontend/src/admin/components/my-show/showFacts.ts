import type { Series, UpcomingEpisode } from '../../api';
import { dayAndTime } from '../dashboard/dashboard';
import { rhythmText } from '../show-page/showPage';

/** One line of a Show's details in read mode: "Rhythm · Every week · Sundays · 12:00–14:00". */
export interface ShowFact {
  label: string;
  value: string;
}

/**
 * A Show's details as plain text for My Show (#1050): Next episode, Rhythm
 * and Instagram. Every card has the same three, so no value moves to another
 * place when a Show has no rhythm or no episode. `next` is the Show's next
 * episode, null when none is booked, undefined when the episodes could not load.
 */
export function showFacts(show: Series, next: UpcomingEpisode | null | undefined): ShowFact[] {
  const nextValue = next === undefined ? 'Could not load' : next ? dayAndTime(next) : 'None booked';
  return [
    { label: 'Next episode', value: nextValue },
    { label: 'Rhythm', value: rhythmText(show) },
    { label: 'Instagram', value: show.instagram_handle ? `@${show.instagram_handle}` : 'Not set' },
  ];
}
