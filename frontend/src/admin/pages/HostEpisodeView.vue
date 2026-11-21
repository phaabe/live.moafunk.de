<script setup lang="ts">
import { ref, toRef, watch } from 'vue';
import { announcementsApi, type AnnouncementStatus, type ShowDetail } from '../api';
import BoardIcon from '../components/BoardIcon.vue';
import EpisodeRecordingCard from '../components/EpisodeRecordingCard.vue';
import PromotionCard from '../components/PromotionCard.vue';
import EpisodeHeading from '../components/episode/EpisodeHeading.vue';
import ShowTimeCard from '../components/episode/ShowTimeCard.vue';
import SoundcheckCard from '../components/episode/SoundcheckCard.vue';
import { useEpisode } from '../components/episode/useEpisode';

// Another host's episode (#1052): the same page as the host's own, read-only.
// No Edit and no action buttons, but every status and the public links.

const props = defineProps<{ show: ShowDetail }>();

const show = toRef(props, 'show');
/** Where each announcement slot is; no text, image or error. */
const slots = ref<AnnouncementStatus[]>([]);
const { now, phase, recording, segments } = useEpisode(show, slots);

watch(
  () => props.show.id,
  async (id) => {
    try {
      slots.value = await announcementsApi.status(id);
    } catch {
      // The Social media card then reads "Not set up"; the rest still shows.
      slots.value = [];
    }
  },
  { immediate: true }
);
</script>

<template>
  <div class="episode-view host-page">
    <router-link to="/dashboard" class="back-link"
      ><BoardIcon name="back" :size="16" /><span class="sep">&lt; </span>Dashboard</router-link
    >
    <EpisodeHeading :show="show" :phase="phase" :segments="segments" />
    <div class="ep-cards">
      <SoundcheckCard :show="show" :phase="phase" :now="now" read-only />
      <ShowTimeCard :show="show" :phase="phase" :now="now" read-only />
      <EpisodeRecordingCard :show="show" :phase="phase" :recording="recording" read-only />
      <PromotionCard :show="show" :phase="phase" :announcements="slots" read-only />
    </div>
  </div>
</template>

<style scoped>
.episode-view {
  max-width: 1180px;
}

.ep-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 440px), 1fr));
  gap: var(--spacing-lg);
}

@media (max-width: 640px) {
  .ep-cards {
    gap: 14px;
  }
}
</style>
