import { api } from './index';

export interface RecoveryStatus {
  enabled: boolean;
  eligible: boolean;
  email: string | null;
}

export const recoveryApi = {
  status: () => api.get<RecoveryStatus>('/api/auth/recovery'),
  enroll: (email: string, currentPassword: string) =>
    api.post('/api/auth/recovery-email', { email, current_password: currentPassword }),
  remove: (currentPassword: string) =>
    api.post('/api/auth/recovery-email/remove', { current_password: currentPassword }),
  verify: (token: string) => api.post('/api/auth/recovery-email/verify', { token }),
  request: (email: string) =>
    api.post<{ success: boolean; message: string }>('/api/auth/password-reset/request', { email }),
  complete: (token: string, newPassword: string) =>
    api.post('/api/auth/password-reset/complete', { token, new_password: newPassword }),
  clear: (userId: number) => api.post(`/api/users/${userId}/recovery-email/clear`),
};
