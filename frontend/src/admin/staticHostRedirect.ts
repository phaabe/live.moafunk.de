/**
 * The built admin SPA ships to two origins: the backend host
 * (`admin.live.moafunk.de`, nginx + API) and GitHub Pages
 * (`live.moafunk.de/admin/`, static only — the Pages artifact is the whole
 * `frontend/dist`). The API client talks to its own origin, so on the static
 * origin every call lands on a host without an API: `POST /api/auth/login`
 * answers 405 and login is impossible.
 *
 * So visitors on a static host get bounced to the backend host before the app
 * boots. Only the hosts listed here redirect — the box, localhost and any
 * preview build are left alone, so this can never loop.
 */
export const ADMIN_ORIGIN = 'https://admin.live.moafunk.de';

const STATIC_HOSTS = ['live.moafunk.de', 'www.live.moafunk.de', 'phaabe.github.io'];

/**
 * Where this visitor should go instead, or `null` to boot the app normally.
 *
 * The admin router uses hash history, so the fragment carries the target route
 * and survives the hop. The `/admin/` path prefix does not exist on the backend
 * host and is dropped.
 */
export function staticHostRedirectTarget(loc: {
  hostname: string;
  hash: string;
  search: string;
}): string | null {
  if (!STATIC_HOSTS.includes(loc.hostname.toLowerCase())) {
    return null;
  }
  return `${ADMIN_ORIGIN}/${loc.search}${loc.hash}`;
}
