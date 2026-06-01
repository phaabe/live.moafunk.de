<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref } from 'vue';
import { useShowWizard } from '../../composables';
import MonthCalendar from '../MonthCalendar.vue';
import DayTimeline from './DayTimeline.vue';

const wizard = useShowWizard();
const { startDateTime, endDateTime, scheduledShows, conflictingShow } = wizard;

// Match the timeline's height to the calendar's (which changes with 5/6-week
// months) by measuring it live.
const calWrap = ref<HTMLElement | null>(null);
const calHeight = ref(420);
let resizeObserver: ResizeObserver | null = null;

onMounted(() => {
  void wizard.loadScheduledShows();
  if (calWrap.value && typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(() => {
      if (calWrap.value) calHeight.value = calWrap.value.clientHeight;
    });
    resizeObserver.observe(calWrap.value);
    calHeight.value = calWrap.value.clientHeight;
  }
});

onBeforeUnmount(() => {
  resizeObserver?.disconnect();
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

/** Midnight of the selected day, in epoch ms — the timeline's minute origin. */
const dayBase = computed(() => {
  if (!selectedDateStr.value) return null;
  const [y, m, d] = selectedDateStr.value.split('-').map(Number);
  return new Date(y, m - 1, d, 0, 0, 0, 0).getTime();
});

/** Show window as minutes-from-midnight, so a cross-midnight end stays > start. */
const startMinutes = computed(() =>
  startDateTime.value && dayBase.value !== null
    ? Math.round((startDateTime.value.getTime() - dayBase.value) / 60000)
    : null
);
const endMinutes = computed(() =>
  endDateTime.value && dayBase.value !== null
    ? Math.round((endDateTime.value.getTime() - dayBase.value) / 60000)
    : null
);

function onDayClick(dateStr: string) {
  const [y, m, d] = dateStr.split('-').map(Number);
  const s = startDateTime.value;
  const e = endDateTime.value;
  startDateTime.value = new Date(y, m - 1, d, s ? s.getHours() : 20, s ? s.getMinutes() : 0);
  endDateTime.value = new Date(y, m - 1, d, e ? e.getHours() : 22, e ? e.getMinutes() : 0);
}

/** Apply a window dragged/clicked on the timeline back onto the Date refs. */
function onTimelineUpdate({ start, end }: { start: number; end: number }) {
  if (dayBase.value === null) return;
  startDateTime.value = new Date(dayBase.value + start * 60000);
  endDateTime.value = new Date(dayBase.value + end * 60000);
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

    <div class="date-grid">
      <div ref="calWrap" class="cal-wrap">
        <MonthCalendar class="compact" :shows="scheduledShows" @day-click="onDayClick" />
      </div>

      <DayTimeline
        v-if="selectedDateStr"
        :date="selectedDateStr"
        :start-minutes="startMinutes"
        :end-minutes="endMinutes"
        :shows="scheduledShows"
        :conflict="!!conflictingShow"
        :height="calHeight"
        @update="onTimelineUpdate"
      />
      <div v-else class="timeline-placeholder">Pick a day in the calendar.</div>
    </div>

    <div v-if="selectedDateLabel && selectedDateStr" class="time-controls">
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
      <p v-if="conflictingShow" class="field-error">
        This overlaps “{{ conflictingShow.title }}” ({{ conflictingShow.start_time
        }}<template v-if="conflictingShow.end_time">–{{ conflictingShow.end_time }}</template
        >). Pick another time.
      </p>
      <p v-else-if="!wizard.rangeValid.value && wizard.rangeError.value" class="field-error">
        {{ wizard.rangeError.value }}
      </p>
    </div>
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

.date-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr);
  gap: var(--spacing-xl);
  align-items: start;
}

/* Enlarge the month calendar so it matches the day timeline's height. */
.date-grid :deep(.month-calendar.compact .vc-day) {
  min-height: 52px;
}

.timeline-placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 320px;
  border: 1px dashed var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text-muted);
  text-align: center;
  padding: var(--spacing-lg);
}

.time-controls {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--spacing-md);
  margin-top: var(--spacing-lg);
}

.selected-date {
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
}

.time-fields {
  display: flex;
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
  margin: 0;
}

@media (max-width: 800px) {
  .date-grid {
    grid-template-columns: 1fr;
    gap: var(--spacing-lg);
  }
}
</style>
