//! Co-hosts of a Show and of one episode, against real handlers and SQLite.

use super::*;
use crate::broadcast_tests::{add_user, headers, state};
use crate::series_tests::{create, episode, form, get, json_of, list, BRUNCHTIME};
use axum::{extract::Path, Json};
use serde_json::{json, Value};

async fn user(state: &Arc<AppState>, id: i64) -> models::User {
    sqlx::query_as("SELECT * FROM users WHERE id = ?")
        .bind(id)
        .fetch_one(&state.db)
        .await
        .unwrap()
}

/// May the user go live with this episode?
async fn may_broadcast(state: &Arc<AppState>, user_id: i64, show: i64) -> bool {
    auth::authorize_broadcast(&state.db, &user(state, user_id).await, Some(show), false)
        .await
        .is_ok()
}

fn body(user_id: i64) -> Json<handlers::hosts::AddHostRequest> {
    Json(serde_json::from_value(json!({ "user_id": user_id })).unwrap())
}

async fn add_series_host(state: &Arc<AppState>, by: i64, user_id: i64) -> Result<Value> {
    let response = handlers::hosts::api_add_series_host(
        State(state.clone()),
        Path("brunchtime".to_string()),
        headers(state, by).await,
        body(user_id),
    )
    .await?;
    Ok(json_of(response).await.1)
}

async fn remove_series_host(state: &Arc<AppState>, by: i64, user_id: i64) -> Result<Value> {
    let response = handlers::hosts::api_remove_series_host(
        State(state.clone()),
        Path(("brunchtime".to_string(), user_id)),
        headers(state, by).await,
    )
    .await?;
    Ok(json_of(response).await.1)
}

async fn add_episode_host(
    state: &Arc<AppState>,
    by: i64,
    show: i64,
    user_id: i64,
) -> Result<Value> {
    let response = handlers::hosts::api_add_episode_host(
        State(state.clone()),
        Path(show),
        headers(state, by).await,
        body(user_id),
    )
    .await?;
    Ok(json_of(response).await.1)
}

async fn remove_episode_host(
    state: &Arc<AppState>,
    by: i64,
    show: i64,
    user_id: i64,
) -> Result<Value> {
    let response = handlers::hosts::api_remove_episode_host(
        State(state.clone()),
        Path((show, user_id)),
        headers(state, by).await,
    )
    .await?;
    Ok(json_of(response).await.1)
}

async fn show_detail(state: &Arc<AppState>, by: i64, show: i64) -> Value {
    let response =
        handlers::api::api_show_detail(State(state.clone()), Path(show), headers(state, by).await)
            .await
            .unwrap();
    json_of(response).await.1
}

/// (username, role) of each host, in order.
fn names(hosts: &Value) -> Vec<(String, String)> {
    hosts
        .as_array()
        .unwrap()
        .iter()
        .map(|h| {
            (
                h["username"].as_str().unwrap().to_string(),
                h["role"].as_str().unwrap().to_string(),
            )
        })
        .collect()
}

fn pairs(expected: &[(&str, &str)]) -> Vec<(String, String)> {
    expected
        .iter()
        .map(|(u, r)| (u.to_string(), r.to_string()))
        .collect()
}

async fn count(state: &Arc<AppState>, sql: &str) -> i64 {
    sqlx::query_scalar(sql).fetch_one(&state.db).await.unwrap()
}

