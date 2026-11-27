<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import { seriesApi, type BookedTime, type Series, type UploadedSet } from '../api';
import { episodeDay, setLength } from '../episodeRow';
import { currentReplaceTarget, type ReplaceTarget } from '../replaceFile';
import { initials, rhythmText } from '../components/show-page/showPage';
import {
  addDays,
  berlinNow,
  bookedWindows,
  DAYS_LONG,
  DAYS_SHORT,
  dayNumber,
  DAY_MINUTES,
  hhmm,
  LENGTHS,
  lengthBetween,
  lengthText,
  MAX_LENGTH,
  MIN_LENGTH,
  nextFullHour,
  parseHhmm,
  problemOf,
  takeOverOf,
  taken,
  windowOf,
} from '../bookSlot';

// Flow screens `book-slot` (/shows/<slug>/book), `book-now` (the Now card)
// and `upload` (?mode=prerecorded: the same page with Pre-recorded picked),
// flow rev 41, #1118. Pre-recorded is upload-first: picking and Book episode
// wait for the finished upload.

const route = useRoute();
const router = useRouter();

const slug = computed(() => String(route.params.slug));
const isUpload = computed(() => route.query.mode === 'prerecorded');
const live = computed(() => !isUpload.value);

const series = ref<Series | null>(null);
/** "Replace file": the booked episode keeps its day and time, only the file changes. */
const replacing = ref<ReplaceTarget | null>(null);
const booked = ref<BookedTime[]>([]);
const error = ref<string | null>(null);
const booking = ref(false);

/** Berlin wall clock; the Now card and "is it over" follow it. */
const clock = ref(berlinNow());
let tick: ReturnType<typeof setInterval> | undefined;
onMounted(() => {
  tick = setInterval(() => (clock.value = berlinNow()), 15_000);
});
onUnmounted(() => clearInterval(tick));
const today = computed(() => clock.value.date);
const nowAt = computed(() => dayNumber(today.value) * DAY_MINUTES + clock.value.minute);

/** The pick: a day and a start minute, or Now; and a length in minutes. */
const pickedNow = ref(false);
const date = ref(clock.value.date);
const start = ref(0);
const length = ref(120);
const moreDays = ref(false);
const editing = ref(false);

const fileInput = ref<HTMLInputElement | null>(null);
const dragging = ref(false);
const uploading = ref(false);
const progress = ref(0);
const uploaded = ref<UploadedSet | null>(null);

const isNow = computed(() => live.value && pickedNow.value);
const waitingForFile = computed(() => isUpload.value && !uploaded.value);
/** Pickers are off while the set uploads first, and for Replace file. */
const locked = computed(() => waitingForFile.value || !!replacing.value);

const startDate = computed(() => (isNow.value ? today.value : date.value));
const startMinute = computed(() => (isNow.value ? clock.value.minute : start.value));
const span = computed(() => windowOf(startDate.value, startMinute.value, length.value));
const endDate = computed(() =>
  addDays(startDate.value, Math.floor((startMinute.value + length.value) / DAY_MINUTES))
);
const crosses = computed(() => endDate.value !== startDate.value);
const takenWindows = computed(() => bookedWindows(booked.value.filter((b) => !b.take_over)));
/** Idle shows (booked, not on air for 10 min) don't block: Book asks first. */
const idleWindows = computed(() => bookedWindows(booked.value.filter((b) => b.take_over)));

const problem = computed(() =>
  replacing.value
    ? null
    : problemOf(span.value, nowAt.value, isNow.value, takenWindows.value, episodeDay)
);
const takeOver = computed(() =>
  replacing.value || problem.value ? null : takeOverOf(span.value, idleWindows.value)
);
/** Book asked "Book over it?"; a pick off the idle show drops the question. */
const confirming = ref(false);
watch(
  () => !!takeOver.value,
  (on) => {
    if (!on) confirming.value = false;
  }
);
const canBook = computed(
  () => !booking.value && !uploading.value && !waitingForFile.value && !problem.value
);

