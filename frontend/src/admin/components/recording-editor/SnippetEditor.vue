<script lang="ts">
import { ref } from 'vue';
import type { Snippet } from './edit';

// The Recording tab and the post composer (a popup over it) can both show an
// editor of the same episode: the last save reaches both, one snippet plays.
const lastSave = ref<{ showId: number; snippets: Snippet[]; by: symbol } | null>(null);
const nowPlaying = ref<symbol | null>(null);
</script>

<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, shallowRef, watch } from 'vue';
import { recordingEditApi } from '../../api';
import WaveBars from './WaveBars.vue';
import { useEditPlayer } from './useEditPlayer';
import {
  SNIPPET_MAX,
  SNIPPET_MIN,
  SNIPPET_SLOTS,
  clamp,
  clock,
  fmt,
  gainAt,
  newEdit,
  parseTime,
  type RecordingEdit,
} from './edit';

// The snippets of the picked file (flow screen `episode-recording-instagram`,
// and the post slot of `announce`): up to 3 clips that go into the
// post-announcement as video slides. Placed on the original recording; every
// change is saved at once and leaves the edit and its file alone.

const props = defineProps<{ showId: number }>();
const emit = defineEmits<{ change: [snippets: Snippet[]] }>();

/** Seconds the zoom strip shows around the picked snippet. */
const ZOOM = 120;
const me = Symbol('snippets');

const loading = ref(true);
const loadError = ref<string | null>(null);
const noRecording = ref(false);
const sourceUrl = ref<string | null>(null);
const sourceDuration = ref<number | null>(null);
const edit = ref<RecordingEdit | null>(null);
const peaks = shallowRef<Float32Array | null>(null);
const perSecond = ref(20);
const duration = ref(0);
const snippets = ref<Snippet[]>([]);
const slot = ref(1);
/** Length of each slot, also while it is not placed. */
const lengths = reactive<Record<number, number>>({ 1: 30, 2: 30, 3: 30 });
/** Typed times that are not set yet, by slot. */
const drafts = reactive<Record<number, string>>({});
const saveState = ref<'idle' | 'saving' | 'saved' | 'error'>('idle');
const saveError = ref<string | null>(null);

/** Where a snippet can end at the latest (the server checks its stored length). */
const lastEnd = computed(() => Math.min(duration.value, sourceDuration.value ?? Infinity));
const drawEdit = computed(() => edit.value ?? newEdit(duration.value));

// ---- play: the snippet as it goes into the post -----------------------------

const playingSlot = ref<number | null>(null);
/** The clip's filter: the edit's volume and sections, fades at the snippet's edges. */
const playEdit = computed<RecordingEdit>(() => {
  const s = snippet(playingSlot.value ?? 0);
  const whole = { ...drawEdit.value, start: 0, end: duration.value };
  return s ? { ...whole, start: s.start, end: s.start + s.length } : whole;
});
const player = useEditPlayer(sourceUrl, (t) => gainAt(playEdit.value, t));
const now = computed(() => player.time.value);

watch(player.playing, (on) => {
  if (!on) playingSlot.value = null;
});
watch(nowPlaying, (who) => {
  if (who !== me) player.pause();
});

function togglePlay(n: number) {
  if (playingSlot.value === n) return player.pause();
  const s = snippet(n);
  if (!s) return;
  playingSlot.value = n;
  nowPlaying.value = me;
  void player.play(s.start, s.start + s.length);
}

// ---- load -----------------------------------------------------------------

let stopped = false;
const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

async function load(showId: number) {
  loading.value = true;
  loadError.value = null;
  try {
    const state = await recordingEditApi.get(showId);
    if (stopped) return;
    if (!state.source) {
      noRecording.value = true;
      return;
    }
    sourceUrl.value = state.source.url;
    sourceDuration.value = state.source.duration;
    edit.value = state.edit;
    setSnippets(state.snippets ?? []);
    emit('change', snippets.value);
    for (;;) {
      const res = await recordingEditApi.peaks(showId);
      if (stopped) return;
      if ('peaks' in res) {
        peaks.value = Float32Array.from(res.peaks);
        perSecond.value = res.per_second;
        duration.value = res.duration;
        break;
      }
      // The server is still reading the recording (about 10 s for 2 h).
      await wait(2000);
    }
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : 'Could not load the snippets';
  } finally {
    loading.value = false;
  }
}
void load(props.showId);

function setSnippets(list: Snippet[]) {
  snippets.value = [...list].sort((a, b) => a.slot - b.slot);
  for (const s of snippets.value) lengths[s.slot] = s.length;
  slot.value = SNIPPET_SLOTS.find((n) => !snippet(n)) ?? 1;
}

