//! Track preview video generation for Instagram carousel posts.
//!
//! Generates a 30-second MP4 video from:
//! - Artist profile image (background, scaled to 1080×1080)
//! - Track audio (first 30 seconds)
//! - Animated waveform visualization via FFmpeg's `showwaves` filter
//!
//! Uses a single FFmpeg invocation — no external tools required beyond FFmpeg.

use crate::{models, storage, AppError, AppState, Result};
use aws_sdk_s3::primitives::ByteStream;
use std::sync::Arc;
use tokio::process::Command;

/// Output video dimensions (Instagram square format).
const VIDEO_WIDTH: u32 = 1080;
const VIDEO_HEIGHT: u32 = 1080;

/// Waveform overlay dimensions and position.
const WAVEFORM_WIDTH: u32 = VIDEO_WIDTH;
const WAVEFORM_HEIGHT: u32 = 200;
const WAVEFORM_Y_OFFSET: u32 = VIDEO_HEIGHT - WAVEFORM_HEIGHT; // 880

/// FFmpeg `showwaves` filter configuration.
const WAVEFORM_MODE: &str = "cline"; // symmetric centered bars
const WAVEFORM_SCALE: &str = "sqrt"; // balanced dynamic range

/// Brightness threshold for choosing waveform color.
/// Below this value (0–255) the bottom quarter is considered dark → white waveform.
/// Above → black waveform for contrast.
const BRIGHTNESS_THRESHOLD: u8 = 128;

/// Video encoding defaults.
const VIDEO_FPS: u32 = 30;
const VIDEO_DURATION_SECS: u32 = 30;

// ────────────────────────────────────────────────────────────────────────────
// Public API
// ────────────────────────────────────────────────────────────────────────────

/// Generate a 30-second MP4 track preview video.
///
/// Downloads the artist image and track audio from R2, then composites
/// an animated waveform overlay onto the image using FFmpeg's `showwaves`
/// filter, producing a 1080×1080 H.264+AAC MP4.
///
/// # Arguments
/// * `state`            – Shared app state (for R2 access)
/// * `artist_image_key` – R2 key for the artist's profile image
/// * `track_key`        – R2 key for the track MP3
/// * `_peaks_key`       – Unused (kept for API compatibility)
/// * `duration_secs`    – Duration of the preview (default 30)
///
/// # Returns
/// MP4 file bytes
pub async fn generate_track_preview_video(
    state: &Arc<AppState>,
    artist_image_key: &str,
    track_key: &str,
    _peaks_key: &str,
    duration_secs: u32,
) -> Result<Vec<u8>> {
    let duration = if duration_secs == 0 {
        VIDEO_DURATION_SECS
    } else {
        duration_secs
    };

    let unique_id = uuid::Uuid::new_v4().to_string();
    let temp_dir = std::env::temp_dir().join(format!("video_{}", unique_id));
    tokio::fs::create_dir_all(&temp_dir)
        .await
        .map_err(|e| AppError::Internal(format!("Failed to create temp dir: {}", e)))?;

    // Ensure cleanup even on error
    let result = generate_inner(state, artist_image_key, track_key, duration, &temp_dir).await;

    // Clean up temp directory
    let _ = tokio::fs::remove_dir_all(&temp_dir).await;

    result
}

