<script setup lang="ts">
import { computed } from 'vue';
import type { ShowDetail } from '../../api';
import type { ShowPhase } from '../../composables/useShowPhase';

/**
 * Announcements checklist of the redesigned show dashboard: the four
 * scheduled slots (IG/TG × pre/post) as clickable rows that open the
 * matching composer (Instagram preview modal / Telegram composer).
 *
 * Scheduling stays inert until the scheduled-announcements backend lands
 * (docs/adr/0001-scheduled-announcements-model.md); until then composing
 * offers the immediate legacy actions inside the modals, and the chips make
 * the not-yet-scheduled state explicit.
 */
const props = defineProps<{
  show: ShowDetail;
  phase: ShowPhase;
}>();

const emit = defineEmits<{
  /** Open the Instagram composer/preview modal. */
  'compose-instagram': [];
  /** Open the Telegram composer/preview modal. */
  'compose-telegram': [];
}>();

interface SlotRow {
  icon: string;
  label: string;
  channel: 'instagram' | 'telegram';
}

const slots: SlotRow[] = [
  { icon: '📸', label: 'Story · pre-announcement', channel: 'instagram' },
  { icon: '📸', label: 'Story · post-announcement', channel: 'instagram' },
  { icon: '📱', label: 'Post · pre-announcement', channel: 'telegram' },
  { icon: '📱', label: 'Post · post-announcement', channel: 'telegram' },
];

/** Legacy immediate IG post — surfaced on the Instagram rows. */
const igPosted = computed(() => !!props.show.instagram_posted_at);

function chipFor(row: SlotRow): { label: string; sent: boolean } {
  if (row.channel === 'instagram' && igPosted.value) {
    return { label: 'Posted ✓', sent: true };
  }
  return { label: props.phase === 'wrapup' ? 'Not sent' : 'Not scheduled', sent: false };
}

function compose(row: SlotRow) {
  if (row.channel === 'instagram') {
    emit('compose-instagram');
  } else {
    emit('compose-telegram');
  }
}
</script>

<template>
  <div class="card ann-card">
    <div class="ann-head">
      <h2 class="ann-title">Announcements</h2>
      <span class="ann-count">{{ igPosted ? '1' : '0' }} / 4</span>
    </div>

    <button
      v-for="row in slots"
      :key="row.label"
      type="button"
      class="ann-row"
      :title="`Compose ${row.channel} announcement`"
      @click="compose(row)"
    >
      <span class="ann-label">{{ row.icon }} {{ row.label }}</span>
      <span class="ann-chip" :class="{ sent: chipFor(row).sent }">{{ chipFor(row).label }}</span>
      <span class="ann-chevron">›</span>
    </button>

    <p class="ann-hint">
      Click a slot to compose &amp; preview. Scheduling arrives in a later update.
    </p>
  </div>
</template>

<style scoped>
.ann-card {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
  min-height: 100%;
  font-family: var(--font-ui);
}

.ann-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: var(--spacing-xs);
}

.ann-title {
  margin: 0;
  font-size: var(--font-size-sm);
  font-weight: 700;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}

.ann-count {
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  font-variant-numeric: tabular-nums;
}

.ann-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-sm);
  padding: var(--spacing-sm) var(--spacing-xs);
  border: none;
  border-bottom: 1px solid var(--color-border);
  background: none;
  text-align: left;
  cursor: pointer;
  border-radius: var(--radius-sm);
  transition: background var(--transition-fast);
}

.ann-row:hover {
  background: var(--color-surface-hover);
}

.ann-row:last-of-type {
  border-bottom: none;
}

.ann-label {
  flex: 1 1 auto;
  font-family: var(--font-ui);
  font-size: var(--font-size-sm);
  color: var(--color-text);
}

.ann-chip {
  flex: 0 0 auto;
  font-size: var(--font-size-xs);
  font-weight: 600;
  padding: 2px 10px;
  border-radius: var(--radius-full);
  background: var(--color-surface-alt);
  color: var(--color-text-muted);
}

.ann-chip.sent {
  background: var(--color-success-bg);
  color: var(--color-success);
}

.ann-chevron {
  flex: 0 0 auto;
  color: var(--color-text-muted);
  font-size: var(--font-size-lg);
  line-height: 1;
}

.ann-hint {
  margin: var(--spacing-xs) 0 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  font-style: italic;
}
</style>
