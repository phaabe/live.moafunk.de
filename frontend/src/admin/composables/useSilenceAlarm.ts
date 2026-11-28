import { computed, onScopeDispose, ref } from 'vue';
import { SILENT_DB } from './useSoundcheck';

/** On Air warns after this long without sound (#1053). */
export const SILENCE_ALARM_MS = 10_000;
/** How often the level is read. */
export const SILENCE_SAMPLE_MS = 500;

/** One reading of the sent audio: the louder channel, in dBFS. */
export interface LevelReading {
  rmsDb: number;
  peakDb: number;
}

/**
 * The silence alarm of the live panel (#1053): a show once went out silent
 * while the panel said live. `read` returns the level of the audio that is
 * sent, or null while it cannot be measured (no meter running); then there
 * is no alarm. Under SILENT_DB for SILENCE_ALARM_MS raises the alarm; any
 * sound clears it. Visual only: nothing plays into the host's headphones.
 * Must be called inside an active effect scope.
 */
export function useSilenceAlarm(read: () => LevelReading | null) {
  const now = ref(Date.now());
  const silentSince = ref<number | null>(null);
  /** The last reading, for the level status (Good level, Quiet, Too loud). */
  const last = ref<LevelReading | null>(null);

  function sample() {
    now.value = Date.now();
    const reading = read();
    last.value = reading;
    if (!reading || reading.rmsDb > SILENT_DB) silentSince.value = null;
    else silentSince.value ??= now.value;
  }
  sample();
  const timer = setInterval(sample, SILENCE_SAMPLE_MS);
  onScopeDispose(() => clearInterval(timer));

  const silentMs = computed(() =>
    silentSince.value === null ? 0 : Math.max(0, now.value - silentSince.value)
  );
  return {
    /** True after SILENCE_ALARM_MS without sound. */
    alarm: computed(() => silentMs.value >= SILENCE_ALARM_MS),
    /** Whole seconds without sound so far (0 while there is sound). */
    silentSeconds: computed(() => Math.floor(silentMs.value / 1000)),
    last,
  };
}
