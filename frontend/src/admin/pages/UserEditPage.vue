<script setup lang="ts">
import { ref, onMounted, computed } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { usersApi, type AdminUser } from '../api';
import { BaseButton, FormInput } from '@shared/components';
import { useAuthStore } from '../stores/auth';
import { useFlash } from '../composables/useFlash';

const route = useRoute();
const router = useRouter();
const authStore = useAuthStore();
const flash = useFlash();

const user = ref<AdminUser | null>(null);
const loading = ref(true);
const saving = ref(false);
const error = ref<string | null>(null);

const editForm = ref({
  role: '',
  expires_at: '',
});

const newPassword = ref<string | null>(null);
const generatingPassword = ref(false);

// Role hierarchy helper
function canEditRole(targetRole: string): boolean {
  const currentUser = authStore.user;
  if (!currentUser) return false;
  
  const roleLevel = { artist: 1, admin: 2, superadmin: 3 };
  const currentLevel = roleLevel[currentUser.role as keyof typeof roleLevel] || 0;
  const targetLevel = roleLevel[targetRole as keyof typeof roleLevel] || 0;
  
  return currentLevel > targetLevel;
}

// Get available roles for selection based on current user's role
const availableRoles = computed(() => {
  const currentUser = authStore.user;
  if (!currentUser) return [];
  
  const roles = ['artist', 'admin', 'superadmin'];
  return roles.filter(role => {
    const roleLevel = { artist: 1, admin: 2, superadmin: 3 };
    const currentLevel = roleLevel[currentUser.role as keyof typeof roleLevel] || 0;
    const targetLevel = roleLevel[role as keyof typeof roleLevel] || 0;
    return currentLevel > targetLevel;
  });
});

function canDeleteUser(): boolean {
  const currentUser = authStore.user;
  if (!currentUser || !user.value) return false;
  
  // Can't delete yourself
  if (user.value.username === currentUser.username) return false;
  
  // Only superadmin can delete admins and superadmins
  if (user.value.role === 'superadmin' || user.value.role === 'admin') {
    return currentUser.role === 'superadmin';
  }
  
  // Admin and superadmin can delete artists
  return currentUser.role === 'admin' || currentUser.role === 'superadmin';
}

