use std::sync::Arc;

use chrono::{DateTime, NaiveDate, NaiveTime, TimeZone, Utc};
use chrono_tz::Europe::Berlin;

use crate::{models, telegram_notify, AppState};

/// How long after a live show's scheduled end we wait before alerting that no
/// recording was produced — avoids false alarms for a show that just ended or a
/// finalize still in flight.
const RECORDING_ALERT_GRACE_MINS: i64 = 15;

/// If a pre-recorded show has no `end_time` recorded, how long past its
/// scheduled start we'll still auto-start it. Bounds how far a long server
/// outage can reach back and kick off a stale, long-past show.
const PRERECORDED_START_GRACE_HOURS: i64 = 6;

/// Check if any artist Instagram previews need to be sent today.
///
/// Logic:
/// - For each show in the last 30 days: compute `days_since = today - show.date`
/// - If `days_since` falls within 1..=artist_count, pick the artist at index `days_since - 1`
/// - If that artist hasn't been sent yet (telegram_artist_preview_sent_at IS NULL)
///   and has a caption (instagram_caption IS NOT NULL), send the preview
pub async fn check_artist_preview_schedule(state: Arc<AppState>) {
    let today = Utc::now().with_timezone(&Berlin).date_naive();

    // Shows from the past 30 days
    let shows: Vec<models::Show> = match sqlx::query_as(
        "SELECT * FROM shows WHERE date >= date('now', '-30 days') AND date < date('now') ORDER BY date ASC",
    )
    .fetch_all(&state.db)
    .await
    {
        Ok(rows) => rows,
        Err(e) => {
            tracing::error!("Scheduler: failed to query shows: {e}");
            return;
        }
    };

    for show in &shows {
        let show_date = match NaiveDate::parse_from_str(&show.date, "%Y-%m-%d") {
            Ok(d) => d,
            Err(e) => {
                tracing::warn!(
                    "Scheduler: cannot parse show date '{}' for show {}: {e}",
                    show.date,
                    show.id
                );
                continue;
            }
        };

        let days_since = (today - show_date).num_days();
        if days_since < 1 {
            continue;
        }

        // Fetch assigned artists in sort order
        let artists: Vec<models::Artist> = match sqlx::query_as(
            "SELECT a.* FROM artists a \
             INNER JOIN artist_show_assignments asa ON a.id = asa.artist_id \
             WHERE asa.show_id = ? ORDER BY asa.sort_order, a.name COLLATE NOCASE",
        )
        .bind(show.id)
        .fetch_all(&state.db)
        .await
        {
            Ok(rows) => rows,
            Err(e) => {
                tracing::error!(
                    "Scheduler: failed to query artists for show {}: {e}",
                    show.id
                );
                continue;
            }
        };

        let artist_count = artists.len() as i64;
        if days_since > artist_count {
            continue; // Past the last artist for this show
        }

        let idx = (days_since - 1) as usize;
        let artist = &artists[idx];

        // Guard: already sent
        if artist.telegram_artist_preview_sent_at.is_some() {
            tracing::debug!(
                "Scheduler: artist {} (show {}) already sent, skipping",
                artist.id,
                show.id
            );
            continue;
        }

        // Guard: no caption
        if artist.instagram_caption.is_none() {
            tracing::warn!(
                "Scheduler: artist {} '{}' has no instagram_caption, skipping preview for show {}",
                artist.id,
                artist.name,
                show.id
            );
            continue;
        }

        tracing::info!(
            "Scheduler: sending preview for artist {} '{}' (day {} of show {} '{}')",
            artist.id,
            artist.name,
            days_since,
            show.id,
            show.title
        );

        match telegram_notify::send_artist_instagram_preview(&state, artist).await {
            Ok(()) => {
                tracing::info!(
                    "Scheduler: preview sent for artist {} '{}'",
                    artist.id,
                    artist.name
                );
            }
            Err(e) => {
                tracing::error!(
                    "Scheduler: failed to send preview for artist {} '{}': {e}",
                    artist.id,
                    artist.name
                );
            }
        }
    }
}

/// Parse an "HH:MM" string into a `NaiveTime`.
fn parse_hhmm(s: &str) -> Option<NaiveTime> {
    NaiveTime::parse_from_str(s, "%H:%M").ok()
}

/// Compute a show's scheduled start as a UTC instant, interpreting `date`+`start_time`
/// in Europe/Berlin. Returns `None` if the date/time can't be parsed or the
/// local time is invalid (DST gap).
fn show_start_utc(date: &str, start_time: &str) -> Option<DateTime<Utc>> {
    let day = NaiveDate::parse_from_str(date, "%Y-%m-%d").ok()?;
    let start = parse_hhmm(start_time)?;
    Berlin
        .from_local_datetime(&day.and_time(start))
        .single()
        .map(|dt| dt.with_timezone(&Utc))
}

