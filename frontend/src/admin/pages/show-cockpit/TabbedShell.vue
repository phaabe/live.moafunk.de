<script setup lang="ts">
import { ref, watch } from 'vue';
import type { ShowPhase } from '../../composables/useShowPhase';

/**
 * Tabbed dashboard shell — one of the two layouts under A/B
 * (docs/stream-rework/show-cockpit-plan.md). Pure presentation: it arranges
 * the panels passed via named slots into tabs, and picks a default tab from
 * the show's soft phase. All data/handlers live on ShowDetailPage.
 *
 * Slots: banner, identity, schedule, media, promotion, wrapup.
 */
const props = defineProps<{
  phase: ShowPhase;
  /** Hide promotion + wrap-up tabs (e.g. for guests). */
  hideAftershow?: boolean;
}>();

type TabKey = 'details' | 'broadcast' | 'promotion' | 'wrapup';

const TABS: { key: TabKey; label: string }[] = [
  { key: 'details', label: 'Details' },
  { key: 'broadcast', label: 'Broadcast' },
  { key: 'promotion', label: 'Promotion' },
  { key: 'wrapup', label: 'Wrap-up' },
];

/** Which tab a phase opens on by default. */
const PHASE_TAB: Record<ShowPhase, TabKey> = {
  prep: 'details',
  broadcast: 'broadcast',
  wrapup: 'wrapup',
};

const active = ref<TabKey>(PHASE_TAB[props.phase]);

// Only re-sync the active tab when the phase actually changes (not on every
// unrelated re-render), so a user's manual tab choice isn't yanked away.
watch(
  () => props.phase,
  (p) => {
    if (props.hideAftershow && (PHASE_TAB[p] === 'promotion' || PHASE_TAB[p] === 'wrapup')) {
      active.value = 'details';
    } else {
      active.value = PHASE_TAB[p];
    }
  }
);

const visibleTabs = () =>
  props.hideAftershow ? TABS.filter((t) => t.key === 'details' || t.key === 'broadcast') : TABS;
</script>

<template>
  <div class="tabbed-shell">
    <div class="tab-bar" role="tablist">
      <button
        v-for="t in visibleTabs()"
        :key="t.key"
        type="button"
        role="tab"
        :aria-selected="active === t.key"
        :class="['tab', { active: active === t.key }]"
        @click="active = t.key"
      >
        {{ t.label }}
      </button>
    </div>

    <div class="tab-panel" role="tabpanel">
      <template v-if="active === 'details'">
        <slot name="identity" />
        <slot name="schedule" />
      </template>

      <template v-else-if="active === 'broadcast'">
        <slot name="banner" />
        <slot name="media" />
      </template>

      <template v-else-if="active === 'promotion'">
        <slot name="promotion" />
      </template>

      <template v-else-if="active === 'wrapup'">
        <slot name="wrapup" />
      </template>
    </div>
  </div>
</template>

<style scoped>
.tab-bar {
  display: flex;
  gap: var(--spacing-xs);
  border-bottom: 1px solid var(--color-border);
  margin-bottom: var(--spacing-lg);
  overflow-x: auto;
}

.tab {
  padding: var(--spacing-sm) var(--spacing-lg);
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  color: var(--color-text-muted);
  font-family: var(--font-family);
  font-size: var(--font-size-sm);
  font-weight: 600;
  letter-spacing: 0.03em;
  cursor: pointer;
  white-space: nowrap;
  transition: color var(--transition-fast), border-color var(--transition-fast);
}

.tab:hover {
  color: var(--color-text);
}

.tab.active {
  color: var(--color-primary);
  border-bottom-color: var(--color-primary);
}

.tab-panel {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
}
</style>
