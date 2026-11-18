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
use sqlx::SqlitePool;
use std::sync::Arc;

const GRAPH_API_VERSION: &str = "v24.0";

/// Graph API host. Tests point it at a local fake.
fn api_base() -> String {
    #[cfg(not(test))]
    let base = "https://graph.instagram.com".to_string();
    #[cfg(test)]
    let base = crate::fake_instagram::API_BASE.with(|base| base.borrow().clone());
    base
}

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
    pub permalink: Option<String>,
    pub error: Option<String>,
}

/// A published story or feed post.
#[derive(Debug, Clone, Serialize)]
pub struct PublishedMedia {
    pub media_id: String,
    pub permalink: Option<String>,
}

/// Instagram client wrapping reqwest
pub struct InstagramClient {
    http: reqwest::Client,
    access_token: String,
    business_account_id: String,
}

impl InstagramClient {
    /// Create a new Instagram client from config for the specified account.
    ///
    /// `account` should be `"dev"` (moafunk_tester) or `"prod"` (moafunk_radio).
    /// Defaults to `"dev"` if not specified or unrecognised.
    pub fn from_config(config: &crate::Config, account: &str) -> Result<Self> {
        let (access_token, business_account_id) = match account {
            "prod" => {
                let token = config.instagram_access_token_prod.clone().ok_or_else(|| {
                    AppError::Config("INSTAGRAM_ACCESS_TOKEN_PROD not configured".to_string())
                })?;
                let id = config
                    .instagram_business_account_id_prod
                    .clone()
                    .ok_or_else(|| {
                        AppError::Config(
                            "INSTAGRAM_BUSINESS_ACCOUNT_ID_PROD not configured".to_string(),
                        )
                    })?;
                (token, id)
            }
            _ => {
                let token = config.instagram_access_token_dev.clone().ok_or_else(|| {
                    AppError::Config("INSTAGRAM_ACCESS_TOKEN_DEV not configured".to_string())
                })?;
                let id = config
                    .instagram_business_account_id_dev
                    .clone()
                    .ok_or_else(|| {
                        AppError::Config(
                            "INSTAGRAM_BUSINESS_ACCOUNT_ID_DEV not configured".to_string(),
                        )
                    })?;
                (token, id)
            }
        };

        tracing::info!(
            "Using Instagram account: {} ({})",
            if account == "prod" {
                "moafunk_radio"
            } else {
                "moafunk_tester"
            },
            account
        );

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
            api_base(),
            GRAPH_API_VERSION,
            self.business_account_id
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
        let url = format!("{}/{}/{}", api_base(), GRAPH_API_VERSION, container_id);

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
            .map_err(|e| {
                AppError::External(format!(
                    "Failed to check container status: {}",
                    e.without_url()
                ))
            })?;

        let status = response.status();
        let body = response.text().await.map_err(|e| {
            AppError::External(format!(
                "Failed to read status response: {}",
                e.without_url()
            ))
        })?;

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

    /// Fetch the permalink for a published media post.
    ///
    /// After publishing, the `media_id` returned by the API can be used
    /// to look up the direct post URL via `GET /{media_id}?fields=permalink`.
    async fn fetch_permalink(&self, media_id: &str) -> Option<String> {
        let url = format!("{}/{}/{}", api_base(), GRAPH_API_VERSION, media_id);
        let params = [
            ("fields", "permalink"),
            ("access_token", &self.access_token),
        ];

        #[derive(Deserialize)]
        struct PermalinkResponse {
            permalink: Option<String>,
        }

        match self.http.get(&url).query(&params).send().await {
            Ok(resp) if resp.status().is_success() => {
                match resp.json::<PermalinkResponse>().await {
                    Ok(pr) => pr.permalink,
                    Err(e) => {
                        tracing::warn!("Failed to parse permalink response: {}", e.without_url());
                        None
                    }
                }
            }
            Ok(resp) => {
                tracing::warn!("Permalink fetch returned {}", resp.status());
                None
            }
            Err(e) => {
                tracing::warn!("Failed to fetch permalink: {}", e.without_url());
                None
            }
        }
    }

    /// Publish a media container to Instagram feed
    async fn publish_container(&self, container_id: &str) -> Result<String> {
        let url = format!(
            "{}/{}/{}/media_publish",
            api_base(),
            GRAPH_API_VERSION,
            self.business_account_id
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

    /// Publish a ready container. Instagram sometimes reports FINISHED before
    /// the media reaches the publish endpoint ("Media ID is not available",
    /// code 9007), so that error is retried.
    async fn publish_with_retry(&self, container_id: &str) -> Result<String> {
        const MAX_PUBLISH_RETRIES: u32 = 3;
        const PUBLISH_RETRY_DELAY: std::time::Duration = std::time::Duration::from_secs(5);

        let mut attempt = 1;
        loop {
            tracing::info!(
                "Container {} ready, publishing (attempt {}/{})...",
                container_id,
                attempt,
                MAX_PUBLISH_RETRIES
            );
            match self.publish_container(container_id).await {
                Err(e) if attempt < MAX_PUBLISH_RETRIES && e.to_string().contains("9007") => {
                    tracing::warn!(
                        "Publish attempt {} failed with 9007, retrying in {}s...",
                        attempt,
                        PUBLISH_RETRY_DELAY.as_secs()
                    );
                    tokio::time::sleep(PUBLISH_RETRY_DELAY).await;
                    attempt += 1;
                }
                result => return result,
            }
        }
    }

    // ────────────────────────────────────────────────────────────────────────
    // Carousel API methods
    // ────────────────────────────────────────────────────────────────────────

    /// Create a carousel image item container.
    ///
    /// `is_carousel_item=true` tells Instagram this is a child of a carousel,
    /// not a standalone post. No caption is set on individual items.
    async fn create_carousel_image_item(&self, image_url: &str) -> Result<String> {
        let url = format!(
            "{}/{}/{}/media",
            api_base(),
            GRAPH_API_VERSION,
            self.business_account_id
        );

        let params = [
            ("image_url", image_url),
            ("is_carousel_item", "true"),
            ("access_token", &self.access_token),
        ];

        let response = self
            .http
            .post(&url)
            .form(&params)
            .send()
            .await
            .map_err(|e| {
                AppError::External(format!("Failed to create carousel image item: {}", e))
            })?;

        let status = response.status();
        let body = response.text().await.map_err(|e| {
            AppError::External(format!("Failed to read carousel image response: {}", e))
        })?;

        if !status.is_success() {
            if let Ok(error_resp) = serde_json::from_str::<InstagramErrorResponse>(&body) {
                return Err(AppError::External(format!(
                    "Instagram carousel image error: {} (code: {:?})",
                    error_resp.error.message, error_resp.error.code
                )));
            }
            return Err(AppError::External(format!(
                "Instagram carousel image error: {} - {}",
                status, body
            )));
        }

        let container: CreateContainerResponse = serde_json::from_str(&body).map_err(|e| {
            AppError::External(format!(
                "Failed to parse carousel image response: {} - {}",
                e, body
            ))
        })?;

        tracing::info!("Created carousel image item: {}", container.id);
        Ok(container.id)
    }

    /// Create a carousel video item container.
    ///
    /// Videos require `media_type=VIDEO` and use `video_url` instead of `image_url`.
    /// Instagram will process the video asynchronously — poll with `check_container_status`.
    async fn create_carousel_video_item(&self, video_url: &str) -> Result<String> {
        let url = format!(
            "{}/{}/{}/media",
            api_base(),
            GRAPH_API_VERSION,
            self.business_account_id
        );

        let params = [
            ("media_type", "VIDEO"),
            ("video_url", video_url),
            ("is_carousel_item", "true"),
            ("access_token", &self.access_token),
        ];

        let response = self
            .http
            .post(&url)
            .form(&params)
            .send()
            .await
            .map_err(|e| {
                AppError::External(format!("Failed to create carousel video item: {}", e))
            })?;

        let status = response.status();
        let body = response.text().await.map_err(|e| {
            AppError::External(format!("Failed to read carousel video response: {}", e))
        })?;

        if !status.is_success() {
            if let Ok(error_resp) = serde_json::from_str::<InstagramErrorResponse>(&body) {
                return Err(AppError::External(format!(
                    "Instagram carousel video error: {} (code: {:?})",
                    error_resp.error.message, error_resp.error.code
                )));
            }
            return Err(AppError::External(format!(
                "Instagram carousel video error: {} - {}",
                status, body
            )));
        }

        let container: CreateContainerResponse = serde_json::from_str(&body).map_err(|e| {
            AppError::External(format!(
                "Failed to parse carousel video response: {} - {}",
                e, body
            ))
        })?;

        tracing::info!("Created carousel video item: {}", container.id);
        Ok(container.id)
    }

    /// Create a carousel container that groups child items together.
    ///
    /// `children` is a comma-separated list of container IDs.
    /// The `caption` applies to the entire carousel post.
    async fn create_carousel_container(
        &self,
        children_ids: &[String],
        caption: &str,
    ) -> Result<String> {
        let url = format!(
            "{}/{}/{}/media",
            api_base(),
            GRAPH_API_VERSION,
            self.business_account_id
        );

        let children_csv = children_ids.join(",");

        let params = [
            ("media_type", "CAROUSEL"),
            ("children", &children_csv),
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
                AppError::External(format!("Failed to create carousel container: {}", e))
            })?;

        let status = response.status();
        let body = response.text().await.map_err(|e| {
            AppError::External(format!("Failed to read carousel container response: {}", e))
        })?;

        if !status.is_success() {
            if let Ok(error_resp) = serde_json::from_str::<InstagramErrorResponse>(&body) {
                return Err(AppError::External(format!(
                    "Instagram carousel container error: {} (code: {:?})",
                    error_resp.error.message, error_resp.error.code
                )));
            }
            return Err(AppError::External(format!(
                "Instagram carousel container error: {} - {}",
                status, body
            )));
        }

        let container: CreateContainerResponse = serde_json::from_str(&body).map_err(|e| {
            AppError::External(format!(
                "Failed to parse carousel container response: {} - {}",
                e, body
            ))
        })?;

        tracing::info!("Created carousel container: {}", container.id);
        Ok(container.id)
    }

    /// Wait for a container to reach FINISHED status.
    ///
    /// Videos take longer to process, so this uses a configurable timeout.
    async fn poll_container_until_ready(
        &self,
        container_id: &str,
        label: &str,
        max_attempts: u32,
        poll_interval: std::time::Duration,
    ) -> Result<()> {
        for attempt in 1..=max_attempts {
            let status = self.check_container_status(container_id).await?;
            tracing::info!(
                "{} container {} status: {} (attempt {}/{})",
                label,
                container_id,
                status,
                attempt,
                max_attempts
            );

            match status.as_str() {
                "FINISHED" => return Ok(()),
                "ERROR" | "EXPIRED" => {
                    return Err(AppError::External(format!(
                        "{} container processing failed with status: {}",
                        label, status
                    )));
                }
                _ => {
                    if attempt == max_attempts {
                        return Err(AppError::External(format!(
                            "{} container processing timed out after {} attempts",
                            label, max_attempts
                        )));
                    }
                    tokio::time::sleep(poll_interval).await;
                }
            }
        }

        Ok(())
    }

    /// Post a carousel (multi-slide album) to Instagram.
    ///
    /// Orchestrates the full flow:
    /// 1. Create an image item container for the profile picture
    /// 2. Create video item containers for each track preview
    /// 3. Poll all children until they are processed
    /// 4. Create the parent carousel container with caption
    /// 5. Poll the carousel container
    /// 6. Publish
    pub async fn post_carousel(
        &self,
        image_url: &str,
        video_urls: &[String],
        caption: &str,
    ) -> Result<InstagramPostResult> {
        tracing::info!(
            "Creating Instagram carousel: 1 image + {} videos",
            video_urls.len()
        );

        // Step 1: Create the image child item
        let image_item_id = match self.create_carousel_image_item(image_url).await {
            Ok(id) => id,
            Err(e) => {
                return Ok(InstagramPostResult {
                    success: false,
                    media_id: None,
                    permalink: None,
                    error: Some(format!("Failed to create image slide: {}", e)),
                });
            }
        };

        // Step 2: Create video child items
        let mut video_item_ids = Vec::new();
        for (i, video_url) in video_urls.iter().enumerate() {
            match self.create_carousel_video_item(video_url).await {
                Ok(id) => video_item_ids.push(id),
                Err(e) => {
                    return Ok(InstagramPostResult {
                        success: false,
                        media_id: None,
                        permalink: None,
                        error: Some(format!("Failed to create video slide {}: {}", i + 1, e)),
                    });
                }
            }
        }

        // Step 3: Poll all children until FINISHED
        // Image containers are fast (~5s), video containers can take 60-120s
        const IMAGE_POLL_INTERVAL: std::time::Duration = std::time::Duration::from_secs(2);
        const VIDEO_POLL_INTERVAL: std::time::Duration = std::time::Duration::from_secs(3);
        const IMAGE_MAX_ATTEMPTS: u32 = 30; // ~60s
        const VIDEO_MAX_ATTEMPTS: u32 = 60; // ~180s

        // Poll image item
        if let Err(e) = self
            .poll_container_until_ready(
                &image_item_id,
                "Image",
                IMAGE_MAX_ATTEMPTS,
                IMAGE_POLL_INTERVAL,
            )
            .await
        {
            return Ok(InstagramPostResult {
                success: false,
                media_id: None,
                permalink: None,
                error: Some(format!("Image slide processing failed: {}", e)),
            });
        }

        // Poll video items in sequence (Instagram may throttle parallel polling)
        for (i, vid_id) in video_item_ids.iter().enumerate() {
            if let Err(e) = self
                .poll_container_until_ready(
                    vid_id,
                    &format!("Video {}", i + 1),
                    VIDEO_MAX_ATTEMPTS,
                    VIDEO_POLL_INTERVAL,
                )
                .await
            {
                return Ok(InstagramPostResult {
                    success: false,
                    media_id: None,
                    permalink: None,
                    error: Some(format!("Video slide {} processing failed: {}", i + 1, e)),
                });
            }
        }

        // Step 4: Create carousel container
        let mut children_ids = vec![image_item_id];
        children_ids.extend(video_item_ids);

        let carousel_id = match self.create_carousel_container(&children_ids, caption).await {
            Ok(id) => id,
            Err(e) => {
                return Ok(InstagramPostResult {
                    success: false,
                    media_id: None,
                    permalink: None,
                    error: Some(format!("Failed to create carousel: {}", e)),
                });
            }
        };

        // Step 5: Poll carousel container
        if let Err(e) = self
            .poll_container_until_ready(
                &carousel_id,
                "Carousel",
                IMAGE_MAX_ATTEMPTS,
                IMAGE_POLL_INTERVAL,
            )
            .await
        {
            return Ok(InstagramPostResult {
                success: false,
                media_id: None,
                permalink: None,
                error: Some(format!("Carousel processing failed: {}", e)),
            });
        }

        // Step 6: Publish (with retry for transient 9007 errors)
        let media_id = match self.publish_with_retry(&carousel_id).await {
            Ok(id) => id,
            Err(e) => {
                return Ok(InstagramPostResult {
                    success: false,
                    media_id: None,
                    permalink: None,
                    error: Some(format!("Failed to publish carousel: {}", e)),
                });
            }
        };

        tracing::info!("Successfully published carousel to Instagram: {}", media_id);

        // Fetch the permalink for the published carousel
        let permalink = self.fetch_permalink(&media_id).await;
        if let Some(ref url) = permalink {
            tracing::info!("Instagram carousel permalink: {}", url);
        }

        Ok(InstagramPostResult {
            success: true,
            media_id: Some(media_id),
            permalink,
            error: None,
        })
    }

    // ────────────────────────────────────────────────────────────────────────
    // Single image posting
    // ────────────────────────────────────────────────────────────────────────

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
                    permalink: None,
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
                        permalink: None,
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
                            permalink: None,
                            error: Some(
                                "Instagram container processing timed out after 60s".to_string(),
                            ),
                        });
                    }
                    tokio::time::sleep(POLL_INTERVAL).await;
                }
            }
        }

        // Step 3: Publish container (with retry for transient 9007 errors)
        let media_id = match self.publish_with_retry(&container_id).await {
            Ok(id) => id,
            Err(e) => {
                tracing::error!("Failed to publish Instagram container: {}", e);
                return Ok(InstagramPostResult {
                    success: false,
                    media_id: None,
                    permalink: None,
                    error: Some(e.to_string()),
                });
            }
        };

        tracing::info!("Successfully published to Instagram: {}", media_id);

        // Fetch the permalink for the published post
        let permalink = self.fetch_permalink(&media_id).await;
        if let Some(ref url) = permalink {
            tracing::info!("Instagram post permalink: {}", url);
        }

        Ok(InstagramPostResult {
            success: true,
            media_id: Some(media_id),
            permalink,
            error: None,
        })
    }

    // ────────────────────────────────────────────────────────────────────────
    // Announcements: stories and feed images on the environment's account
    // ────────────────────────────────────────────────────────────────────────

    /// Client for the account pinned by `INSTAGRAM_ACCOUNT`. Errors (and posts
    /// nothing) when that account has no env token and account id.
    #[allow(dead_code)] // first caller: the announcement scheduler
    pub async fn for_environment(config: &crate::Config, db: &SqlitePool) -> Result<Self> {
        let account = config.instagram_account.as_str();
        let Some((access_token, business_account_id)) =
            account_credentials(config, db, account).await?
        else {
            let msg = format!(
                "Instagram account '{account}' (INSTAGRAM_ACCOUNT) has no token and account id configured"
            );
            tracing::error!("{msg}");
            return Err(AppError::Config(msg));
        };
        Ok(Self {
            http: reqwest::Client::new(),
            access_token,
            business_account_id,
        })
    }

    /// Posts used and allowed in the current 24 h window.
    pub async fn content_publishing_quota(&self) -> Result<(u32, u32)> {
        let url = format!(
            "{}/{}/{}/content_publishing_limit",
            api_base(),
            GRAPH_API_VERSION,
            self.business_account_id
        );
        let req = self.http.get(&url).query(&[
            ("fields", "quota_usage,config"),
            ("access_token", &self.access_token),
        ]);
        let body = graph_send(req, "Instagram publishing limit").await?;
        let value: serde_json::Value = serde_json::from_str(&body).map_err(|e| {
            AppError::External(format!("Failed to parse publishing limit response: {e}"))
        })?;
        let entry = &value["data"][0];
        match (
            entry["quota_usage"].as_u64(),
            entry["config"]["quota_total"].as_u64(),
        ) {
            (Some(used), Some(total)) => Ok((used as u32, total as u32)),
            _ => Err(AppError::External(format!(
                "Unexpected publishing limit response: {body}"
            ))),
        }
    }

    async fn ensure_quota_available(&self) -> Result<()> {
        let (used, total) = self.content_publishing_quota().await?;
        if used >= total {
            return Err(AppError::External(format!(
                "Instagram publishing quota used up ({used}/{total} in 24 h)"
            )));
        }
        Ok(())
    }

    /// Create a media container from form `params` (the token is added here).
    async fn create_media(&self, params: &[(&str, &str)]) -> Result<String> {
        let url = format!(
            "{}/{}/{}/media",
            api_base(),
            GRAPH_API_VERSION,
            self.business_account_id
        );
        let mut form = params.to_vec();
        form.push(("access_token", &self.access_token));
        let body = graph_send(self.http.post(&url).form(&form), "Instagram container").await?;
        let container: CreateContainerResponse = serde_json::from_str(&body).map_err(|e| {
            AppError::External(format!("Failed to parse Instagram response: {e} - {body}"))
        })?;
        Ok(container.id)
    }

    /// Quota check, container, wait until ready, publish, permalink.
    async fn publish_media(&self, params: &[(&str, &str)], label: &str) -> Result<PublishedMedia> {
        self.ensure_quota_available().await?;
        let container_id = self.create_media(params).await?;
        self.poll_container_until_ready(
            &container_id,
            label,
            30,
            std::time::Duration::from_secs(2),
        )
        .await?;
        let media_id = self.publish_with_retry(&container_id).await?;
        tracing::info!("Published Instagram {label}: {media_id}");
        let permalink = self.fetch_permalink(&media_id).await;
        Ok(PublishedMedia {
            media_id,
            permalink,
        })
    }

    /// Publish a story image (JPEG URL), optionally tagging one account.
    #[allow(dead_code)] // first caller: the announcement scheduler
    pub async fn publish_story_image(
        &self,
        image_url: &str,
        mention: Option<&str>,
    ) -> Result<PublishedMedia> {
        let user_tags = mention
            .map(|m| m.trim_start_matches('@'))
            .filter(|m| !m.is_empty())
            .map(|m| serde_json::json!([{ "username": m }]).to_string());
        let mut params = vec![("media_type", "STORIES"), ("image_url", image_url)];
        if let Some(tags) = &user_tags {
            params.push(("user_tags", tags));
        }
        self.publish_media(&params, "Story").await
    }

    /// Publish a feed image with caption, alt text and collaborator accounts.
    #[allow(dead_code)] // first caller: the announcement scheduler
    pub async fn publish_feed_image(
        &self,
        image_url: &str,
        caption: &str,
        alt_text: Option<&str>,
        collaborators: &[String],
    ) -> Result<PublishedMedia> {
        let collaborators: Vec<&str> = collaborators
            .iter()
            .map(|c| c.trim_start_matches('@'))
            .filter(|c| !c.is_empty())
            .collect();
        let collaborators =
            (!collaborators.is_empty()).then(|| serde_json::json!(collaborators).to_string());
        let mut params = vec![("image_url", image_url), ("caption", caption)];
        if let Some(alt) = alt_text {
            params.push(("alt_text", alt));
        }
        if let Some(c) = &collaborators {
            params.push(("collaborators", c));
        }
        self.publish_media(&params, "Feed image").await
    }
}

