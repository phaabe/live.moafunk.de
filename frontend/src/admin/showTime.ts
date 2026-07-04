/**
 * Shared Berlin-wall-clock time helpers for show scheduling.
 *
 * Shows are stored as a Berlin-local `date` (YYYY-MM-DD) plus `start_time` /
 * `end_time` (HH:MM). These helpers are the single source of truth for
 * "is this show running / ended" and for converting that wall-clock schedule
 * into UTC instants for countdown arithmetic. Previously duplicated in
 * `useHostFlow` and `ShowDetailPage`.
 */

/** The minimal schedule shape shared by `MyShowInfo` and `ShowDetail`. */
export interface ShowSchedule {
  date: string;
  start_time?: string;
  end_time?: string;
}

/**
 * Get the current Berlin wall-clock time as an ISO-comparable string (YYYY-MM-DDTHH:MM:SS).
 * Uses sv-SE locale which formats as "YYYY-MM-DD HH:MM:SS".
 */
export function getBerlinNowISO(): string {
  return new Date().toLocaleString('sv-SE', { timeZone: 'Europe/Berlin' }).replace(' ', 'T');
}

/**
 * Convert a Berlin wall-clock date + time to a proper UTC Date object.
 * Useful for countdown arithmetic (target.getTime() - Date.now()).
 */
export function berlinToUtcDate(dateStr: string, timeStr: string): Date {
  // Interpret as UTC first, then adjust by the Berlin-UTC offset
  const asUtc = new Date(`${dateStr}T${timeStr}:00Z`);
  const berlinMs = new Date(asUtc.toLocaleString('en-US', { timeZone: 'Europe/Berlin' })).getTime();
  const utcMs = new Date(asUtc.toLocaleString('en-US', { timeZone: 'UTC' })).getTime();
  const berlinOffsetMs = berlinMs - utcMs;
  return new Date(asUtc.getTime() - berlinOffsetMs);
}

/** The day after a YYYY-MM-DD date string, as YYYY-MM-DD. */
export function nextDayDateStr(dateStr: string): string {
  const d = new Date(`${dateStr}T00:00:00`);
  d.setDate(d.getDate() + 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/** Check if a show is currently running (between start and end time in Berlin TZ). */
export function isShowRunning(s: ShowSchedule): boolean {
  if (!s.date || !s.start_time || !s.end_time) return false;
  const nowStr = getBerlinNowISO();
  const startStr = `${s.date}T${s.start_time}:00`;
  const endDateStr = s.end_time <= s.start_time ? nextDayDateStr(s.date) : s.date;
  const endStr = `${endDateStr}T${s.end_time}:00`;
  return nowStr >= startStr && nowStr <= endStr;
}

/** Check if a show has ended (end date/time is past, handling overnight shows). */
export function isShowEnded(s: ShowSchedule): boolean {
  if (!s.date || !s.end_time) return false;
  const nowStr = getBerlinNowISO();
  const endDateStr = s.start_time && s.end_time <= s.start_time ? nextDayDateStr(s.date) : s.date;
  const endStr = `${endDateStr}T${s.end_time}:00`;
  return nowStr > endStr;
}
