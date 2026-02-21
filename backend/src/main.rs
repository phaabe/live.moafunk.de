mod ai;
mod audio;
mod auth;
mod config;
mod db;
mod error;
mod handlers;
mod image_overlay;
mod instagram;
mod models;
mod pdf;
mod recording;
mod scheduler;
mod soundcloud;
mod storage;
mod stream_bridge;
mod telegram;
mod telegram_notify;
mod video;

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

use recording::SharedRecordingManager;
use stream_bridge::SharedStreamState;

/// Tracks pending cover regeneration requests with debounce
pub type CoverDebounceMap = Arc<RwLock<HashMap<i64, tokio::time::Instant>>>;

/// Tracks pending show update notifications with debounce (show_id -> task handle)
pub type PendingShowNotifications =
    Arc<tokio::sync::Mutex<HashMap<i64, tokio::task::JoinHandle<()>>>>;

/// Active Telegram edit sessions keyed by chat_id (only one edit at a time per chat)
pub type TelegramEditSessions = Arc<tokio::sync::Mutex<HashMap<i64, models::TelegramEditSession>>>;

pub struct AppState {
    pub db: sqlx::SqlitePool,
    pub config: Config,
    pub s3_client: aws_sdk_s3::Client,
    pub stream_state: SharedStreamState,
    /// Recording session manager for show recordings
    pub recording_manager: SharedRecordingManager,
    /// Debounce tracker for show cover regeneration (show_id -> last_request_time)
    pub cover_debounce: CoverDebounceMap,
    /// Cached default cover image (4 black tiles with UN/HEARD branding)
    pub default_cover: tokio::sync::OnceCell<Vec<u8>>,
    /// Telegram bot instance (None if TELEGRAM_BOT_TOKEN not set)
    pub telegram_bot: Option<teloxide::Bot>,
    /// Pending show update notifications (debounced to avoid spam)
    pub pending_show_notifications: PendingShowNotifications,
    /// Active Telegram edit sessions (chat_id -> session)
    pub telegram_edit_sessions: TelegramEditSessions,
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

// Recording handler wrappers that extract recording_manager from AppState
async fn recording_start_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    body: axum::Json<handlers::recording::StartRecordingRequest>,
) -> Result<impl IntoResponse> {
    handlers::recording::start_recording(
        State(state.clone()),
        State(state.recording_manager.clone()),
        State(state.stream_state.clone()),
        headers,
        body,
    )
    .await
}

async fn recording_status_handler(State(state): State<Arc<AppState>>) -> impl IntoResponse {
    handlers::recording::recording_status(State(state.recording_manager.clone())).await
}

async fn recording_marker_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    body: axum::Json<handlers::recording::AddMarkerRequest>,
) -> Result<impl IntoResponse> {
    handlers::recording::add_marker(
        State(state.clone()),
        State(state.recording_manager.clone()),
        headers,
        body,
    )
    .await
}

async fn recording_stop_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    handlers::recording::stop_recording(
        State(state.clone()),
        State(state.recording_manager.clone()),
        State(state.stream_state.clone()),
        headers,
    )
    .await
}

async fn list_recording_versions_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    path: axum::extract::Path<i64>,
) -> Result<impl IntoResponse> {
    handlers::recording::list_recording_versions(State(state), headers, path).await
}

