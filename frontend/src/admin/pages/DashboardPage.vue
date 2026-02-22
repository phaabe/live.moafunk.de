<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue';
import { useRouter } from 'vue-router';
import { showsApi, streamApi, settingsApi, type Show, type StreamStatus } from '../api';
import ShowList from '../components/ShowList.vue';
import MonthCalendar from '../components/MonthCalendar.vue';

const router = useRouter();

// --- Notifications ---
const notificationsEnabled = ref(true);
const notificationsLoading = ref(true);

async function loadNotificationSettings() {
  try {
    const response = await settingsApi.getNotifications();
    notificationsEnabled.value = response.enabled;
  } catch (e) {
    console.error('Failed to load notification settings:', e);
  } finally {
    notificationsLoading.value = false;
  }
}

async function toggleNotifications() {
  const newValue = !notificationsEnabled.value;
  notificationsEnabled.value = newValue;
  try {
    await settingsApi.setNotifications(newValue);
  } catch (e) {
    // Revert on failure
    notificationsEnabled.value = !newValue;
    console.error('Failed to update notification settings:', e);
  }
}

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
const shows = ref<Show[]>([]);
const showsLoading = ref(true);

async function loadShows() {
  try {
    const response = await showsApi.list();
    shows.value = response.shows;
  } catch (e) {
    console.error('Failed to load shows:', e);
  } finally {
    showsLoading.value = false;
  }
}

function goToShow(show: Show) {
  router.push(`/shows/${show.id}`);
}

onMounted(async () => {
  // Load all data in parallel
  await Promise.all([loadNotificationSettings(), loadStreamStatus(), loadShows()]);

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
      <!-- Moafunkbot Notifications Card -->
      <div class="card dashboard-card">
        <div class="card-header">
          <h2 class="card-title">🤖 Moafunkbot</h2>
        </div>
        <div class="card-body">
          <div class="notification-row">
            <div class="notification-info">
              <span class="notification-label">Telegram Notifications</span>
              <span :class="['badge', notificationsEnabled ? 'badge-success' : 'badge-error']">
                {{ notificationsEnabled ? 'Enabled' : 'Disabled' }}
              </span>
            </div>
            <label class="toggle-switch" :class="{ loading: notificationsLoading }">
              <input type="checkbox" :checked="notificationsEnabled" :disabled="notificationsLoading"
                @change="toggleNotifications" />
              <span class="toggle-slider"></span>
            </label>
          </div>
          <p class="text-muted notification-hint">
            Controls all bot notifications: stream alerts, artist submissions, show updates, Instagram previews.
          </p>
        </div>
      </div>

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
          <MonthCalendar v-else class="compact" :shows="shows"
            @day-click="(dateStr: string) => router.push(`/calendar?date=${dateStr}`)" />
        </div>
      </div>

      <!-- List View Card -->
      <div class="card dashboard-card dashboard-shows-card">
        <div class="card-header">
          <h2 class="card-title">📋 Upcoming Shows</h2>
          <router-link to="/calendar?view=list" class="view-all-link">View all →</router-link>
        </div>
        <div class="card-body">
          <div v-if="showsLoading" class="loading-spinner"></div>
          <ShowList v-else :shows="shows" :limit="3" filter="upcoming" @show-click="goToShow" />
        </div>
      </div>
    </div><!-- end dashboard-grid -->
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

/* ===== Notification toggle ===== */
.notification-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-md);
}

.notification-info {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
}

.notification-label {
  font-weight: var(--font-weight-medium);
  color: var(--color-text);
}

.notification-hint {
  font-size: var(--font-size-sm);
  margin-top: var(--spacing-md);
  margin-bottom: 0;
}

/* Toggle switch */
.toggle-switch {
  position: relative;
  display: inline-block;
  width: 44px;
  height: 24px;
  flex-shrink: 0;
  cursor: pointer;
}

.toggle-switch.loading {
  opacity: 0.5;
  pointer-events: none;
}

.toggle-switch input {
  opacity: 0;
  width: 0;
  height: 0;
}

.toggle-slider {
  position: absolute;
  inset: 0;
  background-color: var(--color-surface-alt, #444);
  border-radius: 12px;
  transition: background-color var(--transition-fast);
}

.toggle-slider::before {
  content: '';
  position: absolute;
  height: 18px;
  width: 18px;
  left: 3px;
  bottom: 3px;
  background-color: var(--color-text);
  border-radius: 50%;
  transition: transform var(--transition-fast);
}

.toggle-switch input:checked+.toggle-slider {
  background-color: var(--color-success, #34c759);
}

.toggle-switch input:checked+.toggle-slider::before {
  transform: translateX(20px);
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
