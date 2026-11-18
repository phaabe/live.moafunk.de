<script setup lang="ts">
import { browserProblem } from '../composables/browserSupport';

// Checked once: the browser does not change while the page is open.
const problem = browserProblem();
</script>

<template>
  <!-- On every page, so a host sees it days before the show. It cannot be closed. -->
  <p v-if="problem" class="browser-warning" role="alert">{{ problem }}</p>
</template>

<style scoped>
.browser-warning {
  margin: 0;
  padding: var(--spacing-md) var(--spacing-lg);
  border-bottom: 1px solid #6b2424;
  background: #3a1414;
  color: #ffd2cc;
  font-weight: 600;
  text-align: center;
}
</style>
