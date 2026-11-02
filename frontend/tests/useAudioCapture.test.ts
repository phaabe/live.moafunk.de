import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  audioContextOptionsForStream,
  useAudioCapture,
  NO_AUDIO_DATA_MS,
  NO_AUDIO_DATA_MESSAGE,
  BROWSER_UNSUPPORTED_MESSAGE,
  streamRecordingMimeType,
} from '../src/admin/composables/useAudioCapture';

/** Minimal MediaStream stub exposing just the audio-track sample rate. */
function streamWithRate(rate: number | undefined): MediaStream {
  return {
    getAudioTracks: () => [{ getSettings: () => (rate === undefined ? {} : { sampleRate: rate }) }],
  } as unknown as MediaStream;
}

describe('audioContextOptionsForStream', () => {
  it('matches the context to a 96 kHz pro interface (Xone:23C)', () => {
    expect(audioContextOptionsForStream(streamWithRate(96000))).toEqual({
      sampleRate: 96000,
    });
  });

  it('matches the context to a 44.1 kHz device', () => {
    expect(audioContextOptionsForStream(streamWithRate(44100))).toEqual({
      sampleRate: 44100,
    });
  });

  it('falls back to the default context when the rate is unknown', () => {
    expect(audioContextOptionsForStream(streamWithRate(undefined))).toBeUndefined();
  });

  it('falls back to the default context when there is no audio track', () => {
    const empty = { getAudioTracks: () => [] } as unknown as MediaStream;
    expect(audioContextOptionsForStream(empty)).toBeUndefined();
  });
});

// ─── Capture → recorder (fake Web Audio + MediaRecorder) ─────────────────────

const node = () => ({ connect: vi.fn(), disconnect: vi.fn(), gain: { value: 1 } });

/** A context that starts the way the browser starts one outside a gesture. */
class FakeAudioContext {
  static instances: FakeAudioContext[] = [];
  state: AudioContextState = 'suspended';
  resume = vi.fn(async () => {
    this.state = 'running';
  });
  close = vi.fn(async () => {});
  createMediaStreamSource = vi.fn(node);
  createGain = vi.fn(node);
  createAnalyser = vi.fn(() => ({ ...node(), fftSize: 0, smoothingTimeConstant: 0 }));
  createMediaStreamDestination = vi.fn(() => ({ ...node(), stream: fakeStream('live') }));
  constructor() {
    FakeAudioContext.instances.push(this);
  }
}

class FakeMediaRecorder {
  static instances: FakeMediaRecorder[] = [];
  static supported: string[] = [];
  static isTypeSupported = (type: string) => FakeMediaRecorder.supported.includes(type);
  state: RecordingState = 'inactive';
  ondataavailable:
    | ((event: { data: { size: number; arrayBuffer: () => Promise<ArrayBuffer> } }) => void)
    | null = null;
  onerror: ((e: unknown) => void) | null = null;
  constructor() {
    FakeMediaRecorder.instances.push(this);
  }
  start() {
    this.state = 'recording';
  }
  stop() {
    this.state = 'inactive';
  }
  emit(size: number) {
    this.ondataavailable?.({ data: { size, arrayBuffer: async () => new ArrayBuffer(size) } });
  }
}

const CHROME_TYPE = 'audio/webm;codecs=opus';
/** Safari before 18.4 records only MP4. */
const SAFARI_18_3_TYPES = ['audio/mp4'];

function fakeStream(readyState: MediaStreamTrackState): MediaStream {
  const track = { readyState, getSettings: () => ({}), stop: vi.fn() };
  return { getAudioTracks: () => [track], getTracks: () => [track] } as unknown as MediaStream;
}

