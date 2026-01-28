<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
import { streamApi } from '../api';
import { BaseButton } from '@shared/components';
import { useFlash, useStreamSocket, useAudioCapture, useAudioMeter, useRecordingSession, useFinalizeProgress } from '../composables';

const flash = useFlash();

// Recording session composable
const recording = useRecordingSession({
  onError: (err) => flash.error(err),
  onRecordingStarted: () => flash.success('Recording started'),
  onRecordingStopped: (version) => {
    flash.success('Recording stopped and saved');
    lastRecordingVersion.value = version;
  },
});

// Finalize progress composable
const finalize = useFinalizeProgress({
  onStarted: () => {
    showFinalizePanel.value = true;
  },
  onComplete: (finalKey) => {
    flash.success(`Recording finalized: ${finalKey}`);
  },
  onError: (err) => {
    flash.error(`Finalize failed: ${err}`);
  },
  onDisconnected: () => {
    flash.error('Connection lost during finalize');
  },
});

// UI state for finalize panel
const showFinalizePanel = ref(false);
const lastRecordingVersion = ref<string | null>(null);

// Stream status
const streamActive = ref(false);

// Composables for audio capture (reused from StreamPage)
const streamSocket = useStreamSocket({
  onLive: () => {
    audioCapture.startRecording();
    flash.success('Stream connected');
  },
  onDisconnected: () => {
    audioCapture.stopRecording();
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

// Use processedStream so the meter reflects the volume slider setting
const audioMeter = useAudioMeter(audioCapture.processedStream);

// Computed
const canStartRecording = computed(() =>
  recording.selectedShow.value !== null &&
  streamSocket.state.value === 'live' &&
  !recording.isRecording.value
);

// Can finalize: have a completed recording version and not currently finalizing
const canFinalize = computed(() =>
  lastRecordingVersion.value !== null &&
  recording.selectedShow.value !== null &&
  !finalize.isRunning.value &&
  !recording.isRecording.value
);

// Selected recording for finalization from list
const selectedRecordingForFinalize = ref<{ id: number; version: string } | null>(null);

// Select a recording to finalize
function selectRecordingForFinalize(rec: { id: number; version: string; status: string }) {
  if (rec.status === 'failed' || rec.status === 'raw') {
    selectedRecordingForFinalize.value =
      selectedRecordingForFinalize.value?.id === rec.id ? null : { id: rec.id, version: rec.version };
  }
}

// Handle finalize from list selection
function handleFinalizeSelected() {
  if (!recording.selectedShow.value || !selectedRecordingForFinalize.value) return;
  finalize.startFinalize(recording.selectedShow.value.id, selectedRecordingForFinalize.value.version);
  selectedRecordingForFinalize.value = null;
}

// Format duration in ms to MM:SS
function formatDurationMs(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000);
  const mins = Math.floor(totalSeconds / 60);
  const secs = totalSeconds % 60;
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}

// Format date string
function formatDate(dateStr: string): string {
  const date = new Date(dateStr);
  return date.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

// Handle finalize button click
function handleFinalize() {
  if (!recording.selectedShow.value || !lastRecordingVersion.value) return;
  finalize.startFinalize(recording.selectedShow.value.id, lastRecordingVersion.value);
}

// Handle recording start - restart MediaRecorder to get fresh WebM header
async function handleStartRecording() {
  // Restart MediaRecorder to ensure we get a fresh WebM container with proper EBML header
  // This is crucial because the backend needs a valid WebM file for finalization
  if (!audioCapture.restartRecording()) {
    flash.error('Failed to restart audio recorder');
    return;
  }

  // Small delay to ensure the new recorder is active
  await new Promise(resolve => setTimeout(resolve, 100));

  // Now start the backend recording session
  await recording.startRecording();
}

// Close finalize panel
function closeFinalizePanel() {
  if (finalize.isRunning.value) return; // Don't allow closing while running
  showFinalizePanel.value = false;
  if (finalize.isComplete.value || finalize.isError.value) {
    finalize.reset();
  }
}

// Check stream status periodically
async function checkStreamStatus() {
  try {
    const status = await streamApi.status();
    streamActive.value = status.active;
  } catch (e) {
    console.error('Failed to check stream status:', e);
  }
}

// Device selection
const selectedDevice = ref('');

async function handleDeviceSelect() {
  if (!selectedDevice.value) {
    flash.error('Please select an audio device first');
    return;
  }
  await audioCapture.captureDevice(selectedDevice.value);
}

async function handleStartStream() {
  if (!audioCapture.isCapturing.value) {
    flash.error('No audio source selected');
    return;
  }
  await streamSocket.connect();
}

function handleStopStream() {
  if (recording.isRecording.value) {
    flash.error('Stop recording first before stopping the stream');
    return;
  }
  streamSocket.disconnect();
  audioCapture.stopCapture();
}

let statusInterval: ReturnType<typeof setInterval> | null = null;

onMounted(async () => {
  await Promise.all([
    recording.loadShows(),
    audioCapture.refreshDevices(),
    checkStreamStatus(),
    recording.checkRecordingStatus(),
  ]);
  statusInterval = setInterval(checkStreamStatus, 5000);
});

onUnmounted(() => {
  if (statusInterval) clearInterval(statusInterval);
});
</script>

<template>
  <div class="recording-page">
    <div class="page-header">
      <h1 class="page-title">🎙️ Show Recording</h1>
    </div>

    <div class="recording-layout">
      <!-- Left Panel: Stream & Recording Controls -->
      <div class="control-panel card">
        <h2>Stream Controls</h2>

        <!-- Stream Status -->
        <div class="status-indicator">
          <div :class="['status-dot', streamSocket.state.value]"></div>
          <span class="status-text">
            {{ streamSocket.state.value === 'live' ? 'Live' :
              streamSocket.state.value === 'connecting' ? 'Connecting...' :
                'Disconnected' }}
          </span>
        </div>

        <!-- Audio Level Meter -->
        <div v-if="audioCapture.isCapturing.value" class="audio-level">
          <div class="audio-level-bar" :style="{ width: `${audioMeter.level.value}%` }"></div>
        </div>

        <!-- Input Volume Control -->
        <div v-if="audioCapture.isCapturing.value" class="volume-control">
          <label class="volume-label">
            <span class="volume-icon">🔊</span>
            <span>Input: {{ Math.round(audioCapture.inputVolume.value * 100) }}%</span>
          </label>
          <input type="range" min="0" max="200" :value="audioCapture.inputVolume.value * 100"
            @input="audioCapture.setInputVolume(Number(($event.target as HTMLInputElement).value) / 100)"
            class="volume-slider" />
        </div>
        <!-- Device Selection -->
        <div v-if="!audioCapture.isCapturing.value && streamSocket.state.value !== 'live'" class="device-section">
          <label for="device-select">Audio Source:</label>
          <select id="device-select" v-model="selectedDevice" class="device-select">
            <option value="">-- Select device --</option>
            <option v-for="device in audioCapture.devices.value" :key="device.deviceId" :value="device.deviceId">
              {{ device.label }}
            </option>
          </select>
          <BaseButton variant="ghost" size="sm" @click="audioCapture.refreshDevices()">
            🔄
          </BaseButton>
          <BaseButton variant="primary" size="sm" @click="handleDeviceSelect">
            Use Device
          </BaseButton>
        </div>

        <!-- Stream Buttons -->
        <div class="stream-buttons">
          <BaseButton v-if="audioCapture.isCapturing.value && streamSocket.state.value === 'disconnected'"
            variant="primary" @click="handleStartStream">
            ▶️ Start Stream
          </BaseButton>
          <BaseButton v-if="streamSocket.state.value === 'live'" variant="secondary" @click="handleStopStream"
            :disabled="recording.isRecording.value">
            ⏹️ Stop Stream
          </BaseButton>
        </div>

        <hr class="divider" />

        <h2>Recording</h2>

        <!-- Show Selector -->
        <div class="show-selector">
          <label for="show-select">Show:</label>
          <select id="show-select" v-model="recording.selectedShowId.value" class="show-select"
            :disabled="recording.isRecording.value">
            <option :value="null">-- Select show --</option>
            <option v-for="show in recording.shows.value" :key="show.id" :value="show.id">
              {{ show.title }} ({{ show.date }})
            </option>
          </select>
        </div>

        <!-- Recording Status -->
        <div v-if="recording.isRecording.value" class="recording-status">
          <div class="recording-indicator">
            <span class="rec-dot"></span>
            <span class="rec-text">REC</span>
          </div>
          <div class="recording-timers">
            <div class="recording-time">{{ recording.formattedDuration.value }}</div>
            <div class="countdown-time" :class="{
              warning: recording.countdownWarning.value,
              critical: recording.countdownCritical.value
            }">
              <span class="countdown-label">remaining</span>
              <span class="countdown-value">{{ recording.formattedCountdown.value }}</span>
            </div>
          </div>
        </div>

        <!-- Recording Buttons -->
        <div class="recording-buttons">
          <BaseButton v-if="!recording.isRecording.value" variant="primary" :disabled="!canStartRecording"
            @click="handleStartRecording">
            🔴 Start Recording
          </BaseButton>
          <BaseButton v-if="recording.isRecording.value" variant="danger" :disabled="recording.isStopping.value"
            @click="recording.stopRecording">
            {{ recording.isStopping.value ? '⏳ Stopping...' : '⏹️ Stop Recording' }}
          </BaseButton>
        </div>

        <!-- Finalize Button -->
        <div v-if="lastRecordingVersion" class="finalize-section">
          <hr class="divider" />
          <h2>Post-Production</h2>
          <p class="version-info">Last recording: <code>{{ lastRecordingVersion }}</code></p>
          <BaseButton variant="secondary" :disabled="!canFinalize" @click="handleFinalize">
            ✨ Finalize Recording
          </BaseButton>
          <p class="help-text">Merges pre-recorded tracks with the live stream audio.</p>
        </div>

        <p v-if="!canStartRecording && !recording.isRecording.value" class="help-text">
          <template v-if="!recording.selectedShow.value">Select a show to record.</template>
          <template v-else-if="streamSocket.state.value !== 'live'">Connect to stream first.</template>
        </p>
      </div>

      <!-- Right Panel: Artist Track Grid -->
      <div class="tracks-panel card">
        <h2>
          Artist Tracks
          <span v-if="recording.preloadingTracks.value" class="preload-indicator">
            Loading tracks... {{ recording.preloadProgress.value }}%
          </span>
          <span v-else-if="recording.preloadedTracks.value.size > 0" class="preload-complete">
            ✓ {{ recording.preloadedTracks.value.size }} tracks cached
          </span>
        </h2>

        <div v-if="recording.loadingShowDetails.value" class="loading">Loading artists...</div>

        <div v-else-if="!recording.selectedShow.value" class="empty-state">
          Select a show to see artist tracks
        </div>

        <div v-else-if="recording.artists.value.length === 0" class="empty-state">
          No artists assigned to this show
        </div>

        <div v-else class="artists-grid">
          <div v-for="artist in recording.artists.value" :key="artist.id" class="artist-card">
            <h3 class="artist-name">{{ artist.name }}</h3>
            <div class="artist-pronouns">{{ artist.pronouns }}</div>

            <div class="track-buttons">
              <!-- Voice Track -->
              <div class="track-row" :class="{ disabled: !artist.voice_url }">
                <button class="track-btn voice-btn" :class="{
                  playing: recording.getTrackState(artist.id, 'voice_message').playing,
                  disabled: !artist.voice_url
                }" :disabled="!artist.voice_url" @click="recording.toggleTrack(artist, 'voice_message')">
                  <span class="track-icon">🎤</span>
                  <span class="track-label">Voice</span>
                  <div v-if="recording.getTrackState(artist.id, 'voice_message').playing" class="progress-bar"
                    :style="{ width: `${recording.getTrackState(artist.id, 'voice_message').progress}%` }"></div>
                </button>
                <input v-if="artist.voice_url" type="range" min="0" max="100"
                  :value="recording.getTrackState(artist.id, 'voice_message').volume * 100"
                  @input="recording.setTrackVolume(artist.id, 'voice_message', Number(($event.target as HTMLInputElement).value) / 100)"
                  class="track-volume-slider"
                  :title="`Volume: ${Math.round(recording.getTrackState(artist.id, 'voice_message').volume * 100)}%`" />
              </div>

              <!-- Track 1 -->
              <div class="track-row" :class="{ disabled: !artist.track1_url }">
                <button class="track-btn track1-btn" :class="{
                  playing: recording.getTrackState(artist.id, 'track1').playing,
                  disabled: !artist.track1_url
                }" :disabled="!artist.track1_url" @click="recording.toggleTrack(artist, 'track1')">
                  <span class="track-icon">🎵</span>
                  <span class="track-label">{{ recording.getTrackName(artist, 'track1') }}</span>
                  <div v-if="recording.getTrackState(artist.id, 'track1').playing" class="progress-bar"
                    :style="{ width: `${recording.getTrackState(artist.id, 'track1').progress}%` }"></div>
                </button>
                <input v-if="artist.track1_url" type="range" min="0" max="100"
                  :value="recording.getTrackState(artist.id, 'track1').volume * 100"
                  @input="recording.setTrackVolume(artist.id, 'track1', Number(($event.target as HTMLInputElement).value) / 100)"
                  class="track-volume-slider"
                  :title="`Volume: ${Math.round(recording.getTrackState(artist.id, 'track1').volume * 100)}%`" />
              </div>

              <!-- Track 2 -->
              <div class="track-row" :class="{ disabled: !artist.track2_url }">
                <button class="track-btn track2-btn" :class="{
                  playing: recording.getTrackState(artist.id, 'track2').playing,
                  disabled: !artist.track2_url
                }" :disabled="!artist.track2_url" @click="recording.toggleTrack(artist, 'track2')">
                  <span class="track-icon">🎵</span>
                  <span class="track-label">{{ recording.getTrackName(artist, 'track2') }}</span>
                  <div v-if="recording.getTrackState(artist.id, 'track2').playing" class="progress-bar"
                    :style="{ width: `${recording.getTrackState(artist.id, 'track2').progress}%` }"></div>
                </button>
                <input v-if="artist.track2_url" type="range" min="0" max="100"
                  :value="recording.getTrackState(artist.id, 'track2').volume * 100"
                  @input="recording.setTrackVolume(artist.id, 'track2', Number(($event.target as HTMLInputElement).value) / 100)"
                  class="track-volume-slider"
                  :title="`Volume: ${Math.round(recording.getTrackState(artist.id, 'track2').volume * 100)}%`" />
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Recordings List Panel -->
    <div v-if="recording.selectedShow.value" class="recordings-section">
      <div class="card recordings-panel">
        <div class="recordings-header">
          <h2>📼 Recordings</h2>
          <BaseButton variant="ghost" size="sm" @click="recording.loadRecordings">
            🔄 Refresh
          </BaseButton>
        </div>

        <div v-if="recording.loadingRecordings.value" class="loading">Loading recordings...</div>

        <div v-else-if="recording.recordings.value.length === 0" class="empty-state-small">
          No recordings yet for this show
        </div>

        <div v-else class="recordings-list">
          <!-- Raw/Pending Recordings -->
          <div v-if="recording.rawRecordings.value.length > 0" class="recordings-group">
            <h3 class="group-label">📝 Pending Finalization</h3>
            <div v-for="rec in recording.rawRecordings.value" :key="rec.id" class="recording-item" :class="{
              selected: selectedRecordingForFinalize?.id === rec.id,
              failed: rec.status === 'failed'
            }" @click="selectRecordingForFinalize(rec)">
              <div class="recording-info">
                <span class="recording-version">{{ rec.version }}</span>
                <span class="recording-meta">
                  {{ rec.marker_count }} markers
                  <span v-if="rec.duration_ms"> · {{ formatDurationMs(rec.duration_ms) }}</span>
                </span>
              </div>
              <span v-if="rec.status === 'failed'" class="status-badge failed" :title="rec.error_message">
                ❌ Failed
              </span>
              <span v-else class="status-badge raw">Raw</span>
            </div>
            <div v-if="selectedRecordingForFinalize" class="finalize-selected-action">
              <BaseButton variant="primary" size="sm" :disabled="finalize.isRunning.value"
                @click="handleFinalizeSelected">
                ✨ Finalize Selected
              </BaseButton>
            </div>
          </div>

          <!-- Finalizing Recordings -->
          <div v-if="recording.finalizingRecordings.value.length > 0" class="recordings-group">
            <h3 class="group-label">⏳ Finalizing</h3>
            <div v-for="rec in recording.finalizingRecordings.value" :key="rec.id" class="recording-item finalizing">
              <div class="recording-info">
                <span class="recording-version">{{ rec.version }}</span>
                <span class="recording-meta">Processing...</span>
              </div>
              <span class="status-badge finalizing">⏳</span>
            </div>
          </div>

          <!-- Finalized Recordings -->
          <div v-if="recording.finalizedRecordings.value.length > 0" class="recordings-group">
            <h3 class="group-label">✅ Finalized</h3>
            <div v-for="rec in recording.finalizedRecordings.value" :key="rec.id" class="recording-item finalized">
              <div class="recording-info">
                <span class="recording-version">{{ rec.version }}</span>
                <span class="recording-meta">
                  <span v-if="rec.duration_ms">{{ formatDurationMs(rec.duration_ms) }}</span>
                  <span v-if="rec.finalized_at"> · {{ formatDate(rec.finalized_at) }}</span>
                </span>
              </div>
              <a v-if="rec.download_url" :href="rec.download_url" class="download-btn" target="_blank" @click.stop>
                ⬇️ Download
              </a>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Finalize Progress Panel -->
    <Teleport to="body">
      <div v-if="showFinalizePanel" class="finalize-overlay" @click.self="closeFinalizePanel">
        <div class="finalize-panel">
          <div class="finalize-header">
            <h2>✨ Finalizing Recording</h2>
            <button v-if="!finalize.isRunning.value" class="close-btn" @click="closeFinalizePanel">
              ✕
            </button>
          </div>

          <div class="finalize-content">
            <!-- Resumed badge -->
            <div v-if="finalize.isResumed.value" class="resumed-badge">
              ⚡ Resumed from checkpoint
            </div>

            <!-- Phase label -->
            <div class="phase-label">{{ finalize.phaseLabel.value }}</div>

            <!-- Progress bar -->
            <div class="finalize-progress">
              <div class="finalize-progress-bar"
                :class="{ error: finalize.isError.value, complete: finalize.isComplete.value }"
                :style="{ width: `${finalize.progressPercent.value}%` }"></div>
            </div>

            <!-- Percent text -->
            <div class="progress-text">{{ finalize.progressPercent.value }}%</div>

            <!-- Detail message -->
            <div class="detail-text">{{ finalize.detail.value }}</div>

            <!-- Error message -->
            <div v-if="finalize.isError.value" class="error-message">
              ❌ {{ finalize.error.value }}
            </div>

            <!-- Complete message -->
            <div v-if="finalize.isComplete.value" class="complete-message">
              ✅ Recording finalized successfully!
            </div>

            <!-- Reconnect info -->
            <div v-if="finalize.reconnectAttempts.value > 0 && finalize.isRunning.value" class="reconnect-info">
              Reconnecting... (attempt {{ finalize.reconnectAttempts.value }}/3)
            </div>
          </div>

          <div class="finalize-footer">
            <BaseButton v-if="finalize.isRunning.value" variant="secondary" @click="finalize.cancel">
              Cancel
            </BaseButton>
            <BaseButton v-else variant="primary" @click="closeFinalizePanel">
              {{ finalize.isComplete.value ? 'Done' : 'Close' }}
            </BaseButton>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.recording-page {
  padding: var(--spacing-lg);
}