/// Send a Graph API request and return the body. Errors carry Instagram's
/// message but never the request URL, which can hold the access token.
async fn graph_send(req: reqwest::RequestBuilder, what: &str) -> Result<String> {
    let response = req
        .send()
        .await
        .map_err(|e| AppError::External(format!("{what} request failed: {}", e.without_url())))?;
    let status = response.status();
    let body = response.text().await.map_err(|e| {
        AppError::External(format!(
            "{what}: failed to read response: {}",
            e.without_url()
        ))
    })?;
    if !status.is_success() {
        if let Ok(err) = serde_json::from_str::<InstagramErrorResponse>(&body) {
            return Err(AppError::External(format!(
                "{what} error: {} (code: {:?})",
                err.error.message, err.error.code
            )));
        }
        return Err(AppError::External(format!(
            "{what} error: {status} - {body}"
        )));
    }
    Ok(body)
}

/// Token and account id of `account` (`dev` / `prod`). A refreshed token in
/// the DB replaces the env token. `None` when the env lacks the token or the
/// id, so a stored token alone (a prod DB copied to staging) never posts.
async fn account_credentials(
    config: &crate::Config,
    db: &SqlitePool,
    account: &str,
) -> Result<Option<(String, String)>> {
    let (token, id) = match account {
        "dev" => (
            &config.instagram_access_token_dev,
            &config.instagram_business_account_id_dev,
        ),
        "prod" => (
            &config.instagram_access_token_prod,
            &config.instagram_business_account_id_prod,
        ),
        _ => return Ok(None),
    };
    let set = |v: &Option<String>| v.clone().filter(|v| !v.is_empty());
    let (Some(token), Some(id)) = (set(token), set(id)) else {
        return Ok(None);
    };
    let stored: Option<String> =
        sqlx::query_scalar("SELECT access_token FROM instagram_tokens WHERE account = ?")
            .bind(account)
            .fetch_optional(db)
            .await?;
    Ok(Some((stored.unwrap_or(token), id)))
}

