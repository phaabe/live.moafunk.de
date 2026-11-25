import type { NewSeries, RepeatRule, Series } from './api';
import { lengthBetween, MAX_LENGTH, MIN_LENGTH, parseHhmm } from './bookSlot';

/** The New Show / Show Details form. `repeatRule: 'none'` is "No fixed rhythm". */
export interface SeriesDraft {
  name: string;
  description: string;
  repeatRule: RepeatRule | 'none';
  weekday: number;
  startTime: string;
  endTime: string;
}

/** Defaults as on the flow's New Show screen: "Every week", "Sunday". */
export function emptyDraft(): SeriesDraft {
  return {
    name: '',
    description: '',
    repeatRule: 'weekly',
    weekday: 7,
    startTime: '',
    endTime: '',
  };
}

export function draftOf(series: Series): SeriesDraft {
  const r = series.rhythm;
  return {
    name: series.name,
    description: series.description ?? '',
    repeatRule: r?.repeat_rule ?? 'none',
    weekday: r?.weekday ?? 7,
    startTime: r?.start_time ?? '',
    endTime: r?.end_time ?? '',
  };
}

/** An end at or before the start is on the next day (23:00–01:00), as in Book Slot. */
export function timeError(d: SeriesDraft): string | null {
  const start = parseHhmm(d.startTime);
  const end = parseHhmm(d.endTime);
  if (d.repeatRule === 'none' || start === null || end === null) return null;
  const length = lengthBetween(start, end);
  if (length < MIN_LENGTH) return 'The end must be at least 15 minutes after the start';
  return length > MAX_LENGTH ? 'A show can be 12 h at most' : null;
}

export function draftValid(d: SeriesDraft): boolean {
  return (
    d.name.trim() !== '' &&
    (d.repeatRule === 'none' ||
      (d.weekday >= 1 && d.startTime !== '' && d.endTime !== '' && !timeError(d)))
  );
}

export function toNewSeries(d: SeriesDraft, cover: File | null): NewSeries {
  return {
    name: d.name.trim(),
    description: d.description.trim(),
    rhythm:
      d.repeatRule === 'none'
        ? null
        : {
            repeat_rule: d.repeatRule,
            weekday: d.weekday,
            start_time: d.startTime,
            end_time: d.endTime,
          },
    cover,
  };
}
