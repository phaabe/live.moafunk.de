import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import pageHtml from '../index.html?raw';

const { cfg, device, flv } = vi.hoisted(() => {
  const flvPlayer = {
    on: vi.fn(),
    attachMediaElement: vi.fn(),
    load: vi.fn(),
    pause: vi.fn(),
    unload: vi.fn(),
    destroy: vi.fn(),
  };
  return {
    cfg: {
      stream: {
        hls: 'https://stream.test/live/index.m3u8',
        flv: 'https://stream.test/live.flv',
        icecast: '',
        icecastTestUrl: '',
        statusUrl: '',
      },
      analytics: { domain: 'test', scriptUrl: '' },
    },
    device: { ios: false },
    flv: { supported: false, player: flvPlayer, createPlayer: vi.fn(() => flvPlayer) },
  };
});
vi.mock('../config', () => ({ config: cfg }));
vi.mock('../streamDetector', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../streamDetector')>()),
  isIOSDevice: () => device.ios,
}));
vi.mock('flv.js', () => ({
  default: { isSupported: () => flv.supported, createPlayer: flv.createPlayer },
}));
vi.mock('../versionWatcher', () => ({ startVersionWatcher: vi.fn() }));

const ICECAST = 'https://icecast.test/live.mp3';

let player: typeof import('../player');
const audio = () => document.getElementById('player') as HTMLAudioElement;
const button = () => document.getElementById('btn-play') as HTMLButtonElement;
const status = () => document.getElementById('status') as HTMLElement;

beforeEach(async () => {
  cfg.stream.icecast = ICECAST;
  cfg.stream.statusUrl = '';
  device.ios = false;
  flv.supported = false;
  Object.values(flv.player).forEach((fn) => fn.mockReset());
  flv.createPlayer.mockClear();
  // jsdom does not implement media playback.
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined);
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
  vi.spyOn(console, 'log').mockImplementation(() => {});

  document.body.innerHTML = new DOMParser().parseFromString(pageHtml, 'text/html').body.innerHTML;
  vi.resetModules();
  player = await import('../player');
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('live status', () => {
  it('shows "Live now" while a show is on', () => {
    player.updateLiveStatus(true);
    expect(status().textContent).toBe('Live now');
  });

  it('shows "Off air" and where shows are announced', () => {
    player.updateLiveStatus(false);
    expect(status().textContent).toContain('Off air');
    expect(status().textContent).toContain('Tele- and Instagram');
  });
});

describe('start and stop (Icecast)', () => {
  beforeEach(() => {
    player.updateLiveStatus(true);
    player.initializePlayer();
  });

  it('loads the Icecast MP3 mount into the audio player', () => {
    expect(audio().src).toBe(ICECAST);
  });

  it('starts the audio on Play and shows the pause button', () => {
    player.play();
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1);
    expect(button().className).toBe('btn btn-pause');
  });

  it('pauses on the second press and shows the play button again', () => {
    player.play();
    player.play();
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledTimes(1);
    expect(button().className).toBe('btn');
  });

  it('gives the page button its onclick="play()"', () => {
    expect(button().getAttribute('onclick')).toBe('play()');
    expect((window as unknown as { play: unknown }).play).toBe(player.play);
  });

  it('does not start while off air', () => {
    player.updateLiveStatus(false);
    player.play();
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    expect(button().className).toBe('btn');
  });

  it('stops the audio and drops the stream when the show ends', () => {
    player.play();
    player.destroyPlayer();
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalled();
    expect(audio().hasAttribute('src')).toBe(false);
    expect(button().className).toBe('btn');
  });

  it('loads the stream again when the next show starts', () => {
    player.destroyPlayer();
    player.restartPlayer();
    expect(audio().src).toBe(ICECAST);
  });

  it('keeps the button usable after a stream error', () => {
    audio().dispatchEvent(new Event('error'));
    player.play();
    expect(button().className).toBe('btn btn-pause');
  });

  // Today a stalled stream stays silent until the listener presses Play again.
  // https://github.com/phaabe/live.moafunk.de/issues/368
  it.skip('retries a stalled stream without a new press on Play', async () => {
    vi.useFakeTimers();
    player.play();
    audio().dispatchEvent(new Event('stalled'));
    await vi.advanceTimersByTimeAsync(10_000);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(2);
  });
});

