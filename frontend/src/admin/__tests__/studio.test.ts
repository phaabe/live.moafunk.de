import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import {
  createApp,
  defineComponent,
  h,
  nextTick,
  ref,
  type App,
  type Component,
  type Ref,
} from 'vue';

// Flow screens `studio` (/stream/live) and `studio-onair` (/stream/on-air)
// for a live episode: a manual Go Live, then a lean On Air.

const mocks = vi.hoisted(() => ({
  push: vi.fn(),
  connect: vi.fn(),
  stopStream: vi.fn(),
  options: {} as { onLive?: () => void; onError?: (message: string) => void },
  passed: null as null | { value: boolean },
  record: null as null | { value: boolean },
  socketState: null as null | { value: string },
  audioDropped: null as null | { value: boolean },
  verdict: null as null | { value: string },
  capture: {} as Record<string, unknown>,
  /** The recorder's onError, as the live session gave it (ownCaptureCallbacks). */
  captureOnError: null as null | ((message: string) => void),
  flowReset: vi.fn(),
  recording: { is_recording: true, show_id: 7 },
  // On Air's live panel (#1053).
  listeners: 37 as number | null,
  /** L and R meters of the sent audio. */
  meters: [] as {
    db: Ref<number>;
    peakDb: Ref<number>;
    level: Ref<number>;
    running: Ref<boolean>;
  }[],
  socketStats: {} as Record<string, unknown>,
  chat: null as null | { messages: Ref<unknown[]>; connected: Ref<boolean> },
  health: {} as Record<string, Ref<unknown>>,
}));

vi.mock('vue-router', () => ({ useRouter: () => ({ push: mocks.push }) }));
vi.mock('@admin/components/LiveSetupTest.vue', () => ({
  __esModule: true,
  default: { render: () => h('div', 'Audio input') },
}));
vi.mock('@admin/components/DbMeter.vue', () => ({
  __esModule: true,
  default: { props: ['label'], render: (p: { label: string }) => h('div', p.label) },
}));
vi.mock('@admin/api', () => ({
  streamApi: {
    metrics: vi.fn(() =>
      Promise.resolve({
        online: mocks.listeners !== null,
        total_listeners: mocks.listeners ?? 0,
        mounts: [],
      })
    ),
  },
  recordingApi: { status: vi.fn(() => Promise.resolve(mocks.recording)) },
}));
// The public stream: On Air offers "What listeners hear".
vi.mock('@/config', () => ({
  config: { stream: { icecast: 'https://stream.example/live.mp3', icecastTestUrl: '' } },
}));

// Refs are made fresh in beforeEach; the stubs read them when a page mounts.
vi.mock('@admin/composables', async () => {
  const meter = await vi.importActual<typeof import('../composables/useDbMeter')>(
    '../composables/useDbMeter'
  );
  return {
    dbToLevel: meter.dbToLevel,
    DB_FLOOR: meter.DB_FLOOR,
    useDbMeter: (_stream: unknown, channel?: 0 | 1) => mocks.meters[channel ?? 0],
    useSpectrum: () => ({ bands: ref(new Array(24).fill(0)) }),
    getStreamSocketStats: () => mocks.socketStats,
    QUALITY_STEPS_KBPS: [320, 192, 128, 96],
    useLiveChat: () => ({ ...mocks.chat!, error: ref(null), send: vi.fn(() => true) }),
    BROWSER_UNSUPPORTED_MESSAGE: 'unsupported',
    DEVICE_ENDED_MESSAGE: 'Audio device is no longer active. Re-select your device.',
    STREAM_AUDIO_BITS_PER_SECOND: 128000,
    streamRecordingMimeType: () => 'audio/webm',
    useHostFlow: () => ({
      show: ref({
        id: 7,
        title: 'Brunchtime',
        date: '2026-11-15',
        start_time: '12:00',
        end_time: '14:00',
      }),
      showId: ref(7),
      liveTestPassed: mocks.passed,
      recordStream: mocks.record,
      setRecordStream: (value: boolean) => (mocks.record!.value = value),
      setShowStarted: vi.fn(),
      reset: mocks.flowReset,
      // The test clock runs in UTC.
      berlinToUtcDate: (date: string, time: string) => new Date(`${date}T${time}:00Z`),
    }),
    useAudioCapture: () => mocks.capture,
    // The live session takes the callbacks; the stubs keep them for the tests.
    ownStreamCallbacks: (callbacks: typeof mocks.options | null) => {
      if (callbacks) mocks.options = callbacks;
    },
    ownCaptureCallbacks: (callbacks: { onError: (message: string) => void } | null) => {
      if (callbacks) mocks.captureOnError = callbacks.onError;
    },
    streamShowId: () => 7,
    useStreamSocket: (options: typeof mocks.options = {}) => {
      mocks.options = options;
      return {
        state: mocks.socketState,
        audioDropped: mocks.audioDropped,
        connect: mocks.connect,
        send: vi.fn(),
        stopStream: mocks.stopStream,
      };
    },
    useUploadHealth: () => ({ ...mocks.health, verdict: mocks.verdict }),
    useAutoBitrate: vi.fn(),
  };
});

