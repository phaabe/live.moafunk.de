import { ref, computed } from 'vue';

/** Default show length when none is known yet (2 hours). */
export const DEFAULT_DURATION_MIN = 120;

/**
 * Composable to manage a show's air window as **start + duration**.
 *
 * Duration (minutes) is the source of truth; the end is derived
 * (`end = start + duration`), so moving the start keeps the length fixed.
 * Still converts to/from the API format (date: YYYY-MM-DD, start_time: HH:MM,
 * end_time: HH:MM) — `apiEndTime` is computed from the derived end, so callers
 * and the backend contract are unchanged.
 */
export function useDateTimeRange(options?: {
  initialDate?: string;
  initialStartTime?: string;
  initialEndTime?: string;
}) {
  const startDateTime = ref<Date | null>(null);
  const durationMinutes = ref<number>(DEFAULT_DURATION_MIN);

  /** Parse "YYYY-MM-DD" + "HH:MM" into a Date */
  function parseDateTime(date: string, time: string): Date {
    const [year, month, day] = date.split('-').map(Number);
    const [hours, minutes] = time.split(':').map(Number);
    return new Date(year, month - 1, day, hours, minutes);
  }

  /** Format a Date to "YYYY-MM-DD" */
  function formatDate(d: Date): string {
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    return `${yyyy}-${mm}-${dd}`;
  }

  /** Format a Date to "HH:MM" */
  function formatTime(d: Date): string {
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    return `${hh}:${mm}`;
  }

  /**
   * Minutes from `start` to `end`, wrapping past midnight: an end at or before
   * the start is treated as the next day (overnight show).
   */
  function diffMinutes(start: Date, end: Date): number {
    let ms = end.getTime() - start.getTime();
    if (ms <= 0) ms += 24 * 60 * 60 * 1000;
    return Math.round(ms / 60000);
  }

  // Initialize from API format if provided.
  if (options?.initialDate && options.initialStartTime) {
    startDateTime.value = parseDateTime(options.initialDate, options.initialStartTime);
    if (options.initialEndTime) {
      const end = parseDateTime(options.initialDate, options.initialEndTime);
      durationMinutes.value = diffMinutes(startDateTime.value, end);
    }
  }

  /**
   * The end of the window, derived from start + duration. Writable: assigning a
   * Date back-computes the duration (used by the timeline drag + legacy pickers).
   */
  const endDateTime = computed<Date | null>({
    get() {
      if (!startDateTime.value) return null;
      return new Date(startDateTime.value.getTime() + durationMinutes.value * 60000);
    },
    set(value: Date | null) {
      if (!value || !startDateTime.value) return;
      durationMinutes.value = diffMinutes(startDateTime.value, value);
    },
  });

  /** Validation: a start must be set and the duration positive. */
  const isValid = computed(() => !!startDateTime.value && durationMinutes.value > 0);

  const validationError = computed(() => {
    if (!startDateTime.value) return 'Start date & time is required';
    if (durationMinutes.value <= 0) return 'Duration must be greater than zero';
    return null;
  });

  /** API-ready values derived from the start + duration. */
  const apiDate = computed(() => (startDateTime.value ? formatDate(startDateTime.value) : ''));

  const apiStartTime = computed(() => (startDateTime.value ? formatTime(startDateTime.value) : ''));

  const apiEndTime = computed(() => (endDateTime.value ? formatTime(endDateTime.value) : ''));

  /** Set the show length in minutes (clamped to a non-negative whole number). */
  function setDuration(minutes: number) {
    durationMinutes.value = Math.max(0, Math.round(minutes));
  }

  /** Bulk set from API data. Duration is inferred from start/end. */
  function setFromApi(date: string, startTime?: string, endTime?: string) {
    if (date && startTime) {
      startDateTime.value = parseDateTime(date, startTime);
      if (endTime) {
        durationMinutes.value = diffMinutes(startDateTime.value, parseDateTime(date, endTime));
      }
    } else {
      startDateTime.value = null;
    }
  }

  /** Reset to no start and the default duration. */
  function reset() {
    startDateTime.value = null;
    durationMinutes.value = DEFAULT_DURATION_MIN;
  }

  return {
    startDateTime,
    endDateTime,
    durationMinutes,
    isValid,
    validationError,
    apiDate,
    apiStartTime,
    apiEndTime,
    setDuration,
    setFromApi,
    reset,
  };
}
