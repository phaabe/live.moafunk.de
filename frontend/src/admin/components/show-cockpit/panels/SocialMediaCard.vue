<script setup lang="ts">
/**
 * The "Social Media" card of the 2×2 show dashboard
 * (docs/stream-rework/show-cockpit-plan.md). An overview of the two Instagram
 * stories and two Telegram posts per show (pre- / post-announcement slots).
 *
 * PLACEHOLDER: the announcement composer + scheduling wiring lands in a later
 * PR (plan PR 4/5). For now this shows the four slots with an inert
 * active/inactive chip so the layout is complete and reviewable.
 */
interface SlotRow {
  label: string;
  active: boolean;
}

const instagram: SlotRow[] = [
  { label: 'Story 1 · Pre-announcement', active: false },
  { label: 'Story 2 · Post-announcement', active: false },
];

const telegram: SlotRow[] = [
  { label: 'Post 1 · Pre-announcement', active: false },
  { label: 'Post 2 · Post-announcement', active: false },
];
</script>

<template>
  <div class="card social-card">
    <h2 class="section-title">Social Media</h2>

    <div class="sm-cols">
      <div class="sm-col">
        <p class="sm-channel">Instagram</p>
        <div v-for="row in instagram" :key="row.label" class="sm-row">
          <span class="sm-label">{{ row.label }}</span>
          <span :class="['sm-chip', { active: row.active }]">
            {{ row.active ? 'Active' : 'Inactive' }}
          </span>
        </div>
      </div>

      <div class="sm-col">
        <p class="sm-channel">Telegram</p>
        <div v-for="row in telegram" :key="row.label" class="sm-row">
          <span class="sm-label">{{ row.label }}</span>
          <span :class="['sm-chip', { active: row.active }]">
            {{ row.active ? 'Active' : 'Inactive' }}
          </span>
        </div>
      </div>
    </div>

    <p class="sm-hint">Scheduling &amp; composing announcements arrives in a later update.</p>
  </div>
</template>

<style scoped>
.social-card {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  min-height: 100%;
}

.section-title {
  margin: 0;
  font-size: var(--font-size-sm);
  font-weight: 700;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}

.sm-cols {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--spacing-lg);
}

.sm-channel {
  margin: 0 0 var(--spacing-sm);
  font-size: var(--font-size-sm);
  font-weight: 700;
  color: var(--color-text);
  border-bottom: 1px solid var(--color-border);
  padding-bottom: var(--spacing-xs);
}

.sm-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-sm);
  padding: var(--spacing-xs) 0;
}

.sm-label {
  font-size: var(--font-size-sm);
  color: var(--color-text);
}

.sm-chip {
  flex: 0 0 auto;
  font-size: 0.7rem;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: var(--radius-full);
  background: var(--color-surface);
  color: var(--color-text-muted);
  border: 1px solid var(--color-border);
}

.sm-chip.active {
  background: rgba(34, 197, 94, 0.12);
  color: #22c55e;
  border-color: transparent;
}

.sm-hint {
  margin: auto 0 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  font-style: italic;
}
</style>
