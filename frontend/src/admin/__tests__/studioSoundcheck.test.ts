import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, defineComponent, h, nextTick, ref, type App, type Ref } from 'vue';

// The Studio's soundcheck (flow screens `studio`, `studio-ready`,
// `studio-checking`, `studio-problem`): the host tests the music and the mic,
// then the server and the return feed are checked and a result is proposed.
// The page is real; the audio level, the socket, the recorder and the return
// feed are fakes that each test sets.

interface FakeRecorder {
  state: string;
  ondataavailable: ((event: { data: unknown }) => void) | null;
}

const mocks = vi.hoisted(() => ({
  push: vi.fn(),
  passed: null as null | Ref<boolean>,
  capturing: null as null | Ref<boolean>,
  // The level of the audio that would be sent.
  db: null as null | Ref<number>,
  peak: null as null | Ref<number>,
  connect: vi.fn(),
  stopStream: vi.fn(),
  socketOptions: {} as { onError?: (message: string) => void },
  // Chunks the server confirmed.
  serverChunks: 0,
  recorder: null as null | FakeRecorder,
  // The return feed: its peak (null: not measurable) and whether it plays.
  feedPeak: -10 as number | null,
  feedPlaying: true,
  setAudible: vi.fn(),
  closeFeed: vi.fn(),
  captureDevice: vi.fn(),
  // GET /api/shows/:id: `on_air` opens Studio · Back on air.
  showsGet: vi.fn(),
}));

vi.mock('vue-router', () => ({ useRouter: () => ({ push: mocks.push }) }));
vi.mock('@admin/api', () => ({ showsApi: { get: mocks.showsGet } }));
vi.mock('@/config', () => ({
  config: { stream: { icecastTestUrl: 'https://test.example/test.mp3' } },
}));
vi.mock('@admin/components/DbMeter.vue', () => ({
  __esModule: true,
  default: { props: ['label'], render: (p: { label: string }) => h('div', p.label) },
}));
vi.mock('@admin/composables', async () => {
  const real = await vi.importActual<typeof import('../composables/useSoundcheck')>(
    '../composables/useSoundcheck'
  );
  return {
    STREAM_AUDIO_BITS_PER_SECOND: 128000,
    BROWSER_UNSUPPORTED_MESSAGE: 'unsupported',
    streamRecordingMimeType: () => 'audio/webm',
    useSoundcheck: real.useSoundcheck,
    createReturnFeed: () => ({
      peakDb: () => mocks.feedPeak,
      playing: () => mocks.feedPlaying,
      setAudible: mocks.setAudible,
      close: mocks.closeFeed,
    }),
    useDbMeter: () => ({ db: mocks.db, peakDb: mocks.peak }),
    getStreamSocketStats: () => ({ serverChunks: mocks.serverChunks }),
    useHostFlow: () => ({
      show: ref({ id: 7, date: '2026-11-15', start_time: '12:00', end_time: '14:00' }),
      liveTestPassed: mocks.passed,
      setLiveTestPassed: (value = true) => (mocks.passed!.value = value),
      recordStream: ref(true),
      setRecordStream: vi.fn(),
      setShowStarted: vi.fn(),
      // The test clock runs in UTC.
      berlinToUtcDate: (date: string, time: string) => new Date(`${date}T${time}:00Z`),
    }),
    useAudioCapture: () => ({
      devices: ref([
        { deviceId: 'usb', label: 'Focusrite USB' },
        { deviceId: 'mic', label: 'Built-in' },
      ]),
      selectedDeviceId: ref('usb'),
      isCapturing: mocks.capturing,
      error: ref(null),
      mediaStream: ref({ getAudioTracks: () => [{ readyState: 'live' }] }),
      processedStream: ref(null),
      refreshDevices: vi.fn(async () => {}),
      listDevices: vi.fn(),
      captureDevice: mocks.captureDevice,
      captureScreenAudio: vi.fn(async () => true),
      resumeAudioContext: vi.fn(),
      setOnError: vi.fn(),
      setOnData: vi.fn(),
      startRecording: vi.fn(() => true),
      stopRecording: vi.fn(),
    }),
    useStreamSocket: (options?: typeof mocks.socketOptions) => {
      if (options) mocks.socketOptions = options;
      return { connect: mocks.connect, stopStream: mocks.stopStream, send: vi.fn() };
    },
    useUploadHealth: () => ({ verdict: ref('good'), verdictText: ref(''), uploadKbps: ref(0) }),
  };
});

