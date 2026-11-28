<script setup lang="ts">
import type { RouteLocationRaw } from 'vue-router';
import BoardIcon from '../BoardIcon.vue';

// A link drawn as the host pages' button: a route (`to`) or the public site (`href`).
// The text sits in its own span, so the link reads as that text alone.
defineProps<{
  to?: RouteLocationRaw;
  href?: string;
  icon: InstanceType<typeof BoardIcon>['$props']['name'];
  primary?: boolean;
}>();
</script>

<template>
  <router-link v-if="to" :to="to" :class="['card-action', { primary }]"
    ><span><slot /></span><BoardIcon :name="icon" :size="16"
  /></router-link>
  <a v-else :href="href" target="_blank" rel="noopener" :class="['card-action', { primary }]"
    ><span><slot /></span><BoardIcon :name="icon" :size="16"
  /></a>
</template>

<style scoped>
.card-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 44px;
  padding: 0 18px;
  border: 1px solid #4a4a60;
  border-radius: var(--radius-lg);
  background: transparent;
  color: #eee;
  font: 700 14px/1 var(--font-family);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  text-decoration: none;
}

.card-action:hover {
  border-color: #6a6a84;
  color: #fff;
}

.card-action.primary {
  border-color: var(--color-primary);
  background: var(--color-primary);
  color: #000;
}

.card-action.primary:hover {
  background: var(--color-primary-hover);
  color: #000;
}

.card-action:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

@media (max-width: 640px) {
  .card-action {
    width: 100%;
    min-height: 48px;
  }
}
</style>
