<script setup lang="ts">
import { computed } from 'vue';
import type { GuestCredentials, ShowDetail } from '../../../api';
import { BaseButton } from '@shared/components';
import { VueDatePicker } from '@vuepic/vue-datepicker';
import '@vuepic/vue-datepicker/dist/main.css';
import DurationField from '../../DurationField.vue';

/**
 * Air-date + assigned-host tile pair, extracted verbatim from ShowDetailPage's
 * external/brunchtime dashboard.
 *
 * The datetime range and host-assignment form state stay on the owning
 * page/shell (they feed the combined Save flow and the API handlers there);
 * this panel renders them via v-model pairs and emits the assignment actions.
 */
const props = defineProps<{
  show: ShowDetail;
  /** Whether the combined dashboard edit mode is active. */
  editMode: boolean;
  /** Whether the viewer may (re)assign the host. */
  canEditHost: boolean;
  /** Host assignment request in flight. */
  assigningHost: boolean;
  /** Credentials of a freshly created guest host (shown once). */
  guestCreds: GuestCredentials | null;
  /** Datetime editing state (from useDateTimeRange on the page). */
  editStart: Date | null;
  /** Show length in minutes; the end is derived from start + duration. */
  editDuration: number;
  editTimeValid: boolean;
  editTimeError: string | null;
  /** Host-assignment form state. */
  selectedHostId: number | null;
  hostEditMode: 'existing' | 'guest';
  newGuestUsername: string;
}>();

const emit = defineEmits<{
  'update:editStart': [value: Date | null];
  'update:editDuration': [value: number];
  'update:selectedHostId': [value: number | null];
  'update:hostEditMode': [value: 'existing' | 'guest'];
  'update:newGuestUsername': [value: string];
  /** Assign the selected existing user as host. */
  'assign-host': [];
  /** Create a one-off guest with the entered username and assign them. */
  'create-guest': [];
  /** Remove the current host. */
  'unassign-host': [];
}>();

const hasHost = computed(() => !!props.show.host_user_id);

const hostInitials = computed(() => {
  const name = props.show.host_username;
  if (!name) return '?';
  return name
    .split(/\s+/)
    .map((p) => p[0])
    .slice(0, 2)
    .join('')
    .toUpperCase();
});

const airDateLabel = computed(() => {
  if (!props.show.date) return '—';
  return new Date(props.show.date + 'T12:00:00').toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
});

function durationLabel(min: number): string {
  const h = Math.floor(min / 60);
  const m = min % 60;
  if (h && m) return `${h}h ${m}m`;
  if (h) return `${h}h`;
  return `${m}m`;
}

/** Read-only air time as "start · duration" (duration inferred from start/end). */
const airTimeRange = computed(() => {
  const start = props.show.start_time;
  if (!start) return '';
  if (!props.show.end_time) return start;
  const [sh, sm] = start.split(':').map(Number);
  const [eh, em] = props.show.end_time.split(':').map(Number);
  let mins = eh * 60 + em - (sh * 60 + sm);
  if (mins <= 0) mins += 24 * 60; // overnight
  return `${start} · ${durationLabel(mins)}`;
});
</script>