/// A Show co-host runs the Show and its episodes like the owner, but only the
/// owner changes who hosts it. A co-host may leave.
#[tokio::test]
async fn show_cohost_has_the_owners_rights_except_the_host_list() {
    let (state, _temp) = state().await;
    let lena = add_user(&state, "lena", "host").await;
    let jonas = add_user(&state, "jonas", "host").await;
    let mira = add_user(&state, "mira", "host").await;
    let series = create(&state, lena, BRUNCHTIME).await.unwrap()["id"]
        .as_i64()
        .unwrap();
    let ep = episode(&state, series, "2099-01-04", "live").await;

    assert!(!may_broadcast(&state, jonas, ep).await);
    assert!(matches!(
        add_series_host(&state, jonas, jonas).await,
        Err(AppError::Forbidden(_))
    ));

    let hosts = add_series_host(&state, lena, jonas).await.unwrap();
    assert_eq!(
        hosts,
        json!([
            {"user_id": lena, "username": "lena", "role": "owner"},
            {"user_id": jonas, "username": "jonas", "role": "cohost"},
        ])
    );

    // Jonas runs the Show now: list, edit, book, cancel, go live.
    assert!(may_broadcast(&state, jonas, ep).await);
    assert_eq!(list(&state, jonas).await["series"][0]["slug"], "brunchtime");
    let shown = get(&state, jonas, "brunchtime").await.unwrap();
    assert_eq!(shown["can_edit"], true);
    assert_eq!(
        names(&shown["hosts"]),
        pairs(&[("lena", "owner"), ("jonas", "cohost")])
    );
    handlers::series::api_update_series(
        State(state.clone()),
        Path("brunchtime".to_string()),
        headers(&state, jonas).await,
        form(&[("name", "Brunch Club"), ("repeat_rule", "none")]).await,
    )
    .await
    .unwrap();
    let jonas_headers = headers(&state, jonas).await;
    assert!(
        handlers::api::require_show_manager(&state, &jonas_headers, ep)
            .await
            .is_ok()
    );

    // Only the owner changes who hosts the Show.
    assert!(matches!(
        add_series_host(&state, jonas, mira).await,
        Err(AppError::Forbidden(_))
    ));
    assert!(matches!(
        remove_series_host(&state, mira, jonas).await,
        Err(AppError::Forbidden(_))
    ));
    assert!(!may_broadcast(&state, mira, ep).await);

    // Jonas leaves: he is a viewer again.
    let hosts = remove_series_host(&state, jonas, jonas).await.unwrap();
    assert_eq!(names(&hosts), pairs(&[("lena", "owner")]));
    assert!(!may_broadcast(&state, jonas, ep).await);
    assert_eq!(
        get(&state, jonas, "brunchtime").await.unwrap()["can_edit"],
        false
    );
    assert_eq!(list(&state, jonas).await["series"], json!([]));
}

/// An episode co-host runs that one episode, but cannot cancel it or change
/// who hosts it, and the Show stays read-only. They may leave.
#[tokio::test]
async fn episode_cohost_runs_one_episode_but_does_not_manage_it() {
    let (state, _temp) = state().await;
    let lena = add_user(&state, "lena", "host").await;
    let jonas = add_user(&state, "jonas", "host").await;
    let mira = add_user(&state, "mira", "host").await;
    let kai = add_user(&state, "kai", "host").await;
    let series = create(&state, lena, BRUNCHTIME).await.unwrap()["id"]
        .as_i64()
        .unwrap();
    let ep = episode(&state, series, "2099-01-04", "live").await;
    let other_ep = episode(&state, series, "2099-01-11", "live").await;
    add_series_host(&state, lena, jonas).await.unwrap();

    // A Show co-host may add an episode co-host.
    let hosts = add_episode_host(&state, jonas, ep, mira).await.unwrap();
    assert_eq!(
        names(&hosts),
        pairs(&[("lena", "owner"), ("jonas", "cohost"), ("mira", "episode")])
    );

    assert!(may_broadcast(&state, mira, ep).await);
    assert!(!may_broadcast(&state, mira, other_ep).await);
    let mira_headers = headers(&state, mira).await;
    assert!(
        handlers::api::require_show_editor(&state, &mira_headers, ep)
            .await
            .is_ok()
    );
    assert!(matches!(
        handlers::booking::api_cancel_booking(State(state.clone()), Path(ep), mira_headers).await,
        Err(AppError::Forbidden(_))
    ));
    assert!(matches!(
        add_episode_host(&state, mira, ep, kai).await,
        Err(AppError::Forbidden(_))
    ));
    assert!(matches!(
        remove_episode_host(&state, kai, ep, mira).await,
        Err(AppError::Forbidden(_))
    ));
    assert_eq!(
        get(&state, mira, "brunchtime").await.unwrap()["can_edit"],
        false
    );
    // Mira may move her episode, so she sees the free slots; Kai does not.
    for (user, allowed) in [(mira, true), (kai, false)] {
        let result = handlers::booking::api_series_slots(
            State(state.clone()),
            Path("brunchtime".to_string()),
            headers(&state, user).await,
        )
        .await;
        assert_eq!(result.is_ok(), allowed);
    }

    // The episode detail tells each viewer what they may do.
    let detail = show_detail(&state, mira, ep).await;
    assert_eq!(
        (detail["can_edit"].clone(), detail["can_manage"].clone()),
        (json!(true), json!(false))
    );
    assert_eq!(names(&detail["hosts"]), names(&hosts));
    let detail = show_detail(&state, jonas, ep).await;
    assert_eq!(
        (detail["can_edit"].clone(), detail["can_manage"].clone()),
        (json!(true), json!(true))
    );
    let detail = show_detail(&state, kai, ep).await;
    assert_eq!(
        (detail["can_edit"].clone(), detail["can_manage"].clone()),
        (json!(false), json!(false))
    );

    // Mira leaves the episode.
    let hosts = remove_episode_host(&state, mira, ep, mira).await.unwrap();
    assert_eq!(
        names(&hosts),
        pairs(&[("lena", "owner"), ("jonas", "cohost")])
    );
    assert!(!may_broadcast(&state, mira, ep).await);
}

