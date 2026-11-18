import { ref } from 'vue';

/**
 * The Studio soundcheck (flow screens `studio`, `studio-checking`,
 * `studio-ready`, `studio-problem`): the host tests the music and the mic,
 * then the server and the return feed are checked and a result is proposed. The browser and server parts are passed in, so the sequence runs in
 * a test with fake sources and timers.
 */

/** A peak under this is no signal: a muted or dead input. The meter floor is −60. */
export const SILENT_DB = -55;
/** A peak under this is too quiet to send. */
export const QUIET_DB = -30;
/** A peak over this clips. */
export const CLIP_DB = -1;
/** The return feed carries a signal above this peak. */
export const RETURN_SIGNAL_DB = -50;
/** Chunks the server must confirm: one second of the recorder's 250 ms chunks. */
export const SERVER_MIN_CHUNKS = 4;

/** How long the page listens to the music: time to start a track. */
export const MUSIC_MS = 10000;
/** How long the page listens to the mic. */
export const MIC_MS = 5000;
/** The server reports its counters with each pong, every 2 s. */
export const SERVER_TIMEOUT_MS = 10000;
/** The relay runs a few seconds behind. */
export const RETURN_TIMEOUT_MS = 20000;
const POLL_MS = 250;

export type SoundcheckStepId = 'music' | 'mic' | 'server' | 'return';
export type SoundcheckStepState = 'pending' | 'running' | 'ok' | 'warn' | 'fail' | 'skipped';
export interface SoundcheckStep {
  id: SoundcheckStepId;
  name: string;
  state: SoundcheckStepState;
  detail: string;
}
/** `manual`: the return feed plays but cannot be measured, so the host decides. */
export type SoundcheckOutcome = 'ok' | 'manual' | 'problem';

/** The test broadcast as it comes back from the relay. */
export interface ReturnFeed {
  /** Its peak in dBFS; null when the browser may not read it (no CORS). */
  peakDb(): number | null;
  /** True once audio data arrived and plays. */
  playing(): boolean;
  /** Send it to the speakers, or keep it silent. */
  setAudible(on: boolean): void;
  close(): void;
}

export interface SoundcheckDeps {
  /** Level of the audio that would be sent, in dBFS; null without an input. */
  inputLevel(): { rmsDb: number; peakDb: number } | null;
  inputLabel(): string;
  /** Start the private test broadcast. Rejects with the reason. */
  startBroadcast(): Promise<void>;
  stopBroadcast(): void;
  /** The server's reason when it ended the test broadcast. */
  broadcastError(): string | null;
  /** Chunks the browser sent. */
  sentChunks(): number;
  /** Chunks the server confirmed. */
  serverChunks(): number;
  /** Called in the click itself, so the browser lets it play. */
  openReturn(): ReturnFeed;
  setPassed(passed: boolean): void;
  wait?(ms: number): Promise<void>;
}

const NAMES: Record<SoundcheckStepId, string> = {
  music: 'Music',
  mic: 'Mic',
  server: 'Server',
  return: 'Return feed',
};

/**
 * Music and mic come in on the same audio input, so the host starts a check
 * for each and the page measures what arrives. It cannot tell which source
 * it was.
 */
const SOURCES = {
  music: { name: 'The music', ms: MUSIC_MS, prompt: 'Play your music now.' },
  mic: { name: 'The mic', ms: MIC_MS, prompt: 'Now talk into the mic.' },
} as const;
export type SoundcheckSource = keyof typeof SOURCES;
const SOURCE_IDS = Object.keys(SOURCES) as SoundcheckSource[];

/** "−9 dB", with a real minus sign. */
export function formatDb(db: number): string {
  return `${db < 0 ? '−' : ''}${Math.abs(Math.round(db))} dB`;
}

const freshStep = (id: SoundcheckStepId): SoundcheckStep => ({
  id,
  name: NAMES[id],
  state: 'pending',
  detail: 'Not checked yet',
});

const CANCELLED = Symbol('cancelled');