<template>
  <div class="card info-card">
    <div class="info-block">
      <h2 class="section-title"><span class="ico">📅</span> Air date</h2>
      <template v-if="editMode">
        <div class="edit-row edit-row-datetime">
          <div class="datetime-field">
            <label class="form-label">Start</label>
            <VueDatePicker
              :model-value="props.editStart"
              :enable-time-picker="true"
              :dark="true"
              :minutes-increment="5"
              :flow="{ steps: ['calendar', 'time'] }"
              :action-row="{
                showCancel: false,
                showPreview: false,
                selectBtnLabel: 'Confirm',
              }"
              placeholder="Start date & time"
              text-input
              teleport="body"
              @update:model-value="emit('update:editStart', $event)"
            />
          </div>
          <div class="datetime-field">
            <label class="form-label">Duration</label>
            <DurationField
              :model-value="props.editDuration"
              @update:model-value="emit('update:editDuration', $event)"
            />
          </div>
        </div>
        <p v-if="props.editStart && !props.editTimeValid" class="field-error">
          {{ props.editTimeError }}
        </p>
      </template>
      <template v-else>
        <p class="tile-value">{{ airDateLabel }}</p>
        <p class="tile-sub">{{ airTimeRange }}</p>
      </template>
    </div>

    <div class="info-divider"></div>

    <div class="info-block">
      <h2 class="section-title"><span class="ico">👤</span> Assigned host</h2>
      <div v-if="hasHost" class="host-row">
        <span class="host-avatar">{{ hostInitials }}</span>
        <div>
          <p class="tile-value">{{ show.host_username }}</p>
          <p class="tile-sub">Host</p>
        </div>
      </div>
      <p v-else class="empty-state">No host assigned.</p>

      <!-- Admins and the show's creator may (re)assign the host inline,
           either to an existing user or to a freshly created guest. -->
      <div v-if="canEditHost && editMode" class="host-edit">
        <div class="host-edit-toggle">
          <button
            type="button"
            :class="['toggle-chip', { active: props.hostEditMode === 'existing' }]"
            @click="emit('update:hostEditMode', 'existing')"
          >
            Existing user
          </button>
          <button
            type="button"
            :class="['toggle-chip', { active: props.hostEditMode === 'guest' }]"
            @click="emit('update:hostEditMode', 'guest')"
          >
            Create guest
          </button>
        </div>

        <template v-if="props.hostEditMode === 'existing'">
          <template v-if="show.available_hosts && show.available_hosts.length > 0">
            <select
              :value="props.selectedHostId"
              class="select-input"
              @change="
                emit(
                  'update:selectedHostId',
                  ($event.target as HTMLSelectElement).value
                    ? Number(($event.target as HTMLSelectElement).value)
                    : null
                )
              "
            >
              <option :value="null" disabled>
                {{ hasHost ? '-- Reassign host --' : '-- Select a host --' }}
              </option>
              <option v-for="h in show.available_hosts" :key="h.id" :value="h.id">
                {{ h.username }}
              </option>
            </select>
            <BaseButton
              variant="success"
              size="sm"
              :disabled="!props.selectedHostId || assigningHost"
              :loading="assigningHost"
              @click="emit('assign-host')"
            >
              {{ hasHost ? 'Reassign' : 'Assign' }}
            </BaseButton>
            <BaseButton v-if="hasHost" variant="ghost" size="sm" @click="emit('unassign-host')">
              Remove
            </BaseButton>
          </template>
          <p v-else class="text-muted assign-note">No assignable users available.</p>
        </template>

        <template v-else>
          <input
            :value="props.newGuestUsername"
            type="text"
            class="select-input"
            placeholder="Guest username"
            autocomplete="off"
            @input="emit('update:newGuestUsername', ($event.target as HTMLInputElement).value)"
          />
          <BaseButton
            variant="success"
            size="sm"
            :disabled="!props.newGuestUsername.trim() || assigningHost"
            :loading="assigningHost"
            @click="emit('create-guest')"
          >
            Create &amp; assign
          </BaseButton>
          <p class="text-muted assign-note">
            The guest can only log in on the show date and is deleted afterwards.
          </p>
          <p v-if="guestCreds" class="guest-creds">
            Username <code>{{ guestCreds.username }}</code> · one-time password
            <code>{{ guestCreds.password }}</code> (shown once)
          </p>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.info-card {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
}

.info-divider {
  height: 1px;
  background: var(--color-border);
}

.section-title {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
  margin: 0 0 var(--spacing-md);
  font-size: var(--font-size-sm);
  font-weight: 700;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}

.tile-value {
  margin: 0;
  font-size: var(--font-size-xl);
  font-weight: 700;
  color: var(--color-text);
}

.tile-sub {
  margin: 2px 0 0;
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.host-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-md);
}

.host-avatar {
  flex: 0 0 auto;
  width: 44px;
  height: 44px;
  display: grid;
  place-items: center;
  border-radius: var(--radius-full);
  background: var(--color-success-bg);
  color: var(--color-success);
  font-weight: 700;
}

.edit-row {
  display: flex;
  gap: var(--spacing-sm);
  align-items: center;
  flex-wrap: wrap;
}

.edit-row-datetime {
  flex-direction: column;
  align-items: stretch;
}

.datetime-field {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.field-error {
  color: var(--color-error);
  font-size: var(--font-size-sm);
  margin: 0;
}

.host-edit {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--spacing-sm);
  margin-top: var(--spacing-md);
}

.host-edit-toggle {
  display: flex;
  gap: var(--spacing-xs);
  flex-basis: 100%;
}

.toggle-chip {
  padding: 4px 12px;
  background: var(--color-surface-alt);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text-muted);
  font-family: var(--font-family);
  font-size: var(--font-size-sm);
  cursor: pointer;
}

.toggle-chip.active {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.guest-creds {
  flex-basis: 100%;
  margin: 0;
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.guest-creds code {
  font-family: monospace;
  background: var(--color-surface-alt);
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  color: var(--color-text);
}

.select-input {
  flex: 1;
  padding: 8px 10px;
  background: #ffec44;
  border: 1px solid #111;
  border-radius: var(--radius-md);
  color: #111;
  font-size: 1em;
  font-family: var(--font-family);
}

.select-input:focus {
  outline: none;
  border-color: #111;
  box-shadow: 0 0 0 2px rgba(255, 236, 68, 0.35);
}

.assign-note {
  margin-bottom: var(--spacing-md);
}

.empty-state {
  color: var(--color-text-muted);
  font-style: italic;
  padding: var(--spacing-md) 0;
}
</style>
