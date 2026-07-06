import { watch, type Ref } from 'vue';

/**
 * Upload quality ladder + auto stepping (#276).
 *
 * The upload bitrate only shapes the *source* feed — as long as it stays
 * above the Icecast mount's output encoding, listeners hear no difference,
 * so stepping down is aggressive and stepping up is patient.
 */

/** Selectable encoder targets, highest first (kbps). */
export const QUALITY_STEPS_KBPS = [320, 192, 128, 96] as const;
/** Auto never climbs above this — 320 is manual-only headroom. */
export const AUTO_CEILING_KBPS = 192;
/** Buffer depth that counts as congestion (matches the "tight" verdict). */
export const STEP_DOWN_BUFFER_S = 0.5;
/** Consecutive congested ticks (≈ seconds) before stepping down a notch. */
export const STEP_DOWN_TICKS = 3;
/** Consecutive clean ticks (≈ seconds) before stepping back up a notch. */
export const STEP_UP_TICKS = 30;

/** 'auto' or a fixed kbps value from QUALITY_STEPS_KBPS. */
export type UploadQualityMode = 'auto' | number;

export interface AutoBitrateState {
  congestedTicks: number;
  stableTicks: number;
}

export function initialAutoState(): AutoBitrateState {
  return { congestedTicks: 0, stableTicks: 0 };
}

/**
 * One auto-mode decision per telemetry tick. Pure — unit tested.
 * Returns the kbps to switch to (or null to stay) plus the carried state;
 * any step decision resets both counters so switches can't cascade.
 */
export function decideBitrate(
  currentKbps: number,
  bufferSeconds: number,
  state: AutoBitrateState
): { nextKbps: number | null; state: AutoBitrateState } {
  if (bufferSeconds > STEP_DOWN_BUFFER_S) {
    const congestedTicks = state.congestedTicks + 1;
    if (congestedTicks >= STEP_DOWN_TICKS) {
      // Ladder is sorted high→low, so the first smaller entry is one notch down.
      const lower = QUALITY_STEPS_KBPS.find((k) => k < currentKbps) ?? null;
      return { nextKbps: lower, state: initialAutoState() };
    }
    return { nextKbps: null, state: { congestedTicks, stableTicks: 0 } };
  }

  const stableTicks = state.stableTicks + 1;
  if (stableTicks >= STEP_UP_TICKS && currentKbps < AUTO_CEILING_KBPS) {
    const higher = [...QUALITY_STEPS_KBPS].reverse().find((k) => k > currentKbps);
    const next = Math.min(higher ?? currentKbps, AUTO_CEILING_KBPS);
    return { nextKbps: next !== currentKbps ? next : null, state: initialAutoState() };
  }
  return { nextKbps: null, state: { congestedTicks: 0, stableTicks } };
}

/**
 * Drive auto bitrate off the upload-health telemetry: evaluates one decision
 * per completed tick while `mode` is 'auto', and calls `onChange` with the new
 * kbps when a step is due. Counters reset whenever the mode flips (manual →
 * auto starts a fresh observation window).
 */
export function useAutoBitrate(opts: {
  mode: Ref<UploadQualityMode>;
  currentKbps: Ref<number>;
  bufferSeconds: Ref<number>;
  /** Tick counter from useUploadHealth — one decision per increment. */
  ticks: Ref<number>;
  onChange: (kbps: number) => void;
}): void {
  let state = initialAutoState();

  watch(opts.mode, () => {
    state = initialAutoState();
  });

  watch(opts.ticks, () => {
    if (opts.mode.value !== 'auto') return;
    const decision = decideBitrate(opts.currentKbps.value, opts.bufferSeconds.value, state);
    state = decision.state;
    if (decision.nextKbps !== null) opts.onChange(decision.nextKbps);
  });
}
