//! HTTP handlers for recording control endpoints.
//!
//! Provides REST API for starting/stopping recording sessions and logging track markers.
//! Recording is coordinated between RecordingManager (session state, markers) and
//! StreamState (actual file writing during stream).

use crate::auth;
use crate::models;
use crate::recording::{RecordingError, SharedRecordingManager, TrackMarker};
use crate::stream_bridge::SharedStreamState;
use crate::{audio, storage, AppError, AppState, Result};
use axum::{
    extract::ws::{Message, WebSocket},
    extract::{Query, State, WebSocketUpgrade},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use tokio::process::Command;

/// Request body for starting a recording session.
#[derive(Debug, Deserialize)]
pub struct StartRecordingRequest {
    /// ID of the show to record
    pub show_id: i64,
}

/// Request body for adding a track marker.
#[derive(Debug, Deserialize)]
pub struct AddMarkerRequest {
    /// ID of the artist whose track was played
    pub artist_id: i64,
    /// Type of track: "track1", "track2", or "voice_message"
    pub track_type: String,
    /// S3 key of the original track file
    pub track_key: String,
    /// Duration of the track in milliseconds
    pub duration_ms: u64,
}

/// Response for marker addition.
#[derive(Debug, Serialize)]
pub struct MarkerResponse {
    pub success: bool,
    pub marker: TrackMarker,
}

/// Response for stopping a recording.
#[derive(Debug, Serialize)]
pub struct StopRecordingResponse {
    pub success: bool,
    pub message: String,
    pub show_id: i64,
    pub version: String,
    pub marker_count: usize,
    /// S3 key where raw recording was uploaded
    pub raw_key: Option<String>,
    /// S3 key where markers JSON was uploaded
    pub markers_key: Option<String>,
}

/// Response for listing recording versions.
#[derive(Debug, Serialize)]
pub struct RecordingVersionResponse {
    pub id: i64,
    pub show_id: i64,
    pub version: String,
    pub status: String,
    pub duration_ms: Option<i64>,
    pub marker_count: i64,
    pub created_at: String,
    pub finalized_at: Option<String>,
    /// Download URL for the finalized recording (if finalized)
    pub download_url: Option<String>,
    /// Error message if failed
    pub error_message: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct ListRecordingVersionsResponse {
    pub recordings: Vec<RecordingVersionResponse>,
}

/// Helper to require admin authentication
async fn require_admin(state: &Arc<AppState>, headers: &HeaderMap) -> Result<models::User> {
    let token = auth::get_session_from_headers(headers);
    let user = auth::get_current_user(state, token.as_deref())
        .await
        .ok_or_else(|| AppError::Unauthorized("Not authenticated".to_string()))?;

    if !user.role_enum().can_access_admin() {
        return Err(AppError::Forbidden("Admin access required".to_string()));
    }

    Ok(user)
}

/// POST /api/recording/start
///
/// Start a new recording session for the specified show.
/// Only one recording can be active at a time.
/// Also tells the stream to start tee-ing audio chunks to the recording file.
pub async fn start_recording(
    State(state): State<Arc<AppState>>,
    State(recording_manager): State<SharedRecordingManager>,
    State(stream_state): State<SharedStreamState>,
    headers: HeaderMap,
    Json(body): Json<StartRecordingRequest>,
) -> Result<impl IntoResponse> {
    // Require admin authentication
    let _user = require_admin(&state, &headers).await?;

    // Validate show exists
    let show: Option<models::Show> = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(body.show_id)
        .fetch_optional(&state.db)
        .await?;

    let show =
        show.ok_or_else(|| AppError::NotFound(format!("Show {} not found", body.show_id)))?;

    tracing::info!("Starting recording for show {}: {}", show.id, show.title);

    // Start the recording session (creates temp file path)
    let mut manager = recording_manager.lock().await;
    let status = manager
        .start(body.show_id)
        .await
        .map_err(|e| AppError::Internal(e.to_string()))?;

    // Get the temp file path from the session
    let temp_path = manager.get_temp_file_path().ok_or_else(|| {
        AppError::Internal("Recording session started but no temp file path available".to_string())
    })?;

    // Tell the stream to start recording to this file
    let mut stream = stream_state.lock().await;
    stream
        .start_recording(temp_path)
        .await
        .map_err(|e| AppError::Internal(format!("Failed to start stream recording: {}", e)))?;

    Ok((StatusCode::OK, Json(status)))
}

/// GET /api/recording/status
///
/// Get the current recording status.
pub async fn recording_status(
    State(recording_manager): State<SharedRecordingManager>,
) -> impl IntoResponse {
    let manager = recording_manager.lock().await;
    let status = manager.get_status();
    Json(status)
}

/// POST /api/recording/marker
///
/// Add a track marker to the current recording session.
/// Records the current offset from recording start.
pub async fn add_marker(
    State(state): State<Arc<AppState>>,
    State(recording_manager): State<SharedRecordingManager>,
    headers: HeaderMap,
    Json(body): Json<AddMarkerRequest>,
) -> Result<impl IntoResponse> {
    // Require admin authentication
    let _user = require_admin(&state, &headers).await?;

    // Validate track_type
    if !["track1", "track2", "voice_message"].contains(&body.track_type.as_str()) {
        return Err(AppError::Validation(format!(
            "Invalid track_type '{}'. Must be 'track1', 'track2', or 'voice_message'",
            body.track_type
        )));
    }

    // Add the marker
    let mut manager = recording_manager.lock().await;
    let marker = manager
        .add_marker(
            body.artist_id,
            body.track_type,
            body.track_key,
            body.duration_ms,
        )
        .map_err(|e| match e {
            RecordingError::NotRecording => {
                AppError::BadRequest("No recording session active".to_string())
            }
            _ => AppError::Internal(e.to_string()),
        })?;

    Ok((
        StatusCode::OK,
        Json(MarkerResponse {
            success: true,
            marker,
        }),
    ))
}

/// POST /api/recording/stop
///
/// Stop the current recording session and upload raw recording + markers to R2.
/// Also stops the stream from tee-ing audio chunks.
pub async fn stop_recording(
    State(state): State<Arc<AppState>>,
    State(recording_manager): State<SharedRecordingManager>,
    State(stream_state): State<SharedStreamState>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    // Require admin authentication
    let _user = require_admin(&state, &headers).await?;

    // Stop the stream recording first (flushes and closes the file)
    {
        let mut stream = stream_state.lock().await;
        if let Err(e) = stream.stop_recording().await {
            tracing::warn!("Error stopping stream recording: {}", e);
            // Continue anyway - we still want to process the session
        }
    }

    // Stop the recording session
    let mut manager = recording_manager.lock().await;
    let session = manager
        .stop()
        .await
        .map_err(|e| AppError::Internal(e.to_string()))?;

    let session = match session {
        Some(s) => s,
        None => {
            return Err(AppError::BadRequest(
                "No recording session was active".to_string(),
            ));
        }
    };

    let show_id = session.show_id;
    let version = session.version_timestamp.clone();
    let marker_count = session.markers.len();

    // Upload raw recording to R2
    let raw_key = format!("recordings/{}/{}/raw.webm", show_id, version);
    let raw_data = tokio::fs::read(&session.temp_file_path)
        .await
        .map_err(|e| AppError::Internal(format!("Failed to read recording file: {}", e)))?;

    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&raw_key)
        .body(aws_sdk_s3::primitives::ByteStream::from(raw_data))
        .content_type("audio/webm")
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload raw recording: {}", e)))?;

    tracing::info!("Uploaded raw recording to {}", raw_key);

    // Upload markers JSON to R2
    let markers_key = format!("recordings/{}/{}/markers.json", show_id, version);
    let markers_json = session
        .markers_json()
        .map_err(|e| AppError::Internal(e.to_string()))?;

    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&markers_key)
        .body(aws_sdk_s3::primitives::ByteStream::from(
            markers_json.into_bytes(),
        ))
        .content_type("application/json")
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload markers: {}", e)))?;

    tracing::info!("Uploaded markers to {}", markers_key);

    // Create recording version entry in database
    if let Err(e) = crate::db::create_recording_version(
        &state.db,
        show_id,
        &version,
        &raw_key,
        &markers_key,
        marker_count as i64,
    )
    .await
    {
        tracing::error!("Failed to create recording version in database: {}", e);
        // Don't fail the request - the recording was uploaded successfully
    } else {
        tracing::info!(
            "Created recording version in database: show_id={}, version={}",
            show_id,
            version
        );
    }

    // Clean up temp file
    if let Err(e) = tokio::fs::remove_file(&session.temp_file_path).await {
        tracing::warn!("Failed to clean up temp file: {}", e);
    }

    Ok((
        StatusCode::OK,
        Json(StopRecordingResponse {
            success: true,
            message: format!("Recording stopped and uploaded for show {}", show_id),
            show_id,
            version,
            marker_count,
            raw_key: Some(raw_key),
            markers_key: Some(markers_key),
        }),
    ))
}

