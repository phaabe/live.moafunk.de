//! Scheduler decisions with an injected clock, against real SQLite.
use super::*;
use crate::broadcast_tests::{add_user, state};
use chrono::{DateTime, Duration, Utc};

/// 20:00-22:00 Berlin (CEST) on 2026-09-26 is 18:00-20:00 UTC.
const DATE: &str = "2026-09-26";

fn utc(rfc3339: &str) -> DateTime<Utc> {
    DateTime::parse_from_rfc3339(rfc3339)
        .unwrap()
        .with_timezone(&Utc)
}

fn start() -> DateTime<Utc> {
    utc("2026-09-26T18:00:00+00:00")
}

fn end() -> DateTime<Utc> {
    utc("2026-09-26T20:00:00+00:00")
}

/// A confirmed pre-recorded set as show 1.
async fn prerecorded_show(
    state: &Arc<AppState>,
    host: Option<i64>,
    start_time: &str,
    end_time: Option<&str>,
    started: bool,
) {
    sqlx::query(
        "INSERT INTO shows (id, title, date, start_time, end_time, show_type, host_user_id, \
         stream_mode, prerecorded_key, prerecorded_confirmed_at, prerecorded_started_at) \
         VALUES (1, 'Tape', ?, ?, ?, 'external', ?, 'prerecorded', 'shows/tape.mp3', \
         datetime('now'), CASE WHEN ? THEN datetime('now') END)",
    )
    .bind(DATE)
    .bind(start_time)
    .bind(end_time)
    .bind(host)
    .bind(started)
    .execute(&state.db)
    .await
    .unwrap();
}

/// (show 1 claimed as started, status of its occurrence)
async fn prerecorded_state(state: &Arc<AppState>) -> (bool, Option<String>) {
    sqlx::query_as(
        "SELECT prerecorded_started_at IS NOT NULL, \
                (SELECT status FROM prerecorded_occurrences WHERE show_id = 1) \
         FROM shows WHERE id = 1",
    )
    .fetch_one(&state.db)
    .await
    .unwrap()
}

async fn on_air(state: &Arc<AppState>) -> Option<String> {
    let stream = state.stream_state.lock().await;
    stream
        .is_active()
        .then(|| stream.current_user.clone())
        .flatten()
}

#[tokio::test]
async fn a_due_set_starts_once_for_its_host() {
    let (state, _temp) = state().await;
    let host = add_user(&state, "host", "host").await;
    prerecorded_show(&state, Some(host), "20:00", Some("22:00"), false).await;

    scheduler::check_prerecorded_show_start(state.clone(), start() + Duration::seconds(30)).await;
    assert_eq!(
        prerecorded_state(&state).await,
        (true, Some("started".into()))
    );
    assert_eq!(on_air(&state).await.as_deref(), Some("host"));

    // A later tick inside the window finds the show already started.
    scheduler::check_prerecorded_show_start(state.clone(), start() + Duration::minutes(1)).await;
    assert_eq!(
        prerecorded_state(&state).await,
        (true, Some("started".into()))
    );
    state.stream_state.lock().await.stop_stream().await.unwrap();
}

/// UNHEARD shows have no host_user_id: the first assigned artist's user hosts.
#[tokio::test]
async fn a_due_set_without_end_time_starts_for_the_first_artist_user() {
    let (state, _temp) = state().await;
    let musician = add_user(&state, "musician", "artist").await;
    prerecorded_show(&state, None, "20:00", None, false).await;
    sqlx::query(
        "INSERT INTO artists (id, name, pronouns, track1_name, track2_name, user_id) \
         VALUES (1, 'Band', 'they', 'a', 'b', ?)",
    )
    .bind(musician)
    .execute(&state.db)
    .await
    .unwrap();
    sqlx::query("INSERT INTO artist_show_assignments (artist_id, show_id) VALUES (1, 1)")
        .execute(&state.db)
        .await
        .unwrap();

    // No end time: still due inside the 6-hour grace after the start.
    scheduler::check_prerecorded_show_start(state.clone(), start() + Duration::hours(5)).await;
    assert_eq!(
        prerecorded_state(&state).await,
        (true, Some("started".into()))
    );
    assert_eq!(on_air(&state).await.as_deref(), Some("musician"));
    state.stream_state.lock().await.stop_stream().await.unwrap();
}

#[tokio::test]
async fn a_set_that_is_not_due_stale_hostless_or_started_is_left_alone() {
    let six_hours = Duration::hours(6);
    // (case, has host, start_time, end_time, already started, now)
    let cases = [
        (
            "not yet due",
            true,
            "20:00",
            Some("22:00"),
            false,
            start() - Duration::seconds(1),
        ),
        ("ended", true, "20:00", Some("22:00"), false, end()),
        (
            "no end time, past grace",
            true,
            "20:00",
            None,
            false,
            start() + six_hours,
        ),
        (
            "bad end time, past grace",
            true,
            "20:00",
            Some("late"),
            false,
            start() + six_hours,
        ),
        (
            "bad start time",
            true,
            "soon",
            Some("22:00"),
            false,
            start(),
        ),
        ("no host", false, "20:00", Some("22:00"), false, start()),
        (
            "already started",
            true,
            "20:00",
            Some("22:00"),
            true,
            start(),
        ),
    ];
    for (case, has_host, start_time, end_time, started, now) in cases {
        let (state, _temp) = state().await;
        let host = add_user(&state, "host", "host").await;
        prerecorded_show(
            &state,
            has_host.then_some(host),
            start_time,
            end_time,
            started,
        )
        .await;

        scheduler::check_prerecorded_show_start(state.clone(), now).await;
        assert_eq!(prerecorded_state(&state).await, (started, None), "{case}");
        assert_eq!(on_air(&state).await, None, "{case}");
    }
}

