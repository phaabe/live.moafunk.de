//! Instagram announcement images: one look for every show.
//!
//! Three templates, all black / `#ffec44` / white with Shoika and the moafunk
//! badge: `Pre` (feed post before the show, band or split layout), `OnAir`
//! (story while live) and `Post` (feed post after the show). The renderer is
//! sync and CPU-bound: call it from `spawn_blocking`.

use crate::handlers::api::require_show_editor;
use crate::image_overlay::{apply_all_filters, load_expanded_fonts, OverlayFilterParams};
use crate::{models, storage, AppError, AppState, Result};
use ab_glyph::{FontArc, PxScale};
use anyhow::Context;
use aws_sdk_s3::primitives::ByteStream;
use axum::{
    extract::{Path, Query, State},
    http::{header, HeaderMap, StatusCode},
    response::IntoResponse,
};
use image::codecs::jpeg::JpegEncoder;
use image::imageops::{self, FilterType};
use image::{DynamicImage, Rgba, RgbaImage};
use imageproc::drawing::{draw_filled_circle_mut, draw_filled_rect_mut, draw_text_mut, text_size};
use imageproc::rect::Rect;
use serde::Deserialize;
use std::sync::Arc;

const BLACK: Rgba<u8> = Rgba([0, 0, 0, 255]);
const WHITE: Rgba<u8> = Rgba([255, 255, 255, 255]);
const YELLOW: Rgba<u8> = Rgba([0xff, 0xec, 0x44, 255]);
const GREY: Rgba<u8> = Rgba([0x4a, 0x4a, 0x60, 255]);

const URL: &str = "LIVE.MOAFUNK.DE";
/// Instagram's own size limit for published images.
const MAX_JPEG_BYTES: usize = 8 * 1024 * 1024;
/// The longest headline the composer accepts.
pub const MAX_HEADLINE_CHARS: usize = 18;

/// Feed posts (`Pre`, `Post`) are 1080×1350, the story (`OnAir`) 1080×1920.
const W: u32 = 1080;
const FEED_H: u32 = 1350;
const STORY_H: u32 = 1920;
/// Instagram draws its UI over the top 14 % and bottom 20 % of a story.
const STORY_SAFE_TOP: u32 = 269;
const STORY_SAFE_BOTTOM: u32 = STORY_H - 384;
/// Side padding of every template.
const PAD_X: u32 = 72;
const LOGO: u32 = 110;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AnnouncementKind {
    Pre,
    OnAir,
    Post,
}

impl AnnouncementKind {
    /// The slot name in URLs and storage keys.
    pub fn slot(self) -> &'static str {
        match self {
            Self::Pre => "pre",
            Self::OnAir => "onair",
            Self::Post => "post",
        }
    }

    pub fn from_slot(slot: &str) -> Option<Self> {
        match slot {
            "pre" => Some(Self::Pre),
            "onair" => Some(Self::OnAir),
            "post" => Some(Self::Post),
            _ => None,
        }
    }
}

/// Only `Pre` has two layouts; the other kinds ignore it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AnnouncementLayout {
    #[default]
    Band,
    Split,
}

pub struct AnnouncementRender {
    pub kind: AnnouncementKind,
    pub layout: AnnouncementLayout,
    /// Up to 18 chars, uppercased on render. Used by `Pre`; `OnAir` and
    /// `Post` print their fixed "ON AIR" / "LISTEN BACK".
    pub headline: String,
    pub show_name: String,
    /// e.g. "FRI 21.11 · 20:00–22:00".
    pub date_line: String,
    /// e.g. "22:00", for "NOW · UNTIL 22:00".
    pub end_time: String,
    /// e.g. "EP. 12 · 21.11.26", for `Post`.
    pub episode_label: String,
    /// Cover, uploaded or guest photo. Center-cropped; the station filter is
    /// applied here.
    pub image: DynamicImage,
}

/// Render one announcement as an sRGB JPEG (quality 90, under 8 MB).
pub fn render_announcement_jpeg(input: &AnnouncementRender) -> anyhow::Result<Vec<u8>> {
    let canvas = render_canvas(input)?;
    let rgb = DynamicImage::ImageRgba8(canvas).to_rgb8();
    let mut out = Vec::new();
    JpegEncoder::new_with_quality(&mut out, 90)
        .encode_image(&rgb)
        .context("encode announcement JPEG")?;
    anyhow::ensure!(
        out.len() < MAX_JPEG_BYTES,
        "announcement JPEG is {} bytes, Instagram allows under {MAX_JPEG_BYTES}",
        out.len()
    );
    Ok(out)
}

