import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { ref, type Ref } from 'vue';

// The live session (#1063) runs without any page: the bar and the live panel
// only show it. The socket, the recorder and the host flow are stubs; the
// upload health and Auto quality are the real ones.

// jsdom does not implement font loading used by the composables barrel.
vi.hoisted(() => {
  vi.stubGlobal(
    'FontFace',
    class {
      load() {
        return Promise.resolve(this);
      }
    }
  );
  Object.defineProperty(document, 'fonts', { value: { add() {} }, configurable: true });
});

const m = vi.hoisted(() => ({
  state: null as null | { value: string },
  audioDropped: null as null | { value: boolean },
  stats: {} as Record<string, unknown>,
  showId: 7 as number | null,
  socketCallbacks: null as null | { onError?: (message: string) => void },
  captureCallbacks: null as null | {
    onData: (data: ArrayBuffer, first: boolean) => void;
    onError: (message: string) => void;
  },
  calls: [] as string[],
  capture: {} as Record<string, unknown>,
  flowReset: vi.fn(),
  saveStudioLog: vi.fn(),
}));

// The panel's values go to the episode (studioLog.ts).
vi.mock('@admin/api', () => ({ showsApi: { saveStudioLog: m.saveStudioLog } }));

vi.mock('../composables/useStreamSocket', () => ({
  useStreamSocket: () => ({
    state: m.state,
    audioDropped: m.audioDropped,
    send: vi.fn(),
    stopStream: vi.fn(() => {
      m.calls.push('stop stream');
      m.showId = null;
    }),
  }),
  getStreamSocketStats: () => m.stats,
  liveStreamShowId: () => m.showId,
  streamShowId: () => m.showId,
  ownStreamCallbacks: (callbacks: typeof m.socketCallbacks) => (m.socketCallbacks = callbacks),
  parsePong: vi.fn(),
}));
vi.mock('../composables/useAudioCapture', () => ({
  useAudioCapture: () => m.capture,
  ownCaptureCallbacks: (callbacks: typeof m.captureCallbacks) => (m.captureCallbacks = callbacks),
  DEVICE_ENDED_MESSAGE: 'Audio device is no longer active. Re-select your device.',
  STREAM_AUDIO_BITS_PER_SECOND: 192000,
  BROWSER_UNSUPPORTED_MESSAGE: 'unsupported',
  streamRecordingMimeType: () => 'audio/webm',
}));
vi.mock('../composables/useHostFlow', () => ({
  useHostFlow: () => ({
    show: ref({ id: 7, title: 'Brunchtime', start_time: '12:00', end_time: '14:00' }),
    reset: m.flowReset,
  }),
}));

const DEVICE_ENDED = 'Audio device is no longer active. Re-select your device.';

type SessionModule = typeof import('../liveSession');
let mod: SessionModule;

interface FakeLock {
  released: boolean;
  release: Mock;
}
let locks: FakeLock[];

beforeEach(async () => {
  vi.useFakeTimers();
  m.state = ref('live');
  m.audioDropped = ref(false);
  m.showId = 7;
  m.calls = [];
  m.socketCallbacks = null;
  m.captureCallbacks = null;
  m.flowReset.mockReset();
  m.saveStudioLog.mockReset().mockResolvedValue(undefined);
  // A good line: nothing waits in the send buffer.
  m.stats = { connected: true, bufferedAmount: 0, bytesQueued: 0, rttMs: 40 };
  const bitsPerSecond: Ref<number> = ref(192000);
  m.capture = {
    streamBitsPerSecond: bitsPerSecond,
    error: ref(null),
    setStreamBitsPerSecond: vi.fn((bps: number) => (bitsPerSecond.value = bps)),
    listDevices: vi.fn(),
    captureDevice: vi.fn(async () => true),
    captureScreenAudio: vi.fn(async () => true),
    startRecording: vi.fn(() => true),
    stopRecordingAndFlush: vi.fn(async () => {
      m.calls.push('flush');
    }),
    stopCapture: vi.fn(() => m.calls.push('stop capture')),
    selectedDeviceId: ref('usb'),
    devices: ref([{ deviceId: 'usb', label: 'USB Mixer' }]),
    inputVolume: ref(1.2),
  };
  locks = [];
  Object.defineProperty(navigator, 'wakeLock', {
    configurable: true,
    value: {
      request: vi.fn(async () => {
        const lock: FakeLock = {
          released: false,
          release: vi.fn(async () => {
            lock.released = true;
          }),
        };
        locks.push(lock);
        return { ...lock, addEventListener: () => {}, release: lock.release };
      }),
    },
  });
  document.title = 'Moafunk Admin';
  // The session is module state; every test gets a fresh module.
  vi.resetModules();
  mod = await import('../liveSession');
});

