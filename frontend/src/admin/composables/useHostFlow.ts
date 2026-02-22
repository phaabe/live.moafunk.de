import { ref, computed, readonly } from 'vue';
import { hostFlowApi, type MyShowInfo, type UploadResult } from '../api';

// ─────────────────────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────────────────────

export type FlowStep =
  | 'not-assigned'
  | 'select'
  | 'info'
  | 'mode'
  | 'upload'
  | 'confirm'
  | 'live'
  | 'waiting'
  | 'streaming';

export type LiveSubStep = 'os-select' | 'tutorial' | 'test';

export type SelectedOs = 'windows' | 'macos' | 'linux';

export type UploadMode = 'prerecorded' | 'live';

export interface UploadProgress {
  phase: 'uploading' | 'finalizing';
  percent: number;
  chunkIndex?: number;
  totalChunks?: number;
}

export interface HostFlowState {
  /** Whether the initial fetch has completed */
  loaded: boolean;
  /** Whether a fetch is in progress */
  loading: boolean;
  /** Error message from last operation */
  error: string | null;
  /** Whether the user is assigned to at least one show */
  assigned: boolean;
  /** All shows assigned to the user */
  shows: MyShowInfo[];
  /** Currently selected show */
  show: MyShowInfo | undefined;
  /** Current flow step */
  currentStep: FlowStep;
  /** Chosen upload mode */
  uploadMode: UploadMode | null;
  /** Whether an upload is in progress */
  uploading: boolean;
  /** Upload progress info */
  uploadProgress: UploadProgress | null;
}

// ─────────────────────────────────────────────────────────────────────────────
// Singleton state (shared across components, like useFlash)
// ─────────────────────────────────────────────────────────────────────────────

const loaded = ref(false);
const loading = ref(false);
const error = ref<string | null>(null);
const assigned = ref(false);
const shows = ref<MyShowInfo[]>([]);
const show = ref<MyShowInfo | undefined>(undefined);
const currentStep = ref<FlowStep>('select');
const uploadMode = ref<UploadMode | null>(null);
const uploading = ref(false);
const uploadProgress = ref<UploadProgress | null>(null);

// Live-specific state
const liveSubStep = ref<LiveSubStep>('os-select');
const liveTestPassed = ref(false);
const selectedOs = ref<SelectedOs | null>(null);
const showStarted = ref(false);
const recordStream = ref(false);

// ─────────────────────────────────────────────────────────────────────────────
// Computed
// ─────────────────────────────────────────────────────────────────────────────

const hasUpload = computed(() => !!show.value?.prerecorded_key);
const isConfirmed = computed(() => !!show.value?.prerecorded_confirmed_at);
const prerecordedUrl = computed(() => show.value?.prerecorded_url);
const prerecordedFilename = computed(() => show.value?.prerecorded_filename);
const showId = computed(() => show.value?.id);

