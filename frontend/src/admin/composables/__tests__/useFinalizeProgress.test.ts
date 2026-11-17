import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, defineComponent, type App } from 'vue';
import {
  useFinalizeProgress,
  type FinalizeProgressMessage,
  type UseFinalizeProgressOptions,
} from '../useFinalizeProgress';

class FakeSocket {
  static all: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onclose: ((event: { code: number; reason: string }) => void) | null = null;
  closed = false;
  constructor(public url: string) {
    FakeSocket.all.push(this);
  }
  close() {
    this.closed = true;
  }
  progress(msg: FinalizeProgressMessage) {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
  drop(code = 1006) {
    this.onclose?.({ code, reason: '' });
  }
}

const latest = () => FakeSocket.all.at(-1)!;

let app: App | undefined;

/** Run the composable inside a component so unmount cleanup is real. */
function mountFinalize(options: UseFinalizeProgressOptions = {}) {
  let result!: ReturnType<typeof useFinalizeProgress>;
  app = createApp(
    defineComponent({
      setup() {
        result = useFinalizeProgress(options);
        return () => null;
      },
    })
  );
  app.mount(document.createElement('div'));
  return result;
}

beforeEach(() => {
  vi.useFakeTimers();
  FakeSocket.all = [];
  vi.stubGlobal('WebSocket', FakeSocket);
  vi.spyOn(console, 'log').mockImplementation(() => {});
  vi.spyOn(console, 'warn').mockImplementation(() => {});
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  app?.unmount();
  app = undefined;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('finalize progress', () => {
  it('walks the host through download, merge and upload to done', () => {
    const onStarted = vi.fn();
    const onComplete = vi.fn();
    const f = mountFinalize({ onStarted, onComplete });
    f.startFinalize(48, '2026-10-30 20:00');

    expect(onStarted).toHaveBeenCalled();
    expect(latest().url).toBe(
      `ws://${window.location.host}/ws/recording/finalize?show_id=48&version=2026-10-30%2020%3A00`
    );
    expect(f.isRunning.value).toBe(true);
    expect(f.detail.value).toBe('Connecting...');
    latest().onopen?.();
    expect(f.detail.value).toBe('Connected, starting finalize...');

    const steps: [FinalizeProgressMessage, number, string][] = [
      [{ phase: 'downloading', percent: 50, detail: 'raw.webm' }, 25, 'Downloading files...'],
      [{ phase: 'merging', percent: 50, detail: 'mixing' }, 70, 'Merging audio...'],
      [{ phase: 'uploading', percent: 50, detail: 'final.mp3' }, 95, 'Uploading result...'],
    ];
    for (const [msg, overall, label] of steps) {
      latest().progress(msg);
      expect(f.progressPercent.value).toBe(overall);
      expect(f.phaseLabel.value).toBe(label);
      expect(f.detail.value).toBe(msg.detail);
    }

    latest().progress({
      phase: 'complete',
      percent: 100,
      detail: 'Recording finalized: shows/UNHEARD/2026-10-30-robi/final.mp3',
    });
    expect(f.isComplete.value).toBe(true);
    expect(f.progressPercent.value).toBe(100);
    expect(f.phaseLabel.value).toBe('Complete!');
    expect(onComplete).toHaveBeenCalledWith('shows/UNHEARD/2026-10-30-robi/final.mp3');
    expect(latest().closed).toBe(true);
  });

  it('shows the backend error and stops', async () => {
    const onError = vi.fn();
    const f = mountFinalize({ onError });
    f.startFinalize(48, 'v1');
    latest().progress({ phase: 'error', percent: 0, detail: 'ffmpeg merge failed' });

    expect(f.isError.value).toBe(true);
    expect(f.error.value).toBe('ffmpeg merge failed');
    expect(f.phaseLabel.value).toBe('Error');
    expect(f.progressPercent.value).toBe(0);
    expect(onError).toHaveBeenCalledWith('ffmpeg merge failed');
    expect(latest().closed).toBe(true);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeSocket.all).toHaveLength(1);
  });

  it('marks a finalize that resumed after a crash', () => {
    const f = mountFinalize();
    f.startFinalize(48, 'v1');
    latest().progress({ phase: 'merging', percent: 10, detail: 'resumed', resumed: true });
    expect(f.isResumed.value).toBe(true);
  });

  it('ignores a garbled message and keeps running', () => {
    const f = mountFinalize();
    f.startFinalize(48, 'v1');
    latest().onmessage?.({ data: 'not json' });
    expect(f.isRunning.value).toBe(true);
    expect(f.phase.value).toBeNull();
  });

  it('does not open a second connection while one finalize runs', () => {
    const f = mountFinalize();
    f.startFinalize(48, 'v1');
    f.startFinalize(48, 'v2');
    expect(FakeSocket.all).toHaveLength(1);
  });
});

describe('a dropped finalize connection', () => {
  it('reconnects after 1 s, then 2 s, and carries on', async () => {
    const onComplete = vi.fn();
    const f = mountFinalize({ onComplete });
    f.startFinalize(48, 'v1');
    latest().onopen?.();
    latest().progress({ phase: 'merging', percent: 40, detail: 'mixing' });

    latest().drop();
    expect(f.detail.value).toBe('Reconnecting (attempt 1/3)...');
    await vi.advanceTimersByTimeAsync(999);
    expect(FakeSocket.all).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(FakeSocket.all).toHaveLength(2);
    expect(latest().url).toBe(FakeSocket.all[0].url);

    // The retry fails too: the wait doubles.
    latest().drop();
    expect(f.detail.value).toBe('Reconnecting (attempt 2/3)...');
    await vi.advanceTimersByTimeAsync(1999);
    expect(FakeSocket.all).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(FakeSocket.all).toHaveLength(3);

    // Back online: the attempt budget is reset and progress goes on.
    latest().onopen?.();
    expect(f.reconnectAttempts.value).toBe(0);
    latest().progress({ phase: 'complete', percent: 100, detail: 'Recording finalized: k.mp3' });
    expect(onComplete).toHaveBeenCalledWith('k.mp3');
  });

  it('gives up after 3 retries and tells the host', async () => {
    const onError = vi.fn();
    const onDisconnected = vi.fn();
    const f = mountFinalize({ onError, onDisconnected });
    f.startFinalize(48, 'v1');
    for (const wait of [1000, 2000, 4000]) {
      latest().drop();
      await vi.advanceTimersByTimeAsync(wait);
    }
    expect(FakeSocket.all).toHaveLength(4);
    latest().drop();

    expect(f.isError.value).toBe(true);
    expect(f.error.value).toBe('Connection lost after multiple attempts');
    expect(onError).toHaveBeenCalledWith('Connection lost after multiple attempts');
    expect(onDisconnected).toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeSocket.all).toHaveLength(4);
  });

  it('treats a clean close before the end as an error', () => {
    const onDisconnected = vi.fn();
    const f = mountFinalize({ onDisconnected });
    f.startFinalize(48, 'v1');
    latest().drop(1000);
    expect(f.isError.value).toBe(true);
    expect(f.error.value).toBe('Connection closed unexpectedly');
    expect(onDisconnected).toHaveBeenCalled();
  });

  it('uses the configured retry budget and delay', async () => {
    const f = mountFinalize({ maxReconnectAttempts: 1, reconnectDelayMs: 50 });
    f.startFinalize(48, 'v1');
    latest().drop();
    await vi.advanceTimersByTimeAsync(50);
    expect(FakeSocket.all).toHaveLength(2);
    latest().drop();
    expect(f.isError.value).toBe(true);
  });
});

describe('stopping a finalize', () => {
  it('cancel closes the connection and drops a pending retry', async () => {
    const f = mountFinalize();
    f.startFinalize(48, 'v1');
    const first = latest();
    first.drop();
    f.cancel();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeSocket.all).toHaveLength(1);
    expect(first.closed).toBe(true);
    expect(f.isIdle.value).toBe(true);
    expect(f.detail.value).toBe('');
  });

  it('reset after an error lets the host start again', () => {
    const f = mountFinalize();
    f.startFinalize(48, 'v1');
    latest().progress({ phase: 'error', percent: 0, detail: 'boom' });
    f.reset();
    expect(f.isIdle.value).toBe(true);
    expect(f.error.value).toBeNull();
    f.startFinalize(48, 'v1');
    expect(FakeSocket.all).toHaveLength(2);
  });

  it('leaving the page closes the connection without a retry', async () => {
    const f = mountFinalize();
    f.startFinalize(48, 'v1');
    const socket = latest();
    app!.unmount();
    app = undefined;
    expect(socket.closed).toBe(true);
    // The close handler is detached, so a late close event cannot reconnect.
    expect(socket.onclose).toBeNull();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeSocket.all).toHaveLength(1);
  });
});