struct Fonts {
    headline: FontArc,
    name: FontArc,
    line: FontArc,
}

fn render_canvas(input: &AnnouncementRender) -> anyhow::Result<RgbaImage> {
    let fonts = load_expanded_fonts().context("load Shoika fonts")?;
    let pick = |w: &str, s: &str| fonts.resolve(w, s).cloned().context("Shoika font missing");
    let fonts = Fonts {
        headline: pick("700", "italic")?,
        name: pick("700", "normal")?,
        line: pick("600", "normal")?,
    };
    let logo = image::open("./assets/brand/moafunk.png")
        .context("load moafunk logo")?
        .to_rgba8();
    Ok(match (input.kind, input.layout) {
        (AnnouncementKind::Pre, AnnouncementLayout::Band) => pre_band(input, &fonts, &logo),
        (AnnouncementKind::Pre, AnnouncementLayout::Split) => pre_split(input, &fonts, &logo),
        (AnnouncementKind::OnAir, _) => on_air(input, &fonts, &logo),
        (AnnouncementKind::Post, _) => post(input, &fonts, &logo),
    })
}

/// The image center-cropped to `w`×`h` with the station look
/// (CSS `contrast(1.08) saturate(.85)`).
fn station_image(img: &DynamicImage, w: u32, h: u32) -> RgbaImage {
    let mut out = img.resize_to_fill(w, h, FilterType::Lanczos3).to_rgba8();
    let filter = OverlayFilterParams {
        brightness: 1.0,
        contrast: 1.08,
        saturate: 0.85,
        hue_rotate: 0.0,
        grayscale: 0.0,
        sepia: 0.0,
        blur: 0.0,
    };
    apply_all_filters(&mut out, &filter);
    out
}

/// The badge contain-fitted into a `size` square, top-left at (x, y).
fn draw_logo(canvas: &mut RgbaImage, logo: &RgbaImage, x: u32, y: u32, size: u32) {
    let (lw, lh) = logo.dimensions();
    let scale = size as f32 / lw.max(lh) as f32;
    let (w, h) = (
        (lw as f32 * scale).round() as u32,
        (lh as f32 * scale).round() as u32,
    );
    let resized = imageops::resize(logo, w, h, FilterType::Lanczos3);
    imageops::overlay(
        canvas,
        &resized,
        (x + (size - w) / 2) as i64,
        (y + (size - h) / 2) as i64,
    );
}

/// The largest size up to `max_px` at which `text` fits in `max_w`.
fn fit(font: &FontArc, text: &str, max_px: f32, max_w: u32) -> f32 {
    if text_size(PxScale::from(max_px), font, text).0 <= max_w {
        return max_px;
    }
    let (mut low, mut high) = (8.0_f32, max_px);
    for _ in 0..14 {
        let mid = (low + high) / 2.0;
        if text_size(PxScale::from(mid), font, text).0 <= max_w {
            low = mid;
        } else {
            high = mid;
        }
    }
    low.floor()
}

/// Draw `text` auto-fitted to `max_w`, top-left at (x, y).
#[allow(clippy::too_many_arguments)]
fn text(
    canvas: &mut RgbaImage,
    font: &FontArc,
    text: &str,
    max_px: f32,
    max_w: u32,
    x: u32,
    y: u32,
    color: Rgba<u8>,
) {
    let px = fit(font, text, max_px, max_w);
    draw_text_mut(
        canvas,
        color,
        x as i32,
        y as i32,
        PxScale::from(px),
        font,
        text,
    );
}

fn rect(canvas: &mut RgbaImage, x: u32, y: u32, w: u32, h: u32, color: Rgba<u8>) {
    draw_filled_rect_mut(canvas, Rect::at(x as i32, y as i32).of_size(w, h), color);
}

/// Text heights for layout: size × line height.
fn line_h(px: f32, line_height: f32) -> u32 {
    (px * line_height).round() as u32
}

