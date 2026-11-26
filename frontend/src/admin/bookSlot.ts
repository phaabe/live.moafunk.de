/**
 * Time math for Book Slot (#1118). Times are Berlin wall-clock minutes. An
 * end at or before the start is on the next day, so a show can pass midnight;
 * the backend reads it the same way.
 */
import type { BookedTime } from './api';

export const DAY_MINUTES = 1440;
/** The shortest and the longest show (the backend checks the same). */
export const MIN_LENGTH = 15;
export const MAX_LENGTH = 12 * 60;
/** The Length chips, in minutes. */
export const LENGTHS = [30, 60, 90, 120, 180, 240];
/** Day cards: 7, or 14 after More days. */
export const DAYS_SHORT = 7;
export const DAYS_LONG = 14;

const pad = (n: number) => String(n).padStart(2, '0');

/** The Berlin date ("YYYY-MM-DD") and minute of the day of an instant. */
export function berlinNow(at: Date = new Date()): { date: string; minute: number } {
  // sv-SE writes "2026-10-27 22:13:05".
  const s = at.toLocaleString('sv-SE', { timeZone: 'Europe/Berlin' });
  return { date: s.slice(0, 10), minute: Number(s.slice(11, 13)) * 60 + Number(s.slice(14, 16)) };
}

/** Days since 1970-01-01 of a "YYYY-MM-DD" date. */
export function dayNumber(date: string): number {
  const [y, m, d] = date.split('-').map(Number);
  return Date.UTC(y, m - 1, d) / 86_400_000;
}

export function addDays(date: string, days: number): string {
  return new Date((dayNumber(date) + days) * 86_400_000).toISOString().slice(0, 10);
}

/** "HH:MM" of a minute; minutes past midnight wrap to the next day. */
export function hhmm(minute: number): string {
  const m = ((minute % DAY_MINUTES) + DAY_MINUTES) % DAY_MINUTES;
  return `${pad(Math.floor(m / 60))}:${pad(m % 60)}`;
}

/** Minute of the day of "HH:MM", or null. */
export function parseHhmm(value: string): number | null {
  const m = /^(\d{2}):(\d{2})$/.exec(value);
  if (!m || Number(m[1]) > 23 || Number(m[2]) > 59) return null;
  return Number(m[1]) * 60 + Number(m[2]);
}

/** Minutes from start to end; an end at or before the start is on the next day. */
export function lengthBetween(start: number, end: number): number {
  const length = end - start;
  return length > 0 ? length : length + DAY_MINUTES;
}

/** "30 min", "1 h", "1½ h", "2 h 15 min" as the flow writes them. */
export function lengthText(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  if (!h) return `${m} min`;
  if (m === 30) return `${h}½ h`;
  return m ? `${h} h ${m} min` : `${h} h`;
}

/** A window [from, to) in minutes since 1970-01-01, Berlin wall clock. */
export type Window = [number, number];

export function windowOf(date: string, start: number, length: number): Window {
  const from = dayNumber(date) * DAY_MINUTES + start;
  return [from, from + length];
}

/** Booked shows as windows. No end makes a point at the start, as the backend reads it. */
export function bookedWindows(list: readonly BookedTime[]): Window[] {
  return list.flatMap((b) => {
    const start = parseHhmm(b.start_time);
    if (start === null) return [];
    const end = b.end_time === null ? null : parseHhmm(b.end_time);
    return [windowOf(b.date, start, end === null ? 0 : lengthBetween(start, end))];
  });
}

/** The first booked window that clashes with `w`: overlap, or the same start. */
export function clash(booked: readonly Window[], w: Window): Window | undefined {
  return booked.find((b) => (w[0] < b[1] && b[0] < w[1]) || w[0] === b[0]);
}

/** Is this minute inside a booked show (or at a booked start)? */
export function taken(booked: readonly Window[], at: number): boolean {
  return booked.some((b) => (at >= b[0] && at < b[1]) || at === b[0]);
}

/** The next full hour after `now`, on the next day after 23:xx. */
export function nextFullHour(now: { date: string; minute: number }): {
  date: string;
  minute: number;
} {
  const minute = (Math.floor(now.minute / 60) + 1) * 60;
  return minute >= DAY_MINUTES
    ? { date: addDays(now.date, 1), minute: minute - DAY_MINUTES }
    : { date: now.date, minute };
}

/** Problem with a picked window, as the overview names it; null when it can be booked. */
export function problemOf(
  w: Window,
  nowAt: number,
  startsNow: boolean,
  booked: readonly Window[],
  dayText: (date: string) => string
): string | null {
  const length = w[1] - w[0];
  if (length < MIN_LENGTH) return 'The end must be at least 15 minutes after the start.';
  if (length > MAX_LENGTH) return 'A show can be 12 h at most.';
  if (!startsNow && w[0] <= nowAt) return 'This time is over. Pick a later one.';
  const hit = clash(booked, w);
  if (!hit) return null;
  const date = new Date(Math.floor(hit[0] / DAY_MINUTES) * 86_400_000).toISOString().slice(0, 10);
  return `Another show has ${dayText(date)} ${hhmm(hit[0])}–${hhmm(hit[1])}. Pick another time or make it shorter.`;
}
