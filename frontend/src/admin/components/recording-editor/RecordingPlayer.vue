<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { recordingEditApi, type RecordingPeaks } from '../../api';
import { clock, newEdit, type RecordingEdit } from './edit';
import WaveBars from './WaveBars.vue';

// The player of the Recording tab (#1082): the picked file as a waveform with
// the marks of its applied edit, like Edit recording. The waveform shows the
// whole original; with an edit, Play plays the edited file, so its time starts
// at the edit's start.

const props = defineProps<{
  showId: number;
  /** Changes when the picked file changes, so the peaks load again. */
  fileKey: string;
  title: string;
  /** What Play plays. */
  src: string | null;
  /** The applied edit: its marks show and `src` is the edited file. */
  edit: RecordingEdit | null;
  downloads: { label: string; url: string }[];
}>();

const peaks = ref<RecordingPeaks | null>(null);
const peaksError = ref(false);
const playing = ref(false);
/** Seconds into the original. */
const head = ref(0);

const duration = computed(() => peaks.value?.duration ?? 0);
const offset = computed(() => props.edit?.start ?? 0);
const length = computed(() => (props.edit ? props.edit.end - props.edit.start : duration.value));
const drawEdit = computed(() => props.edit ?? newEdit(duration.value));
const pct = (t: number) => `${duration.value ? (t / duration.value) * 100 : 0}%`;

const marks = computed(() => {
  const e = props.edit;
  if (!e || !duration.value) return [];
  const box = (kind: string, a: number, b: number) => ({
    kind,
    style: { left: pct(a), width: pct(Math.max(0, b - a)) },
  });
  return [
    box('cut', 0, e.start),
    box('cut', e.end, duration.value),
    ...e.sections.map((s) => box('section', s.start, s.end)),
    ...e.snippets.map((s) => box('snippet', s.start, s.start + s.length)),
  ];
});

let poll: ReturnType<typeof setTimeout> | undefined;
let loadId = 0;
async function loadPeaks() {
  clearTimeout(poll);
  const id = ++loadId;
  peaks.value = null;
  peaksError.value = false;
  if (!props.fileKey) return;
  try {
    const result = await recordingEditApi.peaks(props.showId);
    if (id !== loadId) return;
    if ('peaks' in result) peaks.value = result;
    else poll = setTimeout(loadPeaks, 2000);
  } catch {
    if (id === loadId) peaksError.value = true;
  }
}
watch(() => [props.showId, props.fileKey], loadPeaks, { immediate: true });

let audio: HTMLAudioElement | null = null;
let frame = 0;
function stop() {
  cancelAnimationFrame(frame);
  audio?.pause();
  playing.value = false;
}
watch(
  () => props.src,
  () => {
    stop();
    audio = null;
    head.value = offset.value;
  }
);
watch(offset, (t) => (head.value = t), { immediate: true });

function tick() {
  if (!audio) return;
  head.value = offset.value + audio.currentTime;
  frame = requestAnimationFrame(tick);
}

async function toggle() {
  if (playing.value) return stop();
  if (!props.src) return;
  if (!audio) {
    audio = new Audio(props.src);
    audio.preload = 'metadata';
    audio.addEventListener('ended', stop);
  }
  audio.currentTime = Math.max(0, head.value - offset.value);
  try {
    await audio.play();
  } catch {
    return;
  }
  playing.value = true;
  frame = requestAnimationFrame(tick);
}

/** A click on the waveform jumps there; cut parts are not in the edited file. */
function jump(event: MouseEvent) {
  const box = (event.currentTarget as HTMLElement).getBoundingClientRect();
  if (!box.width || !duration.value) return;
  let t = ((event.clientX - box.left) / box.width) * duration.value;
  if (props.edit) t = Math.min(props.edit.end, Math.max(props.edit.start, t));
  head.value = t;
  if (audio) audio.currentTime = t - offset.value;
}

onBeforeUnmount(() => {
  clearTimeout(poll);
  stop();
  audio?.removeAttribute('src');
});
</script>

