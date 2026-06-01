<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { settingsApi } from '../api';

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

onMounted(loadNotificationSettings);
</script>

<template>
  <div class="config-page">
    <div class="page-header">
      <h1 class="page-title">Config</h1>
    </div>

    <div class="config-grid">
      <!-- Moafunkbot Notifications Card -->
      <div class="card config-card">
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
              <input
                type="checkbox"
                :checked="notificationsEnabled"
                :disabled="notificationsLoading"
                @change="toggleNotifications"
              />
              <span class="toggle-slider"></span>
            </label>
          </div>
          <p class="text-muted notification-hint">
            Controls all bot notifications: stream alerts, artist submissions, show updates,
            Instagram previews.
          </p>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.config-page {
  max-width: 1200px;
}

.config-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--spacing-lg);
}

.config-card {
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

.toggle-switch input:checked + .toggle-slider {
  background-color: var(--color-success, #34c759);
}

.toggle-switch input:checked + .toggle-slider::before {
  transform: translateX(20px);
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
  .config-grid {
    grid-template-columns: 1fr;
  }
}
</style>
