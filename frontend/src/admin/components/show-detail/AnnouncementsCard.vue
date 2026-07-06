<script setup lang="ts">
import { computed } from 'vue';
import type { ShowDetail } from '../../api';
import { BaseButton } from '@shared/components';
import type { ShowPhase } from '../../composables/useShowPhase';

/**
 * Announcements checklist of the redesigned show dashboard: the four
 * scheduled slots (IG/TG × pre/post) as an actionable list, plus the two
 * immediate legacy actions the backend supports today (Instagram post,
 * Telegram preview).
 *
 * The slot rows stay inert until the scheduled-announcements backend lands
 * (docs/adr/0001-scheduled-announcements-model.md); the chips and hint make
 * that state explicit instead of hiding it.
 */
const props = defineProps<{
  show: ShowDetail;
  phase: ShowPhase;
  /** Admin-only legacy actions (matches the backend require_admin gate). */
  canPost: boolean;
  sendingTelegramPreview: boolean;
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
}

const slots: SlotRow[] = [
  { icon: '📸', label: 'Story · pre-announcement' },
  { icon: '📸', label: 'Story · post-announcement' },
  { icon: '📱', label: 'Post · pre-announcement' },
  { icon: '📱', label: 'Post · post-announcement' },
];

const chipLabel = computed(() => (props.phase === 'wrapup' ? 'Not sent' : 'Not scheduled'));

const igPosted = computed(() => !!props.show.instagram_posted_at);
</script>

<template>
  <div class="card ann-card">
    <div class="ann-head">
      <h2 class="ann-title">Announcements</h2>
      <span class="ann-count">0 / 4</span>
    </div>

    <div v-for="row in slots" :key="row.label" class="ann-row">
      <span class="ann-label">{{ row.icon }} {{ row.label }}</span>
      <span class="ann-chip">{{ chipLabel }}</span>
    </div>

    <p class="ann-hint">Scheduling &amp; composing announcements arrives in a later update.</p>

    <template v-if="canPost">
      <div class="ann-divider"></div>
      <p class="ann-subtitle">Post now</p>
      <div class="ann-row">
        <span class="ann-label">📸 Instagram</span>
        <BaseButton size="sm" :variant="igPosted ? 'ghost' : 'primary'" @click="emit('compose-instagram')">
          {{ igPosted ? 'Posted ✓ · again?' : 'Compose' }}
        </BaseButton>
      </div>
      <div class="ann-row">
        <span class="ann-label">📱 Telegram</span>
        <BaseButton
          size="sm"
          variant="ghost"
          :loading="sendingTelegramPreview"
          @click="emit('compose-telegram')"
        >
          Compose
        </BaseButton>
      </div>
    </template>
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
  padding: var(--spacing-xs) 0;
  border-bottom: 1px solid var(--color-border);
}

.ann-row:last-child {
  border-bottom: none;
}

.ann-label {
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

.ann-hint {
  margin: var(--spacing-xs) 0 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  font-style: italic;
}

.ann-divider {
  border-top: 1px solid var(--color-border);
  margin: var(--spacing-sm) 0;
}

.ann-subtitle {
  margin: 0 0 var(--spacing-xs);
  font-size: var(--font-size-xs);
  font-weight: 700;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}
</style>
