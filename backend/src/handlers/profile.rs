//! Profiles (#1117): photo, display name, About me and social links.
//! Hosts and admins edit their own on /profile; every host and admin can
//! read every profile. The server builds every link, so the page needs no
//! URL rules of its own: it gets them from [`PLATFORMS`].

use std::io::Cursor;
use std::sync::Arc;

use aws_sdk_s3::primitives::ByteStream;
use axum::{
    extract::{Multipart, Path, State},
    http::HeaderMap,
    response::IntoResponse,
    Json,
};
use image::{ImageFormat, ImageReader};
use serde::{Deserialize, Serialize};

use super::api::require_show_creator;
use crate::{models, storage, thumbs, AppError, AppState, Result};

const MAX_NAME: usize = 60;
const MAX_ABOUT: usize = 500;
const MAX_LABEL: usize = 40;
const MAX_VALUE: usize = 300;
const MAX_LINKS: usize = 30;
const MAX_PHOTO_BYTES: usize = 8 * 1024 * 1024;

/// A platform a link can point to. `prefix` "https://" means the host types
/// a whole address; otherwise they type a handle between `prefix` and `suffix`.
#[derive(Debug, Serialize)]
pub struct Platform {
    key: &'static str,
    name: &'static str,
    /// "common" (always in the form), "music", "social" or "other".
    group: &'static str,
    prefix: &'static str,
    suffix: &'static str,
    /// The profile shows the handle as "@handle".
    at: bool,
    hint: &'static str,
}

const ADDRESS: &str = "https://";

const fn handle(
    key: &'static str,
    name: &'static str,
    group: &'static str,
    prefix: &'static str,
    suffix: &'static str,
    at: bool,
    hint: &'static str,
) -> Platform {
    Platform {
        key,
        name,
        group,
        prefix,
        suffix,
        at,
        hint,
    }
}

const fn address(
    key: &'static str,
    name: &'static str,
    group: &'static str,
    hint: &'static str,
) -> Platform {
    handle(key, name, group, ADDRESS, "", false, hint)
}

/// Every platform, in the order of the edit form and its list (flow rev 40).
pub const PLATFORMS: &[Platform] = &[
    handle(
        "instagram",
        "Instagram",
        "common",
        "instagram.com/",
        "",
        true,
        "yourhandle",
    ),
    handle(
        "soundcloud",
        "SoundCloud",
        "common",
        "soundcloud.com/",
        "",
        false,
        "yourhandle",
    ),
    handle(
        "mixcloud",
        "Mixcloud",
        "common",
        "mixcloud.com/",
        "",
        false,
        "yourhandle",
    ),
    handle(
        "bandcamp",
        "Bandcamp",
        "common",
        "",
        ".bandcamp.com",
        false,
        "yourhandle",
    ),
    handle(
        "ra",
        "Resident Advisor",
        "music",
        "ra.co/dj/",
        "",
        false,
        "yourhandle",
    ),
    handle(
        "spotify",
        "Spotify",
        "music",
        "open.spotify.com/artist/",
        "",
        false,
        "artist ID",
    ),
    address("applemusic", "Apple Music", "music", "music.apple.com/…"),
    address("beatport", "Beatport", "music", "beatport.com/artist/…"),
    address("discogs", "Discogs", "music", "discogs.com/artist/…"),
    handle(
        "youtube",
        "YouTube",
        "social",
        "youtube.com/@",
        "",
        true,
        "yourhandle",
    ),
    handle(
        "tiktok",
        "TikTok",
        "social",
        "tiktok.com/@",
        "",
        true,
        "yourhandle",
    ),
    handle(
        "facebook",
        "Facebook",
        "social",
        "facebook.com/",
        "",
        false,
        "yourhandle",
    ),
    handle(
        "x",
        "X (Twitter)",
        "social",
        "x.com/",
        "",
        true,
        "yourhandle",
    ),
    handle(
        "threads",
        "Threads",
        "social",
        "threads.net/@",
        "",
        true,
        "yourhandle",
    ),
    handle(
        "bluesky",
        "Bluesky",
        "social",
        "bsky.app/profile/",
        "",
        false,
        "you.bsky.social",
    ),
    address("mastodon", "Mastodon", "social", "mastodon.social/@you"),
    handle(
        "twitch",
        "Twitch",
        "social",
        "twitch.tv/",
        "",
        false,
        "yourhandle",
    ),
    handle(
        "linktree",
        "Linktree",
        "other",
        "linktr.ee/",
        "",
        false,
        "yourhandle",
    ),
    address("website", "Website", "other", "yourwebsite.com"),
    address("custom", "Custom", "other", "yourlink.com"),
];