.page-header {
  margin-bottom: var(--spacing-lg);
}

.page-title {
  font-size: 1.75rem;
  font-weight: 600;
}

.recording-layout {
  display: grid;
  grid-template-columns: 350px 1fr;
  gap: var(--spacing-lg);
}

@media (max-width: 900px) {
  .recording-layout {
    grid-template-columns: 1fr;
  }
}

.card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-lg);
}

.card h2 {
  font-size: 1.1rem;
  font-weight: 600;
  margin-bottom: var(--spacing-md);
  color: var(--color-text);
}

/* Control Panel */
.control-panel {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.status-indicator {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  padding: var(--spacing-sm) var(--spacing-md);
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
}

.status-dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--color-text-muted);
}

.status-dot.live {
  background: #22c55e;
  animation: pulse 1.5s infinite;
}

.status-dot.connecting {
  background: #eab308;
  animation: pulse 1s infinite;
}

.status-dot.disconnected {
  background: var(--color-text-muted);
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

.audio-level {
  height: 8px;
  background: var(--color-surface-alt);
  border-radius: var(--radius-sm);
  overflow: hidden;
}

.audio-level-bar {
  height: 100%;
  background: linear-gradient(90deg, #22c55e, #eab308, #ef4444);
  transition: width 50ms ease-out;
}

/* Volume Control */
.volume-control {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.volume-label {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
  font-size: 0.75rem;
  color: var(--color-text-muted);
}

.volume-icon {
  font-size: 0.875rem;
}

.volume-slider {
  width: 100%;
  height: 6px;
  -webkit-appearance: none;
  appearance: none;
  background: var(--color-surface-alt);
  border-radius: 3px;
  outline: none;
  cursor: pointer;
}

.volume-slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--color-primary);
  cursor: pointer;
  transition: transform 0.1s ease;
}

.volume-slider::-webkit-slider-thumb:hover {
  transform: scale(1.2);
}

.volume-slider::-moz-range-thumb {
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--color-primary);
  cursor: pointer;
  border: none;
}

