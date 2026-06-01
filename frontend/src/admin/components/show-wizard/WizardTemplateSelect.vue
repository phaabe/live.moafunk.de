<script setup lang="ts">
import { onMounted } from 'vue';
import { useShowWizard } from '../../composables';

const wizard = useShowWizard();
const { selectedTemplateId } = wizard;

onMounted(() => {
  if (wizard.templates.value.length === 0 && !wizard.templatesLoading.value) {
    wizard.loadTemplates();
  }
});
</script>

<template>
  <div class="step">
    <h2 class="step-title">Choose a template</h2>

    <div v-if="wizard.templatesLoading.value" class="loading-spinner"></div>

    <div v-else-if="wizard.templates.value.length === 0" class="empty">
      <p class="text-muted">You don't have any templates yet.</p>
      <p class="text-muted">Go back and choose “Create new template”.</p>
    </div>

    <div v-else class="template-grid">
      <button
        v-for="tpl in wizard.templates.value"
        :key="tpl.id"
        type="button"
        :class="['template-card', { selected: selectedTemplateId === tpl.id }]"
        @click="selectedTemplateId = tpl.id"
      >
        <div class="template-cover">
          <img v-if="tpl.cover_url" :src="tpl.cover_url" :alt="tpl.name" />
          <div v-else class="template-cover-placeholder">No cover</div>
        </div>
        <span class="template-name">{{ tpl.name }}</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.step-title {
  font-size: var(--font-size-lg);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin: 0 0 var(--spacing-lg);
  text-align: center;
}

.empty {
  text-align: center;
  padding: var(--spacing-2xl) var(--spacing-md);
}

.template-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: var(--spacing-md);
}

.template-card {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
  padding: var(--spacing-sm);
  background: var(--color-surface-alt);
  border: 2px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  font-family: var(--font-family);
  transition: all var(--transition-fast);
}

.template-card:hover,
.template-card.selected {
  border-color: var(--color-primary);
}

.template-cover {
  aspect-ratio: 1;
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--color-surface);
}

.template-cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.template-cover-placeholder {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.template-name {
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-medium);
  color: var(--color-text);
  text-align: center;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