// ─── Pickers ────────────────────────────────────────────────────────────────

const days = computed(() =>
  Array.from({ length: moreDays.value ? DAYS_LONG : DAYS_SHORT }, (_, i) => {
    const d = addDays(today.value, i);
    const at = new Date(`${d}T12:00:00Z`);
    return {
      date: d,
      top: i === 0 ? 'Today' : i === 1 ? 'Tmrw' : episodeDay(d).slice(0, 2),
      num: String(at.getUTCDate()),
      month: at.toLocaleDateString('en-GB', { month: 'short', timeZone: 'UTC' }),
      label: i === 0 ? `Today · ${episodeDay(d)}` : episodeDay(d),
      on: !isNow.value && date.value === d,
      busy: booked.value.some((b) => b.date === d),
    };
  })
);

/** A minute of the picked day that is over or inside another show. */
function blocked(minute: number): boolean {
  if (date.value === today.value && minute <= clock.value.minute) return true;
  return taken(takenWindows.value, dayNumber(date.value) * DAY_MINUTES + minute);
}
const QUARTERS = [0, 15, 30, 45];

const hours = computed(() =>
  Array.from({ length: 24 }, (_, h) => {
    const free = QUARTERS.map((m) => h * 60 + m).filter((t) => !blocked(t));
    const over = date.value === today.value && h * 60 + 45 <= clock.value.minute;
    return {
      h,
      label: String(h).padStart(2, '0'),
      on: Math.floor(start.value / 60) === h,
      free,
      // Crossed out: another show has the whole hour.
      taken: !over && !free.length,
    };
  })
);
const minutes = computed(() =>
  QUARTERS.map((m) => ({
    m,
    label: `:${String(m).padStart(2, '0')}`,
    on: start.value % 60 === m,
    disabled: locked.value || blocked(Math.floor(start.value / 60) * 60 + m),
  }))
);

function pickNow() {
  pickedNow.value = true;
}
function pickDay(d: string) {
  pickedNow.value = false;
  date.value = d;
  // Today: a start that is over moves to the next full hour.
  if (d === today.value && start.value <= clock.value.minute) {
    const next = nextFullHour(clock.value);
    date.value = next.date;
    start.value = next.minute;
  }
}
function pickHour(h: (typeof hours.value)[number]) {
  const minute = start.value % 60;
  start.value = blocked(h.h * 60 + minute) ? h.free[0] : h.h * 60 + minute;
}
function pickMinute(m: number) {
  start.value = Math.floor(start.value / 60) * 60 + m;
}
function step(by: number) {
  length.value = Math.min(MAX_LENGTH, Math.max(MIN_LENGTH, length.value + by));
}

// ─── Overview ───────────────────────────────────────────────────────────────

const startText = computed(
  () => `${isNow.value ? 'Now · ' : ''}${episodeDay(startDate.value)} · ${hhmm(startMinute.value)}`
);
const endText = computed(
  () => `${episodeDay(endDate.value)} · ${hhmm(startMinute.value + length.value)}`
);
/** Where 00:00 falls on the time bar, in percent. */
const midnight = computed(() => ((DAY_MINUTES - startMinute.value) / length.value) * 100);
const rhythmLine = computed(() =>
  series.value?.rhythm ? rhythmText(series.value) : 'No fixed rhythm · pick any free time'
);

/** Edit manually: typed date, start and end. An end before the start is the next day. */
function typeDate(event: Event) {
  const value = (event.target as HTMLInputElement).value;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return;
  if (isNow.value) start.value = clock.value.minute;
  pickedNow.value = false;
  date.value = value;
}
function typeStart(event: Event) {
  const minute = parseHhmm((event.target as HTMLInputElement).value);
  if (minute === null) return;
  if (isNow.value) date.value = today.value;
  pickedNow.value = false;
  start.value = minute;
}
function typeEnd(event: Event) {
  const minute = parseHhmm((event.target as HTMLInputElement).value);
  if (minute !== null) length.value = lengthBetween(startMinute.value, minute);
}

