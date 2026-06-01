import { ref, computed, readonly } from 'vue';
import {
  showsApi,
  showTemplatesApi,
  usersApi,
  guestsApi,
  type Show,
  type ShowTemplate,
  type AdminUser,
  type GuestCredentials,
} from '../api';
import { useDateTimeRange } from './useDateTimeRange';

// ─────────────────────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────────────────────

export type WizardMode = 'existing' | 'new';

export type StreamMode = 'live' | 'prerecorded';

export type WizardStep =
  | 'choice'
  | 'select'
  | 'name'
  | 'cover'
  | 'description'
  | 'date'
  | 'assign'
  | 'guest'
  | 'stream-mode'
  | 'confirm';

// ─────────────────────────────────────────────────────────────────────────────
// Singleton state (shared across the wizard's step components)
// ─────────────────────────────────────────────────────────────────────────────

const isAdmin = ref(false);
const mode = ref<WizardMode | null>(null);

// Existing-template branch
const templates = ref<ShowTemplate[]>([]);
const templatesLoading = ref(false);
const selectedTemplateId = ref<number | null>(null);

// New-template branch
const newName = ref('');
const newDescription = ref('');
const coverFile = ref<File | null>(null);
const coverPreviewUrl = ref<string | null>(null);

// Date / time (start + end)
const range = useDateTimeRange();

// Assignee (admin only)
const assignableUsers = ref<AdminUser[]>([]);
const assigneeLoading = ref(false);
const assigneeUserId = ref<number | null>(null);

// Guest account (optional — created during show setup, login limited to show date)
const guestUsername = ref('');
const guestCredentials = ref<GuestCredentials | null>(null);

// Delivery mode (live vs prerecorded upload)
const streamMode = ref<StreamMode | null>(null);

// Navigation
const stepIndex = ref(0);
const maxVisited = ref(0);
const submitting = ref(false);

// ─────────────────────────────────────────────────────────────────────────────
// Derived step machine
// ─────────────────────────────────────────────────────────────────────────────

/**
 * The ordered steps for the current branch + role. `choice` is always first;
 * admins get an extra `assign` step before `confirm`.
 */
const steps = computed<WizardStep[]>(() => {
  const out: WizardStep[] = ['choice'];
  if (mode.value === 'new') {
    out.push('name', 'cover', 'description');
  } else if (mode.value === 'existing') {
    out.push('select');
  }
  if (mode.value) {
    out.push('date');
    if (isAdmin.value) out.push('assign');
    out.push('guest', 'stream-mode', 'confirm');
  }
  return out;
});

const currentStep = computed<WizardStep>(() => steps.value[stepIndex.value] ?? 'choice');
const isFirstStep = computed(() => stepIndex.value === 0);
const isLastStep = computed(() => stepIndex.value === steps.value.length - 1);

/** Whether the current step is complete enough to advance. */
const canProceed = computed(() => {
  switch (currentStep.value) {
    case 'choice':
      return mode.value !== null;
    case 'select':
      return selectedTemplateId.value !== null;
    case 'name':
      return newName.value.trim().length > 0;
    case 'cover':
      return true; // optional
    case 'description':
      return true; // optional
    case 'date':
      return range.isValid.value;
    case 'assign':
      return assigneeUserId.value !== null;
    case 'guest':
      return true; // optional
    case 'stream-mode':
      return streamMode.value !== null;
    case 'confirm':
      return true;
    default:
      return false;
  }
});

/** A step is reachable by direct navigation only if already visited. */
function canNavigateTo(index: number): boolean {
  return index >= 0 && index < steps.value.length && index <= maxVisited.value;
}

/** True once the user has at least one saved template (drives the choice gate). */
const hasTemplates = computed(() => templates.value.length > 0);

/**
 * Jump to a named step (used by the confirm page's per-row "Edit" affordances).
 * Only succeeds if that step exists in the current branch and has been visited.
 */
function goToNamedStep(step: WizardStep): boolean {
  const index = steps.value.indexOf(step);
  if (index === -1) return false;
  return goToStep(index);
}

// ─────────────────────────────────────────────────────────────────────────────
// Summary for the confirm step
// ─────────────────────────────────────────────────────────────────────────────

const selectedTemplate = computed(() =>
  templates.value.find((t) => t.id === selectedTemplateId.value)
);

const summaryName = computed(() =>
  mode.value === 'new' ? newName.value.trim() : (selectedTemplate.value?.name ?? '')
);
const summaryDescription = computed(() =>
  mode.value === 'new' ? newDescription.value.trim() : (selectedTemplate.value?.description ?? '')
);
const summaryCoverUrl = computed(() =>
  mode.value === 'new' ? coverPreviewUrl.value : (selectedTemplate.value?.cover_url ?? null)
);
const assigneeUsername = computed(
  () => assignableUsers.value.find((u) => u.id === assigneeUserId.value)?.username ?? null
);

const streamModeLabel = computed(() => {
  if (streamMode.value === 'live') return 'Live';
  if (streamMode.value === 'prerecorded') return 'Pre-recorded upload';
  return null;
});

const summaryGuest = computed(() => guestUsername.value.trim() || null);

// ─────────────────────────────────────────────────────────────────────────────
// Actions
// ─────────────────────────────────────────────────────────────────────────────

function setMode(m: WizardMode): void {
  if (mode.value === m) return;
  mode.value = m;
  // Switching branch invalidates any forward progress; force a re-walk.
  maxVisited.value = stepIndex.value;
}

