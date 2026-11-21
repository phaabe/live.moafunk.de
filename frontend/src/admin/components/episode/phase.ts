import { differenceInCalendarDays, parseISO } from 'date-fns';
import type { ShowDetail } from '../../api';
import { fmtClock } from '../../composables/useShowClocks';
import { setLength } from '../../episodeRow';
import { berlinToUtcDate, nextDayDateStr } from '../../showTime';

/**
 * The episode page's three phases (#1052): Pre-show › Show time › Post-show.
 * One layout for all of them. The phase (and, before the show, the social
 * media setup) decides which of the four steps is current.
 */
export type EpisodePhase = 'pre' | 'live' | 'post';

/** The Studio opens this long before the start. */
export const STUDIO_OPENS_MS = 30 * 60 * 1000;

/** The colour of a card's status chip. */
export type Tone = 'grey' | 'blue' | 'amber' | 'green' | 'orange' | 'red';
export interface Status {
  text: string;
  tone: Tone;
}

export type Schedule = Pick<ShowDetail, 'date' | 'start_time' | 'end_time'>;

/** Start, end and Studio opening of an episode in ms (null without a time). */
export function episodeTimes(s: Schedule): {
  startMs: number | null;
  endMs: number | null;
  openMs: number | null;
} {
  const startMs = s.start_time ? berlinToUtcDate(s.date, s.start_time).getTime() : null;
  let endMs: number | null = null;
  if (s.end_time) {
    // An end at or before the start is on the next day.
    const endDay = s.start_time && s.end_time <= s.start_time ? nextDayDateStr(s.date) : s.date;
    endMs = berlinToUtcDate(endDay, s.end_time).getTime();
  }
  return { startMs, endMs, openMs: startMs === null ? null : startMs - STUDIO_OPENS_MS };
}

/**
 * Where the episode is. On air is always Show time; an episode the backend no
 * longer counts as upcoming (ended, ended early, published) is Post-show.
 */
export function episodePhase(
  s: Schedule & Pick<ShowDetail, 'on_air' | 'episode_status'>,
  now: number
): EpisodePhase {
  if (s.on_air) return 'live';
  if (s.episode_status && s.episode_status !== 'upcoming') return 'post';
  const { startMs, endMs } = episodeTimes(s);
  if (endMs !== null && now > endMs) return 'post';
  if (startMs !== null && now >= startMs) return 'live';
  return 'pre';
}

/** "12:00–14:00", or the start alone. */
export function timeRange(s: Schedule): string {
  if (s.start_time && s.end_time) return `${s.start_time}–${s.end_time}`;
  return s.start_time ?? '';
}

/** Today in Berlin as YYYY-MM-DD. */
function berlinToday(now: number): string {
  return new Date(now).toLocaleDateString('en-CA', { timeZone: 'Europe/Berlin' });
}

/** "in 20 min", "in 3 h", "tomorrow", "in 2 days": how long until the start. */
export function untilStart(s: Schedule, now: number): string {
  const { startMs } = episodeTimes(s);
  if (startMs === null) return '';
  const ms = startMs - now;
  if (ms <= 0) return 'now';
  if (ms < 60 * 60 * 1000) return `in ${Math.ceil(ms / 60000)} min`;
  const days = differenceInCalendarDays(parseISO(s.date), parseISO(berlinToday(now)));
  if (days <= 0 || ms < 12 * 60 * 60 * 1000) return `in ${Math.round(ms / 3600000)} h`;
  if (days === 1) return 'tomorrow';
  return `in ${days} days`;
}

