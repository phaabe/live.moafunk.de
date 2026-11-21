import type { RepeatRule, Series } from '../../api';

/** One line of a Show's details in read mode: "Repeats · Every week". */
export interface ShowFact {
  label: string;
  value: string;
}

const DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

/** The words of the Repeats select in the Show form. */
const REPEATS: Record<RepeatRule, string> = {
  weekly: 'Every week',
  biweekly: 'Every 2 weeks',
};

/**
 * A Show's details as plain text for My Show (#1050): Repeats, Day, Time,
 * Instagram and Hosts. `compact` (the grid) puts Day and Time in one item.
 * Without a rhythm there is no Day and no Time.
 */
export function showFacts(show: Series, hosts: string[], compact = false): ShowFact[] {
  const rhythm = show.rhythm;
  const facts: ShowFact[] = [
    { label: 'Repeats', value: rhythm ? REPEATS[rhythm.repeat_rule] : 'No fixed rhythm' },
  ];
  if (rhythm) {
    const day = DAYS[rhythm.weekday - 1] ?? '';
    const time = `${rhythm.start_time}–${rhythm.end_time}`;
    if (compact) facts.push({ label: 'Day · Time', value: `${day} · ${time}` });
    else facts.push({ label: 'Day', value: day }, { label: 'Time', value: time });
  }
  facts.push(
    { label: 'Instagram', value: show.instagram_handle ? `@${show.instagram_handle}` : 'Not set' },
    { label: 'Hosts', value: hosts.join(', ') || '—' }
  );
  return facts;
}
