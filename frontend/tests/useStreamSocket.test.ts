import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { parsePong, useStreamSocket } from '../src/admin/composables/useStreamSocket';

describe('parsePong', () => {
  it('parses a full server pong frame', () => {
    const msg = 'pong:{"echo":12345.678,"chunks":40,"bytes":276000,"late":2}';
    expect(parsePong(msg)).toEqual({
      echoMs: 12345.678,
      chunks: 40,
      bytes: 276000,
      late: 2,
    });
  });

  it('defaults missing counters to zero', () => {
    expect(parsePong('pong:{"echo":1}')).toEqual({
      echoMs: 1,
      chunks: 0,
      bytes: 0,
      late: 0,
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

  it('a failed first reconnect still schedules the second and third attempts', async () => {
    await startLive(true, 42);

    sockets[0].drop();
    expect(stream.state.value).toBe('connecting');
    expect(stream.reconnectAttempts.value).toBe(1);

    // 1 s / 2 s / 4 s backoff, each attempt reusing force + show_id.
    for (const [attempt, delay] of [
      [1, 1000],
      [2, 2000],
      [3, 4000],
    ]) {
      const before = sockets.length;
      vi.advanceTimersByTime(delay - 1);
      expect(sockets).toHaveLength(before);
      vi.advanceTimersByTime(1);
      expect(sockets).toHaveLength(before + 1);
      expect(latest().url).toContain('force=true');
      expect(latest().url).toContain('show_id=42');
      expect(stream.reconnectAttempts.value).toBe(attempt);

      latest().fail();
      if (attempt < 3) {
        expect(stream.state.value).toBe('connecting');
        expect(stream.reconnectAttempts.value).toBe(attempt + 1);
      }
    }

    expect(stream.state.value).toBe('error');
    expect(stream.error.value).toBe('Connection lost after multiple attempts');
    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(4); // the original + 3 attempts, no more
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

  it('a successful reconnect resets the counter', async () => {
    const onLive = vi.fn();
    stream = useStreamSocket({ onLive });
    await startLive(false, 7);

    // More drops than maxReconnectAttempts in one show — each recovers.
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

  it('tells onDisconnected whether an auto-reconnect is scheduled', async () => {
    const onDisconnected = vi.fn();
    stream = useStreamSocket({ onDisconnected });
    await startLive();

    sockets[0].drop();
    expect(onDisconnected).toHaveBeenLastCalledWith(true);

    // Each failed attempt still has another one queued…
    for (const delay of [1000, 2000]) {
      vi.advanceTimersByTime(delay);
      latest().fail();
      expect(onDisconnected).toHaveBeenLastCalledWith(true);
    }

    // …until the last one fails and the socket gives up.
    vi.advanceTimersByTime(4000);
    latest().fail();
    expect(onDisconnected).toHaveBeenLastCalledWith(false);
    expect(onDisconnected).toHaveBeenCalledTimes(4);
  });

  it('reports error-frame closes and stopStream() as final disconnects', async () => {
    const onDisconnected = vi.fn();
    stream = useStreamSocket({ onDisconnected });
    await startLive();

    sockets[0].message('error: Another connection took over the stream');
    sockets[0].drop(1005);
    expect(onDisconnected).toHaveBeenLastCalledWith(false);

    const connected = stream.connect();
    goLive(sockets[1]);
    await connected;
    stream.stopStream();
    sockets[1].drop(1006);
    expect(onDisconnected).toHaveBeenLastCalledWith(false);
  });
});
