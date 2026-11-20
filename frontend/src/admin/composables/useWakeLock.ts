import { onScopeDispose, ref } from 'vue';

/** The part of the Screen Wake Lock API this uses (not in every TS DOM lib). */
interface WakeLockSentinelLike {
  readonly released: boolean;
  release(): Promise<void>;
  addEventListener(type: 'release', listener: () => void): void;
}
interface WakeLockLike {
  request(type: 'screen'): Promise<WakeLockSentinelLike>;
}

/**
 * Keep the screen on while On Air is open (#1053): a laptop that goes to
 * sleep cuts the stream. Uses the Screen Wake Lock API when the browser has
 * it. The browser lets go of the lock when the tab is hidden, so it is asked
 * for again when the tab is visible again. `active` is false when the API is
 * missing or the browser refuses: the page then shows nothing.
 * Must be called inside an active effect scope.
 */
export function useWakeLock() {
  const wakeLock =
    typeof navigator === 'undefined'
      ? undefined
      : (navigator as Navigator & { wakeLock?: WakeLockLike }).wakeLock;
  const supported = !!wakeLock;
  const active = ref(false);

  let sentinel: WakeLockSentinelLike | null = null;
  let requesting = false;
  let disposed = false;

  async function acquire(): Promise<void> {
    if (!wakeLock || disposed || requesting || (sentinel && !sentinel.released)) return;
    if (document.visibilityState !== 'visible') return;
    requesting = true;
    try {
      const lock = await wakeLock.request('screen');
      if (disposed) {
        void lock.release().catch(() => {});
        return;
      }
      sentinel = lock;
      active.value = true;
      lock.addEventListener('release', () => {
        if (sentinel !== lock) return;
        sentinel = null;
        active.value = false;
      });
    } catch {
      // Refused (battery saver, no permission): the screen may sleep.
      active.value = false;
    } finally {
      requesting = false;
    }
  }

  function onVisibilityChange() {
    if (document.visibilityState === 'visible') void acquire();
  }

  if (supported) {
    void acquire();
    document.addEventListener('visibilitychange', onVisibilityChange);
  }

  onScopeDispose(() => {
    disposed = true;
    document.removeEventListener('visibilitychange', onVisibilityChange);
    const lock = sentinel;
    sentinel = null;
    active.value = false;
    void lock?.release().catch(() => {});
  });

  return { supported, active };
}
