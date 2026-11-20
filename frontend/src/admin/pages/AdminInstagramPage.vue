<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { RouterLink } from 'vue-router';
import { BaseButton } from '@shared/components';
import {
  instagramAdminApi,
  type AnnouncementSettings,
  type ApprovalMode,
  type InstagramStatus,
  type QueuedAnnouncement,
} from '../api';
import {
  parseSlots,
  rowMeta,
  rowWhen,
  statusDetail,
  statusPill,
  usage,
  weekOf,
} from '../instagramQueue';
import { getBerlinNowISO } from '../showTime';

// Flow screen `admin-instagram` (admin at /admin/instagram): every queued
// Instagram announcement of this week, the account this server posts to and
// the station policy.

const today = getBerlinNowISO().slice(0, 10);
const week = weekOf(today);

const status = ref<InstagramStatus | null>(null);
const settings = ref<AnnouncementSettings | null>(null);
const rows = ref<QueuedAnnouncement[]>([]);
const error = ref('');
const busy = ref(false);

/** The policy form; slots as the text the admin types. */
const form = ref({
  limit_per_episode: 0,
  feed_per_day: 0,
  feed_per_week: 0,
  stories_per_day: 0,
  pre_days_before: 0,
  post_days_after: 0,
  slots: '',
  approval_mode: 'first_two' as ApprovalMode,
});
const formError = ref('');
const saving = ref(false);

const NUMBERS = [
  ['limit_per_episode', 'Announcements per episode'],
  ['feed_per_day', 'Feed posts per day'],
  ['feed_per_week', 'Feed posts per week'],
  ['stories_per_day', 'Stories per day'],
  ['pre_days_before', 'Pre-announcement days before'],
  ['post_days_after', 'Post-announcement days after'],
] as const;

const MODES: { value: ApprovalMode; label: string }[] = [
  { value: 'first_two', label: 'Approve the first 2 posts of a new host' },
  { value: 'always', label: 'Approve always' },
  { value: 'never', label: 'Never approve' },
];

const pill = computed(() => status.value && statusPill(status.value, new Date()));
const detail = computed(() => (status.value ? statusDetail(status.value) : ''));
const used = computed(() => usage(rows.value, today));

function fillForm(s: AnnouncementSettings) {
  settings.value = s;
  form.value = {
    limit_per_episode: s.limit_per_episode,
    feed_per_day: s.feed_per_day,
    feed_per_week: s.feed_per_week,
    stories_per_day: s.stories_per_day,
    pre_days_before: s.pre_days_before,
    post_days_after: s.post_days_after,
    slots: s.slots.join(', '),
    approval_mode: s.approval_mode,
  };
}

onMounted(async () => {
  try {
    const [s, p, list] = await Promise.all([
      instagramAdminApi.status(),
      instagramAdminApi.getSettings(),
      instagramAdminApi.list(week.from, week.to),
    ]);
    status.value = s;
    fillForm(p);
    rows.value = list;
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not load the queue';
  }
});

async function act(row: QueuedAnnouncement, action: 'approve' | 'reject' | 'retry') {
  busy.value = true;
  error.value = '';
  try {
    const updated = await instagramAdminApi.act(row.id, action);
    rows.value = rows.value.map((r) => (r.id === updated.id ? updated : r));
  } catch (e) {
    error.value = e instanceof Error ? e.message : `Could not ${action} the post`;
  } finally {
    busy.value = false;
  }
}

async function togglePause() {
  if (!settings.value) return;
  busy.value = true;
  error.value = '';
  try {
    // Only the switch changes; unsaved policy edits stay in the form.
    const saved = await instagramAdminApi.saveSettings({
      ...settings.value,
      paused: !settings.value.paused,
    });
    settings.value = saved;
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not change posting';
  } finally {
    busy.value = false;
  }
}

function validate(): string {
  for (const [key, label] of NUMBERS) {
    const n = form.value[key];
    if (!Number.isInteger(n) || n < 0) return `${label}: enter a whole number, 0 or more.`;
  }
  if (form.value.limit_per_episode > 3) return 'Announcements per episode: at most 3.';
  if (!parseSlots(form.value.slots)) return 'Slots: enter times like 12:00, 18:00.';
  return '';
}