/// GET /api/shows/:id/recordings
///
/// List all recording versions for a show, including download URLs for finalized recordings.
pub async fn list_recording_versions(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    axum::extract::Path(show_id): axum::extract::Path<i64>,
) -> Result<impl IntoResponse> {
    // Require admin authentication
    let _user = require_admin(&state, &headers).await?;

    // Get all recording versions for this show
    let versions = crate::db::list_recording_versions(&state.db, show_id).await?;

    // Build response with download URLs for finalized recordings
    let mut recordings = Vec::with_capacity(versions.len());

    for v in versions {
        // Generate presigned download URL for finalized recordings
        let download_url = if v.status == "finalized" {
            if let Some(ref key) = v.final_key {
                storage::get_presigned_url(&state, key, 3600 * 24)
                    .await
                    .ok() // 24 hour URL
            } else {
                None
            }
        } else {
            None
        };

        recordings.push(RecordingVersionResponse {
            id: v.id,
            show_id: v.show_id,
            version: v.version,
            status: v.status,
            duration_ms: v.duration_ms,
            marker_count: v.marker_count,
            created_at: v.created_at,
            finalized_at: v.finalized_at,
            download_url,
            error_message: v.error_message,
        });
    }

    Ok(Json(ListRecordingVersionsResponse { recordings }))
}

