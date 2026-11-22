// Local stand-in for the backend and the relay, to open the Studio soundcheck
// without the stream stack. See docs/stream-rework/studio-soundcheck-local.md.
//
//   node scripts/studio-mock.mjs          (in frontend/, port 8000)
//
// - /api/*: one host, always signed in, with one live episode at air time.
// - /ws/stream: counts the audio chunks the browser sends and reports them in
//   each pong, like the backend.
// - /test.mp3: the test broadcast as MP3 (through ffmpeg), like the relay.
// - /mock/mode/<mode>: ok | no-chunks | no-return | no-cors. It also ends the
//   show and signs in the host again, so each test starts off air.
// - /mock/role/<role>: sign in as host or admin.
// - /mock/viewer/<id>: sign in as user 1 (the episode's host) or another host.
// - PUT /api/shows/1/studio-log: what the Studio and the live panel saved;
//   GET /api/shows/1 returns it, like the backend.
// - /mock/live: how many LIVE broadcasts are open, and the audio chunks each got.
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { WebSocketServer } from 'ws';

const PORT = Number(process.env.PORT ?? 8000);
const MODES = ['ok', 'no-chunks', 'no-return', 'no-cors'];
/** What ffmpeg says about a WebM stream that ends inside a block. */
const TAIL_CUT = /File ended prematurely|Error parsing Opus packet header/;
let mode = 'ok';

const HOST_ID = 1;
const user = { id: HOST_ID, username: 'local-host', role: 'host', has_show: true };
/** What the host's browser saved for the episode. */
let studioLog = {};

// Like the backend: the show is on air while a LIVE broadcast is open, and
// for 5 minutes after one dropped without "stop".
const GRACE_MS = 300_000;
const live = new Set();
let droppedAt = -Infinity;
const onAir = () => live.size > 0 || Date.now() - droppedAt < GRACE_MS;

function endShow() {
  const open = [...live];
  studioLog = {};
  live.clear();
  droppedAt = -Infinity;
  for (const ws of open) ws.terminate();
}

/** One live episode that started a minute ago, Berlin time. */
function show() {
  const berlin = (date) =>
    new Intl.DateTimeFormat('sv-SE', {
      timeZone: 'Europe/Berlin',
      dateStyle: 'short',
      timeStyle: 'short',
    })
      .format(date)
      .split(' ');
  const [date, start] = berlin(new Date(Date.now() - 60_000));
  // Ends at 23:59, so it never runs over midnight.
  return {
    id: 1,
    title: 'Brunchtime',
    date,
    start_time: start,
    end_time: '23:59',
    show_type: 'external',
    stream_mode: 'live',
    status: 'scheduled',
    artists: [],
    on_air: onAir(),
    // The host's own episode, so /shows/1 opens its episode page with the Studio tab.
    host_user_id: HOST_ID,
    created_by: HOST_ID,
    host_username: 'local-host',
    episode_status: 'upcoming',
    studio_log: studioLog,
    listener_samples: [],
  };
}

const routes = {
  'POST /api/auth/login': () => ({ user }),
  'GET /api/auth/me': () => user,
  'GET /api/my-show': () =>
    user.id === HOST_ID ? { assigned: true, shows: [show()] } : { assigned: false, shows: [] },
  'GET /api/shows/1/announcements/status': () => [],
  'GET /api/shows/1': () => show(),
  'GET /api/shows/1/announcements': () => [],
  'GET /api/shows/1/announcements/meta': () => ({
    paused: false,
    needs_approval: false,
    services: { soundcloud: true, instagram: true },
  }),
  'GET /api/stream/status': () => ({ active: false }),
  'GET /api/stream/metrics': () => ({ online: true, total_listeners: 0, mounts: [] }),
  'GET /api/recording/status': () => ({ is_recording: false }),
};

/** Browsers listening to /test.mp3. */
const listeners = new Set();

