<script setup lang="ts">
import { computed } from 'vue';
import { BaseButton } from '@shared/components';
import type { ShowDetail } from '../../api';
import { setLength } from '../../episodeRow';
import BoardIcon from '../BoardIcon.vue';
import EpisodeCard from './EpisodeCard.vue';
import EpisodeIcon from './EpisodeIcon.vue';
import {
  airedSecs,
  berlinTime,
  elapsed,
  untilStart,
  type EpisodePhase,
  type Status,
  type StepState,
} from './phase';

// Show time card (step 3, during the show): the countdown, then On air with
// its clock and Back to On Air, then the aired time. A pre-recorded set reads
// "Plays at 12:00".

const LISTEN = 'https://live.moafunk.de';

const props = defineProps<{
  show: ShowDetail;
  phase: EpisodePhase;
  /** Where the Show time step is. */
  state: StepState;
  now: number;
  /** Another host's episode: status and the public stream link only. */
  readOnly?: boolean;
  busy?: boolean;
}>();
const emit = defineEmits<{ openStudio: [path: '/stream/live' | '/stream/on-air'] }>();

const prerecorded = computed(() => props.show.stream_mode === 'prerecorded');
const air = computed(() => elapsed(props.show, props.now));
const aired = computed(() => airedSecs(props.show));
const endedAt = computed(() =>
  props.show.live_ended_at ? berlinTime(Date.parse(props.show.live_ended_at)) : null
);
const until = computed(() => untilStart(props.show, props.now));

const status = computed<Status>(() => {
  if (props.show.on_air) return { tone: 'red', text: 'On air' };
  if (props.phase === 'pre') return { tone: 'blue', text: `Starts ${until.value}` };
  if (props.phase === 'live')
    return prerecorded.value
      ? { tone: 'blue', text: 'Playing' }
      : { tone: 'amber', text: 'Not on air yet' };
  if (props.show.episode_status === 'finished' || aired.value || props.show.live_ended_at)
    return { tone: 'green', text: 'Aired' };
  return { tone: 'grey', text: 'Ended' };
});
</script>

<template>
  <EpisodeCard title="Show time" :step="3" :state="state" :status="status">
    <template #icon><BoardIcon name="broadcast" :size="16" /></template>

    <!-- On air (or playing): the clock since the start. -->
    <template v-if="phase === 'live'">
      <div class="big">
        <b>{{ air.clock }}</b
        ><span>since {{ show.start_time }} · until {{ show.end_time }}</span>
      </div>
      <p v-if="!show.on_air && !prerecorded" class="ep-lead">
        The show started at {{ show.start_time }}. Open the Studio to go live.
      </p>
      <p v-else-if="prerecorded" class="ep-lead">
        The uploaded set plays until {{ show.end_time }}.
      </p>
    </template>
    <!-- After the show: how long it aired. -->
    <template v-else-if="phase === 'post'">
      <div class="big">
        <b>{{ aired ? setLength(aired) : `${show.start_time}–${show.end_time}` }}</b
        ><span>{{ aired ? 'on air' : 'scheduled' }}</span>
      </div>
    </template>
    <!-- Before: when it starts. -->
    <template v-else>
      <div class="big">
        <b>{{ show.start_time }}</b
        ><span>until {{ show.end_time }}</span>
      </div>
      <p v-if="prerecorded" class="ep-lead">Plays at {{ show.start_time }}.</p>
    </template>

    <dl class="ep-kv">
      <div v-if="phase === 'post'">
        <dt>Time</dt>
        <dd>{{ show.start_time }}–{{ show.end_time }}</dd>
      </div>
      <div v-if="phase === 'post' && endedAt">
        <dt>Ended</dt>
        <dd>{{ endedAt }}</dd>
      </div>
      <div v-if="phase === 'live'">
        <dt>Length</dt>
        <dd>{{ air.length }}</dd>
      </div>
      <div>
        <dt>Mode</dt>
        <dd>{{ prerecorded ? 'Pre-recorded set' : 'Live from the Studio' }}</dd>
      </div>
      <div v-if="phase !== 'post'">
        <dt>Listen</dt>
        <dd><a :href="LISTEN" target="_blank" rel="noopener">live.moafunk.de</a></dd>
      </div>
    </dl>

    <template v-if="phase === 'live' && (readOnly ? show.on_air : !prerecorded)" #actions>
      <a v-if="readOnly" class="listen" :href="LISTEN" target="_blank" rel="noopener"
        >Listen live<EpisodeIcon name="external"
      /></a>
      <BaseButton
        v-else-if="show.on_air"
        class="xl"
        :loading="busy"
        @click="emit('openStudio', '/stream/on-air')"
      >
        <BoardIcon name="broadcast" />Back to On Air
      </BaseButton>
      <BaseButton v-else class="xl" :loading="busy" @click="emit('openStudio', '/stream/live')">
        <BoardIcon name="mic" />Open Studio
      </BaseButton>
    </template>
  </EpisodeCard>
</template>

<style scoped>
.big {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px 12px;
}

.big b {
  font: 700 2.5rem/1 var(--font-family);
  font-variant-numeric: tabular-nums;
  color: #fff;
}

.big span {
  font: 700 15px var(--font-family);
  color: #b0b0c4;
}

.listen {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 52px;
  padding: 0 24px;
  border: 1px solid #4a4a60;
  border-radius: var(--radius-lg);
  color: #eee;
  font: 700 15px/1 var(--font-family);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  text-decoration: none;
}

.listen:hover {
  border-color: #fff;
}

@media (max-width: 640px) {
  .big b {
    font-size: 2rem;
  }

  .listen {
    width: 100%;
  }
}
</style>
