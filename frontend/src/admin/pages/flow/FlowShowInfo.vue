<script setup lang="ts">
import { computed } from 'vue';
import { useRouter } from 'vue-router';
import { useHostFlow } from '@admin/composables';

const router = useRouter();
const flow = useHostFlow();

const show = computed(() => flow.show.value);

/** Format a date string + time string into a readable datetime */
function fmtDateTime(date: string, time: string): string {
  const d = new Date(date + 'T' + time + ':00');
  return d.toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  }) + ' · ' + time;
}

function computeEndDate(date: string, startTime: string, endTime: string): string {
  if (endTime <= startTime) {
    const d = new Date(date + 'T00:00:00');
    d.setDate(d.getDate() + 1);
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    return `${yyyy}-${mm}-${dd}`;
  }
  return date;
}

const formattedStart = computed(() => {
  if (!show.value?.date || !show.value?.start_time) return '—';
  return fmtDateTime(show.value.date, show.value.start_time);
});

const formattedEnd = computed(() => {
  if (!show.value?.date || !show.value?.end_time) return '—';
  const endDate = show.value.start_time
    ? computeEndDate(show.value.date, show.value.start_time, show.value.end_time)
    : show.value.date;
  return fmtDateTime(endDate, show.value.end_time);
});

function proceed() {
  flow.goToStep('mode');
  router.push('/stream/mode');
}
</script>

<template>
  <div class="flow-info">
    <div v-if="!flow.assigned.value" class="flow-info-empty">
      <p>You are not assigned to a show.</p>
    </div>

    <template v-else-if="show">
      <h1 class="flow-info-title">{{ show.title }}</h1>

      <div class="flow-info-meta">
        <div class="meta-item">
          <span class="meta-label">Start</span>
          <span class="meta-value">{{ formattedStart }}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">End</span>
          <span class="meta-value">{{ formattedEnd }}</span>
        </div>
      </div>

      <div v-if="show.description" class="flow-info-description">
        <h3>Description</h3>
        <p>{{ show.description }}</p>
      </div>

      <div class="flow-info-artists">
        <h3>Artists</h3>
        <div class="artist-badges">
          <span v-for="artist in show.artists" :key="artist.id" class="artist-badge">
            {{ artist.name }}
          </span>
        </div>
      </div>

      <div class="flow-info-actions">
        <button class="btn-primary" @click="proceed">
          Next →
        </button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.flow-info-title {
  font-size: var(--font-size-3xl);
  font-weight: var(--font-weight-bold);
  color: var(--color-primary);
  margin: 0 0 var(--spacing-xl);
}

.flow-info-meta {
  display: flex;
  gap: var(--spacing-xl);
  margin-bottom: var(--spacing-xl);
}

.meta-item {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.meta-label {
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.meta-value {
  font-size: var(--font-size-lg);
  color: var(--color-text);
}

.flow-info-description {
  margin-bottom: var(--spacing-xl);
}

.flow-info-description h3,
.flow-info-artists h3 {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin: 0 0 var(--spacing-sm);
}

.flow-info-description p {
  color: var(--color-text);
  line-height: var(--line-height-relaxed);
  margin: 0;
}

.flow-info-artists {
  margin-bottom: var(--spacing-2xl);
}

.artist-badges {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
}

.artist-badge {
  background: var(--color-surface-alt);
  color: var(--color-text);
  padding: var(--spacing-xs) var(--spacing-md);
  border-radius: var(--radius-full);
  font-size: var(--font-size-sm);
  border: 1px solid var(--color-border);
}

.flow-info-actions {
  display: flex;
  justify-content: flex-end;
}

.btn-primary {
  background: var(--color-primary);
  color: var(--color-primary-text);
  border: none;
  padding: var(--spacing-sm) var(--spacing-xl);
  border-radius: var(--radius-md);
  font-family: var(--font-family);
  font-size: var(--font-size-md);
  font-weight: var(--font-weight-bold);
  cursor: pointer;
  transition: background var(--transition-fast);
}

.btn-primary:hover {
  background: var(--color-primary-hover);
}

.flow-info-empty {
  text-align: center;
  color: var(--color-text-muted);
  padding: var(--spacing-2xl);
}
</style>
