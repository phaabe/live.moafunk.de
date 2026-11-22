<script setup lang="ts">
import { computed } from 'vue';
import { LISTENER_WINDOW_MS, type ListenerSample } from '@admin/composables/useListeners';
import LivePanelCard from './LivePanelCard.vue';
import Sparkline from './Sparkline.vue';
import { formatClockTime } from './onAir';

/**
 * Listeners of the live panel (#1053): now, the peak and the last 40
 * minutes. Counted by Icecast (GET /api/stream/metrics); peak and line come
 * from this page's polling only, so they start when On Air opened.
 */
const props = defineProps<{
  current: number | null;
  peak: ListenerSample | null;
  samples: ListenerSample[];
  /** Unix ms, ticking: the right end of the line. */
  now: number;
  /** The host listens to the stream: their own player is counted too. */
  monitoring: boolean;
  /** The counts the server took for the episode (read-only panel), not this page's. */
  readOnly?: boolean;
}>();

// The line spans the last 40 minutes, or the time since the first count
// when the page has been open for less.
const points = computed(() => {
  const first = props.samples[0]?.at ?? props.now;
  const from = Math.max(props.now - LISTENER_WINDOW_MS, Math.min(first, props.now - 60_000));
  const span = props.now - from;
  return props.samples.map((s) => ({ x: (s.at - from) / span, y: s.count }));
});
const max = computed(() => Math.max(5, ...props.samples.map((s) => s.count)) * 1.15);
const minutes = LISTENER_WINDOW_MS / 60_000;
</script>

<template>
  <LivePanelCard title="Listeners" icon="headphones">
    <dl class="figures">
      <div class="now">
        <dt>Listeners now</dt>
        <dd>{{ current ?? '—' }}</dd>
      </div>
      <div v-if="peak" class="peak">
        <dt>Peak</dt>
        <dd>{{ peak.count }}</dd>
      </div>
    </dl>
    <p v-if="peak" class="peak-at">Peak at {{ formatClockTime(peak.at) }}</p>
    <Sparkline
      :points="points"
      :max="max"
      :height="64"
      tone="yellow"
      :label="`Listeners in the last ${minutes} minutes, now ${current ?? 'unknown'}`"
    />
    <p class="note">
      <template v-if="readOnly">Counted by the server every 15 s while on air</template
      ><template v-else>Last {{ minutes }} min, since this page opened</template
      ><template v-if="monitoring"> · your own monitor counts as one</template>
    </p>
  </LivePanelCard>
</template>

<style scoped>
.figures {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 8px 28px;
  margin: 0;
}

.figures > div {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.figures dt {
  font: 700 11px var(--font-ui);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #a8a8bc;
}

.figures dd {
  margin: 0;
  font: 700 22px/1.1 var(--font-family);
  font-variant-numeric: tabular-nums;
  color: #fff;
}

.figures .now dd {
  font-size: 52px;
  line-height: 1;
}

.peak-at {
  margin: -8px 0 0;
  font-size: var(--font-size-sm);
  color: #c8c8d4;
}

.note {
  margin: 0;
  font-size: 12px;
  color: #a8a8bc;
}
</style>