describe('useAudioCapture recording', () => {
  let deviceStream: MediaStream;

  beforeEach(() => {
    vi.useFakeTimers();
    FakeAudioContext.instances = [];
    FakeMediaRecorder.instances = [];
    FakeMediaRecorder.supported = [CHROME_TYPE, 'audio/webm'];
    deviceStream = fakeStream('live');
    vi.stubGlobal('AudioContext', FakeAudioContext);
    vi.stubGlobal('MediaRecorder', FakeMediaRecorder);
    vi.stubGlobal('navigator', {
      mediaDevices: { getUserMedia: vi.fn(async () => deviceStream) },
    });
  });

  afterEach(() => {
    useAudioCapture().stopCapture();
    useAudioCapture().setOnError(null);
    useAudioCapture().setOnData(null);
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('resumes a suspended audio context when capture starts', async () => {
    const capture = useAudioCapture();
    expect(await capture.captureDevice('mic')).toBe(true);
    expect(FakeAudioContext.instances[0].resume).toHaveBeenCalled();
  });

  it('refuses to record from a device whose track has ended', async () => {
    deviceStream = fakeStream('ended');
    const capture = useAudioCapture();
    await capture.captureDevice('mic');
    expect(capture.startRecording()).toBe(false);
    expect(capture.error.value).toMatch(/no longer active/);
    expect(FakeMediaRecorder.instances).toHaveLength(0);
  });

  it('reports an error when the recorder sends no audio', async () => {
    const onError = vi.fn();
    const capture = useAudioCapture({ onError, onData: vi.fn() });
    await capture.captureDevice('mic');
    expect(capture.startRecording()).toBe(true);

    // Empty blobs, as from a suspended context, do not count as audio.
    FakeMediaRecorder.instances[0].emit(0);
    vi.advanceTimersByTime(NO_AUDIO_DATA_MS);
    expect(onError).toHaveBeenCalledWith(NO_AUDIO_DATA_MESSAGE);
  });

  it('stays quiet when the recorder sends audio', async () => {
    const onError = vi.fn();
    const onData = vi.fn();
    const capture = useAudioCapture({ onError, onData });
    await capture.captureDevice('mic');
    capture.startRecording();

    FakeMediaRecorder.instances[0].emit(512);
    await vi.advanceTimersByTimeAsync(NO_AUDIO_DATA_MS);
    expect(onData).toHaveBeenCalledOnce();
    expect(onError).not.toHaveBeenCalled();
  });

  it('does not report a recorder that was stopped in time', async () => {
    const onError = vi.fn();
    const capture = useAudioCapture({ onError, onData: vi.fn() });
    await capture.captureDevice('mic');
    capture.startRecording();
    capture.stopRecording();

    vi.advanceTimersByTime(NO_AUDIO_DATA_MS);
    expect(onError).not.toHaveBeenCalled();
  });
});

describe('streamRecordingMimeType', () => {
  beforeEach(() => {
    vi.stubGlobal('MediaRecorder', FakeMediaRecorder);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('prefers WebM with Opus', () => {
    FakeMediaRecorder.supported = [CHROME_TYPE, 'audio/webm'];
    expect(streamRecordingMimeType()).toBe(CHROME_TYPE);
  });

  it('falls back to plain WebM', () => {
    FakeMediaRecorder.supported = ['audio/webm'];
    expect(streamRecordingMimeType()).toBe('audio/webm');
  });

  it('refuses Safari before 18.4, which records only MP4', () => {
    FakeMediaRecorder.supported = SAFARI_18_3_TYPES;
    expect(streamRecordingMimeType()).toBeNull();
  });

  it('refuses a browser without MediaRecorder', () => {
    vi.stubGlobal('MediaRecorder', undefined);
    expect(streamRecordingMimeType()).toBeNull();
  });
});

describe('useAudioCapture on an unsupported browser', () => {
  beforeEach(() => {
    FakeMediaRecorder.instances = [];
    FakeMediaRecorder.supported = SAFARI_18_3_TYPES;
    vi.stubGlobal('AudioContext', FakeAudioContext);
    vi.stubGlobal('MediaRecorder', FakeMediaRecorder);
    vi.stubGlobal('navigator', {
      mediaDevices: { getUserMedia: vi.fn(async () => fakeStream('live')) },
    });
  });

  afterEach(() => {
    useAudioCapture().stopCapture();
    vi.unstubAllGlobals();
  });

  it('does not start a recorder and says why', async () => {
    const capture = useAudioCapture();
    await capture.captureDevice('mic');
    expect(capture.startRecording()).toBe(false);
    expect(capture.error.value).toBe(BROWSER_UNSUPPORTED_MESSAGE);
    expect(FakeMediaRecorder.instances).toHaveLength(0);
  });
});
