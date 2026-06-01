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
// Remembered across reconnects so the backend keeps auto-recording the same show.
let currentShowId: number | null = null;
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

export function useStreamSocket(options: UseStreamSocketOptions = {}) {
  const { maxReconnectAttempts = 3, onConnected, onDisconnected, onError, onLive } = options;

  // Update callbacks so the currently-mounted component receives events
  currentCallbacks = { onConnected, onDisconnected, onError, onLive };

  function connect(force = false, showId?: number): Promise<void> {
    return new Promise((resolve, reject) => {
      if (socket && socket.readyState === WebSocket.OPEN) {
        resolve();
        return;
      }

      // Remember the show so reconnects re-send it; backend keys recording on it.
      if (showId != null) {
        currentShowId = showId;
      }

      error.value = null;
      state.value = 'connecting';

      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const params = new URLSearchParams();
      if (force) params.set('force', 'true');
      if (currentShowId != null) params.set('show_id', String(currentShowId));
      const qs = params.toString();
      const wsUrl = `${protocol}//${window.location.host}/ws/stream${qs ? `?${qs}` : ''}`;

      socket = new WebSocket(wsUrl);
      socket.binaryType = 'arraybuffer';

      socket.onopen = () => {
        console.log('[StreamSocket] Connected');
        state.value = 'connected';
        currentCallbacks.onConnected?.();
        resolve();
      };

      socket.onmessage = (event) => {
        const msg = event.data;
        if (msg === 'connected') {
          state.value = 'live';
          currentCallbacks.onLive?.();
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
    return true;
  }

  /**
   * Explicitly stop the stream: sends 'stop' command to backend, then closes.
   * Use this ONLY for the explicit "Stop Streaming" user action.
   */
  function stopStream() {
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
    reconnectAttempts.value = 0;
    state.value = 'disconnected';
    error.value = null;
  }

  /**
   * Close the socket without sending 'stop' — safe for component cleanup / unmount.
   * The stream continues on the backend until explicitly stopped.
   */
  function cleanup() {
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