// ─── Load ───────────────────────────────────────────────────────────────────

async function loadBooked() {
  // Two weeks of day cards; a show on the last day may pass midnight.
  booked.value = (await seriesApi.bookedTimes(today.value, addDays(today.value, DAYS_LONG))).times;
}

/** The first pick: a Replace file episode, else the free date from the Show
 * page (?date=), else the next free rhythm date, else today; a rhythm Show
 * starts at its rhythm time and length, one without at the next full hour, 2 h. */
async function preset(found: Series) {
  const target = replacing.value;
  if (target) {
    date.value = target.date;
    start.value = parseHhmm(target.start_time) ?? 0;
    length.value = lengthBetween(start.value, parseHhmm(target.end_time) ?? start.value);
  } else {
    await presetPick(found);
  }
  moreDays.value = dayNumber(date.value) - dayNumber(today.value) >= DAYS_SHORT;
}

async function presetPick(found: Series) {
  const q = route.query.date;
  const asked = typeof q === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(q) ? q : null;
  const next = nextFullHour(clock.value);
  const rhythm = found.rhythm;
  const rStart = rhythm ? parseHhmm(rhythm.start_time) : null;
  const rEnd = rhythm ? parseHhmm(rhythm.end_time) : null;
  if (rStart !== null && rEnd !== null) {
    const firstFree = asked ? null : (await seriesApi.slots(found.slug)).slots[0]?.date;
    date.value = asked ?? firstFree ?? next.date;
    start.value = rStart;
    length.value = lengthBetween(rStart, rEnd);
  } else {
    date.value = asked ?? next.date;
    start.value = next.minute;
    length.value = 120;
  }
}

async function load() {
  error.value = null;
  uploaded.value = null;
  pickedNow.value = false;
  editing.value = false;
  try {
    const found = await seriesApi.get(slug.value);
    // Another host's Show is read-only: its page, not Book Slot.
    if (!found || found.can_edit === false) {
      await router.replace(found ? `/shows/${found.slug}` : '/shows');
      return;
    }
    replacing.value = isUpload.value ? currentReplaceTarget(router) : null;
    await Promise.all([loadBooked(), preset(found)]);
    // Shown once the first pick is set.
    series.value = found;
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not load the show';
  }
}

watch(slug, load, { immediate: true });
// Live ↔ Pre-recorded keeps the pick; Replace file brings its own episode.
watch(isUpload, (upload) => {
  uploaded.value = null;
  replacing.value = upload ? currentReplaceTarget(router) : null;
  if (replacing.value && series.value) void preset(series.value);
});

function chooseMode(prerecorded: boolean) {
  if (prerecorded === isUpload.value) return;
  const query = { ...route.query };
  if (prerecorded) query.mode = 'prerecorded';
  else delete query.mode;
  router.push({ path: `/shows/${slug.value}/book`, query });
}

// ─── Upload ─────────────────────────────────────────────────────────────────

async function uploadFile(file: File | undefined) {
  if (!file) return;
  if (!/\.(mp3|wav)$/i.test(file.name)) {
    error.value = 'Upload an MP3 or WAV file';
    return;
  }
  error.value = null;
  uploaded.value = null;
  uploading.value = true;
  progress.value = 0;
  try {
    uploaded.value = await seriesApi.uploadSet(slug.value, file, (p) => (progress.value = p));
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'The upload failed';
  } finally {
    uploading.value = false;
  }
}
function onPick(event: Event) {
  uploadFile((event.target as HTMLInputElement).files?.[0]);
}
function onDrop(event: DragEvent) {
  dragging.value = false;
  uploadFile(event.dataTransfer?.files?.[0]);
}

// ─── Book ───────────────────────────────────────────────────────────────────

