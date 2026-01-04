use serde::{Deserialize, Serialize};
use sqlx::FromRow;

#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct Artist {
    pub id: i64,
    pub name: String,
    pub pronouns: String,

    pub pic_key: Option<String>,
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
pub struct ShowWithArtists {
    #[serde(flatten)]
    pub show: Show,
    pub artists: Vec<Artist>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ArtistWithShows {
    #[serde(flatten)]
    pub artist: Artist,
    pub shows: Vec<Show>,
}

#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct Session {
    pub token: String,
    pub created_at: String,
    pub expires_at: String,
}

// Form submission types
#[derive(Debug, Deserialize)]
pub struct SubmitFormData {
    #[serde(rename = "artist-name")]
    pub artist_name: String,
    pub pronouns: String,
    #[serde(rename = "track1-name")]
    pub track1_name: String,
    #[serde(rename = "track2-name")]
    pub track2_name: String,
    #[serde(rename = "no-voice-message", default)]
    pub no_voice_message: bool,
    pub instagram: Option<String>,
    pub soundcloud: Option<String>,
    pub bandcamp: Option<String>,
    pub spotify: Option<String>,
    #[serde(rename = "other-social")]
    pub other_social: Option<String>,
    #[serde(rename = "upcoming-events")]
    pub upcoming_events: Option<String>,
    pub mentions: Option<String>,
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
pub struct StatusUpdateForm {
    pub status: String,
}

#[derive(Debug, Deserialize)]
pub struct AssignArtistForm {
    pub artist_id: i64,
}

#[derive(Debug, Deserialize)]
pub struct AssignShowForm {
    pub show_id: i64,
}
