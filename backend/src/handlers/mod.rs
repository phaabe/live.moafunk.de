pub mod api;
pub mod backup_trigger;
pub mod download;
pub mod recording;
pub mod stream_ws;
pub mod submit;
pub mod submit_chunked;
pub mod upload_recording_chunked;

use axum::Json;
use serde_json::{json, Value};

pub async fn health_check() -> Json<Value> {
    Json(json!({
        "status": "healthy",
        "app": "unheard-backend"
    }))
}