/// Refresh a long-lived token (valid 60 days, must be at least 24 h old).
pub async fn refresh_access_token(current_token: &str) -> Result<TokenRefreshResponse> {
    let req = reqwest::Client::new()
        .get(format!("{}/refresh_access_token", api_base()))
        .query(&[
            ("grant_type", "ig_refresh_token"),
            ("access_token", current_token),
        ]);
    let body = graph_send(req, "Instagram token refresh").await?;
    // The body holds the new token, so it stays out of the error.
    serde_json::from_str(&body)
        .map_err(|e| AppError::External(format!("Failed to parse token refresh response: {e}")))
}

/// Weekly job: refresh the token of each configured account and store it for
/// that account only. Alerts the admin group on failure or near expiry.
pub async fn refresh_tokens(state: &Arc<AppState>) {
    const ALERT_BELOW_SECS: i64 = 14 * 24 * 60 * 60;
    for account in ["dev", "prod"] {
        let alert = match refresh_account(state, account).await {
            Ok(Some(expires_in)) if expires_in < ALERT_BELOW_SECS => Some(format!(
                "Instagram token for {account} expires in {} days.",
                expires_in / 86_400
            )),
            Ok(_) => None,
            Err(e) => {
                tracing::error!("Instagram token refresh failed for {account}: {e}");
                Some(format!("Instagram token refresh failed for {account}: {e}"))
            }
        };
        if let Some(alert) = alert {
            crate::telegram_notify::notify_unconditional(state, &alert).await;
        }
    }
}

