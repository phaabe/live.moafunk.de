//! Permission rules and the host's show-day flows in `handlers/api.rs`,
//! against real handlers, SQLite and the fake R2.

use super::*;
use crate::broadcast_tests::{add_user, headers, state};
use crate::submit_download_tests::{form, setup};
use axum::{
    body::to_bytes,
    extract::{Path, Query},
    http::StatusCode,
    Json,
};
use handlers::{api, upload_recording_chunked::ChunkQueryWithShowId};
use serde_json::{json, Value};
use std::future::Future;

const NOBODY: StatusCode = StatusCode::UNAUTHORIZED;
const DENIED: StatusCode = StatusCode::FORBIDDEN;
const OK: StatusCode = StatusCode::OK;

fn status<T: IntoResponse>(result: Result<T>) -> StatusCode {
    result.into_response().status()
}

/// Status and JSON body of a handler result.
async fn reply<T: IntoResponse>(result: Result<T>) -> (StatusCode, Value) {
    let response = result.into_response();
    let status = response.status();
    let body = to_bytes(response.into_body(), 1024 * 1024).await.unwrap();
    (status, serde_json::from_slice(&body).unwrap_or(Value::Null))
}

/// A JSON request body for handlers whose request fields are private.
fn req<T: serde::de::DeserializeOwned>(value: Value) -> Json<T> {
    Json(serde_json::from_value(value).unwrap())
}

/// One user per role, plus a second host.
struct People {
    guest: i64,
    host: i64,
    other_host: i64,
    admin: i64,
    superadmin: i64,
}

async fn people(state: &Arc<AppState>) -> People {
    People {
        guest: add_user(state, "guest", "guest").await,
        host: add_user(state, "host", "host").await,
        other_host: add_user(state, "other", "host").await,
        admin: add_user(state, "admin", "admin").await,
        superadmin: add_user(state, "superadmin", "superadmin").await,
    }
}

/// Login headers, or none for `None`.
async fn login(state: &Arc<AppState>, user: Option<i64>) -> HeaderMap {
    match user {
        Some(id) => headers(state, id).await,
        None => HeaderMap::new(),
    }
}

/// The status each caller gets, in caller order.
async fn statuses<F, Fut, T>(
    state: &Arc<AppState>,
    callers: &[Option<i64>],
    call: F,
) -> Vec<StatusCode>
where
    F: Fn(HeaderMap) -> Fut,
    Fut: Future<Output = Result<T>>,
    T: IntoResponse,
{
    let mut out = Vec::new();
    for caller in callers {
        out.push(status(call(login(state, *caller).await).await));
    }
    out
}

/// An external show on 2099-01-01, without a start time.
async fn add_show(state: &Arc<AppState>, id: i64, host: Option<i64>, creator: Option<i64>) {
    sqlx::query(
        "INSERT INTO shows (id, title, date, show_type, host_user_id, created_by) \
         VALUES (?, 'Night Shift', '2099-01-01', 'external', ?, ?)",
    )
    .bind(id)
    .bind(host)
    .bind(creator)
    .execute(&state.db)
    .await
    .unwrap();
}

async fn show_row(state: &Arc<AppState>, id: i64) -> Option<models::Show> {
    sqlx::query_as("SELECT * FROM shows WHERE id = ?")
        .bind(id)
        .fetch_optional(&state.db)
        .await
        .unwrap()
}

async fn show_count(state: &Arc<AppState>) -> i64 {
    sqlx::query_scalar("SELECT COUNT(*) FROM shows")
        .fetch_one(&state.db)
        .await
        .unwrap()
}

/// A guest account created by `creator`.
async fn add_guest_of(state: &Arc<AppState>, name: &str, creator: i64) -> i64 {
    let id = add_user(state, name, "guest").await;
    sqlx::query("UPDATE users SET created_by = ? WHERE id = ?")
        .bind(creator)
        .bind(id)
        .execute(&state.db)
        .await
        .unwrap();
    id
}

// ── Permission gates ────────────────────────────────────────────────────────

#[tokio::test]
async fn admin_endpoints_refuse_everyone_below_admin() {
    let (state, _temp) = state().await;
    let p = people(&state).await;
    add_show(&state, 1, Some(p.host), Some(p.host)).await;
    let callers = [
        None,
        Some(p.guest),
        Some(p.host),
        Some(p.admin),
        Some(p.superadmin),
    ];
    let admin_only = vec![NOBODY, DENIED, DENIED, OK, OK];
    let s = &state;

    let got = statuses(s, &callers, |h| api::api_shows_list(State(s.clone()), h)).await;
    assert_eq!(got, admin_only, "GET /api/shows");
    let got = statuses(s, &callers, |h| api::api_users_list(State(s.clone()), h)).await;
    assert_eq!(got, admin_only, "GET /api/users");
    let got = statuses(s, &callers, |h| {
        api::api_reset_password(State(s.clone()), Path(p.other_host), h)
    })
    .await;
    assert_eq!(got, admin_only, "POST /api/users/:id/reset-password");
    let got = statuses(s, &callers, |h| {
        api::api_show_unassign_artist(State(s.clone()), Path((1, 99)), h)
    })
    .await;
    assert_eq!(got, admin_only, "DELETE /api/shows/:id/artists/:id");
    let got = statuses(s, &callers, |h| {
        api::api_create_user(
            State(s.clone()),
            h,
            req(json!({"username": "new", "role": "host", "expires_at": "2099-12-31"})),
        )
    })
    .await;
    // The superadmin call hits the taken name, past the gate.
    let created = StatusCode::CREATED;
    assert_eq!(
        got[..4],
        [NOBODY, DENIED, DENIED, created],
        "POST /api/users"
    );
}

/// Any login may read the schedule and a show's detail. Only editors get
/// the lists to reassign the host.
#[tokio::test]
async fn any_login_reads_shows_but_only_editors_get_the_host_list() {
    let (state, _temp) = state().await;
    let p = people(&state).await;
    add_show(&state, 1, Some(p.host), Some(p.host)).await;
    let own_guest = add_guest_of(&state, "own-guest", p.host).await;
    let callers = [
        None,
        Some(p.guest),
        Some(p.other_host),
        Some(p.host),
        Some(p.admin),
    ];
    let s = &state;
    let any_login = vec![NOBODY, OK, OK, OK, OK];

    let got = statuses(s, &callers, |h| {
        api::api_shows_overview(State(s.clone()), h)
    })
    .await;
    assert_eq!(got, any_login, "GET /api/shows/overview");
    let got = statuses(s, &callers, |h| {
        api::api_show_detail(State(s.clone()), Path(1), h)
    })
    .await;
    assert_eq!(got, any_login, "GET /api/shows/:id");

    let hosts = |user: i64| async move {
        let (code, detail) =
            reply(api::api_show_detail(State(s.clone()), Path(1), headers(s, user).await).await)
                .await;
        assert_eq!(code, OK);
        let mut ids: Vec<i64> = detail["available_hosts"]
            .as_array()
            .unwrap()
            .iter()
            .map(|h| h["id"].as_i64().unwrap())
            .collect();
        ids.sort();
        ids
    };
    assert_eq!(hosts(p.other_host).await, Vec::<i64>::new());
    assert_eq!(hosts(p.guest).await, Vec::<i64>::new());
    // The creator may switch between themselves and a guest they created.
    assert_eq!(hosts(p.host).await, vec![p.host, own_guest]);
    // Admins may pick any host or admin.
    let mut all = vec![p.host, p.other_host, p.admin];
    all.sort();
    assert_eq!(hosts(p.admin).await, all);

    let missing = api::api_show_detail(State(state.clone()), Path(9), headers(s, p.host).await);
    assert_eq!(status(missing.await), StatusCode::NOT_FOUND);
}

