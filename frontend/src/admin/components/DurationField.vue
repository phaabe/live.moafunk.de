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

const PRESETS = [30, 60, 90, 120, 180, 240, 300, 360, 420, 480, 540, 720];

function label(min: number): string {
  // Clean half-hour steps read as decimal hours (0.5h, 1.5h, 2h); anything
  // off-grid (an injected loaded value) falls back to "Xh Ym".
  if (min % 30 === 0) return `${min / 60}h`;
  const h = Math.floor(min / 60);
  const m = min % 60;
  return h ? `${h}h ${m}m` : `${m}m`;
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