const server = createServer((req, res) => {
  const path = req.url.split('?')[0];
  if (path.startsWith('/mock/mode/')) {
    const wanted = path.slice('/mock/mode/'.length);
    if (MODES.includes(wanted)) mode = wanted;
    endShow();
    user.role = 'host';
    user.id = HOST_ID;
    console.log(`mode: ${mode}`);
    res.writeHead(200, { 'content-type': 'text/plain' }).end(`mode: ${mode}\n`);
    return;
  }
  if (path.startsWith('/mock/role/')) {
    const wanted = path.slice('/mock/role/'.length);
    if (wanted === 'host' || wanted === 'admin') user.role = wanted;
    res.writeHead(200, { 'content-type': 'text/plain' }).end(`role: ${user.role}\n`);
    return;
  }
  if (path.startsWith('/mock/viewer/')) {
    user.id = Number(path.slice('/mock/viewer/'.length)) || HOST_ID;
    res.writeHead(200, { 'content-type': 'text/plain' }).end(`viewer: ${user.id}\n`);
    return;
  }
  if (req.method === 'PUT' && path === '/api/shows/1/studio-log') {
    let body = '';
    req.on('data', (chunk) => (body += chunk));
    req.on('end', () => {
      Object.assign(studioLog, JSON.parse(body));
      res.writeHead(204).end();
    });
    return;
  }
  if (path === '/mock/live') {
    const chunks = [...live].map((ws) => ws.chunks);
    res
      .writeHead(200, { 'content-type': 'application/json' })
      .end(JSON.stringify({ live: live.size, chunks }));
    return;
  }
  if (path === '/test.mp3') {
    const headers = { 'content-type': 'audio/mpeg', 'cache-control': 'no-store' };
    if (mode !== 'no-cors') headers['access-control-allow-origin'] = '*';
    res.writeHead(200, headers);
    listeners.add(res);
    req.on('close', () => listeners.delete(res));
    return;
  }
  const route = routes[`${req.method} ${path}`];
  if (!route) console.log(`not mocked: ${req.method} ${path}`);
  res.writeHead(route ? 200 : 404, { 'content-type': 'application/json' });
  res.end(JSON.stringify(route ? route() : { error: 'not mocked' }));
});

const sockets = new WebSocketServer({ server, path: '/ws/stream' });
sockets.on('connection', (ws, req) => {
  const test = new URL(req.url, 'http://x').searchParams.get('test') === 'true';
  let chunks = 0;
  ws.chunks = 0;
  let bytes = 0;
  let stream = null;
  let ended = false;
  let stopped = false;
  if (!test) live.add(ws);
  // The relay: WebM/Opus in, MP3 out to every listener. Started on the first
  // chunk: an ffmpeg that gets no data at all fails with "Error opening input".
  let relay = null;
  function startRelay() {
    const ffmpeg = spawn(
      'ffmpeg',
      ['-loglevel', 'error', '-i', 'pipe:0', '-vn', '-c:a', 'libmp3lame', '-b:a', '128k'].concat([
        '-f',
        'mp3',
        'pipe:1',
      ]),
      { stdio: ['pipe', 'pipe', 'pipe'] }
    );
    ffmpeg.stdin.on('error', () => {});
    ffmpeg.stdout.on('data', (data) => {
      if (mode === 'no-return' || mode === 'no-chunks') return;
      for (const listener of listeners) listener.write(data);
    });
    createInterface({ input: ffmpeg.stderr }).on('line', (line) => {
      // The Studio closes the socket before the recorder's last chunk, so the
      // broadcast can end inside a WebM block: expected, not an error.
      if (ended && TAIL_CUT.test(line)) return;
      console.error(line);
    });
    return ffmpeg;
  }
  console.log(`${test ? 'test broadcast' : 'LIVE broadcast'} connected (mode ${mode})`);
  ws.send('connected');

  ws.on('message', (data, isBinary) => {
    if (isBinary) {
      // no-chunks: the socket is up, but the audio never arrives.
      if (mode === 'no-chunks') return;
      ws.chunks = ++chunks;
      bytes += data.length;
      relay ??= startRelay();
      relay.stdin.write(data);
      return;
    }
    const text = data.toString();
    if (text.startsWith('ping:')) {
      const echo = Number(text.slice(5));
      ws.send(`pong:${JSON.stringify({ echo, chunks, bytes, late: 0, stream, offset: bytes })}`);
    } else if (text.startsWith('stream:')) {
      stream = text.slice(7);
    } else if (text === 'stop') {
      stopped = true;
      ws.close(1000);
    }
  });
  ws.on('close', () => {
    console.log(`broadcast ended: ${chunks} chunks / ${bytes} bytes received`);
    ended = true;
    // endShow() already took it off air.
    if (live.delete(ws)) droppedAt = stopped ? -Infinity : Date.now();
    relay?.stdin.end();
  });
});

server.listen(PORT, '127.0.0.1', () =>
  console.log(`Studio mock on http://localhost:${PORT} (mode ${mode})`)
);
