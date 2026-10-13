//! HTTP handlers for recording control endpoints.
//!
//! Provides REST API for starting/stopping recording sessions and logging track markers.
//! Recording is coordinated between RecordingManager (session state, markers) and
//! StreamState (actual file writing during stream).

use crate::auth;
use crate::models;
use crate::recording::{RecordingError, RecordingSession, SharedRecordingManager, TrackMarker};
use crate::recording_finalizer::FinalizeOps;
use crate::stream_bridge::SharedStreamState;
use crate::{audio, storage, AppError, AppState, Result};
use axum::{
    extract::ws::{Message, WebSocket},
    extract::{Query, State, WebSocketUpgrade},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use futures_util::future::BoxFuture;
use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
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
    /// Offset from recording start when track started playing (in milliseconds)
    pub offset_ms: u64,
    /// Volume level (0-200, where 100 is 100%). Defaults to 100.
    #[serde(default = "default_volume")]
    pub volume: u32,
}

/// Default volume is 100%
fn default_volume() -> u32 {
    100
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
        .add_marker_with_offset(
            body.artist_id,
            body.track_type,
            body.track_key,
            body.duration_ms,
            body.offset_ms,
            body.volume,
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
///
/// Recording is normally finalized automatically by the stream lifecycle (see
/// [`crate::recording_finalizer`]); this endpoint remains for explicit/manual
/// stops. It goes through the same finalizer, so it never aborts or repeats a
/// running finalize.
pub async fn stop_recording(
    State(state): State<Arc<AppState>>,
    State(_recording_manager): State<SharedRecordingManager>,
    State(_stream_state): State<SharedStreamState>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    // Require admin authentication
    let _user = require_admin(&state, &headers).await?;

    let finalized = state
        .recording_finalizer
        .finalize_now(LiveFinalize::new(&state))
        .await
        .map_err(AppError::Internal)?;
    match finalized {
        Some(result) => Ok((
            StatusCode::OK,
            Json(StopRecordingResponse {
                success: true,
                message: format!("Recording stopped and uploaded for show {}", result.show_id),
                show_id: result.show_id,
                version: result.version,
                marker_count: result.marker_count,
                raw_key: Some(result.raw_key),
                markers_key: Some(result.markers_key),
            }),
        )),
        None => Err(AppError::BadRequest(
            "No recording session was active".to_string(),
        )),
    }
}

/// Outcome of finalizing a recording session.
#[derive(Clone, Debug)]
pub struct FinalizedRecording {
    pub show_id: i64,
    pub version: String,
    pub marker_count: usize,
    pub raw_key: String,
    pub markers_key: String,
    /// True if a write failure occurred mid-recording, so the raw archive is
    /// incomplete (the DB version is marked `failed`).
    pub incomplete: bool,
}

/// A recording shorter than this is treated as essentially empty — the recorder
/// started but produced no usable audio (e.g. it died seconds in). No real show
/// is under a minute, so this floor never fails a legitimate broadcast.
pub(crate) const MIN_RECORDING_SECS: u64 = 60;

/// Stable token embedded in the failure reason of a sub-threshold capture so the
/// dead-man's-switch ([`crate::scheduler::check_missing_recordings`]) can word its
/// alert accurately — "under the minimum, not archived" vs. "recorder may be
/// broken" — without re-deriving the duration. Do not reword without updating the
/// scheduler classifier (and note existing `failed` rows carry the old text).
pub(crate) const SHORT_RECORDING_MARKER: &str = "essentially empty";

/// If the recorded duration is implausibly short, return a human-readable reason;
/// otherwise `None`. Returns `None` when the duration is unknown (can't judge).
///
/// We deliberately do NOT compare against the show's *scheduled* slot. Slots are
/// routinely padded (a 3 h show booked into a 12 h block), so a schedule-relative
/// floor fails legitimate short shows — and the recorder runs exactly as long as
/// the broadcast, so on a normal stop the recording is complete by definition.
/// Duration alone cannot distinguish a short show from a truncated one; real
/// truncation is caught by the structural checks (mid-recording write failure,
/// segment-concat failure, R2 size mismatch). This leaves only an absolute floor
/// to catch a recorder that produced essentially nothing.
fn detect_short_recording(show_id: i64, local_duration_ms: Option<u64>) -> Option<String> {
    let duration_ms = local_duration_ms?;
    if duration_ms < MIN_RECORDING_SECS * 1000 {
        let reason = format!(
            "recording for show {} is only {}s — {}",
            show_id,
            duration_ms / 1000,
            SHORT_RECORDING_MARKER
        );
        Some(reason)
    } else {
        None
    }
}

/// A recording session taken off the stream tee, ready to upload.
pub struct DetachedSession {
    session: RecordingSession,
    write_failure: Option<String>,
    /// The segments could not be joined; they stay on disk for boot recovery.
    join_failure: Option<String>,
}

/// The real recording steps behind [`crate::recording_finalizer::Finalizer`],
/// the single path that ends a recording: explicit stop, admin stop, the stop
/// endpoint and the grace finalize after a drop.
pub struct LiveFinalize(Arc<AppState>);

impl LiveFinalize {
    pub fn new(state: &Arc<AppState>) -> Arc<Self> {
        Arc::new(Self(Arc::clone(state)))
    }
}

impl FinalizeOps for LiveFinalize {
    type Session = DetachedSession;

    fn detach(
        &self,
        show: Option<i64>,
    ) -> BoxFuture<'_, std::result::Result<Option<DetachedSession>, String>> {
        Box::pin(async move {
            detach_session(&self.0, show)
                .await
                .map_err(|e| e.to_string())
        })
    }

    fn upload(
        &self,
        session: DetachedSession,
    ) -> BoxFuture<'_, std::result::Result<FinalizedRecording, String>> {
        Box::pin(async move {
            upload_session(&self.0, session)
                .await
                .map_err(|e| e.to_string())
        })
    }

    fn stream_active(&self) -> BoxFuture<'_, bool> {
        Box::pin(async move { self.0.stream_state.lock().await.is_active() })
    }
}

/// Stop the stream tee and take the active recording session, if any. With
/// `show`, a session of another show is left running.
async fn detach_session(
    state: &Arc<AppState>,
    show: Option<i64>,
) -> Result<Option<DetachedSession>> {
    if let Some(expected) = show {
        let current = state.recording_manager.lock().await.current_show_id();
        if current != Some(expected) {
            return Ok(None);
        }
    }

    // Stop the stream tee first (flushes + closes the file) and capture whether
    // a write failure had already abandoned the recording, and whether the
    // segments could be joined.
    let (write_failure, join_failure) = {
        let mut stream = state.stream_state.lock().await;
        let failure = stream.recording_failure().map(|s| s.to_string());
        // Stop the session below anyway, so the next show can record.
        let join_failure = stream.stop_recording().await.err().map(|e| e.to_string());
        (failure, join_failure)
    };

    // Stop the recording session.
    let session = {
        let mut manager = state.recording_manager.lock().await;
        manager
            .stop()
            .await
            .map_err(|e| AppError::Internal(e.to_string()))?
    };

    Ok(session.map(|session| DetachedSession {
        session,
        write_failure,
        join_failure,
    }))
}

/// Upload a detached session's raw recording + markers to R2 and record a
/// `recording_versions` row.
async fn upload_session(
    state: &Arc<AppState>,
    detached: DetachedSession,
) -> Result<FinalizedRecording> {
    let finalized = persist_session(state, &R2Store(state), detached).await?;

    // Auto-publish a completed streamed broadcast to SoundCloud (private, into the
    // show's playlist). Streamed shows finalize here — not via handle_finalize_socket
    // — so this is where their auto-upload fires. Detached + best-effort; skip known-
    // incomplete captures (they're marked `failed` and never reach the archive).
    if !finalized.incomplete {
        let sc_state = Arc::clone(state);
        let sc_show_id = finalized.show_id;
        tokio::spawn(async move {
            crate::soundcloud::auto_upload_on_finalize(&sc_state, sc_show_id).await;
        });
    }

    Ok(finalized)
}

/// The persist half of [`upload_session`], against any [`RecordingStore`].
///
/// After a failed join the local file is empty or cut off, and the segments
/// are the only full copy. Then nothing is uploaded and no row is written: boot
/// recovery joins the segments again. Only the markers and a finalize note are
/// stored, because recovery reads both from R2 and has no other copy of them.
/// The note says whether audio was lost during the show (see
/// [`recovered_failure`]).
async fn persist_session(
    state: &Arc<AppState>,
    store: &dyn RecordingStore,
    detached: DetachedSession,
) -> Result<FinalizedRecording> {
    let DetachedSession {
        session,
        write_failure,
        join_failure,
    } = detached;
    let show_id = session.show_id;
    let version = session.version_timestamp.clone();
    let json = session
        .markers_json()
        .map_err(|e| AppError::Internal(e.to_string()))?;

    if let Some(e) = join_failure {
        let markers_key = format!("recordings/{}/{}/markers.json", show_id, version);
        if let Err(put) = store.put_json(&markers_key, json).await {
            tracing::error!(
                "Cannot store markers for show {} version {} after a failed join: {}",
                show_id,
                version,
                put
            );
        }
        let note = serde_json::json!({ "write_failure": write_failure }).to_string();
        if let Err(put) = store
            .put_json(&finalize_note_key(show_id, &version), note)
            .await
        {
            tracing::error!(
                "Cannot store the finalize note for show {} version {}: {} — recovery will mark it failed",
                show_id,
                version,
                put
            );
        }
        return Err(AppError::Internal(format!(
            "recording for show {} version {} not saved, boot recovery will retry: {}",
            show_id, version, e
        )));
    }

    upload_artifact_and_record(
        state,
        store,
        show_id,
        &version,
        &session.temp_file_path,
        Markers::Session {
            json,
            count: session.markers.len(),
        },
        write_failure,
    )
    .await
}

