<script setup lang="ts">
import { ref, computed } from 'vue';
import { useAuthStore } from '../stores/auth';
import { showsApi, type Show } from '../api';
import { BaseButton, BaseModal, FormInput } from '@shared/components';
import { useFlash } from '../composables/useFlash';
import { useDateTimeRange } from '../composables/useDateTimeRange';
import { buildShowCreatePayload, validateShowCreate } from '../showCreatePayload';
import { VueDatePicker } from '@vuepic/vue-datepicker';
import '@vuepic/vue-datepicker/dist/main.css';

defineProps<{ open: boolean }>();
const emit = defineEmits<{ close: []; created: [show: Show] }>();

const auth = useAuthStore();
const flash = useFlash();

/** Admins get the full form; hosts get the constrained one (issue #146). */
const isAdmin = computed(
  () => auth.user?.role === 'admin' || auth.user?.role === 'superadmin'
);

const creating = ref(false);
const title = ref('');
const description = ref('');
const showType = ref('unheard');

const {
  startDateTime,
  endDateTime,
  isValid: rangeValid,
  validationError: rangeError,
  apiDate,
  apiStartTime,
  apiEndTime,
  reset: resetRange,
} = useDateTimeRange();

function resetForm() {
  title.value = '';
  description.value = '';
  showType.value = 'unheard';
  resetRange();
}

function close() {
  emit('close');
}

async function submit() {
  const validationError = validateShowCreate(isAdmin.value, {
    title: title.value,
    startTime: apiStartTime.value,
    endTime: apiEndTime.value,
    startBeforeEnd: rangeValid.value,
  });
  if (validationError) {
    flash.error(validationError);
    return;
  }

  creating.value = true;
  try {
    const created = await showsApi.create(
      buildShowCreatePayload(isAdmin.value, {
        title: title.value,
        description: description.value,
        showType: showType.value,
        date: apiDate.value,
        startTime: apiStartTime.value,
        endTime: apiEndTime.value,
      })
    );
    flash.success('Show created successfully');
    resetForm();
    emit('created', created);
  } catch (e) {
    flash.error(e instanceof Error ? e.message : 'Failed to create show');
  } finally {
    creating.value = false;
  }
}
</script>

<template>
  <BaseModal :open="open" title="Create New Show" @close="close">
    <form class="create-form" @submit.prevent="submit">
      <div v-if="isAdmin" class="form-group">
        <label class="form-label">Show Type</label>
        <select v-model="showType" class="type-select">
          <option value="unheard">UNHEARD</option>
          <option value="brunchtime">Brunchtime</option>
          <option value="external">External</option>
        </select>
      </div>

      <FormInput v-model="title" label="Title" required />

      <div class="form-group">
        <label class="form-label">Start <span class="form-required">*</span></label>
        <VueDatePicker
          v-model="startDateTime"
          :enable-time-picker="true"
          :dark="true"
          :minutes-increment="5"
          :max-date="isAdmin ? endDateTime || undefined : undefined"
          :flow="{ steps: ['calendar', 'time'] }"
          :action-row="{ showCancel: false, showPreview: false, selectBtnLabel: 'Confirm' }"
          placeholder="Start date & time"
          text-input
          teleport="body"
        />
      </div>

      <template v-if="isAdmin">
        <div class="form-group">
          <label class="form-label">End <span class="form-required">*</span></label>
          <VueDatePicker
            v-model="endDateTime"
            :enable-time-picker="true"
            :dark="true"
            :minutes-increment="5"
            :min-date="startDateTime || undefined"
            :flow="{ steps: ['calendar', 'time'] }"
            :action-row="{ showCancel: false, showPreview: false, selectBtnLabel: 'Confirm' }"
            placeholder="End date & time"
            text-input
            teleport="body"
          />
        </div>
        <p v-if="startDateTime && endDateTime && !rangeValid" class="field-error">
          {{ rangeError }}
        </p>
        <FormInput v-model="description" label="Description" />
      </template>
    </form>
    <template #footer>
      <BaseButton variant="ghost" @click="close">Cancel</BaseButton>
      <BaseButton variant="primary" :loading="creating" @click="submit">Create Show</BaseButton>
    </template>
  </BaseModal>
</template>

<style scoped>
.create-form {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.form-label {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  font-weight: var(--font-weight-medium);
}

.form-required {
  color: var(--color-error);
}

.field-error {
  color: var(--color-error);
  font-size: var(--font-size-sm);
  margin: 0;
}

.type-select {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-family: var(--font-family);
  padding: var(--spacing-sm) var(--spacing-md);
}
</style>
