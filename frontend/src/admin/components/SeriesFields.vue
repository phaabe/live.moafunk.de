<script setup lang="ts">
import { computed } from 'vue';
import { FormInput } from '@shared/components';
import { timeError, type SeriesDraft } from '../seriesDraft';

// Name, Description and rhythm of a Show, for New Show and the Show page.

const draft = defineModel<SeriesDraft>({ required: true });
const props = defineProps<{
  /** New Show puts the rhythm under its own "Rhythm" heading. */
  rhythmTitle?: boolean;
  /** Only one part: `about` (Name, Description) or `rhythm` (Repeats, Day, Time). Default: both. */
  part?: 'about' | 'rhythm';
}>();
const showAbout = computed(() => props.part !== 'rhythm');
const showRhythm = computed(() => props.part !== 'about');

const DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

const fixedRhythm = computed(() => draft.value.repeatRule !== 'none');
const error = computed(() => timeError(draft.value));
</script>

<template>
  <FormInput v-if="showAbout" v-model="draft.name" label="Name" placeholder="Brunchtime" />
  <label v-if="showAbout" class="field">
    <span class="label">Description</span>
    <textarea
      v-model="draft.description"
      class="input"
      rows="3"
      placeholder="Sunday brunch sounds, every week from 12:00."
    ></textarea>
  </label>

  <h2 v-if="rhythmTitle && showRhythm" class="section-title">Rhythm</h2>
  <div v-if="showRhythm" class="rhythm">
    <label class="field">
      <span class="label">Repeats</span>
      <select v-model="draft.repeatRule" class="input">
        <option value="weekly">Every week</option>
        <option value="biweekly">Every 2 weeks</option>
        <option value="none">No fixed rhythm</option>
      </select>
    </label>
    <label v-if="fixedRhythm" class="field">
      <span class="label">Day</span>
      <select v-model.number="draft.weekday" class="input">
        <option v-for="(day, i) in DAYS" :key="day" :value="i + 1">{{ day }}</option>
      </select>
    </label>
    <div v-if="fixedRhythm" class="field">
      <span class="label">Time</span>
      <div class="time-range">
        <input v-model="draft.startTime" type="time" class="input" aria-label="Start time" />
        <span>–</span>
        <input v-model="draft.endTime" type="time" class="input" aria-label="End time" />
      </div>
    </div>
  </div>
  <p v-if="error && showRhythm" class="field-error">{{ error }}</p>
</template>

<style scoped>
.section-title {
  font-size: var(--font-size-lg);
  margin: var(--spacing-md) 0 0;
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
