import { describe, it, expect } from 'vitest';
import { useDateTimeRange, DEFAULT_DURATION_MIN } from '../useDateTimeRange';

describe('useDateTimeRange (start + duration)', () => {
  it('defaults to the default duration and no start', () => {
    const r = useDateTimeRange();
    expect(r.durationMinutes.value).toBe(DEFAULT_DURATION_MIN);
    expect(r.startDateTime.value).toBeNull();
    expect(r.endDateTime.value).toBeNull();
    expect(r.isValid.value).toBe(false);
  });

  it('derives the end and apiEndTime from start + duration', () => {
    const r = useDateTimeRange();
    r.startDateTime.value = new Date(2026, 5, 1, 20, 0);
    r.setDuration(90);
    expect(r.apiDate.value).toBe('2026-06-01');
    expect(r.apiStartTime.value).toBe('20:00');
    expect(r.apiEndTime.value).toBe('21:30');
    expect(r.isValid.value).toBe(true);
  });

  it('keeps the duration fixed when the start moves', () => {
    const r = useDateTimeRange();
    r.startDateTime.value = new Date(2026, 5, 1, 20, 0);
    r.setDuration(120);
    // Push the start an hour later — the end should follow, length unchanged.
    r.startDateTime.value = new Date(2026, 5, 1, 21, 0);
    expect(r.durationMinutes.value).toBe(120);
    expect(r.apiEndTime.value).toBe('23:00');
  });

  it('back-computes duration when the end is assigned directly', () => {
    const r = useDateTimeRange();
    r.startDateTime.value = new Date(2026, 5, 1, 20, 0);
    r.endDateTime.value = new Date(2026, 5, 1, 22, 30);
    expect(r.durationMinutes.value).toBe(150);
  });

  it('infers duration from an API start/end, wrapping overnight', () => {
    const r = useDateTimeRange();
    r.setFromApi('2026-06-01', '23:00', '01:00');
    expect(r.durationMinutes.value).toBe(120);
    expect(r.apiEndTime.value).toBe('01:00');
    expect(r.isValid.value).toBe(true);
  });

  it('resets back to no start and the default duration', () => {
    const r = useDateTimeRange();
    r.setFromApi('2026-06-01', '20:00', '22:30');
    r.reset();
    expect(r.startDateTime.value).toBeNull();
    expect(r.durationMinutes.value).toBe(DEFAULT_DURATION_MIN);
  });
});
