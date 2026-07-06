<script setup lang="ts">
// Dumb bar-spectrum renderer for the Live Panel 2.0 analyzer cards (#274).
// Feed it normalized bands from useSpectrum(); colors match the prototype
// (teal input, purple stream).
defineProps<{
  /** Normalized band magnitudes, 0..1 (from useSpectrum). */
  bands: number[];
  /** Color variant: 'input' (teal, default) or 'stream' (purple). */
  variant?: 'input' | 'stream';
}>();
</script>

<template>
  <div :class="['spectrum', variant ?? 'input']" role="img" aria-label="Spectrum analyzer">
    <div
      v-for="(v, i) in bands"
      :key="i"
      class="spectrum-bar"
      :style="{ height: `${Math.max(6, Math.round(v * 100))}%` }"
    ></div>
  </div>
</template>

<style scoped>
.spectrum {
  display: flex;
  align-items: flex-end;
  gap: 2px;
  height: 56px;
}

.spectrum-bar {
  flex: 1 1 0;
  min-height: 2px;
  border-radius: 1px;
  background: var(--spectrum-color);
  transition: height 60ms linear;
}

.spectrum.input {
  --spectrum-color: #1d9e75;
}

.spectrum.stream {
  --spectrum-color: #7f77dd;
}
</style>
