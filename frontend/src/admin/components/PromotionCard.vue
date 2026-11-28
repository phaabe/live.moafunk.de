<script setup lang="ts">
import { computed, reactive, ref, useId } from 'vue';
import { BaseButton, BaseModal } from '@shared/components';
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
  NOT_READY_NOTE,
  PAUSED_NOTE,
  postTime,
  PUBLIC_TRACK_NOTE,
  SLOT_NAMES,
  unsetNote,
  untilText,
} from '../promotion';
import BoardIcon from './BoardIcon.vue';
import BookTime from './BookTime.vue';
import EpisodeCard from './episode/EpisodeCard.vue';
import EpisodeIcon from './episode/EpisodeIcon.vue';
import { episodeTimes, type EpisodePhase, type StepState, type Tone } from './episode/phase';

// Social media card of the episode page (step 1, #1052, #1091): one tile per
// Instagram slot in time order (Before, On air, After). Each tile shows its
// picture, one status line, when it goes out and one button. The GOES OUT box
// books or changes the time of a feed post, also before the post is set up;
// the post itself is set up in the post widget (`compose`). Other hosts see
// the same tiles without buttons, and pictures only of sent posts.

type Row = Announcement | AnnouncementStatus;

const props = defineProps<{
  show: ShowDetail;
  phase: EpisodePhase;
  /** Where the Set up social media step is. */
  state: StepState;
  announcements: Row[];
  /** An admin has paused all posting. */
  paused?: boolean;
  /** An admin turned Instagram off: nothing can be set up, edited or booked. */
  instagramOff?: boolean;
  /** Another host's episode: status, links and sent pictures only. */
  readOnly?: boolean;
}>();
const emit = defineEmits<{ changed: []; compose: [slot: AnnouncementSlot] }>();

const SLOTS = ['pre', 'onair', 'post'] as const;
const LABEL: Record<AnnouncementSlot, string> = { pre: 'Before', onair: 'On air', post: 'After' };
const ICON: Partial<Record<Tone, 'check' | 'clock' | 'close'>> = {
  green: 'check',
  blue: 'clock',
  amber: 'clock',
  red: 'close',
};

const uid = useId();
const busy = ref(false);
/** Pictures that did not load: the tile shows its frame instead. */
const broken = reactive(new Set<AnnouncementSlot>());
/** The feed slot whose time the host is booking (the Book a time popup). */
const booking = ref<'pre' | 'post' | null>(null);
const bookError = ref<string | null>(null);

const trackPublic = computed(() => !!props.show.soundcloud_public);
const upcoming = computed(() => props.show.episode_status === 'upcoming' && !props.show.on_air);
/** The episode start as an instant, for the on-air story and a closed pre slot. */
const start = computed(() => {
  const ms = episodeTimes(props.show).startMs;
  return ms === null ? null : new Date(ms).toISOString();
});

const cancelled = (slot: AnnouncementSlot) =>
  props.announcements.some((x) => x.slot === slot && x.state === 'cancelled');

/** A slot that can still be set up or booked: pre and the story until the show starts. */
const open = (slot: AnnouncementSlot) => slot === 'post' || upcoming.value;

/** Nothing to send yet: no row, or a time booked before the post (`draft`). */
const notSetUp = (a: Row | undefined) => !a || a.state === 'draft';

function status(slot: AnnouncementSlot, a: Row | undefined): { tone: Tone; text: string } {
  if (notSetUp(a)) {
    if (a?.scheduled_at && open(slot)) return { tone: 'amber', text: 'Booked · post not ready' };
    if (!open(slot)) return { tone: 'grey', text: 'Not sent' };
    return { tone: 'grey', text: cancelled(slot) ? 'Cancelled' : 'Not set up' };
  }
  switch (a!.state as string) {
    case 'sent':
      return { tone: 'green', text: 'Sent' };
    case 'failed':
      return { tone: 'red', text: 'Failed' };
    case 'rejected':
      return { tone: 'red', text: 'Rejected' };
    case 'pending_approval':
      return { tone: 'amber', text: 'Needs approval' };
    case 'waiting_soundcloud':
      return { tone: 'amber', text: 'Ready · no time booked' };
    default:
      return { tone: 'blue', text: 'Scheduled' };
  }
}

