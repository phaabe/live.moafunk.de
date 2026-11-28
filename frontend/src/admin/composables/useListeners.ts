import { onScopeDispose, ref } from 'vue';
import { streamApi } from '../api';

/** How often On Air reads the listener count (as before hotfix #1028). */
export const LISTENER_POLL_MS = 10_000;
/** The listener line covers this much time. */
export const LISTENER_WINDOW_MS = 40 * 60_000;

export interface ListenerSample {
  /** Unix ms. */
  at: number;
  count: number;
}

/** Add a sample and drop the ones older than the window. Pure — unit tested. */
export function addListenerSample(
  samples: readonly ListenerSample[],
  sample: ListenerSample,
  windowMs = LISTENER_WINDOW_MS
): ListenerSample[] {
  return [...samples.filter((s) => s.at > sample.at - windowMs), sample];
}

// The history of this tab's live episode, kept across remounts of On Air
// (like its on-air clock). The server keeps no history: peak and line count
// only the minutes this page polled.
let kept: { showId: number; samples: ListenerSample[]; peak: ListenerSample | null } | null = null;

/** Forget the history (End Episode). */
export function forgetListeners(): void {
  kept = null;
}

/**
 * Listeners of the live episode (#1053), from the cached Icecast snapshot
 * (`GET /api/stream/metrics`, all mounts). `current` is null while the
 * snapshot is offline or cannot be read. Must be called inside an active
 * effect scope.
 */
export function useListeners(showId: number) {
  if (kept?.showId !== showId) kept = { showId, samples: [], peak: null };
  const store = kept;

  const current = ref<number | null>(null);
  const samples = ref<ListenerSample[]>(store.samples);
  const peak = ref<ListenerSample | null>(store.peak);

  async function poll() {
    try {
      const metrics = await streamApi.metrics();
      if (!metrics.online) {
        current.value = null;
        return;
      }
      const sample = { at: Date.now(), count: metrics.total_listeners };
      current.value = sample.count;
      store.samples = addListenerSample(store.samples, sample);
      if (!store.peak || sample.count > store.peak.count) store.peak = sample;
      samples.value = store.samples;
      peak.value = store.peak;
    } catch {
      current.value = null;
    }
  }
  void poll();
  const timer = setInterval(poll, LISTENER_POLL_MS);
  onScopeDispose(() => clearInterval(timer));

  return { current, samples, peak };
}
