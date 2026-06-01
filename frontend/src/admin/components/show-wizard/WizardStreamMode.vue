<script setup lang="ts">
import { useShowWizard, type StreamMode } from '../../composables';

const wizard = useShowWizard();

function choose(mode: StreamMode) {
  wizard.setStreamMode(mode);
  wizard.goNext();
}
</script>

<template>
  <div class="step">
    <h2 class="step-title">How will the show be delivered?</h2>
    <p class="step-hint">You can change this later.</p>

    <div class="mode-grid">
      <button
        type="button"
        :class="['mode-card', { selected: wizard.streamMode.value === 'live' }]"
        @click="choose('live')"
      >
        <span class="mode-icon">🎙️</span>
        <span class="mode-label">Live</span>
        <span class="mode-sub">Stream the set live from the browser</span>
      </button>

      <button
        type="button"
        :class="['mode-card', { selected: wizard.streamMode.value === 'prerecorded' }]"
        @click="choose('prerecorded')"
      >
        <span class="mode-icon">📁</span>
        <span class="mode-label">Upload</span>
        <span class="mode-sub">Upload a pre-recorded set to play at the scheduled time</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.step {
  text-align: center;
}

.step-title {
  font-size: var(--font-size-lg);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin: 0 0 var(--spacing-xs);
}

.step-hint {
  color: var(--color-text-muted);
  margin: 0 0 var(--spacing-xl);
}

.mode-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--spacing-lg);
}

.mode-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--spacing-sm);
  padding: var(--spacing-xl) var(--spacing-lg);
  background: var(--color-surface-alt);
  border: 2px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  font-family: var(--font-family);
  transition: all var(--transition-fast);
}

.mode-card:hover,
.mode-card.selected {
  border-color: var(--color-primary);
  background: var(--color-surface-hover);
}

.mode-icon {
  font-size: 2rem;
}

.mode-label {
  font-size: var(--font-size-base);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
}

.mode-sub {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

@media (max-width: 600px) {
  .mode-grid {
    grid-template-columns: 1fr;
  }
}
</style>
