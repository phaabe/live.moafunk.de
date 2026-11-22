<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { gainAt, type RecordingEdit } from './edit';

// Waveform bars of the Edit recording page (#883), drawn on a canvas from the
// server's peaks (a 2 h set is ~170 MB; the browser never decodes it).
// `map`: the whole set, flat colours. `main`: the zoomed part, two-tone bars
// (peak + loudness) that follow the edit's volume, the played part yellow,
// orange caps where the edit is too loud.

const props = defineProps<{
  peaks: ArrayLike<number>;
  perSecond: number;
  /** The seconds the canvas shows. */
  from: number;
  to: number;
  edit: RecordingEdit;
  playhead: number;
  mode: 'map' | 'main';
  /** Orange caps where the edit goes over 0 dB. */
  showClip?: boolean;
}>();

const COLORS = {
  cut: ['#1e1e32', '#2c2c44'],
  kept: ['#43435d', '#b4b4ca'],
  played: ['#6e6830', '#ffec44'],
};
const BAR = 2;
const GAP = 1;

const canvas = ref<HTMLCanvasElement | null>(null);
let frame = 0;
let observer: ResizeObserver | null = null;

function draw() {
  frame = 0;
  const el = canvas.value;
  const g = el?.getContext('2d');
  if (!el || !g) return;
  const ratio = window.devicePixelRatio || 1;
  const w = el.clientWidth;
  const h = el.clientHeight;
  if (el.width !== Math.round(w * ratio) || el.height !== Math.round(h * ratio)) {
    el.width = Math.round(w * ratio);
    el.height = Math.round(h * ratio);
  }
  g.setTransform(ratio, 0, 0, ratio, 0, 0);
  g.clearRect(0, 0, w, h);

  const n = Math.max(1, Math.floor(w / (BAR + GAP)));
  const span = props.to - props.from;
  const mid = h / 2;
  for (let i = 0; i < n; i++) {
    const a = props.from + (i / n) * span;
    const b = props.from + ((i + 1) / n) * span;
    // Loudest peak in the bar's seconds.
    const i0 = Math.max(0, Math.floor(a * props.perSecond));
    const i1 = Math.min(props.peaks.length, Math.max(i0 + 1, Math.ceil(b * props.perSecond)));
    let p = 0;
    for (let k = i0; k < i1; k++) if (props.peaks[k] > p) p = props.peaks[k];
    const t = (a + b) / 2;
    const x = i * (BAR + GAP);
    if (props.mode === 'map') {
      const cut = t < props.edit.start || t > props.edit.end;
      g.fillStyle = cut ? '#2a2a42' : '#7a7a94';
      const bh = Math.max(2, p * h * 0.9);
      g.fillRect(x, mid - bh / 2, BAR, bh);
      continue;
    }
    const gain = gainAt(props.edit, t);
    const v = gain === null ? p : p * gain;
    const c = gain === null ? COLORS.cut : t <= props.playhead ? COLORS.played : COLORS.kept;
    const bh = Math.max(2, Math.min(1, v) * (h - 2));
    g.fillStyle = c[0];
    g.fillRect(x, mid - bh / 2, BAR, bh);
    g.fillStyle = c[1];
    g.fillRect(x, mid - bh * 0.3, BAR, bh * 0.6);
    if (props.showClip && gain !== null && v > 1) {
      g.fillStyle = '#ff7a59';
      g.fillRect(x, mid - bh / 2, BAR, 3);
      g.fillRect(x, mid + bh / 2 - 3, BAR, 3);
    }
  }
}

function schedule() {
  if (!frame) frame = requestAnimationFrame(draw);
}

// Not deep: the peaks are 144 000 values for 2 h; the edit is small.
watch(
  () => [
    props.peaks,
    props.from,
    props.to,
    props.playhead,
    props.showClip,
    JSON.stringify(props.edit),
  ],
  schedule
);

onMounted(() => {
  schedule();
  if (typeof ResizeObserver !== 'undefined' && canvas.value) {
    observer = new ResizeObserver(schedule);
    observer.observe(canvas.value);
  }
});

onBeforeUnmount(() => {
  cancelAnimationFrame(frame);
  observer?.disconnect();
});
</script>

<template>
  <canvas ref="canvas" class="wave-bars" aria-hidden="true"></canvas>
</template>

<style scoped>
.wave-bars {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
}
</style>
