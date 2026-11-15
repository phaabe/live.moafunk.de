import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick, ref, type App } from 'vue';

// A stand-in for wavesurfer: records how the player loads a file.
const fake = vi.hoisted(() => {
  type Handler = (...args: unknown[]) => void;
  class FakeWaveSurfer {
    handlers = new Map<string, Handler[]>();
    playing = false;
    duration = 0;
    constructor(public options: Record<string, unknown>) {}
    on(event: string, handler: Handler) {
      this.handlers.set(event, [...(this.handlers.get(event) ?? []), handler]);
    }
    emit(event: string, ...args: unknown[]) {
      for (const handler of this.handlers.get(event) ?? []) handler(...args);
    }
    load = vi.fn((_url: string, _peaks?: Float32Array[], duration?: number) => {
      this.duration = duration ?? 180;
      return Promise.resolve();
    });
    getDuration = () => this.duration;
    getCurrentTime = () => 0;
    setVolume = vi.fn();
    play = vi.fn(() => {
      this.playing = true;
      this.emit('play');
      return Promise.resolve();
    });
    pause = vi.fn(() => {
      this.playing = false;
      this.emit('pause');
    });
    playPause = () => (this.playing ? this.pause() : this.play());
    isPlaying = () => this.playing;
    destroy = vi.fn();
  }
  const instances: FakeWaveSurfer[] = [];
  const create = vi.fn((options: Record<string, unknown>) => {
    const ws = new FakeWaveSurfer(options);
    instances.push(ws);
    return ws;
  });
  return { instances, create };
});

vi.mock('wavesurfer.js', () => ({ default: { create: fake.create } }));

import AudioPlayer from '../components/AudioPlayer.vue';

const SET_URL = 'https://r2.example/brunchtime-0811.mp3?X-Amz-Signature=abc';
const TRACK_URL = 'https://r2.example/track1.mp3?X-Amz-Signature=def';

let apps: App[] = [];
let probes: HTMLAudioElement[] = [];

beforeEach(() => {
  fake.instances.length = 0;
  fake.create.mockClear();
  probes = [];
  // Catch the audio element that reads the file header.
  const createElement = document.createElement.bind(document);
  vi.spyOn(document, 'createElement').mockImplementation(
    (tag: string, options?: ElementCreationOptions) => {
      const el = createElement(tag, options);
      if (tag === 'audio') probes.push(el as HTMLAudioElement);
      return el;
    }
  );
  // jsdom does not implement media loading.
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
});

afterEach(() => {
  apps.forEach((app) => app.unmount());
  apps = [];
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

function mount(src: string | { value: string }): HTMLElement {
  const root = document.createElement('div');
  document.body.appendChild(root);
  const app = createApp({
    render: () => h(AudioPlayer, { src: typeof src === 'string' ? src : src.value }),
  });
  app.mount(root);
  apps.push(app);
  return root;
}

async function flush(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  await nextTick();
}

function playButton(root: HTMLElement): HTMLButtonElement {
  return root.querySelector('.play-btn') as HTMLButtonElement;
}

/** The header arrives: the browser now knows the length of the file. */
async function headerLoaded(probe: HTMLAudioElement, secs: number): Promise<void> {
  Object.defineProperty(probe, 'duration', { value: secs, configurable: true });
  probe.dispatchEvent(new Event('loadedmetadata'));
  await flush();
}

describe('AudioPlayer · loading a file', () => {
  it('plays a 2 h set while it streams, without downloading it first', async () => {
    const root = mount(SET_URL);
    playButton(root).click();
    await flush();

    // Only the header is read first; wavesurfer is not created yet.
    expect(probes).toHaveLength(1);
    expect(probes[0].preload).toBe('metadata');
    expect(probes[0].getAttribute('src')).toBe(SET_URL);
    expect(fake.create).not.toHaveBeenCalled();
    expect(playButton(root).disabled).toBe(true);

    await headerLoaded(probes[0], 7200);

    // Peaks and length are given, so wavesurfer neither fetches nor decodes the file.
    const [ws] = fake.instances;
    expect(ws.options.normalize).toBe(false);
    expect(ws.load).toHaveBeenCalledTimes(1);
    const [url, peaks, duration] = ws.load.mock.calls[0];
    expect(url).toBe(SET_URL);
    expect(duration).toBe(7200);
    expect(peaks).toHaveLength(1);
    expect(peaks![0].length).toBeGreaterThan(0);
    expect(new Set(peaks![0]).size).toBe(1);
    // The header probe is stopped and does not keep downloading.
    expect(probes[0].hasAttribute('src')).toBe(false);

    ws.emit('ready');
    await nextTick();
    expect(root.querySelector('.duration')?.textContent).toBe('120:00');
    expect(ws.play).toHaveBeenCalledTimes(1);
    expect(playButton(root).getAttribute('aria-label')).toBe('Pause');
  });

  it('draws the real waveform of a short track', async () => {
    const root = mount(TRACK_URL);
    playButton(root).click();
    await flush();
    await headerLoaded(probes[0], 180);

    const [ws] = fake.instances;
    expect(ws.options.normalize).toBe(true);
    // No peaks: wavesurfer downloads and decodes the track.
    expect(ws.load.mock.calls).toEqual([[TRACK_URL]]);

    ws.emit('ready');
    await nextTick();
    expect(root.querySelector('.duration')?.textContent).toBe('3:00');
    expect(ws.play).toHaveBeenCalledTimes(1);
  });

  it('pauses the other player when one starts', async () => {
    const first = mount(SET_URL);
    const second = mount(TRACK_URL);

    playButton(first).click();
    await flush();
    await headerLoaded(probes[0], 7200);
    fake.instances[0].emit('ready');

    playButton(second).click();
    await flush();
    await headerLoaded(probes[1], 180);
    fake.instances[1].emit('ready');
    await nextTick();

    expect(fake.instances[0].pause).toHaveBeenCalledTimes(1);
    expect(playButton(first).getAttribute('aria-label')).toBe('Play');
    expect(playButton(second).getAttribute('aria-label')).toBe('Pause');
  });

  it('lets the host press Play again when the file does not load', async () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    const root = mount(SET_URL);
    playButton(root).click();
    await flush();

    probes[0].dispatchEvent(new Event('error'));
    await flush();

    expect(fake.create).not.toHaveBeenCalled();
    expect(error).toHaveBeenCalled();
    expect(playButton(root).disabled).toBe(false);

    playButton(root).click();
    await flush();
    expect(probes).toHaveLength(2);
  });

  it('drops a load when another file comes in while the header loads', async () => {
    const src = ref(SET_URL);
    const root = mount(src);
    playButton(root).click();
    await flush();

    src.value = TRACK_URL;
    await nextTick();
    await headerLoaded(probes[0], 7200);

    expect(fake.create).not.toHaveBeenCalled();
    expect(playButton(root).disabled).toBe(false);
  });
});
