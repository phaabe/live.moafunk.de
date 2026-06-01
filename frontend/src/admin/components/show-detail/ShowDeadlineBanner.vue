<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
import type { ShowDetail } from '../../api';
import { BaseButton } from '@shared/components';

const props = defineProps<{
  show: ShowDetail;
  /** Air datetime (UTC) used for the countdown, or null if unknown. */
  airTarget: Date | null;
  /** Whether the current viewer (assigned host) may upload. */
  canUpload: boolean;
}>();

const emit = defineEmits<{ upload: [] }>();

const hasFile = computed(() => !!props.show.prerecorded_key);

const now = ref(Date.now());
let timer: ReturnType<typeof setInterval> | null = null;

onMounted(() => {
  timer = setInterval(() => {
    now.value = Date.now();
  }, 1000);
});

onUnmounted(() => {
  if (timer) clearInterval(timer);
});

const remainingMs = computed(() =>
  props.airTarget ? props.airTarget.getTime() - now.value : null
);

const overdue = computed(() => remainingMs.value !== null && remainingMs.value <= 0);

/** Format remaining time as "04d 12h 33m 38s". */
const countdown = computed(() => {
  if (remainingMs.value === null) return '—';
  const total = Math.max(0, Math.floor(remainingMs.value / 1000));
  const d = Math.floor(total / 86400);
  const h = Math.floor((total % 86400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d)}d ${pad(h)}h ${pad(m)}m ${pad(s)}s`;
});
</script>

<template>
  <!-- Uploaded but not yet confirmed -->
  <div v-if="hasFile" class="deadline-banner warn">
    <div class="banner-icon">⏳</div>
    <div class="banner-body">
      <p class="banner-label">FILE UPLOADED — NOT CONFIRMED</p>
      <p class="banner-title">Confirm before air time</p>
      <p class="banner-sub">
        {{ show.prerecorded_filename || 'A file' }} is uploaded. Mark it as uploaded to lock it in.
      </p>
    </div>
  </div>

  <!-- No file submitted yet -->
  <div v-else class="deadline-banner danger">
    <div class="banner-icon">🚫</div>
    <div class="banner-body">
      <p class="banner-label">SHOW NOT UPLOADED — DEADLINE {{ overdue ? 'PASSED' : 'IN' }}</p>
      <p class="banner-title">{{ overdue ? 'Air time reached' : countdown }}</p>
      <p class="banner-sub">No file submitted yet. Upload before air time.</p>
    </div>
    <BaseButton v-if="canUpload" variant="primary" @click="emit('upload')">
      ⬆ Upload show
    </BaseButton>
  </div>
</template>

<style scoped>
.deadline-banner {
  display: flex;
  align-items: center;
  gap: var(--spacing-lg);
  padding: var(--spacing-lg) var(--spacing-xl);
  border-radius: var(--radius-xl);
  margin-bottom: var(--spacing-xl);
  border: 1px solid transparent;
}

.deadline-banner.danger {
  background: var(--color-error-bg);
  border-color: var(--color-error);
}

.deadline-banner.warn {
  background: var(--color-warning-bg);
  border-color: var(--color-warning);
}

.banner-icon {
  flex: 0 0 auto;
  width: 48px;
  height: 48px;
  display: grid;
  place-items: center;
  border-radius: var(--radius-full);
  font-size: 1.4rem;
  background: rgba(255, 255, 255, 0.06);
}

.banner-body {
  flex: 1 1 auto;
  min-width: 0;
}

.banner-label {
  margin: 0;
  font-size: var(--font-size-xs);
  font-weight: 700;
  letter-spacing: 0.05em;
  color: var(--color-text-muted);
}

.banner-title {
  margin: 2px 0;
  font-size: var(--font-size-xl);
  font-weight: 700;
  color: var(--color-text);
  font-variant-numeric: tabular-nums;
}

.banner-sub {
  margin: 0;
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}
</style>
