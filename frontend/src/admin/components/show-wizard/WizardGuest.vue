<script setup lang="ts">
import { computed } from 'vue';
import { useShowWizard } from '../../composables';

const wizard = useShowWizard();
const { guestUsername } = wizard;

// Login is limited to the show's date; surface that to the admin.
const showDate = computed(() => wizard.startDateTime.value);
const dateLabel = computed(() =>
  showDate.value
    ? showDate.value.toLocaleDateString('en-US', {
        weekday: 'short',
        month: 'short',
        day: 'numeric',
        year: 'numeric',
      })
    : 'the show date'
);
</script>

<template>
  <div class="step">
    <h2 class="step-title">Invite a guest (optional)</h2>
    <p class="step-hint">
      Create a guest login that only works on {{ dateLabel }}. Leave blank to skip.
    </p>

    <div class="field">
      <label class="field-label" for="guest-username">Guest username</label>
      <input
        id="guest-username"
        v-model="guestUsername"
        type="text"
        class="field-input"
        placeholder="e.g. dj-guest"
        autocomplete="off"
      />
    </div>

    <p class="field-note">
      A one-time password is generated when the show is created. The guest must choose their own
      password on first login.
    </p>
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

.field {
  max-width: 360px;
  margin: 0 auto;
  text-align: left;
}

.field-label {
  display: block;
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-medium);
  color: var(--color-text-muted);
  margin-bottom: var(--spacing-xs);
}

.field-input {
  width: 100%;
  padding: var(--spacing-sm) var(--spacing-md);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-family: var(--font-family);
  font-size: var(--font-size-base);
}

.field-input:focus {
  outline: none;
  border-color: var(--color-primary);
}

.field-note {
  max-width: 360px;
  margin: var(--spacing-lg) auto 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
}
</style>
