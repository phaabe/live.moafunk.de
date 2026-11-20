import { liveStreamShowId } from './composables/useStreamSocket';

const RELOADED_AT = 'chunk-reload-at';
/** No second reload this soon: then the files are missing, not just old. */
const RETRY_MS = 10_000;

/**
 * Handler for Vite's `vite:preloadError`: a lazy-loaded file failed to load.
 * After a deploy, an open tab still asks for the old files, which are gone
 * (404). Reload once to get the new build. Never while this tab is live: a
 * reload would cut the broadcast.
 */
export function reloadOnStaleChunk(reload: () => void = () => location.reload()): void {
  if (liveStreamShowId() !== null) return;
  try {
    if (Date.now() - Number(sessionStorage.getItem(RELOADED_AT)) < RETRY_MS) return;
    sessionStorage.setItem(RELOADED_AT, String(Date.now()));
  } catch {
    // Without storage nothing stops a reload loop, so keep the error.
    return;
  }
  reload();
}
