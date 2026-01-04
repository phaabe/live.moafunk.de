use crate::AppState;
use ab_glyph::{FontArc, PxScale};
use image::{codecs::jpeg::JpegEncoder, codecs::png::PngEncoder, ColorType, ImageEncoder};
use image::{DynamicImage, ImageFormat, Rgba};
use imageproc::drawing::{draw_text_mut, text_size};
use std::path::{Path, PathBuf};
use std::sync::Arc;

fn crop_square_rgba(img: &DynamicImage) -> image::RgbaImage {
    let rgba = img.to_rgba8();
    let (w, h) = rgba.dimensions();
    let side = w.min(h).max(1);
    let x = (w - side) / 2;
    let y = (h - side) / 2;
    image::imageops::crop_imm(&rgba, x, y, side, side).to_image()
}

fn blend_text_with_outline(
    dst: &mut image::RgbaImage,
    font: &FontArc,
    scale: PxScale,
    text: &str,
    x: i32,
    y: i32,
) {
    let outline = render_text_image(font, scale, Rgba([0, 0, 0, 200]), text);
    let fill = render_text_image(font, scale, Rgba([255, 255, 255, 255]), text);

    let ox = x.max(0) as u32;
    let oy = y.max(0) as u32;

    // Subtle outline/shadow for readability.
    let offsets: &[(i32, i32)] = &[(-2, 0), (2, 0), (0, -2), (0, 2)];
    for (dx, dy) in offsets {
        alpha_blend(
            dst,
            &outline,
            (x + dx).max(0) as u32,
            (y + dy).max(0) as u32,
        );
    }
    alpha_blend(dst, &fill, ox, oy);
}

