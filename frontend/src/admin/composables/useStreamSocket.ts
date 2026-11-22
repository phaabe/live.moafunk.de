import { ref } from 'vue';

export type StreamConnectionState = 'disconnected' | 'connecting' | 'connected' | 'live' | 'error';

export interface UseStreamSocketOptions {
  maxReconnectAttempts?: number;
  onConnected?: () => void;
  onDisconnected?: () => void;
  onError?: (error: string) => void;
  onLive?: () => void;
}

// ─── Singleton state (shared across components / route navigations) ─────────
const state = ref<StreamConnectionState>('disconnected');
const error = ref<string | null>(null);
const reconnectAttempts = ref(0);

let socket: WebSocket | null = null;
let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;
// True from an unexpected drop of a live stream until it is live again, the
// server rejects an attempt with an `error:` frame, or we give up / the user
// stops. A failed attempt never reaches 'live' (onerror leaves it at 'error'),
// so this flag, not the state, keeps the backoff going past the first retry.
let autoReconnecting = false;
// Total binary payload bytes handed to socket.send() since (re)connect.
// Together with bufferedAmount this is the browser-side upload-health signal
// (#275): egress = Δqueued − Δbuffered, buffer-seconds = buffered / byte-rate.
let bytesQueued = 0;
// App-level ping/pong telemetry (#277) — browsers can't send protocol pings
// from JS, so `ping:<performance.now()>` text frames measure RTT and carry
// back the server's per-connection delivery counters.
const PING_INTERVAL_MS = 2000;
let pingInterval: ReturnType<typeof setInterval> | null = null;
let rttMs: number | null = null;
let serverStats = { chunks: 0, bytes: 0, late: 0 };
// Remembered across reconnects so the backend keeps auto-recording the same show.
let currentShowId: number | null = null;
// Remembered across reconnects so a rehearsal stays on the private `/test` mount.
// Without this, an auto-reconnect (which calls connect() with no flags) would
// silently fall back to the public `/live` producer.
let currentTest = false;
// "Record this show", remembered so an auto-reconnect never starts recording
// a show the host chose not to record.
let currentRecord = true;
// The recorder's current WebM stream. Each connection gets a new producer
// ffmpeg, which needs the stream's header (the bytes before its first
// Cluster) before any audio, so a reconnect re-sends the header and the
// stream goes on where it is.
let webmStreamId: string | null = null;
let webmHeader: Uint8Array | null = null;
// The stream's bytes so far while its first Cluster has not arrived.
let webmHead = new Uint8Array(0);
// A header is a few hundred bytes; stop looking for it after this.
const WEBM_HEADER_SCAN_LIMIT = 64 * 1024;
// Chunks of the stream the server has not confirmed yet (pong `offset`),
// with their byte offsets. A drop keeps them; the reconnect re-sends them, so
// the recording has no gap.
let pending: { offset: number; data: ArrayBuffer }[] = [];
let webmOffset = 0;
/** About 10 minutes of the recorder's 250 ms chunks. Older ones are dropped. */
export const MAX_PENDING_CHUNKS = 2400;
/** True once a drop lasted so long that audio was dropped from the buffer. */
const audioDropped = ref(false);
let currentCallbacks: {
  onConnected?: () => void;
  onDisconnected?: () => void;
  onError?: (error: string) => void;
  onLive?: () => void;
} = {};
// Set while the live session (liveSession.ts) owns the callbacks.
let callbacksOwned = false;

/**
 * Give the callbacks to the live session (null lets go). While this tab is
 * live or reconnecting, other useStreamSocket() calls then keep them: a page
 * that calls it cannot take them. After a lost connection Go back on air may
 * take them; the session takes them back when the show is live again.
 */
export function ownStreamCallbacks(callbacks: UseStreamSocketOptions | null): void {
  callbacksOwned = callbacks !== null;
  if (callbacks) {
    const { onConnected, onDisconnected, onError, onLive } = callbacks;
    currentCallbacks = { onConnected, onDisconnected, onError, onLive };
  }
}

// ─── Page unload: close the socket without 'stop' ───────────────────────────
// The server stops the stream on any close. Without 'stop' it treats a reload
// like a dropped connection: the episode stays on air for the reconnect grace,
// so the host can open the Studio again and go back on air.
// While on air, the browser asks first ("Leave site?"). The close waits for
// pagehide: closing in beforeunload would end the stream even when the host
// clicks Stay, and a 1000 close never reconnects.
if (typeof window !== 'undefined') {
  window.addEventListener('beforeunload', (event) => {
    if (liveStreamShowId() === null) return;
    event.preventDefault();
    // Some browsers show the prompt only when returnValue is set.
    event.returnValue = '';
  });
  window.addEventListener('pagehide', () => {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.close(1000, 'Page unload');
    }
  });
}

