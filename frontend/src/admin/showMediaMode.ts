import type { ShowDetail } from './api';
import type { ShowPhase } from './composables/useShowPhase';

/**
 * Decide which media tab the show detail page opens on (live vs upload).
 *
 * A present prerecorded file always implies upload mode (the host has already
 * staged a file); otherwise honour the delivery mode the show was created with
 * (`stream_mode`), defaulting to 'upload' when it is unset.
 *
 * Regression: a show created as `stream_mode: 'live'` must open on the live tab
 * rather than silently defaulting to upload.
 */
export function resolveMediaMode(
  show: Pick<ShowDetail, 'prerecorded_key' | 'stream_mode'>
): 'live' | 'upload' {
  if (show.prerecorded_key) return 'upload';
  return show.stream_mode === 'live' ? 'live' : 'upload';
}

/**
 * Whether the host may still switch between live and prerecorded.
 *
 * Only before air time: the upload exists only in the prep phase, so from air
 * time on the switch would change a label and nothing else.
 */
export function canSwitchMediaMode(phase: ShowPhase): boolean {
  return phase === 'prep';
}
