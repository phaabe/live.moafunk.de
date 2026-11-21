<script setup lang="ts">
import { computed, reactive, ref } from 'vue';
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
  SLOT_NAMES,
} from '../promotion';
import BoardIcon from './BoardIcon.vue';
import EpisodeCard from './episode/EpisodeCard.vue';
import EpisodeIcon from './episode/EpisodeIcon.vue';
import type { EpisodePhase, Status, StepState, Tone } from './episode/phase';

// Social media card of the episode page (step 1 Set up social media, #1052):
// the three Instagram slots on a timeline. Each starts as "Not set up"; nothing goes
// out until a host sets it up in the post widget (`compose`). A set-up slot
// shows a small picture of the post, a sent one its link. Other hosts see the
// status, links and the pictures of sent posts, without buttons.

type Row = Announcement | AnnouncementStatus;

const props = defineProps<{
  show: ShowDetail;
  phase: EpisodePhase;
  /** Where the Set up social media step is. */
  state: StepState;
  announcements: Row[];
  /** An admin has paused all posting. */
  paused?: boolean;
  /** An admin turned Instagram off: nothing can be set up or edited. */
  instagramOff?: boolean;
  /** Another host's episode: status, links and sent pictures only. */
  readOnly?: boolean;
}>();
const emit = defineEmits<{ changed: []; compose: [slot: AnnouncementSlot] }>();

const SLOTS = ['pre', 'onair', 'post'] as const;
const FORMAT: Record<AnnouncementSlot, string> = {
  pre: 'Instagram feed post',
  onair: 'Instagram story',
  post: 'Instagram feed post',
};
/** What a slot that is not set up says. */
const NOT_SET_UP: Record<AnnouncementSlot, string> = {
  pre: 'before the show',
  onair: 'at go-live',
  post: 'after the show, once the track is public',
};
const LOWER: Record<AnnouncementSlot, string> = {
  pre: 'pre-announcement',
  onair: 'on-air story',
  post: 'post-announcement',
};

const busy = ref(false);
const error = ref<string | null>(null);
/** Pictures that did not load: the row shows its frame instead. */
const broken = reactive(new Set<AnnouncementSlot>());

const trackPublic = computed(() => !!props.show.soundcloud_public);
const upcoming = computed(() => props.show.episode_status === 'upcoming' && !props.show.on_air);

/** The post slot waits until the SoundCloud track is public. */
const waits = (slot: AnnouncementSlot, a: Row | undefined) =>
  slot === 'post' &&
  !!a &&
  !trackPublic.value &&
  ['scheduled', 'armed', 'waiting_soundcloud'].includes(a.state);

const cancelled = (slot: AnnouncementSlot) =>
  props.announcements.some((x) => x.slot === slot && x.state === 'cancelled');

/** A slot that can still be set up now. */
function open(slot: AnnouncementSlot): boolean {
  if (slot === 'post') return true;
  return upcoming.value;
}

function chip(slot: AnnouncementSlot, a: Row | undefined): Status {
  if (!a || a.state === 'draft') {
    return { tone: 'grey', text: cancelled(slot) ? 'Cancelled' : 'Not set up' };
  }
  if (a.state === 'sent') return { tone: 'green', text: 'Sent' };
  if (a.state === 'failed') return { tone: 'red', text: 'Failed' };
  if ((a.state as string) === 'rejected') return { tone: 'red', text: 'Rejected' };
  if (a.state === 'pending_approval') return { tone: 'amber', text: 'Needs approval' };
  if (waits(slot, a)) return { tone: 'amber', text: 'Waiting for SoundCloud' };
  return { tone: 'blue', text: 'Scheduled' };
}

