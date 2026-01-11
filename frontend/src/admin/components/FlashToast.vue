<script setup lang="ts">
import { useFlash } from '../composables/useFlash';

const { messages, dismiss } = useFlash();
</script>

<template>
  <Teleport to="body">
    <TransitionGroup name="flash" tag="div" class="flash-container">
      <div
        v-for="msg in messages"
        :key="msg.id"
        :class="['flash-toast', msg.type]"
        @click="dismiss(msg.id)"
      >
        <span class="flash-icon">
          <template v-if="msg.type === 'success'">✓</template>
          <template v-else-if="msg.type === 'error'">✕</template>
          <template v-else>ℹ</template>
        </span>
        <span class="flash-text">{{ msg.message }}</span>
        <button class="flash-close" @click.stop="dismiss(msg.id)">×</button>
      </div>
    </TransitionGroup>
  </Teleport>
</template>

<style scoped>
.flash-container {
  position: fixed;
  top: var(--spacing-lg);
  right: var(--spacing-lg);
  z-index: 10000;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
  max-width: 400px;
}

.flash-toast {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  padding: var(--spacing-md) var(--spacing-lg);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.flash-toast:hover {
  transform: translateX(-4px);
}

.flash-toast.success {
  background-color: var(--color-success);
  color: var(--color-bg);
}

.flash-toast.error {
  background-color: var(--color-error);
  color: var(--color-bg);
}

.flash-toast.info {
  background-color: var(--color-primary);
  color: var(--color-bg);
}

.flash-icon {
  font-weight: bold;
  font-size: var(--font-size-lg);
}

.flash-text {
  flex: 1;
}

.flash-close {
  background: none;
  border: none;
  color: inherit;
  cursor: pointer;
  font-size: var(--font-size-lg);
  padding: 0;
  opacity: 0.7;
}

.flash-close:hover {
  opacity: 1;
}

/* Transition animations */
.flash-enter-active,
.flash-leave-active {
  transition: all 0.3s ease;
}

.flash-enter-from {
  opacity: 0;
  transform: translateX(100%);
}

.flash-leave-to {
  opacity: 0;
  transform: translateX(100%);
}
</style>
