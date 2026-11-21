<script setup lang="ts">
import { computed } from 'vue';
import type { UpcomingEpisode } from '../../api';
import BoardIcon from '../BoardIcon.vue';
import CardAction from './CardAction.vue';
import EpisodeHead from './EpisodeHead.vue';
import { airWindow, dayAndTime, hostsOf, modeAndHosts, startsIn } from './dashboard';

// Dashboard card "My next show" (#1049): the host's next episode, a countdown
// and Open episode, the page's one primary button.
const props = defineProps<{
  episode: UpcomingEpisode | null;
  coverUrl: string | null;
  /** The episodes could not load. */
  error: string | null;
  loaded: boolean;
  /** The clock, in ms since the epoch. */
  now: number;
}>();

const countdown = computed(() => {
  const window = props.episode ? airWindow(props.episode) : null;
  return window ? startsIn(window.start - props.now) : null;
});
</script>

<template>
  <section class="board-card next-show-card">
    <div class="board-card-head">
      <h2 class="board-card-title"><BoardIcon name="clock" />My next show</h2>
    </div>
    <div class="board-card-body">
      <p v-if="error" class="field-error" role="alert">{{ error }}</p>
      <template v-else-if="episode">
        <EpisodeHead
          :cover-url="coverUrl"
          :name="episode.title"
          :line="modeAndHosts(episode.mode_label, hostsOf(episode))"
          :time="dayAndTime(episode)"
        />
        <p v-if="countdown" class="countdown">{{ countdown }}</p>
        <div class="actions">
          <CardAction :to="`/shows/${episode.id}`" icon="chevron" primary>Open episode</CardAction>
        </div>
      </template>
      <p v-else-if="loaded" class="none">You have no upcoming episode.</p>
    </div>
  </section>
</template>

<style scoped>
.next-show-card {
  display: flex;
  flex-direction: column;
}

.board-card-body {
  flex: 1;
}

.countdown {
  margin: 0;
  font: 700 1.25rem var(--font-family);
  color: var(--color-primary);
}

.none {
  margin: 0;
  font: 700 var(--font-size-md) var(--font-family);
  color: #dcdce6;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: auto;
}

@media (max-width: 640px) {
  .countdown {
    font-size: 1.0625rem;
  }
}
</style>