/** The line under the slot name: its format, then its time or what happens next. */
function detail(slot: AnnouncementSlot, a: Row | undefined): string {
  const format = FORMAT[slot];
  const at = a?.scheduled_at ? postTime(a.scheduled_at) : '';
  if (!a || a.state === 'draft' || (a.state as string) === 'rejected') {
    if (cancelled(slot)) return `${format} · cancelled, not sent`;
    if (!open(slot)) return `${format} · not sent`;
    return `${format} · ${NOT_SET_UP[slot]}`;
  }
  if (a.state === 'sent') return `${format} · sent${a.sent_at ? ` ${postTime(a.sent_at)}` : ''}`;
  if (slot === 'onair') return `${format} · when you go live`;
  if (waits(slot, a))
    return at
      ? `${format} · ${at} · waits for the public track`
      : `${format} · waits for the public track`;
  if (a.state === 'pending_approval')
    return `${format}${at ? ` · ${at}` : ''} · an admin checks it first`;
  return at ? `${format} · ${at}` : format;
}

/** Version of the slot's picture, when this viewer may load it. */
function version(a: Row | undefined): string | null {
  if (!a || a.state === 'draft') return null;
  if ('updated_at' in a) return a.updated_at;
  return a.image_version ?? null;
}

const summary = computed<Status>(() => {
  const rows = SLOTS.map((slot) => ({ slot, a: inSlot(props.announcements, slot) }));
  const count = (test: (x: (typeof rows)[number]) => boolean) => rows.filter(test).length;
  const sent = count((x) => x.a?.state === 'sent');
  const failed = count((x) => x.a?.state === 'failed');
  const approval = count((x) => x.a?.state === 'pending_approval');
  const waiting = count((x) => waits(x.slot, x.a));
  const scheduled = count(
    (x) => !!x.a && ['scheduled', 'armed'].includes(x.a.state) && !waits(x.slot, x.a)
  );
  if (failed) return { tone: 'red', text: `${failed} failed` };
  if (approval) return { tone: 'amber', text: `${approval} needs approval` };
  const parts = [
    sent && `${sent} sent`,
    scheduled && `${scheduled} scheduled`,
    waiting && `${waiting} waiting`,
  ].filter(Boolean);
  if (!parts.length) return { tone: 'grey', text: 'Nothing set up' };
  const tone: Tone = waiting ? 'amber' : scheduled ? 'blue' : 'green';
  return { tone, text: parts.join(' · ') };
});

/** The story can be changed until go-live. */
const onairLocked = computed(() => !upcoming.value);

async function cancel(slot: AnnouncementSlot) {
  if (!window.confirm(`Cancel the ${LOWER[slot]}?`)) return;
  busy.value = true;
  error.value = null;
  try {
    await announcementsApi.cancel(props.show.id, slot);
    emit('changed');
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not cancel the announcement';
  } finally {
    busy.value = false;
  }
}

interface Action {
  label: string;
  /** A "Set up" button: yellow outline. */
  setup?: boolean;
  off: boolean;
  run: () => void;
}

/** Set up, or Edit and Cancel, of a slot. */
function actions(slot: AnnouncementSlot, a: Row | undefined): Action[] {
  if (props.readOnly) return [];
  const name = LOWER[slot];
  const locked = slot === 'onair' && onairLocked.value;
  if (editable(a)) {
    return [
      {
        label: `Edit ${name}`,
        off: busy.value || !!props.instagramOff || locked,
        run: () => emit('compose', slot),
      },
      { label: `Cancel ${name}`, off: busy.value || locked, run: () => cancel(slot) },
    ];
  }
  if (a?.state === 'sent' || !open(slot)) return [];
  return [
    {
      label: `Set up ${name}`,
      setup: true,
      off: busy.value || !!props.instagramOff,
      run: () => emit('compose', slot),
    },
  ];
}

