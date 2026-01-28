//! Stream bridge: manages FFmpeg process for WebM→RTMP audio streaming.
//!
//! Also supports tee-ing audio chunks to a file for recording purposes.

use std::path::PathBuf;
use std::process::Stdio;
use tokio::fs::File;
use tokio::io::AsyncWriteExt;
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::Mutex;

/// Current state of the audio stream.
pub struct StreamState {
    /// Username of the currently streaming user (if any).
    pub current_user: Option<String>,
    /// Handle to the FFmpeg stdin for writing audio chunks.
    ffmpeg_stdin: Option<ChildStdin>,
    /// Handle to the FFmpeg child process.
    ffmpeg_handle: Option<Child>,
    /// Optional file handle for recording audio chunks to disk.
    /// When Some, audio chunks are tee'd to this file alongside FFmpeg.
    recording_file: Option<File>,
    /// Path to the current recording file (for status reporting).
    recording_path: Option<PathBuf>,
}

impl Default for StreamState {
    fn default() -> Self {
        Self::new()
    }
}

impl StreamState {
    pub fn new() -> Self {
        Self {
            current_user: None,
            ffmpeg_stdin: None,
            ffmpeg_handle: None,
            recording_file: None,
            recording_path: None,
        }
    }

    /// Returns true if a stream is currently active.
    pub fn is_active(&self) -> bool {
        self.current_user.is_some() && self.ffmpeg_stdin.is_some()
    }

    /// Returns true if recording to file is active.
    pub fn is_recording(&self) -> bool {
        self.recording_file.is_some()
    }