import FlowLive from '../pages/flow/FlowLive.vue';
import { expectFlow } from '../flow/expectFlow';

let app: App | null = null;
let root: HTMLElement;

/** Air time is 12:00 UTC. Go Live needs the start time and a confirmed soundcheck. */
async function mount(at = '2026-11-15T12:00:30Z') {
  vi.setSystemTime(new Date(at));
  root = document.createElement('div');
  document.body.appendChild(root);
  app = createApp(FlowLive);
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
}

const text = () => root.textContent ?? '';
// Go Live is the countdown at the start: its name is the big line, under the label.
const button = (name: string) =>
  [...root.querySelectorAll('button')].find(
    (b) => (b.querySelector('.countdown-clock') ?? b).textContent?.trim() === name
  );
const goLive = () => button('Go Live')!;
const rows = () =>
  Object.fromEntries(
    [...root.querySelectorAll('.sc-step')].map((li) => [
      li.querySelector('.sc-name')!.textContent,
      `${li.querySelector('.sc-state')!.getAttribute('aria-label')}: ${li.querySelector('.sc-detail')!.textContent}`,
    ])
  );
const notice = () => root.querySelector('[role="alert"]')?.textContent?.trim();

async function press(name: string, ms = 0) {
  button(name)!.click();
  await vi.advanceTimersByTimeAsync(ms);
  await nextTick();
}

/** The recorder hands over chunks, as it does every 250 ms. */
async function sendChunks(count: number) {
  for (let i = 0; i < count; i++) {
    mocks.recorder!.ondataavailable!({
      data: { size: 1, arrayBuffer: async () => new ArrayBuffer(1) },
    });
  }
  await vi.advanceTimersByTimeAsync(0);
}

const MUSIC_MS = 10000;
const MIC_MS = 5000;

type RowName = 'Music' | 'Mic' | 'Server' | 'Return feed';
const row = (name: RowName) =>
  [...root.querySelectorAll('.sc-step')].find(
    (li) => li.querySelector('.sc-name')!.textContent === name
  )!;
const rowButtons = (name: RowName) =>
  [...row(name).querySelectorAll('button')].map((b) => b.textContent?.trim());
const rowButton = (name: RowName, label: string) =>
  [...row(name).querySelectorAll('button')].find((b) => b.textContent?.trim() === label)!;

/** Press a button in the Music or the Mic row. */
async function pressIn(name: RowName, label: string, ms = 0) {
  rowButton(name, label).click();
  await vi.advanceTimersByTimeAsync(ms);
  await nextTick();
}

/** Start test in the Music row, until the music is measured. */
async function testMusic() {
  await pressIn('Music', 'Start test', MUSIC_MS);
}

/** Start test in the Mic row; the server and the return feed answer at once. */
async function testMic(more = 0) {
  await pressIn('Mic', 'Start test', MIC_MS + 500 + more);
}

async function testAll(more = 0) {
  await testMusic();
  await testMic(more);
}

beforeEach(() => {
  vi.useFakeTimers();
  // No "Skip test (dev)" button.
  vi.stubEnv('DEV', false);
  mocks.push.mockReset();
  mocks.passed = ref(false);
  mocks.capturing = ref(true);
  mocks.db = ref(-21);
  mocks.peak = ref(-9);
  mocks.connect.mockReset().mockResolvedValue(undefined);
  mocks.stopStream.mockReset();
  mocks.socketOptions = {};
  mocks.serverChunks = 4;
  mocks.recorder = null;
  mocks.feedPeak = -10;
  mocks.feedPlaying = true;
  mocks.setAudible.mockReset();
  mocks.closeFeed.mockReset();
  mocks.captureDevice.mockReset().mockResolvedValue(true);
  mocks.showsGet.mockReset().mockResolvedValue({ on_air: false });
  vi.stubGlobal('navigator', {
    ...navigator,
    mediaDevices: { addEventListener: vi.fn(), removeEventListener: vi.fn() },
  });
  vi.stubGlobal(
    'AudioContext',
    class {
      close = vi.fn();
    }
  );
  vi.stubGlobal(
    'MediaRecorder',
    class implements FakeRecorder {
      state = 'inactive';
      ondataavailable = null;
      constructor() {
        // eslint-disable-next-line @typescript-eslint/no-this-alias
        mocks.recorder = this;
      }
      start() {
        this.state = 'recording';
      }
      stop() {
        this.state = 'inactive';
      }
    }
  );
});

