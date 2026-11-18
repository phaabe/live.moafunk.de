import { ref } from 'vue';

/**
 * The Studio soundcheck (flow screens `studio`, `studio-ready`,
 * `studio-problem`): one click runs four checks by themselves and proposes a
 * result. The browser and server parts are passed in, so the sequence runs in
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

export const INPUT_MS = 1500;
export const LEVEL_MS = 4000;
/** The server reports its counters with each pong, every 2 s. */
export const SERVER_TIMEOUT_MS = 10000;
/** The relay runs a few seconds behind. */
export const RETURN_TIMEOUT_MS = 20000;
const POLL_MS = 250;

export type SoundcheckStepId = 'input' | 'level' | 'server' | 'return';
export type SoundcheckStepState = 'pending' | 'running' | 'ok' | 'warn' | 'fail';
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
  input: 'Input',
  level: 'Level',
  server: 'Server',
  return: 'Return feed',
};

/** "−9 dB", with a real minus sign. */
export function formatDb(db: number): string {
  return `${db < 0 ? '−' : ''}${Math.abs(Math.round(db))} dB`;
}

const freshSteps = (): SoundcheckStep[] =>
  (Object.keys(NAMES) as SoundcheckStepId[]).map((id) => ({
    id,
    name: NAMES[id],
    state: 'pending',
    detail: 'Not checked yet',
  }));

const CANCELLED = Symbol('cancelled');

export function useSoundcheck(deps: SoundcheckDeps) {
  const wait = deps.wait ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));

  const steps = ref<SoundcheckStep[]>(freshSteps());
  const running = ref(false);
  const outcome = ref<SoundcheckOutcome | null>(null);
  /** The first problem, with its fix. */
  const problem = ref<string | null>(null);
  /** The server received audio, so the host may confirm against the proposal. */
  const canOverride = ref(false);
  const listening = ref(false);
  const listenError = ref<string | null>(null);

  // Each run and each teardown gets a new number; older ones stop at their next step.
  let run = 0;
  let epoch = 0;
  let broadcasting = false;
  let feed: ReturnFeed | null = null;

  function set(id: SoundcheckStepId, state: SoundcheckStepState, detail: string) {
    const step = steps.value.find((s) => s.id === id)!;
    step.state = state;
    step.detail = detail;
  }

  function note(text: string) {
    problem.value ??= text;
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

  /** Peak and average level over `ms`. */
  async function measure(token: number, ms: number) {
    let peak = -Infinity;
    let sum = 0;
    let count = 0;
    await until(token, ms, () => {
      const level = deps.inputLevel();
      if (level) {
        peak = Math.max(peak, level.peakDb);
        sum += level.rmsDb;
        count++;
      }
      return false;
    });
    return { peak, average: count ? sum / count : -Infinity };
  }

  /** The checks in order. False: a check failed, the rest did not run. */
  async function checks(token: number): Promise<boolean> {
    set('input', 'running', 'Checking…');
    if (!deps.inputLevel()) {
      set('input', 'fail', 'No audio input');
      note('No audio input. Pick your input under “Audio input” and allow the microphone.');
      return false;
    }
    if ((await measure(token, INPUT_MS)).peak < SILENT_DB) {
      set('input', 'fail', 'No signal');
      note(`No signal from ${deps.inputLabel()}. Unmute it or play something, then run again.`);
      return false;
    }
    set('input', 'ok', `Signal from ${deps.inputLabel()}`);

    set('level', 'running', 'Measuring…');
    const { peak, average } = await measure(token, LEVEL_MS);
    if (peak > CLIP_DB) {
      set('level', 'warn', `Too loud · peak ${formatDb(peak)}`);
      note(
        `Too loud: peak ${formatDb(peak)} clips. Turn down the input gain until peaks stay under −3 dB.`
      );
    } else if (peak < QUIET_DB) {
      set('level', 'warn', `Too quiet · peak ${formatDb(peak)}`);
      note(
        `Too quiet: peak ${formatDb(peak)}. Turn up the input gain until peaks reach about −12 dB.`
      );
    } else {
      set('level', 'ok', `Peak ${formatDb(peak)} · average ${formatDb(average)}`);
    }

    set('server', 'running', 'Connecting…');
    try {
      await ensureBroadcast();
    } catch (e) {
      if (token !== run) throw CANCELLED;
      const reason = e instanceof Error ? e.message : 'Connection error';
      set('server', 'fail', 'Not connected');
      note(`The test broadcast did not start: ${reason}`);
      return false;
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
      set('server', 'fail', 'Test broadcast ended');
      note(`The server ended the test broadcast: ${ended}`);
      return false;
    }
    if (!arrived) {
      if (deps.sentChunks() === 0) {
        set('server', 'fail', 'No audio sent');
        note('The browser sent no audio. Pick your audio input again, then run again.');
      } else {
        set('server', 'fail', 'No audio arrived');
        note('The server got no audio. Check your internet connection, then run again.');
      }
      return false;
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
      set('return', 'fail', feed?.playing() ? 'Only silence came back' : 'Nothing came back');
      note(
        'Your audio did not come back from the relay. Run again; if it stays, tell the tech team.'
      );
    }
    return true;
  }

  /** One click: run all checks, then propose the result. */
  async function start() {
    const token = ++run;
    teardown();
    steps.value = freshSteps();
    outcome.value = null;
    problem.value = null;
    canOverride.value = false;
    listenError.value = null;
    deps.setPassed(false);
    running.value = true;
    // Opened in the click, so the browser lets it play later.
    feed = deps.openReturn();
    try {
      await checks(token);
    } catch (e) {
      if (e === CANCELLED) return;
      throw e;
    }
    running.value = false;
    const manual = steps.value.some((s) => s.id === 'return' && s.state === 'warn');
    if (problem.value) {
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
    if (!listening.value) teardown();
  }

  /** Stop a run or forget a result, for example after a new audio input. */
  function reset() {
    run++;
    teardown();
    running.value = false;
    steps.value = freshSteps();
    outcome.value = null;
    problem.value = null;
    canOverride.value = false;
    listenError.value = null;
    deps.setPassed(false);
  }

  /** The host confirms: "Sounds right", or "Confirm anyway" against a problem. */
  function confirm() {
    deps.setPassed(true);
  }

  /** The host takes the confirmation back. */
  function reject() {
    deps.setPassed(false);
    outcome.value = 'problem';
    problem.value = 'You marked the soundcheck as not OK. Change your setup, then run again.';
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
    run++;
    running.value = false;
    teardown();
  }

  return {
    steps,
    running,
    outcome,
    problem,
    canOverride,
    listening,
    listenError,
    start,
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