async function savePolicy() {
  if (!settings.value) return;
  formError.value = validate();
  if (formError.value) return;
  saving.value = true;
  try {
    fillForm(
      await instagramAdminApi.saveSettings({
        ...settings.value,
        ...form.value,
        slots: parseSlots(form.value.slots)!,
      })
    );
  } catch (e) {
    formError.value = e instanceof Error ? e.message : 'Could not save the policy';
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div class="instagram-page">
    <div class="head">
      <h1 class="page-title">Instagram queue</h1>
      <BaseButton
        v-if="settings"
        :variant="settings.paused ? 'primary' : 'danger'"
        :disabled="busy"
        @click="togglePause"
        >{{ settings.paused ? 'Resume posting' : 'Pause all posting' }}</BaseButton
      >
    </div>
    <p v-if="pill" class="pill status-banner" :class="pill.tone">{{ pill.text }}</p>
    <p v-if="detail" class="muted">{{ detail }}</p>
    <p v-if="settings?.paused" class="paused">All posting is paused. Nothing is sent.</p>
    <p v-if="error" class="field-error" role="alert">{{ error }}</p>

    <div class="board-grid">
      <section class="board-card">
        <div class="board-card-head">
          <h2 class="board-card-title">{{ week.title }}</h2>
          <span v-if="settings" class="muted">
            Feed posts {{ used.feed }} / {{ settings.feed_per_week }} · stories today
            {{ used.stories }} / {{ settings.stories_per_day }}
          </span>
        </div>
        <div class="board-card-body">
          <p v-if="!rows.length" class="muted">No announcements this week.</p>
          <ul v-else class="board-rows">
            <li v-for="row in rows" :key="row.id" class="queue-row" :class="row.state">
              <RouterLink :to="`/shows/${row.show_id}`" class="board-row">
                <span>{{ rowWhen(row) }}</span>
                <span class="muted">{{ rowMeta(row) }}</span>
              </RouterLink>
              <div class="actions">
                <template v-if="row.state === 'pending_approval'">
                  <BaseButton size="sm" :disabled="busy" @click="act(row, 'approve')"
                    >Approve</BaseButton
                  >
                  <BaseButton
                    size="sm"
                    variant="secondary"
                    :disabled="busy"
                    @click="act(row, 'reject')"
                    >Reject</BaseButton
                  >
                </template>
                <BaseButton
                  v-else-if="row.state === 'failed'"
                  size="sm"
                  variant="secondary"
                  :disabled="busy"
                  @click="act(row, 'retry')"
                  >Retry</BaseButton
                >
                <a v-else-if="row.permalink" :href="row.permalink" target="_blank" rel="noopener"
                  >Open</a
                >
              </div>
            </li>
          </ul>
        </div>
      </section>

      <section class="board-card">
        <div class="board-card-head">
          <h2 class="board-card-title">Policy</h2>
        </div>
        <form class="board-card-body" novalidate @submit.prevent="savePolicy">
          <label v-for="[key, label] in NUMBERS" :key="key" class="field">
            <span class="label">{{ label }}</span>
            <input
              v-model.number="form[key]"
              type="number"
              min="0"
              step="1"
              class="input"
              :name="key"
            />
          </label>
          <label class="field">
            <span class="label">Slots (Berlin time)</span>
            <input v-model="form.slots" type="text" class="input" name="slots" />
          </label>
          <fieldset class="modes">
            <legend class="legend">Approval</legend>
            <label v-for="mode in MODES" :key="mode.value">
              <input
                v-model="form.approval_mode"
                type="radio"
                name="approval"
                :value="mode.value"
              />
              {{ mode.label }}
            </label>
          </fieldset>
          <p v-if="formError" class="field-error" role="alert">{{ formError }}</p>
          <div>
            <BaseButton type="submit" :loading="saving" :disabled="!settings"
              >Save policy</BaseButton
            >
          </div>
        </form>
      </section>
    </div>
  </div>
</template>

<style scoped>
.instagram-page {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
}

.head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-md);
}

.page-title {
  margin: 0;
}

.muted,
.legend {
  margin: 0;
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.paused {
  margin: 0;
  color: var(--color-warning);
  font-weight: var(--font-weight-bold);
}

.queue-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--spacing-sm);
}

.queue-row .board-row {
  flex: 1 1 320px;
}

.queue-row.pending_approval .board-row {
  border-color: var(--color-warning);
}

.queue-row.failed .board-row {
  border-color: var(--color-error);
}

.actions {
  display: flex;
  gap: var(--spacing-sm);
}

.field {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.label {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.input {
  background-color: var(--color-surface-alt);
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-family: var(--font-family);
  padding: var(--spacing-sm) var(--spacing-md);
}

.modes {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
  margin: 0;
  padding: 0;
  border: 0;
}

.modes label {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  min-height: 36px;
}

.field-error {
  color: var(--color-error);
  font-size: var(--font-size-sm);
  margin: 0;
}
</style>
