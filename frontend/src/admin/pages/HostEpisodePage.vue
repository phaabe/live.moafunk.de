<script setup lang="ts">
import { computed, defineAsyncComponent, onUnmounted, provide, ref, useId, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { BaseButton, BaseModal } from '@shared/components';
import {
  announcementsApi,
  seriesApi,
  showsApi,
  streamApi,
  type Announcement,
  type AnnouncementSlot,
  type AnnouncementStatus,
  type ServiceSettings,
  type ShowDetail,
} from '../api';
import { useAuthStore } from '../stores/auth';
import { liveStreamShowId, useHostFlow, type SoundcheckStep } from '../composables';
import AnnounceComposer from '../components/AnnounceComposer.vue';
import EpisodeRecordingCard from '../components/EpisodeRecordingCard.vue';
import PromotionCard from '../components/PromotionCard.vue';
import BoardIcon from '../components/BoardIcon.vue';
import EpisodeHeading from '../components/episode/EpisodeHeading.vue';
import EpisodeTabs from '../components/episode/EpisodeTabs.vue';
import OnAirView from '../components/episode/OnAirView.vue';
import ShowTimeCard from '../components/episode/ShowTimeCard.vue';
import SoundcheckCard from '../components/episode/SoundcheckCard.vue';
import StudioView from '../components/episode/StudioView.vue';
import EpisodeIcon from '../components/episode/EpisodeIcon.vue';
import { episodeTimes } from '../components/episode/phase';
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
import { SLOT_NAMES } from '../promotion';

// The one episode page (/shows/<id>), #1052: a heading box, then the four
// steps as tab tiles (Social media, Soundcheck, Show time, Recording) above one
// main card, in one layout for upcoming, on air and ended episodes. The Studio
// (soundcheck and Go Live) opens inside the page as the Soundcheck tab. Show
// time has no soundcheck: it starts the show with the Studio's Go Live, or
// warns and leads to Soundcheck, then shows the live panel. After the show the
// Recording card holds what the Wrap-up had (Publish episode). Another host's
// episode is the same page read-only: no Edit, no Studio, no card buttons.
// A live episode's Soundcheck and Show time tabs always show the Studio card
// and the live panel: working in the host's own tab, else read-only with what
// the host's browser saved (StudioView, OnAirView).

const FlowLive = defineAsyncComponent(() => import('./flow/FlowLive.vue'));
const StudioOnAir = defineAsyncComponent(() => import('./flow/StudioOnAir.vue'));
provide(EPISODE_TABS, true);

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const hostFlow = useHostFlow();
const { ended: liveEnded } = useLiveSession();

const show = ref<ShowDetail | null>(null);
const error = ref<string | null>(null);
const busy = ref(false);
/** The host's own announcements; for another host only where each slot is. */
const announcements = ref<(Announcement | AnnouncementStatus)[]>([]);
const paused = ref(false);
/** Admin switches; off reads "temporarily not available". */
const services = ref<ServiceSettings>({ soundcloud: true, instagram: true });
/** The slot whose post widget is open in the popup. */
const composing = ref<AnnouncementSlot | null>(null);
let poll: ReturnType<typeof setTimeout> | null = null;

const id = computed(() => Number(route.params.id));
/** One of the episode's hosts (#1047): owner, co-host or episode co-host. */
const isMine = computed(() => !!show.value?.can_edit);
/** Who sends the stream while the episode is on air; null when the server cannot say. */
const onAirUser = ref<string | null>(null);
/** This host went live with the episode (maybe in another tab); a co-host who did not gets Listen live. */
const mineOnAir = computed(
  () =>
    isMine.value &&
    !!show.value?.on_air &&
    (onAirUser.value === null || onAirUser.value === auth.user?.username)
);

async function loadOnAirUser() {
  try {
    const status = await streamApi.status();
    onAirUser.value = status.active ? (status.user ?? null) : null;
  } catch {
    onAirUser.value = null;
  }
}
const showName = computed(() => show.value?.series_name ?? show.value?.title ?? '');
const backTo = computed(() =>
  show.value?.series_slug ? `/shows/${show.value.series_slug}` : '/shows'
);
/** The Studio soundcheck passed in this tab for this episode (the Studio does not save it). */
const tested = computed(
  () => !!show.value && hostFlow.showId.value === show.value.id && hostFlow.liveTestPassed.value
);
const { now, phase, recording, segments, step } = useEpisode(show, announcements, tested);

/** End Episode on On Air opens the Recording tab, also while the recording
 * is still being saved (the episode can be on air for a moment). */
function endedOnAir(): boolean {
  const state = router.options.history.state as { episodeEnded?: number } | null;
  return state?.episodeEnded === id.value;
}
/** The episode ended in this tab: Recording shows and the page waits for the file. */
const ended = ref(false);

async function load(reset: boolean) {
  error.value = null;
  try {
    show.value = await showsApi.get(id.value);
    if (show.value.on_air) await loadOnAirUser();
    if (reset) {
      ended.value = endedOnAir();
      picked.value = ended.value ? 3 : null;
    }
    if (isMine.value) await loadPromotion();
    else await loadSlots();
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not load the episode';
  }
  schedulePoll();
}

/** Another host's episode: where each slot is; no text, image or error. */
async function loadSlots() {
  try {
    announcements.value = await announcementsApi.status(id.value);
  } catch {
    // The Social media card then reads "Not set up"; the rest still shows.
    announcements.value = [];
  }
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

const openComposer = (slot: AnnouncementSlot) => (composing.value = slot);

/** The post widget saved: close it and show the new state. */
async function composed() {
  composing.value = null;
  await reloadPromotion();
}

// ─── Tabs ───────────────────────────────────────────────────────────────────
const panelId = useId();
/** The tab the host picked (0–3); null follows the current step. */
const picked = ref<number | null>(null);

/** While the recording is processing after the show, look again every 10 s.
 * While another tab sends the episode (or it may start), every 15 s: the
 * read-only live panel shows what the host's browser saved. */
function schedulePoll() {
  if (poll) clearTimeout(poll);
  poll = null;
  const waiting = ended.value || phase.value === 'post';
  if (waiting && recording.value.state === 'processing') {
    poll = setTimeout(() => load(false), 10000);
  } else if ((show.value?.on_air || phase.value === 'live') && !liveHere.value) {
    poll = setTimeout(() => load(false), 15000);
  }
}

watch(id, () => load(true), { immediate: true });
onUnmounted(() => {
  if (poll) clearTimeout(poll);
});

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
    ? soundcheckItems(
        show.value,
        phase.value,
        tested.value,
        studio.value?.checks ?? show.value.studio_log?.soundcheck?.checks ?? null
      )
    : []
);

/** Another host's episode on air: the public stream. */
const LISTEN = 'https://live.moafunk.de';

// ─── Go Live on the Show time tab (the host's own live episode) ─────────────
const prerecorded = computed(() => show.value?.stream_mode === 'prerecorded');
/** Go Live in the live panel's bar: own live episode, not on air, not over. */
const showGoLive = computed(
  () => isMine.value && !prerecorded.value && !show.value?.on_air && phase.value !== 'post'
);
/** From 30 min before the start, Go Live warns about a missing soundcheck or input. */
const studioOpenNow = computed(() => {
  const openMs = show.value ? episodeTimes(show.value).openMs : null;
  return openMs !== null && now.value >= openMs;
});
const goLiveWarn = computed(() => {
  if (!showGoLive.value || !studioOpenNow.value) return '';
  if (studio.value?.canGoLive || studio.value?.goingLive) return '';
  if (!tested.value) return 'No soundcheck in this browser yet. Do it before you go live.';
  if (phase.value === 'live') return 'Your audio input is off. Pick it in the Soundcheck tab.';
  return '';
});
const showTimeAlert = computed(() => {
  const failed = showGoLive.value ? studio.value?.goLiveError : null;
  if (failed) return { chip: 'Go Live failed', text: failed };
  return goLiveWarn.value ? { chip: 'Soundcheck', text: goLiveWarn.value } : null;
});
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

/** End Episode in the live panel or the on-air bar: Recording, also while the recording is still saved. */
async function endedHere() {
  ended.value = true;
  picked.value = 3;
  await load(false);
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
</script>

<template>
  <div v-if="show" class="episode-page host-page">
    <!-- Heading box, the four step tabs and one main card -->
    <router-link :to="backTo" class="back-link"
      ><BoardIcon name="back" :size="16" /><span class="sep">&lt; </span>{{ showName }}</router-link
    >
    <EpisodeHeading
      :show="show"
      :phase="phase"
      :can-edit="isMine"
      @saved="load(true)"
      @hosts="show.hosts = $event"
      @left="router.push('/dashboard')"
    >
      <template v-if="show.can_manage" #danger>
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
        :read-only="!isMine"
        @changed="reloadPromotion"
        @compose="openComposer"
      />
      <SoundcheckCard
        v-if="prerecorded"
        v-show="tab === 1"
        :show="show"
        :phase="phase"
        :state="step(1)"
        :busy="busy"
        :read-only="!isMine"
        @replace-file="replaceFile"
      />
      <ShowTimeCard
        v-if="prerecorded"
        v-show="tab === 2"
        :show="show"
        :phase="phase"
        :state="step(2)"
        :now="now"
        :read-only="!isMine"
      />
      <EpisodeRecordingCard
        v-show="tab === 3"
        :show="show"
        :phase="phase"
        :state="step(3)"
        :recording="recording"
        :soundcloud-off="!services.soundcloud"
        :read-only="!isMine"
        :announcements="announcements"
        @uploaded="load(false)"
        @changed="load(false)"
        @social="picked = 0"
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
      <section
        v-if="!prerecorded && !studioAllowed"
        v-show="tab === 1"
        :class="['ep-embed', step(1)]"
        aria-label="Studio"
      >
        <StudioView :show="show" :phase="phase" />
      </section>
      <section
        v-if="!prerecorded && !onAirHere"
        v-show="tab === 2"
        :class="['ep-embed', step(2)]"
        aria-label="Live Panel"
      >
        <OnAirView :show="show" :phase="phase" :now="now" :alert="showTimeAlert">
          <template v-if="show.on_air && !mineOnAir" #end>
            <div class="end">
              <a class="listen" :href="LISTEN" target="_blank" rel="noopener"
                >Listen live<EpisodeIcon name="external"
              /></a>
            </div>
          </template>
          <template v-else-if="isMine && (show.on_air || showGoLive)" #end>
            <div class="end">
              <BaseButton v-if="show.on_air" class="xl" :loading="busy" @click="picked = 1">
                <BoardIcon name="broadcast" />Back to On Air
              </BaseButton>
              <BaseButton v-else-if="goLiveWarn" variant="secondary" class="xl" @click="picked = 1">
                <BoardIcon name="mic" />Go to Soundcheck
              </BaseButton>
              <!-- At the start it blinks until clicked. -->
              <!-- Yellow only while Show time is the current step. -->
              <BaseButton
                v-else
                :variant="step(2) === 'now' ? 'primary' : 'secondary'"
                :class="['xl', { blink: studio?.canGoLive && !studio?.goingLive }]"
                :disabled="!studio?.canGoLive"
                :loading="!!studio?.goingLive"
                @click="studio?.goLive()"
              >
                <BoardIcon name="broadcast" />Go Live
              </BaseButton>
            </div>
          </template>
        </OnAirView>
      </section>
    </div>
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

/* Listen live: another host's episode on air. */
.listen {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 52px;
  padding: 0 24px;
  border: 1px solid #4a4a60;
  border-radius: var(--radius-lg);
  color: #eee;
  font: 700 15px/1 var(--font-family);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  text-decoration: none;
}

.listen:hover {
  border-color: #fff;
}

/* Go Live in the live panel's bar blinks until clicked. */
.blink {
  animation: go-live-blink 1.2s steps(1, end) infinite;
}

@keyframes go-live-blink {
  50% {
    opacity: 0.35;
  }
}

@media (prefers-reduced-motion: reduce) {
  .blink {
    animation: none;
  }
}

@media (max-width: 640px) {
  .ep-embed {
    padding: var(--spacing-sm);
  }
}
</style>
