<script setup lang="ts">
import { computed } from 'vue';
import type { SeriesOverviewItem } from '../../api';
import BoardIcon from '../BoardIcon.vue';
import ShowCover from './ShowCover.vue';
import { byEpisodes, episodeCount, hostsOf, isMine } from './dashboard';

// Dashboard card "Shows" (#1049): every host's Show as a tile with cover,
// host and finished episodes, most episodes first. A tile opens the Show's
// page, read-only for other hosts.
const props = defineProps<{
  shows: SeriesOverviewItem[];
  error: string | null;
  loaded: boolean;
  username: string | undefined;
}>();

const tiles = computed(() =>
  byEpisodes(props.shows).map((s) => {
    const hosts = hostsOf(s);
    return { ...s, hosts, mine: isMine(hosts, props.username) };
  })
);
</script>

<template>
  <section class="board-card shows-card">
    <div class="board-card-head">
      <h2 class="board-card-title"><BoardIcon name="grid" />Shows</h2>
      <span class="shows-sub">Most episodes first</span>
    </div>
    <p v-if="error" class="empty field-error" role="alert">{{ error }}</p>
    <p v-else-if="loaded && !tiles.length" class="empty">No shows yet.</p>
    <ul v-if="tiles.length" class="show-tiles">
      <li v-for="s in tiles" :key="s.slug">
        <router-link :to="`/shows/${s.slug}`" class="show-tile">
          <ShowCover
            :url="s.cover_url"
            :srcset="s.cover_srcset"
            sizes="(max-width: 640px) 50vw, 300px"
            class="tile-cover"
          />
          <span class="tile-name"
            >{{ s.name
            }}<span v-if="s.mine" class="yours"><span class="sep"> · </span>Yours</span></span
          >
          <span class="tile-meta"
            ><span class="tile-hosts">{{ s.hosts.join(', ') }}</span
            ><span class="sep"> · </span
            ><span class="tile-count">{{ episodeCount(s.finished_episodes) }}</span></span
          >
        </router-link>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.host-page .shows-card .board-card-head {
  justify-content: flex-start;
  flex-wrap: wrap;
  gap: 10px;
}

.shows-sub {
  margin-left: auto;
  font: 600 12px var(--font-ui);
  color: #a0a0b4;
}

.show-tiles {
  list-style: none;
  margin: 0;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 20px;
  padding: 20px;
}

.show-tile {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
  border-radius: 10px;
  color: #eee;
  text-decoration: none;
}

.show-tile:hover .tile-name {
  color: var(--color-primary);
}

.show-tile:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 4px;
}

.tile-cover {
  width: 100%;
  aspect-ratio: 1;
  border-radius: 10px;
}

.tile-name {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  font-weight: 700;
  font-size: var(--font-size-md);
  color: #fff;
  overflow-wrap: anywhere;
}

.yours {
  padding: 2px 6px;
  border-radius: var(--radius-sm);
  background: var(--color-primary);
  color: #000;
  font: 700 10px var(--font-ui);
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.tile-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 4px 8px;
  font-size: var(--font-size-sm);
  color: #b0b0c4;
}

.tile-count {
  font: 700 13px var(--font-family);
  color: #e8e2a8;
}

@media (max-width: 640px) {
  .show-tiles {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 14px;
    padding: 16px;
  }
}
</style>