/** "11:30" Berlin time of an instant. */
export function berlinTime(ms: number): string {
  return new Date(ms).toLocaleTimeString('de-DE', {
    timeZone: 'Europe/Berlin',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** The aired length in seconds, when it is known. */
export function airedSecs(s: ShowDetail): number | null {
  if (s.stream_mode === 'prerecorded') return s.prerecorded_duration_secs ?? null;
  const ms = s.latest_recording?.duration_ms;
  return ms ? ms / 1000 : null;
}

/** Time on air so far by the schedule, as "00:42:10", with the fill of the show length. */
export function elapsed(s: Schedule, now: number): { clock: string; length: string; fill: number } {
  const { startMs, endMs } = episodeTimes(s);
  if (startMs === null || endMs === null || endMs <= startMs) {
    return { clock: fmtClock(0), length: '', fill: 0 };
  }
  const passed = Math.min(Math.max(now - startMs, 0), endMs - startMs);
  return {
    clock: fmtClock(passed),
    length: setLength((endMs - startMs) / 1000),
    fill: Math.round((passed / (endMs - startMs)) * 100),
  };
}

/** The three post-show steps the bar counts. */
export interface PostSteps {
  recording: boolean;
  trackPublic: boolean;
  postSent: boolean;
}

/** Set-up and sent slots of the two posts before the show (pre-announcement, on-air story). */
export interface SocialSetup {
  ready: number;
  sent: number;
}

export type StepState = 'done' | 'now' | 'next';

export interface Segment {
  name: 'Set up social media' | 'Soundcheck' | 'Show time' | 'Publish recording';
  /** The name on phones. */
  short: string;
  state: StepState;
  /** 0–100: how far the step is. */
  fill: number;
  label: string;
}

export interface ProgressInput {
  show: ShowDetail;
  now: number;
  phase: EpisodePhase;
  /** The Studio soundcheck passed in this browser tab (it is not saved). */
  tested: boolean;
  social: SocialSetup;
  steps: PostSteps;
}

const clamp = (n: number) => Math.round(Math.min(Math.max(n, 0), 100));

/**
 * The four steps of an episode, in the order of its cards: Set up social
 * media › Soundcheck › Show time › Publish recording. Steps before the
 * current one are done, the ones after it are next. Before the show, Set up
 * social media is current until both posts are set up or the Studio opens.
 * The current step fills: social media by its set-up posts, Soundcheck by the
 * time from Studio open to the start, Show time by the time on air of the
 * show length, Publish recording by its done steps.
 */
export function progress({ show, now, phase, tested, social, steps }: ProgressInput): Segment[] {
  const { openMs } = episodeTimes(show);
  const prerecorded = show.stream_mode === 'prerecorded';
  const studioOpen = openMs !== null && now >= openMs;
  let at: number;
  if (phase === 'live') at = 2;
  else if (phase === 'post') at = 3;
  else at = social.ready >= 2 || studioOpen ? 1 : 0;
  const state = (i: number): StepState => (i < at ? 'done' : i === at ? 'now' : 'next');

  const setup: Segment = {
    name: 'Set up social media',
    short: 'Social',
    state: state(0),
    fill: at > 0 ? 100 : clamp((social.ready / 2) * 100),
    label:
      social.sent >= 2
        ? '2 posts sent'
        : social.ready >= 2
          ? '2 posts ready'
          : `${social.ready} of 2 posts ready`,
  };

  let preLabel: string;
  if (at === 1) preLabel = `starts ${untilStart(show, now)}`;
  else if (prerecorded) preLabel = show.prerecorded_filename ? 'set uploaded' : 'no set';
  else if (tested) preLabel = 'tested';
  else preLabel = openMs === null ? 'done' : `Studio from ${berlinTime(openMs)}`;
  const pre: Segment = {
    name: 'Soundcheck',
    short: 'Soundcheck',
    state: state(1),
    fill:
      at > 1
        ? 100
        : at < 1 || openMs === null
          ? 0
          : clamp(((now - openMs) / STUDIO_OPENS_MS) * 100),
    label: preLabel,
  };

  const air = elapsed(show, now);
  const aired = airedSecs(show);
  const live: Segment = {
    name: 'Show time',
    short: 'Show time',
    state: state(2),
    fill: phase === 'pre' ? 0 : phase === 'post' ? 100 : air.fill,
    label:
      phase === 'live'
        ? show.on_air || prerecorded
          ? `${air.clock} of ${air.length}`
          : 'not on air yet'
        : phase === 'post' && aired
          ? `aired ${setLength(aired)}`
          : timeRange(show),
  };

  const done = [
    steps.recording && 'recording ready',
    steps.trackPublic && 'track public',
    steps.postSent && 'post sent',
  ].filter((x): x is string => !!x);
  const finished = show.episode_status === 'finished';
  let postLabel = show.end_time ? `after ${show.end_time}` : '';
  if (phase === 'post') {
    postLabel = finished
      ? 'published'
      : `${done.length} of 3${done.length ? ` · ${done[done.length - 1]}` : ''}`;
  }
  const post: Segment = {
    name: 'Publish recording',
    short: 'Publish',
    state: phase === 'post' && finished ? 'done' : state(3),
    fill: phase !== 'post' ? 0 : finished ? 100 : clamp((done.length / 3) * 100),
    label: postLabel,
  };
  return [setup, pre, live, post];
}