fn platform(key: &str) -> Option<&'static Platform> {
    PLATFORMS.iter().find(|p| p.key == key)
}

/// The value to store, the link and the text the profile shows, or why the
/// value is no good. Hosts may paste a whole link or type "@handle".
fn link_for(p: &Platform, value: &str) -> std::result::Result<(String, String, String), String> {
    let value = value.trim();
    if value.chars().count() > MAX_VALUE {
        return Err(format!("{}: at most {MAX_VALUE} characters.", p.name));
    }
    if p.prefix == ADDRESS {
        let full = if value.contains("://") {
            value.to_string()
        } else {
            format!("{ADDRESS}{value}")
        };
        let url = reqwest::Url::parse(&full)
            .ok()
            .filter(|u| matches!(u.scheme(), "http" | "https") && u.host_str().is_some())
            .ok_or_else(|| format!("{}: “{value}” is not a web address.", p.name))?;
        let url = url.to_string();
        let text = url
            .split_once("://")
            .map_or(url.as_str(), |(_, rest)| rest)
            .trim_end_matches('/')
            .to_string();
        return Ok((url.clone(), url, text));
    }
    let mut h = value;
    for scheme in ["https://", "http://"] {
        h = h.strip_prefix(scheme).unwrap_or(h);
    }
    h = h.strip_prefix("www.").unwrap_or(h);
    // `get` and not `[..]`: a cut inside a multi-byte letter must not panic.
    if !p.prefix.is_empty()
        && h.get(..p.prefix.len())
            .is_some_and(|head| head.eq_ignore_ascii_case(p.prefix))
    {
        h = &h[p.prefix.len()..];
    }
    // "open.spotify.com/artist/ID?si=…" → "ID"
    h = h.split(['?', '#']).next().unwrap_or(h);
    if !p.suffix.is_empty() {
        // "lena.bandcamp.com/album/x" → "lena"
        let lower = h.to_ascii_lowercase();
        if let Some(at) = lower.find(p.suffix) {
            h = &h[..at];
        }
    }
    let h = h.trim_start_matches('@').trim_end_matches('/');
    let ok = !h.is_empty()
        && h.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'));
    if !ok {
        return Err(format!(
            "{}: “{value}” is not a handle. Use letters, numbers, dots, dashes or underscores.",
            p.name
        ));
    }
    let url = format!("{ADDRESS}{}{h}{}", p.prefix, p.suffix);
    let text = if p.at {
        format!("@{h}")
    } else {
        format!("{}{h}{}", p.prefix, p.suffix)
    };
    Ok((h.to_string(), url, text))
}

#[derive(Debug, Serialize)]
pub struct Link {
    platform: &'static str,
    /// The platform's name, or the host's own name for a Custom link.
    name: String,
    label: Option<String>,
    /// What the host typed, cleaned up: the handle, or the whole address.
    value: String,
    url: String,
    /// "@lena.brunch", "soundcloud.com/lena-brunch", "lena.example.org"
    text: String,
}

#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct ShowRef {
    slug: String,
    name: String,
}

#[derive(Debug, Serialize)]
pub struct Profile {
    username: String,
    display_name: Option<String>,
    /// The display name, else the username.
    name: String,
    role: String,
    about: Option<String>,
    photo_url: Option<String>,
    /// Small copies of the photo, see `thumbs::srcset`.
    photo_srcset: Option<String>,
    /// The Shows the user owns or co-hosts.
    shows: Vec<ShowRef>,
    links: Vec<Link>,
}