/// Helper to convert RecordingError to AppError
impl From<RecordingError> for AppError {
    fn from(e: RecordingError) -> Self {
        match e {
            RecordingError::NotRecording => AppError::BadRequest("Not recording".to_string()),
            RecordingError::AlreadyRecording(id) => {
                AppError::BadRequest(format!("Already recording show {}", id))
            }
            _ => AppError::Internal(e.to_string()),
        }
    }
}

// ============================================================================
// Finalize WebSocket Endpoint
// ============================================================================

/// Query parameters for the finalize WebSocket endpoint.
#[derive(Debug, Deserialize)]
pub struct FinalizeQuery {
    /// ID of the show to finalize
    pub show_id: i64,
    /// Version timestamp (e.g., "2026-01-28T19-30-00")
    pub version: String,
}

/// Progress message sent to the client during finalize.
#[derive(Debug, Clone, Serialize)]
pub struct FinalizeProgress {
    /// Current phase: "downloading", "merging", "uploading", "complete", "error"
    pub phase: String,
    /// Progress percentage (0-100)
    pub percent: u8,
    /// Human-readable detail message
    pub detail: String,
    /// Whether this is a resumed session
    #[serde(skip_serializing_if = "Option::is_none")]
    pub resumed: Option<bool>,
}

impl FinalizeProgress {
    fn downloading(percent: u8, detail: impl Into<String>) -> Self {
        Self {
            phase: "downloading".to_string(),
            percent,
            detail: detail.into(),
            resumed: None,
        }
    }

    fn merging(percent: u8, detail: impl Into<String>) -> Self {
        Self {
            phase: "merging".to_string(),
            percent,
            detail: detail.into(),
            resumed: None,
        }
    }

    fn uploading(percent: u8, detail: impl Into<String>) -> Self {
        Self {
            phase: "uploading".to_string(),
            percent,
            detail: detail.into(),
            resumed: None,
        }
    }