async function book() {
  if (!canBook.value) return;
  if (takeOver.value && !confirming.value) {
    confirming.value = true;
    return;
  }
  booking.value = true;
  error.value = null;
  try {
    if (replacing.value && uploaded.value) {
      await seriesApi.replaceFile(replacing.value.id, uploaded.value.upload_id);
      await router.push(`/shows/${replacing.value.id}`);
      return;
    }
    // Now books the current minute; the server takes a start a few minutes back.
    const at = isNow.value ? berlinNow() : { date: date.value, minute: start.value };
    const episode = await seriesApi.book(slug.value, {
      date: at.date,
      start_time: hhmm(at.minute),
      end_time: hhmm(at.minute + length.value),
      mode: isUpload.value ? 'prerecorded' : 'live',
      upload_id: uploaded.value?.upload_id,
      ...(takeOver.value ? { take_over: true } : {}),
    });
    await router.push(
      isNow.value
        ? { path: `/shows/${episode.id}`, state: { episodeSocial: episode.id } }
        : `/shows/${episode.id}`
    );
  } catch (e) {
    // Usually another show took the time meanwhile: say why, mark it as taken.
    error.value = e instanceof Error ? e.message : 'Could not book the episode';
    await loadBooked().catch(() => undefined);
  } finally {
    booking.value = false;
    confirming.value = false;
  }
}
</script>