/// Compute a show's scheduled end as a UTC instant, interpreting `date`+`end_time`
/// in Europe/Berlin. Handles overnight shows (end ≤ start → next calendar day).
/// Returns `None` if the date/time can't be parsed or the local time is invalid
/// (DST gap).
fn show_end_utc(date: &str, start_time: Option<&str>, end_time: &str) -> Option<DateTime<Utc>> {
    let day = NaiveDate::parse_from_str(date, "%Y-%m-%d").ok()?;
    let end = parse_hhmm(end_time)?;
    let mut end_dt = day.and_time(end);
    if let Some(start) = start_time.and_then(parse_hhmm) {
        if end <= start {
            end_dt += chrono::Duration::days(1);
        }
    }
    Berlin
        .from_local_datetime(&end_dt)
        .single()
        .map(|dt| dt.with_timezone(&Utc))
}

/// True if the show's scheduled end is at least `grace_mins` in the past relative
/// to `now` — i.e. it's been long enough that a missing recording is a real miss.
/// Operates on primitives so it's pure and unit-testable.
fn recording_overdue(
    date: &str,
    start_time: Option<&str>,
    end_time: Option<&str>,
    now: DateTime<Utc>,
    grace_mins: i64,
) -> bool {
    match show_end_utc(date, start_time, end_time.unwrap_or("")) {
        Some(end) => now >= end + chrono::Duration::minutes(grace_mins),
        None => false,
    }
}

/// Dead-man's-switch: alert (exactly once) when a live show that should have been
/// recorded produced no usable recording.
///
/// A show qualifies if it is live-mode, ended at least [`RECORDING_ALERT_GRACE_MINS`]
/// ago, has no successful `recording_versions` row (statuses raw/finalizing/finalized
/// — a short/corrupt recording is marked `failed` by the upload verifier and so
/// counts as missing), and has not already been alerted. Scoped to the last 2 days
/// so enabling the feature never back-alerts the whole archive.
pub async fn check_missing_recordings(state: Arc<AppState>) {
    let now = Utc::now();

    let shows: Vec<models::Show> = match sqlx::query_as(
        "SELECT * FROM shows s \
         WHERE s.stream_mode = 'live' \
           AND s.recording_alert_sent_at IS NULL \
           AND s.end_time IS NOT NULL \
           AND s.date >= date('now', '-2 days') \
           AND NOT EXISTS ( \
             SELECT 1 FROM recording_versions rv \
             WHERE rv.show_id = s.id \
               AND rv.status IN ('raw', 'finalizing', 'finalized') \
           )",
    )
    .fetch_all(&state.db)
    .await
    {
        Ok(rows) => rows,
        Err(e) => {
            tracing::error!("Dead-man's-switch: failed to query shows: {e}");
            return;
        }
    };

    for show in &shows {
        if !recording_overdue(
            &show.date,
            show.start_time.as_deref(),
            show.end_time.as_deref(),
            now,
            RECORDING_ALERT_GRACE_MINS,
        ) {
            continue; // still live, just ended, or unparseable schedule
        }

        // A qualifying show has no successful recording, but the cause matters:
        // a capture rejected purely for being under the minimum length is benign
        // (someone tested / aborted quickly) and shouldn't read like a broken
        // recorder. Inspect the latest version row to word the alert accordingly.
        let latest: Option<(String, Option<String>)> = match sqlx::query_as(
            "SELECT status, error_message FROM recording_versions \
             WHERE show_id = ? ORDER BY id DESC LIMIT 1",
        )
        .bind(show.id)
        .fetch_optional(&state.db)
        .await
        {
            Ok(row) => row,
            Err(e) => {
                tracing::error!(
                    "Dead-man's-switch: failed to query recording versions for show {}: {e}",
                    show.id
                );
                None
            }
        };
        let too_short = latest
            .as_ref()
            .is_some_and(|(status, msg)| is_short_capture(status, msg.as_deref()));

        tracing::warn!(
            "Dead-man's-switch: show {} ('{}') ended with no usable recording (too_short={}) — alerting",
            show.id,
            show.title,
            too_short
        );

        telegram_notify::notify(
            &state,
            &missing_recording_alert(
                &show.title,
                show.id,
                &show.date,
                show.end_time.as_deref(),
                too_short,
            ),
        )
        .await;

        // Mark alerted regardless of Telegram delivery so we never spam on retry.
        if let Err(e) =
            sqlx::query("UPDATE shows SET recording_alert_sent_at = datetime('now') WHERE id = ?")
                .bind(show.id)
                .execute(&state.db)
                .await
        {
            tracing::error!(
                "Dead-man's-switch: failed to mark show {} as alerted: {e}",
                show.id
            );
        }
    }
}