    fn complete(detail: impl Into<String>) -> Self {
        Self {
            phase: "complete".to_string(),
            percent: 100,
            detail: detail.into(),
            resumed: None,
        }
    }

    fn error(detail: impl Into<String>) -> Self {
        Self {
            phase: "error".to_string(),
            percent: 0,
            detail: detail.into(),
            resumed: None,
        }
    }

    fn with_resumed(mut self, resumed: bool) -> Self {
        self.resumed = Some(resumed);
        self
    }
}

// ============================================================================
// Checkpoint Recovery
// ============================================================================

/// Finalize phase for checkpoint tracking
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FinalizePhase {
    /// Not started
    NotStarted,
    /// Downloading files from R2
    Downloading,
    /// All files downloaded, ready for merge
    Downloaded,
    /// Merging with FFmpeg
    Merging,
    /// Merge complete, ready for upload
    Merged,
    /// Uploading to R2
    Uploading,
    /// Complete
    Complete,
}

/// Checkpoint state for resumable finalize operations.
///
/// Saved to R2 after each phase completion to enable recovery.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CheckpointState {
    /// Show ID being finalized
    pub show_id: i64,
    /// Version being finalized
    pub version: String,
    /// Current phase
    pub phase: FinalizePhase,
    /// Tracks that have been downloaded (track_key -> local filename)
    pub downloaded_tracks: HashMap<String, String>,
    /// Whether raw.webm has been downloaded
    pub raw_downloaded: bool,
    /// Whether final.mp3 has been generated
    pub merge_complete: bool,
    /// Timestamp when checkpoint was created
    pub created_at: String,
    /// Timestamp when checkpoint was last updated
    pub updated_at: String,
}

impl CheckpointState {
    fn new(show_id: i64, version: &str) -> Self {
        let now = chrono::Utc::now().to_rfc3339();
        Self {
            show_id,
            version: version.to_string(),
            phase: FinalizePhase::NotStarted,
            downloaded_tracks: HashMap::new(),
            raw_downloaded: false,
            merge_complete: false,
            created_at: now.clone(),
            updated_at: now,
        }
    }

    fn update_phase(&mut self, phase: FinalizePhase) {
        self.phase = phase;
        self.updated_at = chrono::Utc::now().to_rfc3339();
    }

    fn mark_raw_downloaded(&mut self) {
        self.raw_downloaded = true;
        self.updated_at = chrono::Utc::now().to_rfc3339();
    }

    fn mark_track_downloaded(&mut self, track_key: &str, local_filename: &str) {
        self.downloaded_tracks
            .insert(track_key.to_string(), local_filename.to_string());
        self.updated_at = chrono::Utc::now().to_rfc3339();
    }

    fn mark_merge_complete(&mut self) {
        self.merge_complete = true;
        self.updated_at = chrono::Utc::now().to_rfc3339();
    }
}

/// Get the R2 key for a checkpoint file.
fn checkpoint_key(show_id: i64, version: &str) -> String {
    format!("recordings/{}/{}/checkpoint.json", show_id, version)
}

/// Load checkpoint from R2 if it exists.
async fn load_checkpoint(
    state: &Arc<AppState>,
    show_id: i64,
    version: &str,
) -> Option<CheckpointState> {
    let key = checkpoint_key(show_id, version);
    match storage::download_file(state, &key).await {
        Ok((data, _)) => match serde_json::from_slice(&data) {
            Ok(checkpoint) => {
                tracing::info!("Loaded checkpoint for show {} version {}", show_id, version);
                Some(checkpoint)
            }
            Err(e) => {
                tracing::warn!("Failed to parse checkpoint: {}", e);
                None
            }
        },
        Err(_) => None, // No checkpoint exists
    }
}

/// Save checkpoint to R2.
async fn save_checkpoint(state: &Arc<AppState>, checkpoint: &CheckpointState) -> Result<()> {
    let key = checkpoint_key(checkpoint.show_id, &checkpoint.version);
    let json = serde_json::to_string_pretty(checkpoint)
        .map_err(|e| AppError::Internal(format!("Failed to serialize checkpoint: {}", e)))?;

    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&key)
        .body(aws_sdk_s3::primitives::ByteStream::from(json.into_bytes()))
        .content_type("application/json")
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to save checkpoint: {}", e)))?;

    tracing::debug!("Saved checkpoint: phase={:?}", checkpoint.phase);
    Ok(())
}

