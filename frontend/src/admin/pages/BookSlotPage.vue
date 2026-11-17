<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import { seriesApi, type Series, type SeriesSlot, type UploadedSet } from '../api';
import { setLength, slotDate, slotTime } from '../episodeRow';
import { currentReplaceTarget, type ReplaceTarget } from '../replaceFile';

// Flow screens `book-slot` (/shows/<slug>/book) and `upload`
// (?mode=prerecorded). Pre-recorded episodes are upload-first: Date, Time
// and "Book slot" wait for the finished upload.

const route = useRoute();
const router = useRouter();

const slug = computed(() => String(route.params.slug));
const isUpload = computed(() => route.query.mode === 'prerecorded');

const series = ref<Series | null>(null);
const myShows = ref<Series[]>([]);
const slots = ref<SeriesSlot[]>([]);
/** "Replace file": the booked episode keeps its date and time. */
const replacing = ref<ReplaceTarget | null>(null);

const date = ref('');
const time = ref('');
const error = ref<string | null>(null);
const booking = ref(false);

const fileInput = ref<HTMLInputElement | null>(null);
const dragging = ref(false);
const uploading = ref(false);
const progress = ref(0);
const uploaded = ref<UploadedSet | null>(null);

/** A Show with no fixed rhythm books any free date and time ahead. */
const freeChoice = computed(() => series.value?.rhythm === null && !replacing.value);
const freeDate = ref('');
const freeStart = ref('');
const freeEnd = ref('');
const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Berlin' }).format(new Date());
const freeTimeError = computed(() =>
  freeStart.value && freeEnd.value && freeEnd.value <= freeStart.value
    ? 'The end time must be after the start time'
    : null
);

const choices = computed<SeriesSlot[]>(() => (replacing.value ? [replacing.value] : slots.value));
const dates = computed(() => [...new Set(choices.value.map((s) => s.date))]);
const times = computed(() =>
  choices.value.filter((s) => s.date === date.value).map((s) => slotTime(s))
);
const selected = computed<SeriesSlot | null>(() => {
  if (freeChoice.value) {
    if (!freeDate.value || !freeStart.value || !freeEnd.value || freeTimeError.value) return null;
    return { date: freeDate.value, start_time: freeStart.value, end_time: freeEnd.value };
  }
  return choices.value.find((s) => s.date === date.value && slotTime(s) === time.value) ?? null;
});

watch(dates, (list) => {
  if (!list.includes(date.value)) date.value = list[0] ?? '';
});
watch(times, (list) => {
  if (!list.includes(time.value)) time.value = list[0] ?? '';
});

/** On the upload screen nothing is picked or booked before the file is up. */
const waitingForFile = computed(() => isUpload.value && !uploaded.value);
const canBook = computed(
  () => !booking.value && !uploading.value && !waitingForFile.value && selected.value !== null
);

async function loadSlots() {
  slots.value = (await seriesApi.slots(slug.value)).slots;
}

async function load() {
  error.value = null;
  uploaded.value = null;
  try {
    const [found, list] = await Promise.all([seriesApi.get(slug.value), seriesApi.list()]);
    if (!found) {
      await router.replace('/shows');
      return;
    }
    series.value = found;
    myShows.value = list.series;
    replacing.value = isUpload.value ? currentReplaceTarget(router) : null;
    if (!replacing.value && found.rhythm) await loadSlots();
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not load the show';
  }
}

watch([slug, isUpload], load, { immediate: true });

function chooseShow(event: Event) {
  router.push(`/shows/${(event.target as HTMLSelectElement).value}/book`);
}

function choosePrerecorded() {
  router.push({ path: `/shows/${slug.value}/book`, query: { mode: 'prerecorded' } });
}

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

async function bookSlot() {
  const slot = selected.value;
  if (!canBook.value || !slot) return;
  booking.value = true;
  error.value = null;
  try {
    if (replacing.value && uploaded.value) {
      await seriesApi.replaceFile(replacing.value.id, uploaded.value.upload_id);
      await router.push(`/shows/${replacing.value.id}`);
      return;
    }
    const episode = await seriesApi.book(slug.value, {
      date: slot.date,
      start_time: slot.start_time,
      end_time: slot.end_time,
      mode: isUpload.value ? 'prerecorded' : 'live',
      upload_id: uploaded.value?.upload_id,
    });
    await router.push(`/shows/${episode.id}`);
  } catch (e) {
    // Usually someone took the slot in the meantime: show why, offer the rest.
    error.value = e instanceof Error ? e.message : 'Could not book the slot';
    if (!replacing.value && !freeChoice.value) await loadSlots().catch(() => undefined);
  } finally {
    booking.value = false;
  }
}
</script>

