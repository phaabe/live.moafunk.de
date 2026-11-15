import type { Router } from 'vue-router';

/** The booked pre-recorded episode whose set "Replace file" swaps. */
export interface ReplaceTarget {
  id: number;
  date: string;
  start_time: string;
  end_time: string;
}

/**
 * Open Upload Your Set to replace an episode's file. The episode travels in
 * the history state, so the path stays `/shows/<slug>/book?mode=prerecorded`
 * as the flow writes it.
 */
export function openReplaceFile(router: Router, slug: string, target: ReplaceTarget) {
  return router.push({
    path: `/shows/${slug}/book`,
    query: { mode: 'prerecorded' },
    state: { replaceEpisode: { ...target } },
  });
}

/** The replace target of the current history entry, if there is one. */
export function currentReplaceTarget(router: Router): ReplaceTarget | null {
  const state = router.options.history.state as { replaceEpisode?: unknown } | null;
  const target = state?.replaceEpisode as Partial<ReplaceTarget> | undefined;
  if (
    !target ||
    typeof target.id !== 'number' ||
    typeof target.date !== 'string' ||
    typeof target.start_time !== 'string' ||
    typeof target.end_time !== 'string'
  ) {
    return null;
  }
  return target as ReplaceTarget;
}
