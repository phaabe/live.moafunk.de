import { computed, onScopeDispose, ref, type Ref } from 'vue';
import { differenceInMilliseconds } from 'date-fns';
import { berlinToUtcDate, type ShowSchedule } from '../showTime';

export interface HostLoginWarningSource extends ShowSchedule {
  status: string;
  host_has_logged_in?: boolean | null;
}

/** Keep imminent-show warnings current even when the schedule does not change. */
export function useHostLoginWarnings<T extends HostLoginWarningSource>(shows: Ref<T[]>) {
  const now = ref(Date.now());
  const timer = setInterval(() => {
    now.value = Date.now();
  }, 1000);
  onScopeDispose(() => clearInterval(timer));

  return computed(() =>
    shows.value.filter((show) => {
      if (show.status !== 'scheduled' || show.host_has_logged_in !== false || !show.start_time) {
        return false;
      }
      const untilStart = differenceInMilliseconds(
        berlinToUtcDate(show.date, show.start_time),
        now.value
      );
      return untilStart >= 0 && untilStart <= 60 * 60 * 1000;
    })
  );
}