export function useSoundcheck(deps: SoundcheckDeps) {
  const wait = deps.wait ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));

  const steps = ref<SoundcheckStep[]>((Object.keys(NAMES) as SoundcheckStepId[]).map(freshStep));
  const running = ref(false);
  const outcome = ref<SoundcheckOutcome | null>(null);
  /** The first problem, with its fix. */
  const problem = ref<string | null>(null);
  /** The server received audio, so the host may confirm against the proposal. */
  const canOverride = ref(false);
  /** What the host should do now. */
  const prompt = ref<string | null>(null);
  const listening = ref(false);
  const listenError = ref<string | null>(null);

  // Each check and each teardown gets a new number; older ones stop at their next step.
  let run = 0;
  let epoch = 0;
  let broadcasting = false;
  let feed: ReturnFeed | null = null;
  // The problem each check found, so a re-run of one check keeps the others'.
  const notes: Partial<Record<SoundcheckStepId, string>> = {};

  const step = (id: SoundcheckStepId) => steps.value.find((s) => s.id === id)!;

  function set(id: SoundcheckStepId, state: SoundcheckStepState, detail: string, note?: string) {
    Object.assign(step(id), { state, detail });
    if (note) notes[id] = note;
    else delete notes[id];
  }

  function clear(...ids: SoundcheckStepId[]) {
    for (const id of ids) {
      Object.assign(step(id), freshStep(id));
      delete notes[id];
    }
  }

  /** A check changed: the old result and the old server and relay checks are gone. */
  function forgetResult() {
    clear('server', 'return');
    outcome.value = null;
    problem.value = null;
    canOverride.value = false;
    listenError.value = null;
    deps.setPassed(false);
  }

  async function tick(token: number) {
    await wait(POLL_MS);
    if (token !== run) throw CANCELLED;
  }

  async function until(token: number, timeoutMs: number, done: () => boolean): Promise<boolean> {
    for (let t = 0; t < timeoutMs; t += POLL_MS) {
      if (done()) return true;
      await tick(token);
    }
    return done();
  }

  async function ensureBroadcast() {
    if (broadcasting) return;
    const started = epoch;
    await deps.startBroadcast();
    // Torn down while it connected: do not leave it running.
    if (started !== epoch) {
      deps.stopBroadcast();
      return;
    }
    broadcasting = true;
  }

  /** Stop the test broadcast and the return feed. */
  function teardown() {
    epoch++;
    if (broadcasting) deps.stopBroadcast();
    broadcasting = false;
    feed?.close();
    feed = null;
    listening.value = false;
  }

  /** Measure one source for its time. */
  async function checkSource(token: number, id: SoundcheckSource) {
    const source = SOURCES[id];
    if (!deps.inputLevel()) {
      set(
        id,
        'fail',
        'No audio input',
        'No audio input. Pick your input under “Audio input” and allow the microphone.'
      );
      return;
    }
    set(id, 'running', 'Listening…');
    prompt.value = source.prompt;
    let peak = -Infinity;
    let sum = 0;
    let count = 0;
    for (let elapsed = 0; elapsed < source.ms; elapsed += POLL_MS) {
      const level = deps.inputLevel();
      if (level) {
        peak = Math.max(peak, level.peakDb);
        sum += level.rmsDb;
        count++;
      }
      await tick(token);
    }
    if (peak < SILENT_DB) {
      set(
        id,
        'fail',
        'No signal',
        `${source.name} did not arrive from ${deps.inputLabel()}. Turn it on or turn up its channel, then run its test again.`
      );
    } else if (peak > CLIP_DB) {
      set(
        id,
        'warn',
        `Too loud · peak ${formatDb(peak)}`,
        `${source.name} is too loud: peak ${formatDb(peak)} clips. Turn down its gain until peaks stay under −3 dB.`
      );
    } else if (peak < QUIET_DB) {
      set(
        id,
        'warn',
        `Too quiet · peak ${formatDb(peak)}`,
        `${source.name} is too quiet: peak ${formatDb(peak)}. Turn up its gain until peaks reach about −12 dB.`
      );
    } else {
      set(id, 'ok', `Peak ${formatDb(peak)} · average ${formatDb(sum / count)}`);
    }
  }

  /** The test broadcast: does the server get it, does it come back? */
  async function checkChain(token: number) {
    prompt.value = 'Keep the sound on until the checks are done.';
    set('server', 'running', 'Connecting…');
    try {
      await ensureBroadcast();
    } catch (e) {
      if (token !== run) throw CANCELLED;
      const reason = e instanceof Error ? e.message : 'Connection error';
      set('server', 'fail', 'Not connected', `The test broadcast did not start: ${reason}`);
      return;
    }
    if (token !== run) throw CANCELLED;
    set('server', 'running', 'Sending a test broadcast…');
    const arrived = await until(
      token,
      SERVER_TIMEOUT_MS,
      () => deps.serverChunks() >= SERVER_MIN_CHUNKS || deps.broadcastError() !== null
    );
    const ended = deps.broadcastError();
    if (ended) {
      set(
        'server',
        'fail',
        'Test broadcast ended',
        `The server ended the test broadcast: ${ended}`
      );
      return;
    }
    if (!arrived && deps.sentChunks() === 0) {
      set(
        'server',
        'fail',
        'No audio sent',
        'The browser sent no audio. Pick your audio input again, then run a test again.'
      );
      return;
    }
    if (!arrived) {
      set(
        'server',
        'fail',
        'No audio arrived',
        'The server got no audio. Check your internet connection, then run a test again.'
      );
      return;
    }
    set('server', 'ok', 'Received your audio');
    canOverride.value = true;

    set('return', 'running', 'Waiting for the relay…');
    const signal = () => (feed?.peakDb() ?? -Infinity) > RETURN_SIGNAL_DB;
    const unmeasured = () => !!feed?.playing() && feed.peakDb() === null;
    await until(token, RETURN_TIMEOUT_MS, () => signal() || unmeasured());
    if (signal()) {
      set('return', 'ok', 'Your audio comes back');
    } else if (unmeasured()) {
      set('return', 'warn', 'Playing · not measured in this browser');
    } else {
      set(
        'return',
        'fail',
        feed?.playing() ? 'Only silence came back' : 'Nothing came back',
        'Your audio did not come back from the relay. Run a test again; if it stays, tell the tech team.'
      );
    }
  }

  /** Once music and mic are both tested or skipped: check the chain and propose the result. */
  async function conclude(token: number) {
    const firstNote = () => steps.value.map((s) => notes[s.id]).find(Boolean) ?? null;
    // A problem shows at once, also while the other source is still open.
    problem.value = firstNote();
    const sources = SOURCE_IDS.map(step);
    if (sources.some((s) => s.state === 'pending')) return;
    const heard = sources.some((s) => s.state === 'ok' || s.state === 'warn');
    if (heard) await checkChain(token);
    problem.value = firstNote();
    const manual = step('return').state === 'warn';
    if (problem.value) {
      outcome.value = 'problem';
    } else if (!heard) {
      problem.value = 'Music and mic were both skipped. Test at least one.';
      outcome.value = 'problem';
    } else if (manual) {
      // Not measured: play it, the host decides.
      outcome.value = 'manual';
      listening.value = true;
      feed?.setAudible(true);
    } else {
      outcome.value = 'ok';
      deps.setPassed(true);
    }
  }

  /** Run `work` as the one thing that runs now. */
  async function only(work: (token: number) => Promise<void>) {
    const token = ++run;
    forgetResult();
    running.value = true;
    // Opened in the click, so the browser lets it play later.
    feed ??= deps.openReturn();
    try {
      await work(token);
    } catch (e) {
      if (e === CANCELLED) return;
      throw e;
    }
    running.value = false;
    prompt.value = null;
    if (!listening.value) teardown();
  }

  /** Start test, or Re-run: measure the music or the mic. */
  function test(id: SoundcheckSource) {
    return only(async (token) => {
      await checkSource(token, id);
      await conclude(token);
    });
  }

  /** The show has no music, or no mic. */
  function skip(id: SoundcheckSource) {
    return only(async (token) => {
      set(id, 'skipped', 'Not in this show');
      await conclude(token);
    });
  }

  /** Stop the check that runs. A source that was being tested is open again. */
  function stop() {
    run++;
    running.value = false;
    prompt.value = null;
    for (const s of steps.value) if (s.state === 'running') clear(s.id);
    if (!listening.value) teardown();
  }

  /** Forget everything, for example after a new audio input. */
  function reset() {
    run++;
    teardown();
    running.value = false;
    prompt.value = null;
    clear('music', 'mic');
    forgetResult();
  }

  /** The host confirms: "Sounds right", or "Confirm anyway" against a problem. */
  function confirm() {
    deps.setPassed(true);
  }

  /** The host takes the confirmation back. */
  function reject() {
    deps.setPassed(false);
    outcome.value = 'problem';
    problem.value =
      'You marked the soundcheck as not OK. Change your setup, then run a test again.';
  }

  /** Optional: hear the return feed. It keeps the test broadcast up. */
  async function setListening(on: boolean) {
    listenError.value = null;
    if (!on) {
      listening.value = false;
      feed?.setAudible(false);
      if (!running.value) teardown();
      return;
    }
    listening.value = true;
    feed ??= deps.openReturn();
    feed.setAudible(true);
    const started = epoch;
    try {
      await ensureBroadcast();
    } catch (e) {
      if (started !== epoch) return;
      listenError.value = e instanceof Error ? e.message : 'Could not play the return feed.';
      if (!running.value) teardown();
    }
  }

  /** Leave: stop everything, keep the result. */
  function dispose() {
    stop();
    teardown();
  }

  return {
    steps,
    running,
    outcome,
    problem,
    canOverride,
    prompt,
    listening,
    listenError,
    test,
    skip,
    stop,
    reset,
    confirm,
    reject,
    setListening,
    dispose,
  };
}

