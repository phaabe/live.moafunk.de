import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// A WebSocket that the test drives: open, server frames, server close.
class FakeWebSocket {
  static OPEN = 1;
  static instances: FakeWebSocket[] = [];
  readyState = 0;
  binaryType = '';
  bufferedAmount = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((event: { code: number; reason: string }) => void) | null = null;
  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }
  send() {}
  close() {
    this.readyState = 3;
  }
  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }
  frame(data: string) {
    this.onmessage?.({ data });
  }
  serverClose(code: number) {
    this.readyState = 3;
    this.onclose?.({ code, reason: '' });
  }
}

type SocketModule = typeof import('../useStreamSocket');
let mod: SocketModule;

beforeEach(async () => {
  FakeWebSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeWebSocket);
  vi.spyOn(console, 'log').mockImplementation(() => {});
  // The composable keeps singleton state; every test gets a fresh module.
  vi.resetModules();
  mod = await import('../useStreamSocket');
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function goLive(showId: number) {
  const socket = mod.useStreamSocket();
  const connected = socket.connect(false, showId, false, true);
  const ws = FakeWebSocket.instances.at(-1)!;
  ws.open();
  await connected;
  ws.frame('connected');
  return { socket, ws };
}

describe('the live session owns the callbacks (#1063)', () => {
  it('keeps them while live, so another page cannot take them', async () => {
    const { ws } = await goLive(7);
    const session = { onError: vi.fn() };
    mod.ownStreamCallbacks(session);
    // A page opened while live, e.g. a Studio with its soundcheck.
    const page = { onError: vi.fn() };
    mod.useStreamSocket(page);
    mod.useStreamSocket();

    ws.frame('error: Another connection took over the stream');
    expect(session.onError).toHaveBeenCalledWith('Another connection took over the stream');
    expect(page.onError).not.toHaveBeenCalled();
  });

  it('lets Go back on air take them after a lost connection', async () => {
    const { socket, ws } = await goLive(7);
    mod.ownStreamCallbacks({ onError: vi.fn() });
    ws.frame('error: Another connection took over the stream');
    ws.serverClose(1000);
    // Lost, but not stopped: the show is still this tab's.
    expect(mod.liveStreamShowId()).toBeNull();
    expect(mod.streamShowId()).toBe(7);

    const goBack = { onLive: vi.fn() };
    mod.useStreamSocket(goBack);
    const again = socket.connect(false, 7, false, true);
    const ws2 = FakeWebSocket.instances.at(-1)!;
    ws2.open();
    await again;
    ws2.frame('connected');
    expect(goBack.onLive).toHaveBeenCalledOnce();
  });

  it('knows the show until the stream is stopped, and none for a test broadcast', async () => {
    const { socket } = await goLive(7);
    expect(mod.streamShowId()).toBe(7);
    socket.stopStream();
    expect(mod.streamShowId()).toBeNull();

    const test = socket.connect(false, undefined, true);
    FakeWebSocket.instances.at(-1)!.open();
    await test;
    expect(mod.streamShowId()).toBeNull();
  });
});
