<script setup lang="ts">
import { computed, ref } from 'vue';
import { useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import { announcementsApi, type Announcement, type ShowDetail } from '../api';
import {
  editable,
  inSlot,
  INSTAGRAM_OFF_NOTE,
  movedNote,
  PAUSED_NOTE,
  slotState,
} from '../promotion';
import BoardIcon from './BoardIcon.vue';

// Episode page "Promotion · Instagram": the three announcement slots.
// Pre opens the composer, on-air is an on/off switch, post is set up in Wrap-up.

const props = defineProps<{
  show: ShowDetail;
  announcements: Announcement[];
  /** The Show's Instagram handle, without "@". */
  handle: string | null;
  /** An admin has paused all posting. */
  paused: boolean;
  /** An admin turned Instagram off: nothing can be set up. */
  instagramOff?: boolean;
}>();
const emit = defineEmits<{ changed: [] }>();

const router = useRouter();
const busy = ref(false);
const error = ref<string | null>(null);

const pre = computed(() => inSlot(props.announcements, 'pre'));
const onair = computed(() => inSlot(props.announcements, 'onair'));
const post = computed(() => inSlot(props.announcements, 'post'));

const onairOn = computed(() => !!onair.value && onair.value.state !== 'draft');
const onairMeta = computed(() =>
  onair.value?.state === 'sent' || onair.value?.state === 'failed'
    ? slotState(props.announcements, 'onair')
    : 'Sent when you go live'
);
/** The story can be switched off until go-live. */
const onairLocked = computed(
  () =>
    busy.value ||
    props.instagramOff ||
    !!props.show.on_air ||
    props.show.episode_status !== 'upcoming'
);

const composer = () => router.push(`/shows/${props.show.id}/announce/pre`);

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

function cancelPre() {
  if (!window.confirm('Cancel the pre-announcement?')) return;
  run(() => announcementsApi.cancel(props.show.id, 'pre'), 'Could not cancel the announcement');
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
</script>

<template>
  <section class="board-card promotion">
    <div class="board-card-head">
      <h2 class="board-card-title"><BoardIcon name="image" />Promotion · Instagram</h2>
    </div>
    <div class="board-card-body">
      <p class="intro">
        Posted by @moafunk_radio.
        <template v-if="handle">@{{ handle }} is invited as collaborator.</template>
      </p>
      <p v-if="instagramOff" class="notice">{{ INSTAGRAM_OFF_NOTE }}</p>
      <p v-if="paused" class="notice">{{ PAUSED_NOTE }}</p>
      <ul class="slots">
        <li class="slot">
          <span class="slot-name">Pre · feed post 4:5</span>
          <span class="slot-state">{{ slotState(announcements, 'pre') }}</span>
          <span v-if="movedNote(pre)" class="slot-note">{{ movedNote(pre) }}</span>
          <span v-if="pre?.permalink" class="slot-link">
            <a :href="pre.permalink" target="_blank" rel="noopener">Open post</a>
          </span>
          <span class="slot-actions">
            <template v-if="editable(pre)">
              <BaseButton variant="secondary" :disabled="busy || instagramOff" @click="composer"
                >Edit pre-announcement</BaseButton
              >
              <BaseButton variant="secondary" :disabled="busy" @click="cancelPre"
                >Cancel pre-announcement</BaseButton
              >
            </template>
            <BaseButton
              v-else-if="pre?.state !== 'sent'"
              variant="secondary"
              :disabled="busy || instagramOff || show.episode_status !== 'upcoming'"
              @click="composer"
              >Set up pre-announcement</BaseButton
            >
          </span>
        </li>
      </ul>
      <label class="check slot onair">
        <input
          type="checkbox"
          :checked="onairOn"
          :disabled="onairLocked || onair?.state === 'sent'"
          @change="toggleOnair"
        />
        <span class="slot-name">On air · story 9:16</span>{{ ' '
        }}<span class="slot-state">{{ onairMeta }}</span>
      </label>
      <ul class="slots">
        <li class="slot">
          <span class="slot-name">Post · feed post 4:5</span>
          <span class="slot-state">{{
            post
              ? slotState(announcements, 'post', !!show.soundcloud_public)
              : 'Set up in Wrap-up when the SoundCloud track is public'
          }}</span>
          <span v-if="movedNote(post)" class="slot-note">{{ movedNote(post) }}</span>
          <span v-if="post?.permalink" class="slot-link">
            <a :href="post.permalink" target="_blank" rel="noopener">Open post</a>
          </span>
        </li>
      </ul>
      <p v-if="error" class="field-error" role="alert">{{ error }}</p>
    </div>
  </section>
</template>

<style scoped>
.intro {
  margin: 0;
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.slots {
  margin: 0;
  padding: 0;
  list-style: none;
}

.slot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 12px;
  padding: 10px 0;
  border-top: 1px solid #2a2a42;
}

.slot-name {
  font-weight: 700;
}

.slot-state {
  flex: 1;
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  font-weight: 400;
}

.slot-note {
  flex-basis: 100%;
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.notice {
  margin: 0;
  padding: 12px 14px;
  border: 1px solid #6b5a1e;
  border-radius: var(--radius-lg);
  color: #f3d77a;
}

.slot-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
}

.slot-actions:empty {
  display: none;
}
</style>