.device-section {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
  align-items: center;
}

.device-select,
.show-select {
  flex: 1;
  min-width: 150px;
  padding: var(--spacing-sm);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  color: var(--color-text);
}

.stream-buttons,
.recording-buttons {
  display: flex;
  gap: var(--spacing-sm);
}

.divider {
  border: none;
  border-top: 1px solid var(--color-border);
  margin: var(--spacing-md) 0;
}

.show-selector {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.show-selector label {
  font-size: 0.875rem;
  color: var(--color-text-muted);
}

.recording-status {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--spacing-md);
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: var(--radius-md);
}

.recording-indicator {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
}

.rec-dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: #ef4444;
  animation: pulse 1s infinite;
}

.rec-text {
  font-weight: 600;
  color: #ef4444;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.recording-timers {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
}

.recording-time {
  font-family: monospace;
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--color-text);
}

.countdown-time {
  display: flex;
  align-items: baseline;
  gap: var(--spacing-xs);
  font-family: monospace;
  font-size: 0.75rem;
  color: var(--color-text-muted);
}

.countdown-time.warning {
  color: #eab308;
}

.countdown-time.critical {
  color: #ef4444;
  animation: blink 1s infinite;
}

@keyframes blink {

  0%,
  50% {
    opacity: 1;
  }

  51%,
  100% {
    opacity: 0.5;
  }
}

