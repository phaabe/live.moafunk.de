<script setup lang="ts">
import { computed, onMounted } from 'vue';
import { useRouter, useRoute } from 'vue-router';
import { useAuthStore } from '../stores/auth';
import { useShowWizard, type WizardStep } from '../composables';
import { useFlash } from '../composables/useFlash';
import { BaseButton } from '@shared/components';
import WizardTemplateChoice from '../components/show-wizard/WizardTemplateChoice.vue';
import WizardTemplateSelect from '../components/show-wizard/WizardTemplateSelect.vue';
import WizardTemplateName from '../components/show-wizard/WizardTemplateName.vue';
import WizardTemplateCover from '../components/show-wizard/WizardTemplateCover.vue';
import WizardTemplateDescription from '../components/show-wizard/WizardTemplateDescription.vue';
import WizardDate from '../components/show-wizard/WizardDate.vue';
import WizardAssign from '../components/show-wizard/WizardAssign.vue';
import WizardConfirm from '../components/show-wizard/WizardConfirm.vue';

const router = useRouter();
const route = useRoute();
const auth = useAuthStore();
const flash = useFlash();
const wizard = useShowWizard();

const isAdmin = computed(() => auth.user?.role === 'admin' || auth.user?.role === 'superadmin');

const STEP_LABELS: Record<WizardStep, string> = {
  choice: 'Template',
  select: 'Select',
  name: 'Name',
  cover: 'Cover',
  description: 'About',
  date: 'Date',
  assign: 'Host',
  confirm: 'Confirm',
};

const STEP_COMPONENTS = {
  choice: WizardTemplateChoice,
  select: WizardTemplateSelect,
  name: WizardTemplateName,
  cover: WizardTemplateCover,
  description: WizardTemplateDescription,
  date: WizardDate,
  assign: WizardAssign,
  confirm: WizardConfirm,
} as const;

const currentComponent = computed(() => STEP_COMPONENTS[wizard.currentStep.value]);

onMounted(() => {
  const prefillDate = typeof route.query.date === 'string' ? route.query.date : undefined;
  wizard.start({ isAdmin: isAdmin.value, prefillDate });
});

function onCancel() {
  router.push(isAdmin.value ? '/shows' : '/stream');
}

async function onCreate() {
  try {
    const show = await wizard.submit();
    flash.success('Show created successfully');
    if (isAdmin.value) {
      router.push(show?.id ? `/shows/${show.id}` : '/shows');
    } else {
      router.push('/stream');
    }
  } catch (e) {
    flash.error(e instanceof Error ? e.message : 'Failed to create show');
  }
}
</script>

<template>
  <div class="wizard-page">
    <div class="wizard-header">
      <h1 class="wizard-title">Create New Show</h1>
    </div>

    <!-- Progress stepper -->
    <div class="wizard-progress">
      <div class="wizard-progress-inner">
        <div
          v-for="(step, index) in wizard.steps.value"
          :key="step"
          :class="[
            'wizard-step-dot',
            {
              active: index === wizard.stepIndex.value,
              completed: index < wizard.stepIndex.value,
              clickable: wizard.canNavigateTo(index),
            },
          ]"
          @click="wizard.goToStep(index)"
        >
          <div class="dot">
            <span v-if="index < wizard.stepIndex.value" class="dot-check">✓</span>
            <span v-else class="dot-number">{{ index + 1 }}</span>
          </div>
          <span class="dot-label">{{ STEP_LABELS[step] }}</span>
        </div>
        <div class="wizard-progress-line">
          <div
            class="wizard-progress-fill"
            :style="{
              width: `${(Math.max(0, wizard.stepIndex.value) / Math.max(1, wizard.steps.value.length - 1)) * 100}%`,
            }"
          />
        </div>
      </div>
    </div>

    <!-- Step content -->
    <div class="wizard-content card">
      <component :is="currentComponent" />
    </div>

    <!-- Navigation -->
    <div class="wizard-nav">
      <BaseButton variant="ghost" @click="wizard.isFirstStep.value ? onCancel() : wizard.goBack()">
        {{ wizard.isFirstStep.value ? 'Cancel' : 'Back' }}
      </BaseButton>
      <BaseButton
        v-if="!wizard.isLastStep.value"
        variant="primary"
        :disabled="!wizard.canProceed.value"
        @click="wizard.goNext()"
      >
        Next
      </BaseButton>
      <BaseButton
        v-else
        variant="primary"
        :loading="wizard.submitting.value"
        :disabled="!wizard.canProceed.value"
        @click="onCreate"
      >
        Create Show
      </BaseButton>
    </div>
  </div>
</template>

<style scoped>
.wizard-page {
  max-width: 720px;
  margin: 0 auto;
  padding: var(--spacing-lg);
}

.wizard-header {
  text-align: center;
  margin-bottom: var(--spacing-lg);
}

.wizard-title {
  font-size: var(--font-size-xl);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin: 0;
}

/* Progress stepper (adapted from the host stream flow) */
.wizard-progress {
  margin-bottom: var(--spacing-xl);
  padding: var(--spacing-lg) var(--spacing-sm);
}

.wizard-progress-inner {
  max-width: 560px;
  margin: 0 auto;
  display: flex;
  justify-content: space-between;
  position: relative;
}

.wizard-progress-line {
  position: absolute;
  top: 16px;
  left: 16px;
  right: 16px;
  height: 2px;
  background: var(--color-border);
  z-index: 0;
}

.wizard-progress-fill {
  height: 100%;
  background: var(--color-primary);
  transition: width var(--transition-normal);
}

.wizard-step-dot {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--spacing-sm);
  z-index: 1;
  cursor: default;
}

.wizard-step-dot.clickable {
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

.wizard-step-dot.active .dot,
.wizard-step-dot.completed .dot {
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

.wizard-step-dot.active .dot-label {
  color: var(--color-primary);
  font-weight: var(--font-weight-bold);
}

.wizard-step-dot.completed .dot-label {
  color: var(--color-text);
}

.wizard-content {
  min-height: 280px;
  padding: var(--spacing-xl);
}

.wizard-nav {
  display: flex;
  justify-content: space-between;
  margin-top: var(--spacing-lg);
}
</style>