<template>
  <div v-if="series" class="book-page">
    <router-link :to="`/shows/${series.slug}`" class="back-link"
      >&lt; {{ series.name }}</router-link
    >

    <header class="book-head">
      <div class="cover book-cover">
        <img
          v-if="series.cover_url"
          :src="series.cover_url"
          :srcset="series.cover_srcset ?? undefined"
          sizes="96px"
          :alt="`Cover of ${series.name}`"
        />
        <span v-else class="cover-initials" aria-hidden="true">{{ initials(series.name) }}</span>
      </div>
      <div class="head-text">
        <p class="pill eyebrow">Book an episode</p>
        <h1 class="page-title">{{ series.name }}</h1>
        <p class="rhythm">{{ rhythmLine }}</p>
      </div>
    </header>

    <div class="book-grid">
      <section class="board-card pickers" aria-label="Pick a time">
        <div class="modes" role="group" aria-label="Mode">
          <button
            type="button"
            class="mode live"
            :class="{ on: live }"
            aria-label="Live"
            aria-describedby="mode-live-hint"
            :aria-pressed="live"
            :disabled="!!replacing"
            @click="chooseMode(false)"
          >
            <svg class="mode-icon" width="26" height="26" viewBox="0 0 24 24" aria-hidden="true">
              <circle cx="12" cy="12" r="2" />
              <path
                d="M16.2 7.8a6 6 0 0 1 0 8.4M7.8 16.2a6 6 0 0 1 0-8.4M19.1 4.9a10 10 0 0 1 0 14.2M4.9 19.1a10 10 0 0 1 0-14.2"
              />
            </svg>
            <span><b>Live</b><small id="mode-live-hint">You stream from the Studio</small></span>
          </button>
          <button
            type="button"
            class="mode pre"
            :class="{ on: !live }"
            aria-label="Pre-recorded"
            aria-describedby="mode-pre-hint"
            :aria-pressed="!live"
            :disabled="!!replacing"
            @click="chooseMode(true)"
          >
            <svg class="mode-icon" width="26" height="26" viewBox="0 0 24 24" aria-hidden="true">
              <rect x="2" y="6" width="20" height="13" rx="2" />
              <circle cx="8" cy="12.5" r="2" />
              <circle cx="16" cy="12.5" r="2" />
              <path d="M8 14.5h8" />
            </svg>
            <span><b>Pre-recorded</b><small id="mode-pre-hint">Upload your set first</small></span>
          </button>
        </div>

        <template v-if="isUpload">
          <div
            class="drop-zone"
            :class="{ dragging }"
            role="button"
            tabindex="0"
            @click="fileInput?.click()"
            @keydown.enter.prevent="fileInput?.click()"
            @dragover.prevent="dragging = true"
            @dragleave="dragging = false"
            @drop.prevent="onDrop"
          >
            <span>Drop your set here · MP3 or WAV</span>
            <progress v-if="uploading" :value="progress" max="100"></progress>
          </div>
          <input
            ref="fileInput"
            type="file"
            accept=".mp3,.wav,audio/mpeg,audio/wav"
            class="file-input"
            @change="onPick"
          />
          <p v-if="uploaded" class="uploaded">
            {{ uploaded.filename }} · {{ setLength(uploaded.duration) }} · uploaded
          </p>
        </template>

        <section class="part">
          <h2 class="part-title">Day</h2>
          <div class="days">
            <button
              v-if="live"
              type="button"
              class="day now"
              :class="{ on: isNow }"
              :aria-label="`Now · ${hhmm(clock.minute)}`"
              :aria-pressed="isNow"
              :disabled="locked"
              @click="pickNow"
            >
              <span>Now</span>
              <span class="dot" aria-hidden="true"></span>
              <span class="mono">{{ hhmm(clock.minute) }}</span>
            </button>
            <button
              v-for="d in days"
              :key="d.date"
              type="button"
              class="day"
              :class="{ on: d.on, busy: d.busy }"
              :aria-label="d.label"
              :aria-describedby="d.busy ? 'day-busy-hint' : undefined"
              :aria-pressed="d.on"
              :disabled="locked"
              @click="pickDay(d.date)"
            >
              <span>{{ d.top }}</span>
              <b>{{ d.num }}</b>
              <span class="month">{{ d.month }}</span>
            </button>
            <button type="button" class="day more" :disabled="locked" @click="moreDays = !moreDays">
              <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
                <rect x="3" y="5" width="18" height="16" rx="2" />
                <path d="M3 10h18M8 3v4M16 3v4" />
              </svg>
              {{ moreDays ? 'Fewer days' : 'More days' }}
            </button>
          </div>
          <span id="day-busy-hint" class="sr-only">Another show is booked that day.</span>
        </section>

        <p v-if="isNow" class="notice now-line">
          <span class="dot" aria-hidden="true"></span>Starts right away at {{ hhmm(clock.minute) }}.
        </p>
        <section v-else class="part">
          <h2 class="part-title">Start · {{ episodeDay(date || today) }}</h2>
          <div class="hours">
            <button
              v-for="h in hours"
              :key="h.h"
              type="button"
              class="hour"
              :class="{ on: h.on, taken: h.taken }"
              :aria-pressed="h.on"
              :disabled="locked || !h.free.length"
              @click="pickHour(h)"
            >
              {{ h.label }}
            </button>
          </div>
          <div class="minutes">
            <span class="hint">Minutes</span>
            <div class="seg" role="group" aria-label="Minutes">
              <button
                v-for="m in minutes"
                :key="m.m"
                type="button"
                :class="{ on: m.on }"
                :aria-pressed="m.on"
                :disabled="m.disabled"
                @click="pickMinute(m.m)"
              >
                {{ m.label }}
              </button>
            </div>
          </div>
        </section>

        <section class="part">
          <h2 class="part-title">Length</h2>
          <div class="lengths">
            <button
              v-for="l in LENGTHS"
              :key="l"
              type="button"
              class="chip"
              :class="{ on: length === l }"
              :aria-pressed="length === l"
              :disabled="locked"
              @click="length = l"
            >
              {{ lengthText(l) }}
            </button>
            <span class="gap" aria-hidden="true"></span>
            <button type="button" class="chip step" :disabled="locked" @click="step(-15)">
              −15 min
            </button>
            <button type="button" class="chip step" :disabled="locked" @click="step(15)">
              +15 min
            </button>
          </div>
        </section>
      </section>

      <aside class="board-card overview" aria-label="Overview">
        <div class="overview-head">
          <h2 class="part-title">Overview</h2>
          <button
            type="button"
            class="edit-button"
            :class="{ on: editing }"
            :aria-pressed="editing"
            :disabled="locked"
            @click="editing = !editing"
          >
            {{ editing ? 'Done' : 'Edit manually' }}
          </button>
        </div>
        <p class="pill mode-pill" :class="live ? 'live' : 'pre'">
          {{ live ? 'Live' : 'Pre-recorded' }} · {{ series.name }}
        </p>

        <div v-if="editing" class="manual">
          <label class="field">
            <span class="field-name">Date</span>
            <input type="date" class="input" :min="today" :value="startDate" @change="typeDate" />
          </label>
          <div class="manual-times">
            <label class="field">
              <span class="field-name">Start</span>
              <input type="time" class="input" :value="hhmm(startMinute)" @change="typeStart" />
            </label>
            <label class="field">
              <span class="field-name">End</span>
              <input
                type="time"
                class="input"
                :value="hhmm(startMinute + length)"
                @change="typeEnd"
              />
            </label>
          </div>
          <p class="hint">An end before the start means the next day.</p>
        </div>
        <dl v-else class="rows">
          <div>
            <dt>Start</dt>
            <dd>{{ startText }}</dd>
          </div>
          <div>
            <dt>End</dt>
            <dd>
              {{ endText
              }}<template v-if="crosses"
                ><span class="sr-only"> · </span><span class="badge">+1 day</span></template
              >
            </dd>
          </div>
          <div>
            <dt>Length</dt>
            <dd>{{ lengthText(length) }}</dd>
          </div>
        </dl>

        <div class="bar-wrap" aria-hidden="true">
          <div class="bar" :class="{ bad: !!problem }">
            <span v-if="crosses" class="midnight" :style="{ left: `${midnight}%` }"
              ><span class="midnight-label">00:00</span></span
            >
          </div>
          <div class="bar-ends">
            <span>{{ hhmm(startMinute) }}</span>
            <span>{{ hhmm(startMinute + length) }}</span>
          </div>
        </div>

        <p v-if="problem" class="field-error" role="alert">{{ problem }}</p>
        <p v-if="error" class="field-error" role="alert">{{ error }}</p>
        <div v-if="confirming && takeOver" class="take-over">
          <p class="take-over-question" role="alert">{{ takeOver }}</p>
          <div class="take-over-actions">
            <BaseButton variant="secondary" :disabled="booking" @click="confirming = false"
              >Cancel</BaseButton
            >
            <BaseButton :disabled="!canBook" :loading="booking" @click="book"
              >Book over it</BaseButton
            >
          </div>
        </div>
        <BaseButton
          v-else
          class="book-button"
          :disabled="!canBook"
          :loading="booking"
          @click="book"
          >{{ isNow ? 'Start show now' : 'Book episode' }}</BaseButton
        >
      </aside>
    </div>
  </div>
  <p v-else-if="error" class="field-error" role="alert">{{ error }}</p>
