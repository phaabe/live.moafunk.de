use serde::{Deserialize, Serialize};
use sqlx::FromRow;

/// What field is being edited in a Telegram Instagram preview session.
#[derive(Debug, Clone)]
pub enum TelegramEditField {
    Caption,
    Image,
    Timecode,
}

/// In-memory session tracking an active edit on a Telegram preview message.
#[derive(Debug, Clone)]
pub struct TelegramEditSession {
    pub show_id: i64,
    /// Optional artist ID (for artist-level previews)
    pub artist_id: Option<i64>,
    /// Chat ID where the preview message lives
    pub preview_chat_id: i64,
    /// Message ID of the preview message (photo + caption)
    pub preview_message_id: i32,
    /// Which field the user is editing
    pub field: TelegramEditField,
    /// Track number (1 or 2) for Timecode sessions
    pub track_number: Option<u8>,
    /// Message ID of the video message being re-generated
    pub video_msg_id: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct Artist {
    pub id: i64,
    pub name: String,
    pub pronouns: String,

    pub pic_key: Option<String>,
    pub pic_cropped_key: Option<String>,
    pub pic_overlay_key: Option<String>,
    pub voice_message_key: Option<String>,
    pub no_voice_message: bool,

    pub track1_name: String,
    pub track1_key: Option<String>,
    pub track2_name: String,
    pub track2_key: Option<String>,

    // Original audio file keys (before mp3 conversion)
    pub track1_original_key: Option<String>,
    pub track2_original_key: Option<String>,
    pub voice_original_key: Option<String>,

    // Pre-generated waveform preview video keys (MP4 in R2)
    pub track1_video_key: Option<String>,
    pub track2_video_key: Option<String>,

    pub instagram: Option<String>,
    pub soundcloud: Option<String>,
    pub bandcamp: Option<String>,
    pub spotify: Option<String>,
    pub other_social: Option<String>,

    pub upcoming_events: Option<String>,
    pub mentions: Option<String>,

    pub music_description: Option<String>,
    pub ai_bio: Option<String>,
    pub instagram_caption: Option<String>,
    pub instagram_posted_at: Option<String>,

    // Telegram artist preview tracking
    pub telegram_preview_message_id: Option<i64>,
    pub telegram_video1_message_id: Option<i64>,
    pub telegram_video2_message_id: Option<i64>,
    pub telegram_artist_preview_sent_at: Option<String>,

    // Active overlay preset (references overlay_presets.id)
    pub active_overlay_preset_id: Option<i64>,

    // Linked login user account
    pub user_id: Option<i64>,

    pub status: String,
    pub created_at: String,
    pub updated_at: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct Show {
    pub id: i64,
    pub title: String,
    pub date: String,
    pub description: Option<String>,
    pub status: String,
    pub show_type: String,
    pub created_at: String,
    pub updated_at: Option<String>,
    pub cover_generated_at: Option<String>,
    pub recording_key: Option<String>,
    pub recording_filename: Option<String>,
    pub instagram_posted_at: Option<String>,
    pub instagram_post_url: Option<String>,
    pub ai_bio: Option<String>,
    pub soundcloud_track_id: Option<String>,
    pub soundcloud_url: Option<String>,
    pub soundcloud_uploaded_at: Option<String>,
    pub soundcloud_public: Option<bool>,
    pub telegram_preview_sent_at: Option<String>,
    pub active_overlay_preset_id: Option<i64>,
    pub start_time: Option<String>,
    pub prerecorded_key: Option<String>,
    pub prerecorded_filename: Option<String>,
    pub prerecorded_confirmed_at: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct SubmitResponse {
    pub success: bool,
    pub message: String,
    pub artist_id: Option<i64>,
}

// User roles for the admin dashboard
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum UserRole {
    Superadmin,
    Admin,
    Artist,
}

impl UserRole {
    pub fn as_str(&self) -> &'static str {
        match self {
            UserRole::Superadmin => "superadmin",
            UserRole::Admin => "admin",
            UserRole::Artist => "artist",
        }
    }

    pub fn from_str(s: &str) -> Option<Self> {
        match s.to_lowercase().as_str() {
            "superadmin" => Some(UserRole::Superadmin),
            "admin" => Some(UserRole::Admin),
            "artist" => Some(UserRole::Artist),
            _ => None,
        }
    }

    /// Check if this role can access admin pages (artists, shows, users)
    pub fn can_access_admin(&self) -> bool {
        matches!(self, UserRole::Superadmin | UserRole::Admin)
    }

    /// Check if this role can manage users
    pub fn can_manage_users(&self) -> bool {
        matches!(self, UserRole::Superadmin | UserRole::Admin)
    }

    /// Check if this role can manage superadmin accounts
    pub fn can_manage_superadmins(&self) -> bool {
        matches!(self, UserRole::Superadmin)
    }

    /// Check if this role can change their own password
    pub fn can_change_password(&self) -> bool {
        matches!(
            self,
            UserRole::Superadmin | UserRole::Admin | UserRole::Artist
        )
    }
}

impl std::fmt::Display for UserRole {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.as_str())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct User {
    pub id: i64,
    pub username: String,
    pub password_hash: String,
    pub role: String,
    pub created_by: Option<i64>,
    pub expires_at: Option<String>,
    pub created_at: String,
    pub updated_at: Option<String>,
}

impl User {
    pub fn role_enum(&self) -> UserRole {
        UserRole::from_str(&self.role).unwrap_or(UserRole::Artist)
    }

    pub fn is_expired(&self) -> bool {
        if let Some(ref expires_at) = self.expires_at {
            // Compare with current time (ISO 8601 format allows string comparison)
            let now = chrono::Utc::now().format("%Y-%m-%dT%H:%M:%S").to_string();
            expires_at < &now
        } else {
            false
        }
    }
}

/// Status of a recording version in the finalize pipeline
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum RecordingVersionStatus {
    /// Raw recording saved, not yet finalized
    Raw,
    /// Finalize in progress
    Finalizing,
    /// Successfully finalized
    Finalized,
    /// Finalize failed
    Failed,
}

impl RecordingVersionStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            RecordingVersionStatus::Raw => "raw",
            RecordingVersionStatus::Finalizing => "finalizing",
            RecordingVersionStatus::Finalized => "finalized",
            RecordingVersionStatus::Failed => "failed",
        }
    }

    pub fn from_str(s: &str) -> Option<Self> {
        match s.to_lowercase().as_str() {
            "raw" => Some(RecordingVersionStatus::Raw),
            "finalizing" => Some(RecordingVersionStatus::Finalizing),
            "finalized" => Some(RecordingVersionStatus::Finalized),
            "failed" => Some(RecordingVersionStatus::Failed),
            _ => None,
        }
    }
}