fn make_show_collage_sync(
    items: &[(String, Vec<u8>, String)],
    font_bytes: Option<&[u8]>,
) -> Option<Vec<u8>> {
    // 2x2 grid. Fill missing tiles with black.
    let tile: u32 = 1024;
    let canvas: u32 = tile * 2;
    let mut out = image::RgbaImage::from_pixel(canvas, canvas, Rgba([0, 0, 0, 255]));

    let font = font_bytes.and_then(|b| FontArc::try_from_vec(b.to_vec()).ok());

    for idx in 0..4usize {
        let x0 = if idx % 2 == 0 { 0 } else { tile };
        let y0 = if idx < 2 { 0 } else { tile };

        let Some((artist_name, data, ext)) = items.get(idx) else {
            continue;
        };

        let img = decode_image(data, ext)?;
        let cropped = crop_square_rgba(&img);
        let resized =
            image::imageops::resize(&cropped, tile, tile, image::imageops::FilterType::Lanczos3);
        alpha_blend(&mut out, &resized, x0, y0);

        if let Some(ref font) = font {
            let name = artist_name.trim();
            if !name.is_empty() {
                let name = name.to_uppercase();
                let max_w = (tile as f32 * 0.86) as u32;
                let mut size = (tile as f32 * 0.085).clamp(18.0, 120.0);

                for _ in 0..12 {
                    let scale = PxScale::from(size);
                    let (tw, _th) = text_size(scale, font, &name);
                    if tw <= max_w {
                        break;
                    }
                    size *= 0.92;
                    if size < 14.0 {
                        break;
                    }
                }

                let scale = PxScale::from(size);
                let (tw, th) = text_size(scale, font, &name);
                let cx = x0 as i32 + (tile as i32 / 2);
                let cy = y0 as i32 + (tile as i32 / 2);
                let tx = cx - (tw as i32 / 2);
                let ty = cy - (th as i32 / 2);
                blend_text_with_outline(&mut out, font, scale, &name, tx, ty);
            }
        }
    }

    // Center UN / HEARD
    if let Some(ref font) = font {
        let label_un = "UN";
        let label_heard = "HEARD";

        let line_gap = (canvas as f32 * 0.015).round().clamp(6.0, 32.0) as i32;
        let mut heard_size = (canvas as f32 * 0.13).clamp(40.0, 220.0);

        // Ensure HEARD fits comfortably within center region.
        let max_heard_w = (canvas as f32 * 0.70) as u32;
        for _ in 0..12 {
            let heard_scale = PxScale::from(heard_size);
            let (hw, _hh) = text_size(heard_scale, font, label_heard);
            if hw <= max_heard_w {
                break;
            }
            heard_size *= 0.92;
        }

        let heard_scale = PxScale::from(heard_size);
        let (heard_w, heard_h) = text_size(heard_scale, font, label_heard);

        // Scale UN to match HEARD width.
        let mut un_scale = PxScale::from(heard_size);
        let (un_w0, _un_h0) = text_size(un_scale, font, label_un);
        if un_w0 > 0 {
            let factor = heard_w as f32 / un_w0 as f32;
            un_scale = PxScale::from((un_scale.y * factor).clamp(20.0, 260.0));
        }
        let (un_w, un_h) = text_size(un_scale, font, label_un);

        let block_h = un_h as i32 + line_gap + heard_h as i32;
        let cx = canvas as i32 / 2;
        let cy = canvas as i32 / 2;
        let top = cy - block_h / 2;

        // Draw UN (yellow) with outline.
        let un_x = cx - (un_w as i32 / 2);
        let un_y = top;
        let un_outline = render_text_image(font, un_scale, Rgba([0, 0, 0, 200]), label_un);
        let un_fill = render_text_image(font, un_scale, Rgba([255, 236, 68, 255]), label_un);
        for (dx, dy) in [(-3, 0), (3, 0), (0, -3), (0, 3)] {
            alpha_blend(
                &mut out,
                &un_outline,
                (un_x + dx).max(0) as u32,
                (un_y + dy).max(0) as u32,
            );
        }
        alpha_blend(&mut out, &un_fill, un_x.max(0) as u32, un_y.max(0) as u32);

        // Draw HEARD (white) with outline.
        let heard_x = cx - (heard_w as i32 / 2);
        let heard_y = top + un_h as i32 + line_gap;
        blend_text_with_outline(&mut out, font, heard_scale, label_heard, heard_x, heard_y);
    }

    encode_image(&DynamicImage::ImageRgba8(out), "png")
}

pub async fn build_show_collage(
    state: &Arc<AppState>,
    mut items: Vec<(String, Vec<u8>, String)>,
) -> Option<Vec<u8>> {
    // Only up to 4 tiles.
    if items.len() > 4 {
        items.truncate(4);
    }

    let font_override = state.config.overlay_font_path_path().map(|s| s.to_string());

    let items_clone = items.clone();

    tokio::task::spawn_blocking(move || {
        let font_bytes = load_font_bytes(font_override.as_deref());
        make_show_collage_sync(&items_clone, font_bytes.as_deref())
    })
    .await
    .ok()
    .flatten()
}

fn sanitize_filename(name: &str) -> String {
    name.chars()
        .map(|c| match c {
            '<' | '>' | ':' | '"' | '/' | '\\' | '|' | '?' | '*' => '_',
            _ => c,
        })
        .collect::<String>()
        .trim()
        .to_string()
}

fn candidate_font_paths() -> &'static [&'static str] {
    &[
        // Repo-provided Shoika (always prefer and only use Shoika)
        "./assets/fonts/Shoika-font/Shoika Semi Bold Italic.ttf",
        "./assets/fonts/Shoika-font/Shoika Bold Italic.ttf",
        "./assets/fonts/Shoika-font/Shoika Regular Italic.ttf",
    ]
}

fn load_font_bytes(font_path_override: Option<&str>) -> Option<Vec<u8>> {
    if let Some(p) = font_path_override {
        if let Ok(bytes) = std::fs::read(p) {
            return Some(bytes);
        }
    }

    for p in candidate_font_paths() {
        if let Ok(bytes) = std::fs::read(p) {
            return Some(bytes);
        }
    }

    None
}

