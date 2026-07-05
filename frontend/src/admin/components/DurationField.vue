<script setup lang="ts">
import { computed } from 'vue';

/**
 * Show-length picker (minutes). Presets cover typical radio slots; a show
 * loaded with an off-preset length has that value injected so it round-trips.
 */
const props = defineProps<{
  /** Current duration in minutes. */
  modelValue: number;
}>();

const emit = defineEmits<{
  'update:modelValue': [value: number];
}>();

const PRESETS = [30, 45, 60, 75, 90, 105, 120, 150, 180, 210, 240];

function label(min: number): string {
  const h = Math.floor(min / 60);
  const m = min % 60;
  if (h && m) return `${h}h ${m}m`;
  if (h) return `${h}h`;
  return `${m}m`;
}

/** Presets plus the current value if it isn't one, so any loaded show fits. */
const options = computed(() => {
  const mins = PRESETS.includes(props.modelValue)
    ? PRESETS
    : [...PRESETS, props.modelValue].sort((a, b) => a - b);
  return mins.map((m) => ({ value: m, text: label(m) }));
});
</script>

<template>
  <select
    class="duration-select"
    :value="modelValue"
    @change="emit('update:modelValue', Number(($event.target as HTMLSelectElement).value))"
  >
    <option v-for="opt in options" :key="opt.value" :value="opt.value">{{ opt.text }}</option>
  </select>
</template>

<style scoped>
.duration-select {
  padding: var(--spacing-sm) var(--spacing-md);
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-family: var(--font-family);
  font-size: 1em;
}

.duration-select:focus {
  outline: none;
  border-color: var(--color-primary);
}
</style>
