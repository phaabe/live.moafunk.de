<script setup lang="ts">
import { computed, toRef, watch } from 'vue';
import { useDbMeter, useSpectrum, dbToLevel, DB_FLOOR } from '@admin/composables';
import { CLIP_DB, QUIET_AVERAGE_DB, SILENT_DB } from '@admin/composables/useSoundcheck';
import { useSilenceAlarm, SILENCE_ALARM_MS } from '@admin/composables/useSilenceAlarm';
import { useStreamMonitor } from '@admin/composables/useStreamMonitor';
import SpectrumBars from '@admin/components/SpectrumBars.vue';
import LivePanelCard from './LivePanelCard.vue';
import PanelIcon from './PanelIcon.vue';

/**
 * Audio of the live panel (#1053): the input, L/R level meters with peak,
 * the input spectrum and the stream's ("what listeners hear", from the
 * public mount), input gain, monitor volume and the silence alarm.
 * Read-only except the two faders and the Change link; the input picker
 * itself comes from the page (slot `input`).
 */
const props = withDefaults(
  defineProps<{
    /** The audio that is sent (after the gain). */
    stream: MediaStream | null;
    /** Post-gain analyser of the capture, for the input spectrum. */
    analyser: AnalyserNode | null;
    inputLabel: string;
    /** The input device is gone: the page shows its picker and notice. */
    inputGone?: boolean;
    /** The page shows the input picker (slot `input`). */
    picker?: boolean;
    /** Input gain, 0..2 (1 = as it comes in). */
    gain: number;
    /** The public stream to monitor; empty hides "What listeners hear". */
    monitorSrc?: string;
  }>(),
  { inputGone: false, picker: false, monitorSrc: '' }
);

const emit = defineEmits<{
  'change-input': [];
  'update:gain': [gain: number];
  /** The host listens to the stream (their monitor is a listener too). */
  monitoring: [playing: boolean];
}>();

const stream = toRef(props, 'stream');
const left = useDbMeter(stream, 0);
const right = useDbMeter(stream, 1);
const { bands: inputBands } = useSpectrum(toRef(props, 'analyser'), 24);
const monitor = props.monitorSrc ? useStreamMonitor(() => props.monitorSrc, 60) : null;
watch(
  () => monitor?.playing.value ?? false,
  (playing) => emit('monitoring', playing)
);

// ─── Silence alarm ──────────────────────────────────────────────────────────
const silence = useSilenceAlarm(() =>
  left.running.value || right.running.value
    ? {
        rmsDb: Math.max(left.db.value, right.db.value),
        peakDb: Math.max(left.peakDb.value, right.peakDb.value),
      }
    : null
);
/** Input gone has its own notice; the alarm would only repeat it. */
const alarm = computed(() => silence.alarm.value && !props.inputGone);
const ALARM_SECONDS = SILENCE_ALARM_MS / 1000;

// The level status holds a little, so a gap between two tracks or one loud
// beat does not flip it.
let loudAt = 0;
let clipAt = 0;
watch(
  silence.last,
  (reading) => {
    const now = Date.now();
    if (reading && reading.rmsDb >= QUIET_AVERAGE_DB) loudAt = now;
    if (reading && reading.peakDb > CLIP_DB) clipAt = now;
  },
  { immediate: true }
);

const status = computed<{ text: string; tone: 'ok' | 'warn' | 'bad' | 'muted' }>(() => {
  const reading = silence.last.value;
  const silent = silence.silentSeconds.value;
  if (props.inputGone) return { text: 'Input gone', tone: 'bad' };
  if (alarm.value) return { text: `No sound · ${silent} s`, tone: 'bad' };
  if (!reading) return { text: 'Not measured', tone: 'muted' };
  const now = Date.now();
  if (now - clipAt < 2000) return { text: 'Too loud', tone: 'warn' };
  if (now - loudAt < 3000) return { text: 'Good level', tone: 'ok' };
  if (reading.rmsDb <= SILENT_DB && silent >= 3)
    return { text: `No sound · ${silent} s`, tone: 'warn' };
  return { text: 'Quiet', tone: 'warn' };
});

// ─── Meters ─────────────────────────────────────────────────────────────────
const SCALE = [-60, -40, -20, -10, -3, 0].map((db) => ({
  label: db < 0 ? `−${-db}` : '0',
  pos: dbToLevel(db) * 100,
}));
const dbText = (db: number) =>
  db <= DB_FLOOR + 0.5 ? '−∞' : `${db < -0.5 ? '−' : ''}${Math.abs(Math.round(db))}`;
