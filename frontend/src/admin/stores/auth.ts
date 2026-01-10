import { defineStore } from 'pinia';
import { ref, computed } from 'vue';
import { useRouter } from 'vue-router';
import { authApi, type User } from '../api';

export const useAuthStore = defineStore('auth', () => {
  const router = useRouter();

  const user = ref<User | null>(null);
  const initialized = ref(false);
  const loading = ref(false);
  const error = ref<string | null>(null);

  const isAuthenticated = computed(() => user.value !== null);

  async function login(username: string, password: string): Promise<boolean> {
    loading.value = true;
    error.value = null;

    try {
      const response = await authApi.login(username, password);
      user.value = response.user;
      return true;
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Login failed';
      return false;
    } finally {
      loading.value = false;
    }
  }

  async function logout(): Promise<void> {
    try {
      await authApi.logout();
    } catch {
      // Ignore logout errors
    } finally {
      user.value = null;
      router.push({ name: 'login' });
    }
  }

  async function checkAuth(): Promise<void> {
    if (initialized.value) return;

    try {
      user.value = await authApi.me();
    } catch {
      user.value = null;
    } finally {
      initialized.value = true;
    }
  }

  function clearError(): void {
    error.value = null;
  }

  return {
    user,
    initialized,
    loading,
    error,
    isAuthenticated,
    login,
    logout,
    checkAuth,
    clearError,
  };
});
