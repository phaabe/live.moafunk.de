<script setup lang="ts">
import { useShowWizard } from '../../composables';
import { FormInput } from '@shared/components';

const wizard = useShowWizard();
const { newName } = wizard;
</script>

<template>
  <div class="step">
    <h2 class="step-title">What's the show called?</h2>
    <p class="step-hint">This becomes the show's title.</p>
    <FormInput v-model="newName" label="Show name" required />
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
</style>
