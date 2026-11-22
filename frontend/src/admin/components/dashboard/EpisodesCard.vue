<script setup lang="ts">
import { computed, ref } from 'vue';
import type { EpisodeStatus, UpcomingEpisode } from '../../api';
import BoardIcon from '../BoardIcon.vue';
import DateChip from '../DateChip.vue';
import { hostsOf, isMine, timeRange } from './dashboard';

// Dashboard card "Episodes" (#1049): all hosts' episodes, Past (the default)
// or Upcoming, three at a time with arrows, so the card keeps one height.
// Rows read "So 25.10. · 12:00–14:00 · Brunchtime · lena | Upcoming · Live".
// Own episodes open the episode; other hosts' ones open it read-only (eye
// icon). Color: yours white, others gray, your open wrap-up yellow.

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
/** Rows per page: the card keeps this height, the arrows page through the rest. */
const PAGE = 3;

const tab = ref<Tab>('past');
const tabEls = ref<HTMLButtonElement[]>([]);
const page = ref(0);

const all = computed<Row[]>(() =>
  tab.value === 'past'
    ? props.past
    : props.upcoming.map((e) => ({ ...e, status: 'upcoming', status_label: 'Upcoming' }))
);
const pages = computed(() => Math.max(1, Math.ceil(all.value.length / PAGE)));
/** Clamped, so a reload with fewer episodes never leaves an empty page. */
const current = computed(() => Math.min(page.value, pages.value - 1));
const rows = computed(() => all.value.slice(current.value * PAGE, (current.value + 1) * PAGE));
/** "4–6 of 12" */
const range = computed(() => {
  const first = current.value * PAGE + 1;
  return `${first}–${first + rows.value.length - 1} of ${all.value.length}`;
});

function select(id: Tab) {
  tab.value = id;
  page.value = 0;
}

function go(step: -1 | 1) {
  page.value = Math.min(pages.value - 1, Math.max(0, current.value + step));
}
const error = computed(() => (tab.value === 'past' ? props.pastError : props.upcomingError));
const emptyText = computed(() =>
  tab.value === 'past' ? 'No past episodes.' : 'No upcoming episodes.'
);

function mine(e: Row): boolean {
  return isMine(hostsOf(e), props.username);
}

/** The host's own episode waits for its wrap-up: the one row in yellow. */
function needsYou(e: Row): boolean {
  return e.status === 'wrapup_open' && mine(e);
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
      <div class="panel-rows">
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
                <span :class="['tag', `tag-${e.status}`, { 'needs-you': needsYou(e) }]">{{
                  e.status_label
                }}</span>
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
      </div>
      <div class="pager">
        <span class="pager-range" aria-live="polite">{{ rows.length ? range : '' }}</span>
        <button
          type="button"
          class="pager-button"
          aria-label="Previous episodes"
          :disabled="current === 0"
          @click="go(-1)"
        >
          <BoardIcon name="back" />
        </button>
        <button
          type="button"
          class="pager-button"
          aria-label="Next episodes"
          :disabled="current >= pages - 1"
          @click="go(1)"
        >
          <BoardIcon name="chevron" />
        </button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.episodes-card {
  --row-h: 70px;
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
  background: #2a2a42;
  color: #fff;
}

.tab:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

/* One height for every page and both tabs: three row slots, empty ones too. */
.panel-rows {
  min-height: calc(3 * var(--row-h) + 2 * var(--spacing-sm));
}

.episodes {
  display: grid;
  grid-template-rows: repeat(3, minmax(var(--row-h), auto));
}

.episodes:empty {
  display: none;
}

.episodes > li > .board-row {
  height: 100%;
}

.pager {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px var(--spacing-md);
  border-top: 1px solid #1e1e34;
}

.pager-range {
  margin-right: auto;
  font: 700 13px var(--font-family);
  color: #a0a0b4;
  font-variant-numeric: tabular-nums;
}

.pager-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  border: 1px solid #4a4a60;
  border-radius: var(--radius-lg);
  background: transparent;
  color: #fff;
  cursor: pointer;
}

.pager-button:hover:not(:disabled) {
  border-color: #6a6a84;
}

.pager-button:disabled {
  border-color: #2a2a42;
  color: #4a4a60;
  cursor: default;
}

.pager-button:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

/* Priority colors (#1049): yours white, others gray, your open wrap-up yellow. */
.host-page .episodes-card .date-chip.mine {
  background: var(--color-surface-alt);
  box-shadow: inset 0 0 0 1.5px #fff;
  color: #fff;
}

.host-page .episodes-card :deep(.date-chip.mine small) {
  color: #d0d0dc;
}

.host-page .episodes-card .row-host.mine,
.host-page .episodes-card .row-icon {
  color: #fff;
}

.host-page .episodes-card .row-icon.read-only {
  color: #8e8ea4;
}

.host-page .episodes-card .tag {
  border-color: #4a4a60;
  background: transparent;
  color: #b0b0c4;
}

.host-page .episodes-card .tag.needs-you {
  border-color: var(--color-primary);
  color: var(--color-primary);
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
  /* Room for a row whose two tags wrap, so every page keeps one height. */
  .episodes-card {
    --row-h: 141px;
  }

  /* host.css lays rows out like this only for a list right under the card;
     here the list sits in the tab panel. Tags go under the text. */
  .host-page .episodes-card .board-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-areas:
      'main icon'
      'tags icon';
    align-content: center;
    row-gap: 6px;
  }

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