/** The GOES OUT box: book, change, or a fixed time (story, sent, closed). */
type Box =
  | { kind: 'book' }
  | { kind: 'change'; at: string; rel: string }
  | { kind: 'fixed'; head: string; at: string; rel: string };

function box(slot: AnnouncementSlot, a: Row | undefined): Box | null {
  if (a?.state === 'sent')
    return { kind: 'fixed', head: 'Went out', at: a.sent_at ? postTime(a.sent_at) : '', rel: '' };
  if (slot === 'onair')
    return start.value
      ? { kind: 'fixed', head: 'Goes out', at: postTime(start.value), rel: 'when you go live' }
      : null;
  if (!open(slot))
    return start.value
      ? { kind: 'fixed', head: 'Closed', at: postTime(start.value), rel: 'at your show start' }
      : null;
  const at = a?.scheduled_at;
  // A failed post is fixed in the post widget, not by a new time.
  const canBook = !props.readOnly && a?.state !== 'failed';
  if (at && canBook) return { kind: 'change', at: postTime(at), rel: untilText(at) };
  if (at) return { kind: 'fixed', head: 'Goes out', at: postTime(at), rel: untilText(at) };
  return canBook ? { kind: 'book' } : null;
}

interface Note {
  text: string;
  tone: 'amber' | 'red' | 'grey';
}

function notes(slot: AnnouncementSlot, a: Row | undefined): Note[] {
  const out: Note[] = [];
  if (!open(slot) && notSetUp(a) && slot === 'pre')
    out.push({ text: 'Only before the show. It can no longer go out.', tone: 'grey' });
  if (a && a.state === 'failed' && 'error' in a)
    out.push({ text: a.error || 'Instagram did not take the post.', tone: 'red' });
  if (props.readOnly || !open(slot)) return out;
  const unset = unsetNote(a);
  if (unset) out.push({ text: unset, tone: 'amber' });
  if (a?.state === 'draft' && a.scheduled_at) out.push({ text: NOT_READY_NOTE, tone: 'amber' });
  else if (
    slot === 'post' &&
    !trackPublic.value &&
    a?.scheduled_at &&
    ['scheduled', 'pending_approval'].includes(a.state)
  )
    out.push({ text: PUBLIC_TRACK_NOTE, tone: 'amber' });
  const moved = a && 'requested_at' in a ? movedNote(a) : null;
  if (moved) out.push({ text: moved, tone: 'grey' });
  return out;
}

/** Version of the slot's picture, when this viewer may load it. */
function version(a: Row | undefined): string | null {
  if (notSetUp(a)) return null;
  if ('updated_at' in a!) return a.updated_at;
  return a!.image_version ?? null;
}

interface Action {
  label: string;
  primary: boolean;
  off: boolean;
}

/** One button: Set up, or Edit. Sent posts get their link instead. */
function action(slot: AnnouncementSlot, a: Row | undefined): Action | null {
  if (props.readOnly || a?.state === 'sent') return null;
  // The story can be changed until go-live.
  const locked = slot === 'onair' && !upcoming.value;
  if (editable(a))
    return { label: 'Edit', primary: false, off: busy.value || !!props.instagramOff || locked };
  if (!open(slot)) return null;
  return { label: 'Set up', primary: true, off: busy.value || !!props.instagramOff };
}

const tiles = computed(() =>
  SLOTS.map((slot) => {
    const a = inSlot(props.announcements, slot);
    const v = version(a);
    const act = action(slot, a);
    const st = status(slot, a);
    return {
      slot,
      label: LABEL[slot],
      name: SLOT_NAMES[slot],
      status: st,
      icon: ICON[st.tone] ?? null,
      picture:
        v && !broken.has(slot) ? announcementsApi.imageUrl(props.show.id, slot, v, 'thumb') : null,
      /** Without a picture: yellow with a plus when the host can set it up now. */
      todo: !!act?.primary,
      dim: st.text === 'Not sent',
      box: box(slot, a),
      notes: notes(slot, a),
      link: a?.state === 'sent' ? (a.permalink ?? null) : null,
      action: act,
    };
  })
);

const bookable = computed(() =>
  booking.value ? inSlot(props.announcements, booking.value)?.scheduled_at : null
);

function openBooking(slot: AnnouncementSlot) {
  if (slot === 'onair') return;
  bookError.value = null;
  booking.value = slot;
}