/// Where a finalize that could not save a recording notes whether audio was
/// lost during the show: `{"write_failure": null}` or the reason.
fn finalize_note_key(show_id: i64, version: &str) -> String {
    format!("recordings/{}/{}/finalize.json", show_id, version)
}

/// Why a recovered recording is incomplete, or `None` when it is complete.
///
/// Only the note of a failed join can tell that no audio was lost. Without it
/// (a crash, a failed save, or a note that could not be stored) the recording
/// may be incomplete. Then it is marked failed and never published.
async fn recovered_failure(
    store: &dyn RecordingStore,
    show_id: i64,
    version: &str,
) -> Result<Option<String>> {
    let key = finalize_note_key(show_id, version);
    if store.size(&key).await?.is_none() {
        return Ok(Some(
            "recovered without a finalize note, so it may be incomplete".to_string(),
        ));
    }
    let data = store.read(&key).await?;
    let note = serde_json::from_slice::<serde_json::Value>(&data).ok();
    Ok(
        match note.as_ref().and_then(|note| note.get("write_failure")) {
            Some(serde_json::Value::Null) => None,
            Some(serde_json::Value::String(reason)) => Some(reason.clone()),
            _ => Some(format!("unreadable finalize note at {}", key)),
        },
    )
}

/// Markers to store with a recording.
enum Markers {
    /// A finished session's own markers. They replace any stored copy.
    Session { json: String, count: usize },
    /// Recovery after a crash has no markers. It keeps a copy that an
    /// interrupted finalize already stored, else it stores an empty list.
    Recovered,
}

/// The R2 calls that persisting a recording makes. [`R2Store`] is the real
/// bucket; tests use a fake, so the persist and recovery paths run offline.
trait RecordingStore: Send + Sync {
    /// Size of an object, or `None` when it does not exist.
    fn size<'a>(&'a self, key: &'a str) -> BoxFuture<'a, Result<Option<u64>>>;
    /// Integrity-checked multipart upload of a raw recording.
    fn upload_raw<'a>(&'a self, key: &'a str, data: Vec<u8>) -> BoxFuture<'a, Result<()>>;
    fn put_json<'a>(&'a self, key: &'a str, json: String) -> BoxFuture<'a, Result<()>>;
    fn read<'a>(&'a self, key: &'a str) -> BoxFuture<'a, Result<Vec<u8>>>;
    /// Publish the show to the curated shows archive; returns the archive key.
    fn publish<'a>(&'a self, show_id: i64, artifact: &'a Path) -> BoxFuture<'a, Result<String>>;
}

/// The production [`RecordingStore`]: the recordings bucket of the app.
struct R2Store<'s>(&'s Arc<AppState>);

impl RecordingStore for R2Store<'_> {
    fn size<'a>(&'a self, key: &'a str) -> BoxFuture<'a, Result<Option<u64>>> {
        Box::pin(storage::object_size(
            &self.0.s3_client,
            &self.0.config.r2_bucket_name,
            key,
        ))
    }

    fn upload_raw<'a>(&'a self, key: &'a str, data: Vec<u8>) -> BoxFuture<'a, Result<()>> {
        Box::pin(storage::upload_multipart(
            &self.0.s3_client,
            &self.0.config.r2_bucket_name,
            key,
            data,
            "audio/webm",
        ))
    }

    fn put_json<'a>(&'a self, key: &'a str, json: String) -> BoxFuture<'a, Result<()>> {
        Box::pin(async move {
            self.0
                .s3_client
                .put_object()
                .bucket(&self.0.config.r2_bucket_name)
                .key(key)
                .body(aws_sdk_s3::primitives::ByteStream::from(json.into_bytes()))
                .content_type("application/json")
                .send()
                .await
                .map_err(|e| AppError::Storage(format!("Failed to upload {}: {}", key, e)))?;
            Ok(())
        })
    }

    fn read<'a>(&'a self, key: &'a str) -> BoxFuture<'a, Result<Vec<u8>>> {
        Box::pin(async move {
            let object = self
                .0
                .s3_client
                .get_object()
                .bucket(&self.0.config.r2_bucket_name)
                .key(key)
                .send()
                .await
                .map_err(|e| AppError::Storage(format!("Failed to download {}: {}", key, e)))?;
            let data = object
                .body
                .collect()
                .await
                .map_err(|e| AppError::Storage(format!("Failed to read {}: {}", key, e)))?;
            Ok(data.into_bytes().to_vec())
        })
    }

    fn publish<'a>(&'a self, show_id: i64, artifact: &'a Path) -> BoxFuture<'a, Result<String>> {
        Box::pin(publish_stream_to_shows_archive(self.0, show_id, artifact))
    }
}

