<script setup lang="ts">
import { computed } from 'vue';
import { Calendar } from 'v-calendar';
import type { Show } from '../api';

const props = defineProps<{
  shows: Show[];
}>();

const emit = defineEmits<{
  (e: 'day-click', dateStr: string): void;
  (e: 'show-click', show: Show): void;
}>();

function getDaysUntil(dateStr: string): number {
  const showDate = new Date(dateStr);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  showDate.setHours(0, 0, 0, 0);
  const diffTime = showDate.getTime() - today.getTime();
  return Math.ceil(diffTime / (1000 * 60 * 60 * 24));
}

const calendarAttributes = computed(() => {
  const attrs: Record<string, unknown>[] = [];

  // Highlight today
  attrs.push({
    key: 'today',
    highlight: {
      color: 'yellow',
      fillMode: 'solid',
    },
    dates: new Date(),
  });

  for (const show of props.shows) {
    const daysUntil = getDaysUntil(show.date);
    let color = 'yellow';
    if (daysUntil < 0) {
      color = 'gray';
    } else {
      const type = show.show_type || 'unheard';
      if (type === 'brunchtime') color = 'green';
      else if (type === 'external') color = 'blue';
      else color = 'yellow';
    }

    attrs.push({
      key: `show-${show.id}`,
      dot: { color, class: 'show-dot' },
      dates: new Date(show.date + 'T12:00:00'),
      popover: {
        label: show.title,
        visibility: 'hover' as const,
      },
      customData: show,
    });
  }

  return attrs;
});

function onDayClick(day: { id: string; date: Date }) {
  const d = day.date;
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  emit('day-click', `${yyyy}-${mm}-${dd}`);
}
</script>

<template>
  <div class="month-calendar">
    <Calendar :attributes="calendarAttributes" :is-dark="true" :first-day-of-week="2" is-expanded
      @dayclick="onDayClick" />
    <div class="calendar-legend">
      <span class="legend-item"><span class="legend-dot dot-yellow"></span> UNHEARD</span>
      <span class="legend-item"><span class="legend-dot dot-green"></span> Brunchtime</span>
      <span class="legend-item"><span class="legend-dot dot-blue"></span> External</span>
      <span class="legend-item"><span class="legend-dot dot-gray"></span> Past</span>
    </div>
  </div>
</template>

<style scoped>
/* v-calendar dark theme overrides */
.month-calendar :deep(.vc-container) {
  --vc-bg: var(--color-surface);
  --vc-border: var(--color-border);
  --vc-color: var(--color-text);
  --vc-font-family: var(--font-family);
  --vc-text-lg: var(--font-size-lg);
  --vc-text-base: var(--font-size-base);
  --vc-text-sm: var(--font-size-sm);
  --vc-white: var(--color-text);
  /* Accent scale (yellow primary) */
  --vc-accent-50: rgba(255, 236, 68, 0.05);
  --vc-accent-100: rgba(255, 236, 68, 0.1);
  --vc-accent-200: rgba(255, 236, 68, 0.2);
  --vc-accent-300: rgba(255, 236, 68, 0.3);
  --vc-accent-400: rgba(255, 236, 68, 0.5);
  --vc-accent-500: #ffec44;
  --vc-accent-600: #ffec44;
  --vc-accent-700: #e6d43e;
  --vc-accent-800: #ccbc37;
  --vc-accent-900: #b3a530;
  /* Gray scale */
  --vc-gray-50: rgba(255, 255, 255, 0.05);
  --vc-gray-100: rgba(255, 255, 255, 0.08);
  --vc-gray-200: rgba(255, 255, 255, 0.12);
  --vc-gray-300: var(--color-border);
  --vc-gray-400: var(--color-border-light);
  --vc-gray-500: var(--color-text-muted);
  --vc-gray-600: #aaa;
  --vc-gray-700: var(--color-border);
  --vc-gray-800: var(--color-surface-alt);
  --vc-gray-900: var(--color-surface);
  /* Header */
  --vc-header-title-color: var(--color-text);
  --vc-header-arrow-color: var(--color-text-muted);
  --vc-header-arrow-hover-bg: var(--color-surface-hover);
  /* Weekdays */
  --vc-weekday-color: var(--color-text-muted);
  /* Popover */
  --vc-popover-content-color: var(--color-text);
  --vc-popover-content-bg: var(--color-surface-alt);
  --vc-popover-content-border: var(--color-border);
  /* Nav */
  --vc-nav-hover-bg: var(--color-surface-hover);
  --vc-nav-title-color: var(--color-text);
  --vc-nav-item-active-color: var(--color-primary-text);
  --vc-nav-item-active-bg: var(--color-primary);
  --vc-nav-item-current-color: var(--color-primary);
  /* Hover */
  --vc-hover-bg: var(--color-surface-hover);
  background: var(--color-surface);
  border: none;
  width: 100%;
}

