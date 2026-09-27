import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { effectScope, ref, type EffectScope } from 'vue';
import { useHostLoginWarnings, type HostLoginWarningSource } from '../useHostLoginWarnings';

const show: HostLoginWarningSource = {
  date: '2026-09-12',
  start_time: '12:23',
  status: 'scheduled',
  host_has_logged_in: false,
};
let scope: EffectScope;
function warningsFor(shows: HostLoginWarningSource[]) {
  const source = ref(shows);
  const warnings = scope.run(() => useHostLoginWarnings(source))!;
  return { source, warnings };
}

beforeEach(() => {
  vi.useFakeTimers();
  scope = effectScope();
});
afterEach(() => {
  scope.stop();
  vi.useRealTimers();
});

describe('host login warnings', () => {
  it.each([
    ['2026-09-12T09:22:59.999Z', false],
    ['2026-09-12T09:23:00.000Z', true],
    ['2026-09-12T10:22:59.999Z', true],
    ['2026-09-12T10:23:00.000Z', true],
    ['2026-09-12T10:23:00.001Z', false],
  ])('at %s, warning is %s (Berlin summer time)', (now, expected) => {
    vi.setSystemTime(new Date(now));
    expect(warningsFor([show]).warnings.value.length).toBe(expected ? 1 : 0);
  });

  it.each([
    ['2026-01-12', '12:23', '2026-01-12T10:23:00Z'],
    ['2026-09-13', '00:15', '2026-09-12T21:15:00Z'],
    ['2026-03-29', '04:00', '2026-03-29T01:00:00Z'],
    ['2026-10-25', '04:00', '2026-10-25T02:00:00Z'],
  ])(
    'uses Berlin time for %s %s including winter, midnight and DST days',
    (date, start_time, now) => {
      vi.setSystemTime(new Date(now));
      expect(warningsFor([{ ...show, date, start_time }]).warnings.value).toHaveLength(1);
    }
  );

  it('requires a host with explicitly no recorded login and a scheduled start', () => {
    vi.setSystemTime(new Date('2026-09-12T10:00:00Z'));
    const excluded = [
      { ...show, host_has_logged_in: true },
      { ...show, host_has_logged_in: null },
      { ...show, host_has_logged_in: undefined },
      { ...show, status: 'completed' },
      { ...show, status: 'live' },
      { ...show, start_time: undefined },
      { ...show, date: '' },
    ];
    expect(warningsFor(excluded).warnings.value).toEqual([]);
  });

  it('enters and leaves the window while the page stays open and disposes its timer', () => {
    vi.setSystemTime(new Date('2026-09-12T09:22:59Z'));
    const { warnings } = warningsFor([show]);
    expect(warnings.value).toEqual([]);
    vi.advanceTimersByTime(1000);
    expect(warnings.value).toEqual([show]);
    vi.advanceTimersByTime(3_601_000);
    expect(warnings.value).toEqual([]);
    scope.stop();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('reacts to refreshed login and schedule data', () => {
    vi.setSystemTime(new Date('2026-09-12T10:00:00Z'));
    const { source, warnings } = warningsFor([show]);
    expect(warnings.value).toHaveLength(1);
    source.value[0].host_has_logged_in = true;
    expect(warnings.value).toEqual([]);
    source.value = [{ ...show, start_time: '14:00' }];
    expect(warnings.value).toEqual([]);
  });
});
