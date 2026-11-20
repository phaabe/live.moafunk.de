//! Show series API against real handlers and SQLite.

use super::*;
use crate::broadcast_tests::{add_user, headers, state};
use axum::{
    body::{to_bytes, Body},
    extract::{FromRequest, Multipart, Path},
    http::{Request, StatusCode},
};
use serde_json::{json, Value};

const BOUNDARY: &str = "series-test-boundary";

/// A multipart body with the given text fields.
async fn form(fields: &[(&str, &str)]) -> Multipart {
    form_with_cover(fields, None).await
}

/// A multipart body with text fields and, optionally, a `cover` file.
async fn form_with_cover(fields: &[(&str, &str)], cover: Option<&str>) -> Multipart {
    let mut body = String::new();
    for (name, value) in fields {
        body.push_str(&format!(
            "--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n"
        ));
    }
    if let Some(cover) = cover {
        body.push_str(&format!(
            "--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"cover\"; filename=\"c.png\"\r\n\
             Content-Type: image/png\r\n\r\n{cover}\r\n"
        ));
    }
    body.push_str(&format!("--{BOUNDARY}--\r\n"));
    let request = Request::builder()
        .header(
            "content-type",
            format!("multipart/form-data; boundary={BOUNDARY}"),
        )
        .body(Body::from(body))
        .unwrap();
    Multipart::from_request(request, &()).await.unwrap()
}

const BRUNCHTIME: &[(&str, &str)] = &[
    ("name", "Brunchtime"),
    ("description", "Sunday brunch sounds."),
    ("repeat_rule", "weekly"),
    ("weekday", "7"),
    ("start_time", "12:00"),
    ("end_time", "14:00"),
];

async fn json_of(response: impl IntoResponse) -> (StatusCode, Value) {
    let response = response.into_response();
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 1024 * 1024).await.unwrap();
    (status, serde_json::from_slice(&bytes).unwrap())
}

async fn create(state: &Arc<AppState>, user: i64, fields: &[(&str, &str)]) -> Result<Value> {
    let response = handlers::series::api_create_series(
        State(state.clone()),
        headers(state, user).await,
        form(fields).await,
    )
    .await?;
    let (status, body) = json_of(response).await;
    assert_eq!(status, StatusCode::CREATED);
    Ok(body)
}

async fn get(state: &Arc<AppState>, user: i64, slug: &str) -> Result<Value> {
    let response = handlers::series::api_get_series(
        State(state.clone()),
        Path(slug.to_string()),
        headers(state, user).await,
    )
    .await?;
    Ok(json_of(response).await.1)
}

async fn list(state: &Arc<AppState>, user: i64) -> Value {
    let response =
        handlers::series::api_list_series(State(state.clone()), headers(state, user).await)
            .await
            .unwrap();
    json_of(response).await.1
}

async fn episode(state: &Arc<AppState>, series_id: i64, date: &str, mode: &str) -> i64 {
    sqlx::query(
        "INSERT INTO shows (title, date, start_time, show_type, stream_mode, template_id) \
         VALUES ('Brunchtime', ?, '12:00', 'external', ?, ?)",
    )
    .bind(date)
    .bind(mode)
    .bind(series_id)
    .execute(&state.db)
    .await
    .unwrap()
    .last_insert_rowid()
}