/// Generate preview videos for all tracks of an artist and store them in R2.
///
/// Loads the artist from DB, generates a waveform preview video for each
/// track that has an MP3 key, uploads the result to R2, and updates the
/// artist's `track1_video_key` / `track2_video_key` in the database.
///
/// Uses the best available profile image (overlay → cropped → original).
pub async fn generate_and_store_artist_videos(state: Arc<AppState>, artist_id: i64) -> Result<()> {
    tracing::info!("Generating preview videos for artist {}", artist_id);

    // Load artist from DB
    let artist: models::Artist = sqlx::query_as("SELECT * FROM artists WHERE id = ?")
        .bind(artist_id)
        .fetch_optional(&state.db)
        .await?
        .ok_or_else(|| {
            AppError::NotFound(format!(
                "Artist {} not found for video generation",
                artist_id
            ))
        })?;

    // Determine background image (overlay → cropped → original)
    let image_key = artist
        .pic_overlay_key
        .as_ref()
        .or(artist.pic_cropped_key.as_ref())
        .or(artist.pic_key.as_ref())
        .ok_or_else(|| {
            AppError::Validation(format!(
                "Artist {} has no profile picture for video generation",
                artist_id
            ))
        })?
        .clone();

    // Generate video for each track
    let tracks: Vec<(&str, Option<&str>, &str)> = vec![
        ("track1", artist.track1_key.as_deref(), "track1_video_key"),
        ("track2", artist.track2_key.as_deref(), "track2_video_key"),
    ];

    for (label, track_key, db_column) in &tracks {
        let Some(track_key) = track_key else {
            tracing::info!("Artist {} has no {} — skipping video", artist_id, label);
            continue;
        };

        tracing::info!(
            "Generating {} preview video for artist {}",
            label,
            artist_id
        );

        match generate_track_preview_video(&state, &image_key, track_key, "", VIDEO_DURATION_SECS)
            .await
        {
            Ok(mp4_bytes) => {
                let video_key = format!("artists/{}/{}_video/preview.mp4", artist_id, label);

                // Upload to R2
                if let Err(e) = upload_video_to_r2(&state, &video_key, mp4_bytes).await {
                    tracing::error!(
                        "Failed to upload {} video for artist {}: {}",
                        label,
                        artist_id,
                        e
                    );
                    continue;
                }

                // Update DB
                let query = format!(
                    "UPDATE artists SET {} = ?, updated_at = datetime('now') WHERE id = ?",
                    db_column
                );
                if let Err(e) = sqlx::query(&query)
                    .bind(&video_key)
                    .bind(artist_id)
                    .execute(&state.db)
                    .await
                {
                    tracing::error!(
                        "Failed to update {} for artist {}: {}",
                        db_column,
                        artist_id,
                        e
                    );
                    continue;
                }

                tracing::info!(
                    "Stored {} preview video for artist {}: {}",
                    label,
                    artist_id,
                    video_key
                );
            }
            Err(e) => {
                tracing::error!(
                    "Failed to generate {} preview video for artist {}: {}",
                    label,
                    artist_id,
                    e
                );
            }
        }
    }

    tracing::info!(
        "Finished generating preview videos for artist {}",
        artist_id
    );
    Ok(())
}

// ────────────────────────────────────────────────────────────────────────────
// Internal pipeline
// ────────────────────────────────────────────────────────────────────────────

