<script setup lang="ts">
import { computed, ref } from 'vue';
import type { ShowDetail } from '../../api';
import { BaseButton, FormInput } from '@shared/components';
import type { ShowPhase } from '../../composables/useShowPhase';

/**
 * Identity header of the redesigned show dashboard: cover thumbnail (with
 * replace), show title as the page h1, type + phase badges, and a single meta
 * line (show # · air date/time · host). Replaces the old "Episode overview"
 * page title + separate identity card.
 *
 * Pure presentation: edit state and all side effects stay on ShowDetailPage.
 */
const props = defineProps<{
  show: ShowDetail;
  phase: ShowPhase;
  editMode: boolean;
  canEdit: boolean;
  uploadingCover: boolean;
  saving: boolean;
  /** Title form value while editing. */
  title: string;
}>();

const emit = defineEmits<{
  'update:title': [value: string];
  'cover-selected': [file: File];
  'start-edit': [];
  save: [];
  cancel: [];
}>();

const coverInput = ref<HTMLInputElement | null>(null);

const showTypeLabel = computed(() => (props.show.show_type || 'unheard').toUpperCase());

const phaseBadge = computed(() => {
  switch (props.phase) {
    case 'broadcast':
      return { label: 'On air', cls: 'onair' };
    case 'wrapup':
      return { label: 'Finished', cls: 'finished' };
    default:
      return { label: 'Upcoming', cls: 'upcoming' };
  }
});

/** "Sun, Jul 12, 2026 at 16:40–18:40" (end segment optional). */
const airLabel = computed(() => {
  const s = props.show;
  if (!s.date) return null;
  const d = new Date(s.date + 'T' + (s.start_time || '00:00') + ':00');
  const day = d.toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
  const verb = props.phase === 'wrapup' ? 'Aired' : '';
  let time = '';
  if (s.start_time) {
    time = ` at ${s.start_time}`;
    if (s.end_time) time += `–${s.end_time}`;
  }
  return `${verb ? verb + ' ' : ''}${day}${time}`;
});

function onCoverChange(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (file) {
    emit('cover-selected', file);
  }
  input.value = '';
}
</script>

<template>
  <div class="show-header">
    <div class="sh-cover">
      <img v-if="show.cover_url" :src="show.cover_url" alt="Cover" class="sh-cover-img" />
      <div v-else class="sh-cover-placeholder">🎙</div>
      <button
        v-if="canEdit"
        type="button"
        class="sh-cover-replace"
        :disabled="uploadingCover"
        @click="coverInput?.click()"
      >
        ⬆ {{ show.cover_url ? 'Replace' : 'Upload' }}
      </button>
      <input
        ref="coverInput"
        type="file"
        accept="image/*"
        class="sh-cover-input"
        @change="onCoverChange"
      />
    </div>

    <div class="sh-body">
      <div class="sh-title-row">
        <FormInput
          v-if="editMode"
          class="sh-title-input"
          :model-value="props.title"
          required
          @update:model-value="emit('update:title', $event)"
        />
        <h1 v-else class="sh-title">{{ show.title }}</h1>
        <span class="sh-badge" :class="`type-${show.show_type || 'unheard'}`">
          {{ showTypeLabel }}
        </span>
        <span class="sh-badge phase" :class="phaseBadge.cls">
          <span v-if="phase === 'broadcast'" class="sh-dot"></span>
          {{ phaseBadge.label }}
        </span>
      </div>
      <p class="sh-meta">
        Show #{{ show.id }}<template v-if="airLabel"> · {{ airLabel }}</template>
        <template v-if="show.host_username"> · Hosted by {{ show.host_username }}</template>
      </p>
    </div>

    <div class="sh-actions">
      <BaseButton v-if="canEdit && !editMode" variant="ghost" size="sm" @click="emit('start-edit')">
        ✎ Edit
      </BaseButton>
      <template v-if="editMode">
        <BaseButton variant="ghost" size="sm" @click="emit('cancel')">Cancel</BaseButton>
        <BaseButton variant="primary" size="sm" :loading="saving" @click="emit('save')">
          Save
        </BaseButton>
      </template>
    </div>
  </div>
</template>

<style scoped>
.show-header {
  display: flex;
  align-items: center;
  gap: var(--spacing-lg);
  padding-bottom: var(--spacing-lg);
  border-bottom: 1px solid var(--color-border);
  margin-bottom: var(--spacing-lg);
}

.sh-cover {
  position: relative;
  flex: 0 0 auto;
  width: 72px;
  height: 72px;
  border-radius: var(--radius-lg);
  overflow: hidden;
  background: var(--color-surface-alt);
}

.sh-cover-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.sh-cover-placeholder {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 1.5rem;
}

.sh-cover-replace {
  position: absolute;
  inset: auto 0 0 0;
  border: none;
  padding: 3px 0;
  font-size: 0.65rem;
  font-family: var(--font-ui);
  background: rgba(0, 0, 0, 0.65);
  color: #fff;
  cursor: pointer;
  opacity: 0;
  transition: opacity var(--transition-fast);
}

.sh-cover:hover .sh-cover-replace,
.sh-cover-replace:focus-visible {
  opacity: 1;
}

.sh-cover-replace:disabled {
  opacity: 0.6;
  cursor: default;
}

.sh-cover-input {
  display: none;
}

.sh-body {
  flex: 1 1 auto;
  min-width: 0;
}

.sh-title-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
}

.sh-title {
  margin: 0;
  font-size: var(--font-size-2xl);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
}

.sh-title-input {
  min-width: 220px;
}

.sh-badge {
  padding: 2px 10px;
  border-radius: var(--radius-full);
  font-family: var(--font-ui);
  font-size: var(--font-size-xs);
  font-weight: 700;
  letter-spacing: 0.06em;
  background: var(--color-surface-alt);
  color: var(--color-text-muted);
}

.sh-badge.type-external {
  background: rgba(59, 130, 246, 0.12);
  color: #3b82f6;
}

.sh-badge.type-brunchtime {
  background: rgba(245, 158, 11, 0.14);
  color: #f59e0b;
}

.sh-badge.phase {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  text-transform: none;
  letter-spacing: 0.02em;
}

.sh-badge.phase.upcoming {
  background: rgba(59, 130, 246, 0.12);
  color: #3b82f6;
}

.sh-badge.phase.onair {
  background: var(--color-error-bg);
  color: var(--color-error);
}

.sh-badge.phase.finished {
  background: var(--color-success-bg);
  color: var(--color-success);
}

.sh-dot {
  width: 8px;
  height: 8px;
  border-radius: var(--radius-full);
  background: var(--color-error);
  animation: sh-pulse 1.4s ease-in-out infinite;
}

@keyframes sh-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.3;
  }
}

.sh-meta {
  margin: var(--spacing-xs) 0 0;
  font-family: var(--font-ui);
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.sh-actions {
  flex: 0 0 auto;
  display: flex;
  gap: var(--spacing-sm);
}

@media (max-width: 640px) {
  .show-header {
    flex-wrap: wrap;
  }
}
</style>
