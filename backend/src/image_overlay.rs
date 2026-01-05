use crate::AppState;
use ab_glyph::{FontArc, PxScale};
use image::{codecs::jpeg::JpegEncoder, codecs::png::PngEncoder, ColorType, ImageEncoder};
use image::{DynamicImage, ImageFormat, Rgba};
use imageproc::drawing::{draw_text_mut, text_size};
use std::sync::Arc;

struct OverlayFonts {
    un: FontArc,
    heard: FontArc,
    name: FontArc,
}

fn crop_square_rgba(img: &DynamicImage) -> image::RgbaImage {
    let rgba = img.to_rgba8();
    let (w, h) = rgba.dimensions();
    let side = w.min(h).max(1);
    let x = (w - side) / 2;
    let y = (h - side) / 2;
    image::imageops::crop_imm(&rgba, x, y, side, side).to_image()
}

fn make_show_collage_sync(
    items: &[(String, Vec<u8>, String)],
    fonts: Option<&OverlayFonts>,
) -> Option<Vec<u8>> {
    // 2x2 grid. Fill missing tiles with black.
    let tile: u32 = 1024;
    let canvas: u32 = tile * 2;
    let mut out = image::RgbaImage::from_pixel(canvas, canvas, Rgba([0, 0, 0, 255]));

    let fonts = fonts;

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

        if let Some(fonts) = fonts {
            let name = artist_name.trim();
            if !name.is_empty() {
                let name = name.to_uppercase();
                let max_w = (tile as f32 * 0.86) as u32;
                // Artist names should be readable but not dominate the tile.
                let mut size = (tile as f32 * 0.06).clamp(14.0, 90.0);

                for _ in 0..12 {
                    let scale = PxScale::from(size);
                    let (tw, _th) = text_size(scale, &fonts.name, &name);
                    if tw <= max_w {
                        break;
                    }
                    size *= 0.92;
                    if size < 14.0 {
                        break;
                    }
                }

                let scale = PxScale::from(size);
                let (tw, th) = text_size(scale, &fonts.name, &name);
                let cx = x0 as i32 + (tile as i32 / 2);
                let cy = y0 as i32 + (tile as i32 / 2);
                let tx = cx - (tw as i32 / 2);
                let ty = cy - (th as i32 / 2);
                draw_text_mut(
                    &mut out,
                    Rgba([255, 255, 255, 255]),
                    tx,
                    ty,
                    scale,
                    &fonts.name,
                    &name,
                );
            }
        }
    }

    // Center UN / HEARD
    if let Some(fonts) = fonts {
        let label_un = "UN";
        let label_heard = "HEARD";

        let line_gap = (canvas as f32 * 0.015).round().clamp(6.0, 32.0) as i32;
        // HEARD a bit smaller; UN much larger (roughly matching the frontend ratio).
        let mut heard_size = (canvas as f32 * 0.105).clamp(34.0, 190.0);

        // Ensure HEARD fits comfortably within center region.
        let max_heard_w = (canvas as f32 * 0.70) as u32;
        for _ in 0..12 {
            let heard_scale = PxScale::from(heard_size);
            let (hw, _hh) = text_size(heard_scale, &fonts.heard, label_heard);
            if hw <= max_heard_w {
                break;
            }
            heard_size *= 0.92;
        }

        let heard_scale = PxScale::from(heard_size);
        let (heard_w, heard_h) = text_size(heard_scale, &fonts.heard, label_heard);

        // Scale UN to match HEARD's rendered width.
        // Since "UN" is shorter, matching width naturally makes it much larger.
        let mut un_scale = PxScale::from(heard_size);
        let (un_w0, _un_h0) = text_size(un_scale, &fonts.un, label_un);
        if un_w0 > 0 {
            let factor = heard_w as f32 / un_w0 as f32;
            un_scale = PxScale::from((un_scale.y * factor).clamp(40.0, 420.0));
        }
        let (un_w, un_h) = text_size(un_scale, &fonts.un, label_un);

        let block_h = un_h as i32 + line_gap + heard_h as i32;
        let cx = canvas as i32 / 2;
        let cy = canvas as i32 / 2;
        let top = cy - block_h / 2;

        // Draw UN (yellow) (match frontend overlay: no outline).
        let un_x = cx - (un_w as i32 / 2);
        let un_y = top;
        draw_text_mut(
            &mut out,
            Rgba([255, 236, 68, 255]),
            un_x,
            un_y,
            un_scale,
            &fonts.un,
            label_un,
        );

        // Draw HEARD (white) (match frontend overlay: no outline).
        let heard_x = cx - (heard_w as i32 / 2);
        let heard_y = top + un_h as i32 + line_gap;
        draw_text_mut(
            &mut out,
            Rgba([255, 255, 255, 255]),
            heard_x,
            heard_y,
            heard_scale,
            &fonts.heard,
            label_heard,
        );
    }

    encode_image(&DynamicImage::ImageRgba8(out), "png")
}

pub async fn build_show_collage(
    _state: &Arc<AppState>,
    mut items: Vec<(String, Vec<u8>, String)>,
) -> Option<Vec<u8>> {
    // Only up to 4 tiles.
    if items.len() > 4 {
        items.truncate(4);
    }

    let items_clone = items.clone();

    // Use repo-provided Shoika fonts (match frontend overlay).
    // (We intentionally do not rely on system fonts.)

    tokio::task::spawn_blocking(move || {
        let fonts = load_overlay_fonts();
        make_show_collage_sync(&items_clone, fonts.as_ref())
    })
    .await
    .ok()
    .flatten()
}

fn load_overlay_fonts() -> Option<OverlayFonts> {
    let un = std::fs::read("./assets/fonts/Shoika-font/Shoika Bold Italic.ttf")
        .ok()
        .and_then(|b| FontArc::try_from_vec(b).ok())?;
    let heard = std::fs::read("./assets/fonts/Shoika-font/Shoika Regular Italic.ttf")
        .ok()
        .and_then(|b| FontArc::try_from_vec(b).ok())?;
    let name = std::fs::read("./assets/fonts/Shoika-font/Shoika Bold.ttf")
        .ok()
        .and_then(|b| FontArc::try_from_vec(b).ok())?;

    Some(OverlayFonts { un, heard, name })
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