fn pre_band(input: &AnnouncementRender, f: &Fonts, logo: &RgbaImage) -> RgbaImage {
    let mut c = station_image(&input.image, W, FEED_H);
    let content_w = W - 2 * PAD_X;
    let (headline_px, name_px, date_px, gap) = (128.0, 60.0, 32.0, 16);
    let band_h = 64
        + line_h(headline_px, 0.92)
        + gap
        + line_h(name_px, 1.15)
        + gap
        + line_h(date_px, 1.2)
        + 72;
    let band_y = FEED_H - band_h;
    // rgba(0,0,0,.78) over the image.
    for y in band_y..FEED_H {
        for x in 0..W {
            let p = c.get_pixel_mut(x, y);
            for ch in 0..3 {
                p[ch] = (p[ch] as f32 * 0.22).round() as u8;
            }
        }
    }
    let mut y = band_y + 64;
    let headline = input.headline.to_uppercase();
    text(
        &mut c,
        &f.headline,
        &headline,
        headline_px,
        content_w,
        PAD_X,
        y,
        YELLOW,
    );
    y += line_h(headline_px, 0.92) + gap;
    let name = input.show_name.to_uppercase();
    text(&mut c, &f.name, &name, name_px, content_w, PAD_X, y, WHITE);
    y += line_h(name_px, 1.15) + gap;
    let date = format!("{} · {URL}", input.date_line.to_uppercase());
    text(&mut c, &f.line, &date, date_px, content_w, PAD_X, y, WHITE);
    draw_logo(&mut c, logo, 48, 40, LOGO);
    c
}

fn pre_split(input: &AnnouncementRender, f: &Fonts, logo: &RgbaImage) -> RgbaImage {
    let image_h = FEED_H * 3 / 5;
    let mut c = RgbaImage::from_pixel(W, FEED_H, YELLOW);
    imageops::overlay(&mut c, &station_image(&input.image, W, image_h), 0, 0);
    let content_w = W - 2 * PAD_X;
    let (headline_px, name_px, date_px, gap) = (112.0, 56.0, 32.0, 16);
    let block_h =
        line_h(headline_px, 0.92) + gap + line_h(name_px, 1.15) + gap + line_h(date_px, 1.2);
    // The text block sits centered in the yellow panel.
    let mut y = image_h + (FEED_H - image_h - block_h) / 2;
    let headline = input.headline.to_uppercase();
    text(
        &mut c,
        &f.headline,
        &headline,
        headline_px,
        content_w,
        PAD_X,
        y,
        BLACK,
    );
    y += line_h(headline_px, 0.92) + gap;
    let name = input.show_name.to_uppercase();
    text(&mut c, &f.name, &name, name_px, content_w, PAD_X, y, BLACK);
    y += line_h(name_px, 1.15) + gap;
    let date = format!("{} · {URL}", input.date_line.to_uppercase());
    text(&mut c, &f.line, &date, date_px, content_w, PAD_X, y, BLACK);
    draw_logo(&mut c, logo, 48, 40, LOGO);
    c
}

fn on_air(input: &AnnouncementRender, f: &Fonts, logo: &RgbaImage) -> RgbaImage {
    let mut c = RgbaImage::from_pixel(W, STORY_H, BLACK);
    let content_w = W - 2 * PAD_X;
    let (title_px, name_px, until_px, button_px, gap) = (160.0, 68.0, 34.0, 36.0, 32);
    let title_h = line_h(title_px, 0.88);
    let info_h = line_h(name_px, 1.15) + 12 + line_h(until_px, 1.2);
    let row_h = LOGO;
    // The square image takes what is left of the safe area.
    let rest = title_h + gap + gap + info_h + gap + row_h;
    let side = (STORY_SAFE_BOTTOM - STORY_SAFE_TOP - rest).min(content_w);
    let mut y = STORY_SAFE_TOP + (STORY_SAFE_BOTTOM - STORY_SAFE_TOP - rest - side) / 2;

    let dot = 22;
    draw_filled_circle_mut(
        &mut c,
        ((PAD_X + dot) as i32, (y + title_h / 2) as i32),
        dot as i32,
        YELLOW,
    );
    let title_x = PAD_X + 2 * dot + 28;
    text(
        &mut c,
        &f.headline,
        "ON AIR",
        title_px,
        W - PAD_X - title_x,
        title_x,
        y,
        YELLOW,
    );
    y += title_h + gap;

    imageops::overlay(
        &mut c,
        &station_image(&input.image, side, side),
        PAD_X as i64,
        y as i64,
    );
    y += side + gap;

    let name = input.show_name.to_uppercase();
    text(&mut c, &f.name, &name, name_px, content_w, PAD_X, y, WHITE);
    y += line_h(name_px, 1.15) + 12;
    let until = match input.end_time.trim() {
        "" => "NOW".to_string(),
        end => format!("NOW · UNTIL {end}"),
    };
    text(
        &mut c, &f.line, &until, until_px, content_w, PAD_X, y, WHITE,
    );
    y += line_h(until_px, 1.2) + gap;

    // Yellow "LISTEN › URL" button, badge on the right of the same row.
    let label = format!("LISTEN › {URL}");
    let max_label_w = content_w - LOGO - 2 * 32 - 24;
    let px = fit(&f.line, &label, button_px, max_label_w);
    let (tw, _) = text_size(PxScale::from(px), &f.line, &label);
    let button_h = line_h(px, 1.2) + 40;
    let button_y = y + (row_h - button_h) / 2;
    rect(&mut c, PAD_X, button_y, tw + 64, button_h, YELLOW);
    text(
        &mut c,
        &f.line,
        &label,
        px,
        max_label_w,
        PAD_X + 32,
        button_y + 20,
        BLACK,
    );
    draw_logo(&mut c, logo, W - PAD_X - LOGO, y, LOGO);
    c
}