</template>

<style scoped>
.book-page {
  --live: #ff5a5a;
  --pre: #7ea2ff;
  max-width: 1120px;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
}

.back-link {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.book-head {
  display: flex;
  align-items: center;
  gap: var(--spacing-lg);
}

.book-cover {
  width: 96px;
  height: 96px;
  border-radius: var(--radius-lg);
  background: var(--color-surface-alt);
}

.book-cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.cover-initials {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  color: var(--color-primary);
  font: 700 2rem/1 var(--font-family);
}

.head-text {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
  min-width: 0;
}

.eyebrow {
  margin: 0;
  font-size: var(--font-size-xs);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}

.page-title,
.rhythm {
  margin: 0;
}

.rhythm {
  color: var(--color-text-muted);
}

.book-grid {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-lg);
  align-items: flex-start;
}

.pickers {
  flex: 999 1 520px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xl);
}

.overview {
  flex: 1 1 300px;
  max-width: 420px;
  position: sticky;
  top: var(--spacing-lg);
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.pickers,
.overview {
  padding: var(--spacing-lg);
}

.part {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
}

.part-title {
  margin: 0;
  font: 600 var(--font-size-xs) var(--font-ui);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}

button {
  font-family: var(--font-ui);
}

button:disabled {
  cursor: not-allowed;
}

/* Mode: Live red, Pre-recorded blue. */
.modes {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
}

.mode {
  flex: 1 1 220px;
  display: flex;
  align-items: center;
  gap: var(--spacing-md);
  min-height: 64px;
  padding: var(--spacing-sm) var(--spacing-md);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-surface-alt);
  color: var(--color-text-muted);
  text-align: left;
  cursor: pointer;
}

