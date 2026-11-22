<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { showsApi } from '@admin/api';
import {
  useHostFlow,
  useAudioCapture,
  useStreamSocket,
  BROWSER_UNSUPPORTED_MESSAGE,
  streamRecordingMimeType,
  type SoundcheckStep,
} from '@admin/composables';
import LiveSetupTest from '@admin/components/LiveSetupTest.vue';
import BoardIcon from '@admin/components/BoardIcon.vue';
import { startLiveSession } from '@admin/liveSession';

/**
 * The Studio (/stream/live, flow screen `studio`): soundcheck, then a manual
 * Go Live. Nothing goes live by itself. The router gate opens it 30 min
 * before the start. At the start time the countdown turns into Go Live, which
 * blinks until clicked once the soundcheck is confirmed. Every show is recorded.
 *
 * Studio · Back on air (`studio-backonair`): the episode is still on air but
 * no tab sends audio, e.g. after a reload. The server keeps it on air for
 * 5 minutes after a drop. Going back on air needs only an input: the show
 * passed its soundcheck before.
 *
 * `embedded`: the Studio as a tab of the host's episode page (#1052). It has
 * no back link and title, and going live tells the page instead of opening
 * On Air.
 */
const props = defineProps<{ embedded?: boolean }>();
const emit = defineEmits<{ live: [] }>();
const router = useRouter();
const flow = useHostFlow();
const audioCapture = useAudioCapture();

const show = computed(() => flow.show.value);
const episodePath = computed(() => (show.value ? `/shows/${show.value.id}` : '/shows'));

const now = ref(Date.now());
const clock = setInterval(() => (now.value = Date.now()), 1000);

const secondsToStart = computed(() => {
  const s = show.value;
  if (!s?.start_time) return 0;
  return Math.ceil((flow.berlinToUtcDate(s.date, s.start_time).getTime() - now.value) / 1000);
});

/** The pill: "Soundcheck · show starts in 18:42", the clock drawn big. Null at the start. */
const countdown = computed(() => {
  const left = secondsToStart.value;
  if (left <= 0) return null;
  const mm = String(Math.floor(left / 60)).padStart(2, '0');
  const ss = String(left % 60).padStart(2, '0');
  return { label: 'Soundcheck · show starts in ', clock: `${mm}:${ss}` };
});

// The soundcheck card. Its test broadcast uses the socket that Go Live needs.
const soundcheck = ref<{ stopTest?: () => void; steps?: SoundcheckStep[] } | null>(null);

const goingLive = ref(false);
const wentLive = ref(false);
const goLiveError = ref<string | null>(null);

// The router opens On Air when this tab is live, so on air here means this tab
// does not send: a reload, a closed tab or a lost connection. (If another tab
// of the host is live, going back on air takes over, as Go Live does.)
const backOnAir = ref(false);

/** Is the episode on air? Null when the server cannot say. */
async function onAirNow(showId: number): Promise<boolean | null> {
  try {
    return (await showsApi.get(showId)).on_air === true;
  } catch {
    return null;
  }
}

onMounted(async () => {
  if (show.value) backOnAir.value = (await onAirNow(show.value.id)) === true;
});

const canGoLive = computed(
  () =>
    !!show.value &&
    (backOnAir.value || flow.liveTestPassed.value) &&
    audioCapture.isCapturing.value &&
    secondsToStart.value <= 0
);

const goLiveName = computed(() =>
  goingLive.value ? 'Going live…' : backOnAir.value ? 'Go back on air' : 'Go Live'
);

/** The line above Go Live once the countdown is over. */
const goLiveLabel = computed(() => {
  if (backOnAir.value)
    return canGoLive.value ? 'Go back on air now' : 'Pick your input, then go back on air';
  return canGoLive.value
    ? 'It is air time · start your show'
    : 'It is air time · confirm the soundcheck first';
});

function failGoLive(message: string) {
  goLiveError.value = message;
  goingLive.value = false;
  audioCapture.stopRecording();
}

async function goLive() {
  if (!show.value || !canGoLive.value || goingLive.value) return;
  const showId = show.value.id;
  goingLive.value = true;
  goLiveError.value = null;
  if (backOnAir.value) {
    // This click is a user gesture: resume audio before the request.
    audioCapture.resumeAudioContext();
    // The 5 minutes ran out: the normal soundcheck. If the server cannot say, go on.
    if ((await onAirNow(showId)) === false) {
      backOnAir.value = false;
      goingLive.value = false;
      return;
    }
  }
  // A test broadcast the host still listens to: connect() would reuse its socket.
  soundcheck.value?.stopTest?.();
  // Take over the socket's callbacks from the soundcheck: this connection is
  // the broadcast. Once live, the live session takes them.
  const socket = useStreamSocket({
    onLive: () => {
      wentLive.value = true;
      startLiveSession(
        showId,
        props.embedded ? `/shows/${showId}` : `/stream/on-air?show=${showId}`
      );
      if (props.embedded) emit('live');
      else void router.push({ path: '/stream/on-air', query: { show: String(showId) } });
    },
    onError: (message) => {
      socket.stopStream();
      failGoLive(message);
    },
    onDisconnected: () => {
      if (goingLive.value && !wentLive.value) failGoLive('Could not go live. Try again.');
    },
  });
  try {
    // A socket without audio looks live while listeners hear silence.
    if (!streamRecordingMimeType()) throw new Error(BROWSER_UNSUPPORTED_MESSAGE);
    // This click is a user gesture: start a suspended audio context.
    audioCapture.resumeAudioContext();
    audioCapture.setOnError((message) => {
      goLiveError.value = message;
    });
    flow.setShowStarted();
    // Every show is recorded.
    await socket.connect(false, showId, false, true);
    audioCapture.setOnData((data, first) => socket.send(data, first));
    if (!audioCapture.startRecording()) {
      socket.stopStream();
      throw new Error(audioCapture.error.value ?? 'Failed to start audio');
    }
  } catch (err) {
    failGoLive(err instanceof Error ? err.message : 'Failed to go live');
  }
}

