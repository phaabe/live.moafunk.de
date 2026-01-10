<script setup lang="ts">
interface Props {
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost';
  size?: 'sm' | 'md' | 'lg';
  disabled?: boolean;
  loading?: boolean;
  type?: 'button' | 'submit' | 'reset';
}

withDefaults(defineProps<Props>(), {
  variant: 'primary',
  size: 'md',
  disabled: false,
  loading: false,
  type: 'button',
});

defineEmits<{
  click: [event: MouseEvent];
}>();
</script>

<template>
  <button
    :type="type"
    :disabled="disabled || loading"
    :class="['base-button', `variant-${variant}`, `size-${size}`, { loading }]"
    @click="$emit('click', $event)"
  >
    <span v-if="loading" class="spinner"></span>
    <slot />
  </button>
</template>

<style scoped>
.base-button {
  font-family: var(--font-family);
  font-weight: var(--font-weight-medium);
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--spacing-sm);
  transition: all var(--transition-fast);
}

.base-button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* Variants */
.variant-primary {
  background-color: var(--color-primary);
  color: var(--color-primary-text);
}

.variant-primary:hover:not(:disabled) {
  background-color: var(--color-primary-hover);
}

.variant-secondary {
  background-color: var(--color-surface-alt);
  color: var(--color-text);
  border: 1px solid var(--color-border);
}

.variant-secondary:hover:not(:disabled) {
  background-color: var(--color-surface-hover);
}

.variant-danger {
  background-color: var(--color-error);
  color: var(--color-text);
}

.variant-danger:hover:not(:disabled) {
  background-color: #dc2626;
}

.variant-ghost {
  background-color: transparent;
  color: var(--color-text-muted);
}

.variant-ghost:hover:not(:disabled) {
  background-color: var(--color-surface-alt);
  color: var(--color-text);
}

/* Sizes */
.size-sm {
  padding: var(--spacing-xs) var(--spacing-sm);
  font-size: var(--font-size-sm);
}

.size-md {
  padding: var(--spacing-sm) var(--spacing-md);
  font-size: var(--font-size-md);
}

.size-lg {
  padding: var(--spacing-md) var(--spacing-lg);
  font-size: var(--font-size-lg);
}

/* Loading spinner */
.spinner {
  width: 1em;
  height: 1em;
  border: 2px solid currentColor;
  border-right-color: transparent;
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
