import { ref, onUnmounted, watch, type Ref } from 'vue';

/** Number of bars in the analyzer display (matches the Live Panel 2.0 prototype). */
export const SPECTRUM_BANDS = 28;
/** Band range in Hz — log-spaced from low bass to the top of typical MP3/Opus content. */
const F_MIN = 40;
const F_MAX = 16000;
/** Minimum ms between reactive band updates (rAF is throttled to this). */
const UPDATE_INTERVAL_MS = 40;

/**
 * Group an FFT magnitude array (0..255 per bin, from `getByteFrequencyData`)
 * into `bandCount` log-spaced bands between `fMin` and `fMax`, normalized to
 * 0..1 (per-band peak). Pure — unit-tested separately from the rAF loop.
 */
export function groupBands(
  freqData: Uint8Array,
  sampleRate: number,
  fftSize: number,
  bandCount = SPECTRUM_BANDS,
  fMin = F_MIN,
  fMax = F_MAX
): number[] {
  const binHz = sampleRate / fftSize;
  const logMin = Math.log10(fMin);
  const logMax = Math.log10(fMax);
  const bands = new Array<number>(bandCount).fill(0);

  for (let b = 0; b < bandCount; b++) {
    const lo = Math.pow(10, logMin + ((logMax - logMin) * b) / bandCount);
    const hi = Math.pow(10, logMin + ((logMax - logMin) * (b + 1)) / bandCount);
    const startBin = Math.min(freqData.length - 1, Math.max(0, Math.floor(lo / binHz)));
    const endBin = Math.min(freqData.length - 1, Math.max(startBin, Math.ceil(hi / binHz)));

    let peak = 0;
    for (let i = startBin; i <= endBin; i++) {
      if (freqData[i] > peak) peak = freqData[i];
    }
    bands[b] = peak / 255;
  }

  return bands;
}

/**
 * Drive a bar-spectrum display from a Web-Audio `AnalyserNode`.
 *
 * Follows `useDbMeter`'s lifecycle contract: auto start/stops when the
 * analyser ref changes and cleans up on unmount. The caller owns the analyser
 * (and its AudioContext) — this composable only reads from it.
 */
export function useSpectrum(analyser: Ref<AnalyserNode | null>, bandCount = SPECTRUM_BANDS) {
  /** Normalized band magnitudes (0..1), `bandCount` entries. */
  const bands = ref<number[]>(new Array<number>(bandCount).fill(0));

  let animationId: number | null = null;
  let freqData: Uint8Array<ArrayBuffer> | null = null;
  let lastUpdateTs = 0;

  function update(ts: number) {
    const node = analyser.value;
    if (!node) return;

    if (ts - lastUpdateTs >= UPDATE_INTERVAL_MS) {
      lastUpdateTs = ts;
      if (!freqData || freqData.length !== node.frequencyBinCount) {
        freqData = new Uint8Array(node.frequencyBinCount);
      }
      node.getByteFrequencyData(freqData);
      bands.value = groupBands(freqData, node.context.sampleRate, node.fftSize, bandCount);
    }

    animationId = requestAnimationFrame(update);
  }

  function stop() {
    if (animationId !== null) {
      cancelAnimationFrame(animationId);
      animationId = null;
    }
    bands.value = new Array<number>(bandCount).fill(0);
    freqData = null;
    lastUpdateTs = 0;
  }

  watch(
    analyser,
    (node) => {
      stop();
      if (node) animationId = requestAnimationFrame(update);
    },
    { immediate: true }
  );

  onUnmounted(stop);

  return { bands };
}
