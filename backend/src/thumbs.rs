//! Small copies of stored images, so a small picture on a page does not load
//! the full-size upload.
//!
//! Next to an image's presigned URL the API sends a `srcset`: one signed
//! `/api/img` link per width in [`WIDTHS`]. The browser picks the width it
//! needs. The first request for a width makes a resized copy and keeps it in R2
//! under `thumbs/<width>/<etag of the original>/<key>`, so a replaced original
//! (same key, new ETag) gets new copies.

use crate::{storage, AppError, AppState, Result};
use aws_sdk_s3::primitives::ByteStream;
use axum::{
    extract::{Query, State},
    http::header,
    response::{IntoResponse, Redirect, Response},
};
use hmac::{Hmac, Mac};
use image::codecs::jpeg::JpegEncoder;
use image::imageops::FilterType;
use image::{DynamicImage, ImageDecoder, ImageFormat, ImageReader};
use serde::Deserialize;
use sha2::Sha256;
use std::io::Cursor;
use std::sync::Arc;
use tokio::sync::Semaphore;

/// The widths a page can ask for. Few on purpose: each is one stored copy.
pub const WIDTHS: [u32; 4] = [160, 320, 640, 1280];

/// How long a link works: as long as a presigned R2 link.
const LINK_SECS: u64 = 3600;

/// At most this many images are resized at once. A large photo needs about
/// 100 MB of memory while it is resized.
static RESIZING: Semaphore = Semaphore::const_new(2);

fn mac(secret: &str, key: &str, expires: u64) -> Hmac<Sha256> {
    let mut mac =
        Hmac::<Sha256>::new_from_slice(secret.as_bytes()).expect("HMAC takes a key of any size");
    mac.update(format!("img\n{expires}\n{key}").as_bytes());
    mac
}

/// The `srcset` of an `<img>` that shows the stored image at `key`.
pub fn srcset(state: &AppState, key: &str) -> String {
    let expires = chrono::Utc::now().timestamp() as u64 + LINK_SECS;
    let signature = base64_url::encode(
        &mac(&state.config.secret_key, key, expires)
            .finalize()
            .into_bytes(),
    );
    let query =
        serde_urlencoded::to_string([("k", key), ("e", &expires.to_string()), ("s", &signature)])
            .expect("strings always encode");
    WIDTHS
        .iter()
        .map(|w| format!("/api/img?{query}&w={w} {w}w"))
        .collect::<Vec<_>>()
        .join(", ")
}

#[derive(Debug, Deserialize)]
pub struct ImageQuery {
    /// Key of the original in the default bucket.
    k: String,
    /// Unix time the link stops working.
    e: u64,
    /// Signature of `k` and `e`.
    s: String,
    /// Width, one of [`WIDTHS`].
    w: u32,
}

/// GET /api/img?k=<key>&e=<expires>&s=<signature>&w=<width>
///
/// A copy of the stored image `k`, at most `w` pixels wide. The signature
/// stands in for a login, like a presigned R2 link: only the API makes one, for
/// an image the user may see. An image this backend cannot read (e.g. WebP)
/// redirects to the original.
pub async fn api_image(
    State(state): State<Arc<AppState>>,
    Query(query): Query<ImageQuery>,
) -> Result<Response> {
    if !WIDTHS.contains(&query.w) {
        return Err(AppError::BadRequest("Unknown image width".to_string()));
    }
    let signed = base64_url::decode(&query.s).is_ok_and(|signature| {
        mac(&state.config.secret_key, &query.k, query.e)
            .verify_slice(&signature)
            .is_ok()
    });
    if !signed || query.e < chrono::Utc::now().timestamp() as u64 {
        return Err(AppError::Forbidden(
            "Image link is not valid (anymore)".to_string(),
        ));
    }

    let bucket = &state.config.r2_bucket_name;
    let head = state
        .s3_client
        .head_object()
        .bucket(bucket)
        .key(&query.k)
        .send()
        .await
        .map_err(|e| match e.into_service_error() {
            e if e.is_not_found() => AppError::NotFound("Image not found".to_string()),
            e => AppError::Storage(format!("Failed to read image info: {e}")),
        })?;
    let Some(etag) = head
        .e_tag()
        .map(|t| t.trim_matches('"'))
        .filter(|t| !t.is_empty())
    else {
        // Without an ETag a stored copy could outlive a replaced original.
        return original(&state, &query.k).await;
    };
    let copy_key = format!("thumbs/{}/{etag}/{}", query.w, query.k);
    if let Ok((data, content_type)) = storage::download_file(&state, &copy_key).await {
        return Ok(image_response(data, content_type));
    }

    let made = {
        let _permit = RESIZING.acquire().await.expect("never closed");
        let (data, _) = storage::download_file(&state, &query.k).await?;
        let width = query.w;
        tokio::task::spawn_blocking(move || resize(&data, width)).await
    };
    let Ok(Ok((data, content_type))) = made else {
        return original(&state, &query.k).await;
    };
    if let Err(e) = state
        .s3_client
        .put_object()
        .bucket(bucket)
        .key(&copy_key)
        .body(ByteStream::from(data.clone()))
        .content_type(content_type)
        .send()
        .await
    {
        // The copy is made again next time.
        tracing::warn!("Failed to store image copy {copy_key}: {e}");
    }
    Ok(image_response(data, content_type.to_string()))
}

