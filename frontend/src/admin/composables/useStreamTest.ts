import { ref, onUnmounted } from 'vue';

export type StreamTestState = 'idle' | 'recording' | 'waiting' | 'playing' | 'done' | 'error';

export interface UseStreamTestOptions {
  /** Duration to record audio in milliseconds (default: 10000) */
  recordDuration?: number;
  /** Called when playback audio data is received (binary WebM/Opus chunks) */
  onPlaybackData?: (data: ArrayBuffer) => void;
  /** Called when an error occurs */
  onError?: (error: string) => void;
}

/**
 * Composable for the stream test loopback.
 *
 * Connects to `/ws/stream-test`, sends audio chunks from `useAudioCapture`
 * for `recordDuration` ms, then triggers server-side playback and receives
 * the buffered audio back at real-time pace.
 *
 * Usage:
 * ```ts
 * const capture = useAudioCapture({ onData: (buf) => streamTest.sendChunk(buf) });
 * const streamTest = useStreamTest({
 *   onPlaybackData: (buf) => { /* decode and play * / },
 * });
 * ```
 */
export function useStreamTest(options: UseStreamTestOptions = {}) {
  const { recordDuration = 10_000, onPlaybackData, onError } = options;

  const state = ref<StreamTestState>('idle');
  const error = ref<string | null>(null);
  /** Number of chunks sent to the server during recording */
  const chunksSent = ref(0);
  /** Number of chunks received back during playback */
  const chunksReceived = ref(0);

  let socket: WebSocket | null = null;
  let recordTimer: ReturnType<typeof setTimeout> | null = null;

  /**
   * Connect to the stream test WebSocket.
   * Resolves when the server sends 'ready'.
   */
  function connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      if (socket && socket.readyState === WebSocket.OPEN) {
        resolve();
        return;
      }

      error.value = null;
      state.value = 'idle';
      chunksSent.value = 0;
      chunksReceived.value = 0;

      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const wsUrl = `${protocol}//${window.location.host}/ws/stream-test`;

      socket = new WebSocket(wsUrl);
      socket.binaryType = 'arraybuffer';

      socket.onopen = () => {
        console.log('[StreamTest] WebSocket connected');
      };

      socket.onmessage = (event) => {
        const data = event.data;

        if (typeof data === 'string') {
          handleTextMessage(data, resolve);
        } else if (data instanceof ArrayBuffer) {
          // Playback binary chunk from server
          chunksReceived.value++;
          onPlaybackData?.(data);
        }
      };

      socket.onerror = () => {
        const msg = 'Stream test connection failed';
        console.error('[StreamTest]', msg);
        setError(msg);
        reject(new Error(msg));
      };

      socket.onclose = (event) => {
        console.log('[StreamTest] WebSocket closed:', event.code, event.reason);
        // If we weren't already in 'done' or 'error' state, treat as unexpected
        if (state.value !== 'done' && state.value !== 'error' && state.value !== 'idle') {
          setError('Connection closed unexpectedly');
        }
        socket = null;
      };
    });
  }

  function handleTextMessage(msg: string, onReady?: (value: void) => void) {
    switch (msg) {
      case 'ready':
        console.log('[StreamTest] Server ready');
        onReady?.();
        break;

      case 'playback-start':
        console.log('[StreamTest] Playback starting');
        state.value = 'playing';
        break;

      case 'playback-done':
        console.log('[StreamTest] Playback complete');
        state.value = 'done';
        break;

      default:
        if (msg.startsWith('error:')) {
          setError(msg.substring(7).trim());
        }
        break;
    }
  }

  /**
   * Send a binary audio chunk to the server for buffering.
   * Call this from your `useAudioCapture`'s `onData` callback.
   */
  function sendChunk(data: ArrayBuffer): boolean {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return false;
    }
    if (state.value !== 'recording') {
      return false;
    }
    socket.send(data);
    chunksSent.value++;
    return true;
  }

  /**
   * Start the test: begin recording phase.
   *
   * The caller is responsible for starting `useAudioCapture.startRecording()`
   * and wiring `onData` → `sendChunk()` before calling this.
   *
   * After `recordDuration` ms, automatically sends 'play' command.
   * Returns a cleanup function to stop the recording timer.
   */
  function startRecording(): () => void {
    if (state.value !== 'idle') {
      console.warn('[StreamTest] Cannot start recording in state:', state.value);
      return () => {};
    }

    state.value = 'recording';
    chunksSent.value = 0;
    chunksReceived.value = 0;

    console.log(`[StreamTest] Recording for ${recordDuration}ms`);

    // After the recording duration, trigger playback
    recordTimer = setTimeout(() => {
      requestPlayback();
    }, recordDuration);

    return () => {
      if (recordTimer) {
        clearTimeout(recordTimer);
        recordTimer = null;
      }
    };
  }

  /**
   * Send the 'play' command to trigger server-side playback.
   * Called automatically after `recordDuration`, but can be called manually.
   */
  function requestPlayback() {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      setError('Not connected');
      return;
    }

    state.value = 'waiting';
    console.log(`[StreamTest] Requesting playback (${chunksSent.value} chunks sent)`);
    socket.send('play');
  }

  /**
   * Abort the test and close the WebSocket.
   */
  function stop() {
    if (recordTimer) {
      clearTimeout(recordTimer);
      recordTimer = null;
    }

    if (socket) {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send('stop');
        socket.close(1000, 'Test stopped');
      }
      socket = null;
    }

    state.value = 'idle';
    error.value = null;
    chunksSent.value = 0;
    chunksReceived.value = 0;
  }

  /**
   * Full cleanup — call on unmount or when done.
   */
  function cleanup() {
    stop();
  }

  function setError(msg: string) {
    error.value = msg;
    state.value = 'error';
    onError?.(msg);
  }

  onUnmounted(() => {
    cleanup();
  });

  return {
    /** Current test state */
    state,
    /** Error message, if any */
    error,
    /** Number of audio chunks sent to server */
    chunksSent,
    /** Number of audio chunks received back during playback */
    chunksReceived,
    /** Connect to the stream test WebSocket */
    connect,
    /** Send a binary audio chunk (wire to useAudioCapture.onData) */
    sendChunk,
    /** Start the recording phase */
    startRecording,
    /** Manually trigger playback (normally auto-triggered after recordDuration) */
    requestPlayback,
    /** Abort the test */
    stop,
    /** Full cleanup */
    cleanup,
  };
}
