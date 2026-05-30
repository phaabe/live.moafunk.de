<script setup lang="ts">
import { toRef, computed } from 'vue';
import { useDbMeter, dbToLevel, DB_FLOOR } from '@admin/composables';

const props = withDefaults(
  defineProps<{
    /** The capture stream to meter. Pass the ref's value; reactivity is preserved. */
    mediaStream: MediaStream | null;
    /** Optional caption shown above the meter. */
    label?: string;
  }>(),
  {
    label: 'Audio Level',
  }
);

const streamRef = toRef(props, 'mediaStream');
const { db, peakDb, level } = useDbMeter(streamRef);

// Scale ticks (dBFS). Positioned along the bar via dbToLevel().
const TICKS = [-60, -48, -36, -24, -12, -6, 0];
const ticks = TICKS.map((value) => ({ value, pos: dbToLevel(value) * 100 }));

const fillPct = computed(() => level.value * 100);
const peakPct = computed(() => dbToLevel(peakDb.value) * 100);
// Hide the peak marker when there's effectively no signal.
const peakVisible = computed(() => peakDb.value > DB_FLOOR + 0.5);

const dbText = computed(() => {
  if (db.value <= DB_FLOOR + 0.5) return '−∞';
  return `${db.value <= 0 ? '' : '+'}${db.value.toFixed(1)}`;
});
</script>

<template>
  <div class="db-meter">
    <div class="db-meter-head">
      <span class="db-meter-label">{{ label }}</span>
      <span class="db-meter-readout">{{ dbText }} dB</span>
    </div>

    <div class="db-meter-track">
      <!-- Lit portion masked by a dark overlay from the current level onward -->
      <div class="db-meter-unlit" :style="{ left: `${fillPct}%` }"></div>
      <!-- Peak-hold marker -->
      <div v-if="peakVisible" class="db-meter-peak" :style="{ left: `${peakPct}%` }"></div>
    </div>

    <div class="db-meter-scale">
      <span v-for="t in ticks" :key="t.value" class="db-meter-tick" :style="{ left: `${t.pos}%` }">
        {{ t.value }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.db-meter {
  width: 100%;
}

.db-meter-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: var(--spacing-sm);
}

.db-meter-label {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.db-meter-readout {
  font-size: var(--font-size-sm);
  font-variant-numeric: tabular-nums;
  color: var(--color-text);
}

.db-meter-track {
  position: relative;
  height: 14px;
  border-radius: var(--radius-sm);
  overflow: hidden;
  /* Full-scale gradient: green up to -12 dB, yellow -12..-3, red -3..0. */
  background: linear-gradient(
    90deg,
    #22c55e 0%,
    #22c55e 80%,
    #eab308 80%,
    #eab308 95%,
    #ef4444 95%,
    #ef4444 100%
  );
}

/* Darkens the un-lit portion of the scale to the right of the current level. */
.db-meter-unlit {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  background: var(--color-surface-alt);
  opacity: 0.82;
  transition: left 60ms linear;
}

.db-meter-peak {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 2px;
  margin-left: -1px;
  background: var(--color-text);
  box-shadow: 0 0 4px rgba(255, 255, 255, 0.6);
}

.db-meter-scale {
  position: relative;
  height: 1.2em;
  margin-top: 2px;
}

.db-meter-tick {
  position: absolute;
  transform: translateX(-50%);
  font-size: 0.625rem;
  font-variant-numeric: tabular-nums;
  color: var(--color-text-muted);
  white-space: nowrap;
}

/* Keep the edge labels inside the track bounds. */
.db-meter-tick:first-child {
  transform: translateX(0);
}

.db-meter-tick:last-child {
  transform: translateX(-100%);
}
</style>
