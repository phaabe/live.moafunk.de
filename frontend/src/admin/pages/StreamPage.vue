<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
import { streamApi } from '../api';
import { BaseButton, BaseModal } from '@shared/components';
import { useFlash, useStreamSocket, useAudioCapture, useAudioMeter } from '../composables';
import { useAuthStore } from '../stores/auth';

const authStore = useAuthStore();
const flash = useFlash();

// Stream status polling
const currentStreamer = ref<string | null>(null);
const isOtherUserStreaming = computed(() => 
  currentStreamer.value !== null && currentStreamer.value !== authStore.user?.username
);

// Composables
const streamSocket = useStreamSocket({
  onLive: () => {
    audioCapture.startRecording();
    flash.success('Stream started');
    checkStreamStatus();
  },
  onDisconnected: () => {
    audioCapture.stopRecording();
    checkStreamStatus();
  },
  onError: (err) => {
    flash.error(err);
  },
});

const audioCapture = useAudioCapture({
  onData: (data) => {
    streamSocket.send(data);
  },
  onError: (err) => {
    flash.error(err);
  },
});

const audioMeter = useAudioMeter(audioCapture.mediaStream);

// Takeover modal
const showTakeoverModal = ref(false);

// Device selection
const selectedDevice = ref('');

// Status text
const statusText = computed(() => {
  switch (streamSocket.state.value) {
    case 'disconnected':
      return audioCapture.isCapturing.value ? 'Audio Ready' : 'Disconnected';
    case 'connecting':
      if (streamSocket.reconnectAttempts.value > 0) {
        return `Reconnecting (${streamSocket.reconnectAttempts.value}/${streamSocket.maxReconnectAttempts})...`;
      }
      return 'Connecting...';
    case 'connected':
      return 'Connected';
    case 'live':
      return 'Live';
    case 'error':
      return 'Error';
    default:
      return 'Unknown';
  }
});

// Status indicator class
const statusClass = computed(() => {
  if (audioCapture.isCapturing.value && streamSocket.state.value === 'disconnected') {
    return 'connecting'; // Show yellow when audio is ready
  }
  return streamSocket.state.value;
});

async function checkStreamStatus() {
  try {
    const status = await streamApi.status();
    currentStreamer.value = status.active ? (status.user || 'Unknown') : null;
  } catch (e) {
    console.error('Failed to check stream status:', e);
  }
}

async function handleDeviceSelect() {
  if (!selectedDevice.value) {
    flash.error('Please select an audio device first');
    return;
  }
  await audioCapture.captureDevice(selectedDevice.value);
}

async function handleScreenShare() {
  await audioCapture.captureScreenAudio();
}

async function handleStartStream(force = false) {
  if (!audioCapture.isCapturing.value) {
    flash.error('No audio source selected');
    return;
  }

  try {
    await streamSocket.connect(force);
  } catch (e) {
    // Error already handled by callback
  }
}

function handleStopStream() {
  streamSocket.disconnect();
  audioCapture.stopCapture();
}

function confirmTakeover() {
  showTakeoverModal.value = true;
}

async function handleTakeover() {
  showTakeoverModal.value = false;
  
  if (!audioCapture.isCapturing.value) {
    // Need to get audio first
    const success = await audioCapture.captureScreenAudio();
    if (success) {
      await handleStartStream(true);
    }
  } else {
    await handleStartStream(true);
  }
}

function handleRetry() {
  streamSocket.resetReconnect();
  handleStartStream();
}

// Polling for stream status
let statusInterval: ReturnType<typeof setInterval> | null = null;

onMounted(async () => {
  await audioCapture.refreshDevices();
  await checkStreamStatus();
  statusInterval = setInterval(checkStreamStatus, 5000);
});

onUnmounted(() => {
  if (statusInterval) {
    clearInterval(statusInterval);
  }
});
</script>

