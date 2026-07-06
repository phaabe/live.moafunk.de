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
let currentCallbacks: {
  onConnected?: () => void;
  onDisconnected?: () => void;
  onError?: (error: string) => void;
  onLive?: () => void;
} = {};

// ─── Browser close safety net: send 'stop' if page is unloaded ─────────────
if (typeof window !== 'undefined') {
  window.addEventListener('beforeunload', () => {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send('stop');
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

/** Server pong frame: `pong:{"echo":…,"chunks":…,"bytes":…,"late":…}` */
export interface PongStats {
  echoMs: number;
  chunks: number;
  bytes: number;
  late: number;
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

export function useStreamSocket(options: UseStreamSocketOptions = {}) {
  const { maxReconnectAttempts = 3, onConnected, onDisconnected, onError, onLive } = options;

  // Update callbacks so the currently-mounted component receives events
  currentCallbacks = { onConnected, onDisconnected, onError, onLive };

  function connect(force = false, showId?: number, test?: boolean): Promise<void> {
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

      error.value = null;
      state.value = 'connecting';

      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const params = new URLSearchParams();
      if (force) params.set('force', 'true');
      if (currentShowId != null) params.set('show_id', String(currentShowId));
      if (currentTest) params.set('test', 'true');
      const qs = params.toString();
      const wsUrl = `${protocol}//${window.location.host}/ws/stream${qs ? `?${qs}` : ''}`;

      socket = new WebSocket(wsUrl);
      socket.binaryType = 'arraybuffer';
      bytesQueued = 0;
      rttMs = null;
      serverStats = { chunks: 0, bytes: 0, late: 0 };

      socket.onopen = () => {
        console.log('[StreamSocket] Connected');
        state.value = 'connected';
        startPings();
        currentCallbacks.onConnected?.();
        resolve();
      };

      socket.onmessage = (event) => {
        const msg = event.data;
        if (msg === 'connected') {
          state.value = 'live';
          currentCallbacks.onLive?.();
        } else if (typeof msg === 'string' && msg.startsWith('pong:')) {
          const pong = parsePong(msg);
          if (pong) {
            rttMs = Math.max(0, Math.round(performance.now() - pong.echoMs));
            serverStats = { chunks: pong.chunks, bytes: pong.bytes, late: pong.late };
          }
        } else if (typeof msg === 'string' && msg.startsWith('error:')) {
          const errMsg = msg.substring(7);
          error.value = errMsg;
          state.value = 'error';
          currentCallbacks.onError?.(errMsg);
        }
      };

      socket.onerror = () => {
        console.error('[StreamSocket] Connection error');
        error.value = 'Connection error';
        state.value = 'error';
        reject(new Error('Connection error'));
      };

      socket.onclose = (event) => {
        console.log('[StreamSocket] Closed:', event.code, event.reason);
        stopPings();
        const wasLive = state.value === 'live';

        if (event.code !== 1000 && reconnectAttempts.value < maxReconnectAttempts && wasLive) {
          // Auto-reconnect on unexpected disconnect
          reconnectAttempts.value++;
          const delay = Math.pow(2, reconnectAttempts.value - 1) * 1000;
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

  function send(data: ArrayBuffer | string): boolean {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return false;
    }
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
    reconnectAttempts.value = 0;
    state.value = 'disconnected';
    error.value = null;
  }

  /**
   * Close the socket without sending 'stop' — safe for component cleanup / unmount.
   * The stream continues on the backend until explicitly stopped.
   */
  function cleanup() {
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
    reconnectAttempts.value = 0;
    state.value = 'disconnected';
    error.value = null;
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
