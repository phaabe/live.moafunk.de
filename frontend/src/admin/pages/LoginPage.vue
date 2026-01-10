<script setup lang="ts">
import { ref } from 'vue';
import { useRouter, useRoute } from 'vue-router';
import { useAuthStore } from '../stores/auth';
import { BaseButton, FormInput } from '@shared/components';

const router = useRouter();
const route = useRoute();
const authStore = useAuthStore();

const username = ref('');
const password = ref('');

async function handleSubmit() {
  const success = await authStore.login(username.value, password.value);

  if (success) {
    const redirect = (route.query.redirect as string) || '/artists';
    router.push(redirect);
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-header">
        <img src="/assets/brand/moafunk.png" alt="Moafunk" class="logo" />
        <h1 class="title">Admin Login</h1>
      </div>

      <form class="login-form" @submit.prevent="handleSubmit">
        <div v-if="authStore.error" class="flash-message error">
          {{ authStore.error }}
        </div>

        <FormInput
          v-model="username"
          label="Username"
          type="text"
          autocomplete="username"
          required
          :disabled="authStore.loading"
        />

        <FormInput
          v-model="password"
          label="Password"
          type="password"
          autocomplete="current-password"
          required
          :disabled="authStore.loading"
        />

        <BaseButton
          type="submit"
          variant="primary"
          size="lg"
          :loading="authStore.loading"
          :disabled="!username || !password"
        >
          Login
        </BaseButton>
      </form>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--spacing-lg);
}

.login-card {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-2xl);
  width: 100%;
  max-width: 400px;
}

.login-header {
  text-align: center;
  margin-bottom: var(--spacing-xl);
}

.logo {
  height: 60px;
  margin-bottom: var(--spacing-md);
}

.title {
  font-size: var(--font-size-2xl);
  font-weight: var(--font-weight-bold);
  color: var(--color-primary);
  margin: 0;
}

.login-form {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.login-form :deep(.base-button) {
  width: 100%;
  margin-top: var(--spacing-sm);
}
</style>
