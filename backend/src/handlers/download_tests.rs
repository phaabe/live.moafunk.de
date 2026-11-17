//! Admin downloads against a fake R2 and real SQLite: which files each ZIP
//! holds, their names, missing R2 objects and the admin-only rule.

use super::*;
use crate::broadcast_tests::{add_user, headers, state_with};
use crate::fake_r2::FakeR2;
use axum::{body::Body, http::HeaderMap};
use serde_json::json;
use std::io::Read;

async fn setup() -> (Arc<AppState>, tempfile::TempDir, FakeR2, HeaderMap) {
    let (r2, endpoint) = FakeR2::serve().await;
    let (state, temp) = state_with(json!({ "r2_endpoint": endpoint })).await;
    let admin = headers(&state, add_user(&state, "admin", "admin").await).await;
    (state, temp, r2, admin)
}

fn request(user: &HeaderMap) -> Request<Body> {
    let mut request = Request::builder().body(Body::empty()).unwrap();
    request.headers_mut().extend(user.clone());
    request
}

/// A small real PNG, so the collage and the PDF can decode it.
fn png() -> Vec<u8> {
    let mut data = Vec::new();
    image::DynamicImage::new_rgb8(8, 8)
        .write_to(
            &mut std::io::Cursor::new(&mut data),
            image::ImageFormat::Png,
        )
        .unwrap();
    data
}

/// An artist with every file key set. All files are in R2 except the
/// original track 2, so the ZIPs show how a missing object is handled.
async fn add_artist(state: &Arc<AppState>, r2: &FakeR2, name: &str) -> i64 {
    let id: i64 = sqlx::query_scalar(
        "INSERT INTO artists (name, pronouns, track1_name, track2_name, instagram, mentions) \
         VALUES (?, 'she/her', 'One: Intro', 'Two', '@nova', 'Thanks') RETURNING id",
    )
    .bind(name)
    .fetch_one(&state.db)
    .await
    .unwrap();
    let keys = [
        ("pic_key", format!("a/{id}/pic.jpeg")),
        ("pic_cropped_key", format!("a/{id}/crop.png")),
        ("pic_overlay_key", format!("a/{id}/overlay.png")),
        ("voice_message_key", format!("a/{id}/voice.mp3")),
        ("voice_original_key", format!("a/{id}/voice.m4a")),
        ("track1_key", format!("a/{id}/one.mp3")),
        ("track1_original_key", format!("a/{id}/one.wav")),
        ("track2_key", format!("a/{id}/two.mp3")),
        ("track2_original_key", format!("a/{id}/two.flac")),
    ];
    for (column, key) in &keys {
        sqlx::query(&format!("UPDATE artists SET {column} = ? WHERE id = ?"))
            .bind(key)
            .bind(id)
            .execute(&state.db)
            .await
            .unwrap();
        if *column == "track2_original_key" {
            continue;
        }
        let data = if key.ends_with(".png") || key.ends_with(".jpeg") {
            png()
        } else {
            key.as_bytes().to_vec()
        };
        r2.insert(state, key, &data);
    }
    id
}

async fn add_show(state: &Arc<AppState>, artists: &[i64]) -> i64 {
    let show: i64 = sqlx::query_scalar(
        "INSERT INTO shows (title, date) VALUES ('Late/Night?', '2026-11-20T20:00:00Z') RETURNING id",
    )
    .fetch_one(&state.db)
    .await
    .unwrap();
    for artist in artists {
        sqlx::query("INSERT INTO artist_show_assignments (artist_id, show_id) VALUES (?, ?)")
            .bind(artist)
            .bind(show)
            .execute(&state.db)
            .await
            .unwrap();
    }
    show
}

