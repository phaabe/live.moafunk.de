//! Instagram Graph API client for posting show covers to Instagram
//!
//! This module handles:
//! - Creating media containers with image URLs
//! - Publishing containers to Instagram feed
//! - Automatic token refresh before expiry
//!
//! Reference: https://developers.facebook.com/docs/instagram-platform/content-publishing

use crate::{storage, AppError, AppState, Result};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

const GRAPH_API_VERSION: &str = "v24.0";
const GRAPH_API_BASE: &str = "https://graph.instagram.com";

/// Instagram API error response
#[derive(Debug, Deserialize)]
struct InstagramErrorResponse {
    error: InstagramError,
}

#[derive(Debug, Deserialize)]
struct InstagramError {
    message: String,
    #[allow(dead_code)]
    #[serde(rename = "type")]
    error_type: Option<String>,
    code: Option<i32>,
}

/// Response from creating a media container
#[derive(Debug, Deserialize)]
struct CreateContainerResponse {
    id: String,
}

/// Response from publishing a media container
#[derive(Debug, Deserialize)]
struct PublishResponse {
    id: String,
}

/// Response from checking container status
#[derive(Debug, Deserialize)]
struct ContainerStatusResponse {
    status_code: Option<String>,
}

/// Response from token refresh
#[derive(Debug, Deserialize)]
#[allow(dead_code)]
pub struct TokenRefreshResponse {
    pub access_token: String,
    pub token_type: String,
    pub expires_in: i64,
}

/// Result of posting to Instagram
#[derive(Debug, Serialize)]
pub struct InstagramPostResult {
    pub success: bool,
    pub media_id: Option<String>,
    pub error: Option<String>,
}

/// Instagram client wrapping reqwest
pub struct InstagramClient {
    http: reqwest::Client,
    access_token: String,
    business_account_id: String,
}

impl InstagramClient {
    /// Create a new Instagram client from config
    pub fn from_config(config: &crate::Config) -> Result<Self> {
        let access_token = config
            .instagram_access_token
            .clone()
            .ok_or_else(|| AppError::Config("INSTAGRAM_ACCESS_TOKEN not configured".to_string()))?;

        let business_account_id =
            config
                .instagram_business_account_id
                .clone()
                .ok_or_else(|| {
                    AppError::Config("INSTAGRAM_BUSINESS_ACCOUNT_ID not configured".to_string())
                })?;

        Ok(Self {
            http: reqwest::Client::new(),
            access_token,
            business_account_id,
        })
    }

    /// Create a media container for an image post
    ///
    /// Instagram requires the image to be hosted at a publicly accessible URL.
    /// We use a presigned R2 URL that's valid for 1 hour.
    async fn create_container(&self, image_url: &str, caption: &str) -> Result<String> {
        let url = format!(
            "{}/{}/{}/media",
            GRAPH_API_BASE, GRAPH_API_VERSION, self.business_account_id
        );

        let params = [
            ("image_url", image_url),
            ("caption", caption),
            ("access_token", &self.access_token),
        ];

        let response = self
            .http
            .post(&url)
            .form(&params)
            .send()
            .await
            .map_err(|e| {
                AppError::External(format!("Failed to create Instagram container: {}", e))
            })?;

        let status = response.status();
        let body = response
            .text()
            .await
            .map_err(|e| AppError::External(format!("Failed to read Instagram response: {}", e)))?;

        if !status.is_success() {
            // Try to parse error response
            if let Ok(error_resp) = serde_json::from_str::<InstagramErrorResponse>(&body) {
                return Err(AppError::External(format!(
                    "Instagram API error: {} (code: {:?})",
                    error_resp.error.message, error_resp.error.code
                )));
            }
            return Err(AppError::External(format!(
                "Instagram API error: {} - {}",
                status, body
            )));
        }

        let container: CreateContainerResponse = serde_json::from_str(&body).map_err(|e| {
            AppError::External(format!(
                "Failed to parse Instagram response: {} - {}",
                e, body
            ))
        })?;

        Ok(container.id)
    }

    /// Check the status of a media container
    /// Returns the status code: EXPIRED, ERROR, FINISHED, IN_PROGRESS, PUBLISHED
    async fn check_container_status(&self, container_id: &str) -> Result<String> {
        let url = format!("{}/{}/{}", GRAPH_API_BASE, GRAPH_API_VERSION, container_id);

        let params = [
            ("fields", "status_code"),
            ("access_token", &self.access_token),
        ];

        let response = self
            .http
            .get(&url)
            .query(&params)
            .send()
            .await
            .map_err(|e| AppError::External(format!("Failed to check container status: {}", e)))?;

        let status = response.status();
        let body = response
            .text()
            .await
            .map_err(|e| AppError::External(format!("Failed to read status response: {}", e)))?;

        if !status.is_success() {
            return Err(AppError::External(format!(
                "Instagram API error checking status: {} - {}",
                status, body
            )));
        }

        let status_resp: ContainerStatusResponse = serde_json::from_str(&body).map_err(|e| {
            AppError::External(format!("Failed to parse status response: {} - {}", e, body))
        })?;

        Ok(status_resp
            .status_code
            .unwrap_or_else(|| "UNKNOWN".to_string()))
    }

