<script setup lang="ts">
import { computed, ref } from 'vue';
import type { EpisodeStatus, UpcomingEpisode } from '../../api';
import BoardIcon from '../BoardIcon.vue';
import DateChip from '../DateChip.vue';
import { hostsOf, isMine, timeRange } from './dashboard';

// Dashboard card "Episodes" (#1049): all hosts' episodes, Past (the default)
// or Upcoming. Rows read "So 25.10. · 12:00–14:00 · Brunchtime · lena |
// Upcoming · Live". Own episodes open the episode; other hosts' ones open it
// read-only (eye icon).

type Tab = 'past' | 'upcoming';

interface Row extends UpcomingEpisode {
  status: EpisodeStatus;
  status_label: string;
}

const props = defineProps<{
  past: Row[];
  upcoming: UpcomingEpisode[];
  pastError: string | null;
  upcomingError: string | null;
  loaded: boolean;
  username: string | undefined;
}>();

const TABS: { id: Tab; label: string }[] = [
  { id: 'past', label: 'Past' },
  { id: 'upcoming', label: 'Upcoming' },
];
/** Rows a tab shows before "Show more", so the Shows grid stays in reach. */
const PAGE = 10;

const tab = ref<Tab>('past');
const tabEls = ref<HTMLButtonElement[]>([]);
const shown = ref(PAGE);

const all = computed<Row[]>(() =>
  tab.value === 'past'
    ? props.past
    : props.upcoming.map((e) => ({ ...e, status: 'upcoming', status_label: 'Upcoming' }))
);
const rows = computed(() => all.value.slice(0, shown.value));

function select(id: Tab) {
  tab.value = id;
  shown.value = PAGE;
}
const error = computed(() => (tab.value === 'past' ? props.pastError : props.upcomingError));
const emptyText = computed(() =>
  tab.value === 'past' ? 'No past episodes.' : 'No upcoming episodes.'
);

function mine(e: Row): boolean {
  return isMine(hostsOf(e), props.username);
}

/** Arrow keys move between the tabs, as in any tab list. */
function onKey(event: KeyboardEvent) {
  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
  const index = TABS.findIndex((t) => t.id === tab.value);
  const next = (index + (event.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length;
  select(TABS[next].id);
  tabEls.value[next]?.focus();
}
</script>

<template>
  <section class="board-card episodes-card">
    <div class="board-card-head">
      <h2 id="dashboard-episodes-title" class="board-card-title">
        <BoardIcon name="calendar" />Episodes
      </h2>
      <div class="tabs" role="tablist" aria-labelledby="dashboard-episodes-title" @keydown="onKey">
        <button
          v-for="t in TABS"
          :id="`dashboard-tab-${t.id}`"
          :key="t.id"
          ref="tabEls"
          type="button"
          role="tab"
          :class="['tab', { on: tab === t.id }]"
          :aria-selected="tab === t.id"
          aria-controls="dashboard-episodes"
          :tabindex="tab === t.id ? 0 : -1"
          @click="select(t.id)"
        >
          {{ t.label }}
        </button>
      </div>
    </div>
    <div id="dashboard-episodes" role="tabpanel" :aria-labelledby="`dashboard-tab-${tab}`">
      <p v-if="error" class="empty field-error" role="alert">{{ error }}</p>
      <p v-else-if="loaded && !rows.length" class="empty">{{ emptyText }}</p>
      <ul :class="['episodes', 'board-rows', tab]">
        <li v-for="e in rows" :key="e.id">
          <router-link :to="`/shows/${e.id}`" class="board-row">
            <span class="row-main">
              <DateChip :date="e.date" :tone="mine(e) ? 'mine' : ''" />
              <span class="row-body">
                <span class="row-time"
                  ><template v-if="timeRange(e)"
                    ><span class="sep"> · </span>{{ timeRange(e) }}</template
                  ></span
                >
                <span class="sep"> · </span>
                <span class="row-title">{{ e.show_name }}</span>
                <span class="sep"> · </span>
                <span :class="['row-host', { mine: mine(e) }]">{{ hostsOf(e).join(', ') }}</span>
              </span>
            </span>
            <span class="row-tags">
              <span :class="['tag', `tag-${e.status}`]">{{ e.status_label }}</span>
              <span class="sep"> · </span>
              <span class="tag">{{ e.mode_label }}</span>
            </span>
            <BoardIcon
              :name="mine(e) ? 'chevron' : 'eye'"
              :class="['row-icon', { 'read-only': !mine(e) }]"
            />
          </router-link>
        </li>
      </ul>
      <div v-if="all.length > rows.length" class="more">
        <button type="button" class="more-button" @click="shown += PAGE">Show more</button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.episodes-card {
  container-type: inline-size;
}

.host-page .episodes-card .board-card-head {
  flex-wrap: wrap;
  gap: 10px;
  min-height: 52px;
  padding-top: 8px;
  padding-bottom: 8px;
}

.tabs {
  display: inline-flex;
  gap: 4px;
  margin-left: auto;
  padding: 3px;
  border: 1px solid #2a2a42;
  border-radius: 10px;
  background: #06060f;
}

.tab {
  min-height: 44px;
  padding: 0 16px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: #b0b0c4;
  font: 700 12px var(--font-family);
  letter-spacing: 0.1em;
  text-transform: uppercase;
  cursor: pointer;
}

.tab:hover {
  color: #fff;
}

.tab.on {
  background: var(--color-primary);
  color: #000;
}

.tab:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

.more {
  display: flex;
  justify-content: center;
  padding: 12px var(--spacing-md);
  border-top: 1px solid #1e1e34;
}

.more-button {
  min-height: 44px;
  padding: 0 18px;
  border: 1px solid #4a4a60;
  border-radius: var(--radius-lg);
  background: transparent;
  color: #eee;
  font: 700 13px var(--font-family);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  cursor: pointer;
}

.more-button:hover {
  border-color: #6a6a84;
}

/* A wide card: time, Show and host line up in columns, like the design. */
@container (min-width: 760px) {
  .host-page .episodes-card .row-body {
    display: grid;
    grid-template-columns: 104px minmax(0, 1fr) minmax(0, 112px);
    align-items: baseline;
  }

  .host-page .episodes-card .row-tags {
    width: 240px;
  }
}

@media (max-width: 640px) {
  .host-page .episodes-card .board-card-head {
    flex-direction: column;
    align-items: stretch;
  }

  .tabs {
    display: flex;
    margin-left: 0;
  }

  .tab {
    flex: 1;
  }
}
</style>