export interface StreamSocketStats {
  /** Whether a socket exists and is OPEN. */
  connected: boolean;
  /** Bytes accepted by send() but not yet handed to the OS/network. */
  bufferedAmount: number;
  /** Total binary payload bytes queued since the socket (re)connected. */
  bytesQueued: number;
  /** Last measured round-trip time (ms), or null before the first pong (#277). */
  rttMs: number | null;
  /** Chunks the server confirmed received on this connection. */
  serverChunks: number;
  /** Payload bytes the server confirmed received on this connection. */
  serverBytes: number;
  /** Chunks that arrived after a >1 s cadence gap (server-side). */
  serverLate: number;
}

/** Server pong frame: `pong:{"echo":…,"chunks":…,"bytes":…,"late":…,"stream":…,"offset":…}` */
export interface PongStats {
  echoMs: number;
  chunks: number;
  bytes: number;
  late: number;
  /** The WebM stream the server reads, and how many of its bytes arrived. */
  stream: string | null;
  offset: number;
}

/** Parse a pong text frame; null for anything malformed. Pure — unit tested. */
export function parsePong(msg: string): PongStats | null {
  if (!msg.startsWith('pong:')) return null;
  try {
    const o = JSON.parse(msg.slice(5)) as Record<string, unknown>;
    if (typeof o.echo !== 'number') return null;
    return {
      echoMs: o.echo,
      chunks: typeof o.chunks === 'number' ? o.chunks : 0,
      bytes: typeof o.bytes === 'number' ? o.bytes : 0,
      late: typeof o.late === 'number' ? o.late : 0,
      stream: typeof o.stream === 'string' ? o.stream : null,
      offset: typeof o.offset === 'number' ? o.offset : 0,
    };
  } catch {
    return null;
  }
}

function startPings(): void {
  stopPings();
  pingInterval = setInterval(() => {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(`ping:${performance.now()}`);
    }
  }, PING_INTERVAL_MS);
}

function stopPings(): void {
  if (pingInterval) {
    clearInterval(pingInterval);
    pingInterval = null;
  }
  rttMs = null;
}

/**
 * Read-only upload counters for the singleton stream socket (#275).
 *
 * A standalone export (NOT part of useStreamSocket()) so telemetry consumers
 * can poll it without calling the composable — calling useStreamSocket()
 * replaces the singleton's event callbacks, which would break the component
 * that owns the connection.
 */
export function getStreamSocketStats(): StreamSocketStats {
  return {
    connected: socket !== null && socket.readyState === WebSocket.OPEN,
    bufferedAmount: socket?.bufferedAmount ?? 0,
    bytesQueued,
    rttMs,
    serverChunks: serverStats.chunks,
    serverBytes: serverStats.bytes,
    serverLate: serverStats.late,
  };
}

/**
 * The show of the live broadcast in this tab: live, or reconnecting after a
 * drop. Null for a test broadcast or when nothing is live. A standalone export
 * like getStreamSocketStats, so a route guard can read it without taking over
 * the socket's callbacks.
 */
export function liveStreamShowId(): number | null {
  if (currentTest || currentShowId == null) return null;
  const live = state.value === 'live' || (state.value === 'connecting' && autoReconnecting);
  return live ? currentShowId : null;
}

/**
 * The show this tab streams: live, reconnecting, or lost and not stopped yet.
 * Null after stopStream() or cleanup(), and for a test broadcast.
 */
export function streamShowId(): number | null {
  return currentTest ? null : currentShowId;
}

/** Where a WebM stream's header ends: the offset of its first Cluster, or -1. */
export function webmHeaderLength(bytes: Uint8Array): number {
  for (let i = 0; i + 3 < bytes.length; i++) {
    if (
      bytes[i] === 0x1f &&
      bytes[i + 1] === 0x43 &&
      bytes[i + 2] === 0xb6 &&
      bytes[i + 3] === 0x75
    ) {
      return i;
    }
  }
  return -1;
}

/** Follow the recorder's WebM stream: a new stream, and its header once complete. */
function trackWebm(data: ArrayBuffer, newStream: boolean): void {
  if (newStream) {
    webmStreamId = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
    webmHeader = null;
    webmHead = new Uint8Array(0);
    pending = [];
    webmOffset = 0;
  }
  if (!webmStreamId) return;
  pending.push({ offset: webmOffset, data });
  webmOffset += data.byteLength;
  if (pending.length > MAX_PENDING_CHUNKS) {
    pending.shift();
    audioDropped.value = true;
  }
  if (webmHeader || webmHead.length > WEBM_HEADER_SCAN_LIMIT) return;
  const head = new Uint8Array(webmHead.length + data.byteLength);
  head.set(webmHead);
  head.set(new Uint8Array(data), webmHead.length);
  const end = webmHeaderLength(head);
  if (end >= 0) {
    webmHeader = head.slice(0, end);
    webmHead = new Uint8Array(0);
  } else {
    webmHead = head;
  }
}

