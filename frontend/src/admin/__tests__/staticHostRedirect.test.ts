import { describe, it, expect } from 'vitest';

import { ADMIN_ORIGIN, staticHostRedirectTarget } from '../staticHostRedirect';

describe('staticHostRedirectTarget', () => {
  it('sends the Pages origin to the admin host and keeps the route', () => {
    expect(
      staticHostRedirectTarget({
        hostname: 'live.moafunk.de',
        hash: '#/login',
        search: '',
      })
    ).toBe(`${ADMIN_ORIGIN}/#/login`);
  });

  it('drops the /admin/ prefix and keeps the query string', () => {
    expect(
      staticHostRedirectTarget({
        hostname: 'live.moafunk.de',
        hash: '#/shows/12',
        search: '?from=mail',
      })
    ).toBe(`${ADMIN_ORIGIN}/?from=mail#/shows/12`);
  });

  it('handles a bare /admin/ visit with no route yet', () => {
    expect(staticHostRedirectTarget({ hostname: 'live.moafunk.de', hash: '', search: '' })).toBe(
      `${ADMIN_ORIGIN}/`
    );
  });

  it('also covers the www and github.io spellings, case-insensitively', () => {
    for (const hostname of ['www.live.moafunk.de', 'phaabe.github.io', 'Live.Moafunk.De']) {
      expect(staticHostRedirectTarget({ hostname, hash: '#/login', search: '' })).toBe(
        `${ADMIN_ORIGIN}/#/login`
      );
    }
  });

  it('never redirects the backend host (would loop)', () => {
    expect(
      staticHostRedirectTarget({
        hostname: 'admin.live.moafunk.de',
        hash: '#/login',
        search: '',
      })
    ).toBeNull();
  });

  it('leaves local dev and unknown hosts alone', () => {
    for (const hostname of ['localhost', '127.0.0.1', 'staging.example.com']) {
      expect(staticHostRedirectTarget({ hostname, hash: '#/login', search: '' })).toBeNull();
    }
  });
});
