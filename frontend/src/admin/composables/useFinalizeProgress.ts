import { ref, computed, onUnmounted } from 'vue';

/**
 * Finalize phases matching backend FinalizePhase enum
 */
export type FinalizePhase = 'downloading' | 'merging' | 'uploading' | 'complete' | 'error';

/**
 * Overall status of the finalize operation
 */
export type FinalizeStatus = 'idle' | 'running' | 'complete' | 'error';

/**
 * Progress message from the backend WebSocket
 */
export interface FinalizeProgressMessage {
  phase: FinalizePhase;
  percent: number;
  detail: string;
  resumed?: boolean;
}

/**
 * Options for the useFinalizeProgress composable
 */
export interface UseFinalizeProgressOptions {
  /** Called when finalize starts */
  onStarted?: () => void;
  /** Called when finalize completes successfully */
  onComplete?: (finalKey: string) => void;
  /** Called when finalize fails */
  onError?: (error: string) => void;
  /** Called on each progress update */
  onProgress?: (progress: FinalizeProgressMessage) => void;
  /** Called when connection is lost during finalize */
  onDisconnected?: () => void;
  /** Maximum reconnect attempts */
  maxReconnectAttempts?: number;
  /** Reconnect delay in ms (doubles each attempt) */
  reconnectDelayMs?: number;
}

/**
 * Composable for managing finalize WebSocket progress tracking.
 *
 * Connects to /ws/recording/finalize with show_id and version params,
 * tracks progress through downloading → merging → uploading → complete phases,
 * and handles auto-reconnect on disconnect.
 */
