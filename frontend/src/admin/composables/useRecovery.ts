import { ref } from 'vue';
import { recoveryApi, type RecoveryStatus } from '../api/recovery';

export function useRecovery() {
  const loading = ref(false);
  const error = ref('');
  const message = ref('');
  const status = ref<RecoveryStatus | null>(null);

  async function run(action: () => Promise<void>): Promise<boolean> {
    if (loading.value) return false;
    loading.value = true;
    error.value = '';
    message.value = '';
    try {
      await action();
      return true;
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : 'Please try again.';
      return false;
    } finally {
      loading.value = false;
    }
  }

  const load = () =>
    run(async () => {
      status.value = await recoveryApi.status();
    });
  const enroll = (email: string, password: string) =>
    run(async () => {
      await recoveryApi.enroll(email, password);
      message.value =
        'Check your inbox and verify the address. Your current recovery email stays active until then. If no email arrives, try again later.';
    });
  const remove = (password: string) =>
    run(async () => {
      await recoveryApi.remove(password);
      if (status.value) status.value.email = null;
      message.value = 'Recovery email and pending links removed.';
    });
  const request = (email: string) =>
    run(async () => {
      const response = await recoveryApi.request(email);
      message.value = response.message;
    });
  const verify = (token: string) =>
    run(async () => {
      await recoveryApi.verify(token);
      message.value = 'Recovery email verified.';
    });
  const complete = (token: string, password: string) =>
    run(async () => {
      await recoveryApi.complete(token, password);
      message.value = 'Password changed. Sign in with your username and new password.';
    });

  return { loading, error, message, status, load, enroll, remove, request, verify, complete };
}
