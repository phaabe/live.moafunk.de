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
// - MOCK_RECORDING=<mp3>: episode 1 has that file as its recording, for the
//   Edit recording page (/admin/#/shows/1/edit-recording): edit, peaks (ffmpeg)
//   and /mock/recording.mp3 with CORS and Range, like R2. Apply only pretends.
// - MOCK_EPISODE=after (with MOCK_RECORDING, best a 30 min file): episode 1
//   ended yesterday, its recording edited and private on SoundCloud, for the
//   Recording tab (/admin/#/shows/1). Picking the file and Private / Public work.
// - MOCK_EPISODE=soon: episode 1 is in 5 days at 19:00, for the Social media
//   tiles and Book a time (#1091). Announcements live in memory: set up, book
//   a time (pre: 7 days before, post: 14 days after; every third day is full)
//   and cancel work.
import { createReadStream, statSync } from 'node:fs';
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
/** Moafunkbot switches on the admin Config page. */
const botSwitches = { enabled: true, live_chat: true };
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
    // Like the backend: an admin or the episode's host runs it (#1047).
    can_edit: user.role === 'admin' || user.id === HOST_ID,
    can_manage: user.role === 'admin' || user.id === HOST_ID,
    episode_status: 'upcoming',
    studio_log: studioLog,
    listener_samples: [],
    ...(AFTER ? after(berlin(new Date(Date.now() - 86_400_000))[0]) : {}),
    ...(SOON
      ? { date: berlin(new Date(Date.now() + 5 * 86_400_000))[0], start_time: '19:00', end_time: '21:00' }
      : {}),
  };
}

// ---- Social media (#1091): announcements in memory ----
const SOON = process.env.MOCK_EPISODE === 'soon';
let announcements = [];

/** 12:00 and 18:00 Berlin (UTC+1 in winter, rough) in the slot's window; every third day full. */
function feedTimes(slot) {
  const { date, start_time } = show();
  const start = new Date(`${date}T${start_time}:00+01:00`);
  const out = [];
  for (let d = -8; d <= 15; d++) {
    const day = new Date(start.getTime() + d * 86_400_000).toISOString().slice(0, 10);
    for (const hh of ['11', '17']) {
      const at = new Date(`${day}T${hh}:00:00Z`);
      const before = at < start && at > new Date(start.getTime() - 7 * 86_400_000);
      const after = at > start && at <= new Date(start.getTime() + 14 * 86_400_000);
      if (at <= new Date() || !(slot === 'pre' ? before : after)) continue;
      const full = Math.abs(d) % 3 === 1;
      out.push({ scheduled_at: at.toISOString().replace('.000', ''), free: !full, reason: full ? 'Full' : null });
    }
  }
  const pick = out.filter((t) => t.free)[slot === 'pre' ? 2 : 3] ?? out.find((t) => t.free);
  if (pick) pick.default = true;
  return out;
}

function announcement(slot, fields) {
  const old = announcements.find((a) => a.slot === slot) ?? {};
  const row = {
    id: { pre: 1, onair: 2, post: 3 }[slot], show_id: 1, slot, channel: 'instagram',
    state: 'draft', scheduled_at: null, requested_at: null, headline: '', caption: '',
    image_source: 'cover', image_key: null, layout: 'band', collaborator: true,
    extra_collaborators: [], user_tags: [], sent_at: null, permalink: null, error: null,
    send_now: false, updated_at: new Date().toISOString(), ...old, ...fields,
  };
  announcements = [...announcements.filter((a) => a.slot !== slot), row];
  return row;
}

/** The same episode the day after: recording edited, private on SoundCloud (#1082). */
function after(date) {
  const url = `http://localhost:${PORT}/mock/recording.mp3`;
  const edit = recording.edit;
  const done = recording.status === 'done' && edit;
  return {
    date,
    start_time: '12:00',
    end_time: '14:00',
    on_air: false,
    episode_status: 'wrapup_open',
    latest_recording: { id: 1, version: 'v1', status: 'raw', duration_ms: 1_800_000, download_url: url },
    recording_source: recordingSource,
    recording_edit_status: recording.status,
    recording_edited_at: `${date} 13:00:00`,
    recording_edited_seconds: done ? edit.end - edit.start : null,
    edited_recording_url: done ? url : null,
    recording_download_url: url,
    recording_original_download_url: done ? url : null,
    ...soundcloud,
  };
}

