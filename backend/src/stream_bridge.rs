//! Stream bridge: manages FFmpeg process for WebM→RTMP audio streaming.

use std::process::Stdio;
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
        }
    }

    /// Returns true if a stream is currently active.
    pub fn is_active(&self) -> bool {
        self.current_user.is_some() && self.ffmpeg_stdin.is_some()
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

    /// Write audio chunk data to FFmpeg stdin.
    pub async fn write_chunk(&mut self, data: &[u8]) -> Result<(), StreamError> {
        if let Some(ref mut stdin) = self.ffmpeg_stdin {
            stdin.write_all(data).await.map_err(|e| {
                tracing::error!("Failed to write to FFmpeg stdin: {}", e);
                StreamError::WriteFailed(e.to_string())
            })?;
            Ok(())
        } else {
            Err(StreamError::NotStreaming)
        }
    }

    /// Get status information about the current stream.
    pub fn get_status(&self) -> StreamStatus {
        StreamStatus {
            active: self.is_active(),
            user: self.current_user.clone(),
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
}