/// Upload a finished raw recording artifact to R2, verify it, persist the
/// markers JSON + a `recording_versions` row, and delete the local artifact
/// only after R2 has confirmed it and the row is written.
///
/// The live stop path ([`upload_session`]) and startup recovery
/// ([`recover_orphaned_recordings`]) share this path. A raw object that an
/// interrupted finalize already uploaded with the same size is reused, not
/// uploaded again. If the row cannot be written, this returns an error and
/// keeps the local file, so the next recovery can finish the job.
/// `write_failure`, when set, marks the row `failed` (the archive is known
/// incomplete); recovery passes `None` and relies on the short-recording check.
async fn upload_artifact_and_record(
    state: &Arc<AppState>,
    store: &dyn RecordingStore,
    show_id: i64,
    version: &str,
    artifact_path: &Path,
    markers: Markers,
    write_failure: Option<String>,
) -> Result<FinalizedRecording> {
    // Read the local artifact and probe its duration BEFORE upload — we keep this
    // file on disk until R2 has verified it and the row exists (see cleanup below).
    let raw_key = format!("recordings/{}/{}/raw.webm", show_id, version);
    let raw_data = tokio::fs::read(artifact_path)
        .await
        .map_err(|e| AppError::Internal(format!("Failed to read recording file: {}", e)))?;
    let local_size = raw_data.len() as u64;
    let local_duration_ms = audio::get_duration(artifact_path).await.ok();

    // An interrupted finalize may have uploaded this file already. A missing,
    // different or unreadable object is uploaded (again).
    let reused = local_size > 0 && store.size(&raw_key).await.ok().flatten() == Some(local_size);
    if reused {
        tracing::info!(
            "Raw recording {} is already in R2 with the local size; not uploading again",
            raw_key
        );
    } else {
        // Resumable, integrity-checked multipart upload (explicit CRC32C). On
        // failure this returns Err and we keep the local file.
        store.upload_raw(&raw_key, raw_data).await.map_err(|e| {
            crate::metrics::inc_recording_r2_upload_failure();
            AppError::Storage(format!("Failed to upload raw recording: {}", e))
        })?;
    }

    // Verify before delete: HEAD the object and confirm the size matches, and
    // sanity-check the duration against the scheduled show length (a recorder
    // that died early still produces a valid-but-short object).
    let remote_size = if reused {
        local_size
    } else {
        store.size(&raw_key).await.ok().flatten().unwrap_or(0)
    };
    let size_verified = remote_size == local_size && local_size > 0;

    let mut failure_reason = write_failure;
    if !size_verified {
        let msg = format!(
            "R2 size mismatch after upload (local {} != remote {})",
            local_size, remote_size
        );
        tracing::error!("{}", msg);
        crate::metrics::inc_recording_size_mismatch();
        failure_reason.get_or_insert(msg);
    }
    if let Some(short) = detect_short_recording(show_id, local_duration_ms) {
        tracing::warn!("Recording for show {} looks short: {}", show_id, short);
        failure_reason.get_or_insert(short);
    }

    let incomplete = failure_reason.is_some();
    if incomplete {
        crate::metrics::inc_recording_incomplete();
    }
    tracing::info!(
        "Raw recording at {} ({} bytes, reused={}, size_verified={}, duration_ms={:?})",
        raw_key,
        local_size,
        reused,
        size_verified,
        local_duration_ms
    );

    let markers_key = format!("recordings/{}/{}/markers.json", show_id, version);
    let marker_count = match markers {
        Markers::Session { json, count } => {
            store
                .put_json(&markers_key, json)
                .await
                .map_err(|e| AppError::Storage(format!("Failed to upload markers: {}", e)))?;
            count
        }
        Markers::Recovered => match store.size(&markers_key).await? {
            Some(_) => {
                // Best-effort count for the row; the stored markers stay as they are.
                let count = match store.read(&markers_key).await {
                    Ok(data) => serde_json::from_slice::<Vec<serde_json::Value>>(&data)
                        .map(|markers| markers.len())
                        .unwrap_or(0),
                    Err(e) => {
                        tracing::warn!("Cannot read stored markers {}: {}", markers_key, e);
                        0
                    }
                };
                tracing::info!(
                    "Kept {} stored marker(s) at {} for the recovered recording",
                    count,
                    markers_key
                );
                count
            }
            None => {
                store
                    .put_json(&markers_key, "[]".to_string())
                    .await
                    .map_err(|e| AppError::Storage(format!("Failed to upload markers: {}", e)))?;
                0
            }
        },
    };
    tracing::info!("Markers at {}", markers_key);

    // The row comes before the local file may go. Without it, the dashboard
    // shows no recording, so keep the file for the next recovery.
    let recording = crate::db::create_recording_version(
        &state.db,
        show_id,
        version,
        &raw_key,
        &markers_key,
        marker_count as i64,
    )
    .await
    .map_err(|e| {
        tracing::error!(
            "Failed to create recording version for show {} version {}: {} — keeping {:?} for recovery",
            show_id,
            version,
            e,
            artifact_path
        );
        AppError::Internal(format!("Failed to create recording version: {}", e))
    })?;
    tracing::info!(
        "Created recording version in database: show_id={}, version={}",
        show_id,
        version
    );
    // Mark incomplete recordings as failed so the operator sees the gap
    // (write failure, R2 size mismatch, or a suspiciously short archive).
    if let Some(ref reason) = failure_reason {
        tracing::error!(
            "Marking recording version {} as failed: {}",
            recording.id,
            reason
        );
        if let Err(e) = crate::db::update_recording_version_status(
            &state.db,
            recording.id,
            "failed",
            Some(reason),
        )
        .await
        {
            tracing::error!("Failed to mark recording version as failed: {}", e);
        }
    }

    // Persist the probed duration (best-effort) so the dashboard can show the
    // show length — including for `failed`/short captures, whose length is
    // exactly what the "essentially empty" alert is about.
    if let Some(ms) = local_duration_ms {
        if let Err(e) = crate::db::set_recording_duration(&state.db, recording.id, ms as i64).await
        {
            tracing::warn!(
                "Failed to store duration for recording {}: {}",
                recording.id,
                e
            );
        }
    }

    // Auto-publish the streamed broadcast to the curated shows archive
    // (moafunk-prod/shows/{type}/{date}-{title}/…mp3). Best-effort convenience
    // copy — any failure is logged but never fails the recording (the raw capture
    // in `r2_bucket_name` stays the system of record). Skip known-bad captures so a
    // truncated/failed show never lands in the public archive. Must run before the
    // cleanup below, which deletes the local artifact we transcode from. On success,
    // record the archive key so the API can offer a download without a finalize.
    if incomplete {
        tracing::warn!(
            "Skipping shows-archive publish for show {} — recording marked incomplete",
            show_id
        );
    } else {
        match store.publish(show_id, artifact_path).await {
            Ok(archive_key) => {
                if let Err(e) =
                    crate::db::set_recording_archive_key(&state.db, recording.id, &archive_key)
                        .await
                {
                    tracing::error!(
                        "Failed to store archive key for recording {}: {}",
                        recording.id,
                        e
                    );
                }
            }
            Err(e) => {
                tracing::error!(
                    "Failed to publish streamed show {} to shows archive: {}",
                    show_id,
                    e
                );
            }
        }
    }

    // Verify-before-delete: only remove the local artifact once R2 confirms the
    // object landed intact (size match). On a mismatch we keep it for recovery.
    if size_verified {
        if let Err(e) = tokio::fs::remove_file(artifact_path).await {
            tracing::warn!("Failed to clean up temp file: {}", e);
        }
    } else {
        tracing::warn!(
            "Keeping local recording {:?} — R2 verification did not pass",
            artifact_path
        );
    }

    Ok(FinalizedRecording {
        show_id,
        version: version.to_string(),
        marker_count,
        raw_key,
        markers_key,
        incomplete,
    })
}
/// Parse a segment directory name `recording_{show_id}_{version}.segs` into its
/// `(show_id, version)` parts. `version` is a `%Y-%m-%dT%H-%M-%S` timestamp and
/// therefore contains no `_`, so splitting on the first `_` after the prefix is
/// unambiguous. Returns `None` for any name that doesn't match the shape.
fn parse_seg_dir_name(name: &str) -> Option<(i64, String)> {
    let rest = name.strip_suffix(".segs")?.strip_prefix("recording_")?;
    let (id_str, version) = rest.split_once('_')?;
    let show_id: i64 = id_str.parse().ok()?;
    if version.is_empty() {
        return None;
    }
    Some((show_id, version.to_string()))
}

/// Parse a joined artifact name `recording_{show_id}_{version}.webm`, the
/// sibling of the segment dir, like [`parse_seg_dir_name`].
fn parse_artifact_name(name: &str) -> Option<(i64, String)> {
    parse_seg_dir_name(&format!("{}.segs", name.strip_suffix(".webm")?))
}

/// What a crashed or interrupted recording left in the temp dir.
#[derive(Debug, Default)]
struct Leftover {
    /// `recording_{id}_{version}.segs/`: the recorder's segments. Some may be
    /// gone when a crash hit the removal after the join.
    seg_dir: Option<PathBuf>,
    /// `recording_{id}_{version}.webm`: the joined file, or the empty file a
    /// session creates at start.
    artifact: Option<PathBuf>,
}

#[derive(Debug, Default, PartialEq)]
struct RecoveryCounts {
    recovered: u32,
    skipped: u32,
    failed: u32,
}

/// Recover recordings that a crash or failed finalize left on disk.
///
/// A restart mid-show kills the recorder FFmpeg (`kill_on_drop`) and leaves a
/// `recording_{show_id}_{version}.segs/` dir of MPEG-TS segments. A crash or a
/// failed row insert after the join leaves `recording_{show_id}_{version}.webm`.
/// `cleanup_stale_files` never deletes either, so they wait for this.
///
/// Run once at startup. For each recording present at boot that has no
/// `recording_versions` row, this keeps the larger of the joined file and a new
/// join of the surviving segments ([`choose_artifact`]), then runs the normal
/// upload + verify + record path ([`upload_artifact_and_record`]) with recovered
/// markers. A recording counts as complete only when a finalize note says so
/// ([`recovered_failure`]); otherwise it is marked failed and not published.
/// A raw object already in R2 with the same size is reused.
/// A recording that has a row is persisted; its leftovers are removed.
/// Idempotent: a second run adds no row and uploads nothing. Failures are logged
/// and the files stay for the next attempt and the dead-man's-switch. The live
/// session's own files are never touched.
pub async fn recover_orphaned_recordings(state: &Arc<AppState>, temp_dir: &Path) {
    recover_leftovers(state, &R2Store(state), temp_dir).await;
}

async fn recover_leftovers(
    state: &Arc<AppState>,
    store: &dyn RecordingStore,
    temp_dir: &Path,
) -> RecoveryCounts {
    let mut counts = RecoveryCounts::default();
    let mut read_dir = match tokio::fs::read_dir(temp_dir).await {
        Ok(rd) => rd,
        Err(e) => {
            // A fresh install has no temp dir yet — not an error.
            tracing::debug!(
                "Recording recovery: nothing to scan in {:?}: {}",
                temp_dir,
                e
            );
            return counts;
        }
    };

    // Snapshot the leftovers present at boot, grouped by recording.
    let mut leftovers: std::collections::BTreeMap<(i64, String), Leftover> =
        std::collections::BTreeMap::new();
    loop {
        match read_dir.next_entry().await {
            Ok(Some(entry)) => {
                let path = entry.path();
                let name = entry.file_name().to_string_lossy().into_owned();
                if name.ends_with(".segs") && path.is_dir() {
                    match parse_seg_dir_name(&name) {
                        Some(id) => leftovers.entry(id).or_default().seg_dir = Some(path),
                        None => tracing::warn!(
                            "Recording recovery: unparseable segment dir {:?}, skipping",
                            path
                        ),
                    }
                } else if path.is_file() {
                    if let Some(id) = parse_artifact_name(&name) {
                        leftovers.entry(id).or_default().artifact = Some(path);
                    }
                }
            }
            Ok(None) => break,
            Err(e) => {
                tracing::warn!("Recording recovery: error scanning {:?}: {}", temp_dir, e);
                break;
            }
        }
    }

    if leftovers.is_empty() {
        return counts;
    }

    tracing::info!(
        "Recording recovery: found {} leftover recording(s) to evaluate",
        leftovers.len()
    );

    let active = state.recording_manager.lock().await.get_temp_file_path();
    for ((show_id, version), leftover) in leftovers {
        // The joined file's path, as the live stop path names it.
        let artifact_path = temp_dir.join(format!("recording_{}_{}.webm", show_id, version));
        if active.as_deref() == Some(artifact_path.as_path()) {
            tracing::info!(
                "Recording recovery: show {} version {} is the live session, skipping",
                show_id,
                version
            );
            counts.skipped += 1;
            continue;
        }

        match crate::db::get_recording_version(&state.db, show_id, &version).await {
            Ok(Some(_)) => {
                remove_persisted_leftover(store, show_id, &version, &leftover).await;
                counts.skipped += 1;
                continue;
            }
            Ok(None) => {}
            Err(e) => {
                tracing::error!(
                    "Recording recovery: cannot read the row for show {} version {}: {} — leaving files in place",
                    show_id,
                    version,
                    e
                );
                counts.failed += 1;
                continue;
            }
        }

        // Only a finalize note can tell that the recording is complete.
        let write_failure = match recovered_failure(store, show_id, &version).await {
            Ok(failure) => failure,
            Err(e) => {
                tracing::error!(
                    "Recording recovery: cannot read the finalize note for show {} version {}: {} — leaving files in place",
                    show_id,
                    version,
                    e
                );
                counts.failed += 1;
                continue;
            }
        };

        if let Err(reason) = choose_artifact(leftover.seg_dir.as_deref(), &artifact_path).await {
            // Leave it for the next boot and the dead-man's-switch; never
            // upload a 0-byte or possibly cut-off file.
            tracing::warn!(
                "Recording recovery: cannot recover show {} version {}: {} — leaving files in place",
                show_id,
                version,
                reason
            );
            counts.failed += 1;
            continue;
        }

        match upload_artifact_and_record(
            state,
            store,
            show_id,
            &version,
            &artifact_path,
            Markers::Recovered,
            write_failure,
        )
        .await
        {
            Ok(_) => {
                tracing::info!(
                    "Recording recovery: recovered show {} version {}",
                    show_id,
                    version
                );
                if let Some(seg_dir) = &leftover.seg_dir {
                    if let Err(e) = tokio::fs::remove_dir_all(seg_dir).await {
                        tracing::warn!(
                            "Recording recovery: failed to remove seg dir {:?} after upload: {}",
                            seg_dir,
                            e
                        );
                    }
                }
                counts.recovered += 1;
            }
            Err(e) => {
                tracing::error!(
                    "Recording recovery: upload failed for show {} version {}: {} — leaving files for retry",
                    show_id,
                    version,
                    e
                );
                counts.failed += 1;
            }
        }
    }

    tracing::info!(
        "Recording recovery complete: {} recovered, {} skipped, {} failed/left",
        counts.recovered,
        counts.skipped,
        counts.failed
    );
    counts
}

