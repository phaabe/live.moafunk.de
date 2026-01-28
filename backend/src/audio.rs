//! Audio file processing utilities.
//!
//! This module provides functions to convert audio files to MP3 format using ffmpeg.

use crate::{config::Config, AppError, Result};
use std::path::Path;
use tokio::process::Command;

/// Supported audio file extensions that can be converted to MP3.
pub const SUPPORTED_AUDIO_EXTENSIONS: &[&str] = &[
    "mp3", "wav", "aiff", "aif", "ogg", "flac", "m4a", "aac", "opus", "wma", "webm",
];

/// Check if a file extension is a supported audio format.
pub fn is_supported_audio_format(filename: &str) -> bool {
    let ext = Path::new(filename)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();

    SUPPORTED_AUDIO_EXTENSIONS.contains(&ext.as_str())
}

/// Check if a file is already in MP3 format.
pub fn is_mp3(filename: &str) -> bool {
    let ext = Path::new(filename)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();

    ext == "mp3"
}

/// Convert audio data to MP3 format using ffmpeg.
///
/// Returns the converted MP3 data. If the input is already MP3, it's returned as-is
/// (though it may still be re-encoded for consistency).
///
/// # Arguments
/// * `data` - The raw audio file bytes
/// * `original_filename` - The original filename (used to determine input format)
/// * `config` - Application config with ffmpeg settings
///
/// # Returns
/// * `Ok(Vec<u8>)` - The MP3 encoded audio data
/// * `Err(AppError)` - If conversion fails
pub async fn convert_to_mp3(
    data: &[u8],
    original_filename: &str,
    config: &Config,
) -> Result<Vec<u8>> {
    // Create temporary files for input and output
    let temp_dir = std::env::temp_dir();
    let unique_id = uuid::Uuid::new_v4().to_string();

    let input_ext = Path::new(original_filename)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("bin");

    let input_path = temp_dir.join(format!("audio_input_{}.{}", unique_id, input_ext));
    let output_path = temp_dir.join(format!("audio_output_{}.mp3", unique_id));

    // Write input data to temp file
    tokio::fs::write(&input_path, data)
        .await
        .map_err(|e| AppError::Internal(format!("Failed to write temp input file: {}", e)))?;

    // Run ffmpeg to convert to MP3
    // -i: input file
    // -vn: no video
    // -acodec libmp3lame: use LAME MP3 encoder
    // -ab: bitrate (from config, e.g., "192k", "256k", "320k")
    // -ar: sample rate (from config, e.g., 44100, 48000)
    // -y: overwrite output file
    let sample_rate_str = config.ffmpeg_mp3_sample_rate.to_string();
    let output = Command::new("ffmpeg")
        .args([
            "-i",
            input_path.to_str().unwrap(),
            "-vn",
            "-acodec",
            "libmp3lame",
            "-ab",
            &config.ffmpeg_mp3_bitrate,
            "-ar",
            &sample_rate_str,
            "-y",
            output_path.to_str().unwrap(),
        ])
        .output()
        .await
        .map_err(|e| {
            AppError::Internal(format!("Failed to run ffmpeg (is it installed?): {}", e))
        })?;

    // Clean up input file
    let _ = tokio::fs::remove_file(&input_path).await;

    if !output.status.success() {
        // Clean up output file if it exists
        let _ = tokio::fs::remove_file(&output_path).await;

        let stderr = String::from_utf8_lossy(&output.stderr);
        tracing::error!("ffmpeg conversion failed: {}", stderr);
        return Err(AppError::Internal(format!(
            "Audio conversion failed: {}",
            stderr.lines().last().unwrap_or("Unknown error")
        )));
    }

    // Read the converted MP3 data
    let mp3_data = tokio::fs::read(&output_path)
        .await
        .map_err(|e| AppError::Internal(format!("Failed to read converted audio: {}", e)))?;

    // Clean up output file
    let _ = tokio::fs::remove_file(&output_path).await;

    tracing::debug!(
        "Converted {} ({} bytes) to MP3 ({} bytes)",
        original_filename,
        data.len(),
        mp3_data.len()
    );

    Ok(mp3_data)
}

