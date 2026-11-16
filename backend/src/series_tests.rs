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

#[tokio::test]
async fn series_are_only_visible_to_their_owner() {
    let (state, _temp) = state().await;
    let alice = add_user(&state, "alice", "host").await;
    let bob = add_user(&state, "bob", "host").await;
    let admin = add_user(&state, "admin", "admin").await;
    let guest = add_user(&state, "guest", "guest").await;
    create(&state, alice, BRUNCHTIME).await.unwrap();

    for user in [bob, admin] {
        assert!(matches!(
            get(&state, user, "brunchtime").await,
            Err(AppError::NotFound(_))
        ));
        assert_eq!(list(&state, user).await["series"], json!([]));
    }
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
/// Only the caller's own rows carry the episode id.
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
    episode(&state, night, "2099-01-02", "live").await;
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
                "show_name": "Brunchtime", "host_username": "lena",
                "mode": "live", "mode_label": "Live", "own_episode_id": sooner
            },
            {
                "date": "2099-01-02", "start_time": "12:00", "end_time": null,
                "show_name": "Night Shift", "host_username": "jonas",
                "mode": "live", "mode_label": "Live", "own_episode_id": null
            },
            {
                "date": "2099-01-04", "start_time": "12:00", "end_time": "14:00",
                "show_name": "Brunchtime", "host_username": "lena",
                "mode": "prerecorded", "mode_label": "Pre-recorded", "own_episode_id": later
            },
        ])
    );
}
