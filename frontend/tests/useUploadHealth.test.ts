import { describe, it, expect, vi, afterEach } from 'vitest';
import { createApp, defineComponent, nextTick, ref, type Ref } from 'vue';

const { statsMock } = vi.hoisted(() => ({ statsMock: vi.fn() }));
vi.mock('../src/admin/composables/useStreamSocket', () => ({
  getStreamSocketStats: statsMock,
}));

import {
  computeTick,
  verdictFor,
  useUploadHealth,
  CONTAINER_OVERHEAD,
  HISTORY_SECONDS,
} from '../src/admin/composables/useUploadHealth';

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

describe('useUploadHealth (stateful)', () => {
  afterEach(() => {
    vi.useRealTimers();
    statsMock.mockReset();
  });

  /** Mount the composable inside a throwaway component for lifecycle hooks. */
  function mountHealth(active: Ref<boolean>) {
    let health!: ReturnType<typeof useUploadHealth>;
    const app = createApp(
      defineComponent({
        setup() {
          health = useUploadHealth(active, ref(TARGET));
          return () => null;
        },
      })
    );
    app.mount(document.createElement('div'));
    return { health, unmount: () => app.unmount() };
  }

  it('caps history at HISTORY_SECONDS and counts each slow tick as late', () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'performance'] });
    // Steady 27,600 bytes/s queued, 60,000 bytes stuck in the buffer
    // (≈ 2.2 s at the target rate → every tick is "late").
    let queued = 0;
    statsMock.mockImplementation(() => ({
      connected: true,
      bufferedAmount: 60_000,
      bytesQueued: (queued += 27_600),
    }));

    const { health, unmount } = mountHealth(ref(true));
    const ticks = HISTORY_SECONDS + 10;
    vi.advanceTimersByTime(ticks * 1000);

    expect(health.history.value.length).toBe(HISTORY_SECONDS);
    // First tick only seeds prev; every following tick is late.
    expect(health.lateCount.value).toBe(ticks - 1);
    expect(health.verdict.value).toBe('slow');
    // No rttMs in the stats → stays null (pre-#277 socket or no pong yet).
    expect(health.rttMs.value).toBeNull();
    unmount();
  });

  it('passes the socket RTT through on each tick', () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'performance'] });
    let queued = 0;
    statsMock.mockImplementation(() => ({
      connected: true,
      bufferedAmount: 0,
      bytesQueued: (queued += 27_600),
      rttMs: 42,
    }));

    const { health, unmount } = mountHealth(ref(true));
    vi.advanceTimersByTime(2000);
    expect(health.rttMs.value).toBe(42);
    unmount();
  });

  it('skips ticks while disconnected and stops polling on unmount', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'performance'] });
    statsMock.mockImplementation(() => ({
      connected: false,
      bufferedAmount: 0,
      bytesQueued: 0,
    }));

    const active = ref(true);
    const { health, unmount } = mountHealth(active);
    vi.advanceTimersByTime(5000);
    expect(health.history.value).toEqual([]);
    expect(health.uploadKbps.value).toBe(0);

    // Deactivating clears the interval — no further polls.
    active.value = false;
    await nextTick();
    const polls = statsMock.mock.calls.length;
    vi.advanceTimersByTime(5000);
    expect(statsMock.mock.calls.length).toBe(polls);

    // Reactivate, then unmount must also stop the timer.
    active.value = true;
    await nextTick();
    unmount();
    const pollsAfterUnmount = statsMock.mock.calls.length;
    vi.advanceTimersByTime(5000);
    expect(statsMock.mock.calls.length).toBe(pollsAfterUnmount);
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
