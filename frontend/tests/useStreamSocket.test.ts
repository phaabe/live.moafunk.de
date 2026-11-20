import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  liveStreamShowId,
  MAX_PENDING_CHUNKS,
  parsePong,
  useStreamSocket,
  webmHeaderLength,
} from '../src/admin/composables/useStreamSocket';

describe('webmHeaderLength', () => {
  it('finds the first Cluster', () => {
    expect(webmHeaderLength(new Uint8Array([0x1a, 9, 0x1f, 0x43, 0xb6, 0x75, 0x1f]))).toBe(2);
  });

  it('is -1 before a Cluster arrives', () => {
    expect(webmHeaderLength(new Uint8Array([0x1a, 0x45, 0xdf, 0xa3, 0x1f, 0x43, 0xb6]))).toBe(-1);
  });
});

describe('parsePong', () => {
  it('parses a full server pong frame', () => {
    const msg =
      'pong:{"echo":12345.678,"chunks":40,"bytes":276000,"late":2,"stream":"s1","offset":9000}';
    expect(parsePong(msg)).toEqual({
      echoMs: 12345.678,
      chunks: 40,
      bytes: 276000,
      late: 2,
      stream: 's1',
      offset: 9000,
    });
  });

  it('defaults missing counters to zero', () => {
    expect(parsePong('pong:{"echo":1}')).toEqual({
      echoMs: 1,
      chunks: 0,
      bytes: 0,
      late: 0,
      stream: null,
      offset: 0,
    });
  });

  it('rejects frames without a numeric echo', () => {
    expect(parsePong('pong:{"echo":"nope"}')).toBeNull();
    expect(parsePong('pong:{"chunks":1}')).toBeNull();
  });

  it('rejects malformed JSON and non-pong frames', () => {
    expect(parsePong('pong:{oops')).toBeNull();
    expect(parsePong('connected')).toBeNull();
    expect(parsePong('error: nope')).toBeNull();
  });
});

/** Minimal stand-in for the browser WebSocket; tests drive its events by hand. */
class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  readyState = FakeWebSocket.CONNECTING;
  binaryType = 'blob';
  bufferedAmount = 0;
  sent: (string | ArrayBuffer)[] = [];
  closedWith: { code?: number; reason?: string } | null = null;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;

  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(data: string | ArrayBuffer): void {
    this.sent.push(data);
  }

  /** Client-initiated close; the close event is fired separately via drop(). */
  close(code?: number, reason?: string): void {
    this.closedWith = { code, reason };
    this.readyState = FakeWebSocket.CLOSING;
  }

  open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.(new Event('open'));
  }

  message(data: string): void {
    this.onmessage?.({ data } as MessageEvent);
  }

  /** Connection closes (1006 = abnormal: network drop, no close frame). */
  drop(code = 1006): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({ code, reason: '' } as CloseEvent);
  }

  /** Connection attempt fails: the browser fires error, then close. */
  fail(): void {
    this.onerror?.(new Event('error'));
    this.drop(1006);
  }
}

const sockets = FakeWebSocket.instances;
const latest = () => sockets[sockets.length - 1];

function goLive(ws: FakeWebSocket): void {
  ws.open();
  ws.message('connected');
}

