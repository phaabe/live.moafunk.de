import { onBeforeUnmount, ref, watch, type Ref } from 'vue';

/** Cut parts still play, quietly, so the host hears where the playhead is. */
const CUT_GAIN = 0.35;

/**
 * Plays the original recording as the edit will sound (#883). An audio element
 * streams the file; its volume cannot go above 100 %, so the gain runs through
 * Web Audio. That needs CORS on the file: without it the element plays on its
 * own and louder parts play at 100 % (`boost` turns false).
 *
 * `gainAt(t)` is the edit's gain at time t, or null where the set is cut.
 */
export function useEditPlayer(src: Ref<string | null>, gainAt: (t: number) => number | null) {
  const time = ref(0);
  const playing = ref(false);
  /** False when the browser cannot play the edit louder than the original. */
  const boost = ref(true);
  const error = ref<string | null>(null);

  let audio: HTMLAudioElement | null = null;
  let ctx: AudioContext | null = null;
  let gain: GainNode | null = null;
  let stopAt: number | null = null;
  let frame = 0;

  function load(url: string, cors: boolean) {
    audio?.pause();
    audio = new Audio();
    audio.preload = 'metadata';
    if (cors) audio.crossOrigin = 'anonymous';
    audio.addEventListener('error', () => {
      // Without CORS headers the crossorigin request fails: play without Web Audio.
      if (cors) {
        boost.value = false;
        load(url, false);
      } else error.value = 'The recording could not be played.';
    });
    audio.addEventListener('ended', () => pause());
    audio.src = url;
    gain = null;
    ctx?.close().catch(() => {});
    ctx = null;
  }

  watch(
    src,
    (url) => {
      pause();
      boost.value = true;
      error.value = null;
      if (url) load(url, true);
    },
    { immediate: true }
  );

  /** Web Audio can only start after a click, so the first Play builds it. */
  function ensureGraph() {
    if (!audio || !boost.value || gain || typeof AudioContext === 'undefined') return;
    ctx = new AudioContext();
    gain = ctx.createGain();
    ctx.createMediaElementSource(audio).connect(gain).connect(ctx.destination);
  }

  function applyGain(t: number) {
    if (!audio) return;
    const g = gainAt(t) ?? CUT_GAIN;
    if (gain && ctx) gain.gain.setTargetAtTime(g, ctx.currentTime, 0.015);
    else audio.volume = Math.min(1, g);
  }

  function tick() {
    if (!audio) return;
    time.value = audio.currentTime;
    if (stopAt !== null && audio.currentTime >= stopAt) {
      seek(stopAt);
      pause();
      return;
    }
    applyGain(audio.currentTime);
    frame = requestAnimationFrame(tick);
  }

  function seek(t: number) {
    time.value = Math.max(0, t);
    if (audio) audio.currentTime = time.value;
    applyGain(time.value);
  }

  /** Play from `from` (or the playhead) and stop at `to`. */
  async function play(from?: number, to?: number) {
    if (!audio) return;
    ensureGraph();
    if (from !== undefined) seek(from);
    stopAt = to ?? null;
    applyGain(time.value);
    try {
      await ctx?.resume();
      await audio.play();
    } catch {
      error.value = 'The recording could not be played.';
      return;
    }
    playing.value = true;
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(tick);
  }

  function pause() {
    cancelAnimationFrame(frame);
    audio?.pause();
    playing.value = false;
    stopAt = null;
  }

  onBeforeUnmount(() => {
    pause();
    audio?.removeAttribute('src');
    audio?.load();
    ctx?.close().catch(() => {});
  });

  return { time, playing, boost, error, play, pause, seek, applyGain };
}
