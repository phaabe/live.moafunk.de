import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { createApp, defineComponent, h, nextTick, ref, type App, type Component } from 'vue';

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
  flowReset: vi.fn(),
  recording: { is_recording: true, show_id: 7 },
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
    metrics: vi.fn(() => Promise.resolve({ online: true, total_listeners: 37, mounts: [] })),
  },
  recordingApi: { status: vi.fn(() => Promise.resolve(mocks.recording)) },
}));

// Refs are made fresh in beforeEach; the stubs read them when a page mounts.
vi.mock('@admin/composables', () => {
  return {
    BROWSER_UNSUPPORTED_MESSAGE: 'unsupported',
    DEVICE_ENDED_MESSAGE: 'Audio device is no longer active. Re-select your device.',
    STREAM_AUDIO_BITS_PER_SECOND: 128000,
    streamRecordingMimeType: () => 'audio/webm',
    useHostFlow: () => ({
      show: ref({ id: 7, date: '2026-11-15', start_time: '12:00', end_time: '14:00' }),
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
    useUploadHealth: () => ({ bufferSeconds: ref(0), ticks: ref(0), verdict: mocks.verdict }),
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
const recordBox = () =>
  [...root.querySelectorAll('label')]
    .find((l) => l.textContent?.includes('Record this show'))!
    .querySelector('input')!;

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
    stopCapture: vi.fn(),
    setStreamBitsPerSecond: vi.fn(),
    devices: ref([{ deviceId: 'mic-2', label: 'Focusrite USB' }]),
    listDevices: vi.fn(),
    captureDevice: vi.fn(() => Promise.resolve(true)),
    captureScreenAudio: vi.fn(() => Promise.resolve(true)),
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

  it('shows the soundcheck and keeps Go Live closed before the start', async () => {
    await mountStudio('2026-11-15T11:41:18Z');
    for (const part of [
      '< Episode',
      'Studio',
      'Soundcheck · show starts in 18:42',
      'Audio input',
      'Record this show',
      'Go Live',
    ]) {
      expect(text()).toContain(part);
    }
    expect(root.querySelector('a')!.getAttribute('data-to')).toBe('/shows/7');
    expect(recordBox().checked).toBe(true);

    mocks.passed!.value = true;
    await nextTick();
    expect(goLiveButton().disabled).toBe(true);
  });

  it('opens Go Live at the start once the test sounds right', async () => {
    await mountStudio('2026-11-15T11:59:58Z');
    await vi.advanceTimersByTimeAsync(2000);
    expect(goLiveButton().disabled).toBe(true);

    mocks.passed!.value = true;
    await nextTick();
    expect(goLiveButton().disabled).toBe(false);
  });

  it('never goes live by itself', async () => {
    mocks.passed!.value = true;
    await mountStudio('2026-11-15T11:59:00Z');
    await vi.advanceTimersByTimeAsync(5 * 60 * 1000);
    expect(mocks.connect).not.toHaveBeenCalled();
  });

  it('goes live with the record choice, then opens On Air', async () => {
    mocks.passed!.value = true;
    await mountStudio('2026-11-15T12:00:30Z');
    recordBox().click();
    await nextTick();
    expect(mocks.record!.value).toBe(false);

    goLiveButton().click();
    await vi.advanceTimersByTimeAsync(0);
    expect(mocks.connect).toHaveBeenCalledWith(false, 7, false, false);
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
  async function mountOnAir() {
    vi.setSystemTime(new Date('2026-11-15T12:42:10Z'));
    const { default: StudioOnAir } = await import('../pages/flow/StudioOnAir.vue');
    await mountPage(StudioOnAir);
  }

  it('shows only the flow items', async () => {
    await mountOnAir();
    await vi.advanceTimersByTimeAsync(3000);
    for (const part of [
      'Studio',
      'On air · 00:00:03 · REC',
      'Live level',
      'Listeners now37',
      'Connectionstable',
      'End Episode',
    ]) {
      expect(text()).toContain(part);
    }
    for (const gone of ['Reconnect', 'Stream Ended', 'Ends in', 'chat', 'Moments', 'Stop']) {
      expect(text()).not.toContain(gone);
    }
    expect(root.querySelectorAll('button')).toHaveLength(1);
  });

  it('shows REC only while this episode records', async () => {
    mocks.recording = { is_recording: false, show_id: 7 };
    await mountOnAir();
    expect(text()).toContain('On air · 00:00:00');
    expect(text()).not.toContain('REC');
  });

  it('keeps the screen on a drop and shows the connection state', async () => {
    await mountOnAir();
    mocks.socketState!.value = 'connecting';
    await nextTick();
    expect(text()).toContain('Connectionreconnecting…');
    mocks.socketState!.value = 'error';
    await nextTick();
    expect(text()).toContain('Connectionlost');
    expect(mocks.push).not.toHaveBeenCalled();
  });

  it('warns when a long drop cost recorded audio', async () => {
    await mountOnAir();
    expect(text()).not.toContain('misses some audio');
    mocks.audioDropped!.value = true;
    await nextTick();
    expect(text()).toContain('The recording misses some audio.');
  });

  it('is stable again after a short drop, with no notice', async () => {
    await mountOnAir();
    mocks.socketState!.value = 'connecting';
    await nextTick();
    expect(text()).toContain('Connectionreconnecting…');
    mocks.socketState!.value = 'live';
    await nextTick();
    expect(text()).toContain('Connectionstable');
    expect(root.querySelector('[role="alert"]')).toBeNull();
  });

  it('keeps the 10-minute notice after the reconnect', async () => {
    mocks.socketState!.value = 'connecting';
    mocks.audioDropped!.value = true;
    await mountOnAir();
    mocks.socketState!.value = 'live';
    await nextTick();
    expect(text()).toContain('Connectionstable');
    expect(text()).toContain('The recording misses some audio.');
  });

  it('shows the server’s reason when the connection is lost', async () => {
    await mountOnAir();
    mocks.socketState!.value = 'disconnected';
    mocks.options.onError!('Another connection took over the stream');
    await nextTick();
    expect(text()).toContain('Connectionlost');
    expect(text()).toContain('Another connection took over the stream');
    expect(mocks.push).not.toHaveBeenCalled();
  });

  describe('when the audio input is gone', () => {
    /** What useAudioCapture calls when the device's track ends on air. */
    const captureError = (message: string) =>
      (mocks.capture.setOnError as Mock).mock.calls.at(-1)![0](message);
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
      expect(text()).toContain('Audio input');
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
  });

  const press = async (label: string) => {
    [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === label)!.click();
    await vi.advanceTimersByTimeAsync(0);
  };

  it('End Episode asks once; Keep on air goes back and stops nothing', async () => {
    await mountOnAir();
    await press('End Episode');
    expect(text()).toContain('End the episode?');
    expect(mocks.stopStream).not.toHaveBeenCalled();
    await press('Keep on air');
    expect(text()).not.toContain('End the episode?');
    expect(root.querySelectorAll('button')).toHaveLength(1);
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
    expect(mocks.push).toHaveBeenCalledWith({ path: '/shows/7', state: { episodeEnded: 7 } });
  });
});

// Keep this block last: it swaps in the real LiveSetupTest.
describe('flow', () => {
  afterEach(() => {
    // Unmount before the stubbed navigator goes: LiveSetupTest unhooks from it.
    app?.unmount();
    app = null;
    vi.unstubAllGlobals();
    vi.doMock('@admin/components/LiveSetupTest.vue', () => ({
      __esModule: true,
      default: { render: () => h('div', 'Audio input') },
    }));
  });

  it('Studio matches the flow', async () => {
    // The flow names the audio controls, so check the real ones.
    vi.doUnmock('@admin/components/LiveSetupTest.vue');
    Object.assign(mocks.capture, {
      devices: ref([]),
      selectedDeviceId: ref(''),
      refreshDevices: vi.fn(),
      listDevices: vi.fn(),
    });
    vi.stubGlobal('navigator', {
      ...navigator,
      mediaDevices: { addEventListener: vi.fn(), removeEventListener: vi.fn() },
    });
    mocks.passed = ref(true);
    vi.setSystemTime(new Date('2026-11-15T11:41:18Z'));
    const { default: FlowLive } = await import('../pages/flow/FlowLive.vue');
    await mountPage(FlowLive);
    await expectFlow('studio', root);
  });

  it('Studio · On Air matches the flow', async () => {
    vi.setSystemTime(new Date('2026-11-15T12:00:00Z'));
    const { default: StudioOnAir } = await import('../pages/flow/StudioOnAir.vue');
    await mountPage(StudioOnAir);
    await vi.advanceTimersByTimeAsync((42 * 60 + 10) * 1000);
    await expectFlow('studio-onair', root);
  });

  async function mountOnAirAt4210() {
    vi.setSystemTime(new Date('2026-11-15T12:00:00Z'));
    const { default: StudioOnAir } = await import('../pages/flow/StudioOnAir.vue');
    await mountPage(StudioOnAir);
    await vi.advanceTimersByTimeAsync((42 * 60 + 10) * 1000);
  }

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
    (mocks.capture.setOnError as Mock).mock.calls.at(-1)![0](DEVICE_ENDED);
    await nextTick();
    await expectFlow('studio-onair-noinput', root);
  });
});
