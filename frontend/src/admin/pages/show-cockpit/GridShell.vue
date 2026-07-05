<script setup lang="ts">
/**
 * The show host dashboard shell: a fixed 2×2 grid of cards
 * (docs/stream-rework/show-cockpit-plan.md, per Anton's layout sketch).
 *
 *   ┌── identity (metadata) ──┬── live (phase machine) ──┐
 *   ├── schedule (time+host) ─┴── social (announcements) ─┘
 *
 * Pure layout: the four cards are passed as named slots; all data and handlers
 * live on ShowDetailPage. Collapses to a single column on narrow viewports.
 */
</script>

<template>
  <div class="grid-shell">
    <div class="grid-cell"><slot name="identity" /></div>
    <div class="grid-cell"><slot name="live" /></div>
    <div class="grid-cell"><slot name="schedule" /></div>
    <div class="grid-cell"><slot name="social" /></div>
  </div>
</template>

<style scoped>
.grid-shell {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--spacing-lg);
  align-items: stretch;
}

.grid-cell {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

/* Cards stretch to fill their cell so paired rows stay flush. */
.grid-cell > :deep(.card) {
  flex: 1 1 auto;
  margin-bottom: 0;
}

@media (max-width: 900px) {
  .grid-shell {
    grid-template-columns: 1fr;
  }
}
</style>