afterEach(() => {
  // Unmount before the stubbed navigator goes: the card unhooks from it.
  app?.unmount();
  app = null;
  root?.remove();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.useRealTimers();
});

describe('Studio soundcheck', () => {
  it('shows one card with the input and four checks, with buttons for music and mic', async () => {
    await mount();
    expect(text()).toContain('Input level');
    expect(text()).toContain('Music and mic use this one input. You do not need two');
    const options = [...root.querySelectorAll('option')].map((o) => o.textContent?.trim());
    expect(options).toEqual(['Select audio input', 'Focusrite USB', 'Built-in', 'Screen audio']);
    expect(rows()).toEqual({
      Music: 'Waiting: Not checked yet',
      Mic: 'Waiting: Not checked yet',
      Server: 'Waiting: Not checked yet',
      'Return feed': 'Waiting: Not checked yet',
    });
    expect(rowButtons('Music')).toEqual(['Start test', 'Skip']);
    expect(rowButtons('Mic')).toEqual(['Start test', 'Skip']);
    expect(rowButtons('Server')).toEqual([]);
    expect(rowButtons('Return feed')).toEqual([]);
    for (const gone of ['Run soundcheck', 'Play', 'Test broadcast sounds right']) {
      expect(text()).not.toContain(gone);
    }
    expect(goLive().disabled).toBe(true);
  });

  it('tests the music, then the mic, proposes OK and opens Go Live', async () => {
    await mount();
    await pressIn('Music', 'Start test');
    expect(rows().Music).toBe('Checking: Listening…');
    expect(text()).toContain('Play your music now.');
    expect(rowButtons('Music')).toEqual(['Stop']);
    expect(rowButton('Mic', 'Start test').disabled).toBe(true);

    // After the music nothing goes on by itself: the mic has its own button.
    await vi.advanceTimersByTimeAsync(MUSIC_MS + 60000);
    expect(rows().Music).toBe('OK: Good · peak −9 dB · average −21 dB');
    expect(rowButtons('Music')).toEqual(['Re-run', 'Listen to return feed']);
    expect(rows().Mic).toBe('Waiting: Not checked yet');
    expect(rowButtons('Mic')).toEqual(['Start test', 'Skip']);
    expect(rows().Server).toBe('Waiting: Not checked yet');
    expect(mocks.connect).not.toHaveBeenCalled();
    expect(text()).not.toContain('Play your music now.');

    // The mic is a bit quieter than the music.
    mocks.peak!.value = -12;
    mocks.db!.value = -24;
    await pressIn('Mic', 'Start test', 250);
    expect(rows().Mic).toBe('Checking: Listening…');
    expect(text()).toContain('Now talk into the mic.');

    // The server and the return feed follow by themselves.
    await vi.advanceTimersByTimeAsync(MIC_MS + 500);
    expect(rows()).toEqual({
      Music: 'OK: Good · peak −9 dB · average −21 dB',
      Mic: 'OK: Good · peak −12 dB · average −24 dB',
      Server: 'OK: Received your audio',
      'Return feed': 'OK: Your audio comes back',
    });
    expect(text()).toContain('Sounds good — ready to go live');
    // A test broadcast, not the show.
    expect(mocks.connect).toHaveBeenCalledWith(false, undefined, true);
    // The test broadcast is over; the return feed never played aloud.
    expect(mocks.stopStream).toHaveBeenCalledOnce();
    expect(mocks.closeFeed).toHaveBeenCalled();
    expect(mocks.setAudible).not.toHaveBeenCalledWith(true);
    expect(text()).not.toContain('headphones');
    expect(goLive().disabled).toBe(false);
  });

  it('waits for the server and the relay', async () => {
    mocks.serverChunks = 0;
    mocks.feedPeak = -Infinity;
    await mount();
    await testAll();
    expect(rows().Server).toBe('Checking: Sending a test broadcast…');
    expect(text()).toContain('Keep the sound on until the checks are done.');

    mocks.serverChunks = 4;
    await vi.advanceTimersByTimeAsync(250);
    expect(rows().Server).toBe('OK: Received your audio');
    expect(rows()['Return feed']).toBe('Checking: Waiting for the relay…');

    mocks.feedPeak = -14;
    await vi.advanceTimersByTimeAsync(250);
    expect(rows()['Return feed']).toBe('OK: Your audio comes back');
    expect(goLive().disabled).toBe(false);
  });

  describe('names the problem and keeps Go Live closed', () => {
    it('no audio input', async () => {
      mocks.capturing!.value = false;
      await mount();
      await pressIn('Music', 'Start test', 250);
      expect(rows().Music).toBe('Failed: No audio input');
      expect(notice()).toBe(
        'No audio input. Pick your input under “Audio input” and allow the microphone.'
      );
      await pressIn('Mic', 'Skip', 250);
      expect(mocks.connect).not.toHaveBeenCalled();
      expect(button('Confirm anyway')).toBeUndefined();
      expect(goLive().disabled).toBe(true);
    });

    it('no signal at all', async () => {
      mocks.peak!.value = -60;
      await mount();
      await testAll();
      expect(rows().Music).toBe('Failed: No signal');
      expect(rows().Mic).toBe('Failed: No signal');
      expect(rows().Server).toBe('Waiting: Not checked yet');
      expect(notice()).toBe(
        'The music did not arrive from Focusrite USB. Turn it on or turn up its channel, then run its test again.'
      );
      expect(mocks.connect).not.toHaveBeenCalled();
      expect(button('Confirm anyway')).toBeUndefined();
      expect(goLive().disabled).toBe(true);
    });

    it('no mic signal after good music', async () => {
      await mount();
      await testMusic();
      mocks.peak!.value = -60;
      await testMic();
      expect(rows().Music).toBe('OK: Good · peak −9 dB · average −21 dB');
      expect(rows().Mic).toBe('Failed: No signal');
      // The music can be sent, so the other checks still ran.
      expect(rows().Server).toBe('OK: Received your audio');
      expect(notice()).toBe(
        'The mic did not arrive from Focusrite USB. Turn it on or turn up its channel, then run its test again.'
      );
      expect(goLive().disabled).toBe(true);
    });

    it('music too quiet', async () => {
      mocks.peak!.value = -38;
      await mount();
      await testMusic();
      // The problem shows at once, while the mic is still open.
      expect(rows().Music).toBe('Warning: Too quiet · peak −38 dB · average −21 dB');
      expect(notice()).toBe(
        'The music is too quiet: peak −38 dB, average −21 dB. Turn up its gain until the average is around −20 dB.'
      );
      mocks.peak!.value = -9;
      await testMic();
      expect(rows().Mic).toBe('OK: Good · peak −9 dB · average −21 dB');
      expect(rows().Server).toBe('OK: Received your audio');
      expect(notice()).toContain('The music is too quiet');
      expect(goLive().disabled).toBe(true);
    });

    it('mic clipping', async () => {
      await mount();
      await testMusic();
      mocks.peak!.value = 0;
      await testMic();
      expect(rows().Mic).toBe('Warning: Too loud · peak 0 dB · average −21 dB');
      expect(notice()).toBe(
        'The mic is too loud: peak 0 dB, average −21 dB. Turn down its gain until peaks stay under −3 dB.'
      );
      expect(goLive().disabled).toBe(true);
    });

    it('too quiet or too loud on average, with a good peak', async () => {
      mocks.db!.value = -40;
      await mount();
      await testMusic();
      expect(rows().Music).toBe('Warning: Too quiet · peak −9 dB · average −40 dB');
      mocks.db!.value = -6;
      await testMic();
      expect(rows().Mic).toBe('Warning: Too loud · peak −9 dB · average −6 dB');
      expect(notice()).toBe(
        'The music is too quiet: peak −9 dB, average −40 dB. Turn up its gain until the average is around −20 dB.'
      );
      expect(goLive().disabled).toBe(true);
    });

    it('rates only the time with sound', async () => {
      // Silence while the track starts, then good music.
      mocks.db!.value = -60;
      mocks.peak!.value = -60;
      await mount();
      await pressIn('Music', 'Start test', 4900);
      mocks.db!.value = -21;
      mocks.peak!.value = -9;
      await vi.advanceTimersByTimeAsync(MUSIC_MS - 4900);
      expect(rows().Music).toBe('OK: Good · peak −9 dB · average −21 dB');
    });

    it('music and mic both skipped', async () => {
      await mount();
      await pressIn('Music', 'Skip', 250);
      await pressIn('Mic', 'Skip', 250);
      expect(rows().Music).toBe('Skipped: Not in this show');
      expect(rows().Mic).toBe('Skipped: Not in this show');
      // A skipped source can still be tested.
      expect(rowButtons('Music')).toEqual(['Start test']);
      expect(notice()).toBe('Music and mic were both skipped. Test at least one.');
      expect(mocks.connect).not.toHaveBeenCalled();
      expect(goLive().disabled).toBe(true);
    });

    it('the test broadcast does not connect', async () => {
      mocks.connect.mockRejectedValue(new Error('Connection error'));
      await mount();
      await testAll();
      expect(rows().Server).toBe('Failed: Not connected');
      expect(notice()).toBe(
        'The test broadcast did not start: Failed to connect to the test stream.'
      );
      expect(button('Confirm anyway')).toBeUndefined();
      expect(goLive().disabled).toBe(true);
    });

    it('the server ends the test broadcast', async () => {
      mocks.serverChunks = 0;
      await mount();
      await testAll();
      mocks.socketOptions.onError!('test stream is not configured on this server');
      await vi.advanceTimersByTimeAsync(250);
      expect(rows().Server).toBe('Failed: Test broadcast ended');
      expect(notice()).toBe(
        'The server ended the test broadcast: test stream is not configured on this server'
      );
      expect(goLive().disabled).toBe(true);
    });

    // https://github.com/phaabe/live.moafunk.de/issues/958: the host is on air in another tab.
    it('the server refuses the soundcheck of the host who is on air', async () => {
      mocks.serverChunks = 0;
      await mount();
      await testAll();
      mocks.socketOptions.onError!('Soundcheck not started: you are on air right now');
      await vi.advanceTimersByTimeAsync(250);
      expect(rows().Server).toBe('Failed: Test broadcast ended');
      expect(notice()).toBe(
        'The server ended the test broadcast: Soundcheck not started: you are on air right now'
      );
      expect(button('Confirm anyway')).toBeUndefined();
      expect(goLive().disabled).toBe(true);
    });

    it('the server gets no chunks although the browser sent some', async () => {
      mocks.serverChunks = 0;
      await mount();
      await testAll();
      await sendChunks(8);
      await vi.advanceTimersByTimeAsync(10000);
      expect(rows().Server).toBe('Failed: No audio arrived');
      expect(rows()['Return feed']).toBe('Waiting: Not checked yet');
      expect(notice()).toBe(
        'The server got no audio. Check your internet connection, then run a test again.'
      );
      // Without audio on the server nothing can be confirmed.
      expect(button('Confirm anyway')).toBeUndefined();
      expect(mocks.stopStream).toHaveBeenCalled();
      expect(goLive().disabled).toBe(true);
    });

    it('the browser sends no chunks', async () => {
      mocks.serverChunks = 0;
      await mount();
      await testAll(10000);
      expect(rows().Server).toBe('Failed: No audio sent');
      expect(notice()).toBe(
        'The browser sent no audio. Pick your audio input again, then run a test again.'
      );
      expect(goLive().disabled).toBe(true);
    });

    it('no signal comes back from the relay', async () => {
      mocks.feedPeak = -Infinity;
      await mount();
      await testAll(20000);
      expect(rows()['Return feed']).toBe('Failed: Only silence came back');
      expect(notice()).toBe(
        'Your audio did not come back from the relay. Run a test again; if it stays, tell the tech team.'
      );
      expect(goLive().disabled).toBe(true);

      mocks.feedPlaying = false;
      await pressIn('Mic', 'Re-run', MIC_MS + 500 + 20000);
      expect(rows()['Return feed']).toBe('Failed: Nothing came back');
    });
  });

  it('passes a show without a mic once the music is good', async () => {
    await mount();
    await testMusic();
    await pressIn('Mic', 'Skip', 500);
    expect(rows().Mic).toBe('Skipped: Not in this show');
    expect(rows()['Return feed']).toBe('OK: Your audio comes back');
    expect(text()).toContain('Sounds good — ready to go live');
    expect(goLive().disabled).toBe(false);
  });

  it('re-runs one source: the result goes, the server and the relay are checked again', async () => {
    await mount();
    await testAll();
    expect(goLive().disabled).toBe(false);

    mocks.peak!.value = -38;
    await pressIn('Mic', 'Re-run', 250);
    expect(goLive().disabled).toBe(true);
    expect(rows().Music).toBe('OK: Good · peak −9 dB · average −21 dB');
    expect(rows().Mic).toBe('Checking: Listening…');
    expect(rows().Server).toBe('Waiting: Not checked yet');

    await vi.advanceTimersByTimeAsync(MIC_MS + 500);
    expect(rows().Mic).toBe('Warning: Too quiet · peak −38 dB · average −21 dB');
    expect(rows().Server).toBe('OK: Received your audio');
    expect(notice()).toContain('The mic is too quiet');
    expect(mocks.connect).toHaveBeenCalledTimes(2);
    expect(goLive().disabled).toBe(true);
  });

  it('lets the host confirm against a problem once the server has audio', async () => {
    mocks.peak!.value = -38;
    await mount();
    await testAll();
    await press('Confirm anyway');
    expect(text()).toContain('Soundcheck confirmed · ready to go live');
    expect(goLive().disabled).toBe(false);
  });

  it('lets the host mark a good result as not OK', async () => {
    await mount();
    await testAll();
    await press('Not OK');
    expect(goLive().disabled).toBe(true);
    expect(notice()).toContain('You marked the soundcheck as not OK.');
  });

  it('plays the return feed and asks the host when it cannot be measured', async () => {
    mocks.feedPeak = null;
    await mount();
    await testAll();
    expect(rows()['Return feed']).toBe('Warning: Playing · not measured in this browser');
    expect(text()).toContain('Do you hear your audio?');
    expect(mocks.setAudible).toHaveBeenLastCalledWith(true);
    expect(text()).toContain('headphones');
    // Nothing is confirmed, and the test broadcast stays up to be heard.
    expect(goLive().disabled).toBe(true);
    expect(mocks.stopStream).not.toHaveBeenCalled();

    await press('Sounds right');
    expect(goLive().disabled).toBe(false);

    // Go Live needs the socket: the test broadcast stops first.
    await press('Go Live');
    expect(mocks.stopStream).toHaveBeenCalledOnce();
    expect(mocks.connect).toHaveBeenLastCalledWith(false, 7, false, true);
  });

  it('plays the return feed on request, from a tested row', async () => {
    await mount();
    await testMusic();
    await pressIn('Music', 'Listen to return feed');
    expect(mocks.connect).toHaveBeenCalledWith(false, undefined, true);
    expect(mocks.setAudible).toHaveBeenLastCalledWith(true);
    expect(text()).toContain('headphones');
    expect(rowButton('Music', 'Stop listening').getAttribute('aria-pressed')).toBe('true');

    await pressIn('Music', 'Stop listening');
    expect(mocks.stopStream).toHaveBeenCalledOnce();
    expect(mocks.closeFeed).toHaveBeenCalled();
    expect(text()).not.toContain('headphones');
  });

  it('forgets the result when the audio input changes', async () => {
    await mount();
    await testAll();
    expect(goLive().disabled).toBe(false);

    const select = root.querySelector('select')!;
    select.value = 'mic';
    select.dispatchEvent(new Event('change'));
    await vi.advanceTimersByTimeAsync(0);
    expect(mocks.captureDevice).toHaveBeenCalledWith('mic');
    expect(rows().Music).toBe('Waiting: Not checked yet');
    expect(rows().Mic).toBe('Waiting: Not checked yet');
    expect(text()).not.toContain('ready to go live');
    expect(goLive().disabled).toBe(true);
  });

  it('stops a test on request and leaves the row open', async () => {
    await mount();
    await pressIn('Music', 'Start test', 1000);
    await pressIn('Music', 'Stop');
    expect(rows().Music).toBe('Waiting: Not checked yet');
    expect(rowButtons('Music')).toEqual(['Start test', 'Skip']);
    expect(text()).not.toContain('Play your music now.');
    await vi.advanceTimersByTimeAsync(MUSIC_MS);
    expect(rows().Music).toBe('Waiting: Not checked yet');
    expect(goLive().disabled).toBe(true);
  });

  it('stops the test broadcast and the return feed when the host leaves', async () => {
    mocks.serverChunks = 0;
    await mount();
    await testAll();
    expect(mocks.recorder!.state).toBe('recording');

    app!.unmount();
    app = null;
    expect(mocks.stopStream).toHaveBeenCalled();
    expect(mocks.recorder).toMatchObject({ state: 'inactive' });
    expect(mocks.closeFeed).toHaveBeenCalled();
    // The run is over: nothing is confirmed later.
    mocks.serverChunks = 4;
    await vi.advanceTimersByTimeAsync(30000);
    expect(mocks.passed!.value).toBe(false);
  });

  it('keeps an earlier confirmed soundcheck', async () => {
    mocks.passed!.value = true;
    await mount();
    expect(text()).toContain('Soundcheck confirmed · ready to go live');
    expect(goLive().disabled).toBe(false);
    await press('Not OK');
    expect(goLive().disabled).toBe(true);
  });
});

