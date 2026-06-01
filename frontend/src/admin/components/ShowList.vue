<script setup lang="ts">
import { computed } from 'vue';
import type { ScheduleItem } from '../api';
import ShowListItem from './ShowListItem.vue';

const props = withDefaults(
  defineProps<{
    shows: ScheduleItem[];
    limit?: number;
    filter?: 'upcoming' | 'all' | 'past';
  }>(),
  {
    filter: 'upcoming',
  }
);

const emit = defineEmits<{
  (e: 'show-click', show: ScheduleItem): void;
}>();

function getDaysUntil(dateStr: string): number {
  const showDate = new Date(dateStr);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  showDate.setHours(0, 0, 0, 0);
  const diffTime = showDate.getTime() - today.getTime();
  return Math.ceil(diffTime / (1000 * 60 * 60 * 24));
}

const filteredShows = computed(() => {
  let filtered = props.shows;
  if (props.filter === 'upcoming') {
    filtered = props.shows.filter((s) => getDaysUntil(s.date) >= 0);
  } else if (props.filter === 'past') {
    filtered = props.shows.filter((s) => getDaysUntil(s.date) < 0);
  }

  const sorted = [...filtered].sort(
    (a, b) => new Date(a.date).getTime() - new Date(b.date).getTime()
  );

  if (props.limit && props.limit > 0) {
    return sorted.slice(0, props.limit);
  }
  return sorted;
});

const showsByMonth = computed(() => {
  const groups: { month: string; shows: ScheduleItem[] }[] = [];
  let currentMonth = '';
  for (const show of filteredShows.value) {
    const d = new Date(show.date + 'T12:00:00');
    const month = d.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
    if (month !== currentMonth) {
      groups.push({ month, shows: [] });
      currentMonth = month;
    }
    groups[groups.length - 1].shows.push(show);
  }
  return groups;
});
</script>

<template>
  <div class="show-list">
    <div v-if="filteredShows.length === 0" class="show-list-empty">
      <p class="text-muted">No shows found</p>
    </div>

    <div v-for="group in showsByMonth" :key="group.month" class="show-list-month-group">
      <h3 class="show-list-month-header">{{ group.month }}</h3>
      <div class="show-list-items">
        <ShowListItem
          v-for="show in group.shows"
          :key="show.id"
          :show="show"
          :is-past="getDaysUntil(show.date) < 0"
          @click="emit('show-click', show)"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.show-list-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--spacing-2xl);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

.show-list-month-group {
  margin-bottom: var(--spacing-xl);
}

.show-list-month-group:last-child {
  margin-bottom: 0;
}

.show-list-month-header {
  font-size: var(--font-size-base);
  font-weight: var(--font-weight-bold);
  color: var(--color-text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: var(--spacing-sm);
  padding-bottom: var(--spacing-xs);
  border-bottom: 1px solid var(--color-border);
}

.show-list-items {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}
</style>
