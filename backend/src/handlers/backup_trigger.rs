//! GitHub Actions backup trigger
//!
//! Triggers a `repository_dispatch` event to run backup workflow after artist submission.
//! This is fire-and-forget - submission success is not dependent on backup trigger success.

use crate::config::Config;

/// Trigger a GitHub Actions backup workflow via repository_dispatch
///
/// This is non-blocking and failure-tolerant - we don't want backup trigger failures
/// to affect the user's submission experience.
pub fn trigger_backup_on_submission(config: &Config, artist_id: i64) {
    // Only trigger if GitHub dispatch token is configured
    let Some(token) = &config.github_dispatch_token else {
        tracing::debug!("GitHub dispatch token not configured, skipping backup trigger");
        return;
    };

    let repo = &config.github_repo;
    let url = format!("https://api.github.com/repos/{}/dispatches", repo);

    tracing::info!(
        "Triggering backup workflow for artist_id={} via {}",
        artist_id,
        url
    );

    // Spawn a detached task so we don't block the response
    let token = token.clone();
    tokio::spawn(async move {
        match send_dispatch_event(&url, &token, artist_id).await {
            Ok(()) => {
                tracing::info!(
                    "Successfully triggered backup workflow for artist_id={}",
                    artist_id
                );
            }
            Err(e) => {
                tracing::warn!(
                    "Failed to trigger backup workflow for artist_id={}: {}",
                    artist_id,
                    e
                );
            }
        }
    });
}

async fn send_dispatch_event(url: &str, token: &str, artist_id: i64) -> Result<(), String> {
    let client = reqwest::Client::new();

    let payload = serde_json::json!({
        "event_type": "backup-on-submission",
        "client_payload": {
            "artist_id": artist_id,
            "trigger": "artist-submission"
        }
    });

    let response = client
        .post(url)
        .header("Accept", "application/vnd.github+json")
        .header("Authorization", format!("Bearer {}", token))
        .header("X-GitHub-Api-Version", "2022-11-28")
        .header("User-Agent", "unheard-backend")
        .json(&payload)
        .send()
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    let status = response.status();
    if status.is_success() {
        Ok(())
    } else {
        let body = response.text().await.unwrap_or_default();
        Err(format!("GitHub API returned {}: {}", status, body))
    }
}
