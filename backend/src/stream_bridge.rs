//! Stream bridge: manages FFmpeg process for WebM→RTMP audio streaming.
//!
//! Recording is tee'd to a **second, independent** FFmpeg process that writes
//! crash-safe MPEG-TS segments (`-f segment`). A recorder crash then loses at
//! most one ~10s segment instead of the whole show; the segments are
//! concat-demuxed into a single artifact at stop. The live RTMP stream is never
//! affected by a recording failure.

use std::path::{Path, PathBuf};
use std::process::Stdio;
use tokio::io::AsyncWriteExt;
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::Mutex;

/// Length of each recording segment, in seconds. A crash loses at most one.
const SEGMENT_SECONDS: u32 = 10;

/// Current state of the audio stream.
pub struct StreamState {
    /// Username of the currently streaming user (if any).
    pub current_user: Option<String>,
    /// Handle to the FFmpeg stdin for writing audio chunks.
    ffmpeg_stdin: Option<ChildStdin>,
    /// Handle to the FFmpeg child process.
    ffmpeg_handle: Option<Child>,
    /// Stdin of the recording FFmpeg (segment muxer). When Some, audio chunks
    /// are tee'd here alongside the streaming FFmpeg.
    recording_stdin: Option<ChildStdin>,
    /// Handle to the recording FFmpeg child process.
    recording_handle: Option<Child>,
    /// Directory holding the in-progress MPEG-TS segments for this session.
    recording_seg_dir: Option<PathBuf>,
    /// Final concat target — the single artifact stop produces from the segments
    /// (also used for status reporting).
    recording_path: Option<PathBuf>,
    /// Set when a recording tee write fails (e.g. disk full, or the recorder
    /// FFmpeg died). The recording is abandoned but the live stream keeps
    /// running. Surfaced via [`StreamStatus`] so it's never silently swallowed.
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
            recording_stdin: None,
            recording_handle: None,
            recording_seg_dir: None,
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
        self.recording_stdin.is_some()
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

        // Tee to the recording FFmpeg (segment muxer) if active. A write failure
        // here (disk full, or the recorder died) must NOT silently corrupt the
        // archive: surface it as a recording-failed status + error log, drop the
        // recorder stdin (segments already flushed to disk survive), and keep the
        // live stream running.
        if let Some(ref mut stdin) = self.recording_stdin {
            if let Err(e) = stdin.write_all(data).await {
                let msg = e.to_string();
                tracing::error!(
                    "Recording tee write failed (archive will be incomplete): {} — segments={:?}",
                    msg,
                    self.recording_seg_dir
                );
                self.recording_failed = Some(msg);
                // Stop teeing. Completed segments on disk are still recoverable
                // at stop. The live stream (FFmpeg) is unaffected and continues.
                self.recording_stdin = None;
            }
        }