const meters = computed(() =>
  [left, right].map((m, i) => ({
    name: i ? 'R' : 'L',
    fill: m.level.value * 100,
    peak: dbToLevel(m.peakDb.value) * 100,
    peakShown: m.peakDb.value > DB_FLOOR + 0.5,
    text: dbText(m.db.value),
  }))
);
const metersLabel = computed(
  () => `Level meters: left ${meters.value[0].text} dB, right ${meters.value[1].text} dB`
);

// ─── Faders ─────────────────────────────────────────────────────────────────
const gainPct = computed(() => Math.round(props.gain * 100));
function onGain(event: Event) {
  emit('update:gain', Number((event.target as HTMLInputElement).value) / 100);
}
</script>

<template>
  <LivePanelCard
    title="Audio"
    icon="mic"
    :status="status.text"
    :tone="status.tone"
    :alert="alarm || inputGone"
    phone-open
  >
    <slot name="input" />
    <p v-if="alarm" class="alarm" role="alert">
      <PanelIcon name="mute" :size="18" />
      <span
        ><b>No sound for {{ silence.silentSeconds.value }} s.</b> Listeners hear silence. Check your
        mixer and cables, or pick another input.</span
      >
    </p>
    <p v-if="!inputGone" class="source">
      Input <b>{{ inputLabel }}</b>
      <button v-if="!picker" type="button" class="link" @click="emit('change-input')">
        Change
      </button>
    </p>

    <div class="meters" role="img" :aria-label="metersLabel">
      <div v-for="m in meters" :key="m.name" class="mrow">
        <span class="ml" aria-hidden="true">{{ m.name }}</span>
        <div class="meter">
          <span class="hot"></span>
          <i :style="{ width: `${m.fill}%` }"></i>
          <span v-if="m.peakShown" class="pk" :style="{ left: `${m.peak}%` }"></span>
        </div>
        <span :class="['db', { off: m.text === '−∞' }]">{{ m.text }} dB</span>
      </div>
      <div class="scale" aria-hidden="true">
        <span v-for="t in SCALE" :key="t.label" :style="{ left: `${t.pos}%` }">{{ t.label }}</span>
      </div>
    </div>

    <div class="specs">
      <div class="spec-box">
        <p class="spl"><b>Your input</b><span>now</span></p>
        <SpectrumBars :bands="inputBands" class="spec" />
      </div>
      <div v-if="monitor" class="spec-box">
        <p class="spl">
          <b>What listeners hear</b><span>~6 s behind</span>
          <button
            type="button"
            class="listen"
            :disabled="monitor.starting.value"
            :aria-pressed="monitor.playing.value"
            @click="monitor.toggle"
          >
            <PanelIcon :name="monitor.playing.value ? 'stop' : 'play'" :size="12" />{{
              monitor.playing.value ? 'Stop' : 'Listen'
            }}
          </button>
        </p>
        <SpectrumBars :bands="monitor.bands.value" variant="stream" class="spec" />
        <p v-if="monitor.playError.value" class="hint bad">{{ monitor.playError.value }}</p>
        <p v-else-if="monitor.corsBlocked.value" class="hint">
          No spectrum from the stream server; you still hear it.
        </p>
        <p v-else-if="!monitor.playing.value" class="hint">
          Listen with headphones: speakers would echo.
        </p>
      </div>
    </div>

    <div class="faders">
      <label class="fad">
        <span class="fad-head"
          ><span class="label">Input gain</span><output>{{ gainPct }} %</output></span
        >
        <input type="range" min="0" max="150" step="5" :value="gainPct" @input="onGain" />
      </label>
      <label v-if="monitor" class="fad">
        <span class="fad-head"
          ><span class="label">Monitor volume</span
          ><output>{{ monitor.monitorVolume.value }} %</output></span
        >
        <input
          v-model.number="monitor.monitorVolume.value"
          type="range"
          min="0"
          max="100"
          step="5"
        />
      </label>
    </div>

    <div class="guard">
      <PanelIcon name="speaker" :size="18" />
      <span class="guard-text"
        >Silence alarm on · warns after {{ ALARM_SECONDS }} s without sound</span
      >
      <span v-if="inputGone" class="chip bad">Input gone</span>
      <span v-else-if="alarm" class="chip bad">No sound</span>
      <span v-else-if="silence.last.value" class="chip ok">Sound OK</span>
      <span v-else class="chip">Not measured</span>
    </div>
  </LivePanelCard>
</template>

