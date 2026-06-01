// API client for admin panel
// In production, admin panel is hosted at admin.live.moafunk.de (same origin as API)
const API_BASE = '';

interface ApiError {
  error: string;
  details?: string;
}

class ApiClient {
  private baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl;
  }

  private async request<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
    const url = `${this.baseUrl}${endpoint}`;

    const response = await fetch(url, {
      ...options,
      credentials: 'include', // Include cookies for auth
      headers: {
        'Content-Type': 'application/json',
        ...options.headers,
      },
    });

    if (!response.ok) {
      const error: ApiError = await response.json().catch(() => ({
        error: `HTTP ${response.status}: ${response.statusText}`,
      }));
      throw new Error(error.error || error.details || 'Unknown error');
    }

    // Handle 204 No Content
    if (response.status === 204) {
      return undefined as T;
    }

    return response.json();
  }

  get<T>(endpoint: string): Promise<T> {
    return this.request<T>(endpoint, { method: 'GET' });
  }

  post<T>(endpoint: string, data?: unknown): Promise<T> {
    return this.request<T>(endpoint, {
      method: 'POST',
      body: data ? JSON.stringify(data) : undefined,
    });
  }

  put<T>(endpoint: string, data?: unknown): Promise<T> {
    return this.request<T>(endpoint, {
      method: 'PUT',
      body: data ? JSON.stringify(data) : undefined,
    });
  }

  delete<T>(endpoint: string): Promise<T> {
    return this.request<T>(endpoint, { method: 'DELETE' });
  }
}

export const api = new ApiClient(API_BASE);

// Auth API
export interface User {
  id: number;
  username: string;
  role: 'host' | 'admin' | 'superadmin' | 'guest';
  artist_id?: number;
  artist_name?: string;
  has_show?: boolean;
  must_change_password?: boolean;
}

export interface LoginResponse {
  user: User;
  redirect_url?: string;
}

export const authApi = {
  login: (username: string, password: string) =>
    api.post<LoginResponse>('/api/auth/login', { username, password }),

  logout: () => api.post<void>('/api/auth/logout'),

  me: () => api.get<User>('/api/auth/me'),

  // First-login password set: no current password required (session proves
  // identity); only valid while the account has must_change_password set.
  setInitialPassword: (newPassword: string) =>
    api.post<void>('/api/auth/set-initial-password', { new_password: newPassword }),
};

// Overlay types
export interface OverlayShadowParams {
  offsetX: number;
  offsetY: number;
  color: string;
}

export interface OverlayElementParams {
  visible: boolean;
  x: number;
  y: number;
  size: number;
  color: string;
  fontWeight?: string;
  fontStyle?: string;
  shadow?: OverlayShadowParams;
}

export interface OverlayFilterParams {
  brightness: number;
  contrast: number;
  saturate: number;
  hueRotate: number;
  grayscale: number;
  sepia: number;
  blur: number;
}

export interface OverlayParams {
  un: OverlayElementParams;
  heard: OverlayElementParams;
  logo: OverlayElementParams;
  artistName: OverlayElementParams;
  filter: OverlayFilterParams;
  /** Per-tile name colors for show overlays (up to 4 hex strings). */
  tileColors?: string[];
  /** Per-tile shadow colors for show overlays (up to 4 hex strings). */
  tileShadowColors?: string[];
}

export interface OverlayImage {
  key: string;
  url: string;
  last_modified: string;
  size: number;
}

export interface OverlayListResponse {
  overlays: OverlayImage[];
  active_key: string | null;
}

export interface OverlayPreset {
  id: number;
  name: string;
  params: OverlayParams;
  preset_type: 'artist' | 'show';
  created_at: string;
  updated_at: string;
}

// Artists API
export interface Artist {
  id: number;
  name: string;
  status: string;
  created_at: string;
  show_titles?: string;
}

export interface AvailableShow {
  id: number;
  title: string;
  date: string;
  artists_left: number;
}