function setStreamMode(m: StreamMode): void {
  streamMode.value = m;
}

function goNext(): boolean {
  if (!canProceed.value) return false;
  if (stepIndex.value >= steps.value.length - 1) return false;
  stepIndex.value += 1;
  if (stepIndex.value > maxVisited.value) maxVisited.value = stepIndex.value;
  return true;
}

function goBack(): boolean {
  if (stepIndex.value === 0) return false;
  stepIndex.value -= 1;
  return true;
}

function goToStep(index: number): boolean {
  if (!canNavigateTo(index)) return false;
  stepIndex.value = index;
  return true;
}

function setCover(file: File | null): void {
  if (coverPreviewUrl.value) URL.revokeObjectURL(coverPreviewUrl.value);
  coverFile.value = file;
  coverPreviewUrl.value = file ? URL.createObjectURL(file) : null;
}

async function loadTemplates(): Promise<void> {
  templatesLoading.value = true;
  try {
    const res = await showTemplatesApi.list();
    templates.value = res.templates;
  } finally {
    templatesLoading.value = false;
  }
}

async function loadAssignableUsers(): Promise<void> {
  assigneeLoading.value = true;
  try {
    const res = await usersApi.list();
    // Mirror the backend's "available hosts": host + admin users.
    assignableUsers.value = res.users.filter((u) => u.role === 'host' || u.role === 'admin');
  } finally {
    assigneeLoading.value = false;
  }
}

/** Create the show (template + optional guest, if requested). Returns the created show. */
async function submit(): Promise<Show> {
  submitting.value = true;
  try {
    let templateId: number | undefined;
    let title: string;
    let description: string | undefined;

    if (mode.value === 'new') {
      const created = await showTemplatesApi.create({
        name: newName.value.trim(),
        description: newDescription.value.trim() || undefined,
      });
      templateId = created.id;
      if (coverFile.value) {
        await showTemplatesApi.uploadCover(created.id, coverFile.value);
      }
      title = newName.value.trim();
      description = newDescription.value.trim() || undefined;
    } else {
      const tpl = selectedTemplate.value;
      if (!tpl) throw new Error('No template selected');
      templateId = tpl.id;
      title = tpl.name;
      description = tpl.description || undefined;
    }

    const show = await showsApi.create({
      title,
      description,
      show_type: 'external',
      date: range.apiDate.value,
      start_time: range.apiStartTime.value,
      end_time: range.apiEndTime.value,
      stream_mode: streamMode.value ?? 'live',
      template_id: templateId,
      host_user_id: isAdmin.value ? (assigneeUserId.value ?? undefined) : undefined,
    });

    // Optionally create a date-restricted guest account for this show's date.
    const guest = guestUsername.value.trim();
    if (guest) {
      guestCredentials.value = await guestsApi.create({
        username: guest,
        login_date: range.apiDate.value,
      });
    }

    return show;
  } finally {
    submitting.value = false;
  }
}

/** Reset all state and (re)initialise the wizard for a fresh run. */
function start(opts: { isAdmin: boolean; prefillDate?: string }): void {
  isAdmin.value = opts.isAdmin;
  mode.value = null;
  templates.value = [];
  templatesLoading.value = false;
  selectedTemplateId.value = null;
  newName.value = '';
  newDescription.value = '';
  setCover(null);
  range.reset();
  assignableUsers.value = [];
  assigneeLoading.value = false;
  assigneeUserId.value = null;
  guestUsername.value = '';
  guestCredentials.value = null;
  streamMode.value = null;
  stepIndex.value = 0;
  maxVisited.value = 0;
  submitting.value = false;

  // Prefill the calendar day (e.g. from the calendar's "+" buttons) with a
  // sensible default 20:00–22:00 window the user can adjust.
  if (opts.prefillDate) {
    range.setFromApi(opts.prefillDate, '20:00', '22:00');
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Composable
// ─────────────────────────────────────────────────────────────────────────────

export function useShowWizard() {
  return {
    // State
    isAdmin: readonly(isAdmin),
    mode: readonly(mode),
    templates: readonly(templates),
    templatesLoading: readonly(templatesLoading),
    selectedTemplateId,
    newName,
    newDescription,
    coverFile: readonly(coverFile),
    coverPreviewUrl: readonly(coverPreviewUrl),
    startDateTime: range.startDateTime,
    endDateTime: range.endDateTime,
    rangeValid: range.isValid,
    rangeError: range.validationError,
    assignableUsers: readonly(assignableUsers),
    assigneeLoading: readonly(assigneeLoading),
    assigneeUserId,
    guestUsername,
    guestCredentials: readonly(guestCredentials),
    streamMode: readonly(streamMode),
    submitting: readonly(submitting),

    // Step machine
    steps,
    stepIndex: readonly(stepIndex),
    maxVisited: readonly(maxVisited),
    currentStep,
    isFirstStep,
    isLastStep,
    canProceed,
    canNavigateTo,
    hasTemplates,
    goToNamedStep,

    // Summary
    summaryName,
    summaryDescription,
    summaryCoverUrl,
    assigneeUsername,
    streamModeLabel,
    summaryGuest,

    // Actions
    start,
    setMode,
    setStreamMode,
    goNext,
    goBack,
    goToStep,
    setCover,
    loadTemplates,
    loadAssignableUsers,
    submit,
  };
}
