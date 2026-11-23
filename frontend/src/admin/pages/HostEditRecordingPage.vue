<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, shallowRef, watch } from 'vue';
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import { recordingEditApi, showsApi, type ShowDetail } from '../api';
import BoardIcon from '../components/BoardIcon.vue';
import WaveBars from '../components/recording-editor/WaveBars.vue';
import { useEditPlayer } from '../components/recording-editor/useEditPlayer';
import {
  GAIN_MAX,
  GAIN_MIN,
  SNIPPET_MAX,
  SNIPPET_MIN,
  SNIPPET_SLOTS,
  clamp,
  clampGain,
  clock,
  fmt,
  gainAt,
  loudness,
  newEdit,
  parseTime,
  problems,
  signed,
  span,
  type RecordingEdit,
} from '../components/recording-editor/edit';
import { episodeDay } from '../episodeRow';

// Flow screens `edit-recording` (Snippets), `edit-recording-start-end` and
// `edit-recording-volume` (#883): one page, opened from the Recording card.
// A whole-set map on top, a zoomable waveform with lanes, then three tabs.
// Nothing is saved until Apply edit: it saves the edit and the server makes a
// new file from the original recording.

type Tab = 'snippets' | 'set' | 'volume';

const TABS: { id: Tab; title: string }[] = [
  { id: 'snippets', title: 'Snippets' },
  { id: 'set', title: 'Start & end' },
  { id: 'volume', title: 'Volume' },
];
const ZOOMS = [
  { label: 'All', len: Infinity },
  { label: '30 min', len: 1800 },
  { label: '10 min', len: 600 },
  { label: '2 min', len: 120 },
  { label: '30 s', len: 30 },
];
const TICK_STEPS = [1, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1200, 1800];

const route = useRoute();
const router = useRouter();
const id = computed(() => Number(route.params.id));

const show = ref<ShowDetail | null>(null);
const sourceUrl = ref<string | null>(null);
const noRecording = ref(false);
const loadError = ref<string | null>(null);
const preparing = ref(false);
const peaks = shallowRef<Float32Array | null>(null);
const perSecond = ref(20);
const duration = ref(0);
/** The server's length of the recording, when it knows one. */
const sourceDuration = ref<number | null>(null);
/** The picked file this page edits (#1082): "Show recording" or "Own file · set.mp3". */
const fileName = ref<string | null>(null);
/**
 * Where the set can end at the latest. The server measures the length from the
 * capture and checks the end against it; the decoded file can be a little longer.
 */
const lastEnd = computed(() => Math.min(duration.value, sourceDuration.value ?? Infinity));

const edit = reactive<RecordingEdit>(newEdit(0));
const saved = ref('');
const dirty = computed(() => saved.value !== '' && JSON.stringify(edit) !== saved.value);
const tab = ref<Tab>('snippets');
const slot = ref(1);
/** Length of each snippet slot, also while it is not placed. */
const lengths = reactive<Record<number, number>>({ 1: 30, 2: 30, 3: 30 });
/** Add section: waiting for the first click, then for the second. */
const placing = ref<null | { start?: number }>(null);
const zoom = ref(0);
const viewStart = ref(0);
/** Typed times that are not set yet, by field. */
const drafts = reactive<Record<string, string>>({});
const applying = ref(false);
const actionError = ref<string | null>(null);
let applied = false;

const player = useEditPlayer(sourceUrl, (t) => gainAt(edit, t));
const now = computed(() => player.time.value);

// ---- loading ---------------------------------------------------------------

let stopped = false;
const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

async function loadPeaks(showId: number) {
  for (;;) {
    const res = await recordingEditApi.peaks(showId);
    if (stopped || showId !== id.value) return;
    if ('peaks' in res) {
      peaks.value = Float32Array.from(res.peaks);
      perSecond.value = res.per_second;
      duration.value = res.duration;
      preparing.value = false;
      return;
    }
    // The server is still reading the recording (about 10 s for 2 h).
    preparing.value = true;
    await wait(2000);
  }
}

watch(
  id,
  async (showId) => {
    loadError.value = null;
    peaks.value = null;
    try {
      const [s, state] = await Promise.all([showsApi.get(showId), recordingEditApi.get(showId)]);
      show.value = s;
      if (!state.source) {
        noRecording.value = true;
        return;
      }
      sourceUrl.value = state.source.url;
      sourceDuration.value = state.source.duration;
      fileName.value =
        state.file === 'own'
          ? `Own file · ${state.filename ?? ''}`
          : state.file === 'show'
            ? 'Show recording'
            : null;
      await loadPeaks(showId);
      if (!peaks.value) return;
      Object.assign(edit, state.edit ?? newEdit(lastEnd.value));
      for (const sn of edit.snippets) lengths[sn.slot] = sn.length;
      saved.value = JSON.stringify(edit);
      slot.value = SNIPPET_SLOTS.find((n) => !snippet(n)) ?? 1;
      zoom.value = duration.value > 1800 ? 1 : 0;
      viewStart.value = edit.start;
    } catch (e) {
      loadError.value = e instanceof Error ? e.message : 'Could not load the recording';
    }
  },
  { immediate: true }
);

onBeforeUnmount(() => {
  stopped = true;
});

onBeforeRouteLeave(() => {
  if (!dirty.value || applied) return true;
  return window.confirm('Leave without applying? Your changes are lost.');
});

// ---- view: zoom window -----------------------------------------------------

const viewLen = computed(() => Math.min(ZOOMS[zoom.value].len, duration.value || 1));
const w0 = computed(() => clamp(viewStart.value, 0, Math.max(0, duration.value - viewLen.value)));
const w1 = computed(() => w0.value + viewLen.value);
const pct = (t: number) => ((t - w0.value) / viewLen.value) * 100;
const pctAll = (t: number) => (t / (duration.value || 1)) * 100;
const inView = (t: number) => t >= w0.value && t <= w1.value;

