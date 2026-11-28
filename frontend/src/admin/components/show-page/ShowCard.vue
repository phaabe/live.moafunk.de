<script setup lang="ts">
import { computed } from 'vue';
import { BaseButton } from '@shared/components';
import type { SeriesDetail } from '../../api';
import { useAuthStore } from '../../stores/auth';
import { initials, profilePath, rhythmText } from './showPage';

// The Show's header card on its detail page (issue 1051, design D1): cover,
// name, Edit, description and the facts rhythm, hosts and Instagram.
// Read-only; its owner edits it on My Show.

const props = defineProps<{
  series: SeriesDetail;
  /** Show "Edit" (the owner, on the host pages). */
  editable: boolean;
}>();
const emit = defineEmits<{ edit: [] }>();

const rhythm = computed(() => rhythmText(props.series));
const hosts = computed(() => (props.series.hosts ?? []).map((h) => h.username));
const hostsLabel = computed(() => (hosts.value.length === 1 ? 'Host' : 'Hosts'));
const auth = useAuthStore();
const handle = computed(() => props.series.instagram_handle || null);
</script>

<template>
  <section class="board-card show-card" :aria-label="`Show ${series.name}`">
    <div class="cover show-cover">
      <img
        v-if="series.cover_url"
        :src="series.cover_url"
        :srcset="series.cover_srcset ?? undefined"
        sizes="(max-width: 640px) 100vw, 232px"
        :alt="`Cover of ${series.name}`"
      />
      <span v-else class="cover-initials" aria-hidden="true">{{ initials(series.name) }}</span>
    </div>
    <div class="show-text">
      <div class="show-head">
        <h1 class="page-title show-name">{{ series.name }}</h1>
        <BaseButton v-if="editable" variant="secondary" class="edit" @click="emit('edit')"
          ><svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z" /></svg
          >Edit</BaseButton
        >
      </div>
      <p v-if="series.description" class="description">{{ series.description }}</p>
      <dl class="facts">
        <div class="fact">
          <dt>Rhythm<span class="sep">: </span></dt>
          <dd>{{ rhythm }}</dd>
        </div>
        <div v-if="hosts.length" class="fact">
          <!-- Each name opens the host's profile (#1117). The first link carries
               the label for screen readers and the flow check ("Host: | lena"). -->
          <dt aria-hidden="true">{{ hostsLabel }}<span class="sep">: </span></dt>
          <dd class="hosts">
            <template v-for="(name, i) in hosts" :key="name"
              ><span v-if="i" class="sep">, </span
              ><router-link :to="profilePath(name, auth.user?.username)" class="host"
                ><span v-if="!i" class="sep">{{ hostsLabel }}: </span
                ><span class="avatar" :data-initial="name.charAt(0).toUpperCase()"></span
                ><span>{{ name }}</span></router-link
              ></template
            >
          </dd>
        </div>
        <div class="fact">
          <dt>Instagram<span class="sep">: </span></dt>
          <dd :class="{ muted: !handle }">{{ handle ? `@${handle}` : 'Not linked' }}</dd>
        </div>
      </dl>
    </div>
  </section>
</template>

<style scoped>
.show-card {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-lg) var(--spacing-xl);
  padding: var(--spacing-md);
}

.show-cover {
  flex: none;
  width: 232px;
  height: 232px;
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-lg);
}

.cover-initials {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  background: var(--color-surface-alt);
  color: var(--color-primary);
  font: 700 4.5rem/1 var(--font-family);
  letter-spacing: -0.04em;
}

.show-text {
  flex: 1 1 320px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-top: 4px;
}

.show-head {
  display: flex;
  align-items: flex-start;
  gap: var(--spacing-md);
}

.show-name {
  font-size: 2.75rem;
  line-height: 1.05;
  overflow-wrap: anywhere;
}

.edit {
  flex: none;
  margin-left: auto;
}

/* Long descriptions stop after three lines; the card keeps its height. */
.description {
  margin: 0;
  max-width: 60ch;
  font-size: var(--font-size-md);
  color: #c8c8d4;
  white-space: pre-line;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(180px, 100%), 1fr));
  gap: var(--spacing-md) var(--spacing-lg);
  margin: auto 0 0;
  padding-top: var(--spacing-md);
  border-top: 1px solid #2a2a42;
}

.fact {
  min-width: 0;
}

.fact dt {
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #a8a8bc;
}

.fact dd {
  margin: 4px 0 0;
  font-size: 15px;
  font-weight: 500;
  color: #eee;
}

.fact dd.muted {
  color: #a8a8bc;
}

.hosts {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
}

.host {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: inherit;
  text-decoration: none;
}

.host:hover span:last-child {
  color: var(--color-primary);
  text-decoration: underline;
}

.avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border-radius: 50%;
  background: var(--color-primary);
  color: #000;
  font-size: 12px;
  font-weight: 700;
}

/* The initial is decoration: out of the text, so the row reads "Host: lena". */
.avatar::before {
  content: attr(data-initial);
}

@media (max-width: 640px) {
  .show-cover {
    width: 100%;
    height: auto;
    aspect-ratio: 1 / 1;
  }

  .show-name {
    font-size: 2rem;
  }
}
</style>
