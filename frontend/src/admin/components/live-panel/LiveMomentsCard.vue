<script lang="ts">
/** Typing here must not mark a moment (the chat reply, the input picker). */
const TYPING = 'input, textarea, select, [contenteditable=""], [contenteditable="true"]';

/** Is this key press the M shortcut? Pure — unit tested through the page. */
export function isMarkKey(event: KeyboardEvent): boolean {
  if (event.key !== 'm' && event.key !== 'M') return false;
  if (event.ctrlKey || event.metaKey || event.altKey || event.repeat) return false;
  const target = event.target;
  if (target instanceof Element && target.closest(TYPING)) return false;
  return !(target instanceof HTMLElement && target.isContentEditable);
}
</script>

<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue';
import LivePanelCard from './LivePanelCard.vue';
import PanelIcon from './PanelIcon.vue';
import { formatHms, type Moment } from './onAir';

/**
 * Moments of the live panel (#1053): Mark moment (or the M key, while no
 * text field has the focus) keeps the on-air timecode. The key works only
 * while On Air is open. Moments stay in this tab: Wrap-up does not get them
 * yet.
 */
const props = defineProps<{ moments: Moment[] }>();
const emit = defineEmits<{ mark: [] }>();

/** Told to screen readers after a mark. */
const announced = ref('');
watch(
  () => props.moments.length,
  (count, before) => {
    const moment = props.moments.at(-1);
    if (moment && count > before) announced.value = `Moment marked at ${formatHms(moment.at)}`;
  }
);

function mark() {
  emit('mark');
}

function onKeydown(event: KeyboardEvent) {
  if (event.defaultPrevented || !isMarkKey(event)) return;
  event.preventDefault();
  mark();
}

onMounted(() => window.addEventListener('keydown', onKeydown));
onUnmounted(() => window.removeEventListener('keydown', onKeydown));
</script>

<template>
  <LivePanelCard
    title="Moments"
    icon="bookmark"
    :status="moments.length ? `${moments.length} marked` : ''"
    tone="info"
  >
    <template #action>
      <button
        type="button"
        class="mark-button"
        aria-label="Mark moment"
        aria-keyshortcuts="M"
        @click="mark"
      >
        <PanelIcon name="plus" :size="18" /><span class="long">Mark moment</span
        ><span class="short" aria-hidden="true">Moment</span><kbd aria-hidden="true">M</kbd>
      </button>
    </template>
    <ul v-if="moments.length" class="mom">
      <li v-for="(moment, i) in moments" :key="moment.markedAt">
        <b>{{ formatHms(moment.at) }}</b
        ><span>Moment {{ i + 1 }}</span>
      </li>
    </ul>
    <p class="note">
      <template v-if="!moments.length"
        >Press M or Mark moment to keep the time of a good part. </template
      >Moments stay in this browser tab; Wrap-up does not use them yet.
    </p>
    <p class="sr" aria-live="polite">{{ announced }}</p>
  </LivePanelCard>
</template>

<style scoped>
.mark-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  width: 100%;
  min-height: 52px;
  border: 1px solid #4a4a60;
  border-radius: var(--radius-lg);
  background: transparent;
  color: #eee;
  font: 700 15px var(--font-family);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  cursor: pointer;
}

.mark-button:hover {
  border-color: var(--color-primary);
  color: #fff;
}

.mark-button:active {
  background: #1d1d36;
}

.mark-button:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

.mark-button .short {
  display: none;
}

.mark-button kbd {
  padding: 1px 6px;
  border: 1px solid #6a6a80;
  border-radius: 4px;
  font: 700 12px var(--font-family);
  color: #c8c8d4;
}

.mom {
  list-style: none;
  margin: 0;
  padding: 0;
}

.mom li {
  display: flex;
  gap: 12px;
  padding: 10px 0;
  border-top: 1px solid #1e1e34;
  font-size: var(--font-size-sm);
  color: #d4d4de;
}

.mom li:first-child {
  border-top: 0;
  padding-top: 0;
}

.mom b {
  font: 700 14px var(--font-family);
  font-variant-numeric: tabular-nums;
  color: var(--color-primary);
}

.note {
  margin: 0;
  font-size: 12px;
  color: #a8a8bc;
}

.sr {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: 0;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}

/* Phones: Mark moment sits in the bottom dock, left of End Episode. */
@media (max-width: 640px) {
  .mark-button {
    position: fixed;
    left: 16px;
    right: calc(50% + 5px);
    bottom: calc(16px + env(safe-area-inset-bottom, 0px));
    z-index: 21;
    width: auto;
    min-height: 56px;
    background: #05050f;
    font-size: 14px;
  }

  .mark-button .long,
  .mark-button kbd {
    display: none;
  }

  .mark-button .short {
    display: inline;
  }
}
</style>