/// True when a `recording_versions` row represents a capture rejected purely for
/// being under [`crate::handlers::recording::MIN_RECORDING_SECS`] (an "essentially
/// empty" broadcast), as opposed to a write failure, R2 size mismatch, or no
/// recording at all. Keyed on the shared marker so wording stays in sync.
fn is_short_capture(status: &str, error_message: Option<&str>) -> bool {
    status == "failed"
        && error_message
            .is_some_and(|m| m.contains(crate::handlers::recording::SHORT_RECORDING_MARKER))
}

/// Build the dead-man's-switch Telegram alert. A sub-threshold capture gets an
/// informational message explaining it was intentionally not archived; anything
/// else keeps the "archive may be missing — check the recorder" alarm.
fn missing_recording_alert(
    title: &str,
    id: i64,
    date: &str,
    end_time: Option<&str>,
    too_short: bool,
) -> String {
    let ending = end_time.map(|t| format!(" ending {t}")).unwrap_or_default();
    if too_short {
        format!(
            "ℹ️ No archive for show \"{title}\" (#{id}) on {date}{ending}: the live recording was under the {min}s minimum, so it was treated as empty and not archived. If you expected a full recording, check the recorder.",
            min = crate::handlers::recording::MIN_RECORDING_SECS,
        )
    } else {
        format!(
            "⚠️ No recording for show \"{title}\" (#{id}) on {date}{ending}. The live archive may be missing — check the recorder."
        )
    }
}

/// Resolve the username to stream as for a show: the directly-assigned host
/// (`host_user_id`, external/brunchtime shows), or failing that the first
/// assigned artist's linked user (UNHEARD shows). Mirrors the two lookup paths
/// in `handlers::api::resolve_user_shows`.
async fn resolve_show_host_username(state: &Arc<AppState>, show: &models::Show) -> Option<String> {
    if let Some(host_user_id) = show.host_user_id {
        if let Ok(Some(username)) =
            sqlx::query_scalar::<_, String>("SELECT username FROM users WHERE id = ?")
                .bind(host_user_id)
                .fetch_optional(&state.db)
                .await
        {
            return Some(username);
        }
    }

    sqlx::query_scalar::<_, String>(
        "SELECT u.username FROM artist_show_assignments asa \
         INNER JOIN artists a ON a.id = asa.artist_id \
         INNER JOIN users u ON u.id = a.user_id \
         WHERE asa.show_id = ? \
         ORDER BY asa.sort_order LIMIT 1",
    )
    .bind(show.id)
    .fetch_optional(&state.db)
    .await
    .ok()
    .flatten()
}