export interface ArtistDetail {
  id: number;
  name: string;
  pronouns: string;
  status: string;
  created_at: string;
  mentions?: string;
  upcoming_events?: string;
  music_description?: string;
  ai_bio?: string;
  instagram_caption?: string;
  instagram_posted_at?: string;
  soundcloud?: string;
  instagram?: string;
  bandcamp?: string;
  spotify?: string;
  other_social?: string;
  track1_name: string;
  track2_name: string;
  file_urls: Record<string, string>;
  shows: {
    id: number;
    title: string;
    date: string;
    cover_url?: string;
    cover_generated_at?: string;
  }[];
  available_shows: AvailableShow[];
  active_overlay_preset_id?: number;
}

export const artistsApi = {
  list: (params?: { filter?: string; sort?: string; dir?: string; unlinked?: boolean }) => {
    const searchParams = new URLSearchParams();
    if (params?.filter) searchParams.set('filter', params.filter);
    if (params?.sort) searchParams.set('sort', params.sort);
    if (params?.dir) searchParams.set('dir', params.dir);
    if (params?.unlinked) searchParams.set('unlinked', 'true');
    const query = searchParams.toString();
    return api.get<{ artists: Artist[]; total: number }>(`/api/artists${query ? `?${query}` : ''}`);
  },

  get: (id: number) => api.get<ArtistDetail>(`/api/artists/${id}`),

  updateStatus: (id: number, status: string) =>
    api.put<void>(`/api/artists/${id}/status`, { status }),

  delete: (id: number) => api.delete<void>(`/api/artists/${id}`),

  assignShow: (artistId: number, showId: number) =>
    api.post<void>(`/api/artists/${artistId}/shows`, { show_id: showId }),

  unassignShow: (artistId: number, showId: number) =>
    api.delete<void>(`/api/artists/${artistId}/shows/${showId}`),

  updateDetails: (
    id: number,
    data: {
      mentions?: string;
      upcoming_events?: string;
      music_description?: string;
      soundcloud?: string;
      instagram?: string;
      bandcamp?: string;
      spotify?: string;
      other_social?: string;
    }
  ) => api.put<void>(`/api/artists/${id}/details`, data),

  generateBio: (id: number) =>
    api.post<{ success: boolean; ai_bio: string }>(`/api/artists/${id}/generate-bio`),

  generateInstagramCaption: (id: number) =>
    api.post<{ success: boolean; instagram_caption: string }>(
      `/api/artists/${id}/generate-instagram-caption`
    ),

  updateInstagramCaption: (id: number, instagram_caption: string) =>
    api.put<{ success: boolean; instagram_caption: string }>(
      `/api/artists/${id}/instagram-caption`,
      { instagram_caption }
    ),

  postToInstagram: (id: number, force = false, account = 'dev') =>
    api.post<{
      success: boolean;
      media_id?: string;
      error?: string;
      already_posted: boolean;
    }>(`/api/artists/${id}/instagram`, { force, account }),

  generateVideos: (id: number) =>
    api.post<{
      success: boolean;
      track1_video_key?: string | null;
      track2_video_key?: string | null;
    }>(`/api/artists/${id}/generate-videos`),

  sendTelegramPreview: (id: number) =>
    api.post<{ success: boolean }>(`/api/artists/${id}/telegram-preview`),

  /** Fetch an artist image as a same-origin blob (avoids R2 CORS issues). */
  getImageBlob: async (
    id: number,
    type: 'original' | 'cropped' | 'overlay' = 'original'
  ): Promise<Blob> => {
    const response = await fetch(`${API_BASE}/api/artists/${id}/image-proxy?type=${type}`, {
      credentials: 'include',
    });
    if (!response.ok) {
      throw new Error(`Failed to fetch image: ${response.status}`);
    }
    return response.blob();
  },

  updatePicture: async (
    id: number,
    data: {
      original: File;
      cropped: Blob;
      branded: Blob;
    }
  ): Promise<void> => {
    const formData = new FormData();
    formData.append('original', data.original);
    formData.append('cropped', data.cropped, 'cropped.jpg');
    formData.append('branded', data.branded, 'branded.jpg');
    const response = await fetch(`${API_BASE}/api/artists/${id}/picture`, {
      method: 'PUT',
      credentials: 'include',
      body: formData,
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({ error: 'Upload failed' }));
      throw new Error(error.error || 'Upload failed');
    }
  },

  listOverlays: (id: number) => api.get<OverlayListResponse>(`/api/artists/${id}/overlays`),

  saveOverlay: async (id: number, blob: Blob): Promise<{ key: string; url: string }> => {
    const formData = new FormData();
    formData.append('image', blob, 'overlay.jpg');
    const response = await fetch(`${API_BASE}/api/artists/${id}/overlays`, {
      method: 'POST',
      credentials: 'include',
      body: formData,
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({ error: 'Upload failed' }));
      throw new Error(error.error || 'Upload failed');
    }
    return response.json();
  },

  setActiveOverlay: (id: number, key: string) =>
    api.put<void>(`/api/artists/${id}/overlays/active`, { key }),

  setActivePreset: (id: number, presetId: number | null) =>
    api.put<{ success: boolean }>(`/api/artists/${id}/active-preset`, { preset_id: presetId }),

  updateAudio: async (
    id: number,
    data: {
      voice?: File;
      track1?: File;
      track2?: File;
      track1_name?: string;
      track2_name?: string;
    }
  ): Promise<void> => {
    const formData = new FormData();

    // Extract peaks for each audio file before upload
    const { extractWaveformPeaksJson } = await import('../../pages/waveformExtractor');

    if (data.voice) {
      formData.append('voice', data.voice);
      try {
        const peaksJson = await extractWaveformPeaksJson(data.voice);
        formData.append('voice_peaks', peaksJson);
      } catch (err) {
        console.warn('Failed to extract voice peaks:', err);
      }
    }
    if (data.track1) {
      formData.append('track1', data.track1);
      try {
        const peaksJson = await extractWaveformPeaksJson(data.track1);
        formData.append('track1_peaks', peaksJson);
      } catch (err) {
        console.warn('Failed to extract track1 peaks:', err);
      }
    }
    if (data.track2) {
      formData.append('track2', data.track2);
      try {
        const peaksJson = await extractWaveformPeaksJson(data.track2);
        formData.append('track2_peaks', peaksJson);
      } catch (err) {
        console.warn('Failed to extract track2 peaks:', err);
      }
    }
    if (data.track1_name) formData.append('track1_name', data.track1_name);
    if (data.track2_name) formData.append('track2_name', data.track2_name);

    const response = await fetch(`${API_BASE}/api/artists/${id}/audio`, {
      method: 'PUT',
      credentials: 'include',
      body: formData,
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({ error: 'Upload failed' }));
      throw new Error(error.error || 'Upload failed');
    }
  },
};