async fn photo_key(state: &AppState, user_id: i64) -> Result<Option<String>> {
    Ok(
        sqlx::query_scalar("SELECT photo_key FROM user_profiles WHERE user_id = ?")
            .bind(user_id)
            .fetch_optional(&state.db)
            .await?
            .flatten(),
    )
}

/// The nav bar's photo of the signed-in user.
pub async fn photo_srcset(state: &AppState, user_id: i64) -> Result<Option<String>> {
    Ok(photo_key(state, user_id)
        .await?
        .map(|key| thumbs::srcset(state, &key)))
}

async fn load(state: &Arc<AppState>, user: &models::User) -> Result<Profile> {
    let row: Option<(Option<String>, Option<String>, Option<String>)> = sqlx::query_as(
        "SELECT display_name, about, photo_key FROM user_profiles WHERE user_id = ?",
    )
    .bind(user.id)
    .fetch_optional(&state.db)
    .await?;
    let (display_name, about, photo_key) = row.unwrap_or_default();
    let stored: Vec<(String, String, Option<String>)> = sqlx::query_as(
        "SELECT platform, value, label FROM user_links WHERE user_id = ? ORDER BY position",
    )
    .bind(user.id)
    .fetch_all(&state.db)
    .await?;
    let links = stored
        .into_iter()
        .filter_map(|(key, value, label)| {
            let p = platform(&key)?;
            let (value, url, text) = link_for(p, &value).ok()?;
            let name = match (&label, p.key) {
                (Some(label), "custom") => label.clone(),
                _ => p.name.to_string(),
            };
            Some(Link {
                platform: p.key,
                name,
                label,
                value,
                url,
                text,
            })
        })
        .collect();
    let shows: Vec<ShowRef> = sqlx::query_as(
        "SELECT slug, name FROM show_templates \
         WHERE slug IS NOT NULL AND (owner_user_id = ?1 OR id IN \
               (SELECT series_id FROM series_hosts WHERE user_id = ?1)) \
         ORDER BY name COLLATE NOCASE, id",
    )
    .bind(user.id)
    .fetch_all(&state.db)
    .await?;
    let photo_url = match &photo_key {
        Some(key) => storage::get_presigned_url(state, key, 3600).await.ok(),
        None => None,
    };
    Ok(Profile {
        name: display_name
            .clone()
            .unwrap_or_else(|| user.username.clone()),
        username: user.username.clone(),
        display_name,
        role: user.role.clone(),
        about,
        photo_srcset: photo_key.as_deref().map(|key| thumbs::srcset(state, key)),
        photo_url,
        shows,
        links,
    })
}

/// GET /api/profile — the signed-in host's or admin's own profile, with the
/// platform list for the edit form.
pub async fn api_get_profile(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let user = require_show_creator(&state, &headers).await?;
    let profile = load(&state, &user).await?;
    Ok(Json(
        serde_json::json!({ "profile": profile, "platforms": PLATFORMS }),
    ))
}

/// GET /api/profiles/:username — another host's or admin's profile, read-only.
pub async fn api_get_user_profile(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(username): Path<String>,
) -> Result<impl IntoResponse> {
    require_show_creator(&state, &headers).await?;
    let user: Option<models::User> = sqlx::query_as("SELECT * FROM users WHERE username = ?")
        .bind(&username)
        .fetch_optional(&state.db)
        .await?;
    let user = user
        .filter(|u| u.role_enum().can_create_show())
        .ok_or_else(|| AppError::NotFound(format!("No profile for “{username}”.")))?;
    Ok(Json(load(&state, &user).await?))
}

