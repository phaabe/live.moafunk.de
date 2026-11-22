<script setup lang="ts">
import { computed, defineAsyncComponent, onUnmounted, provide, ref, useId, watch } from 'vue';
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
import { liveStreamShowId, useHostFlow, type SoundcheckStep } from '../composables';
import AnnounceComposer from '../components/AnnounceComposer.vue';
import EpisodeRecordingCard from '../components/EpisodeRecordingCard.vue';
import PromotionCard from '../components/PromotionCard.vue';
import HostEpisodeView from './HostEpisodeView.vue';
import BoardIcon from '../components/BoardIcon.vue';
import EpisodeHeading from '../components/episode/EpisodeHeading.vue';
import EpisodeTabs from '../components/episode/EpisodeTabs.vue';
import ShowTimeCard from '../components/episode/ShowTimeCard.vue';
import SoundcheckCard from '../components/episode/SoundcheckCard.vue';
import {
  EPISODE_TABS,
  recordingItems,
  showClock,
  socialItems,
  soundcheckItems,
} from '../components/episode/tabs';
import { useEpisode } from '../components/episode/useEpisode';
import { openReplaceFile } from '../replaceFile';
import { useLiveSession } from '../liveSession';
import {
  defaultPostTime,
  editable,
  inSlot,
  INSTAGRAM_OFF_NOTE,
  postTime,
  SLOT_NAMES,
  slotState,
} from '../promotion';

// The host's own episode on one path (/shows/<id>), #1052: a heading box, then
// the four steps as tab tiles (Social media, Soundcheck, Show time, Recording)
// above one main card, in one layout for pre-show, on air and post-show. The
// Studio (soundcheck and Go Live) opens inside the page as the Soundcheck tab.
// Show time has no soundcheck: it starts the show with the Studio's Go Live,
// or warns and leads to Soundcheck, then shows the live panel. The flow
// screens `wrapup` and `done` stay their own screens on the same path; `done`
// shows only right after Publish, so a finished episode stays reachable. Another
// host's episode gets the cards read-only (HostEpisodeView).

const FlowLive = defineAsyncComponent(() => import('./flow/FlowLive.vue'));
const StudioOnAir = defineAsyncComponent(() => import('./flow/StudioOnAir.vue'));
provide(EPISODE_TABS, true);

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const hostFlow = useHostFlow();
const { ended: liveEnded } = useLiveSession();

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
const { now, phase, recording, segments, step } = useEpisode(show, announcements, tested);

/** End Episode on On Air opens the Wrap-up, also while the recording is
 * still being saved (the episode can be on air for a moment). */
function endedOnAir(): boolean {
  const state = router.options.history.state as { episodeEnded?: number } | null;
  return state?.episodeEnded === id.value;
}