/// New lifetime in seconds, or `None` when the account is not configured or
/// was refreshed in the last day.
async fn refresh_account(state: &Arc<AppState>, account: &str) -> Result<Option<i64>> {
    let Some((token, _)) = account_credentials(&state.config, &state.db, account).await? else {
        return Ok(None);
    };
    let recent: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM instagram_tokens \
         WHERE account = ? AND refreshed_at > datetime('now', '-1 day'))",
    )
    .bind(account)
    .fetch_one(&state.db)
    .await?;
    if recent {
        return Ok(None);
    }
    let fresh = refresh_access_token(&token).await?;
    sqlx::query(
        "INSERT INTO instagram_tokens (account, access_token, expires_at, refreshed_at) \
         VALUES (?, ?, datetime('now', ?), datetime('now')) \
         ON CONFLICT(account) DO UPDATE SET access_token = excluded.access_token, \
         expires_at = excluded.expires_at, refreshed_at = excluded.refreshed_at",
    )
    .bind(account)
    .bind(&fresh.access_token)
    .bind(format!("+{} seconds", fresh.expires_in))
    .execute(&state.db)
    .await?;
    tracing::info!(
        "Instagram token refreshed for {account}, valid {} days",
        fresh.expires_in / 86_400
    );
    Ok(Some(fresh.expires_in))
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
    account: &str,
) -> Result<InstagramPostResult> {
    // Check if Instagram is configured
    let client = InstagramClient::from_config(&state.config, account)?;

    // Check if cover exists
    if show.cover_generated_at.is_none() {
        return Ok(InstagramPostResult {
            success: false,
            media_id: None,
            permalink: None,
            error: Some("Show has no cover image. Assign artists first.".to_string()),
        });
    }

    // Generate presigned URL for the cover image (1 hour validity)
    let cover_key = format!("shows/{}/cover.png", show.id);
    let cover_url = storage::get_presigned_url(state, &cover_key, 3600).await?;

    // Build caption using the shared builder
    let caption = build_show_caption(state, show).await?;

    // Post to Instagram
    client.post_image(&cover_url, &caption).await
}