/// Leave the best copy of a recording without a row at `artifact_path`.
///
/// The live stop path joins the segments into that file, then removes the
/// segment dir. A crash during the join leaves a cut-off file next to all
/// segments. A crash during the removal leaves the complete file next to some
/// segments. So the segments are joined into a candidate inside their dir, and
/// the larger file wins: a cut-off write or a join of fewer segments is always
/// smaller. A dir without segments (a crash before the first one flushed)
/// leaves the joined file as it is.
///
/// Errors when the join fails, because the joined file may be cut off: both
/// copies stay for the next attempt. Also errors when there is nothing to upload.
async fn choose_artifact(
    seg_dir: Option<&Path>,
    artifact_path: &Path,
) -> std::result::Result<(), String> {
    let mut size = tokio::fs::metadata(artifact_path)
        .await
        .map_or(0, |m| m.len());
    if let Some(seg_dir) = seg_dir {
        let segments = has_segments(seg_dir)
            .await
            .map_err(|e| format!("cannot read {:?}: {}", seg_dir, e))?;
        if segments {
            let candidate = seg_dir.join(REJOINED_FILE);
            crate::stream_bridge::concat_segments(seg_dir, &candidate)
                .await
                .map_err(|e| format!("cannot join {:?}: {}", seg_dir, e))?;
            let joined = tokio::fs::metadata(&candidate)
                .await
                .map_err(|e| format!("cannot read {:?}: {}", candidate, e))?
                .len();
            if joined > size {
                tokio::fs::rename(&candidate, artifact_path)
                    .await
                    .map_err(|e| format!("move {:?} into place: {}", candidate, e))?;
                size = joined;
            } else {
                tracing::info!(
                    "Recording recovery: {:?} ({} bytes) is not shorter than the segments in {:?} ({} bytes), keeping it",
                    artifact_path,
                    size,
                    seg_dir,
                    joined
                );
            }
        }
    }
    if size == 0 {
        return Err(format!(
            "{:?} is empty and there are no segments",
            artifact_path
        ));
    }
    Ok(())
}

/// True if `seg_dir` holds at least one segment.
async fn has_segments(seg_dir: &Path) -> std::io::Result<bool> {
    let mut entries = tokio::fs::read_dir(seg_dir).await?;
    while let Some(entry) = entries.next_entry().await? {
        if crate::stream_bridge::is_segment_file(&entry.path()) {
            return Ok(true);
        }
    }
    Ok(false)
}

/// The candidate join inside a segment dir. It is not a segment, so a later
/// join ignores it, and it goes with the dir.
const REJOINED_FILE: &str = "rejoined.webm";

