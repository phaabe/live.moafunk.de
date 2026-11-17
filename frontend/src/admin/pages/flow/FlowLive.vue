<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import {
  useHostFlow,
  useAudioCapture,
  useStreamSocket,
  BROWSER_UNSUPPORTED_MESSAGE,
  streamRecordingMimeType,
} from '@admin/composables';
import LiveSetupTest from '@admin/components/LiveSetupTest.vue';

/**
 * The Studio (/stream/live, flow screen `studio`): soundcheck, then a manual
 * Go Live. Nothing goes live by itself. The router gate opens it 30 min
 * before the start. Go Live works from the start time once the test
 * broadcast sounds right.
 */
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

const soundcheck = computed(() => {
  const left = secondsToStart.value;
  if (left <= 0) return 'Soundcheck · it is air time';
  const mm = String(Math.floor(left / 60)).padStart(2, '0');
  const ss = String(left % 60).padStart(2, '0');
  return `Soundcheck · show starts in ${mm}:${ss}`;
});

const record = computed({
  get: () => flow.recordStream.value,
  set: (value: boolean) => flow.setRecordStream(value),
});

const goingLive = ref(false);
const wentLive = ref(false);
const goLiveError = ref<string | null>(null);

const canGoLive = computed(
  () =>
    !!show.value &&
    flow.liveTestPassed.value &&
    audioCapture.isCapturing.value &&
    secondsToStart.value <= 0
);

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
  // Take over the socket's callbacks from the soundcheck: this connection is
  // the broadcast. On Air registers its own once it opens.
  const socket = useStreamSocket({
    onLive: () => {
      wentLive.value = true;
      void router.push({ path: '/stream/on-air', query: { show: String(showId) } });
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
    await socket.connect(false, showId, false, flow.recordStream.value);
    audioCapture.setOnData((data, first) => socket.send(data, first));
    if (!audioCapture.startRecording()) {
      socket.stopStream();
      throw new Error(audioCapture.error.value ?? 'Failed to start audio');
    }
  } catch (err) {
    failGoLive(err instanceof Error ? err.message : 'Failed to go live');
  }
}

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
  <div class="studio">
    <router-link :to="episodePath" class="back-link">&lt; Episode</router-link>
    <h1 class="page-title">Studio</h1>
    <p class="pill status-banner warn">{{ soundcheck }}</p>

    <section class="board-card">
      <div class="board-card-body">
        <LiveSetupTest studio />

        <label class="check">
          <input v-model="record" type="checkbox" />
          Record this show
        </label>

        <p v-if="goLiveError" class="field-error" role="alert">{{ goLiveError }}</p>
        <div>
          <BaseButton :disabled="!canGoLive" :loading="goingLive" @click="goLive"
            >Go Live</BaseButton
          >
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.studio {
  max-width: 800px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
}

.back-link {
  align-self: flex-start;
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.page-title {
  margin: 0;
}

.check {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  font-size: var(--font-size-sm);
}

.field-error {
  color: var(--color-error);
  font-size: var(--font-size-sm);
  margin: 0;
}
</style>