#[derive(Debug, Deserialize)]
pub struct LinkIn {
    platform: String,
    #[serde(default)]
    value: String,
    #[serde(default)]
    label: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct ProfileIn {
    #[serde(default)]
    display_name: Option<String>,
    #[serde(default)]
    about: Option<String>,
    #[serde(default)]
    links: Vec<LinkIn>,
}

/// Trimmed text, `None` when empty, or an error past `max` characters.
fn text_field(value: Option<String>, what: &str, max: usize) -> Result<Option<String>> {
    let value = value
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty());
    if value.as_ref().is_some_and(|v| v.chars().count() > max) {
        return Err(AppError::Validation(format!(
            "{what} has at most {max} characters."
        )));
    }
    Ok(value)
}

/// PUT /api/profile — save name, About me and links. Lines with an empty
/// value are dropped; the photo stays.
pub async fn api_save_profile(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(body): Json<ProfileIn>,
) -> Result<impl IntoResponse> {
    let user = require_show_creator(&state, &headers).await?;
    let display_name = text_field(body.display_name, "Name", MAX_NAME)?;
    let about = text_field(body.about, "About me", MAX_ABOUT)?;
    let mut links = Vec::new();
    for link in body
        .links
        .into_iter()
        .filter(|l| !l.value.trim().is_empty())
    {
        let p = platform(&link.platform).ok_or_else(|| {
            AppError::Validation(format!("Unknown platform “{}”.", link.platform))
        })?;
        let label = text_field(link.label, "A link name", MAX_LABEL)?;
        if p.key == "custom" && label.is_none() {
            return Err(AppError::Validation(
                "A custom link needs a name.".to_string(),
            ));
        }
        let (value, _, _) = link_for(p, &link.value).map_err(AppError::Validation)?;
        links.push((p.key, value, label.filter(|_| p.key == "custom")));
    }
    if links.len() > MAX_LINKS {
        return Err(AppError::Validation(format!(
            "A profile has at most {MAX_LINKS} links."
        )));
    }
    let mut tx = state.db.begin().await?;
    sqlx::query(
        "INSERT INTO user_profiles (user_id, display_name, about) VALUES (?, ?, ?) \
         ON CONFLICT(user_id) DO UPDATE SET display_name = excluded.display_name, \
         about = excluded.about, updated_at = datetime('now')",
    )
    .bind(user.id)
    .bind(&display_name)
    .bind(&about)
    .execute(&mut *tx)
    .await?;
    sqlx::query("DELETE FROM user_links WHERE user_id = ?")
        .bind(user.id)
        .execute(&mut *tx)
        .await?;
    for (position, (key, value, label)) in links.iter().enumerate() {
        sqlx::query(
            "INSERT INTO user_links (user_id, position, platform, value, label) VALUES (?, ?, ?, ?, ?)",
        )
        .bind(user.id)
        .bind(position as i64)
        .bind(key)
        .bind(value)
        .bind(label)
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    Ok(Json(load(&state, &user).await?))
}

async fn set_photo_key(state: &AppState, user_id: i64, key: Option<&str>) -> Result<()> {
    sqlx::query(
        "INSERT INTO user_profiles (user_id, photo_key) VALUES (?, ?) \
         ON CONFLICT(user_id) DO UPDATE SET photo_key = excluded.photo_key, \
         updated_at = datetime('now')",
    )
    .bind(user_id)
    .bind(key)
    .execute(&state.db)
    .await?;
    Ok(())
}

fn photo_object(user_id: i64) -> String {
    format!("users/{user_id}/photo")
}

/// POST /api/profile/photo — multipart field `photo`, a JPG or PNG up to 8 MB.
pub async fn api_upload_photo(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    mut multipart: Multipart,
) -> Result<impl IntoResponse> {
    let user = require_show_creator(&state, &headers).await?;
    let mut data = None;
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|e| AppError::BadRequest(format!("Failed to read multipart field: {e}")))?
    {
        if field.name() == Some("photo") {
            data = Some(
                field
                    .bytes()
                    .await
                    .map_err(|e| AppError::BadRequest(format!("Failed to read the photo: {e}")))?,
            );
        }
    }
    let data = data
        .filter(|d| !d.is_empty())
        .ok_or_else(|| AppError::BadRequest("No photo in the upload.".to_string()))?;
    if data.len() > MAX_PHOTO_BYTES {
        return Err(AppError::Validation(
            "The photo is larger than 8 MB.".to_string(),
        ));
    }
    let reader = ImageReader::new(Cursor::new(&data[..]))
        .with_guessed_format()
        .map_err(|e| AppError::Internal(format!("Failed to read the photo: {e}")))?;
    let content_type = match reader.format() {
        Some(ImageFormat::Png) => "image/png",
        Some(ImageFormat::Jpeg) => "image/jpeg",
        _ => return Err(AppError::Validation("Use a JPG or PNG photo.".to_string())),
    };
    // Reads the header only: a broken file is refused before it is stored.
    reader
        .into_dimensions()
        .map_err(|_| AppError::Validation("The photo cannot be read.".to_string()))?;
    let key = photo_object(user.id);
    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&key)
        .body(ByteStream::from(data.to_vec()))
        .content_type(content_type)
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload the photo: {e}")))?;
    set_photo_key(&state, user.id, Some(&key)).await?;
    Ok(Json(load(&state, &user).await?))
}

