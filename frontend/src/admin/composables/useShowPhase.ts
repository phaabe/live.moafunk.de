import { computed, onScopeDispose, ref, type ComputedRef, type Ref } from 'vue';
import { isShowEnded, isShowRunning, type ShowSchedule } from '../showTime';

/**
 * Soft lifecycle phase of a show, per docs/stream-rework/show-cockpit-plan.md:
 * - `prep`      — before air time
 * - `broadcast` — between air time and scheduled end
 * - `wrapup`    — past scheduled end, or a live recording already exists
 *
 * Phases drive emphasis / default tab only — they never lock a panel.
 */
export type ShowPhase = 'prep' | 'broadcast' | 'wrapup';

/** Minimal show shape the phase derivation needs (ShowDetail satisfies this). */
export interface PhaseSource extends ShowSchedule {
  latest_recording?: unknown;
}

export interface UseShowPhase {
  phase: ComputedRef<ShowPhase>;
}

/**
 * Derive the current phase of a show, re-evaluated on a timer so the UI
 * transitions Prep → Broadcast → Wrap-up without a reload.
 *
 * Must be called inside an active effect scope (component `setup` or
 * `effectScope()`); the internal interval is cleaned up on scope dispose.
 */
export function useShowPhase(
  show: Ref<PhaseSource | null | undefined>,
  opts: { tickMs?: number } = {}
): UseShowPhase {
  // Bumped on an interval purely to invalidate the computed — wall-clock time
  // is an implicit input that Vue can't track.
  const tick = ref(0);
  const timer = setInterval(() => {
    tick.value++;
  }, opts.tickMs ?? 30_000);
  onScopeDispose(() => clearInterval(timer));

  const phase = computed<ShowPhase>(() => {
    void tick.value;
    const s = show.value;
    if (!s) return 'prep';
    if (s.latest_recording || isShowEnded(s)) return 'wrapup';
    if (isShowRunning(s)) return 'broadcast';
    return 'prep';
  });

  return { phase };
}
