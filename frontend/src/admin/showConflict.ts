import type { ScheduleItem } from './api';

/**
 * Whether two time windows clash. A missing end is treated as a zero-length
 * point at its start, and identical starts always clash — mirrors the backend's
 * `time_windows_overlap` so the wizard blocks exactly what the API would reject.
 */
export function rangesOverlap(
  startA: Date,
  endA: Date | null,
  startB: Date,
  endB: Date | null
): boolean {
  const ea = (endA ?? startA).getTime();
  const eb = (endB ?? startB).getTime();
  const sa = startA.getTime();
  const sb = startB.getTime();
  // Half-open overlap [start, end), plus an explicit equal-start collision.
  return (sa < eb && sb < ea) || sa === sb;
}

/** Build a Date from a "YYYY-MM-DD" date and "HH:MM" time. */
function parseDateTime(date: string, time: string): Date {
  const [y, m, d] = date.split('-').map(Number);
  const [hh, mm] = time.split(':').map(Number);
  return new Date(y, m - 1, d, hh, mm);
}

/**
 * Find the first scheduled show whose time window overlaps [start, end).
 * `excludeId` skips a show (e.g. the one being edited). Shows without a start
 * time can't be placed on the clock and are ignored. Returns null when free.
 */
export function findConflictingShow(
  start: Date | null,
  end: Date | null,
  shows: ScheduleItem[],
  excludeId?: number
): ScheduleItem | null {
  if (!start) return null;
  for (const show of shows) {
    if (show.id === excludeId) continue;
    if (!show.start_time) continue;
    const showStart = parseDateTime(show.date, show.start_time);
    const showEnd = show.end_time ? parseDateTime(show.date, show.end_time) : null;
    if (rangesOverlap(start, end, showStart, showEnd)) return show;
  }
  return null;
}
