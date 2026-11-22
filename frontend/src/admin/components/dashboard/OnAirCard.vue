<script setup lang="ts">
import { computed } from 'vue';
import type { UpcomingEpisode } from '../../api';
import CardAction from './CardAction.vue';
import EpisodeHead from './EpisodeHead.vue';
import { LISTEN_URL, dayAndTime, hostsOf, isMine, progressOf, timeRange } from './dashboard';

// Dashboard card "On air" (#1049): the episode on air now, with its clock and
// progress and listener count. The host's own episode opens On Air; any other
// one Listen live.
// With nothing on air it says so and names the next episode.
const props = defineProps<{
  /** The stream is live. */
  active: boolean;
  /** The episode on air, when the stream is live and an episode airs now. */
  episode: UpcomingEpisode | null;
  /** The next episode of any host, for the card when nothing is on air. */
  next: UpcomingEpisode | null;
  coverUrl: string | null;
  coverSrcset?: string | null;
  username: string | undefined;
  /** The clock, in ms since the epoch. */
  now: number;
  /** Listeners now, or null while the count cannot be read. */
  listeners: number | null;
}>();

const hosts = computed(() => (props.episode ? hostsOf(props.episode) : []));
const mine = computed(() => isMine(hosts.value, props.username));
const progress = computed(() => (props.episode ? progressOf(props.episode, props.now) : null));
const line = computed(() =>
  props.episode ? [...hosts.value, props.episode.mode_label].join(' · ') : ''
);
const nextLine = computed(() => {
  const e = props.next;
  if (!e) return null;
  return [e.show_name, dayAndTime(e), ...hostsOf(e)].join(' · ');
});
</script>

<template>
  <section :class="['board-card', 'on-air-card', { live: active }]">
    <div class="board-card-head">
      <h2 class="board-card-title">
        <span :class="['pulse', { off: !active }]" aria-hidden="true"></span>On air
      </h2>
    </div>
    <div class="board-card-body">
      <template v-if="active && episode">
        <EpisodeHead
          :cover-url="coverUrl"
          :cover-srcset="coverSrcset"
          :name="episode.show_name"
          :line="line"
          :time="timeRange(episode) ?? ''"
        />
        <template v-if="progress">
          <div class="air-clock-row">
            <p class="air-clock">{{ progress.elapsed }}</p>
            <span v-if="listeners !== null" class="air-listeners">{{ listeners }} listening</span>
          </div>
          <div
            v-if="progress.percent !== null"
            class="air-progress"
            role="progressbar"
            aria-label="Show progress"
            aria-valuemin="0"
            aria-valuemax="100"
            :aria-valuenow="progress.percent"
          >
            <i :style="{ width: `${progress.percent}%` }"></i>
          </div>
        </template>
        <div class="actions">
          <CardAction
            v-if="mine"
            :to="{ path: '/stream/on-air', query: { show: String(episode.id) } }"
            icon="broadcast"
            >Back to On Air</CardAction
          >
          <CardAction v-else :href="LISTEN_URL" icon="headphones">Listen live</CardAction>
        </div>
      </template>
      <template v-else-if="active">
        <p class="air-note">Live now on live.moafunk.de.</p>
        <div class="actions">
          <CardAction :href="LISTEN_URL" icon="headphones">Listen live</CardAction>
        </div>
      </template>
      <template v-else>
        <p class="air-note">Nothing on air right now.</p>
        <p v-if="nextLine" class="air-next">Next: {{ nextLine }}</p>
      </template>
    </div>
  </section>
</template>

<style scoped>
.on-air-card {
  display: flex;
  flex-direction: column;
}

.host-page .on-air-card.live {
  border-color: #5a2424;
  background: #140a0d;
}

.host-page .on-air-card.live .board-card-head {
  border-bottom-color: #3a1c1c;
}

.host-page .on-air-card.live .board-card-title {
  color: #ffb3ab;
}

.pulse {
  flex: none;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--color-error);
  box-shadow: 0 0 0 4px rgba(239, 68, 68, 0.25);
}

.pulse.off {
  background: #5c5c78;
  box-shadow: none;
}

.board-card-body {
  flex: 1;
}

.air-clock {
  margin: 0;
  font: 700 2.125rem/1 var(--font-family);
  font-variant-numeric: tabular-nums;
  color: #fff;
}

.air-clock-row {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 12px;
}

.air-listeners {
  color: #ffb3ab;
  font-size: var(--font-size-sm);
  font-variant-numeric: tabular-nums;
}

.air-progress {
  height: 6px;
  overflow: hidden;
  border-radius: 3px;
  background: #3a1c1c;
}

.air-progress i {
  display: block;
  height: 100%;
  background: var(--color-error);
}

.air-note {
  margin: 0;
  font: 700 var(--font-size-md) var(--font-family);
  color: #dcdce6;
}

.air-next {
  margin: 0;
  color: #b0b0c4;
  font-size: var(--font-size-sm);
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: auto;
}

@media (max-width: 640px) {
  .air-clock {
    font-size: 1.75rem;
  }
}
</style>
