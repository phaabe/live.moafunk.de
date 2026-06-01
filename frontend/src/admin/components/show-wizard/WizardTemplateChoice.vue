<script setup lang="ts">
import { useShowWizard } from '../../composables';

const wizard = useShowWizard();

async function choose(mode: 'existing' | 'new') {
  wizard.setMode(mode);
  if (mode === 'existing') {
    await wizard.loadTemplates();
  }
  wizard.goNext();
}
</script>

<template>
  <div class="step">
    <h2 class="step-title">How do you want to start?</h2>
    <p class="step-hint">Reuse a saved show template, or build a new one.</p>

    <div class="choice-grid">
      <button
        type="button"
        :class="['choice-card', { selected: wizard.mode.value === 'existing' }]"
        @click="choose('existing')"
      >
        <span class="choice-icon">📁</span>
        <span class="choice-label">Use existing template</span>
        <span class="choice-sub">Pick from your saved templates</span>
      </button>

      <button
        type="button"
        :class="['choice-card', { selected: wizard.mode.value === 'new' }]"
        @click="choose('new')"
      >
        <span class="choice-icon">✨</span>
        <span class="choice-label">Create new template</span>
        <span class="choice-sub">Name, cover photo & description</span>
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

.choice-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--spacing-lg);
}

.choice-card {
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

.choice-card:hover,
.choice-card.selected {
  border-color: var(--color-primary);
  background: var(--color-surface-hover);
}

.choice-icon {
  font-size: 2rem;
}

.choice-label {
  font-size: var(--font-size-base);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
}

.choice-sub {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

@media (max-width: 600px) {
  .choice-grid {
    grid-template-columns: 1fr;
  }
}
</style>
