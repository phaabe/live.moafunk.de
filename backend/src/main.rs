mod auth;
mod config;
mod db;
mod error;
mod handlers;
mod image_overlay;
mod models;
mod pdf;
mod storage;

use axum::{
    extract::DefaultBodyLimit,
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

pub struct AppState {
    pub db: sqlx::SqlitePool,
    pub config: Config,
    pub templates: tera::Tera,
    pub s3_client: aws_sdk_s3::Client,
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

    let state = Arc::new(AppState {
        db,
        config: config.clone(),
        templates,
        s3_client,
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
        // Auth routes
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
        .layer(DefaultBodyLimit::max(config.max_request_body_bytes()))
        .layer(cors)
        .layer(TraceLayer::new_for_http())
        .with_state(state);

    let addr = format!("{}:{}", config.host, config.port);
    tracing::info!("Starting server on {}", addr);

    let listener = tokio::net::TcpListener::bind(&addr).await?;
    axum::serve(listener, app).await?;

    Ok(())
}