/// Add host takes active host accounts only, and nobody hosts twice.
#[tokio::test]
async fn only_host_accounts_are_added_and_only_once() {
    let (state, _temp) = state().await;
    let lena = add_user(&state, "lena", "host").await;
    let jonas = add_user(&state, "jonas", "host").await;
    let guest = add_user(&state, "guest", "guest").await;
    let admin = add_user(&state, "admin", "admin").await;
    let old = add_user(&state, "old", "host").await;
    sqlx::query("UPDATE users SET expires_at = '2020-01-01T00:00:00' WHERE id = ?")
        .bind(old)
        .execute(&state.db)
        .await
        .unwrap();
    let series = create(&state, lena, BRUNCHTIME).await.unwrap()["id"]
        .as_i64()
        .unwrap();
    let ep = episode(&state, series, "2099-01-04", "live").await;

    let response =
        handlers::hosts::api_list_hosts(State(state.clone()), headers(&state, lena).await)
            .await
            .unwrap();
    assert_eq!(
        json_of(response).await.1,
        json!([{"id": jonas, "username": "jonas"}, {"id": lena, "username": "lena"}])
    );

    for id in [guest, admin, old] {
        assert!(matches!(
            add_series_host(&state, lena, id).await,
            Err(AppError::BadRequest(_))
        ));
        assert!(matches!(
            add_episode_host(&state, lena, ep, id).await,
            Err(AppError::BadRequest(_))
        ));
    }

    assert!(matches!(
        add_series_host(&state, lena, lena).await,
        Err(AppError::Conflict(_))
    ));
    add_series_host(&state, lena, jonas).await.unwrap();
    assert!(matches!(
        add_series_host(&state, lena, jonas).await,
        Err(AppError::Conflict(_))
    ));
    // A Show co-host already hosts every episode.
    assert!(matches!(
        add_episode_host(&state, lena, ep, jonas).await,
        Err(AppError::Conflict(_))
    ));
    // The owner is not a co-host and cannot leave.
    assert!(matches!(
        remove_series_host(&state, lena, lena).await,
        Err(AppError::NotFound(_))
    ));
}