.countdown-label {
  font-weight: 400;
}

.countdown-value {
  font-weight: 600;
}

.help-text {
  font-size: 0.875rem;
  color: var(--color-text-muted);
  margin: 0;
}

/* Tracks Panel */
.tracks-panel {
  min-height: 400px;
}

.tracks-panel h2 {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
}

.preload-indicator {
  font-size: 0.75rem;
  font-weight: normal;
  color: var(--color-warning);
  animation: pulse 1s ease-in-out infinite;
}

.preload-complete {
  font-size: 0.75rem;
  font-weight: normal;
  color: var(--color-success);
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

.loading,
.empty-state {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 200px;
  color: var(--color-text-muted);
}

.artists-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
  gap: var(--spacing-md);
}

.artist-card {
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
  padding: var(--spacing-md);
}

.artist-name {
  font-size: 1rem;
  font-weight: 600;
  margin: 0 0 var(--spacing-xs);
  color: var(--color-text);
}

.artist-pronouns {
  font-size: 0.75rem;
  color: var(--color-text-muted);
  margin-bottom: var(--spacing-md);
}

.track-buttons {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
}

.track-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
}

.track-row.disabled {
  opacity: 0.4;
}

.track-btn {
  position: relative;
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  padding: var(--spacing-sm) var(--spacing-md);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  color: var(--color-text);
  cursor: pointer;
  overflow: hidden;
  transition: all 0.15s ease;
  flex: 1;
}