<style scoped>
.alarm {
  display: flex;
  gap: 10px;
  margin: 0;
  padding: 12px 14px;
  border-radius: 10px;
  background: #3a1414;
  color: #ffd6d2;
  font-size: var(--font-size-sm);
}

.alarm :deep(svg) {
  flex: none;
  color: #ff8a80;
}

.alarm b {
  color: #fff;
}

.source {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 12px;
  margin: 0;
  font-size: var(--font-size-sm);
  color: #c8c8d4;
}

.source b {
  min-width: 0;
  overflow-wrap: anywhere;
  color: #fff;
}

.link {
  min-height: 32px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--color-primary);
  font: 600 var(--font-size-sm) var(--font-ui);
  text-decoration: underline;
  cursor: pointer;
}

.link:hover {
  color: #fff;
}

.meters {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.mrow {
  display: flex;
  align-items: center;
  gap: 10px;
}

.ml {
  flex: none;
  width: 14px;
  font: 700 13px var(--font-family);
  color: #a8a8bc;
}

.meter {
  position: relative;
  flex: 1;
  min-width: 0;
  height: 16px;
  border-radius: 4px;
  background: #1a1a2e;
  overflow: hidden;
}

.meter i {
  position: absolute;
  inset: 0 auto 0 0;
  background: #34c759;
  transition: width 60ms linear;
}

/* Over −9 dBFS: the red end of the scale. */
.meter .hot {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 85%;
  right: 0;
  background: rgba(239, 68, 68, 0.18);
}

.meter .pk {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 3px;
  margin-left: -1.5px;
  background: var(--color-primary);
}

.db {
  flex: none;
  width: 64px;
  text-align: right;
  font: 700 13px var(--font-family);
  font-variant-numeric: tabular-nums;
  color: #fff;
}

.db.off {
  color: #ff8a80;
}

.scale {
  position: relative;
  height: 1.2em;
  margin: 0 74px 0 24px;
  font: 12px var(--font-family);
  color: #a0a0b4;
}

.scale span {
  position: absolute;
  transform: translateX(-50%);
}

.scale span:first-child {
  transform: none;
}

.scale span:last-child {
  transform: translateX(-100%);
}

.specs,
.faders {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 260px), 1fr));
  gap: var(--spacing-md);
}

.spec-box {
  min-width: 0;
}

.spl {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 8px;
  margin: 0 0 6px;
  font-size: 13px;
  color: #c8c8d4;
}

.spl b {
  margin-right: auto;
  color: #fff;
}

/* Two classes: wins over SpectrumBars' own height. */
.spec-box .spec {
  height: 84px;
  padding: 8px;
  border-radius: var(--radius-lg);
  background: #0a0a1c;
}

.listen {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 32px;
  padding: 0 10px;
  border: 1px solid #4a4a60;
  border-radius: var(--radius-md);
  background: transparent;
  color: #eee;
  font: 700 12px var(--font-family);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  cursor: pointer;
}

.listen[aria-pressed='true'] {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.hint {
  margin: 6px 0 0;
  font-size: 12px;
  color: #a8a8bc;
}

.hint.bad {
  color: #ff8a80;
}

.fad {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.fad-head {
  display: flex;
  justify-content: space-between;
  gap: 8px;
}

.fad .label {
  font: 700 11px var(--font-ui);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #a8a8bc;
}

.fad output {
  font: 700 13px var(--font-family);
  color: #fff;
}

.fad input {
  width: 100%;
  height: 28px;
  margin: 0;
  accent-color: var(--color-primary);
}

.guard {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  padding: 12px 14px;
  border: 1px solid #26263e;
  border-radius: 10px;
  background: #0a0a1c;
  font-size: var(--font-size-sm);
  color: #d4d4de;
}

.guard :deep(svg) {
  flex: none;
  color: var(--color-primary);
}

.guard-text {
  flex: 1 1 200px;
  min-width: 0;
}

.chip {
  margin-left: auto;
  padding: 4px 10px;
  border: 1px solid #4a4a60;
  border-radius: var(--radius-full);
  color: #c4c4cc;
  font: 700 11px/1.2 var(--font-ui);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  white-space: nowrap;
}

.chip.ok {
  border-color: #34c759;
  background: rgba(52, 199, 89, 0.16);
  color: #6ee79a;
}

.chip.bad {
  border-color: var(--color-error);
  background: rgba(239, 68, 68, 0.18);
  color: #ffb3ab;
}

@media (max-width: 640px) {
  .scale {
    display: none;
  }

  .db {
    width: 52px;
  }
}
</style>
