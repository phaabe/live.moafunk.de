<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue';
import { useRouter } from 'vue-router';
import { showsApi, streamApi, type ScheduleItem, type StreamStatus } from '../api';
import { useAuthStore } from '../stores/auth';
import ShowList from '../components/ShowList.vue';
import MonthCalendar from '../components/MonthCalendar.vue';

const router = useRouter();
const authStore = useAuthStore();

// --- Stream Status ---
const streamStatus = ref<StreamStatus>({ active: false });
const streamLoading = ref(true);
let streamPollTimer: ReturnType<typeof setInterval> | null = null;

async function loadStreamStatus() {
  try {
    streamStatus.value = await streamApi.status();
  } catch (e) {
    console.error('Failed to load stream status:', e);
  } finally {
    streamLoading.value = false;
  }
}

// --- Shows ---
const shows = ref<ScheduleItem[]>([]);
const showsLoading = ref(true);

async function loadShows() {
  try {
    // Admins get the full editable list; hosts read the open schedule overview.
    const response = authStore.isAdmin ? await showsApi.list() : await showsApi.overview();
    shows.value = response.shows;
  } catch (e) {
    console.error('Failed to load shows:', e);
  } finally {
    showsLoading.value = false;
  }
}

function goToShow(show: ScheduleItem) {
  router.push(`/shows/${show.id}`);
}

onMounted(async () => {
  // Load all data in parallel
  await Promise.all([loadStreamStatus(), loadShows()]);

  // Poll stream status every 10s
  streamPollTimer = setInterval(loadStreamStatus, 10000);
});

onUnmounted(() => {
  if (streamPollTimer) {
    clearInterval(streamPollTimer);
    streamPollTimer = null;
  }
});
</script>

<template>
  <div class="dashboard-page">
    <div class="page-header">
      <h1 class="page-title">Dashboard</h1>
    </div>

    <div class="dashboard-grid">
      <!-- Stream Status Card -->
      <div class="card dashboard-card">
        <div class="card-header">
          <h2 class="card-title">📡 Stream</h2>
        </div>
        <div class="card-body">
          <div v-if="streamLoading" class="loading-spinner"></div>
          <template v-else>
            <div class="stream-status-row">
              <span :class="['badge', streamStatus.active ? 'badge-success' : 'badge-error']">
                {{ streamStatus.active ? 'LIVE' : 'Off Air' }}
              </span>
              <span v-if="streamStatus.active && streamStatus.user" class="stream-user">
                {{ streamStatus.user }}
              </span>
            </div>
            <p v-if="!streamStatus.active" class="text-muted stream-hint">
              No active stream. Hosts can start streaming from their show page.
            </p>
          </template>
        </div>
      </div>

      <!-- Month View Card -->
      <div class="card dashboard-card calendar-month-card">
        <div class="card-header">
          <h2 class="card-title">📅 Month</h2>
          <router-link to="/calendar" class="view-all-link">Open calendar →</router-link>
        </div>
        <div class="card-body card-body-flush">
          <div v-if="showsLoading" class="loading-spinner"></div>
          <MonthCalendar
            v-else
            class="compact"
            :shows="shows"
            @day-click="(dateStr: string) => router.push(`/calendar?date=${dateStr}`)"
          />
        </div>
      </div>

      <!-- List View Card -->
      <div class="card dashboard-card dashboard-shows-card">
        <div class="card-header">
          <h2 class="card-title">📋 Upcoming Shows</h2>
          <router-link to="/shows" class="view-all-link">View all →</router-link>
        </div>
        <div class="card-body">
          <div v-if="showsLoading" class="loading-spinner"></div>
          <ShowList v-else :shows="shows" :limit="3" filter="upcoming" @show-click="goToShow" />
        </div>
      </div>
    </div>
    <!-- end dashboard-grid -->
  </div>
</template>

<style scoped>
.dashboard-page {
  max-width: 1200px;
}

.dashboard-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--spacing-lg);
}

.dashboard-card {
  padding: 0;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--spacing-md) var(--spacing-lg);
  border-bottom: 1px solid var(--color-border);
}

.card-title {
  font-size: var(--font-size-base);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin: 0;
}

.card-body {
  padding: var(--spacing-lg);
}

/* ===== Stream status ===== */
.stream-status-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-md);
}

.stream-user {
  font-weight: var(--font-weight-medium);
  color: var(--color-text);
}

.stream-hint {
  font-size: var(--font-size-sm);
  margin-top: var(--spacing-md);
  margin-bottom: 0;
}

/* ===== Shows section ===== */
.dashboard-shows-card .card-body {
  padding: var(--spacing-md) var(--spacing-lg) var(--spacing-lg);
}

/* Cards in second row should align to top */
.calendar-month-card,
.dashboard-shows-card {
  align-self: start;
}

.card-body-flush {
  padding: 0;
}

.calendar-month-card .card-body-flush {
  overflow: hidden;
}

.view-all-link {
  font-size: var(--font-size-sm);
  color: var(--color-primary);
  text-decoration: none;
  font-weight: var(--font-weight-medium);
  transition: opacity var(--transition-fast);
}

.view-all-link:hover {
  opacity: 0.8;
}

/* Badge variants */
.badge-success {
  background-color: rgba(52, 199, 89, 0.2);
  color: #34c759;
  border: 1px solid #34c759;
}

.badge-error {
  background-color: rgba(255, 59, 48, 0.2);
  color: #ff3b30;
  border: 1px solid #ff3b30;
}

/* Responsive */
@media (max-width: 768px) {
  .dashboard-grid {
    grid-template-columns: 1fr;
  }
}
</style>