<template>
  <div class="stream-page">
    <div class="page-header">
      <h1 class="page-title">🎵 Audio Stream</h1>
    </div>

    <div class="stream-panel card">
      <!-- Status Indicator -->
      <div class="status-indicator">
        <div :class="['status-dot', statusClass]"></div>
        <span class="status-text">{{ statusText }}</span>
      </div>

      <!-- Current Streamer Info -->
      <div v-if="currentStreamer" class="current-streamer">
        Currently streaming: <strong>{{ currentStreamer }}</strong>
      </div>

      <!-- Audio Level Meter -->
      <div v-if="audioCapture.isCapturing.value" class="audio-level">
        <div class="audio-level-bar" :style="{ width: `${audioMeter.level.value}%` }"></div>
      </div>

      <!-- Controls -->
      <div class="controls">
        <!-- Device Selection (shown when not capturing) -->
        <template v-if="!audioCapture.isCapturing.value && streamSocket.state.value !== 'live'">
          <div class="audio-source-selector">
            <label for="device-select">Audio Source:</label>
            <select id="device-select" v-model="selectedDevice" class="device-select">
              <option value="">-- Select audio device --</option>
              <option
                v-for="device in audioCapture.devices.value"
                :key="device.deviceId"
                :value="device.deviceId"
              >
                {{ device.label }}
              </option>
            </select>
            <BaseButton variant="ghost" size="sm" @click="audioCapture.refreshDevices()">
              🔄 Refresh
            </BaseButton>
          </div>

          <BaseButton variant="primary" @click="handleDeviceSelect">
            🎧 Use Selected Device
          </BaseButton>

          <BaseButton variant="secondary" @click="handleScreenShare">
            🖥️ Share Screen Audio (Windows only)
          </BaseButton>
        </template>

        <!-- Start Button (shown when audio ready, not streaming) -->
        <BaseButton
          v-if="audioCapture.isCapturing.value && streamSocket.state.value === 'disconnected'"
          variant="primary"
          size="lg"
          @click="() => handleStartStream()"
        >
          ▶️ Start Streaming
        </BaseButton>

        <!-- Stop Button (shown when live) -->
        <BaseButton
          v-if="streamSocket.state.value === 'live'"
          variant="danger"
          size="lg"
          @click="handleStopStream"
        >
          ⏹️ Stop Streaming
        </BaseButton>

        <!-- Takeover Button (shown when another user is streaming) -->
        <BaseButton
          v-if="isOtherUserStreaming && streamSocket.state.value !== 'live'"
          variant="secondary"
          @click="confirmTakeover"
        >
          🔄 Take Over Stream
        </BaseButton>

        <!-- Retry Button (shown on error after max reconnects) -->
        <BaseButton
          v-if="streamSocket.state.value === 'error'"
          variant="secondary"
          @click="handleRetry"
        >
          🔄 Retry Connection
        </BaseButton>
      </div>

      <!-- Help Text -->
      <div class="info-text">
        <p>
          <strong>macOS users:</strong> Install
          <a href="https://existential.audio/blackhole/" target="_blank">BlackHole</a>
          to capture system audio. Route your audio through BlackHole, then select it above.
        </p>
        <p>
          <strong>Windows users:</strong> Click "Share Screen Audio" and check "Share audio" in the dialog.
        </p>
        <p>The audio will be streamed to the Moafunk radio server.</p>
      </div>
    </div>

    <!-- Takeover Confirmation Modal -->
    <BaseModal :open="showTakeoverModal" title="⚠️ Take Over Stream?" @close="showTakeoverModal = false">
      <p>
        <strong>{{ currentStreamer }}</strong> is currently streaming.
        Taking over will stop their stream.
      </p>
      <template #footer>
        <BaseButton variant="ghost" @click="showTakeoverModal = false">Cancel</BaseButton>
        <BaseButton variant="danger" @click="handleTakeover">Take Over</BaseButton>
      </template>
    </BaseModal>
  </div>
</template>

<style scoped>
.stream-panel {
  max-width: 800px;
  margin: 0 auto;
  text-align: center;
}

.status-indicator {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--spacing-md);
  margin-bottom: var(--spacing-lg);
  padding: var(--spacing-md);
  background-color: var(--color-surface-alt);
  border-radius: var(--radius-md);
}

.status-dot {
  width: 14px;
  height: 14px;
  border-radius: 50%;
  animation: pulse 2s infinite;
}

.status-dot.disconnected {
  background-color: var(--color-text-muted);
  animation: none;
}

.status-dot.connecting {
  background-color: var(--color-primary);
}

.status-dot.connected,
.status-dot.live {
  background-color: var(--color-success);
}

.status-dot.error {
  background-color: var(--color-error);
  animation: none;
}

@keyframes pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.5;
  }
}

.status-text {
  font-size: var(--font-size-lg);
  font-weight: var(--font-weight-bold);
}

.current-streamer {
  margin-bottom: var(--spacing-lg);
  color: var(--color-text-muted);
}

.current-streamer strong {
  color: var(--color-primary);
}

.audio-level {
  width: 100%;
  height: 8px;
  background-color: var(--color-surface-alt);
  border-radius: var(--radius-full);
  margin-bottom: var(--spacing-lg);
  overflow: hidden;
}

.audio-level-bar {
  height: 100%;
  background: linear-gradient(90deg, var(--color-success), var(--color-primary), var(--color-error));
  transition: width 0.1s;
}

.controls {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  align-items: center;
  margin-bottom: var(--spacing-xl);
}

.audio-source-selector {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
  justify-content: center;
}

.audio-source-selector label {
  color: var(--color-text-muted);
}

.device-select {
  background-color: var(--color-surface-alt);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-family: var(--font-family);
  font-size: var(--font-size-md);
  padding: var(--spacing-sm) var(--spacing-md);
  min-width: 250px;
  cursor: pointer;
}

.device-select:focus {
  outline: none;
  border-color: var(--color-primary);
}

.info-text {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  line-height: 1.6;
}

.info-text p {
  margin-bottom: var(--spacing-sm);
}

.info-text a {
  color: var(--color-primary);
  text-decoration: none;
}

.info-text a:hover {
  text-decoration: underline;
}
</style>
