<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { useShowWizard } from '../../composables';
import { showsApi, type Show } from '../../api';
import MonthCalendar from '../MonthCalendar.vue';

const wizard = useShowWizard();
const { startDateTime, endDateTime } = wizard;

const shows = ref<Show[]>([]);

onMounted(async () => {
  try {
    const res = await showsApi.overview();
    shows.value = res.shows as Show[];
  } catch {
    shows.value = [];
  }
});

function pad(n: number): string {
  return String(n).padStart(2, '0');
}

const selectedDateStr = computed(() => {
  const d = startDateTime.value;
  if (!d) return null;
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
});

const selectedDateLabel = computed(() => {
  if (!startDateTime.value) return null;
  return startDateTime.value.toLocaleDateString('en-US', {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
    year: 'numeric',
  });
});

function onDayClick(dateStr: string) {
  const [y, m, d] = dateStr.split('-').map(Number);
  const s = startDateTime.value;
  const e = endDateTime.value;
  startDateTime.value = new Date(y, m - 1, d, s ? s.getHours() : 20, s ? s.getMinutes() : 0);
  endDateTime.value = new Date(y, m - 1, d, e ? e.getHours() : 22, e ? e.getMinutes() : 0);
}

function timeModel(target: 'start' | 'end') {
  return computed<string>({
    get() {
      const d = target === 'start' ? startDateTime.value : endDateTime.value;
      return d ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : '';
    },
    set(value: string) {
      if (!value || !selectedDateStr.value) return;
      const [hh, mm] = value.split(':').map(Number);
      const [y, m, d] = selectedDateStr.value.split('-').map(Number);
      const next = new Date(y, m - 1, d, hh, mm);
      if (target === 'start') startDateTime.value = next;
      else endDateTime.value = next;
    },
  });
}

const startTime = timeModel('start');
const endTime = timeModel('end');
</script>

<template>
  <div class="step">
    <h2 class="step-title">When is the show?</h2>

    <MonthCalendar class="compact" :shows="shows" @day-click="onDayClick" />

    <div v-if="selectedDateLabel" class="time-row">
      <span class="selected-date">{{ selectedDateLabel }}</span>
      <div class="time-fields">
        <label class="time-field">
          <span>Start</span>
          <input v-model="startTime" type="time" class="time-input" />
        </label>
        <label class="time-field">
          <span>End</span>
          <input v-model="endTime" type="time" class="time-input" />
        </label>
      </div>
      <p v-if="!wizard.rangeValid.value && wizard.rangeError.value" class="field-error">
        {{ wizard.rangeError.value }}
      </p>
    </div>
    <p v-else class="step-hint">Pick a day in the calendar above.</p>
  </div>
</template>

<style scoped>
.step-title {
  font-size: var(--font-size-lg);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin: 0 0 var(--spacing-lg);
  text-align: center;
}

.step-hint {
  color: var(--color-text-muted);
  margin: var(--spacing-lg) 0 0;
  text-align: center;
}

.time-row {
  margin-top: var(--spacing-lg);
  text-align: center;
}

.selected-date {
  display: block;
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin-bottom: var(--spacing-md);
}

.time-fields {
  display: flex;
  justify-content: center;
  gap: var(--spacing-lg);
}

.time-field {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.time-input {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-family: var(--font-family);
  padding: var(--spacing-sm) var(--spacing-md);
}

.time-input:focus {
  outline: none;
  border-color: var(--color-primary);
}

.field-error {
  color: var(--color-error);
  font-size: var(--font-size-sm);
  margin: var(--spacing-md) 0 0;
}
</style>
