import type { NewSeries, RepeatRule, Series } from './api';

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

export function timeError(d: SeriesDraft): string | null {
  return d.repeatRule !== 'none' && d.startTime && d.endTime && d.endTime <= d.startTime
    ? 'The end time must be after the start time'
    : null;
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
