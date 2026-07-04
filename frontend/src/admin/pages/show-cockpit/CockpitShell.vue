<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import type { ShowPhase } from '../../composables/useShowPhase';

/**
 * Phase-aware "cockpit" dashboard shell — one of the two layouts under A/B
 * (docs/stream-rework/show-cockpit-plan.md). Pure presentation: renders the
 * banner plus all panel slots in a single scroll, grouped under soft-phase
 * headers, emphasising the current phase and auto-scrolling to it. Nothing is
 * locked — every group stays visible and interactive. All data/handlers live
 * on ShowDetailPage.
 *
 * Slots: banner, identity, schedule, media, promotion, wrapup.
 */
const props = defineProps<{
  phase: ShowPhase;
  /** Hide promotion + wrap-up groups (e.g. for guests). */
  hideAftershow?: boolean;
}>();

const RAIL: { key: ShowPhase; label: string }[] = [
  { key: 'prep', label: 'Prep' },
  { key: 'broadcast', label: 'Broadcast' },
  { key: 'wrapup', label: 'Wrap-up' },
];

const rail = computed(() => (props.hideAftershow ? RAIL.slice(0, 2) : RAIL));
const currentIndex = computed(() => RAIL.findIndex((r) => r.key === props.phase));

const prepGroup = ref<HTMLElement | null>(null);
const broadcastGroup = ref<HTMLElement | null>(null);
const wrapupGroup = ref<HTMLElement | null>(null);

function groupEl(phase: ShowPhase): HTMLElement | null {
  if (phase === 'prep') return prepGroup.value;
  if (phase === 'broadcast') return broadcastGroup.value;
  return wrapupGroup.value;
}

function scrollToPhase(phase: ShowPhase) {
  groupEl(phase)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// Bring the current phase into view on load and whenever it advances, so the
// host lands on what matters now without losing access to the rest.
onMounted(() => nextTick(() => groupEl(props.phase)?.scrollIntoView({ block: 'start' })));
watch(
  () => props.phase,
  (p) => nextTick(() => scrollToPhase(p))
);
</script>

<template>
  <div class="cockpit-shell">
    <slot name="banner" />

    <!-- Phase rail -->
    <nav class="phase-rail" aria-label="Show phase">
      <button
        v-for="(step, i) in rail"
        :key="step.key"
        type="button"
        :class="[
          'rail-step',
          { active: step.key === phase, done: i < currentIndex },
        ]"
        @click="scrollToPhase(step.key)"
      >
        <span class="rail-dot"></span>
        <span class="rail-label">{{ step.label }}</span>
      </button>
    </nav>

    <!-- PREP -->
    <section ref="prepGroup" :class="['phase-group', { current: phase === 'prep' }]">
      <p class="phase-heading">Prep</p>
      <slot name="identity" />
      <slot name="schedule" />
      <template v-if="!hideAftershow">
        <slot name="promotion" />
      </template>
    </section>

    <!-- BROADCAST -->
    <section ref="broadcastGroup" :class="['phase-group', { current: phase === 'broadcast' }]">
      <p class="phase-heading">Broadcast</p>
      <slot name="media" />
    </section>

    <!-- WRAP-UP -->
    <section
      v-if="!hideAftershow"
      ref="wrapupGroup"
      :class="['phase-group', { current: phase === 'wrapup' }]"
    >
      <p class="phase-heading">Wrap-up</p>
      <slot name="wrapup" />
    </section>
  </div>
</template>

<style scoped>
.cockpit-shell {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
}

.phase-rail {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  position: sticky;
  top: 0;
  z-index: 5;
  padding: var(--spacing-sm) 0;
  background: var(--color-bg);
}

.rail-step {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
  padding: 4px 10px;
  background: transparent;
  border: none;
  color: var(--color-text-muted);
  font-family: var(--font-family);
  font-size: var(--font-size-xs);
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  cursor: pointer;
}

.rail-step:not(:last-child)::after {
  content: '';
  width: 24px;
  height: 1px;
  background: var(--color-border);
  margin-left: var(--spacing-xs);
}

.rail-dot {
  width: 10px;
  height: 10px;
  border-radius: var(--radius-full);
  border: 2px solid var(--color-border);
}

.rail-step.done .rail-dot {
  background: var(--color-success);
  border-color: var(--color-success);
}

.rail-step.active {
  color: var(--color-primary);
}

.rail-step.active .rail-dot {
  background: var(--color-primary);
  border-color: var(--color-primary);
}

.phase-group {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
  padding-left: var(--spacing-md);
  border-left: 3px solid transparent;
  opacity: 0.6;
  transition: opacity var(--transition-fast), border-color var(--transition-fast);
  scroll-margin-top: 60px;
}

.phase-group.current {
  opacity: 1;
  border-left-color: var(--color-primary);
}

.phase-heading {
  margin: 0;
  font-size: var(--font-size-xs);
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}
</style>