fn render_text_image(
    font: &FontArc,
    scale: PxScale,
    color: Rgba<u8>,
    text: &str,
) -> image::RgbaImage {
    // Supersample for sharper edges, then downscale.
    let ss: f32 = 2.0;

    let (tw, th) = text_size(scale, font, text);
    let pad = 6u32;
    let target_w = (tw + pad * 2).max(1);
    let target_h = (th + pad * 2).max(1);

    let hi_scale = PxScale::from((scale.y * ss).max(1.0));
    let (hi_tw, hi_th) = text_size(hi_scale, font, text);
    let hi_pad = ((pad as f32) * ss).round().max(1.0) as u32;
    let hi_w = (hi_tw + hi_pad * 2).max(1);
    let hi_h = (hi_th + hi_pad * 2).max(1);

    let mut hi = image::RgbaImage::from_pixel(hi_w, hi_h, Rgba([0, 0, 0, 0]));
    draw_text_mut(
        &mut hi,
        color,
        hi_pad as i32,
        hi_pad as i32,
        hi_scale,
        font,
        text,
    );

    if hi_w == target_w && hi_h == target_h {
        return hi;
    }

    image::imageops::resize(
        &hi,
        target_w,
        target_h,
        image::imageops::FilterType::Lanczos3,
    )
}

fn try_read_file(path: &Path) -> Option<Vec<u8>> {
    std::fs::read(path).ok()
}

fn pick_artist_logo_bytes(
    artist_logo_dir: &str,
    artist_id: i64,
    artist_name: &str,
) -> Option<Vec<u8>> {
    let dir = PathBuf::from(artist_logo_dir);

    let by_id = dir.join(format!("{}.png", artist_id));
    if let Some(bytes) = try_read_file(&by_id) {
        return Some(bytes);
    }

    let by_name = dir.join(format!("{}.png", sanitize_filename(artist_name)));
    if let Some(bytes) = try_read_file(&by_name) {
        return Some(bytes);
    }

    None
}

fn decode_image(data: &[u8], ext: &str) -> Option<DynamicImage> {
    let ext = ext.to_ascii_lowercase();
    let format = match ext.as_str() {
        "jpg" | "jpeg" => ImageFormat::Jpeg,
        "png" => ImageFormat::Png,
        // Best-effort fallback
        _ => ImageFormat::Jpeg,
    };

    image::load_from_memory_with_format(data, format)
        .or_else(|_| image::load_from_memory(data))
        .ok()
}

fn encode_image(img: &DynamicImage, ext: &str) -> Option<Vec<u8>> {
    let ext = ext.to_ascii_lowercase();
    let mut out = Vec::new();

    match ext.as_str() {
        "png" => {
            let rgba = img.to_rgba8();
            let encoder = PngEncoder::new(&mut out);
            encoder
                .write_image(
                    rgba.as_raw(),
                    rgba.width(),
                    rgba.height(),
                    ColorType::Rgba8.into(),
                )
                .ok()?;
            Some(out)
        }
        "jpg" | "jpeg" => {
            let rgb = img.to_rgb8();
            let encoder = JpegEncoder::new_with_quality(&mut out, 92);
            encoder
                .write_image(
                    rgb.as_raw(),
                    rgb.width(),
                    rgb.height(),
                    ColorType::Rgb8.into(),
                )
                .ok()?;
            Some(out)
        }
        _ => {
            // Default to png for unknown formats
            let rgba = img.to_rgba8();
            let encoder = PngEncoder::new(&mut out);
            encoder
                .write_image(
                    rgba.as_raw(),
                    rgba.width(),
                    rgba.height(),
                    ColorType::Rgba8.into(),
                )
                .ok()?;
            Some(out)
        }
    }
}