#[tokio::test]
async fn host_creates_a_series_and_sees_its_episodes_by_slug() {
    let (state, _temp) = state().await;
    let host = add_user(&state, "host", "host").await;

    let created = create(&state, host, BRUNCHTIME).await.unwrap();
    assert_eq!(created["slug"], "brunchtime");
    assert_eq!(created["description"], "Sunday brunch sounds.");
    assert_eq!(created["rhythm_line"], "Sundays · 12:00–14:00");
    assert_eq!(
        created["rhythm"],
        json!({"repeat_rule": "weekly", "weekday": 7, "start_time": "12:00", "end_time": "14:00"})
    );
    let series_id = created["id"].as_i64().unwrap();

    let listed = list(&state, host).await;
    assert_eq!(listed["series"].as_array().unwrap().len(), 1);
    assert_eq!(listed["series"][0]["rhythm_line"], "Sundays · 12:00–14:00");

    // POST /api/shows with a template_id keeps the link on the new date.
    let request = serde_json::from_value(json!({
        "title": "Brunchtime #1", "date": "2099-01-04", "start_time": "12:00",
        "template_id": series_id
    }))
    .unwrap();
    let response = handlers::api::api_create_show(
        State(state.clone()),
        headers(&state, host).await,
        axum::Json(request),
    )
    .await
    .unwrap();
    let (status, show) = json_of(response).await;
    assert_eq!(status, StatusCode::CREATED);
    let upcoming = show["id"].as_i64().unwrap();
    let linked: Option<i64> = sqlx::query_scalar("SELECT template_id FROM shows WHERE id = ?")
        .bind(upcoming)
        .fetch_one(&state.db)
        .await
        .unwrap();
    assert_eq!(linked, Some(series_id));

    let open = episode(&state, series_id, "2020-01-12", "live").await;
    let finished = episode(&state, series_id, "2020-01-05", "prerecorded").await;
    sqlx::query("UPDATE shows SET wrapup_published_at = datetime('now') WHERE id = ?")
        .bind(finished)
        .execute(&state.db)
        .await
        .unwrap();
    // A date of another series is not listed.
    let other = create(
        &state,
        host,
        &[
            ("name", "Night"),
            ("repeat_rule", "biweekly"),
            ("weekday", "5"),
            ("start_time", "22:00"),
            ("end_time", "23:30"),
        ],
    )
    .await
    .unwrap();
    assert_eq!(other["rhythm_line"], "Every other Friday · 22:00–23:30");
    episode(&state, other["id"].as_i64().unwrap(), "2099-01-02", "live").await;

    let detail = get(&state, host, "brunchtime").await.unwrap();
    assert_eq!(detail["name"], "Brunchtime");
    let rows: Vec<(i64, String, String, String)> = detail["episodes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| {
            (
                e["id"].as_i64().unwrap(),
                e["mode_label"].as_str().unwrap().to_string(),
                e["status"].as_str().unwrap().to_string(),
                e["status_label"].as_str().unwrap().to_string(),
            )
        })
        .collect();
    let row = |id, mode: &str, status: &str, label: &str| {
        (id, mode.to_string(), status.to_string(), label.to_string())
    };
    assert_eq!(
        rows,
        [
            row(upcoming, "Live", "upcoming", "Upcoming"),
            row(open, "Live", "wrapup_open", "Finished · wrap-up open"),
            row(finished, "Pre-recorded", "finished", "Finished"),
        ]
    );
}

/// Every host may view a series (read-only); only its owner lists it, and
/// only the owner or an admin may manage it.
#[tokio::test]
async fn every_host_views_a_series_and_only_its_owner_lists_it() {
    let (state, _temp) = state().await;
    let alice = add_user(&state, "alice", "host").await;
    let bob = add_user(&state, "bob", "host").await;
    let admin = add_user(&state, "admin", "admin").await;
    let guest = add_user(&state, "guest", "guest").await;
    create(&state, alice, BRUNCHTIME).await.unwrap();

    for (user, can_edit) in [(alice, true), (bob, false), (admin, true)] {
        let detail = get(&state, user, "brunchtime").await.unwrap();
        assert_eq!(detail["name"], "Brunchtime");
        assert_eq!(detail["can_edit"], can_edit);
        assert_eq!(detail["hosts"], json!([{ "username": "alice" }]));
    }
    for user in [bob, admin] {
        assert_eq!(list(&state, user).await["series"], json!([]));
    }
    assert!(matches!(
        get(&state, bob, "no-such-show").await,
        Err(AppError::NotFound(_))
    ));
    assert!(matches!(
        get(&state, guest, "brunchtime").await,
        Err(AppError::Forbidden(_))
    ));
    assert!(matches!(
        create(&state, guest, BRUNCHTIME).await,
        Err(AppError::Forbidden(_))
    ));

    // Bob's own "Brunchtime" gets the next free slug.
    let bobs = create(&state, bob, BRUNCHTIME).await.unwrap();
    assert_eq!(bobs["slug"], "brunchtime-2");
    assert_eq!(get(&state, alice, "brunchtime").await.unwrap()["id"], 1);
}

/// "No fixed rhythm": no day or times, and no rhythm line.
#[tokio::test]
async fn create_without_a_fixed_rhythm() {
    let (state, _temp) = state().await;
    let host = add_user(&state, "host", "host").await;
    let created = create(
        &state,
        host,
        &[("name", "Most Silent Stepper"), ("repeat_rule", "none")],
    )
    .await
    .unwrap();
    assert_eq!(created["slug"], "most-silent-stepper");
    assert_eq!(created["rhythm"], Value::Null);
    assert_eq!(created["rhythm_line"], Value::Null);
}

