<script setup lang="ts">
import { computed } from 'vue';
import { useShowWizard } from '../../composables';

const wizard = useShowWizard();

const dateLabel = computed(() => {
  const d = wizard.startDateTime.value;
  const e = wizard.endDateTime.value;
  if (!d) return '—';
  const day = d.toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
  const pad = (n: number) => String(n).padStart(2, '0');
  const start = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const end = e ? `${pad(e.getHours())}:${pad(e.getMinutes())}` : '';
  return end ? `${day}, ${start}–${end}` : `${day}, ${start}`;
});
</script>

<template>
  <div class="step">
    <h2 class="step-title">Review &amp; confirm</h2>

    <div class="summary">
      <div class="summary-cover">
        <img v-if="wizard.summaryCoverUrl.value" :src="wizard.summaryCoverUrl.value" alt="Cover" />
        <div v-else class="summary-cover-placeholder">No cover</div>
      </div>

      <dl class="summary-details">
        <dt>Title</dt>
        <dd>{{ wizard.summaryName.value || '—' }}</dd>

        <dt>Description</dt>
        <dd>{{ wizard.summaryDescription.value || '—' }}</dd>

        <dt>When</dt>
        <dd>{{ dateLabel }}</dd>

        <template v-if="wizard.isAdmin.value">
          <dt>Host</dt>
          <dd>{{ wizard.assigneeUsername.value || '—' }}</dd>
        </template>
      </dl>
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

.summary {
  display: flex;
  gap: var(--spacing-lg);
  align-items: flex-start;
}

.summary-cover,
.summary-cover-placeholder {
  width: 160px;
  height: 160px;
  flex-shrink: 0;
  border-radius: var(--radius-md);
  overflow: hidden;
  background: var(--color-surface-alt);
}

.summary-cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.summary-cover-placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.summary-details {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: var(--spacing-xs) var(--spacing-md);
  margin: 0;
  flex: 1;
}

.summary-details dt {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  font-weight: var(--font-weight-medium);
}

.summary-details dd {
  margin: 0;
  color: var(--color-text);
}

@media (max-width: 600px) {
  .summary {
    flex-direction: column;
    align-items: center;
    text-align: center;
  }
}
</style>