import { expectFlow } from '../flow/expectFlow';

let app: App | null = null;
let root: HTMLElement;

async function mountPage(page: Component) {
  root = document.createElement('div');
  document.body.appendChild(root);
  app = createApp(page);
  app.component(
    'RouterLink',
    defineComponent({
      props: { to: { type: String, required: true } },
      setup(props, { slots }) {
        return () => h('a', { 'data-to': props.to }, slots.default?.());
      },
    })
  );
  app.mount(root);
  await vi.advanceTimersByTimeAsync(0);
  await nextTick();
}

const text = () => root.textContent ?? '';
const goLiveButton = () =>
  [...root.querySelectorAll('button')].find((b) => b.textContent?.includes('Go Live'))!;

beforeEach(() => {
  vi.useFakeTimers();
  mocks.push.mockReset();
  mocks.connect.mockReset().mockResolvedValue(undefined);
  mocks.stopStream.mockReset();
  mocks.flowReset.mockReset();
  mocks.passed = ref(false);
  mocks.record = ref(true);
  mocks.socketState = ref('live');
  mocks.audioDropped = ref(false);
  mocks.verdict = ref('good');
  mocks.recording = { is_recording: true, show_id: 7 };
  mocks.capture = {
    isCapturing: ref(true),
    error: ref(null),
    mediaStream: ref(null),
    processedStream: ref(null),
    streamBitsPerSecond: ref(128000),
    resumeAudioContext: vi.fn(),
    setOnError: vi.fn(),
    setOnData: vi.fn(),
    startRecording: vi.fn(() => true),
    stopRecording: vi.fn(),
    stopRecordingAndFlush: vi.fn(() => Promise.resolve()),
    stopCapture: vi.fn(),
    setStreamBitsPerSecond: vi.fn(),
    devices: ref([
      { deviceId: 'mic-1', label: 'USB Audio Interface' },
      { deviceId: 'mic-2', label: 'Focusrite USB' },
    ]),
    selectedDeviceId: ref('mic-1'),
    analyserNode: ref(null),
    inputVolume: ref(1),
    setInputVolume: vi.fn(),
    listDevices: vi.fn(),
    captureDevice: vi.fn(() => Promise.resolve(true)),
    captureScreenAudio: vi.fn(() => Promise.resolve(true)),
  };
  mocks.listeners = 37;
  // A good level on both sides.
  mocks.meters = [0, 1].map(() => ({
    db: ref(-18),
    peakDb: ref(-6),
    level: ref(0.7),
    running: ref(true),
  }));
  // The server confirmed all the audio sent so far.
  mocks.socketStats = {
    connected: true,
    bufferedAmount: 0,
    bytesQueued: 4_000_000,
    rttMs: 48,
    serverChunks: 2531,
    serverBytes: 3_990_000,
    serverLate: 0,
  };
  mocks.chat = { messages: ref([]), connected: ref(true) };
  mocks.health = {
    uploadKbps: ref(150),
    bufferSeconds: ref(0.2),
    rttMs: ref(48),
    history: ref([140, 150, 148]),
    ticks: ref(0),
  };
});

const DEVICE_ENDED = 'Audio device is no longer active. Re-select your device.';

afterEach(() => {
  app?.unmount();
  app = null;
  root?.remove();
  vi.useRealTimers();
  vi.resetModules();
});

