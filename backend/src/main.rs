mod audio;
mod auth;
mod config;
mod db;
mod error;
mod handlers;
mod image_overlay;
mod models;
mod pdf;
mod storage;
mod stream_bridge;

use axum::{
    extract::{DefaultBodyLimit, Query, State, WebSocketUpgrade},
    http::HeaderMap,
    response::{IntoResponse, Response},
    routing::{get, post},
    Router,
};
use sqlx::sqlite::SqlitePoolOptions;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::RwLock;
use tower_http::cors::{Any, CorsLayer};
use tower_http::services::{ServeDir, ServeFile};
use tower_http::trace::TraceLayer;
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};

pub use config::Config;
pub use error::{AppError, Result};

use stream_bridge::SharedStreamState;

/// Tracks pending cover regeneration requests with debounce
pub type CoverDebounceMap = Arc<RwLock<HashMap<i64, tokio::time::Instant>>>;

pub struct AppState {
    pub db: sqlx::SqlitePool,
    pub config: Config,
    pub s3_client: aws_sdk_s3::Client,
    pub stream_state: SharedStreamState,
    /// Debounce tracker for show cover regeneration (show_id -> last_request_time)
    pub cover_debounce: CoverDebounceMap,
    /// Cached default cover image (4 black tiles with UN/HEARD branding)
    pub default_cover: tokio::sync::OnceCell<Vec<u8>>,
}

// Stream handler wrappers that extract stream_state from AppState
async fn stream_ws_handler(
    ws: WebSocketUpgrade,
    State(state): State<Arc<AppState>>,
    Query(query): Query<handlers::stream_ws::StreamQuery>,
    headers: HeaderMap,
) -> Result<Response> {
    handlers::stream_ws::stream_ws_handler(
        ws,
        State(state.clone()),
        State(state.stream_state.clone()),
        Query(query),
        headers,
    )
    .await
}

async fn stream_status_handler(State(state): State<Arc<AppState>>) -> impl IntoResponse {
    handlers::stream_ws::stream_status(State(state.stream_state.clone())).await
}

