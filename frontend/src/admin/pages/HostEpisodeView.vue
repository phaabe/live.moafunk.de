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
  <div class="episode-view">
    <router-link to="/dashboard" class="back-link">&lt; Dashboard</router-link>
    <div class="show-head">
      <div class="cover">
        <img v-if="show.series_cover_url" :src="show.series_cover_url" alt="Show cover" />
      </div>
      <div class="show-text">
        <h1 class="page-title">{{ heading }}</h1>
        <p v-if="show.series_description" class="description">{{ show.series_description }}</p>
      </div>
    </div>
    <p class="pill status-banner">{{ pill }}</p>
    <section v-if="show.host_username" class="board-card">
      <div class="board-card-head">
        <h2 class="board-card-title"><BoardIcon name="details" />Details</h2>
      </div>
      <div class="board-card-body">
        <dl class="details">
          <div>
            <dt>Host</dt>
            <dd>{{ show.host_username }}</dd>
          </div>
        </dl>
      </div>
    </section>
  </div>
</template>

<style scoped>
.episode-view {
  max-width: 960px;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
}

.back-link {
  align-self: flex-start;
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.show-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--spacing-lg);
}

.cover {
  width: 72px;
  height: 72px;
  flex-shrink: 0;
  border-radius: var(--radius-lg);
  overflow: hidden;
  background-color: var(--color-surface-alt);
}

.cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.show-text {
  flex: 1;
  min-width: 200px;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.page-title,
.description,
.details,
.details dd {
  margin: 0;
}

.description {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.details > div {
  display: flex;
  gap: var(--spacing-md);
}

.details dt {
  min-width: 80px;
  color: var(--color-text-muted);
}
</style>