fn alpha_blend(dst: &mut image::RgbaImage, src: &image::RgbaImage, x0: u32, y0: u32) {
    let (dw, dh) = dst.dimensions();
    let (sw, sh) = src.dimensions();

    for y in 0..sh {
        let dy = y0 + y;
        if dy >= dh {
            continue;
        }
        for x in 0..sw {
            let dx = x0 + x;
            if dx >= dw {
                continue;
            }

            let sp = src.get_pixel(x, y);
            let sa = sp[3] as f32 / 255.0;
            if sa <= 0.0 {
                continue;
            }

            let dp = dst.get_pixel(dx, dy);
            let da = dp[3] as f32 / 255.0;

            let out_a = sa + da * (1.0 - sa);
            if out_a <= 0.0 {
                continue;
            }

            let blend_channel = |sc: u8, dc: u8| -> u8 {
                let sc = sc as f32 / 255.0;
                let dc = dc as f32 / 255.0;
                let out = (sc * sa + dc * da * (1.0 - sa)) / out_a;
                (out.clamp(0.0, 1.0) * 255.0).round() as u8
            };

            let out = Rgba([
                blend_channel(sp[0], dp[0]),
                blend_channel(sp[1], dp[1]),
                blend_channel(sp[2], dp[2]),
                (out_a.clamp(0.0, 1.0) * 255.0).round() as u8,
            ]);

            dst.put_pixel(dx, dy, out);
        }
    }
}

