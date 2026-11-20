import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { BROWSER_UNSUPPORTED_MESSAGE } from '../useAudioCapture';

// Fakes for the browser audio APIs jsdom does not have. They model only what
// the host would notice: which chunks leave the browser, in which order, and
// which errors reach the screen.

class FakeTrack extends EventTarget {
  readyState: MediaStreamTrackState = 'live';
  constructor(
    public kind: 'audio' | 'video' = 'audio',
    private sampleRate?: number
  ) {
    super();
  }
  /** Like the browser: a track the page stops fires no 'ended'. */
  stop() {
    this.readyState = 'ended';
  }
  /** The device goes away (unplugged, or the shared tab closed). */
  unplug() {
    this.readyState = 'ended';
    this.dispatchEvent(new Event('ended'));
  }
  getSettings() {
    return { sampleRate: this.sampleRate };
  }
}

class FakeStream {
  constructor(public tracks: FakeTrack[] = [new FakeTrack()]) {}
  getTracks() {
    return this.tracks;
  }
  getAudioTracks() {
    return this.tracks.filter((t) => t.kind === 'audio');
  }
  getVideoTracks() {
    return this.tracks.filter((t) => t.kind === 'video');
  }
}

const audioNode = () => ({ connect() {}, disconnect() {} });

class FakeAudioContext {
  static instances: FakeAudioContext[] = [];
  /** A context made without a user gesture starts suspended. */
  static gesture = true;
  state: AudioContextState = FakeAudioContext.gesture ? 'running' : 'suspended';
  constructor(public options?: AudioContextOptions) {
    FakeAudioContext.instances.push(this);
  }
  createMediaStreamSource() {
    return audioNode();
  }
  createGain() {
    return { ...audioNode(), gain: { value: 1 } };
  }
  createAnalyser() {
    return audioNode();
  }
  createMediaStreamDestination() {
    return { ...audioNode(), stream: new FakeStream() };
  }
  async resume() {
    if (!FakeAudioContext.gesture) throw new Error('resume needs a user gesture');
    this.state = 'running';
  }
  async close() {
    this.state = 'closed';
  }
}

interface FakeBlob {
  size: number;
  arrayBuffer(): Promise<ArrayBuffer>;
}

const chunk = (...bytes: number[]): FakeBlob => ({
  size: bytes.length,
  arrayBuffer: async () => new Uint8Array(bytes).buffer,
});

/** A chunk whose read finishes only when the test says so. */
function slowChunk(...bytes: number[]) {
  let finish!: () => void;
  const read = new Promise<ArrayBuffer>((resolve) => {
    finish = () => resolve(new Uint8Array(bytes).buffer);
  });
  return { blob: { size: bytes.length, arrayBuffer: () => read }, finish };
}

