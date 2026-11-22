//! What the host's browser measured in the Studio and the live panel, kept on
//! the episode (`shows.studio_log`), and the listener counts the server takes
//! itself while an episode is on air (`show_listener_samples`). The episode
//! page shows both read-only: to other hosts while on air, to everyone after
//! the show.

use std::sync::Arc;

use axum::{
    extract::{Path, State},
    http::{HeaderMap, StatusCode},
    response::IntoResponse,
    Json,
};
use serde::Serialize;
use serde_json::{Map, Value};
use sqlx::SqlitePool;

use crate::handlers::api::require_show_editor;
use crate::{AppError, AppState, Result};

/// The parts a browser saves: the soundcheck at Go Live, the live panel while on air.
const PARTS: [&str; 2] = ["soundcheck", "on_air"];
/// One part: a few checks, a minute of upload samples and the moments.
const MAX_PART_BYTES: usize = 16 * 1024;
/// At most one listener sample per show in this time (the poller runs every 15 s).
const SAMPLE_GAP_MS: i64 = 10_000;

/// One listener count of an episode on air.
#[derive(Debug, Serialize, sqlx::FromRow, PartialEq)]
pub struct ListenerSample {
    /// Unix ms.
    pub at: i64,
    pub count: i64,
}

/// The parts of a save, each as JSON text. Refuses unknown parts, parts that
/// are not objects and parts that are too big.
fn parts(patch: Map<String, Value>) -> std::result::Result<Vec<(&'static str, String)>, String> {
    if patch.is_empty() {
        return Err("Nothing to save".to_string());
    }
    let mut out = Vec::new();
    for (key, value) in patch {
        let Some(part) = PARTS.iter().find(|p| **p == key) else {
            return Err(format!("Unknown part: {key}"));
        };
        if !value.is_object() {
            return Err(format!("{key} must be an object"));
        }
        let text = value.to_string();
        if text.len() > MAX_PART_BYTES {
            return Err(format!("{key} is too big"));
        }
        out.push((*part, text));
    }
    Ok(out)
}

/// `PUT /api/shows/:id/studio-log`: the host's browser saves `soundcheck`
/// and/or `on_air`. Each part replaces the stored one; the other stays.
pub async fn api_save_studio_log(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    headers: HeaderMap,
    Json(patch): Json<Map<String, Value>>,
) -> Result<impl IntoResponse> {
    require_show_editor(&state, &headers, id).await?;
    for (part, text) in parts(patch).map_err(AppError::BadRequest)? {
        // One statement per part, so a soundcheck save and a panel save never
        // overwrite each other.
        sqlx::query(
            "UPDATE shows SET studio_log = json_set(COALESCE(studio_log, '{}'), ?, json(?)) \
             WHERE id = ?",
        )
        .bind(format!("$.{part}"))
        .bind(text)
        .bind(id)
        .execute(&state.db)
        .await?;
    }
    Ok(StatusCode::NO_CONTENT)
}

/// The stored log as JSON; None when there is none or it does not parse.
pub fn parse(stored: Option<&str>) -> Option<Value> {
    stored.and_then(|text| serde_json::from_str(text).ok())
}

/// Count the listeners of the episode on air, at most once per [`SAMPLE_GAP_MS`].
pub async fn record_listeners(
    pool: &SqlitePool,
    show_id: i64,
    count: u64,
    at_ms: i64,
) -> std::result::Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO show_listener_samples (show_id, at, count) SELECT ?1, ?2, ?3 \
         WHERE NOT EXISTS (SELECT 1 FROM show_listener_samples \
                           WHERE show_id = ?1 AND at > ?2 - ?4)",
    )
    .bind(show_id)
    .bind(at_ms)
    .bind(count as i64)
    .bind(SAMPLE_GAP_MS)
    .execute(pool)
    .await?;
    Ok(())
}

/// After an Icecast poll: count the listeners of the episode on air, if any.
pub async fn after_poll(state: &AppState, online: bool, listeners: u64) {
    if !online {
        return;
    }
    let Some(show_id) = crate::handlers::series::on_air_show_id(state).await else {
        return;
    };
    let now = chrono::Utc::now().timestamp_millis();
    if let Err(e) = record_listeners(&state.db, show_id, listeners, now).await {
        tracing::warn!("Could not save the listener count of show {show_id}: {e}");
    }
}

/// An episode's listener counts, oldest first.
pub async fn listener_samples(
    pool: &SqlitePool,
    show_id: i64,
) -> std::result::Result<Vec<ListenerSample>, sqlx::Error> {
    sqlx::query_as("SELECT at, count FROM show_listener_samples WHERE show_id = ? ORDER BY at")
        .bind(show_id)
        .fetch_all(pool)
        .await
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn map(value: Value) -> Map<String, Value> {
        value.as_object().unwrap().clone()
    }

    #[test]
    fn parts_takes_known_objects_only() {
        let got = parts(map(json!({"soundcheck": {"input": "USB"}}))).unwrap();
        assert_eq!(got, vec![("soundcheck", r#"{"input":"USB"}"#.to_string())]);
        assert!(parts(map(json!({}))).is_err());
        assert!(parts(map(json!({"other": {}}))).is_err());
        assert!(parts(map(json!({"on_air": [1, 2]}))).is_err());
        let big = "x".repeat(MAX_PART_BYTES);
        assert!(parts(map(json!({"on_air": {"input": big}}))).is_err());
    }

    #[test]
    fn parse_skips_broken_text() {
        assert_eq!(parse(None), None);
        assert_eq!(parse(Some("{")), None);
        assert_eq!(parse(Some(r#"{"a":1}"#)), Some(json!({"a": 1})));
    }
}
