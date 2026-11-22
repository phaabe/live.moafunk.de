import { computed, onScopeDispose, ref, type Ref } from 'vue';
import type { ShowDetail } from '../../api';
import { setLength } from '../../episodeRow';
import { inSlot, type SlotReading } from '../../promotion';
import { berlinToUtcDate } from '../../showTime';
import { episodePhase, progress, type EpisodePhase, type StepState } from './phase';

/** How long after the end a missing recording still counts as processing
 * (the backend's own "no recording" alert waits as long). */
export const RECORDING_GRACE_MS = 15 * 60 * 1000;

export type RecordingPill = { state: 'ok' | 'processing' | 'failed'; text: string };

/** Where the recording of an episode is, as the Wrap-up says it. */
export function recordingPill(s: ShowDetail | null, now: number): RecordingPill {
  if (!s) return { state: 'processing', text: 'Recording processing…' };
  if (s.stream_mode === 'prerecorded') {
    const length = s.prerecorded_duration_secs;
    return {
      state: 'ok',
      text: length ? `Recording ready · ${setLength(length)}` : 'Recording ready',
    };
  }
  // While on air (or still saving) an older version is not the final one, and
  // the server refuses Publish.
  if (s.on_air) return { state: 'processing', text: 'Recording processing…' };
  const r = s.latest_recording;
  if (r?.status === 'failed') {
    return {
      state: 'failed',
      text: `No usable recording · ${r.error_message || 'The recording failed.'}`,
    };
  }
  if (r?.download_url) {
    return {
      state: 'ok',
      text: r.duration_ms
        ? `Recording ready · ${setLength(r.duration_ms / 1000)}`
        : 'Recording ready',
    };
  }
  if (r) return { state: 'processing', text: 'Recording processing…' };
  if (s.live_recorded === false) {
    return { state: 'failed', text: 'No usable recording · Record this show was off.' };
  }
  // An End Episode before the scheduled end starts the wait at that moment.
  const ends = [
    s.end_time ? berlinToUtcDate(s.date, s.end_time).getTime() : NaN,
    s.live_ended_at ? Date.parse(s.live_ended_at) : NaN,
  ].filter((ms) => !Number.isNaN(ms));
  const endMs = ends.length ? Math.min(...ends) : null;
  if (endMs !== null && now < endMs + RECORDING_GRACE_MS) {
    return { state: 'processing', text: 'Recording processing…' };
  }
  return { state: 'failed', text: 'No usable recording · Nothing was recorded.' };
}

/**
 * The shared state of the episode page, for its hosts and for every other
 * host: the clock, the phase, the recording and the four steps. Call it in
 * a component's setup; the clock stops with the component.
 */
export function useEpisode(
  show: Readonly<Ref<ShowDetail | null>>,
  slots: Readonly<Ref<SlotReading[]>>,
  /** The Studio soundcheck passed in this tab for this episode. */
  tested: Readonly<Ref<boolean>> = ref(false)
) {
  const now = ref(Date.now());
  const clock = setInterval(() => (now.value = Date.now()), 1000);
  onScopeDispose(() => clearInterval(clock));

  const phase = computed<EpisodePhase>(() =>
    show.value ? episodePhase(show.value, now.value) : 'pre'
  );
  const recording = computed(() => recordingPill(show.value, now.value));
  /** Recording ready only counts after the show (the live capture is not final before). */
  const recordingReady = computed(() => phase.value === 'post' && recording.value.state === 'ok');
  const trackPublic = computed(() => !!show.value?.soundcloud_public);
  const postSent = computed(() => inSlot(slots.value, 'post')?.state === 'sent');
  const social = computed(() => {
    const before = (['pre', 'onair'] as const).map((slot) => inSlot(slots.value, slot));
    return {
      ready: before.filter((a) => !!a && a.state !== 'draft').length,
      sent: before.filter((a) => a?.state === 'sent').length,
    };
  });
  const segments = computed(() =>
    show.value
      ? progress({
          show: show.value,
          now: now.value,
          phase: phase.value,
          tested: tested.value,
          social: social.value,
          steps: {
            recording: recordingReady.value,
            trackPublic: trackPublic.value,
            postSent: postSent.value,
          },
        })
      : []
  );
  /** The state of step `i` (0 = Set up social media … 3 = Publish recording). */
  const step = (i: number): StepState => segments.value[i]?.state ?? 'next';
  return { now, phase, recording, trackPublic, segments, step };
}
