<script setup lang="ts">
import { computed } from 'vue';
import type { ShowDetail } from '../../api';
import { STREAM_AUDIO_BITS_PER_SECOND } from '../../composables/useAudioCapture';
import LivePanel from '../live-panel/LivePanel.vue';
import {
  formatHms,
  formatLeft,
  type PanelAudio,
  type PanelBar,
  type PanelLink,
  type PanelListeners,
} from '../live-panel/onAir';
import { airedSecs, berlinTime, elapsed, episodeTimes, type EpisodePhase } from './phase';

// The live panel on the Show time tab when this browser tab does not send the
// episode: read-only. Its values are what the host's browser saved (every
// 15 s while on air, and at End Episode) and the listener counts the server
// took. Before the show it has no values. The host's own episode puts Go Live
// or Back to On Air into the bar (slot `end`).

/** The host's browser saves every 15 s; older than this, the bar says so. */
const STALE_MS = 45_000;

const props = defineProps<{
  show: ShowDetail;
  phase: EpisodePhase;
  /** Unix ms, ticking. */
  now: number;
  /** The page's own line for the bar (the soundcheck warning, a Go Live error). */
  alert?: { chip: string; text: string } | null;
}>();

const log = computed(() => props.show.studio_log?.on_air ?? null);
const samples = computed(() => props.show.listener_samples ?? []);
const live = computed(() => !!props.show.on_air);

const badge = computed<PanelBar['badge']>(() => {
  if (live.value) return 'live';
  return log.value || props.phase === 'post' ? 'aired' : 'off';
});

/**
 * Time on air. Live: since the host's tab went on air, else by the schedule.
 * After: the recording's length (it spans a reload or a drop, the saved
 * start does not), else from the saved start to the last save.
 */
const clock = computed(() => {
  const l = log.value;
  if (badge.value === 'live') {
    return l ? formatHms((props.now - l.since) / 1000) : elapsed(props.show, props.now).clock;
  }
  if (badge.value === 'aired') {
    const secs = airedSecs(props.show) ?? (l ? (l.at - l.since) / 1000 : null);
    return secs ? formatHms(secs) : '--:--:--';
  }
  return '--:--:--';
});

const staleAlert = computed(() => {
  const l = log.value;
  if (!live.value || !l || props.now - l.at <= STALE_MS) return null;
  return {
    chip: `Last update ${berlinTime(l.at)}`,
    text: 'The host’s browser sent no values since then. They may be out of date.',
  };
});

const bar = computed<PanelBar>(() => {
  const s = props.show;
  const { endMs } = episodeTimes(s);
  let left = '';
  if (live.value && endMs !== null) {
    const secs = Math.floor((endMs - props.now) / 1000);
    left = secs > 0 ? `ends in ${formatLeft(secs)}` : `over by ${formatLeft(-secs)}`;
  }
  const last = samples.value.at(-1);
  return {
    badge: badge.value,
    clock: clock.value,
    rec: live.value,
    screenOn: false,
    listeners: live.value && last ? last.count : null,
    title: s.title,
    slot: s.start_time && s.end_time ? `${s.start_time}–${s.end_time}` : '',
    left,
    soon: false,
    progress: live.value ? elapsed(s, props.now).fill : null,
    alert: props.alert ?? staleAlert.value,
  };
});

const audio = computed<PanelAudio>(() => ({
  stream: null,
  analyser: null,
  inputLabel: log.value?.input ?? '—',
  inputGone: false,
  picker: false,
  gain: log.value?.gain ?? 1,
  // Read-only has no player: the page links the public stream.
  monitorSrc: '',
}));

const link = computed<PanelLink>(() => {
  const l = log.value;
  const target = Math.round(STREAM_AUDIO_BITS_PER_SECOND / 1000);
  if (!l) {
    return {
      connection: null,
      uploadKbps: 0,
      bufferSeconds: 0,
      rttMs: null,
      history: [],
      targetKbps: target,
      quality: 'auto',
      lastStep: null,
    };
  }
  return {
    connection: l.connection,
    uploadKbps: l.upload_kbps,
    bufferSeconds: l.buffer_s,
    rttMs: l.rtt_ms,
    history: l.history,
    targetKbps: l.target_kbps,
    quality: l.quality,
    lastStep: null,
  };
});

const listeners = computed<PanelListeners>(() => {
  const list = samples.value;
  const peak = list.reduce<PanelListeners['peak']>(
    (best, s) => (!best || s.count > best.count ? s : best),
    null
  );
  return { current: live.value ? (list.at(-1)?.count ?? null) : null, peak, samples: list };
});

/** The right end of the listener line: now while on air, else the last count. */
const lineEnd = computed(() =>
  live.value ? props.now : (samples.value.at(-1)?.at ?? log.value?.at ?? props.now)
);
</script>

<template>
  <LivePanel
    embedded
    read-only
    :bar="bar"
    :audio="audio"
    :link="link"
    :listeners="listeners"
    :now="lineEnd"
    :moments="log?.moments ?? []"
    :chat-active="live"
  >
    <template v-if="$slots.end" #end><slot name="end" /></template>
  </LivePanel>
</template>