// Shows API
export interface Show {
  id: number;
  title: string;
  date: string;
  start_time?: string;
  end_time?: string;
  description?: string;
  ai_bio?: string;
  status: string;
  show_type: string;
  /** Intended delivery: 'live' or 'prerecorded' (changeable after creation). */
  stream_mode?: 'live' | 'prerecorded';
  artists: { id: number; name: string }[];
}

/** Read-only schedule entry returned by GET /api/shows-overview. */
export interface ShowOverviewItem {
  id: number;
  title: string;
  date: string;
  start_time?: string;
  end_time?: string;
  description?: string;
  status: string;
  show_type: string;
  host_username?: string;
  artists: { id: number; name: string }[];
}

export interface AssignedArtist {
  id: number;
  name: string;
  pronouns: string;
  pic_url?: string;
  voice_url?: string;
  track1_url?: string;
  track2_url?: string;
  track1_peaks_url?: string;
  track2_peaks_url?: string;
  voice_peaks_url?: string;
  has_pic: boolean;
}

export interface ShowDetail {
  id: number;
  title: string;
  date: string;
  start_time?: string;
  end_time?: string;
  description?: string;
  ai_bio?: string;
  status: string;
  show_type: string;
  created_at: string;
  updated_at?: string;
  artists: AssignedArtist[];
  available_artists: { id: number; name: string; pronouns: string }[];
  artists_left: number;
  cover_url?: string;
  collage_url?: string;
  cover_generated_at?: string;
  active_overlay_preset_id?: number;
  recording_url?: string;
  recording_peaks_url?: string;
  recording_filename?: string;
  instagram_posted_at?: string;
  soundcloud_track_id?: string;
  soundcloud_url?: string;
  soundcloud_uploaded_at?: string;
  soundcloud_public?: boolean;
  prerecorded_key?: string;
  prerecorded_filename?: string;
  prerecorded_confirmed_at?: string;
  prerecorded_url?: string;
  // Host assignment (external/brunchtime shows)
  host_user_id?: number;
  host_username?: string;
  available_hosts?: { id: number; username: string }[];
  /** Intended delivery: 'live' or 'prerecorded' (changeable after creation). */
  stream_mode?: 'live' | 'prerecorded';
}