<template>
  <div class="rp">
    <div class="rp-head">
      <span class="rp-title">{{ title }}</span>
      <a v-for="d in downloads" :key="d.label" class="rp-dl" :href="d.url" download>
        <svg
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <path d="M12 4v11M7 10l5 5 5-5M5 20h14" /></svg
        ><span>{{ d.label }}</span>
      </a>
    </div>
    <template v-if="peaks">
      <button type="button" class="rp-wave" aria-label="Waveform" @click="jump">
        <WaveBars
          :peaks="peaks.peaks"
          :per-second="peaks.per_second"
          :from="0"
          :to="duration"
          :edit="drawEdit"
          :playhead="head"
          mode="main"
        />
        <span v-for="(m, i) in marks" :key="i" :class="['rp-mark', m.kind]" :style="m.style"></span>
        <template v-if="edit">
          <span class="rp-edge" :style="{ left: pct(edit.start) }"
            ><span class="rp-flag start">START</span></span
          >
          <span class="rp-edge" :style="{ left: pct(edit.end) }"
            ><span class="rp-flag end">END</span></span
          >
        </template>
        <span class="rp-head-line" :style="{ left: pct(head) }"></span>
      </button>
      <div class="rp-controls">
        <button
          type="button"
          class="rp-play"
          :aria-label="playing ? 'Pause' : 'Play'"
          :disabled="!src"
          @click="toggle"
        >
          <svg v-if="playing" width="20" height="20" viewBox="0 0 24 24" aria-hidden="true">
            <path fill="currentColor" d="M7 5h4v14H7zM13 5h4v14h-4z" />
          </svg>
          <svg v-else width="20" height="20" viewBox="0 0 24 24" aria-hidden="true">
            <path fill="currentColor" d="M8 5v14l11-7z" />
          </svg>
        </button>
        <span class="rp-time">{{ clock(head - offset) }} / {{ clock(length) }}</span>
        <span class="rp-sub">{{
          edit
            ? 'Plays the edit. Hatched parts are cut, blue is louder or quieter, pink are snippets.'
            : 'Plays the file as it is.'
        }}</span>
      </div>
    </template>
    <p v-else class="rp-msg">
      {{
        peaksError ? 'The waveform could not be loaded.' : 'Reading the recording for the waveform…'
      }}
    </p>
  </div>
</template>

<style scoped>
.rp {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 14px;
  border: 1px solid #2a2a42;
  border-radius: 10px;
  background: #06060f;
}

.rp-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
}

.rp-title {
  margin-right: auto;
  font: 700 11px var(--font-family);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #a8a8bc;
}

.rp-dl {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 44px;
  padding: 0 12px;
  border-radius: var(--radius-lg);
  color: #c4c4d4;
  font: 700 13px var(--font-family);
  letter-spacing: 0.05em;
  text-transform: uppercase;
  text-decoration: none;
}

.rp-dl:hover {
  background: #151530;
  color: #fff;
}

.rp-wave {
  position: relative;
  display: block;
  width: 100%;
  height: 96px;
  padding: 0;
  border: 1px solid #1e1e34;
  border-radius: 8px;
  background: #0a0a18;
  cursor: pointer;
  overflow: hidden;
}

.rp-mark {
  position: absolute;
  top: 0;
  bottom: 0;
  pointer-events: none;
}

.rp-mark.cut {
  background: repeating-linear-gradient(
    135deg,
    rgba(6, 6, 15, 0.72) 0 5px,
    rgba(6, 6, 15, 0.35) 5px 10px
  );
}

.rp-mark.section {
  background: rgba(96, 165, 250, 0.14);
  border-top: 3px solid #60a5fa;
}

.rp-mark.snippet {
  top: auto;
  height: 6px;
  min-width: 5px;
  background: #f472b6;
}

.rp-edge {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 2px;
  margin-left: -1px;
  background: var(--color-primary);
  pointer-events: none;
}

.rp-flag {
  position: absolute;
  top: 4px;
  padding: 2px 6px;
  background: var(--color-primary);
  color: #000;
  font: 700 11px var(--font-family);
  white-space: nowrap;
}

.rp-flag.start {
  left: 1px;
  border-radius: 0 4px 4px 0;
}

.rp-flag.end {
  right: 1px;
  border-radius: 4px 0 0 4px;
}

.rp-head-line {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 2px;
  margin-left: -1px;
  background: #fff;
  box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.6);
  pointer-events: none;
}

.rp-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 14px;
  margin-top: 4px;
}

.rp-play {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 48px;
  height: 48px;
  border: 0;
  border-radius: 50%;
  background: var(--color-primary);
  color: #000;
  cursor: pointer;
}

.rp-play:disabled {
  opacity: 0.35;
  cursor: default;
}

.rp-time {
  font: 700 18px var(--font-family);
  color: #fff;
  font-variant-numeric: tabular-nums;
}

.rp-sub {
  flex: 1 1 240px;
  font-size: 12px;
  color: #a8a8bc;
}

.rp-msg {
  margin: 0;
  padding: 24px 0;
  font-size: 14px;
  color: #a8a8bc;
}

button:focus-visible,
a:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}
</style>
