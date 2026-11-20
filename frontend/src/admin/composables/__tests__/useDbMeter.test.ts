import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, defineComponent, nextTick, ref, type App, type Ref } from 'vue';
import { DB_FLOOR, dbToLevel, useDbMeter } from '../useDbMeter';

/** The signal the fake analyser hands out: sample i of n. */
let signal: (i: number, n: number) => number;
const silence = () => 0;
/** Full-scale sine, 8 whole periods per frame: RMS 1/√2 (−3.01 dBFS), peak 1 (0 dBFS). */
const fullSine = (i: number, n: number) => Math.sin((2 * Math.PI * 8 * i) / n);

class FakeAudioContext {
  static all: FakeAudioContext[] = [];
  static refuse: Error | null = null;
  state: AudioContextState = 'running';
  constructor() {
    FakeAudioContext.all.push(this);
  }
  createAnalyser() {
    return {
      fftSize: 0,
      smoothingTimeConstant: 1,
      getFloatTimeDomainData(samples: Float32Array) {
        for (let i = 0; i < samples.length; i++) samples[i] = signal(i, samples.length);
      },
    };
  }
  createMediaStreamSource() {
    if (FakeAudioContext.refuse) throw FakeAudioContext.refuse;
    return { connect() {}, disconnect() {} };
  }
  async close() {
    this.state = 'closed';
  }
}

let frames = new Map<number, FrameRequestCallback>();
let nextFrame = 1;
/** Run one animation frame at time `ts` (ms). */
function frame(ts: number) {
  const due = [...frames.values()];
  frames.clear();
  due.forEach((cb) => cb(ts));
}

let app: App | undefined;
function mountMeter(stream: Ref<MediaStream | null>) {
  let result!: ReturnType<typeof useDbMeter>;
  app = createApp(
    defineComponent({
      setup() {
        result = useDbMeter(stream);
        return () => null;
      },
    })
  );
  app.mount(document.createElement('div'));
  return result;
}

const input = () => ({}) as MediaStream;

beforeEach(() => {
  signal = silence;
  frames = new Map();
  FakeAudioContext.all = [];
  FakeAudioContext.refuse = null;
  vi.stubGlobal('AudioContext', FakeAudioContext);
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    frames.set(nextFrame, cb);
    return nextFrame++;
  });
  vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id));
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  app?.unmount();
  app = undefined;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('dB meter scale', () => {
  it('maps -60..0 dBFS onto the bar and clamps outside it', () => {
    expect(dbToLevel(-60)).toBe(0);
    expect(dbToLevel(-30)).toBe(0.5);
    expect(dbToLevel(0)).toBe(1);
    expect(dbToLevel(-90)).toBe(0);
    expect(dbToLevel(6)).toBe(1);
  });
});

describe('dB meter reading', () => {
  it('reads a full-scale sine as -3 dBFS with its peak at 0', () => {
    const meter = mountMeter(ref(input()));
    signal = fullSine;
    frame(16);
    expect(meter.db.value).toBeCloseTo(-3.01, 2);
    expect(meter.peakDb.value).toBeCloseTo(0, 5);
    expect(meter.level.value).toBeCloseTo(0.95, 2);
  });

  it('keeps silence at the floor', () => {
    const meter = mountMeter(ref(input()));
    frame(16);
    expect(meter.db.value).toBe(DB_FLOOR);
    expect(meter.peakDb.value).toBe(DB_FLOOR);
    expect(meter.level.value).toBe(0);
  });

  it('rises at once and falls a quarter of the way per frame', () => {
    const meter = mountMeter(ref(input()));
    signal = fullSine;
    frame(16);
    signal = silence;
    frame(32);
    // -3.01 + (-60 - -3.01) * 0.25
    expect(meter.db.value).toBeCloseTo(-17.26, 2);
  });

  it('holds the peak for 1.2 s, then lets it fall 12 dB per second', () => {
    const meter = mountMeter(ref(input()));
    signal = fullSine;
    frame(1000);
    signal = silence;
    frame(2000);
    expect(meter.peakDb.value).toBeCloseTo(0, 5);
    frame(2300);
    expect(meter.peakDb.value).toBeCloseTo(-3.6, 5);
    frame(2800);
    expect(meter.peakDb.value).toBeCloseTo(-9.6, 5);
  });
});

describe('dB meter lifecycle', () => {
  it('restarts on a new input and resets when the input goes away', async () => {
    const stream = ref<MediaStream | null>(input());
    const meter = mountMeter(stream);
    signal = fullSine;
    frame(16);
    expect(meter.db.value).toBeGreaterThan(-4);

    stream.value = input();
    await nextTick();
    expect(FakeAudioContext.all.map((c) => c.state)).toEqual(['closed', 'running']);
    expect(meter.db.value).toBe(DB_FLOOR);

    stream.value = null;
    await nextTick();
    expect(FakeAudioContext.all[1].state).toBe('closed');
    expect(frames.size).toBe(0);
    expect(meter.level.value).toBe(0);
  });

  it('stops when the page goes away', () => {
    mountMeter(ref(input()));
    app!.unmount();
    app = undefined;
    expect(FakeAudioContext.all[0].state).toBe('closed');
    expect(frames.size).toBe(0);
  });

  it('stays at the floor and does not break the page when the audio graph fails', () => {
    FakeAudioContext.refuse = new Error(
      'Connecting AudioNodes from AudioContexts with different sample-rate is currently not supported.'
    );
    const meter = mountMeter(ref(input()));
    expect(meter.db.value).toBe(DB_FLOOR);
    expect(frames.size).toBe(0);
  });
});
