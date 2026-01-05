pub mod admin;
pub mod auth;
pub mod download;
pub mod submit;
pub mod submit_chunked;

use axum::Json;
use serde_json::{json, Value};

pub async fn health_check() -> Json<Value> {
    Json(json!({
        "status": "healthy",
        "app": "unheard-backend"
    }))
}