function defaultView(s: ShowDetail): typeof view.value {
  if (s.episode_status === 'finished') return 'episode';
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

// ─── Tabs ───────────────────────────────────────────────────────────────────
const panelId = useId();
/** The tab the host picked (0–3); null follows the current step. */
const picked = ref<number | null>(null);

/** This browser tab sends this episode's stream. `now` makes it look again every second. */
const liveHere = computed(() => {
  void now.value;
  return !!show.value && liveStreamShowId() === show.value.id;
});
/** This browser tab is live with another episode: its Studio stays as it is. */
const otherLive = computed(() => {
  void now.value;
  const live = liveStreamShowId();
  return live !== null && live !== show.value?.id;
});
const onAirNow = computed(() => !!show.value?.on_air || liveHere.value);

const tab = computed(() => {
  if (picked.value !== null) return picked.value;
  if (liveHere.value) return 2;
  const i = segments.value.findIndex((s) => s.state === 'now');
  // A finished episode has every step done: open its last one, Recording.
  return i < 0 ? segments.value.length - 1 : i;
});

/** The Studio may open on this page: the rules of its own route gate. */
const studioAllowed = computed(() => {
  const s = show.value;
  return (
    !!s &&
    isMine.value &&
    s.stream_mode !== 'prerecorded' &&
    !s.prerecorded_confirmed_at &&
    phase.value !== 'post' &&
    !liveHere.value &&
    !otherLive.value
  );
});
/** The Studio (soundcheck, input, Go Live) is the Soundcheck tab's page, also after the start.
 * Show time shows only the live panel; until then its card leads here. */
const studioTab = computed(() => studioAllowed.value && tab.value === 1);
/** The live panel is the Show time page while this browser tab is live. */
const onAirHere = computed(() => liveHere.value && hostFlow.showId.value === show.value?.id);

/** Mounted on first open, then kept: the checks and the audio input survive a tab switch. */
const studioMounted = ref(false);
const studio = ref<{
  checks: SoundcheckStep[] | null;
  canGoLive: boolean;
  goingLive: boolean;
  goLiveError: string | null;
  goLive: () => Promise<void>;
} | null>(null);
watch(
  studioTab,
  async (want) => {
    if (want && !studioMounted.value && (await selectForStudio())) studioMounted.value = true;
  },
  { immediate: true }
);
// Gone live, ended or closed: the Studio goes, as it did when On Air opened.
watch(studioAllowed, (ok) => {
  if (!ok) studioMounted.value = false;
});

const tabSocial = computed(() => socialItems(announcements.value, trackPublic.value));
const tabSoundcheck = computed(() =>
  show.value
    ? soundcheckItems(show.value, phase.value, tested.value, studio.value?.checks ?? null)
    : []
);
const tabClock = computed(() =>
  show.value
    ? showClock(show.value, phase.value, now.value, onAirNow.value)
    : { kind: 'pre' as const, clock: '', sub: '' }
);
const tabRecording = computed(() =>
  show.value
    ? recordingItems(
        show.value,
        phase.value,
        recording.value,
        onAirNow.value,
        !services.value.soundcloud
      )
    : []
);

/** Select this episode for the Studio, so it uses it even when the host has several. */
async function selectForStudio(): Promise<boolean> {
  if (!show.value) return false;
  try {
    let mine = hostFlow.shows.value.find((s) => s.id === show.value?.id);
    if (!mine) {
      await hostFlow.fetchMyShow();
      mine = hostFlow.shows.value.find((s) => s.id === show.value?.id);
    }
    if (!mine) throw new Error('This episode is not assigned to you');
    hostFlow.selectShow(mine);
    hostFlow.selectMode('live');
    return true;
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not open the Studio';
    return false;
  }
}

/** Go Live worked (Studio or Show time tab): Show time shows the live panel. */
function wentLive() {
  picked.value = null;
  void load(false);
}

/** End Episode in the live panel or the on-air bar: the Wrap-up, also while the recording is still saved. */
async function endedHere() {
  picked.value = null;
  await load(false);
  view.value = 'wrapup';
}
watch(liveEnded, (e) => {
  if (e && e.showId === show.value?.id) void endedHere();
});

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

    <!-- Episode: heading box, the four step tabs and one main card -->
    <template v-else>
      <router-link :to="backTo" class="back-link"
        ><BoardIcon name="back" :size="16" /><span class="sep">&lt; </span
        >{{ showName }}</router-link
      >
      <EpisodeHeading :show="show" :phase="phase" can-edit @saved="load(true)">
        <template #danger>
          <BaseButton variant="danger" class="quiet-danger" :disabled="busy" @click="cancelBooking">
            <BoardIcon name="close" :size="16" />Cancel booking
          </BaseButton>
        </template>
      </EpisodeHeading>
      <p v-if="error" class="field-error" role="alert">{{ error }}</p>
      <EpisodeTabs
        :segments="segments"
        :selected="tab"
        :social="tabSocial"
        :soundcheck="tabSoundcheck"
        :clock="tabClock"
        :recording="tabRecording"
        :panel-id="panelId"
        @select="picked = $event"
      />
      <div :id="panelId" class="ep-main" role="tabpanel" :aria-labelledby="`${panelId}-tab-${tab}`">
        <PromotionCard
          v-show="tab === 0"
          :show="show"
          :phase="phase"
          :state="step(0)"
          :announcements="announcements"
          :paused="paused"
          :instagram-off="!services.instagram"
          @changed="reloadPromotion"
          @compose="openComposer"
        />
        <SoundcheckCard
          v-show="tab === 1 && !studioTab"
          :show="show"
          :phase="phase"
          :state="step(1)"
          :tested="tested"
          :busy="busy"
          @open-studio="picked = 1"
          @replace-file="replaceFile"
        />
        <ShowTimeCard
          v-show="tab === 2 && !onAirHere"
          :show="show"
          :phase="phase"
          :state="step(2)"
          :now="now"
          :busy="busy"
          :tested="tested"
          :ready="!!studio?.canGoLive"
          :starting="!!studio?.goingLive"
          :error="studio?.goLiveError"
          @open-studio="picked = 1"
          @go-live="studio?.goLive()"
        />
        <EpisodeRecordingCard
          v-show="tab === 3"
          :show="show"
          :phase="phase"
          :state="step(3)"
          :recording="recording"
          :soundcloud-off="!services.soundcloud"
          @uploaded="load(false)"
          @changed="load(false)"
          @open-wrapup="view = 'wrapup'"
        />
        <section
          v-if="studioMounted"
          v-show="studioTab"
          :class="['ep-embed', step(tab)]"
          aria-label="Studio"
        >
          <FlowLive ref="studio" embedded @live="wentLive" />
        </section>
        <section v-if="onAirHere" v-show="tab === 2" class="ep-embed now" aria-label="Live Panel">
          <StudioOnAir embedded :shown="tab === 2" />
        </section>
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

.ep-main {
  display: flex;
  flex-direction: column;
}

/* The Studio and the live panel in the main card, framed in their step's colour. */
.ep-embed {
  padding: var(--spacing-lg);
  border: 2px solid #34344c;
  border-radius: var(--radius-xl);
  background: var(--color-surface);
}

.ep-embed.done {
  border-color: #8f8536;
}

.ep-embed.now {
  border-color: var(--color-primary);
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
  .ep-embed {
    padding: var(--spacing-sm);
  }
}
</style>
