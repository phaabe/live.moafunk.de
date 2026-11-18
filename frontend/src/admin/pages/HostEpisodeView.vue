<script setup lang="ts">
import { computed } from 'vue';
import type { ShowDetail } from '../api';
import BoardIcon from '../components/BoardIcon.vue';
import { episodeDay } from '../episodeRow';

// Flow screen `episode-view`: another host's episode, read-only. Only the
// owner gets the episode screen with edit and Open Studio.

const props = defineProps<{ show: ShowDetail }>();

const heading = computed(
  () => `${episodeDay(props.show.date)} · ${props.show.series_name ?? props.show.title}`
);

/** "Upcoming · Live · 20:00–22:00" */
const pill = computed(() => {
  const s = props.show;
  const time = s.start_time && s.end_time ? `${s.start_time}–${s.end_time}` : s.start_time;
  const mode = s.stream_mode === 'prerecorded' ? 'Pre-recorded' : 'Live';
  return [s.episode_status_label, mode, time].filter(Boolean).join(' · ');
});
</script>

<template>
  <div class="episode-view host-page">
    <router-link to="/dashboard" class="back-link"
      ><BoardIcon name="back" :size="16" /><span class="sep">&lt; </span>Dashboard</router-link
    >
    <div class="view-head">
      <div class="cover">
        <img v-if="show.series_cover_url" :src="show.series_cover_url" alt="Show cover" />
        <BoardIcon v-else name="image" :size="40" />
      </div>
      <div class="show-text">
        <h1 class="page-title">{{ heading }}</h1>
        <p class="pill status-banner"><span class="dot" aria-hidden="true"></span>{{ pill }}</p>
        <p v-if="show.series_description" class="description">{{ show.series_description }}</p>
        <section v-if="show.host_username" class="board-card">
          <div class="board-card-head">
            <h2 class="board-card-title"><BoardIcon name="details" />Details</h2>
          </div>
          <dl class="details">
            <div>
              <dt>Host</dt>
              <dd><BoardIcon name="user" :size="16" />{{ show.host_username }}</dd>
            </div>
          </dl>
        </section>
      </div>
    </div>
  </div>
</template>

<style scoped>
.episode-view {
  max-width: 1040px;
}

.view-head {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: var(--spacing-xl);
}

.cover {
  flex: 0 1 280px;
  aspect-ratio: 1 / 1;
  max-width: 100%;
}

.show-text {
  flex: 999 1 380px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.description {
  margin: 0;
  color: #c8c8d4;
  font-size: var(--font-size-lg);
}

.board-card {
  max-width: 480px;
}

.details {
  margin: 0;
  padding: 0 var(--spacing-md);
}

.details > div {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 52px;
}

.details dt {
  font: 700 11px var(--font-ui);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #a8a8bc;
}

.details dd {
  margin: 0;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-weight: 700;
  color: #fff;
}
</style>
