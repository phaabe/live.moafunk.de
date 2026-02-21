<script setup lang="ts">
import { computed } from 'vue';
import { useHostFlow } from '@admin/composables';

const flow = useHostFlow();
const show = computed(() => flow.show.value);

const formattedDate = computed(() => {
  if (!show.value?.date) return '';
  try {
    const d = new Date(show.value.date + 'T00:00:00');
    return d.toLocaleDateString('en-US', {
      weekday: 'long',
      year: 'numeric',
      month: 'long',
      day: 'numeric',
    });
  } catch {
    return show.value.date;
  }
});
</script>

<template>
  <div class="flow-live">
    <div class="live-placeholder">
      <div class="live-icon">🎙️</div>
      <h1 class="live-title">Live Streaming Room</h1>
      <p class="live-coming-soon">Coming Soon</p>

      <div v-if="show" class="live-show-info">
        <p class="live-show-title">{{ show.title }}</p>
        <p class="live-show-date">
          {{ formattedDate }}
          <template v-if="show.start_time"> · {{ show.start_time }}</template>
        </p>
      </div>

      <p class="live-description">
        The live streaming room will allow you to broadcast your set in real-time
        directly from your browser. Stay tuned!
      </p>
    </div>
  </div>
</template>

<style scoped>
.flow-live {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 50vh;
}

.live-placeholder {
  text-align: center;
  max-width: 480px;
}

.live-icon {
  font-size: 3rem;
  margin-bottom: var(--spacing-lg);
}

.live-title {
  font-size: var(--font-size-2xl);
  font-weight: var(--font-weight-bold);
  margin: 0 0 var(--spacing-sm);
}

.live-coming-soon {
  display: inline-block;
  background: var(--color-surface-alt);
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  padding: var(--spacing-xs) var(--spacing-md);
  border-radius: var(--radius-full);
  border: 1px solid var(--color-border);
  margin: 0 0 var(--spacing-xl);
}

.live-show-info {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-lg);
  margin-bottom: var(--spacing-xl);
}

.live-show-title {
  font-weight: var(--font-weight-bold);
  color: var(--color-primary);
  margin: 0 0 var(--spacing-xs);
}

.live-show-date {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  margin: 0;
}

.live-description {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  line-height: var(--line-height-relaxed);
  margin: 0;
}
</style>
