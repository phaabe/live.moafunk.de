import { ref, onUnmounted, watch, type Ref } from 'vue';

/** Floor of the meter scale in dBFS. Anything quieter clamps here. */
export const DB_FLOOR = -60;
/** Ceiling of the meter scale in dBFS (digital full scale). */
export const DB_CEIL = 0;

/** How long (ms) the peak-hold marker stays before it starts decaying. */
const PEAK_HOLD_MS = 1200;
/** Peak decay rate in dB per second once the hold expires. */
const PEAK_DECAY_DB_PER_S = 12;
/** Release smoothing for the RMS bar (fraction toward target per frame, slow fall). */
const RMS_RELEASE = 0.25;

/**
 * Convert a linear amplitude (0..1) to dBFS, clamped to the meter floor.
 */
function toDb(amplitude: number): number {
  if (amplitude <= 0) return DB_FLOOR;
  const db = 20 * Math.log10(amplitude);
  return Math.max(DB_FLOOR, Math.min(DB_CEIL, db));
}

/** Map a dBFS value to a 0..1 position on the meter scale. */
export function dbToLevel(db: number): number {
  return Math.max(0, Math.min(1, (db - DB_FLOOR) / (DB_CEIL - DB_FLOOR)));
}

/**
 * A calibrated dBFS level meter driven by a MediaStream.
 *
 * Reads time-domain samples from an AnalyserNode, computes the RMS level
 * (converted to dBFS) for the moving bar plus an instantaneous peak with
 * peak-hold + slow decay. Mirrors `useAudioMeter`'s lifecycle: it auto
 * start/stops when the stream changes and cleans up on unmount.
 */
export function useDbMeter(mediaStream: Ref<MediaStream | null>) {
  /** Smoothed RMS level in dBFS (DB_FLOOR..DB_CEIL). */
  const db = ref(DB_FLOOR);
  /** Held peak level in dBFS (DB_FLOOR..DB_CEIL). */
  const peakDb = ref(DB_FLOOR);
  /** RMS level mapped to 0..1 for bar width. */
  const level = ref(0);

  let audioContext: AudioContext | null = null;
  let analyser: AnalyserNode | null = null;
  let source: MediaStreamAudioSourceNode | null = null;
  let animationId: number | null = null;

  // Peak-hold bookkeeping. We track wall-clock-ish time via rAF timestamps.
  let peakValueDb = DB_FLOOR;
  let peakHeldUntil = 0;
  let lastFrameTs = 0;

  function start() {
    if (!mediaStream.value) return;

    try {
      audioContext = new AudioContext();
      analyser = audioContext.createAnalyser();
      analyser.fftSize = 2048;
      analyser.smoothingTimeConstant = 0;

      source = audioContext.createMediaStreamSource(mediaStream.value);
      source.connect(analyser);

      const samples = new Float32Array(analyser.fftSize);

      const update = (ts: number) => {
        if (!analyser) return;

        analyser.getFloatTimeDomainData(samples);

        // RMS over the frame → dBFS.
        let sumSquares = 0;
        let frameMax = 0;
        for (let i = 0; i < samples.length; i++) {
          const s = samples[i];
          sumSquares += s * s;
          const abs = Math.abs(s);
          if (abs > frameMax) frameMax = abs;
        }
        const rms = Math.sqrt(sumSquares / samples.length);
        const rmsDb = toDb(rms);
        const frameMaxDb = toDb(frameMax);

        // RMS bar: fast attack, slow release for a readable meter.
        if (rmsDb >= db.value) {
          db.value = rmsDb;
        } else {
          db.value = db.value + (rmsDb - db.value) * RMS_RELEASE;
        }
        level.value = dbToLevel(db.value);

        // Peak-hold with decay.
        const dt = lastFrameTs ? (ts - lastFrameTs) / 1000 : 0;
        lastFrameTs = ts;

        if (frameMaxDb >= peakValueDb) {
          peakValueDb = frameMaxDb;
          peakHeldUntil = ts + PEAK_HOLD_MS;
        } else if (ts > peakHeldUntil) {
          peakValueDb = Math.max(DB_FLOOR, peakValueDb - PEAK_DECAY_DB_PER_S * dt);
        }
        peakDb.value = peakValueDb;

        animationId = requestAnimationFrame(update);
      };

      animationId = requestAnimationFrame(update);
    } catch (e) {
      console.error('[DbMeter] Failed to start:', e);
    }
  }

  function stop() {
    if (animationId !== null) {
      cancelAnimationFrame(animationId);
      animationId = null;
    }
    if (source) {
      source.disconnect();
      source = null;
    }
    if (audioContext) {
      audioContext.close();
      audioContext = null;
      analyser = null;
    }

    db.value = DB_FLOOR;
    peakDb.value = DB_FLOOR;
    level.value = 0;
    peakValueDb = DB_FLOOR;
    peakHeldUntil = 0;
    lastFrameTs = 0;
  }

  // Auto start/stop when the stream changes.
  watch(
    () => mediaStream.value,
    (stream) => {
      stop();
      if (stream) start();
    },
    { immediate: true }
  );

  onUnmounted(() => {
    stop();
  });

  return {
    db,
    peakDb,
    level,
    start,
    stop,
  };
}