/// The file name from Content-Disposition and the ZIP entries by path.
async fn zip(response: Result<Response>) -> (String, Vec<(String, Vec<u8>)>) {
    let response = response.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(response.headers()[header::CONTENT_TYPE], "application/zip");
    let disposition = response.headers()[header::CONTENT_DISPOSITION]
        .to_str()
        .unwrap()
        .to_string();
    let name = disposition
        .strip_prefix("attachment; filename=\"")
        .and_then(|s| s.strip_suffix('"'))
        .unwrap()
        .to_string();
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let mut archive = zip::ZipArchive::new(std::io::Cursor::new(body)).unwrap();
    let mut entries = Vec::new();
    for i in 0..archive.len() {
        let mut file = archive.by_index(i).unwrap();
        let mut data = Vec::new();
        file.read_to_end(&mut data).unwrap();
        entries.push((file.name().to_string(), data));
    }
    (name, entries)
}

fn paths(entries: &[(String, Vec<u8>)]) -> Vec<&str> {
    entries.iter().map(|(p, _)| p.as_str()).collect()
}

fn text<'a>(entries: &'a [(String, Vec<u8>)], path: &str) -> &'a str {
    let (_, data) = entries.iter().find(|(p, _)| p == path).unwrap();
    std::str::from_utf8(data).unwrap()
}

// ── Pure helpers ────────────────────────────────────────────────────────────

#[test]
fn package_names_and_aliases() {
    for (input, package) in [
        ("recording", ShowPackage::Recording),
        ("REC", ShowPackage::Recording),
        (" social ", ShowPackage::SocialMedia),
        ("social-media", ShowPackage::SocialMedia),
        ("media", ShowPackage::SocialMedia),
        ("all-data", ShowPackage::AllData),
        ("all", ShowPackage::AllData),
        ("material", ShowPackage::AllData),
        ("all-material", ShowPackage::AllData),
    ] {
        assert_eq!(ShowPackage::parse(input), Some(package), "{input}");
    }
    assert_eq!(ShowPackage::parse("everything"), None);
    assert_eq!(ShowPackage::parse(""), None);
}

