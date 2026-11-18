<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { RouterLink, useRoute, useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import { seriesApi, type SeriesDetail } from '../api';
import { episodeTarget } from '../episodeRow';
import BoardIcon from '../components/BoardIcon.vue';
import CoverDrop from '../components/CoverDrop.vue';
import DateChip from '../components/DateChip.vue';
import SeriesFields from '../components/SeriesFields.vue';
import { draftOf, draftValid, emptyDraft, toNewSeries } from '../seriesDraft';
import { cleanHandle, handleError } from '../promotion';

// Flow screen `show`: one Show (series) with its episodes, newest first.
// Only the owner opens it, so Details always edits the Show.

const route = useRoute();
const router = useRouter();

const series = ref<SeriesDetail | null>(null);
const error = ref<string | null>(null);
const draft = ref(emptyDraft());
const cover = ref<File | null>(null);
const saving = ref(false);
/** The host's Instagram account; announcements invite it as collaborator. */
const handle = ref('');
const handleProblem = computed(() => handleError(cleanHandle(handle.value)));

/** Date chip per status: yellow while upcoming, amber while the wrap-up is open. */
const CHIP = { upcoming: 'mine', wrapup_open: 'wrapup', finished: '' } as const;

const slug = computed(() => String(route.params.slug));

const rows = computed(() =>
  (series.value?.episodes ?? []).map((episode) => ({
    id: episode.id,
    date: episode.date,
    mode: episode.mode_label,
    live: episode.mode === 'live',
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
    handle.value = found.instagram_handle ? `@${found.instagram_handle}` : '';
    cover.value = null;
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not load the show';
  }
}

async function save() {
  if (!series.value || !draftValid(draft.value) || handleProblem.value || saving.value) return;
  saving.value = true;
  error.value = null;
  try {
    await seriesApi.update(series.value.slug, {
      ...toNewSeries(draft.value, cover.value),
      instagram_handle: cleanHandle(handle.value),
    });
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
  <div class="show-series-page host-page">
    <router-link to="/shows" class="back-link"
      ><BoardIcon name="back" :size="16" /><span class="sep">&lt; </span>My Shows</router-link
    >
    <p v-if="error" class="field-error" role="alert">{{ error }}</p>
    <template v-if="series">
      <div class="show-head">
        <CoverDrop v-model="cover" :current-url="series.cover_url" :size="112" />
        <div class="show-text">
          <h1 class="page-title">{{ series.name }}</h1>
          <p v-if="series.description" class="description">{{ series.description }}</p>
        </div>
        <BaseButton class="xl book" @click="router.push(`/shows/${series.slug}/book`)"
          ><BoardIcon name="calendar-plus" />Book Slot</BaseButton
        >
      </div>

      <!-- Episodes first: hosts open them more often than they edit the rhythm. -->
      <div class="cols">
        <section class="board-card main">
          <div class="board-card-head">
            <h2 class="board-card-title"><BoardIcon name="list" />Episodes</h2>
          </div>
          <ul class="episodes board-rows">
            <li v-for="row in rows" :key="row.id">
              <component
                :is="row.to ? RouterLink : 'div'"
                v-bind="row.to ? { to: row.to } : {}"
                class="board-row"
              >
                <span class="row-main">
                  <DateChip :date="row.date" :tone="CHIP[row.status]" />
                  <span class="sep"> · </span>
                  <span class="row-title row-mode"
                    ><BoardIcon :name="row.live ? 'mic' : 'disc'" :size="16" />{{ row.mode }}</span
                  >
                </span>
                <span :class="['tag', `tag-${row.status}`]">{{ row.right }}</span>
                <BoardIcon v-if="row.to" name="chevron" class="row-icon" />
                <span v-else class="row-icon none"></span>
              </component>
            </li>
          </ul>
        </section>

        <section class="board-card side">
          <div class="board-card-head">
            <h2 class="board-card-title"><BoardIcon name="details" />Details</h2>
          </div>
          <div class="board-card-body">
            <SeriesFields v-model="draft" />
            <label class="field">
              <span class="label">Instagram handle</span>
              <input v-model="handle" type="text" class="input" placeholder="@brunchtime" />
            </label>
            <p v-if="handleProblem" class="field-error">{{ handleProblem }}</p>
            <p class="hint">Booked episodes keep their date and time.</p>
            <div>
              <BaseButton
                variant="secondary"
                :disabled="!draftValid(draft) || !!handleProblem"
                :loading="saving"
                @click="save"
              >
                Save
              </BaseButton>
            </div>
          </div>
        </section>
      </div>
    </template>
  </div>
</template>

<style scoped>
.show-series-page {
  max-width: 1120px;
}

.show-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--spacing-lg);
}

.show-text {
  flex: 1 1 260px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.description {
  margin: 0;
  color: #c8c8d4;
}

.hint {
  margin: 0;
  color: #a0a0b4;
  font-size: var(--font-size-sm);
}

.row-mode {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.row-mode svg {
  color: #a0a0b4;
}

@media (max-width: 640px) {
  .book {
    width: 100%;
  }
}
</style>