describe('Studio · Back on air', () => {
  const back = () => button('Go back on air')!;

  it('goes back on air with only an input, no soundcheck', async () => {
    mocks.showsGet.mockResolvedValue({ on_air: true });
    await mount();
    expect(mocks.showsGet).toHaveBeenCalledWith(7);
    expect(text()).toContain('Still on air · no audio');
    expect(notice()).toBe(
      'Your show is still on air, but no audio arrives. Listeners hear silence.'
    );
    expect(button('Start test')).toBeUndefined();
    expect(text()).not.toContain('Record this show');

    mocks.capturing!.value = false;
    await nextTick();
    expect(back().disabled).toBe(true);
    mocks.capturing!.value = true;
    await nextTick();
    await press('Go back on air');
    expect(mocks.connect).toHaveBeenCalledWith(false, 7, false, true);
  });

  it('shows the normal soundcheck when the 5 minutes ran out', async () => {
    mocks.showsGet.mockResolvedValueOnce({ on_air: true }).mockResolvedValue({ on_air: false });
    await mount();
    await press('Go back on air');
    expect(mocks.connect).not.toHaveBeenCalled();
    expect(text()).toContain('It is air time · confirm the soundcheck first');
    expect(goLive().disabled).toBe(true);
  });

  it('goes back on air when the server cannot say', async () => {
    mocks.showsGet.mockResolvedValueOnce({ on_air: true }).mockRejectedValue(new Error('offline'));
    await mount();
    await press('Go back on air');
    expect(mocks.connect).toHaveBeenCalledWith(false, 7, false, true);
  });

  it('opens the normal Studio when the server cannot say at the start', async () => {
    mocks.showsGet.mockRejectedValue(new Error('offline'));
    await mount();
    expect(button('Go back on air')).toBeUndefined();
    expect(goLive().disabled).toBe(true);
  });
});

