import { ref, onUnmounted } from 'vue';

export type StreamConnectionState = 'disconnected' | 'connecting' | 'connected' | 'live' | 'error';

export interface UseStreamSocketOptions {
  maxReconnectAttempts?: number;
  onConnected?: () => void;
  onDisconnected?: () => void;
  onError?: (error: string) => void;
  onLive?: () => void;
}

export function useStreamSocket(options: UseStreamSocketOptions = {}) {
  const { maxReconnectAttempts = 3, onConnected, onDisconnected, onError, onLive } = options;

  const state = ref<StreamConnectionState>('disconnected');
  const error = ref<string | null>(null);
  const reconnectAttempts = ref(0);

  let socket: WebSocket | null = null;
  let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;

  function connect(force = false): Promise<void> {
    return new Promise((resolve, reject) => {
      if (socket && socket.readyState === WebSocket.OPEN) {
        resolve();
        return;
      }

      error.value = null;
      state.value = 'connecting';

      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const wsUrl = `${protocol}//${window.location.host}/ws/stream${force ? '?force=true' : ''}`;

      socket = new WebSocket(wsUrl);
      socket.binaryType = 'arraybuffer';

      socket.onopen = () => {
        console.log('[StreamSocket] Connected');
        state.value = 'connected';
        onConnected?.();
        resolve();
      };

      socket.onmessage = (event) => {
        const msg = event.data;
        if (msg === 'connected') {
          state.value = 'live';
          onLive?.();
        } else if (typeof msg === 'string' && msg.startsWith('error:')) {
          const errMsg = msg.substring(7);
          error.value = errMsg;
          state.value = 'error';
          onError?.(errMsg);
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
          console.log(`[StreamSocket] Reconnecting in ${delay}ms (${reconnectAttempts.value}/${maxReconnectAttempts})`);
          
          reconnectTimeout = setTimeout(() => {
            connect(force).catch(() => {});
          }, delay);
        } else if (reconnectAttempts.value >= maxReconnectAttempts) {
          state.value = 'error';
          error.value = 'Connection lost after multiple attempts';
        } else {
          state.value = 'disconnected';
        }

        onDisconnected?.();
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

  function disconnect() {
    if (reconnectTimeout) {
      clearTimeout(reconnectTimeout);
      reconnectTimeout = null;
    }

    if (socket) {
      socket.send('stop');
      socket.close(1000, 'User stopped');
      socket = null;
    }

    reconnectAttempts.value = 0;
    state.value = 'disconnected';
    error.value = null;
  }

  function resetReconnect() {
    reconnectAttempts.value = 0;
  }

  onUnmounted(() => {
    disconnect();
  });

  return {
    state,
    error,
    reconnectAttempts,
    maxReconnectAttempts,
    connect,
    send,
    disconnect,
    resetReconnect,
  };
}