/** The three slot rows as the card shows them. */
const rows = computed(() =>
  SLOTS.map((slot) => {
    const a = inSlot(props.announcements, slot);
    const c = chip(slot, a);
    const v = version(a);
    const acts = actions(slot, a);
    const todo = !a || a.state === 'draft';
    const link = a?.state === 'sent' ? (a.permalink ?? null) : null;
    const failure =
      a && a.state === 'failed' && 'error' in a
        ? a.error || 'Instagram did not take the post.'
        : null;
    const moved = a && 'requested_at' in a ? movedNote(a) : null;
    return {
      slot,
      name: SLOT_NAMES[slot],
      detail: detail(slot, a),
      chip: c,
      picture:
        v && !broken.has(slot) ? announcementsApi.imageUrl(props.show.id, slot, v, 'thumb') : null,
      /** Without a picture: yellow with a plus when the host can set it up now. */
      frame: !todo ? 'empty' : acts.length ? 'todo' : cancelled(slot) ? 'off' : 'empty',
      dot: dotOf(c.tone, todo, acts.length > 0),
      dim: todo && !acts.length && cancelled(slot),
      link,
      error: failure,
      moved,
      actions: acts,
    };
  })
);

type DotIcon = 'check' | 'clock' | 'close' | 'plus' | null;
const DOT_ICONS: Record<Tone, DotIcon> = {
  green: 'check',
  blue: 'clock',
  amber: 'clock',
  orange: 'clock',
  red: 'close',
  grey: 'close',
};

/** The timeline dot: the chip's colour; yellow with a plus for a slot to set up. */
function dotOf(tone: Tone, todo: boolean, canSetUp: boolean): { tone: string; icon: DotIcon } {
  if (todo) return canSetUp ? { tone: 'todo', icon: 'plus' } : { tone: 'empty', icon: null };
  return { tone, icon: DOT_ICONS[tone] };
}
</script>

<template>
  <EpisodeCard
    title="Social media"
    short="Social"
    :step="1"
    :state="state"
    :status="summary"
    class="promotion"
  >
    <template #icon><EpisodeIcon name="instagram" /></template>

    <template v-if="!readOnly">
      <p v-if="instagramOff" class="notice">{{ INSTAGRAM_OFF_NOTE }}</p>
      <p v-if="paused" class="notice">{{ PAUSED_NOTE }}</p>
    </template>
    <ol class="slots">
      <!-- Name and chip are direct children: the flow check reads "name | chip". -->
      <li v-for="(row, i) in rows" :key="row.slot" :class="['slot', row.slot, { dim: row.dim }]">
        <span class="vis" aria-hidden="true">
          <span class="rail">
            <span :class="['dot', row.dot.tone]"
              ><BoardIcon v-if="row.dot.icon" :name="row.dot.icon" :size="13"
            /></span>
            <span v-if="i < rows.length - 1" class="line"></span>
          </span>
          <span :class="['pic', row.slot === 'onair' ? 'story' : 'feed']">
            <img v-if="row.picture" :src="row.picture" alt="" @error="broken.add(row.slot)" />
            <span v-else :class="['frame', row.frame]"
              ><BoardIcon v-if="row.frame === 'todo'" name="plus" :size="16"
            /></span>
          </span>
        </span>
        <span class="slt"
          ><b>{{ row.name }}</b
          ><span class="sep"> · </span><span>{{ row.detail }}</span></span
        >
        <span :class="['ep-status', row.chip.tone]">{{ row.chip.text }}</span>
        <span v-if="row.error" class="slot-note bad">{{ row.error }}</span>
        <span v-if="row.moved" class="slot-note">{{ row.moved }}</span>
        <a v-if="row.link" class="lnk" :href="row.link" target="_blank" rel="noopener"
          >{{ row.slot === 'onair' ? 'View story' : 'View post' }}<EpisodeIcon name="external"
        /></a>
        <span v-if="row.actions.length" class="slot-actions">
          <BaseButton
            v-for="act in row.actions"
            :key="act.label"
            variant="secondary"
            :class="{ setup: act.setup }"
            :disabled="act.off"
            @click="act.run"
            >{{ act.label }}</BaseButton
          >
        </span>
      </li>
    </ol>
    <p class="foot">Instagram posts go out on @moafunk_radio.</p>
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
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: start;
  /* No row gap: the picture column spans spare empty rows (see .vis). */
  column-gap: 12px;
  padding-bottom: 20px;
}

.slot > .slot-note,
.slot > .lnk,
.slot > .slot-actions {
  margin-top: 6px;
}