#[test]
fn file_names_drop_unsafe_characters() {
    assert_eq!(
        sanitize_filename(r#" a<b>c:d"e/f\g|h?i*j "#),
        "a_b_c_d_e_f_g_h_i_j"
    );
    assert_eq!(sanitize_filename("Ünïcode ok"), "Ünïcode ok");
    assert_eq!(
        audio_filename("A/B", "T1", "Song?", "mp3"),
        "A_B - T1 - Song_.mp3"
    );
    assert_eq!(audio_filename("A", "T2", "  ", "wav"), "A - T2.wav");
}

// ── Artist downloads ────────────────────────────────────────────────────────

/// The full artist ZIP: every picture, audio file and original, plus
/// info.txt. A file missing in R2 is left out without an error.
#[tokio::test]
async fn artist_zip_holds_all_files_and_info() {
    let (state, _temp, r2, admin) = setup().await;
    let id = add_artist(&state, &r2, "Nova: Live").await;
    let (name, entries) =
        zip(download_artist(State(state.clone()), Path(id), request(&admin)).await).await;
    assert_eq!(name, format!("UNHEARD_artist_Nova_ Live_{id}.zip"));
    assert_eq!(
        paths(&entries),
        vec![
            "Nova_ Live/artist_pic_original.jpeg",
            "Nova_ Live/artist_pic_cropped.png",
            "Nova_ Live/artist_pic_overlay.png",
            "Nova_ Live/voice_message.mp3",
            "Nova_ Live/originals/voice_message_original.m4a",
            "Nova_ Live/track1_One_ Intro.mp3",
            "Nova_ Live/originals/track1_One_ Intro_original.wav",
            "Nova_ Live/track2_Two.mp3",
            "Nova_ Live/info.txt",
        ]
    );
    assert_eq!(
        text(&entries, "Nova_ Live/track2_Two.mp3"),
        format!("a/{id}/two.mp3")
    );
    let info = text(&entries, "Nova_ Live/info.txt");
    assert!(
        info.starts_with(
            "Artist: Nova: Live\nPronouns: she/her\nStatus: unassigned\nAssigned Show: N/A\n"
        ),
        "{info}"
    );
    assert!(info.contains("Voice Message: Uploaded"), "{info}");
    assert!(
        info.contains("- Instagram: @nova\n- SoundCloud: N/A"),
        "{info}"
    );
    assert!(info.ends_with("Things to Mention:\nThanks\n"), "{info}");

    // Assigned and opted out of the voice message.
    let show = add_show(&state, &[id]).await;
    sqlx::query("UPDATE artists SET no_voice_message = 1 WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await
        .unwrap();
    let (_, entries) =
        zip(download_artist(State(state.clone()), Path(id), request(&admin)).await).await;
    let info = text(&entries, "Nova_ Live/info.txt");
    assert!(
        info.contains("Assigned Show: Late/Night? (2026-11-20T20:00:00Z)"),
        "{info} {show}"
    );
    assert!(info.contains("Voice Message: Artist opted out"), "{info}");
}

#[tokio::test]
async fn artist_audio_and_image_zips() {
    let (state, _temp, r2, admin) = setup().await;
    let id = add_artist(&state, &r2, "Nova").await;

    let (name, entries) =
        zip(download_artist_audio(State(state.clone()), Path(id), request(&admin)).await).await;
    assert_eq!(name, "UNHEARD_artist_Nova_audio.zip");
    assert_eq!(
        paths(&entries),
        vec![
            "Nova/voice_message.mp3",
            "Nova/track1_One_ Intro.mp3",
            "Nova/track2_Two.mp3"
        ]
    );

    let (name, entries) =
        zip(download_artist_images(State(state.clone()), Path(id), request(&admin)).await).await;
    assert_eq!(name, "UNHEARD_artist_Nova_images.zip");
    assert_eq!(
        paths(&entries),
        vec![
            "Nova/artist_pic_original.jpeg",
            "Nova/artist_pic_cropped.png",
            "Nova/artist_pic_overlay.png"
        ]
    );

    // With nothing in R2 the ZIPs are empty, not an error.
    r2.objects.lock().unwrap().clear();
    let (_, entries) =
        zip(download_artist_images(State(state.clone()), Path(id), request(&admin)).await).await;
    assert!(entries.is_empty());

    let response = download_artist_pdf(State(state.clone()), Path(id), request(&admin))
        .await
        .unwrap();
    assert_eq!(
        response.headers()[header::CONTENT_DISPOSITION],
        "attachment; filename=\"UNHEARD_artist_Nova_info.pdf\""
    );
}

#[tokio::test]
async fn unknown_artist_is_not_found() {
    let (state, _temp, _r2, admin) = setup().await;
    let superadmin = headers(&state, add_user(&state, "root", "superadmin").await).await;
    for user in [&admin, &superadmin] {
        assert!(matches!(
            download_artist(State(state.clone()), Path(999), request(user)).await,
            Err(AppError::NotFound(_))
        ));
    }
}

// ── Show packages ───────────────────────────────────────────────────────────

async fn package(
    state: &Arc<AppState>,
    user: &HeaderMap,
    show: i64,
    name: &str,
) -> Result<Response> {
    download_show_package(
        State(state.clone()),
        Path((show, name.to_string())),
        request(user),
    )
    .await
}

#[tokio::test]
async fn show_package_errors() {
    let (state, _temp, _r2, admin) = setup().await;
    assert!(matches!(
        package(&state, &admin, 1, "everything").await,
        Err(AppError::Validation(_))
    ));
    assert!(matches!(
        package(&state, &admin, 999, "rec").await,
        Err(AppError::NotFound(_))
    ));
    let empty = add_show(&state, &[]).await;
    assert!(matches!(
        package(&state, &admin, empty, "rec").await,
        Err(AppError::Validation(msg)) if msg == "No artists assigned to this show"
    ));
}

/// The recording package: one PDF with all artists and their audio, named
/// "Artist - T0/T1/T2 - title".
#[tokio::test]
async fn recording_package_has_pdf_and_named_audio() {
    let (state, _temp, r2, admin) = setup().await;
    let nova = add_artist(&state, &r2, "Nova").await;
    let kai = add_artist(&state, &r2, "Kai").await;
    // Kai's track 2 is missing in R2.
    r2.objects
        .lock()
        .unwrap()
        .remove(&format!("{}/a/{kai}/two.mp3", state.config.r2_bucket_name));
    let show = add_show(&state, &[nova, kai]).await;

    let (name, entries) = zip(package(&state, &admin, show, "recording").await).await;
    assert_eq!(name, "UNHEARD_2026-11-20_Late_Night__recording.zip");
    assert_eq!(
        paths(&entries),
        vec![
            "artists.pdf",
            "audio/Kai - T0 - voicemail.mp3",
            "audio/Kai - T1 - One_ Intro.mp3",
            "audio/Nova - T0 - voicemail.mp3",
            "audio/Nova - T1 - One_ Intro.mp3",
            "audio/Nova - T2 - Two.mp3",
        ]
    );
    assert!(text_bytes(&entries, "artists.pdf").starts_with(b"%PDF"));
}

fn text_bytes<'a>(entries: &'a [(String, Vec<u8>)], path: &str) -> &'a [u8] {
    &entries.iter().find(|(p, _)| p == path).unwrap().1
}

