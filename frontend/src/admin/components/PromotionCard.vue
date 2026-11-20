<script setup lang="ts">
import { computed, ref } from 'vue';
import { useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import {
  announcementsApi,
  type Announcement,
  type AnnouncementSlot,
  type AnnouncementStatus,
  type ShowDetail,
} from '../api';
import {
  editable,
  inSlot,
  INSTAGRAM_OFF_NOTE,
  movedNote,
  PAUSED_NOTE,
  postTime,
} from '../promotion';
import EpisodeCard from './episode/EpisodeCard.vue';
import EpisodeIcon from './episode/EpisodeIcon.vue';
import type { EpisodePhase, Status, Tone } from './episode/phase';

// Social media card of the episode page (all phases, #1052): the three
// announcement slots, each with its status and a link to the sent post. Pre
// opens the composer, on-air is an on/off switch, post is set up after the
// show. Other hosts see the status and links, without buttons.

type Row = Announcement | AnnouncementStatus;

const props = defineProps<{
  show: ShowDetail;
  phase: EpisodePhase;
  announcements: Row[];
  /** The Show's Instagram handle, without "@". */
  handle?: string | null;
  /** An admin has paused all posting. */
  paused?: boolean;
  /** An admin turned Instagram off: nothing can be set up. */
  instagramOff?: boolean;
  /** Another host's episode: status and links only. */
  readOnly?: boolean;
}>();
const emit = defineEmits<{ changed: [] }>();

const router = useRouter();
const busy = ref(false);
const error = ref<string | null>(null);

const trackPublic = computed(() => !!props.show.soundcloud_public);
const pre = computed(() => inSlot(props.announcements, 'pre'));
const onair = computed(() => inSlot(props.announcements, 'onair'));
const post = computed(() => inSlot(props.announcements, 'post'));

const NAMES: Record<AnnouncementSlot, string> = {
  pre: 'Pre · feed post 4:5',
  onair: 'On air · story 9:16',
  post: 'Post · feed post 4:5',
};

/** The post slot waits until the SoundCloud track is public. */
const waits = (slot: AnnouncementSlot, a: Row | undefined) =>
  slot === 'post' &&
  !!a &&
  !trackPublic.value &&
  ['scheduled', 'armed', 'waiting_soundcloud'].includes(a.state);

/** The status chip of a slot. */
function chip(slot: AnnouncementSlot): Status {
  const a = inSlot(props.announcements, slot);
  if (!a || a.state === 'draft') {
    if (props.announcements.some((x) => x.slot === slot && x.state === 'cancelled'))
      return { tone: 'grey', text: 'Cancelled' };
    return { tone: 'grey', text: slot === 'onair' ? 'Off' : 'Not set up' };
  }
  if (a.state === 'sent') return { tone: 'green', text: 'Sent' };
  if (a.state === 'failed') return { tone: 'red', text: 'Failed' };
  if ((a.state as string) === 'rejected') return { tone: 'red', text: 'Rejected' };
  if (a.state === 'pending_approval') return { tone: 'amber', text: 'Needs approval' };
  if (waits(slot, a)) return { tone: 'amber', text: 'Waiting for SoundCloud' };
  return { tone: 'blue', text: 'Scheduled' };
}

/** The line under a slot's name: its time, or what happens next. */
function detail(slot: AnnouncementSlot): string {
  const a = inSlot(props.announcements, slot);
  const at = a?.scheduled_at ? postTime(a.scheduled_at) : '';
  if (a?.state === 'sent') return a.sent_at ? postTime(a.sent_at) : '';
  if (a?.state === 'failed') {
    const why = 'error' in a ? a.error : null;
    return why || 'Instagram did not take the post.';
  }
  if (slot === 'onair') return 'Sent when you go live';
  if (!a || a.state === 'draft' || (a.state as string) === 'rejected') {
    if (slot === 'post') {
      return props.phase === 'post' ? 'Sent when the track is public' : 'Set up after the show';
    }
    return props.phase === 'pre' ? 'Before the show' : 'Not sent';
  }
  if (waits(slot, a))
    return at ? `${at} · waits for the public track` : 'Waits for the public track';
  return at;
}

const moved = (a: Row | undefined) => (a && 'requested_at' in a ? movedNote(a) : null);

const summary = computed<Status>(() => {
  const list = (['pre', 'onair', 'post'] as const).map((s) => ({
    slot: s,
    a: inSlot(props.announcements, s),
  }));
  const count = (test: (x: (typeof list)[number]) => boolean) => list.filter(test).length;
  const sent = count((x) => x.a?.state === 'sent');
  const failed = count((x) => x.a?.state === 'failed');
  const waiting = count((x) => waits(x.slot, x.a));
  const planned = count(
    (x) =>
      !!x.a && ['scheduled', 'armed', 'pending_approval'].includes(x.a.state) && !waits(x.slot, x.a)
  );
  let tone: Tone = 'grey';
  let text = 'Not set up';
  if (failed) [tone, text] = ['red', `${failed} failed`];
  else if (sent && waiting) [tone, text] = ['amber', `${sent} sent · ${waiting} waiting`];
  else if (sent) [tone, text] = ['green', `${sent} of 3 sent`];
  else if (planned + waiting) [tone, text] = ['blue', `${planned + waiting} scheduled`];
  return { tone, text };
});

const onairOn = computed(() => !!onair.value && onair.value.state !== 'draft');
/** The story can be switched off until go-live. */
const onairLocked = computed(
  () =>
    busy.value ||
    props.instagramOff ||
    !!props.show.on_air ||
    props.show.episode_status !== 'upcoming' ||
    onair.value?.state === 'sent'
);

const composer = (slot: 'pre' | 'post') => router.push(`/shows/${props.show.id}/announce/${slot}`);

async function run(action: () => Promise<unknown>, failed: string) {
  busy.value = true;
  error.value = null;
  try {
    await action();
    emit('changed');
  } catch (e) {
    error.value = e instanceof Error ? e.message : failed;
  } finally {
    busy.value = false;
  }
}

function cancel(slot: 'pre' | 'post') {
  const name = slot === 'pre' ? 'pre-announcement' : 'post-announcement';
  if (!window.confirm(`Cancel the ${name}?`)) return;
  run(() => announcementsApi.cancel(props.show.id, slot), 'Could not cancel the announcement');
}

async function toggleOnair(event: Event) {
  const box = event.target as HTMLInputElement;
  const on = box.checked;
  await run(
    () =>
      on
        ? announcementsApi.save(props.show.id, 'onair', {
            headline: '',
            caption: '',
            image_source: 'cover',
            layout: 'band',
            // A story takes no collaborator.
            collaborator: false,
          })
        : announcementsApi.cancel(props.show.id, 'onair'),
    'Could not change the on-air story'
  );
  // A refused change must not leave the box showing the wrong state.
  box.checked = onairOn.value;
}

/** Set up, Edit and Cancel of a feed post slot. */
function actions(slot: 'pre' | 'post') {
  const a = slot === 'pre' ? pre.value : post.value;
  if (props.readOnly) return [];
  if (slot === 'post' && props.phase !== 'post') return [];
  const name = slot === 'pre' ? 'pre-announcement' : 'post-announcement';
  if (editable(a)) {
    return [
      { label: `Edit ${name}`, off: busy.value || !!props.instagramOff, run: () => composer(slot) },
      { label: `Cancel ${name}`, off: busy.value, run: () => cancel(slot) },
    ];
  }
  if (a?.state === 'sent') return [];
  const closed = slot === 'pre' && props.show.episode_status !== 'upcoming';
  return [
    {
      label: `Set up ${name}`,
      off: busy.value || !!props.instagramOff || closed,
      run: () => composer(slot),
    },
  ];
}

/** The three slot rows as the card shows them. */
const rows = computed(() =>
  (['pre', 'onair', 'post'] as const).map((slot) => {
    const a = inSlot(props.announcements, slot);
    return {
      slot,
      name: NAMES[slot],
      detail: detail(slot),
      chip: chip(slot),
      link: a?.state === 'sent' ? (a.permalink ?? null) : null,
      moved: moved(a),
      actions: slot === 'onair' ? [] : actions(slot),
    };
  })
);
</script>

<template>
  <EpisodeCard
    title="Social media"
    short="Social"
    phase="All phases"
    :status="summary"
    class="promotion"
  >
    <template #icon><EpisodeIcon name="instagram" /></template>

    <p class="ep-lead">
      Posted by @moafunk_radio.
      <template v-if="handle">@{{ handle }} is invited as collaborator.</template>
    </p>
    <template v-if="!readOnly">
      <p v-if="instagramOff" class="notice">{{ INSTAGRAM_OFF_NOTE }}</p>
      <p v-if="paused" class="notice">{{ PAUSED_NOTE }}</p>
    </template>
    <ul class="slots">
      <li v-for="row in rows" :key="row.slot" :class="['slot', row.slot]">
        <label v-if="row.slot === 'onair' && !readOnly" class="check slot-main">
          <input type="checkbox" :checked="onairOn" :disabled="onairLocked" @change="toggleOnair" />
          <span class="slt"
            ><b>{{ row.name }}</b
            >{{ ' ' }}<span>{{ row.detail }}</span></span
          >
        </label>
        <span v-else class="slt slot-main"
          ><b>{{ row.name }}</b
          >{{ ' ' }}<span v-if="row.detail">{{ row.detail }}</span></span
        >
        <span :class="['ep-status', row.chip.tone]">{{ row.chip.text }}</span>
        <a v-if="row.link" class="lnk" :href="row.link" target="_blank" rel="noopener"
          >{{ row.slot === 'onair' ? 'View story' : 'View post' }}<EpisodeIcon name="external"
        /></a>
        <span v-if="row.moved" class="slot-note">{{ row.moved }}</span>
        <span v-if="row.actions.length" class="slot-actions">
          <BaseButton
            v-for="act in row.actions"
            :key="act.label"
            variant="secondary"
            :disabled="act.off"
            @click="act.run"
            >{{ act.label }}</BaseButton
          >
        </span>
      </li>
    </ul>
    <p v-if="error" class="field-error" role="alert">{{ error }}</p>
  </EpisodeCard>
</template>

<style scoped>
.slots {
  display: flex;
  flex-direction: column;
  margin: 0;
  padding: 0;
  list-style: none;
}

.slot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 12px;
  padding: 12px 0;
  border-top: 1px solid #1e1e34;
}

.slot:first-child {
  padding-top: 0;
  border-top: 0;
}

.slot-main {
  flex: 1 1 200px;
  min-width: 0;
}

.slt {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.check .slt {
  font-weight: 400;
}

.slt b {
  font-size: 15px;
  color: #fff;
}

.slt span {
  color: #a8a8bc;
  font-size: var(--font-size-sm);
}

.lnk {
  color: var(--color-primary);
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 44px;
  font-weight: 700;
  font-size: var(--font-size-sm);
  text-decoration: none;
}

.slot-note {
  flex-basis: 100%;
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.slot-actions {
  display: flex;
  flex-basis: 100%;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
}

.notice {
  margin: 0;
  padding: 12px 14px;
  border: 1px solid #6b5a1e;
  border-radius: var(--radius-lg);
  color: #f3d77a;
}
</style>
