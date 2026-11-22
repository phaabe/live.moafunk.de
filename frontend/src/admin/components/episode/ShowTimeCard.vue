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
  episodeTimes,
  untilStart,
  type EpisodePhase,
  type Status,
  type StepState,
} from './phase';

// Show time card (step 3, during the show): the countdown, then On air with
// its clock and Back to On Air, then the aired time. A pre-recorded set reads
// "Plays at 12:00". It has no soundcheck: at the start Go Live blinks until
// clicked; without a soundcheck passed in this tab it warns and leads to the
// Soundcheck tab.

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
  /** The soundcheck passed in this tab. */
  tested?: boolean;
  /** The Studio can go live now: soundcheck passed, input on, air time. */
  ready?: boolean;
  /** Go Live is connecting. */
  starting?: boolean;
  /** Why the last Go Live failed. */
  error?: string | null;
}>();
const emit = defineEmits<{
  openStudio: [path: '/stream/live' | '/stream/on-air'];
  goLive: [];
}>();

const prerecorded = computed(() => props.show.stream_mode === 'prerecorded');
const studioOpen = computed(() => {
  const { openMs } = episodeTimes(props.show);
  return openMs !== null && props.now >= openMs;
});
/** Own live show, not on air, Studio open: Go Live, or the soundcheck warning. */
const startable = computed(
  () =>
    !props.readOnly &&
    !prerecorded.value &&
    !props.show.on_air &&
    props.phase !== 'post' &&
    studioOpen.value
);
const warn = computed(() => {
  if (!startable.value || props.ready || props.starting) return '';
  if (!props.tested) return 'No soundcheck in this browser yet. Do it before you go live.';
  if (props.phase === 'live') return 'Your audio input is off. Pick it in the Soundcheck tab.';
  return '';
});
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
      <p v-if="!show.on_air && !prerecorded && !readOnly && ready" class="ep-lead">
        It is air time. Go Live starts your show.
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

    <p v-if="warn" class="warn" role="alert">{{ warn }}</p>
    <p v-if="error && startable" class="field-error" role="alert">{{ error }}</p>

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

    <template v-if="(phase === 'live' && (readOnly ? show.on_air : !prerecorded)) || warn" #actions>
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
      <BaseButton
        v-else-if="warn"
        variant="secondary"
        class="xl"
        @click="emit('openStudio', '/stream/live')"
      >
        <BoardIcon name="mic" />Go to Soundcheck
      </BaseButton>
      <!-- At the start it blinks until clicked. -->
      <BaseButton
        v-else
        :class="['xl', { blink: ready && !starting }]"
        :disabled="!ready"
        :loading="starting"
        @click="emit('goLive')"
      >
        <BoardIcon name="broadcast" />Go Live
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

.warn {
  margin: 0;
  padding: 12px 14px;
  border: 1px solid #5a4416;
  border-left: 4px solid var(--color-warning);
  border-radius: var(--radius-lg);
  background: rgba(245, 158, 11, 0.1);
  color: #fde7b0;
  font-weight: 600;
}

.blink {
  animation: go-live-blink 1.2s steps(1, end) infinite;
}

@keyframes go-live-blink {
  50% {
    opacity: 0.35;
  }
}

@media (prefers-reduced-motion: reduce) {
  .blink {
    animation: none;
  }
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
