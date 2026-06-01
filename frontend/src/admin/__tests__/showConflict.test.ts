import { describe, it, expect } from 'vitest';
import { rangesOverlap, findConflictingShow } from '../showConflict';
import type { ScheduleItem } from '../api';

const at = (h: number, m = 0) => new Date(2026, 5, 1, h, m);

function show(partial: Partial<ScheduleItem> & Pick<ScheduleItem, 'id' | 'date'>): ScheduleItem {
  return {
    title: 'Show',
    start_time: undefined,
    end_time: undefined,
    status: 'scheduled',
    show_type: 'external',
    artists: [],
    ...partial,
  } as ScheduleItem;
}

describe('rangesOverlap', () => {
  it('treats boundary-touching windows as free (half-open)', () => {
    expect(rangesOverlap(at(20), at(22), at(22), at(23))).toBe(false);
  });

  it('detects partial overlap and containment', () => {
    expect(rangesOverlap(at(20), at(22), at(21), at(23))).toBe(true);
    expect(rangesOverlap(at(20), at(23), at(21), at(22))).toBe(true);
  });

  it('treats identical starts as a clash even without end times', () => {
    expect(rangesOverlap(at(20), null, at(20), null)).toBe(true);
  });

  it('treats a start-only point inside another window as a clash', () => {
    expect(rangesOverlap(at(21), null, at(20), at(22))).toBe(true);
    expect(rangesOverlap(at(22), null, at(20), at(22))).toBe(false);
  });
});

describe('findConflictingShow', () => {
  const shows: ScheduleItem[] = [
    show({ id: 1, date: '2026-06-01', start_time: '20:00', end_time: '22:00', title: 'Evening' }),
    show({ id: 2, date: '2026-06-02', start_time: '20:00', end_time: '22:00', title: 'Other day' }),
  ];

  it('returns null when the slot is free', () => {
    expect(findConflictingShow(at(22), at(23), shows)).toBeNull();
  });

  it('returns the clashing show on the same day', () => {
    expect(findConflictingShow(at(21), at(23), shows)?.id).toBe(1);
  });

  it('ignores shows on other days', () => {
    // 20:00–22:00 on June 1 does not clash with the June 2 show.
    expect(findConflictingShow(at(20), at(22), [shows[1]])).toBeNull();
  });

  it('skips the excluded show (e.g. the one being edited)', () => {
    expect(findConflictingShow(at(20), at(22), shows, 1)).toBeNull();
  });

  it('ignores shows without a start time', () => {
    const noTime = [show({ id: 3, date: '2026-06-01', title: 'TBD' })];
    expect(findConflictingShow(at(20), at(22), noTime)).toBeNull();
  });
});
