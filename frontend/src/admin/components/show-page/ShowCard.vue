<script setup lang="ts">
import { computed } from 'vue';
import { BaseButton } from '@shared/components';
import type { SeriesDetail } from '../../api';
import BoardIcon from '../BoardIcon.vue';
import { hostsLine, rhythmText } from './showPage';

// The Show's header card on its detail page: cover, name, description, rhythm,
// Instagram handle and hosts. Read-only; its owner edits it on My Show.

const props = defineProps<{
  series: SeriesDetail;
  /** Show "Edit" (the owner, on the host pages). */
  editable: boolean;
}>();
const emit = defineEmits<{ edit: [] }>();

const rhythm = computed(() => rhythmText(props.series));
const hosts = computed(() => hostsLine(props.series.hosts));
</script>

<template>
  <section class="board-card show-card" :aria-label="`Show ${series.name}`">
    <div class="cover show-cover">
      <img
        v-if="series.cover_url"
        :src="series.cover_url"
        :srcset="series.cover_srcset ?? undefined"
        sizes="(max-width: 640px) 100vw, 180px"
        :alt="`Cover of ${series.name}`"
      />
      <span v-else class="cover-name" aria-hidden="true">{{ series.name }}</span>
    </div>
    <div class="show-text">
      <h1 class="page-title show-name">{{ series.name }}</h1>
      <p v-if="series.description" class="description">{{ series.description }}</p>
      <p class="facts">
        <span class="fact"><BoardIcon name="repeat" :size="16" />{{ rhythm }}</span>
        <span v-if="series.instagram_handle" class="fact"
          ><svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <path
              d="M7 2h10a5 5 0 0 1 5 5v10a5 5 0 0 1-5 5H7a5 5 0 0 1-5-5V7a5 5 0 0 1 5-5zM16 11.4A4 4 0 1 1 12.6 8a4 4 0 0 1 3.4 3.4zM17.5 6.5h.01"
            /></svg
          ><span class="sep">Instagram </span>@{{ series.instagram_handle }}</span
        >
        <span v-if="hosts" class="fact"><BoardIcon name="user" :size="16" />{{ hosts }}</span>
      </p>
    </div>
    <BaseButton v-if="editable" variant="secondary" class="edit" @click="emit('edit')"
      ><svg
        width="16"
        height="16"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z" /></svg
      >Edit</BaseButton
    >
  </section>
</template>

<style scoped>
.show-card {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--spacing-lg) var(--spacing-xl);
  padding: var(--spacing-lg);
}

.show-cover {
  width: 180px;
  height: 180px;
}

.cover-name {
  align-self: stretch;
  display: flex;
  align-items: flex-end;
  width: 100%;
  padding: var(--spacing-md);
  background: var(--color-primary);
  color: #000;
  font: 700 1.25rem/1.1 var(--font-family);
  letter-spacing: 0.04em;
  text-transform: uppercase;
  overflow-wrap: break-word;
}

.show-text {
  flex: 1 1 360px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.show-name {
  font-size: 2.75rem;
  line-height: 1.05;
  overflow-wrap: anywhere;
}

.description {
  margin: 0;
  font-size: var(--font-size-md);
  color: #c8c8d4;
  white-space: pre-line;
}

.facts {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 20px;
  margin: 4px 0 0;
}

.fact {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: #d0d0dc;
}

.fact svg {
  flex: none;
  color: var(--color-primary);
}

@media (max-width: 640px) {
  .show-card {
    flex-direction: column;
    align-items: stretch;
    gap: 0;
    padding: 0;
  }

  .show-cover {
    width: 100%;
    height: 150px;
    border-radius: 0;
  }

  .show-text {
    flex: none;
    padding: var(--spacing-md);
  }

  .show-name {
    font-size: 2rem;
  }

  .facts {
    flex-direction: column;
    gap: 6px;
  }

  .edit {
    margin: 0 var(--spacing-md) var(--spacing-md);
  }
}
</style>
