<script setup lang="ts">
import BoardIcon from '../BoardIcon.vue';
import type { Segment } from './phase';

// Progress bar option A (#1052): three equal parts Pre-show › Show time ›
// Post-show. Done parts are green, the current part fills in yellow.

defineProps<{ segments: Segment[] }>();
</script>

<template>
  <div class="ep-progress">
    <ol class="segs" aria-label="Episode progress">
      <li
        v-for="seg in segments"
        :key="seg.name"
        :class="['seg', seg.state]"
        :aria-current="seg.state === 'now' ? 'step' : undefined"
      >
        <span class="bar" aria-hidden="true"
          ><i :style="{ width: `${seg.fill}%` }" :data-fill="seg.fill"></i
        ></span>
        <b
          ><BoardIcon v-if="seg.state === 'done'" name="check" :size="14" />{{ seg.name
          }}<span v-if="seg.state === 'done'" class="sep"> (done)</span></b
        >
        <span class="seg-label">{{ seg.label }}</span>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.ep-progress {
  padding: 16px var(--spacing-lg) 18px;
  border-top: 1px solid #2a2a42;
  background: #0a0a1c;
}

.segs {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 6px 14px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.seg {
  display: grid;
  grid-template-columns: minmax(0, auto) minmax(0, auto);
  align-items: baseline;
  justify-content: space-between;
  gap: 2px 8px;
  min-width: 0;
}

.bar {
  grid-column: 1 / -1;
  height: 10px;
  margin-bottom: 8px;
  border-radius: 5px;
  background: #26263e;
  overflow: hidden;
}

.bar i {
  display: block;
  height: 100%;
  background: var(--color-primary);
}

.seg.done .bar i {
  background: #34c759;
}

.seg b {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  color: #fff;
}

.seg.now b {
  color: var(--color-primary);
}

.seg.done b svg {
  color: #34c759;
}

.seg-label {
  font: 700 12px var(--font-family);
  color: #a8a8bc;
  text-align: right;
  overflow-wrap: anywhere;
}

.seg.now .seg-label {
  color: #e8e2a8;
}

@media (max-width: 640px) {
  .ep-progress {
    padding: 14px var(--spacing-md) 16px;
  }

  .segs {
    gap: 4px;
  }

  .bar {
    height: 8px;
  }

  .seg {
    grid-template-columns: minmax(0, 1fr);
    justify-content: stretch;
    line-height: 1.25;
  }

  .seg b {
    font-size: 12px;
  }

  .seg-label {
    font-size: 11px;
    text-align: left;
  }
}
</style>
