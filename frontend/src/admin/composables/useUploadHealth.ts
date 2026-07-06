import { ref, computed, onUnmounted, watch, type Ref } from 'vue';
import { getStreamSocketStats, type StreamSocketStats } from './useStreamSocket';

/** Seconds of throughput history kept for the sparkline. */
export const HISTORY_SECONDS = 60;
/** Container/framing overhead on top of the raw encoder bitrate. */
export const CONTAINER_OVERHEAD = 1.15;
/** Send-buffer seconds above which a tick counts as "late". */
const LATE_BUFFER_S = 1.5;
/** Buffer thresholds for the verdict pill. */
const BUFFER_GOOD_S = 0.5;

export type UploadVerdict = 'good' | 'tight' | 'slow';

export interface UploadTick {
  /** Payload kbit/s handed to the socket this tick (encoder output rate). */
  encodedKbps: number;
  /** Payload kbit/s actually drained to the network this tick. */
  egressKbps: number;
  /** Send-buffer depth in seconds at the target byte rate. */
  bufferSeconds: number;
}

/**
 * Compute one telemetry tick from two socket-stat snapshots. Pure — unit
 * tested. `elapsedMs` guards against timer jitter; a `bytesQueued` that went
 * backwards means the socket reconnected, which yields a zero tick.
 */
export function computeTick(
  prev: Pick<StreamSocketStats, 'bufferedAmount' | 'bytesQueued'>,
  cur: Pick<StreamSocketStats, 'bufferedAmount' | 'bytesQueued'>,
  targetBitsPerSecond: number,
  elapsedMs: number
): UploadTick {
  if (elapsedMs <= 0 || cur.bytesQueued < prev.bytesQueued) {
    return { encodedKbps: 0, egressKbps: 0, bufferSeconds: 0 };
  }

  const queuedBytes = cur.bytesQueued - prev.bytesQueued;
  const drainedBytes = Math.max(0, queuedBytes - (cur.bufferedAmount - prev.bufferedAmount));
  const secs = elapsedMs / 1000;

  const targetBytesPerSecond = (targetBitsPerSecond * CONTAINER_OVERHEAD) / 8;

  return {
    encodedKbps: (queuedBytes * 8) / secs / 1000,
    egressKbps: (drainedBytes * 8) / secs / 1000,
    bufferSeconds: targetBytesPerSecond > 0 ? cur.bufferedAmount / targetBytesPerSecond : 0,
  };
}

/**
 * Verdict from the send-buffer depth. The browser can't measure headroom
 * beyond the produced bitrate (egress is capped at what the encoder emits),
 * so buffer drain IS the congestion signal: an empty buffer means the network
 * keeps up with the target; a growing one means it doesn't.
 */
export function verdictFor(bufferSeconds: number): UploadVerdict {
  if (bufferSeconds < BUFFER_GOOD_S) return 'good';
  if (bufferSeconds < LATE_BUFFER_S) return 'tight';
  return 'slow';
}

/**
 * Browser-side upload health for the stream WebSocket (#275).
 *
 * Polls the singleton socket's counters once a second while `active` is true:
 * encoded rate (bytes handed to send()), real egress (bufferedAmount drain),
 * send-buffer seconds, late-tick counter, sparkline history, and a
 * good/tight/slow verdict. Zero backend involvement.
 */
export function useUploadHealth(active: Ref<boolean>, targetBitsPerSecond: Ref<number>) {
  const uploadKbps = ref(0);
  const encodedKbps = ref(0);
  const bufferSeconds = ref(0);
  const lateCount = ref(0);
  /** Last HISTORY_SECONDS egress samples (kbps), oldest first. */
  const history = ref<number[]>([]);

  const neededKbps = computed(() => (targetBitsPerSecond.value * CONTAINER_OVERHEAD) / 1000);
  const verdict = computed<UploadVerdict>(() => verdictFor(bufferSeconds.value));
  const verdictText = computed(() => {
    switch (verdict.value) {
      case 'good':
        return 'Good — keeping up with the target';
      case 'tight':
        return `Tight — ${bufferSeconds.value.toFixed(1)} s buffered`;
      case 'slow':
        return 'Too slow — lower the quality';
    }
  });

  let interval: ReturnType<typeof setInterval> | null = null;
  let prev: StreamSocketStats | null = null;
  let prevTs = 0;

  function tick() {
    const cur = getStreamSocketStats();
    const now = performance.now();

    if (prev && prev.connected && cur.connected) {
      const t = computeTick(prev, cur, targetBitsPerSecond.value, now - prevTs);
      encodedKbps.value = Math.round(t.encodedKbps);
      uploadKbps.value = Math.round(t.egressKbps);
      bufferSeconds.value = t.bufferSeconds;
      if (t.bufferSeconds > LATE_BUFFER_S) lateCount.value++;
      history.value = [...history.value.slice(-(HISTORY_SECONDS - 1)), t.egressKbps];
    }

    prev = cur;
    prevTs = now;
  }

  function reset() {
    uploadKbps.value = 0;
    encodedKbps.value = 0;
    bufferSeconds.value = 0;
    lateCount.value = 0;
    history.value = [];
    prev = null;
    prevTs = 0;
  }

  function stop() {
    if (interval) {
      clearInterval(interval);
      interval = null;
    }
  }

  watch(
    active,
    (on) => {
      stop();
      if (on) {
        reset();
        interval = setInterval(tick, 1000);
      }
    },
    { immediate: true }
  );

  onUnmounted(stop);

  return {
    uploadKbps,
    encodedKbps,
    bufferSeconds,
    lateCount,
    history,
    neededKbps,
    verdict,
    verdictText,
  };
}
