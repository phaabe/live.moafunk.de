<script setup lang="ts">
import { computed, ref } from 'vue';
import { useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import { seriesApi } from '../api';
import CoverDrop from '../components/CoverDrop.vue';
import SeriesFields from '../components/SeriesFields.vue';
import { draftValid, emptyDraft, toNewSeries } from '../seriesDraft';

// Flow screen `new-show`: a host creates a Show (series) once, then books
// its episodes on the Show page.

const router = useRouter();

const draft = ref(emptyDraft());
const cover = ref<File | null>(null);
const saving = ref(false);
const error = ref<string | null>(null);

const canSubmit = computed(() => draftValid(draft.value) && !saving.value);

async function createShow() {
  if (!canSubmit.value) return;
  saving.value = true;
  error.value = null;
  try {
    const created = await seriesApi.create(toNewSeries(draft.value, cover.value));
    await router.push(`/shows/${created.slug}`);
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not create the show';
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div class="new-show-page">
    <router-link to="/shows" class="back-link">&lt; My Show</router-link>
    <h1 class="page-title">New Show</h1>
    <p class="intro">A show is your series. You book its episodes after this.</p>

    <section class="board-card">
      <div class="board-card-body">
        <CoverDrop v-model="cover" />
        <SeriesFields v-model="draft" rhythm-title />
        <p v-if="error" class="field-error" role="alert">{{ error }}</p>

        <BaseButton :disabled="!canSubmit" :loading="saving" @click="createShow">
          Create show
        </BaseButton>
      </div>
    </section>
  </div>
</template>

<style scoped>
.new-show-page {
  max-width: 560px;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
}
.back-link {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}
.page-title {
  margin: 0;
}
.intro {
  margin: 0;
  color: var(--color-text-muted);
}
.field-error {
  color: var(--color-error);
  font-size: var(--font-size-sm);
  margin: 0;
}
</style>