describe('useStreamSocket auto-reconnect', () => {
  let stream: ReturnType<typeof useStreamSocket>;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal('WebSocket', FakeWebSocket);
    vi.spyOn(console, 'log').mockImplementation(() => {});
    vi.spyOn(console, 'error').mockImplementation(() => {});
    sockets.length = 0;
    stream = useStreamSocket();
  });

  afterEach(() => {
    // Module-level singleton state — reset it for the next test.
    stream.cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  /** Connect and bring the first socket to 'live'. */
  async function startLive(force = false, showId?: number, test?: boolean) {
    const connected = stream.connect(force, showId, test);
    goLive(sockets[0]);
    await connected;
    expect(stream.state.value).toBe('live');
  }

  it('keeps retrying while live, stops after End Episode', async () => {
    await startLive(true, 42);

    sockets[0].drop();
    expect(stream.state.value).toBe('connecting');

    // 1 s / 2 s / 4 s, then every 5 s, with no attempt limit; each attempt
    // reuses force + show_id.
    for (const [attempt, delay] of [1000, 2000, 4000, 5000, 5000, 5000, 5000, 5000].entries()) {
      const before = sockets.length;
      vi.advanceTimersByTime(delay - 1);
      expect(sockets).toHaveLength(before);
      vi.advanceTimersByTime(1);
      expect(sockets).toHaveLength(before + 1);
      expect(latest().url).toContain('force=true');
      expect(latest().url).toContain('show_id=42');
      expect(stream.reconnectAttempts.value).toBe(attempt + 1);

      latest().fail();
      expect(stream.state.value).toBe('connecting');
    }

    // End Episode during the backoff.
    stream.stopStream();
    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(9);
    expect(stream.state.value).toBe('disconnected');
  });

  it("a reconnect re-sends the recorder's WebM header before its audio", async () => {
    await startLive(false, 42);
    const head = new Uint8Array([0x1a, 0x45, 0xdf, 0xa3, 1, 2]).buffer;
    const rest = new Uint8Array([3, 0x1f, 0x43, 0xb6, 0x75, 7]).buffer;
    stream.send(head, true);
    stream.send(rest);
    const announce = sockets[0].sent[0] as string;
    expect(announce).toMatch(/^stream:\w+$/);
    expect(sockets[0].sent.slice(1)).toEqual([head, rest]);

    sockets[0].drop();
    vi.advanceTimersByTime(1000);
    expect(latest().url).toContain(`resume=${announce.slice('stream:'.length)}`);
    latest().open();
    // Nothing was confirmed, so both chunks follow the header again.
    expect(latest().sent).toEqual([
      new Uint8Array([0x1a, 0x45, 0xdf, 0xa3, 1, 2, 3]),
      'at:0',
      head,
      rest,
    ]);

    const next = new Uint8Array([8]).buffer;
    stream.send(next);
    expect(latest().sent[4]).toBe(next);

    // End Episode: the next show starts a stream of its own.
    stream.stopStream();
    const again = stream.connect(false, 43);
    goLive(latest());
    await again;
    expect(latest().url).not.toContain('resume=');
  });

  describe('chunks of a drop', () => {
    const header = [0x1a, 0x45, 0xdf, 0xa3];
    const cluster = [0x1f, 0x43, 0xb6, 0x75];
    const chunk = (...bytes: number[]) => new Uint8Array(bytes).buffer;

    /** Live, with a stream whose header is known; returns its id. */
    async function liveStream(): Promise<string> {
      await startLive(false, 42);
      stream.send(chunk(...header, ...cluster), true);
      return (sockets[0].sent[0] as string).slice('stream:'.length);
    }

    function confirm(ws: FakeWebSocket, id: string, offset: number) {
      ws.message(`pong:${JSON.stringify({ echo: 1, stream: id, offset })}`);
    }

    async function reconnect() {
      vi.advanceTimersByTime(1000);
      latest().open();
      latest().message('connected');
    }

    it('keeps them while reconnecting and sends them in order', async () => {
      const id = await liveStream();
      confirm(sockets[0], id, 8);
      sockets[0].drop();

      const missed = [chunk(1), chunk(2, 3)];
      for (const c of missed) expect(stream.send(c)).toBe(false);
      await reconnect();
      expect(latest().url).toContain(`resume=${id}`);
      expect(latest().sent).toEqual([new Uint8Array(header), 'at:8', ...missed]);

      const later = chunk(4);
      expect(stream.send(later)).toBe(true);
      expect(latest().sent.at(-1)).toBe(later);
    });

    it('re-sends from the last offset the server confirmed', async () => {
      const id = await liveStream();
      const inFlight = chunk(5, 6);
      stream.send(inFlight);
      // The pong confirms the first chunk only; the second may be lost.
      confirm(sockets[0], id, 8);
      confirm(sockets[0], 'another-stream', 10);
      sockets[0].drop();

      await reconnect();
      expect(latest().sent.slice(1)).toEqual(['at:8', inFlight]);
    });

    it('drops the oldest after about 10 minutes and warns', async () => {
      const id = await liveStream();
      confirm(sockets[0], id, 8);
      sockets[0].drop();
      for (let i = 0; i < MAX_PENDING_CHUNKS; i++) stream.send(chunk(i % 256));
      expect(stream.audioDropped.value).toBe(false);
      stream.send(chunk(7));
      expect(stream.audioDropped.value).toBe(true);

      await reconnect();
      // The first missed chunk is gone: the stream goes on from the second.
      expect(latest().sent[1]).toBe('at:9');
      expect(latest().sent).toHaveLength(2 + MAX_PENDING_CHUNKS);

      stream.stopStream();
      expect(stream.audioDropped.value).toBe(false);
    });
  });

  it('keeps test mode across reconnect attempts', async () => {
    await startLive(false, undefined, true);

    sockets[0].drop();
    vi.advanceTimersByTime(1000);
    latest().fail();
    vi.advanceTimersByTime(2000);

    expect(sockets).toHaveLength(3);
    for (const ws of sockets.slice(1)) {
      expect(ws.url).toContain('test=true');
      expect(ws.url).not.toContain('force=true');
    }
  });

  it('keeps "Record this show" off across reconnect attempts', async () => {
    const connected = stream.connect(false, 42, false, false);
    goLive(sockets[0]);
    await connected;
    expect(sockets[0].url).toContain('show_id=42');
    expect(sockets[0].url).toContain('record=false');

    sockets[0].drop();
    vi.advanceTimersByTime(1000);
    expect(sockets).toHaveLength(2);
    expect(latest().url).toContain('show_id=42');
    expect(latest().url).toContain('record=false');
  });

  it('records by default and after a stop', async () => {
    const connected = stream.connect(false, 42, false, false);
    goLive(sockets[0]);
    await connected;
    stream.stopStream();

    const again = stream.connect(false, 42);
    goLive(latest());
    await again;
    expect(latest().url).not.toContain('record=');
  });

  it('reports the live show while live or reconnecting, never for a test', async () => {
    expect(liveStreamShowId()).toBeNull();
    await startLive(false, 42);
    expect(liveStreamShowId()).toBe(42);

    sockets[0].drop();
    expect(liveStreamShowId()).toBe(42);
    stream.stopStream();
    expect(liveStreamShowId()).toBeNull();

    await startLiveAgain(true);
    expect(liveStreamShowId()).toBeNull();
  });

  async function startLiveAgain(test: boolean) {
    const connected = stream.connect(false, undefined, test);
    goLive(latest());
    await connected;
  }

  it('a successful reconnect resets the counter', async () => {
    const onLive = vi.fn();
    stream = useStreamSocket({ onLive });
    await startLive(false, 7);

    // Many drops in one show — each recovers.
    for (let drop = 1; drop <= 4; drop++) {
      latest().drop();
      expect(stream.state.value).toBe('connecting');
      expect(stream.reconnectAttempts.value).toBe(1);

      // Always the first (1 s) step of the backoff again.
      vi.advanceTimersByTime(1000);
      expect(sockets).toHaveLength(drop + 1);

      // An open socket isn't success yet; the producer going live is.
      latest().open();
      expect(stream.reconnectAttempts.value).toBe(1);
      latest().message('connected');
      expect(stream.state.value).toBe('live');
      expect(stream.reconnectAttempts.value).toBe(0);
    }

    expect(onLive).toHaveBeenCalledTimes(5);
    expect(stream.error.value).toBeNull();
  });

  it('an error: frame followed by a close does not reconnect', async () => {
    const onError = vi.fn();
    stream = useStreamSocket({ onError });
    await startLive(true, 42);

    sockets[0].message('error: Another connection took over the stream');
    sockets[0].drop(1005);

    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(1);
    expect(stream.reconnectAttempts.value).toBe(0);
    expect(stream.state.value).toBe('disconnected');
    expect(stream.error.value).toBe('Another connection took over the stream');
    expect(onError).toHaveBeenCalledWith('Another connection took over the stream');
  });

  it('an error: frame on a reconnect attempt stops the backoff', async () => {
    await startLive();

    sockets[0].drop();
    vi.advanceTimersByTime(1000);
    expect(sockets).toHaveLength(2);

    sockets[1].open();
    sockets[1].message('error: Another connection took over the stream');
    sockets[1].drop(1005);

    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(2);
    expect(stream.state.value).toBe('disconnected');
  });

  it('stopStream() sends stop and never reconnects', async () => {
    await startLive(false, 42);

    stream.stopStream();
    expect(sockets[0].sent).toContain('stop');
    expect(sockets[0].closedWith?.code).toBe(1000);

    // Even if the close arrives as abnormal (server dropped without a close frame).
    sockets[0].drop(1006);
    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(1);
    expect(stream.state.value).toBe('disconnected');
    expect(stream.reconnectAttempts.value).toBe(0);
  });

  it('stopStream() during the backoff cancels the pending attempt', async () => {
    await startLive();

    sockets[0].drop();
    vi.advanceTimersByTime(1000);
    sockets[1].fail(); // second attempt now pending (2 s)

    stream.stopStream();
    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(2);
    expect(stream.state.value).toBe('disconnected');
    expect(stream.reconnectAttempts.value).toBe(0);
  });

  it('stopStream() reports the disconnect at once, and only once', async () => {
    const onDisconnected = vi.fn();
    stream = useStreamSocket({ onDisconnected });
    await startLive();

    stream.stopStream();
    expect(onDisconnected).toHaveBeenCalledTimes(1);
    sockets[0].drop(1000);
    expect(onDisconnected).toHaveBeenCalledTimes(1);
  });

  it("a stopped soundcheck's late close leaves the Go Live connection alone", async () => {
    await startLive(false, undefined, true);
    stream.stopStream();

    // Go Live takes over the callbacks before the soundcheck's close arrives.
    const onDisconnected = vi.fn();
    stream = useStreamSocket({ onDisconnected });
    const connected = stream.connect(false, 42);
    goLive(sockets[1]);
    await connected;
    sockets[0].drop(1000);

    expect(onDisconnected).not.toHaveBeenCalled();
    expect(stream.state.value).toBe('live');
    // Its pings go on, and a drop still reconnects.
    vi.advanceTimersByTime(2000);
    expect(sockets[1].sent.some((m) => typeof m === 'string' && m.startsWith('ping:'))).toBe(true);
    sockets[1].drop();
    expect(stream.state.value).toBe('connecting');
  });

  /** Fires the page's beforeunload; true when the browser would ask "Leave site?". */
  function leaveAsks(): boolean {
    const event = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  }

  it('asks before leaving while on air, and Stay keeps the stream', async () => {
    await startLive(false, 42);
    expect(leaveAsks()).toBe(true);
    expect(sockets[0].closedWith).toBeNull();
    expect(stream.state.value).toBe('live');

    // Reconnecting is still on air.
    sockets[0].drop();
    expect(leaveAsks()).toBe(true);
  });

  it('does not ask for a soundcheck or when nothing is live', async () => {
    expect(leaveAsks()).toBe(false);
    await startLive(false, 42, true);
    expect(leaveAsks()).toBe(false);
  });

  it('closes without stop once the page goes', async () => {
    await startLive(false, 42);
    window.dispatchEvent(new Event('pagehide'));
    expect(sockets[0].closedWith).toEqual({ code: 1000, reason: 'Page unload' });
    expect(sockets[0].sent).not.toContain('stop');
  });
});