const routes = {
  'POST /api/auth/login': () => ({ user }),
  'GET /api/auth/me': () => user,
  'GET /api/settings/notifications': () => botSwitches,
  'GET /api/settings/services': () => ({ soundcloud: true, instagram: true }),
  'GET /api/my-show': () =>
    user.id === HOST_ID ? { assigned: true, shows: [show()] } : { assigned: false, shows: [] },
  'GET /api/shows/1/announcements/status': () => announcements.filter((a) => a.state !== 'draft'),
  'GET /api/shows/1': () => show(),
  'GET /api/shows/1/announcements': () => announcements,
  'GET /api/shows/1/announcements/meta': () => ({
    paused: false,
    needs_approval: false,
    post_now: true,
    services: { soundcloud: true, instagram: true },
  }),
  'GET /api/series/brunchtime': () => ({ slug: 'brunchtime', name: 'Brunchtime', instagram_handle: 'brunchtime', episodes: [] }),
  'GET /api/stream/status': () => ({ active: false }),
  'GET /api/stream/metrics': () => ({ online: true, total_listeners: 0, mounts: [] }),
  'GET /api/recording/status': () => ({ is_recording: false }),
};

/** Browsers listening to /test.mp3. */
const listeners = new Set();

// ---- Edit recording (#883) ----
const RECORDING = process.env.MOCK_RECORDING ?? null;
const recording = { edit: null, status: 'none', peaks: null, reading: false };
const AFTER = !!RECORDING && process.env.MOCK_EPISODE === 'after';
let recordingSource = 'show';
const soundcloud = {
  soundcloud_url: 'https://soundcloud.com/radio-moafunk/brunchtime-1',
  soundcloud_public: false,
  soundcloud_uploaded_at: null,
  soundcloud_source: 'show',
  soundcloud_edited: true,
  soundcloud_current: true,
};
if (AFTER) {
  // An applied edit of a 30 min file: cut, one louder part, two snippets.
  recording.edit = {
    start: 60,
    end: 1740,
    gainDb: 2,
    sections: [{ id: 'a', start: 450, end: 540, gainDb: 4 }],
    snippets: [
      { slot: 1, start: 720, length: 30 },
      { slot: 2, start: 1080, length: 20 },
    ],
  };
  recording.status = 'done';
  soundcloud.soundcloud_uploaded_at = new Date(Date.now() - 82_800_000)
    .toISOString()
    .replace('T', ' ')
    .slice(0, 19);
}

/** Peaks like the backend: 8 kHz mono, loudest sample per 1/20 s, 0..1. */
function readPeaks() {
  recording.reading = true;
  const ffmpeg = spawn('ffmpeg', ['-v', 'error', '-i', RECORDING, '-ac', '1', '-ar', '8000', '-f', 's16le', '-']);
  const peaks = [];
  let rest = Buffer.alloc(0);
  let samples = 0;
  ffmpeg.stdout.on('data', (chunk) => {
    const data = Buffer.concat([rest, chunk]);
    const whole = data.length - (data.length % 800);
    for (let i = 0; i < whole; i += 800) {
      let max = 0;
      for (let k = i; k < i + 800; k += 2) max = Math.max(max, Math.abs(data.readInt16LE(k)));
      peaks.push(Math.round((max / 32768) * 100) / 100);
    }
    samples += whole / 2;
    rest = data.subarray(whole);
  });
  ffmpeg.on('close', () => {
    recording.peaks = { duration: samples / 8000, per_second: 20, peaks };
    recording.reading = false;
    console.log(`recording peaks: ${peaks.length}`);
  });
}

function readJson(req, done) {
  let body = '';
  req.on('data', (chunk) => (body += chunk));
  req.on('end', () => done(JSON.parse(body || '{}')));
}

function editState() {
  if (!RECORDING) return { source: null, edit: null, status: 'none', error: null, edited_url: null, rendered_at: null };
  return {
    source: { url: `http://localhost:${PORT}/mock/recording.mp3`, duration: recording.peaks?.duration ?? null },
    edit: recording.edit,
    status: recording.status,
    error: null,
    edited_url: null,
    rendered_at: null,
  };
}

