/**
 * Short episode day as the flow writes it: "So 25.10." (German weekday
 * without its dot, then day and month). `date` is "YYYY-MM-DD".
 */
export function episodeDay(date: string): string {
  const [year, month, day] = date.split('-').map(Number);
  const weekday = new Date(Date.UTC(year, month - 1, day))
    .toLocaleDateString('de-DE', { weekday: 'short', timeZone: 'UTC' })
    .replace(/\.$/, '');
  return `${weekday} ${String(day).padStart(2, '0')}.${String(month).padStart(2, '0')}.`;
}

/** A Book Slot date option: "So 25.10.2026". */
export function slotDate(date: string): string {
  return `${episodeDay(date)}${date.slice(0, 4)}`;
}

/** A Book Slot time option: "12:00–14:00". */
export function slotTime(slot: { start_time: string; end_time: string }): string {
  return `${slot.start_time}–${slot.end_time}`;
}

/** A set's length as the flow writes it: "1:58:40", or "58:40" under an hour. */
export function setLength(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  const pad = (n: number) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}
