//! Icecast listener/quality telemetry (#177).
//!
//! Polls Icecast `GET /status-json.xsl` on an interval, normalizes the per-mount
//! stats (handling the documented `icestats.source` object-vs-array quirk), and
//! caches the latest snapshot in [`crate::AppState`] for `GET /api/stream/metrics`
//! and the admin SPA. Icecast's persistent connections give a *genuine*
//! concurrent listener count — something HLS structurally can't.

use serde::{Deserialize, Serialize};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::RwLock;

use crate::AppState;

/// Per-request timeout — the poller must never hang on a wedged Icecast.
const REQUEST_TIMEOUT_SECS: u64 = 5;

/// Latest snapshot served from cache. Always present (starts as "never polled":
/// `online=false`, no `polled_at_ms`, no `error`).
#[derive(Clone, Debug, Default, Serialize)]
pub struct StreamMetrics {
    /// True if the last poll succeeded AND at least one source is connected.
    pub online: bool,
    /// Total listeners across all mounts.
    pub total_listeners: u64,
    /// Per-mount detail.
    pub mounts: Vec<MountMetrics>,
    /// Unix-ms timestamp of the last successful poll, if any.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub polled_at_ms: Option<i64>,
    /// Set when the last poll failed (network/parse); mounts are then empty.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// Quality + listener stats for a single Icecast mount.
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct MountMetrics {
    /// Mount path, e.g. `/live.mp3` (derived from the source `listenurl`).
    pub mount: String,
    pub listeners: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub listener_peak: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bitrate: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub samplerate: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub channels: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub server_name: Option<String>,
}

pub type SharedStreamMetrics = Arc<RwLock<StreamMetrics>>;

pub fn new_shared() -> SharedStreamMetrics {
    Arc::new(RwLock::new(StreamMetrics::default()))
}

// ── Raw `/status-json.xsl` shape ────────────────────────────────────────────

#[derive(Deserialize)]
struct StatusJson {
    icestats: IceStats,
}

#[derive(Deserialize)]
struct IceStats {
    #[serde(default)]
    source: Option<SourceField>,
}

/// Icecast serializes `source` as a single object when exactly one mount is
/// connected, and as an array when several are. Untagged so either parses; an
/// array can't deserialize into the (map-shaped) struct and vice-versa, so the
/// two variants are disjoint regardless of order.
#[derive(Deserialize)]
#[serde(untagged)]
enum SourceField {
    One(Box<RawSource>),
    Many(Vec<RawSource>),
}

#[derive(Deserialize)]
struct RawSource {
    #[serde(default)]
    listeners: Option<u64>,
    #[serde(default)]
    listener_peak: Option<u64>,
    #[serde(default)]
    bitrate: Option<u64>,
    #[serde(default)]
    samplerate: Option<u64>,
    #[serde(default)]
    channels: Option<u64>,
    #[serde(default)]
    server_name: Option<String>,
    #[serde(default)]
    listenurl: Option<String>,
}

/// Extract the mount path from a `listenurl` like `http://host:8000/live.mp3`.
fn mount_from_listenurl(url: &str) -> Option<String> {
    let after_scheme = url.split_once("://").map(|(_, r)| r).unwrap_or(url);
    after_scheme
        .find('/')
        .map(|i| after_scheme[i..].to_string())
}

/// Parse a `/status-json.xsl` body into a normalized snapshot. `polled_at_ms`
/// is left `None` here and stamped by the caller on success.
fn parse_status(body: &str) -> Result<StreamMetrics, String> {
    let parsed: StatusJson =
        serde_json::from_str(body).map_err(|e| format!("parse status-json: {e}"))?;

    let raw_sources = match parsed.icestats.source {
        Some(SourceField::One(s)) => vec![*s],
        Some(SourceField::Many(v)) => v,
        None => vec![],
    };

    let mounts: Vec<MountMetrics> = raw_sources
        .into_iter()
        .map(|s| MountMetrics {
            mount: s
                .listenurl
                .as_deref()
                .and_then(mount_from_listenurl)
                .unwrap_or_default(),
            listeners: s.listeners.unwrap_or(0),
            listener_peak: s.listener_peak,
            bitrate: s.bitrate,
            samplerate: s.samplerate,
            channels: s.channels,
            server_name: s.server_name,
        })
        .collect();

    let total_listeners = mounts.iter().map(|m| m.listeners).sum();

    Ok(StreamMetrics {
        online: !mounts.is_empty(),
        total_listeners,
        mounts,
        polled_at_ms: None,
        error: None,
    })
}

async fn fetch(client: &reqwest::Client, url: &str) -> Result<StreamMetrics, String> {
    let resp = client
        .get(url)
        .send()
        .await
        .map_err(|e| format!("request: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("status {}", resp.status()));
    }
    let body = resp.text().await.map_err(|e| format!("read body: {e}"))?;
    parse_status(&body)
}

/// One poll cycle: fetch + parse, stamping the timestamp on success. Never
/// panics — a failure becomes an `online=false` snapshot carrying the error.
async fn poll_once(client: &reqwest::Client, url: &str) -> StreamMetrics {
    match fetch(client, url).await {
        Ok(mut m) => {
            m.polled_at_ms = Some(chrono::Utc::now().timestamp_millis());
            m
        }
        Err(e) => {
            tracing::warn!("Icecast metrics poll failed: {e}");
            StreamMetrics {
                online: false,
                error: Some(e),
                ..Default::default()
            }
        }
    }
}

/// Spawn the background poller. No-op (logged) when no status URL is configured,
/// so it's safe to call unconditionally at startup before Icecast exists.
pub fn spawn_poller(state: Arc<AppState>) {
    let Some(url) = state.config.icecast_status_url() else {
        tracing::info!(
            "Icecast metrics poller disabled (no ICECAST_STATUS_URL and no icecast_url to derive from)"
        );
        return;
    };
    let interval_secs = state.config.icecast_metrics_poll_secs.max(1);
    let cache = state.stream_metrics.clone();
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(REQUEST_TIMEOUT_SECS))
        .build()
        .unwrap_or_else(|_| reqwest::Client::new());

    tracing::info!("Starting Icecast metrics poller: {url} every {interval_secs}s");
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(interval_secs));
        loop {
            interval.tick().await;
            let snapshot = poll_once(&client, &url).await;
            *cache.write().await = snapshot;
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_single_source_object() {
        // One connected mount → `source` is an OBJECT.
        let body = r#"{"icestats":{"source":{
            "listeners":7,"listener_peak":12,"bitrate":128,"samplerate":44100,
            "channels":2,"server_name":"Moafunk","listenurl":"http://h:8010/live.mp3"}}}"#;
        let m = parse_status(body).unwrap();
        assert!(m.online);
        assert_eq!(m.total_listeners, 7);
        assert_eq!(m.mounts.len(), 1);
        let mount = &m.mounts[0];
        assert_eq!(mount.mount, "/live.mp3");
        assert_eq!(mount.listeners, 7);
        assert_eq!(mount.bitrate, Some(128));
        assert_eq!(mount.samplerate, Some(44100));
        assert_eq!(mount.channels, Some(2));
    }

    #[test]
    fn parses_multiple_sources_array() {
        // Several mounts → `source` is an ARRAY. The object-vs-array quirk.
        let body = r#"{"icestats":{"source":[
            {"listeners":3,"listenurl":"http://h:8010/live.mp3"},
            {"listeners":5,"listenurl":"http://h:8010/test.mp3"}]}}"#;
        let m = parse_status(body).unwrap();
        assert!(m.online);
        assert_eq!(m.total_listeners, 8);
        assert_eq!(m.mounts.len(), 2);
        assert_eq!(m.mounts[0].mount, "/live.mp3");
        assert_eq!(m.mounts[1].mount, "/test.mp3");
    }

    #[test]
    fn handles_no_sources_connected() {
        // Idle Icecast → no `source` key at all.
        let m = parse_status(r#"{"icestats":{}}"#).unwrap();
        assert!(!m.online);
        assert_eq!(m.total_listeners, 0);
        assert!(m.mounts.is_empty());
    }

    #[test]
    fn missing_optional_fields_default_gracefully() {
        let body = r#"{"icestats":{"source":{"listenurl":"http://h:8010/live.mp3"}}}"#;
        let m = parse_status(body).unwrap();
        assert_eq!(m.mounts[0].listeners, 0);
        assert_eq!(m.mounts[0].bitrate, None);
    }

    #[test]
    fn rejects_garbage_body() {
        assert!(parse_status("not json").is_err());
    }

    #[test]
    fn mount_path_extracted_from_listenurl() {
        assert_eq!(
            mount_from_listenurl("http://host:8010/live.mp3").as_deref(),
            Some("/live.mp3")
        );
        assert_eq!(mount_from_listenurl("http://host:8010").as_deref(), None);
    }
}