/// Delete checkpoint from R2 after successful completion.
async fn delete_checkpoint(state: &Arc<AppState>, show_id: i64, version: &str) {
    let key = checkpoint_key(show_id, version);
    if let Err(e) = state
        .s3_client
        .delete_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&key)
        .send()
        .await
    {
        tracing::warn!("Failed to delete checkpoint: {}", e);
    } else {
        tracing::info!(
            "Deleted checkpoint for show {} version {}",
            show_id,
            version
        );
    }
}

/// WebSocket upgrade handler for recording finalize.
///
/// Authenticates via session cookie, then streams progress as it:
/// 1. Downloads raw.webm and all track files from R2
/// 2. Merges tracks at their recorded offsets using FFmpeg
/// 3. Uploads final.mp3 to R2
pub async fn finalize_ws_handler(
    ws: WebSocketUpgrade,
    State(state): State<Arc<AppState>>,
    Query(query): Query<FinalizeQuery>,
    headers: HeaderMap,
) -> Result<Response> {
    // Authenticate via session cookie
    let token = auth::get_session_from_headers(&headers);
    let user = auth::get_current_user(&state, token.as_deref())
        .await
        .ok_or_else(|| AppError::Unauthorized("Not authenticated".to_string()))?;

    if !user.role_enum().can_access_admin() {
        return Err(AppError::Forbidden("Admin access required".to_string()));
    }

    tracing::info!(
        "Finalize WebSocket connection from user '{}' for show {} version {}",
        user.username,
        query.show_id,
        query.version
    );

    Ok(ws.on_upgrade(move |socket| handle_finalize_socket(socket, state, query)))
}

/// Handle the finalize WebSocket connection.
async fn handle_finalize_socket(socket: WebSocket, state: Arc<AppState>, query: FinalizeQuery) {
    let (mut sender, mut receiver) = socket.split();

    // Run the finalize process
    let result = run_finalize(&state, &query, &mut sender).await;

    match result {
        Ok(final_key) => {
            // Update the database to mark this recording as finalized
            if let Some(recording) =
                crate::db::get_recording_version(&state.db, query.show_id, &query.version)
                    .await
                    .ok()
                    .flatten()
            {
                if let Err(e) =
                    crate::db::finalize_recording_version(&state.db, recording.id, &final_key).await
                {
                    tracing::error!("Failed to update recording version status: {}", e);
                } else {
                    tracing::info!(
                        "Marked recording version {} as finalized with key {}",
                        recording.id,
                        final_key
                    );
                }
            } else {
                tracing::warn!(
                    "Could not find recording version for show {} version {} to mark as finalized",
                    query.show_id,
                    query.version
                );
            }

            let progress =
                FinalizeProgress::complete(format!("Recording finalized: {}", final_key));
            if let Ok(json) = serde_json::to_string(&progress) {
                let _ = sender.send(Message::Text(json.into())).await;
            }
        }
        Err(e) => {
            tracing::error!(
                "Finalize failed for show {} version {}: {}",
                query.show_id,
                query.version,
                e
            );

            // Update status to failed in the database
            if let Some(recording) =
                crate::db::get_recording_version(&state.db, query.show_id, &query.version)
                    .await
                    .ok()
                    .flatten()
            {
                let _ = crate::db::update_recording_version_status(
                    &state.db,
                    recording.id,
                    "failed",
                    Some(&e.to_string()),
                )
                .await;
            }

            let progress = FinalizeProgress::error(e.to_string());
            if let Ok(json) = serde_json::to_string(&progress) {
                let _ = sender.send(Message::Text(json.into())).await;
            }
        }
    }

    // Close the socket gracefully
    let _ = sender.close().await;

    // Drain any remaining messages
    while let Some(_) = receiver.next().await {}
}