    /// Start streaming for the given user to the specified RTMP destination.
    ///
    /// # Arguments
    /// * `user` - Username of the streamer
    /// * `rtmp_destination` - Full RTMP URL including stream key (e.g., rtmp://host/live/key)
    pub async fn start_stream(
        &mut self,
        user: String,
        rtmp_destination: &str,
    ) -> Result<(), StreamError> {
        // Clean up any existing stream first
        if self.is_active() {
            self.stop_stream().await?;
        }

        tracing::info!(
            "Starting stream for user '{}' to {}",
            user,
            rtmp_destination
        );

        // Spawn FFmpeg process:
        // - Input: WebM/Opus from stdin
        // - Output: AAC audio to RTMP
        // - High quality audio settings for smooth playback
        let mut child = Command::new("ffmpeg")
            .args([
                "-hide_banner",
                "-loglevel",
                "warning",
                // Input from stdin as WebM container
                "-f",
                "webm",
                "-i",
                "pipe:0",
                // Audio codec: AAC with good quality
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
                "-ac",
                "2",
                // Output format: FLV for RTMP
                "-f",
                "flv",
                rtmp_destination,
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .map_err(|e| StreamError::FfmpegSpawn(e.to_string()))?;

        let stdin = child.stdin.take().ok_or(StreamError::NoStdin)?;

        self.current_user = Some(user);
        self.ffmpeg_stdin = Some(stdin);
        self.ffmpeg_handle = Some(child);

        Ok(())
    }

    /// Stop the current stream gracefully.
    pub async fn stop_stream(&mut self) -> Result<(), StreamError> {
        let user = self.current_user.take();

        // Close stdin to signal EOF to FFmpeg
        if let Some(mut stdin) = self.ffmpeg_stdin.take() {
            let _ = stdin.shutdown().await;
        }

        // Wait for FFmpeg to finish (with timeout)
        if let Some(mut handle) = self.ffmpeg_handle.take() {
            match tokio::time::timeout(std::time::Duration::from_secs(5), handle.wait()).await {
                Ok(Ok(status)) => {
                    tracing::info!("FFmpeg exited with status: {}", status);
                }
                Ok(Err(e)) => {
                    tracing::warn!("FFmpeg wait error: {}", e);
                }
                Err(_) => {
                    tracing::warn!("FFmpeg did not exit in time, killing...");
                    let _ = handle.kill().await;
                }
            }
        }

        if let Some(u) = user {
            tracing::info!("Stream stopped for user '{}'", u);
        }

        Ok(())
    }

    /// Write audio chunk data to FFmpeg stdin and optionally to recording file.
    ///
    /// If recording is active, chunks are tee'd to the recording file alongside FFmpeg.
    /// Recording file write errors are logged but don't fail the stream.
    pub async fn write_chunk(&mut self, data: &[u8]) -> Result<(), StreamError> {
        // Write to FFmpeg (required for streaming)
        if let Some(ref mut stdin) = self.ffmpeg_stdin {
            stdin.write_all(data).await.map_err(|e| {
                tracing::error!("Failed to write to FFmpeg stdin: {}", e);
                StreamError::WriteFailed(e.to_string())
            })?;
        } else {
            return Err(StreamError::NotStreaming);
        }

        // Tee to recording file if active (non-fatal errors)
        if let Some(ref mut file) = self.recording_file {
            if let Err(e) = file.write_all(data).await {
                tracing::warn!("Failed to write to recording file: {}", e);
                // Don't fail the stream, just log the error
            }
        }

        Ok(())
    }

    /// Start recording audio chunks to a file.
    ///
    /// Creates the file at the specified path and begins tee-ing all audio chunks.
    /// The file is created with the parent directories if needed.
    ///
    /// # Arguments
    /// * `path` - Path to the recording file (e.g., "./data/recordings-temp/recording_1_2026-01-28T19-30-00.webm")
    pub async fn start_recording(&mut self, path: PathBuf) -> Result<(), StreamError> {
        // Close any existing recording first
        if self.recording_file.is_some() {
            self.stop_recording().await?;
        }

        // Ensure parent directory exists
        if let Some(parent) = path.parent() {
            tokio::fs::create_dir_all(parent).await.map_err(|e| {
                StreamError::RecordingError(format!("Failed to create directory: {}", e))
            })?;
        }

        // Create the recording file
        let file = File::create(&path).await.map_err(|e| {
            StreamError::RecordingError(format!("Failed to create recording file: {}", e))
        })?;

        tracing::info!("Started recording to {:?}", path);

        self.recording_file = Some(file);
        self.recording_path = Some(path);

        Ok(())
    }

    /// Stop recording and close the recording file.
    ///
    /// Returns the path to the recording file if one was active.
    pub async fn stop_recording(&mut self) -> Result<Option<PathBuf>, StreamError> {
        let path = self.recording_path.take();

        if let Some(mut file) = self.recording_file.take() {
            // Flush and close the file
            if let Err(e) = file.flush().await {
                tracing::warn!("Failed to flush recording file: {}", e);
            }
            if let Err(e) = file.shutdown().await {
                tracing::warn!("Failed to close recording file: {}", e);
            }

            if let Some(ref p) = path {
                tracing::info!("Stopped recording to {:?}", p);
            }
        }

        Ok(path)
    }

    /// Get status information about the current stream.
    pub fn get_status(&self) -> StreamStatus {
        StreamStatus {
            active: self.is_active(),
            user: self.current_user.clone(),
            recording: self.is_recording(),
            recording_path: self.recording_path.clone(),
        }
    }
}

/// Shared stream state wrapped in a Mutex for concurrent access.
pub type SharedStreamState = std::sync::Arc<Mutex<StreamState>>;

/// Create a new shared stream state.
pub fn new_shared_state() -> SharedStreamState {
    std::sync::Arc::new(Mutex::new(StreamState::new()))
}

/// Status information for API responses.
#[derive(serde::Serialize)]
pub struct StreamStatus {
    pub active: bool,
    pub user: Option<String>,
    pub recording: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub recording_path: Option<PathBuf>,
}

/// Errors that can occur during streaming.
#[derive(Debug, thiserror::Error)]
pub enum StreamError {
    #[error("Failed to spawn FFmpeg: {0}")]
    FfmpegSpawn(String),

    #[error("FFmpeg stdin not available")]
    NoStdin,

    #[error("Not currently streaming")]
    NotStreaming,

    #[error("Failed to write audio data: {0}")]
    WriteFailed(String),

    #[error("Recording error: {0}")]
    RecordingError(String),
}