// The window pages along while it plays.
watch(now, (t) => {
  if (player.playing.value && !inView(t)) viewStart.value = t - viewLen.value * 0.1;
});

function setZoom(i: number) {
  zoom.value = clamp(i, 0, ZOOMS.length - 1);
  viewStart.value = now.value - Math.min(ZOOMS[zoom.value].len, duration.value) / 2;
}

const viewLabel = computed(() =>
  viewLen.value >= duration.value ? 'Whole set' : `Showing ${clock(w0.value)} – ${clock(w1.value)}`
);

const ticks = computed(() => {
  const step = TICK_STEPS.find((s) => viewLen.value / s <= 10) ?? 1800;
  const out: { left: string; label: string }[] = [];
  for (let t = Math.ceil(w0.value / step) * step; t <= w1.value; t += step) {
    const p = pct(t);
    if (p < 2 || p > 97) continue;
    out.push({ left: `${p}%`, label: step < 60 ? clock(t) : clock(t).replace(/:\d\d$/, '') });
  }
  return out;
});

/** Seconds at a click on an element that spans `from`..`to`. */
function timeAt(e: MouseEvent, from: number, to: number): number {
  const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
  return from + clamp((e.clientX - r.left) / (r.width || 1), 0, 1) * (to - from);
}

function onMap(e: MouseEvent) {
  const t = timeAt(e, 0, duration.value);
  player.seek(t);
  viewStart.value = t - viewLen.value / 2;
}

function onWave(e: MouseEvent) {
  const t = timeAt(e, w0.value, w1.value);
  if (tab.value === 'snippets') return placeSnippet(slot.value, t);
  if (tab.value === 'volume' && placing.value) {
    if (placing.value.start === undefined) {
      placing.value = { start: t };
      return player.seek(t);
    }
    const a = Math.min(placing.value.start, t);
    const b = Math.max(placing.value.start, t);
    if (b - a < 1) return;
    edit.sections.push({ id: `s${Date.now()}`, start: a, end: b, gainDb: 3 });
    edit.sections.sort((x, y) => x.start - y.start);
    placing.value = null;
    return player.seek(a);
  }
  player.seek(t);
}

// ---- marks on the map and the waveform -------------------------------------

const mapMarks = computed(() => [
  ...edit.sections.map((s) => ({
    cls: 'm-section',
    style: { left: `${pctAll(s.start)}%`, width: `${pctAll(s.end - s.start)}%` },
  })),
  ...edit.snippets.map((s) => ({
    cls: 'm-snippet',
    style: { left: `${pctAll(s.start)}%`, width: `${pctAll(s.length)}%` },
  })),
  { cls: 'm-edge', style: { left: `${pctAll(edit.start)}%` } },
  { cls: 'm-edge', style: { left: `${pctAll(edit.end)}%` } },
]);

const curtains = computed(() => {
  const out: { left: string; width: string }[] = [];
  if (edit.start > w0.value) out.push({ left: '0%', width: `${Math.min(100, pct(edit.start))}%` });
  if (edit.end < w1.value) {
    const l = Math.max(0, pct(edit.end));
    out.push({ left: `${l}%`, width: `${100 - l}%` });
  }
  return out;
});

const visible = (a: number, b: number) => b > w0.value && a < w1.value;
const band = (a: number, b: number) => {
  const l = pct(Math.max(a, w0.value));
  return { left: `${l}%`, width: `${Math.max(0.4, pct(Math.min(b, w1.value)) - l)}%` };
};

const laneSections = computed(() =>
  edit.sections
    .filter((s) => visible(s.start, s.end))
    .map((s) => ({ id: s.id, label: signed(s.gainDb), style: band(s.start, s.end) }))
);
const laneSnippets = computed(() =>
  edit.snippets
    .filter((s) => visible(s.start, s.start + s.length))
    .map((s) => ({
      slot: s.slot,
      label:
        pct(s.start + s.length) - pct(s.start) > 6 ? `${s.slot} · ${s.length} s` : String(s.slot),
      style: band(s.start, s.start + s.length),
    }))
);
const flags = computed(() =>
  [
    { t: edit.start, label: `START ${clock(edit.start)}`, end: false },
    { t: edit.end, label: `END ${clock(edit.end)}`, end: true },
  ].filter((f) => inView(f.t))
);

// ---- tabs ------------------------------------------------------------------

const placedCount = computed(() => edit.snippets.length);
const summaries = computed<Record<Tab, string>>(() => ({
  snippets: `${placedCount.value} of 3 placed`,
  set: `${span(edit.end - edit.start)} · ${signed(edit.gainDb)}`,
  volume:
    edit.sections.length === 0
      ? 'No sections'
      : `${edit.sections.length} section${edit.sections.length === 1 ? '' : 's'}`,
}));

function openTab(t: Tab) {
  tab.value = t;
  placing.value = null;
}

const hint = computed(() => {
  if (tab.value === 'snippets') return `Click the waveform to place snippet ${slot.value}.`;
  if (tab.value === 'volume') {
    if (!placing.value) return 'Add a section, or click to move the playhead.';
    return placing.value.start === undefined
      ? 'Click where the section starts.'
      : 'Now click where it ends.';
  }
  return 'Click to move the playhead, then set start or end.';
});

// ---- snippets ---------------------------------------------------------------

const snippet = (n: number) => edit.snippets.find((s) => s.slot === n);

function placeSnippet(n: number, t: number) {
  const length = lengths[n];
  const start = clamp(t, edit.start, edit.end - length);
  const sn = snippet(n);
  if (sn) sn.start = start;
  else {
    edit.snippets.push({ slot: n, start, length });
    edit.snippets.sort((a, b) => a.slot - b.slot);
  }
  delete drafts[`snip${n}`];
  player.seek(start);
  slot.value = SNIPPET_SLOTS.find((k) => !snippet(k)) ?? n;
}

