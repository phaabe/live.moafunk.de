import type { Show } from './api';

/** Form state collected by the create-show modal. */
export interface ShowCreateForm {
  title: string;
  description: string;
  showType: string;
  /** YYYY-MM-DD derived from the start picker. */
  date: string;
  /** HH:MM derived from the start picker. */
  startTime: string;
  /** HH:MM derived from the end picker (admins only). */
  endTime: string;
}

/**
 * Build the `POST /api/shows` payload, applying the role-based field rules from
 * issue #146.
 *
 * Hosts submit only title + start; the backend forces the show type, drops
 * description/end time and self-assigns the host. Admins submit every field.
 */
export function buildShowCreatePayload(isAdmin: boolean, form: ShowCreateForm): Partial<Show> {
  const base: Partial<Show> = {
    title: form.title.trim(),
    date: form.date,
    start_time: form.startTime,
  };

  if (!isAdmin) return base;

  return {
    ...base,
    show_type: form.showType,
    end_time: form.endTime,
    description: form.description,
  };
}

/**
 * Validate the create-show form for the given role. Returns an error message,
 * or `null` when the form is ready to submit.
 *
 * Admins require a valid start/end range; hosts only require a start time.
 */
export function validateShowCreate(
  isAdmin: boolean,
  form: { title: string; startTime: string; endTime: string; startBeforeEnd: boolean }
): string | null {
  if (!form.title.trim()) return 'Title is required';
  if (!form.startTime) return 'Start date & time is required';
  if (isAdmin) {
    if (!form.endTime) return 'End date & time is required';
    if (!form.startBeforeEnd) return 'Start must be before end';
  }
  return null;
}
