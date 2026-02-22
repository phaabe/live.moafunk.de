<script setup lang="ts">
import { computed } from 'vue';
import { useRouter } from 'vue-router';
import { useHostFlow } from '@admin/composables';
import type { MyShowInfo } from '@admin/api';

const router = useRouter();
const flow = useHostFlow();

const shows = computed(() => flow.shows.value);

/** Format a date string into a readable date */
function fmtDate(dateStr: string): string {
  const d = new Date(dateStr + 'T12:00:00');
  return d.toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

/** Days until this show */
function getDaysUntil(dateStr: string): number {
  const showDate = new Date(dateStr);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  showDate.setHours(0, 0, 0, 0);
  const diffTime = showDate.getTime() - today.getTime();
  return Math.ceil(diffTime / (1000 * 60 * 60 * 24));
}

function daysLabel(dateStr: string): string {
  const days = getDaysUntil(dateStr);
  if (days === 0) return 'Today';
  if (days === 1) return 'Tomorrow';
  if (days < 0) return `${Math.abs(days)}d ago`;
  return `In ${days}d`;
}

function daysClass(dateStr: string): string {
  const days = getDaysUntil(dateStr);
  if (days < 0) return 'days-past';
  if (days === 0) return 'days-today';
  if (days <= 3) return 'days-soon';
  return 'days-future';
}

/** Select a show and navigate to its info page */
function pickShow(s: MyShowInfo) {
  flow.selectShow(s);
  router.push('/stream/info');
}

/** Show type badge text */
function showTypeBadge(type: string): string {
  switch (type) {
    case 'unheard': return 'UNHEARD';
    case 'brunchtime': return 'Brunchtime';
    case 'external': return 'External';
    default: return type;
  }
}
</script>

<template>
  <div class="flow-select">
    <h1 class="flow-select-title">My Shows</h1>
    <p class="flow-select-subtitle">Select a show to prepare for streaming.</p>

    <div v-if="shows.length === 0" class="flow-select-empty">
      <p class="text-muted">No shows assigned.</p>
    </div>

    <div class="show-cards">
      <button v-for="s in shows" :key="s.id" class="show-card" @click="pickShow(s)">
        <div class="show-card-header">
          <span class="show-card-type">{{ showTypeBadge(s.show_type) }}</span>
          <span :class="['show-card-days', daysClass(s.date)]">{{ daysLabel(s.date) }}</span>
        </div>
        <h2 class="show-card-title">{{ s.title }}</h2>
        <div class="show-card-date">{{ fmtDate(s.date) }}</div>
        <div v-if="s.start_time" class="show-card-time">
          {{ s.start_time }}<template v-if="s.end_time"> – {{ s.end_time }}</template>
        </div>
        <div v-if="s.artists.length > 0" class="show-card-artists">
          <span v-for="artist in s.artists" :key="artist.id" class="artist-chip">
            {{ artist.name }}
          </span>
        </div>
        <div v-if="s.prerecorded_confirmed_at" class="show-card-status status-confirmed">
          ✓ Pre-recorded & confirmed
        </div>
        <div v-else-if="s.prerecorded_key" class="show-card-status status-uploaded">
          ↑ Uploaded — needs confirmation
        </div>
      </button>
    </div>
  </div>
</template>

<style scoped>
.flow-select-title {
  font-size: var(--font-size-3xl);
  font-weight: var(--font-weight-bold);
  color: var(--color-primary);
  margin: 0 0 var(--spacing-xs);
}

.flow-select-subtitle {
  color: var(--color-text-muted);
  margin: 0 0 var(--spacing-xl);
}

.flow-select-empty {
  text-align: center;
  padding: var(--spacing-2xl);
}

.show-cards {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.show-card {
  display: block;
  width: 100%;
  text-align: left;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-lg);
  cursor: pointer;
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
  font-family: var(--font-family);
}

.show-card:hover {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 1px var(--color-primary);
}

.show-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--spacing-sm);
}

.show-card-type {
  font-size: var(--font-size-xs);
  font-weight: var(--font-weight-bold);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--color-text-muted);
}

.show-card-days {
  font-size: var(--font-size-xs);
  font-weight: var(--font-weight-bold);
  padding: 2px 8px;
  border-radius: var(--radius-full);
}

.days-today {
  background: rgba(52, 199, 89, 0.2);
  color: #34c759;
}

.days-soon {
  background: rgba(255, 204, 0, 0.2);
  color: #ffcc00;
}

.days-future {
  background: rgba(94, 152, 210, 0.15);
  color: #5e98d2;
}

.days-past {
  background: rgba(142, 142, 147, 0.15);
  color: #8e8e93;
}

.show-card-title {
  font-size: var(--font-size-xl);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin: 0 0 var(--spacing-xs);
}

.show-card-date {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.show-card-time {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  margin-top: 2px;
}

.show-card-artists {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-xs);
  margin-top: var(--spacing-md);
}

.artist-chip {
  background: var(--color-surface-alt);
  color: var(--color-text);
  padding: 2px var(--spacing-sm);
  border-radius: var(--radius-full);
  font-size: var(--font-size-xs);
  border: 1px solid var(--color-border);
}

.show-card-status {
  margin-top: var(--spacing-md);
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-medium);
}

.status-confirmed {
  color: #34c759;
}

.status-uploaded {
  color: #ffcc00;
}
</style>
