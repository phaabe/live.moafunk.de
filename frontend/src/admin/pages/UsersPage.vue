<script setup lang="ts">
import { ref, onMounted, computed } from 'vue';
import { usersApi, type AdminUser } from '../api';
import { BaseButton, BaseModal, FormInput } from '@shared/components';
import { useAuthStore } from '../stores/auth';
import { useFlash } from '../composables/useFlash';

const authStore = useAuthStore();
const flash = useFlash();

const users = ref<AdminUser[]>([]);
const loading = ref(true);
const error = ref<string | null>(null);

// Get available roles for creation based on current user's role
const availableRoles = computed(() => {
  const currentUser = authStore.user;
  if (!currentUser) return [];
  
  const roles: Array<{ value: string; label: string }> = [
    { value: 'artist', label: 'Artist' },
    { value: 'admin', label: 'Admin' },
    { value: 'superadmin', label: 'Superadmin' },
  ];
  
  const roleLevel: Record<string, number> = { artist: 1, admin: 2, superadmin: 3 };
  const currentLevel = roleLevel[currentUser.role] || 0;
  
  // Can only create users below current user's level
  return roles.filter(role => {
    const targetLevel = roleLevel[role.value] || 0;
    return targetLevel < currentLevel;
  });
});

const showCreateModal = ref(false);
const creating = ref(false);
const newUser = ref({
  username: '',
  role: 'artist',
  expires_at: '',
});
const createdPassword = ref<string | null>(null);
const usernameError = ref<string | null>(null);
const expiresAtError = ref<string | null>(null);

async function loadUsers() {
  loading.value = true;
  error.value = null;

  try {
    const response = await usersApi.list();
    users.value = response.users;
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load users';
  } finally {
    loading.value = false;
  }
}

async function createUser() {
  creating.value = true;
  error.value = null;
  usernameError.value = null;
  expiresAtError.value = null;

  // Trim username
  newUser.value.username = newUser.value.username.trim();

  // Validate username
  if (!newUser.value.username) {
    usernameError.value = 'Username is required';
    creating.value = false;
    return;
  }

  if (newUser.value.username.length < 3) {
    usernameError.value = 'Username must be at least 3 characters';
    creating.value = false;
    return;
  }

  if (newUser.value.username.length > 50) {
    usernameError.value = 'Username must be less than 50 characters';
    creating.value = false;
    return;
  }

  if (!/^[a-zA-Z0-9_-]+$/.test(newUser.value.username)) {
    usernameError.value = 'Username can only contain letters, numbers, hyphens, and underscores';
    creating.value = false;
    return;
  }

  // Check for duplicate username
  const isDuplicate = users.value.some(
    (user) => user.username.toLowerCase() === newUser.value.username.toLowerCase()
  );
  
  if (isDuplicate) {
    usernameError.value = 'Username already exists';
    creating.value = false;
    return;
  }

  // Validate artist expiration date
  if (newUser.value.role === 'artist') {
    if (!newUser.value.expires_at) {
      expiresAtError.value = 'Expiration date is required for artist users';
      creating.value = false;
      return;
    }

    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const expiryDate = new Date(newUser.value.expires_at);
    
    if (expiryDate < today) {
      expiresAtError.value = 'Expiration date cannot be in the past';
      creating.value = false;
      return;
    }
  }

  try {
    const response = await usersApi.create({
      username: newUser.value.username,
      role: newUser.value.role,
      expires_at: newUser.value.expires_at || undefined,
    });
    createdPassword.value = response.password;
    flash.success(`User "${response.user.username}" created successfully`);
    newUser.value = { username: '', role: 'artist', expires_at: '' };
    await loadUsers();
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to create user';
  } finally {
    creating.value = false;
  }
}

// Check if current user can edit target user based on role hierarchy
function canEditUser(targetUser: AdminUser): boolean {
  const currentUser = authStore.user;
  if (!currentUser) return false;
  
  const roleLevel: Record<string, number> = { artist: 1, admin: 2, superadmin: 3 };
  const currentLevel = roleLevel[currentUser.role] || 0;
  const targetLevel = roleLevel[targetUser.role] || 0;
  
  // Can only edit users below your role level
  return currentLevel > targetLevel;
}

