<script lang="ts">
let nextId = 1;
/** Phones fold the panel's cards (the design's phone artboard). */
export const PHONE_QUERY = '(max-width: 640px)';
</script>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import PanelIcon, { type PanelIconName } from './PanelIcon.vue';

/**
 * A card of the live panel (#1053): a title with its status, then the body.
 * On phones the card folds: a tap on the title opens it. A card with a
 * problem (`alert`) always stays open and gets a red frame.
 */
const props = withDefaults(
  defineProps<{
    title: string;
    icon: PanelIconName;
    /** Short status next to the title, e.g. "Good level". */
    status?: string;
    tone?: 'ok' | 'warn' | 'bad' | 'info' | 'muted';
    /** A problem: red frame, never folded. */
    alert?: boolean;
    /** Open on phones until the host folds it. */
    phoneOpen?: boolean;
    /** The body has no padding (lists that run to the edge). */
    flush?: boolean;
  }>(),
  { status: '', tone: 'muted', alert: false, phoneOpen: false, flush: false }
);

const emit = defineEmits<{
  /** The body is shown (always on desktop). */
  'update:open': [open: boolean];
}>();

const id = `panel-card-${nextId++}`;

const media =
  typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia(PHONE_QUERY)
    : null;
const phone = ref(media?.matches ?? false);
const onMedia = (event: MediaQueryListEvent) => (phone.value = event.matches);
media?.addEventListener?.('change', onMedia);
onUnmounted(() => media?.removeEventListener?.('change', onMedia));

const folded = ref(!props.phoneOpen);
const collapsible = computed(() => phone.value && !props.alert);
const open = computed(() => !collapsible.value || !folded.value);
watch(open, (value) => emit('update:open', value), { immediate: true });
</script>

<template>
  <section :class="['panel-card', { alert }]" :aria-labelledby="`${id}-title`">
    <header class="panel-head" @click="collapsible && (folded = !folded)">
      <h2 :id="`${id}-title`" class="panel-title">
        <button
          v-if="collapsible"
          type="button"
          class="panel-toggle"
          :aria-expanded="open"
          :aria-controls="`${id}-body`"
          @click.stop="folded = !folded"
        >
          <PanelIcon :name="icon" :size="16" />{{ title }}
        </button>
        <template v-else><PanelIcon :name="icon" :size="16" />{{ title }}</template>
      </h2>
      <span v-if="status" :class="['panel-status', tone]"
        ><span class="d" aria-hidden="true"></span>{{ status }}</span
      >
      <PanelIcon v-if="collapsible" name="chevron" :class="['chev', { open }]" :size="18" />
    </header>
    <div v-if="$slots.action" class="panel-action"><slot name="action" /></div>
    <div v-show="open" :id="`${id}-body`" :class="['panel-body', { flush }]">
      <slot />
    </div>
  </section>
</template>

<style scoped>
.panel-card {
  min-width: 0;
  background: #0f0f23;
  border: 1px solid #2a2a42;
  border-radius: var(--radius-xl);
  overflow: hidden;
}

.panel-card.alert {
  border: 2px solid var(--color-error);
}

.panel-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 10px;
  min-height: 52px;
  padding: 8px var(--spacing-md);
  border-bottom: 1px solid #2a2a42;
}

.panel-title {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0;
  font: 700 13px var(--font-family);
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: #ddd;
}

.panel-title :deep(svg) {
  flex: none;
  color: var(--color-primary);
}

.panel-toggle {
  display: inline-flex;
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
  cursor: pointer;
}

.panel-status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: 1px solid #4a4a60;
  border-radius: var(--radius-full);
  color: #c4c4cc;
  font: 700 11px/1.2 var(--font-ui);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  white-space: nowrap;
}

.panel-status .d {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
}

.panel-status.ok {
  border-color: #34c759;
  background: rgba(52, 199, 89, 0.16);
  color: #6ee79a;
}

.panel-status.warn {
  border-color: var(--color-warning);
  background: rgba(245, 158, 11, 0.16);
  color: #fbbf24;
}

.panel-status.bad {
  border-color: var(--color-error);
  background: rgba(239, 68, 68, 0.18);
  color: #ffb3ab;
}

.panel-status.info {
  border-color: #3478f6;
  background: rgba(52, 120, 246, 0.18);
  color: #a3c4ff;
}

.chev {
  flex: none;
  color: #a0a0b4;
  transition: transform var(--transition-fast, 0.15s);
}

.chev.open {
  transform: rotate(180deg);
}

.panel-action {
  padding: var(--spacing-md) var(--spacing-md) 0;
}

.panel-body {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  padding: var(--spacing-md);
}

.panel-body.flush {
  gap: 0;
  padding: 0;
}

@media (max-width: 640px) {
  .panel-head {
    cursor: pointer;
  }

  .panel-action {
    padding: 0;
  }
}
</style>