/// Waveform bar heights from the board (half scale), played then unplayed.
const WAVE_PLAYED: [u32; 27] = [
    8, 20, 32, 16, 40, 24, 12, 28, 36, 20, 8, 24, 32, 16, 40, 26, 12, 30, 20, 8, 34, 22, 14, 38,
    24, 10, 28,
];
const WAVE_REST: [u32; 25] = [
    18, 32, 12, 26, 38, 20, 8, 30, 16, 34, 22, 10, 28, 18, 36, 14, 24, 8, 32, 20, 12, 26, 16, 8, 20,
];

fn post(input: &AnnouncementRender, f: &Fonts, logo: &RgbaImage) -> RgbaImage {
    let mut c = RgbaImage::from_pixel(W, FEED_H, BLACK);
    let content_w = W - 2 * PAD_X;
    let (title_px, name_px, line_px, gap) = (116.0, 56.0, 30.0, 36);
    let text_h = line_h(title_px, 0.92) + 12 + line_h(name_px, 1.15) + 12 + line_h(line_px, 1.2);
    let wave_h = 88;

    // Header: badge left, episode label right.
    let mut y = 56;
    draw_logo(&mut c, logo, PAD_X, y, LOGO);
    let label = input.episode_label.to_uppercase();
    let label_px = fit(&f.line, &label, 28.0, content_w - LOGO - 24);
    let (lw, _) = text_size(PxScale::from(label_px), &f.line, &label);
    let label_y = y + (LOGO - line_h(label_px, 1.2)) / 2;
    text(
        &mut c,
        &f.line,
        &label,
        label_px,
        content_w,
        W - PAD_X - lw,
        label_y,
        YELLOW,
    );
    y += LOGO + gap;

    let image_h = FEED_H - 64 - text_h - gap - wave_h - gap - y;
    imageops::overlay(
        &mut c,
        &station_image(&input.image, content_w, image_h),
        PAD_X as i64,
        y as i64,
    );
    y += image_h + gap;

    let bars = WAVE_PLAYED
        .iter()
        .map(|h| (h, YELLOW))
        .chain(WAVE_REST.iter().map(|h| (h, GREY)));
    for (i, (h, color)) in bars.enumerate() {
        let bar_h = h * 2;
        rect(
            &mut c,
            PAD_X + i as u32 * 18,
            y + (wave_h - bar_h) / 2,
            10,
            bar_h,
            color,
        );
    }
    y += wave_h + gap;

    text(
        &mut c,
        &f.headline,
        "LISTEN BACK",
        title_px,
        content_w,
        PAD_X,
        y,
        YELLOW,
    );
    y += line_h(title_px, 0.92) + 12;
    let name = input.show_name.to_uppercase();
    text(&mut c, &f.name, &name, name_px, content_w, PAD_X, y, WHITE);
    y += line_h(name_px, 1.15) + 12;
    let link = "ON SOUNDCLOUD · LINK IN BIO";
    text(&mut c, &f.line, link, line_px, content_w, PAD_X, y, WHITE);
    c
}

