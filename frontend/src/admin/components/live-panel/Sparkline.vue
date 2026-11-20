<script setup lang="ts">
import { computed } from 'vue';

/**
 * A small line over time for the live panel (#1053): upload in the last
 * minute, listeners in the last 40 minutes. `x` runs 0..1 from left to
 * right; `y` is scaled to `max`. `refLine` draws a dashed level (the upload
 * the stream needs).
 */
const props = withDefaults(
  defineProps<{
    points: { x: number; y: number }[];
    max: number;
    refLine?: number | null;
    /** What the line shows, for screen readers. */
    label: string;
    height?: number;
    tone?: 'teal' | 'yellow';
  }>(),
  { refLine: null, height: 96, tone: 'teal' }
);

const W = 300;
const y = (value: number) => {
  const h = props.height;
  const top = 3;
  const usable = h - top * 2;
  const scaled = props.max > 0 ? Math.min(1, Math.max(0, value / props.max)) : 0;
  return +(h - top - scaled * usable).toFixed(1);
};
const line = computed(() =>
  props.points.map((p) => `${+(Math.max(0, Math.min(1, p.x)) * W).toFixed(1)},${y(p.y)}`).join(' ')
);
const refY = computed(() => (props.refLine === null ? null : y(props.refLine)));
</script>

<template>
  <svg
    class="sparkline"
    :viewBox="`0 0 ${W} ${height}`"
    :style="{ height: `${height}px` }"
    preserveAspectRatio="none"
    role="img"
    :aria-label="label"
  >
    <line
      v-if="refY !== null"
      x1="0"
      :y1="refY"
      :x2="W"
      :y2="refY"
      class="ref"
      vector-effect="non-scaling-stroke"
    />
    <polyline
      v-if="points.length > 1"
      :points="line"
      :class="['value', tone]"
      fill="none"
      vector-effect="non-scaling-stroke"
    />
  </svg>
</template>

<style scoped>
.sparkline {
  display: block;
  width: 100%;
}

.ref {
  stroke: var(--color-primary);
  stroke-width: 2;
  stroke-dasharray: 6 5;
}

.value {
  stroke-width: 3;
  stroke-linejoin: round;
  stroke-linecap: round;
}

.value.teal {
  stroke: #2dd4bf;
}

.value.yellow {
  stroke: var(--color-primary);
}
</style>