function forgetWebm(): void {
  webmStreamId = null;
  webmHeader = null;
  webmHead = new Uint8Array(0);
  pending = [];
  webmOffset = 0;
  audioDropped.value = false;
}

/** The server has the stream up to `pong.offset`: let go of those chunks. */
function confirmWebm(pong: PongStats): void {
  if (pong.stream !== webmStreamId) return;
  while (pending.length && pending[0].offset + pending[0].data.byteLength <= pong.offset) {
    pending.shift();
  }
}

/**
 * On a new connection, go on with the recorder's stream: the header (or the
 * stream's start), then from where the server may have lost it, every chunk
 * it has not confirmed. The server skips the bytes it already has.
 */
function resumeWebm(ws: WebSocket, header: Uint8Array | null, headerOf: string | null): number {
  if (!webmStreamId) return 0;
  let bytes = 0;
  if (header && headerOf === webmStreamId) {
    ws.send(header);
    bytes += header.byteLength;
  } else {
    ws.send(`stream:${webmStreamId}`);
  }
  ws.send(`at:${pending[0]?.offset ?? webmOffset}`);
  for (const chunk of pending) {
    ws.send(chunk.data);
    bytes += chunk.data.byteLength;
  }
  return bytes;
}

/** Longest wait between reconnect attempts: 1, 2, 4, then every 5 seconds. */
const MAX_RECONNECT_DELAY_MS = 5000;