/// Store a rendered announcement at `shows/{id}/announcements/{slot}.jpg` and
/// return the key, for a presigned URL Meta can fetch.
pub async fn store_announcement_jpeg(
    state: &Arc<AppState>,
    show_id: i64,
    slot: AnnouncementKind,
    bytes: Vec<u8>,
) -> Result<String> {
    let key = format!("shows/{}/announcements/{}.jpg", show_id, slot.slot());
    state
        .s3_client
        .put_object()
        .bucket(&state.config.r2_bucket_name)
        .key(&key)
        .body(ByteStream::from(bytes))
        .content_type("image/jpeg")
        .send()
        .await
        .map_err(|e| AppError::Storage(format!("Failed to upload announcement: {}", e)))?;
    Ok(key)
}

#[derive(Debug, Deserialize)]
pub struct PreviewQuery {
    #[serde(default)]
    pub headline: String,
    #[serde(default)]
    pub layout: AnnouncementLayout,
}

/// Preview of one announcement slot with unsaved composer values.
/// GET /api/shows/:id/announcements/:slot/preview.jpg?headline=…&layout=band|split
/// &image_source=cover|upload|guest&image_key=… (image fields checked like a save)
pub async fn api_announcement_preview(
    State(state): State<Arc<AppState>>,
    Path((id, slot)): Path<(i64, String)>,
    Query(query): Query<PreviewQuery>,
    Query(image): Query<crate::handlers::announcements::AnnouncementInput>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let (_user, show) = require_show_editor(&state, &headers, id).await?;
    let kind = AnnouncementKind::from_slot(&slot)
        .ok_or_else(|| AppError::BadRequest(format!("Unknown announcement slot: {slot}")))?;
    if query.headline.chars().count() > MAX_HEADLINE_CHARS {
        return Err(AppError::BadRequest(format!(
            "Headline is longer than {MAX_HEADLINE_CHARS} characters"
        )));
    }

    let (_, image_key) = crate::handlers::announcements::image_of(id, &image)?;
    let image = load_announcement_image(&state, id, image_key.as_deref()).await?;
    let input = announcement_for_show(&state, &show, kind, query, image).await?;
    let jpeg = tokio::task::spawn_blocking(move || render_announcement_jpeg(&input))
        .await
        .map_err(|e| AppError::Internal(format!("Render task failed: {e}")))?
        .map_err(|e| AppError::Internal(format!("Render failed: {e:#}")))?;

    Ok((
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, "image/jpeg"),
            (header::CACHE_CONTROL, "no-store"),
        ],
        jpeg,
    ))
}

/// Width of the Social media card's small picture: sharp at 48 px on a 3×
/// screen.
const THUMB_W: u32 = 160;

#[derive(Debug, Default, Deserialize)]
pub struct ImageQuery {
    /// "thumb" for the card's small picture.
    pub size: Option<String>,
}

