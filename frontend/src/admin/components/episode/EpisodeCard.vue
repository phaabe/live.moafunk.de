<script setup lang="ts">
import { ref, useId, watch } from 'vue';
import type { Status } from './phase';
import { usePhone } from './phone';
import EpisodeIcon from './EpisodeIcon.vue';

// One status card of the episode page (#1052): title, the phase it belongs
// to and its status, then the content and the actions. The card of the
// current phase has a yellow frame. On phones only that card is open; the
// others show title and status and open on tap.

const props = defineProps<{
  title: string;
  /** A shorter title for phones ("Recording", "Social"). */
  short?: string;
  /** "Pre-show", "During the show", "Post-show", "All phases". */
  phase: string;
  status: Status;
  /** The card of the current phase. */
  current?: boolean;
}>();

const phone = usePhone();
const open = ref(!!props.current);
// A new phase opens its card and folds the one before.
watch(
  () => props.current,
  (current) => (open.value = !!current)
);
const bodyId = useId();
</script>

<template>
  <section :class="['ep-card', { current, folded: phone && !open }]">
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
      <span v-if="!phone" class="ep-card-phase">{{ phase }}</span>
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
  position: relative;
  display: flex;
  flex-direction: column;
  min-width: 0;
  border: 1px solid #2a2a42;
  border-radius: var(--radius-xl);
  background: var(--color-surface);
  overflow: hidden;
}

.ep-card.current {
  border: 2px solid var(--color-primary);
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
  color: var(--color-primary);
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
  color: #a0a0b4;
  font-size: 12px;
  font-weight: 600;
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