async fn generate_inner(
    state: &Arc<AppState>,
    artist_image_key: &str,
    track_key: &str,
    duration_secs: u32,
    temp_dir: &std::path::Path,
) -> Result<Vec<u8>> {
    tracing::info!(
        "Generating track preview video ({}s) — image: {}, track: {}",
        duration_secs,
        artist_image_key,
        track_key,
    );

    // Step 1: Download image and track from R2
    let (image_result, track_result) = tokio::join!(
        storage::download_file(state, artist_image_key),
        storage::download_file(state, track_key),
    );

    let (image_data, _) = image_result?;
    let (track_data, _) = track_result?;

    tracing::info!(
        "Downloaded files: image={}KB, track={}KB",
        image_data.len() / 1024,
        track_data.len() / 1024,
    );

    // Step 2: Write inputs to temp files
    let image_ext = if artist_image_key.ends_with(".png") {
        "png"
    } else {
        "jpg"
    };
    let image_path = temp_dir.join(format!("artist.{}", image_ext));
    let track_path = temp_dir.join("track.mp3");
    let output_path = temp_dir.join("output.mp4");

    tokio::fs::write(&image_path, &image_data)
        .await
        .map_err(|e| AppError::Internal(format!("Failed to write image temp file: {}", e)))?;
    tokio::fs::write(&track_path, &track_data)
        .await
        .map_err(|e| AppError::Internal(format!("Failed to write track temp file: {}", e)))?;

    // Step 3: Analyze bottom quarter of image to pick waveform color
    let waveform_color = choose_waveform_color(&image_data);
    tracing::info!("Waveform color chosen: {}", waveform_color);

    // Step 4: Compose video with FFmpeg (showwaves + overlay on background)
    compose_video(
        &image_path,
        &track_path,
        &output_path,
        duration_secs,
        waveform_color,
    )
    .await?;

    // Step 5: Read output
    let mp4_data = tokio::fs::read(&output_path)
        .await
        .map_err(|e| AppError::Internal(format!("Failed to read output video: {}", e)))?;

    tracing::info!(
        "Video generated successfully: {} bytes ({:.1} MB)",
        mp4_data.len(),
        mp4_data.len() as f64 / 1_048_576.0
    );

    Ok(mp4_data)
}

// ────────────────────────────────────────────────────────────────────────────
// Image brightness analysis
// ────────────────────────────────────────────────────────────────────────────

/// Analyze the average brightness of the bottom quarter of an image.
///
/// Returns a value 0–255 (0 = fully black, 255 = fully white).
fn analyze_bottom_brightness(data: &[u8]) -> u8 {
    let img = match image::load_from_memory(data) {
        Ok(img) => img.into_rgba8(),
        Err(e) => {
            tracing::warn!(
                "Failed to decode image for brightness analysis: {} — defaulting to dark",
                e
            );
            return 0; // assume dark → white waveform
        }
    };

    let (width, height) = (img.width(), img.height());
    if width == 0 || height == 0 {
        return 0;
    }

    let quarter_start = height - (height / 4);
    let mut total_luma: u64 = 0;
    let mut pixel_count: u64 = 0;

    for y in quarter_start..height {
        for x in 0..width {
            let px = img.get_pixel(x, y);
            // ITU-R BT.601 luma: 0.299*R + 0.587*G + 0.114*B
            let luma = (0.299 * px[0] as f64) + (0.587 * px[1] as f64) + (0.114 * px[2] as f64);
            total_luma += luma as u64;
            pixel_count += 1;
        }
    }

    if pixel_count == 0 {
        return 0;
    }

    (total_luma / pixel_count) as u8
}

/// Pick the waveform color based on the bottom quarter brightness.
///
/// - Dark background (below threshold) → `"white"` waveform
/// - Bright background (at or above threshold) → `"black"` waveform
fn choose_waveform_color(image_data: &[u8]) -> &'static str {
    let brightness = analyze_bottom_brightness(image_data);
    tracing::info!("Bottom-quarter average brightness: {}/255", brightness);
    if brightness >= BRIGHTNESS_THRESHOLD {
        "black"
    } else {
        "white"
    }
}

// ────────────────────────────────────────────────────────────────────────────
// FFmpeg video composition (showwaves + colorkey + overlay)
// ────────────────────────────────────────────────────────────────────────────