impl std::fmt::Display for RecordingVersionStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.as_str())
    }
}

/// A versioned recording for a show (each recording session creates a new version)
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct RecordingVersion {
    pub id: i64,
    pub show_id: i64,
    /// Version identifier (timestamp string like "20260128-143052")
    pub version: String,
    /// Status: raw, finalizing, finalized, failed
    pub status: String,
    /// Duration in milliseconds (populated after recording stops)
    pub duration_ms: Option<i64>,
    /// Number of track markers in this recording
    pub marker_count: i64,
    /// R2 key for raw.webm file
    pub raw_key: Option<String>,
    /// R2 key for markers.json file
    pub markers_key: Option<String>,
    /// R2 key for final.mp3 file (after finalize)
    pub final_key: Option<String>,
    /// Error message if finalize failed
    pub error_message: Option<String>,
    pub created_at: String,
    pub updated_at: Option<String>,
    pub finalized_at: Option<String>,
}

impl RecordingVersion {
    pub fn status_enum(&self) -> RecordingVersionStatus {
        RecordingVersionStatus::from_str(&self.status).unwrap_or(RecordingVersionStatus::Raw)
    }
}

/// Overlay parameter preset (shared across all admin users)
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct OverlayPreset {
    pub id: i64,
    pub name: String,
    /// JSON blob storing all overlay parameters (positions, sizes, colors, filters)
    pub params: String,
    pub created_at: String,
    pub updated_at: String,
    /// 'artist' or 'show'
    pub preset_type: String,
}