.month-calendar :deep(.vc-header) {
  padding: var(--spacing-lg) var(--spacing-lg) var(--spacing-xl);
}

.month-calendar :deep(.vc-header .vc-title),
.month-calendar :deep(.vc-header .vc-prev),
.month-calendar :deep(.vc-header .vc-next) {
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  font-family: var(--font-family);
  color: var(--color-text-muted);
}

.month-calendar :deep(.vc-header .vc-title) {
  color: var(--color-text);
  font-weight: var(--font-weight-bold);
}

.month-calendar :deep(.vc-header .vc-title:hover),
.month-calendar :deep(.vc-header .vc-prev:hover),
.month-calendar :deep(.vc-header .vc-next:hover) {
  background: var(--color-surface-hover);
}

/* Nav popover */
.month-calendar :deep(.vc-popover-content) {
  background: var(--color-surface-alt) !important;
  border-color: var(--color-border) !important;
  color: var(--color-text);
  font-family: var(--font-family);
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
  border-radius: var(--radius-md);
}

.month-calendar :deep(.vc-nav-title),
.month-calendar :deep(.vc-nav-arrow) {
  font-family: var(--font-family);
  color: var(--color-text);
  background: transparent;
}

.month-calendar :deep(.vc-nav-title:hover),
.month-calendar :deep(.vc-nav-arrow:hover) {
  background: var(--color-surface-hover) !important;
}

.month-calendar :deep(.vc-nav-item) {
  font-family: var(--font-family);
  color: var(--color-text-muted);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
}

.month-calendar :deep(.vc-nav-item:hover) {
  background: var(--color-surface-hover) !important;
  color: var(--color-text);
}

.month-calendar :deep(.vc-nav-item.is-active) {
  color: var(--color-primary-text) !important;
  background: var(--color-primary) !important;
  border-color: var(--color-primary) !important;
}

.month-calendar :deep(.vc-nav-item.is-current) {
  color: var(--color-primary) !important;
  border-color: var(--color-primary) !important;
}

.month-calendar :deep(.vc-weekday) {
  color: var(--color-text-muted);
  font-family: var(--font-family);
  font-weight: var(--font-weight-medium);
}

.month-calendar :deep(.vc-day) {
  min-height: 60px;
}

.month-calendar :deep(.vc-day-content) {
  font-family: var(--font-family);
  color: var(--color-text);
  border-radius: var(--radius-md);
  width: 32px;
  height: 32px;
  transition: background var(--transition-fast);
}

.month-calendar :deep(.vc-day-content:hover) {
  background: var(--color-surface-hover);
}

.month-calendar :deep(.vc-day-content:focus) {
  background: var(--color-surface-alt);
}

.month-calendar :deep(.vc-highlight) {
  background: var(--color-primary) !important;
  border-radius: var(--radius-md);
}

.month-calendar :deep(.vc-highlight + .vc-day-content),
.month-calendar :deep(.vc-day.is-today .vc-day-content),
.month-calendar :deep(.vc-highlights + .vc-day-content) {
  color: #000 !important;
}

.month-calendar :deep(.vc-dot) {
  width: 8px;
  height: 8px;
}

/* Legend */
.calendar-legend {
  display: flex;
  gap: var(--spacing-lg);
  padding: var(--spacing-md) var(--spacing-lg);
  border-top: 1px solid var(--color-border);
  justify-content: center;
}

.legend-item {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.legend-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}

.dot-yellow {
  background-color: #ffec44;
}

.dot-green {
  background-color: #34c759;
}

.dot-blue {
  background-color: #3478f6;
}

.dot-gray {
  background-color: #888;
}

/* Compact mode: tighter spacing for embedded use */
.month-calendar.compact :deep(.vc-header) {
  padding: var(--spacing-sm) var(--spacing-sm) var(--spacing-md);
}

.month-calendar.compact :deep(.vc-day) {
  min-height: 44px;
}

.month-calendar.compact .calendar-legend {
  gap: var(--spacing-md);
  padding: var(--spacing-sm) var(--spacing-md);
}

@media (max-width: 900px) {
  .calendar-legend {
    flex-wrap: wrap;
    gap: var(--spacing-md);
  }
}
</style>
