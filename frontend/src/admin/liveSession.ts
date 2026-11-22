import {
  computed,
  effectScope,
  onScopeDispose,
  ref,
  shallowRef,
  watch,
  type ComputedRef,
  type EffectScope,
  type Ref,
} from 'vue';
import {
  useAudioCapture,
  useAutoBitrate,
  useHostFlow,
  useStreamSocket,
  useUploadHealth,
  ownCaptureCallbacks,
  ownStreamCallbacks,
  streamShowId,
  DEVICE_ENDED_MESSAGE,
  type UploadQualityMode,
} from '@admin/composables';
import { useWakeLock } from '@admin/composables/useWakeLock';
import { forgetListeners } from '@admin/composables/useListeners';
import {
  forgetMoments,
  formatHms,
  formatLeft,
  type ConnectionVerdict,
} from '@admin/components/live-panel/onAir';

/** After a drop of more than 10 minutes (the browser keeps 10 minutes of audio). */
export const AUDIO_DROPPED_MESSAGE =
  'The connection was down for more than 10 minutes. The recording misses some audio.';

export interface LiveWarning {
  text: string;
  /** Read out at once (role alert); the reconnect line changes every second (role status). */
  urgent: boolean;
}

/**
 * The live broadcast of this browser tab (#1063), from Go Live until End
 * Episode or until this tab's stream ends. It lives outside the pages, so the
 * live panel (StudioOnAir) and the on-air bar on every other admin page show
 * the same state, and the screen wake lock, the upload health and Auto
 * quality keep running while the host opens other pages.
 */
export interface LiveSession {
  readonly showId: number;
  /** Unix ms when it went on air. */
  readonly since: number;
  /** The live panel: On Air, or the episode page (its Show time tab). */
  readonly panelPath: string;
  readonly title: string;
  /** "12:00–14:00", or '' without times. */
  readonly slot: string;
  /** Unix ms, one tick a second. */
  now: Ref<number>;
  elapsedSeconds: ComputedRef<number>;
  /** "00:42:10" */
  elapsed: ComputedRef<string>;
  /** A server or audio error; the input gone has its own flag. */
  error: Ref<string | null>;
  inputGone: Ref<boolean>;
  /** The screen wake lock is held. */
  screenOn: Ref<boolean>;
  health: ReturnType<typeof useUploadHealth>;
  quality: Ref<UploadQualityMode>;
  targetKbps: ComputedRef<number>;
  /** Auto's last switch. */
  lastStep: Ref<{ from: number; to: number; at: number } | null>;
  connection: ComputedRef<ConnectionVerdict>;
  /** How long the socket has been down: "4 s", "1:05". */
  downFor: ComputedRef<string>;
  audioDropped: Ref<boolean>;
  /** The on-air bar's lines. */
  warnings: ComputedRef<LiveWarning[]>;
  setQuality(mode: UploadQualityMode): void;
  /** Send another input: a device id or 'screen'. False when it failed. */
  changeInput(id: string): Promise<boolean>;
  /** End Episode: stops the stream and the recording. Returns the show id. */
  end(): Promise<number>;
}

const current = shallowRef<LiveSession | null>(null);
let scope: EffectScope | null = null;
/** The last End Episode in this tab: a new object each time, so a watcher sees each one. */
const ended = shallowRef<{ showId: number } | null>(null);
/** The live panel is on screen, so the on-air bar hides. */
const panelShown = ref(false);

export function useLiveSession() {
  return { session: current, ended, panelShown };
}

/**
 * Start the live session for a show that just went live in this tab, or
 * return the one already running for it. Takes the socket and recorder
 * callbacks, also again for a running one (Go back on air after a lost
 * connection takes them for a moment).
 */
export function startLiveSession(showId: number, panelPath: string): LiveSession {
  // Before taking the callbacks: a call while they are not owned resets them.
  const socket = useStreamSocket();
  const capture = useAudioCapture();
  if (current.value && current.value.showId !== showId) stopLiveSession();
  const live = current.value ?? (current.value = createSession(showId, panelPath));
  ownStreamCallbacks({
    onError: (message) => {
      live.error.value = message;
    },
  });
  ownCaptureCallbacks({
    onData: (data, first) => socket.send(data, first),
    onError: (message) => {
      if (message !== DEVICE_ENDED_MESSAGE) {
        live.error.value = message;
        return;
      }
      // The device's track ended: listeners hear silence until a new input.
      live.inputGone.value = true;
      void capture.listDevices();
    },
  });
  return live;
}

/** Let go of everything; the stream itself is not touched. */
function stopLiveSession(): void {
  scope?.stop();
  scope = null;
  current.value = null;
  ownStreamCallbacks(null);
  ownCaptureCallbacks(null);
}

function createSession(showId: number, panelPath: string): LiveSession {
  scope = effectScope(true);
  return scope.run(() => sessionIn(showId, panelPath))!;
}