/** The recording with CORS and Range, so the editor can seek and use Web Audio. */
function serveRecording(req, res) {
  const size = statSync(RECORDING).size;
  const headers = {
    'content-type': 'audio/mpeg',
    'accept-ranges': 'bytes',
    'access-control-allow-origin': '*',
  };
  const m = /bytes=(\d*)-(\d*)/.exec(req.headers.range ?? '');
  if (!m) {
    res.writeHead(200, { ...headers, 'content-length': size });
    createReadStream(RECORDING).pipe(res);
    return;
  }
  const start = m[1] ? Number(m[1]) : 0;
  const end = m[2] ? Math.min(Number(m[2]), size - 1) : size - 1;
  res.writeHead(206, {
    ...headers,
    'content-range': `bytes ${start}-${end}/${size}`,
    'content-length': end - start + 1,
  });
  createReadStream(RECORDING, { start, end }).pipe(res);
}

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
  if (RECORDING && path === '/mock/recording.mp3') {
    serveRecording(req, res);
    return;
  }
  if (path === '/api/shows/1/recording/peaks') {
    if (!RECORDING) return res.writeHead(404).end('{"error":"no recording"}');
    if (!recording.peaks && !recording.reading) readPeaks();
    res.writeHead(recording.peaks ? 200 : 202, { 'content-type': 'application/json' });
    res.end(JSON.stringify(recording.peaks ?? { status: 'processing' }));
    return;
  }
  if (path === '/api/shows/1/recording/edit' && req.method === 'PUT') {
    let body = '';
    req.on('data', (chunk) => (body += chunk));
    req.on('end', () => {
      recording.edit = JSON.parse(body);
      recording.status = 'draft';
      res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify(editState()));
    });
    return;
  }
  if (path === '/api/shows/1/recording/edit/apply' && req.method === 'POST') {
    recording.status = 'rendering';
    setTimeout(() => (recording.status = 'done'), 5000);
    res.writeHead(202, { 'content-type': 'application/json' }).end('{"status":"rendering"}');
    return;
  }
  if (path === '/api/shows/1/recording/edit') {
    res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify(editState()));
    return;
  }
  if (AFTER && req.method === 'PUT' && path === '/api/shows/1/recording/source') {
    readJson(req, (body) => {
      recordingSource = body.source;
      soundcloud.soundcloud_current = body.source === soundcloud.soundcloud_source;
      res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify(body));
    });
    return;
  }
  if (AFTER && req.method === 'POST' && path === '/api/shows/1/soundcloud/privacy') {
    readJson(req, (body) => {
      soundcloud.soundcloud_public = !!body.public;
      res
        .writeHead(200, { 'content-type': 'application/json' })
        .end(JSON.stringify({ success: true, track_url: soundcloud.soundcloud_url }));
    });
    return;
  }
  if (path === '/api/shows/1/announcements/slots') {
    const slot = new URL(req.url, 'http://x').searchParams.get('slot');
    res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify(feedTimes(slot)));
    return;
  }
  const booked = path.match(/^\/api\/shows\/1\/announcements\/(pre|post)\/time$/);
  if (booked && req.method === 'PUT') {
    readJson(req, (body) => {
      const old = announcements.find((a) => a.slot === booked[1]);
      const state = !old || ['draft', 'cancelled'].includes(old.state) ? 'draft'
        : old.state === 'waiting_soundcloud' ? 'scheduled' : old.state;
      const row = announcement(booked[1], { state, scheduled_at: body.scheduled_at, requested_at: null });
      res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify(row));
    });
    return;
  }
  const slotOf = path.match(/^\/api\/shows\/1\/announcements\/(pre|onair|post)$/);
  if (slotOf && req.method === 'PUT') {
    readJson(req, (body) => {
      const { date, start_time } = show();
      const at = slotOf[1] === 'onair' ? new Date(`${date}T${start_time}:00+01:00`).toISOString() : body.scheduled_at ?? null;
      const state = slotOf[1] === 'onair' ? 'armed' : at ? 'scheduled' : 'waiting_soundcloud';
      const row = announcement(slotOf[1], { ...body, state, scheduled_at: at, requested_at: null });
      res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify(row));
    });
    return;
  }
  if (slotOf && req.method === 'DELETE') {
    const row = announcement(slotOf[1], { state: 'cancelled' });
    res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify(row));
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
  if (req.method === 'PUT' && path === '/api/settings/notifications') {
    readJson(req, (body) => {
      Object.assign(botSwitches, body);
      res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify(botSwitches));
    });
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