describe('other platforms (no Icecast)', () => {
  beforeEach(() => {
    cfg.stream.icecast = '';
  });

  it('plays HLS in the audio player on iOS and drops it when the show ends', () => {
    device.ios = true;
    player.initializePlayer();
    expect(audio().src).toBe(cfg.stream.hls);
    player.destroyPlayer();
    expect(audio().hasAttribute('src')).toBe(false);
  });

  it('plays FLV in the video element elsewhere and frees it when the show ends', () => {
    flv.supported = true;
    player.updateLiveStatus(true);
    player.initializePlayer();
    expect(flv.createPlayer).toHaveBeenCalledWith({ type: 'flv', url: cfg.stream.flv });
    expect(flv.player.attachMediaElement).toHaveBeenCalledWith(
      document.getElementById('videoElement')
    );
    expect(flv.player.load).toHaveBeenCalled();

    player.play();
    expect(button().className).toBe('btn btn-pause');

    player.destroyPlayer();
    expect(flv.player.unload).toHaveBeenCalled();
    expect(flv.player.destroy).toHaveBeenCalled();
    expect(button().className).toBe('btn');
  });

  it('still resets the player when freeing FLV fails', () => {
    flv.supported = true;
    player.initializePlayer();
    flv.player.destroy.mockImplementation(() => {
      throw new Error('already destroyed');
    });
    player.destroyPlayer();
    player.restartPlayer();
    expect(flv.createPlayer).toHaveBeenCalledTimes(2);
  });

  it('does nothing on Play when the browser can play neither', () => {
    player.updateLiveStatus(true);
    player.initializePlayer();
    player.play();
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    expect(button().className).toBe('btn');
  });
});

describe('status poll on the public page', () => {
  const live = (active: boolean) => new Response(JSON.stringify({ active }), { status: 200 });

  async function openPage(...responses: Array<Response | Error>): Promise<void> {
    cfg.stream.statusUrl = 'https://api.test/api/stream/status';
    const fetchMock = vi.fn();
    for (const r of responses) {
      if (r instanceof Error) fetchMock.mockRejectedValueOnce(r);
      else fetchMock.mockResolvedValueOnce(r);
    }
    vi.stubGlobal('fetch', fetchMock);
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.useFakeTimers();
    await import('../main');
    await vi.advanceTimersByTimeAsync(0);
    player.play();
  }

  it('starts the player when the page opens during a show', async () => {
    await openPage(live(true));
    expect(status().textContent).toBe('Live now');
    expect(audio().src).toBe(ICECAST);
    expect(button().className).toBe('btn btn-pause');
  });

  it('stops the audio when the show ends', async () => {
    await openPage(live(true), live(false));
    await vi.advanceTimersByTimeAsync(8000);
    expect(status().textContent).toContain('Off air');
    expect(audio().hasAttribute('src')).toBe(false);
    expect(button().className).toBe('btn');
  });

  it('loads the stream when a show starts while the page is open', async () => {
    await openPage(live(false), live(true));
    expect(audio().hasAttribute('src')).toBe(false);
    await vi.advanceTimersByTimeAsync(8000);
    expect(status().textContent).toBe('Live now');
    expect(audio().src).toBe(ICECAST);
  });

  // Today one failed status check counts as "off air" and stops the listener's audio.
  // https://github.com/phaabe/live.moafunk.de/issues/367
  it.skip('keeps the audio playing when one status check fails', async () => {
    await openPage(live(true), new TypeError('Failed to fetch'));
    await vi.advanceTimersByTimeAsync(8000);
    expect(audio().src).toBe(ICECAST);
    expect(button().className).toBe('btn btn-pause');
  });
});