/// DELETE /api/profile/photo — back to the placeholder.
pub async fn api_remove_photo(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let user = require_show_creator(&state, &headers).await?;
    if let Some(key) = photo_key(&state, user.id).await? {
        state
            .s3_client
            .delete_object()
            .bucket(&state.config.r2_bucket_name)
            .key(&key)
            .send()
            .await
            .map_err(|e| AppError::Storage(format!("Failed to delete the photo: {e}")))?;
    }
    set_photo_key(&state, user.id, None).await?;
    Ok(Json(load(&state, &user).await?))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::broadcast_tests::{add_user, headers, state, state_with};
    use crate::fake_r2::FakeR2;
    use axum::{
        body::{to_bytes, Body},
        extract::FromRequest,
        http::{Request, StatusCode},
    };
    use serde_json::{json, Value};

    async fn json_of(response: impl IntoResponse) -> Value {
        let response = response.into_response();
        assert_eq!(response.status(), StatusCode::OK);
        serde_json::from_slice(&to_bytes(response.into_body(), 1 << 20).await.unwrap()).unwrap()
    }

    async fn save(state: &Arc<AppState>, user: i64, body: Value) -> Result<Value> {
        let body: ProfileIn = serde_json::from_value(body).unwrap();
        let response =
            api_save_profile(State(state.clone()), headers(state, user).await, Json(body)).await?;
        Ok(json_of(response).await)
    }

    async fn own(state: &Arc<AppState>, user: i64) -> Value {
        json_of(
            api_get_profile(State(state.clone()), headers(state, user).await)
                .await
                .unwrap(),
        )
        .await
    }

    async fn of(state: &Arc<AppState>, viewer: i64, username: &str) -> Result<Value> {
        let response = api_get_user_profile(
            State(state.clone()),
            headers(state, viewer).await,
            Path(username.to_string()),
        )
        .await?;
        Ok(json_of(response).await)
    }

    fn validation(result: Result<Value>) -> String {
        match result {
            Err(AppError::Validation(msg)) => msg,
            other => panic!("expected a validation error, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn saves_name_about_and_links_in_order() {
        let (state, _temp) = state().await;
        let lena = add_user(&state, "lena", "host").await;
        let profile = save(
            &state,
            lena,
            json!({
                "display_name": "  Lena K. ",
                "about": "Sunday brunch sounds.",
                "links": [
                    { "platform": "instagram", "value": "@lena.brunch" },
                    { "platform": "soundcloud", "value": "https://soundcloud.com/lena-brunch/" },
                    { "platform": "mixcloud", "value": "  " },
                    { "platform": "bandcamp", "value": "https://lenabrunch.bandcamp.com/album/x" },
                    { "platform": "website", "value": "lena.example.org" },
                    { "platform": "youtube", "value": "lenatv" },
                    { "platform": "youtube", "value": "lenalive" },
                    { "platform": "custom", "label": "Mixtapes", "value": "https://lena.example.org/mixes" }
                ]
            }),
        )
        .await
        .unwrap();

        assert_eq!(profile["name"], "Lena K.");
        assert_eq!(profile["username"], "lena");
        assert_eq!(profile["about"], "Sunday brunch sounds.");
        let links: Vec<(String, String, String)> = profile["links"]
            .as_array()
            .unwrap()
            .iter()
            .map(|l| {
                (
                    l["name"].as_str().unwrap().to_string(),
                    l["text"].as_str().unwrap().to_string(),
                    l["url"].as_str().unwrap().to_string(),
                )
            })
            .collect();
        let expected = [
            (
                "Instagram",
                "@lena.brunch",
                "https://instagram.com/lena.brunch",
            ),
            (
                "SoundCloud",
                "soundcloud.com/lena-brunch",
                "https://soundcloud.com/lena-brunch",
            ),
            (
                "Bandcamp",
                "lenabrunch.bandcamp.com",
                "https://lenabrunch.bandcamp.com",
            ),
            ("Website", "lena.example.org", "https://lena.example.org/"),
            ("YouTube", "@lenatv", "https://youtube.com/@lenatv"),
            ("YouTube", "@lenalive", "https://youtube.com/@lenalive"),
            (
                "Mixtapes",
                "lena.example.org/mixes",
                "https://lena.example.org/mixes",
            ),
        ];
        assert_eq!(
            links,
            expected
                .iter()
                .map(|(a, b, c)| (a.to_string(), b.to_string(), c.to_string()))
                .collect::<Vec<_>>()
        );
        // The stored handle is clean, so the edit form shows "lena.brunch".
        assert_eq!(profile["links"][0]["value"], "lena.brunch");

        // Saving again replaces the links and keeps nothing old.
        let profile = save(&state, lena, json!({ "links": [] })).await.unwrap();
        assert_eq!(profile["links"], json!([]));
        assert_eq!(profile["name"], "lena");
        assert_eq!(profile["about"], Value::Null);
    }

    #[tokio::test]
    async fn refuses_bad_values() {
        let (state, _temp) = state().await;
        let lena = add_user(&state, "lena", "host").await;
        let link = |platform: &str, value: &str| json!({ "links": [{ "platform": platform, "value": value }] });

        assert!(
            validation(save(&state, lena, link("custom", "javascript:alert(1)")).await)
                .contains("needs a name")
        );
        let bad_scheme = json!({ "links": [{ "platform": "custom", "label": "x", "value": "javascript:alert(1)" }] });
        assert!(validation(save(&state, lena, bad_scheme).await).contains("not a web address"));
        assert!(
            validation(save(&state, lena, link("website", "ftp://lena.example.org")).await)
                .contains("not a web address")
        );
        assert!(
            validation(save(&state, lena, link("instagram", "lena brunch")).await)
                .contains("not a handle")
        );
        assert!(
            validation(save(&state, lena, link("myspace", "lena")).await)
                .contains("Unknown platform")
        );
        let long = json!({ "about": "x".repeat(501) });
        assert!(validation(save(&state, lena, long).await).contains("at most 500"));
        // Nothing was saved.
        assert_eq!(own(&state, lena).await["profile"]["links"], json!([]));
    }

    #[tokio::test]
    async fn other_hosts_read_it_and_guests_do_not() {
        let (state, _temp) = state().await;
        let lena = add_user(&state, "lena", "host").await;
        let jonas = add_user(&state, "jonas", "host").await;
        let guest = add_user(&state, "dj-guest", "guest").await;
        let series = sqlx::query(
            "INSERT INTO show_templates (name, slug, owner_user_id) VALUES ('Night Shift', 'night-shift', ?)",
        )
        .bind(jonas)
        .execute(&state.db)
        .await
        .unwrap()
        .last_insert_rowid();
        sqlx::query(
            "INSERT INTO show_templates (name, slug, owner_user_id) VALUES ('Brunchtime', 'brunchtime', ?)",
        )
        .bind(lena)
        .execute(&state.db)
        .await
        .unwrap();
        sqlx::query("INSERT INTO series_hosts (series_id, user_id) VALUES (?, ?)")
            .bind(series)
            .bind(lena)
            .execute(&state.db)
            .await
            .unwrap();
        save(
            &state,
            jonas,
            json!({ "links": [{ "platform": "mixcloud", "value": "nightshift" }] }),
        )
        .await
        .unwrap();

        let profile = of(&state, lena, "jonas").await.unwrap();
        assert_eq!(profile["name"], "jonas");
        assert_eq!(
            profile["links"][0]["url"],
            "https://mixcloud.com/nightshift"
        );
        assert_eq!(
            profile["shows"],
            json!([{ "slug": "night-shift", "name": "Night Shift" }])
        );
        // A co-hosted Show counts as the host's own.
        let shows = &own(&state, lena).await["profile"]["shows"];
        assert_eq!(shows.as_array().unwrap().len(), 2);
        // The platform list comes only with the own profile.
        assert!(
            own(&state, lena).await["platforms"]
                .as_array()
                .unwrap()
                .len()
                > 10
        );

        assert!(matches!(
            of(&state, guest, "jonas").await,
            Err(AppError::Forbidden(_))
        ));
        assert!(matches!(
            of(&state, lena, "dj-guest").await,
            Err(AppError::NotFound(_))
        ));
        assert!(matches!(
            of(&state, lena, "nobody").await,
            Err(AppError::NotFound(_))
        ));
    }

    fn png() -> Vec<u8> {
        let mut out = Cursor::new(Vec::new());
        image::RgbImage::new(2, 2)
            .write_to(&mut out, ImageFormat::Png)
            .unwrap();
        out.into_inner()
    }

    async fn photo_form(data: &[u8]) -> Multipart {
        const BOUNDARY: &str = "profile-test-boundary";
        let mut body = format!(
            "--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"me.png\"\r\n\
             Content-Type: image/png\r\n\r\n"
        )
        .into_bytes();
        body.extend_from_slice(data);
        body.extend_from_slice(format!("\r\n--{BOUNDARY}--\r\n").as_bytes());
        let request = Request::builder()
            .header(
                "content-type",
                format!("multipart/form-data; boundary={BOUNDARY}"),
            )
            .body(Body::from(body))
            .unwrap();
        Multipart::from_request(request, &()).await.unwrap()
    }

    async fn upload(state: &Arc<AppState>, user: i64, data: &[u8]) -> Result<Value> {
        let response = api_upload_photo(
            State(state.clone()),
            headers(state, user).await,
            photo_form(data).await,
        )
        .await?;
        Ok(json_of(response).await)
    }

    #[tokio::test]
    async fn photo_upload_and_remove() {
        let (r2, endpoint) = FakeR2::serve().await;
        let (state, _temp) = state_with(json!({ "r2_endpoint": endpoint })).await;
        let lena = add_user(&state, "lena", "host").await;

        assert!(validation(upload(&state, lena, b"not an image").await).contains("JPG or PNG"));
        assert_eq!(r2.object(&state, &format!("users/{lena}/photo")), None);

        let profile = upload(&state, lena, &png()).await.unwrap();
        assert!(profile["photo_srcset"]
            .as_str()
            .unwrap()
            .contains("/api/img?"));
        assert_eq!(
            r2.object(&state, &format!("users/{lena}/photo")),
            Some(png())
        );
        assert!(photo_srcset(&state, lena).await.unwrap().is_some());

        // Saving the text keeps the photo.
        save(&state, lena, json!({ "about": "Hi" })).await.unwrap();
        assert!(photo_srcset(&state, lena).await.unwrap().is_some());

        let response = api_remove_photo(State(state.clone()), headers(&state, lena).await)
            .await
            .unwrap();
        assert_eq!(json_of(response).await["photo_srcset"], Value::Null);
        assert_eq!(r2.object(&state, &format!("users/{lena}/photo")), None);
        assert_eq!(own(&state, lena).await["profile"]["about"], "Hi");
    }
}