/** Whether the user can navigate to a given step */
function canNavigateTo(step: FlowStep): boolean {
  if (!assigned.value) return step === 'not-assigned';
  switch (step) {
    case 'not-assigned':
      return false;
    case 'select':
      return true; // can always go back to show selection
    case 'info':
      return !!show.value;
    case 'mode':
      return !!show.value;
    case 'upload':
      return !!show.value && uploadMode.value === 'prerecorded';
    case 'confirm':
      return !!show.value && uploadMode.value === 'prerecorded' && hasUpload.value;
    case 'live':
      return !!show.value && uploadMode.value === 'live';
    case 'waiting':
      return (
        !!show.value &&
        ((uploadMode.value === 'prerecorded' && isConfirmed.value) ||
          (uploadMode.value === 'live' && liveTestPassed.value))
      );
    case 'streaming':
      return showStarted.value;
    default:
      return false;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Actions
// ─────────────────────────────────────────────────────────────────────────────

async function fetchMyShow(): Promise<void> {
  loading.value = true;
  error.value = null;

  try {
    const response = await hostFlowApi.getMyShows();
    assigned.value = response.assigned;
    shows.value = response.shows;

    if (!response.assigned || response.shows.length === 0) {
      currentStep.value = 'not-assigned';
      show.value = undefined;
    } else if (response.shows.length === 1 && !loaded.value) {
      // Auto-select if only one show
      selectShow(response.shows[0]);
    } else if (!show.value && !loaded.value) {
      // Multiple shows, no selection yet → go to select
      currentStep.value = 'select';
    }

    loaded.value = true;
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Failed to load show data';
  } finally {
    loading.value = false;
  }
}

/** Select a show and determine the starting step based on its state */
function selectShow(selectedShow: MyShowInfo): void {
  show.value = selectedShow;

  if (selectedShow.prerecorded_confirmed_at) {
    currentStep.value = 'confirm';
    uploadMode.value = 'prerecorded';
  } else if (selectedShow.prerecorded_key) {
    currentStep.value = 'confirm';
    uploadMode.value = 'prerecorded';
  } else {
    currentStep.value = 'info';
  }
}

/** Go back to show selection (clears selected show) */
function deselectShow(): void {
  show.value = undefined;
  uploadMode.value = null;
  currentStep.value = 'select';
  // Reset flow-specific state
  liveSubStep.value = 'os-select';
  liveTestPassed.value = false;
  selectedOs.value = null;
  showStarted.value = false;
  recordStream.value = false;
}

function goToStep(step: FlowStep): boolean {
  if (!canNavigateTo(step)) return false;
  currentStep.value = step;
  return true;
}

function selectMode(mode: UploadMode): void {
  uploadMode.value = mode;
  if (mode === 'prerecorded') {
    currentStep.value = 'upload';
  } else {
    currentStep.value = 'live';
  }
}

async function uploadFile(file: File): Promise<UploadResult> {
  if (!showId.value) throw new Error('No show selected');

  uploading.value = true;
  uploadProgress.value = null;
  error.value = null;

  try {
    const result = await hostFlowApi.uploadFile(showId.value, file, (progress) => {
      uploadProgress.value = progress;
    });

    // Update local show state
    if (show.value) {
      show.value = {
        ...show.value,
        prerecorded_key: result.key,
        prerecorded_url: result.prerecorded_url,
        prerecorded_filename: result.filename,
        prerecorded_confirmed_at: undefined,
      };
    }

    currentStep.value = 'confirm';
    return result;
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Upload failed';
    throw err;
  } finally {
    uploading.value = false;
    uploadProgress.value = null;
  }
}

async function confirmUpload(): Promise<void> {
  if (!showId.value) throw new Error('No show selected');
  error.value = null;

  try {
    const result = await hostFlowApi.confirm(showId.value);

    if (show.value) {
      show.value = {
        ...show.value,
        prerecorded_confirmed_at: result.confirmed_at,
      };
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Confirmation failed';
    throw err;
  }
}

async function deleteUpload(): Promise<void> {
  if (!showId.value) throw new Error('No show selected');
  error.value = null;

  try {
    await hostFlowApi.deleteUpload(showId.value);

    if (show.value) {
      show.value = {
        ...show.value,
        prerecorded_key: undefined,
        prerecorded_url: undefined,
        prerecorded_filename: undefined,
        prerecorded_confirmed_at: undefined,
      };
    }

    currentStep.value = 'upload';
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Delete failed';
    throw err;
  }
}

function setLiveSubStep(step: LiveSubStep): void {
  liveSubStep.value = step;
}

function setLiveTestPassed(passed = true): void {
  liveTestPassed.value = passed;
}

function setSelectedOs(os: SelectedOs): void {
  selectedOs.value = os;
}

function setShowStarted(started = true): void {
  showStarted.value = started;
}

function setRecordStream(record: boolean): void {
  recordStream.value = record;
}

function reset(): void {
  loaded.value = false;
  loading.value = false;
  error.value = null;
  assigned.value = false;
  shows.value = [];
  show.value = undefined;
  currentStep.value = 'select';
  uploadMode.value = null;
  uploading.value = false;
  uploadProgress.value = null;
  liveSubStep.value = 'os-select';
  liveTestPassed.value = false;
  selectedOs.value = null;
  showStarted.value = false;
  recordStream.value = false;
}

// ─────────────────────────────────────────────────────────────────────────────
// Composable
// ─────────────────────────────────────────────────────────────────────────────

export function useHostFlow() {
  return {
    // State (readonly refs)
    loaded: readonly(loaded),
    loading: readonly(loading),
    error: readonly(error),
    assigned: readonly(assigned),
    shows: readonly(shows),
    show: readonly(show),
    currentStep: readonly(currentStep),
    uploadMode: readonly(uploadMode),
    uploading: readonly(uploading),
    uploadProgress: readonly(uploadProgress),
    liveSubStep: readonly(liveSubStep),
    liveTestPassed: readonly(liveTestPassed),
    selectedOs: readonly(selectedOs),
    showStarted: readonly(showStarted),
    recordStream: readonly(recordStream),

    // Computed
    hasUpload,
    isConfirmed,
    prerecordedUrl,
    prerecordedFilename,
    showId,

    // Actions
    fetchMyShow,
    selectShow,
    deselectShow,
    goToStep,
    selectMode,
    uploadFile,
    confirmUpload,
    deleteUpload,
    canNavigateTo,
    setLiveSubStep,
    setLiveTestPassed,
    setSelectedOs,
    setShowStarted,
    setRecordStream,
    reset,
  };
}