export const showsApi = {
  list: () => api.get<{ shows: Show[]; artists: Artist[] }>('/api/shows'),

  /** Read-only list of all shows (incl. other users') for any authenticated user. */
  overview: () => api.get<{ shows: ShowOverviewItem[] }>('/api/shows-overview'),

  /** List all shows assigned to the authenticated user (host or artist) */
  myShows: () => api.get<{ shows: Show[] }>('/api/my-shows'),

  get: (id: number) => api.get<ShowDetail>(`/api/shows/${id}`),

  create: (data: Partial<Show> & { template_id?: number; host_user_id?: number }) =>
    api.post<Show>('/api/shows', data),

  update: (id: number, data: Partial<Show>) => api.put<Show>(`/api/shows/${id}`, data),

  delete: (id: number) => api.delete<void>(`/api/shows/${id}`),

  uploadCover: async (
    showId: number,
    file: File
  ): Promise<{ success: boolean; cover_url?: string; cover_generated_at?: string }> => {
    const formData = new FormData();
    formData.append('file', file);
    const token = localStorage.getItem('auth_token');
    const resp = await fetch(`/api/shows/${showId}/upload-cover`, {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: formData,
    });
    if (!resp.ok) throw new Error(await resp.text());
    return resp.json();
  },

  assignArtist: (showId: number, artistId: number) =>
    api.post<{ success: boolean; artist: AssignedArtist }>(`/api/shows/${showId}/artists`, {
      artist_id: artistId,
    }),

  unassignArtist: (showId: number, artistId: number) =>
    api.delete<{ success: boolean }>(`/api/shows/${showId}/artists/${artistId}`),

  assignHost: (showId: number, userId: number) =>
    api.post<{ success: boolean; host_user_id: number; host_username: string }>(
      `/api/shows/${showId}/host`,
      {
        user_id: userId,
      }
    ),

  unassignHost: (showId: number) => api.delete<{ success: boolean }>(`/api/shows/${showId}/host`),

  regenerateBio: (showId: number) =>
    api.post<{ success: boolean; ai_bio: string | null }>(`/api/shows/${showId}/regenerate-bio`),

  uploadRecording: async (
    showId: number,
    file: File,
    onProgress?: (progress: {
      phase: 'extracting' | 'uploading' | 'finalizing';
      percent: number;
      chunkIndex?: number;
      totalChunks?: number;
    }) => void
  ): Promise<{
    success: boolean;
    key: string;
    recording_url?: string;
    recording_peaks_url?: string;
  }> => {
    // Use chunked upload for files > 50MB to stay under Cloudflare's 100MB limit
    const CHUNK_SIZE = 50 * 1024 * 1024; // 50MB chunks
    const useChunked = file.size > CHUNK_SIZE;

    // Extract waveform peaks before upload
    let peaksJson: string | undefined;
    try {
      onProgress?.({ phase: 'extracting', percent: 0 });
      const { extractWaveformPeaksJson } = await import('../../pages/waveformExtractor');
      peaksJson = await extractWaveformPeaksJson(file);
      onProgress?.({ phase: 'extracting', percent: 100 });
    } catch (err) {
      console.warn('Failed to extract waveform peaks:', err);
      // Continue without peaks - not critical
    }

    if (!useChunked) {
      // Small file: use single request upload
      const formData = new FormData();
      formData.append('file', file);
      if (peaksJson) {
        formData.append('peaks', peaksJson);
      }

      onProgress?.({ phase: 'uploading', percent: 0 });
      const response = await fetch(`${API_BASE}/api/shows/${showId}/upload-recording`, {
        method: 'POST',
        credentials: 'include',
        body: formData,
      });

      if (!response.ok) {
        const error = await response.json().catch(() => ({ error: 'Upload failed' }));
        throw new Error(error.error || 'Upload failed');
      }

      onProgress?.({ phase: 'uploading', percent: 100 });
      return response.json();
    }

    // Large file: use chunked upload
    const totalChunks = Math.ceil(file.size / CHUNK_SIZE);

    // Step 1: Initialize upload
    onProgress?.({ phase: 'uploading', percent: 0, chunkIndex: 0, totalChunks });
    const initResponse = await fetch(`${API_BASE}/api/shows/${showId}/upload-recording/init`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        filename: file.name,
        total_size: file.size,
        total_chunks: totalChunks,
        peaks: peaksJson,
      }),
    });

    if (!initResponse.ok) {
      const error = await initResponse.json().catch(() => ({ error: 'Init failed' }));
      throw new Error(error.error || error.message || 'Failed to initialize upload');
    }

    const { session_id } = await initResponse.json();

    // Step 2: Upload chunks sequentially
    for (let i = 0; i < totalChunks; i++) {
      const start = i * CHUNK_SIZE;
      const end = Math.min(start + CHUNK_SIZE, file.size);
      const chunk = file.slice(start, end);

      const chunkFormData = new FormData();
      chunkFormData.append('chunk', chunk);

      const chunkResponse = await fetch(
        `${API_BASE}/api/shows/${showId}/upload-recording/chunk/${session_id}?index=${i}`,
        {
          method: 'POST',
          credentials: 'include',
          body: chunkFormData,
        }
      );

      if (!chunkResponse.ok) {
        const error = await chunkResponse.json().catch(() => ({ error: 'Chunk upload failed' }));
        throw new Error(
          error.error || error.message || `Failed to upload chunk ${i + 1}/${totalChunks}`
        );
      }

      const percent = Math.round(((i + 1) / totalChunks) * 100);
      onProgress?.({ phase: 'uploading', percent, chunkIndex: i + 1, totalChunks });
    }

    // Step 3: Finalize upload
    onProgress?.({ phase: 'finalizing', percent: 0 });
    const finalizeResponse = await fetch(
      `${API_BASE}/api/shows/${showId}/upload-recording/finalize/${session_id}`,
      {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      }
    );

    if (!finalizeResponse.ok) {
      const error = await finalizeResponse.json().catch(() => ({ error: 'Finalize failed' }));
      throw new Error(error.error || error.message || 'Failed to finalize upload');
    }

    onProgress?.({ phase: 'finalizing', percent: 100 });
    return finalizeResponse.json();
  },

  deleteRecording: (showId: number) =>
    api.delete<{ success: boolean }>(`/api/shows/${showId}/recording`),

  postToInstagram: (showId: number, force = false, account = 'dev') =>
    api.post<{
      success: boolean;
      media_id?: string;
      error?: string;
      already_posted: boolean;
    }>(`/api/shows/${showId}/instagram`, { force, account }),

  sendTelegramPreview: (showId: number) =>
    api.post<{ success: boolean; error?: string }>(`/api/shows/${showId}/telegram-preview`),

  uploadToSoundCloud: (showId: number) =>
    api.post<{
      success: boolean;
      track_id?: string;
      track_url?: string;
      error?: string;
    }>(`/api/shows/${showId}/soundcloud/upload`),

  setSoundCloudPrivacy: (showId: number, isPublic: boolean) =>
    api.post<{
      success: boolean;
      track_id?: string;
      track_url?: string;
      error?: string;
    }>(`/api/shows/${showId}/soundcloud/privacy`, { public: isPublic }),

  setActivePreset: (id: number, presetId: number | null) =>
    api.put<{ success: boolean }>(`/api/shows/${id}/active-preset`, { preset_id: presetId }),

  /** Trigger server-side cover regeneration (plain collage + overlay if preset is set). */
  regenerateCover: (id: number) =>
    api.post<{ success: boolean; cover_url?: string }>(`/api/shows/${id}/regenerate-cover`),

  /** Fetch a show image as a same-origin blob (avoids R2 CORS issues).
   *  @param type 'cover' (default, with overlay) or 'collage' (plain 2×2 grid) */
  getImageBlob: async (id: number, type: 'cover' | 'collage' = 'cover'): Promise<Blob> => {
    const response = await fetch(`${API_BASE}/api/shows/${id}/image-proxy?type=${type}`, {
      credentials: 'include',
    });
    if (!response.ok) {
      throw new Error(`Failed to fetch show image: ${response.status}`);
    }
    return response.blob();
  },

  listOverlays: (id: number) => api.get<OverlayListResponse>(`/api/shows/${id}/overlays`),

  saveOverlay: async (id: number, blob: Blob): Promise<{ key: string; url: string }> => {
    const formData = new FormData();
    formData.append('image', blob, 'overlay.jpg');
    const response = await fetch(`${API_BASE}/api/shows/${id}/overlays`, {
      method: 'POST',
      credentials: 'include',
      body: formData,
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({ error: 'Upload failed' }));
      throw new Error(error.error || 'Upload failed');
    }
    return response.json();
  },
};