/// A start that fails releases its claims, so the next tick tries again.
#[tokio::test]
async fn a_failed_start_is_released_for_the_next_tick() {
    let (state, _temp) = state().await;
    let host = add_user(&state, "host", "host").await;
    prerecorded_show(&state, Some(host), "20:00", Some("22:00"), false).await;
    sqlx::query("UPDATE shows SET prerecorded_key = NULL WHERE id = 1")
        .execute(&state.db)
        .await
        .unwrap();

    scheduler::check_prerecorded_show_start(state.clone(), start()).await;
    assert_eq!(prerecorded_state(&state).await, (false, None));
    assert_eq!(on_air(&state).await, None);
}

/// A live show on `date`, 20:00-22:00 Berlin, with an optional latest recording.
async fn live_show(
    state: &Arc<AppState>,
    id: i64,
    date: &str,
    mode: &str,
    end_time: Option<&str>,
    recording: Option<(&str, Option<&str>)>,
) {
    sqlx::query(
        "INSERT INTO shows (id, title, date, start_time, end_time, show_type, stream_mode) \
         VALUES (?, 'Live', ?, '20:00', ?, 'external', ?)",
    )
    .bind(id)
    .bind(date)
    .bind(end_time)
    .bind(mode)
    .execute(&state.db)
    .await
    .unwrap();
    if let Some((status, error)) = recording {
        sqlx::query(
            "INSERT INTO recording_versions (show_id, version, status, error_message) \
             VALUES (?, 'v1', ?, ?)",
        )
        .bind(id)
        .bind(status)
        .bind(error)
        .execute(&state.db)
        .await
        .unwrap();
    }
}

async fn alerted(state: &Arc<AppState>, id: i64) -> Option<String> {
    sqlx::query_scalar("SELECT recording_alert_sent_at FROM shows WHERE id = ?")
        .bind(id)
        .fetch_one(&state.db)
        .await
        .unwrap()
}

#[tokio::test]
async fn missing_recording_alert_fires_once_after_the_grace() {
    let (state, _temp) = state().await;
    live_show(&state, 1, DATE, "live", Some("22:00"), None).await;

    scheduler::check_missing_recordings(state.clone(), end() + Duration::minutes(14)).await;
    assert_eq!(alerted(&state, 1).await, None, "still inside the grace");

    scheduler::check_missing_recordings(state.clone(), end() + Duration::minutes(15)).await;
    assert!(alerted(&state, 1).await.is_some());

    // Mark the first alert so a second one would overwrite it.
    sqlx::query("UPDATE shows SET recording_alert_sent_at = 'first' WHERE id = 1")
        .execute(&state.db)
        .await
        .unwrap();
    scheduler::check_missing_recordings(state.clone(), end() + Duration::hours(1)).await;
    assert_eq!(alerted(&state, 1).await.as_deref(), Some("first"));
}

#[tokio::test]
async fn missing_recording_alert_only_covers_live_shows_without_a_recording_in_scope() {
    let (state, _temp) = state().await;
    let short = "recording for show 4 is only 29s — essentially empty";
    // (id, date, mode, end_time, latest recording, alerted)
    let shows = [
        (1, "2026-09-24", "live", Some("22:00"), None, true), // 2 days back: in scope
        (2, "2026-09-23", "live", Some("22:00"), None, false), // 3 days back: out of scope
        (
            3,
            DATE,
            "live",
            Some("22:00"),
            Some(("finalized", None)),
            false,
        ),
        (
            4,
            DATE,
            "live",
            Some("22:00"),
            Some(("failed", Some(short))),
            true,
        ),
        (
            5,
            DATE,
            "live",
            Some("22:00"),
            Some(("failed", Some("R2 size mismatch"))),
            true,
        ),
        (6, DATE, "prerecorded", Some("22:00"), None, false),
        (7, DATE, "live", None, None, false),
    ];
    for (id, date, mode, end_time, recording, _) in shows {
        live_show(&state, id, date, mode, end_time, recording).await;
    }

    scheduler::check_missing_recordings(state.clone(), utc("2026-09-26T21:00:00+00:00")).await;
    for (id, _, _, _, _, expected) in shows {
        assert_eq!(alerted(&state, id).await.is_some(), expected, "show {id}");
    }
}

#[tokio::test]
async fn a_database_error_ends_both_checks_quietly() {
    let (state, _temp) = state().await;
    state.db.close().await;
    scheduler::check_missing_recordings(state.clone(), end()).await;
    scheduler::check_prerecorded_show_start(state.clone(), start()).await;
}