class FakeRecorder {
  static instances: FakeRecorder[] = [];
  static supported = ['audio/webm;codecs=opus', 'audio/webm'];
  static refuse: Error | null = null;
  static isTypeSupported(type: string) {
    return FakeRecorder.supported.includes(type);
  }
  state: RecordingState = 'inactive';
  ondataavailable: ((event: { data: FakeBlob }) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  constructor(
    public stream: FakeStream,
    public options: MediaRecorderOptions
  ) {
    if (FakeRecorder.refuse) throw FakeRecorder.refuse;
    FakeRecorder.instances.push(this);
  }
  start() {
    this.state = 'recording';
  }
  stop() {
    this.state = 'inactive';
  }
  emit(data: FakeBlob) {
    this.ondataavailable?.({ data });
  }
}

type CaptureModule = typeof import('../useAudioCapture');
let mod: CaptureModule;
let device: FakeStream;
let mediaDevices: {
  getUserMedia: Mock;
  enumerateDevices: Mock;
  getDisplayMedia: Mock;
};

beforeEach(async () => {
  vi.useFakeTimers();
  FakeRecorder.instances = [];
  FakeRecorder.supported = ['audio/webm;codecs=opus', 'audio/webm'];
  FakeRecorder.refuse = null;
  FakeAudioContext.instances = [];
  FakeAudioContext.gesture = true;
  device = new FakeStream([new FakeTrack('audio', 48000)]);
  mediaDevices = {
    getUserMedia: vi.fn(async () => device),
    enumerateDevices: vi.fn(async () => []),
    getDisplayMedia: vi.fn(),
  };
  vi.stubGlobal('MediaRecorder', FakeRecorder);
  vi.stubGlobal('AudioContext', FakeAudioContext);
  vi.stubGlobal('MediaStream', FakeStream);
  vi.stubGlobal('navigator', { mediaDevices });
  vi.spyOn(console, 'log').mockImplementation(() => {});
  vi.spyOn(console, 'warn').mockImplementation(() => {});
  vi.spyOn(console, 'error').mockImplementation(() => {});
  // The composable keeps singleton state; every test gets a fresh module.
  vi.resetModules();
  mod = await import('../useAudioCapture');
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

/** Let pending chunk reads finish. */
const flush = () => vi.advanceTimersByTimeAsync(0);

const bytesOf = (call: unknown[]) => [...new Uint8Array(call[0] as ArrayBuffer)];

async function goLive() {
  const onData = vi.fn();
  const onError = vi.fn();
  const capture = mod.useAudioCapture({ onData, onError });
  expect(await capture.captureDevice('mic-1')).toBe(true);
  expect(capture.startRecording()).toBe(true);
  return { capture, onData, onError, recorder: FakeRecorder.instances.at(-1)! };
}

describe('on air: audio leaves the browser', () => {
  it('sends every chunk in order and marks only the first one of the recorder', async () => {
    const { onData, recorder } = await goLive();
    recorder.emit(chunk(1));
    recorder.emit(chunk(2));
    recorder.emit(chunk(3));
    await flush();
    expect(onData.mock.calls.map(bytesOf)).toEqual([[1], [2], [3]]);
    expect(onData.mock.calls.map((c) => c[1])).toEqual([true, false, false]);
  });

  it('keeps the order when an earlier chunk takes longer to read', async () => {
    const { onData, recorder } = await goLive();
    const slow = slowChunk(1);
    recorder.emit(slow.blob);
    recorder.emit(chunk(2));
    await flush();
    expect(onData).not.toHaveBeenCalled();
    slow.finish();
    await flush();
    expect(onData.mock.calls.map(bytesOf)).toEqual([[1], [2]]);
  });

  it('keeps sending after one chunk cannot be read', async () => {
    const { onData, recorder } = await goLive();
    recorder.emit({ size: 1, arrayBuffer: () => Promise.reject(new Error('read failed')) });
    recorder.emit(chunk(2));
    await flush();
    expect(onData.mock.calls.map(bytesOf)).toEqual([[2]]);
  });

  it('on a bitrate change, sends the old recorder’s last chunk before the new one’s first', async () => {
    const { capture, onData, recorder } = await goLive();
    const last = slowChunk(1);
    recorder.emit(last.blob);
    capture.setStreamBitsPerSecond(128_000);
    const next = FakeRecorder.instances.at(-1)!;
    expect(next).not.toBe(recorder);
    expect(recorder.state).toBe('inactive');
    expect(next.options.audioBitsPerSecond).toBe(128_000);
    next.emit(chunk(2));
    last.finish();
    await flush();
    expect(onData.mock.calls.map(bytesOf)).toEqual([[1], [2]]);
    // The new recorder starts a new WebM stream with its own header.
    expect(onData.mock.calls.map((c) => c[1])).toEqual([true, true]);
  });

  it('keeps the recorder when the bitrate does not change', async () => {
    const { capture } = await goLive();
    capture.setStreamBitsPerSecond(mod.STREAM_AUDIO_BITS_PER_SECOND);
    expect(FakeRecorder.instances).toHaveLength(1);
  });

  it('starts a suspended audio context with the Go Live click', async () => {
    FakeAudioContext.gesture = false;
    const capture = mod.useAudioCapture();
    await capture.captureDevice('mic-1');
    await flush();
    const context = FakeAudioContext.instances[0];
    expect(context.state).toBe('suspended');
    FakeAudioContext.gesture = true;
    capture.startRecording();
    await flush();
    expect(context.state).toBe('running');
  });

  it('runs the audio context at the device’s own sample rate', async () => {
    device = new FakeStream([new FakeTrack('audio', 96_000)]);
    await mod.useAudioCapture().captureDevice('xone');
    expect(FakeAudioContext.instances[0].options).toEqual({ sampleRate: 96_000 });
    expect(mod.audioContextOptionsForStream(new FakeStream() as never)).toBeUndefined();
  });
});

describe('a failed start is shown to the host, never swallowed', () => {
  it.each([
    {
      why: 'no input was picked',
      setup: async () => {},
      message: 'No audio source available',
    },
    {
      why: 'the input was unplugged',
      setup: async (capture: ReturnType<CaptureModule['useAudioCapture']>) => {
        await capture.captureDevice('mic-1');
        device.tracks[0].readyState = 'ended';
      },
      message: 'Audio device is no longer active. Re-select your device.',
    },
    {
      why: 'the browser cannot record WebM (Safari before 18.4)',
      setup: async (capture: ReturnType<CaptureModule['useAudioCapture']>) => {
        FakeRecorder.supported = [];
        await capture.captureDevice('mic-1');
      },
      message: BROWSER_UNSUPPORTED_MESSAGE,
    },
    {
      why: 'the browser refuses to create the recorder',
      setup: async (capture: ReturnType<CaptureModule['useAudioCapture']>) => {
        FakeRecorder.refuse = new Error('NotSupportedError: audio/webm');
        await capture.captureDevice('mic-1');
      },
      message: 'NotSupportedError: audio/webm',
    },
  ])('when $why', async ({ setup, message }) => {
    const onData = vi.fn();
    const onError = vi.fn();
    const capture = mod.useAudioCapture({ onData, onError });
    await setup(capture);
    expect(capture.startRecording()).toBe(false);
    expect(onError).toHaveBeenCalledWith(message);
    expect(capture.error.value).toBe(message);
    expect(capture.isRecording.value).toBe(false);
  });

  it('tells the host when an on-air bitrate change cannot restart the recorder', async () => {
    const { capture, onError, recorder } = await goLive();
    device.tracks[0].readyState = 'ended';
    capture.setStreamBitsPerSecond(96_000);
    expect(recorder.state).toBe('inactive');
    expect(onError).toHaveBeenCalledWith(
      'Audio device is no longer active. Re-select your device.'
    );
  });

  it('warns after 5 s when the recorder sends nothing', async () => {
    const { capture, onError } = await goLive();
    await vi.advanceTimersByTimeAsync(mod.NO_AUDIO_DATA_MS - 1);
    expect(onError).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(onError).toHaveBeenCalledWith(mod.NO_AUDIO_DATA_MESSAGE);
    expect(capture.error.value).toBe(mod.NO_AUDIO_DATA_MESSAGE);
  });

  it('counts empty chunks as no audio', async () => {
    const { onError, recorder } = await goLive();
    for (let t = 0; t < mod.NO_AUDIO_DATA_MS; t += 250) {
      recorder.emit(chunk());
      await vi.advanceTimersByTimeAsync(250);
    }
    expect(onError).toHaveBeenCalledWith(mod.NO_AUDIO_DATA_MESSAGE);
  });

  it('warns when the chunks go nowhere because no sink is wired', async () => {
    const onError = vi.fn();
    const capture = mod.useAudioCapture({ onError });
    capture.setOnData(null);
    await capture.captureDevice('mic-1');
    capture.startRecording();
    FakeRecorder.instances[0].emit(chunk(1));
    await vi.advanceTimersByTimeAsync(mod.NO_AUDIO_DATA_MS);
    expect(onError).toHaveBeenCalledWith(mod.NO_AUDIO_DATA_MESSAGE);
  });

  it('stays quiet while chunks flow', async () => {
    const { onError, recorder } = await goLive();
    for (let t = 0; t < 2 * mod.NO_AUDIO_DATA_MS; t += 250) {
      recorder.emit(chunk(1));
      await vi.advanceTimersByTimeAsync(250);
    }
    expect(onError).not.toHaveBeenCalled();
  });

  it('reports a recorder error on air', async () => {
    const { capture, onError, recorder } = await goLive();
    recorder.onerror?.(new Event('error'));
    expect(onError).toHaveBeenCalledWith('Recording error');
    expect(capture.error.value).toBe('Recording error');
  });

  it('reports a refused microphone', async () => {
    mediaDevices.getUserMedia.mockRejectedValueOnce(new Error('Permission denied'));
    const onError = vi.fn();
    const capture = mod.useAudioCapture({ onError });
    expect(await capture.captureDevice('mic-1')).toBe(false);
    expect(onError).toHaveBeenCalledWith('Permission denied');
    expect(capture.isCapturing.value).toBe(false);
  });

  it('asks the host to pick an input before capturing', async () => {
    const capture = mod.useAudioCapture();
    expect(await capture.captureDevice('')).toBe(false);
    expect(capture.error.value).toBe('Please select an audio device');
  });

  it('warns when the input is unplugged while on air', async () => {
    const { capture, onError, recorder } = await goLive();
    device.tracks[0].unplug();
    expect(onError).toHaveBeenCalledWith(mod.DEVICE_ENDED_MESSAGE);
    expect(capture.error.value).toBe(mod.DEVICE_ENDED_MESSAGE);
    // The recorder keeps sending (silence), so the recording keeps its timeline.
    expect(recorder.state).toBe('recording');
    expect(capture.isRecording.value).toBe(true);
  });

  it('warns when the shared screen stops while on air', async () => {
    const audio = new FakeTrack();
    mediaDevices.getDisplayMedia.mockResolvedValue(new FakeStream([audio, new FakeTrack('video')]));
    const onError = vi.fn();
    const capture = mod.useAudioCapture({ onError });
    await capture.captureScreenAudio();
    capture.startRecording();
    audio.unplug();
    expect(onError).toHaveBeenCalledWith(mod.DEVICE_ENDED_MESSAGE);
  });

  it('stays quiet when one of two input tracks ends', async () => {
    device = new FakeStream([new FakeTrack(), new FakeTrack()]);
    const { onError } = await goLive();
    device.tracks[0].unplug();
    expect(onError).not.toHaveBeenCalled();
    device.tracks[1].unplug();
    expect(onError).toHaveBeenCalledWith(mod.DEVICE_ENDED_MESSAGE);
  });

  it('leaves an input unplugged before Go Live to the Go Live check', async () => {
    const onError = vi.fn();
    const capture = mod.useAudioCapture({ onError });
    await capture.captureDevice('mic-1');
    device.tracks[0].unplug();
    expect(onError).not.toHaveBeenCalled();
    expect(capture.startRecording()).toBe(false);
    expect(onError).toHaveBeenCalledWith(mod.DEVICE_ENDED_MESSAGE);
  });

  it('ignores the end of an input that was replaced', async () => {
    const { capture, onError } = await goLive();
    const old = device;
    device = new FakeStream([new FakeTrack('audio', 48000)]);
    await capture.captureDevice('mic-2');
    expect(capture.startRecording()).toBe(true);
    old.tracks[0].unplug();
    expect(onError).not.toHaveBeenCalled();
    // The new input is watched.
    device.tracks[0].unplug();
    expect(onError).toHaveBeenCalledWith(mod.DEVICE_ENDED_MESSAGE);
  });
});

describe('End Episode stops cleanly', () => {
  it('stops the audio, releases the microphone and resets the bitrate', async () => {
    const { capture, onData, onError, recorder } = await goLive();
    capture.setStreamBitsPerSecond(128_000);
    const current = FakeRecorder.instances.at(-1)!;
    capture.stopCapture();
    expect(recorder.state).toBe('inactive');
    expect(current.state).toBe('inactive');
    expect(device.tracks.every((t) => t.readyState === 'ended')).toBe(true);
    expect(FakeAudioContext.instances.every((c) => c.state === 'closed')).toBe(true);
    expect(capture.isRecording.value).toBe(false);
    expect(capture.isCapturing.value).toBe(false);
    expect(capture.streamBitsPerSecond.value).toBe(mod.STREAM_AUDIO_BITS_PER_SECOND);
    // Ended within the first 5 s: no false "no audio" warning afterwards.
    await vi.advanceTimersByTimeAsync(2 * mod.NO_AUDIO_DATA_MS);
    expect(onError).not.toHaveBeenCalled();
    expect(onData).not.toHaveBeenCalled();
  });
});

describe('inputs', () => {
  it('lists real inputs, hides BlackHole and names unnamed ones', async () => {
    mediaDevices.enumerateDevices.mockResolvedValue([
      { kind: 'audioinput', deviceId: 'bh', label: 'BlackHole 2ch' },
      { kind: 'audioinput', deviceId: 'a', label: '' },
      { kind: 'videoinput', deviceId: 'cam', label: 'FaceTime' },
      { kind: 'audioinput', deviceId: 'x', label: 'Xone:23C' },
    ]);
    const capture = mod.useAudioCapture();
    await capture.refreshDevices();
    expect(capture.devices.value).toEqual([
      { deviceId: 'a', label: 'Audio Input 1' },
      { deviceId: 'x', label: 'Xone:23C' },
    ]);
    // The permission prompt's stream is not kept open.
    expect(device.tracks[0].readyState).toBe('ended');
  });

  it('shows why the device list failed', async () => {
    mediaDevices.getUserMedia.mockRejectedValueOnce(new Error('NotAllowedError'));
    const onError = vi.fn();
    const capture = mod.useAudioCapture({ onError });
    await capture.refreshDevices();
    expect(capture.error.value).toBe('NotAllowedError');
    expect(onError).toHaveBeenCalledWith('NotAllowedError');
  });

  it('captures only the audio of a shared screen', async () => {
    const video = new FakeTrack('video');
    mediaDevices.getDisplayMedia.mockResolvedValue(new FakeStream([new FakeTrack(), video]));
    const capture = mod.useAudioCapture();
    expect(await capture.captureScreenAudio()).toBe(true);
    expect(capture.selectedDeviceId.value).toBe('screen');
    expect(video.readyState).toBe('ended');
  });

  it('asks for "Share audio" when the shared screen has no sound', async () => {
    const video = new FakeTrack('video');
    mediaDevices.getDisplayMedia.mockResolvedValue(new FakeStream([video]));
    const capture = mod.useAudioCapture();
    expect(await capture.captureScreenAudio()).toBe(false);
    expect(capture.error.value).toBe(
      'No audio track. Make sure to check "Share audio" in the dialog.'
    );
    expect(video.readyState).toBe('ended');
  });

  it('explains a refused screen share', async () => {
    mediaDevices.getDisplayMedia.mockRejectedValue(
      Object.assign(new Error('denied'), { name: 'NotAllowedError' })
    );
    const onError = vi.fn();
    const capture = mod.useAudioCapture({ onError });
    expect(await capture.captureScreenAudio()).toBe(false);
    expect(onError).toHaveBeenCalledWith(
      'Permission denied. Please allow screen sharing with audio.'
    );
  });
});