/// Build the FFmpeg `filter_complex` string for showwaves overlay.
///
/// Pipeline:
/// 1. `[1:a]showwaves` → animated waveform on black background
/// 2. `colorkey=black` → make black transparent (for white waveform)
///    or `colorkey=white` → make white transparent (for black waveform)
/// 3. `[0:v]scale+crop` → artist image to 1080×1080
/// 4. `overlay` → composite waveform onto image
fn build_filter_complex(_duration_secs: u32, waveform_color: &str) -> String {
    // When the waveform is white, it's drawn on a black background → colorkey black.
    // When the waveform is black, it's drawn on a black background too,
    // so we negate the waveform video to get black-on-white, then colorkey white.
    let (wave_post, colorkey_color, colorkey_similarity, colorkey_blend) =
        if waveform_color == "black" {
            // showwaves draws white on black → negate → black on white → colorkey white
            ("negate,", "white", "0.01", "0.15")
        } else {
            // showwaves draws white on black → colorkey black
            ("", "black", "0.01", "0.15")
        };

    format!(
        "[1:a]showwaves=s={ww}x{wh}:mode={mode}:rate={fps}:colors=white:scale={scale}:draw=full[wave];\
         [wave]{wave_post}colorkey={ck_color}:{ck_sim}:{ck_blend}[wavealpha];\
         [0:v]scale={w}:{h}:force_original_aspect_ratio=increase,\
         crop={w}:{h},setsar=1,fps={fps}[bg];\
         [bg][wavealpha]overlay=(W-w)/2:{wfy}:shortest=1[out]",
        w = VIDEO_WIDTH,
        h = VIDEO_HEIGHT,
        ww = WAVEFORM_WIDTH,
        wh = WAVEFORM_HEIGHT,
        wfy = WAVEFORM_Y_OFFSET,
        fps = VIDEO_FPS,
        mode = WAVEFORM_MODE,
        scale = WAVEFORM_SCALE,
        wave_post = wave_post,
        ck_color = colorkey_color,
        ck_sim = colorkey_similarity,
        ck_blend = colorkey_blend,
    )
}

/// Compose the final video using FFmpeg with the `showwaves` filter.
///
/// Inputs:
/// - `image_path`: artist profile image (any resolution, will be scaled)
/// - `track_path`: MP3 audio
/// - `output_path`: where to write the MP4
/// - `duration_secs`: video length (e.g. 30)
///
/// Only 2 inputs are needed (image + audio). The waveform is generated
/// in real-time by FFmpeg's `showwaves` filter and overlaid on the image
/// via `colorkey` transparency.
async fn compose_video(
    image_path: &std::path::Path,
    track_path: &std::path::Path,
    output_path: &std::path::Path,
    duration_secs: u32,
    waveform_color: &str,
) -> Result<()> {
    let image_str = image_path
        .to_str()
        .ok_or_else(|| AppError::Internal("Invalid image path".to_string()))?;
    let track_str = track_path
        .to_str()
        .ok_or_else(|| AppError::Internal("Invalid track path".to_string()))?;
    let output_str = output_path
        .to_str()
        .ok_or_else(|| AppError::Internal("Invalid output path".to_string()))?;

    let duration_str = duration_secs.to_string();
    let fps_str = VIDEO_FPS.to_string();
    let filter_complex = build_filter_complex(duration_secs, waveform_color);

    tracing::info!(
        "Running FFmpeg video composition ({}s, waveform={})",
        duration_secs,
        waveform_color
    );

    let output = Command::new("ffmpeg")
        .args([
            // Input 0: artist image, looped
            "-loop",
            "1",
            "-framerate",
            &fps_str,
            "-i",
            image_str,
            // Input 1: audio track (first N seconds)
            "-t",
            &duration_str,
            "-i",
            track_str,
            // Duration limit
            "-t",
            &duration_str,
            // Filter
            "-filter_complex",
            &filter_complex,
            // Map outputs
            "-map",
            "[out]",
            "-map",
            "1:a",
            // Video codec
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-r",
            &fps_str,
            // Audio codec
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "44100",
            // Web/Instagram compatibility
            "-movflags",
            "+faststart",
            // Overwrite
            "-y",
            output_str,
        ])
        .output()
        .await
        .map_err(|e| {
            AppError::Internal(format!("Failed to run ffmpeg (is it installed?): {}", e))
        })?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        tracing::error!("FFmpeg video composition failed:\n{}", stderr);
        return Err(AppError::Internal(format!(
            "Video generation failed: {}",
            stderr.lines().last().unwrap_or("Unknown error")
        )));
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    if !stdout.is_empty() {
        tracing::debug!("FFmpeg stdout: {}", stdout);
    }

    Ok(())
}