#[tokio::test]
async fn create_rejects_a_missing_name_or_a_bad_rhythm() {
    let (state, _temp) = state().await;
    let host = add_user(&state, "host", "host").await;
    let with = |key: &str, value: &'static str| -> Vec<(String, &'static str)> {
        BRUNCHTIME
            .iter()
            .map(|(k, v)| (k.to_string(), if *k == key { value } else { *v }))
            .collect()
    };
    let cases = [
        with("name", "   "),
        with("repeat_rule", "monthly"),
        with("weekday", "0"),
        with("weekday", "8"),
        with("start_time", "9:00"),
        with("end_time", "12:00"),
        with("end_time", "11:00"),
    ];
    for fields in cases {
        let fields: Vec<(&str, &str)> = fields.iter().map(|(k, v)| (k.as_str(), *v)).collect();
        assert!(
            matches!(
                create(&state, host, &fields).await,
                Err(AppError::BadRequest(_))
            ),
            "{fields:?}"
        );
    }
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM show_templates")
        .fetch_one(&state.db)
        .await
        .unwrap();
    assert_eq!(count, 0);
}

/// The admin ShowWizard keeps creating and listing templates as before; the
/// new template is a series with a slug.
#[tokio::test]
async fn wizard_templates_get_a_slug_and_still_list() {
    let (state, _temp) = state().await;
    let admin = add_user(&state, "admin", "admin").await;
    let request = serde_json::from_value(json!({"name": "New", "description": "x"})).unwrap();
    let response = handlers::api::api_create_show_template(
        State(state.clone()),
        headers(&state, admin).await,
        axum::Json(request),
    )
    .await
    .unwrap();
    let (status, created) = json_of(response).await;
    assert_eq!(status, StatusCode::CREATED);
    assert_eq!(created["slug"], "show-new");

    let response =
        handlers::api::api_list_show_templates(State(state.clone()), headers(&state, admin).await)
            .await
            .unwrap();
    let (_, templates) = json_of(response).await;
    assert_eq!(templates["templates"][0]["name"], "New");
    let detail = get(&state, admin, "show-new").await.unwrap();
    assert_eq!(detail["rhythm"], Value::Null);
    assert_eq!(detail["rhythm_line"], Value::Null);
}

/// R2 is unreachable in tests, so the cover upload fails: no series is left.
#[tokio::test]
async fn a_failed_cover_upload_leaves_no_series() {
    let (state, _temp) = state().await;
    let host = add_user(&state, "host", "host").await;
    let result = handlers::series::api_create_series(
        State(state.clone()),
        headers(&state, host).await,
        form_with_cover(BRUNCHTIME, Some("not-really-a-png")).await,
    )
    .await;
    assert!(matches!(result, Err(AppError::Storage(_))));
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM show_templates")
        .fetch_one(&state.db)
        .await
        .unwrap();
    assert_eq!(count, 0);
}

/// Episode rows carry the set file and the SoundCloud state, so the Show page
/// can show the recording state of past episodes.
#[tokio::test]
async fn episodes_carry_their_file_and_soundcloud_state() {
    let (state, _temp) = state().await;
    let host = add_user(&state, "host", "host").await;
    let other = add_user(&state, "other", "host").await;
    let series_id = create(&state, host, BRUNCHTIME).await.unwrap()["id"]
        .as_i64()
        .unwrap();
    let set = episode(&state, series_id, "2099-01-11", "prerecorded").await;
    let no_set = episode(&state, series_id, "2099-01-04", "prerecorded").await;
    let public = episode(&state, series_id, "2020-01-12", "live").await;
    let private = episode(&state, series_id, "2020-01-05", "live").await;
    let none = episode(&state, series_id, "2019-12-29", "live").await;
    for (sql, id) in [
        (
            "UPDATE shows SET prerecorded_filename = 'set.mp3' WHERE id = ?",
            set,
        ),
        (
            "UPDATE shows SET soundcloud_url = 'https://soundcloud.com/x/1', \
             soundcloud_public = 1 WHERE id = ?",
            public,
        ),
        (
            "UPDATE shows SET soundcloud_url = 'https://soundcloud.com/x/2', \
             soundcloud_public = 0 WHERE id = ?",
            private,
        ),
    ] {
        sqlx::query(sql).bind(id).execute(&state.db).await.unwrap();
    }

    // Another host sees the same rows.
    for user in [host, other] {
        let detail = get(&state, user, "brunchtime").await.unwrap();
        let rows: Vec<(i64, bool, Value)> = detail["episodes"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| {
                (
                    e["id"].as_i64().unwrap(),
                    e["has_file"].as_bool().unwrap(),
                    e["soundcloud"].clone(),
                )
            })
            .collect();
        assert_eq!(
            rows,
            [
                (set, true, Value::Null),
                (no_set, false, Value::Null),
                (public, false, json!("public")),
                (private, false, json!("private")),
                (none, false, Value::Null),
            ]
        );
    }
}

