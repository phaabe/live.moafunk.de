use serde::{Deserialize, Serialize};
use sqlx::FromRow;

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

    pub instagram: Option<String>,
    pub soundcloud: Option<String>,
    pub bandcamp: Option<String>,
    pub spotify: Option<String>,
    pub other_social: Option<String>,

    pub upcoming_events: Option<String>,
    pub mentions: Option<String>,

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
    pub created_at: String,
    pub updated_at: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ArtistWithShows {
    #[serde(flatten)]
    pub artist: Artist,
    pub shows: Vec<Show>,
}

#[derive(Debug, Serialize)]
pub struct SubmitResponse {
    pub success: bool,
    pub message: String,
    pub artist_id: Option<i64>,
}

#[derive(Debug, Deserialize)]
pub struct CreateShowForm {
    pub title: String,
    pub date: String,
    pub description: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct AssignArtistForm {
    pub artist_id: i64,
}

#[derive(Debug, Deserialize)]
pub struct AssignShowForm {
    pub show_id: i64,
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
        matches!(self, UserRole::Superadmin | UserRole::Admin)
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

#[derive(Debug, Deserialize)]
pub struct CreateUserForm {
    pub username: String,
    pub role: String,
    pub expires_at: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct ChangePasswordForm {
    pub current_password: String,
    pub new_password: String,
    pub confirm_password: String,
}
