# `gh api -i` fixtures

Output of `gh api -i` (gh 2.87.3) as the shared reader (`github_state.py`)
reads it: status line, CRLF headers, a blank line, then the body.

Recorded on 2026-09-29 against `repos/phaabe/live.moafunk.de/issues/...`:

- `200-page.txt`: a 200 page with `Etag` and `Link`. Headers as recorded; the
  body is cut to one row. Request IDs are removed.
- `304.txt`: the same URL with `If-None-Match`. gh exits 1, prints the headers,
  an empty body, no `Link`, and `gh: HTTP 304` on stderr.
- `404.txt`: a missing issue. gh exits 1.

Built from the recorded 404 headers (these errors cannot be caused safely):

- `401.txt`: bad credentials.
- `403-rate-limit.txt`: primary rate limit (`X-Ratelimit-Remaining: 0`).
- `403-secondary.txt`: secondary rate limit (`Retry-After`).
- `403-forbidden.txt`: a 403 that is not a rate limit.
- `502.txt`: a server error.

Tests replace `Etag` and `Link` per URL and the body per request.
