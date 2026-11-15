import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, nextTick, ref, type App, type Component } from 'vue';

// The end time never cuts a show: a show set for 12:00–14:00 keeps
// streaming after 14:00 (and past midnight) until the host stops it.

const mocks = vi.hoisted(() => ({
  socket: null as null | Record<string, unknown>,
  capture: null as null | Record<string, unknown>,
  streamStop: vi.fn(() => Promise.resolve()),
  goLive: vi.fn(() => Promise.resolve()),
}));

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }));
for (const path of ['@admin/components/LiveChatCard.vue', '@admin/components/DbMeter.vue']) {
  vi.doMock(path, () => ({ default: { render: () => null } }));
}

vi.mock('@admin/api', () => ({
  streamApi: {
    stop: mocks.streamStop,
    metrics: vi.fn(() => Promise.resolve({ online: true, total_listeners: 3, mounts: [] })),
    status: vi.fn(() => Promise.resolve({ active: true, user: null, recording: false })),
  },
  recordingApi: {
    status: vi.fn(() => Promise.resolve({ is_recording: true, show_id: 7 })),
  },
  hostFlowApi: { goLive: mocks.goLive },
}));

vi.mock('@admin/composables', () => ({
  useHostFlow: () => ({
    show: ref({
      id: 7,
      title: 'Brunchtime',
      date: '2026-11-15',
      start_time: '12:00',
      end_time: '14:00',
    }),
    showId: ref(7),
    recordStream: ref(true),
    setShowStarted: vi.fn(),
    reset: vi.fn(),
    stopStreamAndRevert: vi.fn(),
    // The test clock runs in UTC.
    berlinToUtcDate: (date: string, time: string) => new Date(`${date}T${time}:00Z`),
  }),
  useAudioCapture: () => {
    mocks.capture = {
      mediaStream: ref(null),
      processedStream: ref(null),
      streamBitsPerSecond: ref(128000),
      setOnError: vi.fn(),
      stopCapture: vi.fn(),
      setStreamBitsPerSecond: vi.fn(),
    };
    return mocks.capture;
  },
  useStreamSocket: () => {
    mocks.socket = { state: ref('live'), audioDropped: ref(false), stopStream: vi.fn() };
    return mocks.socket;
  },
  useUploadHealth: () => ({ bufferSeconds: ref(0), ticks: ref(0), verdict: ref('good') }),
  useAutoBitrate: vi.fn(),
}));

let app: App | null = null;
let root: HTMLElement;

async function mountPage(page: Component) {
  root = document.createElement('div');
  document.body.appendChild(root);
  app = createApp(page);
  app.mount(root);
  await vi.advanceTimersByTimeAsync(0);
  await nextTick();
}

describe('On Air at the end time', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-11-15T13:59:50Z'));
    mocks.streamStop.mockClear();
    mocks.goLive.mockClear();
  });

  afterEach(() => {
    app?.unmount();
    app = null;
    root?.remove();
    vi.useRealTimers();
    vi.resetModules();
  });

  it('keeps a live show on air past its end time', async () => {
    const { default: StudioOnAir } = await import('../pages/flow/StudioOnAir.vue');
    await mountPage(StudioOnAir);

    // 14:00 passes, then another ten minutes.
    await vi.advanceTimersByTimeAsync(10 * 60 * 1000 + 10_000);
    await nextTick();

    expect(mocks.socket?.stopStream).not.toHaveBeenCalled();
    expect(mocks.capture?.stopCapture).not.toHaveBeenCalled();
    expect(root.textContent).toContain('On air · 00:10:10 · REC');
    expect(root.textContent).toContain('End Episode');
  });

  it('keeps a prerecorded show on air past its end time', async () => {
    const { default: FlowOnAir } = await import('../pages/flow/FlowOnAir.vue');
    // The start time has passed, so the set starts on mount.
    await mountPage(FlowOnAir);
    expect(mocks.goLive).toHaveBeenCalledOnce();

    await vi.advanceTimersByTimeAsync(10 * 60 * 1000 + 10_000);
    await nextTick();

    expect(mocks.streamStop).not.toHaveBeenCalled();
    expect(root.textContent).not.toContain('Stream Ended');
    // The "Ends in" clock is gone once the end time has passed.
    expect(root.textContent).not.toContain('Ends in');
  });
});