// Recording finalize WebSocket handler wrapper
async fn recording_finalize_ws_handler(
    ws: WebSocketUpgrade,
    State(state): State<Arc<AppState>>,
    query: Query<handlers::recording::FinalizeQuery>,
    headers: HeaderMap,
) -> Result<Response> {
    handlers::recording::finalize_ws_handler(ws, State(state), query, headers).await
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
    // R2 requires path-style addressing (not virtual-hosted style)
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
        .force_path_style(true)
        .build();

    let s3_client = aws_sdk_s3::Client::from_conf(s3_config);

    // Initialize stream state
    let stream_state = stream_bridge::new_shared_state();

    // Initialize recording manager (temp files in ./data/recordings-temp)
    let recording_temp_dir = std::path::PathBuf::from("./data/recordings-temp");
    let recording_manager = recording::new_shared_manager(recording_temp_dir);

    // Initialize cover regeneration debounce tracker
    let cover_debounce = Arc::new(RwLock::new(HashMap::new()));

    // Initialize Telegram bot (if configured)
    let telegram_bot = config.telegram_bot_token.as_ref().map(|token| {
        tracing::info!(
            chat_id = ?config.telegram_admin_chat_id,
            topic_id = ?config.telegram_topic_id,
            "Telegram bot configured"
        );
        teloxide::Bot::new(token)
    });

    // Initialize pending show notifications tracker (for debouncing)
    let pending_show_notifications = Arc::new(tokio::sync::Mutex::new(HashMap::new()));

    // Initialize Telegram edit sessions tracker
    let telegram_edit_sessions: TelegramEditSessions =
        Arc::new(tokio::sync::Mutex::new(HashMap::new()));

    let state = Arc::new(AppState {
        db,
        config: config.clone(),
        s3_client,
        stream_state,
        recording_manager,
        cover_debounce,
        default_cover: tokio::sync::OnceCell::new(),
        telegram_bot,
        pending_show_notifications,
        telegram_edit_sessions,
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
        // Chunked file upload endpoints (for individual files >100MB)
        .route(
            "/api/submit/file-chunk/init/:session_id",
            post(handlers::submit_chunked::submit_file_chunk_init),
        )
        .route(
            "/api/submit/file-chunk/:session_id",
            post(handlers::submit_chunked::submit_file_chunk),
        )
        .route(
            "/api/submit/file-chunk/finalize/:session_id",
            post(handlers::submit_chunked::submit_file_chunk_finalize),
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
        // Artist flow — my show
        .route("/api/my-show", get(handlers::api::api_my_show))
        .route(
            "/api/my-show/upload",
            post(handlers::api::api_my_show_upload),
        )
        .route(
            "/api/my-show/upload/init",
            post(handlers::api::api_my_show_upload_init),
        )
        .route(
            "/api/my-show/upload/chunk/:session_id",
            post(handlers::api::api_my_show_upload_chunk),
        )
        .route(
            "/api/my-show/upload/finalize/:session_id",
            post(handlers::api::api_my_show_upload_finalize),
        )
        .route(
            "/api/my-show/confirm",
            post(handlers::api::api_my_show_confirm),
        )
        .route(
            "/api/my-show/upload",
            axum::routing::delete(handlers::api::api_my_show_delete_upload),
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
            "/api/artists/:id/generate-bio",
            post(handlers::api::api_generate_artist_bio),
        )
        .route(
            "/api/artists/:id/generate-videos",
            post(handlers::api::api_generate_artist_videos),
        )
        .route(
            "/api/artists/:id/generate-instagram-caption",
            post(handlers::api::api_generate_instagram_caption),
        )
        .route(
            "/api/artists/:id/instagram-caption",
            axum::routing::put(handlers::api::api_update_instagram_caption),
        )
        .route(
            "/api/artists/:id/instagram",
            post(handlers::api::api_post_artist_to_instagram),
        )
        .route(
            "/api/artists/:id/telegram-preview",
            post(handlers::api::api_send_artist_telegram_preview),
        )
        .route(
            "/api/artists/:id/picture",
            axum::routing::put(handlers::api::api_update_artist_picture),
        )
        .route(
            "/api/artists/:id/audio",
            axum::routing::put(handlers::api::api_update_artist_audio),
        )
        // Artist overlay gallery (list, save, set active)
        .route(
            "/api/artists/:id/image-proxy",
            get(handlers::api::api_artist_image_proxy),
        )
        .route(
            "/api/artists/:id/overlays",
            get(handlers::api::api_list_artist_overlays),
        )
        .route(
            "/api/artists/:id/overlays",
            post(handlers::api::api_save_artist_overlay),
        )
        .route(
            "/api/artists/:id/overlays/active",
            axum::routing::put(handlers::api::api_set_active_overlay),
        )
        .route(
            "/api/artists/:id/active-preset",
            axum::routing::put(handlers::api::api_set_artist_active_preset),
        )
        // Overlay parameter presets (CRUD)
        .route(
            "/api/overlay-presets",
            get(handlers::api::api_list_overlay_presets),
        )
        .route(
            "/api/overlay-presets",
            post(handlers::api::api_create_overlay_preset),
        )
        .route(
            "/api/overlay-presets/:id",
            axum::routing::put(handlers::api::api_update_overlay_preset),
        )
        .route(
            "/api/overlay-presets/:id",
            axum::routing::delete(handlers::api::api_delete_overlay_preset),
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
            "/api/shows/:id/image-proxy",
            get(handlers::api::api_show_image_proxy),
        )
        .route(
            "/api/shows/:id/overlays",
            get(handlers::api::api_list_show_overlays),
        )
        .route(
            "/api/shows/:id/overlays",
            post(handlers::api::api_save_show_overlay),
        )
        .route(
            "/api/shows/:id/with-artists",
            get(handlers::api::api_show_with_artists),
        )
        .route(
            "/api/shows/:id",
            axum::routing::put(handlers::api::api_update_show),
        )
        .route(
            "/api/shows/:id",
            axum::routing::delete(handlers::api::api_delete_show),
        )
        .route(
            "/api/shows/:id/active-preset",
            axum::routing::put(handlers::api::api_set_show_active_preset),
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
            "/api/shows/:id/regenerate-bio",
            post(handlers::api::api_regenerate_show_bio),
        )
        .route(
            "/api/shows/:id/upload-recording",
            post(handlers::api::api_upload_show_recording),
        )
        // Chunked recording upload (for large files > 100MB to bypass Cloudflare limit)
        .route(
            "/api/shows/:id/upload-recording/init",
            post(handlers::upload_recording_chunked::init_recording_upload),
        )
        .route(
            "/api/shows/:id/upload-recording/chunk/:session_id",
            post(handlers::upload_recording_chunked::upload_recording_chunk),
        )
        .route(
            "/api/shows/:id/upload-recording/finalize/:session_id",
            post(handlers::upload_recording_chunked::finalize_recording_upload),
        )
        .route(
            "/api/shows/:id/upload-cover",
            post(handlers::api::api_upload_show_cover),
        )
        .route(
            "/api/shows/:id/recording",
            axum::routing::delete(handlers::api::api_delete_show_recording),
        )
        .route(
            "/api/shows/:id/instagram",
            post(handlers::api::api_post_show_to_instagram),
        )
        .route(
            "/api/shows/:id/telegram-preview",
            post(handlers::api::api_send_telegram_preview),
        )
        .route(
            "/api/shows/:id/soundcloud/upload",
            post(handlers::api::api_upload_to_soundcloud),
        )
        .route(
            "/api/shows/:id/soundcloud/privacy",
            post(handlers::api::api_set_soundcloud_privacy),
        )
        .route(
            "/api/soundcloud/status",
            get(handlers::api::api_soundcloud_status),
        )
        .route(
            "/api/soundcloud/auth",
            get(handlers::api::api_soundcloud_auth),
        )
        .route(
            "/api/soundcloud/callback",
            get(handlers::api::api_soundcloud_callback),
        )
        .route(
            "/api/soundcloud/disconnect",
            post(handlers::api::api_soundcloud_disconnect),
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
        // Recording API for show recording with timecoded track markers
        .route("/api/recording/start", post(recording_start_handler))
        .route("/api/recording/status", get(recording_status_handler))
        .route("/api/recording/marker", post(recording_marker_handler))
        .route("/api/recording/stop", post(recording_stop_handler))
        .route(
            "/api/shows/:id/recordings",
            get(list_recording_versions_handler),
        )
        // Recording finalize WebSocket for merging tracks with progress
        .route("/ws/recording/finalize", get(recording_finalize_ws_handler))
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

    // Spawn Telegram bot (long-polling, runs alongside HTTP server)
    tokio::spawn(telegram::run(state.clone()));

    // Spawn scheduled show preview task (sends Telegram preview at 19:00 Berlin time)
    {
        let state = state.clone();
        tokio::spawn(async move {
            use chrono::Timelike;

            let mut interval = tokio::time::interval(std::time::Duration::from_secs(60));
            let mut last_check_date = String::new();

            loop {
                interval.tick().await;

                // Only run if Telegram is configured
                if state.telegram_bot.is_none() {
                    continue;
                }

                let berlin_now = chrono::Utc::now().with_timezone(&chrono_tz::Europe::Berlin);

                // Trigger during the first 5 minutes of 19:00 (19:00-19:04) Berlin time
                if berlin_now.hour() != 19 || berlin_now.minute() >= 5 {
                    continue;
                }

                let today = berlin_now.format("%Y-%m-%d").to_string();

                // Only run once per day (skip if already processed today)
                if today == last_check_date {
                    continue;
                }

                // Mark as processed BEFORE running to prevent double-execution
                last_check_date = today.clone();

                tracing::info!("Show preview scheduler: checking for shows on {today}");

                // Find shows today with covers that haven't been previewed today
                let shows: Vec<crate::models::Show> = match sqlx::query_as(
                    "SELECT * FROM shows WHERE date = ? AND cover_generated_at IS NOT NULL \
                     AND (telegram_preview_sent_at IS NULL OR telegram_preview_sent_at < ?)",
                )
                .bind(&today)
                .bind(&format!("{today}T00:00:00"))
                .fetch_all(&state.db)
                .await
                {
                    Ok(shows) => shows,
                    Err(e) => {
                        tracing::error!("Show preview scheduler DB query failed: {e}");
                        continue;
                    }
                };

                for show in &shows {
                    tracing::info!(
                        "Sending scheduled Telegram preview for show {} ('{}')",
                        show.id,
                        show.title
                    );
                    if let Err(e) =
                        crate::telegram_notify::send_show_instagram_preview(&state, show.id).await
                    {
                        tracing::warn!(
                            "Scheduled Telegram preview failed for show {}: {e}",
                            show.id
                        );
                    }
                }
            }
        });
    }

    // Spawn artist preview scheduler (sends Telegram artist previews at 16:00 Berlin time)
    {
        let state = state.clone();
        tokio::spawn(async move {
            use chrono::Timelike;

            let mut interval = tokio::time::interval(std::time::Duration::from_secs(60));
            let mut last_check_date = String::new();

            loop {
                interval.tick().await;

                // Only run if Telegram is configured
                if state.telegram_bot.is_none() {
                    continue;
                }

                let berlin_now = chrono::Utc::now().with_timezone(&chrono_tz::Europe::Berlin);

                // Trigger during the first 5 minutes of the configured hour (default 16:00-16:04) Berlin time
                if berlin_now.hour() != state.config.telegram_artist_preview_hour
                    || berlin_now.minute() >= 5
                {
                    continue;
                }

                let today = berlin_now.format("%Y-%m-%d").to_string();

                // Only run once per day (skip if already processed today)
                if today == last_check_date {
                    continue;
                }

                // Mark as processed BEFORE running to prevent double-execution
                last_check_date = today.clone();

                tracing::info!("Artist preview scheduler: running daily check for {today}");
                scheduler::check_artist_preview_schedule(state.clone()).await;
            }
        });
    }

    let listener = tokio::net::TcpListener::bind(&addr).await?;
    axum::serve(listener, app).await?;

    Ok(())
}
