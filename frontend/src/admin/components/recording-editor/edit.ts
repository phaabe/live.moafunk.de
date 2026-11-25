// The edit of an episode recording (#883): what the host decides on the Edit
// recording page. The server renders the new file from it with ffmpeg; gainAt()
// is the same volume math for the preview in the browser, so what the host
// hears is what gets rendered. All times are seconds from the start of the
// original recording.

export interface GainSection {
  id: string;
  start: number;
  end: number;
  gainDb: number;
}

/** A short clip for the Instagram post that announces the recording. Stored apart from the edit. */
export interface Snippet {
  slot: number;
  start: number;
  length: number;
}

export interface RecordingEdit {
  /** Where the set starts and ends; the rest is cut. */
  start: number;
  end: number;
  /** Volume of the whole set. */
  gainDb: number;
  sections: GainSection[];
}

export const SNIPPET_SLOTS = [1, 2, 3];
export const SNIPPET_MIN = 5;
export const SNIPPET_MAX = 30;
export const GAIN_MIN = -12;
export const GAIN_MAX = 12;
export const GAIN_STEP = 0.5;
/** Volume ramp at each section edge, so a change does not click. */
export const RAMP_S = 0.3;
/** Fades at the cuts. */
export const FADE_IN_S = 0.5;
export const FADE_OUT_S = 2;
export const MIN_SET_S = FADE_IN_S + FADE_OUT_S;
export const MIN_SECTION_S = 2 * RAMP_S;
export const MAX_SECTIONS = 20;

export const dbToGain = (db: number): number => 10 ** (db / 20);

const clip01 = (x: number): number => Math.min(1, Math.max(0, x));
export const clamp = (x: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, x));

/** −12 … +12 dB in 0.5 dB steps. */
export const clampGain = (db: number): number =>
  clamp(Math.round(db / GAIN_STEP) * GAIN_STEP, GAIN_MIN, GAIN_MAX);

export function newEdit(duration: number): RecordingEdit {
  return { start: 0, end: duration, gainDb: 0, sections: [] };
}

/** 0 outside the section, 1 inside, linear ramps of RAMP_S at both edges. */
function sectionWeight(s: GainSection, t: number): number {
  return clip01(Math.min(t - s.start, s.end - t) / RAMP_S);
}

/** The linear gain the render applies at time `t`; null where the set is cut. */
export function gainAt(edit: RecordingEdit, t: number): number | null {
  if (t < edit.start || t > edit.end) return null;
  const fade = Math.min(clip01((t - edit.start) / FADE_IN_S), clip01((edit.end - t) / FADE_OUT_S));
  // Sections never overlap, so at most one weight is above 0.
  const sections = edit.sections.reduce(
    (sum, s) => sum + (dbToGain(s.gainDb) - 1) * sectionWeight(s, t),
    1
  );
  return dbToGain(edit.gainDb) * sections * fade;
}

/** What is wrong with the edit, as text for the host. Empty when it can be applied. */
export function problems(edit: RecordingEdit, duration: number): string[] {
  const out: string[] = [];
  if (edit.start < 0 || edit.end > duration + 0.05 || edit.end - edit.start < MIN_SET_S)
    out.push('The set must start before it ends.');
  const sorted = [...edit.sections].sort((a, b) => a.start - b.start);
  if (sorted.length > MAX_SECTIONS) out.push(`Use at most ${MAX_SECTIONS} volume sections.`);
  sorted.forEach((s, i) => {
    if (s.end - s.start < MIN_SECTION_S) out.push('A volume section is too short.');
    else if (s.start < edit.start || s.end > edit.end)
      out.push('A volume section must be inside the set.');
    const next = sorted[i + 1];
    if (next && next.start < s.end) out.push('Volume sections must not overlap.');
  });
  return out;
}

/** How loud the edit gets: places where it goes over 0 dB, and the loudest peak in dB. */
export function loudness(
  edit: RecordingEdit,
  peaks: ArrayLike<number>,
  perSecond: number
): { places: number; peakDb: number } {
  let max = 0;
  let places = 0;
  let over = false;
  const from = Math.max(0, Math.floor(edit.start * perSecond));
  const to = Math.min(peaks.length, Math.ceil(edit.end * perSecond));
  for (let i = from; i < to; i++) {
    const v = peaks[i] * (gainAt(edit, (i + 0.5) / perSecond) ?? 0);
    if (v > max) max = v;
    if (v > 1 && !over) places++;
    over = v > 1;
  }
  return { places, peakDb: 20 * Math.log10(Math.max(max, 0.001)) };
}

const pad = (v: number | string, w: number) => String(v).padStart(w, '0');

/** 1:05:09.4 */
export function fmt(seconds: number): string {
  const d = Math.floor(Math.max(0, seconds) * 10 + 1e-6);
  const s = ((d % 600) / 10).toFixed(1);
  return `${Math.floor(d / 36000)}:${pad(Math.floor(d / 600) % 60, 2)}:${pad(s, 4)}`;
}

/** 1:05:09 */
export function clock(seconds: number): string {
  const t = Math.round(Math.max(0, seconds));
  return `${Math.floor(t / 3600)}:${pad(Math.floor(t / 60) % 60, 2)}:${pad(t % 60, 2)}`;
}

/** A length: 5:00, or 1:47:40 from an hour. */
export function span(seconds: number): string {
  const t = Math.round(seconds);
  return t >= 3600 ? clock(t) : `${Math.floor(t / 60)}:${pad(t % 60, 2)}`;
}

/** +2.5 dB, −3.0 dB, 0.0 dB */
export function signed(db: number): string {
  return `${db > 0 ? '+' : db < 0 ? '−' : ''}${Math.abs(db).toFixed(1)} dB`;
}

/** "1:02:03.4", "6:09.6" or "369.6" to seconds; null when it is not a time. */
export function parseTime(text: string): number | null {
  const parts = text.trim().replace(',', '.').split(':');
  if (parts.length > 3 || parts.some((p) => p === '' || !Number.isFinite(Number(p)))) return null;
  return parts.reduce((acc, p) => acc * 60 + Number(p), 0);
}