// Show Templates API — per-user reusable (name + cover + description) bundles
export interface ShowTemplate {
  id: number;
  name: string;
  description?: string;
  cover_url?: string;
  created_at: string;
}

export const showTemplatesApi = {
  /** List the current user's templates (with presigned cover URLs). */
  list: () => api.get<{ templates: ShowTemplate[] }>('/api/show-templates'),

  create: (data: { name: string; description?: string }) =>
    api.post<{ id: number; name: string; description?: string }>('/api/show-templates', data),

  uploadCover: async (
    templateId: number,
    file: File
  ): Promise<{ success: boolean; cover_url?: string }> => {
    const formData = new FormData();
    formData.append('file', file);
    const resp = await fetch(`/api/show-templates/${templateId}/cover`, {
      method: 'POST',
      credentials: 'include',
      body: formData,
    });
    if (!resp.ok) throw new Error(await resp.text());
    return resp.json();
  },

  delete: (id: number) => api.delete<void>(`/api/show-templates/${id}`),
};

// SoundCloud API
export interface SoundCloudStatus {
  configured: boolean;
  authorized: boolean;
  auth_url?: string;
}

export const soundcloudApi = {
  getStatus: () => api.get<SoundCloudStatus>('/api/soundcloud/status'),
  disconnect: () => api.post<{ success: boolean }>('/api/soundcloud/disconnect'),
};

