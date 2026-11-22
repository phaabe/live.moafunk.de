<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue';
import {
  getStreamSocketStats,
  QUALITY_STEPS_KBPS,
  type StreamSocketStats,
  type UploadQualityMode,
} from '@admin/composables';
import LivePanelCard from './LivePanelCard.vue';
import Sparkline from './Sparkline.vue';
import { formatClockTime, type ConnectionVerdict } from './onAir';

/**
 * Connection of the live panel (#1053): the verdict, upload against what the
 * stream needs (last 60 s), upload, buffer, round trip, the chunks the
 * server confirmed, and the upload quality (Auto or a fixed rate). All of it
 * comes from the browser's socket counters and the server's pongs
 * (useUploadHealth, getStreamSocketStats); nothing here changes the socket.
 */
const props = defineProps<{
  /** Null: nothing measured (before the show, or nothing saved). */
  connection: ConnectionVerdict | null;
  uploadKbps: number;
  bufferSeconds: number;
  rttMs: number | null;
  /** Upload kbit/s, one sample a second, oldest first (useUploadHealth). */
  history: number[];
  /** The encoder's target now (kbit/s). */
  targetKbps: number;
  quality: UploadQualityMode;
  /** Auto's last switch. */
  lastStep: { from: number; to: number; at: number } | null;
  /** Values the host's browser saved: no chunk counter (it is this tab's socket), no quality buttons. */
  readOnly?: boolean;
}>();

const emit = defineEmits<{ 'update:quality': [mode: UploadQualityMode] }>();

const status = computed<{ text: string; tone: 'ok' | 'warn' | 'bad' | 'muted' }>(() => {
  switch (props.connection) {
    case null:
      return { text: 'Not measured', tone: 'muted' };
    case 'stable':
      return { text: 'Stable', tone: 'ok' };
    case 'tight':
      return { text: `Tight · ${props.bufferSeconds.toFixed(1)} s buffered`, tone: 'warn' };
    case 'too slow':
      return { text: 'Too slow', tone: 'bad' };
    case 'reconnecting…':
      return { text: 'Reconnecting…', tone: 'warn' };
    case 'lost':
      return { text: 'Lost', tone: 'bad' };
  }
});
const problem = computed(() => props.connection === 'too slow' || props.connection === 'lost');

// ─── Upload against the needed line ─────────────────────────────────────────
const points = computed(() => {
  const n = props.history.length;
  return props.history.map((kbps, i) => ({ x: n > 1 ? i / (n - 1) : 0, y: kbps }));
});
const scaleMax = computed(() => Math.max(props.targetKbps * 1.4, ...props.history, 1) * 1.1);
/** The counters stand still while the socket is down. */
const stale = computed(() => props.connection === 'reconnecting…' || props.connection === 'lost');

// ─── Chunks the server confirmed ────────────────────────────────────────────
// The browser counts what it handed to the socket, the server what arrived
// (sent back with each pong, every 2 s). More than a few seconds between the
// two means audio leaves the browser but does not arrive.
const CONFIRM_LAG_S = 4;
const stats = ref<StreamSocketStats>(getStreamSocketStats());
const sampler = props.readOnly
  ? null
  : setInterval(() => (stats.value = getStreamSocketStats()), 1000);
onUnmounted(() => {
  if (sampler) clearInterval(sampler);
});

const chunks = computed(() => {
  const s = stats.value;
  if (!s.connected) return { value: '—', sub: 'not connected', late: false };
  if (s.serverChunks === 0) return { value: '—', sub: 'waiting for the server', late: false };
  const bytesPerSecond = (props.targetKbps * 1000 * 1.15) / 8;
  const lag = bytesPerSecond > 0 ? Math.max(0, s.bytesQueued - s.serverBytes) / bytesPerSecond : 0;
  return lag > CONFIRM_LAG_S
    ? {
        value: String(s.serverChunks),
        sub: `confirmed · ${Math.round(lag)} s on the way`,
        late: true,
      }
    : { value: String(s.serverChunks), sub: 'confirmed · all arrived', late: false };
});

// ─── Upload quality ─────────────────────────────────────────────────────────
const note = computed(() => {
  const step = props.lastStep;
  if (props.connection === 'too slow' && !(step && step.to < step.from)) {
    return props.quality === 'auto'
      ? `Upload is below what ${props.targetKbps} kbps needs. Auto steps down in a few seconds.`
      : `Upload is below what ${props.targetKbps} kbps needs. Pick a lower quality or Auto.`;
  }
  if (props.quality === 'auto' && step) {
    const way = step.to < step.from ? 'down' : 'up';
    return `Auto stepped ${way} from ${step.from} to ${step.to} kbps at ${formatClockTime(step.at)}. Listeners hear no gap.`;
  }
  return 'Auto steps down when the buffer grows. A switch restarts the encoder for less than 1 s; the relay covers the gap.';
});
</script>

