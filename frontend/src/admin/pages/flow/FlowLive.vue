<script setup lang="ts">
import { useRouter } from 'vue-router';
import { useHostFlow } from '@admin/composables';
import LiveSetupTest from '@admin/components/LiveSetupTest.vue';

/**
 * Fullscreen live-preparation step (/stream/live). The actual device setup +
 * rehearsal logic lives in the shared LiveSetupTest component, which the show
 * dashboard also renders inline; this page only wraps it with the step chrome
 * and handles navigation.
 */
const router = useRouter();
const flow = useHostFlow();

function goToStream() {
  flow.goToStep('on-air');
  router.push('/stream/on-air');
}

function goBackToMode() {
  flow.revertToMode();
  router.push(flow.showId.value ? `/shows/${flow.showId.value}` : '/stream/select');
}
</script>

<template>
  <div class="flow-live">
    <h1 class="step-title">Set Up Audio &amp; Test</h1>
    <p class="step-subtitle">
      Pick your audio input, check the level on the meter, then run a quick test.
    </p>

    <div class="setup-card">
      <LiveSetupTest @passed="goToStream" />
    </div>

    <div class="step-actions">
      <button class="btn-secondary" type="button" @click="goBackToMode">← Back</button>
    </div>
  </div>
</template>

<style scoped>
.flow-live {
  max-width: 640px;
  margin: 0 auto;
}

.step-title {
  font-size: var(--font-size-2xl);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin: 0 0 var(--spacing-sm);
}

.step-subtitle {
  color: var(--color-text-muted);
  margin: 0 0 var(--spacing-xl);
}

.setup-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-lg);
}

.step-actions {
  display: flex;
  justify-content: space-between;
  margin-top: var(--spacing-2xl);
  gap: var(--spacing-md);
}

.btn-secondary {
  background: none;
  border: 1px solid var(--color-border);
  color: var(--color-text-muted);
  padding: var(--spacing-sm) var(--spacing-xl);
  border-radius: var(--radius-md);
  font-family: var(--font-family);
  font-size: var(--font-size-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-secondary:hover {
  color: var(--color-text);
  border-color: var(--color-border-light);
}
</style>