// Settings API
export interface NotificationSettings {
  enabled: boolean;
}

export const settingsApi = {
  getNotifications: () => api.get<NotificationSettings>('/api/settings/notifications'),

  setNotifications: (enabled: boolean) =>
    api.put<NotificationSettings>('/api/settings/notifications', { enabled }),
};

// Users API
export interface AdminUser {
  id: number;
  username: string;
  role: string;
  expires_at?: string;
  created_at: string;
  linked_artist_id?: number;
  linked_artist_name?: string;
}

export const usersApi = {
  list: () => api.get<{ users: AdminUser[] }>('/api/users'),

  create: (data: { username: string; role: string; expires_at?: string; artist_id?: number }) =>
    api.post<{ user: AdminUser; password: string }>('/api/users', data),

  update: (id: number, data: { role?: string; expires_at?: string; artist_id?: number | null }) =>
    api.put<{ user: AdminUser }>(`/api/users/${id}`, data),

  resetPassword: (id: number) =>
    api.post<{ password: string }>(`/api/users/${id}/reset-password`, {}),

  delete: (id: number) => api.delete<void>(`/api/users/${id}`),

  changePassword: (currentPassword: string, newPassword: string) =>
    api.post<void>('/api/auth/change-password', {
      current_password: currentPassword,
      new_password: newPassword,
    }),
};