describe('flow', () => {
  /** The flow's example: "show starts in 18:42". */
  async function at1842() {
    vi.setSystemTime(new Date('2026-11-15T11:41:17Z'));
    await vi.advanceTimersByTimeAsync(1000);
  }
  // With this the mic test takes whole seconds, so the page's clock stays in step.
  const SECOND_MS = 500;

  it('Studio matches the flow', async () => {
    await mount('2026-11-15T11:41:18Z');
    await expectFlow('studio', root);
  });

  it('Studio · Soundcheck, music tested matches the flow', async () => {
    await mount('2026-11-15T11:40:00Z');
    await testMusic();
    await at1842();
    await expectFlow('studio-checking', root);
  });

  it('Studio · Soundcheck, ready matches the flow', async () => {
    await mount('2026-11-15T11:40:00Z');
    await testMusic();
    mocks.peak!.value = -12;
    mocks.db!.value = -24;
    await testMic(SECOND_MS);
    await at1842();
    await expectFlow('studio-ready', root);
  });

  it('Studio · Soundcheck, problem matches the flow', async () => {
    await mount('2026-11-15T11:40:00Z');
    await testMusic();
    mocks.peak!.value = -38;
    mocks.db!.value = -48;
    await testMic(SECOND_MS);
    await at1842();
    await expectFlow('studio-problem', root);
  });

  it('Studio · Back on air matches the flow', async () => {
    mocks.showsGet.mockResolvedValue({ on_air: true });
    await mount();
    await expectFlow('studio-backonair', root);
  });
});