// ── Create a show ───────────────────────────────────────────────────────────

#[tokio::test]
async fn guests_and_strangers_cannot_create_shows() {
    let (state, _temp, _r2) = setup(json!({})).await;
    let p = people(&state).await;
    let s = &state;
    let got = statuses(s, &[None, Some(p.guest)], |h| {
        api::api_create_show(
            State(s.clone()),
            h,
            req(json!({"title": "Mine", "date": "2099-01-01"})),
        )
    })
    .await;
    assert_eq!(got, vec![NOBODY, DENIED]);
    assert_eq!(show_count(&state).await, 0);
}

/// A host gets an external show they host themselves. Admin-only fields
/// in the request are dropped.
#[tokio::test]
async fn a_host_creates_a_constrained_show_for_themselves() {
    let (state, _temp, _r2) = setup(json!({})).await;
    let p = people(&state).await;
    let (code, show) = reply(
        api::api_create_show(
            State(state.clone()),
            headers(&state, p.host).await,
            req(json!({
                "title": "Night Shift", "date": "2099-01-01", "start_time": "20:00",
                "end_time": "23:00", "description": "secret", "show_type": "unheard",
                "host_user_id": p.host, "stream_mode": "prerecorded"
            })),
        )
        .await,
    )
    .await;
    assert_eq!(code, StatusCode::CREATED);
    assert_eq!(show["show_type"], "external");
    assert_eq!(show["description"], Value::Null);
    assert_eq!(show["end_time"], Value::Null);
    assert_eq!(show["host_user_id"], p.host);
    assert_eq!(show["stream_mode"], "prerecorded");
    let row = show_row(&state, show["id"].as_i64().unwrap())
        .await
        .unwrap();
    assert_eq!(row.created_by, Some(p.host));
    assert_eq!(row.start_time.as_deref(), Some("20:00"));
    assert_eq!(row.description, None);
}

#[tokio::test]
async fn a_host_hands_a_new_show_only_to_their_own_guest() {
    let (state, _temp, _r2) = setup(json!({})).await;
    let p = people(&state).await;
    let own_guest = add_guest_of(&state, "own-guest", p.host).await;
    let foreign_guest = add_guest_of(&state, "foreign-guest", p.other_host).await;
    let create = |host_user_id: i64| {
        let state = state.clone();
        async move {
            let h = headers(&state, p.host).await;
            reply(
                api::api_create_show(
                    State(state),
                    h,
                    req(json!({"title": "Mine", "date": "2099-01-01", "host_user_id": host_user_id})),
                )
                .await,
            )
            .await
        }
    };

    assert_eq!(create(p.other_host).await.0, DENIED);
    assert_eq!(create(foreign_guest).await.0, DENIED);
    assert_eq!(create(p.admin).await.0, DENIED);
    assert_eq!(show_count(&state).await, 0);

    let (code, show) = create(own_guest).await;
    assert_eq!(code, StatusCode::CREATED);
    let row = show_row(&state, show["id"].as_i64().unwrap())
        .await
        .unwrap();
    assert_eq!(row.host_user_id, Some(own_guest));
    // The creator keeps edit rights over the guest's show.
    assert_eq!(row.created_by, Some(p.host));
}

#[tokio::test]
async fn admin_create_checks_type_assignee_and_time_slot() {
    let (state, _temp, _r2) = setup(json!({})).await;
    let p = people(&state).await;
    let create = |body: Value| {
        let state = state.clone();
        async move {
            let h = headers(&state, p.admin).await;
            reply(api::api_create_show(State(state), h, req(body)).await).await
        }
    };

    let (code, _) = create(json!({"title": "X", "date": "2099-01-01", "show_type": "jazz"})).await;
    assert_eq!(code, StatusCode::BAD_REQUEST);
    let (code, _) = create(json!({"title": "X", "date": "2099-01-01", "host_user_id": 999})).await;
    assert_eq!(code, StatusCode::BAD_REQUEST);
    assert_eq!(show_count(&state).await, 0);

    let (code, show) = create(json!({
        "title": "Brunch", "date": "2099-01-01", "start_time": "12:00", "end_time": "14:00",
        "show_type": "brunchtime", "description": "Sunday", "host_user_id": p.other_host,
        "stream_mode": "tape"
    }))
    .await;
    assert_eq!(code, StatusCode::CREATED);
    assert_eq!(show["show_type"], "brunchtime");
    assert_eq!(show["description"], "Sunday");
    assert_eq!(show["host_user_id"], p.other_host);
    // An unknown delivery mode falls back to live.
    assert_eq!(show["stream_mode"], "live");

    // Same date, overlapping time: refused. Another date: fine.
    let clash = json!({"title": "Late", "date": "2099-01-01", "start_time": "13:00"});
    let (code, error) = create(clash).await;
    assert_eq!(code, StatusCode::CONFLICT);
    assert!(error["error"].as_str().unwrap().contains("Brunch"));
    let (code, _) =
        create(json!({"title": "Late", "date": "2099-01-02", "start_time": "13:00"})).await;
    assert_eq!(code, StatusCode::CREATED);
    assert_eq!(show_count(&state).await, 2);
}

#[tokio::test]
async fn a_template_of_another_user_is_not_found() {
    let (state, _temp, _r2) = setup(json!({})).await;
    let p = people(&state).await;
    sqlx::query("INSERT INTO show_templates (id, owner_user_id, name) VALUES (7, ?, 'Theirs')")
        .bind(p.other_host)
        .execute(&state.db)
        .await
        .unwrap();
    let result = api::api_create_show(
        State(state.clone()),
        headers(&state, p.host).await,
        req(json!({"title": "Mine", "date": "2099-01-01", "template_id": 7})),
    )
    .await;
    assert_eq!(status(result), StatusCode::NOT_FOUND);
    assert_eq!(show_count(&state).await, 0);
}

// ── Edit, delete and reassign a show ────────────────────────────────────────