// Another editor of this episode saved: show its snippets, unless ours wait.
watch(lastSave, (s) => {
  if (s && s.by !== me && s.showId === props.showId && !timer) setSnippets(s.snippets);
});

// ---- save -----------------------------------------------------------------

let timer: ReturnType<typeof setTimeout> | null = null;

async function save() {
  timer = null;
  const list = snippets.value.map((s) => ({ ...s }));
  saveState.value = 'saving';
  saveError.value = null;
  try {
    await recordingEditApi.saveSnippets(props.showId, list);
    lastSave.value = { showId: props.showId, snippets: list, by: me };
    if (!timer) saveState.value = 'saved';
  } catch (e) {
    saveState.value = 'error';
    saveError.value = e instanceof Error ? e.message : 'Could not save the snippets';
  }
}

/** Saves shortly after the last change, so a row of clicks is one save. */
function changed() {
  emit('change', snippets.value);
  saveState.value = 'saving';
  if (timer) clearTimeout(timer);
  timer = setTimeout(save, 600);
}

onBeforeUnmount(() => {
  stopped = true;
  if (timer) {
    clearTimeout(timer);
    void save();
  }
});

// ---- place ----------------------------------------------------------------

const snippet = (n: number) => snippets.value.find((s) => s.slot === n);

function place(n: number, t: number) {
  const length = lengths[n];
  const start = Math.round(clamp(t, 0, lastEnd.value - length) * 10) / 10;
  const s = snippet(n);
  if (s) s.start = start;
  else
    snippets.value = [...snippets.value, { slot: n, start, length }].sort(
      (a, b) => a.slot - b.slot
    );
  delete drafts[n];
  player.seek(start);
  slot.value = SNIPPET_SLOTS.find((k) => !snippet(k)) ?? n;
  changed();
}

function nudge(n: number, by: number) {
  const s = snippet(n);
  if (!s) return;
  s.start = Math.round(clamp(s.start + by, 0, lastEnd.value - s.length) * 10) / 10;
  changed();
}

function setLength(n: number, length: number) {
  lengths[n] = clamp(length, SNIPPET_MIN, SNIPPET_MAX);
  const s = snippet(n);
  if (!s) return;
  s.length = lengths[n];
  s.start = clamp(s.start, 0, lastEnd.value - s.length);
  changed();
}

function remove(n: number) {
  if (playingSlot.value === n) player.pause();
  snippets.value = snippets.value.filter((s) => s.slot !== n);
  slot.value = n;
  changed();
}

/** A typed time counts on Enter or when the field loses focus; a wrong one goes back. */
function commitTime(n: number) {
  const text = drafts[n];
  delete drafts[n];
  if (text === undefined) return;
  const t = parseTime(text);
  if (t !== null) place(n, t);
}

// ---- waveforms --------------------------------------------------------------

/** Seconds at a click on an element that spans `from`..`to`. */
function timeAt(e: MouseEvent, from: number, to: number): number {
  const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
  return from + clamp((e.clientX - r.left) / (r.width || 1), 0, 1) * (to - from);
}

const pctAll = (t: number) => `${(t / (duration.value || 1)) * 100}%`;

/** The zoom shows 2 minutes around the picked snippet, else around the playhead. */
const z0 = computed(() => {
  const s = snippet(slot.value);
  const centre = s ? s.start + s.length / 2 : now.value;
  return clamp(centre - ZOOM / 2, 0, Math.max(0, duration.value - ZOOM));
});
const z1 = computed(() => Math.min(duration.value, z0.value + ZOOM));
const pctZoom = (t: number) => ((t - z0.value) / (z1.value - z0.value || 1)) * 100;

const mapMarks = computed(() =>
  snippets.value.map((s) => ({
    slot: s.slot,
    style: { left: pctAll(s.start), width: pctAll(s.length) },
  }))
);
const zoomMarks = computed(() =>
  snippets.value
    .filter((s) => s.start + s.length > z0.value && s.start < z1.value)
    .map((s) => {
      const l = pctZoom(Math.max(s.start, z0.value));
      return {
        slot: s.slot,
        style: { left: `${l}%`, width: `${pctZoom(Math.min(s.start + s.length, z1.value)) - l}%` },
      };
    })
);

const rows = computed(() =>
  SNIPPET_SLOTS.map((n) => {
    const s = snippet(n);
    const picked = slot.value === n;
    return {
      n,
      s,
      picked,
      length: s?.length ?? lengths[n],
      sub: picked
        ? s
          ? 'Click the waveform to move it'
          : 'Click the waveform or type a time'
        : s
          ? `${clock(s.start)} – ${clock(s.start + s.length)}`
          : 'Empty',
    };
  })
);

const saveLine = computed(() =>
  saveState.value === 'saving' ? 'Saving…' : saveState.value === 'saved' ? 'Saved.' : ''
);
</script>