.track-btn:hover:not(.disabled) {
  background: var(--color-primary);
  color: white;
  border-color: var(--color-primary);
}

.track-btn.playing {
  background: var(--color-primary);
  color: white;
  border-color: var(--color-primary);
}

.track-btn.disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.track-icon {
  font-size: 1rem;
}

.track-label {
  flex: 1;
  text-align: left;
  font-size: 0.875rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.progress-bar {
  position: absolute;
  bottom: 0;
  left: 0;
  height: 3px;
  background: rgba(255, 255, 255, 0.5);
  transition: width 100ms linear;
}

.voice-btn.playing {
  background: #8b5cf6;
  border-color: #8b5cf6;
}

.track1-btn.playing {
  background: #0ea5e9;
  border-color: #0ea5e9;
}

.track2-btn.playing {
  background: #22c55e;
  border-color: #22c55e;
}

/* Track Volume Slider */
.track-volume-slider {
  width: 50px;
  height: 4px;
  -webkit-appearance: none;
  appearance: none;
  background: var(--color-surface-alt);
  border-radius: 2px;
  outline: none;
  cursor: pointer;
  flex-shrink: 0;
}

.track-volume-slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--color-text-muted);
  cursor: pointer;
  transition: all 0.1s ease;
}

.track-volume-slider::-webkit-slider-thumb:hover {
  background: var(--color-primary);
  transform: scale(1.2);
}

