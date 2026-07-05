import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { effectScope, ref } from 'vue';
import { useShowPhase, type PhaseSource, type ShowPhase } from '../useShowPhase';

function freezeAtUtc(iso: string) {
  vi.setSystemTime(new Date(iso));
}

/** Run useShowPhase inside an effect scope and return the current phase. */
function phaseFor(show: PhaseSource | null, tickMs?: number): ShowPhase {
  const scope = effectScope();
  const result = scope.run(() => useShowPhase(ref(show), { tickMs }).phase.value);
  scope.stop();
  return result as ShowPhase;
}

// 20:00–22:00 Berlin on 2026-07-04 (CEST, UTC+2) → 18:00–20:00 UTC
const SHOW: PhaseSource = { date: '2026-07-04', start_time: '20:00', end_time: '22:00' };

describe('useShowPhase', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('is prep before air time', () => {
    freezeAtUtc('2026-07-04T12:00:00Z');
    expect(phaseFor(SHOW)).toBe('prep');
  });

  it('is broadcast between air time and scheduled end', () => {
    freezeAtUtc('2026-07-04T19:00:00Z'); // 21:00 Berlin
    expect(phaseFor(SHOW)).toBe('broadcast');
  });

  it('is wrapup after the scheduled end', () => {
    freezeAtUtc('2026-07-04T20:30:00Z'); // 22:30 Berlin
    expect(phaseFor(SHOW)).toBe('wrapup');
  });

  it('is wrapup as soon as a recording exists, even mid-broadcast', () => {
    freezeAtUtc('2026-07-04T19:00:00Z'); // 21:00 Berlin, show still running
    expect(phaseFor({ ...SHOW, latest_recording: { status: 'raw' } })).toBe('wrapup');
  });

  it('is broadcast during an overnight show just after midnight', () => {
    const overnight: PhaseSource = { date: '2026-07-04', start_time: '23:00', end_time: '01:00' };
    freezeAtUtc('2026-07-04T22:30:00Z'); // 00:30 Berlin on the 5th
    expect(phaseFor(overnight)).toBe('broadcast');
  });

  it('defaults to prep with no show', () => {
    freezeAtUtc('2026-07-04T12:00:00Z');
    expect(phaseFor(null)).toBe('prep');
  });

  it('transitions on the internal tick without a show mutation', () => {
    freezeAtUtc('2026-07-04T17:59:30Z'); // 19:59:30 Berlin — 30s before air
    const scope = effectScope();
    const phase = scope.run(() => useShowPhase(ref<PhaseSource>(SHOW), { tickMs: 1000 }).phase)!;
    expect(phase.value).toBe('prep');

    vi.advanceTimersByTime(60_000); // now 20:00:30 Berlin — on air
    expect(phase.value).toBe('broadcast');
    scope.stop();
  });

  it('clears its interval when the scope is disposed', () => {
    freezeAtUtc('2026-07-04T12:00:00Z');
    const clearSpy = vi.spyOn(globalThis, 'clearInterval');
    const scope = effectScope();
    scope.run(() => useShowPhase(ref<PhaseSource>(SHOW)));
    scope.stop();
    expect(clearSpy).toHaveBeenCalled();
    clearSpy.mockRestore();
  });
});
