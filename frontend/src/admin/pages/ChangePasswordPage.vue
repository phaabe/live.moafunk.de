<script setup lang="ts">
import { ref, computed } from 'vue';
import { useRouter } from 'vue-router';
import { authApi, usersApi } from '../api';
import { useAuthStore } from '../stores/auth';
import { BaseButton, FormInput } from '@shared/components';
import { useFlash } from '../composables/useFlash';

const router = useRouter();
const flash = useFlash();
const authStore = useAuthStore();

// Forced first-login flow: the user still has an admin-generated password and
// must replace it before reaching the rest of the app.
const forced = computed(() => authStore.user?.must_change_password === true);

const currentPassword = ref('');
const newPassword = ref('');
const confirmPassword = ref('');
const loading = ref(false);
const error = ref<string | null>(null);

function defaultRouteForRole(): string {
  return authStore.user?.role === 'host' ? '/stream' : '/dashboard';
}

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
    if (forced.value) {
      // No current password required — the session proves identity.
      await authApi.setInitialPassword(newPassword.value);
      authStore.markPasswordChanged();
    } else {
      await usersApi.changePassword(currentPassword.value, newPassword.value);
    }
    flash.success('Password changed successfully');
    router.push(defaultRouteForRole());
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
      <h1 class="page-title">{{ forced ? 'Set Your Password' : 'Change Password' }}</h1>
    </div>

    <div class="card" style="max-width: 500px;">
      <p v-if="forced" class="forced-notice">
        You're signed in with a temporary password. Choose your own password to continue.
      </p>

      <form class="password-form" @submit.prevent="changePassword">
        <div v-if="error" class="flash-message error">{{ error }}</div>

        <FormInput
          v-if="!forced"
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
          <BaseButton v-if="!forced" type="button" variant="ghost" @click="router.back()">
            Cancel
          </BaseButton>
          <BaseButton type="submit" variant="primary" :loading="loading">
            {{ forced ? 'Set Password' : 'Change Password' }}
          </BaseButton>
        </div>
      </form>
    </div>
  </div>
</template>

<style scoped>
.forced-notice {
  margin: 0 0 var(--spacing-md);
  color: var(--color-text-muted, inherit);
}

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