.track-volume-slider::-moz-range-thumb {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--color-text-muted);
  cursor: pointer;
  border: none;
}

.track-volume-slider::-moz-range-thumb:hover {
  background: var(--color-primary);
}

/* Finalize Section */
.finalize-section {
  margin-top: var(--spacing-sm);
}

.version-info {
  font-size: 0.875rem;
  color: var(--color-text-muted);
  margin-bottom: var(--spacing-sm);
}

.version-info code {
  font-family: monospace;
  background: var(--color-surface-alt);
  padding: 2px 6px;
  border-radius: var(--radius-sm);
}

/* Finalize Overlay */
.finalize-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  backdrop-filter: blur(4px);
}

.finalize-panel {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  width: 90%;
  max-width: 480px;
  box-shadow: 0 20px 40px rgba(0, 0, 0, 0.3);
}

.finalize-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--spacing-md) var(--spacing-lg);
  border-bottom: 1px solid var(--color-border);
}

.finalize-header h2 {
  font-size: 1.1rem;
  font-weight: 600;
  margin: 0;
}

.close-btn {
  width: 28px;
  height: 28px;
  border: none;
  background: transparent;
  color: var(--color-text-muted);
  cursor: pointer;
  border-radius: var(--radius-sm);
  font-size: 1rem;
  transition: all 0.15s ease;
}

