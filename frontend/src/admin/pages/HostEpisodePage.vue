<script setup lang="ts">
import { computed, defineAsyncComponent, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import { seriesApi, showsApi, type ShowDetail } from '../api';
import { useAuthStore } from '../stores/auth';
import { useHostFlow } from '../composables';
import AudioPlayer from '../components/AudioPlayer.vue';
import BoardIcon from '../components/BoardIcon.vue';
import { episodeDay, setLength } from '../episodeRow';
import { openReplaceFile } from '../replaceFile';
import { berlinToUtcDate, isShowEnded } from '../showTime';

// Flow screens `episode`, `episode-pre`, `wrapup` and `done` on one path
// (/shows/<id>) for the host's own episodes. Anything else (another host's
// show) keeps today's show page.

const ShowDetailPage = defineAsyncComponent(() => import('./ShowDetailPage.vue'));

/** The Studio opens this long before the start. */
const STUDIO_OPENS_MS = 30 * 60 * 1000;
/** How long after the end a missing recording still counts as processing
 * (the backend's own "no recording" alert waits as long). */
const RECORDING_GRACE_MS = 15 * 60 * 1000;

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const hostFlow = useHostFlow();

const show = ref<ShowDetail | null>(null);
const view = ref<'episode' | 'wrapup' | 'done'>('episode');
const error = ref<string | null>(null);
const busy = ref(false);
const title = ref('');
const now = ref(Date.now());
const clock = setInterval(() => (now.value = Date.now()), 15000);
let poll: ReturnType<typeof setTimeout> | null = null;

const id = computed(() => Number(route.params.id));
const isMine = computed(
  () =>
    !!show.value &&
    (show.value.host_user_id === auth.user?.id || show.value.created_by === auth.user?.id)
);
const isPrerecorded = computed(() => show.value?.stream_mode === 'prerecorded');
const showName = computed(() => show.value?.series_name ?? show.value?.title ?? '');
const backTo = computed(() =>
  show.value?.series_slug ? `/shows/${show.value.series_slug}` : '/shows'
);
const heading = computed(() =>
  show.value ? `${episodeDay(show.value.date)} · ${showName.value}` : ''
);
const slot = computed(() =>
  show.value?.start_time && show.value.end_time
    ? `${show.value.start_time}–${show.value.end_time}`
    : (show.value?.start_time ?? '')
);

const startMs = computed(() =>
  show.value?.start_time ? berlinToUtcDate(show.value.date, show.value.start_time).getTime() : null
);
const studioOpen = computed(
  () =>
    !!show.value?.on_air || (startMs.value !== null && now.value >= startMs.value - STUDIO_OPENS_MS)
);
const ended = computed(() => {
  void now.value; // re-evaluate on the clock tick
  return (
    !!show.value &&
    !show.value.on_air &&
    (show.value.episode_status !== 'upcoming' || isShowEnded(show.value))
  );
});

/** The booked set as the flow writes it: "brunchtime-0811.mp3 · 1:58:40". */
const setFile = computed(() => {
  const name = show.value?.prerecorded_filename;
  const length = show.value?.prerecorded_duration_secs;
  if (!name) return '';
  return length ? `${name} · ${setLength(length)}` : name;
});

type Pill = { state: 'ok' | 'processing' | 'failed'; text: string };
const pill = computed<Pill>(() => {
  const s = show.value;
  if (!s) return { state: 'processing', text: 'Recording processing…' };
  if (isPrerecorded.value) {
    const length = s.prerecorded_duration_secs;
    return {
      state: 'ok',
      text: length ? `Recording ready · ${setLength(length)}` : 'Recording ready',
    };
  }
  const r = s.latest_recording;
  if (r?.status === 'failed') {
    return {
      state: 'failed',
      text: `No usable recording · ${r.error_message || 'The recording failed.'}`,
    };
  }
  if (r?.download_url) {
    return {
      state: 'ok',
      text: r.duration_ms
        ? `Recording ready · ${setLength(r.duration_ms / 1000)}`
        : 'Recording ready',
    };
  }
  if (r || s.on_air) return { state: 'processing', text: 'Recording processing…' };
  if (s.live_recorded === false) {
    return { state: 'failed', text: 'No usable recording · Record this show was off.' };
  }
  // An End Episode before the scheduled end starts the wait at that moment.
  const ends = [
    s.end_time ? berlinToUtcDate(s.date, s.end_time).getTime() : NaN,
    s.live_ended_at ? Date.parse(s.live_ended_at) : NaN,
  ].filter((ms) => !Number.isNaN(ms));
  const endMs = ends.length ? Math.min(...ends) : null;
  if (endMs !== null && now.value < endMs + RECORDING_GRACE_MS) {
    return { state: 'processing', text: 'Recording processing…' };
  }
  return { state: 'failed', text: 'No usable recording · Nothing was recorded.' };
});

/** End Episode on On Air opens the Wrap-up, also while the recording is
 * still being saved (the episode can be on air for a moment). */
function endedOnAir(): boolean {
  const state = router.options.history.state as { episodeEnded?: number } | null;
  return state?.episodeEnded === id.value;
}

function defaultView(s: ShowDetail): typeof view.value {
  if (s.episode_status === 'finished') return 'done';
  if (endedOnAir()) return 'wrapup';
  if (s.on_air) return 'episode';
  if (s.episode_status === 'wrapup_open') return 'wrapup';
  if (s.stream_mode !== 'prerecorded' && s.latest_recording) return 'wrapup';
  return 'episode';
}

async function load(resetView: boolean) {
  error.value = null;
  try {
    const loaded = await showsApi.get(id.value);
    show.value = loaded;
    title.value = loaded.title;
    if (resetView) view.value = defaultView(loaded);
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not load the episode';
  }
  schedulePoll();
}

/** While the recording is processing, look again every 10 s. */
function schedulePoll() {
  if (poll) clearTimeout(poll);
  poll = null;
  if (view.value === 'wrapup' && pill.value.state === 'processing') {
    poll = setTimeout(() => load(false), 10000);
  }
}

watch(id, () => load(true), { immediate: true });
watch(view, schedulePoll);
onUnmounted(() => {
  clearInterval(clock);
  if (poll) clearTimeout(poll);
});

async function saveTitle() {
  const value = title.value.trim();
  if (!show.value || !value || value === show.value.title) return;
  try {
    await showsApi.update(show.value.id, { title: value });
    show.value = { ...show.value, title: value };
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not save the title';
  }
}

async function openStudio() {
  if (!show.value || !studioOpen.value) return;
  busy.value = true;
  try {
    // Select this episode, so the Studio uses it even when the host has several.
    let mine = hostFlow.shows.value.find((s) => s.id === show.value?.id);
    if (!mine) {
      await hostFlow.fetchMyShow();
      mine = hostFlow.shows.value.find((s) => s.id === show.value?.id);
    }
    if (!mine) throw new Error('This episode is not assigned to you');
    if (hostFlow.showId.value !== mine.id) hostFlow.setLiveTestPassed(false);
    hostFlow.selectShow(mine);
    hostFlow.selectMode('live');
    await router.push({ path: '/stream/live', query: { show: String(mine.id) } });
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not open the Studio';
  } finally {
    busy.value = false;
  }
}

async function cancelBooking() {
  if (!show.value || !window.confirm('Cancel this booking?')) return;
  busy.value = true;
  try {
    const result = await seriesApi.cancelBooking(show.value.id);
    await router.push(result.series_slug ? `/shows/${result.series_slug}` : '/shows');
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not cancel the booking';
  } finally {
    busy.value = false;
  }
}

function replaceFile() {
  const s = show.value;
  if (!s?.series_slug || !s.start_time || !s.end_time) return;
  openReplaceFile(router, s.series_slug, {
    id: s.id,
    date: s.date,
    start_time: s.start_time,
    end_time: s.end_time,
  });
}

async function publish() {
  if (!show.value || pill.value.state === 'processing') return;
  busy.value = true;
  error.value = null;
  try {
    await seriesApi.publish(show.value.id);
    view.value = 'done';
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not publish the episode';
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <ShowDetailPage v-if="show && !isMine" />
  <div v-else-if="show" class="episode-page">
    <!-- Done -->
    <template v-if="view === 'done'">
      <h1 class="page-title">Episode published</h1>
      <div>
        <BaseButton @click="router.push('/schedule')">Back to Schedule</BaseButton>
      </div>
    </template>

    <!-- Wrap-up -->
    <template v-else-if="view === 'wrapup'">
      <button type="button" class="back-link" @click="view = 'episode'">&lt; Episode</button>
      <h1 class="page-title">Wrap-up</h1>
      <p :class="['pill', 'status-banner', pill.state]">{{ pill.text }}</p>
      <p v-if="error" class="field-error" role="alert">{{ error }}</p>
      <div>
        <BaseButton :disabled="pill.state === 'processing'" :loading="busy" @click="publish">
          Publish
        </BaseButton>
      </div>
    </template>

    <!-- Episode · Pre-recorded -->
    <template v-else-if="isPrerecorded">
      <router-link :to="backTo" class="back-link">&lt; {{ showName }}</router-link>
      <h1 class="page-title">{{ heading }}</h1>
      <p class="pill status-banner ok">
        Pre-recorded &amp; confirmed · plays at {{ show.start_time }}
      </p>
      <section class="board-card">
        <div class="board-card-head">
          <h2 class="board-card-title"><BoardIcon name="next" />Next step</h2>
        </div>
        <div class="board-card-body">
          <p class="text">
            Nothing to do. The set plays at air time. Wrap-up opens after {{ show.end_time }}.
          </p>
          <!-- The player shows no file name, and 0:00 until its first Play. -->
          <p v-if="setFile" class="set-file">{{ setFile }}</p>
          <AudioPlayer v-if="show.prerecorded_url" :src="show.prerecorded_url" />
          <p v-if="error" class="field-error" role="alert">{{ error }}</p>
          <div class="actions">
            <BaseButton variant="secondary" :disabled="!show.series_slug" @click="replaceFile">
              Replace file
            </BaseButton>
            <BaseButton :disabled="!ended" @click="view = 'wrapup'">Open Wrap-up</BaseButton>
          </div>
        </div>
      </section>
    </template>

    <!-- Episode (live) -->
    <template v-else>
      <router-link :to="backTo" class="back-link">&lt; {{ showName }}</router-link>
      <h1 class="page-title">{{ heading }}</h1>
      <p class="pill status-banner">Upcoming · Live · {{ slot }}</p>
      <div class="board-grid">
        <section class="board-card">
          <div class="board-card-head">
            <h2 class="board-card-title"><BoardIcon name="next" />Next step</h2>
          </div>
          <div class="board-card-body">
            <p class="text">
              Test your audio before air time. The Studio opens 30 min before the start.
            </p>
            <div>
              <BaseButton :disabled="!studioOpen" :loading="busy" @click="openStudio">
                Open Studio
              </BaseButton>
            </div>
          </div>
        </section>
        <section class="board-card">
          <div class="board-card-head">
            <h2 class="board-card-title"><BoardIcon name="details" />Details</h2>
          </div>
          <div class="board-card-body">
            <label class="field">
              <span class="label">Title</span>
              <input
                v-model="title"
                class="input"
                type="text"
                @change="saveTitle"
                @keydown.enter.prevent="saveTitle"
              />
            </label>
            <div>
              <BaseButton variant="danger" :disabled="busy" @click="cancelBooking">
                Cancel booking
              </BaseButton>
            </div>
          </div>
        </section>
      </div>
      <p v-if="error" class="field-error" role="alert">{{ error }}</p>
    </template>
  </div>
  <p v-else-if="error" class="field-error" role="alert">{{ error }}</p>
</template>

<style scoped>
.episode-page {
  max-width: 960px;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
}

.back-link {
  align-self: flex-start;
  background: none;
  border: none;
  padding: 0;
  font: inherit;
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  cursor: pointer;
}

.page-title {
  margin: 0;
}

.text {
  margin: 0;
  color: var(--color-text-muted);
}

.set-file {
  margin: 0;
  overflow-wrap: anywhere;
}

.actions {
  display: flex;
  gap: var(--spacing-md);
}

.field {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.label {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.input {
  background-color: var(--color-surface-alt);
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-family: var(--font-family);
  padding: var(--spacing-sm) var(--spacing-md);
}

.field-error {
  color: var(--color-error);
  font-size: var(--font-size-sm);
  margin: 0;
}
</style>
