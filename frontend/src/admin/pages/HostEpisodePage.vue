<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { BaseButton, BaseModal } from '@shared/components';
import {
  announcementsApi,
  seriesApi,
  showsApi,
  type Announcement,
  type AnnouncementSlot,
  type ServiceSettings,
  type ShowDetail,
} from '../api';
import { useAuthStore } from '../stores/auth';
import { useHostFlow } from '../composables';
import AnnounceComposer from '../components/AnnounceComposer.vue';
import EpisodeRecordingCard from '../components/EpisodeRecordingCard.vue';
import PromotionCard from '../components/PromotionCard.vue';
import HostEpisodeView from './HostEpisodeView.vue';
import BoardIcon from '../components/BoardIcon.vue';
import EpisodeHeading from '../components/episode/EpisodeHeading.vue';
import ShowTimeCard from '../components/episode/ShowTimeCard.vue';
import SoundcheckCard from '../components/episode/SoundcheckCard.vue';
import { episodeTimes } from '../components/episode/phase';
import { useEpisode } from '../components/episode/useEpisode';
import { openReplaceFile } from '../replaceFile';
import {
  defaultPostTime,
  editable,
  inSlot,
  INSTAGRAM_OFF_NOTE,
  postTime,
  SLOT_NAMES,
  slotState,
} from '../promotion';

// The host's own episode on one path (/shows/<id>), #1052: a heading box and
// four status cards (Soundcheck, Show time, Recording & SoundCloud, Social
// media) in one layout for pre-show, on air and post-show. The flow screens
// `wrapup` and `done` stay their own screens on the same path. Another host's
// episode gets the same page read-only (HostEpisodeView).

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
/** Admin switches; off reads "temporarily not available". */
const services = ref<ServiceSettings>({ soundcloud: true, instagram: true });
/** The slot whose post widget is open in the popup. */
const composing = ref<AnnouncementSlot | null>(null);
let poll: ReturnType<typeof setTimeout> | null = null;

const id = computed(() => Number(route.params.id));
const isMine = computed(
  () =>
    !!show.value &&
    (show.value.host_user_id === auth.user?.id || show.value.created_by === auth.user?.id)
);
const showName = computed(() => show.value?.series_name ?? show.value?.title ?? '');
const backTo = computed(() =>
  show.value?.series_slug ? `/shows/${show.value.series_slug}` : '/shows'
);
/** The Studio soundcheck passed in this tab for this episode (the Studio does not save it). */
const tested = computed(
  () => !!show.value && hostFlow.showId.value === show.value.id && hostFlow.liveTestPassed.value
);
const { now, phase, recording, segments } = useEpisode(show, announcements, tested);

const studioOpen = computed(() => {
  if (!show.value) return false;
  const { openMs } = episodeTimes(show.value);
  return !!show.value.on_air || (openMs !== null && now.value >= openMs);
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
  const [list, meta] = await Promise.all([
    announcementsApi.list(id.value),
    announcementsApi.meta(id.value),
  ]);
  announcements.value = list;
  paused.value = meta.paused;
  services.value = meta.services;
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

const openComposer = (slot: AnnouncementSlot) => (composing.value = slot);

/** The post widget saved: close it and show the new state. */
async function composed() {
  composing.value = null;
  await reloadPromotion();
}

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

/** While the recording is processing after the show, look again every 10 s. */
function schedulePoll() {
  if (poll) clearTimeout(poll);
  poll = null;
  const waiting = view.value === 'wrapup' || (view.value === 'episode' && phase.value === 'post');
  if (waiting && recording.value.state === 'processing') {
    poll = setTimeout(() => load(false), 10000);
  }
}

watch(id, () => load(true), { immediate: true });
watch(view, schedulePoll);
onUnmounted(() => {
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
  if (!show.value || recording.value.state === 'processing') return;
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
        <BaseButton @click="router.push(backTo)">Back to Show</BaseButton>
      </div>
    </template>

    <!-- Wrap-up -->
    <template v-else-if="view === 'wrapup'">
      <button type="button" class="back-link" @click="view = 'episode'">
        <BoardIcon name="back" :size="16" /><span class="sep">&lt; </span>Episode
      </button>
      <h1 class="page-title">Wrap-up</h1>
      <p :class="['pill', 'status-banner', recording.state]">
        <span class="dot" aria-hidden="true"></span>{{ recording.text }}
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
          <p v-if="!services.instagram" class="notice">{{ INSTAGRAM_OFF_NOTE }}</p>
          <div class="actions">
            <template v-if="editable(post)">
              <BaseButton
                variant="secondary"
                :disabled="busy || !services.instagram"
                @click="openComposer('post')"
                >Edit post-announcement</BaseButton
              >
              <BaseButton variant="secondary" :disabled="busy" @click="cancelPost"
                >Cancel post-announcement</BaseButton
              >
            </template>
            <BaseButton
              v-else-if="post?.state !== 'sent'"
              variant="secondary"
              :disabled="busy || !services.instagram"
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
          :disabled="recording.state === 'processing'"
          :loading="busy"
          @click="publish"
        >
          Publish
        </BaseButton>
      </div>
    </template>

    <!-- Episode: heading box and the four status cards -->
    <template v-else>
      <router-link :to="backTo" class="back-link"
        ><BoardIcon name="back" :size="16" /><span class="sep">&lt; </span
        >{{ showName }}</router-link
      >
      <EpisodeHeading :show="show" :phase="phase" :segments="segments" can-edit @saved="load(true)">
        <template #danger>
          <BaseButton variant="danger" class="quiet-danger" :disabled="busy" @click="cancelBooking">
            <BoardIcon name="close" :size="16" />Cancel booking
          </BaseButton>
        </template>
      </EpisodeHeading>
      <p v-if="error" class="field-error" role="alert">{{ error }}</p>
      <div class="ep-cards">
        <SoundcheckCard
          :show="show"
          :phase="phase"
          :now="now"
          :tested="tested"
          :busy="busy"
          @open-studio="openStudio()"
          @replace-file="replaceFile"
        />
        <ShowTimeCard
          :show="show"
          :phase="phase"
          :now="now"
          :busy="busy"
          @open-studio="openStudio"
        />
        <EpisodeRecordingCard
          :show="show"
          :phase="phase"
          :recording="recording"
          :soundcloud-off="!services.soundcloud"
          @uploaded="load(false)"
          @changed="load(false)"
          @open-wrapup="view = 'wrapup'"
        />
        <PromotionCard
          :show="show"
          :phase="phase"
          :announcements="announcements"
          :paused="paused"
          :instagram-off="!services.instagram"
          @changed="reloadPromotion"
          @compose="openComposer"
        />
      </div>
    </template>
    <BaseModal
      :open="!!composing"
      :title="composing ? SLOT_NAMES[composing] : ''"
      size="xl"
      @close="composing = null"
    >
      <div v-if="composing" class="host-page">
        <AnnounceComposer
          :show-id="show.id"
          :slot="composing"
          @done="composed"
          @cancel="composing = null"
        />
      </div>
    </BaseModal>
  </div>
  <p v-else-if="error" class="field-error" role="alert">{{ error }}</p>
</template>

<style scoped>
.episode-page {
  max-width: 1180px;
}

.ep-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 440px), 1fr));
  gap: var(--spacing-lg);
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

.notice {
  margin: 0;
  padding: 12px 14px;
  border: 1px solid #6b5a1e;
  border-radius: var(--radius-lg);
  color: #f3d77a;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: var(--spacing-md);
}

@media (max-width: 640px) {
  .ep-cards {
    gap: 14px;
  }
}
</style>
