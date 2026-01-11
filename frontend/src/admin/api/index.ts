// API client for admin panel

const API_BASE = import.meta.env.DEV ? '' : 'https://admin.live.moafunk.de';

interface ApiError {
  error: string;
  details?: string;
}

class ApiClient {
  private baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl;
  }

  private async request<T>(
    endpoint: string,
    options: RequestInit = {}
  ): Promise<T> {
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
  role: 'artist' | 'admin' | 'superadmin';
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
};

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
  soundcloud?: string;
  instagram?: string;
  bandcamp?: string;
  spotify?: string;
  other_social?: string;
  track1_name: string;
  track2_name: string;
  file_urls: Record<string, string>;
  shows: { id: number; title: string; date: string }[];
  available_shows: AvailableShow[];
}

export const artistsApi = {
  list: (params?: { filter?: string; sort?: string; dir?: string }) => {
    const searchParams = new URLSearchParams();
    if (params?.filter) searchParams.set('filter', params.filter);
    if (params?.sort) searchParams.set('sort', params.sort);
    if (params?.dir) searchParams.set('dir', params.dir);
    const query = searchParams.toString();
    return api.get<{ artists: Artist[]; total: number }>(
      `/api/artists${query ? `?${query}` : ''}`
    );
  },

  get: (id: number) => api.get<ArtistDetail>(`/api/artists/${id}`),

  updateStatus: (id: number, status: string) =>
    api.put<void>(`/api/artists/${id}/status`, { status }),

  delete: (id: number) => api.delete<void>(`/api/artists/${id}`),

  assignShow: (artistId: number, showId: number) =>
    api.post<void>(`/api/artists/${artistId}/shows`, { show_id: showId }),

  unassignShow: (artistId: number, showId: number) =>
    api.delete<void>(`/api/artists/${artistId}/shows/${showId}`),

  updateDetails: (id: number, data: {
    mentions?: string;
    upcoming_events?: string;
    soundcloud?: string;
    instagram?: string;
    bandcamp?: string;
    spotify?: string;
    other_social?: string;
  }) => api.put<void>(`/api/artists/${id}/details`, data),

  updatePicture: async (id: number, data: {
    original: File;
    cropped: Blob;
    branded: Blob;
  }): Promise<void> => {
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

  updateAudio: async (id: number, data: {
    voice?: File;
    track1?: File;
    track2?: File;
    track1_name?: string;
    track2_name?: string;
  }): Promise<void> => {
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
  description?: string;
  status: string;
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
  description?: string;
  status: string;
  created_at: string;
  updated_at?: string;
  artists: AssignedArtist[];
  available_artists: { id: number; name: string; pronouns: string }[];
  artists_left: number;
  cover_url?: string;
  cover_generated_at?: string;
  recording_url?: string;
  recording_peaks_url?: string;
}

export const showsApi = {
  list: () => api.get<{ shows: Show[]; artists: Artist[] }>('/api/shows'),

  get: (id: number) => api.get<ShowDetail>(`/api/shows/${id}`),

  create: (data: Partial<Show>) => api.post<Show>('/api/shows', data),

  update: (id: number, data: Partial<Show>) =>
    api.put<Show>(`/api/shows/${id}`, data),

  delete: (id: number) => api.delete<void>(`/api/shows/${id}`),

  assignArtist: (showId: number, artistId: number) =>
    api.post<{ success: boolean; artist: AssignedArtist }>(`/api/shows/${showId}/artists`, { artist_id: artistId }),

  unassignArtist: (showId: number, artistId: number) =>
    api.delete<{ success: boolean }>(`/api/shows/${showId}/artists/${artistId}`),

  uploadRecording: async (showId: number, file: File): Promise<{ success: boolean; key: string; recording_url?: string; recording_peaks_url?: string }> => {
    const formData = new FormData();
    formData.append('file', file);
    
    // Extract waveform peaks before upload
    try {
      const { extractWaveformPeaksJson } = await import('../../pages/waveformExtractor');
      const peaksJson = await extractWaveformPeaksJson(file);
      formData.append('peaks', peaksJson);
    } catch (err) {
      console.warn('Failed to extract waveform peaks:', err);
      // Continue without peaks - not critical
    }
    
    const response = await fetch(`${API_BASE}/api/shows/${showId}/upload-recording`, {
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

  deleteRecording: (showId: number) =>
    api.delete<{ success: boolean }>(`/api/shows/${showId}/recording`),
};

// Users API
export interface AdminUser {
  id: number;
  username: string;
  role: string;
  expires_at?: string;
  created_at: string;
}

export const usersApi = {
  list: () => api.get<{ users: AdminUser[] }>('/api/users'),

  create: (data: { username: string; role: string; expires_at?: string }) =>
    api.post<{ user: AdminUser; password: string }>('/api/users', data),

  update: (id: number, data: { role?: string; expires_at?: string }) =>
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

// Stream API
export interface StreamStatus {
  active: boolean;
  user?: string;
}

export const streamApi = {
  status: () => api.get<StreamStatus>('/api/stream/status'),

  stop: () => api.post<void>('/api/stream/stop'),
};