export function useFinalizeProgress(options: UseFinalizeProgressOptions = {}) {
  const {
    onStarted,
    onComplete,
    onError,
    onProgress,
    onDisconnected,
    maxReconnectAttempts = 3,
    reconnectDelayMs = 1000,
  } = options;

  // State
  const status = ref<FinalizeStatus>('idle');
  const phase = ref<FinalizePhase | null>(null);
  const percent = ref(0);
  const detail = ref('');
  const error = ref<string | null>(null);
  const isResumed = ref(false);
  const reconnectAttempts = ref(0);

  // Active finalize params (for reconnect)
  let activeShowId: number | null = null;
  let activeVersion: string | null = null;

  // WebSocket and reconnect timer
  let socket: WebSocket | null = null;
  let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;

  // Computed
  const isRunning = computed(() => status.value === 'running');
  const isComplete = computed(() => status.value === 'complete');
  const isError = computed(() => status.value === 'error');
  const isIdle = computed(() => status.value === 'idle');

  const progressPercent = computed(() => {
    // Map phase + percent to overall progress (0-100)
    if (!phase.value) return 0;

    switch (phase.value) {
      case 'downloading':
        // Downloading is 0-50%
        return Math.round(percent.value * 0.5);
      case 'merging':
        // Merging is 50-90%
        return 50 + Math.round(percent.value * 0.4);
      case 'uploading':
        // Uploading is 90-100%
        return 90 + Math.round(percent.value * 0.1);
      case 'complete':
        return 100;
      case 'error':
        return 0;
      default:
        return 0;
    }
  });

  const phaseLabel = computed(() => {
    switch (phase.value) {
      case 'downloading':
        return 'Downloading files...';
      case 'merging':
        return 'Merging audio...';
      case 'uploading':
        return 'Uploading result...';
      case 'complete':
        return 'Complete!';
      case 'error':
        return 'Error';
      default:
        return '';
    }
  });

  /**
   * Start the finalize process for a show version.
   */
  function startFinalize(showId: number, version: string): void {
    if (status.value === 'running') {
      console.warn('[FinalizeProgress] Already running, ignoring start request');
      return;
    }

    // Reset state
    status.value = 'running';
    phase.value = null;
    percent.value = 0;
    detail.value = 'Connecting...';
    error.value = null;
    isResumed.value = false;
    reconnectAttempts.value = 0;

    // Store for reconnect
    activeShowId = showId;
    activeVersion = version;

    onStarted?.();
    connect(showId, version);
  }

  /**
   * Connect to the finalize WebSocket.
   */
  function connect(showId: number, version: string): void {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/recording/finalize?show_id=${showId}&version=${encodeURIComponent(version)}`;

    console.log('[FinalizeProgress] Connecting to:', wsUrl);
    socket = new WebSocket(wsUrl);

    socket.onopen = () => {
      console.log('[FinalizeProgress] Connected');
      reconnectAttempts.value = 0;
      detail.value = 'Connected, starting finalize...';
    };

    socket.onmessage = (event) => {
      try {
        const msg: FinalizeProgressMessage = JSON.parse(event.data);
        handleProgress(msg);
      } catch (e) {
        console.error('[FinalizeProgress] Failed to parse message:', event.data);
      }
    };

    socket.onerror = (event) => {
      console.error('[FinalizeProgress] WebSocket error:', event);
    };

    socket.onclose = (event) => {
      console.log('[FinalizeProgress] Closed:', event.code, event.reason);

      // If we're still running and connection closed unexpectedly, try to reconnect
      if (status.value === 'running' && event.code !== 1000) {
        attemptReconnect();
      } else if (status.value === 'running') {
        // Normal close but still running = unexpected
        status.value = 'error';
        error.value = 'Connection closed unexpectedly';
        onDisconnected?.();
      }
    };
  }

  /**
   * Handle a progress message from the backend.
   */
  function handleProgress(msg: FinalizeProgressMessage): void {
    phase.value = msg.phase;
    percent.value = msg.percent;
    detail.value = msg.detail;

    if (msg.resumed) {
      isResumed.value = true;
    }

    onProgress?.(msg);

    if (msg.phase === 'complete') {
      status.value = 'complete';
      // Extract final key from detail message
      const match = msg.detail.match(/Recording finalized: (.+)/);
      const finalKey = match ? match[1] : msg.detail;
      onComplete?.(finalKey);
      cleanup();
    } else if (msg.phase === 'error') {
      status.value = 'error';
      error.value = msg.detail;
      onError?.(msg.detail);
      cleanup();
    }
  }

  /**
   * Attempt to reconnect after a disconnect.
   */
  function attemptReconnect(): void {
    if (reconnectAttempts.value >= maxReconnectAttempts) {
      console.error('[FinalizeProgress] Max reconnect attempts reached');
      status.value = 'error';
      error.value = 'Connection lost after multiple attempts';
      onError?.('Connection lost after multiple attempts');
      onDisconnected?.();
      cleanup();
      return;
    }

    if (activeShowId === null || activeVersion === null) {
      console.error('[FinalizeProgress] No active finalize params for reconnect');
      return;
    }

    reconnectAttempts.value++;
    const delay = reconnectDelayMs * Math.pow(2, reconnectAttempts.value - 1);
    console.log(
      `[FinalizeProgress] Reconnecting in ${delay}ms (attempt ${reconnectAttempts.value}/${maxReconnectAttempts})`
    );
    detail.value = `Reconnecting (attempt ${reconnectAttempts.value}/${maxReconnectAttempts})...`;

    reconnectTimeout = setTimeout(() => {
      if (activeShowId !== null && activeVersion !== null) {
        connect(activeShowId, activeVersion);
      }
    }, delay);
  }

  /**
   * Cancel the finalize operation and disconnect.
   */
  function cancel(): void {
    console.log('[FinalizeProgress] Cancelling');
    cleanup();
    status.value = 'idle';
    phase.value = null;
    percent.value = 0;
    detail.value = '';
    error.value = null;
  }

  /**
   * Reset state after completion or error.
   */
  function reset(): void {
    cancel();
  }

  /**
   * Clean up WebSocket and timers.
   */
  function cleanup(): void {
    if (reconnectTimeout) {
      clearTimeout(reconnectTimeout);
      reconnectTimeout = null;
    }

    if (socket) {
      socket.onclose = null; // Prevent triggering reconnect
      socket.close();
      socket = null;
    }

    activeShowId = null;
    activeVersion = null;
  }

  // Cleanup on unmount
  onUnmounted(() => {
    cleanup();
  });

  return {
    // State
    status,
    phase,
    percent,
    detail,
    error,
    isResumed,
    reconnectAttempts,

    // Computed
    isRunning,
    isComplete,
    isError,
    isIdle,
    progressPercent,
    phaseLabel,

    // Actions
    startFinalize,
    cancel,
    reset,
  };
}