.close-btn:hover {
  background: var(--color-surface-alt);
  color: var(--color-text);
}

.finalize-content {
  padding: var(--spacing-lg);
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.resumed-badge {
  display: inline-flex;
  align-items: center;
  gap: var(--spacing-xs);
  padding: var(--spacing-xs) var(--spacing-sm);
  background: rgba(234, 179, 8, 0.1);
  border: 1px solid rgba(234, 179, 8, 0.3);
  border-radius: var(--radius-sm);
  color: #eab308;
  font-size: 0.75rem;
  font-weight: 500;
  width: fit-content;
}

.phase-label {
  font-size: 1rem;
  font-weight: 500;
  color: var(--color-text);
}

.finalize-progress {
  height: 8px;
  background: var(--color-surface-alt);
  border-radius: var(--radius-sm);
  overflow: hidden;
}

.finalize-progress-bar {
  height: 100%;
  background: var(--color-primary);
  transition: width 200ms ease-out;
}

.finalize-progress-bar.error {
  background: #ef4444;
}

.finalize-progress-bar.complete {
  background: #22c55e;
}

.progress-text {
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--color-text);
  text-align: center;
}

.detail-text {
  font-size: 0.8125rem;
  color: var(--color-text-muted);
  text-align: center;
  min-height: 1.2em;
}