// ────────────────────────────────────────────────────────────────────────────
// R2 upload helper
// ────────────────────────────────────────────────────────────────────────────

/// Upload raw MP4 bytes to R2 at a specific key.
async fn upload_video_to_r2(state: &Arc<AppState>, key: &str, data: Vec<u8>) -> Result<()> {
    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(key)
        .body(ByteStream::from(data))
        .content_type("video/mp4")
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload video to R2: {}", e)))?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_constants_are_consistent() {
        // Waveform fits below the artist image
        assert!(WAVEFORM_Y_OFFSET + WAVEFORM_HEIGHT <= VIDEO_HEIGHT);
        assert!(WAVEFORM_WIDTH == VIDEO_WIDTH);
    }

    #[test]
    fn test_filter_complex_white_waveform() {
        let filter = build_filter_complex(30, "white");

        // Must contain showwaves with our configured mode and scale
        assert!(filter.contains("showwaves"));
        assert!(filter.contains(&format!("mode={}", WAVEFORM_MODE)));
        assert!(filter.contains("colors=white"));
        assert!(filter.contains(&format!("scale={}", WAVEFORM_SCALE)));

        // White waveform on black bg → colorkey black
        assert!(filter.contains("colorkey=black"));
        assert!(!filter.contains("negate"));

        // Must contain overlay positioning
        assert!(filter.contains(&format!("overlay=(W-w)/2:{}", WAVEFORM_Y_OFFSET)));
        assert!(filter.contains("[out]"));
    }

    #[test]
    fn test_filter_complex_black_waveform() {
        let filter = build_filter_complex(30, "black");

        // Black waveform: showwaves still renders white, then negate → black on white
        assert!(filter.contains("colors=white"));
        assert!(filter.contains("negate"));
        assert!(filter.contains("colorkey=white"));

        assert!(filter.contains(&format!("overlay=(W-w)/2:{}", WAVEFORM_Y_OFFSET)));
        assert!(filter.contains("[out]"));
    }

    #[test]
    fn test_brightness_analysis() {
        // A fully black 2×2 image should return 0
        let black_img = image::RgbaImage::from_pixel(2, 2, image::Rgba([0, 0, 0, 255]));
        let mut buf = std::io::Cursor::new(Vec::new());
        black_img
            .write_to(&mut buf, image::ImageFormat::Png)
            .unwrap();
        assert_eq!(analyze_bottom_brightness(buf.get_ref()), 0);

        // A fully white 2×2 image should return 255
        let white_img = image::RgbaImage::from_pixel(2, 2, image::Rgba([255, 255, 255, 255]));
        let mut buf2 = std::io::Cursor::new(Vec::new());
        white_img
            .write_to(&mut buf2, image::ImageFormat::Png)
            .unwrap();
        assert_eq!(analyze_bottom_brightness(buf2.get_ref()), 255);
    }

    #[test]
    fn test_choose_waveform_color_logic() {
        // Dark image → white waveform
        let dark_img = image::RgbaImage::from_pixel(4, 4, image::Rgba([30, 30, 30, 255]));
        let mut buf = std::io::Cursor::new(Vec::new());
        dark_img
            .write_to(&mut buf, image::ImageFormat::Png)
            .unwrap();
        assert_eq!(choose_waveform_color(buf.get_ref()), "white");

        // Bright image → black waveform
        let bright_img = image::RgbaImage::from_pixel(4, 4, image::Rgba([220, 220, 220, 255]));
        let mut buf2 = std::io::Cursor::new(Vec::new());
        bright_img
            .write_to(&mut buf2, image::ImageFormat::Png)
            .unwrap();
        assert_eq!(choose_waveform_color(buf2.get_ref()), "black");
    }
}