async function loadUser() {
  loading.value = true;
  error.value = null;

  try {
    const response = await usersApi.list();
    const userId = parseInt(route.params.id as string);
    user.value = response.users.find((u) => u.id === userId) || null;
    
    if (!user.value) {
      error.value = 'User not found';
      return;
    }
    
    // Check if current user can edit this user
    if (!canEditRole(user.value.role)) {
      error.value = 'You do not have permission to edit this user';
      return;
    }
    
    editForm.value.role = user.value.role;
    // Extract date portion from expires_at (handles both 'YYYY-MM-DD' and 'YYYY-MM-DDTHH:MM:SS' formats)
    if (user.value.expires_at) {
      editForm.value.expires_at = user.value.expires_at.includes('T') 
        ? user.value.expires_at.split('T')[0]
        : user.value.expires_at.split(' ')[0];
    } else {
      editForm.value.expires_at = '';
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load user';
  } finally {
    loading.value = false;
  }
}

async function saveChanges() {
  if (!user.value) return;
  
  saving.value = true;
  error.value = null;

  try {
    await usersApi.update(user.value.id, {
      role: editForm.value.role,
      expires_at: editForm.value.role === 'artist' && editForm.value.expires_at 
        ? editForm.value.expires_at 
        : undefined,
    });
    flash.success('User updated successfully');
    await loadUser();
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to update user';
  } finally {
    saving.value = false;
  }
}

async function generatePassword() {
  if (!user.value) return;
  
  generatingPassword.value = true;
  error.value = null;

  try {
    const response = await usersApi.resetPassword(user.value.id);
    newPassword.value = response.password;
    flash.success('New password generated');
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to generate password';
  } finally {
    generatingPassword.value = false;
  }
}

async function deleteUser() {
  if (!user.value) return;
  
  if (!confirm(`Are you sure you want to delete user "${user.value.username}"?`)) return;

  try {
    await usersApi.delete(user.value.id);
    flash.success(`User "${user.value.username}" deleted`);
    router.push('/users');
  } catch (e) {
    flash.error(e instanceof Error ? e.message : 'Failed to delete user');
  }
}

function copyPassword() {
  if (newPassword.value) {
    navigator.clipboard.writeText(newPassword.value);
    flash.success('Password copied to clipboard');
  }
}

onMounted(loadUser);
</script>

<template>
  <div class="user-edit-page">
    <div class="page-header">
      <h1 class="page-title">Edit User</h1>
      <BaseButton variant="ghost" @click="router.push('/users')">
        ← Back to Users
      </BaseButton>
    </div>

    <div v-if="error" class="flash-message error">{{ error }}</div>

    <div v-if="loading" class="loading-spinner"></div>

    <div v-else-if="user" class="card">
      <div class="user-info">
        <h2>{{ user.username }}</h2>
        <span :class="['badge', user.role === 'superadmin' ? 'warning' : 'success']">
          {{ user.role }}
        </span>
      </div>

      <form class="edit-form" @submit.prevent="saveChanges">
        <div class="form-group">
          <label class="label">Role</label>
          <select v-model="editForm.role" class="select-input" :disabled="availableRoles.length === 0">
            <option v-for="role in availableRoles" :key="role" :value="role">
              {{ role.charAt(0).toUpperCase() + role.slice(1) }}
            </option>
          </select>
          <p v-if="availableRoles.length === 0" class="help-text">
            You cannot modify this user's role
          </p>
        </div>

        <FormInput
          v-if="editForm.role === 'artist'"
          v-model="editForm.expires_at"
          label="Expires At"
          type="date"
          required
        />

        <div class="form-actions">
          <BaseButton type="submit" variant="primary" :loading="saving">
            Save Changes
          </BaseButton>
        </div>
      </form>

      <hr class="divider" />

      <div class="password-section">
        <h3>Password Reset</h3>
        <p class="help-text">Generate a new password for this user.</p>
        
        <div v-if="newPassword" class="password-result">
          <p>New password generated! Copy it below (it won't be shown again):</p>
          <code class="password-display">
            {{ newPassword }}
            <button class="copy-btn" @click="copyPassword" title="Copy to clipboard">
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
              </svg>
            </button>
          </code>
          <BaseButton variant="ghost" @click="newPassword = null">
            Close
          </BaseButton>
        </div>
        <BaseButton v-else variant="secondary" :loading="generatingPassword" @click="generatePassword">
          Generate New Password
        </BaseButton>
      </div>

      <hr class="divider" />

      <div class="danger-zone">
        <h3>Danger Zone</h3>
        <p class="help-text">Permanently delete this user. This action cannot be undone.</p>
        <BaseButton 
          v-if="canDeleteUser()" 
          variant="danger" 
          @click="deleteUser"
        >
          Delete User
        </BaseButton>
        <p v-else class="help-text">You do not have permission to delete this user.</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.user-info {
  display: flex;
  align-items: center;
  gap: var(--spacing-md);
  margin-bottom: var(--spacing-lg);
}

.user-info h2 {
  margin: 0;
  font-size: var(--font-size-xl);
}

.edit-form {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.label {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.select-input {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-family: var(--font-family);
  font-size: var(--font-size-md);
  padding: var(--spacing-sm) var(--spacing-md);
}

.select-input:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.help-text {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  margin: var(--spacing-xs) 0 0 0;
}

.form-actions {
  display: flex;
  gap: var(--spacing-sm);
  margin-top: var(--spacing-md);
}

.divider {
  border: none;
  border-top: 1px solid var(--color-border);
  margin: var(--spacing-lg) 0;
}

.password-section,
.danger-zone {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.password-section h3,
.danger-zone h3 {
  margin: 0;
  font-size: var(--font-size-lg);
}

.password-result {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  padding: var(--spacing-md);
  background-color: var(--color-surface-alt);
  border-radius: var(--radius-md);
}

.password-display {
  display: block;
  position: relative;
  background-color: var(--color-surface);
  padding: var(--spacing-md);
  padding-right: calc(var(--spacing-md) + 32px);
  border-radius: var(--radius-md);
  font-size: var(--font-size-lg);
  word-break: break-all;
  border: 1px solid var(--color-border);
}

.copy-btn {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  right: var(--spacing-sm);
  background: transparent;
  border: none;
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  color: #ffffff;
  transition: opacity var(--transition-fast);
}

.copy-btn:hover {
  opacity: 0.7;
}

.danger-zone {
  padding: var(--spacing-md);
  background-color: rgba(239, 68, 68, 0.1);
  border-radius: var(--radius-md);
}
</style>
