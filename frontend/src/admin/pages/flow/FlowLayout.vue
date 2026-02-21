<script setup lang="ts">
import { computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { useHostFlow, type FlowStep } from '@admin/composables';

const router = useRouter();
const flow = useHostFlow();

// Steps shown in the progress indicator (not-assigned and live are special)
const progressSteps: { key: FlowStep; label: string }[] = [
  { key: 'info', label: 'Show Info' },
  { key: 'mode', label: 'Mode' },
  { key: 'upload', label: 'Upload' },
  { key: 'confirm', label: 'Confirm' },
];

const currentStepIndex = computed(() =>
  progressSteps.findIndex((s) => s.key === flow.currentStep.value)
);

const showProgressBar = computed(() =>
  flow.assigned.value &&
  flow.currentStep.value !== 'not-assigned' &&
  flow.currentStep.value !== 'live'
);

onMounted(async () => {
  if (!flow.loaded.value) {
    await flow.fetchMyShow();
  }
});

function navigateToStep(step: FlowStep) {
  if (flow.canNavigateTo(step)) {
    flow.goToStep(step);
    router.push(`/stream/${step}`);
  }
}
</script>

<template>
  <div class="flow-layout">
    <!-- Progress bar -->
    <div v-if="showProgressBar" class="flow-progress">
      <div class="flow-progress-inner">
        <div v-for="(step, index) in progressSteps" :key="step.key" :class="[
          'flow-step-dot',
          {
            active: step.key === flow.currentStep.value,
            completed: index < currentStepIndex,
            clickable: flow.canNavigateTo(step.key),
          },
        ]" @click="navigateToStep(step.key)">
          <div class="dot">
            <span v-if="index < currentStepIndex" class="dot-check">✓</span>
            <span v-else class="dot-number">{{ index + 1 }}</span>
          </div>
          <span class="dot-label">{{ step.label }}</span>
        </div>
        <!-- Connecting lines -->
        <div class="flow-progress-line">
          <div class="flow-progress-fill"
            :style="{ width: `${(Math.max(0, currentStepIndex) / (progressSteps.length - 1)) * 100}%` }" />
        </div>
      </div>
    </div>

    <!-- Loading state -->
    <div v-if="flow.loading.value && !flow.loaded.value" class="flow-loading">
      Loading...
    </div>

    <!-- Main content -->
    <main v-else class="flow-content">
      <router-view />
    </main>
  </div>
</template>

<style scoped>
.flow-layout {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

/* Progress bar */
.flow-progress {
  background: var(--color-surface);
  border-bottom: 1px solid var(--color-border);
  padding: var(--spacing-lg) var(--spacing-lg) var(--spacing-xl);
}

.flow-progress-inner {
  max-width: 500px;
  margin: 0 auto;
  display: flex;
  justify-content: space-between;
  position: relative;
}

.flow-progress-line {
  position: absolute;
  top: 16px;
  left: 16px;
  right: 16px;
  height: 2px;
  background: var(--color-border);
  z-index: 0;
}

.flow-progress-fill {
  height: 100%;
  background: var(--color-primary);
  transition: width var(--transition-normal);
}

.flow-step-dot {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--spacing-sm);
  z-index: 1;
  cursor: default;
}

.flow-step-dot.clickable {
  cursor: pointer;
}

.dot {
  width: 32px;
  height: 32px;
  border-radius: var(--radius-full);
  border: 2px solid var(--color-border);
  background: var(--color-surface);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-bold);
  color: var(--color-text-muted);
  transition: all var(--transition-fast);
}

.flow-step-dot.active .dot {
  border-color: var(--color-primary);
  background: var(--color-primary);
  color: var(--color-primary-text);
}

.flow-step-dot.completed .dot {
  border-color: var(--color-primary);
  background: var(--color-primary);
  color: var(--color-primary-text);
}

.dot-label {
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  white-space: nowrap;
  transition: color var(--transition-fast);
}

.flow-step-dot.active .dot-label {
  color: var(--color-primary);
  font-weight: var(--font-weight-bold);
}

.flow-step-dot.completed .dot-label {
  color: var(--color-text);
}

/* Loading */
.flow-loading {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-muted);
  font-size: var(--font-size-lg);
}

/* Content */
.flow-content {
  flex: 1;
  max-width: 800px;
  width: 100%;
  margin: 0 auto;
  padding: var(--spacing-xl) var(--spacing-lg);
}
</style>