/// Booking an episode gives no rights of its own: a removed co-host loses the
/// episodes they booked.
#[tokio::test]
async fn a_removed_cohost_loses_the_episodes_they_booked() {
    let (state, _temp) = state().await;
    let lena = add_user(&state, "lena", "host").await;
    let jonas = add_user(&state, "jonas", "host").await;
    let series = create(&state, lena, BRUNCHTIME).await.unwrap()["id"]
        .as_i64()
        .unwrap();
    add_series_host(&state, lena, jonas).await.unwrap();
    // How a booking stores it: the owner hosts, the booker created it.
    let ep = episode(&state, series, "2099-01-04", "live").await;
    sqlx::query("UPDATE shows SET host_user_id = ?, created_by = ? WHERE id = ?")
        .bind(lena)
        .bind(jonas)
        .bind(ep)
        .execute(&state.db)
        .await
        .unwrap();
    assert!(may_broadcast(&state, jonas, ep).await);

    remove_series_host(&state, lena, jonas).await.unwrap();
    assert!(!may_broadcast(&state, jonas, ep).await);
    let jonas_headers = headers(&state, jonas).await;
    assert!(matches!(
        handlers::api::require_show_editor(&state, &jonas_headers, ep).await,
        Err(AppError::Forbidden(_))
    ));
}

/// Host rows go with their user, their episode and their Show.
#[tokio::test]
async fn host_rows_go_with_their_user_episode_and_show() {
    let (state, _temp) = state().await;
    let lena = add_user(&state, "lena", "host").await;
    let jonas = add_user(&state, "jonas", "host").await;
    let mira = add_user(&state, "mira", "host").await;
    let kai = add_user(&state, "kai", "host").await;
    let series = create(&state, lena, BRUNCHTIME).await.unwrap()["id"]
        .as_i64()
        .unwrap();
    let ep = episode(&state, series, "2099-01-04", "live").await;
    add_series_host(&state, lena, jonas).await.unwrap();
    add_episode_host(&state, lena, ep, mira).await.unwrap();

    sqlx::query("DELETE FROM users WHERE id IN (?, ?)")
        .bind(jonas)
        .bind(mira)
        .execute(&state.db)
        .await
        .unwrap();
    assert_eq!(count(&state, "SELECT COUNT(*) FROM series_hosts").await, 0);
    assert_eq!(count(&state, "SELECT COUNT(*) FROM episode_hosts").await, 0);

    add_episode_host(&state, lena, ep, kai).await.unwrap();
    sqlx::query("DELETE FROM shows WHERE id = ?")
        .bind(ep)
        .execute(&state.db)
        .await
        .unwrap();
    assert_eq!(count(&state, "SELECT COUNT(*) FROM episode_hosts").await, 0);

    add_series_host(&state, lena, kai).await.unwrap();
    sqlx::query("DELETE FROM show_templates WHERE id = ?")
        .bind(series)
        .execute(&state.db)
        .await
        .unwrap();
    assert_eq!(count(&state, "SELECT COUNT(*) FROM series_hosts").await, 0);
}

/// The Dashboard names every host of an episode and of a Show.
#[tokio::test]
async fn dashboard_names_all_hosts() {
    let (state, _temp) = state().await;
    let lena = add_user(&state, "lena", "host").await;
    let jonas = add_user(&state, "jonas", "host").await;
    let mira = add_user(&state, "mira", "host").await;
    let series = create(&state, lena, BRUNCHTIME).await.unwrap()["id"]
        .as_i64()
        .unwrap();
    let ep = episode(&state, series, "2099-01-04", "live").await;
    episode(&state, series, "2099-01-11", "live").await;
    add_series_host(&state, lena, jonas).await.unwrap();
    add_episode_host(&state, lena, ep, mira).await.unwrap();

    let response =
        handlers::series::api_upcoming_episodes(State(state.clone()), headers(&state, mira).await)
            .await
            .unwrap();
    let episodes = json_of(response).await.1["episodes"].clone();
    assert_eq!(episodes[0]["hosts"], json!(["lena", "jonas", "mira"]));
    assert_eq!(episodes[1]["hosts"], json!(["lena", "jonas"]));

    let response =
        handlers::series::api_series_overview(State(state.clone()), headers(&state, mira).await)
            .await
            .unwrap();
    assert_eq!(
        json_of(response).await.1["series"][0]["hosts"],
        json!(["lena", "jonas"])
    );
}
