//! Recording session management for show recordings with timecoded track markers.
//!
//! This module manages recording sessions that capture the live stream while
//! logging timecode markers when pre-recorded tracks are played. After the show,
//! the raw recording and markers can be used to merge high-quality original
//! tracks at their exact playback times.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::fs::File;
use tokio::io::AsyncWriteExt;
use tokio::sync::Mutex;

/// A marker indicating when a pre-recorded track was played during the recording.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TrackMarker {
    /// Offset from recording start in milliseconds
    pub offset_ms: u64,
    /// Duration of the track in milliseconds
    pub duration_ms: u64,
    /// ID of the artist whose track was played
    pub artist_id: i64,
    /// Type of track: "track1", "track2", or "voice_message"
    pub track_type: String,
    /// S3 key of the original (high-quality) track file
    pub track_key: String,
}

/// An active recording session for a show.
#[derive(Debug)]
pub struct RecordingSession {
    /// ID of the show being recorded
    pub show_id: i64,
    /// When the recording started (for calculating offsets)
    started_at: Instant,
    /// ISO 8601 timestamp for versioning (e.g., "2026-01-28T19-30-00")
    pub version_timestamp: String,
    /// Track markers collected during the session
    pub markers: Vec<TrackMarker>,
    /// Path to the temporary file where raw WebM is being written
    pub temp_file_path: PathBuf,
    /// File handle for writing chunks (None if not yet opened or closed)
    file_handle: Option<File>,
}

impl RecordingSession {
    /// Create a new recording session for the given show.
    ///
    /// # Arguments
    /// * `show_id` - The ID of the show being recorded
    /// * `temp_dir` - Directory for temporary recording files
    pub async fn new(show_id: i64, temp_dir: &PathBuf) -> Result<Self, RecordingError> {
        let now = chrono::Utc::now();
        let version_timestamp = now.format("%Y-%m-%dT%H-%M-%S").to_string();
        let temp_file_path =
            temp_dir.join(format!("recording_{}_{}.webm", show_id, version_timestamp));

        // Ensure temp directory exists
        if let Some(parent) = temp_file_path.parent() {
            tokio::fs::create_dir_all(parent).await.map_err(|e| {
                RecordingError::FileSystem(format!("Failed to create temp dir: {}", e))
            })?;
        }

        // Create the recording file
        let file_handle = File::create(&temp_file_path).await.map_err(|e| {
            RecordingError::FileSystem(format!("Failed to create recording file: {}", e))
        })?;

        tracing::info!(
            "Started recording session for show {} at {}, temp file: {:?}",
            show_id,
            version_timestamp,
            temp_file_path
        );

        Ok(Self {
            show_id,
            started_at: Instant::now(),
            version_timestamp,
            markers: Vec::new(),
            temp_file_path,
            file_handle: Some(file_handle),
        })
    }

    /// Get the elapsed time since recording started.
    pub fn elapsed(&self) -> Duration {
        self.started_at.elapsed()
    }

    /// Get the elapsed time in milliseconds.
    pub fn elapsed_ms(&self) -> u64 {
        self.started_at.elapsed().as_millis() as u64
    }

    /// Add a track marker at the current recording position.
    ///
    /// # Arguments
    /// * `artist_id` - ID of the artist whose track was played
    /// * `track_type` - Type of track ("track1", "track2", or "voice_message")
    /// * `track_key` - S3 key of the original track file
    /// * `duration_ms` - Duration of the track in milliseconds
    pub fn add_marker(
        &mut self,
        artist_id: i64,
        track_type: String,
        track_key: String,
        duration_ms: u64,
    ) {
        let marker = TrackMarker {
            offset_ms: self.elapsed_ms(),
            duration_ms,
            artist_id,
            track_type: track_type.clone(),
            track_key: track_key.clone(),
        };

        tracing::info!(
            "Added marker for show {}: artist={}, type={}, offset={}ms, duration={}ms",
            self.show_id,
            artist_id,
            track_type,
            marker.offset_ms,
            duration_ms
        );

        self.markers.push(marker);
    }

    /// Write a chunk of audio data to the recording file.
    pub async fn write_chunk(&mut self, data: &[u8]) -> Result<(), RecordingError> {
        if let Some(ref mut file) = self.file_handle {
            file.write_all(data)
                .await
                .map_err(|e| RecordingError::WriteFailed(e.to_string()))?;
            Ok(())
        } else {
            Err(RecordingError::NotRecording)
        }
    }

