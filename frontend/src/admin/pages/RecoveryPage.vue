<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { BaseButton, FormInput } from '@shared/components';
import { useRecovery } from '../composables/useRecovery';
import { useAuthStore } from '../stores/auth';

const route = useRoute();
const authStore = useAuthStore();
const { loading, error, message, request, verify, complete } = useRecovery();
const email = ref('');
const password = ref('');
const confirmation = ref('');
const done = ref(false);
const mode = computed(() => route.name);
const token = computed(() => (typeof route.query.token === 'string' ? route.query.token : ''));
const title = computed(() =>
  mode.value === 'verify-email' ? 'Verify Recovery Email' : 'Reset Password'
);

watch([mode, token], () => {
  done.value = false;
  error.value = '';
  message.value = '';
  email.value = '';
  password.value = '';
  confirmation.value = '';
});

async function submit() {
  if (mode.value === 'forgot-password') {
    done.value = await request(email.value);
  } else if (mode.value === 'verify-email') {
    done.value = await verify(token.value);
  } else {
    if (password.value.length < 8 || password.value !== confirmation.value) {
      error.value = 'Use at least 8 characters and enter the same password twice.';
      return;
    }
    done.value = await complete(token.value, password.value);
    if (done.value) {
      // Reset revokes sessions. Drop the cached user so the login guard allows entry.
      authStore.user = null;
      password.value = '';
      confirmation.value = '';
    }
  }
}
</script>

<template>
  <div class="recovery-page card">
    <h1>{{ title }}</h1>
    <p v-if="message" role="status">{{ message }}</p>
    <p v-if="error" class="flash-message error" role="alert">{{ error }}</p>
    <template v-if="!done">
      <p v-if="mode !== 'forgot-password' && !token" role="alert">
        This link is missing its token.
      </p>
      <form v-else @submit.prevent="submit">
        <template v-if="mode === 'forgot-password'">
          <p>Enter your verified recovery email. The email includes your username.</p>
          <FormInput
            v-model="email"
            label="Email"
            type="email"
            autocomplete="email"
            required
            :disabled="loading"
          />
        </template>
        <p v-else-if="mode === 'verify-email'">
          Confirm that you want to use this email to recover your account.
        </p>
        <template v-else>
          <FormInput
            v-model="password"
            label="New Password"
            type="password"
            autocomplete="new-password"
            required
            :disabled="loading"
          />
          <FormInput
            v-model="confirmation"
            label="Confirm New Password"
            type="password"
            autocomplete="new-password"
            required
            :disabled="loading"
          />
        </template>
        <BaseButton type="submit" :loading="loading">
          {{
            mode === 'forgot-password'
              ? 'Send Reset Link'
              : mode === 'verify-email'
                ? 'Verify Email'
                : 'Set Password'
          }}
        </BaseButton>
      </form>
    </template>
    <p v-if="mode === 'forgot-password'">
      If no email arrives, check spam or try again later. You can also ask an admin for help.
    </p>
    <p v-if="mode === 'reset-password' && !done">
      <router-link to="/forgot-password">Request a new reset link</router-link>
    </p>
    <p v-if="mode === 'verify-email' && !done">
      If the link expired, sign in and request another from Recovery Email settings.
    </p>
    <router-link to="/login">Back to login</router-link>
  </div>
</template>

<style scoped>
.recovery-page {
  max-width: 500px;
  margin: var(--spacing-2xl) auto;
}
form {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}
</style>
