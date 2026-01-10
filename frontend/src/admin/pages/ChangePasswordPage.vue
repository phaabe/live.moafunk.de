<script setup lang="ts">
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { usersApi } from '../api';
import { BaseButton, FormInput } from '@shared/components';

const router = useRouter();

const currentPassword = ref('');
const newPassword = ref('');
const confirmPassword = ref('');
const loading = ref(false);
const error = ref<string | null>(null);

async function changePassword() {
  if (newPassword.value !== confirmPassword.value) {
    error.value = 'New passwords do not match';
    return;
  }

  if (newPassword.value.length < 8) {
    error.value = 'New password must be at least 8 characters';
    return;
  }

  loading.value = true;
  error.value = null;

  try {
    await usersApi.changePassword(currentPassword.value, newPassword.value);
    router.push('/artists');
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to change password';
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="change-password-page">
    <div class="page-header">
      <h1 class="page-title">Change Password</h1>
    </div>

    <div class="card" style="max-width: 500px;">
      <form class="password-form" @submit.prevent="changePassword">
        <div v-if="error" class="flash-message error">{{ error }}</div>

        <FormInput
          v-model="currentPassword"
          label="Current Password"
          type="password"
          autocomplete="current-password"
          required
        />

        <FormInput
          v-model="newPassword"
          label="New Password"
          type="password"
          autocomplete="new-password"
          required
        />

        <FormInput
          v-model="confirmPassword"
          label="Confirm New Password"
          type="password"
          autocomplete="new-password"
          required
        />

        <div class="form-actions">
          <BaseButton type="button" variant="ghost" @click="router.back()">
            Cancel
          </BaseButton>
          <BaseButton type="submit" variant="primary" :loading="loading">
            Change Password
          </BaseButton>
        </div>
      </form>
    </div>
  </div>
</template>

<style scoped>
.password-form {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.form-actions {
  display: flex;
  gap: var(--spacing-md);
  justify-content: flex-end;
  margin-top: var(--spacing-md);
}
</style>