fn stamp_artist_picture_sync(
    original: &[u8],
    ext: &str,
    artist_name: &str,
    logo_bytes: Option<&[u8]>,
    font_bytes: Option<&[u8]>,
) -> Option<Vec<u8>> {
    let mut base = decode_image(original, ext)?.to_rgba8();
    let (w, h) = base.dimensions();

    let padding = (w.min(h) as f32 * 0.04).round().clamp(10.0, 56.0) as i32;
    let gap = (w.min(h) as f32 * 0.03).round().clamp(14.0, 44.0) as i32;
    let logo_side_gap = ((gap as f32) * 1.25).round() as i32;

    // Logo (optional). Center aligned at bottom.
    let mut logo_resized: Option<image::RgbaImage> = None;
    let mut logo_w: u32 = 0;
    let mut logo_h: u32 = 0;

    if let Some(bytes) = logo_bytes {
        if let Some(logo_img) = decode_image(bytes, "png") {
            let logo = logo_img.to_rgba8();
            let (lw, lh) = logo.dimensions();
            if lw > 0 && lh > 0 {
                let max_logo_h = (h as f32 * 0.16).round().clamp(40.0, 180.0) as u32;
                let max_logo_w = (w as f32 * 0.22).round().clamp(40.0, 260.0) as u32;

                let scale_h = max_logo_h as f32 / lh as f32;
                let scale_w = max_logo_w as f32 / lw as f32;
                let scale = scale_h.min(scale_w).min(1.0);

                logo_w = ((lw as f32) * scale).round().max(1.0) as u32;
                logo_h = ((lh as f32) * scale).round().max(1.0) as u32;

                logo_resized = Some(image::imageops::resize(
                    &logo,
                    logo_w,
                    logo_h,
                    image::imageops::FilterType::Lanczos3,
                ));
            }
        }
    }

    // Text requires font. We only support Shoika (loaded from repo assets or explicit override).
    let font = font_bytes.and_then(|b| FontArc::try_from_vec(b.to_vec()).ok());

    // Layout strings (all caps)
    let label_un = "UN";
    let label_heard = "HEARD";
    let artist = artist_name.trim().to_uppercase();

    // If we have no logo and no text to draw, bail.
    if logo_resized.is_none() && (font.is_none() || artist.is_empty()) {
        return Some(original.to_vec());
    }

    // Determine baseline sizes from logo height (or image height as fallback).
    let base_h = if logo_h > 0 {
        logo_h
    } else {
        (h as f32 * 0.12).round().max(40.0) as u32
    };

    // Make HEARD slightly smaller/narrower than the previous version.
    let mut heard_size = (base_h as f32 * 0.52).round().clamp(18.0, 120.0);
    let mut artist_size = (base_h as f32 * 0.40).round().clamp(16.0, 110.0);
    let heard_squeeze_x: f32 = 0.86;

    // Iteratively shrink to fit available width.
    for _ in 0..10 {
        let mut left_block_w: u32 = 0;
        let mut artist_w: u32 = 0;

        if let Some(ref font) = font {
            let heard_scale = PxScale::from(heard_size);
            let (heard_w, _) = text_size(heard_scale, font, label_heard);
            let heard_w = ((heard_w as f32) * heard_squeeze_x).round().max(1.0) as u32;

            // Scale UN so its rendered width matches HEARD.
            let mut un_size = heard_size;
            let mut un_scale = PxScale::from(un_size);
            let (mut un_w, mut _un_h) = text_size(un_scale, font, label_un);
            if un_w > 0 {
                let factor = heard_w as f32 / un_w as f32;
                un_size = (un_size * factor).clamp(12.0, 200.0);
                un_scale = PxScale::from(un_size);
                (un_w, _un_h) = text_size(un_scale, font, label_un);
            }

            left_block_w = heard_w.max(un_w);

            if !artist.is_empty() {
                let artist_scale = PxScale::from(artist_size);
                (artist_w, _) = text_size(artist_scale, font, &artist);
            }
        }

        let total_w =
            left_block_w as i32 + logo_side_gap + logo_w as i32 + logo_side_gap + artist_w as i32;
        let avail_w = w as i32 - 2 * padding;

        if total_w <= avail_w {
            break;
        }

        heard_size *= 0.92;
        artist_size *= 0.92;

        if heard_size < 14.0 {
            break;
        }
    }

    // Place logo centered at bottom.
    let logo_x = (w as i32 / 2).max(0);
    let logo_left = logo_x - (logo_w as i32 / 2);
    let logo_top = (h as i32 - padding - logo_h as i32).max(0);

    if let Some(logo_img) = logo_resized.as_ref() {
        alpha_blend(
            &mut base,
            logo_img,
            logo_left.max(0) as u32,
            logo_top.max(0) as u32,
        );
    }

    // Draw text blocks around logo.
    if let Some(ref font) = font {
        // Build UN/HEARD so their combined height matches the logo height.
        let mut heard_scale = PxScale::from(heard_size);
        let (heard_w0, heard_h0) = text_size(heard_scale, font, label_heard);
        let heard_w0 = ((heard_w0 as f32) * heard_squeeze_x).round().max(1.0) as u32;

        // UN scale adjusted to match (squeezed) HEARD width.
        let mut un_size = heard_size;
        let mut un_scale = PxScale::from(un_size);
        let (un_w0, mut un_h0) = text_size(un_scale, font, label_un);
        if un_w0 > 0 {
            let factor = heard_w0 as f32 / un_w0 as f32;
            un_size = (un_size * factor).clamp(12.0, 220.0);
            un_scale = PxScale::from(un_size);
            (_, un_h0) = text_size(un_scale, font, label_un);
        }

        // Gap between lines as a small fraction of logo height.
        let line_gap = ((base_h as f32) * 0.06).round().clamp(2.0, 18.0) as i32;
        let total_h0 = un_h0 as i32 + line_gap + heard_h0 as i32;
        let target_h = (if logo_h > 0 { logo_h } else { base_h }) as i32;
        if total_h0 > 0 {
            let k = (target_h as f32 / total_h0 as f32).clamp(0.5, 2.5);
            heard_scale = PxScale::from((heard_size * k).clamp(12.0, 240.0));
            let (heard_w1, _) = text_size(heard_scale, font, label_heard);
            let heard_w1 = ((heard_w1 as f32) * heard_squeeze_x).round().max(1.0) as u32;

            // Recompute UN scale to match width again after resizing.
            un_scale = PxScale::from((un_size * k).clamp(12.0, 240.0));
            let (un_w1, _) = text_size(un_scale, font, label_un);
            if un_w1 > 0 {
                let factor = heard_w1 as f32 / un_w1 as f32;
                let un_px = (un_scale.y * factor).clamp(12.0, 260.0);
                un_scale = PxScale::from(un_px);
            }

            // Render images for precise placement (and HEARD horizontal squeeze).
            let un_img = render_text_image(font, un_scale, Rgba([255, 220, 0, 255]), label_un);
            let heard_img_raw =
                render_text_image(font, heard_scale, Rgba([255, 255, 255, 255]), label_heard);
            let heard_w = ((heard_img_raw.width() as f32) * heard_squeeze_x)
                .round()
                .max(1.0) as u32;
            let heard_h = heard_img_raw.height();
            let heard_img = image::imageops::resize(
                &heard_img_raw,
                heard_w,
                heard_h,
                image::imageops::FilterType::Lanczos3,
            );

            let left_block_w = un_img.width().max(heard_img.width()) as i32;
            let left_block_h = un_img.height() as i32 + line_gap + heard_img.height() as i32;

            // Vertically center left block + artist name to logo.
            let center_y = logo_top + (logo_h as i32 / 2);
            let left_top = (center_y - left_block_h / 2).max(0);

            // Left block placed immediately left of logo.
            let left_right_edge = logo_left - logo_side_gap;
            let left_x = (left_right_edge - left_block_w).max(padding);

            let un_x = left_x + ((left_block_w - un_img.width() as i32) / 2);
            let un_y = left_top;
            let heard_x = left_x + ((left_block_w - heard_img.width() as i32) / 2);
            let heard_y = left_top + un_img.height() as i32 + line_gap;

            alpha_blend(&mut base, &un_img, un_x.max(0) as u32, un_y.max(0) as u32);
            alpha_blend(
                &mut base,
                &heard_img,
                heard_x.max(0) as u32,
                heard_y.max(0) as u32,
            );

            // Artist name to the right of the logo (no shadow).
            if !artist.is_empty() {
                let artist_scale = PxScale::from(artist_size);
                let artist_img =
                    render_text_image(font, artist_scale, Rgba([255, 255, 255, 255]), &artist);
                let artist_left = logo_left + logo_w as i32 + logo_side_gap;
                let artist_y = (center_y - artist_img.height() as i32 / 2).max(0);
                let artist_x = artist_left.max(padding);
                alpha_blend(
                    &mut base,
                    &artist_img,
                    artist_x.max(0) as u32,
                    artist_y.max(0) as u32,
                );
            }
        }
    }

    encode_image(&DynamicImage::ImageRgba8(base), ext)
}

pub async fn stamp_artist_picture(
    state: &Arc<AppState>,
    artist_id: i64,
    artist_name: &str,
    ext: &str,
    original: Vec<u8>,
) -> Vec<u8> {
    let artist_logo_dir = state.config.artist_logo_dir_path().to_string();
    let default_logo_path = state.config.default_logo_path_path().to_string();
    let font_override = state.config.overlay_font_path_path().map(|s| s.to_string());

    let artist_name_owned = artist_name.to_string();
    let ext_owned = ext.to_string();

    let original_fallback = original.clone();

    tokio::task::spawn_blocking(move || {
        let logo = pick_artist_logo_bytes(&artist_logo_dir, artist_id, &artist_name_owned)
            .or_else(|| try_read_file(Path::new(&default_logo_path)));

        let font_bytes = load_font_bytes(font_override.as_deref());

        match stamp_artist_picture_sync(
            &original,
            &ext_owned,
            &artist_name_owned,
            logo.as_deref(),
            font_bytes.as_deref(),
        ) {
            Some(stamped) => stamped,
            None => original,
        }
    })
    .await
    .unwrap_or(original_fallback)
}