afterEach(() => {
  delete (navigator as { wakeLock?: unknown }).wakeLock;
  vi.useRealTimers();
});

const start = () => mod.startLiveSession(7, '/shows/7');

describe('live session', () => {
  it('keeps one session per show: a second start keeps its clock and takes the callbacks again', async () => {
    const live = start();
    await vi.advanceTimersByTimeAsync(5000);
    m.socketCallbacks = null;
    m.captureCallbacks = null;

    expect(mod.startLiveSession(7, '/stream/on-air?show=7')).toBe(live);
    expect(live.elapsed.value).toBe('00:00:05');
    expect(live.panelPath).toBe('/shows/7');
    expect(live.title).toBe('Brunchtime');
    expect(live.slot).toBe('12:00–14:00');
    expect(m.socketCallbacks).not.toBeNull();
    expect(m.captureCallbacks).not.toBeNull();
    expect(mod.useLiveSession().session.value).toBe(live);
  });

  it('starts the tab title with "● ON AIR" while live', async () => {
    const live = start();
    expect(document.title).toBe('● ON AIR · Moafunk Admin');
    await live.end();
    expect(document.title).toBe('Moafunk Admin');
  });

  it('holds the screen wake lock without any page, until End Episode', async () => {
    const live = start();
    await vi.advanceTimersByTimeAsync(0);
    expect(locks).toHaveLength(1);
    expect(live.screenOn.value).toBe(true);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(locks[0].release).not.toHaveBeenCalled();

    await live.end();
    expect(locks[0].release).toHaveBeenCalled();
    expect(live.screenOn.value).toBe(false);
  });

  it('keeps the upload health and Auto quality running without any page', async () => {
    const live = start();
    await vi.advanceTimersByTimeAsync(1000);
    // The line got slow: 1.1 s of audio waits in the send buffer.
    for (let i = 1; i <= 6; i++) {
      m.stats = { ...m.stats, bufferedAmount: 30_000, bytesQueued: i * 24_000 };
      await vi.advanceTimersByTimeAsync(1000);
    }
    expect(m.capture.setStreamBitsPerSecond).toHaveBeenCalledWith(128000);
    // At 128 kbps the same backlog is 1.6 s of audio.
    expect(live.health.verdict.value).toBe('slow');
    expect(live.connection.value).toBe('too slow');
    expect(live.lastStep.value).toMatchObject({ from: 192, to: 128 });
  });

  it('shows a gone input and other errors as warnings; a new input clears them', async () => {
    const live = start();
    m.captureCallbacks!.onError(DEVICE_ENDED);
    m.socketCallbacks!.onError!('Another connection took over the stream');
    expect(live.inputGone.value).toBe(true);
    expect(m.capture.listDevices).toHaveBeenCalled();
    expect(live.warnings.value.map((w) => w.text)).toEqual([
      DEVICE_ENDED,
      'Another connection took over the stream',
    ]);

    expect(await live.changeInput('mic-2')).toBe(true);
    expect(m.capture.captureDevice).toHaveBeenCalledWith('mic-2');
    expect(live.inputGone.value).toBe(false);
    expect(live.warnings.value).toEqual([]);
  });

  it('keeps a fixed quality on a new input, and shows why a new input failed', async () => {
    const live = start();
    live.setQuality(96);
    expect(m.capture.setStreamBitsPerSecond).toHaveBeenLastCalledWith(96000);
    m.captureCallbacks!.onError(DEVICE_ENDED);
    expect(await live.changeInput('screen')).toBe(true);
    expect(m.capture.captureScreenAudio).toHaveBeenCalled();
    expect(m.capture.setStreamBitsPerSecond).toHaveBeenLastCalledWith(96000);

    m.captureCallbacks!.onError(DEVICE_ENDED);
    (m.capture.captureDevice as Mock).mockResolvedValueOnce(false);
    (m.capture.error as Ref<string>).value = 'Permission denied';
    expect(await live.changeInput('mic-2')).toBe(false);
    expect(live.inputGone.value).toBe(true);
    expect(live.error.value).toBe('Permission denied');
  });

  it('warns while the connection comes back, when it is lost, and after a long drop', async () => {
    const live = start();
    m.state!.value = 'connecting';
    await vi.advanceTimersByTimeAsync(4000);
    expect(live.warnings.value).toEqual([
      { text: 'Reconnecting · 4 s. Your audio waits in this browser.', urgent: false },
    ]);
    m.state!.value = 'disconnected';
    m.audioDropped!.value = true;
    expect(live.warnings.value).toEqual([
      { text: 'Connection lost. Listeners may hear silence.', urgent: true },
      { text: mod.AUDIO_DROPPED_MESSAGE, urgent: true },
    ]);
    // Lost is not the end: the host still ends the episode.
    await vi.advanceTimersByTimeAsync(5000);
    expect(mod.useLiveSession().session.value).toBe(live);
  });

  it('End Episode sends the last chunk first, stops everything and tells the episode page', async () => {
    const live = start();
    expect(await live.end()).toBe(7);
    expect(m.calls).toEqual(['flush', 'stop stream', 'stop capture']);
    expect(m.flowReset).toHaveBeenCalledOnce();
    const { session, ended } = mod.useLiveSession();
    expect(session.value).toBeNull();
    expect(ended.value).toEqual({ showId: 7 });
    expect(m.socketCallbacks).toBeNull();
    expect(m.captureCallbacks).toBeNull();
  });

  it('saves its values to the episode every 15 s and once more at End Episode', async () => {
    const live = start();
    const saved = () => m.saveStudioLog.mock.calls.map(([id, part]) => [id, part.on_air.ended]);
    expect(saved()).toEqual([[7, false]]);
    expect(m.saveStudioLog.mock.calls[0][1].on_air).toMatchObject({
      input: 'USB Mixer',
      gain: 1.2,
      connection: 'stable',
      quality: 'auto',
      moments: [],
    });
    await vi.advanceTimersByTimeAsync(15_000);
    expect(saved()).toEqual([
      [7, false],
      [7, false],
    ]);
    // A failed save does not stop the broadcast.
    m.saveStudioLog.mockRejectedValueOnce(new Error('offline'));
    await live.end();
    expect(saved().at(-1)).toEqual([7, true]);
    // The session is gone: no more saves.
    await vi.advanceTimersByTimeAsync(30_000);
    expect(saved()).toHaveLength(3);
  });

  it('ends by itself when another page stops the stream', async () => {
    start();
    await vi.advanceTimersByTimeAsync(0);
    m.showId = null;
    await vi.advanceTimersByTimeAsync(1000);
    const { session, ended } = mod.useLiveSession();
    expect(session.value).toBeNull();
    // Not an End Episode: the episode page is not sent to its Recording tab.
    expect(ended.value).toBeNull();
    expect(locks[0].release).toHaveBeenCalled();
    expect(document.title).toBe('Moafunk Admin');
  });

  it('a new show ends the old session first', () => {
    const first = start();
    m.showId = 8;
    const second = mod.startLiveSession(8, '/shows/8');
    expect(second).not.toBe(first);
    expect(second.showId).toBe(8);
    expect(document.title).toBe('● ON AIR · Moafunk Admin');
  });
});