/// The social media package: the 2x2 collage plus each artist's overlay.
#[tokio::test]
async fn social_media_package_has_collage_and_overlays() {
    let (state, _temp, r2, admin) = setup().await;
    let nova = add_artist(&state, &r2, "Nova").await;
    let kai = add_artist(&state, &r2, "Kai").await;
    sqlx::query("UPDATE artists SET pic_overlay_key = NULL WHERE id = ?")
        .bind(kai)
        .execute(&state.db)
        .await
        .unwrap();
    let show = add_show(&state, &[nova, kai]).await;

    let (name, entries) = zip(package(&state, &admin, show, "social").await).await;
    assert_eq!(name, "UNHEARD_2026-11-20_Late_Night__social-media.zip");
    assert_eq!(
        paths(&entries),
        vec!["cover_overall.png", "cover_artist/Nova.png"]
    );
    let collage = image::load_from_memory(text_bytes(&entries, "cover_overall.png")).unwrap();
    assert_eq!((collage.width(), collage.height()), (2048, 2048));
}

/// The full package: one folder per artist with every file and info.txt,
/// plus the collage as cover.png.
#[tokio::test]
async fn all_data_package_has_a_folder_per_artist() {
    let (state, _temp, r2, admin) = setup().await;
    let nova = add_artist(&state, &r2, "Nova").await;
    let show = add_show(&state, &[nova]).await;

    let (name, entries) = zip(package(&state, &admin, show, "all").await).await;
    assert_eq!(name, "UNHEARD_2026-11-20_Late_Night__all-data.zip");
    assert_eq!(
        paths(&entries),
        vec![
            "Nova/artist_pic_original.jpeg",
            "Nova/artist_pic_cropped.png",
            "Nova/artist_pic_overlay.png",
            "Nova/voice_message.mp3",
            "Nova/originals/voice_message_original.m4a",
            "Nova/track1_One_ Intro.mp3",
            "Nova/originals/track1_One_ Intro_original.wav",
            "Nova/track2_Two.mp3",
            "Nova/info.txt",
            "cover.png",
        ]
    );
    let info = text(&entries, "Nova/info.txt");
    assert!(
        info.starts_with("Artist: Nova\nPronouns: she/her\n\nTrack 1: One: Intro\n"),
        "{info}"
    );
}

/// Two artists with the same name must not break the show ZIP.
#[tokio::test]
#[ignore = "https://github.com/phaabe/live.moafunk.de/issues/936"]
async fn same_artist_names_still_give_a_show_zip() {
    let (state, _temp, r2, admin) = setup().await;
    let a = add_artist(&state, &r2, "Twin").await;
    let b = add_artist(&state, &r2, "Twin").await;
    let show = add_show(&state, &[a, b]).await;
    for name in ["recording", "social", "all"] {
        let (_, entries) = zip(package(&state, &admin, show, name).await).await;
        assert!(!entries.is_empty(), "{name}");
    }
}
