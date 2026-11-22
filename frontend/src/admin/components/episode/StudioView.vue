<script setup lang="ts">
import { computed } from 'vue';
import type { ShowDetail } from '../../api';
import { episodeDay } from '../../episodeRow';
import LiveSetupTest from '../LiveSetupTest.vue';
import { berlinTime, type EpisodePhase } from './phase';

// The Studio card read-only (Soundcheck tab): for another host's episode, and
// for the host's own once it went live. The input and the checks the host's
// browser saved at Go Live, or "Not checked yet" before. It never asks for an
// audio input and has no buttons.

const props = defineProps<{ show: ShowDetail; phase: EpisodePhase }>();

const log = computed(() => props.show.studio_log?.soundcheck ?? null);

/** The pill: when the saved soundcheck is from, or when the show starts. */
const pill = computed(() => {
  const s = props.show;
  if (log.value) {
    return {
      label: 'Soundcheck · saved at Go Live',
      clock: `${episodeDay(s.date)} · ${berlinTime(log.value.at)}`,
    };
  }
  if (props.phase === 'pre') {
    return { label: 'Soundcheck · show starts', clock: `${episodeDay(s.date)} · ${s.start_time}` };
  }
  return { label: 'Soundcheck · nothing saved', clock: '—' };
});
</script>

<template>
  <div class="studio host-page embedded" aria-label="Studio, read-only">
    <p class="pill status-banner warn countdown">
      <span class="countdown-label"
        ><span class="dot" aria-hidden="true"></span>{{ pill.label }}</span
      ><span class="countdown-clock">{{ pill.clock }}</span>
    </p>
    <LiveSetupTest studio read-only :log="log" />
  </div>
</template>

<style scoped>
/* As the Studio's countdown pill (FlowLive). */
.countdown {
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
}

.countdown-label {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font: 700 12px var(--font-ui);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #fde7b0;
}

.countdown-clock {
  font: 700 3rem/1 var(--font-family);
  font-variant-numeric: tabular-nums;
  color: #fff;
}

@media (max-width: 640px) {
  .countdown-clock {
    font-size: 2.25rem;
  }
}
</style>
