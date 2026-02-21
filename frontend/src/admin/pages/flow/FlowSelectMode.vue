<script setup lang="ts">
import { useRouter } from 'vue-router';
import { useArtistFlow } from '@admin/composables';

const router = useRouter();
const flow = useArtistFlow();

function selectPrerecorded() {
  flow.selectMode('prerecorded');
  router.push('/stream/upload');
}

function goBack() {
  flow.goToStep('info');
  router.push('/stream/info');
}
</script>

<template>
  <div class="flow-mode">
    <h1 class="flow-mode-title">How would you like to deliver your show?</h1>
    <p class="flow-mode-subtitle">Choose how you want your set to be played on air.</p>

    <div class="mode-cards">
      <!-- Pre-recorded card -->
      <button class="mode-card" @click="selectPrerecorded">
        <div class="mode-icon">📁</div>
        <h2 class="mode-card-title">Pre-recorded</h2>
        <p class="mode-card-desc">
          Upload your pre-recorded set. We'll play it at your scheduled time.
        </p>
      </button>

      <!-- Live card (disabled) -->
      <div class="mode-card disabled">
        <div class="mode-badge">Coming Soon</div>
        <div class="mode-icon">🎙️</div>
        <h2 class="mode-card-title">Live</h2>
        <p class="mode-card-desc">
          Stream your set live directly from your browser.
        </p>
      </div>
    </div>

    <div class="flow-mode-actions">
      <button class="btn-secondary" @click="goBack">
        ← Back
      </button>
    </div>
  </div>
</template>

<style scoped>
.flow-mode-title {
  font-size: var(--font-size-2xl);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin: 0 0 var(--spacing-sm);
}

.flow-mode-subtitle {
  color: var(--color-text-muted);
  margin: 0 0 var(--spacing-2xl);
}

.mode-cards {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--spacing-lg);
  margin-bottom: var(--spacing-2xl);
}

@media (max-width: 600px) {
  .mode-cards {
    grid-template-columns: 1fr;
  }
}

.mode-card {
  background: var(--color-surface);
  border: 2px solid var(--color-border);
  border-radius: var(--radius-xl);
  padding: var(--spacing-2xl) var(--spacing-xl);
  text-align: center;
  cursor: pointer;
  transition: all var(--transition-fast);
  position: relative;
  font-family: var(--font-family);
  color: var(--color-text);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--spacing-md);
}

.mode-card:not(.disabled):hover {
  border-color: var(--color-primary);
  background: var(--color-surface-hover);
}

.mode-card.disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.mode-badge {
  position: absolute;
  top: var(--spacing-md);
  right: var(--spacing-md);
  background: var(--color-surface-alt);
  color: var(--color-text-muted);
  font-size: var(--font-size-xs);
  padding: var(--spacing-xs) var(--spacing-sm);
  border-radius: var(--radius-full);
  border: 1px solid var(--color-border);
}

.mode-icon {
  font-size: 2.5rem;
}

.mode-card-title {
  font-size: var(--font-size-xl);
  font-weight: var(--font-weight-bold);
  margin: 0;
}

.mode-card-desc {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  margin: 0;
  line-height: var(--line-height-relaxed);
}

.flow-mode-actions {
  display: flex;
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
