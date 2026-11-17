<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import { seriesApi, type SeriesDetail } from '../api';
import { episodeDay, episodeTarget } from '../episodeRow';
import BoardIcon from '../components/BoardIcon.vue';
import CoverDrop from '../components/CoverDrop.vue';
import SeriesFields from '../components/SeriesFields.vue';
import { draftOf, draftValid, emptyDraft, toNewSeries } from '../seriesDraft';

// Flow screen `show`: one Show (series) with its episodes, newest first.
// Only the owner opens it, so Details always edits the Show.

const route = useRoute();
const router = useRouter();

const series = ref<SeriesDetail | null>(null);
const error = ref<string | null>(null);
const draft = ref(emptyDraft());
const cover = ref<File | null>(null);
const saving = ref(false);

const slug = computed(() => String(route.params.slug));

const rows = computed(() =>
  (series.value?.episodes ?? []).map((episode) => ({
    id: episode.id,
    left: `${episodeDay(episode.date)} · ${episode.mode_label}`,
    right: episode.status_label,
    status: episode.status,
    to: episodeTarget(episode),
  }))
);

async function load() {
  error.value = null;
  try {
    const found = await seriesApi.get(slug.value);
    if (!found) {
      // Not this host's show (or no such show).
      await router.replace('/shows');
      return;
    }
    series.value = found;
    draft.value = draftOf(found);
    cover.value = null;
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not load the show';
  }
}

async function save() {
  if (!series.value || !draftValid(draft.value) || saving.value) return;
  saving.value = true;
  error.value = null;
  try {
    await seriesApi.update(series.value.slug, toNewSeries(draft.value, cover.value));
    await load();
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not save the show';
  } finally {
    saving.value = false;
  }
}

watch(slug, load, { immediate: true });
</script>

<template>
  <div class="show-series-page">
    <router-link to="/shows" class="back-link">&lt; My Shows</router-link>
    <p v-if="error" class="field-error" role="alert">{{ error }}</p>

    <template v-if="series">
      <div class="show-head">
        <CoverDrop v-model="cover" :current-url="series.cover_url" :size="96" />
        <div class="show-text">
          <h1 class="page-title">{{ series.name }}</h1>
          <p v-if="series.description" class="description">{{ series.description }}</p>
        </div>
        <BaseButton @click="router.push(`/shows/${series.slug}/book`)">Book Slot</BaseButton>
      </div>

      <section class="board-card">
        <div class="board-card-head">
          <h2 class="board-card-title"><BoardIcon name="details" />Details</h2>
        </div>
        <div class="board-card-body">
          <SeriesFields v-model="draft" />
          <p class="hint">Booked episodes keep their date and time.</p>
          <div>
            <BaseButton
              variant="secondary"
              :disabled="!draftValid(draft)"
              :loading="saving"
              @click="save"
            >
              Save
            </BaseButton>
          </div>
        </div>
      </section>

      <section class="board-card">
        <div class="board-card-head">
          <h2 class="board-card-title"><BoardIcon name="list" />Episodes</h2>
        </div>
        <div class="board-card-body">
          <ul class="episodes board-rows">
            <li v-for="row in rows" :key="row.id">
              <router-link v-if="row.to" :to="row.to" class="board-row">
                <span>{{ row.left }}</span>
                <span :class="['tag', `tag-${row.status}`]">{{ row.right }}</span>
              </router-link>
              <div v-else class="board-row">
                <span>{{ row.left }}</span>
                <span :class="['tag', `tag-${row.status}`]">{{ row.right }}</span>
              </div>
            </li>
          </ul>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.show-series-page {
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
  padding-bottom: var(--spacing-lg);
  border-bottom: 1px solid var(--color-border);
}

.hint {
  margin: 0;
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.show-text {
  flex: 1;
  min-width: 200px;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.page-title {
  margin: 0;
}

.description {
  margin: 0;
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.field-error {
  color: var(--color-error);
  font-size: var(--font-size-sm);
  margin: 0;
}
</style>