<template>
  <div class="sn">
    <p v-if="loadError" class="field-error" role="alert">{{ loadError }}</p>
    <p v-else-if="noRecording" class="sn-msg">
      Snippets can be placed once the recording is ready.
    </p>
    <p v-else-if="!peaks" class="sn-msg" aria-live="polite">
      {{ loading ? 'Loading the snippets…' : 'Reading the recording for the waveform…' }}
    </p>

    <template v-else>
      <button
        type="button"
        class="sn-wave sn-map"
        aria-label="Whole set"
        aria-describedby="sn-hint"
        @click="place(slot, timeAt($event, 0, duration))"
      >
        <WaveBars
          mode="map"
          :peaks="peaks"
          :per-second="perSecond"
          :from="0"
          :to="duration"
          :edit="drawEdit"
          :playhead="now"
        />
        <span
          v-for="m in mapMarks"
          :key="m.slot"
          :class="['sn-mark', { picked: m.slot === slot }]"
          :style="m.style"
        ></span>
        <span class="sn-head" :style="{ left: pctAll(now) }"></span>
      </button>
      <span class="sn-lbl">Zoom · {{ clock(z0) }} – {{ clock(z1) }}</span>
      <button
        type="button"
        class="sn-wave sn-zoom"
        aria-label="Zoom"
        aria-describedby="sn-hint"
        @click="place(slot, timeAt($event, z0, z1))"
      >
        <WaveBars
          mode="main"
          :peaks="peaks"
          :per-second="perSecond"
          :from="z0"
          :to="z1"
          :edit="drawEdit"
          :playhead="now"
        />
        <span
          v-for="m in zoomMarks"
          :key="m.slot"
          :class="['sn-band', { picked: m.slot === slot }]"
          :style="m.style"
          >{{ m.slot }}</span
        >
        <span
          v-if="now >= z0 && now <= z1"
          class="sn-head"
          :style="{ left: `${pctZoom(now)}%` }"
        ></span>
      </button>
      <span id="sn-hint" class="sn-mut">Click the waveform to place snippet {{ slot }}.</span>

      <div v-for="r in rows" :key="r.n" :class="['sn-row', { picked: r.picked }]">
        <button
          type="button"
          class="sn-pick"
          :aria-pressed="r.picked"
          :aria-label="`Select snippet ${r.n}`"
          @click="slot = r.n"
        >
          <span :class="['sn-badge', { set: r.s }]">{{ r.n }}</span>
          <span class="sn-text">
            <span class="sn-name">Snippet {{ r.n }}</span>
            <span class="sn-mut">{{ r.sub }}</span>
          </span>
        </button>
        <div class="sn-step" role="group" :aria-label="`Start of snippet ${r.n}`">
          <button
            type="button"
            class="icon-btn"
            :aria-label="`Snippet ${r.n} 0.1 seconds earlier`"
            :disabled="!r.s"
            @click="nudge(r.n, -0.1)"
          >
            −.1
          </button>
          <input
            class="sn-time"
            type="text"
            inputmode="decimal"
            spellcheck="false"
            autocomplete="off"
            placeholder="not placed"
            :aria-label="`Start time of snippet ${r.n}`"
            :value="drafts[r.n] ?? (r.s ? fmt(r.s.start) : '')"
            @focus="slot = r.n"
            @input="drafts[r.n] = ($event.target as HTMLInputElement).value"
            @keydown.enter="($event.target as HTMLInputElement).blur()"
            @blur="commitTime(r.n)"
          />
          <button
            type="button"
            class="icon-btn"
            :aria-label="`Snippet ${r.n} 0.1 seconds later`"
            :disabled="!r.s"
            @click="nudge(r.n, 0.1)"
          >
            +.1
          </button>
        </div>
        <div class="sn-step sn-len" role="group" :aria-label="`Length of snippet ${r.n}`">
          <button
            type="button"
            class="icon-btn"
            :aria-label="`Snippet ${r.n} 1 second shorter`"
            :disabled="r.length <= SNIPPET_MIN"
            @click="setLength(r.n, r.length - 1)"
          >
            −
          </button>
          <output class="sn-len-text">{{ r.length }} s</output>
          <button
            type="button"
            class="icon-btn"
            :aria-label="`Snippet ${r.n} 1 second longer`"
            :disabled="r.length >= SNIPPET_MAX"
            @click="setLength(r.n, r.length + 1)"
          >
            +
          </button>
        </div>
        <button
          type="button"
          class="sn-play"
          :aria-label="`${playingSlot === r.n ? 'Stop' : 'Play'} snippet ${r.n}`"
          :disabled="!r.s"
          @click="togglePlay(r.n)"
        >
          <svg
            v-if="playingSlot === r.n"
            width="18"
            height="18"
            viewBox="0 0 24 24"
            aria-hidden="true"
          >
            <path fill="currentColor" d="M7 5h10v14H7z" />
          </svg>
          <svg v-else width="18" height="18" viewBox="0 0 24 24" aria-hidden="true">
            <path fill="currentColor" d="M8 5v14l11-7z" />
          </svg>
        </button>
        <button
          type="button"
          class="icon-btn"
          :aria-label="`Remove snippet ${r.n}`"
          :disabled="!r.s"
          @click="remove(r.n)"
        >
          <svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            aria-hidden="true"
          >
            <path d="M6 6l12 12M18 6L6 18" />
          </svg>
        </button>
      </div>
      <p class="sn-mut" aria-live="polite">{{ saveLine }}</p>
      <p v-if="saveError" class="field-error" role="alert">{{ saveError }}</p>
      <p v-if="player.error.value" class="field-error">{{ player.error.value }}</p>
    </template>
  </div>