/// The picture of a set-up slot, for the Social media card.
/// GET /api/shows/:id/announcements/:slot/image.jpg?size=thumb
///
/// The episode's hosts see every set-up slot; other signed-in users only a
/// sent post, like its link in `/status`. A sent post is the stored JPEG;
/// otherwise the saved row is rendered. Sent through the backend, never as a
/// presigned R2 link.
pub async fn api_announcement_image(
    State(state): State<Arc<AppState>>,
    Path((id, slot)): Path<(i64, String)>,
    Query(query): Query<ImageQuery>,
    headers: HeaderMap,
) -> Result<impl IntoResponse> {
    let token = crate::auth::get_session_from_headers(&headers);
    crate::auth::get_current_user(&state, token.as_deref())
        .await
        .ok_or_else(|| AppError::Unauthorized("Not authenticated".to_string()))?;
    let kind = AnnouncementKind::from_slot(&slot)
        .ok_or_else(|| AppError::BadRequest(format!("Unknown announcement slot: {slot}")))?;
    let row: Option<crate::announcements::Announcement> = sqlx::query_as(
        "SELECT * FROM announcements WHERE show_id = ? AND slot = ? AND channel = 'instagram'",
    )
    .bind(id)
    .bind(kind.slot())
    .fetch_optional(&state.db)
    .await?;
    let row = row
        .filter(|r| !matches!(r.state.as_str(), "draft" | "cancelled" | "rejected"))
        .ok_or_else(|| AppError::NotFound("This slot has no post".to_string()))?;

    let stored = if row.state == "sent" {
        let key = format!("shows/{id}/announcements/{}.jpg", kind.slot());
        storage::download_file(&state, &key)
            .await
            .ok()
            .map(|(d, _)| d)
    } else {
        // Not sent yet: only the episode's hosts may see it.
        require_show_editor(&state, &headers, id).await?;
        None
    };
    let jpeg = match stored {
        Some(jpeg) => jpeg,
        None => {
            let show: models::Show = sqlx::query_as("SELECT * FROM shows WHERE id = ?")
                .bind(id)
                .fetch_optional(&state.db)
                .await?
                .ok_or_else(|| AppError::NotFound("Show not found".to_string()))?;
            render_jpeg(row_render_input(&state, &show, &row).await?).await?
        }
    };
    let jpeg = if query.size.as_deref() == Some("thumb") {
        tokio::task::spawn_blocking(move || thumb_jpeg(&jpeg))
            .await
            .map_err(|e| AppError::Internal(format!("Resize task failed: {e}")))?
            .map_err(|e| AppError::Internal(format!("Resize failed: {e:#}")))?
    } else {
        jpeg
    };
    Ok((
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, "image/jpeg"),
            // The card asks with ?v=<image_version>, so an edit gets a new URL.
            (header::CACHE_CONTROL, "private, max-age=3600"),
        ],
        jpeg,
    ))
}

/// A small copy of a JPEG, [`THUMB_W`] wide.
fn thumb_jpeg(jpeg: &[u8]) -> anyhow::Result<Vec<u8>> {
    let image = image::load_from_memory(jpeg).context("read announcement JPEG")?;
    let small = image
        .resize(THUMB_W, THUMB_W * 4, FilterType::Triangle)
        .to_rgb8();
    let mut out = Vec::new();
    JpegEncoder::new_with_quality(&mut out, 80)
        .encode_image(&small)
        .context("encode thumbnail")?;
    Ok(out)
}

/// The render input of a saved announcement: its photo (the upload, else the
/// current cover), headline and layout.
pub async fn row_render_input(
    state: &Arc<AppState>,
    show: &models::Show,
    row: &crate::announcements::Announcement,
) -> Result<AnnouncementRender> {
    let kind = AnnouncementKind::from_slot(&row.slot)
        .ok_or_else(|| AppError::Internal(format!("Unknown slot {}", row.slot)))?;
    let image_key = row
        .image_key
        .as_deref()
        .filter(|_| row.image_source != "cover");
    let image = load_announcement_image(state, row.show_id, image_key).await?;
    let layout = if row.layout == "split" {
        AnnouncementLayout::Split
    } else {
        AnnouncementLayout::Band
    };
    let query = PreviewQuery {
        headline: row.headline.clone().unwrap_or_default(),
        layout,
    };
    announcement_for_show(state, show, kind, query, image).await
}

/// Render off the async runtime.
pub async fn render_jpeg(input: AnnouncementRender) -> Result<Vec<u8>> {
    tokio::task::spawn_blocking(move || render_announcement_jpeg(&input))
        .await
        .map_err(|e| AppError::Internal(format!("Render task failed: {e}")))?
        .map_err(|e| AppError::Internal(format!("Render failed: {e:#}")))
}

/// The photo of an announcement: the uploaded `image_key`, else the episode
/// cover. A show without a cover gets the default black cover.
pub async fn load_announcement_image(
    state: &Arc<AppState>,
    show_id: i64,
    image_key: Option<&str>,
) -> Result<DynamicImage> {
    let cover = format!("shows/{show_id}/cover.png");
    let data = match storage::download_file(state, image_key.unwrap_or(&cover)).await {
        Ok((data, _)) => data,
        Err(_) if image_key.is_none() => {
            return Ok(DynamicImage::ImageRgba8(RgbaImage::from_pixel(1, 1, BLACK)))
        }
        Err(e) => return Err(e),
    };
    image::load_from_memory(&data)
        .map_err(|e| AppError::Internal(format!("Announcement photo is not an image: {e}")))
}

