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
    extract::{DefaultBodyLimit, State, Query, WebSocketUpgrade},
    http::HeaderMap,
    response::{IntoResponse, Response},
    routing::{get, post},
    Router,
};
use sqlx::sqlite::SqlitePoolOptions;
use std::sync::Arc;
use tower_http::cors::{Any, CorsLayer};
use tower_http::services::ServeDir;
use tower_http::trace::TraceLayer;
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};

pub use config::Config;
pub use error::{AppError, Result};

use stream_bridge::SharedStreamState;

pub struct AppState {
    pub db: sqlx::SqlitePool,
    pub config: Config,
    pub templates: tera::Tera,
    pub s3_client: aws_sdk_s3::Client,
    pub stream_state: SharedStreamState,
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
    ).await
}

async fn stream_status_handler(
    State(state): State<Arc<AppState>>,
) -> impl IntoResponse {
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
    ).await
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

    // Initialize templates
    let mut templates = tera::Tera::new("templates/**/*")?;

    // Register custom date filter
    templates.register_filter(
        "date",
        |value: &tera::Value,
         args: &std::collections::HashMap<String, tera::Value>|
         -> tera::Result<tera::Value> {
            let _format = args
                .get("format")
                .and_then(|v| v.as_str())
                .unwrap_or("%Y-%m-%d %H:%M");

            if let Some(s) = value.as_str() {
                // Parse the datetime string and reformat it
                // For now, just return the input as-is (you can add chrono parsing if needed)
                Ok(tera::Value::String(s.to_string()))
            } else {
                Ok(value.clone())
            }
        },
    );

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

    let state = Arc::new(AppState {
        db,
        config: config.clone(),
        templates,
        s3_client,
        stream_state,
    });

    // Build CORS layer
    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods(Any)
        .allow_headers(Any);

    // Build router
    let app = Router::new()
        // Static assets (brand)
        .nest_service("/assets/brand", ServeDir::new("assets/brand"))
        // Health check
        .route("/health", get(handlers::health_check))
        // Public API (single-request upload, kept for backwards compatibility)
        .route("/api/submit", post(handlers::submit::submit_form))
        // Chunked upload API (multi-request, stays under Cloudflare 100MB limit)
        .route("/api/submit/init", post(handlers::submit_chunked::submit_init))
        .route("/api/submit/file/:session_id", post(handlers::submit_chunked::submit_file))
        .route("/api/submit/finalize/:session_id", post(handlers::submit_chunked::submit_finalize))
        // JSON API for admin SPA
        .route("/api/auth/login", post(handlers::api::api_login))
        .route("/api/auth/logout", post(handlers::api::api_logout))
        .route("/api/auth/me", get(handlers::api::api_me))
        .route("/api/auth/change-password", post(handlers::api::api_change_password))
        .route("/api/artists", get(handlers::api::api_artists_list))
        .route("/api/artists/:id", get(handlers::api::api_artist_detail))
        .route("/api/artists/:id", axum::routing::delete(handlers::api::api_delete_artist))
        .route("/api/artists/:id/shows", post(handlers::api::api_assign_artist_to_show))
        .route("/api/artists/:id/shows/:show_id", axum::routing::delete(handlers::api::api_unassign_artist_from_show))
        .route("/api/shows", get(handlers::api::api_shows_list))
        .route("/api/shows", post(handlers::api::api_create_show))
        .route("/api/shows/:id", get(handlers::api::api_show_detail))
        .route("/api/shows/:id", axum::routing::put(handlers::api::api_update_show))
        .route("/api/shows/:id", axum::routing::delete(handlers::api::api_delete_show))
        .route("/api/users", get(handlers::api::api_users_list))
        .route("/api/users", post(handlers::api::api_create_user))
        .route("/api/users/:id", axum::routing::delete(handlers::api::api_delete_user))
        // Auth routes (template-based, kept for backwards compatibility)
        .route("/login", get(handlers::auth::login_page))
        .route("/login", post(handlers::auth::login))
        .route("/logout", get(handlers::auth::logout))
        // Admin routes
        .route("/", get(handlers::admin::index))
        .route("/artists", get(handlers::admin::artists_list))
        .route("/artists/:id", get(handlers::admin::artist_detail))
        .route(
            "/artists/:id/download",
            get(handlers::download::download_artist),
        )
        .route("/artists/:id/delete", post(handlers::admin::delete_artist))
        .route("/artists/:id/assign", post(handlers::admin::assign_show))
        .route(
            "/artists/:id/unassign/:show_id",
            post(handlers::admin::unassign_show),
        )
        .route("/shows", get(handlers::admin::shows_list))
        .route("/shows", post(handlers::admin::create_show))
        .route("/shows/:id", get(handlers::admin::show_detail))
        .route("/shows/:id/delete", post(handlers::admin::delete_show))
        .route("/shows/:id/assign", post(handlers::admin::assign_artist))
        .route(
            "/shows/:id/unassign/:artist_id",
            post(handlers::admin::unassign_artist),
        )
        .route("/shows/:id/date", post(handlers::admin::update_show_date))
        .route(
            "/shows/:id/description",
            post(handlers::admin::update_show_description),
        )
        .route(
            "/shows/:id/download",
            get(handlers::download::download_show),
        )
        .route(
            "/shows/:id/download/:package",
            get(handlers::download::download_show_package),
        )
        // Stream page (accessible to all roles)
        .route("/stream", get(handlers::admin::stream_page))
        // Stream WebSocket and API
        .route("/ws/stream", get(stream_ws_handler))
        .route("/api/stream/status", get(stream_status_handler))
        .route("/api/stream/stop", post(stream_stop_handler))
        // User management (admin/superadmin only)
        .route("/users", get(handlers::admin::users_list))
        .route("/users", post(handlers::admin::create_user))
        .route("/users/:id/delete", post(handlers::admin::delete_user))
        // Change password (admin/superadmin only)
        .route("/change-password", get(handlers::admin::change_password_page))
        .route("/change-password", post(handlers::admin::change_password))
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
            match sqlx::query("DELETE FROM users WHERE expires_at IS NOT NULL AND expires_at < datetime('now')")
                .execute(&cleanup_db)
                .await
            {
                Ok(result) => {
                    if result.rows_affected() > 0 {
                        tracing::info!("Cleaned up {} expired user accounts", result.rows_affected());
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
