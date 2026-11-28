<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { showsApi, type ShowDetail } from '../api';
import AnnounceComposer from '../components/AnnounceComposer.vue';
import BoardIcon from '../components/BoardIcon.vue';
import { episodeDay } from '../episodeRow';
import { SLOT_NAMES } from '../promotion';

// Flow screen `announce`: the post widget of the pre- or post-announcement as
// a page (/shows/<id>/announce/<pre|post>). The episode page opens the same
// widget in a popup.

const route = useRoute();
const router = useRouter();

const id = computed(() => Number(route.params.id));
const slot = computed(() => (route.params.slot === 'post' ? 'post' : 'pre'));
const show = ref<ShowDetail | null>(null);
const error = ref<string | null>(null);
const showName = computed(() => show.value?.series_name ?? show.value?.title ?? '');

watch(
  id,
  async (showId) => {
    error.value = null;
    try {
      show.value = await showsApi.get(showId);
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Could not load the announcement';
    }
  },
  { immediate: true }
);

const back = () => router.push(`/shows/${id.value}`);
</script>

<template>
  <div class="announce-page host-page">
    <router-link :to="`/shows/${id}`" class="back-link"
      ><BoardIcon name="back" :size="16" /><span class="sep">&lt; </span>Episode</router-link
    >
    <template v-if="show">
      <h1 class="page-title">{{ SLOT_NAMES[slot] }}</h1>
      <p class="pill status-banner">
        <span class="dot" aria-hidden="true"></span>Feed post 4:5 · {{ episodeDay(show.date) }} ·
        {{ showName }}
      </p>
      <AnnounceComposer :show-id="id" :slot="slot" @done="back" @cancel="back" />
    </template>
    <p v-else-if="error" class="field-error" role="alert">{{ error }}</p>
  </div>
</template>

<style scoped>
.announce-page {
  max-width: 1120px;
}
</style>