        Ok(())
    }

    /// Start recording by spawning a segment-muxer FFmpeg.
    ///
    /// Audio chunks tee'd via [`write_chunk`] are written to a second, independent
    /// FFmpeg process that emits ~[`SEGMENT_SECONDS`]s MPEG-TS segments into a
    /// sibling `.segs/` directory. At [`stop_recording`] the segments are
    /// concat-demuxed into `path`. A crash loses at most the in-progress segment.
    ///
    /// # Arguments
    /// * `path` - Final artifact path (e.g. ".../recording_1_2026-01-28T19-30-00.webm").
    ///   The concatenated artifact is MPEG-TS; the extension is kept for
    ///   backward compatibility with the existing R2 layout (FFmpeg probes
    ///   content, not the name).
    pub async fn start_recording(&mut self, path: PathBuf) -> Result<(), StreamError> {
        // Close any existing recording first
        if self.is_recording() {
            self.stop_recording().await?;
        }

        // Per-session segment directory: ".../recording_<id>_<ts>.segs/"
        let seg_dir = path.with_extension("segs");
        tokio::fs::create_dir_all(&seg_dir).await.map_err(|e| {
            StreamError::RecordingError(format!("Failed to create segment directory: {}", e))
        })?;

        let seg_pattern = seg_dir.join("seg_%05d.ts");

        // Recording FFmpeg: WebM/Opus on stdin → AAC → MPEG-TS segments.
        // Audio-only means every frame is effectively a keyframe, so there is no
        // segment-alignment caveat.
        let mut child = Command::new("ffmpeg")
            .args([
                "-hide_banner",
                "-loglevel",
                "warning",
                "-f",
                "webm",
                "-i",
                "pipe:0",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-f",
                "segment",
                "-segment_time",
                &SEGMENT_SECONDS.to_string(),
                "-segment_format",
                "mpegts",
                "-reset_timestamps",
                "1",
            ])
            .arg(&seg_pattern)
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .map_err(|e| StreamError::FfmpegSpawn(e.to_string()))?;

        let stdin = child.stdin.take().ok_or(StreamError::NoStdin)?;

        tracing::info!(
            "Started crash-safe segment recording to {:?} (final artifact {:?})",
            seg_dir,
            path
        );

        self.recording_stdin = Some(stdin);
        self.recording_handle = Some(child);
        self.recording_seg_dir = Some(seg_dir);
        self.recording_path = Some(path);
        self.recording_failed = None;

        Ok(())
    }

    /// Stop recording: signal EOF to the recorder, wait for it to flush the last
    /// segment, then concat-demux all segments into the final artifact path.
    ///
    /// Returns the path to the concatenated artifact if a recording was active.
    pub async fn stop_recording(&mut self) -> Result<Option<PathBuf>, StreamError> {
        let path = self.recording_path.take();
        let seg_dir = self.recording_seg_dir.take();

        // Close stdin to signal EOF so FFmpeg finalizes the last segment.
        if let Some(mut stdin) = self.recording_stdin.take() {
            let _ = stdin.shutdown().await;
        }

        // Wait for the recorder to exit (it may already be dead, e.g. crash).
        if let Some(mut handle) = self.recording_handle.take() {
            match tokio::time::timeout(std::time::Duration::from_secs(10), handle.wait()).await {
                Ok(Ok(status)) => tracing::info!("Recording FFmpeg exited with status: {}", status),
                Ok(Err(e)) => tracing::warn!("Recording FFmpeg wait error: {}", e),
                Err(_) => {
                    tracing::warn!("Recording FFmpeg did not exit in time, killing...");
                    let _ = handle.kill().await;
                }
            }
        }

        // Concat the surviving segments into the final artifact.
        if let (Some(ref dir), Some(ref out)) = (&seg_dir, &path) {
            if let Err(e) = concat_segments(dir, out).await {
                tracing::error!("Failed to concat recording segments: {}", e);
                self.recording_failed = Some(format!("Segment concat failed: {}", e));
            } else {
                tracing::info!("Concatenated recording segments into {:?}", out);
            }
            // Best-effort cleanup of the segment directory.
            if let Err(e) = tokio::fs::remove_dir_all(dir).await {
                tracing::warn!("Failed to clean up segment directory {:?}: {}", dir, e);
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

/// True if `path` names a recording segment (`seg_*.ts`).
fn is_segment_file(path: &Path) -> bool {
    path.file_name()
        .and_then(|n| n.to_str())
        .map(|n| n.starts_with("seg_") && n.ends_with(".ts"))
        .unwrap_or(false)
}

/// Build an FFmpeg concat-demuxer list from ordered segment paths.
/// Paths are app-controlled (`seg_%05d.ts`), so no quote-escaping is needed.
fn build_concat_list(segments: &[PathBuf]) -> String {
    let mut list = String::new();
    for seg in segments {
        list.push_str(&format!("file '{}'\n", seg.to_string_lossy()));
    }
    list
}

/// Concat-demux the MPEG-TS segments in `seg_dir` into a single `output` file.
///
/// Segments are concatenated in lexicographic order (`seg_00000.ts`, …), which
/// matches their chronological order. Uses stream copy (`-c copy`) so there's no
/// re-encode, and forces `-f mpegts` because the output path keeps a legacy
/// `.webm` extension for R2-layout compatibility.
async fn concat_segments(seg_dir: &Path, output: &Path) -> Result<(), StreamError> {
    // Collect + sort the segment files.
    let mut segments: Vec<PathBuf> = Vec::new();
    let mut entries = tokio::fs::read_dir(seg_dir)
        .await
        .map_err(|e| StreamError::RecordingError(format!("read segment dir: {}", e)))?;
    while let Some(entry) = entries
        .next_entry()
        .await
        .map_err(|e| StreamError::RecordingError(format!("read segment entry: {}", e)))?
    {
        if is_segment_file(&entry.path()) {
            segments.push(entry.path());
        }
    }
    segments.sort();

    if segments.is_empty() {
        return Err(StreamError::RecordingError(
            "no recording segments were produced".to_string(),
        ));
    }

    // Build the concat demuxer list file.
    let list = build_concat_list(&segments);
    let list_path = seg_dir.join("concat_list.txt");
    tokio::fs::write(&list_path, list)
        .await
        .map_err(|e| StreamError::RecordingError(format!("write concat list: {}", e)))?;

    let status = Command::new("ffmpeg")
        .args([
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
        ])
        .arg(&list_path)
        .args(["-c", "copy", "-f", "mpegts"])
        .arg(output)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .output()
        .await
        .map_err(|e| StreamError::RecordingError(format!("spawn concat ffmpeg: {}", e)))?;

    if !status.status.success() {
        let stderr = String::from_utf8_lossy(&status.stderr);
        return Err(StreamError::RecordingError(format!(
            "concat ffmpeg failed: {}",
            stderr.lines().last().unwrap_or("unknown error")
        )));
    }

    tracing::info!(
        "Concatenated {} segment(s) from {:?}",
        segments.len(),
        seg_dir
    );
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn segment_files_are_recognized() {
        assert!(is_segment_file(Path::new("/tmp/x.segs/seg_00000.ts")));
        assert!(is_segment_file(Path::new("/tmp/x.segs/seg_00042.ts")));
        assert!(!is_segment_file(Path::new("/tmp/x.segs/concat_list.txt")));
        assert!(!is_segment_file(Path::new("/tmp/x.segs/seg_00000.tmp")));
        assert!(!is_segment_file(Path::new("/tmp/x.segs/raw.webm")));
    }

    #[test]
    fn concat_list_is_ordered_and_quoted() {
        let segs = vec![
            PathBuf::from("/r/seg_00000.ts"),
            PathBuf::from("/r/seg_00001.ts"),
            PathBuf::from("/r/seg_00002.ts"),
        ];
        let list = build_concat_list(&segs);
        assert_eq!(
            list,
            "file '/r/seg_00000.ts'\nfile '/r/seg_00001.ts'\nfile '/r/seg_00002.ts'\n"
        );
    }

    #[tokio::test]
    async fn concat_errors_when_no_segments_survive() {
        // Simulates a crash before any segment was flushed: stop must surface an
        // error rather than silently producing an empty artifact.
        let dir = tempfile::TempDir::new().unwrap();
        let out = dir.path().join("raw.webm");
        let err = concat_segments(dir.path(), &out).await.unwrap_err();
        assert!(matches!(err, StreamError::RecordingError(_)));
        assert!(!out.exists());
    }
}