/// The end time never cuts a show: the episode on air stays open past it.
#[tokio::test]
async fn an_episode_on_air_is_not_over_at_its_end_time() {
    let (state, _temp) = state().await;
    let host = add_user(&state, "host", "host").await;
    let series = create(&state, host, BRUNCHTIME).await.unwrap();
    let series_id = series["id"].as_i64().unwrap();
    let on_air = episode(&state, series_id, "2020-01-12", "live").await;
    let over = episode(&state, series_id, "2020-01-05", "live").await;
    state
        .recording_manager
        .lock()
        .await
        .start(on_air)
        .await
        .unwrap();

    let detail = get(&state, host, "brunchtime").await.unwrap();
    let status = |id: i64| {
        detail["episodes"]
            .as_array()
            .unwrap()
            .iter()
            .find(|e| e["id"] == id)
            .unwrap()["status"]
            .clone()
    };
    assert_eq!(status(on_air), "upcoming");
    assert_eq!(status(over), "wrapup_open");
}

/// The host Dashboard lists every host's upcoming episodes, soonest first.
#[tokio::test]
async fn dashboard_lists_all_hosts_upcoming_episodes() {
    let (state, _temp) = state().await;
    let lena = add_user(&state, "lena", "host").await;
    let jonas = add_user(&state, "jonas", "host").await;
    let brunch = create(&state, lena, BRUNCHTIME).await.unwrap()["id"]
        .as_i64()
        .unwrap();
    let night = create(
        &state,
        jonas,
        &[("name", "Night Shift"), ("repeat_rule", "none")],
    )
    .await
    .unwrap()["id"]
        .as_i64()
        .unwrap();

    let later = episode(&state, brunch, "2099-01-04", "prerecorded").await;
    let sooner = episode(&state, brunch, "2099-01-01", "live").await;
    let night_episode = episode(&state, night, "2099-01-02", "live").await;
    // Over: an old date, and yesterday's episode (it ended at 14:00).
    episode(&state, brunch, "2020-01-05", "live").await;
    let yesterday = (chrono::Utc::now() - chrono::Duration::days(1))
        .format("%Y-%m-%d")
        .to_string();
    episode(&state, brunch, &yesterday, "live").await;
    // A date outside any series is not a host episode.
    sqlx::query(
        "INSERT INTO shows (title, date, show_type) VALUES ('UNHEARD', '2099-01-03', 'unheard')",
    )
    .execute(&state.db)
    .await
    .unwrap();

    let response =
        handlers::series::api_upcoming_episodes(State(state.clone()), headers(&state, lena).await)
            .await
            .unwrap();
    let (status, body) = json_of(response).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        body["episodes"],
        json!([
            {
                "date": "2099-01-01", "start_time": "12:00", "end_time": "14:00",
                "title": "Brunchtime", "show_name": "Brunchtime", "series_slug": "brunchtime",
                "host_username": "lena", "mode": "live", "mode_label": "Live", "id": sooner
            },
            {
                "date": "2099-01-02", "start_time": "12:00", "end_time": null,
                "title": "Brunchtime", "show_name": "Night Shift", "series_slug": "night-shift",
                "host_username": "jonas", "mode": "live", "mode_label": "Live",
                "id": night_episode
            },
            {
                "date": "2099-01-04", "start_time": "12:00", "end_time": "14:00",
                "title": "Brunchtime", "show_name": "Brunchtime", "series_slug": "brunchtime",
                "host_username": "lena", "mode": "prerecorded", "mode_label": "Pre-recorded",
                "id": later
            },
        ])
    );
}

/// Publish an episode's wrap-up, so it is Finished.
async fn publish_wrapup(state: &Arc<AppState>, id: i64) {
    sqlx::query("UPDATE shows SET wrapup_published_at = '2020-01-06T12:00:00Z' WHERE id = ?")
        .bind(id)
        .execute(&state.db)
        .await
        .unwrap();
}