function setLength(n: number, length: number) {
  lengths[n] = clamp(length, SNIPPET_MIN, SNIPPET_MAX);
  const sn = snippet(n);
  if (sn) {
    sn.length = lengths[n];
    sn.start = clamp(sn.start, edit.start, edit.end - sn.length);
  }
}

function removeSnippet(n: number) {
  edit.snippets = edit.snippets.filter((s) => s.slot !== n);
}

const snippetRows = computed(() =>
  SNIPPET_SLOTS.map((n) => {
    const sn = snippet(n);
    const picked = slot.value === n;
    return {
      n,
      sn,
      picked,
      length: sn?.length ?? lengths[n],
      sub: picked
        ? sn
          ? 'Click the waveform to move it'
          : 'Click the waveform or type a time'
        : sn
          ? 'For the Instagram post'
          : 'Empty',
    };
  })
);

// ---- typed times -----------------------------------------------------------

function timeValue(key: string, t: number | undefined): string {
  return drafts[key] ?? (t === undefined ? '' : fmt(t));
}

/** A typed time counts on Enter or when the field loses focus; a wrong one goes back. */
function commitTime(key: string, set: (t: number) => void) {
  const text = drafts[key];
  delete drafts[key];
  if (text === undefined) return;
  const t = parseTime(text);
  if (t !== null) set(t);
}

function setStart(t: number) {
  edit.start = clamp(t, 0, edit.end - 5);
}
function setEnd(t: number) {
  edit.end = clamp(t, edit.start + 5, lastEnd.value);
}
function nudgeSnippet(n: number, by: number) {
  const sn = snippet(n);
  if (sn) sn.start = clamp(sn.start + by, edit.start, edit.end - sn.length);
}

// ---- volume ----------------------------------------------------------------

const loud = computed(() =>
  peaks.value ? loudness(edit, peaks.value, perSecond.value) : { places: 0, peakDb: -60 }
);
const volFill = computed(() => {
  const pos = ((edit.gainDb - GAIN_MIN) / (GAIN_MAX - GAIN_MIN)) * 100;
  return { left: `${Math.min(50, pos)}%`, width: `${Math.abs(pos - 50)}%` };
});

function stepSection(sid: string, by: number) {
  const s = edit.sections.find((x) => x.id === sid);
  if (s) s.gainDb = clampGain(s.gainDb + by);
}

function removeSection(sid: string) {
  edit.sections = edit.sections.filter((x) => x.id !== sid);
}

// ---- save and apply -------------------------------------------------------

const issues = computed(() => (duration.value ? problems(edit, lastEnd.value) : []));

function discard() {
  if (!saved.value) return;
  Object.assign(edit, JSON.parse(saved.value));
  for (const k of Object.keys(drafts)) delete drafts[k];
  placing.value = null;
  actionError.value = null;
}

async function apply() {
  if (issues.value.length) return;
  applying.value = true;
  actionError.value = null;
  try {
    await recordingEditApi.save(id.value, { ...edit });
    saved.value = JSON.stringify(edit);
    await recordingEditApi.apply(id.value);
    applied = true;
    player.pause();
    await router.push(`/shows/${id.value}`);
  } catch (e) {
    actionError.value = e instanceof Error ? e.message : 'Could not apply the edit';
  } finally {
    applying.value = false;
  }
}

const header = computed(() => {
  const s = show.value;
  if (!s) return '';
  const name = s.series_name ?? s.title;
  const length = duration.value ? ` · original ${clock(duration.value)}` : '';
  const file = fileName.value ? ` · ${fileName.value}` : '';
  return `${name} · ${episodeDay(s.date)}${file}${length}`;
});
</script>