export function useStreamSocket(options: UseStreamSocketOptions = {}) {
  // No limit: a live show reconnects until End Episode (stopStream).
  const { maxReconnectAttempts = Infinity, onConnected, onDisconnected, onError, onLive } = options;

  // Update callbacks so the currently-mounted component receives events,
  // unless the live session owns them (ownStreamCallbacks).
  if (!callbacksOwned || liveStreamShowId() === null) {
    currentCallbacks = { onConnected, onDisconnected, onError, onLive };
  }

  function connect(
    force = false,
    showId?: number,
    test?: boolean,
    record?: boolean
  ): Promise<void> {
    return new Promise((resolve, reject) => {
      if (socket && socket.readyState === WebSocket.OPEN) {
        resolve();
        return;
      }

      // Remember the show so reconnects re-send it; backend keys recording on it.
      if (showId != null) {
        currentShowId = showId;
      }
      // Remember test mode for reconnects (auto-reconnect passes no flags).
      if (test != null) {
        currentTest = test;
      }
      if (record != null) {
        currentRecord = record;
      }

      error.value = null;
      state.value = 'connecting';

      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const params = new URLSearchParams();
      if (force) params.set('force', 'true');
      if (currentShowId != null) params.set('show_id', String(currentShowId));
      if (currentTest) params.set('test', 'true');
      if (!currentRecord) params.set('record', 'false');
      // Continue the recorder's stream: the header goes first, then its audio.
      const resumeHeader = webmHeader;
      const resumeId = resumeHeader ? webmStreamId : null;
      if (resumeId) params.set('resume', resumeId);
      const qs = params.toString();
      const wsUrl = `${protocol}//${window.location.host}/ws/stream${qs ? `?${qs}` : ''}`;

      // A socket that was stopped or replaced (a soundcheck that just ended)
      // still fires events; they must not touch the current connection.
      const ws = new WebSocket(wsUrl);
      socket = ws;
      ws.binaryType = 'arraybuffer';
      bytesQueued = 0;
      rttMs = null;
      serverStats = { chunks: 0, bytes: 0, late: 0 };

      ws.onopen = () => {
        if (ws !== socket) return;
        console.log('[StreamSocket] Connected');
        bytesQueued += resumeWebm(ws, resumeHeader, resumeId);
        state.value = 'connected';
        startPings();
        currentCallbacks.onConnected?.();
        resolve();
      };

      ws.onmessage = (event) => {
        if (ws !== socket) return;
        const msg = event.data;
        if (msg === 'connected') {
          state.value = 'live';
          // Live again (the producer is up, not just the socket): the next drop
          // gets the full backoff budget.
          reconnectAttempts.value = 0;
          autoReconnecting = false;
          currentCallbacks.onLive?.();
        } else if (typeof msg === 'string' && msg.startsWith('pong:')) {
          const pong = parsePong(msg);
          if (pong) {
            rttMs = Math.max(0, Math.round(performance.now() - pong.echoMs));
            serverStats = { chunks: pong.chunks, bytes: pong.bytes, late: pong.late };
            confirmWebm(pong);
          }
        } else if (typeof msg === 'string' && msg.startsWith('error:')) {
          const errMsg = msg.substring(7);
          // The server ended this connection on purpose (e.g. "Another
          // connection took over the stream") — the close that follows must
          // not auto-reconnect, even mid-backoff.
          autoReconnecting = false;
          error.value = errMsg;
          state.value = 'error';
          currentCallbacks.onError?.(errMsg);
        }
      };

      ws.onerror = () => {
        console.error('[StreamSocket] Connection error');
        if (ws === socket) {
          error.value = 'Connection error';
          state.value = 'error';
        }
        reject(new Error('Connection error'));
      };

      ws.onclose = (event) => {
        console.log('[StreamSocket] Closed:', event.code, event.reason);
        if (ws !== socket) return;
        stopPings();
        const wasLive = state.value === 'live';
        // Consumed here; set again below only if another attempt is scheduled.
        const wasReconnecting = autoReconnecting;
        autoReconnecting = false;

        if (
          event.code !== 1000 &&
          reconnectAttempts.value < maxReconnectAttempts &&
          (wasLive || wasReconnecting)
        ) {
          // Auto-reconnect on unexpected disconnect, or after a failed attempt
          autoReconnecting = true;
          reconnectAttempts.value++;
          const delay = Math.min(
            Math.pow(2, reconnectAttempts.value - 1) * 1000,
            MAX_RECONNECT_DELAY_MS
          );
          state.value = 'connecting';
          console.log(
            `[StreamSocket] Reconnecting in ${delay}ms (${reconnectAttempts.value}/${maxReconnectAttempts})`
          );

          reconnectTimeout = setTimeout(() => {
            connect(force).catch(() => {});
          }, delay);
        } else if (reconnectAttempts.value >= maxReconnectAttempts) {
          state.value = 'error';
          error.value = 'Connection lost after multiple attempts';
        } else {
          state.value = 'disconnected';
        }

        currentCallbacks.onDisconnected?.();
      };
    });
  }

  /**
   * Send a recorder chunk. `newStream` marks the first chunk of a new
   * MediaRecorder, so the server reads it as a new WebM stream. While the
   * socket is down, chunks wait for the reconnect (false).
   */
  function send(data: ArrayBuffer | string, newStream = false): boolean {
    if (data instanceof ArrayBuffer) trackWebm(data, newStream);
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return false;
    }
    if (newStream && webmStreamId) socket.send(`stream:${webmStreamId}`);
    socket.send(data);
    if (data instanceof ArrayBuffer) {
      bytesQueued += data.byteLength;
    }
    return true;
  }

  /**
   * Explicitly stop the stream: sends 'stop' command to backend, then closes.
   * Use this ONLY for the explicit "Stop Streaming" user action.
   */
  function stopStream() {
    const hadSocket = socket !== null;
    stopPings();
    if (reconnectTimeout) {
      clearTimeout(reconnectTimeout);
      reconnectTimeout = null;
    }

    if (socket) {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send('stop');
      }
      socket.close(1000, 'User stopped');
      socket = null;
    }

    currentShowId = null;
    currentTest = false;
    currentRecord = true;
    forgetWebm();
    autoReconnecting = false;
    reconnectAttempts.value = 0;
    state.value = 'disconnected';
    error.value = null;
    // The socket's own close event is ignored from here on (see connect).
    if (hadSocket) currentCallbacks.onDisconnected?.();
  }

  /**
   * Close the socket without sending 'stop' — safe for component cleanup / unmount.
   * The stream continues on the backend until explicitly stopped.
   */
  function cleanup() {
    const hadSocket = socket !== null;
    stopPings();
    if (reconnectTimeout) {
      clearTimeout(reconnectTimeout);
      reconnectTimeout = null;
    }

    if (socket) {
      // Close cleanly without telling the backend to stop
      socket.close(1000, 'Component cleanup');
      socket = null;
    }

    currentShowId = null;
    currentTest = false;
    currentRecord = true;
    forgetWebm();
    autoReconnecting = false;
    reconnectAttempts.value = 0;
    state.value = 'disconnected';
    error.value = null;
    // The socket's own close event is ignored from here on (see connect).
    if (hadSocket) currentCallbacks.onDisconnected?.();
  }

  /**
   * @deprecated Use stopStream() for explicit stops or cleanup() for unmount.
   */
  function disconnect() {
    stopStream();
  }

  function resetReconnect() {
    reconnectAttempts.value = 0;
  }

  // NOTE: No onUnmounted hook — callers manage their own lifecycle.
  // This allows the socket to survive route navigations (e.g. FlowWaiting → FlowStreaming).

  return {
    state,
    error,
    audioDropped,
    reconnectAttempts,
    maxReconnectAttempts,
    connect,
    send,
    stopStream,
    cleanup,
    disconnect,
    resetReconnect,
  };
}
