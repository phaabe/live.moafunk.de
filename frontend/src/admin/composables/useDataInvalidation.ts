/**
 * Lightweight invalidation tracker for keep-alive pages.
 *
 * When one page mutates data that another cached page displays,
 * call `invalidate('shows', id)` or `invalidate('artists', id)`.
 * The target page checks `consume()` inside `onActivated` and
 * reloads only when its own entity was marked dirty.
 */
import { reactive } from 'vue';

const dirty = reactive({
  shows: new Set<number>(),
  artists: new Set<number>(),
});

export function useDataInvalidation() {
  /** Mark an entity as needing a refresh. */
  function invalidate(type: 'shows' | 'artists', id: number) {
    dirty[type].add(id);
  }

  /** Check and clear the dirty flag. Returns true if the entity was dirty. */
  function consume(type: 'shows' | 'artists', id: number): boolean {
    if (dirty[type].has(id)) {
      dirty[type].delete(id);
      return true;
    }
    return false;
  }

  return { invalidate, consume };
}
