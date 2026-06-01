<script setup lang="ts">
import type { ScheduleItem } from '../api';

const props = defineProps<{
  show: ScheduleItem;
  isPast: boolean;
}>();

function getDaysUntil(dateStr: string): number {
  const showDate = new Date(dateStr);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  showDate.setHours(0, 0, 0, 0);
  const diffTime = showDate.getTime() - today.getTime();
  return Math.ceil(diffTime / (1000 * 60 * 60 * 24));
}

function getDaysClass(days: number): string {
  if (days < 0) return 'days-past';
  if (days <= 7) return 'days-critical';
  if (days <= 15) return 'days-warning';
  return 'days-ok';
}

function getDotColor(show: ScheduleItem): string {
  const daysUntil = getDaysUntil(show.date);
  if (daysUntil < 0) return 'dot-gray';
  const type = show.show_type || 'unheard';
  if (type === 'brunchtime') return 'dot-green';
  if (type === 'external') return 'dot-blue';
  return 'dot-yellow';
}

function formatDateShort(dateStr: string): string {
  const d = new Date(dateStr + 'T12:00:00');
  return d.toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
  });
}

const daysUntil = getDaysUntil(props.show.date);
</script>

<template>
  <div class="list-show-item">
    <div class="list-show-date-col">
      <span class="list-show-date">{{ formatDateShort(show.date) }}</span>
      <span :class="['badge', 'days-badge', getDaysClass(daysUntil)]">
        {{ daysUntil < 0 ? 'Past' : daysUntil + 'd' }}
      </span>
    </div>
    <div class="list-show-info">
      <span class="list-show-title-row">
        <span class="list-show-title">{{ show.title }}</span>
        <span
          v-if="show.show_type === 'unheard' || !show.show_type"
          :class="[
            'badge',
            'artist-badge',
            {
              'count-empty': show.artists.length === 0,
              'count-partial': show.artists.length > 0 && show.artists.length < 4,
              'count-full': show.artists.length >= 4,
            },
          ]"
        >
          {{ show.artists.length }}/4
        </span>
      </span>
      <span
        v-if="show.show_type === 'unheard' || !show.show_type"
        class="list-show-artists text-muted"
      >
        {{ show.artists.map((a) => a.name).join(', ') || 'No artists assigned' }}
      </span>
      <span v-if="show.host_username" class="list-show-host text-muted">
        Host: {{ show.host_username }}
      </span>
    </div>
    <div class="list-show-meta">
      <span :class="['badge', 'show-type-badge', `type-${show.show_type || 'unheard'}`]">
        {{ (show.show_type || 'unheard').toUpperCase() }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.list-show-item {
  display: flex;
  align-items: center;
  gap: var(--spacing-lg);
  padding: var(--spacing-md) var(--spacing-lg);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.list-show-item:hover {
  background: var(--color-surface-alt);
  border-color: var(--color-border-light);
}

.list-show-date-col {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--spacing-xs);
  min-width: 100px;
}

.list-show-date {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  white-space: nowrap;
}

.list-show-info {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
  flex: 1;
  min-width: 0;
}

.list-show-title-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  min-width: 0;
}

.list-show-title {
  color: var(--color-primary);
  font-weight: var(--font-weight-medium);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.show-type-badge {
  font-size: var(--font-size-xs, 0.65rem);
  padding: 0.15rem 0.4rem;
  border-radius: var(--radius-sm);
  font-weight: var(--font-weight-bold);
  text-transform: uppercase;
  letter-spacing: 0.03em;
  flex-shrink: 0;
}

.type-unheard {
  background-color: rgba(255, 236, 68, 0.2);
  color: #ffec44;
  border: 1px solid #ffec44;
}

.type-brunchtime {
  background-color: rgba(52, 199, 89, 0.2);
  color: #34c759;
  border: 1px solid #34c759;
}

.type-external {
  background-color: rgba(52, 120, 246, 0.2);
  color: #3478f6;
  border: 1px solid #3478f6;
}

.list-show-artists {
  font-size: var(--font-size-sm);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.list-show-host {
  font-size: var(--font-size-sm);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.list-show-meta {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  flex-shrink: 0;
}

/* Badge colors */
.days-badge {
  font-size: var(--font-size-xs, 0.65rem);
  padding: 0.15rem 0.4rem;
  border-radius: var(--radius-sm);
  font-weight: var(--font-weight-bold);
}

.days-past {
  background-color: rgba(142, 142, 147, 0.2);
  color: #8e8e93;
  border: 1px solid #8e8e93;
}

.days-critical {
  background-color: rgba(255, 59, 48, 0.2);
  color: #ff3b30;
  border: 1px solid #ff3b30;
}

.days-warning {
  background-color: rgba(255, 149, 0, 0.2);
  color: #ff9500;
  border: 1px solid #ff9500;
}

.days-ok {
  background-color: rgba(52, 199, 89, 0.2);
  color: #34c759;
  border: 1px solid #34c759;
}

.count-empty {
  background-color: rgba(255, 59, 48, 0.2);
  color: #ff3b30;
  border: 1px solid #ff3b30;
}

.count-partial {
  background-color: rgba(255, 149, 0, 0.2);
  color: #ff9500;
  border: 1px solid #ff9500;
}

.count-full {
  background-color: rgba(52, 199, 89, 0.2);
  color: #34c759;
  border: 1px solid #34c759;
}

/* Dot colors */
.legend-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
  flex-shrink: 0;
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
  background-color: #8e8e93;
}

/* Responsive */
@media (max-width: 900px) {
  .list-show-item {
    flex-wrap: wrap;
    gap: var(--spacing-sm);
  }

  .list-show-date-col {
    flex-direction: row;
    min-width: auto;
  }
}
</style>