/// Build the Instagram caption for a show.
///
/// Format: `{title}\n\n{ai_bio}\n\n💛\n\n@handle1\n@handle2`
///
/// This is the single source of truth for show caption formatting,
/// used by both `post_show_to_instagram` and the Telegram preview.
pub async fn build_show_caption(
    state: &Arc<AppState>,
    show: &crate::models::Show,
) -> Result<String> {
    let mut caption = show.title.clone();

    if let Some(ref bio) = show.ai_bio {
        if !bio.is_empty() {
            caption.push_str("\n\n");
            caption.push_str(bio);
        }
    }

    // Fetch assigned artists and append their Instagram handles
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
        caption.push_str("\n\n💛\n\n");
        for (_name, instagram) in &artists {
            if let Some(ig) = instagram {
                if !ig.is_empty() {
                    // Extract @handle from URL like https://instagram.com/handle
                    let handle = ig.trim_end_matches('/').rsplit('/').next().unwrap_or(ig);
                    caption.push_str(&format!("@{}\n", handle));
                }
            }
        }
    }

    Ok(caption)
}

/// Post an artist's image to Instagram with their generated caption
///
/// If the artist has track audio + peaks data, posts a 3-slide carousel:
/// - Slide 1: Artist profile image
/// - Slides 2–3: 30-second track preview videos with animated waveform
///
/// Falls back to a single-image post if tracks or peaks are unavailable.
pub async fn post_artist_to_instagram(
    state: &Arc<AppState>,
    artist: &crate::models::Artist,
    account: &str,
) -> Result<InstagramPostResult> {
    let client = InstagramClient::from_config(&state.config, account)?;

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

    // Generate presigned URL for profile image (1 hour validity)
    let image_url = storage::get_presigned_url(state, pic_key, 3600).await?;

    tracing::info!(
        "Posting artist {} (id={}) to Instagram with key: {}",
        artist.name,
        artist.id,
        pic_key
    );

    // Attempt carousel with track preview videos
    let track_keys: Vec<(&str, &str)> = [
        (artist.track1_key.as_deref(), "track1"),
        (artist.track2_key.as_deref(), "track2"),
    ]
    .iter()
    .filter_map(|(key, label)| key.map(|k| (k, *label)))
    .collect();

    if track_keys.is_empty() {
        tracing::info!("No tracks available — posting single image");
        return client.post_image(&image_url, caption).await;
    }

    // Build video data for each track
    let video_data: Vec<(String, String, String)> = track_keys
        .iter()
        .map(|(track_key, label)| {
            let peaks_key = derive_peaks_key(track_key);
            (track_key.to_string(), peaks_key, label.to_string())
        })
        .collect();

    if video_data.is_empty() {
        tracing::info!("No tracks available — posting single image");
        return client.post_image(&image_url, caption).await;
    }

    // Get presigned URLs for track preview videos.
    // Prefer pre-generated videos stored in R2 (track1_video_key / track2_video_key).
    // Fall back to on-demand generation if a pre-generated video is missing.
    tracing::info!(
        "Preparing {} track preview video(s) for carousel",
        video_data.len()
    );

    let mut video_presigned_urls: Vec<String> = Vec::new();
    let mut temp_video_keys: Vec<String> = Vec::new();

    // Map label to pre-generated video key
    let pre_generated: std::collections::HashMap<&str, Option<&String>> = [
        ("track1", artist.track1_video_key.as_ref()),
        ("track2", artist.track2_video_key.as_ref()),
    ]
    .into_iter()
    .collect();

    for (track_key, peaks_key, label) in &video_data {
        // Check for pre-generated video first
        if let Some(Some(video_key)) = pre_generated.get(label.as_str()) {
            tracing::info!("Using pre-generated video for {}: {}", label, video_key);
            match storage::get_presigned_url(state, video_key, 3600).await {
                Ok(url) => {
                    video_presigned_urls.push(url);
                    continue;
                }
                Err(e) => {
                    tracing::warn!(
                        "Pre-generated video key {} exists but presigned URL failed: {}. Falling back to on-demand.",
                        video_key, e
                    );
                }
            }
        }

        // Fallback: generate on-demand
        tracing::info!("Generating preview video on-demand for {}", label);

        let mp4_bytes = match crate::video::generate_track_preview_video(
            state, pic_key, track_key, peaks_key, 30, 0,
        )
        .await
        {
            Ok(bytes) => bytes,
            Err(e) => {
                tracing::error!("Failed to generate {} preview video: {}", label, e);
                cleanup_temp_videos(state, &temp_video_keys).await;
                tracing::info!("Falling back to single image post");
                return client.post_image(&image_url, caption).await;
            }
        };

        // Upload to a temporary R2 path
        let temp_key = format!("artists/{}/instagram-preview-{}.mp4", artist.id, label);

        if let Err(e) = upload_raw(state, &temp_key, mp4_bytes, "video/mp4").await {
            tracing::error!("Failed to upload {} video to R2: {}", label, e);
            cleanup_temp_videos(state, &temp_video_keys).await;
            return client.post_image(&image_url, caption).await;
        }

        temp_video_keys.push(temp_key.clone());

        match storage::get_presigned_url(state, &temp_key, 3600).await {
            Ok(url) => video_presigned_urls.push(url),
            Err(e) => {
                tracing::error!("Failed to get presigned URL for {}: {}", label, e);
                cleanup_temp_videos(state, &temp_video_keys).await;
                return client.post_image(&image_url, caption).await;
            }
        }
    }

    // Post carousel
    tracing::info!(
        "Posting carousel: 1 image + {} video(s)",
        video_presigned_urls.len()
    );

    let result = client
        .post_carousel(&image_url, &video_presigned_urls, caption)
        .await;

    // Clean up only temp (on-demand) video files — pre-generated videos stay in R2
    if !temp_video_keys.is_empty() {
        cleanup_temp_videos(state, &temp_video_keys).await;
    }

    result
}

