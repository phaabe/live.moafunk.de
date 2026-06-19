/**
 * Keep public clients on the latest deploy without a manual hard refresh.
 *
 * GitHub Pages serves index.html with a 10-minute cache and we can't set headers,
 * so a returning visitor (or an already-open tab) can keep running an old bundle —
 * which, after the Icecast cutover, polls the dead NMS mount and never shows a live
 * show. To fix that going forward, the build stamps a `/version.json` with a build
 * id; the running page polls it and reloads when it changes. A plain
 * `location.reload()` forces the browser to revalidate index.html, so the client
 * lands on the new bundle.
 *
 * Playback-aware: never reload while audio is actively playing — that would cut a
 * live listen (and autoplay policies wouldn't resume it). The reload is deferred to
 * the next poll where playback is idle, which is exactly the "off air / not
 * listening" state where staleness actually matters.
 */

declare const __BUILD_ID__: string;

/** Build id baked in at build time via Vite `define`; `'dev'` when unset (tests/dev). */
export function currentBuildId(): string {
  return typeof __BUILD_ID__ !== 'undefined' ? __BUILD_ID__ : 'dev';
}

/**
 * Pure decision: reload only when the deployed build differs from ours AND audio
 * isn't actively playing. Missing `latest` (failed or absent fetch) never reloads.
 */
export function shouldReload(
  current: string,
  latest: string | null | undefined,
  isPlaying: boolean
): boolean {
  if (!latest) return false;
  if (latest === current) return false;
  return !isPlaying;
}

export interface VersionWatcherOptions {
  /** True while a live listen is in progress — defers the reload so we don't cut it. */
  isPlaying: () => boolean;
  /** Poll cadence in ms (default 60s). */
  intervalMs?: number;
  /** Injectable for tests. */
  reload?: () => void;
  /** Injectable for tests. */
  fetchVersion?: () => Promise<string | null>;
}

/** Fetch the deployed build id from /version.json, cache-busted; null on any error. */
async function defaultFetchVersion(): Promise<string | null> {
  try {
    const res = await fetch(`/version.json?v=${Date.now()}`, { cache: 'no-store' });
    if (!res.ok) return null;
    const data = (await res.json()) as { hash?: string };
    return data.hash ?? null;
  } catch {
    return null;
  }
}

/**
 * Start polling for new deploys; reload when one lands and playback is idle.
 * Returns the interval handle (so callers/tests can clear it).
 */
export function startVersionWatcher(opts: VersionWatcherOptions): ReturnType<typeof setInterval> {
  const intervalMs = opts.intervalMs ?? 60_000;
  const reload = opts.reload ?? (() => location.reload());
  const fetchVersion = opts.fetchVersion ?? defaultFetchVersion;
  const current = currentBuildId();

  return setInterval(async () => {
    const latest = await fetchVersion();
    if (shouldReload(current, latest, opts.isPlaying())) {
      reload();
    }
  }, intervalMs);
}