    /// Flush and close the recording file.
    pub async fn close(&mut self) -> Result<(), RecordingError> {
        if let Some(mut file) = self.file_handle.take() {
            file.flush()
                .await
                .map_err(|e| RecordingError::WriteFailed(format!("Failed to flush: {}", e)))?;
            file.shutdown()
                .await
                .map_err(|e| RecordingError::WriteFailed(format!("Failed to shutdown: {}", e)))?;
            tracing::info!("Closed recording file: {:?}", self.temp_file_path);
        }
        Ok(())
    }

    /// Export markers as JSON.
    pub fn markers_json(&self) -> Result<String, RecordingError> {
        serde_json::to_string_pretty(&self.markers)
            .map_err(|e| RecordingError::Serialization(e.to_string()))
    }
}

/// Manager for recording sessions.
///
/// Only one recording session can be active at a time.
/// Wrapped in Arc<Mutex<>> for shared access across handlers.
pub struct RecordingManager {
    /// The currently active recording session, if any
    session: Option<RecordingSession>,
    /// Directory for temporary recording files
    temp_dir: PathBuf,
}

impl RecordingManager {
    /// Create a new recording manager.
    ///
    /// # Arguments
    /// * `temp_dir` - Directory for temporary recording files
    pub fn new(temp_dir: PathBuf) -> Self {
        Self {
            session: None,
            temp_dir,
        }
    }

    /// Check if a recording is currently active.
    pub fn is_recording(&self) -> bool {
        self.session.is_some()
    }

    /// Get the current session's show ID, if recording.
    pub fn current_show_id(&self) -> Option<i64> {
        self.session.as_ref().map(|s| s.show_id)
    }

    /// Get the current session's version timestamp, if recording.
    pub fn current_version(&self) -> Option<String> {
        self.session.as_ref().map(|s| s.version_timestamp.clone())
    }

    /// Get the temp file path for the current session, if recording.
    /// Used by the stream handler to start file tee-ing.
    pub fn get_temp_file_path(&self) -> Option<PathBuf> {
        self.session.as_ref().map(|s| s.temp_file_path.clone())
    }

    /// Get recording status information.
    pub fn get_status(&self) -> RecordingStatus {
        match &self.session {
            Some(session) => RecordingStatus {
                active: true,
                show_id: Some(session.show_id),
                version: Some(session.version_timestamp.clone()),
                elapsed_ms: Some(session.elapsed_ms()),
                marker_count: Some(session.markers.len()),
            },
            None => RecordingStatus {
                active: false,
                show_id: None,
                version: None,
                elapsed_ms: None,
                marker_count: None,
            },
        }
    }

    /// Start a new recording session for the given show.
    ///
    /// If a session is already active, it will be stopped first.
    pub async fn start(&mut self, show_id: i64) -> Result<RecordingStatus, RecordingError> {
        // Stop any existing session
        if self.session.is_some() {
            tracing::warn!("Stopping existing recording session before starting new one");
            self.stop().await?;
        }

        let session = RecordingSession::new(show_id, &self.temp_dir).await?;
        let status = RecordingStatus {
            active: true,
            show_id: Some(session.show_id),
            version: Some(session.version_timestamp.clone()),
            elapsed_ms: Some(0),
            marker_count: Some(0),
        };
        self.session = Some(session);

        Ok(status)
    }

    /// Stop the current recording session.
    ///
    /// Returns the completed session for further processing (upload, etc.)
    pub async fn stop(&mut self) -> Result<Option<RecordingSession>, RecordingError> {
        if let Some(mut session) = self.session.take() {
            session.close().await?;
            tracing::info!(
                "Stopped recording session for show {}: {} markers, {:?}",
                session.show_id,
                session.markers.len(),
                session.temp_file_path
            );
            Ok(Some(session))
        } else {
            Ok(None)
        }
    }

