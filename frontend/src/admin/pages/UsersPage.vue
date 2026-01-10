<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { usersApi, type AdminUser } from '../api';
import { BaseButton, BaseModal, FormInput } from '@shared/components';
import { useAuthStore } from '../stores/auth';
import { useFlash } from '../composables/useFlash';

const authStore = useAuthStore();
const flash = useFlash();

const users = ref<AdminUser[]>([]);
const loading = ref(true);
const error = ref<string | null>(null);

const showCreateModal = ref(false);
const creating = ref(false);
const newUser = ref({
  username: '',
  role: 'artist',
  expires_at: '',
});
const createdPassword = ref<string | null>(null);

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

async function deleteUser(id: number, username: string) {
  if (!confirm(`Are you sure you want to delete user "${username}"?`)) return;

  try {
    await usersApi.delete(id);
    flash.success(`User "${username}" deleted`);
    await loadUsers();
  } catch (e) {
    flash.error(e instanceof Error ? e.message : 'Failed to delete user');
  }
}

// Check if current user can delete target user
function canDeleteUser(targetUser: AdminUser): boolean {
  const currentUser = authStore.user;
  if (!currentUser) return false;
  
  // Can't delete yourself
  if (targetUser.username === currentUser.username) return false;
  
  // Only superadmin can delete admins and superadmins
  if (targetUser.role === 'superadmin' || targetUser.role === 'admin') {
    return currentUser.role === 'superadmin';
  }
  
  // Admin and superadmin can delete artists
  return currentUser.role === 'admin' || currentUser.role === 'superadmin';
}

function closeCreateModal() {
  showCreateModal.value = false;
  createdPassword.value = null;
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
              <span :class="['badge', user.role === 'superadmin' ? 'warning' : 'success']">
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
              <button
                v-if="canDeleteUser(user)"
                class="action-link danger"
                @click="deleteUser(user.id, user.username)"
              >
                Delete
              </button>
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
          <code class="password-display">{{ createdPassword }}</code>
        </div>
      </template>
      <template v-else>
        <form class="create-form" @submit.prevent="createUser">
          <FormInput v-model="newUser.username" label="Username" required />
          <div class="form-group">
            <label class="label">Role</label>
            <select v-model="newUser.role" class="select-input">
              <option value="artist">Artist</option>
              <option value="admin">Admin</option>
              <option v-if="authStore.user?.role === 'superadmin'" value="superadmin">
                Superadmin
              </option>
            </select>
          </div>
          <FormInput
            v-if="newUser.role === 'artist'"
            v-model="newUser.expires_at"
            label="Expires At"
            type="date"
            required
          />
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

.password-result {
  text-align: center;
}

.password-display {
  display: block;
  background-color: var(--color-surface-alt);
  padding: var(--spacing-md);
  border-radius: var(--radius-md);
  margin-top: var(--spacing-md);
  font-size: var(--font-size-lg);
  word-break: break-all;
}
</style>
