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