/**
 * The relay's test mount as a ReturnFeed. With CORS on the mount the audio
 * goes through an analyser and is silent until `setAudible`. Without CORS it
 * plays muted and cannot be measured. A mount that is not up yet is tried
 * again every second.
 */
export function createReturnFeed(url: string, context: AudioContext): ReturnFeed {
  let el: HTMLAudioElement | null = null;
  let source: MediaElementAudioSourceNode | null = null;
  let analyser: AnalyserNode | null = null;
  let retry: ReturnType<typeof setTimeout> | null = null;
  let audible = false;
  let closed = false;
  const samples = new Float32Array(2048);

  function drop() {
    if (el) {
      el.onerror = null;
      el.pause();
      el.removeAttribute('src');
      el.load();
      el = null;
    }
    source?.disconnect();
    analyser?.disconnect();
    source = null;
    analyser = null;
  }

  function route() {
    if (analyser) {
      if (audible) analyser.connect(context.destination);
      else analyser.disconnect();
    } else if (el) {
      el.muted = !audible;
    }
  }

  function open(withCors: boolean) {
    drop();
    if (closed) return;
    const audio = new Audio();
    el = audio;
    audio.preload = 'none';
    // Join the live edge, not a cached start.
    audio.src = url + (url.includes('?') ? '&' : '?') + '_=' + Date.now();
    if (withCors) {
      audio.crossOrigin = 'anonymous';
      source = context.createMediaElementSource(audio);
      analyser = context.createAnalyser();
      analyser.fftSize = samples.length;
      source.connect(analyser);
    }
    route();
    audio.onerror = () => {
      if (withCors) open(false);
      else retry = setTimeout(() => open(true), 1000);
    };
    void context.resume();
    audio.play().catch(() => {});
  }

  open(true);

  return {
    peakDb() {
      if (!analyser) return null;
      analyser.getFloatTimeDomainData(samples);
      let max = 0;
      for (const s of samples) max = Math.max(max, Math.abs(s));
      return max > 0 ? 20 * Math.log10(max) : -Infinity;
    },
    playing: () => !!el && el.currentTime > 0,
    setAudible(on) {
      audible = on;
      route();
    },
    close() {
      closed = true;
      if (retry) clearTimeout(retry);
      drop();
    },
  };
}