// ────────────────────────────────────────────────────────────────────────────
// Helpers
// ────────────────────────────────────────────────────────────────────────────

/// Derive the peaks JSON key from an audio file key.
///
/// Peaks are stored alongside the audio file with `.peaks.json` extension:
///   `artists/5/track1/ursi murps.mp3` → `artists/5/track1/ursi murps.peaks.json`
fn derive_peaks_key(audio_key: &str) -> String {
    if let Some((base, _ext)) = audio_key.rsplit_once('.') {
        format!("{}.peaks.json", base)
    } else {
        format!("{}.peaks.json", audio_key)
    }
}

/// Upload raw bytes directly to R2 with a specific key and content type.
async fn upload_raw(
    state: &Arc<AppState>,
    key: &str,
    data: Vec<u8>,
    content_type: &str,
) -> Result<()> {
    use aws_sdk_s3::primitives::ByteStream;

    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(key)
        .body(ByteStream::from(data))
        .content_type(content_type)
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload video to R2: {}", e)))?;

    Ok(())
}

/// Delete temporary video files from R2.
async fn cleanup_temp_videos(state: &Arc<AppState>, keys: &[String]) {
    for key in keys {
        tracing::info!("Cleaning up temp video: {}", key);
        let result = state
            .s3_client
            .delete_object()
            .bucket(&state.config.r2_bucket_name)
            .key(key)
            .send()
            .await;

        if let Err(e) = result {
            tracing::warn!("Failed to delete temp video {}: {}", key, e);
        }
    }
}

#[cfg(test)]
#[path = "instagram_tests.rs"]
mod tests;
