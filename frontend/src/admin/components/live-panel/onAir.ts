import { computed, ref } from 'vue';
import { nextDayDateStr } from '@admin/showTime';
import type { UploadQualityMode } from '@admin/composables';
import type { ListenerSample } from '@admin/composables/useListeners';

/** "00:42:10": time on air, a moment's timecode. */
export function formatHms(totalSeconds: number): string {
  const total = Math.max(0, Math.floor(totalSeconds));
  const parts = [Math.floor(total / 3600), Math.floor((total % 3600) / 60), total % 60];
  return parts.map((n) => String(n).padStart(2, '0')).join(':');
}

/** "1:17:50", or "4:12" under an hour: time left in the slot. */
export function formatLeft(totalSeconds: number): string {
  const total = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, '0');
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
}

/** "12:42": a wall-clock time of this browser, 24 h. */
export function formatClockTime(ms: number): string {
  return new Date(ms).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
}

/** The Connection verdict, in the flow's words (row "Connection | stable"). */
export type ConnectionVerdict = 'stable' | 'tight' | 'too slow' | 'reconnecting…' | 'lost';

/** The status bar warns this long before the slot ends. */
export const ENDING_SOON_S = 5 * 60;

export interface SlotSchedule {
  date: string;
  start_time?: string;
  end_time?: string;
}

/**
 * The episode's slot as instants. An end time at or before the start time is
 * on the next day. The slot end never cuts a live show: only End Episode does.
 */
export function slotWindow(
  show: SlotSchedule | undefined,
  toUtc: (date: string, time: string) => Date
): { start: Date | null; end: Date | null } {
  if (!show?.date) return { start: null, end: null };
  try {
    const start = show.start_time ? toUtc(show.date, show.start_time) : null;
    if (!show.end_time) return { start, end: null };
    const overnight = !!show.start_time && show.end_time <= show.start_time;
    const end = toUtc(overnight ? nextDayDateStr(show.date) : show.date, show.end_time);
    return { start, end };
  } catch {
    return { start: null, end: null };
  }
}

// ─── Moments ────────────────────────────────────────────────────────────────
// Mark moment keeps an on-air timecode. Kept in this tab, across remounts of
// On Air like its clock. There is no server side yet, so the Recording card
// does not see them (its Instagram moments are a later release).

export interface Moment {
  /** Seconds on air. */
  at: number;
  /** Unix ms when it was marked. */
  markedAt: number;
}

const kept = ref<{ showId: number | null; list: Moment[] }>({ showId: null, list: [] });

export function useMoments(showId: number) {
  if (kept.value.showId !== showId) kept.value = { showId, list: [] };
  return {
    list: computed(() => (kept.value.showId === showId ? kept.value.list : [])),
    mark(atSeconds: number): Moment {
      const moment = { at: Math.max(0, Math.floor(atSeconds)), markedAt: Date.now() };
      kept.value = { showId, list: [...kept.value.list, moment] };
      return moment;
    },
  };
}

/** Forget the moments (End Episode). */
export function forgetMoments(): void {
  kept.value = { showId: null, list: [] };
}

// ─── The live panel's values (LivePanel.vue) ────────────────────────────────
// The host's live tab fills them from the live session; the episode page,
// read-only, from what the host's browser saved (or nothing before the show).

/** The status bar. `badge`: on air now, not on air (before the show), or aired. */
export interface PanelBar {
  badge: 'live' | 'off' | 'aired';
  /** "00:42:10" */
  clock: string;
  rec: boolean;
  screenOn: boolean;
  listeners: number | null;
  title: string;
  /** "12:00–14:00" */
  slot: string;
  /** "ends in 4:12", or '' */
  left: string;
  soon: boolean;
  progress: number | null;
  alert: { chip: string; text: string } | null;
}

/** The Audio card. A null stream: no meter (another browser, or after the show). */
export interface PanelAudio {
  stream: MediaStream | null;
  analyser: AnalyserNode | null;
  inputLabel: string;
  inputGone: boolean;
  picker: boolean;
  gain: number;
  /** The public stream to monitor; '' hides "What listeners hear". */
  monitorSrc: string;
}

/** The Connection card; `connection` null: nothing measured. */
export interface PanelLink {
  connection: ConnectionVerdict | null;
  uploadKbps: number;
  bufferSeconds: number;
  rttMs: number | null;
  history: number[];
  targetKbps: number;
  quality: UploadQualityMode;
  lastStep: { from: number; to: number; at: number } | null;
}

export interface PanelListeners {
  current: number | null;
  peak: ListenerSample | null;
  samples: ListenerSample[];
}
