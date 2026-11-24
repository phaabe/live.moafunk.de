<script setup lang="ts">
import { computed, ref } from 'vue';
import { BaseButton } from '@shared/components';
import type { SeriesHost, ShowDetail } from '../../api';
import { episodeDay } from '../../episodeRow';
import BoardIcon from '../BoardIcon.vue';
import EpisodeDetailsCard from '../EpisodeDetailsCard.vue';
import { episodeHostsText } from '../show-page/showPage';
import EpisodeIcon from './EpisodeIcon.vue';
import { timeRange, type EpisodePhase } from './phase';

// Heading box of the episode page (#1052): cover, date and time, title,
// description, status, mode and hosts as read-only text. Edit (the episode's
// hosts, before the start) turns it into the form.

const props = defineProps<{
  show: ShowDetail;
  phase: EpisodePhase;
  /** One of the episode's hosts: may edit before the start. */
  canEdit?: boolean;
}>();
const emit = defineEmits<{ saved: []; hosts: [hosts: SeriesHost[]]; left: [] }>();

const editing = ref(false);

/** The episode's hosts (#1047): the Show's hosts, then the episode's own co-hosts. */
const hostCount = computed(() => props.show.hosts?.length ?? (props.show.host_username ? 1 : 0));
const hostsText = computed(() =>
  props.show.hosts ? episodeHostsText(props.show.hosts) : (props.show.host_username ?? '')
);
const when = computed(() =>
  [episodeDay(props.show.date), timeRange(props.show)].filter(Boolean).join(' · ')
);
const description = computed(() => props.show.description || props.show.series_description);
const cover = computed(() => props.show.series_cover_url ?? props.show.cover_url ?? null);
const coverSrcset = computed(
  () =>
    (props.show.series_cover_url ? props.show.series_cover_srcset : props.show.cover_srcset) ??
    undefined
);
const showName = computed(() => props.show.series_name ?? props.show.title);
const mode = computed(() => (props.show.stream_mode === 'prerecorded' ? 'Pre-recorded' : 'Live'));

const STATUS = {
  upcoming: 'Upcoming',
  wrapup_open: 'Finished · wrap-up open',
  finished: 'Finished',
};
const pill = computed(() => {
  const s = props.show;
  if (s.on_air) return { tone: 'live', text: `On air · until ${s.end_time ?? ''}`.trim() };
  if (props.phase === 'live') return { tone: 'warn', text: `Show time · until ${s.end_time}` };
  const text = s.episode_status_label || STATUS[s.episode_status ?? 'upcoming'];
  if (s.episode_status === 'finished') return { tone: 'ok', text };
  if (s.episode_status === 'wrapup_open' || props.phase === 'post') return { tone: 'warn', text };
  return { tone: '', text };
});

/** Why Edit is locked: details change only before the start. */
const locked = computed(() => {
  if (props.phase === 'live') return 'locked on air';
  if (props.phase === 'post') return 'locked after the show';
  return null;
});

function saved() {
  editing.value = false;
  emit('saved');
}
</script>

<template>
  <section
    :class="['ep-head', { editing }]"
    :aria-label="editing ? 'Episode details, editing' : 'Episode details'"
  >
    <EpisodeDetailsCard
      v-if="editing && canEdit"
      :show="show"
      @saved="saved"
      @cancel="editing = false"
      @hosts="emit('hosts', $event)"
      @left="emit('left')"
    >
      <template v-if="$slots.danger" #default><slot name="danger" /></template>
    </EpisodeDetailsCard>
    <template v-else>
      <div class="top">
        <div class="cover ep-cover">
          <img
            v-if="cover"
            :src="cover"
            :srcset="coverSrcset"
            sizes="140px"
            :alt="`Cover of ${showName}`"
          />
          <BoardIcon v-else name="image" :size="40" />
        </div>
        <div class="text">
          <p class="when">{{ when }}</p>
          <h1 class="title">{{ show.title }}</h1>
          <p v-if="description" class="desc">{{ description }}</p>
          <div class="meta">
            <span :class="['pill', 'status-banner', pill.tone]"
              ><span class="dot" aria-hidden="true"></span>{{ pill.text }}</span
            >
            <span class="chip">{{ mode }}</span>
            <span v-if="hostCount" class="hosts"
              ><BoardIcon :name="hostCount > 1 ? 'users' : 'user'" :size="16" />{{
                hostCount > 1 ? 'Hosts' : 'Host'
              }}: {{ hostsText }}</span
            >
          </div>
        </div>
        <BaseButton
          v-if="canEdit"
          variant="secondary"
          class="edit"
          :disabled="!!locked"
          @click="editing = true"
        >
          <EpisodeIcon :name="locked ? 'lock' : 'edit'" />{{ locked ? `Edit · ${locked}` : 'Edit' }}
        </BaseButton>
        <span v-else class="read-only"
          ><BoardIcon name="eye" :size="16" />Read only · only its hosts can edit</span
        >
      </div>
    </template>
  </section>
</template>

<style scoped>
.ep-head {
  border: 1px solid #2a2a42;
  border-radius: var(--radius-xl);
  background: var(--color-surface);
  overflow: hidden;
}

.ep-head.editing {
  border: 2px solid var(--color-primary);
}

.top {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 20px 28px;
  padding: var(--spacing-lg);
}

.ep-cover {
  width: 140px;
  height: 140px;
}

.text {
  flex: 1 1 380px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.when {
  margin: 0;
  font: 700 15px var(--font-family);
  color: #d0d0dc;
}

.title {
  margin: 0;
  overflow-wrap: anywhere;
  font: 700 2.5rem/1.08 var(--font-family);
  color: #fff;
}

.desc {
  margin: 0;
  font-size: var(--font-size-md);
  color: #c8c8d4;
}

.meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  margin-top: 4px;
}

/* The status line as a compact pill. */
.meta .status-banner {
  display: inline-flex;
  flex-wrap: nowrap;
  gap: 8px;
  padding: 6px 12px;
  border: 1px solid #3478f6;
  border-radius: var(--radius-lg);
  background: rgba(52, 120, 246, 0.18);
  color: #a3c4ff;
  font: 700 12px/1.2 var(--font-ui);
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.meta .status-banner.live {
  border-color: #ef4444;
  background: rgba(239, 68, 68, 0.18);
  color: #ffb3ab;
}

.meta .status-banner.warn {
  border-color: #f59e0b;
  background: rgba(245, 158, 11, 0.16);
  color: #fbbf24;
}

.meta .status-banner.ok {
  border-color: #34c759;
  background: rgba(52, 199, 89, 0.16);
  color: #6ee79a;
}

.chip {
  display: inline-flex;
  align-items: center;
  padding: 6px 10px;
  border: 1px solid #55556a;
  border-radius: var(--radius-lg);
  color: #c4c4cc;
  font: 700 12px/1.2 var(--font-ui);
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.hosts {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  color: #d0d0dc;
}

.hosts svg,
.read-only svg {
  color: #a0a0b4;
}

.read-only {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: #a0a0b4;
  font-size: var(--font-size-sm);
  font-weight: 600;
}

@media (max-width: 640px) {
  .top {
    gap: 12px 14px;
    padding: var(--spacing-md);
  }

  .ep-cover {
    width: 88px;
    height: 88px;
  }

  .text {
    flex-basis: 0;
    flex-grow: 1;
    min-width: 160px;
  }

  .when {
    font-size: 13px;
  }

  .title {
    font-size: 1.625rem;
  }

  .edit {
    width: 100%;
  }
}
</style>