describe('Studio', () => {
  async function mountStudio(at: string) {
    vi.setSystemTime(new Date(at));
    const { default: FlowLive } = await import('../pages/flow/FlowLive.vue');
    await mountPage(FlowLive);
  }

  it('shows the soundcheck and the countdown before the start, no Go Live and no record choice', async () => {
    await mountStudio('2026-11-15T11:41:18Z');
    for (const part of [
      '< Episode',
      'Studio',
      'Soundcheck · show starts in 18:42',
      'Audio input',
    ]) {
      expect(text()).toContain(part);
    }
    expect(root.querySelector('a')!.getAttribute('data-to')).toBe('/shows/7');
    expect(text()).not.toContain('Record this show');

    mocks.passed!.value = true;
    await nextTick();
    expect(goLiveButton()).toBeUndefined();
  });

  it('is open hours and days before the start: the pill counts hours, then names the day', async () => {
    await mountStudio('2026-11-15T09:41:18Z');
    expect(text()).toContain('Soundcheck · show starts in 2:18:42');
    app!.unmount();
    root.remove();
    await mountStudio('2026-11-13T15:00:00Z');
    expect(text()).toContain('Soundcheck · show starts So 15.11. · 12:00');
    // Go Live comes at the start, from the countdown.
    expect(goLiveButton()).toBeUndefined();
  });

  it('turns the countdown into Go Live at the start; it blinks once the soundcheck is confirmed', async () => {
    await mountStudio('2026-11-15T11:59:58Z');
    await vi.advanceTimersByTimeAsync(2000);
    expect(text()).not.toContain('show starts in');
    expect(text()).toContain('It is air time · confirm the soundcheck first');
    expect(goLiveButton().disabled).toBe(true);
    expect(goLiveButton().classList.contains('ready')).toBe(false);

    mocks.passed!.value = true;
    await nextTick();
    expect(goLiveButton().disabled).toBe(false);
    expect(goLiveButton().classList.contains('ready')).toBe(true);
    expect(text()).toContain('It is air time · start your show');
  });

  it('never goes live by itself', async () => {
    mocks.passed!.value = true;
    await mountStudio('2026-11-15T11:59:00Z');
    await vi.advanceTimersByTimeAsync(5 * 60 * 1000);
    expect(mocks.connect).not.toHaveBeenCalled();
  });

  it('goes live, always recorded, then opens On Air', async () => {
    mocks.passed!.value = true;
    await mountStudio('2026-11-15T12:00:30Z');

    goLiveButton().click();
    await vi.advanceTimersByTimeAsync(0);
    expect(mocks.connect).toHaveBeenCalledWith(false, 7, false, true);
    expect(mocks.capture.startRecording).toHaveBeenCalled();
    expect(mocks.push).not.toHaveBeenCalled();

    mocks.options.onLive!();
    expect(mocks.push).toHaveBeenCalledWith({ path: '/stream/on-air', query: { show: '7' } });
  });

  it('stays on the Studio and shows the error when Go Live fails', async () => {
    mocks.passed!.value = true;
    mocks.connect.mockRejectedValue(new Error('Connection error'));
    await mountStudio('2026-11-15T12:00:30Z');

    goLiveButton().click();
    await vi.advanceTimersByTimeAsync(0);
    await nextTick();
    expect(text()).toContain('Connection error');
    expect(mocks.push).not.toHaveBeenCalled();
    expect(goLiveButton().disabled).toBe(false);
  });

  it('stays on the Studio when the input was unplugged before Go Live', async () => {
    mocks.passed!.value = true;
    mocks.capture.startRecording = vi.fn(() => {
      (mocks.capture.error as { value: string }).value = DEVICE_ENDED;
      return false;
    });
    await mountStudio('2026-11-15T12:00:30Z');

    goLiveButton().click();
    await vi.advanceTimersByTimeAsync(0);
    await nextTick();
    expect(text()).toContain(DEVICE_ENDED);
    expect(mocks.stopStream).toHaveBeenCalled();
    expect(mocks.push).not.toHaveBeenCalled();
  });
});

