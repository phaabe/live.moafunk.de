import { describe, it, expect } from 'vitest';
import { computeTick, verdictFor, CONTAINER_OVERHEAD } from '../src/admin/composables/useUploadHealth';

const TARGET = 192_000; // bits/s
const TARGET_BYTES_PER_S = (TARGET * CONTAINER_OVERHEAD) / 8;

function stats(bytesQueued: number, bufferedAmount = 0) {
  return { bytesQueued, bufferedAmount };
}

describe('computeTick', () => {
  it('reports encoded == egress when the buffer stays empty', () => {
    // 27,600 bytes/s ≈ 192 kbps × 1.15 overhead
    const t = computeTick(stats(0), stats(27_600), TARGET, 1000);
    expect(t.encodedKbps).toBeCloseTo(220.8, 1);
    expect(t.egressKbps).toBeCloseTo(220.8, 1);
    expect(t.bufferSeconds).toBe(0);
  });

  it('subtracts buffer growth from egress under congestion', () => {
    // Queued 27,600 bytes but 20,000 of them are stuck in the buffer.
    const t = computeTick(stats(0, 0), stats(27_600, 20_000), TARGET, 1000);
    expect(t.encodedKbps).toBeCloseTo(220.8, 1);
    expect(t.egressKbps).toBeCloseTo(((27_600 - 20_000) * 8) / 1000, 1);
    expect(t.bufferSeconds).toBeCloseTo(20_000 / TARGET_BYTES_PER_S, 3);
  });

  it('adds buffer drain to egress during recovery', () => {
    // Buffer shrank by 10,000 while 27,600 new bytes were queued.
    const t = computeTick(stats(0, 10_000), stats(27_600, 0), TARGET, 1000);
    expect(t.egressKbps).toBeCloseTo(((27_600 + 10_000) * 8) / 1000, 1);
  });

  it('never reports negative egress', () => {
    // Buffer grew by more than was queued (shouldn't happen, but clamp).
    const t = computeTick(stats(0, 0), stats(100, 5_000), TARGET, 1000);
    expect(t.egressKbps).toBe(0);
  });

  it('scales rates by the elapsed time', () => {
    const t = computeTick(stats(0), stats(27_600), TARGET, 2000);
    expect(t.encodedKbps).toBeCloseTo(110.4, 1);
  });

  it('yields a zero tick when the counter reset (socket reconnect)', () => {
    const t = computeTick(stats(1_000_000), stats(5_000), TARGET, 1000);
    expect(t).toEqual({ encodedKbps: 0, egressKbps: 0, bufferSeconds: 0 });
  });

  it('yields a zero tick for non-positive elapsed time', () => {
    const t = computeTick(stats(0), stats(27_600), TARGET, 0);
    expect(t).toEqual({ encodedKbps: 0, egressKbps: 0, bufferSeconds: 0 });
  });
});

describe('verdictFor', () => {
  it('is good while the buffer drains immediately', () => {
    expect(verdictFor(0)).toBe('good');
    expect(verdictFor(0.4)).toBe('good');
  });

  it('is tight when the buffer holds noticeable backlog', () => {
    expect(verdictFor(0.5)).toBe('tight');
    expect(verdictFor(1.4)).toBe('tight');
  });

  it('is slow when the backlog exceeds the late threshold', () => {
    expect(verdictFor(1.5)).toBe('slow');
    expect(verdictFor(9)).toBe('slow');
  });
});
