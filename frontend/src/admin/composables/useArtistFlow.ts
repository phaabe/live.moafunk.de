import { ref, computed, readonly } from 'vue';
import { artistFlowApi, type MyShowInfo, type UploadResult } from '../api';

// ─────────────────────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────────────────────

export type FlowStep = 'not-assigned' | 'info' | 'mode' | 'upload' | 'confirm' | 'live';

export type UploadMode = 'prerecorded' | 'live';

export interface UploadProgress {
  phase: 'uploading' | 'finalizing';
  percent: number;
  chunkIndex?: number;
  totalChunks?: number;
}

export interface ArtistFlowState {
  /** Whether the initial fetch has completed */
  loaded: boolean;
  /** Whether a fetch is in progress */
  loading: boolean;
  /** Error message from last operation */
  error: string | null;
  /** Whether the user is assigned to a show */
  assigned: boolean;
  /** Show data (undefined if not assigned) */
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
const show = ref<MyShowInfo | undefined>(undefined);
const currentStep = ref<FlowStep>('info');
const uploadMode = ref<UploadMode | null>(null);
const uploading = ref(false);
const uploadProgress = ref<UploadProgress | null>(null);

// ─────────────────────────────────────────────────────────────────────────────
// Computed
// ─────────────────────────────────────────────────────────────────────────────

const hasUpload = computed(() => !!show.value?.prerecorded_key);
const isConfirmed = computed(() => !!show.value?.prerecorded_confirmed_at);
const prerecordedUrl = computed(() => show.value?.prerecorded_url);
const prerecordedFilename = computed(() => show.value?.prerecorded_filename);

/** Whether the user can navigate to a given step */
function canNavigateTo(step: FlowStep): boolean {
  if (!assigned.value) return step === 'not-assigned';
  switch (step) {
    case 'not-assigned':
      return false;
    case 'info':
      return true;
    case 'mode':
      return true; // can always go back to mode selection
    case 'upload':
      return uploadMode.value === 'prerecorded';
    case 'confirm':
      return uploadMode.value === 'prerecorded' && hasUpload.value;
    case 'live':
      return uploadMode.value === 'live' || isConfirmed.value;
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
    const response = await artistFlowApi.getMyShow();
    assigned.value = response.assigned;
    show.value = response.show ?? undefined;

    if (!response.assigned) {
      currentStep.value = 'not-assigned';
    } else if (!loaded.value) {
      // First load: determine starting step based on show state
      if (show.value?.prerecorded_confirmed_at) {
        // Already confirmed → go to confirm (they can review or go live)
        currentStep.value = 'confirm';
      } else if (show.value?.prerecorded_key) {
        // Has upload but not confirmed → go to confirm step
        currentStep.value = 'confirm';
        uploadMode.value = 'prerecorded';
      } else {
        currentStep.value = 'info';
      }
    }

    loaded.value = true;
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Failed to load show data';
  } finally {
    loading.value = false;
  }
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
  uploading.value = true;
  uploadProgress.value = null;
  error.value = null;

  try {
    const result = await artistFlowApi.uploadFile(file, (progress) => {
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
  error.value = null;

  try {
    const result = await artistFlowApi.confirm();

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
  error.value = null;

  try {
    await artistFlowApi.deleteUpload();

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

function reset(): void {
  loaded.value = false;
  loading.value = false;
  error.value = null;
  assigned.value = false;
  show.value = undefined;
  currentStep.value = 'info';
  uploadMode.value = null;
  uploading.value = false;
  uploadProgress.value = null;
}

// ─────────────────────────────────────────────────────────────────────────────
// Composable
// ─────────────────────────────────────────────────────────────────────────────

export function useArtistFlow() {
  return {
    // State (readonly refs)
    loaded: readonly(loaded),
    loading: readonly(loading),
    error: readonly(error),
    assigned: readonly(assigned),
    show: readonly(show),
    currentStep: readonly(currentStep),
    uploadMode: readonly(uploadMode),
    uploading: readonly(uploading),
    uploadProgress: readonly(uploadProgress),

    // Computed
    hasUpload,
    isConfirmed,
    prerecordedUrl,
    prerecordedFilename,

    // Actions
    fetchMyShow,
    goToStep,
    selectMode,
    uploadFile,
    confirmUpload,
    deleteUpload,
    canNavigateTo,
    reset,
  };
}
