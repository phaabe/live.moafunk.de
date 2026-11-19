<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import { announcementsApi, seriesApi, showsApi, type Announcement, type ShowDetail } from '../api';
import { useAuthStore } from '../stores/auth';
import { useHostFlow } from '../composables';
import AudioPlayer from '../components/AudioPlayer.vue';
import EpisodeDetailsCard from '../components/EpisodeDetailsCard.vue';
import EpisodeRecordingCard from '../components/EpisodeRecordingCard.vue';
import PromotionCard from '../components/PromotionCard.vue';
import HostEpisodeView from './HostEpisodeView.vue';
import BoardIcon from '../components/BoardIcon.vue';
import { episodeDay, setLength } from '../episodeRow';
import { openReplaceFile } from '../replaceFile';
import { berlinToUtcDate, isShowEnded } from '../showTime';
import { defaultPostTime, editable, inSlot, postTime, slotState } from '../promotion';

// Flow screens `episode`, `episode-pre`, `wrapup` and `done` on one path
// (/shows/<id>) for the host's own episodes. Another host's episode gets the
// read-only `episode-view`.

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
const announcements = ref<Announcement[]>([]);
const paused = ref(false);
/** The Show's Instagram handle (collaborator), without "@". */
const handle = ref<string | null>(null);
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
    if (resetView) view.value = defaultView(loaded);
    if (isMine.value) await loadPromotion();
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not load the episode';
  }
  schedulePoll();
}

async function loadPromotion() {
  const slug = show.value?.series_slug;
  const [list, meta, series] = await Promise.all([
    announcementsApi.list(id.value),
    announcementsApi.meta(id.value),
    slug ? seriesApi.get(slug) : null,
  ]);
  announcements.value = list;
  paused.value = meta.paused;
  handle.value = series?.instagram_handle || null;
}

async function reloadPromotion() {
  try {
    await loadPromotion();
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not load the announcements';
  }
}

const trackPublic = computed(() => !!show.value?.soundcloud_public);
const post = computed(() => inSlot(announcements.value, 'post'));
/** Wrap-up row of the post slot; it is sent only when the SoundCloud track is public. */
const postReading = computed(() => {
  if (post.value) return slotState(announcements.value, 'post', trackPublic.value);
  if (trackPublic.value) return 'Not set up';
  return show.value ? `${defaultPostTime(show.value.date)} · waits for the public track` : '';
});
const instagramPill = computed(() => {
  const p = post.value;
  if (!p || p.state === 'draft') return 'Instagram · no post-announcement';
  if (p.state === 'sent') return 'Instagram · post-announcement sent';
  if (!trackPublic.value) return 'Instagram · waits for the public SoundCloud track';
  return p.scheduled_at
    ? `Instagram · post-announcement ${postTime(p.scheduled_at)}`
    : 'Instagram · post-announcement';
});

const openComposer = (slot: 'pre' | 'post') => router.push(`/shows/${id.value}/announce/${slot}`);

