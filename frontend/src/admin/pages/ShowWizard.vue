<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
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
import WizardHost from '../components/show-wizard/WizardHost.vue';
import WizardStreamMode from '../components/show-wizard/WizardStreamMode.vue';
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
  host: 'Host',
  'stream-mode': 'Mode',
  confirm: 'Confirm',
};

const STEP_COMPONENTS = {
  choice: WizardTemplateChoice,
  select: WizardTemplateSelect,
  name: WizardTemplateName,
  cover: WizardTemplateCover,
  description: WizardTemplateDescription,
  date: WizardDate,
  host: WizardHost,
  'stream-mode': WizardStreamMode,
  confirm: WizardConfirm,
} as const;

const currentComponent = computed(() => STEP_COMPONENTS[wizard.currentStep.value]);

// After creation we hold here to show guest credentials (which can't be
// retrieved again) before navigating away.
const createdShowId = ref<number | null>(null);

onMounted(() => {
  const prefillDate = typeof route.query.date === 'string' ? route.query.date : undefined;
  wizard.start({ isAdmin: isAdmin.value, prefillDate });
});

function onCancel() {
  router.push(isAdmin.value ? '/shows' : '/stream');
}

function leave() {
  if (isAdmin.value) {
    router.push(createdShowId.value ? `/shows/${createdShowId.value}` : '/shows');
  } else {
    router.push('/stream');
  }
}

async function onCreate() {
  try {
    const show = await wizard.submit();
    flash.success('Show created successfully');
    createdShowId.value = show?.id ?? null;
    // If a guest was created, stay and show the one-time credentials.
    if (!wizard.guestCredentials.value) {
      leave();
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

    <!-- Guest credentials (shown once, after creation) -->
    <div v-if="wizard.guestCredentials.value" class="wizard-content card">
      <h2 class="creds-title">Guest login created</h2>
      <p class="creds-hint">
        Share these with your guest now — the password is shown only once. They can sign in on
        {{ wizard.guestCredentials.value.login_date }} and will be asked to choose their own
        password.
      </p>
      <dl class="creds">
        <dt>Username</dt>
        <dd>{{ wizard.guestCredentials.value.username }}</dd>
        <dt>Password</dt>
        <dd>
          <code>{{ wizard.guestCredentials.value.password }}</code>
        </dd>
      </dl>
      <div class="wizard-nav">
        <span />
        <BaseButton variant="primary" @click="leave">Done</BaseButton>
      </div>
    </div>

    <template v-else>
      <!-- Step content -->
      <div class="wizard-content card">
        <component :is="currentComponent" />
      </div>

      <!-- Navigation -->
      <div class="wizard-nav">
        <BaseButton
          variant="ghost"
          @click="wizard.isFirstStep.value ? onCancel() : wizard.goBack()"
        >
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
    </template>
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

.creds-title {
  font-size: var(--font-size-lg);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin: 0 0 var(--spacing-sm);
  text-align: center;
}

.creds-hint {
  color: var(--color-text-muted);
  margin: 0 0 var(--spacing-lg);
  text-align: center;
}

.creds {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: var(--spacing-sm) var(--spacing-md);
  max-width: 360px;
  margin: 0 auto;
}

.creds dt {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  font-weight: var(--font-weight-medium);
}

.creds dd {
  margin: 0;
  color: var(--color-text);
}

.creds code {
  font-family: monospace;
  background: var(--color-surface-alt);
  padding: 2px var(--spacing-xs);
  border-radius: var(--radius-sm);
}
</style>
