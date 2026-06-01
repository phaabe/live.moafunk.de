<script setup lang="ts">
import { computed } from 'vue';
import { useShowWizard, type WizardStep } from '../../composables';

const wizard = useShowWizard();

const dateLabel = computed(() => {
  const d = wizard.startDateTime.value;
  const e = wizard.endDateTime.value;
  if (!d) return '—';
  const day = d.toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
  const pad = (n: number) => String(n).padStart(2, '0');
  const start = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const end = e ? `${pad(e.getHours())}:${pad(e.getMinutes())}` : '';
  return end ? `${day}, ${start}–${end}` : `${day}, ${start}`;
});

// For the existing-template branch, title/description/cover all come from the
// selected template, so their "Edit" jumps back to the template picker.
const contentStep = computed<WizardStep>(() =>
  wizard.mode.value === 'existing' ? 'select' : 'name'
);
const descriptionStep = computed<WizardStep>(() =>
  wizard.mode.value === 'existing' ? 'select' : 'description'
);
const coverStep = computed<WizardStep>(() =>
  wizard.mode.value === 'existing' ? 'select' : 'cover'
);

function edit(step: WizardStep) {
  wizard.goToNamedStep(step);
}
</script>

<template>
  <div class="step">
    <h2 class="step-title">Review &amp; confirm</h2>

    <div class="summary">
      <div class="summary-cover">
        <img v-if="wizard.summaryCoverUrl.value" :src="wizard.summaryCoverUrl.value" alt="Cover" />
        <div v-else class="summary-cover-placeholder">No cover</div>
        <button type="button" class="edit-link cover-edit" @click="edit(coverStep)">Edit</button>
      </div>

      <dl class="summary-details">
        <dt>Title</dt>
        <dd>
          <span>{{ wizard.summaryName.value || '—' }}</span>
          <button type="button" class="edit-link" @click="edit(contentStep)">Edit</button>
        </dd>

        <dt>Description</dt>
        <dd>
          <span>{{ wizard.summaryDescription.value || '—' }}</span>
          <button type="button" class="edit-link" @click="edit(descriptionStep)">Edit</button>
        </dd>

        <dt>When</dt>
        <dd>
          <span>{{ dateLabel }}</span>
          <button type="button" class="edit-link" @click="edit('date')">Edit</button>
        </dd>

        <template v-if="wizard.isAdmin.value">
          <dt>Host</dt>
          <dd>
            <span>{{ wizard.assigneeUsername.value || '—' }}</span>
            <button type="button" class="edit-link" @click="edit('assign')">Edit</button>
          </dd>
        </template>

        <dt>Delivery</dt>
        <dd>
          <span>{{ wizard.streamModeLabel.value || '—' }}</span>
          <button type="button" class="edit-link" @click="edit('stream-mode')">Edit</button>
        </dd>

        <dt>Guest</dt>
        <dd>
          <span>{{ wizard.summaryGuest.value || 'None' }}</span>
          <button type="button" class="edit-link" @click="edit('guest')">Edit</button>
        </dd>
      </dl>
    </div>
  </div>
</template>

<style scoped>
.step-title {
  font-size: var(--font-size-lg);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin: 0 0 var(--spacing-lg);
  text-align: center;
}

.summary {
  display: flex;
  gap: var(--spacing-lg);
  align-items: flex-start;
}

.summary-cover {
  position: relative;
}

.summary-cover,
.summary-cover-placeholder {
  width: 160px;
  height: 160px;
  flex-shrink: 0;
  border-radius: var(--radius-md);
  overflow: hidden;
  background: var(--color-surface-alt);
}

.cover-edit {
  position: absolute;
  bottom: var(--spacing-xs);
  right: var(--spacing-xs);
}

.edit-link {
  background: none;
  border: none;
  padding: 0;
  margin-left: var(--spacing-sm);
  color: var(--color-primary);
  font-family: var(--font-family);
  font-size: var(--font-size-xs);
  font-weight: var(--font-weight-medium);
  cursor: pointer;
}

.edit-link:hover {
  text-decoration: underline;
}

.summary-cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.summary-cover-placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.summary-details {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: var(--spacing-xs) var(--spacing-md);
  margin: 0;
  flex: 1;
}

.summary-details dt {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  font-weight: var(--font-weight-medium);
}

.summary-details dd {
  margin: 0;
  color: var(--color-text);
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--spacing-sm);
}

@media (max-width: 600px) {
  .summary {
    flex-direction: column;
    align-items: center;
    text-align: center;
  }
}
</style>