.error-message {
  padding: var(--spacing-md);
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: var(--radius-md);
  color: #ef4444;
  font-size: 0.875rem;
  text-align: center;
}

.complete-message {
  padding: var(--spacing-md);
  background: rgba(34, 197, 94, 0.1);
  border: 1px solid rgba(34, 197, 94, 0.3);
  border-radius: var(--radius-md);
  color: #22c55e;
  font-size: 0.875rem;
  text-align: center;
  font-weight: 500;
}

.reconnect-info {
  font-size: 0.75rem;
  color: #eab308;
  text-align: center;
}

.finalize-footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--spacing-sm);
  padding: var(--spacing-md) var(--spacing-lg);
  border-top: 1px solid var(--color-border);
}

/* Recordings Section */
.recordings-section {
  margin-top: var(--spacing-lg);
}

.recordings-panel {
  max-width: 100%;
}

.recordings-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--spacing-md);
}

.recordings-header h2 {
  margin: 0;
}

.recordings-list {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
}

.recordings-group {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
}

.group-label {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--color-text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin: 0;
}

.recording-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--spacing-sm) var(--spacing-md);
  background: var(--color-surface-alt);
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all 0.15s ease;
}

.recording-item:hover {
  border-color: var(--color-border);
}

.recording-item.selected {
  border-color: var(--color-primary);
  background: rgba(var(--color-primary-rgb), 0.1);
}

.recording-item.failed {
  background: rgba(239, 68, 68, 0.05);
}

.recording-item.finalizing {
  cursor: default;
  opacity: 0.7;
}

.recording-item.finalized {
  cursor: default;
}

.recording-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.recording-version {
  font-family: monospace;
  font-size: 0.875rem;
  font-weight: 500;
  color: var(--color-text);
}

.recording-meta {
  font-size: 0.75rem;
  color: var(--color-text-muted);
}

.status-badge {
  font-size: 0.75rem;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  font-weight: 500;
}

.status-badge.raw {
  background: rgba(234, 179, 8, 0.1);
  color: #eab308;
}

.status-badge.failed {
  background: rgba(239, 68, 68, 0.1);
  color: #ef4444;
}

.status-badge.finalizing {
  background: rgba(59, 130, 246, 0.1);
  color: #3b82f6;
}

.download-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--spacing-xs);
  padding: var(--spacing-xs) var(--spacing-sm);
  background: var(--color-primary);
  color: white;
  border-radius: var(--radius-sm);
  font-size: 0.75rem;
  font-weight: 500;
  text-decoration: none;
  transition: all 0.15s ease;
}

.download-btn:hover {
  opacity: 0.9;
}

.finalize-selected-action {
  margin-top: var(--spacing-sm);
  text-align: right;
}

.empty-state-small {
  padding: var(--spacing-md);
  text-align: center;
  color: var(--color-text-muted);
  font-size: 0.875rem;
}
</style>
