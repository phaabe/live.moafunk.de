<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { BaseButton } from '@shared/components';
import { hostsApi, type HostAccount, type SeriesHost } from '../api';
import { useAuthStore } from '../stores/auth';
import { episodeHostsText, hostGroups } from './show-page/showPage';

// The Hosts of a Show or of one episode in edit mode (#1047). Who manages
// the list adds a host account at once (no invite, no mail) and removes a
// co-host (asks once). A co-host sees the names as text and may leave.
// Show: one row per host and an Add host select. Episode: one Hosts row,
// a × after each episode co-host, and an Add host button.

const props = defineProps<{
  /** A Show by slug, or one episode by id. */
  target: { slug: string } | { showId: number };
  hosts: SeriesHost[];
  /** May add and remove: the Show's owner; for an episode, its Show's hosts. */
  manage: boolean;
}>();
const emit = defineEmits<{ changed: [hosts: SeriesHost[]]; left: [] }>();

const ROLE = { owner: 'Owner', cohost: 'Co-host', episode: 'Episode co-host' } as const;

const auth = useAuthStore();
const accounts = ref<HostAccount[]>([]);
const picking = ref(false);
const pick = ref('');
const busy = ref(false);
const error = ref<string | null>(null);

const isEpisode = computed(() => 'showId' in props.target);
/** Who a manager may remove: the Show's co-hosts, or the episode's own co-hosts. */
const removable = (h: SeriesHost) =>
  props.manage && h.role === (isEpisode.value ? 'episode' : 'cohost');
const me = computed(() => props.hosts.find((h) => h.user_id === auth.user?.id));
const canLeave = computed(
  () => !props.manage && !!me.value && me.value.role === (isEpisode.value ? 'episode' : 'cohost')
);
const choices = computed(() =>
  accounts.value.filter((a) => !props.hosts.some((h) => h.user_id === a.id))
);
const groups = computed(() => hostGroups(props.hosts));
const names = computed(() =>
  isEpisode.value ? episodeHostsText(props.hosts) : props.hosts.map((h) => h.username).join(', ')
);

onMounted(async () => {
  if (!props.manage) return;
  try {
    accounts.value = await hostsApi.list();
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not load the hosts';
  }
});

async function run(change: () => Promise<SeriesHost[]>): Promise<SeriesHost[] | null> {
  busy.value = true;
  error.value = null;
  try {
    return await change();
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not change the hosts';
    return null;
  } finally {
    busy.value = false;
  }
}

function add(id: number) {
  const t = props.target;
  return 'slug' in t ? hostsApi.addToSeries(t.slug, id) : hostsApi.addToEpisode(t.showId, id);
}
function remove(id: number) {
  const t = props.target;
  return 'slug' in t
    ? hostsApi.removeFromSeries(t.slug, id)
    : hostsApi.removeFromEpisode(t.showId, id);
}

async function addPicked() {
  const id = Number(pick.value);
  pick.value = '';
  if (!id) return;
  const hosts = await run(() => add(id));
  if (hosts) {
    picking.value = false;
    emit('changed', hosts);
  }
}

async function removeHost(h: SeriesHost) {
  if (!window.confirm(`Remove ${h.username} as co-host?`)) return;
  const hosts = await run(() => remove(h.user_id));
  if (hosts) emit('changed', hosts);
}

async function leave() {
  const what = isEpisode.value ? 'episode' : 'show';
  if (!me.value || !window.confirm(`Leave this ${what}?`)) return;
  const id = me.value.user_id;
  if (await run(() => remove(id))) emit('left');
}
</script>

<template>
  <div :class="['hosts-field', { episode: isEpisode }]">
    <template v-if="isEpisode">
      <dl class="hosts-row">
        <div>
          <dt>Hosts</dt>
          <dd>
            <template v-for="(group, g) in groups" :key="g"
              >{{ g ? ' · ' : ''
              }}<template v-for="(h, i) in group" :key="h.user_id"
                >{{ i ? ', ' : ''
                }}<span class="name"
                  >{{ h.username
                  }}<button
                    v-if="removable(h)"
                    type="button"
                    class="x"
                    :aria-label="`Remove ${h.username}`"
                    :disabled="busy"
                    @click="removeHost(h)"
                  >
                    ×
                  </button></span
                ></template
              ></template
            >
          </dd>
        </div>
      </dl>
      <div v-if="manage" class="actions">
        <BaseButton
          v-if="!picking"
          variant="secondary"
          :disabled="busy || !choices.length"
          @click="picking = true"
          >Add host</BaseButton
        >
        <label v-else class="field">
          <span class="label">Pick a host</span>
          <select v-model="pick" class="input" :disabled="busy" @change="addPicked">
            <option value="" disabled>Pick a host</option>
            <option v-for="a in choices" :key="a.id" :value="String(a.id)">{{ a.username }}</option>
          </select>
        </label>
      </div>
    </template>

    <template v-else>
      <h2 class="title">Hosts</h2>
      <ul v-if="manage" class="rows">
        <li v-for="h in hosts" :key="h.user_id">
          <span class="name">{{ h.username }}</span>
          <span class="role">{{ ROLE[h.role] }}</span>
          <BaseButton
            v-if="removable(h)"
            variant="secondary"
            class="remove"
            :aria-label="`Remove ${h.username}`"
            :disabled="busy"
            @click="removeHost(h)"
            >Remove</BaseButton
          >
        </li>
      </ul>
      <p v-else class="names">{{ names }}</p>
      <label v-if="manage" class="field">
        <span class="label">Add host</span>
        <select
          v-model="pick"
          class="input"
          :disabled="busy || !choices.length"
          @change="addPicked"
        >
          <option value="" disabled>{{ choices.length ? 'Pick a host' : 'No other hosts' }}</option>
          <option v-for="a in choices" :key="a.id" :value="String(a.id)">{{ a.username }}</option>
        </select>
      </label>
    </template>

    <BaseButton v-if="canLeave" variant="secondary" :disabled="busy" @click="leave">{{
      isEpisode ? 'Leave episode' : 'Leave show'
    }}</BaseButton>
    <p v-if="error" class="field-error" role="alert">{{ error }}</p>
  </div>
</template>

<style scoped>
.hosts-field {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  flex-basis: 100%;
}

.title {
  flex-basis: 100%;
  margin: 0;
  font-size: var(--font-size-md, 1rem);
}

.rows {
  flex-basis: 100%;
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.rows li {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 44px;
}

.rows .name {
  font-weight: 700;
}

.rows .role {
  color: #a0a0b4;
}

.rows .remove {
  margin-left: auto;
}

.names {
  margin: 0;
  font-weight: 700;
}

.hosts-row {
  margin: 0;
}

.hosts-row > div {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 10px;
}

.hosts-row dt {
  color: #a0a0b4;
}

.hosts-row dd {
  margin: 0;
  font-weight: 700;
  color: #d0d0dc;
}

.x {
  min-width: 32px;
  min-height: 32px;
  margin-left: 2px;
  border: 0;
  background: none;
  color: #a0a0b4;
  font: inherit;
  cursor: pointer;
}

.x:hover,
.x:focus-visible {
  color: var(--color-text, #fff);
}

.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 200px;
}

.actions {
  display: flex;
  gap: 8px;
}
</style>