/// Run the complete finalize process, reporting progress via WebSocket.
/// Supports checkpoint recovery for resuming interrupted operations.
async fn run_finalize(
    state: &Arc<AppState>,
    query: &FinalizeQuery,
    sender: &mut futures_util::stream::SplitSink<WebSocket, Message>,
) -> Result<String> {
    let show_id = query.show_id;
    let version = &query.version;
    let temp_dir = PathBuf::from("./data/finalize-temp");

    // Ensure temp directory exists
    tokio::fs::create_dir_all(&temp_dir)
        .await
        .map_err(|e| AppError::Internal(format!("Failed to create temp dir: {}", e)))?;

    let session_dir = temp_dir.join(format!("{}_{}", show_id, version));
    tokio::fs::create_dir_all(&session_dir)
        .await
        .map_err(|e| AppError::Internal(format!("Failed to create session dir: {}", e)))?;

    // =========================================================================
    // Check for existing checkpoint to resume from
    // =========================================================================
    let existing_checkpoint = load_checkpoint(state, show_id, version).await;
    let mut checkpoint = existing_checkpoint
        .clone()
        .unwrap_or_else(|| CheckpointState::new(show_id, version));
    let is_resumed = existing_checkpoint.is_some();

    if is_resumed {
        tracing::info!(
            "Resuming finalize from checkpoint: phase={:?}",
            checkpoint.phase
        );
        send_progress_msg(
            sender,
            FinalizeProgress::downloading(0, format!("Resuming from {:?} phase", checkpoint.phase))
                .with_resumed(true),
        )
        .await;
    }

    // =========================================================================
    // Phase 1: Download markers (always needed)
    // =========================================================================
    send_progress_msg(
        sender,
        FinalizeProgress::downloading(0, "Fetching markers.json"),
    )
    .await;

    let markers_key = format!("recordings/{}/{}/markers.json", show_id, version);
    let (markers_data, _) = storage::download_file(state, &markers_key).await?;
    let markers: Vec<TrackMarker> = serde_json::from_slice(&markers_data)
        .map_err(|e| AppError::Internal(format!("Failed to parse markers.json: {}", e)))?;

    tracing::info!("Loaded {} markers for finalize", markers.len());

    // =========================================================================
    // Phase 1a: Download raw recording (skip if already done)
    // =========================================================================
    let raw_path = session_dir.join("raw.webm");
    let skip_raw = checkpoint.raw_downloaded && raw_path.exists();

    if skip_raw {
        tracing::info!("Skipping raw download (checkpoint: already downloaded)");
        send_progress_msg(
            sender,
            FinalizeProgress::downloading(10, "Raw recording cached"),
        )
        .await;
    } else {
        send_progress_msg(
            sender,
            FinalizeProgress::downloading(10, "Downloading raw recording"),
        )
        .await;

        let raw_key = format!("recordings/{}/{}/raw.webm", show_id, version);
        let (raw_data, _) = storage::download_file(state, &raw_key).await?;
        tokio::fs::write(&raw_path, &raw_data)
            .await
            .map_err(|e| AppError::Internal(format!("Failed to write raw.webm: {}", e)))?;

        tracing::info!("Downloaded raw recording: {} bytes", raw_data.len());
        checkpoint.mark_raw_downloaded();
    }

    // =========================================================================
    // Phase 1b: Download all unique track files (skip already downloaded)
    // =========================================================================
    checkpoint.update_phase(FinalizePhase::Downloading);
    save_checkpoint(state, &checkpoint).await?;

    let mut track_files: HashMap<String, PathBuf> = HashMap::new();
    let unique_tracks: Vec<&str> = markers
        .iter()
        .map(|m| m.track_key.as_str())
        .collect::<std::collections::HashSet<_>>()
        .into_iter()
        .collect();

    let total_tracks = unique_tracks.len();
    for (i, track_key) in unique_tracks.iter().enumerate() {
        // Check if already downloaded in checkpoint
        let local_filename = format!(
            "track_{}_{}",
            i,
            track_key.split('/').last().unwrap_or("track.mp3")
        );
        let track_path = session_dir.join(&local_filename);

        if checkpoint.downloaded_tracks.contains_key(*track_key) && track_path.exists() {
            tracing::debug!(
                "Skipping track {} (checkpoint: already downloaded)",
                track_key
            );
            track_files.insert(track_key.to_string(), track_path);
            continue;
        }

        let percent = 20 + ((i * 30) / total_tracks.max(1)) as u8;
        send_progress_msg(
            sender,
            FinalizeProgress::downloading(
                percent,
                format!("Downloading track {}/{}", i + 1, total_tracks),
            ),
        )
        .await;

        let (track_data, _) = storage::download_file(state, track_key).await?;

        tokio::fs::write(&track_path, &track_data)
            .await
            .map_err(|e| AppError::Internal(format!("Failed to write track: {}", e)))?;

        track_files.insert(track_key.to_string(), track_path.clone());
        checkpoint.mark_track_downloaded(track_key, &local_filename);

        // Save checkpoint after each track download
        save_checkpoint(state, &checkpoint).await?;

        tracing::debug!(
            "Downloaded track: {} ({} bytes)",
            track_key,
            track_data.len()
        );
    }

    checkpoint.update_phase(FinalizePhase::Downloaded);
    save_checkpoint(state, &checkpoint).await?;

    send_progress_msg(
        sender,
        FinalizeProgress::downloading(50, "All files downloaded"),
    )
    .await;

    // =========================================================================
    // Phase 2: Merge with FFmpeg (skip if already done)
    // =========================================================================
    let output_path = session_dir.join("final.mp3");
    let skip_merge = checkpoint.merge_complete && output_path.exists();

    if skip_merge {
        tracing::info!("Skipping FFmpeg merge (checkpoint: already complete)");
        send_progress_msg(sender, FinalizeProgress::merging(100, "Merge cached")).await;
    } else {
        checkpoint.update_phase(FinalizePhase::Merging);
        save_checkpoint(state, &checkpoint).await?;

        send_progress_msg(
            sender,
            FinalizeProgress::merging(0, "Preparing FFmpeg merge"),
        )
        .await;

        // Get duration of raw recording
        let raw_duration_ms = audio::get_duration(&raw_path).await?;
        tracing::info!("Raw recording duration: {} ms", raw_duration_ms);

        // Build FFmpeg command with filter_complex for mixing
        let ffmpeg_result =
            build_and_run_ffmpeg(&raw_path, &markers, &track_files, &output_path, sender).await;

        if let Err(e) = ffmpeg_result {
            // Don't clean up on error - checkpoint allows resume
            return Err(e);
        }

        checkpoint.mark_merge_complete();
        checkpoint.update_phase(FinalizePhase::Merged);
        save_checkpoint(state, &checkpoint).await?;

        send_progress_msg(sender, FinalizeProgress::merging(100, "Merge complete")).await;
    }

    // =========================================================================
    // Phase 3: Upload final.mp3 to R2
    // =========================================================================
    checkpoint.update_phase(FinalizePhase::Uploading);
    save_checkpoint(state, &checkpoint).await?;

    send_progress_msg(
        sender,
        FinalizeProgress::uploading(0, "Reading final output"),
    )
    .await;

    let final_data = tokio::fs::read(&output_path)
        .await
        .map_err(|e| AppError::Internal(format!("Failed to read final.mp3: {}", e)))?;

    let final_key = format!("recordings/{}/{}/final.mp3", show_id, version);

    send_progress_msg(
        sender,
        FinalizeProgress::uploading(50, format!("Uploading {} bytes", final_data.len())),
    )
    .await;

    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&final_key)
        .body(aws_sdk_s3::primitives::ByteStream::from(final_data))
        .content_type("audio/mpeg")
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload final.mp3: {}", e)))?;

    tracing::info!("Uploaded finalized recording to {}", final_key);

    send_progress_msg(sender, FinalizeProgress::uploading(100, "Upload complete")).await;

    // =========================================================================
    // Cleanup: Delete checkpoint and temp files
    // =========================================================================
    checkpoint.update_phase(FinalizePhase::Complete);
    delete_checkpoint(state, show_id, version).await;

    if let Err(e) = tokio::fs::remove_dir_all(&session_dir).await {
        tracing::warn!("Failed to clean up temp directory: {}", e);
    }

    Ok(final_key)
}