/** The session's parts; runs inside its effect scope, which stopLiveSession() stops. */
function sessionIn(showId: number, panelPath: string): LiveSession {
  const socket = useStreamSocket();
  const capture = useAudioCapture();
  const flow = useHostFlow();
  const show = flow.show.value;
  const since = Date.now();

  const now = ref(since);
  const clock = setInterval(() => {
    now.value = Date.now();
    // This tab's stream ended without End Episode: another page stopped it.
    if (streamShowId() !== showId) stopLiveSession();
  }, 1000);
  const elapsedSeconds = computed(() => Math.max(0, Math.floor((now.value - since) / 1000)));

  const pageTitle = document.title;
  document.title = `● ON AIR · ${pageTitle}`;
  onScopeDispose(() => {
    clearInterval(clock);
    document.title = pageTitle;
  });

  const wakeLock = useWakeLock();
  const error = ref<string | null>(null);
  const inputGone = ref(false);

  // The upload telemetry also drives Auto's quality steps. A fixed quality
  // (Upload quality in the Connection card) switches Auto off.
  const quality = ref<UploadQualityMode>('auto');
  const targetKbps = computed(() => Math.round(capture.streamBitsPerSecond.value / 1000));
  const lastStep = ref<{ from: number; to: number; at: number } | null>(null);
  const health = useUploadHealth(
    computed(() => socket.state.value === 'live'),
    capture.streamBitsPerSecond
  );
  useAutoBitrate({
    mode: quality,
    currentKbps: targetKbps,
    bufferSeconds: health.bufferSeconds,
    ticks: health.ticks,
    onChange: (kbps) => {
      lastStep.value = { from: targetKbps.value, to: kbps, at: Date.now() };
      capture.setStreamBitsPerSecond(kbps * 1000);
    },
  });

  const connection = computed<ConnectionVerdict>(() => {
    switch (socket.state.value) {
      case 'live':
        if (health.verdict.value === 'good') return 'stable';
        return health.verdict.value === 'tight' ? 'tight' : 'too slow';
      case 'connecting':
      case 'connected':
        return 'reconnecting…';
      default:
        return 'lost';
    }
  });

  // When the socket went down, for "Reconnecting · 4 s".
  const droppedAt = ref<number | null>(socket.state.value === 'live' ? null : since);
  watch(socket.state, (state) => {
    if (state === 'live') droppedAt.value = null;
    else droppedAt.value ??= Date.now();
  });
  const downFor = computed(() => {
    const s =
      droppedAt.value === null ? 0 : Math.max(0, Math.floor((now.value - droppedAt.value) / 1000));
    return s < 60 ? `${s} s` : formatLeft(s);
  });

  const warnings = computed(() => {
    const list: LiveWarning[] = [];
    if (connection.value === 'reconnecting…') {
      list.push({
        text: `Reconnecting · ${downFor.value}. Your audio waits in this browser.`,
        urgent: false,
      });
    } else if (connection.value === 'lost') {
      list.push({ text: 'Connection lost. Listeners may hear silence.', urgent: true });
    }
    if (inputGone.value) list.push({ text: DEVICE_ENDED_MESSAGE, urgent: true });
    if (error.value) list.push({ text: error.value, urgent: true });
    if (socket.audioDropped.value) list.push({ text: AUDIO_DROPPED_MESSAGE, urgent: true });
    return list;
  });

  function setQuality(mode: UploadQualityMode) {
    if (mode === quality.value) return;
    quality.value = mode;
    lastStep.value = null;
    if (mode !== 'auto') capture.setStreamBitsPerSecond(mode * 1000);
  }

  async function changeInput(id: string): Promise<boolean> {
    const ok =
      id === 'screen' ? await capture.captureScreenAudio() : await capture.captureDevice(id);
    if (!ok) {
      error.value = capture.error.value;
      return false;
    }
    // A new capture starts at the default rate: keep a fixed upload quality.
    if (quality.value !== 'auto') capture.setStreamBitsPerSecond(quality.value * 1000);
    // The capture stopped the old recorder; the new one sends to the same socket.
    if (!capture.startRecording()) return false;
    inputGone.value = false;
    error.value = null;
    return true;
  }

  async function end(): Promise<number> {
    // Send the recorder's last chunk before "stop" closes the stream.
    await capture.stopRecordingAndFlush();
    // "stop" ends the stream and the recording on the backend.
    socket.stopStream();
    capture.stopCapture();
    forgetListeners();
    forgetMoments();
    flow.reset();
    stopLiveSession();
    ended.value = { showId };
    return showId;
  }

  return {
    showId,
    since,
    panelPath,
    title: show?.title ?? '',
    slot: show?.start_time && show.end_time ? `${show.start_time}–${show.end_time}` : '',
    now,
    elapsedSeconds,
    elapsed: computed(() => formatHms(elapsedSeconds.value)),
    error,
    inputGone,
    screenOn: wakeLock.active,
    health,
    quality,
    targetKbps,
    lastStep,
    connection,
    downFor,
    audioDropped: socket.audioDropped,
    warnings,
    setQuality,
    changeInput,
    end,
  };
}
