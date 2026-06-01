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
    /// Set when a recording-file write fails (e.g. disk full). The recording
    /// is abandoned (file handle dropped) but the live stream keeps running.
    /// Surfaced via [`StreamStatus`] so the failure is never silently swallowed.
    recording_failed: Option<String>,
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
            recording_failed: None,
        }
    }

    /// Returns true if a stream is currently active.
    /// For live streams, `ffmpeg_stdin` is set. For prerecorded streams, only `ffmpeg_handle`.
    pub fn is_active(&self) -> bool {
        self.current_user.is_some() && (self.ffmpeg_stdin.is_some() || self.ffmpeg_handle.is_some())
    }

    /// Returns true if recording to file is active.
    pub fn is_recording(&self) -> bool {
        self.recording_file.is_some()
    }

    /// Returns the recording write-failure message, if a tee write has failed
    /// since recording started. Used to mark the archive as incomplete.
    pub fn recording_failure(&self) -> Option<&str> {
        self.recording_failed.as_deref()
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
    ///
    /// For live streams (stdin-based), closes stdin to signal EOF then waits.
    /// For prerecorded streams (no stdin), kills the FFmpeg process directly.
    pub async fn stop_stream(&mut self) -> Result<(), StreamError> {
        let user = self.current_user.take();
        let had_stdin = self.ffmpeg_stdin.is_some();

        // Close stdin to signal EOF to FFmpeg (live streams only)
        if let Some(mut stdin) = self.ffmpeg_stdin.take() {
            let _ = stdin.shutdown().await;
        }

        // Wait for FFmpeg to finish (with timeout), or kill immediately for prerecorded
        if let Some(mut handle) = self.ffmpeg_handle.take() {
            if !had_stdin {
                // Prerecorded stream: no stdin to close, kill directly
                tracing::info!("Killing prerecorded FFmpeg process");
                let _ = handle.kill().await;
            } else {
                // Live stream: wait for graceful exit after stdin close
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

        // Tee to recording file if active. A write failure here (e.g. disk full)
        // must NOT silently corrupt the archive: surface it as a recording-failed
        // status + error log, drop the file handle so we stop appending to a
        // half-written file, and keep the live stream running.
        if let Some(ref mut file) = self.recording_file {
            if let Err(e) = file.write_all(data).await {
                let msg = e.to_string();
                tracing::error!(
                    "Recording write failed (archive will be incomplete): {} — path={:?}",
                    msg,
                    self.recording_path
                );
                self.recording_failed = Some(msg);
                // Stop teeing — the file is now unreliable. The live stream
                // (FFmpeg) is unaffected and continues.
                self.recording_file = None;
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
        self.recording_failed = None;

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
            recording_failed: self.recording_failed.clone(),
        }
    }
}

/// Shared stream state wrapped in a Mutex for concurrent access.
pub type SharedStreamState = std::sync::Arc<Mutex<StreamState>>;

/// Create a new shared stream state.
pub fn new_shared_state() -> SharedStreamState {
    std::sync::Arc::new(Mutex::new(StreamState::new()))
}

/// Start a prerecorded stream: FFmpeg reads from a URL and outputs to RTMP.
///
/// Unlike live streaming (where audio chunks are piped via stdin), this spawns
/// FFmpeg with `-re` (real-time playback) reading directly from the presigned
/// URL. A background task monitors when FFmpeg exits and cleans up the state.
pub async fn start_prerecorded_stream(
    stream_state: &SharedStreamState,
    user: String,
    input_url: &str,
    rtmp_destination: &str,
) -> Result<(), StreamError> {
    {
        let mut state = stream_state.lock().await;

        // Clean up any existing stream first
        if state.is_active() {
            state.stop_stream().await?;
        }

        tracing::info!(
            "Starting prerecorded stream for user '{}' to {}",
            user,
            rtmp_destination
        );

        // Spawn FFmpeg with file/URL input:
        // -re: read at native frame rate (essential for streaming prerecorded content)
        let child = Command::new("ffmpeg")
            .args([
                "-hide_banner",
                "-loglevel",
                "warning",
                "-re",
                "-i",
                input_url,
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-f",
                "flv",
                rtmp_destination,
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .map_err(|e| StreamError::FfmpegSpawn(e.to_string()))?;

        state.current_user = Some(user.clone());
        state.ffmpeg_handle = Some(child);
        // No ffmpeg_stdin for prerecorded streams
    }

    // Spawn a background task to monitor when FFmpeg exits
    let monitor_state = stream_state.clone();
    let monitor_user = user.clone();
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(std::time::Duration::from_secs(2)).await;
            let mut state = monitor_state.lock().await;
            let should_break = if let Some(ref mut handle) = state.ffmpeg_handle {
                match handle.try_wait() {
                    Ok(Some(status)) => {
                        tracing::info!(
                            "Prerecorded stream FFmpeg exited with status: {} (user '{}')",
                            status,
                            monitor_user
                        );
                        state.current_user = None;
                        state.ffmpeg_handle = None;
                        true
                    }
                    Ok(None) => false, // still running
                    Err(e) => {
                        tracing::error!(
                            "Error checking prerecorded FFmpeg status: {} (user '{}')",
                            e,
                            monitor_user
                        );
                        state.current_user = None;
                        state.ffmpeg_handle = None;
                        true
                    }
                }
            } else {
                // Handle was taken by stop_stream
                true
            };
            drop(state);
            if should_break {
                break;
            }
        }
        tracing::info!("Prerecorded stream for '{}' has ended", monitor_user);
    });

    Ok(())
}

/// Status information for API responses.
#[derive(serde::Serialize)]
pub struct StreamStatus {
    pub active: bool,
    pub user: Option<String>,
    pub recording: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub recording_path: Option<PathBuf>,
    /// Non-null when a recording write failed mid-stream; the archive for this
    /// session is incomplete. Stays set until the next recording starts.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub recording_failed: Option<String>,
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