    /// Publish a media container to Instagram feed
    async fn publish_container(&self, container_id: &str) -> Result<String> {
        let url = format!(
            "{}/{}/{}/media_publish",
            GRAPH_API_BASE, GRAPH_API_VERSION, self.business_account_id
        );

        let params = [
            ("creation_id", container_id),
            ("access_token", &self.access_token),
        ];

        let response = self
            .http
            .post(&url)
            .form(&params)
            .send()
            .await
            .map_err(|e| AppError::External(format!("Failed to publish Instagram media: {}", e)))?;

        let status = response.status();
        let body = response
            .text()
            .await
            .map_err(|e| AppError::External(format!("Failed to read publish response: {}", e)))?;

        if !status.is_success() {
            if let Ok(error_resp) = serde_json::from_str::<InstagramErrorResponse>(&body) {
                return Err(AppError::External(format!(
                    "Instagram publish error: {} (code: {:?})",
                    error_resp.error.message, error_resp.error.code
                )));
            }
            return Err(AppError::External(format!(
                "Instagram publish error: {} - {}",
                status, body
            )));
        }

        let publish_resp: PublishResponse = serde_json::from_str(&body).map_err(|e| {
            AppError::External(format!(
                "Failed to parse publish response: {} - {}",
                e, body
            ))
        })?;

        Ok(publish_resp.id)
    }

    /// Post an image to Instagram feed
    ///
    /// This is the main entry point for posting. It:
    /// 1. Creates a media container with the image URL and caption
    /// 2. Polls until the container is ready
    /// 3. Publishes the container to the feed
    pub async fn post_image(&self, image_url: &str, caption: &str) -> Result<InstagramPostResult> {
        tracing::info!("Creating Instagram container for image: {}", image_url);

        // Step 1: Create container
        let container_id = match self.create_container(image_url, caption).await {
            Ok(id) => id,
            Err(e) => {
                tracing::error!("Failed to create Instagram container: {}", e);
                return Ok(InstagramPostResult {
                    success: false,
                    media_id: None,
                    error: Some(e.to_string()),
                });
            }
        };

        tracing::info!(
            "Created container: {}, waiting for processing...",
            container_id
        );

        // Step 2: Poll until container is ready (max 30 attempts, ~60s)
        const MAX_POLL_ATTEMPTS: u32 = 30;
        const POLL_INTERVAL: std::time::Duration = std::time::Duration::from_secs(2);

        for attempt in 1..=MAX_POLL_ATTEMPTS {
            let status = self.check_container_status(&container_id).await?;
            tracing::info!(
                "Container {} status: {} (attempt {}/{})",
                container_id,
                status,
                attempt,
                MAX_POLL_ATTEMPTS
            );

            match status.as_str() {
                "FINISHED" => break,
                "ERROR" | "EXPIRED" => {
                    return Ok(InstagramPostResult {
                        success: false,
                        media_id: None,
                        error: Some(format!(
                            "Instagram container processing failed with status: {}",
                            status
                        )),
                    });
                }
                "IN_PROGRESS" | _ => {
                    if attempt == MAX_POLL_ATTEMPTS {
                        return Ok(InstagramPostResult {
                            success: false,
                            media_id: None,
                            error: Some(
                                "Instagram container processing timed out after 60s".to_string(),
                            ),
                        });
                    }
                    tokio::time::sleep(POLL_INTERVAL).await;
                }
            }
        }

        // Step 3: Publish container
        tracing::info!("Container {} ready, publishing...", container_id);
        let media_id = match self.publish_container(&container_id).await {
            Ok(id) => id,
            Err(e) => {
                tracing::error!("Failed to publish Instagram container: {}", e);
                return Ok(InstagramPostResult {
                    success: false,
                    media_id: None,
                    error: Some(e.to_string()),
                });
            }
        };

        tracing::info!("Successfully published to Instagram: {}", media_id);

        Ok(InstagramPostResult {
            success: true,
            media_id: Some(media_id),
            error: None,
        })
    }
}

