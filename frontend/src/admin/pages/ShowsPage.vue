<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { showsApi, type ScheduleItem, type MyShowInfo } from '../api';
import { useAuthStore } from '../stores/auth';
import { useHostFlow } from '@admin/composables';
import { BaseButton } from '@shared/components';
import ShowList from '../components/ShowList.vue';

const router = useRouter();
const authStore = useAuthStore();
const flow = useHostFlow();

// ─── My Shows (the user's own assignments, with streaming-prep actions) ───
// Hide ended shows; the section itself is hidden when the user has none
// (typical for pure admins who aren't assigned to any show).
const myShows = computed(() => flow.shows.value.filter((s) => !flow.isShowEnded(s)));

/** Select a show and jump to the right place in the streaming flow. */
function pickShow(s: MyShowInfo) {
  flow.selectShow(s);
  // A confirmed/running show resumes on-air; otherwise the media type is chosen
  // on the show dashboard (the mode-selection step has been removed).
  if (flow.currentStep.value === 'on-air') {
    router.push('/stream/on-air');
  } else {
    router.push(`/shows/${s.id}`);
  }
}

// ─── All Shows (full schedule) ───
const shows = ref<ScheduleItem[]>([]);
const loading = ref(true);
const error = ref<string | null>(null);

type ListFilter = 'all' | 'upcoming' | 'past';
const listFilter = ref<ListFilter>('upcoming');

function getDaysUntil(dateStr: string): number {
  const showDate = new Date(dateStr);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  showDate.setHours(0, 0, 0, 0);
  const diffTime = showDate.getTime() - today.getTime();
  return Math.ceil(diffTime / (1000 * 60 * 60 * 24));
}

const showCount = computed(() => {
  let filtered = shows.value;
  if (listFilter.value === 'upcoming') {
    filtered = shows.value.filter((s) => getDaysUntil(s.date) >= 0);
  } else if (listFilter.value === 'past') {
    filtered = shows.value.filter((s) => getDaysUntil(s.date) < 0);
  }
  return filtered.length;
});

function goToShow(show: ScheduleItem) {
  router.push(`/shows/${show.id}`);
}

function openCreateWizard() {
  router.push('/shows/new');
}

async function loadShows() {
  loading.value = true;
  error.value = null;
  try {
    // Admins get the full editable list; hosts read the open schedule overview.
    const response = authStore.isAdmin ? await showsApi.list() : await showsApi.overview();
    shows.value = response.shows;
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load shows';
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  // My Shows + the full schedule load independently.
  flow.fetchMyShow();
  loadShows();
});

// ─── Card helpers (My Shows) ───
function fmtDate(dateStr: string): string {
  const d = new Date(dateStr + 'T12:00:00');
  return d.toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

function daysLabel(dateStr: string): string {
  const days = getDaysUntil(dateStr);
  if (days === 0) return 'Today';
  if (days === 1) return 'Tomorrow';
  if (days < 0) return `${Math.abs(days)}d ago`;
  return `In ${days}d`;
}

function daysClass(dateStr: string): string {
  const days = getDaysUntil(dateStr);
  if (days < 0) return 'days-past';
  if (days === 0) return 'days-today';
  if (days <= 3) return 'days-soon';
  return 'days-future';
}

function showTypeBadge(type: string): string {
  switch (type) {
    case 'unheard':
      return 'UNHEARD';
    case 'brunchtime':
      return 'Brunchtime';
    case 'external':
      return 'External';
    default:
      return type;
  }
}
</script>

<template>
  <div class="shows-page">
    <div class="page-header">
      <h1 class="page-title">Shows</h1>
      <div class="page-header-actions">
        <BaseButton variant="primary" @click="openCreateWizard">+ New Show</BaseButton>
      </div>
    </div>

    <!-- My Shows — own assignments with streaming-prep actions -->
    <section v-if="myShows.length > 0" class="my-shows">
      <h2 class="section-title">My Shows</h2>
      <p class="section-subtitle">Select a show to prepare for streaming.</p>

      <div class="show-cards">
        <div
          v-for="s in myShows"
          :key="s.id"
          class="show-card"
          role="button"
          tabindex="0"
          @click="goToShow(s)"
          @keydown.enter.self="goToShow(s)"
        >
          <div class="show-card-header">
            <span class="show-card-type">{{ showTypeBadge(s.show_type) }}</span>
            <span :class="['show-card-days', daysClass(s.date)]">{{ daysLabel(s.date) }}</span>
          </div>
          <h3 class="show-card-title">{{ s.title }}</h3>
          <div class="show-card-date">{{ fmtDate(s.date) }}</div>
          <div v-if="s.start_time" class="show-card-time">
            {{ s.start_time }}<template v-if="s.end_time"> – {{ s.end_time }}</template>
          </div>
          <div v-if="s.artists.length > 0" class="show-card-artists">
            <span v-for="artist in s.artists" :key="artist.id" class="artist-chip">
              {{ artist.name }}
            </span>
          </div>
          <div v-if="flow.isShowRunning(s)" class="show-card-status status-live">🔴 Live Now</div>
          <div v-else-if="s.prerecorded_confirmed_at" class="show-card-status status-confirmed">
            ✓ Pre-recorded &amp; confirmed
          </div>
          <div v-else-if="s.prerecorded_key" class="show-card-status status-uploaded">
            ↑ Uploaded — needs confirmation
          </div>
          <div class="show-card-actions">
            <BaseButton variant="primary" size="sm" @click.stop="pickShow(s)">
              {{ flow.isShowRunning(s) ? '🔴 Go on air' : 'Prepare to stream' }}
            </BaseButton>
          </div>
        </div>
      </div>
    </section>

    <!-- All Shows — full schedule -->
    <section class="all-shows">
      <h2 class="section-title">All Shows</h2>

      <div v-if="error" class="flash-message error">{{ error }}</div>
      <div v-if="loading" class="loading-spinner"></div>

      <template v-else>
        <div class="list-toolbar">
          <div class="list-filters">
            <button
              :class="['filter-btn', { active: listFilter === 'upcoming' }]"
              @click="listFilter = 'upcoming'"
            >
              Upcoming
            </button>
            <button
              :class="['filter-btn', { active: listFilter === 'all' }]"
              @click="listFilter = 'all'"
            >
              All
            </button>
            <button
              :class="['filter-btn', { active: listFilter === 'past' }]"
              @click="listFilter = 'past'"
            >
              Past
            </button>
          </div>
          <span class="list-count text-muted">{{ showCount }} shows</span>
        </div>

        <ShowList :shows="shows" :filter="listFilter" @show-click="goToShow" />
      </template>
    </section>
  </div>
</template>

<style scoped>
.shows-page {
  max-width: 1200px;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--spacing-lg);
}