#[tokio::test]
async fn only_the_host_creator_or_an_admin_edits_a_show() {
    let (state, _temp) = state().await;
    let p = people(&state).await;
    add_show(&state, 1, Some(p.host), Some(p.admin)).await;
    let callers = [
        None,
        Some(p.guest),
        Some(p.other_host),
        Some(p.host),
        Some(p.admin),
        Some(p.superadmin),
    ];
    let s = &state;
    let editors = vec![NOBODY, DENIED, DENIED, OK, OK, OK];

    let got = statuses(s, &callers, |h| {
        api::api_update_show(
            State(s.clone()),
            Path(1),
            h,
            req(json!({"title": "Renamed"})),
        )
    })
    .await;
    assert_eq!(got, editors, "PUT /api/shows/:id");

    // Refused callers change nothing.
    for caller in [p.guest, p.other_host] {
        let h = headers(s, caller).await;
        let update = api::api_update_show(
            State(s.clone()),
            Path(1),
            h.clone(),
            req(json!({"title": "Hijacked"})),
        );
        assert_eq!(status(update.await), DENIED);
        let assign = api::api_show_assign_host(
            State(s.clone()),
            Path(1),
            h.clone(),
            req(json!({"user_id": caller})),
        );
        assert_eq!(status(assign.await), DENIED);
        let unassign = api::api_show_unassign_host(State(s.clone()), Path(1), h.clone());
        assert_eq!(status(unassign.await), DENIED);
        let delete = api::api_delete_show(State(s.clone()), Path(1), h);
        assert_eq!(status(delete.await), DENIED);
    }
    let row = show_row(s, 1).await.unwrap();
    assert_eq!(row.title, "Renamed");
    assert_eq!(row.host_user_id, Some(p.host));

    let missing = api::api_update_show(
        State(s.clone()),
        Path(9),
        headers(s, p.admin).await,
        req(json!({"title": "X"})),
    );
    assert_eq!(status(missing.await), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn a_host_edits_their_show_but_not_the_admin_fields() {
    let (state, _temp) = state().await;
    let p = people(&state).await;
    add_show(&state, 1, Some(p.host), Some(p.host)).await;
    sqlx::query(
        "INSERT INTO shows (id, title, date, start_time, end_time) \
         VALUES (2, 'Brunch', '2099-01-02', '12:00', '14:00')",
    )
    .execute(&state.db)
    .await
    .unwrap();
    let update = |user: i64, body: Value| {
        let state = state.clone();
        async move {
            let h = headers(&state, user).await;
            reply(api::api_update_show(State(state), Path(1), h, req(body)).await).await
        }
    };

    assert_eq!(
        update(p.host, json!({"show_type": "unheard"})).await.0,
        DENIED
    );
    assert_eq!(update(p.host, json!({"ai_bio": "x"})).await.0, DENIED);
    let bad = StatusCode::BAD_REQUEST;
    assert_eq!(update(p.host, json!({"stream_mode": "tape"})).await.0, bad);
    assert_eq!(update(p.host, json!({})).await.0, bad);
    // Moving onto another show's slot is refused.
    let clash = json!({"date": "2099-01-02", "start_time": "13:00"});
    assert_eq!(update(p.host, clash).await.0, StatusCode::CONFLICT);

    let (code, show) = update(
        p.host,
        json!({"title": "Late Shift", "start_time": "22:00", "end_time": "23:59",
               "description": "Deep", "stream_mode": "prerecorded"}),
    )
    .await;
    assert_eq!(code, OK);
    assert_eq!(show["title"], "Late Shift");
    let row = show_row(&state, 1).await.unwrap();
    assert_eq!(row.start_time.as_deref(), Some("22:00"));
    assert_eq!(row.stream_mode.as_deref(), Some("prerecorded"));
    assert_eq!(row.date, "2099-01-01");
    assert_eq!(row.show_type, "external");

    assert_eq!(update(p.admin, json!({"show_type": "jazz"})).await.0, bad);
    assert_eq!(
        update(p.admin, json!({"show_type": "brunchtime"})).await.0,
        OK
    );
    assert_eq!(show_row(&state, 1).await.unwrap().show_type, "brunchtime");
}

/// The creator may hand the show to a guest they created and keeps control.
#[tokio::test]
async fn the_creator_hands_a_show_to_their_guest_and_keeps_control() {
    let (state, _temp) = state().await;
    let p = people(&state).await;
    add_show(&state, 1, Some(p.host), Some(p.host)).await;
    let own_guest = add_guest_of(&state, "own-guest", p.host).await;
    let foreign_guest = add_guest_of(&state, "foreign-guest", p.other_host).await;
    let assign = |user: i64, target: i64| {
        let state = state.clone();
        async move {
            let h = headers(&state, user).await;
            reply(
                api::api_show_assign_host(
                    State(state),
                    Path(1),
                    h,
                    req(json!({"user_id": target})),
                )
                .await,
            )
            .await
        }
    };

    assert_eq!(assign(p.host, p.other_host).await.0, DENIED);
    assert_eq!(assign(p.host, foreign_guest).await.0, DENIED);
    assert_eq!(assign(p.host, 999).await.0, StatusCode::NOT_FOUND);
    let (code, body) = assign(p.host, own_guest).await;
    assert_eq!(code, OK);
    assert_eq!(body["host_username"], "own-guest");
    assert_eq!(
        show_row(&state, 1).await.unwrap().host_user_id,
        Some(own_guest)
    );

    // No longer the host, but still the creator.
    let rename = api::api_update_show(
        State(state.clone()),
        Path(1),
        headers(&state, p.host).await,
        req(json!({"title": "Guest Night"})),
    );
    assert_eq!(status(rename.await), OK);
    assert_eq!(assign(p.host, p.host).await.0, OK);

    // Admins assign hosts and admins, not guests.
    assert_eq!(assign(p.admin, own_guest).await.0, StatusCode::BAD_REQUEST);
    assert_eq!(assign(p.admin, p.other_host).await.0, OK);
    assert_eq!(
        show_row(&state, 1).await.unwrap().host_user_id,
        Some(p.other_host)
    );

    let unassign =
        api::api_show_unassign_host(State(state.clone()), Path(1), headers(&state, p.host).await);
    assert_eq!(status(unassign.await), OK);
    assert_eq!(show_row(&state, 1).await.unwrap().host_user_id, None);
}

#[tokio::test]
async fn unheard_shows_take_no_host() {
    let (state, _temp) = state().await;
    let p = people(&state).await;
    sqlx::query("INSERT INTO shows (id, title, date, show_type) VALUES (1, 'UNHEARD', '2099-01-01', 'unheard')")
        .execute(&state.db)
        .await
        .unwrap();
    let result = api::api_show_assign_host(
        State(state.clone()),
        Path(1),
        headers(&state, p.admin).await,
        req(json!({"user_id": p.host})),
    )
    .await;
    assert_eq!(status(result), StatusCode::BAD_REQUEST);
    assert_eq!(show_row(&state, 1).await.unwrap().host_user_id, None);
}

#[tokio::test]
async fn a_host_deletes_their_own_show_once() {
    let (state, _temp) = state().await;
    let p = people(&state).await;
    add_show(&state, 1, Some(p.host), Some(p.host)).await;
    add_show(&state, 2, Some(p.other_host), Some(p.other_host)).await;
    let delete = |user: Option<i64>, id: i64| {
        let state = state.clone();
        async move {
            let h = login(&state, user).await;
            status(api::api_delete_show(State(state), Path(id), h).await)
        }
    };

    assert_eq!(delete(None, 1).await, NOBODY);
    assert_eq!(delete(Some(p.host), 1).await, OK);
    assert!(show_row(&state, 1).await.is_none());
    assert_eq!(delete(Some(p.host), 1).await, StatusCode::NOT_FOUND);
    // An admin may delete any show.
    assert_eq!(delete(Some(p.admin), 2).await, OK);
    assert_eq!(show_count(&state).await, 0);
}

// ── My show: the host's pre-recorded upload ─────────────────────────────────

#[tokio::test]
async fn my_show_lists_only_the_callers_shows() {
    let (state, _temp) = state().await;
    let p = people(&state).await;
    let artist_user = add_user(&state, "artist", "host").await;
    add_show(&state, 1, Some(p.host), Some(p.host)).await;
    add_show(&state, 2, Some(p.other_host), Some(p.other_host)).await;
    sqlx::query(
        "INSERT INTO artists (id, name, pronouns, track1_name, track2_name, user_id) \
         VALUES (5, 'Band', 'they', 'a', 'b', ?)",
    )
    .bind(artist_user)
    .execute(&state.db)
    .await
    .unwrap();
    sqlx::query("INSERT INTO artist_show_assignments (artist_id, show_id) VALUES (5, 2)")
        .execute(&state.db)
        .await
        .unwrap();
    let mine = |user: i64| {
        let state = state.clone();
        async move {
            let h = headers(&state, user).await;
            let (code, body) = reply(api::api_my_show(State(state), h).await).await;
            assert_eq!(code, OK);
            let ids: Vec<i64> = body["shows"]
                .as_array()
                .unwrap()
                .iter()
                .map(|s| s["id"].as_i64().unwrap())
                .collect();
            (body["assigned"].as_bool().unwrap(), ids)
        }
    };

    assert_eq!(mine(p.host).await, (true, vec![1]));
    assert_eq!(mine(p.other_host).await, (true, vec![2]));
    assert_eq!(mine(artist_user).await, (true, vec![2]));
    assert_eq!(mine(p.guest).await, (false, vec![]));
    let nobody = api::api_my_show(State(state.clone()), HeaderMap::new()).await;
    assert_eq!(status(nobody), NOBODY);
}

async fn upload_init(
    state: &Arc<AppState>,
    user: i64,
    show_id: i64,
    total_chunks: u32,
) -> (StatusCode, Value) {
    reply(
        api::api_my_show_upload_init(
            State(state.clone()),
            Query(api::ShowIdQuery { show_id }),
            headers(state, user).await,
            Json(api::PrerecordedInitRequest {
                filename: "set.mp3".to_string(),
                total_size: 6,
                total_chunks,
            }),
        )
        .await,
    )
    .await
}

async fn upload_chunk(
    state: &Arc<AppState>,
    user: i64,
    show_id: i64,
    session: &str,
    index: u32,
    data: &[u8],
) -> StatusCode {
    status(
        api::api_my_show_upload_chunk(
            State(state.clone()),
            Path(session.to_string()),
            Query(ChunkQueryWithShowId { index, show_id }),
            headers(state, user).await,
            form(&[("chunk", Some("blob"), data)]).await,
        )
        .await,
    )
}

async fn upload_finalize(
    state: &Arc<AppState>,
    user: i64,
    show_id: i64,
    session: &str,
) -> (StatusCode, Value) {
    reply(
        api::api_my_show_upload_finalize(
            State(state.clone()),
            Path(session.to_string()),
            Query(api::ShowIdQuery { show_id }),
            headers(state, user).await,
        )
        .await,
    )
    .await
}

async fn pending_rows(state: &Arc<AppState>) -> (i64, i64) {
    sqlx::query_as(
        "SELECT (SELECT COUNT(*) FROM pending_recording_uploads), \
                (SELECT COUNT(*) FROM pending_recording_chunks)",
    )
    .fetch_one(&state.db)
    .await
    .unwrap()
}

/// Chunks may arrive in any order. Finalize joins them by index, stores the
/// file on the show and cleans up. Deleting the upload removes it again.
#[tokio::test]
async fn a_chunked_prerecorded_upload_is_joined_in_order() {
    let (state, _temp, r2) = setup(json!({})).await;
    let p = people(&state).await;
    add_show(&state, 1, Some(p.host), Some(p.host)).await;
    sqlx::query("UPDATE shows SET prerecorded_confirmed_at = '2099-01-01T00:00:00Z' WHERE id = 1")
        .execute(&state.db)
        .await
        .unwrap();

    let (code, init) = upload_init(&state, p.host, 1, 2).await;
    assert_eq!(code, OK);
    let session = init["session_id"].as_str().unwrap().to_string();
    assert_eq!(
        upload_chunk(&state, p.host, 1, &session, 1, b"bbb").await,
        OK
    );
    assert_eq!(
        upload_chunk(&state, p.host, 1, &session, 0, b"aaa").await,
        OK
    );
    let (code, done) = upload_finalize(&state, p.host, 1, &session).await;
    assert_eq!(code, OK);
    assert_eq!(done["key"], "prerecorded/1/set-mp3");
    assert_eq!(done["filename"], "set.mp3");

    assert_eq!(
        r2.object(&state, "prerecorded/1/set-mp3").as_deref(),
        Some(&b"aaabbb"[..])
    );
    let bucket = &state.config.r2_bucket_name;
    assert!(r2.keys_in(bucket, "pending-prerecorded/").is_empty());
    assert_eq!(pending_rows(&state).await, (0, 0));
    let row = show_row(&state, 1).await.unwrap();
    assert_eq!(
        row.prerecorded_key.as_deref(),
        Some("prerecorded/1/set-mp3")
    );
    assert_eq!(row.prerecorded_filename.as_deref(), Some("set.mp3"));
    // A new file needs a new confirmation.
    assert_eq!(row.prerecorded_confirmed_at, None);

    let (_, flow) =
        reply(api::api_my_show(State(state.clone()), headers(&state, p.host).await).await).await;
    assert_eq!(flow["shows"][0]["prerecorded_filename"], "set.mp3");
    assert!(flow["shows"][0]["prerecorded_url"].is_string());

    let delete = api::api_my_show_delete_upload(
        State(state.clone()),
        Query(api::ShowIdQuery { show_id: 1 }),
        headers(&state, p.host).await,
    );
    assert_eq!(status(delete.await), OK);
    assert_eq!(r2.object(&state, "prerecorded/1/set-mp3"), None);
    let row = show_row(&state, 1).await.unwrap();
    assert_eq!(row.prerecorded_key, None);
    assert_eq!(row.prerecorded_filename, None);
}

#[tokio::test]
async fn finalize_refuses_missing_chunks_and_a_session_of_another_show() {
    let (state, _temp, r2) = setup(json!({})).await;
    let p = people(&state).await;
    add_show(&state, 1, Some(p.host), Some(p.host)).await;
    sqlx::query(
        "INSERT INTO shows (id, title, date, host_user_id) VALUES (2, 'Other', '2099-01-02', ?)",
    )
    .bind(p.host)
    .execute(&state.db)
    .await
    .unwrap();
    let bad = StatusCode::BAD_REQUEST;

    let (_, init) = upload_init(&state, p.host, 1, 2).await;
    let session = init["session_id"].as_str().unwrap().to_string();
    assert_eq!(
        upload_chunk(&state, p.host, 1, &session, 2, b"ccc").await,
        bad
    );
    assert_eq!(
        upload_chunk(&state, p.host, 1, &session, 0, b"aaa").await,
        OK
    );
    let (code, error) = upload_finalize(&state, p.host, 1, &session).await;
    assert_eq!(code, bad);
    assert!(error["error"].as_str().unwrap().contains("received 1 of 2"));

    // The host also hosts show 2, but this session belongs to show 1.
    assert_eq!(
        upload_chunk(&state, p.host, 2, &session, 1, b"bbb").await,
        bad
    );
    assert_eq!(upload_finalize(&state, p.host, 2, &session).await.0, bad);
    assert_eq!(
        upload_finalize(&state, p.host, 1, "no-such-session")
            .await
            .0,
        bad
    );

    assert_eq!(show_row(&state, 1).await.unwrap().prerecorded_key, None);
    assert_eq!(show_row(&state, 2).await.unwrap().prerecorded_key, None);
    assert!(r2
        .keys_in(&state.config.r2_bucket_name, "prerecorded/")
        .is_empty());
}

async fn upload_small(
    state: &Arc<AppState>,
    user: Option<i64>,
    show_id: i64,
    parts: &[(&str, Option<&str>, &[u8])],
) -> (StatusCode, Value) {
    reply(
        api::api_my_show_upload(
            State(state.clone()),
            Query(api::ShowIdQuery { show_id }),
            login(state, user).await,
            form(parts).await,
        )
        .await,
    )
    .await
}

#[tokio::test]
async fn a_small_upload_is_stored_on_the_show() {
    let (state, _temp, r2) = setup(json!({})).await;
    let p = people(&state).await;
    add_show(&state, 1, Some(p.host), Some(p.host)).await;

    let (code, _) = upload_small(&state, Some(p.host), 1, &[("other", None, b"x")]).await;
    assert_eq!(code, StatusCode::BAD_REQUEST);

    r2.refuse_writes(true);
    let (code, _) = upload_small(
        &state,
        Some(p.host),
        1,
        &[("file", Some("tape.mp3"), b"abc")],
    )
    .await;
    assert_eq!(code, StatusCode::INTERNAL_SERVER_ERROR);
    assert_eq!(show_row(&state, 1).await.unwrap().prerecorded_key, None);

    r2.refuse_writes(false);
    let (code, body) = upload_small(
        &state,
        Some(p.host),
        1,
        &[("file", Some("tape.mp3"), b"abc")],
    )
    .await;
    assert_eq!(code, OK);
    assert_eq!(body["key"], "prerecorded/1/tape-mp3");
    assert_eq!(
        r2.object(&state, "prerecorded/1/tape-mp3").as_deref(),
        Some(&b"abc"[..])
    );
    let row = show_row(&state, 1).await.unwrap();
    assert_eq!(row.prerecorded_filename.as_deref(), Some("tape.mp3"));

    let confirm = |user: i64| {
        let state = state.clone();
        async move {
            let h = headers(&state, user).await;
            status(
                api::api_my_show_confirm(State(state), Query(api::ShowIdQuery { show_id: 1 }), h)
                    .await,
            )
        }
    };
    assert_eq!(confirm(p.host).await, OK);
    assert_eq!(
        show_row(&state, 1).await.unwrap().stream_mode.as_deref(),
        Some("prerecorded")
    );
}

#[tokio::test]
async fn confirm_needs_an_uploaded_file() {
    let (state, _temp) = state().await;
    let p = people(&state).await;
    add_show(&state, 1, Some(p.host), Some(p.host)).await;
    let result = api::api_my_show_confirm(
        State(state.clone()),
        Query(api::ShowIdQuery { show_id: 1 }),
        headers(&state, p.host).await,
    )
    .await;
    assert_eq!(status(result), StatusCode::BAD_REQUEST);
    assert_eq!(
        show_row(&state, 1).await.unwrap().prerecorded_confirmed_at,
        None
    );
}

/// Another host and a guest of another show may not upload to, confirm or
/// delete this show's file. Admins may.
#[tokio::test]
async fn only_the_shows_people_and_admins_touch_its_upload() {
    let (state, _temp, r2) = setup(json!({})).await;
    let p = people(&state).await;
    add_show(&state, 1, Some(p.host), Some(p.host)).await;
    add_show(&state, 2, Some(p.guest), Some(p.other_host)).await;
    let file = &[("file", Some("tape.mp3"), &b"abc"[..])];
    assert_eq!(upload_small(&state, Some(p.host), 1, file).await.0, OK);
    let (_, init) = upload_init(&state, p.host, 1, 1).await;
    let session = init["session_id"].as_str().unwrap().to_string();
    let before = show_row(&state, 1).await.unwrap();

    // Not assigned: refused with 400 today (403 is asked for in
    // https://github.com/phaabe/live.moafunk.de/issues/350).
    let refused = StatusCode::BAD_REQUEST;
    for stranger in [p.other_host, p.guest] {
        let other = &[("file", Some("evil.mp3"), &b"evil"[..])];
        assert_eq!(
            upload_small(&state, Some(stranger), 1, other).await.0,
            refused
        );
        assert_eq!(upload_init(&state, stranger, 1, 1).await.0, refused);
        assert_eq!(
            upload_chunk(&state, stranger, 1, &session, 0, b"evil").await,
            refused
        );
        assert_eq!(
            upload_finalize(&state, stranger, 1, &session).await.0,
            refused
        );
        let h = headers(&state, stranger).await;
        let q = || Query(api::ShowIdQuery { show_id: 1 });
        let confirm = api::api_my_show_confirm(State(state.clone()), q(), h.clone());
        assert_eq!(status(confirm.await), refused);
        let delete = api::api_my_show_delete_upload(State(state.clone()), q(), h);
        assert_eq!(status(delete.await), refused);
    }
    assert_eq!(upload_small(&state, None, 1, file).await.0, NOBODY);
    let after = show_row(&state, 1).await.unwrap();
    assert_eq!(after.prerecorded_key, before.prerecorded_key);
    assert_eq!(after.prerecorded_confirmed_at, None);
    assert_eq!(
        r2.object(&state, "prerecorded/1/tape-mp3").as_deref(),
        Some(&b"abc"[..])
    );
    assert!(r2.object(&state, "prerecorded/1/evil-mp3").is_none());
    assert_eq!(pending_rows(&state).await, (1, 0));

    // Admins may act on any show.
    let confirm = api::api_my_show_confirm(
        State(state.clone()),
        Query(api::ShowIdQuery { show_id: 1 }),
        headers(&state, p.admin).await,
    );
    assert_eq!(status(confirm.await), OK);
}

async fn go_live(state: &Arc<AppState>, user: i64, show_id: i64) -> StatusCode {
    status(
        api::api_my_show_go_live(
            State(state.clone()),
            Query(api::GoLiveQuery {
                show_id,
                retry: false,
            }),
            headers(state, user).await,
        )
        .await,
    )
}

/// A prerecorded show of host 1, and show 2 hosted by the guest.
async fn prerecorded_shows(state: &Arc<AppState>, p: &People) {
    add_show(state, 1, Some(p.host), Some(p.host)).await;
    add_show(state, 2, Some(p.guest), Some(p.other_host)).await;
    sqlx::query(
        "UPDATE shows SET stream_mode = 'prerecorded', prerecorded_key = 'prerecorded/1/tape-mp3' \
         WHERE id = 1",
    )
    .execute(&state.db)
    .await
    .unwrap();
}

#[tokio::test]
async fn strangers_cannot_start_a_prerecorded_show() {
    let (state, _temp) = state().await;
    let p = people(&state).await;
    prerecorded_shows(&state, &p).await;
    for stranger in [p.other_host, p.guest] {
        assert!(go_live(&state, stranger, 1).await.is_client_error());
    }
    assert_eq!(
        show_row(&state, 1).await.unwrap().prerecorded_started_at,
        None
    );
}

#[tokio::test]
#[ignore = "https://github.com/phaabe/live.moafunk.de/issues/350"]
async fn go_live_answers_strangers_with_403() {
    let (state, _temp) = state().await;
    let p = people(&state).await;
    prerecorded_shows(&state, &p).await;
    for stranger in [p.other_host, p.guest] {
        assert_eq!(go_live(&state, stranger, 1).await, DENIED);
    }
}

// ── Users and guests ────────────────────────────────────────────────────────

async fn create_user(state: &Arc<AppState>, caller: i64, body: Value) -> (StatusCode, Value) {
    reply(
        api::api_create_user(
            State(state.clone()),
            headers(state, caller).await,
            req(body),
        )
        .await,
    )
    .await
}

#[tokio::test]
async fn admins_create_users_below_superadmin() {
    let (state, _temp) = state().await;
    let p = people(&state).await;

    let (code, _) = create_user(&state, p.admin, json!({"username": "h1", "role": "host"})).await;
    assert_eq!(code, StatusCode::BAD_REQUEST, "a host needs an end date");
    let (code, _) = create_user(
        &state,
        p.admin,
        json!({"username": "s1", "role": "superadmin"}),
    )
    .await;
    assert_eq!(code, DENIED);

    let (code, created) = create_user(
        &state,
        p.admin,
        json!({"username": "h1", "role": "host", "expires_at": "2099-12-31"}),
    )
    .await;
    assert_eq!(code, StatusCode::CREATED);
    assert_eq!(created["user"]["role"], "host");
    let password = created["password"].as_str().unwrap().to_string();
    assert_eq!(password.len(), 16);
    let must_change: bool =
        sqlx::query_scalar("SELECT must_change_password FROM users WHERE username = 'h1'")
            .fetch_one(&state.db)
            .await
            .unwrap();
    assert!(must_change);
    // The one-time password works.
    let login = api::api_login(
        State(state.clone()),
        req(json!({"username": "h1", "password": password})),
    );
    let (code, body) = reply(login.await).await;
    assert_eq!(code, OK);
    assert_eq!(body["user"]["must_change_password"], true);
    assert_eq!(body["redirect_url"], "/#/stream");

    let (code, _) = create_user(
        &state,
        p.superadmin,
        json!({"username": "s1", "role": "superadmin"}),
    )
    .await;
    assert_eq!(code, StatusCode::CREATED);
}

#[tokio::test]
async fn user_changes_follow_the_role_order() {
    let (state, _temp) = state().await;
    let p = people(&state).await;
    let other_admin = add_user(&state, "admin2", "admin").await;
    let update = |caller: i64, id: i64, body: Value| {
        let state = state.clone();
        async move {
            let h = headers(&state, caller).await;
            reply(api::api_update_user(State(state), Path(id), h, req(body)).await).await
        }
    };
    let delete = |caller: i64, id: i64| {
        let state = state.clone();
        async move {
            let h = headers(&state, caller).await;
            status(api::api_delete_user(State(state), Path(id), h).await)
        }
    };
    let reset = |caller: i64, id: i64| {
        let state = state.clone();
        async move {
            let h = headers(&state, caller).await;
            status(api::api_reset_password(State(state), Path(id), h).await)
        }
    };

    // Admins may not touch their peers or promote to admin.
    assert_eq!(
        update(p.admin, other_admin, json!({"expires_at": "2099-12-31"}))
            .await
            .0,
        DENIED
    );
    assert_eq!(
        update(p.admin, p.host, json!({"role": "admin"})).await.0,
        DENIED
    );
    assert_eq!(reset(p.admin, other_admin).await, DENIED);
    assert_eq!(delete(p.admin, other_admin).await, DENIED);
    assert_eq!(delete(p.admin, p.superadmin).await, DENIED);
    assert_eq!(delete(p.admin, p.admin).await, StatusCode::BAD_REQUEST);
    assert_eq!(
        update(p.admin, 999, json!({})).await.0,
        StatusCode::NOT_FOUND
    );
    assert_eq!(reset(p.admin, 999).await, StatusCode::NOT_FOUND);
    let role: String = sqlx::query_scalar("SELECT role FROM users WHERE id = ?")
        .bind(p.host)
        .fetch_one(&state.db)
        .await
        .unwrap();
    assert_eq!(role, "host");

    // A host keeps needing an end date.
    let (code, _) = update(p.admin, p.host, json!({})).await;
    assert_eq!(code, StatusCode::BAD_REQUEST);
    let (code, body) = update(p.admin, p.host, json!({"expires_at": "2099-12-31"})).await;
    assert_eq!(code, OK);
    assert_eq!(body["user"]["expires_at"], "2099-12-31");
    assert_eq!(reset(p.admin, p.host).await, OK);

    // A superadmin may change and remove admins.
    let (code, body) = update(
        p.superadmin,
        other_admin,
        json!({"role": "host", "expires_at": "2099-12-31"}),
    )
    .await;
    assert_eq!(code, OK);
    assert_eq!(body["user"]["role"], "host");
    assert_eq!(delete(p.superadmin, p.admin).await, OK);
    let left: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM users WHERE id = ?")
        .bind(p.admin)
        .fetch_one(&state.db)
        .await
        .unwrap();
    assert_eq!(left, 0);
}

async fn create_guest(
    state: &Arc<AppState>,
    caller: Option<i64>,
    body: Value,
) -> (StatusCode, Value) {
    reply(api::api_create_guest(State(state.clone()), login(state, caller).await, req(body)).await)
        .await
}

fn berlin_today() -> String {
    chrono::Utc::now()
        .with_timezone(&chrono_tz::Europe::Berlin)
        .format("%Y-%m-%d")
        .to_string()
}

/// A host creates a guest for the show day. The guest may log in only on
/// that day.
#[tokio::test]
async fn a_host_creates_a_guest_who_logs_in_on_the_show_day_only() {
    let (state, _temp) = state().await;
    let p = people(&state).await;
    let today = berlin_today();
    let guest = |name: &str, date: &str| json!({"username": name, "login_date": date});

    assert_eq!(
        create_guest(&state, None, guest("g", &today)).await.0,
        NOBODY
    );
    assert_eq!(
        create_guest(&state, Some(p.guest), guest("g", &today))
            .await
            .0,
        DENIED
    );
    let bad = StatusCode::BAD_REQUEST;
    assert_eq!(
        create_guest(&state, Some(p.host), guest("  ", &today))
            .await
            .0,
        bad
    );
    assert_eq!(
        create_guest(&state, Some(p.host), guest("g", "17.11.2026"))
            .await
            .0,
        bad
    );
    assert_eq!(
        create_guest(&state, Some(p.host), guest("other", &today))
            .await
            .0,
        bad
    );

    let (code, today_guest) = create_guest(&state, Some(p.host), guest(" dj ", &today)).await;
    assert_eq!(code, StatusCode::CREATED);
    assert_eq!(today_guest["username"], "dj");
    let (role, created_by, expires_at): (String, Option<i64>, String) =
        sqlx::query_as("SELECT role, created_by, expires_at FROM users WHERE username = 'dj'")
            .fetch_one(&state.db)
            .await
            .unwrap();
    assert_eq!((role.as_str(), created_by), ("guest", Some(p.host)));
    assert_eq!(expires_at, format!("{today}T23:59:59"));
    let (_, later_guest) = create_guest(&state, Some(p.host), guest("later", "2099-01-01")).await;

    let login = |guest: &Value| {
        let state = state.clone();
        let body = json!({"username": guest["username"], "password": guest["password"]});
        async move { reply(api::api_login(State(state), req(body)).await).await }
    };
    let (code, body) = login(&today_guest).await;
    assert_eq!(code, OK);
    assert_eq!(body["redirect_url"], "/#/stream");
    assert_eq!(login(&later_guest).await.0, NOBODY);
}

#[tokio::test]
async fn an_expired_account_cannot_log_in() {
    let (state, _temp) = state().await;
    add_user(&state, "old", "host").await;
    sqlx::query("UPDATE users SET expires_at = '2020-01-01' WHERE username = 'old'")
        .execute(&state.db)
        .await
        .unwrap();
    let login = api::api_login(
        State(state.clone()),
        req(json!({"username": "old", "password": "test-password"})),
    );
    let (code, body) = reply(login.await).await;
    assert_eq!(code, NOBODY);
    assert!(body["error"].as_str().unwrap().contains("expired"));
    let unknown = api::api_login(
        State(state.clone()),
        req(json!({"username": "nobody", "password": "test-password"})),
    );
    assert_eq!(status(unknown.await), NOBODY);
}

#[tokio::test]
async fn me_reads_the_session() {
    let (state, _temp) = state().await;
    let p = people(&state).await;
    let (code, me) =
        reply(api::api_me(State(state.clone()), headers(&state, p.host).await).await).await;
    assert_eq!(code, OK);
    assert_eq!(me["username"], "host");
    assert_eq!(me["role"], "host");
    let mut stale = HeaderMap::new();
    stale.insert("cookie", "session=not-a-token".parse().unwrap());
    assert_eq!(
        status(api::api_me(State(state.clone()), stale).await),
        NOBODY
    );
    assert_eq!(
        status(api::api_me(State(state), HeaderMap::new()).await),
        NOBODY
    );
}

#[tokio::test]
#[ignore = "https://github.com/phaabe/live.moafunk.de/issues/926"]
async fn deleting_a_user_who_hosts_a_show_is_no_database_error() {
    let (state, _temp) = state().await;
    let p = people(&state).await;
    add_show(&state, 1, Some(p.host), Some(p.host)).await;
    let delete = api::api_delete_user(
        State(state.clone()),
        Path(p.host),
        headers(&state, p.admin).await,
    );
    assert_ne!(status(delete.await), StatusCode::INTERNAL_SERVER_ERROR);
}

#[tokio::test]
#[ignore = "https://github.com/phaabe/live.moafunk.de/issues/927"]
async fn the_session_of_an_expired_account_is_refused() {
    let (state, _temp) = state().await;
    let p = people(&state).await;
    add_show(&state, 1, Some(p.host), Some(p.host)).await;
    let session = headers(&state, p.host).await;
    sqlx::query("UPDATE users SET expires_at = '2020-01-01' WHERE id = ?")
        .bind(p.host)
        .execute(&state.db)
        .await
        .unwrap();
    assert_eq!(
        status(api::api_me(State(state.clone()), session.clone()).await),
        NOBODY
    );
    assert_eq!(
        status(api::api_my_show(State(state), session).await),
        NOBODY
    );
}

async fn add_artist(state: &Arc<AppState>, id: i64, user: Option<i64>) {
    sqlx::query(
        "INSERT INTO artists (id, name, pronouns, track1_name, track2_name, user_id) \
         VALUES (?, 'Band', 'they', 'a', 'b', ?)",
    )
    .bind(id)
    .bind(user)
    .execute(&state.db)
    .await
    .unwrap();
}

async fn artist_user(state: &Arc<AppState>, artist: i64) -> Option<i64> {
    sqlx::query_scalar("SELECT user_id FROM artists WHERE id = ?")
        .bind(artist)
        .fetch_one(&state.db)
        .await
        .unwrap()
}

/// A taken name or a bad artist is refused before the user is written.
#[tokio::test]
async fn a_refused_user_create_leaves_no_user() {
    let (state, _temp) = state().await;
    let p = people(&state).await;
    add_artist(&state, 5, Some(p.other_host)).await;
    add_artist(&state, 6, None).await;
    let users = || async {
        sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM users")
            .fetch_one(&state.db)
            .await
            .unwrap()
    };
    let before = users().await;
    let admin = |username: &str, artist_id: i64| json!({"username": username, "role": "admin", "artist_id": artist_id});

    let (code, error) = create_user(
        &state,
        p.admin,
        json!({"username": "host", "role": "admin"}),
    )
    .await;
    assert_eq!(code, StatusCode::BAD_REQUEST);
    assert!(error["error"].as_str().unwrap().contains("already exists"));
    let (code, _) = create_user(&state, p.admin, admin("new", 77)).await;
    assert_eq!(code, StatusCode::NOT_FOUND);
    let (code, _) = create_user(&state, p.admin, admin("new", 5)).await;
    assert_eq!(code, StatusCode::BAD_REQUEST);
    assert_eq!(users().await, before);

    // The name is still free after the refusals.
    let (code, created) = create_user(&state, p.admin, admin("new", 6)).await;
    assert_eq!(code, StatusCode::CREATED);
    assert_eq!(created["user"]["linked_artist_id"], 6);
    assert_eq!(artist_user(&state, 6).await, created["user"]["id"].as_i64());
}

/// A bad artist link is refused before any other field changes.
#[tokio::test]
async fn a_refused_user_update_changes_nothing() {
    let (state, _temp) = state().await;
    let p = people(&state).await;
    add_artist(&state, 5, Some(p.host)).await;
    add_artist(&state, 6, Some(p.other_host)).await;
    add_artist(&state, 7, None).await;
    let update = |body: Value| {
        let state = state.clone();
        async move {
            let h = headers(&state, p.admin).await;
            status(api::api_update_user(State(state), Path(p.host), h, req(body)).await)
        }
    };
    let expires = || async {
        sqlx::query_scalar::<_, Option<String>>("SELECT expires_at FROM users WHERE id = ?")
            .bind(p.host)
            .fetch_one(&state.db)
            .await
            .unwrap()
    };

    let bad_link = |artist: i64| json!({"expires_at": "2099-12-31", "artist_id": artist});
    assert_eq!(update(bad_link(77)).await, StatusCode::NOT_FOUND);
    assert_eq!(update(bad_link(6)).await, StatusCode::BAD_REQUEST);
    assert_eq!(expires().await, None);
    assert_eq!(artist_user(&state, 5).await, Some(p.host));
    assert_eq!(artist_user(&state, 6).await, Some(p.other_host));

    assert_eq!(
        update(json!({"expires_at": "2099-12-31", "artist_id": 7})).await,
        OK
    );
    assert_eq!(expires().await.as_deref(), Some("2099-12-31"));
    assert_eq!(artist_user(&state, 5).await, None);
    assert_eq!(artist_user(&state, 7).await, Some(p.host));
    assert_eq!(update(json!({"artist_id": null})).await, OK);
    assert_eq!(artist_user(&state, 7).await, None);
}

// ── Episode recording upload ────────────────────────────────────────────────

async fn recording_small(
    state: &Arc<AppState>,
    user: Option<i64>,
    show_id: i64,
    file: &str,
) -> (StatusCode, Value) {
    let parts = [("file", Some(file), &b"mp3"[..])];
    reply(
        api::api_upload_show_recording(
            State(state.clone()),
            Path(show_id),
            login(state, user).await,
            form(&parts).await,
        )
        .await,
    )
    .await
}

async fn recording_init(state: &Arc<AppState>, user: i64, show_id: i64) -> (StatusCode, Value) {
    use handlers::upload_recording_chunked::{init_recording_upload, InitRequest};
    let request = InitRequest {
        filename: format!("long-{show_id}.mp3"),
        total_size: 3,
        total_chunks: 1,
        peaks: None,
    };
    let headers = headers(state, user).await;
    reply(init_recording_upload(State(state.clone()), Path(show_id), headers, Json(request)).await)
        .await
}

async fn recording_chunk(
    state: &Arc<AppState>,
    user: i64,
    show_id: i64,
    session: &str,
) -> StatusCode {
    use handlers::upload_recording_chunked::{upload_recording_chunk, ChunkQuery};
    status(
        upload_recording_chunk(
            State(state.clone()),
            Path((show_id, session.to_string())),
            Query(ChunkQuery { index: 0 }),
            headers(state, user).await,
            form(&[("chunk", Some("blob"), b"mp3")]).await,
        )
        .await,
    )
}

async fn recording_finalize(
    state: &Arc<AppState>,
    user: i64,
    show_id: i64,
    session: &str,
) -> (StatusCode, Value) {
    use handlers::upload_recording_chunked::finalize_recording_upload;
    let result = finalize_recording_upload(
        State(state.clone()),
        Path((show_id, session.to_string())),
        headers(state, user).await,
        Json(Default::default()),
    )
    .await;
    reply(result).await
}

/// How often the background SoundCloud upload read `key` from R2, after the
/// spawned task had time to run. The test API base is a dead port, so the
/// upload itself fails after that read.
async fn soundcloud_reads(state: &Arc<AppState>, r2: &crate::fake_r2::FakeR2, key: &str) -> usize {
    for _ in 0..50 {
        if r2.count(state, "GET", key) > 0 {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    }
    r2.count(state, "GET", key)
}

/// The episode's host may upload its recording, in one request or in chunks.
/// Another host, a guest and nobody may not. Only an admin upload starts
/// the SoundCloud upload on its own; the host page has a button for it.
#[tokio::test]
async fn a_host_uploads_a_recording_only_to_their_own_episode() {
    let (state, _temp, r2) = setup(json!({ "soundcloud_access_token": "static-token" })).await;
    let p = people(&state).await;
    add_show(&state, 1, Some(p.host), Some(p.host)).await;
    add_show(&state, 2, Some(p.other_host), Some(p.other_host)).await;

    // One request.
    let (code, body) = recording_small(&state, Some(p.host), 1, "mine.mp3").await;
    assert_eq!(code, OK);
    let key = body["key"].as_str().unwrap().to_string();
    let row = show_row(&state, 1).await.unwrap();
    assert_eq!(row.recording_key.as_deref(), Some(key.as_str()));
    assert_eq!(row.recording_filename.as_deref(), Some("mine.mp3"));
    assert_eq!(soundcloud_reads(&state, &r2, &key).await, 0);
    for (caller, want) in [
        (None, NOBODY),
        (Some(p.guest), DENIED),
        (Some(p.host), DENIED),
    ] {
        assert_eq!(recording_small(&state, caller, 2, "evil.mp3").await.0, want);
    }
    assert_eq!(show_row(&state, 2).await.unwrap().recording_key, None);

    // In chunks.
    let (code, init) = recording_init(&state, p.host, 1).await;
    assert_eq!(code, OK);
    let session = init["session_id"].as_str().unwrap().to_string();
    assert_eq!(
        recording_chunk(&state, p.other_host, 1, &session).await,
        DENIED
    );
    assert_eq!(recording_chunk(&state, p.host, 1, &session).await, OK);
    assert_eq!(
        recording_finalize(&state, p.other_host, 1, &session)
            .await
            .0,
        DENIED
    );
    let (code, done) = recording_finalize(&state, p.host, 1, &session).await;
    assert_eq!(code, OK);
    let key = done["key"].as_str().unwrap().to_string();
    assert_eq!(
        show_row(&state, 1).await.unwrap().recording_key.as_deref(),
        Some(key.as_str())
    );
    assert_eq!(soundcloud_reads(&state, &r2, &key).await, 0);
    assert_eq!(recording_init(&state, p.host, 2).await.0, DENIED);
    assert_eq!(recording_init(&state, p.guest, 1).await.0, DENIED);
    assert_eq!(pending_rows(&state).await, (0, 0));

    // An admin upload to any show still starts SoundCloud in the background.
    let (code, body) = recording_small(&state, Some(p.admin), 2, "admin.mp3").await;
    assert_eq!(code, OK);
    let key = body["key"].as_str().unwrap().to_string();
    assert!(soundcloud_reads(&state, &r2, &key).await > 0);
}
