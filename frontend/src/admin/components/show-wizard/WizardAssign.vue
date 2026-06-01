<script setup lang="ts">
import { onMounted } from 'vue';
import { useShowWizard } from '../../composables';

const wizard = useShowWizard();
const { assigneeUserId } = wizard;

onMounted(() => {
  if (wizard.assignableUsers.value.length === 0 && !wizard.assigneeLoading.value) {
    wizard.loadAssignableUsers();
  }
});
</script>

<template>
  <div class="step">
    <h2 class="step-title">Who presents the show?</h2>
    <p class="step-hint">Assign the user who will run this show.</p>

    <div v-if="wizard.assigneeLoading.value" class="loading-spinner"></div>

    <div v-else-if="wizard.assignableUsers.value.length === 0" class="empty">
      <p class="text-muted">No assignable users found.</p>
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
  margin: 0 0 var(--spacing-xl);
  text-align: center;
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
</style>
