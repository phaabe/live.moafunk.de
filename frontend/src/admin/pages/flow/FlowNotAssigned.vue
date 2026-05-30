<script setup lang="ts">
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { useHostFlow } from '@admin/composables';
import { BaseButton } from '@shared/components';
import ShowCreateModal from '@admin/components/ShowCreateModal.vue';

const router = useRouter();
const flow = useHostFlow();

const showCreateModal = ref(false);

/** Reset the cached flow so the new show is picked up, then re-enter /stream. */
async function onShowCreated() {
  showCreateModal.value = false;
  flow.reset();
  await router.push('/stream');
}
</script>

<template>
  <div class="flow-not-assigned">
    <div class="not-assigned-content">
      <img src="/assets/brand/moafunk.png" alt="Moafunk" class="not-assigned-logo" />
      <h1 class="not-assigned-title">No Shows Yet</h1>
      <p class="not-assigned-message">
        You are not currently assigned to a show.<br />
        Create your own to start streaming.
      </p>
      <BaseButton variant="primary" class="not-assigned-cta" @click="showCreateModal = true">+ New Show</BaseButton>
    </div>

    <ShowCreateModal
      :open="showCreateModal"
      @close="showCreateModal = false"
      @created="onShowCreated"
    />
  </div>
</template>

<style scoped>
.flow-not-assigned {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 60vh;
}

.not-assigned-content {
  text-align: center;
  max-width: 400px;
}

.not-assigned-logo {
  height: 48px;
  width: auto;
  margin-bottom: var(--spacing-xl);
  opacity: 0.6;
}

.not-assigned-title {
  font-size: var(--font-size-2xl);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin: 0 0 var(--spacing-md);
}

.not-assigned-message {
  color: var(--color-text-muted);
  line-height: var(--line-height-relaxed);
  margin: 0;
}

.not-assigned-cta {
  margin-top: var(--spacing-xl);
}
</style>