<template>
  <div class="edit-recording host-page">
    <router-link :to="`/shows/${id}`" class="back-link"
      ><BoardIcon name="back" :size="16" /><span class="sep">&lt; </span>Episode</router-link
    >
    <h1 class="page-title">Edit recording</h1>
    <p v-if="header" class="er-header">{{ header }}</p>

    <p v-if="loadError" class="field-error" role="alert">{{ loadError }}</p>
    <p v-else-if="noRecording" class="er-empty">
      This episode has no recording yet. You can edit it once the recording is ready.
    </p>
    <p v-else-if="!peaks" class="er-empty" aria-live="polite">
      {{ preparing ? 'Reading the recording for the waveform…' : 'Loading the recording…' }}
    </p>

    <template v-else>
      <section class="er-card er-wave" aria-label="Recording">
        <button
          type="button"
          class="er-map"
          aria-label="Whole set"
          aria-describedby="er-map-help"
          @click="onMap"
        >
          <WaveBars
            mode="map"
            :peaks="peaks"
            :per-second="perSecond"
            :from="0"
            :to="duration"
            :edit="edit"
            :playhead="now"
          />
          <span
            v-for="(m, i) in mapMarks"
            :key="i"
            :class="['er-mark', m.cls]"
            :style="m.style"
          ></span>
          <span class="er-dim" :style="{ left: '0', width: `${pctAll(w0)}%` }"></span>
          <span class="er-dim" :style="{ left: `${pctAll(w1)}%`, right: '0' }"></span>
          <span
            class="er-frame"
            :style="{ left: `${pctAll(w0)}%`, width: `${pctAll(viewLen)}%` }"
          ></span>
          <span class="er-head" :style="{ left: `${pctAll(now)}%` }"></span>
        </button>

        <div class="er-zoom-row">
          <span class="er-lbl">{{ viewLabel }}</span>
          <div class="er-zoom" role="group" aria-label="Zoom">
            <button
              type="button"
              class="icon-btn"
              aria-label="Zoom out"
              :disabled="zoom === 0"
              @click="setZoom(zoom - 1)"
            >
              <svg
                width="18"
                height="18"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                aria-hidden="true"
              >
                <circle cx="11" cy="11" r="7" />
                <path d="M20 20l-3.5-3.5M8 11h6" />
              </svg>
            </button>
            <output class="er-zoom-text">{{ ZOOMS[zoom].label }}</output>
            <button
              type="button"
              class="icon-btn"
              aria-label="Zoom in"
              :disabled="zoom === ZOOMS.length - 1"
              @click="setZoom(zoom + 1)"
            >
              <svg
                width="18"
                height="18"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                aria-hidden="true"
              >
                <circle cx="11" cy="11" r="7" />
                <path d="M20 20l-3.5-3.5M11 8v6M8 11h6" />
              </svg>
            </button>
            <BaseButton variant="ghost" size="sm" :disabled="zoom === 0" @click="setZoom(0)"
              >Show all</BaseButton
            >
          </div>
        </div>

        <div
          class="er-lane er-lane-sections"
          :class="{ faded: tab !== 'volume' }"
          aria-hidden="true"
        >
          <span v-for="s in laneSections" :key="s.id" class="er-lane-section" :style="s.style">{{
            s.label
          }}</span>
        </div>
        <button
          type="button"
          class="er-main"
          aria-label="Waveform"
          aria-describedby="er-hint"
          @click="onWave"
        >
          <WaveBars
            mode="main"
            :peaks="peaks"
            :per-second="perSecond"
            :from="w0"
            :to="w1"
            :edit="edit"
            :playhead="now"
            :show-clip="tab === 'set'"
          />
          <span v-for="(c, i) in curtains" :key="i" class="er-curtain" :style="c"></span>
          <template v-if="tab === 'volume'">
            <span
              v-for="s in laneSections"
              :key="s.id"
              class="er-section-band"
              :style="s.style"
            ></span>
          </template>
          <span
            v-if="tab === 'volume' && placing?.start !== undefined && inView(placing.start)"
            class="er-placing"
            :style="{ left: `${pct(placing.start)}%` }"
          ></span>
          <span
            v-for="f in flags"
            :key="f.label"
            :class="['er-flag-line', { faded: tab !== 'set' }]"
            :style="{ left: `${pct(f.t)}%` }"
            ><span v-if="tab === 'set'" :class="['er-flag', { end: f.end }]">{{
              f.label
            }}</span></span
          >
          <span v-if="inView(now)" class="er-playhead" :style="{ left: `${pct(now)}%` }"></span>
        </button>
        <div
          class="er-lane er-lane-snippets"
          :class="{ faded: tab !== 'snippets' }"
          aria-hidden="true"
        >
          <span
            v-for="s in laneSnippets"
            :key="s.slot"
            :class="['er-lane-snippet', { picked: tab === 'snippets' && slot === s.slot }]"
            :style="s.style"
            >{{ s.label }}</span
          >
        </div>
        <div class="er-ruler" aria-hidden="true">
          <span v-for="t in ticks" :key="t.left" :style="{ left: t.left }">{{ t.label }}</span>
        </div>

        <div class="er-transport">
          <button
            type="button"
            class="er-play"
            :aria-label="player.playing.value ? 'Pause' : 'Play'"
            @click="player.playing.value ? player.pause() : player.play()"
          >
            <svg
              v-if="player.playing.value"
              width="20"
              height="20"
              viewBox="0 0 24 24"
              fill="currentColor"
              aria-hidden="true"
            >
              <path d="M7 5h4v14H7zM13 5h4v14h-4z" />
            </svg>
            <svg
              v-else
              width="20"
              height="20"
              viewBox="0 0 24 24"
              fill="currentColor"
              aria-hidden="true"
            >
              <path d="M8 5v14l11-7z" />
            </svg>
          </button>
          <div class="er-now">
            <span class="er-time">{{ fmt(now) }}</span>
            <span class="er-mut">{{
              now >= edit.start && now <= edit.end
                ? `${span(now - edit.start)} into the set`
                : 'In a part that gets cut'
            }}</span>
          </div>
          <span id="er-hint" class="er-hint" aria-live="polite">{{ hint }}</span>
        </div>
        <span id="er-map-help" hidden>Click to move the view and the playhead there.</span>
        <p v-if="!player.boost.value" class="er-mut">
          This browser plays louder parts at the original volume here. The new file gets the full
          volume.
        </p>
        <p v-if="player.error.value" class="field-error">{{ player.error.value }}</p>
      </section>

      <section class="er-card er-tabs-card">
        <div class="er-tabs" role="tablist" aria-label="Edit steps">
          <button
            v-for="(t, i) in TABS"
            :id="`er-tab-${t.id}`"
            :key="t.id"
            type="button"
            role="tab"
            :aria-selected="tab === t.id"
            :aria-label="t.title"
            :aria-describedby="`er-sum-${t.id}`"
            :class="['er-tab', { on: tab === t.id }]"
            @click="openTab(t.id)"
          >
            <span class="er-badge">{{ i + 1 }}</span>
            <span class="er-tab-text">
              <span class="er-tab-title">{{ t.title }}</span>
              <span :id="`er-sum-${t.id}`" class="er-tab-sum">{{ summaries[t.id] }}</span>
            </span>
          </button>
        </div>

        <div class="er-panel" role="tabpanel" :aria-labelledby="`er-tab-${tab}`">
          <!-- 1 Snippets -->
          <template v-if="tab === 'snippets'">
            <div class="er-info pink">
              <span class="er-info-icon" aria-hidden="true"
                ><svg
                  width="22"
                  height="22"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                >
                  <rect x="3" y="3" width="18" height="18" rx="5" />
                  <circle cx="12" cy="12" r="4" />
                  <circle cx="17.5" cy="6.5" r="0.6" fill="currentColor" /></svg
              ></span>
              <div>
                <h2>For the Instagram post</h2>
                <p>
                  Snippets are short clips from your set, up to 30 s each. They go into the
                  Instagram post that announces the recording, so pick the moments that make people
                  want to listen.
                </p>
              </div>
            </div>
            <div v-for="r in snippetRows" :key="r.n" :class="['er-snip', { picked: r.picked }]">
              <button
                type="button"
                class="er-snip-pick"
                :aria-pressed="r.picked"
                :aria-label="`Select snippet ${r.n}`"
                @click="slot = r.n"
              >
                <span :class="['er-snip-badge', { set: r.sn }]">{{ r.n }}</span>
                <span class="er-snip-text">
                  <span class="er-snip-name">Snippet {{ r.n }}</span>
                  <span class="er-mut">{{ r.sub }}</span>
                </span>
              </button>
              <div class="er-step" role="group" :aria-label="`Start of snippet ${r.n}`">
                <button
                  type="button"
                  class="icon-btn"
                  :aria-label="`Snippet ${r.n} 0.1 seconds earlier`"
                  :disabled="!r.sn"
                  @click="nudgeSnippet(r.n, -0.1)"
                >
                  −.1
                </button>
                <input
                  class="er-time-input"
                  type="text"
                  inputmode="decimal"
                  spellcheck="false"
                  autocomplete="off"
                  placeholder="not placed"
                  :aria-label="`Start time of snippet ${r.n}`"
                  :value="timeValue(`snip${r.n}`, r.sn?.start)"
                  @focus="slot = r.n"
                  @input="drafts[`snip${r.n}`] = ($event.target as HTMLInputElement).value"
                  @keydown.enter="($event.target as HTMLInputElement).blur()"
                  @blur="commitTime(`snip${r.n}`, (t) => placeSnippet(r.n, t))"
                />
                <button
                  type="button"
                  class="icon-btn"
                  :aria-label="`Snippet ${r.n} 0.1 seconds later`"
                  :disabled="!r.sn"
                  @click="nudgeSnippet(r.n, 0.1)"
                >
                  +.1
                </button>
              </div>
              <div class="er-step er-len" role="group" :aria-label="`Length of snippet ${r.n}`">
                <button
                  type="button"
                  class="icon-btn"
                  :aria-label="`Snippet ${r.n} 1 second shorter`"
                  :disabled="r.length <= SNIPPET_MIN"
                  @click="setLength(r.n, r.length - 1)"
                >
                  −
                </button>
                <output class="er-len-text">{{ r.length }} s</output>
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
                class="er-snip-play"
                :aria-label="`Play snippet ${r.n}`"
                :disabled="!r.sn"
                @click="r.sn && player.play(r.sn.start, r.sn.start + r.sn.length)"
              >
                <svg
                  width="18"
                  height="18"
                  viewBox="0 0 24 24"
                  fill="currentColor"
                  aria-hidden="true"
                >
                  <path d="M8 5v14l11-7z" />
                </svg>
              </button>
              <button
                type="button"
                class="icon-btn"
                :aria-label="`Remove snippet ${r.n}`"
                :disabled="!r.sn"
                @click="removeSnippet(r.n)"
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
            <div class="er-next">
              <BaseButton variant="secondary" @click="openTab('set')"
                >Next: start &amp; end</BaseButton
              >
            </div>
          </template>

          <!-- 2 Start & end -->
          <template v-else-if="tab === 'set'">
            <div class="er-info yellow">
              <span class="er-info-icon" aria-hidden="true"
                ><svg
                  width="22"
                  height="22"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                >
                  <circle cx="6" cy="6" r="3" />
                  <circle cx="6" cy="18" r="3" />
                  <path d="M20 4L8.12 15.88M14.47 14.48L20 20M8.12 8.12L12 12" /></svg
              ></span>
              <div>
                <h2>Cut the set</h2>
                <p>
                  Set where your set starts and ends. Everything before and after is cut, like the
                  soundcheck or the silence at the end, and the cuts get a short fade. Here you also
                  set the volume of the whole set.
                </p>
              </div>
            </div>
            <div class="er-set">
              <template
                v-for="e in [
                  { key: 'start', label: 'Start', t: edit.start, set: setStart },
                  { key: 'end', label: 'End', t: edit.end, set: setEnd },
                ]"
                :key="e.key"
              >
                <label :for="`er-${e.key}`" :class="['er-lbl', `a-${e.key}l`]">{{ e.label }}</label>
                <div :class="['er-step', `a-${e.key}c`]">
                  <button
                    type="button"
                    class="icon-btn"
                    :aria-label="`${e.label} 0.1 seconds earlier`"
                    @click="e.set(e.t - 0.1)"
                  >
                    −.1
                  </button>
                  <input
                    :id="`er-${e.key}`"
                    class="er-time-input"
                    type="text"
                    inputmode="decimal"
                    spellcheck="false"
                    autocomplete="off"
                    aria-describedby="er-time-help"
                    :value="timeValue(e.key, e.t)"
                    @input="drafts[e.key] = ($event.target as HTMLInputElement).value"
                    @keydown.enter="($event.target as HTMLInputElement).blur()"
                    @blur="commitTime(e.key, e.set)"
                  />
                  <button
                    type="button"
                    class="icon-btn"
                    :aria-label="`${e.label} 0.1 seconds later`"
                    @click="e.set(e.t + 0.1)"
                  >
                    +.1
                  </button>
                  <BaseButton variant="secondary" class="er-to-playhead" @click="e.set(now)"
                    >Set to playhead</BaseButton
                  >
                </div>
              </template>
              <span id="er-time-help" class="er-mut a-help"
                >Type a time like 0:06:09.6 and press Enter.</span
              >
              <div class="a-div" aria-hidden="true"></div>
              <span id="er-vol-label" class="label er-lbl a-vl">Volume of the whole set</span>
              <div class="er-vol a-vc" role="group" aria-labelledby="er-vol-label">
                <button
                  type="button"
                  class="icon-btn"
                  aria-label="Whole set 0.5 dB quieter"
                  :disabled="edit.gainDb <= GAIN_MIN"
                  @click="edit.gainDb = clampGain(edit.gainDb - 0.5)"
                >
                  −
                </button>
                <output class="er-vol-text">{{ signed(edit.gainDb) }}</output>
                <button
                  type="button"
                  class="icon-btn"
                  aria-label="Whole set 0.5 dB louder"
                  :disabled="edit.gainDb >= GAIN_MAX"
                  @click="edit.gainDb = clampGain(edit.gainDb + 0.5)"
                >
                  +
                </button>
              </div>
              <div class="er-vol-bar a-vb" aria-hidden="true">
                <span class="er-vol-track"
                  ><span class="er-vol-fill" :style="volFill"></span
                  ><span class="er-vol-zero"></span
                ></span>
                <span class="er-vol-scale"><span>−12</span><span>0 dB</span><span>+12</span></span>
              </div>
              <div class="er-vol-notes a-vn">
                <p :class="loud.places ? 'warn' : 'ok'">
                  {{
                    loud.places
                      ? `Too loud in ${loud.places} ${loud.places === 1 ? 'place' : 'places'}. The limiter catches it, but a little less volume sounds better.`
                      : `Loudest peak ${loud.peakDb.toFixed(1)} dB. No clipping.`
                  }}
                </p>
                <p class="er-mut">
                  The set is <b>{{ fmt(edit.end - edit.start) }}</b> long. Cut parts get a short
                  fade.
                </p>
              </div>
            </div>
            <div class="er-next">
              <BaseButton variant="secondary" @click="openTab('volume')">Next: volume</BaseButton>
            </div>
          </template>

          <!-- 3 Volume -->
          <template v-else>
            <div class="er-info blue">
              <span class="er-info-icon" aria-hidden="true"
                ><svg
                  width="22"
                  height="22"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                >
                  <path d="M11 5L6 9H2v6h4l5 4V5z" />
                  <path d="M15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14" /></svg
              ></span>
              <div>
                <h2>Fix loud or quiet parts</h2>
                <p>
                  Mark a part that is too loud or too quiet, like a talk break or one loud track,
                  and change only its volume. The rest of the set stays as it is. The volume changes
                  smoothly at the edges, so there are no clicks.
                </p>
              </div>
            </div>
            <p v-if="edit.sections.length === 0 && !placing" class="er-mut">
              No sections yet. The whole set plays at one volume.
            </p>
            <div v-for="(s, i) in edit.sections" :key="s.id" class="er-section">
              <span class="er-dot" aria-hidden="true"></span>
              <span class="er-section-range">{{ clock(s.start) }} → {{ clock(s.end) }}</span>
              <span class="er-mut">{{ span(s.end - s.start) }}</span>
              <div
                class="er-vol er-section-vol"
                role="group"
                :aria-label="`Volume of section ${i + 1}`"
              >
                <button
                  type="button"
                  class="icon-btn"
                  :aria-label="`Section ${i + 1} 0.5 dB quieter`"
                  :disabled="s.gainDb <= GAIN_MIN"
                  @click="stepSection(s.id, -0.5)"
                >
                  −
                </button>
                <output class="er-section-db">{{ signed(s.gainDb) }}</output>
                <button
                  type="button"
                  class="icon-btn"
                  :aria-label="`Section ${i + 1} 0.5 dB louder`"
                  :disabled="s.gainDb >= GAIN_MAX"
                  @click="stepSection(s.id, 0.5)"
                >
                  +
                </button>
              </div>
              <button
                type="button"
                class="icon-btn"
                :aria-label="`Play section ${i + 1}`"
                @click="player.play(s.start, s.end)"
              >
                <svg
                  width="16"
                  height="16"
                  viewBox="0 0 24 24"
                  fill="currentColor"
                  aria-hidden="true"
                >
                  <path d="M8 5v14l11-7z" />
                </svg>
              </button>
              <button
                type="button"
                class="icon-btn"
                :aria-label="`Remove section ${i + 1}`"
                @click="removeSection(s.id)"
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
            <div v-if="placing" class="er-placing-box">
              <span>{{
                placing.start === undefined
                  ? 'Click on the waveform where the section starts.'
                  : `Starts at ${clock(placing.start)}. Now click where it ends.`
              }}</span>
              <BaseButton variant="ghost" size="sm" @click="placing = null">Cancel</BaseButton>
            </div>
            <div class="er-next start">
              <BaseButton variant="secondary" :disabled="!!placing" @click="placing = {}"
                >Add section</BaseButton
              >
            </div>
          </template>
        </div>
      </section>

      <ul v-if="issues.length" class="er-issues" role="alert">
        <li v-for="p in issues" :key="p">{{ p }}</li>
      </ul>
      <p v-if="actionError" class="field-error" role="alert">{{ actionError }}</p>
      <div class="er-footer">
        <span class="er-mut">{{
          dirty
            ? 'Nothing changes until you apply. The original stays.'
            : 'The original recording stays as it is.'
        }}</span>
        <div class="er-footer-actions">
          <BaseButton variant="ghost" :disabled="!dirty || applying" @click="discard"
            >Discard</BaseButton
          >
          <BaseButton class="xl" :disabled="issues.length > 0" :loading="applying" @click="apply"
            >Apply edit</BaseButton
          >
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.edit-recording {
  max-width: 1040px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.er-header {
  margin: -8px 0 0;
  color: #a8a8bc;
  font-size: 14px;
}

.er-empty {
  margin: 0;
  color: #c9c9d6;
}

.er-card {
  border: 2px solid #34344c;
  border-radius: var(--radius-xl);
  background: var(--color-surface);
}

.er-wave {
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.er-map,
.er-main {
  position: relative;
  display: block;
  width: 100%;
  padding: 0;
  border: 1px solid #2a2a42;
  background: #06060f;
  overflow: hidden;
}

.er-map {
  height: 44px;
  border-radius: 6px;
  cursor: pointer;
}

.er-main {
  height: 150px;
  border-radius: 8px;
  cursor: crosshair;
}

.er-mark,
.er-dim,
.er-frame,
.er-head,
.er-curtain,
.er-section-band,
.er-placing,
.er-flag-line,
.er-playhead {
  position: absolute;
  top: 0;
  bottom: 0;
}

.m-section {
  min-width: 3px;
  border-top: 4px solid #60a5fa;
  background: rgba(96, 165, 250, 0.28);
}

.m-snippet {
  min-width: 3px;
  border-bottom: 4px solid #f472b6;
  background: rgba(244, 114, 182, 0.3);
}

.m-edge {
  width: 2px;
  margin-left: -1px;
  background: var(--color-primary);
}

.er-dim {
  background: rgba(0, 0, 0, 0.55);
}

.er-frame {
  min-width: 6px;
  border: 2px solid #fff;
  border-radius: 4px;
}

.er-head,
.er-playhead {
  width: 2px;
  margin-left: -1px;
  background: #fff;
}

.er-zoom-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  margin-top: 6px;
}

.er-lbl {
  font: 700 11px var(--font-family);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #a8a8bc;
}

.er-zoom {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-left: auto;
}

.er-zoom-text {
  min-width: 64px;
  text-align: center;
  font: 700 14px var(--font-family);
  color: #fff;
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

.er-lane {
  position: relative;
  height: 22px;
}

.er-lane-snippets {
  height: 24px;
}

.er-lane.faded {
  opacity: 0.35;
}

.er-lane-section,
.er-lane-snippet {
  position: absolute;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  white-space: nowrap;
  font: 700 11px var(--font-family);
}

.er-lane-section {
  top: 1px;
  bottom: 3px;
  min-width: 6px;
  border: 1px solid #60a5fa;
  border-radius: 4px;
  background: #24446e;
  color: #dbeaff;
}

.er-lane-snippet {
  top: 3px;
  bottom: 1px;
  min-width: 20px;
  border-radius: 999px;
  background: #f472b6;
  color: #1a0010;
}

.er-lane-snippet.picked {
  box-shadow: 0 0 0 2px #fff;
}

.er-curtain {
  background: rgba(0, 0, 0, 0.45);
}

.er-section-band {
  border-left: 1px solid #60a5fa;
  border-right: 1px solid #60a5fa;
  background: rgba(96, 165, 250, 0.14);
}

.er-placing {
  border-left: 2px dashed #60a5fa;
}

.er-flag-line {
  width: 2px;
  margin-left: -1px;
  background: var(--color-primary);
}

.er-flag-line.faded {
  opacity: 0.5;
}

.er-flag {
  position: absolute;
  top: 5px;
  left: 2px;
  padding: 2px 6px;
  border-radius: 0 4px 4px 0;
  background: var(--color-primary);
  color: #000;
  font: 700 11px var(--font-family);
  white-space: nowrap;
}

.er-flag.end {
  left: auto;
  right: 2px;
  border-radius: 4px 0 0 4px;
}

.er-ruler {
  position: relative;
  height: 16px;
}

.er-ruler span {
  position: absolute;
  top: 1px;
  transform: translateX(-50%);
  font: 700 11px var(--font-family);
  color: #8e8ea4;
  white-space: nowrap;
}

.er-transport {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px 14px;
  margin-top: 6px;
}

.er-play {
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

.er-now {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.er-time {
  font: 700 18px var(--font-family);
  color: #fff;
  font-variant-numeric: tabular-nums;
}

.er-hint {
  flex: 1;
  min-width: 180px;
  text-align: right;
  font-size: 13px;
  color: #c9c9d6;
}

.er-mut {
  margin: 0;
  font-size: 13px;
  color: #a8a8bc;
}

.er-tabs-card {
  overflow: hidden;
}

.er-tabs {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  border-bottom: 1px solid #2a2a42;
}

.er-tab {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 68px;
  margin-bottom: -1px;
  padding: 10px 16px;
  border: 0;
  border-bottom: 3px solid transparent;
  background: transparent;
  color: #b0b0c4;
  text-align: left;
  cursor: pointer;
}

.er-tab:hover {
  background: #151530;
}

.er-tab.on {
  border-bottom-color: var(--color-primary);
  background: #151530;
  color: #fff;
}

.er-badge {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: 1.5px solid #8e8ea4;
  border-radius: 50%;
  color: #a8a8bc;
  font: 700 13px var(--font-family);
}

.er-tab.on .er-badge {
  border-color: var(--color-primary);
  background: var(--color-primary);
  color: #000;
}

.er-tab-text {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.er-tab-title {
  font: 700 13px var(--font-family);
  letter-spacing: 0.1em;
  text-transform: uppercase;
}

.er-tab-sum {
  overflow: hidden;
  font-size: 12px;
  color: #a8a8bc;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.er-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 18px 16px;
}

.er-info {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  margin-bottom: 6px;
  padding: 14px 16px;
  border: 1px solid;
  border-radius: 10px;
}

.er-info h2 {
  margin: 0 0 4px;
  font: 700 13px var(--font-family);
  letter-spacing: 0.1em;
  text-transform: uppercase;
}

.er-info p {
  max-width: 640px;
  margin: 0;
  font-size: 14px;
  line-height: 1.5;
  color: #e4e4ee;
}

.er-info-icon {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border-radius: 10px;
}

.er-info.pink {
  border-color: rgba(244, 114, 182, 0.45);
  background: rgba(244, 114, 182, 0.08);
}

.er-info.pink h2 {
  color: #f9a8d4;
}

.er-info.pink .er-info-icon {
  background: #f472b6;
  color: #1a0010;
}

.er-info.yellow {
  border-color: rgba(255, 236, 68, 0.4);
  background: rgba(255, 236, 68, 0.06);
}

.er-info.yellow h2 {
  color: var(--color-primary);
}

.er-info.yellow .er-info-icon {
  background: var(--color-primary);
  color: #000;
}

.er-info.blue {
  border-color: rgba(96, 165, 250, 0.45);
  background: rgba(96, 165, 250, 0.08);
}

.er-info.blue h2 {
  color: #9cc5f8;
}

.er-info.blue .er-info-icon {
  background: #60a5fa;
  color: #06101f;
}

.er-step {
  display: flex;
  align-items: center;
  gap: 6px;
}

.er-time-input {
  width: 150px;
  height: 44px;
  padding: 0 10px;
  border: 1px solid #4a4a60;
  border-radius: var(--radius-lg);
  background: #06060f;
  color: #fff;
  font: 700 20px var(--font-family);
  text-align: center;
  font-variant-numeric: tabular-nums;
}

.er-time-input:focus {
  border-color: var(--color-primary);
  outline: none;
}

.er-snip {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  padding: 6px 10px;
  border: 1px solid #2a2a42;
  border-radius: 10px;
}

.er-snip.picked {
  padding: 5px 9px;
  border: 2px solid #f472b6;
  background: #1a0f1f;
}

.er-snip-pick {
  flex: 1 1 200px;
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

.er-snip-badge {
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

.er-snip-badge.set {
  border: 0;
  background: #f472b6;
  color: #1a0010;
}

.er-snip-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.er-snip-name {
  font: 700 14px var(--font-family);
  color: #fff;
}

.er-len {
  margin-left: 18px;
}

.er-len-text {
  min-width: 56px;
  text-align: center;
  font: 700 18px var(--font-family);
  color: #fff;
}

.er-snip-play {
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

.er-snip-play:disabled {
  opacity: 0.3;
  cursor: default;
}

.er-next {
  display: flex;
  justify-content: flex-end;
  padding-top: 12px;
  border-top: 1px solid #1e1e34;
}

.er-next.start {
  justify-content: flex-start;
}

/* Start & end: one grid, so both sides line up row by row. */
.er-set {
  display: grid;
  grid-template-columns: minmax(0, 1.3fr) 1px minmax(0, 1fr);
  grid-template-areas:
    'sl d vl'
    'sc d vc'
    'el d vb'
    'ec d vb'
    'h d vn';
  align-items: start;
  gap: 6px 32px;
}

.a-startl {
  grid-area: sl;
}

.a-startc {
  grid-area: sc;
}

.a-endl {
  grid-area: el;
  margin-top: 8px;
}

.a-endc {
  grid-area: ec;
}

.a-help {
  grid-area: h;
  margin-top: 8px;
}

.a-div {
  grid-area: d;
  align-self: stretch;
  background: #2a2a42;
}

.a-vl {
  grid-area: vl;
}

.a-vc {
  grid-area: vc;
}

.a-vb {
  grid-area: vb;
  margin-top: 8px;
}

.a-vn {
  grid-area: vn;
  margin-top: 8px;
}

.er-to-playhead {
  margin-left: 18px;
}

.er-vol {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 300px;
  max-width: 100%;
}

.er-vol-text {
  font: 700 22px var(--font-family);
  color: #fff;
}

.er-vol-bar {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 300px;
  max-width: 100%;
}

.er-vol-track {
  position: relative;
  height: 6px;
  margin-top: 4px;
  border-radius: 3px;
  background: #34344c;
}

.er-vol-fill {
  position: absolute;
  top: 0;
  bottom: 0;
  border-radius: 3px;
  background: var(--color-primary);
}

.er-vol-zero {
  position: absolute;
  top: -4px;
  left: 50%;
  width: 2px;
  height: 14px;
  margin-left: -1px;
  background: #8e8ea4;
}

.er-vol-scale {
  display: flex;
  justify-content: space-between;
  font: 700 11px var(--font-family);
  color: #8e8ea4;
}

.er-vol-notes {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-width: 300px;
}

.er-vol-notes p {
  margin: 0;
  font-size: 13px;
  line-height: 1.45;
}

.er-vol-notes .warn {
  color: #fbbf24;
}

.er-vol-notes .ok {
  color: #4ade80;
}

.er-section {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 14px;
  padding: 8px 12px;
  border: 1px solid #2a2a42;
  border-radius: 10px;
}

.er-dot {
  flex: none;
  width: 10px;
  height: 10px;
  border-radius: 3px;
  background: #60a5fa;
}

.er-section-range {
  min-width: 170px;
  font: 700 14px var(--font-family);
  color: #fff;
}

.er-section-vol {
  width: auto;
  gap: 4px;
  margin-left: auto;
}

.er-section-db {
  min-width: 80px;
  text-align: center;
  font: 700 15px var(--font-family);
  color: #9cc5f8;
}

.er-placing-box {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border: 1.5px dashed #60a5fa;
  border-radius: 10px;
  color: #dcdce6;
  font-size: 14px;
}

.er-placing-box span {
  flex: 1;
}

.er-issues {
  margin: 0;
  padding-left: 20px;
  color: var(--color-error);
}

.er-footer {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
}

.er-footer-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-left: auto;
}

@media (max-width: 560px) {
  .er-tabs {
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (max-width: 900px) {
  .er-set {
    grid-template-columns: minmax(0, 1fr);
    grid-template-areas: 'sl' 'sc' 'el' 'ec' 'h' 'vl' 'vc' 'vb' 'vn';
  }

  .a-div {
    display: none;
  }

  .a-vl {
    margin-top: 12px;
    padding-top: 18px;
    border-top: 1px solid #2a2a42;
  }
}
</style>