// Guest accounts — date-restricted logins created during show setup.
export interface GuestCredentials {
  user_id: number;
  username: string;
  /** One-time bootstrap password; the guest must replace it on first login. */
  password: string;
  login_date: string;
}

export const guestsApi = {
  /** Create a guest who may only log in on `login_date` (the show date). */
  create: (data: { username: string; login_date: string }) =>
    api.post<GuestCredentials>('/api/guests', data),
};

// Stream API
export interface StreamStatus {
  active: boolean;
  user?: string;
}

export const streamApi = {
  status: () => api.get<StreamStatus>('/api/stream/status'),

  stop: () => api.post<void>('/api/stream/stop'),
};

// Recording API
export interface RecordingArtist {
  id: number;
  name: string;
  pronouns: string;
  pic_url?: string;
  voice_url?: string;
  voice_key?: string;
  track1_url?: string;
  track1_key?: string;
  track1_name: string;
  track2_url?: string;
  track2_key?: string;
  track2_name: string;
}

export interface ShowWithArtists {
  id: number;
  title: string;
  date: string;
  description?: string;
  status: string;
  artists: RecordingArtist[];
}

export interface RecordingStatus {
  is_recording: boolean;
  show_id?: number;
  started_at?: string;
  elapsed_ms?: number;
  marker_count?: number;
}

export interface RecordingVersionInfo {
  id: number;
  show_id: number;
  version: string;
  status: 'raw' | 'finalizing' | 'finalized' | 'failed';
  duration_ms?: number;
  marker_count: number;
  created_at: string;
  finalized_at?: string;
  download_url?: string;
  error_message?: string;
}

export const recordingApi = {
  getShowWithArtists: (showId: number) =>
    api.get<ShowWithArtists>(`/api/shows/${showId}/with-artists`),

  start: (showId: number) =>
    api.post<{ success: boolean; started_at: string }>('/api/recording/start', { show_id: showId }),

  stop: () =>
    api.post<{ success: boolean; raw_key: string; markers_key: string }>('/api/recording/stop'),

  status: () => api.get<RecordingStatus>('/api/recording/status'),

  addMarker: (data: {
    artist_id: number;
    track_type: 'voice_message' | 'track1' | 'track2';
    track_key: string;
    duration_ms: number;
    offset_ms: number;
    volume: number;
  }) => api.post<{ success: boolean }>('/api/recording/marker', data),

  listRecordings: (showId: number) =>
    api.get<{ recordings: RecordingVersionInfo[] }>(`/api/shows/${showId}/recordings`),
};

// Overlay Presets API
export const presetsApi = {
  list: (type?: 'artist' | 'show') => {
    const qs = type ? `?type=${type}` : '';
    return api.get<{ presets: OverlayPreset[] }>(`/api/overlay-presets${qs}`);
  },

  create: (name: string, params: OverlayParams, preset_type: 'artist' | 'show' = 'artist') =>
    api.post<OverlayPreset>('/api/overlay-presets', { name, params, preset_type }),

  update: (id: number, data: { name?: string; params?: OverlayParams }) =>
    api.put<OverlayPreset>(`/api/overlay-presets/${id}`, data),

  delete: (id: number) => api.delete<void>(`/api/overlay-presets/${id}`),
};

// Artist Flow API (My Show)
export interface MyShowArtist {
  id: number;
  name: string;
}

export interface MyShowInfo {
  id: number;
  title: string;
  date: string;
  start_time?: string;
  end_time?: string;
  description?: string;
  show_type: string;
  artists: MyShowArtist[];
  cover_url?: string;
  prerecorded_key?: string;
  prerecorded_url?: string;
  prerecorded_filename?: string;
  prerecorded_confirmed_at?: string;
}

export interface MyShowResponse {
  assigned: boolean;
  shows: MyShowInfo[];
}

export interface UploadInitResponse {
  success: boolean;
  session_id: string;
  message: string;
}

export interface UploadChunkResponse {
  success: boolean;
  index: number;
  received_bytes: number;
}

export interface UploadResult {
  success: boolean;
  key: string;
  prerecorded_url?: string;
  filename: string;
}