</template>

<style scoped>
.sn {
  display: flex;
  flex-direction: column;
  align-self: stretch;
  gap: 8px;
  min-width: 0;
}

.sn-msg {
  margin: 0;
  font-size: 14px;
  color: #c9c9d6;
}

.sn-wave {
  position: relative;
  display: block;
  width: 100%;
  padding: 0;
  border: 1px solid #2a2a42;
  background: #06060f;
  overflow: hidden;
  cursor: crosshair;
}

.sn-map {
  height: 44px;
  border-radius: 6px;
}

.sn-zoom {
  height: 84px;
  border-radius: 8px;
}

.sn-wave:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

.sn-mark,
.sn-band,
.sn-head {
  position: absolute;
  top: 0;
  bottom: 0;
}

.sn-mark {
  min-width: 3px;
  border-bottom: 4px solid #f472b6;
  background: rgba(244, 114, 182, 0.3);
}

.sn-band {
  display: flex;
  align-items: flex-end;
  justify-content: center;
  min-width: 6px;
  padding-bottom: 2px;
  border-left: 1px solid #f472b6;
  border-right: 1px solid #f472b6;
  background: rgba(244, 114, 182, 0.18);
  color: #f9a8d4;
  font: 700 11px var(--font-family);
}

.sn-mark.picked,
.sn-band.picked {
  background: rgba(244, 114, 182, 0.42);
}

.sn-head {
  width: 2px;
  margin-left: -1px;
  background: #fff;
}

.sn-lbl {
  margin-top: 2px;
  font: 700 11px var(--font-family);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #a8a8bc;
}

.sn-mut {
  margin: 0;
  font-size: 13px;
  color: #a8a8bc;
}

.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 44px;
  height: 44px;
  padding: 0 8px;
  border: 1px solid #34344c;
  border-radius: var(--radius-lg);
  background: transparent;
  color: #dcdce6;
  font: 700 12px var(--font-family);
  cursor: pointer;
}

.icon-btn:hover:not(:disabled) {
  background: #151530;
  color: #fff;
}

.icon-btn:disabled {
  opacity: 0.3;
  cursor: default;
}

.sn-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  padding: 6px 10px;
  border: 1px solid #2a2a42;
  border-radius: 10px;
}

.sn-row.picked {
  padding: 5px 9px;
  border: 2px solid #f472b6;
  background: #1a0f1f;
}

.sn-pick {
  flex: 1 1 180px;
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
  min-height: 48px;
  padding: 2px;
  border: 0;
  background: transparent;
  color: inherit;
  text-align: left;
  cursor: pointer;
}

.sn-badge {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border: 1.5px dashed #f472b6;
  border-radius: 50%;
  color: #f9a8d4;
  font: 700 15px var(--font-family);
}

.sn-badge.set {
  border: 0;
  background: #f472b6;
  color: #1a0010;
}

.sn-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.sn-name {
  font: 700 14px var(--font-family);
  color: #fff;
}

.sn-step {
  display: flex;
  align-items: center;
  gap: 6px;
}

.sn-time {
  width: 132px;
  height: 44px;
  padding: 0 8px;
  border: 1px solid #4a4a60;
  border-radius: var(--radius-lg);
  background: #06060f;
  color: #fff;
  font: 700 18px var(--font-family);
  text-align: center;
  font-variant-numeric: tabular-nums;
}

.sn-time:focus {
  border-color: var(--color-primary);
  outline: none;
}

.sn-len-text {
  min-width: 48px;
  text-align: center;
  font: 700 16px var(--font-family);
  color: #fff;
}

.sn-play {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  border: 0;
  border-radius: 50%;
  background: #f472b6;
  color: #1a0010;
  cursor: pointer;
}

.sn-play:disabled {
  opacity: 0.3;
  cursor: default;
}

.sn-play:focus-visible,
.sn-pick:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}
</style>