describe('On Air', () => {
  async function mountOnAir(at = '2026-11-15T12:42:10Z') {
    vi.setSystemTime(new Date(at));
    const { default: StudioOnAir } = await import('../pages/flow/StudioOnAir.vue');
    await mountPage(StudioOnAir);
  }

  /** A card of the live panel, by its title. */
  const card = (title: string) =>
    [...root.querySelectorAll('section')].find(
      (s) => s.querySelector('h2')?.textContent?.trim() === title
    )!;
  const cardStatus = (title: string) =>
    card(title).querySelector('.panel-status')?.textContent?.trim();
  const bar = () => root.querySelector('.bar')!;
  const button = (label: string) =>
    [...root.querySelectorAll('button')].find(
      (b) => (b.getAttribute('aria-label') ?? b.textContent?.trim()) === label
    );

  it('shows the live panel: status bar, audio, connection, listeners, chat, moments', async () => {
    await mountOnAir();
    await vi.advanceTimersByTimeAsync(3000);
    expect(root.querySelector('h1')!.textContent).toBe('Studio');
    // The flow's pill, read as one line.
    expect(root.querySelector('.pill')!.textContent).toBe('On air · 00:00:03 · REC');
    for (const part of ['Brunchtime', '12:00–14:00', 'ends in 1:17:47', 'End Episode']) {
      expect(bar().textContent).toContain(part);
    }
    expect(root.querySelector('.progress')!.getAttribute('aria-label')).toBe(
      'Show progress 35 percent'
    );
    expect([...root.querySelectorAll('h2')].map((h) => h.textContent?.trim())).toEqual([
      'Audio',
      'Connection',
      'Listeners',
      'Live chat',
      'Moments',
    ]);
    expect(cardStatus('Audio')).toBe('Good level');
    expect(card('Audio').textContent).toContain('Input USB Audio Interface');
    expect(card('Audio').textContent).toContain('What listeners hear');
    expect(cardStatus('Connection')).toBe('Stable');
    expect(cardStatus('Live chat')).toBe('Telegram');
    expect(card('Listeners').textContent).toContain('Listeners now37');
    expect(bar().textContent).toContain('37 listening');
    // No problem: no notice, no alarm, no bar warning.
    expect(root.querySelector('[role="alert"]')).toBeNull();
    expect(root.querySelector('.bar-alert')).toBeNull();
    for (const gone of ['Reconnect', 'Stream Ended', 'Stop', 'End the episode?']) {
      expect(text()).not.toContain(gone);
    }
    expect(root.querySelector('select')).toBeNull();
  });

  it('hides the on-air bar while it is on screen (#1063)', async () => {
    await mountOnAir();
    const { useLiveSession } = await import('../liveSession');
    const { panelShown, session } = useLiveSession();
    expect(session.value?.showId).toBe(7);
    expect(panelShown.value).toBe(true);
    // Leaving On Air keeps the session: the bar shows on the next page.
    app!.unmount();
    app = null;
    expect(panelShown.value).toBe(false);
    expect(session.value?.showId).toBe(7);
  });

  it('lets the on-air bar show while the episode page shows another tab', async () => {
    vi.setSystemTime(new Date('2026-11-15T12:42:10Z'));
    const { default: StudioOnAir } = await import('../pages/flow/StudioOnAir.vue');
    const shown = ref(false);
    await mountPage(
      defineComponent(() => () => h(StudioOnAir, { embedded: true, shown: shown.value }))
    );
    const { panelShown } = (await import('../liveSession')).useLiveSession();
    expect(panelShown.value).toBe(false);
    shown.value = true;
    await nextTick();
    expect(panelShown.value).toBe(true);
  });

  it('shows REC only while this episode records', async () => {
    mocks.recording = { is_recording: false, show_id: 7 };
    await mountOnAir();
    expect(root.querySelector('.pill')!.textContent).toBe('On air · 00:00:00');
    expect(text()).not.toContain('REC');
  });

  it('keeps the screen on a drop and shows the connection state', async () => {
    await mountOnAir();
    mocks.socketState!.value = 'connecting';
    await vi.advanceTimersByTimeAsync(4000);
    expect(cardStatus('Connection')).toBe('Reconnecting…');
    expect(bar().querySelector('[role="status"]')!.textContent).toContain('Reconnecting · 4 s');
    mocks.socketState!.value = 'error';
    await nextTick();
    expect(cardStatus('Connection')).toBe('Lost');
    expect(bar().textContent).toContain('Connection lost');
    expect(mocks.push).not.toHaveBeenCalled();
  });

  it('warns when the upload is too slow and says when Auto stepped down', async () => {
    mocks.verdict!.value = 'slow';
    await mountOnAir();
    expect(cardStatus('Connection')).toBe('Too slow');
    expect(card('Connection').classList).toContain('alert');

    // Auto's switch, as useAutoBitrate reports it.
    const { useAutoBitrate } = await import('@admin/composables');
    const options = (useAutoBitrate as Mock).mock.calls.at(-1)![0];
    options.onChange(96);
    await nextTick();
    expect(mocks.capture.setStreamBitsPerSecond).toHaveBeenCalledWith(96000);
    expect(card('Connection').textContent).toContain('Auto stepped down from 128 to 96 kbps');
  });

  it('lets the host pick a fixed upload quality, and Auto again', async () => {
    await mountOnAir();
    expect(button('Auto · 128')!.getAttribute('aria-pressed')).toBe('true');
    button('96')!.click();
    await nextTick();
    expect(mocks.capture.setStreamBitsPerSecond).toHaveBeenCalledWith(96000);
    expect(button('96')!.getAttribute('aria-pressed')).toBe('true');
    button('Auto · 128')!.click();
    await nextTick();
    expect(button('Auto · 128')!.getAttribute('aria-pressed')).toBe('true');
    expect(mocks.capture.setStreamBitsPerSecond).toHaveBeenCalledOnce();
  });

  describe('chunks', () => {
    const chunks = () =>
      [...card('Connection').querySelectorAll('dl > div')]
        .find((d) => d.querySelector('dt')?.textContent === 'Chunks')!
        .querySelector('dd')!
        .textContent!.replace(/\s+/g, ' ')
        .trim();

    it('shows the chunks the server confirmed', async () => {
      await mountOnAir();
      expect(chunks()).toBe('2531 confirmed · all arrived');
    });

    it('says when sent audio does not arrive', async () => {
      await mountOnAir();
      // 10 s of 128 kbps (with the container) sent, not confirmed.
      mocks.socketStats = {
        ...mocks.socketStats,
        bytesQueued: 3_990_000 + 184_000,
      };
      await vi.advanceTimersByTimeAsync(1000);
      expect(chunks()).toBe('2531 confirmed · 10 s on the way');
    });

    it('waits for the first pong', async () => {
      mocks.socketStats = {
        ...mocks.socketStats,
        serverChunks: 0,
        serverBytes: 0,
      };
      await mountOnAir();
      expect(chunks()).toBe('— waiting for the server');
    });
  });

  describe('silence alarm', () => {
    const silent = () => mocks.meters.forEach((m) => (m.db.value = -60));

    it('warns after 10 s without sound, and stops when the sound is back', async () => {
      await mountOnAir();
      silent();
      await vi.advanceTimersByTimeAsync(9000);
      expect(root.querySelector('[role="alert"]')).toBeNull();
      expect(cardStatus('Audio')).toBe('No sound · 8 s');

      await vi.advanceTimersByTimeAsync(1500);
      const alarm = card('Audio').querySelector('[role="alert"]')!;
      expect(alarm.textContent).toContain('No sound for 10 s.');
      expect(alarm.textContent).toContain('Listeners hear silence.');
      expect(cardStatus('Audio')).toBe('No sound · 10 s');
      expect(card('Audio').classList).toContain('alert');

      mocks.meters[1].db.value = -20;
      await vi.advanceTimersByTimeAsync(500);
      expect(root.querySelector('[role="alert"]')).toBeNull();
      expect(cardStatus('Audio')).toBe('Good level');
    });

    it('does not count a short gap between two tracks', async () => {
      await mountOnAir();
      silent();
      await vi.advanceTimersByTimeAsync(2000);
      expect(cardStatus('Audio')).toBe('Good level');
      mocks.meters[0].db.value = -18;
      await vi.advanceTimersByTimeAsync(500);
      silent();
      await vi.advanceTimersByTimeAsync(9500);
      expect(root.querySelector('[role="alert"]')).toBeNull();
    });

    it('stays quiet while the meter cannot read the audio', async () => {
      mocks.meters.forEach((m) => (m.running.value = false));
      await mountOnAir();
      silent();
      await vi.advanceTimersByTimeAsync(30_000);
      expect(root.querySelector('[role="alert"]')).toBeNull();
      expect(cardStatus('Audio')).toBe('Not measured');
    });

    it('leaves a gone input to its own notice', async () => {
      await mountOnAir();
      silent();
      mocks.captureOnError!(DEVICE_ENDED);
      await vi.advanceTimersByTimeAsync(15_000);
      expect([...root.querySelectorAll('[role="alert"]')].map((a) => a.textContent)).toEqual([
        DEVICE_ENDED,
      ]);
      expect(cardStatus('Audio')).toBe('Input gone');
    });
  });

  it('shows the listener peak and keeps it across a remount', async () => {
    await mountOnAir();
    const figures = () =>
      [...card('Listeners').querySelectorAll('dl > div')].map((d) =>
        [...d.children].map((c) => c.textContent).join(' | ')
      );
    expect(figures()).toEqual(['Listeners now | 37', 'Peak | 37']);
    mocks.listeners = 41;
    await vi.advanceTimersByTimeAsync(10_000);
    mocks.listeners = 30;
    await vi.advanceTimersByTimeAsync(10_000);
    expect(figures()).toEqual(['Listeners now | 30', 'Peak | 41']);
    expect(card('Listeners').textContent).toContain('Peak at');

    // Leaving On Air does not end the broadcast; the history stays.
    app!.unmount();
    root.remove();
    const { default: StudioOnAir } = await import('../pages/flow/StudioOnAir.vue');
    await mountPage(StudioOnAir);
    expect(figures()).toEqual(['Listeners now | 30', 'Peak | 41']);
  });

  it('shows no count while the stream server cannot say', async () => {
    mocks.listeners = null;
    await mountOnAir();
    expect(card('Listeners').querySelector('dd')!.textContent).toBe('—');
    expect(bar().textContent).not.toContain('listening');
  });

  describe('Mark moment', () => {
    const momentRows = () =>
      [...card('Moments').querySelectorAll('li')].map((li) =>
        [...li.children].map((c) => c.textContent).join(' | ')
      );
    const press = (target: EventTarget, init: KeyboardEventInit = {}) =>
      target.dispatchEvent(new KeyboardEvent('keydown', { key: 'm', bubbles: true, ...init }));

    it('keeps the on-air time with the button or the M key', async () => {
      await mountOnAir();
      await vi.advanceTimersByTimeAsync(5000);
      button('Mark moment')!.click();
      await nextTick();
      await vi.advanceTimersByTimeAsync(7000);
      press(document.body);
      await nextTick();
      expect(momentRows()).toEqual(['00:00:05 | Moment 1', '00:00:12 | Moment 2']);
      expect(cardStatus('Moments')).toBe('2 marked');
      expect(button('Mark moment')!.getAttribute('aria-keyshortcuts')).toBe('M');
    });

    it('does not mark while the host types a chat reply', async () => {
      await mountOnAir();
      const reply = card('Live chat').querySelector('input')!;
      press(reply);
      press(document.body, { ctrlKey: true });
      press(document.body, { repeat: true });
      await nextTick();
      expect(momentRows()).toEqual([]);
    });

    it('listens for M only while On Air is open', async () => {
      const add = vi.spyOn(window, 'addEventListener');
      const remove = vi.spyOn(window, 'removeEventListener');
      await mountOnAir();
      const handler = add.mock.calls.find(([type]) => type === 'keydown')![1];
      expect(remove).not.toHaveBeenCalledWith('keydown', handler);
      app!.unmount();
      app = null;
      expect(remove).toHaveBeenCalledWith('keydown', handler);
    });
  });

  describe('Screen stays on', () => {
    interface FakeLock {
      released: boolean;
      release: Mock;
      addEventListener: (type: string, cb: () => void) => void;
      fire: () => void;
    }
    let locks: FakeLock[];
    function fakeLock(): FakeLock {
      let onRelease = () => {};
      const lock: FakeLock = {
        released: false,
        release: vi.fn(async () => {
          lock.released = true;
        }),
        addEventListener: (_type, cb) => (onRelease = cb),
        fire: () => {
          lock.released = true;
          onRelease();
        },
      };
      locks.push(lock);
      return lock;
    }
    const chip = () => bar().querySelector('.wake-chip');

    beforeEach(() => {
      locks = [];
    });
    afterEach(() => {
      delete (navigator as { wakeLock?: unknown }).wakeLock;
    });

    it('keeps the screen on while On Air is open, and asks again when the tab is back', async () => {
      Object.defineProperty(navigator, 'wakeLock', {
        configurable: true,
        value: { request: vi.fn(async () => fakeLock()) },
      });
      await mountOnAir();
      expect(chip()!.textContent).toBe('Screen stays on');

      // The browser lets go when the tab is hidden.
      locks[0].fire();
      await nextTick();
      expect(chip()).toBeNull();
      document.dispatchEvent(new Event('visibilitychange'));
      await vi.advanceTimersByTimeAsync(0);
      expect(locks).toHaveLength(2);
      expect(chip()).not.toBeNull();

      // Leaving On Air keeps it: the show is still on air (#1063).
      app!.unmount();
      app = null;
      expect(locks[1].release).not.toHaveBeenCalled();
    });

    it('shows nothing when the browser has no wake lock or refuses it', async () => {
      await mountOnAir();
      expect(chip()).toBeNull();
      app!.unmount();
      root.remove();

      Object.defineProperty(navigator, 'wakeLock', {
        configurable: true,
        value: {
          request: vi.fn(() => Promise.reject(new Error('NotAllowedError'))),
        },
      });
      vi.resetModules();
      await mountOnAir();
      expect(chip()).toBeNull();
    });
  });

  it('keeps the screen on a drop: reconnecting warns in the bar, nothing ends', async () => {
    await mountOnAir();
    mocks.socketState!.value = 'connecting';
    await nextTick();
    expect(bar().classList).toContain('warn');
    expect(bar().textContent).toContain('Your audio waits in this browser');
    mocks.socketState!.value = 'live';
    await nextTick();
    expect(bar().classList).not.toContain('warn');
    expect(cardStatus('Connection')).toBe('Stable');
    expect(root.querySelector('[role="alert"]')).toBeNull();
  });

  it('warns when a long drop cost recorded audio', async () => {
    await mountOnAir();
    expect(text()).not.toContain('misses some audio');
    mocks.audioDropped!.value = true;
    await nextTick();
    expect(text()).toContain('The recording misses some audio.');
  });

  it('keeps the 10-minute notice after the reconnect', async () => {
    mocks.socketState!.value = 'connecting';
    mocks.audioDropped!.value = true;
    await mountOnAir();
    mocks.socketState!.value = 'live';
    await nextTick();
    expect(cardStatus('Connection')).toBe('Stable');
    expect(text()).toContain('The recording misses some audio.');
  });

  it('shows the server’s reason when the connection is lost', async () => {
    await mountOnAir();
    mocks.socketState!.value = 'disconnected';
    mocks.options.onError!('Another connection took over the stream');
    await nextTick();
    expect(cardStatus('Connection')).toBe('Lost');
    expect(text()).toContain('Another connection took over the stream');
    expect(mocks.push).not.toHaveBeenCalled();
  });

  it('sets the input gain', async () => {
    await mountOnAir();
    const gain = [...card('Audio').querySelectorAll('label')]
      .find((l) => l.textContent?.includes('Input gain'))!
      .querySelector('input')!;
    expect(gain.value).toBe('100');
    gain.value = '80';
    gain.dispatchEvent(new Event('input'));
    expect(mocks.capture.setInputVolume).toHaveBeenCalledWith(0.8);
  });

  describe('when the audio input is gone', () => {
    /** What useAudioCapture calls when the device's track ends on air. */
    const captureError = (message: string) => mocks.captureOnError!(message);
    const inputSelect = () => root.querySelector('select');
    async function pick(value: string) {
      const select = inputSelect()!;
      select.value = value;
      select.dispatchEvent(new Event('change'));
      await vi.advanceTimersByTimeAsync(0);
      await nextTick();
    }

    it('warns and lets the host pick an input again', async () => {
      await mountOnAir();
      expect(text()).not.toContain(DEVICE_ENDED);
      expect(inputSelect()).toBeNull();

      captureError(DEVICE_ENDED);
      await nextTick();
      expect(text()).toContain(DEVICE_ENDED);
      expect(card('Audio').querySelector('.input-field')!.textContent).toContain('Audio input');
      expect(mocks.capture.listDevices).toHaveBeenCalled();
      expect(mocks.push).not.toHaveBeenCalled();

      await pick('mic-2');
      expect(mocks.capture.captureDevice).toHaveBeenCalledWith('mic-2');
      expect(mocks.capture.startRecording).toHaveBeenCalledOnce();
      expect(text()).not.toContain(DEVICE_ENDED);
      expect(inputSelect()).toBeNull();
    });

    it('can share screen audio instead', async () => {
      await mountOnAir();
      captureError(DEVICE_ENDED);
      await nextTick();
      await pick('screen');
      expect(mocks.capture.captureScreenAudio).toHaveBeenCalledOnce();
      expect(mocks.capture.captureDevice).not.toHaveBeenCalled();
      expect(text()).not.toContain(DEVICE_ENDED);
    });

    it('keeps the notice and shows why when the picked input fails', async () => {
      (mocks.capture.captureDevice as Mock).mockResolvedValue(false);
      (mocks.capture.error as { value: string | null }).value = 'Could not start audio source';
      await mountOnAir();
      captureError(DEVICE_ENDED);
      await nextTick();
      await pick('mic-2');
      expect(mocks.capture.startRecording).not.toHaveBeenCalled();
      expect(text()).toContain(DEVICE_ENDED);
      expect(text()).toContain('Could not start audio source');

      // The same input again is a new try.
      expect(inputSelect()!.value).toBe('');
      (mocks.capture.captureDevice as Mock).mockResolvedValue(true);
      await pick('mic-2');
      expect(mocks.capture.captureDevice).toHaveBeenCalledTimes(2);
      expect(text()).not.toContain(DEVICE_ENDED);
      expect(text()).not.toContain('Could not start audio source');
    });

    it('keeps the notice when the picked input cannot start', async () => {
      mocks.capture.startRecording = vi.fn(() => {
        captureError(DEVICE_ENDED);
        return false;
      });
      await mountOnAir();
      captureError(DEVICE_ENDED);
      await nextTick();
      await pick('mic-2');
      expect(text()).toContain(DEVICE_ENDED);
      expect(inputSelect()).not.toBeNull();
    });

    it('shows other audio errors without the input picker', async () => {
      await mountOnAir();
      captureError('Recording error');
      await nextTick();
      expect(text()).toContain('Recording error');
      expect(inputSelect()).toBeNull();
    });

    it('lets the host change a working input, and keeps a fixed quality', async () => {
      await mountOnAir();
      button('128')!.click();
      button('Change')!.click();
      await nextTick();
      expect(text()).not.toContain(DEVICE_ENDED);
      expect(mocks.capture.listDevices).toHaveBeenCalled();
      expect(button('Change')).toBeUndefined();

      await pick('mic-2');
      expect(mocks.capture.captureDevice).toHaveBeenCalledWith('mic-2');
      expect(mocks.capture.setStreamBitsPerSecond).toHaveBeenLastCalledWith(128000);
      expect(mocks.capture.startRecording).toHaveBeenCalledOnce();
      expect(inputSelect()).toBeNull();
    });

    it('closes the picker when the host keeps the input', async () => {
      await mountOnAir();
      button('Change')!.click();
      await nextTick();
      button('Keep this input')!.click();
      await nextTick();
      expect(inputSelect()).toBeNull();
      expect(mocks.capture.captureDevice).not.toHaveBeenCalled();
    });
  });

  const press = async (label: string) => {
    [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === label)!.click();
    await vi.advanceTimersByTimeAsync(0);
  };
  const endButtons = () =>
    [...root.querySelectorAll('button')].filter((b) => b.textContent?.trim() === 'End Episode');

  it('End Episode asks once; Keep on air goes back and stops nothing', async () => {
    await mountOnAir();
    await press('End Episode');
    expect(text()).toContain('End the episode?');
    expect(endButtons()).toHaveLength(1);
    expect(mocks.stopStream).not.toHaveBeenCalled();
    await press('Keep on air');
    expect(text()).not.toContain('End the episode?');
    expect(button('Keep on air')).toBeUndefined();
    expect(endButtons()).toHaveLength(1);
    expect(mocks.stopStream).not.toHaveBeenCalled();
    expect(mocks.push).not.toHaveBeenCalled();
  });

  it('End Episode, asked and confirmed, stops the stream and opens the Wrap-up', async () => {
    mocks.socketState!.value = 'error';
    await mountOnAir();
    await press('End Episode');
    expect(mocks.stopStream).not.toHaveBeenCalled();
    await press('End Episode');
    expect(mocks.stopStream).toHaveBeenCalledOnce();
    expect(mocks.capture.stopCapture).toHaveBeenCalledOnce();
    expect(mocks.push).toHaveBeenCalledWith({
      path: '/shows/7',
      state: { episodeEnded: 7 },
    });
  });

  it('End Episode sends the last recorder chunk before it stops the stream', async () => {
    let flush!: () => void;
    (mocks.capture.stopRecordingAndFlush as Mock).mockReturnValue(
      new Promise<void>((r) => (flush = r))
    );
    await mountOnAir();
    await press('End Episode');
    await press('End Episode');
    expect(mocks.capture.stopRecordingAndFlush).toHaveBeenCalledOnce();
    expect(mocks.stopStream).not.toHaveBeenCalled();

    flush();
    await vi.advanceTimersByTimeAsync(0);
    expect(mocks.stopStream).toHaveBeenCalledOnce();
    expect(mocks.capture.stopCapture).toHaveBeenCalledOnce();
  });

  it('End Episode forgets the moments and the listener history', async () => {
    await mountOnAir();
    button('Mark moment')!.click();
    await press('End Episode');
    await press('End Episode');
    app!.unmount();
    root.remove();
    mocks.listeners = 3;
    const { default: StudioOnAir } = await import('../pages/flow/StudioOnAir.vue');
    await mountPage(StudioOnAir);
    expect(card('Moments').querySelectorAll('li')).toHaveLength(0);
    expect(card('Listeners').textContent).toContain('Peak3');
  });
});
// The Studio's own flow checks are in studioSoundcheck.test.ts.
describe('flow', () => {
  // Jump the clock to 42:09, then run one 1 s tick. Running every timer
  // through 42 minutes took over 5 s per test on a busy CI box.
  async function mountOnAirAt4210() {
    vi.setSystemTime(new Date('2026-11-15T12:00:00Z'));
    const { default: StudioOnAir } = await import('../pages/flow/StudioOnAir.vue');
    await mountPage(StudioOnAir);
    vi.setSystemTime(new Date('2026-11-15T12:42:09Z'));
    await vi.advanceTimersByTimeAsync(1000);
  }

  it('Studio · On Air matches the flow', async () => {
    await mountOnAirAt4210();
    await expectFlow('studio-onair', root);
  });

  it('Studio · On Air, reconnecting matches the flow (a drop of more than 10 minutes)', async () => {
    mocks.socketState = ref('connecting');
    mocks.audioDropped = ref(true);
    await mountOnAirAt4210();
    await expectFlow('studio-onair-reconnecting', root);
  });

  it('Studio · On Air, connection lost matches the flow', async () => {
    mocks.socketState = ref('disconnected');
    await mountOnAirAt4210();
    mocks.options.onError!('Another connection took over the stream');
    await nextTick();
    await expectFlow('studio-onair-lost', root);
  });

  it('Studio · On Air, ending matches the flow (End Episode asks once)', async () => {
    await mountOnAirAt4210();
    [...root.querySelectorAll('button')]
      .find((b) => b.textContent?.trim() === 'End Episode')!
      .click();
    await nextTick();
    await expectFlow('studio-onair-end', root);
  });

  it('Studio · On Air, input gone matches the flow', async () => {
    await mountOnAirAt4210();
    mocks.captureOnError!(DEVICE_ENDED);
    await nextTick();
    await expectFlow('studio-onair-noinput', root);
  });
});