    /// Add a track marker to the current session.
    ///
    /// Returns an error if no recording is active.
    pub fn add_marker(
        &mut self,
        artist_id: i64,
        track_type: String,
        track_key: String,
        duration_ms: u64,
    ) -> Result<TrackMarker, RecordingError> {
        if let Some(ref mut session) = self.session {
            let offset_ms = session.elapsed_ms();
            session.add_marker(
                artist_id,
                track_type.clone(),
                track_key.clone(),
                duration_ms,
            );

            // Return the marker that was just added
            Ok(TrackMarker {
                offset_ms,
                duration_ms,
                artist_id,
                track_type,
                track_key,
            })
        } else {
            Err(RecordingError::NotRecording)
        }
    }

    /// Write audio chunk to the current recording.
    ///
    /// This is called from the stream handler to tee audio data.
    /// Returns Ok if no recording is active (silent no-op for easier integration).
    pub async fn write_chunk(&mut self, data: &[u8]) -> Result<(), RecordingError> {
        if let Some(ref mut session) = self.session {
            session.write_chunk(data).await?;
        }
        // If not recording, silently ignore (not an error)
        Ok(())
    }
}

/// Shared recording manager wrapped in Arc<Mutex<>> for concurrent access.
pub type SharedRecordingManager = Arc<Mutex<RecordingManager>>;

/// Create a new shared recording manager.
pub fn new_shared_manager(temp_dir: PathBuf) -> SharedRecordingManager {
    Arc::new(Mutex::new(RecordingManager::new(temp_dir)))
}

/// Status information for API responses.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RecordingStatus {
    pub active: bool,
    pub show_id: Option<i64>,
    pub version: Option<String>,
    pub elapsed_ms: Option<u64>,
    pub marker_count: Option<usize>,
}

/// Errors that can occur during recording.
#[derive(Debug, thiserror::Error)]
pub enum RecordingError {
    #[error("File system error: {0}")]
    FileSystem(String),

    #[error("Not currently recording")]
    NotRecording,

    #[error("Failed to write recording data: {0}")]
    WriteFailed(String),

    #[error("Serialization error: {0}")]
    Serialization(String),

    #[error("Recording already active for show {0}")]
    AlreadyRecording(i64),
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[tokio::test]
    async fn test_recording_session_lifecycle() {
        let temp_dir = TempDir::new().unwrap();
        let mut manager = RecordingManager::new(temp_dir.path().to_path_buf());

        // Initially not recording
        assert!(!manager.is_recording());
        assert!(manager.current_show_id().is_none());

        // Start recording
        let status = manager.start(42).await.unwrap();
        assert!(status.active);
        assert_eq!(status.show_id, Some(42));
        assert!(manager.is_recording());

        // Add a marker
        let marker = manager
            .add_marker(1, "track1".into(), "artists/1/track1.mp3".into(), 180000)
            .unwrap();
        assert_eq!(marker.artist_id, 1);
        assert_eq!(marker.track_type, "track1");
        assert_eq!(marker.duration_ms, 180000);
        assert!(marker.offset_ms >= 0); // Should have some offset

        // Write some data
        manager.write_chunk(b"test audio data").await.unwrap();

        // Stop recording
        let session = manager.stop().await.unwrap();
        assert!(session.is_some());
        let session = session.unwrap();
        assert_eq!(session.show_id, 42);
        assert_eq!(session.markers.len(), 1);

        // No longer recording
        assert!(!manager.is_recording());

        // Adding marker when not recording should error
        let result = manager.add_marker(1, "track1".into(), "key".into(), 1000);
        assert!(matches!(result, Err(RecordingError::NotRecording)));
    }

    #[tokio::test]
    async fn test_markers_json_export() {
        let temp_dir = TempDir::new().unwrap();
        let mut manager = RecordingManager::new(temp_dir.path().to_path_buf());

        manager.start(1).await.unwrap();
        manager
            .add_marker(10, "track1".into(), "artists/10/track1.mp3".into(), 200000)
            .unwrap();
        manager
            .add_marker(10, "track2".into(), "artists/10/track2.mp3".into(), 180000)
            .unwrap();
        manager
            .add_marker(
                11,
                "voice_message".into(),
                "artists/11/voice.mp3".into(),
                30000,
            )
            .unwrap();

        let session = manager.stop().await.unwrap().unwrap();
        let json = session.markers_json().unwrap();

        // Verify JSON structure
        let parsed: Vec<TrackMarker> = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.len(), 3);
        assert_eq!(parsed[0].artist_id, 10);
        assert_eq!(parsed[0].track_type, "track1");
        assert_eq!(parsed[2].track_type, "voice_message");
    }
}