.mode b {
  display: block;
  color: var(--color-text);
  font-size: var(--font-size-md);
}

.mode small {
  display: block;
  font-size: var(--font-size-xs);
}

.mode-icon {
  flex: none;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
}

.mode.live.on {
  border-color: var(--live);
  background: rgb(255 90 90 / 14%);
  box-shadow: inset 0 0 0 1px var(--live);
  color: #ffd0d0;
}

.mode.live.on .mode-icon {
  color: var(--live);
}

.mode.pre.on {
  border-color: var(--pre);
  background: rgb(126 162 255 / 14%);
  box-shadow: inset 0 0 0 1px var(--pre);
  color: #d6e1ff;
}

.mode.pre.on .mode-icon {
  color: var(--pre);
}

.drop-zone {
  border: 1.5px dashed var(--pre);
  border-radius: var(--radius-lg);
  background: rgb(126 162 255 / 5%);
  padding: var(--spacing-lg);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--spacing-sm);
  cursor: pointer;
  color: var(--color-text-muted);
  text-align: center;
}

.drop-zone.dragging {
  background: rgb(126 162 255 / 14%);
}

.file-input {
  display: none;
}

.uploaded {
  margin: 0;
  color: var(--color-success);
}

/* Day cards scroll sideways on a phone. */
.days {
  display: flex;
  gap: var(--spacing-sm);
  overflow-x: auto;
  padding-bottom: var(--spacing-xs);
}

.day {
  flex: 0 0 78px;
  min-height: 96px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  position: relative;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-surface-alt);
  color: var(--color-text);
  font-size: var(--font-size-xs);
  cursor: pointer;
}

.day b {
  font: 700 1.6rem/1.1 var(--font-family);
}

.day .month {
  opacity: 0.7;
}

.day.busy::after {
  content: '';
  position: absolute;
  top: 8px;
  right: 8px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--color-text-muted);
}

.day.now {
  border-color: rgb(255 90 90 / 55%);
  background: rgb(255 90 90 / 8%);
  color: #ffc2c2;
  font-weight: 700;
}

.day.more {
  border-style: dashed;
  color: var(--color-text-muted);
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
}

.day.on,
.chip.on,
.hour.on {
  border-color: var(--color-primary);
  background: rgb(255 236 68 / 10%);
  color: var(--color-primary);
}

.day:disabled,
.chip:disabled,
.hour:disabled,
.seg button:disabled {
  opacity: 0.35;
}

.dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  margin: 6px 0;
  border-radius: 50%;
  background: var(--live);
  animation: pulse 1.4s infinite;
}

@keyframes pulse {
  from {
    box-shadow: 0 0 0 0 rgb(255 90 90 / 60%);
  }
  to {
    box-shadow: 0 0 0 8px rgb(255 90 90 / 0%);
  }
}

@media (prefers-reduced-motion: reduce) {
  .dot {
    animation: none;
  }
}

.now-line {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  margin: 0;
  padding: var(--spacing-sm) var(--spacing-md);
  border: 1px solid rgb(255 90 90 / 35%);
  border-radius: var(--radius-lg);
  background: rgb(255 90 90 / 8%);
}

.now-line .dot {
  margin: 0;
}

.hours {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: var(--spacing-sm);
}

.hour {
  min-height: 48px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface-alt);
  color: var(--color-text);
  font: 700 1.05rem var(--font-family);
  cursor: pointer;
}