/// Build and run the FFmpeg command for merging.
///
/// Creates a filter graph that:
/// 1. Takes the raw recording as the base
/// 2. Delays each track to its recorded offset
/// 3. Mixes all tracks together
async fn build_and_run_ffmpeg(
    raw_path: &PathBuf,
    markers: &[TrackMarker],
    track_files: &HashMap<String, PathBuf>,
    output_path: &PathBuf,
    sender: &mut futures_util::stream::SplitSink<WebSocket, Message>,
) -> Result<()> {
    send_progress_msg(
        sender,
        FinalizeProgress::merging(10, "Building FFmpeg filter graph"),
    )
    .await;

    // Start building command
    let mut args: Vec<String> = vec![
        "-y".to_string(), // Overwrite output
        "-i".to_string(), // Input 0: raw recording
        raw_path.to_string_lossy().to_string(),
    ];

    // Add each track as an input
    let mut input_index = 1;
    let mut filter_inputs: Vec<String> = vec![];

    for marker in markers {
        if let Some(track_path) = track_files.get(&marker.track_key) {
            args.push("-i".to_string());
            args.push(track_path.to_string_lossy().to_string());

            // Build adelay filter for this track: adelay=offset_ms|offset_ms (stereo)
            // [1:a]adelay=5000|5000[a1]
            let delay_filter = format!(
                "[{}:a]adelay={}|{}[a{}]",
                input_index, marker.offset_ms, marker.offset_ms, input_index
            );
            filter_inputs.push(delay_filter);
            input_index += 1;
        }
    }

    send_progress_msg(
        sender,
        FinalizeProgress::merging(20, format!("Mixing {} inputs", input_index)),
    )
    .await;

    // Build the amix filter
    // [0:a][a1][a2]...amix=inputs=N:duration=longest[out]
    if filter_inputs.is_empty() {
        // No tracks to mix, just transcode the raw recording
        args.extend([
            "-vn".to_string(),
            "-acodec".to_string(),
            "libmp3lame".to_string(),
            "-ab".to_string(),
            "192k".to_string(),
            "-ar".to_string(),
            "44100".to_string(),
            output_path.to_string_lossy().to_string(),
        ]);
    } else {
        // Build filter_complex
        let delayed_inputs: String = (1..input_index)
            .map(|i| format!("[a{}]", i))
            .collect::<Vec<_>>()
            .join("");

        let filter_complex = format!(
            "{};[0:a]{}amix=inputs={}:duration=longest:normalize=0[out]",
            filter_inputs.join(";"),
            delayed_inputs,
            input_index
        );

        args.extend([
            "-filter_complex".to_string(),
            filter_complex,
            "-map".to_string(),
            "[out]".to_string(),
            "-vn".to_string(),
            "-acodec".to_string(),
            "libmp3lame".to_string(),
            "-ab".to_string(),
            "192k".to_string(),
            "-ar".to_string(),
            "44100".to_string(),
            output_path.to_string_lossy().to_string(),
        ]);
    }

    tracing::info!("Running FFmpeg with {} args", args.len());
    tracing::debug!("FFmpeg args: {:?}", args);

    send_progress_msg(sender, FinalizeProgress::merging(30, "Running FFmpeg")).await;

    // Run FFmpeg
    let output = Command::new("ffmpeg")
        .args(&args)
        .output()
        .await
        .map_err(|e| AppError::Internal(format!("Failed to run ffmpeg: {}", e)))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        tracing::error!("FFmpeg failed: {}", stderr);
        return Err(AppError::Internal(format!(
            "FFmpeg merge failed: {}",
            stderr.lines().last().unwrap_or("Unknown error")
        )));
    }

    send_progress_msg(sender, FinalizeProgress::merging(90, "FFmpeg completed")).await;

    Ok(())
}

/// Helper to send a progress message over the WebSocket.
async fn send_progress_msg(
    sender: &mut futures_util::stream::SplitSink<WebSocket, Message>,
    progress: FinalizeProgress,
) {
    if let Ok(json) = serde_json::to_string(&progress) {
        let _ = sender.send(Message::Text(json.into())).await;
    }
}
