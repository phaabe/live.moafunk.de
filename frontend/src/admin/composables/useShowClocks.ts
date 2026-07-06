import { computed, onScopeDispose, ref, type ComputedRef, type Ref } from 'vue';
import type { LatestRecording } from '../api';

/**
 * Live clock values for the show dashboard status strip: countdown to air,
 * elapsed since air, and the finished-show duration.
 *
 * Extracted from the former LiveCard so the redesigned dashboard can render
 * them in a metric card. Owns a 1 s ticker; must be called inside an active
 * effect scope.
 */
export interface UseShowClocks {
  /** "04d 12h 33m 38s" until air (days dropped once under a day), or '—'. */
  countdown: ComputedRef<string>;
  /** Same format, time since air, or '—'. */
  elapsed: ComputedRef<string>;
  /** "HH:MM:SS" duration of the finished show, or null if unknown. */
  duration: ComputedRef<string | null>;
}

/** "04d 12h 33m 38s" — days segment dropped once under a day. */
export function fmtDuration(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const d = Math.floor(total / 86400);
  const h = Math.floor((total % 86400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, '0');
  return d > 0 ? `${d}d ${pad(h)}h ${pad(m)}m ${pad(s)}s` : `${pad(h)}h ${pad(m)}m ${pad(s)}s`;
}

/** "HH:MM:SS" clock — used for the finished-show duration. */
export function fmtClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(h)}:${pad(m)}:${pad(s)}`;
}

export function useShowClocks(
  airTarget: Ref<Date | null>,
  latestRecording: Ref<LatestRecording | null>
): UseShowClocks {
  const now = ref(Date.now());
  const timer = setInterval(() => {
    now.value = Date.now();
  }, 1000);
  onScopeDispose(() => clearInterval(timer));

  const countdown = computed(() =>
    airTarget.value ? fmtDuration(airTarget.value.getTime() - now.value) : '—'
  );

  const elapsed = computed(() =>
    airTarget.value ? fmtDuration(now.value - airTarget.value.getTime()) : '—'
  );

  /**
   * Duration of the finished show. Prefers the recording's stored length; for
   * legacy rows that predate duration persistence, a failed short capture
   * still carries its length in the reason string ("… is only 3s …").
   * Returns null when unknown so the caller can fall back to the scheduled
   * duration instead of rendering a bare dash.
   */
  const duration = computed<string | null>(() => {
    const rec = latestRecording.value?.duration_ms;
    if (rec) return fmtClock(rec);
    const shortMatch = latestRecording.value?.error_message?.match(/only (\d+)s/);
    if (shortMatch) return fmtClock(Number(shortMatch[1]) * 1000);
    return null;
  });

  return { countdown, elapsed, duration };
}