/// Render input from the episode row. `image` is the seam for uploaded or
/// guest photos: callers pick the source.
pub async fn announcement_for_show(
    state: &Arc<AppState>,
    show: &models::Show,
    kind: AnnouncementKind,
    query: PreviewQuery,
    image: DynamicImage,
) -> Result<AnnouncementRender> {
    // Series name and episode number, when the episode belongs to a series.
    let series: Option<(String, i64)> = sqlx::query_as(
        "SELECT st.name, (SELECT COUNT(*) FROM shows e \
           WHERE e.template_id = s.template_id AND e.date <= s.date) \
         FROM shows s JOIN show_templates st ON st.id = s.template_id WHERE s.id = ?",
    )
    .bind(show.id)
    .fetch_optional(&state.db)
    .await?;

    let date = chrono::NaiveDate::parse_from_str(&show.date, "%Y-%m-%d").ok();
    let mut date_line = date
        .map(|d| d.format("%a %d.%m").to_string().to_uppercase())
        .unwrap_or_else(|| show.date.clone());
    match (show.start_time.as_deref(), show.end_time.as_deref()) {
        (Some(start), Some(end)) => date_line.push_str(&format!(" · {start}–{end}")),
        (Some(start), None) => date_line.push_str(&format!(" · {start}")),
        _ => {}
    }
    let short_date = date
        .map(|d| d.format("%d.%m.%y").to_string())
        .unwrap_or_else(|| show.date.clone());
    let (show_name, episode_label) = match series {
        Some((name, n)) => (name, format!("EP. {n} · {short_date}")),
        None => (show.title.clone(), short_date),
    };

    Ok(AnnouncementRender {
        kind,
        layout: query.layout,
        headline: query.headline,
        show_name,
        date_line,
        end_time: show.end_time.clone().unwrap_or_default(),
        episode_label,
        image,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A colourful test photo so filter and crop are visible in renders.
    fn photo(w: u32, h: u32) -> DynamicImage {
        DynamicImage::ImageRgba8(RgbaImage::from_fn(w, h, |x, y| {
            Rgba([(x * 255 / w) as u8, (y * 255 / h) as u8, 180, 255])
        }))
    }

    fn input(kind: AnnouncementKind, layout: AnnouncementLayout) -> AnnouncementRender {
        AnnouncementRender {
            kind,
            layout,
            headline: "Next Friday".into(),
            show_name: "Late Night Bass".into(),
            date_line: "FRI 21.11 · 20:00–22:00".into(),
            end_time: "22:00".into(),
            episode_label: "EP. 12 · 21.11.26".into(),
            image: photo(1600, 900),
        }
    }

    const ALL: [(AnnouncementKind, AnnouncementLayout, &str, u32); 4] = [
        (
            AnnouncementKind::Pre,
            AnnouncementLayout::Band,
            "pre-band",
            FEED_H,
        ),
        (
            AnnouncementKind::Pre,
            AnnouncementLayout::Split,
            "pre-split",
            FEED_H,
        ),
        (
            AnnouncementKind::OnAir,
            AnnouncementLayout::Band,
            "onair",
            STORY_H,
        ),
        (
            AnnouncementKind::Post,
            AnnouncementLayout::Band,
            "post",
            FEED_H,
        ),
    ];

    #[test]
    fn every_kind_renders_a_jpeg_of_the_exact_size() {
        // Set ANNOUNCEMENT_RENDER_DIR to keep the samples for a visual check.
        let out_dir = std::env::var("ANNOUNCEMENT_RENDER_DIR").ok();
        for (kind, layout, name, h) in ALL {
            let jpeg = render_announcement_jpeg(&input(kind, layout)).unwrap();
            assert!(jpeg.len() < MAX_JPEG_BYTES, "{name}");
            let fmt = image::guess_format(&jpeg).unwrap();
            assert_eq!(fmt, image::ImageFormat::Jpeg, "{name}");
            let decoded = image::load_from_memory(&jpeg).unwrap();
            assert_eq!((decoded.width(), decoded.height()), (W, h), "{name}");
            if let Some(dir) = &out_dir {
                std::fs::write(format!("{dir}/{name}.jpg"), &jpeg).unwrap();
            }
        }
    }

    #[test]
    fn the_glyphs_used_exist_in_shoika() {
        let fonts = load_expanded_fonts().unwrap();
        let font = fonts.resolve("600", "normal").unwrap();
        for ch in ['·', '–', '›'] {
            assert_ne!(ab_glyph::Font::glyph_id(font, ch).0, 0, "missing {ch}");
        }
    }

    #[test]
    fn a_long_headline_shrinks_to_the_content_width() {
        let fonts = load_expanded_fonts().unwrap();
        let font = fonts.resolve("700", "italic").unwrap();
        let long = "WWWWWWWWWWWWWWWWWW";
        let max_w = W - 2 * PAD_X;
        let px = fit(font, long, 128.0, max_w);
        assert!(px < 128.0);
        assert!(text_size(PxScale::from(px), font, long).0 <= max_w);

        // Nothing is drawn into the side padding.
        let mut pre = input(AnnouncementKind::Pre, AnnouncementLayout::Split);
        pre.headline = long.into();
        pre.show_name = "A very long show name that will not fit".into();
        let c = render_canvas(&pre).unwrap();
        for y in FEED_H * 3 / 5..FEED_H {
            for x in (0..PAD_X / 2).chain(W - PAD_X / 2..W) {
                assert_eq!(*c.get_pixel(x, y), YELLOW, "ink at ({x},{y})");
            }
        }
    }

    #[test]
    fn the_story_keeps_instagrams_ui_zones_black() {
        let mut story = input(AnnouncementKind::OnAir, AnnouncementLayout::Band);
        story.image = DynamicImage::ImageRgba8(RgbaImage::from_pixel(10, 10, WHITE));
        let c = render_canvas(&story).unwrap();
        for y in (0..STORY_SAFE_TOP).chain(STORY_SAFE_BOTTOM..STORY_H) {
            for x in 0..W {
                assert_eq!(*c.get_pixel(x, y), BLACK, "ink at ({x},{y})");
            }
        }
        // The image is inside the safe area.
        assert!(c.pixels().any(|p| p[0] > 200 && p[1] > 200 && p[2] > 200));
    }

    #[test]
    fn slots_round_trip() {
        for kind in [
            AnnouncementKind::Pre,
            AnnouncementKind::OnAir,
            AnnouncementKind::Post,
        ] {
            assert_eq!(AnnouncementKind::from_slot(kind.slot()), Some(kind));
        }
        assert_eq!(AnnouncementKind::from_slot("story"), None);
    }

    #[tokio::test]
    async fn store_writes_the_jpeg_under_the_show() {
        let (state, _temp, r2) = crate::submit_download_tests::setup(serde_json::json!({})).await;
        let key = store_announcement_jpeg(&state, 7, AnnouncementKind::OnAir, vec![1, 2, 3])
            .await
            .unwrap();
        assert_eq!(key, "shows/7/announcements/onair.jpg");
        assert_eq!(r2.object(&state, &key), Some(vec![1, 2, 3]));
    }

    #[tokio::test]
    async fn preview_is_for_the_host_only() {
        use crate::broadcast_tests::{add_user, headers};
        let (state, _temp, _r2) = crate::submit_download_tests::setup(serde_json::json!({})).await;
        let host = add_user(&state, "host", "host").await;
        let other = add_user(&state, "other", "host").await;
        sqlx::query(
            "INSERT INTO shows (id, title, date, host_user_id, start_time, end_time) \
             VALUES (1, 'Night Shift', '2026-06-01', ?, '20:00', '22:00')",
        )
        .bind(host)
        .execute(&state.db)
        .await
        .unwrap();
        let call = |user| {
            let state = state.clone();
            async move {
                api_announcement_preview(
                    State(state.clone()),
                    Path((1, "pre".to_string())),
                    Query(PreviewQuery {
                        headline: "Tonight".into(),
                        layout: AnnouncementLayout::Split,
                    }),
                    Query(Default::default()),
                    headers(&state, user).await,
                )
                .await
                .map(|r| r.into_response())
            }
        };

        assert!(matches!(call(other).await, Err(AppError::Forbidden(_))));

        let response = call(host).await.unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let h = response.headers();
        assert_eq!(h[header::CONTENT_TYPE], "image/jpeg");
        assert_eq!(h[header::CACHE_CONTROL], "no-store");
        let body = axum::body::to_bytes(response.into_body(), MAX_JPEG_BYTES)
            .await
            .unwrap();
        let img = image::load_from_memory(&body).unwrap();
        assert_eq!((img.width(), img.height()), (W, FEED_H));
    }
}
