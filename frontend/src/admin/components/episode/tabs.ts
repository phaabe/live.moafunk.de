import type { InjectionKey } from 'vue';
import type { ShowDetail } from '../../api';
import type { SoundcheckStep, SoundcheckStepState } from '../../composables';
import { fmtClock } from '../../composables/useShowClocks';
import { setLength } from '../../episodeRow';
import { inSlot, slotState, type SlotReading } from '../../promotion';
import {
  airedSecs,
  elapsed,
  episodeTimes,
  timeRange,
  untilStart,
  type EpisodePhase,
} from './phase';
import type { RecordingPill } from './useEpisode';

/**
 * The four tab tiles of the episode page (#1052, icon draft): one icon per
 * item and a small badge for its status. Every status has its own badge
 * shape, so it does not rest on colour alone.
 */
/** Provided by the episode page: its cards are tab content, so they never fold on phones. */
export const EPISODE_TABS: InjectionKey<boolean> = Symbol('episodeTabs');

export type Badge =
  | 'todo' // not set up
  | 'sched' // scheduled
  | 'approve' // needs approval
  | 'lock' // waits for the public track, or a private track
  | 'ok' // sent, passed, ready, public
  | 'fail' // failed
  | 'off' // cancelled, skipped, turned off
  | 'run' // running, processing
  | 'warn' // plays, not measured
  | 'wait' // not yet
  | 'rec'; // recording

export type TabIcon =
  | 'megaphone'
  | 'story'
  | 'postPlay'
  | 'music'
  | 'mic'
  | 'server'
  | 'returnFeed'
  | 'cloud'
  | 'soundcloud';

export interface TabItem {
  icon: TabIcon;
  badge: Badge;
  /** "Pre-announcement: Scheduled · Fr 23.10. · 18:00", for the screen reader and the tooltip. */
  label: string;
}

const SOCIAL = [
  { slot: 'pre', icon: 'megaphone', name: 'Pre-announcement' },
  { slot: 'onair', icon: 'story', name: 'On-air story' },
  { slot: 'post', icon: 'postPlay', name: 'Post-announcement' },
] as const;

/** One icon per Instagram slot. */
export function socialItems(list: SlotReading[], trackPublic: boolean): TabItem[] {
  return SOCIAL.map(({ slot, icon, name }) => {
    const a = inSlot(list, slot);
    let badge: Badge;
    if (!a) badge = list.some((x) => x.slot === slot && x.state === 'cancelled') ? 'off' : 'todo';
    else if (a.state === 'draft') badge = 'todo';
    else if (a.state === 'sent') badge = 'ok';
    else if (a.state === 'failed') badge = 'fail';
    else if (a.state === 'pending_approval') badge = 'approve';
    else if (slot === 'post' && !trackPublic) badge = 'lock';
    else badge = 'sched';
    return { icon, badge, label: `${name}: ${slotState(list, slot, trackPublic)}` };
  });
}

const TESTS = [
  { id: 'music', icon: 'music', name: 'Music' },
  { id: 'mic', icon: 'mic', name: 'Mic' },
  { id: 'server', icon: 'server', name: 'Server' },
  { id: 'return', icon: 'returnFeed', name: 'Return feed' },
] as const;

const TEST_BADGE: Record<SoundcheckStepState, Badge> = {
  pending: 'wait',
  running: 'run',
  ok: 'ok',
  warn: 'warn',
  fail: 'fail',
  skipped: 'off',
};

/**
 * One icon per soundcheck test. The live results come from the Studio while
 * it is open on this page; otherwise only "passed in this tab" is known (the
 * Studio does not save its result). A pre-recorded episode has its set instead.
 */
export function soundcheckItems(
  show: ShowDetail,
  phase: EpisodePhase,
  tested: boolean,
  checks: SoundcheckStep[] | null
): TabItem[] {
  if (show.stream_mode === 'prerecorded') {
    return show.prerecorded_filename
      ? [{ icon: 'music', badge: 'ok', label: `Set: uploaded · ${show.prerecorded_filename}` }]
      : [{ icon: 'music', badge: 'todo', label: 'Set: no file yet' }];
  }
  return TESTS.map(({ id, icon, name }) => {
    const step = checks?.find((s) => s.id === id);
    if (step) return { icon, badge: TEST_BADGE[step.state], label: `${name}: ${step.detail}` };
    if (tested) return { icon, badge: 'ok', label: `${name}: Passed` };
    const word = phase === 'pre' ? 'Not checked yet' : 'No result in this browser';
    return { icon, badge: 'wait', label: `${name}: ${word}` };
  });
}

/** The recording and the SoundCloud track. */
export function recordingItems(
  show: ShowDetail,
  phase: EpisodePhase,
  recording: RecordingPill,
  onAir: boolean,
  soundcloudOff: boolean
): TabItem[] {
  let rec: TabItem;
  if (show.stream_mode === 'prerecorded') {
    rec = show.prerecorded_filename
      ? { icon: 'cloud', badge: 'ok', label: 'Recording: the set is ready' }
      : { icon: 'cloud', badge: 'todo', label: 'Recording: no set yet' };
  } else if (onAir) {
    rec = { icon: 'cloud', badge: 'rec', label: 'Recording: on air' };
  } else if (phase !== 'post') {
    rec = { icon: 'cloud', badge: 'wait', label: 'Recording: not started' };
  } else if (recording.state === 'ok') {
    rec = { icon: 'cloud', badge: 'ok', label: 'Recording: ready' };
  } else if (recording.state === 'processing') {
    rec = { icon: 'cloud', badge: 'run', label: 'Recording: processing' };
  } else {
    rec = { icon: 'cloud', badge: 'fail', label: 'Recording: no usable recording' };
  }
  let sc: TabItem;
  if (soundcloudOff) sc = { icon: 'soundcloud', badge: 'off', label: 'SoundCloud: turned off' };
  else if (show.soundcloud_public)
    sc = { icon: 'soundcloud', badge: 'ok', label: 'SoundCloud: public track' };
  else if (show.soundcloud_url)
    sc = { icon: 'soundcloud', badge: 'lock', label: 'SoundCloud: private track' };
  else sc = { icon: 'soundcloud', badge: 'wait', label: 'SoundCloud: no track yet' };
  return [rec, sc];
}

export interface TabClock {
  /** pre: countdown, live: on air, post: aired. */
  kind: 'pre' | 'live' | 'post';
  clock: string;
  sub: string;
}

/** Show time: the countdown to the start, the time on air, then the aired length. */
export function showClock(
  show: ShowDetail,
  phase: EpisodePhase,
  now: number,
  onAir: boolean
): TabClock {
  if (phase === 'post') {
    const aired = airedSecs(show);
    return { kind: 'post', clock: aired ? setLength(aired) : 'Over', sub: timeRange(show) };
  }
  if (onAir) {
    const air = elapsed(show, now);
    return { kind: 'live', clock: air.clock, sub: air.length ? `of ${air.length}` : '' };
  }
  const { startMs } = episodeTimes(show);
  if (startMs === null) return { kind: 'pre', clock: '', sub: '' };
  if (phase === 'live') return { kind: 'pre', clock: fmtClock(0), sub: 'not on air yet' };
  const left = startMs - now;
  const clock = left < 24 * 60 * 60 * 1000 ? fmtClock(left) : untilStart(show, now);
  return { kind: 'pre', clock, sub: show.start_time ? `until ${show.start_time}` : '' };
}