function copyPassword() {
  if (createdPassword.value) {
    navigator.clipboard.writeText(createdPassword.value);
    flash.success('Password copied to clipboard');
  }
}

function closeCreateModal() {
  showCreateModal.value = false;
  createdPassword.value = null;
  usernameError.value = null;
  expiresAtError.value = null;
}

onMounted(loadUsers);
</script>

<template>
  <div class="users-page">
    <div class="page-header">
      <h1 class="page-title">Users</h1>
      <BaseButton variant="primary" @click="showCreateModal = true">
        + New User
      </BaseButton>
    </div>

    <div v-if="error" class="flash-message error">{{ error }}</div>

    <div v-if="loading" class="loading-spinner"></div>

    <div v-else class="card">
      <table class="data-table">
        <thead>
          <tr>
            <th>Username</th>
            <th>Role</th>
            <th>Expires</th>
            <th>Created</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="user in users" :key="user.id">
            <td>
              {{ user.username }}
              <span v-if="user.username === authStore.user?.username" class="badge success">you</span>
            </td>
            <td>
              <span :class="['badge', user.role === 'superadmin' ? 'warning' : user.role === 'artist' ? 'pink' : 'success']">
                {{ user.role }}
              </span>
            </td>
            <td class="text-muted">
              {{ user.expires_at ? new Date(user.expires_at).toLocaleDateString() : 'Never' }}
            </td>
            <td class="text-muted">
              {{ new Date(user.created_at).toLocaleDateString() }}
            </td>
            <td>
              <router-link v-if="canEditUser(user)" :to="`/users/${user.id}`" class="action-link">
                Edit
              </router-link>
              <span v-else class="text-muted">-</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <BaseModal :open="showCreateModal" title="Create New User" @close="closeCreateModal">
      <template v-if="createdPassword">
        <div class="password-result">
          <p>User created! Copy the password below (it won't be shown again):</p>
          <code class="password-display">
            {{ createdPassword }}
            <button class="copy-btn" @click="copyPassword" title="Copy to clipboard">
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
              </svg>
            </button>
          </code>
        </div>
      </template>
      <template v-else>
        <form class="create-form" @submit.prevent="createUser">
          <div class="form-group">
            <FormInput v-model="newUser.username" label="Username" required />
            <p v-if="usernameError" class="error-message">{{ usernameError }}</p>
          </div>
          <div class="form-group">
            <label class="label">Role</label>
            <select v-model="newUser.role" class="select-input">
              <option v-for="role in availableRoles" :key="role.value" :value="role.value">
                {{ role.label }}
              </option>
            </select>
          </div>
          <div v-if="newUser.role === 'artist'" class="form-group">
            <FormInput
              v-model="newUser.expires_at"
              label="Expires At"
              type="date"
              required
            />
            <p v-if="expiresAtError" class="error-message">{{ expiresAtError }}</p>
          </div>
        </form>
      </template>
      <template #footer>
        <BaseButton v-if="createdPassword" variant="primary" @click="closeCreateModal">
          Done
        </BaseButton>
        <template v-else>
          <BaseButton variant="ghost" @click="closeCreateModal">Cancel</BaseButton>
          <BaseButton variant="primary" :loading="creating" @click="createUser">
            Create User
          </BaseButton>
        </template>
      </template>
    </BaseModal>
  </div>
</template>

<style scoped>
.action-link {
  background: none;
  border: none;
  color: var(--color-link);
  cursor: pointer;
  font-family: var(--font-family);
  font-size: inherit;
  padding: 0;
}

.action-link.danger:hover {
  color: var(--color-error);
}

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

.error-message {
  color: var(--color-error);
  font-size: var(--font-size-sm);
  margin: var(--spacing-xs) 0 0 0;
}

.password-result {
  text-align: center;
}

.password-display {
  display: block;
  position: relative;
  background-color: var(--color-surface-alt);
  padding: var(--spacing-md);
  padding-right: calc(var(--spacing-md) + 32px);
  border-radius: var(--radius-md);
  margin-top: var(--spacing-md);
  font-size: var(--font-size-lg);
  word-break: break-all;
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
</style>