async fn stream_stop_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    handlers::stream_ws::stream_stop(
        State(state.clone()),
        State(state.stream_state.clone()),
        headers,
    )
    .await
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Load environment variables
    dotenvy::dotenv().ok();

    // Initialize tracing
    tracing_subscriber::registry()
        .with(tracing_subscriber::EnvFilter::new(
            std::env::var("RUST_LOG").unwrap_or_else(|_| "info,tower_http=debug".into()),
        ))
        .with(tracing_subscriber::fmt::layer())
        .init();

    // Load config
    let config = Config::from_env()?;

    // Initialize database
    let db = SqlitePoolOptions::new()
        .max_connections(5)
        .connect(&config.database_url)
        .await?;

    // Run migrations
    db::run_migrations(&db).await?;

    // Seed superadmin if no users exist
    db::seed_superadmin(&db, &config).await?;

    // Initialize S3 client for R2 (avoid aws-config to reduce dependencies/compile time)
    let s3_config = aws_sdk_s3::Config::builder()
        .endpoint_url(&config.r2_endpoint)
        .credentials_provider(aws_sdk_s3::config::Credentials::new(
            &config.r2_access_key_id,
            &config.r2_secret_access_key,
            None,
            None,
            "r2",
        ))
        .region(aws_sdk_s3::config::Region::new("auto"))
        .build();

    let s3_client = aws_sdk_s3::Client::from_conf(s3_config);

    // Initialize stream state
    let stream_state = stream_bridge::new_shared_state();

    // Initialize cover regeneration debounce tracker
    let cover_debounce = Arc::new(RwLock::new(HashMap::new()));

    let state = Arc::new(AppState {
        db,
        config: config.clone(),
        s3_client,
        stream_state,
        cover_debounce,
        default_cover: tokio::sync::OnceCell::new(),
    });

    // Pre-generate and upload default cover to S3 at startup
    {
        let state_clone = state.clone();
        tokio::spawn(async move {
            if let Err(e) = handlers::api::ensure_default_cover_exists(&state_clone).await {
                tracing::warn!("Failed to generate default cover at startup: {}", e);
            }
        });
    }

    // Build CORS layer (permissive for same-origin setup)
    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods(Any)
        .allow_headers(Any);

    // Build router
    // Admin SPA static files with fallback for SPA routing
    let admin_spa =
        ServeDir::new("static/admin").not_found_service(ServeFile::new("static/admin/index.html"));

    let app = Router::new()
        // Admin SPA assets (JS, CSS, etc.) - must come before catch-all
        .nest_service("/assets", ServeDir::new("static/admin/assets"))
        // Static assets (brand)
        .nest_service("/assets/brand", ServeDir::new("assets/brand"))
        // Health check
        .route("/health", get(handlers::health_check))
        // Public API (single-request upload, kept for backwards compatibility)
        .route("/api/submit", post(handlers::submit::submit_form))
        // Chunked upload API (multi-request, stays under Cloudflare 100MB limit)
        .route(
            "/api/submit/init",
            post(handlers::submit_chunked::submit_init),
        )
        .route(
            "/api/submit/file/:session_id",
            post(handlers::submit_chunked::submit_file),
        )
        .route(
            "/api/submit/finalize/:session_id",
            post(handlers::submit_chunked::submit_finalize),
        )
        // JSON API for admin SPA
        .route("/api/auth/login", post(handlers::api::api_login))
        .route("/api/auth/logout", post(handlers::api::api_logout))
        .route("/api/auth/me", get(handlers::api::api_me))
        .route(
            "/api/auth/change-password",
            post(handlers::api::api_change_password),
        )
        .route("/api/artists", get(handlers::api::api_artists_list))
        .route("/api/artists/:id", get(handlers::api::api_artist_detail))
        .route(
            "/api/artists/:id",
            axum::routing::delete(handlers::api::api_delete_artist),
        )
        .route(
            "/api/artists/:id/details",
            axum::routing::put(handlers::api::api_update_artist_details),
        )
        .route(
            "/api/artists/:id/picture",
            axum::routing::put(handlers::api::api_update_artist_picture),
        )
        .route(
            "/api/artists/:id/audio",
            axum::routing::put(handlers::api::api_update_artist_audio),
        )
        .route(
            "/api/artists/:id/shows",
            post(handlers::api::api_assign_artist_to_show),
        )
        .route(
            "/api/artists/:id/shows/:show_id",
            axum::routing::delete(handlers::api::api_unassign_artist_from_show),
        )
        .route("/api/shows", get(handlers::api::api_shows_list))
        .route("/api/shows", post(handlers::api::api_create_show))
        .route("/api/shows/:id", get(handlers::api::api_show_detail))
        .route(
            "/api/shows/:id",
            axum::routing::put(handlers::api::api_update_show),
        )
        .route(
            "/api/shows/:id",
            axum::routing::delete(handlers::api::api_delete_show),
        )
        .route(
            "/api/shows/:id/artists",
            post(handlers::api::api_show_assign_artist),
        )
        .route(
            "/api/shows/:id/artists/:artist_id",
            axum::routing::delete(handlers::api::api_show_unassign_artist),
        )
        .route(
            "/api/shows/:id/upload-recording",
            post(handlers::api::api_upload_show_recording),
        )
        .route(
            "/api/shows/:id/recording",
            axum::routing::delete(handlers::api::api_delete_show_recording),
        )
        .route("/api/users", get(handlers::api::api_users_list))
        .route("/api/users", post(handlers::api::api_create_user))
        .route(
            "/api/users/:id",
            axum::routing::put(handlers::api::api_update_user),
        )
        .route(
            "/api/users/:id",
            axum::routing::delete(handlers::api::api_delete_user),
        )
        .route(
            "/api/users/:id/reset-password",
            post(handlers::api::api_reset_password),
        )
        // Download routes (needed by SPA)
        .route(
            "/artists/:id/download",
            get(handlers::download::download_artist),
        )
        .route(
            "/artists/:id/download/audio",
            get(handlers::download::download_artist_audio),
        )
        .route(
            "/artists/:id/download/images",
            get(handlers::download::download_artist_images),
        )
        .route(
            "/artists/:id/download/pdf",
            get(handlers::download::download_artist_pdf),
        )
        .route(
            "/shows/:id/download",
            get(handlers::download::download_show),
        )
        .route(
            "/shows/:id/download/:package",
            get(handlers::download::download_show_package),
        )
        // Stream WebSocket and API
        .route("/ws/stream", get(stream_ws_handler))
        .route("/api/stream/status", get(stream_status_handler))
        .route("/api/stream/stop", post(stream_stop_handler))
        // Admin SPA fallback - serves index.html for client-side routing
        .fallback_service(admin_spa)
        .layer(DefaultBodyLimit::max(config.max_request_body_bytes()))
        .layer(cors)
        .layer(TraceLayer::new_for_http())
        .with_state(state.clone());

    // Spawn background task to clean up expired user accounts (runs weekly)
    let cleanup_db = state.db.clone();
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(std::time::Duration::from_secs(7 * 24 * 60 * 60)); // 1 week
        loop {
            interval.tick().await;
            tracing::info!("Running expired user cleanup...");
            match sqlx::query(
                "DELETE FROM users WHERE expires_at IS NOT NULL AND expires_at < datetime('now')",
            )
            .execute(&cleanup_db)
            .await
            {
                Ok(result) => {
                    if result.rows_affected() > 0 {
                        tracing::info!(
                            "Cleaned up {} expired user accounts",
                            result.rows_affected()
                        );
                    }
                }
                Err(e) => tracing::error!("Failed to clean up expired users: {}", e),
            }
        }
    });

    let addr = format!("{}:{}", config.host, config.port);
    tracing::info!("Starting server on {}", addr);

    let listener = tokio::net::TcpListener::bind(&addr).await?;
    axum::serve(listener, app).await?;

    Ok(())
}