.section-title {
  font-size: var(--font-size-2xl);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin: 0 0 var(--spacing-xs);
}

.section-subtitle {
  color: var(--color-text-muted);
  margin: 0 0 var(--spacing-lg);
}

/* ─── My Shows ─── */
.my-shows {
  margin-bottom: var(--spacing-2xl);
  padding-bottom: var(--spacing-xl);
  border-bottom: 1px solid var(--color-border);
}

.show-cards {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.show-card {
  display: block;
  width: 100%;
  text-align: left;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-lg);
  cursor: pointer;
  transition:
    border-color var(--transition-fast),
    box-shadow var(--transition-fast);
  font-family: var(--font-family);
}

.show-card:hover {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 1px var(--color-primary);
}

.show-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--spacing-sm);
}

.show-card-type {
  font-size: var(--font-size-xs);
  font-weight: var(--font-weight-bold);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--color-text-muted);
}

.show-card-days {
  font-size: var(--font-size-xs);
  font-weight: var(--font-weight-bold);
  padding: 2px 8px;
  border-radius: var(--radius-full);
}

.days-today {
  background: rgba(52, 199, 89, 0.2);
  color: #34c759;
}

.days-soon {
  background: rgba(255, 204, 0, 0.2);
  color: #ffcc00;
}

.days-future {
  background: rgba(94, 152, 210, 0.15);
  color: #5e98d2;
}

.days-past {
  background: rgba(142, 142, 147, 0.15);
  color: #8e8e93;
}

.show-card-title {
  font-size: var(--font-size-xl);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin: 0 0 var(--spacing-xs);
}

.show-card-date {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.show-card-time {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  margin-top: 2px;
}

.show-card-artists {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-xs);
  margin-top: var(--spacing-md);
}

.artist-chip {
  background: var(--color-surface-alt);
  color: var(--color-text);
  padding: 2px var(--spacing-sm);
  border-radius: var(--radius-full);
  font-size: var(--font-size-xs);
  border: 1px solid var(--color-border);
}

.show-card-status {
  margin-top: var(--spacing-md);
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-medium);
}

.status-confirmed {
  color: #34c759;
}

.status-uploaded {
  color: #ffcc00;
}

.status-live {
  color: #ef4444;
  font-weight: var(--font-weight-bold);
}

.show-card-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--spacing-md);
}

/* ─── All Shows toolbar ─── */
.list-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--spacing-lg);
}

.list-filters {
  display: flex;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  overflow: hidden;
}

.filter-btn {
  background: transparent;
  border: none;
  color: var(--color-text-muted);
  font-family: var(--font-family);
  font-size: var(--font-size-lg);
  padding: var(--spacing-sm) var(--spacing-xl);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.filter-btn:hover {
  background: var(--color-surface-hover);
  color: var(--color-text);
}

.filter-btn.active {
  background: var(--color-primary);
  color: var(--color-primary-text);
  font-weight: var(--font-weight-bold);
}

.list-count {
  font-size: var(--font-size-sm);
}
</style>