.hour.taken {
  text-decoration: line-through;
}

.minutes {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--spacing-md);
}

.hint {
  margin: 0;
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.seg {
  display: inline-flex;
  gap: 4px;
  padding: 4px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface-alt);
}

.seg button {
  min-height: 44px;
  padding: 0 var(--spacing-md);
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text-muted);
  font: 700 1rem var(--font-family);
  cursor: pointer;
}

.seg button.on {
  background: rgb(255 236 68 / 14%);
  color: var(--color-primary);
}

.lengths {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--spacing-sm);
}

.chip {
  min-height: 48px;
  min-width: 64px;
  padding: 0 var(--spacing-md);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface-alt);
  color: var(--color-text);
  font-weight: 600;
  cursor: pointer;
}

.gap {
  width: var(--spacing-sm);
}

.overview-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-sm);
}

.edit-button {
  min-height: 40px;
  padding: 0 var(--spacing-sm);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text-muted);
  font-size: var(--font-size-xs);
  font-weight: 600;
  cursor: pointer;
}

.edit-button.on {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.mode-pill {
  margin: 0;
  padding: var(--spacing-xs) var(--spacing-sm);
  border-radius: var(--radius-md);
  font: 700 var(--font-size-sm) var(--font-family);
}

.mode-pill.live {
  border: 1px solid rgb(255 90 90 / 45%);
  background: rgb(255 90 90 / 10%);
  color: #ff8a8a;
}

.mode-pill.pre {
  border: 1px solid rgb(126 162 255 / 45%);
  background: rgb(126 162 255 / 10%);
  color: #9fbaff;
}

.rows {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
}

.rows > div {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr);
  align-items: baseline;
  gap: var(--spacing-md);
}

.rows dt {
  font-size: var(--font-size-xs);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}

.rows dd {
  margin: 0;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--spacing-xs) var(--spacing-sm);
  font: 700 var(--font-size-md) var(--font-family);
}

.badge {
  padding: 2px 8px;
  border: 1px solid rgb(255 236 68 / 45%);
  border-radius: 999px;
  background: rgb(255 236 68 / 12%);
  color: var(--color-primary);
  font: 700 var(--font-size-xs) var(--font-ui);
}

.manual {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
}

.manual-times {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--spacing-sm);
}

.field {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.field-name {
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
}

.input {
  min-height: 44px;
  padding: 0 var(--spacing-sm);
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-md);
  background: var(--color-surface-alt);
  color: var(--color-text);
  font: 600 1rem var(--font-family);
  color-scheme: dark;
}

.bar-wrap {
  padding-top: 18px;
}

.bar {
  position: relative;
  height: 14px;
  border-radius: 7px;
  background: var(--color-primary);
}

.bar.bad {
  background: #ff8a8a;
}

.midnight {
  position: absolute;
  top: -4px;
  bottom: -4px;
  width: 3px;
  margin-left: -1px;
  border-radius: 2px;
  background: #000;
}

.midnight-label {
  position: absolute;
  bottom: calc(100% + 4px);
  left: 50%;
  transform: translateX(-50%);
  font: 11px var(--font-family);
  color: var(--color-text);
}

.bar-ends {
  display: flex;
  justify-content: space-between;
  margin-top: var(--spacing-xs);
  font: 11px var(--font-family);
  color: var(--color-text-muted);
}

.field-error {
  margin: 0;
  color: var(--color-error);
  font-size: var(--font-size-sm);
}

.book-button {
  width: 100%;
}

.take-over {
  display: grid;
  gap: var(--spacing-sm);
}

.take-over-question {
  margin: 0;
  font-size: var(--font-size-sm);
}

.take-over-actions {
  display: flex;
  gap: var(--spacing-sm);
}

.take-over-actions > * {
  flex: 1;
}

.mono {
  font-family: var(--font-family);
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

@media (max-width: 640px) {
  .hours {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }

  .overview {
    position: static;
    max-width: none;
  }
}
</style>