// The episode page's Show time tab starts the show with these too.
defineExpose({
  checks: computed(() => soundcheck.value?.steps ?? null),
  canGoLive,
  goingLive,
  goLiveError,
  goLive,
});

onUnmounted(() => {
  clearInterval(clock);
  // Left the Studio while Go Live was still connecting: do not leave a
  // broadcast running without its On Air screen.
  if (goingLive.value && !wentLive.value) {
    useStreamSocket().stopStream();
    audioCapture.stopRecording();
  }
});
</script>

<template>
  <div :class="['studio', 'host-page', { embedded }]">
    <template v-if="!embedded">
      <router-link :to="episodePath" class="back-link"
        ><BoardIcon name="back" :size="16" /><span class="sep">&lt; </span>Episode</router-link
      >
      <h1 class="page-title">Studio</h1>
    </template>
    <template v-if="backOnAir">
      <p class="pill status-banner live">
        <span class="dot" aria-hidden="true"></span>Still on air · no audio
      </p>
      <p class="notice" role="alert">
        Your show is still on air, but no audio arrives. Listeners hear silence.
      </p>
    </template>
    <p v-else-if="countdown" class="pill status-banner warn countdown">
      <span class="countdown-label"
        ><span class="dot" aria-hidden="true"></span>{{ countdown.label }}</span
      ><span class="countdown-clock">{{ countdown.clock }}</span>
    </p>
    <!-- At the start the countdown turns into Go Live; it blinks until clicked. -->
    <button
      v-if="backOnAir || !countdown"
      type="button"
      :class="['status-banner', 'warn', 'countdown', 'go-live', { ready: canGoLive && !goingLive }]"
      :disabled="!canGoLive || goingLive"
      :aria-label="goLiveName"
      aria-describedby="go-live-label"
      @click="goLive"
    >
      <span id="go-live-label" class="countdown-label"
        ><span class="dot" aria-hidden="true"></span>{{ goLiveLabel }}</span
      ><span class="countdown-clock"
        ><BoardIcon name="broadcast" :size="36" />{{ goLiveName }}</span
      >
    </button>
    <p v-if="goLiveError" class="field-error" role="alert">{{ goLiveError }}</p>
    <LiveSetupTest ref="soundcheck" studio :input-only="backOnAir" />
  </div>
</template>

<style scoped>
.studio {
  max-width: 1120px;
  margin: 0 auto;
}

.countdown {
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
}

.countdown-label {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font: 700 12px var(--font-ui);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #fde7b0;
}

.countdown-clock {
  font: 700 3rem/1 var(--font-family);
  font-variant-numeric: tabular-nums;
  color: #fff;
}

.notice {
  margin: 0 0 var(--spacing-md);
  padding: var(--spacing-md);
  border: 1px solid #5a4416;
  border-left: 4px solid var(--color-warning);
  border-radius: var(--radius-xl);
  background: rgba(245, 158, 11, 0.1);
  color: #fde7b0;
  font-weight: 600;
}

/* The countdown as Go Live: a button the width of the page. */
.go-live {
  width: 100%;
  color: #fff;
  font: inherit;
  font-weight: var(--font-weight-bold);
  text-align: left;
  cursor: pointer;
}

.go-live .countdown-label,
.go-live .countdown-clock {
  display: inline-flex;
  align-items: center;
  gap: 12px;
  color: inherit;
}

.go-live:disabled {
  cursor: not-allowed;
}

.go-live:disabled .countdown-clock {
  color: #8e8ea4;
}

.go-live:focus-visible {
  outline: 3px solid #fff;
  outline-offset: 2px;
}

/* Ready: it blinks until the host clicks it. */
.go-live.ready {
  border-color: var(--color-primary);
  border-left-color: var(--color-primary);
  animation: go-live-blink 1.2s steps(1, end) infinite;
}

@keyframes go-live-blink {
  50% {
    background-color: var(--color-primary);
    color: #0f0f23;
  }
}

@media (prefers-reduced-motion: reduce) {
  .go-live.ready {
    animation: none;
    background-color: var(--color-primary);
    color: #0f0f23;
  }
}

@media (max-width: 640px) {
  .go-live .countdown-clock {
    font-size: 2.25rem;
  }
}
</style>
