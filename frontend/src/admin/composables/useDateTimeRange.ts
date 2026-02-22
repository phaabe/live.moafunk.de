import { ref, computed } from 'vue';

/**
 * Composable to manage a start/end datetime range with validation.
 * Converts between Date objects (for the picker) and the API format
 * (date: YYYY-MM-DD, start_time: HH:MM, end_time: HH:MM).
 */
export function useDateTimeRange(options?: {
  initialDate?: string;
  initialStartTime?: string;
  initialEndTime?: string;
}) {
  const startDateTime = ref<Date | null>(null);
  const endDateTime = ref<Date | null>(null);

  // Initialize from API format if provided
  if (options?.initialDate) {
    const datePart = options.initialDate;
    if (options.initialStartTime) {
      startDateTime.value = parseDateTime(datePart, options.initialStartTime);
    }
    if (options.initialEndTime) {
      endDateTime.value = parseDateTime(datePart, options.initialEndTime);
      // If end is before or equal to start, assume next day
      if (startDateTime.value && endDateTime.value && endDateTime.value <= startDateTime.value) {
        endDateTime.value = new Date(endDateTime.value.getTime() + 24 * 60 * 60 * 1000);
      }
    }
  }

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

  /** Validation: start must be before end */
  const isValid = computed(() => {
    if (!startDateTime.value || !endDateTime.value) return false;
    return startDateTime.value < endDateTime.value;
  });

  const validationError = computed(() => {
    if (!startDateTime.value || !endDateTime.value) return 'Both start and end are required';
    if (startDateTime.value >= endDateTime.value) return 'Start must be before end';
    return null;
  });

  /** API-ready values derived from the datetime pickers */
  const apiDate = computed(() => {
    if (!startDateTime.value) return '';
    return formatDate(startDateTime.value);
  });

  const apiStartTime = computed(() => {
    if (!startDateTime.value) return '';
    return formatTime(startDateTime.value);
  });

  const apiEndTime = computed(() => {
    if (!endDateTime.value) return '';
    return formatTime(endDateTime.value);
  });

  /** Bulk set from API data */
  function setFromApi(date: string, startTime?: string, endTime?: string) {
    if (date && startTime) {
      startDateTime.value = parseDateTime(date, startTime);
    } else {
      startDateTime.value = null;
    }
    if (date && endTime) {
      endDateTime.value = parseDateTime(date, endTime);
      if (startDateTime.value && endDateTime.value && endDateTime.value <= startDateTime.value) {
        endDateTime.value = new Date(endDateTime.value.getTime() + 24 * 60 * 60 * 1000);
      }
    } else {
      endDateTime.value = null;
    }
  }

  /** Reset both values */
  function reset() {
    startDateTime.value = null;
    endDateTime.value = null;
  }

  return {
    startDateTime,
    endDateTime,
    isValid,
    validationError,
    apiDate,
    apiStartTime,
    apiEndTime,
    setFromApi,
    reset,
  };
}