async fn original(state: &Arc<AppState>, key: &str) -> Result<Response> {
    let url = storage::get_presigned_url(state, key, LINK_SECS).await?;
    Ok(Redirect::temporary(&url).into_response())
}

fn image_response(data: Vec<u8>, content_type: String) -> Response {
    (
        [
            (header::CONTENT_TYPE, content_type),
            // The link changes with every API response, like a presigned link.
            (header::CACHE_CONTROL, "private, max-age=3600".to_string()),
        ],
        data,
    )
        .into_response()
}

/// `data` as an image at most `width` pixels wide, turned upright like the
/// browser shows the original. A JPEG, or a PNG if it has see-through parts.
fn resize(data: &[u8], width: u32) -> anyhow::Result<(Vec<u8>, &'static str)> {
    let mut decoder = ImageReader::new(Cursor::new(data))
        .with_guessed_format()?
        .into_decoder()?;
    let orientation = decoder.orientation()?;
    let mut image = DynamicImage::from_decoder(decoder)?;
    image.apply_orientation(orientation);
    if image.width() > width {
        image = image.resize(width, u32::MAX, FilterType::Triangle);
    }
    let mut out = Vec::new();
    if image.color().has_alpha() && image.to_rgba8().pixels().any(|p| p[3] < u8::MAX) {
        image.write_to(&mut Cursor::new(&mut out), ImageFormat::Png)?;
        return Ok((out, "image/png"));
    }
    JpegEncoder::new_with_quality(&mut out, 80).encode_image(&image.to_rgb8())?;
    Ok((out, "image/jpeg"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::StatusCode;
    use image::{Rgb, RgbImage, Rgba, RgbaImage};

    fn jpeg(width: u32, height: u32, color: [u8; 3]) -> Vec<u8> {
        let mut out = Vec::new();
        JpegEncoder::new_with_quality(&mut out, 90)
            .encode_image(&RgbImage::from_pixel(width, height, Rgb(color)))
            .unwrap();
        out
    }

    fn png(image: RgbaImage) -> Vec<u8> {
        let mut out = Vec::new();
        DynamicImage::ImageRgba8(image)
            .write_to(&mut Cursor::new(&mut out), ImageFormat::Png)
            .unwrap();
        out
    }

    /// The query of the `srcset` link for `width`.
    fn query(srcset: &str, width: u32) -> ImageQuery {
        let link = srcset
            .split(", ")
            .find(|candidate| candidate.ends_with(&format!(" {width}w")))
            .unwrap();
        let url = link.split(' ').next().unwrap();
        serde_urlencoded::from_str(url.strip_prefix("/api/img?").unwrap()).unwrap()
    }

    async fn get(state: &Arc<AppState>, query: ImageQuery) -> Result<Response> {
        api_image(State(state.clone()), Query(query)).await
    }

    async fn body(response: Response) -> Vec<u8> {
        axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap()
            .to_vec()
    }

    #[tokio::test]
    async fn srcset_has_a_link_per_width() {
        let (state, _temp, _r2) = crate::submit_download_tests::setup(serde_json::json!({})).await;
        let srcset = srcset(&state, "shows/1/cover.png");
        for width in WIDTHS {
            let query = query(&srcset, width);
            assert_eq!(query.k, "shows/1/cover.png");
            assert_eq!(query.w, width);
        }
    }

    #[test]
    fn resize_makes_a_narrower_jpeg_and_never_wider() {
        let (small, content_type) = resize(&jpeg(2000, 1000, [200, 0, 0]), 320).unwrap();
        assert_eq!(content_type, "image/jpeg");
        let small = image::load_from_memory(&small).unwrap();
        assert_eq!((small.width(), small.height()), (320, 160));

        let (same, _) = resize(&jpeg(100, 50, [0, 0, 0]), 640).unwrap();
        assert_eq!(image::load_from_memory(&same).unwrap().width(), 100);
    }

    #[test]
    fn resize_keeps_see_through_parts_as_png() {
        let opaque = RgbaImage::from_pixel(800, 800, Rgba([10, 20, 30, 255]));
        assert_eq!(resize(&png(opaque), 320).unwrap().1, "image/jpeg");
        let mut clear = RgbaImage::from_pixel(800, 800, Rgba([10, 20, 30, 255]));
        clear.put_pixel(0, 0, Rgba([0, 0, 0, 0]));
        assert_eq!(resize(&png(clear), 320).unwrap().1, "image/png");
    }

    #[tokio::test]
    async fn serves_and_keeps_a_small_copy() {
        let (state, _temp, r2) = crate::submit_download_tests::setup(serde_json::json!({})).await;
        let key = "artists/1/pic/photo.jpg";
        r2.insert(&state, key, &jpeg(2000, 1000, [200, 0, 0]));
        let srcset = srcset(&state, key);

        let response = get(&state, query(&srcset, 320)).await.unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(response.headers()[header::CONTENT_TYPE], "image/jpeg");
        let small = image::load_from_memory(&body(response).await).unwrap();
        assert_eq!(small.width(), 320);
        let copies = r2.keys_in(&state.config.r2_bucket_name, "thumbs/320/");
        assert_eq!(copies.len(), 1);
        assert!(copies[0].ends_with(key));

        // The second request is served from the stored copy.
        let response = get(&state, query(&srcset, 320)).await.unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(r2.count(&state, "GET", key), 1);
    }

    #[tokio::test]
    async fn a_replaced_original_gets_a_new_copy() {
        let (state, _temp, r2) = crate::submit_download_tests::setup(serde_json::json!({})).await;
        let key = "templates/3/cover.png";
        r2.insert(&state, key, &jpeg(1000, 1000, [255, 0, 0]));
        get(&state, query(&srcset(&state, key), 160)).await.unwrap();

        r2.insert(&state, key, &jpeg(1000, 1000, [0, 0, 255]));
        let response = get(&state, query(&srcset(&state, key), 160)).await.unwrap();
        let small = image::load_from_memory(&body(response).await)
            .unwrap()
            .to_rgb8();
        let Rgb([red, _, blue]) = *small.get_pixel(80, 80);
        assert!(blue > 200 && red < 50, "still the old picture");
        assert_eq!(
            r2.keys_in(&state.config.r2_bucket_name, "thumbs/160/")
                .len(),
            2
        );
    }

    #[tokio::test]
    async fn an_unreadable_image_redirects_to_the_original() {
        let (state, _temp, r2) = crate::submit_download_tests::setup(serde_json::json!({})).await;
        let key = "artists/1/pic/photo.heic";
        r2.insert(&state, key, b"not an image this backend reads");
        let response = get(&state, query(&srcset(&state, key), 640)).await.unwrap();
        assert_eq!(response.status(), StatusCode::TEMPORARY_REDIRECT);
        let location = response.headers()[header::LOCATION].to_str().unwrap();
        assert!(location.contains("photo.heic"));
    }

    #[tokio::test]
    async fn refuses_a_changed_expired_or_unknown_link() {
        let (state, _temp, r2) = crate::submit_download_tests::setup(serde_json::json!({})).await;
        r2.insert(&state, "a.jpg", &jpeg(10, 10, [0, 0, 0]));
        let srcset = srcset(&state, "a.jpg");

        let mut other_key = query(&srcset, 320);
        other_key.k = "b.jpg".to_string();
        assert!(matches!(
            get(&state, other_key).await,
            Err(AppError::Forbidden(_))
        ));

        let mut later = query(&srcset, 320);
        later.e += 1;
        assert!(matches!(
            get(&state, later).await,
            Err(AppError::Forbidden(_))
        ));

        let mut expired = query(&srcset, 320);
        expired.e = 1;
        expired.s = base64_url::encode(
            &mac(&state.config.secret_key, "a.jpg", 1)
                .finalize()
                .into_bytes(),
        );
        assert!(matches!(
            get(&state, expired).await,
            Err(AppError::Forbidden(_))
        ));

        let mut odd_width = query(&srcset, 320);
        odd_width.w = 321;
        assert!(matches!(
            get(&state, odd_width).await,
            Err(AppError::BadRequest(_))
        ));
    }
}
