<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { settingsApi, type NotificationSettings, type ServiceSettings } from '../api';

// --- Moafunkbot: one switch per Telegram integration ---
const bot = ref<NotificationSettings>({ enabled: true, live_chat: true, onair_message: true });
const botLoading = ref(true);
const BOT_SWITCHES: { key: keyof NotificationSettings; label: string }[] = [
  { key: 'enabled', label: 'Technical alerts' },
  { key: 'live_chat', label: 'Live chat' },
  { key: 'onair_message', label: 'On-air message' },
];

async function loadBotSettings() {
  try {
    bot.value = await settingsApi.getNotifications();
  } catch (e) {
    console.error('Failed to load notification settings:', e);
  } finally {
    botLoading.value = false;
  }
}

async function toggleBot(key: keyof NotificationSettings) {
  const before = bot.value;
  bot.value = { ...before, [key]: !before[key] };
  try {
    bot.value = await settingsApi.setNotifications({ [key]: bot.value[key] });
  } catch (e) {
    bot.value = before;
    console.error('Failed to update notification settings:', e);
  }
}

// --- Connections: SoundCloud / Instagram ---
const services = ref<ServiceSettings>({ soundcloud: true, instagram: true });
const servicesLoading = ref(true);
const SERVICES: { key: keyof ServiceSettings; label: string }[] = [
  { key: 'soundcloud', label: 'SoundCloud' },
  { key: 'instagram', label: 'Instagram' },
];

async function loadServices() {
  try {
    services.value = await settingsApi.getServices();
  } catch (e) {
    console.error('Failed to load service settings:', e);
  } finally {
    servicesLoading.value = false;
  }
}

async function toggleService(key: keyof ServiceSettings) {
  const before = services.value;
  services.value = { ...before, [key]: !before[key] };
  try {
    services.value = await settingsApi.setServices(services.value);
  } catch (e) {
    services.value = before;
    console.error('Failed to update service settings:', e);
  }
}

onMounted(() => {
  loadBotSettings();
  loadServices();
});
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
          <div v-for="item in BOT_SWITCHES" :key="item.key" class="notification-row service-row">
            <div class="notification-info">
              <span class="notification-label">{{ item.label }}</span>
              <span :class="['badge', bot[item.key] ? 'badge-success' : 'badge-error']">
                {{ bot[item.key] ? 'Enabled' : 'Disabled' }}
              </span>
            </div>
            <label class="toggle-switch" :class="{ loading: botLoading }">
              <input
                type="checkbox"
                :aria-label="item.label"
                :checked="bot[item.key]"
                :disabled="botLoading"
                @change="toggleBot(item.key)"
              />
              <span class="toggle-slider"></span>
            </label>
          </div>
          <p class="text-muted notification-hint">
            Technical alerts: all bot messages to the admin group (stream alerts, artist
            submissions, show updates, Instagram previews). Live chat: listener messages in On Air
            and host replies to the live chat group. On-air message: the post to the Moafunk channel
            at go-live.
          </p>
        </div>
      </div>

      <div class="card config-card">
        <div class="card-header">
          <h2 class="card-title">🔌 Connections</h2>
        </div>
        <div class="card-body">
          <div v-for="service in SERVICES" :key="service.key" class="notification-row service-row">
            <div class="notification-info">
              <span class="notification-label">{{ service.label }}</span>
              <span :class="['badge', services[service.key] ? 'badge-success' : 'badge-error']">
                {{ services[service.key] ? 'Enabled' : 'Disabled' }}
              </span>
            </div>
            <label class="toggle-switch" :class="{ loading: servicesLoading }">
              <input
                type="checkbox"
                :aria-label="service.label"
                :checked="services[service.key]"
                :disabled="servicesLoading"
                @change="toggleService(service.key)"
              />
              <span class="toggle-slider"></span>
            </label>
          </div>
          <p class="text-muted notification-hint">
            Off blocks every post and upload there. Hosts see “temporarily not available”. Scheduled
            Instagram posts wait until it is on again.
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

.service-row + .service-row {
  margin-top: var(--spacing-md);
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