<template>
  <LivePanelCard
    title="Connection"
    icon="wifi"
    :status="status.text"
    :tone="status.tone"
    :alert="problem"
  >
    <div class="upload-line">
      <Sparkline
        :points="points"
        :max="scaleMax"
        :ref-line="targetKbps"
        :label="`Upload in the last minute, now ${uploadKbps} kbps; the stream needs ${targetKbps} kbps`"
      />
      <p class="legend">
        <span><i></i>Upload, last 60 s</span
        ><span><i class="need"></i>Needed for {{ targetKbps }} kbps</span>
      </p>
    </div>
    <dl :class="['tiles', { stale }]">
      <div>
        <dt>Upload</dt>
        <dd>
          <b>{{ uploadKbps }}</b> <span>kbps</span>
        </dd>
      </div>
      <div>
        <dt>Buffer</dt>
        <dd>
          <b>{{ bufferSeconds.toFixed(1) }}</b> <span>seconds</span>
        </dd>
      </div>
      <div>
        <dt>Round trip</dt>
        <dd>
          <b>{{ rttMs ?? '—' }}</b> <span>{{ rttMs === null ? 'no answer yet' : 'ms' }}</span>
        </dd>
      </div>
      <div v-if="!readOnly" :class="{ late: chunks.late }">
        <dt>Chunks</dt>
        <dd>
          <b>{{ chunks.value }}</b> <span>{{ chunks.sub }}</span>
        </dd>
      </div>
    </dl>
    <div class="qual">
      <span id="upload-quality-label" class="qual-label">Upload quality</span>
      <p v-if="readOnly" class="qual-value">
        {{ quality === 'auto' ? `Auto · ${targetKbps} kbps` : `${quality} kbps` }}
      </p>
      <div v-else class="seg" role="group" aria-labelledby="upload-quality-label">
        <button
          type="button"
          :class="{ on: quality === 'auto' }"
          :aria-pressed="quality === 'auto'"
          @click="emit('update:quality', 'auto')"
        >
          Auto · {{ targetKbps }}
        </button>
        <button
          v-for="kbps in QUALITY_STEPS_KBPS"
          :key="kbps"
          type="button"
          :class="{ on: quality === kbps }"
          :aria-pressed="quality === kbps"
          @click="emit('update:quality', kbps)"
        >
          {{ kbps }}
        </button>
      </div>
      <p v-if="!readOnly" class="note">{{ note }}</p>
    </div>
  </LivePanelCard>
</template>

<style scoped>
.upload-line {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  margin: 0;
  font-size: 12px;
  color: #a8a8bc;
}

.legend i {
  display: inline-block;
  width: 18px;
  height: 3px;
  margin-right: 6px;
  vertical-align: middle;
  background: #2dd4bf;
}

.legend i.need {
  background: repeating-linear-gradient(90deg, var(--color-primary) 0 5px, transparent 5px 9px);
}

.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  gap: 10px;
  margin: 0;
}

.tiles > div {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  padding: 12px;
  border: 1px solid #26263e;
  border-radius: 10px;
  background: #0a0a1c;
}

.tiles dt {
  font: 700 11px var(--font-ui);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #a8a8bc;
}

.tiles dd {
  display: flex;
  flex-direction: column;
  margin: 0;
}

.tiles b {
  font: 700 22px/1.1 var(--font-family);
  font-variant-numeric: tabular-nums;
  color: #fff;
}

.tiles dd span {
  font-size: 12px;
  color: #a8a8bc;
}

.tiles.stale {
  opacity: 0.55;
}

.tiles .late b,
.tiles .late dd span {
  color: #fbbf24;
}

.qual {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.qual-value {
  margin: 0;
  font: 700 15px var(--font-family);
  color: #fff;
}

.qual-label {
  font: 700 11px var(--font-ui);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #a8a8bc;
}

.seg {
  display: flex;
  gap: 4px;
  padding: 3px;
  border: 1px solid #2a2a42;
  border-radius: 10px;
  background: #06060f;
}

.seg button {
  flex: 1;
  min-width: 0;
  min-height: 44px;
  padding: 0 4px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: #b0b0c4;
  font: 700 13px var(--font-family);
  white-space: nowrap;
  cursor: pointer;
}

.seg button:hover {
  color: #fff;
}

.seg button.on {
  background: var(--color-primary);
  color: #000;
}

.seg button:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

.seg button:first-child {
  flex: 1.6;
}

.note {
  margin: 0;
  font-size: 12px;
  color: #a8a8bc;
}

@media (max-width: 640px) {
  .tiles {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .tiles b {
    font-size: 18px;
  }
}
</style>
