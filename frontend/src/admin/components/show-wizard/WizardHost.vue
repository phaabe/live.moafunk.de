<script setup lang="ts">
import { computed, onMounted } from 'vue';
import { useShowWizard } from '../../composables';

const wizard = useShowWizard();
const { assigneeUserId, guestUsername } = wizard;

// Load assignable users lazily when the "existing user" sub-mode is in use.
onMounted(() => {
  if (
    wizard.hostMode.value === 'existing' &&
    wizard.assignableUsers.value.length === 0 &&
    !wizard.assigneeLoading.value
  ) {
    wizard.loadAssignableUsers();
  }
});

const showDate = computed(() => wizard.startDateTime.value);
const dateLabel = computed(() =>
  showDate.value
    ? showDate.value.toLocaleDateString('en-US', {
        weekday: 'short',
        month: 'short',
        day: 'numeric',
        year: 'numeric',
      })
    : 'the show date'
);

async function pickExisting() {
  wizard.setHostMode('existing');
  if (wizard.assignableUsers.value.length === 0 && !wizard.assigneeLoading.value) {
    await wizard.loadAssignableUsers();
  }
}

function pickGuest() {
  wizard.setHostMode('guest');
}
</script>

<template>
  <div class="step">
    <h2 class="step-title">Who presents the show?</h2>
    <p class="step-hint">Assign an existing user, or create a guest as the host.</p>

    <div class="mode-toggle">
      <button
        type="button"
        :class="['toggle-btn', { active: wizard.hostMode.value === 'existing' }]"
        @click="pickExisting"
      >
        Existing user
      </button>
      <button
        type="button"
        :class="['toggle-btn', { active: wizard.hostMode.value === 'guest' }]"
        @click="pickGuest"
      >
        Create guest
      </button>
    </div>

    <!-- Existing user -->
    <template v-if="wizard.hostMode.value === 'existing'">
      <div v-if="wizard.assigneeLoading.value" class="loading-spinner"></div>

      <div v-else-if="wizard.assignableUsers.value.length === 0" class="empty">
        <p class="text-muted">No assignable users found.</p>
        <p class="text-muted">Switch to “Create guest” to add a one-off host.</p>
      </div>

      <div v-else class="user-list">
        <button
          v-for="user in wizard.assignableUsers.value"
          :key="user.id"
          type="button"
          :class="['user-item', { selected: assigneeUserId === user.id }]"
          @click="assigneeUserId = user.id"
        >
          <span class="user-name">{{ user.username }}</span>
          <span class="user-role">{{ user.role }}</span>
        </button>
      </div>
    </template>

    <!-- Create guest -->
    <template v-else>
      <div class="field">
        <label class="field-label" for="guest-username">Guest username</label>
        <input
          id="guest-username"
          v-model="guestUsername"
          type="text"
          class="field-input"
          placeholder="e.g. dj-guest"
          autocomplete="off"
        />
      </div>

      <p class="field-note">
        Creates a host account that can only log in on {{ dateLabel }} and is deleted automatically
        afterwards. A one-time password is generated on creation; the guest sets their own on first
        login.
      </p>
    </template>
  </div>
</template>

<style scoped>
.step-title {
  font-size: var(--font-size-lg);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin: 0 0 var(--spacing-xs);
  text-align: center;
}

.step-hint {
  color: var(--color-text-muted);
  margin: 0 0 var(--spacing-lg);
  text-align: center;
}

.mode-toggle {
  display: flex;
  justify-content: center;
  gap: var(--spacing-sm);
  margin-bottom: var(--spacing-xl);
}

.toggle-btn {
  padding: var(--spacing-sm) var(--spacing-lg);
  background: var(--color-surface-alt);
  border: 2px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text-muted);
  font-family: var(--font-family);
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-medium);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.toggle-btn.active {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.empty {
  text-align: center;
  padding: var(--spacing-2xl) var(--spacing-md);
}

.user-list {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
  max-width: 420px;
  margin: 0 auto;
}

.user-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--spacing-md);
  background: var(--color-surface-alt);
  border: 2px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  font-family: var(--font-family);
  transition: all var(--transition-fast);
}

.user-item:hover,
.user-item.selected {
  border-color: var(--color-primary);
}

.user-name {
  font-weight: var(--font-weight-medium);
  color: var(--color-text);
}

.user-role {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  text-transform: uppercase;
}

.field {
  max-width: 360px;
  margin: 0 auto;
  text-align: left;
}

.field-label {
  display: block;
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-medium);
  color: var(--color-text-muted);
  margin-bottom: var(--spacing-xs);
}

.field-input {
  width: 100%;
  padding: var(--spacing-sm) var(--spacing-md);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-family: var(--font-family);
  font-size: var(--font-size-base);
}

.field-input:focus {
  outline: none;
  border-color: var(--color-primary);
}

.field-note {
  max-width: 360px;
  margin: var(--spacing-lg) auto 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  text-align: center;
}
</style>
