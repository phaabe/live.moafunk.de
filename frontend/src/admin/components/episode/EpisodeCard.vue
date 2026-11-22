<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue';
import type { Status, StepState } from './phase';
import { usePhone } from './phone';
import EpisodeIcon from './EpisodeIcon.vue';

// One status card of the episode page (#1052): title, its step (1–4) and its
// status, then the content and the actions. The frame has the colour of its
// step: yellow for the current step, dim yellow when done, gray when next.
// On phones only the current card is open; the others show title and status
// and open on tap.

const props = defineProps<{
  title: string;
  /** A shorter title for phones ("Recording", "Social"). */
  short?: string;
  /** Its step in the heading box: 1 Set up social media … 4 Publish recording. */
  step: number;
  /** Where that step is. */
  state: StepState;
  status: Status;
}>();

const phone = usePhone();
const current = computed(() => props.state === 'now');
const open = ref(current.value);
// A new step opens its card and folds the one before.
watch(current, (now) => (open.value = now));
const bodyId = useId();
</script>

<template>
  <section :class="['ep-card', current ? 'current' : state, { folded: phone && !open }]">
    <div class="ep-card-head">
      <h2 class="ep-card-title">
        <button
          v-if="phone"
          type="button"
          class="ep-card-toggle"
          :aria-expanded="open"
          :aria-controls="bodyId"
          @click="open = !open"
        >
          <slot name="icon" />{{ short ?? title }}
        </button>
        <template v-else><slot name="icon" />{{ title }}</template>
      </h2>
      <span v-if="!phone" class="ep-card-phase">Step {{ step }}</span>
      <span :class="['ep-status', status.tone]"
        ><span class="dot" aria-hidden="true"></span>{{ status.text }}</span
      >
      <EpisodeIcon v-if="phone" name="down" :size="18" class="chev" />
    </div>
    <div v-show="!phone || open" :id="bodyId" class="ep-card-content">
      <div class="ep-card-body"><slot /></div>
      <div v-if="$slots.actions" class="ep-card-actions"><slot name="actions" /></div>
    </div>
  </section>
</template>

<style scoped>
.ep-card {
  --step: #8e8ea4;
  position: relative;
  display: flex;
  flex-direction: column;
  min-width: 0;
  border: 2px solid #34344c;
  border-radius: var(--radius-xl);
  background: var(--color-surface);
  overflow: hidden;
}

.ep-card.done {
  --step: #8f8536;
  border-color: #8f8536;
}

.ep-card.current {
  --step: var(--color-primary);
  border-color: var(--color-primary);
}

.ep-card-head {
  position: relative;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
  min-height: 56px;
  padding: 10px var(--spacing-md);
  border-bottom: 1px solid #2a2a42;
}

.folded .ep-card-head {
  border-bottom: 0;
}

.ep-card-title {
  margin: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  font: 700 13px var(--font-family);
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: #ddd;
}

.ep-card-title :deep(svg) {
  flex: none;
  color: var(--step);
}

/* On phones the whole head opens and folds the card. */
.ep-card-toggle {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 44px;
  padding: 0;
  border: 0;
  background: none;
  color: inherit;
  font: inherit;
  letter-spacing: inherit;
  text-transform: inherit;
  text-align: left;
  cursor: pointer;
}

.ep-card-toggle::after {
  content: '';
  position: absolute;
  inset: 0;
}

.ep-card-toggle:focus-visible {
  outline: none;
}

.ep-card-head:has(.ep-card-toggle:focus-visible) {
  outline: 2px solid var(--color-primary);
  outline-offset: -2px;
}

.ep-card-phase {
  color: var(--step);
  font-size: 12px;
  font-weight: 700;
}

.ep-card-head .ep-status {
  margin-left: auto;
}

.chev {
  flex: none;
  color: #a0a0b4;
  transition: transform 0.15s;
}

.ep-card:not(.folded) .chev {
  transform: rotate(180deg);
}

.ep-card-content {
  display: flex;
  flex: 1;
  flex-direction: column;
}

.ep-card-body {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 14px;
  padding: var(--spacing-md);
}

.ep-card-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  padding: 0 var(--spacing-md) var(--spacing-md);
}

.ep-card-actions:empty {
  display: none;
}

@media (max-width: 640px) {
  .ep-card-actions :deep(.base-button) {
    width: 100%;
  }
}

@media (prefers-reduced-motion: reduce) {
  .chev {
    transition: none;
  }
}
</style>
