# Studio soundcheck: open it locally

The Studio soundcheck needs the backend socket and the relay's test mount.
`frontend/scripts/studio-mock.mjs` stands in for both, so the page runs
without the backend, Icecast and Liquidsoap. It needs `ffmpeg` and Node 22+.

## Run

Two terminals, in `frontend/`:

```bash
node scripts/studio-mock.mjs
```

```bash
VITE_STREAM_ICECAST_TEST_URL=http://localhost:8000/test.mp3 npm run dev
```

Open http://localhost:3000/admin/#/stream/live?show=1

There is no login: the mock signs in one host (`local-host`) with one live
episode that started a minute ago, so the pill reads "Soundcheck · it is air
time" and Go Live opens after a confirmed soundcheck. Allow the microphone,
pick it under "Audio input", then click "Run soundcheck".

In Claude Code the same two servers are in `.claude/launch.json`
(`studio-mock`, `frontend-studio`).

## What the mock does

| Part | Mock |
|---|---|
| `/api/*` | One host and one episode. Other calls answer 404 and are logged. |
| `/ws/stream` | Counts the audio chunks the browser sends and reports them in each pong, like the backend. |
| `/test.mp3` | The test broadcast as MP3 through `ffmpeg`, like the relay. Sent with CORS, so the page can measure it. |

## Failure paths

Switch the mode while the mock runs, then click "Run again":

```bash
curl http://localhost:8000/mock/mode/no-chunks
```

| Mode | What happens | Result on the page |
|---|---|---|
| `ok` | Everything works. | "Sounds good — ready to go live" |
| `no-chunks` | The socket is up, but no audio arrives at the server. | "The server got no audio." |
| `no-return` | The server gets audio, the relay sends nothing back. | "Your audio did not come back from the relay." |
| `no-cors` | The relay sends no CORS header. | The page plays the return feed and asks "Do you hear your audio?" |

Without a mode switch: mute the microphone for "No signal", turn the input
gain far down for "Too quiet".

## Limits

- The mock is not the backend. For the real chain use
  `docs/stream-rework/local-test-harness/` with `cargo run` in `backend/`.
- Go Live works against the mock, but nothing is recorded or published.