/// Get the duration of an audio file in milliseconds using ffprobe.
///
/// Uses ffprobe to extract the duration from the audio file's metadata or by
/// decoding if necessary.
///
/// # Arguments
/// * `path` - Path to the audio file
///
/// # Returns
/// * `Ok(u64)` - Duration in milliseconds
/// * `Err(AppError)` - If ffprobe fails or duration cannot be parsed
pub async fn get_duration<P: AsRef<Path>>(path: P) -> Result<u64> {
    let path = path.as_ref();
    let path_str = path
        .to_str()
        .ok_or_else(|| AppError::Internal("Invalid path encoding".to_string()))?;

    // First try: Get duration from format header (fast, works for most files)
    let output = Command::new("ffprobe")
        .args([
            "-v",
            "quiet",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            path_str,
        ])
        .output()
        .await
        .map_err(|e| {
            AppError::Internal(format!("Failed to run ffprobe (is it installed?): {}", e))
        })?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        tracing::error!("ffprobe failed for {}: {}", path.display(), stderr);
        return Err(AppError::Internal(format!(
            "Failed to get audio duration: {}",
            stderr.lines().last().unwrap_or("Unknown error")
        )));
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    let duration_str = stdout.trim();

    // If format duration is available, use it
    if !duration_str.is_empty() && duration_str != "N/A" {
        if let Ok(duration_secs) = duration_str.parse::<f64>() {
            let duration_ms = (duration_secs * 1000.0).round() as u64;
            tracing::debug!(
                "Audio duration for {} (from format): {} ms ({:.2} s)",
                path.display(),
                duration_ms,
                duration_secs
            );
            return Ok(duration_ms);
        }
    }

    // Fallback: For streaming WebM files without duration header,
    // get the last packet timestamp (slower but works for all valid files)
    tracing::debug!(
        "Duration not in format header for {}, scanning packets...",
        path.display()
    );

    let output = Command::new("ffprobe")
        .args([
            "-v",
            "quiet",
            "-select_streams",
            "a:0",
            "-show_entries",
            "packet=pts_time",
            "-of",
            "csv=p=0",
            path_str,
        ])
        .output()
        .await
        .map_err(|e| AppError::Internal(format!("Failed to run ffprobe packet scan: {}", e)))?;

    if !output.status.success() {
        return Err(AppError::Internal(format!(
            "Failed to scan packets for duration: {}",
            String::from_utf8_lossy(&output.stderr)
        )));
    }

    let stdout = String::from_utf8_lossy(&output.stdout);

    // Get the last valid timestamp from packet scan
    let last_pts = stdout
        .lines()
        .filter_map(|line| line.trim().parse::<f64>().ok())
        .last();

    match last_pts {
        Some(duration_secs) => {
            // Add a small buffer for the last packet's duration (~20ms for Opus)
            let duration_ms = ((duration_secs + 0.02) * 1000.0).round() as u64;
            tracing::debug!(
                "Audio duration for {} (from packets): {} ms ({:.2} s)",
                path.display(),
                duration_ms,
                duration_secs
            );
            Ok(duration_ms)
        }
        None => Err(AppError::Internal(format!(
            "Could not determine duration - file may be empty or corrupted ({})",
            path.display()
        ))),
    }
}

/// Get the duration of audio data from raw bytes using ffprobe.
///
/// Creates a temporary file, probes it, and cleans up.
///
/// # Arguments
/// * `data` - The raw audio file bytes
/// * `filename_hint` - A filename hint for determining the format (extension)
///
/// # Returns
/// * `Ok(u64)` - Duration in milliseconds
/// * `Err(AppError)` - If ffprobe fails or duration cannot be parsed
pub async fn get_duration_from_bytes(data: &[u8], filename_hint: &str) -> Result<u64> {
    let temp_dir = std::env::temp_dir();
    let unique_id = uuid::Uuid::new_v4().to_string();

    let ext = Path::new(filename_hint)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("bin");

    let temp_path = temp_dir.join(format!("audio_probe_{}.{}", unique_id, ext));

    // Write data to temp file
    tokio::fs::write(&temp_path, data)
        .await
        .map_err(|e| AppError::Internal(format!("Failed to write temp file: {}", e)))?;

    // Get duration
    let result = get_duration(&temp_path).await;

    // Clean up
    let _ = tokio::fs::remove_file(&temp_path).await;

    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_is_supported_audio_format() {
        assert!(is_supported_audio_format("track.mp3"));
        assert!(is_supported_audio_format("track.MP3"));
        assert!(is_supported_audio_format("track.wav"));
        assert!(is_supported_audio_format("track.WAV"));
        assert!(is_supported_audio_format("track.ogg"));
        assert!(is_supported_audio_format("track.flac"));
        assert!(is_supported_audio_format("track.m4a"));
        assert!(is_supported_audio_format("track.aiff"));
        assert!(is_supported_audio_format("track.opus"));
        assert!(!is_supported_audio_format("track.txt"));
        assert!(!is_supported_audio_format("track.jpg"));
    }

    #[test]
    fn test_is_mp3() {
        assert!(is_mp3("track.mp3"));
        assert!(is_mp3("track.MP3"));
        assert!(!is_mp3("track.wav"));
        assert!(!is_mp3("track.flac"));
    }
}