<template>
  <div v-if="series" class="book-slot-page">
    <template v-if="!isUpload">
      <router-link :to="`/shows/${series.slug}`" class="back-link"
        >&lt; {{ series.name }}</router-link
      >
      <h1 class="page-title">Book Slot</h1>
    </template>
    <template v-else>
      <router-link :to="`/shows/${series.slug}/book`" class="back-link">&lt; Book Slot</router-link>
      <h1 class="page-title">Upload Your Set</h1>
      <p class="intro">Pre-recorded episodes start with the file.</p>
    </template>

    <section class="board-card">
      <div class="board-card-body">
        <template v-if="!isUpload">
          <label class="field">
            <span class="label">Show</span>
            <select class="input" :value="series.slug" @change="chooseShow">
              <option v-for="show in myShows" :key="show.slug" :value="show.slug">
                {{ show.name }}
              </option>
            </select>
          </label>
          <h2 class="section-title">Mode</h2>
          <div class="mode">
            <label><input type="radio" name="mode" value="live" checked /> Live</label>
            <label>
              <input type="radio" name="mode" value="prerecorded" @change="choosePrerecorded" />
              Pre-recorded
            </label>
          </div>
        </template>

        <template v-else>
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

        <template v-if="freeChoice">
          <label class="field">
            <span class="label">Date</span>
            <input
              v-model="freeDate"
              type="date"
              class="input"
              :min="today"
              :disabled="waitingForFile"
            />
          </label>
          <div class="field">
            <span class="label">Time</span>
            <div class="time-range">
              <input
                v-model="freeStart"
                type="time"
                class="input"
                aria-label="Start time"
                :disabled="waitingForFile"
              />
              <span>–</span>
              <input
                v-model="freeEnd"
                type="time"
                class="input"
                aria-label="End time"
                :disabled="waitingForFile"
              />
            </div>
          </div>
          <p v-if="freeTimeError" class="field-error">{{ freeTimeError }}</p>
        </template>
        <template v-else>
          <label class="field">
            <span class="label">Date</span>
            <select v-model="date" class="input" :disabled="waitingForFile || !!replacing">
              <option v-for="d in dates" :key="d" :value="d">{{ slotDate(d) }}</option>
            </select>
          </label>
          <label class="field">
            <span class="label">Time</span>
            <select v-model="time" class="input" :disabled="waitingForFile || !!replacing">
              <option v-for="t in times" :key="t" :value="t">{{ t }}</option>
            </select>
          </label>
        </template>
        <p v-if="!isUpload" class="hint">
          Pre-recorded shows need the file before you pick a time.
        </p>
        <p v-if="error" class="field-error" role="alert">{{ error }}</p>

        <div>
          <BaseButton :disabled="!canBook" :loading="booking" @click="bookSlot"
            >Book slot</BaseButton
          >
        </div>
      </div>
    </section>
  </div>
  <p v-else-if="error" class="field-error" role="alert">{{ error }}</p>
</template>

<style scoped>
.book-slot-page {
  max-width: 560px;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
}

.back-link {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.page-title {
  margin: 0;
}

.intro,
.hint {
  margin: 0;
  color: var(--color-text-muted);
}

.section-title {
  font-size: var(--font-size-lg);
  margin: var(--spacing-sm) 0 0;
}

.mode {
  display: flex;
  gap: var(--spacing-lg);
}

.field {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.label {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.input {
  background-color: var(--color-surface-alt);
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-family: var(--font-family);
  padding: var(--spacing-sm) var(--spacing-md);
}

.input:disabled {
  opacity: 0.5;
}

.drop-zone {
  border: 1px dashed var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--spacing-xl);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--spacing-sm);
  cursor: pointer;
  color: var(--color-text-muted);
  text-align: center;
}

.drop-zone.dragging {
  border-color: var(--color-primary);
}

.file-input {
  display: none;
}

.uploaded {
  margin: 0;
  color: var(--color-success, var(--color-text));
}

.field-error {
  color: var(--color-error);
  font-size: var(--font-size-sm);
  margin: 0;
}

.time-range {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
}
</style>
