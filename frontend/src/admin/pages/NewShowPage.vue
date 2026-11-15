<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { BaseButton, FormInput } from '@shared/components';
import { seriesApi, type RepeatRule } from '../api';

// Flow screen `new-show`: a host creates a Show (series) once, then books
// its episodes on the Show page.

const router = useRouter();

const name = ref('');
const description = ref('');
// Defaults as on the flow screen: "Every week", "Sunday".
const repeatRule = ref<RepeatRule>('weekly');
const weekday = ref(7);
const startTime = ref('');
const endTime = ref('');
const cover = ref<File | null>(null);
const coverPreview = ref<string | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const dragging = ref(false);
const saving = ref(false);
const error = ref<string | null>(null);

const DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

const timeError = computed(() =>
  startTime.value && endTime.value && endTime.value <= startTime.value
    ? 'The end time must be after the start time'
    : null
);

const canSubmit = computed(
  () =>
    name.value.trim() !== '' &&
    weekday.value >= 1 &&
    startTime.value !== '' &&
    endTime.value !== '' &&
    !timeError.value &&
    !saving.value
);

function setCover(file: File | null) {
  if (coverPreview.value) URL.revokeObjectURL(coverPreview.value);
  cover.value = file && file.type.startsWith('image/') ? file : null;
  coverPreview.value = cover.value ? URL.createObjectURL(cover.value) : null;
}

function onPick(event: Event) {
  setCover((event.target as HTMLInputElement).files?.[0] ?? null);
}

function onDrop(event: DragEvent) {
  dragging.value = false;
  setCover(event.dataTransfer?.files?.[0] ?? null);
}

async function createShow() {
  if (!canSubmit.value) return;
  saving.value = true;
  error.value = null;
  try {
    const created = await seriesApi.create({
      name: name.value.trim(),
      description: description.value.trim(),
      repeat_rule: repeatRule.value,
      weekday: weekday.value,
      start_time: startTime.value,
      end_time: endTime.value,
      cover: cover.value,
    });
    await router.push(`/shows/${created.slug}`);
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not create the show';
  } finally {
    saving.value = false;
  }
}

onUnmounted(() => setCover(null));
</script>

<template>
  <div class="new-show-page">
    <router-link to="/shows" class="back-link">&lt; My Shows</router-link>
    <h1 class="page-title">New Show</h1>
    <p class="intro">A show is your series. You book its episodes after this.</p>

    <div
      class="cover-drop"
      :class="{ dragging }"
      role="button"
      tabindex="0"
      @click="fileInput?.click()"
      @keydown.enter.prevent="fileInput?.click()"
      @dragover.prevent="dragging = true"
      @dragleave="dragging = false"
      @drop.prevent="onDrop"
    >
      <img v-if="coverPreview" :src="coverPreview" alt="Show cover" />
      <span v-else>Show cover · drop an image</span>
    </div>
    <!-- Outside the drop zone, so its click does not bubble back into it. -->
    <input ref="fileInput" type="file" accept="image/*" class="file-input" @change="onPick" />

    <FormInput v-model="name" label="Name" placeholder="Brunchtime" />
    <label class="field">
      <span class="label">Description</span>
      <textarea
        v-model="description"
        class="input"
        rows="3"
        placeholder="Sunday brunch sounds, every week from 12:00."
      ></textarea>
    </label>

    <h2 class="section-title">Rhythm</h2>
    <div class="rhythm">
      <label class="field">
        <span class="label">Repeats</span>
        <select v-model="repeatRule" class="input">
          <option value="weekly">Every week</option>
          <option value="biweekly">Every 2 weeks</option>
        </select>
      </label>
      <label class="field">
        <span class="label">Day</span>
        <select v-model.number="weekday" class="input">
          <option v-for="(day, i) in DAYS" :key="day" :value="i + 1">{{ day }}</option>
        </select>
      </label>
      <div class="field">
        <span class="label">Time</span>
        <div class="time-range">
          <input v-model="startTime" type="time" class="input" aria-label="Start time" />
          <span>–</span>
          <input v-model="endTime" type="time" class="input" aria-label="End time" />
        </div>
      </div>
    </div>
    <p v-if="timeError" class="field-error">{{ timeError }}</p>
    <p v-if="error" class="field-error" role="alert">{{ error }}</p>

    <BaseButton :disabled="!canSubmit" :loading="saving" @click="createShow">
      Create show
    </BaseButton>
  </div>
</template>

<style scoped>
.new-show-page {
  max-width: 560px;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.back-link {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.page-title {
  margin: 0;
}

.intro {
  margin: 0;
  color: var(--color-text-muted);
}

.section-title {
  font-size: var(--font-size-lg);
  margin: var(--spacing-md) 0 0;
}

.cover-drop {
  width: 220px;
  height: 220px;
  border: 1px dashed var(--color-border);
  border-radius: var(--radius-md);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  cursor: pointer;
  color: var(--color-text-muted);
  text-align: center;
}

.cover-drop.dragging {
  border-color: var(--color-primary);
}

.cover-drop img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.file-input {
  display: none;
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
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-family: var(--font-family);
  padding: var(--spacing-sm) var(--spacing-md);
}

.input:focus {
  outline: none;
  border-color: var(--color-primary);
}

.rhythm {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-md);
}

.time-range {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
}

.field-error {
  color: var(--color-error);
  font-size: var(--font-size-sm);
  margin: 0;
}
</style>
