<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { RouterLink } from 'vue-router';
import { showsApi, streamApi, type SeriesDetail, type ShowOverviewItem } from '../../api';
import { useAuthStore } from '../../stores/auth';
import { isShowRunning } from '../../showTime';

// "On air now" on the show detail page (it was on Schedule): the show on air,
// its host and its end. It opens On Air only when the episode is the viewer's.

const props = defineProps<{
  /** The Show on this page: its episodes get its name. */
  series: SeriesDetail;
  /** The viewer owns this Show. */
  owner: boolean;
}>();

const auth = useAuthStore();
const active = ref(false);
const schedule = ref<ShowOverviewItem[]>([]);
let poll: ReturnType<typeof setInterval> | null = null;

const running = computed(() => schedule.value.find((s) => isShowRunning(s)) ?? null);
const ofThisShow = computed(
  () => !!running.value && props.series.episodes.some((e) => e.id === running.value?.id)
);
const name = computed(() => (ofThisShow.value ? props.series.name : (running.value?.title ?? '')));

/** On Air only for the viewer's own episode. */
const link = computed(() => {
  const show = running.value;
  if (!show) return null;
  const mine =
    (ofThisShow.value && props.owner) ||
    (!!show.host_username && show.host_username === auth.user?.username);
  return mine ? { path: '/stream/on-air', query: { show: String(show.id) } } : null;
});

async function loadStatus() {
  const was = active.value;
  active.value = (await streamApi.status()).active;
  if (active.value && !was) schedule.value = (await showsApi.overview()).shows;
}

function check() {
  loadStatus().catch((e) => console.error('Failed to load the stream status:', e));
}

onMounted(() => {
  check();
  poll = setInterval(check, 10000);
});

onUnmounted(() => {
  if (poll) clearInterval(poll);
  poll = null;
});
</script>

<template>
  <component
    :is="link ? RouterLink : 'section'"
    v-if="active"
    v-bind="link ? { to: link } : {}"
    class="status-banner live on-air-strip"
  >
    <h2 class="strip-title"><span class="dot" aria-hidden="true"></span>On air now</h2>
    <template v-if="running">
      <span class="strip-show">{{ name }}</span>
      <span v-if="running.host_username" class="strip-host">{{ running.host_username }}</span>
      <span v-if="running.end_time" class="strip-until">until {{ running.end_time }}</span>
    </template>
  </component>
</template>

<style scoped>
.on-air-strip {
  justify-content: flex-start;
  align-items: baseline;
  gap: 6px 18px;
  padding: var(--spacing-md) var(--spacing-lg);
  border: 1px solid #5a2424;
  background-color: #1d0b0b;
  color: #fff;
  text-decoration: none;
}

a.on-air-strip:hover {
  background-color: #2a0f0f;
  color: #fff;
}

.strip-title {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0;
  font: 700 12px var(--font-family);
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: #ffb3ab;
}

.strip-show {
  font: 700 1.375rem/1.2 var(--font-family);
  color: #fff;
}

.strip-host {
  font-weight: 600;
  color: #d0c4c4;
}

.strip-until {
  font: 700 15px var(--font-family);
  color: #ffb3ab;
}

@media (max-width: 640px) {
  .on-air-strip {
    gap: 4px 12px;
    padding: 14px var(--spacing-md);
  }

  .strip-title {
    width: 100%;
  }

  .strip-show {
    font-size: 1.25rem;
  }
}
</style>
