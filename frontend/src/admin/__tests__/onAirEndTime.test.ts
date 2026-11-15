import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, nextTick, ref, type App } from 'vue';

// The end time never cuts a show: a show set for 12:00–14:00 keeps
// streaming after 14:00 (and past midnight) until the host stops it.

const mocks = vi.hoisted(() => ({
  mode: 'live' as 'live' | 'prerecorded',
  socket: null as null | Record<string, unknown>,
  capture: null as null | Record<string, unknown>,
  streamStop: vi.fn(() => Promise.resolve()),
  goLive: vi.fn(() => Promise.resolve()),
}));

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('@/config', () => ({ config: { stream: { icecastTestUrl: '' } } }));
for (const path of [
  '@admin/components/ConnectionCard.vue',
  '@admin/components/LiveChatCard.vue',
  '@admin/components/DbMeter.vue',
  '@admin/components/SpectrumBars.vue',
  '@admin/components/StreamPreviewPlayer.vue',
]) {
  vi.doMock(path, () => ({ default: { render: () => null } }));
}

vi.mock('@admin/api', () => ({
  streamApi: {
    stop: mocks.streamStop,
    metrics: vi.fn(() => Promise.resolve(null)),
    status: vi.fn(() => Promise.resolve({ active: true, user: null, recording: false })),
  },
  recordingApi: {
    start: vi.fn(() => Promise.resolve()),
    stop: vi.fn(() => Promise.resolve()),
    status: vi.fn(() => Promise.resolve({ active: false })),
  },
  hostFlowApi: { goLive: mocks.goLive },
}));

vi.mock('@admin/composables', () => ({
  DB_FLOOR: -60,
  STREAM_AUDIO_BITS_PER_SECOND: 128000,
  BROWSER_UNSUPPORTED_MESSAGE: 'unsupported',
  streamRecordingMimeType: () => 'audio/webm',
  useSpectrum: () => ({ bands: ref([]) }),
  useDbMeter: () => ({ peakDb: ref(-60) }),
  useHostFlow: () => ({
    show: ref({
      id: 7,
      title: 'Brunchtime',
      date: '2026-11-15',
      start_time: '12:00',
      end_time: '14:00',
    }),
    showId: ref(7),
    uploadMode: ref(mocks.mode),
    recordStream: ref(false),
    setShowStarted: vi.fn(),
    setRecordStream: vi.fn(),
    reset: vi.fn(),
    stopStreamAndRevert: vi.fn(),
    // The test clock runs in UTC.
    berlinToUtcDate: (date: string, time: string) => new Date(`${date}T${time}:00Z`),
  }),
  useAudioCapture: () => {
    mocks.capture = {
      isCapturing: ref(true),
      error: ref(null),
      mediaStream: ref(null),
      analyserNode: ref(null),
      processedStream: ref(null),
      inputVolume: ref(1),
      streamBitsPerSecond: ref(128000),
      setOnError: vi.fn(),
      setOnData: vi.fn(),
      startRecording: vi.fn(() => true),
      stopCapture: vi.fn(),
      resumeAudioContext: vi.fn(),
      setInputVolume: vi.fn(),
      setStreamBitsPerSecond: vi.fn(),
    };
    return mocks.capture;
  },
  useStreamSocket: (options: { onLive: () => void }) => {
    mocks.socket = {
      connect: vi.fn(async () => options.onLive()),
      stopStream: vi.fn(),
      send: vi.fn(),
      cleanup: vi.fn(),
      resetReconnect: vi.fn(),
    };
    return mocks.socket;
  },
}));

let app: App | null = null;
let root: HTMLElement;

async function mountOnAir() {
  const { default: FlowOnAir } = await import('../pages/flow/FlowOnAir.vue');
  root = document.createElement('div');
  document.body.appendChild(root);
  app = createApp(FlowOnAir);
  app.mount(root);
  // The start time has passed, so the page goes live on mount.
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

  it.each(['live', 'prerecorded'] as const)(
    'keeps a %s show on air past its end time',
    async (mode) => {
      mocks.mode = mode;
      await mountOnAir();
      if (mode === 'live') expect(mocks.socket?.connect).toHaveBeenCalledOnce();
      else expect(mocks.goLive).toHaveBeenCalledOnce();

      // 14:00 passes, then another ten minutes.
      await vi.advanceTimersByTimeAsync(10 * 60 * 1000 + 10_000);
      await nextTick();

      expect(mocks.streamStop).not.toHaveBeenCalled();
      expect(mocks.socket?.stopStream).not.toHaveBeenCalled();
      if (mode === 'live') expect(mocks.capture?.stopCapture).not.toHaveBeenCalled();
      expect(root.textContent).not.toContain('Stream Ended');
      // The "Ends in" clock is gone once the end time has passed.
      expect(root.textContent).not.toContain('Ends in');
    }
  );
});