/// Auto-start a pre-recorded show's stream once its scheduled start time
/// arrives, so going live doesn't depend on an admin having the waiting-room
/// page open in a browser tab (see issue #240). Scoped to confirmed uploads
/// that haven't been started yet, and bounded so a show that's already ended
/// (or, lacking an end time, is well past its start) is never resurrected.
pub async fn check_prerecorded_show_start(state: Arc<AppState>) {
    let now = Utc::now();

    let shows: Vec<models::Show> = match sqlx::query_as(
        "SELECT * FROM shows \
         WHERE stream_mode = 'prerecorded' \
           AND prerecorded_confirmed_at IS NOT NULL \
           AND prerecorded_started_at IS NULL \
           AND start_time IS NOT NULL \
           AND date >= date('now', '-2 days')",
    )
    .fetch_all(&state.db)
    .await
    {
        Ok(rows) => rows,
        Err(e) => {
            tracing::error!("Prerecorded auto-start: failed to query shows: {e}");
            return;
        }
    };

    for show in &shows {
        let Some(start_time) = show.start_time.as_deref() else {
            continue;
        };
        let Some(start) = show_start_utc(&show.date, start_time) else {
            tracing::warn!(
                "Prerecorded auto-start: cannot parse start time for show {}",
                show.id
            );
            continue;
        };
        if now < start {
            continue; // not time yet
        }

        // Never resurrect a show that has already ended, or that's well past
        // its start with no recorded end time.
        let past_grace = match show.end_time.as_deref() {
            Some(end_time) => match show_end_utc(&show.date, Some(start_time), end_time) {
                Some(end) => now >= end,
                None => now >= start + chrono::Duration::hours(PRERECORDED_START_GRACE_HOURS),
            },
            None => now >= start + chrono::Duration::hours(PRERECORDED_START_GRACE_HOURS),
        };
        if past_grace {
            tracing::warn!(
                "Prerecorded auto-start: show {} ('{}') is stale, skipping",
                show.id,
                show.title
            );
            continue;
        }

        let Some(username) = resolve_show_host_username(&state, show).await else {
            tracing::error!(
                "Prerecorded auto-start: no host user found for show {} ('{}')",
                show.id,
                show.title
            );
            continue;
        };

        tracing::info!(
            "Prerecorded auto-start: starting show {} ('{}') for user '{}'",
            show.id,
            show.title,
            username
        );

        if let Err(e) =
            crate::handlers::api::start_prerecorded_show_stream(&state, show, &username).await
        {
            tracing::error!(
                "Prerecorded auto-start: failed to start show {} ('{}'): {e}",
                show.id,
                show.title
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn show_start_utc_handles_evening_show() {
        // 18:00 Berlin (CEST, summer) → 16:00 UTC.
        let start = show_start_utc("2026-06-01", "18:00").unwrap();
        assert_eq!(start.to_rfc3339(), "2026-06-01T16:00:00+00:00");
    }

    #[test]
    fn show_start_utc_rejects_garbage() {
        assert!(show_start_utc("not-a-date", "18:00").is_none());
        assert!(show_start_utc("2026-06-01", "nope").is_none());
    }

    #[test]
    fn show_end_utc_handles_evening_show() {
        // 20:00 Berlin (CEST, summer) → 18:00 UTC.
        let end = show_end_utc("2026-06-01", Some("18:00"), "20:00").unwrap();
        assert_eq!(end.to_rfc3339(), "2026-06-01T18:00:00+00:00");
    }

    #[test]
    fn show_end_utc_handles_overnight() {
        // 23:00 → 01:00 crosses midnight, so end is the next day 01:00 Berlin.
        let end = show_end_utc("2026-06-01", Some("23:00"), "01:00").unwrap();
        // 01:00 CEST on 2026-06-02 → 23:00 UTC on 2026-06-01.
        assert_eq!(end.to_rfc3339(), "2026-06-01T23:00:00+00:00");
    }

    #[test]
    fn show_end_utc_rejects_garbage() {
        assert!(show_end_utc("not-a-date", Some("18:00"), "20:00").is_none());
        assert!(show_end_utc("2026-06-01", None, "nope").is_none());
    }

    #[test]
    fn recording_overdue_respects_grace() {
        let end = show_end_utc("2026-06-01", Some("18:00"), "20:00").unwrap();
        // 10 min after end: within 15-min grace → not overdue.
        assert!(!recording_overdue(
            "2026-06-01",
            Some("18:00"),
            Some("20:00"),
            end + chrono::Duration::minutes(10),
            15
        ));
        // 20 min after end: past grace → overdue.
        assert!(recording_overdue(
            "2026-06-01",
            Some("18:00"),
            Some("20:00"),
            end + chrono::Duration::minutes(20),
            15
        ));
        // Unparseable schedule is never overdue (don't alert blindly).
        assert!(!recording_overdue(
            "bad",
            None,
            Some("20:00"),
            end + chrono::Duration::minutes(60),
            15
        ));
    }

    #[test]
    fn is_short_capture_only_matches_sub_threshold_failures() {
        // The exact reason the upload verifier writes for a too-short capture.
        assert!(is_short_capture(
            "failed",
            Some("recording for show 27 is only 29s — essentially empty")
        ));
        // Other failure reasons are genuine problems, not the benign short case.
        assert!(!is_short_capture(
            "failed",
            Some("R2 size mismatch after upload (local 5 != remote 0)")
        ));
        // A non-failed row is never the short case.
        assert!(!is_short_capture("raw", None));
        assert!(!is_short_capture("finalized", None));
        // Failed with no reason recorded → treat as a real gap, not too-short.
        assert!(!is_short_capture("failed", None));
    }

    #[test]
    fn missing_recording_alert_words_short_vs_missing() {
        let short =
            missing_recording_alert("phils test show", 27, "2026-07-01", Some("19:52"), true);
        assert!(short.starts_with("ℹ️"));
        assert!(short.contains("under the 60s minimum"));
        assert!(short.contains("not archived"));
        assert!(short.contains("ending 19:52"));

        let missing =
            missing_recording_alert("phils test show", 27, "2026-07-01", Some("19:52"), false);
        assert!(missing.starts_with("⚠️"));
        assert!(missing.contains("check the recorder"));
        assert!(!missing.contains("minimum"));

        // No end_time → no "ending …" suffix.
        let no_end = missing_recording_alert("x", 1, "2026-07-01", None, true);
        assert!(!no_end.contains("ending"));
    }
}