.slot:last-child {
  padding-bottom: 0;
}

/* Dot, line and picture: the left column of every line of the row. */
.vis {
  display: flex;
  gap: 12px;
  align-self: stretch;
  /* More rows than a slot ever has; empty ones take no space. */
  grid-row: 1 / span 8;
}

/* The line runs on to the next slot's dot. */
.slot:not(:last-child) .vis {
  margin-bottom: -20px;
}

/* The timeline: a dot per slot, joined by a line. */
.rail {
  display: flex;
  flex: none;
  flex-direction: column;
  align-items: center;
  width: 24px;
}

.dot {
  display: flex;
  flex: none;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  box-sizing: border-box;
  border: 1.5px solid #55556a;
  border-radius: 50%;
  background: rgba(142, 142, 147, 0.14);
  color: #c4c4cc;
}

.dot.green {
  border-color: #34c759;
  background: rgba(52, 199, 89, 0.16);
  color: #6ee79a;
}

.dot.blue {
  border-color: #3478f6;
  background: rgba(52, 120, 246, 0.18);
  color: #a3c4ff;
}

.dot.amber {
  border-color: #f59e0b;
  background: rgba(245, 158, 11, 0.16);
  color: #fbbf24;
}

.dot.red {
  border-color: #ef4444;
  background: rgba(239, 68, 68, 0.18);
  color: #ffb3ab;
}

.dot.todo {
  border-color: var(--color-primary);
  background: rgba(255, 236, 68, 0.1);
  color: var(--color-primary);
}

.dot.empty {
  border-style: dashed;
  border-color: #3a3a55;
  background: transparent;
}

.line {
  flex: 1;
  width: 2px;
  min-height: 16px;
  margin-top: 6px;
  border-radius: 1px;
  background: #2a2a42;
}

/* The post picture in its real shape: feed 4:5, story 9:16. */
.pic {
  display: flex;
  flex: none;
  justify-content: center;
  width: 48px;
}

.pic img,
.frame {
  width: 48px;
  height: 60px;
  box-sizing: border-box;
  border-radius: 6px;
  object-fit: cover;
}

.pic.story img,
.pic.story .frame {
  width: 34px;
}

.frame {
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1.5px dashed #3a3a55;
}

.frame.todo {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.frame.off {
  border-style: solid;
  border-color: #2a2a42;
}

.slt {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.slot-note,
.lnk,
.slot-actions {
  grid-column: 2 / 4;
}

.slt b {
  font-size: 15px;
  color: #fff;
}

.slt span {
  color: #a8a8bc;
  font-size: var(--font-size-sm);
}

/* Read as one line by the flow check, shown as two. */
.slt .sep {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
}

.dim .slt b,
.dim .pic {
  color: #a8a8bc;
  opacity: 0.6;
}

.lnk {
  justify-self: start;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 44px;
  margin: -8px 0 -10px;
  color: var(--color-primary);
  font-weight: 700;
  font-size: var(--font-size-sm);
  text-decoration: none;
}

.slot-note {
  color: #c4c4cc;
  font-size: var(--font-size-sm);
}

.slot-note.bad {
  color: #ffb3ab;
}

.slot-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
  padding-top: 4px;
}

.host-page .slot-actions :deep(button.base-button.setup) {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.foot {
  margin: 0;
  padding-top: 14px;
  border-top: 1px solid #1e1e34;
  color: #a8a8bc;
  font-size: 13px;
}

.notice {
  margin: 0;
  padding: 12px 14px;
  border: 1px solid #6b5a1e;
  border-radius: var(--radius-lg);
  color: #f3d77a;
}

/* Phones: the chip goes under the name, so the name keeps its width. */
@media (max-width: 640px) {
  .slot {
    grid-template-columns: auto minmax(0, 1fr);
  }

  .slot > .ep-status {
    justify-self: start;
    margin-top: 6px;
  }

  .slot-note,
  .lnk,
  .slot-actions {
    grid-column: 2;
  }
}
</style>
