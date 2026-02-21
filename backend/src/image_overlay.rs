use crate::AppState;
use ab_glyph::{FontArc, PxScale};
use image::{codecs::jpeg::JpegEncoder, codecs::png::PngEncoder, ColorType, ImageEncoder};
use image::{DynamicImage, ImageFormat, Rgba, RgbaImage};
use imageproc::drawing::{draw_text_mut, text_size};
use serde::Deserialize;
use std::collections::HashMap;
use std::sync::Arc;

// ---------------------------------------------------------------------------
// Overlay preset params (mirror frontend TS types, camelCase for JSON compat)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct OverlayShadowParams {
    pub offset_x: f64,
    pub offset_y: f64,
    pub color: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct OverlayElementParams {
    pub visible: bool,
    pub x: f64,
    pub y: f64,
    pub size: f64,
    pub color: String,
    #[serde(default)]
    pub font_weight: Option<String>,
    #[serde(default)]
    pub font_style: Option<String>,
    #[serde(default)]
    pub shadow: Option<OverlayShadowParams>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct OverlayFilterParams {
    #[serde(default = "default_one")]
    pub brightness: f64,
    #[serde(default = "default_one")]
    pub contrast: f64,
    #[serde(default = "default_one")]
    pub saturate: f64,
    #[serde(default)]
    pub hue_rotate: f64,
    #[serde(default)]
    pub grayscale: f64,
    #[serde(default)]
    pub sepia: f64,
    #[serde(default)]
    pub blur: f64,
}

fn default_one() -> f64 {
    1.0
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct OverlayParams {
    pub un: OverlayElementParams,
    pub heard: OverlayElementParams,
    pub logo: OverlayElementParams,
    pub artist_name: OverlayElementParams,
    pub filter: OverlayFilterParams,
    /// Per-tile name colours for show overlays (up to 4 hex strings).
    #[serde(default)]
    pub tile_colors: Option<Vec<String>>,
    /// Per-tile shadow colours for show overlays (up to 4 hex strings).
    #[serde(default)]
    pub tile_shadow_colors: Option<Vec<String>>,
}

// ---------------------------------------------------------------------------
// Font system — original (for legacy collage) + expanded (for overlay presets)
// ---------------------------------------------------------------------------

struct OverlayFonts {
    un: FontArc,
    heard: FontArc,
    name: FontArc,
}

/// Font key: (weight, style) where weight is "400"|"600"|"700" and style is "normal"|"italic"
type FontKey = (String, String);

pub struct ExpandedFonts {
    map: HashMap<FontKey, FontArc>,
}

impl ExpandedFonts {
    /// Resolve a font by (fontWeight, fontStyle). Falls back through closest matches.
    pub fn resolve(&self, weight: &str, style: &str) -> Option<&FontArc> {
        // Exact match
        let key = (weight.to_string(), style.to_string());
        if let Some(f) = self.map.get(&key) {
            return Some(f);
        }
        // Fallback: same weight, opposite style
        let alt_style = if style == "italic" {
            "normal"
        } else {
            "italic"
        };
        let key2 = (weight.to_string(), alt_style.to_string());
        if let Some(f) = self.map.get(&key2) {
            return Some(f);
        }
        // Fallback: 700 + normal (Bold) as ultimate default
        let key3 = ("700".to_string(), "normal".to_string());
        self.map.get(&key3)
    }
}

pub fn load_expanded_fonts() -> Option<ExpandedFonts> {
    let font_dir = "./assets/fonts/Shoika-font";
    let mappings: &[(&str, &str, &str)] = &[
        ("400", "normal", "Shoika Regular.ttf"),
        ("400", "italic", "Shoika Regular Italic.ttf"),
        ("600", "normal", "Shoika Semi Bold.ttf"),
        ("600", "italic", "Shoika Bold Italic.ttf"),
        ("700", "normal", "Shoika Bold.ttf"),
        ("700", "italic", "Shoika Bold Italic.ttf"),
    ];

    let mut map = HashMap::new();
    for (weight, style, filename) in mappings {
        let path = format!("{}/{}", font_dir, filename);
        let data = std::fs::read(&path).ok()?;
        let font = FontArc::try_from_vec(data).ok()?;
        map.insert((weight.to_string(), style.to_string()), font);
    }

    Some(ExpandedFonts { map })
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

/// Build a plain 2x2 collage with NO text or branding.
/// Used as the base image for server-side overlay preset rendering.
pub async fn build_plain_collage(mut items: Vec<(Vec<u8>, String)>) -> Option<Vec<u8>> {
    if items.len() > 4 {
        items.truncate(4);
    }

    tokio::task::spawn_blocking(move || make_plain_collage_sync(&items))
        .await
        .ok()
        .flatten()
}

/// 2x2 grid of artist images — no text, no branding, no overlay.
fn make_plain_collage_sync(items: &[(Vec<u8>, String)]) -> Option<Vec<u8>> {
    let tile: u32 = 1024;
    let canvas: u32 = tile * 2;
    let mut out = image::RgbaImage::from_pixel(canvas, canvas, Rgba([0, 0, 0, 255]));

    for idx in 0..4usize {
        let x0 = if idx % 2 == 0 { 0 } else { tile };
        let y0 = if idx < 2 { 0 } else { tile };

        let Some((data, ext)) = items.get(idx) else {
            continue;
        };

        let img = decode_image(data, ext)?;
        let cropped = crop_square_rgba(&img);
        let resized =
            image::imageops::resize(&cropped, tile, tile, image::imageops::FilterType::Lanczos3);
        alpha_blend(&mut out, &resized, x0, y0);
    }

    encode_image(&DynamicImage::ImageRgba8(out), "png")
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

// ===========================================================================
// Step 5 — Server-side CSS filter emulation
// ===========================================================================

/// Clamp an f64 to 0–255 and round to u8.
fn clamp_u8(v: f64) -> u8 {
    v.round().clamp(0.0, 255.0) as u8
}

/// CSS brightness(factor): multiply each RGB channel by factor.
fn apply_brightness(img: &mut RgbaImage, factor: f64) {
    if (factor - 1.0).abs() < 1e-6 {
        return;
    }
    for p in img.pixels_mut() {
        p[0] = clamp_u8(p[0] as f64 * factor);
        p[1] = clamp_u8(p[1] as f64 * factor);
        p[2] = clamp_u8(p[2] as f64 * factor);
    }
}

/// CSS contrast(factor): (channel - 128) * factor + 128.
fn apply_contrast(img: &mut RgbaImage, factor: f64) {
    if (factor - 1.0).abs() < 1e-6 {
        return;
    }
    for p in img.pixels_mut() {
        p[0] = clamp_u8((p[0] as f64 - 128.0) * factor + 128.0);
        p[1] = clamp_u8((p[1] as f64 - 128.0) * factor + 128.0);
        p[2] = clamp_u8((p[2] as f64 - 128.0) * factor + 128.0);
    }
}

/// CSS saturate(factor): uses the SVG/CSS matrix approach.
/// At factor=0 → full desaturation, factor=1 → identity.
fn apply_saturate(img: &mut RgbaImage, factor: f64) {
    if (factor - 1.0).abs() < 1e-6 {
        return;
    }
    let s = factor;
    // CSS saturate matrix (from W3C Filter Effects spec)
    let m = [
        [
            0.2126 + 0.7874 * s,
            0.7152 - 0.7152 * s,
            0.0722 - 0.0722 * s,
        ],
        [
            0.2126 - 0.2126 * s,
            0.7152 + 0.2848 * s,
            0.0722 - 0.0722 * s,
        ],
        [
            0.2126 - 0.2126 * s,
            0.7152 - 0.7152 * s,
            0.0722 + 0.9278 * s,
        ],
    ];
    for p in img.pixels_mut() {
        let r = p[0] as f64;
        let g = p[1] as f64;
        let b = p[2] as f64;
        p[0] = clamp_u8(m[0][0] * r + m[0][1] * g + m[0][2] * b);
        p[1] = clamp_u8(m[1][0] * r + m[1][1] * g + m[1][2] * b);
        p[2] = clamp_u8(m[2][0] * r + m[2][1] * g + m[2][2] * b);
    }
}

/// CSS hue-rotate(deg): rotate the hue by `deg` degrees using the CSS spec matrix.
fn apply_hue_rotate(img: &mut RgbaImage, deg: f64) {
    if deg.abs() < 1e-6 {
        return;
    }
    let rad = deg.to_radians();
    let cos = rad.cos();
    let sin = rad.sin();
    // W3C Filter Effects hueRotate matrix
    let m = [
        [
            0.213 + cos * 0.787 - sin * 0.213,
            0.715 - cos * 0.715 - sin * 0.715,
            0.072 - cos * 0.072 + sin * 0.928,
        ],
        [
            0.213 - cos * 0.213 + sin * 0.143,
            0.715 + cos * 0.285 + sin * 0.140,
            0.072 - cos * 0.072 - sin * 0.283,
        ],
        [
            0.213 - cos * 0.213 - sin * 0.787,
            0.715 - cos * 0.715 + sin * 0.715,
            0.072 + cos * 0.928 + sin * 0.072,
        ],
    ];
    for p in img.pixels_mut() {
        let r = p[0] as f64;
        let g = p[1] as f64;
        let b = p[2] as f64;
        p[0] = clamp_u8(m[0][0] * r + m[0][1] * g + m[0][2] * b);
        p[1] = clamp_u8(m[1][0] * r + m[1][1] * g + m[1][2] * b);
        p[2] = clamp_u8(m[2][0] * r + m[2][1] * g + m[2][2] * b);
    }
}

/// CSS grayscale(amount): amount 0–1, lerp from color to luminance.
fn apply_grayscale(img: &mut RgbaImage, amount: f64) {
    if amount.abs() < 1e-6 {
        return;
    }
    let a = amount.clamp(0.0, 1.0);
    // Same matrix as saturate(1 - amount) but conceptually clearer
    let m = [
        [
            0.2126 + 0.7874 * (1.0 - a),
            0.7152 - 0.7152 * (1.0 - a),
            0.0722 - 0.0722 * (1.0 - a),
        ],
        [
            0.2126 - 0.2126 * (1.0 - a),
            0.7152 + 0.2848 * (1.0 - a),
            0.0722 - 0.0722 * (1.0 - a),
        ],
        [
            0.2126 - 0.2126 * (1.0 - a),
            0.7152 - 0.7152 * (1.0 - a),
            0.0722 + 0.9278 * (1.0 - a),
        ],
    ];
    for p in img.pixels_mut() {
        let r = p[0] as f64;
        let g = p[1] as f64;
        let b = p[2] as f64;
        p[0] = clamp_u8(m[0][0] * r + m[0][1] * g + m[0][2] * b);
        p[1] = clamp_u8(m[1][0] * r + m[1][1] * g + m[1][2] * b);
        p[2] = clamp_u8(m[2][0] * r + m[2][1] * g + m[2][2] * b);
    }
}

/// CSS sepia(amount): standard sepia matrix, lerped by amount 0–1.
fn apply_sepia(img: &mut RgbaImage, amount: f64) {
    if amount.abs() < 1e-6 {
        return;
    }
    let a = amount.clamp(0.0, 1.0);
    // W3C sepia matrix (amount=1)
    let sepia = [
        [0.393, 0.769, 0.189],
        [0.349, 0.686, 0.168],
        [0.272, 0.534, 0.131],
    ];
    for p in img.pixels_mut() {
        let r = p[0] as f64;
        let g = p[1] as f64;
        let b = p[2] as f64;
        let sr = sepia[0][0] * r + sepia[0][1] * g + sepia[0][2] * b;
        let sg = sepia[1][0] * r + sepia[1][1] * g + sepia[1][2] * b;
        let sb = sepia[2][0] * r + sepia[2][1] * g + sepia[2][2] * b;
        // Lerp between original and sepia by amount
        p[0] = clamp_u8(r + (sr - r) * a);
        p[1] = clamp_u8(g + (sg - g) * a);
        p[2] = clamp_u8(b + (sb - b) * a);
    }
}

/// CSS blur(px): Gaussian blur. Uses the `image` crate's built-in blur.
fn apply_blur(img: &mut RgbaImage, px: f64) {
    if px.abs() < 0.5 {
        return;
    }
    // CSS blur px maps roughly to Gaussian sigma
    let sigma = px as f32;
    let dyn_img = DynamicImage::ImageRgba8(img.clone());
    let blurred = dyn_img.blur(sigma);
    *img = blurred.to_rgba8();
}

/// Apply all CSS filters in spec order: brightness, contrast, saturate,
/// hue-rotate, grayscale, sepia, blur.
pub fn apply_all_filters(img: &mut RgbaImage, f: &OverlayFilterParams) {
    apply_brightness(img, f.brightness);
    apply_contrast(img, f.contrast);
    apply_saturate(img, f.saturate);
    apply_hue_rotate(img, f.hue_rotate);
    apply_grayscale(img, f.grayscale);
    apply_sepia(img, f.sepia);
    apply_blur(img, f.blur);
}

// ===========================================================================
// Step 6 — Server-side text rendering, logo, auto-fit, and overlay composer
// ===========================================================================

/// Parse a CSS hex color string (#RGB, #RRGGBB, #RRGGBBAA) into Rgba.
fn parse_hex_color(hex: &str) -> Rgba<u8> {
    let hex = hex.trim_start_matches('#');
    match hex.len() {
        3 => {
            let r = u8::from_str_radix(&hex[0..1], 16).unwrap_or(0) * 17;
            let g = u8::from_str_radix(&hex[1..2], 16).unwrap_or(0) * 17;
            let b = u8::from_str_radix(&hex[2..3], 16).unwrap_or(0) * 17;
            Rgba([r, g, b, 255])
        }
        6 => {
            let r = u8::from_str_radix(&hex[0..2], 16).unwrap_or(0);
            let g = u8::from_str_radix(&hex[2..4], 16).unwrap_or(0);
            let b = u8::from_str_radix(&hex[4..6], 16).unwrap_or(0);
            Rgba([r, g, b, 255])
        }
        8 => {
            let r = u8::from_str_radix(&hex[0..2], 16).unwrap_or(0);
            let g = u8::from_str_radix(&hex[2..4], 16).unwrap_or(0);
            let b = u8::from_str_radix(&hex[4..6], 16).unwrap_or(0);
            let a = u8::from_str_radix(&hex[6..8], 16).unwrap_or(255);
            Rgba([r, g, b, a])
        }
        _ => Rgba([255, 255, 255, 255]),
    }
}

/// Draw text at position with optional shadow — matches frontend drawText().
///
/// Frontend: x = (el.x / 100) * canvasW, y = (el.y / 100) * canvasH
///           textAlign = 'left', textBaseline = 'top'
///           Shadow drawn first at (x + offsetX, y + offsetY)
fn draw_overlay_text(
    img: &mut RgbaImage,
    text: &str,
    el: &OverlayElementParams,
    fonts: &ExpandedFonts,
    canvas_w: u32,
    canvas_h: u32,
) {
    if !el.visible || text.is_empty() {
        return;
    }

    let font_weight = el.font_weight.as_deref().unwrap_or("700");
    let font_style = el.font_style.as_deref().unwrap_or("normal");
    let font = match fonts.resolve(font_weight, font_style) {
        Some(f) => f,
        None => return,
    };

    // Frontend designs overlays at 1024×1024; scale text to actual canvas size
    let scale_factor = canvas_w as f32 / 1024.0;
    let font_size = el.size as f32 * scale_factor;
    let scale = PxScale::from(font_size);

    let x = (el.x / 100.0) * canvas_w as f64;
    let y = (el.y / 100.0) * canvas_h as f64;

    // Draw shadow first
    if let Some(shadow) = &el.shadow {
        let shadow_color = parse_hex_color(&shadow.color);
        let sx = (x + shadow.offset_x * scale_factor as f64) as i32;
        let sy = (y + shadow.offset_y * scale_factor as f64) as i32;
        draw_text_mut(img, shadow_color, sx, sy, scale, font, text);
    }

    // Draw main text
    let color = parse_hex_color(&el.color);
    draw_text_mut(img, color, x as i32, y as i32, scale, font, text);
}

/// Draw the moafunk logo, contain-fitted within a box — matches frontend drawLogo().
///
/// Frontend: centerX = (el.x / 100) * canvasW
///           centerY = (el.y / 100) * canvasH
///           boxSize = (el.size / 100) * canvasW
///           Contain-fit logo into boxSize×boxSize, centered at (centerX, centerY)
fn draw_overlay_logo(img: &mut RgbaImage, el: &OverlayElementParams, canvas_w: u32, canvas_h: u32) {
    if !el.visible {
        return;
    }

    let logo_data = match std::fs::read("./assets/brand/moafunk.png") {
        Ok(d) => d,
        Err(_) => return,
    };
    let logo_img = match image::load_from_memory_with_format(&logo_data, ImageFormat::Png) {
        Ok(i) => i,
        Err(_) => return,
    };

    let center_x = (el.x / 100.0) * canvas_w as f64;
    let center_y = (el.y / 100.0) * canvas_h as f64;
    let box_size = (el.size / 100.0) * canvas_w as f64;

    let src_w = logo_img.width() as f64;
    let src_h = logo_img.height() as f64;
    if src_w <= 0.0 || src_h <= 0.0 || box_size <= 0.0 {
        return;
    }
    let src_aspect = src_w / src_h;

    // Contain-fit
    let (draw_w, draw_h) = if src_aspect >= 1.0 {
        (box_size, box_size / src_aspect)
    } else {
        (box_size * src_aspect, box_size)
    };

    let draw_w_u32 = draw_w.round().max(1.0) as u32;
    let draw_h_u32 = draw_h.round().max(1.0) as u32;

    let resized = image::imageops::resize(
        &logo_img.to_rgba8(),
        draw_w_u32,
        draw_h_u32,
        image::imageops::FilterType::Lanczos3,
    );

    let draw_x = (center_x - draw_w / 2.0).round().max(0.0) as u32;
    let draw_y = (center_y - draw_h / 2.0).round().max(0.0) as u32;
    alpha_blend(img, &resized, draw_x, draw_y);
}

/// Draw entity name with binary-search auto-fit — matches frontend drawArtistName().
///
/// Frontend: Binary search font size (min 12, max el.size) until text width <= 35% canvas width.
///           textAlign = 'center', textBaseline = 'middle' (centered at el.x%, el.y%)
fn draw_entity_name(
    img: &mut RgbaImage,
    text: &str,
    el: &OverlayElementParams,
    fonts: &ExpandedFonts,
    canvas_w: u32,
    canvas_h: u32,
) {
    if !el.visible || text.is_empty() {
        return;
    }

    let text = text.to_uppercase();
    let font_weight = el.font_weight.as_deref().unwrap_or("700");
    let font_style = el.font_style.as_deref().unwrap_or("normal");
    let font = match fonts.resolve(font_weight, font_style) {
        Some(f) => f,
        None => return,
    };

    let center_x = (el.x / 100.0) * canvas_w as f64;
    let center_y = (el.y / 100.0) * canvas_h as f64;
    let max_width = canvas_w as f64 * 0.35;
    // Frontend designs overlays at 1024×1024; scale text to actual canvas size
    let scale_factor = canvas_w as f32 / 1024.0;
    let max_px = el.size as f32 * scale_factor;
    let min_px: f32 = 12.0 * scale_factor;

    // Binary search for best-fit font size
    let mut low = min_px;
    let mut high = max_px;
    for _ in 0..12 {
        let mid = (low + high) / 2.0;
        let scale = PxScale::from(mid);
        let (tw, _) = text_size(scale, font, &text);
        if (tw as f64) <= max_width {
            low = mid;
        } else {
            high = mid;
        }
    }
    let font_size = low.floor();
    let scale = PxScale::from(font_size);
    let (tw, th) = text_size(scale, font, &text);

    // Center text at (center_x, center_y) — matching textAlign='center', textBaseline='middle'
    let tx = (center_x - tw as f64 / 2.0).round() as i32;
    let ty = (center_y - th as f64 / 2.0).round() as i32;

    // Draw shadow first
    if let Some(shadow) = &el.shadow {
        let shadow_color = parse_hex_color(&shadow.color);
        let sx = tx + (shadow.offset_x as f32 * scale_factor) as i32;
        let sy = ty + (shadow.offset_y as f32 * scale_factor) as i32;
        draw_text_mut(img, shadow_color, sx, sy, scale, font, &text);
    }

    // Draw main text
    let color = parse_hex_color(&el.color);
    draw_text_mut(img, color, tx, ty, scale, font, &text);
}

/// Draw artist names centered in each 2×2 tile (for show overlays).
///
/// Each name is drawn centered in its quadrant. Positions are fixed:
///   tile 0: top-left, tile 1: top-right, tile 2: bottom-left, tile 3: bottom-right
fn draw_tile_names(
    img: &mut RgbaImage,
    names: &[String],
    el: &OverlayElementParams,
    fonts: &ExpandedFonts,
    canvas_w: u32,
    canvas_h: u32,
    tile_colors: Option<&[String]>,
    tile_shadow_colors: Option<&[String]>,
) {
    if !el.visible {
        return;
    }

    let font_weight = el.font_weight.as_deref().unwrap_or("700");
    let font_style = el.font_style.as_deref().unwrap_or("normal");
    let font = match fonts.resolve(font_weight, font_style) {
        Some(f) => f,
        None => return,
    };

    let tile_w = canvas_w / 2;
    let tile_h = canvas_h / 2;

    for (idx, name) in names.iter().take(4).enumerate() {
        let name = name.trim();
        if name.is_empty() {
            continue;
        }
        let name = name.to_uppercase();

        let tile_x0 = if idx % 2 == 0 { 0 } else { tile_w };
        let tile_y0 = if idx < 2 { 0 } else { tile_h };
        let center_x = (tile_x0 + tile_w / 2) as f64;
        let center_y = (tile_y0 + tile_h / 2) as f64;

        // Auto-fit: shrink font from el.size down to min until it fits in ~86% of tile width
        // Frontend designs overlays at 1024×1024; scale text to actual canvas size
        let scale_factor = canvas_w as f32 / 1024.0;
        let max_width = tile_w as f64 * 0.86;
        let max_px = el.size as f32 * scale_factor;
        let min_px: f32 = 12.0 * scale_factor;

        let mut low = min_px;
        let mut high = max_px;
        for _ in 0..12 {
            let mid = (low + high) / 2.0;
            let scale = PxScale::from(mid);
            let (tw, _) = text_size(scale, font, &name);
            if (tw as f64) <= max_width {
                low = mid;
            } else {
                high = mid;
            }
        }
        let font_size = low.floor();
        let scale = PxScale::from(font_size);
        let (tw, th) = text_size(scale, font, &name);

        let tx = (center_x - tw as f64 / 2.0).round() as i32;
        let ty = (center_y - th as f64 / 2.0).round() as i32;

        // Draw shadow first
        if let Some(shadow) = &el.shadow {
            let shadow_hex = tile_shadow_colors
                .and_then(|c| c.get(idx))
                .map(|s| s.as_str())
                .unwrap_or(&shadow.color);
            let shadow_color = parse_hex_color(shadow_hex);
            let sx = tx + (shadow.offset_x as f32 * scale_factor) as i32;
            let sy = ty + (shadow.offset_y as f32 * scale_factor) as i32;
            draw_text_mut(img, shadow_color, sx, sy, scale, font, &name);
        }

        let hex = tile_colors
            .and_then(|c| c.get(idx))
            .map(|s| s.as_str())
            .unwrap_or(&el.color);
        let color = parse_hex_color(hex);
        draw_text_mut(img, color, tx, ty, scale, font, &name);
    }
}

/// Apply a full overlay preset to an image: filters → text → logo → entity name.
///
/// Matches the frontend renderPreview() pipeline:
///   1. Apply CSS filters (brightness, contrast, saturate, hueRotate, grayscale, sepia, blur)
///   2. Draw "UN" text
///   3. Draw "HEARD" text
///   4. Draw moafunk logo
///   5. Draw entity name (auto-fit)
///
/// Returns the composed image as PNG bytes. Input image is not modified.
pub fn apply_overlay_preset_sync(
    img_data: &[u8],
    params: &OverlayParams,
    entity_name: &str,
    fonts: &ExpandedFonts,
    tile_artist_names: Option<&[String]>,
) -> Option<Vec<u8>> {
    let dyn_img = image::load_from_memory(img_data).ok()?;
    let mut canvas = dyn_img.to_rgba8();
    let (w, h) = canvas.dimensions();

    // 1. Apply CSS filters
    apply_all_filters(&mut canvas, &params.filter);

    // 2-3. Draw UN + HEARD text
    draw_overlay_text(&mut canvas, "UN", &params.un, fonts, w, h);
    draw_overlay_text(&mut canvas, "HEARD", &params.heard, fonts, w, h);

    // 4. Draw logo
    draw_overlay_logo(&mut canvas, &params.logo, w, h);

    // 5. Draw entity/tile names
    if let Some(names) = tile_artist_names {
        // Show mode: draw artist names centered in each 2×2 tile
        draw_tile_names(
            &mut canvas,
            names,
            &params.artist_name,
            fonts,
            w,
            h,
            params.tile_colors.as_deref(),
            params.tile_shadow_colors.as_deref(),
        );
    } else {
        // Artist mode: single entity name
        draw_entity_name(&mut canvas, entity_name, &params.artist_name, fonts, w, h);
    }

    encode_image(&DynamicImage::ImageRgba8(canvas), "png")
}

/// Async wrapper for apply_overlay_preset_sync.
pub async fn apply_overlay_preset(
    img_data: Vec<u8>,
    params: OverlayParams,
    entity_name: String,
    tile_artist_names: Option<Vec<String>>,
) -> Option<Vec<u8>> {
    tokio::task::spawn_blocking(move || {
        let fonts = load_expanded_fonts()?;
        apply_overlay_preset_sync(
            &img_data,
            &params,
            &entity_name,
            &fonts,
            tile_artist_names.as_deref(),
        )
    })
    .await
    .ok()
    .flatten()
}
