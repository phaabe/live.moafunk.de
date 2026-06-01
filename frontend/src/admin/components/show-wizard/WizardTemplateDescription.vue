<script setup lang="ts">
import { useShowWizard } from '../../composables';

const wizard = useShowWizard();
const { newDescription } = wizard;
</script>

<template>
  <div class="step">
    <h2 class="step-title">Describe the show</h2>
    <p class="step-hint">Optional — a short description shown with the show.</p>
    <textarea
      v-model="newDescription"
      class="description-input"
      rows="6"
      placeholder="What's this show about?"
    ></textarea>
  </div>
</template>

<style scoped>
.step-title {
  font-size: var(--font-size-lg);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin: 0 0 var(--spacing-xs);
  text-align: center;
}

.step-hint {
  color: var(--color-text-muted);
  margin: 0 0 var(--spacing-xl);
  text-align: center;
}

.description-input {
  width: 100%;
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-family: var(--font-family);
  font-size: var(--font-size-base);
  padding: var(--spacing-sm) var(--spacing-md);
  resize: vertical;
}

.description-input:focus {
  outline: none;
  border-color: var(--color-primary);
}
</style>
