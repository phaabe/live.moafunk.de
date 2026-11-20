<script setup lang="ts">
import { computed } from 'vue';
import { BaseButton } from '@shared/components';
import type { SeriesEpisode } from '../../api';
import BoardIcon from '../BoardIcon.vue';
import { episodeDay } from '../../episodeRow';
import { countdown, episodeTime, nextStep } from './showPage';

// The Show's next episode: big date, countdown and, for its owner, the
// flow's next step and "Open episode" (the page's one yellow button).
// Other hosts open it read-only.

const props = defineProps<{
  episode: SeriesEpisode;
  /** The viewer manages this Show's episodes. */
  mine: boolean;
  /** Clock (ms) for the countdown. */
  now: number;
}>();
const emit = defineEmits<{ open: [] }>();

const day = computed(() => episodeDay(props.episode.date));
const slot = computed(() =>
  [episodeTime(props.episode), props.episode.mode_label].filter(Boolean).join(' · ')
);
const count = computed(() => countdown(props.episode, props.now));
const hint = computed(() => (props.mine ? nextStep(props.episode, props.now) : ''));
</script>

<template>
  <section class="board-card next-episode" aria-labelledby="next-episode-title">
    <div class="next-when">
      <h2 id="next-episode-title" class="next-label">Next episode</h2>
      <b class="next-day">{{ day }}</b>
      <span class="next-slot">{{ slot }}</span>
    </div>
    <div class="next-text">
      <p v-if="count" class="next-count">{{ count }}</p>
      <p v-if="hint" class="next-hint">{{ hint }}</p>
      <div>
        <BaseButton v-if="mine" class="xl open" @click="emit('open')"
          >Open episode<BoardIcon name="chevron"
        /></BaseButton>
        <BaseButton v-else variant="secondary" class="open" @click="emit('open')"
          ><BoardIcon name="eye" :size="16" />View episode</BaseButton
        >
      </div>
    </div>
  </section>
</template>

<style scoped>
.next-episode {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 20px var(--spacing-xl);
  padding: var(--spacing-lg);
  border-color: #5c5620;
  background-color: #14130a;
}

.next-when {
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.next-label {
  margin: 0;
  font: 700 13px var(--font-family);
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: #d8cf7a;
}

.next-day {
  font: 700 3rem/1 var(--font-family);
  color: var(--color-primary);
}

.next-slot {
  font: 700 var(--font-size-md) var(--font-family);
  color: #e8e2a8;
}

.next-text {
  flex: 1 1 280px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.next-count {
  margin: 0;
  font: 700 1.375rem/1.25 var(--font-family);
  color: #fff;
}

.next-hint {
  margin: 0;
  color: #c8c8d4;
}

@media (max-width: 640px) {
  .next-episode {
    flex-direction: column;
    align-items: stretch;
    gap: 12px;
    padding: 18px var(--spacing-md);
  }

  .next-text {
    flex: none;
  }

  .next-day {
    font-size: 2.5rem;
  }

  .next-count {
    font-size: 1.2rem;
  }

  .open {
    width: 100%;
  }
}
</style>