async function cancelPost() {
  if (!window.confirm('Cancel the post-announcement?')) return;
  busy.value = true;
  error.value = null;
  try {
    await announcementsApi.cancel(id.value, 'post');
    await loadPromotion();
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not cancel the announcement';
  } finally {
    busy.value = false;
  }
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

/** Open Studio; while on air, "Back to On Air" opens On Air (its gate falls back to the Studio). */
async function openStudio(path: '/stream/live' | '/stream/on-air' = '/stream/live') {
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
    await router.push({ path, query: { show: String(mine.id) } });
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
  <HostEpisodeView v-if="show && !isMine" :show="show" />
  <div v-else-if="show" class="episode-page host-page">
    <!-- Done -->
    <template v-if="view === 'done'">
      <h1 class="page-title">Episode published</h1>
      <p class="pill status-banner ok">
        <span class="dot" aria-hidden="true"></span>{{ instagramPill }}
      </p>
      <div>
        <BaseButton @click="router.push('/schedule')">Back to Schedule</BaseButton>
      </div>
    </template>

    <!-- Wrap-up -->
    <template v-else-if="view === 'wrapup'">
      <button type="button" class="back-link" @click="view = 'episode'">
        <BoardIcon name="back" :size="16" /><span class="sep">&lt; </span>Episode
      </button>
      <h1 class="page-title">Wrap-up</h1>
      <p :class="['pill', 'status-banner', pill.state]">
        <span class="dot" aria-hidden="true"></span>{{ pill.text }}
      </p>
      <section class="board-card post-announcement">
        <div class="board-card-head">
          <h2 class="board-card-title"><BoardIcon name="image" />Post-announcement</h2>
        </div>
        <div class="board-card-body">
          <ul class="post-slot">
            <li>
              <span class="slot-name">Post · feed post 4:5</span>
              <span class="slot-state">{{ postReading }}</span>
            </li>
          </ul>
          <p class="hint">
            Sent at its time only when the track is public. Publish does not need it.
          </p>
          <div class="actions">
            <template v-if="editable(post)">
              <BaseButton variant="secondary" :disabled="busy" @click="openComposer('post')"
                >Edit post-announcement</BaseButton
              >
              <BaseButton variant="secondary" :disabled="busy" @click="cancelPost"
                >Cancel post-announcement</BaseButton
              >
            </template>
            <BaseButton
              v-else-if="post?.state !== 'sent'"
              variant="secondary"
              :disabled="busy"
              @click="openComposer('post')"
              >Set up post-announcement</BaseButton
            >
          </div>
        </div>
      </section>
      <p v-if="error" class="field-error" role="alert">{{ error }}</p>
      <div>
        <BaseButton
          class="xl"
          :disabled="pill.state === 'processing'"
          :loading="busy"
          @click="publish"
        >
          Publish
        </BaseButton>
      </div>
    </template>

    <template v-else>
      <router-link :to="backTo" class="back-link"
        ><BoardIcon name="back" :size="16" /><span class="sep">&lt; </span
        >{{ showName }}</router-link
      >
      <h1 class="page-title">{{ heading }}</h1>

      <!-- Episode · Pre-recorded, no file yet -->
      <template v-if="isPrerecorded && !show.prerecorded_filename">
        <p class="pill status-banner warn">
          <span class="dot" aria-hidden="true"></span>Pre-recorded · no file yet · plays at
          {{ show.start_time }}
        </p>
        <div class="cols">
          <section class="board-card next-step needs-file main">
            <div class="board-card-head">
              <h2 class="board-card-title"><BoardIcon name="next" />Next step</h2>
            </div>
            <div class="board-card-body">
              <div class="lead">
                <span class="lead-icon"><BoardIcon name="music" :size="20" /></span>
                <p class="text">Upload your set before air time. Without a file nothing plays.</p>
              </div>
              <p v-if="error" class="field-error" role="alert">{{ error }}</p>
              <BaseButton class="block xl" :disabled="!show.series_slug" @click="replaceFile">
                <BoardIcon name="upload" />Upload your set
              </BaseButton>
            </div>
          </section>
          <EpisodeDetailsCard class="side" :show="show" @saved="load(true)" />
        </div>
      </template>

      <!-- Episode · Pre-recorded -->
      <template v-else-if="isPrerecorded">
        <p class="pill status-banner ok">
          <span class="dot" aria-hidden="true"></span>Pre-recorded &amp; confirmed · plays at
          {{ show.start_time }}
        </p>
        <div class="cols">
          <section class="board-card next-step main">
            <div class="board-card-head">
              <h2 class="board-card-title"><BoardIcon name="next" />Next step</h2>
            </div>
            <div class="board-card-body">
              <p class="lead-text">
                Nothing to do. The set plays at air time. Wrap-up opens after {{ show.end_time }}.
              </p>
              <!-- The player shows no file name, and 0:00 until its first Play. -->
              <div v-if="setFile || show.prerecorded_url" class="set">
                <p v-if="setFile" class="set-file">
                  <BoardIcon name="music" :size="16" />{{ setFile }}
                </p>
                <AudioPlayer v-if="show.prerecorded_url" :src="show.prerecorded_url" />
              </div>
              <p v-if="error" class="field-error" role="alert">{{ error }}</p>
              <div class="actions">
                <BaseButton variant="secondary" :disabled="!show.series_slug" @click="replaceFile">
                  <BoardIcon name="upload" :size="16" />Replace file
                </BaseButton>
                <BaseButton :disabled="!ended" @click="view = 'wrapup'">Open Wrap-up</BaseButton>
              </div>
            </div>
          </section>
          <EpisodeDetailsCard class="side" :show="show" @saved="load(true)" />
        </div>
      </template>

      <!-- Episode (live) -->
      <template v-else>
        <p v-if="show.on_air" class="pill status-banner live">
          <span class="dot" aria-hidden="true"></span>On air · until {{ show.end_time }}
        </p>
        <p v-else class="pill status-banner">
          <span class="dot" aria-hidden="true"></span>Upcoming · Live · {{ slot }}
        </p>
        <div class="cols">
          <section class="board-card next-step side">
            <div class="board-card-head">
              <h2 class="board-card-title"><BoardIcon name="next" />Next step</h2>
            </div>
            <div class="board-card-body">
              <div class="lead">
                <span class="lead-icon"><BoardIcon name="headphones" :size="20" /></span>
                <p class="text">
                  Test your audio before air time. The Studio opens 30 min before the start.
                </p>
              </div>
              <BaseButton
                v-if="show.on_air"
                class="block xl"
                :loading="busy"
                @click="openStudio('/stream/on-air')"
              >
                <BoardIcon name="broadcast" />Back to On Air
              </BaseButton>
              <BaseButton
                v-else
                class="block xl"
                :disabled="!studioOpen"
                :loading="busy"
                @click="openStudio()"
              >
                Open Studio<BoardIcon name="next" />
              </BaseButton>
            </div>
          </section>
          <EpisodeDetailsCard class="main" :show="show" @saved="load(true)">
            <BaseButton
              variant="danger"
              class="quiet-danger"
              :disabled="busy"
              @click="cancelBooking"
            >
              <BoardIcon name="close" :size="16" />Cancel booking
            </BaseButton>
          </EpisodeDetailsCard>
          <PromotionCard
            class="main"
            :show="show"
            :announcements="announcements"
            :handle="handle"
            :paused="paused"
            @changed="reloadPromotion"
          />
          <EpisodeRecordingCard
            class="side"
            :show="show"
            @uploaded="load(false)"
            @changed="load(false)"
          />
        </div>
        <p v-if="error" class="field-error" role="alert">{{ error }}</p>
      </template>
    </template>
  </div>
  <p v-else-if="error" class="field-error" role="alert">{{ error }}</p>
</template>

<style scoped>
.episode-page {
  max-width: 1120px;
}

.lead-text {
  margin: 0;
  color: #dcdce6;
  line-height: 1.5;
}

.set {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  border: 1px solid #2a2a42;
  border-radius: var(--radius-lg);
  background: #07071a;
}

.set-file {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  overflow-wrap: anywhere;
  font: 700 var(--font-size-sm) var(--font-family);
}

.set-file svg {
  flex: none;
  color: var(--color-primary);
}

.post-slot {
  margin: 0;
  padding: 0;
  list-style: none;
}

.post-slot li {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
}

.slot-name {
  font-weight: 700;
}

.slot-state,
.hint {
  margin: 0;
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: var(--spacing-md);
}
</style>
