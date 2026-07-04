import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { berlinToUtcDate, isShowEnded, isShowRunning, nextDayDateStr } from '../showTime';

/** Freeze wall-clock at a UTC instant. */
function freezeAtUtc(iso: string) {
  vi.setSystemTime(new Date(iso));
}

describe('showTime', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  describe('berlinToUtcDate', () => {
    it('converts Berlin summer time (CEST, UTC+2)', () => {
      expect(berlinToUtcDate('2026-07-04', '20:00').toISOString()).toBe(
        '2026-07-04T18:00:00.000Z'
      );
    });

    it('converts Berlin winter time (CET, UTC+1)', () => {
      expect(berlinToUtcDate('2026-01-15', '20:00').toISOString()).toBe(
        '2026-01-15T19:00:00.000Z'
      );
    });
  });

  describe('nextDayDateStr', () => {
    it('increments across a month boundary', () => {
      expect(nextDayDateStr('2026-06-30')).toBe('2026-07-01');
    });
  });

  describe('isShowRunning', () => {
    const show = { date: '2026-07-04', start_time: '20:00', end_time: '22:00' };

    it('is false before air time', () => {
      freezeAtUtc('2026-07-04T17:59:00Z'); // 19:59 Berlin
      expect(isShowRunning(show)).toBe(false);
    });

    it('is true during the show', () => {
      freezeAtUtc('2026-07-04T19:00:00Z'); // 21:00 Berlin
      expect(isShowRunning(show)).toBe(true);
    });

    it('is false after the scheduled end', () => {
      freezeAtUtc('2026-07-04T20:01:00Z'); // 22:01 Berlin
      expect(isShowRunning(show)).toBe(false);
    });

    it('handles overnight shows (end <= start rolls to the next day)', () => {
      const overnight = { date: '2026-07-04', start_time: '23:00', end_time: '01:00' };
      freezeAtUtc('2026-07-04T22:30:00Z'); // 00:30 Berlin on the 5th
      expect(isShowRunning(overnight)).toBe(true);
    });

    it('is false without a start or end time', () => {
      freezeAtUtc('2026-07-04T19:00:00Z');
      expect(isShowRunning({ date: '2026-07-04', start_time: '20:00' })).toBe(false);
      expect(isShowRunning({ date: '2026-07-04', end_time: '22:00' })).toBe(false);
    });
  });

  describe('isShowEnded', () => {
    const show = { date: '2026-07-04', start_time: '20:00', end_time: '22:00' };

    it('is false while the show is still running', () => {
      freezeAtUtc('2026-07-04T19:00:00Z'); // 21:00 Berlin
      expect(isShowEnded(show)).toBe(false);
    });

    it('is true after the scheduled end', () => {
      freezeAtUtc('2026-07-04T20:01:00Z'); // 22:01 Berlin
      expect(isShowEnded(show)).toBe(true);
    });

    it('handles overnight shows — not ended just after midnight', () => {
      const overnight = { date: '2026-07-04', start_time: '23:00', end_time: '01:00' };
      freezeAtUtc('2026-07-04T22:30:00Z'); // 00:30 Berlin on the 5th
      expect(isShowEnded(overnight)).toBe(false);
    });

    it('is false without an end time', () => {
      freezeAtUtc('2026-07-05T12:00:00Z');
      expect(isShowEnded({ date: '2026-07-04', start_time: '20:00' })).toBe(false);
    });
  });
});