/// The Dashboard's Past tab lists every host's past episodes, newest first,
/// with their status; upcoming and on-air episodes stay out.
#[tokio::test]
async fn dashboard_lists_all_hosts_past_episodes() {
    let (state, _temp) = state().await;
    let lena = add_user(&state, "lena", "host").await;
    let jonas = add_user(&state, "jonas", "host").await;
    let brunch = create(&state, lena, BRUNCHTIME).await.unwrap()["id"]
        .as_i64()
        .unwrap();
    let night = create(
        &state,
        jonas,
        &[("name", "Night Shift"), ("repeat_rule", "none")],
    )
    .await
    .unwrap()["id"]
        .as_i64()
        .unwrap();

    let older = episode(&state, brunch, "2020-01-05", "prerecorded").await;
    publish_wrapup(&state, older).await;
    let newer = episode(&state, night, "2020-01-07", "live").await;
    let on_air = episode(&state, brunch, "2020-01-12", "live").await;
    state
        .recording_manager
        .lock()
        .await
        .start(on_air)
        .await
        .unwrap();
    episode(&state, brunch, "2099-01-04", "live").await;
    // A date outside any series is not a host episode.
    sqlx::query(
        "INSERT INTO shows (title, date, show_type) VALUES ('UNHEARD', '2020-01-06', 'unheard')",
    )
    .execute(&state.db)
    .await
    .unwrap();

    let response =
        handlers::series::api_past_episodes(State(state.clone()), headers(&state, lena).await)
            .await
            .unwrap();
    let (status, body) = json_of(response).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        body["episodes"],
        json!([
            {
                "date": "2020-01-07", "start_time": "12:00", "end_time": null,
                "title": "Brunchtime", "show_name": "Night Shift", "series_slug": "night-shift",
                "host_username": "jonas", "mode": "live", "mode_label": "Live", "id": newer,
                "status": "wrapup_open", "status_label": "Finished · wrap-up open"
            },
            {
                "date": "2020-01-05", "start_time": "12:00", "end_time": "14:00",
                "title": "Brunchtime", "show_name": "Brunchtime", "series_slug": "brunchtime",
                "host_username": "lena", "mode": "prerecorded", "mode_label": "Pre-recorded",
                "id": older, "status": "finished", "status_label": "Finished"
            },
        ])
    );
}

/// The Dashboard's Shows grid: every host's Show with its host and its
/// finished episodes (wrap-up open or finished), most first, then by name.
#[tokio::test]
async fn dashboard_lists_all_shows_by_finished_episodes() {
    let (state, _temp) = state().await;
    let lena = add_user(&state, "lena", "host").await;
    let jonas = add_user(&state, "jonas", "host").await;
    let brunch = create(&state, lena, BRUNCHTIME).await.unwrap()["id"]
        .as_i64()
        .unwrap();
    let night = create(
        &state,
        jonas,
        &[("name", "Night Shift"), ("repeat_rule", "none")],
    )
    .await
    .unwrap()["id"]
        .as_i64()
        .unwrap();
    let dub = create(
        &state,
        jonas,
        &[("name", "Dub Kitchen"), ("repeat_rule", "none")],
    )
    .await
    .unwrap()["id"]
        .as_i64()
        .unwrap();
    create(
        &state,
        lena,
        &[("name", "Ambient"), ("repeat_rule", "none")],
    )
    .await
    .unwrap();

    // Night Shift: two over (one still in wrap-up) and one upcoming.
    let done = episode(&state, night, "2020-01-01", "live").await;
    publish_wrapup(&state, done).await;
    episode(&state, night, "2020-01-08", "live").await;
    episode(&state, night, "2099-01-01", "live").await;
    // Brunchtime: one over; the one on air and the upcoming one do not count.
    episode(&state, brunch, "2020-01-05", "live").await;
    let on_air = episode(&state, brunch, "2020-01-12", "live").await;
    state
        .recording_manager
        .lock()
        .await
        .start(on_air)
        .await
        .unwrap();
    episode(&state, brunch, "2099-01-04", "live").await;
    // Dub Kitchen: one over, the same count as Brunchtime, so the name decides.
    episode(&state, dub, "2020-01-03", "prerecorded").await;

    let response =
        handlers::series::api_series_overview(State(state.clone()), headers(&state, lena).await)
            .await
            .unwrap();
    let (status, body) = json_of(response).await;
    assert_eq!(status, StatusCode::OK);
    let tiles: Vec<(String, String, i64)> = body["series"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| {
            (
                s["name"].as_str().unwrap().to_string(),
                s["host_username"].as_str().unwrap().to_string(),
                s["finished_episodes"].as_i64().unwrap(),
            )
        })
        .collect();
    let tile = |name: &str, host: &str, n: i64| (name.to_string(), host.to_string(), n);
    assert_eq!(
        tiles,
        vec![
            tile("Night Shift", "jonas", 2),
            tile("Brunchtime", "lena", 1),
            tile("Dub Kitchen", "jonas", 1),
            tile("Ambient", "lena", 0),
        ]
    );
    assert_eq!(body["series"][0]["slug"], "night-shift");
    assert_eq!(body["series"][0]["cover_url"], Value::Null);
}