/// Refresh a long-lived access token
///
/// Long-lived tokens are valid for 60 days. This function exchanges
/// an existing token for a new one, extending validity.
///
/// Should be called when token is about to expire (e.g., within 7 days).
#[allow(dead_code)]
pub async fn refresh_access_token(
    current_token: &str,
    _app_id: &str,
    _app_secret: &str,
) -> Result<TokenRefreshResponse> {
    let client = reqwest::Client::new();

    let url = format!(
        "{}/{}/oauth/access_token",
        GRAPH_API_BASE, GRAPH_API_VERSION
    );

    let params = [
        ("grant_type", "ig_refresh_token"),
        ("access_token", current_token),
    ];

    let response = client
        .get(&url)
        .query(&params)
        .send()
        .await
        .map_err(|e| AppError::External(format!("Failed to refresh token: {}", e)))?;

    let status = response.status();
    let body = response
        .text()
        .await
        .map_err(|e| AppError::External(format!("Failed to read refresh response: {}", e)))?;

    if !status.is_success() {
        return Err(AppError::External(format!(
            "Token refresh error: {} - {}",
            status, body
        )));
    }

    let token_resp: TokenRefreshResponse = serde_json::from_str(&body).map_err(|e| {
        AppError::External(format!("Failed to parse token response: {} - {}", e, body))
    })?;

    Ok(token_resp)
}

/// Post a show's cover image to Instagram
///
/// This is the high-level function called by the API handler.
/// It:
/// 1. Generates a presigned URL for the cover image (valid for 1 hour)
/// 2. Builds the caption from show title, date, and description
/// 3. Posts to Instagram via the Graph API
pub async fn post_show_to_instagram(
    state: &Arc<AppState>,
    show: &crate::models::Show,
) -> Result<InstagramPostResult> {
    // Check if Instagram is configured
    let client = InstagramClient::from_config(&state.config)?;

    // Check if cover exists
    if show.cover_generated_at.is_none() {
        return Ok(InstagramPostResult {
            success: false,
            media_id: None,
            error: Some("Show has no cover image. Assign artists first.".to_string()),
        });
    }

    // Generate presigned URL for the cover image (1 hour validity)
    let cover_key = format!("shows/{}/cover.png", show.id);
    let cover_url = storage::get_presigned_url(state, &cover_key, 3600).await?;

    // Build caption
    let mut caption = format!("{} - {}", show.title, show.date);
    if let Some(ref desc) = show.description {
        if !desc.is_empty() {
            caption.push_str("\n\n");
            caption.push_str(desc);
        }
    }

    // Fetch assigned artists and append their Instagram profiles
    let artists: Vec<(String, Option<String>)> = sqlx::query_as(
        "SELECT a.name, a.instagram FROM artists a \
         INNER JOIN artist_show_assignments asa ON a.id = asa.artist_id \
         WHERE asa.show_id = ? \
         ORDER BY a.name",
    )
    .bind(show.id)
    .fetch_all(&state.db)
    .await?;

    if !artists.is_empty() {
        caption.push_str("\n💛\n\n");
        for (name, instagram) in &artists {
            if let Some(ig) = instagram {
                if !ig.is_empty() {
                    // Extract @handle from URL like https://instagram.com/handle
                    let handle = ig.trim_end_matches('/').rsplit('/').next().unwrap_or(ig);
                    caption.push_str(&format!("@{}\n", handle));
                } else {
                    caption.push_str(&format!("{}\n", name));
                }
            } else {
                caption.push_str(&format!("{}\n", name));
            }
        }
    }

    // Post to Instagram
    client.post_image(&cover_url, &caption).await
}

/// Post an artist's image to Instagram with their generated caption
///
/// Uses the artist's overlay (branded) image and the pre-generated caption
/// stored in `artists.instagram_caption`.
pub async fn post_artist_to_instagram(
    state: &Arc<AppState>,
    artist: &crate::models::Artist,
) -> Result<InstagramPostResult> {
    let client = InstagramClient::from_config(&state.config)?;

    // Require a stored caption
    let caption = artist.instagram_caption.as_deref().ok_or_else(|| {
        AppError::Validation("Artist has no Instagram caption. Generate one first.".to_string())
    })?;

    // Use overlay → cropped → original pic, in order of preference
    let pic_key = artist
        .pic_overlay_key
        .as_ref()
        .or(artist.pic_cropped_key.as_ref())
        .or(artist.pic_key.as_ref())
        .ok_or_else(|| {
            AppError::Validation("Artist has no profile picture to post.".to_string())
        })?;

    // Generate presigned URL (1 hour validity)
    let image_url = storage::get_presigned_url(state, pic_key, 3600).await?;

    tracing::info!(
        "Posting artist {} (id={}) to Instagram with key: {}",
        artist.name,
        artist.id,
        pic_key
    );

    client.post_image(&image_url, caption).await
}