export interface ConfirmResponse {
  success: boolean;
  confirmed_at: string;
}

export const hostFlowApi = {
  getMyShows: () => api.get<MyShowResponse>('/api/my-show'),

  confirm: (showId: number) => api.post<ConfirmResponse>(`/api/my-show/confirm?show_id=${showId}`),

  goLive: (showId: number) =>
    api.post<{ success: boolean; message: string }>(`/api/my-show/go-live?show_id=${showId}`),

  deleteUpload: (showId: number) =>
    api.delete<{ success: boolean }>(`/api/my-show/upload?show_id=${showId}`),

  /** Upload a small prerecorded file (≤50MB) directly. */
  uploadSmall: async (showId: number, file: File): Promise<UploadResult> => {
    const formData = new FormData();
    formData.append('file', file);

    const response = await fetch(`${API_BASE}/api/my-show/upload?show_id=${showId}`, {
      method: 'POST',
      credentials: 'include',
      body: formData,
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({ error: 'Upload failed' }));
      throw new Error(error.error || 'Upload failed');
    }

    return response.json();
  },

  /** Upload a large prerecorded file via chunked upload with progress callback. */
  uploadFile: async (
    showId: number,
    file: File,
    onProgress?: (progress: {
      phase: 'uploading' | 'finalizing';
      percent: number;
      chunkIndex?: number;
      totalChunks?: number;
    }) => void
  ): Promise<UploadResult> => {
    const CHUNK_SIZE = 50 * 1024 * 1024; // 50MB
    const useChunked = file.size > CHUNK_SIZE;

    if (!useChunked) {
      onProgress?.({ phase: 'uploading', percent: 0 });
      const result = await hostFlowApi.uploadSmall(showId, file);
      onProgress?.({ phase: 'uploading', percent: 100 });
      return result;
    }

    // Chunked upload
    const totalChunks = Math.ceil(file.size / CHUNK_SIZE);

    // Step 1: Init
    onProgress?.({ phase: 'uploading', percent: 0, chunkIndex: 0, totalChunks });
    const initResponse = await fetch(`${API_BASE}/api/my-show/upload/init?show_id=${showId}`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        filename: file.name,
        total_size: file.size,
        total_chunks: totalChunks,
      }),
    });

    if (!initResponse.ok) {
      const error = await initResponse.json().catch(() => ({ error: 'Init failed' }));
      throw new Error(error.error || error.message || 'Failed to initialize upload');
    }

    const { session_id } = (await initResponse.json()) as UploadInitResponse;

    // Step 2: Upload chunks
    for (let i = 0; i < totalChunks; i++) {
      const start = i * CHUNK_SIZE;
      const end = Math.min(start + CHUNK_SIZE, file.size);
      const chunk = file.slice(start, end);

      const chunkFormData = new FormData();
      chunkFormData.append('chunk', chunk);

      const chunkResponse = await fetch(
        `${API_BASE}/api/my-show/upload/chunk/${session_id}?index=${i}&show_id=${showId}`,
        {
          method: 'POST',
          credentials: 'include',
          body: chunkFormData,
        }
      );

      if (!chunkResponse.ok) {
        const error = await chunkResponse.json().catch(() => ({ error: 'Chunk upload failed' }));
        throw new Error(
          error.error || error.message || `Failed to upload chunk ${i + 1}/${totalChunks}`
        );
      }

      const percent = Math.round(((i + 1) / totalChunks) * 100);
      onProgress?.({ phase: 'uploading', percent, chunkIndex: i + 1, totalChunks });
    }

    // Step 3: Finalize
    onProgress?.({ phase: 'finalizing', percent: 0 });
    const finalizeResponse = await fetch(
      `${API_BASE}/api/my-show/upload/finalize/${session_id}?show_id=${showId}`,
      {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      }
    );

    if (!finalizeResponse.ok) {
      const error = await finalizeResponse.json().catch(() => ({ error: 'Finalize failed' }));
      throw new Error(error.error || error.message || 'Failed to finalize upload');
    }

    onProgress?.({ phase: 'finalizing', percent: 100 });
    return finalizeResponse.json();
  },
};
