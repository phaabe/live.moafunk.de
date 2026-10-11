<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { BaseButton, FormInput } from '@shared/components';
import { useRecovery } from '../composables/useRecovery';

const { loading, error, message, status, load, enroll, remove } = useRecovery();
const email = ref('');
const password = ref('');
onMounted(load);

async function save() {
  if (await enroll(email.value, password.value)) password.value = '';
}
async function clear() {
  if (!window.confirm('Remove your recovery email and cancel pending recovery links?')) return;
  if (await remove(password.value)) password.value = '';
}
</script>

<template>
  <div class="recovery-settings">
    <h1 class="page-title">Recovery Email</h1>
    <div class="card">
      <p v-if="error" class="flash-message error" role="alert">{{ error }}</p>
      <p v-if="message" role="status">{{ message }}</p>
      <p v-if="!status && loading">Loading…</p>
      <template v-if="status">
        <p>
          Verified email: <strong>{{ status.email || 'None' }}</strong>
        </p>
        <p v-if="!status.eligible">Email recovery is not available for this account.</p>
        <template v-else>
          <p v-if="!status.enabled">
            Email recovery is not available yet. Ask an admin if you need a password reset.
          </p>
          <p v-else>
            Add an email and verify it to reset a forgotten password. A replacement takes effect
            only after verification.
          </p>
          <form @submit.prevent="save">
            <FormInput
              v-if="status.enabled"
              v-model="email"
              label="Recovery Email"
              type="email"
              autocomplete="email"
              required
              :disabled="loading"
            />
            <FormInput
              v-model="password"
              label="Current Password"
              type="password"
              autocomplete="current-password"
              required
              :disabled="loading"
            />
            <BaseButton v-if="status.enabled" type="submit" :loading="loading"
              >Send Verification Email</BaseButton
            >
            <BaseButton variant="danger" :loading="loading" :disabled="!password" @click="clear"
              >Remove Email and Pending Links</BaseButton
            >
          </form>
        </template>
      </template>
    </div>
  </div>
</template>

<style scoped>
.card {
  max-width: 500px;
}
form {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}
</style>