async function book(value: string) {
  const slot = booking.value;
  if (!slot) return;
  busy.value = true;
  bookError.value = null;
  try {
    await announcementsApi.book(props.show.id, slot, value);
    booking.value = null;
    emit('changed');
  } catch (e) {
    // A 409 names the taken day or the limit.
    bookError.value = e instanceof Error ? e.message : 'Could not book the time';
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <EpisodeCard title="Social media" short="Social" :step="1" :state="state" class="promotion">
    <template #icon><EpisodeIcon name="instagram" /></template>

    <template v-if="!readOnly">
      <p v-if="instagramOff" class="notice">{{ INSTAGRAM_OFF_NOTE }}</p>
      <p v-if="paused" class="notice">{{ PAUSED_NOTE }}</p>
    </template>
    <p class="handle">Posts go out on @moafunk_radio.</p>
    <div class="tiles">
      <div
        v-for="t in tiles"
        :key="t.slot"
        :class="['tile', t.slot, { dim: t.dim }]"
        role="group"
        :aria-labelledby="`${uid}-${t.slot}`"
      >
        <span class="tile-label" aria-hidden="true">{{ t.label }}</span>
        <span :class="['pic', t.slot === 'onair' ? 'story' : 'feed']" aria-hidden="true">
          <img v-if="t.picture" :src="t.picture" alt="" @error="broken.add(t.slot)" />
          <span v-else :class="['frame', { todo: t.todo }]"
            ><BoardIcon v-if="t.todo" name="plus" :size="18"
          /></span>
        </span>
        <!-- Name and status are one row for the flow check: "name | status". -->
        <ul class="tile-lines">
          <li>
            <b :id="`${uid}-${t.slot}`">{{ t.name }}</b>
            <span :class="['tile-status', t.status.tone]"
              ><BoardIcon v-if="t.icon" :name="t.icon" :size="14" />{{ t.status.text }}</span
            >
          </li>
        </ul>
        <button
          v-if="t.box?.kind === 'book'"
          type="button"
          class="box book"
          :disabled="busy || instagramOff"
          @click="openBooking(t.slot)"
        >
          <BoardIcon name="calendar" :size="18" />Book a time
        </button>
        <button
          v-else-if="t.box?.kind === 'change'"
          type="button"
          class="box change"
          :disabled="busy || instagramOff"
          @click="openBooking(t.slot)"
        >
          <span class="box-head">Goes out</span><span class="sep"> | </span
          ><span class="box-at">{{ t.box.at }}</span
          ><span class="sep"> · </span><span class="box-rel">{{ t.box.rel }}</span>
        </button>
        <dl v-else-if="t.box?.kind === 'fixed'" class="box fixed">
          <div>
            <dt class="box-head">{{ t.box.head }}</dt>
            <dd>
              <span class="box-at">{{ t.box.at }}</span
              ><template v-if="t.box.rel"
                ><span class="sep"> · </span><span class="box-rel">{{ t.box.rel }}</span></template
              >
            </dd>
          </div>
        </dl>
        <p v-for="n in t.notes" :key="n.text" :class="['tile-note', n.tone]">{{ n.text }}</p>
        <span class="tile-action">
          <a v-if="t.link" class="lnk" :href="t.link" target="_blank" rel="noopener"
            >{{ t.slot === 'onair' ? 'View story' : 'View post' }}<EpisodeIcon name="external"
          /></a>
          <BaseButton
            v-else-if="t.action"
            variant="secondary"
            :class="{ setup: t.action.primary }"
            :disabled="t.action.off"
            :aria-describedby="`${uid}-${t.slot}`"
            @click="emit('compose', t.slot)"
            >{{ t.action.label }}</BaseButton
          >
        </span>
      </div>
    </div>

    <BaseModal :open="!!booking" size="lg" @close="booking = null">
      <div v-if="booking" class="host-page">
        <BookTime
          :show-id="show.id"
          :slot="booking"
          :show="show"
          :picked="bookable"
          :error="bookError"
          :busy="busy"
          @pick="book"
          @cancel="booking = null"
        />
      </div>
    </BaseModal>
  </EpisodeCard>
</template>

<style scoped>
.handle {
  margin: 0 0 var(--spacing-md);
  color: #a8a8bc;
  font-size: 13px;
}

.notice {
  margin: 0 0 var(--spacing-md);
  padding: 12px 14px;
  border: 1px solid #6b5a1e;
  border-radius: var(--radius-lg);
  color: #f3d77a;
}

/* Three tiles in time order; on phones they stack as rows. */
.tiles {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
}

.tile {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
  padding: 16px;
  border: 1px solid #2a2a46;
  border-radius: 12px;
  background: #15152e;
}

.tile.dim {
  opacity: 0.55;
}

.tile-label {
  color: #8e8ea4;
  font-family: var(--font-family-mono, 'Courier New', monospace);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}

/* The post picture in its real shape: feed 4:5, story 9:16. */
.pic {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 150px;
  border-radius: 8px;
  background: #1b1b38;
}

.pic img,
.frame {
  width: 120px;
  height: 150px;
  box-sizing: border-box;
  border-radius: 6px;
  object-fit: cover;
}

.pic.story img,
.pic.story .frame {
  width: 84px;
}

.frame {
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1.5px dashed #3a3a58;
}

.frame.todo {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.tile-lines {
  margin: 0;
  padding: 0;
  list-style: none;
}

.tile-lines li {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.tile-lines b {
  color: #fff;
  font-size: 16px;
}

.tile-status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}

.tile-status.green {
  color: #6ee79a;
}

.tile-status.blue {
  color: #a3c4ff;
}

.tile-status.amber {
  color: #fbbf24;
}

.tile-status.red {
  color: #ffb3ab;
}

.tile-status.grey {
  color: #a5a5b8;
}

/* GOES OUT: when the post goes out, large; a click books or changes it. */
.box {
  display: flex;
  flex-direction: column;
  gap: 4px;
  width: 100%;
  margin: 0;
  padding: 12px;
  box-sizing: border-box;
  border: 1px solid #34345a;
  border-radius: 10px;
  background: #1b1b38;
  color: #f2f2f7;
  font: inherit;
  text-align: left;
}

button.box {
  cursor: pointer;
}

button.box:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.box.fixed {
  border-color: #2a2a46;
}

.box.fixed div {
  display: contents;
}

.box dd {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
}

.box-head {
  display: flex;
  justify-content: space-between;
  color: #8e8ea4;
  font-family: var(--font-family-mono, 'Courier New', monospace);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}

/* Shown, not read: the flow check reads the box as "Goes out | <time>". */
.box.change .box-head::after {
  content: 'Change';
  color: var(--color-primary);
}

.box-at {
  font-size: 18px;
  font-weight: 600;
}

.box-rel {
  color: #a5a5b8;
  font-size: 13px;
}

.box.book {
  flex-direction: row;
  align-items: center;
  gap: 10px;
  min-height: 56px;
  border: 1.5px dashed var(--color-primary);
  background: transparent;
  color: var(--color-primary);
  font-size: 15px;
  font-weight: 600;
}

/* Read as one line by the flow check, shown as lines. */
.sep {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
}

.tile-note {
  margin: -4px 0 0;
  font-size: 13px;
}

.tile-note.amber {
  color: #fbbf24;
}

.tile-note.red {
  color: #ffb3ab;
}

.tile-note.grey {
  color: #a5a5b8;
}

.tile-action {
  display: flex;
  flex-direction: column;
  margin-top: auto;
}

.lnk {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-height: 44px;
  border: 1px solid #3a3a58;
  border-radius: 8px;
  color: var(--color-primary);
  font-weight: 700;
  font-size: var(--font-size-sm);
  text-decoration: none;
}

/* Set up: yellow outline. The page keeps one yellow button, in the current card. */
.host-page .tile-action :deep(button.base-button.setup) {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

/* Phones: one row per slot, picture on the left. */
@media (max-width: 720px) {
  .tiles {
    grid-template-columns: 1fr;
    gap: 12px;
  }

  .tile {
    display: grid;
    grid-template-columns: 72px minmax(0, 1fr);
    column-gap: 16px;
    row-gap: 8px;
    align-items: start;
    padding: 14px;
  }

  .tile > .pic {
    grid-row: 1 / span 7;
    height: 90px;
    background: none;
  }

  .tile > :not(.pic) {
    grid-column: 2;
  }

  .pic img,
  .frame {
    width: 72px;
    height: 90px;
  }

  .pic.story img,
  .pic.story .frame {
    width: 51px;
  }
}
</style>