/// The recording has a row, so it is persisted. Remove the segment dir, and
/// the joined file only when R2 holds an object of the same size: a `failed`
/// row can point at a short object, and then the local copy is the good one.
async fn remove_persisted_leftover(
    store: &dyn RecordingStore,
    show_id: i64,
    version: &str,
    leftover: &Leftover,
) {
    tracing::info!(
        "Recording recovery: show {} version {} already has a row, removing leftovers",
        show_id,
        version
    );
    if let Some(seg_dir) = &leftover.seg_dir {
        if let Err(e) = tokio::fs::remove_dir_all(seg_dir).await {
            tracing::warn!(
                "Recording recovery: failed to remove stale seg dir {:?}: {}",
                seg_dir,
                e
            );
        }
    }
    let Some(artifact) = &leftover.artifact else {
        return;
    };
    let raw_key = format!("recordings/{}/{}/raw.webm", show_id, version);
    let local = tokio::fs::metadata(artifact).await.map(|m| m.len()).ok();
    let remote = store.size(&raw_key).await.ok().flatten();
    if local.is_some_and(|size| size > 0) && local == remote {
        if let Err(e) = tokio::fs::remove_file(artifact).await {
            tracing::warn!("Recording recovery: failed to remove {:?}: {}", artifact, e);
        }
    } else {
        tracing::warn!(
            "Recording recovery: keeping {:?}: its size {:?} does not match {} in R2 ({:?})",
            artifact,
            local,
            raw_key,
            remote
        );
    }
}
/// Ensure a recording session is running for `show_id`, idempotently.
///
/// Called when a live stream connects (auto-record on go-live). Safe to call on
/// every (re)connect:
/// - If already recording this show, it's a no-op (a transient WS reconnect must
///   NOT restart the archive or truncate the tee file).
/// - If recording a *different* show, the existing session is left untouched and
///   a warning is logged (the caller decides whether to take over).
/// - Otherwise a new session is started and the stream tee is pointed at it.
pub async fn ensure_recording_started(state: &Arc<AppState>, show_id: i64) -> Result<()> {
    // Validate the show exists before recording for it.
    let show: Option<models::Show> = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(show_id)
        .fetch_optional(&state.db)
        .await?;
    if show.is_none() {
        tracing::warn!(
            "Auto-record skipped: show {} not found (stream will continue unrecorded)",
            show_id
        );
        return Ok(());
    }

    let temp_path = {
        let mut manager = state.recording_manager.lock().await;
        match manager.current_show_id() {
            Some(current) if current == show_id => {
                // Already recording this show (reconnect) — keep the existing tee.
                return Ok(());
            }
            Some(current) => {
                tracing::warn!(
                    "Stream for show {} connected while recording show {}; leaving existing recording untouched",
                    show_id,
                    current
                );
                return Ok(());
            }
            None => {
                manager
                    .start(show_id)
                    .await
                    .map_err(|e| AppError::Internal(e.to_string()))?;
                manager.get_temp_file_path().ok_or_else(|| {
                    AppError::Internal(
                        "Recording session started but no temp file path available".to_string(),
                    )
                })?
            }
        }
    };

    // Point the stream tee at the new recording file.
    let mut stream = state.stream_state.lock().await;
    stream
        .start_recording(temp_path)
        .await
        .map_err(|e| AppError::Internal(format!("Failed to start stream recording: {}", e)))?;

    tracing::info!("Auto-started recording for show {}", show_id);
    Ok(())
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

/// Request body for [`reexport_recording_to_archive`]. Empty body is fine.
#[derive(Debug, Default, Deserialize)]
pub struct ReexportRequest {
    /// Version to re-export. Defaults to the show's most recent recording.
    #[serde(default)]
    pub version: Option<String>,
}

/// Response for a successful re-export to the shows archive.
#[derive(Debug, Serialize)]
pub struct ReexportResponse {
    pub show_id: i64,
    pub version: String,
    pub archive_key: String,
}

/// POST /api/shows/:id/recordings/reexport
///
/// Re-derive the curated archive mp3 (`moafunk-prod/shows/…mp3`) from a finished
/// raw capture and clear a stale `failed` status. Recovery tool for a recording
/// the verifier wrongly flagged (e.g. a normal-length show in a padded slot) or
/// whose best-effort archive publish was skipped/failed. The raw capture under
/// `recordings/…` is the system of record and is never touched — this only pulls
/// it back, transcodes a fresh mp3 to the archive, and un-fails the row. Admin only.
pub async fn reexport_recording_to_archive(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    axum::extract::Path(show_id): axum::extract::Path<i64>,
    body: Option<Json<ReexportRequest>>,
) -> Result<impl IntoResponse> {
    let _user = require_admin(&state, &headers).await?;
    let req = body.map(|Json(b)| b).unwrap_or_default();
    let resp = reexport_to_archive(&state, show_id, req.version).await?;
    Ok(Json(resp))
}

/// Core of the archive re-export, shared by the HTTP handler and the
/// `reexport-archive` CLI subcommand (which carries no auth — it runs locally on
/// the box via `docker exec`). Resolves the target version (explicit or latest),
/// pulls the raw capture from R2, re-derives the archive mp3, and clears any
/// stale `failed` status.
pub async fn reexport_to_archive(
    state: &Arc<AppState>,
    show_id: i64,
    version_filter: Option<String>,
) -> Result<ReexportResponse> {
    // Resolve the target version: the explicit one, else the most recent.
    let version = match version_filter {
        Some(ref v) => crate::db::get_recording_version(&state.db, show_id, v)
            .await?
            .ok_or_else(|| {
                AppError::NotFound(format!("No recording '{}' for show {}", v, show_id))
            })?,
        None => crate::db::list_recording_versions(&state.db, show_id)
            .await?
            .into_iter()
            .next()
            .ok_or_else(|| AppError::NotFound(format!("No recordings for show {}", show_id)))?,
    };

    let raw_key = version.raw_key.clone().ok_or_else(|| {
        AppError::BadRequest(format!(
            "Recording '{}' for show {} has no raw capture to re-export",
            version.version, show_id
        ))
    })?;

    // Pull the raw capture (system of record) back from R2 to a temp file so the
    // existing transcode→publish path can run file→file. ./data/recordings-temp
    // is the same scratch dir the live recorder uses.
    let (raw_data, _content_type) = storage::download_file(state, &raw_key).await?;
    let temp_path = PathBuf::from("./data/recordings-temp")
        .join(format!("reexport_{}_{}.webm", show_id, version.version));
    if let Some(parent) = temp_path.parent() {
        let _ = tokio::fs::create_dir_all(parent).await;
    }
    tokio::fs::write(&temp_path, &raw_data)
        .await
        .map_err(|e| AppError::Internal(format!("Failed to stage raw recording: {}", e)))?;

    // Re-derive the archive mp3, always cleaning up the staged webm afterwards.
    let publish = publish_stream_to_shows_archive(state, show_id, &temp_path).await;
    let _ = tokio::fs::remove_file(&temp_path).await;
    let archive_key = publish?;

    // The capture is a complete raw recording — clear any stale `failed` status so
    // it stops counting as a missing archive (the dead-man's-switch).
    if version.status == "failed" {
        if let Err(e) =
            crate::db::update_recording_version_status(&state.db, version.id, "raw", None).await
        {
            tracing::error!(
                "Re-export: failed to clear failed status on version {}: {}",
                version.id,
                e
            );
        }
    }

    tracing::info!(
        "Re-exported show {} recording '{}' to shows archive: {}",
        show_id,
        version.version,
        archive_key
    );

    Ok(ReexportResponse {
        show_id,
        version: version.version,
        archive_key,
    })
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

                    // Fire-and-forget: auto-publish the finalized take to SoundCloud
                    // (private, with title + description). Detached so it neither
                    // delays the finalize socket nor fails the finalize on error.
                    let sc_state = Arc::clone(&state);
                    let sc_show_id = query.show_id;
                    tokio::spawn(async move {
                        crate::soundcloud::auto_upload_on_finalize(&sc_state, sc_show_id).await;
                    });
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

    // Note: the curated shows archive (moafunk-prod/shows/…) is populated from the
    // *streamed broadcast* on stream-end (see `publish_stream_to_shows_archive`),
    // NOT from this UNHEARD multi-track merge. UNHEARD finalize stays out of the
    // shows archive — its `final.mp3` lives only under `recordings/…` above.

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

/// Transcode a streamed broadcast to MP3 and publish it to the curated
/// shows-archive bucket (`r2_shows_bucket_name`) under a human-friendly
/// `shows/{type}/{date}-{title}/…mp3` key (see [`storage::build_show_archive_key`]).
///
/// The recorded artifact is whatever the browser captured (WebM/Opus), so it is
/// transcoded to MP3 first. The transcode runs file→file and the MP3 is streamed
/// to R2 via `ByteStream::from_path`, so a multi-hour show never doubles in
/// memory. Called best-effort from [`upload_artifact_and_record`] when a streamed
/// show ends — a failure here never affects the raw capture (the system of record
/// under `recordings/…`).
async fn publish_stream_to_shows_archive(
    state: &Arc<AppState>,
    show_id: i64,
    artifact_path: &Path,
) -> Result<String> {
    let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(show_id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| AppError::NotFound(format!("Show {} not found", show_id)))?;

    // Build the human-friendly key, then number it (`…-2.mp3`, `…-3.mp3`, …) if a
    // recording for this show already exists, so repeat broadcasts of the same
    // show never overwrite each other.
    let base_key = storage::build_show_archive_key(&show.show_type, &show.title, &show.date);
    let key = storage::unique_object_key(
        &state.s3_client,
        &state.config.r2_shows_bucket_name,
        &base_key,
    )
    .await;

    // Transcode the captured broadcast (WebM/Opus) to a temp MP3 on disk.
    let mp3_path = audio::convert_file_to_mp3(artifact_path, &state.config).await?;

    // Upload, then always clean up the temp MP3 regardless of upload outcome.
    let upload = async {
        let body = aws_sdk_s3::primitives::ByteStream::from_path(&mp3_path)
            .await
            .map_err(|e| {
                AppError::Internal(format!(
                    "Failed to open transcoded mp3 for shows upload: {}",
                    e
                ))
            })?;

        state
            .s3_client
            .put_object()
            .bucket(&state.config.r2_shows_bucket_name)
            .key(&key)
            .body(body)
            .content_type("audio/mpeg")
            .send()
            .await
            .map_err(|e| AppError::Storage(format!("Failed to upload to shows archive: {}", e)))?;
        Ok::<(), AppError>(())
    }
    .await;

    let _ = tokio::fs::remove_file(&mp3_path).await;
    upload?;

    tracing::info!(
        "Published streamed show {} to shows archive: {}/{}",
        show_id,
        state.config.r2_shows_bucket_name,
        key
    );
    Ok(key)
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

            // Build filter for this track:
            // - atrim: trim to the actual played duration (handles early stops)
            // - volume: apply volume adjustment (0-200% maps to 0.0-2.0)
            // - adelay: position at the correct offset in the recording
            // Format: [1:a]atrim=0:5.5,asetpts=PTS-STARTPTS,volume=1.5,adelay=5000|5000[a1]
            let duration_seconds = marker.duration_ms as f64 / 1000.0;
            let volume_factor = marker.volume as f64 / 100.0;
            let delay_filter = format!(
                "[{}:a]atrim=0:{:.3},asetpts=PTS-STARTPTS,volume={:.2},adelay={}|{}[a{}]",
                input_index,
                duration_seconds,
                volume_factor,
                marker.offset_ms,
                marker.offset_ms,
                input_index
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn short_recording_flags_only_essentially_empty_captures() {
        // Unknown duration → can't judge → never flagged.
        assert_eq!(detect_short_recording(18, None), None);
        // A normal-length broadcast in a padded slot must NOT be flagged
        // (regression: Küssen #18 — 3.5 h show in a 12 h slot was failed).
        assert_eq!(detect_short_recording(18, Some(12_564_000)), None);
        // A couple of minutes is a real (if short) show — keep it.
        assert_eq!(detect_short_recording(18, Some(120_000)), None);
        // Right at the floor is still acceptable.
        assert_eq!(
            detect_short_recording(18, Some(MIN_RECORDING_SECS * 1000)),
            None
        );
        // Below the absolute floor → essentially empty → flagged.
        assert!(detect_short_recording(18, Some(5_000)).is_some());
        assert!(detect_short_recording(18, Some(0)).is_some());
    }

    #[test]
    fn parses_well_formed_seg_dir_names() {
        assert_eq!(
            parse_seg_dir_name("recording_1_2026-01-28T19-30-00.segs"),
            Some((1, "2026-01-28T19-30-00".to_string()))
        );
        // Multi-digit show id.
        assert_eq!(
            parse_seg_dir_name("recording_4271_2026-06-01T00-00-05.segs"),
            Some((4271, "2026-06-01T00-00-05".to_string()))
        );
    }

    #[test]
    fn rejects_malformed_seg_dir_names() {
        // Missing suffix.
        assert_eq!(parse_seg_dir_name("recording_1_2026-01-28T19-30-00"), None);
        // Missing prefix.
        assert_eq!(parse_seg_dir_name("1_2026-01-28T19-30-00.segs"), None);
        // Non-numeric show id.
        assert_eq!(
            parse_seg_dir_name("recording_abc_2026-01-28T19-30-00.segs"),
            None
        );
        // No version part.
        assert_eq!(parse_seg_dir_name("recording_1.segs"), None);
        // Empty version.
        assert_eq!(parse_seg_dir_name("recording_1_.segs"), None);
        // Unrelated file.
        assert_eq!(parse_seg_dir_name("concat_list.txt"), None);
    }

    #[test]
    fn seg_dir_name_roundtrips_artifact_path() {
        // The sibling artifact recovery writes to must match the normal stop
        // path: `recording_{id}_{version}.webm` next to the `.segs` dir.
        let seg =
            std::path::Path::new("/data/recordings-temp/recording_7_2026-01-28T19-30-00.segs");
        assert_eq!(
            seg.with_extension("webm"),
            std::path::Path::new("/data/recordings-temp/recording_7_2026-01-28T19-30-00.webm")
        );
    }

    /// Generate real MPEG-TS segments (`seg_%05d.ts`) into `seg_dir` from a short
    /// synthetic sine tone, mirroring what the recorder FFmpeg produces.
    #[cfg(test)]
    async fn gen_test_segments(seg_dir: &Path) {
        gen_test_segments_of(seg_dir, 4).await;
    }

    /// [`gen_test_segments`] with `secs` seconds of audio.
    async fn gen_test_segments_of(seg_dir: &Path, secs: u64) {
        let pattern = seg_dir.join("seg_%05d.ts");
        let status = tokio::process::Command::new("ffmpeg")
            .args([
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                &format!("sine=frequency=440:duration={secs}"),
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-f",
                "segment",
                "-segment_time",
                "2",
                "-segment_format",
                "mpegts",
                "-reset_timestamps",
                "1",
            ])
            .arg(&pattern)
            .status()
            .await
            .expect("spawn ffmpeg to generate test segments");
        assert!(status.success(), "ffmpeg failed to generate test segments");
    }

    /// End-to-end orphan recovery against a **real R2 bucket**: seed a `.segs`
    /// dir with real MPEG-TS segments, run [`recover_orphaned_recordings`], and
    /// assert the raw object + `recording_versions` row land and the seg dir is
    /// removed. Then prove idempotency: re-seeding the same dir and re-running
    /// must skip (already persisted) — no duplicate row, no re-upload.
    ///
    /// Ignored by default (hits the network + needs ffmpeg). Run with R2 creds:
    ///   cargo test --bin unheard-backend -- --ignored recover_orphaned_recordings_roundtrip
    /// Skips gracefully if the R2 environment is not configured.
    #[tokio::test]
    #[ignore = "requires real R2 credentials + ffmpeg"]
    async fn recover_orphaned_recordings_roundtrip() {
        dotenvy::dotenv().ok();
        let config = match crate::Config::from_env() {
            Ok(c) => c,
            Err(e) => {
                eprintln!("Skipping recovery integration test — config not available: {e}");
                return;
            }
        };
        let s3_client = storage::build_s3_client(&config);
        let bucket = config.r2_bucket_name.clone();

        // In-memory DB with a show row (recording_versions has an FK → shows).
        let db = sqlx::SqlitePool::connect("sqlite::memory:").await.unwrap();
        crate::db::run_migrations(&db).await.unwrap();
        let show_id: i64 = sqlx::query_scalar(
            "INSERT INTO shows (title, date) VALUES ('Recovery IT', '2026-06-01') RETURNING id",
        )
        .fetch_one(&db)
        .await
        .unwrap();

        // Temp recording dir with a per-session seg dir of REAL segments.
        let temp = tempfile::TempDir::new().unwrap();
        let temp_dir = temp.path().to_path_buf();
        let version = "2026-06-01T20-00-00";
        let seg_dir = temp_dir.join(format!("recording_{}_{}.segs", show_id, version));
        tokio::fs::create_dir_all(&seg_dir).await.unwrap();
        gen_test_segments(&seg_dir).await;

        let state = Arc::new(AppState {
            recovery_mail: crate::recovery_mail::RecoveryMailer::default(),
            recovery_controls: crate::handlers::recovery::RecoveryControls::default(),
            db: db.clone(),
            config: config.clone(),
            s3_client: s3_client.clone(),
            stream_state: crate::stream_bridge::new_shared_state(),
            stream_metrics: crate::stream_metrics::new_shared(),
            recording_manager: crate::recording::new_shared_manager(temp_dir.clone()),
            recording_finalizer: Arc::default(),
            cover_debounce: Arc::new(tokio::sync::RwLock::new(HashMap::new())),
            default_cover: tokio::sync::OnceCell::new(),
            telegram_bot: None,
            pending_show_notifications: Arc::new(tokio::sync::Mutex::new(HashMap::new())),
            telegram_edit_sessions: Arc::new(tokio::sync::Mutex::new(HashMap::new())),
            chat_hub: Arc::new(crate::chat_bridge::ChatHub::new()),
        });

        let raw_key = format!("recordings/{}/{}/raw.webm", show_id, version);
        let markers_key = format!("recordings/{}/{}/markers.json", show_id, version);

        // First run: recovers the orphaned segments.
        recover_orphaned_recordings(&state, &temp_dir).await;

        let size = storage::object_size(&s3_client, &bucket, &raw_key)
            .await
            .expect("head the raw object")
            .expect("raw object should exist in R2 after recovery");
        assert!(size > 0, "recovered object is empty");
        assert!(
            crate::db::get_recording_version(&db, show_id, version)
                .await
                .unwrap()
                .is_some(),
            "a recording_versions row should exist after recovery"
        );
        assert!(
            !seg_dir.exists(),
            "seg dir should be removed after successful recovery"
        );

        // Idempotency: re-seed the same seg dir (simulating a crash between upload
        // and seg-dir cleanup) and re-run. Must skip — exactly one row, no
        // re-upload — and still clean up the stale dir.
        tokio::fs::create_dir_all(&seg_dir).await.unwrap();
        gen_test_segments(&seg_dir).await;
        recover_orphaned_recordings(&state, &temp_dir).await;

        let count: i64 =
            sqlx::query_scalar("SELECT COUNT(*) FROM recording_versions WHERE show_id = ?")
                .bind(show_id)
                .fetch_one(&db)
                .await
                .unwrap();
        assert_eq!(count, 1, "recovery must not create a duplicate version");
        assert!(
            !seg_dir.exists(),
            "an already-persisted seg dir should be cleaned up on re-run"
        );

        // Cleanup R2.
        let _ = s3_client
            .delete_object()
            .bucket(&bucket)
            .key(&raw_key)
            .send()
            .await;
        let _ = s3_client
            .delete_object()
            .bucket(&bucket)
            .key(&markers_key)
            .send()
            .await;
    }

    /// An in-memory [`RecordingStore`] that records every write.
    #[derive(Default)]
    struct FakeStore {
        objects: std::sync::Mutex<HashMap<String, Vec<u8>>>,
        raw_uploads: std::sync::Mutex<Vec<String>>,
        json_puts: std::sync::Mutex<Vec<String>>,
        publishes: std::sync::Mutex<Vec<i64>>,
        rejected_put: std::sync::Mutex<Option<String>>,
    }

    impl FakeStore {
        fn object(&self, key: &str) -> Option<Vec<u8>> {
            self.objects.lock().unwrap().get(key).cloned()
        }

        fn insert(&self, key: &str, data: &[u8]) {
            self.objects
                .lock()
                .unwrap()
                .insert(key.to_string(), data.to_vec());
        }

        /// Make every JSON put to `key` fail.
        fn reject_puts_to(&self, key: &str) {
            *self.rejected_put.lock().unwrap() = Some(key.to_string());
        }

        /// Raw uploads, JSON puts and archive publishes so far.
        fn writes(&self) -> (usize, usize, usize) {
            (
                self.raw_uploads.lock().unwrap().len(),
                self.json_puts.lock().unwrap().len(),
                self.publishes.lock().unwrap().len(),
            )
        }
    }

    impl RecordingStore for FakeStore {
        fn size<'a>(&'a self, key: &'a str) -> BoxFuture<'a, Result<Option<u64>>> {
            let size = self.object(key).map(|data| data.len() as u64);
            Box::pin(async move { Ok(size) })
        }

        fn upload_raw<'a>(&'a self, key: &'a str, data: Vec<u8>) -> BoxFuture<'a, Result<()>> {
            self.raw_uploads.lock().unwrap().push(key.to_string());
            self.insert(key, &data);
            Box::pin(async { Ok(()) })
        }

        fn put_json<'a>(&'a self, key: &'a str, json: String) -> BoxFuture<'a, Result<()>> {
            if self.rejected_put.lock().unwrap().as_deref() == Some(key) {
                return Box::pin(async { Err(AppError::Storage("rejected".into())) });
            }
            self.json_puts.lock().unwrap().push(key.to_string());
            self.insert(key, json.as_bytes());
            Box::pin(async { Ok(()) })
        }

        fn read<'a>(&'a self, key: &'a str) -> BoxFuture<'a, Result<Vec<u8>>> {
            let data = self.object(key);
            Box::pin(async move { data.ok_or_else(|| AppError::Storage("missing".into())) })
        }

        fn publish<'a>(&'a self, show_id: i64, _: &'a Path) -> BoxFuture<'a, Result<String>> {
            self.publishes.lock().unwrap().push(show_id);
            Box::pin(async { Ok("shows/fake.mp3".to_string()) })
        }
    }

    const VERSION: &str = "2026-06-01T20-00-00";

    /// An app state with in-memory SQLite and no reachable R2; the temp dir is
    /// both the recording manager's dir and the recovery scan dir.
    async fn offline_state() -> (Arc<AppState>, tempfile::TempDir) {
        let db = sqlx::SqlitePool::connect("sqlite::memory:").await.unwrap();
        crate::db::run_migrations(&db).await.unwrap();
        let mut config: crate::Config = serde_json::from_value(serde_json::json!({
            "secret_key": "test-only", "superadmin_password_hash": "unused",
            "r2_account_id": "unused", "r2_access_key_id": "unused",
            "r2_secret_access_key": "unused", "rtmp_url": "rtmp://127.0.0.1:1/live"
        }))
        .unwrap();
        config.r2_endpoint = "http://127.0.0.1:1".to_string();
        let temp = tempfile::tempdir().unwrap();
        let state = Arc::new(AppState {
            recovery_mail: crate::recovery_mail::RecoveryMailer::default(),
            recovery_controls: crate::handlers::recovery::RecoveryControls::default(),
            db,
            s3_client: storage::build_s3_client(&config),
            config,
            stream_state: crate::stream_bridge::new_shared_state(),
            stream_metrics: crate::stream_metrics::new_shared(),
            recording_manager: crate::recording::new_shared_manager(temp.path().to_path_buf()),
            recording_finalizer: Arc::default(),
            cover_debounce: Arc::new(tokio::sync::RwLock::new(HashMap::new())),
            default_cover: tokio::sync::OnceCell::new(),
            telegram_bot: None,
            pending_show_notifications: Arc::new(tokio::sync::Mutex::new(HashMap::new())),
            telegram_edit_sessions: Arc::new(tokio::sync::Mutex::new(HashMap::new())),
            chat_hub: Arc::new(crate::chat_bridge::ChatHub::new()),
        });
        (state, temp)
    }

    async fn add_show(state: &Arc<AppState>, show_id: i64) {
        sqlx::query("INSERT INTO shows (id, title, date) VALUES (?, 'Recovery', '2026-06-01')")
            .bind(show_id)
            .execute(&state.db)
            .await
            .unwrap();
    }

    async fn row_count(state: &Arc<AppState>, show_id: i64) -> i64 {
        sqlx::query_scalar("SELECT COUNT(*) FROM recording_versions WHERE show_id = ?")
            .bind(show_id)
            .fetch_one(&state.db)
            .await
            .unwrap()
    }

    fn raw_key(show_id: i64) -> String {
        format!("recordings/{show_id}/{VERSION}/raw.webm")
    }

    fn markers_key(show_id: i64) -> String {
        format!("recordings/{show_id}/{VERSION}/markers.json")
    }

    /// Write the joined file a finalize leaves before its upload.
    async fn leftover_webm(temp: &Path, show_id: i64, data: &[u8]) -> PathBuf {
        let path = temp.join(format!("recording_{show_id}_{VERSION}.webm"));
        tokio::fs::write(&path, data).await.unwrap();
        path
    }

    /// Real segments for `show_id` (needs ffmpeg, as the recorder does) and the
    /// bytes of their complete join.
    async fn segments_and_join(temp: &Path, show_id: i64) -> (PathBuf, Vec<u8>) {
        let seg_dir = temp.join(format!("recording_{show_id}_{VERSION}.segs"));
        tokio::fs::create_dir_all(&seg_dir).await.unwrap();
        gen_test_segments(&seg_dir).await;
        let joined = temp.join("earlier-join.webm.tmp");
        crate::stream_bridge::concat_segments(&seg_dir, &joined)
            .await
            .unwrap();
        let data = tokio::fs::read(&joined).await.unwrap();
        tokio::fs::remove_file(&joined).await.unwrap();
        (seg_dir, data)
    }

    #[tokio::test]
    async fn failed_row_insert_keeps_the_file_and_recovery_writes_the_row() {
        let (state, temp) = offline_state().await;
        let store = FakeStore::default();
        // Show 41 has no `shows` row yet, so the foreign key refuses the insert.
        let artifact = leftover_webm(temp.path(), 41, b"joined audio").await;
        let markers = Markers::Session {
            json: r#"[{"track":1},{"track":2}]"#.to_string(),
            count: 2,
        };

        let result =
            upload_artifact_and_record(&state, &store, 41, VERSION, &artifact, markers, None).await;

        assert!(result.is_err(), "a failed insert must fail the persist");
        assert!(artifact.exists(), "the file must stay for a retry");
        assert_eq!(store.object(&raw_key(41)).unwrap(), b"joined audio");
        assert_eq!(
            store.writes(),
            (1, 1, 0),
            "no archive publish without a row"
        );

        add_show(&state, 41).await;
        let counts = recover_leftovers(&state, &store, temp.path()).await;

        assert_eq!(counts.recovered, 1);
        let row = crate::db::get_recording_version(&state.db, 41, VERSION)
            .await
            .unwrap()
            .expect("recovery writes the row");
        assert_eq!(row.marker_count, 2, "the stored session markers are kept");
        assert_eq!(
            store.object(&markers_key(41)).unwrap(),
            br#"[{"track":1},{"track":2}]"#
        );
        // No finalize note: the recording may be incomplete, so it is marked
        // failed and not published.
        assert_eq!(row.status, "failed");
        assert_eq!(
            store.writes(),
            (1, 1, 0),
            "no second upload, no markers overwrite, no publish"
        );
        assert!(!artifact.exists(), "the file goes once the row exists");
    }

    #[tokio::test]
    async fn recovery_uploads_a_leftover_webm_unless_r2_has_the_same_size() {
        for (case, stored, uploads) in [
            ("no object", None, 1),
            ("same size", Some(&b"joined audio"[..]), 0),
            ("other size", Some(&b"short"[..]), 1),
        ] {
            let (state, temp) = offline_state().await;
            let store = FakeStore::default();
            add_show(&state, 7).await;
            if let Some(data) = stored {
                store.insert(&raw_key(7), data);
            }
            let artifact = leftover_webm(temp.path(), 7, b"joined audio").await;

            let counts = recover_leftovers(&state, &store, temp.path()).await;

            assert_eq!(counts.recovered, 1, "{case}");
            assert_eq!(store.writes(), (uploads, 1, 0), "{case}");
            assert_eq!(
                store.object(&raw_key(7)).unwrap(),
                b"joined audio",
                "{case}"
            );
            assert_eq!(store.object(&markers_key(7)).unwrap(), b"[]", "{case}");
            assert_eq!(row_count(&state, 7).await, 1, "{case}");
            assert!(!artifact.exists(), "{case}");
        }
    }

    #[tokio::test]
    async fn a_second_recovery_run_changes_nothing() {
        let (state, temp) = offline_state().await;
        let store = FakeStore::default();
        add_show(&state, 9).await;
        leftover_webm(temp.path(), 9, b"joined audio").await;
        assert_eq!(
            recover_leftovers(&state, &store, temp.path())
                .await
                .recovered,
            1
        );
        let writes = store.writes();

        // A crash after the insert, before the delete, leaves the file again.
        let artifact = leftover_webm(temp.path(), 9, b"joined audio").await;
        let counts = recover_leftovers(&state, &store, temp.path()).await;

        assert_eq!(
            counts,
            RecoveryCounts {
                recovered: 0,
                skipped: 1,
                failed: 0
            }
        );
        assert_eq!(store.writes(), writes, "no upload, put or publish");
        assert_eq!(row_count(&state, 9).await, 1, "no duplicate row");
        assert!(
            !artifact.exists(),
            "R2 holds the same size, so the copy goes"
        );
    }

    #[tokio::test]
    async fn a_row_keeps_a_local_file_that_differs_from_r2() {
        let (state, temp) = offline_state().await;
        let store = FakeStore::default();
        add_show(&state, 5).await;
        crate::db::create_recording_version(&state.db, 5, VERSION, &raw_key(5), &markers_key(5), 0)
            .await
            .unwrap();
        store.insert(&raw_key(5), b"short");
        let artifact = leftover_webm(temp.path(), 5, b"the full recording").await;

        let counts = recover_leftovers(&state, &store, temp.path()).await;

        assert_eq!(counts.skipped, 1);
        assert!(artifact.exists(), "the local copy is the good one");
        assert_eq!(store.writes(), (0, 0, 0));
    }

    #[tokio::test]
    async fn an_empty_webm_without_segments_is_never_uploaded() {
        for empty_seg_dir in [false, true] {
            let (state, temp) = offline_state().await;
            let store = FakeStore::default();
            add_show(&state, 3).await;
            let artifact = leftover_webm(temp.path(), 3, b"").await;
            let seg_dir = temp.path().join(format!("recording_3_{VERSION}.segs"));
            if empty_seg_dir {
                tokio::fs::create_dir_all(&seg_dir).await.unwrap();
            }

            let counts = recover_leftovers(&state, &store, temp.path()).await;

            assert_eq!(counts.failed, 1, "{empty_seg_dir}");
            assert!(artifact.exists(), "{empty_seg_dir}");
            assert_eq!(seg_dir.exists(), empty_seg_dir);
            assert_eq!(store.writes(), (0, 0, 0), "never create a 0-byte object");
            assert_eq!(row_count(&state, 3).await, 0, "{empty_seg_dir}");
        }
    }

    #[tokio::test]
    async fn a_complete_file_is_uploaded_next_to_an_empty_segment_dir() {
        let (state, temp) = offline_state().await;
        let store = FakeStore::default();
        add_show(&state, 19).await;
        let seg_dir = temp.path().join(format!("recording_19_{VERSION}.segs"));
        tokio::fs::create_dir_all(&seg_dir).await.unwrap();
        let artifact = leftover_webm(temp.path(), 19, b"joined audio").await;

        let counts = recover_leftovers(&state, &store, temp.path()).await;

        assert_eq!(counts.recovered, 1);
        assert_eq!(store.object(&raw_key(19)).unwrap(), b"joined audio");
        assert_eq!(store.writes(), (1, 1, 0));
        assert!(!seg_dir.exists() && !artifact.exists());
    }

    #[tokio::test]
    async fn the_live_session_is_never_recovered() {
        let (state, temp) = offline_state().await;
        let store = FakeStore::default();
        add_show(&state, 11).await;
        let status = state
            .recording_manager
            .lock()
            .await
            .start(11)
            .await
            .unwrap();
        let version = status.version.unwrap();
        let artifact = temp.path().join(format!("recording_11_{version}.webm"));
        let seg_dir = temp.path().join(format!("recording_11_{version}.segs"));
        tokio::fs::create_dir_all(&seg_dir).await.unwrap();
        tokio::fs::write(&artifact, b"being written").await.unwrap();

        let counts = recover_leftovers(&state, &store, temp.path()).await;

        assert_eq!(counts.skipped, 1);
        assert!(artifact.exists() && seg_dir.exists());
        assert_eq!(store.writes(), (0, 0, 0));
        assert_eq!(row_count(&state, 11).await, 0);
    }

    /// The ticket's crash between upload and insert, on the segment path: the
    /// segments survived next to the empty file of the session start, and R2
    /// already has the joined upload. Needs ffmpeg, as the recorder does.
    #[tokio::test]
    async fn segments_are_joined_again_and_reuse_the_uploaded_object() {
        let (state, temp) = offline_state().await;
        let store = FakeStore::default();
        add_show(&state, 13).await;
        let (seg_dir, joined) = segments_and_join(temp.path(), 13).await;
        store.insert(&raw_key(13), &joined);
        let artifact = leftover_webm(temp.path(), 13, b"").await;

        let counts = recover_leftovers(&state, &store, temp.path()).await;

        assert_eq!(counts.recovered, 1);
        // No second upload. The 4 s test audio counts as short, so the row is
        // marked failed and nothing goes to the shows archive.
        assert_eq!(
            store.writes(),
            (0, 1, 0),
            "the same join is not uploaded again"
        );
        let row = crate::db::get_recording_version(&state.db, 13, VERSION)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(row.status, "failed");
        assert!(!seg_dir.exists() && !artifact.exists());
    }

    /// A crash while the stop path removed the segment dir, after the upload:
    /// the complete join is next to some of its segments, and R2 has it.
    #[tokio::test]
    async fn a_complete_file_beats_a_partial_segment_dir() {
        let (state, temp) = offline_state().await;
        let store = FakeStore::default();
        add_show(&state, 15).await;
        let (seg_dir, joined) = segments_and_join(temp.path(), 15).await;
        store.insert(&raw_key(15), &joined);
        let artifact = leftover_webm(temp.path(), 15, &joined).await;
        tokio::fs::remove_file(seg_dir.join("seg_00000.ts"))
            .await
            .unwrap();

        let counts = recover_leftovers(&state, &store, temp.path()).await;

        assert_eq!(counts.recovered, 1);
        assert_eq!(store.object(&raw_key(15)).unwrap(), joined);
        assert_eq!(
            store.writes(),
            (0, 1, 0),
            "the shorter join is not uploaded"
        );
        assert!(!seg_dir.exists() && !artifact.exists());
    }

    /// A crash during the stop path's join: a cut-off file next to all segments.
    #[tokio::test]
    async fn the_segments_beat_a_cut_off_file() {
        let (state, temp) = offline_state().await;
        let store = FakeStore::default();
        add_show(&state, 17).await;
        let (seg_dir, joined) = segments_and_join(temp.path(), 17).await;
        let artifact = leftover_webm(temp.path(), 17, &joined[..joined.len() / 2]).await;

        let counts = recover_leftovers(&state, &store, temp.path()).await;

        assert_eq!(counts.recovered, 1);
        assert_eq!(store.object(&raw_key(17)).unwrap().len(), joined.len());
        assert_eq!(store.writes(), (1, 1, 0));
        assert!(!seg_dir.exists() && !artifact.exists());
    }

    /// A crash during the join, and the new join fails too: the joined file
    /// may be cut off, so nothing is uploaded and both copies stay.
    #[tokio::test]
    async fn a_failed_join_keeps_the_segments_and_the_cut_off_file() {
        let (state, temp) = offline_state().await;
        let store = FakeStore::default();
        add_show(&state, 21).await;
        let (seg_dir, joined) = segments_and_join(temp.path(), 21).await;
        let cut_off = &joined[..joined.len() / 2];
        let artifact = leftover_webm(temp.path(), 21, cut_off).await;
        // A dir at the candidate path makes FFmpeg fail to write the join.
        tokio::fs::create_dir(seg_dir.join(REJOINED_FILE))
            .await
            .unwrap();
        let segments = |dir: &Path| {
            let mut names: Vec<_> = std::fs::read_dir(dir)
                .unwrap()
                .map(|e| e.unwrap().path())
                .filter(|p| crate::stream_bridge::is_segment_file(p))
                .collect();
            names.sort();
            names
        };
        let before = segments(&seg_dir);
        assert!(!before.is_empty());

        let counts = recover_leftovers(&state, &store, temp.path()).await;

        assert_eq!(
            counts,
            RecoveryCounts {
                recovered: 0,
                skipped: 0,
                failed: 1
            }
        );
        assert_eq!(store.writes(), (0, 0, 0));
        assert_eq!(row_count(&state, 21).await, 0);
        assert_eq!(tokio::fs::read(&artifact).await.unwrap(), cut_off);
        assert_eq!(segments(&seg_dir), before, "every segment stays");
    }

    /// The stop path's join failed: the finalize keeps the files and stores
    /// only the markers. The next boot recovers the show with its markers.
    #[tokio::test]
    async fn a_failed_join_is_recovered_at_the_next_boot_with_its_markers() {
        let (state, temp) = offline_state().await;
        let store = FakeStore::default();
        add_show(&state, 23).await;
        let session = {
            let mut manager = state.recording_manager.lock().await;
            manager.start(23).await.unwrap();
            manager
                .add_marker(1, "track".to_string(), "key".to_string(), 1000, 100)
                .unwrap();
            manager.stop().await.unwrap().unwrap()
        };
        let artifact = session.temp_file_path.clone();
        let version = session.version_timestamp.clone();
        let seg_dir = artifact.with_extension("segs");
        tokio::fs::create_dir_all(&seg_dir).await.unwrap();
        gen_test_segments(&seg_dir).await;
        let detached = DetachedSession {
            session,
            write_failure: None,
            join_failure: Some("segment join failed".to_string()),
        };

        let result = persist_session(&state, &store, detached).await;

        assert!(result.is_err(), "a failed join fails the finalize");
        assert_eq!(store.writes(), (0, 2, 0), "only the markers and the note");
        assert_eq!(row_count(&state, 23).await, 0);
        assert!(artifact.exists() && seg_dir.exists());

        let counts = recover_leftovers(&state, &store, temp.path()).await;

        assert_eq!(counts.recovered, 1);
        assert_eq!(store.writes(), (1, 2, 0), "one raw upload, markers kept");
        let row = crate::db::get_recording_version(&state.db, 23, &version)
            .await
            .unwrap()
            .expect("recovery writes the row");
        assert_eq!(row.marker_count, 1);
        assert!(!seg_dir.exists() && !artifact.exists());
    }

    /// A failed join with audio long enough to pass the duration check. The
    /// recovered recording is published only when the finalize note says no
    /// audio was lost. A lost note counts as a possible gap.
    #[tokio::test]
    async fn recovery_publishes_a_failed_join_only_with_a_clean_finalize_note() {
        for (write_failure, note_saved, status, reason, publishes) in [
            (None, true, "raw", None, 1),
            (
                Some("chunks dropped"),
                true,
                "failed",
                Some("chunks dropped"),
                0,
            ),
            (
                Some("chunks dropped"),
                false,
                "failed",
                Some("may be incomplete"),
                0,
            ),
            (None, false, "failed", Some("may be incomplete"), 0),
        ] {
            let case = format!("{write_failure:?}, note saved: {note_saved}");
            let (state, temp) = offline_state().await;
            let store = FakeStore::default();
            add_show(&state, 25).await;
            let session = {
                let mut manager = state.recording_manager.lock().await;
                manager.start(25).await.unwrap();
                manager.stop().await.unwrap().unwrap()
            };
            let version = session.version_timestamp.clone();
            let seg_dir = session.temp_file_path.with_extension("segs");
            tokio::fs::create_dir_all(&seg_dir).await.unwrap();
            gen_test_segments_of(&seg_dir, MIN_RECORDING_SECS + 5).await;
            let note_key = finalize_note_key(25, &version);
            if !note_saved {
                store.reject_puts_to(&note_key);
            }
            let detached = DetachedSession {
                session,
                write_failure: write_failure.map(str::to_string),
                join_failure: Some("segment join failed".to_string()),
            };
            assert!(persist_session(&state, &store, detached).await.is_err());
            assert_eq!(store.object(&note_key).is_some(), note_saved, "{case}");

            let counts = recover_leftovers(&state, &store, temp.path()).await;

            assert_eq!(counts.recovered, 1, "{case}");
            let row = crate::db::get_recording_version(&state.db, 25, &version)
                .await
                .unwrap()
                .expect("recovery writes the row");
            assert!(row.duration_ms.unwrap() > MIN_RECORDING_SECS as i64 * 1000);
            assert_eq!(row.status, status, "{case}");
            match reason {
                Some(reason) => assert!(
                    row.error_message.as_deref().unwrap().contains(reason),
                    "{case}: {:?}",
                    row.error_message
                ),
                None => assert_eq!(row.error_message, None, "{case}"),
            }
            assert_eq!(store.writes().2, publishes, "{case}");
        }
    }
}