/// Guests do not see the Dashboard's lists.
#[tokio::test]
async fn dashboard_lists_need_a_host() {
    let (state, _temp) = state().await;
    let guest = add_user(&state, "guest", "guest").await;
    let past =
        handlers::series::api_past_episodes(State(state.clone()), headers(&state, guest).await)
            .await;
    assert!(matches!(past, Err(AppError::Forbidden(_))));
    let shows =
        handlers::series::api_series_overview(State(state.clone()), headers(&state, guest).await)
            .await;
    assert!(matches!(shows, Err(AppError::Forbidden(_))));
}

/// Another host may view an episode: it carries the Show's name and
/// description, the status label and the host.
#[tokio::test]
async fn another_host_sees_an_episode_with_its_show() {
    let (state, _temp) = state().await;
    let lena = add_user(&state, "lena", "host").await;
    let jonas = add_user(&state, "jonas", "host").await;
    let series = create(&state, lena, BRUNCHTIME).await.unwrap()["id"]
        .as_i64()
        .unwrap();
    let id = episode(&state, series, "2099-01-04", "live").await;
    sqlx::query("UPDATE shows SET host_user_id = ? WHERE id = ?")
        .bind(lena)
        .bind(id)
        .execute(&state.db)
        .await
        .unwrap();

    let response = handlers::api::api_show_detail(
        State(state.clone()),
        Path(id),
        headers(&state, jonas).await,
    )
    .await
    .unwrap();
    let (status, body) = json_of(response).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["series_name"], "Brunchtime");
    assert_eq!(body["series_description"], "Sunday brunch sounds.");
    assert_eq!(body["series_cover_url"], Value::Null);
    assert_eq!(body["episode_status_label"], "Upcoming");
    assert_eq!(body["host_username"], "lena");
}

/// The owner edits a series in place: the slug stays, the rhythm can go.
/// Another host cannot.
#[tokio::test]
async fn the_owner_edits_a_series_and_keeps_its_slug() {
    let (state, _temp) = state().await;
    let host = add_user(&state, "host", "host").await;
    let other = add_user(&state, "other", "host").await;
    create(&state, host, BRUNCHTIME).await.unwrap();
    let edit = |user: i64, fields: &'static [(&'static str, &'static str)]| {
        let state = state.clone();
        async move {
            handlers::series::api_update_series(
                State(state.clone()),
                Path("brunchtime".to_string()),
                headers(&state, user).await,
                form(fields).await,
            )
            .await
            .map(|r| r.into_response())
        }
    };

    let fields: &[(&str, &str)] = &[
        ("name", "Brunch Club"),
        ("description", "Now every other Saturday."),
        ("repeat_rule", "biweekly"),
        ("weekday", "6"),
        ("start_time", "11:00"),
        ("end_time", "13:00"),
    ];
    let (status, body) = json_of(edit(host, fields).await.unwrap()).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["slug"], "brunchtime");
    assert_eq!(body["name"], "Brunch Club");
    assert_eq!(body["description"], "Now every other Saturday.");
    assert_eq!(body["rhythm_line"], "Every other Saturday · 11:00–13:00");

    let none: &[(&str, &str)] = &[("name", "Brunch Club"), ("repeat_rule", "none")];
    let (_, body) = json_of(edit(host, none).await.unwrap()).await;
    assert_eq!(body["rhythm"], Value::Null);
    assert_eq!(body["description"], Value::Null);

    assert!(matches!(
        edit(other, none).await,
        Err(AppError::NotFound(_))
    ));
    let empty: &[(&str, &str)] = &[("name", " "), ("repeat_rule", "none")];
    assert!(matches!(
        edit(host, empty).await,
        Err(AppError::BadRequest(_))
    ));
}
